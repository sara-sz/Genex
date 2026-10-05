"""pilot_runtime/projection_server.py — the internal projection entrypoint.

A SECOND entrypoint, deployed as its own Cloud Run service. It is not the
browser-facing Pilot API and must never become it.

## IT DOES NOT BUILD THE BROWSER RUNTIME

`build_runtime` assembles the whole browser application: the Firebase token
decoder, the CORS policy, the audit recorder, the child-context service and the
routed WSGI app. This entrypoint builds NONE of that. It takes the document
store and the repositories, and nothing else.

That is a security property, not an optimisation. The projection service has no
end-user credential and no end-user routes, so binding a Firebase decoder here
would create an authentication path that nothing needs and that a future edit
could reach a caregiver route through. There is no `CorsMiddleware` either:
`build_wsgi_application` wraps the browser app in one, and its absence here is
what makes a browser request impossible rather than merely unauthorised.

A test asserts this module's import graph never reaches `CorsMiddleware` or the
browser `WsgiApplication`, and that `pilot_runtime.server`'s graph never
reaches the projection app. Neither artifact can become the other.

## FAIL CLOSED AT STARTUP

Three variables are required and none has a default:

    PILOT_PROJECTION_AUDIENCE          the exact audience tokens must carry
    PILOT_PROJECTION_CALLER            the one service account permitted
    PILOT_GCP_PROJECT_ID / database    via the existing settings loader

## IT DOES NOT IMPORT THE COMPOSITION ROOT EITHER

`pilot_runtime.composition` imports the Firebase decoder at module level, so
reaching `build_store` through it would pull `firebase_admin` into this
service's import graph — and this image deliberately does not install it. The
Firestore store is therefore constructed directly from
`pilot_runtime.persistence.firestore_store`, which has no Firebase dependency
at all.

A missing audience or caller raises before the first request. An
`allUsers`-reachable service that accepted anyone would be the worst possible
failure, so an unconfigured deployment refuses to start instead of starting
permissively.

PROD is refused outright. This slice is the fictional staging pairing only, and
production gets its own service, its own identity and its own review after
PRE-PHI approval — so a prod environment here is a misconfiguration, not a
mode.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional

from pilot_backend.config.settings import PilotSettings
from pilot_backend.integration.baseline_projection_service import (
    BaselineProjectionService,
)
from pilot_backend.persistence import FirestoreRepositories
from pilot_backend.transport.projection_wsgi import ProjectionApp
from pilot_runtime.google_oidc import GoogleServiceIdentityVerifier
from pilot_runtime.persistence.firestore_store import (
    FirestoreDocumentStore,
    build_firestore_client,
    emulator_host_from,
)

AUDIENCE_ENV_VAR = "PILOT_PROJECTION_AUDIENCE"
CALLER_ENV_VAR = "PILOT_PROJECTION_CALLER"


class ProjectionConfigError(RuntimeError):
    """The projection service is not configured to start. PHI-safe."""

    PHI_SAFE_MESSAGE = True


def build_projection_application(env: Optional[Mapping[str, str]] = None, *,
                                 firestore_client: Any = None,
                                 verifier: Any = None):
    """Assemble the projection application or raise. No degraded mode."""
    env = os.environ if env is None else env

    audience = (env.get(AUDIENCE_ENV_VAR) or "").strip()
    caller = (env.get(CALLER_ENV_VAR) or "").strip()
    if not audience:
        raise ProjectionConfigError(
            f"{AUDIENCE_ENV_VAR} is required; without it the service could "
            f"accept a token minted for a different audience")
    if not caller:
        raise ProjectionConfigError(
            f"{CALLER_ENV_VAR} is required; without it the service could "
            f"accept any Google service identity")

    settings = PilotSettings.from_env(env)
    if settings.environment.is_prod:
        # 0.5F-A2 is the fictional staging pairing. Production needs its own
        # service, its own caller identity and PRE-PHI approval, so a prod
        # environment reaching this code means something is pointed wrong.
        raise ProjectionConfigError(
            "the projection service is not approved for production")

    # The store is built DIRECTLY rather than through
    # `pilot_runtime.composition.build_store`.
    #
    # That is not a shortcut: `composition` imports the Firebase decoder at
    # module level, so importing it would drag `firebase_admin` into this
    # service's import graph — and the projection image deliberately does not
    # install it, because no end-user authentication exists here. Routing
    # through composition would have made the image fail at import in
    # production, which the Dockerfile's own in-build assertion caught.
    client = firestore_client or build_firestore_client(
        project_id=settings.gcp_project_id,
        database=settings.firestore_database,
        emulator_host=emulator_host_from(env))
    repos = FirestoreRepositories(FirestoreDocumentStore(client))

    identity = verifier or GoogleServiceIdentityVerifier(
        audience=audience, expected_service_account=caller)

    # A factory, not an instance: the service holds no cross-request state and
    # building it per request keeps it that way by construction.
    return ProjectionApp(verifier=identity,
                         service_factory=lambda: BaselineProjectionService(
                             repos=repos))


def __getattr__(name: str):
    """Resolve `pilot_runtime.projection_server:application` on first access.

    PEP 562, the same pattern `pilot_runtime.server` uses and for the same
    reason: assembling at import time would make a configuration error surface
    as an import failure during collection, where the traceback is least
    readable.
    """
    if name == "application":
        return build_projection_application()
    raise AttributeError(name)
