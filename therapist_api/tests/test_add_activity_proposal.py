"""Therapist ADD-activity proposal creation (Phase 1B.2D, fictional dev data).

The product rule under test: **Add means ADD ANOTHER activity to the weekday.**
The destination day need not be empty, several current activities may already sit
there, several pending Adds may name the same day, nothing existing is touched,
and no position is reserved until a parent accepts.
"""

from __future__ import annotations

import threading

from app.domain.enums import AssignmentStatus, PlanApprovalStatus
from app.domain.read_models import PlanAssignment
from app.repository import collections as C
from app.services import add_proposal_service as A
from app.services import assignment_order, eligibility
from tests.conftest import ELENA, HANNAH, OMAR, PRIYA, UNCONNECTED, read_slice_client

ADD_NOAH = "/api/v1/children/child_noah/weekly-plan/proposals/add"
ADD_MAYA = "/api/v1/children/child_maya/weekly-plan/proposals/add"

TUESDAY = 1          # a weekday Noah's fictional plan leaves free
NOAH_DAY = 5         # assign_noah_turntake already sits here at display_order 0


def act_social(**over):
    a = {
        "title": "Extra turn-taking", "developmental_domain": "social_and_emotional",
        "milestone_id": "mile_turn_taking", "skill_focus": "waiting a turn",
        "duration_minutes": 8, "difficulty": "just_right", "materials": ["soft ball"],
        "materials_type": "home_items", "setup": "Sit facing each other.",
        "parent_instructions": ["Name each turn."], "what_to_say": ["my turn"],
        "how_to_help": ["Pause and wait."], "success_signals": ["Child waits."],
        "variations": ["Use a car."], "routine_tags": ["play"], "theme_tags": [],
        "safety_risk_flags": [],
    }
    a.update(over)
    return a


def act_talking(**over):
    return act_social(
        title="Bedtime language", developmental_domain="talking_and_communicating",
        milestone_id="mile_request_items", **over,
    )


def body(day=TUESDAY, plan="wp_noah", activity=None, **over):
    b = {"scheduled_day": day, "expected_weekly_plan_id": plan,
         "activity": activity or act_social(),
         "change_reason": "Additional communication practice for this week.",
         "save_scope": "child_only"}
    b.update(over)
    return b


def _c():
    return read_slice_client()


def _repo(c):
    return c.app.state.repo


def _post(c, path=ADD_NOAH, headers=HANNAH, key="k1", b=None):
    h = dict(headers)
    if key is not None:
        h["Idempotency-Key"] = key
    return c.post(path, headers=h, json=b if b is not None else body())


def _assignments(c, child="child_noah"):
    return _repo(c).query(C.PLAN_ASSIGNMENTS, child_id=child)


def _snapshot(c, child="child_noah"):
    """Full, comparable copy of every assignment record for one child."""
    return {a["id"]: dict(a) for a in _assignments(c, child)}


def _add_assignment(c, assignment_id, day, order, child="child_noah", plan="wp_noah",
                    status=AssignmentStatus.CURRENT):
    """Insert an extra fictional assignment so a day holds several activities."""
    repo = _repo(c)
    base = repo.query(C.PLAN_ASSIGNMENTS, id="assign_noah_turntake")[0]
    extra = PlanAssignment(
        id=assignment_id, weekly_plan_id=plan, child_id=child,
        activity_template_id=base["activity_template_id"],
        activity_version_id=base["activity_version_id"],
        scheduled_day=day, display_order=order,
        plan_approval_status=PlanApprovalStatus.APPROVED,
        assignment_status=status, version=1, environment="dev",
    )
    repo.set(C.PLAN_ASSIGNMENTS, assignment_id, extra.model_dump())
    return extra.model_dump()


# ── 2-18: successful creation ───────────────────────────────────────────────
def test_authorized_therapist_creates_an_add_proposal():
    """(2)(3)(4)(5)(6)(7)(8)(9)(10)(11)"""
    c = _c()
    r = _post(c)
    assert r.status_code == 200
    p = r.json()["proposal"]
    assert p["proposal_type"] == "add"                                   # (3)
    assert p["proposal_status"] == "pending_parent_acceptance"           # (4)
    assert p["weekly_plan_id"] == "wp_noah"                              # (5)
    assert p["destination_scheduled_day"] == TUESDAY                     # (6)
    assert p["resulting_assignment_id"] is None                          # (11)
    assert p["proposed_activity_version_id"]                             # (10)
    assert p["version"] == 1

    stored = _repo(c).query(C.PLAN_CHANGE_PROPOSALS, id=p["proposal_id"])[0]
    assert stored["target_assignment_id"] is None                        # (7)
    assert stored["original_activity_template_id"] is None               # (8)
    assert stored["original_activity_version_id"] is None                # (9)
    assert stored["destination_scheduled_day"] == TUESDAY


