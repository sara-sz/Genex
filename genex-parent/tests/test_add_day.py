"""
tests/test_add_day.py — Beta 2.1 Add Activity day-stability fix

POST /activities/add must be day-specific and day-stable:
  - explicit day validated (case-insensitive/trim) or 400 invalid_day,
  - day-specific deterministic id (session+plan+suggestion+canonical_day),
  - same suggestion on a different day → distinct entry (no relocation to first day),
  - same suggestion on the same day → idempotent,
  - resolver renders each added activity on its own stored day.

In-process TestClient; Firebase mocked; local /tmp store; ACTIVITY_MODEL empty.

Run: PYTHONPATH=. python3 tests/test_add_day.py
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


def _add(sid, pid, suggestion_id, day=None):
    payload = {"suggestion_id": suggestion_id}
    if day is not None:
        payload["day"] = day
    return client.post(f"/api/v1/session/{sid}/plan/{pid}/activities/add", headers=_hdr(), json=payload)


def _get(sid):
    return client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()


def _ids_on_day(g, day):
    block = [d for d in g["plan"]["week"] if d["day"] == day]
    return [a["id"] for a in block[0]["activities"]] if block else []


def _all_resolved_ids(g):
    return [a["id"] for d in g["plan"]["week"] for a in d["activities"]]


def run_all():
    global _passed, _failed
    sid, w1 = _start_and_plan()
    pid = w1["plan_period"]["plan_id"]
    days = [d["day"] for d in w1["week"]]
    if len(days) < 3:
        print(f"  (plan has only {len(days)} days today: {days} — need >= 3 for day tests; skipping)")
        print("\n" + "=" * 50 + "\nResults: 0 passed, 0 failed (skipped — insufficient plan days)\n✅ skipped")
        return
    first_day, dayA, dayB = days[0], days[1], days[2]

    sugg = client.get(f"/api/v1/session/{sid}/plan/{pid}/activity-suggestions", headers=_hdr()).json()["suggestions"]
    if len(sugg) < 2:
        print(f"  (only {len(sugg)} add-suggestions — need >= 2; skipping)")
        print("\n" + "=" * 50 + "\nResults: 0 passed, 0 failed (skipped)\n✅ skipped")
        return
    sA, sB = sugg[0]["suggestion_id"], sugg[1]["suggestion_id"]

    # immutability snapshot
    doc0 = session_store.load("uid-a", sid)
    pr0 = copy.deepcopy(doc0["plans"][pid]["plan_response"])
    pi0 = copy.deepcopy(doc0["plans"][pid]["plan_internal"])
    bank0 = copy.deepcopy(doc0["brain_state"].get("activity_banks", {}))

    print(f"\n── Test 1: add A to {dayA} → appears on {dayA}, not {first_day}")
    r1 = _add(sid, pid, sA, day=dayA)
    check("add A → 200", r1.status_code == 200, r1.text[:160])
    check(f"response day == {dayA}", r1.json().get("day") == dayA, r1.json())
    idA1 = r1.json()["activity_id"]
    g = _get(sid)
    check(f"A on {dayA}", idA1 in _ids_on_day(g, dayA))
    check(f"A NOT on first day {first_day}", idA1 not in _ids_on_day(g, first_day))

    print(f"\n── Test 2: add B to {dayA} → both A and B on {dayA} (no relocation)")
    r2 = _add(sid, pid, sB, day=dayA)
    idB1 = r2.json()["activity_id"]
    check(f"B → 200 on {dayA}", r2.status_code == 200 and r2.json()["day"] == dayA)
    g = _get(sid)
    on_a = _ids_on_day(g, dayA)
    check("A still present after adding B", idA1 in on_a, on_a)
    check("B present on same day", idB1 in on_a, on_a)
    check("neither relocated to first day",
          idA1 not in _ids_on_day(g, first_day) and idB1 not in _ids_on_day(g, first_day))

    print(f"\n── Test 3: add A to {dayB} → distinct entry on {dayB}; {dayA} A preserved; nothing on first day")
    r3 = _add(sid, pid, sA, day=dayB)
    idA2 = r3.json()["activity_id"]
    check(f"A→{dayB} → 200", r3.status_code == 200 and r3.json()["day"] == dayB, r3.json())
    check("distinct id for different day", idA2 != idA1, (idA1, idA2))
    g = _get(sid)
    check(f"A still on {dayA}", idA1 in _ids_on_day(g, dayA))
    check(f"A-copy on {dayB}", idA2 in _ids_on_day(g, dayB))
    check("nothing added landed on first day",
          all(x not in _ids_on_day(g, first_day) for x in (idA1, idA2, idB1)))

    print(f"\n── Test 4: add A to {dayA} again → idempotent (same id, no duplicate)")
    r4 = _add(sid, pid, sA, day=dayA)
    check("same id returned", r4.json().get("activity_id") == idA1, (idA1, r4.json().get("activity_id")))
    doc = session_store.load("uid-a", sid)
    entries = doc["plan_customizations"][pid]["added_activities"]
    a_on_dayA = [e for e in entries if e["activity"]["id"] == idA1]
    check("no duplicate entry for A on dayA", len(a_on_dayA) == 1, len(a_on_dayA))
    check("exactly 3 added entries total (A@dayA, B@dayA, A@dayB)", len(entries) == 3, len(entries))

    print("\n── Test 5: invalid day → 400 invalid_day, nothing added")
    before = len(session_store.load("uid-a", sid)["plan_customizations"][pid]["added_activities"])
    r5 = _add(sid, pid, sA, day="Funday")
    check("→ 400", r5.status_code == 400, r5.status_code)
    check("code invalid_day", r5.json().get("detail", {}).get("code") == "invalid_day", r5.json())
    after = len(session_store.load("uid-a", sid)["plan_customizations"][pid]["added_activities"])
    check("nothing added on invalid day", after == before, (before, after))

    print("\n── Test 6: case-insensitive / trimmed day matches")
    r6 = _add(sid, pid, sB, day=f"  {dayB.upper()}  ")
    check("trimmed/upper day accepted", r6.status_code == 200 and r6.json()["day"] == dayB, r6.json())

    print("\n── Test 7: GET /session preserves all stored days after multiple adds")
    g = _get(sid)
    check(f"A@{dayA} preserved", idA1 in _ids_on_day(g, dayA))
    check(f"B@{dayA} preserved", idB1 in _ids_on_day(g, dayA))
    check(f"A@{dayB} preserved", idA2 in _ids_on_day(g, dayB))

    print("\n── Test 8: day-specific added activity can be removed")
    rr = client.post(f"/api/v1/session/{sid}/plan/{pid}/activity/{idA2}/remove", headers=_hdr())
    check("remove day-specific added → 200", rr.status_code == 200, rr.text[:160])
    g = _get(sid)
    check(f"A-copy removed from {dayB}", idA2 not in _ids_on_day(g, dayB))
    check(f"A on {dayA} unaffected by removing the {dayB} copy", idA1 in _ids_on_day(g, dayA))

    print("\n── Test 9: original plan_response / plan_internal / activity_banks not mutated")
    doc = session_store.load("uid-a", sid)
    check("plan_response unchanged", doc["plans"][pid]["plan_response"] == pr0)
    check("plan_internal unchanged", doc["plans"][pid]["plan_internal"] == pi0)
    check("activity_banks unchanged", doc["brain_state"].get("activity_banks", {}) == bank0)

    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ add-day tests FAILED")
        sys.exit(1)
    print("✅ All add-day tests PASSED")


if __name__ == "__main__":
    run_all()
