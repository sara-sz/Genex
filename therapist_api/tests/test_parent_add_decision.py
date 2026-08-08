"""Parent acceptance and decline of an ADD proposal (Phase 1B.2F, fictional data).

Add is not Modify. Accepting an Add **appends** exactly one activity to the
destination weekday: nothing is retired, nothing is replaced, no existing
assignment's version or `display_order` changes.

Two invariants carry most of the weight here:

* **Only a pending Add targeting the canonical CURRENT weekly plan is
  actionable.** A historical Add stays readable but cannot be accepted OR
  declined — a stale proposal is not a live decision.
* **`display_order` is allocated inside the acceptance transaction** as
  `max(order) + 1`, never `len()`, so gaps survive and concurrent acceptances on
  one day cannot collide.
"""

from __future__ import annotations

import copy
import threading

from app.domain.enums import (
    AssignmentStatus,
    PlanApprovalStatus,
    WeeklyPlanStatus,
)
from app.domain.read_models import PlanAssignment, WeeklyPlan
from app.repository import collections as C
from app.services import add_decision_service as A
from app.services import assignment_order, eligibility
from app.services.weekly_plan import current_weekly_plan_id
from tests.conftest import ELENA, HANNAH, OMAR, UNCONNECTED, read_slice_client

CHILD = "child_maya"
PLAN = "wp_maya"
OLD_PLAN = "wp_maya_prev"          # fictional COMPLETED plan
MONDAY = 0                         # holds assign_maya_bubbles at display_order 0
THURSDAY = 3                       # empty in the fictional plan

WATCHED = (C.PLAN_ASSIGNMENTS, C.PLAN_CHANGE_PROPOSALS, C.ACTIVITY_VERSIONS,
           C.AUDIT_EVENTS, C.IDEMPOTENCY_RECORDS, C.WEEKLY_PLANS)


def activity(**over):
    a = {"title": "Articulation practice",
         "developmental_domain": "talking_and_communicating",
         "milestone_id": "mile_request_items", "skill_focus": "clear sounds",
         "duration_minutes": 10, "materials": ["mirror"],
         "setup": "Sit facing a mirror.",
         "parent_instructions": ["Model the sound slowly."]}
    a.update(over)
    return a


def _c():
    return read_slice_client()


def _repo(c):
    return c.app.state.repo


def _snapshot(c):
    repo = _repo(c)
    return {n: copy.deepcopy(sorted(repo.query(n), key=lambda r: r["id"])) for n in WATCHED}


def _add(c, key="a1", day=MONDAY, plan=PLAN, title=None, child=CHILD):
    r = c.post(f"/api/v1/children/{child}/weekly-plan/proposals/add",
               headers={**HANNAH, "Idempotency-Key": key},
               json={"scheduled_day": day, "expected_weekly_plan_id": plan,
                     "activity": activity(**({"title": title} if title else {})),
                     "change_reason": "Extra practice.", "save_scope": "child_only"})
    assert r.status_code == 200, r.text
    return r.json()["proposal"]["proposal_id"]


def _accept(c, pid, key="acc", version=1, headers=ELENA, child=CHILD, body=None):
    return c.post(f"/api/v1/children/{child}/proposals/{pid}/accept",
                  headers={**headers, "Idempotency-Key": key},
                  json=body if body is not None else {"expected_proposal_version": version})


def _decline(c, pid, key="dec", version=1, headers=ELENA, child=CHILD, body=None):
    return c.post(f"/api/v1/children/{child}/proposals/{pid}/decline",
                  headers={**headers, "Idempotency-Key": key},
                  json=body if body is not None else {"expected_proposal_version": version})


def _sibling(c, assignment_id, order, day=MONDAY, plan=PLAN, child=CHILD,
             status=AssignmentStatus.CURRENT, version_id="ver_turn_taking_v1"):
    repo = _repo(c)
    base = repo.query(C.PLAN_ASSIGNMENTS, id="assign_maya_bubbles")[0]
    repo.set(C.PLAN_ASSIGNMENTS, assignment_id, PlanAssignment(
        id=assignment_id, weekly_plan_id=plan, child_id=child,
        activity_template_id=base["activity_template_id"],
        activity_version_id=version_id, scheduled_day=day, display_order=order,
        plan_approval_status=PlanApprovalStatus.APPROVED, assignment_status=status,
        version=1, environment="dev").model_dump())


def _day(c, day=MONDAY, plan=PLAN, child=CHILD):
    return assignment_order.assignment_order_map(
        assignment_order.current_assignments_for_day(_repo(c), child, plan, day))


def _retarget(c, pid, plan_id):
    """Point a proposal at another weekly plan (to build stale/draft states)."""
    repo = _repo(c)
    p = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]
    p["weekly_plan_id"] = plan_id
    repo.set(C.PLAN_CHANGE_PROPOSALS, pid, p)
    return p


def _set_plan_status(c, plan_id, status):
    repo = _repo(c)
    p = repo.query(C.WEEKLY_PLANS, id=plan_id)[0]
    p["status"] = status.value
    repo.set(C.WEEKLY_PLANS, plan_id, p)


def _draft_plan(c, plan_id="wp_maya_draft", week="2026-08-10"):
    _repo(c).set(C.WEEKLY_PLANS, plan_id, WeeklyPlan(
        id=plan_id, child_id=CHILD, week_start_date=week,
        status=WeeklyPlanStatus.DRAFT, environment="dev").model_dump())
    return plan_id


