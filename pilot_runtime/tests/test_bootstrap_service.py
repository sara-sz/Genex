"""The internal bootstrap service entrypoint (0.5F-A3).

Pins the properties that only exist at the composition layer:

  * the mode is DECLARED, never defaulted
  * prod is refused outright
  * the service cannot reach Firebase, CORS or the browser application
  * the A2 projection artifact is byte-identical to its frozen tag
  * this image's deploy files are a FOURTH set, not an edit of A2's

The Firebase-import tests exist because that exact bug shipped in A2's first
draft and was caught by the container build, not by a unit test.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess

import pytest

from pilot_backend.auth.interface import VerifiedToken

from pilot_runtime.bootstrap_server import (
    AUTH_MODE_ENV_VAR,
    AUTH_MODE_IAM_ONLY,
    AUTH_MODE_IAM_PLUS_TOKEN,
    AUTH_MODES,
    BootstrapConfigError,
    build_bootstrap_application,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "pilot_runtime" / "deploy"

BASE_ENV = {
    "PILOT_ENVIRONMENT": "dev",
    "PILOT_GCP_PROJECT_ID": "genex-pilot-staging",
    "PILOT_FIRESTORE_DATABASE": "pilot-staging",
}
AUDIENCE = "https://pilot-bootstrap-staging-abc-uc.a.run.app"
CALLER = "genex-parent-staging-run@genex-mvp-2026.iam.gserviceaccount.com"


class _FakeClient:
    """Stands in for a Firestore client. Never called by construction alone."""

    def collection(self, *a, **k):  # pragma: no cover - never reached
        raise AssertionError("the bootstrap service touched Firestore at build")


# ---------------------------------------------------------------------------
# 1. the mode is declared, never inferred
# ---------------------------------------------------------------------------

def test_there_is_no_default_auth_mode():
    with pytest.raises(BootstrapConfigError):
        build_bootstrap_application(dict(BASE_ENV),
                                    firestore_client=_FakeClient())


@pytest.mark.parametrize("mode", ["", "   ", "iam", "IAM_ONLY", "token_only",
                                  "none", "off", "iam_plus", "anything"])
def test_an_unknown_auth_mode_is_refused(mode):
    env = dict(BASE_ENV, **{AUTH_MODE_ENV_VAR: mode})
    with pytest.raises(BootstrapConfigError):
        build_bootstrap_application(env, firestore_client=_FakeClient())


def test_the_mode_vocabulary_is_exactly_two():
    assert AUTH_MODES == (AUTH_MODE_IAM_ONLY, AUTH_MODE_IAM_PLUS_TOKEN)


def test_iam_only_starts_with_only_non_secret_config():
    env = dict(BASE_ENV, **{AUTH_MODE_ENV_VAR: AUTH_MODE_IAM_ONLY})
    app = build_bootstrap_application(env, firestore_client=_FakeClient())
    assert app.verifies_tokens is False


@pytest.mark.parametrize("extra", [
    {},
    {"PILOT_BOOTSTRAP_AUDIENCE": AUDIENCE},
    {"PILOT_BOOTSTRAP_CALLER": CALLER},
])
def test_iam_plus_token_refuses_when_underconfigured(extra):
    env = dict(BASE_ENV, **{AUTH_MODE_ENV_VAR: AUTH_MODE_IAM_PLUS_TOKEN},
               **extra)
    with pytest.raises(BootstrapConfigError):
        build_bootstrap_application(env, firestore_client=_FakeClient())


def test_iam_plus_token_starts_when_fully_configured():
    env = dict(BASE_ENV, **{AUTH_MODE_ENV_VAR: AUTH_MODE_IAM_PLUS_TOKEN,
                            "PILOT_BOOTSTRAP_AUDIENCE": AUDIENCE,
                            "PILOT_BOOTSTRAP_CALLER": CALLER})
    app = build_bootstrap_application(env, firestore_client=_FakeClient())
    assert app.verifies_tokens is True


def test_iam_only_does_not_require_an_audience_or_caller():
    """Requiring them would imply a verification that is not happening."""
    env = dict(BASE_ENV, **{AUTH_MODE_ENV_VAR: AUTH_MODE_IAM_ONLY})
    assert "PILOT_BOOTSTRAP_AUDIENCE" not in env
    app = build_bootstrap_application(env, firestore_client=_FakeClient())
    assert app.verifies_tokens is False


# ---------------------------------------------------------------------------
# 2. production is refused
# ---------------------------------------------------------------------------

#: A prod environment that SATISFIES `PilotSettings._validate_prod`, so the
#: refusal under test is the bootstrap service's own and not a missing-config
#: error arriving first. Without this the test would pass for the wrong reason.
FULL_PROD_ENV = {
    "PILOT_ENVIRONMENT": "prod",
    "PILOT_GCP_PROJECT_ID": "genex-pilot-prod",
    "PILOT_FIREBASE_PROJECT_ID": "genex-pilot-prod",
    "PILOT_FIRESTORE_DATABASE": "pilot-prod",
    "PILOT_ALLOWED_ORIGINS": "https://example.invalid",
}


def test_production_is_refused_by_the_services_own_guard():
    """Fully valid prod configuration, refused anyway.

    Deliberately NOT a partially-configured prod: that is rejected earlier by
    `PilotSettings`, which would make this test pass without ever reaching the
    bootstrap service's prod check.
    """
    env = dict(FULL_PROD_ENV, **{AUTH_MODE_ENV_VAR: AUTH_MODE_IAM_ONLY})
    with pytest.raises(BootstrapConfigError):
        build_bootstrap_application(env, firestore_client=_FakeClient())


def test_production_is_refused_even_when_fully_configured():
    env = dict(FULL_PROD_ENV,
               **{AUTH_MODE_ENV_VAR: AUTH_MODE_IAM_PLUS_TOKEN,
                  "PILOT_BOOTSTRAP_AUDIENCE": AUDIENCE,
                  "PILOT_BOOTSTRAP_CALLER": CALLER})
    with pytest.raises(BootstrapConfigError):
        build_bootstrap_application(env, firestore_client=_FakeClient())


def test_an_underconfigured_prod_is_also_refused():
    """Belt and braces: refused by settings validation rather than silently."""
    from pilot_backend.config.errors import ConfigError

    env = dict(BASE_ENV, PILOT_ENVIRONMENT="prod",
               **{AUTH_MODE_ENV_VAR: AUTH_MODE_IAM_ONLY})
    with pytest.raises((BootstrapConfigError, ConfigError)):
        build_bootstrap_application(env, firestore_client=_FakeClient())


def test_the_auth_mode_is_checked_before_the_environment():
    """An undeclared mode must fail even on a prod environment, so the mode
    check cannot be bypassed by pointing the service somewhere invalid."""
    env = dict(FULL_PROD_ENV)
    with pytest.raises(BootstrapConfigError):
        build_bootstrap_application(env, firestore_client=_FakeClient())


# ---------------------------------------------------------------------------
# 3. it cannot become the browser application
# ---------------------------------------------------------------------------

def _graph(start: str) -> set:
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
                    raise AssertionError(f"{rel} imports firebase_admin")
                if module.startswith(("pilot_runtime", "pilot_backend")):
                    queue.append(module.replace(".", "/") + ".py")
                    queue.append(module.replace(".", "/") + "/__init__.py")
    return seen


def test_the_bootstrap_graph_names_no_firebase_module():
    reached = _graph("pilot_runtime/bootstrap_server.py")
    assert "pilot_runtime/composition.py" not in reached, (
        "composition imports the Firebase decoder at module level")
    assert "pilot_runtime/auth/__init__.py" not in reached, (
        "the auth package's __init__ imports the Firebase decoder")


def test_the_bootstrap_entrypoint_imports_no_firebase_sdk():
    """The bootstrap image does not install `firebase-admin`, so the service
    must not import it — otherwise the container fails at startup.

    The A2 projection service hit exactly this and was caught by its container
    build. Pinned here so it is caught a step earlier.
    """
    import builtins
    import sys

    for module in [m for m in list(sys.modules)
                   if m.startswith("pilot_runtime.bootstrap_server")]:
        del sys.modules[module]

    real = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.split(".")[0] == "firebase_admin":
            raise ImportError(f"blocked for this test: {name}")
        return real(name, *args, **kwargs)

    builtins.__import__ = blocked
    try:
        from pilot_runtime import bootstrap_server as reimported
        with pytest.raises(reimported.BootstrapConfigError):
            reimported.build_bootstrap_application({})
    finally:
        builtins.__import__ = real


def test_the_bootstrap_entrypoint_never_names_the_browser_application():
    source = (REPO_ROOT / "pilot_runtime" / "bootstrap_server.py").read_text()
    effective = "\n".join(line.split("#", 1)[0]
                          for line in source.split('\"\"\"', 2)[2].splitlines())
    for forbidden in ("CorsMiddleware", "build_runtime",
                      "build_wsgi_application", "WsgiApplication",
                      "initialize_firebase_app", "FirebaseTokenDecoder"):
        assert forbidden not in effective, forbidden


def test_the_browser_server_graph_never_reaches_the_bootstrap_app():
    reached = _graph("pilot_runtime/server.py")
    assert "pilot_runtime/bootstrap_server.py" not in reached
    assert "pilot_backend/transport/bootstrap_wsgi.py" not in reached


def test_the_bootstrap_graph_never_reaches_the_projection_app():
    """Two separate internal services; neither image can serve the other."""
    reached = _graph("pilot_runtime/bootstrap_server.py")
    assert "pilot_runtime/projection_server.py" not in reached


# ---------------------------------------------------------------------------
# 4. the A2 artifact is frozen, and this is a FOURTH set of deploy files
# ---------------------------------------------------------------------------

A2_FROZEN = (
    "pilot_runtime/deploy/Dockerfile.projection",
    "pilot_runtime/deploy/requirements-projection.txt",
    "pilot_runtime/deploy/cloudbuild-projection.yaml",
    "pilot_runtime/deploy/gcloudignore-projection",
    "pilot_runtime/projection_server.py",
)
SERVING_AND_GENERATION = (
    "pilot_runtime/deploy/Dockerfile",
    "pilot_runtime/deploy/requirements.txt",
    "pilot_runtime/deploy/cloudbuild.yaml",
    "pilot_runtime/deploy/gcloudignore",
    "pilot_runtime/deploy/Dockerfile.generation",
    "pilot_runtime/deploy/requirements-generation.txt",
    "pilot_runtime/deploy/cloudbuild-generation.yaml",
    "pilot_runtime/deploy/gcloudignore-generation",
)
A2_TAG = "october-pilot-0.5f-a2-parent-baseline-projection"


@pytest.mark.parametrize("path", A2_FROZEN + SERVING_AND_GENERATION)
def test_the_earlier_build_surfaces_are_byte_identical_to_the_a2_tag(path):
    want = subprocess.run(["git", "rev-parse", f"{A2_TAG}:{path}"],
                          capture_output=True, text=True, cwd=REPO_ROOT)
    if want.returncode != 0:
        pytest.skip("the A2 tag is not present in this checkout")
    got = subprocess.run(["git", "hash-object", path], capture_output=True,
                         text=True, check=True, cwd=REPO_ROOT)
    assert got.stdout.strip() == want.stdout.strip(), (
        f"{path} is not byte-identical to the frozen A2 tag")


def test_the_four_images_publish_four_distinct_names():
    names = {}
    for cfg, key in (("cloudbuild.yaml", "serving"),
                     ("cloudbuild-generation.yaml", "generation"),
                     ("cloudbuild-projection.yaml", "projection"),
                     ("cloudbuild-bootstrap.yaml", "bootstrap")):
        text = (DEPLOY / cfg).read_text()
        line = [l for l in text.splitlines() if "_IMAGE:" in l][0]
        names[key] = line.split("_IMAGE:")[1].strip()
    assert len(set(names.values())) == 4, names
    assert names["bootstrap"].endswith("/pilot-bootstrap")
    assert names["projection"].endswith("/pilot-projection")


def test_the_bootstrap_image_installs_no_firebase_sdk():
    pins = (DEPLOY / "requirements-bootstrap.txt").read_text()
    effective = [l.strip() for l in pins.splitlines()
                 if l.strip() and not l.strip().startswith("#")]
    for banned in ("firebase-admin", "pandas", "openpyxl", "openai",
                   "anthropic"):
        assert not any(p.startswith(banned) for p in effective), banned
    assert any(p.startswith("google-cloud-firestore") for p in effective)
    assert any(p.startswith("google-auth") for p in effective)
    assert any(p.startswith("gunicorn") for p in effective)


def test_the_bootstrap_context_admits_nothing_from_genex_parent():
    ign = (DEPLOY / "gcloudignore-bootstrap").read_text()
    effective = [l.strip() for l in ign.splitlines()
                 if l.strip() and not l.strip().startswith("#")]
    assert "*" in effective
    admitted = [l for l in effective if l.startswith("!")]
    assert set(admitted) == {"!pilot_backend/", "!pilot_backend/**",
                             "!pilot_runtime/", "!pilot_runtime/**"}
    assert not any("genex-parent" in l for l in admitted)


def test_the_bootstrap_dockerfile_runs_the_bootstrap_entrypoint():
    text = (DEPLOY / "Dockerfile.bootstrap").read_text()
    assert "pilot_runtime.bootstrap_server:application" in text
    assert "pilot_runtime.projection_server:application" not in text
    assert "pilot_runtime.server:application" not in text
    # An inherited base-image CMD was a real A2-era defect: python:slim ships
    # `CMD ["python3"]`, so an image with no CMD of its own starts a REPL and
    # exits 0. An explicit CMD is therefore load-bearing.
    assert "\nCMD " in text


def test_no_deploy_file_embeds_iam_or_key_material():
    for name in ("Dockerfile.bootstrap", "requirements-bootstrap.txt",
                 "cloudbuild-bootstrap.yaml", "gcloudignore-bootstrap"):
        text = (DEPLOY / name).read_text()
        effective = "\n".join(line.split("#", 1)[0]
                              for line in text.splitlines())
        for token in ("allUsers", "allAuthenticatedUsers",
                      "add-iam-policy-binding", "set-iam-policy",
                      "roles/owner", "roles/editor"):
            assert token not in effective, f"{name} names {token}"
        assert "PRIVATE KEY" not in text
        assert "private_key" not in text


# ---------------------------------------------------------------------------
# 5. the IAM boundary versus the application boundary
# ---------------------------------------------------------------------------
#
# These are DIFFERENT boundaries and the distinction is load-bearing. Firestore
# server-side IAM is `datastore.entities.*` at the DATABASE level: there is no
# per-collection IAM resource and no collection-scoped condition, so a principal
# with `datastore.entities.get` can read any document whose id it knows.
#
# What confines this service to ONE collection is the application: it reaches
# exactly one repository. Saying IAM does that would be a security claim that is
# not true, which is why both halves are pinned separately below.

from pilot_runtime import bootstrap_iam  # noqa: E402


def test_the_iam_boundary_is_exactly_two_entity_permissions():
    assert set(bootstrap_iam.REQUIRED_PERMISSIONS) == {
        "datastore.entities.create", "datastore.entities.get"}


def test_the_iam_boundary_excludes_update_delete_and_list():
    """`list` is the one permission the A2 projection role needs and this does
    not: the registration service addresses a claim by its document id."""
    for excluded in ("datastore.entities.update", "datastore.entities.delete",
                     "datastore.entities.list"):
        assert excluded not in bootstrap_iam.REQUIRED_PERMISSIONS
        assert excluded in bootstrap_iam.EXCLUDED_PERMISSIONS


def test_broad_datastore_roles_are_rejected_by_name():
    for role in ("roles/datastore.user", "roles/datastore.owner",
                 "roles/editor", "roles/owner"):
        assert role in bootstrap_iam.REJECTED_ROLES
    for command in bootstrap_iam.gcloud_commands():
        for role in ("roles/datastore.user", "roles/datastore.owner",
                     "roles/editor", "roles/owner", "roles/datastore.importExportAdmin"):
            assert role not in command, command


def test_the_proposal_uses_a_custom_role_and_a_dedicated_identity():
    commands = " ".join(bootstrap_iam.gcloud_commands())
    assert f"roles/{bootstrap_iam.CUSTOM_ROLE_ID}" in commands
    assert bootstrap_iam.BOOTSTRAP_SERVICE_ACCOUNT in commands
    # NOT the A2 projection runtime identity.
    assert "pilot-projection-staging-run@" not in commands
    assert "pilot-staging-run@" not in commands


def test_exactly_one_invoker_is_proposed_and_it_is_not_allusers():
    commands = " ".join(bootstrap_iam.gcloud_commands())
    assert bootstrap_iam.PERMITTED_INVOKER in commands
    assert "allUsers" not in commands
    assert "allAuthenticatedUsers" not in commands


def test_the_module_states_that_firestore_iam_is_not_per_collection():
    """The correction itself is pinned, so a future reader cannot re-acquire the
    wrong mental model from this file."""
    doc = bootstrap_iam.__doc__ or ""
    assert "NOT PER-COLLECTION" in doc.upper()
    assert "database" in doc.lower()
    # And the claim is not quietly made elsewhere in the module.
    assert "only on the claims collection" not in doc


def test_the_application_boundary_names_one_repository():
    assert bootstrap_iam.PERMITTED_REPOSITORIES == ("parent_session_claims",)
    for forbidden in ("children", "source_links", "identity_claims",
                      "caregiver_child", "goal_suggestions", "clinical_goals",
                      "focus_plans", "weekly_cycles", "observation_events",
                      "rtm_episodes", "rtm_periods",
                      "parent_baseline_projections"):
        assert forbidden in bootstrap_iam.FORBIDDEN_REPOSITORIES


def test_the_bootstrap_service_reaches_only_the_claim_repository():
    """EMPIRICAL, not structural: the registration service is run against a
    recording store and every operation it attempts is checked.

    This is the assertion that actually confines the service to one collection,
    since IAM cannot."""
    from datetime import datetime, timezone

    from pilot_backend.domain.parent_session_claim import claim_digest
    from pilot_backend.integration.parent_session_claim_service import (
        ClaimRegistrationConflict,
        ParentSessionClaimRegistrationService,
    )
    from pilot_backend.persistence import (
        FakeDocumentStore,
        FirestoreRepositories,
    )

    class _Recording:
        def __init__(self, inner):
            self._inner = inner
            self.calls = []

        def __getattr__(self, name):
            attr = getattr(self._inner, name)
            if callable(attr):
                def wrapped(*a, **k):
                    self.calls.append((name, a[0] if a else None))
                    return attr(*a, **k)
                return wrapped
            return attr

    store = _Recording(FakeDocumentStore())
    repos = FirestoreRepositories(store)
    svc = ParentSessionClaimRegistrationService(
        repos=repos, now=lambda: datetime.now(timezone.utc))

    digest = claim_digest("T" * 43)
    svc.register({"claim_digest": digest, "source_session_id": "sess-1"})
    svc.register({"claim_digest": digest, "source_session_id": "sess-1"})
    try:
        svc.register({"claim_digest": digest, "source_session_id": "other"})
    except ClaimRegistrationConflict:
        pass

    operations = {op for op, _ in store.calls}
    collections = {c for _, c in store.calls if c}

    assert operations == set(bootstrap_iam.APPLICATION_OPERATIONS), operations
    assert collections == {"pilot_parent_session_claims"}, collections
    for forbidden in bootstrap_iam.FORBIDDEN_OPERATIONS:
        assert forbidden not in operations, forbidden


def test_the_registration_service_never_names_another_repository():
    """Structural companion: the AST must not mention a forbidden repository."""
    source = (REPO_ROOT / "pilot_backend" / "integration"
              / "parent_session_claim_service.py").read_text()
    effective = "\n".join(line.split("#", 1)[0]
                          for line in source.split('\"\"\"', 2)[2].splitlines())
    for forbidden in bootstrap_iam.FORBIDDEN_REPOSITORIES:
        assert f"repos.{forbidden}" not in effective, forbidden
        assert f"_repos.{forbidden}" not in effective, forbidden
    assert "_repos.parent_session_claims" in effective


def test_the_bootstrap_transport_never_names_another_repository():
    source = (REPO_ROOT / "pilot_backend" / "transport"
              / "bootstrap_wsgi.py").read_text()
    effective = "\n".join(line.split("#", 1)[0]
                          for line in source.split('\"\"\"', 2)[2].splitlines())
    for forbidden in bootstrap_iam.FORBIDDEN_REPOSITORIES:
        assert f".{forbidden}" not in effective, forbidden


def test_the_bootstrap_entrypoint_wires_only_the_registration_service():
    source = (REPO_ROOT / "pilot_runtime" / "bootstrap_server.py").read_text()
    effective = "\n".join(line.split("#", 1)[0]
                          for line in source.split('\"\"\"', 2)[2].splitlines())
    assert "ParentSessionClaimRegistrationService" in effective
    for forbidden in ("IntegrationIdentityService",
                      "LongitudinalIdentityService",
                      "BaselineProjectionService", "GoalService",
                      "MonthlyPlanService", "WeeklyService", "RtmService"):
        assert forbidden not in effective, forbidden


# ---------------------------------------------------------------------------
# 6. what 0.5F-A3 does NOT solve — pilot-readiness, recorded deliberately
# ---------------------------------------------------------------------------

PILOT_READINESS_GAP = """
0.5F-A3 establishes the secure Parent-session -> canonical-Pilot-child handoff
ONCE THE SAME HUMAN ALREADY HAS AN AUTHENTICATED PILOT CAREGIVER IDENTITY.

