"""Parent Question / Note / Update writes (Phase 1B.3A, fictional data).

One-way parent -> therapist. A parent submits an item into the child's shared
care workspace and the therapist reads it through the endpoint that already
exists. There is no reply, no thread, no conversation and no therapist write.

The two guarantees carrying most of the weight:

* `review_status` and `session_preparation_status` are SYSTEM-owned, start at
  `new` / `none`, and stay INDEPENDENT — a future Reviewed must not imply
  Discuss Next Session.
* A linked activity must be one of this child's CURRENT activities, and every
  other case is existence-blind.
"""

from __future__ import annotations

import ast
import copy
import inspect

from app.api import schemas as S
from app.domain.enums import (
    ParentNoteReviewStatus,
    ParentNoteType,
    SessionPreparationStatus,
)
from app.domain.read_models import ParentNote
from app.repository import collections as C
from app.services import parent_note_service as P
from tests.conftest import ELENA, HANNAH, OMAR, PRIYA, UNCONNECTED, read_slice_client

CHILD = "child_maya"
NOTES = f"/api/v1/children/{CHILD}/notes"
CURRENT_ASSIGNMENT = "assign_maya_bubbles"        # CURRENT, day 0
OTHER_CHILD_ASSIGNMENT = "assign_noah_turntake"   # belongs to child_noah

WATCHED = (C.PARENT_NOTES, C.PRIVATE_THERAPIST_NOTES, C.AUDIT_EVENTS,
           C.IDEMPOTENCY_RECORDS, C.PLAN_ASSIGNMENTS, C.PLAN_CHANGE_PROPOSALS,
           C.ACTIVITY_VERSIONS, C.WEEKLY_PLANS, C.CHILDREN, C.CONNECTIONS)


def _c():
    return read_slice_client()


def _repo(c):
    return c.app.state.repo


def _snapshot(c):
    repo = _repo(c)
    return {n: copy.deepcopy(sorted(repo.query(n), key=lambda r: r["id"])) for n in WATCHED}


def _post(c, note_type="question", body="Is signing ok?", linked=None,
          key="k1", headers=ELENA, child=CHILD, payload=None):
    h = dict(headers)
    if key is not None:
        h["Idempotency-Key"] = key
    if payload is None:
        payload = {"note_type": note_type, "body": body}
        if linked is not None:
            payload["linked_assignment_id"] = linked
    return c.post(f"/api/v1/children/{child}/notes", headers=h, json=payload)


def _stored(c, note_id):
    return _repo(c).query(C.PARENT_NOTES, id=note_id)[0]


def _therapist_notes(c, child=CHILD, headers=HANNAH):
    return c.get(f"/api/v1/children/{child}/notes", headers=headers)


# ── 2-6: domain contract ────────────────────────────────────────────────────
def test_existing_parent_note_model_and_enums_are_reused():
    """(2)(3) No new type, no aliases — the canonical enum values exactly."""
    assert [t.value for t in ParentNoteType] == ["question", "note", "update"]
    fields = set(ParentNote.model_fields)
    assert {"id", "child_id", "parent_id", "note_type", "body", "review_status",
            "session_preparation_status", "linked_assignment_id",
            "linked_activity_title", "created_at"} <= fields
    # The service writes THIS model, not a parallel one.
    assert "ParentNote(" in inspect.getsource(P)


def test_canonical_initial_statuses():
    """(4)(5) Defaults mean 'therapist has not acted', and the service uses them."""
    assert ParentNoteReviewStatus.NEW.value == "new"
    assert SessionPreparationStatus.NONE.value == "none"
    assert ParentNote.model_fields["review_status"].default is ParentNoteReviewStatus.NEW
    assert ParentNote.model_fields["session_preparation_status"].default is \
        SessionPreparationStatus.NONE
    assert P.INITIAL_REVIEW_STATUS is ParentNoteReviewStatus.NEW
    assert P.INITIAL_SESSION_PREPARATION_STATUS is SessionPreparationStatus.NONE


def test_the_two_status_dimensions_remain_independent():
    """(6) Two fields, two enums — never collapsed into one generic status."""
    assert set(ParentNoteReviewStatus) != set(SessionPreparationStatus)
    assert len(SessionPreparationStatus) == 3      # none | discuss | discussed
    assert len(ParentNoteReviewStatus) == 2        # new | reviewed
    # Independence is structural: a Reviewed value cannot express discussion.
    assert "reviewed" not in {s.value for s in SessionPreparationStatus}
    assert "discuss_at_next_session" not in {s.value for s in ParentNoteReviewStatus}
    # And the fixtures already exercise a note that is reviewed but NOT discussed
    # only in combination — proving the pair varies independently.
    c = _c()
    combos = {(n["review_status"], n["session_preparation_status"])
              for n in _repo(c).query(C.PARENT_NOTES)}
    assert len(combos) > 1


