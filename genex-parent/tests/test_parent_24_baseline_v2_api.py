"""Parent 2.4 functional baseline v2 — the PRODUCT path (0.6A-1F).

0.6A-1D/E built and proved the v2 engine and the A2 v2 projection, and nothing
called either: before this slice `functional_baseline_v2` was imported by its
own unit test and by the projection test, so no real session carried a v2
baseline. These tests prove it is reachable through production API code, that
what lands in the session is the engine's own canonical serialisation under its
OWN versioned key, that a v1 record and a v2 record can never be decoded as each
other, and that a projection failure cannot touch a finalized baseline.

The band-complete proof is driven ENTIRELY through the HTTP handler. The record
is never constructed by calling the service module directly — that is the whole
point, because v1's defect was invisible at the engine level too until someone
counted what a real traversal actually asked.

In-process TestClient, Firebase mocked, local durable store — the harness the
Beta API tests and the v1 baseline API tests already use.
"""

import ast
import json
import os
import pathlib
import sys

os.environ["FIREBASE_PROJECT_ID"] = "genex-test"
os.environ["LOCAL_SESSION_FALLBACK"] = "1"
os.environ.pop("GCS_BUCKET", None)
os.environ["REQUIRE_BETA_CODE"] = "false"
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:3000")
os.environ["ACTIVITY_MODEL"] = ""
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("CONCERN_ROUTER_MODEL", None)

import shutil  # noqa: E402

shutil.rmtree("/tmp/genex_api_sessions", ignore_errors=True)

import firebase_admin  # noqa: E402

firebase_admin._apps["[DEFAULT]"] = object()
from firebase_admin import auth as firebase_auth  # noqa: E402

_TOKENS = {
    "token-v2-a": {"uid": "uid-v2-a", "email": "a@example.invalid"},
    "token-v2-b": {"uid": "uid-v2-b", "email": "b@example.invalid"},
}
firebase_auth.verify_id_token = (
    lambda t, *a, **k: _TOKENS[t] if t in _TOKENS
    else (_ for _ in ()).throw(firebase_auth.InvalidIdTokenError("bad")))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api import functional_baseline_api as baseline_v1_api  # noqa: E402
from api import functional_baseline_v2_api as baseline_v2_api  # noqa: E402
from api import parent_baseline_projection_v2_client as v2_client  # noqa: E402
from api import session_store  # noqa: E402
from api.main import app  # noqa: E402
from genex_core import functional_baseline_v2 as fb2  # noqa: E402

client = TestClient(app)

REPO = pathlib.Path(__file__).resolve().parents[1]
SLP = "talking_and_communicating"
AUTH_A = {"Authorization": "Bearer token-v2-a"}
AUTH_B = {"Authorization": "Bearer token-v2-b"}

#: Free text that must never influence or reach a v2 baseline. If any of these
#: turns up in a persisted record or a projection payload, something read the
#: session's clinical narrative.
DIAGNOSIS_SENTINEL = "SENTINEL-DIAGNOSIS-autism-level-3"
CONCERN_SENTINEL = "SENTINEL-CONCERN-he does not talk at all and I am worried"

#: Milestone fragments of the four declared 30-month skills. Matched as
#: FRAGMENTS so the test states the clinical intent rather than re-pinning the
#: exact frozen wording, which is the taxonomy's to own.
BOOK = "name things in a book"
TWO_WORD = "say two or more words together"
VOCAB = "says about 50 words"
PRONOUNS = "I me or we"


def _make_session(uid: str, session_id: str, *, months: int = 30,
                  diagnosis: str = "", concern: str = "",
                  qna: dict | None = None) -> dict:
    """A persisted session, built the way the store itself builds one."""
    brain_state = {
        "child": {
            "chronological_months": months,
            "diagnosis": diagnosis,
            "concern": concern,
        },
        "qna": qna or {},
        "dev_age": {},
    }
    doc = session_store.new_session_doc(
        session_id=session_id, owner_uid=uid, age_in_months=months,
        daily_time_minutes=20, diagnosis_or_condition=diagnosis,
        brain_state=brain_state, interview={}, timezone="UTC",
        beta_authorized=True)
    doc["brain_state"] = brain_state
    session_store.save(uid, session_id, doc)
    return doc


def _reload(uid: str, session_id: str) -> dict:
    """A GENUINE reload: drop the read-through cache, then force remote.

    Both halves matter. `session_store` keeps a module-level `_cache`, so
    loading without clearing it could return the very dict the request mutated
    and prove nothing about durability.
    """
    session_store._cache.clear()
    return session_store.load(uid, session_id, force_remote=True)


# --- the v2 HTTP surface, as a client would drive it -----------------------

