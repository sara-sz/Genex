"""pilot_backend/integration/baseline_projection_service.py — accept a projection.

The Pilot-side half of the A2 trust boundary. Called only by the internal
projection transport, which has already established that the caller is the
authenticated Parent staging service.

## WHAT THIS SERVICE TRUSTS, AND WHAT IT DOES NOT

TRUSTED, because only an authenticated Parent service can assert it and the
Pilot has no way to check it: the SOURCE PROVENANCE — which Parent session
this is, and the digest of the finalized record it came from.

NOT TRUSTED, and independently validated here: the SHAPE. The seven-field
allowlist, the domain, the status vocabulary, the month ranges. A well-formed
caller and a well-formed payload are different claims.

NEVER ACCEPTED AT ALL: `child_id`, `projection_id`, `source_system`,
`projected_at`. The Pilot derives every one. `child_id` in particular is
resolved only through the existing `SourceSystemLink`, so even the
authenticated Parent service cannot name a child — it can only name a session
it owns, and the Pilot decides which canonical child that is.

## CHILD RESOLUTION IS THE ONLY JOIN

    SourceSystemLink(source_system=PARENT, external_id=<source_session_id>)
        -> child_id

`list_for_external_id` returns ACTIVE links only, and
`(source_system, external_id)` is a write-time uniqueness constraint, so the
expected result is exactly zero or one. Both of the other cases are refused:

  zero  -> the session was never linked to a canonical child. Refuse; there is
           nothing to attach a projection to, and inventing a Child here would
           create a clinical record from a message.
  many  -> a uniqueness constraint that is enforced at write time has been
           violated. That is an integrity failure, not an ambiguity to
           resolve by picking one.

`external_owner_ref` is never read. It is provenance only — an owner handle is
not an identity — and this module does not reference the field at all.

## IDEMPOTENCY VERSUS INTEGRITY

These are different outcomes and the difference matters:

  same session + same domain + SAME digest
      -> the existing projection, unchanged. The deterministic
         `projection_id` means the retry addresses the same document, so this
         is cheap and needs no write.

  same session + same domain + DIFFERENT digest
      -> `ProjectionIntegrityError`. An A1 finalized baseline is immutable, so
         two digests cannot both be it. Versioning would record a history the
         source does not have, and overwriting would destroy evidence. Fail
         closed and let a human look.

## WHAT THIS SERVICE MUST NOT TOUCH

No `GoalSuggestion`, no `ClinicalGoal`, no anchor, no monthly plan, no weekly
cycle. It writes exactly one record type. A test walks this module's AST and
asserts none of those repositories is even named here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

from ..domain.parent_baseline_projection import (
    ParentBaselineProjection,
    ProjectionIntegrityError,
    ProjectionValidationError,
    projection_id_for,
    validate_projection_payload,
)
from ..domain.source_link import SourceSystem


class ProjectionChildUnresolved(ProjectionValidationError):
    """No ACTIVE Parent link maps this session to a canonical child.

    A `ProjectionValidationError` subclass so the transport's PHI-safe
    handling covers it by inheritance, but named separately so the service's
    own tests can tell "unlinked session" from "malformed payload".
    """


class ProjectionLinkAmbiguous(ProjectionIntegrityError):
    """More than one ACTIVE Parent link for this session.

    Should be unreachable: `(source_system, external_id)` is enforced at
    write time. Reachable only if that constraint was bypassed, which is
    exactly when failing closed matters most.
    """


@dataclass(frozen=True)
class ProjectionResult:
    """The outcome, with enough detail for the transport to pick a status.

    `created` is False for an idempotent replay. The transport maps that to
    200 rather than 201, so a retrying caller can tell the difference without
    the service having to know about HTTP.
    """

    projection: ParentBaselineProjection
    created: bool


class BaselineProjectionService:
    """Accept one Parent baseline projection. One public method, one write."""

    def __init__(self, repos: Any, *,
                 now: Optional[Callable[[], datetime]] = None) -> None:
        self._repos = repos
        self._now = now

    def _stamp(self) -> Optional[datetime]:
        return self._now() if self._now is not None else None

    def _resolve_child(self, source_session_id: str) -> str:
        """The canonical child for this Parent session, or a refusal.

        Reads `external_id` and `source_system` only. The caller's claimed
        child, if it somehow sent one, has already been refused by payload
        validation — there is no code path here that could consult it.
        """
        links = [
            link for link in
            self._repos.source_links.list_for_external_id(source_session_id)
            if link.source_system is SourceSystem.PARENT
        ]
        if not links:
            raise ProjectionChildUnresolved(
                "no canonical child for this source session")
        if len(links) > 1:
            raise ProjectionLinkAmbiguous(
                "more than one active link for this source session")
        return links[0].child_id

    def accept(self, *, source_session_id: str, source_record_digest: str,
               projection: Mapping[str, Any]) -> ProjectionResult:
        """Validate, resolve, and create-or-replay. The only write path.

        Order is deliberate: SHAPE first, then identity, then integrity, then
        the write. Validating first means a malformed payload never causes a
        repository read, so a caller cannot use validation failures to probe
        which sessions exist.
        """
        session_id = (source_session_id or "").strip()
        if not session_id:
            raise ProjectionValidationError("a source session id is required")
        # Lowercase hex EXACTLY, not normalised. `hashlib.hexdigest()` is
        # already lowercase, so anything else did not come from the canonical
        # digest function and should be refused rather than coerced into
        # matching.
        digest = (source_record_digest or "").strip()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ProjectionValidationError(
                "source_record_digest must be a sha256 hex digest")

        validated = validate_projection_payload(projection)
        domain = validated["domain"]

        # --- idempotent replay, checked BEFORE resolving a child ----------
        #
        # An exact retry should cost nothing and must not depend on the link
        # still being active: a projection already written is already written.
        wanted_id = projection_id_for(session_id, domain, digest)
        existing = self._repos.parent_baseline_projections.find(wanted_id)
        if existing is not None:
            return ProjectionResult(projection=existing, created=False)

        # --- integrity: a different digest for an immutable source --------
        #
        # Checked before the write and before child resolution, so a conflict
        # is reported as a conflict rather than as whatever the write would
        # have failed with.
        for other in self._repos.parent_baseline_projections.list_for_source(
                session_id, domain):
            if other.source_record_digest != digest:
                raise ProjectionIntegrityError(
                    "a projection exists for this source with a different "
                    "digest; the source baseline is immutable")

        child_id = self._resolve_child(session_id)

        record = ParentBaselineProjection.build(
            child_id=child_id,
            source_session_id=session_id,
            source_record_digest=digest,
            projection=validated,
            now=self._stamp(),
        )
        # A create-only repository. If two callers race, the loser's create
        # collides on the deterministic id and raises — which is the correct
        # outcome for an immutable record, not something to retry around.
        return ProjectionResult(
            projection=self._repos.parent_baseline_projections.create(record),
            created=True,
        )
