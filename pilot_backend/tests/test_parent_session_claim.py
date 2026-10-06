"""The Parent -> Pilot session-claim handoff (0.5F-A3).

Covers the capability domain, the registration service, the private bootstrap
transport, the caregiver consume path, and the properties that matter most:

  * a capability is single-use, and two redeemers cannot both mint a child
  * no uid of either system is stored or accepted anywhere
  * no raw token is ever stored
  * every unusable state is ONE refusal, so the route is not an oracle
  * the 0.5A pull path is behaviourally unchanged

Real `FirestoreRepositories` over the in-memory document store, exactly as the
A2 projection suite does: the codec, the claim mutexes and the transaction are
the things under test, so a fake repository would test nothing.
"""

from __future__ import annotations

import ast
import io
import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from pilot_backend.audit.events import AuditAction
from pilot_backend.auth.verifiers import VerifiedToken
from pilot_backend.domain.connections import CaregiverChildConnection
from pilot_backend.domain.entities import Caregiver, Child
from pilot_backend.domain.enums import CaregiverRelationship
from pilot_backend.domain.identity_claims import (
    ClaimKind,
    claim_document_id,
    key_digest,
)
from pilot_backend.domain.parent_session_claim import (
    CLAIM_DIGEST_DOMAIN,
    CLAIM_TTL_SECONDS,
    MIN_TOKEN_LENGTH,
    ParentSessionClaim,
    ParentSessionClaimError,
    claim_digest,
    generate_claim_token,
)
from pilot_backend.domain.source_link import SourceSystem
from pilot_backend.integration.errors import (
    ParentSessionClaimUnusable,
    ParentSessionUnavailable,
    SecondSessionUnresolved,
    SubjectAlreadyHeld,
)
from pilot_backend.integration.identity_service import (
    IntegrationIdentityService,
)
from pilot_backend.integration.parent_session_claim_service import (
    FORBIDDEN_REGISTRATION_FIELDS,
    REGISTRATION_FIELDS,
    ClaimRegistrationConflict,
    ClaimRegistrationError,
    ParentSessionClaimRegistrationService,
    validate_registration_payload,
)
from pilot_backend.persistence import FakeDocumentStore, FirestoreRepositories
from pilot_backend.repository.interface import DuplicateRecord
from pilot_backend.transport.bootstrap_wsgi import (
    BODY_KEYS,
    BOOTSTRAP_ROUTE,
    BootstrapApp,
)

SESSION = "4af92e4b-477c-40ea-8674-e77db6414860"
OTHER_SESSION = "11111111-2222-3333-4444-555555555555"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
TOKEN = "T" * 43
OTHER_TOKEN = "U" * 43


def _repos():
    return FirestoreRepositories(FakeDocumentStore())


def _caregiver(repos, subject: str, name: str = "Fictional Caregiver"):
    caregiver = Caregiver.create(name, auth_subject=subject, now=NOW)
    repos.caregivers.create(caregiver)
    return caregiver


def _service(repos, *, now=None, recorder=None):
    return IntegrationIdentityService(repos=repos, now=now or (lambda: NOW),
                                      recorder=recorder)


def _principal(repos, subject: str):
    from pilot_backend.auth.resolver import resolve_principal

    return resolve_principal(VerifiedToken(subject=subject), repos)


def _register(repos, token=TOKEN, session=SESSION, *, now=NOW, ttl=None):
    svc = ParentSessionClaimRegistrationService(repos=repos, now=lambda: now)
    payload = {"claim_digest": claim_digest(token), "source_session_id": session}
    if ttl is not None:
        payload["ttl_seconds"] = ttl
    return svc.register(payload)


# ---------------------------------------------------------------------------
# 1. the capability domain
# ---------------------------------------------------------------------------

def test_a_generated_token_has_full_entropy_and_is_unique():
    tokens = {generate_claim_token() for _ in range(200)}
    assert len(tokens) == 200, "two generated tokens collided"
    for t in tokens:
        # 32 bytes base64url-encoded, unpadded.
        assert len(t) >= 43


def test_the_token_generator_uses_the_csprng_not_random():
    """`random` is a Mersenne Twister and reconstructible from its output."""
    source = pathlib.Path(
        "pilot_backend/domain/parent_session_claim.py").read_text()
    tree = ast.parse(source)
    imported = {n.names[0].name for n in ast.walk(tree)
                if isinstance(n, ast.Import)}
    assert "secrets" in imported
    assert "random" not in imported
    assert "random.random" not in source and "random.choice" not in source


def test_the_digest_is_domain_separated():
    """A bare sha256 of the token would collide with any other sha256 of it."""
    import hashlib

    bare = hashlib.sha256(TOKEN.encode()).hexdigest()
    assert claim_digest(TOKEN) != bare
    assert claim_digest(TOKEN) == hashlib.sha256(
        f"{CLAIM_DIGEST_DOMAIN}\x00{TOKEN}".encode()).hexdigest()


def test_the_digest_is_stable_and_distinguishes_tokens():
    assert claim_digest(TOKEN) == claim_digest(TOKEN)
    assert claim_digest(TOKEN) != claim_digest(OTHER_TOKEN)
    assert len(claim_digest(TOKEN)) == 64


