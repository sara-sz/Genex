"""pilot_backend.persistence — Firestore-shaped document persistence."""

from .document_store import (
    DocumentStore,
    DocumentStoreError,
    FakeDocumentStore,
)
from .collections import (
    COLLECTIONS,
    PILOT_COLLECTION_PREFIX,
    audit_collection,
    collection_for,
)
from .codecs import CodecError, decode, encode
from .firestore_repos import FirestoreAuditEventRepository, FirestoreRepositories

__all__ = [
    "DocumentStore",
    "DocumentStoreError",
    "FakeDocumentStore",
    "COLLECTIONS",
    "PILOT_COLLECTION_PREFIX",
    "collection_for",
    "audit_collection",
    "CodecError",
    "encode",
    "decode",
    "FirestoreRepositories",
    "FirestoreAuditEventRepository",
]
