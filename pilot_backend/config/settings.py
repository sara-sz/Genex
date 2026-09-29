"""pilot_backend/config/settings.py — environment-aware configuration.

One immutable settings object, built from an EXPLICIT mapping, validated at
construction, and refusing to exist at all if production is misconfigured.

## Why an explicit mapping instead of reading os.environ

`from_env(mapping)` takes the environment as an argument. Nothing in this
package reads `os.environ` implicitly, which means every validation rule below
is testable without mutating process state, and a test cannot accidentally
inherit a developer's shell.

## No fallbacks, in either direction

There is no default for any production-significant value. A missing key is a
`ConfigError`, never a substituted default:

  * prod must not fall back to dev — the failure mode is a production service
    quietly writing patient records into a development database;
  * dev must not fall back to prod — the failure mode is a test run mutating
    live clinical data.

Both directions are refused by the same rule (no defaults) plus
`DEV_RESOURCE_MARKERS`, which rejects a production configuration that names a
resource looking like a development one.

## The legacy-resource denylist is a guard, not a hard-coded resource

`FORBIDDEN_LEGACY_RESOURCES` names Parent 2.3 infrastructure. It is a DENY
list: these strings can never be selected, and no value here is ever used AS
configuration. That is the opposite of hard-coding a project id — it exists
precisely so the new pilot store can never be pointed at the protected
historical Parent session bucket.

## Secrets

Only secret REFERENCES are held (`OPENAI_API_KEY=projects/x/secrets/y/versions/z`
style names), never secret values. There is no default production secret and no
committed credential. `BETA_ACCESS_CODE` is modelled here as an ordinary
reference if it is retained at all — it is NOT an authorization primitive and
nothing in `authz` consults it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Tuple

from .errors import ConfigError


class Environment(str, Enum):
    """The three service environments. There is no "unknown" that still serves."""

    DEV = "dev"
    TEST = "test"
    PROD = "prod"

    @property
    def is_prod(self) -> bool:
        return self is Environment.PROD

    @property
    def is_dev_or_test(self) -> bool:
        return self in (Environment.DEV, Environment.TEST)


#: Configuration keys that production cannot start without. Absence is a
#: startup failure, never a default.
REQUIRED_PROD_KEYS: Tuple[str, ...] = (
    "PILOT_GCP_PROJECT_ID",
    "PILOT_FIREBASE_PROJECT_ID",
    "PILOT_FIRESTORE_DATABASE",
    "PILOT_ALLOWED_ORIGINS",
)

#: Substrings that mark a resource as non-production. A prod configuration
#: naming any of these is refused: it almost certainly means a dev value was
#: promoted by accident, and the consequence is production PHI in a dev store.
DEV_RESOURCE_MARKERS: Tuple[str, ...] = (
    "dev", "staging", "test", "sandbox", "emulator", "localhost", "127.0.0.1",
)

#: Parent 2.3 / Beta infrastructure. Protected historical baseline. The pilot
#: backend must never read or write these, in any environment.
FORBIDDEN_LEGACY_RESOURCES: frozenset = frozenset({
    "genex-api-dev-sessions-genex-mvp-2026",
    "genex-api-prod-sessions-genex-mvp-2026",
    "genex-api-staging",
    "genex-api-prod",
})

#: Origins that may never appear in a production CORS allowlist. Third-party
#: preview/build hosts are development conveniences; a production PHI origin
#: must be an origin we control and can attest to.
FORBIDDEN_PROD_ORIGIN_MARKERS: Tuple[str, ...] = (
    "lovable.dev", "lovableproject.com", "lovable.app", "localhost", "127.0.0.1", "ngrok",
)

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off", ""})


def _as_bool(raw: str, key: str) -> bool:
    """Strict boolean. An unrecognised value is a config error, not False.

    Silently coercing "TRUE " or "disabled" to False would let a security flag
    read as off when the operator believed it was on — or worse, the reverse.
    """
    value = (raw or "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{key}: not a recognised boolean")


def _as_origins(raw: str) -> Tuple[str, ...]:
    return tuple(o.strip() for o in (raw or "").split(",") if o.strip())


def _as_secret_refs(raw: str) -> Mapping[str, str]:
    """Parse NAME=reference pairs. References only — never values.

    We cannot prove a string is a reference rather than a secret, but we can
    refuse the shapes that are obviously secrets, which catches the realistic
    mistake of pasting a key into the wrong variable.
    """
    refs = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ConfigError("PILOT_SECRET_REFS: expected NAME=reference pairs")
        name, _, ref = pair.partition("=")
        name, ref = name.strip(), ref.strip()
        if not name or not ref:
            raise ConfigError("PILOT_SECRET_REFS: empty name or reference")
        if ref.startswith(("sk-", "AIza", "-----BEGIN")):
            raise ConfigError(f"PILOT_SECRET_REFS: {name} looks like a secret VALUE, not a reference")
        refs[name] = ref
    return dict(refs)


@dataclass(frozen=True)
class PilotSettings:
    """Validated configuration for one running pilot backend."""

    environment: Environment
    gcp_project_id: str = ""
    firebase_project_id: str = ""
    firestore_database: str = ""
    allowed_origins: Tuple[str, ...] = ()
    secret_refs: Mapping[str, str] = field(default_factory=dict)

    #: PHI-bearing AI egress. Default OFF everywhere. See `aipolicy`.
    ai_phi_egress_enabled: bool = False
    #: Required alongside the flag before PHI may leave for an AI vendor.
    ai_phi_baa_reference: str = ""

    #: Local fictional dev auth. Never honoured in prod — enforced twice, here
    #: and again in `auth.build_verifier`.
    dev_auth_enabled: bool = False

    #: PRE-PHI 0.3. Firestore emulator endpoint, dev/test ONLY. Held as
    #: configuration rather than read from the ambient FIRESTORE_EMULATOR_HOST
    #: so that pointing at an emulator is something a deployment must ASK for,
    #: and so production can refuse it at startup instead of discovering it
    #: when a write silently lands nowhere real.
    firestore_emulator_host: str = ""

    def __post_init__(self) -> None:
        self._validate()

    # -- validation ---------------------------------------------------------

    def _validate(self) -> None:
        self._reject_legacy_resources()
        if self.environment.is_prod:
            self._validate_prod()

    def _reject_legacy_resources(self) -> None:
        """§6: the pilot store is not the Parent 2.3 session store.

        Checked in EVERY environment, not just prod. A dev pointer at the real
        Parent bucket is the same disclosure as a prod one.
        """
        configured = (
            self.gcp_project_id, self.firebase_project_id, self.firestore_database,
        ) + tuple(self.allowed_origins) + tuple(self.secret_refs.values())
        for value in configured:
            for legacy in FORBIDDEN_LEGACY_RESOURCES:
                if legacy and legacy in value:
                    raise ConfigError(
                        f"configuration names protected Parent 2.3 resource: {legacy}"
                    )

    def _validate_prod(self) -> None:
        missing = [
            key for key, value in (
                ("PILOT_GCP_PROJECT_ID", self.gcp_project_id),
                ("PILOT_FIREBASE_PROJECT_ID", self.firebase_project_id),
                ("PILOT_FIRESTORE_DATABASE", self.firestore_database),
            ) if not value.strip()
        ]
        if not self.allowed_origins:
            missing.append("PILOT_ALLOWED_ORIGINS")
        if missing:
            raise ConfigError(f"prod is missing required configuration: {', '.join(sorted(missing))}")

        if self.dev_auth_enabled:
            raise ConfigError("prod must not enable dev auth (PILOT_DEV_AUTH_ENABLED)")

        if self.firestore_emulator_host.strip():
            raise ConfigError(
                "prod must not configure a Firestore emulator "
                "(PILOT_FIRESTORE_EMULATOR_HOST)")

        # A dev-looking resource in prod means a dev value was promoted.
        for label, value in (
            ("PILOT_GCP_PROJECT_ID", self.gcp_project_id),
            ("PILOT_FIREBASE_PROJECT_ID", self.firebase_project_id),
            ("PILOT_FIRESTORE_DATABASE", self.firestore_database),
        ):
            lowered = value.lower()
            for marker in DEV_RESOURCE_MARKERS:
                if marker in lowered:
                    raise ConfigError(
                        f"prod {label} names a non-production resource (contains '{marker}')"
                    )

        for origin in self.allowed_origins:
            if origin == "*":
                raise ConfigError("prod CORS must not use a wildcard origin")
            if not origin.startswith("https://"):
                raise ConfigError("prod CORS origins must be https")
            lowered = origin.lower()
            for marker in FORBIDDEN_PROD_ORIGIN_MARKERS:
                if marker in lowered:
                    raise ConfigError(f"prod CORS origin is not production-safe: contains '{marker}'")

        if self.ai_phi_egress_enabled and not self.ai_phi_baa_reference.strip():
            raise ConfigError(
                "prod PHI AI egress requires an explicit BAA reference "
                "(PILOT_AI_PHI_BAA_REFERENCE)"
            )

    # -- construction -------------------------------------------------------

    @staticmethod
    def from_env(env: Mapping[str, str]) -> "PilotSettings":
        """Build from an explicit mapping. Missing environment is fatal."""
        raw_env = (env.get("PILOT_ENVIRONMENT") or "").strip().lower()
        if not raw_env:
            raise ConfigError("PILOT_ENVIRONMENT is required (dev|test|prod); there is no default")
        try:
            environment = Environment(raw_env)
        except ValueError:
            raise ConfigError(f"PILOT_ENVIRONMENT must be one of dev|test|prod, got '{raw_env}'")

        return PilotSettings(
            environment=environment,
            gcp_project_id=(env.get("PILOT_GCP_PROJECT_ID") or "").strip(),
            firebase_project_id=(env.get("PILOT_FIREBASE_PROJECT_ID") or "").strip(),
            firestore_database=(env.get("PILOT_FIRESTORE_DATABASE") or "").strip(),
            allowed_origins=_as_origins(env.get("PILOT_ALLOWED_ORIGINS", "")),
            secret_refs=_as_secret_refs(env.get("PILOT_SECRET_REFS", "")),
            ai_phi_egress_enabled=_as_bool(
                env.get("PILOT_AI_PHI_EGRESS_ENABLED", ""), "PILOT_AI_PHI_EGRESS_ENABLED"),
            ai_phi_baa_reference=(env.get("PILOT_AI_PHI_BAA_REFERENCE") or "").strip(),
            dev_auth_enabled=_as_bool(
                env.get("PILOT_DEV_AUTH_ENABLED", ""), "PILOT_DEV_AUTH_ENABLED"),
            firestore_emulator_host=(
                env.get("PILOT_FIRESTORE_EMULATOR_HOST") or "").strip(),
        )

    # -- safe projection ----------------------------------------------------

    def public_config(self) -> Mapping[str, str]:
        """Config safe to expose to an unauthenticated caller.

        Environment name only. No project ids, no database name, no origins, no
        secret references — an unauthenticated caller learns nothing about the
        infrastructure it is talking to.
        """
        return {"environment": self.environment.value}
