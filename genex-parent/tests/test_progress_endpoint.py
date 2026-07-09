"""
tests/test_progress_endpoint.py — Beta 2.3 Phase 1

GET /api/v1/session/{id}/progress contract: auth-gated, exact versioned shape,
7-day week, weekly/all-time stars from completions, survives refresh & sign-out/in.

Run: PYTHONPATH=. python3 tests/test_progress_endpoint.py
"""
import os, sys
os.environ.update(FIREBASE_PROJECT_ID="genex-test", LOCAL_SESSION_FALLBACK="1",
    REQUIRE_BETA_CODE="true", BETA_ACCESS_CODE="genex", ALLOWED_ORIGINS="http://localhost:3000",
    ACTIVITY_MODEL="")
os.environ.pop("GCS_BUCKET", None); os.environ.pop("CONCERN_ROUTER_MODEL", None)
import shutil; shutil.rmtree("/tmp/genex_api_sessions", ignore_errors=True)
import firebase_admin; firebase_admin._apps["[DEFAULT]"] = object()
from firebase_admin import auth as fa
_T = {"tok": {"uid": "uid-a", "email": "a@x.com"}, "tok2": {"uid": "uid-a", "email": "a@x.com"}}
fa.verify_id_token = lambda t, *a, **k: _T[t] if t in _T else (_ for _ in ()).throw(fa.InvalidIdTokenError("bad"))
from fastapi.testclient import TestClient
from api.main import app
from api import session_store
client = TestClient(app)
_p = _f = 0
def check(l, ok, d=""):
    global _p, _f
    if ok: _p += 1; print(f"  ✓ {l}")
    else: _f += 1; print(f"  ✗ FAIL: {l} — {d}")
def P(p, j=None, tok="tok"): return client.post(p, headers={"Authorization": f"Bearer {tok}"}, json=j)
def G(p, tok="tok"): return client.get(p, headers={"Authorization": f"Bearer {tok}"})

def _start_and_complete(n=1):
    sid = P("/api/v1/session/start", {"child_name": "R", "age_years": 4, "age_months": 0,
        "age_in_months": 48, "diagnosis_or_condition": "ADHD", "parent_concern": "ADHD, lack of attention",
        "daily_time_minutes": 20, "timezone": "America/Los_Angeles", "beta_access_code": "genex"}).json()["session_id"]
    q = G(f"/api/v1/session/{sid}").json().get("current_question")
    while q:
        a = P(f"/api/v1/session/{sid}/answer", {"question_id": q["question_id"], "answer": "with_help"}).json()
        if a.get("status") == "interview_complete": break
        q = a.get("current_question")
    P(f"/api/v1/session/{sid}/plan")
    plan = G("/api/v1/session/current").json()["plan"]
    cards = [(d["day"], a) for d in plan["week"] for a in d["activities"] if a["source"] == "primary"][:n]
    for day, c in cards:
        P(f"/api/v1/session/{sid}/feedback", {"plan_id": c["plan_id"], "activity_id": c["activity_id"],
            "day": day, "activity_date": c["activity_date"], "enjoyment": "loved_it",
            "difficulty": "just_right", "completion": "did_it"})
    return sid, len(cards)


def test_auth_and_shape():
    print("\n── auth gate + exact versioned shape")
    sid, _ = _start_and_complete(0)
    check("no token → 401/403", client.get(f"/api/v1/session/{sid}/progress").status_code in (401, 403))
    r = G(f"/api/v1/session/{sid}/progress")
    check("200 with auth", r.status_code == 200, r.status_code)
    b = r.json()
    keys = {"progress_schema_version", "session_id", "timezone", "week", "stars",
            "categories_in_practice", "latest_wins", "badges", "milestones_in_practice",
            "checkins_ready", "cups_by_domain"}
    check("exact top-level keys", set(b.keys()) == keys, set(b.keys()) ^ keys)
    check("schema version 1", b["progress_schema_version"] == 1)
    check("week has 7 entries Mon→Sun", len(b["week"]) == 7 and [d["day"] for d in b["week"]][0] == "Monday" and b["week"][-1]["day"] == "Sunday")
    check("stars shape", set(b["stars"].keys()) == {"this_week", "all_time"})
    check("future arrays empty", b["latest_wins"] == [] and b["badges"] == [] and b["cups_by_domain"] == [])
    check("no daily_plan_completed emitted", all(d["status"] in ("no_practice", "practiced") for d in b["week"]))
    check("exactly one is_today", sum(1 for d in b["week"] if d["is_today"]) == 1)


def test_stars_reflect_completions():
    print("\n── stars reflect completions")
    sid, n = _start_and_complete(2)
    b = G(f"/api/v1/session/{sid}/progress").json()
    check("all_time == completions", b["stars"]["all_time"] == n, (b["stars"], n))
    check("today counted this_week", b["stars"]["this_week"] == n, b["stars"])
    check("today shows practiced", any(d["is_today"] and d["status"] == "practiced" for d in b["week"]))


def test_survives_refresh_and_signin():
    print("\n── /progress stable across refresh + sign-out/in (authoritative)")
    sid, n = _start_and_complete(2)
    b1 = G(f"/api/v1/session/{sid}/progress").json()
    session_store._cache.clear()   # simulate a cold instance / refresh
    b2 = G(f"/api/v1/session/{sid}/progress").json()
    b3 = G(f"/api/v1/session/{sid}/progress", tok="tok2").json()   # new sign-in token, same uid
    check("stars stable across refresh", b1["stars"] == b2["stars"] == b3["stars"], (b1["stars"], b2["stars"], b3["stars"]))
    check("week stable across refresh", b1["week"] == b2["week"])


def run_all():
    test_auth_and_shape()
    test_stars_reflect_completions()
    test_survives_refresh_and_signin()
    print(f"\n{'='*50}\nResults: {_p} passed, {_f} failed")
    if _f: print("❌ progress endpoint FAILED"); sys.exit(1)
    print("✅ All progress endpoint tests PASSED")

if __name__ == "__main__":
    run_all()
