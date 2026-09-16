"""A therapist writes a PRIVATE note (Phase 1B.3E).

The guarantees worth stating plainly:

* **Private means private to the AUTHOR.** The note is reachable only through the
  frozen therapist read, filtered on both `child_id` and `therapist_id` in the
  repository query. No parent-facing surface reads this collection at all.
* **Append-only.** No edit, delete, withdraw or archive, and no toggle for
  `marked_for_next_session` — it is chosen at creation and never afterwards.
* **Server owns identity.** Author, child, id, timestamps and environment are
  derived server-side; a client cannot supply or forge any of them.
* **The audit does not store the note body.** Provenance records who wrote what
  kind of thing, about whom, when — never the clinical text.

A private note is not a parent message, chat, reply, RTM work entry or billing
documentation, and writing one notifies nobody.
"""

from __future__ import annotations

import ast
import copy
import inspect
import pathlib
import threading

from app.api import schemas as S
from app.domain.read_models import PrivateTherapistNote
from app.repository import collections as C
from app.services import private_note_service as V
from tests.conftest import ELENA, HANNAH, OMAR, PRIYA, UNCONNECTED, read_slice_client

#: Resolved from THIS file, never from the process working directory — pytest
#: runs with `working-directory: therapist_api` in CI, so a CWD-relative
#: "therapist_api/app" resolves to nothing there, and `rglob` on a missing path
#: yields an empty iterator that would let source-scanning guards pass while
#: inspecting nothing. Same form as tests/test_isolation.py.
APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"

CHILD = "child_maya"
NOAH_CHILD = "child_noah"
# Hannah's NON-ACTIVE connections (verified against fixtures): a private write
# must fail closed for every one of them, exactly as the frozen read does.
PENDING_CHILD = "child_amara"           # pending_parent_acceptance
PAUSED_CHILD = "child_sana"             # paused_by_parent
ENDED_CHILD = "child_rue"               # ended
RESTRICTED_CHILDREN = (PENDING_CHILD, PAUSED_CHILD, ENDED_CHILD)
PRIYA_CHILD = "child_theo"              # Priya's own active caseload
HANNAH_ID = "ther_hannah"
ELENA_ID = "par_elena"

# Fixture private notes (authored by Hannah).
MAYA_PRIVATE = "ptn_maya_1"
NOAH_PRIVATE = "ptn_noah_1"

ROUTE = "/api/v1/children/{child}/private-notes"
PARENT_NOTES_ROUTE = "/api/v1/children/{child}/notes"

WATCHED = (C.PARENT_NOTES, C.PRIVATE_THERAPIST_NOTES, C.AUDIT_EVENTS,
           C.IDEMPOTENCY_RECORDS, C.PLAN_ASSIGNMENTS, C.PLAN_CHANGE_PROPOSALS,
           C.ACTIVITY_VERSIONS, C.WEEKLY_PLANS, C.CHILDREN, C.CONNECTIONS,
           C.PARENT_PROFILES, C.THERAPIST_PROFILES)


def _c():
    return read_slice_client()


def _repo(c):
    return c.app.state.repo


def _write(c, body="Watch fatigue in the last ten minutes.", marked=None,
           child=CHILD, headers=HANNAH, key="pk-default", extra=None):
    payload = {"body": body}
    if marked is not None:
        payload["marked_for_next_session"] = marked
    if extra:
        payload.update(extra)
    return c.post(ROUTE.format(child=child),
                  headers={**headers, "Idempotency-Key": key}, json=payload)


def _get_private(c, child=CHILD, headers=HANNAH):
    return c.get(ROUTE.format(child=child), headers=headers)


def _next_session(c, child=CHILD, headers=HANNAH):
    return c.get(f"/api/v1/children/{child}/next-session", headers=headers)


def _notes(c, child=CHILD, headers=HANNAH):
    return _repo(c).query(C.PRIVATE_THERAPIST_NOTES)


def _events(c, note_id=None):
    out = [e for e in _repo(c).query(C.AUDIT_EVENTS) if e["event_type"] == V.EVENT_TYPE]
    return [e for e in out if e["subject_id"] == note_id] if note_id else out


def _plain(v):
    return getattr(v, "value", v)


# ── 1-9: creation ───────────────────────────────────────────────────────────
def test_authorized_therapist_creates_a_private_note():
    """(1)(2)"""
    c = _c()
    r = _write(c, key="t1")
    assert r.status_code == 200, r.text
    note = r.json()["note"]
    assert note["child_id"] == CHILD
    assert note["body"] == "Watch fatigue in the last ten minutes."
    assert note["marked_for_next_session"] is False
    assert note["note_id"].startswith("ptn_")
    assert r.json()["idempotent_replay"] is False


def test_the_note_is_persisted_with_the_authenticated_author():
    """(3) Server-derived author."""
    c = _c()
    nid = _write(c, key="t2").json()["note"]["note_id"]
    stored = _repo(c).get(C.PRIVATE_THERAPIST_NOTES, nid)
    assert stored["therapist_id"] == HANNAH_ID
    assert stored["child_id"] == CHILD


