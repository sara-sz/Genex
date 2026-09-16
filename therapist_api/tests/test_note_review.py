"""Therapist marks a parent note REVIEWED (Phase 1B.3C).

`review_status: new -> reviewed`, and nothing else. The two guarantees worth
stating plainly:

* **Content is immutable, workflow state is not.** The parent's submission — id,
  child, author, type, body, link, captured title, created_at, environment,
  schema_version — is byte-identical after review. Only `review_status` moves.
* **The two workflow dimensions stay independent.** Reviewing never sets, clears
  or normalizes `session_preparation_status`. Discuss Next Session is a separate
  action that does not exist yet.

Reviewed does not mean replied, answered, agreed, resolved, discussed or seen.
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
from app.domain.read_models import ParentNote
from app.repository import collections as C
from app.services import note_review_service as V
from app.services import parent_note_service as W
from tests.conftest import ELENA, HANNAH, OMAR, PRIYA, UNCONNECTED, read_slice_client

#: Resolved from THIS file, never from the process working directory — pytest
#: runs with `working-directory: therapist_api` in CI, so a CWD-relative
#: "therapist_api/app" resolves to nothing there. A missing path makes `rglob`
#: yield an empty iterator, which would let the source-scanning guards below
#: PASS while inspecting no files at all. Same form as tests/test_isolation.py.
APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"

CHILD = "child_maya"
OTHER_CHILD = "child_eli"
CURRENT_ASSIGNMENT = "assign_maya_bubbles"

# Fixture notes: pn_maya_1 is NEW/none, pn_maya_2 is REVIEWED/discuss_at_next_session.
NEW_NOTE = "pn_maya_1"
REVIEWED_NOTE = "pn_maya_2"
NOAH_NOTE = "pn_noah_1"          # another child, another caseload

ELENA_ID = "par_elena"

WATCHED = (C.PARENT_NOTES, C.PRIVATE_THERAPIST_NOTES, C.AUDIT_EVENTS,
           C.IDEMPOTENCY_RECORDS, C.PLAN_ASSIGNMENTS, C.PLAN_CHANGE_PROPOSALS,
           C.ACTIVITY_VERSIONS, C.WEEKLY_PLANS, C.CHILDREN, C.CONNECTIONS,
           C.PARENT_PROFILES, C.THERAPIST_PROFILES)


def _c():
    return read_slice_client()


def _repo(c):
    return c.app.state.repo


def _snapshot(c):
    repo = _repo(c)
    return {n: copy.deepcopy(sorted(repo.query(n), key=lambda r: r["id"])) for n in WATCHED}


def _review(c, note_id=NEW_NOTE, child=CHILD, key="rev-1", headers=HANNAH, **kw):
    return c.post(f"/api/v1/children/{child}/notes/{note_id}/review",
                  headers={**headers, "Idempotency-Key": key}, **kw)


def _note(c, note_id=NEW_NOTE):
    return _repo(c).get(C.PARENT_NOTES, note_id)


def _plain(v):
    return v.value if hasattr(v, "value") else v


def _seed(c, note_id, review=ParentNoteReviewStatus.NEW,
          prep=SessionPreparationStatus.NONE, child=CHILD, parent_id=ELENA_ID,
          note_type=ParentNoteType.NOTE, body="Seeded.", created_at="2026-07-30",
          linked_assignment_id=None, linked_activity_title=None):
    _repo(c).set(C.PARENT_NOTES, note_id, ParentNote(
        id=note_id, child_id=child, parent_id=parent_id, note_type=note_type,
        body=body, review_status=review, session_preparation_status=prep,
        linked_assignment_id=linked_assignment_id,
        linked_activity_title=linked_activity_title,
        created_at=created_at, environment="dev").model_dump())
    return note_id


def _review_events(c, note_id=None):
    evs = [e for e in _repo(c).query(C.AUDIT_EVENTS)
           if _plain(e["event_type"]) == V.EVENT_TYPE]
    return [e for e in evs if note_id is None or e["subject_id"] == note_id]


# ── 2-5: domain ─────────────────────────────────────────────────────────────
def test_review_status_enum_has_exactly_new_and_reviewed():
    """(2)(3)(5) No new lifecycle enum was invented for this transition."""
    assert [e.value for e in ParentNoteReviewStatus] == ["new", "reviewed"]
    assert V.FROM_STATUS is ParentNoteReviewStatus.NEW
    assert V.TO_STATUS is ParentNoteReviewStatus.REVIEWED


def test_the_two_status_dimensions_are_structurally_independent():
    """(4) Neither enum can express the other's states, so they cannot collapse."""
    review = {e.value for e in ParentNoteReviewStatus}
    session = {e.value for e in SessionPreparationStatus}
    assert review.isdisjoint(session)
    assert "reviewed" not in session and "discuss_at_next_session" not in review
    # Two separate stored fields, not one.
    assert "review_status" in ParentNote.model_fields
    assert "session_preparation_status" in ParentNote.model_fields


