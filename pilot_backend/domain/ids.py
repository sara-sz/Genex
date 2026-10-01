"""pilot_backend/domain/ids.py — stable application identifiers.

Every pilot entity is keyed by a random UUID4 with a short type prefix. The ID
is generated, never derived.

## Why not derive IDs from a natural key

The therapist service derives some document ids from a natural key on purpose
(`therapist_api/app/domain/ids.py`) — that is an IDEMPOTENCY mechanism, and it
is correct there: the same logical operation must collide on the same document.

Identity is the opposite problem. An identifier derived from a name or an email
changes when the person changes their name, collides when two people share one,
and leaks the value into logs, URLs and error messages. So these are random and
opaque, and the tests assert that a person's name cannot be recovered from, or
used to predict, their ID.

## Prefixes

The prefix is a readability aid in logs and fixtures, not a parsing contract.
Nothing branches on it; `entity_type_of` exists for diagnostics only.
"""

from __future__ import annotations

import uuid
from typing import Optional

PRACTICE_PREFIX = "prac"
PROVIDER_PREFIX = "prov"
CAREGIVER_PREFIX = "cgvr"
CHILD_PREFIX = "chld"
CAREGIVER_CHILD_CONNECTION_PREFIX = "ccxn"
PROVIDER_CHILD_CONNECTION_PREFIX = "pcxn"
#: BACKEND 0.2. Audit events and record revisions are identified the same way
#: as entities — generated, opaque, never derived from their content. An audit
#: event id derived from what it describes would leak the description.
AUDIT_EVENT_PREFIX = "audt"
REVISION_PREFIX = "revn"
#: PRE-PHI 0.3. The one mutable pilot record.
CHILD_CONTEXT_PREFIX = "cctx"
#: 0.4A longitudinal identity. Claims are NOT listed here: their ids are
#: deterministic by design (see domain/identity_claims.py), which is the whole
#: uniqueness mechanism, so they must never be minted from a uuid.
SOURCE_LINK_PREFIX = "sslk"
MANAGING_CLINICIAN_PREFIX = "mcas"
#: 0.4B goals. A suggestion, a goal and each of the goal's versions are three
#: distinct things and each gets its own identifier. Collapsing the goal and its
#: current version into one id would make "which wording was in force in
#: October?" unanswerable, which is the question the version chain exists for.
GOAL_SUGGESTION_PREFIX = "gsug"
GOAL_VERSION_PREFIX = "gver"
CLINICAL_GOAL_PREFIX = "clgl"
CAREGIVER_APPROVED_GOAL_PREFIX = "cagl"
#: 0.4C monthly focus plan. The plan id is random even though a plan is unique
#: per (child, month): uniqueness is enforced by a deterministic CLAIM document,
#: exactly as in 0.4A, not by making the record's own id guessable. A derived
#: plan id would let anyone holding a child id enumerate that child's months.
MONTHLY_FOCUS_PLAN_PREFIX = "mfpl"
MONTHLY_ALLOCATION_PREFIX = "galc"
MONTHLY_GOAL_SNAPSHOT_PREFIX = "gsnp"
#: 0.4D weekly layer. `WeeklyCycle` is the MONTHLY LAYER's representation of a
#: week — it is not the Parent weekly plan and never shares its identifiers.
#: The Parent plan is reached only through `WeeklyPlanLink.external_plan_id`,
#: which is an external identifier and never canonical, exactly as 0.4A ruled
#: for Parent session ids.
WEEKLY_CYCLE_PREFIX = "wcyc"
WEEKLY_PLAN_LINK_PREFIX = "wlnk"
WEEKLY_PLAN_SNAPSHOT_PREFIX = "wsnp"
ACTIVITY_GOAL_ALIGNMENT_PREFIX = "algn"
COVERAGE_GAP_PREFIX = "cgap"
CAPACITY_LEDGER_PREFIX = "cled"
#: 0.4E evidence and adaptation.
OBSERVATION_EVENT_PREFIX = "obsv"
CUSTOMIZATION_SIGNAL_PREFIX = "csig"
DEFER_RECORD_PREFIX = "dfer"
THERAPIST_INTERVENTION_PREFIX = "invn"
ADAPTATION_RECORD_PREFIX = "adpt"
#: 0.4F RTM evidence. An EPISODE is clinical and may span months; a PERIOD is
#: one calendar month inside it. Two prefixes because they have different
#: lifetimes, and a month end that closed an episode would be exactly the
#: conflation these separate identifiers exist to prevent.
RTM_EPISODE_PREFIX = "repi"
RTM_PERIOD_PREFIX = "rper"
RTM_TECHNOLOGY_PREFIX = "rtec"
THERAPIST_REVIEW_PREFIX = "trev"
CLINICAL_ACTION_PREFIX = "clac"
TIME_ENTRY_PREFIX = "tent"
SYNCHRONOUS_INTERACTION_PREFIX = "sync"
#: 0.4G derived artefacts. REGENERABLE: a fresh id per generation, so
#: regenerating never overwrites the summary a clinician already read.
RTM_EVIDENCE_SUMMARY_PREFIX = "revs"
CODING_ASSISTANCE_PREFIX = "casm"
MONTH_END_REPORT_PREFIX = "mrep"

