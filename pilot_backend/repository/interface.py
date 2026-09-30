"""pilot_backend/repository/interface.py — narrow persistence contracts.

One protocol per entity, each with only the operations the pilot actually
needs. No generic CRUD: a `delete()` nobody needs is a delete somebody
eventually calls on a clinical relationship.

## Shaped for a Firestore implementation, not tied to one

The therapist service already proved this shape works against a
Firestore-style store (`therapist_api/app/repository/`): documents keyed by id,
create-if-absent, equality queries, transactional swap. These protocols stay
inside that intersection — no joins, no partial updates, no server-side
aggregation — so a production implementation can be written without any change
to domain or business logic.

Nothing here imports a database driver, and `InMemoryStore` is the only
implementation in 0.1. Provisioning is blocked until the HIPAA workstream
approves the project, Identity Platform configuration, BAA coverage, IAM,
logging and backup posture.

## No delete

Entities are retired with `update_status`. Relationships are ended with
`end_connection`, which stamps `ended_at` and keeps the row. Neither protocol
exposes a way to remove history.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Protocol, runtime_checkable

from ..domain.connections import CaregiverChildConnection, ProviderChildConnection
from ..domain.entities import Caregiver, Child, Practice, Provider
from ..domain.enums import ConnectionStatus, EntityStatus


class RecordNotFound(KeyError):
    """No record with that identifier."""


class DuplicateRecord(ValueError):
    """A record with that identifier already exists."""


class AmbiguousAuthSubject(ValueError):
    """More than one application record carries the same auth subject.

    A data-integrity fault, not a lookup miss. Returning "the first one" would
    make the effective identity depend on document ordering, so resolution
    fails closed instead. PHI-safe: names neither the subject nor the records.
    """

    PHI_SAFE_MESSAGE = True


class AmbiguousRecordState(ValueError):
    """Two records hold a state that a write-time claim guarantees is unique.

    Same reasoning as `AmbiguousAuthSubject`, generalised: when uniqueness is
    enforced by a claim document, finding two survivors means the claim was
    bypassed, and "return the first one" would let document order decide which
    plan a child's month actually followed. Fails closed instead.

    PHI-safe: names neither the records nor their contents.
    """

    PHI_SAFE_MESSAGE = True


@runtime_checkable
class PracticeRepository(Protocol):
    def create(self, practice: Practice) -> Practice: ...
    def get_by_id(self, practice_id: str) -> Practice: ...
    def update_status(self, practice_id: str, status: EntityStatus,
                      *, now: Optional[datetime] = None) -> Practice: ...


@runtime_checkable
class ProviderRepository(Protocol):
    def create(self, provider: Provider) -> Provider: ...
    def get_by_id(self, provider_id: str) -> Provider: ...
    def update_status(self, provider_id: str, status: EntityStatus,
                      *, now: Optional[datetime] = None) -> Provider: ...
    def list_by_practice(self, practice_id: str) -> List[Provider]: ...
    #: Resolve an authenticated subject to the application record. The ONLY
    #: place auth identity meets application identity.
    def get_by_auth_subject(self, auth_subject: str) -> Optional[Provider]: ...


@runtime_checkable
class CaregiverRepository(Protocol):
    def create(self, caregiver: Caregiver) -> Caregiver: ...
    def get_by_id(self, caregiver_id: str) -> Caregiver: ...
    def update_status(self, caregiver_id: str, status: EntityStatus,
                      *, now: Optional[datetime] = None) -> Caregiver: ...
    def get_by_auth_subject(self, auth_subject: str) -> Optional[Caregiver]: ...


@runtime_checkable
class ChildRepository(Protocol):
    def create(self, child: Child) -> Child: ...
    def get_by_id(self, child_id: str) -> Child: ...
    def update_status(self, child_id: str, status: EntityStatus,
                      *, now: Optional[datetime] = None) -> Child: ...


@runtime_checkable
class CaregiverChildConnectionRepository(Protocol):
    def connect(self, connection: CaregiverChildConnection) -> CaregiverChildConnection: ...
    def get_by_id(self, connection_id: str) -> CaregiverChildConnection: ...
    def end_connection(self, connection_id: str, *,
                       status: ConnectionStatus = ConnectionStatus.ENDED,
                       now: Optional[datetime] = None) -> CaregiverChildConnection: ...
    #: `include_ended` defaults False for the normal "who can see this child
    #: now" question, and True is how audit reads the full history.
    def list_children_for_caregiver(self, caregiver_id: str, *,
                                    include_ended: bool = False
                                    ) -> List[CaregiverChildConnection]: ...
    def list_caregivers_for_child(self, child_id: str, *,
                                  include_ended: bool = False
                                  ) -> List[CaregiverChildConnection]: ...


@runtime_checkable
class ProviderChildConnectionRepository(Protocol):
    def connect(self, connection: ProviderChildConnection) -> ProviderChildConnection: ...
    def get_by_id(self, connection_id: str) -> ProviderChildConnection: ...
    def activate(self, connection_id: str,
                 *, now: Optional[datetime] = None) -> ProviderChildConnection: ...
    def end_connection(self, connection_id: str, *,
                       status: ConnectionStatus = ConnectionStatus.ENDED,
                       now: Optional[datetime] = None) -> ProviderChildConnection: ...
    def list_children_for_provider(self, provider_id: str, *,
                                   include_ended: bool = False
                                   ) -> List[ProviderChildConnection]: ...
    def list_providers_for_child(self, child_id: str, *,
                                 include_ended: bool = False
                                 ) -> List[ProviderChildConnection]: ...
