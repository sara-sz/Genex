"""
tests/test_weekly_refresh.py — Beta 2.0 Step 5B

Weekly refresh MVP (Week-2 repeat-adapt). Tests the feedback-translation helpers
and the POST /api/v1/session/{id}/plan/next-week endpoint end-to-end with an
in-process TestClient. Firebase is mocked; sessions use the local /tmp fallback;
no OpenAI (deterministic fallback). Week 2 is repeat-adapt → no LLM calls.

Run: PYTHONPATH=. python3 tests/test_weekly_refresh.py
"""

import os
import sys

os.environ["FIREBASE_PROJECT_ID"] = "genex-test"
os.environ["LOCAL_SESSION_FALLBACK"] = "1"
os.environ.pop("GCS_BUCKET", None)
os.environ["REQUIRE_BETA_CODE"] = "true"
os.environ["BETA_ACCESS_CODE"] = "genex"
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:3000")
os.environ.setdefault("ACTIVITY_MODEL", "")
os.environ.pop("CONCERN_ROUTER_MODEL", None)

import firebase_admin  # noqa: E402
firebase_admin._apps["[DEFAULT]"] = object()
from firebase_admin import auth as firebase_auth  # noqa: E402

_TOKENS = {
    "token-user-a": {"uid": "uid-a", "email": "a@example.com"},
    "token-user-b": {"uid": "uid-b", "email": "b@example.com"},
}


def _verify(token, *a, **k):
    if token in _TOKENS:
        return _TOKENS[token]
    raise firebase_auth.InvalidIdTokenError("bad token")


firebase_auth.verify_id_token = _verify

from fastapi.testclient import TestClient  # noqa: E402
from api.main import app  # noqa: E402
from api.pipeline import (  # noqa: E402
    _aggregate_signal,
    translate_feedback_to_activity_feedback,
)

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


def _hdr(t):
    return {"Authorization": f"Bearer {t}"}


_START = {
    "child_name": "C", "age_years": 3, "age_months": 0, "age_in_months": 36,
    "diagnosis_or_condition": "No known diagnosis / not sure",
    "parent_concern": "speech delay", "daily_time_minutes": 10,
    "timezone": "UTC", "beta_access_code": "genex",
}

_PLAN_KEYS = {
    "session_id", "plan_period", "age_in_months", "daily_time_minutes",
    "daily_card_count", "week", "progress_summary",
}


# ── Unit: _aggregate_signal ──────────────────────────────────────────────────
def test_aggregate_signal():
    print("\n── _aggregate_signal (conservative mapping)")
    easy = _aggregate_signal([{"difficulty": "too_easy", "completion": "did_it", "enjoyment": "loved_it"}])
    check("too_easy + did_it → harder", easy["difficulty"] == "too_easy" and easy["performance"] == "done_independently", easy)
    hard = _aggregate_signal([{"difficulty": "too_hard", "completion": "did_it", "enjoyment": "it_was_okay"}])
    check("too_hard → easier", hard["difficulty"] == "too_hard" and hard["performance"] == "couldnt_do_it", hard)
    notready = _aggregate_signal([{"difficulty": "just_right", "completion": "wasnt_ready_yet", "enjoyment": "it_was_okay"}])
    check("wasn't ready → easier", notready["performance"] == "couldnt_do_it", notready)
    refused = _aggregate_signal([{"difficulty": "just_right", "completion": "didnt_want_to_try", "enjoyment": "not_really"}])
    check("refused → easier + resisted", refused["performance"] == "couldnt_do_it" and refused["engagement"] == "resisted_it", refused)
    just = _aggregate_signal([{"difficulty": "just_right", "completion": "did_it", "enjoyment": "loved_it"}])
    check("just_right + did_it → same (no mastery claim)", just["difficulty"] == "just_right" and just["performance"] == "", just)
    mixed = _aggregate_signal([
        {"difficulty": "too_easy", "completion": "did_it", "enjoyment": "loved_it"},
        {"difficulty": "too_hard", "completion": "did_it", "enjoyment": "it_was_okay"},
    ])
    check("mixed easy+hard → easier wins (conservative)", mixed["difficulty"] == "too_hard", mixed)
    check("no records → just_right/neutral", _aggregate_signal([])["difficulty"] == "just_right")


