"""Parent 2.4 functional baseline — the PRODUCT path (0.5F-A1).

Before this slice `genex_core/functional_baseline.py` was imported by exactly
two files in the repository: itself and its own unit test. The engine was
frozen and correct and completely unreachable, so no real session ever carried
a baseline. These tests prove it is now reachable through production API code,
that what lands in the session is the engine's own canonical serialisation,
and that it survives a durable round trip.

In-process TestClient, Firebase mocked, local durable store — the harness the
Beta API tests already use. One group additionally drives the REAL GCS adapter
through a fake storage client, so the blob path and the JSON round trip are
exercised rather than assumed.
"""

import ast
import copy
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
    "token-parent-a": {"uid": "uid-parent-a", "email": "a@example.invalid"},
    "token-parent-b": {"uid": "uid-parent-b", "email": "b@example.invalid"},
}
firebase_auth.verify_id_token = (
    lambda t, *a, **k: _TOKENS[t] if t in _TOKENS
    else (_ for _ in ()).throw(firebase_auth.InvalidIdTokenError("bad")))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api import functional_baseline_api as baseline_api  # noqa: E402
from api import session_store  # noqa: E402
from api.main import app  # noqa: E402
from genex_core import functional_baseline as fb  # noqa: E402

client = TestClient(app)

REPO = pathlib.Path(__file__).resolve().parents[1]
SLP = "talking_and_communicating"
AUTH_A = {"Authorization": "Bearer token-parent-a"}
AUTH_B = {"Authorization": "Bearer token-parent-b"}

#: Free text that must never influence or reach the baseline. If any of these
#: strings turns up in a persisted baseline record, something read the session's
#: clinical narrative.
DIAGNOSIS_SENTINEL = "SENTINEL-DIAGNOSIS-autism-level-3"
CONCERN_SENTINEL = "SENTINEL-CONCERN-he does not talk at all and I am worried"


