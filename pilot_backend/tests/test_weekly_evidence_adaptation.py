"""0.4D/E — weekly allocation, evidence and adaptation.

Same harness as 0.4A–C: `FirestoreRepositories` over `FakeDocumentStore`, so
these exercise the production repository code rather than a parallel in-memory
implementation that could drift. No concurrency claim is made here —
`FakeDocumentStore` is not thread-safe and the 0.4A freeze recorded that as
carried debt. Racing writers are proven only in
`pilot_runtime/tests/integration/`.

FICTIONAL ONLY. The neutral `build_secure_topology` aliases — Child-Alpha,
Caregiver-Alpha, Provider-Alpha, Family Beta — are used throughout; no
real-person-associated name appears anywhere in this suite.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pilot_backend.audit.events import ALLOWED_METADATA_KEYS, AuditAction, AuditResult
from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth import VerifiedToken, resolve_principal
from pilot_backend.domain.adaptation import (
    AdaptationDirection,
    AdaptationError,
    AdaptationOrigin,
    AdaptationRecord,
    NormalizedSignal,
    SignalKind,
    SignalSource,
)
from pilot_backend.domain.alignment import (
    ActivityGoalAlignment,
    AlignmentError,
    AlignmentRole,
    AlignmentSource,
    CapacityLedger,
    CoverageGap,
    CoverageGapReason,
)
from pilot_backend.domain.enums import ConnectionStatus
from pilot_backend.domain.goals import EditType, EvidenceSource, GoalKind, GoalRef
from pilot_backend.domain.identity_claims import ClaimKind, key_digest
from pilot_backend.domain.intervention import (
    InterventionAction,
    InterventionError,
    InterventionScope,
    TherapistIntervention,
)
from pilot_backend.domain.monthly_plan import MonthlyGoalAllocation, TimezoneError
from pilot_backend.domain.observation import (
    AttemptOutcome,
    CustomizationSignalType,
    DeferRecord,
    Difficulty,
    Enjoyment,
    ObservationError,
    ObservationEvent,
    ParentCustomizationSignal,
    TimezoneSource,
    attribution_month_for,
)
from pilot_backend.domain.roles import ActorRole
from pilot_backend.domain.source_link import SourceSystem
from pilot_backend.domain.weekly_cycle import (
    GenerationReason,
    PartialReason,
    WeeklyCycle,
    WeeklyCycleError,
    WeeklyPlanLink,
    WeeklyPlanSnapshot,
    plan_cycle_bounds,
)
from pilot_backend.fixtures.secure_topology import (
    CAREGIVER_ALPHA_SUBJECT,
    CAREGIVER_BETA_SUBJECT,
    PROVIDER_ALPHA_SUBJECT,
    PROVIDER_BETA_SUBJECT,
    PROVIDER_GAMMA_SUBJECT,
    build_secure_topology,
)
from pilot_backend.goals.service import GoalService
from pilot_backend.goals.suggestion_engine import ObservationSnapshot, ObservedDomain
from pilot_backend.identity import LongitudinalIdentityService
from pilot_backend.persistence import FakeDocumentStore, FirestoreRepositories, encode
from pilot_backend.persistence.codecs import decode
from pilot_backend.persistence.collections import COLLECTIONS, PILOT_COLLECTION_PREFIX
from pilot_backend.planning.service import MonthlyPlanService
from pilot_backend.weekly.adaptation import (
    ADAPTATION_RULE_VERSION,
    describe_change,
    normalize,
    plan_adaptation,
)
from pilot_backend.weekly.allocator import (
    ALLOCATION_RULE_VERSION,
    AllocationError,
    CandidateActivity,
    allocate,
    effective_allocations,
    suppressed_identities,
)
from pilot_backend.weekly.counting import summarize
from pilot_backend.weekly.errors import (
    ReleasedPlanImmutable,
    WeeklyAuthorizationError,
    WeeklyConflict,
    WeeklyValidationError,
)
from pilot_backend.weekly.service import WeeklyService

from .test_secure_foundation import ALL_SENTINELS, SENTINEL_CONCERN, SENTINEL_NOTE

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
PILOT_ROOT = Path(__file__).resolve().parents[1]
CYCLE_MONTH = "2026-10"
ZONE = "America/New_York"

GOAL_A = GoalRef(GoalKind.CLINICAL, "clgl_alpha")
GOAL_B = GoalRef(GoalKind.CLINICAL, "clgl_beta")
GOAL_C = GoalRef(GoalKind.CLINICAL, "clgl_gamma")


class _AdvancingClock:
    """Deterministic but strictly increasing — see 0.4A for why frozen fails."""

    def __init__(self, start: datetime) -> None:
        self._t = start
        self._tick = 0

    def __call__(self) -> datetime:
        self._tick += 1
        return self._t.replace(microsecond=0) + timedelta(seconds=self._tick)


class Stack:
    def __init__(self):
        self.store = FakeDocumentStore()
        self.repos = FirestoreRepositories(self.store)
        self.topo = build_secure_topology(self.repos, now=T0)
        self.recorder = AuditRecorder(self.repos.audit_events, environment="test")
        self.clock = _AdvancingClock(T0)
        self.identity = LongitudinalIdentityService(
            repos=self.repos, recorder=self.recorder, now=self.clock)
        self.goals = GoalService(
            repos=self.repos, recorder=self.recorder, now=self.clock)
        self.plans = MonthlyPlanService(
            repos=self.repos, recorder=self.recorder, now=self.clock)
        self.weekly = WeeklyService(
            repos=self.repos, recorder=self.recorder, now=self.clock)

    def principal(self, subject):
        return resolve_principal(VerifiedToken(subject=subject), self.repos)

    @property
    def caregiver_alpha(self): return self.principal(CAREGIVER_ALPHA_SUBJECT)
    @property
    def caregiver_beta(self): return self.principal(CAREGIVER_BETA_SUBJECT)
    @property
    def provider_alpha(self): return self.principal(PROVIDER_ALPHA_SUBJECT)
    @property
    def provider_beta(self): return self.principal(PROVIDER_BETA_SUBJECT)
    @property
    def child(self): return self.topo.child_alpha.child_id

    def connected_provider_gamma(self):
        """Provider-Gamma, ACTIVE on Child-Alpha but never the owner.

        The topology ships Gamma as PENDING, which the 0.2 gate refuses before
        any ownership rule runs. Activating the connection makes the OWNERSHIP
        check the thing under test rather than the access check.
        """
        self.repos.provider_child.activate(
            self.topo.link_gamma_provider_pending.connection_id, now=T0)
        return self.principal(PROVIDER_GAMMA_SUBJECT)

    # -- fictional workflow builders ------------------------------------

    def make_managing_clinician(self):
        return self.identity.assign_managing_clinician(
            self.provider_alpha, self.child, self.topo.provider_alpha.provider_id)

    def approve_goal(self, text: str):
        return self.goals.approve_clinical_goal(
            self.provider_alpha, self.child, edit_type=EditType.AUTHORED_FRESH,
            text=text, reason="fictional pilot scenario")

    def active_month(self, *, goal_count: int = 2):
        """Managing clinician, N clinical goals, an ACTIVE October plan."""
        self.make_managing_clinician()
        goals = [self.approve_goal(f"Fictional focus {n}.")
                 for n in range(1, goal_count + 1)]
        plan = self.plans.create_plan(self.provider_alpha, self.child,
                                      CYCLE_MONTH, ZONE)
        for rank, goal in enumerate(goals, start=1):
            self.plans.allocate_goal(self.provider_alpha, plan.focus_plan_id,
                                     goal.ref, priority_rank=rank)
        return self.plans.activate_plan(
            self.provider_alpha, plan.focus_plan_id), goals


@pytest.fixture()
def s():
    return Stack()


def candidates_for(goal_a: GoalRef, goal_b: GoalRef):
    """The section-31 activity set: A serves one goal, B the other, C both."""
    return [
        CandidateActivity("activity-A", (goal_a,), primary_for=goal_a,
                          milestone_refs=("mv1:fictional:a",)),
        CandidateActivity("activity-B", (goal_b,), primary_for=goal_b,
                          milestone_refs=("mv1:fictional:b",)),
        CandidateActivity("activity-C", (goal_a, goal_b), primary_for=goal_a,
                          milestone_refs=("mv1:fictional:c",)),
    ]


def allocations_for(*specs):
    """Fictional MonthlyGoalAllocation rows: (goal, rank, weight)."""
    return [MonthlyGoalAllocation.create(
        "mfpl_fictional", "chld_fictional", goal,
        priority_rank=rank, emphasis_weight=weight)
        for goal, rank, weight in specs]


# ===========================================================================
# weekly cycle
# ===========================================================================

def test_cycle_one_is_partial_when_the_month_starts_midweek():
    """Section 24. October 2026 begins on a Thursday."""
    starts, ends, partial, reason = plan_cycle_bounds("2026-10", 1)
    assert (starts, ends) == ("2026-10-01", "2026-10-04")
    assert partial and reason is PartialReason.MONTH_STARTS_MIDWEEK
    assert plan_cycle_bounds("2026-10", 2)[:3] == ("2026-10-05", "2026-10-11", False)


def test_a_month_starting_on_sunday_gives_a_one_day_cycle():
    """Section 25. November 2026 begins on a Sunday."""
    starts, ends, partial, _ = plan_cycle_bounds("2026-11", 1)
    assert starts == ends == "2026-11-01"
    assert partial


def test_a_cycle_may_span_a_month_boundary():
    cycle = WeeklyCycle.create("mfpl_1", "chld_1", sequence_in_month=5,
                               starts_on="2026-10-26", ends_on="2026-11-01")
    assert cycle.spans_month_boundary
    assert cycle.day_count == 7
    assert cycle.covers("2026-11-01") and not cycle.covers("2026-11-02")


def test_spans_month_boundary_is_derived_not_supplied():
    """A caller cannot assert a boundary crossing that is not there."""
    cycle = WeeklyCycle.create("mfpl_1", "chld_1", sequence_in_month=2,
                               starts_on="2026-10-05", ends_on="2026-10-11")
    assert not cycle.spans_month_boundary


def test_a_partial_cycle_must_say_why():
    with pytest.raises(WeeklyCycleError):
        WeeklyCycle("wcyc_1", "mfpl_1", "chld_1", 1, "2026-10-01",
                    "2026-10-04", is_partial=True, partial_reason=None)
    with pytest.raises(WeeklyCycleError):
        WeeklyCycle("wcyc_1", "mfpl_1", "chld_1", 1, "2026-10-01",
                    "2026-10-04", is_partial=False,
                    partial_reason=PartialReason.MONTH_STARTS_MIDWEEK)


def test_a_cycle_carries_no_activity_list():
    """`WeeklyCycle` is direction, not content. The plan lives in the snapshot."""
    assert not any("activit" in f
                   for f in WeeklyCycle.__dataclass_fields__)


def test_the_weekly_cycle_is_not_the_parent_plan():
    """The Parent plan is reached only by EXTERNAL id, never a canonical key."""
    link = WeeklyPlanLink.create("wcyc_1", SourceSystem.PARENT, "parent-plan-fict")
    assert link.external_plan_id == "parent-plan-fict"
    assert COLLECTIONS["weekly_cycle"] != COLLECTIONS["weekly_plan_link"]
    # No pilot record is keyed by the external id.
    assert not link.link_id.startswith("parent-plan")


# ===========================================================================
# plan snapshot
# ===========================================================================

def test_a_snapshot_freezes_the_resolved_document(s):
    document = {"days": [{"local_date": "2026-10-01", "activities": ["A"]}]}
    snapshot = WeeklyPlanSnapshot.capture(
        "wcyc_1", SourceSystem.PARENT, "parent-plan-fict", document)
    assert snapshot.document() == document
    # Deterministic serialization: the same document captures identically.
    again = WeeklyPlanSnapshot.capture(
        "wcyc_1", SourceSystem.PARENT, "parent-plan-fict",
        {"days": [{"activities": ["A"], "local_date": "2026-10-01"}]})
    assert again.resolved_plan_document == snapshot.resolved_plan_document


def test_a_snapshot_refuses_a_non_json_document():
    with pytest.raises(WeeklyCycleError):
        WeeklyPlanSnapshot("wsnp_1", "wcyc_1", "not json at all",
                           SourceSystem.PARENT, "p1")


def test_the_snapshot_repository_has_no_update_path():
    """A snapshot that could be rewritten answers nothing."""
    repos = FirestoreRepositories(FakeDocumentStore())
    assert not hasattr(repos.weekly_plan_snapshots, "update")
    assert not hasattr(repos.weekly_plan_snapshots, "set")


# ===========================================================================
# allocation — stage 1, the coverage floor
# ===========================================================================

def test_each_goal_receives_the_coverage_floor():
    result = allocate("wcyc_1", allocations_for((GOAL_A, 1, 3), (GOAL_B, 2, 2)),
                      candidates_for(GOAL_A, GOAL_B), capacity=4)
    assert result.gaps == ()
    assert result.attributions_for(GOAL_A) >= 1
    assert result.attributions_for(GOAL_B) >= 1


def test_one_goal_is_supported():
    result = allocate("wcyc_1", allocations_for((GOAL_A, 1, 3)),
                      [CandidateActivity("activity-A", (GOAL_A,))], capacity=2)
    assert result.placed_count == 1 and result.gaps == ()


def test_three_goals_need_no_schema_change():
    """Section 26 and 31: N clinician goals, arbitrary weights."""
    allocations = allocations_for((GOAL_A, 1, 3), (GOAL_B, 2, 2), (GOAL_C, 3, 1))
    candidates = [CandidateActivity(f"activity-{k}", (g,), primary_for=g)
                  for k, g in (("A", GOAL_A), ("B", GOAL_B), ("C", GOAL_C))]
    result = allocate("wcyc_1", allocations, candidates, capacity=3)
    assert result.gaps == ()
    for goal in (GOAL_A, GOAL_B, GOAL_C):
        assert result.attributions_for(goal) == 1


def test_a_multi_goal_activity_satisfies_two_floors_at_once():
    """Section 5. One opportunity, two attributions — never two activities."""
    result = allocate("wcyc_1", allocations_for((GOAL_A, 1, 3), (GOAL_B, 2, 2)),
                      [CandidateActivity("activity-C", (GOAL_A, GOAL_B),
                                         primary_for=GOAL_A)],
                      capacity=1)
    assert result.placed_count == 1
    assert result.gaps == ()
    assert result.attributions_for(GOAL_A) == 1
    assert result.attributions_for(GOAL_B) == 1


def test_the_floor_is_not_met_by_duplicating_an_activity():
    placements = allocate(
        "wcyc_1", allocations_for((GOAL_A, 1, 3), (GOAL_B, 2, 2)),
        candidates_for(GOAL_A, GOAL_B), capacity=2).placements
    identities = [p.activity_identity_ref for p in placements]
    assert len(identities) == len(set(identities))


def test_insufficient_capacity_produces_a_gap_not_an_invented_activity():
    result = allocate("wcyc_1", allocations_for((GOAL_A, 1, 3), (GOAL_B, 2, 2)),
                      [CandidateActivity("activity-A", (GOAL_A,)),
                       CandidateActivity("activity-B", (GOAL_B,))],
                      capacity=1)
    assert result.placed_count == 1
    assert [g.goal_ref for g in result.gaps] == [GOAL_B]
    assert result.gaps[0].reason is CoverageGapReason.INSUFFICIENT_CAPACITY


def test_no_suitable_activity_produces_its_own_reason():
    result = allocate("wcyc_1", allocations_for((GOAL_A, 1, 3), (GOAL_B, 2, 2)),
                      [CandidateActivity("activity-A", (GOAL_A,))], capacity=5)
    assert [g.reason for g in result.gaps] == [CoverageGapReason.NO_SUITABLE_ACTIVITY]


def test_a_partial_week_gap_says_partial_week():
    result = allocate("wcyc_1", allocations_for((GOAL_A, 1, 3), (GOAL_B, 2, 2)),
                      [CandidateActivity("activity-A", (GOAL_A,)),
                       CandidateActivity("activity-B", (GOAL_B,))],
                      capacity=1, is_partial_week=True)
    assert [g.reason for g in result.gaps] == [CoverageGapReason.PARTIAL_WEEK]


def test_a_one_day_week_covers_the_primary_and_gaps_the_secondary():
    """Section 25, the single-activity case."""
    result = allocate("wcyc_1", allocations_for((GOAL_A, 1, 3), (GOAL_B, 2, 2)),
                      [CandidateActivity("activity-A", (GOAL_A,)),
                       CandidateActivity("activity-B", (GOAL_B,))],
                      capacity=1, is_partial_week=True)
    assert result.attributions_for(GOAL_A) == 1
    assert [g.goal_ref for g in result.gaps] == [GOAL_B]


def test_a_one_day_week_covers_both_when_one_activity_serves_both():
    """Section 25's explicit exception."""
    result = allocate("wcyc_1", allocations_for((GOAL_A, 1, 3), (GOAL_B, 2, 2)),
                      [CandidateActivity("activity-C", (GOAL_A, GOAL_B))],
                      capacity=1, is_partial_week=True)
    assert result.gaps == ()
    assert result.placed_count == 1


