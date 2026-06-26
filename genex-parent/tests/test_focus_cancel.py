"""
tests/test_focus_cancel.py — Beta 2.2: cancel/abandon an unfinished add-on focus

A parent who opens an add-on intake and closes it before generating can cancel it;
the focus returns to remaining_focus_areas. Cancellable: interviewing,
interview_complete, error, stale-generating. Not cancellable: ready, fresh-generating,
primary. Idempotent when already absent. No primary plan / doc["plans"] mutation,
no generation, no genex_core changes. ACTIVITY_MODEL empty.

In-process TestClient; Firebase mocked; local /tmp store.

Run: PYTHONPATH=. python3 tests/test_focus_cancel.py
"""

import copy
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
_TOKENS = {
    "token-user-a": {"uid": "uid-a", "email": "a@example.com"},
    "token-user-b": {"uid": "uid-b", "email": "b@example.com"},
}
firebase_auth.verify_id_token = lambda t, *a, **k: _TOKENS[t] if t in _TOKENS else (_ for _ in ()).throw(
    firebase_auth.InvalidIdTokenError("bad"))

from fastapi.testclient import TestClient  # noqa: E402
from api.main import app, ADDON_GENERATION_STALE_SECONDS  # noqa: E402
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


def _start(concern="speech delay and trouble talking"):
    r = client.post("/api/v1/session/start", headers=_hdr(), json={
        "child_name": "C", "age_years": 3, "age_months": 0, "age_in_months": 36,
        "diagnosis_or_condition": "No known diagnosis / not sure", "parent_concern": concern,
        "daily_time_minutes": 20, "timezone": "UTC", "beta_access_code": "genex"})
    return r.json()["session_id"]


def _start_focus(sid, fk):
    return client.post(f"/api/v1/session/{sid}/focus/{fk}/start", headers=_hdr())


def _cancel(sid, fk, token="token-user-a"):
    return client.post(f"/api/v1/session/{sid}/focus/{fk}/cancel", headers=_hdr(token))


def _remaining(sid):
    return {x["focus_key"] for x in client.get(f"/api/v1/session/{sid}/focus-areas", headers=_hdr()).json()["remaining"]}


def _added(sid):
    return {x["focus_key"]: x["status"] for x in client.get(f"/api/v1/session/{sid}/focus-areas", headers=_hdr()).json()["added"]}


def _complete_focus_intake(sid, fk):
    st = _start_focus(sid, fk).json()
    q = st.get("current_question")
    while q:
        r = client.post(f"/api/v1/session/{sid}/focus/{fk}/answer", headers=_hdr(),
                        json={"question_id": q["question_id"], "answer": "yes"}).json()
        if r.get("status") == "interview_complete":
            break
        q = r.get("current_question")


def _ready(sid, fk="cognitive"):
    _complete_focus_intake(sid, fk)
    return client.post(f"/api/v1/session/{sid}/focus/{fk}/generate", headers=_hdr()).json()


def _iso_ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _set_status(sid, fk, status, **extra):
    doc = session_store.load("uid-a", sid)
    doc["added_focus"][fk]["status"] = status
    doc["added_focus"][fk].update(extra)
    session_store.save("uid-a", sid, doc)


def _finish_primary_and_plan(sid):
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    q = g.get("current_question")
    while q:
        a = client.post(f"/api/v1/session/{sid}/answer", headers=_hdr(),
                        json={"question_id": q["question_id"], "answer": "yes"}).json()
        if a.get("status") == "interview_complete":
            break
        q = a.get("current_question")
    return client.post(f"/api/v1/session/{sid}/plan", headers=_hdr()).json()


# ── 1. cancel interviewing → returns to remaining ───────────────────────────
def test_cancel_interviewing():
    print("\n── cancel an interviewing add-on")
    sid = _start()
    _start_focus(sid, "cognitive")
    check("cognitive removed from remaining after start", "cognitive" not in _remaining(sid))
    r = _cancel(sid, "cognitive")
    check("→ 200", r.status_code == 200, r.text[:160])
    b = r.json()
    check("status canceled", b["status"] == "canceled", b)
    check("payload remaining includes cognitive again",
          "cognitive" in {x["focus_key"] for x in b["remaining"]}, b["remaining"])
    check("focus-areas remaining includes cognitive", "cognitive" in _remaining(sid))
    check("focus-areas added no longer lists cognitive", "cognitive" not in _added(sid))
    check("doc added_focus entry removed", "cognitive" not in (session_store.load("uid-a", sid).get("added_focus") or {}))


# ── 2. cancel interview_complete → returns to remaining ─────────────────────
def test_cancel_interview_complete():
    print("\n── cancel an interview_complete add-on")
    sid = _start()
    _complete_focus_intake(sid, "cognitive")
    check("excluded from remaining when complete", "cognitive" not in _remaining(sid))
    r = _cancel(sid, "cognitive")
    check("→ 200 canceled", r.status_code == 200 and r.json()["status"] == "canceled", r.text[:160])
    check("back in remaining", "cognitive" in _remaining(sid))


# ── 3. cancel error → returns to remaining ──────────────────────────────────
def test_cancel_error():
    print("\n── cancel an errored add-on")
    sid = _start()
    _complete_focus_intake(sid, "cognitive")
    _set_status(sid, "cognitive", "error", error="boom")
    r = _cancel(sid, "cognitive")
    check("→ 200 canceled", r.status_code == 200 and r.json()["status"] == "canceled", r.text[:160])
    check("back in remaining", "cognitive" in _remaining(sid))


