"""0.4A — longitudinal identity bridge.

## One repository implementation, two stores

There is no parallel in-memory repository. `FirestoreRepositories` is written
against the `DocumentStore` port, and `FakeDocumentStore` is the in-memory
implementation of that port — so "in-memory behaviour" and "Firestore
behaviour" are the SAME repository code over two stores. That is what makes
"the adapter matches in-memory" a meaningful claim rather than a comparison of
two hand-written implementations that could drift.

These tests run against `FakeDocumentStore`. The identical scenarios run
against a real emulator in `pilot_runtime/tests/integration/`.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pilot_backend.audit.events import AuditAction, AuditResult
from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth import VerifiedToken, resolve_principal
from pilot_backend.domain.enums import ConnectionStatus
from pilot_backend.domain.identity_claims import (
    ClaimKind,
    ClaimError,
    IdentityClaim,
    claim_document_id,
    key_digest,
)
from pilot_backend.domain.managing_clinician import (
    ManagingClinicianAssignment,
    ManagingClinicianError,
    ManagingClinicianStatus,
)
from pilot_backend.domain.roles import ActorRole
from pilot_backend.domain.source_link import (
    SourceLinkError,
    SourceLinkStatus,
    SourceSystem,
    SourceSystemLink,
)
from pilot_backend.fixtures.secure_topology import (
    CAREGIVER_ALPHA_SUBJECT,
    CAREGIVER_BETA_SUBJECT,
    PROVIDER_ALPHA_SUBJECT,
    PROVIDER_BETA_SUBJECT,
    build_secure_topology,
)
from pilot_backend.identity import (
    IdentityAuthorizationError,
    IdentityConflict,
    IdentityValidationError,
    LongitudinalIdentityService,
)
from pilot_backend.persistence import FakeDocumentStore, FirestoreRepositories, encode
from pilot_backend.repository.interface import DuplicateRecord

from .test_secure_foundation import ALL_SENTINELS, SENTINEL_NOTE

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
PILOT_ROOT = Path(__file__).resolve().parents[1]


# --- harness ---------------------------------------------------------------

class _AdvancingClock:
    """Deterministic but strictly increasing.

    A frozen clock gave two successive assignments the same `effective_from`,
    so history ordering fell back to the id tiebreak and read as reversed.
    Real wall-clock time always advances between two writes; the test clock
    should too, or it tests a situation that cannot occur.
    """

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
        self.svc = LongitudinalIdentityService(
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


@pytest.fixture()
def s():
    return Stack()


# ===========================================================================
# canonical identity
# ===========================================================================

def test_canonical_child_identity_is_still_chld(s):
    assert s.topo.child_alpha.child_id.startswith("chld_")
    link = s.svc.link_source_system(
        s.caregiver_alpha, s.topo.child_alpha.child_id,
        SourceSystem.PARENT, "sess-fictional-001")
    assert link.child_id == s.topo.child_alpha.child_id
    assert link.child_id.startswith("chld_")


def test_external_ids_are_never_canonical(s):
    """The link points AT the canonical id; it does not become one."""
    link = s.svc.link_source_system(
        s.caregiver_alpha, s.topo.child_alpha.child_id,
        SourceSystem.PARENT, "sess-fictional-001",
        external_owner_ref="uid-fictional-alpha")
    assert not link.external_id.startswith("chld_")
    assert link.link_id.startswith("sslk_")
    # The owner ref is provenance only — never a join key.
    assert link.external_owner_ref == "uid-fictional-alpha"


# ===========================================================================
# linking, both source systems
# ===========================================================================

def test_parent_external_id_links_correctly(s):
    link = s.svc.link_source_system(
        s.caregiver_alpha, s.topo.child_alpha.child_id,
        SourceSystem.PARENT, "sess-fictional-001")
    assert link.is_active and link.source_system is SourceSystem.PARENT
    assert s.svc.resolve_authorized_child(
        s.caregiver_alpha, SourceSystem.PARENT,
        "sess-fictional-001") == s.topo.child_alpha.child_id


def test_therapist_external_child_id_links_correctly(s):
    link = s.svc.link_source_system(
        s.provider_alpha, s.topo.child_alpha.child_id,
        SourceSystem.THERAPIST, "therapist-child-fictional-001")
    assert link.is_active and link.source_system is SourceSystem.THERAPIST
    assert s.svc.resolve_authorized_child(
        s.provider_alpha, SourceSystem.THERAPIST,
        "therapist-child-fictional-001") == s.topo.child_alpha.child_id


def test_both_systems_may_link_the_same_child(s):
    s.svc.link_source_system(s.caregiver_alpha, s.topo.child_alpha.child_id,
                             SourceSystem.PARENT, "sess-fictional-001")
    s.svc.link_source_system(s.provider_alpha, s.topo.child_alpha.child_id,
                             SourceSystem.THERAPIST, "therapist-child-001")
    active = s.svc.list_source_links(s.caregiver_alpha, s.topo.child_alpha.child_id)
    assert {l.source_system for l in active} == {SourceSystem.PARENT,
                                                 SourceSystem.THERAPIST}


def test_an_exact_repeat_is_idempotent_not_a_conflict(s):
    first = s.svc.link_source_system(
        s.caregiver_alpha, s.topo.child_alpha.child_id,
        SourceSystem.PARENT, "sess-fictional-001")
    again = s.svc.link_source_system(
        s.caregiver_alpha, s.topo.child_alpha.child_id,
        SourceSystem.PARENT, "sess-fictional-001")
    assert again.link_id == first.link_id
    assert len(s.svc.list_source_links(
        s.caregiver_alpha, s.topo.child_alpha.child_id)) == 1


# ===========================================================================
# uniqueness — the point of this slice
# ===========================================================================

@pytest.mark.parametrize("system,actor", [
    (SourceSystem.PARENT, "caregiver_alpha"),
    (SourceSystem.THERAPIST, "provider_alpha"),
])
def test_a_child_cannot_have_two_active_links_for_one_system(s, system, actor):
    principal = getattr(s, actor)
    s.svc.link_source_system(principal, s.topo.child_alpha.child_id,
                             system, "external-fictional-001")
    with pytest.raises(IdentityConflict) as exc:
        s.svc.link_source_system(principal, s.topo.child_alpha.child_id,
                                 system, "external-fictional-002")
    assert "already has an active link" in str(exc.value)


def test_one_external_id_cannot_map_actively_to_two_children(s):
    """The cross-child constraint — the dangerous one."""
    s.svc.link_source_system(s.caregiver_alpha, s.topo.child_alpha.child_id,
                             SourceSystem.PARENT, "sess-fictional-shared")
    # Caregiver-Beta is legitimately authorized for Child-Beta, so this is
    # refused by the UNIQUENESS rule, not by authorization.
    with pytest.raises(IdentityConflict) as exc:
        s.svc.link_source_system(s.caregiver_beta, s.topo.child_beta.child_id,
                                 SourceSystem.PARENT, "sess-fictional-shared")
    assert "another child" in str(exc.value)


def test_the_claim_is_what_enforces_uniqueness_not_the_read(s):
    """Bypass the advisory read entirely; the claim must still refuse."""
    s.svc.link_source_system(s.caregiver_alpha, s.topo.child_alpha.child_id,
                             SourceSystem.PARENT, "sess-fictional-001")
    digest = key_digest(s.topo.child_alpha.child_id, SourceSystem.PARENT.value)
    replay = IdentityClaim.build(
        ClaimKind.CHILD_SOURCE,
        (s.topo.child_alpha.child_id, SourceSystem.PARENT.value), 0,
        holder_ref="forged", child_id=s.topo.child_alpha.child_id, now=T0)
    with pytest.raises(DuplicateRecord):
        s.repos.identity_claims.claim(replay)
    assert s.repos.identity_claims.count_claims_for_key(digest) == 1


def test_the_generation_does_not_advance_when_a_competitor_wins(s):
    """The property that makes the mutex work.

    An earlier version counted claims, so a winner advanced the counter and
    the next contender simply took generation+1 and ALSO won. Generations
    must move only on release.
    """
    child = s.topo.child_alpha.child_id
    digest = key_digest(child, SourceSystem.PARENT.value)
    assert s.repos.identity_claims.next_generation(
        ClaimKind.CHILD_SOURCE, digest) == 0

    link = s.svc.link_source_system(s.caregiver_alpha, child,
                                    SourceSystem.PARENT, "sess-fictional-001")
    assert s.repos.identity_claims.next_generation(
        ClaimKind.CHILD_SOURCE, digest) == 0, "a winner must not advance it"

    s.svc.end_source_link(s.caregiver_alpha, link.link_id)
    assert s.repos.identity_claims.next_generation(
        ClaimKind.CHILD_SOURCE, digest) == 1, "a release must advance it"


def test_claim_ids_are_deterministic_and_collision_resistant():
    a = claim_document_id(ClaimKind.CHILD_SOURCE, key_digest("chld_1", "parent"), 0)
    b = claim_document_id(ClaimKind.CHILD_SOURCE, key_digest("chld_1", "parent"), 0)
    assert a == b, "the same key must produce the same document id"
    # NUL-joining prevents ("a","bc") colliding with ("ab","c").
    assert key_digest("a", "bc") != key_digest("ab", "c")
    assert key_digest("CHLD_1", "PARENT") == key_digest("chld_1", "parent")
    with pytest.raises(ClaimError):
        key_digest("chld_1", "")
    with pytest.raises(ClaimError):
        claim_document_id(ClaimKind.CHILD_SOURCE, "d", -1)


def test_a_failed_second_claim_leaves_nothing_behind(s):
    """Atomicity replaced the orphan-release workaround.

    Previously the two claims were separate writes, so losing the second one
    stranded the first and the service had to release it explicitly. Both
    claims and the link now commit in ONE transaction, so a lost race writes
    NOTHING — there is no orphan to recover, and no generation is consumed.
    """
    child_b = s.topo.child_beta.child_id
    external = "sess-fictional-orphan"

    competitor = IdentityClaim.build(
        ClaimKind.CHILD_SOURCE, (child_b, SourceSystem.PARENT.value), 0,
        holder_ref="pending:competitor", child_id=child_b, now=T0)
    s.repos.identity_claims.claim(competitor)

    external_digest = key_digest(SourceSystem.PARENT.value, external)
    assert s.repos.identity_claims.count_claims_for_key(external_digest) == 0

    with pytest.raises(IdentityConflict):
        s.svc.link_source_system(s.caregiver_beta, child_b,
                                 SourceSystem.PARENT, external)

    # Nothing was persisted: no external claim, no release, no generation used.
    assert s.repos.identity_claims.count_claims_for_key(external_digest) == 0
    assert s.repos.identity_claims.next_generation(
        ClaimKind.EXTERNAL_IDENTITY, external_digest) == 0
    assert s.repos.source_links.list_for_child(child_b, include_ended=True) == []

    # ...and the external identity is untouched, so a legitimate child gets it.
    link = s.svc.link_source_system(s.caregiver_alpha, s.topo.child_alpha.child_id,
                                    SourceSystem.PARENT, external)
    assert link.is_active


def test_a_crash_between_claim_and_record_persists_nothing(s):
    """Fault injection: the exact window the founder review flagged.

    A process death after the claims but before the link write used to strand
    the external identity permanently. The write is now one transaction, so an
    exception raised mid-acquisition rolls everything back.
    """
    child = s.topo.child_alpha.child_id
    external = "sess-fictional-crash"
    digest = key_digest(SourceSystem.PARENT.value, external)

    real_factory = s.svc._repos_factory

    class _CrashAfterClaims:
        """Repository set that dies immediately after both claims are staged."""

        def __init__(self, inner):
            self._inner = inner
            self.identity_claims = inner.identity_claims
            self.managing_clinicians = inner.managing_clinicians

        @property
        def source_links(self):
            raise RuntimeError("simulated process failure before the record write")

        def __getattr__(self, name):
            return getattr(self._inner, name)

    s.svc._repos_factory = lambda store: _CrashAfterClaims(real_factory(store))
    try:
        with pytest.raises(RuntimeError):
            s.svc.link_source_system(s.caregiver_alpha, child,
                                     SourceSystem.PARENT, external)
    finally:
        s.svc._repos_factory = real_factory

    assert s.repos.identity_claims.count_claims_for_key(digest) == 0, "claim survived a crash"
    assert s.repos.source_links.list_for_child(child, include_ended=True) == []

    # The key is still usable — nothing was stranded.
    link = s.svc.link_source_system(s.caregiver_alpha, child,
                                    SourceSystem.PARENT, external)
    assert link.is_active


def test_a_crash_during_managing_clinician_assignment_persists_nothing(s):
    child = s.topo.child_alpha.child_id
    digest = key_digest(child)
    real_factory = s.svc._repos_factory

    class _CrashAfterClaim:
        def __init__(self, inner):
            self._inner = inner
            self.identity_claims = inner.identity_claims

        @property
        def managing_clinicians(self):
            raise RuntimeError("simulated process failure before the record write")

        def __getattr__(self, name):
            return getattr(self._inner, name)

    s.svc._repos_factory = lambda store: _CrashAfterClaim(real_factory(store))
    try:
        with pytest.raises(RuntimeError):
            s.svc.assign_managing_clinician(
                s.provider_alpha, child, s.topo.provider_alpha.provider_id)
    finally:
        s.svc._repos_factory = real_factory

    assert s.repos.identity_claims.count_claims_for_key(digest) == 0
    assert s.repos.managing_clinicians.list_for_child(child, include_ended=True) == []
    assert s.svc.assign_managing_clinician(
        s.provider_alpha, child, s.topo.provider_alpha.provider_id).is_active


def test_ambiguous_external_resolution_fails_closed(s):
    """Two active links for one external id must refuse, never pick one."""
    first = s.svc.link_source_system(s.caregiver_alpha, s.topo.child_alpha.child_id,
                                     SourceSystem.PARENT, "sess-fictional-dup")
    # Force the corrupt state the service would never create.
    forged = SourceSystemLink.create(
        s.topo.child_beta.child_id, SourceSystem.PARENT, "sess-fictional-dup",
        now=T0)
    s.repos.source_links.create(forged)
    with pytest.raises(IdentityConflict) as exc:
        s.svc._resolve_child_for_external(SourceSystem.PARENT, "sess-fictional-dup")
    assert "more than one" in str(exc.value)


def test_internal_resolution_of_an_unknown_external_id_is_none_not_an_error(s):
    assert s.svc._resolve_child_for_external(SourceSystem.PARENT, "nope") is None
    assert s.svc._resolve_child_for_external(SourceSystem.PARENT, "") is None
    assert s.svc._resolve_child_for_external(SourceSystem.PARENT, "   ") is None


# ===========================================================================
# ending, replacement, history
# ===========================================================================

def test_ended_links_remain_reconstructible(s):
    child = s.topo.child_alpha.child_id
    link = s.svc.link_source_system(s.caregiver_alpha, child,
                                    SourceSystem.PARENT, "sess-fictional-001")
    ended = s.svc.end_source_link(s.caregiver_alpha, link.link_id,
                                  reason="family changed account")

    assert not ended.is_active and ended.ended_at is not None
    assert ended.end_reason == "family changed account"
    assert s.svc.list_source_links(s.caregiver_alpha, child) == []
    history = s.svc.list_source_links(s.caregiver_alpha, child, include_ended=True)
    assert [h.link_id for h in history] == [link.link_id]
    assert s.repos.source_links.get_by_id(link.link_id).external_id == "sess-fictional-001"


def test_a_new_link_is_possible_after_the_previous_one_ends(s):
    child = s.topo.child_alpha.child_id
    first = s.svc.link_source_system(s.caregiver_alpha, child,
                                     SourceSystem.PARENT, "sess-fictional-001")
    s.svc.end_source_link(s.caregiver_alpha, first.link_id)
    second = s.svc.link_source_system(s.caregiver_alpha, child,
                                      SourceSystem.PARENT, "sess-fictional-002")
    assert second.is_active and second.link_id != first.link_id
    assert len(s.svc.list_source_links(s.caregiver_alpha, child,
                                       include_ended=True)) == 2


def test_replacement_preserves_bidirectional_lineage(s):
    child = s.topo.child_alpha.child_id
    first = s.svc.link_source_system(s.caregiver_alpha, child,
                                     SourceSystem.PARENT, "sess-fictional-001")
    successor = s.svc.replace_source_link(
        s.caregiver_alpha, first.link_id, "sess-fictional-002",
        reason="account migration")

    predecessor = s.repos.source_links.get_by_id(first.link_id)
    assert predecessor.status is SourceLinkStatus.SUPERSEDED
    assert predecessor.superseded_by_link_id == successor.link_id
    assert successor.supersedes_link_id == first.link_id
    assert successor.is_active


def test_ending_an_already_ended_link_is_refused(s):
    link = s.svc.link_source_system(s.caregiver_alpha, s.topo.child_alpha.child_id,
                                    SourceSystem.PARENT, "sess-fictional-001")
    s.svc.end_source_link(s.caregiver_alpha, link.link_id)
    with pytest.raises(IdentityConflict):
        s.svc.end_source_link(s.caregiver_alpha, link.link_id)


def test_a_terminal_status_is_inactive_even_without_an_ended_timestamp():
    """Both halves of `is_active` must be load-bearing.

    Mutation testing found this: `end()` always sets status AND ended_at
    together, so dropping the status check from `is_active` changed nothing
    observable. A stored document CAN carry a terminal status with a null
    ended_at — the codec permits it, and a hand-edited or partially-migrated
    record would produce exactly that — and it must read as inactive.
    """
    from dataclasses import replace

    base = SourceSystemLink.create("chld_1", SourceSystem.PARENT, "e1", now=T0)
    for status in (SourceLinkStatus.ENDED, SourceLinkStatus.SUPERSEDED):
        corrupt = replace(base, status=status, ended_at=None)
        assert not corrupt.is_active, status

    orphan_timestamp = replace(base, status=SourceLinkStatus.ACTIVE, ended_at=T0)
    assert not orphan_timestamp.is_active


def test_a_terminal_assignment_is_inactive_even_without_an_effective_to():
    from dataclasses import replace

    base = ManagingClinicianAssignment.create(
        "chld_1", "prov_1", "prac_1", provider_connection_id="pcxn_1", now=T0)
    for status in (ManagingClinicianStatus.ENDED,
                   ManagingClinicianStatus.TRANSFERRED):
        assert not replace(base, status=status, effective_to=None).is_active, status
    assert not replace(base, effective_to=T0).is_active


def test_domain_refuses_a_non_terminal_end_status():
    link = SourceSystemLink.create("chld_1", SourceSystem.PARENT, "e1", now=T0)
    with pytest.raises(SourceLinkError):
        link.end(status=SourceLinkStatus.ACTIVE)


# ===========================================================================
# managing clinician
# ===========================================================================

def test_exactly_one_active_managing_clinician(s):
    child = s.topo.child_alpha.child_id
    s.svc.assign_managing_clinician(s.provider_alpha, child,
                                    s.topo.provider_alpha.provider_id)
    with pytest.raises(IdentityConflict) as exc:
        s.svc.assign_managing_clinician(s.provider_alpha, child,
                                        s.topo.provider_alpha.provider_id)
    assert "already has an active managing clinician" in str(exc.value)
    assert len(s.repos.managing_clinicians.list_for_child(child)) == 1


def test_practice_of_record_comes_from_the_connection(s):
    assignment = s.svc.assign_managing_clinician(
        s.provider_alpha, s.topo.child_alpha.child_id,
        s.topo.provider_alpha.provider_id)
    assert assignment.practice_id == s.topo.link_alpha_provider.practice_id
    assert assignment.provider_connection_id == s.topo.link_alpha_provider.connection_id


def test_practice_mismatch_fails_closed(s):
    with pytest.raises(IdentityValidationError) as exc:
        s.svc.assign_managing_clinician(
            s.provider_alpha, s.topo.child_alpha.child_id,
            s.topo.provider_alpha.provider_id,
            expected_practice_id="prac_fictional_other")
    assert "practice" in str(exc.value)


def test_provider_without_an_active_connection_cannot_be_assigned(s):
    """Provider-Gamma's connection to Child-Alpha is PENDING, never activated."""
    with pytest.raises(IdentityValidationError) as exc:
        s.svc.assign_managing_clinician(
            s.provider_alpha, s.topo.child_alpha.child_id,
            s.topo.provider_gamma.provider_id)
    assert "no active connection" in str(exc.value)


