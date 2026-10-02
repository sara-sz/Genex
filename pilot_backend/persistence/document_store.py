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
import threading
from typing import (
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    TypeVar,
    runtime_checkable,
)

T = TypeVar("T")


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
        """Overwrite an existing document wholesale.

        NOT available inside a transaction: the repository layer verifies
        existence with a read first, and Firestore forbids a read after a
        write. Use `overwrite` for a transactional status change.
        """

    def overwrite(self, collection: str, doc_id: str,
                  data: Mapping[str, object]) -> None:
        """Write a document wholesale WITHOUT first checking it exists. 0.5B.

        The difference from `set` is the absent read, and that is the whole
        point: it is the only whole-document write available inside a
        transaction.

        Added because 0.5B has a genuinely atomic multi-document requirement —
        pausing or revoking a provider connection must end that provider's
        managing-clinician assignment in the SAME transaction, or a crash
        between the two leaves an inactive connection with a live assignment
        that a later reconnection silently honours.

        Caller's obligation: having skipped the existence check, the caller
        must read the document INSIDE the transaction before writing it. That
        read is what enrolls the document in Firestore's conflict detection, so
        a competing writer causes a retry instead of a lost update. Writing
        blind from a value read before the transaction opened would be atomic
        and still wrong.
        """

    def query_equals(self, collection: str, field: str,
                     value: object) -> List[Tuple[str, Mapping[str, object]]]:
        """(doc_id, data) pairs where `field == value`, ordered by doc id."""

    def list_all(self, collection: str) -> List[Tuple[str, Mapping[str, object]]]:
        """Every (doc_id, data) pair in the collection, ordered by doc id."""

    def run_in_transaction(self, fn: "Callable[[DocumentStore], T]") -> T:
        """Run `fn` atomically. Either every write lands, or none does.

        `fn` receives a store bound to the transaction and exposing this same
        interface, so repositories can be constructed over it unchanged.

        Two constraints, both inherited from Firestore and therefore part of
        the port rather than of one implementation:

          * **All reads must precede all writes.** A read issued after a write
            in the same transaction is rejected by the server, so callers must
            gather every count and lookup first.
          * **`set` is unavailable inside a transaction.** The port's `set`
            means "replace an EXISTING document", which requires a read to
            verify existence — and that read would have to follow earlier
            writes. Rather than silently drop the existence guarantee, the
            transactional store refuses the call.

        `create` keeps its full meaning: a competing transaction that created
        the same document id causes this one to fail at commit, so uniqueness
        survives atomicity.
        """


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
        #: Guards transactional snapshot/rollback. Individual operations are
        #: NOT claimed to be thread-safe — see the class docstring.
        self._lock = threading.RLock()

    def run_in_transaction(self, fn):
        """Atomic by snapshot-and-rollback, within one process.

        This gives the ALL-OR-NOTHING property a crash-consistency test needs.
        It does NOT simulate contention between processes, and it must never
        be used to make a concurrency claim — `FakeDocumentStore` is a plain
        dict and the only evidence about racing writers comes from the real
        emulator suite.
        """
        with self._lock:
            snapshot = copy.deepcopy(self._data)
            try:
                return fn(self)
            except BaseException:
                self._data = snapshot
                raise

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

    def overwrite(self, collection: str, doc_id: str,
                  data: Mapping[str, object]) -> None:
        """No existence check, by contract — see the port.

        Deliberately NOT implemented as `set` with the check removed in a way
        that makes the fake MORE permissive than Firestore. The real adapter
        allows this inside a transaction and so does this one; the difference
        that matters is the one the fake CANNOT model — Firestore's conflict
        detection — which is why the atomicity claims are proven against the
        emulator and not here.
        """
        self._collection(collection)[doc_id] = copy.deepcopy(dict(data))

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
