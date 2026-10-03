"""0.4B/C goals and monthly plans against a REAL Firestore emulator.

The `pilot_backend` suite runs the same service code over `FakeDocumentStore`,
which is a plain dict and is NOT thread-safe. Two claims therefore cannot be
made there and are made here instead:

**The child-month claim actually wins races.** Eight threads activating the
same child-month against a real server, exactly one survivor.

**Activation is crash-consistent where it can be.** The port refuses `set`
inside a transaction, so activation is a transaction (claim + snapshots)
followed by one state write. Fault injection proves the transaction is
all-or-nothing and that a crash at the boundary is forward-recoverable by the
rightful holder rather than stranding the key.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth import VerifiedToken, resolve_principal
from pilot_backend.domain.goals import EditType, EvidenceSource, GoalStatus
from pilot_backend.domain.identity_claims import ClaimKind, key_digest
from pilot_backend.domain.monthly_plan import MonthlyFocusPlan, MonthlyPlanState
from pilot_backend.domain.planning_policy import POLICY_2026_10
from pilot_backend.domain.roles import ActorRole
from pilot_backend.goals.errors import GoalConflict
from pilot_backend.goals.service import GoalService
from pilot_backend.goals.suggestion_engine import (
    ObservationSnapshot,
    ObservedDomain,
    generate_suggestions,
)
from pilot_backend.identity import LongitudinalIdentityService
from pilot_backend.persistence import encode
from pilot_backend.persistence.codecs import decode
from pilot_backend.planning.service import MonthlyPlanService

from ..test_sentinels import ALL_SENTINELS

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
CYCLE = "2026-10"
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
def gp(repos, topology, unique_suffix):
    """Goal + planning services over the real emulator-backed repositories."""
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
    bundle.caregiver_alpha = principal(CAREGIVER_ALPHA_SUBJECT)
    bundle.caregiver_beta = principal(CAREGIVER_BETA_SUBJECT)
    bundle.provider_alpha = principal(PROVIDER_ALPHA_SUBJECT)
    bundle.child = topology.child_alpha.child_id
    return bundle


#: 0.5E-A canonical provenance, so suggestions generated here go through the
#: real anchored path and the goals they produce are allocatable. No anchor
#: row is ever written by hand: `generate_suggestions` persists the
#: SuggestionCanonicalAnchor and `approve_clinical_goal` copies it onto the
#: goal inside the same transaction.
def _rung_for(domain, *, months=24, family=None):
    from pilot_backend.domain.canonical_rung import (
        ActivityFamilyBinding,
        CanonicalRung,
    )

    return CanonicalRung.build(
        domain_key=domain, source_rung_months=months,
        milestone_text=f"Fictional canonical rung for {domain}",
        subdomain=f"{domain}_track",
        family_bindings=[ActivityFamilyBinding(family or f"{domain}_family",
                                               (domain,))],
        track_subdomains=(f"{domain}_track",),
        taxonomy_version="activity_family_taxonomy_v1",
        baseline_version="parent-2.4-functional-baseline-v1")


def _snapshot(child_id, cycle=CYCLE) -> ObservationSnapshot:
    return ObservationSnapshot(child_id, cycle, (
        ObservedDomain("talking_and_communicating", True,
                       EvidenceSource.CLINICIAN_OBSERVATION,
                       milestone_refs=("mv1:cdc:comm:24m:two-word",),
                       functional_baseline_area="requesting",
                       canonical_rung=_rung_for("talking_and_communicating")),
        ObservedDomain("fine_motor", True,
                       EvidenceSource.CAREGIVER_REPORTED_MILESTONE,
                       milestone_refs=("mv1:cdc:fine:24m:scribble",),
                       canonical_rung=_rung_for("fine_motor")),
    ))


def _anchored_goal(gp, text, reason="r"):
    """One ALLOCATABLE clinical goal, through the real anchored path.

    0.5E-A. These two call sites used AUTHORED_FRESH only to obtain a goal to
    allocate — their subject is the activation race and the losing-writer
    snapshot rule, not authoring. `MODIFIED` keeps the caller's wording.
    """
    offered = gp.goals.generate_suggestions(
        gp.provider_alpha, gp.child, _snapshot(gp.child))
    return gp.goals.approve_clinical_goal(
        gp.provider_alpha, gp.child, edit_type=EditType.MODIFIED,
        suggestion_id=offered[0].suggestion_id, text=text, reason=reason)


def _prepare(gp, *, cycle=CYCLE, goal_count=2):
    """Managing clinician, approved goals, a draft plan with allocations."""
    gp.identity.assign_managing_clinician(
        gp.provider_alpha, gp.child, gp.topo.provider_alpha.provider_id)
    offered = gp.goals.generate_suggestions(
        gp.provider_alpha, gp.child, _snapshot(gp.child, cycle))
    goals = [gp.goals.approve_clinical_goal(
        gp.provider_alpha, gp.child, edit_type=EditType.ACCEPTED_VERBATIM,
        suggestion_id=x.suggestion_id) for x in offered[:goal_count]]
    plan = gp.plans.create_plan(gp.provider_alpha, gp.child, cycle, ZONE)
    for rank, goal in enumerate(goals, start=1):
        gp.plans.allocate_goal(gp.provider_alpha, plan.focus_plan_id, goal.ref,
                               priority_rank=rank)
    return plan, goals


def _race(target, count: int = 8):
    """Run `target(i)` on `count` threads released together.

    Every thread is accounted for, so a stalled harness reports itself rather
    than returning a short list that reads as a uniqueness failure — see the
    same helper in `test_identity_emulator.py` for the incident.
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

