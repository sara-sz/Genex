"""WSGI entrypoint for the FICTIONAL-DATA staging deployment of 0.5C.

This module exists because `build_runtime`'s dev path is deliberately
permissive — correctly so, for a laptop. On a laptop, a missing
`PILOT_GCP_PROJECT_ID` should not stop you working, so `build_store` falls
back to a `pilot-dev` project literal and `build_token_decoder` returns None,
which leaves the verifier failing closed. Both are sensible locally and both
are wrong for a service a browser can reach: the first would write to a
project nobody chose, and the second would serve a 401 to every request while
looking perfectly healthy.

So the fail-closed rule the deployment needs is enforced HERE, at the edge,
rather than by tightening `pilot_backend/config/settings.py` — which is frozen
product logic and must not change to suit a deployment.

## What this refuses to start with

    missing any of the five required variables
    PILOT_ENVIRONMENT=prod            - production needs its own reviewed
                                        entrypoint, not the fictional one
    PILOT_DEV_AUTH_ENABLED truthy     - staging uses REAL Firebase tokens
    a Firestore emulator configured   - or inherited from the environment
    a Parent session bucket           - this service reaches no Parent
                                        2.3 storage at all
    an empty or non-https origin      - stricter than dev settings require
    a fail-closed verifier            - which would mean the Firebase binding
                                        silently did not happen

Every one of these is a configuration mistake that would otherwise produce a
service that looks up and is wrong. Refusing at import means Cloud Run's
revision never goes healthy, which is the loud failure we want.

## Identity is still server-derived

Nothing here participates in deciding who the caller is. The container sets no
claim, injects no decoder and holds no credential: `ApplicationDefault` picks
up the runtime service account, and role comes from the Firestore identity
records exactly as it does in the proven suites. `_FORWARDED_CLAIMS` in the
decoder does not forward custom claims, so a token cannot assert a role.
"""

from __future__ import annotations

import os
from typing import Mapping

from pilot_backend.auth.verifiers import FailClosedAuthVerifier
from pilot_runtime.composition import CompositionError, build_runtime
from pilot_runtime.http import CorsMiddleware
from pilot_runtime.integration.parent_gcs_source import BUCKET_ENV_VAR

#: Absent or blank, any one of these stops the service from starting.
REQUIRED_VARIABLES = (
    "PILOT_ENVIRONMENT",
    "PILOT_GCP_PROJECT_ID",
    "PILOT_FIREBASE_PROJECT_ID",
    "PILOT_FIRESTORE_DATABASE",
    "PILOT_ALLOWED_ORIGINS",
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class StagingConfigError(Exception):
    """The staging service was asked to start with unsafe configuration.

    PHI-safe: names the variable at fault and never its value, so a
    misconfiguration is diagnosable from logs without the logs becoming the
    place a secret or an identifier leaks.
    """

    PHI_SAFE_MESSAGE = True


def _require(env: Mapping[str, str]) -> None:
    missing = [name for name in REQUIRED_VARIABLES
               if not (env.get(name) or "").strip()]
    if missing:
        raise StagingConfigError(
            "staging refuses to start without: " + ", ".join(sorted(missing)))

    environment = (env.get("PILOT_ENVIRONMENT") or "").strip().lower()
    if environment == "prod":
        raise StagingConfigError(
            "this entrypoint serves the fictional staging environment only; "
            "PILOT_ENVIRONMENT=prod requires a separately reviewed entrypoint")
    if environment not in {"dev", "test"}:
        raise StagingConfigError(
            "PILOT_ENVIRONMENT must be dev or test for this entrypoint")

    if (env.get("PILOT_DEV_AUTH_ENABLED") or "").strip().lower() in _TRUTHY:
        raise StagingConfigError(
            "staging must use real Firebase tokens; "
            "PILOT_DEV_AUTH_ENABLED must not be set")

    for variable in ("PILOT_FIRESTORE_EMULATOR_HOST", "FIRESTORE_EMULATOR_HOST"):
        if (env.get(variable) or "").strip():
            raise StagingConfigError(
                f"staging must not talk to an emulator ({variable} is set)")

    if (env.get(BUCKET_ENV_VAR) or "").strip():
        raise StagingConfigError(
            f"staging must not reach Parent 2.3 storage ({BUCKET_ENV_VAR} is set)")

    origins = [part.strip() for part
               in (env.get("PILOT_ALLOWED_ORIGINS") or "").split(",")
               if part.strip()]
    if not origins:
        raise StagingConfigError("PILOT_ALLOWED_ORIGINS names no origin")
    for origin in origins:
        if origin == "*":
            raise StagingConfigError("staging CORS must not use a wildcard origin")
        if not origin.startswith("https://"):
            # Stricter than dev settings. A browser app served over https
            # cannot call an http origin anyway, so an http entry here is
            # always either a mistake or a downgrade.
            raise StagingConfigError(
                "staging CORS origins must be https (one entry is not)")


def build_wsgi_application(env: Mapping[str, str] = None):
    """Assemble the staging application or raise. No degraded mode."""
    env = os.environ if env is None else env
    _require(env)

    try:
        runtime = build_runtime(env, process_env=env)
    except CompositionError:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised as PHI-safe
        if getattr(exc, "PHI_SAFE_MESSAGE", False):
            raise
        raise StagingConfigError("staging runtime assembly failed") from None

    if isinstance(runtime.verifier, FailClosedAuthVerifier):
        # Reachable when the Firebase binding did not happen. Without this the
        # service would start, pass its health check, and 401 every real
        # request — the failure mode hardest to diagnose from the frontend.
        raise StagingConfigError(
            "the Firebase token decoder was not bound; every request would be "
            "refused. Check PILOT_FIREBASE_PROJECT_ID and the runtime "
            "service account's credentials")

    return CorsMiddleware(runtime.application, policy=runtime.cors)


def __getattr__(name: str):
    """Resolve `pilot_runtime.server:application` on first access.

    PEP 562, for one specific reason. Assigning `application` at module level
    would build the runtime as a side effect of IMPORTING this module, which
    makes it impossible to unit-test the guards above without a live Firestore
    and a service account — the tests would have to not import the module they
    are testing.

    Resolving it on attribute access keeps both properties:

        gunicorn asks for `application` while loading, under `--preload`, in
        the master process before any worker forks and before any request is
        served. A `StagingConfigError` there kills the master, so the Cloud Run
        revision never becomes healthy — still a startup failure, still loud.

        a test can `from pilot_runtime.server import _require` and never touch
        `application` at all.
    """
    if name == "application":
        return build_wsgi_application()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
