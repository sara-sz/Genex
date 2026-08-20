"""Parent-safe read of the parent's OWN submitted notes (Phase 1B.3B).

A parent submits one-way collaboration items; this is how they read back what
*they* sent. The substantive guarantee is authorship isolation: the filter is on
the note's STORED `parent_id`, never on child ownership — so another caregiver's
submissions are not this parent's to read, today or when multi-caregiver access
arrives.

Still one-way. No reply, thread, conversation, read receipt or therapist
response. Still append-only: this phase adds GET and nothing else.
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
from app.services import parent_note_service as W
from app.services import read_service as R
from tests.conftest import ELENA, HANNAH, OMAR, PRIYA, UNCONNECTED, read_slice_client

CHILD = "child_maya"
NOTES = f"/api/v1/children/{CHILD}/notes"
INBOX = "/api/v1/notes"
CURRENT_ASSIGNMENT = "assign_maya_bubbles"

# ELENA is Maya's authorized parent; OMAR is Eli's parent (no access to Maya).
ELENA_ID = "par_elena"
OMAR_ID = "par_omar"

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


def _write(c, note_type="question", body="Is signing ok?", linked=None, key="k1",
           headers=ELENA, child=CHILD):
    payload = {"note_type": note_type, "body": body}
    if linked is not None:
        payload["linked_assignment_id"] = linked
    return c.post(f"/api/v1/children/{child}/notes",
                  headers={**headers, "Idempotency-Key": key}, json=payload)


def _read(c, headers=ELENA, child=CHILD):
    return c.get(f"/api/v1/children/{child}/notes", headers=headers)


def _seed_note(c, note_id, parent_id, child_id=CHILD, note_type=ParentNoteType.NOTE,
               body="Seeded.", review=ParentNoteReviewStatus.NEW,
               prep=SessionPreparationStatus.NONE, created_at="2026-07-30",
               linked_assignment_id=None, linked_activity_title=None):
    """Seed a ParentNote directly — the only way to construct another author's
    note, since the write path always stamps the authenticated parent."""
    _repo(c).set(C.PARENT_NOTES, note_id, ParentNote(
        id=note_id, child_id=child_id, parent_id=parent_id, note_type=note_type,
        body=body, review_status=review, session_preparation_status=prep,
        linked_assignment_id=linked_assignment_id,
        linked_activity_title=linked_activity_title,
        created_at=created_at, environment="dev").model_dump())
    return note_id


# ── 2-4: authorship audit ───────────────────────────────────────────────────
def test_parent_note_persists_deterministic_author_identity():
    """(2) `parent_id` is a required stored field, not derived."""
    field = ParentNote.model_fields["parent_id"]
    assert field.is_required(), "author identity must be mandatory"
    assert field.annotation is str
    # Every fixture note carries one.
    c = _c()
    assert all(n["parent_id"] for n in _repo(c).query(C.PARENT_NOTES))
    # And the write path stamps it from the resolved parent profile.
    assert 'parent_id=parent["id"]' in inspect.getsource(W)


def test_read_filters_on_stored_author_not_on_child_ownership():
    """(3)(4) The query is scoped by parent_id; no audit/session inference."""
    source = inspect.getsource(R.ReadService.get_parent_own_notes)
    assert "parent_id=parent[\"id\"]" in source, "must filter by stored author"
    assert "C.PARENT_NOTES" in source
    # Authorship is never inferred from anything else.
    for banned in ("AUDIT_EVENTS", "CONNECTIONS", "created_at ==", "[0]"):
        assert banned not in source, f"authorship inferred via {banned!r}"


# ── 5-9: route strategy ─────────────────────────────────────────────────────
def test_same_path_serves_both_roles_without_new_routes():
    """(5)(6)(8)(9)"""
    c = _c()
    schema = c.app.openapi()
    assert len(schema["paths"]) == 22
    operations = sum(
        len([m for m in v if m in ("get", "post", "put", "patch", "delete")])
        for v in schema["paths"].values())
    assert operations == 23, "role-aware GET adds no operation"
    notes = schema["paths"]["/api/v1/children/{child_id}/notes"]
    assert {m for m in notes if m in ("get", "post", "put", "patch", "delete")} == {"get", "post"}
    refs = {m["$ref"].rsplit("/", 1)[-1] for m in
            notes["get"]["responses"]["200"]["content"]["application/json"]["schema"]["anyOf"]}
    assert refs == {"Page", "ParentNoteHistoryResponse"}
    # No duplicate parent route was introduced.
    for path in schema["paths"]:
        for banned in ("parent-notes", "my-notes", "submissions", "history",
                       "collaboration", "message", "thread", "repl"):
            assert banned not in path.lower(), path


def test_parent_and_therapist_envelopes_are_disjoint():
    """The union cannot reshape one response into the other."""
    parent_required = {n for n, f in S.ParentNoteHistoryResponse.model_fields.items()
                       if f.is_required()}
    page_fields = set(S.Page.model_fields)
    assert "child_id" in parent_required and "child_id" not in page_fields


def test_cross_child_inbox_remains_therapist_only():
    """(7)(33)(34)"""
    c = _c()
    assert c.get(INBOX, headers=HANNAH).status_code == 200
    r = c.get(INBOX, headers=ELENA)
    assert r.status_code == 403, "a parent must not reach the therapist inbox"
    assert r.json()["error"] == "forbidden"
    # And no parent cross-child aggregation exists anywhere.
    assert "/api/v1/notes" in c.app.openapi()["paths"]
    assert {m for m in c.app.openapi()["paths"]["/api/v1/notes"]
            if m in ("get", "post", "put", "patch", "delete")} == {"get"}


# ── 10-18: basic parent read ────────────────────────────────────────────────
def test_parent_reads_back_all_three_types_with_correct_fields():
    """(10)(11)(12)(13)(14)(15)(16)(17)(18)"""
    c = _c()
    written = {}
    for note_type, text in (("question", "Is signing ok?"),
                            ("note", "Tires after five minutes."),
                            ("update", "Said 'more bubbles' unprompted!")):
        r = _write(c, note_type=note_type, body=text, key=f"w-{note_type}",
                   linked=CURRENT_ASSIGNMENT if note_type == "update" else None)
        written[note_type] = r.json()["note"]

    out = _read(c).json()
    assert out["child_id"] == CHILD
    items = {i["note_id"]: i for i in out["items"]}
    for note_type, created in written.items():
        got = items[created["note_id"]]
        assert got["note_type"] == note_type                             # (13)
        assert got["body"] == created["body"]                            # (14)
        assert got["created_at"] == created["created_at"]                # (15)
        assert got["review_status"] == "new"                             # (16)
        assert got["session_preparation_status"] == "none"               # (17)
        assert got["linked_activity_title"] == created["linked_activity_title"]  # (18)
    # The read item shape matches the create response exactly.
    assert set(S.ParentNoteHistoryItem.model_fields) == set(S.ParentNoteCreated.model_fields)


def test_fixture_notes_authored_by_this_parent_are_included():
    """Elena authored pn_maya_1 and pn_maya_2 in the fictional fixtures."""
    c = _c()
    ids = {i["note_id"] for i in _read(c).json()["items"]}
    assert {"pn_maya_1", "pn_maya_2"} <= ids
    assert "pn_noah_1" not in ids, "another child's note must not appear"


# ── 19-20: empty state ──────────────────────────────────────────────────────
def test_authorized_parent_with_no_notes_gets_an_empty_list():
    """(19)(20) Absence of content is not absence of authorization."""
    c = _c()
    repo = _repo(c)
    for n in repo.query(C.PARENT_NOTES, child_id=CHILD):
        repo._col(C.PARENT_NOTES).pop(n["id"], None)     # fictional teardown
    r = _read(c)
    assert r.status_code == 200                                          # (19)
    assert r.json()["items"] == [] and r.json()["total"] == 0            # (20)
    assert r.json()["child_id"] == CHILD


# ── 21-27: own-note isolation (BLOCKING) ────────────────────────────────────
def test_a_parent_never_sees_another_authors_note_for_the_same_child():
    """(21)(22)(23)(24)(25) Authorship isolation, the blocking guarantee.

    NOTE ON CONSTRUCTION: the frozen auth model gives a child exactly ONE
    authorized parent (`Child.parent_id`, gated by
    `access.require_parent_child_access`), so "two parents both authorized for
    one child" is not reachable through the API today. What IS constructible —
    and what actually proves the guarantee — is a note on this child authored by
    a DIFFERENT parent. If the read filtered on child ownership rather than
    stored authorship, that note would leak. It must not.
    """
    c = _c()
    mine = _write(c, body="Mine.", key="mine").json()["note"]["note_id"]
    theirs = _seed_note(c, "pn_other_author", parent_id=OMAR_ID,
                        body="Authored by another caregiver.")

    out = _read(c).json()
    ids = {i["note_id"] for i in out["items"]}
    assert mine in ids                                                   # (21)(23)
    assert theirs not in ids                                             # (22)(24)
    assert "Authored by another caregiver." not in _read(c).text
    assert out["total"] == len(out["items"])

    # The therapist, under the frozen policy, sees BOTH.
    therapist = c.get(NOTES, headers=HANNAH).json()                      # (25)
    tids = {i["note_id"] for i in therapist["items"]}
    assert mine in tids and theirs in tids


def test_an_unrelated_parent_is_existence_blind_and_sees_nothing():
    """(26)(27) Parent C: not connected, receives neither A's nor B's data."""
    c = _c()
    a = _write(c, body="Elena's note.", key="a").json()["note"]["note_id"]
    b = _seed_note(c, "pn_other_author", parent_id=OMAR_ID, body="Other caregiver.")

    r = _read(c, headers=OMAR)                                           # (26)
    assert r.status_code == 404
    assert r.json() == {"error": "not_found", "detail": "Not found."}
    for marker in (a, b, "Elena's note.", "Other caregiver."):           # (27)
        assert marker not in r.text


# ── 28-32: authorization ────────────────────────────────────────────────────
def test_authorization_matrix():
    """(28)(29)(30)(31)(32)"""
    c = _c()
    blind = {"error": "not_found", "detail": "Not found."}
    assert c.get(NOTES).status_code == 401                               # (28)
    r = _read(c, child="child_ghost")                                    # (29)
    assert r.status_code == 404 and r.json() == blind
    for child in ("child_amara", "child_sana", "child_rue"):             # (30)(31)(32)
        r = _read(c, child=child)
        assert r.status_code == 404, child
        assert r.json() == blind, child
    # Therapists keep therapist behaviour on this route.
    assert _read(c, headers=HANNAH).status_code == 200
    assert _read(c, headers=UNCONNECTED).status_code == 404
    assert _read(c, headers=PRIYA).status_code == 404


# ── 35-46: privacy ──────────────────────────────────────────────────────────
FORBIDDEN = (
    "parent_id", "author_id", "therapist_id", "linked_assignment_id",
    "assignment_id", "weekly_plan_id", "display_order", "activity_version_id",
    "activity_template_id", "audit_event_id", "idempotency", "request_hash",
    "operation", "environment", "schema_version", "provenance", "save_scope",
    "marked_for_next_session",
)


def test_parent_read_exposes_no_internals():
    """(35)-(46)"""
    c = _c()
    _write(c, linked=CURRENT_ASSIGNMENT, key="p1")
    _seed_note(c, "pn_other_author", parent_id=OMAR_ID, body="Other caregiver.")
    blob = _read(c).text
    for marker in FORBIDDEN:
        assert marker not in blob, f"parent read leaked {marker!r}"
    for other in ("child_eli", "child_noah", "par_omar", "par_elena",
                  "ther_hannah", "ptn_maya_1", "Consider AAC backup",
                  "Other caregiver.", "pn_noah_1"):
        assert other not in blob, other


def test_parent_read_schema_key_sets_are_pinned():
    """The response model IS the privacy boundary."""
    assert set(S.ParentNoteHistoryItem.model_fields) == {
        "note_id", "note_type", "body", "created_at", "review_status",
        "session_preparation_status", "linked_activity_title"}
    assert set(S.ParentNoteHistoryResponse.model_fields) == {
        "child_id", "items", "total", "next_cursor"}
    for model in (S.ParentNoteHistoryItem, S.ParentNoteHistoryResponse):
        for marker in FORBIDDEN:
            assert marker not in model.model_fields, f"{model.__name__}: {marker}"


# ── 47-52: point-in-time linked activity ────────────────────────────────────
def test_linked_title_survives_the_activity_being_replaced():
    """(47)(48)(49)(50)(51)(52) Pins the founder's point-in-time decision.

    The note is linked to a CURRENT assignment; the assignment is then genuinely
    retired by the frozen Modify accept workflow. The note must keep the title it
    captured, keep appearing, and never relink to the replacement.
    """
    from tests.test_modify_proposal import body as modify_body
    from tests.test_parent_acceptance import MAYA_MODIFY_PATH, _talking_activity

    c = _c()
    repo = _repo(c)
    note_id = _write(c, note_type="update", body="This went well.",
                     linked=CURRENT_ASSIGNMENT, key="pit").json()["note"]["note_id"]
    before_item = next(i for i in _read(c).json()["items"] if i["note_id"] == note_id)
    assert before_item["linked_activity_title"] == "Bubble requesting"   # (47)
    stored_before = copy.deepcopy(repo.query(C.PARENT_NOTES, id=note_id)[0])

    # Genuinely retire the linked assignment through the frozen workflow.
    m = c.post(MAYA_MODIFY_PATH, headers={**HANNAH, "Idempotency-Key": "m"},
               json={**modify_body(), "activity": _talking_activity(),
                     "expected_assignment_version": 1})
    assert m.status_code == 200
    live = repo.query(C.PLAN_ASSIGNMENTS, id=CURRENT_ASSIGNMENT)[0]
    acc = c.post(f"/api/v1/children/{CHILD}/proposals/"
                 f"{m.json()['proposal']['proposal_id']}/accept",
                 headers={**ELENA, "Idempotency-Key": "acc"},
                 json={"expected_proposal_version": 1,
                       "expected_assignment_version": live["version"]})
    assert acc.status_code == 200                                        # (48)
    replacement = acc.json()["replacement_assignment"]
    assert repo.query(C.PLAN_ASSIGNMENTS, id=CURRENT_ASSIGNMENT)[0][
        "assignment_status"] == "replaced"

    after = _read(c).json()
    after_item = next((i for i in after["items"] if i["note_id"] == note_id), None)
    assert after_item is not None, "a retired link must not hide the note"  # (49)
    assert after_item["linked_activity_title"] == "Bubble requesting"    # (50)
    assert after_item == before_item, "nothing about the note changed"
    # (51) no relink — the stored record still names the ORIGINAL assignment
    stored_after = repo.query(C.PARENT_NOTES, id=note_id)[0]
    assert stored_after == stored_before
    assert stored_after["linked_assignment_id"] == CURRENT_ASSIGNMENT
    assert stored_after["linked_assignment_id"] != replacement["assignment_id"]
    # (52) and the id is still hidden from the parent
    assert "linked_assignment_id" not in _read(c).text
    assert replacement["assignment_id"] not in _read(c).text


def test_the_title_shown_is_the_stored_one_not_a_re_resolved_one():
    """(50) Distinguishes STORED from RE-RESOLVED, which the test above cannot.

    Retiring an assignment leaves it resolvable at the same title, so a read that
    re-resolved from the plan would still look correct. Here the stored title is
    deliberately different from what any live lookup would produce: only an
    implementation that reads the note's own field can return it.
    """
    c = _c()
    _seed_note(c, "pn_captured", parent_id=ELENA_ID,
               linked_assignment_id=CURRENT_ASSIGNMENT,
               linked_activity_title="Bubble requesting (as it was in July)")
    live_title = _repo(c).query(
        C.ACTIVITY_VERSIONS,
        id=_repo(c).query(C.PLAN_ASSIGNMENTS, id=CURRENT_ASSIGNMENT)[0]
        ["activity_version_id"])[0]["title"]
    assert live_title == "Bubble requesting", "precondition: live title differs"

    item = next(i for i in _read(c).json()["items"] if i["note_id"] == "pn_captured")
    assert item["linked_activity_title"] == "Bubble requesting (as it was in July)"
    assert item["linked_activity_title"] != live_title


# ── 53-57: status visibility ────────────────────────────────────────────────
def test_stored_statuses_are_shown_verbatim_and_independently():
    """(53)(54)(55)(56)(57)"""
    c = _c()
    combos = {
        "pn_new_none": (ParentNoteReviewStatus.NEW, SessionPreparationStatus.NONE),
        "pn_rev_none": (ParentNoteReviewStatus.REVIEWED, SessionPreparationStatus.NONE),
        "pn_new_disc": (ParentNoteReviewStatus.NEW,
                        SessionPreparationStatus.DISCUSS_AT_NEXT_SESSION),
        "pn_rev_disc": (ParentNoteReviewStatus.REVIEWED,
                        SessionPreparationStatus.DISCUSS_AT_NEXT_SESSION),
        "pn_rev_done": (ParentNoteReviewStatus.REVIEWED,
                        SessionPreparationStatus.DISCUSSED),
    }
    for nid, (review, prep) in combos.items():
        _seed_note(c, nid, parent_id=ELENA_ID, review=review, prep=prep)

    items = {i["note_id"]: i for i in _read(c).json()["items"]}
    for nid, (review, prep) in combos.items():
        assert items[nid]["review_status"] == review.value, nid          # (53)(54)
        assert items[nid]["session_preparation_status"] == prep.value, nid  # (55)
    # (56) all four combinations coexist -> the dimensions vary independently
    seen = {(items[n]["review_status"], items[n]["session_preparation_status"])
            for n in combos}
    assert len(seen) == len(combos)
    # (57) no synthesized labels
    blob = _read(c).text
    for invented in ("Seen", "Read", "Replied", "Answered", "seen", "replied"):
        assert invented not in blob, invented


def test_reading_a_new_note_never_marks_it_reviewed():
    """(57)(66) The single most important read-only property."""
    c = _c()
    note_id = _write(c, key="ro").json()["note"]["note_id"]
    before = copy.deepcopy(_repo(c).query(C.PARENT_NOTES, id=note_id)[0])
    for _ in range(5):
        _read(c)
        c.get(NOTES, headers=HANNAH)
    after = _repo(c).query(C.PARENT_NOTES, id=note_id)[0]
    assert after == before
    assert after["review_status"] == "new"
    assert after["session_preparation_status"] == "none"


# ── 58-60: ordering ─────────────────────────────────────────────────────────
def test_ordering_is_newest_first_and_deterministic():
    """(58)(59)(60)"""
    c = _c()
    repo = _repo(c)
    for n in repo.query(C.PARENT_NOTES, child_id=CHILD):
        repo._col(C.PARENT_NOTES).pop(n["id"], None)
    _seed_note(c, "pn_b", parent_id=ELENA_ID, created_at="2026-07-10")
    _seed_note(c, "pn_a", parent_id=ELENA_ID, created_at="2026-07-20")
    _seed_note(c, "pn_c", parent_id=ELENA_ID, created_at="2026-07-15")

    order = [i["note_id"] for i in _read(c).json()["items"]]
    assert order == ["pn_a", "pn_c", "pn_b"], "newest created_at first"  # (58)
    for _ in range(5):                                                   # (59)
        assert [i["note_id"] for i in _read(c).json()["items"]] == order

    # (60) equal timestamps -> deterministic id tie-break, and it must not
    # depend on insertion order.
    for n in repo.query(C.PARENT_NOTES, child_id=CHILD):
        repo._col(C.PARENT_NOTES).pop(n["id"], None)
    for nid in ("pn_mid", "pn_zzz", "pn_aaa"):
        _seed_note(c, nid, parent_id=ELENA_ID, created_at="2026-07-20")
    tie = [i["note_id"] for i in _read(c).json()["items"]]
    assert tie == ["pn_zzz", "pn_mid", "pn_aaa"], "descending id tie-break"
    assert tie != ["pn_mid", "pn_zzz", "pn_aaa"], "not insertion order"


def test_parent_ordering_matches_the_frozen_therapist_key():
    """The two surfaces agree; the therapist helper itself is untouched."""
    c = _c()
    _write(c, body="Newest.", key="o1")
    parent_order = [i["note_id"] for i in _read(c).json()["items"]]
    therapist_order = [i["note_id"] for i in c.get(NOTES, headers=HANNAH).json()["items"]
                       if i["note_id"] in set(parent_order)]
    assert parent_order == therapist_order


# ── 61-67: read-only ────────────────────────────────────────────────────────
def test_parent_read_mutates_nothing():
    """(61)(62)(63)(67)"""
    c = _c()
    _write(c, linked=CURRENT_ASSIGNMENT, key="m1")
    _seed_note(c, "pn_other_author", parent_id=OMAR_ID)
    before = _snapshot(c)
    first = _read(c).json()
    for _ in range(5):
        assert _read(c).json() == first
        c.get(NOTES, headers=HANNAH)
        c.get(INBOX, headers=HANNAH)
    after = _snapshot(c)
    for coll in WATCHED:
        assert after[coll] == before[coll], f"{coll} changed during a read"


def test_no_read_receipt_or_viewed_at_exists():
    """(64)(65)"""
    for model in (ParentNote, S.ParentNoteHistoryItem, S.ParentNoteHistoryResponse,
                  S.ParentNoteView):
        for banned in ("viewed_at", "read_at", "last_viewed", "read_receipt",
                       "seen_at", "opened_at", "view_count"):
            assert banned not in model.model_fields, f"{model.__name__}: {banned}"
    c = _c()
    _write(c, key="rr")
    assert not any(k in _read(c).text for k in ("viewed_at", "read_at", "seen_at"))


# ── 68-76: no chat / append-only ────────────────────────────────────────────
CHAT_MARKERS = ("reply_to_note_id", "reply_to", "thread_id", "thread",
                "conversation_id", "conversation", "message_status",
                "sender_id", "recipient_id", "message_id", "typing", "realtime")


def test_read_introduces_no_chat_concept():
    """(68)(69)(70)(71)(72)"""
    c = _c()
    _write(c, linked=CURRENT_ASSIGNMENT, key="nc")
    blob = _read(c).text
    for marker in CHAT_MARKERS:
        assert marker not in blob, marker
        for model in (ParentNote, S.ParentNoteHistoryItem,
                      S.ParentNoteHistoryResponse, S.ParentNoteCreated):
            assert marker not in model.model_fields, f"{model.__name__}: {marker}"
    schema = c.app.openapi()
    for name in schema["components"]["schemas"]:
        for banned in ("Chat", "Message", "Thread", "Reply", "Conversation"):
            assert banned not in name, name


def test_append_only_holds_after_adding_the_read():
    """(73)(74)(75)(76)"""
    c = _c()
    schema = c.app.openapi()
    mutations = [(p, m) for p, v in schema["paths"].items()
                 for m in v if m in ("put", "patch", "delete")]
    assert mutations == [], f"append-only broken: {mutations}"
    # POST on the notes path is still the ONLY ParentNote write.
    posts = [p for p, v in schema["paths"].items() if "post" in v]
    assert "/api/v1/children/{child_id}/notes" in posts


# ── 77-89: regression ───────────────────────────────────────────────────────
def test_therapist_child_note_read_is_unchanged():
    """(77)(79)(80)"""
    c = _c()
    seeded = {n["id"]: copy.deepcopy(n) for n in _repo(c).query(C.PARENT_NOTES)}
    fresh = _write(c, note_type="update", body="New from parent.",
                   key="tr").json()["note"]["note_id"]

    out = c.get(NOTES, headers=HANNAH).json()
    assert set(out) == {"items", "total", "next_cursor"}                 # (77)
    assert set(out["items"][0]) == {
        "note_id", "child_id", "note_type", "review_status",
        "session_preparation_status", "linked_assignment_id",
        "linked_activity_title", "body", "created_at"}
    ids = {i["note_id"] for i in out["items"]}
    assert fresh in ids                                                  # (80)
    assert {"pn_maya_1", "pn_maya_2"} <= ids
    # Therapist still sees linked_assignment_id — NOT reduced to the parent shape.
    linked = next(i for i in out["items"] if i["note_id"] == "pn_maya_1")
    assert linked["linked_assignment_id"] == "assign_maya_bubbles"
    for nid, row in seeded.items():                                      # (79)
        assert _repo(c).query(C.PARENT_NOTES, id=nid)[0] == row


def test_therapist_inbox_is_unchanged():
    """(78)"""
    c = _c()
    fresh = _write(c, key="inb").json()["note"]["note_id"]
    out = c.get(INBOX, headers=HANNAH).json()
    assert set(out) == {"items", "total", "next_cursor"}
    assert fresh in {i["note_id"] for i in out["items"]}
    assert "linked_assignment_id" in out["items"][0]


def test_parent_write_path_is_unchanged():
    """(81)(82)(83)(84)(85)(86)"""
    c = _c()
    for note_type in ("question", "note", "update"):
        r = _write(c, note_type=note_type, body=f"A {note_type}.", key=f"w-{note_type}")
        assert r.status_code == 200
        assert set(r.json()) == {"note", "idempotent_replay"}
    linked = _write(c, linked=CURRENT_ASSIGNMENT, key="wl")              # (84)
    assert linked.json()["note"]["linked_activity_title"] == "Bubble requesting"
    replay = _write(c, linked=CURRENT_ASSIGNMENT, key="wl")              # (85)
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["note"] == linked.json()["note"]
    conflict = _write(c, body="Changed.", linked=CURRENT_ASSIGNMENT, key="wl")
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "idempotency_key_conflict"
    # Retired links still rejected on write (frozen behaviour).
    repo = _repo(c)
    a = repo.query(C.PLAN_ASSIGNMENTS, id=CURRENT_ASSIGNMENT)[0]
    a["assignment_status"] = "replaced"
    repo.set(C.PLAN_ASSIGNMENTS, CURRENT_ASSIGNMENT, a)
    assert _write(c, linked=CURRENT_ASSIGNMENT, key="wr").status_code == 404


def test_plan_workflows_are_unchanged():
    """(87)(88)(89)"""
    from app.services.weekly_plan import current_weekly_plan_id
    from tests.test_modify_proposal import body as modify_body
    from tests.test_parent_acceptance import MAYA_MODIFY_PATH, _talking_activity

    c = _c()
    _write(c, key="pw")
    repo = _repo(c)
    m = c.post(MAYA_MODIFY_PATH, headers={**HANNAH, "Idempotency-Key": "m"},
               json={**modify_body(), "activity": _talking_activity(),
                     "expected_assignment_version": 1})
    assert m.status_code == 200                                          # (87)
    live = repo.query(C.PLAN_ASSIGNMENTS, id=CURRENT_ASSIGNMENT)[0]
    assert c.post(f"/api/v1/children/{CHILD}/proposals/"
                  f"{m.json()['proposal']['proposal_id']}/accept",
                  headers={**ELENA, "Idempotency-Key": "ma"},
                  json={"expected_proposal_version": 1,
                        "expected_assignment_version": live["version"]}).status_code == 200

    c2 = _c()
    _write(c2, key="pw2")
    add = c2.post(f"/api/v1/children/{CHILD}/weekly-plan/proposals/add",
                  headers={**HANNAH, "Idempotency-Key": "a"},
                  json={"scheduled_day": 3, "expected_weekly_plan_id": "wp_maya",
                        "activity": {"title": "Extra",
                                     "developmental_domain": "social_and_emotional",
                                     "milestone_id": "mile_turn_taking"},
                        "change_reason": "r", "save_scope": "child_only"})
    assert add.status_code == 200                                        # (88)
    assert c2.post(f"/api/v1/children/{CHILD}/proposals/"
                   f"{add.json()['proposal']['proposal_id']}/accept",
                   headers={**ELENA, "Idempotency-Key": "aa"},
                   json={"expected_proposal_version": 1}).status_code == 200
    assert current_weekly_plan_id(_repo(c2), CHILD) == "wp_maya"         # (89)


# ── integrity ───────────────────────────────────────────────────────────────
def test_read_path_imports_no_parent_api_or_cloud_sdk():
    """(95)(97)"""
    banned = ("firebase", "firestore", "google", "genex_core", "boto3", "azure")
    for module in (R, W):
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for name in names:
                assert not any(b in name.lower() for b in banned), (module, name)
