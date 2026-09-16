"""Therapist marks a parent note DISCUSS NEXT SESSION (Phase 1B.3D).

`session_preparation_status: none -> discuss_at_next_session`, and nothing else.
The guarantees worth stating plainly:

* **Content is immutable, workflow state is not.** The parent's submission — id,
  child, author, type, body, link, captured title, created_at, environment,
  schema_version — is byte-identical afterwards. Only the session field moves.
* **The two workflow dimensions are independent.** Flagging never sets, clears
  or normalizes `review_status`, and the two commands compose in EITHER order to
  the same final state.
* **One-way.** No unflag, no clear, no reverse, and no transition into
  `discussed` — a note already `discussed` fails closed rather than moving back.

Discuss Next Session does not mean replied, answered, resolved, notified,
scheduled, discussed or completed.
"""

from __future__ import annotations

import ast
import copy
import inspect
import pathlib
import threading

from app.api import schemas as S
from app.domain.enums import (
    ParentNoteReviewStatus,
    ParentNoteType,
    PrincipalRole,
    SessionPreparationStatus,
)
from app.repository import collections as C
from app.services import note_review_service as RV
from app.services import note_session_service as V
from tests.conftest import ELENA, HANNAH, OMAR, PRIYA, UNCONNECTED, read_slice_client

#: Resolved from THIS file, never from the process working directory — pytest
#: runs with `working-directory: therapist_api` in CI, so a CWD-relative
#: "therapist_api/app" resolves to nothing there. A missing path makes `rglob`
#: yield an empty iterator, which would let the source-scanning guards below
#: PASS while inspecting no files at all. Same form as tests/test_isolation.py.
APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"

CHILD = "child_maya"
OTHER_CHILD = "child_eli"
NOAH_CHILD = "child_noah"
CURRENT_ASSIGNMENT = "assign_maya_bubbles"

# Fixture notes: pn_maya_1 is NEW/none, pn_maya_2 is REVIEWED/discuss_at_next_session.
NONE_NOTE = "pn_maya_1"
FLAGGED_NOTE = "pn_maya_2"
NOAH_NOTE = "pn_noah_1"          # another child, another caseload

ELENA_ID = "par_elena"

WATCHED = (C.PARENT_NOTES, C.PRIVATE_THERAPIST_NOTES, C.AUDIT_EVENTS,
           C.IDEMPOTENCY_RECORDS, C.PLAN_ASSIGNMENTS, C.PLAN_CHANGE_PROPOSALS,
           C.ACTIVITY_VERSIONS, C.WEEKLY_PLANS, C.CHILDREN, C.CONNECTIONS,
           C.PARENT_PROFILES, C.THERAPIST_PROFILES)

ROUTE = "/api/v1/children/{child}/notes/{note}/discuss-next-session"
REVIEW_ROUTE = "/api/v1/children/{child}/notes/{note}/review"


def _c():
    return read_slice_client()


def _repo(c):
    return c.app.state.repo


def _flag(c, note_id=NONE_NOTE, child=CHILD, headers=HANNAH, key="k-default"):
    return c.post(ROUTE.format(child=child, note=note_id),
                  headers={**headers, "Idempotency-Key": key})


def _review(c, note_id=NONE_NOTE, child=CHILD, headers=HANNAH, key="rk-default"):
    return c.post(REVIEW_ROUTE.format(child=child, note=note_id),
                  headers={**headers, "Idempotency-Key": key})


def _note(c, note_id=NONE_NOTE):
    return _repo(c).get(C.PARENT_NOTES, note_id)


def _plain(v):
    return getattr(v, "value", v)


def _events(c, note_id=NONE_NOTE, event_type=V.EVENT_TYPE):
    return [e for e in _repo(c).query(C.AUDIT_EVENTS)
            if e["event_type"] == event_type and e["subject_id"] == note_id]


def _seed(c, note_id, review, session, child=CHILD, parent=ELENA_ID):
    """Insert a fictional note in an exact (review, session) starting state."""
    base = copy.deepcopy(_note(c, NONE_NOTE))
    base.update(id=note_id, child_id=child, parent_id=parent,
                review_status=review, session_preparation_status=session)
    _repo(c).set(C.PARENT_NOTES, note_id, base)
    return base


# ── 1-6: the transition ─────────────────────────────────────────────────────
def test_authorized_therapist_marks_a_none_note_for_next_session():
    """(1)(2)"""
    c = _c()
    assert _plain(_note(c)["session_preparation_status"]) == "none"
    r = _flag(c, key="t1")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "note_id": NONE_NOTE,
        "session_preparation_status": "discuss_at_next_session",
        "review_status": "new",
        "idempotent_replay": False,
    }
    assert _plain(_note(c)["session_preparation_status"]) == "discuss_at_next_session"


def test_transition_persists_in_the_store():
    """(3)"""
    c = _c()
    _flag(c, key="t2")
    assert (_plain(_repo(c).get(C.PARENT_NOTES, NONE_NOTE)["session_preparation_status"])
            == SessionPreparationStatus.DISCUSS_AT_NEXT_SESSION.value)


def test_success_from_new_review_status():
    """(4) new / none -> new / discuss_at_next_session"""
    c = _c()
    _seed(c, "pn_a", "new", "none")
    r = _flag(c, "pn_a", key="t3")
    assert r.status_code == 200
    n = _note(c, "pn_a")
    assert (_plain(n["review_status"]), _plain(n["session_preparation_status"])) \
        == ("new", "discuss_at_next_session")


