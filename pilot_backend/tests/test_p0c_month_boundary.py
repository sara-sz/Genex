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

from .first_plan import activate_first_plan, release_with_first_plan  # noqa: F401

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
from pilot_backend.goals.errors import GoalConflict, SuccessorActivationPremature
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
        # First-plan gate: the clinician's explicit plan, then the release.
        return release_with_first_plan(
            self.service, self.provider, CHILD,
            family_declared_capacity=CAPACITY,
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

    def draft_plan(self, month="2026-12", when="2026-11-24"):
        """A successor PREPARED as a draft (create + allocate), as the
        monthly-plan routes do. A draft has no clinical period."""
        self.at(when)
        plan = self.plans.create_plan(self.provider, CHILD, month, self.tz)
        self.plans.allocate_goal(
            self.provider, plan.focus_plan_id,
            GoalRef(GoalKind.CLINICAL, self.goal.clinical_goal_id),
            priority_rank=1)
        return plan

    def activate(self, plan, when):
        self.at(when)
        return self.plans.activate_plan(self.provider, plan.focus_plan_id)

    def renewal_plan(self, month="2026-12", when="2026-12-14"):
        """Draft + explicit clinician activation on the same day."""
        return self.activate(self.draft_plan(month, when), when)

    def forced_active_plan(self, month="2026-12", when="2026-11-30"):
        """An ACTIVE plan written straight to the repository, bypassing the
        activation guard — the state a pre-renewal-guard deployment or a forged
        row could leave. Used ONLY to prove the weekly-layer overlap defences
        hold even when two plans are active."""
        from pilot_backend.domain.monthly_plan import MonthlyFocusPlan
        self.at(when)
        plan = MonthlyFocusPlan.create(CHILD, month, self.tz,
                                       policy_version="planning-policy-2026.10")
        active = plan.activate(claim_id="forced-by-test", now=self.clock())
        self.repos.focus_plans.create(active)
        return active

    def governance_claims(self):
        from pilot_backend.domain.identity_claims import key_digest
        return [d for _i, d in self.repos.store.list_all("pilot_identity_claims")
                if d.get("kind") == ClaimKind.CLINICAL_GOVERNANCE.value
                and d.get("key_digest") == key_digest(CHILD)]

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
    renewed = b3.renewal_plan(month="2026-12", when="2026-12-14")
    first = b3.release("2026-12-14")
    cycle = b3.cycle(first)
    assert cycle.owning_focus_plan_id == renewed.focus_plan_id
    assert (cycle.sequence_in_month, cycle.starts_on, cycle.ends_on) == (
        1, "2026-12-14", "2026-12-20")


# ===========================================================================
# 11-12. no plan steals governance; an explicit month cannot override it
# ===========================================================================


def test_11_an_early_successor_is_refused_and_cannot_steal(b):
    """Prepared as a draft Nov 24; activation refused at every point where the
    November plan still holds today's or the upcoming week — with zero writes —
    so the incumbent is never displaced and Weeks 3-4 stay under it."""
    b.release("2026-11-23")
    december = b.draft_plan()
    assert b.this_week("2026-11-24")["week"]["starts_on"] == "2026-11-23", \
        "a draft changes nothing about governance"
    for day in ("2026-11-24", "2026-11-30", "2026-12-07", "2026-12-13"):
        # On Nov 30 and Dec 7 the attempt comes BEFORE that week is released:
        # no cycle covers today, but the incumbent can still HOLD the week.
        before = (b.repos.store.list_all("pilot_monthly_focus_plans"),
                  b.repos.store.list_all("pilot_monthly_goal_snapshots"),
                  b.repos.store.list_all("pilot_identity_claims"))
        with pytest.raises(SuccessorActivationPremature):
            b.activate(december, day)
        after = (b.repos.store.list_all("pilot_monthly_focus_plans"),
                 b.repos.store.list_all("pilot_monthly_goal_snapshots"),
                 b.repos.store.list_all("pilot_identity_claims"))
        assert after == before, f"{day}: a refused activation wrote something"
        if day in ("2026-11-30", "2026-12-07"):
            b.release(day)
    incumbent = b.repos.focus_plans.get_by_id(b.week_one.focus_plan_id)
    assert incumbent.is_active
    assert {c.owning_focus_plan_id for c in b.cycles()} == {
        b.week_one.focus_plan_id}
    assert len(b.cycles()) == 4


def test_11_renewal_at_the_renewal_point_gets_a_fresh_period(b):
    """Dec 14: Week 5 cannot fit November's period, so the prepared successor
    may be activated. Its OWN 28 days begin; the expired incumbent is retired
    (not silently — only because it can no longer govern) and stays readable."""
    b.release("2026-11-23")
    december = b.draft_plan()
    for day in ("2026-11-30", "2026-12-07"):
        b.release(day)
    successor = b.activate(december, "2026-12-14")
    assert clinical_period_for(successor) == ClinicalPeriod("2026-12-14",
                                                            "2027-01-10")
    old = b.repos.focus_plans.get_by_id(b.week_one.focus_plan_id)
    assert old.state.value == "closed" and old.activated_at is not None
    assert len(b.repos.weekly_cycles.list_for_plan(old.focus_plan_id)) == 4
    first = b.release("2026-12-14")
    cycle = b.cycle(first)
    assert cycle.owning_focus_plan_id == successor.focus_plan_id
    assert (cycle.sequence_in_month, cycle.starts_on, cycle.ends_on) == (
        1, "2026-12-14", "2026-12-20")
    assert b.this_week("2026-12-13")["week"]["starts_on"] == "2026-12-07", \
        "the retired plan's last week is still the family's week on Dec 13"
    assert b.this_week("2026-12-15")["week"]["cycle_id"] == first.cycle_id
    held = [c for c in b.governance_claims() if c["record_kind"] == "claim"]
    released = [c for c in b.governance_claims()
                if c["record_kind"] == "release"]
    assert [c["holder_ref"] for c in sorted(held, key=lambda c: c["generation"])] \
        == [b.week_one.focus_plan_id, successor.focus_plan_id]
    assert len(released) == 1 and released[0]["generation"] == 0


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
    december = b3.forced_active_plan(when="2026-11-30")
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
    december = b.forced_active_plan(when="2026-11-30")
    january = b.forced_active_plan(month="2027-01", when="2026-11-30")
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
    december = b.forced_active_plan(when="2026-11-23")
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


def test_the_period_starts_at_activation_not_at_draft_creation(b3):
    """A successor drafted Dec 1 and activated at the renewal point Dec 14 is
    valid from Dec 14: approval, not drafting, starts the clinical clock."""
    b3.release("2026-12-07")
    draft = b3.draft_plan(when="2026-12-01")
    assert clinical_period_for(draft) is None
    active = b3.activate(draft, "2026-12-14")
    assert clinical_period_for(active) == ClinicalPeriod("2026-12-14",
                                                         "2027-01-10")


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


# ===========================================================================
# P0-C renewal — rolling clinician-approved plans, two in one calendar month
# ===========================================================================


@pytest.fixture
def dec():
    """Plan A activated Tue Dec 1: period Dec 1 .. Dec 28; Weeks 1-4 to Dec 27."""
    world = Boundary(first_day="2026-12-01")
    for day in ("2026-12-07", "2026-12-14", "2026-12-21"):
        world.release(day)
    return world


def test_same_month_renewal_needs_no_fake_label(dec):
    plan_a = dec.plan_of(dec.week_one)
    assert clinical_period_for(plan_a) == ClinicalPeriod("2026-12-01",
                                                         "2026-12-28")
    with pytest.raises(ClinicalRenewalRequired):
        dec.release("2026-12-28")                 # Dec 28 .. Jan 3 cannot fit
    plan_b = dec.draft_plan(month="2026-12", when="2026-12-20")   # honest label
    with pytest.raises(SuccessorActivationPremature):
        dec.activate(plan_b, "2026-12-27")        # A's Week 4 still covers today
    plan_b = dec.activate(plan_b, "2026-12-28")
    assert plan_b.cycle_month == plan_a.cycle_month == "2026-12"
    assert plan_b.focus_plan_id != plan_a.focus_plan_id
    assert plan_b.activated_at != plan_a.activated_at, "a distinct approval"
    assert clinical_period_for(plan_b) == ClinicalPeriod("2026-12-28",
                                                         "2027-01-24")
    week = dec.release("2026-12-28")
    cycle = dec.cycle(week)
    assert cycle.owning_focus_plan_id == plan_b.focus_plan_id
    assert (cycle.sequence_in_month, cycle.starts_on, cycle.ends_on) == (
        1, "2026-12-28", "2027-01-03")
    # Two December plans, one unambiguous current week on each side.
    assert dec.this_week("2026-12-27")["week"]["starts_on"] == "2026-12-21"
    assert dec.this_week("2026-12-29")["week"]["cycle_id"] == week.cycle_id
    assert dec.current_cycle("2026-12-29").cycle_id == week.cycle_id
    assert dec.release("2026-12-30").cycle_id == week.cycle_id
    assert sorted(p.state.value for p in dec.plan_rows()) == ["active", "closed"]


def test_a_draft_successor_never_governs_and_nothing_auto_activates(dec):
    dec.draft_plan(when="2026-12-20")
    with pytest.raises(ClinicalRenewalRequired):
        dec.release("2026-12-28")
    assert sorted(p.state.value for p in dec.plan_rows()) == ["active", "draft"]


def test_an_explicitly_closed_incumbent_no_longer_blocks(b3):
    """Service-level close (no HTTP route): the clinician ends the plan, then
    activates a successor mid-period. The closed plan's released week remains
    the family's week until it ends; the successor governs after."""
    successor = b3.draft_plan(when="2026-12-02")
    b3.at("2026-12-03")
    b3.plans.close_plan(b3.provider, b3.week_one.focus_plan_id)
    active = b3.activate(successor, "2026-12-03")
    assert clinical_period_for(active).starts_on == "2026-12-03"
    assert b3.this_week("2026-12-04")["week"]["cycle_id"] == \
        b3.week_three.cycle_id
    nxt = b3.release("2026-12-07")
    assert b3.cycle(nxt).owning_focus_plan_id == active.focus_plan_id
    assert (b3.cycle(nxt).starts_on, b3.cycle(nxt).ends_on) == (
        "2026-12-07", "2026-12-13")


@pytest.mark.parametrize("incumbent", ["expired", "closed"])
def test_two_concurrent_successor_activations_exactly_one_governs(incumbent):
    """Both writers read the holder AND pass the guard before either commits
    (forced interleaving). With a CLOSED incumbent the guard sees no other
    active plan at all, so only the governance claim can stop the second."""
    b = Boundary()
    for day in ("2026-11-23", "2026-11-30", "2026-12-07"):
        b.release(day)
    first = b.draft_plan(when="2026-12-01")
    second = b.draft_plan(month="2026-12", when="2026-12-02")
    if incumbent == "closed":
        b.at("2026-12-13")
        b.plans.close_plan(b.provider, b.week_one.focus_plan_id)
    b.at("2026-12-14")
    barrier = threading.Barrier(2, timeout=10)
    real = b.plans._an_incumbent_still_governs

    def interleaved(others, now):
        verdict = real(others, now)
        barrier.wait()
        return verdict

    b.plans._an_incumbent_still_governs = interleaved
    results = {}

    def activate(plan):
        try:
            results[plan.focus_plan_id] = b.plans.activate_plan(
                b.provider, plan.focus_plan_id)
        except GoalConflict as exc:
            results[plan.focus_plan_id] = exc

    threads = [threading.Thread(target=activate, args=(p,))
               for p in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    won = [r for r in results.values() if not isinstance(r, Exception)]
    lost = [k for k, r in results.items() if isinstance(r, Exception)]
    assert len(won) == 1 and len(lost) == 1, results
    active = [p for p in b.plan_rows() if p.is_active]
    assert [p.focus_plan_id for p in active] == [won[0].focus_plan_id]
    loser = b.repos.focus_plans.get_by_id(lost[0])
    assert loser.state.value == "draft"
    assert b.repos.goal_snapshots.list_for_plan(lost[0]) == []
    holders = [c["holder_ref"] for c in b.governance_claims()
               if c["record_kind"] == "claim"]
    assert holders.count(won[0].focus_plan_id) == 1
    assert lost[0] not in holders


def test_http_month_reads_never_pick_one_of_two_active_plans(dec, monkeypatch):
    """After a same-month renewal the month read returns the ONE active plan.
    If two were ever active for one label (a forged/legacy state), the month
    read refuses with the route's constant 403 rather than pick one."""
    plan_b = dec.renewal_plan(month="2026-12", when="2026-12-28")
    app_world = _serve(dec, monkeypatch)
    status, body = _get(app_world, f"/pilot/children/{CHILD}/monthly-plan",
                        "pr", query="cycle_month=2026-12")
    assert status == 200 and body["plan"]["focus_plan_id"] == plan_b.focus_plan_id
    dec.forced_active_plan(month="2026-12", when="2026-12-28")
    for path in ("monthly-plan", "current-cycle"):
        status, body = _get(app_world, f"/pilot/children/{CHILD}/{path}", "pr",
                            query="cycle_month=2026-12")
        assert status == 403, (path, body)


def test_http_activation_route_refuses_an_early_successor(b3, monkeypatch):
    world = _serve(b3, monkeypatch)
    draft = b3.draft_plan(when="2026-12-01")
    b3.at("2026-12-02")
    # The planning service built by the route stamps with the real clock, so
    # pin the period-relevant instant through the same seam the service uses.
    from pilot_backend.planning import service as planning_module
    monkeypatch.setattr(planning_module.MonthlyPlanService, "_stamp",
                        lambda self: b3.clock())
    status, body, _ = call(world.app,
                           f"/pilot/monthly-plans/{draft.focus_plan_id}/activate",
                           method="POST", bearer="Bearer pr", body=b"{}")
    assert status == 403
    assert b3.repos.focus_plans.get_by_id(draft.focus_plan_id).state.value \
        == "draft"


def test_an_ambiguous_governance_state_blocks_activation(b):
    """Two ACTIVE plans whose cycles both cover today (a forged/legacy state):
    the guard cannot tell which governs, so it fails CLOSED and refuses."""
    from pilot_backend.domain.weekly_cycle import WeeklyCycle
    forged = b.forced_active_plan(month="2026-11", when="2026-11-18")
    b.repos.weekly_cycles.create(WeeklyCycle.create(
        forged.focus_plan_id, CHILD, sequence_in_month=1,
        starts_on="2026-11-18", ends_on="2026-11-22"))
    successor = b.draft_plan(when="2026-11-19")
    with pytest.raises(SuccessorActivationPremature):
        b.activate(successor, "2026-11-20")
    assert b.repos.focus_plans.get_by_id(successor.focus_plan_id).state.value \
        == "draft"
