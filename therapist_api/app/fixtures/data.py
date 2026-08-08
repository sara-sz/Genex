"""Fictional fixture data (dev/test only).

All identities, children, notes, and activities are invented. Emails use the
reserved `.example` TLD. Nothing here is real personal or health information.
"""

from __future__ import annotations

from typing import List

from ..domain.enums import (
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
    WeeklyPlanStatus,
)
from ..domain.read_models import (
    ActivityTemplate,
    ActivityVersion,
    Child,
    DevelopmentalMilestone,
    ParentNote,
    ParentProfile,
    PlanAssignment,
    PlanChangeProposal,
    PrivateTherapistNote,
    TherapistChildConnection,
    TherapistProfile,
    UserPrincipal,
    WeeklyPlan,
)

ENV = "dev"

# ── Principals (fictional dev-auth subjects) ────────────────────────────────
HANNAH_UID = "dev-hannah"
ELENA_UID = "dev-elena"
UNCONNECTED_THERAPIST_UID = "dev-unconnected-therapist"
PRIYA_UID = "dev-priya"

THERAPIST_HANNAH = "ther_hannah"
THERAPIST_OTHER = "ther_other"
THERAPIST_PRIYA = "ther_priya"   # actively connected, but to a DIFFERENT child
PARENT_ELENA = "par_elena"
PARENT_OMAR = "par_omar"
PARENT_ROSA = "par_rosa"
PARENT_DEV = "par_dev"
PARENT_LEE = "par_lee"
PARENT_TAMSIN = "par_tamsin"


def principals() -> List[UserPrincipal]:
    return [
        UserPrincipal(
            id="prin_hannah", uid=HANNAH_UID, role=PrincipalRole.THERAPIST,
            display_name="Hannah Lieberknecht", therapist_id=THERAPIST_HANNAH, environment=ENV,
        ),
        UserPrincipal(
            id="prin_elena", uid=ELENA_UID, role=PrincipalRole.PARENT,
            display_name="Elena Ruiz", parent_id=PARENT_ELENA, environment=ENV,
        ),
        UserPrincipal(
            id="prin_unconnected", uid=UNCONNECTED_THERAPIST_UID, role=PrincipalRole.THERAPIST,
            display_name="Unconnected Therapist", therapist_id=THERAPIST_OTHER, environment=ENV,
        ),
        UserPrincipal(
            id="prin_priya", uid=PRIYA_UID, role=PrincipalRole.THERAPIST,
            display_name="Priya Raman", therapist_id=THERAPIST_PRIYA, environment=ENV,
        ),
    ]


def therapists() -> List[TherapistProfile]:
    return [
        TherapistProfile(
            id=THERAPIST_HANNAH, uid=HANNAH_UID, display_name="Hannah Lieberknecht",
            credentials="MA, SLP", discipline="slp", organization="TalkShop",
            contact_email="hannah@talkshop.example", environment=ENV,
        ),
        TherapistProfile(
            id=THERAPIST_OTHER, uid=UNCONNECTED_THERAPIST_UID, display_name="Unconnected Therapist",
            credentials="MA, SLP", discipline="slp", organization="Elsewhere Clinic",
            contact_email="other@elsewhere.example", environment=ENV,
        ),
        # Actively connected to Theo only — used to prove that a therapist with a
        # live caseload still cannot see another therapist's child_only versions.
        TherapistProfile(
            id=THERAPIST_PRIYA, uid=PRIYA_UID, display_name="Priya Raman",
            credentials="MS, SLP", discipline="slp", organization="Northside Therapy",
            contact_email="priya@northside.example", environment=ENV,
        ),
    ]


