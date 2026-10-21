"""pilot_backend/domain/rtm_summary.py — derived, regenerable summaries.

    RTMEvidenceSummary       factual monthly derivation. Not authoritative.
    CodingAssistanceSummary  potential code candidates. Not authoritative.

## Derived means the source rows remain the truth

Neither record is a source of fact. Both are reproducible from the
`ObservationEvent`, `TimeEntry`, `SynchronousInteraction`, `TherapistReview`
and `ClinicalAction` rows that already exist, and each stores the rule version
it was generated under so a historical summary stays explainable after the
rules move on.

Regenerating mints a NEW id rather than overwriting. A clinician who read a
summary must be able to find the one they read.

## Language discipline

`distinct_observed_local_dates` is a count of distinct local dates on which an
observation was recorded. It is a FACT about the record. It is not qualifying
days, not billable days, not eligible days, and `MonitoringDay` qualification
remains deferred — there is no field here that could hold such a conclusion.

Nothing stores payer eligibility, claim eligibility, reimbursement likelihood,
reimbursement amount or a medical-necessity determination. A test asserts the
field names of both dataclasses contain no such vocabulary.

## Goal status language

`GoalStatusRecommendation` has five members and `ACHIEVED` is not one of them.
Activity completion does not establish goal achievement, and Genex does not
generate that claim. A clinician who independently documents achievement does
so in `ClinicalAction.narrative`, as clinician-authored interpretation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

from ..coding.rules import (
    CODING_RULE_SET_ID,
    CODING_RULE_VERSION,
    CodeCandidate,
    ConfirmationStatus,
    MissingRequirement,
)
from .entities import SCHEMA_VERSION, utc_now
from .enums import Visibility
from .ids import new_coding_assistance_id, new_rtm_evidence_summary_id
from .rtm import RegulatoryStatus

#: Bumped when the derivation changes. Stored on every summary.
EVIDENCE_RULE_VERSION = "rtm-evidence-derivation-2026-v1"


class SummaryError(ValueError):
    """Invalid summary construction. PHI-safe."""

    PHI_SAFE_MESSAGE = True


class GoalStatusRecommendation(str, Enum):
    """Genex-generated status language. ACHIEVED is deliberately absent.

    Activity completion does not establish goal achievement. There is no
    member a rule could set to claim it, so the claim cannot be generated —
    only authored by a clinician, in their own narrative.
    """

    CONTINUE = "continue"
    PROGRESS = "progress"
    MODIFY = "modify"
    REPLACE = "replace"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True)
class GoalEvidenceLine:
    """Per-goal attributed evidence. These counts OVERLAP across goals.

    ## event_ids is the line's SOURCE, not a decoration

    Slice 3B. `weekly/counting.summarize` already computes exactly which
    `ObservationEvent` ids it attributed to this goal, and the derivation used
    to discard them — so "which events support this goal's four attempts?" was
    recomputable but never recorded, and a stored summary could not be audited
    against the evidence it claimed to summarise.

    They are carried as the SAME ids the counts were derived from, in the same
    pass. Nothing reconstructs them afterwards from counts, prose or a second
    query: a reconstruction could agree with the number while naming different
    events, which is the failure this field exists to prevent.
    """

    goal_kind: str
    goal_id: str
    attributed_attempts: int
    attributed_completions: int
    scheduled_opportunities: int
    status_recommendation: GoalStatusRecommendation = \
        GoalStatusRecommendation.INSUFFICIENT_EVIDENCE
    #: Distinct `ObservationEvent` ids attributed to this goal. OVERLAPS with
    #: other lines by design — one activity serving two goals contributes its
    #: event id to both, which is why the per-goal figures must never be summed.
    event_ids: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_ids", tuple(self.event_ids))
        if len(set(self.event_ids)) != len(self.event_ids):
            raise SummaryError(
                "a goal evidence line cannot name the same event twice")
        if self.event_ids:
            # `summarize` increments `attempted` once per DISTINCT event and
            # appends that same id once, so for THIS count the number is
            # literally the cardinality of the source set. Asserted rather
            # than trusted.
            if self.attributed_attempts != len(self.event_ids):
                raise SummaryError(
                    "attributed_attempts must equal the number of source "
                    "events this line names")
            # Completions are a SUBSET of attempts, not a second set. A bound,
            # deliberately not an equality.
            if self.attributed_completions > len(self.event_ids):
                raise SummaryError(
                    "attributed_completions cannot exceed the source events")
        # `scheduled_opportunities` gets NO invariant: it counts aligned
        # activity instances, not events, so it is not a cardinality of this
        # set and tying the two would be a false equality.
        #
        # An EMPTY `event_ids` stays legal on purpose. Summaries persisted
        # before slice 3B carry no source refs, and refusing to decode them
        # would turn a traceability improvement into data loss. New records
        # cannot be empty-with-attempts: `derive_evidence_summary` populates
        # from the same pass that produced the counts, and a test pins that no
        # derived line has attempts without ids.


@dataclass(frozen=True)
class RTMEvidenceSummary:
    """Factual monthly derivation. Regenerable, never authoritative."""

    summary_id: str
    period_id: str
    child_id: str
    generated_at: datetime
    rule_version: str = EVIDENCE_RULE_VERSION
    focus_plan_ref: str = ""
    clinical_goal_refs: Tuple[Tuple[str, str], ...] = ()
    cycle_refs: Tuple[str, ...] = ()
    #: Distinct ObservationEvent ids. The ONLY correct attempt total.
    total_distinct_observation_events: int = 0
    #: A FACT about the record: distinct local dates with an observation.
    #: NOT qualifying days, NOT billable days, NOT eligible days.
    distinct_observed_local_dates: int = 0
    did_it_count: int = 0
    wasnt_ready_yet_count: int = 0
    didnt_want_to_try_count: int = 0
    per_goal_evidence: Tuple[GoalEvidenceLine, ...] = ()
    #: Slice 3B. The distinct `ObservationEvent` ids this summary counted, for
    #: the month it covers. The SOURCE of `total_distinct_observation_events`
    #: and of the outcome tallies, carried so a stored summary can be audited
    #: against the evidence rather than only recomputed from it.
    #:
    #: A SUPERSET of the union of `per_goal_evidence` event ids: an event whose
    #: activity is aligned to no goal is still an observation that happened and
    #: is still counted here, but it appears on no goal line.
    observed_event_ids: Tuple[str, ...] = ()
    #: Events attributed to more than one goal. Published so the per-goal
    #: figures are never mistaken for an additive breakdown.
    multi_goal_overlap_count: int = 0
    therapist_review_count: int = 0
    clinical_action_count: int = 0
    documented_management_minutes: int = 0
    synchronous_interaction_count: int = 0
    real_time_interactive_communication_present: bool = False
    documentation_missing_flags: Tuple[str, ...] = ()
    technology_regulatory_status: RegulatoryStatus = RegulatoryStatus.UNDER_REVIEW
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.THERAPIST_ONLY

    #: Reproducible from source rows; the rows remain the truth.
    is_derived = True

    def __post_init__(self) -> None:
        for name in ("clinical_goal_refs", "cycle_refs", "per_goal_evidence",
                     "documentation_missing_flags", "observed_event_ids"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if self.total_distinct_observation_events < 0:
            raise SummaryError("counts cannot be negative")
        if len(set(self.observed_event_ids)) != len(self.observed_event_ids):
            raise SummaryError("observed_event_ids cannot repeat an event")
        if self.observed_event_ids:
            # `total_distinct_observation_events` is DEFINED as the count of
            # distinct observation events, so here the number is the
            # cardinality of the source set and the equality is the field's
            # own definition rather than a convenience.
            if self.total_distinct_observation_events != len(
                    self.observed_event_ids):
                raise SummaryError(
                    "total_distinct_observation_events must equal the number "
                    "of source events this summary names")
            # The outcome tallies PARTITION that set, so each is a bound and
            # together they cannot exceed it. Not three equalities: a single
            # event has exactly one outcome, so no tally equals the whole.
            tallies = (self.did_it_count + self.wasnt_ready_yet_count
                       + self.didnt_want_to_try_count)
            if tallies > len(self.observed_event_ids):
                raise SummaryError(
                    "outcome tallies cannot exceed the source events")
            # Distinct DATES cannot outnumber the events that fell on them.
            # A bound, not an equality: several events share a date.
            if self.distinct_observed_local_dates > len(
                    self.observed_event_ids):
                raise SummaryError(
                    "distinct observed dates cannot exceed the source events")
            # Every goal line's source must be drawn from this set. Without
            # this a line could cite an event the summary never counted.
            named = {eid for line in self.per_goal_evidence
                     for eid in line.event_ids}
            if not named <= set(self.observed_event_ids):
                raise SummaryError(
                    "a goal evidence line names an event this summary did "
                    "not count")
        # Empty stays legal for pre-3B stored summaries; see GoalEvidenceLine.

    @property
    def sum_of_per_goal_attempts(self) -> int:
        """Deliberately NOT a total.

        Named so nobody reaches for it by accident, and exposed so a test can
        assert it differs from `total_distinct_observation_events` whenever a
        multi-goal activity was attempted.
        """
        return sum(line.attributed_attempts for line in self.per_goal_evidence)

    @staticmethod
    def create(period_id: str, child_id: str, *,
               now: Optional[datetime] = None,
               **fields) -> "RTMEvidenceSummary":
        return RTMEvidenceSummary(
            summary_id=new_rtm_evidence_summary_id(),
            period_id=period_id,
            child_id=child_id,
            generated_at=now or utc_now(),
            **fields,
        )


@dataclass(frozen=True)
class CodingAssistanceSummary:
    """Potential code candidates for clinician confirmation. Never a decision.

    Confirming or rejecting records a DECISION beside the generated
    candidates; it never rewrites them. `potential_code_candidates`,
    `rule_explanations` and the rule-set version stay exactly as generated, so
    "what was Genex showing when the clinician confirmed?" is answerable.
    """

    coding_summary_id: str
    period_id: str
    child_id: str
    generated_at: datetime
    coding_rule_set_id: str = CODING_RULE_SET_ID
    coding_rule_version: str = CODING_RULE_VERSION
    documented_management_minutes: int = 0
    real_time_interactive_communication_present: bool = False
    synchronous_interaction_refs: Tuple[str, ...] = ()
    time_entry_refs: Tuple[str, ...] = ()
    #: (code, units) pairs. POTENTIAL candidates only.
    potential_code_candidates: Tuple[Tuple[str, int], ...] = ()
    rule_explanations: Tuple[str, ...] = ()
    missing_requirement_flags: Tuple[MissingRequirement, ...] = ()
    technology_regulatory_status: RegulatoryStatus = RegulatoryStatus.UNDER_REVIEW
    clinician_confirmation_status: ConfirmationStatus = \
        ConfirmationStatus.NOT_REVIEWED
    clinician_confirmed_by: Optional[str] = None
    clinician_confirmed_at: Optional[datetime] = None
    confirmation_note: str = ""
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.THERAPIST_ONLY

    is_derived = True
    #: Read by reporting. This summary never authorises anything.
    is_authoritative = False

    def __post_init__(self) -> None:
        for name in ("synchronous_interaction_refs", "time_entry_refs",
                     "potential_code_candidates", "rule_explanations",
                     "missing_requirement_flags"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        codes = {code for code, _ in self.potential_code_candidates}
        if "98979" in codes and (codes & {"98980", "98981"}):
            raise SummaryError(
                "98979 is mutually exclusive with 98980 and 98981")
        if "98981" in codes and "98980" not in codes:
            raise SummaryError("98981 cannot appear without 98980")

    @property
    def candidates(self) -> Tuple[CodeCandidate, ...]:
        return tuple(CodeCandidate(code, units)
                     for code, units in self.potential_code_candidates)

    @property
    def requires_clinician_confirmation(self) -> bool:
        """Always True. Confirmation does not make it authoritative either."""
        return True

    @staticmethod
    def create(period_id: str, child_id: str, *,
               now: Optional[datetime] = None,
               **fields) -> "CodingAssistanceSummary":
        return CodingAssistanceSummary(
            coding_summary_id=new_coding_assistance_id(),
            period_id=period_id,
            child_id=child_id,
            generated_at=now or utc_now(),
            **fields,
        )

    def with_decision(self, status: ConfirmationStatus, *, actor_id: str,
                      note: str = "",
                      now: Optional[datetime] = None
                      ) -> "CodingAssistanceSummary":
        """Record the clinician's decision WITHOUT touching what was generated.

        Only the decision fields move. The candidates, explanations, flags and
        rule version are left exactly as produced, because the record has to
        answer what the clinician was looking at when they decided.
        """
        from dataclasses import replace

        if status is ConfirmationStatus.NOT_REVIEWED:
            raise SummaryError("a decision must be CONFIRMED or REJECTED")
        if self.clinician_confirmation_status is not ConfirmationStatus.NOT_REVIEWED:
            raise SummaryError("this summary has already been decided")
        if not (actor_id or "").strip():
            raise SummaryError("a decision requires an actor")
        return replace(self, clinician_confirmation_status=status,
                       clinician_confirmed_by=actor_id,
                       clinician_confirmed_at=now or utc_now(),
                       confirmation_note=note)
