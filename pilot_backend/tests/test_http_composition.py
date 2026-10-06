"""BACKEND 0.2 — HTTP composition proof.

Proves the protected-request chain over a REAL HTTP boundary: a
`wsgiref.simple_server` bound to a loopback port, driven with `urllib`. Real
request lines, real headers, real status codes, real sockets.

## Why not an in-process test client

An in-process client calls the application object directly, which is fine for
exercising handlers but silently skips the parts of a transport that can leak:
header parsing, status-line construction, response headers, and what the
server does with an exception that escapes. Those are the layers where a token
ends up in a log or a traceback reaches a client, so they are tested through a
socket.

Fast in-process WSGI invocation is used as well, for the cases where the
assertion is about the chain rather than the wire.

## Sentinels

The same fictional markers as the security suite. Every sentinel is fed
through the boundary — as a token, a header, a query string and a body — and
then every log line and audit document is searched for it.
"""

from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Tuple
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest

from pilot_backend.audit.events import AuditAction, AuditResult
from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth import DevAuthVerifier, IdentityPlatformVerifier, VerifiedToken
from pilot_backend.authz.decisions import Denial
from pilot_backend.config import PilotSettings
from pilot_backend.domain.enums import ConnectionStatus
from pilot_backend.fixtures.secure_topology import (
    CAREGIVER_ALPHA_SUBJECT,
    CAREGIVER_GAMMA_SUBJECT,
    PROVIDER_ALPHA_SUBJECT,
    PROVIDER_GAMMA_SUBJECT,
    UNPROVISIONED_SUBJECT,
    build_secure_topology,
)
from pilot_backend.persistence import FakeDocumentStore, FirestoreRepositories, encode
from pilot_backend.transport import PROTECTED_CHILD_ROUTE, build_application
from pilot_backend.transport.wsgi_app import route_templates

from .test_secure_foundation import (
    ALL_SENTINELS,
    SENTINEL_CONCERN,
    SENTINEL_EMAIL,
    SENTINEL_NOTE,
    SENTINEL_SECRET,
    SENTINEL_TOKEN,
    dev_settings,
    standard_tokens,
)


# ===========================================================================
# harness
# ===========================================================================

class CountingChildRepository:
    """Wraps the child repository and counts reads.

    The instrument for the ordering requirement: if authorization runs before
    repository access, an unauthorized request leaves this counter at zero.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.get_calls = 0
        self.requested_ids = []

    def get_by_id(self, child_id: str):
        self.get_calls += 1
        self.requested_ids.append(child_id)
        return self._inner.get_by_id(child_id)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class SpyRepositories:
    """Repository set whose child repository counts reads."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.children = CountingChildRepository(inner.children)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def build_stack(*, dev_auth: bool = True, decoder=None, with_audit: bool = True):
    """Assemble settings, repositories, verifier, recorder and application."""
    settings = dev_settings() if dev_auth else PilotSettings.from_env(
        {"PILOT_ENVIRONMENT": "dev"})
    base = FirestoreRepositories(FakeDocumentStore())
    topology = build_secure_topology(base)
    repos = SpyRepositories(base)

    if decoder is not None:
        verifier = IdentityPlatformVerifier("dev", decoder)
    else:
        verifier = DevAuthVerifier("dev")
        for token, subject in standard_tokens().items():
            verifier.add(token, VerifiedToken(subject=subject, email=SENTINEL_EMAIL))

    recorder = AuditRecorder(base.audit_events, environment="dev") if with_audit else None
    logs = []
    app = build_application(settings=settings, repos=repos, verifier=verifier,
                            recorder=recorder, log_sink=logs)
    return app, repos, base, topology, logs


