"""0.5B — caregiver-initiated provider connections and managing clinicians.

Concurrency is NOT proven here. `FakeDocumentStore` is a plain dict and is not
thread-safe, so every contention claim is made against the real Firestore
emulator in `pilot_runtime/tests/integration/test_provider_connection_emulator.py`.
What this file pins is the state machine, the authorization rules, and the
atomic cascade that fixes the stale-assignment defect.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth.interface import VerifiedToken
from pilot_backend.auth.resolver import resolve_principal
from pilot_backend.authz.policy import authorize_child_access
from pilot_backend.connections import ProviderConnectionService
from pilot_backend.connections.errors import (
    ConnectionNotFound,
    ConnectionStateConflict,
    DuplicateLiveConnection,
    ProviderNotConnectable,
)
from pilot_backend.domain.enums import (
    ConnectionInitiator,
    ConnectionStatus,
    EntityStatus,
    ProviderDiscipline,
)
from pilot_backend.domain.identity_claims import ClaimKind, key_digest
from pilot_backend.fixtures.secure_topology import (
    CAREGIVER_ALPHA_SUBJECT,
    CAREGIVER_BETA_SUBJECT,
    PROVIDER_BETA_SUBJECT,
    build_secure_topology,
)
from pilot_backend.persistence import FakeDocumentStore, FirestoreRepositories
from pilot_backend.provisioning import provision_provider_record

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

#: A clinician provisioned the way Tuesday's pilot account is: known out of
#: band, provisioned by an administrator, never self-registered.
HANNAH_SUBJECT = "fictional-subject-provider-hannah"


class _AdvancingClock:
    def __init__(self, start: datetime) -> None:
        self._start = start.replace(microsecond=0)
        self._tick = 0

    def __call__(self) -> datetime:
        self._tick += 1
        return self._start + timedelta(seconds=self._tick)


def principal_for(repos, subject):
    return resolve_principal(VerifiedToken(subject=subject), repos)


@pytest.fixture()
def wiring():
    """Topology, a provisioned Hannah, and the service under test."""
    repos = FirestoreRepositories(FakeDocumentStore())
    topo = build_secure_topology(repos, now=T0)
    recorder = AuditRecorder(repos.audit_events, environment="test")

    hannah = provision_provider_record(
        repos, auth_subject=HANNAH_SUBJECT,
        practice_id=topo.practice.practice_id,
        discipline=ProviderDiscipline.SLP, display_name="Provider-Hannah",
        now=T0).provider

    class Bundle:
        pass

    bundle = Bundle()
    bundle.repos = repos
    bundle.topo = topo
    bundle.hannah = hannah
    bundle.service = ProviderConnectionService(
        repos=repos, recorder=recorder, now=_AdvancingClock(T0))
    bundle.caregiver = principal_for(repos, CAREGIVER_ALPHA_SUBJECT)
    bundle.other_caregiver = principal_for(repos, CAREGIVER_BETA_SUBJECT)
    bundle.hannah_principal = principal_for(repos, HANNAH_SUBJECT)
    bundle.child = topo.child_alpha.child_id
    return bundle


def audit_actions(repos):
    return [doc["action"] for _id, doc
            in repos.store.list_all("pilot_audit_events")]


# ===========================================================================
# the caregiver invites a KNOWN provider
# ===========================================================================

def test_invite_creates_a_pending_caregiver_initiated_connection(wiring):
    connection = wiring.service.invite_provider(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)

    assert connection.status is ConnectionStatus.PENDING
    assert connection.initiated_by is ConnectionInitiator.CAREGIVER
    assert connection.provider_id == wiring.hannah.provider_id
    assert connection.child_id == wiring.child
    # Practice of record comes from the PROVIDER, never from the caller.
    assert connection.practice_id == wiring.hannah.practice_id
    assert connection.activated_at is None


def test_a_pending_connection_grants_no_clinical_access(wiring):
    """PENDING is the case a naive `if a connection exists` check passes."""
    wiring.service.invite_provider(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)
    decision = authorize_child_access(
        wiring.hannah_principal, wiring.child, wiring.repos)
    assert decision.allowed is False


def test_invite_refuses_an_unknown_provider_id(wiring):
    with pytest.raises(ProviderNotConnectable):
        wiring.service.invite_provider(
            wiring.caregiver, wiring.child, "prov_does_not_exist")


def test_an_unknown_and_a_retired_provider_are_indistinguishable(wiring):
    """Non-enumeration: the error must not confirm that an id exists.

    If absent and retired produced different errors, the invite endpoint would
    be an oracle for the `prov_` id space — which is exactly what having no
    provider directory is supposed to prevent.
    """
    retired = provision_provider_record(
        wiring.repos, auth_subject="fictional-subject-provider-retired",
        practice_id=wiring.topo.practice.practice_id,
        discipline=ProviderDiscipline.SLP, display_name="Provider-Retired",
        now=T0).provider
    wiring.repos.providers.update_status(
        retired.provider_id, EntityStatus.INACTIVE, now=T0)

    with pytest.raises(ProviderNotConnectable) as absent:
        wiring.service.invite_provider(
            wiring.caregiver, wiring.child, "prov_does_not_exist")
    with pytest.raises(ProviderNotConnectable) as inactive:
        wiring.service.invite_provider(
            wiring.caregiver, wiring.child, retired.provider_id)
    assert str(absent.value) == str(inactive.value)


def test_a_caregiver_cannot_invite_onto_someone_elses_child(wiring):
    """And the refusal is the non-enumerating one, not a distinct error."""
    with pytest.raises(ConnectionNotFound):
        wiring.service.invite_provider(
            wiring.other_caregiver, wiring.child, wiring.hannah.provider_id)
    # Nothing was written for the clinician the intruder named.
    assert [c for c in wiring.repos.provider_child.list_providers_for_child(
        wiring.child, include_ended=True)
        if c.provider_id == wiring.hannah.provider_id] == []


def test_a_provider_cannot_invite_themselves(wiring):
    """There is no provider-initiated path in 0.5B. Deferred, not forgotten."""
    with pytest.raises(ConnectionNotFound):
        wiring.service.invite_provider(
            wiring.hannah_principal, wiring.child, wiring.hannah.provider_id)


def test_a_second_live_invite_for_one_pair_is_refused(wiring):
    """The no-duplicate rule, as a write-time claim."""
    wiring.service.invite_provider(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)
    with pytest.raises(DuplicateLiveConnection):
        wiring.service.invite_provider(
            wiring.caregiver, wiring.child, wiring.hannah.provider_id)

    live = [c for c in wiring.repos.provider_child.list_providers_for_child(
        wiring.child, include_ended=True)
        if c.provider_id == wiring.hannah.provider_id]
    assert len(live) == 1


# ===========================================================================
# the provider responds
# ===========================================================================

def test_the_invited_provider_accepts_and_gains_access(wiring):
    connection = wiring.service.invite_provider(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)
    accepted = wiring.service.accept_invitation(
        wiring.hannah_principal, connection.connection_id)

    assert accepted.status is ConnectionStatus.ACTIVE
    assert accepted.activated_at is not None
    assert authorize_child_access(
        wiring.hannah_principal, wiring.child, wiring.repos).allowed is True


def test_another_provider_cannot_accept_someone_elses_invitation(wiring):
    connection = wiring.service.invite_provider(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)
    intruder = principal_for(wiring.repos, PROVIDER_BETA_SUBJECT)

    with pytest.raises(ConnectionNotFound):
        wiring.service.accept_invitation(intruder, connection.connection_id)
    assert wiring.repos.provider_child.get_by_id(
        connection.connection_id).status is ConnectionStatus.PENDING


def test_a_declined_invitation_grants_nothing_and_frees_the_pair(wiring):
    connection = wiring.service.invite_provider(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)
    declined = wiring.service.decline_invitation(
        wiring.hannah_principal, connection.connection_id)

    assert declined.status is ConnectionStatus.DECLINED
    assert authorize_child_access(
        wiring.hannah_principal, wiring.child, wiring.repos).allowed is False
    # The key was released, so the family may invite this clinician again.
    again = wiring.service.invite_provider(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)
    assert again.connection_id != connection.connection_id
    assert again.status is ConnectionStatus.PENDING


def test_only_a_pending_invitation_can_be_declined_at_the_domain_layer(wiring):
    """DECLINED means "refused before it started", and only that.

    Found by mutation testing. Removing the PENDING guard from `decline()`
    survived, because `decline_invitation` checks PENDING first and refused
    before reaching the domain. But the repository exposes `decline()`
    directly, and a trail showing DECLINED for a clinician who had been
    treating a child for a month would misrepresent what happened — an ACTIVE
    relationship being withdrawn is a REVOKE.
    """
    from pilot_backend.domain.connections import (
        ConnectionError_,
        ProviderChildConnection,
    )

    pending = ProviderChildConnection.create(
        wiring.hannah.provider_id, wiring.child, wiring.hannah.practice_id,
        now=T0)
    active = pending.activate(now=T0)
    paused = active.pause(now=T0)
    revoked = active.end(status=ConnectionStatus.REVOKED, now=T0)

    for state in (active, paused, revoked, pending.decline(now=T0)):
        with pytest.raises(ConnectionError_):
            state.decline(now=T0)
    # The one legal case still works.
    assert pending.decline(now=T0).status is ConnectionStatus.DECLINED


def test_a_connection_that_was_never_active_cannot_be_resumed(wiring):
    """`is_resumable` requires a prior activation, not merely PAUSED.

    Found by mutation testing. Dropping the `activated_at is not None` conjunct
    survived because the service cannot reach the state: `pause()` requires
    `is_active`, so a paused row always carries `activated_at`.

    It is still the right guard. Resuming is defined as returning to a state
    the relationship was previously IN, and a row that never activated has no
    such state — "resume" would be silently promoting a never-accepted
    invitation to active, skipping the clinician's consent entirely. Asserted
    on a directly-constructed row, which is the only way the combination
    exists.
    """
    from pilot_backend.domain.connections import (
        ConnectionError_,
        ProviderChildConnection,
    )

    never_active = ProviderChildConnection(
        connection_id="pcxn_never", provider_id=wiring.hannah.provider_id,
        child_id=wiring.child, practice_id=wiring.hannah.practice_id,
        status=ConnectionStatus.PAUSED, created_at=T0, updated_at=T0,
        activated_at=None, paused_at=T0)

    assert never_active.is_paused is True
    assert never_active.is_resumable is False, (
        "a connection that was never active reports itself resumable")
    with pytest.raises(ConnectionError_):
        never_active.resume(now=T0)

    # A genuinely paused row — one that WAS active — resumes.
    once_active = ProviderChildConnection.create(
        wiring.hannah.provider_id, wiring.child, wiring.hannah.practice_id,
        now=T0).activate(now=T0).pause(now=T0)
    assert once_active.is_resumable is True
    assert once_active.resume(now=T0).status is ConnectionStatus.ACTIVE


def test_a_declined_connection_cannot_be_reactivated_at_the_domain_layer(wiring):
    """DECLINED is terminal, asserted on the domain object itself.

    Found by mutation testing. Removing DECLINED from
    `TERMINAL_CONNECTION_STATUSES` survived every service test, because
    `accept_invitation` independently requires status PENDING and refused
    first. The terminal-set membership was therefore redundant *through the
    service* — but it is the only guard on `activate()`, which the repository
    exposes directly and which fixtures already call.

    So this asserts at the layer the invariant actually lives on: a refused
    invitation cannot be turned back into an active relationship by any route,
    and re-inviting must mint a new row instead.
    """
    from pilot_backend.domain.connections import (
        ConnectionError_,
        ProviderChildConnection,
    )
    from pilot_backend.domain.enums import TERMINAL_CONNECTION_STATUSES

    assert ConnectionStatus.DECLINED in TERMINAL_CONNECTION_STATUSES
    declined = ProviderChildConnection.create(
        wiring.hannah.provider_id, wiring.child, wiring.hannah.practice_id,
        now=T0).decline(now=T0)

    with pytest.raises(ConnectionError_):
        declined.activate(now=T0)
    # And through the repository, which is the route the service does not take.
    stored = wiring.repos.provider_child.connect(declined)
    with pytest.raises(ConnectionError_):
        wiring.repos.provider_child.activate(stored.connection_id, now=T0)


def test_a_declined_invitation_cannot_be_accepted_afterwards(wiring):
    connection = wiring.service.invite_provider(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)
    wiring.service.decline_invitation(
        wiring.hannah_principal, connection.connection_id)
    with pytest.raises(ConnectionStateConflict):
        wiring.service.accept_invitation(
            wiring.hannah_principal, connection.connection_id)


# ===========================================================================
# acceptance is NOT managing-clinician assignment
# ===========================================================================

def _connect(wiring):
    connection = wiring.service.invite_provider(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)
    return wiring.service.accept_invitation(
        wiring.hannah_principal, connection.connection_id)


def test_accepting_a_connection_does_not_assign_a_managing_clinician(wiring):
    """The two consents are separate, and must stay separate."""
    _connect(wiring)
    assert wiring.service.current_managing_clinician(
        wiring.caregiver, wiring.child) is None
    assert wiring.repos.managing_clinicians.list_for_child(wiring.child) == []


def test_a_caregiver_assigns_an_actively_connected_provider(wiring):
    _connect(wiring)
    assignment = wiring.service.assign_managing_clinician(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)

    assert assignment.provider_id == wiring.hannah.provider_id
    assert assignment.is_active is True
    assert assignment.practice_id == wiring.hannah.practice_id
    current = wiring.service.current_managing_clinician(
        wiring.caregiver, wiring.child)
    assert current.assignment_id == assignment.assignment_id


def test_assignment_requires_an_active_connection(wiring):
    """Pending is not enough — the clinician has not agreed yet."""
    wiring.service.invite_provider(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)
    with pytest.raises(ConnectionStateConflict):
        wiring.service.assign_managing_clinician(
            wiring.caregiver, wiring.child, wiring.hannah.provider_id)


def test_only_one_active_managing_clinician(wiring):
    _connect(wiring)
    wiring.service.assign_managing_clinician(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)
    with pytest.raises(ConnectionStateConflict):
        wiring.service.assign_managing_clinician(
            wiring.caregiver, wiring.child,
            wiring.topo.provider_alpha.provider_id)


def test_the_managing_clinician_claim_key_matches_the_frozen_derivation(wiring):
    """Both assignment paths must contend on ONE document.

    The frozen 0.4A service requires a PROVIDER principal and so cannot serve
    the caregiver flow, which is why 0.5B acquires the claim itself. That is
    only safe if the KEY is derived identically — otherwise the two paths would
    take different mutexes and "exactly one active managing clinician" would
    hold within each path and fail across them.
    """
    _connect(wiring)
    assignment = wiring.service.assign_managing_clinician(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)
    claim = wiring.repos.identity_claims.get_by_id(assignment.claim_id)
    assert claim.kind is ClaimKind.MANAGING_CLINICIAN
    assert claim.key_digest == key_digest(wiring.child)


# ===========================================================================
# THE DEFECT: an inactive connection must not leave a live assignment
# ===========================================================================

def test_pausing_a_connection_ends_the_managing_assignment(wiring):
    connection = _connect(wiring)
    wiring.service.assign_managing_clinician(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)

    paused = wiring.service.pause_connection(
        wiring.caregiver, connection.connection_id)

    assert paused.status is ConnectionStatus.PAUSED
    assert paused.ended_at is None, "pause must not be terminal"
    assert paused.activated_at is not None, "pause must preserve activation"
    assert wiring.repos.managing_clinicians.list_for_child(wiring.child) == []
    assert authorize_child_access(
        wiring.hannah_principal, wiring.child, wiring.repos).allowed is False


def test_resuming_restores_access_but_not_managing_status(wiring):
    """The pilot-critical half of the fix.

    Access comes back because it is the same relationship. Clinical OWNERSHIP
    does not, because "you still own this child" is not a thing to restore
    silently after an interruption of unknown length.
    """
    connection = _connect(wiring)
    wiring.service.assign_managing_clinician(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)
    wiring.service.pause_connection(wiring.caregiver, connection.connection_id)

    resumed = wiring.service.resume_connection(
        wiring.caregiver, connection.connection_id)

    assert resumed.status is ConnectionStatus.ACTIVE
    assert resumed.connection_id == connection.connection_id, "same row"
    assert authorize_child_access(
        wiring.hannah_principal, wiring.child, wiring.repos).allowed is True
    assert wiring.service.current_managing_clinician(
        wiring.caregiver, wiring.child) is None


def test_revoking_a_connection_ends_the_managing_assignment(wiring):
    connection = _connect(wiring)
    wiring.service.assign_managing_clinician(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)

    revoked = wiring.service.revoke_connection(
        wiring.caregiver, connection.connection_id)

    assert revoked.status is ConnectionStatus.REVOKED
    assert revoked.ended_at is not None
    assert wiring.repos.managing_clinicians.list_for_child(wiring.child) == []
    assert authorize_child_access(
        wiring.hannah_principal, wiring.child, wiring.repos).allowed is False


def test_reconnecting_after_a_revoke_does_not_restore_managing_status(wiring):
    """THE regression test for the stale-assignment defect.

    Before the atomic cascade, revoking left the assignment ACTIVE. It granted
    nothing while the connection was inactive — authorization runs first — but
    a later reconnection of the SAME provider made `authorize_child_access`
    pass again while the stale assignment still named them, restoring
    managing-clinician status with no explicit assignment and a
    `provider_connection_id` pointing at a revoked row.
    """
    first = _connect(wiring)
    wiring.service.assign_managing_clinician(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)
    wiring.service.revoke_connection(wiring.caregiver, first.connection_id)

    # The family changes its mind and reconnects the same clinician.
    second = wiring.service.invite_provider(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)
    reconnected = wiring.service.accept_invitation(
        wiring.hannah_principal, second.connection_id)

    assert reconnected.status is ConnectionStatus.ACTIVE
    assert authorize_child_access(
        wiring.hannah_principal, wiring.child, wiring.repos).allowed is True
    # Access is back; clinical OWNERSHIP is not, and must be asked for again.
    assert wiring.service.current_managing_clinician(
        wiring.caregiver, wiring.child) is None
    assert wiring.repos.managing_clinicians.list_for_child(wiring.child) == []

    regained = wiring.service.assign_managing_clinician(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)
    assert regained.provider_connection_id == second.connection_id, \
        "the new assignment must cite the NEW connection"


def test_the_cascade_is_audited_distinguishably(wiring):
    """An assignment ended by a connection change says so."""
    connection = _connect(wiring)
    wiring.service.assign_managing_clinician(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)
    wiring.service.revoke_connection(wiring.caregiver, connection.connection_id)

    cascade = [doc for _id, doc in wiring.repos.store.list_all("pilot_audit_events")
               if doc.get("metadata", {}).get("integration_state")
               == "ENDED_BY_CONNECTION_CHANGE"]
    assert len(cascade) == 1


def test_a_revoke_releases_the_pair_so_reinvite_works(wiring):
    connection = _connect(wiring)
    wiring.service.revoke_connection(wiring.caregiver, connection.connection_id)
    again = wiring.service.invite_provider(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)
    assert again.status is ConnectionStatus.PENDING


def test_pausing_keeps_the_pair_held(wiring):
    """A paused relationship still occupies the slot."""
    connection = _connect(wiring)
    wiring.service.pause_connection(wiring.caregiver, connection.connection_id)
    with pytest.raises(DuplicateLiveConnection):
        wiring.service.invite_provider(
            wiring.caregiver, wiring.child, wiring.hannah.provider_id)


# ===========================================================================
# every inactive state denies, as one table
# ===========================================================================

@pytest.mark.parametrize("closer", ["decline", "pause", "revoke", "end"])
def test_no_inactive_state_grants_clinical_access(wiring, closer):
    connection = wiring.service.invite_provider(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)
    if closer == "decline":
        wiring.service.decline_invitation(
            wiring.hannah_principal, connection.connection_id)
    else:
        wiring.service.accept_invitation(
            wiring.hannah_principal, connection.connection_id)
        if closer == "pause":
            wiring.service.pause_connection(
                wiring.caregiver, connection.connection_id)
        elif closer == "revoke":
            wiring.service.revoke_connection(
                wiring.caregiver, connection.connection_id)
        else:
            wiring.service.revoke_connection(
                wiring.caregiver, connection.connection_id,
                status=ConnectionStatus.ENDED)

    assert authorize_child_access(
        wiring.hannah_principal, wiring.child, wiring.repos).allowed is False


# ===========================================================================
# connected children
# ===========================================================================

def test_connected_children_lists_only_active_connections(wiring):
    connection = _connect(wiring)
    found = wiring.service.connected_children(wiring.hannah_principal)
    assert [c.child_id for c in found] == [wiring.child]
    assert found[0].connection_id == connection.connection_id
    assert found[0].practice_id == wiring.hannah.practice_id
    assert found[0].is_managing_clinician is False


def test_connected_children_marks_the_managing_clinician(wiring):
    _connect(wiring)
    wiring.service.assign_managing_clinician(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)
    found = wiring.service.connected_children(wiring.hannah_principal)
    assert found[0].is_managing_clinician is True


@pytest.mark.parametrize("state", ["pending", "declined", "paused", "revoked"])
def test_connected_children_hides_every_non_active_state(wiring, state):
    connection = wiring.service.invite_provider(
        wiring.caregiver, wiring.child, wiring.hannah.provider_id)
    if state == "declined":
        wiring.service.decline_invitation(
            wiring.hannah_principal, connection.connection_id)
    elif state != "pending":
        wiring.service.accept_invitation(
            wiring.hannah_principal, connection.connection_id)
        if state == "paused":
            wiring.service.pause_connection(
                wiring.caregiver, connection.connection_id)
        else:
            wiring.service.revoke_connection(
                wiring.caregiver, connection.connection_id)

    assert wiring.service.connected_children(wiring.hannah_principal) == []


def test_connected_children_does_not_leak_across_providers(wiring):
    _connect(wiring)
    other = principal_for(wiring.repos, PROVIDER_BETA_SUBJECT)
    mine = {c.child_id for c in wiring.service.connected_children(
        wiring.hannah_principal)}
    theirs = {c.child_id for c in wiring.service.connected_children(other)}
    assert wiring.child in mine
    assert wiring.child not in theirs


def test_connected_children_refuses_a_caregiver(wiring):
    with pytest.raises(ConnectionNotFound):
        wiring.service.connected_children(wiring.caregiver)


def test_connected_children_takes_no_provider_id_parameter():
    """Structural: there is no shape in which it lists someone else's caseload."""
    signature = inspect.signature(ProviderConnectionService.connected_children)
    assert list(signature.parameters) == ["self", "principal"]


