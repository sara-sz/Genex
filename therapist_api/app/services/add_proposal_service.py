"""Idempotent ADD-activity proposal creation (Phase 1B.2D).

Fictional, in-memory, dev-only. No Firestore, no Firebase Auth, no Cloud Run, no
frontend, no real data. The atomic critical section is `repo.run_in_transaction`
(snapshot/restore rollback), shaped to map onto a future Firestore transaction.

**Add means ADD ANOTHER activity to the chosen weekday.** Genex already generates
an activity for most days, so the destination day does not need to be empty and
may already hold several current activities. An Add must therefore never behave
like a Replace: nothing existing is touched, retired, reordered or renumbered.

What this phase creates, and nothing else:

  * one immutable derived ActivityVersion,
  * one PlanChangeProposal of type `add`, targeting a WEEKDAY (no
    `target_assignment_id`, no original activity),
  * one structured audit event,
  * one idempotency record.

**Zero PlanAssignments are created and no display_order is reserved.** Position
allocation — `max(display_order on that day) + 1` — belongs inside the future
Add-*acceptance* transaction, which is the only place two concurrent acceptances
can be ordered without colliding. Reserving a position here would hand out the
same number to two pending proposals.

Parent visibility, acceptance and decline of ADD are NOT implemented.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..auth.interface import AuthenticatedUser
from ..domain.audit_state import plain
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
    idempotency_doc_id,
    key_hash,
    operation_identity,
    operation_scoped_id,
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
from .weekly_plan import current_weekly_plan_id
from .approval_service import (  # shared base + generic write errors
    ApprovalError,
    IdempotencyKeyConflict,
    MissingIdempotencyKey,
)
from .assignment_order import (  # shared read-only day-ordering invariant
    DuplicateAssignmentDisplayOrder,
    assignment_order_map,
    current_assignments_for_day,
    has_duplicate_display_order,
)
from .proposal_service import InvalidRequest, MilestoneDomainMismatch

ACTION = "create_add_activity_proposal"
EVENT_TYPE = "add_activity_proposal_created"

#: Monday..Sunday, matching `PlanAssignment.scheduled_day`.
MIN_SCHEDULED_DAY = 0
MAX_SCHEDULED_DAY = 6


class WeeklyPlanConflict(ApprovalError):
    """`expected_weekly_plan_id` is not the child's current weekly plan."""

    code = "weekly_plan_conflict"
    http_status = 409


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def destination_target_token(weekly_plan_id: str, scheduled_day: int) -> str:
    """Internal idempotency operation target for a destination WEEKDAY.

    `IdempotencyRecord` was shaped around assignment-oriented writes and requires
    an operation target, but an Add has no assignment at creation time — that is
    the whole point of the corrected product model. Rather than refactor the
    record (out of scope for this phase) we give the operation a deterministic
    token naming the day it acts on.

    It is an INTERNAL idempotency detail only. It is never an assignment id, is
    never written to a semantic audit field such as `AuditEvent.assignment_id`,
    is never returned by any API, and never reaches a parent or therapist view.
    Two different Idempotency-Keys naming the same token remain two distinct
    operations — sharing a weekday is not a conflict.
    """
    return f"{weekly_plan_id}:{scheduled_day}"


def _pending_proposal_count(tx: CollaborationRepository, child_id: str) -> int:
    return sum(
        1 for p in tx.query(C.PLAN_CHANGE_PROPOSALS, child_id=child_id)
        if plain(p["status"]) == ProposalStatus.PENDING_PARENT_ACCEPTANCE.value
    )


def _plan_review_count(tx: CollaborationRepository, child_id: str) -> int:
    return sum(
        1 for a in tx.query(C.PLAN_ASSIGNMENTS, child_id=child_id)
        if plain(a["plan_approval_status"]) == PlanApprovalStatus.NEEDS_PLAN_REVIEW.value
        and plain(a["assignment_status"]) == AssignmentStatus.CURRENT.value
    )