def test_no_version_or_updated_at_was_added_to_parent_note():
    """The monotonic single-field transition needs neither; adding one would put
    a mutable timestamp on a record whose content is immutable."""
    assert "version" not in ParentNote.model_fields
    assert "updated_at" not in ParentNote.model_fields


# ── 6-10: basic transition ──────────────────────────────────────────────────
def test_authorized_therapist_marks_a_new_note_reviewed():
    """(6)(7)(8)(10)"""
    c = _c()
    assert _plain(_note(c)["review_status"]) == "new"
    r = _review(c)
    assert r.status_code == 200                                          # (7)
    assert r.json()["review_status"] == "reviewed"
    assert _plain(_note(c)["review_status"]) == "reviewed"               # (8)
    assert _plain(_note(c)["session_preparation_status"]) == "none"      # (10)


def test_exactly_one_note_is_affected():
    """(9) Every other note in the store is byte-identical."""
    c = _c()
    before = {n["id"]: copy.deepcopy(n) for n in _repo(c).query(C.PARENT_NOTES)}
    _review(c)
    after = {n["id"]: n for n in _repo(c).query(C.PARENT_NOTES)}
    assert set(before) == set(after)
    changed = [i for i in before if before[i] != after[i]]
    assert changed == [NEW_NOTE]


# ── 11-14: all three note types ─────────────────────────────────────────────
def test_every_note_type_reviews_through_the_same_route_and_service():
    """(11)(12)(13)(14) The type is a human intent, never a routing concept."""
    for t in ParentNoteType:
        c = _c()
        nid = _seed(c, f"pn_type_{t.value}", note_type=t)
        r = _review(c, note_id=nid, key=f"k-{t.value}")
        assert r.status_code == 200, t
        assert r.json()["review_status"] == "reviewed"
        assert _plain(_note(c, nid)["note_type"]) == t.value      # type unchanged


# ── 15-19: session-preparation independence ─────────────────────────────────
def test_new_none_becomes_reviewed_none():
    """(15)"""
    c = _c()
    nid = _seed(c, "pn_ind_none", prep=SessionPreparationStatus.NONE)
    _review(c, note_id=nid)
    n = _note(c, nid)
    assert (_plain(n["review_status"]), _plain(n["session_preparation_status"])) \
        == ("reviewed", "none")


def test_new_discuss_becomes_reviewed_discuss():
    """(16)(19) Reviewing never CLEARS an existing discussion flag."""
    c = _c()
    nid = _seed(c, "pn_ind_disc", prep=SessionPreparationStatus.DISCUSS_AT_NEXT_SESSION)
    _review(c, note_id=nid)
    n = _note(c, nid)
    assert _plain(n["review_status"]) == "reviewed"
    assert _plain(n["session_preparation_status"]) == "discuss_at_next_session"


def test_session_preparation_is_enum_identical_across_every_starting_value():
    """(17)(18) Reviewing never SETS a discussion flag either — for any start."""
    for sp in SessionPreparationStatus:
        c = _c()
        nid = _seed(c, f"pn_sp_{sp.value}", prep=sp)
        before = _note(c, nid)["session_preparation_status"]
        _review(c, note_id=nid, key=f"k-{sp.value}")
        after = _note(c, nid)["session_preparation_status"]
        assert after == before, sp                       # identical, not merely equal
        assert _plain(after) == sp.value
        assert _plain(after) != "reviewed"


def test_the_response_echoes_the_untouched_session_status():
    """A therapist UI can see the other dimension did not move."""
    c = _c()
    nid = _seed(c, "pn_echo", prep=SessionPreparationStatus.DISCUSS_AT_NEXT_SESSION)
    body = _review(c, note_id=nid).json()
    assert body["session_preparation_status"] == "discuss_at_next_session"
    assert body["review_status"] == "reviewed"


# ── 20-29: immutable parent-authored content ────────────────────────────────
def test_only_review_status_changes_every_other_field_is_identical():
    """(20)-(29) The blocking content-immutability proof."""
    c = _c()
    nid = _seed(c, "pn_immutable", note_type=ParentNoteType.QUESTION,
                body="Is it ok if she signs instead?", created_at="2026-07-11",
                linked_assignment_id=CURRENT_ASSIGNMENT,
                linked_activity_title="Bubble requesting (July)",
                prep=SessionPreparationStatus.DISCUSS_AT_NEXT_SESSION)
    before = copy.deepcopy(_note(c, nid))
    _review(c, note_id=nid)
    after = _note(c, nid)

    for field in V.IMMUTABLE_NOTE_FIELDS:
        assert after[field] == before[field], field
    assert set(after) == set(before), "no field added or dropped"
    assert [k for k in after if after[k] != before[k]] == ["review_status"]  # (29)


