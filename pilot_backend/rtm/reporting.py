"""pilot_backend/rtm/reporting.py — assemble the month-end report.

Sections A through K, built from records that already exist. Pure functions:
the service persists, this decides what each section indexes.

## Sections hold counts and REFERENCES, never clinical prose

No observation text, clinical interpretation, action narrative or activity
description is copied into a section. The report is an index over evidence,
and rendering it for a human is a presentation concern. Keeping the text out
keeps the report safe to count, query, audit and store.

## Section C declares its overlap

Per-goal attributed counts overlap when an activity serves several goals.
Section C sets `overlap_declared=True` and carries the overlap count, so a
renderer cannot present the per-goal figures as an additive breakdown of the
total without contradicting the record it is rendering.

## Section D is attributed to the caregiver

A parent observation is the family's. `attributed_to_role="caregiver"` is
stored so a report cannot restate it as a clinician finding — the one part of
the month the family contributed should not be absorbed into clinical voice.

## Section K says what it is

Coding assistance is carried as candidates plus the missing-requirement
flags, never as a conclusion. `TECHNOLOGY_STATUS_UNRESOLVED` and
`CLINICIAN_CONFIRMATION_REQUIRED` travel with it into the report.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from ..domain.month_end import MonthEndReport, ReportSection, SectionContent
from ..domain.rtm_summary import CodingAssistanceSummary, RTMEvidenceSummary


def _goals_section(evidence: RTMEvidenceSummary,
                   snapshots: Sequence) -> SectionContent:
    """A. Snapshotted goal wording, approver, priority and emphasis.

    Reads `MonthlyGoalSnapshot` from 0.4C — the wording as it read when the
    month was activated, not as it reads today. The snapshot id is carried;
    the text is not copied in.
    """
    return SectionContent(
        section=ReportSection.MONTHLY_GOALS,
        record_refs=tuple(sorted(s.snapshot_id for s in snapshots)),
        counts=tuple(sorted(
            (f"rank_{s.priority_rank}_emphasis", s.emphasis_weight)
            for s in snapshots)),
        labels=tuple(sorted(
            (s.goal_id, s.approved_by_role.value) for s in snapshots)),
    )


def _engagement_section(evidence: RTMEvidenceSummary) -> SectionContent:
    """B. Factual attempts and outcomes. `distinct_observed_local_dates` is
    a count of dates, never a qualification."""
    return SectionContent(
        section=ReportSection.ENGAGEMENT,
        counts=(
            ("distinct_attempts", evidence.total_distinct_observation_events),
            ("did_it", evidence.did_it_count),
            ("wasnt_ready_yet", evidence.wasnt_ready_yet_count),
            ("didnt_want_to_try", evidence.didnt_want_to_try_count),
            ("distinct_observed_local_dates",
             evidence.distinct_observed_local_dates),
        ),
    )


def _attribution_section(evidence: RTMEvidenceSummary) -> SectionContent:
    """C. Per-goal attribution WITH the overlap declared."""
    counts = []
    labels = []
    for line in evidence.per_goal_evidence:
        counts.append((f"{line.goal_id}_attempts", line.attributed_attempts))
        counts.append((f"{line.goal_id}_completions",
                       line.attributed_completions))
        labels.append((line.goal_id, line.status_recommendation.value))
    counts.append(("multi_goal_overlap", evidence.multi_goal_overlap_count))
    # The total is carried alongside so a reader can see directly that it is
    # not the sum of the per-goal figures.
    counts.append(("distinct_total", evidence.total_distinct_observation_events))
    return SectionContent(
        section=ReportSection.GOAL_ATTRIBUTION,
        counts=tuple(counts), labels=tuple(sorted(labels)),
        overlap_declared=True,
    )


def _parent_observations_section(events: Sequence) -> SectionContent:
    """D. The family's observations, attributed to the family."""
    return SectionContent(
        section=ReportSection.PARENT_OBSERVATIONS,
        record_refs=tuple(sorted(e.event_id for e in events)),
        counts=(("observation_count", len(events)),),
        attributed_to_role="caregiver",
    )


def _adaptations_section(adaptations: Sequence,
                         gaps: Sequence) -> SectionContent:
    """E. What changed between weeks, why, and by whose decision."""
    return SectionContent(
        section=ReportSection.WEEKLY_ADAPTATIONS,
        record_refs=tuple(sorted(a.record_id for a in adaptations)),
        counts=(("adaptation_count", len(adaptations)),
                ("coverage_gap_count", len(gaps))),
        labels=tuple(sorted((a.record_id, a.origin.value)
                            for a in adaptations)),
    )


