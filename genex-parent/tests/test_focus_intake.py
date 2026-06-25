"""
tests/test_focus_intake.py — Beta 2.2 Slice 2b: add-on focused intake

Starting + answering a focused mini-onboarding for one chosen non-primary focus
area. Intake state lives only under doc["added_focus"][focus_key]; the primary
interview, primary plan, and doc["plans"] stay byte-identical. No generation, no LLM.

In-process TestClient; Firebase mocked; local /tmp store; ACTIVITY_MODEL empty.

Run: PYTHONPATH=. python3 tests/test_focus_intake.py
"""

import copy
import os
import sys

os.environ["FIREBASE_PROJECT_ID"] = "genex-test"
os.environ["LOCAL_SESSION_FALLBACK"] = "1"
os.environ.pop("GCS_BUCKET", None)
os.environ["REQUIRE_BETA_CODE"] = "true"
os.environ["BETA_ACCESS_CODE"] = "genex"
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:3000")
os.environ["ACTIVITY_MODEL"] = ""
os.environ.pop("CONCERN_ROUTER_MODEL", None)

import shutil  # noqa: E402
shutil.rmtree("/tmp/genex_api_sessions", ignore_errors=True)

import firebase_admin  # noqa: E402
firebase_admin._apps["[DEFAULT]"] = object()
from firebase_admin import auth as firebase_auth  # noqa: E402
_TOKENS = {
    "token-user-a": {"uid": "uid-a", "email": "a@example.com"},
    "token-user-b": {"uid": "uid-b", "email": "b@example.com"},
}
firebase_auth.verify_id_token = lambda t, *a, **k: _TOKENS[t] if t in _TOKENS else (_ for _ in ()).throw(
    firebase_auth.InvalidIdTokenError("bad"))

from fastapi.testclient import TestClient  # noqa: E402
from api.main import app  # noqa: E402
from api import session_store  # noqa: E402

client = TestClient(app)
_passed = 0
_failed = 0