# ── Unit: translate_feedback_to_activity_feedback ───────────────────────────
def test_translate_feedback():
    print("\n── translate_feedback_to_activity_feedback")
    base_plan = {"week": [{"day": "Monday", "activities": [
        {"id": "aid-1", "title": "Naming Walk", "domain": "language_and_communication"},
        {"id": "aid-2", "title": "Sock Sort", "domain": "movement_and_physical"},
    ]}]}
    feedback = [
        {"activity_id": "aid-1", "plan_id": "p1", "domain": "language_and_communication",
         "difficulty": "too_easy", "completion": "did_it", "enjoyment": "loved_it"},
        {"activity_id": "aid-2", "plan_id": "p1", "domain": "movement_and_physical",
         "difficulty": "too_hard", "completion": "did_it", "enjoyment": "it_was_okay"},
        # feedback for a different plan → ignored
        {"activity_id": "aid-1", "plan_id": "OTHER", "domain": "language_and_communication",
         "difficulty": "too_hard", "completion": "did_it", "enjoyment": "not_really"},
    ]
    af = translate_feedback_to_activity_feedback(feedback, base_plan, "p1")
    check("language card mapped to harder",
          af.get("language_and_communication", {}).get("Naming Walk", {}).get("difficulty") == "too_easy", af)
    check("movement card mapped to easier",
          af.get("movement_and_physical", {}).get("Sock Sort", {}).get("difficulty") == "too_hard", af)
    check("other-plan feedback excluded (no double signal)",
          af["language_and_communication"]["Naming Walk"]["difficulty"] == "too_easy", af)
    check("unmappable activity_id skipped",
          translate_feedback_to_activity_feedback(
              [{"activity_id": "nope", "plan_id": "p1", "domain": "x"}], base_plan, "p1") == {})


# ── Helpers to drive a full session ──────────────────────────────────────────
def _start_and_plan(token="token-user-a"):
    r = client.post("/api/v1/session/start", headers=_hdr(token), json=_START)
    sid = r.json()["session_id"]
    q = r.json()["current_question"]
    while q is not None:
        a = client.post(f"/api/v1/session/{sid}/answer", headers=_hdr(token),
                        json={"question_id": q["question_id"], "answer": "yes"}).json()
        if a.get("status") == "interview_complete":
            break
        q = a.get("current_question")
    plan = client.post(f"/api/v1/session/{sid}/plan", headers=_hdr(token)).json()
    return sid, plan


def _log_feedback(sid, plan, *, difficulty, completion, enjoyment="it_was_okay", token="token-user-a"):
    """Log feedback for every weekday activity in the plan."""
    n = 0
    for day in plan["week"]:
        for act in day["activities"]:
            client.post(f"/api/v1/session/{sid}/feedback", headers=_hdr(token), json={
                "plan_id": plan["plan_period"]["plan_id"], "activity_id": act["id"],
                "day": day["day"], "activity_date": act["activity_date"],
                "enjoyment": enjoyment, "difficulty": difficulty, "completion": completion,
            })
            n += 1
    return n


def _repeat_modes(plan):
    modes = set()
    for day in plan.get("week", []):
        for act in day["activities"]:
            if "repeat_mode" in act:
                modes.add(act["repeat_mode"])
    return modes


# ── E2E: happy path ──────────────────────────────────────────────────────────
def test_next_week_happy_path_preserves_week1():
    print("\n── next-week: builds Week 2, preserves Week 1, updates pointer")
    sid, w1 = _start_and_plan()
    w1_id = w1["plan_period"]["plan_id"]
    _log_feedback(sid, w1, difficulty="just_right", completion="did_it")

    r = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-a"))
    check("next-week → 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    w2 = r.json()
    check("response shape matches /plan", set(w2.keys()) == _PLAN_KEYS, sorted(w2.keys()))
    w2_id = w2["plan_period"]["plan_id"]
    check("new plan_id differs from Week 1", w2_id != w1_id, (w1_id, w2_id))
    check("plan_period.cycle_week == 2", w2["plan_period"].get("cycle_week") == 2, w2["plan_period"])
    check("plan_period.plan_type == 'next_week'", w2["plan_period"].get("plan_type") == "next_week")
    check("plan_period.base_plan_id == Week 1", w2["plan_period"].get("base_plan_id") == w1_id)
    check("Week 2 cards carry repeat cues", len(_repeat_modes(w2)) >= 1, _repeat_modes(w2))

    # Week 1 still retrievable and unchanged via GET (current points to W2)
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr("token-user-a")).json()
    check("current_plan_id now Week 2", g.get("current_plan_id") == w2_id, g.get("current_plan_id"))


