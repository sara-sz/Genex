"""The composition root: production cannot be assembled out of test parts.

Every prohibition here is evaluated before anything is constructed. That
ordering was wrong once — the injected-decoder prohibition originally lived
inside `build_token_decoder`, which runs after `build_store`, so on a machine
without ambient credentials store construction failed first and the security
check was never reached. A prohibition that only fires when the infrastructure
happens to succeed is not a prohibition, so these tests assert the refusal
WITHOUT any credentials present.
"""

from __future__ import annotations

import pytest

from pilot_backend.auth.verifiers import DevAuthVerifier, FailClosedAuthVerifier
from pilot_backend.config import ConfigError, Environment, PilotSettings
from pilot_backend.persistence import FakeDocumentStore
from pilot_backend.persistence.document_store import DocumentStoreError
from pilot_runtime.composition import CompositionError, build_runtime, build_store
from pilot_runtime.persistence.firestore_store import EMULATOR_ENV_VAR

from .test_sentinels import ALL_SENTINELS

PROD_ENV = {
    "PILOT_ENVIRONMENT": "prod",
    "PILOT_GCP_PROJECT_ID": "genex-pilot-prod",
    "PILOT_FIREBASE_PROJECT_ID": "genex-pilot-prod",
    "PILOT_FIRESTORE_DATABASE": "clinical",
    "PILOT_ALLOWED_ORIGINS": "https://app.genex.health",
}

DEV_ENV = {
    "PILOT_ENVIRONMENT": "test",
    "PILOT_DEV_AUTH_ENABLED": "true",
    "PILOT_ALLOWED_ORIGINS": "http://localhost:5173",
}


def dev_runtime(**kwargs):
    return build_runtime(DEV_ENV, process_env={}, in_memory=True, **kwargs)


# ===========================================================================
# dev/test assembles
# ===========================================================================

def test_dev_runtime_assembles_with_fakes_when_asked():
    runtime = dev_runtime()
    assert isinstance(runtime.store, FakeDocumentStore)
    assert isinstance(runtime.verifier, DevAuthVerifier)
    assert runtime.child_context is not None
    assert runtime.application is not None
    assert not runtime.is_production


def test_dev_without_firebase_or_dev_auth_is_fail_closed():
    runtime = build_runtime({"PILOT_ENVIRONMENT": "test"}, process_env={}, in_memory=True)
    assert isinstance(runtime.verifier, FailClosedAuthVerifier)


def test_every_port_is_wired_to_one_store():
    runtime = dev_runtime()
    assert runtime.repos.store is runtime.store
    assert runtime.repos.audit_events._store is runtime.store
    assert runtime.repos.revisions._store is runtime.store
    assert runtime.repos.child_contexts._store is runtime.store


# ===========================================================================
# production prohibitions — asserted with no credentials present
# ===========================================================================

def test_prod_refuses_in_memory_persistence():
    with pytest.raises(CompositionError) as exc:
        build_runtime(PROD_ENV, process_env={}, in_memory=True)
    assert "in-memory" in str(exc.value)


def test_prod_refuses_an_injected_token_decoder():
    with pytest.raises(CompositionError) as exc:
        build_runtime(PROD_ENV, process_env={},
                      decoder=lambda token, *, check_revoked: {"uid": "x"})
    assert "injected token decoder" in str(exc.value)


def test_prod_refuses_a_configured_emulator():
    env = dict(PROD_ENV, PILOT_FIRESTORE_EMULATOR_HOST="127.0.0.1:8080")
    # Refused at settings construction, before composition even begins.
    with pytest.raises(ConfigError):
        build_runtime(env, process_env={})


def test_prod_refuses_an_ambient_emulator_variable():
    """The client library reads this on its own; configuration cannot catch it."""
    with pytest.raises(CompositionError) as exc:
        build_runtime(PROD_ENV, process_env={EMULATOR_ENV_VAR: "127.0.0.1:8080"})
    assert EMULATOR_ENV_VAR in str(exc.value)