def parents() -> List[ParentProfile]:
    return [
        ParentProfile(id=PARENT_ELENA, uid=ELENA_UID, display_name="Elena Ruiz", contact_email="elena@family.example", environment=ENV),
        ParentProfile(id=PARENT_OMAR, uid="dev-omar", display_name="Omar Haddad", contact_email="omar@family.example", environment=ENV),
        ParentProfile(id=PARENT_ROSA, uid="dev-rosa", display_name="Rosa Nkemi", contact_email="rosa@family.example", environment=ENV),
        ParentProfile(id=PARENT_DEV, uid="dev-devika", display_name="Devika Rao", contact_email="devika@family.example", environment=ENV),
        ParentProfile(id=PARENT_LEE, uid="dev-lee", display_name="Lee Park", contact_email="lee@family.example", environment=ENV),
        ParentProfile(id=PARENT_TAMSIN, uid="dev-tamsin", display_name="Tamsin Boyd", contact_email="tamsin@family.example", environment=ENV),
    ]


# ── Children (fictional) ────────────────────────────────────────────────────
CHILD_MAYA = "child_maya"
CHILD_ELI = "child_eli"
CHILD_NOAH = "child_noah"
CHILD_AMARA = "child_amara"
CHILD_SANA = "child_sana"
CHILD_THEO = "child_theo"   # Priya's caseload only — never Hannah's
CHILD_RUE = "child_rue"     # Hannah's connection has ENDED


def children() -> List[Child]:
    return [
        Child(id=CHILD_MAYA, parent_id=PARENT_ELENA, display_name="Maya",
              family_context="Lives with mom and older sibling; bilingual home (Spanish/English).",
              active_practice_domains=["Talking & Communicating", "Social & Emotional"],
              home_practice_availability="~10 min on weekday evenings",
              interests_and_motivators=["bubbles", "toy animals", "peekaboo"], environment=ENV),
        Child(id=CHILD_ELI, parent_id=PARENT_OMAR, display_name="Eli",
              family_context="Lives with both parents; loves the park.",
              active_practice_domains=["Talking & Communicating"],
              home_practice_availability="mornings before daycare",
              interests_and_motivators=["cars", "music"], environment=ENV),
        Child(id=CHILD_NOAH, parent_id=PARENT_ROSA, display_name="Noah",
              family_context="Lives with grandmother and mom.",
              active_practice_domains=["Social & Emotional", "Learning & Thinking"],
              home_practice_availability="weekend afternoons",
              interests_and_motivators=["blocks", "picture books"], environment=ENV),
        Child(id=CHILD_AMARA, parent_id=PARENT_DEV, display_name="Amara",
              family_context="Connection invitation pending parent acceptance.",
              active_practice_domains=[], home_practice_availability="",
              interests_and_motivators=[], environment=ENV),
        Child(id=CHILD_SANA, parent_id=PARENT_LEE, display_name="Sana",
              family_context="Connection paused by parent.",
              active_practice_domains=["Talking & Communicating"],
              home_practice_availability="", interests_and_motivators=[], environment=ENV),
        Child(id=CHILD_THEO, parent_id=PARENT_TAMSIN, display_name="Theo",
              family_context="Seen by a different therapist (Priya).",
              active_practice_domains=["Talking & Communicating"],
              home_practice_availability="after school",
              interests_and_motivators=["trains"], environment=ENV),
        Child(id=CHILD_RUE, parent_id=PARENT_LEE, display_name="Rue",
              family_context="Connection ended; retained for audit history only.",
              active_practice_domains=[], home_practice_availability="",
              interests_and_motivators=[], environment=ENV),
    ]