# ── 7-19: create Question / Note / Update ───────────────────────────────────
def test_parent_creates_a_question():
    """(7)(8)(9)(10)(11)(12)(13)"""
    c = _c()
    repo = _repo(c)
    before = len(repo.query(C.PARENT_NOTES))
    r = _post(c, note_type="question", body="Is signing ok instead of saying it?")
    assert r.status_code == 200                                          # (7)
    out = r.json()["note"]

    assert len(repo.query(C.PARENT_NOTES)) == before + 1                 # (8)
    stored = _stored(c, out["note_id"])
    assert stored["note_type"] == "question"                             # (9)
    assert stored["body"] == "Is signing ok instead of saying it?"       # (10)
    assert stored["child_id"] == CHILD                                   # (11)
    assert stored["parent_id"] == "par_elena"                            # (11)
    assert stored["review_status"] == "new"                              # (12)
    assert stored["session_preparation_status"] == "none"                # (13)
    assert stored["created_at"]


def test_parent_creates_a_note_and_an_update_by_the_same_path():
    """(14)(15)(16)(17)(18)(19) Type is intent, not routing."""
    c = _c()
    for note_type, text in (("note", "Maya tires after five minutes."),
                            ("update", "She said 'more bubbles' on her own!")):
        r = _post(c, note_type=note_type, body=text, key=f"k-{note_type}")
        assert r.status_code == 200, note_type
        stored = _stored(c, r.json()["note"]["note_id"])
        assert stored["note_type"] == note_type
        assert stored["body"] == text
        assert stored["review_status"] == "new"
        assert stored["session_preparation_status"] == "none"
    # All three live in ONE collection, reached by ONE route.
    created = [n for n in _repo(c).query(C.PARENT_NOTES, child_id=CHILD)
               if n["id"].startswith("pn_") and len(n["id"]) > 12]
    assert {n["note_type"] for n in created} >= {"note", "update"}


# ── 20-21: unlinked ─────────────────────────────────────────────────────────
def test_child_level_note_needs_no_assignment():
    """(20)(21)"""
    c = _c()
    r = _post(c, note_type="note", body="General context for the team.")
    assert r.status_code == 200
    stored = _stored(c, r.json()["note"]["note_id"])
    assert stored["linked_assignment_id"] is None                        # (21)
    assert stored["linked_activity_title"] is None
    assert r.json()["note"]["linked_activity_title"] is None


# ── 22-26: linked ───────────────────────────────────────────────────────────
def test_note_linked_to_a_current_assignment():
    """(22)(23)(24)(25)(26)"""
    c = _c()
    repo = _repo(c)
    before = _snapshot(c)
    r = _post(c, linked=CURRENT_ASSIGNMENT, body="This one went really well.")
    assert r.status_code == 200                                          # (22)

    stored = _stored(c, r.json()["note"]["note_id"])
    assert stored["linked_assignment_id"] == CURRENT_ASSIGNMENT          # (23)
    # The denormalized title is resolved at write time, or the therapist would
    # see a link with no name.
    assert stored["linked_activity_title"] == "Bubble requesting"
    assert r.json()["note"]["linked_activity_title"] == "Bubble requesting"

    for coll in (C.PLAN_ASSIGNMENTS, C.WEEKLY_PLANS, C.ACTIVITY_VERSIONS,
                 C.PLAN_CHANGE_PROPOSALS, C.PRIVATE_THERAPIST_NOTES):
        after = sorted(repo.query(coll), key=lambda x: x["id"])
        assert after == before[coll], f"{coll} changed"                  # (24)(25)(26)


# ── 27-31: bad link ─────────────────────────────────────────────────────────
def test_bad_assignment_links_are_existence_blind_and_write_nothing():
    """(27)(28)(29)(30)(31)"""
    blind = {"error": "not_found", "detail": "Not found."}
    cases = {
        "unknown": "assign_ghost",                       # (27)
        "another child's": OTHER_CHILD_ASSIGNMENT,       # (28)(29)
    }
    for label, assignment_id in cases.items():
        c = _c()
        before = _snapshot(c)
        r = _post(c, linked=assignment_id, key=f"bad-{label}")
        assert r.status_code == 404, label
        assert r.json() == blind, label
        assert _snapshot(c) == before, label                             # (31)


