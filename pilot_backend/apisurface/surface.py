"""pilot_backend/apisurface/surface.py — what is public, and what CORS allows.

## Why there is no web framework here

This module is transport-agnostic on purpose, and the reason is worth stating
because "add FastAPI" would have been the obvious move.

  * The contract asks for infrastructure, not product. Inventing routes to
    demonstrate authentication would mean inventing product surface — and
    every route invented now is one that has to be secured, versioned and
    later removed.
  * All the behaviour that actually needs proving — 401 vs 403, deny by
    default, forged input ignored, revoked relationship denying — is decided
    before any HTTP concern. Proving it at the HTTP layer would test the
    framework's dependency injection more than this system's policy.
  * The pilot test suite runs on a dependency-pure CI job with no third-party
    packages installed. Security behaviour must be provable there, not in a
    job that could be skipped.

`RouteGuard` is the whole integration contract: a transport asks it whether a
path is public, and if not, hands it the bearer header and the target child id
and receives an `AccessDecision` carrying the status code to return. Mounting
this under FastAPI later is a handful of lines and changes no policy.

## Public surface is an allowlist of exact paths

`PUBLIC_ROUTES` is a frozenset of exact strings, not a prefix rule. A prefix
rule is how `/health` quietly becomes `/health/db-dump`. Everything not listed
is protected — including any route added later, which is the direction the
default has to fail.

There is no debug endpoint and no admin endpoint. The Parent service's
`ADMIN_DEBUG`-gated route is a reasonable pattern, but the safest version of a
conditional debug endpoint is still one that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

from ..authz.decisions import AccessDecision, Denial
from ..authz.policy import authenticate_and_authorize_child

#: Every unauthenticated path. Exact match only.
PUBLIC_ROUTES = frozenset({"/health"})


class SurfaceError(ValueError):
    """An unsafe API surface configuration."""

    PHI_SAFE_MESSAGE = True


def is_public_route(path: str) -> bool:
    """Exact-match membership. Not a prefix test — see the module docstring."""
    return (path or "") in PUBLIC_ROUTES


def health_payload(settings) -> Mapping[str, str]:
    """The only unauthenticated response body.

    Liveness and environment name. No project ids, no database name, no
    version, no origins, no dependency status — an unauthenticated caller
    learns that the service is up and nothing about what it is made of.
    """
    return {"status": "ok", "environment": settings.environment.value}


@dataclass(frozen=True)
class CorsPolicy:
    """A validated CORS allowlist."""

    allowed_origins: Tuple[str, ...]
    allow_credentials: bool = True

    def permits(self, origin: str) -> bool:
        return origin in self.allowed_origins


def cors_policy_for(settings) -> CorsPolicy:
    """Build the CORS policy, refusing anything unsafe for the environment.

    Production rules are enforced in `PilotSettings` at construction — wildcard
    origins, non-https origins and third-party preview hosts all prevent the
    service from starting. Re-checked here so a hand-built settings object
    cannot route around it.
    """
    origins = tuple(settings.allowed_origins)
    if settings.environment.is_prod:
        if not origins:
            raise SurfaceError("prod requires an explicit CORS allowlist")
        for origin in origins:
            if origin == "*":
                raise SurfaceError("prod CORS must not use a wildcard origin")
            if not origin.startswith("https://"):
                raise SurfaceError("prod CORS origins must be https")
    return CorsPolicy(allowed_origins=origins)


class RouteGuard:
    """The single entry point a transport layer calls.

    Holds the verifier, repositories and (optionally) an audit recorder, so a
    transport cannot assemble a partial version of the check.
    """

    def __init__(self, *, verifier, repos, recorder=None) -> None:
        self._verifier = verifier
        self._repos = repos
        self._recorder = recorder

    def guard_child_route(self, path: str, bearer: Optional[str], child_id: str, *,
                          request_id: str = "") -> AccessDecision:
        """Authenticate, authorize and audit one child-scoped request.

        A public path reaching here is a routing bug, not an access grant: it
        is refused rather than waved through, because a child-scoped operation
        has no business being public.
        """
        if is_public_route(path):
            raise SurfaceError(f"child-scoped route must not be public: {path}")

        decision = authenticate_and_authorize_child(
            bearer, child_id, verifier=self._verifier, repos=self._repos)

        if self._recorder is not None:
            self._recorder.record_access_decision(decision, request_id=request_id)
        return decision

    @staticmethod
    def public_reason_for(denial: Optional[Denial]) -> str:
        """Caller-facing text. Constant per class of failure."""
        if denial is None:
            return "ok"
        return AccessDecision.deny(denial).public_reason
