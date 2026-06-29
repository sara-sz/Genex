"""
tests/test_plan_inflight.py — primary /plan in-flight guard (freeze-blocker fix)

A synchronous primary generation runs ~80–126s. This guard prevents a retry (e.g.
after a mobile timeout) from starting a SECOND run_plan_pipeline: while a generation
is in flight the endpoint returns 409 plan_generating; a stale marker recovers; errors
clear the marker so retry is possible. No planning-logic / genex_core / LLM-path change.

Run: PYTHONPATH=. python3 tests/test_plan_inflight.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone

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
_TOKENS = {"token-user-a": {"uid": "uid-a", "email": "a@example.com"}}
firebase_auth.verify_id_token = lambda t, *a, **k: _TOKENS[t] if t in _TOKENS else (_ for _ in ()).throw(
    firebase_auth.InvalidIdTokenError("bad"))

from fastapi.testclient import TestClient  # noqa: E402
import api.main as main  # noqa: E402
from api.main import app, PLAN_GENERATION_STALE_SECONDS  # noqa: E402
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


def _hdr():
    return {"Authorization": "Bearer token-user-a"}


def _intake_done():
    """Start a session and answer all primary questions (no /plan yet)."""
    sid = client.post("/api/v1/session/start", headers=_hdr(), json={
        "child_name": "C", "age_years": 3, "age_months": 0, "age_in_months": 36,
        "diagnosis_or_condition": "No known diagnosis / not sure",
        "parent_concern": "speech delay and trouble talking",
        "daily_time_minutes": 20, "timezone": "UTC", "beta_access_code": "genex"}).json()["session_id"]
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    q = g.get("current_question")
    while q:
        a = client.post(f"/api/v1/session/{sid}/answer", headers=_hdr(),
                        json={"question_id": q["question_id"], "answer": "yes"}).json()
        if a.get("status") == "interview_complete":
            break
        q = a.get("current_question")
    return sid


def _plan(sid):
    return client.post(f"/api/v1/session/{sid}/plan", headers=_hdr())


def _set_marker(sid, started_at):
    doc = session_store.load("uid-a", sid)
    doc["plan_generation_started_at"] = started_at
    session_store.save("uid-a", sid, doc)


# ── 1. first /plan → plan_ready, marker cleared ─────────────────────────────
def test_first_plan_ready():
    print("\n── first /plan generates and reaches plan_ready")
    sid = _intake_done()
    r = _plan(sid)
    check("→ 200 with week", r.status_code == 200 and "week" in r.json(), r.text[:120])
    doc = session_store.load("uid-a", sid)
    check("status plan_ready", doc["status"] == "plan_ready")
    check("current_plan_id set", bool(doc.get("current_plan_id")))
    check("in-flight marker cleared after success", "plan_generation_started_at" not in doc, list(doc)[:8])


# ── 2. retry after completion → cached, same plan_id, fast ──────────────────
def test_retry_cached():
    print("\n── retry after completion returns the cached plan")
    sid = _intake_done()
    p1 = _plan(sid).json()
    pid = session_store.load("uid-a", sid)["current_plan_id"]
    p2 = _plan(sid).json()
    check("retry → 200", "week" in p2)
    check("same plan_id (idempotent)", p1.get("plan_period", {}).get("plan_id") == p2.get("plan_period", {}).get("plan_id") == pid)


# ── 3,4. in-flight (fresh marker) → 409 plan_generating, no duplicate run ────
def test_inflight_blocks_duplicate():
    print("\n── fresh in-flight marker → 409 plan_generating (no duplicate generation)")
    sid = _intake_done()
    _set_marker(sid, datetime.now(timezone.utc).isoformat())
    # count run_plan_pipeline calls during the guarded request
    calls = {"n": 0}
    real = main.run_plan_pipeline
    main.run_plan_pipeline = lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1) or real(*a, **k))
    try:
        r = _plan(sid)
    finally:
        main.run_plan_pipeline = real
    check("→ 409", r.status_code == 409, r.text[:160])
    check("detail code plan_generating", r.json()["detail"]["code"] == "plan_generating", r.json())
    check("response carries poll hint", "poll" in r.json()["detail"], r.json()["detail"])
    check("run_plan_pipeline NOT invoked (no duplicate)", calls["n"] == 0, calls["n"])
    # /session/current surfaces plan_generating for polling
    cur = client.get("/api/v1/session/current", headers=_hdr()).json()
    check("/session/current shows plan_generating=true", cur.get("plan_generating") is True, cur.get("plan_generating"))


# ── 5. stale marker recovers (regenerates) ──────────────────────────────────
def test_stale_marker_recovers():
    print(f"\n── stale marker (> {PLAN_GENERATION_STALE_SECONDS}s) recovers")
    sid = _intake_done()
    old = (datetime.now(timezone.utc) - timedelta(seconds=PLAN_GENERATION_STALE_SECONDS + 120)).isoformat()
    _set_marker(sid, old)
    r = _plan(sid)
    check("→ 200 (recovered + regenerated)", r.status_code == 200 and "week" in r.json(), r.text[:160])
    doc = session_store.load("uid-a", sid)
    check("plan_ready + marker cleared", doc["status"] == "plan_ready" and "plan_generation_started_at" not in doc)
    # unparseable marker is also treated as stale
    sid2 = _intake_done()
    _set_marker(sid2, "not-a-timestamp")
    check("unparseable marker → recovers (200)", _plan(sid2).status_code == 200)


# ── 6. error path clears marker so retry is possible ────────────────────────
def test_error_clears_marker_retry():
    print("\n── generation error clears the marker; retry then succeeds")
    sid = _intake_done()
    def _boom(*a, **k):
        raise RuntimeError("simulated pipeline failure")
    real = main.run_plan_pipeline
    main.run_plan_pipeline = _boom
    try:
        r = _plan(sid)
        check("→ 500 on generation error", r.status_code == 500, r.text[:120])
        doc = session_store.load("uid-a", sid)
        check("marker cleared after error (retry possible)", "plan_generation_started_at" not in doc, list(doc)[:8])
        check("status still not plan_ready", doc.get("status") != "plan_ready")
    finally:
        main.run_plan_pipeline = real
    # retry now succeeds
    r2 = _plan(sid)
    check("retry after error → 200 plan_ready", r2.status_code == 200 and "week" in r2.json(), r2.text[:120])


# ── 7. normal single-call behavior unchanged (no marker leaks) ──────────────
def test_normal_behavior_unchanged():
    print("\n── normal /plan flow unchanged (single domain, no leaked marker)")
    sid = _intake_done()
    plan = _plan(sid).json()
    domains = {a["domain"] for d in plan["week"] for a in d["activities"]}
    check("plan builds the primary domain", domains <= {"language_and_communication"}, domains)
    cur = client.get("/api/v1/session/current", headers=_hdr()).json()
    check("plan_ready view has no plan_generating flag set", not cur.get("plan_generating", False))


def run_all():
    test_first_plan_ready()
    test_retry_cached()
    test_inflight_blocks_duplicate()
    test_stale_marker_recovers()
    test_error_clears_marker_retry()
    test_normal_behavior_unchanged()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ plan in-flight tests FAILED")
        sys.exit(1)
    print("✅ All plan in-flight tests PASSED")


if __name__ == "__main__":
    run_all()