def test_revoking_the_acting_providers_own_connection_denies_first(s):
    """Authorization runs before validation, so access is lost immediately.

    Originally written expecting IdentityValidationError. The actual
    behaviour is stricter and correct: revoking Provider-Alpha's connection
    removes their access to the child, so the request is refused at the
    authorization gate before the connection is ever validated.
    """
    child = s.topo.child_alpha.child_id
    s.repos.provider_child.end_connection(
        s.topo.link_alpha_provider.connection_id, status=ConnectionStatus.REVOKED)
    with pytest.raises(IdentityAuthorizationError) as exc:
        s.svc.assign_managing_clinician(s.provider_alpha, child,
                                        s.topo.provider_alpha.provider_id)
    assert "inactive_relationship" in str(exc.value)


def test_assigning_a_provider_whose_connection_was_revoked_fails_closed(s):
    """The validation path, with an acting provider who still has access."""
    from pilot_backend.domain.connections import ProviderChildConnection

    child = s.topo.child_alpha.child_id
    link = s.repos.provider_child.connect(ProviderChildConnection.create(
        s.topo.provider_beta.provider_id, child, s.topo.practice.practice_id, now=T0))
    s.repos.provider_child.activate(link.connection_id, now=T0)
    s.repos.provider_child.end_connection(link.connection_id,
                                          status=ConnectionStatus.REVOKED)

    with pytest.raises(IdentityValidationError) as exc:
        s.svc.assign_managing_clinician(s.provider_alpha, child,
                                        s.topo.provider_beta.provider_id)
    assert "no active connection" in str(exc.value)


