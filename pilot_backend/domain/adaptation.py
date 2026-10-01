"""pilot_backend/domain/adaptation.py — why Week N+1 differs from Week N.

    NormalizedSignal  one classified input, with its provenance kept
    AdaptationRecord  the immutable answer to "why did the plan change?"

## The signal namespace keeps sources separable

Parent evidence and clinician decisions both change next week, and they must
never be confused for one another. So `SignalSource` is stored beside every
signal and `SignalKind` uses a prefix per origin:

    child_ / plan_       from caregiver observation and family edits
    clinician_           from a TherapistIntervention
    parent_declined_     a decision about a proposal, its own category

A clinician replacing an activity and a child finding it too hard produce
different next weeks and different conversations. `not_a_failure` is carried
on the record and on every clinician-origin signal so a downstream reader
cannot present one as the other.

## Conservative by construction

The mapping is a table, not a judgement:

    too_hard | wasnt_ready_yet | didnt_want_to_try  -> EASIER_OR_MORE_SUPPORT
    too_easy AND did_it                             -> HARDER_OR_PROGRESSED
    anything else                                   -> MAINTAIN

`MAINTAIN` is the default, and unmappable feedback stays unmapped rather than
being invented into a domain-level signal. Nothing here infers mastery: a
`did_it` alone is not progression — it needs `too_easy` beside it, and even
then the output is a harder VARIANT, not a claim about the child.

No model, no prompt, no network. `weekly/adaptation.py` is checked by the same
AST import scan that guards the 0.4B/C suggestion engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

from .entities import SCHEMA_VERSION, utc_now
from .enums import Visibility
from .ids import new_adaptation_record_id


class AdaptationError(ValueError):
    """Invalid adaptation record. PHI-safe."""

    PHI_SAFE_MESSAGE = True


class SignalSource(str, Enum):
    """Where a normalized signal came from. Never inferred, always stored."""

    CAREGIVER_OBSERVATION = "caregiver_observation"
    PARENT_CUSTOMIZATION = "parent_customization"
    CAREGIVER_DEFER = "caregiver_defer"
    CLINICIAN_INTERVENTION = "clinician_intervention"
    PLANNER_CONDITION = "planner_condition"


class SignalKind(str, Enum):
    """The classified meaning of one input.

    Prefixed by origin so a clinician decision can never be read as child
    performance by a caller that forgot to check `source`.
    """

    # --- from caregiver observation -------------------------------------
    CHILD_FOUND_IT_HARD = "child_found_it_hard"
    CHILD_WASNT_READY = "child_wasnt_ready"
    CHILD_DIDNT_WANT_TO_TRY = "child_didnt_want_to_try"
    CHILD_FOUND_IT_EASY = "child_found_it_easy"
    CHILD_COMPLETED = "child_completed"

    # --- from family plan edits -----------------------------------------
    PLAN_DEFERRED_BY_CAREGIVER = "plan_deferred_by_caregiver"
    PLAN_REMOVED_BY_CAREGIVER = "plan_removed_by_caregiver"
    PLAN_SWAPPED_BY_CAREGIVER = "plan_swapped_by_caregiver"
    PLAN_SIMPLIFIED_BY_CAREGIVER = "plan_simplified_by_caregiver"
    PLAN_INTENSIFIED_BY_CAREGIVER = "plan_intensified_by_caregiver"
    PLAN_ADDED_BY_CAREGIVER = "plan_added_by_caregiver"

    # --- from clinician decisions (section 21) ---------------------------
    CLINICIAN_ENDORSED = "clinician_endorsed"
    CLINICIAN_MODIFIED = "clinician_modified"
    CLINICIAN_ADDED = "clinician_added"
    CLINICIAN_REPLACED = "clinician_replaced"
    CLINICIAN_GUIDANCE = "clinician_guidance"
    CLINICIAN_DEFERRED = "clinician_deferred"
    CLINICIAN_REMOVED_FROM_SCHEDULING = "clinician_removed_from_scheduling"

    # --- section 22: a decision about a proposal, not a failure ----------
    PARENT_DECLINED_CHANGE = "parent_declined_change"

    # --- planner conditions ----------------------------------------------
    COVERAGE_GAP_RECORDED = "coverage_gap_recorded"


#: Signals that originate in a clinical or family DECISION rather than in
#: something the child did. A difference driven only by these must never be
#: described as the child struggling.
NON_PERFORMANCE_SIGNALS = frozenset({
    SignalKind.PLAN_DEFERRED_BY_CAREGIVER,
    SignalKind.PLAN_REMOVED_BY_CAREGIVER,
    SignalKind.PLAN_SWAPPED_BY_CAREGIVER,
    SignalKind.PLAN_SIMPLIFIED_BY_CAREGIVER,
    SignalKind.PLAN_INTENSIFIED_BY_CAREGIVER,
    SignalKind.PLAN_ADDED_BY_CAREGIVER,
    SignalKind.CLINICIAN_ENDORSED,
    SignalKind.CLINICIAN_MODIFIED,
    SignalKind.CLINICIAN_ADDED,
    SignalKind.CLINICIAN_REPLACED,
    SignalKind.CLINICIAN_GUIDANCE,
    SignalKind.CLINICIAN_DEFERRED,
    SignalKind.CLINICIAN_REMOVED_FROM_SCHEDULING,
    SignalKind.PARENT_DECLINED_CHANGE,
    SignalKind.COVERAGE_GAP_RECORDED,
})

#: Caregiver observations that lean the next cycle toward more support.
SUPPORT_SIGNALS = frozenset({
    SignalKind.CHILD_FOUND_IT_HARD,
    SignalKind.CHILD_WASNT_READY,
    SignalKind.CHILD_DIDNT_WANT_TO_TRY,
})


class AdaptationDirection(str, Enum):
    """What the next cycle should do with an activity.

    `MAINTAIN` is the default and the conservative answer. Progression
    requires positive evidence; it is never the fallback.
    """

    EASIER_OR_MORE_SUPPORT = "easier_or_more_support"
    MAINTAIN = "maintain"
    HARDER_OR_PROGRESSED = "harder_or_progressed"
    SUPPRESSED = "suppressed"


class AdaptationOrigin(str, Enum):
    AUTOMATIC = "automatic"
    THERAPIST_DIRECTED = "therapist_directed"
    MIXED = "mixed"


@dataclass(frozen=True)
class NormalizedSignal:
    """One classified input with its provenance intact."""

    kind: SignalKind
    source: SignalSource
    #: The record this was derived from — an event, signal, defer or
    #: intervention id. Kept so every conclusion is traceable to a row.
    source_ref: str
    activity_identity_ref: str = ""
    goal_ref_key: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SignalKind):
            raise AdaptationError("kind must be a SignalKind")
        if not isinstance(self.source, SignalSource):
            raise AdaptationError("source must be a SignalSource")
        if not (self.source_ref or "").strip():
            raise AdaptationError("a normalized signal requires its source record")

    @property
    def is_performance_signal(self) -> bool:
        """Whether this says something about what the CHILD did."""
        return self.kind not in NON_PERFORMANCE_SIGNALS

    def as_key(self) -> str:
        return f"{self.source.value}:{self.kind.value}:{self.source_ref}"


@dataclass(frozen=True)
class AdaptationRecord:
    """Why Week N+1 differs from Week N. Immutable."""

    record_id: str
    child_id: str
    focus_plan_id: str
    from_cycle_id: str
    to_cycle_id: str
    evidence_event_ids: Tuple[str, ...] = ()
    customization_signal_ids: Tuple[str, ...] = ()
    defer_record_ids: Tuple[str, ...] = ()
    normalized_signals: Tuple[NormalizedSignal, ...] = ()
    rule_version: str = ""
    origin: AdaptationOrigin = AdaptationOrigin.AUTOMATIC
    #: Distinct sources that contributed, for a one-glance provenance read.
    signal_source: Tuple[SignalSource, ...] = ()
    clinician_decision: str = ""
    source_action_ref: str = ""
    intervention_id: Optional[str] = None
    goal_alignment_before: Tuple[str, ...] = ()
    goal_alignment_after: Tuple[str, ...] = ()
    coverage_gaps: Tuple[str, ...] = ()
    #: Always True. A plan differing between weeks is never, by itself,
    #: evidence that anyone failed — and a reader should not have to infer
    #: that from the absence of a field.
    not_a_failure: bool = True
    resulting_change: str = ""
    created_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.SYSTEM_AUDIT

    def __post_init__(self) -> None:
        if self.from_cycle_id == self.to_cycle_id:
            raise AdaptationError("an adaptation must link two different cycles")
        for label, value in (("child_id", self.child_id),
                             ("focus_plan_id", self.focus_plan_id),
                             ("to_cycle_id", self.to_cycle_id)):
            if not (value or "").strip():
                raise AdaptationError(f"an adaptation record requires {label}")
        if not self.not_a_failure:
            raise AdaptationError(
                "not_a_failure is invariant: a plan change is not a failure")
        for name in ("evidence_event_ids", "customization_signal_ids",
                     "defer_record_ids", "normalized_signals", "signal_source",
                     "goal_alignment_before", "goal_alignment_after",
                     "coverage_gaps"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if (self.origin is AdaptationOrigin.THERAPIST_DIRECTED
                and self.intervention_id is None):
            raise AdaptationError(
                "a therapist-directed adaptation must name its intervention")

    @property
    def has_performance_evidence(self) -> bool:
        """Whether ANY signal describes what the child did.

        False means the whole difference is explained by decisions — plan
        edits, clinician direction, planner conditions. A report must not
        describe such a week as the child struggling.
        """
        return any(s.is_performance_signal for s in self.normalized_signals)

    @staticmethod
    def create(child_id: str, focus_plan_id: str, from_cycle_id: str,
               to_cycle_id: str, *,
               normalized_signals: Tuple[NormalizedSignal, ...] = (),
               evidence_event_ids: Tuple[str, ...] = (),
               customization_signal_ids: Tuple[str, ...] = (),
               defer_record_ids: Tuple[str, ...] = (),
               rule_version: str = "",
               origin: AdaptationOrigin = AdaptationOrigin.AUTOMATIC,
               clinician_decision: str = "", source_action_ref: str = "",
               intervention_id: Optional[str] = None,
               goal_alignment_before: Tuple[str, ...] = (),
               goal_alignment_after: Tuple[str, ...] = (),
               coverage_gaps: Tuple[str, ...] = (),
               resulting_change: str = "",
               now: Optional[datetime] = None) -> "AdaptationRecord":
        signals = tuple(normalized_signals)
        return AdaptationRecord(
            record_id=new_adaptation_record_id(),
            child_id=child_id,
            focus_plan_id=focus_plan_id,
            from_cycle_id=from_cycle_id,
            to_cycle_id=to_cycle_id,
            evidence_event_ids=tuple(evidence_event_ids),
            customization_signal_ids=tuple(customization_signal_ids),
            defer_record_ids=tuple(defer_record_ids),
            normalized_signals=signals,
            rule_version=rule_version,
            origin=origin,
            # DERIVED from the signals rather than restated by the caller, so
            # the provenance summary cannot disagree with the signals it
            # summarises.
            signal_source=tuple(sorted({s.source for s in signals},
                                       key=lambda s: s.value)),
            clinician_decision=clinician_decision,
            source_action_ref=source_action_ref,
            intervention_id=intervention_id,
            goal_alignment_before=tuple(goal_alignment_before),
            goal_alignment_after=tuple(goal_alignment_after),
            coverage_gaps=tuple(coverage_gaps),
            resulting_change=resulting_change,
            created_at=now or utc_now(),
        )
