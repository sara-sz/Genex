"""Canonical domain models for the therapist read-slice (Phase 1A).

Frontend-only, fictional, read-only. Every entity carries a stable string id.
These are the persistence/domain contracts; response DTOs (built in the service
layer) reshape them for the approved Therapist Dev frontend.

Design notes:
  * Activity matching is by milestone id + developmental domain — NEVER by
    chronological age. `DevelopmentalMilestone` may keep a source age band for
    provenance only; `ActivityTemplate`/`ActivityVersion` carry NO age range.
  * Activity templates are immutable; a "modify" produces an immutable derived
    `ActivityVersion` referencing the original template/version.
  * Parent-note review status and session-preparation status are independent.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from .enums import (
    ActivitySaveScope,
    AssignmentStatus,
    ConnectionStatus,
    CreatedByType,
    ParentNoteReviewStatus,
    ParentNoteType,
    PlanApprovalStatus,
    PracticeStatus,
    PrincipalRole,
    ProposalStatus,
    ProposalType,
    SessionPreparationStatus,
)

SCHEMA_VERSION = "therapist-read-0.2"


class UserPrincipal(BaseModel):
    """An authenticated actor (resolved server-side, never from the UI)."""

    id: str
    uid: str  # auth subject
    role: PrincipalRole
    display_name: str
    therapist_id: Optional[str] = None  # set when role == therapist
    parent_id: Optional[str] = None     # set when role == parent
    environment: str = "dev"
    schema_version: str = SCHEMA_VERSION


class TherapistPreferences(BaseModel):
    default_session_length_minutes: int = 30
    show_next_session_prep: bool = True
    schema_version: str = SCHEMA_VERSION


class TherapistProfile(BaseModel):
    id: str
    uid: str
    display_name: str
    credentials: str = ""          # e.g. "MA, SLP" (fictional)
    discipline: str = "slp"
    organization: str = ""
    contact_email: str = ""        # fictional .example only
    preferences: TherapistPreferences = Field(default_factory=TherapistPreferences)
    environment: str = "dev"
    schema_version: str = SCHEMA_VERSION


class ParentProfile(BaseModel):
    id: str
    uid: str
    display_name: str
    contact_email: str = ""        # fictional .example only
    environment: str = "dev"
    schema_version: str = SCHEMA_VERSION


class Child(BaseModel):
    """A (fictional) child. Name-minimal by design; alias-style display name."""

    id: str
    parent_id: str
    display_name: str              # fictional first name / alias
    family_context: str = ""
    active_practice_domains: List[str] = Field(default_factory=list)
    home_practice_availability: str = ""
    interests_and_motivators: List[str] = Field(default_factory=list)
    environment: str = "dev"
    schema_version: str = SCHEMA_VERSION


class TherapistChildConnection(BaseModel):
    id: str
    therapist_id: str
    child_id: str
    parent_id: str
    status: ConnectionStatus
    invited_at: str = ""
    activated_at: Optional[str] = None
    activation_reminder_simulated: bool = False  # restricted-connection reminder sim
    environment: str = "dev"
    schema_version: str = SCHEMA_VERSION


class DevelopmentalMilestone(BaseModel):
    id: str
    domain: str                     # developmental domain (display taxonomy)
    title: str
    description: str = ""
    # Provenance-only source age band; NOT used for activity matching/gating.
    source_age_band_months: Optional[List[int]] = None
    environment: str = "dev"
    schema_version: str = SCHEMA_VERSION


class ActivityTemplate(BaseModel):
    """Immutable canonical activity. NO chronological age range by design."""

    id: str
    title: str
    domain: str
    milestone_ids: List[str] = Field(default_factory=list)
    instructions: str = ""
    materials: str = ""
    created_by_type: CreatedByType = CreatedByType.GENEX
    created_by_user_id: Optional[str] = None
    created_by_display_name: Optional[str] = None
    immutable: bool = True
    environment: str = "dev"
    schema_version: str = SCHEMA_VERSION


class ActivityVersion(BaseModel):
    """A concrete (possibly therapist-derived) immutable version of an activity."""

    id: str
    activity_template_id: str
    title: str
    domain: str
    milestone_ids: List[str] = Field(default_factory=list)
    instructions: str = ""
    materials: str = ""
    # Provenance
    created_by_type: CreatedByType = CreatedByType.GENEX
    created_by_user_id: Optional[str] = None
    created_by_display_name: Optional[str] = None
    original_activity_template_id: Optional[str] = None
    original_activity_version_id: Optional[str] = None
    modified_by_user_id: Optional[str] = None
    modified_by_display_name: Optional[str] = None
    save_scope: ActivitySaveScope = ActivitySaveScope.CHILD_ONLY
    is_derived: bool = False
    immutable: bool = True
    environment: str = "dev"
    schema_version: str = SCHEMA_VERSION


class PlanAssignment(BaseModel):
    """One activity assigned into a child's weekly plan on a given weekday."""

    id: str
    weekly_plan_id: str
    child_id: str
    activity_template_id: str
    activity_version_id: str
    scheduled_day: int              # 0=Mon .. 6=Sun
    plan_approval_status: PlanApprovalStatus
    practice_status: PracticeStatus = PracticeStatus.NOT_TRIED
    assignment_status: AssignmentStatus = AssignmentStatus.CURRENT
    parent_feedback_summary: str = ""
    pending_proposal_id: Optional[str] = None
    environment: str = "dev"
    schema_version: str = SCHEMA_VERSION


class WeeklyPlan(BaseModel):
    id: str
    child_id: str
    week_start_date: str            # fictional ISO date
    environment: str = "dev"
    schema_version: str = SCHEMA_VERSION


class PlanChangeProposal(BaseModel):
    """A proposed add/modify/replace/remove awaiting parent acceptance.

    Read-only in this phase (no writes). Present so assignments can reference a
    pending proposal and restricted/pending counts can be derived.
    """

    id: str
    child_id: str
    therapist_id: str
    proposal_type: ProposalType
    status: ProposalStatus
    target_assignment_id: Optional[str] = None
    proposed_activity_version_id: Optional[str] = None
    replacement_activity_version_id: Optional[str] = None
    rationale: str = ""
    environment: str = "dev"
    schema_version: str = SCHEMA_VERSION


class ParentNote(BaseModel):
    """A parent's written comment. No therapist replies / no chat.

    Review status and session-preparation status are INDEPENDENT fields.
    """

    id: str
    child_id: str
    parent_id: str
    note_type: ParentNoteType
    body: str
    review_status: ParentNoteReviewStatus = ParentNoteReviewStatus.NEW
    session_preparation_status: SessionPreparationStatus = SessionPreparationStatus.NONE
    linked_assignment_id: Optional[str] = None
    linked_activity_title: Optional[str] = None
    created_at: str = ""
    environment: str = "dev"
    schema_version: str = SCHEMA_VERSION


class PrivateTherapistNote(BaseModel):
    """Visible ONLY to the authoring therapist."""

    id: str
    child_id: str
    therapist_id: str
    body: str
    marked_for_next_session: bool = False
    created_at: str = ""
    environment: str = "dev"
    schema_version: str = SCHEMA_VERSION


class AuditEvent(BaseModel):
    """Append-only audit record. No writes in this phase — schema only."""

    id: str
    event_type: str
    actor_uid: str
    actor_role: PrincipalRole
    subject_type: str
    subject_id: str
    child_id: Optional[str] = None
    request_id: Optional[str] = None
    created_at: str = ""
    environment: str = "dev"
    schema_version: str = SCHEMA_VERSION
