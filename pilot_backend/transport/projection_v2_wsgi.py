"""pilot_backend/transport/projection_v2_wsgi.py — the A2 v2 internal route.

    POST /internal/parent-baseline-projections-v2

A SECOND route on the SAME internal service as A2 v1, added as a separate app
behind a router rather than as a branch inside `ProjectionApp`.

## WHY A ROUTER AND NOT AN EDIT TO v1

`ProjectionApp` is byte-unchanged by this slice. It keeps its own body reader,
its own 8 KB cap sized for seven scalars, its own validation and its own status
mapping. A v2 branch inside it would have meant one handler with two body
contracts and two size limits, where a later edit to the shared part could change
v1's behaviour — and v1 is deployed and in use.

`InternalProjectionRouter` dispatches by path and delegates EVERYTHING that is
not the v2 route to the v1 app, including unknown paths and wrong methods. So
v1's 404 and 405 behaviour is preserved by construction rather than
reimplemented, and there is exactly one place that decides what an unroutable
request looks like.

## WHY THE SAME SERVICE AND NOT A THIRD ONE

The trust relationship is identical: the same single `roles/run.invoker` binding,
the same Parent runtime identity, the same audience, no `allUsers`, no CORS, no
end-user credential. A separate service would need a second IAM boundary and a
second deployment to describe one relationship, and would make the two
generations of one data flow diverge operationally for no security gain.

What a SEPARATE route does buy — and the reason it is not one route with a
version field in the body — is that the v1 contract cannot be reached with a v2
body or vice versa. A version field would make the first thing a handler does a
branch on attacker-supplied data.

## NO IDENTIFIERS IN THE PATH, AND NO CORS

`source_session_id` travels in the BODY. Cloud Run's request log records the
path, so a session id in the URL would be logged by the platform where this
application cannot redact it. The `-v2` suffix is a contract version, not data.

There is no CORS middleware and no allowed-origin configuration anywhere in this
app or in the entrypoint that builds it. A browser cannot use this endpoint, so
there is nothing to negotiate and nothing an origins list could widen. `OPTIONS`
falls through to 405 like any other method.

## CLOUD RUN IAM IS THE AUTHORITATIVE GATE

`verifier` may be None, and that is a SUPPORTED, DECLARED mode — `iam_only` —
not a degraded one. See `pilot_runtime/projection_server.py` for why app-level
re-verification of the same Google token is opt-in: it rests on an assumption
about which header Cloud Run delivers to the container, and making correctness
depend on that would let a correctly authorised deployment be refused by its own
application. The mode is declared at startup and never inferred.

## NOTHING HERE LOGS

No logger is constructed, no `print` is called, and no exception text is
propagated to the caller. That is a release blocker for this slice rather than a
style preference: a v2 body carries MILESTONE PROSE, so a logged request body or
a quoted parse error would put clinical content into a log this application
cannot redact afterwards.

Every external error is a constant, PHI-safe string. The response says only that
something was refused — never which field, and never a value.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Optional

from ..domain.parent_baseline_projection import (
    ProjectionIntegrityError,
    ProjectionValidationError,
)
from ..domain.parent_baseline_projection_v2 import ProjectionV2Error
from ..integration.baseline_projection_v2_service import (
    ProjectionV2ChildUnresolved,
)
from .projection_v2_request import (
    ProjectionV2RequestError,
    ProjectionV2RequestTooLarge,
    read_request,
)
from .projection_wsgi import bearer_from_environ

PROJECTION_V2_ROUTE = "/internal/parent-baseline-projections-v2"

HTTP_OK = 200
HTTP_CREATED = 201
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_METHOD_NOT_ALLOWED = 405
HTTP_PAYLOAD_TOO_LARGE = 413
HTTP_SERVER_ERROR = 500

#: WSGI requires a full status LINE, not a bare code.
_STATUS_TEXT = {
    HTTP_OK: "200 OK",
    HTTP_CREATED: "201 Created",
    HTTP_BAD_REQUEST: "400 Bad Request",
    HTTP_UNAUTHORIZED: "401 Unauthorized",
    HTTP_NOT_FOUND: "404 Not Found",
    HTTP_METHOD_NOT_ALLOWED: "405 Method Not Allowed",
    HTTP_CONFLICT: "409 Conflict",
    HTTP_PAYLOAD_TOO_LARGE: "413 Payload Too Large",
    HTTP_SERVER_ERROR: "500 Internal Server Error",
}


class ProjectionV2App:
    """WSGI app: verify the caller, then accept one v2 skill-level projection."""

    def __init__(self, *, service_factory: Callable[[], Any],
                 verifier: Any = None,
                 bearer_reader: Optional[Callable[[Mapping], Optional[str]]] = None
                 ) -> None:
        #: None means IAM-ONLY: Cloud Run is the gate and this app inspects no
        #: token. Keyword-only and explicit at every call site, so the mode is
        #: always visible where the app is built.
        self._verifier = verifier
        self._service_factory = service_factory
        self._bearer_reader = bearer_reader or bearer_from_environ

    @property
    def verifies_tokens(self) -> bool:
        """Whether app-level verification is in force. For tests and probes."""
        return self._verifier is not None

    @staticmethod
    def _respond(start_response, status: int, payload: Mapping[str, Any]):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        start_response(_STATUS_TEXT[status], [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            # No Access-Control-* header is emitted anywhere in this app.
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
        ])
        return [body]

    def __call__(self, environ, start_response):
        method = (environ.get("REQUEST_METHOD") or "").upper()
        if method != "POST":
            return self._respond(start_response, HTTP_METHOD_NOT_ALLOWED,
                                 {"error": "method not allowed"})

        # The caller first, and only when app-level verification is in force.
        # When it IS enabled it runs BEFORE the body is read, so a wrong caller
        # cannot make this process parse JSON.
        if self._verifier is not None:
            try:
                self._verifier.verify(self._bearer_reader(environ))
            except Exception:
                return self._respond(start_response, HTTP_UNAUTHORIZED,
                                     {"error": "not permitted"})

        try:
            body = read_request(environ)
        except ProjectionV2RequestTooLarge:
            return self._respond(start_response, HTTP_PAYLOAD_TOO_LARGE,
                                 {"error": "not accepted"})
        except ProjectionV2RequestError:
            return self._respond(start_response, HTTP_BAD_REQUEST,
                                 {"error": "not accepted"})

        try:
            result = self._service_factory().accept(
                source_session_id=body["source_session_id"],
                source_record_digest=body["source_record_digest"],
                summary=body["summary"],
                skills=body["skills"],
                band_totals=body["band_totals"],
            )
        except ProjectionV2ChildUnresolved:
            # 404, distinct from a shape failure: the payload was fine and the
            # session is simply not linked to a canonical child yet. The message
            # stays constant, so this does not become an oracle for which
            # sessions exist — only the status differs, and only an authenticated
            # Parent service can see it at all.
            return self._respond(start_response, HTTP_NOT_FOUND,
                                 {"error": "not accepted"})
        except ProjectionIntegrityError:
            return self._respond(start_response, HTTP_CONFLICT,
                                 {"error": "integrity conflict"})
        except (ProjectionValidationError, ProjectionV2Error):
            # Covers canonicalisation refusals and denominator refusals:
            # `SkillCanonicalisationError` is a `ProjectionV2Error`. A 400 says
            # the request was not accepted and nothing about why — the reason
            # names a clinical field, so it stays inside the process.
            return self._respond(start_response, HTTP_BAD_REQUEST,
                                 {"error": "not accepted"})
        except Exception:
            # NEVER the exception text. A canonicalisation failure could
            # otherwise quote a milestone back to the caller, and a chained
            # cause could carry one into a traceback.
            return self._respond(start_response, HTTP_SERVER_ERROR,
                                 {"error": "unavailable"})

        # The response carries NO baseline values and no evidence — only the
        # derived ids, so a caller can correlate without holding clinical
        # content.
        return self._respond(
            start_response,
            HTTP_CREATED if result.created else HTTP_OK,
            {
                "projection_id": result.projection.projection_id,
                "child_id": result.projection.child_id,
                "created": result.created,
            },
        )


class InternalProjectionRouter:
    """Dispatch the internal projection service's two versioned routes.

    The v2 route goes to the v2 app; EVERYTHING else — the v1 route, an unknown
    path, a wrong method on an unknown path — goes to the v1 app unchanged.

    Delegating the fallthrough rather than reimplementing it means v1 keeps
    ownership of what an unroutable internal request looks like, and that there
    is no second 404 in this package that could drift away from the first.
    """

    def __init__(self, *, v1_app: Any, v2_app: Any) -> None:
        self._v1 = v1_app
        self._v2 = v2_app

    @property
    def v1_app(self) -> Any:
        return self._v1

    @property
    def v2_app(self) -> Any:
        return self._v2

    @property
    def verifies_tokens(self) -> bool:
        """Whether app-level verification is in force, for the WHOLE service.

        Delegates to both apps and REFUSES to answer if they disagree. One
        declared auth mode governs this service, so a state where one route
        re-verifies the Google token and the other does not is a configuration
        fault rather than a question with an answer — and answering `True`
        because the first app said so would hide exactly the half-applied
        posture an operator needs to see.

        Exposed with the same name the v1 app uses so the entrypoint's existing
        mode assertions and the deployed auth probe keep working against the
        composed application rather than having to reach inside it.
        """
        v1 = bool(getattr(self._v1, "verifies_tokens", False))
        v2 = bool(getattr(self._v2, "verifies_tokens", False))
        if v1 != v2:
            raise ValueError(
                "the internal projection routes disagree about app-level "
                "verification; one declared mode governs the whole service")
        return v1

    def __call__(self, environ, start_response):
        path = (environ.get("PATH_INFO") or "").rstrip("/")
        if path == PROJECTION_V2_ROUTE:
            return self._v2(environ, start_response)
        return self._v1(environ, start_response)


__all__ = [
    "PROJECTION_V2_ROUTE",
    "InternalProjectionRouter",
    "ProjectionV2App",
]