def call_wsgi(app, path: str, *, method: str = "GET",
              bearer: Optional[str] = None,
              headers: Optional[dict] = None,
              query: str = "",
              body: bytes = b"") -> Tuple[int, dict, dict]:
    """Invoke the WSGI app in-process. Returns (status, body, headers)."""
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SERVER_NAME": "testserver",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.input": io.BytesIO(body),
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
    payload = json.loads(b"".join(chunks).decode("utf-8"))
    return captured["status"], payload, captured["headers"]


class _QuietHandler(WSGIRequestHandler):
    """wsgiref logs every request to stderr; tests do not need the noise."""

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass


class LiveServer:
    """A real HTTP server on a loopback port, for the duration of a test."""

    def __init__(self, app) -> None:
        self._server = make_server("127.0.0.1", 0, app, handler_class=_QuietHandler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "LiveServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path: str, *, bearer: Optional[str] = None,
            headers: Optional[dict] = None) -> Tuple[int, dict, dict]:
        request = urllib.request.Request(self.url(path), method="GET")
        if bearer is not None:
            request.add_header("Authorization", bearer)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return (response.status,
                        json.loads(response.read().decode("utf-8")),
                        dict(response.headers))
        except urllib.error.HTTPError as exc:
            return (exc.code,
                    json.loads(exc.read().decode("utf-8")),
                    dict(exc.headers))


def child_path(child_id: str) -> str:
    return f"/pilot/children/{child_id}/access-check"


# ===========================================================================
# the chain works end to end, over a real socket
# ===========================================================================

def test_authorized_caregiver_succeeds_over_real_http():
    app, _, _, topo, _ = build_stack()
    with LiveServer(app) as server:
        status, body, headers = server.get(
            child_path(topo.child_alpha.child_id), bearer="Bearer token-caregiver-alpha")
    assert status == 200
    assert body["authorized"] is True
    assert body["child_id"] == topo.child_alpha.child_id
    assert body["actor_role"] == "caregiver"
    assert headers.get("X-Content-Type-Options") == "nosniff"


def test_authorized_provider_succeeds_over_real_http():
    app, _, _, topo, _ = build_stack()
    with LiveServer(app) as server:
        status, body, _ = server.get(
            child_path(topo.child_alpha.child_id), bearer="Bearer token-provider-alpha")
    assert status == 200 and body["actor_role"] == "provider"


def test_health_is_public_over_real_http():
    app, _, _, _, _ = build_stack()
    with LiveServer(app) as server:
        status, body, _ = server.get("/health")
    assert status == 200
    assert set(body) == {"status", "environment"}


def test_health_needs_no_credential_and_leaks_no_infrastructure():
    app, _, _, _, _ = build_stack()
    status, body, _ = call_wsgi(app, "/health")
    assert status == 200
    blob = json.dumps(body)
    for marker in ("genex", "firestore", "project", "pilot-dev", "localhost"):
        assert marker not in blob.lower() or marker == "genex"


# ===========================================================================
# 1-3. authentication failures -> 401
# ===========================================================================

@pytest.mark.parametrize("bearer", [None, "", "   ", "Bearer", "Bearer ",
                                    "Basic abc", "Bearer a b c",
                                    "Bearer " + SENTINEL_TOKEN])
def test_missing_or_invalid_token_is_401_over_http(bearer):
    app, _, _, topo, _ = build_stack()
    with LiveServer(app) as server:
        status, body, _ = server.get(child_path(topo.child_alpha.child_id),
                                     bearer=bearer)
    assert status == 401
    assert body == {"error": "authentication required"}


def test_revoked_token_is_401_over_http():
    def decoder(token, *, check_revoked):
        raise RuntimeError("The Firebase ID token has been revoked.")

    app, _, _, topo, _ = build_stack(dev_auth=False, decoder=decoder)
    with LiveServer(app) as server:
        status, body, _ = server.get(child_path(topo.child_alpha.child_id),
                                     bearer="Bearer " + SENTINEL_TOKEN)
    assert status == 401
    assert body == {"error": "authentication required"}