def test_priority_rank_decides_which_floor_is_closed_first():
    """Section 8: the floor is filled in `priority_rank` order.

    The candidate serving the PRIMARY goal is named so it sorts LAST
    alphabetically. A mutation that dropped the rank tie-break fell back to
    the identity ref and survived until this existed, because every earlier
    test happened to agree with alphabetical order.
    """
    allocations = allocations_for((GOAL_A, 1, 3), (GOAL_B, 2, 2))
    candidates = [
        CandidateActivity("aaa-serves-secondary", (GOAL_B,), primary_for=GOAL_B),
        CandidateActivity("zzz-serves-primary", (GOAL_A,), primary_for=GOAL_A),
    ]
    result = allocate("wcyc_1", allocations, candidates, capacity=1,
                      is_partial_week=True)
    assert result.placed_count == 1
    assert result.placements[0].activity_identity_ref == "zzz-serves-primary"
    assert [g.goal_ref for g in result.gaps] == [GOAL_B]


def test_a_coverage_gap_is_a_planner_condition_never_a_failure():
    gap = CoverageGap.create("wcyc_1", "chld_1", GOAL_A,
                             CoverageGapReason.PARTIAL_WEEK)
    assert gap.is_planner_condition and gap.not_a_failure
    # No reason exists for non-adherence or child performance.
    names = {m.name for m in CoverageGapReason}
    for forbidden in ("ADHERENCE", "CHILD", "CAREGIVER", "FAILURE", "NONCOMPLIANCE"):
        assert not any(forbidden in name for name in names), names


# ===========================================================================
# allocation — stage 2, relative emphasis
# ===========================================================================

def test_emphasis_distributes_by_ratio_not_percentage():
    allocations = allocations_for((GOAL_A, 1, 3), (GOAL_B, 2, 2))
    candidates = [CandidateActivity(f"a{n}", (GOAL_A,)) for n in range(5)] + \
                 [CandidateActivity(f"b{n}", (GOAL_B,)) for n in range(5)]
    result = allocate("wcyc_1", allocations, candidates, capacity=5)
    a, b = result.attributions_for(GOAL_A), result.attributions_for(GOAL_B)
    assert a + b == 5
    assert a > b, "the primary goal receives more emphasis"


