"""In-memory repository for tests. Single-threaded, deep-copied on read/write."""

from __future__ import annotations

import copy
import threading
from typing import Callable, Dict, List, Tuple, TypeVar

from .interface import CollaborationRepository, RecordNotFound

T = TypeVar("T")


class InMemoryRepository(CollaborationRepository):
    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, Dict]] = {}
        self._lock = threading.RLock()

    def run_in_transaction(self, fn: "Callable[[CollaborationRepository], T]") -> T:
        # Re-entrant lock: `fn` may call get/set/query/create_if_absent, which
        # also acquire the lock. Holding it for the whole `fn` is the critical
        # section that prevents concurrent double-approval.
        with self._lock:
            return fn(self)

    def _col(self, collection: str) -> Dict[str, Dict]:
        return self._data.setdefault(collection, {})

    def get(self, collection: str, doc_id: str) -> Dict:
        with self._lock:
            col = self._col(collection)
            if doc_id not in col:
                raise RecordNotFound(f"{collection}/{doc_id}")
            return copy.deepcopy(col[doc_id])

    def exists(self, collection: str, doc_id: str) -> bool:
        with self._lock:
            return doc_id in self._col(collection)

    def create_if_absent(self, collection: str, doc_id: str, data: Dict) -> Tuple[bool, Dict]:
        with self._lock:  # stands in for a Firestore transaction
            col = self._col(collection)
            if doc_id in col:
                return False, copy.deepcopy(col[doc_id])
            col[doc_id] = copy.deepcopy(data)
            return True, copy.deepcopy(col[doc_id])

    def set(self, collection: str, doc_id: str, data: Dict) -> None:
        with self._lock:
            self._col(collection)[doc_id] = copy.deepcopy(data)

    def query(self, collection: str, **equals: object) -> List[Dict]:
        with self._lock:
            out: List[Dict] = []
            for doc in self._col(collection).values():
                if all(doc.get(k) == v for k, v in equals.items()):
                    out.append(copy.deepcopy(doc))
            return out