def test_non_current_assignment_link_fails_closed():
    """(30) A note must attach to an activity the family is working on NOW."""
    c = _c()
    repo = _repo(c)
    a = repo.query(C.PLAN_ASSIGNMENTS, id=CURRENT_ASSIGNMENT)[0]
    a["assignment_status"] = "replaced"
    repo.set(C.PLAN_ASSIGNMENTS, CURRENT_ASSIGNMENT, a)
    before = _snapshot(c)

    r = _post(c, linked=CURRENT_ASSIGNMENT, key="retired")
    assert r.status_code == 404
    assert r.json() == {"error": "not_found", "detail": "Not found."}
    assert _snapshot(c) == before


# ── 32-38: authorization ────────────────────────────────────────────────────
def test_authorization_is_existence_blind():
    """(32)(33)(34)(35)"""
    c = _c()
    blind = {"error": "not_found", "detail": "Not found."}

    r = _post(c, headers=OMAR, key="o")                                  # (32)
    assert r.status_code == 404 and r.json() == blind
    r = _post(c, child="child_ghost", key="u")                           # (33)
    assert r.status_code == 404 and r.json() == blind
    r = c.post(NOTES, headers={"Idempotency-Key": "n"},                  # (34)
               json={"note_type": "note", "body": "x"})
    assert r.status_code == 401
    for therapist in (HANNAH, PRIYA, UNCONNECTED):                       # (35)
        r = _post(c, headers=therapist, key=f"t-{therapist['Authorization']}")
        assert r.status_code == 403, therapist
        assert r.json()["error"] == "forbidden"


def test_non_active_connections_fail_closed():
    """(36)(37)(38) — Amara pending, Sana paused, Rue ended."""
    c = _c()
    for child in ("child_amara", "child_sana", "child_rue"):
        r = _post(c, child=child, key=f"c-{child}")
        assert r.status_code == 404, child
        assert r.json() == {"error": "not_found", "detail": "Not found."}


def test_missing_idempotency_key_is_rejected():
    r = _post(_c(), key=None)
    assert r.status_code == 400 and r.json()["error"] == "missing_idempotency_key"


# ── 39-45: validation ───────────────────────────────────────────────────────
def test_empty_and_whitespace_bodies_are_rejected():
    """(40)(41)"""
    for label, body in (("empty", ""), ("spaces", "   "), ("newline", "\n\t ")):
        c = _c()
        before = _snapshot(c)
        r = _post(c, body=body, key=f"e-{label}")
        assert r.status_code == 422, label
        assert r.json()["error"] == "invalid_request", label
        assert _snapshot(c) == before, label


def test_missing_body_or_note_type_is_rejected():
    """(39) A structurally incomplete request never reaches the service."""
    c = _c()
    for payload in ({"note_type": "note"}, {"body": "x"}, {}):
        r = _post(c, key="m", payload=payload)
        assert r.status_code == 422, payload


def test_invalid_note_type_is_rejected():
    """(42)"""
    for bad in ("message", "chat", "reply", "QUESTION", ""):
        c = _c()
        before = _snapshot(c)
        r = _post(c, note_type=bad, key=f"t-{bad}")
        assert r.status_code == 422, bad
        assert r.json()["error"] == "invalid_request", bad
        assert _snapshot(c) == before, bad


def test_client_cannot_set_system_owned_fields():
    """(43)(44)(45) Extra fields are ignored; the system's values always win."""
    c = _c()
    r = _post(c, key="inject", payload={
        "note_type": "question", "body": "Trying to inject.",
        "review_status": "reviewed",
        "session_preparation_status": "discuss_at_next_session",
        "id": "pn_attacker", "parent_id": "par_omar", "child_id": "child_eli",
        "created_at": "1999-01-01", "environment": "prod",
        "schema_version": "hacked", "linked_activity_title": "Fake title",
    })
    assert r.status_code == 200
    stored = _stored(c, r.json()["note"]["note_id"])
    assert stored["review_status"] == "new"                              # (43)
    assert stored["session_preparation_status"] == "none"                # (44)
    assert stored["id"] != "pn_attacker"                                 # (45)
    assert stored["parent_id"] == "par_elena"
    assert stored["child_id"] == CHILD
    assert stored["created_at"] != "1999-01-01"
    assert stored["environment"] == "dev"
    assert stored["linked_activity_title"] is None


