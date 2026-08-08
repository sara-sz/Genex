"""Parent-safe ADD proposal list + detail reads (Phase 1B.2E, fictional data).

The product question an Add raises is not "what changes?" but:

    Monday currently has: Bubble requesting, Turn-taking with a ball.
    Your therapist recommends ADDING: Articulation practice.
    Nothing already there is removed or altered.

These tests pin that meaning, the privacy allow-list, and the fact that ADD is
readable but **not actionable** in this checkpoint.
"""

from __future__ import annotations

import copy

from app.api import schemas as S
from app.domain.enums import AssignmentStatus, PlanApprovalStatus
from app.domain.read_models import PlanAssignment
from app.repository import collections as C
from app.services import eligibility
from tests.conftest import ELENA, HANNAH, OMAR, PRIYA, UNCONNECTED, read_slice_client

MAYA_ADD = "/api/v1/children/child_maya/weekly-plan/proposals/add"
LIST_URL = "/api/v1/children/child_maya/proposals"

MONDAY = 0        # Maya's day 0 already holds assign_maya_bubbles at order 0
THURSDAY = 3      # empty in the fictional plan

#: Never allowed anywhere in a parent ADD list or detail response.
FORBIDDEN = (
    "save_scope", "is_derived", "immutable", "created_by_user_id", "created_by_type",
    "created_by_display_name", "modified_by_user_id", "modified_by_display_name",
    "activity_template_id", "original_activity_template_id",
    "original_activity_version_id", "proposed_activity_version_id",
    "activity_version_id", "target_assignment_id", "current_assignment_id",
    "assignment_id", "weekly_plan_id", "therapist_id", "display_order",
    "assignment_status", "plan_approval_status", "pending_proposal_id",
    "replaced_by_assignment_id", "replaces_assignment_id", "source_proposal_id",
    "practice_status", "idempotency_key_hash", "request_hash", "audit_event_id",
    "operation_target", "destination_target_token", "request_id", "environment",
    "schema_version", "submitted_for_genex_review", "therapist_library",
    "contact_email", "organization",
)


def activity(**over):
    a = {
        "title": "Articulation practice",
        "developmental_domain": "talking_and_communicating",
        "milestone_id": "mile_request_items", "skill_focus": "clear sounds",
        "duration_minutes": 10, "difficulty": "just_right", "materials": ["mirror"],
        "materials_type": "home_items", "setup": "Sit facing a mirror.",
        "parent_instructions": ["Model the sound slowly."],
        "what_to_say": ["watch my mouth"], "how_to_help": ["Wait after modelling."],
        "success_signals": ["Child attempts the sound."], "variations": ["Use a puppet."],
        "routine_tags": ["play"], "theme_tags": ["animals"], "safety_risk_flags": [],
    }
    a.update(over)
    return a


def _c():
    return read_slice_client()


def _repo(c):
    return c.app.state.repo


def _add(c, key="a1", day=MONDAY, plan="wp_maya", reason="Extra articulation work.",
         act=None, child="child_maya"):
    r = c.post(f"/api/v1/children/{child}/weekly-plan/proposals/add",
               headers={**HANNAH, "Idempotency-Key": key},
               json={"scheduled_day": day, "expected_weekly_plan_id": plan,
                     "activity": act or activity(), "change_reason": reason,
                     "save_scope": "child_only"})
    assert r.status_code == 200, r.text
    return r.json()["proposal"]["proposal_id"]


def _list(c, headers=ELENA, child="child_maya"):
    return c.get(f"/api/v1/children/{child}/proposals", headers=headers)


def _detail(c, pid, headers=ELENA, child="child_maya"):
    return c.get(f"/api/v1/children/{child}/proposals/{pid}", headers=headers)


def _item(payload, pid):
    return next(i for i in payload["items"] if i["proposal_id"] == pid)


def _sibling(c, assignment_id="assign_maya_sib", day=MONDAY, order=1,
             version_id="ver_turn_taking_v1", status=AssignmentStatus.CURRENT,
             child="child_maya", plan="wp_maya"):
    """Insert another fictional assignment so a day holds several activities."""
    repo = _repo(c)
    base = repo.query(C.PLAN_ASSIGNMENTS, id="assign_maya_bubbles")[0]
    extra = PlanAssignment(
        id=assignment_id, weekly_plan_id=plan, child_id=child,
        activity_template_id=base["activity_template_id"],
        activity_version_id=version_id, scheduled_day=day, display_order=order,
        plan_approval_status=PlanApprovalStatus.APPROVED, assignment_status=status,
        version=1, environment="dev",
    )
    repo.set(C.PLAN_ASSIGNMENTS, assignment_id, extra.model_dump())
    return extra.model_dump()


