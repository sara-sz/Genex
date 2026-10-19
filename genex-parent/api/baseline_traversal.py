"""api/baseline_traversal.py — Channel-A target-finding traversal.

Slice 3 of the founder-approved 6/9 Baseline v2 redesign. See the canonical
design note: `pilot_runtime/docs/parent_baseline_v2_design.md`.

## IT COMPOSES FROZEN PRIMITIVES; IT DOES NOT REPLACE THEM

`band_skill_rows`, `band_assessment` and `ladder` are used exactly as they are.
`next_question_v2` is NOT modified and NOT called: it carries the old stop rule
(a bracket ends the interview, capped by `MAX_BANDS`) which the audit showed
can finish after two questions on singleton bands and run to nine elsewhere.
This planner is a second, policy-driven traversal beside it, in the pattern v1
and v2 already follow — the frozen one stays readable and its tests keep
passing.

## THE FIVE RULES

  1. ANCHOR from the entry choice, honoured even above the guardrail so an
     explicitly reported advanced ability gets verified. The anchor is routing
     and never evidence: nothing below it is assumed demonstrated.
  2. ASCENT is capped at the guardrail — `chronological + lookahead` — so a
     somewhat advanced child is noticed without being routinely asked
     substantially future skills.
  3. DESCENT is unbounded by age. A five-year-old may need two-month skills
     assessed, and clamping to chronological age would make that impossible.
  4. FLOOR is a MASTERED band below the candidate. `unknown` cannot establish
     it, because an unanswerable question is not evidence a skill is present.
  5. BUDGET is enforced ONLY at complete-band boundaries: a band is never
     entered unless the whole band fits.

Rule 5 is the one that exists because of a measured failure. Truncating a band
mid-way leaves an entered band incomplete; that record still PROJECTS
successfully, and then `select_target_band` raises `BandAssessmentIncomplete`
at the first incomplete band, which the transport renders as a constant 403.
The clinician would see an opaque refusal with no way to tell why. Enforcing at
band boundaries makes that unreachable by construction rather than by care.

## THE CANDIDATE TARGET IS THE LOWEST UNRESOLVED ENTERED BAND

Chosen to match `select_target_band` on the Pilot side, which scans ascending
and returns the first unresolved band. If this module picked differently, the
outcome state it reports would not predict what generation will actually do —
and a state that disagrees with the downstream reality is worse than no state.

## WHAT THIS MODULE DOES NOT DECIDE

It plans WHICH canonical rung to ask next and when to stop. It never decides
what an answer means: no classification, no completeness rule, no concern rule
lives here. It reads `band_assessment`, which is frozen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from genex_core.functional_baseline import _nearest_rung
from genex_core.functional_baseline_v2 import (
    band_assessment,
    band_skill_rows,
    ladder,
    skill_key,
)

# -- step kinds -------------------------------------------------------------

ASK = "ask"
STOP = "stop"

# -- terminal reasons -------------------------------------------------------

#: A complete, unresolved band with a mastered floor below it. The one reason
#: that yields an actionable candidate.
TARGET_FOUND = "target_found"
#: Unresolved at the very bottom of the declared ladder: there is no lower band
#: to establish a floor with, so the bottom rung IS the floor.
TARGET_AT_LADDER_BOTTOM = "target_at_ladder_bottom"
#: Every entered band mastered and nothing further within the guardrail.
NO_TARGET_WITHIN_GUARDRAIL = "no_target_within_guardrail"
#: A deficit exists but no mastered floor was established within budget.
FLOOR_UNESTABLISHED = "floor_unestablished"
#: Mastered, something above exists, but the next band does not fit.
CEILING_BUDGET_EXHAUSTED = "ceiling_budget_exhausted"
#: The band is complete and not mastered, yet holds no unresolved skill — so
#: every non-demonstrated answer in it was `unknown`.
ONLY_UNKNOWN = "only_unknown"
#: The starting band is wider than the whole target-finding budget.
START_BAND_DOES_NOT_FIT = "start_band_does_not_fit"

TERMINAL_REASONS: Tuple[str, ...] = (
    TARGET_FOUND, TARGET_AT_LADDER_BOTTOM, NO_TARGET_WITHIN_GUARDRAIL,
    FLOOR_UNESTABLISHED, CEILING_BUDGET_EXHAUSTED, ONLY_UNKNOWN,
    START_BAND_DOES_NOT_FIT,
)

#: Reasons that mean "a clinically appropriate target was located". Content
#: support is a SEPARATE question, decided in slice 7 — a target is no less
#: appropriate for Genex lacking cards for it.
TARGET_REASONS: Tuple[str, ...] = (TARGET_FOUND, TARGET_AT_LADDER_BOTTOM)


class TraversalError(Exception):
    """The traversal could not be planned. PHI-safe: names no child."""

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class TraversalStep:
    """What to do next: ask one canonical rung, or stop with a reason."""

    kind: str
    question: Optional[Dict[str, Any]] = None
    enters_band: Optional[int] = None
    reason: Optional[str] = None

    @property
    def is_ask(self) -> bool:
        return self.kind == ASK


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------

def band_width(record, months: int) -> int:
    """Declared-track skills in a band. The unit the budget is measured in."""
    return len(band_skill_rows(record, months))


def start_band(record) -> Optional[int]:
    """The band the ENTRY CHOICE selects. Routing only, never evidence.

    `entry_anchor_months` is `None` for the `not_sure` descriptor, where
    chronological age is the most neutral place to begin — and still asserts
    nothing about ability.
    """
    anchor = record.entry_anchor_months
    if anchor is None:
        anchor = record.chronological_months
    subdomains, families = record.track()
    return _nearest_rung(record.domain, int(anchor), subdomains, families)


def ascent_ceiling_band(record, *, lookahead_months: int) -> Optional[int]:
    """The highest band ascent may reach.

    `max(start, nearest(chronological + lookahead))` — the `max` is rule 1: an
    explicitly reported advanced ability is verified at the level the parent
    named, rather than being discarded for sitting above the guardrail. Ascent
    does not continue past the guardrail afterwards.
    """
    subdomains, families = record.track()
    guard = _nearest_rung(record.domain,
                          int(record.chronological_months) + int(lookahead_months),
                          subdomains, families)
    start = start_band(record)
    if guard is None:
        return start
    if start is None:
        return guard
    return max(guard, start)


def _unasked_in_band(record, months: int) -> List[Dict[str, Any]]:
    out = []
    for row in band_skill_rows(record, months):
        key = skill_key(record.domain, row["subdomain"], months,
                        row["milestone"])
        if key not in record.skills:
            out.append(row)
    return out


def _question(record, months: int, row: Dict[str, Any]) -> Dict[str, Any]:
    """The client-facing question shape, identical to the engine's."""
    return {
        "skill_key": skill_key(record.domain, row["subdomain"], months,
                               row["milestone"]),
        "domain": record.domain,
        "subdomain": row["subdomain"],
        "months": int(months),
        "milestone": row["milestone"],
        "parent_explanation": row.get("parent_explanation", ""),
    }