# ── 2-4: branch / foundation ────────────────────────────────────────────────
def test_current_plan_comes_from_the_canonical_resolver():
    """(3)(4)(32) Not the first query result — the COMPLETED plan is seeded first."""
    c = _c()
    repo = _repo(c)
    assert repo.query(C.WEEKLY_PLANS, child_id=CHILD)[0]["id"] == OLD_PLAN, "precondition"
    assert current_weekly_plan_id(repo, CHILD) == PLAN


# ── 5-15: Add accept ────────────────────────────────────────────────────────
def test_parent_accepts_an_add_and_exactly_one_activity_is_appended():
    """(5)(6)(7)(8)(9)(10)(11)(12)(13)(14)(15)"""
    c = _c()
    repo = _repo(c)
    _sibling(c, "assign_sib", order=1)
    before = {a["id"]: dict(a) for a in repo.query(C.PLAN_ASSIGNMENTS, child_id=CHILD)}
    n_before = len(before)
    pid = _add(c)
    version_id = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]["proposed_activity_version_id"]

    r = _accept(c, pid)
    assert r.status_code == 200                                          # (5)
    out = r.json()
    assert out["proposal"]["proposal_status"] == "accepted"              # (6)
    assert out["proposal"]["version"] == 2                               # (7)
    new_id = out["proposal"]["resulting_assignment_id"]
    assert new_id                                                        # (8)

    after = {a["id"]: dict(a) for a in repo.query(C.PLAN_ASSIGNMENTS, child_id=CHILD)}
    assert len(after) == n_before + 1                                    # (9)
    created = after[new_id]
    assert created["activity_version_id"] == version_id                  # (10)
    assert created["scheduled_day"] == MONDAY                            # (11)
    assert created["assignment_status"] == "current"                     # (12)
    assert created["plan_approval_status"] == "approved"
    assert created["version"] == 1
    assert created["source_proposal_id"] == pid
    # (14) an Add supersedes nothing
    assert created["replaces_assignment_id"] is None
    assert created["replaced_by_assignment_id"] is None

    for aid, row in before.items():                                      # (13)(15)
        assert after[aid] == row, f"{aid} changed"
    assert all(after[a]["assignment_status"] == "current" for a in before)
    # The internally stored proposal also records the link.
    assert repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]["resulting_assignment_id"] == new_id


# ── 16-23: display_order allocation ─────────────────────────────────────────
def test_empty_day_allocates_order_zero():
    """(16)"""
    c = _c()
    assert _day(c, THURSDAY) == {}
    pid = _add(c, day=THURSDAY)
    r = _accept(c, pid)
    assert r.json()["added_assignment"]["display_order"] == 0


def test_day_with_one_activity_allocates_one():
    """(17)"""
    c = _c()
    assert _day(c) == {"assign_maya_bubbles": 0}
    r = _accept(c, _add(c))
    assert r.json()["added_assignment"]["display_order"] == 1


def test_day_with_two_activities_allocates_two():
    """(18)"""
    c = _c()
    _sibling(c, "assign_sib", order=1)
    r = _accept(c, _add(c))
    assert r.json()["added_assignment"]["display_order"] == 2


def test_gapped_day_allocates_max_plus_one_not_the_count():
    """(19)(20)(21) [0,3,7] -> 8. len() would give 3 and collide."""
    c = _c()
    _sibling(c, "assign_g3", order=3)
    _sibling(c, "assign_g7", order=7)
    assert sorted(_day(c).values()) == [0, 3, 7]
    r = _accept(c, _add(c))
    assert r.json()["added_assignment"]["display_order"] == 8            # (19)
    # (20) gaps preserved, (21) siblings not renumbered
    assert sorted(_day(c).values()) == [0, 3, 7, 8]
    assert _day(c)["assign_g3"] == 3 and _day(c)["assign_g7"] == 7


def test_next_display_order_is_pure_and_correct():
    """(19) The allocator itself, independent of the transaction."""
    assert A.next_display_order([]) == 0
    assert A.next_display_order([{"display_order": 0}]) == 1
    assert A.next_display_order([{"display_order": 0}, {"display_order": 1}]) == 2
    assert A.next_display_order(
        [{"display_order": 0}, {"display_order": 3}, {"display_order": 7}]) == 8
    # Order of the input list must not matter.
    assert A.next_display_order(
        [{"display_order": 7}, {"display_order": 0}, {"display_order": 3}]) == 8


def test_duplicate_existing_display_order_fails_closed_and_writes_nothing():
    """(22)(23) The duplicate must appear AFTER creation.

    Add *creation* already rejects an ambiguously ordered day, so introducing the
    collision first would test the creation guard instead of the acceptance one.
    Creating the proposal into a clean day and corrupting the day afterwards is
    what actually exercises the allocation path.
    """
    c = _c()
    pid = _add(c)
    _sibling(c, "assign_dupe", order=0)          # now collides with bubbles
    before = _snapshot(c)
    r = _accept(c, pid)
    assert r.status_code == 409
    assert r.json()["error"] == "duplicate_assignment_display_order"
    assert _snapshot(c) == before                                        # (23)


