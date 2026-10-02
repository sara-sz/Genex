"""The staging edge: CORS behaviour and the fail-closed startup contract.

These test DEPLOYMENT code, not product logic. The frozen transport is treated
as a black box here — wrapped, never reached into — because the point of the
middleware is that it adds nothing to the request path except an origin
decision.
"""

from __future__ import annotations

import pytest

from pilot_backend.apisurface.surface import CorsPolicy
from pilot_runtime.http import PREFLIGHT_MAX_AGE_SECONDS, CorsMiddleware
from pilot_runtime.server import REQUIRED_VARIABLES, StagingConfigError, _require

PARENT = "https://genex-kiddo-compass.lovable.app"
THERAPIST = "https://your-childs-plan.lovable.app"
POLICY = CorsPolicy(allowed_origins=(PARENT, THERAPIST))


class RecordingApp:
    """A stand-in for the frozen application. Records what reached it."""

    def __init__(self, status: str = "200 OK") -> None:
        self.status = status
        self.calls = []

    def __call__(self, environ, start_response):
        self.calls.append((environ.get("REQUEST_METHOD"),
                           environ.get("PATH_INFO")))
        start_response(self.status, [("Content-Type", "application/json")])
        return [b'{"ok": true}']


def call(app, *, method="GET", path="/pilot/me", origin=None):
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path}
    if origin is not None:
        environ["HTTP_ORIGIN"] = origin
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = headers

    body = b"".join(app(environ, start_response))
    captured["body"] = body
    captured["map"] = {}
    for name, value in captured["headers"]:
        captured["map"].setdefault(name.lower(), []).append(value)
    return captured


# ===========================================================================
# an allowed origin
# ===========================================================================

@pytest.mark.parametrize("origin", [PARENT, THERAPIST])
def test_an_allowed_origin_is_echoed_exactly(origin):
    inner = RecordingApp()
    result = call(CorsMiddleware(inner, policy=POLICY), origin=origin)

    assert result["map"]["access-control-allow-origin"] == [origin]
    assert result["map"]["access-control-allow-credentials"] == ["true"]
    assert "Origin" in result["map"]["vary"]
    # The request still reached the frozen application.
    assert inner.calls == [("GET", "/pilot/me")]


def test_the_wildcard_is_never_emitted():
    """Credentialed CORS with `*` is rejected by browsers and wrong here."""
    result = call(CorsMiddleware(RecordingApp(), policy=POLICY), origin=PARENT)
    assert "*" not in result["map"]["access-control-allow-origin"]


def test_a_refusal_still_carries_the_origin_header():
    """A 403 must be READABLE by the app, or the UI cannot show why.

    Without the header the browser hides the response and the frontend sees an
    opaque network error instead of the refusal the server actually sent.
    """
    inner = RecordingApp(status="403 Forbidden")
    result = call(CorsMiddleware(inner, policy=POLICY), origin=PARENT)
    assert result["status"] == "403 Forbidden"
    assert result["map"]["access-control-allow-origin"] == [PARENT]


# ===========================================================================
# a disallowed origin
# ===========================================================================

@pytest.mark.parametrize("origin", [
    "https://evil.example",
    "http://genex-kiddo-compass.lovable.app",      # scheme downgrade
    "https://genex-kiddo-compass.lovable.app.evil.example",  # suffix attack
    "https://genex-kiddo-compass.lovable.app:8443",          # port added
    "https://GENEX-KIDDO-COMPASS.lovable.app",     # case differs
])
def test_a_disallowed_origin_gets_no_cors_header(origin):
    inner = RecordingApp()
    result = call(CorsMiddleware(inner, policy=POLICY), origin=origin)

    assert "access-control-allow-origin" not in result["map"]
    assert "access-control-allow-credentials" not in result["map"]
    # Still varies, so a cache cannot replay this to a permitted origin.
    assert "Origin" in result["map"]["vary"]
    # And the application still ran: CORS is not an authorization layer. The
    # response simply is not readable by the calling page.
    assert inner.calls == [("GET", "/pilot/me")]


def test_no_origin_header_means_no_cors_headers():
    """curl, server-side fetch and same-origin requests are untouched."""
    result = call(CorsMiddleware(RecordingApp(), policy=POLICY), origin=None)
    assert "access-control-allow-origin" not in result["map"]
    assert "vary" not in result["map"]


# ===========================================================================
# preflight
# ===========================================================================

def test_a_preflight_is_answered_without_reaching_the_application():
    """A browser sends no Authorization on a preflight; the frozen app would
    correctly refuse it. Answering here does not widen the auth surface."""
    inner = RecordingApp()
    result = call(CorsMiddleware(inner, policy=POLICY),
                  method="OPTIONS", origin=PARENT)

    assert result["status"] == "204 No Content"
    assert inner.calls == [], "the application must not see the preflight"
    assert result["map"]["access-control-allow-methods"] == ["GET, POST, OPTIONS"]
    assert result["map"]["access-control-allow-headers"] == [
        "Authorization, Content-Type"]
    assert result["map"]["access-control-max-age"] == [
        str(PREFLIGHT_MAX_AGE_SECONDS)]