def test_a_short_token_is_refused_before_any_lookup():
    with pytest.raises(ParentSessionClaimError):
        claim_digest("x" * (MIN_TOKEN_LENGTH - 1))


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_an_empty_token_is_refused(bad):
    with pytest.raises(ParentSessionClaimError):
        claim_digest(bad)


def test_issue_takes_a_digest_and_never_a_raw_token():
    """The constructor cannot be where a raw token is accidentally stored."""
    claim = ParentSessionClaim.issue(claim_digest(TOKEN), SESSION, now=NOW)
    assert claim.claim_digest == claim_digest(TOKEN)
    assert TOKEN not in json.dumps(claim.__dict__, default=str)


def test_the_ttl_ceiling_cannot_be_exceeded():
    with pytest.raises(ParentSessionClaimError):
        ParentSessionClaim.issue(claim_digest(TOKEN), SESSION, now=NOW,
                                 ttl_seconds=CLAIM_TTL_SECONDS + 1)


@pytest.mark.parametrize("ttl", [0, -1, -600])
def test_a_non_positive_ttl_is_refused(ttl):
    with pytest.raises(ParentSessionClaimError):
        ParentSessionClaim.issue(claim_digest(TOKEN), SESSION, now=NOW,
                                 ttl_seconds=ttl)


def test_a_shorter_ttl_is_permitted():
    claim = ParentSessionClaim.issue(claim_digest(TOKEN), SESSION, now=NOW,
                                     ttl_seconds=60)
    assert claim.expires_at == NOW + timedelta(seconds=60)


def test_the_default_ttl_is_a_handoff_window_not_hours():
    assert 300 <= CLAIM_TTL_SECONDS <= 900, (
        "a handoff capability must live minutes, not hours")


def test_expiry_is_inclusive_of_the_boundary():
    claim = ParentSessionClaim.issue(claim_digest(TOKEN), SESSION, now=NOW)
    assert not claim.is_expired_at(claim.expires_at - timedelta(seconds=1))
    assert claim.is_expired_at(claim.expires_at)
    assert claim.is_expired_at(claim.expires_at + timedelta(seconds=1))


@pytest.mark.parametrize("digest", ["", "abc", "z" * 64, "A" * 64, "f" * 63])
def test_a_malformed_digest_is_refused(digest):
    with pytest.raises(ParentSessionClaimError):
        ParentSessionClaim(claim_digest=digest,
                           source_system=SourceSystem.PARENT,
                           source_session_id=SESSION,
                           issued_at=NOW,
                           expires_at=NOW + timedelta(seconds=60))


def test_only_a_parent_claim_can_be_constructed():
    with pytest.raises(ParentSessionClaimError):
        ParentSessionClaim(claim_digest=claim_digest(TOKEN),
                           source_system=SourceSystem.THERAPIST,
                           source_session_id=SESSION,
                           issued_at=NOW,
                           expires_at=NOW + timedelta(seconds=60))


def test_the_claim_record_is_immutable_and_has_no_consumed_flag():
    claim = ParentSessionClaim.issue(claim_digest(TOKEN), SESSION, now=NOW)
    with pytest.raises(Exception):
        claim.source_session_id = "other"  # type: ignore[misc]
    assert not hasattr(claim, "consumed")
    assert not any(n.startswith("with_") or n in ("expire", "revoke", "consume")
                   for n in dir(claim))


def test_the_claim_carries_no_uid_of_either_system():
    claim = ParentSessionClaim.issue(claim_digest(TOKEN), SESSION, now=NOW)
    fields = set(claim.__dataclass_fields__)
    assert fields == {"claim_digest", "source_system", "source_session_id",
                      "issued_at", "expires_at", "schema_version"}
    for forbidden in ("owner_uid", "parent_uid", "pilot_uid", "auth_subject",
                      "caregiver_id", "child_id", "external_owner_ref",
                      "email", "subject"):
        assert forbidden not in fields


# ---------------------------------------------------------------------------
# 2. registration (Phase A, the private boundary)
# ---------------------------------------------------------------------------

def test_a_registration_stores_the_claim_and_reports_created():
    repos = _repos()
    result = _register(repos)
    assert result.created is True
    stored = repos.parent_session_claims.find(claim_digest(TOKEN))
    assert stored is not None
    assert stored.source_session_id == SESSION
    assert stored.source_system is SourceSystem.PARENT


def test_the_stored_document_contains_no_raw_token():
    repos = _repos()
    _register(repos)
    raw = json.dumps(repos.store.list_all("pilot_parent_session_claims"),
                     default=str)
    assert TOKEN not in raw
    assert claim_digest(TOKEN) in raw


def test_an_exact_replay_is_idempotent_and_does_not_rewrite():
    repos = _repos()
    first = _register(repos)
    before = json.dumps(repos.store.list_all("pilot_parent_session_claims"),
                        default=str)
    second = _register(repos, now=NOW + timedelta(seconds=30))
    after = json.dumps(repos.store.list_all("pilot_parent_session_claims"),
                       default=str)
    assert second.created is False
    assert second.claim.expires_at == first.claim.expires_at
    assert before == after, "an idempotent replay rewrote the stored claim"


