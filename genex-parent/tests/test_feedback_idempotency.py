"""
tests/test_feedback_idempotency.py — Beta 2.3 Phase 1

Feedback + completion idempotency and append-only revisions:
  - a did_it write creates exactly 1 feedback + 1 completion + 3 events
    (feedback_recorded, activity_completed, star_awarded) + 1 completion_index entry
  - exact retry (did_it or non-did_it) → 200 replay, no new records/events
  - changed feedback → append-only revision (feedback_updated), earlier preserved
  - not_yet → did_it creates the one completion/star; later edits never add a 2nd star

Run: PYTHONPATH=. python3 tests/test_feedback_idempotency.py
"""
import os, sys
os.environ.update(FIREBASE_PROJECT_ID="genex-test", LOCAL_SESSION_FALLBACK="1",
    REQUIRE_BETA_CODE="true", BETA_ACCESS_CODE="genex", ALLOWED_ORIGINS="http://localhost:3000",
    ACTIVITY_MODEL="")
os.environ.pop("GCS_BUCKET", None); os.environ.pop("CONCERN_ROUTER_MODEL", None)
import shutil; shutil.rmtree("/tmp/genex_api_sessions", ignore_errors=True)
import firebase_admin; firebase_admin._apps["[DEFAULT]"] = object()
from firebase_admin import auth as fa
_T = {"tok": {"uid": "uid-a", "email": "a@x.com"}}
fa.verify_id_token = lambda t, *a, **k: _T[t] if t in _T else (_ for _ in ()).throw(fa.InvalidIdTokenError("bad"))
from fastapi.testclient import TestClient
from api.main import app
from api import session_store
client = TestClient(app); H = {"Authorization": "Bearer tok"}
_p = _f = 0
def check(l, ok, d=""):
    global _p, _f
    if ok: _p += 1; print(f"  ✓ {l}")
    else: _f += 1; print(f"  ✗ FAIL: {l} — {d}")
def P(p, j=None): return client.post(p, headers=H, json=j)
def G(p): return client.get(p, headers=H)

def _bootstrap():
    sid = P("/api/v1/session/start", {"child_name": "R", "age_years": 4, "age_months": 0,
        "age_in_months": 48, "diagnosis_or_condition": "ADHD",
        "parent_concern": "ADHD, lack of attention", "daily_time_minutes": 10,
        "timezone": "America/Los_Angeles", "beta_access_code": "genex"}).json()["session_id"]
    q = G(f"/api/v1/session/{sid}").json().get("current_question")
    while q:
        a = P(f"/api/v1/session/{sid}/answer", {"question_id": q["question_id"], "answer": "with_help"}).json()
        if a.get("status") == "interview_complete": break
        q = a.get("current_question")
    P(f"/api/v1/session/{sid}/plan")
    plan = G("/api/v1/session/current").json()["plan"]
    card = next(a for d in plan["week"] for a in d["activities"] if a["source"] == "primary")
    day = next(d["day"] for d in plan["week"] for a in d["activities"] if a["activity_id"] == card["activity_id"])
    return sid, {"plan_id": card["plan_id"], "activity_id": card["activity_id"], "day": day,
                 "activity_date": card["activity_date"]}
def fb(sid, base, **over):
    body = {"plan_id": base["plan_id"], "activity_id": base["activity_id"], "day": base["day"],
            "activity_date": base["activity_date"], "enjoyment": "loved_it",
            "difficulty": "just_right", "completion": "did_it"}
    body.update(over)
    return P(f"/api/v1/session/{sid}/feedback", body)
def doc(sid): return session_store.load("uid-a", sid)
def ev_types(d):
    from collections import Counter
    return Counter(e["type"] for e in d.get("events", []))


def test_didit_creates_exactly_one_of_each():
    print("\n── did_it → 1 feedback + 1 attempt + 1 completion + 3 events + indexes")
    sid, base = _bootstrap()
    r = fb(sid, base).json()
    check("star_awarded true", r["star_awarded"] is True, r)
    check("completion_id set", bool(r["completion_id"]))
    check("attempt_id set", bool(r["attempt_id"]))
    check("not replay", r["idempotent_replay"] is False)
    d = doc(sid)
    check("1 feedback record", len(d["feedback"]) == 1, len(d["feedback"]))
    check("1 attempt record (effort)", len(d["attempts"]) == 1, len(d["attempts"]))
    check("attempt is_completion=True + outcome completed", d["attempts"][0]["is_completion"] is True
          and d["attempts"][0]["outcome"] == "completed", d["attempts"][0])
    check("1 completion record", len(d["completions"]) == 1, len(d["completions"]))
    check("completion links attempt_id", d["completions"][0]["attempt_id"] == d["attempts"][0]["attempt_id"])
    et = ev_types(d)
    check("exactly 3 events", sum(et.values()) == 3, dict(et))
    check("1 feedback_recorded + 1 star_awarded + 1 activity_completed",
          et["feedback_recorded"] == 1 and et["star_awarded"] == 1 and et["activity_completed"] == 1, dict(et))
    check("indexes: 1 attempt + 1 completion + 1 feedback",
          len(d["attempt_index"]) == 1 and len(d["completion_index"]) == 1 and len(d["feedback_index"]) == 1)


