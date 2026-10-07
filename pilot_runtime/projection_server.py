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

## CLOUD RUN IAM IS AUTHORITATIVE; APP VERIFICATION IS OPTIONAL

`PILOT_PROJECTION_AUTH_MODE` is REQUIRED and has NO DEFAULT. Two values:

    iam_only          Cloud Run IAM is the gate. This app inspects no token.
    iam_plus_token    IAM is still the gate, AND the app re-verifies.

The distinction matters because app-level re-verification rests on an
assumption about what Cloud Run delivers to the container. Depending on header
handling — `Authorization` versus `X-Serverless-Authorization` — the value the
container sees may not remain independently signature-verifiable. If
correctness depended on that, a correctly authorised deployment could be
refused by its own application.

So `iam_plus_token` is OPT-IN and valid only once the deployed header
behaviour has been PROVEN by `pilot_runtime/deploy/probe_projection_auth.sh`.
Until then `iam_only` is the supported posture, and it is not the weaker one:
the service has no `allUsers` invoker and exactly one `roles/run.invoker`
binding, so a wrong caller never reaches Python.

`PILOT_PROJECTION_AUDIENCE` and `PILOT_PROJECTION_CALLER` are required ONLY in
`iam_plus_token`. Demanding them in `iam_only` would imply a verification that
is not happening.

## IT DOES NOT IMPORT THE COMPOSITION ROOT EITHER

`pilot_runtime.composition` imports the Firebase decoder at module level, so
reaching `build_store` through it would pull `firebase_admin` into this
service's import graph — and this image deliberately does not install it. The
Firestore store is therefore constructed directly from
`pilot_runtime.persistence.firestore_store`, which has no Firebase dependency
at all.

## FAIL CLOSED AT STARTUP

The mode is declared, never inferred. An absent or unrecognised mode raises
before the first request, so a deployment cannot start in an undeclared
posture.

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
from pilot_backend.integration.baseline_projection_v2_service import (
    BaselineProjectionV2Service,
)
from pilot_backend.persistence import FirestoreRepositories
from pilot_backend.transport.projection_v2_wsgi import (
    InternalProjectionRouter,
    ProjectionV2App,
)
from pilot_backend.transport.projection_wsgi import ProjectionApp
from pilot_runtime.google_oidc import GoogleServiceIdentityVerifier
from pilot_runtime.integration.static_rung_source import build_static_rung_source
from pilot_runtime.persistence.firestore_store import (
    FirestoreDocumentStore,
    build_firestore_client,
    emulator_host_from,
)

AUDIENCE_ENV_VAR = "PILOT_PROJECTION_AUDIENCE"
CALLER_ENV_VAR = "PILOT_PROJECTION_CALLER"
AUTH_MODE_ENV_VAR = "PILOT_PROJECTION_AUTH_MODE"

#: Cloud Run IAM is the gate; this app inspects no token. The supported
#: default posture for the fictional staging deployment.
AUTH_MODE_IAM_ONLY = "iam_only"
#: Cloud Run IAM is still the gate, AND the app re-verifies the Google token.
#: Only valid once the deployed header behaviour has been PROVEN by the auth
#: probe — see pilot_runtime/deploy/probe_projection_auth.sh.
AUTH_MODE_IAM_PLUS_TOKEN = "iam_plus_token"

AUTH_MODES = (AUTH_MODE_IAM_ONLY, AUTH_MODE_IAM_PLUS_TOKEN)


class ProjectionConfigError(RuntimeError):
    """The projection service is not configured to start. PHI-safe."""

    PHI_SAFE_MESSAGE = True


def build_projection_application(env: Optional[Mapping[str, str]] = None, *,
                                 firestore_client: Any = None,
                                 verifier: Any = None):
    """Assemble the projection application or raise. No degraded mode."""
    env = os.environ if env is None else env

    # The auth mode is DECLARED, never inferred and never defaulted. An
    # operator has to say which model is in force, so the code can never
    # silently drop verification and can never silently depend on it.
    mode = (env.get(AUTH_MODE_ENV_VAR) or "").strip()
    if mode not in AUTH_MODES:
        raise ProjectionConfigError(
            f"{AUTH_MODE_ENV_VAR} must be one of {AUTH_MODES}; there is no "
            f"default, because the two modes rest on different guarantees")

    audience = (env.get(AUDIENCE_ENV_VAR) or "").strip()
    caller = (env.get(CALLER_ENV_VAR) or "").strip()
    if mode == AUTH_MODE_IAM_PLUS_TOKEN:
        # Only required in the mode that uses them. Requiring them in
        # `iam_only` would imply the app was verifying when it is not.
        if not audience:
            raise ProjectionConfigError(
                f"{AUDIENCE_ENV_VAR} is required in {AUTH_MODE_IAM_PLUS_TOKEN}; "
                f"without it the service could accept a token minted for a "
                f"different audience")
        if not caller:
            raise ProjectionConfigError(
                f"{CALLER_ENV_VAR} is required in {AUTH_MODE_IAM_PLUS_TOKEN}; "
                f"without it the service could accept any Google service "
                f"identity")

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

    if verifier is not None:
        identity = verifier
    elif mode == AUTH_MODE_IAM_PLUS_TOKEN:
        identity = GoogleServiceIdentityVerifier(
            audience=audience, expected_service_account=caller)
    else:
        # iam_only. None is the explicit, declared posture — Cloud Run has
        # already refused every caller but the one permitted service account.
        identity = None

    # 0.6A-1F. The frozen rung table, loaded ONCE at startup rather than per
    # request: it is a read-only static artifact, and `build_static_rung_source`
    # raises rather than returning a degraded source — so a missing or corrupt
    # artifact refuses the deployment at startup instead of failing the first
    # projection. The v2 boundary cannot canonicalise without it, and a service
    # that accepted identities it could not verify would be worse than one that
    # does not start.
    #
    # This is a LOOKUP-ONLY adapter over a static JSON file. It imports no
    # spreadsheet reader, no Parent package and no model client; the CI drift
    # gate regenerates the artifact from the frozen source and compares.
    rung_source = build_static_rung_source()

    # Factories, not instances: neither service holds cross-request state, and
    # building per request keeps it that way by construction.
    v1_app = ProjectionApp(verifier=identity,
                           service_factory=lambda: BaselineProjectionService(
                               repos=repos))
    v2_app = ProjectionV2App(
        verifier=identity,
        service_factory=lambda: BaselineProjectionV2Service(
            repos=repos, rung_source=rung_source))

    # The router delegates every non-v2 path to the v1 app, so v1's route,
    # validation, size cap, status mapping and 404/405 behaviour are unchanged by
    # this slice. Both apps share the one declared auth mode: there is no path on
    # this service that is verified while another is not.
    return InternalProjectionRouter(v1_app=v1_app, v2_app=v2_app)


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