def test_marked_for_next_session_defaults_to_false_when_omitted():
    """(4)"""
    c = _c()
    r = _write(c, marked=None, key="t3")
    assert r.json()["note"]["marked_for_next_session"] is False
    nid = r.json()["note"]["note_id"]
    assert _repo(c).get(C.PRIVATE_THERAPIST_NOTES, nid)["marked_for_next_session"] is False
    assert V.DEFAULT_MARKED_FOR_NEXT_SESSION is False


def test_explicit_false_is_honoured():
    """(5)"""
    c = _c()
    r = _write(c, marked=False, key="t4")
    assert r.json()["note"]["marked_for_next_session"] is False


def test_explicit_true_is_honoured():
    """(6)"""
    c = _c()
    r = _write(c, marked=True, key="t5")
    assert r.json()["note"]["marked_for_next_session"] is True
    nid = r.json()["note"]["note_id"]
    assert _repo(c).get(C.PRIVATE_THERAPIST_NOTES, nid)["marked_for_next_session"] is True


def test_server_derives_id_timestamp_environment_and_schema_version():
    """(7)"""
    c = _c()
    nid = _write(c, key="t6").json()["note"]["note_id"]
    stored = _repo(c).get(C.PRIVATE_THERAPIST_NOTES, nid)
    assert stored["id"] == nid
    assert stored["created_at"] and stored["created_at"].startswith("20")
    assert stored["environment"] == "dev"
    assert stored["schema_version"]


def test_client_supplied_server_owned_fields_are_ignored():
    """(8) A forged author, child, id or timestamp must not take effect."""
    c = _c()
    r = _write(c, key="t7", extra={
        "id": "ptn_forged",
        "child_id": NOAH_CHILD,
        "therapist_id": "ther_priya",
        "created_at": "1999-01-01",
        "environment": "prod",
        "schema_version": "forged",
    })
    assert r.status_code == 200, r.text
    nid = r.json()["note"]["note_id"]
    stored = _repo(c).get(C.PRIVATE_THERAPIST_NOTES, nid)
    assert nid != "ptn_forged"
    assert stored["child_id"] == CHILD              # from the authorized path
    assert stored["therapist_id"] == HANNAH_ID      # from the principal
    assert stored["created_at"] != "1999-01-01"
    assert stored["environment"] == "dev"
    assert not _repo(c).exists(C.PRIVATE_THERAPIST_NOTES, "ptn_forged")


def test_a_therapist_cannot_author_a_note_for_another_therapist():
    """(9) The substantive authorship guarantee."""
    c = _c()
    nid = _write(c, key="t8", extra={"therapist_id": "ther_priya"}).json()["note"]["note_id"]
    assert _repo(c).get(C.PRIVATE_THERAPIST_NOTES, nid)["therapist_id"] == HANNAH_ID
    # ...and it is not visible to Priya's own read of any child.
    assert _get_private(c, headers=PRIYA).status_code == 404


def test_the_request_model_allows_only_body_and_the_flag():
    """(10)"""
    assert set(S.PrivateNoteCreateRequest.model_fields) == {"body", "marked_for_next_session"}
    assert V.CLIENT_SUPPLIED_FIELDS == ("body", "marked_for_next_session")
    for f in V.SERVER_DERIVED_FIELDS:
        assert f not in S.PrivateNoteCreateRequest.model_fields, f


# ── 11-15: body validation ──────────────────────────────────────────────────
def test_empty_body_is_rejected():
    """(11) 422 invalid_request — the canonical shape used by the frozen
    parent-note write, not a new error code invented here."""
    c = _c()
    before = len(_notes(c))
    r = _write(c, body="", key="v1")
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "invalid_request"
    assert len(_notes(c)) == before


def test_whitespace_only_body_is_rejected():
    """(12)"""
    c = _c()
    before = len(_notes(c))
    for i, blank in enumerate(("   ", "\t", "\n", "  \n\t ")):
        r = _write(c, body=blank, key=f"v2-{i}")
        assert r.status_code == 422, (blank, r.text)
        assert r.json()["error"] == "invalid_request"
    assert len(_notes(c)) == before


def test_meaningful_text_is_preserved_verbatim():
    """(13) No trimming, normalising or re-encoding of clinical text."""
    c = _c()
    text = "  Leading space kept.\n\nParagraph two — em dash, 'quotes', 50%.  "
    nid = _write(c, body=text, key="v3").json()["note"]["note_id"]
    assert _repo(c).get(C.PRIVATE_THERAPIST_NOTES, nid)["body"] == text


def test_no_maximum_length_is_imposed():
    """(14) Matches the documented project convention — see also test below."""
    c = _c()
    long_body = "x" * 20000
    r = _write(c, body=long_body, key="v4")
    assert r.status_code == 200, r.text
    assert len(_repo(c).get(C.PRIVATE_THERAPIST_NOTES, r.json()["note"]["note_id"])["body"]) == 20000


