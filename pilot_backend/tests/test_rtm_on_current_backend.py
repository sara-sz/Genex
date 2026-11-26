"""RTM v2 integrated onto the deployed backend (first-plan gate + Week 1 resume).

The RTM v2 commits were authored on P0-C (c610145). These tests prove, in ONE
world, that bringing them onto d03aafc regresses nothing that was deployed
after P0-C, and that RTM's own guarantees still hold on top of it:

1. a never-planned child still cannot release Week 1 (ClinicalPlanRequired);
2. no automatic UTC bootstrap path exists;
3. an explicitly created + activated plan works normally — and RTM opens on it;
4. an interrupted Week 1 release still resumes — and RTM counts it once;
5. RTM's one-timezone-per-child protection still holds (activation + RTM);
6. RTM does not restore calendar-month governance (the explicit billing
   month never selects a plan; P0-C governance is unchanged by it).
"""

from __future__ import annotations

import inspect

import pytest

from pilot_backend.domain.goals import GoalKind, GoalRef
from pilot_backend.goals.errors import ClinicalTimezoneMismatch
from pilot_backend.integration.week_one_release import ClinicalPlanRequired, WeekOneReleaseService
from pilot_backend.rtm.errors import RTMTimezoneInconsistent
from pilot_backend.rtm.service import RTMService

from .test_first_plan_timezone_gate import CHILD, LA, Fresh
from .test_week_one_resume import CAPACITY, ONE_OF_EVERYTHING, _counts, _interrupt

DAY = "2026-11-17T15:00:00"


def _rtm(f):
    return RTMService(repos=f.repos, recorder=f.world.recorder, now=f.clock)


def _la_plan(f):
    _s, body = f.create_plan("2026-11", LA)
    return f.allocate_and_activate(body["plan"]["focus_plan_id"])


def _episode(f):
    return _rtm(f).open_episode(f.provider, CHILD, [GoalRef(GoalKind.CLINICAL, f.goal.clinical_goal_id)])


def test_1_a_never_planned_child_still_cannot_release_week_one(monkeypatch):
    f = Fresh(monkeypatch, at=DAY)
    with pytest.raises(ClinicalPlanRequired):
        f.service.release_week_one(f.provider, CHILD, family_declared_capacity=CAPACITY)
    assert f.release() == (403, {"error": "not permitted"})
    assert f.plan_rows() == [] and f.repos.weekly_cycles.list_for_child(CHILD) == []


def test_2_no_automatic_utc_bootstrap_path_exists():
    params = inspect.signature(WeekOneReleaseService.release_week_one).parameters
    assert "timezone_of_record" not in params
    assert not hasattr(WeekOneReleaseService, "_resolve_plan")
    assert not hasattr(WeekOneReleaseService, "_snapshot_and_release")


def test_3_an_activated_plan_releases_normally_and_rtm_opens_on_it(monkeypatch):
    f = Fresh(monkeypatch, at=DAY)
    plan = _la_plan(f)
    status, week = f.release()
    assert status == 200 and week["created"] is True
    rtm = _rtm(f)
    period = rtm.open_period(f.provider, _episode(f).episode_id, plan["focus_plan_id"], cycle_month="2026-11")
    assert period.timezone_of_record == LA, "the period takes the clinician-chosen zone"
    f.clock.at("2026-12-01T15:00:00")
    summary = rtm.generate_evidence_summary(f.provider, period.period_id)
    assert summary.total_scheduled_opportunities == CAPACITY   # 11-17, 11-18, 11-19
    assert summary.contributing_focus_plan_ids == (plan["focus_plan_id"],)


@pytest.mark.parametrize("point", ["allocate_cycle:after", "capture_snapshot:after", "release_cycle:before"])
def test_4_an_interrupted_week_one_resumes_and_rtm_counts_it_once(point, monkeypatch):
    f = Fresh(monkeypatch, at=DAY)
    plan = _la_plan(f)
    _interrupt(f, point)
    rtm = _rtm(f)
    period = rtm.open_period(f.provider, _episode(f).episode_id, plan["focus_plan_id"], cycle_month="2026-11")
    # Unreleased: nothing was given to the family, so nothing is scheduled.
    assert rtm.generate_evidence_summary(f.provider, period.period_id).total_scheduled_opportunities == 0

    status, body = f.release()                         # the resume
    assert status == 200 and body["created"] is True
    assert _counts(f) == ONE_OF_EVERYTHING
    summary = rtm.generate_evidence_summary(f.provider, period.period_id)
    assert summary.total_scheduled_opportunities == CAPACITY, "counted once, after release"


def test_5_one_timezone_per_child_holds_on_a_gate_created_plan(monkeypatch):
    f = Fresh(monkeypatch, at=DAY)
    plan = _la_plan(f)
    assert f.release()[0] == 200
    # A successor in another zone is refused at activation (RTM guard)...
    f.clock.at("2026-12-14T15:00:00")
    _s, body = f.create_plan("2026-12", "America/New_York")
    status, _a = f.http("POST", f"/pilot/monthly-plans/{body['plan']['focus_plan_id']}/allocations",
                        body={"goal_kind": "clinical", "goal_id": f.goal.clinical_goal_id, "priority_rank": 1})
    assert status == 200          # allocated, so activation reaches the timezone guard
    with pytest.raises(ClinicalTimezoneMismatch):
        f.plans.activate_plan(f.provider, body["plan"]["focus_plan_id"])
    assert f.repos.focus_plans.get_by_id(body["plan"]["focus_plan_id"]).state.value == "draft"
    # ...and RTM refuses a period opened against that other-zone draft.
    with pytest.raises(RTMTimezoneInconsistent):
        _rtm(f).open_period(f.provider, _episode(f).episode_id, body["plan"]["focus_plan_id"],
                            cycle_month="2026-12")
    assert plan["timezone_of_record"] == LA


def test_6_the_billing_month_never_selects_a_plan_or_changes_governance(monkeypatch):
    f = Fresh(monkeypatch, at=DAY)
    plan = _la_plan(f)
    assert f.release()[0] == 200
    before = f.service.governance(f.provider, CHILD, local_date="2026-11-20")
    rtm = _rtm(f)
    episode = _episode(f)
    # A December billing period opens against the November-labelled plan:
    # the month is explicit metadata, not a plan selector...
    dec = rtm.open_period(f.provider, episode.episode_id, plan["focus_plan_id"], cycle_month="2026-12")
    assert dec.cycle_month == "2026-12" and dec.focus_plan_id == plan["focus_plan_id"]
    after = f.service.governance(f.provider, CHILD, local_date="2026-11-20")
    # ...and clinical governance (P0-C) is untouched by RTM.
    assert (after.outcome, after.plan.focus_plan_id, after.cycle.cycle_id) == (
        before.outcome, before.plan.focus_plan_id, before.cycle.cycle_id)
    assert len(f.plan_rows()) == 1, "no month-labelled plan was created"
