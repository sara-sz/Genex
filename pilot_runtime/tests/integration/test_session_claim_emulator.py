"""0.5F-A3 — the handoff capability against a REAL Firestore emulator.

The `pilot_backend` suite runs this same service code over `FakeDocumentStore`,
which is a plain dict and NOT thread-safe. Every claim about CONTENTION is made
here and only here.

What a real server is needed to prove:

**One token, one child, under contention.** Eight threads redeem the same
capability simultaneously. Exactly one child, one source link and one active
connection exist afterwards, and the losers wrote nothing.

**The consumption claim is the mutex, not a flag.** A `consumed` boolean would
be a read-then-write guard — right almost always, wrong exactly when two
redemptions land together, which is the case that matters. The deterministic
claim document id is what makes the second redeemer collide.

**Registration races converge.** Eight concurrent registrations of one token
produce one stored claim and no conflict.

**A spent capability stays spent across processes**, because nothing is cached.

The 0.4A emulator lesson applies throughout: a stalled harness must report
itself rather than return a short result list that reads as a uniqueness win.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth.interface import VerifiedToken
from pilot_backend.auth.resolver import resolve_principal
from pilot_backend.domain.identity_claims import (
    ClaimKind,
    claim_document_id,
    key_digest,
)
from pilot_backend.domain.parent_session_claim import (
    claim_digest,
    generate_claim_token,
)
from pilot_backend.domain.source_link import SourceSystem
from pilot_backend.integration.errors import (
    ParentSessionClaimUnusable,
    ParentSessionLinkContended,
    ParentSessionUnavailable,
)
from pilot_backend.integration.identity_service import (
    IntegrationIdentityService,
)
from pilot_backend.integration.parent_session_claim_service import (
    ClaimRegistrationConflict,
    ParentSessionClaimRegistrationService,
)

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
def handoff(repos, unique_suffix):
    """The services over the REAL emulator-backed repositories."""
    recorder = AuditRecorder(repos.audit_events, environment="test")

    class Bundle:
        pass

    bundle = Bundle()
    bundle.repos = repos
    bundle.recorder = recorder
    bundle.clock = _AdvancingClock(T0)
    bundle.service = IntegrationIdentityService(
        repos=repos, recorder=recorder, now=bundle.clock)
    bundle.registration = ParentSessionClaimRegistrationService(
        repos=repos, now=lambda: T0)

    # Unique per test AND per run: the emulator database is deliberately never
    # reset, and these tests assert ABSOLUTE counts, so a second run would
    # otherwise see the first run's records. Same reasoning as the 0.5A bridge
    # suite, which found this by running twice.
    nonce = "-" + uuid.uuid4().hex[:10]
    bundle.subject = "fictional-subject-handoff" + unique_suffix + nonce
    bundle.other_subject = "fictional-subject-handoff-2" + unique_suffix + nonce
    bundle.session = "fictional-parent-session" + unique_suffix + nonce
    bundle.token = generate_claim_token()
    return bundle


def _race(call, width: int = RACE_WIDTH):
    """Run `call(i)` on `width` threads released together.

    A barrier rather than bare thread starts: without it the first thread
    usually finishes before the last begins, and the test would pass on a
    service with no mutex at all.
    """
    barrier = threading.Barrier(width)
    results, errors = [], []
    lock = threading.Lock()

    def run(i):
        barrier.wait(timeout=30)
        try:
            value = call(i)
        except Exception as exc:  # noqa: BLE001 - classified by the caller
            with lock:
                errors.append(exc)
            return
        with lock:
            results.append(value)

    with ThreadPoolExecutor(max_workers=width) as pool:
        list(pool.map(run, range(width)))

    assert len(results) + len(errors) == width, (
        f"the harness stalled: {len(results)} results + {len(errors)} errors "
        f"for {width} threads")
    return results, errors


def _principal(handoff, subject=None):
    return resolve_principal(
        VerifiedToken(subject=subject or handoff.subject), handoff.repos)


# ---------------------------------------------------------------------------
# registration under contention
# ---------------------------------------------------------------------------

def test_eight_concurrent_registrations_produce_one_claim(handoff):
    digest = claim_digest(handoff.token)
    payload = {"claim_digest": digest, "source_session_id": handoff.session}

    results, errors = _race(lambda i: handoff.registration.register(payload))

    assert errors == [], [type(e).__name__ for e in errors]
    assert len(results) == RACE_WIDTH
    assert sum(1 for r in results if r.created) == 1, (
        "more than one writer believed it created the claim")
    stored = handoff.repos.parent_session_claims.find(digest)
    assert stored is not None
    assert stored.source_session_id == handoff.session


def test_a_racing_registration_for_a_different_session_still_conflicts(handoff):
    digest = claim_digest(handoff.token)
    handoff.registration.register(
        {"claim_digest": digest, "source_session_id": handoff.session})
    with pytest.raises(ClaimRegistrationConflict):
        handoff.registration.register(
            {"claim_digest": digest,
             "source_session_id": handoff.session + "-other"})


def test_the_stored_claim_round_trips_through_firestore(handoff):
    digest = claim_digest(handoff.token)
    handoff.registration.register(
        {"claim_digest": digest, "source_session_id": handoff.session})
    stored = handoff.repos.parent_session_claims.find(digest)
    assert stored.claim_digest == digest
    assert stored.source_system is SourceSystem.PARENT
    assert stored.expires_at > stored.issued_at


def test_the_raw_token_is_absent_from_the_real_store(handoff):
    digest = claim_digest(handoff.token)
    handoff.registration.register(
        {"claim_digest": digest, "source_session_id": handoff.session})
    rows = handoff.repos.store.list_all("pilot_parent_session_claims")
    rendered = repr(rows)
    assert handoff.token not in rendered
    assert digest in rendered


# ---------------------------------------------------------------------------
# redemption under contention — the property that needs a real server
# ---------------------------------------------------------------------------

def test_eight_concurrent_redemptions_mint_exactly_one_child(handoff):
    """THE test. A `consumed` flag would let several of these through."""
    caregiver = handoff.service.bootstrap_caregiver(handoff.subject)
    handoff.registration.register(
        {"claim_digest": claim_digest(handoff.token),
         "source_session_id": handoff.session})
    principal = _principal(handoff)

    results, errors = _race(
        lambda i: handoff.service.consume_parent_session_claim(
            principal, handoff.token, request_id=f"req-claim-{i}"))

    assert len(results) == 1, f"{len(results)} writers believed they won"
    for exc in errors:
        assert isinstance(exc, (ParentSessionLinkContended,
                                ParentSessionClaimUnusable)), type(exc).__name__

    links = handoff.repos.source_links.list_for_external_id(handoff.session)
    assert len(links) == 1, f"{len(links)} source links for one session"
    assert handoff.service.my_children(principal) == [results[0].child_id]

    created = handoff.repos.store.query_equals(
        "pilot_children", "created_by_actor_id", caregiver.caregiver_id)
    assert len(created) == 1, f"{len(created)} children for one capability"
    linked = {link.child_id for link in links}
    orphans = [cid for cid, _ in created if cid not in linked]
    assert orphans == [], f"{len(orphans)} orphan children"

    active = handoff.repos.caregiver_child.list_children_for_caregiver(
        caregiver.caregiver_id)
    assert len(active) == 1, f"{len(active)} active connections"


def test_the_consumption_claim_document_is_the_mutex(handoff):
    handoff.service.bootstrap_caregiver(handoff.subject)
    handoff.registration.register(
        {"claim_digest": claim_digest(handoff.token),
         "source_session_id": handoff.session})
    principal = _principal(handoff)
    _race(lambda i: handoff.service.consume_parent_session_claim(
        principal, handoff.token))

    spent = claim_document_id(
        ClaimKind.PARENT_SESSION_CLAIM,
        key_digest(claim_digest(handoff.token)), 0)
    assert handoff.repos.identity_claims.exists(spent)
    # Generation 1 must NOT exist: a capability is never released, so no second
    # generation can be opened and no second redemption can ever win.
    nxt = claim_document_id(
        ClaimKind.PARENT_SESSION_CLAIM,
        key_digest(claim_digest(handoff.token)), 1)
    assert not handoff.repos.identity_claims.exists(nxt)


def test_a_contended_loser_can_retry_onto_the_winners_child(handoff):
    handoff.service.bootstrap_caregiver(handoff.subject)
    handoff.registration.register(
        {"claim_digest": claim_digest(handoff.token),
         "source_session_id": handoff.session})
    principal = _principal(handoff)

    results, _ = _race(lambda i: handoff.service.consume_parent_session_claim(
        principal, handoff.token))
    assert len(results) == 1

    retried = handoff.service.consume_parent_session_claim(
        principal, handoff.token)
    assert retried.child_id == results[0].child_id
    assert retried.created is False
    assert len(handoff.service.my_children(principal)) == 1


def test_the_pending_claim_is_never_rewritten_by_redemption(handoff):
    digest = claim_digest(handoff.token)
    handoff.service.bootstrap_caregiver(handoff.subject)
    handoff.registration.register(
        {"claim_digest": digest, "source_session_id": handoff.session})
    before = repr(handoff.repos.parent_session_claims.find(digest))

    handoff.service.consume_parent_session_claim(
        _principal(handoff), handoff.token)

    assert repr(handoff.repos.parent_session_claims.find(digest)) == before


def test_a_second_caregiver_racing_the_same_token_gets_no_child(handoff):
    """A stolen capability must not produce a child for the thief after the
    rightful holder has redeemed it."""
    handoff.service.bootstrap_caregiver(handoff.subject)
    handoff.service.bootstrap_caregiver(handoff.other_subject)
    handoff.registration.register(
        {"claim_digest": claim_digest(handoff.token),
         "source_session_id": handoff.session})

    owner = _principal(handoff)
    thief = _principal(handoff, handoff.other_subject)
    handoff.service.consume_parent_session_claim(owner, handoff.token)

    with pytest.raises(ParentSessionUnavailable):
        handoff.service.consume_parent_session_claim(thief, handoff.token)

    assert handoff.service.my_children(thief) == []
    links = handoff.repos.source_links.list_for_external_id(handoff.session)
    assert len(links) == 1


def test_an_unregistered_token_persists_nothing(handoff):
    handoff.service.bootstrap_caregiver(handoff.subject)
    principal = _principal(handoff)
    with pytest.raises(ParentSessionClaimUnusable):
        handoff.service.consume_parent_session_claim(
            principal, generate_claim_token())
    assert handoff.service.my_children(principal) == []


def test_an_expired_capability_persists_nothing_on_a_real_store(handoff):
    handoff.service.bootstrap_caregiver(handoff.subject)
    handoff.registration.register(
        {"claim_digest": claim_digest(handoff.token),
         "source_session_id": handoff.session,
         "ttl_seconds": 1})
    late = T0 + timedelta(seconds=120)
    service = IntegrationIdentityService(
        repos=handoff.repos, recorder=handoff.recorder, now=lambda: late)
    principal = _principal(handoff)

    with pytest.raises(ParentSessionClaimUnusable):
        service.consume_parent_session_claim(principal, handoff.token)

    assert service.my_children(principal) == []
    assert handoff.repos.source_links.list_for_external_id(
        handoff.session) == []


def test_the_child_is_usable_for_the_provider_workflow_afterwards(handoff):
    """A3's reason for existing: not a link-only orphan, a usable child."""
    handoff.service.bootstrap_caregiver(handoff.subject)
    handoff.registration.register(
        {"claim_digest": claim_digest(handoff.token),
         "source_session_id": handoff.session})
    principal = _principal(handoff)

    result = handoff.service.consume_parent_session_claim(
        principal, handoff.token)

    # Visible to the caregiver, and authorized — which is what lets the
    # caregiver invite a clinician next.
    assert handoff.service.my_children(principal) == [result.child_id]
    connection = handoff.repos.caregiver_child.get_by_id(result.connection_id)
    assert connection.child_id == result.child_id
    assert connection.is_active
    identity = handoff.service.whoami(principal)
    assert identity is not None
    assert result.child_id in getattr(identity, "child_ids", [result.child_id])
