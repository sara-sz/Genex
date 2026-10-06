"""pilot_backend/integration/baseline_suggestion_generation.py — 0.5F-B.

Turns an immutable `ParentBaselineProjection` into a deterministic anchored
`GoalSuggestion`, through the frozen 0.5E-A/0.5E-B machinery.

## IT COMPUTES NOTHING CLINICAL OF ITS OWN

Every clinical decision here is made by frozen code somewhere else:

    which statuses may plan        Parent `finalize()` semantics, restated as
                                   GENERATABLE_STATUSES with the reason
    where the child is now         the projection's `routing_anchor_months`,
                                   which IS Parent's `_floor_months()`
    what comes next               `GoldStandardRungSource.next_rung_target`,
                                   which the live adapter answers by calling
                                   Parent's own `_step(domain, m, +1, track)`
    what the rung IS              `rung_for_target` over the real workbook and
                                   the real taxonomy
    whether it is usable          `CanonicalRung.is_activity_mappable`

This module's whole job is ORDERING those gates and refusing when any of them
says no. It contains no month arithmetic, no milestone text, no family names and
no second ladder.

## CHRONOLOGICAL AGE AND DIAGNOSIS ARE NOT CONSULTED

Neither is reachable: the projection carries no age and no diagnosis — A2
refuses both by name — and the target is a function of the observed floor alone.
That is the foundational Genex rule, enforced here by the shape of the input
rather than by a check that could be removed.

## LINEAGE LIVES ON THE GENERATION CLAIM, NOT ON THE OBSERVATION

    GoalSuggestionGenerationClaim.projection_id
      -> GoalSuggestionGenerationClaim.suggestion_ids
      -> GoalSuggestion
      -> SuggestionCanonicalAnchor

That chain is complete and unambiguous, so the observation carries NO projection
reference at all.

An earlier revision put the projection id in
`ObservedDomain.prior_month_summary_id`, reasoning that it was the one existing
field meaning "the record that supplied this evidence". That was WRONG, and not
only by name. The field is load-bearing in the frozen engine:

    evidence_score()  adds SCORE_PRIOR_CYCLE_CONTINUITY (15) when it is set,
                      measured 25 -> 40 for this observation
    explain()         appends the reason literal "prior_cycle_continuity"

Its documented contract is "Prior-month evidence, when a later cycle supplies
it". A first-ever Parent functional baseline is the opposite of that, so using
it would have inflated this suggestion's rank against other domains by 60% and
shown a clinician a continuity reason that does not exist.

`ObservedDomain` still carries `functional_baseline_area`, `observed_level` and
`evidence_source`, which describe the observation itself and are true. No new
provenance field was added; `asked[]`, the diagnosis, the concern, the qna and
both uids stay where they are.

## FAIL CLOSED, ALWAYS

Every refusal raises. There is no partial result, no "nearest" rung, no default
starting level and no skip to the next mappable rung. The four intentionally
unresolved declared-SLP cases stay refused because `rung_for_target` refuses
them, not because this module lists them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Tuple

from ..domain.source_link import SourceSystem
from ..domain.suggestion_generation import (
    BaselineNotGeneratable,
    ProjectionLineageInvalid,
    TargetNotMappable,
    TargetNotResolvable,
    is_generatable_status,
    target_within_ceiling,
)
from ..goals.suggestion_engine import EvidenceSource, ObservedDomain

#: The only domain 0.5F-B generates for. Widening this is a clinical decision
#: with its own track, taxonomy coverage and review — not a config change.
SUPPORTED_DOMAIN = "talking_and_communicating"


@dataclass(frozen=True)
class GenerationOutcome:
    """What one generation request produced."""

    suggestions: Tuple[Any, ...]
    created: bool
    projection_id: str
    target_rung_ref: str
    target_rung_months: int


class BaselineSuggestionGenerationService:
    """Resolve the projection, decide the target, delegate the commit."""

    def __init__(self, *, repos: Any, goals: Any, rung_source: Any) -> None:
        self._repos = repos
        self._goals = goals
        self._rungs = rung_source

    # -- lineage ----------------------------------------------------------

    def _current_parent_session(self, child_id: str) -> str:
        """The child's ONE active Parent source link, or a refusal.

        Resolved through the link rather than by scanning for the newest
        projection, so the chain is
        `child -> active PARENT link -> session -> projection` end to end. A
        "latest projection" search would quietly plan from a session the child
        is no longer linked to.
        """
        links = [link for link in self._repos.source_links.list_for_child(child_id)
                 if link.source_system is SourceSystem.PARENT and link.is_active]
        if not links:
            raise ProjectionLineageInvalid(
                "this child has no active Parent source link")
        if len(links) > 1:
            # The 0.4A CHILD_SOURCE claim makes this impossible; refuse rather
            # than pick, because picking would choose whose baseline counts.
            raise ProjectionLineageInvalid(
                "this child has more than one active Parent source link")
        return links[0].external_id

    def _applicable_projection(self, child_id: str, session_id: str):
        """The ONE projection for this session and domain, or a refusal."""
        found = [p for p in self._repos.parent_baseline_projections
                 .list_for_source(session_id, SUPPORTED_DOMAIN)]
        if not found:
            raise ProjectionLineageInvalid(
                "no baseline projection exists for this child's Parent session")
        if len(found) > 1:
            # Two digests for one immutable source. A2 raises an integrity
            # conflict on the way in, so reaching here means something already
            # went wrong — never resolved by choosing the newer one.
            raise ProjectionLineageInvalid(
                "more than one baseline projection applies to this session")
        projection = found[0]
        if projection.child_id != child_id:
            raise ProjectionLineageInvalid(
                "the baseline projection names a different canonical child")
        return projection

    # -- the frozen algorithm ---------------------------------------------

    def resolve_target(self, projection) -> Any:
        """The canonical target rung for a projection, or a refusal.

        Public and side-effect free so a test — and a reviewer — can exercise
        the clinical decision without writing anything.
        """
        if projection.domain != SUPPORTED_DOMAIN:
            raise BaselineNotGeneratable(
                "this domain is not generatable in the pilot")
        if not is_generatable_status(projection.status):
            # UNRESOLVED or CONTRADICTORY. The states Parent 0.4 exists to stop
            # becoming a silent starting level.
            raise BaselineNotGeneratable(
                "this baseline status cannot produce a target")
        if not projection.has_routing_anchor:
            # Derived, never stored — so no drifted copy can claim an anchor the
            # baseline does not have.
            raise BaselineNotGeneratable(
                "this baseline resolved to no planning anchor")

        floor = projection.routing_anchor_months
        target = self._rungs.next_rung_target(projection.domain, floor)
        if target is None:
            raise TargetNotResolvable(
                "there is no next rung on the declared track")
        if not target_within_ceiling(target.source_rung_months,
                                     projection.not_demonstrated_months):
            # Above a rung the child has already been shown not to have.
            # Refused rather than clamped: clamping would re-target the goal at
            # a rung the algorithm did not choose.
            raise TargetNotResolvable(
                "the next rung is above the known baseline ceiling")

        try:
            rung = self._rungs.rung_for_target(target)
        except Exception as exc:
            # RungNotFound / RungNotMappable / RungTrackUndeclared all land
            # here. Collapsed to one refusal: the caller may not learn which
            # milestone failed, and all three mean the same thing operationally
            # — this target cannot become a mappable goal.
            raise TargetNotMappable(
                "the next rung cannot be resolved as a mappable target"
            ) from exc
        if not rung.is_activity_mappable:
            raise TargetNotMappable(
                "the next rung has no usable activity family")
        return rung

    def _observed_domain(self, projection, rung) -> ObservedDomain:
        """The smallest valid observation carrying this projection's lineage."""
        return ObservedDomain(
            domain_key=projection.domain,
            answered=True,
            # The EXISTING member for a functional-baseline area assessment.
            # Note what is not representable: `EvidenceSource` has no diagnosis
            # member at all, so "diagnosis does not override observation" is
            # enforced by the enum's shape rather than by a check here.
            evidence_source=EvidenceSource.FUNCTIONAL_BASELINE,
            functional_baseline_area=projection.area_id,
            observed_level=projection.entry_choice_id,
            explicitly_selected=False,
            # `prior_month_summary_id` is deliberately LEFT UNSET — see the
            # module docstring. It would add a prior-cycle-continuity score
            # bonus and an `explain()` reason this baseline has not earned.
            # Lineage is on the generation claim instead.
            canonical_rung=rung)

    # -- the operation ----------------------------------------------------

    def generate_for_child(self, principal, child_id: str, *,
                           request_id: str = "") -> GenerationOutcome:
        """Generate (or resolve) this child's anchored suggestion.

        Authorization is the caller's: this method performs none of its own and
        delegates to `GoalService`, which gates on `authorize_child_access`. The
        transport layer additionally requires the managing clinician.
        """
        session_id = self._current_parent_session(child_id)
        projection = self._applicable_projection(child_id, session_id)
        rung = self.resolve_target(projection)
        observed = self._observed_domain(projection, rung)

        suggestions, created = self._goals.generate_anchored_suggestion(
            principal, child_id, projection=projection, canonical_rung=rung,
            observed=observed, request_id=request_id)
        return GenerationOutcome(
            suggestions=tuple(suggestions),
            created=created,
            projection_id=projection.projection_id,
            target_rung_ref=rung.rung_ref,
            target_rung_months=rung.source_rung_months)