def test_creates_exactly_one_of_each_record_and_zero_assignments():
    """(12)(13)(14)(15)(16)"""
    c = _c()
    repo = _repo(c)
    versions_before = len(repo.query(C.ACTIVITY_VERSIONS))
    proposals_before = len(repo.query(C.PLAN_CHANGE_PROPOSALS))
    audits_before = len(repo.query(C.AUDIT_EVENTS))
    records_before = len(repo.query(C.IDEMPOTENCY_RECORDS))
    assignments_before = len(repo.query(C.PLAN_ASSIGNMENTS))

    assert _post(c).status_code == 200

    assert len(repo.query(C.ACTIVITY_VERSIONS)) == versions_before + 1        # (12)
    assert len(repo.query(C.PLAN_CHANGE_PROPOSALS)) == proposals_before + 1   # (13)
    assert len(repo.query(C.AUDIT_EVENTS)) == audits_before + 1               # (14)
    assert len(repo.query(C.IDEMPOTENCY_RECORDS)) == records_before + 1       # (15)
    assert len(repo.query(C.PLAN_ASSIGNMENTS)) == assignments_before          # (16)


def test_existing_assignments_are_byte_identical_and_no_order_reserved():
    """(17)(18)"""
    c = _c()
    before = _snapshot(c)
    assert _post(c).status_code == 200
    assert _snapshot(c) == before                                        # (17)

    prop = _repo(c).query(C.PLAN_CHANGE_PROPOSALS, child_id="child_noah")[0]
    # (18) no position anywhere on the proposal, under any spelling.
    for banned in ("display_order", "reserved_display_order", "position",
                   "slot_id", "activity_index", "placeholder_assignment_id"):
        assert banned not in prop


def test_proposed_version_is_immutable_therapist_authored_and_not_derived():
    c = _c()
    v = _post(c).json()["proposed_activity_version"]
    assert v["immutable"] is True
    assert v["created_by_type"] == "therapist"
    assert v["created_by_user_id"] == "ther_hannah"
    # An Add is new work, not a version of an existing plan item.
    assert v["is_derived"] is False
    assert v["activity_template_id"] is None
    assert v["original_activity_template_id"] is None
    assert v["original_activity_version_id"] is None
    assert v["version_number"] == 1


# ── 19-23: occupied and multi-activity destination days ─────────────────────
def test_add_succeeds_on_a_day_that_already_has_one_activity():
    """(19) The corrected product model: the day need NOT be empty."""
    c = _c()
    before = _snapshot(c)
    r = _post(c, b=body(day=NOAH_DAY))
    assert r.status_code == 200
    assert r.json()["proposal"]["destination_scheduled_day"] == NOAH_DAY
    assert _snapshot(c) == before                                        # (21)


def test_add_succeeds_on_a_day_with_several_current_activities():
    """(20)(21)(22)"""
    c = _c()
    _add_assignment(c, "assign_noah_extra1", day=NOAH_DAY, order=1)
    _add_assignment(c, "assign_noah_extra2", day=NOAH_DAY, order=2)
    before = _snapshot(c)
    assert len(assignment_order.current_assignments_for_day(
        _repo(c), "child_noah", "wp_noah", NOAH_DAY)) == 3

    assert _post(c, b=body(day=NOAH_DAY)).status_code == 200
    after = _snapshot(c)
    assert after == before                                               # (21)
    assert {a["id"]: a["display_order"] for a in after.values()
            if a["scheduled_day"] == NOAH_DAY} == {
        "assign_noah_turntake": 0, "assign_noah_extra1": 1, "assign_noah_extra2": 2}  # (22)


def test_duplicate_current_display_order_fails_creation_closed():
    """(23) Fail closed rather than create a proposal into an ambiguous day."""
    c = _c()
    _add_assignment(c, "assign_noah_dupe", day=NOAH_DAY, order=0)   # collides
    before = _snapshot(c)
    repo = _repo(c)
    proposals_before = len(repo.query(C.PLAN_CHANGE_PROPOSALS))
    versions_before = len(repo.query(C.ACTIVITY_VERSIONS))

    r = _post(c, b=body(day=NOAH_DAY))
    assert r.status_code == 409
    assert r.json()["error"] == "duplicate_assignment_display_order"
    # Nothing repaired, renumbered or created.
    assert _snapshot(c) == before
    assert len(repo.query(C.PLAN_CHANGE_PROPOSALS)) == proposals_before
    assert len(repo.query(C.ACTIVITY_VERSIONS)) == versions_before


def test_retired_assignment_does_not_block_creation():
    """A retired row may reuse an order — it never participates in the invariant."""
    c = _c()
    _add_assignment(c, "assign_noah_retired", day=NOAH_DAY, order=0,
                    status=AssignmentStatus.REPLACED)
    assert _post(c, b=body(day=NOAH_DAY)).status_code == 200


def test_add_succeeds_on_a_completely_empty_day():
    """Zero current activities is still a valid destination."""
    c = _c()
    assert assignment_order.current_assignments_for_day(
        _repo(c), "child_noah", "wp_noah", TUESDAY) == []
    assert _post(c, b=body(day=TUESDAY)).status_code == 200


