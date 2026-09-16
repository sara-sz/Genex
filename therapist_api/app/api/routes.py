"""Read-only therapist API routes (Phase 1A).

Every route is authenticated (no unauthenticated pathway) and delegates to the
ReadService, which enforces the access policy. Service exceptions are mapped to
HTTP status by exception handlers registered in the app factory.

Lists use a pagination-ready envelope: {items, total, next_cursor}.
"""

from __future__ import annotations

from typing import List, Optional, Union

from fastapi import APIRouter, Depends, Header, Request

from ..auth.interface import AuthenticatedUser
from ..constants import API_VERSION
from ..domain.enums import PrincipalRole
from ..services import access
from ..services import (
    acceptance_service,
    add_proposal_service,
    approval_service,
    decline_service,
    note_review_service,
    note_session_service,
    parent_note_service,
    private_note_service,
    proposal_service,
)
from . import schemas as S
from .deps import get_service, require_principal
from ..services.read_service import ReadService

router = APIRouter(prefix=f"/api/{API_VERSION}")


def _page(items: List) -> S.Page:
    return S.Page(items=[i.model_dump() for i in items], total=len(items), next_cursor=None)


@router.get("/me", response_model=S.MeResponse)
async def me(principal: AuthenticatedUser = Depends(require_principal),
             svc: ReadService = Depends(get_service)):
    return svc.get_me(principal)


@router.get("/children", response_model=S.Page)
async def children(principal: AuthenticatedUser = Depends(require_principal),
                   svc: ReadService = Depends(get_service)):
    return _page(svc.list_connected_children(principal))


@router.get("/children/{child_id}", response_model=S.ChildOverview)
async def child_overview(child_id: str,
                         principal: AuthenticatedUser = Depends(require_principal),
                         svc: ReadService = Depends(get_service)):
    return svc.get_child_overview(principal, child_id)


@router.get("/children/{child_id}/weekly-plan", response_model=S.WeeklyPlanResponse)
async def weekly_plan(child_id: str,
                      principal: AuthenticatedUser = Depends(require_principal),
                      svc: ReadService = Depends(get_service)):
    return svc.get_weekly_plan(principal, child_id)


@router.get("/children/{child_id}/progress", response_model=S.ProgressSummary)
async def progress(child_id: str,
                   principal: AuthenticatedUser = Depends(require_principal),
                   svc: ReadService = Depends(get_service)):
    return svc.get_progress(principal, child_id)


@router.get("/notes", response_model=S.Page)
async def notes(principal: AuthenticatedUser = Depends(require_principal),
                svc: ReadService = Depends(get_service)):
    return _page(svc.list_notes(principal))


@router.get(
    "/children/{child_id}/notes",
    response_model=Union[S.Page, S.ParentNoteHistoryResponse],
    responses={403: {"model": S.ErrorResponse}, 404: {"model": S.ErrorResponse}},
)
async def child_notes(child_id: str,
                      principal: AuthenticatedUser = Depends(require_principal),
                      svc: ReadService = Depends(get_service)):
    """Role-aware collaboration-note read for one child.

    A therapist receives the existing `Page` envelope unchanged — every parent
    note for the child, per the frozen therapist policy.

    An authorized parent receives `ParentNoteHistoryResponse`: only the items
    THAT parent submitted, in a dedicated allow-list projection. Another
    caregiver's submissions are never theirs to read, so the filter is on the
    note's stored author, not on child ownership.

    The two shapes are disjoint on required fields — only the parent envelope
    requires `child_id` — so neither response can validate as the other.

    Read-only for both roles: reading a NEW note never marks it REVIEWED.
    """
    if access.principal_role(principal) == PrincipalRole.PARENT:
        return svc.get_parent_own_notes(principal, child_id)
    return _page(svc.get_child_notes(principal, child_id))