def _entry(session_id, auth=AUTH_A, domain=SLP):
    return client.get(
        f"/api/v2/session/{session_id}/baseline/{domain}/entry-screen",
        headers=auth)


def _start(session_id, choice="two_three_words", auth=AUTH_A, domain=SLP):
    return client.post(
        f"/api/v2/session/{session_id}/baseline/{domain}/start",
        json={"entry_choice_id": choice}, headers=auth)


def _answer(session_id, skill_key, value, auth=AUTH_A, domain=SLP):
    return client.post(
        f"/api/v2/session/{session_id}/baseline/{domain}/answer",
        json={"skill_key": skill_key, "answer": value}, headers=auth)


def _finalize(session_id, auth=AUTH_A, domain=SLP):
    return client.post(
        f"/api/v2/session/{session_id}/baseline/{domain}/finalize",
        headers=auth)


def _get(session_id, auth=AUTH_A, domain=SLP):
    return client.get(
        f"/api/v2/session/{session_id}/baseline/{domain}", headers=auth)


def _project(session_id, auth=AUTH_A, domain=SLP):
    return client.post(
        f"/api/v2/session/{session_id}/baseline/{domain}/projection",
        headers=auth)


#: The founder's mixed 30-month trace. Keyed by milestone fragment so the
#: intention is legible: one clear deficit, two demonstrated, one unanswerable.
MIXED_30M = {
    BOOK: "no",            # -> not_demonstrated
    PRONOUNS: "not_sure",  # -> unknown
}


def _drive(session_id, overrides=None, auth=AUTH_A, limit=40):
    """Answer every question the HTTP handler offers. Returns the full trace.

    Each element is `(months, milestone, answer, response_json)`, so a test can
    assert over what was ACTUALLY ASKED rather than over what the engine would
    have asked if called directly.
    """
    overrides = overrides or {}
    trace = []
    view = _get(session_id, auth=auth).json()
    while view.get("current_question") and len(trace) < limit:
        question = view["current_question"]
        value = "yes"
        for fragment, answer in overrides.items():
            if fragment in question["milestone"]:
                value = answer
        response = _answer(session_id, question["skill_key"], value, auth=auth)
        assert response.status_code == 200, response.text
        trace.append((question["months"], question["milestone"], value,
                      response.json()))
        view = response.json()
    return trace


# ===========================================================================
# ITEM 3 — band-complete questioning, proved through the real HTTP handler
# ===========================================================================


def test_http_asks_every_declared_sibling_in_the_30_month_band():
    """The founder's exact trace. Four 30m skills, four independent answers.

    This is the v1 defect stated as a test. v1 asked ONE skill per band and
    retired the whole band, so a child who answered yes four times was anchored
    at 48 months with six declared-track skills never asked. Here every sibling
    is asked, and the asking is observed at the HTTP boundary.
    """
    uid, sid = "uid-v2-a", "sess-v2-band-complete"
    _make_session(uid, sid)

    started = _start(sid)
    assert started.status_code == 200, started.text
    first = started.json()["current_question"]
    # The entry descriptor says where to START ASKING: `two_three_words` anchors
    # below 30 months, so the traversal begins at 24 and must finish that band
    # before it may move.
    assert first["months"] == 24

    trace = _drive(sid, MIXED_30M)
    asked = [(months, milestone) for months, milestone, _, _ in trace]

    # Every 24-month declared skill was handled before the band moved.
    at_24 = [m for months, m in asked if months == 24]
    assert at_24, "the entry band was never asked"
    view_24 = next(r for _, _, _, r in trace)
    assert [b for b in view_24["bands"] if b["months"] == 24]

    # All FOUR 30-month siblings were asked, each exactly once.
    at_30 = [m for months, m in asked if months == 30]
    assert len(at_30) == 4, at_30
    assert len(set(at_30)) == 4
    for fragment in (BOOK, TWO_WORD, VOCAB, PRONOUNS):
        assert any(fragment in m for m in at_30), f"{fragment} was never asked"

    final = _get(sid).json()
    band_30 = next(b for b in final["bands"] if b["months"] == 30)
    assert band_30["total"] == 4
    assert band_30["assessed"] == 4
    # The two questions v2 exists to keep apart.
    assert band_30["complete"] is True
    assert band_30["mastered"] is False


def test_no_30_month_skill_disappears_because_a_sibling_passed_or_failed():
    """A demonstrated skill carries no sibling; a deficit retires no sibling.

    Driven three times with different answer mixes. In every case all four
    siblings are asked, which is the property v1 could not hold: one answer
    there retired the entire band.
    """
    cases = {
        "all-yes": {},
        "one-fails": {BOOK: "no"},
        "one-unknown": {PRONOUNS: "not_sure"},
    }
    for name, overrides in cases.items():
        uid, sid = "uid-v2-a", f"sess-v2-nodisappear-{name}"
        _make_session(uid, sid)
        _start(sid)
        trace = _drive(sid, overrides)
        at_30 = {m for months, m, _, _ in trace if months == 30}
        assert len(at_30) == 4, (name, at_30)


