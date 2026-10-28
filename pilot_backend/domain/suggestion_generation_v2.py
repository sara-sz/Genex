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

#: The fifth outcome. Every MAPPABLE skill through the top of the track is
#: demonstrated, but at least one REGISTERED-UNRESOLVED deficit was passed over.
#:
#: It exists because `no_unresolved_band` would be a false statement here: it
#: reads as "this child has no unresolved skills", and the child demonstrably
#: has one — Genex simply has no intervention for it this cycle. Collapsing the
#: two would turn "we cannot help with this yet" into "this is not a problem",
#: which is the exact concealment the registered-unresolved design exists to
#: prevent.
OUTCOME_NO_SUPPORTED_TARGET_REGISTERED_GAPS = \
    "no_supported_target_registered_gaps"

OUTCOMES: Tuple[str, ...] = (
    OUTCOME_GENERATED, OUTCOME_NO_SUPPORTED_TARGET,
    OUTCOME_INSUFFICIENT_KNOWN_EVIDENCE, OUTCOME_NO_UNRESOLVED_BAND,
    OUTCOME_NO_SUPPORTED_TARGET_REGISTERED_GAPS,
)

#: How the canonical table classifies ONE ref for planning. Three states, and
#: conflating any two of them is the defect this vocabulary removes:
#:
#:   mappable               -> a real target; generate for it
#:   registered_unresolved  -> canonical, deliberately unsupported, REPORTABLE
#:   unregistered_unmappable -> a taxonomy/data defect; FAIL CLOSED
REF_MAPPABLE = "mappable"
REF_REGISTERED_UNRESOLVED = "registered_unresolved"
REF_UNREGISTERED_UNMAPPABLE = "unregistered_unmappable"

REF_SUPPORT_STATES: Tuple[str, ...] = (
    REF_MAPPABLE, REF_REGISTERED_UNRESOLVED, REF_UNREGISTERED_UNMAPPABLE,
)

#: The `unresolved_reason` the rung-table artifact writes when EVERY unresolved
#: family on a rung is in the taxonomy's register.
#:
#: Defined HERE, in the domain, rather than in the generator: the generator
#: writes it and the serving-side artifact reader classifies on it, and those
#: two live in different container images on purpose. Importing the generator
#: from the reader to share one constant would give the serving image an edge
#: to the offline generation module — the separation `0.5E-B` and `0.5F-B`
#: built two build contexts to maintain. The domain is already in both.
REASON_REGISTERED_UNRESOLVED = REF_REGISTERED_UNRESOLVED


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


class UnregisteredUnmappableSkill(GenerationV2Error):
    """A deficit is unmappable and NOT in the taxonomy's unresolved register.

    REFUSED, and deliberately not passed over. Passing over an unmappable
    deficit is only honest when the taxonomy has recorded that the gap is
    intentional; the register IS that record. Without it we cannot tell a
    founder-approved deferral from a data defect, and treating a defect as a
    deferral would silently drop a real deficit and plan a HIGHER target
    instead — concealing the very gap this path exists to surface.

    So this is the fail-closed half of the bounded pass-over: the register
    makes a skip possible, and its absence makes the skip impossible.
    """


@dataclass(frozen=True)
class PassedOverDeficit:
    """One canonical deficit Genex knows about and cannot support this cycle.

    Machine-readable only. `months` and `rung_ref` identify it stably, and
    `resolution_status` says why it is unsupported. The FAMILY KEY is carried
    because it is the stable identifier a clinician-facing surface can label
    without quoting milestone prose — the no-prose contract on the v2 route
    forbids the milestone text itself, and inventing a human-readable sentence
    here would smuggle clinical copy into an API response.
    """

    months: int
    rung_ref: str
    resolution_status: str
    family_keys: Tuple[str, ...] = ()

    def as_dict(self) -> dict:
        """PHI-safe and prose-free: months, ref, status, family keys."""
        return {
            "months": self.months,
            "rung_ref": self.rung_ref,
            "resolution_status": self.resolution_status,
            "family_keys": list(self.family_keys),
        }


@dataclass(frozen=True)
class BandScan:
    """The result of scanning every band: what to target, and what was skipped.

    `selected` is None when no band holds a mappable deficit. `passed_over` is
    never discarded — a caller that ignored it would reproduce the concealment
    the scan exists to prevent, which is why the scan returns them together
    rather than offering the band alone.
    """

    selected: Optional["BandView"] = None
    passed_over: Tuple[PassedOverDeficit, ...] = ()


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
    #: `target_refs` PARTITIONED by canonical support. Their union is exactly
    #: `target_refs`, which stays the whole deficit population so no existing
    #: reader loses sight of a deficit just because it is unsupported.
    #:
    #: When the caller supplies no support map these default to "every deficit
    #: is mappable", which reproduces the pre-registry behaviour exactly — the
    #: band is selected and supportability is decided downstream.
    mappable_target_refs: Tuple[str, ...] = ()
    registered_target_refs: Tuple[str, ...] = ()
    unregistered_target_refs: Tuple[str, ...] = ()
    #: ref -> resolution status, for the registered ones only.
    registered_statuses: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_mastered(self) -> bool:
        return self.classification == BAND_MASTERED

    @property
    def is_unresolved(self) -> bool:
        return self.classification == BAND_UNRESOLVED

    @property
    def is_complete(self) -> bool:
        return self.classification != BAND_INCOMPLETE

    @property
    def has_mappable_deficit(self) -> bool:
        """Whether this band holds a target Genex can actually plan for."""
        return bool(self.mappable_target_refs)


