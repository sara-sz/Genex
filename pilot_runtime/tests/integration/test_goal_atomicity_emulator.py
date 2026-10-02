"""0.5C — PRE-PHI blocker 3: goal + first version are one transaction.

0.4B/C wrote the `GoalVersion` and then the goal in two separate calls, ordered
version-first so that a crash between them left an INERT orphan rather than a
goal whose `current_version_id` named nothing. Founder-reviewed and accepted
for the fictional freeze, with one objection standing: a clinical record store
must not accumulate unreferenced clinical text even when it is unreachable.

0.5C commits both in one transaction, so neither exists unless both do.

Proven against the REAL emulator, because this is the only place it can be.
`FakeDocumentStore` is a plain dict whose `run_in_transaction` is a lock around
a callable; it would report success for a sequence Firestore refuses, and it
cannot show that a failed commit discards writes already queued in the batch.

What is NOT claimed: uniqueness. A goal id is freshly minted and cannot
collide, and a child may legitimately hold many goals. The property at stake
is ATOMICITY between two records that reference each other, which is why no
claim is acquired here.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth.interface import VerifiedToken
from pilot_backend.auth.resolver import resolve_principal
from pilot_backend.domain.goals import EditType, GoalKind, GoalStatus
from pilot_backend.goals.service import GoalService
from pilot_backend.provisioning import provision_provider_record

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
RACE_WIDTH = 8


class _AdvancingClock:
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
def gw(repos):
    """A child with an ACTIVE connection and a managing clinician."""
    from pilot_backend.connections import ProviderConnectionService
    from pilot_backend.domain.connections import CaregiverChildConnection
    from pilot_backend.domain.entities import Caregiver, Child, Practice
    from pilot_backend.domain.enums import CaregiverRelationship, ProviderDiscipline

    tag = uuid.uuid4().hex[:16]
    clock = _AdvancingClock(T0)
    recorder = AuditRecorder(repos.audit_events, environment="test")

    practice = repos.practices.create(Practice.create(f"P{tag}", now=T0))
    caregiver = repos.caregivers.create(Caregiver.create(
        "Caregiver", auth_subject=f"fictional-cg-{tag}", now=T0))
    child = repos.children.create(
        Child.create(actor_id=caregiver.caregiver_id, now=T0))
    repos.caregiver_child.connect(CaregiverChildConnection.create(
        caregiver.caregiver_id, child.child_id, CaregiverRelationship.PARENT,
        actor_id=caregiver.caregiver_id, now=T0))
    provider = provision_provider_record(
        repos, auth_subject=f"fictional-prov-{tag}",
        practice_id=practice.practice_id, discipline=ProviderDiscipline.SLP,
        display_name="Provider-Hannah", now=T0).provider

    caregiver_principal = resolve_principal(
        VerifiedToken(subject=f"fictional-cg-{tag}"), repos)
    provider_principal = resolve_principal(
        VerifiedToken(subject=provider.auth_subject), repos)

    connections = ProviderConnectionService(
        repos=repos, recorder=recorder, now=clock)
    pending = connections.invite_provider(
        caregiver_principal, child.child_id, provider.provider_id)
    connections.accept_invitation(provider_principal, pending.connection_id)
    connections.assign_managing_clinician(
        caregiver_principal, child.child_id, provider.provider_id)

    class Bundle:
        pass

    bundle = Bundle()
    bundle.repos = repos
    bundle.child = child.child_id
    bundle.caregiver = caregiver_principal
    bundle.provider = provider_principal
    bundle.goals = GoalService(repos=repos, recorder=recorder, now=clock)
    return bundle


def _clinical_goals(gw):
    return [g for g in gw.repos.clinical_goals.list_for_child(gw.child)]


def _versions_for(gw, goal_id: str):
    return gw.repos.goal_versions.list_chain(goal_id)


def _orphan_version_ids(gw):
    """Ids of versions whose goal does not exist. The defect, made measurable.

    Returns a SET of ids rather than a count, and callers compare it against a
    baseline taken at the start of their own test.

    Scoping matters here: the emulator database is deliberately not reset
    between tests, and a `GoalVersion` carries `goal_id` but no `child_id`, so
    an orphan cannot be attributed to a child once its goal is gone. A global
    "there are zero orphans" assertion therefore fails in whichever test runs
    AFTER the one that legitimately created an orphan — which is exactly what
    happened while verifying this suite against the old two-write shape, and
    would have read as a defect in the wrong test.
    """
    from pilot_backend.repository.interface import RecordNotFound

    orphans = set()
    for _doc_id, data in gw.repos.store.list_all("pilot_goal_versions"):
        goal_id = data.get("goal_id")
        if not goal_id:
            continue
        for repo in (gw.repos.clinical_goals, gw.repos.caregiver_goals):
            try:
                repo.get_by_id(goal_id)
                break
            except RecordNotFound:
                continue
        else:
            orphans.add(data.get("version_id"))
    return orphans


def _approve(gw, text="Fictional-target-A"):
    # AUTHORED_FRESH requires a stated reason — a domain invariant, respected
    # rather than routed around by picking a laxer edit type.
    return gw.goals.approve_clinical_goal(
        gw.provider, gw.child, edit_type=EditType.AUTHORED_FRESH,
        text=text, reason="Fictional clinical rationale")


# ===========================================================================
# the happy path, and the lineage it must leave
# ===========================================================================

def test_both_records_exist_after_a_clean_approval(gw):
    baseline = _orphan_version_ids(gw)
    goal = _approve(gw)

    assert goal.current_version_id, "the goal names no version"
    stored = gw.repos.clinical_goals.get_by_id(goal.clinical_goal_id)
    assert stored.current_version_id == goal.current_version_id
    chain = _versions_for(gw, goal.clinical_goal_id)
    assert len(chain) == 1
    assert chain[0].version_id == goal.current_version_id
    assert chain[0].version_number == 1
    assert _orphan_version_ids(gw) == baseline


# ===========================================================================
# fault injection — the blocker itself
# ===========================================================================

def test_a_crash_before_the_goal_write_persists_NEITHER_record(gw):
    """THE blocker 3 proof.

    The version is queued first and the goal write explodes. Under 0.4B/C this
    left a committed orphan version; under one transaction the failed commit
    discards the queued version too.
    """
    from pilot_backend.persistence.firestore_repos import (
        FirestoreClinicalGoalRepository,
    )

    baseline = _orphan_version_ids(gw)
    real_create = FirestoreClinicalGoalRepository.create

    def exploding_create(self, goal):
        raise RuntimeError("process died after the version was queued")

    FirestoreClinicalGoalRepository.create = exploding_create
    try:
        with pytest.raises(RuntimeError):
            _approve(gw)
    finally:
        FirestoreClinicalGoalRepository.create = real_create

    assert _clinical_goals(gw) == [], "a goal survived a failed transaction"
    assert _orphan_version_ids(gw) == baseline, (
        "an orphan GoalVersion survived — PRE-PHI blocker 3 is NOT closed")
    # Nothing is stranded: a clean retry succeeds and leaves one of each.
    goal = _approve(gw)
    assert len(_clinical_goals(gw)) == 1
    assert len(_versions_for(gw, goal.clinical_goal_id)) == 1


def test_a_crash_on_the_version_write_persists_NEITHER_record(gw):
    """The other end of the same transaction."""
    from pilot_backend.persistence.firestore_repos import (
        FirestoreGoalVersionRepository,
    )

    baseline = _orphan_version_ids(gw)
    real_append = FirestoreGoalVersionRepository.append

    def exploding_append(self, version):
        raise RuntimeError("process died before anything was queued")

    FirestoreGoalVersionRepository.append = exploding_append
    try:
        with pytest.raises(RuntimeError):
            _approve(gw)
    finally:
        FirestoreGoalVersionRepository.append = real_append

    assert _clinical_goals(gw) == []
    assert _orphan_version_ids(gw) == baseline
    assert _approve(gw).current_version_id


def test_the_caregiver_path_is_equally_atomic(gw):
    """A caregiver-approved goal carries the family's own words.

    Not RTM-eligible, and an unreferenced orphan of it is no more acceptable.
    """
    from pilot_backend.persistence.firestore_repos import (
        FirestoreCaregiverGoalRepository,
    )

    baseline = _orphan_version_ids(gw)
    real_create = FirestoreCaregiverGoalRepository.create

    def exploding_create(self, goal):
        raise RuntimeError("process died after the version was queued")

    FirestoreCaregiverGoalRepository.create = exploding_create
    try:
        with pytest.raises(RuntimeError):
            gw.goals.approve_caregiver_goal(
                gw.caregiver, gw.child, edit_type=EditType.AUTHORED_FRESH,
                text="Fictional-family-wording",
                reason="Fictional family rationale")
    finally:
        FirestoreCaregiverGoalRepository.create = real_create

    assert gw.repos.caregiver_goals.list_for_child(gw.child) == []
    assert _orphan_version_ids(gw) == baseline


# ===========================================================================
# concurrency — many approvals, no cross-contamination
# ===========================================================================

def test_eight_concurrent_approvals_leave_no_orphan_version(gw):
    """Eight distinct goals, each with exactly one version, no orphans.

    A goal id cannot collide, so this is not a uniqueness race — it is a test
    that eight transactions interleaving on the same two COLLECTIONS each
    commit both of their records or neither.
    """
    baseline = _orphan_version_ids(gw)
    barrier = threading.Barrier(RACE_WIDTH)
    results, errors = [], []
    lock = threading.Lock()

    def runner(index: int) -> None:
        barrier.wait()
        try:
            goal = _approve(gw, text=f"Fictional-target-{index}")
            with lock:
                results.append(goal)
        except Exception as exc:  # noqa: BLE001 - reported below
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=runner, args=(i,))
               for i in range(RACE_WIDTH)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "the harness stalled; not a result"

    assert errors == [], errors
    assert len({g.clinical_goal_id for g in results}) == RACE_WIDTH
    assert len(_clinical_goals(gw)) == RACE_WIDTH
    for goal in results:
        chain = _versions_for(gw, goal.clinical_goal_id)
        assert len(chain) == 1, (goal.clinical_goal_id, len(chain))
        assert chain[0].version_id == goal.current_version_id
    assert _orphan_version_ids(gw) == baseline


def test_a_crash_among_concurrent_approvals_strands_nothing(gw):
    """One writer dies mid-flight while seven succeed.

    The forbidden outcome is a version belonging to the dead writer's goal
    surviving. Every committed goal must still have exactly one version, and
    there must be no version without a goal.
    """
    from pilot_backend.persistence.firestore_repos import (
        FirestoreClinicalGoalRepository,
    )

    baseline = _orphan_version_ids(gw)
    real_create = FirestoreClinicalGoalRepository.create
    state = {"n": 0}
    guard = threading.Lock()

    def sometimes_exploding(self, goal):
        with guard:
            state["n"] += 1
            nth = state["n"]
        if nth == 3:
            raise RuntimeError("process died mid-transaction")
        return real_create(self, goal)

    FirestoreClinicalGoalRepository.create = sometimes_exploding
    barrier = threading.Barrier(RACE_WIDTH)
    survived, died = [], []
    lock = threading.Lock()

    def runner(index: int) -> None:
        barrier.wait()
        try:
            goal = _approve(gw, text=f"Fictional-target-{index}")
            with lock:
                survived.append(goal)
        except Exception:  # noqa: BLE001 - the injected death
            with lock:
                died.append(index)

    try:
        threads = [threading.Thread(target=runner, args=(i,))
                   for i in range(RACE_WIDTH)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            assert not thread.is_alive(), "the harness stalled"
    finally:
        FirestoreClinicalGoalRepository.create = real_create

    assert len(died) >= 1, "the fault was never injected"
    assert len(survived) == RACE_WIDTH - len(died)
    assert len(_clinical_goals(gw)) == len(survived)
    for goal in survived:
        assert len(_versions_for(gw, goal.clinical_goal_id)) == 1
    assert _orphan_version_ids(gw) == baseline, (
        "a dead writer left its GoalVersion behind")


# ===========================================================================
# immutability is preserved, not traded away for atomicity
# ===========================================================================

def test_the_first_version_is_still_immutable(gw):
    """A transaction changed WHEN the version appears, not whether it can change."""
    from pilot_backend.repository.interface import DuplicateRecord

    goal = _approve(gw)
    chain = _versions_for(gw, goal.clinical_goal_id)
    original = chain[0]

    # Appending the same version id again is refused: `append` is create-only.
    with pytest.raises((DuplicateRecord, Exception)):
        gw.repos.goal_versions.append(original)

    # And a revision ADDS a version rather than rewriting version 1.
    gw.goals.revise_goal(gw.provider, goal.ref, "Fictional-target-revised",
                         edit_type=EditType.AUTHORED_FRESH,
                         reason="Fictional revision rationale")
    chain = _versions_for(gw, goal.clinical_goal_id)
    assert len(chain) == 2
    assert chain[0].version_id == original.version_id
    assert chain[0].text == original.text, "version 1 was rewritten"
    assert chain[0].version_number == 1 and chain[1].version_number == 2


def test_an_rtm_eligible_goal_still_requires_the_managing_clinician(gw):
    """The atomicity fix did not loosen authorization.

    A provider with an ACTIVE connection but no managing-clinician assignment
    must still be refused — connection alone is never enough.
    """
    from pilot_backend.connections import ProviderConnectionService
    from pilot_backend.domain.enums import ProviderDiscipline
    from pilot_backend.goals.errors import GoalAuthorizationError

    tag = uuid.uuid4().hex[:12]
    other = provision_provider_record(
        gw.repos, auth_subject=f"fictional-other-{tag}",
        practice_id=gw.repos.providers.get_by_id(
            gw.provider.application_id).practice_id,
        discipline=ProviderDiscipline.SLP, display_name="Provider-Other").provider
    connections = ProviderConnectionService(repos=gw.repos)
    pending = connections.invite_provider(gw.caregiver, gw.child,
                                          other.provider_id)
    other_principal = resolve_principal(
        VerifiedToken(subject=other.auth_subject), gw.repos)
    connections.accept_invitation(other_principal, pending.connection_id)

    baseline = _orphan_version_ids(gw)
    before = len(_clinical_goals(gw))
    with pytest.raises(GoalAuthorizationError):
        gw.goals.approve_clinical_goal(
            other_principal, gw.child,
            edit_type=EditType.AUTHORED_FRESH, text="Fictional-intruder",
            reason="Fictional intruder rationale")
    assert len(_clinical_goals(gw)) == before
    assert _orphan_version_ids(gw) == baseline