def connections() -> List[TherapistChildConnection]:
    return [
        TherapistChildConnection(id="conn_maya", therapist_id=THERAPIST_HANNAH, child_id=CHILD_MAYA,
                                 parent_id=PARENT_ELENA, status=ConnectionStatus.ACTIVE,
                                 invited_at="2026-06-01", activated_at="2026-06-03", environment=ENV),
        TherapistChildConnection(id="conn_eli", therapist_id=THERAPIST_HANNAH, child_id=CHILD_ELI,
                                 parent_id=PARENT_OMAR, status=ConnectionStatus.ACTIVE,
                                 invited_at="2026-06-05", activated_at="2026-06-06", environment=ENV),
        TherapistChildConnection(id="conn_noah", therapist_id=THERAPIST_HANNAH, child_id=CHILD_NOAH,
                                 parent_id=PARENT_ROSA, status=ConnectionStatus.ACTIVE,
                                 invited_at="2026-06-08", activated_at="2026-06-09", environment=ENV),
        TherapistChildConnection(id="conn_amara", therapist_id=THERAPIST_HANNAH, child_id=CHILD_AMARA,
                                 parent_id=PARENT_DEV, status=ConnectionStatus.PENDING_PARENT_ACCEPTANCE,
                                 invited_at="2026-07-20", activation_reminder_simulated=True, environment=ENV),
        TherapistChildConnection(id="conn_sana", therapist_id=THERAPIST_HANNAH, child_id=CHILD_SANA,
                                 parent_id=PARENT_LEE, status=ConnectionStatus.PAUSED_BY_PARENT,
                                 invited_at="2026-05-10", activated_at="2026-05-11", environment=ENV),
        # Ended: never surfaced in Hannah's caseload and grants no visibility.
        TherapistChildConnection(id="conn_rue", therapist_id=THERAPIST_HANNAH, child_id=CHILD_RUE,
                                 parent_id=PARENT_LEE, status=ConnectionStatus.ENDED,
                                 invited_at="2026-03-01", activated_at="2026-03-02", environment=ENV),
        # Priya's only connection — a live caseload that excludes Hannah's children.
        TherapistChildConnection(id="conn_theo", therapist_id=THERAPIST_PRIYA, child_id=CHILD_THEO,
                                 parent_id=PARENT_TAMSIN, status=ConnectionStatus.ACTIVE,
                                 invited_at="2026-06-15", activated_at="2026-06-16", environment=ENV),
    ]


# ── Milestones (fictional; no age gating for matching) ──────────────────────
M_REQUEST = "mile_request_items"
M_TURNTAKE = "mile_turn_taking"
M_TWO_WORD = "mile_two_word_combos"


def milestones() -> List[DevelopmentalMilestone]:
    return [
        DevelopmentalMilestone(id=M_REQUEST, domain="Talking & Communicating",
                               title="Requests a desired item", description="Uses a word/sign/gesture to request.",
                               source_age_band_months=[12, 18], environment=ENV),
        DevelopmentalMilestone(id=M_TURNTAKE, domain="Social & Emotional",
                               title="Takes turns in a simple game", description="Waits and takes a turn with a partner.",
                               source_age_band_months=[18, 30], environment=ENV),
        DevelopmentalMilestone(id=M_TWO_WORD, domain="Talking & Communicating",
                               title="Combines two words", description="Produces two-word phrases.",
                               source_age_band_months=[20, 30], environment=ENV),
    ]


# ── Activity templates + versions ───────────────────────────────────────────
T_BUBBLES = "tmpl_bubbles"
T_TURNTAKE = "tmpl_turn_taking_ball"
V_BUBBLES = "ver_bubbles_v1"
V_BUBBLES_DERIVED = "ver_bubbles_hannah_v1"
V_TURNTAKE = "ver_turn_taking_v1"

# Derived versions exercising every save_scope / child-association combination.
# All are owned by Hannah and hang off the same canonical bubbles template, so a
# shared activity_template_id can never be mistaken for a grant of visibility.
V_LIB_HANNAH = "ver_lib_hannah_v1"          # therapist_library -> owner only
V_REVIEW_HANNAH = "ver_review_hannah_v1"    # submitted_for_genex_review -> submitter only
V_ORPHAN_HANNAH = "ver_orphan_hannah_v1"    # child_only, no child association -> hidden
V_PAUSED_HANNAH = "ver_paused_hannah_v1"    # child_only on a PAUSED connection -> hidden
V_PENDING_HANNAH = "ver_pending_hannah_v1"  # child_only on a PENDING connection -> hidden
V_ENDED_HANNAH = "ver_ended_hannah_v1"      # child_only on an ENDED connection -> hidden


