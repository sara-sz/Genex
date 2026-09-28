"""pilot_backend/domain/entities.py — the four core pilot entities.

Practice, Provider, Caregiver, Child.

## Application identity is not authentication identity

Every entity carries its own generated `*_id`. `auth_subject` is a SEPARATE,
nullable field holding whatever an approved identity provider eventually issues.
Keeping them apart means the production auth decision — Identity Platform or
otherwise, still owned by the HIPAA workstream — maps onto these records
without touching them, and a person can change provider without changing
identity.

Nothing here imports Firebase or any auth SDK. `auth_subject` is a plain
string, and a test asserts the domain layer has no auth dependency.

## Child has no owner field

`Child` deliberately carries NO `caregiver_id`. The therapist service's
`Child.parent_id` is exactly the shape that makes multi-caregiver support a
data migration rather than a feature. Ownership lives in
`CaregiverChildConnection`, so a second caregiver is a new row.

## Minimal child fields

No PHI. No name, date of birth, diagnosis or clinical detail — the pilot only
needs a stable identity to hang relationships from. The Parent service is
already name-blind at rest, and this keeps that property.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Optional

from .enums import EntityStatus, ProviderDiscipline, Visibility
from .ids import new_caregiver_id, new_child_id, new_practice_id, new_provider_id

SCHEMA_VERSION = "october-pilot-0.1"


def utc_now() -> datetime:
    """Timezone-aware UTC timestamp. Injected by callers in tests for determinism."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Practice:
    """A clinical organisation. Providers belong to exactly one in v1."""

    practice_id: str
    legal_name: str
    status: EntityStatus = EntityStatus.ACTIVE
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    created_by_actor_id: Optional[str] = None
    schema_version: str = SCHEMA_VERSION

    #: `legal_name` is organisational, not clinical, and is safe to show a
    #: family ("your provider works at ..."). It is never an identifier.
    VISIBILITY = Visibility.PARENT_VISIBLE

    @staticmethod
    def create(legal_name: str, *, actor_id: Optional[str] = None,
               now: Optional[datetime] = None) -> "Practice":
        stamp = now or utc_now()
        return Practice(
            practice_id=new_practice_id(),
            legal_name=legal_name,
            created_at=stamp,
            updated_at=stamp,
            created_by_actor_id=actor_id,
        )

    def with_status(self, status: EntityStatus, *, now: Optional[datetime] = None) -> "Practice":
        return replace(self, status=status, updated_at=now or utc_now())


@dataclass(frozen=True)
class Provider:
    """A treating clinician."""

    provider_id: str
    practice_id: str
    discipline: ProviderDiscipline
    display_name: str
    #: Set by an approved identity provider later. None until then — a provider
    #: record can exist before anyone has signed in.
    auth_subject: Optional[str] = None
    status: EntityStatus = EntityStatus.ACTIVE
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    created_by_actor_id: Optional[str] = None
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.PARENT_VISIBLE  # name + discipline, not caseload

    @staticmethod
    def create(practice_id: str, discipline: ProviderDiscipline, display_name: str, *,
               auth_subject: Optional[str] = None, actor_id: Optional[str] = None,
               now: Optional[datetime] = None) -> "Provider":
        stamp = now or utc_now()
        return Provider(
            provider_id=new_provider_id(),
            practice_id=practice_id,
            discipline=discipline,
            display_name=display_name,
            auth_subject=auth_subject,
            created_at=stamp,
            updated_at=stamp,
            created_by_actor_id=actor_id,
        )

    def with_status(self, status: EntityStatus, *, now: Optional[datetime] = None) -> "Provider":
        return replace(self, status=status, updated_at=now or utc_now())

    def with_auth_subject(self, subject: str, *, now: Optional[datetime] = None) -> "Provider":
        """Bind an authentication subject WITHOUT changing the application id."""
        return replace(self, auth_subject=subject, updated_at=now or utc_now())


@dataclass(frozen=True)
class Caregiver:
    """A parent or other caregiver."""

    caregiver_id: str
    display_name: str
    auth_subject: Optional[str] = None
    status: EntityStatus = EntityStatus.ACTIVE
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    created_by_actor_id: Optional[str] = None
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.PARENT_VISIBLE

    @staticmethod
    def create(display_name: str, *, auth_subject: Optional[str] = None,
               actor_id: Optional[str] = None, now: Optional[datetime] = None) -> "Caregiver":
        stamp = now or utc_now()
        return Caregiver(
            caregiver_id=new_caregiver_id(),
            display_name=display_name,
            auth_subject=auth_subject,
            created_at=stamp,
            updated_at=stamp,
            created_by_actor_id=actor_id,
        )

    def with_status(self, status: EntityStatus, *, now: Optional[datetime] = None) -> "Caregiver":
        return replace(self, status=status, updated_at=now or utc_now())

    def with_auth_subject(self, subject: str, *, now: Optional[datetime] = None) -> "Caregiver":
        return replace(self, auth_subject=subject, updated_at=now or utc_now())


@dataclass(frozen=True)
class Child:
    """A child. Identity only — no PHI, and deliberately no owner field.

    Who may see this child is answered by the connection tables, never by a
    column here. That is what lets a second caregiver or a second provider be
    a new row instead of a migration.
    """

    child_id: str
    status: EntityStatus = EntityStatus.ACTIVE
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    created_by_actor_id: Optional[str] = None
    schema_version: str = SCHEMA_VERSION

    #: Even the bare identity row is audit-tier: it is not clinical content,
    #: and access to it is mediated entirely by connections.
    VISIBILITY = Visibility.SYSTEM_AUDIT

    @staticmethod
    def create(*, actor_id: Optional[str] = None,
               now: Optional[datetime] = None) -> "Child":
        stamp = now or utc_now()
        return Child(
            child_id=new_child_id(),
            created_at=stamp,
            updated_at=stamp,
            created_by_actor_id=actor_id,
        )

    def with_status(self, status: EntityStatus, *, now: Optional[datetime] = None) -> "Child":
        return replace(self, status=status, updated_at=now or utc_now())