def test_equal_weights_split_evenly():
    """Nothing hard-codes 60/40: equal weights must produce an even split."""
    allocations = allocations_for((GOAL_A, 1, 2), (GOAL_B, 2, 2))
    candidates = [CandidateActivity(f"a{n}", (GOAL_A,)) for n in range(4)] + \
                 [CandidateActivity(f"b{n}", (GOAL_B,)) for n in range(4)]
    result = allocate("wcyc_1", allocations, candidates, capacity=4)
    assert result.attributions_for(GOAL_A) == result.attributions_for(GOAL_B) == 2


def test_allocation_is_deterministic_and_order_independent():
    allocations = allocations_for((GOAL_A, 1, 3), (GOAL_B, 2, 2))
    candidates = candidates_for(GOAL_A, GOAL_B)
    first = allocate("wcyc_1", allocations, candidates, capacity=3)
    second = allocate("wcyc_1", list(reversed(allocations)),
                      list(reversed(candidates)), capacity=3)
    assert ([p.activity_instance_ref for p in first.placements]
            == [p.activity_instance_ref for p in second.placements])


def test_a_candidate_serving_no_goal_is_refused():
    with pytest.raises(AllocationError):
        CandidateActivity("activity-X", ())


def test_negative_capacity_is_refused():
    with pytest.raises(AllocationError):
        allocate("wcyc_1", allocations_for((GOAL_A, 1, 3)), [], capacity=-1)


# ===========================================================================
# no double counting
# ===========================================================================

def _alignment(cycle_id, instance, goal, identity="activity-C"):
    return ActivityGoalAlignment.create(
        cycle_id, "chld_1", instance, goal, activity_identity_ref=identity,
        role=AlignmentRole.PRIMARY, alignment_source=AlignmentSource.GENEX_RULE)


def _event(cycle_id, instance, outcome=AttemptOutcome.DID_IT,
           local_date="2026-10-01", difficulty=None):
    return ObservationEvent.record(
        "chld_1", cycle_id, instance, local_date=local_date,
        occurred_at=T0, timezone_of_record=ZONE, attempt_outcome=outcome,
        difficulty=difficulty)


def test_a_multi_goal_activity_counts_once_in_the_total():
    """Section 6, the whole point."""
    alignments = [_alignment("wcyc_1", "inst-C", GOAL_A),
                  _alignment("wcyc_1", "inst-C", GOAL_B)]
    events = [_event("wcyc_1", "inst-C")]
    summary = summarize(alignments, events)

    assert summary.total_attempts == 1
    assert summary.total_scheduled == 1
    per_goal = {g.goal_ref: g for g in summary.per_goal}
    assert per_goal[GOAL_A].attempted == 1
    assert per_goal[GOAL_B].attempted == 1
    assert summary.sum_of_goal_attempts == 2
    assert summary.total_attempts != summary.sum_of_goal_attempts
    assert summary.has_overlap
    assert summary.multi_goal_event_ids == (events[0].event_id,)


def test_the_same_event_is_never_counted_twice():
    """Deduplication is on `event_id`, not on list position.

    A mutation sweep caught this: nothing passed the same event to
    `summarize` twice, so a key that was not actually the event id looked
    correct. Real callers concatenate per-cycle lists, and a boundary cycle
    can appear in two of them.
    """
    alignments = [_alignment("wcyc_1", "inst-A", GOAL_A, identity="activity-A")]
    event = _event("wcyc_1", "inst-A")
    summary = summarize(alignments, [event, event, event])
    assert summary.total_attempts == 1
    assert summary.total_completed == 1
    assert {g.goal_ref: g.attempted for g in summary.per_goal}[GOAL_A] == 1


def test_totals_are_never_derived_from_summed_goal_streams():
    """Structural: `summarize` must not compute a total from per-goal counts."""
    source = inspect.getsource(summarize)
    assert "total_attempts=len(seen_events)" in source.replace(" ", "")


def test_per_goal_attribution_overlaps_explicitly():
    alignments = [_alignment("wcyc_1", "inst-C", GOAL_A),
                  _alignment("wcyc_1", "inst-C", GOAL_B),
                  _alignment("wcyc_1", "inst-A", GOAL_A, identity="activity-A")]
    events = [_event("wcyc_1", "inst-C"), _event("wcyc_1", "inst-A")]
    summary = summarize(alignments, events)
    assert summary.total_attempts == 2
    per_goal = {g.goal_ref: g for g in summary.per_goal}
    assert per_goal[GOAL_A].attempted == 2
    assert per_goal[GOAL_B].attempted == 1
    assert summary.sum_of_goal_attempts == 3


def test_completion_is_not_mastery():
    alignments = [_alignment("wcyc_1", "inst-A", GOAL_A)]
    events = [_event("wcyc_1", "inst-A", AttemptOutcome.DID_IT)]
    summary = summarize(alignments, events)
    assert summary.total_completed == 1
    # No field anywhere claims improvement, mastery or progress.
    for name in list(ObservationEvent.__dataclass_fields__) + \
            [f for f in dir(summary) if not f.startswith("_")]:
        assert not any(word in name.lower()
                       for word in ("master", "improve", "progress", "score"))


def test_scheduled_attempted_and_completed_stay_separate():
    alignments = [_alignment("wcyc_1", "inst-A", GOAL_A),
                  _alignment("wcyc_1", "inst-B", GOAL_A, identity="activity-B")]
    events = [_event("wcyc_1", "inst-A", AttemptOutcome.WASNT_READY_YET)]
    summary = summarize(alignments, events)
    assert summary.total_scheduled == 2
    assert summary.total_attempts == 1
    assert summary.total_completed == 0


# ===========================================================================
# capacity
# ===========================================================================

def test_overage_is_derived_from_the_counts_it_summarises():
    ledger = CapacityLedger.create("wcyc_1", "chld_1", 3,
                                   allocated_by_planner=3)
    assert ledger.overage == 0 and not ledger.is_over_capacity
    charged = ledger.with_clinician_add(1, reason="clinician_adapt")
    assert charged.overage == 1 and charged.is_over_capacity
    assert charged.overage_reason == "clinician_adapt"
    assert "overage" not in CapacityLedger.__dataclass_fields__


def test_clinician_added_activity_consumes_family_capacity():
    """Section 18. Family capacity is finite, including for clinicians."""
    ledger = CapacityLedger.create("wcyc_1", "chld_1", 2,
                                   allocated_by_planner=2)
    assert ledger.remaining == 0
    assert ledger.with_clinician_add(1).total_placed == 3


# ===========================================================================
# observation
# ===========================================================================

@pytest.mark.parametrize("outcome", list(AttemptOutcome))
def test_every_outcome_is_an_attempt(outcome):
    event = _event("wcyc_1", "inst-A", outcome)
    assert event.was_attempted
    assert event.was_completed is (outcome is AttemptOutcome.DID_IT)


def test_the_three_outcomes_stay_distinct():
    names = {m.value for m in AttemptOutcome}
    assert names == {"did_it", "wasnt_ready_yet", "didnt_want_to_try"}


def test_attribution_is_by_local_date_not_by_cycle():
    """Section 12. A cycle spanning Oct 26 – Nov 1 splits across months."""
    october = _event("wcyc_boundary", "inst-A", local_date="2026-10-31")
    november = _event("wcyc_boundary", "inst-A", local_date="2026-11-01")
    assert october.attribution_month == "2026-10"
    assert november.attribution_month == "2026-11"
    assert october.owning_cycle_id == november.owning_cycle_id


def test_attribution_month_cannot_disagree_with_the_local_date():
    event = _event("wcyc_1", "inst-A", local_date="2026-10-31")
    with pytest.raises(ObservationError):
        replace(event, attribution_month="2026-11")


def test_a_boundary_cycle_is_not_double_counted():
    alignments = [_alignment("wcyc_b", "inst-A", GOAL_A)]
    events = [_event("wcyc_b", "inst-A", local_date="2026-10-31"),
              _event("wcyc_b", "inst-A", local_date="2026-11-01")]
    october = summarize(alignments, events, attribution_month="2026-10")
    november = summarize(alignments, events, attribution_month="2026-11")
    both = summarize(alignments, events)
    assert october.total_attempts == 1
    assert november.total_attempts == 1
    assert both.total_attempts == 2


def test_an_observation_requires_a_valid_timezone():
    with pytest.raises(TimezoneError):
        ObservationEvent.record("chld_1", "wcyc_1", "inst-A",
                                local_date="2026-10-01", occurred_at=T0,
                                timezone_of_record="Mars/Olympus",
                                attempt_outcome=AttemptOutcome.DID_IT)
    with pytest.raises(TimezoneError):
        ObservationEvent.record("chld_1", "wcyc_1", "inst-A",
                                local_date="2026-10-01", occurred_at=T0,
                                timezone_of_record="",
                                attempt_outcome=AttemptOutcome.DID_IT)


def test_a_naive_observation_timestamp_is_refused():
    with pytest.raises(ObservationError):
        ObservationEvent.record("chld_1", "wcyc_1", "inst-A",
                                local_date="2026-10-01",
                                occurred_at=datetime(2026, 10, 1, 12, 0),
                                timezone_of_record=ZONE,
                                attempt_outcome=AttemptOutcome.DID_IT)


def test_observation_prose_is_stored_by_reference_only():
    """Free text is clinical content and is not indexed by this layer."""
    assert "observation_text_ref" in ObservationEvent.__dataclass_fields__
    assert "observation_text" not in ObservationEvent.__dataclass_fields__


# ===========================================================================
# defer
# ===========================================================================

def _defer(from_sequence=1, suppress=1):
    return DeferRecord.create("chld_1", "inst-C", "activity-C",
                              actor_id="cgvr_1", actor_role=ActorRole.CAREGIVER,
                              from_cycle_id="wcyc_1",
                              from_cycle_sequence=from_sequence,
                              suppress_for_cycles=suppress)


