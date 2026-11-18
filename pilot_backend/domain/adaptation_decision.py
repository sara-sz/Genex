"""pilot_backend/domain/adaptation_decision.py — P0-B1 weekly adaptation decision.

The immutable record of WHY Week N+1 contains what it contains, and the proof
that nothing clinical moved while it changed.

## What this record is, and is not

`AdaptationRecord` (0.4E) is kept and still written: it carries normalized
signals and alignment lineage. It cannot carry this slice's per-activity
decision or its invariant proof, and adding fields to it would make every stored
row undecodable — the codec refuses any key-set mismatch. So this is a SIDECAR
in its own collection, the same pattern 0.5E-A used for canonical anchors.

## A violation is unrepresentable

The four `*_unchanged` / `families_within_anchor` booleans are not reporting
fields a caller fills in. `__post_init__` refuses to construct a decision whose
goal id, goal version or anchor rung differ before and after, or whose selected
families fall outside the anchor's bound families. So a stored decision cannot
describe a clinical change, because such a decision cannot exist in memory to be
stored. The booleans are persisted so the proof is legible in the document
itself, without re-deriving it.

## Scope of P0-B1

Genex may change only WHICH reviewed activity a family practises next week. The
branches are closed:

    CHANGE_THEME   repeated disengagement on one activity across two different
                   practice dates -> a reviewed activity with a theme not used
                   in the previous week. NOT an inference of inability.
    MAINTAIN_VARY  no trigger -> same level, a reviewed unused alternative where
                   one exists.
    NO_CHANGE      a repeated signal whose adaptation is NOT YET SUPPORTED
                   (too hard, too easy, wasn't ready) -> the same activity again,
                   with the reason recorded for a clinician. Never the old
                   easier/harder behaviour.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

from .entities import SCHEMA_VERSION, utc_now

#: The P0-B policy version. Recorded on every decision AND on the companion
#: `AdaptationRecord`, so both say which rules produced them.
P0B_RULE_VERSION = "weekly-adaptation-rules-2026.11-p0b"

WEEKLY_ADAPTATION_DECISION_PREFIX = "wadd"


class AdaptationDecisionError(ValueError):
    """A decision that would describe a clinical change, or is malformed.

    PHI-safe: names fields, never activity content or goal text.
    """

    PHI_SAFE_MESSAGE = True


class AdaptationBranch(str, Enum):
    """What Week N+1 does with ONE previous-week activity."""

    CHANGE_THEME = "change_theme"
    MAINTAIN_VARY = "maintain_vary"
    NO_CHANGE = "no_change"


class DecisionReason(str, Enum):
    """Why that branch. Closed; a new reason is a policy-version change."""

    REPEATED_DISENGAGEMENT = "repeated_disengagement"
    NO_TRIGGER = "no_trigger"
    #: The three P0-B2 triggers, recognised so they are RECORDED for a
    #: clinician rather than silently treated as "no trigger" — and so the old
    #: autonomous easier/harder response can never be what happens to them.
    NOT_YET_SUPPORTED_REPEATED_TOO_HARD = "not_yet_supported:repeated_too_hard"
    NOT_YET_SUPPORTED_REPEATED_TOO_EASY = "not_yet_supported:repeated_too_easy"
    NOT_YET_SUPPORTED_REPEATED_WASNT_READY = (
        "not_yet_supported:repeated_wasnt_ready")

    @property
    def is_not_yet_supported(self) -> bool:
        return self.value.startswith("not_yet_supported:")


#: Which reason each branch may carry. Enforced, so a CHANGE_THEME can never be
#: justified by a not-yet-supported reason and a NO_CHANGE never by a trigger
#: that P0-B1 implements.
_REASONS_BY_BRANCH = {
    AdaptationBranch.CHANGE_THEME: frozenset({
        DecisionReason.REPEATED_DISENGAGEMENT}),
    AdaptationBranch.MAINTAIN_VARY: frozenset({DecisionReason.NO_TRIGGER}),
    AdaptationBranch.NO_CHANGE: frozenset({
        DecisionReason.NOT_YET_SUPPORTED_REPEATED_TOO_HARD,
        DecisionReason.NOT_YET_SUPPORTED_REPEATED_TOO_EASY,
        DecisionReason.NOT_YET_SUPPORTED_REPEATED_WASNT_READY}),
}


@dataclass(frozen=True)
class ActivityAdaptationDecision:
    """The decision for one previous-week activity identity."""

    activity_identity_ref: str
    branch: AdaptationBranch
    reason: DecisionReason
    #: The shared signal that triggered the branch ("" when none did).
    signal: str = ""
    #: The two CURRENT observations that triggered it, oldest first ("" none).
    trigger_event_ids: Tuple[str, ...] = ()
    #: Every CURRENT observation considered for this activity, oldest first.
    considered_event_ids: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "trigger_event_ids",
                           tuple(self.trigger_event_ids))
        object.__setattr__(self, "considered_event_ids",
                           tuple(self.considered_event_ids))
        if not (self.activity_identity_ref or "").strip():
            raise AdaptationDecisionError("a decision must name its activity")
        if self.reason not in _REASONS_BY_BRANCH[self.branch]:
            raise AdaptationDecisionError(
                f"reason {self.reason.value} is not valid for branch "
                f"{self.branch.value}")
        triggered = self.reason is not DecisionReason.NO_TRIGGER
        if triggered and len(self.trigger_event_ids) != 2:
            raise AdaptationDecisionError(
                "a triggered decision must cite exactly two observations")
        if not triggered and (self.trigger_event_ids or self.signal):
            raise AdaptationDecisionError(
                "an untriggered decision cites no trigger")
        if not set(self.trigger_event_ids) <= set(self.considered_event_ids):
            raise AdaptationDecisionError(
                "a trigger must be among the considered observations")


@dataclass(frozen=True)
class GoalInvariantProof:
    """One goal, before and after. Equal by construction or not at all."""

    goal_id: str
    goal_version_before: str
    goal_version_after: str
    anchor_rung_ref_before: str
    anchor_rung_ref_after: str
    #: The anchor's bound families — the ONLY families Week N+1 may draw on.
    allowed_family_refs: Tuple[str, ...]
    #: The families Week N+1 actually drew on for this goal.
    selected_family_refs: Tuple[str, ...]
    goal_unchanged: bool = True
    goal_version_unchanged: bool = True
    anchor_unchanged: bool = True
    families_within_anchor: bool = True

    def __post_init__(self) -> None:
        for name in ("allowed_family_refs", "selected_family_refs"):
            object.__setattr__(self, name, tuple(sorted(getattr(self, name))))
        if not (self.goal_id or "").strip():
            raise AdaptationDecisionError("a proof must name its goal")
        computed = {
            "goal_unchanged": True,  # one goal id, by construction of the proof
            "goal_version_unchanged": (
                bool(self.goal_version_before)
                and self.goal_version_before == self.goal_version_after),
            "anchor_unchanged": (
                bool(self.anchor_rung_ref_before)
                and self.anchor_rung_ref_before == self.anchor_rung_ref_after),
            "families_within_anchor": (
                bool(self.allowed_family_refs)
                and set(self.selected_family_refs)
                <= set(self.allowed_family_refs)),
        }
        for name, holds in computed.items():
            if not holds:
                raise AdaptationDecisionError(
                    f"clinical boundary violated: {name}")
            if getattr(self, name) is not True:
                raise AdaptationDecisionError(
                    f"{name} must record the computed proof")


def weekly_adaptation_decision_id(to_cycle_id: str) -> str:
    """DERIVED from the cycle it explains: one decision per Week N+1, ever.

    Create-only on a derived id means a second decision for the same week is a
    collision the store refuses, not a second row that disagrees with the first.
    The cycle id is already opaque, so deriving from it leaks nothing.
    """
    if not (to_cycle_id or "").strip():
        raise AdaptationDecisionError("a decision must name the cycle it explains")
    digest = hashlib.sha256(
        f"weekly_adaptation_decision|{to_cycle_id}".encode("utf-8")).hexdigest()
    return f"{WEEKLY_ADAPTATION_DECISION_PREFIX}_{digest[:32]}"


@dataclass(frozen=True)
class WeeklyAdaptationDecision:
    """Why Week N+1 differs from Week N. Immutable, create-only."""

    decision_id: str
    child_id: str
    focus_plan_id: str
    from_cycle_id: str
    to_cycle_id: str
    adaptation_record_id: str
    rule_version: str
    activity_decisions: Tuple[ActivityAdaptationDecision, ...]
    #: Every CURRENT observation of the previous week the policy read.
    current_event_ids: Tuple[str, ...]
    #: Observations EXCLUDED because a later correction superseded them.
    superseded_event_ids: Tuple[str, ...]
    selected_template_ids: Tuple[str, ...]
    #: Previous-week templates scheduled again (NO_CHANGE carry, or a
    #: MAINTAIN_VARY top-up when no unused alternative existed).
    repeated_template_ids: Tuple[str, ...]
    #: Themes a CHANGE_THEME decision excluded. Empty when none fired.
    excluded_themes: Tuple[str, ...]
    goal_proofs: Tuple[GoalInvariantProof, ...]
    created_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("activity_decisions", "current_event_ids",
                     "superseded_event_ids", "selected_template_ids",
                     "repeated_template_ids", "excluded_themes", "goal_proofs"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if self.decision_id != weekly_adaptation_decision_id(self.to_cycle_id):
            raise AdaptationDecisionError(
                "decision id must be derived from the cycle it explains")
        if self.from_cycle_id == self.to_cycle_id:
            raise AdaptationDecisionError(
                "a decision must link two different cycles")
        if self.rule_version != P0B_RULE_VERSION:
            raise AdaptationDecisionError("unexpected adaptation rule version")
        if not self.goal_proofs:
            raise AdaptationDecisionError("a decision must prove its goals")
        if set(self.current_event_ids) & set(self.superseded_event_ids):
            raise AdaptationDecisionError(
                "an observation cannot be both current and superseded")
        cited = {e for d in self.activity_decisions
                 for e in d.considered_event_ids}
        if not cited <= set(self.current_event_ids):
            raise AdaptationDecisionError(
                "a decision may cite only current observations")
        if not self.selected_template_ids:
            raise AdaptationDecisionError("a released week selects something")
        if not set(self.repeated_template_ids) <= set(
                self.selected_template_ids):
            raise AdaptationDecisionError(
                "a repeated template must be among the selected ones")

    @property
    def branches(self) -> Tuple[AdaptationBranch, ...]:
        return tuple(d.branch for d in self.activity_decisions)

    @staticmethod
    def create(*, child_id: str, focus_plan_id: str, from_cycle_id: str,
               to_cycle_id: str, adaptation_record_id: str,
               activity_decisions, current_event_ids, superseded_event_ids,
               selected_template_ids, repeated_template_ids, excluded_themes,
               goal_proofs, now: Optional[datetime] = None
               ) -> "WeeklyAdaptationDecision":
        return WeeklyAdaptationDecision(
            decision_id=weekly_adaptation_decision_id(to_cycle_id),
            child_id=child_id, focus_plan_id=focus_plan_id,
            from_cycle_id=from_cycle_id, to_cycle_id=to_cycle_id,
            adaptation_record_id=adaptation_record_id,
            rule_version=P0B_RULE_VERSION,
            activity_decisions=tuple(sorted(
                activity_decisions, key=lambda d: d.activity_identity_ref)),
            current_event_ids=tuple(sorted(current_event_ids)),
            superseded_event_ids=tuple(sorted(superseded_event_ids)),
            selected_template_ids=tuple(sorted(selected_template_ids)),
            repeated_template_ids=tuple(sorted(repeated_template_ids)),
            excluded_themes=tuple(sorted(excluded_themes)),
            goal_proofs=tuple(sorted(goal_proofs, key=lambda p: p.goal_id)),
            created_at=now or utc_now(),
        )


__all__ = [
    "ActivityAdaptationDecision",
    "AdaptationBranch",
    "AdaptationDecisionError",
    "DecisionReason",
    "GoalInvariantProof",
    "P0B_RULE_VERSION",
    "WeeklyAdaptationDecision",
    "weekly_adaptation_decision_id",
]