def classify_band(*, months: int, roster: Sequence[str],
                  states_by_ref: Mapping[str, str],
                  support_by_ref: Optional[Mapping[str, str]] = None
                  ) -> BandView:
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

    # Partition the deficits by canonical support. With no support map every
    # deficit counts as mappable, which is exactly the pre-registry behaviour:
    # the band is selected and supportability is settled downstream.
    mappable, registered, unregistered = [], [], []
    statuses: Dict[str, str] = {}
    for ref in targets:
        support = (support_by_ref or {}).get(ref, REF_MAPPABLE)
        if support == REF_MAPPABLE:
            mappable.append(ref)
        elif support == REF_UNREGISTERED_UNMAPPABLE:
            unregistered.append(ref)
        elif isinstance(support, str) and support.startswith(
                REF_REGISTERED_UNRESOLVED):
            registered.append(ref)
            # "registered_unresolved:deferred_post_pilot" -> the status tail.
            _, _, tail = support.partition(":")
            statuses[ref] = tail or REF_REGISTERED_UNRESOLVED
        else:
            # An unrecognised support value. Refused for the same reason an
            # unrecognised evidence state is: guessing in the permissive
            # direction would hide a deficit.
            raise GenerationV2Error(
                "a canonical ref carries an unrecognised support state")

    return BandView(months=months, classification=classification,
                    target_refs=tuple(targets),
                    unknown_refs=tuple(unknowns),
                    demonstrated_refs=tuple(demonstrated),
                    mappable_target_refs=tuple(mappable),
                    registered_target_refs=tuple(registered),
                    unregistered_target_refs=tuple(unregistered),
                    registered_statuses=statuses)


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
    return scan_bands(views).selected


def scan_bands(views: Sequence[BandView]) -> BandScan:
    """Select a band AND report every registered deficit passed over to reach it.

    The founder-approved bounded pass-over, stated once. Scanning bands in
    ascending month order:

      1. INCOMPLETE          -> refuse. Unchanged: an unasked sibling is not a
                               demonstrated one.
      2. MASTERED            -> continue.
      3. UNRESOLVED with ANY unregistered unmappable deficit
                             -> refuse. A data defect, not a deferral.
      4. UNRESOLVED with at least one MAPPABLE deficit
                             -> SELECT it. Registered siblings in the same band
                               are reported but do NOT block the mappable ones,
                               and the scan stops here: the lowest supported
                               band is still the one the child needs next.
      5. UNRESOLVED with any UNKNOWN sibling
                             -> SELECT it, unchanged. See below.
      6. UNRESOLVED with only REGISTERED deficits, all states known
                             -> record them in `passed_over` and continue.
      7. nothing left        -> `selected is None`, and `passed_over` still
                               carries whatever was skipped, so a caller can
                               tell "no unresolved skills" from "none we can
                               support".

    Rule 3 before rule 6 is the whole safety property. Reversed, an unmappable
    deficit with no register entry would be skipped as though the founder had
    approved skipping it, and a higher target would be planned over a real
    lower-band gap — exactly the concealment the register prevents.

    Rule 4 not continuing past the selected band is the second half: a band
    that can be partially supported is still the lowest band Genex can act on,
    so the registered sibling is surfaced rather than traded for a higher
    target.

    RULE 5 IS LOAD-BEARING AND EASY TO MISS. An `unknown` sibling means we do
    not know whether that band holds a MAPPABLE deficit. Passing the band over
    because its KNOWN deficits all happen to be registered would infer the
    unknown skill's mastery from missing evidence — the same error
    `BandAssessmentIncomplete` exists to prevent, arriving by a different door.
    So a pass-over requires every state in the band to be known.
    """
    passed_over: list = []
    for view in sorted(views, key=lambda v: v.months):
        if view.classification == BAND_INCOMPLETE:
            raise BandAssessmentIncomplete(
                "a band's evidence does not cover its canonical roster")
        if not view.is_unresolved:
            continue
        if view.unregistered_target_refs:
            raise UnregisteredUnmappableSkill(
                "a deficit is unmappable and is not recorded in the "
                "taxonomy's unresolved register")
        band_registered = tuple(
            PassedOverDeficit(months=view.months, rung_ref=ref,
                              resolution_status=view.registered_statuses[ref])
            for ref in view.registered_target_refs)
        passable = (bool(view.registered_target_refs)
                    and not view.has_mappable_deficit
                    and not view.unknown_refs)
        if not passable:
            # Cases 1 and 5: this band is the answer, supportable or not.
            return BandScan(selected=view,
                            passed_over=tuple(passed_over) + band_registered)
        # Case 2: every deficit here is a known, registered, unsupported one.
        passed_over.extend(band_registered)
    return BandScan(selected=None, passed_over=tuple(passed_over))


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
