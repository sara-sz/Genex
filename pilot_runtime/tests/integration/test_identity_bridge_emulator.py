"""0.5A — auth-subject uniqueness against a REAL Firestore emulator.

The `pilot_backend` suite runs this same service code over `FakeDocumentStore`,
which is a plain dict and NOT thread-safe. Every claim about CONTENTION is made
here and only here.

Four things are proved against a real server:

**One subject, one caregiver, under contention.** Eight threads bootstrap the
same new subject simultaneously; exactly one claim document and one `Caregiver`
exist afterwards, every thread returns the same `caregiver_id`, and the subject
resolves — no `AmbiguousAuthSubject`.

**The legacy backfill races correctly too.** Eight threads bootstrap a subject
that already has exactly one pre-existing `Caregiver` and no claim. One claim is
created, pointing at that caregiver, and no second caregiver is minted.

**Ambiguity fails closed on a real store.** Two pre-existing caregivers for one
subject: every thread refuses and no claim is written.

**The claim+caregiver write is all-or-nothing.** Fault injection before the
claim, inside the transaction, and after commit on retry — none leaves a
stranded claim, and a clean retry succeeds.

The 0.4A emulator lesson applies throughout: a stalled harness must report
itself rather than return a short result list that reads as a uniqueness win.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth.interface import VerifiedToken
from pilot_backend.auth.resolver import PrincipalResolutionError, resolve_principal
from pilot_backend.domain.auth_identity import (
    AuthSubjectIdentityClaim,
    auth_subject_claim_id,
    subject_fingerprint,
)
from pilot_backend.domain.entities import Caregiver
from pilot_backend.domain.roles import ActorRole
from pilot_backend.domain.source_link import SourceSystem
from pilot_backend.integration.errors import (
    AmbiguousSubjectState,
    ParentSessionLinkContended,
    ParentSessionUnavailable,
    SecondSessionUnresolved,
    SubjectAlreadyHeld,
)
from pilot_backend.integration.identity_service import IntegrationIdentityService
from pilot_backend.integration.parent_source import InMemoryParentSessionSource
from pilot_backend.repository.interface import AmbiguousAuthSubject, RecordNotFound

from ..test_sentinels import ALL_SENTINELS

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


@pytest.fixture()
def bridge(repos, unique_suffix):
    """The integration service over the REAL emulator-backed repositories."""
    recorder = AuditRecorder(repos.audit_events, environment="test")
    parent = InMemoryParentSessionSource()

    class Bundle:
        pass

    bundle = Bundle()
    bundle.repos = repos
    bundle.parent = parent
    bundle.suffix = unique_suffix
    bundle.recorder = recorder
    bundle.service = IntegrationIdentityService(
        repos=repos, parent_source=parent, recorder=recorder,
        now=_AdvancingClock(T0))

    # Subjects are unique per test AND per RUN.
    #
    # `unique_suffix` alone is derived from the test name, which makes it stable
    # across runs — and the emulator database is deliberately never reset. The
    # prior emulator suites are unaffected because they assert on freshly minted
    # random ids. These tests assert ABSOLUTE counts for a given subject ("one
    # caregiver", "no claim"), so a second run would see the first run's records
    # and the fault-injection tests would never reach the injected call at all.
    # Found by running the suite twice.
    nonce = "-" + uuid.uuid4().hex[:10]
    bundle.subject = "fictional-subject-bridge" + unique_suffix + nonce
    bundle.other_subject = "fictional-subject-bridge-two" + unique_suffix + nonce
    bundle.session = "fictional-parent-session" + unique_suffix + nonce
    return bundle


def _race(target, count: int = RACE_WIDTH):
    """Run `target(i)` on `count` threads released together.

    Every thread is accounted for, so a stalled harness reports itself rather
    than returning a short list that reads as a uniqueness result — see
    `test_identity_emulator.py` for the incident that taught this.
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


def _claim_exists(repos, subject) -> bool:
    return repos.auth_subject_claims.find_for_subject(subject) is not None


def _caregivers_for(repos, subject):
    """Every caregiver document carrying this subject, bypassing the resolver.

    `get_by_auth_subject` RAISES on more than one, which is the behaviour under
    test; counting needs the raw query.
    """
    return repos.store.query_equals("pilot_caregivers", "auth_subject", subject)


# ===========================================================================
# the new-subject race — the headline claim
# ===========================================================================