def _make_session(uid: str, session_id: str, *, months: int = 36,
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
    loading without clearing it could return the very dict the request
    mutated and prove nothing about durability.
    """
    session_store._cache.clear()
    return session_store.load(uid, session_id, force_remote=True)


def _start(session_id, choice="many_single_words", auth=AUTH_A, domain=SLP):
    return client.post(
        f"/api/v1/session/{session_id}/baseline/{domain}/start",
        json={"entry_choice_id": choice}, headers=auth)


def _answer(session_id, question_id, value, auth=AUTH_A, domain=SLP):
    return client.post(
        f"/api/v1/session/{session_id}/baseline/{domain}/answer",
        json={"question_id": question_id, "answer": value}, headers=auth)


def _finalize(session_id, auth=AUTH_A, domain=SLP):
    return client.post(
        f"/api/v1/session/{session_id}/baseline/{domain}/finalize",
        headers=auth)


def _get(session_id, auth=AUTH_A, domain=SLP):
    return client.get(
        f"/api/v1/session/{session_id}/baseline/{domain}", headers=auth)


def _run_to_completion(session_id, answers, auth=AUTH_A):
    """Answer until the engine stops asking. Returns the last view."""
    view = _start(session_id, auth=auth).json()
    index = 0
    while view.get("current_question"):
        value = answers[index] if index < len(answers) else "no"
        index += 1
        response = _answer(session_id, view["current_question"]["question_id"],
                           value, auth=auth)
        assert response.status_code == 200, response.text
        view = response.json()
    return view


# ---------------------------------------------------------------------------
# 1. the engine is reachable from PRODUCTION code, not only from tests
# ---------------------------------------------------------------------------

def test_production_api_code_imports_the_frozen_baseline_engine():
    """The whole point of 0.5F-A1. Asserted over the AST of shipped modules,
    so it cannot be satisfied by a test-only import."""
    source = (REPO / "api" / "functional_baseline_api.py").read_text()
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "genex_core.functional_baseline" in imported


def test_main_wires_the_baseline_module_into_the_app():
    source = (REPO / "api" / "main.py").read_text()
    assert "functional_baseline_api" in source
    routes = {r.path for r in app.routes}
    for path in (
        "/api/v1/session/{session_id}/baseline/{domain}/start",
        "/api/v1/session/{session_id}/baseline/{domain}/answer",
        "/api/v1/session/{session_id}/baseline/{domain}/finalize",
        "/api/v1/session/{session_id}/baseline/{domain}",
        "/api/v1/session/{session_id}/baseline/{domain}/entry-screen",
    ):
        assert path in routes, path


def test_the_api_module_calls_the_engine_rather_than_reimplementing_it():
    """Every baseline decision must be a CALL. A second implementation would
    show up as these names being defined here instead of imported."""
    tree = ast.parse((REPO / "api" / "functional_baseline_api.py").read_text())
    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for engine_name in ("start_baseline", "record_answer", "finalize",
                        "next_question", "first_question", "question_at",
                        "_classify", "_nearest_rung", "_step", "ladder_months",
                        "_floor_months", "_ceiling_months", "_track_for"):
        assert engine_name not in defined, (
            f"{engine_name} is redefined in the API layer; the frozen engine "
            f"must be called, never copied")


# ---------------------------------------------------------------------------
# 2. the happy path
# ---------------------------------------------------------------------------

def test_a_caregiver_can_start_the_talking_baseline():
    _make_session("uid-parent-a", "s-start")
    response = _start("s-start")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["domain"] == SLP
    assert body["area_id"] == "talking"
    assert body["baseline_version"] == fb.BASELINE_VERSION
    assert body["entry_choice_id"] == "many_single_words"
    assert body["finalized"] is False
    assert body["answers_recorded"] == 0
    assert body["current_question"]["months"] == 18


def test_the_entry_screen_comes_from_the_engine():
    _make_session("uid-parent-a", "s-entry")
    response = client.get(
        f"/api/v1/session/s-entry/baseline/{SLP}/entry-screen", headers=AUTH_A)
    assert response.status_code == 200
    ids = {c["choice_id"] for c in response.json()["choices"]}
    assert ids == {c.choice_id for c in fb.get_area("talking").choices}


def test_answers_progress_the_baseline_deterministically():
    _make_session("uid-parent-a", "s-progress")
    view = _start("s-progress").json()
    seen = []
    answers = ["yes", "yes", "no"]
    index = 0
    while view.get("current_question"):
        seen.append(view["current_question"]["months"])
        view = _answer("s-progress", view["current_question"]["question_id"],
                       answers[index]).json()
        index += 1
    # Demonstrated steps HARDER; the first "no" brackets and stops the walk.
    assert seen == [18, 24, 30], seen
    assert view["ready_to_finalize"] is True
    assert view["answers_recorded"] == 3


def test_the_same_inputs_produce_the_same_baseline_twice():
    """Determinism across two independent sessions — the property the later
    canonical-rung derivation depends on."""
    results = []
    for name in ("s-det-1", "s-det-2"):
        _make_session("uid-parent-a", name)
        _run_to_completion(name, ["yes", "yes", "no"])
        assert _finalize(name).status_code == 200
        results.append(_reload("uid-parent-a", name)["functional_baseline"][SLP])
    assert results[0] == results[1]


def test_finalization_writes_the_engines_canonical_to_state():
    """The persisted dict must be EXACTLY `BaselineRecord.to_state()` — not a
    reshaped copy. Compared against the engine run independently."""
    _make_session("uid-parent-a", "s-canon")
    _run_to_completion("s-canon", ["yes", "yes", "no"])
    assert _finalize("s-canon").status_code == 200

    stored = _reload("uid-parent-a", "s-canon")["functional_baseline"][SLP]

    expected = fb.start_baseline("talking", "many_single_words", 36)
    for value in ["yes", "yes", "no"]:
        question = fb.next_question(expected)
        fb.record_answer(expected, question, value)
    fb.finalize(expected)

    assert stored == expected.to_state()
    assert stored["status"] == "BOUNDED"
    assert stored["routing_anchor_months"] == 24
    assert stored["not_demonstrated_months"] == 30
    assert stored["baseline_version"] == fb.BASELINE_VERSION


def test_the_persisted_record_carries_the_required_provenance():
    _make_session("uid-parent-a", "s-prov")
    _run_to_completion("s-prov", ["yes", "yes", "no"])
    _finalize("s-prov")
    stored = _reload("uid-parent-a", "s-prov")["functional_baseline"][SLP]
    for field in ("baseline_version", "area_id", "domain", "entry_choice_id",
                  "entry_choice_label", "entry_anchor_months",
                  "chronological_months", "asked", "status",
                  "routing_anchor_months", "demonstrated_months",
                  "not_demonstrated_months"):
        assert field in stored, field
    # The asked history must be structured, per-rung and reproducible.
    assert len(stored["asked"]) == 3
    for entry in stored["asked"]:
        assert set(entry) == {"question_id", "months", "milestone",
                              "subdomain", "answer", "classification"}


def test_the_stored_record_is_exactly_the_engines_key_set():
    """No field invented alongside the engine's serialisation."""
    _make_session("uid-parent-a", "s-keys")
    _start("s-keys")
    stored = _reload("uid-parent-a", "s-keys")["functional_baseline"][SLP]
    reference = fb.start_baseline("talking", "many_single_words", 36).to_state()
    assert set(stored) == set(reference)


# ---------------------------------------------------------------------------
# 3. durable round trip
# ---------------------------------------------------------------------------

def test_the_baseline_survives_a_durable_reload_unchanged():
    _make_session("uid-parent-a", "s-reload")
    _run_to_completion("s-reload", ["yes", "yes", "no"])
    in_request = _finalize("s-reload").json()

    reloaded = _reload("uid-parent-a", "s-reload")
    stored = reloaded["functional_baseline"][SLP]
    assert stored["status"] == "BOUNDED"
    assert stored["routing_anchor_months"] == 24
    assert reloaded["functional_baseline_api"][SLP]["finalized"] is True

    # And the view rebuilt from the reloaded doc matches the response.
    assert baseline_api.view(reloaded, SLP)["status"] == in_request["status"]


def test_the_baseline_round_trips_through_json_byte_identically():
    """Durability means JSON, so the record must be stable across a real
    serialise/parse cycle — no tuples, no datetimes, no non-JSON types."""
    _make_session("uid-parent-a", "s-json")
    _run_to_completion("s-json", ["yes", "yes", "no"])
    _finalize("s-json")
    stored = _reload("uid-parent-a", "s-json")["functional_baseline"][SLP]
    once = json.dumps(stored, sort_keys=True)
    twice = json.dumps(json.loads(once), sort_keys=True)
    assert once == twice


def test_the_in_progress_baseline_also_survives_a_reload():
    """Half-answered state must be durable too, or a parent who closes the app
    mid-baseline loses their answers."""
    _make_session("uid-parent-a", "s-partial")
    view = _start("s-partial").json()
    _answer("s-partial", view["current_question"]["question_id"], "yes")

    reloaded = _reload("uid-parent-a", "s-partial")
    assert reloaded["functional_baseline_api"][SLP]["finalized"] is False
    resumed = baseline_api.view(reloaded, SLP)
    assert resumed["answers_recorded"] == 1
    # The engine picks the walk back up at the next rung, not the first.
    assert resumed["current_question"]["months"] == 24


def test_the_real_gcs_adapter_round_trips_the_baseline(monkeypatch):
    """Exercises `_gcs_save_raw` / `_gcs_load_raw`, not the local fallback.

    A fake storage client backed by a dict, so the production code path —
    including `session_blob_name` and the json dump/parse — actually runs.
    """
    written: dict[str, str] = {}

    class _Blob:
        def __init__(self, name):
            self.name = name

        def upload_from_string(self, data, content_type=None, **kwargs):
            written[self.name] = data

        def exists(self):
            return self.name in written

        def download_as_text(self):
            return written[self.name]

    class _Bucket:
        def blob(self, name):
            return _Blob(name)

    class _Client:
        def bucket(self, name):
            return _Bucket()

    # `_gcs_save_raw` does `from google.cloud import storage` LAZILY, inside
    # the function, so the seam is the module itself rather than an attribute
    # on `session_store`. Registering a stand-in under that name is what makes
    # the real adapter body run.
    import types

    fake_module = types.ModuleType("google.cloud.storage")
    fake_module.Client = _Client
    monkeypatch.setitem(sys.modules, "google.cloud.storage", fake_module)
    try:
        import google.cloud as google_cloud

        monkeypatch.setattr(google_cloud, "storage", fake_module,
                            raising=False)
    except ImportError:  # pragma: no cover - google-cloud not installed
        google_cloud_pkg = types.ModuleType("google.cloud")
        google_cloud_pkg.storage = fake_module
        monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
        monkeypatch.setitem(sys.modules, "google.cloud", google_cloud_pkg)
    monkeypatch.setattr(session_store, "GCS_BUCKET_NAME", "fake-bucket")

    record = fb.start_baseline("talking", "many_single_words", 36)
    for value in ["yes", "yes", "no"]:
        fb.record_answer(record, fb.next_question(record), value)
    fb.finalize(record)
    doc = {"session_id": "s-gcs", "owner_uid": "uid-parent-a"}
    fb.attach_to_state(doc, record)

    session_store._gcs_save_raw("uid-parent-a", "s-gcs", doc)
    assert "sessions/uid-parent-a/s-gcs.json" in written
    loaded = session_store._gcs_load_raw("uid-parent-a", "s-gcs")
    assert loaded["functional_baseline"][SLP] == record.to_state()


def test_the_blob_path_is_keyed_by_the_authenticated_uid():
    """Recorded here because it is the crux of the DEFERRED identity gap: the
    Parent namespace is addressed by the Parent uid and nothing else."""
    assert session_store._blob_name("uid-parent-a", "s-x") == \
        "sessions/uid-parent-a/s-x.json"


# ---------------------------------------------------------------------------
# 4. lifecycle and retry
# ---------------------------------------------------------------------------

def test_restarting_an_in_progress_baseline_is_idempotent():
    """Returns current state, does NOT discard answers already given."""
    _make_session("uid-parent-a", "s-restart")
    view = _start("s-restart").json()
    _answer("s-restart", view["current_question"]["question_id"], "yes")

    again = _start("s-restart")
    assert again.status_code == 200
    assert again.json()["answers_recorded"] == 1
    # A different descriptor cannot retroactively change the entry either.
    other = _start("s-restart", choice="no_words_yet")
    assert other.json()["entry_choice_id"] == "many_single_words"


def test_resubmitting_the_same_answer_is_refused_not_duplicated():
    """The engine's `record_answer` appends without deduplicating, so a double
    submit would count one answer twice and move the floor."""
    _make_session("uid-parent-a", "s-retry")
    view = _start("s-retry").json()
    qid = view["current_question"]["question_id"]
    assert _answer("s-retry", qid, "yes").status_code == 200

    repeat = _answer("s-retry", qid, "yes")
    assert repeat.status_code == 422
    assert "Expected" in repeat.json()["detail"]
    assert _get("s-retry").json()["answers_recorded"] == 1


def test_changing_an_earlier_answer_is_refused():
    """`asked` is append-only in the engine; no rewrite path is invented."""
    _make_session("uid-parent-a", "s-change")
    view = _start("s-change").json()
    first_qid = view["current_question"]["question_id"]
    _answer("s-change", first_qid, "yes")
    changed = _answer("s-change", first_qid, "no")
    assert changed.status_code == 422
    assert _get("s-change").json()["answers_recorded"] == 1


def test_answering_after_finalization_is_refused():
    _make_session("uid-parent-a", "s-after")
    view = _run_to_completion("s-after", ["yes", "yes", "no"])
    _finalize("s-after")
    blocked = _answer("s-after", "anything", "yes")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "baseline_already_finalized"


def test_restarting_after_finalization_is_refused():
    _make_session("uid-parent-a", "s-restart-final")
    _run_to_completion("s-restart-final", ["yes", "yes", "no"])
    _finalize("s-restart-final")
    blocked = _start("s-restart-final")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "baseline_already_finalized"


def test_refinalization_is_idempotent_and_rewrites_nothing():
    _make_session("uid-parent-a", "s-refinal")
    _run_to_completion("s-refinal", ["yes", "yes", "no"])
    first = _finalize("s-refinal")
    before = copy.deepcopy(_reload("uid-parent-a", "s-refinal"))

    second = _finalize("s-refinal")
    assert second.status_code == 200
    after = _reload("uid-parent-a", "s-refinal")
    assert after["functional_baseline"][SLP] == \
        before["functional_baseline"][SLP]
    # Including the envelope: an immutable record must not look edited.
    assert after["functional_baseline_api"][SLP] == \
        before["functional_baseline_api"][SLP]
    assert second.json()["status"] == first.json()["status"]


def test_answering_when_the_engine_has_no_question_is_refused():
    _make_session("uid-parent-a", "s-done")
    view = _run_to_completion("s-done", ["yes", "yes", "no"])
    assert view["ready_to_finalize"] is True
    blocked = _answer("s-done", "anything", "yes")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "baseline_not_in_progress"


def test_answer_before_start_is_refused():
    _make_session("uid-parent-a", "s-noexist")
    blocked = _answer("s-noexist", "q", "yes")
    assert blocked.status_code == 404
    assert blocked.json()["detail"] == "baseline_not_started"
    assert _get("s-noexist").status_code == 404
    assert _finalize("s-noexist").status_code == 404


def test_finalizing_early_yields_unresolved_not_a_fabricated_level():
    """The 0.4 invariant: no evidence must never become a number. The engine
    decides; the API imposes no minimum answer count."""
    _make_session("uid-parent-a", "s-early")
    _start("s-early")
    assert _finalize("s-early").status_code == 200
    stored = _reload("uid-parent-a", "s-early")["functional_baseline"][SLP]
    assert stored["status"] == "UNRESOLVED"
    assert stored["routing_anchor_months"] is None
    assert stored["asked"] == []


def test_a_not_sure_answer_does_not_become_a_level():
    _make_session("uid-parent-a", "s-notsure")
    view = _start("s-notsure").json()
    _answer("s-notsure", view["current_question"]["question_id"], "not_sure")
    _finalize("s-notsure")
    stored = _reload("uid-parent-a", "s-notsure")["functional_baseline"][SLP]
    assert stored["asked"][0]["classification"] == "unknown"
    assert stored["routing_anchor_months"] is None
    assert stored["status"] == "UNRESOLVED"


# ---------------------------------------------------------------------------
# 5. authorization and isolation
# ---------------------------------------------------------------------------

def test_unauthenticated_requests_are_refused():
    _make_session("uid-parent-a", "s-anon")
    assert client.post(f"/api/v1/session/s-anon/baseline/{SLP}/start",
                       json={"entry_choice_id": "many_single_words"}
                       ).status_code in (401, 403)
    assert client.get(
        f"/api/v1/session/s-anon/baseline/{SLP}").status_code in (401, 403)


def test_another_caregiver_cannot_touch_this_baseline():
    _make_session("uid-parent-a", "s-owned")
    _start("s-owned")
    for response in (_start("s-owned", auth=AUTH_B),
                     _answer("s-owned", "q", "yes", auth=AUTH_B),
                     _finalize("s-owned", auth=AUTH_B),
                     _get("s-owned", auth=AUTH_B)):
        assert response.status_code in (403, 404), response.text


def test_a_caregiver_cannot_project_onto_another_sessions_baseline():
    """Two sessions, two owners. B's finalized baseline must be unreachable
    from A even knowing the session id."""
    _make_session("uid-parent-b", "s-b-owned")
    _run_to_completion("s-b-owned", ["yes", "yes", "no"], auth=AUTH_B)
    _finalize("s-b-owned", auth=AUTH_B)
    assert _get("s-b-owned", auth=AUTH_A).status_code in (403, 404)
    assert _answer("s-b-owned", "q", "no", auth=AUTH_A).status_code in (403, 404)
    # B's record is untouched.
    stored = _reload("uid-parent-b", "s-b-owned")["functional_baseline"][SLP]
    assert stored["status"] == "BOUNDED"


# ---------------------------------------------------------------------------
# 6. the client is not authoritative
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", [
    "routing_anchor_months", "demonstrated_months", "not_demonstrated_months",
    "status", "asked", "area_id", "domain", "baseline_version",
    "track_subdomains", "track_families", "chronological_months",
    "entry_anchor_months", "rung_ref", "track_ref", "activity_family",
])
def test_a_client_cannot_supply_a_derived_baseline_field(field):
    """Fail CLOSED, not silently ignored. A dropped field would leave the
    caller believing it had set a clinical result."""
    _make_session("uid-parent-a", f"s-forbid-{field}")
    response = client.post(
        f"/api/v1/session/s-forbid-{field}/baseline/{SLP}/start",
        json={"entry_choice_id": "many_single_words", field: 99},
        headers=AUTH_A)
    assert response.status_code == 422, response.text


