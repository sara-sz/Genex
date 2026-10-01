"""pilot_backend/coding/rules.py — the October 2026 treatment-management rules.

Deterministic, versioned, explainable, regenerable, and NEVER authoritative.
No model, no payer logic, no reimbursement arithmetic, no medical-necessity
determination. The output is a set of POTENTIAL candidates for a clinician to
confirm or reject.

## What this computes, and what it emphatically does not

It answers one narrow factual question: do the DOCUMENTED minutes and the
DOCUMENTED real-time interactive communication match the time pattern these
treatment-management codes describe?

It does not answer whether anything is billable, reimbursable, claim-ready,
eligible or approved. Those words do not appear in any output of this module,
and a test asserts they never do. Device/technology eligibility is a separate
question that is UNRESOLVED (`RTMTechnology.regulatory_status` is
`UNDER_REVIEW`), so `TECHNOLOGY_STATUS_UNRESOLVED` accompanies every candidate
set this slice can produce — a rule-pattern match is not an eligibility
conclusion, and the two are kept visibly apart.

## The 2026 rule subset

    98979   first 10 minutes          10 <= minutes <= 19
    98980   first 20 minutes          minutes >= 20
    98981   each ADDITIONAL completed 20-minute increment after 98980's 20

All three additionally require at least one documented real-time interactive
communication with the patient and/or caregiver during the calendar month.

    minutes      candidates
    0            none
    1-9          none
    10-19        98979 x1
    20-39        98980 x1
    40-59        98980 x1, 98981 x1
    60-79        98980 x1, 98981 x2

`98981` units are `(minutes - 20) // 20` — integer division, so an incomplete
additional increment NEVER rounds up. 98979 is mutually exclusive with both
98980 and 98981, and 98981 cannot appear without 98980; both invariants are
enforced by a post-check on the produced set rather than only by the branch
structure, so a future edit to the branches cannot quietly violate them.

Device-supply codes are out of scope and have no entry here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Optional, Tuple

#: Stored on every generated summary. A historical summary must stay
#: reproducible after the rules change, which is only possible if the record
#: says which rules produced it.
CODING_RULE_SET_ID = "rtm-treatment-management-2026"
CODING_RULE_VERSION = "v1"

#: The ONLY codes this slice may propose. Device-supply codes are absent.
SUPPORTED_CODES = ("98979", "98980", "98981")

_FIRST_TIER_MIN = 10
_FIRST_TIER_MAX = 19
_SECOND_TIER_MIN = 20
_ADDITIONAL_INCREMENT = 20


class CodingRuleError(ValueError):
    """Invalid coding input. PHI-safe."""

    PHI_SAFE_MESSAGE = True


class MissingRequirement(str, Enum):
    """A factual condition that is absent. Versioned enum, never free text."""

    INSUFFICIENT_DOCUMENTED_MINUTES = "insufficient_documented_minutes"
    NO_REAL_TIME_INTERACTIVE_COMMUNICATION = "no_real_time_interactive_communication"
    TECHNOLOGY_STATUS_UNRESOLVED = "technology_status_unresolved"
    PERIOD_NOT_FINALIZED = "period_not_finalized"
    CLINICIAN_CONFIRMATION_REQUIRED = "clinician_confirmation_required"


class ConfirmationStatus(str, Enum):
    NOT_REVIEWED = "not_reviewed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class CodeCandidate:
    """One POTENTIAL code and its unit count. Never an authorisation."""

    code: str
    units: int
    #: Rule-level explanation. Factual and free of billing language.
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.code not in SUPPORTED_CODES:
            raise CodingRuleError(f"unsupported code: {self.code}")
        if self.units < 1:
            raise CodingRuleError("a candidate must have at least one unit")


@dataclass(frozen=True)
class CodingInputs:
    """The documented facts the rules read. Nothing else reaches them."""

    documented_management_minutes: int
    real_time_interactive_communication_present: bool
    technology_eligibility_established: bool = False
    period_finalized: bool = False

    def __post_init__(self) -> None:
        minutes = self.documented_management_minutes
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            raise CodingRuleError("documented minutes must be a whole number")
        if minutes < 0:
            raise CodingRuleError("documented minutes cannot be negative")


@dataclass(frozen=True)
class CodingOutcome:
    """What the rules concluded, and why."""

    candidates: Tuple[CodeCandidate, ...]
    missing_requirements: Tuple[MissingRequirement, ...]
    rule_explanations: Tuple[str, ...]
    rule_set_id: str = CODING_RULE_SET_ID
    rule_version: str = CODING_RULE_VERSION

    @property
    def has_candidates(self) -> bool:
        return bool(self.candidates)

    def units_for(self, code: str) -> int:
        return sum(c.units for c in self.candidates if c.code == code)


def _validate_invariants(candidates: Tuple[CodeCandidate, ...]) -> None:
    """Re-check the mutual-exclusion rules on the PRODUCED set.

    Deliberately redundant with the branch structure. The branches are easy to
    edit and hard to review; this check states the invariant once, in terms of
    the output, so a future rearrangement that produced 98979 alongside 98980
    would fail here rather than ship.
    """
    codes = {c.code for c in candidates}
    if "98979" in codes and (codes & {"98980", "98981"}):
        raise CodingRuleError(
            "98979 is mutually exclusive with 98980 and 98981")
    if "98981" in codes and "98980" not in codes:
        raise CodingRuleError("98981 cannot be suggested without 98980")
    for code in codes:
        if code not in SUPPORTED_CODES:  # pragma: no cover - defensive
            raise CodingRuleError(f"unsupported code produced: {code}")


def evaluate(inputs: CodingInputs) -> CodingOutcome:
    """Apply the 2026 treatment-management rules. Pure and deterministic."""
    minutes = inputs.documented_management_minutes
    missing: list = []
    explanations: list = []
    candidates: Tuple[CodeCandidate, ...] = ()

    has_interaction = inputs.real_time_interactive_communication_present
    if not has_interaction:
        missing.append(MissingRequirement.NO_REAL_TIME_INTERACTIVE_COMMUNICATION)
        explanations.append(
            "No documented real-time interactive communication with the "
            "patient and/or caregiver during the calendar month.")

    if minutes < _FIRST_TIER_MIN:
        missing.append(MissingRequirement.INSUFFICIENT_DOCUMENTED_MINUTES)
        explanations.append(
            f"Documented treatment-management time is {minutes} minutes; the "
            f"first time tier begins at {_FIRST_TIER_MIN} minutes.")

    if has_interaction and minutes >= _FIRST_TIER_MIN:
        if minutes <= _FIRST_TIER_MAX:
            candidates = (CodeCandidate(
                "98979", 1,
                f"Documented time {minutes} minutes falls in the "
                f"{_FIRST_TIER_MIN}-{_FIRST_TIER_MAX} minute first-tier "
                "pattern, with documented real-time interactive "
                "communication."),)
            explanations.append(
                "First-tier time pattern matched; 98980 and 98981 are not "
                "applicable at this documented time.")
        else:
            additional = (minutes - _SECOND_TIER_MIN) // _ADDITIONAL_INCREMENT
            produced = [CodeCandidate(
                "98980", 1,
                f"Documented time {minutes} minutes meets or exceeds the "
                f"{_SECOND_TIER_MIN}-minute pattern, with documented "
                "real-time interactive communication.")]
            if additional >= 1:
                produced.append(CodeCandidate(
                    "98981", additional,
                    f"{additional} additional completed "
                    f"{_ADDITIONAL_INCREMENT}-minute increment(s) after the "
                    f"first {_SECOND_TIER_MIN} minutes. Incomplete "
                    "increments are not counted."))
            else:
                explanations.append(
                    f"No additional completed {_ADDITIONAL_INCREMENT}-minute "
                    "increment; incomplete increments are never rounded up.")
            candidates = tuple(produced)

    # Technology eligibility is a SEPARATE question and is unresolved. It is
    # flagged whether or not the time pattern matched, so a candidate set is
    # never read as an eligibility conclusion.
    if not inputs.technology_eligibility_established:
        missing.append(MissingRequirement.TECHNOLOGY_STATUS_UNRESOLVED)
        explanations.append(
            "Technology/device eligibility remains unresolved. A time and "
            "interactive-communication pattern match is not an RTM "
            "eligibility conclusion.")

    if not inputs.period_finalized:
        missing.append(MissingRequirement.PERIOD_NOT_FINALIZED)
        explanations.append(
            "The monitoring period is not finalized; documented evidence may "
            "still change.")

    # Always present. Clinician confirmation of all RTM requirements is
    # required before any coding or billing decision.
    missing.append(MissingRequirement.CLINICIAN_CONFIRMATION_REQUIRED)
    explanations.append(
        "Potential candidates only. Clinician confirmation of all RTM "
        "requirements is required before coding or billing.")

    _validate_invariants(candidates)
    return CodingOutcome(
        candidates=candidates,
        missing_requirements=tuple(missing),
        rule_explanations=tuple(explanations),
    )


#: Registry so a historical summary can be re-evaluated under the rules it was
#: generated with, rather than under whatever is current.
_RULE_SETS: Mapping[Tuple[str, str], object] = {
    (CODING_RULE_SET_ID, CODING_RULE_VERSION): evaluate,
}


def rule_set_for(rule_set_id: str, rule_version: str):
    """Resolve a versioned rule set, refusing an unknown one.

    Fails closed. Silently substituting the current rules would misreport
    what a historical summary was actually produced by.
    """
    try:
        return _RULE_SETS[(rule_set_id, rule_version)]
    except KeyError:
        raise CodingRuleError(
            f"unknown coding rule set: {rule_set_id}/{rule_version}") from None


def known_rule_sets() -> Tuple[Tuple[str, str], ...]:
    return tuple(sorted(_RULE_SETS))