def test_revocation_semantics_reach_the_transport():
    """A token the provider reports revoked is refused, not merely unverified."""
    calls = {}

    def decoder(token, *, check_revoked):
        calls["check_revoked"] = check_revoked
        return {"uid": CAREGIVER_ALPHA_SUBJECT}

    app, _, _, topo, _ = build_stack(dev_auth=False, decoder=decoder)
    status, _, _ = call_wsgi(app, child_path(topo.child_alpha.child_id),
                             bearer="Bearer good")
    assert status == 200
    assert "check_revoked" in calls, "verification must consult revocation semantics"


# ===========================================================================
# 4-7, 10. authorization failures -> 403
# ===========================================================================

def test_authenticated_but_unprovisioned_is_403():
    app, _, _, topo, _ = build_stack()
    status, body, _ = call_wsgi(app, child_path(topo.child_alpha.child_id),
                                bearer="Bearer token-unprovisioned")
    assert status == 403 and body == {"error": "not permitted"}


def test_unrelated_child_is_403():
    app, _, _, topo, _ = build_stack()
    status, body, _ = call_wsgi(app, child_path(topo.child_beta.child_id),
                                bearer="Bearer token-caregiver-alpha")
    assert status == 403 and body == {"error": "not permitted"}


def test_ended_caregiver_relationship_is_403():
    app, _, _, topo, _ = build_stack()
    status, _, _ = call_wsgi(app, child_path(topo.child_alpha.child_id),
                             bearer="Bearer token-caregiver-gamma")
    assert status == 403


def test_pending_provider_relationship_is_403():
    app, _, _, topo, _ = build_stack()
    status, _, _ = call_wsgi(app, child_path(topo.child_alpha.child_id),
                             bearer="Bearer token-provider-gamma")
    assert status == 403


def test_revoking_mid_session_denies_the_next_request():
    app, _, base, topo, _ = build_stack()
    path = child_path(topo.child_alpha.child_id)
    assert call_wsgi(app, path, bearer="Bearer token-provider-alpha")[0] == 200

    base.provider_child.end_connection(topo.link_alpha_provider.connection_id,
                                       status=ConnectionStatus.REVOKED)

    assert call_wsgi(app, path, bearer="Bearer token-provider-alpha")[0] == 403


def test_403_is_identical_for_existing_and_nonexistent_children():
    """§10: no useful existence leakage across the HTTP boundary."""
    app, _, _, topo, _ = build_stack()
    existing_other = call_wsgi(app, child_path(topo.child_beta.child_id),
                               bearer="Bearer token-caregiver-alpha")
    nonexistent = call_wsgi(app, child_path("chld_" + "0" * 32),
                            bearer="Bearer token-caregiver-alpha")

    assert existing_other[0] == nonexistent[0] == 403
    assert existing_other[1] == nonexistent[1]
    # Response headers must not differ either (beyond the request id).
    strip = lambda h: {k: v for k, v in h.items() if k != "X-Request-Id"}
    assert strip(existing_other[2]) == strip(nonexistent[2])


# ===========================================================================
# 16. authorization precedes repository access
# ===========================================================================

@pytest.mark.parametrize("bearer,label", [
    (None, "no token"),
    ("Bearer " + SENTINEL_TOKEN, "invalid token"),
    ("Bearer token-unprovisioned", "no application record"),
    ("Bearer token-caregiver-gamma", "ended relationship"),
    ("Bearer token-provider-gamma", "pending relationship"),
])
def test_no_repository_read_happens_on_any_refused_request(bearer, label):
    """The child record is never read for a request that is refused.

    This is the existence-oracle guarantee stated positively: an unauthorized
    caller does not cause a lookup of the id they asked about, so there is
    nothing for a response, a log, an audit record or a timing difference to
    disclose.
    """
    app, repos, _, topo, _ = build_stack()
    status, _, _ = call_wsgi(app, child_path(topo.child_alpha.child_id), bearer=bearer)
    assert status in (401, 403), label
    assert repos.children.get_calls == 0, f"child record was read on a refused request ({label})"


