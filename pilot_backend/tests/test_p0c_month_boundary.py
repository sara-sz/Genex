"""P0-C — clinical-plan governance across calendar-month boundaries.

Every release and read here is ROUTE-SHAPED: `release_week_one(principal,
child, family_declared_capacity=3)` and `this_week(principal, child)` with NO
`cycle_month` and NO `local_date`, exactly as the HTTP handlers call them. Only
the clock moves. The P0-B1 suite pins both arguments, which is why the
Nov -> Dec defect was invisible to it; this suite pins neither.

The world mirrors the live positive-path child: one clinician-approved goal,
plan activated by the first release on Tue 2026-11-17 (America/New_York), the
real reviewed bank, capacity 3. Its clinical period is 2026-11-17 .. 12-14:

    Week 1  11-17 .. 11-22   (starter)
    Week 2  11-23 .. 11-29
    Week 3  11-30 .. 12-06   spans the month boundary
    Week 4  12-07 .. 12-13
    Week 5  12-14 .. 12-20   does NOT fit -> clinician renewal required
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest

from pilot_backend.auth import DevAuthVerifier, VerifiedToken
from pilot_backend.config import PilotSettings
from pilot_backend.domain import entities as entities_module
from pilot_backend.domain.adaptation_decision import AdaptationBranch
from pilot_backend.domain.clinical_period import (
    CLINICAL_PERIOD_DAYS,
    ClinicalPeriod,
    clinical_period_for,
    local_date_in,
)
from pilot_backend.domain.goals import EditType, GoalKind, GoalRef
from pilot_backend.domain.identity_claims import ClaimKind
from pilot_backend.domain.observation import AttemptOutcome
from pilot_backend.integration.governing_plan import (
    GovernanceOutcome,
    GoverningPlanAmbiguous,
    resolve_governance,
)
from pilot_backend.integration.parent_source import InMemoryParentSessionSource
from pilot_backend.integration.week_one_release import (
    ClinicalRenewalRequired,
    GoverningPlanConflict,
    WeekOneReleaseError,
    WeekOneReleaseService,
)
from pilot_backend.planning.service import MonthlyPlanService
from pilot_backend.transport import build_application
from pilot_backend.weekly.errors import (
    OutsideClinicalPeriod,
    OverlappingCycle,
    WeeklyConflict,
    WeeklyValidationError,
)
from pilot_backend.weekly.service import WeeklyService
from pilot_runtime.integration.static_activity_bank import StaticActivityBank

from .test_integration_identity import call
from .test_p0b1_adaptive_week_two import CHILD, Clock, _source
from .test_evidence_driven_generation_v2 import World, _projection, mixed

TZ = "America/New_York"
CAPACITY = 3


class Boundary:
    """Approved goal; the first release on `first_day` creates the plan."""

    def __init__(self, first_day="2026-11-17", tz=TZ):
        self.tz = tz
        self.clock = Clock(first_day[:10])
        self.world = World(source=_source(),
                           projection=_projection(states_30=mixed(),
                                                  state_24="emerging"))
        self.repos = self.world.repos
        self.goals = self.world.goals
        self.provider = self.world.principal()
        self.caregiver = self.world.principal("cg-subject")
        self.plans = MonthlyPlanService(repos=self.repos,
                                        recorder=self.world.recorder,
                                        now=self.clock)
        self.weekly = WeeklyService(repos=self.repos,
                                   recorder=self.world.recorder,
                                   now=self.clock)
        self.bank = StaticActivityBank()
        self.service = WeekOneReleaseService(
            repos=self.repos, goals=self.goals, plans=self.plans,
            weekly=self.weekly, activity_bank=self.bank, now=self.clock)
        generated = self.world.generate()
        self.goal = self.goals.approve_clinical_goal(
            self.provider, CHILD, edit_type=EditType.ACCEPTED_VERBATIM,
            suggestion_id=generated.generated[0].suggestion_ids[0])
        self.week_one = self.release(first_day)
        self._fb = 0

    # -- route-shaped calls ------------------------------------------------
    def at(self, when):
        """A day (15:00Z) or an exact ISO instant."""
        if "T" in when:
            self.clock.value = datetime.fromisoformat(when).replace(
                tzinfo=timezone.utc)
        else:
            self.clock.set(when)

    def release(self, when, **kwargs):
        self.at(when)
        return self.service.release_week_one(
            self.provider, CHILD, family_declared_capacity=CAPACITY,
            timezone_of_record=self.tz, **kwargs)

    def this_week(self, when):
        self.at(when)
        return self.service.this_week(self.caregiver, CHILD)

    def current_cycle(self, when):
        self.at(when)
        g = self.service.governance(self.provider, CHILD)
        return g.cycle if g.cycle is not None and g.cycle.covers(
            g.local_date) else None

    # -- helpers -------------------------------------------------------------
    def cycle(self, outcome):
        return self.repos.weekly_cycles.get_by_id(outcome.cycle_id)

    def plan_of(self, outcome):
        return self.repos.focus_plans.get_by_id(outcome.focus_plan_id)

    def cycles(self):
        return sorted(self.repos.weekly_cycles.list_for_child(CHILD),
                      key=lambda c: c.starts_on)

    def plan_rows(self):
        return self.repos.focus_plans.list_for_child(CHILD)

    def early_plan(self, month="2026-12", when="2026-11-24"):
        """A later plan created + activated EXPLICITLY (the monthly-plan
        routes), i.e. by a clinician, ahead of time."""
        self.at(when)
        plan = self.plans.create_plan(self.provider, CHILD, month, self.tz)
        self.plans.allocate_goal(
            self.provider, plan.focus_plan_id,
            GoalRef(GoalKind.CLINICAL, self.goal.clinical_goal_id),
            priority_rank=1)
        return self.plans.activate_plan(self.provider, plan.focus_plan_id)

    def observe(self, outcome, when, title=None, *,
                attempt=AttemptOutcome.DID_IT):
        self.at(when)
        refs = {a["title"]: a["activity_ref"]
                for day in outcome.parent_week["days"]
                for a in day["activities"]}
        ref = refs[title] if title else sorted(refs.values())[0]
        instance = self.weekly.resolve_parent_activity_ref(
            self.caregiver, outcome.cycle_id, ref)
        self._fb += 1
        return self.weekly.record_observation(
            self.caregiver, outcome.cycle_id, instance,
            local_date=when[:10], attempt_outcome=attempt,
            source_feedback_id=f"fb_p0c_{self._fb:03d}")


def week_of(view):
    return (view["week"]["starts_on"], view["week"]["ends_on"]) if view else None


@pytest.fixture
def b():
    return Boundary()


@pytest.fixture
def b3(b):
    """Weeks 1-2 released on the route path; Week 3 released Mon Nov 30."""
    b.week_two = b.release("2026-11-23")
    b.week_three = b.release("2026-11-30")
    return b


# ===========================================================================
# 0. the validity anchor
# ===========================================================================


def test_the_period_is_activation_plus_28_local_days(b):
    plan = b.plan_of(b.week_one)
    assert CLINICAL_PERIOD_DAYS == 28
    assert clinical_period_for(plan) == ClinicalPeriod("2026-11-17",
                                                       "2026-12-14")
    # NOT the calendar month the plan is labelled with.
    assert (plan.starts_on, plan.ends_on) == ("2026-11-01", "2026-11-30")


def test_the_period_uses_the_plan_zone_not_utc():
    """Activated 2026-12-01T03:00Z = Nov 30, 22:00 in New York."""
    b = Boundary(first_day="2026-12-01T03:00:00")
    plan = b.plan_of(b.week_one)
    assert clinical_period_for(plan).starts_on == "2026-11-30"
    assert plan.cycle_month == "2026-11", "the label is the LOCAL month"
    assert (b.cycle(b.week_one).starts_on,
            b.cycle(b.week_one).ends_on) == ("2026-11-30", "2026-12-06"), \
        "Mon Nov 30 local: a full starter week, itself crossing the month"


def test_a_draft_or_closed_plan_has_no_clinical_period(b):
    plan = b.plan_of(b.week_one)
    from dataclasses import replace
    from pilot_backend.domain.monthly_plan import MonthlyPlanState
    assert clinical_period_for(replace(plan, state=MonthlyPlanState.DRAFT)) is None
    assert clinical_period_for(plan.close()) is None


# ===========================================================================
# 1-7. Nov 30 .. Dec 7 on the route path
# ===========================================================================


def test_1_nov_30_creates_adaptive_week_three_under_the_original_plan(b3):
    cycle = b3.cycle(b3.week_three)
    assert b3.week_three.created is True
    assert cycle.owning_focus_plan_id == b3.week_one.focus_plan_id
    assert (cycle.sequence_in_month, cycle.starts_on, cycle.ends_on) == (
        3, "2026-11-30", "2026-12-06")
    assert cycle.predecessor_cycle_id == b3.week_two.cycle_id
    assert cycle.spans_month_boundary is True
    assert b3.repos.adaptation_decisions.find_for_cycle(cycle.cycle_id)


@pytest.mark.parametrize("day", ["2026-12-01", "2026-12-03", "2026-12-06"])
def test_2_dec_caregiver_this_week_is_week_three(b3, day):
    view = b3.this_week(day)
    assert view["week"]["cycle_id"] == b3.week_three.cycle_id
    assert week_of(view) == ("2026-11-30", "2026-12-06")


@pytest.mark.parametrize("day", ["2026-12-01", "2026-12-06"])
def test_3_dec_provider_current_cycle_is_week_three(b3, day):
    assert b3.current_cycle(day).cycle_id == b3.week_three.cycle_id


@pytest.mark.parametrize("day", ["2026-12-01", "2026-12-06"])
def test_4_and_6_dec_provider_replay_is_week_three_and_creates_nothing(
        b3, day):
    before = (len(b3.cycles()), len(b3.plan_rows()))
    replay = b3.release(day)
    assert replay.created is False
    assert replay.cycle_id == b3.week_three.cycle_id
    assert replay.parent_week == b3.week_three.parent_week
    assert (len(b3.cycles()), len(b3.plan_rows())) == before
    assert [p.cycle_month for p in b3.plan_rows()] == ["2026-11"], \
        "no December plan was auto-created"


def test_5_a_first_week_three_release_on_dec_1_creates_the_same_week(b):
    b.release("2026-11-23")
    late = b.release("2026-12-01")
    cycle = b.cycle(late)
    assert late.created is True
    assert cycle.owning_focus_plan_id == b.week_one.focus_plan_id
    assert (cycle.sequence_in_month, cycle.starts_on, cycle.ends_on) == (
        3, "2026-11-30", "2026-12-06")
    assert [p.cycle_month for p in b.plan_rows()] == ["2026-11"]
    assert b.this_week("2026-12-02")["week"]["cycle_id"] == late.cycle_id


def test_7_dec_7_week_four_stays_under_the_same_plan(b3):
    four = b3.release("2026-12-07")
    cycle = b3.cycle(four)
    assert cycle.owning_focus_plan_id == b3.week_one.focus_plan_id
    assert (cycle.sequence_in_month, cycle.starts_on, cycle.ends_on) == (
        4, "2026-12-07", "2026-12-13")
    assert cycle.predecessor_cycle_id == b3.week_three.cycle_id
    assert b3.this_week("2026-12-13")["week"]["cycle_id"] == four.cycle_id
    assert [p.cycle_month for p in b3.plan_rows()] == ["2026-11"]


# ===========================================================================
# 8. a plan activated mid-December runs into January
# ===========================================================================


def test_8_a_mid_december_plan_spans_into_january():
    b = Boundary(first_day="2026-12-10")          # Thursday
    plan = b.plan_of(b.week_one)
    assert clinical_period_for(plan) == ClinicalPeriod("2026-12-10",
                                                       "2027-01-06")
    weeks = [b.week_one, b.release("2026-12-14"), b.release("2026-12-21"),
             b.release("2026-12-28")]
    bounds = [(b.cycle(w).starts_on, b.cycle(w).ends_on) for w in weeks]
    assert bounds == [("2026-12-10", "2026-12-13"), ("2026-12-14", "2026-12-20"),
                      ("2026-12-21", "2026-12-27"), ("2026-12-28", "2027-01-03")]
    assert {b.cycle(w).owning_focus_plan_id for w in weeks} == {
        plan.focus_plan_id}
    assert b.cycle(weeks[3]).spans_month_boundary is True
    for day in ("2027-01-01", "2027-01-03"):
        assert b.this_week(day)["week"]["cycle_id"] == weeks[3].cycle_id
    with pytest.raises(ClinicalRenewalRequired):
        b.release("2027-01-04")                   # Week 5 ends 01-10 > 01-06
    assert [p.cycle_month for p in b.plan_rows()] == ["2026-12"]


# ===========================================================================
# 9-10. expiry requires explicit clinician renewal; nothing auto-created
# ===========================================================================


def test_9_and_10_expiry_refuses_and_auto_creates_nothing(b3):
    b3.release("2026-12-07")
    before = (len(b3.cycles()), len(b3.plan_rows()),
              len(b3.repos.store.list_all("pilot_monthly_goal_allocations")))
    for day in ("2026-12-14", "2026-12-15", "2026-12-21", "2027-01-05"):
        with pytest.raises(ClinicalRenewalRequired):
            b3.release(day)
        assert b3.this_week(day) is None
        assert b3.current_cycle(day) is None
    after = (len(b3.cycles()), len(b3.plan_rows()),
             len(b3.repos.store.list_all("pilot_monthly_goal_allocations")))
    assert after == before, "nothing created, renewed or copied forward"


def test_9_explicit_renewal_then_governs_a_fresh_week(b3):
    b3.release("2026-12-07")
    with pytest.raises(ClinicalRenewalRequired):
        b3.release("2026-12-14")
    renewed = b3.early_plan(month="2026-12", when="2026-12-14")
    first = b3.release("2026-12-14")
    cycle = b3.cycle(first)
    assert cycle.owning_focus_plan_id == renewed.focus_plan_id
    assert (cycle.sequence_in_month, cycle.starts_on, cycle.ends_on) == (
        1, "2026-12-14", "2026-12-20")


# ===========================================================================
# 11-12. no plan steals governance; an explicit month cannot override it
# ===========================================================================


def test_11_an_early_future_plan_cannot_steal_a_valid_plan(b):
    b.release("2026-11-23")
    december = b.early_plan()                      # activated Nov 24
    three = b.release("2026-11-30")
    assert b.cycle(three).owning_focus_plan_id == b.week_one.focus_plan_id
    assert b.this_week("2026-12-01")["week"]["cycle_id"] == three.cycle_id
    assert b.release("2026-12-02").cycle_id == three.cycle_id
    four = b.release("2026-12-07")
    assert b.cycle(four).owning_focus_plan_id == b.week_one.focus_plan_id
    assert not b.repos.weekly_cycles.list_for_plan(december.focus_plan_id)


def test_11_the_explicit_later_plan_governs_only_what_the_first_cannot(b):
    b.release("2026-11-23")
    december = b.early_plan()                      # period 11-24 .. 12-21
    for day in ("2026-11-30", "2026-12-07"):
        b.release(day)
    five = b.release("2026-12-14")                 # Nov plan cannot hold it
    assert b.cycle(five).owning_focus_plan_id == december.focus_plan_id
    assert (b.cycle(five).starts_on, b.cycle(five).ends_on) == (
        "2026-12-14", "2026-12-20")
    assert len({(c.starts_on) for c in b.cycles()}) == len(b.cycles())


def test_12_an_explicit_month_cannot_force_another_plan_or_week(b3):
    before = (len(b3.cycles()), len(b3.plan_rows()))
    with pytest.raises(GoverningPlanConflict):
        b3.release("2026-12-01", cycle_month="2026-12")
    assert (len(b3.cycles()), len(b3.plan_rows())) == before
    # Agreeing with governance is fine.
    assert b3.release("2026-12-01", cycle_month="2026-11").cycle_id == \
        b3.week_three.cycle_id


def test_12_an_explicit_month_cannot_bypass_renewal(b3):
    b3.release("2026-12-07")
    with pytest.raises(ClinicalRenewalRequired):
        b3.release("2026-12-14", cycle_month="2026-12")
    assert [p.cycle_month for p in b3.plan_rows()] == ["2026-11"]


# ===========================================================================
# 13-14. no two cycles of one child share a local date, across plans
# ===========================================================================


def test_13_a_cross_plan_starter_inside_week_three_is_refused(b3):
    december = b3.early_plan(when="2026-11-30")
    b3.at("2026-12-01")
    with pytest.raises(OverlappingCycle):
        b3.weekly.create_cycle(b3.provider, december.focus_plan_id,
                               sequence_in_month=1,
                               first_plan_date="2026-12-01")
    with pytest.raises(OverlappingCycle):      # the legacy month-anchored path
        b3.weekly.create_cycle(b3.provider, december.focus_plan_id,
                               sequence_in_month=1)
    assert not b3.repos.weekly_cycles.list_for_plan(december.focus_plan_id)


def test_13_every_new_cycle_claims_each_of_its_dates(b3):
    claims = [d for _i, d in b3.repos.store.list_all("pilot_identity_claims")
              if d.get("kind") == ClaimKind.CHILD_CARE_DAY.value]
    holders = {}
    for claim in claims:
        holders.setdefault(claim["holder_ref"], 0)
        holders[claim["holder_ref"]] += 1
    assert holders == {b3.week_one.cycle_id: 6, b3.week_two.cycle_id: 7,
                       b3.week_three.cycle_id: 7}


def _two_plan_race(b, *, break_advisory):
    """Two writers, two plans, overlapping weeks, FORCED to interleave: both
    finish the advisory read before either commits. Only the per-day claims
    can then stop the second."""
    december = b.early_plan(when="2026-11-30")
    january = b.early_plan(month="2027-01", when="2026-11-30")
    b.at("2026-12-01")
    barrier = threading.Barrier(2, timeout=10)
    real = b.repos.weekly_cycles.list_for_child
    first_read = threading.local()

    def interleaved(child_id):
        rows = real(child_id)
        if getattr(first_read, "done", False):
            return rows        # the loser's post-failure diagnostic read
        first_read.done = True
        barrier.wait()         # both advisory reads finish before any commit
        return [] if break_advisory else rows

    b.repos.weekly_cycles.list_for_child = interleaved
    results = {}

    def writer(name, plan_id, first):
        try:
            results[name] = b.weekly.create_cycle(
                b.provider, plan_id, sequence_in_month=1,
                first_plan_date=first)
        except WeeklyConflict as exc:
            results[name] = exc

    threads = [threading.Thread(target=writer, args=(
                   "dec", december.focus_plan_id, "2026-12-01")),
               threading.Thread(target=writer, args=(
                   "jan", january.focus_plan_id, "2026-12-03"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    b.repos.weekly_cycles.list_for_child = real
    return results, december, january


def test_14_concurrent_cross_plan_overlap_exactly_one_succeeds():
    b = Boundary()                                  # only Week 1 exists
    results, december, january = _two_plan_race(b, break_advisory=True)
    won = [r for r in results.values() if not isinstance(r, Exception)]
    lost = [r for r in results.values() if isinstance(r, Exception)]
    assert len(won) == 1 and len(lost) == 1, results
    assert isinstance(lost[0], OverlappingCycle)
    stored = (b.repos.weekly_cycles.list_for_plan(december.focus_plan_id)
              + b.repos.weekly_cycles.list_for_plan(january.focus_plan_id))
    assert [c.cycle_id for c in stored] == [won[0].cycle_id]


# ===========================================================================
# 15. same-plan P0-B1 resume stays green across the boundary
# ===========================================================================


def test_15_an_interrupted_week_three_on_dec_1_resumes_exactly(b, monkeypatch):
    b.release("2026-11-23")
    real = b.weekly.record_week_adaptation
    calls = {"n": 0}

    def fail_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected crash after allocation")
        return real(*args, **kwargs)

    monkeypatch.setattr(b.weekly, "record_week_adaptation", fail_once)
    with pytest.raises(RuntimeError):
        b.release("2026-12-01")
    draft = [c for c in b.cycles() if c.sequence_in_month == 3]
    assert len(draft) == 1 and not draft[0].is_released
    assert b.this_week("2026-12-01") is None, "a draft is never shown"
    resumed = b.release("2026-12-02")
    assert resumed.cycle_id == draft[0].cycle_id and resumed.created is True
    assert b.release("2026-12-03").created is False
    assert len([c for c in b.cycles() if c.sequence_in_month == 3]) == 1


# ===========================================================================
# 16. timezone: UTC has flipped, the family's day has not
# ===========================================================================


def test_16_utc_crossed_but_the_family_is_still_in_week_two(b):
    two = b.release("2026-11-23")
    late_sunday = "2026-11-30T03:00:00"            # Sun Nov 29 22:00 in NY
    assert b.this_week(late_sunday)["week"]["cycle_id"] == two.cycle_id
    replay = b.release(late_sunday)
    assert replay.created is False and replay.cycle_id == two.cycle_id
    assert not [c for c in b.cycles() if c.sequence_in_month == 3]


def test_16_utc_crossed_into_december_but_not_the_family(b3):
    """Mon Nov 30, 20:30 in NY = Dec 1 01:30Z: same Week 3 either way, and no
    December plan or starter week appears."""
    view = b3.this_week("2026-12-01T01:30:00")
    assert view["week"]["cycle_id"] == b3.week_three.cycle_id
    assert b3.release("2026-12-01T01:30:00").created is False
    assert [p.cycle_month for p in b3.plan_rows()] == ["2026-11"]


# ===========================================================================
# 17-19. clinical, content and billing semantics are unchanged
# ===========================================================================


def test_17_goal_version_and_anchor_unchanged_across_weeks_2_3_4(b3):
    goal_before = b3.repos.clinical_goals.get_by_id(b3.goal.clinical_goal_id)
    four = b3.release("2026-12-07")
    for outcome in (b3.week_three, four):
        decision = b3.repos.adaptation_decisions.find_for_cycle(
            outcome.cycle_id)
        (proof,) = decision.goal_proofs
        assert proof.goal_id == b3.goal.clinical_goal_id
        assert proof.goal_version_before == proof.goal_version_after == \
            goal_before.current_version_id
        assert proof.anchor_rung_ref_before == proof.anchor_rung_ref_after
    assert b3.repos.clinical_goals.get_by_id(
        b3.goal.clinical_goal_id) == goal_before


def test_18_p0b1_change_theme_still_fires_across_the_boundary(b):
    two = b.release("2026-11-23")
    title = sorted(a["title"] for d in two.parent_week["days"]
                   for a in d["activities"])[0]
    b.observe(two, "2026-11-24", title, attempt=AttemptOutcome.DIDNT_WANT_TO_TRY)
    b.observe(two, "2026-11-26", title, attempt=AttemptOutcome.DIDNT_WANT_TO_TRY)
    three = b.release("2026-12-01")                # first released in December
    decision = b.repos.adaptation_decisions.find_for_cycle(three.cycle_id)
    assert any(d.branch is AdaptationBranch.CHANGE_THEME
               for d in decision.activity_decisions)
    week_two_ids = {a["activity_template_id"] for a in json.loads(
        b.repos.weekly_plan_snapshots.list_for_cycle(
            two.cycle_id)[0].resolved_plan_document)["activities"]}
    assert not set(decision.selected_template_ids) & week_two_ids


def test_19_billing_attribution_stays_on_the_observation_date(b3):
    event = b3.observe(b3.week_three, "2026-12-02")
    assert event.owning_cycle_id == b3.week_three.cycle_id
    assert event.attribution_month == "2026-12"     # the LOCAL date's month
    assert b3.plan_of(b3.week_three).cycle_month == "2026-11"
    earlier = b3.observe(b3.week_three, "2026-11-30")
    assert earlier.attribution_month == "2026-11"


# ===========================================================================
# 20. Week 1 / Week 2 on the route path are what they always were
# ===========================================================================


def test_20_week_one_and_two_are_unchanged_on_the_route_path(b):
    one = b.cycle(b.week_one)
    assert (one.sequence_in_month, one.starts_on, one.ends_on,
            one.is_partial) == (1, "2026-11-17", "2026-11-22", True)
    assert b.this_week("2026-11-20")["week"]["cycle_id"] == b.week_one.cycle_id
    assert b.release("2026-11-22").created is False
    two = b.release("2026-11-23")
    cycle = b.cycle(two)
    assert (cycle.sequence_in_month, cycle.starts_on, cycle.ends_on) == (
        2, "2026-11-23", "2026-11-29")
    assert cycle.owning_focus_plan_id == one.owning_focus_plan_id
    assert b.release("2026-11-29").cycle_id == two.cycle_id


# ===========================================================================
# the resolver itself, and the refusals the route renders as 403
# ===========================================================================


def test_two_covering_cycles_are_refused_never_picked(b3):
    plans = b3.plan_rows()
    cycles = {p.focus_plan_id: tuple(b3.repos.weekly_cycles.list_for_plan(
        p.focus_plan_id)) for p in plans}
    three = b3.cycle(b3.week_three)
    from dataclasses import replace
    ghost = replace(three, cycle_id="wcyc_ghost", sequence_in_month=9)
    cycles[three.owning_focus_plan_id] += (ghost,)
    with pytest.raises(GoverningPlanAmbiguous):
        resolve_governance(plans=plans, cycles_by_plan=cycles,
                           now=b3.clock(), local_date="2026-12-01")


def test_a_child_with_no_plan_bootstraps_and_any_plan_means_renewal(b3):
    assert resolve_governance(plans=[], cycles_by_plan={},
                              now=b3.clock()).outcome is \
        GovernanceOutcome.BOOTSTRAP
    g = resolve_governance(plans=b3.plan_rows(), cycles_by_plan={},
                           now=b3.clock(), local_date="2027-02-01")
    assert g.outcome is GovernanceOutcome.RENEWAL_REQUIRED


def test_the_write_layer_refuses_a_week_outside_the_period(b3):
    b3.release("2026-12-07")
    b3.at("2026-12-14")
    with pytest.raises(OutsideClinicalPeriod):
        b3.weekly.create_cycle(b3.provider, b3.week_one.focus_plan_id,
                               sequence_in_month=5,
                               first_plan_date="2026-11-17")
    assert issubclass(OutsideClinicalPeriod, WeeklyValidationError)


def test_every_new_refusal_is_route_handled():
    for error in (ClinicalRenewalRequired, GoverningPlanConflict):
        assert issubclass(error, WeekOneReleaseError)
    assert issubclass(OverlappingCycle, WeeklyConflict)


# ===========================================================================
# the real HTTP routes on Dec 1 (caregiver this-week, provider current-cycle,
# provider release replay) — no query string, no body month
# ===========================================================================


def _serve(world, monkeypatch):
    """The REAL WSGI app over a world's repositories and clock."""
    # The route builds its release service with the default clock, read from
    # `entities.utc_now` at construction. Point it at the world's clock.
    monkeypatch.setattr(entities_module, "utc_now", world.clock)
    verifier = DevAuthVerifier("dev")
    for token, subject in (("cg", "cg-subject"), ("pr", "pr-subject")):
        verifier.add(token, VerifiedToken(subject=subject,
                                          email="fictional@example.invalid"))
    app = build_application(
        settings=PilotSettings.from_env({
            "PILOT_ENVIRONMENT": "dev",
            "PILOT_ALLOWED_ORIGINS": "http://localhost:3000",
            "PILOT_DEV_AUTH": "1"}),
        repos=world.repos, verifier=verifier, recorder=world.world.recorder,
        parent_source=InMemoryParentSessionSource(), log_sink=[],
        activity_bank=world.bank)
    world.app = app
    return world