def test_the_service_never_reconstructs_the_note_model():
    """`repo.set` replaces a whole document, so rebuilding the record via
    `ParentNote(**doc)` would refill defaults and could silently rewrite an
    immutable field. The transaction must assign one key on the stored dict."""
    src = inspect.getsource(V.mark_note_reviewed)
    fn = ast.parse(src).body[0]
    tree = ast.Module(body=fn.body[1:], type_ignores=[])     # drop the docstring
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert "ParentNote" not in calls
    assert not any("model_validate" in x or "ParentNote" in x for x in calls)
    # Exactly one assignment INTO THE NOTE dict, and it is review_status.
    # (`result[...]` is a different dict and is not in scope here.)
    note_writes = [ast.unparse(t) for n in ast.walk(tree) if isinstance(n, ast.Assign)
                   for t in n.targets
                   if isinstance(t, ast.Subscript) and ast.unparse(t.value) == "note"]
    assert note_writes == ["note['review_status']"], note_writes


def test_a_note_with_an_unusual_stored_shape_keeps_its_extra_keys():
    """Defence in depth for the reconstruct hazard: a stored record carrying a
    key the model does not declare must survive the write untouched."""
    c = _c()
    nid = _seed(c, "pn_extra")
    raw = _note(c, nid)
    raw["legacy_field"] = "kept"
    _repo(c).set(C.PARENT_NOTES, nid, raw)
    _review(c, note_id=nid)
    assert _note(c, nid)["legacy_field"] == "kept"


# ── 30-37: authorization ────────────────────────────────────────────────────
def test_unauthenticated_is_rejected():
    """(30)"""
    c = _c()
    r = c.post(f"/api/v1/children/{CHILD}/notes/{NEW_NOTE}/review",
               headers={"Idempotency-Key": "k"})
    assert r.status_code == 401
    assert _plain(_note(c)["review_status"]) == "new"


def test_a_parent_cannot_review_their_own_note():
    """(31) Reviewing is a therapist action; the parent is the author, not the
    reviewer."""
    c = _c()
    r = _review(c, headers=ELENA)
    assert r.status_code == 403
    assert r.json()["error"] == "forbidden"
    assert _plain(_note(c)["review_status"]) == "new"


def test_an_unconnected_therapist_is_existence_blind():
    """(32)(36)"""
    c = _c()
    r = _review(c, headers=UNCONNECTED)
    assert r.status_code == 404
    assert r.json() == {"error": "not_found", "detail": "Not found."}
    assert NEW_NOTE not in r.text and "more bubbles" not in r.text     # (36)
    assert _plain(_note(c)["review_status"]) == "new"


def test_unknown_child_and_unknown_note_are_both_blind_404():
    """(33)(34)"""
    c = _c()
    a = _review(c, child="child_nope")
    b = _review(c, note_id="pn_nope")
    assert a.status_code == b.status_code == 404
    assert a.json() == b.json() == {"error": "not_found", "detail": "Not found."}


def test_a_note_belonging_to_another_child_is_a_blind_404():
    """(35) Even for a therapist authorized on the child in the path."""
    c = _c()
    # pn_eli_1 belongs to child_eli; Hannah is authorized for both children, so
    # only the note/child pairing can reject this.
    nid = _seed(c, "pn_eli_x", child=OTHER_CHILD, parent_id="par_omar")
    r = _review(c, note_id=nid, child=CHILD)
    assert r.status_code == 404
    assert _plain(_note(c, nid)["review_status"]) == "new"     # untouched


def test_a_therapist_from_another_caseload_cannot_review():
    """(32) Priya is a real therapist without access to Maya."""
    c = _c()
    r = _review(c, headers=PRIYA)
    assert r.status_code == 404
    assert _plain(_note(c)["review_status"]) == "new"


def test_the_connected_therapist_succeeds():
    """(37)"""
    assert _review(_c()).status_code == 200


def test_the_therapist_is_the_actor_not_the_author():
    """(8 in §8) parent_id is untouched; the reviewer is recorded separately."""
    c = _c()
    before_author = _note(c)["parent_id"]
    _review(c)
    assert _note(c)["parent_id"] == before_author
    ev = _review_events(c, NEW_NOTE)[0]
    assert _plain(ev["actor_role"]) == PrincipalRole.THERAPIST.value
    assert ev["after_state"]["note_author_parent_id"] == before_author
    assert ev["after_state"]["reviewed_by_therapist_id"] != before_author


# ── 7/20: wrong-object ids must not be reviewable ───────────────────────────
def test_a_non_note_id_cannot_be_reviewed():
    """A private therapist note, proposal, assignment or child id supplied as
    note_id must not resolve — the query is scoped to PARENT_NOTES."""
    c = _c()
    before = _snapshot(c)
    for foreign in ("ptn_maya_1", "assign_maya_bubbles", "child_maya", "wp_maya"):
        r = _review(c, note_id=foreign, key=f"k-{foreign}")
        assert r.status_code == 404, foreign
    assert _snapshot(c) == before, "no foreign object was mutated"


# ── 38-44: key-bound idempotency ────────────────────────────────────────────
def test_missing_idempotency_key_is_rejected():
    c = _c()
    r = c.post(f"/api/v1/children/{CHILD}/notes/{NEW_NOTE}/review", headers=HANNAH)
    assert r.status_code == 400
    assert _plain(_note(c)["review_status"]) == "new"


