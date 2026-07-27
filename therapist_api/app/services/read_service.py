"""Read service for the therapist slice.

All reads go through here (never fixture dicts, never route-level dict access).
The service enforces the access policy, then shapes canonical domain records
into the response DTOs consumed by the approved Therapist Dev frontend.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..api import schemas as S
from ..auth.interface import AuthenticatedUser
from ..domain.enums import (
    ConnectionStatus,
    ParentNoteReviewStatus,
    PlanApprovalStatus,
    ProposalStatus,
    SessionPreparationStatus,
)
from ..repository import collections as C
from ..repository.interface import CollaborationRepository
from . import access


class ReadService:
    def __init__(self, repo: CollaborationRepository) -> None:
        self.repo = repo

    # ── internal lookups ────────────────────────────────────────────────────
    def _version(self, version_id: str) -> Optional[dict]:
        rows = self.repo.query(C.ACTIVITY_VERSIONS, id=version_id)
        return rows[0] if rows else None

    def _child(self, child_id: str) -> Optional[dict]:
        rows = self.repo.query(C.CHILDREN, id=child_id)
        return rows[0] if rows else None

    def _parent(self, parent_id: str) -> Optional[dict]:
        rows = self.repo.query(C.PARENT_PROFILES, id=parent_id)
        return rows[0] if rows else None

    def _provenance(self, version: dict) -> S.ActivityProvenance:
        return S.ActivityProvenance(
            created_by_type=version["created_by_type"],
            created_by_user_id=version.get("created_by_user_id"),
            created_by_display_name=version.get("created_by_display_name"),
            original_activity_template_id=version.get("original_activity_template_id"),
            original_activity_version_id=version.get("original_activity_version_id"),
            modified_by_user_id=version.get("modified_by_user_id"),
            modified_by_display_name=version.get("modified_by_display_name"),
            save_scope=version["save_scope"],
            is_derived=version.get("is_derived", False),
        )

    # ── /me ─────────────────────────────────────────────────────────────────
    def get_me(self, user: AuthenticatedUser) -> S.MeResponse:
        therapist = access.resolve_therapist(self.repo, user)  # raises AccessDenied
        return S.MeResponse(
            principal_id=therapist["id"],
            uid=therapist["uid"],
            role="therapist",
            display_name=therapist["display_name"],
            therapist_id=therapist["id"],
            credentials=therapist.get("credentials"),
            organization=therapist.get("organization"),
            contact_email=therapist.get("contact_email"),
            preferences=therapist.get("preferences", {}),
        )

    # ── connected children ──────────────────────────────────────────────────
    def _counts_for_child(self, child_id: str) -> Dict[str, int]:
        assignments = self.repo.query(C.PLAN_ASSIGNMENTS, child_id=child_id)
        plan_review = sum(
            1 for a in assignments
            if a["plan_approval_status"] == PlanApprovalStatus.NEEDS_PLAN_REVIEW.value
        )
        notes = self.repo.query(C.PARENT_NOTES, child_id=child_id)
        new_notes = sum(
            1 for n in notes if n["review_status"] == ParentNoteReviewStatus.NEW.value
        )
        proposals = self.repo.query(C.PLAN_CHANGE_PROPOSALS, child_id=child_id)
        pending = sum(
            1 for p in proposals
            if p["status"] == ProposalStatus.PENDING_PARENT_ACCEPTANCE.value
        )
        return {"plan_review": plan_review, "new_notes": new_notes, "pending": pending}

    def list_connected_children(self, user: AuthenticatedUser) -> List[S.ConnectedChildSummary]:
        therapist = access.resolve_therapist(self.repo, user)
        conns = self.repo.query(C.CONNECTIONS, therapist_id=therapist["id"])
        out: List[S.ConnectedChildSummary] = []
        for conn in sorted(conns, key=lambda c: c["child_id"]):
            status = ConnectionStatus(conn["status"])
            if status == ConnectionStatus.ENDED:
                continue  # ended connections are not surfaced
            child = self._child(conn["child_id"]) or {}
            parent = self._parent(conn["parent_id"]) or {}
            is_active = status == ConnectionStatus.ACTIVE
            counts = self._counts_for_child(conn["child_id"]) if is_active else {
                "plan_review": 0, "new_notes": 0, "pending": 0
            }
            out.append(S.ConnectedChildSummary(
                child_id=conn["child_id"],
                display_name=child.get("display_name", ""),
                parent_display_name=parent.get("display_name", ""),
                connection_status=status.value,
                active_practice_domains=child.get("active_practice_domains", []) if is_active else [],
                plan_review_count=counts["plan_review"],
                new_parent_note_count=counts["new_notes"],
                pending_proposal_count=counts["pending"],
            ))
        return out

    # ── child overview (FULL) ───────────────────────────────────────────────
    def get_child_overview(self, user: AuthenticatedUser, child_id: str) -> S.ChildOverview:
        therapist = access.resolve_therapist(self.repo, user)
        conn = access.require_full_access(self.repo, therapist["id"], child_id)  # 404 if not full
        child = self._child(child_id) or {}
        parent = self._parent(conn["parent_id"]) or {}
        recent = [
            {"assignment_id": a["id"], "activity_version_id": a["activity_version_id"],
             "practice_status": a["practice_status"], "scheduled_day": a["scheduled_day"]}
            for a in self.repo.query(C.PLAN_ASSIGNMENTS, child_id=child_id)
        ]
        next_items = self._next_session_items(child_id, therapist["id"])
        counts = self._counts_for_child(child_id)
        return S.ChildOverview(
            child_id=child_id,
            display_name=child.get("display_name", ""),
            family_context=child.get("family_context", ""),
            active_practice_domains=child.get("active_practice_domains", []),
            home_practice_availability=child.get("home_practice_availability", ""),
            interests_and_motivators=child.get("interests_and_motivators", []),
            recent_home_practice=recent,
            next_session_items=next_items,
            connection_summary={
                "connection_status": conn["status"],
                "parent_display_name": parent.get("display_name", ""),
                "restricted": False,
            },
            plan_review_count=counts["plan_review"],
            pending_proposal_count=counts["pending"],
        )

    # ── weekly plan (FULL) ──────────────────────────────────────────────────
    def get_weekly_plan(self, user: AuthenticatedUser, child_id: str) -> S.WeeklyPlanResponse:
        therapist = access.resolve_therapist(self.repo, user)
        access.require_full_access(self.repo, therapist["id"], child_id)
        plans = self.repo.query(C.WEEKLY_PLANS, child_id=child_id)
        plan = plans[0] if plans else {"id": "", "week_start_date": ""}
        assignments = sorted(
            self.repo.query(C.PLAN_ASSIGNMENTS, child_id=child_id),
            key=lambda a: (a["scheduled_day"], a["id"]),
        )
        views: List[S.PlanAssignmentView] = []
        for a in assignments:
            version = self._version(a["activity_version_id"]) or {}
            views.append(S.PlanAssignmentView(
                assignment_id=a["id"],
                scheduled_day=a["scheduled_day"],
                plan_approval_status=a["plan_approval_status"],
                practice_status=a["practice_status"],
                assignment_status=a["assignment_status"],
                activity_template_id=a["activity_template_id"],
                activity_version_id=a["activity_version_id"],
                activity_title=version.get("title", ""),
                provenance=self._provenance(version) if version else S.ActivityProvenance(
                    created_by_type="genex", save_scope="child_only", is_derived=False),
                parent_feedback_summary=a.get("parent_feedback_summary", ""),
                pending_proposal_id=a.get("pending_proposal_id"),
                pending_proposal=self._pending_proposal_summary(a.get("pending_proposal_id")),
                version=a.get("version", 1),
                updated_at=a.get("updated_at", ""),
            ))
        return S.WeeklyPlanResponse(
            child_id=child_id, weekly_plan_id=plan.get("id", ""),
            week_start_date=plan.get("week_start_date", ""), assignments=views,
        )

    # ── progress (FULL) ─────────────────────────────────────────────────────
    def get_progress(self, user: AuthenticatedUser, child_id: str) -> S.ProgressSummary:
        therapist = access.resolve_therapist(self.repo, user)
        access.require_full_access(self.repo, therapist["id"], child_id)
        child = self._child(child_id) or {}
        assignments = self.repo.query(C.PLAN_ASSIGNMENTS, child_id=child_id)
        counts: Dict[str, int] = {}
        recent = []
        for a in assignments:
            counts[a["practice_status"]] = counts.get(a["practice_status"], 0) + 1
            recent.append({"assignment_id": a["id"], "practice_status": a["practice_status"]})
        return S.ProgressSummary(
            child_id=child_id,
            active_practice_domains=child.get("active_practice_domains", []),
            practice_status_counts=counts,
            recent_home_practice=recent,
        )

    # ── parent notes ────────────────────────────────────────────────────────
    def _parent_note_view(self, n: dict) -> S.ParentNoteView:
        return S.ParentNoteView(
            note_id=n["id"], child_id=n["child_id"], note_type=n["note_type"],
            review_status=n["review_status"],
            session_preparation_status=n["session_preparation_status"],
            linked_assignment_id=n.get("linked_assignment_id"),
            linked_activity_title=n.get("linked_activity_title"),
            body=n["body"], created_at=n.get("created_at", ""),
        )

    def list_notes(self, user: AuthenticatedUser) -> List[S.ParentNoteView]:
        """Inbox: parent notes aggregated across the therapist's ACTIVE children."""
        therapist = access.resolve_therapist(self.repo, user)
        conns = self.repo.query(C.CONNECTIONS, therapist_id=therapist["id"], status=ConnectionStatus.ACTIVE.value)
        active_children = {c["child_id"] for c in conns}
        notes = [n for n in self.repo.query(C.PARENT_NOTES) if n["child_id"] in active_children]
        notes.sort(key=lambda n: (n.get("created_at", ""), n["id"]), reverse=True)
        return [self._parent_note_view(n) for n in notes]

    def get_child_notes(self, user: AuthenticatedUser, child_id: str) -> List[S.ParentNoteView]:
        therapist = access.resolve_therapist(self.repo, user)
        access.require_full_access(self.repo, therapist["id"], child_id)
        notes = self.repo.query(C.PARENT_NOTES, child_id=child_id)
        notes.sort(key=lambda n: (n.get("created_at", ""), n["id"]), reverse=True)
        return [self._parent_note_view(n) for n in notes]

    # ── private notes (FULL + own only) ─────────────────────────────────────
    def get_private_notes(self, user: AuthenticatedUser, child_id: str) -> List[S.PrivateNoteView]:
        therapist = access.resolve_therapist(self.repo, user)  # therapist role required
        access.require_full_access(self.repo, therapist["id"], child_id)
        # Only the authoring therapist's own private notes.
        rows = self.repo.query(C.PRIVATE_THERAPIST_NOTES, child_id=child_id, therapist_id=therapist["id"])
        rows.sort(key=lambda n: (n.get("created_at", ""), n["id"]), reverse=True)
        return [S.PrivateNoteView(
            note_id=n["id"], child_id=n["child_id"], body=n["body"],
            marked_for_next_session=n.get("marked_for_next_session", False),
            created_at=n.get("created_at", ""),
        ) for n in rows]

    # ── next session (FULL) ─────────────────────────────────────────────────
    def _next_session_items(self, child_id: str, therapist_id: str) -> List[dict]:
        pn = [n for n in self.repo.query(C.PARENT_NOTES, child_id=child_id)
              if n["session_preparation_status"] == SessionPreparationStatus.DISCUSS_AT_NEXT_SESSION.value]
        ptn = [n for n in self.repo.query(C.PRIVATE_THERAPIST_NOTES, child_id=child_id, therapist_id=therapist_id)
               if n.get("marked_for_next_session")]
        items = [{"source": "parent_note", "id": n["id"], "body": n["body"]} for n in pn]
        items += [{"source": "private_note", "id": n["id"], "body": n["body"]} for n in ptn]
        return items

    def get_next_session(self, user: AuthenticatedUser, child_id: str) -> S.NextSessionResponse:
        therapist = access.resolve_therapist(self.repo, user)
        access.require_full_access(self.repo, therapist["id"], child_id)
        pn = [self._parent_note_view(n) for n in self.repo.query(C.PARENT_NOTES, child_id=child_id)
              if n["session_preparation_status"] == SessionPreparationStatus.DISCUSS_AT_NEXT_SESSION.value]
        ptn = [S.PrivateNoteView(
            note_id=n["id"], child_id=n["child_id"], body=n["body"],
            marked_for_next_session=n.get("marked_for_next_session", False),
            created_at=n.get("created_at", ""))
            for n in self.repo.query(C.PRIVATE_THERAPIST_NOTES, child_id=child_id, therapist_id=therapist["id"])
            if n.get("marked_for_next_session")]
        return S.NextSessionResponse(child_id=child_id, parent_note_items=pn, private_note_items=ptn)

    # ── connection details (any live connection incl restricted) ────────────
    def get_connection_details(self, user: AuthenticatedUser, child_id: str) -> S.ConnectionDetails:
        therapist = access.resolve_therapist(self.repo, user)
        level, conn = access.resolve_child_access(self.repo, therapist["id"], child_id)
        from ..domain.enums import ChildAccessLevel
        if conn is None or level == ChildAccessLevel.NONE:
            raise access.ChildNotFound(child_id)
        child = self._child(child_id) or {}
        parent = self._parent(conn["parent_id"]) or {}
        return S.ConnectionDetails(
            child_id=child_id,
            display_name=child.get("display_name", ""),
            parent_display_name=parent.get("display_name", ""),
            connection_status=conn["status"],
            restricted=(level == ChildAccessLevel.RESTRICTED),
            activation_reminder_simulated=conn.get("activation_reminder_simulated", False),
            invited_at=conn.get("invited_at", ""),
            activated_at=conn.get("activated_at"),
        )

    # ── catalog: activity templates / versions / milestones ─────────────────
    def list_activity_templates(self, user: AuthenticatedUser) -> List[S.ActivityTemplateView]:
        access.resolve_therapist(self.repo, user)  # therapist-only
        out = []
        for t in sorted(self.repo.query(C.ACTIVITY_TEMPLATES), key=lambda t: t["id"]):
            out.append(self._template_view(t))
        return out

    def get_activity_template(self, user: AuthenticatedUser, template_id: str) -> S.ActivityTemplateView:
        access.resolve_therapist(self.repo, user)
        rows = self.repo.query(C.ACTIVITY_TEMPLATES, id=template_id)
        if not rows:
            raise access.ChildNotFound(template_id)  # existence-blind 404 for unknown ids
        return self._template_view(rows[0])

    def _template_view(self, t: dict) -> S.ActivityTemplateView:
        versions = [
            {"activity_version_id": v["id"], "title": v["title"], "is_derived": v.get("is_derived", False),
             "created_by_type": v["created_by_type"], "original_activity_version_id": v.get("original_activity_version_id")}
            for v in self.repo.query(C.ACTIVITY_VERSIONS, activity_template_id=t["id"])
        ]
        return S.ActivityTemplateView(
            activity_template_id=t["id"], title=t["title"], domain=t["domain"],
            milestone_ids=t.get("milestone_ids", []), instructions=t.get("instructions", ""),
            materials=t.get("materials", ""), created_by_type=t["created_by_type"],
            immutable=t.get("immutable", True), versions=versions,
        )

    # ── proposals (read-only, full access) ──────────────────────────────────
    def _pending_proposal_summary(self, proposal_id) -> Optional[dict]:
        if not proposal_id:
            return None
        rows = self.repo.query(C.PLAN_CHANGE_PROPOSALS, id=proposal_id)
        if not rows:
            return None
        p = rows[0]
        return {
            "proposal_id": p["id"], "proposal_type": p["proposal_type"],
            "proposal_status": p["status"], "proposed_activity_version_id": p.get("proposed_activity_version_id"),
        }

    def _proposal_view(self, p: dict) -> S.ProposalView:
        return S.ProposalView(
            proposal_id=p["id"], proposal_type=p["proposal_type"], proposal_status=p["status"],
            child_id=p["child_id"], weekly_plan_id=p.get("weekly_plan_id"),
            current_assignment_id=p.get("target_assignment_id"),
            original_activity_template_id=p.get("original_activity_template_id"),
            original_activity_version_id=p.get("original_activity_version_id"),
            proposed_activity_version_id=p.get("proposed_activity_version_id"),
            change_reason=p.get("change_reason", ""), save_scope=p.get("save_scope", ""),
            created_by_user_id=p.get("created_by_user_id"), created_at=p.get("created_at", ""),
            version=p.get("version", 1),
        )

    def list_proposals(self, user: AuthenticatedUser, child_id: str) -> List[S.ProposalView]:
        therapist = access.resolve_therapist(self.repo, user)
        access.require_full_access(self.repo, therapist["id"], child_id)
        rows = self.repo.query(C.PLAN_CHANGE_PROPOSALS, child_id=child_id)
        rows.sort(key=lambda p: (p.get("created_at", ""), p["id"]), reverse=True)
        return [self._proposal_view(p) for p in rows]

    def get_proposal(self, user: AuthenticatedUser, child_id: str, proposal_id: str) -> S.ProposalView:
        therapist = access.resolve_therapist(self.repo, user)
        access.require_full_access(self.repo, therapist["id"], child_id)
        rows = self.repo.query(C.PLAN_CHANGE_PROPOSALS, id=proposal_id)
        if not rows or rows[0]["child_id"] != child_id:
            raise access.ChildNotFound(proposal_id)  # existence-blind
        return self._proposal_view(rows[0])

    def list_milestones(self, user: AuthenticatedUser) -> List[S.MilestoneView]:
        access.resolve_therapist(self.repo, user)
        return [S.MilestoneView(
            milestone_id=m["id"], domain=m["domain"], title=m["title"],
            description=m.get("description", ""),
            source_age_band_months=m.get("source_age_band_months"),
            age_gated=False,
        ) for m in sorted(self.repo.query(C.MILESTONES), key=lambda m: m["id"])]
