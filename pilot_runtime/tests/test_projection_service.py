"""The projection service's identity verifier, entrypoint and artifacts.

Three things are under test:

  1. `GoogleServiceIdentityVerifier` — the defence-in-depth claim checks.
     Cloud Run IAM is the authoritative caller gate; these assertions are what
     make the expected caller readable and testable from inside the app.
  2. The entrypoint fails closed and builds no browser machinery.
  3. The three deploy artifacts cannot converge: the projection image cannot
     become the browser API, and the browser API's surface is unchanged.

The signature path is injected rather than reaching Google. What is tested
here is everything the verifier does with the claims AFTER `google-auth` has
validated the signature, audience and expiry — which is the part this code
owns.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from pilot_runtime.google_oidc import (
    GOOGLE_ISSUERS,
    GoogleServiceIdentityVerifier,
    ServiceIdentityError,
)
from pilot_runtime.projection_server import (
    AUDIENCE_ENV_VAR,
    CALLER_ENV_VAR,
    ProjectionConfigError,
    build_projection_application,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "pilot_runtime" / "deploy"

AUDIENCE = "https://pilot-projection-staging-abc-uc.a.run.app"
PARENT_STAGING_SA = (
    "genex-parent-staging-run@genex-mvp-2026.iam.gserviceaccount.com")
PARENT_PROD_SA = "genex-parent-prod-run@genex-mvp-2026.iam.gserviceaccount.com"
DEFAULT_COMPUTE_SA = "1003012205867-compute@developer.gserviceaccount.com"

BASE_ENV = {
    "PILOT_ENVIRONMENT": "dev",
    "PILOT_GCP_PROJECT_ID": "genex-pilot-staging",
    "PILOT_FIREBASE_PROJECT_ID": "genex-pilot-staging",
    "PILOT_FIRESTORE_DATABASE": "pilot-staging",
}


def _claims(**overrides):
    base = {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "email": PARENT_STAGING_SA,
        "email_verified": True,
        "sub": "1234567890",
    }
    base.update(overrides)
    return base


def _verifier(claims=None, *, audience=AUDIENCE,
              caller=PARENT_STAGING_SA, raises=None):
    def fake(token, aud):
        if raises is not None:
            raise raises
        return claims if claims is not None else _claims(aud=aud)

    return GoogleServiceIdentityVerifier(
        audience=audience, expected_service_account=caller, verifier=fake)


# ---------------------------------------------------------------------------
# 1. the verifier
# ---------------------------------------------------------------------------

def test_the_expected_parent_staging_identity_is_accepted():
    claims = _verifier().verify(f"Bearer tok")
    assert claims["email"] == PARENT_STAGING_SA


def test_an_unauthenticated_request_is_refused():
    for header in (None, "", "   ", "tok", "Basic abc", "Bearer", "Bearer   "):
        with pytest.raises(ServiceIdentityError):
            _verifier().verify(header)


def test_the_parent_PROD_identity_is_refused():
    """Decision 2: prod must remain disconnected from Pilot staging. Even a
    perfectly valid Google token from the prod service account is refused."""
    with pytest.raises(ServiceIdentityError):
        _verifier(_claims(email=PARENT_PROD_SA)).verify("Bearer tok")


def test_the_default_compute_identity_is_refused():
    """The blocker that made a dedicated Parent SA a prerequisite: the default
    compute account is shared by every workload in the Parent project, so it
    must never be an accepted caller."""
    with pytest.raises(ServiceIdentityError):
        _verifier(_claims(email=DEFAULT_COMPUTE_SA)).verify("Bearer tok")


@pytest.mark.parametrize("email", [
    "", None,
    # A suffix or project match would accept any SA in the Parent project.
    "someone-else@genex-mvp-2026.iam.gserviceaccount.com",
    "genex-parent-staging-run@evil.iam.gserviceaccount.com",
    "genex-parent-staging-run@genex-mvp-2026.iam.gserviceaccount.com.evil.com",
    "prefix-genex-parent-staging-run@genex-mvp-2026.iam.gserviceaccount.com",
])
def test_only_an_exact_caller_match_is_accepted(email):
    with pytest.raises(ServiceIdentityError):
        _verifier(_claims(email=email)).verify("Bearer tok")


def test_the_caller_match_is_case_insensitive_on_the_address():
    """Email addresses are not case sensitive, and a token could legitimately
    carry a different case. The comparison is lowered on both sides."""
    verifier = GoogleServiceIdentityVerifier(
        audience=AUDIENCE, expected_service_account=PARENT_STAGING_SA.upper(),
        verifier=lambda t, a: _claims())
    assert verifier.verify("Bearer tok")["email"] == PARENT_STAGING_SA


def test_an_unverified_email_is_refused():
    for value in (False, None, "true", 1):
        with pytest.raises(ServiceIdentityError):
            _verifier(_claims(email_verified=value)).verify("Bearer tok")


@pytest.mark.parametrize("issuer", [
    "https://securetoken.google.com/genex-pilot-staging",
    "https://securetoken.google.com/genex-mvp-2026",
    "https://evil.example", "", None,
])
def test_a_non_google_issuer_is_refused(issuer):
    """A FIREBASE END-USER TOKEN fails here. That is the structural reason a
    caregiver or provider credential can never authorize the projection
    path — its issuer is `securetoken.google.com/<project>`."""
    with pytest.raises(ServiceIdentityError):
        _verifier(_claims(iss=issuer)).verify("Bearer tok")


def test_the_firebase_issuer_is_not_in_the_accepted_set():
    assert GOOGLE_ISSUERS == {"https://accounts.google.com",
                              "accounts.google.com"}
    for project in ("genex-pilot-staging", "genex-mvp-2026"):
        assert f"https://securetoken.google.com/{project}" not in GOOGLE_ISSUERS


def test_a_wrong_audience_in_the_claims_is_refused():
    """`verify_oauth2_token` already enforces the audience; re-reading it means
    a mutation that dropped the audience argument still fails."""
    with pytest.raises(ServiceIdentityError):
        _verifier(_claims(aud="https://some-other-service.run.app")
                  ).verify("Bearer tok")


def test_a_signature_or_expiry_failure_is_refused():
    """`google-auth` raises for a bad signature, a wrong audience and an
    expired token alike; all collapse to one refusal."""
    with pytest.raises(ServiceIdentityError):
        _verifier(raises=ValueError("Token expired")).verify("Bearer tok")


def test_every_refusal_carries_the_same_message():
    """So a prober cannot learn WHICH check it failed."""
    messages = set()
    for claims in (_claims(email=PARENT_PROD_SA),
                   _claims(iss="https://evil.example"),
                   _claims(email_verified=False),
                   _claims(aud="https://other.run.app")):
        with pytest.raises(ServiceIdentityError) as caught:
            _verifier(claims).verify("Bearer tok")
        messages.add(str(caught.value))
    assert len(messages) == 1, messages


def test_the_verifier_cannot_be_built_unconfigured():
    """An `allUsers`-reachable service accepting anyone would be the worst
    failure, so an unconfigured verifier cannot exist."""
    for audience, caller in ((None, PARENT_STAGING_SA), ("", PARENT_STAGING_SA),
                             (AUDIENCE, None), (AUDIENCE, ""),
                             (AUDIENCE, "not-an-email")):
        with pytest.raises(ServiceIdentityError):
            GoogleServiceIdentityVerifier(
                audience=audience, expected_service_account=caller)


def test_the_error_is_phi_safe():
    assert ServiceIdentityError.PHI_SAFE_MESSAGE is True


# ---------------------------------------------------------------------------
# 2. the entrypoint
# ---------------------------------------------------------------------------

def test_the_entrypoint_requires_an_audience_and_a_caller():
    """In `iam_plus_token` only — see the auth-mode tests below for why
    `iam_only` deliberately requires neither."""
    mode = {"PILOT_PROJECTION_AUTH_MODE": "iam_plus_token"}
    with pytest.raises(ProjectionConfigError):
        build_projection_application(dict(BASE_ENV, **mode))
    with pytest.raises(ProjectionConfigError):
        build_projection_application(dict(BASE_ENV, **mode,
                                          **{AUDIENCE_ENV_VAR: AUDIENCE}))
    with pytest.raises(ProjectionConfigError):
        build_projection_application(dict(
            BASE_ENV, **mode, **{CALLER_ENV_VAR: PARENT_STAGING_SA}))


def test_the_entrypoint_refuses_production():
    """0.5F-A2 is the fictional staging pairing. Production gets its own
    service and its own review after PRE-PHI approval."""
    # PROD-SHAPED values throughout, so this reaches THIS module's check.
    # `PilotSettings.from_env` independently refuses a prod environment whose
    # project id contains "staging" — useful defence in depth, but it meant a
    # staging-named env never got as far as the assertion under test.
    env = {
        "PILOT_ENVIRONMENT": "prod",
        "PILOT_GCP_PROJECT_ID": "genex-pilot-production",
        "PILOT_FIREBASE_PROJECT_ID": "genex-pilot-production",
        "PILOT_FIRESTORE_DATABASE": "pilot-production",
        "PILOT_ALLOWED_ORIGINS": "https://example.invalid",
        AUDIENCE_ENV_VAR: AUDIENCE,
        CALLER_ENV_VAR: PARENT_STAGING_SA,
        "PILOT_PROJECTION_AUTH_MODE": "iam_plus_token",
    }
    # The SPECIFIC error, not any exception: accepting any failure let a
    # mutation that removed this check survive on a different error.
    with pytest.raises(ProjectionConfigError) as caught:
        build_projection_application(env)
    assert "production" in str(caught.value)


def test_the_entrypoint_assembles_with_an_injected_verifier():
    """Both seams injected: a verifier so no token is needed, and a Firestore
    client so the assembly does not try to reach a real project. Those are the
    only two collaborators the entrypoint has — which is the point."""
    class _Always:
        def verify(self, header):
            return _claims()

    class _Collection:
        def document(self, *a, **k):
            return self

        def get(self, *a, **k):
            return None

    class _FakeFirestore:
        def collection(self, *a, **k):
            return _Collection()

    app = build_projection_application(
        dict(BASE_ENV, **{AUDIENCE_ENV_VAR: AUDIENCE,
                          CALLER_ENV_VAR: PARENT_STAGING_SA,
                          "PILOT_PROJECTION_AUTH_MODE": "iam_plus_token"}),
        verifier=_Always(), firestore_client=_FakeFirestore())
    assert callable(app)


def test_the_entrypoint_builds_no_browser_machinery():
    """No Firebase decoder, no CORS, no routed browser app. Their absence is
    what makes a browser request impossible rather than merely unauthorised."""
    tree = ast.parse((REPO_ROOT / "pilot_runtime"
                      / "projection_server.py").read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names |= {a.name for a in node.names}
    for forbidden in ("CorsMiddleware", "build_runtime", "build_wsgi_application",
                      "FirebaseTokenDecoder", "build_token_decoder",
                      "initialize_firebase_app"):
        assert forbidden not in names, forbidden


def test_the_projection_entrypoint_imports_no_firebase_sdk():
    """The projection image does not install `firebase-admin`, so the service
    must not import it — otherwise the container fails at startup.

    This was a REAL bug, not a hypothetical: the entrypoint originally reached
    `build_store` through `pilot_runtime.composition`, and separately imported
    the verifier from `pilot_runtime.auth`, and BOTH pull `firebase_admin` at
    module level. The store is now built directly from `firestore_store` and
    the verifier lives at `pilot_runtime/google_oidc.py`, outside the `auth`
    package whose `__init__` imports the decoder.
    """
    import builtins
    import sys

    for module in [m for m in list(sys.modules)
                   if m.startswith("pilot_runtime.projection_server")]:
        del sys.modules[module]

    real = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.split(".")[0] == "firebase_admin":
            raise ImportError(f"blocked for this test: {name}")
        return real(name, *args, **kwargs)

    builtins.__import__ = blocked
    try:
        from pilot_runtime import projection_server as reimported
        with pytest.raises(reimported.ProjectionConfigError):
            reimported.build_projection_application({})
    finally:
        builtins.__import__ = real


def test_the_projection_graph_names_no_firebase_module():
    """Structural companion to the import test above, over the AST — so the
    reason is visible without running anything."""
    def graph(start: str) -> set:
        seen, queue = set(), [start]
        while queue:
            rel = queue.pop()
            if rel in seen:
                continue
            seen.add(rel)
            path = REPO_ROOT / rel
            if not path.is_file():
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module \
                        and node.level == 0:
                    modules = [node.module]
                for module in modules:
                    if module.split(".")[0] == "firebase_admin":
                        raise AssertionError(
                            f"{rel} imports firebase_admin")
                    if module.startswith(("pilot_runtime", "pilot_backend")):
                        queue.append(module.replace(".", "/") + ".py")
                        queue.append(module.replace(".", "/") + "/__init__.py")
        return seen

    reached = graph("pilot_runtime/projection_server.py")
    assert "pilot_runtime/auth/__init__.py" not in reached
    assert "pilot_runtime/composition.py" not in reached


def test_the_projection_entrypoint_is_not_the_browser_entrypoint():
    """Walked over the import graph: neither artifact can become the other."""
    def graph(start: str) -> set:
        seen, queue = set(), [start]
        while queue:
            rel = queue.pop()
            if rel in seen:
                continue
            seen.add(rel)
            path = REPO_ROOT / rel
            if not path.is_file():
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module \
                        and node.level == 0:
                    modules = [node.module]
                for module in modules:
                    if module.startswith(("pilot_runtime", "pilot_backend")):
                        queue.append(module.replace(".", "/") + ".py")
                        queue.append(module.replace(".", "/") + "/__init__.py")
        return seen

    browser = graph("pilot_runtime/server.py")
    projection = graph("pilot_runtime/projection_server.py")

    assert "pilot_runtime/server.py" in browser
    assert "pilot_runtime/projection_server.py" in projection
    # The browser app must never reach the projection transport...
    assert "pilot_backend/transport/projection_wsgi.py" not in browser
    assert "pilot_runtime/projection_server.py" not in browser
    # ...and the projection service must never reach the browser CORS layer.
    assert "pilot_runtime/http/cors.py" not in projection
    assert "pilot_runtime/server.py" not in projection


# ---------------------------------------------------------------------------
# 3. the three deploy artifacts stay distinct
# ---------------------------------------------------------------------------

SERVING_REQUIREMENTS = {
    "firebase-admin==7.5.0",
    "google-cloud-firestore==2.28.0",
    "gunicorn==23.0.0",
}

PROJECTION_REQUIREMENTS = {
    "google-cloud-firestore==2.28.0",
    "google-auth==2.49.2",
    "requests==2.33.1",
    "gunicorn==23.0.0",
}


def _pins(name: str) -> set:
    return {line.strip() for line
            in (DEPLOY / name).read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")}


def test_the_serving_image_requirements_are_still_unchanged():
    """0.5F-A2 adds a third image. The browser-facing API's dependency surface
    must be byte-identical to what 0.5E-B froze."""
    assert _pins("requirements.txt") == SERVING_REQUIREMENTS


def test_the_generation_image_requirements_are_unchanged():
    assert _pins("requirements-generation.txt") == {
        "firebase-admin==7.5.0", "google-cloud-firestore==2.28.0",
        "pandas==3.0.3", "openpyxl==3.1.5"}


def test_the_projection_image_pins_exactly_its_four_dependencies():
    assert _pins("requirements-projection.txt") == PROJECTION_REQUIREMENTS


def test_the_projection_image_installs_no_firebase_sdk():
    """THE important omission. With no Firebase SDK in the image, a caregiver
    or provider token cannot be verified here even by a future edit — the
    capability is absent, not merely unused."""
    for banned in ("firebase-admin", "pandas", "openpyxl", "openai",
                   "anthropic", "streamlit"):
        assert not any(p.lower().startswith(banned)
                       for p in PROJECTION_REQUIREMENTS), banned


def test_the_projection_image_declares_google_auth_directly():
    """`google_oidc.py` imports `google.oauth2.id_token` by name, so relying on
    it arriving transitively would let a Firestore bump remove it."""
    assert any(p.startswith("google-auth==") for p in PROJECTION_REQUIREMENTS)


def test_the_projection_dockerfile_copies_no_parent_content():
    text = (DEPLOY / "Dockerfile.projection").read_text()
    copies = [line.strip() for line in text.splitlines()
              if line.strip().upper().startswith("COPY")]
    assert copies
    for line in copies:
        assert "genex-parent" not in line, line


def test_the_projection_build_context_admits_no_parent_content():
    admitted = [line.strip() for line
                in (DEPLOY / "gcloudignore-projection").read_text().splitlines()
                if line.strip().startswith("!")]
    assert admitted
    for line in admitted:
        assert "genex-parent" not in line, line


def test_the_three_build_configs_publish_three_different_images():
    serving = (DEPLOY / "cloudbuild.yaml").read_text()
    generation = (DEPLOY / "cloudbuild-generation.yaml").read_text()
    projection = (DEPLOY / "cloudbuild-projection.yaml").read_text()
    assert "pilot/pilot-api" in serving
    assert "pilot/pilot-generation" in generation
    assert "pilot/pilot-projection" in projection
    for other in (generation, projection):
        assert "pilot/pilot-api" not in other
    assert "pilot/pilot-projection" not in serving
    assert "pilot/pilot-projection" not in generation


def test_the_projection_image_serves_the_projection_entrypoint_only():
    text = (DEPLOY / "Dockerfile.projection").read_text()
    assert "pilot_runtime.projection_server:application" in text
    assert "pilot_runtime.server:application" not in text


def test_the_serving_image_does_not_serve_the_projection_entrypoint():
    text = (DEPLOY / "Dockerfile").read_text()
    assert "pilot_runtime.server:application" in text
    assert "projection" not in text.lower()


# ---------------------------------------------------------------------------
# 4. CORRECTION 2 — Cloud Run IAM is the authoritative gate
#
# App-level re-verification rests on an assumption about what Cloud Run
# delivers to the container, and that assumption is unproven until the
# deployment probe runs. So the mode is DECLARED, never defaulted, and
# `iam_only` is a supported posture rather than a degraded one.
# ---------------------------------------------------------------------------

from pilot_runtime.projection_server import (  # noqa: E402
    AUTH_MODE_ENV_VAR,
    AUTH_MODE_IAM_ONLY,
    AUTH_MODE_IAM_PLUS_TOKEN,
    AUTH_MODES,
)


class _FakeFirestore:
    def collection(self, *a, **k):
        return self

    def document(self, *a, **k):
        return self

    def get(self, *a, **k):
        return None


def _build(mode=None, **extra):
    env = dict(BASE_ENV)
    if mode is not None:
        env[AUTH_MODE_ENV_VAR] = mode
    env.update(extra)
    return build_projection_application(env, firestore_client=_FakeFirestore())


def test_the_auth_mode_has_no_default():
    """An undeclared posture must not start. The two modes rest on different
    guarantees, so defaulting either way would hide which one is in force."""
    with pytest.raises(ProjectionConfigError):
        _build(None)


@pytest.mark.parametrize("mode", ["", "  ", "iam", "token", "IAM_ONLY",
                                  "iam_only_please", "none", "off"])
def test_an_unrecognised_auth_mode_is_refused(mode):
    with pytest.raises(ProjectionConfigError):
        _build(mode)


def test_exactly_two_modes_exist():
    assert AUTH_MODES == (AUTH_MODE_IAM_ONLY, AUTH_MODE_IAM_PLUS_TOKEN)
    assert AUTH_MODE_IAM_ONLY == "iam_only"
    assert AUTH_MODE_IAM_PLUS_TOKEN == "iam_plus_token"


def test_iam_only_starts_and_inspects_no_token():
    """The supported posture until the probe proves the header behaviour.
    Cloud Run has already refused every caller but the permitted one."""
    app = _build(AUTH_MODE_IAM_ONLY)
    assert app.verifies_tokens is False


def test_iam_only_requires_no_audience_or_caller():
    """Demanding them would imply a verification that is not happening."""
    app = _build(AUTH_MODE_IAM_ONLY)
    assert app.verifies_tokens is False


def test_iam_plus_token_requires_both_audience_and_caller():
    with pytest.raises(ProjectionConfigError):
        _build(AUTH_MODE_IAM_PLUS_TOKEN)
    with pytest.raises(ProjectionConfigError):
        _build(AUTH_MODE_IAM_PLUS_TOKEN, **{AUDIENCE_ENV_VAR: AUDIENCE})
    with pytest.raises(ProjectionConfigError):
        _build(AUTH_MODE_IAM_PLUS_TOKEN,
               **{CALLER_ENV_VAR: PARENT_STAGING_SA})


def test_iam_plus_token_builds_a_verifier_when_configured():
    app = _build(AUTH_MODE_IAM_PLUS_TOKEN,
                 **{AUDIENCE_ENV_VAR: AUDIENCE,
                    CALLER_ENV_VAR: PARENT_STAGING_SA})
    assert app.verifies_tokens is True


def test_an_iam_only_app_accepts_a_request_with_no_authorization_header():
    """Not a hole: Cloud Run rejected every unauthorised caller before this
    process saw the request. The app's job here is the projection, not the
    caller. Proven by reaching the VALIDATOR — a 400 on an empty body — rather
    than a 401 from an auth layer that is deliberately absent."""
    import io
    import json as _json

    app = _build(AUTH_MODE_IAM_ONLY)
    raw = _json.dumps({"source_session_id": "s",
                       "source_record_digest": "a" * 64,
                       "projection": {}}).encode()
    captured = {}
    chunks = app(
        {"REQUEST_METHOD": "POST",
         "PATH_INFO": "/internal/parent-baseline-projections",
         "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)},
        lambda s, h: captured.setdefault("s", int(s.split(" ")[0])))
    assert captured["s"] == 400, _json.loads(b"".join(chunks))


def test_the_server_documents_why_app_verification_is_optional():
    """The reasoning must travel with the code: a future reader deciding
    whether to switch modes needs to know what is unproven and what proves
    it."""
    source = (REPO_ROOT / "pilot_runtime" / "projection_server.py").read_text()
    assert "X-Serverless-Authorization" in source
    assert "authoritative" in source.lower()
    assert "probe_projection_auth.sh" in source
    assert "NO DEFAULT" in source


def test_no_alternate_authentication_mechanism_exists():
    """No shared secret, no static key, no custom token — in either mode."""
    for relative in ("pilot_runtime/projection_server.py",
                     "pilot_runtime/google_oidc.py",
                     "pilot_backend/transport/projection_wsgi.py"):
        source = (REPO_ROOT / relative).read_text()
        tree = ast.parse(source)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Attribute)}
        for banned in ("hmac", "shared_secret", "api_key", "API_KEY",
                       "from_service_account_file", "from_service_account_json",
                       "service_account_key", "SECRET"):
            assert banned not in names, (relative, banned)


def test_the_probe_script_is_executable_shell_and_runs_nothing_here():
    probe = REPO_ROOT / "pilot_runtime" / "deploy" / "probe_projection_auth.sh"
    text = probe.read_text()
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    # It must not be EXECUTED by CI: it needs a deployed service and IAM that
    # this slice does not apply. CI may READ it — one step asserts the probe
    # mutates nothing — so the assertion is about invocation, not mention.
    workflow = (REPO_ROOT / ".github" / "workflows"
                / "parent-2.4-ci.yml").read_text()
    for invocation in ("bash probe_projection_auth",
                       "./probe_projection_auth",
                       "sh probe_projection_auth",
                       "bash pilot_runtime/deploy/probe_projection_auth",
                       "./pilot_runtime/deploy/probe_projection_auth",
                       "run: probe_projection_auth"):
        assert invocation not in workflow, invocation
    # And it is never made the subject of a `run:` line.
    for line in workflow.splitlines():
        stripped = line.strip()
        if stripped.startswith("run:") and "probe_projection_auth" in stripped:
            raise AssertionError(f"CI runs the probe: {stripped}")
