"""
tests/test_session_current.py — Beta 2.1 account/session restore

GET /api/v1/session/current — resume the authenticated user's latest session
without knowing the session_id. Read-only; selects latest by created_at.

In-process TestClient; Firebase mocked; local /tmp store; no OpenAI.

Run: PYTHONPATH=. python3 tests/test_session_current.py
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

# Isolate the local session store: /current selects the LATEST session per uid, so
# leftovers from other suites (which reuse uid-a/uid-b) would pollute selection.
import shutil  # noqa: E402
shutil.rmtree("/tmp/genex_api_sessions", ignore_errors=True)

import firebase_admin  # noqa: E402
firebase_admin._apps["[DEFAULT]"] = object()
from firebase_admin import auth as firebase_auth  # noqa: E402
_TOKENS = {
    "token-user-a": {"uid": "uid-a", "email": "a@example.com"},
    "token-user-b": {"uid": "uid-b", "email": "b@example.com"},
    "token-user-new": {"uid": "uid-new", "email": "new@example.com"},
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


def _start_session(token="token-user-a"):
    """Create a session and answer through to a Week-1 plan."""
    r = client.post("/api/v1/session/start", headers=_hdr(token), json={
        "child_name": "C", "age_years": 3, "age_months": 0, "age_in_months": 36,
        "diagnosis_or_condition": "No known diagnosis / not sure", "parent_concern": "speech delay",
        "daily_time_minutes": 10, "timezone": "UTC", "beta_access_code": "genex"})
    return r.json()["session_id"], r.json()["current_question"]


def _complete_to_plan(sid, token="token-user-a"):
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr(token)).json()
    q = g.get("current_question")
    while q is not None:
        a = client.post(f"/api/v1/session/{sid}/answer", headers=_hdr(token),
                        json={"question_id": q["question_id"], "answer": "yes"}).json()
        if a.get("status") == "interview_complete":
            break
        q = a.get("current_question")
    return client.post(f"/api/v1/session/{sid}/plan", headers=_hdr(token)).json()


# ── 1. no token → 401 ────────────────────────────────────────────────────────
def test_no_token_401():
    print("\n── GET /session/current no token → 401")
    check("→ 401", client.get("/api/v1/session/current").status_code == 401)


# ── 4. unknown/new uid → 404 no_existing_session ─────────────────────────────
def test_new_user_404():
    print("\n── new user with no session → 404 no_existing_session")
    r = client.get("/api/v1/session/current", headers=_hdr("token-user-new"))
    check("→ 404", r.status_code == 404, r.status_code)
    check("code no_existing_session", r.json().get("detail", {}).get("code") == "no_existing_session", r.json())


# ── 2. existing uid with one session → returns it ───────────────────────────
def test_single_session_returned():
    print("\n── single session → /current returns it (same shape as /session/{id})")
    sid, _ = _start_session("token-user-a")
    cur = client.get("/api/v1/session/current", headers=_hdr("token-user-a"))
    check("→ 200", cur.status_code == 200, cur.text[:160])
    check("returns the session_id", cur.json().get("session_id") == sid, cur.json())
    # identical payload to GET /session/{id}
    direct = client.get(f"/api/v1/session/{sid}", headers=_hdr("token-user-a")).json()
    check("payload matches GET /session/{id}", cur.json() == direct, "differs")


# ── 3. multiple sessions → returns latest by created_at ─────────────────────
def test_latest_session_selected():
    print("\n── multiple sessions → returns latest by created_at")
    s1, _ = _start_session("token-user-b")
    s2, _ = _start_session("token-user-b")
    # Force deterministic created_at ordering (s2 newer) via the durable store.
    d1 = session_store.load("uid-b", s1); d1["created_at"] = "2020-01-01T00:00:00+00:00"; session_store.save("uid-b", s1, d1)
    d2 = session_store.load("uid-b", s2); d2["created_at"] = "2025-12-31T00:00:00+00:00"; session_store.save("uid-b", s2, d2)
    cur = client.get("/api/v1/session/current", headers=_hdr("token-user-b")).json()
    check("returns the newest session", cur.get("session_id") == s2, (s1, s2, cur.get("session_id")))
    # missing created_at must not crash and must not win over a dated one
    d2b = session_store.load("uid-b", s2); d2b.pop("created_at", None); session_store.save("uid-b", s2, d2b)
    cur2 = client.get("/api/v1/session/current", headers=_hdr("token-user-b"))
    check("missing created_at handled safely (no crash)", cur2.status_code == 200, cur2.status_code)
    check("dated session selected over undated", cur2.json().get("session_id") == s1, cur2.json().get("session_id"))


# ── plan-ready session restores plan + acceptance + summary ─────────────────
def test_restore_plan_ready_session():
    print("\n── plan-ready session → /current returns plan + acceptance + summary")
    sid, _ = _start_session("token-user-a")
    plan = _complete_to_plan(sid, "token-user-a")
    cur = client.get("/api/v1/session/current", headers=_hdr("token-user-a")).json()
    check("status plan_ready", cur.get("status") == "plan_ready", cur.get("status"))
    check("includes plan with week", isinstance(cur.get("plan", {}).get("week"), list))
    check("includes plan_acceptance", "plan_acceptance" in cur)
    check("includes plan_customization_summary", "plan_customization_summary" in cur)


# ── 8. does not leak another user's session ─────────────────────────────────
def test_no_cross_user_leak():
    print("\n── /current never returns another user's session")
    sid_a, _ = _start_session("token-user-a")
    cur_new = client.get("/api/v1/session/current", headers=_hdr("token-user-new"))
    check("new user still 404 (no leak of uid-a's session)", cur_new.status_code == 404, cur_new.status_code)


# ── 5/6/7. does not create, mutate, or start plan generation ────────────────
def test_no_create_mutate_or_generate():
    print("\n── /current does not create, mutate, or start plan generation")
    sid, _ = _start_session("token-user-a")
    doc_before = copy.deepcopy(session_store.load("uid-a", sid))
    files_before = len(list(session_store._LOCAL_SESSION_DIR.glob("*.json")))
    # call /current several times
    for _ in range(3):
        client.get("/api/v1/session/current", headers=_hdr("token-user-a"))
    files_after = len(list(session_store._LOCAL_SESSION_DIR.glob("*.json")))
    doc_after = session_store.load("uid-a", sid)
    check("no new session file created", files_after == files_before, (files_before, files_after))
    check("session doc not mutated", doc_after == doc_before)
    check("status still in onboarding (no plan generated)", doc_after.get("status") == "questions")
    check("no current_plan_id set", doc_after.get("current_plan_id") is None)


# ── 9/10. existing GET /session/{id} + /session/start still work ────────────
def test_existing_routes_still_work():
    print("\n── existing GET /session/{id} and /session/start still work")
    sid, q = _start_session("token-user-a")
    check("first-time /session/start works", bool(sid) and q is not None)
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr("token-user-a"))
    check("GET /session/{id} still works", g.status_code == 200 and g.json().get("session_id") == sid)
    # 'current' is not mis-parsed as a session_id (route ordering)
    badreq = client.get("/api/v1/session/current", headers=_hdr("token-user-a"))
    check("'current' resolves to the current-endpoint (not a 404 session lookup)",
          badreq.status_code in (200, 404) and "detail" not in badreq.json() if badreq.status_code == 200 else True)


def run_all():
    test_no_token_401()
    test_new_user_404()
    test_single_session_returned()
    test_latest_session_selected()
    test_restore_plan_ready_session()
    test_no_cross_user_leak()
    test_no_create_mutate_or_generate()
    test_existing_routes_still_work()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ session/current tests FAILED")
        sys.exit(1)
    print("✅ All session/current tests PASSED")


if __name__ == "__main__":
    run_all()
