"""RTM billing-evidence v2 — billing months are not clinical plans.

Clinical plans are rolling 28-day clinician-approved periods (P0-C). An RTM
treatment-management period is (episode, billing calendar month). These tests
prove a billing month collects every CURRENT observation attributed to it by its
own local date — whichever clinical plan owned the cycle — and that a superseded
observation and its correction never both count.

The worlds are the P0-C route-shaped worlds (real release path, real bank,
real caregiver recording path), so the plans, renewals and cycles are produced
exactly as production would produce them.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from pilot_backend.domain.goals import GoalKind, GoalRef
from pilot_backend.domain.month_end import MONTH_END_REPORT_SCHEMA_VERSION
from pilot_backend.domain.observation import AttemptOutcome
from pilot_backend.domain.rtm import RTM_PERIOD_SCHEMA_VERSION
from pilot_backend.domain.rtm_summary import (
    EVIDENCE_RULE_VERSION,
    RTM_EVIDENCE_SUMMARY_SCHEMA_VERSION,
)
from pilot_backend.persistence import encode
from pilot_backend.persistence.codecs import CodecError, decode
from pilot_backend.domain.rtm import RTMMonitoringPeriod
from pilot_backend.domain.rtm_summary import RTMEvidenceSummary
from pilot_backend.domain.month_end import MonthEndReport
from pilot_backend.rtm.errors import RTMValidationError
from pilot_backend.rtm.service import RTMService

from .test_p0c_month_boundary import Boundary, CHILD
from .test_rtm_evidence_reporting import s  # noqa: F401 — the legacy-world fixture


class Billing:
    """A P0-C world plus an RTM episode run by its managing clinician."""

    def __init__(self, first_day="2026-11-17"):
        self.b = Boundary(first_day=first_day)
        self.rtm = RTMService(repos=self.b.repos, recorder=self.b.world.recorder,
                              now=self.b.clock)
        self.b.at(first_day[:10])
        self.episode = self.rtm.open_episode(
            self.b.provider, CHILD,
            [GoalRef(GoalKind.CLINICAL, self.b.goal.clinical_goal_id)])
        self._fb = 0

    @property
    def plan_a(self):
        return self.b.week_one.focus_plan_id

    def observe(self, week, day, *, outcome=AttemptOutcome.DID_IT,
                supersedes=""):
        """The caregiver path, with an explicit feedback lineage."""
        self.b.at(day)
        ref = sorted(a["activity_ref"] for d in week.parent_week["days"]
                     for a in d["activities"])[0]
        instance = self.b.weekly.resolve_parent_activity_ref(
            self.b.caregiver, week.cycle_id, ref)
        self._fb += 1
        return self.b.weekly.record_observation(
            self.b.caregiver, week.cycle_id, instance, local_date=day,
            attempt_outcome=outcome, source_feedback_id=f"fb_rtm_{self._fb:03d}",
            supersedes_source_feedback_id=supersedes)

    def open(self, month, plan_id=None, when=None):
        self.b.at(when or f"{month}-15")
        return self.rtm.open_period(self.b.provider, self.episode.episode_id,
                                    plan_id or self.plan_a, cycle_month=month)

    def summary(self, period):
        self.b.at("2027-03-01")
        return self.rtm.generate_evidence_summary(self.b.provider,
                                                  period.period_id)

    def dates(self, summary, period):
        return sorted({e.local_date for c in summary.cycle_refs
                       for e in self.b.repos.observation_events.list_for_cycle(c)
                       if e.attribution_month == period.cycle_month})


@pytest.fixture
def a():
    """Scenario A world: Plan A (label 2026-11) from Nov 17, Weeks 1-4,
    observations in November AND December, and one corrected observation."""
    w = Billing()
    b = w.b
    w.weeks = {1: b.week_one, 2: b.release("2026-11-23"),
               3: b.release("2026-11-30"), 4: b.release("2026-12-07")}
    w.original = w.observe(w.weeks[1], "2026-11-18")
    w.correction = w.observe(w.weeks[1], "2026-11-18",
                             outcome=AttemptOutcome.DIDNT_WANT_TO_TRY,
                             supersedes=w.original.source_feedback_id)
    w.nov = [w.observe(w.weeks[2], "2026-11-24"),
             w.observe(w.weeks[3], "2026-11-30")]
    w.dec_a = [w.observe(w.weeks[3], "2026-12-02"),
               w.observe(w.weeks[4], "2026-12-09")]
    return w


# ===========================================================================
# A — one plan crosses the month boundary
# ===========================================================================


def test_a_november_sees_every_current_november_observation(a):
    nov = a.open("2026-11")
    s = a.summary(nov)
    assert a.dates(s, nov) == ["2026-11-18", "2026-11-24", "2026-11-30"]
    assert s.total_distinct_observation_events == 3, "the correction, not both"
    assert s.distinct_observed_local_dates == 3
    assert s.contributing_focus_plan_ids == (a.plan_a,)


def test_a_december_opens_without_a_december_labelled_plan(a):
    assert {p.cycle_month for p in a.b.plan_rows()} == {"2026-11"}
    dec = a.open("2026-12")
    assert dec.cycle_month == "2026-12" and dec.focus_plan_id == a.plan_a
    s = a.summary(dec)
    assert a.dates(s, dec) == ["2026-12-02", "2026-12-09"]
    assert s.total_distinct_observation_events == 2
    assert s.contributing_focus_plan_ids == (a.plan_a,)


# ===========================================================================
# B — renewal inside December: one billing month, two clinical plans
# ===========================================================================


@pytest.fixture
def b(a):
    plan_b = a.b.renewal_plan(month="2026-12", when="2026-12-14")
    a.plan_b = plan_b.focus_plan_id
    a.weeks[5] = a.b.release("2026-12-14")
    a.weeks[6] = a.b.release("2026-12-21")
    a.dec_b = [a.observe(a.weeks[5], "2026-12-15"),
               a.observe(a.weeks[6], "2026-12-22")]
    return a


@pytest.mark.parametrize("opened_with", ["plan_a", "plan_b"])
def test_b_one_december_period_sees_both_plans(b, opened_with):
    dec = b.open("2026-12", plan_id=getattr(b, opened_with), when="2026-12-20")
    s = b.summary(dec)
    assert b.dates(s, dec) == ["2026-12-02", "2026-12-09", "2026-12-15",
                               "2026-12-22"], "the opening plan scopes nothing"
    assert s.total_distinct_observation_events == 4
    assert s.contributing_focus_plan_ids == tuple(sorted([b.plan_a, b.plan_b]))
    assert s.focus_plan_ref == getattr(b, opened_with), "opening provenance"


def test_b_a_review_may_cite_either_plans_december_evidence(b):
    dec = b.open("2026-12", when="2026-12-20")
    b.b.at("2026-12-23")
    review = b.rtm.record_review(
        b.b.provider, dec.period_id, clinical_interpretation="reviewed",
        reviewed_event_ids=[b.dec_a[1].event_id, b.dec_b[0].event_id])
    assert set(review.reviewed_event_ids) == {b.dec_a[1].event_id,
                                             b.dec_b[0].event_id}


def test_b_the_report_keeps_each_plans_provenance(b):
    dec = b.open("2026-12", when="2026-12-20")
    b.b.at("2027-01-02")
    b.rtm.generate_evidence_summary(b.b.provider, dec.period_id)
    b.rtm.generate_coding_assistance(b.b.provider, dec.period_id)
    report = b.rtm.generate_report(b.b.provider, dec.period_id)
    assert report.contributing_focus_plan_ids == tuple(sorted([b.plan_a,
                                                               b.plan_b]))
    assert report.focus_plan_id == b.plan_a
    goals = report.sections[0]
    plans_in_labels = {label.split(":")[0] for label, _role in goals.labels}
    assert plans_in_labels == {b.plan_a, b.plan_b}, \
        "each plan's goal snapshot is kept under its own plan"
    snapshot_plans = {b.b.repos.goal_snapshots.get_by_id(ref).focus_plan_id
                      for ref in goals.record_refs}
    assert snapshot_plans == {b.plan_a, b.plan_b}


# ===========================================================================
# C — a mid-December pilot crossing into January
# ===========================================================================


def test_c_january_sees_both_plans_without_a_january_label():
    w = Billing(first_day="2026-12-10")
    b = w.b
    weeks = {1: b.week_one, 2: b.release("2026-12-14"),
             3: b.release("2026-12-21"), 4: b.release("2026-12-28")}
    w.observe(weeks[1], "2026-12-11")
    jan2 = w.observe(weeks[4], "2027-01-02")
    # Opened on Jan 2, BEFORE any renewal: no plan carries a 2027-01 label.
    jan = w.open("2027-01", when="2027-01-02")
    assert {p.cycle_month for p in b.plan_rows()} == {"2026-12"}
    c2 = b.renewal_plan(month="2026-12", when="2027-01-04")  # honest Dec label
    weeks[5] = b.release("2027-01-04")
    jan5 = w.observe(weeks[5], "2027-01-05")
    s = w.summary(jan)
    assert w.dates(s, jan) == ["2027-01-02", "2027-01-05"]
    assert s.total_distinct_observation_events == 2
    assert s.contributing_focus_plan_ids == tuple(sorted(
        [w.plan_a, c2.focus_plan_id]))
    assert {jan2.event_id, jan5.event_id} <= {
        e.event_id for c in s.cycle_refs
        for e in b.repos.observation_events.list_for_cycle(c)}


# ===========================================================================
# D — corrections: storage keeps both rows, RTM counts the current one
# ===========================================================================


def test_d_a_correction_never_counts_twice(a):
    stored = [e for e in a.b.repos.observation_events.list_for_child(CHILD)
              if e.local_date == "2026-11-18"]
    assert {e.event_id for e in stored} == {a.original.event_id,
                                            a.correction.event_id}
    s = a.summary(a.open("2026-11"))
    assert s.total_distinct_observation_events == 3
    assert s.did_it_count == 2 and s.didnt_want_to_try_count == 1, \
        "the corrected outcome counts; the superseded did_it does not"
    assert s.distinct_observed_local_dates == 3


def test_d_a_review_cannot_cite_a_superseded_observation(a):
    nov = a.open("2026-11")
    a.b.at("2026-11-29")
    with pytest.raises(RTMValidationError):
        a.rtm.record_review(a.b.provider, nov.period_id,
                            clinical_interpretation="x",
                            reviewed_event_ids=[a.original.event_id])
    review = a.rtm.record_review(a.b.provider, nov.period_id,
                                 clinical_interpretation="x",
                                 reviewed_event_ids=[a.correction.event_id])
    assert review.reviewed_event_ids == (a.correction.event_id,)


# ===========================================================================
# review validation, double counting, determinism, versions
# ===========================================================================


@pytest.mark.parametrize("pick", ["other_month", "unknown"])
def test_a_review_refuses_evidence_outside_the_period(a, pick):
    nov = a.open("2026-11")
    ref = a.dec_a[0].event_id if pick == "other_month" else "obsv_nonexistent"
    a.b.at("2026-11-29")
    with pytest.raises(RTMValidationError):
        a.rtm.record_review(a.b.provider, nov.period_id,
                            clinical_interpretation="x",
                            reviewed_event_ids=[ref])


def test_a_review_refuses_another_childs_observation(a):
    """Same billing month, different child: refused, never attached."""
    # A November observation that belongs to ANOTHER child, stored in the
    # same database so it is genuinely reachable by id.
    foreign = replace(a.nov[0], child_id="chld_someone_else",
                      event_id="obsv_foreign_child_0001")
    a.b.repos.observation_events.create(foreign)
    nov = a.open("2026-11")
    a.b.at("2026-11-29")
    with pytest.raises(RTMValidationError):
        a.rtm.record_review(a.b.provider, nov.period_id,
                            clinical_interpretation="x",
                            reviewed_event_ids=[foreign.event_id])


def test_the_same_observation_is_never_counted_twice(b, monkeypatch):
    """Even if a query returned a row twice, every count stays distinct."""
    dec = b.open("2026-12", when="2026-12-20")
    real = b.b.repos.observation_events.list_for_child
    monkeypatch.setattr(b.b.repos.observation_events, "list_for_child",
                        lambda child_id: real(child_id) * 2)
    s = b.summary(dec)
    assert s.total_distinct_observation_events == 4
    assert s.did_it_count == 4 and s.distinct_observed_local_dates == 4
    gathered = b.rtm._gather(dec)
    ids = [e.event_id for e in gathered["events"]]
    assert len(ids) == len(set(ids)) == 4


def test_regeneration_is_deterministic(b):
    dec = b.open("2026-12", when="2026-12-20")
    first, second = b.summary(dec), b.summary(dec)
    strip = dict(summary_id="", generated_at=first.generated_at)
    assert replace(first, **strip) == replace(second, **strip)
    assert first.contributing_focus_plan_ids == tuple(sorted(
        set(first.contributing_focus_plan_ids)))
    assert first.rule_version == EVIDENCE_RULE_VERSION == \
        "rtm-evidence-derivation-2026-v2"


def test_the_billing_month_is_explicit_and_validated(a):
    with pytest.raises(RTMValidationError):
        a.open("2026-13", when="2026-12-01")
    jan = a.open("2027-01", when="2026-12-20")
    assert jan.cycle_month == "2027-01" and jan.focus_plan_id == a.plan_a


def test_clinician_time_stays_period_scoped_across_a_renewal(b):
    dec = b.open("2026-12", when="2026-12-01")
    b.b.at("2026-12-10")
    b.rtm.record_time(b.b.provider, dec.period_id, local_date="2026-12-10",
                      minutes=12, activity_description="data review")
    b.b.at("2026-12-20")
    b.rtm.record_time(b.b.provider, dec.period_id, local_date="2026-12-20",
                      minutes=11, activity_description="data review")
    assert b.rtm.documented_minutes_for(b.b.provider, dec.period_id) == 23
    with pytest.raises(RTMValidationError):
        b.rtm.record_time(b.b.provider, dec.period_id, local_date="2027-01-02",
                          minutes=5, activity_description="x")


@pytest.mark.parametrize("cls,version", [
    (RTMMonitoringPeriod, RTM_PERIOD_SCHEMA_VERSION),
    (RTMEvidenceSummary, RTM_EVIDENCE_SUMMARY_SCHEMA_VERSION),
    (MonthEndReport, MONTH_END_REPORT_SCHEMA_VERSION),
])
def test_v2_records_refuse_any_other_schema_version(b, cls, version):
    dec = b.open("2026-12", when="2026-12-20")
    b.b.at("2027-01-02")
    summary = b.rtm.generate_evidence_summary(b.b.provider, dec.period_id)
    b.rtm.generate_coding_assistance(b.b.provider, dec.period_id)
    report = b.rtm.generate_report(b.b.provider, dec.period_id)
    record = {RTMMonitoringPeriod: dec, RTMEvidenceSummary: summary,
              MonthEndReport: report}[cls]
    assert record.schema_version == version
    assert decode(cls, encode(record)) == record
    legacy = dict(encode(record), schema_version="october-pilot-0.1")
    with pytest.raises(CodecError, match="unsupported schema_version"):
        decode(cls, legacy)
    with pytest.raises(CodecError, match="unsupported schema_version"):
        encode(replace(record, schema_version="october-pilot-0.1"))


def test_the_pure_derivation_itself_counts_current_rows_once(a):
    """`derive_evidence_summary` is guarded on its own, not only through the
    service: handed a superseded row, its correction AND a duplicate of a
    current row, it still counts each current observation exactly once."""
    from pilot_backend.rtm.evidence import derive_evidence_summary
    nov = a.open("2026-11")
    raw = [a.original, a.correction, a.correction, *a.nov, a.nov[0]]
    s = derive_evidence_summary(nov, alignments=(), events=raw, cycles=(),
                                contributing_focus_plan_ids=(a.plan_a,),
                                now=a.b.clock())
    assert s.total_distinct_observation_events == 3
    assert (s.did_it_count, s.didnt_want_to_try_count) == (2, 1)
    assert s.distinct_observed_local_dates == 3


# ===========================================================================
# Scheduled opportunities — what was SCHEDULED, independent of what was
# received. Released activities count toward the month of their own scheduled
# local date, across every plan, observed or not.
# ===========================================================================


def _scheduled_dates(w, period):
    """Ground truth from the released weeks the family was shown."""
    out = []
    for cycle in w.b.cycles():
        if not cycle.is_released:
            continue
        snap = w.b.repos.weekly_plan_snapshots.list_for_cycle(cycle.cycle_id)[0]
        import json
        for row in json.loads(snap.resolved_plan_document)["activities"]:
            if row["local_date"][:7] == period.cycle_month:
                out.append(row["local_date"])
    return sorted(out)


def test_scheduled_activities_count_with_no_observation_at_all():
    """1. Nothing was ever submitted, and every scheduled activity counts."""
    w = Billing()
    for monday in ("2026-11-23", "2026-11-30", "2026-12-07"):
        w.b.release(monday)
    nov, dec = w.open("2026-11"), w.open("2026-12")
    s_nov, s_dec = w.summary(nov), w.summary(dec)
    # Week 1: 11-17..19; Week 2: 11-23..25; Week 3: 11-30 | 12-01, 12-02;
    # Week 4: 12-07..09. One activity per day, capacity 3.
    assert _scheduled_dates(w, nov) == ["2026-11-17", "2026-11-18",
                                        "2026-11-19", "2026-11-23",
                                        "2026-11-24", "2026-11-25",
                                        "2026-11-30"]
    assert s_nov.total_scheduled_opportunities == 7
    assert s_dec.total_scheduled_opportunities == 5, "12-01, 12-02 + Week 4"
    assert s_nov.total_distinct_observation_events == 0
    assert s_nov.distinct_observed_local_dates == 0
    (line,) = s_nov.per_goal_evidence
    assert line.scheduled_opportunities == 7
    assert line.attributed_attempts == 0
    assert s_nov.contributing_focus_plan_ids == (w.plan_a,), \
        "a plan contributing ONLY scheduled opportunities is still provenance"


def test_four_scheduled_and_two_observed_stay_separate_facts():
    """2 + 5. Fri 11-27 start: a three-day starter week (11-27..29) plus Week
    2's Monday 11-30 put exactly four activities in November. Two current
    observations; a correction must move neither count."""
    w = Billing(first_day="2026-11-27")
    week_two = w.b.release("2026-11-30")
    first = w.observe(w.b.week_one, "2026-11-27")
    w.observe(week_two, "2026-11-30")
    nov = w.open("2026-11", when="2026-11-30")
    before = w.summary(nov)
    w.observe(w.b.week_one, "2026-11-27", outcome=AttemptOutcome.WASNT_READY_YET,
              supersedes=first.source_feedback_id)
    after = w.summary(nov)
    for s in (before, after):
        assert s.total_scheduled_opportunities == 4
        assert s.per_goal_evidence[0].scheduled_opportunities == 4
        assert s.total_distinct_observation_events == 2
        assert s.distinct_observed_local_dates == 2
    assert (before.did_it_count, after.did_it_count) == (2, 1)
    assert after.wasnt_ready_yet_count == 1
    assert _scheduled_dates(w, nov) == ["2026-11-27", "2026-11-28",
                                        "2026-11-29", "2026-11-30"]


def test_scheduled_opportunities_span_both_plans_in_one_month(b):
    """3. December draws scheduled activities from Plan A AND Plan B."""
    dec = b.open("2026-12", when="2026-12-20")
    s = b.summary(dec)
    # Plan A: 12-01, 12-02 (Week 3) + 12-07..09 (Week 4) = 5
    # Plan B: 12-14..16 (Week 5) + 12-21..23 (Week 6)    = 6
    assert s.total_scheduled_opportunities == 11
    assert len(_scheduled_dates(b, dec)) == 11
    by_plan = {}
    for cycle_id in s.cycle_refs:
        plan = b.b.repos.weekly_cycles.get_by_id(cycle_id).owning_focus_plan_id
        by_plan[plan] = by_plan.get(plan, 0) + 1
    assert set(by_plan) == {b.plan_a, b.plan_b}


def test_a_plan_with_scheduled_activities_but_no_observation_is_kept(a):
    """4. Plan B releases two December weeks and nobody gives feedback: its
    opportunities count and it is named as a contributing plan."""
    plan_b = a.b.renewal_plan(month="2026-12", when="2026-12-14")
    a.b.release("2026-12-14")
    a.b.release("2026-12-21")
    dec = a.open("2026-12", when="2026-12-22")
    s = a.summary(dec)
    assert s.total_scheduled_opportunities == 11
    assert s.total_distinct_observation_events == 2, "only Plan A's 12-02, 12-09"
    assert s.contributing_focus_plan_ids == tuple(sorted(
        [a.plan_a, plan_b.focus_plan_id]))
    a.b.at("2027-01-02")
    a.rtm.generate_coding_assistance(a.b.provider, dec.period_id)
    report = a.rtm.generate_report(a.b.provider, dec.period_id)
    assert report.contributing_focus_plan_ids == s.contributing_focus_plan_ids
    engagement = dict(report.sections[1].counts)
    assert engagement["scheduled_opportunities"] == 11
    assert engagement["distinct_attempts"] == 2


def test_an_unreleased_week_schedules_nothing():
    """Only weeks given to the family are opportunities."""
    w = Billing()
    nov = w.open("2026-11")
    assert w.summary(nov).total_scheduled_opportunities == 3
    cycle = w.b.weekly.create_cycle(w.b.provider, w.plan_a, sequence_in_month=2)
    assert not cycle.is_released
    assert w.summary(nov).total_scheduled_opportunities == 3


def test_the_pure_derivation_counts_distinct_scheduled_instances(a):
    """Duplicated scheduled pairs count once; observations cannot add any."""
    from pilot_backend.rtm.evidence import derive_evidence_summary
    nov = a.open("2026-11")
    week = a.b.cycle(a.weeks[1])
    alignments = a.b.repos.alignments.list_for_cycle(week.cycle_id)
    pairs = [(week.cycle_id, al.activity_instance_ref) for al in alignments]
    s = derive_evidence_summary(nov, alignments=alignments * 2,
                                events=[a.original, a.correction, *a.nov],
                                cycles=[week], scheduled_instances=pairs * 2,
                                now=a.b.clock())
    assert s.total_scheduled_opportunities == len(set(pairs)) == 3
    assert s.per_goal_evidence[0].scheduled_opportunities == 3
    none = derive_evidence_summary(nov, alignments=alignments,
                                   events=[a.original, a.correction, *a.nov],
                                   cycles=[week], now=a.b.clock())
    assert none.total_scheduled_opportunities == 0, \
        "scheduled is never inferred from alignments or observations"


# ===========================================================================
# Timezone authority — one timezone of record per child across every
# clinician-approved plan; fail closed, never pick, never convert.
# ===========================================================================

OTHER_TZ = "America/Los_Angeles"


def _counts_shape(s):
    """Counts without ids (two worlds mint different ids)."""
    return (s.total_scheduled_opportunities, s.total_distinct_observation_events,
            s.distinct_observed_local_dates, s.did_it_count,
            tuple((line.scheduled_opportunities, line.attributed_attempts)
                  for line in s.per_goal_evidence),
            len(s.contributing_focus_plan_ids), len(s.cycle_refs))


def _world_b():
    """Scenario B, built fresh: Plan A Weeks 1-4, Plan B from 12-14."""
    w = Billing()
    bb = w.b
    w.weeks = {1: bb.week_one, 2: bb.release("2026-11-23"),
               3: bb.release("2026-11-30"), 4: bb.release("2026-12-07")}
    w.observe(w.weeks[3], "2026-12-02")
    w.observe(w.weeks[4], "2026-12-09")
    w.plan_b = bb.renewal_plan(month="2026-12", when="2026-12-14").focus_plan_id
    w.weeks[5] = bb.release("2026-12-14")
    w.weeks[6] = bb.release("2026-12-21")
    w.observe(w.weeks[5], "2026-12-15")
    w.observe(w.weeks[6], "2026-12-22")
    return w


def test_two_same_timezone_plans_open_identically():
    """6. Same zone: the period's timezone and every count are identical
    whichever plan opens it (two identical worlds, one opened per plan)."""
    via_a_world, via_b_world = _world_b(), _world_b()
    dec_a = via_a_world.open("2026-12", plan_id=via_a_world.plan_a,
                             when="2026-12-20")
    dec_b = via_b_world.open("2026-12", plan_id=via_b_world.plan_b,
                             when="2026-12-20")
    assert dec_a.timezone_of_record == dec_b.timezone_of_record \
        == "America/New_York"
    via_a, via_b = via_a_world.summary(dec_a), via_b_world.summary(dec_b)
    assert _counts_shape(via_a) == _counts_shape(via_b)
    assert via_a.total_scheduled_opportunities == 11
    assert via_a.total_distinct_observation_events == 4


def test_a_successor_in_another_timezone_is_refused_at_activation(a):
    """7a. The invariant is enforced where a plan joins the child's clinical
    record — activation — and the refusal writes nothing."""
    from pilot_backend.goals.errors import ClinicalTimezoneMismatch
    claims_before = a.b.governance_claims()
    a.b.tz = OTHER_TZ
    draft = a.b.draft_plan(month="2026-12", when="2026-12-14")
    with pytest.raises(ClinicalTimezoneMismatch):
        a.b.activate(draft, "2026-12-14")
    row = a.b.repos.focus_plans.get_by_id(draft.focus_plan_id)
    assert row.state.value == "draft" and row.activated_at is None
    assert a.b.repos.focus_plans.get_by_id(a.plan_a).is_active
    assert a.b.governance_claims() == claims_before
    assert not a.b.repos.goal_snapshots.list_for_plan(draft.focus_plan_id)


def test_a_stale_draft_in_another_zone_does_not_block_a_successor(a):
    """Only clinician-APPROVED plans fix the zone: an abandoned draft in
    another zone neither blocks a same-zone successor nor joins the record."""
    a.b.tz = OTHER_TZ
    a.b.draft_plan(month="2026-12", when="2026-12-03")
    a.b.tz = "America/New_York"
    successor = a.b.renewal_plan(month="2026-12", when="2026-12-14")
    assert a.b.repos.focus_plans.get_by_id(successor.focus_plan_id).is_active


def test_a_draft_in_another_timezone_does_not_poison_rtm(a):
    """A draft is not clinician-approved and governs nothing; RTM ignores it
    unless the period is opened against it — then it refuses."""
    from pilot_backend.rtm.errors import RTMTimezoneInconsistent
    a.b.tz = OTHER_TZ
    draft = a.b.draft_plan(month="2026-12", when="2026-12-03")
    dec = a.open("2026-12", when="2026-12-03")
    assert dec.timezone_of_record == "America/New_York"
    assert a.summary(dec).total_distinct_observation_events == 2
    with pytest.raises(RTMTimezoneInconsistent):
        a.open("2027-01", plan_id=draft.focus_plan_id, when="2027-01-02")


def test_rtm_refuses_conflicting_approved_timezones(a):
    """7b. Legacy/forged state with two approved zones (written past the
    activation guard): opening refuses, and an already-open period's summary
    and report refuse rather than pick, convert or mix."""
    from pilot_backend.rtm.errors import RTMTimezoneInconsistent
    dec = a.open("2026-12", when="2026-12-03")
    a.b.at("2027-01-02")
    a.rtm.generate_evidence_summary(a.b.provider, dec.period_id)
    a.rtm.generate_coding_assistance(a.b.provider, dec.period_id)
    a.b.tz = OTHER_TZ
    a.b.forced_active_plan(month="2026-12", when="2026-12-04")
    with pytest.raises(RTMTimezoneInconsistent):
        a.summary(dec)
    with pytest.raises(RTMTimezoneInconsistent):
        a.rtm.generate_report(a.b.provider, dec.period_id)
    assert a.b.repos.month_end_reports.current_for_period(dec.period_id) is None
    with pytest.raises(RTMTimezoneInconsistent):
        a.open("2027-01", when="2027-01-02")


def test_a_legacy_undated_week_counts_only_when_it_cannot_be_ambiguous(s):
    """A released week captured from a foreign (undated) document: wholly
    inside the month, every activity is necessarily in the month; straddling
    the month boundary, its dates are unknowable — refused, never guessed."""
    from pilot_backend.domain.source_link import SourceSystem
    from pilot_backend.rtm.errors import RTMConflict
    from .test_rtm_evidence_reporting import _planning, _rtm_open
    ctx = _planning(s)                       # Oct cycle 1, foreign snapshot
    _, period = _rtm_open(s, ctx)
    summary = s.rtm.generate_evidence_summary(s.provider_alpha, period.period_id)
    assert summary.total_scheduled_opportunities == 3, "a, b, c in October"
    for sequence in (2, 3, 4, 5):
        s.weekly.create_cycle(s.provider_alpha, ctx["plan"].focus_plan_id,
                              sequence_in_month=sequence)
    crossing = [c for c in s.weekly.list_cycles(
        s.provider_alpha, ctx["plan"].focus_plan_id)
        if c.spans_month_boundary][0]
    s.weekly.allocate_cycle(s.provider_alpha, crossing.cycle_id,
                            ctx["candidates"], family_declared_capacity=2)
    # Allocated but unreleased: still nothing scheduled from it.
    assert s.rtm.generate_evidence_summary(
        s.provider_alpha, period.period_id).total_scheduled_opportunities == 3
    s.weekly.capture_snapshot(s.provider_alpha, crossing.cycle_id,
                              SourceSystem.PARENT, "fictional-parent-plan-005",
                              {"week": 5, "items": []})
    s.weekly.release_cycle(s.provider_alpha, crossing.cycle_id)
    with pytest.raises(RTMConflict, match="no dated schedule"):
        s.rtm.generate_evidence_summary(s.provider_alpha, period.period_id)
