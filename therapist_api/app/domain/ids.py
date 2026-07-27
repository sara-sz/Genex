"""Deterministic document-id helpers for Firestore-style idempotency (correction #3).

Firestore has no SQL-style UNIQUE constraint. We enforce uniqueness by deriving
DETERMINISTIC document ids from the natural key, then using create-if-absent
inside a transaction. The same logical operation therefore maps to the same
document id, so a duplicate submission/response cannot create a second record
(and thus cannot cause a second plan mutation).

Rules:
  * idempotency_doc_id(key)                     — one record per idempotency key
  * recommendation_response_doc_id(rec_id)      — exactly ONE terminal response
                                                  per recommendation
"""

from __future__ import annotations

import hashlib

_HASH_LEN = 32  # 128 bits of hex — collision-safe for our scale


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def idempotency_doc_id(idempotency_key: str) -> str:
    """Deterministic id for an idempotency record derived from the client key."""
    key = (idempotency_key or "").strip()
    if not key:
        raise ValueError("idempotency_key must be a non-empty string.")
    return "idem_" + _sha256_hex(key)[:_HASH_LEN]


def recommendation_response_doc_id(recommendation_id: str) -> str:
    """Deterministic id so a recommendation can have exactly one terminal response.

    Because the id depends only on the recommendation id (not on the parent's
    chosen answer), a duplicate accept/decline collides on the SAME document and
    returns the original stored result instead of creating a second one.
    """
    rec_id = (recommendation_id or "").strip()
    if not rec_id:
        raise ValueError("recommendation_id must be a non-empty string.")
    return "resp_" + _sha256_hex(rec_id)[:_HASH_LEN]


def key_hash(idempotency_key: str) -> str:
    """Safe hash of the raw Idempotency-Key (stored instead of the raw key)."""
    return _sha256_hex((idempotency_key or "").strip())


def canonical_request_hash(*parts: object) -> str:
    """Collision-resistant hash over the canonical operation tuple.

    The order of `parts` is fixed by the caller (actor, action, child,
    assignment, body...). Any difference in actor/action/target/body yields a
    different hash, so a reused key with a different request is detectable.
    """
    import json

    payload = json.dumps(list(parts), sort_keys=True, separators=(",", ":"), default=str)
    return _sha256_hex(payload)


def audit_event_id(request_hash: str, key: str) -> str:
    """Deterministic audit-event id bound to the operation + key (one per op)."""
    return "aud_" + _sha256_hex(request_hash + "|" + (key or ""))[:_HASH_LEN]


def derived_id(prefix: str, *seed: str) -> str:
    """Deterministic id `<prefix>_<sha>` from a stable seed (Firestore-mappable)."""
    return f"{prefix}_" + _sha256_hex("|".join(seed))[:_HASH_LEN]
