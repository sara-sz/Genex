"""api/baseline_eligibility.py — above-age interpretation and target eligibility.

Slice 4 of the founder-approved 6/9 Baseline v2 redesign. See the canonical
design note: `pilot_runtime/docs/parent_baseline_v2_design.md`.

## THE RULE

    Above-age questions may discover STRENGTHS.
    Failure on a future skill must never manufacture a DEFICIT.

Ascent is capped at `chronological + lookahead`, so a rung between the child's
age and that guardrail can legitimately be asked. A `no` there means "not yet,
and not expected yet" — it is not evidence of delay and must not become a
concern or a goal. Without this rule the guardrail would turn a courtesy probe
for advanced ability into a manufactured deficit for every average child.

## THIS CHANGES NO STATE. IT ADDS A SEPARATE PREDICATE LAYER.

`_classify` still maps the five answers to the four states, unchanged and
untouched. What this module adds is three DERIVED questions asked of recorded
evidence:

    target_eligible   may this become a clinical goal?
    counts_as_concern does this belong in the multi-area tally?
    is_strength       is this worth telling the clinician as a positive?

Recorded state stays invariant, which is what keeps the policy's budget numbers
unable to change meaning: a skill does not become demonstrated, and
`not_assessed` does not become evidence, because the budget moved.

## THE PREDICATE KEYS ON CHRONOLOGICAL AGE, NOT ON THE POLICY

`chronological_months` is session data the caregiver supplied. The lookahead is
a policy value and governs ROUTING only — which rungs may be asked. Changing
`+6` to `+9` changes what gets asked; it cannot change what any recorded answer
means. Keeping the two apart is deliberate: if eligibility keyed on the policy
number, a budget change would silently re-interpret stored evidence.

## `unknown` IS UNCHANGED

`not_sure` remains uncertainty everywhere: never a target, never a concern, at
or above age. It is the one state this module does not special-case, because it
already behaves correctly.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from genex_core.functional_baseline import (
    DEMONSTRATED,
    EMERGING,
    NOT_DEMONSTRATED,
    UNKNOWN,
)

#: The four recorded states, named here for readability only. This module does
#: not define them and must never redefine them.
STATE_DEMONSTRATED = "demonstrated"
STATE_EMERGING = "emerging"
STATE_NOT_DEMONSTRATED = "not_demonstrated"
STATE_UNKNOWN = "unknown"

#: States that make a skill a clinical target candidate AT OR BELOW age.
#: `unknown` is deliberately absent: an unanswerable question is not evidence
#: a skill is missing.
UNRESOLVED_STATES: Tuple[str, ...] = (STATE_EMERGING, STATE_NOT_DEMONSTRATED)

#: How an above-age answer is reported. Not a state — a label for the clinician
#: summary, so "ahead of age and not yet there" never reads as a deficit.
ABOVE_AGE_STRENGTH = "above_age_strength"
ABOVE_AGE_EMERGING = "above_age_emerging"
ABOVE_AGE_NOT_YET = "above_age_not_yet"
ABOVE_AGE_UNCERTAIN = "above_age_uncertain"


def is_above_age(months: int, chronological_months: int) -> bool:
    """Is this rung beyond the child's chronological age?

    Strictly greater. A rung AT the child's age is an age-appropriate
    expectation, so a `no` there is ordinary evidence and must stay eligible.
    """
    return int(months) > int(chronological_months)


def target_eligible(state: str, months: int,
                    chronological_months: int) -> bool:
    """May this evidence become a clinical goal?

    Two independent disqualifications:

      * `unknown` — the question was put and could not be answered, which is
        evidence about the asking, not about the skill;
      * above age — a future skill not yet present is not a deficit.

    Everything else unresolved at or below age is eligible.
    """
    if state not in UNRESOLVED_STATES:
        return False
    if is_above_age(months, chronological_months):
        return False
    return True


def counts_as_concern(state: str, months: int,
                      chronological_months: int) -> bool:
    """Does this evidence belong in the multi-area concern tally?

    Identical disqualifications to `target_eligible`, and deliberately so: an
    answer that cannot justify a goal must not inflate a profile either, or the
    multi-area signal would be driven by questions asked above age.
    """
    return target_eligible(state, months, chronological_months)


def is_strength(state: str, months: int, chronological_months: int) -> bool:
    """A demonstrated skill ABOVE age — worth reporting as a positive."""
    return (state == STATE_DEMONSTRATED
            and is_above_age(months, chronological_months))


def above_age_label(state: str, months: int,
                    chronological_months: int) -> Optional[str]:
    """How an above-age answer should be described, or `None` if at/below age."""
    if not is_above_age(months, chronological_months):
        return None
    if state == STATE_DEMONSTRATED:
        return ABOVE_AGE_STRENGTH
    if state == STATE_EMERGING:
        return ABOVE_AGE_EMERGING
    if state == STATE_NOT_DEMONSTRATED:
        return ABOVE_AGE_NOT_YET
    return ABOVE_AGE_UNCERTAIN


# ---------------------------------------------------------------------------
# applied to a record
# ---------------------------------------------------------------------------

def eligible_unresolved(record) -> Tuple[Any, ...]:
    """Recorded evidence that may drive a goal. Ordered deterministically."""
    chrono = int(record.chronological_months)
    out = [e for e in record.skills.values()
           if target_eligible(e.state, e.months, chrono)]
    out.sort(key=lambda e: (e.months, e.subdomain, e.milestone))
    return tuple(out)


def strengths(record) -> Tuple[Any, ...]:
    chrono = int(record.chronological_months)
    out = [e for e in record.skills.values()
           if is_strength(e.state, e.months, chrono)]
    out.sort(key=lambda e: (e.months, e.subdomain, e.milestone))
    return tuple(out)


def eligible_target_band(record) -> Optional[int]:
    """The lowest band holding ELIGIBLE unresolved evidence.

    Distinct from `baseline_traversal.candidate_target_band`, which reports the
    lowest unresolved band as the Pilot's `select_target_band` would — the raw
    bracket. This one additionally requires the band to hold evidence that may
    actually become a goal, so an above-age band whose only deficits are future
    skills is not offered as a target.

    Both are kept, and the difference between them is a real finding worth
    surfacing rather than a discrepancy to hide: it means the traversal
    bracketed something that must not be acted on.
    """
    bands = sorted({e.months for e in eligible_unresolved(record)})
    return bands[0] if bands else None


def eligibility_summary(record) -> Dict[str, Any]:
    """Counts only. No milestone text, no child data, no month judgement."""
    chrono = int(record.chronological_months)
    above = [e for e in record.skills.values()
             if is_above_age(e.months, chrono)]
    return {
        "assessed": len(record.skills),
        "above_age_assessed": len(above),
        "above_age_labels": sorted(
            {above_age_label(e.state, e.months, chrono) for e in above}),
        "eligible_unresolved": len(eligible_unresolved(record)),
        "strengths": len(strengths(record)),
        "eligible_target_band": eligible_target_band(record),
    }


def assert_answer_vocabulary_unchanged() -> None:
    """Guard: this module must never fork the answer vocabulary.

    It imports the four frozen sets rather than restating them, and this
    asserts they still hold the values the predicates assume. A new answer
    value added upstream fails here instead of silently falling through
    `target_eligible` as ineligible.
    """
    if DEMONSTRATED != frozenset({"yes"}):
        raise AssertionError("the demonstrated vocabulary moved")
    if EMERGING != frozenset({"sometimes", "with_help"}):
        raise AssertionError("the emerging vocabulary moved")
    if NOT_DEMONSTRATED != frozenset({"no"}):
        raise AssertionError("the not-demonstrated vocabulary moved")
    if UNKNOWN != frozenset({"not_sure", ""}):
        raise AssertionError("the unknown vocabulary moved")


__all__ = [
    "ABOVE_AGE_EMERGING",
    "ABOVE_AGE_NOT_YET",
    "ABOVE_AGE_STRENGTH",
    "ABOVE_AGE_UNCERTAIN",
    "STATE_DEMONSTRATED",
    "STATE_EMERGING",
    "STATE_NOT_DEMONSTRATED",
    "STATE_UNKNOWN",
    "UNRESOLVED_STATES",
    "above_age_label",
    "assert_answer_vocabulary_unchanged",
    "counts_as_concern",
    "eligibility_summary",
    "eligible_target_band",
    "eligible_unresolved",
    "is_above_age",
    "is_strength",
    "target_eligible",
]