def test_a_defer_suppresses_only_the_next_cycle():
    record = _defer(from_sequence=1)
    assert not record.suppresses(1)
    assert record.suppresses(2)
    assert not record.suppresses(3), "eligible again, not retired"


def test_a_defer_is_never_a_permanent_retirement():
    record = _defer(from_sequence=1)
    assert not record.suppresses(5)
    # Nothing in the record can mark an activity permanently unusable.
    assert not any("retire" in f or "permanent" in f
                   for f in DeferRecord.__dataclass_fields__)


def test_a_defer_must_suppress_at_least_one_cycle():
    with pytest.raises(ObservationError):
        DeferRecord.create("chld_1", "inst-C", "activity-C", actor_id="c",
                           actor_role=ActorRole.CAREGIVER,
                           from_cycle_id="wcyc_1", from_cycle_sequence=1,
                           suppress_for_cycles=0)


def test_the_allocator_never_reintroduces_a_deferred_activity_early():
    """Section 15. A gap is preferred over defeating a defer signal."""
    result = allocate("wcyc_2", allocations_for((GOAL_A, 1, 3)),
                      [CandidateActivity("activity-C", (GOAL_A,))],
                      capacity=5,
                      suppressed_identity_refs=("activity-C",))
    assert result.placements == ()
    assert [g.reason for g in result.gaps] == [CoverageGapReason.DEFERRED_CONSTRAINT]


def test_suppression_lifts_only_on_an_explicit_clinician_override():
    record = _defer(from_sequence=1)
    assert suppressed_identities([record], 2) == ("activity-C",)
    overridden = record.with_clinician_override(actor_id="prov_1",
                                                reason="clinically indicated")
    assert suppressed_identities([overridden], 2) == ()
    assert overridden.was_overridden


def test_an_override_requires_an_actor_and_a_reason():
    record = _defer()
    for kwargs in ({"actor_id": "prov_1", "reason": ""},
                   {"actor_id": "", "reason": "because"}):
        with pytest.raises(ObservationError):
            record.with_clinician_override(**kwargs)


def test_the_allocator_has_no_access_to_the_override():
    """Structural: a system-only coverage floor cannot release a defer."""
    import pilot_backend.weekly.allocator as allocator_module

    source = inspect.getsource(allocator_module)
    assert "with_clinician_override" not in source
    assert "override_reason" not in source


# ===========================================================================
# therapist intervention
# ===========================================================================

def test_an_intervention_requires_a_clinical_rationale():
    with pytest.raises(InterventionError):
        TherapistIntervention.create(
            "chld_1", "wcyc_1", "prov_1", action=InterventionAction.ENDORSE,
            applies_to=InterventionScope.FUTURE_CYCLE, clinical_rationale="")


def test_guidance_requires_guidance_text():
    with pytest.raises(InterventionError):
        TherapistIntervention.create(
            "chld_1", "wcyc_1", "prov_1",
            action=InterventionAction.ADD_GUIDANCE,
            applies_to=InterventionScope.FUTURE_CYCLE,
            clinical_rationale="r", guidance_text="")


def test_a_clinician_change_is_never_a_failure():
    intervention = TherapistIntervention.create(
        "chld_1", "wcyc_1", "prov_1", action=InterventionAction.REPLACE,
        applies_to=InterventionScope.FUTURE_CYCLE, clinical_rationale="r")
    assert intervention.not_a_failure


def test_only_the_managing_clinician_may_intervene(s):
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                            candidates_for(goals[0].ref, goals[1].ref),
                            family_declared_capacity=3)

    gamma = s.connected_provider_gamma()
    assert s.weekly.get_cycle(gamma, cycle.cycle_id) == cycle, "gamma can read"
    with pytest.raises(WeeklyAuthorizationError):
        s.weekly.create_intervention(
            gamma, cycle.cycle_id, action=InterventionAction.ENDORSE,
            applies_to=InterventionScope.FUTURE_CYCLE, clinical_rationale="r")


def test_a_caregiver_cannot_create_an_intervention(s):
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    with pytest.raises(WeeklyAuthorizationError):
        s.weekly.create_intervention(
            s.caregiver_alpha, cycle.cycle_id,
            action=InterventionAction.ENDORSE,
            applies_to=InterventionScope.FUTURE_CYCLE, clinical_rationale="r")


def test_a_caregiver_is_refused_on_role_before_ownership(s):
    """The role check must stand on its own.

    With no managing clinician assigned, the ownership comparison has nothing
    to compare against. A caregiver must still be refused as UNAUTHORIZED
    rather than falling through to a "no managing clinician" conflict — a
    mutation dropping the role check survived until this distinguished them.
    """
    plan = s.plans.create_plan(s.caregiver_alpha, s.child, CYCLE_MONTH, ZONE)
    goal = s.goals.approve_caregiver_goal(
        s.caregiver_alpha, s.child, edit_type=EditType.AUTHORED_FRESH,
        text="Fictional caregiver goal.", reason="family choice")
    s.plans.allocate_goal(s.caregiver_alpha, plan.focus_plan_id, goal.ref,
                          priority_rank=1)
    s.plans.activate_plan(s.caregiver_alpha, plan.focus_plan_id)
    cycle = s.weekly.create_cycle(s.caregiver_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)

    with pytest.raises(WeeklyAuthorizationError):
        s.weekly.create_intervention(
            s.caregiver_alpha, cycle.cycle_id,
            action=InterventionAction.ENDORSE,
            applies_to=InterventionScope.FUTURE_CYCLE, clinical_rationale="r")


def test_an_unrelated_provider_cannot_intervene(s):
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    with pytest.raises(WeeklyAuthorizationError):
        s.weekly.create_intervention(
            s.provider_beta, cycle.cycle_id,
            action=InterventionAction.ENDORSE,
            applies_to=InterventionScope.FUTURE_CYCLE, clinical_rationale="r")


def test_a_released_cycle_cannot_be_rewritten(s):
    """Section 17A. Represent intent; never mutate the released snapshot."""
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                            candidates_for(goals[0].ref, goals[1].ref),
                            family_declared_capacity=3)
    snapshot = s.weekly.capture_snapshot(
        s.provider_alpha, cycle.cycle_id, SourceSystem.PARENT, "parent-fict",
        {"days": []})
    s.weekly.release_cycle(s.provider_alpha, cycle.cycle_id)

    with pytest.raises(ReleasedPlanImmutable):
        s.weekly.create_intervention(
            s.provider_alpha, cycle.cycle_id,
            action=InterventionAction.REPLACE,
            applies_to=InterventionScope.CURRENT_PLAN, clinical_rationale="r")

    stored = s.repos.weekly_plan_snapshots.get_by_id(snapshot.snapshot_id)
    assert stored == snapshot, "the released snapshot was not touched"


def test_a_cycle_cannot_be_released_without_a_snapshot(s):
    """Releasing uncaptured content leaves nothing to compare a change to.

    The snapshot is the only record of what the family was actually given,
    and `released_to_parent_at` without one is a claim the store cannot
    support.
    """
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                            candidates_for(goals[0].ref, goals[1].ref),
                            family_declared_capacity=3)
    with pytest.raises(WeeklyConflict):
        s.weekly.release_cycle(s.provider_alpha, cycle.cycle_id)

    s.weekly.capture_snapshot(s.provider_alpha, cycle.cycle_id,
                              SourceSystem.PARENT, "parent-fict", {"days": []})
    assert s.weekly.release_cycle(s.provider_alpha, cycle.cycle_id).is_released


def test_intent_may_still_be_recorded_against_a_released_cycle(s):
    """Guidance and endorsement are intent, not a rewrite."""
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                            candidates_for(goals[0].ref, goals[1].ref),
                            family_declared_capacity=3)
    s.weekly.capture_snapshot(s.provider_alpha, cycle.cycle_id,
                              SourceSystem.PARENT, "parent-fict", {"days": []})
    s.weekly.release_cycle(s.provider_alpha, cycle.cycle_id)

    recorded = s.weekly.create_intervention(
        s.provider_alpha, cycle.cycle_id,
        action=InterventionAction.ADD_GUIDANCE,
        applies_to=InterventionScope.CURRENT_PLAN,
        clinical_rationale="support the routine", guidance_text="fictional tip")
    assert recorded.intervention_id


def test_a_future_cycle_intervention_needs_no_parent_acceptance(s):
    """Section 17B. Nothing has been shown yet."""
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                            candidates_for(goals[0].ref, goals[1].ref),
                            family_declared_capacity=3)
    intervention = s.weekly.create_intervention(
        s.provider_alpha, cycle.cycle_id,
        action=InterventionAction.ADD_GUIDANCE,
        applies_to=InterventionScope.FUTURE_CYCLE,
        clinical_rationale="carry into next week", guidance_text="fictional tip")
    assert intervention.is_future


def test_a_clinician_add_records_overage_rather_than_removing_content(s):
    """Section 18. Nothing the family already received is taken away."""
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                            candidates_for(goals[0].ref, goals[1].ref),
                            family_declared_capacity=2)
    before = s.weekly.capacity_for(s.provider_alpha, cycle.cycle_id)
    alignments = s.weekly.list_alignments(s.provider_alpha, cycle.cycle_id)

    s.weekly.create_intervention(
        s.provider_alpha, cycle.cycle_id, action=InterventionAction.ADAPT,
        applies_to=InterventionScope.CURRENT_PLAN,
        clinical_rationale="add a supported variant",
        target_ref=alignments[0].activity_instance_ref)

    after = s.weekly.capacity_for(s.provider_alpha, cycle.cycle_id)
    assert after.clinician_added == 1
    assert after.overage == max(0, before.total_placed + 1 - 2)
    assert after.overage_reason
    assert (len(s.weekly.list_alignments(s.provider_alpha, cycle.cycle_id))
            == len(alignments)), "nothing already given was removed"


