"""Shared, read-only assignment-ordering helpers (Phase 1B.2D).

These express one domain invariant that several services now depend on:

    Among CURRENT assignments sharing (child_id, weekly_plan_id, scheduled_day),
    every `display_order` is unique.

A weekday holds SEVERAL current activities — Genex generates an activity for most
days, so a therapist Add appends alongside what is already there rather than
displacing it. The terminology below is deliberately day-oriented
(`current_assignments_for_day`, not `current_in_slot`): there is no slot, no
reserved position, and no at-most-one rule. Retired/replaced rows never
participate in the uniqueness invariant.

Every function here is PURE and READ-ONLY. Nothing mutates a collection, and
nothing allocates a position — allocating `max(display_order) + 1` belongs inside
the future Add-acceptance transaction, where it can be done atomically.

Promoted out of `acceptance_service` once a fourth consumer appeared
(acceptance, decline, eligibility, Add creation). Behavior is unchanged.
"""

from __future__ import annotations

from typing import Dict, List

from ..domain.audit_state import plain
from ..domain.enums import AssignmentStatus
from ..repository import collections as C
from ..repository.interface import CollaborationRepository
from .approval_service import ApprovalError


class DuplicateAssignmentDisplayOrder(ApprovalError):
    """Two CURRENT assignments on one weekday claim the same display_order.

    An internal consistency failure, not a client mistake — but surfacing it as a
    typed 409 keeps the transaction fail-closed instead of silently producing an
    ambiguously ordered day. Discloses no other family's data.
    """

    code = "duplicate_assignment_display_order"
    http_status = 409


def current_assignments_for_day(
    repo: CollaborationRepository,
    child_id: str,
    weekly_plan_id: str,
    scheduled_day: int,
) -> List[dict]:
    """The CURRENT assignments on one (child, weekly plan, weekday).

    Returns a LIST, possibly empty and possibly long: a weekday may legitimately
    hold zero, one or many current activities. Retired/replaced rows are excluded.
    """
    return [
        a
        for a in repo.query(C.PLAN_ASSIGNMENTS, child_id=child_id)
        if a["weekly_plan_id"] == weekly_plan_id
        and a["scheduled_day"] == scheduled_day
        and plain(a["assignment_status"]) == AssignmentStatus.CURRENT.value
    ]


def current_assignments_sharing_day(
    repo: CollaborationRepository, assignment: dict
) -> List[dict]:
    """The CURRENT assignments sharing `assignment`'s day, including itself.

    Convenience wrapper for the acceptance/decline services, which already hold
    the assignment record and care about the day it sits on.
    """
    return current_assignments_for_day(
        repo,
        assignment["child_id"],
        assignment["weekly_plan_id"],
        assignment["scheduled_day"],
    )


def assignment_order_map(assignments: List[dict]) -> Dict[str, int]:
    """Stable {assignment_id: display_order} for comparing a day before/after."""
    return {a["id"]: int(a.get("display_order", 0)) for a in assignments}


def has_duplicate_display_order(assignments: List[dict]) -> bool:
    """True when two CURRENT same-day assignments claim the same position."""
    orders = [int(a.get("display_order", 0)) for a in assignments]
    return len(orders) != len(set(orders))
