"""
tests/test_remove_save.py — Beta 2.1 Step 2C

Backend "remove from this week" + "save for later" (overlay-only, LLM-free).
Tests the two endpoints and the additive GET /session plan_customization_summary
via an in-process TestClient. Firebase mocked; local /tmp store; no OpenAI.

Run: PYTHONPATH=. python3 tests/test_remove_save.py
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
os.environ.setdefault("ACTIVITY_MODEL", "")
os.environ.pop("CONCERN_ROUTER_MODEL", None)

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
_PAST_SUNDAY = "2020-01-05"


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


def _start_and_plan(token="token-user-a"):
    r = client.post("/api/v1/session/start", headers=_hdr(token), json={
        "child_name": "C", "age_years": 3, "age_months": 0, "age_in_months": 36,
        "diagnosis_or_condition": "No known diagnosis / not sure", "parent_concern": "speech delay",
        "daily_time_minutes": 10, "timezone": "UTC", "beta_access_code": "genex"})
    sid = r.json()["session_id"]; q = r.json()["current_question"]
    while q is not None:
        a = client.post(f"/api/v1/session/{sid}/answer", headers=_hdr(token),
                        json={"question_id": q["question_id"], "answer": "yes"}).json()
        if a.get("status") == "interview_complete":
            break
        q = a.get("current_question")
    plan = client.post(f"/api/v1/session/{sid}/plan", headers=_hdr(token)).json()
    return sid, plan


def _make_eligible(sid, base_plan, uid="uid-a"):
    doc = session_store.load(uid, sid)
    doc["plans"][base_plan["plan_period"]["plan_id"]]["plan_period"]["plan_end_date"] = _PAST_SUNDAY
    session_store.save(uid, sid, doc)


def _first_activity(plan):
    return plan["week"][0]["activities"][0]


def _remove(sid, pid, aid, token="token-user-a"):
    return client.post(f"/api/v1/session/{sid}/plan/{pid}/activity/{aid}/remove", headers=_hdr(token))


def _save_later(sid, pid, aid, token="token-user-a"):
    return client.post(f"/api/v1/session/{sid}/plan/{pid}/activity/{aid}/save-for-later", headers=_hdr(token))


def _get(sid, token="token-user-a"):
    return client.get(f"/api/v1/session/{sid}", headers=_hdr(token)).json()


def _resolved_ids(g):
    return [a["id"] for d in g["plan"]["week"] for a in d["activities"]]


# ── auth / ownership / 404 / 409 ─────────────────────────────────────────────
def test_guards():
    print("\n── remove/save guards: 401 / 403 / 404 / 409")
    sid, w1 = _start_and_plan()
    pid = w1["plan_period"]["plan_id"]
    aid = _first_activity(w1)["id"]

    check("remove no token → 401",
          client.post(f"/api/v1/session/{sid}/plan/{pid}/activity/{aid}/remove").status_code == 401)
    check("save no token → 401",
          client.post(f"/api/v1/session/{sid}/plan/{pid}/activity/{aid}/save-for-later").status_code == 401)
    check("remove wrong user → 403", _remove(sid, pid, aid, token="token-user-b").status_code == 403)
    check("save wrong user → 403", _save_later(sid, pid, aid, token="token-user-b").status_code == 403)
    check("unknown plan_id → 404", _remove(sid, "nope", aid).status_code == 404)
    check("unknown activity_id → 404", _remove(sid, pid, "nope-activity").status_code == 404)

    # non-current plan → 409 (create Week 2 so Week 1 is non-current)
    _make_eligible(sid, w1)
    client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr()).json()
    r = _remove(sid, pid, aid)  # Week 1 no longer current
    check("non-current plan remove → 409", r.status_code == 409, r.status_code)
    check("409 code only_current_plan_can_be_customized",
          r.json().get("detail", {}).get("code") == "only_current_plan_can_be_customized", r.json())
    rs = _save_later(sid, pid, aid)
    check("non-current plan save → 409", rs.status_code == 409 and
          rs.json().get("detail", {}).get("code") == "only_current_plan_can_be_customized")


# ── remove: behavior + idempotency + resolution ─────────────────────────────
def test_remove():
    print("\n── remove current activity")
    sid, w1 = _start_and_plan()
    pid = w1["plan_period"]["plan_id"]
    aid = _first_activity(w1)["id"]

    g0 = _get(sid)
    check("plan_customization_summary present", "plan_customization_summary" in g0, sorted(g0.keys()))
    check("has_customizations False initially", g0["plan_customization_summary"]["has_customizations"] is False)
    check("activity present before remove", aid in _resolved_ids(g0))

    r = _remove(sid, pid, aid)
    body = r.json()
    check("remove → 200", r.status_code == 200, r.text[:160])
    check("response removed True / saved_for_later False",
          body.get("removed") is True and body.get("saved_for_later") is False, body)

    doc = session_store.load("uid-a", sid)
    ov = doc["plan_customizations"][pid]
    check("id in removed_activity_ids", aid in ov["removed_activity_ids"], ov["removed_activity_ids"])
    check("not in saved_for_later", aid not in ov["saved_for_later_activity_ids"])

    g1 = _get(sid)
    check("activity hidden in resolved plan", aid not in _resolved_ids(g1), _resolved_ids(g1))
    check("summary removed_count == 1", g1["plan_customization_summary"]["removed_count"] == 1)
    check("summary has_customizations True", g1["plan_customization_summary"]["has_customizations"] is True)

    # idempotent
    _remove(sid, pid, aid)
    doc2 = session_store.load("uid-a", sid)
    check("remove idempotent (no duplicate id)",
          doc2["plan_customizations"][pid]["removed_activity_ids"].count(aid) == 1)


# ── save for later: behavior + idempotency ──────────────────────────────────
def test_save_for_later():
    print("\n── save for later")
    sid, w1 = _start_and_plan()
    pid = w1["plan_period"]["plan_id"]
    aid = _first_activity(w1)["id"]

    r = _save_later(sid, pid, aid)
    body = r.json()
    check("save → 200, removed True saved True",
          r.status_code == 200 and body.get("removed") is True and body.get("saved_for_later") is True, body)

    doc = session_store.load("uid-a", sid)
    ov = doc["plan_customizations"][pid]
    check("id in saved_for_later_activity_ids", aid in ov["saved_for_later_activity_ids"])
    check("id ALSO in removed_activity_ids (hidden this week)", aid in ov["removed_activity_ids"])

    g = _get(sid)
    check("saved activity hidden in resolved plan", aid not in _resolved_ids(g), _resolved_ids(g))
    check("summary saved_for_later_count == 1", g["plan_customization_summary"]["saved_for_later_count"] == 1)

    # idempotent in BOTH lists
    _save_later(sid, pid, aid)
    doc2 = session_store.load("uid-a", sid)
    ov2 = doc2["plan_customizations"][pid]
    check("save idempotent — saved list no dup", ov2["saved_for_later_activity_ids"].count(aid) == 1)
    check("save idempotent — removed list no dup", ov2["removed_activity_ids"].count(aid) == 1)


# ── immutability of original plan + bank + feedback ─────────────────────────
def test_immutability_and_feedback_intact():
    print("\n── original plan/internal/bank immutable; feedback + reports intact")
    sid, w1 = _start_and_plan()
    pid = w1["plan_period"]["plan_id"]
    day0 = w1["week"][0]["day"]
    act = _first_activity(w1)
    aid = act["id"]

    # Log feedback for the activity BEFORE removing it
    fb = client.post(f"/api/v1/session/{sid}/feedback", headers=_hdr(), json={
        "plan_id": pid, "activity_id": aid, "day": day0, "activity_date": act["activity_date"],
        "enjoyment": "loved_it", "difficulty": "just_right", "completion": "did_it",
        "discuss_with_care_team": True, "care_team_tags": ["st"], "note": "REMOVED_ACT_NOTE"})
    check("feedback logged ok", fb.json().get("ok") is True)

    doc_before = session_store.load("uid-a", sid)
    pr_before = copy.deepcopy(doc_before["plans"][pid]["plan_response"])
    pi_before = copy.deepcopy(doc_before["plans"][pid]["plan_internal"])
    bank_before = copy.deepcopy(doc_before["brain_state"].get("activity_banks", {}))

    _remove(sid, pid, aid)
    _save_later(sid, pid, _first_activity(w1)["id"])  # idempotent same id; still no plan mutation

    doc_after = session_store.load("uid-a", sid)
    check("plan_response unchanged", doc_after["plans"][pid]["plan_response"] == pr_before)
    check("plan_internal unchanged", doc_after["plans"][pid]["plan_internal"] == pi_before)
    check("activity bank unchanged", doc_after["brain_state"].get("activity_banks", {}) == bank_before)

    # Prior feedback still present
    recs = [f for f in doc_after["feedback"] if f.get("activity_id") == aid]
    check("prior feedback for removed activity retained", len(recs) == 1, len(recs))
    check("retained feedback keeps domain (reportable)", bool(recs[0].get("domain")), recs[0].get("domain"))

    # Report still generates and includes the flagged note (reports unaffected by removal)
    rep = client.post(f"/api/v1/session/{sid}/report", headers=_hdr(), json={"report_type": "speech_therapist"})
    check("ST report still 200", rep.status_code == 200)
    check("report still includes prior flagged note", "REMOVED_ACT_NOTE" in rep.json().get("body", ""))


# ── acceptance still works alongside customization ──────────────────────────
def test_acceptance_still_works():
    print("\n── plan acceptance still works after customization")
    sid, w1 = _start_and_plan()
    pid = w1["plan_period"]["plan_id"]
    _remove(sid, pid, _first_activity(w1)["id"])
    acc = client.post(f"/api/v1/session/{sid}/plan/{pid}/accept", headers=_hdr())
    check("accept after remove → 200 accepted True", acc.status_code == 200 and acc.json().get("accepted") is True)
    g = _get(sid)
    check("plan_acceptance.accepted True", g["plan_acceptance"]["accepted"] is True)
    check("plan_customization_summary still present", g["plan_customization_summary"]["removed_count"] == 1)


def run_all():
    test_guards()
    test_remove()
    test_save_for_later()
    test_immutability_and_feedback_intact()
    test_acceptance_still_works()

    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ remove/save tests FAILED")
        sys.exit(1)
    print("✅ All remove/save tests PASSED")


if __name__ == "__main__":
    run_all()