def test_success_from_reviewed_review_status():
    """(5) reviewed / none -> reviewed / discuss_at_next_session"""
    c = _c()
    _seed(c, "pn_b", "reviewed", "none")
    r = _flag(c, "pn_b", key="t4")
    assert r.status_code == 200
    n = _note(c, "pn_b")
    assert (_plain(n["review_status"]), _plain(n["session_preparation_status"])) \
        == ("reviewed", "discuss_at_next_session")
    assert r.json()["review_status"] == "reviewed"


def test_review_status_is_unchanged_in_both_starting_states():
    """(6) The dimension this service must not touch."""
    c = _c()
    for nid, review in (("pn_c", "new"), ("pn_d", "reviewed")):
        _seed(c, nid, review, "none")
        before = _plain(_note(c, nid)["review_status"])
        _flag(c, nid, key=f"t5-{nid}")
        assert _plain(_note(c, nid)["review_status"]) == before == review


def test_the_service_declares_exactly_one_transition():
    """(7) Constants, not prose."""
    assert V.FROM_STATUS is SessionPreparationStatus.NONE
    assert V.TO_STATUS is SessionPreparationStatus.DISCUSS_AT_NEXT_SESSION
    assert V.TERMINAL_STATUS is SessionPreparationStatus.DISCUSSED


# ── 8-13: semantic no-op and the DISCUSSED boundary ─────────────────────────
def test_already_flagged_fixture_is_a_semantic_no_op():
    """(8) pn_maya_2 ships REVIEWED/discuss_at_next_session."""
    c = _c()
    before = copy.deepcopy(_note(c, FLAGGED_NOTE))
    r = _flag(c, FLAGGED_NOTE, key="t6")
    assert r.status_code == 200, r.text
    assert r.json()["session_preparation_status"] == "discuss_at_next_session"
    assert r.json()["review_status"] == "reviewed"
    assert _note(c, FLAGGED_NOTE) == before


def test_already_flagged_under_a_brand_new_key_still_succeeds():
    """(9) Flagging is a destination, not a counter."""
    c = _c()
    _flag(c, key="t7")
    r = _flag(c, key="t7-completely-different")
    assert r.status_code == 200
    assert r.json()["session_preparation_status"] == "discuss_at_next_session"
    assert r.json()["idempotent_replay"] is False
    assert len(_events(c)) == 1


def test_no_second_transition_audit_for_a_semantic_no_op():
    """(10)"""
    c = _c()
    _flag(c, key="t8")
    for i in range(5):
        _flag(c, key=f"t8-{i}")
    assert len(_events(c)) == 1


def test_a_discussed_note_fails_closed():
    """(11)(12) DISCUSSED is PAST this transition; re-flagging would reverse it."""
    c = _c()
    _seed(c, "pn_done", "reviewed", "discussed")
    before = copy.deepcopy(_note(c, "pn_done"))
    r = _flag(c, "pn_done", key="t9")
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "invalid_session_preparation_transition"
    # Fail closed means nothing moved, in either direction.
    assert _note(c, "pn_done") == before
    assert _events(c, "pn_done") == []


def test_a_discussed_note_is_not_mistaken_for_already_flagged():
    """(13) The terminal check runs BEFORE the no-op branch."""
    c = _c()
    _seed(c, "pn_done2", "new", "discussed")
    assert _flag(c, "pn_done2", key="t10").status_code == 409
    assert _plain(_note(c, "pn_done2")["session_preparation_status"]) == "discussed"


def test_an_unrecognized_stored_session_value_fails_closed():
    """(14) Never guess what an unknown value meant."""
    c = _c()
    _seed(c, "pn_weird", "new", "something_unmapped")
    r = _flag(c, "pn_weird", key="t11")
    assert r.status_code == 409
    assert r.json()["error"] == "invalid_session_preparation_transition"
    assert _plain(_note(c, "pn_weird")["session_preparation_status"]) == "something_unmapped"


# ── 15-22: ParentNote immutability ──────────────────────────────────────────
def test_parent_authored_fields_are_untouched():
    """(15)(16)"""
    c = _c()
    before = copy.deepcopy(_note(c))
    _flag(c, key="t12")
    after = _note(c)
    for f in V.IMMUTABLE_NOTE_FIELDS:
        assert after.get(f) == before.get(f), f


def test_the_immutable_field_list_includes_review_status_and_all_parent_content():
    """(17) The founder-listed set, pinned as data."""
    assert V.IMMUTABLE_NOTE_FIELDS == (
        "id", "child_id", "parent_id", "note_type", "body",
        "linked_assignment_id", "linked_activity_title", "created_at",
        "environment", "schema_version", "review_status",
    )


def test_only_the_session_field_differs_after_a_real_transition():
    """(18) Whole-document diff, not a field allow-list."""
    c = _c()
    before = copy.deepcopy(_note(c))
    _flag(c, key="t13")
    after = _note(c)
    assert set(after) == set(before)
    assert [k for k in after if after[k] != before[k]] == ["session_preparation_status"]


def test_body_and_author_survive_verbatim():
    """(19)"""
    c = _c()
    before = copy.deepcopy(_note(c))
    _flag(c, key="t14")
    after = _note(c)
    assert after["body"] == before["body"]
    assert after["parent_id"] == before["parent_id"] == ELENA_ID
    assert after["created_at"] == before["created_at"]