# ── 24-29: multiple pending Add proposals on one weekday ────────────────────
def test_two_different_pending_adds_may_target_the_same_weekday():
    """(24)(25)(26)(27)(28) The explicit founder requirement.

    Sharing a weekday is NOT a conflict: an occupied day plus two independent
    recommendations is the normal case, so neither may be rejected and neither
    may be positioned yet.
    """
    c = _c()
    repo = _repo(c)
    assignments_before = len(repo.query(C.PLAN_ASSIGNMENTS))

    a = _post(c, key="add-a", b=body(day=TUESDAY, activity=act_social()))
    b = _post(c, key="add-b", b=body(day=TUESDAY, activity=act_talking()))
    assert a.status_code == 200 and b.status_code == 200                 # (24)(25)

    pid_a = a.json()["proposal"]["proposal_id"]
    pid_b = b.json()["proposal"]["proposal_id"]
    assert pid_a != pid_b
    pending = [
        p for p in repo.query(C.PLAN_CHANGE_PROPOSALS, child_id="child_noah")
        if p["status"] == "pending_parent_acceptance"
        and p["destination_scheduled_day"] == TUESDAY
    ]
    assert {p["id"] for p in pending} == {pid_a, pid_b}                  # (26)
    for p in pending:
        assert "display_order" not in p                                  # (27)
        assert p["target_assignment_id"] is None
        assert p["resulting_assignment_id"] is None
    assert len(repo.query(C.PLAN_ASSIGNMENTS)) == assignments_before     # (28)


def test_two_pending_adds_on_an_occupied_day_and_parent_sees_neither():
    """(29) Tuesday-style scenario: an existing Genex activity plus two Adds."""
    c = _c()
    # Maya's day 0 already holds assign_maya_bubbles at display_order 0.
    r1 = _post(c, ADD_MAYA, key="m-a", b=body(day=0, plan="wp_maya", activity=act_social()))
    r2 = _post(c, ADD_MAYA, key="m-b", b=body(day=0, plan="wp_maya", activity=act_talking()))
    assert r1.status_code == 200 and r2.status_code == 200
    day0 = assignment_order.current_assignments_for_day(
        _repo(c), "child_maya", "wp_maya", 0)
    assert assignment_order.assignment_order_map(day0) == {"assign_maya_bubbles": 0}

    listed = c.get("/api/v1/children/child_maya/proposals", headers=ELENA).json()
    ids = {i["proposal_id"] for i in listed["items"]}
    assert r1.json()["proposal"]["proposal_id"] not in ids               # (29)
    assert r2.json()["proposal"]["proposal_id"] not in ids


# ── 30-41: validation and authorization ─────────────────────────────────────
def test_scheduled_day_below_range_is_rejected():
    """(30)"""
    r = _post(_c(), b=body(day=-1))
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"


def test_scheduled_day_above_range_is_rejected():
    """(31)"""
    r = _post(_c(), b=body(day=7))
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"


def test_every_valid_weekday_is_accepted():
    """0..6 inclusive — the boundaries are valid, not merely the middle."""
    for day in range(7):
        c = _c()
        assert _post(c, b=body(day=day)).status_code == 200, day


def test_wrong_expected_weekly_plan_id_conflicts():
    """(32)"""
    c = _c()
    before = _snapshot(c)
    r = _post(c, b=body(plan="wp_maya"))          # Maya's plan, Noah's child
    assert r.status_code == 409
    assert r.json()["error"] == "weekly_plan_conflict"
    assert _snapshot(c) == before


def test_milestone_domain_mismatch_is_rejected():
    """(33)"""
    c = _c()
    bad = act_social(developmental_domain="talking_and_communicating")  # milestone is social
    r = _post(c, b=body(activity=bad))
    assert r.status_code == 422 and r.json()["error"] == "milestone_domain_mismatch"


def test_unknown_milestone_and_unknown_domain_are_rejected():
    c = _c()
    assert _post(c, b=body(activity=act_social(milestone_id="mile_nope"))
                 ).json()["error"] == "milestone_domain_mismatch"
    assert _post(c, key="k2", b=body(activity=act_social(developmental_domain="nope"))
                 ).json()["error"] == "milestone_domain_mismatch"


def test_invalid_save_scope_is_rejected():
    """(34)"""
    r = _post(_c(), b=body(save_scope="everyone"))
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"


def test_missing_idempotency_key_is_rejected():
    r = _post(_c(), key=None)
    assert r.status_code == 400 and r.json()["error"] == "missing_idempotency_key"


def test_parent_cannot_create_an_add_proposal():
    """(35) A therapist-only write: 403, not a silent success."""
    c = _c()
    before = _snapshot(c, "child_maya")
    r = _post(c, ADD_MAYA, headers=ELENA, b=body(day=3, plan="wp_maya"))
    assert r.status_code == 403 and r.json()["error"] == "forbidden"
    assert _snapshot(c, "child_maya") == before


