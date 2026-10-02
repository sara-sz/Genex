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

    ## 0.5B additions, and why they are separate states

    DECLINED and PAUSED are ADDITIVE. Every pre-0.5B status keeps its exact
    meaning, and `is_active` — which is what every authorization path actually
    consults — still requires ACTIVE, so both new states deny by default
    without a single change to `authorize_child_access`.

    DECLINED is not ENDED. Ending a relationship means one existed; declining
    means a family said no to one that never started. A clinician who was
    refused is a different fact from one whose access finished, and merging
    them would make "was this provider ever connected to this child?"
    unanswerable.

    PAUSED is the only NON-terminal non-active state. A family suspending
    access during a treatment break must not have to re-invite the clinician
    afterwards, so the row keeps its identity, its `connection_id` and its
    `activated_at`, and resumes as the SAME relationship. That matters beyond
    convenience: `ManagingClinicianAssignment.provider_connection_id` points at
    this row, so a pause that minted a new connection would orphan the
    assignment a resume is supposed to restore.
    """

    PENDING = "pending"
    ACTIVE = "active"
    ENDED = "ended"
    REVOKED = "revoked"
    #: A family refused the invitation. Terminal — re-inviting creates a new row.
    DECLINED = "declined"
    #: Temporarily suspended, resumable, same relationship. NOT terminal.
    PAUSED = "paused"


#: Statuses from which a connection can never become active again.
#:
#: DECLINED joins ENDED and REVOKED, so `activate()` refuses all three and a
#: declined invitation cannot be force-accepted. PAUSED is deliberately absent
#: — being resumable is the entire point of it.
TERMINAL_CONNECTION_STATUSES = frozenset({
    ConnectionStatus.ENDED,
    ConnectionStatus.REVOKED,
    ConnectionStatus.DECLINED,
})

#: Statuses that grant NO clinical access to a child.
#:
#: Derived as the complement of ACTIVE rather than listed, so a status added
#: in a later slice denies access by default instead of silently granting it
#: until someone remembers to add it here.
NON_ACCESS_CONNECTION_STATUSES = frozenset(
    status for status in ConnectionStatus if status is not ConnectionStatus.ACTIVE
)


class ConnectionInitiator(str, Enum):
    """Which side asked for a provider-child relationship.

    Recorded because the two directions are authorized completely differently,
    and after the fact that difference is otherwise invisible — both produce an
    ACTIVE `ProviderChildConnection` with identical fields.

    CAREGIVER: a family invited a clinician. The caregiver is proven to hold an
    active relationship to the child, so the child id is something they already
    have access to.

    PROVIDER: a clinician redeemed an invitation the family gave them. It is
    NOT "a provider searched for a child" — there is no family lookup in this
    slice, by design. The provider supplies a token that only the family could
    have produced, and the child id comes from that token rather than from the
    request, so this path can never be used to probe for children.
    """

    CAREGIVER = "caregiver"
    PROVIDER = "provider"


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
