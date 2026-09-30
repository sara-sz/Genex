"""pilot_backend/domain/managing_clinician.py — who clinically owns a child.

Exactly one managing clinician per child at any time. This record is what
later authorizes ClinicalGoal approval, MonthlyGoalAllocation priority and RTM
episode management — none of which exist yet and none of which are
implemented here.

## Append-only, never rewritten

A transfer does not edit the previous assignment. It ends it and creates a new
one that points back at its predecessor, so "who was the managing clinician on
12 October?" is answerable from the record rather than inferred from the
current state. Last-writer-wins is refused explicitly: the write-time claim in
`identity_claims` makes two concurrent assignments impossible rather than
merely unlikely.

## Validated against the real relationship

An assignment requires an ACTIVE `ProviderChildConnection` for that provider
and child, and the assignment's `practice_id` must match the connection's
practice of record — not the provider's current practice. The connection
denormalises `practice_id` precisely so an employer change cannot silently
rewrite who held the relationship, and this validation preserves that.

## Not an RTM transfer

Recorded as forward debt, not implemented here: an open RTM episode will NOT
silently transfer when the managing clinician changes. The episode must be
explicitly closed and a new one opened under the new clinician. No RTM object
exists in this slice.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Optional

from .entities import SCHEMA_VERSION, utc_now
from .enums import Visibility
from .ids import new_managing_clinician_id


class ManagingClinicianStatus(str, Enum):
    ACTIVE = "active"
    ENDED = "ended"
    TRANSFERRED = "transferred"


TERMINAL_ASSIGNMENT_STATUSES = frozenset({
    ManagingClinicianStatus.ENDED, ManagingClinicianStatus.TRANSFERRED,
})


class ManagingClinicianError(ValueError):
    """Invalid managing-clinician transition or validation failure.

    PHI-safe: names the rule that failed, never a person or clinical detail.
    """

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class ManagingClinicianAssignment:
    """One period during which one provider clinically owned one child."""

    assignment_id: str
    child_id: str
    provider_id: str
    #: Practice of record, copied from the ProviderChildConnection at
    #: assignment time — not read live from the Provider.
    practice_id: str
    #: The connection that authorised this assignment, for audit.
    provider_connection_id: str
    status: ManagingClinicianStatus = ManagingClinicianStatus.ACTIVE
    effective_from: datetime = field(default_factory=utc_now)
    effective_to: Optional[datetime] = None
    assigned_by_actor_id: Optional[str] = None
    assigned_by_role: Optional[str] = None
    reason: str = ""
    end_reason: str = ""
    ended_by_actor_id: Optional[str] = None
    claim_id: str = ""
    supersedes_assignment_id: Optional[str] = None
    superseded_by_assignment_id: Optional[str] = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    created_by_actor_id: Optional[str] = None
    schema_version: str = SCHEMA_VERSION

    #: Who clinically owns a child is not parent-facing content, and it is not
    #: clinical interpretation either. It is an audit-tier organisational fact.
    VISIBILITY = Visibility.SYSTEM_AUDIT

    @property
    def is_active(self) -> bool:
        return (self.status is ManagingClinicianStatus.ACTIVE
                and self.effective_to is None)

    @staticmethod
    def create(child_id: str, provider_id: str, practice_id: str, *,
               provider_connection_id: str,
               actor_id: Optional[str] = None,
               actor_role: Optional[str] = None,
               reason: str = "",
               claim_id: str = "",
               supersedes_assignment_id: Optional[str] = None,
               now: Optional[datetime] = None) -> "ManagingClinicianAssignment":
        stamp = now or utc_now()
        for label, value in (("child_id", child_id), ("provider_id", provider_id),
                             ("practice_id", practice_id),
                             ("provider_connection_id", provider_connection_id)):
            if not (value or "").strip():
                raise ManagingClinicianError(f"managing clinician requires {label}")
        return ManagingClinicianAssignment(
            assignment_id=new_managing_clinician_id(),
            child_id=child_id,
            provider_id=provider_id,
            practice_id=practice_id,
            provider_connection_id=provider_connection_id,
            effective_from=stamp,
            assigned_by_actor_id=actor_id,
            assigned_by_role=actor_role,
            reason=reason,
            claim_id=claim_id,
            supersedes_assignment_id=supersedes_assignment_id,
            created_at=stamp,
            updated_at=stamp,
            created_by_actor_id=actor_id,
        )

    def end(self, *, status: ManagingClinicianStatus = ManagingClinicianStatus.ENDED,
            actor_id: Optional[str] = None, reason: str = "",
            now: Optional[datetime] = None) -> "ManagingClinicianAssignment":
        if status not in TERMINAL_ASSIGNMENT_STATUSES:
            raise ManagingClinicianError(f"{status} is not a terminal assignment status")
        if not self.is_active:
            raise ManagingClinicianError("only an active assignment can be ended")
        stamp = now or utc_now()
        return replace(self, status=status, effective_to=stamp,
                       ended_by_actor_id=actor_id, end_reason=reason,
                       updated_at=stamp)

    def with_successor(self, successor_assignment_id: str,
                       *, now: Optional[datetime] = None
                       ) -> "ManagingClinicianAssignment":
        return replace(self, superseded_by_assignment_id=successor_assignment_id,
                       updated_at=now or utc_now())
