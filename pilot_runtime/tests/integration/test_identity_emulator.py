"""0.4A longitudinal identity against a REAL Firestore emulator.

Two things are proven here that a fake cannot prove.

**The adapter matches.** The same repository code and the same service run
against the real client, so "in-memory behaviour" and "Firestore behaviour"
are one implementation over two stores rather than two implementations that
could drift.

**The uniqueness claim actually wins races.** `FakeDocumentStore` is a plain
dict and is not thread-safe, so a concurrency test against it would prove
nothing about Firestore. Here, genuinely concurrent threads issue competing
`create` calls to a real server, and exactly one survives — which is the only
evidence that the write-time uniqueness strategy holds under contention.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from pilot_backend.audit.events import AuditAction, AuditResult
from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth import VerifiedToken, resolve_principal
from pilot_backend.domain.connections import ProviderChildConnection
from pilot_backend.domain.identity_claims import ClaimKind, IdentityClaim, key_digest
from pilot_backend.domain.managing_clinician import ManagingClinicianStatus
from pilot_backend.domain.source_link import SourceLinkStatus, SourceSystem
from pilot_backend.identity import (
    IdentityAuthorizationError,
    IdentityConflict,
    IdentityValidationError,
    LongitudinalIdentityService,
)
from pilot_backend.persistence import FirestoreRepositories, encode
from pilot_backend.repository.interface import DuplicateRecord

from ..test_sentinels import ALL_SENTINELS

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


class _AdvancingClock:
    """Deterministic but strictly increasing, and thread-safe.

    A frozen clock gives two successive writes the same timestamp, so history
    ordering falls back to the id tiebreak — a situation real wall-clock time
    never produces. The lock matters here specifically: these tests run the
    service from several threads at once.
    """

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
def identity(repos, topology, unique_suffix):
    """Service + principals over the real emulator-backed repositories."""
    from pilot_backend.fixtures.secure_topology import (
        CAREGIVER_ALPHA_SUBJECT,
        CAREGIVER_BETA_SUBJECT,
        PROVIDER_ALPHA_SUBJECT,
        PROVIDER_BETA_SUBJECT,
    )

    recorder = AuditRecorder(repos.audit_events, environment="test")
    service = LongitudinalIdentityService(repos=repos, recorder=recorder,
                                          now=_AdvancingClock(T0))

    def principal(base):
        return resolve_principal(VerifiedToken(subject=base + unique_suffix), repos)

    class Bundle:
        pass

    bundle = Bundle()
    bundle.repos = repos
    bundle.topo = topology
    bundle.svc = service
    bundle.suffix = unique_suffix
    bundle.caregiver_alpha = principal(CAREGIVER_ALPHA_SUBJECT)
    bundle.caregiver_beta = principal(CAREGIVER_BETA_SUBJECT)
    bundle.provider_alpha = principal(PROVIDER_ALPHA_SUBJECT)
    bundle.provider_beta = principal(PROVIDER_BETA_SUBJECT)
    return bundle


def ext(identity, name: str) -> str:
    """A fictional external id unique to this test (shared emulator DB)."""
    return f"{name}{identity.suffix}"


# ===========================================================================
# round-trips through the real adapter
# ===========================================================================

def test_source_link_round_trips_in_real_firestore(identity):
    link = identity.svc.link_source_system(
        identity.caregiver_alpha, identity.topo.child_alpha.child_id,
        SourceSystem.PARENT, ext(identity, "sess-fictional"))
    stored = identity.repos.source_links.get_by_id(link.link_id)
    assert stored == link
    assert stored.linked_at.tzinfo is not None
    assert stored.child_id.startswith("chld_")


def test_managing_clinician_round_trips_in_real_firestore(identity):
    assignment = identity.svc.assign_managing_clinician(
        identity.provider_alpha, identity.topo.child_alpha.child_id,
        identity.topo.provider_alpha.provider_id, reason="pilot")
    stored = identity.repos.managing_clinicians.get_by_id(assignment.assignment_id)
    assert stored == assignment
    assert stored.practice_id == identity.topo.link_alpha_provider.practice_id
    assert stored.effective_from.tzinfo is not None


def test_claims_round_trip_and_are_create_only(identity):
    link = identity.svc.link_source_system(
        identity.caregiver_alpha, identity.topo.child_alpha.child_id,
        SourceSystem.PARENT, ext(identity, "sess-claim"))
    claim = identity.repos.identity_claims.get_by_id(link.child_source_claim_id)
    assert claim.kind is ClaimKind.CHILD_SOURCE
    assert claim.generation == 0
    with pytest.raises(DuplicateRecord):
        identity.repos.identity_claims.claim(claim)


# ===========================================================================
# REAL CONCURRENCY — the reason this suite exists
# ===========================================================================

def _race(target, count: int = 8):
    """Run `target(i)` on `count` threads released together.

    Every thread must be accounted for. A thread that outlives its join used
    to return a SHORT result list, so "one winner, nine losers" quietly became
    "one winner, eight losers" and the assertion read as a uniqueness failure.
    It was a harness deadlock — the emulator had blocked writing to an
    undrained pipe (see `conftest.py`) — and it wasted a diagnosis pointing at
    the claim mechanism. A stalled harness now says so.
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


