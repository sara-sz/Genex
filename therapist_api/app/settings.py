"""Typed environment settings for the therapist service.

Settings are parsed from a plain mapping (os.environ by default) into a typed,
immutable dataclass. Parsing does NOT validate cross-field safety rules — that
is `env_validation.validate_settings`, which is fail-closed and called before
the app is allowed to serve traffic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _as_bool(raw: Optional[str]) -> bool:
    return (raw or "").strip().lower() in _TRUE_VALUES


def _as_origins(raw: Optional[str]) -> Tuple[str, ...]:
    if not raw:
        return ()
    return tuple(o.strip() for o in raw.split(",") if o.strip())


@dataclass(frozen=True)
class Settings:
    """Immutable, typed view of the service environment.

    `environment` may be empty/unknown here; validation is what rejects it.
    """

    environment: str
    gcp_project_id: str
    firebase_project_id: str
    firestore_project_id: str
    region: str
    allowed_origins: Tuple[str, ...] = field(default_factory=tuple)
    seed_enabled: bool = False
    registration_policy: str = ""
    debug_panels: bool = False
    release_tag: str = ""
    image_digest: str = ""
    service_account: str = ""
    # Local fictional dev-auth adapter. Disabled by default; only ever honored
    # when environment is dev/test (see env_validation + dev_adapter). Never prod.
    dev_auth_enabled: bool = False

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if environ is None else environ
        return cls(
            environment=(env.get("ENVIRONMENT") or "").strip().lower(),
            gcp_project_id=(env.get("GCP_PROJECT_ID") or "").strip(),
            firebase_project_id=(env.get("FIREBASE_PROJECT_ID") or "").strip(),
            firestore_project_id=(env.get("FIRESTORE_PROJECT_ID") or "").strip(),
            region=(env.get("REGION") or "").strip(),
            allowed_origins=_as_origins(env.get("ALLOWED_ORIGINS")),
            seed_enabled=_as_bool(env.get("SEED_ENABLED")),
            registration_policy=(env.get("REGISTRATION_POLICY") or "").strip(),
            debug_panels=_as_bool(env.get("DEBUG_PANELS")),
            release_tag=(env.get("RELEASE_TAG") or "").strip(),
            image_digest=(env.get("IMAGE_DIGEST") or "").strip(),
            service_account=(env.get("SERVICE_ACCOUNT") or "").strip(),
            dev_auth_enabled=_as_bool(env.get("DEV_AUTH_ENABLED")),
        )

    @property
    def is_prod(self) -> bool:
        return self.environment == "prod"

    @property
    def is_dev(self) -> bool:
        return self.environment == "dev"

    @property
    def is_dev_or_test(self) -> bool:
        return self.environment in ("dev", "test")

    def safe_public_config(self) -> dict:
        """Config safe to expose to unauthenticated clients (no secrets, no infra)."""
        return {
            "environment": self.environment,
            "registration_policy": self.registration_policy,
        }