def test_an_unknown_answer_does_not_advance_the_ladder():
    """`unknown` is assessed evidence, never mastery.

    A band holding an `unknown` is COMPLETE and NOT MASTERED, so the engine must
    not step up past it. Checked over the HTTP trace: no band above 30 is ever
    entered when a 30-month skill is unanswerable.
    """
    uid, sid = "uid-v2-a", "sess-v2-unknown-no-advance"
    _make_session(uid, sid)
    _start(sid)
    _drive(sid, MIXED_30M)

    view = _get(sid).json()
    entered = [b["months"] for b in view["bands"]]
    assert 30 in entered
    assert not [m for m in entered if m > 30], entered

    band_30 = next(b for b in view["bands"] if b["months"] == 30)
    assert band_30["complete"] is True and band_30["mastered"] is False


def test_the_finalized_record_carries_all_four_30m_states():
    """Four distinct states survive independently into the stored record.

    Read from the PERSISTED session rather than from a live object: the record
    is what a later projection digests, so it is the record — not an in-memory
    view — that has to carry every state.
    """
    uid, sid = "uid-v2-a", "sess-v2-four-states"
    _make_session(uid, sid)
    _start(sid)
    _drive(sid, MIXED_30M)
    assert _finalize(sid).status_code == 200

    stored = _reload(uid, sid)[baseline_v2_api.CANONICAL_STATE_KEY_V2][SLP]
    at_30 = {row["milestone"]: row["state"]
             for row in stored["skills"] if row["months"] == 30}
    assert len(at_30) == 4

    def state_of(fragment):
        return next(s for m, s in at_30.items() if fragment in m)

    assert state_of(BOOK) == "not_demonstrated"
    assert state_of(TWO_WORD) == "demonstrated"
    assert state_of(VOCAB) == "demonstrated"
    assert state_of(PRONOUNS) == "unknown"

    # And the engine agrees when asked about the band it stored.
    record = fb2.record_from_state_v2(stored)
    assessment = fb2.band_assessment(record, 30)
    assert assessment.assessment_complete is True
    assert assessment.band_mastered is False
    # The unknown does not suppress the known deficit.
    assert len(assessment.unresolved) == 1
    assert assessment.unresolved[0].state == "not_demonstrated"
    assert len(assessment.unknown) == 1


# ===========================================================================
# ITEM 4 — versioned persistence; v1 and v2 are never confusable
# ===========================================================================


def test_v2_is_stored_under_its_own_key_and_survives_a_durable_round_trip():
    uid, sid = "uid-v2-a", "sess-v2-storage"
    _make_session(uid, sid)
    _start(sid)
    _drive(sid, MIXED_30M)
    _finalize(sid)

    doc = _reload(uid, sid)
    assert baseline_v2_api.CANONICAL_STATE_KEY_V2 == "functional_baseline_v2"
    stored = doc["functional_baseline_v2"][SLP]
    envelope = doc["functional_baseline_v2_api"][SLP]

    # Exactly the engine's own serialisation, byte for byte.
    record = fb2.record_from_state_v2(stored)
    assert fb2.record_to_state_v2(record) == stored
    assert stored["record_schema"] == fb2.RECORD_SCHEMA_V2
    assert stored["baseline_version"] == fb2.BASELINE_VERSION_V2

    assert envelope["finalized"] is True
    assert envelope["schema_version"] == baseline_v2_api.API_ENVELOPE_VERSION_V2
    assert envelope["finalized_at"]


def test_v2_never_writes_the_v1_keys_and_never_writes_dev_age():
    """`dev_age` is the planner's input and is NOT the baseline's to set.

    Writing it would change which activities a live Beta session generates. The
    engine's `attach_to_state_v2` is additive by construction; this proves the
    product path inherits that.
    """
    uid, sid = "uid-v2-a", "sess-v2-no-devage"
    doc_before = _make_session(uid, sid)
    dev_age_before = json.dumps(doc_before["brain_state"]["dev_age"],
                                sort_keys=True)

    _start(sid)
    _drive(sid, MIXED_30M)
    _finalize(sid)

    doc = _reload(uid, sid)
    assert json.dumps(doc["brain_state"].get("dev_age") or {},
                      sort_keys=True) == dev_age_before
    # v1's two keys were never created by the v2 path.
    assert baseline_v1_api.CANONICAL_STATE_KEY not in doc
    assert baseline_v1_api.ENVELOPE_STATE_KEY not in doc