def test_concurrent_parent_links_for_one_child_yield_exactly_one(identity):
    """Eight threads, one child, eight different external ids."""
    child = identity.topo.child_alpha.child_id

    def attempt(index: int):
        return identity.svc.link_source_system(
            identity.caregiver_alpha, child, SourceSystem.PARENT,
            ext(identity, f"sess-race-{index}"))

    results, errors = _race(attempt)
    assert len(results) == 1, f"expected one winner, got {len(results)}"
    assert len(errors) == 7
    assert all(isinstance(e, (IdentityConflict,)) for e in errors), \
        {type(e).__name__ for e in errors}
    assert len(identity.repos.source_links.list_for_child(child)) == 1


def test_concurrent_claims_on_one_external_id_yield_exactly_one(identity):
    """The cross-child constraint under real contention.

    Both children are legitimately authorized for their own caregiver, so
    any refusal here is the uniqueness claim, not authorization.
    """
    shared = ext(identity, "sess-shared-race")
    targets = [
        (identity.caregiver_alpha, identity.topo.child_alpha.child_id),
        (identity.caregiver_beta, identity.topo.child_beta.child_id),
    ]

    def attempt(index: int):
        principal, child = targets[index % 2]
        return identity.svc.link_source_system(
            principal, child, SourceSystem.PARENT, shared)

    results, errors = _race(attempt, count=6)
    # Same (child, external) pairs are idempotent, so up to two distinct
    # children could each return a link ONLY if uniqueness failed.
    children = {r.child_id for r in results}
    assert len(children) == 1, f"external id mapped to {len(children)} children"
    active = identity.repos.source_links.list_for_external_id(shared)
    assert len([a for a in active if a.is_active]) == 1
    assert identity.svc._resolve_child_for_external(SourceSystem.PARENT, shared)


def test_concurrent_managing_clinician_assignments_yield_exactly_one(identity):
    child = identity.topo.child_alpha.child_id

    def attempt(index: int):
        return identity.svc.assign_managing_clinician(
            identity.provider_alpha, child,
            identity.topo.provider_alpha.provider_id, reason=f"race-{index}")

    results, errors = _race(attempt)
    assert len(results) == 1, f"expected one winner, got {len(results)}"
    assert len(identity.repos.managing_clinicians.list_for_child(child)) == 1
    assert all(isinstance(e, IdentityConflict) for e in errors)


def test_concurrent_raw_claims_collide_on_one_document(identity):
    """The primitive itself: `create` is an atomic compare-and-set."""
    child = identity.topo.child_alpha.child_id
    parts = (child, "race-primitive" + identity.suffix)

    def attempt(index: int):
        claim = IdentityClaim.build(
            ClaimKind.CHILD_SOURCE, parts, 0,
            holder_ref=f"holder-{index}", child_id=child, now=T0)
        return identity.repos.identity_claims.claim(claim)

    results, errors = _race(attempt, count=10)
    assert len(results) == 1
    assert len(errors) == 9
    assert all(isinstance(e, DuplicateRecord) for e in errors)
    assert identity.repos.identity_claims.count_claims_for_key(key_digest(*parts)) == 1