def test_goal_records_round_trip_in_real_firestore(gp):
    plan, goals = _prepare(gp)
    activated = gp.plans.activate_plan(gp.provider_alpha, plan.focus_plan_id)

    assert gp.repos.focus_plans.get_by_id(plan.focus_plan_id) == activated
    for goal in goals:
        stored = gp.repos.clinical_goals.get_by_id(goal.ref.goal_id)
        assert stored.opened_at.tzinfo is not None
        version = gp.repos.goal_versions.get_by_id(stored.current_version_id)
        assert version.actor_role is ActorRole.PROVIDER
    for record in (*gp.repos.goal_allocations.list_for_plan(plan.focus_plan_id),
                   *gp.repos.goal_snapshots.list_for_plan(plan.focus_plan_id),
                   *gp.repos.goal_suggestions.list_for_child(gp.child)):
        assert decode(type(record), encode(record)) == record


def test_nested_evidence_survives_the_real_client(gp):
    offered = gp.goals.generate_suggestions(
        gp.caregiver_alpha, gp.child, _snapshot(gp.child))
    stored = gp.repos.goal_suggestions.get_by_id(offered[0].suggestion_id)
    assert stored.evidence == offered[0].evidence
    assert stored.evidence.milestone_refs == offered[0].evidence.milestone_refs
    assert stored.evidence.evidence_source is EvidenceSource.CLINICIAN_OBSERVATION


def test_the_suggestion_engine_is_deterministic_against_the_real_store(gp):
    first = generate_suggestions(_snapshot(gp.child))
    second = generate_suggestions(_snapshot(gp.child))
    assert [x.family_facing_text_template for x in first] == \
           [x.family_facing_text_template for x in second]


# ===========================================================================
# contention — only provable here
# ===========================================================================

