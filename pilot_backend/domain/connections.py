"""pilot_backend/domain/connections.py — explicit relationship records.

Two relationship types, both first-class rows rather than foreign keys on an
entity:

    CaregiverChildConnection   caregiver <-> child
    ProviderChildConnection    provider  <-> child   (carries practice_id)

## Why rows and not columns

October's UI shows one caregiver, one child, one SLP. Modelling that as
`Child.caregiver_id` would make the second caregiver a schema migration on live
clinical data. As rows, every cardinality the product will need already works:

    one caregiver -> many children      many caregivers -> one child
    one provider  -> many children      many providers  -> one child

## Ending never deletes

`end()` stamps `ended_at` and moves the status to a terminal value. The row
stays. A clinical relationship that existed must remain provable afterwards —
for audit, for RTM episode boundaries, and because "was this clinician ever
connected to this child?" is a question with real consequences.

## Practice on the provider connection

`practice_id` is denormalised onto ProviderChildConnection deliberately: a
provider can change employer, and the practice that held the relationship at
the time is part of what happened. Reading it back off `Provider` later would
silently rewrite history.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Optional

from .entities import SCHEMA_VERSION, utc_now
from .enums import (
    CaregiverRelationship,
    ConnectionInitiator,
    ConnectionStatus,
    Visibility,
    TERMINAL_CONNECTION_STATUSES,
)
from .ids import new_caregiver_child_connection_id, new_provider_child_connection_id


class ConnectionError_(ValueError):
    """Invalid connection transition."""


@dataclass(frozen=True)
class CaregiverChildConnection:
    """Links a caregiver to a child. The ONLY expression of child ownership."""

    connection_id: str
    caregiver_id: str
    child_id: str
    relationship_role: CaregiverRelationship
    status: ConnectionStatus = ConnectionStatus.ACTIVE
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    ended_at: Optional[datetime] = None
    created_by_actor_id: Optional[str] = None
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.SYSTEM_AUDIT

    @staticmethod
    def create(caregiver_id: str, child_id: str,
               relationship_role: CaregiverRelationship = CaregiverRelationship.PARENT,
               *, actor_id: Optional[str] = None,
               now: Optional[datetime] = None) -> "CaregiverChildConnection":
        stamp = now or utc_now()
        return CaregiverChildConnection(
            connection_id=new_caregiver_child_connection_id(),
            caregiver_id=caregiver_id,
            child_id=child_id,
            relationship_role=relationship_role,
            created_at=stamp,
            updated_at=stamp,
            created_by_actor_id=actor_id,
        )

    @property
    def is_active(self) -> bool:
        return self.status == ConnectionStatus.ACTIVE and self.ended_at is None

    def end(self, *, status: ConnectionStatus = ConnectionStatus.ENDED,
            now: Optional[datetime] = None) -> "CaregiverChildConnection":
        """Terminate the relationship, preserving the record."""
        if status not in TERMINAL_CONNECTION_STATUSES:
            raise ConnectionError_(f"{status} is not a terminal connection status")
        stamp = now or utc_now()
        return replace(self, status=status, ended_at=stamp, updated_at=stamp)


@dataclass(frozen=True)
class ProviderChildConnection:
    """Links a treating provider to a child, within the practice of record."""

    connection_id: str
    provider_id: str
    child_id: str
    practice_id: str
    status: ConnectionStatus = ConnectionStatus.PENDING
    #: Coarse capability marker. Present so RTM and scope work have somewhere
    #: to land; it grants nothing on its own in 0.1.
    permissions: str = "treating_provider"
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    activated_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    created_by_actor_id: Optional[str] = None
    #: Which side asked for the relationship. 0.5B.
    #:
    #: Defaults to CAREGIVER so every connection written before this field
    #: existed decodes to the only thing it could have been: 0.1-0.5A had no
    #: provider-initiated path at all. A None default would have been the
    #: cautious-looking choice and the wrong one — it would put "unknown" into
    #: rows whose provenance is actually certain.
    initiated_by: ConnectionInitiator = ConnectionInitiator.CAREGIVER
    #: Set only while PAUSED, cleared on resume. Distinct from `ended_at`,
    #: which is terminal: a paused row has neither ended nor stayed active.
    paused_at: Optional[datetime] = None
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.SYSTEM_AUDIT

    @staticmethod
    def create(provider_id: str, child_id: str, practice_id: str, *,
               permissions: str = "treating_provider",
               initiated_by: ConnectionInitiator = ConnectionInitiator.CAREGIVER,
               actor_id: Optional[str] = None,
               now: Optional[datetime] = None) -> "ProviderChildConnection":
        """Created PENDING — a clinician is connected only once it is accepted."""
        stamp = now or utc_now()
        if not isinstance(initiated_by, ConnectionInitiator):
            raise ConnectionError_("initiated_by must be a ConnectionInitiator")
        return ProviderChildConnection(
            connection_id=new_provider_child_connection_id(),
            provider_id=provider_id,
            child_id=child_id,
            practice_id=practice_id,
            permissions=permissions,
            created_at=stamp,
            updated_at=stamp,
            created_by_actor_id=actor_id,
            initiated_by=initiated_by,
        )

    @property
    def is_active(self) -> bool:
        return self.status == ConnectionStatus.ACTIVE and self.ended_at is None

    @property
    def is_paused(self) -> bool:
        return self.status is ConnectionStatus.PAUSED and self.ended_at is None

    @property
    def is_resumable(self) -> bool:
        """Only a PAUSED row can come back, and only if it once was active.

        `activated_at` is the test rather than the status alone: pausing is
        defined as suspending an ACTIVE relationship, so a row that never
        activated has nothing to resume to and must go through acceptance.
        """
        return self.is_paused and self.activated_at is not None

    def activate(self, *, now: Optional[datetime] = None) -> "ProviderChildConnection":
        if self.status in TERMINAL_CONNECTION_STATUSES:
            raise ConnectionError_("cannot reactivate an ended connection; create a new one")
        stamp = now or utc_now()
        return replace(self, status=ConnectionStatus.ACTIVE,
                       activated_at=self.activated_at or stamp, updated_at=stamp)

    def decline(self, *, now: Optional[datetime] = None) -> "ProviderChildConnection":
        """A family refused the invitation. Terminal, and only from PENDING.

        Declining an ACTIVE relationship is not a decline — it is a revoke, and
        the two must not be reachable through one another: a trail showing
        DECLINED for a clinician who had been treating a child for a month
        would misrepresent what happened.
        """
        if self.status is not ConnectionStatus.PENDING:
            raise ConnectionError_(
                "only a pending invitation can be declined")
        stamp = now or utc_now()
        return replace(self, status=ConnectionStatus.DECLINED,
                       ended_at=stamp, updated_at=stamp)

    def pause(self, *, now: Optional[datetime] = None) -> "ProviderChildConnection":
        """Suspend an ACTIVE relationship without ending it.

        `activated_at` is PRESERVED and `ended_at` stays unset. That is what
        makes a resume restore the same relationship rather than mint a new one
        — `ManagingClinicianAssignment.provider_connection_id` points at this
        row, so a pause that created a new connection would orphan it.
        """
        if not self.is_active:
            raise ConnectionError_("only an active connection can be paused")
        stamp = now or utc_now()
        return replace(self, status=ConnectionStatus.PAUSED,
                       paused_at=stamp, updated_at=stamp)

    def resume(self, *, now: Optional[datetime] = None) -> "ProviderChildConnection":
        """Return a PAUSED relationship to ACTIVE, same row, same id."""
        if not self.is_resumable:
            raise ConnectionError_(
                "only a paused connection that was once active can be resumed")
        stamp = now or utc_now()
        return replace(self, status=ConnectionStatus.ACTIVE,
                       paused_at=None, updated_at=stamp)

    def end(self, *, status: ConnectionStatus = ConnectionStatus.ENDED,
            now: Optional[datetime] = None) -> "ProviderChildConnection":
        if status not in TERMINAL_CONNECTION_STATUSES:
            raise ConnectionError_(f"{status} is not a terminal connection status")
        if status is ConnectionStatus.DECLINED:
            # Reachable only by asking for it explicitly, and `decline()` is
            # the operation that means it. Allowing it here would let an
            # ACTIVE relationship be recorded as refused.
            raise ConnectionError_("use decline() to refuse a pending invitation")
        stamp = now or utc_now()
        return replace(self, status=status, ended_at=stamp, updated_at=stamp)