def test_malformed_linked_assignment_id_fails_closed():
    """(45) A non-string or nonsense id must not 500."""
    c = _c()
    r = _post(c, key="bad-id", payload={
        "note_type": "note", "body": "x", "linked_assignment_id": 12345})
    assert r.status_code == 422
    r2 = _post(c, key="blank-id", payload={
        "note_type": "note", "body": "x", "linked_assignment_id": "   "})
    # A blank string means "no link", not a broken link.
    assert r2.status_code == 200
    assert _stored(c, r2.json()["note"]["note_id"])["linked_assignment_id"] is None


# ── 46-52: idempotency ──────────────────────────────────────────────────────
def _counts(c):
    repo = _repo(c)
    return {n: len(repo.query(n)) for n in
            (C.PARENT_NOTES, C.AUDIT_EVENTS, C.IDEMPOTENCY_RECORDS)}


def test_same_key_same_request_replays():
    """(46)(47)(48)(49)"""
    c = _c()
    first = _post(c, key="same", linked=CURRENT_ASSIGNMENT)
    assert first.status_code == 200 and first.json()["idempotent_replay"] is False
    counts = _counts(c)

    second = _post(c, key="same", linked=CURRENT_ASSIGNMENT)
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    assert second.json()["note"] == first.json()["note"]
    assert _counts(c) == counts                                          # (47)(48)(49)
    # Not a second timestamped duplicate.
    assert second.json()["note"]["created_at"] == first.json()["note"]["created_at"]


def test_same_key_changed_request_conflicts():
    """(50)(51)(52)"""
    variants = {
        "note_type": {"note_type": "update", "body": "Is signing ok?"},      # (50)
        "body": {"note_type": "question", "body": "Different text entirely."},  # (51)
        "link": {"note_type": "question", "body": "Is signing ok?",
                 "linked_assignment_id": CURRENT_ASSIGNMENT},                # (52)
    }
    for label, payload in variants.items():
        c = _c()
        assert _post(c, key="dup").status_code == 200, label
        counts = _counts(c)
        r = _post(c, key="dup", payload=payload)
        assert r.status_code == 409, label
        assert r.json()["error"] == "idempotency_key_conflict", label
        assert _counts(c) == counts, label


def test_different_keys_create_distinct_notes():
    """A parent may legitimately submit the same words twice."""
    c = _c()
    a = _post(c, key="one", body="Same words.")
    b = _post(c, key="two", body="Same words.")
    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["note"]["note_id"] != b.json()["note"]["note_id"]
    assert len([n for n in _repo(c).query(C.PARENT_NOTES) if n["body"] == "Same words."]) == 2


# ── 53-56: rollback ─────────────────────────────────────────────────────────
def test_injected_failure_rolls_everything_back(monkeypatch):
    """(53)(54)(55)(56)"""
    c = _c()
    before = _snapshot(c)
    # Fail LATE: IdempotencyRecord is constructed after the note and the audit
    # event are already written into the transaction, so this exercises a real
    # rollback rather than an early abort.
    real_record = P.IdempotencyRecord

    def exploding_record(*a, **kw):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(P, "IdempotencyRecord", exploding_record)
    try:
        raised = False
        try:
            _post(c, key="rollback", linked=CURRENT_ASSIGNMENT)
        except RuntimeError:
            raised = True
        assert raised
    finally:
        monkeypatch.setattr(P, "IdempotencyRecord", real_record)

    assert _snapshot(c) == before
    assert len(_repo(c).query(C.PARENT_NOTES)) == len(before[C.PARENT_NOTES])


# ── 57-63: therapist read integration ───────────────────────────────────────
def test_all_three_types_appear_in_the_existing_therapist_get():
    """(57)(58)(59)(60)(61)(62)"""
    c = _c()
    created = {}
    for note_type, text in (("question", "Is signing ok?"),
                            ("note", "Tires after five minutes."),
                            ("update", "Said 'more bubbles' unprompted!")):
        r = _post(c, note_type=note_type, body=text, key=f"t-{note_type}")
        created[note_type] = (r.json()["note"]["note_id"], text)

    listed = _therapist_notes(c)
    assert listed.status_code == 200                                     # (62)
    rows = {i["note_id"]: i for i in listed.json()["items"]}
    for note_type, (note_id, text) in created.items():
        row = rows[note_id]                                              # (57)(58)(59)
        assert row["note_type"] == note_type                             # (61)
        assert row["body"] == text                                       # (60)
        assert row["review_status"] == "new"
        assert row["session_preparation_status"] == "none"

    # Also reachable through the cross-child therapist inbox, unchanged.
    inbox = c.get("/api/v1/notes", headers=HANNAH).json()
    assert {i["note_id"] for i in inbox["items"]} >= set(rows)