def test_preflight_is_identical_for_a_real_and_a_nonexistent_path():
    """The middleware must not become a route oracle.

    0.5A/0.5B/0.5C all preserve non-enumerating failure. A per-path preflight
    would hand an unauthenticated caller a map of the API.
    """
    app = CorsMiddleware(RecordingApp(), policy=POLICY)
    real = call(app, method="OPTIONS", path="/pilot/children/abc/goals",
                origin=PARENT)
    fake = call(app, method="OPTIONS", path="/pilot/not-a-route",
                origin=PARENT)

    assert real["status"] == fake["status"]
    assert sorted(real["headers"]) == sorted(fake["headers"])


def test_a_preflight_from_a_disallowed_origin_carries_no_cors_headers():
    result = call(CorsMiddleware(RecordingApp(), policy=POLICY),
                  method="OPTIONS", origin="https://evil.example")
    assert "access-control-allow-origin" not in result["map"]
    assert "access-control-allow-methods" not in result["map"]


def test_the_middleware_refuses_to_be_built_without_a_policy():
    with pytest.raises(ValueError):
        CorsMiddleware(RecordingApp(), policy=None)
    with pytest.raises(ValueError):
        CorsMiddleware(None, policy=POLICY)


# ===========================================================================
# the fail-closed startup contract
# ===========================================================================

def base_env():
    return {
        "PILOT_ENVIRONMENT": "dev",
        "PILOT_GCP_PROJECT_ID": "genex-pilot-staging",
        "PILOT_FIREBASE_PROJECT_ID": "genex-pilot-staging",
        "PILOT_FIRESTORE_DATABASE": "pilot-staging",
        "PILOT_ALLOWED_ORIGINS": f"{PARENT},{THERAPIST}",
    }


def test_the_reference_staging_configuration_is_accepted():
    _require(base_env())  # must not raise


@pytest.mark.parametrize("missing", REQUIRED_VARIABLES)
def test_every_required_variable_is_individually_required(missing):
    env = base_env()
    del env[missing]
    with pytest.raises(StagingConfigError) as caught:
        _require(env)
    assert missing in str(caught.value)


@pytest.mark.parametrize("missing", REQUIRED_VARIABLES)
def test_a_blank_variable_is_as_absent_as_a_missing_one(missing):
    env = base_env()
    env[missing] = "   "
    with pytest.raises(StagingConfigError):
        _require(env)


def test_prod_is_refused_by_this_entrypoint():
    env = base_env()
    env["PILOT_ENVIRONMENT"] = "prod"
    with pytest.raises(StagingConfigError) as caught:
        _require(env)
    assert "fictional staging" in str(caught.value)


@pytest.mark.parametrize("flag", ["1", "true", "YES", "on"])
def test_dev_auth_is_refused(flag):
    """Staging must authenticate with real Firebase tokens, not a dev shim."""
    env = base_env()
    env["PILOT_DEV_AUTH_ENABLED"] = flag
    with pytest.raises(StagingConfigError) as caught:
        _require(env)
    assert "PILOT_DEV_AUTH_ENABLED" in str(caught.value)


@pytest.mark.parametrize("variable", ["PILOT_FIRESTORE_EMULATOR_HOST",
                                      "FIRESTORE_EMULATOR_HOST"])
def test_an_emulator_is_refused_whether_configured_or_ambient(variable):
    """The ambient one matters more: the Firestore client reads it itself, so
    a process that merely inherited it would write to nowhere successfully."""
    env = base_env()
    env[variable] = "localhost:8080"
    with pytest.raises(StagingConfigError) as caught:
        _require(env)
    assert variable in str(caught.value)


def test_a_parent_session_bucket_is_refused():
    """This service reaches no Parent 2.3 storage at all."""
    from pilot_runtime.integration.parent_gcs_source import BUCKET_ENV_VAR

    env = base_env()
    env[BUCKET_ENV_VAR] = "genex-api-prod-sessions-genex-mvp-2026"
    with pytest.raises(StagingConfigError) as caught:
        _require(env)
    assert BUCKET_ENV_VAR in str(caught.value)


@pytest.mark.parametrize("origins", [
    "*",
    "http://genex-kiddo-compass.lovable.app",
    f"{PARENT},http://localhost:5173",
    "   ,  ",
])
def test_an_unsafe_origin_list_is_refused(origins):
    env = base_env()
    env["PILOT_ALLOWED_ORIGINS"] = origins
    with pytest.raises(StagingConfigError):
        _require(env)


def test_the_refusal_never_quotes_a_value():
    """Startup logs must stay free of configuration VALUES."""
    env = base_env()
    env["PILOT_GCP_PROJECT_ID"] = "a-secret-looking-value"
    env["PILOT_ENVIRONMENT"] = "prod"
    with pytest.raises(StagingConfigError) as caught:
        _require(env)
    assert "a-secret-looking-value" not in str(caught.value)


def test_the_error_is_marked_phi_safe():
    assert getattr(StagingConfigError, "PHI_SAFE_MESSAGE", False) is True
