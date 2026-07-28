"""Idempotent weekly-plan assignment approval (Phase 1B.1 — first write op).

Fictional, in-memory, development behavior. No Firestore transaction exists yet;
the atomic critical section is provided by `repo.run_in_transaction`. No frontend
is connected; no real data.

One public operation: a therapist approves a single CURRENT weekly-plan
assignment that requires initial plan review (`needs_plan_review → approved`).
The operation is authorization-gated, existence-blind, optimistic-concurrency
checked, idempotent by Idempotency-Key, and writes exactly one audit event.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..auth.interface import AuthenticatedUser
from ..domain.audit_state import assignment_state
from ..domain.enums import (
    AssignmentStatus,
    PlanApprovalStatus,
    PrincipalRole,
)
from ..domain.ids import (
    audit_event_id,
    canonical_request_hash,
    idempotency_doc_id,
    key_hash,
)
from ..domain.read_models import AuditEvent, IdempotencyRecord
from ..repository import collections as C
from ..repository.interface import CollaborationRepository
from . import access

ACTION = "approve_plan_assignment"
EVENT_TYPE = "plan_assignment_approved"


# ── typed errors (mapped to HTTP by app exception handlers) ─────────────────
class ApprovalError(Exception):
    code = "approval_error"
    http_status = 400

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)


class MissingIdempotencyKey(ApprovalError):
    code = "missing_idempotency_key"
    http_status = 400


class IdempotencyKeyConflict(ApprovalError):
    code = "idempotency_key_conflict"
    http_status = 409


class AssignmentVersionConflict(ApprovalError):
    code = "assignment_version_conflict"
    http_status = 409


class InvalidPlanApprovalTransition(ApprovalError):
    code = "invalid_plan_approval_transition"
    http_status = 409


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _assignment_view(a: dict) -> dict:
    return {
        "assignment_id": a["id"],
        "child_id": a["child_id"],
        "weekly_plan_id": a["weekly_plan_id"],
        "scheduled_day": a["scheduled_day"],
        "plan_approval_status": a["plan_approval_status"],
        "practice_status": a["practice_status"],
        "assignment_status": a["assignment_status"],
        "activity_template_id": a["activity_template_id"],
        "activity_version_id": a["activity_version_id"],
        "version": a["version"],
        "updated_at": a.get("updated_at", ""),
    }


def _plan_review_count(tx: CollaborationRepository, child_id: str) -> int:
    return sum(
        1
        for x in tx.query(C.PLAN_ASSIGNMENTS, child_id=child_id)
        if x["plan_approval_status"] == PlanApprovalStatus.NEEDS_PLAN_REVIEW.value
        and x["assignment_status"] == AssignmentStatus.CURRENT.value
    )


def approve_assignment(
    repo: CollaborationRepository,
    user: AuthenticatedUser,
    child_id: str,
    assignment_id: str,
    idempotency_key: Optional[str],
    expected_assignment_version: int,
    environment: str = "dev",
    request_id: Optional[str] = None,
) -> dict:
    """Approve one current, review-needed assignment. Returns the result dict.

    Raises: MissingIdempotencyKey (400), AccessDenied (403), ChildNotFound (404),
    IdempotencyKeyConflict / AssignmentVersionConflict /
    InvalidPlanApprovalTransition (409).
    """
    # C. Missing/blank Idempotency-Key -> 400 (before any mutation attempt).
    key = (idempotency_key or "").strip()
    if not key:
        raise MissingIdempotencyKey("Idempotency-Key header is required.")

    # Authorization (D: failed auth must not create a success idempotency record).
    therapist = access.resolve_therapist(repo, user)          # 403 if parent
    access.require_full_access(repo, therapist["id"], child_id)  # 404 existence-blind

    req_hash = canonical_request_hash(
        user.uid, ACTION, child_id, assignment_id, {"expected_assignment_version": expected_assignment_version}
    )
    rec_id = idempotency_doc_id(key)

    def op(tx: CollaborationRepository) -> dict:
        # 1. Authorization-relevant resource lookup + child match (existence-blind).
        rows = tx.query(C.PLAN_ASSIGNMENTS, id=assignment_id)
        if not rows or rows[0]["child_id"] != child_id:
            raise access.ChildNotFound(assignment_id)  # 404 unknown/mismatch
        a = rows[0]

        # 2/A. Idempotency check FIRST (a replay must not re-run the transition).
        if tx.exists(C.IDEMPOTENCY_RECORDS, rec_id):
            rec = tx.get(C.IDEMPOTENCY_RECORDS, rec_id)
            if rec["request_hash"] == req_hash:
                result = dict(rec["result"])
                result["idempotent_replay"] = True
                return result
            # B. Same key, different operation/body/target/actor -> conflict.
            raise IdempotencyKeyConflict("Idempotency-Key reused for a different request.")

        # 3. Current-plan validation (in the child's current weekly plan + current).
        plans = tx.query(C.WEEKLY_PLANS, child_id=child_id)
        current_plan_id = plans[0]["id"] if plans else None
        if a["weekly_plan_id"] != current_plan_id or a["assignment_status"] != AssignmentStatus.CURRENT.value:
            raise InvalidPlanApprovalTransition("Assignment is not a current weekly-plan item.")

        # 4. State-transition validation: needs_plan_review -> approved only.
        if a["plan_approval_status"] != PlanApprovalStatus.NEEDS_PLAN_REVIEW.value:
            raise InvalidPlanApprovalTransition(
                f"Cannot approve from state '{a['plan_approval_status']}'."
            )

        # 5. Optimistic concurrency: expected version must match (no side effects on fail).
        if expected_assignment_version != a["version"]:
            raise AssignmentVersionConflict("expected_assignment_version does not match.")

        # 6. Assignment mutation (transition + version increment + updated_at).
        # Structured audit state captured BEFORE the mutation (`a` changes in place).
        before_state = assignment_state(a)
        a["plan_approval_status"] = PlanApprovalStatus.APPROVED.value
        a["version"] = int(a["version"]) + 1
        a["updated_at"] = _now()
        tx.set(C.PLAN_ASSIGNMENTS, a["id"], a)
        after_state = assignment_state(a)

        # 7. Exactly one immutable audit event.
        aud_id = audit_event_id(req_hash, key)
        audit = AuditEvent(
            id=aud_id, event_type=EVENT_TYPE, actor_uid=user.uid,
            actor_role=PrincipalRole.THERAPIST, subject_type="plan_assignment",
            subject_id=a["id"], therapist_id=therapist["id"], child_id=child_id,
            weekly_plan_id=a["weekly_plan_id"], assignment_id=a["id"],
            idempotency_key_hash=key_hash(key), before_state=before_state,
            after_state=after_state, request_id=request_id,
            occurred_at=_now(), created_at=_now(), environment=environment,
        )
        tx.set(C.AUDIT_EVENTS, aud_id, audit.model_dump())

        # 9. Post-mutation child plan-review count.
        result = {
            "assignment": _assignment_view(a),
            "child_summary": {"child_id": child_id, "plan_review_count": _plan_review_count(tx, child_id)},
            "audit_event_id": aud_id,
            "idempotent_replay": False,
        }

        # 8. Store idempotency result atomically (same critical section).
        record = IdempotencyRecord(
            id=rec_id, idempotency_key_hash=key_hash(key), actor_user_id=user.uid,
            action=ACTION, child_id=child_id, assignment_id=assignment_id,
            request_hash=req_hash, status="completed", result=result,
            audit_event_id=aud_id, created_at=_now(), environment=environment,
        )
        tx.set(C.IDEMPOTENCY_RECORDS, rec_id, record.model_dump())
        return result

    return repo.run_in_transaction(op)