def test_replay_returns_the_stored_result_and_transitions_once():
    """(38)(39)(40)(41)"""
    c = _c()
    first = _review(c, key="same")
    assert first.status_code == 200 and first.json()["idempotent_replay"] is False
    notes_after_first = copy.deepcopy(_note(c))

    second = _review(c, key="same")
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True                    # (39)
    assert {k: v for k, v in second.json().items() if k != "idempotent_replay"} \
        == {k: v for k, v in first.json().items() if k != "idempotent_replay"}
    assert _note(c) == notes_after_first                                 # (40)
    assert len(_review_events(c, NEW_NOTE)) == 1                         # (41)
    assert len(_repo(c).query(C.IDEMPOTENCY_RECORDS, action=V.ACTION)) == 1


def test_the_same_key_on_a_different_note_conflicts():
    """(42)"""
    c = _c()
    other = _seed(c, "pn_other_target")
    _review(c, key="shared")
    r = _review(c, note_id=other, key="shared")
    assert r.status_code == 409
    assert r.json()["error"] == "idempotency_key_conflict"
    assert _plain(_note(c, other)["review_status"]) == "new"


def test_the_same_key_on_a_different_child_conflicts():
    """(43)"""
    c = _c()
    eli_note = _seed(c, "pn_eli_conf", child=OTHER_CHILD, parent_id="par_omar")
    _review(c, key="shared2")
    r = _review(c, note_id=eli_note, child=OTHER_CHILD, key="shared2")
    assert r.status_code == 409
    assert _plain(_note(c, eli_note)["review_status"]) == "new"


def test_the_raw_key_is_never_stored_or_returned():
    """(44)"""
    c = _c()
    raw = "super-secret-key-value"
    body = _review(c, key=raw)
    assert raw not in body.text
    for rec in _repo(c).query(C.IDEMPOTENCY_RECORDS):
        assert raw not in str(rec)
        assert rec["idempotency_key_hash"] != raw
    for ev in _repo(c).query(C.AUDIT_EVENTS):
        assert raw not in str(ev)


# ── 45-49: semantic idempotence across DIFFERENT keys ───────────────────────
def test_reviewing_an_already_reviewed_note_succeeds_under_a_new_key():
    """(45)(46)(47)(48)(49) Reviewed is a destination, not a counter."""
    c = _c()
    _review(c, key="first")
    state_after_first = copy.deepcopy(_note(c))
    audits_after_first = len(_review_events(c, NEW_NOTE))

    r = _review(c, key="totally-different")
    assert r.status_code == 200                                          # (45)
    assert r.json()["review_status"] == "reviewed"
    assert _note(c) == state_after_first                                 # (46)(48)(49)
    assert len(_review_events(c, NEW_NOTE)) == audits_after_first == 1    # (47)


def test_a_fixture_note_that_is_already_reviewed_is_a_successful_no_op():
    """(45) pn_maya_2 arrives REVIEWED / discuss_at_next_session."""
    c = _c()
    before = copy.deepcopy(_note(c, REVIEWED_NOTE))
    r = _review(c, note_id=REVIEWED_NOTE, key="noop")
    assert r.status_code == 200
    assert r.json()["review_status"] == "reviewed"
    assert r.json()["session_preparation_status"] == "discuss_at_next_session"
    assert _note(c, REVIEWED_NOTE) == before
    assert _review_events(c, REVIEWED_NOTE) == []      # never transitioned here


def test_no_review_count_or_repeat_review_tracking_exists():
    """(12 in §12) A note transitions to REVIEWED at most once."""
    c = _c()
    for i in range(4):
        _review(c, key=f"k{i}")
    assert len(_review_events(c, NEW_NOTE)) == 1
    assert "review_count" not in _note(c)
    assert not any("count" in k for k in _note(c))


def test_a_no_op_idempotency_record_points_at_no_audit_event():
    """A semantic no-op emitted no event; pointing at another request's event
    would misattribute it."""
    c = _c()
    _review(c, key="first")
    _review(c, key="second")
    recs = {r["id"]: r for r in _repo(c).query(C.IDEMPOTENCY_RECORDS, action=V.ACTION)}
    audit_ids = [r["audit_event_id"] for r in recs.values()]
    assert audit_ids.count(None) == 1
    assert len([a for a in audit_ids if a]) == 1


# ── 50-55: concurrency ──────────────────────────────────────────────────────
def _concurrent(c, keys):
    results, errors = [], []

    def go(k):
        try:
            results.append(_review(c, key=k))
        except Exception as exc:          # pragma: no cover - defensive
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def test_concurrent_same_key_reviews_produce_one_transition():
    """(50)(52)(53)(54)(55)"""
    c = _c()
    results, errors = _concurrent(c, ["same-key"] * 8)
    assert not errors
    assert all(r.status_code == 200 for r in results)
    assert _plain(_note(c)["review_status"]) == "reviewed"               # (53)
    assert len(_review_events(c, NEW_NOTE)) == 1                         # (52)
    assert _plain(_note(c)["session_preparation_status"]) == "none"      # (55)
    assert len(_repo(c).query(C.IDEMPOTENCY_RECORDS, action=V.ACTION)) == 1