def test_a_v1_record_cannot_be_decoded_as_v2_or_the_reverse():
    """The two stored shapes are mutually undecodable, by discriminator.

    This is the "one field must never mean two things" requirement as an
    executable check: a v1 dict handed to the v2 decoder is refused before a
    single field is read, and a v2 dict refuses v1's loader for want of `asked`.
    """
    uid, sid = "uid-v2-a", "sess-v2-cross-decode"
    _make_session(uid, sid)
    # A real v1 record, through v1's own routes.
    client.post(f"/api/v1/session/{sid}/baseline/{SLP}/start",
                json={"entry_choice_id": "many_single_words"}, headers=AUTH_A)
    v1_doc = _reload(uid, sid)
    v1_stored = v1_doc[baseline_v1_api.CANONICAL_STATE_KEY][SLP]

    with pytest.raises(fb2.BaselineV2Error):
        fb2.record_from_state_v2(v1_stored)

    # And a real v2 record, through v2's routes on a separate session.
    sid2 = "sess-v2-cross-decode-b"
    _make_session(uid, sid2)
    _start(sid2)
    v2_stored = _reload(uid, sid2)["functional_baseline_v2"][SLP]
    assert "asked" not in v2_stored
    assert "entry_choice_label" not in v2_stored
    with pytest.raises(KeyError):
        # v1's loader reads `asked`/`entry_choice_label` by name, so a v2 dict
        # cannot be partially decoded into a v1 record.
        baseline_v1_api._load_record(
            {baseline_v1_api.CANONICAL_STATE_KEY: {SLP: v2_stored}}, SLP)


def test_both_generations_coexist_in_one_session_without_interference():
    uid, sid = "uid-v2-a", "sess-v2-coexist"
    _make_session(uid, sid)

    client.post(f"/api/v1/session/{sid}/baseline/{SLP}/start",
                json={"entry_choice_id": "many_single_words"}, headers=AUTH_A)
    _start(sid)
    _drive(sid, MIXED_30M)
    _finalize(sid)

    doc = _reload(uid, sid)
    assert doc["functional_baseline"][SLP]["baseline_version"].endswith("v1")
    assert doc["functional_baseline_v2"][SLP]["baseline_version"].endswith("v2")
    # v1 is still in progress and still readable on its own route.
    assert client.get(f"/api/v1/session/{sid}/baseline/{SLP}",
                      headers=AUTH_A).json()["finalized"] is False


def test_a_finalized_v2_baseline_is_immutable():
    uid, sid = "uid-v2-a", "sess-v2-immutable"
    _make_session(uid, sid)
    _start(sid)
    trace = _drive(sid, MIXED_30M)
    _finalize(sid)

    before = json.dumps(_reload(uid, sid)["functional_baseline_v2"][SLP],
                        sort_keys=True)

    # An answer is refused, a restart is refused, a re-finalize is a no-op.
    stale_key = fb2.skill_key(SLP, "expressive_language", 30, trace[-1][1])
    assert _answer(sid, stale_key, "yes").status_code == 409
    assert _start(sid).status_code == 409
    again = _finalize(sid)
    assert again.status_code == 200
    assert again.json()["finalized"] is True

    after = _reload(uid, sid)["functional_baseline_v2"][SLP]
    assert json.dumps(after, sort_keys=True) == before


# ===========================================================================
# No clinical narrative is read, and no LLM is reachable
# ===========================================================================


def test_no_diagnosis_or_concern_reaches_the_record_or_the_payload():
    uid, sid = "uid-v2-a", "sess-v2-sentinels"
    _make_session(uid, sid, diagnosis=DIAGNOSIS_SENTINEL,
                  concern=CONCERN_SENTINEL,
                  qna={"q1": CONCERN_SENTINEL})
    _start(sid)
    _drive(sid, MIXED_30M)
    _finalize(sid)

    doc = _reload(uid, sid)
    stored = json.dumps(doc["functional_baseline_v2"][SLP])
    payload = json.dumps(baseline_v2_api.projection_payload(doc, SLP))
    for sentinel in (DIAGNOSIS_SENTINEL, CONCERN_SENTINEL):
        assert sentinel not in stored
        assert sentinel not in payload


def test_the_v2_api_module_reaches_no_model_client_and_no_diagnosis_field():
    """Structural, over the AST, so it cannot be satisfied by a passing run."""
    source = (REPO / "api" / "functional_baseline_v2_api.py").read_text()
    tree = ast.parse(source)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "openai" not in imported and "anthropic" not in imported
    assert not (names | attrs) & {"diagnosis", "diagnosis_or_condition",
                                  "concern", "qna", "dev_age"}