def test_extra_fields_are_refused_on_the_answer_route_too():
    _make_session("uid-parent-a", "s-forbid-answer")
    view = _start("s-forbid-answer").json()
    response = client.post(
        f"/api/v1/session/s-forbid-answer/baseline/{SLP}/answer",
        json={"question_id": view["current_question"]["question_id"],
              "answer": "yes", "routing_anchor_months": 60},
        headers=AUTH_A)
    assert response.status_code == 422


def test_an_unsupported_answer_vocabulary_is_refused():
    _make_session("uid-parent-a", "s-badanswer")
    view = _start("s-badanswer").json()
    response = _answer("s-badanswer",
                       view["current_question"]["question_id"], "definitely")
    assert response.status_code == 422


def test_the_answer_vocabulary_is_refused_at_the_SCHEMA_not_just_the_engine():
    """Two layers, and this pins the OUTER one.

    A mutation that relaxed `answer` to a bare `str` still produced 422,
    because the engine's `_classify` then raised and that maps to 422 too — the
    behaviour was identical and the mutation survived. The layers are not
    equivalent though: schema rejection happens before the session is loaded,
    so no engine call and no write can occur. This asserts the model itself.
    """
    from pydantic import ValidationError

    from api.schemas import BaselineAnswerRequest

    BaselineAnswerRequest(question_id="q", answer="yes")
    for bad in ("definitely", "maybe", "YES", "1", ""):
        with pytest.raises(ValidationError):
            BaselineAnswerRequest(question_id="q", answer=bad)