# ── E2E: idempotency / no duplicates ─────────────────────────────────────────
def test_next_week_idempotent():
    print("\n── next-week: double-tap does not create duplicate plans")
    sid, w1 = _start_and_plan()
    _log_feedback(sid, w1, difficulty="just_right", completion="did_it")
    r1 = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-a")).json()
    r2 = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-a")).json()
    r3 = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-a")).json()
    check("repeated calls return the same Week-2 plan_id",
          r1["plan_period"]["plan_id"] == r2["plan_period"]["plan_id"] == r3["plan_period"]["plan_id"],
          (r1["plan_period"]["plan_id"], r2["plan_period"]["plan_id"]))


# ── E2E: feedback-driven adaptation ──────────────────────────────────────────
def test_next_week_too_hard_goes_easier():
    print("\n── next-week: all-too-hard feedback → easier cues")
    sid, w1 = _start_and_plan()
    _log_feedback(sid, w1, difficulty="too_hard", completion="did_it")
    w2 = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-a")).json()
    check("Week 2 contains 'easier' repeat_mode", "easier" in _repeat_modes(w2), _repeat_modes(w2))
    check("Week 2 contains no 'harder' cue", "harder" not in _repeat_modes(w2), _repeat_modes(w2))


def test_next_week_too_easy_goes_harder():
    print("\n── next-week: all-too-easy feedback → harder cues")
    sid, w1 = _start_and_plan()
    _log_feedback(sid, w1, difficulty="too_easy", completion="did_it", enjoyment="loved_it")
    w2 = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-a")).json()
    check("Week 2 contains 'harder' repeat_mode", "harder" in _repeat_modes(w2), _repeat_modes(w2))


def test_next_week_no_feedback_safe_repeat():
    print("\n── next-week: no feedback → safe repeat, no mastery, no error")
    sid, w1 = _start_and_plan()
    r = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-a"))
    check("next-week with no feedback → 200", r.status_code == 200, r.text[:200])
    w2 = r.json()
    check("Week 2 built with 'same' repeat cues", "same" in _repeat_modes(w2), _repeat_modes(w2))


def test_next_week_refused_reduces_pressure():
    print("\n── next-week: refused activities → easier / reduced pressure")
    sid, w1 = _start_and_plan()
    _log_feedback(sid, w1, difficulty="just_right", completion="didnt_want_to_try", enjoyment="not_really")
    w2 = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-a")).json()
    check("refused → easier repeat_mode", "easier" in _repeat_modes(w2), _repeat_modes(w2))


# ── E2E: guards / auth ───────────────────────────────────────────────────────
def test_next_week_requires_plan():
    print("\n── next-week: 409 before any plan exists")
    r = client.post("/api/v1/session/start", headers=_hdr("token-user-a"), json=_START)
    sid = r.json()["session_id"]
    rr = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-a"))
    check("no current plan → 409", rr.status_code == 409, f"{rr.status_code} {rr.text[:160]}")


def test_next_week_auth_and_ownership():
    print("\n── next-week: auth + ownership")
    sid, w1 = _start_and_plan()
    no_tok = client.post(f"/api/v1/session/{sid}/plan/next-week")
    check("no token → 401", no_tok.status_code == 401, no_tok.status_code)
    other = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-b"))
    check("wrong user → 403", other.status_code == 403, other.status_code)


def test_first_plan_path_unchanged():
    print("\n── first-plan /plan remains idempotent and unchanged")
    sid, w1 = _start_and_plan()
    again = client.post(f"/api/v1/session/{sid}/plan", headers=_hdr("token-user-a")).json()
    check("repeat /plan returns same Week-1 plan_id (idempotent)",
          again["plan_period"]["plan_id"] == w1["plan_period"]["plan_id"],
          (w1["plan_period"]["plan_id"], again["plan_period"]["plan_id"]))
    check("first-plan response has NO repeat cues (Week-1 shape unchanged)",
          _repeat_modes(again) == set(), _repeat_modes(again))


def run_all():
    test_aggregate_signal()
    test_translate_feedback()
    test_next_week_happy_path_preserves_week1()
    test_next_week_idempotent()
    test_next_week_too_hard_goes_easier()
    test_next_week_too_easy_goes_harder()
    test_next_week_no_feedback_safe_repeat()
    test_next_week_refused_reduces_pressure()
    test_next_week_requires_plan()
    test_next_week_auth_and_ownership()
    test_first_plan_path_unchanged()

    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ weekly refresh tests FAILED")
        sys.exit(1)
    print("✅ All weekly refresh tests PASSED")


if __name__ == "__main__":
    run_all()
