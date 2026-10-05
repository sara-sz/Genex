"""pilot_backend/transport/projection_wsgi.py — the internal projection app.

A SEPARATE WSGI application from `wsgi_app.py`, not a route added to it.

The browser-facing Pilot API is deployed with `allUsers` on `run.invoker`
because Lovable clients cannot present Google OIDC. That means Cloud Run
enforces nothing there: the header is forwarded and the application is the only
gate. Putting an internal endpoint on that service would make it reachable from
the internet, protected solely by this code. So the projection lives on its own
service, which has NO `allUsers` and a single `roles/run.invoker` binding —
Cloud Run refuses the wrong caller before a byte reaches Python, and the
verification here is defence in depth.

## ONE ROUTE, NO IDENTIFIERS IN THE PATH

    POST /internal/parent-baseline-projections

`source_session_id` travels in the BODY, not the path. Cloud Run's request log
records the path, so a session id in the URL would be logged by the platform
where this application cannot redact it. That is the opposite of the Parent
API's `/session/{id}/...` convention, and the deviation is the point.

## NO CORS, EVER

There is no CORS middleware here and no allowed-origin configuration. A
browser cannot use this endpoint, so there is nothing to negotiate — and
nothing that could be widened by editing an origins list. `OPTIONS` is not
handled; it falls through to 405 like any other method.

## WHAT IT WILL NOT DO

No end-user authentication, no `resolve_principal`, no caregiver or provider
role. The only identity is the calling service account. A Firebase end-user
token fails the issuer check in `GoogleServiceIdentityVerifier` and can never
authorize anything here.

Errors are constant, PHI-safe strings. A projection body is clinical content,
so the response says only that something was refused — never which field, and
never a value.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Optional

from ..domain.parent_baseline_projection import (
    ProjectionIntegrityError,
    ProjectionValidationError,
)
from ..integration.baseline_projection_service import (
    ProjectionChildUnresolved,
)

PROJECTION_ROUTE = "/internal/parent-baseline-projections"


def bearer_from_environ(environ: Mapping[str, Any]) -> Optional[str]:
    """The service-identity header, preferring `Authorization`.

    Defined HERE rather than in `pilot_runtime` because it is pure header
    reading with no SDK involved, and `pilot_backend` must never import the
    runtime layer — the dependency only points the other way.

    `X-Serverless-Authorization` is the fallback Cloud Run populates when a
    service reserves `Authorization` for an end-user credential. The
    projection service has no end-user credential, so `Authorization` is the
    normal case; the fallback exists only so a future ingress change cannot
    silently strand the token.
    """
    primary = environ.get("HTTP_AUTHORIZATION")
    if isinstance(primary, str) and primary.strip():
        return primary
    fallback = environ.get("HTTP_X_SERVERLESS_AUTHORIZATION")
    if isinstance(fallback, str) and fallback.strip():
        return fallback
    return None

#: The only body keys accepted. `child_id`, `projection_id`, `source_system`
#: and `projected_at` are absent because the Pilot derives them; sending one
#: is refused by the projection validator, not ignored here.
BODY_KEYS = ("source_session_id", "source_record_digest", "projection")

HTTP_OK = 200
HTTP_CREATED = 201
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_METHOD_NOT_ALLOWED = 405
HTTP_PAYLOAD_TOO_LARGE = 413
HTTP_SERVER_ERROR = 500

#: A projection is seven small fields. Anything larger is not a projection, and
#: refusing by length before parsing means a hostile body is never decoded.
MAX_BODY_BYTES = 8 * 1024

#: WSGI requires a full status LINE, not a bare code — same table the browser
#: transport keeps, restated here because this app shares none of its routing.
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


class _TooLarge(ProjectionValidationError):
    """Body exceeded `MAX_BODY_BYTES`. Separate so it maps to 413."""


class ProjectionApp:
    """WSGI app: verify the caller, then accept one projection."""

    def __init__(self, *, verifier: Any, service_factory: Callable[[], Any],
                 bearer_reader: Optional[Callable[[Mapping], Optional[str]]] = None
                 ) -> None:
        self._verifier = verifier
        self._service_factory = service_factory
        self._bearer_reader = bearer_reader or bearer_from_environ

    # -- helpers ----------------------------------------------------------

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

    def _read_body(self, environ: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            declared = int(environ.get("CONTENT_LENGTH") or 0)
        except (TypeError, ValueError):
            raise ProjectionValidationError("a body is required")
        if declared <= 0:
            raise ProjectionValidationError("a body is required")
        if declared > MAX_BODY_BYTES:
            raise _TooLarge("body too large")
        stream = environ.get("wsgi.input")
        raw = stream.read(declared) if stream is not None else b""
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ProjectionValidationError("a JSON object body is required")
        if not isinstance(parsed, dict):
            raise ProjectionValidationError("a JSON object body is required")
        unexpected = set(parsed) - set(BODY_KEYS)
        if unexpected or set(BODY_KEYS) - set(parsed):
            # Exact key set. An extra top-level key is refused rather than
            # ignored for the same reason the projection allowlist is strict.
            raise ProjectionValidationError("unexpected body shape")
        return parsed

    # -- the application --------------------------------------------------

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO") or ""
        method = (environ.get("REQUEST_METHOD") or "").upper()

        if path.rstrip("/") != PROJECTION_ROUTE:
            return self._respond(start_response, HTTP_NOT_FOUND,
                                 {"error": "not found"})
        if method != "POST":
            return self._respond(start_response, HTTP_METHOD_NOT_ALLOWED,
                                 {"error": "method not allowed"})

        # 1. The caller, before the body is even read. An unauthenticated or
        #    wrong caller must not be able to make this process parse JSON.
        try:
            self._verifier.verify(self._bearer_reader(environ))
        except Exception:
            return self._respond(start_response, HTTP_UNAUTHORIZED,
                                 {"error": "not permitted"})

        try:
            body = self._read_body(environ)
        except _TooLarge:
            return self._respond(start_response, HTTP_PAYLOAD_TOO_LARGE,
                                 {"error": "not accepted"})
        except ProjectionValidationError:
            return self._respond(start_response, HTTP_BAD_REQUEST,
                                 {"error": "not accepted"})

        try:
            result = self._service_factory().accept(
                source_session_id=body.get("source_session_id") or "",
                source_record_digest=body.get("source_record_digest") or "",
                projection=body.get("projection") or {},
            )
        except ProjectionChildUnresolved:
            # 404, distinct from a shape failure: the payload was fine and the
            # session is simply not linked to a canonical child yet. The
            # message stays constant, so this does not become an oracle for
            # which sessions exist — only the status differs, and only an
            # authenticated Parent service can see it at all.
            return self._respond(start_response, HTTP_NOT_FOUND,
                                 {"error": "not accepted"})
        except ProjectionIntegrityError:
            return self._respond(start_response, HTTP_CONFLICT,
                                 {"error": "integrity conflict"})
        except ProjectionValidationError:
            return self._respond(start_response, HTTP_BAD_REQUEST,
                                 {"error": "not accepted"})
        except Exception:
            # Never the exception text: a projection failure could otherwise
            # quote a milestone or a status back to the caller.
            return self._respond(start_response, HTTP_SERVER_ERROR,
                                 {"error": "unavailable"})

        # The response carries NO baseline values — only the derived ids, so a
        # caller can log or correlate without holding clinical content.
        return self._respond(
            start_response,
            HTTP_CREATED if result.created else HTTP_OK,
            {
                "projection_id": result.projection.projection_id,
                "child_id": result.projection.child_id,
                "created": result.created,
            },
        )
