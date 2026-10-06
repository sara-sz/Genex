"""pilot_backend/transport/bootstrap_wsgi.py — the internal bootstrap app.

A THIRD WSGI application, separate from both `wsgi_app.py` (the browser API) and
`projection_wsgi.py` (the A2 baseline projection).

## WHY NOT ON THE A2 PROJECTION SERVICE

The projection service is deliberately single-purpose, and its narrowness is
asserted rather than intended: one route, a three-key body, an 8 KiB cap, no
CORS, and a create-only repository with no update or delete. Its runtime
identity holds exactly three Firestore permissions.

Identity bootstrap is a different kind of operation. It is not a copy of a
clinical result; it registers a capability that will later mint canonical
identity. Putting it on the projection service would widen a reviewed PHI
boundary, and would need Firestore access the projection identity does not and
should not have.

So they are separate services with separate runtime identities, separate
invoker bindings and separate blast radii — the same reasoning that put the
projection on its own service rather than on the browser API.

## WHY NOT ON THE BROWSER API

Registration is authenticated as the Parent SERVICE, not as a person. The
browser API is deployed with `allUsers` on `run.invoker` because Lovable clients
cannot present Google OIDC, so Cloud Run enforces nothing there and the
application is the only gate. An internet-reachable identity-registration
endpoint protected solely by application code is exactly what this slice must
not build.

## ONE ROUTE, NO IDENTIFIERS IN THE PATH

    POST /internal/parent-session-claims

The digest and the session id travel in the BODY. Cloud Run records the request
path in its own logs, where this application cannot redact it — so a session id
in the URL would be logged by the platform. Same deviation, same reason, as the
A2 projection route.

## IT NEVER RECEIVES A RAW TOKEN

The body carries a token DIGEST. The raw capability goes only to the
authenticated Parent browser, over TLS, and never to the Pilot at all — so a
compromise of this service, its logs or its storage yields no redeemable
credential. `FORBIDDEN_REGISTRATION_FIELDS` names `claim_token` explicitly so a
future caller that tried to send one is refused rather than tolerated.

## NO CORS, EVER

There is no CORS middleware and no allowed-origin configuration. A browser
cannot use this endpoint, so there is nothing to negotiate and nothing that
could be widened by editing an origins list. `OPTIONS` falls through to 405.

## CLOUD RUN IAM IS THE AUTHORITATIVE GATE

Identical posture to A2, and for the identical reason: the service has no
`allUsers` invoker and exactly one `roles/run.invoker` binding, so Cloud Run
rejects a wrong caller before a byte reaches Python. Application-level token
verification is OPTIONAL defence in depth, declared at startup, never inferred.

Errors are constant, PHI-safe strings.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Optional

from ..integration.parent_session_claim_service import (
    ClaimRegistrationConflict,
    ClaimRegistrationError,
)
from .projection_wsgi import bearer_from_environ

BOOTSTRAP_ROUTE = "/internal/parent-session-claims"

#: The only body keys accepted. `claim_token` is absent because the Pilot never
#: receives one; `child_id`, `caregiver_id` and every uid are absent because the
#: Pilot derives or refuses them.
BODY_KEYS = ("claim_digest", "source_session_id", "ttl_seconds")

#: `ttl_seconds` may be omitted — the domain default applies. Everything else is
#: required, and the exact-key-set check below enforces both directions.
OPTIONAL_BODY_KEYS = ("ttl_seconds",)

HTTP_OK = 200
HTTP_CREATED = 201
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_METHOD_NOT_ALLOWED = 405
HTTP_PAYLOAD_TOO_LARGE = 413
HTTP_SERVER_ERROR = 500

#: A registration is a digest, a session id and an integer. Anything larger is
#: not one, and refusing by length before parsing means a hostile body is never
#: decoded.
MAX_BODY_BYTES = 4 * 1024

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


class _TooLarge(ClaimRegistrationError):
    """Body exceeded `MAX_BODY_BYTES`. Separate so it maps to 413."""


class BootstrapApp:
    """WSGI app: accept one pending Parent-session claim."""

    def __init__(self, *, service_factory: Callable[[], Any],
                 verifier: Any = None,
                 bearer_reader: Optional[Callable[[Mapping], Optional[str]]] = None
                 ) -> None:
        #: None means IAM-ONLY: Cloud Run is the gate and this app performs no
        #: token inspection. Keyword-only, so the mode is visible at every call
        #: site where the app is built.
        self._verifier = verifier
        self._service_factory = service_factory
        self._bearer_reader = bearer_reader or bearer_from_environ

    @property
    def verifies_tokens(self) -> bool:
        """Whether app-level verification is in force. For tests and probes."""
        return self._verifier is not None

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
            raise ClaimRegistrationError("a body is required")
        if declared <= 0:
            raise ClaimRegistrationError("a body is required")
        if declared > MAX_BODY_BYTES:
            raise _TooLarge("body too large")
        stream = environ.get("wsgi.input")
        raw = stream.read(declared) if stream is not None else b""
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ClaimRegistrationError("a JSON object body is required")
        if not isinstance(parsed, dict):
            raise ClaimRegistrationError("a JSON object body is required")
        unexpected = set(parsed) - set(BODY_KEYS)
        required = set(BODY_KEYS) - set(OPTIONAL_BODY_KEYS)
        if unexpected or required - set(parsed):
            # Exact key set, allowing only the declared optional keys to be
            # absent. An extra top-level key is refused rather than ignored.
            raise ClaimRegistrationError("unexpected body shape")
        return parsed

    # -- the application --------------------------------------------------

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO") or ""
        method = (environ.get("REQUEST_METHOD") or "").upper()

        if path.rstrip("/") != BOOTSTRAP_ROUTE:
            return self._respond(start_response, HTTP_NOT_FOUND,
                                 {"error": "not found"})
        if method != "POST":
            return self._respond(start_response, HTTP_METHOD_NOT_ALLOWED,
                                 {"error": "method not allowed"})

        # The caller — but only when app-level verification is in force. In
        # `iam_only` the header is not read at all: Cloud Run has already
        # rejected every caller but the one permitted service account.
        #
        # When verification IS enabled it runs BEFORE the body is read, so a
        # wrong caller cannot make this process parse JSON.
        if self._verifier is not None:
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
        except ClaimRegistrationError:
            return self._respond(start_response, HTTP_BAD_REQUEST,
                                 {"error": "not accepted"})

        try:
            result = self._service_factory().register(body)
        except ClaimRegistrationConflict:
            return self._respond(start_response, HTTP_CONFLICT,
                                 {"error": "integrity conflict"})
        except ClaimRegistrationError:
            return self._respond(start_response, HTTP_BAD_REQUEST,
                                 {"error": "not accepted"})
        except Exception:
            # Never the exception text: it could quote a session id.
            return self._respond(start_response, HTTP_SERVER_ERROR,
                                 {"error": "unavailable"})

        # The response echoes NO digest and NO session id — only whether the
        # claim was created and when it expires, which is what Parent needs to
        # tell the browser how long it has.
        return self._respond(
            start_response,
            HTTP_CREATED if result.created else HTTP_OK,
            {
                "registered": True,
                "created": result.created,
                "expires_at": result.claim.expires_at.isoformat(),
            },
        )
