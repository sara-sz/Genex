"""A2 v2.1 — the sibling internal route.

    POST /internal/parent-baseline-projections-v21

An EXPLICIT version boundary. The router dispatches on the path, so a v2 body
posted to the v2 route is still read by the v2 decoder and a v2.1 body posted
to the v2 route is refused — rather than either being guessed at from its
shape.

## IT WRITES TWO RECORDS, DELIBERATELY

The clinical projection goes to the existing v2 service, unchanged, so a v2.1
submission produces a projection that is byte-identical to what the v2 route
would have produced from the same evidence. The derived clinician profile goes
to its OWN collection.

That is the safety property, not tidiness. The projection is the document goal
generation reads; profile state inside it would hand the generator a legitimate
reference to profile state, and the structural proof in
`test_goal_generation_eligibility_boundary` could not be stated.

## PARENT'S CONTENT EVALUATION IS NOT STORED HERE

It is provenance for a reconciliation that happens at GENERATION time, against
the Pilot's own activity bank. Persisting it would create a second, stale
record of a content state — exactly the stored-derived-state mistake the
Parent side avoided by never writing `projection_withheld`. It is accepted,
validated, and passed to the caller in the response for correlation.

## THE PROFILE WRITE CANNOT FAIL THE PROJECTION

If the projection succeeds and the profile write fails, the response says so
and the projection stands. A clinician losing derived context is recoverable by
re-projecting; a lost projection is not, and making the clinical record
contingent on an informational one would be the wrong dependency.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Optional

from ..domain.parent_clinician_profile_v21 import (
    ClinicianProfileV21,
    ClinicianProfileV21Error,
)
from ..repository.interface import DuplicateRecord
from .projection_v21_request import (
    PROJECTION_V21_PATH,
    ProjectionV2RequestTooLarge,
    ProjectionV21RequestError,
    read_request_v21,
)
from .projection_v2_wsgi import (
    _STATUS_TEXT,
    HTTP_BAD_REQUEST,
    HTTP_CONFLICT,
    HTTP_CREATED,
    HTTP_METHOD_NOT_ALLOWED,
    HTTP_NOT_FOUND,
    HTTP_OK,
    HTTP_PAYLOAD_TOO_LARGE,
    HTTP_SERVER_ERROR,
    HTTP_UNAUTHORIZED,
    bearer_from_environ,
)

#: Asserted equal to the router's constant in
#: `test_projection_v21_route`, so the codec's path and the router's path
#: cannot drift.
PROJECTION_V21_ROUTE = PROJECTION_V21_PATH


class ProjectionV21App:
    """WSGI app: verify the caller, accept one v2.1 projection plus profile."""

    def __init__(self, *, service_factory: Callable[[], Any],
                 profile_repository_factory: Callable[[], Any],
                 verifier: Any = None,
                 bearer_reader: Optional[
                     Callable[[Mapping], Optional[str]]] = None) -> None:
        #: None means IAM-ONLY, exactly as in v2: Cloud Run is the gate and
        #: this app inspects no token.
        self._verifier = verifier
        self._service_factory = service_factory
        self._profiles = profile_repository_factory
        self._bearer_reader = bearer_reader or bearer_from_environ

    @property
    def verifies_tokens(self) -> bool:
        return self._verifier is not None

    @staticmethod
    def _respond(start_response, status: int, payload: Mapping[str, Any]):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        start_response(_STATUS_TEXT[status], [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
        ])
        return [body]

    def __call__(self, environ, start_response):
        method = (environ.get("REQUEST_METHOD") or "").upper()
        if method != "POST":
            return self._respond(start_response, HTTP_METHOD_NOT_ALLOWED,
                                 {"error": "method not allowed"})

        if self._verifier is not None:
            try:
                self._verifier.verify(self._bearer_reader(environ))
            except Exception:
                return self._respond(start_response, HTTP_UNAUTHORIZED,
                                     {"error": "not permitted"})

        try:
            body = read_request_v21(environ)
        except ProjectionV2RequestTooLarge:
            return self._respond(start_response, HTTP_PAYLOAD_TOO_LARGE,
                                 {"error": "not accepted"})
        except ProjectionV21RequestError:
            return self._respond(start_response, HTTP_BAD_REQUEST,
                                 {"error": "not accepted"})
        except Exception:
            # The v2 decoder's own errors reach here; same constant body.
            return self._respond(start_response, HTTP_BAD_REQUEST,
                                 {"error": "not accepted"})

        # --- the clinical projection, through the UNCHANGED v2 service ----
        try:
            result = self._service_factory().accept(
                source_session_id=body["source_session_id"],
                source_record_digest=body["source_record_digest"],
                summary=body["summary"],
                skills=body["skills"],
                band_totals=body["band_totals"],
            )
        except Exception as exc:
            return self._respond(start_response,
                                 *_classify_projection_failure(exc))

        # --- the derived profile, into its OWN collection -----------------
        #
        # After the projection, because the projection is what resolves the
        # canonical child and the profile links to the same one. A failure here
        # does NOT fail the projection: see this module's docstring.
        profile_stored = False
        try:
            profile = ClinicianProfileV21(
                child_id=result.projection.child_id,
                source_session_id=body["source_session_id"],
                source_record_digest=body["source_record_digest"],
                domain=result.projection.domain,
                outcome=body["clinician_profile"]["outcome"],
                areas=body["clinician_profile"]["areas"],
                not_assessed_causes=body["clinician_profile"][
                    "not_assessed_causes"],
                multi_area=body["clinician_profile"]["multi_area"],
                multi_area_indeterminate_reasons=tuple(
                    body["clinician_profile"][
                        "multi_area_indeterminate_reasons"]),
            )
            self._profiles().create(profile)
            profile_stored = True
        except DuplicateRecord:
            # An exact retry. The stored profile is derived from the same
            # immutable source, so it is the same profile.
            profile_stored = True
        except (ClinicianProfileV21Error, Exception):
            profile_stored = False

        return self._respond(
            start_response,
            HTTP_CREATED if result.created else HTTP_OK,
            {
                "projection_id": result.projection.projection_id,
                "child_id": result.projection.child_id,
                "created": result.created,
                "clinician_profile_stored": profile_stored,
                # Echoed for correlation, NEVER stored: it is provenance for a
                # reconciliation that happens at generation time.
                "parent_content_digest": body["parent_content_evaluation"][
                    "content_digest"],
                "parent_target_serviceability": body[
                    "parent_content_evaluation"]["target_serviceability"],
            },
        )


def _classify_projection_failure(exc: Exception):
    """The v2 app's own status mapping, reused rather than restated."""
    from ..domain.parent_baseline_projection import (
        ProjectionIntegrityError,
        ProjectionValidationError,
    )
    from ..domain.parent_baseline_projection_v2 import ProjectionV2Error
    from ..integration.baseline_projection_v2_service import (
        ProjectionV2ChildUnresolved,
    )

    if isinstance(exc, ProjectionV2ChildUnresolved):
        return HTTP_NOT_FOUND, {"error": "not accepted"}
    if isinstance(exc, ProjectionIntegrityError):
        return HTTP_CONFLICT, {"error": "integrity conflict"}
    if isinstance(exc, (ProjectionValidationError, ProjectionV2Error)):
        return HTTP_BAD_REQUEST, {"error": "not accepted"}
    return HTTP_SERVER_ERROR, {"error": "unavailable"}


__all__ = ["PROJECTION_V21_ROUTE", "ProjectionV21App"]