@pytest.fixture
def http(b3, monkeypatch):
    return _serve(b3, monkeypatch)


def _get(b, path, token, query=""):
    status, body, _ = call(b.app, path, bearer=f"Bearer {token}", query=query)
    return status, json.loads(body) if isinstance(body, (bytes, str)) else body


def test_http_dec_1_this_week_current_cycle_and_release_agree(http):
    http.at("2026-12-01")
    status, week = _get(http, f"/pilot/children/{CHILD}/this-week", "cg")
    assert status == 200 and week["released"] is True
    assert week["week"]["week"]["cycle_id"] == http.week_three.cycle_id

    status, current = _get(http, f"/pilot/children/{CHILD}/current-cycle",
                           "pr")
    assert status == 200
    assert current["cycle"]["cycle_id"] == http.week_three.cycle_id
    assert current["plan"]["focus_plan_id"] == http.week_one.focus_plan_id

    status, body, _ = call(
        http.app, f"/pilot/children/{CHILD}/weekly-cycles/current/release",
        method="POST", bearer="Bearer pr",
        body=json.dumps({"family_declared_capacity": 3}).encode())
    body = json.loads(body) if isinstance(body, (bytes, str)) else body
    assert status == 200, body
    assert body["created"] is False
    assert body["cycle_id"] == http.week_three.cycle_id
    assert [p.cycle_month for p in http.plan_rows()] == ["2026-11"]


