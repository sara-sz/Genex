"""0.4F/G RTM evidence and reporting against a REAL Firestore emulator.

The `pilot_backend` suite runs the same service code over `FakeDocumentStore`,
which is a plain dict and NOT thread-safe. Two claims cannot be made there and
are made here instead:

**The period claim actually wins races.** Eight threads opening a monitoring
period for the same episode-month against a real server, exactly one survivor.

**The claim+record write is all-or-nothing.** Fault injection inside the
activation transaction persists neither the claim nor the period, and the key
is not consumed — a clean retry succeeds.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth import VerifiedToken, resolve_principal
from pilot_backend.coding.rules import ConfirmationStatus, MissingRequirement
from pilot_backend.domain.goals import EditType, EvidenceSource
from pilot_backend.domain.identity_claims import ClaimKind, key_digest
from pilot_backend.domain.observation import AttemptOutcome
from pilot_backend.domain.rtm import RegulatoryStatus, RTMMonitoringPeriod
from pilot_backend.domain.rtm_documentation import (
    ClinicalActionType,
    InteractionModality,
    ParticipantType,
)
from pilot_backend.domain.source_link import SourceSystem
from pilot_backend.goals.service import GoalService
from pilot_backend.goals.suggestion_engine import ObservationSnapshot, ObservedDomain
from pilot_backend.identity import LongitudinalIdentityService
from pilot_backend.persistence import encode
from pilot_backend.persistence.codecs import decode
from pilot_backend.planning.service import MonthlyPlanService
from pilot_backend.rtm.errors import RTMConflict
from pilot_backend.rtm.service import RTMService
from pilot_backend.weekly.allocator import CandidateActivity
from pilot_backend.weekly.service import WeeklyService

from ..test_sentinels import ALL_SENTINELS

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
CYCLE = "2026-10"
ZONE = "America/New_York"


class _AdvancingClock:
    """Deterministic, strictly increasing and thread-safe — see 0.4A."""

    def __init__(self, start: datetime) -> None:
        self._start = start.replace(microsecond=0)
        self._tick = 0
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            self._tick += 1
            tick = self._tick
        return self._start + timedelta(seconds=tick)


@pytest.fixture()
def rt(repos, topology, unique_suffix):
    """Every service over the real emulator-backed repositories."""
    from pilot_backend.fixtures.secure_topology import (
        CAREGIVER_ALPHA_SUBJECT,
        PROVIDER_ALPHA_SUBJECT,
    )

    recorder = AuditRecorder(repos.audit_events, environment="test")
    clock = _AdvancingClock(T0)
    kw = dict(repos=repos, recorder=recorder, now=clock)

    class Bundle:
        pass

    bundle = Bundle()
    bundle.repos = repos
    bundle.topo = topology
    bundle.suffix = unique_suffix
    bundle.identity = LongitudinalIdentityService(**kw)
    bundle.goals = GoalService(**kw)
    bundle.plans = MonthlyPlanService(**kw)
    bundle.weekly = WeeklyService(**kw)
    bundle.rtm = RTMService(**kw)
    bundle.caregiver_alpha = resolve_principal(
        VerifiedToken(subject=CAREGIVER_ALPHA_SUBJECT + unique_suffix), repos)
    bundle.provider_alpha = resolve_principal(
        VerifiedToken(subject=PROVIDER_ALPHA_SUBJECT + unique_suffix), repos)
    bundle.child = topology.child_alpha.child_id
    return bundle


def _snapshot(child_id):
    return ObservationSnapshot(child_id, CYCLE, (
        ObservedDomain("talking_and_communicating", True,
                       EvidenceSource.CLINICIAN_OBSERVATION,
                       milestone_refs=("mv1:cdc:comm:24m:two-word",)),
        ObservedDomain("social_and_emotional", True,
                       EvidenceSource.CAREGIVER_REPORTED_MILESTONE,
                       milestone_refs=("mv1:cdc:social:24m:turns",)),
    ))


def _prepare(rt):
    """Identity, goals, active month, allocated cycle, open episode."""
    rt.identity.assign_managing_clinician(
        rt.provider_alpha, rt.child, rt.topo.provider_alpha.provider_id)
    offered = rt.goals.generate_suggestions(rt.provider_alpha, rt.child,
                                            _snapshot(rt.child))
    goals = [rt.goals.approve_clinical_goal(
        rt.provider_alpha, rt.child, edit_type=EditType.ACCEPTED_VERBATIM,
        suggestion_id=x.suggestion_id) for x in offered]
    plan = rt.plans.create_plan(rt.provider_alpha, rt.child, CYCLE, ZONE)
    for rank, goal in enumerate(goals, start=1):
        rt.plans.allocate_goal(rt.provider_alpha, plan.focus_plan_id,
                               goal.ref, priority_rank=rank)
    rt.plans.activate_plan(rt.provider_alpha, plan.focus_plan_id)

    cycle = rt.weekly.create_cycle(rt.provider_alpha, plan.focus_plan_id,
                                   sequence_in_month=1)
    g1, g2 = goals[0].ref, goals[1].ref
    candidates = (
        CandidateActivity("activity-a", (g1,), primary_for=g1),
        CandidateActivity("activity-c", (g1, g2), primary_for=g1),
    )
    result = rt.weekly.allocate_cycle(rt.provider_alpha, cycle.cycle_id,
                                      candidates, family_declared_capacity=2)
    episode = rt.rtm.open_episode(rt.provider_alpha, rt.child,
                                  [g.ref for g in goals])
    rt.rtm.declare_technology(rt.provider_alpha, episode.episode_id,
                              "Genex RTM pilot")
    return plan, cycle, result, goals, episode


def _race(target, count: int = 8):
    """Run `target(i)` on `count` threads released together.

    Every thread is accounted for, so a stalled harness reports itself
    rather than returning a short list that reads as a uniqueness failure —
    see `test_identity_emulator.py` for the incident that taught this.
    """
    barrier = threading.Barrier(count)
    results, errors = [], []
    lock = threading.Lock()

    def runner(index: int) -> None:
        barrier.wait()
        try:
            outcome = target(index)
            with lock:
                results.append(outcome)
        except Exception as exc:  # noqa: BLE001 - classified by the caller
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    stalled = 0
    for thread in threads:
        thread.join(timeout=60)
        if thread.is_alive():
            stalled += 1
    assert stalled == 0, (
        f"{stalled} of {count} racing threads did not finish within 60s — "
        "the harness stalled; this is not a uniqueness result")
    assert len(results) + len(errors) == count
    return results, errors


# ===========================================================================
# round-trips through the real adapter
# ===========================================================================

def test_every_rtm_record_round_trips_in_real_firestore(rt):
    plan, cycle, result, goals, episode = _prepare(rt)
    period = rt.rtm.open_period(rt.provider_alpha, episode.episode_id,
                                plan.focus_plan_id)

    rt.rtm.record_time(rt.provider_alpha, period.period_id,
                       local_date="2026-10-05", minutes=25,
                       activity_description="review and plan adjustment")
    second = rt.rtm.record_time(rt.provider_alpha, period.period_id,
                                local_date="2026-10-12", minutes=30,
                                activity_description="caregiver coaching")
    rt.rtm.correct_time(rt.provider_alpha, second.time_entry_id, minutes=17,
                        reason="mis-keyed")
    rt.rtm.record_synchronous_interaction(
        rt.provider_alpha, period.period_id, local_date="2026-10-14",
        modality=InteractionModality.PHONE,
        participant_type=ParticipantType.CAREGIVER)
    review = rt.rtm.record_review(rt.provider_alpha, period.period_id,
                                  clinical_interpretation="Steady progress.")
    rt.rtm.record_clinical_action(
        rt.provider_alpha, review.review_id,
        action_type=ClinicalActionType.CONTINUE_PLAN, narrative="Continue.")
    rt.rtm.finalize_period(rt.provider_alpha, period.period_id)
    evidence = rt.rtm.generate_evidence_summary(rt.provider_alpha,
                                                period.period_id)
    coding = rt.rtm.generate_coding_assistance(rt.provider_alpha,
                                               period.period_id)
    report = rt.rtm.generate_report(rt.provider_alpha, period.period_id)

    for record in (episode,
                   rt.repos.rtm_periods.get_by_id(period.period_id),
                   *rt.repos.rtm_technologies.list_for_episode(episode.episode_id),
                   *rt.repos.therapist_reviews.list_for_period(period.period_id),
                   *rt.repos.clinical_actions.list_for_period(period.period_id),
                   *rt.repos.time_entries.list_for_period(period.period_id),
                   *rt.repos.synchronous_interactions.list_for_period(period.period_id),
                   evidence, coding, report):
        assert decode(type(record), encode(record)) == record

    # 42 CURRENT minutes reached through a correction — the no-double-count
    # property holding against a real store.
    assert coding.documented_management_minutes == 42
    assert coding.potential_code_candidates == (("98980", 1), ("98981", 1))


def test_nested_section_content_survives_the_real_client(rt):
    plan, cycle, result, goals, episode = _prepare(rt)
    period = rt.rtm.open_period(rt.provider_alpha, episode.episode_id,
                                plan.focus_plan_id)
    rt.rtm.finalize_period(rt.provider_alpha, period.period_id)
    rt.rtm.generate_evidence_summary(rt.provider_alpha, period.period_id)
    rt.rtm.generate_coding_assistance(rt.provider_alpha, period.period_id)
    report = rt.rtm.generate_report(rt.provider_alpha, period.period_id)

    stored = rt.repos.month_end_reports.get_by_id(report.report_id)
    assert stored == report
    assert len(stored.sections) == 11
    # Ordered pair lists survive: a codec that sorted them would make two
    # different reports compare equal.
    assert [c.section for c in stored.sections] == \
        [c.section for c in report.sections]


# ===========================================================================
# contention — only provable here
# ===========================================================================

def test_concurrent_period_opens_yield_exactly_one(rt):
    """Eight threads, one episode-month. One survivor."""
    plan, cycle, result, goals, episode = _prepare(rt)

    def attempt(index: int):
        return rt.rtm.open_period(rt.provider_alpha, episode.episode_id,
                                  plan.focus_plan_id)

    results, errors = _race(attempt)
    assert len(results) == 1, f"expected one winner, got {len(results)}"
    assert len(errors) == 7
    assert all(isinstance(e, RTMConflict) for e in errors), \
        {type(e).__name__ for e in errors}

    periods = rt.repos.rtm_periods.list_for_episode(episode.episode_id)
    assert len(periods) == 1
    assert rt.repos.identity_claims.count_claims_for_key(
        key_digest(episode.episode_id, CYCLE)) == 1


def test_a_crash_inside_the_period_transaction_persists_nothing(rt):
    """Fault injection against the real server, not the fake."""
    plan, cycle, result, goals, episode = _prepare(rt)
    real_create = rt.repos.rtm_periods.__class__.create

    def exploding_create(self, period):
        raise RuntimeError("process died mid-transaction")

    rt.repos.rtm_periods.__class__.create = exploding_create
    try:
        with pytest.raises(RuntimeError):
            rt.rtm.open_period(rt.provider_alpha, episode.episode_id,
                               plan.focus_plan_id)
    finally:
        rt.repos.rtm_periods.__class__.create = real_create

    assert rt.repos.rtm_periods.list_for_episode(episode.episode_id) == []
    assert rt.repos.identity_claims.count_claims_for_key(
        key_digest(episode.episode_id, CYCLE)) == 0

    # The key was not consumed: a clean retry succeeds.
    period = rt.rtm.open_period(rt.provider_alpha, episode.episode_id,
                                plan.focus_plan_id)
    assert period.period_id


# ===========================================================================
# history that must survive a real store
# ===========================================================================

def test_finalizing_a_month_leaves_the_episode_open_in_firestore(rt):
    plan, cycle, result, goals, episode = _prepare(rt)
    period = rt.rtm.open_period(rt.provider_alpha, episode.episode_id,
                                plan.focus_plan_id)
    rt.rtm.finalize_period(rt.provider_alpha, period.period_id)

    stored = rt.repos.rtm_episodes.get_by_id(episode.episode_id)
    assert stored.is_open, "the episode closed when the month did"
    assert stored.closed_at is None


def test_a_correction_keeps_both_rows_in_firestore(rt):
    plan, cycle, result, goals, episode = _prepare(rt)
    period = rt.rtm.open_period(rt.provider_alpha, episode.episode_id,
                                plan.focus_plan_id)
    first = rt.rtm.record_time(rt.provider_alpha, period.period_id,
                               local_date="2026-10-05", minutes=30,
                               activity_description="d")
    correction = rt.rtm.correct_time(rt.provider_alpha, first.time_entry_id,
                                     minutes=12, reason="mis-keyed")

    rows = rt.repos.time_entries.list_for_period(period.period_id)
    assert len(rows) == 2
    assert rt.repos.time_entries.get_by_id(
        first.time_entry_id).superseded_by_time_entry_id == \
        correction.time_entry_id
    assert rt.rtm.documented_minutes_for(rt.provider_alpha,
                                         period.period_id) == 12
    assert sum(r.minutes for r in rows) == 42, "raw sum would double-count"


def test_an_amended_report_keeps_its_predecessor_in_firestore(rt):
    plan, cycle, result, goals, episode = _prepare(rt)
    period = rt.rtm.open_period(rt.provider_alpha, episode.episode_id,
                                plan.focus_plan_id)
    rt.rtm.finalize_period(rt.provider_alpha, period.period_id)
    rt.rtm.generate_evidence_summary(rt.provider_alpha, period.period_id)
    rt.rtm.generate_coding_assistance(rt.provider_alpha, period.period_id)
    report = rt.rtm.generate_report(rt.provider_alpha, period.period_id)
    finalized = rt.rtm.finalize_report(rt.provider_alpha, report.report_id)

    amended = rt.rtm.amend_report(rt.provider_alpha, finalized.report_id,
                                  reason="late reconciliation")
    chain = rt.repos.month_end_reports.chain_for_period(period.period_id)
    versions = [r.version for r in chain if r.is_finalized]
    assert 1 in versions and 2 in versions
    predecessor = rt.repos.month_end_reports.get_by_id(finalized.report_id)
    assert predecessor.superseded_by_report_id == amended.report_id
    assert predecessor.sections == finalized.sections


def test_a_rejected_amendment_has_zero_durable_side_effects_in_firestore(rt):
    """The atomicity claim against a REAL store, not a dict.

    Persistence is involved — `amend_report` calls `generate_report`, which
    WRITES — so the unit test over the fake is not sufficient evidence. This
    asserts the same five conditions against Firestore.
    """
    from pilot_backend.audit.events import AuditAction
    from pilot_backend.domain.month_end import ReportState
    from pilot_backend.rtm.errors import RTMValidationError

    plan, cycle, result, goals, episode = _prepare(rt)
    period = rt.rtm.open_period(rt.provider_alpha, episode.episode_id,
                                plan.focus_plan_id)
    rt.rtm.finalize_period(rt.provider_alpha, period.period_id)
    rt.rtm.generate_evidence_summary(rt.provider_alpha, period.period_id)
    rt.rtm.generate_coding_assistance(rt.provider_alpha, period.period_id)
    report = rt.rtm.generate_report(rt.provider_alpha, period.period_id)
    finalized = rt.rtm.finalize_report(rt.provider_alpha, report.report_id)

    before_rows = rt.repos.month_end_reports.list_for_period(period.period_id)
    before_ids = {r.report_id for r in before_rows}
    before_source = rt.repos.month_end_reports.get_by_id(finalized.report_id)
    before_audit = len(rt.repos.audit_events.list_for_child(rt.child))

    with pytest.raises(RTMValidationError):
        rt.rtm.amend_report(rt.provider_alpha, finalized.report_id,
                            reason="   ")

    after_rows = rt.repos.month_end_reports.list_for_period(period.period_id)
    after_source = rt.repos.month_end_reports.get_by_id(finalized.report_id)

    assert {r.report_id for r in after_rows} == before_ids, \
        "a rejected amendment persisted a row in Firestore"
    assert after_source == before_source, \
        "a rejected amendment mutated the finalized source report"
    assert after_source.state is ReportState.FINALIZED
    assert after_source.superseded_by_report_id is None
    assert not [r for r in after_rows if r.supersedes_report_id]

    after_events = rt.repos.audit_events.list_for_child(rt.child)
    assert len(after_events) == before_audit, \
        "a rejected amendment emitted an audit event"
    assert not [e for e in after_events
                if e.action is AuditAction.MONTH_END_REPORT_AMENDED]

    assert rt.repos.month_end_reports.current_for_period(
        period.period_id).report_id == finalized.report_id

    # And a legitimate amendment still succeeds afterwards.
    amended = rt.rtm.amend_report(rt.provider_alpha, finalized.report_id,
                                  reason="late reconciliation")
    assert amended.version == 2


def test_a_coding_decision_persists_without_rewriting_candidates(rt):
    plan, cycle, result, goals, episode = _prepare(rt)
    period = rt.rtm.open_period(rt.provider_alpha, episode.episode_id,
                                plan.focus_plan_id)
    rt.rtm.record_time(rt.provider_alpha, period.period_id,
                       local_date="2026-10-05", minutes=42,
                       activity_description="d")
    rt.rtm.record_synchronous_interaction(
        rt.provider_alpha, period.period_id, local_date="2026-10-06",
        modality=InteractionModality.VIDEO,
        participant_type=ParticipantType.BOTH)
    generated = rt.rtm.generate_coding_assistance(rt.provider_alpha,
                                                  period.period_id)
    decided = rt.rtm.decide_coding_assistance(
        rt.provider_alpha, generated.coding_summary_id,
        ConfirmationStatus.CONFIRMED)

    stored = rt.repos.coding_summaries.get_by_id(generated.coding_summary_id)
    assert stored.potential_code_candidates == \
        generated.potential_code_candidates
    assert stored.rule_explanations == generated.rule_explanations
    assert stored.missing_requirement_flags == \
        generated.missing_requirement_flags
    assert stored.clinician_confirmation_status is ConfirmationStatus.CONFIRMED
    assert stored.technology_regulatory_status is RegulatoryStatus.UNDER_REVIEW


# ===========================================================================
# leakage
# ===========================================================================

def test_audit_events_persist_and_leak_no_clinical_text(rt):
    plan, cycle, result, goals, episode = _prepare(rt)
    period = rt.rtm.open_period(rt.provider_alpha, episode.episode_id,
                                plan.focus_plan_id)
    rt.rtm.record_time(rt.provider_alpha, period.period_id,
                       local_date="2026-10-05", minutes=20,
                       activity_description=ALL_SENTINELS[1])
    rt.rtm.record_review(rt.provider_alpha, period.period_id,
                         clinical_interpretation=ALL_SENTINELS[3])

    events = rt.repos.audit_events.list_for_child(rt.child)
    assert events
    for event in events:
        blob = " ".join([event.resource_type, event.resource_id or "",
                         *event.metadata.keys(), *event.metadata.values()])
        for sentinel in ALL_SENTINELS:
            assert sentinel not in blob, event.action


def test_rtm_collections_are_pilot_prefixed(rt):
    plan, cycle, result, goals, episode = _prepare(rt)
    period = rt.rtm.open_period(rt.provider_alpha, episode.episode_id,
                                plan.focus_plan_id)
    rt.rtm.finalize_period(rt.provider_alpha, period.period_id)
    rt.rtm.generate_evidence_summary(rt.provider_alpha, period.period_id)
    rt.rtm.generate_coding_assistance(rt.provider_alpha, period.period_id)
    for name in ("pilot_rtm_episodes", "pilot_rtm_periods",
                 "pilot_rtm_technologies", "pilot_rtm_evidence_summaries",
                 "pilot_coding_assistance_summaries"):
        assert list(rt.repos.store.list_all(name)), name