def check(label, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ✓ {label}")
    else:
        _failed += 1
        print(f"  ✗ FAIL: {label} — {detail}")


def _hdr(t="token-user-a"):
    return {"Authorization": f"Bearer {t}"}


def _start(concern="speech delay and trouble talking", token="token-user-a"):
    r = client.post("/api/v1/session/start", headers=_hdr(token), json={
        "child_name": "C", "age_years": 3, "age_months": 0, "age_in_months": 36,
        "diagnosis_or_condition": "No known diagnosis / not sure", "parent_concern": concern,
        "daily_time_minutes": 10, "timezone": "UTC", "beta_access_code": "genex"})
    return r.json()["session_id"]


def _start_focus(sid, fk, token="token-user-a"):
    return client.post(f"/api/v1/session/{sid}/focus/{fk}/start", headers=_hdr(token))


def _answer_focus(sid, fk, qid, ans="yes", token="token-user-a"):
    return client.post(f"/api/v1/session/{sid}/focus/{fk}/answer", headers=_hdr(token),
                       json={"question_id": qid, "answer": ans})


def _finish_primary_and_plan(sid):
    """Drive the primary interview to completion and generate the primary plan."""
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    q = g.get("current_question")
    while q is not None:
        a = client.post(f"/api/v1/session/{sid}/answer", headers=_hdr(),
                        json={"question_id": q["question_id"], "answer": "yes"}).json()
        if a.get("status") == "interview_complete":
            break
        q = a.get("current_question")
    return client.post(f"/api/v1/session/{sid}/plan", headers=_hdr()).json()


def _drive_focus_to_complete(sid, fk, first_q):
    """Answer the add-on intake until interview_complete; return (#answered, last json)."""
    asked = 0
    q = first_q
    last = None
    while q is not None and asked < 30:
        asked += 1
        last = _answer_focus(sid, fk, q["question_id"]).json()
        if last.get("status") == "interview_complete":
            break
        q = last.get("current_question")
    return asked, last


# ── 1. start cognitive (primary speech) → interviewing + question + ≤7 ──────
def test_start_cognitive():
    print("\n── start add-on focus (cognitive)")
    sid = _start()  # primary = language_and_communication
    r = _start_focus(sid, "cognitive")
    check("→ 200", r.status_code == 200, r.text[:160])
    b = r.json()
    check("status interviewing", b["status"] == "interviewing", b.get("status"))
    check("focus_key cognitive", b["focus_key"] == "cognitive", b)
    check("friendly label", b["focus_label"] == "Learning, Attention & Thinking", b.get("focus_label"))
    check("module_id present", bool(b.get("module_id")), b)
    check("current_question returned", b.get("current_question") is not None, b)
    check("total_questions_estimate <= 7", 0 < b.get("total_questions_estimate", 0) <= 7,
          b.get("total_questions_estimate"))
    check("ready_for_generate False", b.get("ready_for_generate") is False, b)
    # stored separately
    doc = session_store.load("uid-a", sid)
    check("stored under added_focus[cognitive]", "cognitive" in (doc.get("added_focus") or {}))
    entry = doc["added_focus"]["cognitive"]
    check("entry has separate brain_state", isinstance(entry.get("brain_state"), dict))
    check("entry domain is cognitive only",
          entry["interview"]["domain_keys"] == ["cognitive"], entry["interview"]["domain_keys"])
    check("created_at/updated_at set", bool(entry.get("created_at")) and bool(entry.get("updated_at")))


# ── 2. start primary focus → 409 focus_is_primary ───────────────────────────
def test_start_primary_rejected():
    print("\n── starting the primary focus is rejected")
    sid = _start()
    r = _start_focus(sid, "language_and_communication")
    check("→ 409", r.status_code == 409, r.text[:160])
    check("detail focus_is_primary", r.json().get("detail") == "focus_is_primary", r.json())


# ── 3. start unknown focus → 404 unknown_focus ──────────────────────────────
def test_start_unknown():
    print("\n── starting an unknown focus is rejected")
    sid = _start()
    r = _start_focus(sid, "telepathy")
    check("→ 404", r.status_code == 404, r.text[:160])
    check("detail unknown_focus", r.json().get("detail") == "unknown_focus", r.json())
    ra = _answer_focus(sid, "telepathy", "q1")
    check("answer unknown focus → 404", ra.status_code == 404, ra.text[:120])


# ── 4. start same focus twice while interviewing → idempotent, no restart ───
def test_start_idempotent():
    print("\n── re-starting an in-progress focus returns current state (no restart)")
    sid = _start()
    first = _start_focus(sid, "cognitive").json()
    mod = first["module_id"]
    q1 = first["current_question"]["question_id"]
    # answer one question to advance the in-progress intake
    adv = _answer_focus(sid, "cognitive", q1).json()
    # may complete if only one question; guard
    again = _start_focus(sid, "cognitive")
    check("re-start → 200", again.status_code == 200, again.text[:160])
    ab = again.json()
    check("same module_id (not restarted)", ab["module_id"] == mod, (ab.get("module_id"), mod))
    doc = session_store.load("uid-a", sid)
    answered = doc["added_focus"]["cognitive"]["interview"]["questions_answered_total"]
    check("progress preserved (>=1 answered)", answered >= 1, answered)
    if adv.get("status") == "interviewing":
        check("re-start returns the in-progress (not first) question",
              ab["current_question"]["question_id"] == adv["current_question"]["question_id"],
              (ab.get("current_question"), adv.get("current_question")))


# ── 5. started focus disappears from remaining_focus_areas ──────────────────
def test_started_excluded_from_remaining():
    print("\n── starting a focus removes it from remaining")
    sid = _start()
    before = {x["focus_key"] for x in client.get(
        f"/api/v1/session/{sid}/focus-areas", headers=_hdr()).json()["remaining"]}
    check("cognitive in remaining before start", "cognitive" in before, before)
    _start_focus(sid, "cognitive")
    fa = client.get(f"/api/v1/session/{sid}/focus-areas", headers=_hdr()).json()
    rem = {x["focus_key"] for x in fa["remaining"]}
    added = {x["focus_key"]: x["status"] for x in fa["added"]}
    check("cognitive gone from remaining", "cognitive" not in rem, rem)
    check("cognitive listed in added (interviewing)", added.get("cognitive") == "interviewing", added)
    check("other two still remaining",
          {"movement_and_physical", "social_and_emotional"} == rem, rem)


# ── 6. answer flow advances and eventually completes ────────────────────────
def test_answer_completes():
    print("\n── answering the add-on intake reaches interview_complete")
    sid = _start()
    first = _start_focus(sid, "cognitive").json()
    asked, last = _drive_focus_to_complete(sid, "cognitive", first["current_question"])
    check("eventually interview_complete", last and last.get("status") == "interview_complete", last)
    check("ready_for_generate True", last and last.get("ready_for_generate") is True, last)
    check("answered <= 7", asked <= 7, asked)
    doc = session_store.load("uid-a", sid)
    check("stored status interview_complete",
          doc["added_focus"]["cognitive"]["status"] == "interview_complete")
    # completed focus stays out of remaining
    rem = {x["focus_key"] for x in client.get(
        f"/api/v1/session/{sid}/focus-areas", headers=_hdr()).json()["remaining"]}
    check("completed focus excluded from remaining", "cognitive" not in rem, rem)
    # cannot answer further
    r = _answer_focus(sid, "cognitive", "anything")
    check("answering completed focus → 409", r.status_code == 409, r.text[:120])


# ── 7. answers stored ONLY under added_focus[focus_key] ─────────────────────
def test_isolation_from_primary_interview():
    print("\n── add-on answers do not touch the primary interview/brain_state")
    sid = _start()
    doc0 = session_store.load("uid-a", sid)
    primary_interview_before = copy.deepcopy(doc0["interview"])
    primary_brain_before = copy.deepcopy(doc0["brain_state"])

    first = _start_focus(sid, "cognitive").json()
    _drive_focus_to_complete(sid, "cognitive", first["current_question"])

    doc1 = session_store.load("uid-a", sid)
    check("primary interview unchanged", doc1["interview"] == primary_interview_before)
    check("primary brain_state unchanged", doc1["brain_state"] == primary_brain_before)
    check("add-on qna isolated (primary qna empty/unchanged)",
          doc1["brain_state"].get("qna") == primary_brain_before.get("qna"))


# ── 8. primary plan + doc["plans"] byte-identical before/after start+answer ─
def test_primary_plan_byte_stable():
    print("\n── primary plan_response + doc['plans'] byte-stable across add-on intake")
    sid = _start()
    plan = _finish_primary_and_plan(sid)
    check("primary plan generated", "week" in plan, list(plan)[:6])
    doc_b = session_store.load("uid-a", sid)
    plans_before = copy.deepcopy(doc_b["plans"])
    pid = doc_b["current_plan_id"]
    plan_resp_before = copy.deepcopy(doc_b["plans"][pid]["plan_response"])
    plan_internal_before = copy.deepcopy(doc_b["plans"][pid].get("plan_internal"))

    # start + fully answer an add-on focus
    first = _start_focus(sid, "social_and_emotional").json()
    check("add-on start after plan → interviewing", first.get("status") == "interviewing", first)
    _drive_focus_to_complete(sid, "social_and_emotional", first["current_question"])

    doc_a = session_store.load("uid-a", sid)
    check("doc['plans'] byte-identical", doc_a["plans"] == plans_before)
    check("primary plan_response byte-identical", doc_a["plans"][pid]["plan_response"] == plan_resp_before)
    check("primary plan_internal byte-identical", doc_a["plans"][pid].get("plan_internal") == plan_internal_before)
    check("current_plan_id unchanged", doc_a["current_plan_id"] == pid)


# ── 9. /session + /session/current reflect added_focus status ───────────────
def test_session_views_reflect_addon():
    print("\n── GET /session + /session/current show add-on status")
    sid = _start()
    _start_focus(sid, "cognitive")
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    af = {x["focus_key"]: x["status"] for x in g["focus"]["added_focus_areas"]}
    check("GET /session added_focus_areas shows cognitive interviewing",
          af.get("cognitive") == "interviewing", af)
    rem = {x["key"] for x in g["focus"]["remaining_focus_areas"]}
    check("GET /session remaining excludes cognitive", "cognitive" not in rem, rem)
    cur = client.get("/api/v1/session/current", headers=_hdr()).json()
    check("/session/current focus identical", cur["focus"] == g["focus"], cur.get("focus"))


# ── 10. auth / ownership / no-token guards ──────────────────────────────────
def test_guards():
    print("\n── auth / ownership guards")
    sid = _start()
    check("start no token → 401",
          client.post(f"/api/v1/session/{sid}/focus/cognitive/start").status_code == 401)
    check("answer no token → 401",
          client.post(f"/api/v1/session/{sid}/focus/cognitive/answer",
                      json={"question_id": "q", "answer": "yes"}).status_code == 401)
    check("start wrong user → 403", _start_focus(sid, "cognitive", token="token-user-b").status_code == 403)
    check("start unknown session → 404",
          client.post("/api/v1/session/nope/focus/cognitive/start", headers=_hdr()).status_code == 404)
    # answer before start → 404 focus_not_started
    r = _answer_focus(sid, "cognitive", "q1")
    check("answer before start → 404 focus_not_started",
          r.status_code == 404 and r.json().get("detail") == "focus_not_started", r.text[:120])
    # malformed answer (bad enum) → 422 (Pydantic) using the real expected question_id
    first = _start_focus(sid, "cognitive").json()
    first_qid = first["current_question"]["question_id"]
    bad = client.post(f"/api/v1/session/{sid}/focus/cognitive/answer", headers=_hdr(),
                      json={"question_id": first_qid, "answer": "banana"})
    check("malformed answer enum → 422", bad.status_code == 422, bad.text[:120])
    # wrong question_id → 422
    wrong = _answer_focus(sid, "cognitive", "not-the-expected-id")
    check("wrong question_id → 422", wrong.status_code == 422, wrong.text[:120])


def run_all():
    test_start_cognitive()
    test_start_primary_rejected()
    test_start_unknown()
    test_start_idempotent()
    test_started_excluded_from_remaining()
    test_answer_completes()
    test_isolation_from_primary_interview()
    test_primary_plan_byte_stable()
    test_session_views_reflect_addon()
    test_guards()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ focus intake tests FAILED")
        sys.exit(1)
    print("✅ All focus intake tests PASSED")


if __name__ == "__main__":
    run_all()