def activity_templates() -> List[ActivityTemplate]:
    return [
        ActivityTemplate(id=T_BUBBLES, title="Bubble requesting", domain="Talking & Communicating",
                         milestone_ids=[M_REQUEST], instructions="Pause with the bubble wand; wait for a request.",
                         materials="bubbles", created_by_type=CreatedByType.GENEX, environment=ENV),
        ActivityTemplate(id=T_TURNTAKE, title="Turn-taking with a ball", domain="Social & Emotional",
                         milestone_ids=[M_TURNTAKE], instructions="Roll the ball back and forth, naming turns.",
                         materials="soft ball", created_by_type=CreatedByType.GENEX, environment=ENV),
    ]


def _hannah_derived(version_id: str, title: str, save_scope: ActivitySaveScope) -> ActivityVersion:
    """A Hannah-authored derived version of the canonical bubbles activity."""
    return ActivityVersion(
        id=version_id, activity_template_id=T_BUBBLES, title=title,
        domain="Talking & Communicating", milestone_ids=[M_REQUEST], milestone_id=M_REQUEST,
        instructions="Model the word first, then pause and wait.", materials=["bubbles"],
        created_by_type=CreatedByType.THERAPIST, created_by_user_id=THERAPIST_HANNAH,
        created_by_display_name="Hannah Lieberknecht",
        original_activity_template_id=T_BUBBLES, original_activity_version_id=V_BUBBLES,
        modified_by_user_id=THERAPIST_HANNAH, modified_by_display_name="Hannah Lieberknecht",
        save_scope=save_scope, is_derived=True, environment=ENV,
    )


def activity_versions() -> List[ActivityVersion]:
    return [
        ActivityVersion(id=V_BUBBLES, activity_template_id=T_BUBBLES, title="Bubble requesting",
                        domain="Talking & Communicating", milestone_ids=[M_REQUEST],
                        instructions="Pause with the bubble wand; wait for a request.", materials=["bubbles"],
                        milestone_id=M_REQUEST, created_by_type=CreatedByType.GENEX,
                        save_scope=ActivitySaveScope.CHILD_ONLY, is_derived=False, environment=ENV),
        # A therapist-derived immutable version that preserves the original template.
        ActivityVersion(id=V_BUBBLES_DERIVED, activity_template_id=T_BUBBLES, title="Bubble requesting (simplified prompt)",
                        domain="Talking & Communicating", milestone_ids=[M_REQUEST],
                        instructions="Model the word first, then pause and wait for any approximation.",
                        materials=["bubbles"], milestone_id=M_REQUEST, created_by_type=CreatedByType.THERAPIST,
                        created_by_user_id=THERAPIST_HANNAH, created_by_display_name="Hannah Lieberknecht",
                        original_activity_template_id=T_BUBBLES, original_activity_version_id=V_BUBBLES,
                        modified_by_user_id=THERAPIST_HANNAH, modified_by_display_name="Hannah Lieberknecht",
                        save_scope=ActivitySaveScope.CHILD_ONLY, is_derived=True, environment=ENV),
        ActivityVersion(id=V_TURNTAKE, activity_template_id=T_TURNTAKE, title="Turn-taking with a ball",
                        domain="Social & Emotional", milestone_ids=[M_TURNTAKE],
                        instructions="Roll the ball back and forth, naming turns.", materials=["soft ball"],
                        milestone_id=M_TURNTAKE, created_by_type=CreatedByType.GENEX,
                        save_scope=ActivitySaveScope.CHILD_ONLY, is_derived=False, environment=ENV),
        _hannah_derived(V_LIB_HANNAH, "Bubble requesting (Hannah's library copy)",
                        ActivitySaveScope.THERAPIST_LIBRARY),
        _hannah_derived(V_REVIEW_HANNAH, "Bubble requesting (submitted for Genex review)",
                        ActivitySaveScope.SUBMITTED_FOR_GENEX_REVIEW),
        _hannah_derived(V_ORPHAN_HANNAH, "Bubble requesting (draft, unattached)",
                        ActivitySaveScope.CHILD_ONLY),
        _hannah_derived(V_PAUSED_HANNAH, "Bubble requesting (Sana)", ActivitySaveScope.CHILD_ONLY),
        _hannah_derived(V_PENDING_HANNAH, "Bubble requesting (Amara)", ActivitySaveScope.CHILD_ONLY),
        _hannah_derived(V_ENDED_HANNAH, "Bubble requesting (Rue)", ActivitySaveScope.CHILD_ONLY),
    ]