def test_unconnected_therapist_is_existence_blind():
    """(36)"""
    c = _c()
    r = _post(c, headers=UNCONNECTED)
    assert r.status_code == 404
    assert r.json() == {"error": "not_found", "detail": "Not found."}


def test_therapist_with_another_caseload_is_existence_blind():
    c = _c()
    r = _post(c, headers=PRIYA)
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_pending_paused_and_ended_connections_all_fail_existence_blind():
    """(37)(38)(39) — Amara pending, Sana paused, Rue ended."""
    c = _c()
    for child in ("child_amara", "child_sana", "child_rue"):
        r = _post(c, f"/api/v1/children/{child}/weekly-plan/proposals/add",
                  key=f"k-{child}", b=body(plan="wp_x"))
        assert r.status_code == 404, child
        assert r.json() == {"error": "not_found", "detail": "Not found."}


def test_unknown_child_is_existence_blind():
    """(40) Indistinguishable from an unauthorized one."""
    c = _c()
    r = _post(c, "/api/v1/children/child_ghost/weekly-plan/proposals/add")
    assert r.status_code == 404
    assert r.json() == {"error": "not_found", "detail": "Not found."}


def test_unauthenticated_fails_closed():
    """(41)"""
    c = _c()
    r = c.post(ADD_NOAH, headers={"Idempotency-Key": "k1"}, json=body())
    assert r.status_code == 401


def test_another_parent_cannot_create_for_another_family():
    c = _c()
    r = _post(c, ADD_MAYA, headers=OMAR, b=body(day=3, plan="wp_maya"))
    assert r.status_code in (403, 404)


# ── 42-53: idempotency ──────────────────────────────────────────────────────
def test_same_key_same_request_replays_and_creates_nothing_new():
    """(42)(43)(44)(45)(46)(47)"""
    c = _c()
    repo = _repo(c)
    first = _post(c, key="dup")
    assert first.status_code == 200 and first.json()["idempotent_replay"] is False
    counts = {name: len(repo.query(name)) for name in
              (C.ACTIVITY_VERSIONS, C.PLAN_CHANGE_PROPOSALS, C.AUDIT_EVENTS,
               C.IDEMPOTENCY_RECORDS, C.PLAN_ASSIGNMENTS)}

    second = _post(c, key="dup")
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True                    # (43)
    assert second.json()["proposal"] == first.json()["proposal"]
    assert second.json()["audit_event_id"] == first.json()["audit_event_id"]
    for name, n in counts.items():                                       # (44)-(47)
        assert len(repo.query(name)) == n, name


def test_same_key_with_a_changed_field_conflicts():
    """(48)(49)(50)(51)(52) — every bound component detects a change."""
    variants = {
        "day": body(day=TUESDAY + 1),                                    # (48)
        "plan": body(plan="wp_maya"),                                    # (49)
        "activity": body(activity=act_talking()),                        # (50)
        "reason": body(change_reason="Something else entirely."),        # (51)
        "save_scope": body(save_scope="therapist_library"),              # (52)
    }
    for label, changed in variants.items():
        c = _c()
        assert _post(c, key="same").status_code == 200, label
        r = _post(c, key="same", b=changed)
        assert r.status_code == 409, label
        assert r.json()["error"] == "idempotency_key_conflict", label


def test_different_keys_create_two_proposals_on_the_same_day():
    """(53) The decisive id-derivation case.

    Both requests are byte-identical apart from the Idempotency-Key. Seeding
    document ids on the request hash alone — as Modify safely does, because its
    expected_assignment_version increments — would derive the SAME proposal id
    here and silently overwrite the first proposal. Add has no version in its
    request, so its ids must bind the key hash.
    """
    c = _c()
    repo = _repo(c)
    identical = body(day=TUESDAY)
    first = _post(c, key="key-one", b=identical)
    second = _post(c, key="key-two", b=identical)
    assert first.status_code == 200 and second.status_code == 200

    pid1 = first.json()["proposal"]["proposal_id"]
    pid2 = second.json()["proposal"]["proposal_id"]
    assert pid1 != pid2, "same body + different key must not collide on one proposal"
    assert (first.json()["proposal"]["proposed_activity_version_id"]
            != second.json()["proposal"]["proposed_activity_version_id"])
    assert first.json()["audit_event_id"] != second.json()["audit_event_id"]
    assert len([p for p in repo.query(C.PLAN_CHANGE_PROPOSALS, child_id="child_noah")
                if p["destination_scheduled_day"] == TUESDAY]) == 2


def test_idempotency_record_never_stores_the_raw_key():
    c = _c()
    _post(c, key="super-secret-key")
    for rec in _repo(c).query(C.IDEMPOTENCY_RECORDS):
        assert "super-secret-key" not in repr(rec)
        assert rec["idempotency_key_hash"] != "super-secret-key"
    for aud in _repo(c).query(C.AUDIT_EVENTS):
        assert "super-secret-key" not in repr(aud)