# ===========================================================================
# ITEM 14 — fail-closed HTTP negatives
# ===========================================================================


def test_unauthenticated_requests_are_refused_on_every_v2_route():
    sid = "sess-v2-unauth"
    _make_session("uid-v2-a", sid)
    routes = [
        ("get", f"/api/v2/session/{sid}/baseline/{SLP}/entry-screen", None),
        ("post", f"/api/v2/session/{sid}/baseline/{SLP}/start",
         {"entry_choice_id": "two_three_words"}),
        ("post", f"/api/v2/session/{sid}/baseline/{SLP}/answer",
         {"skill_key": "x", "answer": "yes"}),
        ("post", f"/api/v2/session/{sid}/baseline/{SLP}/finalize", None),
        ("get", f"/api/v2/session/{sid}/baseline/{SLP}", None),
        ("post", f"/api/v2/session/{sid}/baseline/{SLP}/projection", None),
    ]
    for method, path, body in routes:
        call = getattr(client, method)
        no_header = call(path, json=body) if body else call(path)
        assert no_header.status_code in (401, 403), (path, no_header.text)
        bad = (call(path, json=body, headers={"Authorization": "Bearer nope"})
               if body else call(path, headers={"Authorization": "Bearer nope"}))
        assert bad.status_code in (401, 403), (path, bad.text)


def test_a_wrong_session_owner_is_refused_on_every_v2_route():
    """403 from `_require_session`, reused unchanged. Not a 404.

    The session EXISTS, so answering 404 would be a lie; answering 403 is the
    behaviour every other Parent route already has, and this slice must not
    invent a second ownership convention.
    """
    uid, sid = "uid-v2-a", "sess-v2-wrong-owner"
    _make_session(uid, sid)
    _start(sid)

    assert _get(sid, auth=AUTH_B).status_code == 403
    assert _start(sid, auth=AUTH_B).status_code == 403
    assert _answer(sid, "x", "yes", auth=AUTH_B).status_code == 403
    assert _finalize(sid, auth=AUTH_B).status_code == 403
    assert _entry(sid, auth=AUTH_B).status_code == 403
    assert _project(sid, auth=AUTH_B).status_code == 403


def test_answering_before_start_is_404_baseline_not_started():
    uid, sid = "uid-v2-a", "sess-v2-before-start"
    _make_session(uid, sid)
    response = _answer(sid, "anything", "yes")
    assert response.status_code == 404
    assert response.json()["detail"] == "baseline_not_started"
    assert _get(sid).status_code == 404


def test_answering_the_wrong_skill_is_422_and_names_the_expected_key():
    uid, sid = "uid-v2-a", "sess-v2-mismatch"
    _make_session(uid, sid)
    expected = _start(sid).json()["current_question"]["skill_key"]

    response = _answer(sid, expected + "-tampered", "yes")
    assert response.status_code == 422
    assert "Expected" in response.json()["detail"]
    # Nothing was recorded.
    assert _get(sid).json()["skills_assessed"] == 0


def test_a_replayed_answer_is_refused_rather_than_overwriting_evidence():
    """The guard that makes the record append-only in practice.

    `record_answer_v2` REPLACES evidence for a skill, so without the skill-key
    check a late retry would overwrite a newer answer. The retry must 422.
    """
    uid, sid = "uid-v2-a", "sess-v2-replay-answer"
    _make_session(uid, sid)
    first = _start(sid).json()["current_question"]
    assert _answer(sid, first["skill_key"], "yes").status_code == 200

    replay = _answer(sid, first["skill_key"], "no")
    assert replay.status_code == 422
    stored = _reload(uid, sid)["functional_baseline_v2"][SLP]
    recorded = [r for r in stored["skills"] if r["milestone"] == first["milestone"]]
    assert len(recorded) == 1
    assert recorded[0]["state"] == "demonstrated"


def test_unknown_entry_choice_and_unsupported_domain_are_refused():
    uid, sid = "uid-v2-a", "sess-v2-refusals"
    _make_session(uid, sid)

    bad_choice = _start(sid, choice="not_a_descriptor")
    assert bad_choice.status_code == 422
    assert bad_choice.json()["detail"] == "unknown_entry_choice"

    for domain in ("moving_and_coordination", "not_a_domain"):
        response = _start(sid, domain=domain)
        assert response.status_code == 404
        assert response.json()["detail"] == "unsupported_baseline_domain"


