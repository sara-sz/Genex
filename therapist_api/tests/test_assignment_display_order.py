"""Ordered multiple activities within one weekday (Phase 1B.2C.1).

Genex generates an activity for most days, so a therapist recommendation must be
able to sit ALONGSIDE an existing activity rather than displace it. This phase
introduces `display_order` and replaces the old "at most one current assignment
per weekday" rule with "display_order is unique among the day's CURRENT
assignments".

No Add-proposal behavior is introduced here. Multi-activity days are built by
tests directly so the canonical fictional fixtures stay single-activity.
"""

from __future__ import annotations

import copy

from app.domain.read_models import PlanAssignment
from app.repository import collections as C
from app.services import eligibility
from tests.conftest import ELENA, HANNAH, read_slice_client
from tests.test_modify_proposal import body
from tests.test_parent_acceptance import MAYA_BUBBLES, MAYA_MODIFY_PATH, _talking_activity

MAYA_PLAN = "wp_maya"
BUBBLES_DAY = 0          # assign_maya_bubbles sits on day 0, display_order 0


def _c():
    return read_slice_client()


def _repo(c):
    return c.app.state.repo


def _add_sibling(client, *, assignment_id, day, order, plan=MAYA_PLAN,
                 child="child_maya", template="tmpl_bubbles", version="ver_bubbles_v1"):
    """Insert an extra CURRENT assignment directly (no Add proposal exists yet)."""
    repo = _repo(client)
    a = PlanAssignment(
        id=assignment_id, weekly_plan_id=plan, child_id=child,
        activity_template_id=template, activity_version_id=version,
        scheduled_day=day, display_order=order,
        plan_approval_status="approved", practice_status="not_tried",
        assignment_status="current", version=1,
    )
    repo.set(C.PLAN_ASSIGNMENTS, assignment_id, a.model_dump())
    return a