# ── Weekly plans + assignments ──────────────────────────────────────────────
def weekly_plans() -> List[WeeklyPlan]:
    return [
        # Seeded BEFORE the current plan on purpose: a completed week that is
        # both older and stored first would be selected by a positional
        # `plans[0]` lookup, so its presence proves the canonical resolver keys
        # off lifecycle status rather than insertion order. It holds no
        # assignments — it exists to make "current" a real choice.
        WeeklyPlan(id="wp_maya_prev", child_id=CHILD_MAYA, week_start_date="2026-07-20",
                   status=WeeklyPlanStatus.COMPLETED, environment=ENV),
        WeeklyPlan(id="wp_maya", child_id=CHILD_MAYA, week_start_date="2026-07-27",
                   status=WeeklyPlanStatus.CURRENT, environment=ENV),
        WeeklyPlan(id="wp_eli", child_id=CHILD_ELI, week_start_date="2026-07-27",
                   status=WeeklyPlanStatus.CURRENT, environment=ENV),
        WeeklyPlan(id="wp_noah", child_id=CHILD_NOAH, week_start_date="2026-07-27",
                   status=WeeklyPlanStatus.CURRENT, environment=ENV),
    ]


PROP_MAYA_MODIFY = "prop_maya_modify_bubbles"


def plan_assignments() -> List[PlanAssignment]:
    return [
        # Maya: one approved + practiced, one needs-review with a pending modify proposal.
        PlanAssignment(id="assign_maya_bubbles", weekly_plan_id="wp_maya", child_id=CHILD_MAYA,
                       activity_template_id=T_BUBBLES, activity_version_id=V_BUBBLES, scheduled_day=0, display_order=0,
                       plan_approval_status=PlanApprovalStatus.APPROVED, practice_status=PracticeStatus.DID_IT,
                       assignment_status=AssignmentStatus.CURRENT,
                       parent_feedback_summary="Maya requested 'more' twice.", environment=ENV),
        PlanAssignment(id="assign_maya_turntake", weekly_plan_id="wp_maya", child_id=CHILD_MAYA,
                       activity_template_id=T_TURNTAKE, activity_version_id=V_TURNTAKE, scheduled_day=2, display_order=0,
                       plan_approval_status=PlanApprovalStatus.CHANGE_PENDING_PARENT,
                       practice_status=PracticeStatus.TRIED_WITH_HELP, assignment_status=AssignmentStatus.CURRENT,
                       parent_feedback_summary="Hard to wait for a turn.", pending_proposal_id=PROP_MAYA_MODIFY,
                       environment=ENV),
        # Eli: needs plan review (never approved).
        PlanAssignment(id="assign_eli_bubbles", weekly_plan_id="wp_eli", child_id=CHILD_ELI,
                       activity_template_id=T_BUBBLES, activity_version_id=V_BUBBLES, scheduled_day=1, display_order=0,
                       plan_approval_status=PlanApprovalStatus.NEEDS_PLAN_REVIEW, practice_status=PracticeStatus.NOT_TRIED,
                       assignment_status=AssignmentStatus.CURRENT, environment=ENV),
        # Noah: approved, loved it.
        PlanAssignment(id="assign_noah_turntake", weekly_plan_id="wp_noah", child_id=CHILD_NOAH,
                       activity_template_id=T_TURNTAKE, activity_version_id=V_TURNTAKE, scheduled_day=5, display_order=0,
                       plan_approval_status=PlanApprovalStatus.APPROVED, practice_status=PracticeStatus.LOVED_IT,
                       assignment_status=AssignmentStatus.CURRENT,
                       parent_feedback_summary="Noah giggled through it.", environment=ENV),
    ]


