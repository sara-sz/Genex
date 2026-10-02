"""0.5A — authenticated identity, caregiver bootstrap, Parent child bridge.

Four operations and four routes. What is proved here:

**Uniqueness is on the auth subject, at write time.** `AuthSubjectIdentityClaim`
keys a document on `sha256(auth_subject)[:32]`, so two bootstraps for one
subject target one document and `create` decides. The CONTENTION proof is in
`pilot_runtime/tests/integration/test_identity_bridge_emulator.py` — this file
uses `FakeDocumentStore`, which is a plain dict and settles nothing about
racing writers.

**A legacy caregiver gains a claim, never a twin.** Caregivers created before
this primitive existed have no claim behind them. Each of the five pinned
outcomes is asserted below, including the two refusals.

**Nothing in the request can assert who the caller is.** The subject comes from
the verified token; the role comes from which repository matched it. The
bootstrap and link handlers never read `wsgi.input` at all, which is asserted
by handing them a body full of forged identity fields and a stream that raises
if anyone touches it.

**The Parent boundary is read-only and non-enumerating.** A session that does
not exist and a session owned by someone else produce the same error and the
same HTTP status.
"""

from __future__ import annotations

import hashlib
import io
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from pilot_backend.audit.events import ALLOWED_METADATA_KEYS, AuditAction
from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth import DevAuthVerifier, VerifiedToken
from pilot_backend.auth.resolver import PrincipalResolutionError, resolve_principal
from pilot_backend.config import PilotSettings
from pilot_backend.domain.auth_identity import (
    AuthIdentityError,
    AuthSubjectIdentityClaim,
    auth_subject_claim_id,
    subject_fingerprint,
)
from pilot_backend.domain.entities import Caregiver, Child
from pilot_backend.domain.enums import ConnectionStatus, EntityStatus
from pilot_backend.domain.roles import ActorRole
from pilot_backend.domain.source_link import SourceSystem
from pilot_backend.fixtures.secure_topology import (
    CAREGIVER_ALPHA_SUBJECT,
    PROVIDER_ALPHA_SUBJECT,
    UNPROVISIONED_SUBJECT,
    build_secure_topology,
)
from pilot_backend.integration.errors import (
    AmbiguousSubjectState,
    IntegrationError,
    ParentSessionUnavailable,
    SecondSessionUnresolved,
    SubjectAlreadyHeld,
)
from pilot_backend.integration.identity_service import (
    AppIdentity,
    IntegrationIdentityService,
    ParentLinkResult,
)
from pilot_backend.integration.parent_source import (
    InMemoryParentSessionSource,
    ParentSessionFacts,
    ParentSessionSource,
)
from pilot_backend.persistence import FakeDocumentStore, FirestoreRepositories, encode
from pilot_backend.persistence.codecs import decode
from pilot_backend.transport import build_application

from .test_secure_foundation import ALL_SENTINELS, SENTINEL_EMAIL

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

#: A subject with no application record of any kind.
NEWCOMER = "fictional-subject-newcomer-alpha"
NEWCOMER_TWO = "fictional-subject-newcomer-beta"
#: A caregiver that predates `AuthSubjectIdentityClaim`.
LEGACY = "fictional-subject-legacy-caregiver"
SESSION_ONE = "fictional-parent-session-0001"
SESSION_TWO = "fictional-parent-session-0002"


class _AdvancingClock:
    """Deterministic, strictly increasing, thread-safe — as in every slice."""

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
def wiring():
    """Repositories, recorder, a Parent source and the service under test."""
    repos = FirestoreRepositories(FakeDocumentStore())
    topology = build_secure_topology(repos)
    recorder = AuditRecorder(repos.audit_events, environment="test")
    parent = InMemoryParentSessionSource()

    class Bundle:
        pass

    bundle = Bundle()
    bundle.repos = repos
    bundle.topo = topology
    bundle.recorder = recorder
    bundle.parent = parent
    bundle.service = IntegrationIdentityService(
        repos=repos, parent_source=parent, recorder=recorder,
        now=_AdvancingClock(T0))
    return bundle


def principal_for(repos, subject):
    return resolve_principal(VerifiedToken(subject=subject), repos)


def seed_legacy_caregiver(repos, subject=LEGACY, *, name="Legacy-Caregiver"):
    """A caregiver with an auth subject and NO subject claim behind it."""
    return repos.caregivers.create(
        Caregiver.create(name, auth_subject=subject, now=T0))


def audit_docs(repos):
    return [doc for _id, doc in repos.store.list_all("pilot_audit_events")]


def claim_docs(repos):
    return repos.store.list_all("pilot_auth_subject_claims")


def snapshot_all(repos):
    """Every document id in every collection except the audit trail.

    Audit is excluded because a REFUSAL is supposed to write an audit event —
    that is the one thing a fail-closed path legitimately persists. Comparing
    ids rather than counts means a write-plus-delete cannot pass as unchanged.
    """
    return {name: sorted(doc_id for doc_id, _ in repos.store.list_all(name))
            for name in repos.store.collections()
            if name != "pilot_audit_events"}


# ===========================================================================
# the claim primitive
# ===========================================================================

def test_claim_id_is_derived_from_the_subject_and_nothing_else():
    """Two callers computing a claim for one subject get the same document id.

    This equality IS the uniqueness mechanism: without it, concurrent writers
    would target different documents and both would succeed.
    """
    first = AuthSubjectIdentityClaim.build(
        NEWCOMER, holder_actor_id="cgvr_one",
        holder_actor_type=ActorRole.CAREGIVER, now=T0)
    second = AuthSubjectIdentityClaim.build(
        NEWCOMER, holder_actor_id="cgvr_two",
        holder_actor_type=ActorRole.CAREGIVER, now=T0 + timedelta(days=5))
    assert first.claim_id == second.claim_id
    assert first.subject_fingerprint == second.subject_fingerprint


def test_claim_id_differs_for_different_subjects():
    a = AuthSubjectIdentityClaim.build(
        NEWCOMER, holder_actor_id="cgvr_one",
        holder_actor_type=ActorRole.CAREGIVER, now=T0)
    b = AuthSubjectIdentityClaim.build(
        NEWCOMER_TWO, holder_actor_id="cgvr_one",
        holder_actor_type=ActorRole.CAREGIVER, now=T0)
    assert a.claim_id != b.claim_id


def test_fingerprint_is_sha256_truncated_and_not_the_subject():
    """Derivable from the subject, not reversible to it."""
    fingerprint = subject_fingerprint(NEWCOMER)
    assert fingerprint == hashlib.sha256(
        NEWCOMER.encode("utf-8")).hexdigest()[:32]
    assert len(fingerprint) == 32
    assert NEWCOMER not in fingerprint
    assert NEWCOMER not in auth_subject_claim_id(fingerprint)


def test_fingerprint_is_case_sensitive():
    """A Firebase uid is case-sensitive; folding case would merge two accounts.

    `key_digest` in 0.4A lower-cases because its inputs are already-normalised
    application ids and enum values. That reasoning does not transfer, and this
    test is what stops someone from "making them consistent".
    """
    assert subject_fingerprint("AbCdEf") != subject_fingerprint("abcdef")


def test_fingerprint_trims_surrounding_whitespace():
    assert subject_fingerprint("  " + NEWCOMER + "  ") == subject_fingerprint(
        NEWCOMER)


def test_empty_subject_is_refused():
    for value in ("", "   ", None):
        with pytest.raises(AuthIdentityError):
            subject_fingerprint(value)


def test_claim_id_must_match_its_fingerprint():
    """A mismatched id would be invisible to the lookup that enforces uniqueness."""
    with pytest.raises(AuthIdentityError):
        AuthSubjectIdentityClaim(
            claim_id="authsubj__" + "0" * 32,
            subject_fingerprint=subject_fingerprint(NEWCOMER),
            holder_actor_id="cgvr_one",
            holder_actor_type=ActorRole.CAREGIVER, created_at=T0)


def test_claim_requires_a_holder_and_a_real_role():
    fingerprint = subject_fingerprint(NEWCOMER)
    with pytest.raises(AuthIdentityError):
        AuthSubjectIdentityClaim(
            claim_id=auth_subject_claim_id(fingerprint),
            subject_fingerprint=fingerprint, holder_actor_id="  ",
            holder_actor_type=ActorRole.CAREGIVER, created_at=T0)
    with pytest.raises(AuthIdentityError):
        AuthSubjectIdentityClaim(
            claim_id=auth_subject_claim_id(fingerprint),
            subject_fingerprint=fingerprint, holder_actor_id="cgvr_one",
            holder_actor_type="caregiver", created_at=T0)


def test_claim_round_trips_through_the_persistence_codec():
    claim = AuthSubjectIdentityClaim.build(
        NEWCOMER, holder_actor_id="cgvr_one",
        holder_actor_type=ActorRole.CAREGIVER, now=T0)
    document = encode(claim)
    assert decode(AuthSubjectIdentityClaim, document) == claim
    # The encoded document holds the fingerprint and NOT the subject.
    assert NEWCOMER not in json.dumps(document)
    assert document["subject_fingerprint"] == subject_fingerprint(NEWCOMER)


def test_encoded_claim_contains_no_nested_array():
    """The 0.4F/G Firestore defect, re-asserted for the new record type.

    Firestore rejects an array of arrays; `FakeDocumentStore` accepts one. A
    record shape that cannot persist must fail here, not in the emulator suite.
    """
    document = encode(AuthSubjectIdentityClaim.build(
        NEWCOMER, holder_actor_id="cgvr_one",
        holder_actor_type=ActorRole.CAREGIVER, now=T0))

    def walk(value):
        if isinstance(value, (list, tuple)):
            for item in value:
                assert not isinstance(item, (list, tuple)), document
                walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)

    walk(document)


def test_claim_repository_is_create_only():
    """No update, no set, no release, no delete — the capability is ABSENT.

    An app identity is permanent and there is no product operation that unbinds
    a subject. A release path that existed "just in case" is a path by which a
    subject could be silently re-pointed at a different person.
    """
    repos = FirestoreRepositories(FakeDocumentStore())
    repo = repos.auth_subject_claims
    for forbidden in ("update", "set", "release", "delete", "remove",
                      "update_status", "transfer"):
        assert not hasattr(repo, forbidden), forbidden
    # `model` is the dataclass the base repository decodes into, not an
    # operation; everything else callable is listed.
    assert sorted(
        name for name in dir(repo)
        if not name.startswith("_") and callable(getattr(repo, name))
        and name != "model"
    ) == ["claim", "find_for_subject", "get_by_id"]


def test_claim_has_no_generation_counter():
    """0.4A claims carry one because their keys get RELEASED. This one must not.

    A counter with nothing to advance it is dead structure a later reader would
    assume means something — and would be the obvious hook for a re-binding
    feature that has had no review.
    """
    fields = AuthSubjectIdentityClaim.__dataclass_fields__
    for name in fields:
        assert "generation" not in name, name


def test_claim_lives_in_its_own_collection():
    """Not mixed in with the child-scoped 0.4A claims."""
    from pilot_backend.persistence.collections import collection_for

    assert collection_for("auth_subject_claim") == "pilot_auth_subject_claims"
    assert collection_for("auth_subject_claim") != collection_for("identity_claim")


def test_child_scoped_claim_kind_gained_no_auth_subject_member():
    """The rejected design, asserted absent.

    `IdentityClaim.child_id` means a real canonical child. An auth-subject
    binding is won before any child exists, so representing it there would mean
    a child-scoped field carrying a non-child value.
    """
    from pilot_backend.domain.identity_claims import ClaimKind

    for member in ClaimKind:
        assert "auth" not in member.value.lower(), member
        assert "subject" not in member.value.lower(), member


# ===========================================================================
# /pilot/me — resolves, never creates
# ===========================================================================

def test_whoami_returns_the_caregiver_identity(wiring):
    principal = principal_for(wiring.repos, CAREGIVER_ALPHA_SUBJECT)
    identity = wiring.service.whoami(principal)
    assert identity.role is ActorRole.CAREGIVER
    assert identity.actor_id == wiring.topo.caregiver_alpha.caregiver_id
    assert identity.caregiver_id == identity.actor_id
    assert identity.provider_id is None


def test_whoami_returns_the_provider_identity_with_its_practice(wiring):
    principal = principal_for(wiring.repos, PROVIDER_ALPHA_SUBJECT)
    identity = wiring.service.whoami(principal)
    assert identity.role is ActorRole.PROVIDER
    assert identity.provider_id == wiring.topo.provider_alpha.provider_id
    assert identity.practice_id == wiring.topo.practice.practice_id
    assert identity.caregiver_id is None