def test_the_project_still_has_no_shared_text_limit():
    """(15) Pins the REASON no limit is enforced: none exists to inherit.

    AST, not substring: the service docstring NAMES `max_length` to record that
    none exists, so a text scan would fail on the prose documenting the fact.
    """
    sources = sorted(APP_DIR.rglob("*.py"))
    assert sources, f"no sources found under {APP_DIR}"
    offenders = []
    for f in sources:
        for n in ast.walk(ast.parse(f.read_text())):
            # `Field(max_length=...)` / `constr(max_length=...)`
            for kw in getattr(n, "keywords", []) or []:
                if kw.arg in ("max_length", "min_length"):
                    offenders.append(f"{f.relative_to(APP_DIR).as_posix()}:{n.lineno}")
            # `MAX_BODY_LENGTH = 500` style module constants
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name) and ("MAX_BODY" in t.id or "MAX_LEN" in t.id):
                        offenders.append(f"{f.relative_to(APP_DIR).as_posix()}:{n.lineno}")
    assert offenders == [], offenders


# ── 16-24: authorization ────────────────────────────────────────────────────
def test_unauthenticated_is_canonical_401():
    """(16)"""
    c = _c()
    r = c.post(ROUTE.format(child=CHILD), headers={"Idempotency-Key": "a1"},
               json={"body": "x"})
    assert r.status_code == 401


def test_parent_cannot_create_a_private_note():
    """(17)"""
    c = _c()
    before = len(_notes(c))
    r = _write(c, headers=ELENA, key="a2")
    assert r.status_code == 403
    assert r.json()["error"] == "forbidden"
    assert len(_notes(c)) == before


def test_another_parent_cannot_either():
    """(18)"""
    c = _c()
    assert _write(c, headers=OMAR, key="a3").status_code == 403


def test_unauthorized_therapist_is_existence_blind_404():
    """(19) Priya is connected to another child only."""
    c = _c()
    before = len(_notes(c))
    r = _write(c, headers=PRIYA, key="a4")
    assert r.status_code == 404
    assert len(_notes(c)) == before


def test_unconnected_therapist_is_404():
    """(20)"""
    c = _c()
    assert _write(c, headers=UNCONNECTED, key="a5").status_code == 404


def test_unknown_child_is_404():
    """(21)"""
    c = _c()
    assert _write(c, child="child_does_not_exist", key="a6").status_code == 404


def test_non_active_relationship_fails_closed():
    """(22) pending / paused / ended all fail closed, like the frozen read."""
    c = _c()
    before = len(_notes(c))
    for i, child in enumerate(RESTRICTED_CHILDREN):
        r = _write(c, child=child, key=f"a7-{i}")
        assert r.status_code == 404, (child, r.text)
        # ...and the frozen READ agrees, so write and read fail closed alike.
        assert _get_private(c, child=child).status_code == 404, child
    assert len(_notes(c)) == before


def test_no_existence_leakage_across_the_404_family():
    """(23)"""
    c = _c()
    bodies = {
        _write(c, child="child_nope", key="a8").text,
        _write(c, headers=PRIYA, key="a9").text,
        _write(c, child=PENDING_CHILD, key="a10").text,
        _write(c, child=PAUSED_CHILD, key="a11b").text,
        _write(c, child=ENDED_CHILD, key="a12").text,
    }
    assert len(bodies) == 1, bodies


def test_authorization_precedes_body_validation():
    """(24) A parent sending a blank body still gets 403, never a hint."""
    c = _c()
    assert _write(c, body="", headers=ELENA, key="a11").status_code == 403


# ── 25-33: PRIVACY BOUNDARY — the critical guarantees ───────────────────────
SECRET = "SECRET-CLINICAL-TEXT-must-never-reach-a-parent"


def _seed_secret(c, marked=False, key="s1"):
    return _write(c, body=SECRET, marked=marked, key=key).json()["note"]["note_id"]


def test_the_note_is_readable_by_its_author():
    """(25)"""
    c = _c()
    nid = _seed_secret(c)
    items = _get_private(c).json()["items"]
    assert nid in [i["note_id"] for i in items]
    assert SECRET in _get_private(c).text


def test_another_therapist_cannot_read_it_via_their_own_child():
    """(26) Priya reads her OWN active caseload and sees nothing of Hannah's."""
    c = _c()
    _seed_secret(c, key="s2")
    r = _get_private(c, child=PRIYA_CHILD, headers=PRIYA)
    assert r.status_code == 200, r.text
    assert SECRET not in r.text


def test_another_therapist_cannot_read_it_by_knowing_the_child():
    """(27) Knowing child_maya is not enough — existence-blind 404."""
    c = _c()
    _seed_secret(c, key="s3")
    r = _get_private(c, headers=PRIYA)
    assert r.status_code == 404
    assert SECRET not in r.text


