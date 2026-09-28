"""pilot_backend/domain/enums.py — status, role and visibility vocabularies."""

from __future__ import annotations

from enum import Enum


class EntityStatus(str, Enum):
    """Lifecycle of a Practice, Provider, Caregiver or Child record.

    There is no DELETED state. Clinical records are retired, never erased —
    an ended relationship or a closed account must remain auditable.
    """

    ACTIVE = "active"
    INACTIVE = "inactive"
    ARCHIVED = "archived"


class ConnectionStatus(str, Enum):
    """Lifecycle of a relationship record.

    ENDED is terminal and keeps the row: `ended_at` is stamped and the history
    stays queryable. Nothing in this package deletes a connection.
    """

    PENDING = "pending"
    ACTIVE = "active"
    ENDED = "ended"
    REVOKED = "revoked"


TERMINAL_CONNECTION_STATUSES = frozenset({ConnectionStatus.ENDED, ConnectionStatus.REVOKED})


class CaregiverRelationship(str, Enum):
    """How a caregiver relates to a child.

    Deliberately coarse for v1. It records the relationship, and never grants
    access by itself — authorization is the connection's job.
    """

    PARENT = "parent"
    GUARDIAN = "guardian"
    OTHER_CAREGIVER = "other_caregiver"


class ProviderDiscipline(str, Enum):
    """Clinical discipline of a provider.

    A DISCIPLINE IS NOT A DEVELOPMENTAL DOMAIN. SLP is not
    talking_and_communicating, OT is not fine_motor, PT is not gross_motor —
    the mapping is many-to-many and lives in the Parent taxonomy, not here.
    The pilot ships SLP first; the others exist so adding one is not a schema
    change.
    """

    SLP = "slp"
    OT = "ot"
    PT = "pt"


class Visibility(str, Enum):
    """Who a piece of data belongs to.

    Declared on the model so the boundary is enforceable server-side later.
    Hiding a field in a frontend is not a privacy boundary.
    """

    #: Safe to return to a caregiver: plans, activity instructions, guidance
    #: written for the family, their own feedback.
    PARENT_VISIBLE = "parent_visible"

    #: Clinician-only: private notes, internal review state, clinical
    #: interpretation, and later RTM clinician time. Never returned to a
    #: caregiver by any endpoint.
    THERAPIST_ONLY = "therapist_only"

    #: Identifiers, timestamps and actor references. Not clinical content and
    #: not parent-facing; retained for audit.
    SYSTEM_AUDIT = "system_audit"