def test_whoami_creates_nothing(wiring):
    """An identity read must not have a write as a side effect."""
    before = sorted(wiring.repos.store.collections())
    counts = {name: len(wiring.repos.store.list_all(name)) for name in before}
    wiring.service.whoami(principal_for(wiring.repos, CAREGIVER_ALPHA_SUBJECT))
    assert sorted(wiring.repos.store.collections()) == before
    assert {name: len(wiring.repos.store.list_all(name))
            for name in before} == counts


def test_whoami_payload_carries_identifiers_and_a_role_only(wiring):
    payload = wiring.service.whoami(
        principal_for(wiring.repos, PROVIDER_ALPHA_SUBJECT)).as_payload()
    assert set(payload) == {"actor_id", "role", "provider_id", "practice_id"}
    assert PROVIDER_ALPHA_SUBJECT not in json.dumps(payload)


def test_whoami_omits_absent_fields_rather_than_sending_nulls(wiring):
    payload = wiring.service.whoami(
        principal_for(wiring.repos, CAREGIVER_ALPHA_SUBJECT)).as_payload()
    assert None not in payload.values()
    assert "provider_id" not in payload


def test_an_unprovisioned_subject_has_no_principal_to_resolve(wiring):
    """`/pilot/me` cannot be reached without an application record.

    This is what makes bootstrap an explicit POST rather than a side effect.
    """
    with pytest.raises(PrincipalResolutionError):
        principal_for(wiring.repos, UNPROVISIONED_SUBJECT)


# ===========================================================================
# caregiver bootstrap — the new-subject path
# ===========================================================================

def test_bootstrap_creates_one_caregiver_and_one_claim(wiring):
    caregiver = wiring.service.bootstrap_caregiver(NEWCOMER)
    assert caregiver.auth_subject == NEWCOMER
    assert caregiver.status is EntityStatus.ACTIVE
    claim = wiring.repos.auth_subject_claims.find_for_subject(NEWCOMER)
    assert claim.holder_actor_id == caregiver.caregiver_id
    assert claim.is_held_by_caregiver
    assert len(claim_docs(wiring.repos)) == 1


def test_bootstrap_is_idempotent_and_returns_the_same_caregiver(wiring):
    first = wiring.service.bootstrap_caregiver(NEWCOMER)
    second = wiring.service.bootstrap_caregiver(NEWCOMER)
    third = wiring.service.bootstrap_caregiver(NEWCOMER)
    assert first.caregiver_id == second.caregiver_id == third.caregiver_id
    assert len(claim_docs(wiring.repos)) == 1


def test_repeat_bootstrap_writes_nothing_at_all(wiring):
    """The common case — every app launch — must be reads only."""
    wiring.service.bootstrap_caregiver(NEWCOMER)
    counts = {name: len(wiring.repos.store.list_all(name))
              for name in wiring.repos.store.collections()}
    wiring.service.bootstrap_caregiver(NEWCOMER)
    assert {name: len(wiring.repos.store.list_all(name))
            for name in wiring.repos.store.collections()} == counts


def test_a_repeat_bootstrap_opens_no_transaction(wiring):
    """The advisory read is the idempotency path, and that is observable.

    Removing it leaves the OUTCOME identical — the transaction collides and the
    service converges on the same caregiver — so no state assertion can tell
    the difference. What differs is the cost: a guaranteed failed transaction
    on every app launch. Asserted by counting transactions, which is why this
    test exists at all rather than the claim living only in a docstring.
    """
    wiring.service.bootstrap_caregiver(NEWCOMER)

    opened = []
    real = wiring.repos.store.run_in_transaction

    def counting(fn):
        opened.append(fn)
        return real(fn)

    wiring.repos.store.run_in_transaction = counting
    try:
        again = wiring.service.bootstrap_caregiver(NEWCOMER)
    finally:
        wiring.repos.store.run_in_transaction = real

    assert again.caregiver_id
    assert opened == [], f"{len(opened)} transactions on a repeat bootstrap"


def test_a_repeat_legacy_bootstrap_attempts_no_claim_write(wiring):
    """Same property on the legacy path: the second pass is reads only."""
    seed_legacy_caregiver(wiring.repos)
    wiring.service.bootstrap_caregiver(LEGACY)

    attempts = []
    repo = wiring.repos.auth_subject_claims
    real = repo.claim

    def counting(claim):
        attempts.append(claim)
        return real(claim)

    repo.claim = counting
    try:
        wiring.service.bootstrap_caregiver(LEGACY)
    finally:
        repo.claim = real
    assert attempts == []


def test_bootstrap_resolves_to_a_working_principal(wiring):
    """The whole point: after bootstrap the subject authenticates."""
    caregiver = wiring.service.bootstrap_caregiver(NEWCOMER)
    principal = principal_for(wiring.repos, NEWCOMER)
    assert principal.role is ActorRole.CAREGIVER
    assert principal.application_id == caregiver.caregiver_id


def test_two_distinct_subjects_get_distinct_identities(wiring):
    one = wiring.service.bootstrap_caregiver(NEWCOMER)
    two = wiring.service.bootstrap_caregiver(NEWCOMER_TWO)
    assert one.caregiver_id != two.caregiver_id
    assert len(claim_docs(wiring.repos)) == 2


def test_bootstrap_refuses_an_empty_subject(wiring):
    for value in ("", "   "):
        with pytest.raises(AuthIdentityError):
            wiring.service.bootstrap_caregiver(value)
    assert claim_docs(wiring.repos) == []


def test_bootstrap_refuses_a_subject_held_by_a_provider_claim(wiring):
    """§5: one person, one app identity."""
    wiring.repos.auth_subject_claims.claim(AuthSubjectIdentityClaim.build(
        NEWCOMER, holder_actor_id="prov_fictional",
        holder_actor_type=ActorRole.PROVIDER, now=T0))
    with pytest.raises(SubjectAlreadyHeld):
        wiring.service.bootstrap_caregiver(NEWCOMER)
    assert wiring.repos.caregivers.get_by_auth_subject(NEWCOMER) is None


def test_bootstrap_refuses_a_subject_already_held_by_a_provider_record(wiring):
    """A PRE-PROVISIONED provider has no claim, and must still be refused.

    Nothing mints provider claims yet, so the claim check above cannot see any
    provider in the system today. Without this, bootstrap would create a
    caregiver and leave the subject resolving to BOTH records — which
    `resolve_principal` refuses forever, with no release path. Found by
    implementation, not by the contract.
    """
    with pytest.raises(SubjectAlreadyHeld):
        wiring.service.bootstrap_caregiver(PROVIDER_ALPHA_SUBJECT)
    assert wiring.repos.caregivers.get_by_auth_subject(
        PROVIDER_ALPHA_SUBJECT) is None
    assert claim_docs(wiring.repos) == []
    # The provider still resolves, untouched.
    assert principal_for(wiring.repos, PROVIDER_ALPHA_SUBJECT).role is (
        ActorRole.PROVIDER)


def test_bootstrapping_an_existing_caregiver_subject_returns_that_caregiver(wiring):
    """The topology's caregivers predate the claim — the legacy path."""
    caregiver = wiring.service.bootstrap_caregiver(CAREGIVER_ALPHA_SUBJECT)
    assert caregiver.caregiver_id == wiring.topo.caregiver_alpha.caregiver_id


# ===========================================================================
# the legacy backfill — five pinned outcomes
# ===========================================================================

def test_legacy_caregiver_gains_a_claim_and_no_twin(wiring):
    """§1: exactly one existing caregiver, no claim -> claim it, do not mint."""
    legacy = seed_legacy_caregiver(wiring.repos)
    before = len(wiring.repos.store.list_all("pilot_caregivers"))

    got = wiring.service.bootstrap_caregiver(LEGACY)

    assert got.caregiver_id == legacy.caregiver_id
    assert len(wiring.repos.store.list_all("pilot_caregivers")) == before
    claim = wiring.repos.auth_subject_claims.find_for_subject(LEGACY)
    assert claim.holder_actor_id == legacy.caregiver_id
    assert claim.is_held_by_caregiver


def test_legacy_backfill_does_not_rewrite_the_caregiver(wiring):
    """The existing record is claimed, not edited."""
    legacy = seed_legacy_caregiver(wiring.repos)
    before = wiring.repos.store.get("pilot_caregivers", legacy.caregiver_id)
    wiring.service.bootstrap_caregiver(LEGACY)
    assert wiring.repos.store.get(
        "pilot_caregivers", legacy.caregiver_id) == before


def test_legacy_backfill_is_create_only_and_idempotent(wiring):
    """§2: a second pass neither rewrites the claim nor adds another."""
    seed_legacy_caregiver(wiring.repos)
    wiring.service.bootstrap_caregiver(LEGACY)
    stored = claim_docs(wiring.repos)
    wiring.service.bootstrap_caregiver(LEGACY)
    wiring.service.bootstrap_caregiver(LEGACY)
    assert claim_docs(wiring.repos) == stored
    assert len(stored) == 1


def test_legacy_backfill_protects_the_subject_for_the_next_writer(wiring):
    """§2: after the backfill the claim IS the guard.

    A later writer attempting the same subject collides on the claim document
    rather than succeeding because a query happened to see nothing.
    """
    from pilot_backend.repository.interface import DuplicateRecord

    legacy = seed_legacy_caregiver(wiring.repos)
    wiring.service.bootstrap_caregiver(LEGACY)
    with pytest.raises((DuplicateRecord, Exception)) as caught:
        wiring.repos.auth_subject_claims.claim(AuthSubjectIdentityClaim.build(
            LEGACY, holder_actor_id="cgvr_interloper",
            holder_actor_type=ActorRole.CAREGIVER, now=T0))
    assert caught.value is not None
    assert wiring.repos.auth_subject_claims.find_for_subject(
        LEGACY).holder_actor_id == legacy.caregiver_id


def test_concurrent_legacy_backfills_converge_on_one_caregiver(wiring):
    """§3: two callers, one claim, the same caregiver_id.

    In-process only — `FakeDocumentStore` is a plain dict. The CONTENTION claim
    is made against the real emulator; what this asserts is that the service's
    collision branch converges rather than raising.
    """
    legacy = seed_legacy_caregiver(wiring.repos)
    barrier = threading.Barrier(2)
    results, errors = [], []
    lock = threading.Lock()

    def runner() -> None:
        barrier.wait()
        try:
            got = wiring.service.bootstrap_caregiver(LEGACY)
            with lock:
                results.append(got.caregiver_id)
        except Exception as exc:  # noqa: BLE001 - classified below
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=runner) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "the harness stalled; not a uniqueness result"

    assert errors == []
    assert len(results) == 2
    assert set(results) == {legacy.caregiver_id}
    assert len(claim_docs(wiring.repos)) == 1
    assert len(wiring.repos.store.list_all("pilot_caregivers")) == len(
        wiring.repos.store.list_all("pilot_caregivers"))


def test_multiple_legacy_caregivers_fail_closed_with_no_claim(wiring):
    """§4: ambiguity is refused and NOT repaired.

    Choosing which record is real decides whose data a person sees. A bootstrap
    endpoint is the wrong place to make that call silently, so it is made
    nowhere.
    """
    first = seed_legacy_caregiver(wiring.repos, name="Legacy-One")
    second = seed_legacy_caregiver(wiring.repos, name="Legacy-Two")
    assert first.caregiver_id != second.caregiver_id

    with pytest.raises(AmbiguousSubjectState):
        wiring.service.bootstrap_caregiver(LEGACY)

    assert claim_docs(wiring.repos) == []
    # Neither record was deleted, merged or rewritten.
    assert wiring.repos.caregivers.get_by_id(first.caregiver_id) == first
    assert wiring.repos.caregivers.get_by_id(second.caregiver_id) == second


def test_ambiguity_is_refused_before_any_write_even_with_a_claim_present(wiring):
    """Ambiguity is checked FIRST, so a stray claim cannot mask it."""
    first = seed_legacy_caregiver(wiring.repos, name="Legacy-One")
    seed_legacy_caregiver(wiring.repos, name="Legacy-Two")
    wiring.repos.auth_subject_claims.claim(AuthSubjectIdentityClaim.build(
        LEGACY, holder_actor_id=first.caregiver_id,
        holder_actor_type=ActorRole.CAREGIVER, now=T0))
    with pytest.raises(AmbiguousSubjectState):
        wiring.service.bootstrap_caregiver(LEGACY)


