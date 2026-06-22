"""
tests/test_weekly_refresh.py — Beta 2.0 Step 5B + 5C

Weekly refresh MVP (Week-2 repeat-adapt) with after-Week-1 eligibility and
Monday–Sunday Week-2 date semantics. Tests the feedback-translation + date
helpers and the POST /api/v1/session/{id}/plan/next-week endpoint end-to-end
with an in-process TestClient. Firebase is mocked; sessions use the local /tmp
fallback; no OpenAI (deterministic fallback). Week 2 is repeat-adapt → no LLM.

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
from api import session_store  # noqa: E402
from api.pipeline import (  # noqa: E402
    _aggregate_signal,
    translate_feedback_to_activity_feedback,
)
from api.planning_period import (  # noqa: E402
    next_week_available_from,
    compute_next_week_period,
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

# A Sunday well in the past so eligibility passes deterministically.
_PAST_SUNDAY = "2020-01-05"      # 2020-01-05 was a Sunday
_EXPECT_W2_START = "2020-01-06"  # the following Monday
_EXPECT_W2_END = "2020-01-12"    # that week's Sunday


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
        {"activity_id": "aid-1", "plan_id": "OTHER", "domain": "language_and_communication",
         "difficulty": "too_hard", "completion": "did_it", "enjoyment": "not_really"},
    ]
    af = translate_feedback_to_activity_feedback(feedback, base_plan, "p1")
    check("language card mapped to harder",
          af.get("language_and_communication", {}).get("Naming Walk", {}).get("difficulty") == "too_easy", af)
    check("movement card mapped to easier",
          af.get("movement_and_physical", {}).get("Sock Sort", {}).get("difficulty") == "too_hard", af)
    check("other-plan feedback excluded",
          af["language_and_communication"]["Naming Walk"]["difficulty"] == "too_easy", af)
    check("unmappable activity_id skipped",
          translate_feedback_to_activity_feedback(
              [{"activity_id": "nope", "plan_id": "p1", "domain": "x"}], base_plan, "p1") == {})


# ── Unit: date helpers ───────────────────────────────────────────────────────
def test_date_helpers():
    print("\n── next_week_available_from / compute_next_week_period")
    # Week 1 ends Sunday 2026-06-21 → Week 2 starts Monday 2026-06-22.
    base = {"plan_id": "w1", "plan_end_date": "2026-06-21"}
    check("available_from = Monday after Week-1 Sunday",
          next_week_available_from(base) == "2026-06-22", next_week_available_from(base))
    p = compute_next_week_period(base, "UTC")
    check("week2 plan_start_date is Monday", p["plan_start_date"] == "2026-06-22", p)
    check("week2 plan_end_date is following Sunday", p["plan_end_date"] == "2026-06-28", p)
    check("week2 is_partial_week False", p["is_partial_week"] is False)
    check("week2 cycle_week 2", p["cycle_week"] == 2)
    check("week2 plan_type next_week", p["plan_type"] == "next_week")
    check("week2 base_plan_id carried", p["base_plan_id"] == "w1")
    check("week2 days_included full Mon–Sun", len(p["days_included"]) == 7)
    check("week2 available_from == start", p["available_from"] == "2026-06-22")


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


def _make_eligible(sid, base_plan, uid="uid-a"):
    """Backdate the base plan's end date so Week 2 becomes eligible (today is well
    past _PAST_SUNDAY). Only plan_end_date changes; plan_id and everything else
    stay intact."""
    doc = session_store.load(uid, sid)
    doc["plans"][base_plan["plan_period"]["plan_id"]]["plan_period"]["plan_end_date"] = _PAST_SUNDAY
    session_store.save(uid, sid, doc)


def _log_feedback(sid, plan, *, difficulty, completion, enjoyment="it_was_okay", token="token-user-a"):
    for day in plan["week"]:
        for act in day["activities"]:
            client.post(f"/api/v1/session/{sid}/feedback", headers=_hdr(token), json={
                "plan_id": plan["plan_period"]["plan_id"], "activity_id": act["id"],
                "day": day["day"], "activity_date": act["activity_date"],
                "enjoyment": enjoyment, "difficulty": difficulty, "completion": completion,
            })


def _repeat_modes(plan):
    modes = set()
    for day in plan.get("week", []):
        for act in day["activities"]:
            if "repeat_mode" in act:
                modes.add(act["repeat_mode"])
    return modes


# ── E2E: eligibility / not-ready ─────────────────────────────────────────────
def test_next_week_not_ready_before_week1_ends():
    print("\n── next-week BEFORE Week 1 ends → 409 not-ready, no plan created")
    sid, w1 = _start_and_plan()  # fresh plan ends this Sunday → not yet eligible
    before = client.get(f"/api/v1/session/{sid}", headers=_hdr("token-user-a")).json()
    r = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-a"))
    check("not-ready → 409", r.status_code == 409, f"{r.status_code} {r.text[:200]}")
    detail = r.json().get("detail", {})
    check("code == next_week_not_ready", isinstance(detail, dict) and detail.get("code") == "next_week_not_ready", detail)
    check("response carries available_from", bool(detail.get("available_from")), detail)
    check("response carries current_plan_end_date", bool(detail.get("current_plan_end_date")), detail)
    # No plan created; current_plan_id unchanged.
    after = client.get(f"/api/v1/session/{sid}", headers=_hdr("token-user-a")).json()
    doc = session_store.load("uid-a", sid)
    check("no Week-2 plan created (still 1 plan)", len(doc.get("plans", {})) == 1, list(doc.get("plans", {})))
    check("current_plan_id unchanged (still Week 1)",
          after.get("current_plan_id") == before.get("current_plan_id"), (before.get("current_plan_id"), after.get("current_plan_id")))


def test_week1_partial_unchanged_by_refresh_attempt():
    print("\n── Week 1 (partial) plan unchanged after a not-ready refresh attempt")
    sid, w1 = _start_and_plan()
    w1_id = w1["plan_period"]["plan_id"]
    doc_before = session_store.load("uid-a", sid)
    w1_period_before = dict(doc_before["plans"][w1_id]["plan_period"])
    client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-a"))  # 409
    doc_after = session_store.load("uid-a", sid)
    check("Week 1 plan_period unchanged",
          doc_after["plans"][w1_id]["plan_period"] == w1_period_before, "mutated")


# ── E2E: eligible happy path + date semantics ────────────────────────────────
def test_next_week_eligible_dates_and_preservation():
    print("\n── next-week AFTER Week 1 ends → Week 2 Mon–Sun, Week 1 preserved")
    sid, w1 = _start_and_plan()
    w1_id = w1["plan_period"]["plan_id"]
    _log_feedback(sid, w1, difficulty="just_right", completion="did_it")
    _make_eligible(sid, w1)

    r = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-a"))
    check("eligible next-week → 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    w2 = r.json()
    check("response shape matches /plan", set(w2.keys()) == _PLAN_KEYS, sorted(w2.keys()))
    pp = w2["plan_period"]
    check("new plan_id differs from Week 1", pp["plan_id"] != w1_id)
    check("Week 2 starts Monday after Week 1 ends", pp["plan_start_date"] == _EXPECT_W2_START, pp)
    check("Week 2 ends following Sunday", pp["plan_end_date"] == _EXPECT_W2_END, pp)
    check("Week 2 is_partial_week False", pp["is_partial_week"] is False, pp)
    check("Week 2 cycle_week 2", pp.get("cycle_week") == 2, pp)
    check("Week 2 plan_type next_week", pp.get("plan_type") == "next_week", pp)
    check("Week 2 base_plan_id == Week 1", pp.get("base_plan_id") == w1_id, pp)
    check("Week 2 cards carry repeat cues", len(_repeat_modes(w2)) >= 1, _repeat_modes(w2))

    # Week 1 preserved + pointer moved to Week 2 only after success.
    doc = session_store.load("uid-a", sid)
    check("Week 1 plan still stored", w1_id in doc["plans"])
    check("current_plan_id now Week 2", doc.get("current_plan_id") == pp["plan_id"])


def test_next_week_idempotent_after_eligibility():
    print("\n── next-week: double call after eligibility → same Week 2, no duplicate")
    sid, w1 = _start_and_plan()
    _make_eligible(sid, w1)
    r1 = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-a")).json()
    r2 = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-a")).json()
    r3 = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-a")).json()
    check("repeated calls return same Week-2 plan_id",
          r1["plan_period"]["plan_id"] == r2["plan_period"]["plan_id"] == r3["plan_period"]["plan_id"])
    doc = session_store.load("uid-a", sid)
    check("exactly 2 plans stored (W1 + W2)", len(doc["plans"]) == 2, list(doc["plans"]))


# ── E2E: feedback-driven adaptation (eligible) ───────────────────────────────
def test_too_hard_goes_easier():
    print("\n── eligible + all-too-hard → easier cues")
    sid, w1 = _start_and_plan()
    _log_feedback(sid, w1, difficulty="too_hard", completion="did_it")
    _make_eligible(sid, w1)
    w2 = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-a")).json()
    check("contains 'easier'", "easier" in _repeat_modes(w2), _repeat_modes(w2))
    check("no 'harder'", "harder" not in _repeat_modes(w2), _repeat_modes(w2))


def test_too_easy_goes_harder():
    print("\n── eligible + all-too-easy → harder cues")
    sid, w1 = _start_and_plan()
    _log_feedback(sid, w1, difficulty="too_easy", completion="did_it", enjoyment="loved_it")
    _make_eligible(sid, w1)
    w2 = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-a")).json()
    check("contains 'harder'", "harder" in _repeat_modes(w2), _repeat_modes(w2))


def test_no_feedback_safe_repeat():
    print("\n── eligible + no feedback → safe repeat, no error")
    sid, w1 = _start_and_plan()
    _make_eligible(sid, w1)
    r = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-a"))
    check("→ 200", r.status_code == 200, r.text[:200])
    check("'same' repeat cues", "same" in _repeat_modes(r.json()), _repeat_modes(r.json()))


def test_refused_reduces_pressure():
    print("\n── eligible + refused → easier / reduced pressure")
    sid, w1 = _start_and_plan()
    _log_feedback(sid, w1, difficulty="just_right", completion="didnt_want_to_try", enjoyment="not_really")
    _make_eligible(sid, w1)
    w2 = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-a")).json()
    check("refused → easier", "easier" in _repeat_modes(w2), _repeat_modes(w2))


# ── E2E: auth / first-plan unchanged ─────────────────────────────────────────
def test_next_week_requires_plan():
    print("\n── next-week: 409 before any plan exists")
    r = client.post("/api/v1/session/start", headers=_hdr("token-user-a"), json=_START)
    sid = r.json()["session_id"]
    rr = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-a"))
    check("no current plan → 409", rr.status_code == 409, f"{rr.status_code} {rr.text[:160]}")


def test_next_week_auth_and_ownership():
    print("\n── next-week: auth + ownership")
    sid, w1 = _start_and_plan()
    _make_eligible(sid, w1)
    check("no token → 401", client.post(f"/api/v1/session/{sid}/plan/next-week").status_code == 401)
    check("wrong user → 403",
          client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr("token-user-b")).status_code == 403)


def test_first_plan_path_unchanged():
    print("\n── first-plan /plan remains idempotent and unchanged")
    sid, w1 = _start_and_plan()
    again = client.post(f"/api/v1/session/{sid}/plan", headers=_hdr("token-user-a")).json()
    check("repeat /plan returns same Week-1 plan_id",
          again["plan_period"]["plan_id"] == w1["plan_period"]["plan_id"])
    check("first-plan response has NO repeat cues (Week-1 shape unchanged)",
          _repeat_modes(again) == set(), _repeat_modes(again))
    check("first-plan has no cycle_week marker (unchanged shape)",
          "cycle_week" not in w1["plan_period"], w1["plan_period"])


def run_all():
    test_aggregate_signal()
    test_translate_feedback()
    test_date_helpers()
    test_next_week_not_ready_before_week1_ends()
    test_week1_partial_unchanged_by_refresh_attempt()
    test_next_week_eligible_dates_and_preservation()
    test_next_week_idempotent_after_eligibility()
    test_too_hard_goes_easier()
    test_too_easy_goes_harder()
    test_no_feedback_safe_repeat()
    test_refused_reduces_pressure()
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