def test_reassignment_after_ending_retains_history(s):
    child = s.topo.child_alpha.child_id
    first = s.svc.assign_managing_clinician(s.provider_alpha, child,
                                            s.topo.provider_alpha.provider_id)
    s.svc.end_managing_clinician(s.provider_alpha, child, reason="caseload change")
    assert s.svc.current_managing_clinician(s.provider_alpha, child) is None

    second = s.svc.assign_managing_clinician(s.provider_alpha, child,
                                             s.topo.provider_alpha.provider_id)
    history = s.svc.managing_clinician_history(s.provider_alpha, child)
    assert [h.assignment_id for h in history] == [first.assignment_id,
                                                  second.assignment_id]
    assert history[0].effective_from < history[1].effective_from
    assert history[0].status is ManagingClinicianStatus.ENDED
    assert history[0].effective_to is not None
    assert history[0].end_reason == "caseload change"


def test_transfer_preserves_bidirectional_lineage(s):
    child = s.topo.child_alpha.child_id
    # Give Provider-Beta an active connection to Child-Alpha so a transfer is legal.
    from pilot_backend.domain.connections import ProviderChildConnection
    link = s.repos.provider_child.connect(ProviderChildConnection.create(
        s.topo.provider_beta.provider_id, child, s.topo.practice.practice_id, now=T0))
    s.repos.provider_child.activate(link.connection_id, now=T0)

    first = s.svc.assign_managing_clinician(s.provider_alpha, child,
                                            s.topo.provider_alpha.provider_id)
    successor = s.svc.transfer_managing_clinician(
        s.provider_alpha, child, s.topo.provider_beta.provider_id,
        reason="transfer of care")

    predecessor = s.repos.managing_clinicians.get_by_id(first.assignment_id)
    assert predecessor.status is ManagingClinicianStatus.TRANSFERRED
    assert predecessor.superseded_by_assignment_id == successor.assignment_id
    assert successor.supersedes_assignment_id == first.assignment_id
    assert successor.provider_id == s.topo.provider_beta.provider_id
    assert s.svc.current_managing_clinician(
        s.provider_alpha, child).assignment_id == successor.assignment_id


