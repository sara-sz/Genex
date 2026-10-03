"""0.5E-A — immutable canonical anchors for suggestions and clinical goals.

Two create-only records, each embedding a `CanonicalRung`:

    SuggestionCanonicalAnchor   written by the generation boundary, beside the
                                GoalSuggestion it belongs to
    ClinicalGoalAnchor          copied from that anchor at approval, in the
                                same transaction as the goal and version 1

## Why SIDECAR records rather than fields on the existing types

Measured, not assumed: `persistence/codecs.decode` refuses any key-set
mismatch in BOTH directions. A document missing a field the model now expects
raises `document is missing fields: ...`, and a document carrying a field the
model does not expect raises `document has unknown fields: ...`.

So adding `canonical_rung` to `GoalSuggestion` or `ClinicalGoal` would make
every already-stored document undecodable — the two fictional staging
suggestions would start returning 500 on a read, and every pre-0.5E-A goal
would too. That would be a migration, and the founder decision was explicitly
no historical mutation and no backfill.

A sidecar inverts the problem into exactly the approved behaviour: absence of
an anchor record IS the unmappable state. No migration, no rewrite, no change
to any existing document, and no change to the 0.5D HTTP payloads.

## Immutability

Both repositories are create-only — no `update`, no `set`, no `overwrite`.
Immutability is therefore structural rather than a rule someone must remember:
there is no method that could rewrite an anchor. A wording revision appends a
new `GoalVersion` and touches nothing here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .canonical_rung import CanonicalRung, RungError
from .entities import SCHEMA_VERSION, utc_now


@dataclass(frozen=True)
class SuggestionCanonicalAnchor:
    """The canonical rung a GoalSuggestion was generated for.

    Keyed by `suggestion_id`, so there is at most one per suggestion and the
    document id carries no information the record does not also hold.

    This is the TRUST ANCHOR for the whole feature. A clinical goal may only
    be anchored by copying one of these, and only the generation boundary
    writes them — a browser request can never produce one.
    """

    suggestion_id: str
    child_id: str
    rung: CanonicalRung
    created_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not (self.suggestion_id or "").strip():
            raise RungError("a suggestion anchor requires a suggestion id")
        if not (self.child_id or "").strip():
            raise RungError("a suggestion anchor requires a child id")
        if not isinstance(self.rung, CanonicalRung):
            raise RungError("a suggestion anchor requires a CanonicalRung")

    @property
    def is_activity_mappable(self) -> bool:
        return self.rung.is_activity_mappable


@dataclass(frozen=True)
class ClinicalGoalAnchor:
    """The canonical rung an approved ClinicalGoal is anchored to.

    Keyed by `clinical_goal_id`. Written once, in the same transaction as the
    goal and its first version, and never again.

    `source_suggestion_id` records WHICH suggestion's provenance was copied,
    so the chain caregiver evidence -> suggestion -> anchor -> goal stays
    walkable without re-deriving anything.

    There is no anchor for an `authored_fresh` goal: a goal with no canonical
    provenance has nothing to anchor to, and inventing one from its text is
    the specific failure this slice exists to prevent.
    """

    clinical_goal_id: str
    child_id: str
    source_suggestion_id: str
    rung: CanonicalRung
    created_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not (self.clinical_goal_id or "").strip():
            raise RungError("a goal anchor requires a clinical goal id")
        if not (self.child_id or "").strip():
            raise RungError("a goal anchor requires a child id")
        if not (self.source_suggestion_id or "").strip():
            # An anchor with no source suggestion would be an anchor nobody
            # can trace, which is indistinguishable from a guess.
            raise RungError("a goal anchor requires its source suggestion")
        if not isinstance(self.rung, CanonicalRung):
            raise RungError("a goal anchor requires a CanonicalRung")

    @property
    def is_activity_mappable(self) -> bool:
        """Derived from the rung. Never stored, so it cannot drift."""
        return self.rung.is_activity_mappable

    @staticmethod
    def from_suggestion_anchor(clinical_goal_id: str,
                               anchor: SuggestionCanonicalAnchor, *,
                               now: Optional[datetime] = None
                               ) -> "ClinicalGoalAnchor":
        """Copy canonical provenance onto a goal. The ONLY way one is minted.

        The rung is carried across by reference — it is a frozen value object,
        so the goal's anchor and the suggestion's anchor hold the identical
        rung, field for field, with no opportunity to re-derive or adjust it.
        """
        if not isinstance(anchor, SuggestionCanonicalAnchor):
            raise RungError(
                "a goal anchor may only be copied from a suggestion anchor")
        return ClinicalGoalAnchor(
            clinical_goal_id=clinical_goal_id,
            child_id=anchor.child_id,
            source_suggestion_id=anchor.suggestion_id,
            rung=anchor.rung,
            created_at=now or utc_now(),
        )
