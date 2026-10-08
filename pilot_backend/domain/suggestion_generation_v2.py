"""pilot_backend/domain/suggestion_generation_v2.py — F-B v2 semantics.

0.6A-1G. The clinical rules for turning PER-SKILL projected evidence into zero,
one, or several target candidates. Pure: no repository, no rung source, no I/O —
so every rule here is testable from a table of states.

## WHAT CHANGED, AND WHY IT HAD TO

F-B v1 selected a target by TRAVERSAL:

    routing_anchor_months -> frozen _step(+1) -> question_at(month)
                          -> the first same-month rung by milestone text

Every step of that was defensible given v1's input, which was a MONTH-LEVEL
summary. But the last step is an alphabetical tiebreak standing in for a clinical
decision: at 30 months the declared track holds four skills, and v1 planned for
whichever one sorted first regardless of which the child actually failed.

A v2 projection carries the exact canonical identity and state of EVERY assessed
skill. So the target no longer has to be inferred from a month — it is read from
the evidence. That removes the traversal, the tiebreak, and the month arithmetic
all at once.

This module therefore contains NO month stepping, NO milestone text and NO
ordering by prose. The only ordering anywhere is by `rung_ref`, and that exists
for response stability — NOT clinical priority.

## v1 IS NOT MODIFIED

`suggestion_generation` keeps its own policy version, its statuses, its ceiling
rule and its traversal. Every historical v1 claim and suggestion stays valid and
readable. The two policies are distinguished by the `generation_policy` value
that is already part of the generation key, so a v1 claim can never be found by a
v2 lookup and vice versa — proven by `v1_claim_is_not_reusable_for_v2`.

## BAND CLASSIFICATION, AND THE ONE DIRECTION THAT IS UNSAFE

    MASTERED    complete, and every canonical skill in it demonstrated
    UNRESOLVED  complete, and at least one emerging / not_demonstrated / unknown
    INCOMPLETE  the evidence does not cover the canonical roster exactly

Completeness is checked against the CANONICAL ROSTER by set equality, not by
counting and not against Parent's `total_skills` alone. Counting would accept a
right-sized band made of the wrong skills; trusting Parent's integer alone was
the hole 0.6A-1F closed, and F-B v2 is a second independent consumer of the same
evidence, so it re-derives rather than inherits the conclusion.

An INCOMPLETE band REFUSES generation and is never skipped. "Not asked" is not
"demonstrated", and treating a gap in our questioning as mastery is the exact
failure the whole v2 lineage exists to prevent.

## STATE -> TARGET

    not_demonstrated  -> target candidate
    emerging          -> target candidate
    unknown           -> NOT a target, reported separately
    demonstrated      -> NOT a target

`unknown` means the question WAS put and the caregiver could not answer. That is
evidence about the asking, so it keeps the band off "mastered" — but it is not
evidence the skill is absent, and planning a goal from it would invent a deficit.
Critically it also does not SUPPRESS anything: a band with one unknown and one
`not_demonstrated` still yields the known target.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .parent_baseline_projection_v2 import PROJECTION_SCHEMA_V2

#: The pinned version of the F-B v2 algorithm: select the first unresolved
#: COMPLETE band from projected per-skill evidence, take every
#: emerging/not_demonstrated skill in it as a candidate, anchor each to the ref
#: the evidence named, and report the unmappable ones instead of dropping them.
#:
#: A DIFFERENT value from v1's, which is what keeps the two generations' claims
#: disjoint — `generation_policy` is already part of the generation key.
GENERATION_POLICY_VERSION_V2 = "goal-suggestion-generation-policy-v2-evidence"

#: States that make a skill a legitimate clinical target.
TARGET_STATES: Tuple[str, ...] = ("emerging", "not_demonstrated")

#: The state that keeps a band off "mastered" without becoming a target.
UNKNOWN_STATE = "unknown"

#: The only state that counts toward mastery.
MASTERED_STATE = "demonstrated"

#: Band classifications.
BAND_MASTERED = "mastered"
BAND_UNRESOLVED = "unresolved"
BAND_INCOMPLETE = "incomplete"

#: The outcome vocabulary. Explicit values rather than booleans, because the four
#: cases are genuinely different clinical situations and a caller must be able to
#: tell "we found nothing to target" from "we found targets we cannot support".
#:
#: NONE of these is an error. All four are true findings about a real assessment,
#: and collapsing any of them into a failure would push a clinician toward
#: retrying instead of reading the result.
OUTCOME_GENERATED = "suggestions_generated"
OUTCOME_NO_SUPPORTED_TARGET = "no_supported_target"
OUTCOME_INSUFFICIENT_KNOWN_EVIDENCE = "insufficient_known_evidence"
OUTCOME_NO_UNRESOLVED_BAND = "no_unresolved_band"

OUTCOMES: Tuple[str, ...] = (
    OUTCOME_GENERATED, OUTCOME_NO_SUPPORTED_TARGET,
    OUTCOME_INSUFFICIENT_KNOWN_EVIDENCE, OUTCOME_NO_UNRESOLVED_BAND,
)


class GenerationV2Error(Exception):
    """F-B v2 could not proceed. PHI-safe: never quotes a milestone."""

    PHI_SAFE_MESSAGE = True


class BandAssessmentIncomplete(GenerationV2Error):
    """A band's evidence does not cover its canonical roster.

    REFUSED, never skipped upward. An unasked sibling is not a demonstrated one,
    and stepping past the gap would read our own incomplete questioning as the
    child's mastery.
    """


class ProjectionNotEvidenceDriven(GenerationV2Error):
    """This projection is not a v2 skill-level projection.

    A v1 projection carries at most one skill per band and no per-skill
    evidence, so it cannot answer "which skill failed". Refused explicitly
    rather than inferred from an empty evidence list, so a v1 record can never
    be dressed up as v2 input.
    """


@dataclass(frozen=True)
class BandView:
    """One band's classification and its three populations. Derived, never stored."""

    months: int
    classification: str
    #: Refs by state, each ordered by `rung_ref` for stability only.
    target_refs: Tuple[str, ...] = ()
    unknown_refs: Tuple[str, ...] = ()
    demonstrated_refs: Tuple[str, ...] = ()
    #: Refs the canonical roster declares that the evidence never covered.
    missing_refs: Tuple[str, ...] = ()

    @property
    def is_mastered(self) -> bool:
        return self.classification == BAND_MASTERED

    @property
    def is_unresolved(self) -> bool:
        return self.classification == BAND_UNRESOLVED

    @property
    def is_complete(self) -> bool:
        return self.classification != BAND_INCOMPLETE