It does NOT solve, and must not be read as solving:

  * automatic provisioning of a Pilot caregiver identity from normal Parent
    signup — the caregiver must already have bootstrapped via
    POST /pilot/bootstrap/caregiver
  * eliminating a visible SECOND-LOGIN experience — the same person
    authenticates twice, once per Firebase directory
  * merging or federating the two Firebase user directories — deliberately out
    of scope, and the thing the capability token exists to avoid needing

For fictional staging verification an existing fictional Pilot caregiver
identity is sufficient.

BEFORE THE REAL-FAMILY PILOT, normal Parent onboarding must still provide an
acceptable Pilot-side caregiver authentication/provisioning experience with no
manual engineer intervention. That is a SEPARATE remaining pilot-readiness item,
not part of A3.
"""


def test_the_remaining_pilot_readiness_gap_is_recorded():
    """A3's scope boundary is an explicit, testable statement rather than an
    assumption someone has to remember."""
    text = PILOT_READINESS_GAP.lower()
    assert "already has an authenticated pilot caregiver identity" in text
    assert "second-login" in text
    assert "does not solve" in text
    for unsolved in ("automatic provisioning", "federating",
                     "separate remaining pilot-readiness item"):
        assert unsolved in text


def test_a3_adds_no_caregiver_provisioning_path():
    """The gap is real because nothing here mints a Pilot caregiver."""
    for rel in ("pilot_backend/integration/parent_session_claim_service.py",
                "pilot_backend/transport/bootstrap_wsgi.py",
                "pilot_runtime/bootstrap_server.py",
                "pilot_backend/domain/parent_session_claim.py"):
        source = (REPO_ROOT / rel).read_text()
        effective = "\n".join(line.split("#", 1)[0]
                              for line in source.split('\"\"\"', 2)[2].splitlines())
        for minting in ("bootstrap_caregiver", "Caregiver.create",
                        "with_auth_subject", "AuthSubjectIdentityClaim"):
            assert minting not in effective, f"{rel} names {minting}"


def test_the_consume_path_requires_a_pre_existing_pilot_caregiver():
    """A subject with no Pilot caregiver record cannot resolve to a principal,
    so redemption is impossible until the human has a Pilot identity.

    `resolve_principal` RAISES rather than returning None — which is the
    stronger behaviour, and the reason the second login is unavoidable in A3.
    """
    from pilot_backend.auth.resolver import (
        PrincipalResolutionError,
        resolve_principal,
    )
    from pilot_backend.persistence import (
        FakeDocumentStore,
        FirestoreRepositories,
    )

    repos = FirestoreRepositories(FakeDocumentStore())
    with pytest.raises(PrincipalResolutionError):
        resolve_principal(
            VerifiedToken(subject="a-parent-who-never-logged-into-pilot"),
            repos)