def test_unrelated_child_request_never_reads_that_child():
    app, repos, _, topo, _ = build_stack()
    call_wsgi(app, child_path(topo.child_beta.child_id),
              bearer="Bearer token-caregiver-alpha")
    assert repos.children.get_calls == 0
    assert topo.child_beta.child_id not in repos.children.requested_ids


def test_the_authorized_path_reads_the_repository_exactly_once():
    """The counter is meaningful only if it moves when access is granted.

    Asserting the exact COUNT, not merely "greater than zero". That is what
    caught the handler re-reading a child record authorization had already
    fetched — a duplicate billed read per authorized request in Firestore, and
    a second window in which the record could change between check and use.
    """
    app, repos, _, topo, _ = build_stack()
    status, _, _ = call_wsgi(app, child_path(topo.child_alpha.child_id),
                             bearer="Bearer token-caregiver-alpha")
    assert status == 200
    assert repos.children.get_calls == 1
    assert repos.children.requested_ids == [topo.child_alpha.child_id]


# ===========================================================================
# 11-15. forged input has no effect
# ===========================================================================

FORGED_HEADERS = {
    "X-Uid": PROVIDER_ALPHA_SUBJECT,
    "X-User-Id": PROVIDER_ALPHA_SUBJECT,
    "X-Role": "provider",
    "X-Actor-Role": "provider",
    "X-Caregiver-Id": "cgvr_forged",
    "X-Provider-Id": "prov_forged",
    "X-Beta-Access-Code": "genex",
    "X-Admin": "true",
}


def test_forged_headers_do_not_grant_access_to_another_family():
    """§11-13: forged uid/role/caregiver_id/provider_id in headers are inert."""
    app, _, _, topo, _ = build_stack()
    with LiveServer(app) as server:
        status, body, _ = server.get(child_path(topo.child_beta.child_id),
                                     bearer="Bearer token-caregiver-alpha",
                                     headers=FORGED_HEADERS)
    assert status == 403 and body == {"error": "not permitted"}


def test_forged_query_string_does_not_grant_access():
    app, _, _, topo, _ = build_stack()
    query = urllib.parse.urlencode({
        "uid": PROVIDER_ALPHA_SUBJECT, "role": "provider",
        "caregiver_id": topo.caregiver_beta.caregiver_id,
        "provider_id": topo.provider_beta.provider_id,
        "beta_access_code": "genex",
    })
    status, _, _ = call_wsgi(app, child_path(topo.child_beta.child_id),
                             bearer="Bearer token-caregiver-alpha", query=query)
    assert status == 403


def test_forged_body_does_not_grant_access():
    app, _, _, topo, _ = build_stack()
    body = json.dumps({"uid": PROVIDER_ALPHA_SUBJECT, "role": "provider",
                       "provider_id": topo.provider_beta.provider_id}).encode()
    status, _, _ = call_wsgi(app, child_path(topo.child_beta.child_id),
                             method="GET", bearer="Bearer token-caregiver-alpha",
                             body=body)
    assert status == 403


def test_forged_identity_does_not_change_the_resolved_role():
    """Claiming provider over a caregiver's token yields caregiver, not provider."""
    app, _, _, topo, _ = build_stack()
    status, body, _ = call_wsgi(app, child_path(topo.child_alpha.child_id),
                                bearer="Bearer token-caregiver-alpha",
                                headers=FORGED_HEADERS)
    assert status == 200
    assert body["actor_role"] == "caregiver"


@pytest.mark.parametrize("code", ["genex", "genex23", "genex-family-beta-22"])
def test_beta_code_alone_grants_no_access_over_http(code):
    """§15."""
    app, _, _, topo, _ = build_stack()
    path = child_path(topo.child_alpha.child_id)
    assert call_wsgi(app, path, bearer=f"Bearer {code}")[0] == 401
    assert call_wsgi(app, path, bearer=None,
                     headers={"X-Beta-Access-Code": code})[0] == 401