ALL_PREFIXES = (
    PRACTICE_PREFIX,
    PROVIDER_PREFIX,
    CAREGIVER_PREFIX,
    CHILD_PREFIX,
    CAREGIVER_CHILD_CONNECTION_PREFIX,
    PROVIDER_CHILD_CONNECTION_PREFIX,
    AUDIT_EVENT_PREFIX,
    REVISION_PREFIX,
    CHILD_CONTEXT_PREFIX,
    SOURCE_LINK_PREFIX,
    MANAGING_CLINICIAN_PREFIX,
    GOAL_SUGGESTION_PREFIX,
    GOAL_VERSION_PREFIX,
    CLINICAL_GOAL_PREFIX,
    CAREGIVER_APPROVED_GOAL_PREFIX,
    MONTHLY_FOCUS_PLAN_PREFIX,
    MONTHLY_ALLOCATION_PREFIX,
    MONTHLY_GOAL_SNAPSHOT_PREFIX,
    WEEKLY_CYCLE_PREFIX,
    WEEKLY_PLAN_LINK_PREFIX,
    WEEKLY_PLAN_SNAPSHOT_PREFIX,
    ACTIVITY_GOAL_ALIGNMENT_PREFIX,
    COVERAGE_GAP_PREFIX,
    CAPACITY_LEDGER_PREFIX,
    OBSERVATION_EVENT_PREFIX,
    CUSTOMIZATION_SIGNAL_PREFIX,
    DEFER_RECORD_PREFIX,
    THERAPIST_INTERVENTION_PREFIX,
    ADAPTATION_RECORD_PREFIX,
    RTM_EPISODE_PREFIX,
    RTM_PERIOD_PREFIX,
    RTM_TECHNOLOGY_PREFIX,
    THERAPIST_REVIEW_PREFIX,
    CLINICAL_ACTION_PREFIX,
    TIME_ENTRY_PREFIX,
    SYNCHRONOUS_INTERACTION_PREFIX,
    RTM_EVIDENCE_SUMMARY_PREFIX,
    CODING_ASSISTANCE_PREFIX,
    MONTH_END_REPORT_PREFIX,
)


def new_id(prefix: str) -> str:
    """A fresh opaque identifier: `<prefix>_<uuid4 hex>`."""
    key = (prefix or "").strip()
    if key not in ALL_PREFIXES:
        raise ValueError(f"unknown id prefix {prefix!r}; expected one of {ALL_PREFIXES}")
    return f"{key}_{uuid.uuid4().hex}"


def new_practice_id() -> str:
    return new_id(PRACTICE_PREFIX)


def new_provider_id() -> str:
    return new_id(PROVIDER_PREFIX)


def new_caregiver_id() -> str:
    return new_id(CAREGIVER_PREFIX)


def new_child_id() -> str:
    return new_id(CHILD_PREFIX)


