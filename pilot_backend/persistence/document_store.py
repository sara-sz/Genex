"""pilot_backend/persistence/document_store.py — the storage port and its fake.

## Why a port rather than the Firestore SDK

`DocumentStore` is the complete set of operations the pilot needs from a
document database: create-if-absent, get by id, overwrite by id, equality
query, list. Nothing else — no joins, no partial updates, no transactions
spanning collections, no server-side aggregation. That is deliberately the
intersection the therapist service already proved against a Firestore-shaped
store, so a real implementation is a thin translation.

Writing the repositories against this port rather than `google.cloud.firestore`
buys three things that matter for this phase specifically:

  * the security tests run on a dependency-pure CI job with no credentials and
    no network, so authorization and audit behaviour is proven in CI rather
    than asserted in a document;
  * BACKEND 0.1's structural guarantee that this package imports no database
    driver stays true and unweakened;
  * binding a real Firestore client is a provisioning decision that the HIPAA
    workstream has not approved — and there is currently no production
    Firestore database to bind to.

A real adapter is a class with these five methods delegating to a
`google.cloud.firestore.Client`, constructed at a composition root OUTSIDE this
package. Because the port matches the Firestore emulator's semantics, the same
repositories run against the emulator without change.

## Determinism

`query_equals` and `list_all` return results sorted by document id. Firestore
does not promise insertion order and neither does this fake — a test that
passes here because a dict preserved insertion order would fail in production,
which is the class of bug BACKEND 0.1 already hit once.
"""

from __future__ import annotations

import copy
from typing import Dict, List, Mapping, Optional, Protocol, Tuple, runtime_checkable


class DocumentStoreError(Exception):
    """A storage-layer failure. PHI-safe: names the collection, never the data."""

    PHI_SAFE_MESSAGE = True


@runtime_checkable
class DocumentStore(Protocol):
    """Narrow document-database contract. Firestore-shaped, Firestore-free."""

    def create(self, collection: str, doc_id: str, data: Mapping[str, object]) -> None:
        """Write a new document. Must raise if `doc_id` already exists."""

    def get(self, collection: str, doc_id: str) -> Optional[Mapping[str, object]]:
        """Return the document, or None if absent. Never raises for absence."""

    def set(self, collection: str, doc_id: str, data: Mapping[str, object]) -> None:
        """Overwrite an existing document wholesale."""

    def query_equals(self, collection: str, field: str,
                     value: object) -> List[Tuple[str, Mapping[str, object]]]:
        """(doc_id, data) pairs where `field == value`, ordered by doc id."""

    def list_all(self, collection: str) -> List[Tuple[str, Mapping[str, object]]]:
        """Every (doc_id, data) pair in the collection, ordered by doc id."""


class FakeDocumentStore:
    """In-memory `DocumentStore` for tests and local development.

    Emulator-compatible by construction: it implements exactly the port, so a
    suite written against it runs unchanged against the Firestore emulator or a
    real database. It deep-copies on both read and write, so a caller holding a
    returned dict cannot mutate stored state — the same isolation a network
    round-trip would give, and the reason a test cannot accidentally depend on
    shared references.
    """

    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, Dict[str, object]]] = {}

    def _collection(self, collection: str) -> Dict[str, Dict[str, object]]:
        return self._data.setdefault(collection, {})

    def create(self, collection: str, doc_id: str, data: Mapping[str, object]) -> None:
        bucket = self._collection(collection)
        if doc_id in bucket:
            raise DocumentStoreError(f"document already exists in {collection}")
        bucket[doc_id] = copy.deepcopy(dict(data))

    def get(self, collection: str, doc_id: str) -> Optional[Mapping[str, object]]:
        found = self._collection(collection).get(doc_id)
        return copy.deepcopy(found) if found is not None else None

    def set(self, collection: str, doc_id: str, data: Mapping[str, object]) -> None:
        bucket = self._collection(collection)
        if doc_id not in bucket:
            raise DocumentStoreError(f"document does not exist in {collection}")
        bucket[doc_id] = copy.deepcopy(dict(data))

    def query_equals(self, collection: str, field: str,
                     value: object) -> List[Tuple[str, Mapping[str, object]]]:
        return [
            (doc_id, copy.deepcopy(data))
            for doc_id, data in sorted(self._collection(collection).items())
            if data.get(field) == value
        ]

    def list_all(self, collection: str) -> List[Tuple[str, Mapping[str, object]]]:
        return [
            (doc_id, copy.deepcopy(data))
            for doc_id, data in sorted(self._collection(collection).items())
        ]

    # -- test affordance ----------------------------------------------------

    def collections(self) -> List[str]:
        """Collection names that have been touched. Used by separation tests."""
        return sorted(self._data)