# ── 2-12: parent ADD list ───────────────────────────────────────────────────
def test_parent_sees_add_in_the_list_with_the_expected_shape():
    """(2)(3)(4)(5)(6)(7)(8)(9)"""
    c = _c()
    pid = _add(c)
    payload = _list(c).json()
    item = _item(payload, pid)                                           # (2)

    assert item["proposal_type"] == "add"                                # (3)
    assert item["proposal_status"] == "pending_parent_acceptance"
    assert item["destination"] == {"scheduled_day": MONDAY,
                                   "day_label": "Monday"}                # (4)
    assert item["proposed_activity"] == {                                # (5)
        "title": "Articulation practice",
        "developmental_domain": "Talking & Communicating",
        "milestone_display_name": "Requests a desired item",
    }
    assert item["therapist"] == {                                        # (6)
        "display_name": "Hannah Lieberknecht, MA, SLP"}
    assert item["change_reason"] == "Extra articulation work."           # (7)
    # 1B.2F: a current-plan Add is now actionable. The non-actionable cases —
    # historical plan, decided, malformed — are pinned in test_parent_add_decision.
    assert item["decision"] == {"needs_parent_attention": True,           # (8)
                                "can_accept": True, "can_decline": True}
    assert item["child"] == {"child_id": "child_maya", "display_name": "Maya"}
    assert payload["total"] == len(payload["items"])                     # (9)
    assert pid in {i["proposal_id"] for i in payload["items"]}


def test_add_is_counted_in_total():
    """(9) total grows by exactly one per safe Add."""
    c = _c()
    before = _list(c).json()["total"]
    _add(c, key="t1", day=THURSDAY)
    assert _list(c).json()["total"] == before + 1
    _add(c, key="t2", day=4)
    assert _list(c).json()["total"] == before + 2


def test_add_list_item_carries_no_instructions_or_internals():
    """(10)(11)(12)"""
    c = _c()
    pid = _add(c)
    item = _item(_list(c).json(), pid)
    assert set(item.keys()) == {
        "proposal_id", "proposal_type", "proposal_status", "created_at",
        "decided_at", "child", "therapist", "proposed_activity", "change_reason",
        "decision", "destination",
    }
    # (10) a teaser only — the day's activities and full instructions live in detail
    assert set(item["proposed_activity"].keys()) == {
        "title", "developmental_domain", "milestone_display_name"}
    for banned in ("parent_instructions", "what_to_say", "how_to_help",
                   "success_signals", "setup", "materials", "variations",
                   "existing_day_activities"):
        assert banned not in item["proposed_activity"] and banned not in item
    blob = _list(c).text
    for marker in FORBIDDEN:                                             # (11)(12)
        assert marker not in blob, f"parent ADD list leaked {marker!r}"


# ── 13-25: parent ADD detail ────────────────────────────────────────────────
def test_parent_can_read_add_detail_with_the_expected_shape():
    """(13)(14)(15)(19)(20)(21)"""
    c = _c()
    pid = _add(c)
    r = _detail(c, pid)
    assert r.status_code == 200                                          # (13)
    out = r.json()

    assert set(out.keys()) == {
        "proposal", "child", "therapist", "destination",
        "existing_day_activities", "proposed_activity", "change_reason", "decision",
    }
    assert out["proposal"]["proposal_version"] == 1                      # (14)
    assert out["proposal"]["proposal_type"] == "add"
    assert out["proposal"]["decided_at"] is None
    assert out["destination"] == {"scheduled_day": MONDAY,
                                  "day_label": "Monday"}                 # (15)
    # (20)(21) nothing is fabricated for a proposal that replaces nothing
    assert "original_activity" not in out
    assert "decision_context" not in out
    assert "expected_assignment_version" not in r.text
    assert "resulting_assignment_id" not in out["decision"]

    proposed = out["proposed_activity"]                                  # (19)
    assert proposed["title"] == "Articulation practice"
    assert proposed["developmental_domain"] == "Talking & Communicating"
    assert proposed["milestone_display_name"] == "Requests a desired item"
    assert proposed["skill_focus"] == "clear sounds"
    assert proposed["duration_minutes"] == 10
    assert proposed["materials"] == ["mirror"]
    assert proposed["setup"] == "Sit facing a mirror."
    assert proposed["parent_instructions"] == ["Model the sound slowly."]
    assert proposed["what_to_say"] == ["watch my mouth"]
    assert proposed["how_to_help"] == ["Wait after modelling."]
    assert proposed["success_signals"] == ["Child attempts the sound."]
    assert proposed["variations"] == ["Use a puppet."]
    assert proposed["routine_tags"] == ["play"]
    assert proposed["theme_tags"] == ["animals"]
    assert proposed["safety_risk_flags"] == []


