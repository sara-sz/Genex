"""Response DTOs for the read-only therapist API.

These are the public contracts consumed by the approved Therapist Dev frontend.
Independent states (plan approval / practice / assignment; note review /
session-preparation) are kept as SEPARATE fields — never collapsed.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class Page(BaseModel):
    """Pagination-ready list envelope (cursor reserved for future growth)."""

    items: List[dict]
    total: int
    next_cursor: Optional[str] = None


class MeResponse(BaseModel):
    principal_id: str
    uid: str
    role: str
    display_name: str
    therapist_id: Optional[str] = None
    credentials: Optional[str] = None
    organization: Optional[str] = None
    contact_email: Optional[str] = None
    preferences: dict = Field(default_factory=dict)


class ActivityProvenance(BaseModel):
    created_by_type: str
    created_by_user_id: Optional[str] = None
    created_by_display_name: Optional[str] = None
    original_activity_template_id: Optional[str] = None
    original_activity_version_id: Optional[str] = None
    modified_by_user_id: Optional[str] = None
    modified_by_display_name: Optional[str] = None
    save_scope: str
    is_derived: bool


class ConnectedChildSummary(BaseModel):
    child_id: str
    display_name: str
    parent_display_name: str
    connection_status: str
    active_practice_domains: List[str]
    plan_review_count: int
    new_parent_note_count: int
    pending_proposal_count: int


class ChildOverview(BaseModel):
    child_id: str
    display_name: str
    family_context: str
    active_practice_domains: List[str]
    home_practice_availability: str
    interests_and_motivators: List[str]
    recent_home_practice: List[dict]  # informational only
    next_session_items: List[dict]
    connection_summary: dict
    plan_review_count: int = 0
    pending_proposal_count: int = 0


class PlanAssignmentView(BaseModel):
    assignment_id: str
    scheduled_day: int
    plan_approval_status: str
    practice_status: str
    assignment_status: str
    activity_template_id: str
    activity_version_id: str
    activity_title: str
    provenance: ActivityProvenance
    parent_feedback_summary: str
    pending_proposal_id: Optional[str] = None
    pending_proposal: Optional[dict] = None   # summary when a pending proposal exists
    version: int = 1
    updated_at: str = ""


class WeeklyPlanResponse(BaseModel):
    child_id: str
    weekly_plan_id: str
    week_start_date: str
    assignments: List[PlanAssignmentView]


class ProgressSummary(BaseModel):
    child_id: str
    active_practice_domains: List[str]
    practice_status_counts: dict
    recent_home_practice: List[dict]  # informational only


class ParentNoteView(BaseModel):
    note_id: str
    child_id: str
    note_type: str
    review_status: str
    session_preparation_status: str
    linked_assignment_id: Optional[str] = None
    linked_activity_title: Optional[str] = None
    body: str
    created_at: str


class PrivateNoteView(BaseModel):
    note_id: str
    child_id: str
    body: str
    marked_for_next_session: bool
    created_at: str


class NextSessionResponse(BaseModel):
    child_id: str
    parent_note_items: List[ParentNoteView]
    private_note_items: List[PrivateNoteView]


class ConnectionDetails(BaseModel):
    child_id: str
    display_name: str
    parent_display_name: str
    connection_status: str
    restricted: bool
    activation_reminder_simulated: bool
    invited_at: str
    activated_at: Optional[str] = None


class ActivityTemplateView(BaseModel):
    activity_template_id: str
    title: str
    domain: str
    milestone_ids: List[str]
    instructions: str
    materials: str
    created_by_type: str
    immutable: bool
    versions: List[dict] = Field(default_factory=list)


class MilestoneView(BaseModel):
    milestone_id: str
    domain: str
    title: str
    description: str
    source_age_band_months: Optional[List[int]] = None
    # explicit: activities match by milestone id + domain, NOT chronological age
    age_gated: bool = False


# ── write: weekly-plan assignment approval ──────────────────────────────────
class ApproveAssignmentRequest(BaseModel):
    expected_assignment_version: int


class ApprovedAssignment(BaseModel):
    assignment_id: str
    child_id: str
    weekly_plan_id: str
    scheduled_day: int
    plan_approval_status: str
    practice_status: str
    assignment_status: str
    activity_template_id: str
    activity_version_id: str
    version: int
    updated_at: str


class ApprovalChildSummary(BaseModel):
    child_id: str
    plan_review_count: int


class ApprovalResponse(BaseModel):
    assignment: ApprovedAssignment
    child_summary: ApprovalChildSummary
    audit_event_id: str
    idempotent_replay: bool


class ErrorResponse(BaseModel):
    """Stable typed error envelope (no stack traces / internal names)."""

    error: str        # stable code, e.g. "assignment_version_conflict"
    detail: str = ""


# ── write: modify-activity proposal creation ────────────────────────────────
class ModifyActivityInput(BaseModel):
    title: str
    developmental_domain: str        # snake-case key, e.g. "talking_and_communicating"
    milestone_id: str
    skill_focus: str = ""
    duration_minutes: Optional[int] = None
    difficulty: str = ""
    materials: List[str] = Field(default_factory=list)
    materials_type: str = ""
    setup: str = ""
    parent_instructions: List[str] = Field(default_factory=list)
    what_to_say: List[str] = Field(default_factory=list)
    how_to_help: List[str] = Field(default_factory=list)
    success_signals: List[str] = Field(default_factory=list)
    variations: List[str] = Field(default_factory=list)
    routine_tags: List[str] = Field(default_factory=list)
    theme_tags: List[str] = Field(default_factory=list)
    safety_risk_flags: List[str] = Field(default_factory=list)
    # NOTE: no chronological age range by design.


class ModifyProposalRequest(BaseModel):
    expected_assignment_version: int
    activity: ModifyActivityInput
    change_reason: str = ""
    save_scope: str = "child_only"


class ProposalSummary(BaseModel):
    proposal_id: str
    proposal_type: str
    proposal_status: str
    child_id: str
    weekly_plan_id: str
    current_assignment_id: str
    original_activity_template_id: str
    original_activity_version_id: str
    proposed_activity_version_id: str
    created_by_user_id: str
    created_at: str
    version: int


class ProposalCreateResponse(BaseModel):
    proposal: ProposalSummary
    proposed_activity_version: dict
    current_assignment: dict
    child_summary: dict
    audit_event_id: str
    idempotent_replay: bool


# ── write: parent acceptance of a modify proposal ───────────────────────────
class AcceptProposalRequest(BaseModel):
    """Both expected versions are REQUIRED (optimistic concurrency)."""

    expected_proposal_version: int
    expected_assignment_version: int


class AcceptedProposalSummary(BaseModel):
    proposal_id: str
    proposal_type: str
    proposal_status: str
    version: int
    decided_by_user_id: Optional[str] = None
    decided_by_role: Optional[str] = None
    decided_at: Optional[str] = None
    resulting_assignment_id: Optional[str] = None
    original_activity_version_id: Optional[str] = None
    proposed_activity_version_id: Optional[str] = None


class AcceptProposalResponse(BaseModel):
    proposal: AcceptedProposalSummary
    retired_assignment: dict
    replacement_assignment: dict
    child_summary: dict
    audit_event_id: str
    idempotent_replay: bool


class ProposalView(BaseModel):
    proposal_id: str
    proposal_type: str
    proposal_status: str
    child_id: str
    weekly_plan_id: Optional[str] = None
    current_assignment_id: Optional[str] = None
    original_activity_template_id: Optional[str] = None
    original_activity_version_id: Optional[str] = None
    proposed_activity_version_id: Optional[str] = None
    change_reason: str = ""
    save_scope: str = ""
    created_by_user_id: Optional[str] = None
    created_at: str = ""
    # decision metadata (populated once a parent has accepted)
    decided_by_user_id: Optional[str] = None
    decided_by_role: Optional[str] = None
    decided_at: Optional[str] = None
    resulting_assignment_id: Optional[str] = None
    version: int = 1
