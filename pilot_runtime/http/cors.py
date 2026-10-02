"""CORS for the deployed pilot service.

The frozen `PilotWSGIApplication` emits no CORS headers and has no `OPTIONS`
handler — deliberately, because 0.1 through 0.5C were proven over direct HTTP
where neither is needed. A browser app cannot call it until something at the
edge answers preflight and echoes an allowed origin, so that something lives
here rather than in the frozen transport.

## What this must NOT become

A CORS layer is a tempting place to put an authorization decision, and it must
never hold one. CORS is a *browser* mechanism: it tells a compliant browser
which origin may read a response. It stops nothing else — curl, a server-side
fetch and a non-compliant client all ignore it entirely. So this middleware
makes exactly one decision (may this origin read the response) and delegates
every other question to the frozen application, which still runs its full
authentication and authorization on the real request.

Two consequences are deliberate:

  * A preflight is answered WITHOUT consulting the application, because a
    browser sends no `Authorization` header on a preflight and the frozen app
    would correctly refuse it. Answering here does not widen the authenticated
    surface — the actual request that follows is authenticated normally.

  * The preflight response is IDENTICAL for every path, including paths that
    do not exist. A per-path preflight would turn this middleware into a route
    oracle, which is precisely the non-enumerating property 0.5A/0.5B/0.5C
    went to such lengths to preserve. `/pilot/children/xyz/goals` and
    `/pilot/nonsense` preflight the same way.

## The allowlist

`CorsPolicy.permits` is an exact string match against the configured origins —
no scheme coercion, no suffix matching, no wildcard. An unlisted origin gets a
response with no CORS headers at all, which the browser then refuses to expose
to the page. That is the correct failure: the request is not blocked by us, it
is simply not readable by the caller.

`Vary: Origin` is always set when an `Origin` header was present, so a shared
cache can never serve one origin's permitted response to another origin.
"""

from __future__ import annotations

from typing import Callable, Iterable, List, Mapping, Tuple

#: How long a browser may cache a preflight. Ten minutes is long enough to
#: keep preflight traffic negligible and short enough that an origin removed
#: from the allowlist stops being usable the same morning.
PREFLIGHT_MAX_AGE_SECONDS = 600

#: Methods the frozen 0.5C surface actually serves. GET and POST are the only
#: two in the route table; OPTIONS is here because preflight asks for it.
_ALLOWED_METHODS = "GET, POST, OPTIONS"

#: Request headers a browser may send. `Authorization` carries the Firebase ID
#: token and `Content-Type` is needed for `application/json` bodies. Nothing
#: else is advertised — a client asking to send a custom header is told no.
_ALLOWED_HEADERS = "Authorization, Content-Type"


class CorsMiddleware:
    """Wraps a WSGI application with an exact-match origin allowlist."""

    def __init__(self, application: Callable, *, policy) -> None:
        if application is None:
            raise ValueError("CorsMiddleware requires an application")
        if policy is None:
            raise ValueError("CorsMiddleware requires a CorsPolicy")
        self._application = application
        self._policy = policy

    # -- helpers ------------------------------------------------------------

    def _permitted_origin(self, environ: Mapping[str, object]) -> str:
        """The request's origin if it is allowlisted, else the empty string."""
        origin = str(environ.get("HTTP_ORIGIN") or "").strip()
        if not origin:
            return ""
        return origin if self._policy.permits(origin) else ""

    def _cors_headers(self, origin: str) -> List[Tuple[str, str]]:
        headers = [("Access-Control-Allow-Origin", origin),
                   ("Vary", "Origin")]
        if getattr(self._policy, "allow_credentials", False):
            # Required for the browser to send the Firebase ID token on a
            # cross-origin request. Safe only BECAUSE the origin is an exact
            # allowlist match — `*` with credentials is rejected by browsers
            # and would be wrong here anyway.
            headers.append(("Access-Control-Allow-Credentials", "true"))
        return headers

    # -- WSGI ---------------------------------------------------------------

    def __call__(self, environ, start_response) -> Iterable[bytes]:
        origin = self._permitted_origin(environ)
        method = str(environ.get("REQUEST_METHOD") or "").upper()

        if method == "OPTIONS":
            return self._preflight(origin, start_response)

        def _start(status, headers, exc_info=None):
            merged = list(headers)
            if origin:
                # Replace rather than append, so a future transport that began
                # setting its own CORS header could not produce two.
                merged = [(name, value) for name, value in merged
                          if name.lower() not in {"access-control-allow-origin",
                                                  "access-control-allow-credentials"}]
                merged.extend(self._cors_headers(origin))
            elif str(environ.get("HTTP_ORIGIN") or "").strip():
                # An origin was present but not allowlisted. Still vary, so a
                # cache cannot hand this response to a permitted origin.
                merged.append(("Vary", "Origin"))
            return start_response(status, merged, exc_info)

        return self._application(environ, _start)

    def _preflight(self, origin: str, start_response) -> Iterable[bytes]:
        """Answer a preflight identically for every path.

        A disallowed origin gets 403 with NO CORS headers. The status is
        irrelevant to the browser — absent headers already fail the check —
        but it keeps the server's logs honest about what happened.
        """
        if not origin:
            start_response("403 Forbidden",
                           [("Content-Length", "0"), ("Vary", "Origin")])
            return [b""]

        headers = self._cors_headers(origin) + [
            ("Access-Control-Allow-Methods", _ALLOWED_METHODS),
            ("Access-Control-Allow-Headers", _ALLOWED_HEADERS),
            ("Access-Control-Max-Age", str(PREFLIGHT_MAX_AGE_SECONDS)),
            ("Content-Length", "0"),
        ]
        start_response("204 No Content", headers)
        return [b""]