def test_the_model_is_not_rebuilt():
    """(20) A rebuild would refill defaults / re-coerce untouched fields.

    AST, not substring: the module docstring deliberately NAMES
    `ParentNote(**doc).model_dump()` to explain why it is avoided, so a text
    scan would fail on the very prose that documents the guarantee.
    """
    calls = []
    for n in ast.walk(ast.parse(inspect.getsource(V))):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name) and f.id == "ParentNote":
                calls.append(f"ParentNote(...):{n.lineno}")
            if isinstance(f, ast.Attribute) and f.attr in ("model_validate", "model_construct"):
                calls.append(f"{f.attr}:{n.lineno}")
    assert calls == [], calls


def test_no_new_note_field_is_introduced():
    """(21) No version, updated_at, flag_count or discussion metadata."""
    c = _c()
    _flag(c, key="t15")
    n = _note(c)
    for banned in ("version", "updated_at", "flag_count", "discussed_at",
                   "marked_at", "session_id", "scheduled_at", "review_count"):
        assert banned not in n, banned


def test_no_edit_delete_or_withdraw_surface_exists():
    """(22)"""
    c = _c()
    paths = c.app.openapi()["paths"]
    verbs = {m for v in paths.values() for m in v if m in ("put", "patch", "delete")}
    assert verbs == set(), verbs


def test_other_notes_are_untouched():
    """(23) A flag is scoped to one note."""
    c = _c()
    before = {n["id"]: copy.deepcopy(n) for n in _repo(c).query(C.PARENT_NOTES)}
    _flag(c, key="t16")
    after = {n["id"]: n for n in _repo(c).query(C.PARENT_NOTES)}
    assert set(after) == set(before)
    for nid in before:
        if nid != NONE_NOTE:
            assert after[nid] == before[nid], nid


# ── 24-33: authorization ────────────────────────────────────────────────────
def test_unauthenticated_is_canonical_401():
    """(24)"""
    c = _c()
    r = c.post(ROUTE.format(child=CHILD, note=NONE_NOTE),
               headers={"Idempotency-Key": "a1"})
    assert r.status_code == 401


def test_parent_cannot_mark_for_next_session():
    """(25) Even the note's own author."""
    c = _c()
    r = _flag(c, headers=ELENA, key="a2")
    assert r.status_code == 403
    assert r.json()["error"] == "forbidden"
    assert _plain(_note(c)["session_preparation_status"]) == "none"


def test_unauthorized_therapist_is_existence_blind_404():
    """(26) Priya is connected to another child only."""
    c = _c()
    r = _flag(c, headers=PRIYA, key="a3")
    assert r.status_code == 404
    assert _plain(_note(c)["session_preparation_status"]) == "none"


def test_unconnected_therapist_is_404():
    """(27)"""
    c = _c()
    assert _flag(c, headers=UNCONNECTED, key="a4").status_code == 404


def test_unknown_child_is_404():
    """(28)"""
    c = _c()
    assert _flag(c, child="child_does_not_exist", key="a5").status_code == 404


def test_unknown_note_is_404():
    """(29)"""
    c = _c()
    assert _flag(c, "pn_does_not_exist", key="a6").status_code == 404


def test_cross_child_note_is_404():
    """(30) Right therapist, right note, wrong child in the path."""
    c = _c()
    r = _flag(c, NOAH_NOTE, child=CHILD, key="a7")
    assert r.status_code == 404
    assert _plain(_note(c, NOAH_NOTE)["session_preparation_status"]) == "none"


def test_unrelated_object_id_as_note_id_is_404():
    """(31) The query is scoped to parent_notes, so nothing else resolves."""
    c = _c()
    for other in (CURRENT_ASSIGNMENT, "wp_maya", "prop_maya_modify", CHILD):
        assert _flag(c, other, key=f"a8-{other}").status_code == 404, other


def test_no_existence_leakage_across_the_404_family():
    """(32) Identical bodies for unknown, cross-child and unauthorized."""
    c = _c()
    bodies = {
        _flag(c, "pn_nope", key="a9").text,
        _flag(c, NOAH_NOTE, child=CHILD, key="a10").text,
        _flag(c, NONE_NOTE, headers=PRIYA, key="a11").text,
        _flag(c, CURRENT_ASSIGNMENT, key="a12").text,
    }
    assert len(bodies) == 1, bodies


def test_another_parent_cannot_reach_it_either():
    """(33)"""
    c = _c()
    assert _flag(c, headers=OMAR, key="a13").status_code == 403


# ── 34-41: idempotency ──────────────────────────────────────────────────────
def test_missing_idempotency_key_is_400():
    """(34) Required at runtime."""
    c = _c()
    r = c.post(ROUTE.format(child=CHILD, note=NONE_NOTE),
               headers=HANNAH)
    assert r.status_code == 400
    assert r.json()["error"] == "missing_idempotency_key"
    assert _plain(_note(c)["session_preparation_status"]) == "none"


def test_blank_idempotency_key_is_400():
    """(35)"""
    c = _c()
    for blank in ("", "   ", "\t"):
        r = c.post(ROUTE.format(child=CHILD, note=NONE_NOTE),
                   headers={**HANNAH, "Idempotency-Key": blank})
        assert r.status_code == 400, blank


def test_same_key_replay_returns_the_stored_result():
    """(36)(37)"""
    c = _c()
    first = _flag(c, key="i1")
    again = _flag(c, key="i1")
    assert first.status_code == again.status_code == 200
    assert again.json()["idempotent_replay"] is True
    assert {k: v for k, v in again.json().items() if k != "idempotent_replay"} \
        == {k: v for k, v in first.json().items() if k != "idempotent_replay"}


