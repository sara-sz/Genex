"""Slice 9 — the three derived views, and the boundary between two of them.

Canonical design: `pilot_runtime/docs/parent_baseline_v2_design.md`.

## HISTORICAL CLINICAL EVIDENCE vs CURRENT GENEX SERVICEABILITY

These are different kinds of fact and the contract keeps them in different
blocks, because conflating them would let a change in our content library read
as a change in the child.

    clinical_history       HISTORICAL. What the caregiver reported and what
                           canonical target the evidence identified, fixed at
                           finalize. Derived from the stored record alone.

    current_serviceability CURRENT. Whether Genex can serve that target today.
                           Derived from the record AND the reviewed-content
                           state, and stamped with the content digest used.

Adding approved Track 2 content may move serviceability from unsupported to
supported. It must never make it appear that the child's answers changed, that
their canonical target changed, that their floor changed, or that the baseline
was reassessed. Nothing in `clinical_history` depends on `SERVED_FAMILIES`, so
that is a property of the split rather than a promise about it.

Every serviceability result carries `content_set_id` and `content_digest`, so
two determinations that disagree are always attributable to the content state
that produced them rather than to a silent change in the child's record.

## THREE AUDIENCES, THREE VIEWS

    parent_view        a caregiver. Non-diagnostic, no clinical vocabulary.
    clinician_summary  A2 v2.1. Minimal derived state, NO month labels.
    provenance         engineering. Everything, including the reason ledger.

The forbid-lists are not the same, and are enforced by scanning the built
payloads rather than by review:

                              parent   clinician   provenance
    outcome state names         no        yes          yes
    canonical family names      no        yes          yes
    developmental months        no         no          yes
    reason codes (R1..R5)       no         no          yes
    milestone prose             no         no           no
    raw caregiver answers       no         no           no
    skill keys / refs           no         no           no
    scores                      no         no           no

Milestone prose, raw answers, skill keys and scores appear in NO view. The
record holds the prose and the states; nothing derived here needs either, so
the narrowest thing that works is to never build them.

The reason ledger is provenance only. It exists for reproducibility and
debugging — "why did this interview run to nine questions" — and is not
parent-facing clinical information.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from api import baseline_additional as additional
from api import baseline_micro_tracks as micro_tracks
from api import baseline_outcome as outcome_mod
from api import baseline_policy as policy_mod
from api import baseline_profile as profile
from api import baseline_projection_gate as projection_gate

# ---------------------------------------------------------------------------
# parent-facing presentation keys
# ---------------------------------------------------------------------------

#: Presentation keys, NOT clinical states. The frontend owns the copy; these
#: exist so it can choose it without ever seeing `TARGET_UNSUPPORTED_BY_CONTENT`
#: or `INSUFFICIENT_EVIDENCE`, which are system and clinician concepts.
#:
#: Deliberately NOT a reversible encoding of the outcome enum: a caregiver
#: surface that wanted the clinical state could not recover it from these, and
#: that is the point.
PARENT_PLAN_READY = "plan_ready"
PARENT_PLAN_NOT_AVAILABLE_YET = "plan_not_available_yet"
PARENT_NO_FOCUS_IDENTIFIED = "no_focus_identified"
PARENT_NEEDS_MORE_INFORMATION = "needs_more_information"

PARENT_STATES: Tuple[str, ...] = (
    PARENT_PLAN_READY, PARENT_PLAN_NOT_AVAILABLE_YET,
    PARENT_NO_FOCUS_IDENTIFIED, PARENT_NEEDS_MORE_INFORMATION,
)

#: The one mapping, total over the four outcomes.
#:
#: `TARGET_UNSUPPORTED_BY_CONTENT` becomes "no plan available yet" rather than
#: "no focus identified". Saying we have nothing to offer yet is a statement
#: about Genex; saying nothing was identified would be a false statement about
#: the child.
_PARENT_STATE_FOR: Dict[str, str] = {
    outcome_mod.OUTCOME_TARGET_SUPPORTED: PARENT_PLAN_READY,
    outcome_mod.OUTCOME_TARGET_UNSUPPORTED_BY_CONTENT:
        PARENT_PLAN_NOT_AVAILABLE_YET,
    outcome_mod.OUTCOME_NO_TARGET_IDENTIFIED: PARENT_NO_FOCUS_IDENTIFIED,
    outcome_mod.OUTCOME_INSUFFICIENT_EVIDENCE: PARENT_NEEDS_MORE_INFORMATION,
}

# ---------------------------------------------------------------------------
# per-area derived state, for the clinician view
# ---------------------------------------------------------------------------

AREA_CONCERN_OBSERVED = "concern_observed"
AREA_UNCERTAIN = "uncertain"
AREA_NO_CONCERN_OBSERVED = "no_concern_observed"
AREA_NOT_ASSESSED_BUDGET = "not_assessed_budget_consumed"
AREA_NOT_ASSESSED_NO_SKILL = "not_assessed_no_age_appropriate_skill"

AREA_STATES: Tuple[str, ...] = (
    AREA_CONCERN_OBSERVED, AREA_UNCERTAIN, AREA_NO_CONCERN_OBSERVED,
    AREA_NOT_ASSESSED_BUDGET, AREA_NOT_ASSESSED_NO_SKILL,
)

#: Tokens that must not appear anywhere in a PARENT payload.
PARENT_FORBIDDEN_TOKENS: Tuple[str, ...] = (
    "TARGET_SUPPORTED", "TARGET_UNSUPPORTED_BY_CONTENT",
    "NO_TARGET_IDENTIFIED", "INSUFFICIENT_EVIDENCE",
    "R1_", "R2_", "R3_", "R4_", "R5_",
    "unserved", "unmappable", "unsupported_families",
    "months", "target_band", "floor_band", "rung", "milestone",
    "skill_key", "subdomain", "reason_code", "score",
)

#: Tokens that must not appear anywhere in a CLINICIAN payload. Narrower than
#: the parent list — outcome states and family names ARE clinician concepts.
#: A STRICT SUBSET of the parent list, asserted as such. If the two ever came
#: apart, one of them was written without reference to the other.
CLINICIAN_FORBIDDEN_TOKENS: Tuple[str, ...] = (
    "months", "target_band", "floor_band", "rung", "milestone",
    "skill_key", "subdomain", "reason_code", "score",
    "R1_", "R2_", "R3_", "R4_", "R5_",
)


class BaselineContractError(Exception):
    """A derived view could not be built. PHI-safe: no child data."""

    PHI_SAFE_MESSAGE = True


# ---------------------------------------------------------------------------
# the two blocks
# ---------------------------------------------------------------------------

def clinical_history(record) -> Dict[str, Any]:
    """HISTORICAL. The finalized evidence and the target it identified.

    Reads the record only. Nothing here consults `SERVED_FAMILIES`, so no
    content change can move any value in this block — which is the whole point
    of separating it.
    """
    result = outcome_mod.derive_outcome(record)
    return {
        "basis": "finalized_baseline_evidence",
        "outcome": result.outcome,
        "target_identified": result.target_band is not None,
        "target_band": result.target_band,
        "floor_band": result.floor_band,
        "floor_established": result.floor_established,
        "insufficient_because": result.insufficient_because,
        "policy_id": record.policy_id,
        "policy_digest": record.policy_digest,
        "baseline_version": record.baseline_version,
    }


def current_serviceability(record) -> Dict[str, Any]:
    """CURRENT. Whether Genex can serve the identified target TODAY.

    Stamped with the content set that produced the determination. Two
    determinations that disagree are then attributable to the content state
    rather than to anything about the child.
    """
    decision = projection_gate.projection_decision(record)
    return {
        "basis": "current_reviewed_content",
        "projectable": decision.allowed,
        "support": decision.target_support,
        "unsupported_families": list(decision.unsupported_families),
        "unmappable_skills": decision.unmappable_skills,
        "withheld_because": decision.withheld_because,
        "content_set_id": decision.served_family_set_id,
        "content_digest": decision.served_family_set_digest,
    }


# ---------------------------------------------------------------------------
# per-area derivation
# ---------------------------------------------------------------------------

def area_state(record, profile_area: str, *,
               policy: policy_mod.BaselinePolicy) -> str:
    """One of the five derived area states. Precedence is deliberate.

    Concern first, then uncertainty, then the two not-assessed causes, and
    `no_concern_observed` only when the area was genuinely sampled and showed
    neither. `not_assessed` is never collapsed into `no_concern_observed` — the
    one substitution that would make a gap in our questioning read as
    reassurance about the child.
    """
    cause = profile.not_assessed_cause(record, profile_area)
    if cause == profile.NOT_ASSESSED_BUDGET_CONSUMED:
        return AREA_NOT_ASSESSED_BUDGET
    if cause == profile.NOT_ASSESSED_NO_AGE_APPROPRIATE_SKILL:
        return AREA_NOT_ASSESSED_NO_SKILL
    if profile_area in additional.concerning_areas(record, policy=policy):
        return AREA_CONCERN_OBSERVED
    if profile_area in outcome_mod.uncertain_areas(record):
        return AREA_UNCERTAIN
    return AREA_NO_CONCERN_OBSERVED


def profile_block(record, *, policy: policy_mod.BaselinePolicy
                  ) -> Dict[str, Any]:
    """Per-area derived state plus the multi-area result. No months, no counts.

    Counts are omitted deliberately: "two probes" invites arithmetic about how
    thoroughly an area was examined, and the five states already say everything
    a clinician can act on.
    """
    areas = {area: area_state(record, area, policy=policy)
             for area in profile.AREA_SUBDOMAIN}
    multi_area = outcome_mod.multi_area_state(record, policy=policy)
    return {
        "areas": areas,
        "multi_area": multi_area,
        "multi_area_indeterminate_reasons": list(
            outcome_mod.multi_area_indeterminate_reasons(record,
                                                         policy=policy)),
        "required_areas": list(policy.required_profile_areas),
    }


# ---------------------------------------------------------------------------
# the three views
# ---------------------------------------------------------------------------

def parent_view(record, *, policy: Optional[
        policy_mod.BaselinePolicy] = None) -> Dict[str, Any]:
    """Caregiver-facing. Non-diagnostic, and carries no clinical vocabulary.

    No outcome state name, no family name, no month, no reason code. The
    frontend chooses qualitative copy from `state`; this contract deliberately
    ships no sentences, because the copy is a product decision and a backend
    that hardcoded it would make every wording change a deploy.
    """
    policy = policy or policy_mod.default_policy()
    result = outcome_mod.derive_outcome(record)
    decision = projection_gate.projection_decision(record)
    state = _PARENT_STATE_FOR[result.outcome]
    if state not in PARENT_STATES:      # pragma: no cover - defensive
        raise BaselineContractError("unmapped parent presentation state")
    return {
        "state": state,
        # A plan can be produced right now. The only boolean a caregiver
        # surface needs, and it says nothing about why not.
        "plan_available": decision.allowed and result.has_target,
        "answers_recorded": len(record.skills) + len(record.profile_probes),
    }


def clinician_summary(record, *, policy: Optional[
        policy_mod.BaselinePolicy] = None) -> Dict[str, Any]:
    """A2 v2.1. Minimal derived state, with the two bases kept separate.

    NO month labels, by instruction: a clinician reading this needs the
    outcome, whether Genex can serve it, and the area picture. The band itself
    is provenance, and a developmental-month label beside a child's name is the
    `dev_age` mistake in a new costume.

    So `clinical_history` is included WITHOUT its band fields. The target's
    existence is reported; its month is not. That is sufficient for the
    sentence this view exists to support — a clinically appropriate target was
    identified, and there is no approved pilot content for it.
    """
    policy = policy or policy_mod.default_policy()
    history = clinical_history(record)
    return {
        "clinical_history": {
            "basis": history["basis"],
            "outcome": history["outcome"],
            "target_identified": history["target_identified"],
            "floor_established": history["floor_established"],
            "insufficient_because": history["insufficient_because"],
        },
        "current_serviceability": current_serviceability(record),
        "profile": profile_block(record, policy=policy),
    }


def provenance(record, *, policy: Optional[
        policy_mod.BaselinePolicy] = None) -> Dict[str, Any]:
    """Engineering. Everything derived, including the reason ledger.

    Not a product surface. It exists so a question count can be explained and
    a decision reproduced, which the two audience views deliberately cannot do.
    """
    policy = policy or policy_mod.default_policy()
    return {
        "clinical_history": clinical_history(record),
        "current_serviceability": current_serviceability(record),
        "profile": profile_block(record, policy=policy),
        "baseline_summary": outcome_mod.baseline_summary(record,
                                                         policy=policy),
        "additional_question_ledger": [dict(entry)
                                       for entry in record.additional_reasons],
        "interview": additional.additional_summary(record, policy=policy),
    }


# ---------------------------------------------------------------------------
# the leak gates, callable so tests and CI share one definition
# ---------------------------------------------------------------------------

def forbidden_tokens_in(payload: Any, tokens: Tuple[str, ...]) -> List[str]:
    """Every forbidden token present in a payload's KEYS or string VALUES."""
    import json

    blob = json.dumps(payload)
    return [token for token in tokens if token in blob]