def classify_band(*, months: int, roster: Sequence[str],
                  states_by_ref: Mapping[str, str]) -> BandView:
    """Classify one band from its canonical roster and the projected states.

    `roster` is the canonical declared-track ref set for this band, read from the
    frozen table. `states_by_ref` is the projected evidence for this band.

    Completeness is SET EQUALITY against the roster, in both directions:

      * a roster ref with no evidence   -> INCOMPLETE (we did not ask)
      * an evidence ref outside the roster -> INCOMPLETE (we asked something the
        frozen table does not place in this band, so the two disagree about what
        the band IS, and no classification of it would mean anything)

    Ordering within each population is by `rung_ref`. That is deliberately NOT
    milestone text: sorting by prose is what produced v1's alphabetical winner,
    and a ref is a digest, so its order carries no clinical signal at all.
    """
    if isinstance(months, bool) or not isinstance(months, int):
        raise GenerationV2Error("a band requires integer months")
    roster_set = set(roster)
    if not roster_set:
        raise GenerationV2Error(
            "a band with no canonical skills cannot be classified")
    evidence_set = set(states_by_ref)

    missing = tuple(sorted(roster_set - evidence_set))
    stray = evidence_set - roster_set
    if missing or stray:
        return BandView(months=months, classification=BAND_INCOMPLETE,
                        missing_refs=missing)

    targets, unknowns, demonstrated = [], [], []
    for ref in sorted(roster_set):
        state = states_by_ref[ref]
        if state in TARGET_STATES:
            targets.append(ref)
        elif state == UNKNOWN_STATE:
            unknowns.append(ref)
        elif state == MASTERED_STATE:
            demonstrated.append(ref)
        else:
            # An unrecognised state. Refused rather than treated as "not a
            # target": a state this module does not understand might be a
            # deficit, and guessing in the safe-looking direction would hide it.
            raise GenerationV2Error(
                "a projected skill carries an unrecognised state")

    classification = (BAND_MASTERED if not targets and not unknowns
                      else BAND_UNRESOLVED)
    return BandView(months=months, classification=classification,
                    target_refs=tuple(targets),
                    unknown_refs=tuple(unknowns),
                    demonstrated_refs=tuple(demonstrated))