def plan_change_proposals() -> List[PlanChangeProposal]:
    return [
        PlanChangeProposal(id=PROP_MAYA_MODIFY, child_id=CHILD_MAYA, therapist_id=THERAPIST_HANNAH,
                           proposal_type=ProposalType.MODIFY, status=ProposalStatus.PENDING_PARENT_ACCEPTANCE,
                           target_assignment_id="assign_maya_turntake",
                           proposed_activity_version_id=V_BUBBLES_DERIVED,
                           rationale="Simplify the prompt; too difficult as-is.", environment=ENV),
        # Historical proposals that bind a child_only version to a child whose
        # connection is no longer active. They are what makes those versions
        # resolvable at all — and they must still not grant catalog visibility.
        PlanChangeProposal(id="prop_sana_modify", child_id=CHILD_SANA, therapist_id=THERAPIST_HANNAH,
                           proposal_type=ProposalType.MODIFY, status=ProposalStatus.PENDING_PARENT_ACCEPTANCE,
                           proposed_activity_version_id=V_PAUSED_HANNAH,
                           rationale="Drafted before the parent paused the connection.", environment=ENV),
        PlanChangeProposal(id="prop_amara_modify", child_id=CHILD_AMARA, therapist_id=THERAPIST_HANNAH,
                           proposal_type=ProposalType.MODIFY, status=ProposalStatus.PENDING_PARENT_ACCEPTANCE,
                           proposed_activity_version_id=V_PENDING_HANNAH,
                           rationale="Drafted while the invitation is still pending.", environment=ENV),
        PlanChangeProposal(id="prop_rue_modify", child_id=CHILD_RUE, therapist_id=THERAPIST_HANNAH,
                           proposal_type=ProposalType.MODIFY, status=ProposalStatus.CANCELLED,
                           proposed_activity_version_id=V_ENDED_HANNAH,
                           rationale="Left over from an ended connection.", environment=ENV),
    ]


def parent_notes() -> List[ParentNote]:
    return [
        ParentNote(id="pn_maya_1", child_id=CHILD_MAYA, parent_id=PARENT_ELENA, note_type=ParentNoteType.UPDATE,
                   body="Maya said 'more bubbles' on her own!", review_status=ParentNoteReviewStatus.NEW,
                   session_preparation_status=SessionPreparationStatus.NONE,
                   linked_assignment_id="assign_maya_bubbles", linked_activity_title="Bubble requesting",
                   created_at="2026-07-25", environment=ENV),
        ParentNote(id="pn_maya_2", child_id=CHILD_MAYA, parent_id=PARENT_ELENA, note_type=ParentNoteType.QUESTION,
                   body="Is it ok if she signs instead of saying the word?",
                   review_status=ParentNoteReviewStatus.REVIEWED,
                   session_preparation_status=SessionPreparationStatus.DISCUSS_AT_NEXT_SESSION,
                   linked_assignment_id="assign_maya_turntake", linked_activity_title="Turn-taking with a ball",
                   created_at="2026-07-24", environment=ENV),
        ParentNote(id="pn_noah_1", child_id=CHILD_NOAH, parent_id=PARENT_ROSA, note_type=ParentNoteType.NOTE,
                   body="Noah gets tired after about 5 minutes.", review_status=ParentNoteReviewStatus.NEW,
                   session_preparation_status=SessionPreparationStatus.NONE, created_at="2026-07-26", environment=ENV),
    ]


def private_therapist_notes() -> List[PrivateTherapistNote]:
    return [
        PrivateTherapistNote(id="ptn_maya_1", child_id=CHILD_MAYA, therapist_id=THERAPIST_HANNAH,
                             body="Consider AAC backup if verbal requests plateau.", marked_for_next_session=True,
                             created_at="2026-07-25", environment=ENV),
        PrivateTherapistNote(id="ptn_noah_1", child_id=CHILD_NOAH, therapist_id=THERAPIST_HANNAH,
                             body="Keep sessions short; watch for fatigue.", marked_for_next_session=False,
                             created_at="2026-07-26", environment=ENV),
    ]