# ── 24-32: current-plan guard ───────────────────────────────────────────────
def test_current_plan_add_accept_succeeds():
    """(24)"""
    c = _c()
    assert _accept(c, _add(c)).status_code == 200


def test_completed_plan_add_accept_is_a_weekly_plan_conflict():
    """(25)(29)(30)(31)"""
    c = _c()
    pid = _add(c)
    _retarget(c, pid, OLD_PLAN)                  # COMPLETED fixture plan
    before = _snapshot(c)
    r = _accept(c, pid, key="stale")
    assert r.status_code == 409
    assert r.json()["error"] == "weekly_plan_conflict"
    assert _snapshot(c) == before                # (29)(30)(31): nothing at all


def test_draft_plan_add_accept_is_a_weekly_plan_conflict():
    """(26)"""
    c = _c()
    pid = _add(c)
    _retarget(c, pid, _draft_plan(c))
    before = _snapshot(c)
    r = _accept(c, pid, key="draft")
    assert r.status_code == 409 and r.json()["error"] == "weekly_plan_conflict"
    assert _snapshot(c) == before


def test_zero_current_plans_fails_closed():
    """(27)"""
    c = _c()
    pid = _add(c)
    _set_plan_status(c, PLAN, WeeklyPlanStatus.COMPLETED)   # now zero CURRENT
    before = _snapshot(c)
    r = _accept(c, pid, key="zero")
    assert r.status_code == 409 and r.json()["error"] == "weekly_plan_conflict"
    assert _snapshot(c) == before


def test_multiple_current_plans_fail_closed():
    """(28)"""
    c = _c()
    pid = _add(c)
    _repo(c).set(C.WEEKLY_PLANS, "wp_dup", WeeklyPlan(
        id="wp_dup", child_id=CHILD, week_start_date="2026-08-10",
        status=WeeklyPlanStatus.CURRENT, environment="dev").model_dump())
    before = _snapshot(c)
    r = _accept(c, pid, key="many")
    assert r.status_code == 409 and r.json()["error"] == "weekly_plan_conflict"
    assert _snapshot(c) == before


def test_plan_belonging_to_another_child_fails_closed():
    """(35)"""
    c = _c()
    pid = _add(c)
    _retarget(c, pid, "wp_noah")
    before = _snapshot(c)
    r = _accept(c, pid, key="other")
    assert r.status_code == 409 and r.json()["error"] == "weekly_plan_conflict"
    assert _snapshot(c) == before


def test_missing_weekly_plan_fails_closed():
    """(36)"""
    c = _c()
    pid = _add(c)
    _retarget(c, pid, "wp_ghost")
    r = _accept(c, pid, key="ghost")
    assert r.status_code == 409 and r.json()["error"] == "weekly_plan_conflict"


# ── 33-42: Add decline ──────────────────────────────────────────────────────
def test_parent_declines_a_current_add():
    """(33)(34)(35)(36)(37)(38)(39)(40)"""
    c = _c()
    repo = _repo(c)
    _sibling(c, "assign_sib", order=1)
    pid = _add(c)
    version_id = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]["proposed_activity_version_id"]
    version_before = copy.deepcopy(repo.query(C.ACTIVITY_VERSIONS, id=version_id)[0])
    assignments_before = {a["id"]: dict(a) for a in repo.query(C.PLAN_ASSIGNMENTS, child_id=CHILD)}
    orders_before = _day(c)

    r = _decline(c, pid)
    assert r.status_code == 200                                          # (33)
    out = r.json()
    assert out["proposal"]["proposal_status"] == "declined"              # (34)
    assert out["proposal"]["version"] == 2                               # (35)
    assert out["proposal"]["resulting_assignment_id"] is None            # (36)
    assert out["destination_scheduled_day"] == MONDAY
    assert "added_assignment" not in out

    after = {a["id"]: dict(a) for a in repo.query(C.PLAN_ASSIGNMENTS, child_id=CHILD)}
    assert after == assignments_before                                   # (37)(38)
    assert _day(c) == orders_before                                      # (39)
    assert repo.query(C.ACTIVITY_VERSIONS, id=version_id)[0] == version_before   # (40)


def test_historical_add_decline_is_a_weekly_plan_conflict():
    """(41) A stale proposal is not a live decision, even to refuse."""
    c = _c()
    pid = _add(c)
    _retarget(c, pid, OLD_PLAN)
    before = _snapshot(c)
    r = _decline(c, pid, key="stale-dec")
    assert r.status_code == 409 and r.json()["error"] == "weekly_plan_conflict"
    assert _snapshot(c) == before


def test_draft_plan_add_decline_is_a_weekly_plan_conflict():
    """(42)"""
    c = _c()
    pid = _add(c)
    _retarget(c, pid, _draft_plan(c))
    r = _decline(c, pid, key="draft-dec")
    assert r.status_code == 409 and r.json()["error"] == "weekly_plan_conflict"


def test_declining_twice_and_accepting_after_decline_both_conflict():
    """Canonical decided-state conflicts."""
    c = _c()
    pid = _add(c)
    assert _decline(c, pid, key="d1").status_code == 200
    r = _decline(c, pid, key="d2", version=2)
    assert r.status_code == 409 and r.json()["error"] == "proposal_already_decided"
    r2 = _accept(c, pid, key="a-after", version=2)
    assert r2.status_code == 409 and r2.json()["error"] == "proposal_already_decided"