def test_reassignment_after_a_clean_end_succeeds_under_the_next_generation(identity):
    child = identity.topo.child_alpha.child_id
    first = identity.svc.assign_managing_clinician(
        identity.provider_alpha, child, identity.topo.provider_alpha.provider_id)
    identity.svc.end_managing_clinician(identity.provider_alpha, child,
                                        reason="caseload change")
    second = identity.svc.assign_managing_clinician(
        identity.provider_alpha, child, identity.topo.provider_alpha.provider_id)

    assert second.assignment_id != first.assignment_id
    digest = key_digest(child)
    assert identity.repos.identity_claims.count_claims_for_key(digest) == 2
    history = identity.svc.managing_clinician_history(identity.provider_alpha, child)
    assert [h.status for h in history] == [ManagingClinicianStatus.ENDED,
                                           ManagingClinicianStatus.ACTIVE]


def test_a_crash_before_the_record_write_persists_nothing_in_firestore(identity):
    """Crash consistency against a REAL transaction, not a rollback emulation.

    This is the founder-review window: a process death after the uniqueness
    claims but before the link write. Firestore commits the whole transaction
    or none of it, so the external identity cannot be stranded.
    """
    child = identity.topo.child_alpha.child_id
    external = ext(identity, "sess-crash")
    digest = key_digest(SourceSystem.PARENT.value, external)
    real_factory = identity.svc._repos_factory

    class _CrashAfterClaims:
        def __init__(self, inner):
            self._inner = inner
            self.identity_claims = inner.identity_claims

        @property
        def source_links(self):
            raise RuntimeError("simulated process failure before the record write")

        def __getattr__(self, name):
            return getattr(self._inner, name)

    identity.svc._repos_factory = lambda store: _CrashAfterClaims(real_factory(store))
    try:
        with pytest.raises(RuntimeError):
            identity.svc.link_source_system(
                identity.caregiver_alpha, child, SourceSystem.PARENT, external)
    finally:
        identity.svc._repos_factory = real_factory

    assert identity.repos.identity_claims.count_claims_for_key(digest) == 0, \
        "a claim survived a crash in real Firestore"
    assert identity.repos.source_links.list_for_child(
        child, include_ended=True) == []

    # The key is untouched, so the mapping can still be made.
    link = identity.svc.link_source_system(
        identity.caregiver_alpha, child, SourceSystem.PARENT, external)
    assert link.is_active
    assert identity.repos.identity_claims.count_claims_for_key(digest) == 1


def test_transactions_still_refuse_a_duplicate_claim(identity):
    """Atomicity must not have loosened uniqueness."""
    child = identity.topo.child_alpha.child_id
    identity.svc.link_source_system(identity.caregiver_alpha, child,
                                    SourceSystem.PARENT, ext(identity, "sess-dup"))
    with pytest.raises(IdentityConflict):
        identity.svc.link_source_system(
            identity.caregiver_alpha, child, SourceSystem.PARENT,
            ext(identity, "sess-dup-2"))


def test_set_is_refused_inside_a_transaction(store):
    """A weakened `set` would silently lose the existence guarantee."""
    from pilot_backend.persistence.document_store import DocumentStoreError

    def attempt(tx_store):
        tx_store.set("pilot_practices", "prac_fictional", {"a": "1"})

    with pytest.raises(DocumentStoreError) as exc:
        store.run_in_transaction(attempt)
    assert "not available inside a transaction" in str(exc.value)


def test_nested_transactions_are_refused(store):
    from pilot_backend.persistence.document_store import DocumentStoreError

    def outer(tx_store):
        return tx_store.run_in_transaction(lambda inner: None)

    with pytest.raises(DocumentStoreError):
        store.run_in_transaction(outer)


def test_resolve_authorized_child_gates_against_real_firestore(identity):
    child = identity.topo.child_alpha.child_id
    external = ext(identity, "sess-authz")
    identity.svc.link_source_system(identity.caregiver_alpha, child,
                                    SourceSystem.PARENT, external)

    assert identity.svc.resolve_authorized_child(
        identity.caregiver_alpha, SourceSystem.PARENT, external) == child
    with pytest.raises(IdentityAuthorizationError):
        identity.svc.resolve_authorized_child(
            identity.caregiver_beta, SourceSystem.PARENT, external)
    with pytest.raises(IdentityAuthorizationError):
        identity.svc.resolve_authorized_child(
            identity.provider_beta, SourceSystem.PARENT, external)


# ===========================================================================
# lifecycle and history in real storage
# ===========================================================================

