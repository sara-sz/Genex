"""Idempotent parent acceptance of a modify proposal (Phase 1B.2B.1).

Fictional, in-memory, dev-only. No Firestore, no Firebase Auth, no frontend, no
real data. **Parent DECLINE is not implemented in this phase.**

A parent accepts one pending `modify` proposal on their own child's current
weekly plan. In ONE atomic critical section this:

  * marks the proposal `accepted` (version +1, decision metadata recorded),
  * retires the original assignment (`current` -> `replaced`, version +1,
    `pending_proposal_id` cleared, replacement linked, `replaced_at` set),
  * creates exactly ONE replacement assignment carrying the already-created
    proposed ActivityVersion, in the original's plan slot, at version 1, and
  * writes exactly one immutable audit event plus one idempotency result.

The original assignment, template and activity versions are preserved unchanged
for history — nothing is deleted or rewritten.
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
from ..domain.ids import (
    canonical_request_hash,
    idempotency_doc_id,
    key_hash,
    operation_identity,
    operation_scoped_id,
)
from ..domain.read_models import AuditEvent, IdempotencyRecord, PlanAssignment
from ..repository import collections as C
from ..repository.interface import CollaborationRepository
from . import access
from .approval_service import (  # reuse shared base + generic write errors
    ApprovalError,
    AssignmentVersionConflict,
    IdempotencyKeyConflict,
    MissingIdempotencyKey,
)

ACTION = "accept_plan_change_proposal"
EVENT_TYPE = "plan_change_proposal_accepted"


class InvalidParentAcceptTransition(ApprovalError):
    code = "invalid_parent_accept_transition"
    http_status = 409


class ProposalVersionConflict(ApprovalError):
    code = "proposal_version_conflict"
    http_status = 409


class ProposalAlreadyDecided(ApprovalError):
    code = "proposal_already_decided"
    http_status = 409


class ProposalAssignmentMismatch(ApprovalError):
    code = "proposal_assignment_mismatch"
    http_status = 409


class ReplacementAssignmentConflict(ApprovalError):
    code = "replacement_assignment_conflict"
    http_status = 409


class DuplicateAssignmentDisplayOrder(ApprovalError):
    """Two CURRENT assignments on one weekday claim the same display_order.

    An internal consistency failure, not a client mistake — but surfacing it as a
    typed 409 keeps the transaction fail-closed instead of silently producing an
    ambiguously ordered day. Discloses no other family's data.
    """

    code = "duplicate_assignment_display_order"
    http_status = 409


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pending_proposal_count(tx: CollaborationRepository, child_id: str) -> int:
    return sum(
        1 for p in tx.query(C.PLAN_CHANGE_PROPOSALS, child_id=child_id)
        if p["status"] == ProposalStatus.PENDING_PARENT_ACCEPTANCE.value
    )


def _plan_review_count(tx: CollaborationRepository, child_id: str) -> int:
    return sum(
        1 for a in tx.query(C.PLAN_ASSIGNMENTS, child_id=child_id)
        if a["plan_approval_status"] == PlanApprovalStatus.NEEDS_PLAN_REVIEW.value
        and a["assignment_status"] == AssignmentStatus.CURRENT.value
    )


def _current_in_slot(tx: CollaborationRepository, assignment: dict) -> list:
    """CURRENT assignments sharing this assignment's (child, plan, day).

    A weekday may now hold SEVERAL current activities, so this is a set rather
    than an at-most-one lookup. Ordering within the day is `display_order`;
    retired/replaced rows are excluded and never participate in the current-order
    uniqueness invariant.
    """
    return [
        a for a in tx.query(C.PLAN_ASSIGNMENTS, child_id=assignment["child_id"])
        if a["weekly_plan_id"] == assignment["weekly_plan_id"]
        and a["scheduled_day"] == assignment["scheduled_day"]
        and plain(a["assignment_status"]) == AssignmentStatus.CURRENT.value
    ]


def _order_map(assignments: list) -> dict:
    """Stable {assignment_id: display_order} for comparing a day before/after."""
    return {a["id"]: int(a.get("display_order", 0)) for a in assignments}


def _has_duplicate_display_order(assignments: list) -> bool:
    """True when two CURRENT same-day assignments claim the same position."""
    orders = [int(a.get("display_order", 0)) for a in assignments]
    return len(orders) != len(set(orders))


def _proposal_view(p: dict) -> dict:
    return {
        "proposal_id": p["id"], "proposal_type": plain(p["proposal_type"]),
        "proposal_status": plain(p["status"]), "version": int(p["version"]),
        "decided_by_user_id": p.get("decided_by_user_id"),
        "decided_by_role": plain(p.get("decided_by_role")),
        "decided_at": p.get("decided_at"),
        "resulting_assignment_id": p.get("resulting_assignment_id"),
    }


def _assignment_view(a: dict) -> dict:
    return {
        "assignment_id": a["id"], "child_id": a["child_id"],
        "weekly_plan_id": a["weekly_plan_id"], "scheduled_day": a["scheduled_day"],
        "display_order": int(a.get("display_order", 0)),
        "activity_template_id": a["activity_template_id"],
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


def accept_proposal(
    repo: CollaborationRepository,
    user: AuthenticatedUser,
    child_id: str,
    proposal_id: str,
    idempotency_key: Optional[str],
    expected_proposal_version: int,
    expected_assignment_version: int,
    environment: str = "dev",
    request_id: Optional[str] = None,
) -> dict:
    """Parent accepts one pending modify proposal. Raises typed errors."""
    key = (idempotency_key or "").strip()
    if not key:
        raise MissingIdempotencyKey("Idempotency-Key header is required.")

    # Authorization first: parent role (403), then existence-blind child (404).
    parent = access.resolve_parent(repo, user)
    connection = access.require_parent_child_access(repo, parent["id"], child_id)

    req_hash = canonical_request_hash(
        user.uid, ACTION, child_id, proposal_id,
        {"expected_proposal_version": expected_proposal_version,
         "expected_assignment_version": expected_assignment_version},
    )
    rec_id = idempotency_doc_id(key)

    def op(tx: CollaborationRepository) -> dict:
        # 1. Existence-blind proposal lookup (unknown / other child -> 404).
        rows = tx.query(C.PLAN_CHANGE_PROPOSALS, id=proposal_id)
        if not rows or rows[0]["child_id"] != child_id:
            raise access.ChildNotFound(proposal_id)
        proposal = rows[0]

        # 2. Idempotency BEFORE any state validation.
        #    Critical: after a successful acceptance the proposal is `accepted`
        #    and `pending_proposal_id` is cleared, so re-validating state first
        #    would make an exact replay fail with proposal_already_decided.
        if tx.exists(C.IDEMPOTENCY_RECORDS, rec_id):
            rec = tx.get(C.IDEMPOTENCY_RECORDS, rec_id)
            if rec["request_hash"] == req_hash:
                result = dict(rec["result"])
                result["idempotent_replay"] = True
                return result
            raise IdempotencyKeyConflict("Idempotency-Key reused for a different request.")

        # 3. Proposal shape + decision state.
        if plain(proposal["proposal_type"]) != ProposalType.MODIFY.value:
            raise InvalidParentAcceptTransition(
                f"Only modify proposals can be accepted here (got "
                f"'{plain(proposal['proposal_type'])}')."
            )
        status = plain(proposal["status"])
        if status != ProposalStatus.PENDING_PARENT_ACCEPTANCE.value:
            raise ProposalAlreadyDecided(f"Proposal is already '{status}'.")
        if not proposal.get("proposed_activity_version_id"):
            raise InvalidParentAcceptTransition("Proposal has no proposed activity version.")
        original_assignment_id = proposal.get("target_assignment_id")
        if not original_assignment_id:
            raise ProposalAssignmentMismatch("Proposal references no original assignment.")

        # 4. Proposal optimistic concurrency (no side effects on failure).
        if expected_proposal_version != int(proposal["version"]):
            raise ProposalVersionConflict("expected_proposal_version does not match.")

        # 5. Original assignment lookup + relationship validation.
        arows = tx.query(C.PLAN_ASSIGNMENTS, id=original_assignment_id)
        if not arows or arows[0]["child_id"] != child_id:
            raise ProposalAssignmentMismatch("Original assignment not found for this child.")
        original = arows[0]
        if original.get("pending_proposal_id") != proposal_id:
            raise ProposalAssignmentMismatch(
                "Assignment does not reference this pending proposal."
            )
        if proposal.get("original_activity_version_id") and (
            original["activity_version_id"] != proposal["original_activity_version_id"]
        ):
            raise ProposalAssignmentMismatch(
                "Assignment activity version no longer matches the proposal."
            )

        # 6. Current-plan + approval state.
        plans = tx.query(C.WEEKLY_PLANS, child_id=child_id)
        current_plan_id = plans[0]["id"] if plans else None
        if original["weekly_plan_id"] != current_plan_id:
            raise InvalidParentAcceptTransition("Assignment is not in the current weekly plan.")
        if plain(original["assignment_status"]) != AssignmentStatus.CURRENT.value:
            raise InvalidParentAcceptTransition(
                f"Assignment is '{plain(original['assignment_status'])}', not current."
            )
        if plain(original["plan_approval_status"]) != PlanApprovalStatus.APPROVED.value:
            raise InvalidParentAcceptTransition(
                f"Assignment approval state is "
                f"'{plain(original['plan_approval_status'])}', not approved."
            )

        # 7. Assignment optimistic concurrency.
        if expected_assignment_version != int(original["version"]):
            raise AssignmentVersionConflict("expected_assignment_version does not match.")

        # 8. Proposed ActivityVersion must exist, be immutable, and be unused.
        proposed_version_id = proposal["proposed_activity_version_id"]
        vrows = tx.query(C.ACTIVITY_VERSIONS, id=proposed_version_id)
        if not vrows:
            raise InvalidParentAcceptTransition("Proposed activity version no longer exists.")
        proposed_version = vrows[0]
        if not proposed_version.get("immutable", True):
            raise InvalidParentAcceptTransition("Proposed activity version is not immutable.")
        already_active = [
            a for a in tx.query(C.PLAN_ASSIGNMENTS, child_id=child_id)
            if a["activity_version_id"] == proposed_version_id
            and plain(a["assignment_status"]) == AssignmentStatus.CURRENT.value
        ]
        if already_active:
            raise ReplacementAssignmentConflict(
                "Proposed activity version is already active on a current assignment."
            )

        # 9. Deterministic ids bound to this operation INCLUDING the key hash.
        identity = operation_identity(
            key, user.uid, ACTION, child_id, proposal_id, original_assignment_id
        )
        replacement_id = operation_scoped_id("assign", identity)
        audit_id = operation_scoped_id("aud", identity)
        if tx.exists(C.PLAN_ASSIGNMENTS, replacement_id):
            raise ReplacementAssignmentConflict("Replacement assignment already exists.")

        # 10. Day invariant BEFORE mutating: the original must be among the day's
        #     current assignments (others may share the day), and every current
        #     display_order on that day must be unique.
        current_before = _current_in_slot(tx, original)
        orders_before = _order_map(current_before)
        if original["id"] not in orders_before:
            raise ReplacementAssignmentConflict(
                "Original assignment is not among the day's current assignments."
            )
        if _has_duplicate_display_order(current_before):
            raise DuplicateAssignmentDisplayOrder(
                "Current assignments on this day have duplicate display_order values."
            )
        original_display_order = int(original.get("display_order", 0))

        before_state = {
            "proposal_status": status,
            "proposal_version": int(proposal["version"]),
            "original_assignment_id": original["id"],
            "original_assignment_version": int(original["version"]),
            "original_assignment_status": plain(original["assignment_status"]),
            "original_pending_proposal_id": original.get("pending_proposal_id"),
            "current_activity_version_id": original["activity_version_id"],
            "replacement_assignment_id": None,
        }

        now = _now()

        # 11. Retire the original (kept for history; content untouched).
        original["assignment_status"] = AssignmentStatus.REPLACED.value
        original["pending_proposal_id"] = None
        original["replaced_by_assignment_id"] = replacement_id
        original["replaced_at"] = now
        original["version"] = int(original["version"]) + 1
        original["updated_at"] = now
        tx.set(C.PLAN_ASSIGNMENTS, original["id"], original)

        # 12. Create exactly one replacement, in the original's plan slot.
        #     No further therapist review: the therapist authored the change and
        #     the parent accepted it, so it starts approved.
        replacement = PlanAssignment(
            id=replacement_id,
            weekly_plan_id=original["weekly_plan_id"],
            child_id=child_id,
            activity_template_id=original["activity_template_id"],
            activity_version_id=proposed_version_id,
            scheduled_day=original["scheduled_day"],
            # Same position in the day: accepting a modify must not reorder the
            # family's day around the activity that changed.
            display_order=original_display_order,
            plan_approval_status=PlanApprovalStatus.APPROVED,
            practice_status=PracticeStatus.NOT_TRIED,      # fresh activity, not yet tried
            assignment_status=AssignmentStatus.CURRENT,
            parent_feedback_summary="",
            pending_proposal_id=None,
            replaces_assignment_id=original["id"],
            source_proposal_id=proposal_id,
            version=1,
            created_at=now,
            updated_at=now,
            environment=environment,
        )
        tx.set(C.PLAN_ASSIGNMENTS, replacement_id, replacement.model_dump())

        # 13. Accept the proposal.
        proposal["status"] = ProposalStatus.ACCEPTED.value
        proposal["decided_by_user_id"] = parent["id"]
        proposal["decided_by_role"] = PrincipalRole.PARENT.value
        proposal["decided_at"] = now
        proposal["resulting_assignment_id"] = replacement_id
        proposal["version"] = int(proposal["version"]) + 1
        tx.set(C.PLAN_CHANGE_PROPOSALS, proposal_id, proposal)

        # 14. Day invariant AFTER mutating. The day must be exactly what it was,
        #     with the replacement swapped in for the original at the SAME
        #     position — every unrelated activity untouched.
        current_after = _current_in_slot(tx, original)
        orders_after = _order_map(current_after)
        expected_ids = (set(orders_before) - {original["id"]}) | {replacement_id}
        if set(orders_after) != expected_ids:
            raise ReplacementAssignmentConflict(
                "Post-condition failed: expected the day's current assignments to "
                f"be {sorted(expected_ids)}, found {sorted(orders_after)}."
            )
        if orders_after.get(replacement_id) != original_display_order:
            raise ReplacementAssignmentConflict(
                "Post-condition failed: replacement did not inherit the original's "
                f"display_order ({original_display_order})."
            )
        unrelated = set(orders_before) - {original["id"]}
        if any(orders_after[i] != orders_before[i] for i in unrelated):
            raise ReplacementAssignmentConflict(
                "Post-condition failed: an unrelated same-day assignment moved."
            )
        if _has_duplicate_display_order(current_after):
            raise DuplicateAssignmentDisplayOrder(
                "Current assignments on this day have duplicate display_order values."
            )

        after_state = {
            "proposal_status": ProposalStatus.ACCEPTED.value,
            "proposal_version": int(proposal["version"]),
            "original_assignment_id": original["id"],
            "original_assignment_version": int(original["version"]),
            "original_assignment_status": AssignmentStatus.REPLACED.value,
            "original_pending_proposal_id": None,
            "current_activity_version_id": original["activity_version_id"],
            "replacement_assignment_id": replacement_id,
            "replacement_assignment_status": AssignmentStatus.CURRENT.value,
            "replacement_activity_version_id": proposed_version_id,
            "replacement_plan_approval_status": PlanApprovalStatus.APPROVED.value,
            "current_assignment_count_in_slot": len(current_after),
        }

        # 15. Exactly one immutable audit event.
        audit = AuditEvent(
            id=audit_id, event_type=EVENT_TYPE, actor_uid=user.uid,
            actor_role=PrincipalRole.PARENT, subject_type="plan_change_proposal",
            subject_id=proposal_id, therapist_id=proposal.get("therapist_id"),
            child_id=child_id, weekly_plan_id=original["weekly_plan_id"],
            assignment_id=original["id"], idempotency_key_hash=key_hash(key),
            before_state=before_state, after_state=after_state, request_id=request_id,
            occurred_at=now, created_at=now, environment=environment,
        )
        tx.set(C.AUDIT_EVENTS, audit_id, audit.model_dump())

        result = {
            "proposal": {
                **_proposal_view(proposal),
                "proposal_type": ProposalType.MODIFY.value,
                "original_activity_version_id": proposal.get("original_activity_version_id"),
                "proposed_activity_version_id": proposed_version_id,
            },
            "retired_assignment": _assignment_view(original),
            "replacement_assignment": _assignment_view(
                tx.query(C.PLAN_ASSIGNMENTS, id=replacement_id)[0]
            ),
            "child_summary": {
                "child_id": child_id,
                "plan_review_count": _plan_review_count(tx, child_id),
                "pending_proposal_count": _pending_proposal_count(tx, child_id),
            },
            "audit_event_id": audit_id,
            "idempotent_replay": False,
        }

        # 16. Store the idempotency result inside the same critical section.
        record = IdempotencyRecord(
            id=rec_id, idempotency_key_hash=key_hash(key), actor_user_id=user.uid,
            action=ACTION, child_id=child_id, assignment_id=original["id"],
            request_hash=req_hash, status="completed", result=result,
            audit_event_id=audit_id, created_at=now, environment=environment,
        )
        tx.set(C.IDEMPOTENCY_RECORDS, rec_id, record.model_dump())
        return result

    return repo.run_in_transaction(op)