def test_eight_concurrent_bootstraps_produce_exactly_one_caregiver(bridge):
    """The claim wins the race on a real server."""
    results, errors = _race(
        lambda i: bridge.service.bootstrap_caregiver(
            bridge.subject, request_id=f"req-race-{i}").caregiver_id)

    assert errors == [], [repr(e) for e in errors]
    assert len(results) == RACE_WIDTH
    assert len(set(results)) == 1, f"{len(set(results))} distinct identities"

    # Exactly one claim, exactly one caregiver.
    claim = bridge.repos.auth_subject_claims.find_for_subject(bridge.subject)
    assert claim is not None
    assert claim.holder_actor_id == results[0]
    assert len(_caregivers_for(bridge.repos, bridge.subject)) == 1


def test_the_raced_subject_still_resolves_to_one_principal(bridge):
    """The whole point: the person can authenticate afterwards.

    Before 0.5A this is exactly what broke — two caregivers shared a subject and
    `get_by_auth_subject` raised `AmbiguousAuthSubject` permanently, with no
    release path because nothing deletes.
    """
    results, errors = _race(
        lambda i: bridge.service.bootstrap_caregiver(bridge.subject).caregiver_id)
    assert errors == []

    principal = resolve_principal(
        VerifiedToken(subject=bridge.subject), bridge.repos)
    assert principal.role is ActorRole.CAREGIVER
    assert principal.application_id == results[0]


def test_the_claim_document_id_is_the_mutex(bridge):
    """The surviving document is keyed on the fingerprint, not on an actor id."""
    _race(lambda i: bridge.service.bootstrap_caregiver(bridge.subject))

    fingerprint = subject_fingerprint(bridge.subject)
    stored = bridge.repos.store.get("pilot_auth_subject_claims",
                                    auth_subject_claim_id(fingerprint))
    assert stored is not None
    assert stored["subject_fingerprint"] == fingerprint
    # The raw subject is in neither the key nor the document.
    assert bridge.subject not in auth_subject_claim_id(fingerprint)
    assert bridge.subject not in str(stored)


def test_a_sequential_retry_after_the_race_adds_nothing(bridge):
    results, _ = _race(
        lambda i: bridge.service.bootstrap_caregiver(bridge.subject).caregiver_id)
    again = bridge.service.bootstrap_caregiver(bridge.subject)
    assert again.caregiver_id == results[0]
    assert len(_caregivers_for(bridge.repos, bridge.subject)) == 1


def test_two_different_subjects_racing_do_not_interfere(bridge):
    """Distinct subjects must not serialise onto one claim."""
    def bootstrap(index: int) -> tuple:
        subject = bridge.subject if index % 2 == 0 else bridge.other_subject
        return subject, bridge.service.bootstrap_caregiver(subject).caregiver_id

    results, errors = _race(bootstrap)
    assert errors == []
    by_subject = {}
    for subject, caregiver_id in results:
        by_subject.setdefault(subject, set()).add(caregiver_id)
    assert set(by_subject) == {bridge.subject, bridge.other_subject}
    for subject, ids in by_subject.items():
        assert len(ids) == 1, (subject, ids)
    first, second = by_subject[bridge.subject], by_subject[bridge.other_subject]
    assert first != second


# ===========================================================================
# the LEGACY BACKFILL race
# ===========================================================================

def test_eight_concurrent_backfills_of_a_legacy_caregiver(bridge):
    """§3 on a real server: one claim, one caregiver, every caller converges.

    The pre-existing caregiver is created directly through the repository, the
    way a fixture or admin provisioning would — no claim behind it.
    """
    legacy = bridge.repos.caregivers.create(Caregiver.create(
        "Legacy-Caregiver", auth_subject=bridge.subject, now=T0))
    assert not _claim_exists(bridge.repos, bridge.subject)

    results, errors = _race(
        lambda i: bridge.service.bootstrap_caregiver(
            bridge.subject, request_id=f"req-legacy-{i}").caregiver_id)

    assert errors == [], [repr(e) for e in errors]
    assert set(results) == {legacy.caregiver_id}, "a twin identity was minted"

    claim = bridge.repos.auth_subject_claims.find_for_subject(bridge.subject)
    assert claim.holder_actor_id == legacy.caregiver_id
    assert claim.is_held_by_caregiver
    assert len(_caregivers_for(bridge.repos, bridge.subject)) == 1