# ── 54-61: concurrency and rollback ─────────────────────────────────────────
def _concurrent(fns):
    results, errors = [], []

    def run(fn):
        try:
            results.append(fn())
        except Exception as exc:      # noqa: BLE001 — recorded, then asserted on
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(fn,)) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def test_same_key_concurrent_requests_yield_one_logical_creation():
    """(54)(56)"""
    c = _c()
    repo = _repo(c)
    assignments_before = len(repo.query(C.PLAN_ASSIGNMENTS))
    results, errors = _concurrent([lambda: _post(c, key="race") for _ in range(8)])
    assert not errors
    assert all(r.status_code == 200 for r in results)
    ids = {r.json()["proposal"]["proposal_id"] for r in results}
    assert len(ids) == 1                                                 # (54)
    assert len(repo.query(C.PLAN_CHANGE_PROPOSALS, child_id="child_noah")) == 1
    assert len(repo.query(C.IDEMPOTENCY_RECORDS)) == 1
    assert len(repo.query(C.PLAN_ASSIGNMENTS)) == assignments_before     # (56)


def test_different_key_same_day_concurrent_requests_may_both_succeed():
    """(55)(56)(57) Sharing a weekday must never be treated as a race to lose."""
    c = _c()
    repo = _repo(c)
    _add_assignment(c, "assign_noah_sib", day=NOAH_DAY, order=1)
    before = _snapshot(c)
    assignments_before = len(repo.query(C.PLAN_ASSIGNMENTS))

    results, errors = _concurrent([
        (lambda k=k: _post(c, key=k, b=body(day=NOAH_DAY, activity=act_social(title=k))))
        for k in ("c-1", "c-2", "c-3", "c-4")
    ])
    assert not errors
    assert all(r.status_code == 200 for r in results)                    # (55)
    assert len({r.json()["proposal"]["proposal_id"] for r in results}) == 4
    assert len(repo.query(C.PLAN_ASSIGNMENTS)) == assignments_before     # (56)
    assert _snapshot(c) == before                                        # (57)
    day = assignment_order.current_assignments_for_day(
        repo, "child_noah", "wp_noah", NOAH_DAY)
    assert not assignment_order.has_duplicate_display_order(day)         # (57)


def test_transaction_failure_rolls_back_every_record(monkeypatch):
    """(58)(59)(60)(61) A late failure must leave no partial proposal."""
    c = _c()
    repo = _repo(c)
    before = {name: len(repo.query(name)) for name in
              (C.ACTIVITY_VERSIONS, C.PLAN_CHANGE_PROPOSALS, C.AUDIT_EVENTS,
               C.IDEMPOTENCY_RECORDS, C.PLAN_ASSIGNMENTS)}
    snapshot = _snapshot(c)

    # Fail AFTER the version, proposal and audit event have been written, so the
    # rollback is genuinely exercised rather than the operation aborting early.
    real = A._plan_review_count

    def boom(tx, child_id):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(A, "_plan_review_count", boom)
    try:
        raised = False
        try:
            _post(c, key="rollback")
        except RuntimeError:
            raised = True
        assert raised
    finally:
        monkeypatch.setattr(A, "_plan_review_count", real)

    for name, n in before.items():
        assert len(repo.query(name)) == n, name       # (58)(59)(60)(61)
    assert _snapshot(c) == snapshot


def test_duplicate_order_failure_leaves_no_partial_records():
    """A rejected creation writes nothing at all, including no idempotency record."""
    c = _c()
    _add_assignment(c, "assign_noah_dupe", day=NOAH_DAY, order=0)
    repo = _repo(c)
    before = {name: len(repo.query(name)) for name in
              (C.ACTIVITY_VERSIONS, C.PLAN_CHANGE_PROPOSALS, C.AUDIT_EVENTS,
               C.IDEMPOTENCY_RECORDS)}
    assert _post(c, key="dupe", b=body(day=NOAH_DAY)).status_code == 409
    for name, n in before.items():
        assert len(repo.query(name)) == n, name


# ── 62-66: audit and privacy ────────────────────────────────────────────────
def _audit_for(c, proposal_id):
    return [a for a in _repo(c).query(C.AUDIT_EVENTS) if a["subject_id"] == proposal_id][0]


def test_audit_states_the_truth_about_what_was_created():
    """(62)(63)"""
    c = _c()
    _add_assignment(c, "assign_noah_extra1", day=NOAH_DAY, order=1)
    r = _post(c, b=body(day=NOAH_DAY))
    aud = _audit_for(c, r.json()["proposal"]["proposal_id"])

    assert aud["event_type"] == "add_activity_proposal_created"
    assert aud["subject_type"] == "plan_change_proposal"
    before, after = aud["before_state"], aud["after_state"]
    assert before["plan_assignment_created"] is False                    # (62)
    assert after["plan_assignment_created"] is False                     # (62)
    assert after["display_order_reserved"] is False                      # (63)
    # The day is described identically on both sides — nothing was replaced.
    assert before["display_order_map_on_day"] == after["display_order_map_on_day"]
    assert before["current_assignment_count_on_day"] == 2
    assert after["current_assignment_count_on_day"] == 2
    assert after["proposal_type"] == "add"
    assert after["proposal_status"] == "pending_parent_acceptance"
    assert after["destination_scheduled_day"] == NOAH_DAY
    assert after["proposed_activity_version_id"]
    assert "resulting_assignment_id" not in after


