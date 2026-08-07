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
from ..domain.weekdays import is_valid_weekday
from ..repository import collections as C
from ..repository.interface import CollaborationRepository
from .assignment_order import (
    current_assignments_for_day,
    current_assignments_sharing_day,
    has_duplicate_display_order,
)


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

    Dispatches on proposal type. Each type's guards mirror the write service that
    owns it, so they must not be shared: a MODIFY is assignment-centric (target
    assignment, pending link, version match) while an ADD is day-centric (a
    destination weekday, no assignment at all). Copying MODIFY's target-assignment
    guards onto an ADD would reject every valid Add.

    Any missing or inconsistent linked record yields INELIGIBLE — never an
    exception, so one bad record cannot break a whole list. An unsupported type
    (REPLACE / REMOVE / anything unknown) also fails closed.
    """
    if not connection_is_active:
        return INELIGIBLE

    proposal_type = plain(proposal.get("proposal_type"))
    if proposal_type == ProposalType.ADD.value:
        return _evaluate_add(repo, child_id, proposal)
    if proposal_type != ProposalType.MODIFY.value:
        return INELIGIBLE                       # REPLACE / REMOVE / unknown
    return _evaluate_modify(repo, child_id, proposal)


def _evaluate_add(
    repo: CollaborationRepository, child_id: str, proposal: dict
) -> DecisionEligibility:
    """Add is READABLE but NOT actionable in this checkpoint.

    Add accept/decline endpoints do not exist yet, so this always returns
    INELIGIBLE. The state below is still validated rather than short-circuited:
    the checks are what a future Add decision will need, and running them now
    means an Add that could never be acted on is already reported as such instead
    of appearing actionable the day those endpoints land.
    """
    if plain(proposal.get("status")) != ProposalStatus.PENDING_PARENT_ACCEPTANCE.value:
        return INELIGIBLE
    if not is_valid_weekday(proposal.get("destination_scheduled_day")):
        return INELIGIBLE
    proposed_version_id = proposal.get("proposed_activity_version_id")
    if not proposed_version_id or not repo.query(C.ACTIVITY_VERSIONS, id=proposed_version_id):
        return INELIGIBLE

    weekly_plan_id = proposal.get("weekly_plan_id")
    if not weekly_plan_id:
        return INELIGIBLE
    day = current_assignments_for_day(
        repo, child_id, weekly_plan_id, proposal["destination_scheduled_day"]
    )
    if has_duplicate_display_order(day):
        return INELIGIBLE                       # ambiguous day -> fail closed

    # Every check above passed. The result is STILL ineligible: there is no
    # endpoint a parent could submit this to. Advertising can_accept here would
    # offer a button that 404s.
    return INELIGIBLE


def _evaluate_modify(
    repo: CollaborationRepository, child_id: str, proposal: dict
) -> DecisionEligibility:
    """Frozen MODIFY eligibility — mirrors acceptance_service / decline_service."""
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

    # ── day invariant: the target is AMONG the day's current assignments ─────
    #     A weekday may hold several activities, so another same-day activity
    #     must not make this proposal ineligible. What must hold is that the
    #     target is still current, and the day's ordering is unambiguous.
    #
    #     These two helpers are the shared read-only ordering invariant. Importing
    #     them does NOT weaken this module's independence from the write services:
    #     they are pure data-shape functions that decide nothing, so the
    #     authoritative accept/decline guards remain solely inside their own
    #     transactions and cannot be influenced from here.
    in_day = current_assignments_sharing_day(repo, assignment)
    if assignment["id"] not in [a["id"] for a in in_day]:
        return INELIGIBLE
    if has_duplicate_display_order(in_day):
        return INELIGIBLE            # duplicate positions -> fail closed

    return ELIGIBLE


def proposal_is_safe_to_show(
    repo: CollaborationRepository, child_id: str, proposal: dict
) -> bool:
    """Whether a parent-safe summary can be built for `proposal` at all.

    Distinct from eligibility: a proposal may be perfectly safe to *show* while
    being ineligible to act on (the common "pending but blocked" case, and every
    ADD in this checkpoint). This is only about whether the records needed to
    render a summary exist and belong to this child — if not, the caller drops the
    item rather than emitting a partial one or disclosing why.

    Visibility is **explicitly type-aware**, not open by default. MODIFY and ADD
    each have their own requirements; every other type — REPLACE, REMOVE, anything
    unrecognised — stays fail-closed until its own phase gives it a parent-safe
    projection.
    """
    if proposal.get("child_id") != child_id:
        return False
    proposed_version_id = proposal.get("proposed_activity_version_id")
    if not proposed_version_id:
        return False
    if not repo.query(C.ACTIVITY_VERSIONS, id=proposed_version_id):
        return False

    proposal_type = plain(proposal.get("proposal_type"))
    if proposal_type == ProposalType.ADD.value:
        return _add_is_safe_to_show(repo, child_id, proposal)
    if proposal_type != ProposalType.MODIFY.value:
        return False                            # REPLACE / REMOVE / unknown

    assignment_id = proposal.get("target_assignment_id")
    if not assignment_id:
        return False
    rows = repo.query(C.PLAN_ASSIGNMENTS, id=assignment_id)
    if not rows or rows[0].get("child_id") != child_id:
        return False
    return True


def _add_is_safe_to_show(
    repo: CollaborationRepository, child_id: str, proposal: dict
) -> bool:
    """Whether an ADD can be rendered as a parent-safe summary.

    Day-centric, deliberately not assignment-centric: an Add has no target
    assignment, so requiring one would drop every valid Add.

    Plan **currency** is intentionally NOT required here, matching MODIFY — an
    old-plan proposal stays readable as history and is made non-actionable by the
    eligibility evaluator instead of vanishing from the list. The plan must still
    RESOLVE, because the destination day's activities are read from it.
    """
    if not is_valid_weekday(proposal.get("destination_scheduled_day")):
        return False
    weekly_plan_id = proposal.get("weekly_plan_id")
    if not weekly_plan_id:
        return False
    plan = repo.query(C.WEEKLY_PLANS, id=weekly_plan_id)
    if not plan or plan[0].get("child_id") != child_id:
        return False
    # An ambiguously ordered destination day cannot be presented in a meaningful
    # order, so the item is dropped rather than shown in an arbitrary one.
    day = current_assignments_for_day(
        repo, child_id, weekly_plan_id, proposal["destination_scheduled_day"]
    )
    return not has_duplicate_display_order(day)


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