def test_the_legacy_caregiver_record_is_not_rewritten_by_the_race(bridge):
    """The existing record is claimed, not edited — under contention too."""
    legacy = bridge.repos.caregivers.create(Caregiver.create(
        "Legacy-Caregiver", auth_subject=bridge.subject, now=T0))
    before = bridge.repos.store.get("pilot_caregivers", legacy.caregiver_id)

    _race(lambda i: bridge.service.bootstrap_caregiver(bridge.subject))

    assert bridge.repos.store.get(
        "pilot_caregivers", legacy.caregiver_id) == before


def test_the_backfilled_claim_then_blocks_a_raw_competing_claim(bridge):
    """After the backfill the claim IS the guard, on a real server."""
    legacy = bridge.repos.caregivers.create(Caregiver.create(
        "Legacy-Caregiver", auth_subject=bridge.subject, now=T0))
    bridge.service.bootstrap_caregiver(bridge.subject)

    with pytest.raises(Exception):
        bridge.repos.auth_subject_claims.claim(AuthSubjectIdentityClaim.build(
            bridge.subject, holder_actor_id="cgvr_interloper",
            holder_actor_type=ActorRole.CAREGIVER, now=T0))

    assert bridge.repos.auth_subject_claims.find_for_subject(
        bridge.subject).holder_actor_id == legacy.caregiver_id


# ===========================================================================
# ambiguity fails closed, under contention
# ===========================================================================

def test_ambiguous_legacy_caregivers_fail_closed_for_every_racer(bridge):
    """§4 on a real server: no claim, no repair, no silent pick of a winner."""
    first = bridge.repos.caregivers.create(Caregiver.create(
        "Legacy-One", auth_subject=bridge.subject, now=T0))
    second = bridge.repos.caregivers.create(Caregiver.create(
        "Legacy-Two", auth_subject=bridge.subject, now=T0))
    assert len(_caregivers_for(bridge.repos, bridge.subject)) == 2

    results, errors = _race(
        lambda i: bridge.service.bootstrap_caregiver(bridge.subject))

    assert results == [], "a bootstrap succeeded against an ambiguous subject"
    assert len(errors) == RACE_WIDTH
    assert all(isinstance(exc, AmbiguousSubjectState) for exc in errors), \
        [type(e).__name__ for e in errors]

    # No claim was written, and neither record was touched.
    assert not _claim_exists(bridge.repos, bridge.subject)
    assert len(_caregivers_for(bridge.repos, bridge.subject)) == 2
    assert bridge.repos.caregivers.get_by_id(first.caregiver_id) == first
    assert bridge.repos.caregivers.get_by_id(second.caregiver_id) == second
    with pytest.raises(AmbiguousAuthSubject):
        bridge.repos.caregivers.get_by_auth_subject(bridge.subject)


def test_a_provider_held_claim_refuses_every_racer(bridge):
    """§5 under contention."""
    bridge.repos.auth_subject_claims.claim(AuthSubjectIdentityClaim.build(
        bridge.subject, holder_actor_id="prov_fictional",
        holder_actor_type=ActorRole.PROVIDER, now=T0))

    results, errors = _race(
        lambda i: bridge.service.bootstrap_caregiver(bridge.subject))

    assert results == []
    assert all(isinstance(exc, SubjectAlreadyHeld) for exc in errors), \
        [type(e).__name__ for e in errors]
    assert _caregivers_for(bridge.repos, bridge.subject) == []
    # The provider claim was not transferred.
    assert bridge.repos.auth_subject_claims.find_for_subject(
        bridge.subject).holder_actor_id == "prov_fictional"


def test_a_claim_naming_a_different_caregiver_refuses_every_racer(bridge):
    """§6 under contention: never overwrite, transfer or repair."""
    legacy = bridge.repos.caregivers.create(Caregiver.create(
        "Legacy-Caregiver", auth_subject=bridge.subject, now=T0))
    other = bridge.repos.caregivers.create(Caregiver.create(
        "Other-Caregiver", auth_subject=bridge.other_subject, now=T0))
    bridge.repos.auth_subject_claims.claim(AuthSubjectIdentityClaim.build(
        bridge.subject, holder_actor_id=other.caregiver_id,
        holder_actor_type=ActorRole.CAREGIVER, now=T0))

    results, errors = _race(
        lambda i: bridge.service.bootstrap_caregiver(bridge.subject))

    assert results == []
    assert all(isinstance(exc, AmbiguousSubjectState) for exc in errors), \
        [type(e).__name__ for e in errors]
    held = bridge.repos.auth_subject_claims.find_for_subject(bridge.subject)
    assert held.holder_actor_id == other.caregiver_id, "the claim was transferred"
    assert bridge.repos.caregivers.get_by_id(legacy.caregiver_id) == legacy
    assert bridge.repos.caregivers.get_by_id(other.caregiver_id) == other