def test_http_after_expiry_release_is_the_constant_403(http):
    http.release("2026-12-07")
    http.at("2026-12-14")
    status, body, _ = call(
        http.app, f"/pilot/children/{CHILD}/weekly-cycles/current/release",
        method="POST", bearer="Bearer pr",
        body=json.dumps({"family_declared_capacity": 3}).encode())
    assert status == 403
    assert [p.cycle_month for p in http.plan_rows()] == ["2026-11"]


def test_http_an_explicit_month_query_is_still_a_month_inspection(http):
    http.at("2026-12-01")
    status, body = _get(http, f"/pilot/children/{CHILD}/current-cycle", "pr",
                        query="cycle_month=2026-12")
    assert status == 200 and body["plan"] is None


def test_13_a_pre_p0c_cycle_without_day_claims_still_blocks_overlap(b):
    """Cycles written before P0-C carry no per-day claims. The advisory
    child-wide read is what protects them."""
    from pilot_backend.domain.weekly_cycle import WeeklyCycle
    legacy = WeeklyCycle.create(b.week_one.focus_plan_id, CHILD,
                                sequence_in_month=7, starts_on="2026-11-23",
                                ends_on="2026-11-29")
    b.repos.weekly_cycles.create(legacy)          # as a pre-P0-C row: no claims
    december = b.early_plan(when="2026-11-23")
    b.at("2026-11-23")
    with pytest.raises(OverlappingCycle):
        b.weekly.create_cycle(b.provider, december.focus_plan_id,
                              sequence_in_month=1,
                              first_plan_date="2026-11-25")


