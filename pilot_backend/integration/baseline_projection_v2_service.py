"""pilot_backend/integration/baseline_projection_v2_service.py — accept A2 v2.

The Pilot-side half of the v2 trust boundary. Called only by the internal
projection transport, which has already established that the caller is the
authenticated Parent service.

A SEPARATE service from v1's. `BaselineProjectionService` is not modified, not
subclassed and not called from here: the two accept different payloads, write
different record types into different collections, and share only the child
resolution RULE, which is restated rather than inherited because inheriting it
would couple two trust boundaries that must be able to change independently.

## WHAT THIS SERVICE TRUSTS, AND WHAT IT DOES NOT

TRUSTED, because only an authenticated Parent service can assert it and the
Pilot has no way to check it: the SOURCE PROVENANCE — which Parent session this
is, and the digest of the finalized record it came from.

NOT TRUSTED, and independently verified here:

  * the SHAPE — the request codec's exact key sets, bounded sizes, strict state
    enum and duplicate detection;
  * the IDENTITY of every skill — resolved through the frozen rung table, not
    accepted from the payload. Parent cannot name a `rung_ref` and does not try;
  * the DENOMINATOR — 0.6A-1F verifies Parent's `total_skills` against the
    canonical declared-track roster, in both directions. A well-formed caller and
    a true claim are different things.

NEVER ACCEPTED AT ALL: `child_id`, `projection_id`, `source_system`,
`projected_at`, `projection_schema`. The Pilot derives every one. `child_id` in
particular is resolved only through the existing `SourceSystemLink`, so even the
authenticated Parent service cannot name a child — it can only name a session it
owns, and the Pilot decides which canonical child that is.

## WHY THE CHECK ORDER IS WHAT IT IS

    1. digest format                    pure
    2. canonicalise + verify            pure, uses the frozen artifact
    3. idempotent replay                repository read
    4. integrity conflict               repository read
    5. resolve the child                repository read
    6. build and create                 the single write

Everything decidable without persistence happens FIRST. That is v1's rule and it
matters for the same reason: if a malformed payload could cause a repository
read, a caller could use validation failures to probe which sessions exist.

Step 2 before step 3 costs a little work on an exact replay, and buys that a
request which is not a valid projection never touches the store at all.

## IDEMPOTENCY VERSUS INTEGRITY

Different outcomes, and the difference matters:

  same session + same domain + SAME digest
      -> the existing projection, unchanged, `created=False`. The deterministic
         id means the retry addresses the same document, so this is cheap and
         needs no write.

  same session + same domain + DIFFERENT digest
      -> `ProjectionIntegrityError`. A finalized Parent v2 baseline is immutable,
         so two digests cannot both be it. Versioning would record a history the
         source does not have, and overwriting would destroy evidence a clinical
         decision may already rest on. Fail closed and let a human look.

## WHAT THIS SERVICE MUST NOT TOUCH

No `GoalSuggestion`, no `ClinicalGoal`, no anchor, no monthly plan, no weekly
cycle, and NOT the v1 projection collection. It writes exactly one record type. A
test walks this module's AST and asserts none of those repositories is even
named here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from ..domain.parent_baseline_projection import (
    ProjectionIntegrityError,
    ProjectionValidationError,
)
from ..domain.parent_baseline_projection_v2 import (
    ParentBaselineProjectionV2,
    projection_v2_id_for,
)
from ..domain.source_link import SourceSystem
from .baseline_skill_projection import (
    SkillCanonicalisationError,
    canonicalise_and_verify,
)


class ProjectionV2ChildUnresolved(ProjectionValidationError):
    """No ACTIVE Parent link maps this session to a canonical child.

    A `ProjectionValidationError` subclass so the transport's PHI-safe handling
    covers it by inheritance, but named separately so this service's tests can
    tell "unlinked session" from "malformed payload".
    """


class ProjectionV2LinkAmbiguous(ProjectionIntegrityError):
    """More than one ACTIVE Parent link for this session.

    Should be unreachable: `(source_system, external_id)` is enforced at write
    time. Reachable only if that constraint was bypassed, which is exactly when
    failing closed matters most.
    """


@dataclass(frozen=True)
class ProjectionV2Result:
    """The outcome, with enough detail for the transport to pick a status.

    `created` is False for an idempotent replay. The transport maps that to 200
    rather than 201, so a retrying caller can tell the difference without this
    service having to know about HTTP.
    """

    projection: ParentBaselineProjectionV2
    created: bool


class BaselineProjectionV2Service:
    """Accept one v2 skill-level projection. One public method, one write."""

    def __init__(self, repos: Any, *, rung_source: Any,
                 now: Optional[Callable[[], datetime]] = None) -> None:
        #: The frozen rung table. REQUIRED and keyword-only: a projection service
        #: with no canonical source could only accept identities it was handed,
        #: which is the one thing this boundary exists to refuse. There is no
        #: default and no degraded mode.
        if rung_source is None:
            raise ValueError(
                "a v2 projection service requires a canonical rung source")
        self._repos = repos
        self._rung_source = rung_source
        self._now = now

    def _stamp(self) -> Optional[datetime]:
        return self._now() if self._now is not None else None

    def _resolve_child(self, source_session_id: str) -> str:
        """The canonical child for this Parent session, or a refusal.

        Reads `external_id` and `source_system` only. `external_owner_ref` is
        never read — an owner handle is not an identity, and this method does not
        reference the field at all.

        `list_for_external_id` returns ACTIVE links only, and
        `(source_system, external_id)` is a write-time uniqueness constraint, so
        the expected result is exactly zero or one. Both other cases refuse:

          zero -> the session was never linked to a canonical child. There is
                  nothing to attach a projection to, and inventing a Child here
                  would create a clinical record out of a message.
          many -> a constraint enforced at write time has been violated. That is
                  an integrity failure, not an ambiguity to resolve by picking.
        """
        links = [
            link for link in
            self._repos.source_links.list_for_external_id(source_session_id)
            if link.source_system is SourceSystem.PARENT
        ]
        if not links:
            raise ProjectionV2ChildUnresolved(
                "no canonical child for this source session")
        if len(links) > 1:
            raise ProjectionV2LinkAmbiguous(
                "more than one active link for this source session")
        return links[0].child_id

    def accept(self, *, source_session_id: str, source_record_digest: str,
               summary: Mapping[str, Any], skills: Sequence[Mapping[str, Any]],
               band_totals: Sequence[Mapping[str, Any]]
               ) -> ProjectionV2Result:
        """Canonicalise, verify, resolve, and create-or-replay. The only write.

        The skill rows arrive as plain mappings from the request codec, and are
        passed to the canonicalisation boundary as-is: `canonicalise_skills`
        reads exactly its five named fields from a mapping, so there is no
        intermediate object here whose construction could add or lose a field.
        """
        session_id = (source_session_id or "").strip()
        if not session_id:
            raise ProjectionValidationError("a source session id is required")
        # Lowercase hex EXACTLY, not normalised — see the codec's `_digest`.
        # Restated here because the service must be safe when called directly,
        # not only behind its own transport.
        digest = (source_record_digest or "").strip()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ProjectionValidationError(
                "source_record_digest must be a sha256 hex digest")

        # --- everything decidable without persistence, FIRST --------------
        #
        # Canonicalisation resolves each Parent source identity to exactly one
        # canonical rung, refuses zero or several matches, refuses a subdomain
        # disagreement, and then verifies every band denominator against the
        # frozen declared-track roster. No match is chosen alphabetically, no
        # first match is taken, and no unresolvable skill is dropped — a skill
        # that cannot be identified fails the WHOLE projection.
        totals_as_pairs = [(int(b["months"]), int(b["total_skills"]))
                           for b in band_totals]
        evidence, projected_totals = canonicalise_and_verify(
            rung_source=self._rung_source, summary=dict(summary),
            skills=list(skills), band_totals=totals_as_pairs)

        domain = summary["domain"]

        # --- idempotent replay, checked BEFORE resolving a child ----------
        #
        # An exact retry should cost nothing and must not depend on the link
        # still being active: a projection already written is already written.
        wanted_id = projection_v2_id_for(session_id, domain, digest)
        existing = self._repos.parent_baseline_projections_v2.find(wanted_id)
        if existing is not None:
            return ProjectionV2Result(projection=existing, created=False)

        # --- integrity: a different digest for an immutable source --------
        #
        # Checked before the write and before child resolution, so a conflict is
        # reported as a conflict rather than as whatever the write would have
        # failed with.
        for other in self._repos.parent_baseline_projections_v2.list_for_source(
                session_id, domain):
            if other.source_record_digest != digest:
                raise ProjectionIntegrityError(
                    "a v2 projection exists for this source with a different "
                    "digest; the source baseline is immutable")

        child_id = self._resolve_child(session_id)

        record = ParentBaselineProjectionV2.build(
            child_id=child_id,
            source_session_id=session_id,
            source_record_digest=digest,
            summary=dict(summary),
            skill_evidence=evidence,
            band_totals=projected_totals,
            now=self._stamp(),
        )
        # A create-only repository. If two callers race, the loser's create
        # collides on the deterministic id and raises — the correct outcome for
        # an immutable record, not something to retry around.
        return ProjectionV2Result(
            projection=self._repos.parent_baseline_projections_v2.create(record),
            created=True,
        )


__all__ = [
    "BaselineProjectionV2Service",
    "ProjectionV2ChildUnresolved",
    "ProjectionV2LinkAmbiguous",
    "ProjectionV2Result",
    "SkillCanonicalisationError",
]