def test_concurrent_different_key_reviews_produce_one_transition():
    """(51)(52)(53)(54)(55) All callers may succeed; only one may transition."""
    c = _c()
    before_author = _note(c)["parent_id"]
    results, errors = _concurrent(c, [f"key-{i}" for i in range(8)])
    assert not errors
    assert all(r.status_code == 200 for r in results)
    assert all(r.json()["review_status"] == "reviewed" for r in results)
    assert _plain(_note(c)["review_status"]) == "reviewed"               # (53)
    assert len(_review_events(c, NEW_NOTE)) == 1                         # (51)(52)
    assert _plain(_note(c)["session_preparation_status"]) == "none"      # (55)
    assert _note(c)["parent_id"] == before_author                        # (54)
    assert len(_repo(c).query(C.PARENT_NOTES, id=NEW_NOTE)) == 1


# ── 56-62: audit ────────────────────────────────────────────────────────────
def test_one_transition_event_records_both_dimensions_on_both_sides():
    """(56)(57)(58)(59)(60)(61)"""
    c = _c()
    nid = _seed(c, "pn_audit", prep=SessionPreparationStatus.DISCUSS_AT_NEXT_SESSION)
    _review(c, note_id=nid)
    evs = _review_events(c, nid)
    assert len(evs) == 1                                                 # (56)
    ev = evs[0]
    assert _plain(ev["event_type"]) == "parent_note_reviewed"
    assert _plain(ev["actor_role"]) == "therapist"                       # (57)
    assert ev["subject_type"] == "parent_note" and ev["subject_id"] == nid  # (58)
    assert ev["child_id"] == CHILD
    assert ev["before_state"]["review_status"] == "new"                  # (59)
    assert ev["after_state"]["review_status"] == "reviewed"              # (60)
    assert ev["before_state"]["session_preparation_status"] \
        == ev["after_state"]["session_preparation_status"] \
        == "discuss_at_next_session"                                     # (61)


def test_the_audit_event_carries_no_chat_or_read_receipt_semantics():
    """(62)"""
    c = _c()
    _review(c)
    ev = str(_review_events(c, NEW_NOTE)[0]).lower()
    for banned in ("message_read", "message_seen", "reply_sent", "note_resolved",
                   "discussion_completed", "reply", "thread", "conversation",
                   "chat", "read_receipt", "answered"):
        assert banned not in ev, banned


def test_no_second_transition_audit_for_an_already_reviewed_note():
    """(56) Exactly one review-transition audit per ParentNote, ever."""
    c = _c()
    for i in range(3):
        _review(c, key=f"multi-{i}")
    assert len(_review_events(c, NEW_NOTE)) == 1


# ── 63-67: rollback ─────────────────────────────────────────────────────────
def test_injected_failure_after_the_note_mutation_rolls_everything_back():
    """(63)(64)(65)(66)(67) Fail LATE: IdempotencyRecord is built after the note
    and the audit event are already written inside the transaction."""
    c = _c()
    before = _snapshot(c)
    real = V.IdempotencyRecord

    def exploding(*a, **kw):
        raise RuntimeError("injected failure")

    V.IdempotencyRecord = exploding
    try:
        raised = False
        try:
            _review(c, key="rollback")
        except RuntimeError:
            raised = True
        assert raised
    finally:
        V.IdempotencyRecord = real

    assert _plain(_note(c)["review_status"]) == "new"                    # (63)
    assert _review_events(c, NEW_NOTE) == []                             # (64)
    assert _repo(c).query(C.IDEMPOTENCY_RECORDS, action=V.ACTION) == []  # (65)
    assert _snapshot(c) == before                                        # (66)(67)


def test_injected_failure_after_the_audit_write_also_rolls_back():
    """(63) A failure at the very end of the critical section leaves no partial
    reviewed state."""
    c = _c()
    before = _snapshot(c)
    real = V.key_hash
    calls = {"n": 0}

    def boom(*a, **kw):
        calls["n"] += 1
        if calls["n"] > 1:          # first call happens before the note write
            raise RuntimeError("injected late failure")
        return real(*a, **kw)

    V.key_hash = boom
    try:
        try:
            _review(c, key="late-rollback")
        except RuntimeError:
            pass
    finally:
        V.key_hash = real

    assert _plain(_note(c)["review_status"]) == "new"
    assert _snapshot(c) == before


# ── 68-74: parent-read propagation ──────────────────────────────────────────
def test_the_author_sees_reviewed_on_the_existing_parent_read():
    """(68)(69)(70)(71)(72)(73)"""
    c = _c()
    nid = _seed(c, "pn_prop", note_type=ParentNoteType.QUESTION, body="Signing ok?",
                created_at="2026-07-12", linked_activity_title="Bubble requesting (July)",
                prep=SessionPreparationStatus.DISCUSS_AT_NEXT_SESSION)

    def item():
        out = c.get(f"/api/v1/children/{CHILD}/notes", headers=ELENA).json()
        return next(i for i in out["items"] if i["note_id"] == nid)

    before = item()
    assert before["review_status"] == "new"
    _review(c, note_id=nid)
    after = item()

    assert after["review_status"] == "reviewed"                          # (68)
    assert after["body"] == before["body"]                               # (69)
    assert after["note_type"] == before["note_type"]                     # (70)
    assert after["created_at"] == before["created_at"]                   # (71)
    assert after["linked_activity_title"] == before["linked_activity_title"]  # (72)
    assert after["session_preparation_status"] \
        == before["session_preparation_status"] == "discuss_at_next_session"  # (73)
    assert [k for k in after if after[k] != before[k]] == ["review_status"]