def test_modified_child_id_does_not_grant_access():
    """§14."""
    app, _, _, topo, _ = build_stack()
    real = topo.child_alpha.child_id
    for forged in (real[:-1] + ("a" if real[-1] != "a" else "b"),
                   real.upper(), real + "x", "chld_" + "0" * 32):
        status, _, _ = call_wsgi(app, child_path(forged),
                                 bearer="Bearer token-caregiver-alpha")
        assert status == 403, forged


def test_path_traversal_and_odd_ids_do_not_route_to_the_handler():
    app, repos, _, topo, _ = build_stack()
    for path in ("/pilot/children/../children/x/access-check",
                 "/pilot/children//access-check",
                 "/pilot/children/a/b/access-check",
                 "/pilot/children/x/access-check/extra"):
        status, _, _ = call_wsgi(app, path, bearer="Bearer token-caregiver-alpha")
        assert status in (403, 404), path
    assert repos.children.get_calls == 0


# ===========================================================================
# 17-18. the public surface stays narrow
# ===========================================================================

def test_health_is_the_only_public_route():
    """§17."""
    table = route_templates()
    assert [t for t, public in table.items() if public] == ["/health"]


@pytest.mark.parametrize("path", [
    "/", "/pilot", "/pilot/children", "/admin", "/debug", "/metrics",
    "/healthz", "/health/db", "/.env", "/pilot/children/x", "/openapi.json",
])
def test_unregistered_routes_are_404_and_never_public(path):
    """§18: an unregistered path is not served and is not public."""
    app, _, _, _, _ = build_stack()
    status, body, _ = call_wsgi(app, path)
    assert status == 404, path
    assert body == {"error": "not found"}


def test_non_get_methods_are_rejected():
    app, _, _, topo, _ = build_stack()
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert call_wsgi(app, "/health", method=method)[0] == 405
        assert call_wsgi(app, child_path(topo.child_alpha.child_id), method=method,
                         bearer="Bearer token-caregiver-alpha")[0] == 405


def test_route_table_and_apisurface_cannot_disagree():
    """`route_templates` asserts agreement with the apisurface allowlist.

    Restated by hand on purpose. Every route this application serves is listed
    here with its public flag, so adding a route is not complete until someone
    has written down whether it is public — which is the moment to notice if
    the answer is the wrong one.
    """
    assert route_templates() == {
        "/health": True,
        PROTECTED_CHILD_ROUTE: False,
        # 0.5A identity surface. Protected, every one.
        "/pilot/me": False,
        "/pilot/me/children": False,
        "/pilot/bootstrap/caregiver": False,
        "/pilot/parent-sessions/{session_id}/link-child": False,
        # 0.5B provider connections. Protected, every one.
        "/pilot/children/{child_id}/provider-connections/{provider_id}": False,
        "/pilot/children/{child_id}/provider-connections": False,
        "/pilot/provider-connections/{connection_id}/{action}": False,
        "/pilot/children/{child_id}/managing-clinician": False,
        "/pilot/children/{child_id}/managing-clinician/{provider_id}": False,
        "/pilot/children/{child_id}/managing-clinician/end": False,
        # 0.5C Tuesday-minimum workflow surface. Protected, every one.
        #
        # Seventeen route/method pairs over fourteen templates. Still ABSENT,
        # and asserted absent by the companion test below: provisioning,
        # provider lookup/search/directory, provider-initiated invitation, and
        # public self-registration.
        "/pilot/children/{child_id}/goals": False,
        "/pilot/children/{child_id}/goal-suggestions": False,
        # 0.5F-B provider generation TRIGGER. Protected, provider-only, and
        # additionally managing-clinician-only. POST because it writes.
        "/pilot/children/{child_id}/goal-suggestions/generate": False,
        "/pilot/children/{child_id}/monthly-plan": False,
        "/pilot/children/{child_id}/current-cycle": False,
        "/pilot/children/{child_id}/rtm": False,
        "/pilot/goals/{goal_kind}/{goal_id}/revisions": False,
        "/pilot/monthly-plans/{focus_plan_id}/allocations": False,
        "/pilot/monthly-plans/{focus_plan_id}/activate": False,
        "/pilot/cycles/{cycle_id}/observations": False,
        "/pilot/cycles/{cycle_id}/defers": False,
        "/pilot/rtm-periods/{period_id}/reviews": False,
        "/pilot/rtm-periods/{period_id}/time-entries": False,
        "/pilot/rtm-periods/{period_id}/interactions": False,
        "/pilot/rtm-periods/{period_id}/report": False,
        "/pilot/rtm-reviews/{review_id}/actions": False,
    }


