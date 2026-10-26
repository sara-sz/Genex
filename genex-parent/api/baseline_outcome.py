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

    CONFIRMED       >=2 sampled areas show an age-appropriate concern.
                    Takes precedence UNCONDITIONALLY — two observed concerns
                    are sufficient positive evidence, and nothing missing
                    elsewhere can erase them.
    NOT_OBSERVED    <2 concerns AND the required evidence can support that
                    negative: every required area sampled, none of the
                    non-concerning ones left uncertain.
    INDETERMINATE   <2 concerns and missing or uncertain evidence could still
                    hold a second one.

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

#: WHY it is indeterminate — a SET, because several gaps can coexist and a
#: clinician needs to know which. The tri-state stays a tri-state.
#:
#: Reported only when fewer than two concerns were observed. Once two are, the
#: positive evidence stands on its own and there is nothing to be indeterminate
#: about.
INDETERMINATE_BUDGET_CONSUMED = "budget_consumed"
INDETERMINATE_NO_AGE_APPROPRIATE_SKILL = "no_age_appropriate_skill"
INDETERMINATE_UNCERTAINTY = "uncertainty"

INDETERMINATE_REASONS: Tuple[str, ...] = (
    INDETERMINATE_BUDGET_CONSUMED,
    INDETERMINATE_NO_AGE_APPROPRIATE_SKILL,
    INDETERMINATE_UNCERTAINTY,
)


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

def _indeterminate_reasons(record, *, policy: policy_mod.BaselinePolicy,
                           concerning: Tuple[str, ...]) -> Tuple[str, ...]:
    """Gaps in the REQUIRED set that could still hide a second concern.

    Scoped to the required areas, deliberately, and asymmetrically with
    `CONFIRMED`: positive evidence counts from ANY sampled area, while the
    negative conclusion is gated on the area set the policy actually requires.
    Letting a never-required fallback area block `NOT_OBSERVED` would make that
    state unreachable in practice, which would be a different way of refusing
    to say anything.

    An area that ALREADY shows concern contributes no reason even if it holds
    an `unknown`: it is counted, and more certainty there cannot change whether
    a SECOND area is concerning.
    """
    reasons = set()
    for area in policy.required_profile_areas:
        cause = profile.not_assessed_cause(record, area)
        if cause == profile.NOT_ASSESSED_BUDGET_CONSUMED:
            reasons.add(INDETERMINATE_BUDGET_CONSUMED)
        elif cause == profile.NOT_ASSESSED_NO_AGE_APPROPRIATE_SKILL:
            reasons.add(INDETERMINATE_NO_AGE_APPROPRIATE_SKILL)
        elif area not in concerning and any(
                e.state == v2.STATE_UNKNOWN
                for e in profile.area_evidence(record, area)):
            # SAMPLED, not concerning, and the caregiver could not answer. The
            # area could still turn out concerning, so this cannot support a
            # reassuring negative — the gap an earlier draft missed by looking
            # only at whether an area had been sampled at all.
            reasons.add(INDETERMINATE_UNCERTAINTY)
    return tuple(r for r in INDETERMINATE_REASONS if r in reasons)


def multi_area_state(record, *, policy: policy_mod.BaselinePolicy) -> str:
    """CONFIRMED / NOT_OBSERVED / INDETERMINATE — never a boolean.

    ## CONFIRMED FIRST, UNCONDITIONALLY

    Two distinct areas with an observed age-appropriate concern is sufficient
    positive evidence, and nothing missing elsewhere can take it away. A child
    under 36 months with a receptive concern and a functional concern is
    CONFIRMED even though `conversation_narrative` begins at 36 months and the
    social area can never be sampled for them.

    Missing evidence must prevent a reassuring NEGATIVE conclusion. It must not
    erase a positive one. Those are different jobs, and an earlier draft of
    this docstring conflated them by claiming younger children could never
    leave INDETERMINATE — the code was right; the sentence was not.

    ## THEN INDETERMINATE, THEN NOT_OBSERVED

    With fewer than two concerns observed, `NOT_OBSERVED` is a claim about the
    child and may only be made when the required evidence can support it: every
    required area sampled, and none of the non-concerning ones left uncertain.
    Anything else is INDETERMINATE with its reasons.
    """
    concerning = additional.concerning_areas(record, policy=policy)
    if len(concerning) >= 2:
        return MULTI_AREA_CONFIRMED
    if _indeterminate_reasons(record, policy=policy, concerning=concerning):
        return MULTI_AREA_INDETERMINATE
    return MULTI_AREA_NOT_OBSERVED


def multi_area_indeterminate_reasons(record, *,
                                     policy: policy_mod.BaselinePolicy
                                     ) -> Tuple[str, ...]:
    """Every reason `multi_area` is INDETERMINATE; empty when it is not.

    A tuple rather than one value: budget, inventory and uncertainty gaps
    coexist routinely, and collapsing them would hide which ones a further
    question could close.
    """
    concerning = additional.concerning_areas(record, policy=policy)
    if len(concerning) >= 2:
        return ()
    return _indeterminate_reasons(record, policy=policy, concerning=concerning)


def uncertain_areas(record) -> Tuple[str, ...]:
    """Sampled areas holding an `unknown`. Distinct from unsampled."""
    return tuple(
        area for area in profile.AREA_SUBDOMAIN
        if any(e.state == v2.STATE_UNKNOWN
               for e in profile.area_evidence(record, area)))


# ---------------------------------------------------------------------------
# the whole summary
# ---------------------------------------------------------------------------

def _policy_for(record, policy):
    """The given policy, or the registered default for THIS record.

    `default_policy` takes `(baseline_version, domain)` — it is per-domain, not
    global — so resolving it needs the record. An earlier revision called it
    with no arguments; every test passed a policy explicitly, so the branch was
    never executed and the TypeError sat there unexercised. Resolved from the
    record here, in one place, so there is one answer to "which policy applies
    to this baseline".
    """
    if policy is not None:
        return policy
    return policy_mod.default_policy(record.baseline_version, record.domain)


def baseline_summary(record, *, policy: Optional[
        policy_mod.BaselinePolicy] = None) -> Dict[str, Any]:
    """Everything a clinician needs, assembled. PHI-safe.

    No milestone text, no skill keys, no raw caregiver answers — bands, counts,
    states and reason codes only. The Parent-facing subset is Slice 9's
    decision; this is the full derived picture.
    """
    policy = _policy_for(record, policy)
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
        "multi_area_indeterminate_reasons": list(
            multi_area_indeterminate_reasons(record, policy=policy)),
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
    "INDETERMINATE_UNCERTAINTY", "INDETERMINATE_REASONS",
    "multi_area_indeterminate_reasons",
    "BaselineOutcomeError", "OutcomeResult",
    "target_band_support", "derive_outcome", "multi_area_state",
    "uncertain_areas", "baseline_summary",
]
