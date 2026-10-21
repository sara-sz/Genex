"""Slice 7 — the four outcome states, and the clinician-facing summary.

Canonical design: `pilot_runtime/docs/parent_baseline_v2_design.md`.

## THE FOUR STATES ARE MUTUALLY EXCLUSIVE AND TOTAL

    TARGET_SUPPORTED               a target, and content exists to work on it
    TARGET_UNSUPPORTED_BY_CONTENT  a target, and no activity family serves it
    NO_TARGET_IDENTIFIED           assessed, and nothing at or below age is
                                   unresolved
    INSUFFICIENT_EVIDENCE          we cannot honestly say

Exactly one holds for any record. The separation that matters clinically is the
first two: an unsupported target is NOT no target. The child has a real,
identified need; what is missing is our content. Collapsing it into
`NO_TARGET_IDENTIFIED` would report a gap in the activity bank as a finding
about the child, and substituting a different served target would treat the
content we happen to have as the clinical question.

## A SPARSE PROFILE NEVER CHANGES THE OUTCOME

`derive_outcome` reads `skills`, `bands_entered` and `chronological_months`.
It does NOT read `profile_probes`, and that is a structural guarantee rather
than a convention — a valid supported target with zero remaining profile
questions is `TARGET_SUPPORTED`, with every profile area honestly
`not_assessed`.

The alternative would be perverse. Channel A legitimately consumes the whole
budget when a floor is hard to establish; measured on coherent profiles that
happens on about a third of interviews. If an empty profile could downgrade the
outcome, the interviews that worked hardest to characterise the target would be
the ones reporting the weakest result.

## NOT_ASSESSED MUST NEVER LOOK REASSURING

`multi_area` is therefore NOT a boolean. `False` would be read as "only one
area is affected", which is a claim about the child; when a required area was
never sampled the honest answer is that we do not know. Three values:

    CONFIRMED       >=2 sampled areas show an age-appropriate concern
    NOT_OBSERVED    every required area was sampled, and <=1 shows concern
    INDETERMINATE   a required area was not sampled at all

And the summary keeps the three distinctions a clinician needs separable:

    areas_with_concern        concern found
    areas_not_assessed        never sampled — split by recoverable/unprobeable
    areas_uncertain           sampled, and the caregiver could not answer

"No additional concerns observed", "other areas not assessed" and "areas
uncertain" are three different sentences, and no field here collapses them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from api import baseline_additional as additional
from api import baseline_eligibility as eligibility
from api import baseline_micro_tracks as micro_tracks
from api import baseline_policy as policy_mod
from api import baseline_profile as profile
from api import baseline_traversal as traversal
from genex_core import functional_baseline_v2 as v2

# ---------------------------------------------------------------------------
# the four outcome states
# ---------------------------------------------------------------------------

OUTCOME_TARGET_SUPPORTED = "TARGET_SUPPORTED"
OUTCOME_TARGET_UNSUPPORTED_BY_CONTENT = "TARGET_UNSUPPORTED_BY_CONTENT"
OUTCOME_NO_TARGET_IDENTIFIED = "NO_TARGET_IDENTIFIED"
OUTCOME_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"

OUTCOME_STATES: Tuple[str, ...] = (
    OUTCOME_TARGET_SUPPORTED,
    OUTCOME_TARGET_UNSUPPORTED_BY_CONTENT,
    OUTCOME_NO_TARGET_IDENTIFIED,
    OUTCOME_INSUFFICIENT_EVIDENCE,
)

#: The two outcomes that carry a clinical target. Only these may project.
TARGET_OUTCOMES: Tuple[str, ...] = (OUTCOME_TARGET_SUPPORTED,
                                    OUTCOME_TARGET_UNSUPPORTED_BY_CONTENT)

#: Why `INSUFFICIENT_EVIDENCE`. Diagnostic, never Parent-facing.
INSUFFICIENT_NO_AGE_APPROPRIATE_ASSESSMENT = "no_age_appropriate_assessment"
INSUFFICIENT_BAND_INCOMPLETE = "band_incomplete"
INSUFFICIENT_ONLY_UNKNOWN = "only_unknown"

# ---------------------------------------------------------------------------
# multi-area
# ---------------------------------------------------------------------------

MULTI_AREA_CONFIRMED = "CONFIRMED"
MULTI_AREA_NOT_OBSERVED = "NOT_OBSERVED"
MULTI_AREA_INDETERMINATE = "INDETERMINATE"

MULTI_AREA_STATES: Tuple[str, ...] = (MULTI_AREA_CONFIRMED,
                                      MULTI_AREA_NOT_OBSERVED,
                                      MULTI_AREA_INDETERMINATE)

#: WHY it is indeterminate. The tri-state stays a tri-state; this says whether
#: the gap is one more question could close.
#:
#: Measured, this matters: `conversation_narrative` begins at 36 months, so for
#: every younger child the required social area is permanently unsampleable and
#: `multi_area` can never leave INDETERMINATE in this release. A clinician
#: reading "indeterminate" needs to know whether that is a budget accident or a
#: standing limit of the question bank.
INDETERMINATE_BUDGET_CONSUMED = "budget_consumed"
INDETERMINATE_NO_AGE_APPROPRIATE_SKILL = "no_age_appropriate_skill"
INDETERMINATE_BOTH = "both"


class BaselineOutcomeError(Exception):
    """An outcome could not be derived. PHI-safe: no child data in messages."""

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class OutcomeResult:
    """One outcome, with the evidence that produced it. No child data."""

    outcome: str
    target_band: Optional[int] = None
    support: Optional[str] = None
    unsupported_families: Tuple[str, ...] = ()
    unmappable_skills: int = 0
    floor_band: Optional[int] = None
    floor_established: bool = False
    insufficient_because: Optional[str] = None

    @property
    def has_target(self) -> bool:
        return self.outcome in TARGET_OUTCOMES


# ---------------------------------------------------------------------------
# content support for a target band
# ---------------------------------------------------------------------------

def target_band_support(record, months: int) -> Dict[str, Any]:
    """Content support for the ELIGIBLE UNRESOLVED skills in a band.

    A band is not one rung. Support is assessed over the skills that actually
    make the band a target — the unresolved, age-eligible ones — because those
    are what a goal would be built from. A demonstrated sibling skill in the
    same band needs no content.

    The band is `SUPPORTED` only if EVERY such skill is, which is the same
    all-families-served rule `rung_support` applies within one rung. One served
    skill beside an unserved one would otherwise let a week practise the half
    we happen to have while reporting the band as covered.
    """
    chronological = int(record.chronological_months)
    states = {micro_tracks.SUPPORTED: 0, micro_tracks.UNSERVED: 0,
              micro_tracks.UNMAPPABLE: 0}
    unsupported_families: List[str] = []
    considered = 0
    for row in v2.band_skill_rows(record, months):
        key = v2.skill_key(record.domain, row["subdomain"], months,
                           row["milestone"])
        evidence = record.skills.get(key)
        if evidence is None:
            continue
        if not eligibility.target_eligible(evidence.state, months,
                                           chronological):
            continue
        considered += 1
        ref = micro_tracks.rung_at(record.area_id, months, row["milestone"])
        if ref is None:
            states[micro_tracks.UNMAPPABLE] += 1
            continue
        support = micro_tracks.rung_support(ref)
        states[support] += 1
        if support != micro_tracks.SUPPORTED:
            unsupported_families.extend(
                f for f in ref.family_bindings
                if f not in micro_tracks.SERVED_FAMILIES)
    if not considered:
        raise BaselineOutcomeError(
            "a target band must hold at least one eligible unresolved skill")
    overall = (micro_tracks.SUPPORTED
               if states[micro_tracks.SUPPORTED] == considered
               else (micro_tracks.UNMAPPABLE
                     if states[micro_tracks.SUPPORTED] == 0
                     and states[micro_tracks.UNSERVED] == 0
                     else micro_tracks.UNSERVED))
    return {
        "support": overall,
        "eligible_unresolved_skills": considered,
        "by_support": dict(states),
        "unsupported_families": tuple(sorted(set(unsupported_families))),
        "unmappable_skills": states[micro_tracks.UNMAPPABLE],
    }


# ---------------------------------------------------------------------------
# the outcome
# ---------------------------------------------------------------------------

def _age_appropriate_evidence(record) -> Tuple[v2.BaselineSkillEvidence, ...]:
    chronological = int(record.chronological_months)
    return tuple(e for e in record.skills.values()
                 if not eligibility.is_above_age(e.months, chronological))


def derive_outcome(record, *, policy: Optional[
        policy_mod.BaselinePolicy] = None) -> OutcomeResult:
    """The one outcome for a record.

    Reads CHANNEL A ONLY — `skills`, `bands_entered`, `chronological_months` —
    plus the content-support derivation. `profile_probes` is never consulted,
    which is what makes "a sparse profile cannot change the outcome" a property
    of the code rather than a promise about it.

    `policy` is accepted for signature symmetry with the rest of the slice and
    is deliberately UNUSED: a budget change must not be able to alter what the
    evidence means.
    """
    floor = traversal.floor_band(record)
    eligible = eligibility.eligible_target_band(record)

    if eligible is not None:
        support = target_band_support(record, eligible)
        outcome = (OUTCOME_TARGET_SUPPORTED
                   if support["support"] == micro_tracks.SUPPORTED
                   else OUTCOME_TARGET_UNSUPPORTED_BY_CONTENT)
        return OutcomeResult(
            outcome=outcome, target_band=eligible,
            support=support["support"],
            unsupported_families=support["unsupported_families"],
            unmappable_skills=support["unmappable_skills"],
            floor_band=floor,
            floor_established=floor is not None and floor < eligible)

    # No eligible target. Distinguishing "nothing is wrong" from "we cannot
    # tell" is the whole of the remaining decision, and it turns on whether the
    # age-appropriate evidence is COMPLETE and FREE OF UNKNOWNS.
    age_appropriate = _age_appropriate_evidence(record)
    if not age_appropriate:
        # Never assessed anything at or below age: an entry choice above age,
        # or a start band that did not fit.
        return OutcomeResult(
            outcome=OUTCOME_INSUFFICIENT_EVIDENCE, floor_band=floor,
            insufficient_because=INSUFFICIENT_NO_AGE_APPROPRIATE_ASSESSMENT)

    incomplete = [m for m in record.bands_entered
                  if not v2.band_assessment(record, m).assessment_complete]
    if incomplete:
        # Measured at zero on every natural stop across 936 + 3072 paths, and
        # kept because "every entered band is complete" is a frozen rule rather
        # than an observation: if it ever broke, the honest report is that we
        # do not know, not that nothing is wrong.
        return OutcomeResult(
            outcome=OUTCOME_INSUFFICIENT_EVIDENCE, floor_band=floor,
            insufficient_because=INSUFFICIENT_BAND_INCOMPLETE)

    if any(e.state == v2.STATE_UNKNOWN for e in age_appropriate):
        # An unanswerable age-appropriate question is not evidence of absence.
        # Without this branch an all-`not_sure` interview would report
        # NO_TARGET_IDENTIFIED, which is the single most misleading thing this
        # module could say.
        return OutcomeResult(
            outcome=OUTCOME_INSUFFICIENT_EVIDENCE, floor_band=floor,
            insufficient_because=INSUFFICIENT_ONLY_UNKNOWN)

    # `floor_established` means ONE thing: a mastered band sits beneath the
    # identified target. With no target there is nothing to bracket, so it is
    # False here even though `floor_band` may report a mastered band. Letting
    # it mean "something was mastered" in this branch and "the deficit is
    # bracketed" in the other would give one field two readings.
    return OutcomeResult(outcome=OUTCOME_NO_TARGET_IDENTIFIED,
                         floor_band=floor, floor_established=False)


# ---------------------------------------------------------------------------
# multi-area derivation
# ---------------------------------------------------------------------------

def multi_area_state(record, *, policy: policy_mod.BaselinePolicy) -> str:
    """CONFIRMED / NOT_OBSERVED / INDETERMINATE — never a boolean.

    `INDETERMINATE` wins over `NOT_OBSERVED` whenever a required area was never
    sampled, including when another area already shows concern but a second one
    is missing. "One area is affected and we checked the others" and "one area
    is affected and we ran out of questions" are different clinical pictures,
    and only the first is reassuring.
    """
    concerning = additional.concerning_areas(record, policy=policy)
    if len(concerning) >= 2:
        return MULTI_AREA_CONFIRMED
    unsampled = [a for a in policy.required_profile_areas
                 if profile.area_status(record, a) == profile.NOT_ASSESSED]
    if unsampled:
        return MULTI_AREA_INDETERMINATE
    return MULTI_AREA_NOT_OBSERVED


def multi_area_indeterminate_because(record, *,
                                     policy: policy_mod.BaselinePolicy
                                     ) -> Optional[str]:
    """Why `multi_area` is INDETERMINATE, or `None` if it is not."""
    if multi_area_state(record, policy=policy) != MULTI_AREA_INDETERMINATE:
        return None
    causes = {profile.not_assessed_cause(record, a)
              for a in policy.required_profile_areas}
    recoverable = profile.NOT_ASSESSED_BUDGET_CONSUMED in causes
    unprobeable = profile.NOT_ASSESSED_NO_AGE_APPROPRIATE_SKILL in causes
    if recoverable and unprobeable:
        return INDETERMINATE_BOTH
    if recoverable:
        return INDETERMINATE_BUDGET_CONSUMED
    return INDETERMINATE_NO_AGE_APPROPRIATE_SKILL


def uncertain_areas(record) -> Tuple[str, ...]:
    """Sampled areas holding an `unknown`. Distinct from unsampled."""
    return tuple(
        area for area in profile.AREA_SUBDOMAIN
        if any(e.state == v2.STATE_UNKNOWN
               for e in profile.area_evidence(record, area)))


# ---------------------------------------------------------------------------
# the whole summary
# ---------------------------------------------------------------------------

def baseline_summary(record, *, policy: Optional[
        policy_mod.BaselinePolicy] = None) -> Dict[str, Any]:
    """Everything a clinician needs, assembled. PHI-safe.

    No milestone text, no skill keys, no raw caregiver answers — bands, counts,
    states and reason codes only. The Parent-facing subset is Slice 9's
    decision; this is the full derived picture.
    """
    policy = policy or policy_mod.default_policy()
    outcome = derive_outcome(record)
    profile_part = profile.profile_summary(record, policy=policy)
    multi_area = multi_area_state(record, policy=policy)
    uncertain = uncertain_areas(record)

    return {
        "outcome": outcome.outcome,
        "target_band": outcome.target_band,
        "target_support": outcome.support,
        "unsupported_families": list(outcome.unsupported_families),
        "unmappable_skills": outcome.unmappable_skills,
        "floor_band": outcome.floor_band,
        "floor_established": outcome.floor_established,
        "insufficient_because": outcome.insufficient_because,

        # Multi-area, as three values. A consumer reading only a boolean here
        # would have to invent one, and the invented one would be wrong.
        "multi_area": multi_area,
        "multi_area_is_determinate": multi_area != MULTI_AREA_INDETERMINATE,
        "multi_area_indeterminate_because":
            multi_area_indeterminate_because(record, policy=policy),
        "areas_with_concern": profile_part["areas_with_concern"],

        # THE THREE DISTINCTIONS, kept separable.
        "areas_assessed": profile_part["areas_assessed"],
        "areas_not_assessed": profile_part["areas_not_assessed"],
        # Cause-partitioned over every area, so the two lists exactly cover
        # `areas_not_assessed`. `recoverable_by_question` is the narrower
        # R3-eligible subset: a required area one more question could sample.
        "areas_not_assessed_recoverable": profile_part["areas_budget_consumed"],
        "areas_recoverable_by_question": profile_part["areas_recoverable"],
        "areas_not_assessed_unprobeable": profile_part["areas_unprobeable"],
        "areas_uncertain": list(uncertain),

        "profile": profile_part["areas"],
        "additional_questions": additional.additional_summary(record,
                                                              policy=policy),
        "policy_id": record.policy_id,
        "served_family_set_id": micro_tracks.SERVED_FAMILY_SET_ID,
        "served_family_set_digest": micro_tracks.served_family_set_digest(),
    }


__all__ = [
    "OUTCOME_TARGET_SUPPORTED", "OUTCOME_TARGET_UNSUPPORTED_BY_CONTENT",
    "OUTCOME_NO_TARGET_IDENTIFIED", "OUTCOME_INSUFFICIENT_EVIDENCE",
    "OUTCOME_STATES", "TARGET_OUTCOMES",
    "INSUFFICIENT_NO_AGE_APPROPRIATE_ASSESSMENT",
    "INSUFFICIENT_BAND_INCOMPLETE", "INSUFFICIENT_ONLY_UNKNOWN",
    "MULTI_AREA_CONFIRMED", "MULTI_AREA_NOT_OBSERVED",
    "MULTI_AREA_INDETERMINATE", "MULTI_AREA_STATES",
    "INDETERMINATE_BUDGET_CONSUMED", "INDETERMINATE_NO_AGE_APPROPRIATE_SKILL",
    "INDETERMINATE_BOTH", "multi_area_indeterminate_because",
    "BaselineOutcomeError", "OutcomeResult",
    "target_band_support", "derive_outcome", "multi_area_state",
    "uncertain_areas", "baseline_summary",
]
