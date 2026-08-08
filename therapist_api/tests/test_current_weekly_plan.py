"""Canonical current weekly plan (Phase 1B.2F.0, fictional dev data).

Definition under test:

    A child's current weekly plan is EXACTLY ONE WeeklyPlan for that child whose
    lifecycle status is CURRENT.

Not the first query result, not the latest `week_start_date`, not the week
containing today. Zero or several CURRENT plans is an ambiguous lifecycle that
every caller must fail closed on.
"""

from __future__ import annotations

import ast
import copy
import pathlib

from app.api import schemas as S
from app.domain.enums import WeeklyPlanStatus
from app.domain.read_models import WeeklyPlan
from app.repository import collections as C
from app.repository.memory import InMemoryRepository
from app.services import eligibility
from app.services.weekly_plan import (
    current_weekly_plan,
    current_weekly_plan_id,
    current_weekly_plans,
)
from tests.conftest import ELENA, HANNAH, OMAR, read_slice_client

CHILD = "child_maya"


def _c():
    return read_slice_client()


def _repo(c):
    return c.app.state.repo


def _bare():
    """An empty repository, so plan lifecycle can be constructed exactly."""
    return InMemoryRepository()


def _plan(repo, plan_id, status, week="2026-07-27", child=CHILD):
    repo.set(C.WEEKLY_PLANS, plan_id, WeeklyPlan(
        id=plan_id, child_id=child, week_start_date=week,
        status=status, environment="dev").model_dump())


# ── 1-10: the resolver ──────────────────────────────────────────────────────
def test_single_current_plan_is_returned():
    """(1)"""
    repo = _bare()
    _plan(repo, "wp_a", WeeklyPlanStatus.CURRENT)
    assert current_weekly_plan(repo, CHILD)["id"] == "wp_a"
    assert current_weekly_plan_id(repo, CHILD) == "wp_a"


def test_current_seeded_after_completed_is_still_returned():
    """(2)(8) The decisive case: a positional lookup would return wp_old."""
    repo = _bare()
    _plan(repo, "wp_old", WeeklyPlanStatus.COMPLETED, week="2026-07-20")
    _plan(repo, "wp_now", WeeklyPlanStatus.CURRENT, week="2026-07-27")
    assert repo.query(C.WEEKLY_PLANS, child_id=CHILD)[0]["id"] == "wp_old", "precondition"
    assert current_weekly_plan_id(repo, CHILD) == "wp_now"


def test_current_seeded_after_draft_is_still_returned():
    """(3)(8)"""
    repo = _bare()
    _plan(repo, "wp_draft", WeeklyPlanStatus.DRAFT, week="2026-08-03")
    _plan(repo, "wp_now", WeeklyPlanStatus.CURRENT, week="2026-07-27")
    assert repo.query(C.WEEKLY_PLANS, child_id=CHILD)[0]["id"] == "wp_draft", "precondition"
    assert current_weekly_plan_id(repo, CHILD) == "wp_now"


def test_completed_is_never_selected():
    """(4)"""
    repo = _bare()
    _plan(repo, "wp_a", WeeklyPlanStatus.COMPLETED)
    _plan(repo, "wp_b", WeeklyPlanStatus.COMPLETED, week="2026-08-03")
    assert current_weekly_plan(repo, CHILD) is None
    assert current_weekly_plan_id(repo, CHILD) is None


def test_draft_is_never_selected():
    """(5)"""
    repo = _bare()
    _plan(repo, "wp_a", WeeklyPlanStatus.DRAFT)
    assert current_weekly_plan(repo, CHILD) is None


def test_zero_current_plans_fails_closed():
    """(6)"""
    repo = _bare()
    assert current_weekly_plan(repo, CHILD) is None
    _plan(repo, "wp_done", WeeklyPlanStatus.COMPLETED)
    assert current_weekly_plan(repo, CHILD) is None