def test_ending_with_no_active_assignment_is_refused(s):
    with pytest.raises(IdentityConflict):
        s.svc.end_managing_clinician(s.provider_alpha, s.topo.child_alpha.child_id)


def test_domain_requires_all_assignment_identifiers():
    for kwargs in ({"child_id": ""}, {"provider_id": ""}, {"practice_id": ""}):
        base = dict(child_id="chld_1", provider_id="prov_1", practice_id="prac_1")
        base.update(kwargs)
        with pytest.raises(ManagingClinicianError):
            ManagingClinicianAssignment.create(
                base["child_id"], base["provider_id"], base["practice_id"],
                provider_connection_id="pcxn_1")


# ===========================================================================
# authorization boundaries
# ===========================================================================

def test_caregiver_cannot_link_another_familys_child(s):
    with pytest.raises(IdentityAuthorizationError):
        s.svc.link_source_system(s.caregiver_alpha, s.topo.child_beta.child_id,
                                 SourceSystem.PARENT, "sess-fictional-x")


def test_caregiver_cannot_create_a_therapist_system_link(s):
    with pytest.raises(IdentityAuthorizationError) as exc:
        s.svc.link_source_system(s.caregiver_alpha, s.topo.child_alpha.child_id,
                                 SourceSystem.THERAPIST, "therapist-child-001")
    assert "require a provider" in str(exc.value)