def test_no_route_exposes_provisioning_lookup_or_discovery():
    """The deferred surfaces are absent, derived rather than remembered.

    0.5B defers provider-to-family invitation, family search, a provider
    directory and public self-registration. Each discloses which families or
    clinicians exist, so their absence is a security property rather than a
    backlog item — which makes it worth a test that fails if one quietly
    appears.

    Provisioning is absent too, and for a reason beyond scope: creating a
    Provider is an administrative act performed on someone else's behalf, and
    `ActorRole` has no principal that could authorize it. An endpoint for it
    would have to either invent an admin role or authorize nobody, and the
    second IS public self-registration.
    """
    banned = ("search", "directory", "lookup", "register", "signup",
              "sign-up", "invite-family", "families", "discover")
    for template in route_templates():
        lowered = template.lower()
        for fragment in banned:
            assert fragment not in lowered, (template, fragment)
    assert not any(t.rstrip("/").endswith("providers")
                   for t in route_templates()), route_templates()


def test_health_is_still_the_only_public_route():
    """The default direction, derived rather than restated.

    0.5A adds four routes and `/health` must remain the only unauthenticated
    one. Computed from `ROUTE_TABLE`, so this cannot pass by agreeing with a
    stale hand-written copy of the list.
    """
    from pilot_backend.apisurface.surface import PUBLIC_ROUTES
    from pilot_backend.transport.wsgi_app import ROUTE_TABLE

    public = {template for _, template, is_public in ROUTE_TABLE if is_public}
    assert public == {"/health"} == set(PUBLIC_ROUTES)


# ===========================================================================
# 19-20. status mapping and response safety
# ===========================================================================

def test_401_and_403_remain_correctly_separated_over_http():
    """§19."""
    app, _, _, topo, _ = build_stack()
    path = child_path(topo.child_alpha.child_id)
    assert call_wsgi(app, path, bearer=None)[0] == 401
    assert call_wsgi(app, path, bearer="Bearer nonsense")[0] == 401
    assert call_wsgi(app, path, bearer="Bearer token-unprovisioned")[0] == 403
    assert call_wsgi(app, path, bearer="Bearer token-caregiver-gamma")[0] == 403


def test_response_never_contains_internal_exception_detail():
    """§20: an exploding dependency yields a bare 500."""
    class ExplodingVerifier:
        environment = "dev"

        def verify(self, bearer):
            raise RuntimeError(
                f"vendor blew up on {SENTINEL_NOTE} secret={SENTINEL_SECRET}")

    settings = dev_settings()
    base = FirestoreRepositories(FakeDocumentStore())
    topo = build_secure_topology(base)
    logs = []
    app = build_application(settings=settings, repos=base,
                            verifier=ExplodingVerifier(), log_sink=logs)

    with LiveServer(app) as server:
        status, body, _ = server.get(child_path(topo.child_alpha.child_id),
                                     bearer="Bearer x")

    assert status == 500
    assert body == {"error": "internal error"}
    blob = json.dumps(body) + "\n".join(logs)
    for sentinel in ALL_SENTINELS:
        assert sentinel not in blob, sentinel
    for leak in ("RuntimeError", "Traceback", "vendor blew up"):
        assert leak not in blob, leak