def test_another_therapist_sees_nothing_even_with_an_active_connection():
    """(28) Authorship, not child access, is what gates a private note.

    A second therapist is given an ACTIVE connection to the same child, so the
    404 above cannot be doing the work. The repository query filters on
    therapist_id, so the note is simply not in their result set.
    """
    c = _c()
    nid = _seed_secret(c, key="s4")
    repo = _repo(c)
    conn = [x for x in repo.query(C.CONNECTIONS, child_id=CHILD)][0]
    extra = dict(conn)
    extra.update(id="conn_priya_maya", therapist_id="ther_priya", status="active")
    repo.set(C.CONNECTIONS, "conn_priya_maya", extra)
    r = _get_private(c, headers=PRIYA)
    assert r.status_code == 200, r.text          # now authorized for the child
    assert SECRET not in r.text                  # ...but still cannot see the note
    assert nid not in [i["note_id"] for i in r.json()["items"]]


def test_private_note_never_appears_in_parent_own_note_history():
    """(29)"""
    c = _c()
    _seed_secret(c, key="s5")
    r = c.get(PARENT_NOTES_ROUTE.format(child=CHILD), headers=ELENA)
    assert r.status_code == 200
    assert SECRET not in r.text


def test_private_note_never_appears_in_the_therapist_parentnote_surface():
    """(30) The ParentNote GET is a different collection entirely."""
    c = _c()
    _seed_secret(c, key="s6")
    r = c.get(PARENT_NOTES_ROUTE.format(child=CHILD), headers=HANNAH)
    assert SECRET not in r.text


def test_private_note_never_appears_in_the_parentnote_inbox():
    """(31)"""
    c = _c()
    _seed_secret(c, key="s7")
    assert SECRET not in c.get("/api/v1/notes", headers=HANNAH).text


def test_private_note_never_appears_in_any_parent_reachable_response():
    """(32) Sweep EVERY endpoint a parent can reach."""
    c = _c()
    _seed_secret(c, key="s8")
    parent_urls = [
        "/api/v1/app/config",
        PARENT_NOTES_ROUTE.format(child=CHILD),
        f"/api/v1/children/{CHILD}/proposals",
    ]
    for p in c.get(f"/api/v1/children/{CHILD}/proposals", headers=ELENA).json()["items"]:
        parent_urls.append(f"/api/v1/children/{CHILD}/proposals/{p['proposal_id']}")
    for url in parent_urls:
        r = c.get(url, headers=ELENA)
        assert SECRET not in r.text, url
        assert "ptn_" not in r.text, url


def test_parent_cannot_reach_the_private_note_routes_at_all():
    """(33)"""
    c = _c()
    _seed_secret(c, key="s9")
    assert _get_private(c, headers=ELENA).status_code == 403
    assert _next_session(c, headers=ELENA).status_code == 403


# ── 34-39: append-only ──────────────────────────────────────────────────────
def test_the_api_has_no_put_patch_or_delete():
    """(34)"""
    c = _c()
    verbs = {m for v in c.app.openapi()["paths"].values() for m in v
             if m in ("put", "patch", "delete")}
    assert verbs == set(), verbs


def test_the_private_notes_path_exposes_only_get_and_post():
    """(35)"""
    c = _c()
    p = c.app.openapi()["paths"]["/api/v1/children/{child_id}/private-notes"]
    assert {m for m in p if m in ("get", "post", "put", "patch", "delete")} == {"get", "post"}


def test_no_toggle_or_mutation_route_exists():
    """(36) No post-creation mark/unmark, in either direction."""
    c = _c()
    for path in c.app.openapi()["paths"]:
        last = path.rsplit("/", 1)[-1]
        assert last not in ("mark-for-next-session", "unmark", "toggle",
                            "mark", "unflag", "edit", "archive", "restore",
                            "withdraw"), path


def test_no_toggle_or_edit_service_function_exists():
    """(37)"""
    service_files = sorted((APP_DIR / "services").rglob("*.py"))
    assert service_files, f"no services found under {APP_DIR / 'services'}"
    services = "\n".join(p.read_text() for p in service_files)
    assert services.strip(), "service sources read as empty"
    for banned in ("def update_private_note", "def edit_private_note",
                   "def delete_private_note", "def archive_private_note",
                   "def toggle_private_note", "def mark_private_note",
                   "def unmark_private_note", "def withdraw_private_note"):
        assert banned not in services, banned


def test_the_service_never_updates_an_existing_private_note():
    """(38) The one write is a create of a fresh id, never a read-modify-write."""
    writes = []
    for n in ast.walk(ast.parse(inspect.getsource(V))):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr in ("set", "create_if_absent"):
            first = n.args[0] if n.args else None
            if isinstance(first, ast.Attribute) and first.attr == "PRIVATE_THERAPIST_NOTES":
                writes.append(n.lineno)
    assert len(writes) == 1, writes


def test_creating_a_second_note_leaves_the_first_untouched():
    """(39) A different follow-up is a NEW note, never an edit."""
    c = _c()
    first = _write(c, body="First observation.", key="ap1").json()["note"]["note_id"]
    snapshot = copy.deepcopy(_repo(c).get(C.PRIVATE_THERAPIST_NOTES, first))
    second = _write(c, body="Revised observation.", key="ap2").json()["note"]["note_id"]
    assert second != first
    assert _repo(c).get(C.PRIVATE_THERAPIST_NOTES, first) == snapshot