# ── write: a parent submits a Question, Note or Update ──────────────────────
#
# POST shares the EXISTING notes path rather than adding /questions, /updates or
# a messages resource: the three types differ by parent intent, not by routing.
# OpenAPI therefore gains an operation, not a path.
@router.post(
    "/children/{child_id}/notes",
    response_model=S.ParentNoteCreateResponse,
    responses={
        400: {"model": S.ErrorResponse}, 403: {"model": S.ErrorResponse},
        404: {"model": S.ErrorResponse}, 409: {"model": S.ErrorResponse},
        422: {"model": S.ErrorResponse},
    },
)
async def create_parent_note(
    child_id: str,
    body: S.ParentNoteCreateRequest,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    principal: AuthenticatedUser = Depends(require_principal),
):
    """One-way parent -> therapist submission. Not a message; there is no reply.

    The therapist reads it through the existing GET on this same path; no second
    inbox route exists and no therapist write is implemented here.
    """
    repo = request.app.state.repo
    from ..logging_config import get_request_id

    return parent_note_service.create_parent_note(
        repo,
        principal,
        child_id=child_id,
        idempotency_key=idempotency_key,
        note_type=body.note_type,
        body=body.body,
        linked_assignment_id=body.linked_assignment_id,
        environment=principal.environment,
        request_id=get_request_id(),
    )


# ── action: therapist marks a parent-submitted note REVIEWED ────────────────
@router.post(
    "/children/{child_id}/notes/{note_id}/review",
    response_model=S.NoteReviewResponse,
    responses={
        400: {"model": S.ErrorResponse}, 401: {"model": S.ErrorResponse},
        403: {"model": S.ErrorResponse}, 404: {"model": S.ErrorResponse},
        409: {"model": S.ErrorResponse},
    },
)
async def review_parent_note(
    child_id: str,
    note_id: str,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    principal: AuthenticatedUser = Depends(require_principal),
):
    """Mark one parent-submitted item reviewed: review_status new -> reviewed.

    A POST action command, matching `/approve`, `/accept` and `/decline` — not a
    PATCH, because this API expresses lifecycle transitions as explicit named
    commands rather than field edits. Deliberately not `/reply`, `/respond`,
    `/answer`, `/resolve`, `/read` or `/seen`: none of those is what happened.

    Takes no request body. `session_preparation_status` is untouched — Discuss
    Next Session is a separate action that does not exist yet.
    """
    repo = request.app.state.repo
    from ..logging_config import get_request_id

    return note_review_service.mark_note_reviewed(
        repo,
        principal,
        child_id=child_id,
        note_id=note_id,
        idempotency_key=idempotency_key,
        environment=principal.environment,
        request_id=get_request_id(),
    )


# ── action: therapist marks a parent note DISCUSS NEXT SESSION ──────────────
@router.post(
    "/children/{child_id}/notes/{note_id}/discuss-next-session",
    response_model=S.NoteSessionPreparationResponse,
    responses={
        400: {"model": S.ErrorResponse}, 401: {"model": S.ErrorResponse},
        403: {"model": S.ErrorResponse}, 404: {"model": S.ErrorResponse},
        409: {"model": S.ErrorResponse},
    },
)
async def mark_note_for_next_session(
    child_id: str,
    note_id: str,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    principal: AuthenticatedUser = Depends(require_principal),
):
    """Surface one parent-submitted item for a future session.

    `session_preparation_status` none -> discuss_at_next_session. A POST action
    command like `/approve`, `/accept`, `/decline` and `/review` — not a PATCH,
    and deliberately not a generic note-update endpoint: this API expresses
    lifecycle transitions as explicit named commands rather than field edits.

    Takes no request body. `review_status` is untouched — Reviewed is a separate
    action, and marking for discussion never implies the therapist reviewed,
    replied, answered or resolved the item, nor that the parent was notified, a
    session was scheduled, or the discussion happened.

    One-way: there is no unflag, no clear, and no transition into `discussed`.
    """
    repo = request.app.state.repo
    from ..logging_config import get_request_id

    return note_session_service.mark_note_for_next_session(
        repo,
        principal,
        child_id=child_id,
        note_id=note_id,
        idempotency_key=idempotency_key,
        environment=principal.environment,
        request_id=get_request_id(),
    )