def test_caregiver_cannot_assign_a_managing_clinician(s):
    with pytest.raises(IdentityAuthorizationError) as exc:
        s.svc.assign_managing_clinician(s.caregiver_alpha,
                                        s.topo.child_alpha.child_id,
                                        s.topo.provider_alpha.provider_id)
    assert "requires a provider" in str(exc.value)


def test_caregiver_cannot_end_or_transfer_a_managing_clinician(s):
    s.svc.assign_managing_clinician(s.provider_alpha, s.topo.child_alpha.child_id,
                                    s.topo.provider_alpha.provider_id)
    with pytest.raises(IdentityAuthorizationError):
        s.svc.end_managing_clinician(s.caregiver_alpha, s.topo.child_alpha.child_id)
    with pytest.raises(IdentityAuthorizationError):
        s.svc.transfer_managing_clinician(
            s.caregiver_alpha, s.topo.child_alpha.child_id,
            s.topo.provider_beta.provider_id)


def test_unrelated_provider_cannot_touch_identity(s):
    """Provider-Beta has no relationship to Child-Alpha."""
    for call in (
        lambda: s.svc.link_source_system(s.provider_beta, s.topo.child_alpha.child_id,
                                         SourceSystem.THERAPIST, "t-1"),
        lambda: s.svc.assign_managing_clinician(
            s.provider_beta, s.topo.child_alpha.child_id,
            s.topo.provider_alpha.provider_id),
        lambda: s.svc.list_source_links(s.provider_beta, s.topo.child_alpha.child_id),
        lambda: s.svc.current_managing_clinician(s.provider_beta,
                                                 s.topo.child_alpha.child_id),
    ):
        with pytest.raises(IdentityAuthorizationError):
            call()