def test_the_live_staging_shape_gets_the_expected_period():
    """UTC plan activated 2026-11-17T22:58:45Z, like the positive-path child."""
    b = Boundary(first_day="2026-11-17T22:58:45", tz="UTC")
    plan = b.plan_of(b.week_one)
    assert clinical_period_for(plan) == ClinicalPeriod("2026-11-17",
                                                       "2026-12-14")
    assert (b.cycle(b.week_one).starts_on,
            b.cycle(b.week_one).ends_on) == ("2026-11-17", "2026-11-22")
    for day in ("2026-11-23", "2026-11-30", "2026-12-07"):
        b.release(day)
    assert b.this_week("2026-12-01T12:00:00")["week"]["starts_on"] == \
        "2026-11-30"
    with pytest.raises(ClinicalRenewalRequired):
        b.release("2026-12-14")


def test_the_period_starts_at_activation_not_at_draft_creation():
    """A plan drafted on Nov 20 and activated by the clinician on Nov 24 is
    valid from Nov 24: approval, not drafting, starts the clinical clock."""
    b = Boundary()
    b.at("2026-11-20")
    draft = b.plans.create_plan(b.provider, CHILD, "2026-12", TZ)
    b.plans.allocate_goal(b.provider, draft.focus_plan_id,
                          GoalRef(GoalKind.CLINICAL, b.goal.clinical_goal_id),
                          priority_rank=1)
    b.at("2026-11-24")
    active = b.plans.activate_plan(b.provider, draft.focus_plan_id)
    assert clinical_period_for(active) == ClinicalPeriod("2026-11-24",
                                                         "2026-12-21")


def test_http_current_cycle_follows_governance_not_the_server_month(
        monkeypatch):
    """A world whose plan month can never equal the real server month: the
    no-query read must still find the governing week."""
    world = _serve(Boundary(first_day="2031-02-04"), monkeypatch)
    week_two = world.release("2031-02-10")
    world.at("2031-02-12")
    status, body = _get(world, f"/pilot/children/{CHILD}/current-cycle", "pr")
    assert status == 200
    assert body["cycle"]["cycle_id"] == week_two.cycle_id
    status, week = _get(world, f"/pilot/children/{CHILD}/this-week", "cg")
    assert week["week"]["week"]["cycle_id"] == week_two.cycle_id