def floor_band(record) -> Optional[int]:
    """The highest entered band that is complete AND mastered, else `None`.

    Rule 4. `band_mastered` requires every skill demonstrated, so an `unknown`
    cannot establish a floor — the predicate gives that for free rather than
    needing a separate check.
    """
    mastered = [m for m in record.bands_entered
                if band_assessment(record, m).band_mastered]
    return max(mastered) if mastered else None


def candidate_target_band(record) -> Optional[int]:
    """The LOWEST entered band that is complete and unresolved.

    Deliberately the same choice `select_target_band` makes on the Pilot side,
    so the state reported here predicts what generation will do.
    """
    for months in sorted(record.bands_entered):
        assessment = band_assessment(record, months)
        if assessment.assessment_complete and assessment.unresolved:
            return months
    return None


# ---------------------------------------------------------------------------
# the planner
# ---------------------------------------------------------------------------

def plan_next(record, *, lookahead_months: int, budget: int) -> TraversalStep:
    """The next Channel-A step. Pure: reads the record, mutates nothing.

    The caller appends `enters_band` to `record.bands_entered` when it acts on
    an `ASK` that opens a new band, so entering a band and asking its first
    question are one atomic decision from the record's point of view.
    """
    spent = len(record.skills)
    rungs = ladder(record)
    if not rungs:
        raise TraversalError("the declared track has no rungs")

    if not record.bands_entered:
        start = start_band(record)
        if start is None:
            raise TraversalError("no starting band could be routed")
        if band_width(record, start) > int(budget):
            # Never enter a band we cannot finish — rule 5, at the first step.
            return TraversalStep(kind=STOP, reason=START_BAND_DOES_NOT_FIT)
        row = band_skill_rows(record, start)[0]
        return TraversalStep(kind=ASK, question=_question(record, start, row),
                             enters_band=start)

    current = record.bands_entered[-1]

    # Finish the entered band BEFORE any vertical movement. This is the frozen
    # v2 correction and the thing the budget must never interrupt.
    unasked = _unasked_in_band(record, current)
    if unasked:
        return TraversalStep(kind=ASK,
                             question=_question(record, current, unasked[0]))

    assessment = band_assessment(record, current)
    ceiling = ascent_ceiling_band(record, lookahead_months=lookahead_months)

    if assessment.band_mastered:
        # DESCENT JUST SUCCEEDED.
        #
        # If a complete, unresolved band is already entered ABOVE this one,
        # then mastering this band is exactly the floor that band was missing,
        # and the traversal is done. Checked before ascent because the planner
        # would otherwise try to climb past the deficit it had just bracketed
        # and — finding the band above already entered — report
        # NO_TARGET_WITHIN_GUARDRAIL while a candidate target plainly existed.
        # Caught by the 36m path report: a deficit at 36m with a mastered 30m
        # floor was being called "no target".
        bracketed = candidate_target_band(record)
        if bracketed is not None and bracketed > current:
            return TraversalStep(kind=STOP, reason=TARGET_FOUND)
        higher = [m for m in rungs if m > current]
        if not higher or (ceiling is not None and higher[0] > ceiling):
            return TraversalStep(kind=STOP, reason=NO_TARGET_WITHIN_GUARDRAIL)
        nxt = higher[0]
        if nxt in record.bands_entered:
            return TraversalStep(kind=STOP, reason=NO_TARGET_WITHIN_GUARDRAIL)
        if spent + band_width(record, nxt) > int(budget):
            return TraversalStep(kind=STOP, reason=CEILING_BUDGET_EXHAUSTED)
        row = band_skill_rows(record, nxt)[0]
        return TraversalStep(kind=ASK, question=_question(record, nxt, row),
                             enters_band=nxt)

    if assessment.unresolved:
        if floor_band(record) is not None and floor_band(record) < current:
            return TraversalStep(kind=STOP, reason=TARGET_FOUND)
        lower = [m for m in rungs if m < current]
        if not lower:
            # Nothing beneath it on the declared ladder: the bottom rung is
            # its own floor, and a deficit there is still a real finding.
            return TraversalStep(kind=STOP, reason=TARGET_AT_LADDER_BOTTOM)
        nxt = lower[-1]
        if nxt in record.bands_entered:
            return TraversalStep(kind=STOP, reason=FLOOR_UNESTABLISHED)
        if spent + band_width(record, nxt) > int(budget):
            # Descent is unbounded by AGE (rule 3) but not by budget. Stopping
            # here is honest: a deficit is known, a floor is not.
            return TraversalStep(kind=STOP, reason=FLOOR_UNESTABLISHED)
        row = band_skill_rows(record, nxt)[0]
        return TraversalStep(kind=ASK, question=_question(record, nxt, row),
                             enters_band=nxt)

    # Complete, not mastered, nothing unresolved: every non-demonstrated answer
    # was `unknown`. Not a target, and not a deficit.
    return TraversalStep(kind=STOP, reason=ONLY_UNKNOWN)


