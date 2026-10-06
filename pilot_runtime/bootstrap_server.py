"""pilot_runtime/bootstrap_server.py — the internal bootstrap entrypoint.

A THIRD entrypoint, deployed as its own Cloud Run service. It is neither the
browser-facing Pilot API nor the A2 projection service, and must never become
either.

## IT DOES NOT BUILD THE BROWSER RUNTIME

`build_runtime` assembles the whole browser application: the Firebase token
decoder, the CORS policy, the audit recorder, the child-context service and the
routed WSGI app. This entrypoint builds NONE of that — only the document store
and the repositories.

That is a security property. This service has no end-user credential and no
end-user routes, so binding a Firebase decoder here would create an
authentication path nothing needs and a future edit could reach a caregiver
route through. There is no `CorsMiddleware` either: its absence is what makes a
browser request impossible rather than merely unauthorised.

## IT DOES NOT IMPORT THE COMPOSITION ROOT

`pilot_runtime.composition` imports the Firebase decoder at module level, so
reaching `build_store` through it would pull `firebase_admin` into this
service's import graph — and this image deliberately does not install it. The
Firestore store is constructed directly from
`pilot_runtime.persistence.firestore_store`, which has no Firebase dependency.

Exactly the trap A2 hit and fixed; the same two tests pin it here.

## IT IS NOT THE PROJECTION SERVICE

Separate image, separate runtime identity, separate invoker binding. The A2
projection service stays single-purpose and frozen: this slice adds nothing to
it, and its runtime identity is deliberately NOT reused, because this service
needs transactional writes across four collections that the projection identity
has no business holding.

## CLOUD RUN IAM IS AUTHORITATIVE; APP VERIFICATION IS OPTIONAL

`PILOT_BOOTSTRAP_AUTH_MODE` is REQUIRED and has NO DEFAULT. Two values:

    iam_only          Cloud Run IAM is the gate. This app inspects no token.
    iam_plus_token    IAM is still the gate, AND the app re-verifies.

The A2 activation PROVED, with a live probe, that Cloud Run forwards
`Authorization` intact and that the forwarded value stays independently
verifiable with the expected audience and caller email. So `iam_plus_token` is
known to work here rather than merely hoped to — but `iam_only` remains the
declared default posture, because the authoritative gate is still IAM and this
service should not fail closed on a credential it does not need to inspect.

`PILOT_BOOTSTRAP_AUDIENCE` and `PILOT_BOOTSTRAP_CALLER` are required ONLY in
`iam_plus_token`. Demanding them in `iam_only` would imply a verification that
is not happening.

## FAIL CLOSED AT STARTUP

The mode is declared, never inferred. PROD is refused outright: this slice is
the fictional staging pairing only, and production gets its own service, its own
identity and its own review after PRE-PHI approval.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional

from pilot_backend.config.settings import PilotSettings
from pilot_backend.integration.parent_session_claim_service import (
    ParentSessionClaimRegistrationService,
)
from pilot_backend.persistence import FirestoreRepositories
from pilot_backend.transport.bootstrap_wsgi import BootstrapApp
from pilot_runtime.google_oidc import GoogleServiceIdentityVerifier
from pilot_runtime.persistence.firestore_store import (
    FirestoreDocumentStore,
    build_firestore_client,
    emulator_host_from,
)

AUDIENCE_ENV_VAR = "PILOT_BOOTSTRAP_AUDIENCE"
CALLER_ENV_VAR = "PILOT_BOOTSTRAP_CALLER"
AUTH_MODE_ENV_VAR = "PILOT_BOOTSTRAP_AUTH_MODE"

#: Cloud Run IAM is the gate; this app inspects no token. The supported default
#: posture for the fictional staging deployment.
AUTH_MODE_IAM_ONLY = "iam_only"
#: Cloud Run IAM is still the gate, AND the app re-verifies the Google token.
#: Proven viable by the A2 deployed probe; opt-in all the same.
AUTH_MODE_IAM_PLUS_TOKEN = "iam_plus_token"

AUTH_MODES = (AUTH_MODE_IAM_ONLY, AUTH_MODE_IAM_PLUS_TOKEN)


class BootstrapConfigError(RuntimeError):
    """The bootstrap service is not configured to start. PHI-safe."""

    PHI_SAFE_MESSAGE = True


def build_bootstrap_application(env: Optional[Mapping[str, str]] = None, *,
                                firestore_client: Any = None,
                                verifier: Any = None):
    """Assemble the bootstrap application or raise. No degraded mode."""
    env = os.environ if env is None else env

    # The auth mode is DECLARED, never inferred and never defaulted. An operator
    # has to say which model is in force, so the code can never silently drop
    # verification and can never silently depend on it.
    mode = (env.get(AUTH_MODE_ENV_VAR) or "").strip()
    if mode not in AUTH_MODES:
        raise BootstrapConfigError(
            f"{AUTH_MODE_ENV_VAR} must be one of {AUTH_MODES}; there is no "
            f"default, because the two modes rest on different guarantees")

    audience = (env.get(AUDIENCE_ENV_VAR) or "").strip()
    caller = (env.get(CALLER_ENV_VAR) or "").strip()
    if mode == AUTH_MODE_IAM_PLUS_TOKEN:
        if not audience:
            raise BootstrapConfigError(
                f"{AUDIENCE_ENV_VAR} is required in {AUTH_MODE_IAM_PLUS_TOKEN}; "
                f"without it the service could accept a token minted for a "
                f"different audience")
        if not caller:
            raise BootstrapConfigError(
                f"{CALLER_ENV_VAR} is required in {AUTH_MODE_IAM_PLUS_TOKEN}; "
                f"without it the service could accept any Google service "
                f"identity")

    settings = PilotSettings.from_env(env)
    if settings.environment.is_prod:
        # 0.5F-A3 is the fictional staging pairing. Production needs its own
        # service, its own caller identity and PRE-PHI approval.
        raise BootstrapConfigError(
            "the bootstrap service is not approved for production")

    # Built DIRECTLY rather than through `pilot_runtime.composition.build_store`,
    # which imports the Firebase decoder at module level — see the docstring.
    client = firestore_client or build_firestore_client(
        project_id=settings.gcp_project_id,
        database=settings.firestore_database,
        emulator_host=emulator_host_from(env))
    repos = FirestoreRepositories(FirestoreDocumentStore(client))

    if verifier is not None:
        identity = verifier
    elif mode == AUTH_MODE_IAM_PLUS_TOKEN:
        identity = GoogleServiceIdentityVerifier(
            audience=audience, expected_service_account=caller)
    else:
        # iam_only. None is the explicit, declared posture — Cloud Run has
        # already refused every caller but the one permitted service account.
        identity = None

    # A factory, not an instance: the service holds no cross-request state and
    # building it per request keeps it that way by construction.
    return BootstrapApp(
        verifier=identity,
        service_factory=lambda: ParentSessionClaimRegistrationService(
            repos=repos))


def __getattr__(name: str):
    """Resolve `pilot_runtime.bootstrap_server:application` on first access.

    PEP 562, the same pattern the other two entrypoints use and for the same
    reason: assembling at import time would make a configuration error surface
    as an import failure during collection, where the traceback is least
    readable.
    """
    if name == "application":
        return build_bootstrap_application()
    raise AttributeError(name)
