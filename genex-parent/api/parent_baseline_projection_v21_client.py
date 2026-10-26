"""A2 v2.1 — the Parent-side sibling client. v2 is untouched.

## WHAT v2.1 ADDS, AND WHY PARENT IS THE ONE THAT CAN SEND IT

Two top-level keys beside v2's five:

    clinician_profile          derived per-area state, the multi-area verdict
                               and its reasons
    parent_content_evaluation  Parent's content digest and its own
                               serviceability verdict, as PROVENANCE

Parent is the authority for the first because it is derived from Channel-B
profile evidence, which deliberately never crosses the boundary — so the Pilot
cannot reconstruct it, however much it would like to.

Parent is NOT the authority for the second. It is a mirror of the Pilot's
approved content state, sent so a disagreement is diagnosable. The Pilot
re-checks against its own bank immediately before it writes, and its verdict
governs.

## THE TWO ARE SEPARATE KEYS, NOT ONE NESTED OBJECT

Folding the content evaluation into the clinician profile was the obvious
shortcut. It would mean a clinician reading a profile was reading a claim about
our content library as if it were a claim about the child. They are different
kinds of fact and the wire format says so.

## NOTHING RAW TRAVELS

`clinician_profile` carries derived STATE only. The builder constructs it by
NAME from the Parent-side contract view rather than copying a dict, so a field
added to that view cannot start travelling by accident — the same discipline
`projection_payload` uses for the v2 clinical parts. No raw answers, no
milestone prose, no developmental months, no skill keys, no scores, no reason
ledger, no Channel-B probes.

The Pilot's own codec refuses every one of those again on arrival. Two
independent refusals, because this is the boundary where a mistake would be
hardest to notice.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

from api import baseline_contract
from api import baseline_micro_tracks as micro_tracks
from api import baseline_outcome as outcome_mod
from api import baseline_policy as policy_mod
from api import baseline_projection_gate as projection_gate
from api.parent_baseline_projection_v2_client import (
    BODY_KEYS,
    ProjectionRejected,
    canonical_source_digest,
)

#: The v2.1 body, exactly. v2's five plus the two additions.
BODY_KEYS_V21: Tuple[str, ...] = BODY_KEYS + ("clinician_profile",
                                              "parent_content_evaluation")

#: The clinician profile's keys, exactly. Mirrors the Pilot's `PROFILE_KEYS`.
PROFILE_KEYS: Tuple[str, ...] = (
    "outcome", "areas", "not_assessed_causes", "multi_area",
    "multi_area_indeterminate_reasons",
)

CONTENT_EVALUATION_KEYS: Tuple[str, ...] = (
    "content_set_id", "content_digest", "target_serviceability",
)

#: Parent's serviceability vocabulary, shared with the Pilot's so a
#: reconciliation compares like with like.
SERVICEABILITY_SUPPORTED = "supported"
SERVICEABILITY_UNSUPPORTED = "unsupported"
SERVICEABILITY_NOT_APPLICABLE = "not_applicable"

#: The v2.1 endpoint path. An explicit version boundary — the Pilot dispatches
#: on the route, never on the body shape.
PROJECTION_V21_PATH = "/internal/parent-baseline-projections-v21"

#: Which outcomes have a target whose serviceability is even a question.
_TARGET_OUTCOMES = frozenset(outcome_mod.TARGET_OUTCOMES)


def _serviceability_for(record) -> str:
    """Parent's own verdict, in the shared vocabulary.

    `not_applicable` for the two outcomes with no target: there is nothing
    whose serviceability could be asserted, and reporting `unsupported` there
    would be a claim about content standing in for a claim about evidence.
    """
    result = outcome_mod.derive_outcome(record)
    if result.outcome not in _TARGET_OUTCOMES:
        return SERVICEABILITY_NOT_APPLICABLE
    decision = projection_gate.projection_decision(record)
    return (SERVICEABILITY_SUPPORTED if decision.allowed
            else SERVICEABILITY_UNSUPPORTED)


def build_clinician_profile(record, *, policy: Optional[
        policy_mod.BaselinePolicy] = None) -> Dict[str, Any]:
    """The derived clinician profile, built BY NAME from the contract view."""
    policy = (policy or policy_mod.default_policy(
        record.baseline_version, record.domain))
    summary = baseline_contract.clinician_summary(record, policy=policy)
    areas = dict(summary["profile"]["areas"])

    # The contract view reports the two not-assessed causes as distinct area
    # STATES. The wire format separates "not assessed" from "why", so a
    # consumer can act on the state without parsing the cause out of it.
    states: Dict[str, str] = {}
    causes: Dict[str, str] = {}
    for area, state in areas.items():
        if state == baseline_contract.AREA_NOT_ASSESSED_BUDGET:
            states[area] = "not_assessed"
            causes[area] = "budget_consumed"
        elif state == baseline_contract.AREA_NOT_ASSESSED_NO_SKILL:
            states[area] = "not_assessed"
            causes[area] = "no_age_appropriate_skill"
        else:
            states[area] = state

    return {
        "outcome": summary["clinical_history"]["outcome"],
        "areas": states,
        "not_assessed_causes": causes,
        "multi_area": summary["profile"]["multi_area"],
        "multi_area_indeterminate_reasons": list(
            summary["profile"]["multi_area_indeterminate_reasons"]),
    }


def build_parent_content_evaluation(record) -> Dict[str, Any]:
    """Parent's content mirror, as provenance. Never authority."""
    return {
        "content_set_id": micro_tracks.SERVED_FAMILY_SET_ID,
        "content_digest": micro_tracks.served_family_set_digest(),
        "target_serviceability": _serviceability_for(record),
    }


def build_body_v21(*, source_session_id: str, record: Mapping[str, Any],
                   payload: Mapping[str, Any],
                   decoded_record) -> Dict[str, Any]:
    """The exact seven-key v2.1 request body.

    `record` is the full canonical stored v2 dict and is used ONLY for the
    digest — exactly as in v2, so the source identity of a v2.1 projection is
    computed the same way and a v2 and a v2.1 projection of the same record
    agree about what they came from.

    `decoded_record` is the rebuilt `BaselineRecordV2`, needed because the
    derived profile cannot be read off the stored dict alone.
    """
    missing = [k for k in ("summary", "skills", "band_totals")
               if k not in payload]
    if missing:
        raise ProjectionRejected("the projection payload is incomplete")
    body = {
        "source_session_id": source_session_id,
        "source_record_digest": canonical_source_digest(record),
        "summary": dict(payload["summary"]),
        "skills": [dict(s) for s in payload["skills"]],
        "band_totals": [dict(b) for b in payload["band_totals"]],
        "clinician_profile": build_clinician_profile(decoded_record),
        "parent_content_evaluation": build_parent_content_evaluation(
            decoded_record),
    }
    if set(body) != set(BODY_KEYS_V21):
        raise ProjectionRejected("the v2.1 body key set is wrong")
    return body


__all__ = [
    "BODY_KEYS_V21", "PROFILE_KEYS", "CONTENT_EVALUATION_KEYS",
    "PROJECTION_V21_PATH",
    "SERVICEABILITY_SUPPORTED", "SERVICEABILITY_UNSUPPORTED",
    "SERVICEABILITY_NOT_APPLICABLE",
    "build_clinician_profile", "build_parent_content_evaluation",
    "build_body_v21",
]
