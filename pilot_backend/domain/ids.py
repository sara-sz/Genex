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
#: BACKEND 0.2. Audit events and record revisions are identified the same way
#: as entities — generated, opaque, never derived from their content. An audit
#: event id derived from what it describes would leak the description.
AUDIT_EVENT_PREFIX = "audt"
REVISION_PREFIX = "revn"
#: PRE-PHI 0.3. The one mutable pilot record.
CHILD_CONTEXT_PREFIX = "cctx"
#: 0.4A longitudinal identity. Claims are NOT listed here: their ids are
#: deterministic by design (see domain/identity_claims.py), which is the whole
#: uniqueness mechanism, so they must never be minted from a uuid.
SOURCE_LINK_PREFIX = "sslk"
MANAGING_CLINICIAN_PREFIX = "mcas"

ALL_PREFIXES = (
    PRACTICE_PREFIX,
    PROVIDER_PREFIX,
    CAREGIVER_PREFIX,
    CHILD_PREFIX,
    CAREGIVER_CHILD_CONNECTION_PREFIX,
    PROVIDER_CHILD_CONNECTION_PREFIX,
    AUDIT_EVENT_PREFIX,
    REVISION_PREFIX,
    CHILD_CONTEXT_PREFIX,
    SOURCE_LINK_PREFIX,
    MANAGING_CLINICIAN_PREFIX,
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


def new_audit_event_id() -> str:
    return new_id(AUDIT_EVENT_PREFIX)


def new_revision_id() -> str:
    return new_id(REVISION_PREFIX)


def new_child_context_id() -> str:
    return new_id(CHILD_CONTEXT_PREFIX)


def new_source_link_id() -> str:
    return new_id(SOURCE_LINK_PREFIX)


def new_managing_clinician_id() -> str:
    return new_id(MANAGING_CLINICIAN_PREFIX)


def entity_type_of(identifier: str) -> Optional[str]:
    """The prefix of an identifier, for diagnostics. Never an authorization input."""
    head = str(identifier or "").split("_", 1)[0]
    return head if head in ALL_PREFIXES else None
