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
    # Presentation order within the day (zero-based). A weekday may hold several
    # current activities; they are returned sorted by this.
    display_order: int = 0
    plan_approval_status: str
    practice_status: str
    assignment_status: str
    activity_template_id: Optional[str] = None   # null for an ADD-created assignment
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


# ── write: parent Question / Note / Update (Phase 1B.3A) ────────────────────
#
# One-way parent -> therapist. NOT a chat message: there is deliberately no
# reply_to_note_id, thread_id, conversation_id, message status, read receipt or
# therapist reply anywhere in this contract, and none may be added.
class ParentNoteCreateRequest(BaseModel):
    """Everything a parent may supply. Everything else is system-owned.

    `review_status` and `session_preparation_status` are absent BY DESIGN — they
    describe what the therapist has done, and at creation the therapist has done
    nothing. A client sending them is simply ignored by this model; a test pins
    that they cannot be injected.
    """

    note_type: str                          # question | note | update
    body: str                               # the canonical ParentNote text field
    # Optional activity context. Must be one of this child's CURRENT activities.
    linked_assignment_id: Optional[str] = None


class ParentNoteCreated(BaseModel):
    """Parent-safe view of the note just submitted.

    Carries the activity TITLE the parent already sees — never the assignment,
    weekly-plan, activity-version or template id.
    """

    note_id: str
    note_type: str
    body: str
    created_at: str
    # Shown so the parent UI can say "your therapist has not read this yet".
    review_status: str
    session_preparation_status: str
    linked_activity_title: Optional[str] = None


class ParentNoteCreateResponse(BaseModel):
    note: ParentNoteCreated
    idempotent_replay: bool


# ── read: a parent's OWN submitted collaboration notes (Phase 1B.3B) ────────
#
# History of what THIS parent submitted for THIS child. Filtered by the stored
# author identity, never by child ownership — another caregiver's submissions
# are not this parent's to read.
class ParentNoteHistoryItem(BaseModel):
    """One item the parent submitted. Identical field set to `ParentNoteCreated`.

    Deliberately the same shape as the create response so a parent client renders
    a just-submitted item and a historical one with one component. `parent_id` is
    absent BY DESIGN: every item in the response is the caller's own, so echoing
    author identity would add no information and only widen the surface.
    """

    note_id: str
    note_type: str
    body: str
    created_at: str
    review_status: str
    session_preparation_status: str
    # Point-in-time context captured at submission — NOT re-resolved on read, so
    # a later Modify/Add never rewrites what the parent originally saw.
    linked_activity_title: Optional[str] = None


class ParentNoteHistoryResponse(BaseModel):
    """Parent-safe note history envelope.

    Carries `child_id` — route context the caller already supplied — which also
    makes this model structurally DISJOINT from the therapist `Page`, so the
    role-aware union on this route cannot validate one response as the other.
    """

    child_id: str
    items: List[ParentNoteHistoryItem] = Field(default_factory=list)
    total: int
    next_cursor: Optional[str] = None


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
    display_order: int = 0
    plan_approval_status: str
    practice_status: str
    assignment_status: str
    activity_template_id: Optional[str] = None   # null for an ADD-created assignment
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


# ── write: ADD-activity proposal creation ───────────────────────────────────
#
# Add means ADD ANOTHER activity to the chosen weekday. The day does not need to
# be empty, several current activities may already sit there, and several pending
# Add proposals may name the same day. Nothing is positioned at creation time:
# there is no display_order in the request or the response, and no PlanAssignment
# exists until a parent accepts.
class AddProposalRequest(BaseModel):
    """Therapist request to add one extra activity to a weekday."""

    # 0=Mon .. 6=Sun. Range is enforced in the service so an out-of-range day
    # returns the project's typed `invalid_request` envelope rather than
    # FastAPI's generic validation shape.
    scheduled_day: int = Field(description="Destination weekday, 0=Mon .. 6=Sun.")
    expected_weekly_plan_id: str = Field(
        description="The weekly plan the therapist believes is current (optimistic check)."
    )
    # The SAME canonical, validated activity content Modify uses.
    activity: ModifyActivityInput
    change_reason: str = ""
    save_scope: str = "child_only"


class AddProposalSummary(BaseModel):
    proposal_id: str
    proposal_type: str                      # always "add"
    proposal_status: str
    child_id: str
    weekly_plan_id: str
    destination_scheduled_day: int
    proposed_activity_version_id: str
    created_by_user_id: str
    created_at: str
    version: int
    # No assignment exists until a parent accepts, so this stays null here.
    resulting_assignment_id: Optional[str] = None


class AddProposalCreateResponse(BaseModel):
    """Therapist-authorized view of a newly created Add proposal.

    Deliberately has NO `current_assignment` block (Add targets a day, not an
    assignment), no display_order, no reserved position, and never the internal
    destination target token used for idempotency.
    """

    proposal: AddProposalSummary
    proposed_activity_version: dict
    child_summary: dict
    audit_event_id: str
    idempotent_replay: bool


