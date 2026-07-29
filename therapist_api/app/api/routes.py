"""Read-only therapist API routes (Phase 1A).

Every route is authenticated (no unauthenticated pathway) and delegates to the
ReadService, which enforces the access policy. Service exceptions are mapped to
HTTP status by exception handlers registered in the app factory.

Lists use a pagination-ready envelope: {items, total, next_cursor}.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Header, Request

from ..auth.interface import AuthenticatedUser
from ..constants import API_VERSION
from ..services import (
    acceptance_service,
    approval_service,
    decline_service,
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


@router.get("/children/{child_id}/notes", response_model=S.Page)
async def child_notes(child_id: str,
                      principal: AuthenticatedUser = Depends(require_principal),
                      svc: ReadService = Depends(get_service)):
    return _page(svc.get_child_notes(principal, child_id))


@router.get("/children/{child_id}/private-notes", response_model=S.Page)
async def private_notes(child_id: str,
                        principal: AuthenticatedUser = Depends(require_principal),
                        svc: ReadService = Depends(get_service)):
    return _page(svc.get_private_notes(principal, child_id))


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


# ── write: parent accepts one pending modify proposal ───────────────────────
@router.post(
    "/children/{child_id}/proposals/{proposal_id}/accept",
    response_model=S.AcceptProposalResponse,
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
    response_model=S.DeclineProposalResponse,
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
@router.get("/children/{child_id}/proposals", response_model=S.Page)
async def list_proposals(child_id: str,
                         principal: AuthenticatedUser = Depends(require_principal),
                         svc: ReadService = Depends(get_service)):
    return _page(svc.list_proposals(principal, child_id))


@router.get("/children/{child_id}/proposals/{proposal_id}", response_model=S.ProposalView)
async def get_proposal(child_id: str, proposal_id: str,
                       principal: AuthenticatedUser = Depends(require_principal),
                       svc: ReadService = Depends(get_service)):
    return svc.get_proposal(principal, child_id, proposal_id)
