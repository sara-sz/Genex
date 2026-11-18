"""pilot_backend/weekly/repeated_signal_policy.py — P0-B1 repeated-signal policy.

Pure and deterministic. No model, no prompt, no network, no clock, no store:
the same observations always produce the same decisions.

## The unit of "consecutive" (founder decision, P0-B1)

ONE activity, across DIFFERENT practice dates. For each previous-week activity
identity, its CURRENT observations are ordered by practice date, then time, then
event id. A rule fires only on the LAST TWO of them: taking literally the last
two means nothing different can sit between them. Two observations on the same
practice date are not "consecutive across days" and fire nothing.

Signals are NOT pooled across different activities toward the whole goal. That
generalisation is deliberately out of scope.

## Only current observations count

`current_observations()` is applied first. A corrected submission leaves two
immutable rows, and counting the superseded one would let a single practice look
like two — which is exactly how one disengaged afternoon would become a
"repeated" signal.

## The table

    last two share a disengagement signal       -> CHANGE_THEME
        (didnt_want_to_try, or enjoyment disliked — Parent's "not really")
    last two both too_hard                       -> NO_CHANGE  not_yet_supported
    last two both too_easy                       -> NO_CHANGE  not_yet_supported
    last two both wasnt_ready_yet                -> NO_CHANGE  not_yet_supported
    anything else, including ONE of any signal   -> MAINTAIN_VARY

Precedence is the order above, so disengagement — the one branch P0-B1
implements — is never masked by a not-yet-supported one.

## What this policy can never do

It has no path to the old easier/harder directions. It never reads `too_easy`
as mastery and never touches a goal, a version, an anchor or a family: its only
output is a branch per previous-week ACTIVITY, and the release composition
enforces the clinical boundary before anything is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Sequence, Tuple

from ..domain.adaptation_decision import (
    P0B_RULE_VERSION,
    ActivityAdaptationDecision,
    AdaptationBranch,
    DecisionReason,
)
from ..domain.alignment import ActivityGoalAlignment
from ..domain.observation import (
    AttemptOutcome,
    Difficulty,
    Enjoyment,
    ObservationEvent,
    current_observations,
)

#: The disengagement signals. Named by their canonical Pilot value, so the
#: shared-signal test compares the same vocabulary the observation stores.
SIGNAL_DIDNT_WANT_TO_TRY = AttemptOutcome.DIDNT_WANT_TO_TRY.value
SIGNAL_DISLIKED = Enjoyment.DISLIKED.value


class RepeatedSignalPolicyError(ValueError):
    """The policy refused its inputs. PHI-safe."""

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class PolicyOutcome:
    """Every previous-week activity's decision, plus the evidence boundary."""

    decisions: Tuple[ActivityAdaptationDecision, ...]
    current_event_ids: Tuple[str, ...]
    superseded_event_ids: Tuple[str, ...]
    rule_version: str = P0B_RULE_VERSION

    def decision_for(self, identity_ref: str) -> ActivityAdaptationDecision:
        for decision in self.decisions:
            if decision.activity_identity_ref == identity_ref:
                return decision
        raise KeyError(identity_ref)

    @property
    def changes_theme(self) -> bool:
        return any(d.branch is AdaptationBranch.CHANGE_THEME
                   for d in self.decisions)


def disengagement_signals(event: ObservationEvent) -> FrozenSet[str]:
    """The disengagement signals ONE observation carries. Possibly none."""
    found = set()
    if event.attempt_outcome is AttemptOutcome.DIDNT_WANT_TO_TRY:
        found.add(SIGNAL_DIDNT_WANT_TO_TRY)
    if event.enjoyment is Enjoyment.DISLIKED:
        found.add(SIGNAL_DISLIKED)
    return frozenset(found)


def _ordered(events: Sequence[ObservationEvent]) -> List[ObservationEvent]:
    return sorted(events, key=lambda e: (e.local_date, e.occurred_at,
                                         e.event_id))


def _decide_one(identity: str, events: Sequence[ObservationEvent]
                ) -> ActivityAdaptationDecision:
    ordered = _ordered(events)
    considered = tuple(e.event_id for e in ordered)
    untriggered = ActivityAdaptationDecision(
        activity_identity_ref=identity,
        branch=AdaptationBranch.MAINTAIN_VARY,
        reason=DecisionReason.NO_TRIGGER,
        considered_event_ids=considered)
    if len(ordered) < 2:
        return untriggered
    older, newer = ordered[-2], ordered[-1]
    if older.local_date == newer.local_date:
        # Same practice date: not "across different practice dates".
        return untriggered
    pair = (older.event_id, newer.event_id)

    shared = disengagement_signals(older) & disengagement_signals(newer)
    if shared:
        return ActivityAdaptationDecision(
            activity_identity_ref=identity,
            branch=AdaptationBranch.CHANGE_THEME,
            reason=DecisionReason.REPEATED_DISENGAGEMENT,
            signal="+".join(sorted(shared)),
            trigger_event_ids=pair, considered_event_ids=considered)

    for reason, holds in (
            (DecisionReason.NOT_YET_SUPPORTED_REPEATED_TOO_HARD,
             older.difficulty is Difficulty.TOO_HARD
             and newer.difficulty is Difficulty.TOO_HARD),
            (DecisionReason.NOT_YET_SUPPORTED_REPEATED_TOO_EASY,
             older.difficulty is Difficulty.TOO_EASY
             and newer.difficulty is Difficulty.TOO_EASY),
            (DecisionReason.NOT_YET_SUPPORTED_REPEATED_WASNT_READY,
             older.attempt_outcome is AttemptOutcome.WASNT_READY_YET
             and newer.attempt_outcome is AttemptOutcome.WASNT_READY_YET)):
        if holds:
            return ActivityAdaptationDecision(
                activity_identity_ref=identity,
                branch=AdaptationBranch.NO_CHANGE, reason=reason,
                signal=reason.value.split(":", 1)[1],
                trigger_event_ids=pair, considered_event_ids=considered)
    return untriggered


def decide(alignments: Sequence[ActivityGoalAlignment],
           events: Sequence[ObservationEvent]) -> PolicyOutcome:
    """One decision per previous-week activity identity. Pure.

    Every aligned identity gets a decision, observed or not, so the outcome
    covers the whole previous week. An observation on an instance the week did
    not schedule is REFUSED rather than ignored: the recording path already
    forbids it, so its presence means the inputs are not what they claim.
    """
    identity_by_instance: Dict[str, str] = {
        a.activity_instance_ref: a.activity_identity_ref for a in alignments}
    current = current_observations(tuple(events))
    current_ids = {e.event_id for e in current}
    superseded = tuple(sorted(e.event_id for e in events
                              if e.event_id not in current_ids))

    by_identity: Dict[str, List[ObservationEvent]] = {
        identity: [] for identity in identity_by_instance.values()}
    for event in current:
        identity = identity_by_instance.get(event.activity_instance_ref)
        if not identity:
            raise RepeatedSignalPolicyError(
                "an observation names an activity the week did not schedule")
        by_identity[identity].append(event)

    decisions = tuple(_decide_one(identity, by_identity[identity])
                      for identity in sorted(by_identity))
    return PolicyOutcome(
        decisions=decisions,
        current_event_ids=tuple(sorted(current_ids)),
        superseded_event_ids=superseded)


__all__ = [
    "PolicyOutcome",
    "RepeatedSignalPolicyError",
    "SIGNAL_DIDNT_WANT_TO_TRY",
    "SIGNAL_DISLIKED",
    "decide",
    "disengagement_signals",
]
