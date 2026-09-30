"""pilot_backend/domain/source_link.py — canonical child ↔ source-system identity.

`Child.child_id` (`chld_*`) is the canonical longitudinal identity. A Parent
`session_id` and a Therapist child id are SOURCE-SYSTEM identifiers: real,
useful for joining, and never canonical. This record is the only bridge.

## Two uniqueness constraints, both enforced at write time

    (child_id, source_system)      one ACTIVE link
    (source_system, external_id)   one ACTIVE canonical child

The second is the one that matters most. Without it, one Parent session could
actively map to two canonical children, and every downstream question —
which goals, which month, whose evidence — would have two answers. The 0.3
auth-subject defect was exactly this shape, found late; here it is refused at
the write.

## Ending never deletes

`end()` stamps `ended_at`, sets a terminal status and keeps the row, so the
history of which external identity was bound to which child, and when, stays
reconstructible. Nothing in this package removes a link.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Optional

from .entities import SCHEMA_VERSION, utc_now
from .enums import Visibility
from .ids import new_source_link_id


class SourceSystem(str, Enum):
    """External systems that hold their own identifier for a child.

    Deliberately NOT a free string: an unrecognised system name would create a
    uniqueness namespace nobody is enforcing.
    """

    PARENT = "parent"
    THERAPIST = "therapist"


class SourceLinkStatus(str, Enum):
    """Lifecycle of a bridge record. There is no DELETED state."""

    ACTIVE = "active"
    ENDED = "ended"
    SUPERSEDED = "superseded"


TERMINAL_LINK_STATUSES = frozenset({SourceLinkStatus.ENDED, SourceLinkStatus.SUPERSEDED})


class SourceLinkError(ValueError):
    """Invalid source-link transition. PHI-safe: names the transition only."""

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class SourceSystemLink:
    """Binds one external identity to one canonical child."""

    link_id: str
    child_id: str
    source_system: SourceSystem
    #: The external system's identifier — Parent `session_id`, Therapist child id.
    external_id: str
    #: The external system's owner handle — Parent `owner_uid`, Therapist
    #: `parent_id`. Retained for provenance; NEVER used as a join key, because
    #: an owner handle is not an identity (the 0.3 email rule, restated).
    external_owner_ref: str = ""
    status: SourceLinkStatus = SourceLinkStatus.ACTIVE
    linked_at: datetime = field(default_factory=utc_now)
    linked_by_actor_id: Optional[str] = None
    linked_by_role: Optional[str] = None
    ended_at: Optional[datetime] = None
    ended_by_actor_id: Optional[str] = None
    end_reason: str = ""
    #: Claim documents that were won to create this link. Recorded so an
    #: auditor can verify the uniqueness race was actually run.
    child_source_claim_id: str = ""
    external_identity_claim_id: str = ""
    superseded_by_link_id: Optional[str] = None
    supersedes_link_id: Optional[str] = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    created_by_actor_id: Optional[str] = None
    schema_version: str = SCHEMA_VERSION

    #: Identifiers and timestamps, not clinical content, and not parent-facing.
    VISIBILITY = Visibility.SYSTEM_AUDIT

    @property
    def is_active(self) -> bool:
        return self.status is SourceLinkStatus.ACTIVE and self.ended_at is None

    @staticmethod
    def create(child_id: str, source_system: SourceSystem, external_id: str, *,
               external_owner_ref: str = "",
               actor_id: Optional[str] = None,
               actor_role: Optional[str] = None,
               child_source_claim_id: str = "",
               external_identity_claim_id: str = "",
               supersedes_link_id: Optional[str] = None,
               now: Optional[datetime] = None) -> "SourceSystemLink":
        stamp = now or utc_now()
        if not (external_id or "").strip():
            raise SourceLinkError("a source link requires a non-empty external id")
        if not (child_id or "").strip():
            raise SourceLinkError("a source link requires a child id")
        return SourceSystemLink(
            link_id=new_source_link_id(),
            child_id=child_id,
            source_system=source_system,
            external_id=external_id.strip(),
            external_owner_ref=(external_owner_ref or "").strip(),
            linked_at=stamp,
            linked_by_actor_id=actor_id,
            linked_by_role=actor_role,
            child_source_claim_id=child_source_claim_id,
            external_identity_claim_id=external_identity_claim_id,
            supersedes_link_id=supersedes_link_id,
            created_at=stamp,
            updated_at=stamp,
            created_by_actor_id=actor_id,
        )

    def end(self, *, status: SourceLinkStatus = SourceLinkStatus.ENDED,
            actor_id: Optional[str] = None, reason: str = "",
            now: Optional[datetime] = None) -> "SourceSystemLink":
        """Terminate the link, preserving the record."""
        if status not in TERMINAL_LINK_STATUSES:
            raise SourceLinkError(f"{status} is not a terminal source-link status")
        if not self.is_active:
            raise SourceLinkError("only an active source link can be ended")
        stamp = now or utc_now()
        return replace(self, status=status, ended_at=stamp,
                       ended_by_actor_id=actor_id, end_reason=reason,
                       updated_at=stamp)

    def with_successor(self, successor_link_id: str,
                       *, now: Optional[datetime] = None) -> "SourceSystemLink":
        """Record which link replaced this one. Lineage, not mutation of state."""
        return replace(self, superseded_by_link_id=successor_link_id,
                       updated_at=now or utc_now())