# ── 40-45: idempotency ──────────────────────────────────────────────────────
def test_missing_idempotency_key_is_400():
    """(40)"""
    c = _c()
    r = c.post(ROUTE.format(child=CHILD), headers=HANNAH, json={"body": "x"})
    assert r.status_code == 400
    assert r.json()["error"] == "missing_idempotency_key"


def test_blank_idempotency_key_is_400():
    """(41)"""
    c = _c()
    for blank in ("", "   "):
        r = c.post(ROUTE.format(child=CHILD),
                   headers={**HANNAH, "Idempotency-Key": blank}, json={"body": "x"})
        assert r.status_code == 400, blank


def test_same_key_identical_request_replays():
    """(42)(43) No duplicate note, no duplicate audit."""
    c = _c()
    before_notes = len(_notes(c))
    first = _write(c, key="i1")
    again = _write(c, key="i1")
    assert first.status_code == again.status_code == 200
    assert again.json()["idempotent_replay"] is True
    assert again.json()["note"] == first.json()["note"]
    assert len(_notes(c)) == before_notes + 1
    assert len(_events(c, first.json()["note"]["note_id"])) == 1


def test_repeated_replays_never_duplicate():
    """(44)"""
    c = _c()
    before = len(_notes(c))
    for _ in range(5):
        assert _write(c, key="i2").status_code == 200
    assert len(_notes(c)) == before + 1
    assert len([e for e in _events(c)]) == 1


def test_same_key_different_body_is_409():
    """(45)"""
    c = _c()
    _write(c, body="Original text.", key="i3")
    before = len(_notes(c))
    r = _write(c, body="Different text.", key="i3")
    assert r.status_code == 409
    assert r.json()["error"] == "idempotency_key_conflict"
    assert len(_notes(c)) == before


def test_same_key_different_marked_flag_is_409():
    """(46) The flag participates in the request hash."""
    c = _c()
    _write(c, body="Same text.", marked=False, key="i4")
    before = len(_notes(c))
    r = _write(c, body="Same text.", marked=True, key="i4")
    assert r.status_code == 409
    assert r.json()["error"] == "idempotency_key_conflict"
    assert len(_notes(c)) == before


def test_same_key_different_child_is_409():
    """(47)"""
    c = _c()
    _write(c, key="i5")
    r = _write(c, child=NOAH_CHILD, key="i5")
    assert r.status_code == 409


def test_same_key_across_a_different_action_is_409():
    """(48) Command identity includes the action."""
    c = _c()
    assert c.post(f"/api/v1/children/{CHILD}/notes/pn_maya_1/review",
                  headers={**HANNAH, "Idempotency-Key": "i6"}).status_code == 200
    assert _write(c, key="i6").status_code == 409


def test_identical_text_under_distinct_keys_creates_two_real_notes():
    """(49) A second observation worded identically is a real second note."""
    c = _c()
    a = _write(c, body="Same words.", key="i7").json()["note"]["note_id"]
    b = _write(c, body="Same words.", key="i8").json()["note"]["note_id"]
    assert a != b
    assert _repo(c).exists(C.PRIVATE_THERAPIST_NOTES, a)
    assert _repo(c).exists(C.PRIVATE_THERAPIST_NOTES, b)


def test_the_note_id_is_deterministic_for_the_same_operation():
    """(50)"""
    c = _c()
    first = _write(c, key="i9").json()["note"]["note_id"]
    again = _write(c, key="i9").json()["note"]["note_id"]
    assert first == again


# ── 51-58: audit ────────────────────────────────────────────────────────────
def test_exactly_one_creation_audit_event():
    """(51)"""
    c = _c()
    nid = _write(c, key="d1").json()["note"]["note_id"]
    assert len(_events(c, nid)) == 1


def test_audit_event_type_follows_existing_naming():
    """(52)"""
    assert V.EVENT_TYPE == "private_therapist_note_created"
    assert V.ACTION == "create_private_therapist_note"


def test_audit_event_is_not_named_for_something_it_is_not():
    """(53) AST/value-checked: prose naming rejected alternatives must not fail."""
    BANNED = {"message_sent", "chat_started", "parent_notified", "reply_sent",
              "note_shared", "rtm_work_logged", "billing_entry_created"}
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


def test_audit_captures_actor_role_subject_and_child():
    """(54)"""
    c = _c()
    nid = _write(c, key="d2").json()["note"]["note_id"]
    e = _events(c, nid)[0]
    assert e["actor_uid"] == "dev-hannah"
    assert _plain(e["actor_role"]) == "therapist"
    assert e["subject_type"] == "private_therapist_note"
    assert e["subject_id"] == nid
    assert e["child_id"] == CHILD
    assert e["after_state"]["authored_by_therapist_id"] == HANNAH_ID
    assert e["after_state"]["visibility"] == "therapist_private"