def test_synthetic_destination_token_never_reaches_a_semantic_field_or_the_api():
    """(64)(65)"""
    c = _c()
    r = _post(c, b=body(day=TUESDAY))
    token = A.destination_target_token("wp_noah", TUESDAY)
    assert token == "wp_noah:1"

    aud = _audit_for(c, r.json()["proposal"]["proposal_id"])
    assert aud["assignment_id"] is None                                  # (64)
    assert token not in repr(aud)
    prop = _repo(c).query(C.PLAN_CHANGE_PROPOSALS, id=r.json()["proposal"]["proposal_id"])[0]
    assert token not in repr(prop)
    assert token not in r.text                                           # (65)

    listed = c.get("/api/v1/children/child_noah/proposals", headers=HANNAH)
    assert token not in listed.text
    detail = c.get(f"/api/v1/children/child_noah/proposals/{r.json()['proposal']['proposal_id']}",
                   headers=HANNAH)
    assert token not in detail.text
    # It IS the internal idempotency operation target, which is where it belongs.
    rec = _repo(c).query(C.IDEMPOTENCY_RECORDS)[0]
    assert rec["assignment_id"] == token
    assert rec["action"] == "create_add_activity_proposal"


def test_response_leaks_no_private_or_unrelated_family_data():
    """(66)"""
    c = _c()
    text = _post(c).text
    for other in ("child_maya", "child_eli", "child_theo", "par_elena", "ther_priya"):
        assert other not in text
    for banned in ("idempotency_key_hash", "request_hash", "Bearer", "dev-hannah"):
        assert banned not in text
    body_json = _post(c, key="k9").json()
    assert set(body_json) == {"proposal", "proposed_activity_version",
                              "child_summary", "audit_event_id", "idempotent_replay"}
    assert "current_assignment" not in body_json     # Add targets a day, not an assignment


# ── 67-72: therapist read compatibility ─────────────────────────────────────
def test_therapist_list_and_detail_include_the_add_proposal():
    """(67)(68)(69)(70)"""
    c = _c()
    pid = _post(c, b=body(day=TUESDAY)).json()["proposal"]["proposal_id"]

    listed = c.get("/api/v1/children/child_noah/proposals", headers=HANNAH).json()
    item = next(i for i in listed["items"] if i["proposal_id"] == pid)    # (67)
    assert item["proposal_type"] == "add"
    assert item["destination_scheduled_day"] == TUESDAY                   # (69)

    r = c.get(f"/api/v1/children/child_noah/proposals/{pid}", headers=HANNAH)
    assert r.status_code == 200                                           # (68)
    out = r.json()
    assert out["proposal_type"] == "add"
    assert out["proposal_status"] == "pending_parent_acceptance"
    assert out["destination_scheduled_day"] == TUESDAY                    # (69)
    assert out["current_assignment_id"] is None                           # (70)
    assert out["original_activity_template_id"] is None                   # (70)
    assert out["original_activity_version_id"] is None                    # (70)
    assert out["resulting_assignment_id"] is None
    assert out["proposed_activity_version_id"]


def test_existing_modify_therapist_shape_is_unchanged():
    """(71) The frozen Modify projection gains one optional field, always null."""
    c = _c()
    modify = c.post(
        "/api/v1/children/child_noah/weekly-plan/assignments/assign_noah_turntake/proposals/modify",
        headers={**HANNAH, "Idempotency-Key": "mod"},
        json={"expected_assignment_version": 1, "activity": act_social(),
              "change_reason": "r", "save_scope": "child_only"},
    )
    assert modify.status_code == 200
    pid = modify.json()["proposal"]["proposal_id"]
    out = c.get(f"/api/v1/children/child_noah/proposals/{pid}", headers=HANNAH).json()
    assert out["proposal_type"] == "modify"
    assert out["destination_scheduled_day"] is None
    assert out["current_assignment_id"] == "assign_noah_turntake"
    assert out["original_activity_version_id"] == "ver_turn_taking_v1"
    # The Modify creation response itself is untouched.
    assert set(modify.json()) == {"proposal", "proposed_activity_version",
                                  "current_assignment", "child_summary",
                                  "audit_event_id", "idempotent_replay"}


def test_unconnected_therapist_cannot_read_the_add_proposal():
    """(72)"""
    c = _c()
    pid = _post(c).json()["proposal"]["proposal_id"]
    r = c.get(f"/api/v1/children/child_noah/proposals/{pid}", headers=UNCONNECTED)
    assert r.status_code == 404
    assert r.json() == {"error": "not_found", "detail": "Not found."}
    assert c.get("/api/v1/children/child_noah/proposals",
                 headers=UNCONNECTED).status_code == 404