# ── 4. cancel ready → 409, module remains ───────────────────────────────────
def test_cancel_ready_blocked():
    print("\n── cancel a ready add-on is blocked (409)")
    sid = _start()
    m = _ready(sid)
    r = _cancel(sid, "cognitive")
    check("→ 409 focus_already_ready", r.status_code == 409 and r.json().get("detail") == "focus_already_ready", r.text[:160])
    check("module still present + ready", session_store.load("uid-a", sid)["added_focus"]["cognitive"]["status"] == "ready")
    check("still excluded from remaining", "cognitive" not in _remaining(sid))
    gm = client.get(f"/api/v1/session/{sid}/focus/cognitive", headers=_hdr()).json()
    check("GET /focus still returns module", gm["status"] == "ready" and "week" in gm.get("plan", {}))
    check("module_id unchanged", gm["module_id"] == m["module_id"])


# ── 5. cancel fresh-generating → 409; stale-generating → cancel ─────────────
def test_cancel_generating():
    print("\n── cancel generating: fresh 409, stale allowed")
    sid = _start()
    _complete_focus_intake(sid, "cognitive")
    _set_status(sid, "cognitive", "generating", generation_started_at=_iso_ago(30))
    r = _cancel(sid, "cognitive")
    check("fresh generating → 409 focus_already_generating",
          r.status_code == 409 and r.json().get("detail") == "focus_already_generating", r.text[:160])
    check("still excluded from remaining", "cognitive" not in _remaining(sid))
    # make it stale → cancel allowed
    _set_status(sid, "cognitive", "generating", generation_started_at=_iso_ago(ADDON_GENERATION_STALE_SECONDS + 120))
    r2 = _cancel(sid, "cognitive")
    check("stale generating → 200 canceled", r2.status_code == 200 and r2.json()["status"] == "canceled", r2.text[:160])
    check("back in remaining", "cognitive" in _remaining(sid))


# ── 6. cancel does not mutate primary plan / doc["plans"] / current_plan_id ─
def test_cancel_primary_byte_stable():
    print("\n── cancel does not touch the primary plan")
    sid = _start()
    _finish_primary_and_plan(sid)
    doc_b = session_store.load("uid-a", sid)
    plans_before = copy.deepcopy(doc_b["plans"])
    pid = doc_b["current_plan_id"]
    _start_focus(sid, "social_and_emotional")
    r = _cancel(sid, "social_and_emotional")
    check("cancel ok", r.status_code == 200 and r.json()["status"] == "canceled", r.text[:160])
    doc_a = session_store.load("uid-a", sid)
    check("doc['plans'] byte-identical", doc_a["plans"] == plans_before)
    check("current_plan_id unchanged", doc_a["current_plan_id"] == pid)
    check("social back in remaining", "social_and_emotional" in _remaining(sid))


# ── 7. /session and /session/current reflect the canceled focus ─────────────
def test_session_views_after_cancel():
    print("\n── /session + /session/current reflect cancel")
    sid = _start()
    _start_focus(sid, "cognitive")
    _cancel(sid, "cognitive")
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    check("not in added_focus_areas", "cognitive" not in {x["focus_key"] for x in g["focus"]["added_focus_areas"]})
    check("in remaining_focus_areas", "cognitive" in {x["key"] for x in g["focus"]["remaining_focus_areas"]})
    cur = client.get("/api/v1/session/current", headers=_hdr()).json()
    check("/session/current identical focus block", cur["focus"] == g["focus"])


# ── 8. idempotency + guards ─────────────────────────────────────────────────
def test_idempotent_and_guards():
    print("\n── idempotent cancel + guards")
    sid = _start()  # primary = language
    # never-started focus → idempotent 200 canceled
    r = _cancel(sid, "cognitive")
    check("never-started → 200 canceled (idempotent)", r.status_code == 200 and r.json()["status"] == "canceled", r.text[:160])
    check("cognitive in remaining", "cognitive" in _remaining(sid))
    # cancel twice → still 200
    _start_focus(sid, "cognitive")
    check("first cancel 200", _cancel(sid, "cognitive").status_code == 200)
    check("second cancel 200 (idempotent)", _cancel(sid, "cognitive").status_code == 200)
    # primary cannot be canceled
    rp = _cancel(sid, "language_and_communication")
    check("primary → 409 focus_is_primary", rp.status_code == 409 and rp.json().get("detail") == "focus_is_primary", rp.text[:160])
    # unknown focus → 404
    ru = _cancel(sid, "telepathy")
    check("unknown focus → 404 unknown_focus", ru.status_code == 404 and ru.json().get("detail") == "unknown_focus", ru.text[:160])
    # auth guards
    check("no token → 401", client.post(f"/api/v1/session/{sid}/focus/cognitive/cancel").status_code == 401)
    check("wrong user → 403", _cancel(sid, "cognitive", token="token-user-b").status_code == 403)
    check("unknown session → 404", client.post("/api/v1/session/nope/focus/cognitive/cancel", headers=_hdr()).status_code == 404)


# ── 9. cancel then re-add works (restart fresh) ─────────────────────────────
def test_cancel_then_readd():
    print("\n── cancel then re-add starts a fresh intake")
    sid = _start()
    first = _start_focus(sid, "cognitive").json()
    _cancel(sid, "cognitive")
    again = _start_focus(sid, "cognitive").json()
    check("re-add returns interviewing", again["status"] == "interviewing", again)
    check("fresh module_id (new add-on)", again["module_id"] != first["module_id"], (again.get("module_id"), first.get("module_id")))


def run_all():
    test_cancel_interviewing()
    test_cancel_interview_complete()
    test_cancel_error()
    test_cancel_ready_blocked()
    test_cancel_generating()
    test_cancel_primary_byte_stable()
    test_session_views_after_cancel()
    test_idempotent_and_guards()
    test_cancel_then_readd()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ focus cancel tests FAILED")
        sys.exit(1)
    print("✅ All focus cancel tests PASSED")


if __name__ == "__main__":
    run_all()