def test_existing_day_activities_are_present_and_ordered():
    """(16)(17)(18)(23) — array ORDER carries the position, not a number.

    The ids are deliberately ADVERSARIAL: sorted alphabetically they are
    `assign_maya_aaa` (order 2), `assign_maya_bubbles` (order 0),
    `assign_maya_zzz` (order 1). Sorting by id alone would therefore produce a
    DIFFERENT sequence from sorting by display_order, so this test fails if the
    ordering key ever degrades to the id tie-break. An earlier version of this
    test used ids whose alphabetical order coincided with display_order and
    silently proved nothing.
    """
    c = _c()
    # Inserted out of order so insertion order cannot pass by accident either.
    _sibling(c, "assign_maya_aaa", day=MONDAY, order=2, version_id="ver_bubbles_v1")
    _sibling(c, "assign_maya_zzz", day=MONDAY, order=1, version_id="ver_turn_taking_v1")
    pid = _add(c)
    out = _detail(c, pid).json()

    titles = [a["title"] for a in out["existing_day_activities"]]
    assert len(titles) == 3                                              # (16)(23)
    # display_order 0 bubbles, 1 turn-taking (zzz), 2 bubbles again (aaa)
    assert titles == ["Bubble requesting", "Turn-taking with a ball",
                      "Bubble requesting"]                               # (17)
    # Sorting by id alone would have produced a different sequence.
    assert titles != ["Bubble requesting", "Bubble requesting",
                      "Turn-taking with a ball"]
    for a in out["existing_day_activities"]:                             # (18)
        assert set(a.keys()) == {"title", "developmental_domain",
                                 "milestone_display_name", "duration_minutes"}
        assert "display_order" not in a


def test_day_activity_id_tie_break_is_only_the_final_key():
    """Equal-position rows cannot occur among CURRENT assignments, but the sort
    key must still be deterministic — five reads return the identical sequence."""
    c = _c()
    _sibling(c, "assign_maya_aaa", day=MONDAY, order=3, version_id="ver_bubbles_v1")
    _sibling(c, "assign_maya_mmm", day=MONDAY, order=1, version_id="ver_turn_taking_v1")
    _sibling(c, "assign_maya_zzz", day=MONDAY, order=2, version_id="ver_bubbles_v1")
    pid = _add(c)
    first = [a["title"] for a in _detail(c, pid).json()["existing_day_activities"]]
    assert first == ["Bubble requesting", "Turn-taking with a ball",
                     "Bubble requesting", "Bubble requesting"]
    for _ in range(5):
        assert [a["title"] for a in
                _detail(c, pid).json()["existing_day_activities"]] == first


def test_existing_day_activities_may_be_empty():
    """(22) An empty destination day is valid, not an error."""
    c = _c()
    pid = _add(c, day=THURSDAY)
    out = _detail(c, pid).json()
    assert out["existing_day_activities"] == []
    assert out["destination"] == {"scheduled_day": THURSDAY, "day_label": "Thursday"}


def test_retired_and_replaced_assignments_are_excluded():
    """(24)(51) Only CURRENT activities are context for the family."""
    c = _c()
    _sibling(c, "assign_maya_retired", day=MONDAY, order=1,
             version_id="ver_turn_taking_v1", status=AssignmentStatus.REPLACED)
    pid = _add(c)
    out = _detail(c, pid).json()
    assert [a["title"] for a in out["existing_day_activities"]] == ["Bubble requesting"]
    assert "Turn-taking with a ball" not in _detail(c, pid).text


def test_standalone_proposed_activity_with_null_template_renders():
    """(25) A null activity_template_id must not affect parent rendering."""
    c = _c()
    pid = _add(c)
    version_id = _repo(c).query(C.PLAN_CHANGE_PROPOSALS,
                                id=pid)[0]["proposed_activity_version_id"]
    version = _repo(c).query(C.ACTIVITY_VERSIONS, id=version_id)[0]
    assert version["activity_template_id"] is None, "precondition: standalone version"

    out = _detail(c, pid).json()
    assert out["proposed_activity"]["title"] == "Articulation practice"
    assert "activity_template_id" not in _detail(c, pid).text
    assert version_id not in _detail(c, pid).text


def test_all_weekday_labels_render():
    """day_label must be correct for every valid weekday, not just Monday."""
    expected = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                "Saturday", "Sunday"]
    c = _c()
    for day, label in enumerate(expected):
        pid = _add(c, key=f"d{day}", day=day)
        assert _detail(c, pid).json()["destination"] == {
            "scheduled_day": day, "day_label": label}