def test_error_bodies_are_constant_per_status():
    app, _, _, topo, _ = build_stack()
    bodies = {}
    for bearer in (None, "Bearer bad", "Bearer " + SENTINEL_TOKEN):
        status, body, _ = call_wsgi(app, child_path(topo.child_alpha.child_id),
                                    bearer=bearer)
        bodies.setdefault(status, set()).add(json.dumps(body, sort_keys=True))
    for status, variants in bodies.items():
        assert len(variants) == 1, (status, variants)


def test_successful_response_carries_no_clinical_or_contact_field():
    app, _, _, topo, _ = build_stack()
    _, body, _ = call_wsgi(app, child_path(topo.child_alpha.child_id),
                           bearer="Bearer token-caregiver-alpha")
    assert set(body) == {"authorized", "child_id", "actor_role", "request_id"}
    for banned in ("name", "email", "diagnosis", "note", "concern",
                   "auth_subject", "display_name"):
        assert banned not in json.dumps(body).lower()


# ===========================================================================
# logging / error safety at the HTTP boundary
# ===========================================================================

def test_no_sentinel_reaches_the_logs_from_any_direction():
    """Token, headers, query string and body all carry sentinels."""
    app, _, _, topo, logs = build_stack()
    query = urllib.parse.urlencode({"concern": SENTINEL_CONCERN,
                                    "email": SENTINEL_EMAIL})
    call_wsgi(app, child_path(topo.child_alpha.child_id),
              bearer="Bearer " + SENTINEL_TOKEN,
              headers={"X-Note": SENTINEL_NOTE, "X-Secret": SENTINEL_SECRET},
              query=query,
              body=json.dumps({"note": SENTINEL_NOTE}).encode())
    call_wsgi(app, child_path(topo.child_alpha.child_id),
              bearer="Bearer token-caregiver-alpha", query=query)

    blob = "\n".join(logs)
    assert blob, "nothing was logged; the assertion would be vacuous"
    for sentinel in ALL_SENTINELS:
        assert sentinel not in blob, sentinel


def test_logs_record_route_templates_not_populated_paths():
    app, _, _, topo, logs = build_stack()
    call_wsgi(app, child_path(topo.child_alpha.child_id),
              bearer="Bearer token-caregiver-alpha")
    blob = "\n".join(logs)
    assert PROTECTED_CHILD_ROUTE in blob
    assert topo.child_alpha.child_id not in blob
    assert "?" not in blob


def test_logs_never_contain_the_authorization_header():
    app, _, _, topo, logs = build_stack()
    call_wsgi(app, child_path(topo.child_alpha.child_id),
              bearer="Bearer " + SENTINEL_TOKEN)
    blob = "\n".join(logs)
    for marker in (SENTINEL_TOKEN, "Bearer", "authorization"):
        assert marker.lower() not in blob.lower(), marker


def test_every_log_line_is_validated_json():
    app, _, _, topo, logs = build_stack()
    call_wsgi(app, "/health")
    call_wsgi(app, child_path(topo.child_alpha.child_id),
              bearer="Bearer token-caregiver-alpha")
    call_wsgi(app, "/nope")
    assert len(logs) == 3
    for line in logs:
        parsed = json.loads(line)
        assert parsed["event"] == "http_request"
        assert "request_id" in parsed and "status" in parsed


# ===========================================================================
# audit behaviour at the HTTP boundary
# ===========================================================================