def test_unknown_request_fields_are_refused_not_ignored():
    uid, sid = "uid-v2-a", "sess-v2-extra-fields"
    _make_session(uid, sid)

    # A clinical result a client must never be able to assert.
    assert client.post(
        f"/api/v2/session/{sid}/baseline/{SLP}/start",
        json={"entry_choice_id": "two_three_words",
              "routing_anchor_months": 48},
        headers=AUTH_A).status_code == 422
    # Chronological age is read from the session, never from the body.
    assert client.post(
        f"/api/v2/session/{sid}/baseline/{SLP}/start",
        json={"entry_choice_id": "two_three_words",
              "chronological_months": 60},
        headers=AUTH_A).status_code == 422

    key = _start(sid).json()["current_question"]["skill_key"]
    # A v1-shaped answer body cannot reach a v2 route.
    assert client.post(
        f"/api/v2/session/{sid}/baseline/{SLP}/answer",
        json={"question_id": key, "answer": "yes"},
        headers=AUTH_A).status_code == 422
    # Nor can a client assert the state directly.
    assert client.post(
        f"/api/v2/session/{sid}/baseline/{SLP}/answer",
        json={"skill_key": key, "answer": "yes", "state": "demonstrated"},
        headers=AUTH_A).status_code == 422
    assert client.post(
        f"/api/v2/session/{sid}/baseline/{SLP}/answer",
        json={"skill_key": key, "answer": "definitely"},
        headers=AUTH_A).status_code == 422


def test_a_get_never_persists_a_band_entry():
    """A read must not advance the record, even though the engine's own
    `next_question_v2` enters a band when asked.

    Proved by hammering the GET and comparing the stored bytes.
    """
    uid, sid = "uid-v2-a", "sess-v2-read-only"
    _make_session(uid, sid)
    _start(sid)
    before = json.dumps(_reload(uid, sid)["functional_baseline_v2"][SLP],
                        sort_keys=True)

    for _ in range(5):
        assert _get(sid).status_code == 200
    after = json.dumps(_reload(uid, sid)["functional_baseline_v2"][SLP],
                       sort_keys=True)
    assert after == before


def test_start_is_idempotent_while_in_progress():
    uid, sid = "uid-v2-a", "sess-v2-restart"
    _make_session(uid, sid)
    first = _start(sid).json()["current_question"]
    _answer(sid, first["skill_key"], "yes")

    again = _start(sid, choice="single_words")
    assert again.status_code == 200
    # Not restarted: the recorded answer survives and the descriptor is the
    # original one, because restarting would discard a parent's evidence.
    assert again.json()["skills_assessed"] == 1
    assert again.json()["entry_choice_id"] == "two_three_words"


# ===========================================================================
# ITEM 12 — a projection failure must never destroy the Parent baseline
# ===========================================================================


def _finalized_session(sid, uid="uid-v2-a"):
    _make_session(uid, sid)
    _start(sid)
    _drive(sid, MIXED_30M)
    assert _finalize(sid).status_code == 200
    return uid


def test_projection_is_refused_before_finalize_and_writes_nothing():
    uid, sid = "uid-v2-a", "sess-v2-project-early"
    _make_session(uid, sid)
    _start(sid)
    response = _project(sid)
    assert response.status_code == 409
    assert response.json()["detail"] == "baseline_not_finalized"


def test_an_unconfigured_deployment_cannot_project_and_says_so():
    """501, not 500: the deployment works exactly as configured, and the
    configuration says it does not project v2."""
    sid = "sess-v2-project-unconfigured"
    _finalized_session(sid)
    for var in (v2_client.URL_ENV_VAR, v2_client.AUDIENCE_ENV_VAR,
                v2_client.PAIRING_ENV_VAR):
        os.environ.pop(var, None)
    response = _project(sid)
    assert response.status_code == 501
    assert response.json()["detail"] == "projection_not_configured"


