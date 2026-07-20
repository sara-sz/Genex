"""Abstract collaboration-store repository.

Deliberately small. The key operation is `create_if_absent`, which maps to a
Firestore transaction that creates a document only if its (deterministic) id is
free — the mechanism we use to make recommendation creation and parent responses
idempotent without SQL unique constraints.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Tuple


class RecordNotFound(KeyError):
    pass


class CollaborationRepository(ABC):
    @abstractmethod
    def get(self, collection: str, doc_id: str) -> Dict:
        raise NotImplementedError

    @abstractmethod
    def exists(self, collection: str, doc_id: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def create_if_absent(self, collection: str, doc_id: str, data: Dict) -> Tuple[bool, Dict]:
        """Create `data` at `doc_id` iff absent.

        Returns (created, stored):
          * (True, data)  when the document was newly created
          * (False, existing) when it already existed (idempotent no-op) — the
            ORIGINAL stored document is returned, never a duplicate.

        Must be atomic with respect to concurrent callers (a Firestore
        transaction in the real implementation).
        """
        raise NotImplementedError

    @abstractmethod
    def set(self, collection: str, doc_id: str, data: Dict) -> None:
        raise NotImplementedError

    @abstractmethod
    def query(self, collection: str, **equals: object) -> List[Dict]:
        raise NotImplementedError