def test_the_same_digest_for_a_different_session_is_a_conflict():
    repos = _repos()
    _register(repos)
    with pytest.raises(ClaimRegistrationConflict):
        _register(repos, session=OTHER_SESSION)


def test_two_registrations_of_different_tokens_coexist():
    repos = _repos()
    _register(repos, token=TOKEN, session=SESSION)
    _register(repos, token=OTHER_TOKEN, session=OTHER_SESSION)
    assert len(repos.store.list_all("pilot_parent_session_claims")) == 2


@pytest.mark.parametrize("field", sorted(set(FORBIDDEN_REGISTRATION_FIELDS)))
def test_every_forbidden_registration_field_is_refused(field):
    payload = {"claim_digest": claim_digest(TOKEN),
               "source_session_id": SESSION, field: "anything"}
    with pytest.raises(ClaimRegistrationError):
        validate_registration_payload(payload)


def test_a_raw_token_in_the_registration_is_refused():
    """The single most important refusal: the Pilot must never see a token."""
    assert "claim_token" in FORBIDDEN_REGISTRATION_FIELDS
    with pytest.raises(ClaimRegistrationError):
        validate_registration_payload({
            "claim_digest": claim_digest(TOKEN),
            "source_session_id": SESSION,
            "claim_token": TOKEN})


def test_an_unexpected_field_is_refused_not_ignored():
    with pytest.raises(ClaimRegistrationError):
        validate_registration_payload({
            "claim_digest": claim_digest(TOKEN),
            "source_session_id": SESSION, "surprise": 1})


@pytest.mark.parametrize("missing", ["claim_digest", "source_session_id"])
def test_an_incomplete_registration_is_refused(missing):
    payload = {"claim_digest": claim_digest(TOKEN),
               "source_session_id": SESSION}
    payload.pop(missing)
    with pytest.raises(ClaimRegistrationError):
        validate_registration_payload(payload)


@pytest.mark.parametrize("digest", ["short", "Z" * 64, "", "f" * 65, "g" * 64])
def test_a_malformed_digest_is_refused_before_storage(digest):
    with pytest.raises(ClaimRegistrationError):
        validate_registration_payload({"claim_digest": digest,
                                       "source_session_id": SESSION})


def test_an_uppercase_digest_is_normalised_rather_than_refused():
    """sha256 hex is case-insensitive in meaning; storage must be canonical."""
    out = validate_registration_payload({
        "claim_digest": claim_digest(TOKEN).upper(),
        "source_session_id": SESSION})
    assert out["claim_digest"] == claim_digest(TOKEN)


@pytest.mark.parametrize("ttl", [CLAIM_TTL_SECONDS + 1, 0, -5, 86400])
def test_the_pilot_enforces_the_ttl_ceiling_itself(ttl):
    """Never trust Parent to have applied it."""
    with pytest.raises(ClaimRegistrationError):
        validate_registration_payload({"claim_digest": claim_digest(TOKEN),
                                       "source_session_id": SESSION,
                                       "ttl_seconds": ttl})


def test_a_boolean_ttl_is_refused():
    """`True` is an int in Python and would become a one-second window."""
    with pytest.raises(ClaimRegistrationError):
        validate_registration_payload({"claim_digest": claim_digest(TOKEN),
                                       "source_session_id": SESSION,
                                       "ttl_seconds": True})


def test_registration_creates_no_identity_at_all():
    """Phase A registers a capability. It must mint nothing."""
    repos = _repos()
    _register(repos)
    for collection in ("pilot_children", "pilot_caregivers",
                       "pilot_source_system_links",
                       "pilot_caregiver_child_connections",
                       "pilot_identity_claims"):
        assert repos.store.list_all(collection) == [], collection


# ---------------------------------------------------------------------------
# 3. the private bootstrap transport
# ---------------------------------------------------------------------------

