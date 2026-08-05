"""Idempotent modify-activity proposal creation (Phase 1B.2A).

Fictional, in-memory, dev-only. No Firestore transaction yet (atomic critical
section via `repo.run_in_transaction`). No frontend. No real data. **Parent
acceptance/decline is NOT implemented in this phase** — the proposal enters
`pending_parent_acceptance` and the original assignment remains active.

A therapist proposes a modified version of one CURRENT, APPROVED weekly-plan
activity. This creates an immutable derived ActivityVersion + a
PlanChangeProposal, sets the assignment's `pending_proposal_id`, and increments
the assignment version — WITHOUT changing the assignment's plan-approval status,
retiring/replacing it, or creating a second current assignment.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..auth.interface import AuthenticatedUser
from ..domain.audit_state import assignment_state
from ..domain.domains import display_for_domain_key
from ..domain.enums import (
    ActivitySaveScope,
    AssignmentStatus,
    CreatedByType,
    PlanApprovalStatus,
    PrincipalRole,
    ProposalStatus,
    ProposalType,
)
from ..domain.ids import (
    audit_event_id,
    canonical_request_hash,
    derived_id,
    idempotency_doc_id,
    key_hash,
)
from ..domain.read_models import (
    ActivityVersion,
    AuditEvent,
    IdempotencyRecord,
    PlanChangeProposal,
)
from ..repository import collections as C
from ..repository.interface import CollaborationRepository
from . import access
from .approval_service import (  # reuse shared base + generic write errors
    ApprovalError,
    AssignmentVersionConflict,
    IdempotencyKeyConflict,
    MissingIdempotencyKey,
)

ACTION = "create_modify_proposal"
EVENT_TYPE = "plan_change_proposal_created"


class InvalidRequest(ApprovalError):
    code = "invalid_request"
    http_status = 422


class InvalidModifyProposalTransition(ApprovalError):
    code = "invalid_modify_proposal_transition"
    http_status = 409


class PendingProposalExists(ApprovalError):
    code = "pending_proposal_exists"
    http_status = 409


class MilestoneDomainMismatch(ApprovalError):
    code = "milestone_domain_mismatch"
    http_status = 422


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pending_proposal_count(tx: CollaborationRepository, child_id: str) -> int:
    return sum(
        1 for p in tx.query(C.PLAN_CHANGE_PROPOSALS, child_id=child_id)
        if p["status"] == ProposalStatus.PENDING_PARENT_ACCEPTANCE.value
    )


def _plan_review_count(tx: CollaborationRepository, child_id: str) -> int:
    return sum(
        1 for x in tx.query(C.PLAN_ASSIGNMENTS, child_id=child_id)
        if x["plan_approval_status"] == PlanApprovalStatus.NEEDS_PLAN_REVIEW.value
        and x["assignment_status"] == AssignmentStatus.CURRENT.value
    )


def _assignment_view(a: dict) -> dict:
    return {
        "assignment_id": a["id"], "child_id": a["child_id"], "weekly_plan_id": a["weekly_plan_id"],
        "scheduled_day": a["scheduled_day"], "display_order": int(a.get("display_order", 0)),
        "plan_approval_status": a["plan_approval_status"],
        "practice_status": a["practice_status"], "assignment_status": a["assignment_status"],
        "activity_template_id": a["activity_template_id"], "activity_version_id": a["activity_version_id"],
        "pending_proposal_id": a.get("pending_proposal_id"), "version": a["version"],
        "updated_at": a.get("updated_at", ""),
    }


def create_modify_proposal(
    repo: CollaborationRepository,
    user: AuthenticatedUser,
    child_id: str,
    assignment_id: str,
    idempotency_key: Optional[str],
    expected_assignment_version: int,
    activity: dict,
    change_reason: str = "",
    save_scope: str = ActivitySaveScope.CHILD_ONLY.value,
    environment: str = "dev",
    request_id: Optional[str] = None,
) -> dict:
    """Create a modify proposal. Returns the result dict. Raises typed errors."""
    key = (idempotency_key or "").strip()
    if not key:
        raise MissingIdempotencyKey("Idempotency-Key header is required.")

    # Authorization (existence-blind for the child).
    therapist = access.resolve_therapist(repo, user)            # 403 if parent
    access.require_full_access(repo, therapist["id"], child_id)  # 404 existence-blind

    if save_scope not in {s.value for s in ActivitySaveScope}:
        raise InvalidRequest(f"Unsupported save_scope '{save_scope}'.")

    req_hash = canonical_request_hash(
        user.uid, ACTION, child_id, assignment_id,
        {"expected_assignment_version": expected_assignment_version,
         "activity": activity, "change_reason": change_reason, "save_scope": save_scope},
    )
    rec_id = idempotency_doc_id(key)

    def op(tx: CollaborationRepository) -> dict:
        # 1. Resource lookup + child match (existence-blind).
        rows = tx.query(C.PLAN_ASSIGNMENTS, id=assignment_id)
        if not rows or rows[0]["child_id"] != child_id:
            raise access.ChildNotFound(assignment_id)
        a = rows[0]

        # 2/A. Idempotency check FIRST (a replay must not re-create anything).
        if tx.exists(C.IDEMPOTENCY_RECORDS, rec_id):
            rec = tx.get(C.IDEMPOTENCY_RECORDS, rec_id)
            if rec["request_hash"] == req_hash:
                result = dict(rec["result"])
                result["idempotent_replay"] = True
                return result
            raise IdempotencyKeyConflict("Idempotency-Key reused for a different request.")

        # 3. Current-plan validation.
        plans = tx.query(C.WEEKLY_PLANS, child_id=child_id)
        current_plan_id = plans[0]["id"] if plans else None
        if a["weekly_plan_id"] != current_plan_id or a["assignment_status"] != AssignmentStatus.CURRENT.value:
            raise InvalidModifyProposalTransition("Assignment is not a current weekly-plan item.")

        # 4/7. Valid-state rule: must be an approved, current assignment.
        if a["plan_approval_status"] != PlanApprovalStatus.APPROVED.value:
            raise InvalidModifyProposalTransition(
                f"Cannot modify from plan-approval state '{a['plan_approval_status']}'."
            )

        # existing pending proposal -> conflict
        if a.get("pending_proposal_id"):
            raise PendingProposalExists("Assignment already has a pending proposal.")

        # 5. Expected-version check (no side effects on fail).
        if expected_assignment_version != a["version"]:
            raise AssignmentVersionConflict("expected_assignment_version does not match.")

        # 6. Milestone / domain validation.
        milestone_id = (activity.get("milestone_id") or "").strip()
        domain_key = (activity.get("developmental_domain") or "").strip()
        display_domain = display_for_domain_key(domain_key)
        if not display_domain:
            raise MilestoneDomainMismatch(f"Unknown developmental_domain '{domain_key}'.")
        mrows = tx.query(C.MILESTONES, id=milestone_id)
        if not mrows:
            raise MilestoneDomainMismatch(f"Unknown milestone '{milestone_id}'.")
        if mrows[0]["domain"] != display_domain:
            raise MilestoneDomainMismatch("milestone does not match developmental_domain.")

        # 7. Immutable derived ActivityVersion (original template/version preserved).
        original_version = tx.query(C.ACTIVITY_VERSIONS, id=a["activity_version_id"])[0]
        template_id = a["activity_template_id"]
        new_ver_id = derived_id("ver", req_hash)
        derived = ActivityVersion(
            id=new_ver_id, activity_template_id=template_id, version_number=2,
            title=activity.get("title", ""), domain=display_domain, developmental_domain_key=domain_key,
            milestone_ids=[milestone_id], milestone_id=milestone_id,
            skill_focus=activity.get("skill_focus", ""), duration_minutes=activity.get("duration_minutes"),
            difficulty=activity.get("difficulty", ""), materials=list(activity.get("materials", [])),
            materials_type=activity.get("materials_type", ""), setup=activity.get("setup", ""),
            parent_instructions=list(activity.get("parent_instructions", [])),
            what_to_say=list(activity.get("what_to_say", [])), how_to_help=list(activity.get("how_to_help", [])),
            success_signals=list(activity.get("success_signals", [])),
            variations=list(activity.get("variations", [])), routine_tags=list(activity.get("routine_tags", [])),
            theme_tags=list(activity.get("theme_tags", [])), safety_risk_flags=list(activity.get("safety_risk_flags", [])),
            created_by_type=CreatedByType.THERAPIST, created_by_user_id=therapist["id"],
            created_by_display_name=therapist.get("display_name"),
            original_activity_template_id=template_id, original_activity_version_id=original_version["id"],
            modified_by_user_id=therapist["id"], modified_by_display_name=therapist.get("display_name"),
            save_scope=save_scope, is_derived=True, immutable=True, created_at=_now(), environment=environment,
        )
        tx.set(C.ACTIVITY_VERSIONS, new_ver_id, derived.model_dump())

        # 8. PlanChangeProposal (references original + proposed).
        prop_id = derived_id("prop", req_hash)
        proposal = PlanChangeProposal(
            id=prop_id, child_id=child_id, therapist_id=therapist["id"], weekly_plan_id=a["weekly_plan_id"],
            proposal_type=ProposalType.MODIFY, status=ProposalStatus.PENDING_PARENT_ACCEPTANCE,
            target_assignment_id=a["id"], original_activity_template_id=template_id,
            original_activity_version_id=original_version["id"], proposed_activity_version_id=new_ver_id,
            change_reason=change_reason, save_scope=save_scope, rationale=change_reason,
            created_by_user_id=therapist["id"], created_at=_now(), version=1, environment=environment,
        )
        tx.set(C.PLAN_CHANGE_PROPOSALS, prop_id, proposal.model_dump())

        # 9/10. Assignment: set pending_proposal_id + version++ + updated_at. Approval status UNCHANGED.
        # Structured audit state captured BEFORE the mutation (`a` changes in place).
        before_state = assignment_state(a)
        a["pending_proposal_id"] = prop_id
        a["version"] = int(a["version"]) + 1
        a["updated_at"] = _now()
        tx.set(C.PLAN_ASSIGNMENTS, a["id"], a)
        # The proposal is recorded on the AFTER side only — it did not exist before.
        # plan_approval_status / assignment_status / current_activity_version_id are
        # deliberately unchanged: the original assignment stays active, nothing is
        # replaced, and the pending proposal is merely attached.
        after_state = assignment_state(
            a,
            proposed_activity_version_id=new_ver_id,
            proposal_id=prop_id,
            proposal_status=ProposalStatus.PENDING_PARENT_ACCEPTANCE.value,
        )

        # 11. Exactly one immutable audit event.
        aud_id = audit_event_id(req_hash, key)
        audit = AuditEvent(
            id=aud_id, event_type=EVENT_TYPE, actor_uid=user.uid, actor_role=PrincipalRole.THERAPIST,
            subject_type="plan_change_proposal", subject_id=prop_id, therapist_id=therapist["id"],
            child_id=child_id, weekly_plan_id=a["weekly_plan_id"], assignment_id=a["id"],
            idempotency_key_hash=key_hash(key), before_state=before_state, after_state=after_state,
            request_id=request_id, occurred_at=_now(), created_at=_now(), environment=environment,
        )
        tx.set(C.AUDIT_EVENTS, aud_id, audit.model_dump())

        # 13/14. Counts.
        result = {
            "proposal": {
                "proposal_id": prop_id, "proposal_type": "modify",
                "proposal_status": ProposalStatus.PENDING_PARENT_ACCEPTANCE.value,
                "child_id": child_id, "weekly_plan_id": a["weekly_plan_id"], "current_assignment_id": a["id"],
                "original_activity_template_id": template_id, "original_activity_version_id": original_version["id"],
                "proposed_activity_version_id": new_ver_id, "created_by_user_id": therapist["id"],
                "created_at": proposal.created_at, "version": 1,
            },
            "proposed_activity_version": derived.model_dump(),
            "current_assignment": _assignment_view(a),
            "child_summary": {
                "child_id": child_id,
                "plan_review_count": _plan_review_count(tx, child_id),
                "pending_proposal_count": _pending_proposal_count(tx, child_id),
            },
            "audit_event_id": aud_id,
            "idempotent_replay": False,
        }

        # 12. Store idempotency result atomically.
        record = IdempotencyRecord(
            id=rec_id, idempotency_key_hash=key_hash(key), actor_user_id=user.uid, action=ACTION,
            child_id=child_id, assignment_id=assignment_id, request_hash=req_hash, status="completed",
            result=result, audit_event_id=aud_id, created_at=_now(), environment=environment,
        )
        tx.set(C.IDEMPOTENCY_RECORDS, rec_id, record.model_dump())
        return result

    return repo.run_in_transaction(op)
