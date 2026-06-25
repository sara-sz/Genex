"""
tests/test_swap_add.py — Beta 2.1 Step 2D

Backend swap activity + add recommended activity (overlay-only, LLM-free; cards
drawn from the existing activity bank). In-process TestClient; Firebase mocked;
local /tmp store; ACTIVITY_MODEL empty (no OpenAI anywhere).

Run: PYTHONPATH=. python3 tests/test_swap_add.py
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
os.environ["ACTIVITY_MODEL"] = ""   # guarantee no LLM
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


def _start_and_plan(token="token-user-a", daily=15):
    r = client.post("/api/v1/session/start", headers=_hdr(token), json={
        "child_name": "C", "age_years": 3, "age_months": 0, "age_in_months": 36,
        "diagnosis_or_condition": "No known diagnosis / not sure", "parent_concern": "speech delay",
        "daily_time_minutes": daily, "timezone": "UTC", "beta_access_code": "genex"})
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


def _first(plan):
    return plan["week"][0]["activities"][0]


def _get(sid, token="token-user-a"):
    return client.get(f"/api/v1/session/{sid}", headers=_hdr(token)).json()


def _resolved_cards(g):
    return [a for d in g["plan"]["week"] for a in d["activities"]]


# ── Swap: guards ─────────────────────────────────────────────────────────────
def test_swap_guards():
    print("\n── swap guards: 401 / 403 / 404 / 409")
    sid, w1 = _start_and_plan()
    pid = w1["plan_period"]["plan_id"]; aid = _first(w1)["id"]
    sug = f"/api/v1/session/{sid}/plan/{pid}/activity/{aid}/swap-suggestions"
    swp = f"/api/v1/session/{sid}/plan/{pid}/activity/{aid}/swap"

    check("swap-suggestions no token → 401", client.get(sug).status_code == 401)
    check("swap no token → 401", client.post(swp, json={"suggestion_id": "x"}).status_code == 401)
    check("swap-suggestions wrong user → 403", client.get(sug, headers=_hdr("token-user-b")).status_code == 403)
    check("unknown plan → 404",
          client.get(f"/api/v1/session/{sid}/plan/nope/activity/{aid}/swap-suggestions", headers=_hdr()).status_code == 404)
    check("unknown activity → 404",
          client.get(f"/api/v1/session/{sid}/plan/{pid}/activity/nope/swap-suggestions", headers=_hdr()).status_code == 404)
    check("unknown suggestion_id → 404",
          client.post(swp, headers=_hdr(), json={"suggestion_id": "not-real"}).status_code == 404)

    _make_eligible(sid, w1)
    client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr()).json()
    r = client.get(sug, headers=_hdr())  # Week 1 non-current now
    check("non-current swap-suggestions → 409",
          r.status_code == 409 and r.json().get("detail", {}).get("code") == "only_current_plan_can_be_customized", r.json())


# ── Swap: suggestions + apply + immutability + feedback + idempotency ────────
def test_swap_flow():
    print("\n── swap: suggestions, apply, resolution, immutability, feedback")
    sid, w1 = _start_and_plan()
    pid = w1["plan_period"]["plan_id"]
    act = _first(w1); aid = act["id"]; day0 = w1["week"][0]["day"]; date0 = act["activity_date"]

    sug = client.get(f"/api/v1/session/{sid}/plan/{pid}/activity/{aid}/swap-suggestions", headers=_hdr()).json()
    suggestions = sug["suggestions"]
    # swap_suggestions contract is "up to 8" (limit=8 since the Step 2D quality fix).
    check("1–8 swap suggestions returned (LLM-free bank)", 1 <= len(suggestions) <= 8, len(suggestions))
    titles = {s["title"].strip().lower() for s in suggestions}
    check("suggestions exclude the original activity title", act["title"].strip().lower() not in titles, titles)
    check("each suggestion has suggestion_id + content",
          all(s.get("suggestion_id") and s.get("instructions") for s in suggestions))

    # snapshot originals + bank for immutability
    doc0 = session_store.load("uid-a", sid)
    pr0 = copy.deepcopy(doc0["plans"][pid]["plan_response"])
    pi0 = copy.deepcopy(doc0["plans"][pid]["plan_internal"])
    bank0 = copy.deepcopy(doc0["brain_state"].get("activity_banks", {}))

    sid0 = suggestions[0]["suggestion_id"]
    r = client.post(f"/api/v1/session/{sid}/plan/{pid}/activity/{aid}/swap",
                    headers=_hdr(), json={"suggestion_id": sid0})
    body = r.json()
    check("swap → 200, swapped True", r.status_code == 200 and body.get("swapped") is True, body)
    repl_id = body["replacement_activity_id"]
    check("summary swapped_count == 1", body["plan_customization_summary"]["swapped_count"] == 1)

    # overlay stored correctly
    doc = session_store.load("uid-a", sid)
    ov = doc["plan_customizations"][pid]["activity_overrides"][aid]
    check("override mode swapped", ov["mode"] == "swapped")
    check("override has replacement_activity id == repl_id", ov["replacement_activity"]["id"] == repl_id)
    check("override replacement_internal has domain", bool(ov["replacement_internal"].get("domain")))
    check("replacement_internal source_bank_type == swap", ov["replacement_internal"]["source_bank_type"] == "swap")

    # resolved plan shows replacement, not original
    g = _get(sid)
    ids = [a["id"] for a in _resolved_cards(g)]
    check("original activity gone from resolved plan", aid not in ids, ids)
    check("replacement present in resolved plan", repl_id in ids, ids)

    # immutability
    check("plan_response unchanged", doc["plans"][pid]["plan_response"] == pr0)
    check("plan_internal unchanged", doc["plans"][pid]["plan_internal"] == pi0)
    check("activity bank unchanged", doc["brain_state"].get("activity_banks", {}) == bank0)

    # feedback on replacement enriches via replacement_internal
    fb = client.post(f"/api/v1/session/{sid}/feedback", headers=_hdr(), json={
        "plan_id": pid, "activity_id": repl_id, "day": day0, "activity_date": date0,
        "enjoyment": "loved_it", "difficulty": "just_right", "completion": "did_it"})
    check("feedback on swapped card metadata_found", fb.json().get("metadata_found") is True, fb.text[:160])
    rec = [f for f in session_store.load("uid-a", sid)["feedback"] if f.get("activity_id") == repl_id][0]
    check("swapped feedback enriched with domain", bool(rec.get("domain")), rec.get("domain"))

    # idempotent re-swap (same suggestion)
    r2 = client.post(f"/api/v1/session/{sid}/plan/{pid}/activity/{aid}/swap",
                     headers=_hdr(), json={"suggestion_id": sid0})
    check("re-swap same suggestion → same replacement id (idempotent)",
          r2.json().get("replacement_activity_id") == repl_id, r2.json())

    # re-swap with a DIFFERENT suggestion replaces override (if a 2nd exists)
    if len(suggestions) >= 2:
        sid1 = suggestions[1]["suggestion_id"]
        r3 = client.post(f"/api/v1/session/{sid}/plan/{pid}/activity/{aid}/swap",
                         headers=_hdr(), json={"suggestion_id": sid1})
        new_repl = r3.json().get("replacement_activity_id")
        check("re-swap different suggestion replaces override", new_repl != repl_id, (repl_id, new_repl))
        doc2 = session_store.load("uid-a", sid)
        check("only one override for the activity (replaced cleanly)",
              len([1 for k in doc2["plan_customizations"][pid]["activity_overrides"] if k == aid]) == 1)


# ── Add: suggestions + apply + resolution + feedback + idempotency ──────────
def test_add_flow():
    print("\n── add: suggestions, apply, resolution, immutability, feedback")
    sid, w1 = _start_and_plan()
    pid = w1["plan_period"]["plan_id"]
    day0 = w1["week"][0]["day"]; date0 = _first(w1)["activity_date"]

    # guards (quick)
    check("add-suggestions no token → 401",
          client.get(f"/api/v1/session/{sid}/plan/{pid}/activity-suggestions").status_code == 401)
    check("add unknown plan → 404",
          client.post(f"/api/v1/session/{sid}/plan/nope/activities/add", headers=_hdr(),
                      json={"suggestion_id": "x"}).status_code == 404)

    sug = client.get(f"/api/v1/session/{sid}/plan/{pid}/activity-suggestions", headers=_hdr()).json()
    suggestions = sug["suggestions"]
    check("1–5 add suggestions returned (unused bank cards)", 1 <= len(suggestions) <= 5, len(suggestions))
    present_titles = {a["title"].strip().lower() for a in w1["week"][0]["activities"]}
    check("suggestions not already in the plan",
          all(s["title"].strip().lower() not in present_titles for s in suggestions))

    doc0 = session_store.load("uid-a", sid)
    pr0 = copy.deepcopy(doc0["plans"][pid]["plan_response"])
    bank0 = copy.deepcopy(doc0["brain_state"].get("activity_banks", {}))

    s0 = suggestions[0]["suggestion_id"]
    r = client.post(f"/api/v1/session/{sid}/plan/{pid}/activities/add",
                    headers=_hdr(), json={"suggestion_id": s0, "day": day0})
    body = r.json()
    check("add → 200, added True", r.status_code == 200 and body.get("added") is True, body)
    new_id = body["activity_id"]
    check("added activity has an id", bool(new_id))
    check("added on requested day", body["day"] == day0, body)
    check("summary added_count == 1", body["plan_customization_summary"]["added_count"] == 1)

    # overlay stored with activity + internal
    doc = session_store.load("uid-a", sid)
    added = doc["plan_customizations"][pid]["added_activities"]
    check("added_activities has 1 entry", len(added) == 1)
    check("entry has activity + internal", "activity" in added[0] and "internal" in added[0])
    check("internal source_bank_type parent_added", added[0]["internal"]["source_bank_type"] == "parent_added")

    # resolved plan shows the added card on the chosen day
    g = _get(sid)
    day_block = [d for d in g["plan"]["week"] if d["day"] == day0][0]
    check("added card present on chosen day", new_id in [a["id"] for a in day_block["activities"]])

    # immutability
    check("plan_response unchanged", doc["plans"][pid]["plan_response"] == pr0)
    check("activity bank unchanged", doc["brain_state"].get("activity_banks", {}) == bank0)

    # feedback on added card enriches
    fb = client.post(f"/api/v1/session/{sid}/feedback", headers=_hdr(), json={
        "plan_id": pid, "activity_id": new_id, "day": day0, "activity_date": date0,
        "enjoyment": "it_was_okay", "difficulty": "just_right", "completion": "did_it"})
    check("feedback on added card metadata_found", fb.json().get("metadata_found") is True, fb.text[:160])
    rec = [f for f in session_store.load("uid-a", sid)["feedback"] if f.get("activity_id") == new_id][0]
    check("added feedback enriched with domain", bool(rec.get("domain")), rec.get("domain"))

    # idempotent duplicate add (same suggestion → same id, no dup)
    r2 = client.post(f"/api/v1/session/{sid}/plan/{pid}/activities/add",
                     headers=_hdr(), json={"suggestion_id": s0, "day": day0})
    check("duplicate add → same id", r2.json().get("activity_id") == new_id, r2.json())
    doc2 = session_store.load("uid-a", sid)
    check("no duplicate in added_activities", len(doc2["plan_customizations"][pid]["added_activities"]) == 1)

    # auto-day add (omit day)
    if len(suggestions) >= 2:
        r3 = client.post(f"/api/v1/session/{sid}/plan/{pid}/activities/add",
                         headers=_hdr(), json={"suggestion_id": suggestions[1]["suggestion_id"]})
        check("auto-day add → 200 with a chosen day", r3.status_code == 200 and bool(r3.json().get("day")), r3.json())


# ── prior endpoints still work alongside swap/add ───────────────────────────
def test_existing_endpoints_intact():
    print("\n── accept / remove still work alongside swap+add")
    sid, w1 = _start_and_plan()
    pid = w1["plan_period"]["plan_id"]
    a2 = w1["week"][0]["activities"][1]["id"] if len(w1["week"][0]["activities"]) > 1 else _first(w1)["id"]
    rem = client.post(f"/api/v1/session/{sid}/plan/{pid}/activity/{a2}/remove", headers=_hdr())
    check("remove still works", rem.status_code == 200 and rem.json().get("removed") is True)
    acc = client.post(f"/api/v1/session/{sid}/plan/{pid}/accept", headers=_hdr())
    check("accept still works", acc.status_code == 200 and acc.json().get("accepted") is True)
    g = _get(sid)
    check("summary has swapped_count + added_count keys",
          "swapped_count" in g["plan_customization_summary"] and "added_count" in g["plan_customization_summary"])


def run_all():
    test_swap_guards()
    test_swap_flow()
    test_add_flow()
    test_existing_endpoints_intact()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ swap/add tests FAILED")
        sys.exit(1)
    print("✅ All swap/add tests PASSED")


if __name__ == "__main__":
    run_all()