def test_therapist_get_remains_read_only():
    """(63)"""
    c = _c()
    _post(c, key="ro")
    before = _snapshot(c)
    for _ in range(5):
        _therapist_notes(c)
        c.get("/api/v1/notes", headers=HANNAH)
        c.get(f"/api/v1/children/{CHILD}/next-session", headers=HANNAH)
    assert _snapshot(c) == before


def test_a_new_note_is_not_marked_for_the_next_session():
    """A fresh note has session_preparation_status none, so it must not appear."""
    c = _c()
    before = c.get(f"/api/v1/children/{CHILD}/next-session", headers=HANNAH).json()
    pid = _post(c, key="ns").json()["note"]["note_id"]
    after = c.get(f"/api/v1/children/{CHILD}/next-session", headers=HANNAH).json()
    assert pid not in {i["note_id"] for i in after["parent_note_items"]}
    assert after["parent_note_items"] == before["parent_note_items"]


# ── 64-66: one-way / no chat ────────────────────────────────────────────────
CHAT_MARKERS = ("reply_to_note_id", "reply_to", "thread_id", "thread",
                "conversation_id", "conversation", "message_status",
                "read_receipt", "read_at", "typing", "realtime", "channel",
                "sender_id", "recipient_id", "message_id")


def test_no_chat_concept_in_the_response_or_the_domain():
    """(64)(65)"""
    c = _c()
    r = _post(c, key="chat", linked=CURRENT_ASSIGNMENT)
    blob = r.text
    for marker in CHAT_MARKERS:
        assert marker not in blob, f"response leaked {marker!r}"
        assert marker not in ParentNote.model_fields, f"domain gained {marker!r}"
        assert marker not in S.ParentNoteCreateRequest.model_fields
        assert marker not in S.ParentNoteCreated.model_fields
        assert marker not in S.ParentNoteView.model_fields
    assert set(S.ParentNoteCreateResponse.model_fields) == {"note", "idempotent_replay"}


def test_no_therapist_write_route_was_added():
    """(66) The notes path carries exactly GET (therapist) and POST (parent)."""
    c = _c()
    schema = c.app.openapi()
    methods = {m for m in schema["paths"][f"/api/v1/children/{{child_id}}/notes"]
               if m in ("get", "post", "put", "patch", "delete")}
    assert methods == {"get", "post"}
    # No reply/thread/chat path anywhere.
    for path in schema["paths"]:
        for banned in ("repl", "thread", "message", "chat", "conversation"):
            assert banned not in path.lower(), path
    # The therapist cannot write here.
    assert _post(c, headers=HANNAH, key="tw").status_code == 403


# ── 67-72: privacy ──────────────────────────────────────────────────────────
FORBIDDEN = (
    "weekly_plan_id", "display_order", "activity_version_id",
    "activity_template_id", "assignment_id", "linked_assignment_id",
    "audit_event_id", "idempotency", "idempotency_key_hash", "request_hash",
    "operation_target", "parent_id", "therapist_id", "environment",
    "schema_version", "save_scope", "is_derived", "provenance",
    "marked_for_next_session", "private",
)


def test_parent_response_exposes_no_internals():
    """(67)(68)(69)(70)(71)(72)"""
    c = _c()
    blob = _post(c, key="priv", linked=CURRENT_ASSIGNMENT).text
    for marker in FORBIDDEN:
        assert marker not in blob, f"parent response leaked {marker!r}"
    for other in ("child_eli", "child_noah", "par_omar", "ther_hannah",
                  "ptn_maya_1", "Consider AAC backup"):
        assert other not in blob, other


def test_parent_create_schema_key_sets_are_pinned():
    """The response model IS the privacy boundary — pin it at source."""
    assert set(S.ParentNoteCreateRequest.model_fields) == {
        "note_type", "body", "linked_assignment_id"}
    assert set(S.ParentNoteCreated.model_fields) == {
        "note_id", "note_type", "body", "created_at", "review_status",
        "session_preparation_status", "linked_activity_title"}
    for model in (S.ParentNoteCreateRequest, S.ParentNoteCreated,
                  S.ParentNoteCreateResponse):
        for marker in FORBIDDEN:
            if marker == "linked_assignment_id":
                continue    # legitimately part of the REQUEST, never the response
            assert marker not in model.model_fields, f"{model.__name__}: {marker}"
    assert "linked_assignment_id" not in S.ParentNoteCreated.model_fields