# ── 26-30: decision state and sorting ───────────────────────────────────────
def test_add_decision_flags_are_actionable_for_the_current_plan():
    """(26)(27)(28) — 1B.2F: Add accept/decline now exists for a current-plan Add.

    List and detail must agree, and `accepted_or_declined_at` stays null while the
    proposal is still pending.
    """
    c = _c()
    pid = _add(c)
    detail = _detail(c, pid).json()["decision"]
    assert detail["can_accept"] is True                                  # (26)
    assert detail["can_decline"] is True                                 # (27)
    assert detail["needs_parent_attention"] is True                      # (28)
    assert detail["accepted_or_declined_at"] is None
    item = _item(_list(c).json(), pid)["decision"]
    assert item == {"needs_parent_attention": True,
                    "can_accept": True, "can_decline": True}

    repo = _repo(c)
    proposal = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]
    assert eligibility.evaluate_parent_decision(
        repo, "child_maya", proposal) == eligibility.ELIGIBLE


def test_an_actionable_add_sorts_alongside_an_actionable_modify():
    """(29)(30) — 1B.2F: a current-plan Add is actionable, so it joins group 0.

    Was: "an actionable Modify must outrank a readable Add", true only while Add
    was non-actionable. What must hold now is that BOTH are actionable and the
    ordering within the group stays deterministic — no type is privileged.
    """
    c = _c()
    from tests.test_parent_acceptance import MAYA_MODIFY_PATH, _talking_activity
    from tests.test_modify_proposal import body as modify_body

    m = c.post(MAYA_MODIFY_PATH, headers={**HANNAH, "Idempotency-Key": "m1"},
               json={**modify_body(), "activity": _talking_activity(),
                     "expected_assignment_version": 1})
    assert m.status_code == 200
    modify_id = m.json()["proposal"]["proposal_id"]
    add_id = _add(c, key="later", day=THURSDAY)

    items = _list(c).json()["items"]
    order = [i["proposal_id"] for i in items]
    actionable = [i["proposal_id"] for i in items if i["decision"]["needs_parent_attention"]]
    assert modify_id in actionable and add_id in actionable              # (29)
    # Both are in the actionable group, so ordering falls to newest-first: the
    # Add was created second and therefore leads.
    assert order.index(add_id) < order.index(modify_id)                  # (30)
    # Determinism: repeated reads produce the identical ordering.
    assert [i["proposal_id"] for i in _list(c).json()["items"]] == order

    # A DECIDED Add drops out of the actionable group entirely.
    assert c.post(f"{LIST_URL}/{add_id}/decline",
                  headers={**ELENA, "Idempotency-Key": "sort-dec"},
                  json={"expected_proposal_version": 1}).status_code == 200
    after = _list(c).json()["items"]
    assert [i["proposal_id"] for i in after
            if i["decision"]["needs_parent_attention"]] == [modify_id]
    assert after[-1]["proposal_id"] == add_id, "a decided proposal sorts last"


# ── 31-34: current-state context, not a stored snapshot ─────────────────────
def test_detail_reflects_activities_added_after_the_proposal():
    """(31)(32)(33)(34) The proposal is the recommendation; the day is live state."""
    c = _c()
    pid = _add(c)
    before = _detail(c, pid).json()
    assert [a["title"] for a in before["existing_day_activities"]] == ["Bubble requesting"]
    proposal_before = copy.deepcopy(_repo(c).query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0])

    _sibling(c, "assign_maya_new", day=MONDAY, order=1,                  # (31)
             version_id="ver_turn_taking_v1")

    after = _detail(c, pid).json()
    assert [a["title"] for a in after["existing_day_activities"]] == [   # (32)
        "Bubble requesting", "Turn-taking with a ball"]
    # (33)(34) the proposal record itself is untouched by either read
    assert _repo(c).query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0] == proposal_before
    assert after["proposal"] == before["proposal"]


# ── 35-43: authorization ────────────────────────────────────────────────────
def test_another_parent_gets_404_for_list_and_detail():
    """(35)(43)"""
    c = _c()
    pid = _add(c)
    assert _list(c, OMAR).status_code == 404
    r = _detail(c, pid, OMAR)
    assert r.status_code == 404
    assert r.json() == {"error": "not_found", "detail": "Not found."}


def test_unknown_child_and_unknown_proposal_are_existence_blind():
    """(36)(37)(43) Identical bodies — nothing reveals which id exists."""
    c = _c()
    pid = _add(c)
    unknown_child = c.get("/api/v1/children/child_ghost/proposals", headers=ELENA)
    unknown_prop = _detail(c, "prop_ghost")
    real_prop_wrong_child = c.get(
        f"/api/v1/children/child_ghost/proposals/{pid}", headers=ELENA)
    assert unknown_child.status_code == 404
    assert unknown_prop.status_code == 404 and real_prop_wrong_child.status_code == 404
    assert unknown_prop.json() == real_prop_wrong_child.json() == {
        "error": "not_found", "detail": "Not found."}


