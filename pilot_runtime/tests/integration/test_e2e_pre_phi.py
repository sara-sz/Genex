"""PRE-PHI end-to-end: real HTTP, real Firestore, real everything but one call.

## Exactly what is real and what is a double

REAL, exercised as production would use it:

    the WSGI transport, over a loopback socket via urllib
    `IdentityPlatformVerifier`        — the production verifier class
    `FirebaseTokenDecoder`            — the production adapter class, including
                                        its full SDK-exception translation
    `resolve_principal`               — auth subject to application identity
    `authorize_child_access`          — relationship authorization
    `FirestoreDocumentStore`          — the production Firestore adapter
    the Firestore emulator            — a real Firestore server
    repositories, codecs, audit recorder, revision chain
    `build_runtime`                   — the production composition root

DOUBLED, one function:

    `firebase_admin.auth.verify_id_token` — the network call to Google.

That single call is replaced by `ScriptedFirebaseAuth`, which returns claims or
raises the SDK's own exception types. Everything downstream of it — including
the translation of those exceptions, which is where the security behaviour
lives — is the real code path.

Reaching the genuine call would need an Identity Platform project and
credentials, which this phase must not use. So this is stated plainly rather
than described as full end-to-end coverage: **token signature verification
itself is not exercised.** It remains a PRE-PHI blocker.

No real PHI. Fictional identities only, and `ChildContextRecord` holds an
opaque reference rather than content, so there is no clinical text in the
system at all.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest

from pilot_backend.audit.events import AuditAction, AuditResult
from pilot_backend.auth.verifiers import IdentityPlatformVerifier
from pilot_backend.domain.enums import ConnectionStatus
from pilot_backend.domain.roles import ActorRole
from pilot_backend.persistence import encode
from pilot_backend.revision.records import RecordState
from pilot_runtime.auth.firebase_decoder import FirebaseTokenDecoder
from pilot_runtime.composition import build_runtime
from pilot_runtime.workflows.child_context import ChildContextService

from ..test_sentinels import (
    ALL_SENTINELS,
    SENTINEL_CONCERN,
    SENTINEL_EMAIL,
    SENTINEL_NOTE,
    SENTINEL_SECRET,
    SENTINEL_TOKEN,
)

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


# ===========================================================================
# the one doubled call
# ===========================================================================

class ScriptedFirebaseAuth:
    """Stands in for `firebase_admin.auth`, with its real exception types."""

    class InvalidIdTokenError(Exception):
        pass

    class ExpiredIdTokenError(InvalidIdTokenError):
        pass

    class RevokedIdTokenError(InvalidIdTokenError):
        pass

    class UserDisabledError(InvalidIdTokenError):
        pass

    class CertificateFetchError(Exception):
        pass

    def __init__(self) -> None:
        self.tokens = {}
        self.revoked = set()
        self.disabled = set()

    def issue(self, token: str, subject: str) -> str:
        self.tokens[token] = subject
        return token

    def verify_id_token(self, token, app=None, check_revoked=False):
        if token in self.revoked:
            raise self.RevokedIdTokenError(f"revoked: {SENTINEL_NOTE}")
        if token in self.disabled:
            raise self.UserDisabledError("user disabled")
        subject = self.tokens.get(token)
        if subject is None:
            raise self.InvalidIdTokenError(f"bad token {SENTINEL_TOKEN}")
        return {"uid": subject, "iss": "https://securetoken.google.com/demo-genex-pilot",
                "aud": "demo-genex-pilot", "email": SENTINEL_EMAIL,
                "email_verified": True}


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        pass


class LiveServer:
    def __init__(self, app) -> None:
        self._server = make_server("127.0.0.1", 0, app, handler_class=_QuietHandler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def get(self, path, *, bearer=None, headers=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method="GET")
        if bearer is not None:
            request.add_header("Authorization", bearer)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())


# ===========================================================================
# the stack
# ===========================================================================

class Stack:
    def __init__(self, runtime, firebase, topology, tokens, logs) -> None:
        self.runtime = runtime
        self.firebase = firebase
        self.topology = topology
        self.tokens = tokens
        self.logs = logs
        self.service: ChildContextService = runtime.child_context
        self.repos = runtime.repos


@pytest.fixture()
def stack(firestore_client, unique_suffix):
    """A full runtime over the emulator, assembled by the composition root."""
    from pilot_backend.fixtures.secure_topology import build_secure_topology
    from pilot_backend.persistence import FirestoreRepositories
    from pilot_runtime.persistence.firestore_store import FirestoreDocumentStore

    firebase = ScriptedFirebaseAuth()
    decoder = FirebaseTokenDecoder(app=object(), verify_module=firebase)

    logs = []
    runtime = build_runtime(
        {"PILOT_ENVIRONMENT": "test", "PILOT_ALLOWED_ORIGINS": "http://localhost:5173"},
        process_env={}, firestore_client=firestore_client, decoder=decoder,
        log_sink=logs)

    topology = build_secure_topology(runtime.repos, now=T0, subject_suffix=unique_suffix)

    tokens = {
        "caregiver-alpha": firebase.issue(
            "tok-caregiver-alpha" + unique_suffix,
            "fictional-subject-caregiver-alpha" + unique_suffix),
        "caregiver-beta": firebase.issue(
            "tok-caregiver-beta" + unique_suffix,
            "fictional-subject-caregiver-beta" + unique_suffix),
        "caregiver-gamma": firebase.issue(
            "tok-caregiver-gamma" + unique_suffix,
            "fictional-subject-caregiver-gamma" + unique_suffix),
        "provider-alpha": firebase.issue(
            "tok-provider-alpha" + unique_suffix,
            "fictional-subject-provider-alpha" + unique_suffix),
        "provider-gamma": firebase.issue(
            "tok-provider-gamma" + unique_suffix,
            "fictional-subject-provider-gamma" + unique_suffix),
        "unprovisioned": firebase.issue(
            "tok-unprovisioned" + unique_suffix, "fictional-subject-nobody"),
    }
    return Stack(runtime, firebase, topology, tokens, logs)


def bearer(stack: Stack, who: str) -> str:
    return "Bearer " + stack.tokens[who]


def child_path(child_id: str) -> str:
    return f"/pilot/children/{child_id}/access-check"


# ===========================================================================
# the composition root really did wire the real adapter
# ===========================================================================

def test_the_runtime_is_built_on_the_real_firestore_adapter(stack):
    from pilot_runtime.persistence.firestore_store import FirestoreDocumentStore

    assert isinstance(stack.runtime.store, FirestoreDocumentStore)
    assert isinstance(stack.runtime.verifier, IdentityPlatformVerifier)
    assert isinstance(stack.runtime.verifier._decoder, FirebaseTokenDecoder)


# ===========================================================================
# FICTIONAL CAREGIVER WORKFLOW
# ===========================================================================

def test_caregiver_end_to_end_over_http_and_firestore(stack):
    child = stack.topology.child_alpha.child_id
    with LiveServer(stack.runtime.application) as server:
        status, body = server.get(child_path(child), bearer=bearer(stack, "caregiver-alpha"))

    assert status == 200
    assert body["authorized"] is True
    assert body["child_id"] == child
    assert body["actor_role"] == "caregiver"

    # ...and the audit event landed in real Firestore.
    events = stack.repos.audit_events.list_for_child(child)
    assert [e.action for e in events] == [AuditAction.CHILD_ACCESS_GRANTED]
    assert events[0].actor_application_id == stack.topology.caregiver_alpha.caregiver_id
    assert events[0].actor_role is ActorRole.CAREGIVER


def test_caregiver_creates_reads_and_persists_a_child_context(stack):
    child = stack.topology.child_alpha.child_id
    created = stack.service.create_draft(
        bearer(stack, "caregiver-alpha"), child,
        content_ref="ctx-blob-fictional-1", request_id="req-fictional-001")
    assert created.ok and created.version == 1 and created.state == "draft"

    # Read back through a second repository instance over the same store.
    stored = stack.repos.child_contexts.get_by_id(created.record_id)
    assert stored.child_id == child
    assert stored.current_revision_id == created.revision_id
    assert stored.last_actor_role is ActorRole.CAREGIVER

    read = stack.service.read(bearer(stack, "caregiver-alpha"), child)
    assert read.ok and read.record_id == created.record_id


def test_caregiver_cannot_reach_another_family(stack):
    with LiveServer(stack.runtime.application) as server:
        status, body = server.get(child_path(stack.topology.child_beta.child_id),
                                  bearer=bearer(stack, "caregiver-alpha"))
    assert status == 403
    assert body == {"error": "not permitted"}


# ===========================================================================
# FICTIONAL PROVIDER WORKFLOW
# ===========================================================================

def test_provider_end_to_end_over_http_and_firestore(stack):
    child = stack.topology.child_alpha.child_id
    with LiveServer(stack.runtime.application) as server:
        status, body = server.get(child_path(child), bearer=bearer(stack, "provider-alpha"))

    assert status == 200
    assert body["actor_role"] == "provider"

    events = stack.repos.audit_events.list_for_child(child)
    assert events[0].actor_application_id == stack.topology.provider_alpha.provider_id
    assert events[0].actor_role is ActorRole.PROVIDER


def test_provider_alpha_cannot_access_unrelated_child_beta(stack):
    """Required explicitly by the contract."""
    beta = stack.topology.child_beta.child_id
    with LiveServer(stack.runtime.application) as server:
        status, body = server.get(child_path(beta), bearer=bearer(stack, "provider-alpha"))
    assert status == 403
    assert body == {"error": "not permitted"}

    outcome = stack.service.read(bearer(stack, "provider-alpha"), beta)
    assert not outcome.ok and outcome.status_code == 403

    failures = [e for e in stack.repos.audit_events.list_for_child(beta)
                if e.result is AuditResult.FAILURE]
    assert failures, "an authorization failure must be audited"
    assert all(e.action is AuditAction.AUTHORIZATION_FAILURE for e in failures)


def test_provider_writes_a_child_context_through_the_same_workflow(stack):
    child = stack.topology.child_alpha.child_id
    created = stack.service.create_draft(bearer(stack, "provider-alpha"), child,
                                         content_ref="ctx-blob-fictional-p1")
    assert created.ok
    assert stack.repos.child_contexts.get_by_id(
        created.record_id).last_actor_role is ActorRole.PROVIDER


# ===========================================================================
# REVISION / HISTORY through real Firestore
# ===========================================================================

def test_finalize_then_amend_retains_history_in_firestore(stack):
    child = stack.topology.child_alpha.child_id
    token = bearer(stack, "caregiver-alpha")

    created = stack.service.create_draft(token, child, content_ref="ctx-blob-v1")
    sealed = stack.service.finalize_current(token, child)
    assert sealed.ok and sealed.state == RecordState.FINALIZED.value

    amended = stack.service.amend_current(
        token, child, reason="corrected the fictional routine",
        content_ref="ctx-blob-v2")
    assert amended.ok and amended.version == 2

    chain = stack.repos.revisions.list_chain(created.record_id)
    assert [r.version for r in chain] == [1, 2]

    first, second = chain
    assert first.state is RecordState.FINALIZED
    assert first.finalized_at is not None
    assert first.content_ref == "ctx-blob-v1", "the prior version is retained intact"
    assert second.supersedes_revision_id == first.revision_id
    assert second.amendment_reason == "corrected the fictional routine"
    assert second.actor_application_id == stack.topology.caregiver_alpha.caregiver_id
    assert second.actor_role is ActorRole.CAREGIVER
    assert second.created_at.tzinfo is not None

    # The record index points at the newest version.
    assert stack.repos.child_contexts.get_by_id(
        created.record_id).current_version == 2


def test_a_finalized_record_cannot_be_silently_overwritten(stack):
    child = stack.topology.child_alpha.child_id
    token = bearer(stack, "caregiver-alpha")
    stack.service.create_draft(token, child, content_ref="ctx-blob-v1")
    stack.service.finalize_current(token, child)

    again = stack.service.finalize_current(token, child)
    assert not again.ok and again.status_code == 409

    no_reason = stack.service.amend_current(token, child, reason="  ")
    assert not no_reason.ok and no_reason.status_code == 409


def test_history_is_authorized_like_any_other_read(stack):
    child = stack.topology.child_alpha.child_id
    token = bearer(stack, "caregiver-alpha")
    stack.service.create_draft(token, child, content_ref="ctx-blob-v1")
    stack.service.finalize_current(token, child)

    assert len(stack.service.history(token, child)) == 1
    assert stack.service.history(bearer(stack, "caregiver-beta"), child) == []
    assert stack.service.history(None, child) == []


def test_workflow_mutations_are_audited(stack):
    child = stack.topology.child_alpha.child_id
    token = bearer(stack, "caregiver-alpha")
    stack.service.create_draft(token, child, content_ref="ctx-blob-v1",
                               request_id="req-fictional-100")
    stack.service.finalize_current(token, child, request_id="req-fictional-101")
    stack.service.amend_current(token, child, reason="fictional correction",
                                content_ref="ctx-blob-v2",
                                request_id="req-fictional-102")

    actions = [e.action for e in stack.repos.audit_events.list_for_child(child)]
    assert AuditAction.CHILD_CREATED in actions
    assert AuditAction.RECORD_FINALIZED in actions
    assert AuditAction.RECORD_AMENDED in actions

    for event in stack.repos.audit_events.list_for_child(child):
        assert event.resource_type == "child_context"
        assert event.occurred_at.tzinfo is not None
        assert event.actor_application_id
        assert event.actor_role is ActorRole.CAREGIVER
        assert event.request_id.startswith("req-fictional-")


# ===========================================================================
# SECURITY REGRESSION over the integrated stack
# ===========================================================================

@pytest.mark.parametrize("token", [None, "", "Bearer nonsense", "Basic abc",
                                   "Bearer " + SENTINEL_TOKEN])
def test_missing_or_invalid_auth_is_401(stack, token):
    with LiveServer(stack.runtime.application) as server:
        status, body = server.get(child_path(stack.topology.child_alpha.child_id),
                                  bearer=token)
    assert status == 401
    assert body == {"error": "authentication required"}


def test_revoked_token_is_401_through_the_real_adapter(stack):
    """Revocation travels: scripted SDK -> real decoder -> verifier -> HTTP."""
    stack.firebase.revoked.add(stack.tokens["caregiver-alpha"])
    with LiveServer(stack.runtime.application) as server:
        status, body = server.get(child_path(stack.topology.child_alpha.child_id),
                                  bearer=bearer(stack, "caregiver-alpha"))
    assert status == 401
    assert body == {"error": "authentication required"}


def test_disabled_account_is_401(stack):
    stack.firebase.disabled.add(stack.tokens["caregiver-alpha"])
    with LiveServer(stack.runtime.application) as server:
        status, _ = server.get(child_path(stack.topology.child_alpha.child_id),
                               bearer=bearer(stack, "caregiver-alpha"))
    assert status == 401


def test_authenticated_but_unprovisioned_is_403(stack):
    with LiveServer(stack.runtime.application) as server:
        status, _ = server.get(child_path(stack.topology.child_alpha.child_id),
                               bearer=bearer(stack, "unprovisioned"))
    assert status == 403


def test_ended_and_pending_relationships_are_403(stack):
    child = stack.topology.child_alpha.child_id
    with LiveServer(stack.runtime.application) as server:
        assert server.get(child_path(child),
                          bearer=bearer(stack, "caregiver-gamma"))[0] == 403
        assert server.get(child_path(child),
                          bearer=bearer(stack, "provider-gamma"))[0] == 403


def test_revoking_a_relationship_denies_the_next_request(stack):
    child = stack.topology.child_alpha.child_id
    with LiveServer(stack.runtime.application) as server:
        assert server.get(child_path(child),
                          bearer=bearer(stack, "provider-alpha"))[0] == 200
        stack.repos.provider_child.end_connection(
            stack.topology.link_alpha_provider.connection_id,
            status=ConnectionStatus.REVOKED)
        assert server.get(child_path(child),
                          bearer=bearer(stack, "provider-alpha"))[0] == 403


FORGED = {
    "X-Uid": "fictional-subject-provider-alpha",
    "X-Role": "provider",
    "X-Actor-Role": "provider",
    "X-Caregiver-Id": "cgvr_forged",
    "X-Provider-Id": "prov_forged",
    "X-Beta-Access-Code": "genex",
    "X-Admin": "true",
}


def test_forged_headers_have_no_effect_on_the_integrated_stack(stack):
    with LiveServer(stack.runtime.application) as server:
        status, _ = server.get(child_path(stack.topology.child_beta.child_id),
                               bearer=bearer(stack, "caregiver-alpha"), headers=FORGED)
        assert status == 403

        status, body = server.get(child_path(stack.topology.child_alpha.child_id),
                                  bearer=bearer(stack, "caregiver-alpha"),
                                  headers=FORGED)
    assert status == 200
    assert body["actor_role"] == "caregiver", "forged role must not take effect"


def test_beta_code_alone_grants_nothing(stack):
    with LiveServer(stack.runtime.application) as server:
        assert server.get(child_path(stack.topology.child_alpha.child_id),
                          bearer="Bearer genex")[0] == 401
        assert server.get(child_path(stack.topology.child_alpha.child_id),
                          bearer=None,
                          headers={"X-Beta-Access-Code": "genex"})[0] == 401


def test_modified_child_id_is_denied(stack):
    real = stack.topology.child_alpha.child_id
    with LiveServer(stack.runtime.application) as server:
        for forged in (real[:-1] + ("a" if real[-1] != "a" else "b"),
                       real.upper(), real + "x", "chld_" + "0" * 32):
            assert server.get(child_path(forged),
                              bearer=bearer(stack, "caregiver-alpha"))[0] == 403, forged


def test_unrelated_and_nonexistent_children_are_indistinguishable(stack):
    with LiveServer(stack.runtime.application) as server:
        existing = server.get(child_path(stack.topology.child_beta.child_id),
                              bearer=bearer(stack, "caregiver-alpha"))
        missing = server.get(child_path("chld_" + "0" * 32),
                             bearer=bearer(stack, "caregiver-alpha"))
    assert existing == missing == (403, {"error": "not permitted"})


def test_authorization_precedes_any_child_read_in_the_real_stack(stack):
    """The counting proof, now against Firestore rather than a fake."""
    reads = []
    inner = stack.repos.children.get_by_id

    def counting(child_id):
        reads.append(child_id)
        return inner(child_id)

    stack.repos.children.get_by_id = counting
    with LiveServer(stack.runtime.application) as server:
        server.get(child_path(stack.topology.child_beta.child_id),
                   bearer=bearer(stack, "caregiver-alpha"))
        server.get(child_path(stack.topology.child_alpha.child_id),
                   bearer=bearer(stack, "caregiver-gamma"))
        server.get(child_path(stack.topology.child_alpha.child_id), bearer=None)
    assert reads == [], "a refused request must not read the child record"

    with LiveServer(stack.runtime.application) as server:
        server.get(child_path(stack.topology.child_alpha.child_id),
                   bearer=bearer(stack, "caregiver-alpha"))
    assert reads == [stack.topology.child_alpha.child_id]


# ===========================================================================
# LEAKAGE
# ===========================================================================

def test_no_sentinel_reaches_responses_logs_or_audit_documents(stack):
    child = stack.topology.child_alpha.child_id
    bodies = []
    with LiveServer(stack.runtime.application) as server:
        bodies.append(server.get(child_path(child), bearer="Bearer " + SENTINEL_TOKEN))
        bodies.append(server.get(child_path(child), bearer=bearer(stack, "caregiver-alpha"),
                                 headers={"X-Note": SENTINEL_NOTE,
                                          "X-Secret": SENTINEL_SECRET}))
        bodies.append(server.get(
            child_path(child) + "?concern=" + SENTINEL_CONCERN,
            bearer=bearer(stack, "caregiver-alpha")))

    stack.service.create_draft(bearer(stack, "caregiver-alpha"), child,
                               content_ref="ctx-blob-fictional")

    documents = [encode(e) for e in stack.repos.audit_events.list_all()]
    assert documents, "no audit events written; the assertion would be vacuous"

    blob = json.dumps(bodies) + "\n".join(stack.logs) + json.dumps(documents)
    for sentinel in ALL_SENTINELS:
        assert sentinel not in blob, sentinel


def test_the_email_from_the_verified_token_is_never_persisted(stack):
    """The scripted token carries a sentinel email; nothing may store it."""
    child = stack.topology.child_alpha.child_id
    stack.service.create_draft(bearer(stack, "caregiver-alpha"), child,
                               content_ref="ctx-blob-fictional")
    documents = [encode(e) for e in stack.repos.audit_events.list_all()]
    documents += [encode(r) for r in stack.repos.revisions.list_chain(
        stack.repos.child_contexts.list_for_child(child)[0].record_id)]
    assert SENTINEL_EMAIL not in json.dumps(documents)


def test_firestore_sdk_exception_text_does_not_reach_the_response(stack):
    """A store failure yields a bare 500, not an SDK message."""
    from pilot_backend.persistence.document_store import DocumentStoreError

    def exploding(*args, **kwargs):
        raise DocumentStoreError("read failed in pilot_children")

    stack.repos.children.get_by_id = exploding
    with LiveServer(stack.runtime.application) as server:
        status, body = server.get(child_path(stack.topology.child_alpha.child_id),
                                  bearer=bearer(stack, "caregiver-alpha"))
    assert status == 500
    assert body == {"error": "internal error"}
    assert "pilot_children" not in json.dumps(body)
