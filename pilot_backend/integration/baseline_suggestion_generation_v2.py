"""pilot_backend/integration/baseline_suggestion_generation_v2.py — F-B v2.

0.6A-1G. Turns an immutable `ParentBaselineProjectionV2` into ZERO, ONE or
SEVERAL anchored `GoalSuggestion`s — one per canonical skill the child was
actually shown not to have.

## WHAT THIS REPLACES

F-B v1 asked the ladder where the child was and stepped up:

    routing_anchor_months -> _step(+1) -> question_at(month)
                          -> the first same-month rung by milestone text

The last step is the defect. At 30 months the declared track holds four skills;
v1 planned for whichever sorted first, regardless of which one the child failed.

F-B v2 asks the EVIDENCE instead. Each `ProjectedSkillEvidence` already carries
the exact canonical `rung_ref` and the observed state, so the target is read, not
inferred. None of `routing_anchor_months`, `not_demonstrated_months`,
`_step(+1)` or `question_at` is reachable from this module — a test walks its AST
and asserts the names do not appear.

## ONE BAND, EVERY DEFICIT IN IT

The first unresolved COMPLETE band is selected, lowest first, and EVERY
targetable skill in it becomes its own suggestion. No winner is chosen, nothing
is ranked, and no skill is called clinically more important than a sibling —
that judgement is Hannah's, and making it here would hide two real findings
behind one.

## MAPPABLE AND UNMAPPABLE DEFICITS ARE BOTH REPORTED

A canonical deficit with no reconciled activity family is NOT dropped, NOT mapped
to a near match, and NOT allowed to block its mappable siblings. It is returned
as an UNSUPPORTED TARGET REF.

No fake suggestion is created for it. A `GoalSuggestion` without a valid
`SuggestionCanonicalAnchor` is a goal `allocate_goal` later refuses — so it would
surface as a mysterious dead goal instead of an honest "we know about this and
cannot support it yet". The deterministic ref list is the cheapest truthful
representation and needs no new entity.

## PER-TARGET ATOMICITY, DELIBERATELY NOT ONE BIG TRANSACTION

Each target commits through the frozen claim-first path on its own: one claim,
one suggestion, one anchor, together. The targets are NOT wrapped in a single
transaction, because then one failing or contended target would roll back its
siblings — which is the "must not block mappable siblings" rule violated by the
transaction boundary instead of by the logic.

A partial result is therefore possible and is honest: the targets that generated
really did generate, and a replay completes the rest without duplicating them.

## NOTHING CLINICAL IS COMPUTED HERE

Band classification and the state-to-target rules live in
`domain/suggestion_generation_v2`, which is pure. Rung resolution is the frozen
table's `rung_by_ref`. The commit is `GoalService.generate_anchored_suggestion`,
unchanged except for an additive policy parameter. This module ORDERS those gates
and refuses when one says no.

## NO ACTIVITY GENERATION

The activity bank is not imported and no plan record is created: no
`MonthlyFocusPlan`, no `MonthlyGoalAllocation`, no `WeeklyCycle`, no
`CandidateActivity`, no `WeeklyPlanSnapshot`. A test asserts none of those
repositories is named in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..domain.source_link import SourceSystem
from ..domain.suggestion_generation import ProjectionLineageInvalid
from ..domain.suggestion_generation_v2 import (
    GENERATION_POLICY_VERSION_V2,
    OUTCOME_GENERATED,
    OUTCOME_INSUFFICIENT_KNOWN_EVIDENCE,
    OUTCOME_NO_SUPPORTED_TARGET,
    OUTCOME_NO_UNRESOLVED_BAND,
    BandView,
    GenerationV2Error,
    classify_band,
    require_v2_projection,
    select_target_band,
)
from ..goals.suggestion_engine import EvidenceSource, ObservedDomain

#: The only domain F-B v2 generates for. Widening this is a clinical decision
#: with its own track, taxonomy coverage and review — not a config change.
SUPPORTED_DOMAIN = "talking_and_communicating"


@dataclass(frozen=True)
class GeneratedTarget:
    """One canonical target that became a real anchored suggestion."""

    rung_ref: str
    months: int
    suggestion_ids: Tuple[str, ...]
    #: False when an identical generation already existed — a replay, not a
    #: second equivalent set.
    created: bool


@dataclass(frozen=True)
class GenerationV2Outcome:
    """What one F-B v2 request found and produced.

    Every field is derived from canonical refs and ids. NOTHING here carries
    milestone prose, a caregiver answer, a Parent identifier or a claim id — see
    the transport for what is then rendered.
    """

    projection_id: str
    generation_policy: str
    outcome: str
    #: None when no band was unresolved at all.
    target_band_months: Optional[int]
    generated: Tuple[GeneratedTarget, ...] = ()
    #: Canonical deficits with no reconciled activity family. Known, reported,
    #: and deliberately NOT turned into suggestions.
    unsupported_target_refs: Tuple[str, ...] = ()
    #: Assessed-but-unanswerable skills in the selected band. They keep it off
    #: "mastered" and are never targets.
    unknown_refs: Tuple[str, ...] = ()

    @property
    def suggestion_ids(self) -> Tuple[str, ...]:
        return tuple(sid for target in self.generated
                     for sid in target.suggestion_ids)

    @property
    def created_any(self) -> bool:
        return any(target.created for target in self.generated)


class BaselineSuggestionGenerationV2Service:
    """Evidence-driven generation. Resolve, classify, then one commit per target."""

    def __init__(self, *, repos: Any, goals: Any, rung_source: Any) -> None:
        self._repos = repos
        self._goals = goals
        self._rungs = rung_source

    # -- lineage ----------------------------------------------------------

    def _current_parent_session(self, child_id: str) -> str:
        """The child's ONE active Parent source link, or a refusal.

        Resolved through the link rather than by scanning for the newest
        projection, so the chain is
        `child -> active PARENT link -> session -> projection` end to end. The
        same rule F-B v1 uses, restated rather than imported, because the two
        services must be able to change independently.
        """
        links = [link for link in self._repos.source_links.list_for_child(child_id)
                 if link.source_system is SourceSystem.PARENT and link.is_active]
        if not links:
            raise ProjectionLineageInvalid(
                "this child has no active Parent source link")
        if len(links) > 1:
            raise ProjectionLineageInvalid(
                "this child has more than one active Parent source link")
        return links[0].external_id

    def _applicable_projection(self, child_id: str, session_id: str):
        """The ONE v2 projection for this session and domain, or a refusal.

        Reads the v2 collection ONLY. A v1 projection for the same session is
        irrelevant here and is never consulted: it carries at most one skill per
        band, so it cannot answer which skill failed.
        """
        found = list(self._repos.parent_baseline_projections_v2
                     .list_for_source(session_id, SUPPORTED_DOMAIN))
        if not found:
            raise ProjectionLineageInvalid(
                "no v2 baseline projection exists for this child's session")
        if len(found) > 1:
            # Two digests for one immutable source. A2 v2 raises an integrity
            # conflict on the way in, so reaching here means something already
            # went wrong — never resolved by choosing the newer one.
            raise ProjectionLineageInvalid(
                "more than one v2 baseline projection applies to this session")
        projection = found[0]
        if projection.child_id != child_id:
            raise ProjectionLineageInvalid(
                "the baseline projection names a different canonical child")
        return projection

    # -- the clinical read, side-effect free ------------------------------

    def band_views(self, projection) -> Tuple[BandView, ...]:
        """Classify every band the projection declares. Writes nothing.

        Public and pure so a reviewer — and a test — can inspect the clinical
        reading without generating anything.

        The roster comes from the FROZEN table, not from the projection's own
        `total_skills`. A2 v2 already verified the two agree, and this re-derives
        rather than inherits that conclusion: F-B v2 is a second independent
        consumer, and a projection written before the 0.6A-1F hardening existed
        would otherwise be trusted on a number nobody checked.
        """
        require_v2_projection(projection)
        if projection.domain != SUPPORTED_DOMAIN:
            raise GenerationV2Error(
                "this domain is not generatable in the pilot")

        views = []
        for band in projection.band_totals:
            months = int(band.months)
            roster = self._rungs.declared_band_roster(projection.domain, months)
            if band.total_skills != len(roster):
                # The projection's declared denominator disagrees with the
                # frozen roster. Refused rather than preferred one way or the
                # other: understating manufactures mastery, overstating hides a
                # target, and we cannot tell which happened.
                raise GenerationV2Error(
                    "a projected band total disagrees with the canonical "
                    "declared-track roster")
            states = {evidence.rung_ref: evidence.state
                      for evidence in projection.assessed_in_band(months)}
            views.append(classify_band(months=months, roster=roster,
                                       states_by_ref=states))
        if not views:
            raise GenerationV2Error(
                "a v2 projection must declare at least one band")
        return tuple(views)

    def resolve_targets(self, projection) -> Tuple[Optional[BandView],
                                                   Tuple[Any, ...],
                                                   Tuple[str, ...]]:
        """`(band, mappable_rungs, unsupported_refs)`. Writes nothing.

        Splits the selected band's deficits into the two populations the founder
        named. Mappability is ASKED (`is_mappable_ref`) rather than discovered by
        catching an exception, so an unmappable deficit is a reported finding
        instead of a failure that would abort its siblings.
        """
        band = select_target_band(self.band_views(projection))
        if band is None:
            return None, (), ()

        rungs: List[Any] = []
        unsupported: List[str] = []
        for ref in band.target_refs:
            if not self._rungs.is_mappable_ref(projection.domain, ref):
                unsupported.append(ref)
                continue
            # Anchored to the ref the EVIDENCE named, at the band the evidence
            # named. No step, no question_at, no tiebreak.
            rungs.append(self._rungs.rung_by_ref(
                projection.domain, ref, expected_months=band.months))
        return band, tuple(rungs), tuple(unsupported)

    def _observed_domain(self, projection, rung) -> ObservedDomain:
        """The smallest valid observation carrying this projection's lineage.

        `prior_month_summary_id` is deliberately LEFT UNSET, the same decision
        F-B v1 had to correct: setting it adds a prior-cycle-continuity score
        bonus and an `explain()` reason a first functional baseline has not
        earned. Lineage lives on the generation claim instead.
        """
        return ObservedDomain(
            domain_key=projection.domain,
            answered=True,
            evidence_source=EvidenceSource.FUNCTIONAL_BASELINE,
            functional_baseline_area=projection.area_id,
            observed_level=projection.entry_choice_id,
            explicitly_selected=False,
            canonical_rung=rung)

    # -- the operation ----------------------------------------------------

    def generate_for_child(self, principal, child_id: str, *,
                           request_id: str = "") -> GenerationV2Outcome:
        """Generate every supportable target in the first unresolved band.

        Authorization is the caller's: this method performs none of its own and
        delegates to `GoalService`, which gates on `authorize_child_access`. The
        transport additionally requires PROVIDER role and the managing clinician.

        The four outcomes are all legitimate results rather than failures — see
        the outcome vocabulary in `domain/suggestion_generation_v2`.
        """
        session_id = self._current_parent_session(child_id)
        projection = self._applicable_projection(child_id, session_id)
        band, rungs, unsupported = self.resolve_targets(projection)

        def _result(outcome: str, generated=()) -> GenerationV2Outcome:
            return GenerationV2Outcome(
                projection_id=projection.projection_id,
                generation_policy=GENERATION_POLICY_VERSION_V2,
                outcome=outcome,
                target_band_months=None if band is None else band.months,
                generated=tuple(generated),
                unsupported_target_refs=unsupported,
                unknown_refs=() if band is None else band.unknown_refs)

        if band is None:
            # Every band complete and mastered. No target, and NO step to a
            # higher band: without evidence up there, a target would be invented.
            return _result(OUTCOME_NO_UNRESOLVED_BAND)

        if not band.target_refs:
            # Unresolved only because of `unknown`. The question was asked and
            # could not be answered, which is not evidence a skill is absent.
            return _result(OUTCOME_INSUFFICIENT_KNOWN_EVIDENCE)

        generated: List[GeneratedTarget] = []
        for rung in rungs:
            suggestions, created = self._goals.generate_anchored_suggestion(
                principal, child_id, projection=projection,
                canonical_rung=rung,
                observed=self._observed_domain(projection, rung),
                generation_policy=GENERATION_POLICY_VERSION_V2,
                request_id=request_id)
            generated.append(GeneratedTarget(
                rung_ref=rung.rung_ref,
                months=rung.source_rung_months,
                suggestion_ids=tuple(s.suggestion_id for s in suggestions),
                created=created))
        # Stable order for the response. By REF, never by milestone text — prose
        # ordering is what produced v1's alphabetical winner, and a ref is a
        # digest, so its order carries no clinical signal.
        generated.sort(key=lambda target: target.rung_ref)

        if not generated:
            # Every known deficit is canonical but unmappable. An explicit,
            # deterministic result — not success, and not a silent step upward
            # to find a milestone we happen to have activities for.
            return _result(OUTCOME_NO_SUPPORTED_TARGET)
        return _result(OUTCOME_GENERATED, generated)


__all__ = [
    "SUPPORTED_DOMAIN",
    "BaselineSuggestionGenerationV2Service",
    "GeneratedTarget",
    "GenerationV2Outcome",
]