def test_two_current_plans_fail_closed():
    """(7) Ambiguity is never resolved by picking one."""
    repo = _bare()
    _plan(repo, "wp_a", WeeklyPlanStatus.CURRENT, week="2026-07-27")
    _plan(repo, "wp_b", WeeklyPlanStatus.CURRENT, week="2026-08-03")
    assert len(current_weekly_plans(repo, CHILD)) == 2
    assert current_weekly_plan(repo, CHILD) is None
    assert current_weekly_plan_id(repo, CHILD) is None


def test_insertion_order_does_not_affect_the_result():
    """(8) Both orderings resolve to the same plan."""
    for order in (("wp_now", "wp_old"), ("wp_old", "wp_now")):
        repo = _bare()
        for pid in order:
            _plan(repo, pid,
                  WeeklyPlanStatus.CURRENT if pid == "wp_now" else WeeklyPlanStatus.COMPLETED,
                  week="2026-07-27" if pid == "wp_now" else "2026-07-20")
        assert current_weekly_plan_id(repo, CHILD) == "wp_now", order


def test_week_start_date_ordering_does_not_affect_the_result():
    """(9) The CURRENT plan is chosen even when it is the OLDEST by date."""
    repo = _bare()
    _plan(repo, "wp_now", WeeklyPlanStatus.CURRENT, week="2026-01-05")     # oldest
    _plan(repo, "wp_done", WeeklyPlanStatus.COMPLETED, week="2026-12-28")  # newest
    latest = max(repo.query(C.WEEKLY_PLANS, child_id=CHILD),
                 key=lambda p: p["week_start_date"])
    assert latest["id"] == "wp_done", "precondition: max(date) is NOT the current plan"
    assert current_weekly_plan_id(repo, CHILD) == "wp_now"


def test_a_future_dated_draft_does_not_become_current():
    """(10) Drafting next week must not silently move the family forward."""
    repo = _bare()
    _plan(repo, "wp_now", WeeklyPlanStatus.CURRENT, week="2026-07-27")
    _plan(repo, "wp_next", WeeklyPlanStatus.DRAFT, week="2099-01-04")
    assert current_weekly_plan_id(repo, CHILD) == "wp_now"


def test_a_record_without_status_reads_as_current():
    """Backward compatibility: pre-existing records lack the key entirely."""
    repo = _bare()
    repo.set(C.WEEKLY_PLANS, "wp_legacy",
             {"id": "wp_legacy", "child_id": CHILD, "week_start_date": "2026-07-27",
              "environment": "dev"})
    assert current_weekly_plan_id(repo, CHILD) == "wp_legacy"


def test_resolver_is_scoped_to_one_child():
    repo = _bare()
    _plan(repo, "wp_maya", WeeklyPlanStatus.CURRENT)
    _plan(repo, "wp_eli", WeeklyPlanStatus.CURRENT, child="child_eli")
    assert current_weekly_plan_id(repo, CHILD) == "wp_maya"
    assert current_weekly_plan_id(repo, "child_eli") == "wp_eli"
    assert current_weekly_plan_id(repo, "child_ghost") is None


# ── 11-18: call-site migration ──────────────────────────────────────────────
SERVICE_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"
MIGRATED = (
    "approval_service.py", "proposal_service.py", "acceptance_service.py",
    "decline_service.py", "add_proposal_service.py", "eligibility.py",
    "read_service.py",
)


def test_all_seven_call_sites_import_the_canonical_resolver():
    """(11)(12)(13)(14)(15)(16)(17)"""
    for name in MIGRATED:
        source = (SERVICE_DIR / "services" / name).read_text()
        assert "from .weekly_plan import current_weekly_plan" in source, name
        assert "current_weekly_plan" in source, name