def test_an_out_of_vocabulary_answer_mutates_nothing():
    """The consequence of rejecting at the schema layer: a refused answer must
    leave the stored baseline exactly as it was."""
    _make_session("uid-parent-a", "s-badnowrite")
    view = _start("s-badnowrite").json()
    before = copy.deepcopy(_reload("uid-parent-a", "s-badnowrite"))
    assert _answer("s-badnowrite", view["current_question"]["question_id"],
                   "definitely").status_code == 422
    after = _reload("uid-parent-a", "s-badnowrite")
    assert after["functional_baseline"] == before["functional_baseline"]
    assert after["functional_baseline_api"] == before["functional_baseline_api"]


def test_the_start_body_is_refused_at_the_schema_layer():
    from pydantic import ValidationError

    from api.schemas import BaselineStartRequest

    BaselineStartRequest(entry_choice_id="many_single_words")
    with pytest.raises(ValidationError):
        BaselineStartRequest(entry_choice_id="x", routing_anchor_months=60)
    with pytest.raises(ValidationError):
        BaselineStartRequest(entry_choice_id="")


def test_an_unknown_entry_choice_is_refused():
    _make_session("uid-parent-a", "s-badchoice")
    response = _start("s-badchoice", choice="speaks_fluent_latin")
    assert response.status_code == 422
    assert response.json()["detail"] == "unknown_entry_choice"