@router.get("/children/{child_id}/private-notes", response_model=S.Page)
async def private_notes(child_id: str,
                        principal: AuthenticatedUser = Depends(require_principal),
                        svc: ReadService = Depends(get_service)):
    return _page(svc.get_private_notes(principal, child_id))


# ── write: a therapist's OWN private note ───────────────────────────────────
#
# POST shares the EXISTING private-notes path rather than adding a new one: this
# is the same resource the therapist already reads. OpenAPI gains an operation,
# not a path.
@router.post(
    "/children/{child_id}/private-notes",
    response_model=S.PrivateNoteCreateResponse,
    responses={
        400: {"model": S.ErrorResponse}, 401: {"model": S.ErrorResponse},
        403: {"model": S.ErrorResponse}, 404: {"model": S.ErrorResponse},
        409: {"model": S.ErrorResponse}, 422: {"model": S.ErrorResponse},
    },
)
async def create_private_note(
    child_id: str,
    body: S.PrivateNoteCreateRequest,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    principal: AuthenticatedUser = Depends(require_principal),
):
    """A therapist's own clinical/workflow note. Visible only to its author.

    Not a parent message, chat, reply or response to a ParentNote: writing one
    notifies nobody and changes no ParentNote state. The author is the
    authenticated therapist and the child is the authorized path segment —
    neither can be supplied by the client.

    Append-only for the pilot: no edit, delete or withdraw, and
    `marked_for_next_session` is chosen here at creation with no later toggle.
    """
    repo = request.app.state.repo
    from ..logging_config import get_request_id

    return private_note_service.create_private_note(
        repo,
        principal,
        child_id=child_id,
        idempotency_key=idempotency_key,
        body=body.body,
        marked_for_next_session=body.marked_for_next_session,
        environment=principal.environment,
        request_id=get_request_id(),
    )


@router.get("/children/{child_id}/next-session", response_model=S.NextSessionResponse)
async def next_session(child_id: str,
                       principal: AuthenticatedUser = Depends(require_principal),
                       svc: ReadService = Depends(get_service)):
    return svc.get_next_session(principal, child_id)


@router.get("/children/{child_id}/connection", response_model=S.ConnectionDetails)
async def connection_details(child_id: str,
                             principal: AuthenticatedUser = Depends(require_principal),
                             svc: ReadService = Depends(get_service)):
    return svc.get_connection_details(principal, child_id)


@router.get("/activity-templates", response_model=S.Page)
async def activity_templates(principal: AuthenticatedUser = Depends(require_principal),
                             svc: ReadService = Depends(get_service)):
    return _page(svc.list_activity_templates(principal))


@router.get("/activity-templates/{activity_template_id}", response_model=S.ActivityTemplateView)
async def activity_template(activity_template_id: str,
                            principal: AuthenticatedUser = Depends(require_principal),
                            svc: ReadService = Depends(get_service)):
    return svc.get_activity_template(principal, activity_template_id)


@router.get("/milestones", response_model=S.Page)
async def milestones(principal: AuthenticatedUser = Depends(require_principal),
                     svc: ReadService = Depends(get_service)):
    return _page(svc.list_milestones(principal))


# ── write: approve one current, review-needed weekly-plan assignment ────────
@router.post(
    "/children/{child_id}/weekly-plan/assignments/{assignment_id}/approve",
    response_model=S.ApprovalResponse,
    responses={
        400: {"model": S.ErrorResponse}, 403: {"model": S.ErrorResponse},
        404: {"model": S.ErrorResponse}, 409: {"model": S.ErrorResponse},
    },
)
async def approve_assignment(
    child_id: str,
    assignment_id: str,
    body: S.ApproveAssignmentRequest,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    principal: AuthenticatedUser = Depends(require_principal),
):
    repo = request.app.state.repo
    from ..logging_config import get_request_id

    return approval_service.approve_assignment(
        repo,
        principal,
        child_id=child_id,
        assignment_id=assignment_id,
        idempotency_key=idempotency_key,
        expected_assignment_version=body.expected_assignment_version,
        environment=principal.environment,
        request_id=get_request_id(),
    )