# ── 73-77: regression ───────────────────────────────────────────────────────
def test_existing_note_fixtures_are_untouched():
    """(77) Parent writes must not rewrite the fictional seed data."""
    c = _c()
    repo = _repo(c)
    seeded = {n["id"]: copy.deepcopy(n) for n in repo.query(C.PARENT_NOTES)}
    assert set(seeded) == {"pn_maya_1", "pn_maya_2", "pn_noah_1"}
    _post(c, key="fix", linked=CURRENT_ASSIGNMENT)
    for nid, row in seeded.items():
        assert repo.query(C.PARENT_NOTES, id=nid)[0] == row, nid


def test_note_writes_do_not_disturb_the_plan_or_proposal_workflows():
    """(73)(74)(75)(76)"""
    from tests.test_modify_proposal import body as modify_body
    from tests.test_parent_acceptance import MAYA_MODIFY_PATH, _talking_activity

    c = _c()
    repo = _repo(c)
    _post(c, key="w1", linked=CURRENT_ASSIGNMENT)
    _post(c, key="w2", note_type="update", body="Went well.")

    # Modify still creates, is eligible, and accepts.
    m = c.post(MAYA_MODIFY_PATH, headers={**HANNAH, "Idempotency-Key": "m"},
               json={**modify_body(), "activity": _talking_activity(),
                     "expected_assignment_version": 1})
    assert m.status_code == 200                                          # (73)
    pid = m.json()["proposal"]["proposal_id"]
    live = repo.query(C.PLAN_ASSIGNMENTS, id=CURRENT_ASSIGNMENT)[0]
    acc = c.post(f"/api/v1/children/{CHILD}/proposals/{pid}/accept",
                 headers={**ELENA, "Idempotency-Key": "ma"},
                 json={"expected_proposal_version": 1,
                       "expected_assignment_version": live["version"]})
    assert acc.status_code == 200

    # Add still creates and accepts.
    c2 = _c()
    _post(c2, key="w3")
    add = c2.post(f"/api/v1/children/{CHILD}/weekly-plan/proposals/add",
                  headers={**HANNAH, "Idempotency-Key": "a"},
                  json={"scheduled_day": 3, "expected_weekly_plan_id": "wp_maya",
                        "activity": {"title": "Extra",
                                     "developmental_domain": "social_and_emotional",
                                     "milestone_id": "mile_turn_taking"},
                        "change_reason": "r", "save_scope": "child_only"})
    assert add.status_code == 200                                        # (74)
    apid = add.json()["proposal"]["proposal_id"]
    assert c2.post(f"/api/v1/children/{CHILD}/proposals/{apid}/accept",
                   headers={**ELENA, "Idempotency-Key": "aa"},
                   json={"expected_proposal_version": 1}).status_code == 200

    # Canonical current-plan resolver and therapist proposal reads unchanged.
    from app.services.weekly_plan import current_weekly_plan_id
    assert current_weekly_plan_id(_repo(c2), CHILD) == "wp_maya"         # (75)
    assert c2.get(f"/api/v1/children/{CHILD}/proposals",
                  headers=HANNAH).status_code == 200                     # (76)


# ── OpenAPI / integrity ─────────────────────────────────────────────────────
def test_openapi_gains_an_operation_not_a_path():
    c = _c()
    schema = c.app.openapi()
    assert schema["openapi"] == "3.1.0"
    assert len(schema["paths"]) == 22, "POST shares the existing notes path"
    operations = sum(
        len([m for m in v if m in ("get", "post", "put", "patch", "delete")])
        for v in schema["paths"].values())
    assert operations == 23, "exactly one new operation"

    sch = schema["components"]["schemas"]
    assert set(sch["ParentNoteCreateRequest"]["required"]) == {"note_type", "body"}
    assert "linked_activity_title" in sch["ParentNoteCreated"]["properties"]
    assert "linked_assignment_id" not in sch["ParentNoteCreated"]["properties"]
    for name in sch:
        for banned in ("Chat", "Message", "Thread", "Reply", "Conversation"):
            assert banned not in name, name


def test_parent_note_service_imports_no_parent_api_or_cloud_sdk():
    """(83)(85)"""
    banned = ("firebase", "firestore", "google", "genex_core", "boto3", "azure")
    for node in ast.walk(ast.parse(inspect.getsource(P))):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        for name in names:
            assert not any(b in name.lower() for b in banned), name
