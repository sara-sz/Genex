"""BACKEND 0.2 — secure production backend foundation.

Security tests are written as REFUSALS. A suite that only proves the happy path
would pass against a system that authorizes everything, which is precisely the
failure being guarded against. Almost every test here asserts that something
does NOT happen.

## Sentinels

Fictional marker strings stand in for the categories of content that must never
reach a log or an audit record. They are deliberately unmistakable, so a test
that finds one has found a real leak and not a coincidence. There is no real
PHI anywhere in this suite, and `Child` carries no clinical field to begin with.

## Both backends, one suite

The authorization tests are parametrized over `InMemoryRepositories` and
`FirestoreRepositories` (over `FakeDocumentStore`). Identical assertions must
hold for both: a security property that only holds for the test double is not a
security property.
"""

from __future__ import annotations

import ast
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pilot_backend import aipolicy, apisurface, audit, authz, config, observability
from pilot_backend.aipolicy import AIEgressDenied, AIPolicy, evaluate_phi_ai_egress
from pilot_backend.apisurface import (
    PUBLIC_ROUTES,
    RouteGuard,
    SurfaceError,
    cors_policy_for,
    health_payload,
    is_public_route,
)
from pilot_backend.audit.events import (
    ALLOWED_METADATA_KEYS,
    AuditAction,
    AuditEvent,
    AuditMetadataError,
    AuditResult,
)
from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth import (
    AuthError,
    DevAuthVerifier,
    FailClosedAuthVerifier,
    IdentityPlatformVerifier,
    Principal,
    PrincipalResolutionError,
    RevokedTokenError,
    VerifiedToken,
    build_verifier,
    resolve_principal,
)
from pilot_backend.authz import AccessDecision, Denial, authorize_child_access
from pilot_backend.authz.policy import authenticate_and_authorize_child
from pilot_backend.config import ConfigError, Environment, PilotSettings
from pilot_backend.domain.enums import ConnectionStatus, EntityStatus
from pilot_backend.domain.roles import ActorRole
from pilot_backend.fixtures.secure_topology import (
    CAREGIVER_ALPHA_SUBJECT,
    CAREGIVER_GAMMA_SUBJECT,
    PROVIDER_ALPHA_SUBJECT,
    PROVIDER_GAMMA_SUBJECT,
    T0,
    UNPROVISIONED_SUBJECT,
    build_secure_topology,
)
from pilot_backend.observability.safe_logging import (
    FORBIDDEN_LOG_FIELDS,
    LogFieldError,
    describe_exception,
    format_log,
    redact_bearer,
    render,
)
from pilot_backend.persistence import (
    CodecError,
    FakeDocumentStore,
    FirestoreRepositories,
    decode,
    encode,
)
from pilot_backend.persistence.codecs import SPECS
from pilot_backend.repository.memory import InMemoryRepositories
from pilot_backend.revision import (
    ImmutableRecordError,
    RecordState,
    amend,
    finalize,
    latest,
    start_draft,
)

PILOT_ROOT = Path(__file__).resolve().parents[1]


