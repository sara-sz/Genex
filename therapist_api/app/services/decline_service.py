"""Idempotent parent decline of a modify proposal (Phase 1B.2B.2).

Fictional, in-memory, dev-only. No Firestore, no Firebase Auth, no Cloud Run, no
frontend, no real data. **No parent proposal-read endpoint is added here.**

Decline is the mirror of acceptance, and its defining property is what it does
NOT do: no replacement assignment is created and the original plan item is left
exactly as the family already knows it. In ONE atomic critical section this:

  * marks the proposal `declined` (version +1, decision metadata recorded,
    `resulting_assignment_id` left null),
  * clears the original assignment's `pending_proposal_id` (version +1) while
    leaving it `current`, `approved`, on its original activity version, with its
    practice history untouched, and
  * writes exactly one immutable audit event plus one idempotency result.

The proposed derived ActivityVersion is preserved unchanged as history and is
never activated.

Read-only response/count helpers are imported from `acceptance_service` so both
parent decisions report identical shapes; no acceptance behavior is modified.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..auth.interface import AuthenticatedUser
from ..domain.audit_state import plain
from ..domain.enums import (
    AssignmentStatus,
    PlanApprovalStatus,
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
from ..domain.read_models import AuditEvent, IdempotencyRecord
from ..repository import collections as C
from ..repository.interface import CollaborationRepository
from . import access, add_decision_service
from .weekly_plan import current_weekly_plan_id
from .proposal_service import InvalidRequest
from .acceptance_service import (  # shared read helpers — response-shape parity
    _assignment_view as assignment_view,
    _plan_review_count as plan_review_count,
    _pending_proposal_count as pending_proposal_count,
    _proposal_view as proposal_view,
)
from .acceptance_service import (  # shared typed errors whose meaning still holds
    ProposalAlreadyDecided,
    ProposalAssignmentMismatch,
    ProposalVersionConflict,
)
from .assignment_order import (  # shared read-only day-ordering invariant
    DuplicateAssignmentDisplayOrder,
    assignment_order_map as order_map,
    current_assignments_sharing_day as current_in_slot,
    has_duplicate_display_order,
)
from .approval_service import (
    ApprovalError,
    AssignmentVersionConflict,
    IdempotencyKeyConflict,
    MissingIdempotencyKey,
)

ACTION = "decline_plan_change_proposal"
EVENT_TYPE = "plan_change_proposal_declined"


class InvalidParentDeclineTransition(ApprovalError):
    code = "invalid_parent_decline_transition"
    http_status = 409


class CurrentAssignmentConflict(ApprovalError):
    """The affected plan slot does not hold exactly the original assignment.

    Decline creates no replacement, so the invariant it protects is that the
    ORIGINAL remains the single current assignment — hence a decline-specific
    code rather than acceptance's `replacement_assignment_conflict`.
    """

    code = "current_assignment_conflict"
    http_status = 409


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def decline_proposal(
    repo: CollaborationRepository,
    user: AuthenticatedUser,
    child_id: str,
    proposal_id: str,
    idempotency_key: Optional[str],
    expected_proposal_version: int,
    expected_assignment_version: Optional[int] = None,
    environment: str = "dev",
    request_id: Optional[str] = None,
) -> dict:
    """Parent declines one pending proposal — MODIFY or ADD. Raises typed errors.

    `expected_assignment_version` is optional in the SIGNATURE only; MODIFY still
    requires it, an ADD touches no assignment and must not fabricate one.
    """
    key = (idempotency_key or "").strip()
    if not key:
        raise MissingIdempotencyKey("Idempotency-Key header is required.")

    # Authorization first: parent role (403), then existence-blind child (404).
    parent = access.resolve_parent(repo, user)
    access.require_parent_child_access(repo, parent["id"], child_id)

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
        #    After a successful decline the proposal is `declined` and
        #    `pending_proposal_id` is null, so validating state first would make
        #    an exact replay fail with proposal_already_decided.
        if tx.exists(C.IDEMPOTENCY_RECORDS, rec_id):
            rec = tx.get(C.IDEMPOTENCY_RECORDS, rec_id)
            if rec["request_hash"] == req_hash:
                result = dict(rec["result"])
                result["idempotent_replay"] = True
                return result
            raise IdempotencyKeyConflict("Idempotency-Key reused for a different request.")

        # 3a. Type dispatch, INSIDE the transaction so the type cannot change
        #     between a client's earlier GET and this decision.
        proposal_type = plain(proposal["proposal_type"])
        if proposal_type == ProposalType.ADD.value:
            return add_decision_service.decline_add(
                tx, user=user, parent=parent, child_id=child_id,
                proposal_id=proposal_id, proposal=proposal, key=key,
                req_hash=req_hash, rec_id=rec_id,
                expected_proposal_version=expected_proposal_version,
                action=ACTION,
                already_decided_error=ProposalAlreadyDecided,
                version_conflict_error=ProposalVersionConflict,
                invalid_transition_error=InvalidParentDeclineTransition,
                environment=environment, request_id=request_id,
            )
        if proposal_type != ProposalType.MODIFY.value:
            raise InvalidParentDeclineTransition(
                f"Only modify and add proposals can be declined (got "
                f"'{proposal_type}')."
            )

        # 3b. MODIFY still REQUIRES expected_assignment_version — the frozen
        #     optimistic-concurrency contract, enforced here now that the
        #     transport schema allows ADD to omit it.
        if expected_assignment_version is None:
            raise InvalidRequest(
                "expected_assignment_version is required for a modify decision."
            )

        # 3. Proposal shape + decision state.
        status = plain(proposal["status"])
        if status != ProposalStatus.PENDING_PARENT_ACCEPTANCE.value:
            raise ProposalAlreadyDecided(f"Proposal is already '{status}'.")
        proposed_version_id = proposal.get("proposed_activity_version_id")
        if not proposed_version_id:
            raise InvalidParentDeclineTransition("Proposal has no proposed activity version.")
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

        # 6. Current-plan + approval state. Canonical resolver: zero or several
        #    CURRENT plans both yield None and raise below — never a guess.
        current_plan_id = current_weekly_plan_id(tx, child_id)
        if original["weekly_plan_id"] != current_plan_id:
            raise InvalidParentDeclineTransition(
                "Assignment is not in the current weekly plan."
            )
        if plain(original["assignment_status"]) != AssignmentStatus.CURRENT.value:
            raise InvalidParentDeclineTransition(
                f"Assignment is '{plain(original['assignment_status'])}', not current."
            )
        if plain(original["plan_approval_status"]) != PlanApprovalStatus.APPROVED.value:
            raise InvalidParentDeclineTransition(
                f"Assignment approval state is "
                f"'{plain(original['plan_approval_status'])}', not approved."
            )

        # 7. Assignment optimistic concurrency.
        if expected_assignment_version != int(original["version"]):
            raise AssignmentVersionConflict("expected_assignment_version does not match.")

        # 8. Proposed ActivityVersion must exist, be immutable, and be inactive.
        vrows = tx.query(C.ACTIVITY_VERSIONS, id=proposed_version_id)
        if not vrows:
            raise InvalidParentDeclineTransition(
                "Proposed activity version no longer exists."
            )
        if not vrows[0].get("immutable", True):
            raise InvalidParentDeclineTransition(
                "Proposed activity version is not immutable."
            )
        active_with_proposed = [
            a for a in tx.query(C.PLAN_ASSIGNMENTS, child_id=child_id)
            if a["activity_version_id"] == proposed_version_id
            and plain(a["assignment_status"]) == AssignmentStatus.CURRENT.value
        ]
        if active_with_proposed:
            raise CurrentAssignmentConflict(
                "Proposed activity version is already active on a current assignment."
            )

        # 9. Deterministic ids bound to this operation INCLUDING the key hash.
        #    Decline creates no assignment, so only the audit id is derived here.
        identity = operation_identity(
            key, user.uid, ACTION, child_id, proposal_id, original_assignment_id
        )
        audit_id = operation_scoped_id("aud", identity)

        # 10. Day pre-check: the original must be among the day's current
        #     assignments (others may share the day), with unique display_order.
        current_before = current_in_slot(tx, original)
        orders_before = order_map(current_before)
        if original["id"] not in orders_before:
            raise CurrentAssignmentConflict(
                "Original assignment is not among the day's current assignments."
            )
        if has_duplicate_display_order(current_before):
            raise DuplicateAssignmentDisplayOrder(
                "Current assignments on this day have duplicate display_order values."
            )

        before_state = {
            "proposal_status": status,
            "proposal_version": int(proposal["version"]),
            "original_assignment_id": original["id"],
            "original_assignment_version": int(original["version"]),
            "original_assignment_status": plain(original["assignment_status"]),
            "original_pending_proposal_id": original.get("pending_proposal_id"),
            "current_activity_version_id": original["activity_version_id"],
            "proposed_activity_version_id": proposed_version_id,
            "current_assignment_count_in_slot": len(current_before),
        }

        now = _now()

        # 11. Decline the proposal (kept for history; no resulting assignment).
        proposal["status"] = ProposalStatus.DECLINED.value
        proposal["decided_by_user_id"] = parent["id"]
        proposal["decided_by_role"] = PrincipalRole.PARENT.value
        proposal["decided_at"] = now
        proposal["resulting_assignment_id"] = None
        proposal["version"] = int(proposal["version"]) + 1
        tx.set(C.PLAN_CHANGE_PROPOSALS, proposal_id, proposal)

        # 12. Release the original: ONLY the pending link is cleared. It stays
        #     current and approved, on its original activity version, with its
        #     practice history and replacement fields untouched.
        original["pending_proposal_id"] = None
        original["version"] = int(original["version"]) + 1
        original["updated_at"] = now
        tx.set(C.PLAN_ASSIGNMENTS, original["id"], original)

        # 13. Day post-check: the ENTIRE day is unchanged — same set of current
        #     assignments, each at the same display_order — and no replacement
        #     was introduced anywhere.
        current_after = current_in_slot(tx, original)
        orders_after = order_map(current_after)
        if orders_after != orders_before:
            raise CurrentAssignmentConflict(
                "Post-condition failed: declining must leave the day's current "
                f"assignments untouched; before={sorted(orders_before.items())} "
                f"after={sorted(orders_after.items())}."
            )
        replacements = [
            a for a in tx.query(C.PLAN_ASSIGNMENTS, child_id=child_id)
            if a.get("source_proposal_id") == proposal_id
            or a.get("replaces_assignment_id") == original["id"]
        ]
        if replacements:
            raise CurrentAssignmentConflict(
                f"Decline must create no replacement, found {[a['id'] for a in replacements]}."
            )

        after_state = {
            "proposal_status": ProposalStatus.DECLINED.value,
            "proposal_version": int(proposal["version"]),
            "original_assignment_id": original["id"],
            "original_assignment_version": int(original["version"]),
            "original_assignment_status": plain(original["assignment_status"]),
            "original_pending_proposal_id": None,
            "current_activity_version_id": original["activity_version_id"],
            "proposed_activity_version_id": proposed_version_id,
            "proposed_activity_version_active": False,
            "replacement_assignment_id": None,
            "current_assignment_count_in_slot": len(current_after),
        }

        # 14. Exactly one immutable audit event.
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
                **proposal_view(proposal),
                "proposal_type": ProposalType.MODIFY.value,
                "original_activity_version_id": proposal.get("original_activity_version_id"),
                "proposed_activity_version_id": proposed_version_id,
            },
            "current_assignment": assignment_view(original),
            "child_summary": {
                "child_id": child_id,
                "plan_review_count": plan_review_count(tx, child_id),
                "pending_proposal_count": pending_proposal_count(tx, child_id),
            },
            "audit_event_id": audit_id,
            "idempotent_replay": False,
        }

        # 15. Store the idempotency result inside the same critical section.
        record = IdempotencyRecord(
            id=rec_id, idempotency_key_hash=key_hash(key), actor_user_id=user.uid,
            action=ACTION, child_id=child_id, assignment_id=original["id"],
            request_hash=req_hash, status="completed", result=result,
            audit_event_id=audit_id, created_at=now, environment=environment,
        )
        tx.set(C.IDEMPOTENCY_RECORDS, rec_id, record.model_dump())
        return result

    return repo.run_in_transaction(op)