# ── 43-52: eligibility ──────────────────────────────────────────────────────
def _flags(c, pid):
    repo = _repo(c)
    return eligibility.evaluate_parent_decision(
        repo, CHILD, repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0])


def test_valid_current_pending_add_is_eligible():
    """(43)"""
    c = _c()
    assert _flags(c, _add(c)) == eligibility.ELIGIBLE


def test_completed_and_draft_plan_adds_are_ineligible():
    """(44)(45)"""
    c = _c()
    pid = _add(c)
    _retarget(c, pid, OLD_PLAN)
    assert _flags(c, pid) == eligibility.INELIGIBLE
    c2 = _c()
    pid2 = _add(c2)
    _retarget(c2, pid2, _draft_plan(c2))
    assert _flags(c2, pid2) == eligibility.INELIGIBLE


def test_zero_and_multiple_current_plans_are_ineligible():
    """(46)(47)"""
    c = _c()
    pid = _add(c)
    _set_plan_status(c, PLAN, WeeklyPlanStatus.COMPLETED)
    assert _flags(c, pid) == eligibility.INELIGIBLE

    c2 = _c()
    pid2 = _add(c2)
    _repo(c2).set(C.WEEKLY_PLANS, "wp_dup", WeeklyPlan(
        id="wp_dup", child_id=CHILD, week_start_date="2026-08-10",
        status=WeeklyPlanStatus.CURRENT, environment="dev").model_dump())
    assert _flags(c2, pid2) == eligibility.INELIGIBLE


def test_decided_adds_are_ineligible():
    """(48)(49)"""
    c = _c()
    pid = _add(c)
    assert _accept(c, pid).status_code == 200
    assert _flags(c, pid) == eligibility.INELIGIBLE

    c2 = _c()
    pid2 = _add(c2)
    assert _decline(c2, pid2).status_code == 200
    assert _flags(c2, pid2) == eligibility.INELIGIBLE


def test_malformed_adds_are_ineligible():
    """(50)"""
    c = _c()
    repo = _repo(c)
    for changes in ({"destination_scheduled_day": None},
                    {"destination_scheduled_day": 7},
                    {"proposed_activity_version_id": None},
                    {"proposed_activity_version_id": "ver_ghost"},
                    {"weekly_plan_id": None}):
        pid = _add(c, key=f"m-{list(changes)[0]}-{changes}")
        p = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]
        p.update(changes)
        repo.set(C.PLAN_CHANGE_PROPOSALS, pid, p)
        assert _flags(c, pid) == eligibility.INELIGIBLE, changes


def test_duplicate_day_order_makes_an_add_ineligible():
    """(50)"""
    c = _c()
    pid = _add(c)
    assert _flags(c, pid) == eligibility.ELIGIBLE
    _sibling(c, "assign_dupe", order=0)
    assert _flags(c, pid) == eligibility.INELIGIBLE


def test_eligibility_allocates_and_mutates_nothing():
    """(51)(52)"""
    c = _c()
    _sibling(c, "assign_sib", order=1)
    pid = _add(c)
    before = _snapshot(c)
    for _ in range(5):
        _flags(c, pid)
        eligibility.proposal_is_safe_to_show(
            _repo(c), CHILD, _repo(c).query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0])
    assert _snapshot(c) == before
    assert _day(c) == {"assign_maya_bubbles": 0, "assign_sib": 1}


# ── 53-60: parent reads around a decision ───────────────────────────────────
def _detail(c, pid, headers=ELENA):
    return c.get(f"/api/v1/children/{CHILD}/proposals/{pid}", headers=headers)


def _item(c, pid, headers=ELENA):
    payload = c.get(f"/api/v1/children/{CHILD}/proposals", headers=headers).json()
    return next((i for i in payload["items"] if i["proposal_id"] == pid), None)


def test_pending_current_add_reads_as_actionable():
    """(53)(54)"""
    c = _c()
    pid = _add(c)
    assert _item(c, pid)["decision"] == {
        "needs_parent_attention": True, "can_accept": True, "can_decline": True}
    d = _detail(c, pid).json()["decision"]
    assert (d["can_accept"], d["can_decline"], d["needs_parent_attention"]) == (
        True, True, True)
    assert d["accepted_or_declined_at"] is None


def test_reads_after_accept_and_decline_are_non_actionable():
    """(55)(56)"""
    for verb, expected in (("accept", "accepted"), ("decline", "declined")):
        c = _c()
        pid = _add(c)
        r = (_accept if verb == "accept" else _decline)(c, pid)
        assert r.status_code == 200
        item = _item(c, pid)
        assert item["proposal_status"] == expected
        assert item["decision"] == {"needs_parent_attention": False,
                                    "can_accept": False, "can_decline": False}
        detail = _detail(c, pid).json()
        assert detail["proposal"]["proposal_status"] == expected
        assert detail["decision"]["can_accept"] is False
        assert detail["decision"]["can_decline"] is False
        assert detail["decision"]["accepted_or_declined_at"] is not None


def test_historical_add_remains_readable_but_non_actionable():
    """(57)(58)"""
    c = _c()
    pid = _add(c)
    _retarget(c, pid, OLD_PLAN)
    item = _item(c, pid)
    assert item is not None, "a historical Add stays readable"
    assert item["decision"] == {"needs_parent_attention": False,
                                "can_accept": False, "can_decline": False}
    assert _detail(c, pid).status_code == 200


