"""pilot_backend/domain/ids.py — stable application identifiers.

Every pilot entity is keyed by a random UUID4 with a short type prefix. The ID
is generated, never derived.

## Why not derive IDs from a natural key

The therapist service derives some document ids from a natural key on purpose
(`therapist_api/app/domain/ids.py`) — that is an IDEMPOTENCY mechanism, and it
is correct there: the same logical operation must collide on the same document.

Identity is the opposite problem. An identifier derived from a name or an email
changes when the person changes their name, collides when two people share one,
and leaks the value into logs, URLs and error messages. So these are random and
opaque, and the tests assert that a person's name cannot be recovered from, or
used to predict, their ID.

## Prefixes

The prefix is a readability aid in logs and fixtures, not a parsing contract.
Nothing branches on it; `entity_type_of` exists for diagnostics only.
"""

from __future__ import annotations

import uuid
from typing import Optional

PRACTICE_PREFIX = "prac"
PROVIDER_PREFIX = "prov"
CAREGIVER_PREFIX = "cgvr"
CHILD_PREFIX = "chld"
CAREGIVER_CHILD_CONNECTION_PREFIX = "ccxn"
PROVIDER_CHILD_CONNECTION_PREFIX = "pcxn"

ALL_PREFIXES = (
    PRACTICE_PREFIX,
    PROVIDER_PREFIX,
    CAREGIVER_PREFIX,
    CHILD_PREFIX,
    CAREGIVER_CHILD_CONNECTION_PREFIX,
    PROVIDER_CHILD_CONNECTION_PREFIX,
)


def new_id(prefix: str) -> str:
    """A fresh opaque identifier: `<prefix>_<uuid4 hex>`."""
    key = (prefix or "").strip()
    if key not in ALL_PREFIXES:
        raise ValueError(f"unknown id prefix {prefix!r}; expected one of {ALL_PREFIXES}")
    return f"{key}_{uuid.uuid4().hex}"


def new_practice_id() -> str:
    return new_id(PRACTICE_PREFIX)


def new_provider_id() -> str:
    return new_id(PROVIDER_PREFIX)


def new_caregiver_id() -> str:
    return new_id(CAREGIVER_PREFIX)


def new_child_id() -> str:
    return new_id(CHILD_PREFIX)


def new_caregiver_child_connection_id() -> str:
    return new_id(CAREGIVER_CHILD_CONNECTION_PREFIX)


def new_provider_child_connection_id() -> str:
    return new_id(PROVIDER_CHILD_CONNECTION_PREFIX)


def entity_type_of(identifier: str) -> Optional[str]:
    """The prefix of an identifier, for diagnostics. Never an authorization input."""
    head = str(identifier or "").split("_", 1)[0]
    return head if head in ALL_PREFIXES else None
