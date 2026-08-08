"""Canonical enums for the therapist read-slice (Phase 1A).

These reflect the FINAL approved Hannah-revised product behavior. Independent
concerns are kept as SEPARATE enums (e.g. parent-note review vs. session
preparation) — never collapsed into a single display string.
"""

from __future__ import annotations

from enum import Enum


class PrincipalRole(str, Enum):
    THERAPIST = "therapist"
    PARENT = "parent"


class ConnectionStatus(str, Enum):
    ACTIVE = "active"
    PENDING_PARENT_ACCEPTANCE = "pending_parent_acceptance"
    INVITATION_NOT_ACTIVATED = "invitation_not_activated"
    PAUSED_BY_PARENT = "paused_by_parent"
    ENDED = "ended"


class PlanApprovalStatus(str, Enum):
    NEEDS_PLAN_REVIEW = "needs_plan_review"
    APPROVED = "approved"
    CHANGE_PENDING_PARENT = "change_pending_parent"
    REPLACED = "replaced"
    ARCHIVED = "archived"


class PracticeStatus(str, Enum):
    NOT_TRIED = "not_tried"
    TRIED = "tried"
    TRIED_WITH_HELP = "tried_with_help"
    DID_IT = "did_it"
    LOVED_IT = "loved_it"


class AssignmentStatus(str, Enum):
    """Lifecycle of a plan assignment (separate from approval/practice)."""

    CURRENT = "current"
    RETIRED = "retired"
    PROPOSED = "proposed"
    # Superseded by an accepted proposal. Kept for history: the record remains
    # readable, but it is no longer a current plan item and a replacement
    # assignment carries `replaces_assignment_id` back to it.
    REPLACED = "replaced"


class ProposalType(str, Enum):
    ADD = "add"
    MODIFY = "modify"
    REPLACE = "replace"
    REMOVE = "remove"


class WeeklyPlanStatus(str, Enum):
    """Lifecycle of a weekly plan — the ONLY thing that makes a plan current.

    Currency is an explicit domain fact, never derived. In particular it is NOT
    computed from `week_start_date` against the wall clock (which would make
    behavior time-varying and would classify every fictional plan as historical),
    NOT `max(week_start_date)` (which would make a drafted future week current the
    moment it exists), and NOT query/insertion order (which returns the
    FIRST-SEEDED plan, so a newer plan could never become current).

    Exactly one CURRENT plan per child is the invariant. Zero or several is an
    ambiguous lifecycle that every caller must fail closed on rather than resolve
    by guessing — see `services/weekly_plan.current_weekly_plan`.
    """

    #: The family's presently active weekly plan.
    CURRENT = "current"
    #: A past week, retained for history. Never selectable as current.
    COMPLETED = "completed"
    #: Prepared but not yet active. Never selectable as current, however recent
    #: or future its `week_start_date` is.
    DRAFT = "draft"


class ProposalStatus(str, Enum):
    PENDING_PARENT_ACCEPTANCE = "pending_parent_acceptance"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    CANCELLED = "cancelled"


class ParentNoteType(str, Enum):
    QUESTION = "question"
    NOTE = "note"
    UPDATE = "update"


class ParentNoteReviewStatus(str, Enum):
    NEW = "new"
    REVIEWED = "reviewed"


class SessionPreparationStatus(str, Enum):
    NONE = "none"
    DISCUSS_AT_NEXT_SESSION = "discuss_at_next_session"
    DISCUSSED = "discussed"


class ActivitySaveScope(str, Enum):
    CHILD_ONLY = "child_only"
    THERAPIST_LIBRARY = "therapist_library"
    SUBMITTED_FOR_GENEX_REVIEW = "submitted_for_genex_review"


class CreatedByType(str, Enum):
    GENEX = "genex"
    THERAPIST = "therapist"


# Access levels the read authorization policy can grant for a child.
class ChildAccessLevel(str, Enum):
    FULL = "full"          # active connection -> full workspace
    RESTRICTED = "restricted"  # pending/paused/not-activated -> connection info only
    NONE = "none"          # no connection / ended -> 404 (existence not revealed)


# Connection statuses that grant only restricted (connection-summary) access.
RESTRICTED_CONNECTION_STATUSES = frozenset(
    {
        ConnectionStatus.PENDING_PARENT_ACCEPTANCE,
        ConnectionStatus.INVITATION_NOT_ACTIVATED,
        ConnectionStatus.PAUSED_BY_PARENT,
    }
)
