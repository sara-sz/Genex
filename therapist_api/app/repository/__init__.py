"""Firestore repository interface + in-memory test implementation.

No live Firestore connection exists in this phase. Routes depend on the abstract
interface; tests use the in-memory implementation. The interface encodes the
idempotency primitive we rely on: `create_if_absent` on a DETERMINISTIC document
id (correction #3).
"""

from .interface import CollaborationRepository, RecordNotFound
from .memory import InMemoryRepository

__all__ = ["CollaborationRepository", "RecordNotFound", "InMemoryRepository"]
