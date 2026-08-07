"""Read service for the therapist slice.

All reads go through here (never fixture dicts, never route-level dict access).
The service enforces the access policy, then shapes canonical domain records
into the response DTOs consumed by the approved Therapist Dev frontend.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..api import schemas as S
from ..auth.interface import AuthenticatedUser
from ..domain.audit_state import plain as _plain
from ..domain.enums import (
    ActivitySaveScope,
    AssignmentStatus,
    ChildAccessLevel,
    ConnectionStatus,
    ParentNoteReviewStatus,
    PlanApprovalStatus,
    ProposalStatus,
    ProposalType,
    SessionPreparationStatus,
)
from ..domain.weekdays import day_label
from ..repository import collections as C
from ..repository.interface import CollaborationRepository
from . import access, assignment_order, eligibility


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
            and _plain(a["assignment_status"]) == AssignmentStatus.CURRENT.value
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
        # Only CURRENT assignments are plan items. An assignment retired by an
        # accepted proposal stays stored for history but must never resurface
        # here as a second active activity alongside its replacement.
        assignments = sorted(
            (a for a in self.repo.query(C.PLAN_ASSIGNMENTS, child_id=child_id)
             if _plain(a["assignment_status"]) == AssignmentStatus.CURRENT.value),
            key=lambda a: (a["scheduled_day"], a.get("display_order", 0), a["id"]),
        )
        views: List[S.PlanAssignmentView] = []
        for a in assignments:
            version = self._version(a["activity_version_id"]) or {}
            views.append(S.PlanAssignmentView(
                assignment_id=a["id"],
                scheduled_day=a["scheduled_day"],
                display_order=int(a.get("display_order", 0)),
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
    def _derived_version_child_id(self, version_id: str) -> Optional[str]:
        """Child a derived version belongs to, via its proposal or assignment.

        `ActivityVersion` carries no `child_id`, so the association is resolved
        through the canonical relationships that do: the PlanChangeProposal that
        proposed it, else a PlanAssignment already pointing at it (the shape a
        future accepted proposal takes). Returns None when neither exists.
        """
        proposals = self.repo.query(C.PLAN_CHANGE_PROPOSALS, proposed_activity_version_id=version_id)
        if proposals:
            return proposals[0]["child_id"]
        assignments = self.repo.query(C.PLAN_ASSIGNMENTS, activity_version_id=version_id)
        if assignments:
            return assignments[0]["child_id"]
        return None

    def _derived_version_visible(self, version: dict, therapist_id: str) -> bool:
        """Whether a DERIVED version may appear in the generic template catalog.

        Fail-closed: ownership is necessary for every scope, and `child_only`
        additionally requires a live (active) connection to the associated child.
        Sharing a canonical `activity_template_id` never grants visibility.
        """
        if version.get("created_by_user_id") != therapist_id:
            return False
        scope = version.get("save_scope")
        scope = scope.value if hasattr(scope, "value") else scope
        if scope in (ActivitySaveScope.THERAPIST_LIBRARY.value,
                     ActivitySaveScope.SUBMITTED_FOR_GENEX_REVIEW.value):
            # Owner-scoped. `submitted_for_genex_review` is submission metadata
            # only — there is no Genex-review role or publication path yet, so it
            # stays private to the submitter and is never globally published.
            return True
        if scope == ActivitySaveScope.CHILD_ONLY.value:
            child_id = self._derived_version_child_id(version["id"])
            if not child_id:
                return False  # no resolvable child -> hide
            level, _ = access.resolve_child_access(self.repo, therapist_id, child_id)
            return level == ChildAccessLevel.FULL  # pending/paused/ended/none -> hidden
        return False  # unknown scope -> fail closed

    def _visible_versions(self, template_id: str, therapist_id: str) -> List[dict]:
        """Canonical (non-derived) versions, plus derived ones this therapist may see."""
        return [
            v for v in self.repo.query(C.ACTIVITY_VERSIONS, activity_template_id=template_id)
            if not v.get("is_derived", False) or self._derived_version_visible(v, therapist_id)
        ]

    def list_activity_templates(self, user: AuthenticatedUser) -> List[S.ActivityTemplateView]:
        therapist = access.resolve_therapist(self.repo, user)  # therapist-only
        out = []
        for t in sorted(self.repo.query(C.ACTIVITY_TEMPLATES), key=lambda t: t["id"]):
            out.append(self._template_view(t, therapist["id"]))
        return out

    def get_activity_template(self, user: AuthenticatedUser, template_id: str) -> S.ActivityTemplateView:
        therapist = access.resolve_therapist(self.repo, user)
        rows = self.repo.query(C.ACTIVITY_TEMPLATES, id=template_id)
        if not rows:
            raise access.ChildNotFound(template_id)  # existence-blind 404 for unknown ids
        return self._template_view(rows[0], therapist["id"])

    def _template_view(self, t: dict, therapist_id: str) -> S.ActivityTemplateView:
        # Filtering happens here in the service layer (not in the response DTO), so
        # a hidden version is never loaded into any view a caller could reach.
        versions = [
            {"activity_version_id": v["id"], "title": v["title"], "is_derived": v.get("is_derived", False),
             "created_by_type": v["created_by_type"], "original_activity_version_id": v.get("original_activity_version_id")}
            for v in self._visible_versions(t["id"], therapist_id)
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
            # ADD only; null for MODIFY, so the frozen Modify view is unchanged.
            destination_scheduled_day=p.get("destination_scheduled_day"),
            original_activity_template_id=p.get("original_activity_template_id"),
            original_activity_version_id=p.get("original_activity_version_id"),
            proposed_activity_version_id=p.get("proposed_activity_version_id"),
            change_reason=p.get("change_reason", ""), save_scope=p.get("save_scope", ""),
            created_by_user_id=p.get("created_by_user_id"), created_at=p.get("created_at", ""),
            decided_by_user_id=p.get("decided_by_user_id"),
            decided_by_role=_plain(p.get("decided_by_role")),
            decided_at=p.get("decided_at"),
            resulting_assignment_id=p.get("resulting_assignment_id"),
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

    # ── parent-safe proposal list (discovery only) ──────────────────────────
    def _parent_therapist_name(self, therapist_id) -> str:
        """Presentable name + credentials. Never the id, email or organization."""
        rows = self.repo.query(C.THERAPIST_PROFILES, id=therapist_id) if therapist_id else []
        if not rows:
            return ""
        name = rows[0].get("display_name", "")
        credentials = rows[0].get("credentials")
        return f"{name}, {credentials}" if credentials else name

    def _parent_activity_summary(self, version: dict) -> S.ParentProposalActivitySummary:
        """Teaser only — no instructions; those live in the detail endpoint."""
        milestone_id = version.get("milestone_id")
        rows = self.repo.query(C.MILESTONES, id=milestone_id) if milestone_id else []
        return S.ParentProposalActivitySummary(
            title=version.get("title", ""),
            developmental_domain=version.get("domain", ""),
            milestone_display_name=rows[0]["title"] if rows else "",
        )

    def get_parent_proposal_list(
        self, user: AuthenticatedUser, child_id: str
    ) -> S.ParentProposalListResponse:
        """Parent-safe proposal list for ONE of the parent's own children.

        Read-only, existence-blind, and single-child by design — there is no
        cross-child parent inbox.

        Items whose linked records are missing or belong to another child are
        DROPPED rather than partially rendered, and `total` counts only the safe
        items. Nothing in the response says how many were dropped or why.

        Actionability comes from the shared `eligibility` evaluator, so the list
        advertises only decisions that would actually succeed — a pending proposal
        blocked by a write guard is shown with all flags false rather than
        offering the family a button that would 409.
        """
        parent = access.resolve_parent(self.repo, user)                # 403 if not a parent
        access.require_parent_child_access(self.repo, parent["id"], child_id)  # 404-blind

        child = self._child(child_id) or {}
        child_summary = S.ParentChildSummary(
            child_id=child_id, display_name=child.get("display_name", "")
        )

        decorated = []
        for proposal in self.repo.query(C.PLAN_CHANGE_PROPOSALS, child_id=child_id):
            if not eligibility.proposal_is_safe_to_show(self.repo, child_id, proposal):
                continue                                              # drop, silently
            proposed = self._version(proposal.get("proposed_activity_version_id"))
            if proposed is None:                                      # belt and braces
                continue
            flags = eligibility.evaluate_parent_decision(self.repo, child_id, proposal)
            decorated.append((eligibility.sort_key(proposal, flags), proposal, proposed, flags))

        decorated.sort(key=lambda entry: entry[0])

        items = [
            S.ParentProposalListItem(
                proposal_id=proposal["id"],
                proposal_type=_plain(proposal.get("proposal_type")),
                proposal_status=_plain(proposal.get("status")),
                created_at=proposal.get("created_at", ""),
                decided_at=proposal.get("decided_at"),
                child=child_summary,
                therapist=S.ParentTherapistSummary(
                    display_name=self._parent_therapist_name(proposal.get("therapist_id"))
                ),
                proposed_activity=self._parent_activity_summary(proposed),
                change_reason=proposal.get("change_reason", "") or proposal.get("rationale", ""),
                decision=S.ParentProposalDecisionSummary(
                    needs_parent_attention=flags.needs_parent_attention,
                    can_accept=flags.can_accept,
                    can_decline=flags.can_decline,
                ),
                # ADD only. Null for MODIFY, so the frozen Modify item is
                # unchanged in value. The day's existing activities are NOT
                # included here — that context belongs in the detail response, so
                # the list stays a lightweight discovery surface.
                destination=self._parent_destination(proposal),
            )
            for _, proposal, proposed, flags in decorated
        ]
        return S.ParentProposalListResponse(items=items, total=len(items), next_cursor=None)

    # ── parent-safe ADD helpers ─────────────────────────────────────────────
    def _parent_destination(self, proposal: dict) -> Optional[S.ParentDestinationDay]:
        """Destination weekday for an ADD, or None for any other proposal type."""
        if _plain(proposal.get("proposal_type")) != ProposalType.ADD.value:
            return None
        day = proposal.get("destination_scheduled_day")
        label = day_label(day)
        if label is None:
            return None                 # caller already dropped it; belt and braces
        return S.ParentDestinationDay(scheduled_day=day, day_label=label)

    def _parent_day_activities(
        self, child_id: str, weekly_plan_id: str, scheduled_day: int
    ) -> List[S.ParentDayActivitySummary]:
        """Parent-safe teasers for the CURRENT activities on one weekday.

        Read at request time from canonical state — never a snapshot stored on the
        proposal. If the therapist or an acceptance changes the day after the
        proposal was created, this reflects the change; the proposal record does
        not, because it represents the recommendation rather than the plan.

        Ordered by `display_order` ascending with the assignment id as the final
        deterministic tie-break. The ORDER is the product signal: `display_order`
        itself is never exposed, and neither is the assignment id, plan id,
        version id, assignment status, approval state or pending link.
        Retired/replaced assignments are excluded — `current_assignments_for_day`
        returns CURRENT rows only.
        """
        day = assignment_order.current_assignments_for_day(
            self.repo, child_id, weekly_plan_id, scheduled_day
        )
        day.sort(key=lambda a: (int(a.get("display_order", 0)), a["id"]))
        summaries = []
        for a in day:
            version = self._version(a.get("activity_version_id"))
            if version is None:
                continue                # a dangling row is omitted, never guessed at
            milestone_id = version.get("milestone_id")
            rows = self.repo.query(C.MILESTONES, id=milestone_id) if milestone_id else []
            summaries.append(S.ParentDayActivitySummary(
                title=version.get("title", ""),
                developmental_domain=version.get("domain", ""),
                milestone_display_name=rows[0]["title"] if rows else "",
                duration_minutes=version.get("duration_minutes"),
            ))
        return summaries

    # ── parent-safe proposal decision detail ────────────────────────────────
    def _parent_activity_view(self, version: dict) -> S.ParentActivityView:
        """Parent-facing activity content only.

        Deliberately omits every provenance, ownership and visibility field that
        exists on `ActivityVersion` — `save_scope`, `is_derived`, `created_by_*`,
        `modified_by_*`, `original_activity_*`, `immutable`, `environment` — so a
        parent response cannot leak how the activity was authored or who may see
        it. Only what a parent needs to judge the change is included.
        """
        milestone_id = version.get("milestone_id")
        milestone_rows = (
            self.repo.query(C.MILESTONES, id=milestone_id) if milestone_id else []
        )
        return S.ParentActivityView(
            title=version.get("title", ""),
            developmental_domain=version.get("domain", ""),
            milestone_id=milestone_id,
            milestone_display_name=milestone_rows[0]["title"] if milestone_rows else "",
            skill_focus=version.get("skill_focus", ""),
            duration_minutes=version.get("duration_minutes"),
            difficulty=version.get("difficulty", ""),
            materials=list(version.get("materials", [])),
            materials_type=version.get("materials_type", ""),
            setup=version.get("setup", ""),
            parent_instructions=list(version.get("parent_instructions", [])),
            what_to_say=list(version.get("what_to_say", [])),
            how_to_help=list(version.get("how_to_help", [])),
            success_signals=list(version.get("success_signals", [])),
            variations=list(version.get("variations", [])),
            routine_tags=list(version.get("routine_tags", [])),
            theme_tags=list(version.get("theme_tags", [])),
            safety_risk_flags=list(version.get("safety_risk_flags", [])),
        )

    def get_parent_proposal_decision(
        self, user: AuthenticatedUser, child_id: str, proposal_id: str
    ) -> S.ParentProposalDecisionDetail:
        """Parent-safe decision detail for ONE proposal about the parent's child.

        Read-only: nothing is written to any collection. Authorization is
        existence-blind — an unknown child, another family's child or proposal,
        and a non-active connection all raise the same `ChildNotFound` (404).

        Authorization to the child + proposal grants sight of exactly the TWO
        activity versions that proposal references. It is not a catalog read: the
        generic activity-template visibility rules are untouched, so
        `child_only` / `therapist_library` / `submitted_for_genex_review`
        versions gain no broader exposure from this endpoint.
        """
        parent = access.resolve_parent(self.repo, user)              # 403 if not a parent
        access.require_parent_child_access(self.repo, parent["id"], child_id)  # 404-blind

        rows = self.repo.query(C.PLAN_CHANGE_PROPOSALS, id=proposal_id)
        if not rows or rows[0]["child_id"] != child_id:
            raise access.ChildNotFound(proposal_id)                  # existence-blind
        proposal = rows[0]

        # Type-aware dispatch. An ADD gets its own projection rather than being
        # forced into the original-vs-proposed comparison below, which would have
        # to fabricate an "original activity" that does not exist. Any type
        # without a parent-safe projection stays existence-blind.
        proposal_type = _plain(proposal.get("proposal_type"))
        if proposal_type == ProposalType.ADD.value:
            return self._parent_add_detail(child_id, proposal)
        if proposal_type != ProposalType.MODIFY.value:
            raise access.ChildNotFound(proposal_id)   # REPLACE / REMOVE / unknown

        # The referenced assignment must belong to the same child; a proposal
        # pointing elsewhere must not reveal that the other assignment exists.
        assignment_id = proposal.get("target_assignment_id")
        assignments = (
            self.repo.query(C.PLAN_ASSIGNMENTS, id=assignment_id) if assignment_id else []
        )
        if not assignments or assignments[0]["child_id"] != child_id:
            raise access.ChildNotFound(proposal_id)
        assignment = assignments[0]

        # Original version: the proposal's recorded original where present,
        # otherwise whatever the assignment currently carries.
        original_version_id = (
            proposal.get("original_activity_version_id") or assignment["activity_version_id"]
        )
        proposed_version_id = proposal.get("proposed_activity_version_id")
        original = self._version(original_version_id)
        proposed = self._version(proposed_version_id) if proposed_version_id else None
        if original is None or proposed is None:
            # A dangling reference is an internal inconsistency, not a hint about
            # what exists — stay existence-blind.
            raise access.ChildNotFound(proposal_id)

        child = self._child(child_id) or {}
        therapist_rows = self.repo.query(C.THERAPIST_PROFILES, id=proposal.get("therapist_id"))
        therapist = therapist_rows[0] if therapist_rows else {}
        therapist_name = therapist.get("display_name", "")
        if therapist.get("credentials"):
            therapist_name = f"{therapist_name}, {therapist['credentials']}"

        status = _plain(proposal["status"])
        decided_at = proposal.get("decided_at")
        # Aligned with the list: actionability comes from the SHARED evaluator, not
        # from `status == pending` alone. A pending proposal blocked by a write
        # guard therefore reports false/false here too, so the detail screen never
        # offers an action that would 409 on submission.
        flags = eligibility.evaluate_parent_decision(self.repo, child_id, proposal)

        return S.ParentProposalDecisionDetail(
            proposal=S.ParentProposalSummary(
                proposal_id=proposal["id"],
                proposal_type=_plain(proposal["proposal_type"]),
                proposal_status=status,
                proposal_version=int(proposal.get("version", 1)),
                created_at=proposal.get("created_at", ""),
                decided_at=decided_at,
            ),
            child=S.ParentChildSummary(
                child_id=child_id, display_name=child.get("display_name", "")
            ),
            therapist=S.ParentTherapistSummary(display_name=therapist_name),
            decision_context=S.ParentDecisionContext(
                change_reason=proposal.get("change_reason", "") or proposal.get("rationale", ""),
                expected_assignment_version=int(assignment.get("version", 1)),
            ),
            original_activity=self._parent_activity_view(original),
            proposed_activity=self._parent_activity_view(proposed),
            decision=S.ParentDecisionFlags(
                can_accept=flags.can_accept,
                can_decline=flags.can_decline,
                accepted_or_declined_at=decided_at,
                resulting_assignment_id=proposal.get("resulting_assignment_id"),
            ),
        )

    def _parent_add_detail(
        self, child_id: str, proposal: dict
    ) -> S.ParentAddProposalDecisionDetail:
        """Parent-safe detail for ONE ADD proposal. Read-only; nothing is written.

        Answers the four questions an Add actually raises for a family: which
        weekday, what is already on it, what would be added, and why. There is no
        `original_activity` and no `expected_assignment_version` — an Add replaces
        nothing and touches no assignment, so fabricating either would describe a
        change that is not being proposed.

        Any inconsistency in the linked records raises the same existence-blind
        `ChildNotFound` (404) used everywhere else, rather than a 500 or a partial
        body that would disclose which record was broken.
        """
        proposal_id = proposal["id"]
        if not eligibility.proposal_is_safe_to_show(self.repo, child_id, proposal):
            raise access.ChildNotFound(proposal_id)

        proposed = self._version(proposal.get("proposed_activity_version_id"))
        destination = self._parent_destination(proposal)
        if proposed is None or destination is None:
            raise access.ChildNotFound(proposal_id)

        child = self._child(child_id) or {}
        therapist_rows = self.repo.query(C.THERAPIST_PROFILES, id=proposal.get("therapist_id"))
        therapist = therapist_rows[0] if therapist_rows else {}
        therapist_name = therapist.get("display_name", "")
        if therapist.get("credentials"):
            therapist_name = f"{therapist_name}, {therapist['credentials']}"

        decided_at = proposal.get("decided_at")
        flags = eligibility.evaluate_parent_decision(self.repo, child_id, proposal)

        return S.ParentAddProposalDecisionDetail(
            proposal=S.ParentProposalSummary(
                proposal_id=proposal_id,
                proposal_type=_plain(proposal["proposal_type"]),
                proposal_status=_plain(proposal["status"]),
                proposal_version=int(proposal.get("version", 1)),
                created_at=proposal.get("created_at", ""),
                decided_at=decided_at,
            ),
            child=S.ParentChildSummary(
                child_id=child_id, display_name=child.get("display_name", "")
            ),
            therapist=S.ParentTherapistSummary(display_name=therapist_name),
            destination=destination,
            # CURRENT plan state, read now — not a snapshot stored on the proposal.
            existing_day_activities=self._parent_day_activities(
                child_id, proposal["weekly_plan_id"],
                proposal["destination_scheduled_day"],
            ),
            proposed_activity=self._parent_activity_view(proposed),
            change_reason=proposal.get("change_reason", "") or proposal.get("rationale", ""),
            decision=S.ParentAddDecisionFlags(
                can_accept=flags.can_accept,
                can_decline=flags.can_decline,
                needs_parent_attention=flags.needs_parent_attention,
                accepted_or_declined_at=decided_at,
            ),
        )

    def list_milestones(self, user: AuthenticatedUser) -> List[S.MilestoneView]:
        access.resolve_therapist(self.repo, user)
        return [S.MilestoneView(
            milestone_id=m["id"], domain=m["domain"], title=m["title"],
            description=m.get("description", ""),
            source_age_band_months=m.get("source_age_band_months"),
            age_gated=False,
        ) for m in sorted(self.repo.query(C.MILESTONES), key=lambda m: m["id"])]