def create_add_proposal(
    repo: CollaborationRepository,
    user: AuthenticatedUser,
    child_id: str,
    idempotency_key: Optional[str],
    scheduled_day: int,
    expected_weekly_plan_id: str,
    activity: dict,
    change_reason: str = "",
    save_scope: str = ActivitySaveScope.CHILD_ONLY.value,
    environment: str = "dev",
    request_id: Optional[str] = None,
) -> dict:
    """Create an Add-activity proposal for one weekday. Raises typed errors."""
    key = (idempotency_key or "").strip()
    if not key:
        raise MissingIdempotencyKey("Idempotency-Key header is required.")

    # Authorization (existence-blind for the child), identical to Modify creation.
    therapist = access.resolve_therapist(repo, user)             # 403 if parent
    access.require_full_access(repo, therapist["id"], child_id)  # 404 existence-blind

    # Static request validation — not state validation, so it may precede the
    # idempotency lookup exactly as Modify creation validates save_scope.
    if save_scope not in {s.value for s in ActivitySaveScope}:
        raise InvalidRequest(f"Unsupported save_scope '{save_scope}'.")
    if not isinstance(scheduled_day, int) or isinstance(scheduled_day, bool):
        raise InvalidRequest("scheduled_day must be an integer 0-6.")
    if not MIN_SCHEDULED_DAY <= scheduled_day <= MAX_SCHEDULED_DAY:
        raise InvalidRequest(
            f"scheduled_day must be between {MIN_SCHEDULED_DAY} and {MAX_SCHEDULED_DAY}."
        )
    if not (expected_weekly_plan_id or "").strip():
        raise InvalidRequest("expected_weekly_plan_id is required.")

    target_token = destination_target_token(expected_weekly_plan_id, scheduled_day)
    req_hash = canonical_request_hash(
        user.uid, ACTION, child_id, target_token,
        {"scheduled_day": scheduled_day,
         "expected_weekly_plan_id": expected_weekly_plan_id,
         "activity": activity, "change_reason": change_reason, "save_scope": save_scope},
    )
    rec_id = idempotency_doc_id(key)

    # Document ids bind the Idempotency-Key's HASH, not just the request hash.
    #
    # Modify seeds its ids on the request hash alone, which is safe only because
    # `expected_assignment_version` participates and increments on every write —
    # so a second operation with the same hash is unreachable. An Add request
    # carries no version at all: the same therapist could legitimately send the
    # SAME activity to the SAME day again under a NEW key. Seeding on the request
    # hash would derive the same proposal id and silently overwrite the first
    # proposal instead of creating a second one. Binding the key hash keeps
    # independent attempts on independent documents, while an exact retry of the
    # same key still resolves to the same ids and replays.
    identity = operation_identity(
        key, user.uid, ACTION, child_id,
        proposal_id="",                 # no proposal exists yet
        assignment_id=target_token,     # internal destination token, NOT an assignment
    )
    prop_id = operation_scoped_id("prop", identity)
    new_ver_id = operation_scoped_id("ver", identity)

    def op(tx: CollaborationRepository) -> dict:
        # 1. Idempotency FIRST, before any state validation, so an exact replay
        #    returns the stored result even if the day has changed since.
        if tx.exists(C.IDEMPOTENCY_RECORDS, rec_id):
            rec = tx.get(C.IDEMPOTENCY_RECORDS, rec_id)
            if rec["request_hash"] == req_hash:
                result = dict(rec["result"])
                result["idempotent_replay"] = True
                return result
            raise IdempotencyKeyConflict("Idempotency-Key reused for a different request.")

        # 2. Current-plan validation via the canonical resolver. An ambiguous
        #    lifecycle (zero or several CURRENT plans) yields None and conflicts.
        current_plan_id = current_weekly_plan_id(tx, child_id)
        if not current_plan_id or expected_weekly_plan_id != current_plan_id:
            raise WeeklyPlanConflict(
                "expected_weekly_plan_id is not the child's current weekly plan."
            )

        # 3. Destination-day integrity.
        #
        #    Deliberately NOT an emptiness check. Zero, one or many current
        #    activities are all valid destinations — Add appends alongside them.
        #    The only thing that must hold is that the day is unambiguously
        #    ordered, because a future acceptance will compute max(order) + 1 and
        #    a duplicated position would make that allocation unsound.
        day_before = current_assignments_for_day(
            tx, child_id, current_plan_id, scheduled_day
        )
        if has_duplicate_display_order(day_before):
            raise DuplicateAssignmentDisplayOrder(
                "Two current assignments on this weekday share a display_order."
            )
        orders_before = assignment_order_map(day_before)

        # 4. Milestone / domain validation (same canonical rules as Modify).
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

        # 5. Immutable proposed ActivityVersion.
        #
        #    An Add has no original activity, so `original_activity_*` stay null
        #    and `is_derived` is False: this version is newly authored by the
        #    therapist rather than derived from an existing plan item.
        proposed = ActivityVersion(
            id=new_ver_id, activity_template_id=None, version_number=1,
            title=activity.get("title", ""), domain=display_domain,
            developmental_domain_key=domain_key,
            milestone_ids=[milestone_id], milestone_id=milestone_id,
            skill_focus=activity.get("skill_focus", ""),
            duration_minutes=activity.get("duration_minutes"),
            difficulty=activity.get("difficulty", ""),
            materials=list(activity.get("materials", [])),
            materials_type=activity.get("materials_type", ""),
            setup=activity.get("setup", ""),
            parent_instructions=list(activity.get("parent_instructions", [])),
            what_to_say=list(activity.get("what_to_say", [])),
            how_to_help=list(activity.get("how_to_help", [])),
            success_signals=list(activity.get("success_signals", [])),
            variations=list(activity.get("variations", [])),
            routine_tags=list(activity.get("routine_tags", [])),
            theme_tags=list(activity.get("theme_tags", [])),
            safety_risk_flags=list(activity.get("safety_risk_flags", [])),
            created_by_type=CreatedByType.THERAPIST, created_by_user_id=therapist["id"],
            created_by_display_name=therapist.get("display_name"),
            original_activity_template_id=None, original_activity_version_id=None,
            modified_by_user_id=None, modified_by_display_name=None,
            save_scope=save_scope, is_derived=False, immutable=True,
            created_at=_now(), environment=environment,
        )
        tx.set(C.ACTIVITY_VERSIONS, new_ver_id, proposed.model_dump())

        # 6. The proposal — targets a WEEKDAY, not an assignment.
        proposal = PlanChangeProposal(
            id=prop_id, child_id=child_id, therapist_id=therapist["id"],
            weekly_plan_id=current_plan_id,
            proposal_type=ProposalType.ADD,
            status=ProposalStatus.PENDING_PARENT_ACCEPTANCE,
            target_assignment_id=None,                  # Add replaces nothing
            original_activity_template_id=None,
            original_activity_version_id=None,
            proposed_activity_version_id=new_ver_id,
            destination_scheduled_day=scheduled_day,
            change_reason=change_reason, save_scope=save_scope, rationale=change_reason,
            created_by_user_id=therapist["id"], created_at=_now(), version=1,
            resulting_assignment_id=None,               # no assignment until acceptance
            environment=environment,
        )
        tx.set(C.PLAN_CHANGE_PROPOSALS, prop_id, proposal.model_dump())

        # 7. Exactly one immutable audit event.
        #
        #    `before_state` / `after_state` describe the DESTINATION DAY, not an
        #    assignment, because no assignment was read or written. Both sides
        #    carry the same day snapshot so a reader can see the day was untouched.
        #    `assignment_id` stays null — the internal destination token is an
        #    idempotency detail and must never masquerade as a semantic id.
        day_state = {
            "weekly_plan_id": current_plan_id,
            "destination_scheduled_day": scheduled_day,
            "current_assignment_ids_on_day": sorted(orders_before),
            "current_assignment_count_on_day": len(orders_before),
            "display_order_map_on_day": dict(sorted(orders_before.items())),
        }
        before_state = dict(day_state, plan_assignment_created=False)
        after_state = dict(
            day_state,
            plan_assignment_created=False,
            display_order_reserved=False,
            proposal_id=prop_id,
            proposal_type=ProposalType.ADD.value,
            proposal_status=ProposalStatus.PENDING_PARENT_ACCEPTANCE.value,
            proposal_version=1,
            proposed_activity_version_id=new_ver_id,
        )
        aud_id = audit_event_id(req_hash, key)
        audit = AuditEvent(
            id=aud_id, event_type=EVENT_TYPE, actor_uid=user.uid,
            actor_role=PrincipalRole.THERAPIST,
            subject_type="plan_change_proposal", subject_id=prop_id,
            therapist_id=therapist["id"], child_id=child_id,
            weekly_plan_id=current_plan_id,
            assignment_id=None,                 # Add creates/touches no assignment
            idempotency_key_hash=key_hash(key),
            before_state=before_state, after_state=after_state,
            request_id=request_id, occurred_at=_now(), created_at=_now(),
            environment=environment,
        )
        tx.set(C.AUDIT_EVENTS, aud_id, audit.model_dump())

        # 8. Post-condition: the destination day is byte-identical. An Add that
        #    altered, reordered or replaced an existing activity is a product-level
        #    failure, so assert it inside the transaction and roll back if broken.
        day_after = current_assignments_for_day(
            tx, child_id, current_plan_id, scheduled_day
        )
        if assignment_order_map(day_after) != orders_before:
            raise DuplicateAssignmentDisplayOrder(
                "Post-condition failed: creating an Add proposal must leave the "
                "destination day's current assignments and display_order values "
                "untouched."
            )

        result = {
            "proposal": {
                "proposal_id": prop_id,
                "proposal_type": ProposalType.ADD.value,
                "proposal_status": ProposalStatus.PENDING_PARENT_ACCEPTANCE.value,
                "child_id": child_id,
                "weekly_plan_id": current_plan_id,
                "destination_scheduled_day": scheduled_day,
                "proposed_activity_version_id": new_ver_id,
                "created_by_user_id": therapist["id"],
                "created_at": proposal.created_at,
                "version": 1,
                "resulting_assignment_id": None,
            },
            "proposed_activity_version": proposed.model_dump(),
            "child_summary": {
                "child_id": child_id,
                "plan_review_count": _plan_review_count(tx, child_id),
                "pending_proposal_count": _pending_proposal_count(tx, child_id),
            },
            "audit_event_id": aud_id,
            "idempotent_replay": False,
        }

        # 9. Idempotency record, stored atomically with everything above.
        record = IdempotencyRecord(
            id=rec_id, idempotency_key_hash=key_hash(key), actor_user_id=user.uid,
            action=ACTION, child_id=child_id,
            assignment_id=target_token,   # internal operation target, not an assignment id
            request_hash=req_hash, status="completed", result=result,
            audit_event_id=aud_id, created_at=_now(), environment=environment,
        )
        tx.set(C.IDEMPOTENCY_RECORDS, rec_id, record.model_dump())
        return result

    return repo.run_in_transaction(op)