# ===========================================================================
# adaptation
# ===========================================================================

def test_support_signals_lead_to_more_support():
    alignments = [_alignment("wcyc_1", "inst-B", GOAL_B, identity="activity-B")]
    events = [_event("wcyc_1", "inst-B", AttemptOutcome.WASNT_READY_YET)]
    plan = plan_adaptation(alignments, events=events, next_cycle_sequence=2)
    assert plan.direction_for("activity-B") is AdaptationDirection.EASIER_OR_MORE_SUPPORT


@pytest.mark.parametrize("outcome,difficulty", [
    (AttemptOutcome.WASNT_READY_YET, None),
    (AttemptOutcome.DIDNT_WANT_TO_TRY, None),
    (AttemptOutcome.DID_IT, Difficulty.TOO_HARD),
])
def test_every_support_case_leans_easier(outcome, difficulty):
    alignments = [_alignment("wcyc_1", "inst-A", GOAL_A, identity="activity-A")]
    events = [_event("wcyc_1", "inst-A", outcome, difficulty=difficulty)]
    plan = plan_adaptation(alignments, events=events, next_cycle_sequence=2)
    assert plan.direction_for("activity-A") is AdaptationDirection.EASIER_OR_MORE_SUPPORT


def test_progression_requires_too_easy_and_did_it():
    alignments = [_alignment("wcyc_1", "inst-A", GOAL_A, identity="activity-A")]
    progressed = plan_adaptation(
        alignments,
        events=[_event("wcyc_1", "inst-A", AttemptOutcome.DID_IT,
                       difficulty=Difficulty.TOO_EASY)],
        next_cycle_sequence=2)
    assert progressed.direction_for("activity-A") is AdaptationDirection.HARDER_OR_PROGRESSED


def test_a_bare_did_it_does_not_progress_anything():
    """Completion is not mastery."""
    alignments = [_alignment("wcyc_1", "inst-A", GOAL_A, identity="activity-A")]
    plan = plan_adaptation(
        alignments, events=[_event("wcyc_1", "inst-A", AttemptOutcome.DID_IT)],
        next_cycle_sequence=2)
    assert plan.direction_for("activity-A") is AdaptationDirection.MAINTAIN


def test_maintain_is_the_default_for_anything_unmapped():
    plan = plan_adaptation([], next_cycle_sequence=2)
    assert plan.direction_for("activity-never-seen") is AdaptationDirection.MAINTAIN
    assert plan.signals == ()


def test_just_right_invents_no_signal():
    """`JUST_RIGHT` is not evidence for a change, so it maps to nothing.

    Paired with a non-completion outcome on purpose: with `DID_IT` the
    outcome signal is already `CHILD_COMPLETED`, so a mutation adding
    `JUST_RIGHT -> CHILD_COMPLETED` produced an identical set and survived.
    """
    alignments = [_alignment("wcyc_1", "inst-A", GOAL_A, identity="activity-A")]
    events = [_event("wcyc_1", "inst-A", AttemptOutcome.WASNT_READY_YET,
                     difficulty=Difficulty.JUST_RIGHT)]
    kinds = {sig.kind for sig in normalize(alignments, events)}
    assert kinds == {SignalKind.CHILD_WASNT_READY}, "just_right added a signal"

    completed = [_event("wcyc_1", "inst-A", AttemptOutcome.DID_IT,
                        difficulty=Difficulty.JUST_RIGHT)]
    assert {sig.kind for sig in normalize(alignments, completed)} == \
        {SignalKind.CHILD_COMPLETED}


def test_support_wins_over_progression_for_the_same_activity():
    """Conservative: one hard attempt outweighs one easy one."""
    alignments = [_alignment("wcyc_1", "inst-A", GOAL_A, identity="activity-A"),
                  _alignment("wcyc_1", "inst-A2", GOAL_A, identity="activity-A")]
    events = [_event("wcyc_1", "inst-A", AttemptOutcome.DID_IT,
                     difficulty=Difficulty.TOO_EASY),
              _event("wcyc_1", "inst-A2", AttemptOutcome.WASNT_READY_YET)]
    plan = plan_adaptation(alignments, events=events, next_cycle_sequence=2)
    assert plan.direction_for("activity-A") is AdaptationDirection.EASIER_OR_MORE_SUPPORT


def test_adaptation_is_deterministic():
    alignments = [_alignment("wcyc_1", "inst-A", GOAL_A, identity="activity-A")]
    events = [_event("wcyc_1", "inst-A", AttemptOutcome.WASNT_READY_YET)]
    first = plan_adaptation(alignments, events=events, next_cycle_sequence=2)
    second = plan_adaptation(alignments, events=events, next_cycle_sequence=2)
    assert first.directions == second.directions
    assert [s.as_key() for s in first.signals] == [s.as_key() for s in second.signals]