def channel_a_summary(record, *, lookahead_months: int,
                      budget: int) -> Dict[str, Any]:
    """Derived Channel-A state. Diagnostics and slice 7's outcome input."""
    step = plan_next(record, lookahead_months=lookahead_months, budget=budget)
    target = candidate_target_band(record)
    return {
        "questions_spent": len(record.skills),
        "bands_entered": [int(m) for m in record.bands_entered],
        "start_band": start_band(record),
        "ascent_ceiling_band": ascent_ceiling_band(
            record, lookahead_months=lookahead_months),
        "floor_band": floor_band(record),
        "candidate_target_band": target,
        "complete": step.kind == STOP,
        "terminal_reason": step.reason,
        "every_entered_band_complete": all(
            band_assessment(record, m).assessment_complete
            for m in record.bands_entered),
    }


__all__ = [
    "ASK",
    "CEILING_BUDGET_EXHAUSTED",
    "FLOOR_UNESTABLISHED",
    "NO_TARGET_WITHIN_GUARDRAIL",
    "ONLY_UNKNOWN",
    "START_BAND_DOES_NOT_FIT",
    "STOP",
    "TARGET_AT_LADDER_BOTTOM",
    "TARGET_FOUND",
    "TARGET_REASONS",
    "TERMINAL_REASONS",
    "TraversalError",
    "TraversalStep",
    "ascent_ceiling_band",
    "band_width",
    "candidate_target_band",
    "channel_a_summary",
    "floor_band",
    "plan_next",
    "start_band",
]
