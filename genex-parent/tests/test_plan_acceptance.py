"""
tests/test_plan_acceptance.py — Beta 2.1 Step 2B

Backend plan-acceptance persistence per session_id + plan_id. Tests the
POST /plan/{plan_id}/accept endpoint and the additive GET /session
plan_acceptance field via an in-process TestClient. Firebase mocked; local /tmp
session store; no OpenAI.

Run: PYTHONPATH=. python3 tests/test_plan_acceptance.py
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


def _accept(sid, plan_id, token="token-user-a"):
    return client.post(f"/api/v1/session/{sid}/plan/{plan_id}/accept", headers=_hdr(token))


def _get(sid, token="token-user-a"):
    return client.get(f"/api/v1/session/{sid}", headers=_hdr(token)).json()


# ── auth / ownership / not-found / non-current ───────────────────────────────
def test_auth_ownership_404_409():
    print("\n── accept: auth / ownership / 404 / 409")
    sid, w1 = _start_and_plan()
    w1_id = w1["plan_period"]["plan_id"]

    check("no token → 401",
          client.post(f"/api/v1/session/{sid}/plan/{w1_id}/accept").status_code == 401)
    check("wrong user → 403", _accept(sid, w1_id, token="token-user-b").status_code == 403)
    check("unknown plan_id → 404", _accept(sid, "no-such-plan").status_code == 404)

    # Create Week 2 so Week 1 becomes non-current.
    _make_eligible(sid, w1)
    w2 = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr()).json()
    w2_id = w2["plan_period"]["plan_id"]
    r = _accept(sid, w1_id)  # Week 1 is no longer current
    check("non-current plan → 409", r.status_code == 409, r.status_code)
    check("409 code == only_current_plan_can_be_accepted",
          r.json().get("detail", {}).get("code") == "only_current_plan_can_be_accepted", r.json())


# ── happy path + idempotency + storage location ──────────────────────────────
def test_accept_and_idempotency():
    print("\n── accept current plan + idempotency")
    sid, w1 = _start_and_plan()
    w1_id = w1["plan_period"]["plan_id"]

    # GET before accept
    g0 = _get(sid)
    check("plan_acceptance present in GET", "plan_acceptance" in g0, sorted(g0.keys()))
    check("before accept → accepted False", g0["plan_acceptance"]["accepted"] is False, g0["plan_acceptance"])
    check("before accept → accepted_at None", g0["plan_acceptance"]["accepted_at"] is None)

    r1 = _accept(sid, w1_id)
    check("accept → 200", r1.status_code == 200, r1.text[:160])
    body = r1.json()
    check("response accepted True", body.get("accepted") is True, body)
    check("response has accepted_at", bool(body.get("accepted_at")), body)
    check("response plan_id == w1", body.get("plan_id") == w1_id)
    first_at = body["accepted_at"]

    # storage location: accepted_at lives on the plan entry, NOT in plan_response/internal
    doc = session_store.load("uid-a", sid)
    entry = doc["plans"][w1_id]
    check("accepted_at stored on plan entry", entry.get("accepted_at") == first_at, entry.get("accepted_at"))
    check("accepted_by_uid stored", entry.get("accepted_by_uid") == "uid-a")
    check("accepted_at NOT in plan_response", "accepted_at" not in entry["plan_response"])
    check("accepted_at NOT in plan_internal", "accepted_at" not in entry["plan_internal"])

    # idempotent re-accept
    r2 = _accept(sid, w1_id)
    check("re-accept → 200", r2.status_code == 200)
    check("re-accept same accepted_at (no overwrite)", r2.json().get("accepted_at") == first_at,
          (first_at, r2.json().get("accepted_at")))

    # GET after accept
    g1 = _get(sid)
    check("after accept → accepted True", g1["plan_acceptance"]["accepted"] is True)
    check("after accept → accepted_at present", g1["plan_acceptance"]["accepted_at"] == first_at)


# ── original plan immutability ───────────────────────────────────────────────
def test_accept_does_not_mutate_plan():
    print("\n── accept does not mutate plan_response / plan_internal")
    sid, w1 = _start_and_plan()
    w1_id = w1["plan_period"]["plan_id"]
    doc_before = session_store.load("uid-a", sid)
    import copy
    pr_before = copy.deepcopy(doc_before["plans"][w1_id]["plan_response"])
    pi_before = copy.deepcopy(doc_before["plans"][w1_id]["plan_internal"])

    _accept(sid, w1_id)

    doc_after = session_store.load("uid-a", sid)
    check("plan_response unchanged", doc_after["plans"][w1_id]["plan_response"] == pr_before)
    check("plan_internal unchanged", doc_after["plans"][w1_id]["plan_internal"] == pi_before)
    # GET /session plan still equals original (overlay foundation unchanged)
    g = _get(sid)
    check("GET plan equals original plan_response", g["plan"] == pr_before)
    check("plan_customizations still empty (foundation intact)",
          doc_after.get("plan_customizations") == {}, doc_after.get("plan_customizations"))


# ── Week 1 accept does not auto-accept Week 2; Week 2 accepts independently ──
def test_week1_accept_does_not_accept_week2():
    print("\n── Week 1 acceptance does not auto-accept Week 2")
    sid, w1 = _start_and_plan()
    w1_id = w1["plan_period"]["plan_id"]
    _accept(sid, w1_id)
    check("Week 1 accepted", session_store.load("uid-a", sid)["plans"][w1_id].get("accepted_at") is not None)

    _make_eligible(sid, w1)
    w2 = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr()).json()
    w2_id = w2["plan_period"]["plan_id"]
    check("Week 2 has a different plan_id", w2_id != w1_id)

    # GET now reflects current = Week 2, which must start UNACCEPTED
    g = _get(sid)
    check("GET current_plan_id == Week 2", g["current_plan_id"] == w2_id, g["current_plan_id"])
    check("Week 2 plan_acceptance.accepted False", g["plan_acceptance"]["accepted"] is False, g["plan_acceptance"])
    doc = session_store.load("uid-a", sid)
    check("Week 2 has no accepted_at yet", doc["plans"][w2_id].get("accepted_at") is None)
    check("Week 1 acceptance preserved", doc["plans"][w1_id].get("accepted_at") is not None)

    # Accept Week 2 independently
    r = _accept(sid, w2_id)
    check("accept Week 2 → 200, accepted True", r.status_code == 200 and r.json().get("accepted") is True)
    doc2 = session_store.load("uid-a", sid)
    check("Week 2 now accepted", doc2["plans"][w2_id].get("accepted_at") is not None)
    check("Week 1 accepted_at unchanged by Week 2 accept",
          doc2["plans"][w1_id].get("accepted_at") == doc["plans"][w1_id].get("accepted_at"))


def run_all():
    test_auth_ownership_404_409()
    test_accept_and_idempotency()
    test_accept_does_not_mutate_plan()
    test_week1_accept_does_not_accept_week2()

    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ plan acceptance tests FAILED")
        sys.exit(1)
    print("✅ All plan acceptance tests PASSED")


if __name__ == "__main__":
    run_all()