def test_the_connected_child_payload_carries_no_clinical_field(wiring):
    _connect(wiring)
    payload = wiring.service.connected_children(
        wiring.hannah_principal)[0].as_payload()
    for banned in ("name", "child_name", "age", "age_months", "birth_date",
                   "diagnosis", "concern", "notes", "note", "plan", "goals",
                   "answers", "auth_subject", "owner_uid", "display_name"):
        assert banned not in payload, banned
    assert set(payload) == {"child_id", "connection_id", "practice_id",
                            "connected_since", "is_managing_clinician"}


# ===========================================================================
# no operation accepts an actor id from the caller
# ===========================================================================

def test_no_lifecycle_method_takes_a_caller_identity_field():
    """Identity is server-derived from the principal, everywhere.

    `invite_provider` legitimately takes a `provider_id` — that is the thing
    being NAMED, and it grants nothing until that provider accepts. What must
    never appear is the CALLER's own identity, practice, role or account
    status as a parameter, because those are the fields a client would forge.

    `status` is deliberately NOT in this set. `revoke_connection(status=...)`
    chooses between REVOKED and ENDED, which is a property of the
    relationship being closed rather than a claim about who the caller is, and
    the method validates it to those two values — pinned separately below.
    """
    banned = {"caregiver_id", "actor_id", "practice_id", "role",
              "auth_subject", "provider_practice_id", "actor_role",
              "application_id", "entity_status"}
    for name, method in inspect.getmembers(
            ProviderConnectionService, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        parameters = set(inspect.signature(method).parameters)
        assert not (parameters & banned), (name, parameters & banned)


@pytest.mark.parametrize("status", [
    ConnectionStatus.PENDING,
    ConnectionStatus.ACTIVE,
    ConnectionStatus.PAUSED,
    ConnectionStatus.DECLINED,
])
def test_revoke_accepts_only_the_two_terminal_caregiver_statuses(wiring, status):
    """A caregiver closes a relationship as REVOKED or ENDED, nothing else.

    DECLINED especially: a family withdrawing access must not be recordable as
    the clinician having refused the work, and the two are not interchangeable
    even though both end up terminal.
    """
    connection = _connect(wiring)
    with pytest.raises(ConnectionStateConflict):
        wiring.service.revoke_connection(
            wiring.caregiver, connection.connection_id, status=status)
    assert wiring.repos.provider_child.get_by_id(
        connection.connection_id).status is ConnectionStatus.ACTIVE