def test_parent_reads_expose_no_order_or_assignment_internals():
    """(59)(60)(114)(115)(116)(117)"""
    c = _c()
    _sibling(c, "assign_sib", order=1)
    pid = _add(c)
    _accept(c, pid)
    blobs = [c.get(f"/api/v1/children/{CHILD}/proposals", headers=ELENA).text,
             _detail(c, pid).text]
    for blob in blobs:
        for marker in ("display_order", "resulting_assignment_id", "weekly_plan_id",
                       "activity_version_id", "activity_template_id", "assignment_id",
                       "audit_event_id", "idempotency", "request_hash",
                       "destination_target_token", "save_scope", "source_proposal_id"):
            assert marker not in blob, marker


# ── 61-74: idempotency ──────────────────────────────────────────────────────
def _counts(c):
    repo = _repo(c)
    return {n: len(repo.query(n)) for n in
            (C.PLAN_ASSIGNMENTS, C.AUDIT_EVENTS, C.IDEMPOTENCY_RECORDS,
             C.PLAN_CHANGE_PROPOSALS)}


def test_accept_replay_is_idempotent():
    """(61)(62)(63)(64)(65)(66)(67)"""
    c = _c()
    _sibling(c, "assign_g7", order=7)
    pid = _add(c)
    first = _accept(c, pid, key="same")
    assert first.status_code == 200 and first.json()["idempotent_replay"] is False
    assert first.json()["added_assignment"]["display_order"] == 8
    counts = _counts(c)
    orders = _day(c)

    second = _accept(c, pid, key="same")
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True                    # (62)
    assert second.json()["proposal"] == first.json()["proposal"]
    assert second.json()["audit_event_id"] == first.json()["audit_event_id"]
    assert _counts(c) == counts                                          # (63)(64)(65)
    assert _repo(c).query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]["version"] == 2   # (66)
    assert _day(c) == orders                                             # (67)


def test_accept_same_key_changed_request_conflicts():
    """(68)"""
    c = _c()
    pid = _add(c)
    assert _accept(c, pid, key="k").status_code == 200
    r = _accept(c, pid, key="k", version=2)
    assert r.status_code == 409 and r.json()["error"] == "idempotency_key_conflict"


def test_decline_replay_is_idempotent():
    """(69)(70)(71)(72)(73)"""
    c = _c()
    pid = _add(c)
    assignments_before = len(_repo(c).query(C.PLAN_ASSIGNMENTS))
    first = _decline(c, pid, key="d")
    assert first.status_code == 200 and first.json()["idempotent_replay"] is False
    counts = _counts(c)

    second = _decline(c, pid, key="d")
    assert second.json()["idempotent_replay"] is True                    # (69)
    assert second.json()["proposal"] == first.json()["proposal"]
    assert _counts(c) == counts                                          # (70)(72)(73)
    assert len(_repo(c).query(C.PLAN_ASSIGNMENTS)) == assignments_before  # (71)


def test_decline_same_key_changed_request_conflicts():
    """(74)"""
    c = _c()
    pid = _add(c)
    assert _decline(c, pid, key="k").status_code == 200
    r = _decline(c, pid, key="k", version=2)
    assert r.status_code == 409 and r.json()["error"] == "idempotency_key_conflict"


def test_different_key_after_acceptance_conflicts_without_a_second_assignment():
    """A new key against a decided proposal must not create anything."""
    c = _c()
    pid = _add(c)
    assert _accept(c, pid, key="one").status_code == 200
    counts = _counts(c)
    r = _accept(c, pid, key="two", version=2)
    assert r.status_code == 409 and r.json()["error"] == "proposal_already_decided"
    assert _counts(c) == counts


# ── 75-81: concurrency ──────────────────────────────────────────────────────
def test_two_same_day_adds_accepted_sequentially_get_n_and_n_plus_one():
    """(75)(79)"""
    c = _c()
    a = _add(c, key="A", title="Add A")
    b = _add(c, key="B", title="Add B")
    ra = _accept(c, a, key="acc-a")
    rb = _accept(c, b, key="acc-b")
    assert ra.status_code == 200 and rb.status_code == 200
    assert ra.json()["added_assignment"]["display_order"] == 1           # (75)
    assert rb.json()["added_assignment"]["display_order"] == 2
    assert _day(c)["assign_maya_bubbles"] == 0                           # (79)
    assert sorted(_day(c).values()) == [0, 1, 2]


def test_reverse_acceptance_order_reverses_ownership():
    """(76) Creation time reserves nothing — decision order decides position."""
    c = _c()
    a = _add(c, key="A", title="Add A")
    b = _add(c, key="B", title="Add B")
    rb = _accept(c, b, key="acc-b")          # B first this time
    ra = _accept(c, a, key="acc-a")
    assert rb.json()["added_assignment"]["display_order"] == 1
    assert ra.json()["added_assignment"]["display_order"] == 2
    assert sorted(_day(c).values()) == [0, 1, 2]


