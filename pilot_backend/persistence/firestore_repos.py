"""pilot_backend/persistence/firestore_repos.py — production-capable repositories.

Implements the BACKEND 0.1 repository protocols over a `DocumentStore`. The
protocols are unchanged: the domain layer does not know a database exists, and
swapping `InMemoryRepositories` for `FirestoreRepositories` changes no business
logic. That was the point of keeping them narrow.

## Still no delete

Same guarantee as 0.1, now at the storage layer: no method here deletes a
document, and the `DocumentStore` port exposes no delete operation at all. A
repository cannot erase history because the storage contract it is written
against has no way to.

## Ordering

Listings sort by `created_at` then the record's OWN id. Firestore returns
query results in its own order, not insertion order, so the sort is applied
after reading rather than assumed. The tiebreak uses `connection_id` first for
connection records — a foreign key would tie every row belonging to the same
caregiver and silently fall back to whatever order the store returned, which is
the intermittent flake BACKEND 0.1 fixed.

## Not connected to anything yet

There is no Firestore client here and no credential. A composition root binds a
real one to the `DocumentStore` port once the HIPAA workstream approves a
production project, Identity Platform configuration, BAA coverage, IAM, logging
and backup posture. Until then these run against `FakeDocumentStore` or the
Firestore emulator, both of which satisfy the same port.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional, Sequence, Type

from ..audit.events import AuditEvent
from ..domain.connections import CaregiverChildConnection, ProviderChildConnection
from ..domain.entities import Caregiver, Child, Practice, Provider
from ..domain.enums import ConnectionStatus, EntityStatus
from ..repository.interface import DuplicateRecord, RecordNotFound
from ..revision.records import Revision
from .codecs import decode, encode
from .collections import collection_for
from .document_store import DocumentStore, DocumentStoreError


def _own_id(record: Any) -> str:
    """The record's OWN identifier, for a deterministic sort tiebreak.

    `connection_id` is checked first and that order is load-bearing — see the
    module docstring.
    """
    for attr in ("connection_id", "practice_id", "provider_id",
                 "caregiver_id", "child_id", "event_id", "revision_id"):
        value = getattr(record, attr, None)
        if value:
            return str(value)
    raise AttributeError("record has no known identifier field")


def _sorted(records: Sequence[Any]) -> List[Any]:
    key_attr = "occurred_at"
    return sorted(
        records,
        key=lambda r: (getattr(r, "created_at", None) or getattr(r, key_attr), _own_id(r)),
    )


class _BaseRepo:
    """Shared document plumbing. One collection, one record type."""

    record_type: str = ""
    model: Type[Any] = object

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    @property
    def _collection(self) -> str:
        return collection_for(self.record_type)

    def _create(self, doc_id: str, record: Any) -> Any:
        try:
            self._store.create(self._collection, doc_id, encode(record))
        except DocumentStoreError:
            raise DuplicateRecord(f"record already exists: {doc_id}")
        return record

    def _get(self, doc_id: str) -> Any:
        document = self._store.get(self._collection, doc_id)
        if document is None:
            raise RecordNotFound(doc_id)
        return decode(self.model, document)

    def _set(self, doc_id: str, record: Any) -> Any:
        self._store.set(self._collection, doc_id, encode(record))
        return record

    def _query(self, field: str, value: Any) -> List[Any]:
        return _sorted([decode(self.model, data)
                        for _, data in self._store.query_equals(self._collection, field, value)])

    def _all(self) -> List[Any]:
        return _sorted([decode(self.model, data)
                        for _, data in self._store.list_all(self._collection)])


class FirestorePracticeRepository(_BaseRepo):
    record_type, model = "practice", Practice

    def create(self, practice: Practice) -> Practice:
        return self._create(practice.practice_id, practice)

    def get_by_id(self, practice_id: str) -> Practice:
        return self._get(practice_id)

    def update_status(self, practice_id: str, status: EntityStatus,
                      *, now: Optional[datetime] = None) -> Practice:
        return self._set(practice_id, self._get(practice_id).with_status(status, now=now))


class FirestoreProviderRepository(_BaseRepo):
    record_type, model = "provider", Provider

    def create(self, provider: Provider) -> Provider:
        return self._create(provider.provider_id, provider)

    def get_by_id(self, provider_id: str) -> Provider:
        return self._get(provider_id)

    def update_status(self, provider_id: str, status: EntityStatus,
                      *, now: Optional[datetime] = None) -> Provider:
        return self._set(provider_id, self._get(provider_id).with_status(status, now=now))

    def list_by_practice(self, practice_id: str) -> List[Provider]:
        return self._query("practice_id", practice_id)

    def get_by_auth_subject(self, auth_subject: str) -> Optional[Provider]:
        """Equality query on the auth subject — never on email.

        An empty subject returns None without querying: an unbound provider
        record stores `auth_subject` as null, and a blank lookup must not match
        it. Matching null to "" would hand an unclaimed clinician record to
        anyone whose token failed to carry a subject.
        """
        subject = (auth_subject or "").strip()
        if not subject:
            return None
        found = self._query("auth_subject", subject)
        return found[0] if found else None


class FirestoreCaregiverRepository(_BaseRepo):
    record_type, model = "caregiver", Caregiver

    def create(self, caregiver: Caregiver) -> Caregiver:
        return self._create(caregiver.caregiver_id, caregiver)

    def get_by_id(self, caregiver_id: str) -> Caregiver:
        return self._get(caregiver_id)

    def update_status(self, caregiver_id: str, status: EntityStatus,
                      *, now: Optional[datetime] = None) -> Caregiver:
        return self._set(caregiver_id, self._get(caregiver_id).with_status(status, now=now))

    def get_by_auth_subject(self, auth_subject: str) -> Optional[Caregiver]:
        subject = (auth_subject or "").strip()
        if not subject:
            return None
        found = self._query("auth_subject", subject)
        return found[0] if found else None


class FirestoreChildRepository(_BaseRepo):
    record_type, model = "child", Child

    def create(self, child: Child) -> Child:
        return self._create(child.child_id, child)

    def get_by_id(self, child_id: str) -> Child:
        return self._get(child_id)

    def update_status(self, child_id: str, status: EntityStatus,
                      *, now: Optional[datetime] = None) -> Child:
        return self._set(child_id, self._get(child_id).with_status(status, now=now))


class FirestoreCaregiverChildConnectionRepository(_BaseRepo):
    record_type, model = "caregiver_child_connection", CaregiverChildConnection

    def connect(self, connection: CaregiverChildConnection) -> CaregiverChildConnection:
        return self._create(connection.connection_id, connection)

    def get_by_id(self, connection_id: str) -> CaregiverChildConnection:
        return self._get(connection_id)

    def end_connection(self, connection_id: str, *,
                       status: ConnectionStatus = ConnectionStatus.ENDED,
                       now: Optional[datetime] = None) -> CaregiverChildConnection:
        ended = self._get(connection_id).end(status=status, now=now)
        return self._set(connection_id, ended)

    def list_children_for_caregiver(self, caregiver_id: str, *, include_ended: bool = False
                                    ) -> List[CaregiverChildConnection]:
        found = self._query("caregiver_id", caregiver_id)
        return found if include_ended else [c for c in found if c.is_active]

    def list_caregivers_for_child(self, child_id: str, *, include_ended: bool = False
                                  ) -> List[CaregiverChildConnection]:
        found = self._query("child_id", child_id)
        return found if include_ended else [c for c in found if c.is_active]


class FirestoreProviderChildConnectionRepository(_BaseRepo):
    record_type, model = "provider_child_connection", ProviderChildConnection

    def connect(self, connection: ProviderChildConnection) -> ProviderChildConnection:
        return self._create(connection.connection_id, connection)

    def get_by_id(self, connection_id: str) -> ProviderChildConnection:
        return self._get(connection_id)

    def activate(self, connection_id: str,
                 *, now: Optional[datetime] = None) -> ProviderChildConnection:
        return self._set(connection_id, self._get(connection_id).activate(now=now))

    def end_connection(self, connection_id: str, *,
                       status: ConnectionStatus = ConnectionStatus.ENDED,
                       now: Optional[datetime] = None) -> ProviderChildConnection:
        ended = self._get(connection_id).end(status=status, now=now)
        return self._set(connection_id, ended)

    def list_children_for_provider(self, provider_id: str, *, include_ended: bool = False
                                   ) -> List[ProviderChildConnection]:
        found = self._query("provider_id", provider_id)
        return found if include_ended else [c for c in found if c.is_active]

    def list_providers_for_child(self, child_id: str, *, include_ended: bool = False
                                 ) -> List[ProviderChildConnection]:
        found = self._query("child_id", child_id)
        return found if include_ended else [c for c in found if c.is_active]


class FirestoreAuditEventRepository(_BaseRepo):
    """Append-only audit log.

    There is no update and no delete — not as policy, but because no such
    method exists and the storage port offers no delete. An audit trail that
    can be rewritten by the system it audits is not evidence of anything.
    """

    record_type, model = "audit_event", AuditEvent

    def append(self, event: AuditEvent) -> AuditEvent:
        return self._create(event.event_id, event)

    def get_by_id(self, event_id: str) -> AuditEvent:
        return self._get(event_id)

    def list_for_child(self, child_id: str) -> List[AuditEvent]:
        return self._query("child_id", child_id)

    def list_for_actor(self, actor_application_id: str) -> List[AuditEvent]:
        return self._query("actor_application_id", actor_application_id)

    def list_all(self) -> List[AuditEvent]:
        return self._all()


class FirestoreRevisionRepository(_BaseRepo):
    """Append-only version chains. No update method exists, by design."""

    record_type, model = "revision", Revision

    def append(self, revision: Revision) -> Revision:
        return self._create(revision.revision_id, revision)

    def get_by_id(self, revision_id: str) -> Revision:
        return self._get(revision_id)

    def list_chain(self, record_id: str) -> List[Revision]:
        """Every version of one logical record, oldest first."""
        return sorted(self._query("record_id", record_id), key=lambda r: r.version)


class FirestoreRepositories:
    """All repositories over one document store — the production composition."""

    def __init__(self, store: DocumentStore) -> None:
        self.store = store
        self.practices = FirestorePracticeRepository(store)
        self.providers = FirestoreProviderRepository(store)
        self.caregivers = FirestoreCaregiverRepository(store)
        self.children = FirestoreChildRepository(store)
        self.caregiver_child = FirestoreCaregiverChildConnectionRepository(store)
        self.provider_child = FirestoreProviderChildConnectionRepository(store)
        self.audit_events = FirestoreAuditEventRepository(store)
        self.revisions = FirestoreRevisionRepository(store)