# ── 73-77: parent invisibility ──────────────────────────────────────────────
def _create_maya_add(c, key="p1", day=3):
    return _post(c, ADD_MAYA, key=key, b=body(day=day, plan="wp_maya")
                 ).json()["proposal"]["proposal_id"]


def test_parent_list_excludes_add_and_total_matches():
    """(73)(74)"""
    c = _c()
    before = c.get("/api/v1/children/child_maya/proposals", headers=ELENA).json()
    pid = _create_maya_add(c)
    after = c.get("/api/v1/children/child_maya/proposals", headers=ELENA).json()

    assert pid not in {i["proposal_id"] for i in after["items"]}          # (73)
    assert after["total"] == before["total"]                             # (74)
    assert after["total"] == len(after["items"])
    assert after["items"] == before["items"]        # existing Modify items unaffected


def test_parent_detail_for_an_add_is_a_canonical_404():
    """(75)"""
    c = _c()
    pid = _create_maya_add(c)
    r = c.get(f"/api/v1/children/child_maya/proposals/{pid}", headers=ELENA)
    assert r.status_code == 404
    # Byte-identical to an unknown proposal — nothing reveals that it exists.
    unknown = c.get("/api/v1/children/child_maya/proposals/prop_ghost", headers=ELENA)
    assert r.json() == unknown.json() == {"error": "not_found", "detail": "Not found."}


def test_add_eligibility_is_false_false_false():
    """(76)"""
    c = _c()
    pid = _create_maya_add(c)
    repo = _repo(c)
    proposal = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]
    flags = eligibility.evaluate_parent_decision(repo, "child_maya", proposal)
    assert flags == eligibility.INELIGIBLE
    assert (flags.can_accept, flags.can_decline, flags.needs_parent_attention) == (
        False, False, False)
    assert eligibility.proposal_is_safe_to_show(repo, "child_maya", proposal) is False


def test_existing_modify_parent_list_and_detail_are_unchanged():
    """(77) An Add in the store must not disturb the parent Modify surface."""
    c = _c()
    listed_before = c.get("/api/v1/children/child_maya/proposals", headers=ELENA).json()
    modify_id = listed_before["items"][0]["proposal_id"]
    detail_before = c.get(f"/api/v1/children/child_maya/proposals/{modify_id}",
                          headers=ELENA).json()

    _create_maya_add(c, key="noise", day=4)

    listed_after = c.get("/api/v1/children/child_maya/proposals", headers=ELENA).json()
    detail_after = c.get(f"/api/v1/children/child_maya/proposals/{modify_id}",
                         headers=ELENA).json()
    assert listed_after == listed_before
    assert detail_after == detail_before
    assert listed_after["items"], "the parent Modify surface must not be emptied"


def test_add_proposal_does_not_break_the_parent_list_with_a_500():
    """An unsupported proposal type must be dropped, never raise."""
    c = _c()
    for i in range(3):
        _create_maya_add(c, key=f"many-{i}", day=i + 2)
    r = c.get("/api/v1/children/child_maya/proposals", headers=ELENA)
    assert r.status_code == 200
    assert r.json()["total"] == len(r.json()["items"])


# ── 78-82: shared helper promotion ──────────────────────────────────────────
def test_shared_helpers_are_pure_and_read_only():
    """(82)"""
    c = _c()
    _add_assignment(c, "assign_noah_extra1", day=NOAH_DAY, order=1)
    repo = _repo(c)
    before = _snapshot(c)

    day = assignment_order.current_assignments_for_day(repo, "child_noah", "wp_noah", NOAH_DAY)
    assignment_order.assignment_order_map(day)
    assignment_order.has_duplicate_display_order(day)
    assignment_order.current_assignments_sharing_day(
        repo, repo.query(C.PLAN_ASSIGNMENTS, id="assign_noah_turntake")[0])

    assert _snapshot(c) == before


def test_shared_helpers_scope_by_child_plan_and_day():
    c = _c()
    repo = _repo(c)
    maya_day0 = assignment_order.current_assignments_for_day(
        repo, "child_maya", "wp_maya", 0)
    assert [a["id"] for a in maya_day0] == ["assign_maya_bubbles"]
    # Same day number, different child -> different set.
    assert assignment_order.current_assignments_for_day(
        repo, "child_noah", "wp_noah", 0) == []
    # Same child and day, different plan -> empty.
    assert assignment_order.current_assignments_for_day(
        repo, "child_maya", "wp_other", 0) == []


def _maya_modify(c, key="mm"):
    """A REAL modify proposal on Maya's day-0 bubbles activity.

    Built through the API rather than reusing the fixture proposal: the fictional
    `prop_maya_modify_bubbles` deliberately carries no `original_activity_version_id`,
    so it is pending-but-ineligible by design and cannot exercise a decision path.
    """
    r = c.post(
        "/api/v1/children/child_maya/weekly-plan/assignments/assign_maya_bubbles"
        "/proposals/modify",
        headers={**HANNAH, "Idempotency-Key": key},
        json={"expected_assignment_version": 1, "activity": act_social(),
              "change_reason": "Adjust after session.", "save_scope": "child_only"},
    )
    assert r.status_code == 200
    return r.json()["proposal"]["proposal_id"]