def test_no_module_selects_a_plan_positionally():
    """(18) AST pin, not a grep: `plans[0]` must not appear as EXECUTABLE code.

    A text search would trip over the resolver's own docstring, which quotes the
    old anti-pattern deliberately. Walking the AST ignores strings entirely, so
    this asserts the thing that actually matters.
    """
    offenders = []
    for path in SERVICE_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "plans"
                    and isinstance(node.slice, ast.Constant)
                    and node.slice.value == 0):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"positional plan selection remains: {offenders}"


def test_only_the_resolver_queries_weekly_plans_by_child():
    """A child-scoped WEEKLY_PLANS query is current-plan logic; centralize it.

    Lookups BY PLAN ID are unaffected — those resolve a specific known plan and
    say nothing about currency.
    """
    offenders = []
    for path in SERVICE_DIR.rglob("*.py"):
        if path.name in ("weekly_plan.py", "loader.py"):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "query"):
                continue
            args = [a for a in node.args
                    if isinstance(a, ast.Attribute) and a.attr == "WEEKLY_PLANS"]
            kw = {k.arg for k in node.keywords}
            if args and "child_id" in kw:
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"un-centralized current-plan lookup: {offenders}"


# ── 19-32: behavioral regression and multi-plan ─────────────────────────────
def test_completed_fixture_plan_does_not_become_current():
    """(27) The fictional wp_maya_prev is seeded FIRST and must never win."""
    c = _c()
    repo = _repo(c)
    plans = repo.query(C.WEEKLY_PLANS, child_id=CHILD)
    assert plans[0]["id"] == "wp_maya_prev", "precondition: completed plan is seeded first"
    assert {p["id"]: p["status"] for p in plans} == {
        "wp_maya_prev": "completed", "wp_maya": "current"}
    assert current_weekly_plan_id(repo, CHILD) == "wp_maya"


def test_weekly_plan_read_returns_the_current_plan():
    """(17)(19) A positional lookup would report the completed week here."""
    c = _c()
    out = c.get(f"/api/v1/children/{CHILD}/weekly-plan", headers=HANNAH).json()
    assert out["weekly_plan_id"] == "wp_maya"
    assert out["week_start_date"] == "2026-07-27"
    assert out["assignments"], "the current plan's activities must still be listed"


def test_add_creation_accepts_the_current_plan_and_rejects_the_completed_one():
    """(28)(29)"""
    activity = {"title": "Extra", "developmental_domain": "social_and_emotional",
                "milestone_id": "mile_turn_taking", "skill_focus": "waiting"}

    def add(client, plan, key):
        return client.post(f"/api/v1/children/{CHILD}/weekly-plan/proposals/add",
                           headers={**HANNAH, "Idempotency-Key": key},
                           json={"scheduled_day": 3, "expected_weekly_plan_id": plan,
                                 "activity": activity, "change_reason": "r",
                                 "save_scope": "child_only"})

    c = _c()
    assert add(c, "wp_maya", "ok").status_code == 200                     # (28)
    r = add(c, "wp_maya_prev", "stale")                                   # (29)
    assert r.status_code == 409 and r.json()["error"] == "weekly_plan_conflict"