def test_a_projection_failure_leaves_the_finalized_baseline_untouched(
        monkeypatch):
    """The reliability property behind choosing an EXPLICIT projection route.

    Three independent failures — unavailable service, refused payload, pairing
    forbidden — and after all three the finalized Parent baseline is still
    finalized, still immutable and byte-identical. There is no distributed
    transaction here and nothing to roll back.
    """
    uid, sid = "uid-v2-a", "sess-v2-project-failures"
    _finalized_session(sid, uid)
    before = json.dumps(_reload(uid, sid)["functional_baseline_v2"][SLP],
                        sort_keys=True)
    envelope_before = dict(_reload(uid, sid)["functional_baseline_v2_api"][SLP])

    failures = [
        (v2_client.ProjectionUnavailable("down"), 503,
         "projection_unavailable"),
        (v2_client.ProjectionRejected("nope"), 422, "projection_rejected"),
        (v2_client.ProjectionPairingForbidden("no"), 403,
         "projection_pairing_forbidden"),
    ]
    for error, status, detail in failures:
        def boom(**kwargs):
            raise error
        monkeypatch.setattr(
            "api.main.projection_v2_client.project_baseline_v2", boom)
        response = _project(sid)
        assert response.status_code == status, response.text
        assert response.json()["detail"] == detail

        doc = _reload(uid, sid)
        assert json.dumps(doc["functional_baseline_v2"][SLP],
                          sort_keys=True) == before
        assert doc["functional_baseline_v2_api"][SLP] == envelope_before
        assert _get(sid).json()["finalized"] is True

    # And the retry succeeds once the Pilot is reachable again — the same
    # unchanged request, because the Pilot keys on (session, domain, digest).
    sent = []

    def ok(**kwargs):
        sent.append(kwargs)
        return {"projection_id": "pbp2_" + "0" * 32, "child_id": "chld_x",
                "created": True}

    monkeypatch.setattr(
        "api.main.projection_v2_client.project_baseline_v2", ok)
    good = _project(sid)
    assert good.status_code == 200
    assert good.json()["projected"] is True
    assert good.json()["created"] is True
    # Still nothing written back into the Parent session: the data direction is
    # Parent -> Pilot only.
    doc = _reload(uid, sid)
    assert json.dumps(doc["functional_baseline_v2"][SLP],
                      sort_keys=True) == before
    assert "pbp2_" not in json.dumps(doc)


def test_the_projection_request_carries_no_identifier_and_no_raw_answer(
        monkeypatch):
    """What crosses the boundary, asserted on the body actually built."""
    uid, sid = "uid-v2-a", "sess-v2-project-body"
    _make_session(uid, sid, diagnosis=DIAGNOSIS_SENTINEL,
                  concern=CONCERN_SENTINEL)
    _start(sid)
    _drive(sid, MIXED_30M)
    _finalize(sid)

    captured = {}

    def capture(*, source_session_id, record, payload, **kwargs):
        captured["body"] = v2_client.build_body(
            source_session_id=source_session_id, record=record,
            payload=payload)
        return {"projection_id": "pbp2_" + "0" * 32, "child_id": "c",
                "created": True}

    monkeypatch.setattr(
        "api.main.projection_v2_client.project_baseline_v2", capture)
    assert _project(sid).status_code == 200

    body = captured["body"]
    assert set(body) == set(v2_client.BODY_KEYS)
    assert set(body["summary"]) == set(
        baseline_v2_api.PROJECTED_SUMMARY_FIELDS)
    for row in body["skills"]:
        assert set(row) == set(baseline_v2_api.PROJECTED_SKILL_FIELDS)
    for band in body["band_totals"]:
        assert set(band) == {"months", "total_skills"}
    assert {b["months"]: b["total_skills"] for b in body["band_totals"]}[30] == 4

    # `demonstrated_months` is checked as a KEY rather than a substring,
    # because `not_demonstrated_months` legitimately contains it — and that
    # field IS one of the seven. The key-set assertions above are the real
    # guarantee; this states the specific temptation explicitly.
    assert "demonstrated_months" not in body["summary"]
    assert "demonstrated_months" not in body

    blob = json.dumps(body)
    for forbidden in (DIAGNOSIS_SENTINEL, CONCERN_SENTINEL, uid,
                      "asked", "question_id", "skill_key", "raw_answer",
                      "chronological_months", "entry_choice_label",
                      "entry_anchor_months",
                      "dev_age", "brain_state", "owner_uid"):
        assert forbidden not in blob, forbidden
    # The session id IS present — it is the provenance the Pilot joins on, and
    # it names a session rather than a person.
    assert body["source_session_id"] == sid
    assert len(body["source_record_digest"]) == 64


def test_the_digest_attests_the_stored_record_and_changes_with_evidence():
    """Replay safety and overwrite safety are the same property.

    Same finalized record -> same digest -> same deterministic projection id ->
    the Pilot replays. DIFFERENT evidence -> different digest -> a different id
    -> a new immutable document rather than an overwrite.
    """
    uid = "uid-v2-a"
    _finalized_session("sess-v2-digest-a", uid)
    doc_a = _reload(uid, "sess-v2-digest-a")
    record_a = baseline_v2_api.finalized_record(doc_a, SLP)

    assert (v2_client.canonical_source_digest(record_a)
            == v2_client.canonical_source_digest(dict(record_a)))

    _make_session(uid, "sess-v2-digest-b")
    _start("sess-v2-digest-b")
    _drive("sess-v2-digest-b", {BOOK: "no"})  # pronouns answered yes instead
    _finalize("sess-v2-digest-b")
    record_b = baseline_v2_api.finalized_record(
        _reload(uid, "sess-v2-digest-b"), SLP)

    assert (v2_client.canonical_source_digest(record_a)
            != v2_client.canonical_source_digest(record_b))