def test_prod_refuses_dev_auth_even_from_a_hand_built_settings_object():
    class Unsafe:
        environment = Environment.PROD
        dev_auth_enabled = True
        firestore_emulator_host = ""
        gcp_project_id = "genex-pilot-prod"
        firebase_project_id = "genex-pilot-prod"
        firestore_database = "clinical"

    with pytest.raises(CompositionError):
        build_store(Unsafe(), process_env={}, in_memory=True)


def test_prod_requires_explicit_project_ids():
    for missing in ("PILOT_GCP_PROJECT_ID", "PILOT_FIREBASE_PROJECT_ID"):
        env = dict(PROD_ENV)
        env.pop(missing)
        with pytest.raises((CompositionError, ConfigError)):
            build_runtime(env, process_env={})


def test_prod_refuses_a_dev_looking_project():
    env = dict(PROD_ENV, PILOT_GCP_PROJECT_ID="genex-pilot-dev")
    with pytest.raises(ConfigError):
        build_runtime(env, process_env={})


def test_prod_cannot_name_parent_23_storage():
    env = dict(PROD_ENV, PILOT_FIRESTORE_DATABASE="genex-api-dev-sessions-genex-mvp-2026")
    with pytest.raises(ConfigError) as exc:
        build_runtime(env, process_env={})
    assert "Parent 2.3" in str(exc.value)


def test_prod_store_selection_always_returns_firestore():
    """There is no branch in which production gets something else."""
    import inspect

    source = inspect.getsource(build_store)
    prod_branch = source.split("# dev / test")[0]
    assert "FakeDocumentStore" not in prod_branch


# ===========================================================================
# SDK failures are translated, never leaked
# ===========================================================================

def test_missing_credentials_surface_as_a_translated_error():
    """No `DefaultCredentialsError`, no URLs, no environment detail."""
    with pytest.raises(DocumentStoreError) as exc:
        build_runtime(PROD_ENV, process_env={})
    message = str(exc.value)
    for leak in ("DefaultCredentialsError", "https://", "gcloud", "ADC",
                 "/Users/", "metadata"):
        assert leak not in message
    for sentinel in ALL_SENTINELS:
        assert sentinel not in message


def test_composition_errors_are_phi_safe_by_declaration():
    assert getattr(CompositionError, "PHI_SAFE_MESSAGE", False) is True


# ===========================================================================
# the AI gate survives composition
# ===========================================================================

def test_ai_phi_gate_is_still_off_after_composition():
    from pilot_backend.aipolicy import AIPolicy

    runtime = dev_runtime()
    assert AIPolicy.from_settings(runtime.settings).allows_phi_egress is False
    prod_settings = PilotSettings.from_env(PROD_ENV)
    assert AIPolicy.from_settings(prod_settings).allows_phi_egress is False


def test_no_openai_or_ai_sdk_reaches_the_runtime_layer():
    import ast
    from pathlib import Path

    banned = {"openai", "anthropic", "cohere", "litellm", "google.generativeai"}
    root = Path(__file__).resolve().parents[1]
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in banned, (path.name, alias.name)
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                assert node.module.split(".")[0] not in banned, (path.name, node.module)


def test_no_telemetry_sdk_reaches_the_runtime_layer():
    import ast
    from pathlib import Path

    banned = {"sentry_sdk", "sentry", "analytics", "segment", "mixpanel", "amplitude",
              "newrelic", "datadog", "ddtrace", "elasticapm", "crashlytics", "posthog"}
    root = Path(__file__).resolve().parents[1]
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in banned, (path.name, alias.name)


def test_no_gcs_client_exists_in_the_runtime_layer():
    """Parent 2.3 session storage is GCS; nothing here can reach it."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for path in sorted(root.rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        text = path.read_text()
        for marker in ("google.cloud.storage", "from google.cloud import storage",
                       "storage.Client", "gs://", "genex-api-dev-sessions"):
            assert marker not in text, (path.name, marker)