# ── write: parent acceptance of a modify proposal ───────────────────────────
class AcceptProposalRequest(BaseModel):
    """Parent decision request — type-aware optimistic concurrency.

    `expected_proposal_version` is always required.

    `expected_assignment_version` is optional **at the transport layer only**, so
    the same route can carry both decision types. It is NOT optional in
    behaviour: a MODIFY decision still REQUIRES it and the service rejects a
    MODIFY that omits it with a typed `invalid_request` (422) — the same status
    FastAPI produced when the field was declared required. An ADD touches no
    existing assignment, so it must not be forced to invent a version number.
    """

    expected_proposal_version: int
    expected_assignment_version: Optional[int] = None


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


# ── read: parent-safe proposal decision detail ──────────────────────────────
#
# A DEDICATED projection, not a filtered therapist model. Every field below is
# explicitly allowed for a parent making a decision about their own child; the
# therapist-only `ProposalView` is untouched and never reaches a parent.
class ParentActivityView(BaseModel):
    """Parent-facing activity content. No provenance, ownership or save_scope."""

    title: str
    developmental_domain: str        # canonical display label, e.g. "Talking & Communicating"
    milestone_id: Optional[str] = None
    milestone_display_name: str = ""
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


class ParentProposalSummary(BaseModel):
    proposal_id: str
    proposal_type: str
    proposal_status: str
    proposal_version: int            # feeds expected_proposal_version on accept/decline
    created_at: str = ""
    decided_at: Optional[str] = None


class ParentChildSummary(BaseModel):
    child_id: str
    display_name: str = ""


class ParentTherapistSummary(BaseModel):
    """Only the therapist's presentable name — never their id or contact data."""

    display_name: str = ""


class ParentDecisionContext(BaseModel):
    change_reason: str = ""
    # feeds expected_assignment_version on accept/decline
    expected_assignment_version: int


class ParentDecisionFlags(BaseModel):
    """Computed from canonical backend state, never from display labels."""

    can_accept: bool
    can_decline: bool
    accepted_or_declined_at: Optional[str] = None
    # Present only for an accepted proposal; null for pending and declined.
    resulting_assignment_id: Optional[str] = None


class ParentProposalDecisionDetail(BaseModel):
    """Parent decision detail for a **MODIFY** proposal — frozen contract.

    Deliberately keeps its original name. An ADD proposal uses the separate
    `ParentAddProposalDecisionDetail` below rather than being forced into this
    shape, because an Add replaces nothing and has no original activity to
    compare against.
    """

    proposal: ParentProposalSummary
    child: ParentChildSummary
    therapist: ParentTherapistSummary
    decision_context: ParentDecisionContext
    original_activity: ParentActivityView
    proposed_activity: ParentActivityView
    decision: ParentDecisionFlags


# ── read: parent-safe ADD proposal projections ──────────────────────────────
#
# An Add asks "put ONE MORE activity on this weekday". There is no original
# activity, so the parent question is not "what changes?" but "what is already on
# that day, and what would be added to it?". These models answer exactly that and
# nothing else.
class ParentDestinationDay(BaseModel):
    """Which weekday the therapist wants to add an activity to."""

    scheduled_day: int               # 0=Mon .. 6=Sun
    day_label: str = ""              # presentation only, e.g. "Tuesday"


class ParentDayActivitySummary(BaseModel):
    """One activity already on the destination day — context, not a decision.

    A teaser, not the full instructions: the parent is being shown what the day
    holds so the addition makes sense, not being asked to re-read every activity.
    Carries NO `display_order` — position is conveyed by array order alone — and
    no assignment id, plan id, version id, status or approval internals.
    """

    title: str
    developmental_domain: str
    milestone_display_name: str = ""
    duration_minutes: Optional[int] = None


class ParentAddDecisionFlags(BaseModel):
    """Actionability for an Add.

    Structurally lighter than `ParentDecisionFlags`: an Add has no
    `resulting_assignment_id` because accepting one is not implemented, and
    inventing the field would imply a capability that does not exist.
    """

    can_accept: bool
    can_decline: bool
    needs_parent_attention: bool
    accepted_or_declined_at: Optional[str] = None


class ParentAddProposalDecisionDetail(BaseModel):
    """Parent-safe detail for an ADD proposal.

    Disjoint from `ParentProposalDecisionDetail` on required fields — this model
    requires `destination` and `existing_day_activities`, that one requires
    `original_activity` and `decision_context` — so the role-aware union cannot
    validate either response as the other and silently reshape it.

    `existing_day_activities` is **current plan state read at request time**, not
    a snapshot stored on the proposal. The proposal records what the therapist
    recommended; the day list records what the family's plan holds right now.
    """

    proposal: ParentProposalSummary
    child: ParentChildSummary
    therapist: ParentTherapistSummary
    destination: ParentDestinationDay
    existing_day_activities: List[ParentDayActivitySummary] = Field(default_factory=list)
    proposed_activity: ParentActivityView
    change_reason: str = ""
    decision: ParentAddDecisionFlags