def test_legacy_caregiver_with_a_provider_held_claim_is_refused(wiring):
    """§5, on the legacy path: the provider claim wins and nothing is created."""
    legacy = seed_legacy_caregiver(wiring.repos)
    wiring.repos.auth_subject_claims.claim(AuthSubjectIdentityClaim.build(
        LEGACY, holder_actor_id="prov_fictional",
        holder_actor_type=ActorRole.PROVIDER, now=T0))
    with pytest.raises(SubjectAlreadyHeld):
        wiring.service.bootstrap_caregiver(LEGACY)
    # The provider claim is NOT transferred to the legacy caregiver.
    assert wiring.repos.auth_subject_claims.find_for_subject(
        LEGACY).holder_actor_id == "prov_fictional"
    assert wiring.repos.caregivers.get_by_id(legacy.caregiver_id) == legacy


def test_claim_naming_a_different_caregiver_fails_closed(wiring):
    """§6: never overwrite, transfer or repair.

    The claim names caregiver A while the subject resolves to caregiver B. One
    of the two records is wrong and the code cannot tell which, so it refuses
    and leaves both exactly as they are.
    """
    legacy = seed_legacy_caregiver(wiring.repos)
    other = wiring.repos.caregivers.create(Caregiver.create(
        "Other-Caregiver", auth_subject="fictional-subject-unrelated", now=T0))
    wiring.repos.auth_subject_claims.claim(AuthSubjectIdentityClaim.build(
        LEGACY, holder_actor_id=other.caregiver_id,
        holder_actor_type=ActorRole.CAREGIVER, now=T0))

    with pytest.raises(AmbiguousSubjectState):
        wiring.service.bootstrap_caregiver(LEGACY)

    held = wiring.repos.auth_subject_claims.find_for_subject(LEGACY)
    assert held.holder_actor_id == other.caregiver_id, "claim was transferred"
    assert wiring.repos.caregivers.get_by_id(legacy.caregiver_id) == legacy
    assert wiring.repos.caregivers.get_by_id(other.caregiver_id) == other
    assert len(claim_docs(wiring.repos)) == 1


def test_claim_naming_a_caregiver_that_does_not_exist_fails_closed(wiring):
    """A stranded claim is reported, never worked around by minting a twin."""
    wiring.repos.auth_subject_claims.claim(AuthSubjectIdentityClaim.build(
        NEWCOMER, holder_actor_id="cgvr_does_not_exist",
        holder_actor_type=ActorRole.CAREGIVER, now=T0))
    with pytest.raises(AmbiguousSubjectState):
        wiring.service.bootstrap_caregiver(NEWCOMER)
    assert wiring.repos.caregivers.get_by_auth_subject(NEWCOMER) is None


def test_a_backfill_collision_naming_another_caregiver_fails_closed(wiring):
    """§3, last outcome: a backfill collision is RESOLVED, never swallowed.

    `_backfill_claim`'s collision branch re-reads the winning claim instead of
    assuming it agrees with the caregiver it was about to protect.
    `test_concurrent_legacy_backfills_converge_on_one_caregiver` pins the
    ordinary case, where the winner names the same legacy caregiver and both
    callers return it. This pins the other one: a winner naming SOMEONE ELSE is
    inconsistent, and `return caregiver` there would silently decide whose data
    this person sees — the single outcome the whole convergence helper exists to
    refuse.

    Only the TIMING is injected. `FakeDocumentStore` is a plain dict, so the
    window between the advisory read and the claim write cannot be hit
    reliably in-process; the branch, the re-read and the refusal are the real
    ones, and the CONTENTION claim is made against the emulator.
    """
    from pilot_backend.repository.interface import DuplicateRecord

    legacy = seed_legacy_caregiver(wiring.repos)
    interloper = AuthSubjectIdentityClaim.build(
        LEGACY, holder_actor_id="cgvr_someone_entirely_else",
        holder_actor_type=ActorRole.CAREGIVER, now=T0)
    real = wiring.repos.auth_subject_claims

    class _LostToAnotherHolder:
        """Unclaimed at the advisory read; held by a stranger by write time."""

        def __init__(self) -> None:
            self.reads = 0

        def find_for_subject(self, auth_subject):
            self.reads += 1
            # Read 1 is the service's advisory idempotency check, which must
            # see nothing for the legacy backfill path to be entered at all.
            return None if self.reads == 1 else interloper

        def claim(self, claim):
            raise DuplicateRecord("another writer won this subject first")

        def __getattr__(self, name):
            return getattr(real, name)

    wiring.repos.auth_subject_claims = _LostToAnotherHolder()

    with pytest.raises(AmbiguousSubjectState):
        wiring.service.bootstrap_caregiver(LEGACY)

    # The re-read actually happened — this is the collision branch, not the
    # advisory-read refusal that `test_claim_naming_a_different_caregiver_
    # fails_closed` already covers.
    assert wiring.repos.auth_subject_claims.reads == 2
    # And the legacy caregiver was neither rewritten nor twinned.
    assert wiring.repos.caregivers.get_by_id(legacy.caregiver_id) == legacy
    assert [doc_id for doc_id, _ in
            wiring.repos.store.list_all("pilot_caregivers")].count(
                legacy.caregiver_id) == 1
    mismatch = [doc for doc in audit_docs(wiring.repos)
                if doc.get("metadata", {}).get(
                    "integration_state") == "CLAIM_HOLDER_MISMATCH"]
    assert len(mismatch) == 1


def test_every_bootstrap_refusal_leaves_the_store_unchanged(wiring):
    """One assertion covering all four refusal shapes."""
    scenarios = []

    def snapshot(repos):
        return {name: sorted(doc_id for doc_id, _ in repos.store.list_all(name))
                for name in repos.store.collections()
                if name != "pilot_audit_events"}

    # provider-held claim
    wiring.repos.auth_subject_claims.claim(AuthSubjectIdentityClaim.build(
        NEWCOMER, holder_actor_id="prov_fictional",
        holder_actor_type=ActorRole.PROVIDER, now=T0))
    scenarios.append((NEWCOMER, SubjectAlreadyHeld))
    # pre-provisioned provider record
    scenarios.append((PROVIDER_ALPHA_SUBJECT, SubjectAlreadyHeld))
    # two legacy caregivers
    seed_legacy_caregiver(wiring.repos, name="Legacy-One")
    seed_legacy_caregiver(wiring.repos, name="Legacy-Two")
    scenarios.append((LEGACY, AmbiguousSubjectState))

    for subject, expected in scenarios:
        before = snapshot(wiring.repos)
        with pytest.raises(expected):
            wiring.service.bootstrap_caregiver(subject)
        assert snapshot(wiring.repos) == before, subject


# ===========================================================================
# the LIMIT of what the claim closes — a pinned PRE-PHI gap
#
# These tests assert the CURRENT, deliberately-unfixed state. They are not
# describing desired behaviour: they exist so the open blocker in SECURITY.md
# cannot drift out of agreement with the code, in either direction. Closing the
# gap MUST break them, which forces whoever closes it to update the blocker
# list in the same change.
# ===========================================================================

def test_provider_provisioning_acquires_the_claim_atomically(wiring):
    """0.5B, CLOSED: the provider path now takes the same claim.

    This test is the inverse of the 0.5A one it replaces, which asserted that
    `providers.create` minted no claim. That assertion was the pinned form of
    PRE-PHI Blocker 4 and it failing is the intended consequence of closing it.

    Both documents must exist. A claim without a provider would strand the
    subject with no release path; a provider without a claim would leave the
    subject unprotected for the next writer — which was the whole defect.
    """
    from pilot_backend.domain.enums import ProviderDiscipline
    from pilot_backend.provisioning import provision_provider_record

    subject = "fictional-subject-provider-provisioned"
    outcome = provision_provider_record(
        wiring.repos, auth_subject=subject,
        practice_id=wiring.topo.practice.practice_id,
        discipline=ProviderDiscipline.SLP, display_name="Provider-Hannah",
        now=T0)

    assert outcome.created is True
    assert outcome.claim_backfilled is False
    claim = wiring.repos.auth_subject_claims.find_for_subject(subject)
    assert claim is not None, "provisioning did not acquire a subject claim"
    assert claim.holder_actor_id == outcome.provider.provider_id
    assert claim.is_held_by_provider is True
    assert claim.subject_fingerprint == subject_fingerprint(subject)
    # The record is really there, and resolves to a provider principal.
    stored = wiring.repos.providers.get_by_id(outcome.provider.provider_id)
    assert stored.auth_subject == subject
    assert principal_for(wiring.repos, subject).role is ActorRole.PROVIDER


def test_provider_provisioning_refuses_a_caregiver_held_subject(wiring):
    """0.5B, CLOSED: the reverse ordering is now guarded too.

    0.5A guarded caregiver-after-provider and left provider-after-caregiver
    open, which is what kept Blocker 4 open: `providers.create` did not read the
    claim, so it could bind a subject a caregiver had already won, and
    `resolve_principal` then refused that subject permanently with no release
    path.

    Provisioning now refuses, and — the part that matters — NOTHING is written,
    so the subject stays resolvable as the caregiver it already was.
    """
    from pilot_backend.domain.enums import ProviderDiscipline
    from pilot_backend.provisioning import provision_provider_record

    caregiver = wiring.service.bootstrap_caregiver(NEWCOMER)
    before = snapshot_all(wiring.repos)

    with pytest.raises(SubjectAlreadyHeld):
        provision_provider_record(
            wiring.repos, auth_subject=NEWCOMER,
            practice_id=wiring.topo.practice.practice_id,
            discipline=ProviderDiscipline.SLP,
            display_name="Provider-Intruder", now=T0)

    assert snapshot_all(wiring.repos) == before, "the refusal wrote something"
    # Still exactly the caregiver's claim, and the subject still authenticates.
    assert wiring.repos.auth_subject_claims.find_for_subject(
        NEWCOMER).holder_actor_id == caregiver.caregiver_id
    assert principal_for(wiring.repos, NEWCOMER).role is ActorRole.CAREGIVER


def test_provisioning_refuses_a_subject_held_by_a_CLAIMLESS_caregiver(wiring):
    """The caregiver check is reached even when no claim exists to catch it.

    Found by mutation testing. Removing the `caregiver is not None` guard
    survived every test, because the tests all used `bootstrap_caregiver`,
    which mints a claim — so the LATER `claim.is_held_by_caregiver` branch
    refused and the first check was never the thing doing the work.

    A legacy caregiver has no claim: fixtures and pre-0.5A admin provisioning
    created them before the primitive existed. For that subject the first
    check is the ONLY guard, and without it provisioning would mint a provider
    alongside the caregiver — leaving `resolve_principal` refusing the subject
    forever, which is the exact unrecoverable state blocker 4 exists to stop.
    """
    from pilot_backend.domain.enums import ProviderDiscipline
    from pilot_backend.provisioning import provision_provider_record

    legacy = seed_legacy_caregiver(wiring.repos, subject=LEGACY)
    assert wiring.repos.auth_subject_claims.find_for_subject(LEGACY) is None, \
        "this test is only meaningful while the caregiver holds no claim"
    before = snapshot_all(wiring.repos)

    with pytest.raises(SubjectAlreadyHeld):
        provision_provider_record(
            wiring.repos, auth_subject=LEGACY,
            practice_id=wiring.topo.practice.practice_id,
            discipline=ProviderDiscipline.SLP,
            display_name="Provider-Intruder", now=T0)

    assert snapshot_all(wiring.repos) == before, "the refusal wrote something"
    # The subject still authenticates as the caregiver it always was.
    assert principal_for(wiring.repos, LEGACY).application_id == \
        legacy.caregiver_id


def test_provisioning_refuses_a_CLAIM_held_by_a_caregiver_with_no_record(wiring):
    """The claim check is reached even when no caregiver record catches it.

    The mirror of `test_provisioning_refuses_a_subject_held_by_a_CLAIMLESS_
    caregiver`, and found the same way. The two guards are redundant for the
    ordinary case — a bootstrapped caregiver has BOTH a record and a claim —
    so each one survived mutation while the other covered for it.

    This is the state only the claim guard sees: a caregiver-held claim whose
    subject resolves to no caregiver record. Reachable whenever a claim
    outlives the record it named, and the claim repository is create-only so
    nothing can clear it.
    """
    from pilot_backend.domain.enums import ProviderDiscipline
    from pilot_backend.provisioning import provision_provider_record

    subject = "fictional-subject-claim-without-record"
    wiring.repos.auth_subject_claims.claim(AuthSubjectIdentityClaim.build(
        subject, holder_actor_id="cgvr_no_such_record",
        holder_actor_type=ActorRole.CAREGIVER, now=T0))
    assert wiring.repos.caregivers.get_by_auth_subject(subject) is None, \
        "this test is only meaningful while no caregiver record resolves"

    with pytest.raises(SubjectAlreadyHeld):
        provision_provider_record(
            wiring.repos, auth_subject=subject,
            practice_id=wiring.topo.practice.practice_id,
            discipline=ProviderDiscipline.SLP,
            display_name="Provider-Intruder", now=T0)