def test_modify_creation_and_decisions_remain_valid_on_the_current_plan():
    """(20)(21)(22)(23)(30)"""
    from tests.test_modify_proposal import body as modify_body
    from tests.test_parent_acceptance import MAYA_MODIFY_PATH, _talking_activity

    c = _c()
    m = c.post(MAYA_MODIFY_PATH, headers={**HANNAH, "Idempotency-Key": "m1"},
               json={**modify_body(), "activity": _talking_activity(),
                     "expected_assignment_version": 1})
    assert m.status_code == 200                                           # (20)(30)
    pid = m.json()["proposal"]["proposal_id"]
    repo = _repo(c)
    proposal = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]
    assert eligibility.evaluate_parent_decision(                          # (23)
        repo, CHILD, proposal) == eligibility.ELIGIBLE

    assignment = repo.query(C.PLAN_ASSIGNMENTS, id="assign_maya_bubbles")[0]
    acc = c.post(f"/api/v1/children/{CHILD}/proposals/{pid}/accept",
                 headers={**ELENA, "Idempotency-Key": "a1"},
                 json={"expected_proposal_version": 1,
                       "expected_assignment_version": assignment["version"]})
    assert acc.status_code == 200                                         # (21)

    c2 = _c()
    m2 = c2.post(MAYA_MODIFY_PATH, headers={**HANNAH, "Idempotency-Key": "m2"},
                 json={**modify_body(), "activity": _talking_activity(),
                       "expected_assignment_version": 1})
    a2 = _repo(c2).query(C.PLAN_ASSIGNMENTS, id="assign_maya_bubbles")[0]
    dec = c2.post(f"/api/v1/children/{CHILD}/proposals/"
                  f"{m2.json()['proposal']['proposal_id']}/decline",
                  headers={**ELENA, "Idempotency-Key": "d1"},
                  json={"expected_proposal_version": 1,
                        "expected_assignment_version": a2["version"]})
    assert dec.status_code == 200                                         # (22)


def _make_ambiguous(c):
    """Give Maya a SECOND current plan — an ambiguous lifecycle."""
    _plan(_repo(c), "wp_maya_dup", WeeklyPlanStatus.CURRENT, week="2026-08-03")


def test_ambiguous_current_lifecycle_fails_every_write_closed():
    """(31)(32) Two CURRENT plans: no write may guess which one is meant."""
    from tests.test_modify_proposal import body as modify_body
    from tests.test_parent_acceptance import MAYA_MODIFY_PATH, _talking_activity

    activity = {"title": "Extra", "developmental_domain": "social_and_emotional",
                "milestone_id": "mile_turn_taking", "skill_focus": "waiting"}

    # (a) Add creation
    c = _c()
    _make_ambiguous(c)
    before = _snapshot(c)
    r = c.post(f"/api/v1/children/{CHILD}/weekly-plan/proposals/add",
               headers={**HANNAH, "Idempotency-Key": "amb"},
               json={"scheduled_day": 3, "expected_weekly_plan_id": "wp_maya",
                     "activity": activity, "change_reason": "r",
                     "save_scope": "child_only"})
    assert r.status_code == 409 and r.json()["error"] == "weekly_plan_conflict"
    assert _snapshot(c) == before                                         # (32)

    # (b) Modify creation
    c = _c()
    _make_ambiguous(c)
    before = _snapshot(c)
    m = c.post(MAYA_MODIFY_PATH, headers={**HANNAH, "Idempotency-Key": "amb2"},
               json={**modify_body(), "activity": _talking_activity(),
                     "expected_assignment_version": 1})
    assert m.status_code == 409
    assert m.json()["error"] == "invalid_modify_proposal_transition"
    assert _snapshot(c) == before                                         # (32)

    # (c) Modify accept + decline, on a proposal created while unambiguous
    for verb, err, key in (("accept", "invalid_parent_accept_transition", "k1"),
                           ("decline", "invalid_parent_decline_transition", "k2")):
        c = _c()
        created = c.post(MAYA_MODIFY_PATH, headers={**HANNAH, "Idempotency-Key": f"c-{key}"},
                         json={**modify_body(), "activity": _talking_activity(),
                               "expected_assignment_version": 1})
        assert created.status_code == 200
        pid = created.json()["proposal"]["proposal_id"]
        _make_ambiguous(c)
        before = _snapshot(c)
        assignment = _repo(c).query(C.PLAN_ASSIGNMENTS, id="assign_maya_bubbles")[0]
        res = c.post(f"/api/v1/children/{CHILD}/proposals/{pid}/{verb}",
                     headers={**ELENA, "Idempotency-Key": key},
                     json={"expected_proposal_version": 1,
                           "expected_assignment_version": assignment["version"]})
        assert res.status_code == 409, verb
        assert res.json()["error"] == err, verb
        assert _snapshot(c) == before, verb                               # (32)