def test_proposal_child_mismatch_is_404():
    """(38) Eli's parent must not read Maya's Add, and vice versa."""
    c = _c()
    pid = _add(c)
    r = c.get(f"/api/v1/children/child_eli/proposals/{pid}", headers=OMAR)
    assert r.status_code == 404
    assert r.json() == {"error": "not_found", "detail": "Not found."}


def test_pending_paused_and_ended_connections_are_404():
    """(39)(40)(41) — Amara pending, Sana paused, Rue ended."""
    c = _c()
    for child in ("child_amara", "child_sana", "child_rue"):
        r = c.get(f"/api/v1/children/{child}/proposals", headers=ELENA)
        assert r.status_code == 404, child
        assert r.json() == {"error": "not_found", "detail": "Not found."}


def test_unauthenticated_fails_closed():
    """(42)"""
    c = _c()
    pid = _add(c)
    assert c.get(LIST_URL).status_code == 401
    assert c.get(f"{LIST_URL}/{pid}").status_code == 401


def test_therapist_principals_do_not_receive_the_parent_projection():
    """A therapist keeps the therapist view; parent schemas never replace it."""
    c = _c()
    pid = _add(c)
    out = _detail(c, pid, HANNAH).json()
    assert "destination_scheduled_day" in out          # therapist ProposalView
    assert "existing_day_activities" not in out
    assert "destination" not in out
    assert _detail(c, pid, UNCONNECTED).status_code == 404
    assert _detail(c, pid, PRIYA).status_code == 404


# ── 44-52: privacy ──────────────────────────────────────────────────────────
def test_no_forbidden_field_appears_in_add_list_or_detail():
    """(44)(45)(46)(47)(48)(49)(50)(51)"""
    c = _c()
    _sibling(c, "assign_maya_sib", day=MONDAY, order=1, version_id="ver_turn_taking_v1")
    _sibling(c, "assign_maya_gone", day=MONDAY, order=2,
             version_id="ver_bubbles_v1", status=AssignmentStatus.REPLACED)
    pid = _add(c)
    for blob in (_list(c).text, _detail(c, pid).text):
        for marker in FORBIDDEN:
            assert marker not in blob, f"parent ADD response leaked {marker!r}"
        # (49) private therapist notes and (50) unrelated families
        assert "private" not in blob.lower() or "parent_instructions" in blob
        for other in ("child_eli", "child_noah", "child_theo", "par_omar",
                      "ther_priya", "ther_hannah"):
            assert other not in blob


def test_parent_add_schema_key_sets_are_pinned():
    """(52) The response model IS the privacy boundary — pin it at source."""
    assert set(S.ParentAddProposalDecisionDetail.model_fields) == {
        "proposal", "child", "therapist", "destination", "existing_day_activities",
        "proposed_activity", "change_reason", "decision"}
    assert set(S.ParentDestinationDay.model_fields) == {"scheduled_day", "day_label"}
    assert set(S.ParentDayActivitySummary.model_fields) == {
        "title", "developmental_domain", "milestone_display_name", "duration_minutes"}
    assert set(S.ParentAddDecisionFlags.model_fields) == {
        "can_accept", "can_decline", "needs_parent_attention", "accepted_or_declined_at"}
    for model in (S.ParentAddProposalDecisionDetail, S.ParentDestinationDay,
                  S.ParentDayActivitySummary, S.ParentAddDecisionFlags):
        for marker in FORBIDDEN:
            assert marker not in model.model_fields, f"{model.__name__} declares {marker}"


def test_add_and_modify_parent_details_are_structurally_disjoint():
    """The union response model must not be able to reshape one into the other."""
    add_fields = set(S.ParentAddProposalDecisionDetail.model_fields)
    modify_fields = set(S.ParentProposalDecisionDetail.model_fields)
    assert "original_activity" in modify_fields and "original_activity" not in add_fields
    assert "decision_context" in modify_fields and "decision_context" not in add_fields
    assert "destination" in add_fields and "destination" not in modify_fields
    assert ("existing_day_activities" in add_fields
            and "existing_day_activities" not in modify_fields)
    # Required-field disjointness is what actually prevents cross-validation.
    required = lambda m: {n for n, f in m.model_fields.items() if f.is_required()}
    assert required(S.ParentAddProposalDecisionDetail) - modify_fields
    assert required(S.ParentProposalDecisionDetail) - add_fields