def _plan_items(client, headers=HANNAH, child="child_maya"):
    r = client.get(f"/api/v1/children/{child}/weekly-plan", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["assignments"]


def _propose(client, key="prop-1", version=1):
    r = client.post(MAYA_MODIFY_PATH, headers={**HANNAH, "Idempotency-Key": key},
                    json={**body(), "activity": _talking_activity(),
                          "expected_assignment_version": version})
    assert r.status_code == 200, r.text
    return r.json()


def _accept(client, pid, key, proposal_version=1, assignment_version=2):
    return client.post(f"/api/v1/children/child_maya/proposals/{pid}/accept",
                       headers={**ELENA, "Idempotency-Key": key},
                       json={"expected_proposal_version": proposal_version,
                             "expected_assignment_version": assignment_version})


def _decline(client, pid, key, proposal_version=1, assignment_version=2):
    return client.post(f"/api/v1/children/child_maya/proposals/{pid}/decline",
                       headers={**ELENA, "Idempotency-Key": key},
                       json={"expected_proposal_version": proposal_version,
                             "expected_assignment_version": assignment_version})


def _current_on_day(repo, day, child="child_maya", plan=MAYA_PLAN):
    return [a for a in repo.query(C.PLAN_ASSIGNMENTS, child_id=child)
            if a["weekly_plan_id"] == plan and a["scheduled_day"] == day
            and a["assignment_status"] == "current"]


# ── 2-8: domain and schema ──────────────────────────────────────────────────
def test_display_order_defaults_to_zero():
    a = PlanAssignment(
        id="a1", weekly_plan_id=MAYA_PLAN, child_id="child_maya",
        activity_template_id="tmpl_bubbles", activity_version_id="ver_bubbles_v1",
        scheduled_day=0, plan_approval_status="approved",
    )
    assert a.display_order == 0


def test_negative_display_order_is_rejected():
    import pydantic
    import pytest
    with pytest.raises(pydantic.ValidationError):
        PlanAssignment(
            id="a2", weekly_plan_id=MAYA_PLAN, child_id="child_maya",
            activity_template_id="tmpl_bubbles", activity_version_id="ver_bubbles_v1",
            scheduled_day=0, display_order=-1, plan_approval_status="approved",
        )


def test_fixture_assignments_are_explicitly_order_zero():
    repo = _repo(_c())
    for a in repo.query(C.PLAN_ASSIGNMENTS):
        assert a["display_order"] == 0, a["id"]


def test_authorized_views_expose_display_order_without_losing_fields():
    c = _c()
    item = _plan_items(c)[0]
    assert item["display_order"] == 0
    for existing in ("assignment_id", "scheduled_day", "plan_approval_status",
                     "practice_status", "assignment_status", "activity_template_id",
                     "activity_version_id", "activity_title", "provenance",
                     "parent_feedback_summary", "pending_proposal_id", "version"):
        assert existing in item, existing


def test_openapi_documents_display_order():
    # Build from explicit settings (not process env) so this does not depend on
    # how the suite happens to be invoked.
    schemas = _c().app.openapi()["components"]["schemas"]
    assert "display_order" in schemas["PlanAssignmentView"]["properties"]
    assert "display_order" in schemas["ApprovedAssignment"]["properties"]


def test_parent_projections_gain_no_assignment_internals():
    """(8) parent-safe schemas must not acquire display_order or other internals."""
    from app.api import schemas as S
    for model in (S.ParentActivityView, S.ParentProposalSummary, S.ParentChildSummary,
                  S.ParentTherapistSummary, S.ParentDecisionContext,
                  S.ParentDecisionFlags, S.ParentProposalDecisionDetail,
                  S.ParentProposalListItem, S.ParentProposalActivitySummary,
                  S.ParentProposalDecisionSummary):
        for internal in ("display_order", "scheduled_day", "assignment_status",
                         "weekly_plan_id", "activity_version_id"):
            assert internal not in model.model_fields, f"{model.__name__}.{internal}"


# ── 9-15: sorting ───────────────────────────────────────────────────────────
def test_multiple_current_activities_may_share_a_weekday_and_sort_by_order():
    c = _c()
    _add_sibling(c, assignment_id="assign_zzz_first", day=BUBBLES_DAY, order=1)
    _add_sibling(c, assignment_id="assign_aaa_second", day=BUBBLES_DAY, order=2)

    day0 = [a for a in _plan_items(c) if a["scheduled_day"] == BUBBLES_DAY]
    assert len(day0) == 3                                          # (9)
    assert [a["display_order"] for a in day0] == [0, 1, 2]         # (10)(11)
    assert [a["assignment_id"] for a in day0] == [
        MAYA_BUBBLES, "assign_zzz_first", "assign_aaa_second"]
    # ...and id order alone would NOT produce this, proving order drives it (15)
    assert sorted(a["assignment_id"] for a in day0) != [a["assignment_id"] for a in day0]


def test_assignment_id_is_only_the_final_tie_break():
    """(12) equal day+order falls back to id — deterministic, never arbitrary."""
    c = _c()
    _add_sibling(c, assignment_id="assign_bbb", day=3, order=0)
    _add_sibling(c, assignment_id="assign_aaa", day=3, order=0)
    day3 = [a for a in _plan_items(c) if a["scheduled_day"] == 3]
    assert [a["assignment_id"] for a in day3] == ["assign_aaa", "assign_bbb"]


def test_weekday_still_dominates_ordering():
    """(13) a low display_order on a later day never outranks an earlier day."""
    c = _c()
    _add_sibling(c, assignment_id="assign_day6", day=6, order=0)
    _add_sibling(c, assignment_id="assign_day0_late", day=BUBBLES_DAY, order=9)
    days = [a["scheduled_day"] for a in _plan_items(c)]
    assert days == sorted(days)
    ids = [a["assignment_id"] for a in _plan_items(c)]
    assert ids.index("assign_day0_late") < ids.index("assign_day6")


def test_repeated_reads_return_identical_ordering():
    c = _c()
    _add_sibling(c, assignment_id="assign_zzz", day=BUBBLES_DAY, order=1)
    _add_sibling(c, assignment_id="assign_aaa", day=BUBBLES_DAY, order=2)
    orders = [[a["assignment_id"] for a in _plan_items(c)] for _ in range(5)]
    assert all(o == orders[0] for o in orders), orders


# ── 16-21: uniqueness invariant scope ───────────────────────────────────────
def test_distinct_orders_on_one_day_are_valid():
    c = _c()
    _add_sibling(c, assignment_id="assign_sib", day=BUBBLES_DAY, order=1)
    pid = _propose(c)["proposal"]["proposal_id"]
    assert _accept(c, pid, "u-1").status_code == 200          # (16)


def test_duplicate_order_among_current_is_rejected():
    c = _c()
    _add_sibling(c, assignment_id="assign_dupe", day=BUBBLES_DAY, order=0)
    pid = _propose(c)["proposal"]["proposal_id"]
    r = _accept(c, pid, "u-2")
    assert r.status_code == 409                                # (17)
    assert r.json()["error"] == "duplicate_assignment_display_order"


def test_retired_assignment_may_reuse_an_order():
    """(18) only CURRENT rows participate in the uniqueness invariant."""
    c = _c()
    retired = _add_sibling(c, assignment_id="assign_retired", day=BUBBLES_DAY, order=0)
    repo = _repo(c)
    row = repo.query(C.PLAN_ASSIGNMENTS, id=retired.id)[0]
    row["assignment_status"] = "replaced"
    repo.set(C.PLAN_ASSIGNMENTS, retired.id, row)

    pid = _propose(c)["proposal"]["proposal_id"]
    assert _accept(c, pid, "u-3").status_code == 200


def test_invariant_is_scoped_by_child_plan_and_day():
    """(19)(20)(21) same order elsewhere is not a conflict."""
    c = _c()
    _add_sibling(c, assignment_id="assign_other_child", day=BUBBLES_DAY, order=0,
                 child="child_eli", plan="wp_eli")             # (19) other child
    _add_sibling(c, assignment_id="assign_other_plan", day=BUBBLES_DAY, order=0,
                 plan="wp_other")                              # (20) other plan
    _add_sibling(c, assignment_id="assign_other_day", day=4, order=0)   # (21) other day

    pid = _propose(c)["proposal"]["proposal_id"]
    assert _accept(c, pid, "u-4").status_code == 200


# ── 22-34: Modify acceptance with several activities on the day ─────────────
def test_acceptance_preserves_position_and_leaves_siblings_untouched():
    c = _c()
    sibling = _add_sibling(c, assignment_id="assign_sibling", day=BUBBLES_DAY, order=1)
    third = _add_sibling(c, assignment_id="assign_third", day=BUBBLES_DAY, order=2)
    repo = _repo(c)
    before = _current_on_day(repo, BUBBLES_DAY)
    assert len(before) == 3                                       # (22)

    pid = _propose(c)["proposal"]["proposal_id"]                  # (23)
    r = _accept(c, pid, "m-1")
    assert r.status_code == 200, r.text                           # (24)

    new_id = r.json()["replacement_assignment"]["assignment_id"]
    assert r.json()["replacement_assignment"]["display_order"] == 0   # (25) inherited

    after = _current_on_day(repo, BUBBLES_DAY)
    assert len(after) == len(before)                              # (30) count unchanged
    by_id = {a["id"]: a for a in after}
    assert set(by_id) == {new_id, sibling.id, third.id}
    # (26)(27)(32) siblings still current, same order, untouched version
    for original in (sibling, third):
        kept = by_id[original.id]
        assert kept["assignment_status"] == "current"
        assert kept["display_order"] == original.display_order
        assert kept["version"] == original.version
    # (28)(29) existing lineage behavior intact
    old = repo.query(C.PLAN_ASSIGNMENTS, id=MAYA_BUBBLES)[0]
    assert old["assignment_status"] == "replaced"
    assert by_id[new_id]["assignment_status"] == "current"
    # (31) orders remain unique
    orders = [a["display_order"] for a in after]
    assert len(orders) == len(set(orders)) == 3


def test_acceptance_inherits_a_NON_ZERO_position():
    """The decisive inheritance case: modify an activity that is NOT first.

    Every other test modifies the day's first activity (order 0), where
    "inherit the original's order" and "always use 0" are indistinguishable.
    Here the target sits at order 2, so the replacement must land at 2 — and the
    activities above and below it must not shift.
    """
    c = _c()
    repo = _repo(c)
    # Move the fixture activity to position 2 and put neighbours at 0 and 1.
    target = repo.query(C.PLAN_ASSIGNMENTS, id=MAYA_BUBBLES)[0]
    target["display_order"] = 2
    repo.set(C.PLAN_ASSIGNMENTS, MAYA_BUBBLES, target)
    first = _add_sibling(c, assignment_id="assign_first", day=BUBBLES_DAY, order=0)
    second = _add_sibling(c, assignment_id="assign_second", day=BUBBLES_DAY, order=1)

    pid = _propose(c)["proposal"]["proposal_id"]
    r = _accept(c, pid, "pos-1")
    assert r.status_code == 200, r.text

    replacement = r.json()["replacement_assignment"]
    assert replacement["display_order"] == 2, "replacement must inherit position 2, not 0"

    # neighbours unmoved, and the day still reads in the intended order
    day = [a for a in _plan_items(c) if a["scheduled_day"] == BUBBLES_DAY]
    assert [a["display_order"] for a in day] == [0, 1, 2]
    assert [a["assignment_id"] for a in day] == [
        first.id, second.id, replacement["assignment_id"]]


def test_acceptance_audit_count_is_unchanged_on_a_multi_activity_day():
    """(34) `current_assignment_count_in_slot` now legitimately exceeds one."""
    c = _c()
    _add_sibling(c, assignment_id="assign_sibling", day=BUBBLES_DAY, order=1)
    pid = _propose(c)["proposal"]["proposal_id"]
    _accept(c, pid, "m-2")
    aud = [e for e in _repo(c).query(C.AUDIT_EVENTS)
           if e["event_type"] == "plan_change_proposal_accepted"][0]
    assert aud["after_state"]["current_assignment_count_in_slot"] == 2


def test_acceptance_idempotency_unaffected_by_siblings():
    """(33) replay still returns the same replacement and creates no second one."""
    c = _c()
    _add_sibling(c, assignment_id="assign_sibling", day=BUBBLES_DAY, order=1)
    pid = _propose(c)["proposal"]["proposal_id"]
    first = _accept(c, pid, "m-3")
    second = _accept(c, pid, "m-3")
    assert second.json()["idempotent_replay"] is True
    assert (second.json()["replacement_assignment"]["assignment_id"]
            == first.json()["replacement_assignment"]["assignment_id"])
    assert len(_current_on_day(_repo(c), BUBBLES_DAY)) == 2


# ── 35-43: Modify decline with several activities on the day ────────────────
def test_decline_leaves_the_whole_day_untouched():
    c = _c()
    sibling = _add_sibling(c, assignment_id="assign_sibling", day=BUBBLES_DAY, order=1)
    third = _add_sibling(c, assignment_id="assign_third", day=BUBBLES_DAY, order=2)
    repo = _repo(c)
    pid = _propose(c)["proposal"]["proposal_id"]                  # (35)(36)
    before = {a["id"]: a["display_order"] for a in _current_on_day(repo, BUBBLES_DAY)}

    r = _decline(c, pid, "d-1")
    assert r.status_code == 200, r.text

    after_rows = _current_on_day(repo, BUBBLES_DAY)
    after = {a["id"]: a["display_order"] for a in after_rows}
    assert after == before                                        # (38)(39)(40)
    original = repo.query(C.PLAN_ASSIGNMENTS, id=MAYA_BUBBLES)[0]
    assert original["assignment_status"] == "current"             # (37)
    assert original["display_order"] == 0
    # (41) no replacement anywhere
    assert not [a for a in repo.query(C.PLAN_ASSIGNMENTS, child_id="child_maya")
                if a.get("replaces_assignment_id") or a.get("source_proposal_id")]
    for kept in (sibling, third):
        row = repo.query(C.PLAN_ASSIGNMENTS, id=kept.id)[0]
        assert row["version"] == kept.version


def test_decline_idempotency_and_audit_unaffected_by_siblings():
    """(42)(43)"""
    c = _c()
    _add_sibling(c, assignment_id="assign_sibling", day=BUBBLES_DAY, order=1)
    pid = _propose(c)["proposal"]["proposal_id"]
    first = _decline(c, pid, "d-2")
    second = _decline(c, pid, "d-2")
    assert second.json()["idempotent_replay"] is True
    assert second.json()["audit_event_id"] == first.json()["audit_event_id"]
    aud = [e for e in _repo(c).query(C.AUDIT_EVENTS)
           if e["event_type"] == "plan_change_proposal_declined"][0]
    assert aud["before_state"]["current_assignment_count_in_slot"] == 2
    assert aud["after_state"]["current_assignment_count_in_slot"] == 2


# ── 44-48: eligibility ──────────────────────────────────────────────────────
def test_sibling_activity_does_not_make_a_proposal_ineligible():
    """(44)"""
    c = _c()
    _add_sibling(c, assignment_id="assign_sibling", day=BUBBLES_DAY, order=1)
    pid = _propose(c)["proposal"]["proposal_id"]
    detail = c.get(f"/api/v1/children/child_maya/proposals/{pid}", headers=ELENA).json()
    assert detail["decision"]["can_accept"] is True
    assert detail["decision"]["can_decline"] is True


def test_duplicate_order_fails_eligibility_closed():
    """(45)"""
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    _add_sibling(c, assignment_id="assign_dupe", day=BUBBLES_DAY, order=0)
    detail = c.get(f"/api/v1/children/child_maya/proposals/{pid}", headers=ELENA).json()
    assert detail["decision"]["can_accept"] is False
    assert detail["decision"]["can_decline"] is False


def test_target_absent_from_current_set_fails_eligibility():
    """(46)"""
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    repo = _repo(c)
    row = repo.query(C.PLAN_ASSIGNMENTS, id=MAYA_BUBBLES)[0]
    row["assignment_status"] = "replaced"
    repo.set(C.PLAN_ASSIGNMENTS, MAYA_BUBBLES, row)
    detail = c.get(f"/api/v1/children/child_maya/proposals/{pid}", headers=ELENA).json()
    assert detail["decision"]["can_accept"] is False


def test_add_proposal_remains_ineligible_and_invisible():
    """(48) Add behavior is not implemented; it must not surface to parents."""
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    repo = _repo(c)
    p = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]
    p["proposal_type"] = "add"
    repo.set(C.PLAN_CHANGE_PROPOSALS, pid, p)

    flags = eligibility.evaluate_parent_decision(repo, "child_maya", p)
    assert flags == eligibility.INELIGIBLE
    listed = c.get("/api/v1/children/child_maya/proposals", headers=ELENA).json()
    item = next(i for i in listed["items"] if i["proposal_id"] == pid)
    assert item["decision"] == {"needs_parent_attention": False,
                                "can_accept": False, "can_decline": False}