# ── write: propose a modified version of a current, approved assignment ─────
@router.post(
    "/children/{child_id}/weekly-plan/assignments/{assignment_id}/proposals/modify",
    response_model=S.ProposalCreateResponse,
    responses={
        400: {"model": S.ErrorResponse}, 403: {"model": S.ErrorResponse},
        404: {"model": S.ErrorResponse}, 409: {"model": S.ErrorResponse},
        422: {"model": S.ErrorResponse},
    },
)
async def create_modify_proposal(
    child_id: str,
    assignment_id: str,
    body: S.ModifyProposalRequest,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    principal: AuthenticatedUser = Depends(require_principal),
):
    repo = request.app.state.repo
    from ..logging_config import get_request_id

    return proposal_service.create_modify_proposal(
        repo,
        principal,
        child_id=child_id,
        assignment_id=assignment_id,
        idempotency_key=idempotency_key,
        expected_assignment_version=body.expected_assignment_version,
        activity=body.activity.model_dump(),
        change_reason=body.change_reason,
        save_scope=body.save_scope,
        environment=principal.environment,
        request_id=get_request_id(),
    )


# ── write: propose an ADDITIONAL activity on one weekday ────────────────────
@router.post(
    "/children/{child_id}/weekly-plan/proposals/add",
    response_model=S.AddProposalCreateResponse,
    responses={
        400: {"model": S.ErrorResponse}, 403: {"model": S.ErrorResponse},
        404: {"model": S.ErrorResponse}, 409: {"model": S.ErrorResponse},
        422: {"model": S.ErrorResponse},
    },
)
async def create_add_proposal(
    child_id: str,
    body: S.AddProposalRequest,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    principal: AuthenticatedUser = Depends(require_principal),
):
    """Propose ONE additional activity on a weekday.

    The route hangs off the weekly plan rather than an assignment because Add
    targets a DAY: the destination need not be empty, and several pending Add
    proposals may name the same day. Nothing existing is modified and no
    PlanAssignment is created until a parent accepts.
    """
    repo = request.app.state.repo
    from ..logging_config import get_request_id

    return add_proposal_service.create_add_proposal(
        repo,
        principal,
        child_id=child_id,
        idempotency_key=idempotency_key,
        scheduled_day=body.scheduled_day,
        expected_weekly_plan_id=body.expected_weekly_plan_id,
        activity=body.activity.model_dump(),
        change_reason=body.change_reason,
        save_scope=body.save_scope,
        environment=principal.environment,
        request_id=get_request_id(),
    )


# ── write: parent accepts one pending modify proposal ───────────────────────
@router.post(
    "/children/{child_id}/proposals/{proposal_id}/accept",
    # Type-aware: a MODIFY accept reports the retired original and its
    # replacement; an ADD accept reports the single APPENDED assignment and has
    # neither. The two are disjoint on required fields, so neither response can
    # validate as the other.
    response_model=Union[S.AcceptProposalResponse, S.AddAcceptResponse],
    responses={
        400: {"model": S.ErrorResponse}, 403: {"model": S.ErrorResponse},
        404: {"model": S.ErrorResponse}, 409: {"model": S.ErrorResponse},
        422: {"model": S.ErrorResponse},
    },
)
async def accept_proposal(
    child_id: str,
    proposal_id: str,
    body: S.AcceptProposalRequest,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    principal: AuthenticatedUser = Depends(require_principal),
):
    repo = request.app.state.repo
    from ..logging_config import get_request_id

    return acceptance_service.accept_proposal(
        repo,
        principal,
        child_id=child_id,
        proposal_id=proposal_id,
        idempotency_key=idempotency_key,
        expected_proposal_version=body.expected_proposal_version,
        expected_assignment_version=body.expected_assignment_version,
        environment=principal.environment,
        request_id=get_request_id(),
    )