# ── 53-58: malformed ADD proposals ──────────────────────────────────────────
def _corrupt(c, pid, **changes):
    repo = _repo(c)
    p = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]
    p.update(changes)
    repo.set(C.PLAN_CHANGE_PROPOSALS, pid, p)
    return p


def test_missing_destination_day_excludes_the_item():
    """(53)(57)(58)"""
    c = _c()
    pid = _add(c)
    _corrupt(c, pid, destination_scheduled_day=None)
    payload = _list(c).json()
    assert pid not in {i["proposal_id"] for i in payload["items"]}
    assert payload["total"] == len(payload["items"])
    r = _detail(c, pid)
    assert r.status_code == 404
    assert r.json() == {"error": "not_found", "detail": "Not found."}


def test_out_of_range_destination_day_excludes_the_item():
    """(53)(58) 7 is not a weekday."""
    c = _c()
    pid = _add(c)
    _corrupt(c, pid, destination_scheduled_day=7)
    assert pid not in {i["proposal_id"] for i in _list(c).json()["items"]}
    assert _detail(c, pid).status_code == 404


def test_missing_proposed_version_excludes_the_item():
    """(54)(57)(58)"""
    c = _c()
    pid = _add(c)
    _corrupt(c, pid, proposed_activity_version_id="ver_ghost")
    assert _list(c).status_code == 200
    assert pid not in {i["proposal_id"] for i in _list(c).json()["items"]}
    assert _detail(c, pid).status_code == 404


def test_broken_plan_linkage_excludes_the_item():
    """(55)(57)(58) An unresolvable weekly plan cannot give day context."""
    c = _c()
    pid = _add(c)
    _corrupt(c, pid, weekly_plan_id="wp_ghost")
    assert pid not in {i["proposal_id"] for i in _list(c).json()["items"]}
    assert _detail(c, pid).status_code == 404


def test_plan_belonging_to_another_child_excludes_the_item():
    """(55) A proposal must not borrow another family's plan for context."""
    c = _c()
    pid = _add(c)
    _corrupt(c, pid, weekly_plan_id="wp_noah")
    assert pid not in {i["proposal_id"] for i in _list(c).json()["items"]}
    assert _detail(c, pid).status_code == 404
    assert "child_noah" not in _list(c).text


def test_duplicate_current_display_order_excludes_the_item():
    """(56)(57)(58) An ambiguously ordered day cannot be presented in order."""
    c = _c()
    pid = _add(c)
    assert pid in {i["proposal_id"] for i in _list(c).json()["items"]}
    _sibling(c, "assign_maya_dupe", day=MONDAY, order=0,       # collides with bubbles
             version_id="ver_turn_taking_v1")
    payload = _list(c).json()
    assert payload["total"] == len(payload["items"])
    assert pid not in {i["proposal_id"] for i in payload["items"]}
    r = _detail(c, pid)
    assert r.status_code == 404
    assert r.json() == {"error": "not_found", "detail": "Not found."}


def test_unsupported_proposal_types_remain_fail_closed():
    """Visibility is type-aware, not open by default.

    The base proposal is a fully-formed, otherwise-ELIGIBLE **MODIFY** whose type
    is then flipped. That matters: flipping the type on an ADD-shaped record would
    prove nothing, because the Modify branch would reject it anyway for having no
    `target_assignment_id`, and the test would pass with the type guard deleted.
    Starting from a Modify makes the guard the only thing standing between an
    unsupported type and a parent-visible, actionable item.
    """
    c = _c()
    mid = _modify(c)
    repo = _repo(c)
    baseline = repo.query(C.PLAN_CHANGE_PROPOSALS, id=mid)[0]
    assert eligibility.evaluate_parent_decision(
        repo, "child_maya", baseline) == eligibility.ELIGIBLE, "precondition"
    assert mid in {i["proposal_id"] for i in _list(c).json()["items"]}

    for unsupported in ("replace", "remove", "something_new"):
        _corrupt(c, mid, proposal_type=unsupported)
        assert mid not in {i["proposal_id"] for i in _list(c).json()["items"]}, unsupported
        assert _detail(c, mid).status_code == 404, unsupported
        proposal = repo.query(C.PLAN_CHANGE_PROPOSALS, id=mid)[0]
        assert eligibility.evaluate_parent_decision(
            repo, "child_maya", proposal) == eligibility.INELIGIBLE, unsupported
        assert eligibility.proposal_is_safe_to_show(
            repo, "child_maya", proposal) is False, unsupported


