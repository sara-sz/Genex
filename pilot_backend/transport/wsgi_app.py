"""pilot_backend/transport/wsgi_app.py — the composition proof, over real HTTP.

## What this is, and what it is not

This is COMPOSITION ASSURANCE. It exists to prove that a real HTTP request
cannot reach a repository without passing through token extraction,
verification, revocation semantics, identity resolution, server-derived role
and relationship authorization — in that order, with no bypass.

It is NOT the product API. It has two routes. It returns no clinical content.
Adding a product endpoint here would be adding product scope, so nothing is
added here that a workflow would need.

## Why WSGI from the standard library, and not FastAPI

The repo's convention is FastAPI — Parent `api/` and `therapist_api` both use
it, and the app-consumer CI job pins `fastapi==0.135.3`. It was still the wrong
choice here, for one decisive reason: the CI job that runs every pilot security
test installs `pytest`, `pandas` and `openpyxl` and nothing else. A FastAPI
proof would have to live in the other job, separated from the suite it is
meant to complete, and the composition gate could then pass while the security
gate was skipped — exactly the "skipped downstream gate" failure this project
has already been bitten by once.

`wsgiref` is in the standard library. That buys:

  * a genuine HTTP boundary — real sockets, real request lines, real headers,
    real status codes — which the tests exercise through `urllib`, not through
    an in-process test client that could mask a transport-layer mistake;
  * zero new dependency, so the proof runs beside the rest of the security
    suite in the dependency-pure job;
  * no throwaway work: WSGI is a standard interface, so this application
    mounts under gunicorn directly, or under Starlette/FastAPI via
    `WSGIMiddleware`, when a production transport is chosen.

"Smallest reasonable option" was taken literally.

## Deny by default at the routing layer too

`ROUTE_TABLE` is an explicit list. An unregistered path is 404 and is never
public — there is no prefix rule, no catch-all, and no fallthrough that could
let a future route be served before anyone remembers to protect it. The
protected route's handler is never even constructed unless authorization
returned an allow.

## Responses carry no internal detail

Every failure renders a constant body for its status class. Exceptions are
caught at the boundary and rendered as a bare 500 with no type, message or
traceback — an unexpected error is exactly when a vendor exception is most
likely to be holding a document fragment.
"""

from __future__ import annotations

import json
import uuid
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from ..apisurface.surface import health_payload, is_public_route
from ..audit.events import AuditAction, AuditResult
from ..authz.decisions import HTTP_FORBIDDEN, HTTP_OK, HTTP_UNAUTHORIZED
from ..authz.policy import authenticate_and_authorize_child
from ..observability.safe_logging import format_log, render

#: The only two paths this application serves.
PUBLIC_HEALTH_ROUTE = "/health"
PROTECTED_CHILD_ROUTE = "/pilot/children/{child_id}/access-check"

#: (method, template, is_public). Explicit; no prefix matching anywhere.
ROUTE_TABLE: Tuple[Tuple[str, str, bool], ...] = (
    ("GET", PUBLIC_HEALTH_ROUTE, True),
    ("GET", PROTECTED_CHILD_ROUTE, False),
)

_STATUS_TEXT = {
    HTTP_OK: "200 OK",
    HTTP_UNAUTHORIZED: "401 Unauthorized",
    HTTP_FORBIDDEN: "403 Forbidden",
    404: "404 Not Found",
    405: "405 Method Not Allowed",
    500: "500 Internal Server Error",
}

#: Constant bodies. A response body never varies with the reason for refusal,
#: so it cannot become an oracle for which child ids exist.
_ERROR_BODIES = {
    HTTP_UNAUTHORIZED: {"error": "authentication required"},
    HTTP_FORBIDDEN: {"error": "not permitted"},
    404: {"error": "not found"},
    405: {"error": "method not allowed"},
    500: {"error": "internal error"},
}


def _match_child_route(path: str) -> Optional[str]:
    """Return the child id if `path` is the protected route, else None.

    Hand-matched rather than regex-routed so the shape is obvious: exactly four
    segments, the fixed ones exact, and the id segment non-empty. A trailing
    segment, an extra segment or a different prefix does not match and
    therefore 404s rather than falling through to a handler.
    """
    parts = [p for p in path.split("/") if p != ""]
    if len(parts) != 4:
        return None
    if parts[0] != "pilot" or parts[1] != "children" or parts[3] != "access-check":
        return None
    return parts[2]


