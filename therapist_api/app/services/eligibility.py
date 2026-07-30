"""Read-only parent decision eligibility (Phase 1B.2B.4).

**Correction this module exists for.** `proposal_status == pending_parent_acceptance`
is NOT sufficient to know whether a parent can act. The accept/decline writes
enforce a further set of guards, so a proposal can be genuinely pending and still
409 on submission. Presenting `can_accept: true` for such a proposal would offer
the family a button that cannot work.

This evaluator re-checks the same canonical state those writes require, so the
parent list and the parent proposal detail can advertise only actions that would
actually succeed. It is a **pure read**: nothing here mutates any collection, and
it never reports *why* a proposal is ineligible — that reasoning stays internal.

It deliberately duplicates the write guards rather than importing them: the write
services own the authoritative check inside their transaction, and this module
must not be able to influence them. If the two ever diverge a submission fails
closed with a typed 409, which is the safe direction.
"""

from __future__ import annotations

from typing import NamedTuple, Optional

from ..domain.audit_state import plain
from ..domain.enums import (
    AssignmentStatus,
    PlanApprovalStatus,
    ProposalStatus,
    ProposalType,
)
from ..repository import collections as C
from ..repository.interface import CollaborationRepository


class DecisionEligibility(NamedTuple):
    """What a parent may actually do with a proposal right now."""

    can_accept: bool
    can_decline: bool
    needs_parent_attention: bool


#: Fail-closed result: pending-but-blocked, decided, or internally inconsistent.
INELIGIBLE = DecisionEligibility(can_accept=False, can_decline=False,
                                 needs_parent_attention=False)
#: A pending proposal that would pass every write guard as things stand.
ELIGIBLE = DecisionEligibility(can_accept=True, can_decline=True,
                               needs_parent_attention=True)


def evaluate_parent_decision(
    repo: CollaborationRepository,
    child_id: str,
    proposal: dict,
    connection_is_active: bool = True,
) -> DecisionEligibility:
    """Whether an authorized parent could accept/decline `proposal` right now.

    Mirrors the guards in `acceptance_service` / `decline_service`. Any missing or
    inconsistent linked record yields INELIGIBLE — never an exception, so one bad
    record cannot break a whole list.
    """
    if not connection_is_active:
        return INELIGIBLE

    # ── proposal shape and decision state ───────────────────────────────────
    if plain(proposal.get("proposal_type")) != ProposalType.MODIFY.value:
        return INELIGIBLE
    if plain(proposal.get("status")) != ProposalStatus.PENDING_PARENT_ACCEPTANCE.value:
        return INELIGIBLE

    assignment_id = proposal.get("target_assignment_id")
    original_version_id = proposal.get("original_activity_version_id")
    proposed_version_id = proposal.get("proposed_activity_version_id")
    if not (assignment_id and original_version_id and proposed_version_id):
        return INELIGIBLE

    # ── original assignment ─────────────────────────────────────────────────
    rows = repo.query(C.PLAN_ASSIGNMENTS, id=assignment_id)
    if not rows:
        return INELIGIBLE
    assignment = rows[0]
    if assignment.get("child_id") != child_id:
        return INELIGIBLE
    if assignment.get("weekly_plan_id") != proposal.get("weekly_plan_id"):
        return INELIGIBLE

    plans = repo.query(C.WEEKLY_PLANS, child_id=child_id)
    current_plan_id = plans[0]["id"] if plans else None
    if assignment.get("weekly_plan_id") != current_plan_id:
        return INELIGIBLE

    if plain(assignment.get("assignment_status")) != AssignmentStatus.CURRENT.value:
        return INELIGIBLE
    if plain(assignment.get("plan_approval_status")) != PlanApprovalStatus.APPROVED.value:
        return INELIGIBLE
    if assignment.get("activity_version_id") != original_version_id:
        return INELIGIBLE
    if assignment.get("pending_proposal_id") != proposal.get("id"):
        return INELIGIBLE

    # ── linked activity versions ────────────────────────────────────────────
    if not repo.query(C.ACTIVITY_VERSIONS, id=original_version_id):
        return INELIGIBLE
    if not repo.query(C.ACTIVITY_VERSIONS, id=proposed_version_id):
        return INELIGIBLE

    child_assignments = repo.query(C.PLAN_ASSIGNMENTS, child_id=child_id)
    proposed_already_active = [
        a for a in child_assignments
        if a.get("activity_version_id") == proposed_version_id
        and plain(a.get("assignment_status")) == AssignmentStatus.CURRENT.value
    ]
    if proposed_already_active:
        return INELIGIBLE

    # ── slot invariant: the original is the slot's only current assignment ──
    in_slot = [
        a for a in child_assignments
        if a.get("weekly_plan_id") == assignment.get("weekly_plan_id")
        and a.get("scheduled_day") == assignment.get("scheduled_day")
        and plain(a.get("assignment_status")) == AssignmentStatus.CURRENT.value
    ]
    if [a["id"] for a in in_slot] != [assignment["id"]]:
        return INELIGIBLE

    return ELIGIBLE


def proposal_is_safe_to_show(
    repo: CollaborationRepository, child_id: str, proposal: dict
) -> bool:
    """Whether a parent-safe summary can be built for `proposal` at all.

    Distinct from eligibility: a proposal may be perfectly safe to *show* while
    being ineligible to act on (the common "pending but blocked" case). This is
    only about whether the records needed to render a summary exist and belong to
    this child — if not, the caller drops the item rather than emitting a partial
    one or disclosing why.
    """
    if proposal.get("child_id") != child_id:
        return False
    proposed_version_id = proposal.get("proposed_activity_version_id")
    if not proposed_version_id:
        return False
    if not repo.query(C.ACTIVITY_VERSIONS, id=proposed_version_id):
        return False
    assignment_id = proposal.get("target_assignment_id")
    if not assignment_id:
        return False
    rows = repo.query(C.PLAN_ASSIGNMENTS, id=assignment_id)
    if not rows or rows[0].get("child_id") != child_id:
        return False
    return True


def sort_key(proposal: dict, eligibility: DecisionEligibility) -> tuple:
    """Deterministic parent-list ordering key.

    Groups, then newest first, then a stable id tie-break:

      0. actionable — needs the parent's attention now
      1. pending but not currently actionable
      2. decided (accepted / declined / cancelled / anything else)

    `created_at` is inverted via a reverse-sorted string so the whole key sorts
    ascending; the id tie-break keeps repeated requests byte-identical regardless
    of dictionary iteration order.
    """
    status = plain(proposal.get("status"))
    if eligibility.needs_parent_attention:
        group = 0
    elif status == ProposalStatus.PENDING_PARENT_ACCEPTANCE.value:
        group = 1
    else:
        group = 2
    # Newest first: negate lexicographic order by using a reversed comparison
    # through a tuple of (group, inverted_created_at, id).
    created_at = proposal.get("created_at", "") or ""
    return (group, _descending(created_at), proposal.get("id", ""))


def _descending(value: str) -> tuple:
    """Map a string to a key that sorts in DESCENDING order ascendingly."""
    # Invert each code point so ascending sort yields descending strings, and
    # pad-compare by length so a longer prefix-matching string sorts first.
    return tuple(-ord(ch) for ch in value)