def test_the_adaptation_engine_reaches_no_network_or_model():
    tree = ast.parse((PILOT_ROOT / "weekly/adaptation.py").read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    banned = {"requests", "httpx", "urllib", "urllib3", "http", "socket",
              "openai", "anthropic", "google", "vertexai", "aiohttp", "grpc"}
    assert not (modules & banned), modules & banned


@pytest.mark.parametrize("module", ["weekly/allocator.py", "weekly/adaptation.py",
                                    "weekly/counting.py"])
def test_the_weekly_engines_reach_no_network(module):
    tree = ast.parse((PILOT_ROOT / module).read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    banned = {"requests", "httpx", "urllib", "http", "socket", "openai",
              "anthropic", "google", "vertexai", "aiohttp", "grpc"}
    assert not (modules & banned), (module, modules & banned)


def test_clinician_direction_is_marked_therapist_directed():
    alignments = [_alignment("wcyc_1", "inst-A", GOAL_A, identity="activity-A")]
    intervention = TherapistIntervention.create(
        "chld_1", "wcyc_1", "prov_1", action=InterventionAction.ADAPT,
        applies_to=InterventionScope.FUTURE_CYCLE, clinical_rationale="r",
        target_ref="inst-A")
    plan = plan_adaptation(alignments, interventions=[intervention],
                           next_cycle_sequence=2)
    assert plan.origin is AdaptationOrigin.THERAPIST_DIRECTED
    assert not plan.has_performance_evidence, "no child evidence here"


def test_mixed_origin_when_both_contributed():
    alignments = [_alignment("wcyc_1", "inst-A", GOAL_A, identity="activity-A")]
    intervention = TherapistIntervention.create(
        "chld_1", "wcyc_1", "prov_1", action=InterventionAction.ENDORSE,
        applies_to=InterventionScope.FUTURE_CYCLE, clinical_rationale="r",
        target_ref="inst-A")
    plan = plan_adaptation(
        alignments, events=[_event("wcyc_1", "inst-A")],
        interventions=[intervention], next_cycle_sequence=2)
    assert plan.origin is AdaptationOrigin.MIXED


@pytest.mark.parametrize("action,expected", [
    (InterventionAction.ENDORSE, SignalKind.CLINICIAN_ENDORSED),
    (InterventionAction.ADAPT, SignalKind.CLINICIAN_MODIFIED),
    (InterventionAction.REPLACE, SignalKind.CLINICIAN_REPLACED),
    (InterventionAction.ADD_GUIDANCE, SignalKind.CLINICIAN_GUIDANCE),
    (InterventionAction.DEFER, SignalKind.CLINICIAN_DEFERRED),
    (InterventionAction.REMOVE_FROM_SCHEDULING,
     SignalKind.CLINICIAN_REMOVED_FROM_SCHEDULING),
])
def test_every_clinician_action_maps_to_its_own_signal(action, expected):
    """Section 21. Clinician decisions stay distinguishable from evidence."""
    intervention = TherapistIntervention.create(
        "chld_1", "wcyc_1", "prov_1", action=action,
        applies_to=InterventionScope.FUTURE_CYCLE, clinical_rationale="r",
        guidance_text="t" if action is InterventionAction.ADD_GUIDANCE else "")
    signals = normalize([], interventions=[intervention])
    assert [s.kind for s in signals] == [expected]
    assert signals[0].source is SignalSource.CLINICIAN_INTERVENTION
    assert not signals[0].is_performance_signal


def test_a_parent_decline_is_not_an_activity_failure():
    """Section 22."""
    signal = ParentCustomizationSignal.create(
        "wcyc_1", "chld_1", "inst-A",
        CustomizationSignalType.PARENT_DECLINED_CHANGE, actor_id="cgvr_1")
    normalized = normalize([_alignment("wcyc_1", "inst-A", GOAL_A)],
                           customization_signals=[signal])
    assert [s.kind for s in normalized] == [SignalKind.PARENT_DECLINED_CHANGE]
    assert not normalized[0].is_performance_signal
    assert signal.not_a_failure


def test_a_parent_decline_sets_no_difficulty_direction():
    alignments = [_alignment("wcyc_1", "inst-A", GOAL_A, identity="activity-A")]
    signal = ParentCustomizationSignal.create(
        "wcyc_1", "chld_1", "inst-A",
        CustomizationSignalType.PARENT_DECLINED_CHANGE, actor_id="cgvr_1")
    plan = plan_adaptation(alignments, customization_signals=[signal],
                           next_cycle_sequence=2)
    assert plan.direction_for("activity-A") is AdaptationDirection.MAINTAIN


def test_an_adaptation_record_is_always_not_a_failure():
    with pytest.raises(AdaptationError):
        AdaptationRecord("adpt_1", "chld_1", "mfpl_1", "wcyc_1", "wcyc_2",
                         not_a_failure=False)


def test_an_adaptation_must_link_two_different_cycles():
    with pytest.raises(AdaptationError):
        AdaptationRecord.create("chld_1", "mfpl_1", "wcyc_1", "wcyc_1")


def test_signal_source_is_derived_from_the_signals():
    record = AdaptationRecord.create(
        "chld_1", "mfpl_1", "wcyc_1", "wcyc_2",
        normalized_signals=(
            NormalizedSignal(SignalKind.CHILD_COMPLETED,
                             SignalSource.CAREGIVER_OBSERVATION, "obsv_1"),
            NormalizedSignal(SignalKind.CLINICIAN_ENDORSED,
                             SignalSource.CLINICIAN_INTERVENTION, "invn_1"),
        ))
    assert record.signal_source == (SignalSource.CAREGIVER_OBSERVATION,
                                    SignalSource.CLINICIAN_INTERVENTION)
    assert record.has_performance_evidence


def test_a_therapist_directed_record_must_name_its_intervention():
    with pytest.raises(AdaptationError):
        AdaptationRecord.create("chld_1", "mfpl_1", "wcyc_1", "wcyc_2",
                                origin=AdaptationOrigin.THERAPIST_DIRECTED)


def test_the_change_summary_carries_no_clinical_content():
    alignments = [_alignment("wcyc_1", "inst-A", GOAL_A, identity="activity-A")]
    plan = plan_adaptation(
        alignments, events=[_event("wcyc_1", "inst-A",
                                   AttemptOutcome.WASNT_READY_YET)],
        next_cycle_sequence=2)
    summary = describe_change(plan)
    for sentinel in ALL_SENTINELS:
        assert sentinel not in summary
    assert "origin=" in summary


def test_adaptation_refuses_to_target_cycle_one():
    with pytest.raises(Exception):
        plan_adaptation([], next_cycle_sequence=1)


# ===========================================================================
# effective allocations (section 26)
# ===========================================================================

def test_a_mid_month_reprioritisation_affects_only_later_cycles():
    first = MonthlyGoalAllocation.create("mfpl_1", "chld_1", GOAL_A,
                                         priority_rank=1, emphasis_weight=3)
    successor = MonthlyGoalAllocation.create(
        "mfpl_1", "chld_1", GOAL_A, priority_rank=2, emphasis_weight=2,
        effective_from_cycle=3, supersedes_allocation_id=first.allocation_id)
    rows = [first.supersede(successor.allocation_id), successor]

    assert effective_allocations(rows, 2)[0].priority_rank == 1
    assert effective_allocations(rows, 3)[0].priority_rank == 2


def test_the_latest_effective_row_wins_regardless_of_id_order():
    """Successors are chosen by `effective_from_cycle`, never by id.

    The ids are set so the LATER-effective row sorts EARLIER alphabetically.
    A mutation that dropped `effective_from_cycle` from the sort key fell
    back to the id and survived, because real ids are random and the earlier
    test agreed with that order roughly half the time — a mutation that
    passes by coincidence is worse than one that fails.
    """
    first = replace(
        MonthlyGoalAllocation.create("mfpl_1", "chld_1", GOAL_A,
                                     priority_rank=1, emphasis_weight=3),
        allocation_id="galc_zzz_earlier_effective")
    successor = replace(
        MonthlyGoalAllocation.create("mfpl_1", "chld_1", GOAL_A,
                                     priority_rank=2, emphasis_weight=2,
                                     effective_from_cycle=3),
        allocation_id="galc_aaa_later_effective")

    effective = effective_allocations([first, successor], 3)
    assert len(effective) == 1
    assert effective[0].allocation_id == successor.allocation_id
    assert effective[0].priority_rank == 2

    # And the predecessor still governs the earlier cycle.
    assert effective_allocations([first, successor], 2)[0].allocation_id == \
        first.allocation_id


def test_a_goal_added_mid_month_appears_only_from_its_effective_cycle():
    existing = MonthlyGoalAllocation.create("mfpl_1", "chld_1", GOAL_A,
                                            priority_rank=1, emphasis_weight=3)
    added = MonthlyGoalAllocation.create(
        "mfpl_1", "chld_1", GOAL_C, priority_rank=3, emphasis_weight=1,
        effective_from_cycle=3)
    rows = [existing, added]
    assert [a.goal_ref for a in effective_allocations(rows, 2)] == [GOAL_A]
    assert [a.goal_ref for a in effective_allocations(rows, 3)] == [GOAL_A, GOAL_C]


def test_historical_alignments_are_never_rewritten(s):
    """Section 26. Past cycles keep the attribution they were built with."""
    repos = FirestoreRepositories(FakeDocumentStore())
    assert not hasattr(repos.alignments, "update")
    assert not hasattr(repos.alignments, "set")


# ===========================================================================
# the fictional section-31 scenario
# ===========================================================================

def test_the_october_scenario_end_to_end(s):
    """Section 31, on the neutral fictional topology.

    October plan, two goals at 3/2, a four-day opening cycle, activities A, B
    and C where C serves BOTH goals. The caregiver attempts A, marks B
    `wasnt_ready_yet` and saves C for later; the clinician adds future-cycle
    guidance. Cycle 2 is then generated and must explain itself.
    """
    plan, goals = s.active_month()
    goal_a, goal_b = goals[0].ref, goals[1].ref

    # ---- cycle 1: Oct 1-4, a partial week -----------------------------
    cycle1 = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                   sequence_in_month=1)
    assert (cycle1.starts_on, cycle1.ends_on) == ("2026-10-01", "2026-10-04")
    assert cycle1.is_partial

    result = s.weekly.allocate_cycle(
        s.provider_alpha, cycle1.cycle_id, candidates_for(goal_a, goal_b),
        family_declared_capacity=3)

    # capacity respected, both goals covered, C counted once
    assert result.placed_count <= 3
    assert result.gaps == ()
    assert result.attributions_for(goal_a) >= 1
    assert result.attributions_for(goal_b) >= 1

    alignments = s.weekly.list_alignments(s.provider_alpha, cycle1.cycle_id)
    by_identity = {}
    for alignment in alignments:
        by_identity.setdefault(alignment.activity_identity_ref, set()).add(
            alignment.goal_ref)
    assert by_identity["activity-C"] == {goal_a, goal_b}, "C serves both goals"

    instances = {a.activity_identity_ref: a.activity_instance_ref
                 for a in alignments}
    # C appears as ONE instance despite two alignments
    c_instances = {a.activity_instance_ref for a in alignments
                   if a.activity_identity_ref == "activity-C"}
    assert len(c_instances) == 1

    # ---- parent evidence ----------------------------------------------
    s.weekly.record_observation(
        s.caregiver_alpha, cycle1.cycle_id, instances["activity-A"],
        local_date="2026-10-02", attempt_outcome=AttemptOutcome.DID_IT)
    s.weekly.record_observation(
        s.caregiver_alpha, cycle1.cycle_id, instances["activity-B"],
        local_date="2026-10-03",
        attempt_outcome=AttemptOutcome.WASNT_READY_YET)
    s.weekly.defer_activity(s.caregiver_alpha, cycle1.cycle_id,
                            instances["activity-C"])

    # ---- therapist future guidance -------------------------------------
    s.weekly.create_intervention(
        s.provider_alpha, cycle1.cycle_id,
        action=InterventionAction.ADD_GUIDANCE,
        applies_to=InterventionScope.FUTURE_CYCLE,
        clinical_rationale="carry the routine into next week",
        guidance_text="fictional clinical guidance",
        target_ref=instances["activity-B"])

    # ---- cycle 2: Oct 5-11 ---------------------------------------------
    cycle2, result2, record = s.weekly.generate_next_cycle(
        s.provider_alpha, cycle1.cycle_id, candidates_for(goal_a, goal_b),
        family_declared_capacity=3)

    assert (cycle2.starts_on, cycle2.ends_on) == ("2026-10-05", "2026-10-11")
    assert cycle2.predecessor_cycle_id == cycle1.cycle_id

    # C is suppressed by the defer
    placed = {p.activity_identity_ref for p in result2.placements}
    assert "activity-C" not in placed, "the defer was honoured"

    # B's signal influenced adaptation conservatively
    directions = {s_.kind for s_ in record.normalized_signals}
    assert SignalKind.CHILD_WASNT_READY in directions
    assert SignalKind.CLINICIAN_GUIDANCE in directions
    assert SignalKind.PLAN_DEFERRED_BY_CAREGIVER in directions

    # the record explains itself and blames nobody
    assert record.from_cycle_id == cycle1.cycle_id
    assert record.to_cycle_id == cycle2.cycle_id
    assert record.not_a_failure
    assert record.origin is AdaptationOrigin.MIXED
    assert record.resulting_change
    assert record.goal_alignment_before and record.goal_alignment_after

    # every feasible goal still gets coverage, primary weighted higher
    assert result2.attributions_for(goal_a) >= 1
    assert result2.attributions_for(goal_b) >= 1

    # no event means clinical improvement
    summary = s.weekly.coverage_summary(s.provider_alpha, plan.focus_plan_id)
    assert summary.total_completed == 1
    assert not any("improve" in f or "master" in f
                   for f in dir(summary) if not f.startswith("_"))


def test_a_third_goal_before_cycle_three_needs_no_redesign(s):
    """Section 31's final step: N goals, same schema."""
    plan, goals = s.active_month()
    goal_a, goal_b = goals[0].ref, goals[1].ref

    cycle1 = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                   sequence_in_month=1)
    s.weekly.allocate_cycle(s.provider_alpha, cycle1.cycle_id,
                            candidates_for(goal_a, goal_b),
                            family_declared_capacity=3)
    cycle2, _, _ = s.weekly.generate_next_cycle(
        s.provider_alpha, cycle1.cycle_id, candidates_for(goal_a, goal_b),
        family_declared_capacity=3)

    # a third clinician goal, effective from cycle 3
    third = s.approve_goal("Fictional third focus.")
    s.plans.allocate_goal(s.provider_alpha, plan.focus_plan_id, third.ref,
                          priority_rank=3, effective_from_cycle=3,
                          reason="clinician judgement")

    candidates = candidates_for(goal_a, goal_b) + [
        CandidateActivity("activity-D", (third.ref,), primary_for=third.ref)]
    cycle3, result3, _ = s.weekly.generate_next_cycle(
        s.provider_alpha, cycle2.cycle_id, candidates,
        family_declared_capacity=4)

    assert cycle3.sequence_in_month == 3
    assert result3.attributions_for(third.ref) >= 1, "the new goal is covered"
    for goal in (goal_a, goal_b, third.ref):
        covered = result3.attributions_for(goal) > 0
        gapped = any(g.goal_ref == goal for g in result3.gaps)
        assert covered or gapped, "every goal is covered or explained"


