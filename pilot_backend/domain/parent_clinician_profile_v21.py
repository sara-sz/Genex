"""A2 v2.1 — the derived clinician profile, and Parent's content evaluation.

Two records, deliberately, because they are two different kinds of fact:

    ClinicianProfileV21     DERIVED CLINICAL CONTEXT. Parent is the authority,
                            because the broad-profile evidence it comes from
                            never crosses the boundary and the Pilot therefore
                            cannot reconstruct it.

    ParentContentEvaluation PROVENANCE about Parent's own content mirror. It
                            exists so a disagreement with the Pilot's approved
                            bank is diagnosable, and for nothing else.

Hiding the second inside the first was the obvious shortcut and is wrong: a
clinician reading a profile would be reading a claim about our content library
as if it were a claim about the child.

## WHY THIS IS NOT A FIELD ON THE PROJECTION

`ParentBaselineProjectionV2` is the object goal generation consumes. Putting
profile state on it would hand the generator a legitimate reference to profile
state, and "profile information cannot influence target selection" would become
a promise instead of a property of the type. It is a separate record linked by
the same source identity, and
`test_goal_generation_eligibility_boundary` keeps the generation path free of
every name used here.

## WHAT MAY TRAVEL, AND WHAT MAY NOT

Only derived STATE: per-area concern/uncertain/not-assessed, the not-assessed
cause, the multi-area verdict and its reasons. The record refuses anything
else at construction rather than trusting its caller — no raw answers, no
milestone prose, no developmental months, no skill refs, no scores, no reason
ledger, no Channel-B probes.

A developmental month is the one worth naming explicitly: it has no legitimate
place in a clinician summary, and a month-valued integer arriving here would be
the `dev_age` mistake crossing a system boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Tuple

from .entities import utc_now

#: The version string for this record. A sibling of the v2 projection schema,
#: never a replacement for it.
PROFILE_SCHEMA_V21 = "parent-clinician-profile-v21"
CONTENT_EVALUATION_SCHEMA_V21 = "parent-content-evaluation-v21"

#: Derived per-area state. Exactly the approved vocabulary.
AREA_CONCERN_OBSERVED = "concern_observed"
AREA_UNCERTAIN = "uncertain"
AREA_NO_CONCERN_OBSERVED = "no_concern_observed"
AREA_NOT_ASSESSED = "not_assessed"

AREA_STATES: Tuple[str, ...] = (AREA_CONCERN_OBSERVED, AREA_UNCERTAIN,
                                AREA_NO_CONCERN_OBSERVED, AREA_NOT_ASSESSED)

#: Why an area was not assessed. Only meaningful for `not_assessed`.
CAUSE_BUDGET_CONSUMED = "budget_consumed"
CAUSE_NO_AGE_APPROPRIATE_SKILL = "no_age_appropriate_skill"

NOT_ASSESSED_CAUSES: Tuple[str, ...] = (CAUSE_BUDGET_CONSUMED,
                                        CAUSE_NO_AGE_APPROPRIATE_SKILL)

MULTI_AREA_CONFIRMED = "CONFIRMED"
MULTI_AREA_NOT_OBSERVED = "NOT_OBSERVED"
MULTI_AREA_INDETERMINATE = "INDETERMINATE"

MULTI_AREA_STATES: Tuple[str, ...] = (MULTI_AREA_CONFIRMED,
                                      MULTI_AREA_NOT_OBSERVED,
                                      MULTI_AREA_INDETERMINATE)

INDETERMINATE_REASONS: Tuple[str, ...] = ("budget_consumed",
                                          "no_age_appropriate_skill",
                                          "uncertainty")

#: The four Parent baseline outcomes. Carried as derived context; it is NOT
#: what authorises generation — that is the Pilot's content authority plus the
#: Pilot's own target resolution.
OUTCOME_STATES: Tuple[str, ...] = ("TARGET_SUPPORTED",
                                   "TARGET_UNSUPPORTED_BY_CONTENT",
                                   "NO_TARGET_IDENTIFIED",
                                   "INSUFFICIENT_EVIDENCE")

#: Parent's view of whether its identified target is servable. Advisory.
SERVICEABILITY_STATES: Tuple[str, ...] = ("supported", "unsupported",
                                          "not_applicable")

#: Area identifiers may only be short lowercase tokens. Not free text, so no
#: prose can arrive under an area key.
MAX_AREA_KEY_CHARS = 32
MAX_AREAS = 8

#: Every forbidden concept, by the name it would arrive under. Checked over the
#: whole decoded structure rather than trusted, because this record is built
#: from another system's request body.
FORBIDDEN_KEY_FRAGMENTS: Tuple[str, ...] = (
    "month", "milestone", "skill_key", "skill_ref", "rung", "answer",
    "raw", "score", "probe", "reason_code", "ledger", "subdomain",
    "dev_age", "band",
)

#: Canonical developmental months. A value equal to any of these, anywhere, is
#: refused: the one leak this record exists to make impossible.
CANONICAL_MONTHS = frozenset({12, 15, 18, 24, 30, 36, 48, 60})


class ClinicianProfileV21Error(Exception):
    """A v2.1 profile was refused. PHI-safe: no child data in messages."""

    PHI_SAFE_MESSAGE = True


def _reject_forbidden(payload: Any, *, what: str) -> None:
    """Refuse any forbidden key name or month-valued integer, recursively."""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            lowered = str(key).lower()
            for fragment in FORBIDDEN_KEY_FRAGMENTS:
                if fragment in lowered:
                    raise ClinicianProfileV21Error(
                        f"{what} carries a forbidden field: {fragment}")
            # A NUMERIC key is a month wearing a different hat: `{"24": ...}`
            # carries the figure without using any forbidden word for it.
            if lowered.isdigit():
                raise ClinicianProfileV21Error(
                    f"{what} carries a numeric key, which is a month in "
                    f"disguise")
            _reject_forbidden(value, what=what)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            _reject_forbidden(value, what=what)
    elif isinstance(payload, int) and not isinstance(payload, bool):
        if payload in CANONICAL_MONTHS:
            raise ClinicianProfileV21Error(
                f"{what} carries a developmental month value")


@dataclass(frozen=True)
class ParentContentEvaluation:
    """Parent's own content verdict, as PROVENANCE. Never authority.

    Separate from the clinician profile because it is a statement about the
    Genex content library at a moment, not about the child. The digest is what
    makes a Parent/Pilot disagreement attributable rather than mysterious.
    """

    content_set_id: str
    content_digest: str
    target_serviceability: str
    evaluation_schema: str = CONTENT_EVALUATION_SCHEMA_V21

    def __post_init__(self) -> None:
        if not (self.content_set_id or "").strip():
            raise ClinicianProfileV21Error(
                "a content evaluation must name its content set")
        if len(self.content_digest or "") != 64:
            raise ClinicianProfileV21Error(
                "content_digest must be a sha256 hex digest")
        if self.target_serviceability not in SERVICEABILITY_STATES:
            raise ClinicianProfileV21Error(
                "target_serviceability is outside the approved vocabulary")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "evaluation_schema": self.evaluation_schema,
            "content_set_id": self.content_set_id,
            "content_digest": self.content_digest,
            "target_serviceability": self.target_serviceability,
        }


@dataclass(frozen=True)
class ClinicianProfileV21:
    """Immutable derived clinical context for one finalized Parent baseline.

    Linked to the child and to the SAME source identity the v2 projection
    carries — `source_session_id` plus `source_record_digest` — so a clinician
    surface can join the two without the profile living inside the object goal
    generation reads.
    """

    child_id: str
    source_session_id: str
    source_record_digest: str
    domain: str
    outcome: str
    areas: Mapping[str, str]
    not_assessed_causes: Mapping[str, str] = field(default_factory=dict)
    multi_area: str = MULTI_AREA_INDETERMINATE
    multi_area_indeterminate_reasons: Tuple[str, ...] = ()
    received_at: datetime = field(default_factory=utc_now)
    profile_schema: str = PROFILE_SCHEMA_V21

    def __post_init__(self) -> None:
        if len(self.source_record_digest or "") != 64:
            raise ClinicianProfileV21Error(
                "source_record_digest must be a sha256 hex digest")
        if self.outcome not in OUTCOME_STATES:
            raise ClinicianProfileV21Error(
                "outcome is outside the approved vocabulary")
        if self.multi_area not in MULTI_AREA_STATES:
            raise ClinicianProfileV21Error(
                "multi_area is outside the approved vocabulary")
        if not self.areas:
            raise ClinicianProfileV21Error(
                "a clinician profile with no area state says nothing")
        if len(self.areas) > MAX_AREAS:
            raise ClinicianProfileV21Error("too many profile areas")

        for area, state in self.areas.items():
            if not str(area).strip() or len(str(area)) > MAX_AREA_KEY_CHARS:
                raise ClinicianProfileV21Error("an area key is malformed")
            if state not in AREA_STATES:
                raise ClinicianProfileV21Error(
                    "an area state is outside the approved vocabulary")

        for area, cause in self.not_assessed_causes.items():
            if area not in self.areas:
                raise ClinicianProfileV21Error(
                    "a not-assessed cause names an unknown area")
            if self.areas[area] != AREA_NOT_ASSESSED:
                # A cause on an ASSESSED area would be nonsense a clinician
                # could reasonably read as a hedge about real evidence.
                raise ClinicianProfileV21Error(
                    "a not-assessed cause was given for an assessed area")
            if cause not in NOT_ASSESSED_CAUSES:
                raise ClinicianProfileV21Error(
                    "a not-assessed cause is outside the approved vocabulary")

        for reason in self.multi_area_indeterminate_reasons:
            if reason not in INDETERMINATE_REASONS:
                raise ClinicianProfileV21Error(
                    "an indeterminate reason is outside the approved "
                    "vocabulary")
        if self.multi_area != MULTI_AREA_INDETERMINATE and \
                self.multi_area_indeterminate_reasons:
            raise ClinicianProfileV21Error(
                "indeterminate reasons were given for a determinate verdict")
        if self.multi_area == MULTI_AREA_INDETERMINATE and \
                not self.multi_area_indeterminate_reasons:
            raise ClinicianProfileV21Error(
                "an indeterminate verdict must say why")

        # Last, over the whole decoded structure: no forbidden name and no
        # month-valued integer anywhere.
        _reject_forbidden(
            {"areas": dict(self.areas),
             "not_assessed_causes": dict(self.not_assessed_causes),
             "multi_area_indeterminate_reasons":
                 list(self.multi_area_indeterminate_reasons)},
            what="a clinician profile")

    def as_dict(self) -> Dict[str, Any]:
        """PHI-safe. States, causes and verdicts only."""
        return {
            "profile_schema": self.profile_schema,
            "child_id": self.child_id,
            "source_session_id": self.source_session_id,
            "source_record_digest": self.source_record_digest,
            "domain": self.domain,
            "outcome": self.outcome,
            "areas": dict(self.areas),
            "not_assessed_causes": dict(self.not_assessed_causes),
            "multi_area": self.multi_area,
            "multi_area_indeterminate_reasons": list(
                self.multi_area_indeterminate_reasons),
        }


__all__ = [
    "PROFILE_SCHEMA_V21", "CONTENT_EVALUATION_SCHEMA_V21",
    "AREA_CONCERN_OBSERVED", "AREA_UNCERTAIN", "AREA_NO_CONCERN_OBSERVED",
    "AREA_NOT_ASSESSED", "AREA_STATES",
    "CAUSE_BUDGET_CONSUMED", "CAUSE_NO_AGE_APPROPRIATE_SKILL",
    "NOT_ASSESSED_CAUSES",
    "MULTI_AREA_CONFIRMED", "MULTI_AREA_NOT_OBSERVED",
    "MULTI_AREA_INDETERMINATE", "MULTI_AREA_STATES",
    "INDETERMINATE_REASONS", "OUTCOME_STATES", "SERVICEABILITY_STATES",
    "FORBIDDEN_KEY_FRAGMENTS", "CANONICAL_MONTHS",
    "ClinicianProfileV21Error", "ParentContentEvaluation",
    "ClinicianProfileV21",
]
