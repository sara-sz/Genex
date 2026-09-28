"""pilot_backend/repository/memory.py — deterministic in-memory implementation.

The only implementation in BACKEND 0.1. Test-grade, but it honours the same
contracts a production store must: records are copied in and out, listings are
deterministically ordered, and nothing can be deleted.

## Copy on read and write

Entities are frozen dataclasses, so the store holds them directly — but the
dict is never handed out, and list results are fresh lists. A caller cannot
reach in and mutate stored state, which is the behaviour a real document store
would give and the behaviour tests should be written against.

## Deterministic ordering

Listings sort by `created_at` then id. A dict happens to preserve insertion
order in CPython, but relying on that would let a test pass here and fail
against Firestore, whose query order is not insertion order.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, TypeVar

from ..domain.connections import CaregiverChildConnection, ProviderChildConnection
from ..domain.entities import Caregiver, Child, Practice, Provider
from ..domain.enums import ConnectionStatus, EntityStatus
from .interface import DuplicateRecord, RecordNotFound

T = TypeVar("T")


def _sorted(records: List[T]) -> List[T]:
    return sorted(records, key=lambda r: (getattr(r, "created_at"), _id_of(r)))


def _id_of(record: object) -> str:
    """The record's OWN identifier, for use as a deterministic sort tiebreak.

    `connection_id` is checked first, and that ordering is load-bearing. A
    connection also carries `caregiver_id`/`provider_id`/`child_id`, so looking
    those up first returned a FOREIGN key — and every connection belonging to
    one caregiver then produced the same tiebreak value. Records created in the
    same instant tied, the sort fell back to insertion order, and listings were
    only accidentally stable. The determinism test caught it intermittently,
    which is exactly the failure mode that survives a green suite.
    """
    for attr in ("connection_id", "practice_id", "provider_id", "caregiver_id", "child_id"):
        value = getattr(record, attr, None)
        if value:
            return str(value)
    raise AttributeError(f"{record!r} has no known identifier field")


class InMemoryStore:
    """Backing store for every pilot repository.

    One object so a test can build a whole topology and query it end to end
    without wiring six separate fakes.
    """

    def __init__(self) -> None:
        self.practices: Dict[str, Practice] = {}
        self.providers: Dict[str, Provider] = {}
        self.caregivers: Dict[str, Caregiver] = {}
        self.children: Dict[str, Child] = {}
        self.caregiver_child: Dict[str, CaregiverChildConnection] = {}
        self.provider_child: Dict[str, ProviderChildConnection] = {}


def _create(table: Dict[str, T], key: str, record: T) -> T:
    if key in table:
        raise DuplicateRecord(f"record already exists: {key}")
    table[key] = record
    return record


def _get(table: Dict[str, T], key: str) -> T:
    if key not in table:
        raise RecordNotFound(key)
    return table[key]


class InMemoryPracticeRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def create(self, practice: Practice) -> Practice:
        return _create(self._store.practices, practice.practice_id, practice)

    def get_by_id(self, practice_id: str) -> Practice:
        return _get(self._store.practices, practice_id)

    def update_status(self, practice_id: str, status: EntityStatus,
                      *, now: Optional[datetime] = None) -> Practice:
        updated = _get(self._store.practices, practice_id).with_status(status, now=now)
        self._store.practices[practice_id] = updated
        return updated


class InMemoryProviderRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def create(self, provider: Provider) -> Provider:
        return _create(self._store.providers, provider.provider_id, provider)

    def get_by_id(self, provider_id: str) -> Provider:
        return _get(self._store.providers, provider_id)

    def update_status(self, provider_id: str, status: EntityStatus,
                      *, now: Optional[datetime] = None) -> Provider:
        updated = _get(self._store.providers, provider_id).with_status(status, now=now)
        self._store.providers[provider_id] = updated
        return updated

    def list_by_practice(self, practice_id: str) -> List[Provider]:
        return _sorted([p for p in self._store.providers.values()
                        if p.practice_id == practice_id])

    def get_by_auth_subject(self, auth_subject: str) -> Optional[Provider]:
        subject = (auth_subject or "").strip()
        if not subject:
            return None
        for provider in _sorted(list(self._store.providers.values())):
            if provider.auth_subject == subject:
                return provider
        return None


class InMemoryCaregiverRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def create(self, caregiver: Caregiver) -> Caregiver:
        return _create(self._store.caregivers, caregiver.caregiver_id, caregiver)

    def get_by_id(self, caregiver_id: str) -> Caregiver:
        return _get(self._store.caregivers, caregiver_id)

    def update_status(self, caregiver_id: str, status: EntityStatus,
                      *, now: Optional[datetime] = None) -> Caregiver:
        updated = _get(self._store.caregivers, caregiver_id).with_status(status, now=now)
        self._store.caregivers[caregiver_id] = updated
        return updated

    def get_by_auth_subject(self, auth_subject: str) -> Optional[Caregiver]:
        subject = (auth_subject or "").strip()
        if not subject:
            return None
        for caregiver in _sorted(list(self._store.caregivers.values())):
            if caregiver.auth_subject == subject:
                return caregiver
        return None


class InMemoryChildRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def create(self, child: Child) -> Child:
        return _create(self._store.children, child.child_id, child)

    def get_by_id(self, child_id: str) -> Child:
        return _get(self._store.children, child_id)

    def update_status(self, child_id: str, status: EntityStatus,
                      *, now: Optional[datetime] = None) -> Child:
        updated = _get(self._store.children, child_id).with_status(status, now=now)
        self._store.children[child_id] = updated
        return updated


class InMemoryCaregiverChildConnectionRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def connect(self, connection: CaregiverChildConnection) -> CaregiverChildConnection:
        return _create(self._store.caregiver_child, connection.connection_id, connection)

    def get_by_id(self, connection_id: str) -> CaregiverChildConnection:
        return _get(self._store.caregiver_child, connection_id)

    def end_connection(self, connection_id: str, *,
                       status: ConnectionStatus = ConnectionStatus.ENDED,
                       now: Optional[datetime] = None) -> CaregiverChildConnection:
        ended = _get(self._store.caregiver_child, connection_id).end(status=status, now=now)
        self._store.caregiver_child[connection_id] = ended
        return ended

    def list_children_for_caregiver(self, caregiver_id: str, *, include_ended: bool = False
                                    ) -> List[CaregiverChildConnection]:
        return _sorted([c for c in self._store.caregiver_child.values()
                        if c.caregiver_id == caregiver_id and (include_ended or c.is_active)])

    def list_caregivers_for_child(self, child_id: str, *, include_ended: bool = False
                                  ) -> List[CaregiverChildConnection]:
        return _sorted([c for c in self._store.caregiver_child.values()
                        if c.child_id == child_id and (include_ended or c.is_active)])


class InMemoryProviderChildConnectionRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def connect(self, connection: ProviderChildConnection) -> ProviderChildConnection:
        return _create(self._store.provider_child, connection.connection_id, connection)

    def get_by_id(self, connection_id: str) -> ProviderChildConnection:
        return _get(self._store.provider_child, connection_id)

    def activate(self, connection_id: str,
                 *, now: Optional[datetime] = None) -> ProviderChildConnection:
        activated = _get(self._store.provider_child, connection_id).activate(now=now)
        self._store.provider_child[connection_id] = activated
        return activated

    def end_connection(self, connection_id: str, *,
                       status: ConnectionStatus = ConnectionStatus.ENDED,
                       now: Optional[datetime] = None) -> ProviderChildConnection:
        ended = _get(self._store.provider_child, connection_id).end(status=status, now=now)
        self._store.provider_child[connection_id] = ended
        return ended

    def list_children_for_provider(self, provider_id: str, *, include_ended: bool = False
                                   ) -> List[ProviderChildConnection]:
        return _sorted([c for c in self._store.provider_child.values()
                        if c.provider_id == provider_id and (include_ended or c.is_active)])

    def list_providers_for_child(self, child_id: str, *, include_ended: bool = False
                                 ) -> List[ProviderChildConnection]:
        return _sorted([c for c in self._store.provider_child.values()
                        if c.child_id == child_id and (include_ended or c.is_active)])


class InMemoryRepositories:
    """All six repositories over one store — the unit under test."""

    def __init__(self, store: Optional[InMemoryStore] = None) -> None:
        self.store = store or InMemoryStore()
        self.practices = InMemoryPracticeRepository(self.store)
        self.providers = InMemoryProviderRepository(self.store)
        self.caregivers = InMemoryCaregiverRepository(self.store)
        self.children = InMemoryChildRepository(self.store)
        self.caregiver_child = InMemoryCaregiverChildConnectionRepository(self.store)
        self.provider_child = InMemoryProviderChildConnectionRepository(self.store)