def test_week_n_to_n_plus_one_is_general_not_hard_coded(s):
    """Nothing special-cases cycle 2."""
    source = inspect.getsource(WeeklyService.generate_next_cycle)
    assert "== 2" not in source and "cycle_2" not in source
    assert "sequence_in_month + 1" in source


# ===========================================================================
# authorization
# ===========================================================================

def test_a_caregiver_records_observations_only_for_their_own_child(s):
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                            candidates_for(goals[0].ref, goals[1].ref),
                            family_declared_capacity=3)
    instance = s.weekly.list_alignments(
        s.provider_alpha, cycle.cycle_id)[0].activity_instance_ref

    with pytest.raises(WeeklyAuthorizationError):
        s.weekly.record_observation(
            s.caregiver_beta, cycle.cycle_id, instance,
            local_date="2026-10-02", attempt_outcome=AttemptOutcome.DID_IT)


def test_a_provider_cannot_record_a_caregiver_observation(s):
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                            candidates_for(goals[0].ref, goals[1].ref),
                            family_declared_capacity=3)
    instance = s.weekly.list_alignments(
        s.provider_alpha, cycle.cycle_id)[0].activity_instance_ref
    with pytest.raises(WeeklyAuthorizationError):
        s.weekly.record_observation(
            s.provider_alpha, cycle.cycle_id, instance,
            local_date="2026-10-02", attempt_outcome=AttemptOutcome.DID_IT)


def test_a_cross_cycle_activity_reference_fails_closed(s):
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                            candidates_for(goals[0].ref, goals[1].ref),
                            family_declared_capacity=3)
    with pytest.raises(WeeklyValidationError):
        s.weekly.record_observation(
            s.caregiver_alpha, cycle.cycle_id, "wcyc_other::activity-A::0",
            local_date="2026-10-02", attempt_outcome=AttemptOutcome.DID_IT)


def test_a_date_outside_the_cycle_is_refused(s):
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                            candidates_for(goals[0].ref, goals[1].ref),
                            family_declared_capacity=3)
    instance = s.weekly.list_alignments(
        s.provider_alpha, cycle.cycle_id)[0].activity_instance_ref
    with pytest.raises(WeeklyValidationError):
        s.weekly.record_observation(
            s.caregiver_alpha, cycle.cycle_id, instance,
            local_date="2026-10-20", attempt_outcome=AttemptOutcome.DID_IT)


def test_a_revoked_connection_fails_closed(s):
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                            candidates_for(goals[0].ref, goals[1].ref),
                            family_declared_capacity=3)
    instance = s.weekly.list_alignments(
        s.provider_alpha, cycle.cycle_id)[0].activity_instance_ref

    s.repos.caregiver_child.end_connection(
        s.topo.link_alpha_caregiver.connection_id,
        status=ConnectionStatus.REVOKED)
    with pytest.raises(WeeklyAuthorizationError):
        s.weekly.record_observation(
            s.caregiver_alpha, cycle.cycle_id, instance,
            local_date="2026-10-02", attempt_outcome=AttemptOutcome.DID_IT)


def test_a_non_managing_provider_cannot_plan_the_week(s):
    plan, goals = s.active_month()
    gamma = s.connected_provider_gamma()
    with pytest.raises(WeeklyAuthorizationError):
        s.weekly.create_cycle(gamma, plan.focus_plan_id, sequence_in_month=1)


def test_an_unrelated_family_cannot_read_the_weekly_layer(s):
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    for call in (
        lambda: s.weekly.get_cycle(s.caregiver_beta, cycle.cycle_id),
        lambda: s.weekly.list_cycles(s.caregiver_beta, plan.focus_plan_id),
        lambda: s.weekly.list_alignments(s.caregiver_beta, cycle.cycle_id),
        lambda: s.weekly.list_observations(s.caregiver_beta, cycle.cycle_id),
        lambda: s.weekly.coverage_summary(s.caregiver_beta, plan.focus_plan_id),
    ):
        with pytest.raises(WeeklyAuthorizationError):
            call()


def test_no_client_supplied_role_can_reach_the_weekly_service(s):
    for fn in (s.weekly.create_cycle, s.weekly.allocate_cycle,
               s.weekly.record_observation, s.weekly.defer_activity,
               s.weekly.create_intervention, s.weekly.generate_next_cycle,
               s.weekly.override_defer):
        params = set(inspect.signature(fn).parameters)
        assert not (params & {"role", "actor_role", "uid", "is_admin",
                              "caregiver_id", "provider_id", "actor_id"}), fn.__name__


def test_every_public_weekly_method_requires_a_principal():
    for name, member in inspect.getmembers(WeeklyService,
                                           predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        params = list(inspect.signature(member).parameters)
        assert params[:2] == ["self", "principal"], (name, params)


def test_an_allocation_from_another_focus_plan_is_refused(s):
    """Goal allocations must belong to the same child AND focus plan."""
    source = inspect.getsource(WeeklyService._reject_foreign_goals)
    assert "child_id" in source and "focus_plan_id" in source


# ===========================================================================
# write-time uniqueness
# ===========================================================================

def test_creating_a_cycle_wins_a_claim(s):
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    digest = key_digest(plan.focus_plan_id, "1")
    held = [c for c in s.repos.identity_claims.list_for_key(digest)
            if c.kind is ClaimKind.WEEKLY_CYCLE]
    assert len(held) == 1
    assert held[0].holder_ref == cycle.cycle_id


def test_a_duplicate_cycle_is_refused(s):
    plan, goals = s.active_month()
    s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                          sequence_in_month=1)
    with pytest.raises(WeeklyConflict):
        s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                              sequence_in_month=1)


def test_allocating_a_cycle_wins_a_claim(s):
    """The claim is AUTHORITATIVE; the advisory read only gives a clear error.

    Asserted explicitly because a mutation removing the claim still failed
    the duplicate-allocation test — the advisory read caught it — and so
    survived. Under contention only the claim decides, and that is proven on
    the real emulator.
    """
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                            candidates_for(goals[0].ref, goals[1].ref),
                            family_declared_capacity=3)
    held = [c for c in s.repos.identity_claims.list_for_key(
        key_digest(cycle.cycle_id)) if c.kind is ClaimKind.WEEKLY_ALLOCATION]
    assert len(held) == 1
    assert held[0].holder_ref == cycle.cycle_id


def test_allocating_a_cycle_twice_is_refused(s):
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                            candidates_for(goals[0].ref, goals[1].ref),
                            family_declared_capacity=3)
    with pytest.raises(WeeklyConflict):
        s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                                candidates_for(goals[0].ref, goals[1].ref),
                                family_declared_capacity=3)


def test_allocation_is_all_or_nothing(s):
    """Claim, alignments, gaps and ledger commit together or not at all.

    Every write is a `create`, so unlike 0.4C activation there is no `set`
    and no boundary to recover across — a crash persists nothing.
    """
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)

    real_create = s.repos.coverage_gaps.__class__.create
    s.repos.coverage_gaps.__class__.create = (
        lambda self, gap: (_ for _ in ()).throw(RuntimeError("process died")))
    try:
        with pytest.raises(RuntimeError):
            s.weekly.allocate_cycle(
                s.provider_alpha, cycle.cycle_id,
                # Only goal 1 is servable, so a gap is written for goal 2 and
                # the injected failure lands mid-transaction.
                [CandidateActivity("activity-A", (goals[0].ref,))],
                family_declared_capacity=3)
    finally:
        s.repos.coverage_gaps.__class__.create = real_create

    assert s.repos.alignments.list_for_cycle(cycle.cycle_id) == []
    assert s.repos.coverage_gaps.list_for_cycle(cycle.cycle_id) == []
    assert s.repos.capacity_ledgers.list_for_cycle(cycle.cycle_id) == []
    assert s.repos.identity_claims.count_claims_for_key(
        key_digest(cycle.cycle_id)) == 0

    # The key was not consumed: a clean retry succeeds.
    result = s.weekly.allocate_cycle(
        s.provider_alpha, cycle.cycle_id,
        candidates_for(goals[0].ref, goals[1].ref),
        family_declared_capacity=3)
    assert result.placed_count > 0


def test_a_crash_creating_a_cycle_persists_nothing(s):
    plan, goals = s.active_month()
    real_create = s.repos.weekly_cycles.__class__.create
    s.repos.weekly_cycles.__class__.create = (
        lambda self, cycle: (_ for _ in ()).throw(RuntimeError("process died")))
    try:
        with pytest.raises(RuntimeError):
            s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    finally:
        s.repos.weekly_cycles.__class__.create = real_create

    assert s.repos.weekly_cycles.list_for_plan(plan.focus_plan_id) == []
    assert s.repos.identity_claims.count_claims_for_key(
        key_digest(plan.focus_plan_id, "1")) == 0
    assert s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                 sequence_in_month=1)


# ===========================================================================
# audit
# ===========================================================================