def test_provisioning_backfills_a_legacy_provider_instead_of_twinning(wiring):
    """A fixture-created provider gains a claim; it is never duplicated.

    Found by mutation testing. Fixture providers are created directly through
    the repository — `secure_topology` is backend-parametrised and the
    in-memory set has no claim collection — so a subject can resolve to
    exactly one provider with NO claim behind it. Provisioning that subject
    must adopt the existing record, and dropping the backfill branch instead
    minted a SECOND provider on the same subject, which is the twin that makes
    `get_by_auth_subject` refuse forever.
    """
    from pilot_backend.domain.enums import ProviderDiscipline
    from pilot_backend.provisioning import provision_provider_record

    legacy = wiring.topo.provider_alpha
    assert wiring.repos.auth_subject_claims.find_for_subject(
        PROVIDER_ALPHA_SUBJECT) is None, "fixture providers hold no claim"

    outcome = provision_provider_record(
        wiring.repos, auth_subject=PROVIDER_ALPHA_SUBJECT,
        practice_id=legacy.practice_id,
        discipline=ProviderDiscipline.SLP, display_name="Provider-Alpha",
        now=T0)

    assert outcome.provider.provider_id == legacy.provider_id, "a twin was minted"
    assert outcome.created is False
    assert outcome.claim_backfilled is True
    # Exactly one provider for the subject, and it now holds the claim.
    matching = [p for p in wiring.repos.providers.list_by_practice(
        legacy.practice_id) if p.auth_subject == PROVIDER_ALPHA_SUBJECT]
    assert len(matching) == 1
    assert wiring.repos.auth_subject_claims.find_for_subject(
        PROVIDER_ALPHA_SUBJECT).holder_actor_id == legacy.provider_id


def test_provisioning_refuses_an_inactive_practice(wiring):
    """A provider is only meaningful inside a live practice.

    `ProviderChildConnection` denormalises `practice_id` and
    `ManagingClinicianAssignment` copies it again, so admitting a retired
    practice here would put an unresolvable reference into both.
    """
    from pilot_backend.domain.entities import Practice
    from pilot_backend.domain.enums import EntityStatus, ProviderDiscipline
    from pilot_backend.provisioning import provision_provider_record
    from pilot_backend.provisioning.errors import ProviderProvisioningError

    retired = wiring.repos.practices.create(
        Practice.create("Practice-Retired", now=T0))
    wiring.repos.practices.update_status(
        retired.practice_id, EntityStatus.ARCHIVED, now=T0)
    before = snapshot_all(wiring.repos)

    with pytest.raises(ProviderProvisioningError):
        provision_provider_record(
            wiring.repos, auth_subject="fictional-subject-retired-practice",
            practice_id=retired.practice_id,
            discipline=ProviderDiscipline.SLP, display_name="Provider-Nowhere",
            now=T0)
    assert snapshot_all(wiring.repos) == before

    # And an absent practice is refused the same way.
    with pytest.raises(ProviderProvisioningError):
        provision_provider_record(
            wiring.repos, auth_subject="fictional-subject-absent-practice",
            practice_id="prac_does_not_exist",
            discipline=ProviderDiscipline.SLP, display_name="Provider-Nowhere",
            now=T0)


def test_a_provider_claim_naming_a_different_provider_fails_closed(wiring):
    """A claim and a record that disagree are never reconciled by guessing.

    Found by mutation testing: dropping this check survived, because no test
    had built the inconsistent state. It is reachable — a claim written for
    one provider while the subject resolves to another — and either record
    could be the wrong one, so choosing would silently decide whose caseload a
    clinician sees.
    """
    from pilot_backend.domain.enums import ProviderDiscipline
    from pilot_backend.provisioning import provision_provider_record

    subject = "fictional-subject-provider-mismatch"
    # A provider record the subject resolves to...
    resolved = provision_provider_record(
        wiring.repos, auth_subject=subject,
        practice_id=wiring.topo.practice.practice_id,
        discipline=ProviderDiscipline.SLP, display_name="Provider-Resolved",
        now=T0).provider

    # ...and a claim for that subject naming a DIFFERENT, REAL provider in the
    # SAME practice.
    #
    # Both details are load-bearing, and getting them wrong is how an earlier
    # version of this test passed against the defect it exists to catch. If
    # the claim names a provider that does not exist, the mutated code falls
    # through to `get_by_id`, hits `RecordNotFound`, and raises
    # `AmbiguousSubjectState` from the NEXT branch — the same exception, so
    # `pytest.raises` could not tell the two apart. If the named provider is
    # in a different practice, the practice check raises instead, for a third
    # unrelated reason.
    #
    # Naming a real provider in the same practice removes every fallback: only
    # the holder-mismatch branch can refuse, so disabling it means the call
    # SUCCEEDS and returns somebody else's provider.
    other = provision_provider_record(
        wiring.repos, auth_subject="fictional-subject-provider-other",
        practice_id=wiring.topo.practice.practice_id,
        discipline=ProviderDiscipline.SLP, display_name="Provider-Other",
        now=T0).provider
    assert other.provider_id != resolved.provider_id
    assert other.practice_id == resolved.practice_id

    class _ClaimNamingAnother:
        def __init__(self, real) -> None:
            self._real = real
            self._interloper = AuthSubjectIdentityClaim.build(
                subject, holder_actor_id=other.provider_id,
                holder_actor_type=ActorRole.PROVIDER, now=T0)

        def find_for_subject(self, auth_subject):
            return self._interloper

        def __getattr__(self, name):
            return getattr(self._real, name)

    wiring.repos.auth_subject_claims = _ClaimNamingAnother(
        wiring.repos.auth_subject_claims)

    with pytest.raises(AmbiguousSubjectState):
        provision_provider_record(
            wiring.repos, auth_subject=subject,
            practice_id=wiring.topo.practice.practice_id,
            discipline=ProviderDiscipline.SLP,
            display_name="Provider-Resolved", now=T0)

    # Neither record was rewritten, merged or repaired.
    assert wiring.repos.providers.get_by_id(
        resolved.provider_id).auth_subject == subject


def test_two_provider_provisions_of_one_subject_produce_no_twin(wiring):
    """0.5B, CLOSED: a repeat provision converges instead of twinning.

    The defect being closed was two Provider records sharing one subject, which
    `get_by_auth_subject` then refuses forever. A second provision of the same
    subject must return the SAME provider and write nothing new.
    """
    from pilot_backend.domain.enums import ProviderDiscipline
    from pilot_backend.provisioning import provision_provider_record

    subject = "fictional-subject-provider-repeat"

    def _provision():
        return provision_provider_record(
            wiring.repos, auth_subject=subject,
            practice_id=wiring.topo.practice.practice_id,
            discipline=ProviderDiscipline.SLP, display_name="Provider-Repeat",
            now=T0)

    first = _provision()
    after_first = snapshot_all(wiring.repos)
    second = _provision()

    assert second.provider.provider_id == first.provider.provider_id
    assert second.created is False
    assert snapshot_all(wiring.repos) == after_first, "the retry wrote again"
    # And the subject is still unambiguous, which is the property at stake.
    assert wiring.repos.providers.get_by_auth_subject(
        subject).provider_id == first.provider.provider_id


def test_no_repository_method_persists_a_late_subject_binding():
    """`Provider.with_auth_subject` is currently inert, and that is load-bearing.

    The domain object can produce a provider with a subject attached after
    creation, but no repository method writes one — `update_status` is the only
    mutation. So late binding is a LATENT path, not a live one. If a setter is
    ever added it must acquire the claim, and this test is what makes that
    addition visible.
    """
    repos = FirestoreRepositories(FakeDocumentStore())
    operations = {name for name in dir(repos.providers)
                  if not name.startswith("_") and name != "model"
                  and callable(getattr(repos.providers, name))}
    assert operations == {"create", "get_by_id", "get_by_auth_subject",
                          "list_by_practice", "update_status"}, operations


def test_the_subject_claim_is_written_from_exactly_the_two_identity_paths():
    """The SUBJECT claim repository has exactly two writers, one per actor kind.

    0.5A asserted ONE writer, because only the caregiver path took the claim.
    0.5B adds the provider path, and that is the widening that closes Blocker 4
    — so this is the expected new value, not a bypass.

    A THIRD writer appearing is what this guards. It would mean either another
    legitimate actor kind (which needs its own review) or a service minting
    claims outside the two audited provisioning paths.

    Matched on the receiver `auth_subject_claims`, not on the bare method name
    `claim`: several prior-slice services call `identity_claims.claim(...)`,
    which is the CHILD-scoped 0.4A mutex — a different primitive that happens
    to share a verb. A guard keyed on the verb alone would flag all of them.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    writers = set()
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and node.attr == "claim"):
                continue
            receiver = node.value
            if (isinstance(receiver, ast.Attribute)
                    and receiver.attr == "auth_subject_claims"):
                writers.add(path.relative_to(root).as_posix())
    assert writers == {"integration/identity_service.py",
                       "provisioning/service.py"}, writers


def test_provider_creation_happens_only_inside_provisioning_or_fixtures():
    """THE gate that actually closes Blocker 4.

    A deterministic claim is only a mutex for writers that TAKE it, so the
    guarantee is not "provisioning acquires the claim" — it is "nothing creates
    a Provider except provisioning". A future service calling
    `repos.providers.create` directly would silently reopen the blocker, and no
    behavioural test would notice, because the provider it created would work
    perfectly right up until a second one appeared on the same subject.

    Two fixture call sites are allowlisted, and the exemption is bounded:

      * `fixtures/secure_topology.py` is backend-parametrised and also builds
        against frozen `InMemoryRepositories`, which has no claim collection
        and no transactions;
      * `fixtures/pilot_topology.py` is the BACKEND 0.1 in-memory demo
        topology.

    Both are test-only, and the companion test below asserts that nothing
    reachable from the composition root imports `pilot_backend.fixtures` — so
    neither is a path the deployed pilot can take. A fixture-seeded provider is
    claim-free until provisioning touches its subject, at which point the
    legacy-backfill branch mints the claim.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    creators = set()
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "create"):
                continue
            receiver = node.func.value
            if (isinstance(receiver, ast.Attribute)
                    and receiver.attr == "providers"):
                creators.add(path.relative_to(root).as_posix())

    assert creators == {
        "provisioning/service.py",
        "fixtures/secure_topology.py",
        "fixtures/pilot_topology.py",
    }, creators


def test_the_deployed_composition_cannot_reach_the_fixtures():
    """What bounds the fixture exemption above.

    If any module reachable from the composition root imported
    `pilot_backend.fixtures`, the allowlist in the previous test would stop
    being a test-only carve-out and become a live provider-creation path that
    bypasses the claim.

    Walks the import graph from the composition root rather than checking the
    root alone: a two-hop import would be just as reachable and far easier to
    add by accident.
    """
    import ast
    import pathlib

    backend = pathlib.Path(__file__).resolve().parent.parent
    runtime = backend.parent / "pilot_runtime"

    def _module_path(dotted: str):
        for base, prefix in ((backend, "pilot_backend."), (runtime, "pilot_runtime.")):
            if not dotted.startswith(prefix):
                continue
            rel = dotted[len(prefix):].replace(".", "/")
            for candidate in (base / f"{rel}.py", base / rel / "__init__.py"):
                if candidate.exists():
                    return candidate
        return None

    def _imports(path: pathlib.Path, dotted: str):
        """Absolute and relative imports, normalised to dotted module names."""
        package = dotted.rsplit(".", 1)[0] if "." in dotted else dotted
        found = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    if node.module:
                        found.add(node.module)
                    continue
                # `from ..x import y` — climb `level - 1` packages.
                parts = package.split(".")
                base = parts[:len(parts) - (node.level - 1)] or parts[:1]
                found.add(".".join(base + ([node.module] if node.module else [])))
        return found

    root = "pilot_runtime.composition"
    seen, queue, offenders = set(), [root], []
    while queue:
        dotted = queue.pop()
        if dotted in seen:
            continue
        seen.add(dotted)
        path = _module_path(dotted)
        if path is None:
            continue
        for imported in _imports(path, dotted):
            if imported.startswith("pilot_backend.fixtures"):
                offenders.append((dotted, imported))
            if imported.startswith(("pilot_backend", "pilot_runtime")):
                queue.append(imported)

    assert not offenders, (
        f"the deployed composition can reach the fixtures: {offenders}")
    # Sanity: the walk actually traversed something, so an empty result is a
    # real absence rather than a graph that never got started.
    assert len(seen) > 5, f"import walk visited too little to be meaningful: {seen}"