def test_the_finalize_route_accepts_no_body_fields():
    """Nothing a client sends may influence the outcome."""
    source = (REPO / "api" / "main.py").read_text()
    block = source.split("async def session_baseline_finalize(", 1)[1]
    signature = block.split(")", 1)[0]
    assert "body" not in signature, signature


# ---------------------------------------------------------------------------
# 7. scope: SLP only
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("domain", [
    "social_and_emotional", "learning_and_thinking", "fine_motor",
    "gross_motor", "daily_living", "sensory", "not_a_domain", "",
])
def test_every_other_domain_is_refused(domain):
    _make_session("uid-parent-a", "s-domain")
    response = client.post(
        f"/api/v1/session/s-domain/baseline/{domain or 'blank'}/start",
        json={"entry_choice_id": "many_single_words"}, headers=AUTH_A)
    assert response.status_code == 404, response.text


def test_only_one_domain_is_declared_supported():
    assert baseline_api.SUPPORTED_DOMAINS == (SLP,)


def test_a_refused_domain_writes_nothing():
    _make_session("uid-parent-a", "s-nowrite")
    client.post("/api/v1/session/s-nowrite/baseline/fine_motor/start",
                json={"entry_choice_id": "many_single_words"}, headers=AUTH_A)
    reloaded = _reload("uid-parent-a", "s-nowrite")
    assert not reloaded.get("functional_baseline")