def test_concurrent_activations_of_one_child_month_yield_exactly_one(gp):
    """Eight threads, eight draft plans, one October. One survivor."""
    gp.identity.assign_managing_clinician(
        gp.provider_alpha, gp.child, gp.topo.provider_alpha.provider_id)
    goal = _anchored_goal(gp, "Shared focus.", "race setup")

    drafts = []
    for _ in range(8):
        plan = MonthlyFocusPlan.create(
            gp.child, CYCLE, ZONE, policy_version=POLICY_2026_10.policy_version)
        gp.repos.focus_plans.create(plan)
        gp.plans.allocate_goal(gp.provider_alpha, plan.focus_plan_id, goal.ref,
                               priority_rank=1)
        drafts.append(plan)

    def attempt(index: int):
        return gp.plans.activate_plan(gp.provider_alpha,
                                      drafts[index].focus_plan_id)

    results, errors = _race(attempt)
    assert len(results) == 1, f"expected one winner, got {len(results)}"
    assert len(errors) == 7
    assert all(isinstance(e, GoalConflict) for e in errors), \
        {type(e).__name__ for e in errors}

    active = [p for p in gp.repos.focus_plans.list_for_cycle(gp.child, CYCLE)
              if p.is_active]
    assert len(active) == 1
    assert gp.repos.identity_claims.count_claims_for_key(
        key_digest(gp.child, CYCLE)) == 1


def test_a_losing_activation_writes_no_snapshot(gp):
    """The transaction is all-or-nothing: losers leave nothing behind."""
    plan, _ = _prepare(gp)
    gp.plans.activate_plan(gp.provider_alpha, plan.focus_plan_id)

    loser = MonthlyFocusPlan.create(
        gp.child, CYCLE, ZONE, policy_version=POLICY_2026_10.policy_version)
    gp.repos.focus_plans.create(loser)
    goal = _anchored_goal(gp, "Losing focus.", "r")
    gp.plans.allocate_goal(gp.provider_alpha, loser.focus_plan_id, goal.ref,
                           priority_rank=1)

    with pytest.raises(GoalConflict):
        gp.plans.activate_plan(gp.provider_alpha, loser.focus_plan_id)
    assert gp.repos.goal_snapshots.list_for_plan(loser.focus_plan_id) == []
    assert gp.repos.focus_plans.get_by_id(loser.focus_plan_id).state \
        is MonthlyPlanState.DRAFT


def test_a_crash_inside_the_activation_transaction_persists_nothing(gp):
    """Fault injection against the real server, not the fake."""
    plan, _ = _prepare(gp)
    real_create = gp.repos.goal_snapshots.__class__.create
    calls = {"n": 0}

    def exploding_create(self, snapshot):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("process died mid-transaction")
        return real_create(self, snapshot)

    gp.repos.goal_snapshots.__class__.create = exploding_create
    try:
        with pytest.raises(RuntimeError):
            gp.plans.activate_plan(gp.provider_alpha, plan.focus_plan_id)
    finally:
        gp.repos.goal_snapshots.__class__.create = real_create

    assert gp.repos.goal_snapshots.list_for_plan(plan.focus_plan_id) == []
    assert gp.repos.identity_claims.count_claims_for_key(
        key_digest(gp.child, CYCLE)) == 0
    assert gp.repos.focus_plans.get_by_id(plan.focus_plan_id).state \
        is MonthlyPlanState.DRAFT

    # And the key was not consumed: a clean retry succeeds.
    assert gp.plans.activate_plan(
        gp.provider_alpha, plan.focus_plan_id).state is MonthlyPlanState.ACTIVE


def test_a_crash_after_the_transaction_is_forward_recoverable(gp):
    """Claim and snapshots committed; the state write did not.

    The rightful holder retries, recognises its own claim, skips the
    transaction — so no duplicate snapshots — and completes. The key is never
    stranded and no cleanup job exists to hide the problem.
    """
    plan, _ = _prepare(gp)
    real_update = gp.repos.focus_plans.update
    gp.repos.focus_plans.update = lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError("process died before the state write"))
    try:
        with pytest.raises(RuntimeError):
            gp.plans.activate_plan(gp.provider_alpha, plan.focus_plan_id)
    finally:
        gp.repos.focus_plans.update = real_update

    assert len(gp.repos.goal_snapshots.list_for_plan(plan.focus_plan_id)) == 2
    recovered = gp.plans.activate_plan(gp.provider_alpha, plan.focus_plan_id)
    assert recovered.state is MonthlyPlanState.ACTIVE
    assert len(gp.repos.goal_snapshots.list_for_plan(plan.focus_plan_id)) == 2