# ===========================================================================
# Parent session -> canonical child
# ===========================================================================

@pytest.fixture()
def linked(wiring):
    """A bootstrapped caregiver with an owned, unlinked Parent session."""
    caregiver = wiring.service.bootstrap_caregiver(NEWCOMER)
    wiring.parent.add(SESSION_ONE, NEWCOMER)
    wiring.principal = principal_for(wiring.repos, NEWCOMER)
    wiring.caregiver = caregiver
    return wiring


def test_link_creates_child_connection_and_source_link(linked):
    result = linked.service.link_parent_session(linked.principal, SESSION_ONE)
    assert result.created is True
    child = linked.repos.children.get_by_id(result.child_id)
    assert child.child_id == result.child_id
    link = linked.repos.source_links.get_by_id(result.source_link_id)
    assert link.source_system is SourceSystem.PARENT
    assert link.external_id == SESSION_ONE
    assert link.child_id == result.child_id
    connection = linked.repos.caregiver_child.get_by_id(result.connection_id)
    assert connection.caregiver_id == linked.caregiver.caregiver_id
    assert connection.status is ConnectionStatus.ACTIVE


def test_both_identity_claims_are_committed_not_merely_referenced(linked):
    """The child-source claim is WRITTEN, not just built and pointed at.

    `SourceSystemLink` carries both claim ids, and they are filled in from the
    claim OBJECTS before the link is persisted. So a bridge that builds a claim
    and never commits it still looks correct from every angle the other tests
    see: the field is populated, the external-identity claim alone keeps the
    session unique, and the child id is freshly minted so nothing collides.

    What goes missing is the per-child invariant — at most one source link per
    (child, source system) — which is the claim this slice reuses from 0.4A
    unchanged. It is held by the document or it is not held at all, so the
    document is what gets asserted.
    """
    from pilot_backend.domain.identity_claims import ClaimKind, key_digest

    result = linked.service.link_parent_session(linked.principal, SESSION_ONE)
    link = linked.repos.source_links.get_by_id(result.source_link_id)

    for field_name, kind, parts in (
            ("child_source_claim_id", ClaimKind.CHILD_SOURCE,
             (result.child_id, SourceSystem.PARENT.value)),
            ("external_identity_claim_id", ClaimKind.EXTERNAL_IDENTITY,
             (SourceSystem.PARENT.value, SESSION_ONE))):
        claim_id = getattr(link, field_name)
        assert claim_id, f"{field_name} was not populated"
        assert linked.repos.identity_claims.exists(claim_id), (
            f"{field_name} is referenced by the link but no claim document "
            f"was committed")
        claim = linked.repos.identity_claims.get_by_id(claim_id)
        assert claim.kind is kind
        assert claim.child_id == result.child_id
        assert claim.key_digest == key_digest(*parts)
        assert claim.holder_ref == link.link_id


def test_the_session_id_never_becomes_the_child_id(linked):
    """Provenance is a source link, not an identity."""
    result = linked.service.link_parent_session(linked.principal, SESSION_ONE)
    assert result.child_id != SESSION_ONE
    assert SESSION_ONE not in result.child_id
    assert result.child_id.startswith("chld_")


def test_the_canonical_child_carries_no_clinical_field(linked):
    """A `Child` holds provenance and nothing else — the bridge adds nothing."""
    result = linked.service.link_parent_session(linked.principal, SESSION_ONE)
    document = linked.repos.store.get("pilot_children", result.child_id)
    for banned in ("name", "child_name", "age", "age_months", "birth_date",
                   "diagnosis", "concern", "notes", "note", "plan",
                   "answers", "session_id", "owner_uid"):
        assert banned not in document, banned


def test_linking_is_idempotent_for_the_same_session(linked):
    first = linked.service.link_parent_session(linked.principal, SESSION_ONE)
    second = linked.service.link_parent_session(linked.principal, SESSION_ONE)
    assert second.created is False
    assert second.child_id == first.child_id
    assert second.source_link_id == first.source_link_id
    assert len(linked.repos.store.list_all("pilot_children")) == len(
        [c for c in linked.repos.store.list_all("pilot_children")])


def test_a_retry_creates_no_second_child(linked):
    linked.service.link_parent_session(linked.principal, SESSION_ONE)
    counts = {name: len(linked.repos.store.list_all(name))
              for name in linked.repos.store.collections()
              if name != "pilot_audit_events"}
    linked.service.link_parent_session(linked.principal, SESSION_ONE)
    assert {name: len(linked.repos.store.list_all(name))
            for name in linked.repos.store.collections()
            if name != "pilot_audit_events"} == counts


def test_an_unknown_session_is_refused(linked):
    with pytest.raises(ParentSessionUnavailable):
        linked.service.link_parent_session(linked.principal, "fictional-absent")


def test_a_session_owned_by_someone_else_is_refused(linked):
    """Ownership is proven against the VERIFIED subject, not a request field."""
    linked.parent.add(SESSION_TWO, "fictional-subject-somebody-else")
    with pytest.raises(ParentSessionUnavailable):
        linked.service.link_parent_session(linked.principal, SESSION_TWO)
    assert linked.repos.source_links.list_for_external_id(SESSION_TWO) == []


def test_absent_and_not_owned_are_indistinguishable(linked):
    """§ the non-enumerating rule: no oracle for which session ids are real."""
    linked.parent.add(SESSION_TWO, "fictional-subject-somebody-else")
    messages = set()
    for session in ("fictional-absent", SESSION_TWO):
        with pytest.raises(ParentSessionUnavailable) as caught:
            linked.service.link_parent_session(linked.principal, session)
        messages.add(str(caught.value))
    assert len(messages) == 1, messages


def test_another_caregiver_cannot_claim_an_already_linked_session(linked):
    """Same refusal as absent — being linked by someone else is not disclosed."""
    linked.service.link_parent_session(linked.principal, SESSION_ONE)
    other = linked.service.bootstrap_caregiver(NEWCOMER_TWO)
    other_principal = principal_for(linked.repos, NEWCOMER_TWO)
    with pytest.raises(ParentSessionUnavailable):
        linked.service.link_parent_session(other_principal, SESSION_ONE)
    # The link still belongs to the original caregiver.
    links = linked.repos.source_links.list_for_external_id(SESSION_ONE)
    assert len(links) == 1
    assert linked.service.my_children(other_principal) == []


def test_a_provider_cannot_link_a_parent_session(linked):
    """Refused for being a PROVIDER, not incidentally for not owning it.

    `pytest.raises(IntegrationError)` passed even with the role check removed:
    a provider then failed the ownership check instead and raised a different
    IntegrationError subclass. The mutation sweep found that; the assertion is
    now on the exact type, so the role gate is what is being tested.
    """
    provider = principal_for(linked.repos, PROVIDER_ALPHA_SUBJECT)
    with pytest.raises(SubjectAlreadyHeld):
        linked.service.link_parent_session(provider, SESSION_ONE)


def test_a_provider_is_refused_even_for_a_session_they_do_own(linked):
    """Isolates the role gate from the ownership gate entirely."""
    linked.parent.add(SESSION_TWO, PROVIDER_ALPHA_SUBJECT)
    provider = principal_for(linked.repos, PROVIDER_ALPHA_SUBJECT)
    facts = linked.parent.fetch_session_facts(
        SESSION_TWO, requesting_subject=PROVIDER_ALPHA_SUBJECT)
    assert facts.is_owned_by(PROVIDER_ALPHA_SUBJECT), "ownership must not be the reason"

    with pytest.raises(SubjectAlreadyHeld):
        linked.service.link_parent_session(provider, SESSION_TWO)
    assert linked.repos.source_links.list_for_external_id(SESSION_TWO) == []


def test_an_ended_relationship_does_not_confer_ownership_on_a_retry(linked):
    """`_caregiver_owns` requires an ACTIVE connection.

    A caregiver whose relationship to the child has ended must not resolve
    their old session through the idempotent retry path — that would hand back
    a child they no longer have access to.
    """
    result = linked.service.link_parent_session(linked.principal, SESSION_ONE)
    linked.repos.caregiver_child.end_connection(
        result.connection_id, status=ConnectionStatus.ENDED,
        now=T0 + timedelta(days=1))

    with pytest.raises(ParentSessionUnavailable):
        linked.service.link_parent_session(linked.principal, SESSION_ONE)
    assert linked.service.my_children(linked.principal) == []


def test_two_caregivers_link_their_own_sessions_independently(linked):
    """The external-identity claim key must include the session id.

    A fixed or truncated key would serialise unrelated sessions onto one
    mutex, so the second caregiver's perfectly valid link would be refused as
    contention.
    """
    first = linked.service.link_parent_session(linked.principal, SESSION_ONE)

    linked.service.bootstrap_caregiver(NEWCOMER_TWO)
    other = principal_for(linked.repos, NEWCOMER_TWO)
    linked.parent.add(SESSION_TWO, NEWCOMER_TWO)
    second = linked.service.link_parent_session(other, SESSION_TWO)

    assert second.created is True
    assert second.child_id != first.child_id
    assert second.source_link_id != first.source_link_id
    assert linked.service.my_children(other) == [second.child_id]
    assert linked.service.my_children(linked.principal) == [first.child_id]


def test_an_empty_session_id_is_refused(linked):
    for value in ("", "   "):
        with pytest.raises(ParentSessionUnavailable):
            linked.service.link_parent_session(linked.principal, value)


def test_a_second_unseen_session_fails_closed(linked):
    """Parent is single-child today; guessing is unrecoverable either way."""
    linked.service.link_parent_session(linked.principal, SESSION_ONE)
    linked.parent.add(SESSION_TWO, NEWCOMER)
    with pytest.raises(SecondSessionUnresolved) as caught:
        linked.service.link_parent_session(linked.principal, SESSION_TWO)
    assert caught.value.code == "SECOND_SESSION_UNRESOLVED"


def test_the_refused_second_session_persists_nothing(linked):
    linked.service.link_parent_session(linked.principal, SESSION_ONE)
    linked.parent.add(SESSION_TWO, NEWCOMER)
    counts = {name: len(linked.repos.store.list_all(name))
              for name in linked.repos.store.collections()
              if name != "pilot_audit_events"}
    with pytest.raises(SecondSessionUnresolved):
        linked.service.link_parent_session(linked.principal, SESSION_TWO)
    assert {name: len(linked.repos.store.list_all(name))
            for name in linked.repos.store.collections()
            if name != "pilot_audit_events"} == counts
    assert linked.repos.source_links.list_for_external_id(SESSION_TWO) == []


def test_a_repeat_of_the_first_session_does_not_trip_the_second_session_rule(linked):
    """Ordering matters: the retry check runs BEFORE the second-session rule."""
    first = linked.service.link_parent_session(linked.principal, SESSION_ONE)
    again = linked.service.link_parent_session(linked.principal, SESSION_ONE)
    assert again.child_id == first.child_id and again.created is False


def test_the_parent_port_is_never_read_on_a_retry(linked):
    """A retry costs no Parent call at all."""
    calls = []
    original = linked.parent.fetch_session_facts

    def counting(session_id):
        calls.append(session_id)
        return original(session_id)

    linked.service.link_parent_session(linked.principal, SESSION_ONE)
    linked.parent.fetch_session_facts = counting
    linked.service.link_parent_session(linked.principal, SESSION_ONE)
    assert calls == []


# ===========================================================================
# the Parent boundary itself
# ===========================================================================

def test_the_parent_port_has_no_write_operation():
    """The capability is ABSENT from the contract, not merely unused."""
    for forbidden in ("save", "write", "update", "delete", "create", "put",
                      "set", "patch"):
        assert not hasattr(ParentSessionSource, forbidden), forbidden
        assert not hasattr(InMemoryParentSessionSource, forbidden), forbidden
    assert [name for name in dir(ParentSessionSource)
            if not name.startswith("_")] == ["fetch_session_facts"]