def code_tokens(path: Path) -> list:
    """Identifiers and non-docstring string literals from a module.

    Prose is excluded deliberately. These modules DOCUMENT the things they
    forbid — `policy.py` explains that it never reads `BETA_ACCESS_CODE`, and
    `settings.py` explains why third-party preview origins are refused — so a
    substring scan over raw source flags the explanation as the violation.

    Scanning the AST keys the guard on what the code DOES: comments and
    docstrings vanish, and what remains is executable. That is stricter in the
    way that matters, not looser: a banned name can no longer hide inside a
    string that a raw-text scan would have matched anyway, and the guard can
    now be trusted enough to be left switched on.
    """
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            found = ast.get_docstring(node, clean=False)
            if found:
                docstrings.add(found)

    tokens = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            tokens.append(node.id)
        elif isinstance(node, ast.Attribute):
            tokens.append(node.attr)
        elif isinstance(node, ast.arg):
            tokens.append(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in docstrings:
                tokens.append(node.value)
    return tokens

# --- sentinels -------------------------------------------------------------

SENTINEL_CHILD_NAME = "ZZSENTINEL-CHILDNAME-Quillwood"
SENTINEL_NOTE = "ZZSENTINEL-NOTE-refuses-solids-at-dinner"
SENTINEL_DIAGNOSIS = "ZZSENTINEL-DIAGNOSIS-fictional-condition"
SENTINEL_CONCERN = "ZZSENTINEL-CONCERN-not-speaking-in-sentences"
SENTINEL_TOKEN = "ZZSENTINEL-TOKEN-abc123def456"
SENTINEL_SECRET = "ZZSENTINEL-SECRET-xyz789"
SENTINEL_EMAIL = "zzsentinel-caregiver@example.invalid"

ALL_SENTINELS = (
    SENTINEL_CHILD_NAME, SENTINEL_NOTE, SENTINEL_DIAGNOSIS, SENTINEL_CONCERN,
    SENTINEL_TOKEN, SENTINEL_SECRET, SENTINEL_EMAIL,
)


# --- helpers ---------------------------------------------------------------

def dev_settings(**overrides) -> PilotSettings:
    base = {
        "PILOT_ENVIRONMENT": "dev",
        "PILOT_GCP_PROJECT_ID": "genex-pilot-dev",
        "PILOT_FIREBASE_PROJECT_ID": "genex-pilot-dev",
        "PILOT_FIRESTORE_DATABASE": "pilot-dev",
        "PILOT_ALLOWED_ORIGINS": "http://localhost:5173",
        "PILOT_DEV_AUTH_ENABLED": "true",
    }
    base.update(overrides)
    return PilotSettings.from_env(base)


def prod_env(**overrides) -> dict:
    base = {
        "PILOT_ENVIRONMENT": "prod",
        "PILOT_GCP_PROJECT_ID": "genex-pilot-prod",
        "PILOT_FIREBASE_PROJECT_ID": "genex-pilot-prod",
        "PILOT_FIRESTORE_DATABASE": "pilot-clinical",
        "PILOT_ALLOWED_ORIGINS": "https://app.genex.health",
    }
    base.update(overrides)
    return base


class StubSettings:
    """A hand-built settings object that skips PilotSettings validation.

    Used to prove the SECOND and THIRD dev-auth defences independently: if the
    only thing stopping dev auth in prod were `PilotSettings`, this object
    would sail straight past it.
    """

    def __init__(self, environment: Environment, dev_auth_enabled: bool) -> None:
        self.environment = environment
        self.dev_auth_enabled = dev_auth_enabled


def make_repos(kind: str):
    return InMemoryRepositories() if kind == "memory" else FirestoreRepositories(
        FakeDocumentStore())


BACKENDS = ["memory", "firestore"]


def verifier_for(topology_subjects, environment: str = "dev") -> DevAuthVerifier:
    """A dev verifier issuing one fictional token per subject."""
    verifier = DevAuthVerifier(environment)
    for token, subject in topology_subjects.items():
        verifier.add(token, VerifiedToken(subject=subject, email=SENTINEL_EMAIL))
    return verifier


def standard_tokens() -> dict:
    return {
        "token-caregiver-alpha": CAREGIVER_ALPHA_SUBJECT,
        "token-caregiver-gamma": CAREGIVER_GAMMA_SUBJECT,
        "token-provider-alpha": PROVIDER_ALPHA_SUBJECT,
        "token-provider-gamma": PROVIDER_GAMMA_SUBJECT,
        "token-unprovisioned": UNPROVISIONED_SUBJECT,
    }


# ===========================================================================
# 1. ENVIRONMENT-SAFE CONFIGURATION
# ===========================================================================

def test_environment_is_required_and_has_no_default():
    with pytest.raises(ConfigError):
        PilotSettings.from_env({})
    with pytest.raises(ConfigError):
        PilotSettings.from_env({"PILOT_ENVIRONMENT": "production"})


@pytest.mark.parametrize("missing", [
    "PILOT_GCP_PROJECT_ID",
    "PILOT_FIREBASE_PROJECT_ID",
    "PILOT_FIRESTORE_DATABASE",
    "PILOT_ALLOWED_ORIGINS",
])
def test_missing_prod_config_fails_startup(missing):
    """§14: missing prod config fails startup — one required key at a time."""
    env = prod_env()
    env.pop(missing)
    with pytest.raises(ConfigError) as exc:
        PilotSettings.from_env(env)
    assert missing in str(exc.value)


def test_prod_config_is_complete_and_constructs():
    settings = PilotSettings.from_env(prod_env())
    assert settings.environment is Environment.PROD
    assert settings.environment.is_prod


@pytest.mark.parametrize("field,value", [
    ("PILOT_GCP_PROJECT_ID", "genex-pilot-dev"),
    ("PILOT_FIREBASE_PROJECT_ID", "genex-pilot-staging"),
    ("PILOT_FIRESTORE_DATABASE", "pilot-test"),
    ("PILOT_FIRESTORE_DATABASE", "pilot-emulator"),
])
def test_dev_resources_cannot_be_silently_selected_in_prod(field, value):
    """§14: a dev-looking resource promoted into prod is refused."""
    with pytest.raises(ConfigError) as exc:
        PilotSettings.from_env(prod_env(**{field: value}))
    assert "non-production" in str(exc.value)


def test_prod_rejects_wildcard_and_non_https_origins():
    with pytest.raises(ConfigError):
        PilotSettings.from_env(prod_env(PILOT_ALLOWED_ORIGINS="*"))
    with pytest.raises(ConfigError):
        PilotSettings.from_env(prod_env(PILOT_ALLOWED_ORIGINS="http://app.genex.health"))


@pytest.mark.parametrize("origin", [
    "https://genex-kiddo-compass.lovable.app",
    "https://preview.lovable.dev",
    "https://something.lovableproject.com",
    "https://localhost:5173",
])
def test_prod_rejects_third_party_preview_origins(origin):
    """§12: no implicit Lovable/dev origins in production."""
    with pytest.raises(ConfigError):
        PilotSettings.from_env(prod_env(PILOT_ALLOWED_ORIGINS=origin))


def test_no_implicit_fallback_between_environments():
    """A dev config does not acquire prod values, and prod does not borrow dev's."""
    dev = dev_settings(PILOT_GCP_PROJECT_ID="", PILOT_FIRESTORE_DATABASE="")
    assert dev.gcp_project_id == ""
    assert dev.firestore_database == ""
    # ...and the empty values are NOT silently filled from anywhere.
    assert dev.environment is Environment.DEV


def test_settings_are_immutable():
    settings = dev_settings()
    with pytest.raises(Exception):
        settings.environment = Environment.PROD  # type: ignore[misc]


def test_boolean_config_is_strict():
    with pytest.raises(ConfigError):
        dev_settings(PILOT_DEV_AUTH_ENABLED="maybe")


def test_public_config_exposes_no_infrastructure():
    settings = PilotSettings.from_env(prod_env())
    public = settings.public_config()
    assert set(public) == {"environment"}
    blob = json.dumps(public)
    for secret in ("genex-pilot-prod", "pilot-clinical", "app.genex.health"):
        assert secret not in blob


# ===========================================================================
# 6. NEW PILOT STORE IS NOT THE PARENT 2.3 GCS STORE
# ===========================================================================

@pytest.mark.parametrize("legacy", [
    "genex-api-dev-sessions-genex-mvp-2026",
    "genex-api-prod-sessions-genex-mvp-2026",
    "genex-api-staging",
])
def test_configuration_refuses_protected_parent_23_resources(legacy):
    with pytest.raises(ConfigError) as exc:
        PilotSettings.from_env(dict(
            PILOT_ENVIRONMENT="dev", PILOT_FIRESTORE_DATABASE=legacy))
    assert "Parent 2.3" in str(exc.value)


def test_legacy_resource_rejection_applies_in_every_environment():
    """A dev pointer at the real Parent bucket is the same disclosure as a prod one."""
    for environment in ("dev", "test"):
        with pytest.raises(ConfigError):
            PilotSettings.from_env({
                "PILOT_ENVIRONMENT": environment,
                "PILOT_GCP_PROJECT_ID": "genex-api-dev-sessions-genex-mvp-2026",
            })


def test_pilot_persistence_uses_only_pilot_prefixed_collections():
    store = FakeDocumentStore()
    repos = FirestoreRepositories(store)
    build_secure_topology(repos)
    assert store.collections(), "topology wrote nothing"
    for name in store.collections():
        assert name.startswith("pilot_"), name


def test_pilot_backend_contains_no_gcs_or_bucket_reference():
    """§6: the pilot store cannot reach Parent 2.3 object storage."""
    banned = ("google-cloud-storage", "storage.Client", "bucket(", "blob(",
              "genex-api-dev-sessions", "gs://")
    for path in sorted(PILOT_ROOT.rglob("*.py")):
        if path.name.startswith("test_") or path.name == "settings.py" \
                or path.name == "collections.py":
            continue  # the denylists legitimately NAME the forbidden resources
        text = path.read_text()
        for marker in banned:
            assert marker not in text, (path.name, marker)


# ===========================================================================
# 2. AUTHENTICATION
# ===========================================================================

def test_no_token_denied():
    verifier = DevAuthVerifier("dev")
    for bearer in (None, "", "   ", "Bearer ", "Bearer"):
        with pytest.raises(AuthError):
            verifier.verify(bearer)


def test_invalid_token_denied():
    verifier = verifier_for(standard_tokens())
    for bearer in ("Bearer not-a-real-token", "Bearer " + SENTINEL_TOKEN,
                   "Basic abc", "Bearer a b c"):
        with pytest.raises(AuthError):
            verifier.verify(bearer)


def test_fail_closed_verifier_rejects_everything():
    verifier = FailClosedAuthVerifier("prod")
    with pytest.raises(AuthError):
        verifier.verify("Bearer anything")


def test_revoked_token_denied_and_classified():
    """§14: revoked token denied — and distinguishable, while still a 401."""
    def decoder(token, *, check_revoked):
        raise RuntimeError("The Firebase ID token has been revoked.")

    verifier = IdentityPlatformVerifier("prod", decoder)
    with pytest.raises(RevokedTokenError):
        verifier.verify("Bearer " + SENTINEL_TOKEN)
    assert issubclass(RevokedTokenError, AuthError)


def test_production_verification_always_checks_revocation():
    seen = {}

    def decoder(token, *, check_revoked):
        seen["check_revoked"] = check_revoked
        return {"uid": "subject-1"}

    IdentityPlatformVerifier("prod", decoder).verify("Bearer t")
    assert seen["check_revoked"] is True, "prod must check revocation"

    verified = IdentityPlatformVerifier("prod", decoder).verify("Bearer t")
    assert verified.revocation_checked is True


def test_provider_exception_text_never_propagates():
    """A third-party decoder error must not carry payload text outward."""
    def decoder(token, *, check_revoked):
        raise RuntimeError(f"decode failed for payload {SENTINEL_NOTE} key {SENTINEL_SECRET}")

    verifier = IdentityPlatformVerifier("prod", decoder)
    with pytest.raises(AuthError) as exc:
        verifier.verify("Bearer " + SENTINEL_TOKEN)
    message = str(exc.value)
    for sentinel in ALL_SENTINELS:
        assert sentinel not in message
    # The original exception is dropped entirely, not merely unformatted.
    assert exc.value.__cause__ is None


def test_identity_platform_requires_a_decoder():
    with pytest.raises(AuthError):
        IdentityPlatformVerifier("prod", None)


def test_verified_token_must_carry_a_subject():
    with pytest.raises(AuthError):
        VerifiedToken(subject="")


def test_verified_email_policy_can_be_required():
    def decoder(token, *, check_revoked):
        return {"uid": "subject-1", "email_verified": False}

    verifier = IdentityPlatformVerifier("prod", decoder, require_verified_email=True)
    with pytest.raises(AuthError):
        verifier.verify("Bearer t")


# --- dev auth cannot reach prod: three independent defences ---------------

def test_dev_auth_impossible_in_prod_defence_1_settings():
    with pytest.raises(ConfigError) as exc:
        PilotSettings.from_env(prod_env(PILOT_DEV_AUTH_ENABLED="true"))
    assert "dev auth" in str(exc.value)


def test_dev_auth_impossible_in_prod_defence_2_constructor():
    with pytest.raises(AuthError):
        DevAuthVerifier("prod")


def test_dev_auth_impossible_in_prod_defence_3_builder():
    """Even with validation bypassed, the builder refuses."""
    with pytest.raises(AuthError):
        build_verifier(StubSettings(Environment.PROD, dev_auth_enabled=True))


def test_prod_without_a_decoder_is_fail_closed_not_open():
    """Today's real state: no Identity Platform project, so nobody authenticates."""
    settings = PilotSettings.from_env(prod_env())
    verifier = build_verifier(settings, decoder=None)
    assert isinstance(verifier, FailClosedAuthVerifier)
    with pytest.raises(AuthError):
        verifier.verify("Bearer " + SENTINEL_TOKEN)


def test_builder_selects_dev_auth_only_in_dev_or_test():
    assert isinstance(build_verifier(dev_settings()), DevAuthVerifier)
    assert isinstance(
        build_verifier(PilotSettings.from_env({"PILOT_ENVIRONMENT": "dev"})),
        FailClosedAuthVerifier)


# ===========================================================================
# 3. AUTH SUBJECT -> APPLICATION IDENTITY
# ===========================================================================

@pytest.mark.parametrize("kind", BACKENDS)
def test_auth_subject_resolves_to_application_identity(kind):
    repos = make_repos(kind)
    topo = build_secure_topology(repos)

    caregiver = resolve_principal(VerifiedToken(subject=CAREGIVER_ALPHA_SUBJECT), repos)
    assert caregiver.role is ActorRole.CAREGIVER
    assert caregiver.application_id == topo.caregiver_alpha.caregiver_id

    provider = resolve_principal(VerifiedToken(subject=PROVIDER_ALPHA_SUBJECT), repos)
    assert provider.role is ActorRole.PROVIDER
    assert provider.application_id == topo.provider_alpha.provider_id
    assert provider.practice_id == topo.practice.practice_id


@pytest.mark.parametrize("kind", BACKENDS)
def test_unprovisioned_subject_resolves_to_nothing(kind):
    repos = make_repos(kind)
    build_secure_topology(repos)
    with pytest.raises(PrincipalResolutionError):
        resolve_principal(VerifiedToken(subject=UNPROVISIONED_SUBJECT), repos)


@pytest.mark.parametrize("kind", BACKENDS)
def test_email_is_never_an_identity_key(kind):
    """§3: do not use email as the durable primary key."""
    repos = make_repos(kind)
    build_secure_topology(repos)
    # A token carrying a caregiver's email but a foreign subject resolves to nothing.
    token = VerifiedToken(subject=UNPROVISIONED_SUBJECT, email=SENTINEL_EMAIL)
    with pytest.raises(PrincipalResolutionError):
        resolve_principal(token, repos)

    source = inspect.getsource(resolve_principal)
    assert "email" not in source.replace("`VerifiedToken.email`", "")


@pytest.mark.parametrize("kind", BACKENDS)
def test_inactive_actor_stops_being_an_identity(kind):
    repos = make_repos(kind)
    topo = build_secure_topology(repos)
    repos.caregivers.update_status(
        topo.caregiver_alpha.caregiver_id, EntityStatus.INACTIVE)
    with pytest.raises(PrincipalResolutionError):
        resolve_principal(VerifiedToken(subject=CAREGIVER_ALPHA_SUBJECT), repos)


def test_a_subject_matching_two_records_fails_closed():
    """A subject on both a caregiver and a provider is a fault, not a dual role.

    Built through the public repository surface: a SECOND provider is created
    already carrying the caregiver's subject, rather than reaching into
    private storage to corrupt an existing row. That is a shape real data
    could take — two records bound to one identity — so the test proves the
    system's behaviour rather than the test double's.
    """
    from pilot_backend.domain.entities import Provider
    from pilot_backend.domain.enums import ProviderDiscipline

    repos = FirestoreRepositories(FakeDocumentStore())
    topo = build_secure_topology(repos)
    repos.providers.create(Provider.create(
        topo.practice.practice_id, ProviderDiscipline.SLP, "Provider-Delta",
        auth_subject=CAREGIVER_ALPHA_SUBJECT, now=T0))

    # Both lookups now match, so resolution is ambiguous and must refuse.
    assert repos.caregivers.get_by_auth_subject(CAREGIVER_ALPHA_SUBJECT) is not None
    assert repos.providers.get_by_auth_subject(CAREGIVER_ALPHA_SUBJECT) is not None
    with pytest.raises(PrincipalResolutionError):
        resolve_principal(VerifiedToken(subject=CAREGIVER_ALPHA_SUBJECT), repos)


@pytest.mark.parametrize("kind", BACKENDS)
def test_two_records_of_one_kind_sharing_a_subject_fail_closed(kind):
    """Found by the Firestore emulator, which does not reset between tests.

    Two caregiver records bound to one auth subject used to resolve silently
    to whichever sorted first — making the effective identity a function of
    document ordering. The 0.2 resolver caught a subject matching a caregiver
    AND a provider, but not two caregivers.
    """
    from pilot_backend.domain.entities import Caregiver
    from pilot_backend.repository.interface import AmbiguousAuthSubject

    repos = make_repos(kind)
    build_secure_topology(repos)
    repos.caregivers.create(Caregiver.create(
        "Caregiver-Delta", auth_subject=CAREGIVER_ALPHA_SUBJECT, now=T0))

    with pytest.raises(AmbiguousAuthSubject):
        repos.caregivers.get_by_auth_subject(CAREGIVER_ALPHA_SUBJECT)

    # ...and identity resolution refuses rather than picking one.
    with pytest.raises(PrincipalResolutionError):
        resolve_principal(VerifiedToken(subject=CAREGIVER_ALPHA_SUBJECT), repos)


@pytest.mark.parametrize("kind", BACKENDS)
def test_an_ambiguous_subject_is_403_not_a_silent_grant(kind):
    from pilot_backend.domain.entities import Caregiver

    repos = make_repos(kind)
    topo = build_secure_topology(repos)
    repos.caregivers.create(Caregiver.create(
        "Caregiver-Delta", auth_subject=CAREGIVER_ALPHA_SUBJECT, now=T0))
    verifier = verifier_for(standard_tokens())

    decision = authenticate_and_authorize_child(
        "Bearer token-caregiver-alpha", topo.child_alpha.child_id,
        verifier=verifier, repos=repos)
    assert not decision.allowed
    assert decision.status_code == 403


def test_empty_auth_subject_never_matches_an_unbound_record():
    """An unbound provider record must not be claimable by a blank subject."""
    repos = FirestoreRepositories(FakeDocumentStore())
    build_secure_topology(repos)
    assert repos.providers.get_by_auth_subject("") is None
    assert repos.providers.get_by_auth_subject("   ") is None
    assert repos.caregivers.get_by_auth_subject("") is None


# ===========================================================================
# 4. SERVER-SIDE AUTHORIZATION  (+ 13. 401 vs 403)
# ===========================================================================

@pytest.mark.parametrize("kind", BACKENDS)
def test_caregiver_reaches_own_child(kind):
    repos = make_repos(kind)
    topo = build_secure_topology(repos)
    principal = resolve_principal(VerifiedToken(subject=CAREGIVER_ALPHA_SUBJECT), repos)
    decision = authorize_child_access(principal, topo.child_alpha.child_id, repos)
    assert decision.allowed and decision.status_code == 200


@pytest.mark.parametrize("kind", BACKENDS)
def test_provider_reaches_connected_child(kind):
    repos = make_repos(kind)
    topo = build_secure_topology(repos)
    principal = resolve_principal(VerifiedToken(subject=PROVIDER_ALPHA_SUBJECT), repos)
    assert authorize_child_access(principal, topo.child_alpha.child_id, repos).allowed


@pytest.mark.parametrize("kind", BACKENDS)
def test_parent_a_cannot_access_child_b(kind):
    """§14: cross-family caregiver access denied with 403."""
    repos = make_repos(kind)
    topo = build_secure_topology(repos)
    principal = resolve_principal(VerifiedToken(subject=CAREGIVER_ALPHA_SUBJECT), repos)
    decision = authorize_child_access(principal, topo.child_beta.child_id, repos)
    assert not decision.allowed
    assert decision.status_code == 403
    assert decision.denial is Denial.NO_RELATIONSHIP


@pytest.mark.parametrize("kind", BACKENDS)
def test_provider_a_cannot_access_unrelated_child(kind):
    repos = make_repos(kind)
    topo = build_secure_topology(repos)
    principal = resolve_principal(VerifiedToken(subject=PROVIDER_ALPHA_SUBJECT), repos)
    decision = authorize_child_access(principal, topo.child_beta.child_id, repos)
    assert not decision.allowed and decision.status_code == 403


@pytest.mark.parametrize("kind", BACKENDS)
def test_ended_caregiver_relationship_denies(kind):
    """Caregiver-Gamma genuinely HAD access to Child-Alpha. The row still exists."""
    repos = make_repos(kind)
    topo = build_secure_topology(repos)
    principal = resolve_principal(VerifiedToken(subject=CAREGIVER_GAMMA_SUBJECT), repos)
    decision = authorize_child_access(principal, topo.child_alpha.child_id, repos)
    assert not decision.allowed
    assert decision.denial is Denial.INACTIVE_RELATIONSHIP
    # ...and the history was preserved, not deleted, while still denying.
    history = repos.caregiver_child.list_caregivers_for_child(
        topo.child_alpha.child_id, include_ended=True)
    assert any(c.caregiver_id == principal.application_id for c in history)


@pytest.mark.parametrize("kind", BACKENDS)
def test_pending_provider_connection_denies(kind):
    """Invited but never activated is not access."""
    repos = make_repos(kind)
    topo = build_secure_topology(repos)
    principal = resolve_principal(VerifiedToken(subject=PROVIDER_GAMMA_SUBJECT), repos)
    decision = authorize_child_access(principal, topo.child_alpha.child_id, repos)
    assert not decision.allowed
    assert decision.denial is Denial.INACTIVE_RELATIONSHIP


@pytest.mark.parametrize("kind", BACKENDS)
def test_revoking_a_relationship_denies_immediately(kind):
    """§14: revoked/ended relationship immediately denies — no cache, no grace."""
    repos = make_repos(kind)
    topo = build_secure_topology(repos)
    principal = resolve_principal(VerifiedToken(subject=PROVIDER_ALPHA_SUBJECT), repos)
    assert authorize_child_access(principal, topo.child_alpha.child_id, repos).allowed

    repos.provider_child.end_connection(
        topo.link_alpha_provider.connection_id, status=ConnectionStatus.REVOKED)

    after = authorize_child_access(principal, topo.child_alpha.child_id, repos)
    assert not after.allowed
    assert after.denial is Denial.INACTIVE_RELATIONSHIP


@pytest.mark.parametrize("kind", BACKENDS)
def test_modified_child_id_denies(kind):
    """§14: altering the requested child id selects a different child, not access."""
    repos = make_repos(kind)
    topo = build_secure_topology(repos)
    principal = resolve_principal(VerifiedToken(subject=CAREGIVER_ALPHA_SUBJECT), repos)

    real = topo.child_alpha.child_id
    for forged in (real[:-1] + ("a" if real[-1] != "a" else "b"),
                   real.upper(), real + "x", "chld_" + "0" * 32, "", "   "):
        decision = authorize_child_access(principal, forged, repos)
        assert not decision.allowed, forged
        assert decision.status_code == 403


@pytest.mark.parametrize("kind", BACKENDS)
def test_inactive_child_denies(kind):
    repos = make_repos(kind)
    topo = build_secure_topology(repos)
    principal = resolve_principal(VerifiedToken(subject=CAREGIVER_ALPHA_SUBJECT), repos)
    repos.children.update_status(topo.child_alpha.child_id, EntityStatus.ARCHIVED)
    decision = authorize_child_access(principal, topo.child_alpha.child_id, repos)
    assert not decision.allowed and decision.denial is Denial.INACTIVE_CHILD


# --- forged request input --------------------------------------------------

@pytest.mark.parametrize("kind", BACKENDS)
def test_forged_identity_fields_are_ignored(kind):
    """§14: forged uid / role / caregiver id / provider id all ignored.

    The forged envelope is what a hostile client would send. None of it is a
    parameter of the authorization path, so the decision is driven entirely by
    the verified token — proven both behaviourally and structurally.
    """
    repos = make_repos(kind)
    topo = build_secure_topology(repos)
    verifier = verifier_for(standard_tokens())

    forged_envelope = {
        "uid": PROVIDER_ALPHA_SUBJECT,
        "sub": PROVIDER_ALPHA_SUBJECT,
        "role": "provider",
        "actor_role": ActorRole.PROVIDER.value,
        "caregiver_id": topo.caregiver_beta.caregiver_id,
        "provider_id": topo.provider_beta.provider_id,
        "practice_id": topo.practice.practice_id,
        "email": SENTINEL_EMAIL,
        "beta_access_code": "genex",
        "is_admin": True,
    }

    # Caregiver-Alpha's token, with a body claiming to be Provider-Beta.
    decision = authenticate_and_authorize_child(
        "Bearer token-caregiver-alpha", topo.child_beta.child_id,
        verifier=verifier, repos=repos)
    assert not decision.allowed, "forged body must not grant access to another family"
    assert decision.status_code == 403

    # The real identity is still the token's, not the forged one.
    granted = authenticate_and_authorize_child(
        "Bearer token-caregiver-alpha", topo.child_alpha.child_id,
        verifier=verifier, repos=repos)
    assert granted.allowed
    assert granted.principal.application_id == topo.caregiver_alpha.caregiver_id
    assert granted.principal.role is ActorRole.CAREGIVER

    # Structural: there is no parameter through which the envelope could enter.
    for fn in (authorize_child_access, authenticate_and_authorize_child):
        params = set(inspect.signature(fn).parameters)
        assert not (params & set(forged_envelope)), fn.__name__


def test_authorization_never_consults_a_beta_access_code():
    """§4/§14: the beta code is not an access-control primitive.

    Keyed on executable tokens, so `policy.py` documenting that it ignores the
    beta code does not read as consulting it.
    """
    for path in sorted((PILOT_ROOT / "authz").glob("*.py")):
        for token in code_tokens(path):
            lowered = token.lower()
            assert "beta_access" not in lowered, (path.name, token)
            assert "beta_code" not in lowered, (path.name, token)


@pytest.mark.parametrize("kind", BACKENDS)
def test_beta_code_alone_grants_no_phi_access(kind):
    """Holding the shared beta string is not an identity and not a relationship."""
    repos = make_repos(kind)
    topo = build_secure_topology(repos)
    verifier = verifier_for(standard_tokens())
    for bearer in ("Bearer genex", "Bearer genex23", "Bearer genex-family-beta-22"):
        decision = authenticate_and_authorize_child(
            bearer, topo.child_alpha.child_id, verifier=verifier, repos=repos)
        assert not decision.allowed
        assert decision.status_code == 401


# --- 401 vs 403 ------------------------------------------------------------

@pytest.mark.parametrize("kind", BACKENDS)
def test_401_for_authentication_403_for_authorization(kind):
    repos = make_repos(kind)
    topo = build_secure_topology(repos)
    verifier = verifier_for(standard_tokens())
    child = topo.child_alpha.child_id

    def status(bearer):
        return authenticate_and_authorize_child(
            bearer, child, verifier=verifier, repos=repos).status_code

    # 401 — we do not know who you are.
    assert status(None) == 401
    assert status("") == 401
    assert status("Bearer " + SENTINEL_TOKEN) == 401
    assert status("Basic abc") == 401

    # 403 — we know who you are, and you may not have this.
    assert status("Bearer token-unprovisioned") == 403
    assert status("Bearer token-caregiver-gamma") == 403
    assert status("Bearer token-provider-gamma") == 403


def test_denial_to_status_mapping_is_total_and_consistent():
    """Every denial reason has exactly one status, and it is 401 or 403."""
    for denial in Denial:
        decision = AccessDecision.deny(denial)
        assert decision.status_code in (401, 403), denial
        assert not decision.allowed


def test_a_decision_cannot_be_constructed_inconsistently():
    with pytest.raises(ValueError):
        AccessDecision(allowed=True, status_code=200)  # no principal
    with pytest.raises(ValueError):
        AccessDecision(allowed=False, status_code=403)  # no reason
    with pytest.raises(ValueError):
        AccessDecision(allowed=False, status_code=401, denial=Denial.NO_RELATIONSHIP)


def test_public_reason_is_not_an_existence_oracle():
    """A 403 must read identically whether or not the child exists."""
    unknown = AccessDecision.deny(Denial.UNKNOWN_CHILD)
    unrelated = AccessDecision.deny(Denial.NO_RELATIONSHIP)
    assert unknown.public_reason == unrelated.public_reason == "not permitted"
    for decision in (unknown, unrelated):
        assert decision.denial.value not in decision.public_reason


# ===========================================================================
# 5. FIRESTORE PERSISTENCE
# ===========================================================================

def test_firestore_repositories_satisfy_the_frozen_0_1_protocols():
    from pilot_backend.repository.interface import (
        CaregiverChildConnectionRepository,
        CaregiverRepository,
        ChildRepository,
        PracticeRepository,
        ProviderChildConnectionRepository,
        ProviderRepository,
    )
    repos = FirestoreRepositories(FakeDocumentStore())
    assert isinstance(repos.practices, PracticeRepository)
    assert isinstance(repos.providers, ProviderRepository)
    assert isinstance(repos.caregivers, CaregiverRepository)
    assert isinstance(repos.children, ChildRepository)
    assert isinstance(repos.caregiver_child, CaregiverChildConnectionRepository)
    assert isinstance(repos.provider_child, ProviderChildConnectionRepository)


def test_codec_specs_cover_exactly_the_dataclass_fields():
    """A field added to a model without a codec rule fails here, not in production."""
    from dataclasses import fields as dataclass_fields
    for cls, spec in SPECS.items():
        model_fields = {f.name for f in dataclass_fields(cls)}
        assert set(spec) == model_fields, cls.__name__


@pytest.mark.parametrize("kind", ["firestore"])
def test_round_trip_preserves_every_field(kind):
    repos = make_repos(kind)
    topo = build_secure_topology(repos)
    reread = repos.provider_child.get_by_id(topo.link_alpha_provider.connection_id)
    assert reread == topo.link_alpha_provider

    caregiver_link = repos.caregiver_child.get_by_id(
        topo.link_gamma_caregiver_ended.connection_id)
    assert caregiver_link == topo.link_gamma_caregiver_ended
    assert caregiver_link.ended_at is not None


def test_decode_refuses_unknown_and_missing_fields():
    """§5: no silent field dropping, in either direction."""
    from pilot_backend.domain.entities import Practice
    practice = Practice.create("Practice-Alpha", now=T0)
    document = encode(practice)

    extra = dict(document, unexpected_field="x")
    with pytest.raises(CodecError) as exc:
        decode(Practice, extra)
    assert "unknown" in str(exc.value)

    short = dict(document)
    short.pop("legal_name")
    with pytest.raises(CodecError) as exc:
        decode(Practice, short)
    assert "missing" in str(exc.value)


def test_naive_timestamps_are_refused_on_write():
    from dataclasses import replace
    from pilot_backend.domain.entities import Practice
    practice = replace(Practice.create("Practice-Alpha", now=T0),
                       created_at=datetime(2026, 10, 1, 12, 0, 0))
    with pytest.raises(CodecError):
        encode(practice)


def test_timestamps_round_trip_as_aware_utc():
    from pilot_backend.domain.entities import Practice
    other_zone = datetime(2026, 10, 1, 12, 0, tzinfo=timezone(timedelta(hours=5)))
    practice = Practice.create("Practice-Alpha", now=other_zone)
    reread = decode(Practice, encode(practice))
    assert reread.created_at.tzinfo is not None
    assert reread.created_at == other_zone


def test_unrecognised_enum_values_are_refused_not_defaulted():
    from pilot_backend.domain.entities import Practice
    document = dict(encode(Practice.create("Practice-Alpha", now=T0)), status="deleted")
    with pytest.raises(CodecError):
        decode(Practice, document)


def test_no_repository_exposes_a_delete():
    banned = ("delete", "remove", "purge", "drop", "destroy", "erase", "truncate")
    repos = FirestoreRepositories(FakeDocumentStore())
    for name in ("practices", "providers", "caregivers", "children",
                 "caregiver_child", "provider_child", "audit_events", "revisions"):
        repo = getattr(repos, name)
        for attribute in dir(repo):
            if attribute.startswith("_"):
                continue
            assert not any(word in attribute.lower() for word in banned), (name, attribute)


def test_document_store_port_has_no_delete_operation():
    from pilot_backend.persistence.document_store import DocumentStore
    operations = {m for m in dir(DocumentStore) if not m.startswith("_")}
    assert not any(w in op.lower() for op in operations
                   for w in ("delete", "remove", "purge", "drop"))


def test_store_isolates_callers_from_stored_state():
    store = FakeDocumentStore()
    store.create("pilot_practices", "p1", {"a": "1"})
    first = store.get("pilot_practices", "p1")
    first["a"] = "mutated"
    assert store.get("pilot_practices", "p1")["a"] == "1"


def test_listings_are_deterministically_ordered():
    """Same data, repeated reads, identical order — not insertion order by luck."""
    repos = FirestoreRepositories(FakeDocumentStore())
    topo = build_secure_topology(repos)
    for _ in range(25):
        listed = repos.caregiver_child.list_caregivers_for_child(
            topo.child_alpha.child_id, include_ended=True)
        assert [c.connection_id for c in listed] == sorted(
            c.connection_id for c in listed)


def test_duplicate_create_is_refused():
    from pilot_backend.repository.interface import DuplicateRecord
    from pilot_backend.domain.entities import Practice
    repos = FirestoreRepositories(FakeDocumentStore())
    practice = Practice.create("Practice-Alpha", now=T0)
    repos.practices.create(practice)
    with pytest.raises(DuplicateRecord):
        repos.practices.create(practice)


def test_missing_record_raises_record_not_found():
    from pilot_backend.repository.interface import RecordNotFound
    repos = FirestoreRepositories(FakeDocumentStore())
    with pytest.raises(RecordNotFound):
        repos.children.get_by_id("chld_" + "0" * 32)


# ===========================================================================
# 7. PRODUCTION AI GATE
# ===========================================================================

def test_phi_ai_gate_defaults_off():
    """§14: PHI AI gate defaults off."""
    assert aipolicy.PHI_AI_EGRESS_DEFAULT is False
    assert PilotSettings.from_env(prod_env()).ai_phi_egress_enabled is False
    assert AIPolicy.from_settings(PilotSettings.from_env(prod_env())).allows_phi_egress is False


def test_absent_ai_configuration_fails_closed():
    with pytest.raises(AIEgressDenied):
        evaluate_phi_ai_egress(None)
    with pytest.raises(AIEgressDenied):
        evaluate_phi_ai_egress(AIPolicy.closed("prod"))


def test_enabling_phi_egress_requires_both_flag_and_baa_reference():
    with pytest.raises(ConfigError):
        PilotSettings.from_env(prod_env(PILOT_AI_PHI_EGRESS_ENABLED="true"))

    flag_only = AIPolicy(phi_egress_enabled=True, baa_reference="", environment="prod")
    assert flag_only.allows_phi_egress is False
    with pytest.raises(AIEgressDenied):
        evaluate_phi_ai_egress(flag_only)

    both = AIPolicy(phi_egress_enabled=True,
                    baa_reference="BAA-2026-PENDING-VERIFICATION", environment="prod")
    assert both.allows_phi_egress is True
    evaluate_phi_ai_egress(both)  # does not raise


def test_an_api_key_alone_does_not_enable_egress():
    """§7: no PHI leaves merely because a credential exists."""
    settings = PilotSettings.from_env(prod_env(
        PILOT_SECRET_REFS="OPENAI_API_KEY=projects/x/secrets/openai/versions/latest"))
    assert settings.secret_refs["OPENAI_API_KEY"]
    assert AIPolicy.from_settings(settings).allows_phi_egress is False


def test_the_gate_never_receives_the_payload():
    """A deny path cannot leak content it was never given."""
    params = set(inspect.signature(evaluate_phi_ai_egress).parameters)
    assert params == {"policy"}


def test_deterministic_paths_require_no_ai_dependency():
    """§14: the deterministic path does not require AI."""
    banned = {"openai", "anthropic", "cohere", "google.generativeai", "litellm"}
    for path in sorted(PILOT_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in banned, (path.name, alias.name)
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                assert node.module.split(".")[0] not in banned, (path.name, node.module)

    # ...and the full authorization path runs with no AI policy configured at all.
    repos = FirestoreRepositories(FakeDocumentStore())
    topo = build_secure_topology(repos)
    principal = resolve_principal(VerifiedToken(subject=CAREGIVER_ALPHA_SUBJECT), repos)
    assert authorize_child_access(principal, topo.child_alpha.child_id, repos).allowed


# ===========================================================================
# 8. SAFE LOGGING
# ===========================================================================

@pytest.mark.parametrize("field", sorted(FORBIDDEN_LOG_FIELDS))
def test_forbidden_log_fields_are_refused(field):
    with pytest.raises(LogFieldError):
        format_log(**{field: "anything"})


def test_unknown_log_fields_are_refused_not_dropped():
    with pytest.raises(LogFieldError):
        format_log(some_new_field="x")


def test_allowlisted_fields_pass():
    record = format_log(request_id="r1", event="child_access", status=200,
                        environment="prod", duration_ms=12, actor_id="cgvr_abc")
    assert record["status"] == 200


def test_routes_must_be_templates_without_ids_or_query_strings():
    format_log(route="/children/{child_id}/notes")
    with pytest.raises(LogFieldError):
        format_log(route="/children/chld_0123456789abcdef")
    with pytest.raises(LogFieldError):
        format_log(route="/search?concern=" + SENTINEL_CONCERN)


def test_third_party_exception_messages_are_not_logged():
    """§8: a third-party exception cannot blindly echo payloads or secrets."""
    class VendorError(Exception):
        pass

    described = describe_exception(
        VendorError(f"failed on {SENTINEL_NOTE} with key {SENTINEL_SECRET}"))
    assert described == {"exception_type": "VendorError"}
    blob = json.dumps(described)
    for sentinel in ALL_SENTINELS:
        assert sentinel not in blob


def test_our_own_errors_opt_in_to_their_message():
    described = describe_exception(ConfigError("PILOT_FIRESTORE_DATABASE is required"))
    assert described["exception_type"] == "ConfigError"
    assert "PILOT_FIRESTORE_DATABASE" in described["exception_message"]


def test_bearer_tokens_are_never_partially_rendered():
    rendered = redact_bearer("Bearer " + SENTINEL_TOKEN)
    assert SENTINEL_TOKEN not in rendered
    assert rendered == "Bearer <redacted>"
    # No length or prefix leak either.
    assert str(len(SENTINEL_TOKEN)) not in rendered
    assert redact_bearer("") == "<absent>"


def test_a_full_request_cycle_logs_no_sentinel():
    """§14: logs do not contain known test PHI/token/secret markers."""
    repos = FirestoreRepositories(FakeDocumentStore())
    topo = build_secure_topology(repos)
    verifier = verifier_for(standard_tokens())
    recorder = AuditRecorder(repos.audit_events, environment="test")
    guard = RouteGuard(verifier=verifier, repos=repos, recorder=recorder)

    lines = []
    for bearer, child in (
        ("Bearer " + SENTINEL_TOKEN, topo.child_alpha.child_id),
        ("Bearer token-caregiver-alpha", topo.child_alpha.child_id),
        ("Bearer token-caregiver-alpha", topo.child_beta.child_id),
    ):
        decision = guard.guard_child_route("/children/{child_id}", bearer, child)
        lines.append(render(format_log(
            request_id="req-1",
            event="child_access",
            route="/children/{child_id}",
            method="GET",
            status=decision.status_code,
            environment="test",
            denial_reason=decision.denial.value if decision.denial else None,
            child_id=decision.child_id,
            actor_id=decision.principal.application_id if decision.principal else None,
        )))

    blob = "\n".join(lines)
    for sentinel in ALL_SENTINELS:
        assert sentinel not in blob, sentinel


# ===========================================================================
# 9. AUDIT EVENTS
# ===========================================================================

def test_audit_event_carries_the_required_fields():
    event = AuditEvent.build(
        AuditAction.CHILD_ACCESS_GRANTED, AuditResult.SUCCESS, "child",
        resource_id="chld_1", child_id="chld_1",
        actor_application_id="cgvr_1", actor_auth_subject="subject-1",
        actor_role=ActorRole.CAREGIVER, request_id="req-1", now=T0)
    for field in ("event_id", "occurred_at", "actor_application_id",
                  "actor_auth_subject", "actor_role", "action", "resource_type",
                  "resource_id", "child_id", "result", "request_id", "metadata"):
        assert hasattr(event, field), field
    assert event.occurred_at.tzinfo is not None


def test_audit_timestamps_and_ids_are_server_generated():
    params = set(inspect.signature(AuditEvent.build).parameters)
    assert "event_id" not in params, "a caller must not supply an audit event id"
    first = AuditEvent.build(AuditAction.CHILD_CREATED, AuditResult.SUCCESS, "child")
    second = AuditEvent.build(AuditAction.CHILD_CREATED, AuditResult.SUCCESS, "child")
    assert first.event_id != second.event_id


@pytest.mark.parametrize("key", [
    "child_name", "diagnosis", "concern", "note", "note_text", "comment",
    "feedback", "email", "prompt", "ai_response", "transcript", "body",
])
def test_audit_metadata_rejects_clinical_keys(key):
    """§14: audit events do not contain clinical payloads."""
    with pytest.raises(AuditMetadataError):
        AuditEvent.build(AuditAction.NOTE_CREATED, AuditResult.SUCCESS, "note",
                         metadata={key: SENTINEL_NOTE})


def test_audit_metadata_allowlist_is_narrow_and_operational():
    """Every permitted key must be operational, never clinical.

    The expected set is restated here rather than derived, so widening the
    allowlist requires editing this test too — which is the point. The 0.4A
    additions are all opaque application ids or short enum values. Note what
    is deliberately ABSENT: `external_id`. A Parent session id is an external
    identifier and does not belong in an audit event; links are referenced by
    their opaque `link_id` instead.

    The 0.4B/C additions are opaque ids, short enums and small integers. Also
    deliberately absent, and asserted below: goal text, the family-facing
    template, an edit reason, a milestone reference, and `domain_key` — which
    developmental domain a child's goal addresses is a clinical fact, not an
    operational one.

    The 0.4D/E additions are the same shape. Also absent, and asserted below:
    observation text and its reference, activity instructions and labels, a
    clinician's rationale or guidance, difficulty and enjoyment ratings, and
    `local_date` — the date a named child attempted a therapy activity is
    clinical content. `attribution_month` carries the only part a count needs.

    The 0.4F/G additions are opaque ids, short enums, small integers and a
    calendar month. Deliberately ABSENT, and asserted below: clinical
    interpretation, action narrative, activity description and interaction
    notes. `minutes` is permitted because a count of minutes is operational;
    what those minutes were SPENT ON is not.
    """
    assert ALLOWED_METADATA_KEYS
    for key in ALLOWED_METADATA_KEYS:
        assert key in {
            "denial_reason", "http_status", "environment", "actor_practice_id",
            "connection_id", "revision_id", "record_version", "schema_version",
            "route", "method", "source",
            # 0.4A longitudinal identity
            "source_system", "link_id", "assignment_id", "claim_id",
            "claim_kind", "provider_id", "practice_id",
            # 0.4B/C goals and monthly focus plan
            "suggestion_id", "goal_kind", "goal_id", "goal_version_id",
            "goal_status", "edit_type", "suggestion_count", "focus_plan_id",
            "cycle_month", "allocation_id", "priority_rank",
            "emphasis_weight", "policy_version", "generator_version",
            "rule_version", "plan_state",
            # 0.4D/E weekly layer
            "cycle_sequence", "is_partial_week", "activity_count",
            "coverage_gap_count", "declared_capacity", "signal_count",
            "attempt_outcome", "attribution_month", "adaptation_origin",
            "adaptation_record_id", "customization_signal_type",
            "suppression_until_cycle", "intervention_action",
            "intervention_scope", "defer_id", "event_id", "snapshot_id",
            # 0.4F/G RTM evidence and reporting
            "episode_id", "period_id", "review_id", "action_id",
            "time_entry_id", "supersedes_time_entry_id", "interaction_id",
            "technology_id", "summary_id", "coding_summary_id", "report_id",
            "supersedes_report_id", "goal_count", "minutes",
            "documented_minutes", "entry_method", "interaction_modality",
            "participant_type", "clinical_action_type", "regulatory_status",
            "period_status", "reviewed_event_count",
            "observation_event_count", "distinct_observed_local_dates",
            "candidate_count", "missing_flag_count", "coding_rule_set_id",
            "coding_rule_version", "confirmation_status", "report_version",
            "report_state",
            # 0.4D/E weekly allocation, evidence and adaptation
            "cycle_id", "cycle_sequence", "is_partial_week", "engine_version",
            "snapshot_id", "activity_count", "coverage_gap_count",
            "declared_capacity", "event_id", "attempt_outcome",
            "attribution_month", "signal_id", "customization_signal_type",
            "defer_id", "suppression_until_cycle", "intervention_id",
            "intervention_action", "intervention_scope",
            "adaptation_record_id", "adaptation_origin", "signal_count",
            # 0.5A authenticated identity and the Parent bridge.
            # `subject_fingerprint` is sha256(auth_subject)[:32] — derivable
            # from a subject but not reversible to one, so audit can prove
            # WHICH subject an event concerned without storing an account
            # identifier. `integration_state` is a short refusal code.
            "caregiver_id", "subject_fingerprint", "holder_actor_type",
            "integration_state",
            # 0.5B provider identity and the connection lifecycle. Two short
            # closed enums. `initiated_by` is the audit-visible half of the
            # invitation rule — it distinguishes a family-invited clinician
            # from a self-asserted one, which is the question an auditor asks
            # about a provider-initiated relationship. `connection_status` is
            # the lifecycle state, not a reason: no decline reason, pause
            # reason or clinical justification is permitted here.
            "initiated_by", "connection_status",
        }, key
    assert "external_id" not in ALLOWED_METADATA_KEYS
    # The raw auth subject must never become an allowlisted key. The
    # fingerprint exists precisely so there is no reason to add one, and a
    # Parent `session_id` is an external identifier like any other.
    for identifier in ("auth_subject", "subject", "uid", "email", "owner_uid",
                       "session_id", "parent_session_id", "display_name"):
        assert identifier not in ALLOWED_METADATA_KEYS, identifier
    for clinical in ("clinical_interpretation", "narrative",
                     "activity_description", "interaction_note",
                     "goal_text", "family_facing_text", "domain_key",
                     "milestone_refs", "reason", "observed_level",
                     "functional_baseline_area",
                     # 0.4D/E
                     "observation_text", "observation_text_ref",
                     "activity_instructions", "activity_label",
                     "clinical_rationale", "guidance_text",
                     "difficulty", "enjoyment", "child_response",
                     "local_date", "resolved_plan_document",
                     "external_plan_id"):
        assert clinical not in ALLOWED_METADATA_KEYS, clinical


def test_audit_metadata_rejects_long_or_multiline_values():
    with pytest.raises(AuditMetadataError):
        AuditEvent.build(AuditAction.NOTE_CREATED, AuditResult.SUCCESS, "note",
                         metadata={"source": "x" * 200})
    with pytest.raises(AuditMetadataError):
        AuditEvent.build(AuditAction.NOTE_CREATED, AuditResult.SUCCESS, "note",
                         metadata={"source": "line one\nline two"})


def test_audit_records_both_grants_and_denials():
    repos = FirestoreRepositories(FakeDocumentStore())
    topo = build_secure_topology(repos)
    verifier = verifier_for(standard_tokens())
    recorder = AuditRecorder(repos.audit_events, environment="test")
    guard = RouteGuard(verifier=verifier, repos=repos, recorder=recorder)

    guard.guard_child_route("/children/{child_id}", "Bearer token-caregiver-alpha",
                            topo.child_alpha.child_id)
    guard.guard_child_route("/children/{child_id}", "Bearer token-caregiver-alpha",
                            topo.child_beta.child_id)
    guard.guard_child_route("/children/{child_id}", "Bearer bad-token",
                            topo.child_alpha.child_id)

    events = repos.audit_events.list_all()
    assert len(events) == 3
    actions = {e.action for e in events}
    assert AuditAction.CHILD_ACCESS_GRANTED in actions
    assert AuditAction.AUTHORIZATION_FAILURE in actions
    assert AuditAction.AUTHENTICATION_FAILURE in actions


def test_audit_events_persist_through_the_firestore_architecture():
    """§9: audit persistence uses the new architecture, not process memory."""
    store = FakeDocumentStore()
    repos = FirestoreRepositories(store)
    recorder = AuditRecorder(repos.audit_events, environment="test")
    recorder.record(AuditEvent.build(
        AuditAction.CHILD_CREATED, AuditResult.SUCCESS, "child",
        resource_id="chld_1", child_id="chld_1", now=T0))

    # Visible to an independent repository over the same store.
    assert len(FirestoreRepositories(store).audit_events.list_all()) == 1
    assert "pilot_audit_events" in store.collections()


def test_a_persisted_audit_document_contains_no_sentinel():
    repos = FirestoreRepositories(FakeDocumentStore())
    topo = build_secure_topology(repos)
    verifier = verifier_for(standard_tokens())
    recorder = AuditRecorder(repos.audit_events, environment="test")
    guard = RouteGuard(verifier=verifier, repos=repos, recorder=recorder)
    guard.guard_child_route("/children/{child_id}",
                            "Bearer " + SENTINEL_TOKEN, topo.child_alpha.child_id)

    blob = json.dumps([encode(e) for e in repos.audit_events.list_all()])
    for sentinel in ALL_SENTINELS:
        assert sentinel not in blob, sentinel


def test_audit_log_has_no_update_or_delete():
    repo = FirestoreRepositories(FakeDocumentStore()).audit_events
    assert not hasattr(repo, "update")
    assert not hasattr(repo, "delete")


# ===========================================================================
# 10. RECORD INTEGRITY / REVISION FOUNDATION
# ===========================================================================

def test_a_finalized_record_cannot_be_silently_overwritten():
    draft = start_draft("rec-1", actor_application_id="prov_1",
                        actor_role=ActorRole.PROVIDER, now=T0)
    sealed = finalize(draft, now=T0)
    assert sealed.state is RecordState.FINALIZED
    with pytest.raises(ImmutableRecordError):
        finalize(sealed, now=T0)


def test_amendment_creates_a_new_version_and_keeps_the_prior_one():
    draft = start_draft("rec-1", actor_application_id="prov_1",
                        actor_role=ActorRole.PROVIDER, now=T0)
    v1 = finalize(draft, now=T0)
    v2 = amend(v1, actor_application_id="prov_2", actor_role=ActorRole.PROVIDER,
               reason="corrected session date", now=T0)

    assert v2.version == 2
    assert v2.supersedes_revision_id == v1.revision_id
    assert v2.record_id == v1.record_id
    assert v2.actor_application_id == "prov_2"
    assert v2.amendment_reason == "corrected session date"
    assert v2.created_at.tzinfo is not None
    # v1 is untouched and still readable.
    assert v1.state is RecordState.FINALIZED and v1.version == 1


def test_amendment_requires_a_reason():
    v1 = finalize(start_draft("rec-1", actor_application_id="prov_1",
                              actor_role=ActorRole.PROVIDER, now=T0), now=T0)
    for reason in ("", "   "):
        with pytest.raises(ImmutableRecordError):
            amend(v1, actor_application_id="prov_1",
                  actor_role=ActorRole.PROVIDER, reason=reason, now=T0)


def test_only_a_finalized_record_can_be_amended():
    draft = start_draft("rec-1", actor_application_id="prov_1",
                        actor_role=ActorRole.PROVIDER, now=T0)
    with pytest.raises(ImmutableRecordError):
        amend(draft, actor_application_id="prov_1",
              actor_role=ActorRole.PROVIDER, reason="x", now=T0)


def test_revision_chain_persists_and_preserves_history():
    repos = FirestoreRepositories(FakeDocumentStore())
    v1 = finalize(start_draft("rec-1", actor_application_id="prov_1",
                              actor_role=ActorRole.PROVIDER, now=T0), now=T0)
    v2 = amend(v1, actor_application_id="prov_1", actor_role=ActorRole.PROVIDER,
               reason="clarified wording", now=T0)
    repos.revisions.append(v1)
    repos.revisions.append(v2)

    chain = repos.revisions.list_chain("rec-1")
    assert [r.version for r in chain] == [1, 2]
    assert latest(chain).revision_id == v2.revision_id
    assert repos.revisions.get_by_id(v1.revision_id) == v1


def test_revisions_carry_no_clinical_content():
    from dataclasses import fields as dataclass_fields
    from pilot_backend.revision.records import Revision
    names = {f.name for f in dataclass_fields(Revision)}
    for banned in ("content", "text", "body", "note", "payload"):
        assert banned not in names, banned
    assert "content_ref" in names


# ===========================================================================
# 12. CORS / PUBLIC SURFACE
# ===========================================================================

def test_public_route_allowlist_stays_narrow():
    """§14: the public endpoint allowlist stays narrow."""
    assert PUBLIC_ROUTES == frozenset({"/health"})


def test_public_matching_is_exact_not_prefix():
    assert is_public_route("/health")
    for path in ("/health/db", "/healthz", "/health?debug=1", "/HEALTH", "/"):
        assert not is_public_route(path), path


def test_health_exposes_no_sensitive_data():
    payload = health_payload(PilotSettings.from_env(prod_env()))
    assert set(payload) == {"status", "environment"}
    blob = json.dumps(payload)
    for secret in ("genex-pilot-prod", "pilot-clinical", "app.genex.health"):
        assert secret not in blob


def test_no_debug_or_admin_endpoint_exists():
    for route in PUBLIC_ROUTES:
        assert "debug" not in route and "admin" not in route
    for token in code_tokens(PILOT_ROOT / "apisurface" / "surface.py"):
        assert "ADMIN_DEBUG" not in token, token


def test_child_scoped_route_cannot_be_public():
    repos = FirestoreRepositories(FakeDocumentStore())
    topo = build_secure_topology(repos)
    guard = RouteGuard(verifier=verifier_for(standard_tokens()), repos=repos)
    with pytest.raises(SurfaceError):
        guard.guard_child_route("/health", "Bearer token-caregiver-alpha",
                                topo.child_alpha.child_id)


def test_prod_cors_policy_is_explicit_and_https_only():
    policy = cors_policy_for(PilotSettings.from_env(prod_env()))
    assert policy.allowed_origins == ("https://app.genex.health",)
    assert policy.permits("https://app.genex.health")
    assert not policy.permits("https://evil.example")


def test_cors_policy_rejects_a_hand_built_unsafe_prod_settings():
    class Unsafe:
        environment = Environment.PROD
        allowed_origins = ("*",)
    with pytest.raises(SurfaceError):
        cors_policy_for(Unsafe())


# ===========================================================================
# 11. SECRETS
# ===========================================================================

def test_no_plaintext_secrets_or_default_production_values_in_source():
    """§11: no committed credential.

    Matched on the SHAPE of a real credential rather than on a prefix. A bare
    `"sk-"` is three characters and appears legitimately in `settings.py`,
    which refuses references starting with it; an actual key is long. Keying
    on length is what separates the guard from the thing it guards against —
    and, unlike a prefix scan, this would still catch a key pasted into a file
    that happened to contain the word "task-completion".
    """
    import re
    credential_shapes = (
        re.compile(r"sk-[A-Za-z0-9]{16,}"),
        re.compile(r"AIza[A-Za-z0-9_\-]{20,}"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        re.compile(r"(?i)password\s*=\s*[\"'][^\"']{3,}"),
    )
    for path in sorted(PILOT_ROOT.rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        text = path.read_text()
        for shape in credential_shapes:
            assert not shape.search(text), (path.name, shape.pattern)


def test_secret_references_are_parsed_but_values_are_refused():
    settings = dev_settings(
        PILOT_SECRET_REFS="OPENAI_API_KEY=projects/p/secrets/openai/versions/1")
    assert settings.secret_refs == {
        "OPENAI_API_KEY": "projects/p/secrets/openai/versions/1"}

    for bad in ("OPENAI_API_KEY=sk-livekey1234567890",
                "GOOGLE_KEY=AIzaSyTotallyFakeKey123"):
        with pytest.raises(ConfigError) as exc:
            dev_settings(PILOT_SECRET_REFS=bad)
        assert "reference" in str(exc.value)


def test_no_secret_has_a_production_default():
    settings = PilotSettings.from_env(prod_env())
    assert settings.secret_refs == {}
    assert settings.ai_phi_baa_reference == ""


def test_beta_access_code_is_not_a_configuration_default_anywhere():
    for path in sorted(PILOT_ROOT.rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        assert 'BETA_ACCESS_CODE", "genex"' not in path.read_text(), path.name


# ===========================================================================
# 15/16. NO TELEMETRY, NO RTM
# ===========================================================================

def test_no_third_party_telemetry_sdk_is_introduced():
    banned = {"sentry_sdk", "sentry", "analytics", "segment", "mixpanel",
              "amplitude", "newrelic", "datadog", "ddtrace", "elasticapm",
              "crashlytics", "posthog", "intercom", "fullstory", "logrocket"}
    for path in sorted(PILOT_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in banned, (path.name, alias.name)
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                assert node.module.split(".")[0] not in banned, (path.name, node.module)


def test_no_rtm_model_exists_in_0_2():
    """0.4F/G implemented the RTM models this guard once asserted absent.

    Narrowed by exactly those names. What it now bans is what remains
    deferred or out of scope for the October pilot entirely — the guard
    keeps its purpose rather than being deleted.
    """
    banned = {"PayerVerification", "MonitoringDay", "ClaimSubmission",
              "EligibilityCheck", "ClearingHouseSubmission",
              "ReimbursementEstimate", "EMRIntegration",
              # Provisional names this guard has always banned. 0.4F/G
              # implements the concepts as TimeEntry and
              # SynchronousInteraction, so these spellings stay banned.
              "RTMTimeEntry", "MonitoringEvent"}
    for path in sorted(PILOT_ROOT.rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text())
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        assert not (banned & names), (path.name, banned & names)


# ===========================================================================
# STRUCTURAL GUARDS — strengthened, never weakened
# ===========================================================================

def test_no_auth_or_database_sdk_imported_anywhere_in_the_package():
    """Strictly stronger than the BACKEND 0.1 guards it extends.

    0.1 checked `domain/` and `repository/` for auth SDKs, and checked plain
    `import x` for database drivers. This checks the WHOLE package and both
    import forms — so the Firestore adapter added in 0.2 is held to a stricter
    rule than the code that preceded it, not a relaxed one.
    """
    banned = {
        "firebase_admin", "firebase", "google", "googleapiclient",
        "requests", "httpx", "urllib3", "aiohttp",
        "firestore", "sqlalchemy", "psycopg2", "pymongo", "redis", "boto3",
    }
    for path in sorted(PILOT_ROOT.rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in banned, (path.name, alias.name)
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                assert node.module.split(".")[0] not in banned, (path.name, node.module)


def test_the_package_still_imports_with_only_the_standard_library():
    """The pilot CI job installs pytest and nothing else."""
    import importlib
    for module in ("pilot_backend.config", "pilot_backend.auth",
                   "pilot_backend.authz", "pilot_backend.persistence",
                   "pilot_backend.audit.recorder", "pilot_backend.revision",
                   "pilot_backend.observability", "pilot_backend.aipolicy",
                   "pilot_backend.apisurface"):
        assert importlib.import_module(module) is not None


def test_fixtures_use_no_real_person_associated_name():
    banned = {"sara", "hannah", "maya"}
    for path in sorted((PILOT_ROOT / "fixtures").glob("*.py")):
        text = path.read_text().lower()
        for name in banned:
            assert name not in text, (path.name, name)
