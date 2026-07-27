"""Fail-closed environment validation.

The service refuses to serve traffic unless the environment is coherent and
safe. Every check that fails raises `EnvironmentValidationError` with an
explicit, human-readable reason. Prod is held to stricter rules than Dev.

This is a SAFETY boundary, not a convenience — when in doubt, it fails closed.
"""

from __future__ import annotations

from typing import List

from .constants import (
    CANONICAL_PROJECT,
    DEV_LOVABLE_ORIGIN,
    ENVIRONMENTS,
    PROD_LOVABLE_ORIGIN,
)
from .settings import Settings


class EnvironmentValidationError(RuntimeError):
    """Raised when the runtime environment is missing, incoherent, or unsafe."""


def _collect_errors(s: Settings) -> List[str]:
    errors: List[str] = []

    # 1. ENVIRONMENT must be present and one of the known values.
    if not s.environment:
        errors.append("ENVIRONMENT is required (expected 'dev' or 'prod').")
        return errors  # nothing else is meaningful without an environment
    if s.environment not in ENVIRONMENTS:
        errors.append(
            f"ENVIRONMENT '{s.environment}' is invalid (expected one of {ENVIRONMENTS})."
        )
        return errors

    expected_project = CANONICAL_PROJECT[s.environment]

    # 2. Required identity fields must be present.
    for name, value in (
        ("GCP_PROJECT_ID", s.gcp_project_id),
        ("FIREBASE_PROJECT_ID", s.firebase_project_id),
        ("FIRESTORE_PROJECT_ID", s.firestore_project_id),
        ("REGION", s.region),
    ):
        if not value:
            errors.append(f"{name} is required.")

    # 3. Project/environment coherence — the three project IDs must all equal the
    #    canonical project for this environment (prevents dev<->prod cross-wiring).
    if s.gcp_project_id and s.gcp_project_id != expected_project:
        errors.append(
            f"GCP_PROJECT_ID '{s.gcp_project_id}' does not match the canonical "
            f"project for '{s.environment}' ('{expected_project}')."
        )
    if s.firebase_project_id and s.firebase_project_id != expected_project:
        errors.append(
            f"FIREBASE_PROJECT_ID '{s.firebase_project_id}' must equal the "
            f"'{s.environment}' project '{expected_project}' (auth issuer isolation)."
        )
    if s.firestore_project_id and s.firestore_project_id != expected_project:
        errors.append(
            f"FIRESTORE_PROJECT_ID '{s.firestore_project_id}' must equal the "
            f"'{s.environment}' project '{expected_project}'."
        )

    # 4. CORS: no wildcard, ever.
    for origin in s.allowed_origins:
        if "*" in origin:
            errors.append(f"Wildcard CORS origin is forbidden: '{origin}'.")

    # 5. CORS: dev and prod front-end origins must never be mixed.
    has_dev_origin = DEV_LOVABLE_ORIGIN in s.allowed_origins
    has_prod_origin = PROD_LOVABLE_ORIGIN in s.allowed_origins
    if has_dev_origin and has_prod_origin:
        errors.append(
            "Dev and Prod front-end origins are both present in ALLOWED_ORIGINS "
            "(environments must not be mixed)."
        )

    # 6. Environment-specific rules.
    if s.environment == "prod":
        if s.seed_enabled:
            errors.append("Prod must not enable seeding (SEED_ENABLED must be false).")
        if s.debug_panels:
            errors.append("Prod must not enable debug bypass (DEBUG_PANELS must be false).")
        if s.dev_auth_enabled:
            errors.append(
                "Prod must not enable the local dev-auth adapter "
                "(DEV_AUTH_ENABLED must be false)."
            )
        if has_dev_origin:
            errors.append("Prod must not allow the Dev front-end origin.")
        for origin in s.allowed_origins:
            if "localhost" in origin or "127.0.0.1" in origin:
                errors.append(f"Prod must not allow a localhost origin: '{origin}'.")
        if s.registration_policy and s.registration_policy != "invite-only":
            errors.append(
                f"Prod registration policy must be 'invite-only' "
                f"(got '{s.registration_policy}')."
            )
    else:  # dev
        if has_prod_origin:
            errors.append("Dev must not allow the Prod front-end origin.")

    return errors


def validate_settings(settings: Settings) -> None:
    """Raise EnvironmentValidationError if the environment is unsafe/incoherent."""
    errors = _collect_errors(settings)
    if errors:
        raise EnvironmentValidationError(
            "Environment validation failed (fail-closed):\n  - "
            + "\n  - ".join(errors)
        )