def test_ambiguous_current_lifecycle_makes_modify_ineligible():
    """(31) The read surface fails closed too, so no button is offered."""
    from tests.test_modify_proposal import body as modify_body
    from tests.test_parent_acceptance import MAYA_MODIFY_PATH, _talking_activity

    c = _c()
    m = c.post(MAYA_MODIFY_PATH, headers={**HANNAH, "Idempotency-Key": "e1"},
               json={**modify_body(), "activity": _talking_activity(),
                     "expected_assignment_version": 1})
    pid = m.json()["proposal"]["proposal_id"]
    repo = _repo(c)
    assert eligibility.evaluate_parent_decision(
        repo, CHILD, repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]) == eligibility.ELIGIBLE

    _make_ambiguous(c)
    assert eligibility.evaluate_parent_decision(
        repo, CHILD, repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]) == eligibility.INELIGIBLE


def test_ambiguous_current_lifecycle_is_not_disclosed_by_the_plan_read():
    """The read keeps its existing empty-plan shape; nothing names the ambiguity.

    Note the deliberate asymmetry: the plan ENVELOPE empties while the child's
    assignments still list, because assignments are queried by child and were
    never plan-scoped in this response. That is exactly the pre-existing
    zero-plan behavior, preserved rather than redesigned.
    """
    c = _c()
    _make_ambiguous(c)
    r = c.get(f"/api/v1/children/{CHILD}/weekly-plan", headers=HANNAH)
    assert r.status_code == 200
    out = r.json()
    assert out["weekly_plan_id"] == "" and out["week_start_date"] == ""
    # No plan id, no lifecycle vocabulary, and no hint that several plans exist.
    for marker in ("wp_maya_dup", "wp_maya_prev", "ambiguous", "multiple",
                   '"status"', "completed", "draft"):
        assert marker not in r.text, marker
    assert out["assignments"], "the child's own activities still list, as before"


def test_parent_add_reads_and_therapist_reads_remain_green():
    """(24)(25)(26)"""
    activity = {"title": "Extra", "developmental_domain": "social_and_emotional",
                "milestone_id": "mile_turn_taking", "skill_focus": "waiting"}
    c = _c()
    add = c.post(f"/api/v1/children/{CHILD}/weekly-plan/proposals/add",
                 headers={**HANNAH, "Idempotency-Key": "pa"},
                 json={"scheduled_day": 3, "expected_weekly_plan_id": "wp_maya",
                       "activity": activity, "change_reason": "r",
                       "save_scope": "child_only"})
    assert add.status_code == 200                                         # (24)
    pid = add.json()["proposal"]["proposal_id"]

    parent_detail = c.get(f"/api/v1/children/{CHILD}/proposals/{pid}", headers=ELENA)
    assert parent_detail.status_code == 200                               # (25)
    assert parent_detail.json()["destination"]["day_label"] == "Thursday"

    therapist_list = c.get(f"/api/v1/children/{CHILD}/proposals", headers=HANNAH)
    assert therapist_list.status_code == 200                              # (26)
    row = next(i for i in therapist_list.json()["items"] if i["proposal_id"] == pid)
    assert row["destination_scheduled_day"] == 3


# ── 33-35: API privacy — status is INTERNAL ─────────────────────────────────
def test_weekly_plan_response_does_not_gain_status():
    """(33)"""
    assert "status" not in S.WeeklyPlanResponse.model_fields
    c = _c()
    r = c.get(f"/api/v1/children/{CHILD}/weekly-plan", headers=HANNAH)
    assert set(r.json()) == {"child_id", "weekly_plan_id", "week_start_date",
                             "assignments"}
    for marker in ("wp_maya_prev", "completed", "draft"):
        assert marker not in r.text