def _review_section(reviews: Sequence) -> SectionContent:
    """F. Clinician-authored interpretation. Referenced, never copied."""
    return SectionContent(
        section=ReportSection.THERAPIST_REVIEW,
        record_refs=tuple(sorted(r.review_id for r in reviews)),
        counts=(("review_count", len(reviews)),),
        attributed_to_role="provider",
    )


def _actions_section(actions: Sequence) -> SectionContent:
    """G. Documented clinician decisions, by type. Narrative stays out."""
    return SectionContent(
        section=ReportSection.CLINICAL_ACTIONS,
        record_refs=tuple(sorted(a.action_id for a in actions)),
        counts=(("action_count", len(actions)),),
        labels=tuple(sorted((a.action_id, a.action_type.value)
                            for a in actions)),
        attributed_to_role="provider",
    )


def _time_section(entries: Sequence, minutes: int) -> SectionContent:
    """H. Documented minutes from CURRENT entries only."""
    current = [e for e in entries if e.is_current]
    return SectionContent(
        section=ReportSection.DOCUMENTED_TIME,
        record_refs=tuple(sorted(e.time_entry_id for e in current)),
        counts=(("documented_management_minutes", minutes),
                ("current_entry_count", len(current)),
                ("superseded_entry_count", len(entries) - len(current))),
        labels=(("entry_method", "manual"),),
        attributed_to_role="provider",
    )


def _interactions_section(interactions: Sequence) -> SectionContent:
    """I. Real-time contacts that actually happened."""
    qualifying = [i for i in interactions
                  if i.counts_as_real_time_communication]
    return SectionContent(
        section=ReportSection.SYNCHRONOUS_INTERACTIONS,
        record_refs=tuple(sorted(i.interaction_id for i in interactions)),
        counts=(("interaction_count", len(interactions)),
                ("real_time_with_patient_or_caregiver", len(qualifying))),
        labels=tuple(sorted((i.interaction_id, i.modality.value)
                            for i in interactions)),
    )


def _evidence_section(evidence: RTMEvidenceSummary) -> SectionContent:
    """J. The derived factual summary."""
    return SectionContent(
        section=ReportSection.RTM_EVIDENCE,
        record_refs=(evidence.summary_id,),
        counts=(("documented_management_minutes",
                 evidence.documented_management_minutes),
                ("synchronous_interaction_count",
                 evidence.synchronous_interaction_count)),
        labels=(("technology_regulatory_status",
                 evidence.technology_regulatory_status.value),
                ("real_time_interactive_communication_present",
                 str(evidence.real_time_interactive_communication_present))),
    )


def _coding_section(coding: CodingAssistanceSummary) -> SectionContent:
    """K. POTENTIAL candidates plus every missing requirement.

    The flags travel with the candidates deliberately: a reader must not be
    able to take the codes without the unresolved technology status and the
    confirmation requirement.
    """
    return SectionContent(
        section=ReportSection.CODING_ASSISTANCE,
        record_refs=(coding.coding_summary_id,),
        counts=tuple((f"potential_{code}_units", units)
                     for code, units in coding.potential_code_candidates),
        labels=(
            (("coding_rule_set", coding.coding_rule_set_id),
             ("coding_rule_version", coding.coding_rule_version),
             ("clinician_confirmation_status",
              coding.clinician_confirmation_status.value))
            + tuple(("missing_requirement", f.value)
                    for f in coding.missing_requirement_flags)
        ),
    )


def build_sections(evidence: RTMEvidenceSummary,
                   coding: CodingAssistanceSummary, *,
                   goal_snapshots: Sequence = (),
                   events: Sequence = (),
                   adaptations: Sequence = (),
                   coverage_gaps: Sequence = (),
                   reviews: Sequence = (),
                   actions: Sequence = (),
                   time_entries: Sequence = (),
                   minutes: int = 0,
                   interactions: Sequence = ()
                   ) -> Tuple[SectionContent, ...]:
    """Sections A through K, in order."""
    return (
        _goals_section(evidence, goal_snapshots),
        _engagement_section(evidence),
        _attribution_section(evidence),
        _parent_observations_section(events),
        _adaptations_section(adaptations, coverage_gaps),
        _review_section(reviews),
        _actions_section(actions),
        _time_section(time_entries, minutes),
        _interactions_section(interactions),
        _evidence_section(evidence),
        _coding_section(coding),
    )
