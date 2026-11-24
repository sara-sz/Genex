"""First-plan timezone gate — no first clinical plan with a defaulted zone.

Founder decision: a child's FIRST clinical plan is created (with an explicit,
validated IANA `timezone_of_record`) and ACTIVATED by the clinician before
Week 1 can be released. The release never creates, allocates or activates a
plan; a child with no activated plan is refused (`ClinicalPlanRequired`) with
nothing written. A draft is not enough. UTC is NOT banned — an explicit UTC
choice is legitimate; only a silent default is removed.

The supported workflow, over the existing routes:

    POST /pilot/children/{child}/monthly-plan   {cycle_month, timezone_of_record}
    POST /pilot/monthly-plans/{plan}/allocations
    POST /pilot/monthly-plans/{plan}/activate
    POST /pilot/children/{child}/weekly-cycles/current/release
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone

import pytest

from pilot_backend.auth import DevAuthVerifier, VerifiedToken
from pilot_backend.config import PilotSettings
from pilot_backend.domain import entities as entities_module
from pilot_backend.domain.clinical_period import clinical_period_for
from pilot_backend.domain.goals import EditType
from pilot_backend.domain.monthly_plan import TimezoneError
from pilot_backend.integration.parent_source import InMemoryParentSessionSource
from pilot_backend.integration.week_one_release import (
    ClinicalPlanRequired,
    WeekOneReleaseService,
)
from pilot_backend.planning.service import MonthlyPlanService
from pilot_backend.transport import build_application
from pilot_backend.weekly.service import WeeklyService
from pilot_runtime.integration.static_activity_bank import StaticActivityBank

from .test_evidence_driven_generation_v2 import World, _projection, mixed
from .test_integration_identity import call
from .test_p0b1_adaptive_week_two import CHILD, _source

LA = "America/Los_Angeles"
CAPACITY = 3
#: Collections whose documents a refused first release must not touch.
WRITE_SURFACE = ("pilot_monthly_focus_plans", "pilot_monthly_goal_allocations",
                 "pilot_monthly_goal_snapshots", "pilot_identity_claims",
                 "pilot_weekly_cycles", "pilot_activity_goal_alignments",
                 "pilot_capacity_ledgers", "pilot_weekly_plan_snapshots",
                 "pilot_coverage_gaps")


class InstantClock:
    """A pinned instant shared by every service and by the route."""

    def __init__(self, iso):
        self.at(iso)

    def at(self, iso):
        self.value = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)

    def __call__(self):
        return self.value


def _pin_wall_clock(monkeypatch, clock):
    """The routes build their services per request with no injected clock, so
    they stamp with `datetime.now(timezone.utc)`. Pin that too, or an HTTP
    activation would anchor the clinical period on the real date."""
    from pilot_backend.goals import service as goals_service
    from pilot_backend.planning import service as planning_service
    from pilot_backend.weekly import service as weekly_service

    class _Pinned(datetime):
        @classmethod
        def now(cls, tz=None):
            value = clock()
            return value if tz is None else value.astimezone(tz)

    for module in (goals_service, planning_service, weekly_service):
        monkeypatch.setattr(module, "datetime", _Pinned)


class Fresh:
    """An approved, anchored goal — and NO plan of any kind yet."""

    def __init__(self, monkeypatch, at="2026-11-17T15:00:00"):
        self.clock = InstantClock(at)
        monkeypatch.setattr(entities_module, "utc_now", self.clock)
        _pin_wall_clock(monkeypatch, self.clock)
        self.world = World(source=_source(),
                           projection=_projection(states_30=mixed(),
                                                  state_24="emerging"))
        self.repos, self.goals = self.world.repos, self.world.goals
        self.provider = self.world.principal()
        self.plans = MonthlyPlanService(repos=self.repos,
                                        recorder=self.world.recorder,
                                        now=self.clock)
        self.weekly = WeeklyService(repos=self.repos,
                                    recorder=self.world.recorder,
                                    now=self.clock)
        self.service = WeekOneReleaseService(
            repos=self.repos, goals=self.goals, plans=self.plans,
            weekly=self.weekly, activity_bank=StaticActivityBank(),
            now=self.clock)
        generated = self.world.generate()
        self.goal = self.goals.approve_clinical_goal(
            self.provider, CHILD, edit_type=EditType.ACCEPTED_VERBATIM,
            suggestion_id=generated.generated[0].suggestion_ids[0])
        verifier = DevAuthVerifier("dev")
        for token, subject in (("cg", "cg-subject"), ("pr", "pr-subject")):
            verifier.add(token, VerifiedToken(subject=subject,
                                              email="fictional@example.invalid"))
        self.app = build_application(
            settings=PilotSettings.from_env({
                "PILOT_ENVIRONMENT": "dev",
                "PILOT_ALLOWED_ORIGINS": "http://localhost:3000",
                "PILOT_DEV_AUTH": "1"}),
            repos=self.repos, verifier=verifier, recorder=self.world.recorder,
            parent_source=InMemoryParentSessionSource(), log_sink=[],
            activity_bank=StaticActivityBank())

    # -- HTTP, exactly as a provider UI would call it ---------------------
    def http(self, method, path, token="pr", body=None):
        status, raw, _ = call(self.app, path, method=method,
                              bearer=f"Bearer {token}",
                              body=json.dumps(body).encode() if body is not None
                              else b"")
        return status, json.loads(raw) if isinstance(raw, (bytes, str)) else raw

    def release(self):
        return self.http("POST", f"/pilot/children/{CHILD}/weekly-cycles/current/release",
                         body={"family_declared_capacity": CAPACITY})

    def create_plan(self, month, tz):
        return self.http("POST", f"/pilot/children/{CHILD}/monthly-plan",
                         body={"cycle_month": month, "timezone_of_record": tz})

    def allocate_and_activate(self, plan_id):
        s1, b1 = self.http("POST", f"/pilot/monthly-plans/{plan_id}/allocations",
                           body={"goal_kind": "clinical",
                                 "goal_id": self.goal.clinical_goal_id,
                                 "priority_rank": 1})
        s2, b2 = self.http("POST", f"/pilot/monthly-plans/{plan_id}/activate")
        assert (s1, s2) == (200, 200), (b1, b2)
        return b2["plan"]

    def this_week_cycle(self):
        s, b = self.http("GET", f"/pilot/children/{CHILD}/this-week", token="cg")
        assert s == 200, b
        return ((b.get("week") or {}).get("week") or {}).get("cycle_id")

    def current_cycle(self):
        s, b = self.http("GET", f"/pilot/children/{CHILD}/current-cycle")
        assert s == 200, b
        return (b.get("cycle") or {}).get("cycle_id")

    def surface(self):
        return {c: sorted(json.dumps(d, sort_keys=True)
                          for _i, d in self.repos.store.list_all(c))
                for c in WRITE_SURFACE}

    def plan_rows(self):
        return self.repos.focus_plans.list_for_child(CHILD)


@pytest.fixture
def fresh(monkeypatch):
    return Fresh(monkeypatch)


# ===========================================================================
# 1. never-planned child
# ===========================================================================


def test_1_never_planned_child_is_refused_and_nothing_is_written(fresh):
    before = fresh.surface()
    with pytest.raises(ClinicalPlanRequired):
        fresh.service.release_week_one(fresh.provider, CHILD,
                                       family_declared_capacity=CAPACITY)
    assert fresh.release() == (403, {"error": "not permitted"})
    assert fresh.surface() == before, "a refused first release wrote something"
    assert fresh.plan_rows() == [] and fresh.this_week_cycle() is None


def test_the_release_service_cannot_be_handed_a_default_zone():
    """The timezone input itself is gone: there is no parameter a caller could
    omit (or default) to bootstrap a plan."""
    params = inspect.signature(WeekOneReleaseService.release_week_one).parameters
    assert "timezone_of_record" not in params
    assert not hasattr(WeekOneReleaseService, "_resolve_plan")


# ===========================================================================
# 2. draft-only child
# ===========================================================================


def test_2_a_draft_is_not_enough_and_no_second_plan_appears(fresh):
    status, body = fresh.create_plan("2026-11", LA)
    assert status == 200 and body["plan"]["state"] == "draft"
    draft_id = body["plan"]["focus_plan_id"]
    fresh.http("POST", f"/pilot/monthly-plans/{draft_id}/allocations",
               body={"goal_kind": "clinical",
                     "goal_id": fresh.goal.clinical_goal_id, "priority_rank": 1})
    draft = fresh.repos.focus_plans.get_by_id(draft_id)
    before = fresh.surface()

    assert fresh.release() == (403, {"error": "not permitted"})
    with pytest.raises(ClinicalPlanRequired):
        fresh.service.release_week_one(fresh.provider, CHILD,
                                       family_declared_capacity=CAPACITY)

    assert fresh.surface() == before
    assert fresh.plan_rows() == [draft], "draft changed or a second plan appeared"
    assert not [p for p in fresh.plan_rows() if p.timezone_of_record == "UTC"]


# ===========================================================================
# 3 + 4. explicit LA plan over HTTP, on a UTC/LA date split
# ===========================================================================

#: Mon 2026-11-16 19:00 PST == Tue 2026-11-17 03:00 UTC.
SPLIT = "2026-11-17T03:00:00"


def test_3_4_explicit_la_plan_governs_week_one_on_the_la_date(fresh):
    fresh.clock.at(SPLIT)
    status, body = fresh.create_plan("2026-11", LA)
    assert status == 200 and body["plan"]["timezone_of_record"] == LA
    plan = fresh.allocate_and_activate(body["plan"]["focus_plan_id"])
    period = clinical_period_for(fresh.repos.focus_plans.get_by_id(
        plan["focus_plan_id"]))
    assert period.starts_on == "2026-11-16", "activation date must be LA's, not UTC's"

    status, rel = fresh.release()
    assert status == 200 and rel["created"] is True
    assert rel["focus_plan_id"] == plan["focus_plan_id"]
    week = rel["week"]["week"]
    assert (week["starts_on"], week["ends_on"]) == ("2026-11-16", "2026-11-22"), \
        "Week 1 must start on the LA local date (UTC would say 11-17)"

    # Every reader agrees on the same week at the split instant.
    assert fresh.this_week_cycle() == rel["cycle_id"]
    assert fresh.current_cycle() == rel["cycle_id"]
    status, again = fresh.release()
    assert (status, again["created"], again["cycle_id"]) == (200, False,
                                                             rel["cycle_id"])
    assert [p.timezone_of_record for p in fresh.plan_rows()] == [LA]


def test_4_the_week_turns_at_la_midnight_not_utc_midnight(fresh):
    fresh.clock.at(SPLIT)
    _s, body = fresh.create_plan("2026-11", LA)
    fresh.allocate_and_activate(body["plan"]["focus_plan_id"])
    _s, week_one = fresh.release()

    # Sun 2026-11-22 23:30 PST == Mon 2026-11-23 07:30 UTC: UTC is in Week 2,
    # the family is still in Week 1 — and so are this-week, current-cycle and
    # release (a replay of Week 1, not a new Week 2).
    fresh.clock.at("2026-11-23T07:30:00")
    assert fresh.this_week_cycle() == week_one["cycle_id"]
    assert fresh.current_cycle() == week_one["cycle_id"]
    status, rel = fresh.release()
    assert (status, rel["created"], rel["cycle_id"]) == (200, False,
                                                         week_one["cycle_id"])
    assert len(fresh.repos.weekly_cycles.list_for_child(CHILD)) == 1

    # Mon 2026-11-23 00:30 PST: now the family's Week 2.
    fresh.clock.at("2026-11-23T08:30:00")
    status, rel = fresh.release()
    assert status == 200 and rel["created"] is True
    assert rel["week"]["week"]["starts_on"] == "2026-11-23"


# ===========================================================================
# 5. validation stays fail-closed
# ===========================================================================


@pytest.mark.parametrize("tz", ["", "   ", "Mars/Olympus", "PST", "utc+8"])
def test_5_invalid_or_blank_timezone_is_refused_and_writes_nothing(fresh, tz):
    before = fresh.surface()
    assert fresh.create_plan("2026-11", tz) == (403, {"error": "not permitted"})
    with pytest.raises(TimezoneError):
        fresh.plans.create_plan(fresh.provider, CHILD, "2026-11", tz)
    assert fresh.surface() == before


def test_5_a_missing_timezone_field_is_refused(fresh):
    status, body = fresh.http("POST", f"/pilot/children/{CHILD}/monthly-plan",
                              body={"cycle_month": "2026-11"})
    assert (status, body) == (403, {"error": "not permitted"})
    assert fresh.plan_rows() == []


# ===========================================================================
# 6 + 7. activated plans are unaffected; an explicit UTC plan still works
# ===========================================================================


def test_6_an_already_activated_plan_releases_exactly_as_before(fresh):
    _s, body = fresh.create_plan("2026-11", "America/New_York")
    fresh.allocate_and_activate(body["plan"]["focus_plan_id"])
    status, rel = fresh.release()
    assert status == 200 and rel["created"] is True
    assert rel["week"]["week"]["starts_on"] == "2026-11-17"


def test_7_the_positive_path_shape_an_active_utc_plan_keeps_working(fresh):
    """The deployed positive child: a plan already ACTIVE in UTC (activated
    2026-11-17T22:58Z), Week 1 released, then the adaptive Week 2. Explicit
    UTC is a legitimate choice and nothing about it is refused."""
    fresh.clock.at("2026-11-17T22:58:45")
    _s, body = fresh.create_plan("2026-11", "UTC")
    plan = fresh.allocate_and_activate(body["plan"]["focus_plan_id"])
    status, w1 = fresh.release()
    assert status == 200 and w1["week"]["week"]["starts_on"] == "2026-11-17"

    fresh.clock.at("2026-11-24T19:00:00")
    status, w2 = fresh.release()
    assert status == 200 and w2["created"] is True
    assert (w2["week"]["week"]["starts_on"], w2["week"]["week"]["ends_on"]) == (
        "2026-11-23", "2026-11-29")
    assert w2["focus_plan_id"] == plan["focus_plan_id"]
    assert [p.timezone_of_record for p in fresh.plan_rows()] == ["UTC"]
    assert fresh.this_week_cycle() == w2["cycle_id"]
    assert fresh.current_cycle() == w2["cycle_id"]