def test_no_parent_or_therapist_response_exposes_plan_lifecycle():
    """(34)(35)"""
    activity = {"title": "Extra", "developmental_domain": "social_and_emotional",
                "milestone_id": "mile_turn_taking", "skill_focus": "waiting"}
    c = _c()
    pid = c.post(f"/api/v1/children/{CHILD}/weekly-plan/proposals/add",
                 headers={**HANNAH, "Idempotency-Key": "pv"},
                 json={"scheduled_day": 3, "expected_weekly_plan_id": "wp_maya",
                       "activity": activity, "change_reason": "r",
                       "save_scope": "child_only"}).json()["proposal"]["proposal_id"]
    blobs = [
        c.get(f"/api/v1/children/{CHILD}/proposals", headers=ELENA).text,
        c.get(f"/api/v1/children/{CHILD}/proposals/{pid}", headers=ELENA).text,
        c.get(f"/api/v1/children/{CHILD}/proposals", headers=HANNAH).text,
        c.get(f"/api/v1/children/{CHILD}/proposals/{pid}", headers=HANNAH).text,
        c.get(f"/api/v1/children/{CHILD}/weekly-plan", headers=HANNAH).text,
    ]
    for blob in blobs:
        assert "wp_maya_prev" not in blob
        assert '"status": "completed"' not in blob
        assert '"status": "draft"' not in blob


def test_openapi_does_not_document_plan_lifecycle():
    c = _c()
    schema = c.app.openapi()
    assert len(schema["paths"]) == 22, "this phase adds no route"
    assert "WeeklyPlanStatus" not in schema["components"]["schemas"]
    assert "status" not in schema["components"]["schemas"][
        "WeeklyPlanResponse"]["properties"]


# ── 36-38: read-only ────────────────────────────────────────────────────────
WATCHED = (C.WEEKLY_PLANS, C.PLAN_ASSIGNMENTS, C.PLAN_CHANGE_PROPOSALS,
           C.ACTIVITY_VERSIONS, C.AUDIT_EVENTS, C.IDEMPOTENCY_RECORDS,
           C.CONNECTIONS, C.CHILDREN)


def _snapshot(c):
    repo = _repo(c)
    return {n: copy.deepcopy(sorted(repo.query(n), key=lambda r: r["id"]))
            for n in WATCHED}


def test_resolver_and_eligibility_mutate_nothing():
    """(36)(37)(38)"""
    c = _c()
    repo = _repo(c)
    before = _snapshot(c)
    for _ in range(5):
        current_weekly_plan(repo, CHILD)
        current_weekly_plan_id(repo, CHILD)
        current_weekly_plans(repo, CHILD)
        for p in repo.query(C.PLAN_CHANGE_PROPOSALS, child_id=CHILD):
            eligibility.evaluate_parent_decision(repo, CHILD, p)
            eligibility.proposal_is_safe_to_show(repo, CHILD, p)
    assert _snapshot(c) == before
    # The resolver never promotes a draft or completes a plan.
    assert {p["id"]: p["status"] for p in repo.query(C.WEEKLY_PLANS, child_id=CHILD)} == {
        "wp_maya_prev": "completed", "wp_maya": "current"}


def test_repeated_weekly_plan_reads_are_identical_and_inert():
    """(38)"""
    c = _c()
    before = _snapshot(c)
    first = c.get(f"/api/v1/children/{CHILD}/weekly-plan", headers=HANNAH).json()
    for _ in range(5):
        assert c.get(f"/api/v1/children/{CHILD}/weekly-plan", headers=HANNAH).json() == first
    assert _snapshot(c) == before


# ── integrity ───────────────────────────────────────────────────────────────
def test_weekly_plan_module_imports_no_parent_api_or_cloud_sdk():
    """(42)(44)"""
    import inspect

    from app.services import weekly_plan

    banned = ("firebase", "firestore", "google", "genex_core", "boto3", "azure")
    for node in ast.walk(ast.parse(inspect.getsource(weekly_plan))):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        for name in names:
            assert not any(b in name.lower() for b in banned), name
