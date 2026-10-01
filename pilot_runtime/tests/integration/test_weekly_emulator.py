"""0.4D/E weekly allocation, evidence and adaptation on a REAL emulator.

The `pilot_backend` suite runs the same service code over `FakeDocumentStore`,
which is a plain dict and is NOT thread-safe. Three claims therefore cannot be
made there and are made here:

**The cycle claim wins races.** Eight threads creating cycle 1 of one month
against a real server — exactly one survivor.

**The allocation claim wins races.** Eight threads allocating one cycle —
exactly one survivor, and the losers leave no alignment, gap or ledger behind.

**Allocation is genuinely atomic.** Every write it makes is a `create`, so
unlike 0.4C activation there is no `set` and no boundary to recover across.
Fault injection against the real server proves a crash persists nothing and
consumes no generation.

FICTIONAL ONLY — the neutral `build_secure_topology` aliases throughout.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth import VerifiedToken, resolve_principal
from pilot_backend.domain.adaptation import AdaptationOrigin, SignalKind
from pilot_backend.domain.goals import EditType, GoalRef
from pilot_backend.domain.identity_claims import ClaimKind, key_digest
from pilot_backend.domain.intervention import InterventionAction, InterventionScope
from pilot_backend.domain.observation import AttemptOutcome, Difficulty
from pilot_backend.domain.source_link import SourceSystem
from pilot_backend.goals.service import GoalService
from pilot_backend.identity import LongitudinalIdentityService
from pilot_backend.persistence import encode
from pilot_backend.persistence.codecs import decode
from pilot_backend.planning.service import MonthlyPlanService
from pilot_backend.weekly.allocator import CandidateActivity
from pilot_backend.weekly.errors import WeeklyConflict
from pilot_backend.weekly.service import WeeklyService

from ..test_sentinels import ALL_SENTINELS

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
CYCLE_MONTH = "2026-10"
ZONE = "America/New_York"


class _AdvancingClock:
    """Deterministic, strictly increasing and thread-safe — see 0.4A."""

    def __init__(self, start: datetime) -> None:
        self._start = start.replace(microsecond=0)
        self._tick = 0
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            self._tick += 1
            tick = self._tick
        return self._start + timedelta(seconds=tick)


@pytest.fixture()
def wk(repos, topology, unique_suffix):
    """Weekly services over the real emulator-backed repositories."""
    from pilot_backend.fixtures.secure_topology import (
        CAREGIVER_ALPHA_SUBJECT,
        CAREGIVER_BETA_SUBJECT,
        PROVIDER_ALPHA_SUBJECT,
    )

    recorder = AuditRecorder(repos.audit_events, environment="test")
    clock = _AdvancingClock(T0)

    def principal(base):
        return resolve_principal(VerifiedToken(subject=base + unique_suffix), repos)

    class Bundle:
        pass

    bundle = Bundle()
    bundle.repos = repos
    bundle.topo = topology
    bundle.suffix = unique_suffix
    bundle.identity = LongitudinalIdentityService(
        repos=repos, recorder=recorder, now=clock)
    bundle.goals = GoalService(repos=repos, recorder=recorder, now=clock)
    bundle.plans = MonthlyPlanService(repos=repos, recorder=recorder, now=clock)
    bundle.weekly = WeeklyService(repos=repos, recorder=recorder, now=clock)
    bundle.caregiver_alpha = principal(CAREGIVER_ALPHA_SUBJECT)
    bundle.caregiver_beta = principal(CAREGIVER_BETA_SUBJECT)
    bundle.provider_alpha = principal(PROVIDER_ALPHA_SUBJECT)
    bundle.child = topology.child_alpha.child_id
    return bundle


def _active_month(wk, *, goal_count: int = 2):
    wk.identity.assign_managing_clinician(
        wk.provider_alpha, wk.child, wk.topo.provider_alpha.provider_id)
    goals = [wk.goals.approve_clinical_goal(
        wk.provider_alpha, wk.child, edit_type=EditType.AUTHORED_FRESH,
        text=f"Fictional focus {n}.", reason="pilot scenario")
        for n in range(1, goal_count + 1)]
    plan = wk.plans.create_plan(wk.provider_alpha, wk.child, CYCLE_MONTH, ZONE)
    for rank, goal in enumerate(goals, start=1):
        wk.plans.allocate_goal(wk.provider_alpha, plan.focus_plan_id, goal.ref,
                               priority_rank=rank)
    return wk.plans.activate_plan(wk.provider_alpha, plan.focus_plan_id), goals


def _candidates(goal_a: GoalRef, goal_b: GoalRef):
    return [
        CandidateActivity("activity-A", (goal_a,), primary_for=goal_a),
        CandidateActivity("activity-B", (goal_b,), primary_for=goal_b),
        CandidateActivity("activity-C", (goal_a, goal_b), primary_for=goal_a),
    ]


def _race(target, count: int = 8):
    """Run `target(i)` on `count` threads released together.

    Every thread is accounted for, so a stalled harness reports itself rather
    than returning a short list that reads as a uniqueness failure — see the
    incident recorded in `test_identity_emulator.py`.
    """
    barrier = threading.Barrier(count)
    results, errors = [], []
    lock = threading.Lock()

    def runner(index: int) -> None:
        barrier.wait()
        try:
            outcome = target(index)
            with lock:
                results.append(outcome)
        except Exception as exc:  # noqa: BLE001 - classified by the caller
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    stalled = 0
    for thread in threads:
        thread.join(timeout=60)
        if thread.is_alive():
            stalled += 1
    assert stalled == 0, (
        f"{stalled} of {count} racing threads did not finish within 60s — "
        "the harness stalled; this is not a uniqueness result")
    assert len(results) + len(errors) == count
    return results, errors


# ===========================================================================
# round-trips through the real adapter
# ===========================================================================

def test_weekly_records_round_trip_in_real_firestore(wk):
    plan, goals = _active_month(wk)
    cycle = wk.weekly.create_cycle(wk.provider_alpha, plan.focus_plan_id,
                                   sequence_in_month=1)
    wk.weekly.allocate_cycle(wk.provider_alpha, cycle.cycle_id,
                             _candidates(goals[0].ref, goals[1].ref),
                             family_declared_capacity=3)
    wk.weekly.link_external_plan(wk.provider_alpha, cycle.cycle_id,
                                 SourceSystem.PARENT, "parent-fict",
                                 coverage_local_dates=("2026-10-01",))
    wk.weekly.capture_snapshot(wk.provider_alpha, cycle.cycle_id,
                               SourceSystem.PARENT, "parent-fict",
                               {"days": [{"local_date": "2026-10-01"}]})
    instance = wk.weekly.list_alignments(
        wk.provider_alpha, cycle.cycle_id)[0].activity_instance_ref
    wk.weekly.record_observation(
        wk.caregiver_alpha, cycle.cycle_id, instance, local_date="2026-10-02",
        attempt_outcome=AttemptOutcome.DID_IT, difficulty=Difficulty.JUST_RIGHT)

    for record in (*wk.repos.weekly_cycles.list_for_plan(plan.focus_plan_id),
                   *wk.repos.weekly_plan_links.list_for_cycle(cycle.cycle_id),
                   *wk.repos.weekly_plan_snapshots.list_for_cycle(cycle.cycle_id),
                   *wk.repos.alignments.list_for_cycle(cycle.cycle_id),
                   *wk.repos.capacity_ledgers.list_for_cycle(cycle.cycle_id),
                   *wk.repos.observation_events.list_for_cycle(cycle.cycle_id)):
        assert decode(type(record), encode(record)) == record


def test_the_snapshot_document_survives_the_real_client(wk):
    plan, goals = _active_month(wk)
    cycle = wk.weekly.create_cycle(wk.provider_alpha, plan.focus_plan_id,
                                   sequence_in_month=1)
    wk.weekly.allocate_cycle(wk.provider_alpha, cycle.cycle_id,
                             _candidates(goals[0].ref, goals[1].ref),
                             family_declared_capacity=3)
    document = {"days": [{"local_date": "2026-10-01", "activities": ["a", "b"]}]}
    snapshot = wk.weekly.capture_snapshot(
        wk.provider_alpha, cycle.cycle_id, SourceSystem.PARENT, "parent-fict",
        document)
    stored = wk.repos.weekly_plan_snapshots.get_by_id(snapshot.snapshot_id)
    assert stored.document() == document
    assert stored.captured_at.tzinfo is not None


def test_the_adaptation_record_round_trips_with_nested_signals(wk):
    plan, goals = _active_month(wk)
    cycle = wk.weekly.create_cycle(wk.provider_alpha, plan.focus_plan_id,
                                   sequence_in_month=1)
    wk.weekly.allocate_cycle(wk.provider_alpha, cycle.cycle_id,
                             _candidates(goals[0].ref, goals[1].ref),
                             family_declared_capacity=3)
    instance = wk.weekly.list_alignments(
        wk.provider_alpha, cycle.cycle_id)[0].activity_instance_ref
    wk.weekly.record_observation(
        wk.caregiver_alpha, cycle.cycle_id, instance, local_date="2026-10-02",
        attempt_outcome=AttemptOutcome.WASNT_READY_YET)

    _, _, record = wk.weekly.generate_next_cycle(
        wk.provider_alpha, cycle.cycle_id,
        _candidates(goals[0].ref, goals[1].ref), family_declared_capacity=3)

    stored = wk.repos.adaptation_records.get_by_id(record.record_id)
    assert stored == record
    assert stored.normalized_signals
    assert SignalKind.CHILD_WASNT_READY in {s.kind for s in stored.normalized_signals}
    assert stored.not_a_failure


# ===========================================================================
# contention — only provable here
# ===========================================================================

def test_concurrent_cycle_creation_yields_exactly_one(wk):
    """Eight threads, one month, one cycle 1."""
    plan, goals = _active_month(wk)

    def attempt(index: int):
        return wk.weekly.create_cycle(wk.provider_alpha, plan.focus_plan_id,
                                      sequence_in_month=1)

    results, errors = _race(attempt)
    assert len(results) == 1, f"expected one winner, got {len(results)}"
    assert len(errors) == 7
    assert all(isinstance(e, WeeklyConflict) for e in errors), \
        {type(e).__name__ for e in errors}
    assert len(wk.repos.weekly_cycles.list_for_plan(plan.focus_plan_id)) == 1
    assert wk.repos.identity_claims.count_claims_for_key(
        key_digest(plan.focus_plan_id, "1")) == 1


def test_concurrent_allocation_of_one_cycle_yields_exactly_one(wk):
    """Eight threads allocating the same cycle. Losers leave nothing."""
    plan, goals = _active_month(wk)
    cycle = wk.weekly.create_cycle(wk.provider_alpha, plan.focus_plan_id,
                                   sequence_in_month=1)
    candidates = _candidates(goals[0].ref, goals[1].ref)

    def attempt(index: int):
        return wk.weekly.allocate_cycle(wk.provider_alpha, cycle.cycle_id,
                                        candidates, family_declared_capacity=3)

    results, errors = _race(attempt)
    assert len(results) == 1, f"expected one winner, got {len(results)}"
    assert all(isinstance(e, WeeklyConflict) for e in errors), \
        {type(e).__name__ for e in errors}

    winner = results[0]
    alignments = wk.repos.alignments.list_for_cycle(cycle.cycle_id)
    ledgers = wk.repos.capacity_ledgers.list_for_cycle(cycle.cycle_id)
    assert len(ledgers) == 1, "losers wrote no ledger"
    assert len({a.activity_instance_ref for a in alignments}) == winner.placed_count
    assert wk.repos.identity_claims.count_claims_for_key(
        key_digest(cycle.cycle_id)) == 1


def test_a_crash_during_allocation_persists_nothing_in_firestore(wk):
    """Fault injection against the real server. Every write is a create."""
    plan, goals = _active_month(wk)
    cycle = wk.weekly.create_cycle(wk.provider_alpha, plan.focus_plan_id,
                                   sequence_in_month=1)

    real_create = wk.repos.capacity_ledgers.__class__.create
    wk.repos.capacity_ledgers.__class__.create = (
        lambda self, ledger: (_ for _ in ()).throw(
            RuntimeError("process died mid-transaction")))
    try:
        with pytest.raises(RuntimeError):
            wk.weekly.allocate_cycle(wk.provider_alpha, cycle.cycle_id,
                                     _candidates(goals[0].ref, goals[1].ref),
                                     family_declared_capacity=3)
    finally:
        wk.repos.capacity_ledgers.__class__.create = real_create

    assert wk.repos.alignments.list_for_cycle(cycle.cycle_id) == []
    assert wk.repos.capacity_ledgers.list_for_cycle(cycle.cycle_id) == []
    assert wk.repos.identity_claims.count_claims_for_key(
        key_digest(cycle.cycle_id)) == 0

    # No generation consumed: a clean retry succeeds.
    result = wk.weekly.allocate_cycle(
        wk.provider_alpha, cycle.cycle_id,
        _candidates(goals[0].ref, goals[1].ref), family_declared_capacity=3)
    assert result.placed_count > 0


def test_concurrent_observations_all_persist(wk):
    """Evidence has no uniqueness constraint: every attempt is its own fact."""
    plan, goals = _active_month(wk)
    cycle = wk.weekly.create_cycle(wk.provider_alpha, plan.focus_plan_id,
                                   sequence_in_month=1)
    wk.weekly.allocate_cycle(wk.provider_alpha, cycle.cycle_id,
                             _candidates(goals[0].ref, goals[1].ref),
                             family_declared_capacity=3)
    instance = wk.weekly.list_alignments(
        wk.provider_alpha, cycle.cycle_id)[0].activity_instance_ref

    def attempt(index: int):
        return wk.weekly.record_observation(
            wk.caregiver_alpha, cycle.cycle_id, instance,
            local_date="2026-10-02", attempt_outcome=AttemptOutcome.DID_IT)

    results, errors = _race(attempt, count=6)
    assert errors == []
    assert len(results) == 6
    stored = wk.repos.observation_events.list_for_cycle(cycle.cycle_id)
    assert len({e.event_id for e in stored}) == 6


# ===========================================================================
# the fictional scenario, against the real store
# ===========================================================================

def test_the_october_scenario_against_firestore(wk):
    plan, goals = _active_month(wk)
    goal_a, goal_b = goals[0].ref, goals[1].ref

    cycle1 = wk.weekly.create_cycle(wk.provider_alpha, plan.focus_plan_id,
                                    sequence_in_month=1)
    assert (cycle1.starts_on, cycle1.ends_on) == ("2026-10-01", "2026-10-04")
    assert cycle1.is_partial

    result = wk.weekly.allocate_cycle(
        wk.provider_alpha, cycle1.cycle_id, _candidates(goal_a, goal_b),
        family_declared_capacity=3)
    assert result.gaps == ()

    alignments = wk.weekly.list_alignments(wk.provider_alpha, cycle1.cycle_id)
    instances = {a.activity_identity_ref: a.activity_instance_ref
                 for a in alignments}
    c_instances = {a.activity_instance_ref for a in alignments
                   if a.activity_identity_ref == "activity-C"}
    assert len(c_instances) == 1, "the multi-goal activity is ONE opportunity"

    wk.weekly.record_observation(
        wk.caregiver_alpha, cycle1.cycle_id, instances["activity-A"],
        local_date="2026-10-02", attempt_outcome=AttemptOutcome.DID_IT)
    wk.weekly.record_observation(
        wk.caregiver_alpha, cycle1.cycle_id, instances["activity-B"],
        local_date="2026-10-03", attempt_outcome=AttemptOutcome.WASNT_READY_YET)
    wk.weekly.defer_activity(wk.caregiver_alpha, cycle1.cycle_id,
                             instances["activity-C"])
    wk.weekly.create_intervention(
        wk.provider_alpha, cycle1.cycle_id,
        action=InterventionAction.ADD_GUIDANCE,
        applies_to=InterventionScope.FUTURE_CYCLE,
        clinical_rationale="carry the routine forward",
        guidance_text="fictional guidance", target_ref=instances["activity-B"])

    cycle2, result2, record = wk.weekly.generate_next_cycle(
        wk.provider_alpha, cycle1.cycle_id, _candidates(goal_a, goal_b),
        family_declared_capacity=3)

    assert (cycle2.starts_on, cycle2.ends_on) == ("2026-10-05", "2026-10-11")
    placed = {p.activity_identity_ref for p in result2.placements}
    assert "activity-C" not in placed, "the defer was honoured against Firestore"
    assert record.origin is AdaptationOrigin.MIXED
    assert record.not_a_failure

    summary = wk.weekly.coverage_summary(wk.provider_alpha, plan.focus_plan_id)
    assert summary.total_attempts == 2
    assert summary.total_completed == 1


def test_month_boundary_attribution_in_firestore(wk):
    """A cycle spanning Oct 26 – Nov 1 splits by LOCAL date."""
    plan, goals = _active_month(wk)
    cycle = wk.weekly.create_cycle(wk.provider_alpha, plan.focus_plan_id,
                                   sequence_in_month=5)
    assert cycle.spans_month_boundary
    wk.weekly.allocate_cycle(wk.provider_alpha, cycle.cycle_id,
                             _candidates(goals[0].ref, goals[1].ref),
                             family_declared_capacity=3)
    instance = wk.weekly.list_alignments(
        wk.provider_alpha, cycle.cycle_id)[0].activity_instance_ref

    wk.weekly.record_observation(
        wk.caregiver_alpha, cycle.cycle_id, instance, local_date="2026-10-31",
        attempt_outcome=AttemptOutcome.DID_IT)
    wk.weekly.record_observation(
        wk.caregiver_alpha, cycle.cycle_id, instance, local_date="2026-11-01",
        attempt_outcome=AttemptOutcome.DID_IT)

    october = wk.repos.observation_events.list_for_attribution_month(
        wk.child, "2026-10")
    november = wk.repos.observation_events.list_for_attribution_month(
        wk.child, "2026-11")
    assert len(october) == 1 and len(november) == 1
    assert october[0].owning_cycle_id == november[0].owning_cycle_id


def test_defer_suppression_and_re_eligibility_in_firestore(wk):
    plan, goals = _active_month(wk)
    goal_a, goal_b = goals[0].ref, goals[1].ref
    cycle1 = wk.weekly.create_cycle(wk.provider_alpha, plan.focus_plan_id,
                                    sequence_in_month=1)
    wk.weekly.allocate_cycle(wk.provider_alpha, cycle1.cycle_id,
                             _candidates(goal_a, goal_b),
                             family_declared_capacity=3)
    instances = {a.activity_identity_ref: a.activity_instance_ref
                 for a in wk.weekly.list_alignments(wk.provider_alpha,
                                                    cycle1.cycle_id)}
    wk.weekly.defer_activity(wk.caregiver_alpha, cycle1.cycle_id,
                             instances["activity-C"])

    cycle2, result2, _ = wk.weekly.generate_next_cycle(
        wk.provider_alpha, cycle1.cycle_id, _candidates(goal_a, goal_b),
        family_declared_capacity=3)
    assert "activity-C" not in {p.activity_identity_ref
                                for p in result2.placements}

    cycle3, result3, _ = wk.weekly.generate_next_cycle(
        wk.provider_alpha, cycle2.cycle_id, _candidates(goal_a, goal_b),
        family_declared_capacity=3)
    # Eligible again — not retired. Whether it is actually chosen is a
    # planner decision, so this asserts only that it is no longer suppressed.
    defers = wk.repos.defer_records.list_for_child(wk.child)
    assert all(not d.suppresses(cycle3.sequence_in_month) for d in defers)


def test_audit_events_persist_and_leak_no_clinical_content(wk):
    plan, goals = _active_month(wk)
    cycle = wk.weekly.create_cycle(wk.provider_alpha, plan.focus_plan_id,
                                   sequence_in_month=1)
    wk.weekly.allocate_cycle(wk.provider_alpha, cycle.cycle_id,
                             _candidates(goals[0].ref, goals[1].ref),
                             family_declared_capacity=3)
    instance = wk.weekly.list_alignments(
        wk.provider_alpha, cycle.cycle_id)[0].activity_instance_ref
    wk.weekly.record_observation(
        wk.caregiver_alpha, cycle.cycle_id, instance, local_date="2026-10-02",
        attempt_outcome=AttemptOutcome.DID_IT,
        observation_text_ref=ALL_SENTINELS[1])
    wk.weekly.create_intervention(
        wk.provider_alpha, cycle.cycle_id,
        action=InterventionAction.ADD_GUIDANCE,
        applies_to=InterventionScope.FUTURE_CYCLE,
        clinical_rationale=ALL_SENTINELS[1], guidance_text=ALL_SENTINELS[3])

    events = wk.repos.audit_events.list_for_child(wk.child)
    assert events
    for event in events:
        blob = " ".join([event.resource_type, event.resource_id or "",
                         *event.metadata.keys(), *event.metadata.values()])
        for sentinel in ALL_SENTINELS:
            assert sentinel not in blob, event.action


def test_weekly_collections_are_pilot_prefixed(wk):
    plan, goals = _active_month(wk)
    cycle = wk.weekly.create_cycle(wk.provider_alpha, plan.focus_plan_id,
                                   sequence_in_month=1)
    wk.weekly.allocate_cycle(wk.provider_alpha, cycle.cycle_id,
                             _candidates(goals[0].ref, goals[1].ref),
                             family_declared_capacity=3)
    for name in ("pilot_weekly_cycles", "pilot_activity_goal_alignments",
                 "pilot_capacity_ledgers"):
        assert list(wk.repos.store.list_all(name)), name