# ── write: parent declines one pending modify proposal ──────────────────────
@router.post(
    "/children/{child_id}/proposals/{proposal_id}/decline",
    # Type-aware: a MODIFY decline reports the preserved current assignment; an
    # ADD decline created nothing and reports its destination weekday instead.
    response_model=Union[S.DeclineProposalResponse, S.AddDeclineResponse],
    responses={
        400: {"model": S.ErrorResponse}, 403: {"model": S.ErrorResponse},
        404: {"model": S.ErrorResponse}, 409: {"model": S.ErrorResponse},
        422: {"model": S.ErrorResponse},
    },
)
async def decline_proposal(
    child_id: str,
    proposal_id: str,
    body: S.DeclineProposalRequest,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    principal: AuthenticatedUser = Depends(require_principal),
):
    repo = request.app.state.repo
    from ..logging_config import get_request_id

    return decline_service.decline_proposal(
        repo,
        principal,
        child_id=child_id,
        proposal_id=proposal_id,
        idempotency_key=idempotency_key,
        expected_proposal_version=body.expected_proposal_version,
        expected_assignment_version=body.expected_assignment_version,
        environment=principal.environment,
        request_id=get_request_id(),
    )


# ── read-only: proposals (authorization-safe) ───────────────────────────────
@router.get(
    "/children/{child_id}/proposals",
    response_model=Union[S.Page, S.ParentProposalListResponse],
    responses={403: {"model": S.ErrorResponse}, 404: {"model": S.ErrorResponse}},
)
async def list_proposals(child_id: str,
                         principal: AuthenticatedUser = Depends(require_principal),
                         svc: ReadService = Depends(get_service)):
    """Role-aware proposal list for one child.

    A therapist receives the existing `Page` envelope unchanged. An authorized
    parent receives `ParentProposalListResponse` — a lighter, discovery-only
    projection with no activity instructions and no optimistic-concurrency
    versions, so a client must open the detail endpoint before deciding.
    """
    if access.principal_role(principal) == PrincipalRole.PARENT:
        return svc.get_parent_proposal_list(principal, child_id)
    return _page(svc.list_proposals(principal, child_id))


@router.get(
    "/children/{child_id}/proposals/{proposal_id}",
    response_model=Union[
        S.ProposalView,
        S.ParentProposalDecisionDetail,
        S.ParentAddProposalDecisionDetail,
    ],
    responses={
        403: {"model": S.ErrorResponse}, 404: {"model": S.ErrorResponse},
        409: {"model": S.ErrorResponse},
    },
)
async def get_proposal(child_id: str, proposal_id: str,
                       principal: AuthenticatedUser = Depends(require_principal),
                       svc: ReadService = Depends(get_service)):
    """Role-aware, type-aware proposal detail.

    A therapist receives the existing `ProposalView` unchanged. An authorized
    parent receives a dedicated projection — never a filtered therapist model —
    chosen by proposal type:

    * MODIFY -> `ParentProposalDecisionDetail`, the frozen original-vs-proposed
      comparison carrying the two versions its accept/decline calls need;
    * ADD -> `ParentAddProposalDecisionDetail`, which has no original activity and
      instead shows the destination weekday plus what is already scheduled on it.

    OpenAPI documents all three via `anyOf`. They are disjoint on required fields
    — `original_activity`/`decision_context` versus `destination`/
    `existing_day_activities` — so no response can validate as the wrong member
    and be silently reshaped.
    """
    if access.principal_role(principal) == PrincipalRole.PARENT:
        return svc.get_parent_proposal_decision(principal, child_id, proposal_id)
    return svc.get_proposal(principal, child_id, proposal_id)