def test_no_malformed_add_produces_a_500():
    """(58) Every corruption yields 200-with-exclusion or a canonical 404."""
    corruptions = [
        {"destination_scheduled_day": None}, {"destination_scheduled_day": -1},
        {"destination_scheduled_day": "tuesday"}, {"destination_scheduled_day": True},
        {"proposed_activity_version_id": None}, {"weekly_plan_id": None},
        {"status": "cancelled"}, {"change_reason": None},
    ]
    for changes in corruptions:
        c = _c()
        pid = _add(c)
        _corrupt(c, pid, **changes)
        listed = _list(c)
        assert listed.status_code == 200, changes
        assert listed.json()["total"] == len(listed.json()["items"]), changes
        assert _detail(c, pid).status_code in (200, 404), changes


# ── 59-65: list / detail consistency ────────────────────────────────────────
def test_list_and_detail_agree_on_every_shared_field():
    """(59)(60)(61)(62)(63)(64)(65)"""
    c = _c()
    _sibling(c, "assign_maya_sib", day=MONDAY, order=1, version_id="ver_turn_taking_v1")
    pid = _add(c)
    item = _item(_list(c).json(), pid)
    detail = _detail(c, pid).json()

    assert item["proposal_id"] == detail["proposal"]["proposal_id"] == pid   # (59)
    assert item["proposal_type"] == detail["proposal"]["proposal_type"]      # (60)
    assert item["proposal_status"] == detail["proposal"]["proposal_status"]  # (61)
    assert item["created_at"] == detail["proposal"]["created_at"]
    assert item["decided_at"] == detail["proposal"]["decided_at"]
    assert item["therapist"] == detail["therapist"]                          # (62)
    assert item["child"] == detail["child"]
    assert item["proposed_activity"]["title"] == detail["proposed_activity"]["title"]  # (63)
    assert item["destination"] == detail["destination"]                      # (64)
    assert item["decision"]["can_accept"] == detail["decision"]["can_accept"]  # (65)
    assert item["decision"]["can_decline"] == detail["decision"]["can_decline"]
    assert (item["decision"]["needs_parent_attention"]
            == detail["decision"]["needs_parent_attention"])
    assert item["change_reason"] == detail["change_reason"]


# ── 66-73: MODIFY and therapist regression ──────────────────────────────────
def _modify(c, key="m1"):
    from tests.test_modify_proposal import body as modify_body
    from tests.test_parent_acceptance import MAYA_MODIFY_PATH, _talking_activity

    r = c.post(MAYA_MODIFY_PATH, headers={**HANNAH, "Idempotency-Key": key},
               json={**modify_body(), "activity": _talking_activity(),
                     "expected_assignment_version": 1})
    assert r.status_code == 200
    return r.json()["proposal"]["proposal_id"]


def test_modify_parent_list_item_and_detail_are_unchanged():
    """(66)(67) The frozen Modify contract, with destination null."""
    c = _c()
    mid = _modify(c)
    item = _item(_list(c).json(), mid)
    assert item["proposal_type"] == "modify"
    assert item["destination"] is None
    detail = _detail(c, mid).json()
    assert set(detail.keys()) == {
        "proposal", "child", "therapist", "decision_context",
        "original_activity", "proposed_activity", "decision"}
    assert detail["decision_context"]["expected_assignment_version"] == 2
    assert detail["decision"]["can_accept"] is True
    assert "destination" not in detail and "existing_day_activities" not in detail


def test_modify_eligibility_and_decisions_remain_unchanged():
    """(68)(69)(70)"""
    c = _c()
    mid = _modify(c)
    repo = _repo(c)
    proposal = repo.query(C.PLAN_CHANGE_PROPOSALS, id=mid)[0]
    assert eligibility.evaluate_parent_decision(
        repo, "child_maya", proposal) == eligibility.ELIGIBLE             # (70)

    assignment = repo.query(C.PLAN_ASSIGNMENTS, id="assign_maya_bubbles")[0]
    r = c.post(f"/api/v1/children/child_maya/proposals/{mid}/accept",
               headers={**ELENA, "Idempotency-Key": "acc"},
               json={"expected_proposal_version": 1,
                     "expected_assignment_version": assignment["version"]})
    assert r.status_code == 200                                           # (68)

    c2 = _c()
    mid2 = _modify(c2)
    a2 = _repo(c2).query(C.PLAN_ASSIGNMENTS, id="assign_maya_bubbles")[0]
    d = c2.post(f"/api/v1/children/child_maya/proposals/{mid2}/decline",
                headers={**ELENA, "Idempotency-Key": "dec"},
                json={"expected_proposal_version": 1,
                      "expected_assignment_version": a2["version"]})
    assert d.status_code == 200                                           # (69)