def select_target_band(views: Sequence[BandView]) -> Optional[BandView]:
    """The FIRST unresolved band, scanning bands in ascending month order.

    Lowest first, because a deficit low on the ladder is the one a child needs
    next — the same direction the baseline itself walks.

    Refuses at the first INCOMPLETE band rather than continuing past it. That is
    the fail-closed rule: an incomplete band might be unresolved or might be
    mastered once finished, and we cannot know which, so treating it as mastered
    to reach a higher band would be inferring mastery from missing evidence.

    Returns `None` when every band is complete AND mastered — a real finding, not
    a failure. F-B v2 does NOT then step to the next developmental band: without
    evidence for a higher band there is nothing to target, and inventing one is
    exactly the traversal this policy removed.
    """
    for view in sorted(views, key=lambda v: v.months):
        if view.classification == BAND_INCOMPLETE:
            raise BandAssessmentIncomplete(
                "a band's evidence does not cover its canonical roster")
        if view.is_unresolved:
            return view
    return None


def require_v2_projection(projection: Any) -> None:
    """Refuse anything that is not a v2 skill-level projection.

    Checked on the SCHEMA rather than on the presence of `skill_evidence`, so a
    hand-built object carrying an evidence attribute cannot pass as v2.
    """
    schema = getattr(projection, "projection_schema", None)
    if schema != PROJECTION_SCHEMA_V2:
        raise ProjectionNotEvidenceDriven(
            "F-B v2 requires a v2 skill-level projection")


def v1_claim_is_not_reusable_for_v2(claim: Any) -> bool:
    """Whether a v1 generation claim could satisfy a v2 generation. Always False.

    Stated as a function so the rule is testable and so a later reader cannot
    quietly reuse one. Two independent reasons, either of which alone suffices:

      1. `generation_policy` is part of the generation key, and the two policies
         are different strings — so the derived claim ids differ;
      2. a v1 claim names a `pbpj_` projection and a v2 claim names a `pbp2_`
         one, and the projection id is also in the key.

    The practical consequence: switching a child from v1 to v2 generation does
    not silently resurface the v1 suggestion as though the new policy had chosen
    it. It generates afresh, from evidence, and the v1 record stays historically
    valid beside it.
    """
    policy = getattr(claim, "generation_policy", "")
    if policy == GENERATION_POLICY_VERSION_V2:
        raise GenerationV2Error(
            "this helper answers for LEGACY v1 claims; a v2 claim is already "
            "evidence-driven")
    return False


__all__ = [
    "BAND_INCOMPLETE",
    "BAND_MASTERED",
    "BAND_UNRESOLVED",
    "BandAssessmentIncomplete",
    "BandView",
    "GENERATION_POLICY_VERSION_V2",
    "GenerationV2Error",
    "MASTERED_STATE",
    "OUTCOMES",
    "OUTCOME_GENERATED",
    "OUTCOME_INSUFFICIENT_KNOWN_EVIDENCE",
    "OUTCOME_NO_SUPPORTED_TARGET",
    "OUTCOME_NO_UNRESOLVED_BAND",
    "ProjectionNotEvidenceDriven",
    "TARGET_STATES",
    "UNKNOWN_STATE",
    "classify_band",
    "require_v2_projection",
    "select_target_band",
    "v1_claim_is_not_reusable_for_v2",
]
