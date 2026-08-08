"""Parent acceptance and decline of an ADD proposal (Phase 1B.2F).

Fictional, in-memory, dev-only. No Firestore, no Firebase Auth, no Cloud Run, no
frontend, no real data.

**Add is not Modify.** A Modify replaces one assignment with a derived version,
inheriting its position. An Add *appends*: nothing is retired, nothing is
replaced, no existing assignment's version or `display_order` changes, and
exactly one new CURRENT assignment lands at the end of the destination day.

Both functions here are **transaction bodies**, invoked from inside the existing
`acceptance_service` / `decline_service` critical sections after those services
have authenticated, authorized and performed the idempotency-replay check. They
never open their own transaction, so the whole decision — assignment, proposal,
audit event and idempotency record — commits or rolls back as one unit.

## Two guards worth stating plainly

**Only a pending Add targeting the child's canonical CURRENT weekly plan is
actionable.** A historical Add stays readable (Phase 1B.2E) but a decision on it
raises `weekly_plan_conflict`: accepting would create a live assignment inside a
week the family has finished, and the proposal is never silently migrated into
the current week. Decline is refused for the same reason rather than allowed
because it "would be harmless" — a stale proposal is not a live decision.

**`display_order` is allocated HERE, inside the transaction**, as
`max(display_order on that day) + 1` (or `0` for an empty day). Creation
deliberately reserved nothing, so two pending Adds on one day both depend on
this being atomic: computing the next position outside the critical section
would hand the same number to both.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..auth.interface import AuthenticatedUser
from ..domain.audit_state import plain
from ..domain.enums import (
    AssignmentStatus,
    PlanApprovalStatus,
    PracticeStatus,
    PrincipalRole,
    ProposalStatus,
    ProposalType,
)
from ..domain.ids import key_hash, operation_identity, operation_scoped_id
from ..domain.read_models import AuditEvent, IdempotencyRecord, PlanAssignment
from ..domain.weekdays import is_valid_weekday
from ..repository import collections as C
from ..repository.interface import CollaborationRepository
from .add_proposal_service import WeeklyPlanConflict, destination_target_token
from .assignment_order import (
    DuplicateAssignmentDisplayOrder,
    assignment_order_map,
    current_assignments_for_day,
    has_duplicate_display_order,
)
from .weekly_plan import current_weekly_plan_id

ACCEPT_EVENT_TYPE = "add_activity_proposal_accepted"
DECLINE_EVENT_TYPE = "add_activity_proposal_declined"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def next_display_order(current_assignments: list) -> int:
    """The position an accepted Add takes on its destination day.

    Empty day -> 0. Otherwise `max(display_order) + 1`.

    Deliberately **not** `len(current_assignments)`: gaps are legal and are never
    compacted, so a day holding `[0, 3, 7]` yields **8**, not 3. Using the count
    would collide with an existing row the moment any gap exists.

    Pure — the caller allocates by writing, inside the transaction.
    """
    if not current_assignments:
        return 0
    return max(int(a.get("display_order", 0)) for a in current_assignments) + 1


def _validate_add_decision(
    tx: CollaborationRepository,
    child_id: str,
    proposal: dict,
    expected_proposal_version: int,
    already_decided_error,
    version_conflict_error,
    invalid_transition_error,
) -> tuple:
    """Authoritative re-validation shared by Add accept and Add decline.

    Every check re-reads canonical state inside the transaction; nothing here
    trusts a value the client may have obtained from an earlier GET.

    Returns `(destination_day, current_plan_id, proposed_version_id, day_before)`.
    """
    # Proposal shape and decision state.
    status = plain(proposal["status"])
    if status != ProposalStatus.PENDING_PARENT_ACCEPTANCE.value:
        raise already_decided_error(f"Proposal is already '{status}'.")

    destination_day = proposal.get("destination_scheduled_day")
    if destination_day is None:
        raise invalid_transition_error("Add proposal has no destination weekday.")
    if not is_valid_weekday(destination_day):
        raise invalid_transition_error(
            f"Add proposal destination weekday '{destination_day}' is not 0-6."
        )

    proposed_version_id = proposal.get("proposed_activity_version_id")
    if not proposed_version_id:
        raise invalid_transition_error("Add proposal has no proposed activity version.")
    vrows = tx.query(C.ACTIVITY_VERSIONS, id=proposed_version_id)
    if not vrows:
        raise invalid_transition_error("Proposed activity version no longer exists.")
    if not vrows[0].get("immutable", True):
        raise invalid_transition_error("Proposed activity version is not immutable.")

    # Optimistic concurrency BEFORE any mutation.
    if expected_proposal_version != int(proposal["version"]):
        raise version_conflict_error("expected_proposal_version does not match.")

    # The proposal's plan must exist, belong to this child, and BE the child's
    # canonical current plan. The canonical resolver returns None for zero and
    # for several CURRENT plans, so both ambiguities fail closed here.
    weekly_plan_id = proposal.get("weekly_plan_id")
    if not weekly_plan_id:
        raise WeeklyPlanConflict("Add proposal references no weekly plan.")
    prows = tx.query(C.WEEKLY_PLANS, id=weekly_plan_id)
    if not prows or prows[0].get("child_id") != child_id:
        raise WeeklyPlanConflict("Add proposal weekly plan is not this child's.")
    current_plan_id = current_weekly_plan_id(tx, child_id)
    if current_plan_id is None or weekly_plan_id != current_plan_id:
        raise WeeklyPlanConflict(
            "Add proposal does not target the child's current weekly plan."
        )

    # The destination day must be unambiguously ordered, or the next position
    # cannot be allocated soundly.
    day_before = current_assignments_for_day(tx, child_id, weekly_plan_id, destination_day)
    if has_duplicate_display_order(day_before):
        raise DuplicateAssignmentDisplayOrder(
            "Current assignments on the destination day have duplicate display_order values."
        )
    return destination_day, current_plan_id, proposed_version_id, day_before


def accept_add(
    tx: CollaborationRepository,
    *,
    user: AuthenticatedUser,
    parent: dict,
    child_id: str,
    proposal_id: str,
    proposal: dict,
    key: str,
    req_hash: str,
    rec_id: str,
    expected_proposal_version: int,
    action: str,
    already_decided_error,
    version_conflict_error,
    invalid_transition_error,
    environment: str = "dev",
    request_id: Optional[str] = None,
) -> dict:
    """Parent accepts an ADD proposal. Runs inside the caller's transaction."""
    destination_day, current_plan_id, proposed_version_id, day_before = _validate_add_decision(
        tx, child_id, proposal, expected_proposal_version,
        already_decided_error, version_conflict_error, invalid_transition_error,
    )
    orders_before = assignment_order_map(day_before)

    # Deterministic ids bound to this operation INCLUDING the key hash, matching
    # the established pattern. There is no assignment to key on, so the internal
    # destination token names the day — never treated as an assignment id.
    target_token = destination_target_token(current_plan_id, destination_day)
    identity = operation_identity(
        key, user.uid, action, child_id, proposal_id, target_token
    )
    new_assignment_id = operation_scoped_id("assign", identity)
    audit_id = operation_scoped_id("aud", identity)
    if tx.exists(C.PLAN_ASSIGNMENTS, new_assignment_id):
        raise invalid_transition_error("Add assignment already exists for this operation.")
    if proposal.get("resulting_assignment_id"):
        raise invalid_transition_error("Add proposal already produced an assignment.")

    # Allocate the position INSIDE the transaction. Nothing before this line may
    # compute it: two concurrent acceptances on one day would otherwise both read
    # the same maximum and collide.
    allocated_order = next_display_order(day_before)

    before_state = {
        "proposal_status": plain(proposal["status"]),
        "proposal_type": ProposalType.ADD.value,
        "proposal_version": int(proposal["version"]),
        "weekly_plan_id": current_plan_id,
        "destination_scheduled_day": destination_day,
        "current_assignment_ids_on_day": sorted(orders_before),
        "display_order_map_on_day": dict(sorted(orders_before.items())),
        "resulting_assignment_id": None,
    }

    now = _now()

    # Exactly ONE new assignment. No lineage is fabricated: an Add supersedes
    # nothing, so `replaces_assignment_id` and `replaced_by_assignment_id` stay
    # null. `source_proposal_id` is set because it is a true fact — this
    # assignment came from that proposal — not a replacement claim.
    new_assignment = PlanAssignment(
        id=new_assignment_id,
        weekly_plan_id=current_plan_id,
        child_id=child_id,
        # Standalone therapist-authored activity: no catalog template exists.
        activity_template_id=None,
        activity_version_id=proposed_version_id,
        scheduled_day=destination_day,
        display_order=allocated_order,
        # The therapist authored it and the parent accepted it, so it needs no
        # further therapist review — the same rule Modify acceptance applies.
        plan_approval_status=PlanApprovalStatus.APPROVED,
        practice_status=PracticeStatus.NOT_TRIED,
        assignment_status=AssignmentStatus.CURRENT,
        parent_feedback_summary="",
        pending_proposal_id=None,
        replaces_assignment_id=None,
        source_proposal_id=proposal_id,
        version=1,
        created_at=now,
        updated_at=now,
        environment=environment,
    )
    tx.set(C.PLAN_ASSIGNMENTS, new_assignment_id, new_assignment.model_dump())

    proposal["status"] = ProposalStatus.ACCEPTED.value
    proposal["decided_by_user_id"] = parent["id"]
    proposal["decided_by_role"] = PrincipalRole.PARENT.value
    proposal["decided_at"] = now
    proposal["resulting_assignment_id"] = new_assignment_id
    proposal["version"] = int(proposal["version"]) + 1
    tx.set(C.PLAN_CHANGE_PROPOSALS, proposal_id, proposal)

    # Post-condition: the day gained EXACTLY the new assignment, every existing
    # sibling kept its identity and its position, and the day is still uniquely
    # ordered. An Add that moved or dropped an existing activity is a
    # product-level failure and rolls the whole decision back.
    day_after = current_assignments_for_day(tx, child_id, current_plan_id, destination_day)
    orders_after = assignment_order_map(day_after)
    if set(orders_after) != set(orders_before) | {new_assignment_id}:
        raise invalid_transition_error(
            "Post-condition failed: accepting an Add must add exactly one activity "
            f"to the day; expected {sorted(set(orders_before) | {new_assignment_id})}, "
            f"found {sorted(orders_after)}."
        )
    if any(orders_after[i] != orders_before[i] for i in orders_before):
        raise invalid_transition_error(
            "Post-condition failed: an existing same-day assignment moved."
        )
    if orders_after[new_assignment_id] != allocated_order:
        raise invalid_transition_error(
            "Post-condition failed: the new assignment did not take the allocated "
            f"display_order ({allocated_order})."
        )
    if has_duplicate_display_order(day_after):
        raise DuplicateAssignmentDisplayOrder(
            "Accepting the Add produced duplicate display_order values on the day."
        )

    after_state = {
        "proposal_status": ProposalStatus.ACCEPTED.value,
        "proposal_type": ProposalType.ADD.value,
        "proposal_version": int(proposal["version"]),
        "weekly_plan_id": current_plan_id,
        "destination_scheduled_day": destination_day,
        "resulting_assignment_id": new_assignment_id,
        "resulting_assignment_display_order": allocated_order,
        "resulting_assignment_status": AssignmentStatus.CURRENT.value,
        "resulting_activity_version_id": proposed_version_id,
        "current_assignment_ids_on_day": sorted(orders_after),
        "display_order_map_on_day": dict(sorted(orders_after.items())),
        # Stated explicitly: an Add appends, it never supersedes.
        "existing_assignments_unchanged": True,
        "retired_assignment_id": None,
        "replaced_assignment_id": None,
    }

    audit = AuditEvent(
        id=audit_id, event_type=ACCEPT_EVENT_TYPE, actor_uid=user.uid,
        actor_role=PrincipalRole.PARENT, subject_type="plan_change_proposal",
        subject_id=proposal_id, therapist_id=proposal.get("therapist_id"),
        child_id=child_id, weekly_plan_id=current_plan_id,
        assignment_id=new_assignment_id,      # the assignment this event created
        idempotency_key_hash=key_hash(key),
        before_state=before_state, after_state=after_state, request_id=request_id,
        occurred_at=now, created_at=now, environment=environment,
    )
    tx.set(C.AUDIT_EVENTS, audit_id, audit.model_dump())

    result = {
        "proposal": {
            "proposal_id": proposal_id,
            "proposal_type": ProposalType.ADD.value,
            "proposal_status": ProposalStatus.ACCEPTED.value,
            "version": int(proposal["version"]),
            "decided_by_user_id": parent["id"],
            "decided_by_role": PrincipalRole.PARENT.value,
            "decided_at": now,
            "resulting_assignment_id": new_assignment_id,
            "proposed_activity_version_id": proposed_version_id,
        },
        "added_assignment": _assignment_view(
            tx.query(C.PLAN_ASSIGNMENTS, id=new_assignment_id)[0]
        ),
        "child_summary": _child_summary(tx, child_id),
        "audit_event_id": audit_id,
        "idempotent_replay": False,
    }

    tx.set(C.IDEMPOTENCY_RECORDS, rec_id, IdempotencyRecord(
        id=rec_id, idempotency_key_hash=key_hash(key), actor_user_id=user.uid,
        action=action, child_id=child_id,
        assignment_id=target_token,   # internal operation target, not an assignment id
        request_hash=req_hash, status="completed", result=result,
        audit_event_id=audit_id, created_at=now, environment=environment,
    ).model_dump())
    return result