def _concurrent(fns):
    results, errors = [], []

    def run(fn):
        try:
            results.append(fn())
        except Exception as exc:      # noqa: BLE001 — recorded then asserted on
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(f,)) for f in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def test_concurrent_different_proposals_allocate_unique_orders():
    """(77)(78)(79)"""
    c = _c()
    pids = [_add(c, key=f"p{i}", title=f"Add {i}") for i in range(4)]
    results, errors = _concurrent(
        [(lambda p=p, i=i: _accept(c, p, key=f"acc{i}")) for i, p in enumerate(pids)])
    assert not errors
    assert all(r.status_code == 200 for r in results)
    orders = _day(c)
    assert orders["assign_maya_bubbles"] == 0                            # (79)
    assert sorted(orders.values()) == [0, 1, 2, 3, 4]                    # (77)(78)
    assert len(set(orders.values())) == len(orders), "duplicate display_order"


def test_concurrent_same_proposal_same_key_yields_one_assignment():
    """(80)"""
    c = _c()
    pid = _add(c)
    before = len(_repo(c).query(C.PLAN_ASSIGNMENTS))
    results, errors = _concurrent([(lambda: _accept(c, pid, key="race")) for _ in range(8)])
    assert not errors
    assert all(r.status_code == 200 for r in results)
    assert len({r.json()["proposal"]["resulting_assignment_id"] for r in results}) == 1
    assert len(_repo(c).query(C.PLAN_ASSIGNMENTS)) == before + 1
    # Count ACCEPTANCE records only — Add creation writes one of its own.
    accepts = [r for r in _repo(c).query(C.IDEMPOTENCY_RECORDS)
               if r["action"] == "accept_plan_change_proposal"]
    assert len(accepts) == 1
    assert len([a for a in _repo(c).query(C.AUDIT_EVENTS)
                if a["event_type"] == "add_activity_proposal_accepted"]) == 1


def test_concurrent_same_proposal_different_keys_yields_one_assignment():
    """(81)"""
    c = _c()
    pid = _add(c)
    before = len(_repo(c).query(C.PLAN_ASSIGNMENTS))
    results, errors = _concurrent(
        [(lambda k=k: _accept(c, pid, key=k)) for k in ("k1", "k2", "k3", "k4")])
    assert not errors
    ok = [r for r in results if r.status_code == 200]
    conflicts = [r for r in results if r.status_code == 409]
    assert len(ok) == 1, "only one key may transition the proposal"
    assert len(conflicts) == 3
    assert len(_repo(c).query(C.PLAN_ASSIGNMENTS)) == before + 1
    assert sorted(_day(c).values()) == [0, 1]


# ── 82-89: rollback ─────────────────────────────────────────────────────────
def test_accept_failure_rolls_everything_back(monkeypatch):
    """(82)(83)(84)(85)(86)"""
    c = _c()
    _sibling(c, "assign_sib", order=1)
    pid = _add(c)
    before = _snapshot(c)

    real = A._child_summary       # called AFTER assignment, proposal and audit writes

    def boom(tx, child_id):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(A, "_child_summary", boom)
    try:
        raised = False
        try:
            _accept(c, pid, key="rb")
        except RuntimeError:
            raised = True
        assert raised
    finally:
        monkeypatch.setattr(A, "_child_summary", real)

    assert _snapshot(c) == before
    assert _repo(c).query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]["status"] == \
        "pending_parent_acceptance"
    assert _day(c) == {"assign_maya_bubbles": 0, "assign_sib": 1}


def test_decline_failure_rolls_everything_back(monkeypatch):
    """(87)(88)(89)"""
    c = _c()
    pid = _add(c)
    before = _snapshot(c)
    real = A._child_summary

    def boom(tx, child_id):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(A, "_child_summary", boom)
    try:
        raised = False
        try:
            _decline(c, pid, key="rb-dec")
        except RuntimeError:
            raised = True
        assert raised
    finally:
        monkeypatch.setattr(A, "_child_summary", real)

    assert _snapshot(c) == before
    assert _repo(c).query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]["status"] == \
        "pending_parent_acceptance"


# ── 90-97: authorization ────────────────────────────────────────────────────
def test_add_decision_authorization_is_existence_blind():
    """(90)(91)(92)(93)(97)"""
    c = _c()
    pid = _add(c)
    blind = {"error": "not_found", "detail": "Not found."}

    r = _accept(c, pid, key="o", headers=OMAR)                           # (90)
    assert r.status_code == 404 and r.json() == blind
    r = _accept(c, pid, key="u", child="child_ghost")                    # (91)
    assert r.status_code == 404 and r.json() == blind
    r = _accept(c, "prop_ghost", key="p")                                # (92)
    assert r.status_code == 404 and r.json() == blind
    r = c.post(f"/api/v1/children/child_eli/proposals/{pid}/accept",      # (93)
               headers={**OMAR, "Idempotency-Key": "m"},
               json={"expected_proposal_version": 1})
    assert r.status_code == 404 and r.json() == blind
    r = c.post(f"/api/v1/children/{CHILD}/proposals/{pid}/accept",        # (97)
               headers={"Idempotency-Key": "n"}, json={"expected_proposal_version": 1})
    assert r.status_code == 401
    # A therapist is not a parent decision-maker.
    assert _accept(c, pid, key="t", headers=HANNAH).status_code == 403


def test_non_active_connections_fail_closed():
    """(94)(95)(96) — Amara pending, Sana paused, Rue ended."""
    c = _c()
    pid = _add(c)
    for child in ("child_amara", "child_sana", "child_rue"):
        r = c.post(f"/api/v1/children/{child}/proposals/{pid}/accept",
                   headers={**ELENA, "Idempotency-Key": f"c-{child}"},
                   json={"expected_proposal_version": 1})
        assert r.status_code == 404, child
        assert r.json() == {"error": "not_found", "detail": "Not found."}


