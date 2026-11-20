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