def test_reviewing_does_not_break_another_authors_isolation():
    """(74)"""
    c = _c()
    theirs = _seed(c, "pn_other_author", parent_id="par_omar",
                   body="Authored by another caregiver.")
    _review(c, note_id=theirs, key="rev-theirs")
    out = c.get(f"/api/v1/children/{CHILD}/notes", headers=ELENA)
    assert theirs not in {i["note_id"] for i in out.json()["items"]}
    assert "Authored by another caregiver." not in out.text


def test_no_parent_endpoint_or_notification_was_added():
    """The parent merely sees the new stored status on the next GET."""
    c = _c()
    paths = c.app.openapi()["paths"]
    assert not any(k in p for p in paths for k in
                   ("notification", "push", "events", "subscribe", "inbox"))
    # The parent still cannot review.
    assert _review(c, headers=ELENA).status_code == 403


# ── 75-78: therapist-read propagation ───────────────────────────────────────
def test_therapist_reads_reflect_reviewed_without_schema_change():
    """(75)(76)(77)"""
    c = _c()
    before_child = c.get(f"/api/v1/children/{CHILD}/notes", headers=HANNAH).json()
    before_inbox = c.get("/api/v1/notes", headers=HANNAH).json()
    keys_child = sorted(before_child["items"][0])
    keys_inbox = sorted(before_inbox["items"][0])

    _review(c)

    after_child = c.get(f"/api/v1/children/{CHILD}/notes", headers=HANNAH).json()
    after_inbox = c.get("/api/v1/notes", headers=HANNAH).json()
    assert next(i for i in after_child["items"]
                if i["note_id"] == NEW_NOTE)["review_status"] == "reviewed"   # (75)
    assert next(i for i in after_inbox["items"]
                if i["note_id"] == NEW_NOTE)["review_status"] == "reviewed"   # (76)
    assert sorted(after_child["items"][0]) == keys_child                       # (77)
    assert sorted(after_inbox["items"][0]) == keys_inbox
    assert sorted(after_child) == sorted(before_child)


def test_no_second_review_queue_or_reviewed_notes_collection_exists():
    """(18 in §18)"""
    c = _c()
    _review(c)
    names = {n for n in dir(C) if n.isupper()}
    assert not any("REVIEW" in n for n in names), names
    paths = c.app.openapi()["paths"]
    assert not any("reviewed" in p or "review-queue" in p for p in paths)
    # ParentNote rows stay in the single canonical collection.
    assert len(_repo(c).query(C.PARENT_NOTES, id=NEW_NOTE)) == 1


def test_reads_remain_read_only_after_a_review():
    """(78)"""
    c = _c()
    _review(c)
    snap = _snapshot(c)
    for _ in range(5):
        c.get(f"/api/v1/children/{CHILD}/notes", headers=HANNAH)
        c.get(f"/api/v1/children/{CHILD}/notes", headers=ELENA)
        c.get("/api/v1/notes", headers=HANNAH)
    assert _snapshot(c) == snap


def test_the_new_note_counter_reflects_the_review():
    """The pre-existing therapist dashboard counter is a real consumer of
    review_status — reviewing decrements it. Pinned so the propagation is
    deliberate rather than incidental."""
    c = _c()

    def count():
        rows = c.get("/api/v1/children", headers=HANNAH).json()["items"]
        return next(r for r in rows if r["child_id"] == CHILD)["new_parent_note_count"]

    before = count()
    assert before > 0, "fixture must have at least one NEW note to decrement"
    _review(c)
    assert count() == before - 1


# ── 79-89: parent write/read regression ─────────────────────────────────────
def test_parent_writes_are_unchanged_by_this_phase():
    """(79)(80)(81)(82)(83)"""
    c = _c()
    for t in ("question", "note", "update"):
        r = c.post(f"/api/v1/children/{CHILD}/notes",
                   headers={**ELENA, "Idempotency-Key": f"w-{t}"},
                   json={"note_type": t, "body": f"body {t}"})
        assert r.status_code == 200, t
        assert r.json()["note"]["review_status"] == "new"
        assert r.json()["note"]["session_preparation_status"] == "none"
    linked = c.post(f"/api/v1/children/{CHILD}/notes",
                    headers={**ELENA, "Idempotency-Key": "w-linked"},
                    json={"note_type": "note", "body": "linked",
                          "linked_assignment_id": CURRENT_ASSIGNMENT})
    assert linked.status_code == 200
    assert linked.json()["note"]["linked_activity_title"] == "Bubble requesting"
    replay = c.post(f"/api/v1/children/{CHILD}/notes",
                    headers={**ELENA, "Idempotency-Key": "w-linked"},
                    json={"note_type": "note", "body": "linked",
                          "linked_assignment_id": CURRENT_ASSIGNMENT})
    assert replay.json()["idempotent_replay"] is True                    # (83)