def test_therapist_add_and_modify_views_are_unchanged():
    """(71)(72)(73)"""
    c = _c()
    pid = _add(c)
    mid = _modify(c)

    listed = c.get(LIST_URL, headers=HANNAH).json()
    assert set(listed.keys()) == {"items", "total", "next_cursor"}         # therapist Page
    add_row = next(i for i in listed["items"] if i["proposal_id"] == pid)
    assert add_row["proposal_type"] == "add"                               # (71)
    assert add_row["destination_scheduled_day"] == MONDAY
    assert add_row["current_assignment_id"] is None
    assert add_row["original_activity_version_id"] is None

    mod_row = next(i for i in listed["items"] if i["proposal_id"] == mid)   # (72)
    assert mod_row["proposal_type"] == "modify"
    assert mod_row["destination_scheduled_day"] is None
    assert mod_row["current_assignment_id"] == "assign_maya_bubbles"

    assert _detail(c, pid, UNCONNECTED).status_code == 404                 # (73)
    assert c.get(LIST_URL, headers=UNCONNECTED).status_code == 404


# ── 74-81: read-only guarantee ──────────────────────────────────────────────
WATCHED = (C.PLAN_ASSIGNMENTS, C.PLAN_CHANGE_PROPOSALS, C.ACTIVITY_VERSIONS,
           C.ACTIVITY_TEMPLATES, C.AUDIT_EVENTS, C.IDEMPOTENCY_RECORDS,
           C.CONNECTIONS, C.CHILDREN)


def _snapshot(c):
    repo = _repo(c)
    return {name: copy.deepcopy(sorted(repo.query(name), key=lambda r: r["id"]))
            for name in WATCHED}


def test_parent_add_reads_mutate_nothing():
    """(74)(75)(76)(77)(78)(79)(80)(81)"""
    c = _c()
    _sibling(c, "assign_maya_sib", day=MONDAY, order=1, version_id="ver_turn_taking_v1")
    pid = _add(c)
    before = _snapshot(c)

    first_list = _list(c).json()
    first_detail = _detail(c, pid).json()
    for _ in range(5):
        assert _list(c).json() == first_list                              # (81)
        assert _detail(c, pid).json() == first_detail

    after = _snapshot(c)
    for name in WATCHED:                                                  # (74)-(80)
        assert after[name] == before[name], f"{name} changed during a read"
    assert len(after[C.AUDIT_EVENTS]) == len(before[C.AUDIT_EVENTS])       # (74)(75)
    assert len(after[C.IDEMPOTENCY_RECORDS]) == len(                       # (76)
        before[C.IDEMPOTENCY_RECORDS])


def test_reads_do_not_change_display_order_or_proposal_version():
    """(78)(79) Explicit, because both are the fields a read might 'helpfully' fix."""
    c = _c()
    _sibling(c, "assign_maya_sib", day=MONDAY, order=1, version_id="ver_turn_taking_v1")
    pid = _add(c)
    repo = _repo(c)
    orders_before = {a["id"]: a["display_order"]
                     for a in repo.query(C.PLAN_ASSIGNMENTS, child_id="child_maya")}
    version_before = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]["version"]

    _list(c), _detail(c, pid), _list(c), _detail(c, pid)

    assert {a["id"]: a["display_order"]
            for a in repo.query(C.PLAN_ASSIGNMENTS, child_id="child_maya")} == orders_before
    assert repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]["version"] == version_before


# ── OpenAPI / isolation ─────────────────────────────────────────────────────
def test_openapi_documents_both_parent_detail_shapes_without_new_routes():
    c = _c()
    schema = c.app.openapi()
    assert schema["openapi"] == "3.1.0"
    assert len(schema["paths"]) == 22, "this phase adds no route"

    detail = schema["paths"]["/api/v1/children/{child_id}/proposals/{proposal_id}"]
    body = detail["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    refs = {m["$ref"].rsplit("/", 1)[-1] for m in body["anyOf"]}
    assert refs == {"ProposalView", "ParentProposalDecisionDetail",
                    "ParentAddProposalDecisionDetail"}

    schemas = schema["components"]["schemas"]
    assert "destination" in schemas["ParentProposalListItem"]["properties"]
    add_detail = schemas["ParentAddProposalDecisionDetail"]["properties"]
    assert "existing_day_activities" in add_detail
    assert "original_activity" not in add_detail
    assert "display_order" not in schemas["ParentDayActivitySummary"]["properties"]


def test_read_path_imports_no_parent_api_or_cloud_sdk():
    """(85)(87)"""
    import ast
    import inspect

    from app.domain import weekdays
    from app.services import read_service

    banned = ("firebase", "firestore", "google", "genex_core", "boto3", "azure")
    for module in (read_service, eligibility, weekdays):
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for name in names:
                assert not any(b in name.lower() for b in banned), (module, name)