def decline_add(
    tx: CollaborationRepository,
    *,
    user: AuthenticatedUser,
    parent: dict,
    child_id: str,
    proposal_id: str,
    proposal: dict,
    key: str,
    req_hash: str,
    rec_id: str,
    expected_proposal_version: int,
    action: str,
    already_decided_error,
    version_conflict_error,
    invalid_transition_error,
    environment: str = "dev",
    request_id: Optional[str] = None,
) -> dict:
    """Parent declines an ADD proposal. Runs inside the caller's transaction.

    Creates NOTHING. The destination day must be byte-identical afterwards, and
    the proposed ActivityVersion is preserved unchanged as historical proposal
    content — it is never activated.
    """
    destination_day, current_plan_id, proposed_version_id, day_before = _validate_add_decision(
        tx, child_id, proposal, expected_proposal_version,
        already_decided_error, version_conflict_error, invalid_transition_error,
    )
    orders_before = assignment_order_map(day_before)

    target_token = destination_target_token(current_plan_id, destination_day)
    identity = operation_identity(
        key, user.uid, action, child_id, proposal_id, target_token
    )
    audit_id = operation_scoped_id("aud", identity)

    before_state = {
        "proposal_status": plain(proposal["status"]),
        "proposal_type": ProposalType.ADD.value,
        "proposal_version": int(proposal["version"]),
        "weekly_plan_id": current_plan_id,
        "destination_scheduled_day": destination_day,
        "current_assignment_ids_on_day": sorted(orders_before),
        "display_order_map_on_day": dict(sorted(orders_before.items())),
        "resulting_assignment_id": None,
    }

    now = _now()
    proposal["status"] = ProposalStatus.DECLINED.value
    proposal["decided_by_user_id"] = parent["id"]
    proposal["decided_by_role"] = PrincipalRole.PARENT.value
    proposal["decided_at"] = now
    proposal["resulting_assignment_id"] = None      # nothing was created
    proposal["version"] = int(proposal["version"]) + 1
    tx.set(C.PLAN_CHANGE_PROPOSALS, proposal_id, proposal)

    # Post-condition: the destination day is byte-identical, and no assignment
    # anywhere claims this proposal as its source.
    day_after = current_assignments_for_day(tx, child_id, current_plan_id, destination_day)
    if assignment_order_map(day_after) != orders_before:
        raise invalid_transition_error(
            "Post-condition failed: declining an Add must leave the destination "
            "day's current assignments and display_order values untouched."
        )
    if any(a.get("source_proposal_id") == proposal_id
           for a in tx.query(C.PLAN_ASSIGNMENTS, child_id=child_id)):
        raise invalid_transition_error(
            "Post-condition failed: a declined Add created an assignment."
        )

    after_state = {
        "proposal_status": ProposalStatus.DECLINED.value,
        "proposal_type": ProposalType.ADD.value,
        "proposal_version": int(proposal["version"]),
        "weekly_plan_id": current_plan_id,
        "destination_scheduled_day": destination_day,
        "resulting_assignment_id": None,
        "plan_assignment_created": False,
        "current_assignment_ids_on_day": sorted(orders_before),
        "display_order_map_on_day": dict(sorted(orders_before.items())),
        "existing_assignments_unchanged": True,
    }

    audit = AuditEvent(
        id=audit_id, event_type=DECLINE_EVENT_TYPE, actor_uid=user.uid,
        actor_role=PrincipalRole.PARENT, subject_type="plan_change_proposal",
        subject_id=proposal_id, therapist_id=proposal.get("therapist_id"),
        child_id=child_id, weekly_plan_id=current_plan_id,
        assignment_id=None,                   # a decline touches no assignment
        idempotency_key_hash=key_hash(key),
        before_state=before_state, after_state=after_state, request_id=request_id,
        occurred_at=now, created_at=now, environment=environment,
    )
    tx.set(C.AUDIT_EVENTS, audit_id, audit.model_dump())

    result = {
        "proposal": {
            "proposal_id": proposal_id,
            "proposal_type": ProposalType.ADD.value,
            "proposal_status": ProposalStatus.DECLINED.value,
            "version": int(proposal["version"]),
            "decided_by_user_id": parent["id"],
            "decided_by_role": PrincipalRole.PARENT.value,
            "decided_at": now,
            "resulting_assignment_id": None,
            "proposed_activity_version_id": proposed_version_id,
        },
        # No `added_assignment` key at all — a decline adds nothing, and a null
        # placeholder would imply the concept applies here. The destination day
        # is reported instead, which is what the client actually needs.
        "destination_scheduled_day": destination_day,
        "child_summary": _child_summary(tx, child_id),
        "audit_event_id": audit_id,
        "idempotent_replay": False,
    }

    tx.set(C.IDEMPOTENCY_RECORDS, rec_id, IdempotencyRecord(
        id=rec_id, idempotency_key_hash=key_hash(key), actor_user_id=user.uid,
        action=action, child_id=child_id, assignment_id=target_token,
        request_hash=req_hash, status="completed", result=result,
        audit_event_id=audit_id, created_at=now, environment=environment,
    ).model_dump())
    return result