def new_caregiver_child_connection_id() -> str:
    return new_id(CAREGIVER_CHILD_CONNECTION_PREFIX)


def new_provider_child_connection_id() -> str:
    return new_id(PROVIDER_CHILD_CONNECTION_PREFIX)


def new_audit_event_id() -> str:
    return new_id(AUDIT_EVENT_PREFIX)


def new_revision_id() -> str:
    return new_id(REVISION_PREFIX)


def new_child_context_id() -> str:
    return new_id(CHILD_CONTEXT_PREFIX)


def new_source_link_id() -> str:
    return new_id(SOURCE_LINK_PREFIX)


def new_managing_clinician_id() -> str:
    return new_id(MANAGING_CLINICIAN_PREFIX)


def new_goal_suggestion_id() -> str:
    return new_id(GOAL_SUGGESTION_PREFIX)


def new_goal_version_id() -> str:
    return new_id(GOAL_VERSION_PREFIX)


def new_clinical_goal_id() -> str:
    return new_id(CLINICAL_GOAL_PREFIX)


def new_caregiver_goal_id() -> str:
    return new_id(CAREGIVER_APPROVED_GOAL_PREFIX)


def new_focus_plan_id() -> str:
    return new_id(MONTHLY_FOCUS_PLAN_PREFIX)


def new_allocation_id() -> str:
    return new_id(MONTHLY_ALLOCATION_PREFIX)


def new_goal_snapshot_id() -> str:
    return new_id(MONTHLY_GOAL_SNAPSHOT_PREFIX)


def new_weekly_cycle_id() -> str:
    return new_id(WEEKLY_CYCLE_PREFIX)


def new_weekly_plan_link_id() -> str:
    return new_id(WEEKLY_PLAN_LINK_PREFIX)


def new_weekly_plan_snapshot_id() -> str:
    return new_id(WEEKLY_PLAN_SNAPSHOT_PREFIX)


def new_alignment_id() -> str:
    return new_id(ACTIVITY_GOAL_ALIGNMENT_PREFIX)


def new_coverage_gap_id() -> str:
    return new_id(COVERAGE_GAP_PREFIX)


def new_capacity_ledger_id() -> str:
    return new_id(CAPACITY_LEDGER_PREFIX)


def new_observation_event_id() -> str:
    return new_id(OBSERVATION_EVENT_PREFIX)


def new_customization_signal_id() -> str:
    return new_id(CUSTOMIZATION_SIGNAL_PREFIX)


def new_defer_record_id() -> str:
    return new_id(DEFER_RECORD_PREFIX)


def new_therapist_intervention_id() -> str:
    return new_id(THERAPIST_INTERVENTION_PREFIX)


def new_adaptation_record_id() -> str:
    return new_id(ADAPTATION_RECORD_PREFIX)


def new_rtm_episode_id() -> str:
    return new_id(RTM_EPISODE_PREFIX)


def new_rtm_period_id() -> str:
    return new_id(RTM_PERIOD_PREFIX)


def new_rtm_technology_id() -> str:
    return new_id(RTM_TECHNOLOGY_PREFIX)


def new_therapist_review_id() -> str:
    return new_id(THERAPIST_REVIEW_PREFIX)


def new_clinical_action_id() -> str:
    return new_id(CLINICAL_ACTION_PREFIX)


def new_time_entry_id() -> str:
    return new_id(TIME_ENTRY_PREFIX)


def new_synchronous_interaction_id() -> str:
    return new_id(SYNCHRONOUS_INTERACTION_PREFIX)


def new_rtm_evidence_summary_id() -> str:
    return new_id(RTM_EVIDENCE_SUMMARY_PREFIX)


def new_coding_assistance_id() -> str:
    return new_id(CODING_ASSISTANCE_PREFIX)


def new_month_end_report_id() -> str:
    return new_id(MONTH_END_REPORT_PREFIX)


def entity_type_of(identifier: str) -> Optional[str]:
    """The prefix of an identifier, for diagnostics. Never an authorization input."""
    head = str(identifier or "").split("_", 1)[0]
    return head if head in ALL_PREFIXES else None
