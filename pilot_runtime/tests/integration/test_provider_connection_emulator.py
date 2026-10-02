"""0.5B — provider identity and connections against a REAL Firestore emulator.

`FakeDocumentStore` is a plain dict and is not thread-safe, so every CONTENTION
claim 0.5B makes is made here and only here. The unit suite pins the state
machine; this pins what happens when two writers act at the same instant.

What is proved:

**One subject, one Provider, under contention.** Eight threads provision the
same new subject simultaneously. Exactly one claim document and one Provider
exist afterwards, every thread returns the same `provider_id`, and the subject
still resolves to one principal.

**Caregiver and provider contend for one subject and the loser fails closed.**
Whichever arrives second is refused, and the subject never ends up resolving
to both — the unrecoverable state PRE-PHI blocker 4 existed to prevent.

**One live connection per (provider, child), under contention.** Eight threads
invite the same clinician to the same child. One PENDING row survives.

**The managing-clinician cascade is atomic on a real store.** A revoke that
must also end an assignment and release two claims either does all of it or
none of it, and eight threads racing revoke-versus-assign cannot leave an
inactive connection with a live assignment.

**Reconnecting does not restore managing status** — the stale-assignment
defect, verified against the store where the writes are real.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth.interface import VerifiedToken
from pilot_backend.auth.resolver import PrincipalResolutionError, resolve_principal
from pilot_backend.authz.policy import authorize_child_access
from pilot_backend.connections import ProviderConnectionService
from pilot_backend.connections.errors import (
    ConnectionStateConflict,
    DuplicateLiveConnection,
)
from pilot_backend.domain.enums import ConnectionStatus, ProviderDiscipline
from pilot_backend.domain.identity_claims import ClaimKind, key_digest
from pilot_backend.domain.roles import ActorRole
from pilot_backend.integration.errors import (
    AmbiguousSubjectState,
    SubjectAlreadyHeld,
)
from pilot_backend.integration.identity_service import IntegrationIdentityService
from pilot_backend.integration.parent_source import InMemoryParentSessionSource
from pilot_backend.provisioning import provision_provider_record

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
RACE_WIDTH = 8


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


def _race(work, width: int = RACE_WIDTH):
    """Run `work(index)` on `width` threads released together.

    A stalled harness must REPORT itself rather than return a short result
    list that reads as a uniqueness win — the 0.4A lesson. A thread that has
    not finished by its join deadline fails the test explicitly.
    """
    barrier = threading.Barrier(width)
    results, errors = [], []
    lock = threading.Lock()

    def runner(index: int) -> None:
        barrier.wait()
        try:
            value = work(index)
            with lock:
                results.append(value)
        except Exception as exc:  # noqa: BLE001 - classified by the caller
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(width)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "the harness stalled; not a result"
    return results, errors


@pytest.fixture()
def unique() -> str:
    """A per-test suffix. The emulator database is shared by design."""
    return uuid.uuid4().hex[:12]


@pytest.fixture()
def world(repos, unique):
    """A practice, a caregiver with a child, and the services under test."""
    from pilot_backend.domain.connections import CaregiverChildConnection
    from pilot_backend.domain.entities import Caregiver, Child, Practice
    from pilot_backend.domain.enums import CaregiverRelationship

    clock = _AdvancingClock(T0)
    recorder = AuditRecorder(repos.audit_events, environment="test")

    practice = repos.practices.create(Practice.create(f"Practice-{unique}", now=T0))
    caregiver = repos.caregivers.create(Caregiver.create(
        "Caregiver", auth_subject=f"fictional-cg-{unique}", now=T0))
    child = repos.children.create(
        Child.create(actor_id=caregiver.caregiver_id, now=T0))
    repos.caregiver_child.connect(CaregiverChildConnection.create(
        caregiver.caregiver_id, child.child_id, CaregiverRelationship.PARENT,
        actor_id=caregiver.caregiver_id, now=T0))

    class Bundle:
        pass

    bundle = Bundle()
    bundle.repos = repos
    bundle.practice = practice
    bundle.child = child.child_id
    bundle.unique = unique
    bundle.clock = clock
    bundle.service = ProviderConnectionService(
        repos=repos, recorder=recorder, now=clock)
    bundle.identity = IntegrationIdentityService(
        repos=repos, parent_source=InMemoryParentSessionSource(),
        recorder=recorder, now=clock)
    bundle.caregiver_principal = resolve_principal(
        VerifiedToken(subject=f"fictional-cg-{unique}"), repos)
    return bundle


def _provision(world, subject: str, name: str = "Provider-Hannah"):
    return provision_provider_record(
        world.repos, auth_subject=subject,
        practice_id=world.practice.practice_id,
        discipline=ProviderDiscipline.SLP, display_name=name,
        now=world.clock()).provider


def _connect(world, provider):
    """An ACTIVE connection, through the real two-step flow."""
    pending = world.service.invite_provider(
        world.caregiver_principal, world.child, provider.provider_id)
    provider_principal = resolve_principal(
        VerifiedToken(subject=provider.auth_subject), world.repos)
    active = world.service.accept_invitation(
        provider_principal, pending.connection_id)
    return active, provider_principal


# ===========================================================================
# 1. eight-way provider provisioning of ONE subject
# ===========================================================================

def test_eight_concurrent_provisions_produce_exactly_one_provider(world):
    subject = f"fictional-prov-race-{world.unique}"

    results, errors = _race(lambda i: _provision(
        world, subject, name=f"Provider-{i}").provider_id)

    assert errors == [], errors
    assert len(results) == RACE_WIDTH
    assert len(set(results)) == 1, f"more than one provider id: {set(results)}"

    stored = [p for p in world.repos.providers.list_by_practice(
        world.practice.practice_id) if p.auth_subject == subject]
    assert len(stored) == 1, f"{len(stored)} Provider documents for one subject"
    claim = world.repos.auth_subject_claims.find_for_subject(subject)
    assert claim is not None
    assert claim.holder_actor_id == results[0]
    assert claim.is_held_by_provider is True


def test_the_raced_subject_resolves_to_one_provider_principal(world):
    subject = f"fictional-prov-resolve-{world.unique}"
    _race(lambda i: _provision(world, subject, name=f"Provider-{i}").provider_id)

    principal = resolve_principal(VerifiedToken(subject=subject), world.repos)
    assert principal.role is ActorRole.PROVIDER


def test_a_provisioning_retry_after_the_race_adds_nothing(world):
    subject = f"fictional-prov-retry-{world.unique}"
    _race(lambda i: _provision(world, subject).provider_id)

    before = len([p for p in world.repos.providers.list_by_practice(
        world.practice.practice_id) if p.auth_subject == subject])
    outcome = provision_provider_record(
        world.repos, auth_subject=subject,
        practice_id=world.practice.practice_id,
        discipline=ProviderDiscipline.SLP, display_name="Provider-Hannah",
        now=world.clock())
    assert outcome.created is False
    after = len([p for p in world.repos.providers.list_by_practice(
        world.practice.practice_id) if p.auth_subject == subject])
    assert after == before == 1


# ===========================================================================
# 2. caregiver vs provider contention for ONE subject
# ===========================================================================

def test_caregiver_and_provider_cannot_both_hold_one_subject(world):
    """Whoever is second is refused, and the subject never resolves to both.

    This is the exact state PRE-PHI blocker 4 existed to prevent:
    `resolve_principal` finding a caregiver AND a provider record for one
    subject refuses that person forever, with no release path.
    """
    subject = f"fictional-contend-{world.unique}"

    def work(index: int) -> str:
        if index % 2 == 0:
            return f"caregiver:{world.identity.bootstrap_caregiver(subject).caregiver_id}"
        return f"provider:{_provision(world, subject).provider_id}"

    results, errors = _race(work)

    kinds = {value.split(":", 1)[0] for value in results}
    assert len(kinds) == 1, (
        f"both actor kinds were created for one subject: {kinds}")
    # Every loser failed closed with a subject-collision error, not a crash.
    for error in errors:
        assert isinstance(error, (SubjectAlreadyHeld, AmbiguousSubjectState)), error
    # And the subject still authenticates as exactly one actor.
    principal = resolve_principal(VerifiedToken(subject=subject), world.repos)
    assert principal.role in (ActorRole.CAREGIVER, ActorRole.PROVIDER)
    claims = [doc for _id, doc in world.repos.store.list_all(
        "pilot_auth_subject_claims")
        if doc.get("subject_fingerprint")]
    held = [c for c in claims if c["holder_actor_id"] == principal.application_id]
    assert len(held) == 1


def test_a_provider_cannot_take_a_subject_a_caregiver_already_holds(world):
    """Sequential form of the above, in the previously-unguarded direction."""
    subject = f"fictional-cg-first-{world.unique}"
    caregiver = world.identity.bootstrap_caregiver(subject)

    with pytest.raises(SubjectAlreadyHeld):
        _provision(world, subject)

    assert resolve_principal(
        VerifiedToken(subject=subject), world.repos
    ).application_id == caregiver.caregiver_id


def test_a_caregiver_cannot_take_a_subject_a_provider_already_holds(world):
    """The direction 0.5A already guarded. Still true with the claim present."""
    subject = f"fictional-prov-first-{world.unique}"
    provider = _provision(world, subject)

    with pytest.raises(SubjectAlreadyHeld):
        world.identity.bootstrap_caregiver(subject)

    assert resolve_principal(
        VerifiedToken(subject=subject), world.repos
    ).application_id == provider.provider_id


# ===========================================================================
# 3. one LIVE connection per (provider, child)
# ===========================================================================

def test_eight_concurrent_invites_create_one_pending_connection(world):
    provider = _provision(world, f"fictional-inv-race-{world.unique}")

    results, errors = _race(lambda i: world.service.invite_provider(
        world.caregiver_principal, world.child, provider.provider_id
    ).connection_id)

    assert len(results) == 1, f"{len(results)} invites succeeded"
    assert len(errors) == RACE_WIDTH - 1
    for error in errors:
        assert isinstance(error, DuplicateLiveConnection), error

    rows = [c for c in world.repos.provider_child.list_providers_for_child(
        world.child, include_ended=True)
        if c.provider_id == provider.provider_id]
    assert len(rows) == 1
    assert rows[0].status is ConnectionStatus.PENDING


def test_contested_accept_and_revoke_leave_one_coherent_state(world):
    """Accept and revoke racing on one connection cannot both take effect.

    Whatever the interleaving, the row ends in exactly one status, and the
    authorization answer agrees with it — there is no outcome where the
    connection reads ACTIVE while the caregiver believes it is revoked.
    """
    provider = _provision(world, f"fictional-contest-{world.unique}")
    pending = world.service.invite_provider(
        world.caregiver_principal, world.child, provider.provider_id)
    provider_principal = resolve_principal(
        VerifiedToken(subject=provider.auth_subject), world.repos)

    def work(index: int) -> str:
        if index % 2 == 0:
            return world.service.accept_invitation(
                provider_principal, pending.connection_id).status.value
        return world.service.revoke_connection(
            world.caregiver_principal, pending.connection_id).status.value

    _race(work)

    final = world.repos.provider_child.get_by_id(pending.connection_id)
    assert final.status in (ConnectionStatus.ACTIVE, ConnectionStatus.REVOKED)
    allowed = authorize_child_access(
        provider_principal, world.child, world.repos).allowed
    assert allowed is (final.status is ConnectionStatus.ACTIVE), (
        f"authorization disagrees with the stored status {final.status}")


# ===========================================================================
# 4. competing managing-clinician assignment
# ===========================================================================

def test_eight_concurrent_assignments_produce_one_managing_clinician(world):
    provider = _provision(world, f"fictional-mc-race-{world.unique}")
    _connect(world, provider)

    results, errors = _race(lambda i: world.service.assign_managing_clinician(
        world.caregiver_principal, world.child, provider.provider_id
    ).assignment_id)

    assert len(results) == 1, f"{len(results)} assignments succeeded"
    for error in errors:
        assert isinstance(error, ConnectionStateConflict), error
    active = world.repos.managing_clinicians.list_for_child(world.child)
    assert len(active) == 1
    assert active[0].assignment_id == results[0]


# ===========================================================================
# 5. the atomic cascade, on a real store
# ===========================================================================

def test_revoking_ends_the_assignment_in_one_transaction(world):
    provider = _provision(world, f"fictional-cascade-{world.unique}")
    connection, provider_principal = _connect(world, provider)
    assignment = world.service.assign_managing_clinician(
        world.caregiver_principal, world.child, provider.provider_id)

    world.service.revoke_connection(
        world.caregiver_principal, connection.connection_id)

    assert world.repos.provider_child.get_by_id(
        connection.connection_id).status is ConnectionStatus.REVOKED
    assert world.repos.managing_clinicians.list_for_child(world.child) == []
    stored = world.repos.managing_clinicians.get_by_id(assignment.assignment_id)
    assert stored.is_active is False
    # The managing-clinician key was handed back, so a new assignment is
    # possible once a new connection exists.
    digest = key_digest(world.child)
    assert world.repos.identity_claims.next_generation(
        ClaimKind.MANAGING_CLINICIAN, digest) >= 1


def test_pausing_ends_the_assignment_and_denies_access(world):
    provider = _provision(world, f"fictional-pause-{world.unique}")
    connection, provider_principal = _connect(world, provider)
    world.service.assign_managing_clinician(
        world.caregiver_principal, world.child, provider.provider_id)

    paused = world.service.pause_connection(
        world.caregiver_principal, connection.connection_id)

    assert paused.status is ConnectionStatus.PAUSED
    assert paused.ended_at is None
    assert world.repos.managing_clinicians.list_for_child(world.child) == []
    assert authorize_child_access(
        provider_principal, world.child, world.repos).allowed is False


def test_reconnecting_does_not_restore_managing_status_on_a_real_store(world):
    """The stale-assignment defect, against the store where writes are real."""
    provider = _provision(world, f"fictional-reconnect-{world.unique}")
    first, provider_principal = _connect(world, provider)
    world.service.assign_managing_clinician(
        world.caregiver_principal, world.child, provider.provider_id)
    world.service.revoke_connection(
        world.caregiver_principal, first.connection_id)

    second, _ = _connect(world, provider)

    assert second.status is ConnectionStatus.ACTIVE
    assert authorize_child_access(
        provider_principal, world.child, world.repos).allowed is True
    assert world.repos.managing_clinicians.list_for_child(world.child) == [], (
        "a revoked connection left a live managing assignment that a "
        "reconnection restored")

    regained = world.service.assign_managing_clinician(
        world.caregiver_principal, world.child, provider.provider_id)
    assert regained.provider_connection_id == second.connection_id


def test_revoke_racing_assign_never_leaves_a_stale_assignment(world):
    """The cascade under contention.

    The forbidden end state is an inactive connection with a live assignment.
    Whatever order the writers land in, that combination must not exist.
    """
    provider = _provision(world, f"fictional-race-cascade-{world.unique}")
    connection, provider_principal = _connect(world, provider)

    def work(index: int) -> str:
        if index % 2 == 0:
            return world.service.assign_managing_clinician(
                world.caregiver_principal, world.child, provider.provider_id
            ).assignment_id
        return world.service.revoke_connection(
            world.caregiver_principal, connection.connection_id).status.value

    _race(work)

    final = world.repos.provider_child.get_by_id(connection.connection_id)
    active = world.repos.managing_clinicians.list_for_child(world.child)
    if final.status is not ConnectionStatus.ACTIVE:
        assert active == [], (
            f"connection is {final.status} but {len(active)} assignment(s) "
            f"remain active")


# ===========================================================================
# 6. provider child-list isolation
# ===========================================================================

def test_connected_children_is_isolated_per_provider(world):
    mine = _provision(world, f"fictional-mine-{world.unique}", name="Mine")
    theirs = _provision(world, f"fictional-theirs-{world.unique}", name="Theirs")
    _connect(world, mine)
    their_principal = resolve_principal(
        VerifiedToken(subject=theirs.auth_subject), world.repos)
    my_principal = resolve_principal(
        VerifiedToken(subject=mine.auth_subject), world.repos)

    my_children = {c.child_id for c in world.service.connected_children(my_principal)}
    their_children = {c.child_id for c in
                      world.service.connected_children(their_principal)}

    assert world.child in my_children
    assert world.child not in their_children
    assert their_children == set()


def test_no_orphan_records_survive_any_race(world):
    """One sweep for the four orphan shapes 0.5B could produce."""
    subject = f"fictional-sweep-{world.unique}"
    _race(lambda i: _provision(world, subject, name=f"P{i}").provider_id)
    provider = world.repos.providers.get_by_auth_subject(subject)

    _race(lambda i: world.service.invite_provider(
        world.caregiver_principal, world.child, provider.provider_id
    ).connection_id)

    providers = [p for p in world.repos.providers.list_by_practice(
        world.practice.practice_id) if p.auth_subject == subject]
    connections = [c for c in world.repos.provider_child.list_providers_for_child(
        world.child, include_ended=True) if c.provider_id == provider.provider_id]
    subject_claims = [doc for _id, doc in world.repos.store.list_all(
        "pilot_auth_subject_claims")
        if doc.get("holder_actor_id") == provider.provider_id]

    assert len(providers) == 1, "orphan Provider"
    assert len(subject_claims) == 1, "orphan AuthSubjectIdentityClaim"
    assert len(connections) == 1, "duplicate connection"
    assert world.repos.managing_clinicians.list_for_child(world.child) == [], \
        "stale managing assignment"