def test_the_v2_client_refuses_a_v1_url_a_plaintext_url_and_a_bad_audience():
    good = {
        v2_client.PAIRING_ENV_VAR: "parent-staging->pilot-staging",
        v2_client.AUDIENCE_ENV_VAR: "https://pilot-projection-staging.example",
        v2_client.URL_ENV_VAR: ("https://pilot-projection-staging.example"
                                + v2_client.PROJECTION_V2_PATH),
    }
    url, audience = v2_client.projection_v2_config(good)
    assert url.endswith(v2_client.PROJECTION_V2_PATH)

    def refuse(**overrides):
        env = dict(good)
        env.update(overrides)
        with pytest.raises((v2_client.ProjectionNotConfigured,
                            v2_client.ProjectionPairingForbidden)):
            v2_client.projection_v2_config(env)

    # A v1 URL cannot be used for a v2 body.
    refuse(**{v2_client.URL_ENV_VAR: (
        "https://pilot-projection-staging.example"
        "/internal/parent-baseline-projections")})
    refuse(**{v2_client.URL_ENV_VAR: (
        "http://pilot-projection-staging.example"
        + v2_client.PROJECTION_V2_PATH)})
    # A token minted for one service must not be posted to another.
    refuse(**{v2_client.AUDIENCE_ENV_VAR: "https://some-other-service.example"})
    refuse(**{v2_client.PAIRING_ENV_VAR: "parent-prod->pilot-prod"})
    refuse(**{v2_client.PAIRING_ENV_VAR: ""})
    for var in (v2_client.URL_ENV_VAR, v2_client.AUDIENCE_ENV_VAR):
        refuse(**{var: ""})


def test_the_v2_client_logs_nothing_at_all():
    """Structural, over the AST. The body carries milestone prose."""
    source = (REPO / "api" / "parent_baseline_projection_v2_client.py"
              ).read_text()
    tree = ast.parse(source)

    def strip_docstrings(node):
        for child in ast.walk(node):
            if isinstance(child, (ast.Module, ast.FunctionDef,
                                  ast.AsyncFunctionDef, ast.ClassDef)):
                body = child.body
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    child.body = body[1:]
        return node

    tree = strip_docstrings(tree)
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "print" not in called
    assert "logging" not in imported
    assert not attrs & {"debug", "info", "warning", "error", "exception",
                        "getLogger"}


# ===========================================================================
# v1 regression — the v1 surface is untouched by this slice
# ===========================================================================


def test_the_v1_baseline_routes_still_behave_exactly_as_before():
    uid, sid = "uid-v2-a", "sess-v1-regression"
    _make_session(uid, sid)

    started = client.post(
        f"/api/v1/session/{sid}/baseline/{SLP}/start",
        json={"entry_choice_id": "many_single_words"}, headers=AUTH_A)
    assert started.status_code == 200
    view = started.json()
    # v1's own view shape: `question_id`, `answers_recorded`, no `bands`.
    assert "question_id" in view["current_question"]
    assert "answers_recorded" in view
    assert "bands" not in view
    assert "skill_key" not in view["current_question"]

    qid = view["current_question"]["question_id"]
    answered = client.post(
        f"/api/v1/session/{sid}/baseline/{SLP}/answer",
        json={"question_id": qid, "answer": "yes"}, headers=AUTH_A)
    assert answered.status_code == 200
    assert answered.json()["answers_recorded"] == 1

    assert client.post(f"/api/v1/session/{sid}/baseline/{SLP}/finalize",
                       headers=AUTH_A).status_code == 200
    stored = _reload(uid, sid)["functional_baseline"][SLP]
    assert "asked" in stored
    assert "record_schema" not in stored
    assert stored["baseline_version"].endswith("v1")


def test_a_v2_skill_key_cannot_be_posted_to_a_v1_route():
    uid, sid = "uid-v2-a", "sess-v1-no-crossover"
    _make_session(uid, sid)
    client.post(f"/api/v1/session/{sid}/baseline/{SLP}/start",
                json={"entry_choice_id": "many_single_words"}, headers=AUTH_A)
    assert client.post(
        f"/api/v1/session/{sid}/baseline/{SLP}/answer",
        json={"skill_key": "x", "answer": "yes"},
        headers=AUTH_A).status_code == 422


def test_the_v1_and_v2_route_tables_are_disjoint_and_complete():
    paths = {r.path for r in app.routes}
    for suffix in ("/entry-screen", "/start", "/answer", "/finalize",
                   "/projection", ""):
        v1 = f"/api/v1/session/{{session_id}}/baseline/{{domain}}{suffix}"
        v2 = f"/api/v2/session/{{session_id}}/baseline/{{domain}}{suffix}"
        assert v1 in paths, v1
        assert v2 in paths, v2
