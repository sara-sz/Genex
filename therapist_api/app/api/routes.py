"""Read-only therapist API routes (Phase 1A).

Every route is authenticated (no unauthenticated pathway) and delegates to the
ReadService, which enforces the access policy. Service exceptions are mapped to
HTTP status by exception handlers registered in the app factory.

Lists use a pagination-ready envelope: {items, total, next_cursor}.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends

from ..auth.interface import AuthenticatedUser
from ..constants import API_VERSION
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