# ===========================================================================
# fault injection — three points, against the real server
# ===========================================================================

def test_a_crash_before_the_claim_leaves_no_trace(bridge):
    """Injection point 1: the caregiver draft is built, nothing is written."""
    repo_class = bridge.repos.auth_subject_claims.__class__
    real_claim = repo_class.claim

    def exploding_claim(self, claim):
        raise RuntimeError("process died before the claim")

    repo_class.claim = exploding_claim
    try:
        with pytest.raises(RuntimeError):
            bridge.service.bootstrap_caregiver(bridge.subject)
    finally:
        repo_class.claim = real_claim

    assert not _claim_exists(bridge.repos, bridge.subject)
    assert _caregivers_for(bridge.repos, bridge.subject) == []

    # The subject was not consumed: a clean retry succeeds.
    caregiver = bridge.service.bootstrap_caregiver(bridge.subject)
    assert caregiver.caregiver_id
    assert bridge.repos.auth_subject_claims.find_for_subject(
        bridge.subject).holder_actor_id == caregiver.caregiver_id


def test_a_crash_inside_the_transaction_persists_neither_record(bridge):
    """Injection point 2: the claim succeeded, the caregiver then failed.

    The decisive case. If the transaction is not genuinely atomic this leaves a
    claim with no caregiver behind it — a subject permanently bound to an
    identity that does not exist, with no release path.
    """
    repo_class = bridge.repos.caregivers.__class__
    real_create = repo_class.create

    def exploding_create(self, caregiver):
        raise RuntimeError("process died mid-transaction")

    repo_class.create = exploding_create
    try:
        with pytest.raises(RuntimeError):
            bridge.service.bootstrap_caregiver(bridge.subject)
    finally:
        repo_class.create = real_create

    assert not _claim_exists(bridge.repos, bridge.subject), "stranded claim"
    assert _caregivers_for(bridge.repos, bridge.subject) == []

    # And the key was not consumed.
    caregiver = bridge.service.bootstrap_caregiver(bridge.subject)
    assert bridge.repos.auth_subject_claims.find_for_subject(
        bridge.subject).holder_actor_id == caregiver.caregiver_id
    assert len(_caregivers_for(bridge.repos, bridge.subject)) == 1


def test_a_crash_after_commit_is_recovered_by_the_retry(bridge):
    """Injection point 3: the write landed; the response never reached the client.

    The client retries, and must receive the identity that was actually created
    rather than a conflict or a second one.
    """
    caregiver = bridge.service.bootstrap_caregiver(bridge.subject)
    committed = caregiver.caregiver_id

    # The retry takes the advisory-read path and resolves the SAME record.
    for attempt in range(3):
        again = bridge.service.bootstrap_caregiver(
            bridge.subject, request_id=f"req-retry-{attempt}")
        assert again.caregiver_id == committed

    assert len(_caregivers_for(bridge.repos, bridge.subject)) == 1
    assert len(bridge.repos.store.query_equals(
        "pilot_auth_subject_claims", "subject_fingerprint",
        subject_fingerprint(bridge.subject))) == 1


def test_a_racing_retry_after_a_crashed_transaction_still_converges(bridge):
    """Fault injection AND contention together.

    Half the threads crash inside the transaction while the other half proceed.
    No stranded claim, and every surviving caller agrees on one identity.
    """
    repo_class = bridge.repos.caregivers.__class__
    real_create = repo_class.create
    crashed = []
    lock = threading.Lock()

    def flaky_create(self, caregiver):
        with lock:
            should_fail = len(crashed) < RACE_WIDTH // 2
            if should_fail:
                crashed.append(caregiver.caregiver_id)
        if should_fail:
            raise RuntimeError("process died mid-transaction")
        return real_create(self, caregiver)

    repo_class.create = flaky_create
    try:
        results, errors = _race(
            lambda i: bridge.service.bootstrap_caregiver(bridge.subject).caregiver_id)
    finally:
        repo_class.create = real_create

    assert len(results) + len(errors) == RACE_WIDTH
    assert all(isinstance(exc, RuntimeError) for exc in errors), \
        [type(e).__name__ for e in errors]
    if results:
        assert len(set(results)) == 1, set(results)
        assert len(_caregivers_for(bridge.repos, bridge.subject)) == 1
        assert bridge.repos.auth_subject_claims.find_for_subject(
            bridge.subject).holder_actor_id == results[0]
    else:
        # Every thread crashed: nothing may be left behind.
        assert not _claim_exists(bridge.repos, bridge.subject)

    # Either way a clean retry resolves to exactly one identity.
    final = bridge.service.bootstrap_caregiver(bridge.subject)
    assert len(_caregivers_for(bridge.repos, bridge.subject)) == 1
    assert resolve_principal(VerifiedToken(subject=bridge.subject),
                             bridge.repos).application_id == final.caregiver_id


