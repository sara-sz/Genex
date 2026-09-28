"""pilot_backend/revision/records.py — once finalized, never silently overwritten.

Infrastructure for records that will later be clinically meaningful — notes,
assessments, signed documentation. There are no such records in 0.2 and none
are created here; this is the mechanism 0.3 will attach them to.

## The rule

A draft may be edited freely. Once FINALIZED it is immutable: changing it means
creating a NEW revision that supersedes the old one, carrying an actor, a
server timestamp and a reason. The prior version stays readable forever.

This is how clinical documentation has to behave — an amended note must show
that it was amended, by whom, when and why. A record that can be edited in
place cannot answer "what did the clinician actually write at the time?", and
that is the only question that matters in a dispute or an audit.

## Content lives elsewhere

`Revision` holds no clinical text. It carries `content_ref`, an opaque pointer
to wherever the content is stored. Two reasons: the revision chain is metadata
and belongs in an audit-tier collection, and keeping content out means the
version history never becomes an unindexed second copy of the PHI.

## Deliberately small

No diffing, no merge, no branching, no soft-delete, no approval workflow.
Version chains acquire those features when a real record type needs them; a
speculative one built now would be designed against imagined requirements.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Sequence

from ..domain.ids import new_revision_id
from ..domain.roles import ActorRole


class ImmutableRecordError(Exception):
    """An attempt to mutate a finalized revision, or to amend an unfinalized one.

    PHI-safe: names the record id and version, never the content.
    """

    PHI_SAFE_MESSAGE = True


class RecordState(str, Enum):
    DRAFT = "draft"
    FINALIZED = "finalized"


@dataclass(frozen=True)
class Revision:
    """One version of a record. Frozen — every change produces a new instance."""

    revision_id: str
    #: Stable across the whole chain. All versions of one logical record share it.
    record_id: str
    version: int
    state: RecordState
    created_at: datetime
    actor_application_id: str
    actor_role: ActorRole
    #: Opaque pointer to the content. Never the content itself.
    content_ref: str = ""
    #: The revision this one replaces. None for version 1.
    supersedes_revision_id: Optional[str] = None
    #: Required when amending a finalized record; empty for drafts and v1.
    amendment_reason: str = ""
    finalized_at: Optional[datetime] = None
    schema_version: str = "october-pilot-0.2"

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ImmutableRecordError("revision versions start at 1")
        if self.created_at.tzinfo is None:
            raise ImmutableRecordError("revision timestamps must be timezone-aware")

    @property
    def is_finalized(self) -> bool:
        return self.state is RecordState.FINALIZED


def start_draft(record_id: str, *, actor_application_id: str, actor_role: ActorRole,
                content_ref: str = "", now: Optional[datetime] = None) -> Revision:
    """Begin a record at version 1, editable."""
    return Revision(
        revision_id=new_revision_id(),
        record_id=record_id,
        version=1,
        state=RecordState.DRAFT,
        created_at=now or datetime.now(timezone.utc),
        actor_application_id=actor_application_id,
        actor_role=actor_role,
        content_ref=content_ref,
    )


def finalize(revision: Revision, *, now: Optional[datetime] = None) -> Revision:
    """Seal a draft. Finalizing an already-finalized revision is refused.

    Refused rather than treated as idempotent: a second finalize means the
    caller believes it is sealing something it has not seen, and silently
    succeeding would hide that.
    """
    if revision.is_finalized:
        raise ImmutableRecordError(
            f"revision {revision.revision_id} v{revision.version} is already finalized")
    stamp = now or datetime.now(timezone.utc)
    return replace(revision, state=RecordState.FINALIZED, finalized_at=stamp)


def amend(previous: Revision, *, actor_application_id: str, actor_role: ActorRole,
          reason: str, content_ref: str = "",
          now: Optional[datetime] = None) -> Revision:
    """Create the next version of a FINALIZED record. The prior version survives.

    The reason is mandatory and non-empty. An amendment without a stated reason
    is indistinguishable from an edit, which is exactly the thing this module
    exists to prevent.
    """
    if not previous.is_finalized:
        raise ImmutableRecordError(
            "only a finalized revision can be amended; edit the draft instead")
    if not (reason or "").strip():
        raise ImmutableRecordError("an amendment must state a reason")
    return Revision(
        revision_id=new_revision_id(),
        record_id=previous.record_id,
        version=previous.version + 1,
        state=RecordState.DRAFT,
        created_at=now or datetime.now(timezone.utc),
        actor_application_id=actor_application_id,
        actor_role=actor_role,
        content_ref=content_ref or previous.content_ref,
        supersedes_revision_id=previous.revision_id,
        amendment_reason=reason.strip(),
    )


def latest(revisions: Sequence[Revision]) -> Optional[Revision]:
    """Highest version in a chain. Returns None for an empty chain."""
    chain: List[Revision] = [r for r in revisions if r is not None]
    if not chain:
        return None
    return max(chain, key=lambda r: r.version)