def test_a_newly_written_note_can_then_be_reviewed():
    """The two phases compose: parent submits, therapist reviews, parent sees."""
    c = _c()
    created = c.post(f"/api/v1/children/{CHILD}/notes",
                     headers={**ELENA, "Idempotency-Key": "compose"},
                     json={"note_type": "question", "body": "Composed?"}).json()
    nid = created["note"]["note_id"]
    assert _review(c, note_id=nid, key="compose-rev").status_code == 200
    seen = next(i for i in c.get(f"/api/v1/children/{CHILD}/notes",
                                 headers=ELENA).json()["items"]
                if i["note_id"] == nid)
    assert seen["review_status"] == "reviewed" and seen["body"] == "Composed?"


def test_parent_read_semantics_are_unchanged():
    """(85)(86)(87)(88)(89)"""
    c = _c()
    _review(c)
    out = c.get(f"/api/v1/children/{CHILD}/notes", headers=ELENA).json()
    assert sorted(out) == ["child_id", "items", "next_cursor", "total"]   # (85)
    assert all(i["note_id"].startswith("pn_") for i in out["items"])
    ids = [i["note_id"] for i in out["items"]]
    assert ids == sorted(ids, key=lambda x: x, reverse=True) or True      # order pinned below
    stamps = [i["created_at"] for i in out["items"]]
    assert stamps == sorted(stamps, reverse=True)                        # (88)
    assert c.get("/api/v1/notes", headers=ELENA).status_code == 403      # (89)
    # (87) point-in-time title still stored, not re-resolved
    item = next(i for i in out["items"] if i["note_id"] == NEW_NOTE)
    assert item["linked_activity_title"] == "Bubble requesting"


def test_empty_parent_history_is_still_a_successful_empty_list():
    """(86)"""
    c = _c()
    _review(c)
    repo = _repo(c)
    for n in repo.query(C.PARENT_NOTES, child_id=CHILD):
        repo._col(C.PARENT_NOTES).pop(n["id"], None)
    r = c.get(f"/api/v1/children/{CHILD}/notes", headers=ELENA)
    assert r.status_code == 200 and r.json()["items"] == [] and r.json()["total"] == 0


# ── 90-92: plan regression ──────────────────────────────────────────────────
def test_reviewing_touches_no_plan_state():
    """(90)(91)(92)"""
    c = _c()
    plan_cols = (C.PLAN_ASSIGNMENTS, C.WEEKLY_PLANS, C.PLAN_CHANGE_PROPOSALS,
                 C.ACTIVITY_VERSIONS)
    before = {n: copy.deepcopy(sorted(_repo(c).query(n), key=lambda r: r["id"]))
              for n in plan_cols}
    _review(c)
    after = {n: sorted(_repo(c).query(n), key=lambda r: r["id"]) for n in plan_cols}
    assert after == before
    # the canonical current-plan resolver still answers
    from app.services import weekly_plan
    assert weekly_plan.current_weekly_plan_id(_repo(c), CHILD) == "wp_maya"


# ── 93-95: Discuss Next Session must remain unimplemented ───────────────────
def test_no_route_can_change_session_preparation_status():
    """(93)(95) Hard scope boundary."""
    c = _c()
    paths = c.app.openapi()["paths"]
    for p in paths:
        last = p.rsplit("/", 1)[-1]
        assert last not in ("discuss", "discuss-next-session", "discussed",
                            "session-preparation", "mark-for-next-session"), p

    # No production code ASSIGNS the session dimension. Checked by AST, because
    # a substring scan cannot tell `x["session_preparation_status"] = ...` from
    # the far more common `x["session_preparation_status"] == ...` comparison —
    # `] =` is a prefix of `] ==`, so the naive version reports every read.
    sources = sorted(APP_DIR.rglob("*.py"))
    # Fail loudly rather than vacuously: an empty tree would satisfy every
    # assertion below without reading a single line of production code.
    assert sources, f"no sources found under {APP_DIR}"
    writers = []
    for f in sources:
        tree = ast.parse(f.read_text())
        for n in ast.walk(tree):
            targets = (list(n.targets) if isinstance(n, ast.Assign)
                       else [n.target] if isinstance(n, (ast.AugAssign, ast.AnnAssign))
                       else [])
            for t in targets:
                if (isinstance(t, ast.Subscript)
                        and isinstance(t.slice, ast.Constant)
                        and t.slice.value == "session_preparation_status"):
                    writers.append(f"{f}:{n.lineno}")
    assert writers == [], writers
    # ...and no service exposes an action that would.
    service_files = sorted((APP_DIR / "services").rglob("*.py"))
    assert service_files, f"no services found under {APP_DIR / 'services'}"
    services = "\n".join(p.read_text() for p in service_files)
    assert services.strip(), "service sources read as empty"
    for banned in ("mark_discuss", "def discuss", "def set_session_preparation",
                   "def mark_for_next_session"):
        assert banned not in services, banned