def test_same_key_replay_creates_no_duplicate_transition_or_audit():
    """(38)"""
    c = _c()
    _flag(c, key="i2")
    n1 = copy.deepcopy(_note(c))
    for _ in range(4):
        _flag(c, key="i2")
    assert _note(c) == n1
    assert len(_events(c)) == 1
    assert len([r for r in _repo(c).query(C.IDEMPOTENCY_RECORDS)
                if r["action"] == V.ACTION]) == 1


def test_same_key_for_a_different_note_is_409():
    """(39)"""
    c = _c()
    _flag(c, key="i3")
    _seed(c, "pn_other", "new", "none")
    r = _flag(c, "pn_other", key="i3")
    assert r.status_code == 409
    assert r.json()["error"] == "idempotency_key_conflict"
    assert _plain(_note(c, "pn_other")["session_preparation_status"]) == "none"


def test_same_key_across_a_different_action_is_409():
    """(40) Command identity includes the action, so /review and this differ."""
    c = _c()
    assert _review(c, key="i4").status_code == 200
    r = _flag(c, key="i4")
    assert r.status_code == 409
    assert r.json()["error"] == "idempotency_key_conflict"


def test_exactly_one_logical_transition_per_note():
    """(41) Many keys, one transition."""
    c = _c()
    for i in range(6):
        assert _flag(c, key=f"i5-{i}").status_code == 200
    assert len(_events(c)) == 1
    assert _plain(_note(c)["session_preparation_status"]) == "discuss_at_next_session"


# ── 42-50: audit ────────────────────────────────────────────────────────────
def test_exactly_one_transition_audit_event():
    """(42)"""
    c = _c()
    _flag(c, key="d1")
    assert len(_events(c)) == 1


def test_audit_event_type_is_named_for_what_happened():
    """(43) Not discussion_completed / reply_sent / parent_notified.

    Checked against the actual emitted values, not the source text: the module
    docstring and inline comments deliberately NAME those wrong event types to
    record that they were rejected, so a substring scan would flag the prose
    that documents the decision.
    """
    assert V.EVENT_TYPE == "parent_note_marked_for_next_session"
    assert V.ACTION == "mark_parent_note_for_next_session"
    BANNED = {"discussion_completed", "reply_sent", "note_resolved",
              "parent_notified", "session_scheduled", "message_sent",
              "message_read", "message_seen"}
    # Every string that could reach an `event_type=` or `action=` field.
    emitted = set()
    for n in ast.walk(ast.parse(inspect.getsource(V))):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) \
                and isinstance(n.value.value, str):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id in ("EVENT_TYPE", "ACTION"):
                    emitted.add(n.value.value)
        for kw in getattr(n, "keywords", []) or []:
            if kw.arg in ("event_type", "action") and isinstance(kw.value, ast.Constant):
                emitted.add(kw.value.value)
    assert emitted & BANNED == set(), emitted & BANNED
    assert emitted == {"parent_note_marked_for_next_session",
                       "mark_parent_note_for_next_session"}, emitted


def test_audit_records_the_therapist_actor_and_role():
    """(44)"""
    c = _c()
    _flag(c, key="d2")
    e = _events(c)[0]
    assert e["actor_uid"] == "dev-hannah"
    assert _plain(e["actor_role"]) == PrincipalRole.THERAPIST.value
    assert e["subject_type"] == "parent_note"
    assert e["subject_id"] == NONE_NOTE
    assert e["child_id"] == CHILD


def test_audit_distinguishes_reviewer_actor_from_note_author():
    """(45) The parent authored it; the therapist acted on it."""
    c = _c()
    _flag(c, key="d3")
    e = _events(c)[0]
    assert e["before_state"]["note_author_parent_id"] == ELENA_ID
    assert e["before_state"]["marked_by_therapist_id"] == "ther_hannah"
    assert e["after_state"]["note_author_parent_id"] == ELENA_ID
    assert e["after_state"]["marked_by_therapist_id"] == "ther_hannah"


def test_audit_captures_the_session_transition_on_both_sides():
    """(46)"""
    c = _c()
    _flag(c, key="d4")
    e = _events(c)[0]
    assert e["before_state"]["session_preparation_status"] == "none"
    assert e["after_state"]["session_preparation_status"] == "discuss_at_next_session"


def test_audit_proves_review_status_did_not_move():
    """(47) Recorded on both sides precisely so the record itself shows it."""
    c = _c()
    _seed(c, "pn_e", "reviewed", "none")
    _flag(c, "pn_e", key="d5")
    e = _events(c, "pn_e")[0]
    assert e["before_state"]["review_status"] == e["after_state"]["review_status"] == "reviewed"


def test_audit_stores_only_a_hashed_idempotency_key():
    """(48)"""
    c = _c()
    _flag(c, key="super-secret-key")
    e = _events(c)[0]
    assert e["idempotency_key_hash"]
    assert "super-secret-key" not in str(e)


def test_audit_carries_request_and_environment_metadata():
    """(49)"""
    c = _c()
    _flag(c, key="d6")
    e = _events(c)[0]
    assert e["environment"] == "dev"
    assert e["occurred_at"] and e["created_at"]
    assert "request_id" in e