def test_an_ended_relationship_revokes_identity_access(s):
    child = s.topo.child_alpha.child_id
    s.svc.link_source_system(s.caregiver_alpha, child,
                             SourceSystem.PARENT, "sess-fictional-001")
    s.repos.caregiver_child.end_connection(
        s.topo.link_alpha_caregiver.connection_id, status=ConnectionStatus.REVOKED)
    with pytest.raises(IdentityAuthorizationError):
        s.svc.list_source_links(s.caregiver_alpha, child)


def test_no_client_supplied_role_can_reach_the_service(s):
    """Role comes from the resolved principal; there is no parameter for it."""
    import inspect

    for fn in (s.svc.link_source_system, s.svc.assign_managing_clinician,
               s.svc.end_source_link, s.svc.transfer_managing_clinician):
        params = set(inspect.signature(fn).parameters)
        assert not (params & {"role", "actor_role", "uid", "is_admin",
                              "caregiver_id", "provider_role"}), fn.__name__


def test_every_public_service_method_requires_a_principal():
    """The structural form of "resolve then authorize" — not a convention.

    Any public method that could hand back a child id must take a principal,
    so there is no callable surface that turns an external identifier into
    usable child access without the gate. The raw resolver is private
    precisely because it cannot satisfy this.
    """
    import inspect

    for name, member in inspect.getmembers(
            LongitudinalIdentityService, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        params = list(inspect.signature(member).parameters)
        assert params[:2] == ["self", "principal"], (name, params)


def test_the_raw_resolver_is_not_part_of_the_public_surface():
    public = [n for n in dir(LongitudinalIdentityService) if not n.startswith("_")]
    assert "resolve_child_for_external" not in public
    assert "resolve_authorized_child" in public


def test_resolve_authorized_child_requires_authorization(s):
    child = s.topo.child_alpha.child_id
    s.svc.link_source_system(s.caregiver_alpha, child,
                             SourceSystem.PARENT, "sess-fictional-001")

    assert s.svc.resolve_authorized_child(
        s.caregiver_alpha, SourceSystem.PARENT, "sess-fictional-001") == child

    # Caregiver-Beta holds a perfectly valid token for another family.
    with pytest.raises(IdentityAuthorizationError):
        s.svc.resolve_authorized_child(s.caregiver_beta, SourceSystem.PARENT,
                                       "sess-fictional-001")
    # ...as does an unrelated provider.
    with pytest.raises(IdentityAuthorizationError):
        s.svc.resolve_authorized_child(s.provider_beta, SourceSystem.PARENT,
                                       "sess-fictional-001")


def test_unknown_and_unauthorized_external_ids_are_indistinguishable(s):
    """Otherwise this becomes an oracle for who is bound to whom."""
    child = s.topo.child_alpha.child_id
    s.svc.link_source_system(s.caregiver_alpha, child,
                             SourceSystem.PARENT, "sess-fictional-001")

    unauthorized, unknown = None, None
    try:
        s.svc.resolve_authorized_child(s.caregiver_beta, SourceSystem.PARENT,
                                       "sess-fictional-001")
    except IdentityAuthorizationError as exc:
        unauthorized = str(exc)
    try:
        s.svc.resolve_authorized_child(s.caregiver_beta, SourceSystem.PARENT,
                                       "sess-never-issued")
    except IdentityAuthorizationError as exc:
        unknown = str(exc)

    assert unauthorized == unknown, "the message distinguishes the two cases"
    assert child not in (unauthorized or "")


def test_an_ended_relationship_revokes_external_resolution(s):
    child = s.topo.child_alpha.child_id
    s.svc.link_source_system(s.caregiver_alpha, child,
                             SourceSystem.PARENT, "sess-fictional-001")
    s.repos.caregiver_child.end_connection(
        s.topo.link_alpha_caregiver.connection_id, status=ConnectionStatus.REVOKED)
    with pytest.raises(IdentityAuthorizationError):
        s.svc.resolve_authorized_child(s.caregiver_alpha, SourceSystem.PARENT,
                                       "sess-fictional-001")


# ===========================================================================
# no destructive delete
# ===========================================================================

@pytest.mark.parametrize("repo_name", ["identity_claims", "source_links",
                                       "managing_clinicians"])
def test_no_identity_repository_exposes_a_delete(repo_name):
    repos = FirestoreRepositories(FakeDocumentStore())
    repo = getattr(repos, repo_name)
    banned = ("delete", "remove", "purge", "drop", "destroy", "erase", "truncate")
    for attribute in dir(repo):
        if attribute.startswith("_"):
            continue
        assert not any(w in attribute.lower() for w in banned), (repo_name, attribute)


def test_identity_modules_never_call_a_delete():
    for relative in ("domain/source_link.py", "domain/managing_clinician.py",
                     "domain/identity_claims.py", "identity/service.py"):
        tree = ast.parse((PILOT_ROOT / relative).read_text())
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not ({"delete", "remove", "purge", "drop"} & called), relative


def test_the_claim_repository_cannot_be_updated():
    repo = FirestoreRepositories(FakeDocumentStore()).identity_claims
    assert not hasattr(repo, "update")
    assert not hasattr(repo, "set")


# ===========================================================================
# audit
# ===========================================================================

def test_material_actions_emit_audit_events(s):
    child = s.topo.child_alpha.child_id
    link = s.svc.link_source_system(s.caregiver_alpha, child,
                                    SourceSystem.PARENT, "sess-fictional-001",
                                    request_id="req-fictional-1")
    s.svc.end_source_link(s.caregiver_alpha, link.link_id,
                          request_id="req-fictional-2")
    s.svc.assign_managing_clinician(s.provider_alpha, child,
                                    s.topo.provider_alpha.provider_id,
                                    request_id="req-fictional-3")
    s.svc.end_managing_clinician(s.provider_alpha, child,
                                 request_id="req-fictional-4")

    actions = {e.action for e in s.repos.audit_events.list_for_child(child)}
    assert AuditAction.SOURCE_LINK_CREATED in actions
    assert AuditAction.SOURCE_LINK_ENDED in actions
    assert AuditAction.MANAGING_CLINICIAN_ASSIGNED in actions
    assert AuditAction.MANAGING_CLINICIAN_ENDED in actions

    for event in s.repos.audit_events.list_for_child(child):
        assert event.occurred_at.tzinfo is not None
        assert event.actor_application_id
        assert event.actor_role in (ActorRole.CAREGIVER, ActorRole.PROVIDER)
        assert event.request_id.startswith("req-fictional-")


def test_refused_writes_are_audited_as_failures(s):
    child = s.topo.child_alpha.child_id
    s.svc.link_source_system(s.caregiver_alpha, child,
                             SourceSystem.PARENT, "sess-fictional-001")
    with pytest.raises(IdentityConflict):
        s.svc.link_source_system(s.caregiver_alpha, child,
                                 SourceSystem.PARENT, "sess-fictional-002")
    failures = [e for e in s.repos.audit_events.list_for_child(child)
                if e.result is AuditResult.FAILURE]
    assert failures and failures[0].action is AuditAction.SOURCE_LINK_CREATED


def test_audit_never_records_the_external_identifier(s):
    """A Parent session id is an external identifier, not audit metadata."""
    child = s.topo.child_alpha.child_id
    secret_external = "sess-" + SENTINEL_NOTE
    s.svc.link_source_system(s.caregiver_alpha, child,
                             SourceSystem.PARENT, secret_external)
    import json

    blob = json.dumps([encode(e) for e in s.repos.audit_events.list_all()])
    assert secret_external not in blob
    for sentinel in ALL_SENTINELS:
        assert sentinel not in blob


def test_identity_errors_are_phi_safe_by_declaration():
    for cls in (IdentityConflict, IdentityAuthorizationError,
                IdentityValidationError, SourceLinkError, ManagingClinicianError,
                ClaimError):
        assert getattr(cls, "PHI_SAFE_MESSAGE", False) is True, cls.__name__


# ===========================================================================
# persistence shape
# ===========================================================================

def test_identity_records_round_trip_through_the_codecs(s):
    child = s.topo.child_alpha.child_id
    link = s.svc.link_source_system(s.caregiver_alpha, child,
                                    SourceSystem.PARENT, "sess-fictional-001")
    assignment = s.svc.assign_managing_clinician(
        s.provider_alpha, child, s.topo.provider_alpha.provider_id)
    assert s.repos.source_links.get_by_id(link.link_id) == link
    assert s.repos.managing_clinicians.get_by_id(assignment.assignment_id) == assignment
    claim = s.repos.identity_claims.get_by_id(link.child_source_claim_id)
    assert claim.kind is ClaimKind.CHILD_SOURCE


def test_only_pilot_prefixed_collections_are_used(s):
    s.svc.link_source_system(s.caregiver_alpha, s.topo.child_alpha.child_id,
                             SourceSystem.PARENT, "sess-fictional-001")
    for name in s.store.collections():
        assert name.startswith("pilot_"), name
    assert "pilot_source_system_links" in s.store.collections()
    assert "pilot_identity_claims" in s.store.collections()


def test_no_parent_23_resource_is_reachable():
    from pilot_backend.persistence.collections import COLLECTIONS
    for name in COLLECTIONS.values():
        assert "genex-api" not in name and name != "sessions"


# ===========================================================================
# scope guard — through 0.4E
# ===========================================================================

def test_no_later_slice_object_was_implemented():
    """0.4F+ and RTM must remain absent.

    0.4B/C narrowed this by exactly six names — `GoalSuggestion`,
    `ClinicalGoal`, `CaregiverApprovedGoal`, `MonthlyFocusPlan`,
    `MonthlyGoalAllocation` and `MonthlyGoalSnapshot`.

    0.4D/E narrowed it by exactly five more — `WeeklyCycle`,
    `ActivityGoalAlignment`, `CoverageGap`, `ObservationEvent` and
    `AdaptationRecord`.

    `WeeklyPlan` and `WeeklyActivityAllocation` deliberately STAY banned:
    0.4D/E does not own a weekly plan — the Parent plan is reached only by
    external id through `WeeklyPlanLink` — and it does not introduce a
    separate activity-level allocation object. Every RTM, month-end and
    coding name is untouched.
    """
    banned = {
        "WeeklyPlan", "WeeklyActivityAllocation",
        "PayerVerification",
        "MonitoringDay", }
    for path in sorted(PILOT_ROOT.rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        names = {n.name for n in ast.walk(ast.parse(path.read_text()))
                 if isinstance(n, ast.ClassDef)}
        assert not (banned & names), (path.name, banned & names)
