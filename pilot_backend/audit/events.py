"""pilot_backend/audit/events.py — what happened, who did it, and to which record.

An audit log is the one structure that is read precisely when something has
gone wrong, so it has two hard properties: it must be complete enough to
reconstruct an access decision, and it must never itself become a second copy
of the clinical record.

## Metadata is key-allowlisted, not content-filtered

`metadata` accepts only keys in `ALLOWED_METADATA_KEYS`. This is the structural
choice, and it is the opposite of scanning values for clinical-looking text:

  * a denylist fails open — the day someone adds `note_excerpt`, it is logged
    until a reviewer notices;
  * an allowlist fails closed — a new key raises until someone deliberately
    adds it here, in a file whose whole purpose is to be reviewed.

Values are additionally constrained to short scalars. Free clinical text is
long and contains newlines; a 64-character single-line cap will not stop a
determined caller, but it does stop the realistic accident of passing a whole
note, concern or diagnosis through as "context".

## Identifiers are not content

Recording `child_id` is required — an audit trail that cannot say which child
was accessed is not an audit trail. An opaque `chld_<uuid4>` is a reference,
not clinical information, and resolving it needs authorized access to the
system that issued it.

## The timestamp is server-generated

`occurred_at` is stamped here, never accepted from a caller. A client-supplied
audit timestamp is a client-controlled audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Mapping, Optional

from ..domain.ids import new_audit_event_id
from ..domain.roles import ActorRole


class AuditMetadataError(ValueError):
    """Metadata violated the allowlist or the value constraints.

    PHI-safe: the message names the offending KEY, never the value.
    """

    PHI_SAFE_MESSAGE = True


class AuditAction(str, Enum):
    """The actions the pilot can record.

    Most have no business operation behind them in 0.2 — the infrastructure is
    what this phase delivers, not the workflows. They are declared now so that
    0.3 emits an already-reviewed constant instead of inventing a string, and
    so the shape of the eventual audit trail is reviewable today.
    """

    AUTHORIZATION_FAILURE = "authorization_failure"
    AUTHENTICATION_FAILURE = "authentication_failure"
    CHILD_ACCESS_GRANTED = "child_access_granted"
    CHILD_ACCESS_REVOKED = "child_access_revoked"
    CHILD_CREATED = "child_created"
    PROFILE_UPDATED = "profile_updated"
    PROVIDER_CONNECTED = "provider_connected"
    PROVIDER_DISCONNECTED = "provider_disconnected"
    NOTE_CREATED = "note_created"
    NOTE_UPDATED = "note_updated"
    ACTIVITY_CHANGED = "activity_changed"
    EXPORT_CREATED = "export_created"
    ADMIN_CHANGE = "admin_change"
    RECORD_FINALIZED = "record_finalized"
    RECORD_AMENDED = "record_amended"
    # 0.4A longitudinal identity.
    SOURCE_LINK_CREATED = "source_link_created"
    SOURCE_LINK_ENDED = "source_link_ended"
    SOURCE_LINK_REPLACED = "source_link_replaced"
    MANAGING_CLINICIAN_ASSIGNED = "managing_clinician_assigned"
    MANAGING_CLINICIAN_ENDED = "managing_clinician_ended"
    MANAGING_CLINICIAN_TRANSFERRED = "managing_clinician_transferred"
    # 0.4B goals. Suggesting, approving and editing are three DIFFERENT
    # actions, not one "goal changed". Collapsing them would leave the trail
    # unable to answer whether a human ever approved the wording that shipped.
    GOAL_SUGGESTIONS_GENERATED = "goal_suggestions_generated"
    GOAL_SUGGESTION_DECLINED = "goal_suggestion_declined"
    CLINICAL_GOAL_APPROVED = "clinical_goal_approved"
    CAREGIVER_GOAL_APPROVED = "caregiver_goal_approved"
    GOAL_VERSION_ADDED = "goal_version_added"
    GOAL_STATUS_CHANGED = "goal_status_changed"
    # 0.4C monthly focus plan.
    MONTHLY_PLAN_CREATED = "monthly_plan_created"
    MONTHLY_PLAN_ACTIVATED = "monthly_plan_activated"
    MONTHLY_PLAN_CLOSED = "monthly_plan_closed"
    GOAL_ALLOCATED = "goal_allocated"
    GOAL_ALLOCATION_REPRIORITIZED = "goal_allocation_reprioritized"


class AuditResult(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"


#: The complete set of metadata keys an audit event may carry. Every entry is
#: a non-PHI operational fact. Adding one is a deliberate, reviewable edit.
ALLOWED_METADATA_KEYS = frozenset({
    "denial_reason",      # authz.Denial value
    "http_status",        # 200 / 401 / 403
    "environment",        # dev | test | prod
    "actor_practice_id",  # opaque prac_ id
    "connection_id",      # opaque ccxn_/pcxn_ id
    "revision_id",        # opaque revn_ id
    "record_version",     # integer version
    "schema_version",
    "route",              # path TEMPLATE, never a populated path
    "method",
    "source",             # subsystem name
    # 0.4A. All opaque identifiers or short enum values — never an external
    # system's clinical content, and never the external identifier itself,
    # which could be a Parent session id.
    "source_system",      # parent | therapist
    "link_id",            # opaque sslk_ id
    "assignment_id",      # opaque mcas_ id
    "claim_id",           # deterministic claim document id
    "claim_kind",
    "provider_id",        # opaque prov_ id
    "practice_id",        # opaque prac_ id
    # 0.4B/C. Opaque ids, short enums and small integers only.
    #
    # Deliberately EXCLUDED, and tested: goal text, the family-facing template,
    # a suggestion's rendered wording, an edit reason, a domain observation and
    # a milestone reference. Goal text is clinical content about a child, and
    # an edit reason is free text a clinician typed — the audit trail records
    # THAT a goal was approved and by whom, never what it said. `domain_key` is
    # excluded on the same grounds: which developmental domain a child's goal
    # addresses is a clinical fact, not an operational one.
    "suggestion_id",      # opaque gsug_ id
    "goal_kind",          # clinical | caregiver_approved
    "goal_id",            # opaque clgl_/cagl_ id
    "goal_version_id",    # opaque gver_ id
    "goal_status",        # active | paused | retired
    "edit_type",          # accepted_verbatim | modified | replaced | authored_fresh
    "suggestion_count",   # how many candidates were offered
    "focus_plan_id",      # opaque mfpl_ id
    "cycle_month",        # "YYYY-MM" — a calendar month, not a date of service
    "allocation_id",      # opaque galc_ id
    "priority_rank",      # small integer
    "emphasis_weight",    # small integer
    "policy_version",     # planning-policy-YYYY.MM
    "generator_version",  # goal-suggestion-engine-YYYY.MM
    "rule_version",       # suggestion-rules-YYYY.MM
    "plan_state",         # draft | active | closed
})

_MAX_METADATA_VALUE_LENGTH = 64


def _validate_metadata(metadata: Mapping[str, object]) -> Mapping[str, str]:
    validated = {}
    for key, value in (metadata or {}).items():
        if key not in ALLOWED_METADATA_KEYS:
            raise AuditMetadataError(f"metadata key not permitted in audit events: {key}")
        if isinstance(value, bool) or isinstance(value, int):
            validated[key] = str(value)
            continue
        if not isinstance(value, str):
            raise AuditMetadataError(f"metadata value for {key} must be a short scalar")
        if len(value) > _MAX_METADATA_VALUE_LENGTH:
            raise AuditMetadataError(f"metadata value for {key} exceeds the scalar length limit")
        if "\n" in value or "\r" in value:
            raise AuditMetadataError(f"metadata value for {key} must be single-line")
        validated[key] = value
    return validated


@dataclass(frozen=True)
class AuditEvent:
    """One recorded action. Immutable once built."""

    event_id: str
    occurred_at: datetime
    action: AuditAction
    result: AuditResult
    resource_type: str
    #: Opaque application id of the thing acted on. None for actions with no
    #: single subject (a failed authentication has no resource).
    resource_id: Optional[str] = None
    child_id: Optional[str] = None
    #: The authenticated actor's APPLICATION id. None only when authentication
    #: itself failed and no principal was ever resolved.
    actor_application_id: Optional[str] = None
    #: Provider-issued subject. Retained because an audit reader investigating
    #: a compromised credential needs the credential, not just the record.
    actor_auth_subject: Optional[str] = None
    actor_role: Optional[ActorRole] = None
    request_id: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)
    schema_version: str = "october-pilot-0.2"

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _validate_metadata(self.metadata))
        if self.occurred_at.tzinfo is None:
            raise AuditMetadataError("audit timestamps must be timezone-aware")
        if not (self.resource_type or "").strip():
            raise AuditMetadataError("audit events must name a resource type")

    @staticmethod
    def build(action: AuditAction, result: AuditResult, resource_type: str, *,
              resource_id: Optional[str] = None,
              child_id: Optional[str] = None,
              actor_application_id: Optional[str] = None,
              actor_auth_subject: Optional[str] = None,
              actor_role: Optional[ActorRole] = None,
              request_id: str = "",
              metadata: Optional[Mapping[str, object]] = None,
              now: Optional[datetime] = None) -> "AuditEvent":
        """Construct an event with a SERVER-generated id and timestamp.

        `now` is injectable for deterministic tests only; there is no parameter
        by which a request could supply either value.
        """
        return AuditEvent(
            event_id=new_audit_event_id(),
            occurred_at=now or datetime.now(timezone.utc),
            action=action,
            result=result,
            resource_type=resource_type,
            resource_id=resource_id,
            child_id=child_id,
            actor_application_id=actor_application_id,
            actor_auth_subject=actor_auth_subject,
            actor_role=actor_role,
            request_id=request_id,
            metadata=dict(metadata or {}),
        )

    def with_metadata(self, **extra: object) -> "AuditEvent":
        merged = dict(self.metadata)
        merged.update(extra)
        return replace(self, metadata=merged)