class PilotWSGIApplication:
    """A two-route WSGI application wired to the BACKEND 0.2 components.

    Holds the verifier, repositories, settings and recorder. A handler cannot
    assemble a partial security chain because it never receives the pieces —
    it receives only the finished `AccessDecision`.
    """

    def __init__(self, *, settings, verifier, repos, recorder=None,
                 log_sink: Optional[List[str]] = None) -> None:
        self._settings = settings
        self._verifier = verifier
        self._repos = repos
        self._recorder = recorder
        #: Tests capture emitted lines here. Production would hand these to a
        #: logging handler; either way they pass through `format_log` first,
        #: so an unsafe field raises rather than being written.
        self.log_sink: List[str] = log_sink if log_sink is not None else []

    # -- WSGI entry point ---------------------------------------------------

    def __call__(self, environ: Mapping[str, object],
                 start_response: Callable) -> Iterable[bytes]:
        request_id = str(environ.get("HTTP_X_REQUEST_ID") or uuid.uuid4().hex)
        try:
            status_code, payload, route_template = self._route(environ, request_id)
        except Exception:
            # Boundary catch-all. Nothing about the exception is rendered or
            # logged — not the type, not the message, not a traceback. An
            # unexpected failure is precisely when third-party exception text
            # is most likely to be carrying request or document content.
            status_code, payload, route_template = 500, _ERROR_BODIES[500], "unrouted"
            self._log(request_id, route_template, str(environ.get("REQUEST_METHOD", "")),
                      status_code, None)

        body = json.dumps(payload).encode("utf-8")
        start_response(_STATUS_TEXT[status_code], [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            ("X-Request-Id", request_id),
            # Defensive headers for a JSON API that must never be framed or
            # sniffed into something executable.
            ("X-Content-Type-Options", "nosniff"),
            ("Cache-Control", "no-store"),
        ])
        return [body]

    # -- routing ------------------------------------------------------------

    def _route(self, environ: Mapping[str, object],
               request_id: str) -> Tuple[int, Mapping, str]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "") or "/")

        if path == PUBLIC_HEALTH_ROUTE:
            if method != "GET":
                return 405, _ERROR_BODIES[405], PUBLIC_HEALTH_ROUTE
            self._log(request_id, PUBLIC_HEALTH_ROUTE, method, HTTP_OK, None)
            return HTTP_OK, dict(health_payload(self._settings)), PUBLIC_HEALTH_ROUTE

        child_id = _match_child_route(path)
        if child_id is not None:
            if method != "GET":
                return 405, _ERROR_BODIES[405], PROTECTED_CHILD_ROUTE
            return self._handle_protected(environ, child_id, request_id)

        # Unregistered. Not public, not served, and no hint that it is neither.
        self._log(request_id, "unrouted", method, 404, None)
        return 404, _ERROR_BODIES[404], "unrouted"

    # -- the protected chain ------------------------------------------------

    def _handle_protected(self, environ: Mapping[str, object], child_id: str,
                          request_id: str) -> Tuple[int, Mapping, str]:
        """Steps 2-11 of the required composition order.

        Note what is NOT read: the request body, the query string, and every
        header other than `Authorization` and `X-Request-Id`. A forged `uid`,
        `role`, `caregiver_id`, `provider_id` or `beta_access_code` is not
        rejected — it is never looked at, because nothing here has a reason to
        parse it.
        """
        # 2. Bearer token extracted from the verified transport header only.
        bearer = environ.get("HTTP_AUTHORIZATION")

        # 3-8. Verify, apply revocation semantics, resolve the application
        # identity, derive the role server-side, and check the child-scoped
        # relationship. One call, so a caller cannot perform half of it.
        decision = authenticate_and_authorize_child(
            bearer if isinstance(bearer, str) else None,
            child_id,
            verifier=self._verifier,
            repos=self._repos,
        )

        # 11. Audit before the response is shaped, for grants and refusals alike.
        if self._recorder is not None:
            self._recorder.record_access_decision(decision, request_id=request_id)

        self._log(request_id, PROTECTED_CHILD_ROUTE, "GET",
                  decision.status_code, decision)

        if not decision.allowed:
            return (decision.status_code,
                    _ERROR_BODIES[decision.status_code],
                    PROTECTED_CHILD_ROUTE)

        # 9. The repository operation runs ONLY on the allow path, and exactly
        # once. The single read of the child record happens inside
        # `authorize_child_access`, AFTER the relationship is proven — so a
        # refused request causes no lookup of the requested id at all, which a
        # counting repository asserts in the tests.
        #
        # An earlier revision re-read the child here to build the response.
        # That was a second read of a record authorization had already
        # fetched: in Firestore a duplicate billed document read on every
        # authorized request, and a second point at which the record could
        # have changed between the check and the use. The identifier the
        # response needs is already on the decision, so the read was pure
        # redundancy. It was found by asserting the expected read COUNT rather
        # than merely that a read had occurred.
        #
        # 10. Minimal non-PHI proof: identifiers and a boolean. `Child` carries
        # no clinical field, and nothing beyond its id is returned.
        return HTTP_OK, {
            "authorized": True,
            "child_id": decision.child_id,
            "actor_role": decision.principal.role.value,
            "request_id": request_id,
        }, PROTECTED_CHILD_ROUTE

    # -- logging ------------------------------------------------------------

    def _log(self, request_id: str, route_template: str, method: str,
             status: int, decision) -> None:
        """Emit one validated line. `format_log` raises on an unsafe field.

        `route_template` is always a template constant from this module, never
        the populated `PATH_INFO`, so a child id cannot reach a log line
        through the path. The query string is never logged at all.
        """
        self.log_sink.append(render(format_log(
            request_id=request_id,
            event="http_request",
            route=route_template,
            method=method,
            status=status,
            environment=self._settings.environment.value,
            denial_reason=(decision.denial.value
                           if decision is not None and decision.denial else None),
            actor_id=(decision.principal.application_id
                      if decision is not None and decision.principal else None),
            actor_role=(decision.principal.role.value
                        if decision is not None and decision.principal else None),
        )))


def build_application(*, settings, repos, verifier, recorder=None,
                      log_sink: Optional[List[str]] = None) -> PilotWSGIApplication:
    """Composition root for the proof.

    Takes an already-built verifier rather than constructing one, so the
    transport layer has no say in how authentication is configured — that
    decision stays in `auth.build_verifier`, where the prod/dev rules live.
    """
    return PilotWSGIApplication(settings=settings, verifier=verifier, repos=repos,
                                recorder=recorder, log_sink=log_sink)


def route_templates() -> Dict[str, bool]:
    """Diagnostics/tests: template -> is_public. Must agree with `apisurface`."""
    table = {template: public for _, template, public in ROUTE_TABLE}
    for template, public in table.items():
        # The two modules must not be able to disagree about what is public.
        assert public == is_public_route(template), template
    return table