def test_proposal_version_mismatch_conflicts():
    """(21 §) Optimistic concurrency, with no side effects."""
    c = _c()
    pid = _add(c)
    before = _snapshot(c)
    r = _accept(c, pid, key="v", version=99)
    assert r.status_code == 409 and r.json()["error"] == "proposal_version_conflict"
    assert _snapshot(c) == before


def test_missing_idempotency_key_is_rejected():
    c = _c()
    pid = _add(c)
    r = c.post(f"/api/v1/children/{CHILD}/proposals/{pid}/accept",
               headers=ELENA, json={"expected_proposal_version": 1})
    assert r.status_code == 400 and r.json()["error"] == "missing_idempotency_key"


# ── 98-102: request contract ────────────────────────────────────────────────
def test_add_decisions_work_without_expected_assignment_version():
    """(98)(99)(102)"""
    c = _c()
    assert _accept(c, _add(c, key="x1"), key="a1").status_code == 200    # (98)
    c2 = _c()
    assert _decline(c2, _add(c2, key="x2"), key="d1").status_code == 200  # (99)
    # (102) nothing fabricated an assignment version for the Add
    repo = _repo(c)
    added = [a for a in repo.query(C.PLAN_ASSIGNMENTS, child_id=CHILD)
             if a.get("source_proposal_id")]
    assert added and all(a["version"] == 1 for a in added)


def test_add_decision_tolerates_an_explicit_null_assignment_version():
    """A client sending the field as null must behave identically."""
    c = _c()
    pid = _add(c)
    r = _accept(c, pid, key="null",
                body={"expected_proposal_version": 1,
                      "expected_assignment_version": None})
    assert r.status_code == 200


def test_modify_still_requires_expected_assignment_version():
    """(100)(101) The frozen Modify optimistic-concurrency contract is intact."""
    from tests.test_modify_proposal import body as modify_body
    from tests.test_parent_acceptance import MAYA_MODIFY_PATH, _talking_activity

    for verb in ("accept", "decline"):
        c = _c()
        m = c.post(MAYA_MODIFY_PATH, headers={**HANNAH, "Idempotency-Key": "m"},
                   json={**modify_body(), "activity": _talking_activity(),
                         "expected_assignment_version": 1})
        assert m.status_code == 200
        pid = m.json()["proposal"]["proposal_id"]
        before = _snapshot(c)
        r = c.post(f"/api/v1/children/{CHILD}/proposals/{pid}/{verb}",
                   headers={**ELENA, "Idempotency-Key": f"no-ver-{verb}"},
                   json={"expected_proposal_version": 1})     # omitted entirely
        assert r.status_code == 422, verb
        assert r.json()["error"] == "invalid_request", verb
        assert _snapshot(c) == before, verb                              # (101)


# ── 103-113: Modify and therapist regression ────────────────────────────────
def test_modify_accept_and_decline_are_unchanged():
    """(103)(104)(105)(106)(107)(108)"""
    from tests.test_modify_proposal import body as modify_body
    from tests.test_parent_acceptance import MAYA_MODIFY_PATH, _talking_activity

    c = _c()
    _sibling(c, "assign_sib", order=1)
    m = c.post(MAYA_MODIFY_PATH, headers={**HANNAH, "Idempotency-Key": "m1"},
               json={**modify_body(), "activity": _talking_activity(),
                     "expected_assignment_version": 1})
    assert m.status_code == 200                                          # (103)
    pid = m.json()["proposal"]["proposal_id"]
    repo = _repo(c)
    assert eligibility.evaluate_parent_decision(                         # (104)
        repo, CHILD, repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]) == eligibility.ELIGIBLE

    assignment = repo.query(C.PLAN_ASSIGNMENTS, id="assign_maya_bubbles")[0]
    r = c.post(f"/api/v1/children/{CHILD}/proposals/{pid}/accept",
               headers={**ELENA, "Idempotency-Key": "ma"},
               json={"expected_proposal_version": 1,
                     "expected_assignment_version": assignment["version"]})
    assert r.status_code == 200                                          # (105)
    out = r.json()
    assert "retired_assignment" in out and "replacement_assignment" in out
    # (108) the replacement INHERITS the original's position — Modify never appends
    assert out["replacement_assignment"]["display_order"] == 0
    assert _day(c)["assign_sib"] == 1
    assert sorted(_day(c).values()) == [0, 1]

    c2 = _c()
    m2 = c2.post(MAYA_MODIFY_PATH, headers={**HANNAH, "Idempotency-Key": "m2"},
                 json={**modify_body(), "activity": _talking_activity(),
                       "expected_assignment_version": 1})
    a2 = _repo(c2).query(C.PLAN_ASSIGNMENTS, id="assign_maya_bubbles")[0]
    d = c2.post(f"/api/v1/children/{CHILD}/proposals/"
                f"{m2.json()['proposal']['proposal_id']}/decline",
                headers={**ELENA, "Idempotency-Key": "md"},
                json={"expected_proposal_version": 1,
                      "expected_assignment_version": a2["version"]})
    assert d.status_code == 200                                          # (106)
    assert "current_assignment" in d.json()