def test_audit_captures_the_marked_flag():
    """(55)"""
    c = _c()
    for marked, key in ((True, "d3"), (False, "d4")):
        nid = _write(c, marked=marked, key=key).json()["note"]["note_id"]
        assert _events(c, nid)[0]["after_state"]["marked_for_next_session"] is marked


def test_audit_carries_request_and_environment_metadata():
    """(56)"""
    c = _c()
    nid = _write(c, key="d5").json()["note"]["note_id"]
    e = _events(c, nid)[0]
    assert e["environment"] == "dev"
    assert e["occurred_at"] and e["created_at"]
    assert "request_id" in e


def test_audit_stores_only_a_hashed_idempotency_key():
    """(57)"""
    c = _c()
    nid = _write(c, key="super-secret-private-key").json()["note"]["note_id"]
    e = _events(c, nid)[0]
    assert e["idempotency_key_hash"]
    assert "super-secret-private-key" not in str(e)


def test_the_audit_event_does_NOT_contain_the_note_body():
    """(58) THE privacy guarantee for the audit stream.

    Clinical free text must not be duplicated into an append-only, broadly
    readable audit record. Checked against the stored event, not the source.
    """
    c = _c()
    nid = _write(c, body=SECRET, key="d6").json()["note"]["note_id"]
    e = _events(c, nid)[0]
    assert SECRET not in str(e)
    assert "body" not in (e.get("before_state") or {})
    assert "body" not in (e.get("after_state") or {})


def test_no_audit_event_anywhere_contains_the_private_body():
    """(59) Sweep the whole audit collection, not just this event."""
    c = _c()
    _write(c, body=SECRET, marked=True, key="d7")
    for e in _repo(c).query(C.AUDIT_EVENTS):
        assert SECRET not in str(e), e["id"]


def test_the_service_never_writes_body_into_audit_state():
    """(60) AST: no `body` key in either audit state dict."""
    src = inspect.getsource(V)
    tree = ast.parse(src)
    offenders = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id in ("before_state", "after_state"):
                    if isinstance(n.value, ast.Dict):
                        for k in n.value.keys:
                            if isinstance(k, ast.Constant) and k.value == "body":
                                offenders.append(n.lineno)
    assert offenders == [], offenders


# ── 61-66: rollback / concurrency ───────────────────────────────────────────
def test_injected_failure_rolls_everything_back(monkeypatch):
    """(61)(62)(63)(64)"""
    c = _c()
    before = {col: copy.deepcopy(sorted(_repo(c).query(col), key=lambda r: r["id"]))
              for col in WATCHED}
    real = V.PrivateTherapistNote

    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("injected")

    monkeypatch.setattr(V, "PrivateTherapistNote", Boom)
    try:
        _write(c, key="rb1")
    except RuntimeError:
        pass
    monkeypatch.setattr(V, "PrivateTherapistNote", real)
    after = {col: sorted(_repo(c).query(col), key=lambda r: r["id"]) for col in WATCHED}
    assert after == before
    assert _events(c) == []
    assert not [r for r in _repo(c).query(C.IDEMPOTENCY_RECORDS) if r["action"] == V.ACTION]


def test_the_key_is_reusable_after_a_rolled_back_attempt(monkeypatch):
    """(65)"""
    c = _c()
    real = V.PrivateTherapistNote

    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("injected")

    monkeypatch.setattr(V, "PrivateTherapistNote", Boom)
    try:
        _write(c, key="rb2")
    except RuntimeError:
        pass
    monkeypatch.setattr(V, "PrivateTherapistNote", real)
    r = _write(c, key="rb2")
    assert r.status_code == 200
    assert _repo(c).exists(C.PRIVATE_THERAPIST_NOTES, r.json()["note"]["note_id"])


