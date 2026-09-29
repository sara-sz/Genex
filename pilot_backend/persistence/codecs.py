"""pilot_backend/persistence/codecs.py — explicit document serialization.

## No silent field dropping, in either direction

Encoding iterates the dataclass's OWN field list, so a field added to an entity
is written automatically and cannot be forgotten. Decoding requires the
document's key set to equal the expected field set EXACTLY — an unknown key and
a missing key are both errors.

That strictness is the point. The failure this prevents is a schema change that
half-lands: a field added to the model, written by the new code, silently
dropped by an old decoder, and then written back as absent. For a relationship
row or a finalized revision, that is data loss that looks like success.

Loud decode failures are also how a corrupted or hand-edited document announces
itself, instead of quietly deserialising into a record with default values —
and a `ConnectionStatus` defaulting to ACTIVE on a malformed row would be an
authorization failure, not a data failure.

## Timestamps

Always stored as ISO-8601 strings with an explicit UTC offset, always decoded
back to timezone-aware datetimes. A naive datetime is rejected on the way in
rather than assumed to be UTC: an assumption about a timezone is how a session
appears to end before it began.

## Enums

Stored as their `.value`, decoded by looking the value up in the enum class. An
unrecognised value raises rather than falling back — a status this code does
not understand must not be treated as one it does.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Type

from ..audit.events import AuditAction, AuditEvent, AuditResult
from ..domain.child_context import ChildContextRecord
from ..domain.connections import CaregiverChildConnection, ProviderChildConnection
from ..domain.entities import Caregiver, Child, Practice, Provider
from ..domain.enums import (
    CaregiverRelationship,
    ConnectionStatus,
    EntityStatus,
    ProviderDiscipline,
)
from ..domain.roles import ActorRole
from ..revision.records import RecordState, Revision


class CodecError(ValueError):
    """A document could not be encoded or decoded.

    PHI-safe: names the type and the offending FIELD, never the field's value.
    """

    PHI_SAFE_MESSAGE = True


# -- field kinds -----------------------------------------------------------

class Kind:
    """Base for the explicit per-field handling rules."""

    optional = False

    def to_doc(self, value: Any, field: str) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def from_doc(self, value: Any, field: str) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError


class _Str(Kind):
    def __init__(self, optional: bool = False) -> None:
        self.optional = optional

    def to_doc(self, value: Any, field: str) -> Any:
        if value is None:
            if not self.optional:
                raise CodecError(f"{field}: required string is None")
            return None
        if not isinstance(value, str):
            raise CodecError(f"{field}: expected a string")
        return value

    def from_doc(self, value: Any, field: str) -> Any:
        if value is None:
            if not self.optional:
                raise CodecError(f"{field}: required string is null")
            return None
        if not isinstance(value, str):
            raise CodecError(f"{field}: expected a string")
        return value


class _Int(Kind):
    def to_doc(self, value: Any, field: str) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            raise CodecError(f"{field}: expected an integer")
        return value

    def from_doc(self, value: Any, field: str) -> Any:
        return self.to_doc(value, field)


class _DateTime(Kind):
    def __init__(self, optional: bool = False) -> None:
        self.optional = optional

    def to_doc(self, value: Any, field: str) -> Any:
        if value is None:
            if not self.optional:
                raise CodecError(f"{field}: required timestamp is None")
            return None
        if not isinstance(value, datetime):
            raise CodecError(f"{field}: expected a datetime")
        if value.tzinfo is None:
            raise CodecError(f"{field}: refusing to store a naive datetime")
        return value.astimezone(timezone.utc).isoformat()

    def from_doc(self, value: Any, field: str) -> Any:
        if value is None:
            if not self.optional:
                raise CodecError(f"{field}: required timestamp is null")
            return None
        if not isinstance(value, str):
            raise CodecError(f"{field}: expected an ISO-8601 string")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise CodecError(f"{field}: not a valid ISO-8601 timestamp")
        if parsed.tzinfo is None:
            raise CodecError(f"{field}: stored timestamp has no timezone")
        return parsed.astimezone(timezone.utc)


class _EnumKind(Kind):
    def __init__(self, enum_cls: Type[Enum], optional: bool = False) -> None:
        self.enum_cls = enum_cls
        self.optional = optional

    def to_doc(self, value: Any, field: str) -> Any:
        if value is None:
            if not self.optional:
                raise CodecError(f"{field}: required enum is None")
            return None
        if not isinstance(value, self.enum_cls):
            raise CodecError(f"{field}: expected {self.enum_cls.__name__}")
        return value.value

    def from_doc(self, value: Any, field: str) -> Any:
        if value is None:
            if not self.optional:
                raise CodecError(f"{field}: required enum is null")
            return None
        try:
            return self.enum_cls(value)
        except ValueError:
            raise CodecError(f"{field}: unrecognised {self.enum_cls.__name__} value")


class _StrMap(Kind):
    def to_doc(self, value: Any, field: str) -> Any:
        if not isinstance(value, Mapping):
            raise CodecError(f"{field}: expected a mapping")
        out = {}
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise CodecError(f"{field}: mapping must be string to string")
            out[key] = item
        return out

    def from_doc(self, value: Any, field: str) -> Any:
        return self.to_doc(value, field)


STR, OPT_STR = _Str(), _Str(optional=True)
INT = _Int()
DT, OPT_DT = _DateTime(), _DateTime(optional=True)
STR_MAP = _StrMap()


#: Per-type field handling. A test asserts each spec covers exactly the
#: dataclass's fields, so a new field cannot be added without a decision here.
SPECS: Dict[type, Dict[str, Kind]] = {
    Practice: {
        "practice_id": STR, "legal_name": STR,
        "status": _EnumKind(EntityStatus),
        "created_at": DT, "updated_at": DT,
        "created_by_actor_id": OPT_STR, "schema_version": STR,
    },
    Provider: {
        "provider_id": STR, "practice_id": STR,
        "discipline": _EnumKind(ProviderDiscipline),
        "display_name": STR, "auth_subject": OPT_STR,
        "status": _EnumKind(EntityStatus),
        "created_at": DT, "updated_at": DT,
        "created_by_actor_id": OPT_STR, "schema_version": STR,
    },
    Caregiver: {
        "caregiver_id": STR, "display_name": STR, "auth_subject": OPT_STR,
        "status": _EnumKind(EntityStatus),
        "created_at": DT, "updated_at": DT,
        "created_by_actor_id": OPT_STR, "schema_version": STR,
    },
    Child: {
        "child_id": STR,
        "status": _EnumKind(EntityStatus),
        "created_at": DT, "updated_at": DT,
        "created_by_actor_id": OPT_STR, "schema_version": STR,
    },
    CaregiverChildConnection: {
        "connection_id": STR, "caregiver_id": STR, "child_id": STR,
        "relationship_role": _EnumKind(CaregiverRelationship),
        "status": _EnumKind(ConnectionStatus),
        "created_at": DT, "updated_at": DT, "ended_at": OPT_DT,
        "created_by_actor_id": OPT_STR, "schema_version": STR,
    },
    ProviderChildConnection: {
        "connection_id": STR, "provider_id": STR, "child_id": STR,
        "practice_id": STR,
        "status": _EnumKind(ConnectionStatus),
        "permissions": STR,
        "created_at": DT, "updated_at": DT,
        "activated_at": OPT_DT, "ended_at": OPT_DT,
        "created_by_actor_id": OPT_STR, "schema_version": STR,
    },
    AuditEvent: {
        "event_id": STR, "occurred_at": DT,
        "action": _EnumKind(AuditAction), "result": _EnumKind(AuditResult),
        "resource_type": STR, "resource_id": OPT_STR, "child_id": OPT_STR,
        "actor_application_id": OPT_STR, "actor_auth_subject": OPT_STR,
        "actor_role": _EnumKind(ActorRole, optional=True),
        "request_id": STR, "metadata": STR_MAP, "schema_version": STR,
    },
    ChildContextRecord: {
        "record_id": STR, "child_id": STR, "content_ref": STR,
        "current_revision_id": OPT_STR, "current_version": INT,
        "created_at": DT, "updated_at": DT,
        "created_by_actor_id": OPT_STR,
        "last_actor_role": _EnumKind(ActorRole, optional=True),
        "schema_version": STR,
    },
    Revision: {
        "revision_id": STR, "record_id": STR, "version": INT,
        "state": _EnumKind(RecordState), "created_at": DT,
        "actor_application_id": STR, "actor_role": _EnumKind(ActorRole),
        "content_ref": STR, "supersedes_revision_id": OPT_STR,
        "amendment_reason": STR, "finalized_at": OPT_DT,
        "schema_version": STR,
    },
}


def _spec_for(cls: type) -> Dict[str, Kind]:
    try:
        return SPECS[cls]
    except KeyError:
        raise CodecError(f"no codec registered for {cls.__name__}")


def encode(record: Any) -> Dict[str, Any]:
    """Serialize a record to a plain document.

    Drives off the dataclass's own fields, so completeness is structural: a
    field present on the model is always written.
    """
    spec = _spec_for(type(record))
    document: Dict[str, Any] = {}
    for f in dataclass_fields(record):
        kind = spec.get(f.name)
        if kind is None:
            raise CodecError(f"{type(record).__name__}.{f.name} has no codec rule")
        document[f.name] = kind.to_doc(getattr(record, f.name), f.name)
    return document


def decode(cls: type, document: Mapping[str, Any]) -> Any:
    """Deserialize a document, refusing any key-set mismatch."""
    spec = _spec_for(cls)
    expected = {f.name for f in dataclass_fields(cls)}
    present = set(document)

    missing = sorted(expected - present)
    unknown = sorted(present - expected)
    if missing:
        raise CodecError(f"{cls.__name__}: document is missing fields: {', '.join(missing)}")
    if unknown:
        raise CodecError(f"{cls.__name__}: document has unknown fields: {', '.join(unknown)}")

    kwargs = {name: spec[name].from_doc(document[name], name) for name in expected}
    return cls(**kwargs)


def registered_types() -> Dict[str, type]:
    """Diagnostics/tests: every type this module can round-trip."""
    return {cls.__name__: cls for cls in SPECS}