def test_therapist_add_and_modify_views_are_unchanged():
    """(109)(110)(111)(112)(113)"""
    c = _c()
    pid = _add(c)
    listed = c.get(f"/api/v1/children/{CHILD}/proposals", headers=HANNAH).json()
    assert set(listed) == {"items", "total", "next_cursor"}
    row = next(i for i in listed["items"] if i["proposal_id"] == pid)     # (112)
    assert row["proposal_type"] == "add"
    assert row["destination_scheduled_day"] == MONDAY
    assert row["current_assignment_id"] is None
    detail = c.get(f"/api/v1/children/{CHILD}/proposals/{pid}", headers=HANNAH).json()
    assert detail["destination_scheduled_day"] == MONDAY                 # (113)
    assert "existing_day_activities" not in detail
    assert c.get(f"/api/v1/children/{CHILD}/proposals",
                 headers=UNCONNECTED).status_code == 404


# ── 118-119: read-only ──────────────────────────────────────────────────────
def test_get_routes_remain_read_only_after_a_decision():
    """(118)(119)"""
    c = _c()
    pid = _add(c)
    _accept(c, pid)
    before = _snapshot(c)
    for _ in range(5):
        c.get(f"/api/v1/children/{CHILD}/proposals", headers=ELENA)
        c.get(f"/api/v1/children/{CHILD}/proposals/{pid}", headers=ELENA)
        c.get(f"/api/v1/children/{CHILD}/weekly-plan", headers=HANNAH)
    assert _snapshot(c) == before


# ── audit ───────────────────────────────────────────────────────────────────
def _audit_for(c, proposal_id, event_type):
    return [a for a in _repo(c).query(C.AUDIT_EVENTS)
            if a["subject_id"] == proposal_id and a["event_type"] == event_type][0]


def test_accept_audit_describes_an_append_not_a_replacement():
    c = _c()
    _sibling(c, "assign_g7", order=7)
    pid = _add(c)
    r = _accept(c, pid)
    new_id = r.json()["proposal"]["resulting_assignment_id"]
    aud = _audit_for(c, pid, "add_activity_proposal_accepted")

    before, after = aud["before_state"], aud["after_state"]
    assert before["proposal_status"] == "pending_parent_acceptance"
    assert before["proposal_type"] == "add"
    assert before["destination_scheduled_day"] == MONDAY
    assert before["resulting_assignment_id"] is None
    assert after["proposal_status"] == "accepted"
    assert after["resulting_assignment_id"] == new_id
    assert after["resulting_assignment_display_order"] == 8
    assert after["existing_assignments_unchanged"] is True
    # Never described as a replacement.
    assert after["retired_assignment_id"] is None
    assert after["replaced_assignment_id"] is None
    assert "replacement_assignment_id" not in after
    assert aud["assignment_id"] == new_id
    assert "wp_maya:0" not in repr(aud), "internal token must not reach a semantic field"


def test_decline_audit_records_that_nothing_was_created():
    c = _c()
    pid = _add(c)
    _decline(c, pid)
    aud = _audit_for(c, pid, "add_activity_proposal_declined")
    before, after = aud["before_state"], aud["after_state"]
    assert after["proposal_status"] == "declined"
    assert after["plan_assignment_created"] is False
    assert after["resulting_assignment_id"] is None
    assert after["display_order_map_on_day"] == before["display_order_map_on_day"]
    assert aud["assignment_id"] is None
    assert "wp_maya:0" not in repr(aud)


# ── integrity ───────────────────────────────────────────────────────────────
def test_add_decision_module_imports_no_parent_api_or_cloud_sdk():
    """(123)(125)"""
    import ast
    import inspect

    banned = ("firebase", "firestore", "google", "genex_core", "boto3", "azure")
    for node in ast.walk(ast.parse(inspect.getsource(A))):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        for name in names:
            assert not any(b in name.lower() for b in banned), name


def test_openapi_documents_type_aware_decisions_without_new_routes():
    c = _c()
    schema = c.app.openapi()
    assert schema["openapi"] == "3.1.0"
    assert len(schema["paths"]) == 22, "this phase adds no route"
    for verb, add_model, modify_model in (
        ("accept", "AddAcceptResponse", "AcceptProposalResponse"),
        ("decline", "AddDeclineResponse", "DeclineProposalResponse"),
    ):
        body = schema["paths"][f"/api/v1/children/{{child_id}}/proposals/"
                               f"{{proposal_id}}/{verb}"]["post"]["responses"]["200"]
        refs = {m["$ref"].rsplit("/", 1)[-1]
                for m in body["content"]["application/json"]["schema"]["anyOf"]}
        assert refs == {add_model, modify_model}, verb

    sch = schema["components"]["schemas"]
    # expected_assignment_version is transport-optional now.
    for req in ("AcceptProposalRequest", "DeclineProposalRequest"):
        assert "expected_assignment_version" in sch[req]["properties"]
        assert sch[req]["required"] == ["expected_proposal_version"], req
    # Disjoint on required fields, so neither response can validate as the other.
    assert "added_assignment" in sch["AddAcceptResponse"]["required"]
    assert "retired_assignment" in sch["AcceptProposalResponse"]["required"]
    assert "destination_scheduled_day" in sch["AddDeclineResponse"]["required"]
    assert "current_assignment" in sch["DeclineProposalResponse"]["required"]