def test_concurrent_same_key_writes_produce_one_note():
    """(66)"""
    c = _c()
    before = len(_notes(c))
    out = []

    def go():
        out.append(_write(c, key="conc-same").status_code)

    ts = [threading.Thread(target=go) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert out.count(200) == 6, out
    assert len(_notes(c)) == before + 1
    assert len(_events(c)) == 1


def test_concurrent_distinct_keys_produce_distinct_notes():
    """(67)"""
    c = _c()
    before = len(_notes(c))
    out = []

    def go(i):
        out.append(_write(c, key=f"conc-{i}").status_code)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert set(out) == {200}, out
    assert len(_notes(c)) == before + 6


def test_a_write_touches_no_plan_or_parentnote_state():
    """(68)"""
    c = _c()
    cols = (C.PLAN_ASSIGNMENTS, C.WEEKLY_PLANS, C.PLAN_CHANGE_PROPOSALS,
            C.ACTIVITY_VERSIONS, C.PARENT_NOTES)
    before = {n: copy.deepcopy(sorted(_repo(c).query(n), key=lambda r: r["id"])) for n in cols}
    _write(c, key="p1")
    after = {n: sorted(_repo(c).query(n), key=lambda r: r["id"]) for n in cols}
    assert after == before


def test_only_two_collections_grow():
    """(69) The note, its audit, its idempotency record — nothing else."""
    c = _c()
    before = {col: len(_repo(c).query(col)) for col in C.ALL}
    _write(c, key="p2")
    after = {col: len(_repo(c).query(col)) for col in C.ALL}
    grew = {k for k in C.ALL if after[k] != before[k]}
    assert grew == {C.PRIVATE_THERAPIST_NOTES, C.AUDIT_EVENTS, C.IDEMPOTENCY_RECORDS}, grew


# ── 70-77: read propagation ─────────────────────────────────────────────────
def test_private_notes_get_returns_the_new_note():
    """(70)"""
    c = _c()
    nid = _write(c, body="Fresh note.", key="r1").json()["note"]["note_id"]
    items = _get_private(c).json()["items"]
    mine = [i for i in items if i["note_id"] == nid]
    assert len(mine) == 1
    assert mine[0]["body"] == "Fresh note."
    assert mine[0]["child_id"] == CHILD


def test_private_notes_get_still_contains_the_seeded_fixture():
    """(71) The new writer does not displace fixture state."""
    c = _c()
    _write(c, key="r2")
    ids = [i["note_id"] for i in _get_private(c).json()["items"]]
    assert MAYA_PRIVATE in ids


def test_next_session_includes_the_note_when_marked_true():
    """(72)(73)"""
    c = _c()
    before = [i["note_id"] for i in _next_session(c).json()["private_note_items"]]
    nid = _write(c, marked=True, key="r3").json()["note"]["note_id"]
    after = [i["note_id"] for i in _next_session(c).json()["private_note_items"]]
    assert nid not in before
    assert nid in after
    assert len(after) == len(before) + 1


def test_next_session_excludes_the_note_when_marked_false():
    """(74)"""
    c = _c()
    before = [i["note_id"] for i in _next_session(c).json()["private_note_items"]]
    nid = _write(c, marked=False, key="r4").json()["note"]["note_id"]
    after = [i["note_id"] for i in _next_session(c).json()["private_note_items"]]
    assert nid not in after
    assert after == before


def test_next_session_excludes_it_when_the_flag_is_omitted():
    """(75) The default really is false end-to-end."""
    c = _c()
    nid = _write(c, marked=None, key="r5").json()["note"]["note_id"]
    assert nid not in [i["note_id"] for i in _next_session(c).json()["private_note_items"]]
    assert nid in [i["note_id"] for i in _get_private(c).json()["items"]]


def test_next_session_parent_items_are_unchanged_by_a_private_write():
    """(76) ParentNote surfaces remain untouched."""
    c = _c()
    before = _next_session(c).json()["parent_note_items"]
    _write(c, marked=True, key="r6")
    assert _next_session(c).json()["parent_note_items"] == before


def test_no_redundant_propagation_route_was_added():
    """(77) POST shares the existing GET path: 24 paths, +1 operation."""
    c = _c()
    s = c.app.openapi()
    verbs = ("get", "post", "put", "patch", "delete")
    assert len(s["paths"]) == 24, "POST shares the existing private-notes path"
    assert sum(len([m for m in v if m in verbs]) for v in s["paths"].values()) == 26


def test_gets_remain_read_only():
    """(78)"""
    c = _c()
    _write(c, marked=True, key="r7")
    before = {n["id"]: copy.deepcopy(n) for n in _repo(c).query(C.PRIVATE_THERAPIST_NOTES)}
    audits = len(_repo(c).query(C.AUDIT_EVENTS))
    for _ in range(3):
        _get_private(c)
        _next_session(c)
        c.get(PARENT_NOTES_ROUTE.format(child=CHILD), headers=HANNAH)
    assert {n["id"]: n for n in _repo(c).query(C.PRIVATE_THERAPIST_NOTES)} == before
    assert len(_repo(c).query(C.AUDIT_EVENTS)) == audits


# ── 79-84: no ParentNote mutation, no chat, no RTM ──────────────────────────
def test_a_private_write_changes_no_parentnote_state():
    """(79) Not Reviewed, not Discuss Next Session."""
    c = _c()
    before = {n["id"]: (_plain(n["review_status"]),
                        _plain(n["session_preparation_status"]))
              for n in _repo(c).query(C.PARENT_NOTES)}
    _write(c, marked=True, key="n1")
    after = {n["id"]: (_plain(n["review_status"]),
                       _plain(n["session_preparation_status"]))
             for n in _repo(c).query(C.PARENT_NOTES)}
    assert after == before


def test_no_parent_note_is_created():
    """(80)"""
    c = _c()
    before = len(_repo(c).query(C.PARENT_NOTES))
    _write(c, key="n2")
    assert len(_repo(c).query(C.PARENT_NOTES)) == before


def test_the_service_never_touches_parent_notes():
    """(81) AST: no write to the ParentNote collection."""
    src = inspect.getsource(V)
    assert "PARENT_NOTES" not in src


def test_no_chat_or_reply_surface_was_introduced():
    """(82)"""
    c = _c()
    s = c.app.openapi()
    for p in s["paths"]:
        last = p.rsplit("/", 1)[-1]
        assert last not in ("reply", "replies", "respond", "answer", "messages",
                            "thread", "threads", "conversation", "notify"), p
    for name, sch in s["components"]["schemas"].items():
        for prop in (sch.get("properties") or {}):
            assert not any(w in prop.lower() for w in
                           ("reply", "thread", "conversation", "typing",
                            "read_receipt", "notified")), f"{name}.{prop}"


def test_no_parent_notification_concept_exists():
    """(83) Writing one notifies nobody — no collection exists to notify into."""
    c = _c()
    before = {col: len(_repo(c).query(col)) for col in C.ALL}
    _write(c, marked=True, key="n3")
    after = {col: len(_repo(c).query(col)) for col in C.ALL}
    assert {k for k in C.ALL if after[k] != before[k]} == {
        C.PRIVATE_THERAPIST_NOTES, C.AUDIT_EVENTS, C.IDEMPOTENCY_RECORDS}


def test_no_rtm_coupling():
    """(84) No minutes, monitoring days, work entries or billing documentation.

    Checked structurally, not by substring: the module docstring NAMES "RTM work
    entry" and "billing documentation" to state what a private note is NOT, so a
    text scan would fail on the prose that documents the boundary.
    """
    c = _c()
    _write(c, marked=True, key="n4")
    BANNED = ("rtm", "cpt", "billing", "billable", "minutes", "monitoring_day",
              "attestation", "payer", "episode", "clinician_time")
    # No RTM identifier is defined, called or written by the service.
    offenders = []
    for n in ast.walk(ast.parse(inspect.getsource(V))):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if any(b in n.name.lower() for b in BANNED):
                offenders.append(f"def/class {n.name}:{n.lineno}")
        if isinstance(n, ast.Name) and any(b in n.id.lower() for b in BANNED):
            offenders.append(f"name {n.id}:{n.lineno}")
        if isinstance(n, ast.Attribute) and any(b in n.attr.lower() for b in BANNED):
            offenders.append(f"attr {n.attr}:{n.lineno}")
        if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                and n.value.islower() and any(b == n.value for b in BANNED):
            offenders.append(f"literal {n.value!r}:{n.lineno}")
    assert offenders == [], offenders
    nid = [i["note_id"] for i in _get_private(c).json()["items"]][0]
    stored = _repo(c).get(C.PRIVATE_THERAPIST_NOTES, nid)
    for banned in ("rtm_minutes", "clinician_minutes", "billable", "cpt_code",
                   "monitoring_day", "work_category"):
        assert banned not in stored, banned
    assert set(stored) == {"id", "child_id", "therapist_id", "body",
                           "marked_for_next_session", "created_at",
                           "environment", "schema_version"}


def test_the_note_model_gained_no_new_field():
    """(85)"""
    assert set(PrivateTherapistNote.model_fields) == {
        "id", "child_id", "therapist_id", "body", "marked_for_next_session",
        "created_at", "environment", "schema_version"}


# ── 86-90: contract / isolation ─────────────────────────────────────────────
def test_openapi_documents_the_write_on_the_existing_path():
    """(86)"""
    c = _c()
    s = c.app.openapi()
    assert s["openapi"] == "3.1.0"
    op = s["paths"]["/api/v1/children/{child_id}/private-notes"]["post"]
    assert op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"] \
        .endswith("PrivateNoteCreateResponse")
    for code in ("400", "401", "403", "404", "409", "422"):
        assert code in op["responses"], code


def test_the_request_schema_exposes_no_server_owned_field():
    """(87)"""
    c = _c()
    props = c.app.openapi()["components"]["schemas"]["PrivateNoteCreateRequest"]["properties"]
    assert set(props) == {"body", "marked_for_next_session"}
    assert props["marked_for_next_session"]["default"] is False


def test_the_response_model_is_an_allow_list():
    """(88)"""
    assert set(S.PrivateNoteCreated.model_fields) == {
        "note_id", "child_id", "body", "marked_for_next_session", "created_at"}
    assert set(S.PrivateNoteCreateResponse.model_fields) == {"note", "idempotent_replay"}


def test_the_response_leaks_no_internals():
    """(89)"""
    c = _c()
    r = _write(c, key="c1")
    for banned in ("therapist_id", "audit", "idempotency_key", "request_hash",
                   "schema_version", "environment", "parent_id"):
        assert banned not in r.text, banned


def test_the_service_imports_nothing_external():
    """(90) AST-checked."""
    source = APP_DIR / "services" / "private_note_service.py"
    assert source.is_file(), f"service not found at {source}"
    names = []
    for n in ast.walk(ast.parse(source.read_text())):
        if isinstance(n, ast.Import):
            names += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            names.append(n.module or "")
    for nm in names:
        assert not any(b in nm.lower() for b in
                       ("firebase", "firestore", "google", "genex_core",
                        "boto3", "azure", "requests", "httpx")), nm