def test_protected_request_produces_a_complete_audit_event():
    app, _, base, topo, _ = build_stack()
    call_wsgi(app, child_path(topo.child_alpha.child_id),
              bearer="Bearer token-caregiver-alpha",
              headers={"X-Request-Id": "req-fictional-001"})

    events = base.audit_events.list_all()
    assert len(events) == 1
    event = events[0]
    assert event.action is AuditAction.CHILD_ACCESS_GRANTED
    assert event.result is AuditResult.SUCCESS
    assert event.actor_application_id == topo.caregiver_alpha.caregiver_id
    assert event.actor_role.value == "caregiver"
    assert event.resource_type == "child"
    assert event.child_id == topo.child_alpha.child_id
    assert event.request_id == "req-fictional-001"
    assert event.occurred_at.tzinfo is not None
    assert event.event_id


def test_refused_requests_are_audited_with_the_right_outcome():
    app, _, base, topo, _ = build_stack()
    call_wsgi(app, child_path(topo.child_beta.child_id),
              bearer="Bearer token-caregiver-alpha")
    call_wsgi(app, child_path(topo.child_alpha.child_id), bearer=None)

    events = base.audit_events.list_all()
    assert {e.action for e in events} == {
        AuditAction.AUTHORIZATION_FAILURE, AuditAction.AUTHENTICATION_FAILURE}
    assert all(e.result is AuditResult.FAILURE for e in events)
    denials = {e.metadata.get("denial_reason") for e in events}
    assert Denial.NO_RELATIONSHIP.value in denials
    assert Denial.NO_TOKEN.value in denials


def test_audit_documents_contain_no_sentinel_after_an_http_request():
    app, _, base, topo, _ = build_stack()
    call_wsgi(app, child_path(topo.child_alpha.child_id),
              bearer="Bearer " + SENTINEL_TOKEN,
              headers={"X-Note": SENTINEL_NOTE},
              query=urllib.parse.urlencode({"concern": SENTINEL_CONCERN}))
    call_wsgi(app, child_path(topo.child_alpha.child_id),
              bearer="Bearer token-caregiver-alpha")

    documents = [encode(e) for e in base.audit_events.list_all()]
    assert documents, "no audit events written; the assertion would be vacuous"
    blob = json.dumps(documents)
    for sentinel in ALL_SENTINELS:
        assert sentinel not in blob, sentinel


def test_the_request_id_correlates_log_and_audit():
    app, _, base, topo, logs = build_stack()
    call_wsgi(app, child_path(topo.child_alpha.child_id),
              bearer="Bearer token-caregiver-alpha",
              headers={"X-Request-Id": "req-fictional-042"})
    assert base.audit_events.list_all()[0].request_id == "req-fictional-042"
    assert "req-fictional-042" in "\n".join(logs)


def test_a_request_id_is_generated_when_the_client_sends_none():
    app, _, base, topo, _ = build_stack()
    _, _, headers = call_wsgi(app, child_path(topo.child_alpha.child_id),
                              bearer="Bearer token-caregiver-alpha")
    generated = headers["X-Request-Id"]
    assert generated
    assert base.audit_events.list_all()[0].request_id == generated


# ===========================================================================
# the transport introduces no new dependency
# ===========================================================================

def test_transport_imports_no_web_framework_or_sdk():
    import ast
    from pathlib import Path

    banned = {"fastapi", "starlette", "flask", "django", "uvicorn", "httpx",
              "requests", "aiohttp", "pydantic", "firebase_admin", "google",
              "firestore", "boto3"}
    transport = Path(__file__).resolve().parents[1] / "transport"
    for path in sorted(transport.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in banned, (path.name, alias.name)
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                assert node.module.split(".")[0] not in banned, (path.name, node.module)


def test_the_application_is_a_plain_wsgi_callable():
    """Mountable under any WSGI server, and under ASGI via a bridge."""
    app, _, _, _, _ = build_stack()
    assert callable(app)
    status, _, headers = call_wsgi(app, "/health")
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert "Content-Length" in headers