# ---------------------------------------------------------------------------
# 8. the baseline is observation-only: no diagnosis, no concern, no legacy qna
# ---------------------------------------------------------------------------

def test_diagnosis_does_not_change_the_baseline_result():
    """Same answers, wildly different diagnosis text — identical record."""
    results = []
    for name, diagnosis in (("s-dx-none", ""),
                            ("s-dx-heavy", DIAGNOSIS_SENTINEL)):
        _make_session("uid-parent-a", name, diagnosis=diagnosis)
        _run_to_completion(name, ["yes", "yes", "no"])
        _finalize(name)
        results.append(_reload("uid-parent-a", name)["functional_baseline"][SLP])
    assert results[0] == results[1]


def test_free_text_concern_does_not_change_the_baseline_result():
    results = []
    for name, concern in (("s-cn-none", ""), ("s-cn-heavy", CONCERN_SENTINEL)):
        _make_session("uid-parent-a", name, concern=concern)
        _run_to_completion(name, ["yes", "yes", "no"])
        _finalize(name)
        results.append(_reload("uid-parent-a", name)["functional_baseline"][SLP])
    assert results[0] == results[1]


def test_legacy_qna_does_not_change_the_baseline_result():
    """The founder's explicit rule: the new baseline is NOT reconstructed from
    legacy intake answers."""
    loaded_qna = {SLP: [
        {"question_id": "legacy:1", "months": 60, "milestone": "tells a story",
         "norm_answer": "yes", "scoring_norm_answer": "yes", "score": 1.0},
        {"question_id": "legacy:2", "months": 48, "milestone": "four words",
         "norm_answer": "yes", "scoring_norm_answer": "yes", "score": 1.0},
    ]}
    results = []
    for name, qna in (("s-qna-none", {}), ("s-qna-full", loaded_qna)):
        _make_session("uid-parent-a", name, qna=qna)
        _run_to_completion(name, ["yes", "yes", "no"])
        _finalize(name)
        results.append(_reload("uid-parent-a", name)["functional_baseline"][SLP])
    assert results[0] == results[1]
    assert results[1]["routing_anchor_months"] == 24  # not 48 or 60