def test_material_weekly_actions_emit_audit_events(s):
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                            candidates_for(goals[0].ref, goals[1].ref),
                            family_declared_capacity=3)
    instance = s.weekly.list_alignments(
        s.provider_alpha, cycle.cycle_id)[0].activity_instance_ref
    s.weekly.capture_snapshot(s.provider_alpha, cycle.cycle_id,
                              SourceSystem.PARENT, "parent-fict", {"days": []})
    s.weekly.record_observation(
        s.caregiver_alpha, cycle.cycle_id, instance, local_date="2026-10-02",
        attempt_outcome=AttemptOutcome.DID_IT)
    s.weekly.defer_activity(s.caregiver_alpha, cycle.cycle_id, instance)
    s.weekly.create_intervention(
        s.provider_alpha, cycle.cycle_id, action=InterventionAction.ENDORSE,
        applies_to=InterventionScope.FUTURE_CYCLE, clinical_rationale="r")
    s.weekly.generate_next_cycle(s.provider_alpha, cycle.cycle_id,
                                 candidates_for(goals[0].ref, goals[1].ref),
                                 family_declared_capacity=3)

    actions = {e.action for e in s.repos.audit_events.list_all()}
    assert {AuditAction.WEEKLY_CYCLE_CREATED,
            AuditAction.WEEKLY_CYCLE_ALLOCATED,
            AuditAction.WEEKLY_PLAN_SNAPSHOT_CAPTURED,
            AuditAction.OBSERVATION_RECORDED,
            AuditAction.ACTIVITY_DEFERRED,
            AuditAction.THERAPIST_INTERVENTION_CREATED,
            AuditAction.ADAPTATION_RECORDED} <= actions


def test_the_audit_trail_carries_no_clinical_content(s):
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                            candidates_for(goals[0].ref, goals[1].ref),
                            family_declared_capacity=3)
    instance = s.weekly.list_alignments(
        s.provider_alpha, cycle.cycle_id)[0].activity_instance_ref
    s.weekly.record_observation(
        s.caregiver_alpha, cycle.cycle_id, instance, local_date="2026-10-02",
        attempt_outcome=AttemptOutcome.DID_IT,
        observation_text_ref=SENTINEL_NOTE, child_response=SENTINEL_CONCERN)
    s.weekly.create_intervention(
        s.provider_alpha, cycle.cycle_id,
        action=InterventionAction.ADD_GUIDANCE,
        applies_to=InterventionScope.FUTURE_CYCLE,
        clinical_rationale=SENTINEL_NOTE, guidance_text=SENTINEL_CONCERN)

    for event in s.repos.audit_events.list_all():
        blob = " ".join([event.resource_type, event.resource_id or "",
                         *event.metadata.keys(), *event.metadata.values()])
        for sentinel in ALL_SENTINELS:
            assert sentinel not in blob, event.action
        assert "local_date" not in event.metadata
        assert "clinical_rationale" not in event.metadata


def test_no_weekly_metadata_key_escapes_the_allowlist(s):
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                            candidates_for(goals[0].ref, goals[1].ref),
                            family_declared_capacity=3)
    s.weekly.generate_next_cycle(s.provider_alpha, cycle.cycle_id,
                                 candidates_for(goals[0].ref, goals[1].ref),
                                 family_declared_capacity=3)
    for event in s.repos.audit_events.list_all():
        assert set(event.metadata) <= ALLOWED_METADATA_KEYS


def test_weekly_errors_are_phi_safe_by_declaration():
    for error in (WeeklyConflict, WeeklyAuthorizationError,
                  WeeklyValidationError, ReleasedPlanImmutable,
                  WeeklyCycleError, AlignmentError, ObservationError,
                  InterventionError, AdaptationError, AllocationError):
        assert getattr(error, "PHI_SAFE_MESSAGE", False), error.__name__


# ===========================================================================
# persistence
# ===========================================================================

def test_every_new_record_round_trips(s):
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                            candidates_for(goals[0].ref, goals[1].ref),
                            family_declared_capacity=2)
    instance = s.weekly.list_alignments(
        s.provider_alpha, cycle.cycle_id)[0].activity_instance_ref
    s.weekly.link_external_plan(s.provider_alpha, cycle.cycle_id,
                                SourceSystem.PARENT, "parent-fict",
                                coverage_local_dates=("2026-10-01",))
    s.weekly.capture_snapshot(s.provider_alpha, cycle.cycle_id,
                              SourceSystem.PARENT, "parent-fict", {"days": []})
    s.weekly.record_observation(
        s.caregiver_alpha, cycle.cycle_id, instance, local_date="2026-10-02",
        attempt_outcome=AttemptOutcome.DID_IT, difficulty=Difficulty.JUST_RIGHT,
        enjoyment=Enjoyment.ENJOYED)
    s.weekly.defer_activity(s.caregiver_alpha, cycle.cycle_id, instance)
    s.weekly.create_intervention(
        s.provider_alpha, cycle.cycle_id, action=InterventionAction.ENDORSE,
        applies_to=InterventionScope.FUTURE_CYCLE, clinical_rationale="r")
    _, _, record = s.weekly.generate_next_cycle(
        s.provider_alpha, cycle.cycle_id,
        candidates_for(goals[0].ref, goals[1].ref),
        family_declared_capacity=2)

    records = [
        *s.repos.weekly_cycles.list_for_plan(plan.focus_plan_id),
        *s.repos.weekly_plan_links.list_for_cycle(cycle.cycle_id),
        *s.repos.weekly_plan_snapshots.list_for_cycle(cycle.cycle_id),
        *s.repos.alignments.list_for_cycle(cycle.cycle_id),
        *s.repos.coverage_gaps.list_for_cycle(cycle.cycle_id),
        *s.repos.capacity_ledgers.list_for_cycle(cycle.cycle_id),
        *s.repos.observation_events.list_for_cycle(cycle.cycle_id),
        *s.repos.customization_signals.list_for_cycle(cycle.cycle_id),
        *s.repos.defer_records.list_for_child(s.child),
        *s.repos.interventions.list_for_cycle(cycle.cycle_id),
        record,
    ]
    assert len(records) >= 10
    for stored in records:
        assert decode(type(stored), encode(stored)) == stored


def test_a_nested_signal_inherits_codec_strictness():
    record = AdaptationRecord.create(
        "chld_1", "mfpl_1", "wcyc_1", "wcyc_2",
        normalized_signals=(NormalizedSignal(
            SignalKind.CHILD_COMPLETED, SignalSource.CAREGIVER_OBSERVATION,
            "obsv_1"),))
    document = encode(record)
    document["normalized_signals"] = [
        {k: v for k, v in document["normalized_signals"][0].items()
         if k != "goal_ref_key"}]
    with pytest.raises(Exception):
        decode(AdaptationRecord, document)


def test_only_pilot_prefixed_collections_are_used(s):
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                            candidates_for(goals[0].ref, goals[1].ref),
                            family_declared_capacity=2)
    for name in s.store.collections():
        assert name.startswith(PILOT_COLLECTION_PREFIX), name


def test_listings_do_not_tie_on_a_foreign_key(s):
    plan, goals = s.active_month()
    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                            candidates_for(goals[0].ref, goals[1].ref),
                            family_declared_capacity=3)
    alignments = s.repos.alignments.list_for_cycle(cycle.cycle_id)
    assert len({a.alignment_id for a in alignments}) == len(alignments)
    assert alignments == s.repos.alignments.list_for_cycle(cycle.cycle_id)


@pytest.mark.parametrize("repo_name", [
    "weekly_cycles", "weekly_plan_links", "weekly_plan_snapshots",
    "alignments", "coverage_gaps", "capacity_ledgers", "observation_events",
    "customization_signals", "defer_records", "interventions",
    "adaptation_records"])
def test_no_weekly_repository_exposes_a_delete(repo_name):
    repos = FirestoreRepositories(FakeDocumentStore())
    repo = getattr(repos, repo_name)
    banned = ("delete", "remove", "purge", "drop", "destroy", "erase", "truncate")
    for attribute in dir(repo):
        if attribute.startswith("_"):
            continue
        assert not any(w in attribute.lower() for w in banned), (repo_name, attribute)


def test_weekly_modules_never_call_a_delete():
    for relative in ("domain/weekly_cycle.py", "domain/alignment.py",
                     "domain/observation.py", "domain/intervention.py",
                     "domain/adaptation.py", "weekly/service.py",
                     "weekly/allocator.py", "weekly/adaptation.py",
                     "weekly/counting.py"):
        tree = ast.parse((PILOT_ROOT / relative).read_text())
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not ({"delete", "remove", "purge", "drop"} & called), relative


@pytest.mark.parametrize("repo_name", [
    "alignments", "coverage_gaps", "observation_events",
    "customization_signals", "interventions", "adaptation_records",
    "weekly_plan_snapshots"])
def test_immutable_records_have_no_update_path(repo_name):
    repos = FirestoreRepositories(FakeDocumentStore())
    repo = getattr(repos, repo_name)
    assert not hasattr(repo, "update"), repo_name
    assert not hasattr(repo, "set"), repo_name


# ===========================================================================
# scope guard — 0.4D/E only
# ===========================================================================

def test_no_rtm_or_reporting_object_was_implemented():
    banned = {
        "PayerVerification",
        "MonitoringDay", }
    for path in sorted(PILOT_ROOT.rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        names = {n.name for n in ast.walk(ast.parse(path.read_text()))
                 if isinstance(n, ast.ClassDef)}
        assert not (banned & names), (path.name, banned & names)


def test_no_billing_vocabulary_entered_the_weekly_layer():
    banned = {"payer", "member_id", "eligibility", "reimbursement",
              "clearinghouse", "copay", "deductible", "cpt_code", "billable"}
    for relative in ("domain/weekly_cycle.py", "domain/alignment.py",
                     "domain/observation.py", "domain/intervention.py",
                     "domain/adaptation.py", "weekly/service.py",
                     "weekly/allocator.py", "weekly/adaptation.py",
                     "weekly/counting.py"):
        tree = ast.parse((PILOT_ROOT / relative).read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.add(node.name)
        assert not (banned & {n.lower() for n in names}), relative