# ===========================================================================
# the Parent bridge, persisted for real
# ===========================================================================

def test_the_parent_bridge_round_trips_through_firestore(bridge):
    caregiver = bridge.service.bootstrap_caregiver(bridge.subject)
    bridge.parent.add(bridge.session, bridge.subject)
    principal = resolve_principal(
        VerifiedToken(subject=bridge.subject), bridge.repos)

    result = bridge.service.link_parent_session(principal, bridge.session)

    child = bridge.repos.children.get_by_id(result.child_id)
    assert child.child_id == result.child_id
    link = bridge.repos.source_links.get_by_id(result.source_link_id)
    assert link.source_system is SourceSystem.PARENT
    assert link.external_id == bridge.session
    connection = bridge.repos.caregiver_child.get_by_id(result.connection_id)
    assert connection.caregiver_id == caregiver.caregiver_id
    assert bridge.service.my_children(principal) == [result.child_id]


def test_concurrent_links_of_one_session_leave_no_orphan_child(bridge):
    """The defect this emulator suite caught, now the regression test.

    An earlier revision created the `Child` and the `CaregiverChildConnection`
    BEFORE calling the 0.4A `link_source_system`. The 0.4A claim worked — one
    source link survived — but the two earlier writes had already committed, so
    eight threads left eight children with eight ACTIVE connections and
    `my_children` returned all of them. Orphans visible in the product surface,
    not the inert kind.

    All three writes and both claims now commit together, so a loser writes
    nothing at all.
    """
    caregiver = bridge.service.bootstrap_caregiver(bridge.subject)
    bridge.parent.add(bridge.session, bridge.subject)
    principal = resolve_principal(
        VerifiedToken(subject=bridge.subject), bridge.repos)

    results, errors = _race(
        lambda i: bridge.service.link_parent_session(
            principal, bridge.session, request_id=f"req-link-{i}"))

    assert len(results) == 1, f"{len(results)} writers believed they won"
    assert all(isinstance(exc, ParentSessionLinkContended) for exc in errors), \
        [type(e).__name__ for e in errors]

    # Exactly one of everything, and NO orphan.
    links = bridge.repos.source_links.list_for_external_id(bridge.session)
    assert len(links) == 1, f"{len(links)} source links for one session"
    assert bridge.service.my_children(principal) == [results[0].child_id]

    created = bridge.repos.store.query_equals(
        "pilot_children", "created_by_actor_id", caregiver.caregiver_id)
    assert len(created) == 1, f"{len(created)} children for one session"
    linked_ids = {link.child_id for link in links}
    orphans = [child_id for child_id, _ in created if child_id not in linked_ids]
    assert orphans == [], f"{len(orphans)} orphan children"

    active = bridge.repos.caregiver_child.list_children_for_caregiver(
        caregiver.caregiver_id)
    assert len(active) == 1, f"{len(active)} active connections"


def test_a_contended_loser_can_retry_onto_the_winners_child(bridge):
    """The refusal is transient, and the retry is the idempotent path."""
    bridge.service.bootstrap_caregiver(bridge.subject)
    bridge.parent.add(bridge.session, bridge.subject)
    principal = resolve_principal(
        VerifiedToken(subject=bridge.subject), bridge.repos)

    results, errors = _race(
        lambda i: bridge.service.link_parent_session(principal, bridge.session))
    assert len(results) == 1 and len(errors) == RACE_WIDTH - 1

    retried = bridge.service.link_parent_session(principal, bridge.session)
    assert retried.child_id == results[0].child_id
    assert retried.created is False
    assert len(bridge.service.my_children(principal)) == 1


def test_a_refused_second_session_persists_nothing_in_firestore(bridge):
    bridge.service.bootstrap_caregiver(bridge.subject)
    bridge.parent.add(bridge.session, bridge.subject)
    second = bridge.session + "-second"
    bridge.parent.add(second, bridge.subject)
    principal = resolve_principal(
        VerifiedToken(subject=bridge.subject), bridge.repos)
    bridge.service.link_parent_session(principal, bridge.session)

    with pytest.raises(SecondSessionUnresolved):
        bridge.service.link_parent_session(principal, second)

    assert bridge.repos.source_links.list_for_external_id(second) == []
    assert len(bridge.service.my_children(principal)) == 1