def test_no_sentinel_free_text_reaches_the_persisted_baseline():
    _make_session("uid-parent-a", "s-leak", diagnosis=DIAGNOSIS_SENTINEL,
                  concern=CONCERN_SENTINEL)
    _run_to_completion("s-leak", ["yes", "yes", "no"])
    _finalize("s-leak")
    reloaded = _reload("uid-parent-a", "s-leak")
    serialized = json.dumps(reloaded["functional_baseline"])
    assert DIAGNOSIS_SENTINEL not in serialized
    assert CONCERN_SENTINEL not in serialized
    assert "SENTINEL" not in json.dumps(
        reloaded["functional_baseline_api"])


def test_the_baseline_module_reads_no_diagnosis_concern_or_qna():
    """Structural, over the shipped module's AST — not just behavioural."""
    tree = ast.parse((REPO / "api" / "functional_baseline_api.py").read_text())
    constants = {n.value for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for forbidden in ("qna", "diagnosis", "concern", "diagnosis_or_condition",
                      "safety_profile", "weekly_schedule", "plans"):
        assert forbidden not in constants, forbidden
        assert forbidden not in attributes, forbidden


def test_only_chronological_months_is_read_from_the_session():
    """Age is CONTEXT — the engine's own word. It is read from stored state so
    a client cannot move the starting rung, and nothing else is read."""
    doc = _make_session("uid-parent-a", "s-ctx", months=36)
    assert baseline_api._chronological_months(doc) == 36
    assert baseline_api._chronological_months({}) == 0
    assert baseline_api._chronological_months(
        {"brain_state": {"child": {"chronological_months": "nonsense"}}}) == 0


# ---------------------------------------------------------------------------
# 9. internal canonical identifiers stay internal
# ---------------------------------------------------------------------------

def test_no_response_exposes_rung_track_or_family_bindings():
    """rung_ref, track_ref and activity-family bindings belong to the later
    canonical-rung path. Shipping them now would create a contract out of a
    field nobody asked for."""
    _make_session("uid-parent-a", "s-internal")
    bodies = [_start("s-internal").json()]
    view = bodies[0]
    for value in ["yes", "yes", "no"]:
        if not view.get("current_question"):
            break
        view = _answer("s-internal",
                       view["current_question"]["question_id"], value).json()
        bodies.append(view)
    bodies.append(_finalize("s-internal").json())
    bodies.append(_get("s-internal").json())

    for body in bodies:
        text = json.dumps(body)
        for leaked in ("rung_ref", "track_ref", "rung1:", "track1:",
                       "activity_family", "subdomain", "track_subdomains",
                       "track_families"):
            assert leaked not in text, (leaked, body)


def test_the_client_view_withholds_the_derived_anchor_numbers():
    """A raw routing anchor in a parent UI would read as a developmental age —
    the exact reading the field's own naming comment warns against."""
    _make_session("uid-parent-a", "s-anchor")
    _run_to_completion("s-anchor", ["yes", "yes", "no"])
    body = _finalize("s-anchor").json()
    for withheld in ("routing_anchor_months", "demonstrated_months",
                     "not_demonstrated_months"):
        assert withheld not in body, withheld
    # But it IS persisted server-side for the later projection.
    stored = _reload("uid-parent-a", "s-anchor")["functional_baseline"][SLP]
    assert stored["routing_anchor_months"] == 24


# ---------------------------------------------------------------------------
# 10. existing Beta behaviour is untouched
# ---------------------------------------------------------------------------

def test_the_baseline_does_not_write_dev_age():
    """`dev_age` is what `bridge_selector.select_next_milestones` reads to
    choose plan targets. Writing it would change which activities an existing
    Beta session generates — a live behaviour change, out of this slice.
    `attach_to_state` is used precisely because it never touches `dev_age`."""
    _make_session("uid-parent-a", "s-devage")
    _run_to_completion("s-devage", ["yes", "yes", "no"])
    _finalize("s-devage")
    reloaded = _reload("uid-parent-a", "s-devage")
    assert reloaded["brain_state"]["dev_age"] == {}


def test_the_api_module_does_not_call_apply_baseline_to_state():
    source = (REPO / "api" / "functional_baseline_api.py").read_text()
    tree = ast.parse(source)
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "attach_to_state" in called
    assert "apply_baseline_to_state" not in called


def test_the_baseline_touches_no_other_session_key():
    """Only the two baseline keys may appear or change."""
    _make_session("uid-parent-a", "s-isolate")
    before = copy.deepcopy(_reload("uid-parent-a", "s-isolate"))
    _run_to_completion("s-isolate", ["yes", "yes", "no"])
    _finalize("s-isolate")
    after = _reload("uid-parent-a", "s-isolate")

    added = set(after) - set(before)
    assert added == {"functional_baseline", "functional_baseline_api"}, added
    for key in set(before) - {"updated_at", "last_updated"}:
        if key in ("functional_baseline", "functional_baseline_api"):
            continue
        assert after.get(key) == before.get(key), key


def test_the_baseline_writes_neither_plans_nor_qna():
    _make_session("uid-parent-a", "s-noplans")
    _run_to_completion("s-noplans", ["yes", "yes", "no"])
    _finalize("s-noplans")
    reloaded = _reload("uid-parent-a", "s-noplans")
    assert not reloaded.get("plans")
    assert reloaded["brain_state"]["qna"] == {}


def test_no_error_detail_carries_child_specific_content():
    """Two of the baseline details are non-constant: the question-id mismatch
    message and a passed-through engine refusal. Both may only ever contain a
    catalogue question id or the caller's own answer token — never a value
    about this child.
    """
    _make_session("uid-parent-a", "s-errtext", diagnosis=DIAGNOSIS_SENTINEL,
                  concern=CONCERN_SENTINEL)
    view = _start("s-errtext").json()
    qid = view["current_question"]["question_id"]
    _answer("s-errtext", qid, "yes")

    # A separate session for the unknown-choice case: on an IN-PROGRESS
    # baseline `start` is idempotent and returns the view, so a bad descriptor
    # never reaches validation there.
    _make_session("uid-parent-a", "s-errchoice", diagnosis=DIAGNOSIS_SENTINEL,
                  concern=CONCERN_SENTINEL)

    details = [
        _answer("s-errtext", qid, "yes").json()["detail"],
        _start("s-errchoice", choice="not_a_choice").json()["detail"],
        client.post("/api/v1/session/s-errtext/baseline/fine_motor/start",
                    json={"entry_choice_id": "x"},
                    headers=AUTH_A).json()["detail"],
    ]
    for detail in details:
        text = str(detail)
        assert DIAGNOSIS_SENTINEL not in text
        assert CONCERN_SENTINEL not in text
        assert "SENTINEL" not in text
        assert "uid-parent-a" not in text
        assert "s-errtext" not in text


def test_the_new_code_constructs_no_logger_and_prints_nothing():
    """A baseline answer is clinical content. The engine is deterministic and
    needs no diagnostics, so there is nothing to log that would be worth the
    risk of logging it."""
    tree = ast.parse((REPO / "api" / "functional_baseline_api.py").read_text())
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
    for noisy in ("print", "getLogger", "debug", "info", "warning", "error",
                  "exception", "critical"):
        assert noisy not in called, noisy


def test_no_model_client_is_reachable_from_the_baseline_path():
    """No LLM, asserted over the module's imports."""
    tree = ast.parse((REPO / "api" / "functional_baseline_api.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported
    for banned in ("openai", "anthropic", "requests", "httpx"):
        assert banned not in imported, banned