# ===========================================================================
# history that must survive a real store
# ===========================================================================

def test_snapshots_outlive_a_later_goal_edit_in_firestore(gp):
    plan, goals = _prepare(gp)
    activated = gp.plans.activate_plan(gp.provider_alpha, plan.focus_plan_id)
    before = [x.goal_text_at_snapshot for x in
              gp.repos.goal_snapshots.list_for_plan(activated.focus_plan_id)]

    gp.goals.revise_goal(gp.provider_alpha, goals[0].ref,
                         "Rewritten a month later.", reason="new evidence")

    after = [x.goal_text_at_snapshot for x in
             gp.repos.goal_snapshots.list_for_plan(activated.focus_plan_id)]
    assert after == before


def test_the_version_chain_persists_in_order(gp):
    plan, goals = _prepare(gp)
    gp.goals.revise_goal(gp.provider_alpha, goals[0].ref, "Second wording.",
                         reason="narrowed")
    gp.goals.revise_goal(gp.provider_alpha, goals[0].ref, "Third wording.",
                         reason="narrowed again")
    chain = gp.repos.goal_versions.list_chain(goals[0].ref.goal_id)
    assert [v.version_number for v in chain] == [1, 2, 3]
    assert chain[2].supersedes_version_id == chain[1].version_id


def test_reprioritising_keeps_both_rows_in_firestore(gp):
    plan, goals = _prepare(gp)
    first = gp.repos.goal_allocations.list_for_plan(plan.focus_plan_id)[0]
    second = gp.plans.reprioritize(
        gp.provider_alpha, first.allocation_id, priority_rank=2,
        effective_from_cycle=2, reason="mid-month change")
    rows = gp.repos.goal_allocations.list_for_plan(plan.focus_plan_id,
                                                   include_inactive=True)
    assert {r.allocation_id for r in rows} >= {first.allocation_id,
                                               second.allocation_id}
    assert gp.repos.goal_allocations.get_by_id(
        first.allocation_id).superseded_by_allocation_id == second.allocation_id


def test_a_retired_goal_remains_readable(gp):
    plan, goals = _prepare(gp)
    gp.goals.set_goal_status(gp.provider_alpha, goals[0].ref, GoalStatus.RETIRED)
    stored = gp.repos.clinical_goals.get_by_id(goals[0].ref.goal_id)
    assert not stored.is_active
    assert stored.closed_at is not None
    assert gp.repos.goal_versions.list_chain(goals[0].ref.goal_id)


# ===========================================================================
# leakage
# ===========================================================================

def test_audit_events_persist_and_leak_no_goal_content(gp):
    plan, goals = _prepare(gp)
    gp.plans.activate_plan(gp.provider_alpha, plan.focus_plan_id)
    gp.goals.revise_goal(gp.provider_alpha, goals[0].ref, ALL_SENTINELS[1],
                         reason=ALL_SENTINELS[3])

    events = gp.repos.audit_events.list_for_child(gp.child)
    assert events
    for event in events:
        blob = " ".join([event.resource_type, event.resource_id or "",
                         *event.metadata.keys(), *event.metadata.values()])
        for sentinel in ALL_SENTINELS:
            assert sentinel not in blob, event.action


def test_goal_collections_are_pilot_prefixed(gp):
    plan, _ = _prepare(gp)
    gp.plans.activate_plan(gp.provider_alpha, plan.focus_plan_id)
    for name in ("pilot_goal_suggestions", "pilot_goal_versions",
                 "pilot_clinical_goals", "pilot_monthly_focus_plans",
                 "pilot_monthly_goal_allocations",
                 "pilot_monthly_goal_snapshots"):
        assert list(gp.repos.store.list_all(name)), name