# ── read: parent-safe proposal LIST (discovery only) ────────────────────────
#
# Deliberately lighter than ParentProposalDecisionDetail. It carries NO activity
# instructions and NO optimistic-concurrency versions: a client must open the
# detail endpoint before submitting a decision, so it always submits the freshest
# proposal_version / expected_assignment_version.
class ParentProposalActivitySummary(BaseModel):
    """Minimal proposed-activity teaser — enough to recognise the change."""

    title: str
    developmental_domain: str
    milestone_display_name: str = ""


class ParentProposalDecisionSummary(BaseModel):
    """Actionability, computed by the shared read-only eligibility evaluator."""

    needs_parent_attention: bool
    can_accept: bool
    can_decline: bool


class ParentProposalListItem(BaseModel):
    proposal_id: str
    proposal_type: str
    proposal_status: str
    created_at: str = ""
    decided_at: Optional[str] = None
    child: ParentChildSummary
    therapist: ParentTherapistSummary
    proposed_activity: ParentProposalActivitySummary
    change_reason: str = ""
    decision: ParentProposalDecisionSummary
    # ADD only: which weekday the extra activity is proposed for. **Null for
    # MODIFY**, so the frozen Modify list item is unchanged in value. The list
    # stays lightweight — the day's existing activities appear in the DETAIL
    # response only, never once per list item.
    destination: Optional[ParentDestinationDay] = None


class ParentProposalListResponse(BaseModel):
    items: List[ParentProposalListItem]
    total: int
    next_cursor: Optional[str] = None


# ── write: parent decline of a modify proposal ──────────────────────────────
# ── write: parent decision on an ADD proposal ───────────────────────────────
#
# Add decisions get their own response models rather than being forced into the
# Modify shapes. A Modify accept reports a `retired_assignment` and a
# `replacement_assignment`; an Add retires and replaces nothing, so reusing that
# model would mean inventing two objects that do not exist.
#
# Each Add model is DISJOINT from its Modify counterpart on required fields —
# `added_assignment` vs `retired_assignment`/`replacement_assignment`, and
# `destination_scheduled_day` vs `current_assignment` — so the role-aware union
# on these routes cannot validate one response as the other and reshape it.
class AddDecisionProposalSummary(BaseModel):
    proposal_id: str
    proposal_type: str                     # always "add"
    proposal_status: str                   # accepted | declined
    version: int
    decided_by_user_id: Optional[str] = None
    decided_by_role: Optional[str] = None
    decided_at: Optional[str] = None
    # Set on accept; stays null on decline because nothing was created.
    resulting_assignment_id: Optional[str] = None
    proposed_activity_version_id: Optional[str] = None


class AddAcceptResponse(BaseModel):
    """Parent accepted an Add: exactly one activity was APPENDED to the day."""

    proposal: AddDecisionProposalSummary
    added_assignment: dict                 # required — the one new assignment
    child_summary: dict
    audit_event_id: str
    idempotent_replay: bool


class AddDeclineResponse(BaseModel):
    """Parent declined an Add: nothing was created and the day is unchanged."""

    proposal: AddDecisionProposalSummary
    # Required, and unique to this model: which weekday the declined Add targeted.
    destination_scheduled_day: int
    child_summary: dict
    audit_event_id: str
    idempotent_replay: bool


class DeclineProposalRequest(BaseModel):
    """Parent decision request — see `AcceptProposalRequest` for the contract.

    `expected_assignment_version` is transport-optional so ADD can omit it;
    MODIFY still requires it and fails with a typed `invalid_request` (422)
    otherwise.
    """

    expected_proposal_version: int
    expected_assignment_version: Optional[int] = None


class DeclinedProposalSummary(BaseModel):
    proposal_id: str
    proposal_type: str
    proposal_status: str
    version: int
    decided_by_user_id: Optional[str] = None
    decided_by_role: Optional[str] = None
    decided_at: Optional[str] = None
    # Always null for a decline: nothing replaces the original assignment.
    resulting_assignment_id: Optional[str] = None
    original_activity_version_id: Optional[str] = None
    proposed_activity_version_id: Optional[str] = None


class DeclineProposalResponse(BaseModel):
    proposal: DeclinedProposalSummary
    current_assignment: dict
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
    # ADD only: destination weekday. Null for MODIFY, so this is strictly
    # additive and the frozen Modify therapist response is unchanged in value.
    destination_scheduled_day: Optional[int] = None
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
