"""pilot_backend/rtm/evidence.py — derive the monthly factual summary.

Pure functions over records that already exist. No network, no model, no
payer logic. The source rows remain the truth; this produces a regenerable
view of them.

## It reuses the 0.4D/E counting rules rather than re-deriving them

`weekly/counting.summarize` already proves the no-double-count property:
totals come from DISTINCT event ids and per-goal attributions are allowed to
overlap. Re-implementing that arithmetic here would create a second place for
it to be wrong, and the two would drift. So this calls it.

## Attribution is by LOCAL month

Events are filtered by their own `attribution_month`, never by owning cycle.
A cycle spanning Oct 26 – Nov 1 contributes its Nov 1 attempt to November's
summary, not October's.

## distinct_observed_local_dates is a FACT, not a qualification

It counts distinct local dates on which an observation was recorded. It is
not qualifying days, not billable days, not eligible days. `MonitoringDay`
qualification stays deferred and no function here computes one.

## Goal status is conservative and never claims achievement

    no attributed attempts          -> INSUFFICIENT_EVIDENCE
    any not-ready / didn't-want     -> MODIFY
    attempts with no completion     -> CONTINUE
    completions present             -> CONTINUE

`PROGRESS` is never generated from completion alone, and `ACHIEVED` does not
exist in the vocabulary. Activity completion is not goal achievement, and the
only thing that can say otherwise is a clinician's own narrative.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence, Tuple

from ..coding.rules import CodingInputs, evaluate
from ..domain.observation import AttemptOutcome
from ..domain.rtm import RegulatoryStatus, RTMMonitoringPeriod, RTMTechnology
from ..domain.rtm_documentation import documented_minutes
from ..domain.rtm_summary import (
    EVIDENCE_RULE_VERSION,
    GoalEvidenceLine,
    GoalStatusRecommendation,
    RTMEvidenceSummary,
)
from ..weekly.counting import summarize


class EvidenceDerivationError(ValueError):
    """Invalid derivation input. PHI-safe."""

    PHI_SAFE_MESSAGE = True


def _status_for(attempts: int, completions: int, not_ready: int,
                didnt_want: int) -> GoalStatusRecommendation:
    """Conservative status language. Never ACHIEVED, never PROGRESS alone."""
    if attempts == 0:
        return GoalStatusRecommendation.INSUFFICIENT_EVIDENCE
    if not_ready or didnt_want:
        return GoalStatusRecommendation.MODIFY
    return GoalStatusRecommendation.CONTINUE


def derive_evidence_summary(period: RTMMonitoringPeriod, *,
                            alignments: Sequence,
                            events: Sequence,
                            cycles: Sequence,
                            reviews: Sequence = (),
                            actions: Sequence = (),
                            time_entries: Sequence = (),
                            interactions: Sequence = (),
                            technology: Optional[RTMTechnology] = None,
                            clinical_goal_refs: Tuple[Tuple[str, str], ...] = (),
                            now=None) -> RTMEvidenceSummary:
    """Build the factual monthly summary. Deterministic and regenerable."""
    month = period.cycle_month

    # Filter by the event's OWN local-month attribution.
    in_month = [e for e in events if e.attribution_month == month]
    coverage = summarize(alignments, in_month, attribution_month=month)

    outcome_counts = {o: 0 for o in AttemptOutcome}
    observed_dates = set()
    for event in in_month:
        outcome_counts[event.attempt_outcome] += 1
        observed_dates.add(event.local_date)

    per_goal = []
    for line in coverage.per_goal:
        goal_events = [e for e in in_month if e.event_id in line.event_ids]
        not_ready = sum(1 for e in goal_events
                        if e.attempt_outcome is AttemptOutcome.WASNT_READY_YET)
        didnt_want = sum(1 for e in goal_events
                         if e.attempt_outcome is AttemptOutcome.DIDNT_WANT_TO_TRY)
        per_goal.append(GoalEvidenceLine(
            goal_kind=line.goal_ref.kind.value,
            goal_id=line.goal_ref.goal_id,
            attributed_attempts=line.attempted,
            attributed_completions=line.completed,
            scheduled_opportunities=line.scheduled,
            status_recommendation=_status_for(
                line.attempted, line.completed, not_ready, didnt_want),
        ))

    minutes = documented_minutes(
        [t for t in time_entries if t.attribution_month == month])
    month_interactions = [i for i in interactions
                          if i.attribution_month == month]
    has_real_time = any(i.counts_as_real_time_communication
                        for i in month_interactions)

    status = (technology.regulatory_status if technology
              else RegulatoryStatus.UNDER_REVIEW)

    # Missing-documentation flags are reused from the coding rule set rather
    # than restated, so the evidence summary and the coding summary can never
    # disagree about what is absent.
    coding_view = evaluate(CodingInputs(
        documented_management_minutes=minutes,
        real_time_interactive_communication_present=has_real_time,
        technology_eligibility_established=(
            technology.eligibility_established if technology else False),
        period_finalized=period.is_finalized))

    return RTMEvidenceSummary.create(
        period.period_id, period.child_id, now=now,
        rule_version=EVIDENCE_RULE_VERSION,
        focus_plan_ref=period.focus_plan_id,
        clinical_goal_refs=clinical_goal_refs,
        cycle_refs=tuple(sorted(c.cycle_id for c in cycles)),
        total_distinct_observation_events=coverage.total_attempts,
        distinct_observed_local_dates=len(observed_dates),
        did_it_count=outcome_counts[AttemptOutcome.DID_IT],
        wasnt_ready_yet_count=outcome_counts[AttemptOutcome.WASNT_READY_YET],
        didnt_want_to_try_count=outcome_counts[AttemptOutcome.DIDNT_WANT_TO_TRY],
        per_goal_evidence=tuple(per_goal),
        multi_goal_overlap_count=len(coverage.multi_goal_event_ids),
        therapist_review_count=len(reviews),
        clinical_action_count=len(actions),
        documented_management_minutes=minutes,
        synchronous_interaction_count=len(month_interactions),
        real_time_interactive_communication_present=has_real_time,
        documentation_missing_flags=tuple(
            f.value for f in coding_view.missing_requirements),
        technology_regulatory_status=status,
    )