def test_session_facts_carry_identity_only():
    """No plan, answers, feedback, note, diagnosis or concern crosses the port."""
    assert set(ParentSessionFacts.__dataclass_fields__) == {
        "session_id", "owner_uid"}


def test_session_facts_ownership_is_exact_and_case_sensitive():
    facts = ParentSessionFacts(session_id=SESSION_ONE, owner_uid="AbCdEf")
    assert facts.is_owned_by("AbCdEf")
    assert not facts.is_owned_by("abcdef")
    assert not facts.is_owned_by("AbCdE")
    assert not facts.is_owned_by("")


def test_session_facts_require_both_identifiers():
    for session, owner in ((" ", "uid"), ("sess", " ")):
        with pytest.raises(ValueError):
            ParentSessionFacts(session_id=session, owner_uid=owner)


def test_the_in_memory_source_returns_none_for_an_unknown_session():
    assert InMemoryParentSessionSource().fetch_session_facts(
        "nope", requesting_subject=NEWCOMER) is None


def test_the_port_takes_the_requesting_subject_for_scoping_only():
    """The GCS blob name is `sessions/{uid}/{session_id}.json`.

    The subject is needed to LOCATE the document, which is why it is a
    parameter at all. It must not become the answer: the facts carry the
    document's own `owner_uid` so the service can refuse a disagreement.
    """
    import inspect

    signature = inspect.signature(ParentSessionSource.fetch_session_facts)
    assert list(signature.parameters) == ["self", "session_id",
                                          "requesting_subject"]
    assert signature.parameters["requesting_subject"].kind is (
        inspect.Parameter.KEYWORD_ONLY)


def test_a_session_filed_under_the_caller_but_owned_by_another_is_refused(linked):
    """The shape the REAL adapter can produce, refused by the service.

    GCS path scoping hides another account's sessions, so the common foreign
    case never reaches the ownership check. A document filed under the caller's
    own prefix while naming a different owner DOES reach it — Parent applies
    exactly this second check in `_require_session`, and so does the pilot.
    """
    linked.parent.add_misfiled(SESSION_TWO, prefix_subject=NEWCOMER,
                               owner_uid="fictional-subject-somebody-else")
    facts = linked.parent.fetch_session_facts(SESSION_TWO,
                                              requesting_subject=NEWCOMER)
    assert facts is not None, "the fixture must reach the ownership check"
    assert facts.owner_uid != NEWCOMER

    with pytest.raises(ParentSessionUnavailable):
        linked.service.link_parent_session(linked.principal, SESSION_TWO)
    assert linked.repos.source_links.list_for_external_id(SESSION_TWO) == []


def test_the_ownership_check_is_against_the_token_not_the_facts(linked):
    """A misfiled document cannot authorise itself by naming its own owner."""
    linked.parent.add_misfiled(SESSION_TWO, prefix_subject=NEWCOMER,
                               owner_uid="fictional-subject-somebody-else")
    with pytest.raises(ParentSessionUnavailable):
        linked.service.link_parent_session(linked.principal, SESSION_TWO)
    # And the refusal is the same non-enumerating one as "absent".
    with pytest.raises(ParentSessionUnavailable) as absent:
        linked.service.link_parent_session(linked.principal, "fictional-absent")
    with pytest.raises(ParentSessionUnavailable) as misfiled:
        linked.service.link_parent_session(linked.principal, SESSION_TWO)
    assert str(absent.value) == str(misfiled.value)


def test_pilot_backend_imports_no_storage_or_network_sdk():
    """Ports not SDKs. The GCS adapter lives in `pilot_runtime`.

    Parsed with `ast`, not grepped: several modules NAME these libraries in
    their docstrings to explain which boundary they sit behind, and a substring
    scan would read those explanations as violations. Only real `import`
    statements count — the lesson the prior slices' structural guards encode.
    """
    import ast
    import pathlib

    banned = {"google", "firebase_admin", "requests", "httpx", "urllib",
              "boto3", "aiohttp"}
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".")[0] in banned:
                    offenders.append((path.name, name))
    assert offenders == [], offenders


# ===========================================================================
# /pilot/me/children
# ===========================================================================

def test_my_children_returns_only_the_callers_children(linked):
    result = linked.service.link_parent_session(linked.principal, SESSION_ONE)
    assert linked.service.my_children(linked.principal) == [result.child_id]


def test_my_children_is_empty_before_any_link(linked):
    assert linked.service.my_children(linked.principal) == []


def test_my_children_does_not_leak_across_caregivers(linked):
    mine = linked.service.link_parent_session(linked.principal, SESSION_ONE)
    linked.service.bootstrap_caregiver(NEWCOMER_TWO)
    other = principal_for(linked.repos, NEWCOMER_TWO)
    assert linked.service.my_children(other) == []
    assert mine.child_id not in linked.service.my_children(other)
    # The topology's unrelated caregiver sees only its own child.
    alpha = principal_for(linked.repos, CAREGIVER_ALPHA_SUBJECT)
    assert mine.child_id not in linked.service.my_children(alpha)


def test_my_children_takes_no_caregiver_id_parameter():
    """There is no shape of this call that reads somebody else's children."""
    import inspect

    signature = inspect.signature(IntegrationIdentityService.my_children)
    assert list(signature.parameters) == ["self", "principal"]


def test_a_provider_cannot_enumerate_children_through_my_children(linked):
    provider = principal_for(linked.repos, PROVIDER_ALPHA_SUBJECT)
    with pytest.raises(IntegrationError):
        linked.service.my_children(provider)


def test_my_children_omits_a_revoked_connection(linked):
    result = linked.service.link_parent_session(linked.principal, SESSION_ONE)
    linked.repos.caregiver_child.end_connection(
        result.connection_id, status=ConnectionStatus.REVOKED,
        now=T0 + timedelta(days=1))
    assert linked.service.my_children(linked.principal) == []
    # The child record still exists; only the relationship ended.
    assert linked.repos.children.get_by_id(result.child_id) is not None


# ===========================================================================
# the premises that make three mutations UNKILLABLE
#
# The 0.5A mutation sweep leaves four survivors that no test can distinguish,
# because the behaviour they weaken is already guaranteed one layer down.
# Three of them depend on a repository contract and one on a dataclass
# invariant. The service-level filters and guards they target are kept as
# defence in depth — removing them to improve a mutation score would trade a
# real safety margin for a number — so these tests pin the PREMISE instead.
#
# If a premise ever changes, the redundant guard becomes load-bearing and the
# mutation becomes a genuine defect. These tests are what makes that arrive as
# a failure rather than as a silent weakening.
# ===========================================================================

def test_the_connection_repository_excludes_ended_rows_by_default(wiring):
    """Premise for `_caregiver_owns` and `my_children`.

    `list_children_for_caregiver` filters to active connections unless
    `include_ended=True`, and the integration service never passes it. That is
    why dropping `and c.is_active` from `_caregiver_owns`, and `if c.is_active`
    from `my_children`, cannot be detected by any test: the comprehensions
    never see a row for which the predicate is false.
    """
    import inspect

    from pilot_backend.domain.connections import CaregiverChildConnection
    from pilot_backend.domain.enums import CaregiverRelationship

    caregiver = wiring.topo.caregiver_alpha
    child = wiring.repos.children.create(
        Child.create(actor_id=caregiver.caregiver_id, now=T0))
    connection = wiring.repos.caregiver_child.connect(
        CaregiverChildConnection.create(
            caregiver.caregiver_id, child.child_id,
            CaregiverRelationship.PARENT,
            actor_id=caregiver.caregiver_id, now=T0))

    wiring.repos.caregiver_child.end_connection(
        connection.connection_id, status=ConnectionStatus.ENDED,
        now=T0 + timedelta(days=1))

    default = wiring.repos.caregiver_child.list_children_for_caregiver(
        caregiver.caregiver_id)
    assert all(c.is_active for c in default)
    assert connection.connection_id not in {c.connection_id for c in default}
    # The row is still there; it is the DEFAULT that hides it.
    with_ended = wiring.repos.caregiver_child.list_children_for_caregiver(
        caregiver.caregiver_id, include_ended=True)
    assert connection.connection_id in {c.connection_id for c in with_ended}

    # And the service relies on that default rather than opting out of it.
    assert "include_ended" not in inspect.getsource(IntegrationIdentityService)


def test_a_verified_token_cannot_carry_a_blank_subject():
    """Premise for `_verified_subject`'s empty-subject guard.

    `VerifiedToken.__post_init__` refuses a blank subject, so by the time
    `_verified_subject` strips and tests one, it cannot be empty. A verifier
    that tries to produce one raises `AuthError` while CONSTRUCTING the token,
    which is the clause that actually returns 401 — the guard below it is
    defence against a non-conforming verifier and is unreachable through every
    verifier this codebase has.
    """
    from pilot_backend.auth.interface import AuthError

    for blank in ("", " ", "   ", "\t", "\n", " \t\n "):
        with pytest.raises(AuthError):
            VerifiedToken(subject=blank)
    # A subject that merely needs trimming is still accepted and still strips
    # to something non-empty, so the guard has no live case there either.
    assert VerifiedToken(subject="  fictional-uid  ").subject.strip()


# ===========================================================================
# no method takes a role, uid or actor id — the structural guard every
# slice since 0.4A has carried
# ===========================================================================

def test_no_service_method_accepts_identity_as_a_parameter():
    """The principal is the only source of identity.

    `bootstrap_caregiver` takes `auth_subject` and that is the one exception:
    it has no principal yet by definition, and the transport layer passes the
    VERIFIED subject. Asserted explicitly so the exception stays deliberate.
    """
    import inspect

    banned = {"role", "actor_role", "uid", "user_id", "caregiver_id",
              "provider_id", "actor_id", "owner_uid", "practice_id"}
    for name, method in inspect.getmembers(IntegrationIdentityService,
                                           inspect.isfunction):
        if name.startswith("_"):
            continue
        parameters = set(inspect.signature(method).parameters)
        assert not (parameters & banned), (name, parameters & banned)
        if name == "bootstrap_caregiver":
            assert "auth_subject" in parameters
        else:
            assert "auth_subject" not in parameters, name
            assert "principal" in parameters, name


# ===========================================================================
# audit
# ===========================================================================

def test_bootstrap_audits_with_a_fingerprint_and_never_the_subject(wiring):
    caregiver = wiring.service.bootstrap_caregiver(
        NEWCOMER, request_id="req-bootstrap")
    events = [doc for doc in audit_docs(wiring.repos)
              if doc.get("action") == AuditAction.CAREGIVER_IDENTITY_BOOTSTRAPPED.value]
    assert len(events) == 1
    metadata = events[0]["metadata"]
    assert metadata["subject_fingerprint"] == subject_fingerprint(NEWCOMER)
    assert metadata["caregiver_id"] == caregiver.caregiver_id
    assert metadata["holder_actor_type"] == "caregiver"
    assert NEWCOMER not in json.dumps(events[0])


def test_the_legacy_backfill_is_audited_distinguishably(wiring):
    seed_legacy_caregiver(wiring.repos)
    wiring.service.bootstrap_caregiver(LEGACY, request_id="req-legacy")
    states = {doc["metadata"].get("integration_state")
              for doc in audit_docs(wiring.repos) if doc.get("metadata")}
    assert "LEGACY_CLAIM_BACKFILLED" in states


def test_every_refusal_is_audited(wiring):
    seed_legacy_caregiver(wiring.repos, name="Legacy-One")
    seed_legacy_caregiver(wiring.repos, name="Legacy-Two")
    with pytest.raises(AmbiguousSubjectState):
        wiring.service.bootstrap_caregiver(LEGACY, request_id="req-ambiguous")
    failures = [doc for doc in audit_docs(wiring.repos)
                if doc.get("result") == "failure"]
    assert failures
    assert LEGACY not in json.dumps(failures)


def test_no_audit_metadata_contains_a_raw_auth_subject(linked):
    """§7, swept across every operation, success and refusal alike.

    Scoped to `metadata` deliberately. `AuditEvent.actor_auth_subject` is a
    top-level field that has existed since 0.2 and holds the ACTING
    principal's own subject — documented there as something an audit reader
    investigating a compromised credential needs. 0.5A neither widens that nor
    relies on it; what it must not do is put a subject into metadata, where the
    fingerprint exists precisely so it never has to.
    """
    linked.service.link_parent_session(linked.principal, SESSION_ONE)
    linked.parent.add(SESSION_TWO, NEWCOMER)
    with pytest.raises(SecondSessionUnresolved):
        linked.service.link_parent_session(linked.principal, SESSION_TWO)
    with pytest.raises(SubjectAlreadyHeld):
        linked.service.bootstrap_caregiver(PROVIDER_ALPHA_SUBJECT)

    metadata_blob = json.dumps([doc.get("metadata") or {}
                                for doc in audit_docs(linked.repos)])
    for subject in (NEWCOMER, NEWCOMER_TWO, PROVIDER_ALPHA_SUBJECT,
                    CAREGIVER_ALPHA_SUBJECT, LEGACY):
        assert subject not in metadata_blob, subject


