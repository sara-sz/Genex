"""0.5B — the stale-assignment race, proven by FORCED interleaving.

`test_provider_connection_emulator.py` races eight threads and asserts the
outcome. That catches gross errors but it is not a proof: the interleaving it
needs may simply not occur, and a mutation sweep showed exactly that — both
concurrency guards survived while that suite passed.

This module forces the orderings instead. No sleeps, no retries, no reliance on
the scheduler: a one-shot barrier is injected at the transaction boundary
through the service's own `repos_factory` seam, so the test decides who reaches
commit first.

## Where the barrier goes, and why it is the only place that works

AFTER every read in the transaction and BEFORE its first write.

Hooking earlier does not work, and the reason is worth recording because the
first attempt at this test was wrong in a way that would have passed on broken
code. The mutation under test moves the assignment lookup from `tx` to the
non-transactional `self._repos`, and that lookup sits AFTER the connection
read. A barrier before it would let the mutated code read assignments while
blocked — it would see the competing assignment, end it, and the test would go
green against the defect it exists to catch.

Placed before the first write, the two implementations diverge on the only
thing that matters: what is in the transaction's READ SET.

    correct : the assignment query ran through `tx`, so the competing commit
              conflicts, Firestore retries, and the retry sees the assignment
    mutated : the query ran outside `tx`, so nothing conflicts and the stale
              assignment survives the commit

## Why a retry cannot deadlock on the barrier

The hook is ONE-SHOT. Firestore re-invokes the transaction function on
conflict, and a barrier that blocked every attempt would hang the retry that
is supposed to resolve the race. The first attempt blocks; every later attempt
runs straight through.

## Production contains no test seam

`repos_factory` exists because the service must build repositories over a
transaction-bound store — it is load-bearing in production and is merely
*supplied* by the test here. Nothing in `pilot_backend` sleeps, polls, or
checks for a test mode; a structural test below asserts that.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth.interface import VerifiedToken
from pilot_backend.auth.resolver import resolve_principal
from pilot_backend.authz.policy import authorize_child_access
from pilot_backend.connections import ProviderConnectionService
from pilot_backend.connections.errors import ConnectionStateConflict
from pilot_backend.domain.enums import ConnectionStatus, ProviderDiscipline
from pilot_backend.persistence.firestore_repos import FirestoreRepositories
from pilot_backend.provisioning import provision_provider_record

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
_WAIT = 60.0


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


class OneShotBarrier:
    """Blocks the FIRST call and lets every later one pass.

    One-shot because Firestore re-invokes a transaction function on conflict,
    and the retry is the attempt that must be allowed to finish.
    """

    def __init__(self) -> None:
        self.reached = threading.Event()
        self._release = threading.Event()
        self._fired = False
        self._lock = threading.Lock()
        self.attempts = 0

    def __call__(self) -> None:
        with self._lock:
            self.attempts += 1
            if self._fired:
                return
            self._fired = True
        self.reached.set()
        if not self._release.wait(timeout=_WAIT):  # pragma: no cover
            raise AssertionError("barrier was never released")

    def release(self) -> None:
        self._release.set()

    def await_reached(self) -> None:
        assert self.reached.wait(timeout=_WAIT), (
            "the hooked operation never reached the barrier; the seam is in "
            "the wrong place or the code path changed")


class _HookedMethod:
    """Wraps one repository, firing `hook` just BEFORE the named method."""

    def __init__(self, inner, method: str, hook) -> None:
        self._inner = inner
        self._method = method
        self._hook = hook

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name != self._method:
            return attr

        def _wrapped(*args, **kwargs):
            self._hook()
            return attr(*args, **kwargs)

        return _wrapped


class _HookedRepos:
    """A repository set with one method instrumented."""

    def __init__(self, inner, collection: str, method: str, hook) -> None:
        self._inner = inner
        self._collection = collection
        self._method = method
        self._hook = hook

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name != self._collection:
            return attr
        return _HookedMethod(attr, self._method, self._hook)


def hooked_factory(collection: str, method: str, hook):
    """A `repos_factory` that instruments the TRANSACTION-bound repositories."""
    def factory(store):
        return _HookedRepos(FirestoreRepositories(store), collection, method,
                            hook)
    return factory


# ===========================================================================
# world
# ===========================================================================

@pytest.fixture()
def world(repos):
    """A practice, a caregiver with a child, a provisioned clinician.

    The tag is a UUID rather than the shared `unique_suffix`, which is derived
    from the test name and truncated. Three parametrisations of one test share
    their first characters, so they collided on one auth subject and the
    second provisioning call raised
    `ProviderProvisioningConflict` — the emulator database is deliberately not
    reset between tests, so uniqueness has to be real rather than probable.
    """
    import uuid

    from pilot_backend.domain.connections import CaregiverChildConnection
    from pilot_backend.domain.entities import Caregiver, Child, Practice
    from pilot_backend.domain.enums import CaregiverRelationship

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

    class Bundle:
        pass

    bundle = Bundle()
    bundle.repos = repos
    bundle.child = child.child_id
    bundle.provider = provider
    bundle.clock = clock
    bundle.recorder = recorder
    bundle.caregiver = resolve_principal(
        VerifiedToken(subject=f"fictional-cg-{tag}"), repos)
    bundle.provider_principal = resolve_principal(
        VerifiedToken(subject=provider.auth_subject), repos)

    def service(repos_factory=None):
        return ProviderConnectionService(
            repos=repos, recorder=recorder, now=clock,
            repos_factory=repos_factory)

    bundle.service = service
    return bundle


def active_connection(world):
    plain = world.service()
    pending = plain.invite_provider(
        world.caregiver, world.child, world.provider.provider_id)
    return plain.accept_invitation(
        world.provider_principal, pending.connection_id)


def assert_no_stale_assignment(world, connection_id: str) -> None:
    """THE invariant: no ACTIVE assignment without an ACTIVE connection."""
    connection = world.repos.provider_child.get_by_id(connection_id)
    active = world.repos.managing_clinicians.list_for_child(world.child)
    if connection.is_active:
        return
    assert active == [], (
        f"connection is {connection.status.value} but "
        f"{len(active)} managing assignment(s) are still ACTIVE")


# ===========================================================================
# RACE A — revoke must read assignments INSIDE its transaction
# ===========================================================================

def test_race_A_revoke_commits_after_a_competing_assignment(world):
    """Forced ordering: revoke finishes its reads, THEN assign commits.

    Exposes an implementation whose assignment lookup happens outside the
    transaction, because only a transactional read puts the assignment in the
    read set and makes the competing commit a conflict.
    """
    connection = active_connection(world)
    barrier = OneShotBarrier()
    # Fires immediately before the first WRITE of the revoke transaction, which
    # is the connection overwrite — after every read in both implementations.
    revoker = world.service(
        repos_factory=hooked_factory("provider_child", "overwrite", barrier))

    outcome = {}

    def revoke() -> None:
        try:
            outcome["result"] = revoker.revoke_connection(
                world.caregiver, connection.connection_id)
        except Exception as exc:  # noqa: BLE001 - reported below
            outcome["error"] = exc

    thread = threading.Thread(target=revoke)
    thread.start()
    barrier.await_reached()

    # Revoke has read everything and is poised to write. Attempt the competing
    # assignment now, through a service with no seam in it.
    #
    # It is EXPECTED to fail against the correct implementation, and that is
    # the discriminator rather than a problem. A Firestore transaction holds
    # read locks on what it read, so a revoke that queried the assignments
    # transactionally blocks this create until it commits — the SDK retries
    # five times and then raises. The mutated implementation holds no such
    # lock, so this call sails through and leaves the stale assignment behind.
    #
    # So the assertion is NOT on whether this succeeds. It is on the invariant.
    try:
        world.service().assign_managing_clinician(
            world.caregiver, world.child, world.provider.provider_id)
        assigned = True
    except Exception:  # noqa: BLE001 - blocked by the read lock, as designed
        assigned = False

    barrier.release()
    thread.join(timeout=_WAIT)
    assert not thread.is_alive(), "the revoke thread never finished"

    stored = world.repos.provider_child.get_by_id(connection.connection_id)
    assert stored.status is ConnectionStatus.REVOKED, outcome.get("error")
    assert_no_stale_assignment(world, connection.connection_id)
    assert authorize_child_access(
        world.provider_principal, world.child, world.repos).allowed is False
    # Record which way it went, so a future reader can see that the correct
    # implementation refuses the competing write rather than racing it.
    assert assigned is False, (
        "the competing assignment committed while a revoke held its read "
        "locks, which means the revoke never read the assignments inside its "
        "transaction")


# ===========================================================================
# RACE B — assign must re-read the connection INSIDE its transaction
# ===========================================================================

def test_race_B_assignment_commits_after_a_competing_revoke(world):
    """Forced ordering: assign finishes its reads, THEN revoke commits.

    Exposes an implementation that validates the connection only before the
    transaction, or that does not re-check it on retry.
    """
    connection = active_connection(world)
    barrier = OneShotBarrier()
    # The barrier goes OUTSIDE the transaction, not inside it.
    #
    # `assign_managing_clinician` validates the connection, then reads
    # `next_generation` for the managing-clinician key, and only THEN opens its
    # transaction. Hooking that last pre-transaction read parks the caller
    # after its advisory check with no locks held, so the competing revoke can
    # actually commit — which an in-transaction barrier would prevent, since
    # the blocked transaction's read locks would make the revoke exhaust its
    # retries instead.
    #
    # That is the ordering the mutation needs: the pre-transaction check saw
    # ACTIVE, the connection then became REVOKED, and only the re-read INSIDE
    # the transaction can still catch it.
    assigner = ProviderConnectionService(
        repos=_HookedRepos(world.repos, "identity_claims", "next_generation",
                           barrier),
        recorder=world.recorder, now=world.clock)

    outcome = {}

    def assign() -> None:
        try:
            outcome["result"] = assigner.assign_managing_clinician(
                world.caregiver, world.child, world.provider.provider_id)
        except Exception as exc:  # noqa: BLE001 - classified below
            outcome["error"] = exc

    thread = threading.Thread(target=assign)
    thread.start()
    barrier.await_reached()

    revoked = world.service().revoke_connection(
        world.caregiver, connection.connection_id)
    assert revoked.status is ConnectionStatus.REVOKED

    barrier.release()
    thread.join(timeout=_WAIT)
    assert not thread.is_alive(), "the assign thread never finished"

    # The assignment MUST have been refused: its connection is gone.
    assert "result" not in outcome, (
        "a managing clinician was assigned on a revoked connection: "
        f"{outcome.get('result')}")
    assert isinstance(outcome.get("error"), ConnectionStateConflict), \
        outcome.get("error")
    assert_no_stale_assignment(world, connection.connection_id)


# ===========================================================================
# the invariant, over every ordering this module can force
# ===========================================================================

@pytest.mark.parametrize("closing", ["revoke", "pause", "end"])
def test_no_ordering_leaves_an_active_assignment_without_a_connection(
        world, closing):
    """The stated final invariant, forced for each closing transition.

    Assign races a close; whichever lands second must not leave the forbidden
    combination of an inactive connection and a live assignment.
    """
    connection = active_connection(world)
    barrier = OneShotBarrier()
    closer = world.service(
        repos_factory=hooked_factory("provider_child", "overwrite", barrier))

    def close() -> None:
        try:
            if closing == "pause":
                closer.pause_connection(world.caregiver, connection.connection_id)
            else:
                closer.revoke_connection(
                    world.caregiver, connection.connection_id,
                    status=(ConnectionStatus.ENDED if closing == "end"
                            else ConnectionStatus.REVOKED))
        except Exception:  # noqa: BLE001 - the invariant is checked below
            pass

    thread = threading.Thread(target=close)
    thread.start()
    barrier.await_reached()
    try:
        world.service().assign_managing_clinician(
            world.caregiver, world.child, world.provider.provider_id)
    except Exception:  # noqa: BLE001 - refusal or lock contention, both fine
        pass
    barrier.release()
    thread.join(timeout=_WAIT)
    assert not thread.is_alive()

    assert_no_stale_assignment(world, connection.connection_id)


# ===========================================================================
# resume / reconnect do not resurrect an assignment
# ===========================================================================

def test_resuming_does_not_resurrect_the_assignment(world):
    connection = active_connection(world)
    service = world.service()
    service.assign_managing_clinician(
        world.caregiver, world.child, world.provider.provider_id)

    service.pause_connection(world.caregiver, connection.connection_id)
    assert world.repos.managing_clinicians.list_for_child(world.child) == []

    resumed = service.resume_connection(
        world.caregiver, connection.connection_id)
    assert resumed.status is ConnectionStatus.ACTIVE
    assert resumed.connection_id == connection.connection_id
    assert world.repos.managing_clinicians.list_for_child(world.child) == [], \
        "a resume resurrected the assignment the pause ended"
    # Access returns; ownership must be asked for again.
    assert authorize_child_access(
        world.provider_principal, world.child, world.repos).allowed is True


def test_reconnecting_does_not_resurrect_the_assignment(world):
    first = active_connection(world)
    service = world.service()
    service.assign_managing_clinician(
        world.caregiver, world.child, world.provider.provider_id)
    service.revoke_connection(world.caregiver, first.connection_id)

    second = active_connection(world)
    assert second.connection_id != first.connection_id
    assert world.repos.managing_clinicians.list_for_child(world.child) == [], \
        "a reconnection resurrected an assignment from the revoked connection"

    regained = service.assign_managing_clinician(
        world.caregiver, world.child, world.provider.provider_id)
    assert regained.provider_connection_id == second.connection_id


# ===========================================================================
# production carries no test seam
# ===========================================================================

def test_production_code_contains_no_sleep_or_test_mode():
    """The determinism lives in the TEST, not in the thing under test.

    A harness that needed production to pause would be proving something about
    a debug build. `repos_factory` is load-bearing in production — the service
    must construct repositories over a transaction-bound store — and the test
    merely supplies a different one.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "pilot_backend"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts or "fixtures" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        # Checked over the AST, not the raw text.
        #
        # A substring scan flagged `wsgi_app.py` for the word "pytest" in a
        # docstring about CI dependencies, which is prose and not a test-mode
        # branch. A guard that cannot tell code from a comment produces
        # exactly that kind of false positive and gets switched off.
        #
        # `threading` is NOT banned either: `FakeDocumentStore` needs a lock
        # to give `run_in_transaction` the all-or-nothing semantics the port
        # promises. A lock is mutual exclusion, not a timing dependency.
        for node in ast.walk(ast.parse(source)):
            # No waiting, in any form. A production path that slept or waited
            # would be timing-coupled — the thing this harness replaces.
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"sleep", "wait"}):
                offenders.append((path.name, node.func.attr))
            # No awareness of being under test: no importing pytest, and no
            # reading a test-mode environment variable.
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in {"pytest", "unittest"}:
                        offenders.append((path.name, alias.name))
            if isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] in {"pytest", "unittest"}:
                    offenders.append((path.name, node.module))
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in {"PYTEST_CURRENT_TEST", "TESTING",
                                  "PILOT_TEST_MODE"}:
                    offenders.append((path.name, node.value))
    assert not offenders, offenders