# ===========================================================================
# nothing leaks into a real store
# ===========================================================================

def test_no_stored_document_contains_a_raw_subject_or_a_sentinel(bridge):
    """Swept across the collections 0.5A writes, on the real adapter."""
    bridge.service.bootstrap_caregiver(bridge.subject)
    bridge.parent.add(bridge.session, bridge.subject)
    principal = resolve_principal(
        VerifiedToken(subject=bridge.subject), bridge.repos)
    bridge.service.link_parent_session(principal, bridge.session)

    claims = str(bridge.repos.store.list_all("pilot_auth_subject_claims"))
    assert bridge.subject not in claims
    for sentinel in ALL_SENTINELS:
        assert sentinel not in claims, sentinel

    # Audit metadata, specifically — `actor_auth_subject` is a top-level field
    # that has held the ACTING principal's own subject since 0.2.
    for _doc_id, doc in bridge.repos.store.list_all("pilot_audit_events"):
        metadata = str(doc.get("metadata") or {})
        assert bridge.subject not in metadata
        assert bridge.session not in metadata
        for sentinel in ALL_SENTINELS:
            assert sentinel not in metadata, sentinel


def test_the_claim_collection_is_separate_from_the_child_scoped_claims(bridge):
    """Two primitives, two collections, on a real store."""
    bridge.service.bootstrap_caregiver(bridge.subject)
    bridge.parent.add(bridge.session, bridge.subject)
    principal = resolve_principal(
        VerifiedToken(subject=bridge.subject), bridge.repos)
    bridge.service.link_parent_session(principal, bridge.session)

    subject_claims = bridge.repos.store.list_all("pilot_auth_subject_claims")
    child_claims = bridge.repos.store.list_all("pilot_identity_claims")
    assert subject_claims and child_claims
    subject_ids = {doc_id for doc_id, _ in subject_claims}
    child_ids = {doc_id for doc_id, _ in child_claims}
    assert subject_ids.isdisjoint(child_ids)
    assert all(doc_id.startswith("authsubj__") for doc_id in subject_ids)
    # And no child-scoped claim gained an auth-subject kind.
    for _doc_id, doc in child_claims:
        assert "auth" not in str(doc.get("claim_kind", "")).lower()


def test_a_claim_naming_a_missing_caregiver_is_reported_not_worked_around(bridge):
    """A stranded claim on a real store refuses rather than minting a twin."""
    bridge.repos.auth_subject_claims.claim(AuthSubjectIdentityClaim.build(
        bridge.subject, holder_actor_id="cgvr_does_not_exist",
        holder_actor_type=ActorRole.CAREGIVER, now=T0))

    with pytest.raises(AmbiguousSubjectState):
        bridge.service.bootstrap_caregiver(bridge.subject)
    assert _caregivers_for(bridge.repos, bridge.subject) == []
    with pytest.raises(RecordNotFound):
        bridge.repos.caregivers.get_by_id("cgvr_does_not_exist")


# ===========================================================================
# the PRE-PHI gap, confirmed against a real server
# ===========================================================================

def test_provider_creation_still_bypasses_the_claim_on_a_real_store(bridge):
    """PRE-PHI, OPEN — asserted here too so the blocker is not fake news.

    The caregiver path is closed. `providers.create` does not consult the claim,
    so a provider can bind a subject a caregiver already won, and the subject
    then resolves to neither. Pinned so closing the gap breaks this test and
    forces `SECURITY.md` blocker 4 to be updated.
    """
    from pilot_backend.domain.entities import Provider
    from pilot_backend.domain.enums import ProviderDiscipline

    caregiver = bridge.service.bootstrap_caregiver(bridge.subject)
    assert bridge.repos.auth_subject_claims.find_for_subject(
        bridge.subject).holder_actor_id == caregiver.caregiver_id

    bridge.repos.providers.create(Provider.create(
        "prac_fictional", ProviderDiscipline.SLP, "Provider-Intruder",
        auth_subject=bridge.subject, now=T0))

    assert len(bridge.repos.store.query_equals(
        "pilot_auth_subject_claims", "subject_fingerprint",
        subject_fingerprint(bridge.subject))) == 1
    with pytest.raises(PrincipalResolutionError):
        resolve_principal(VerifiedToken(subject=bridge.subject), bridge.repos)