def _environ(body: dict, *, method="POST", path=BOOTSTRAP_ROUTE, headers=None):
    raw = json.dumps(body).encode()
    env = {"REQUEST_METHOD": method, "PATH_INFO": path,
           "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}
    env.update(headers or {})
    return env


def _call(app, environ):
    captured = {}

    def start_response(status, headers):
        captured["status"] = int(status.split()[0])
        captured["headers"] = headers

    body = app(environ, start_response)
    return captured["status"], json.loads(b"".join(body)), captured["headers"]


def _app(repos, **kw):
    return BootstrapApp(
        service_factory=lambda: ParentSessionClaimRegistrationService(
            repos=repos, now=lambda: NOW), **kw)


def test_the_transport_registers_and_returns_201_then_200():
    repos = _repos()
    app = _app(repos)
    body = {"claim_digest": claim_digest(TOKEN), "source_session_id": SESSION}
    status, payload, _ = _call(app, _environ(body))
    assert status == 201 and payload["created"] is True
    status, payload, _ = _call(app, _environ(body))
    assert status == 200 and payload["created"] is False


def test_the_response_echoes_neither_digest_nor_session_id():
    repos = _repos()
    status, payload, _ = _call(_app(repos), _environ(
        {"claim_digest": claim_digest(TOKEN), "source_session_id": SESSION}))
    rendered = json.dumps(payload)
    assert SESSION not in rendered
    assert claim_digest(TOKEN) not in rendered
    assert set(payload) == {"registered", "created", "expires_at"}


def test_a_conflict_is_409():
    repos = _repos()
    app = _app(repos)
    _call(app, _environ({"claim_digest": claim_digest(TOKEN),
                         "source_session_id": SESSION}))
    status, payload, _ = _call(app, _environ(
        {"claim_digest": claim_digest(TOKEN),
         "source_session_id": OTHER_SESSION}))
    assert status == 409 and payload == {"error": "integrity conflict"}


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_only_post_is_allowed(method):
    status, _, _ = _call(_app(_repos()), _environ({}, method=method))
    assert status == 405


def test_an_unknown_path_is_404():
    status, _, _ = _call(_app(_repos()),
                         _environ({}, path="/internal/something-else"))
    assert status == 404


def test_no_cors_header_is_ever_emitted():
    repos = _repos()
    _, _, headers = _call(_app(repos), _environ(
        {"claim_digest": claim_digest(TOKEN), "source_session_id": SESSION},
        headers={"HTTP_ORIGIN": "https://evil.example"}))
    names = {k.lower() for k, _ in headers}
    assert not any(n.startswith("access-control-") for n in names)
    # Comments are STRIPPED before scanning: this module explains in prose
    # why it emits no CORS header, and a whole-file substring check would
    # flag that explanation. The same trap the A2 CI gate hit.
    effective = "\n".join(
        line.split("#", 1)[0] for line in
        pathlib.Path("pilot_backend/transport/bootstrap_wsgi.py")
        .read_text().split('\"\"\"', 2)[2].splitlines())
    assert "Access-Control" not in effective


def test_the_transport_body_keys_exclude_a_raw_token_and_identity():
    assert set(BODY_KEYS) == {"claim_digest", "source_session_id",
                              "ttl_seconds"}
    for forbidden in ("claim_token", "child_id", "caregiver_id", "parent_uid",
                      "auth_subject", "owner_uid", "external_owner_ref"):
        assert forbidden not in BODY_KEYS


def test_an_oversized_body_is_413_and_never_parsed():
    repos = _repos()
    env = {"REQUEST_METHOD": "POST", "PATH_INFO": BOOTSTRAP_ROUTE,
           "CONTENT_LENGTH": str(64 * 1024),
           "wsgi.input": io.BytesIO(b"{}")}
    status, payload, _ = _call(_app(repos), env)
    assert status == 413 and payload == {"error": "not accepted"}


def test_an_enabled_verifier_runs_before_the_body_is_read():
    class _Boom:
        def read(self, *a):
            raise AssertionError("the body was read before verification")

    class _Reject:
        def verify(self, header):
            raise ValueError("no")

    env = {"REQUEST_METHOD": "POST", "PATH_INFO": BOOTSTRAP_ROUTE,
           "CONTENT_LENGTH": "2", "wsgi.input": _Boom()}
    status, payload, _ = _call(_app(_repos(), verifier=_Reject()), env)
    assert status == 401 and payload == {"error": "not permitted"}


def test_iam_only_reads_no_token_at_all():
    app = _app(_repos())
    assert app.verifies_tokens is False

    def _explode(environ):
        raise AssertionError("the header was read in iam_only mode")

    app = _app(_repos(), bearer_reader=_explode)
    status, _, _ = _call(app, _environ({"claim_digest": claim_digest(TOKEN),
                                        "source_session_id": SESSION}))
    assert status == 201


def test_the_bootstrap_app_is_not_the_projection_app():
    """Two separate applications; neither route is reachable on the other."""
    from pilot_backend.transport.projection_wsgi import PROJECTION_ROUTE

    assert BOOTSTRAP_ROUTE != PROJECTION_ROUTE
    status, _, _ = _call(_app(_repos()), _environ({}, path=PROJECTION_ROUTE))
    assert status == 404


# ---------------------------------------------------------------------------
# 4. consumption (Phase B, the caregiver route)
# ---------------------------------------------------------------------------

def test_a_caregiver_redeems_a_claim_and_gets_a_canonical_child():
    repos = _repos()
    _caregiver(repos, "pilot-subject-a")
    _register(repos)
    service = _service(repos)
    principal = _principal(repos, "pilot-subject-a")

    result = service.consume_parent_session_claim(principal, TOKEN)

    assert result.created is True
    assert result.child_id.startswith("chld_")
    assert result.connection_id
    link = repos.source_links.list_for_external_id(SESSION)[0]
    assert link.child_id == result.child_id
    assert link.source_system is SourceSystem.PARENT
    assert link.external_id == SESSION
    assert link.is_active


def test_the_child_appears_for_the_caregiver_afterwards():
    """The whole point of A3: a usable child, not an orphan."""
    repos = _repos()
    _caregiver(repos, "pilot-subject-a")
    _register(repos)
    service = _service(repos)
    principal = _principal(repos, "pilot-subject-a")
    result = service.consume_parent_session_claim(principal, TOKEN)
    assert service.my_children(principal) == [result.child_id]


def test_consumption_writes_a_single_use_claim():
    repos = _repos()
    _caregiver(repos, "pilot-subject-a")
    _register(repos)
    service = _service(repos)
    result = service.consume_parent_session_claim(
        _principal(repos, "pilot-subject-a"), TOKEN)
    spent = claim_document_id(ClaimKind.PARENT_SESSION_CLAIM,
                              key_digest(claim_digest(TOKEN)), 0)
    assert repos.identity_claims.exists(spent)
    held = repos.identity_claims.get_by_id(spent)
    assert held.child_id == result.child_id


def test_the_pending_claim_record_is_not_mutated_by_consumption():
    repos = _repos()
    _caregiver(repos, "pilot-subject-a")
    _register(repos)
    before = json.dumps(repos.store.list_all("pilot_parent_session_claims"),
                        default=str)
    _service(repos).consume_parent_session_claim(
        _principal(repos, "pilot-subject-a"), TOKEN)
    after = json.dumps(repos.store.list_all("pilot_parent_session_claims"),
                       default=str)
    assert before == after, "consumption edited the pending claim"


def test_an_exact_replay_by_the_same_caregiver_is_idempotent():
    repos = _repos()
    _caregiver(repos, "pilot-subject-a")
    _register(repos)
    service = _service(repos)
    principal = _principal(repos, "pilot-subject-a")

    first = service.consume_parent_session_claim(principal, TOKEN)
    second = service.consume_parent_session_claim(principal, TOKEN)

    assert second.created is False
    assert second.child_id == first.child_id
    assert len(repos.store.list_all("pilot_children")) == 1
    assert len(repos.store.list_all("pilot_source_system_links")) == 1


def test_a_replay_still_works_after_the_capability_expires():
    """The owner already holds the child; a retry must not depend on the TTL."""
    repos = _repos()
    _caregiver(repos, "pilot-subject-a")
    _register(repos)
    principal = _principal(repos, "pilot-subject-a")
    _service(repos).consume_parent_session_claim(principal, TOKEN)

    late = NOW + timedelta(seconds=CLAIM_TTL_SECONDS + 60)
    result = _service(repos, now=lambda: late).consume_parent_session_claim(
        _principal(repos, "pilot-subject-a"), TOKEN)
    assert result.created is False


def test_a_different_caregiver_presenting_a_consumed_token_fails_closed():
    repos = _repos()
    _caregiver(repos, "pilot-subject-a")
    _caregiver(repos, "pilot-subject-b", name="Other Caregiver")
    _register(repos)
    service = _service(repos)
    service.consume_parent_session_claim(
        _principal(repos, "pilot-subject-a"), TOKEN)

    with pytest.raises(ParentSessionUnavailable):
        service.consume_parent_session_claim(
            _principal(repos, "pilot-subject-b"), TOKEN)

    assert len(repos.store.list_all("pilot_children")) == 1


def test_an_unknown_token_is_refused():
    repos = _repos()
    _caregiver(repos, "pilot-subject-a")
    with pytest.raises(ParentSessionClaimUnusable):
        _service(repos).consume_parent_session_claim(
            _principal(repos, "pilot-subject-a"), OTHER_TOKEN)


def test_a_malformed_token_is_refused_with_the_same_error():
    """Length and shape must not be probeable."""
    repos = _repos()
    _caregiver(repos, "pilot-subject-a")
    with pytest.raises(ParentSessionClaimUnusable):
        _service(repos).consume_parent_session_claim(
            _principal(repos, "pilot-subject-a"), "short")


def test_an_expired_claim_is_refused_and_persists_nothing():
    repos = _repos()
    _caregiver(repos, "pilot-subject-a")
    _register(repos)
    late = NOW + timedelta(seconds=CLAIM_TTL_SECONDS)
    with pytest.raises(ParentSessionClaimUnusable):
        _service(repos, now=lambda: late).consume_parent_session_claim(
            _principal(repos, "pilot-subject-a"), TOKEN)
    assert repos.store.list_all("pilot_children") == []
    assert repos.store.list_all("pilot_source_system_links") == []


def test_a_claim_one_second_before_expiry_still_works():
    repos = _repos()
    _caregiver(repos, "pilot-subject-a")
    _register(repos)
    just = NOW + timedelta(seconds=CLAIM_TTL_SECONDS - 1)
    result = _service(repos, now=lambda: just).consume_parent_session_claim(
        _principal(repos, "pilot-subject-a"), TOKEN)
    assert result.created is True


def test_a_spent_token_with_an_ended_link_is_still_refused():
    """Fail closed rather than mint a second child for a spent capability."""
    repos = _repos()
    _caregiver(repos, "pilot-subject-a")
    _register(repos)
    service = _service(repos)
    principal = _principal(repos, "pilot-subject-a")
    result = service.consume_parent_session_claim(principal, TOKEN)

    link = repos.source_links.list_for_external_id(SESSION)[0]
    repos.source_links.update(link.end(reason="test", actor_id="t", now=NOW))

    with pytest.raises(ParentSessionClaimUnusable):
        service.consume_parent_session_claim(principal, TOKEN)
    assert len(repos.store.list_all("pilot_children")) == 1


def test_a_provider_cannot_redeem_a_family_capability():
    from pilot_backend.domain.entities import Practice, Provider
    from pilot_backend.domain.enums import ProviderDiscipline
    from pilot_backend.domain.roles import ActorRole

    repos = _repos()
    practice = Practice.create("Fictional Practice", now=NOW)
    repos.practices.create(practice)
    provider = Provider.create(practice.practice_id, ProviderDiscipline.SLP,
                               "Hannah", auth_subject="provider-subject",
                               now=NOW)
    repos.providers.create(provider)
    _register(repos)

    principal = _principal(repos, "provider-subject")
    assert principal.role is ActorRole.PROVIDER
    with pytest.raises(SubjectAlreadyHeld):
        _service(repos).consume_parent_session_claim(principal, TOKEN)
    assert repos.store.list_all("pilot_children") == []


def test_the_second_session_rule_still_fails_closed():
    repos = _repos()
    _caregiver(repos, "pilot-subject-a")
    _register(repos, token=TOKEN, session=SESSION)
    _register(repos, token=OTHER_TOKEN, session=OTHER_SESSION)
    service = _service(repos)
    principal = _principal(repos, "pilot-subject-a")
    service.consume_parent_session_claim(principal, TOKEN)

    with pytest.raises(SecondSessionUnresolved):
        service.consume_parent_session_claim(principal, OTHER_TOKEN)
    assert len(repos.store.list_all("pilot_children")) == 1


def test_the_browser_cannot_choose_the_child_id():
    """The only input is a token; the child id is minted server-side."""
    import inspect

    sig = inspect.signature(
        IntegrationIdentityService.consume_parent_session_claim)
    assert set(sig.parameters) == {"self", "principal", "claim_token",
                                   "request_id"}


def test_consumption_audits_the_same_four_events_as_the_pull_path():
    from pilot_backend.audit.recorder import AuditRecorder

    repos = _repos()
    _caregiver(repos, "pilot-subject-a")
    _register(repos)
    recorder = AuditRecorder(repos.audit_events, environment="test")
    _service(repos, recorder=recorder).consume_parent_session_claim(
        _principal(repos, "pilot-subject-a"), TOKEN)
    actions = {doc["action"] for _id, doc
               in repos.store.list_all("pilot_audit_events")}
    for expected in (AuditAction.CHILD_CREATED,
                     AuditAction.CAREGIVER_CHILD_LINKED,
                     AuditAction.SOURCE_LINK_CREATED,
                     AuditAction.PARENT_SESSION_LINKED):
        assert expected.value in actions


def test_no_audit_event_carries_the_token_or_its_digest():
    from pilot_backend.audit.recorder import AuditRecorder

    repos = _repos()
    _caregiver(repos, "pilot-subject-a")
    _register(repos)
    recorder = AuditRecorder(repos.audit_events, environment="test")
    _service(repos, recorder=recorder).consume_parent_session_claim(
        _principal(repos, "pilot-subject-a"), TOKEN)
    rendered = json.dumps(repos.store.list_all("pilot_audit_events"),
                          default=str)
    assert TOKEN not in rendered
    assert claim_digest(TOKEN) not in rendered


# ---------------------------------------------------------------------------
# 5. the 0.5A pull path is unchanged
# ---------------------------------------------------------------------------

def test_the_pull_path_still_refuses_when_no_parent_source_is_configured():
    repos = _repos()
    _caregiver(repos, "pilot-subject-a")
    service = _service(repos)  # no parent_source injected
    with pytest.raises(ParentSessionUnavailable):
        service.link_parent_session(_principal(repos, "pilot-subject-a"),
                                    SESSION)


def test_the_pull_path_signature_is_untouched():
    import inspect

    sig = inspect.signature(IntegrationIdentityService.link_parent_session)
    assert set(sig.parameters) == {"self", "principal", "session_id",
                                   "request_id"}


def test_the_pull_path_writes_no_consumption_claim():
    """Only the capability path spends a capability."""
    from pilot_backend.integration.parent_source import (
        InMemoryParentSessionSource,
    )

    repos = _repos()
    _caregiver(repos, "pilot-subject-a")
    source = InMemoryParentSessionSource({SESSION: "pilot-subject-a"})
    service = IntegrationIdentityService(repos=repos, parent_source=source,
                                         now=lambda: NOW)
    service.link_parent_session(_principal(repos, "pilot-subject-a"), SESSION)
    # No capability was presented, so none can have been spent. Checked by the
    # deterministic id rather than by enumerating claims.
    for token in (TOKEN, OTHER_TOKEN):
        spent = claim_document_id(ClaimKind.PARENT_SESSION_CLAIM,
                                  key_digest(claim_digest(token)), 0)
        assert not repos.identity_claims.exists(spent)


# ---------------------------------------------------------------------------
# 6. structural guarantees
# ---------------------------------------------------------------------------

def test_the_claim_repository_has_no_mutating_method():
    from pilot_backend.persistence.firestore_repos import (
        FirestoreParentSessionClaimRepository,
    )

    public = {n for n in dir(FirestoreParentSessionClaimRepository)
              if not n.startswith("_")}
    assert public == {"create", "find", "record_type", "model"}
    for forbidden in ("update", "set", "delete", "overwrite", "consume", "end"):
        assert forbidden not in public


A2_FROZEN_FILES = (
    "pilot_backend/transport/projection_wsgi.py",
    "pilot_backend/integration/baseline_projection_service.py",
    "pilot_backend/domain/parent_baseline_projection.py",
    "pilot_runtime/projection_server.py",
    "pilot_runtime/deploy/Dockerfile.projection",
    "pilot_runtime/deploy/requirements-projection.txt",
    "pilot_runtime/deploy/cloudbuild-projection.yaml",
    "pilot_runtime/deploy/gcloudignore-projection",
)

A2_TAG = "october-pilot-0.5f-a2-parent-baseline-projection"


@pytest.mark.parametrize("path", A2_FROZEN_FILES)
def test_the_a2_projection_artifact_is_byte_identical_to_its_frozen_tag(path):
    """Stronger than a substring scan: the frozen A2 files must not change.

    A `"claim" not in source` check would pass while someone rewrote the file,
    and would FAIL on the word "claims" appearing in the A2 prose — which it
    does. Comparing the git blob hash to the tag settles it exactly.
    """
    import subprocess

    want = subprocess.run(["git", "rev-parse", f"{A2_TAG}:{path}"],
                          capture_output=True, text=True)
    if want.returncode != 0:
        pytest.skip("the A2 tag is not present in this checkout")
    got = subprocess.run(["git", "hash-object", path],
                         capture_output=True, text=True, check=True)
    assert got.stdout.strip() == want.stdout.strip(), (
        f"{path} differs from the frozen A2 tag")


def test_the_bootstrap_transport_never_logs():
    source = pathlib.Path("pilot_backend/transport/bootstrap_wsgi.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            assert name not in ("print", "warning", "info", "debug", "error",
                                "exception", "getLogger")
    assert "logging" not in {n.names[0].name for n in ast.walk(tree)
                             if isinstance(n, ast.Import)}


def test_token_field_names_are_forbidden_in_logs():
    from pilot_backend.observability.safe_logging import (
        ALLOWED_LOG_FIELDS,
        FORBIDDEN_LOG_FIELDS,
    )

    for name in ("claim_token", "claim_digest", "raw_token", "capability",
                 "capability_token", "handoff_token", "token"):
        assert name in FORBIDDEN_LOG_FIELDS
        assert name not in ALLOWED_LOG_FIELDS


def test_the_registration_fields_are_exactly_three():
    assert set(REGISTRATION_FIELDS) == {"claim_digest", "source_session_id",
                                        "ttl_seconds"}


# ---------------------------------------------------------------------------
# 7. the browser consume route, over real HTTP
# ---------------------------------------------------------------------------
#
# Added because the mutation sweep found a genuine coverage gap: adding
# `child_id` to CONSUME_CLAIM_FIELDS survived every test above. It survived
# because `read_json_body` raises eagerly when a route allowlist intersects
# IDENTITY_FIELDS — which would have broken EVERY request to this route — and
# nothing here exercised the route over HTTP at all.

from pilot_backend.tests.test_integration_identity import (  # noqa: E402
    CAREGIVER_ALPHA_SUBJECT,
    build_http,
    call,
)

CONSUME_PATH = "/pilot/parent-session-claims"
ALPHA_BEARER = "Bearer token-caregiver-alpha"


def _http_with_claim(token=TOKEN, session=SESSION, ttl=None):
    """A live claim.

    Registered at the REAL current time, not the fixed `NOW` the unit tests
    use: `build_http` assembles the identity service with the real clock, so a
    claim issued in a fixed past would already be expired and every success
    assertion below would 403 for the wrong reason. (It did.)
    """
    from datetime import datetime as _dt, timezone as _tz

    http = build_http()
    _register(http.repos, token=token, session=session,
              now=_dt.now(_tz.utc), ttl=ttl)
    return http


def _consume(http, token=TOKEN, *, bearer=ALPHA_BEARER, body=None,
             method="POST"):
    payload = {"claim_token": token} if body is None else body
    raw = json.dumps(payload).encode()
    return call(http.app, CONSUME_PATH, method=method, bearer=bearer, body=raw)


def test_the_consume_route_mints_a_child_over_http():
    http = _http_with_claim()
    status, payload, _ = _consume(http)
    assert status == 200, payload
    assert payload["created"] is True
    assert payload["child_id"].startswith("chld_")
    assert "request_id" in payload


def test_the_consume_route_works_for_the_declared_allowlist():
    """Kills the B1 mutation: an allowlist containing an IDENTITY_FIELD makes
    `read_json_body` raise for EVERY request, so a valid redemption would 403."""
    from pilot_backend.transport.body import IDENTITY_FIELDS
    from pilot_backend.transport.wsgi_app import CONSUME_CLAIM_FIELDS

    assert not (set(CONSUME_CLAIM_FIELDS) & IDENTITY_FIELDS)
    http = _http_with_claim()
    status, _, _ = _consume(http)
    assert status == 200


def test_an_unauthenticated_consume_is_refused():
    http = _http_with_claim()
    status, _, _ = _consume(http, bearer=None)
    assert status in (401, 403)


def test_a_child_id_in_the_consume_body_is_refused():
    """The browser must not be able to name a canonical child."""
    http = _http_with_claim()
    status, payload, _ = _consume(
        http, body={"claim_token": TOKEN, "child_id": "chld_somebody_else"})
    assert status == 403
    assert payload == {"error": "not permitted"}


@pytest.mark.parametrize("extra", [
    {"claim_token": TOKEN, "caregiver_id": "cgvr_x"},
    {"claim_token": TOKEN, "auth_subject": "someone"},
    {"claim_token": TOKEN, "source_session_id": "other-session"},
    {"claim_token": TOKEN, "surprise": 1},
])
def test_any_extra_consume_body_field_is_refused(extra):
    http = _http_with_claim()
    status, _, _ = _consume(http, body=extra)
    assert status == 403


def test_a_missing_claim_token_is_refused():
    http = _http_with_claim()
    status, _, _ = _consume(http, body={})
    assert status == 403


def test_an_unknown_token_over_http_is_the_same_refusal_as_a_wrong_body():
    """Every unusable state is ONE response, so the route is not an oracle."""
    http = _http_with_claim()
    unknown = _consume(http, token=OTHER_TOKEN)
    malformed = _consume(http, token="short")
    bad_body = _consume(http, body={"claim_token": TOKEN, "child_id": "x"})
    assert unknown[0] == malformed[0] == bad_body[0] == 403
    assert unknown[1] == malformed[1] == bad_body[1] == {"error": "not permitted"}


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH"])
def test_only_post_is_accepted_on_the_consume_route(method):
    http = _http_with_claim()
    status, _, _ = _consume(http, method=method)
    assert status == 405


def test_an_http_replay_is_idempotent():
    http = _http_with_claim()
    first = _consume(http)
    second = _consume(http)
    assert first[0] == second[0] == 200
    assert second[1]["created"] is False
    assert second[1]["child_id"] == first[1]["child_id"]
    # Scoped to THIS session, not an absolute count: `build_http` seeds fixture
    # children through `build_secure_topology`, so the collection is not empty
    # to begin with.
    links = http.repos.source_links.list_for_external_id(SESSION)
    assert len(links) == 1
    assert links[0].child_id == first[1]["child_id"]


def test_the_consume_route_logs_no_token():
    http = _http_with_claim()
    _consume(http)
    rendered = json.dumps(http.logs, default=str)
    assert TOKEN not in rendered
    assert claim_digest(TOKEN) not in rendered
    # The route TEMPLATE is logged, never a populated path or a body.
    assert CONSUME_PATH in rendered


def test_the_consume_route_emits_no_identifier_in_its_log_route():
    http = _http_with_claim()
    _consume(http)
    # `log_sink` holds RENDERED lines (strings), not dicts.
    for line in http.logs:
        assert "chld_" not in str(line)


def test_an_expired_claim_over_http_is_refused():
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    http = build_http()
    # Issued well in the past with a short ttl, so it is expired against the
    # real clock the application uses.
    _register(http.repos, now=_dt.now(_tz.utc) - _td(seconds=120), ttl=30)
    status, payload, _ = _consume(http)
    assert status == 403
    assert payload == {"error": "not permitted"}


# ---------------------------------------------------------------------------
# 8. the registration race-convergence branch
# ---------------------------------------------------------------------------
#
# Added because the mutation sweep found the `DuplicateRecord` branch in
# `register` unexercised: the pre-read catches a replay first, so the branch is
# only reachable when a competitor lands BETWEEN the read and the create. A
# repository seam reproduces exactly that interleaving.

class _RacingClaimRepo:
    """A claim repository where a competitor wins between read and create."""

    def __init__(self, inner, *, winner_session):
        self._inner = inner
        self._winner_session = winner_session
        self._armed = True

    def find(self, digest):
        # The first read sees nothing — the competitor has not landed yet.
        if self._armed:
            return None
        return self._inner.find(digest)

    def create(self, claim):
        # The competitor lands HERE, then our create collides.
        if self._armed:
            self._armed = False
            from dataclasses import replace as _replace
            self._inner.create(_replace(claim,
                                        source_session_id=self._winner_session))
            raise DuplicateRecord("the competitor won")
        return self._inner.create(claim)


class _RacingRepos:
    def __init__(self, repos, *, winner_session):
        self._repos = repos
        self.parent_session_claims = _RacingClaimRepo(
            repos.parent_session_claims, winner_session=winner_session)

    def __getattr__(self, name):
        return getattr(self._repos, name)


def test_a_registration_race_converges_on_the_winner():
    """The loser returns the WINNER's claim rather than writing a second."""
    repos = _repos()
    racing = _RacingRepos(repos, winner_session=SESSION)
    svc = ParentSessionClaimRegistrationService(repos=racing, now=lambda: NOW)

    result = svc.register({"claim_digest": claim_digest(TOKEN),
                           "source_session_id": SESSION})

    assert result.created is False, "the loser believed it created the claim"
    assert result.claim.source_session_id == SESSION
    assert len(repos.store.list_all("pilot_parent_session_claims")) == 1


def test_a_registration_race_lost_to_another_session_still_conflicts():
    """Convergence must NOT swallow a winner naming a different session."""
    repos = _repos()
    racing = _RacingRepos(repos, winner_session=OTHER_SESSION)
    svc = ParentSessionClaimRegistrationService(repos=racing, now=lambda: NOW)

    with pytest.raises(ClaimRegistrationConflict):
        svc.register({"claim_digest": claim_digest(TOKEN),
                      "source_session_id": SESSION})