def test_acceptance_and_decline_still_handle_a_multi_activity_day():
    """(78)(79)(81) The promoted helpers preserved accept/decline behavior."""
    c = _c()
    repo = _repo(c)
    pid = _maya_modify(c)
    # A sibling activity sharing Maya's day 0 at a distinct position.
    _add_assignment(c, "assign_maya_sib", day=0, order=1,
                    child="child_maya", plan="wp_maya")
    target = repo.query(C.PLAN_ASSIGNMENTS, id="assign_maya_bubbles")[0]

    r = c.post(f"/api/v1/children/child_maya/proposals/{pid}/decline",
               headers={**ELENA, "Idempotency-Key": "d1"},
               json={"expected_proposal_version": 1,
                     "expected_assignment_version": target["version"]})
    assert r.status_code == 200                                          # (79)
    sib = repo.query(C.PLAN_ASSIGNMENTS, id="assign_maya_sib")[0]
    assert sib["display_order"] == 1 and sib["version"] == 1             # untouched


def test_acceptance_still_inherits_position_on_a_multi_activity_day():
    """(78) Acceptance through the promoted helpers keeps the day's ordering."""
    c = _c()
    repo = _repo(c)
    pid = _maya_modify(c)
    _add_assignment(c, "assign_maya_sib", day=0, order=1,
                    child="child_maya", plan="wp_maya")
    target = repo.query(C.PLAN_ASSIGNMENTS, id="assign_maya_bubbles")[0]

    r = c.post(f"/api/v1/children/child_maya/proposals/{pid}/accept",
               headers={**ELENA, "Idempotency-Key": "a1"},
               json={"expected_proposal_version": 1,
                     "expected_assignment_version": target["version"]})
    assert r.status_code == 200
    day0 = assignment_order.current_assignments_for_day(repo, "child_maya", "wp_maya", 0)
    orders = assignment_order.assignment_order_map(day0)
    assert sorted(orders.values()) == [0, 1]
    assert orders["assign_maya_sib"] == 1                     # sibling kept its place
    replacement = r.json()["replacement_assignment"]["assignment_id"]
    assert orders[replacement] == 0                           # inherited the original's


def test_modify_eligibility_still_allows_a_same_day_sibling():
    """(80)"""
    c = _c()
    repo = _repo(c)
    pid = _maya_modify(c)
    _add_assignment(c, "assign_maya_sib", day=0, order=1,
                    child="child_maya", plan="wp_maya")
    proposal = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]
    flags = eligibility.evaluate_parent_decision(repo, "child_maya", proposal)
    assert flags.can_accept and flags.can_decline


def test_duplicate_order_still_fails_modify_eligibility_closed():
    """(81)"""
    c = _c()
    repo = _repo(c)
    pid = _maya_modify(c)
    _add_assignment(c, "assign_maya_dupe", day=0, order=0,
                    child="child_maya", plan="wp_maya")     # collides with bubbles
    proposal = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]
    assert eligibility.evaluate_parent_decision(
        repo, "child_maya", proposal) == eligibility.INELIGIBLE


# ── OpenAPI / isolation ─────────────────────────────────────────────────────
def test_openapi_documents_the_add_route_and_preserves_modify():
    c = _c()
    schema = c.app.openapi()
    assert schema["openapi"] == "3.1.0"
    assert len(schema["paths"]) == 22
    path = "/api/v1/children/{child_id}/weekly-plan/proposals/add"
    assert path in schema["paths"] and "post" in schema["paths"][path]
    # The frozen Modify route is untouched.
    assert ("/api/v1/children/{child_id}/weekly-plan/assignments/{assignment_id}"
            "/proposals/modify") in schema["paths"]

    props = schema["components"]["schemas"]["AddProposalRequest"]["properties"]
    assert set(props) >= {"scheduled_day", "expected_weekly_plan_id", "activity",
                          "change_reason", "save_scope"}
    assert "display_order" not in props
    assert "expected_assignment_version" not in props

    summary = schema["components"]["schemas"]["AddProposalSummary"]["properties"]
    assert "destination_scheduled_day" in summary
    assert "display_order" not in summary
    assert "destination_scheduled_day" in schema["components"]["schemas"][
        "ProposalView"]["properties"]


def test_add_service_imports_no_parent_api_or_cloud_sdk():
    """(86)(88)

    Scans IMPORT STATEMENTS, not prose: both modules' docstrings legitimately say
    "no Firestore, no Firebase Auth", so a substring search over the whole source
    would fail on its own disclaimer rather than on a real dependency.
    """
    import ast
    import inspect

    banned = ("firebase", "firestore", "google", "genex_core", "boto3", "azure")
    for module in (A, assignment_order):
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert not any(b in name.lower() for b in banned), (module, name)