def test_exact_didit_retry_is_replay():
    print("\n── exact did_it retry → replay, no new records/events")
    sid, base = _bootstrap()
    r1 = fb(sid, base).json()
    r2 = fb(sid, base)
    check("HTTP 200", r2.status_code == 200, r2.status_code)
    j = r2.json()
    check("idempotent_replay true", j["idempotent_replay"] is True, j)
    check("no new star", j["star_awarded"] is False)
    check("returns original feedback_id", j["feedback_id"] == r1["feedback_id"])
    check("returns original completion_id", j["completion_id"] == r1["completion_id"])
    d = doc(sid)
    check("still 1 feedback", len(d["feedback"]) == 1, len(d["feedback"]))
    check("still 1 completion", len(d["completions"]) == 1)
    check("still 3 events", sum(ev_types(d).values()) == 3)


def test_non_didit_earns_star_no_completion():
    print("\n── not_ready: earns EFFORT star + attempt, but NO completion")
    sid, base = _bootstrap()
    r1 = fb(sid, base, completion="wasnt_ready_yet").json()
    check("star_awarded True (effort)", r1["star_awarded"] is True, r1)
    check("NO completion for not_yet", r1["completion_id"] is None, r1)
    check("attempt_id set", bool(r1["attempt_id"]))
    d = doc(sid)
    check("1 feedback, 1 attempt, 0 completions", len(d["feedback"]) == 1 and len(d["attempts"]) == 1 and len(d["completions"]) == 0)
    check("attempt outcome not_ready, is_completion False", d["attempts"][0]["outcome"] == "not_ready" and d["attempts"][0]["is_completion"] is False, d["attempts"][0])
    et = ev_types(d)
    check("2 events: feedback_recorded + star_awarded", et["feedback_recorded"] == 1 and et["star_awarded"] == 1 and sum(et.values()) == 2 and et["activity_completed"] == 0, dict(et))
    r2 = fb(sid, base, completion="wasnt_ready_yet").json()
    check("exact retry is replay", r2["idempotent_replay"] is True)
    d = doc(sid)
    check("still 1 feedback / 1 attempt / 2 events", len(d["feedback"]) == 1 and len(d["attempts"]) == 1 and sum(ev_types(d).values()) == 2)


def test_didnt_want_also_earns_star():
    print("\n── didn't-want-to-try also earns an effort star, no completion")
    sid, base = _bootstrap()
    r = fb(sid, base, completion="didnt_want_to_try").json()
    check("star_awarded True", r["star_awarded"] is True and r["completion_id"] is None, r)
    d = doc(sid)
    check("attempt outcome did_not_want", d["attempts"][0]["outcome"] == "did_not_want", d["attempts"][0])
    check("no completion record", len(d["completions"]) == 0)


def test_changed_feedback_revision_preserved():
    print("\n── changed feedback → append-only revision, earlier preserved")
    sid, base = _bootstrap()
    r1 = fb(sid, base, completion="wasnt_ready_yet", note="first").json()
    r2 = fb(sid, base, completion="wasnt_ready_yet", note="second").json()
    check("new feedback_id (revision)", r2["feedback_id"] != r1["feedback_id"])
    d = doc(sid)
    check("2 feedback records kept", len(d["feedback"]) == 2, len(d["feedback"]))
    check("earlier note preserved", d["feedback"][0]["note"] == "first")
    check("revision supersedes earlier", d["feedback"][1]["supersedes_feedback_id"] == r1["feedback_id"])
    check("feedback_updated event emitted", ev_types(d)["feedback_updated"] == 1, dict(ev_types(d)))


def test_star_on_first_attempt_completion_on_didit():
    print("\n── star earned on FIRST attempt (not_yet); did_it adds completion, not a 2nd star")
    sid, base = _bootstrap()
    r1 = fb(sid, base, completion="wasnt_ready_yet").json()
    check("not_yet earns the star", r1["star_awarded"] is True and r1["completion_id"] is None, r1)
    r2 = fb(sid, base, completion="did_it").json()   # change to did_it same day
    check("did_it adds completion", bool(r2["completion_id"]))
    check("did_it does NOT award a 2nd star (already earned)", r2["star_awarded"] is False, r2)
    d = doc(sid)
    check("exactly 1 attempt", len(d["attempts"]) == 1, len(d["attempts"]))
    check("exactly 1 completion", len(d["completions"]) == 1, len(d["completions"]))
    check("exactly 1 star_awarded event", ev_types(d)["star_awarded"] == 1, dict(ev_types(d)))
    # later edit (enjoyment) — attempt + completion already exist → nothing new
    r3 = fb(sid, base, completion="did_it", enjoyment="it_was_okay").json()
    check("later edit: no new star", r3["star_awarded"] is False, r3)
    d = doc(sid)
    check("still 1 attempt / 1 completion / 1 star event",
          len(d["attempts"]) == 1 and len(d["completions"]) == 1 and ev_types(d)["star_awarded"] == 1)


def run_all():
    test_didit_creates_exactly_one_of_each()
    test_exact_didit_retry_is_replay()
    test_non_didit_earns_star_no_completion()
    test_didnt_want_also_earns_star()
    test_changed_feedback_revision_preserved()
    test_star_on_first_attempt_completion_on_didit()
    print(f"\n{'='*50}\nResults: {_p} passed, {_f} failed")
    if _f: print("❌ feedback idempotency FAILED"); sys.exit(1)
    print("✅ All feedback idempotency tests PASSED")

if __name__ == "__main__":
    run_all()