def test_audit_id_is_derived_from_the_note_not_the_key():
    """(50) Defence in depth for the one-event guarantee."""
    c = _c()
    _flag(c, key="d7")
    from app.domain.ids import derived_id
    assert _events(c)[0]["id"] == derived_id("aud", V.EVENT_TYPE, NONE_NOTE)


def test_no_audit_event_for_a_semantic_no_op_request():
    """(51) The idempotency record points at no event."""
    c = _c()
    _flag(c, key="d8")
    _flag(c, key="d9")
    recs = [r for r in _repo(c).query(C.IDEMPOTENCY_RECORDS)
            if r["action"] == V.ACTION]
    by_event = {r["id"]: r["audit_event_id"] for r in recs}
    assert sorted(v is None for v in by_event.values()) == [False, True]
    assert len(_events(c)) == 1


# ── 52-57: rollback and repeat safety ───────────────────────────────────────
def test_injected_failure_rolls_everything_back(monkeypatch):
    """(52)(53)(54)(55)"""
    c = _c()
    before = {col: copy.deepcopy(sorted(_repo(c).query(col), key=lambda r: r["id"]))
              for col in WATCHED}
    monkeypatch.setattr(V, "_result", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected")))
    try:
        _flag(c, key="rb1")
    except RuntimeError:
        pass
    after = {col: sorted(_repo(c).query(col), key=lambda r: r["id"]) for col in WATCHED}
    assert after == before
    assert _plain(_note(c)["session_preparation_status"]) == "none"
    assert _events(c) == []
    assert not [r for r in _repo(c).query(C.IDEMPOTENCY_RECORDS) if r["action"] == V.ACTION]


def test_the_key_is_reusable_after_a_rolled_back_attempt(monkeypatch):
    """(56) The failed attempt persisted no idempotency record."""
    c = _c()
    monkeypatch.setattr(V, "_result", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected")))
    try:
        _flag(c, key="rb2")
    except RuntimeError:
        pass
    monkeypatch.undo()
    r = _flag(c, key="rb2")
    assert r.status_code == 200
    assert _plain(_note(c)["session_preparation_status"]) == "discuss_at_next_session"


def test_repeated_concurrent_attempts_produce_one_transition():
    """(57) Same key from several threads."""
    c = _c()
    out = []
    def go():
        out.append(_flag(c, key="conc-same").status_code)
    ts = [threading.Thread(target=go) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert out.count(200) == 6, out
    assert len(_events(c)) == 1
    assert _plain(_note(c)["session_preparation_status"]) == "discuss_at_next_session"


def test_distinct_keys_from_several_threads_still_produce_one_transition():
    """(58)"""
    c = _c()
    out = []
    def go(i):
        out.append(_flag(c, key=f"conc-{i}").status_code)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert set(out) == {200}, out
    assert len(_events(c)) == 1


def test_a_failed_flag_leaves_plan_state_alone():
    """(59)"""
    c = _c()
    plan_cols = (C.PLAN_ASSIGNMENTS, C.WEEKLY_PLANS, C.PLAN_CHANGE_PROPOSALS,
                 C.ACTIVITY_VERSIONS)
    before = {n: copy.deepcopy(sorted(_repo(c).query(n), key=lambda r: r["id"]))
              for n in plan_cols}
    _flag(c, key="p1")
    after = {n: sorted(_repo(c).query(n), key=lambda r: r["id"]) for n in plan_cols}
    assert after == before


# ── 60-68: read propagation ─────────────────────────────────────────────────
def test_therapist_child_note_read_reflects_the_flag():
    """(60)"""
    c = _c()
    _flag(c, key="r1")
    items = c.get(f"/api/v1/children/{CHILD}/notes",
                  headers=HANNAH).json()["items"]
    mine = [n for n in items if n["note_id"] == NONE_NOTE][0]
    assert mine["session_preparation_status"] == "discuss_at_next_session"
    assert mine["review_status"] == "new"


def test_therapist_cross_child_inbox_reflects_the_flag():
    """(61)"""
    c = _c()
    _flag(c, key="r2")
    items = c.get("/api/v1/notes",
                  headers=HANNAH).json()["items"]
    mine = [n for n in items if n["note_id"] == NONE_NOTE][0]
    assert mine["session_preparation_status"] == "discuss_at_next_session"


def test_parent_own_note_history_reflects_the_flag():
    """(62)"""
    c = _c()
    _flag(c, key="r3")
    items = c.get(f"/api/v1/children/{CHILD}/notes",
                  headers=ELENA).json()["items"]
    mine = [n for n in items if n["note_id"] == NONE_NOTE][0]
    assert mine["session_preparation_status"] == "discuss_at_next_session"
    assert mine["review_status"] == "new"


def test_next_session_includes_the_note_from_real_runtime_state():
    """(63)(64) Previously this endpoint reflected only seeded fixture state."""
    c = _c()
    h = HANNAH
    before = c.get(f"/api/v1/children/{CHILD}/next-session", headers=h).json()
    assert NONE_NOTE not in [i["note_id"] for i in before["parent_note_items"]]
    _flag(c, key="r4")
    after = c.get(f"/api/v1/children/{CHILD}/next-session", headers=h).json()
    ids = [i["note_id"] for i in after["parent_note_items"]]
    assert NONE_NOTE in ids
    assert len(after["parent_note_items"]) == len(before["parent_note_items"]) + 1


def test_next_session_still_contains_the_seeded_flagged_note():
    """(65) The new writer does not displace fixture state."""
    c = _c()
    h = HANNAH
    _flag(c, key="r5")
    ids = [i["note_id"] for i in
           c.get(f"/api/v1/children/{CHILD}/next-session", headers=h).json()["parent_note_items"]]
    assert FLAGGED_NOTE in ids and NONE_NOTE in ids


def test_next_session_excludes_a_discussed_note():
    """(66) Only discuss_at_next_session is surfaced."""
    c = _c()
    _seed(c, "pn_done3", "reviewed", "discussed")
    ids = [i["note_id"] for i in
           c.get(f"/api/v1/children/{CHILD}/next-session",
                 headers=HANNAH).json()["parent_note_items"]]
    assert "pn_done3" not in ids


def test_no_propagation_endpoint_was_added():
    """(67) 23 frozen paths + exactly one new action."""
    c = _c()
    s = c.app.openapi()
    assert len(s["paths"]) == 24
    verbs = ("get", "post", "put", "patch", "delete")
    assert sum(len([m for m in v if m in verbs]) for v in s["paths"].values()) == 26, \
        "25 frozen operations + the Phase 1B.3E private-note write"
    notes = s["paths"]["/api/v1/children/{child_id}/notes"]
    assert {m for m in notes if m in verbs} == {"get", "post"}


def test_gets_remain_read_only():
    """(68) Reading never flags, and flagging leaves no read receipt."""
    c = _c()
    before = {n["id"]: copy.deepcopy(n) for n in _repo(c).query(C.PARENT_NOTES)}
    audits = len(_repo(c).query(C.AUDIT_EVENTS))
    for _ in range(3):
        for url, tok in ((f"/api/v1/children/{CHILD}/notes", HANNAH),
                         (f"/api/v1/children/{CHILD}/notes", ELENA),
                         ("/api/v1/notes", HANNAH),
                         (f"/api/v1/children/{CHILD}/next-session", HANNAH)):
            c.get(url, headers=tok)
    assert {n["id"]: n for n in _repo(c).query(C.PARENT_NOTES)} == before
    assert len(_repo(c).query(C.AUDIT_EVENTS)) == audits


# ── 69-78: independence from review_status (both orders) ────────────────────
def test_case_a_discuss_then_review():
    """(69)(70) new/none -> discuss -> review == reviewed/discuss."""
    c = _c()
    _seed(c, "pn_order_a", "new", "none")
    assert _flag(c, "pn_order_a", key="o1").status_code == 200
    n = _note(c, "pn_order_a")
    assert (_plain(n["review_status"]), _plain(n["session_preparation_status"])) \
        == ("new", "discuss_at_next_session")
    assert _review(c, "pn_order_a", key="o2").status_code == 200
    n = _note(c, "pn_order_a")
    assert (_plain(n["review_status"]), _plain(n["session_preparation_status"])) \
        == ("reviewed", "discuss_at_next_session")


def test_case_b_review_then_discuss():
    """(71)(72) new/none -> review -> discuss == reviewed/discuss."""
    c = _c()
    _seed(c, "pn_order_b", "new", "none")
    assert _review(c, "pn_order_b", key="o3").status_code == 200
    n = _note(c, "pn_order_b")
    assert (_plain(n["review_status"]), _plain(n["session_preparation_status"])) \
        == ("reviewed", "none")
    assert _flag(c, "pn_order_b", key="o4").status_code == 200
    n = _note(c, "pn_order_b")
    assert (_plain(n["review_status"]), _plain(n["session_preparation_status"])) \
        == ("reviewed", "discuss_at_next_session")


def test_both_orders_converge_on_the_same_state():
    """(73) The substantive independence claim."""
    c = _c()
    _seed(c, "pn_x", "new", "none")
    _seed(c, "pn_y", "new", "none")
    _flag(c, "pn_x", key="o5"); _review(c, "pn_x", key="o6")
    _review(c, "pn_y", key="o7"); _flag(c, "pn_y", key="o8")
    x, y = _note(c, "pn_x"), _note(c, "pn_y")
    assert (_plain(x["review_status"]), _plain(x["session_preparation_status"])) \
        == (_plain(y["review_status"]), _plain(y["session_preparation_status"])) \
        == ("reviewed", "discuss_at_next_session")


def test_all_four_combinations_are_reachable_and_valid():
    """(74)"""
    c = _c()
    combos = set()
    for nid, rev in (("pn_q1", False), ("pn_q2", True)):
        for flag in (False, True):
            key = f"{nid}-{flag}"
            _seed(c, key, "new", "none")
            if rev:
                _review(c, key, key=f"cr-{key}")
            if flag:
                _flag(c, key, key=f"cf-{key}")
            n = _note(c, key)
            combos.add((_plain(n["review_status"]), _plain(n["session_preparation_status"])))
    assert combos == {("new", "none"), ("new", "discuss_at_next_session"),
                      ("reviewed", "none"), ("reviewed", "discuss_at_next_session")}


def test_flagging_never_implicitly_marks_reviewed():
    """(75) Across every note in the store."""
    c = _c()
    before = {n["id"]: _plain(n["review_status"]) for n in _repo(c).query(C.PARENT_NOTES)}
    for nid in list(before):
        n = _note(c, nid)
        if _plain(n["session_preparation_status"]) != "discussed":
            _flag(c, nid, child=n["child_id"], key=f"sweep-{nid}")
    after = {n["id"]: _plain(n["review_status"]) for n in _repo(c).query(C.PARENT_NOTES)}
    assert after == before


def test_reviewing_never_implicitly_flags():
    """(76) The frozen guarantee, restated from this side."""
    c = _c()
    before = {n["id"]: _plain(n["session_preparation_status"])
              for n in _repo(c).query(C.PARENT_NOTES)}
    for nid in list(before):
        n = _note(c, nid)
        _review(c, nid, child=n["child_id"], key=f"rsweep-{nid}")
    after = {n["id"]: _plain(n["session_preparation_status"])
             for n in _repo(c).query(C.PARENT_NOTES)}
    assert after == before


def test_neither_service_imports_the_other():
    """(77) Independence in the code, not just in behavior."""
    for mod, other in ((V, "note_review_service"), (RV, "note_session_service")):
        tree = ast.parse(inspect.getsource(mod))
        names = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names += [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                names.append(n.module or "")
                names += [a.name for a in n.names]
        assert other not in names, (mod.__name__, other, names)


def test_the_session_service_never_assigns_review_status():
    """(78) AST, so docstring prose cannot pass or fail this."""
    writers = []
    for n in ast.walk(ast.parse(inspect.getsource(V))):
        targets = (list(n.targets) if isinstance(n, ast.Assign)
                   else [n.target] if isinstance(n, (ast.AugAssign, ast.AnnAssign))
                   else [])
        for t in targets:
            if (isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                    and t.slice.value == "review_status"):
                writers.append(n.lineno)
    assert writers == [], writers


# ── 79-86: no DISCUSSED, no reverse ─────────────────────────────────────────
def test_discussed_remains_a_declared_value_with_no_writer():
    """(79)(80) The enum member stays; nothing may transition into it."""
    assert SessionPreparationStatus.DISCUSSED.value == "discussed"
    sources = sorted(APP_DIR.rglob("*.py"))
    assert sources, f"no sources found under {APP_DIR}"
    offenders = []
    for f in sources:
        if f.name == "data.py":          # fixtures may seed any state
            continue
        for n in ast.walk(ast.parse(f.read_text())):
            if not isinstance(n, ast.Assign):
                continue
            v = n.value
            puts_discussed = (
                (isinstance(v, ast.Attribute) and v.attr == "DISCUSSED")
                or (isinstance(v, ast.Constant) and v.value == "discussed")
            )
            if not puts_discussed:
                continue
            # Only a WRITE counts: assigning into a record field, e.g.
            # `note["session_preparation_status"] = ...`. Declaring the enum
            # member (`DISCUSSED = "discussed"`) or naming it as a module
            # constant (`TERMINAL_STATUS = SessionPreparationStatus.DISCUSSED`,
            # which exists precisely to REJECT that state) is not a write.
            for t in n.targets:
                if isinstance(t, ast.Subscript):
                    offenders.append(f"{f.relative_to(APP_DIR).as_posix()}:{n.lineno}")
    assert offenders == [], offenders
    # And nothing assigns it as a keyword either (outside fixtures).
    kwargs = []
    for f in sources:
        if f.name == "data.py":
            continue
        for n in ast.walk(ast.parse(f.read_text())):
            for kw in getattr(n, "keywords", []) or []:
                if kw.arg != "session_preparation_status":
                    continue
                v = kw.value
                if (isinstance(v, ast.Attribute) and v.attr == "DISCUSSED") or \
                   (isinstance(v, ast.Constant) and v.value == "discussed"):
                    kwargs.append(f"{f.relative_to(APP_DIR).as_posix()}:{n.lineno}")
    assert kwargs == [], kwargs


def test_no_mark_discussed_route_exists():
    """(81)"""
    c = _c()
    for p in c.app.openapi()["paths"]:
        last = p.rsplit("/", 1)[-1]
        assert last not in ("discussed", "mark-discussed", "complete",
                            "session-complete", "discussion-complete"), p


def test_no_mark_discussed_or_session_completion_service_exists():
    """(82)"""
    service_files = sorted((APP_DIR / "services").rglob("*.py"))
    assert service_files, f"no services found under {APP_DIR / 'services'}"
    services = "\n".join(p.read_text() for p in service_files)
    assert services.strip(), "service sources read as empty"
    for banned in ("def mark_discussed", "def complete_session",
                   "def mark_session_complete", "def finish_discussion",
                   "def set_session_preparation", "def mark_for_next_session",
                   "mark_discuss", "def discuss"):
        assert banned not in services, banned


def test_no_reverse_or_unflag_surface_exists():
    """(83) One-way only."""
    c = _c()
    for p in c.app.openapi()["paths"]:
        last = p.rsplit("/", 1)[-1]
        assert last not in ("unflag", "undiscuss", "clear", "reopen",
                            "clear-session-preparation", "cancel-discussion"), p
    services = "\n".join(p.read_text() for p in sorted((APP_DIR / "services").rglob("*.py")))
    for banned in ("def unflag", "def clear_session", "def undiscuss",
                   "def reopen", "def cancel_discussion"):
        assert banned not in services, banned


def test_the_service_never_writes_none_back():
    """(84) No path assigns the FROM state — the transition is one-way.

    AST, not substring: the service legitimately COMPARES against FROM_STATUS
    (`session_before != FROM_STATUS.value`) to fail closed, and `!=` contains
    `=`, so a text scan would flag the guard that enforces the invariant.
    """
    writers = []
    for n in ast.walk(ast.parse(inspect.getsource(V))):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if (isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                        and t.slice.value == "session_preparation_status"):
                    writers.append(ast.dump(n.value))
    assert len(writers) == 1, writers
    assert "TO_STATUS" in writers[0], writers


def test_reading_next_session_does_not_transition_anything():
    """(85) No automatic transition after reading."""
    c = _c()
    _flag(c, key="n1")
    before = {n["id"]: copy.deepcopy(n) for n in _repo(c).query(C.PARENT_NOTES)}
    for _ in range(5):
        c.get(f"/api/v1/children/{CHILD}/next-session",
              headers=HANNAH)
    assert {n["id"]: n for n in _repo(c).query(C.PARENT_NOTES)} == before


def test_reviewing_does_not_transition_the_session_dimension_into_discussed():
    """(86) No automatic transition after review."""
    c = _c()
    _flag(c, key="n2")
    _review(c, key="n3")
    assert _plain(_note(c)["session_preparation_status"]) == "discuss_at_next_session"


def test_no_session_scheduling_concept_was_introduced():
    """(87)"""
    c = _c()
    s = c.app.openapi()
    for name, sch in s["components"]["schemas"].items():
        for prop in (sch.get("properties") or {}):
            assert prop not in ("scheduled_at", "session_date", "session_id",
                                "appointment_id", "discussed_at"), f"{name}.{prop}"


# ── 88-95: no chat / reply ──────────────────────────────────────────────────
def test_no_chat_surface_was_introduced():
    """(88)(89)(90)"""
    c = _c()
    s = c.app.openapi()
    for p in s["paths"]:
        last = p.rsplit("/", 1)[-1]
        assert last not in ("reply", "replies", "respond", "answer", "messages",
                            "thread", "threads", "conversation"), p
    for name in s["components"]["schemas"]:
        assert not any(w in name.lower() for w in
                       ("reply", "thread", "conversation", "message", "receipt")), name


def test_no_chat_fields_exist_anywhere_in_the_api():
    """(91)(92)"""
    c = _c()
    s = c.app.openapi()
    for name, sch in s["components"]["schemas"].items():
        for prop in (sch.get("properties") or {}):
            assert not any(w in prop.lower() for w in
                           ("reply", "thread", "conversation", "typing",
                            "read_receipt", "notified")), f"{name}.{prop}"


def test_the_response_is_a_four_field_allow_list():
    """(93)"""
    c = _c()
    r = _flag(c, key="c1")
    assert sorted(r.json()) == ["idempotent_replay", "note_id",
                                "review_status", "session_preparation_status"]


def test_the_response_leaks_no_internals():
    """(94)"""
    c = _c()
    r = _flag(c, key="c2")
    for banned in ("audit", "idempotency_key", "parent_id", "therapist_id",
                   "linked_assignment_id", "weekly_plan_id", "body",
                   "created_at", "child_id"):
        assert banned not in r.text, banned


def test_no_parent_notification_was_created():
    """(95) Flagging notifies nobody; there is no notification concept."""
    c = _c()
    cols_before = {col: len(_repo(c).query(col)) for col in C.ALL}
    _flag(c, key="c3")
    cols_after = {col: len(_repo(c).query(col)) for col in C.ALL}
    grew = {k for k in C.ALL if cols_after[k] != cols_before[k]}
    assert grew == {C.AUDIT_EVENTS, C.IDEMPOTENCY_RECORDS}, grew


def test_no_private_therapist_note_is_written():
    """(96) A separate, still-unimplemented phase."""
    c = _c()
    before = _repo(c).query(C.PRIVATE_THERAPIST_NOTES)
    _flag(c, key="c4")
    assert _repo(c).query(C.PRIVATE_THERAPIST_NOTES) == before
    writes = [l for l in inspect.getsource(V).splitlines()
              if "PRIVATE_THERAPIST_NOTES" in l]
    assert writes == [], writes


# ── 97-100: contract / isolation ────────────────────────────────────────────
def test_openapi_documents_the_action_without_a_request_body():
    """(97)"""
    c = _c()
    s = c.app.openapi()
    assert s["openapi"] == "3.1.0"
    path = "/api/v1/children/{child_id}/notes/{note_id}/discuss-next-session"
    op = s["paths"][path]["post"]
    assert "requestBody" not in op
    assert op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"] \
        .endswith("NoteSessionPreparationResponse")
    for code in ("400", "401", "403", "404", "409"):
        assert code in op["responses"], code


def test_the_response_schema_has_no_request_counterpart():
    """(98) Nothing for a client to inject a status into."""
    c = _c()
    names = c.app.openapi()["components"]["schemas"]
    assert "NoteSessionPreparationResponse" in names
    for banned in ("NoteSessionPreparationRequest", "DiscussNextSessionRequest",
                   "SessionPreparationUpdate", "ParentNoteUpdateRequest"):
        assert banned not in names, banned


def test_no_plan_lifecycle_or_proposal_state_leaks_into_the_response():
    """(99)"""
    assert set(S.NoteSessionPreparationResponse.model_fields) == {
        "note_id", "session_preparation_status", "review_status", "idempotent_replay"}


def test_the_session_service_imports_nothing_external():
    """(100) AST-checked: docstring prose must not be able to pass or fail this."""
    source = APP_DIR / "services" / "note_session_service.py"
    assert source.is_file(), f"session service not found at {source}"
    tree = ast.parse(source.read_text())
    names = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            names.append(n.module or "")
    for nm in names:
        assert not any(b in nm.lower() for b in
                       ("firebase", "firestore", "google", "genex_core",
                        "boto3", "azure", "requests", "httpx")), nm