def test_ended_links_survive_in_firestore(identity):
    child = identity.topo.child_alpha.child_id
    link = identity.svc.link_source_system(
        identity.caregiver_alpha, child, SourceSystem.PARENT,
        ext(identity, "sess-history"))
    identity.svc.end_source_link(identity.caregiver_alpha, link.link_id,
                                 reason="fictional migration")

    assert identity.repos.source_links.list_for_child(child) == []
    history = identity.repos.source_links.list_for_child(child, include_ended=True)
    assert [h.link_id for h in history] == [link.link_id]
    assert history[0].status is SourceLinkStatus.ENDED
    assert history[0].ended_at is not None


def test_replacement_lineage_persists(identity):
    child = identity.topo.child_alpha.child_id
    first = identity.svc.link_source_system(
        identity.caregiver_alpha, child, SourceSystem.PARENT,
        ext(identity, "sess-old"))
    successor = identity.svc.replace_source_link(
        identity.caregiver_alpha, first.link_id, ext(identity, "sess-new"),
        reason="fictional migration")

    predecessor = identity.repos.source_links.get_by_id(first.link_id)
    assert predecessor.status is SourceLinkStatus.SUPERSEDED
    assert predecessor.superseded_by_link_id == successor.link_id
    assert identity.repos.source_links.get_by_id(
        successor.link_id).supersedes_link_id == first.link_id


def test_transfer_lineage_persists(identity):
    child = identity.topo.child_alpha.child_id
    link = identity.repos.provider_child.connect(ProviderChildConnection.create(
        identity.topo.provider_beta.provider_id, child,
        identity.topo.practice.practice_id, now=T0))
    identity.repos.provider_child.activate(link.connection_id, now=T0)

    first = identity.svc.assign_managing_clinician(
        identity.provider_alpha, child, identity.topo.provider_alpha.provider_id)
    successor = identity.svc.transfer_managing_clinician(
        identity.provider_alpha, child, identity.topo.provider_beta.provider_id,
        reason="transfer of care")

    predecessor = identity.repos.managing_clinicians.get_by_id(first.assignment_id)
    assert predecessor.status is ManagingClinicianStatus.TRANSFERRED
    assert predecessor.superseded_by_assignment_id == successor.assignment_id
    assert successor.supersedes_assignment_id == first.assignment_id


# ===========================================================================
# authorization + audit through real storage
# ===========================================================================

def test_authorization_boundaries_hold_against_firestore(identity):
    with pytest.raises(IdentityAuthorizationError):
        identity.svc.link_source_system(
            identity.caregiver_alpha, identity.topo.child_beta.child_id,
            SourceSystem.PARENT, ext(identity, "sess-x"))
    with pytest.raises(IdentityAuthorizationError):
        identity.svc.assign_managing_clinician(
            identity.caregiver_alpha, identity.topo.child_alpha.child_id,
            identity.topo.provider_alpha.provider_id)
    with pytest.raises(IdentityValidationError):
        identity.svc.assign_managing_clinician(
            identity.provider_alpha, identity.topo.child_alpha.child_id,
            identity.topo.provider_gamma.provider_id)


def test_audit_events_persist_and_leak_nothing(identity):
    child = identity.topo.child_alpha.child_id
    external = ext(identity, "sess-audit")
    identity.svc.link_source_system(identity.caregiver_alpha, child,
                                    SourceSystem.PARENT, external,
                                    request_id="req-fictional-emu-1")
    identity.svc.assign_managing_clinician(
        identity.provider_alpha, child, identity.topo.provider_alpha.provider_id,
        request_id="req-fictional-emu-2")

    events = identity.repos.audit_events.list_for_child(child)
    actions = {e.action for e in events}
    assert AuditAction.SOURCE_LINK_CREATED in actions
    assert AuditAction.MANAGING_CLINICIAN_ASSIGNED in actions
    assert all(e.result is AuditResult.SUCCESS for e in events)

    import json

    blob = json.dumps([encode(e) for e in events])
    assert external not in blob, "the external identifier must not reach audit"
    for sentinel in ALL_SENTINELS:
        assert sentinel not in blob


def test_identity_collections_are_pilot_prefixed(identity, firestore_client):
    identity.svc.link_source_system(
        identity.caregiver_alpha, identity.topo.child_alpha.child_id,
        SourceSystem.PARENT, ext(identity, "sess-collections"))
    names = sorted(c.id for c in firestore_client.collections())
    assert "pilot_source_system_links" in names
    assert "pilot_identity_claims" in names
    for name in names:
        assert name.startswith("pilot_"), name