def _assignment_view(a: dict) -> dict:
    """Therapist/parent-neutral internal view, matching the Modify decision shape."""
    return {
        "assignment_id": a["id"], "child_id": a["child_id"],
        "weekly_plan_id": a["weekly_plan_id"], "scheduled_day": a["scheduled_day"],
        "display_order": int(a.get("display_order", 0)),
        "activity_template_id": a.get("activity_template_id"),
        "activity_version_id": a["activity_version_id"],
        "plan_approval_status": plain(a["plan_approval_status"]),
        "practice_status": plain(a["practice_status"]),
        "assignment_status": plain(a["assignment_status"]),
        "pending_proposal_id": a.get("pending_proposal_id"),
        "replaced_by_assignment_id": a.get("replaced_by_assignment_id"),
        "replaced_at": a.get("replaced_at"),
        "replaces_assignment_id": a.get("replaces_assignment_id"),
        "source_proposal_id": a.get("source_proposal_id"),
        "version": int(a["version"]), "updated_at": a.get("updated_at", ""),
    }


def _child_summary(tx: CollaborationRepository, child_id: str) -> dict:
    return {
        "child_id": child_id,
        "plan_review_count": sum(
            1 for a in tx.query(C.PLAN_ASSIGNMENTS, child_id=child_id)
            if plain(a["plan_approval_status"]) == PlanApprovalStatus.NEEDS_PLAN_REVIEW.value
            and plain(a["assignment_status"]) == AssignmentStatus.CURRENT.value
        ),
        "pending_proposal_count": sum(
            1 for p in tx.query(C.PLAN_CHANGE_PROPOSALS, child_id=child_id)
            if plain(p["status"]) == ProposalStatus.PENDING_PARENT_ACCEPTANCE.value
        ),
    }
