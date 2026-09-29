"""pilot_backend/domain/child_context.py — the one mutable pilot record.

A deliberately tiny child-scoped record, added so PRE-PHI Integration 0.3 has
something real to persist, authorize, audit and amend. It is NOT a clinical
record and it is not the beginning of one.

## It holds no content

The record carries a `content_ref` — an opaque pointer — and never the content
itself. That is the same decision `revision/records.py` made, for the same
reason: the version chain and the record index are audit-tier metadata, and
putting text here would make every history read a disclosure of the text, and
would put a PHI-shaped field into a model whose whole purpose is to be safe to
reference.

It also means no workflow in 0.3 can accidentally copy clinical content into
an audit event, because there is no clinical content in the system to copy.

## Versioning lives in the revision chain

`current_revision_id` and `current_version` are a pointer into
`pilot_revisions`. The record says which version is current; the chain says
what every version was, who wrote it, when, and why. Finalizing and amending
go through `revision.records`, so this entity never implements its own
history semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Optional

from .entities import SCHEMA_VERSION, utc_now
from .enums import Visibility
from .ids import new_child_context_id
from .roles import ActorRole


@dataclass(frozen=True)
class ChildContextRecord:
    """Index of the current version of one child's context record."""

    record_id: str
    child_id: str
    #: Opaque pointer to wherever the content is held. Never the content.
    content_ref: str = ""
    current_revision_id: Optional[str] = None
    current_version: int = 0
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    created_by_actor_id: Optional[str] = None
    #: Role of whoever last wrote it. Recorded because a caregiver-authored
    #: and a provider-authored record are different things to a reader.
    last_actor_role: Optional[ActorRole] = None
    schema_version: str = SCHEMA_VERSION

    #: Parent-visible: this is the family's own context, not clinical
    #: interpretation. A provider may also write it; nothing here is
    #: therapist-private, and anything that becomes therapist-private must be
    #: a separate record with its own visibility, as ParentNote and
    #: PrivateTherapistNote already are in the therapist service.
    VISIBILITY = Visibility.PARENT_VISIBLE

    @staticmethod
    def create(child_id: str, *, actor_id: Optional[str] = None,
               actor_role: Optional[ActorRole] = None,
               content_ref: str = "",
               now: Optional[datetime] = None) -> "ChildContextRecord":
        stamp = now or utc_now()
        return ChildContextRecord(
            record_id=new_child_context_id(),
            child_id=child_id,
            content_ref=content_ref,
            created_at=stamp,
            updated_at=stamp,
            created_by_actor_id=actor_id,
            last_actor_role=actor_role,
        )

    def with_current_revision(self, revision_id: str, version: int, *,
                              content_ref: str = "",
                              actor_role: Optional[ActorRole] = None,
                              now: Optional[datetime] = None) -> "ChildContextRecord":
        """Point the record at a newly written revision."""
        return replace(
            self,
            current_revision_id=revision_id,
            current_version=version,
            content_ref=content_ref or self.content_ref,
            last_actor_role=actor_role or self.last_actor_role,
            updated_at=now or utc_now(),
        )