def assert_parent_safe(payload: Any) -> None:
    found = forbidden_tokens_in(payload, PARENT_FORBIDDEN_TOKENS)
    if found:
        raise BaselineContractError(
            f"parent payload exposes system concepts: {found}")


def assert_clinician_safe(payload: Any) -> None:
    found = forbidden_tokens_in(payload, CLINICIAN_FORBIDDEN_TOKENS)
    if found:
        raise BaselineContractError(
            f"clinician payload exposes internal data: {found}")


__all__ = [
    "PARENT_PLAN_READY", "PARENT_PLAN_NOT_AVAILABLE_YET",
    "PARENT_NO_FOCUS_IDENTIFIED", "PARENT_NEEDS_MORE_INFORMATION",
    "PARENT_STATES",
    "AREA_CONCERN_OBSERVED", "AREA_UNCERTAIN", "AREA_NO_CONCERN_OBSERVED",
    "AREA_NOT_ASSESSED_BUDGET", "AREA_NOT_ASSESSED_NO_SKILL", "AREA_STATES",
    "PARENT_FORBIDDEN_TOKENS", "CLINICIAN_FORBIDDEN_TOKENS",
    "BaselineContractError",
    "clinical_history", "current_serviceability", "area_state",
    "profile_block", "parent_view", "clinician_summary", "provenance",
    "forbidden_tokens_in", "assert_parent_safe", "assert_clinician_safe",
]
