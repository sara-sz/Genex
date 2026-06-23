"""
tests/test_customization_patch.py — Beta 2.1 Step 2D patch

Fixes:
  A. remove/save/swap must work on overlay-added activities AND on visible swapped
     replacement ids (not just original generated ids).
  B. POST /activities/add must respect a provided day (case-insensitive/trimmed),
     400 invalid_day on mismatch, auto-pick only when omitted.

In-process TestClient; Firebase mocked; local /tmp store; ACTIVITY_MODEL empty.

Run: PYTHONPATH=. python3 tests/test_customization_patch.py
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

import firebase_admin  # noqa: E402
firebase_admin._apps["[DEFAULT]"] = object()
from firebase_admin import auth as firebase_auth  # noqa: E402
_TOKENS = {"token-user-a": {"uid": "uid-a", "email": "a@example.com"}}
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


def _start_and_plan(daily=15):
    r = client.post("/api/v1/session/start", headers=_hdr(), json={
        "child_name": "C", "age_years": 3, "age_months": 0, "age_in_months": 36,
        "diagnosis_or_condition": "No known diagnosis / not sure", "parent_concern": "speech delay",
        "daily_time_minutes": daily, "timezone": "UTC", "beta_access_code": "genex"})
    sid = r.json()["session_id"]; q = r.json()["current_question"]
    while q is not None:
        a = client.post(f"/api/v1/session/{sid}/answer", headers=_hdr(),
                        json={"question_id": q["question_id"], "answer": "yes"}).json()
        if a.get("status") == "interview_complete":
            break
        q = a.get("current_question")
    plan = client.post(f"/api/v1/session/{sid}/plan", headers=_hdr()).json()
    return sid, plan


def _get(sid):
    return client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()


def _resolved_ids(g):
    return [a["id"] for d in g["plan"]["week"] for a in d["activities"]]


def _add_first_suggestion(sid, pid, day=None):
    sug = client.get(f"/api/v1/session/{sid}/plan/{pid}/activity-suggestions", headers=_hdr()).json()
    s0 = sug["suggestions"][0]["suggestion_id"]
    payload = {"suggestion_id": s0}
    if day is not None:
        payload["day"] = day
    r = client.post(f"/api/v1/session/{sid}/plan/{pid}/activities/add", headers=_hdr(), json=payload)
    return r, sug["suggestions"]


# ── Fix A: actions on added activities ───────────────────────────────────────
def test_remove_added_activity():
    print("\n── remove an ADDED activity (was 404)")
    sid, w1 = _start_and_plan()
    pid = w1["plan_period"]["plan_id"]; day0 = w1["week"][0]["day"]
    r, _ = _add_first_suggestion(sid, pid, day=day0)
    added_id = r.json()["activity_id"]
    check("added present before remove", added_id in _resolved_ids(_get(sid)))
    rr = client.post(f"/api/v1/session/{sid}/plan/{pid}/activity/{added_id}/remove", headers=_hdr())
    check("remove added → 200 (not 404)", rr.status_code == 200, rr.text[:160])
    check("added hidden from resolved plan", added_id not in _resolved_ids(_get(sid)))
    doc = session_store.load("uid-a", sid)
    check("added id in removed_activity_ids", added_id in doc["plan_customizations"][pid]["removed_activity_ids"])
    check("added_activities entry kept (metadata preserved)",
          any(it["activity"]["id"] == added_id for it in doc["plan_customizations"][pid]["added_activities"]))


def test_save_added_activity():
    print("\n── save-for-later an ADDED activity")
    sid, w1 = _start_and_plan()
    pid = w1["plan_period"]["plan_id"]; day0 = w1["week"][0]["day"]
    r, _ = _add_first_suggestion(sid, pid, day=day0)
    added_id = r.json()["activity_id"]
    rs = client.post(f"/api/v1/session/{sid}/plan/{pid}/activity/{added_id}/save-for-later", headers=_hdr())
    check("save added → 200", rs.status_code == 200 and rs.json().get("saved_for_later") is True, rs.text[:160])
    check("added hidden from resolved plan", added_id not in _resolved_ids(_get(sid)))
    doc = session_store.load("uid-a", sid)
    ov = doc["plan_customizations"][pid]
    check("in saved_for_later + removed", added_id in ov["saved_for_later_activity_ids"] and added_id in ov["removed_activity_ids"])


def test_swap_added_activity():
    print("\n── swap an ADDED activity; feedback enriches via replacement_internal")
    sid, w1 = _start_and_plan()
    pid = w1["plan_period"]["plan_id"]; day0 = w1["week"][0]["day"]
    date0 = w1["week"][0]["activities"][0]["activity_date"]
    r, suggestions = _add_first_suggestion(sid, pid, day=day0)
    added_id = r.json()["activity_id"]
    # swap-suggestions for the added activity must not 404 (may be empty depending
    # on remaining bank capacity).
    ss = client.get(f"/api/v1/session/{sid}/plan/{pid}/activity/{added_id}/swap-suggestions", headers=_hdr())
    check("swap-suggestions for added → 200 (not 404)", ss.status_code == 200, ss.text[:160])
    if len(suggestions) < 2:
        check("(only one add-suggestion available — skipping swap-apply)", True)
        return
    # Apply a swap to the added activity using another valid bank suggestion id
    # (the swap endpoint resolves any bank suggestion). Exercises override-on-added.
    sw = client.post(f"/api/v1/session/{sid}/plan/{pid}/activity/{added_id}/swap",
                     headers=_hdr(), json={"suggestion_id": suggestions[1]["suggestion_id"]})
    check("swap added → 200", sw.status_code == 200 and sw.json().get("swapped") is True, sw.text[:160])
    repl_id = sw.json()["replacement_activity_id"]
    ids = _resolved_ids(_get(sid))
    check("added replaced in resolved plan", added_id not in ids and repl_id in ids, ids)
    doc = session_store.load("uid-a", sid)
    check("override keyed by added id", added_id in doc["plan_customizations"][pid]["activity_overrides"])
    # feedback on the swapped replacement enriches
    fb = client.post(f"/api/v1/session/{sid}/feedback", headers=_hdr(), json={
        "plan_id": pid, "activity_id": repl_id, "day": day0, "activity_date": date0,
        "enjoyment": "loved_it", "difficulty": "just_right", "completion": "did_it"})
    check("feedback on swapped-added metadata_found", fb.json().get("metadata_found") is True)
    rec = [f for f in session_store.load("uid-a", sid)["feedback"] if f.get("activity_id") == repl_id][0]
    check("swapped-added feedback has domain", bool(rec.get("domain")))


def test_actions_on_visible_replacement_id():
    print("\n── visible swapped replacement id is actionable (remove maps to source)")
    sid, w1 = _start_and_plan()
    pid = w1["plan_period"]["plan_id"]
    orig = w1["week"][0]["activities"][0]; orig_id = orig["id"]; day0 = w1["week"][0]["day"]
    ss = client.get(f"/api/v1/session/{sid}/plan/{pid}/activity/{orig_id}/swap-suggestions", headers=_hdr()).json()
    if not ss["suggestions"]:
        check("(no swap suggestions — skipping)", True)
        return
    sw = client.post(f"/api/v1/session/{sid}/plan/{pid}/activity/{orig_id}/swap",
                     headers=_hdr(), json={"suggestion_id": ss["suggestions"][0]["suggestion_id"]})
    repl_id = sw.json()["replacement_activity_id"]
    check("replacement visible", repl_id in _resolved_ids(_get(sid)))
    # remove using the VISIBLE replacement id → should not 404, maps to original key
    rr = client.post(f"/api/v1/session/{sid}/plan/{pid}/activity/{repl_id}/remove", headers=_hdr())
    check("remove visible replacement → 200 (not 404)", rr.status_code == 200, rr.text[:160])
    ids = _resolved_ids(_get(sid))
    check("both original and replacement hidden", orig_id not in ids and repl_id not in ids, ids)
    doc = session_store.load("uid-a", sid)
    check("original id added to removed (mapped from replacement)",
          orig_id in doc["plan_customizations"][pid]["removed_activity_ids"])


def test_prior_feedback_on_added_survives_removal():
    print("\n── feedback on added survives removal; still reportable")
    sid, w1 = _start_and_plan()
    pid = w1["plan_period"]["plan_id"]; day0 = w1["week"][0]["day"]
    date0 = w1["week"][0]["activities"][0]["activity_date"]
    r, _ = _add_first_suggestion(sid, pid, day=day0)
    added_id = r.json()["activity_id"]
    client.post(f"/api/v1/session/{sid}/feedback", headers=_hdr(), json={
        "plan_id": pid, "activity_id": added_id, "day": day0, "activity_date": date0,
        "enjoyment": "loved_it", "difficulty": "just_right", "completion": "did_it",
        "discuss_with_care_team": True, "care_team_tags": ["st"], "note": "ADDED_NOTE_X"})
    client.post(f"/api/v1/session/{sid}/plan/{pid}/activity/{added_id}/remove", headers=_hdr())
    doc = session_store.load("uid-a", sid)
    recs = [f for f in doc["feedback"] if f.get("activity_id") == added_id]
    check("feedback retained after removal", len(recs) == 1 and bool(recs[0].get("domain")))
    rep = client.post(f"/api/v1/session/{sid}/report", headers=_hdr(), json={"report_type": "speech_therapist"})
    check("report still includes the added note", "ADDED_NOTE_X" in rep.json().get("body", ""))


# ── Fix B: day handling ──────────────────────────────────────────────────────
def test_add_respects_day():
    print("\n── add respects the selected day")
    sid, w1 = _start_and_plan()
    pid = w1["plan_period"]["plan_id"]
    days = [d["day"] for d in w1["week"]]
    check("plan has >= 2 days for the test", len(days) >= 2, days)
    # pick a non-first day that exists
    target_day = days[1]
    r, suggestions = _add_first_suggestion(sid, pid, day=target_day)
    check(f"add with day='{target_day}' → 200", r.status_code == 200, r.text[:160])
    check("response day == requested", r.json().get("day") == target_day, r.json())
    g = _get(sid)
    block = [d for d in g["plan"]["week"] if d["day"] == target_day][0]
    check("added card on the requested day", r.json()["activity_id"] in [a["id"] for a in block["activities"]])
    # and NOT on the first day
    first_block = [d for d in g["plan"]["week"] if d["day"] == days[0]][0]
    check("added card NOT on first day", r.json()["activity_id"] not in [a["id"] for a in first_block["activities"]])

    # case-insensitive / trimmed
    if len(suggestions) >= 2:
        r2 = client.post(f"/api/v1/session/{sid}/plan/{pid}/activities/add", headers=_hdr(),
                         json={"suggestion_id": suggestions[1]["suggestion_id"], "day": f"  {target_day.lower()}  "})
        check("case-insensitive/trimmed day accepted", r2.status_code == 200 and r2.json()["day"] == target_day, r2.json())


def test_add_invalid_day_400():
    print("\n── add with invalid day → 400 invalid_day (no silent first-day fallback)")
    sid, w1 = _start_and_plan()
    pid = w1["plan_period"]["plan_id"]
    r, _ = _add_first_suggestion(sid, pid, day="Funday")
    check("invalid day → 400", r.status_code == 400, r.status_code)
    check("code invalid_day", r.json().get("detail", {}).get("code") == "invalid_day", r.json())
    check("valid_days listed", isinstance(r.json().get("detail", {}).get("valid_days"), list))
    # nothing added
    doc = session_store.load("uid-a", sid)
    check("no activity added on invalid day", not (doc.get("plan_customizations", {}).get(pid, {}).get("added_activities")))


def test_add_omitted_day_autopick():
    print("\n── add with omitted day → auto-pick (a real plan day)")
    sid, w1 = _start_and_plan()
    pid = w1["plan_period"]["plan_id"]
    days = [d["day"] for d in w1["week"]]
    r, _ = _add_first_suggestion(sid, pid, day=None)
    check("omitted day → 200", r.status_code == 200, r.text[:160])
    check("auto-picked a real plan day", r.json().get("day") in days, r.json().get("day"))


def run_all():
    test_remove_added_activity()
    test_save_added_activity()
    test_swap_added_activity()
    test_actions_on_visible_replacement_id()
    test_prior_feedback_on_added_survives_removal()
    test_add_respects_day()
    test_add_invalid_day_400()
    test_add_omitted_day_autopick()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ customization patch tests FAILED")
        sys.exit(1)
    print("✅ All customization patch tests PASSED")


if __name__ == "__main__":
    run_all()