def test_an_audit_event_never_holds_a_subject_other_than_the_actors(linked):
    """The pre-existing top-level field is bounded, not merely tolerated.

    `actor_auth_subject` may hold the acting principal's own subject. It must
    never hold anybody ELSE's — a bootstrap refused because a provider holds
    the subject must not record that provider's credential, and no event may
    carry a third party's.
    """
    linked.service.link_parent_session(linked.principal, SESSION_ONE)
    with pytest.raises(SubjectAlreadyHeld):
        linked.service.bootstrap_caregiver(PROVIDER_ALPHA_SUBJECT)

    foreign = (PROVIDER_ALPHA_SUBJECT, CAREGIVER_ALPHA_SUBJECT, LEGACY,
               NEWCOMER_TWO)
    for doc in audit_docs(linked.repos):
        subject = doc.get("actor_auth_subject")
        if subject is not None:
            assert subject == NEWCOMER, subject
        # And no subject reaches any other field of the record.
        rest = {key: value for key, value in doc.items()
                if key != "actor_auth_subject"}
        blob = json.dumps(rest)
        for other in (*foreign, NEWCOMER):
            assert other not in blob, (other, rest)


def test_the_bootstrap_audit_carries_no_subject_at_all(wiring):
    """There is no principal yet, so the field is empty by construction."""
    wiring.service.bootstrap_caregiver(NEWCOMER, request_id="req-bootstrap")
    events = [doc for doc in audit_docs(wiring.repos)
              if doc.get("action")
              == AuditAction.CAREGIVER_IDENTITY_BOOTSTRAPPED.value]
    assert events
    for doc in events:
        assert doc.get("actor_auth_subject") in (None, "")
        assert NEWCOMER not in json.dumps(doc)


def test_no_audit_record_contains_a_parent_session_id(linked):
    """`external_id` is deliberately NOT an allowlisted metadata key."""
    linked.service.link_parent_session(linked.principal, SESSION_ONE)
    blob = json.dumps(audit_docs(linked.repos))
    assert SESSION_ONE not in blob
    assert "external_id" not in ALLOWED_METADATA_KEYS


def test_integration_metadata_keys_are_all_allowlisted(linked):
    """A key the allowlist does not know about would be refused at write time."""
    linked.service.link_parent_session(linked.principal, SESSION_ONE)
    for doc in audit_docs(linked.repos):
        for key in (doc.get("metadata") or {}):
            assert key in ALLOWED_METADATA_KEYS, key


# ===========================================================================
# errors
# ===========================================================================

def test_every_integration_error_is_phi_safe():
    for error in (IntegrationError, SubjectAlreadyHeld, AmbiguousSubjectState,
                  ParentSessionUnavailable, SecondSessionUnresolved):
        assert error.PHI_SAFE_MESSAGE is True


def test_no_error_message_echoes_a_subject_session_or_sentinel(linked):
    linked.parent.add(SESSION_TWO, "fictional-subject-somebody-else")
    raised = []
    for call in (
        lambda: linked.service.link_parent_session(linked.principal, SESSION_TWO),
        lambda: linked.service.link_parent_session(linked.principal, "absent"),
        lambda: linked.service.bootstrap_caregiver(PROVIDER_ALPHA_SUBJECT),
    ):
        with pytest.raises(IntegrationError) as caught:
            call()
        raised.append(str(caught.value))

    blob = " ".join(raised)
    for secret in (SESSION_TWO, NEWCOMER, PROVIDER_ALPHA_SUBJECT,
                   SENTINEL_EMAIL, *ALL_SENTINELS):
        assert secret not in blob, secret


# ===========================================================================
# the four HTTP routes
# ===========================================================================

class _ExplodingInput:
    """A `wsgi.input` that fails if anyone reads it.

    Stronger than asserting the body is ignored: a handler that reads it at all
    — to validate, to measure, to log — fails loudly rather than passing because
    the parsed value happened to be unused.
    """

    def read(self, *args):
        raise AssertionError("the request body must never be read")

    readline = readlines = read

    def __iter__(self):
        raise AssertionError("the request body must never be iterated")


def build_http(*, parent_source=None):
    """Settings, repositories, verifier, recorder and the WSGI application."""
    settings = PilotSettings.from_env({
        "PILOT_ENVIRONMENT": "dev",
        "PILOT_ALLOWED_ORIGINS": "http://localhost:3000",
        "PILOT_DEV_AUTH": "1",
    })
    repos = FirestoreRepositories(FakeDocumentStore())
    topology = build_secure_topology(repos)
    verifier = DevAuthVerifier("dev")
    tokens = {
        "token-caregiver-alpha": CAREGIVER_ALPHA_SUBJECT,
        "token-provider-alpha": PROVIDER_ALPHA_SUBJECT,
        "token-newcomer": NEWCOMER,
        "token-newcomer-two": NEWCOMER_TWO,
        "token-legacy": LEGACY,
    }
    for token, subject in tokens.items():
        verifier.add(token, VerifiedToken(subject=subject, email=SENTINEL_EMAIL))
    recorder = AuditRecorder(repos.audit_events, environment="dev")
    logs = []
    parent = parent_source if parent_source is not None else InMemoryParentSessionSource()
    app = build_application(settings=settings, repos=repos, verifier=verifier,
                            recorder=recorder, parent_source=parent,
                            log_sink=logs)

    class Bundle:
        pass

    bundle = Bundle()
    bundle.app, bundle.repos, bundle.topo = app, repos, topology
    bundle.parent, bundle.logs, bundle.verifier = parent, logs, verifier
    return bundle


def call(app, path, *, method="GET", bearer=None, body=b"",
         exploding_body=False, query="", headers=None):
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SERVER_NAME": "testserver",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.input": _ExplodingInput() if exploding_body else io.BytesIO(body),
        "wsgi.url_scheme": "http",
        "CONTENT_LENGTH": str(len(body)),
    }
    if bearer is not None:
        environ["HTTP_AUTHORIZATION"] = bearer
    for key, value in (headers or {}).items():
        environ["HTTP_" + key.upper().replace("-", "_")] = value

    captured = {}

    def start_response(status, response_headers):
        captured["status"] = int(status.split()[0])
        captured["headers"] = dict(response_headers)

    chunks = app(environ, start_response)
    return (captured["status"],
            json.loads(b"".join(chunks).decode("utf-8")),
            captured["headers"])


@pytest.fixture()
def http():
    return build_http()


def test_all_four_routes_are_protected(http):
    """`/health` remains the only public path. Deny by default, at routing."""
    for path, method in (("/pilot/me", "GET"),
                         ("/pilot/me/children", "GET"),
                         ("/pilot/bootstrap/caregiver", "POST"),
                         ("/pilot/parent-sessions/sess/link-child", "POST")):
        status, body, _ = call(http.app, path, method=method)
        assert status == 401, (path, status)
        assert body == {"error": "authentication required"}


def test_an_invalid_or_revoked_token_is_401_on_every_route(http):
    for path, method in (("/pilot/me", "GET"),
                         ("/pilot/me/children", "GET"),
                         ("/pilot/bootstrap/caregiver", "POST"),
                         ("/pilot/parent-sessions/sess/link-child", "POST")):
        assert call(http.app, path, method=method,
                    bearer="Bearer nonsense")[0] == 401
        assert call(http.app, path, method=method, bearer="Basic abc")[0] == 401


def test_a_token_with_an_empty_subject_is_401_on_every_route(http):
    """A verified token carrying no subject authenticates nobody.

    `resolve_principal` would refuse it as 403 and `subject_fingerprint` would
    raise, so without the explicit check the two routes disagree and the
    bootstrap path raises rather than refusing. 401 on all four, and nothing
    is created.
    """
    class _BlankSubjectVerifier:
        environment = "dev"

        def verify(self, bearer):
            return VerifiedToken(subject="   ")

    http.app._verifier = _BlankSubjectVerifier()
    for path, method in (("/pilot/me", "GET"),
                         ("/pilot/me/children", "GET"),
                         ("/pilot/bootstrap/caregiver", "POST"),
                         (f"/pilot/parent-sessions/{SESSION_ONE}/link-child",
                          "POST")):
        status, body, _ = call(http.app, path, method=method,
                               bearer="Bearer anything")
        assert status == 401, (path, status)
        assert body == {"error": "authentication required"}
    assert claim_docs(http.repos) == []


def test_me_returns_the_server_derived_identity(http):
    status, body, _ = call(http.app, "/pilot/me",
                           bearer="Bearer token-caregiver-alpha")
    assert status == 200
    assert body["role"] == "caregiver"
    assert body["caregiver_id"] == http.topo.caregiver_alpha.caregiver_id
    assert CAREGIVER_ALPHA_SUBJECT not in json.dumps(body)


def test_me_works_for_a_pre_provisioned_provider(http):
    status, body, _ = call(http.app, "/pilot/me",
                           bearer="Bearer token-provider-alpha")
    assert status == 200
    assert body["role"] == "provider"
    assert body["provider_id"] == http.topo.provider_alpha.provider_id
    assert body["practice_id"] == http.topo.practice.practice_id


def test_me_never_silently_bootstraps(http):
    """A valid token with no record is 403, and creates nothing.

    Counted over DOCUMENTS rather than collection names: `FakeDocumentStore`
    materialises an empty bucket when a collection is merely read, so a new
    name with zero documents in it is a read, not a write.
    """
    def documents(repos):
        return {name: len(repos.store.list_all(name))
                for name in repos.store.collections()
                if name != "pilot_audit_events"
                and repos.store.list_all(name)}

    before = documents(http.repos)
    status, body, _ = call(http.app, "/pilot/me", bearer="Bearer token-newcomer")
    assert status == 403
    assert body == {"error": "not permitted"}
    assert http.repos.caregivers.get_by_auth_subject(NEWCOMER) is None
    assert claim_docs(http.repos) == []
    assert documents(http.repos) == before


def test_bootstrap_over_http_creates_the_identity(http):
    status, body, _ = call(http.app, "/pilot/bootstrap/caregiver",
                           method="POST", bearer="Bearer token-newcomer")
    assert status == 200
    assert body["role"] == "caregiver"
    assert body["caregiver_id"].startswith("cgvr_")
    assert body["actor_id"] == body["caregiver_id"]
    # and the subject now resolves
    assert call(http.app, "/pilot/me",
                bearer="Bearer token-newcomer")[1]["caregiver_id"] == (
        body["caregiver_id"])


def test_bootstrap_never_reads_the_request_body(http):
    """Proved with a stream that raises on any access."""
    status, body, _ = call(http.app, "/pilot/bootstrap/caregiver", method="POST",
                           bearer="Bearer token-newcomer", exploding_body=True)
    assert status == 200 and body["caregiver_id"].startswith("cgvr_")


def test_a_forged_body_cannot_assert_role_or_identity(http):
    """Not rejected — never looked at. There is no parse to defeat."""
    forged = json.dumps({
        "role": "provider", "actor_role": "provider",
        "caregiver_id": "cgvr_forged", "provider_id": "prov_forged",
        "uid": "somebody-else", "auth_subject": PROVIDER_ALPHA_SUBJECT,
        "owner_uid": PROVIDER_ALPHA_SUBJECT, "practice_id": "prac_forged",
        "display_name": "ZZSENTINEL-CHILDNAME-Quillwood",
    }).encode("utf-8")
    status, body, _ = call(http.app, "/pilot/bootstrap/caregiver", method="POST",
                           bearer="Bearer token-newcomer", body=forged)
    assert status == 200
    assert body["role"] == "caregiver"
    assert body["caregiver_id"] != "cgvr_forged"
    caregiver = http.repos.caregivers.get_by_auth_subject(NEWCOMER)
    assert caregiver.caregiver_id == body["caregiver_id"]
    assert caregiver.auth_subject == NEWCOMER