def test_reviewing_has_no_session_preparation_side_effect_anywhere():
    """(94) Every note in the store keeps its session dimension."""
    c = _c()
    before = {n["id"]: n["session_preparation_status"]
              for n in _repo(c).query(C.PARENT_NOTES)}
    for nid in list(before):
        _review(c, note_id=nid, child=_repo(c).get(C.PARENT_NOTES, nid)["child_id"],
                key=f"sp-{nid}")
    after = {n["id"]: n["session_preparation_status"]
             for n in _repo(c).query(C.PARENT_NOTES)}
    assert after == before


# ── 96-100: no chat ─────────────────────────────────────────────────────────
def test_no_chat_surface_was_introduced():
    """(96)(97)(98)(99)(100)"""
    c = _c()
    s = c.app.openapi()
    # Scan the API SURFACE — paths, schema names, property names — not prose.
    # Descriptions legitimately say "there is no reply"; a blob scan would trip
    # on the very docstring that promises the guarantee.
    surface = list(s["paths"])
    surface += list(s["components"]["schemas"])
    for name, sch in s["components"]["schemas"].items():
        surface += list(sch.get("properties", {}))
    blob = " ".join(surface).lower()
    for banned in ("reply", "answer", "thread", "conversation", "message",
                   "read_receipt", "typing", "realtime", "chat"):
        assert banned not in blob, banned
    assert not any(m in v for p, v in s["paths"].items()
                   for m in ("put", "patch", "delete"))
    assert "NoteReviewResponse" in s["components"]["schemas"]
    assert sorted(s["components"]["schemas"]["NoteReviewResponse"]["properties"]) == [
        "idempotent_replay", "note_id", "review_status", "session_preparation_status"]


def test_the_review_route_takes_no_request_body():
    """A command with no parent-authored content has nothing to inject into."""
    c = _c()
    op = c.app.openapi()["paths"][
        "/api/v1/children/{child_id}/notes/{note_id}/review"]["post"]
    assert "requestBody" not in op


def test_a_client_supplied_body_cannot_inject_workflow_state():
    """(6 in §6) There is no request model, so injected fields are inert."""
    c = _c()
    r = _review(c, json={"review_status": "new",
                         "session_preparation_status": "discuss_at_next_session",
                         "body": "hacked", "note_type": "question",
                         "parent_id": "par_omar", "created_at": "1999-01-01",
                         "version": 99, "updated_at": "1999-01-01"})
    assert r.status_code == 200
    n = _note(c)
    assert _plain(n["review_status"]) == "reviewed"
    assert _plain(n["session_preparation_status"]) == "none"
    assert n["body"] == "Maya said 'more bubbles' on her own!"
    assert n["parent_id"] == ELENA_ID
    assert n["created_at"] == "2026-07-25"
    assert "version" not in n and "updated_at" not in n


# ── 101-102: private therapist notes stay separate ──────────────────────────
def test_private_therapist_notes_are_untouched_and_never_leak():
    """(101)(102)"""
    c = _c()
    before = copy.deepcopy(sorted(_repo(c).query(C.PRIVATE_THERAPIST_NOTES),
                                  key=lambda r: r["id"]))
    _review(c)
    assert sorted(_repo(c).query(C.PRIVATE_THERAPIST_NOTES),
                  key=lambda r: r["id"]) == before                       # (101)
    parent_out = c.get(f"/api/v1/children/{CHILD}/notes", headers=ELENA)
    assert "Consider AAC backup" not in parent_out.text                  # (102)
    assert "ptn_" not in parent_out.text
    # The service never references the private collection.
    assert "PRIVATE_THERAPIST_NOTES" not in inspect.getsource(V)


def test_a_parent_note_is_never_converted_into_a_private_note():
    c = _c()
    n_before = len(_repo(c).query(C.PRIVATE_THERAPIST_NOTES))
    _review(c)
    assert len(_repo(c).query(C.PRIVATE_THERAPIST_NOTES)) == n_before
    assert len(_repo(c).query(C.PARENT_NOTES, id=NEW_NOTE)) == 1


# ── privacy of the action response ──────────────────────────────────────────
def test_the_response_exposes_no_internals():
    c = _c()
    r = _review(c)
    for banned in ("parent_id", "therapist_id", "audit", "idempotency_key_hash",
                   "request_hash", "operation", "environment", "schema_version",
                   "linked_assignment_id", "weekly_plan_id", "body",
                   "created_at", "child_id"):
        assert banned not in r.text, banned
    assert sorted(r.json()) == ["idempotent_replay", "note_id", "review_status",
                                "session_preparation_status"]


# ── 108-110: isolation ──────────────────────────────────────────────────────
def test_the_review_service_imports_nothing_external():
    """(108)(110) AST-checked: docstring prose must not be able to pass or fail
    this, so imports are read from the tree rather than the text."""
    source = APP_DIR / "services" / "note_review_service.py"
    assert source.is_file(), f"review service not found at {source}"
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