# ── 59-62: read-only and atomicity ──────────────────────────────────────────
def test_reads_and_eligibility_do_not_mutate_display_order():
    """(59)(60)"""
    c = _c()
    _add_sibling(c, assignment_id="assign_sibling", day=BUBBLES_DAY, order=1)
    repo = _repo(c)
    before = copy.deepcopy(repo._data[C.PLAN_ASSIGNMENTS])
    for _ in range(3):
        _plan_items(c)
        for p in repo.query(C.PLAN_CHANGE_PROPOSALS, child_id="child_maya"):
            eligibility.evaluate_parent_decision(repo, "child_maya", p)
    assert repo._data[C.PLAN_ASSIGNMENTS] == before


def test_duplicate_order_failure_leaves_collections_unchanged():
    """(61)"""
    c = _c()
    _add_sibling(c, assignment_id="assign_dupe", day=BUBBLES_DAY, order=0)
    pid = _propose(c)["proposal"]["proposal_id"]
    repo = _repo(c)
    snapshot = {col: copy.deepcopy(repo._data.get(col, {})) for col in (
        C.PLAN_ASSIGNMENTS, C.PLAN_CHANGE_PROPOSALS, C.AUDIT_EVENTS,
        C.IDEMPOTENCY_RECORDS, C.ACTIVITY_VERSIONS)}

    assert _accept(c, pid, "atomic-1").status_code == 409
    for col, snap in snapshot.items():
        assert repo._data.get(col, {}) == snap, col