def test_a_forged_query_string_cannot_assert_identity(http):
    status, body, _ = call(
        http.app, "/pilot/bootstrap/caregiver", method="POST",
        bearer="Bearer token-newcomer",
        query="role=provider&caregiver_id=cgvr_forged&uid=somebody")
    assert status == 200 and body["role"] == "caregiver"
    assert body["caregiver_id"] != "cgvr_forged"


def test_forged_identity_headers_are_ignored(http):
    status, body, _ = call(
        http.app, "/pilot/me", bearer="Bearer token-caregiver-alpha",
        headers={"X-Role": "provider", "X-Caregiver-Id": "cgvr_forged",
                 "X-Uid": PROVIDER_ALPHA_SUBJECT})
    assert status == 200 and body["role"] == "caregiver"
    assert body["caregiver_id"] == http.topo.caregiver_alpha.caregiver_id


def test_bootstrap_over_http_is_idempotent(http):
    first = call(http.app, "/pilot/bootstrap/caregiver", method="POST",
                 bearer="Bearer token-newcomer")[1]
    second = call(http.app, "/pilot/bootstrap/caregiver", method="POST",
                  bearer="Bearer token-newcomer")[1]
    assert first["caregiver_id"] == second["caregiver_id"]
    assert len(claim_docs(http.repos)) == 1


def test_bootstrap_as_a_provider_subject_is_a_constant_403(http):
    status, body, _ = call(http.app, "/pilot/bootstrap/caregiver",
                           method="POST", bearer="Bearer token-provider-alpha")
    assert status == 403
    assert body == {"error": "not permitted"}


def test_link_child_bridges_an_owned_session_over_http(http):
    http.parent.add(SESSION_ONE, NEWCOMER)
    call(http.app, "/pilot/bootstrap/caregiver", method="POST",
         bearer="Bearer token-newcomer")
    status, body, _ = call(http.app,
                           f"/pilot/parent-sessions/{SESSION_ONE}/link-child",
                           method="POST", bearer="Bearer token-newcomer")
    assert status == 200
    assert body["created"] is True
    assert body["child_id"].startswith("chld_")
    assert body["child_id"] != SESSION_ONE


def test_link_child_never_reads_the_request_body(http):
    http.parent.add(SESSION_ONE, NEWCOMER)
    call(http.app, "/pilot/bootstrap/caregiver", method="POST",
         bearer="Bearer token-newcomer")
    status, _, _ = call(http.app,
                        f"/pilot/parent-sessions/{SESSION_ONE}/link-child",
                        method="POST", bearer="Bearer token-newcomer",
                        exploding_body=True)
    assert status == 200


def test_a_body_owner_uid_cannot_assert_session_ownership(http):
    """The session belongs to somebody else; a payload claim changes nothing."""
    http.parent.add(SESSION_ONE, "fictional-subject-somebody-else")
    call(http.app, "/pilot/bootstrap/caregiver", method="POST",
         bearer="Bearer token-newcomer")
    status, body, _ = call(
        http.app, f"/pilot/parent-sessions/{SESSION_ONE}/link-child",
        method="POST", bearer="Bearer token-newcomer",
        body=json.dumps({"owner_uid": "fictional-subject-somebody-else",
                         "uid": "fictional-subject-somebody-else"}).encode())
    assert status == 403
    assert body == {"error": "not permitted"}
    assert http.repos.source_links.list_for_external_id(SESSION_ONE) == []


def test_an_unknown_and_a_foreign_session_return_the_same_http_response(http):
    http.parent.add(SESSION_TWO, "fictional-subject-somebody-else")
    call(http.app, "/pilot/bootstrap/caregiver", method="POST",
         bearer="Bearer token-newcomer")
    responses = set()
    for session in ("fictional-absent-session", SESSION_TWO):
        status, body, _ = call(
            http.app, f"/pilot/parent-sessions/{session}/link-child",
            method="POST", bearer="Bearer token-newcomer")
        responses.add((status, json.dumps(body, sort_keys=True)))
    assert len(responses) == 1, responses


def test_the_second_session_returns_409_with_its_code(http):
    """A product state the client must act on, not a security refusal."""
    http.parent.add(SESSION_ONE, NEWCOMER)
    http.parent.add(SESSION_TWO, NEWCOMER)
    call(http.app, "/pilot/bootstrap/caregiver", method="POST",
         bearer="Bearer token-newcomer")
    call(http.app, f"/pilot/parent-sessions/{SESSION_ONE}/link-child",
         method="POST", bearer="Bearer token-newcomer")
    status, body, _ = call(http.app,
                           f"/pilot/parent-sessions/{SESSION_TWO}/link-child",
                           method="POST", bearer="Bearer token-newcomer")
    assert status == 409
    assert body["code"] == "SECOND_SESSION_UNRESOLVED"
    assert SESSION_TWO not in json.dumps(body)


def test_link_child_without_a_parent_source_refuses(http):
    """A missing Parent boundary must refuse, not invent a session."""
    app_without = build_http().app
    app_without._parent_source = None
    call(app_without, "/pilot/bootstrap/caregiver", method="POST",
         bearer="Bearer token-newcomer")
    assert call(app_without,
                f"/pilot/parent-sessions/{SESSION_ONE}/link-child",
                method="POST", bearer="Bearer token-newcomer")[0] == 403


def test_my_children_over_http_returns_only_the_callers_children(http):
    http.parent.add(SESSION_ONE, NEWCOMER)
    call(http.app, "/pilot/bootstrap/caregiver", method="POST",
         bearer="Bearer token-newcomer")
    linked = call(http.app, f"/pilot/parent-sessions/{SESSION_ONE}/link-child",
                  method="POST", bearer="Bearer token-newcomer")[1]

    status, body, _ = call(http.app, "/pilot/me/children",
                           bearer="Bearer token-newcomer")
    assert status == 200
    assert body["child_ids"] == [linked["child_id"]]

    call(http.app, "/pilot/bootstrap/caregiver", method="POST",
         bearer="Bearer token-newcomer-two")
    other = call(http.app, "/pilot/me/children",
                 bearer="Bearer token-newcomer-two")[1]
    assert other["child_ids"] == []


def test_a_provider_gets_their_caseload_from_my_children(http):
    """0.5B changes this route's answer for providers, deliberately.

    0.5A refused a provider here with a 403, because provider child listing
    had no authorization review yet and reusing the caregiver path would have
    been an enumeration shortcut. 0.5B gives providers their own reviewed
    path — `connected_children`, filtered to the caller's ACTIVE connections —
    so the route now answers "my children" for both kinds of actor.

    What did NOT change is the thing the 0.5A refusal protected: the two roles
    go to DIFFERENT service methods, neither takes an actor id, and the role
    is derived by `resolve_principal` from which repository matched the
    verified subject. A client still cannot choose which branch runs, and
    still cannot aim either at someone else.

    Provider-Alpha is connected to Child-Alpha in the fixture topology, so the
    caseload is non-empty — a test that passed with an empty list would prove
    nothing about filtering.
    """
    status, body, _ = call(http.app, "/pilot/me/children",
                           bearer="Bearer token-provider-alpha")
    assert status == 200
    assert "children" in body, body
    assert "child_ids" not in body, "the caregiver shape leaked to a provider"
    returned = {row["child_id"] for row in body["children"]}
    assert returned == {http.topo.child_alpha.child_id}
    # Connection-scoped only: being connected is not being the managing
    # clinician, and the payload must say so rather than imply it.
    assert all(row["is_managing_clinician"] is False
               for row in body["children"])


def test_my_children_accepts_no_caregiver_id_from_the_request(http):
    """Neither a query parameter nor a body can redirect the listing."""
    http.parent.add(SESSION_ONE, NEWCOMER)
    call(http.app, "/pilot/bootstrap/caregiver", method="POST",
         bearer="Bearer token-newcomer")
    mine = call(http.app, f"/pilot/parent-sessions/{SESSION_ONE}/link-child",
                method="POST", bearer="Bearer token-newcomer")[1]["child_id"]
    call(http.app, "/pilot/bootstrap/caregiver", method="POST",
         bearer="Bearer token-newcomer-two")
    victim = http.repos.caregivers.get_by_auth_subject(NEWCOMER)

    status, body, _ = call(
        http.app, "/pilot/me/children", bearer="Bearer token-newcomer-two",
        query=f"caregiver_id={victim.caregiver_id}&child_id={mine}")
    assert status == 200
    assert body["child_ids"] == []


def test_me_children_and_me_do_not_shadow_each_other(http):
    call(http.app, "/pilot/bootstrap/caregiver", method="POST",
         bearer="Bearer token-newcomer")
    me = call(http.app, "/pilot/me", bearer="Bearer token-newcomer")[1]
    children = call(http.app, "/pilot/me/children",
                    bearer="Bearer token-newcomer")[1]
    assert "caregiver_id" in me and "child_ids" not in me
    assert "child_ids" in children and "caregiver_id" not in children


def test_each_route_rejects_the_wrong_method(http):
    for path, allowed in (("/pilot/me", "GET"),
                          ("/pilot/me/children", "GET"),
                          ("/pilot/bootstrap/caregiver", "POST"),
                          (f"/pilot/parent-sessions/{SESSION_ONE}/link-child",
                           "POST")):
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            status = call(http.app, path, method=method,
                          bearer="Bearer token-caregiver-alpha")[0]
            if method == allowed:
                assert status != 405, (path, method)
            else:
                assert status == 405, (path, method)


def test_near_miss_paths_are_404_and_never_public(http):
    for path in ("/pilot/me/children/extra", "/pilot/me/child",
                 "/pilot/bootstrap", "/pilot/bootstrap/caregiver/extra",
                 "/pilot/bootstrap/provider",
                 "/pilot/parent-sessions/link-child",
                 "/pilot/parent-sessions//link-child",
                 "/pilot/parent-sessions/sess/link-child/extra",
                 "/pilot/parent-sessions/sess/link", "/pilot/mechildren"):
        status, body, _ = call(http.app, path, method="POST")
        assert status in (404, 405), (path, status)
        assert body != {"status": "ok"}, path


def test_no_route_ever_renders_internal_detail(http):
    """A dependency that explodes yields a bare 500."""
    class Exploding:
        environment = "dev"

        def verify(self, bearer):
            raise RuntimeError("ZZSENTINEL-SECRET-xyz789 at /secret/path")

    http.app._verifier = Exploding()
    for path, method in (("/pilot/me", "GET"),
                         ("/pilot/bootstrap/caregiver", "POST")):
        status, body, _ = call(http.app, path, method=method,
                               bearer="Bearer whatever")
        assert status == 500
        assert body == {"error": "internal error"}


def test_every_response_carries_the_hardening_headers(http):
    call(http.app, "/pilot/bootstrap/caregiver", method="POST",
         bearer="Bearer token-newcomer")
    for path, method in (("/pilot/me", "GET"), ("/pilot/me/children", "GET")):
        _, _, headers = call(http.app, path, method=method,
                             bearer="Bearer token-newcomer")
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Cache-Control"] == "no-store"
        assert headers["Content-Type"] == "application/json"


def test_no_log_line_contains_a_subject_session_or_sentinel(http):
    http.parent.add(SESSION_ONE, NEWCOMER)
    call(http.app, "/pilot/bootstrap/caregiver", method="POST",
         bearer="Bearer token-newcomer",
         body=json.dumps({"display_name": "ZZSENTINEL-NOTE-x"}).encode())
    call(http.app, f"/pilot/parent-sessions/{SESSION_ONE}/link-child",
         method="POST", bearer="Bearer token-newcomer")
    call(http.app, "/pilot/me", bearer="Bearer token-newcomer")
    call(http.app, "/pilot/me/children", bearer="Bearer token-newcomer")
    call(http.app, "/pilot/me", bearer="Bearer nonsense")

    blob = "\n".join(http.logs)
    for secret in (NEWCOMER, CAREGIVER_ALPHA_SUBJECT, SESSION_ONE,
                   "token-newcomer", SENTINEL_EMAIL, *ALL_SENTINELS):
        assert secret not in blob, secret


def test_logs_record_the_route_template_not_the_populated_path(http):
    http.parent.add(SESSION_ONE, NEWCOMER)
    call(http.app, "/pilot/bootstrap/caregiver", method="POST",
         bearer="Bearer token-newcomer")
    call(http.app, f"/pilot/parent-sessions/{SESSION_ONE}/link-child",
         method="POST", bearer="Bearer token-newcomer")
    blob = "\n".join(http.logs)
    assert "/pilot/parent-sessions/{session_id}/link-child" in blob
    assert SESSION_ONE not in blob
