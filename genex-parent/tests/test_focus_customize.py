"""
tests/test_focus_customize.py — Beta 2.2 Slice 2f-1: add-on remove / save-for-later

Remove + save-for-later for activities inside a READY add-on module. Overlay is
stored under doc["added_focus"][focus_key]["customizations"] (separate from the
primary doc["plan_customizations"]). GET /focus returns the resolved module; the
stored plan_response and the entire primary plan stay byte-identical. Overlay-only,
no bank, no LLM, no genex_core. ACTIVITY_MODEL empty.

In-process TestClient; Firebase mocked; local /tmp store.

Run: PYTHONPATH=. python3 tests/test_focus_customize.py
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


def _start(concern="speech delay and trouble talking"):
    r = client.post("/api/v1/session/start", headers=_hdr(), json={
        "child_name": "C", "age_years": 3, "age_months": 0, "age_in_months": 36,
        "diagnosis_or_condition": "No known diagnosis / not sure", "parent_concern": concern,
        "daily_time_minutes": 20, "timezone": "UTC", "beta_access_code": "genex"})
    return r.json()["session_id"]


def _complete_focus_intake(sid, fk):
    st = client.post(f"/api/v1/session/{sid}/focus/{fk}/start", headers=_hdr()).json()
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


def _get_focus(sid, fk="cognitive", token="token-user-a"):
    return client.get(f"/api/v1/session/{sid}/focus/{fk}", headers=_hdr(token))


def _activity_ids(module):
    return [c["id"] for d in module["plan"]["week"] for c in d["activities"]]


def _remove(sid, fk, aid, token="token-user-a", body=None):
    return client.post(f"/api/v1/session/{sid}/focus/{fk}/activity/{aid}/remove",
                       headers=_hdr(token), json=body)


def _save(sid, fk, aid, body=None):
    return client.post(f"/api/v1/session/{sid}/focus/{fk}/activity/{aid}/save-for-later",
                       headers=_hdr(), json=body)


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


# ── 1. remove → hidden from GET /focus ──────────────────────────────────────
def test_remove_hides():
    print("\n── remove an add-on activity hides it from GET /focus")
    sid = _start()
    m = _ready(sid)
    aid = _activity_ids(m)[0]
    n_before = len(_activity_ids(m))
    r = _remove(sid, "cognitive", aid)
    check("→ 200", r.status_code == 200, r.text[:160])
    b = r.json()
    check("removed True", b["removed"] is True, b)
    check("saved_for_later False", b["saved_for_later"] is False, b)
    check("summary removed_count 1", b["plan_customization_summary"]["removed_count"] == 1, b["plan_customization_summary"])
    after = _get_focus(sid).json()
    ids_after = [c["id"] for d in after["plan"]["week"] for c in d["activities"]]
    check("removed id gone from resolved plan", aid not in ids_after, ids_after)
    check("exactly one fewer activity", len(ids_after) == n_before - 1, (len(ids_after), n_before))
    check("GET customization summary present", after["plan_customization_summary"]["has_customizations"] is True)


# ── 2. save-for-later → hidden + listed in saved ────────────────────────────
def test_save_hides_and_lists():
    print("\n── save-for-later hides + records the activity")
    sid = _start()
    m = _ready(sid)
    aid = _activity_ids(m)[0]
    r = _save(sid, "cognitive", aid)
    check("→ 200", r.status_code == 200, r.text[:160])
    b = r.json()
    check("removed True", b["removed"] is True, b)
    check("saved_for_later True", b["saved_for_later"] is True, b)
    check("summary saved_for_later_count 1", b["plan_customization_summary"]["saved_for_later_count"] == 1, b["plan_customization_summary"])
    after = _get_focus(sid).json()
    ids_after = [c["id"] for d in after["plan"]["week"] for c in d["activities"]]
    check("saved id hidden from resolved plan", aid not in ids_after, ids_after)
    doc = session_store.load("uid-a", sid)
    cz = doc["added_focus"]["cognitive"]["customizations"]
    check("stored under added_focus[cognitive].customizations", aid in cz["saved_for_later_activity_ids"], cz)


# ── 3. repeat remove/save is idempotent ─────────────────────────────────────
def test_idempotent():
    print("\n── repeating remove/save is idempotent")
    sid = _start()
    m = _ready(sid)
    aid = _activity_ids(m)[0]
    _remove(sid, "cognitive", aid)
    _remove(sid, "cognitive", aid)
    _save(sid, "cognitive", aid)
    _save(sid, "cognitive", aid)
    doc = session_store.load("uid-a", sid)
    cz = doc["added_focus"]["cognitive"]["customizations"]
    check("removed_activity_ids has no dupes", cz["removed_activity_ids"].count(aid) == 1, cz["removed_activity_ids"])
    check("saved_for_later has no dupes", cz["saved_for_later_activity_ids"].count(aid) == 1, cz["saved_for_later_activity_ids"])


# ── 4. stored add-on plan_response is byte-stable ───────────────────────────
def test_stored_plan_response_byte_stable():
    print("\n── stored add-on plan_response unchanged by customization")
    sid = _start()
    m = _ready(sid)
    doc_b = session_store.load("uid-a", sid)
    pr_before = copy.deepcopy(doc_b["added_focus"]["cognitive"]["plan_response"])
    for aid in _activity_ids(m)[:2]:
        _remove(sid, "cognitive", aid)
    _save(sid, "cognitive", _activity_ids(m)[0])
    doc_a = session_store.load("uid-a", sid)
    check("entry plan_response byte-identical", doc_a["added_focus"]["cognitive"]["plan_response"] == pr_before)


# ── 5. primary plan + primary customizations byte-stable ────────────────────
def test_primary_byte_stable():
    print("\n── primary plan + primary customizations untouched")
    sid = _start()
    plan = _finish_primary_and_plan(sid)
    pid = session_store.load("uid-a", sid)["current_plan_id"]
    # apply a PRIMARY customization so plan_customizations is non-empty
    primary_aid = next(c["id"] for d in plan["week"] for c in d["activities"])
    client.post(f"/api/v1/session/{sid}/plan/{pid}/activity/{primary_aid}/remove", headers=_hdr())
    doc_b = session_store.load("uid-a", sid)
    plans_before = copy.deepcopy(doc_b["plans"])
    pc_before = copy.deepcopy(doc_b["plan_customizations"])
    cpid_before = doc_b["current_plan_id"]

    # now build + customize an add-on (movement reliably has activities)
    m = _ready(sid, "movement_and_physical")
    aids = _activity_ids(m)
    check("add-on module has activities", len(aids) > 0, len(aids))
    _remove(sid, "movement_and_physical", aids[0])
    _save(sid, "movement_and_physical", aids[0])

    doc_a = session_store.load("uid-a", sid)
    check("primary doc['plans'] byte-identical", doc_a["plans"] == plans_before)
    check("primary plan_customizations byte-identical", doc_a["plan_customizations"] == pc_before)
    check("current_plan_id unchanged", doc_a["current_plan_id"] == cpid_before)
    check("add-on overlay only under added_focus (count unchanged in plan_customizations)",
          len(doc_a.get("plan_customizations") or {}) == len(pc_before))


# ── 6. guards: unknown focus / not ready / unknown activity / stale module ──
def test_guards():
    print("\n── guards")
    sid = _start()
    # not ready (interviewing)
    client.post(f"/api/v1/session/{sid}/focus/cognitive/start", headers=_hdr())
    r1 = _remove(sid, "cognitive", "whatever")
    check("not ready → 409 focus_not_ready", r1.status_code == 409 and r1.json().get("detail") == "focus_not_ready", r1.text[:140])
    # ready now
    m = _ready(sid)
    aid = _activity_ids(m)[0]
    # unknown focus
    r2 = _remove(sid, "telepathy", aid)
    check("unknown focus → 404 unknown_focus", r2.status_code == 404 and r2.json().get("detail") == "unknown_focus", r2.text[:140])
    # not started focus
    r3 = _remove(sid, "movement_and_physical", aid)
    check("not started → 404 focus_not_started", r3.status_code == 404 and r3.json().get("detail") == "focus_not_started", r3.text[:140])
    # unknown activity
    r4 = _remove(sid, "cognitive", "no-such-id")
    check("unknown activity → 404 activity_not_found", r4.status_code == 404 and r4.json().get("detail") == "activity_not_found", r4.text[:140])
    # stale module
    r5 = _remove(sid, "cognitive", aid, body={"module_id": "not-the-real-one"})
    check("stale module → 409 stale_module", r5.status_code == 409 and r5.json().get("detail") == "stale_module", r5.text[:140])
    # correct module_id works
    r6 = _remove(sid, "cognitive", aid, body={"module_id": m["module_id"]})
    check("correct module_id → 200", r6.status_code == 200, r6.text[:140])
    # auth guards
    check("no token → 401", client.post(f"/api/v1/session/{sid}/focus/cognitive/activity/{aid}/remove").status_code == 401)
    check("wrong user → 403", _remove(sid, "cognitive", aid, token="token-user-b").status_code == 403)


# ── 7. restore: GET /focus after reload still resolves removed/saved ────────
def test_restore_after_reload():
    print("\n── restore: resolution survives reload + /session/current")
    sid = _start()
    m = _ready(sid)
    aid = _activity_ids(m)[0]
    _remove(sid, "cognitive", aid)
    # reload via a fresh GET (server re-reads the doc each request)
    after = _get_focus(sid).json()
    check("removed still hidden after reload", aid not in [c["id"] for d in after["plan"]["week"] for c in d["activities"]])
    # /session and /session/current still show status only (no module bloat)
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    entry = next(x for x in g["focus"]["added_focus_areas"] if x["focus_key"] == "cognitive")
    check("/session shows ready status only", entry["status"] == "ready" and "plan" not in entry)
    cur = client.get("/api/v1/session/current", headers=_hdr()).json()
    check("/session/current focus identical", cur["focus"] == g["focus"])


# ── 8. cancel drops the entry AND its overlay atomically ────────────────────
def test_cancel_drops_overlay():
    print("\n── cancel removes the entry together with its overlay")
    sid = _start()
    # interviewing entry with an injected overlay → cancelable; overlay must go with it
    client.post(f"/api/v1/session/{sid}/focus/cognitive/start", headers=_hdr())
    doc = session_store.load("uid-a", sid)
    doc["added_focus"]["cognitive"]["customizations"] = {
        "removed_activity_ids": ["x"], "saved_for_later_activity_ids": ["x"],
        "activity_overrides": {}, "added_activities": []}
    session_store.save("uid-a", sid, doc)
    r = client.post(f"/api/v1/session/{sid}/focus/cognitive/cancel", headers=_hdr())
    check("cancel → 200 canceled", r.status_code == 200 and r.json()["status"] == "canceled", r.text[:140])
    doc2 = session_store.load("uid-a", sid)
    check("entry (and overlay) removed", "cognitive" not in (doc2.get("added_focus") or {}))
    check("no orphan in plan_customizations", not (doc2.get("plan_customizations") or {}))
    # ready customized add-on stays non-cancelable (overlay preserved, not orphaned)
    m = _ready(sid)
    _remove(sid, "cognitive", _activity_ids(m)[0])
    rc = client.post(f"/api/v1/session/{sid}/focus/cognitive/cancel", headers=_hdr())
    check("cancel ready customized → 409 focus_already_ready",
          rc.status_code == 409 and rc.json().get("detail") == "focus_already_ready", rc.text[:140])
    check("overlay lives inside the entry (not plan_customizations)",
          "customizations" in session_store.load("uid-a", sid)["added_focus"]["cognitive"])


# ── 9. fresh generate response is identity-safe (no customizations) ─────────
def test_fresh_generate_identity_safe():
    print("\n── a fresh ready module resolves identity-safe (no overlay)")
    sid = _start()
    m = _ready(sid)
    check("fresh module has empty customization summary",
          m["plan_customization_summary"]["has_customizations"] is False, m.get("plan_customization_summary"))
    doc = session_store.load("uid-a", sid)
    check("no customizations key written until first edit",
          "customizations" not in doc["added_focus"]["cognitive"])


def run_all():
    test_remove_hides()
    test_save_hides_and_lists()
    test_idempotent()
    test_stored_plan_response_byte_stable()
    test_primary_byte_stable()
    test_guards()
    test_restore_after_reload()
    test_cancel_drops_overlay()
    test_fresh_generate_identity_safe()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ focus customize tests FAILED")
        sys.exit(1)
    print("✅ All focus customize tests PASSED")


if __name__ == "__main__":
    run_all()
