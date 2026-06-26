"""
tests/test_focus_hardening.py — Beta 2.2 Slice 2d: add-on focus hardening

Stale-generating recovery, retry/idempotency, restore/repeat, and the post-ready
size trim. The primary plan stays byte-identical through every hardening path.
No new generation paths, no LLM beyond the existing /generate. ACTIVITY_MODEL empty.

In-process TestClient; Firebase mocked; local /tmp store.

Run: PYTHONPATH=. python3 tests/test_focus_hardening.py
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


def _generate(sid, fk):
    return client.post(f"/api/v1/session/{sid}/focus/{fk}/generate", headers=_hdr())


def _get_focus(sid, fk):
    return client.get(f"/api/v1/session/{sid}/focus/{fk}", headers=_hdr())


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
    return _generate(sid, fk).json()


def _iso_ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


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


# ── 1. ready /generate returns cached module (same id + generated_at) ────────
def test_ready_idempotent_cached():
    print("\n── ready /generate is cached (no regeneration)")
    sid = _start()
    m1 = _ready(sid)
    m2 = _generate(sid, "cognitive").json()
    check("same module_id", m1["module_id"] == m2["module_id"], (m1["module_id"], m2["module_id"]))
    check("same generated_at", m1["generated_at"] == m2["generated_at"], (m1["generated_at"], m2["generated_at"]))
    check("plan identical", m1["plan"] == m2["plan"])


# ── 2. error /generate retries and clears error on success ──────────────────
def test_error_retry_clears():
    print("\n── error → retry → ready (error cleared)")
    sid = _start()
    _complete_focus_intake(sid, "cognitive")
    doc = session_store.load("uid-a", sid)
    doc["added_focus"]["cognitive"]["status"] = "error"
    doc["added_focus"]["cognitive"]["error"] = "boom"
    doc["added_focus"]["cognitive"]["error_at"] = _iso_ago(10)
    session_store.save("uid-a", sid, doc)
    r = _generate(sid, "cognitive")
    check("retry → ready", r.status_code == 200 and r.json()["status"] == "ready", r.text[:160])
    doc2 = session_store.load("uid-a", sid)
    e = doc2["added_focus"]["cognitive"]
    check("error cleared", "error" not in e and "error_at" not in e, list(e))


# ── 3. generating + NOT stale → 409 ─────────────────────────────────────────
def test_generating_fresh_409():
    print("\n── fresh generating → 409 (no duplicate)")
    sid = _start()
    _complete_focus_intake(sid, "cognitive")
    doc = session_store.load("uid-a", sid)
    doc["added_focus"]["cognitive"]["status"] = "generating"
    doc["added_focus"]["cognitive"]["generation_started_at"] = _iso_ago(30)  # just started
    session_store.save("uid-a", sid, doc)
    r = _generate(sid, "cognitive")
    check("→ 409 focus_already_generating",
          r.status_code == 409 and r.json().get("detail") == "focus_already_generating", r.text[:160])
    check("GET reports generating", _get_focus(sid, "cognitive").json()["status"] == "generating")


# ── 4. generating + STALE → recovers (re-generates) ─────────────────────────
def test_generating_stale_recovers():
    print(f"\n── stale generating (> {ADDON_GENERATION_STALE_SECONDS}s) recovers")
    sid = _start()
    _complete_focus_intake(sid, "cognitive")
    doc = session_store.load("uid-a", sid)
    doc["added_focus"]["cognitive"]["status"] = "generating"
    doc["added_focus"]["cognitive"]["generation_started_at"] = _iso_ago(ADDON_GENERATION_STALE_SECONDS + 120)
    session_store.save("uid-a", sid, doc)
    r = _generate(sid, "cognitive")
    check("stale → recovers → ready", r.status_code == 200 and r.json()["status"] == "ready", r.text[:160])
    # missing timestamp is also treated as stale (never permanently blocked)
    sid2 = _start()
    _complete_focus_intake(sid2, "cognitive")
    d2 = session_store.load("uid-a", sid2)
    d2["added_focus"]["cognitive"]["status"] = "generating"
    d2["added_focus"]["cognitive"].pop("generation_started_at", None)
    d2["added_focus"]["cognitive"].pop("updated_at", None)
    session_store.save("uid-a", sid2, d2)
    r2 = _generate(sid2, "cognitive")
    check("missing timestamp treated as stale → recovers", r2.status_code == 200 and r2.json()["status"] == "ready", r2.text[:160])


# ── 5. retries never create a duplicate module ──────────────────────────────
def test_no_duplicate_module():
    print("\n── retries do not duplicate the module")
    sid = _start()
    m1 = _ready(sid)
    mid = m1["module_id"]
    # several more /generate calls (all cached) + a forced stale recovery
    for _ in range(3):
        _generate(sid, "cognitive")
    doc = session_store.load("uid-a", sid)
    check("exactly one added_focus entry for cognitive", list(doc["added_focus"].keys()).count("cognitive") == 1)
    check("module_id stable across calls", doc["added_focus"]["cognitive"]["module_id"] == mid)
    check("status still ready", doc["added_focus"]["cognitive"]["status"] == "ready")


# ── 6. GET /focus reports all five states ───────────────────────────────────
def test_get_focus_all_states():
    print("\n── GET /focus resumes interviewing/interview_complete/generating/ready/error")
    sid = _start()
    check("before start → 404", _get_focus(sid, "cognitive").status_code == 404)
    client.post(f"/api/v1/session/{sid}/focus/cognitive/start", headers=_hdr())
    check("interviewing", _get_focus(sid, "cognitive").json()["status"] == "interviewing")
    _complete_focus_intake(sid, "cognitive")
    check("interview_complete", _get_focus(sid, "cognitive").json()["status"] == "interview_complete")
    # inject generating + error to assert GET reflects them
    doc = session_store.load("uid-a", sid)
    doc["added_focus"]["cognitive"]["status"] = "generating"
    doc["added_focus"]["cognitive"]["generation_started_at"] = _iso_ago(30)
    session_store.save("uid-a", sid, doc)
    check("generating", _get_focus(sid, "cognitive").json()["status"] == "generating")
    doc["added_focus"]["cognitive"]["status"] = "error"
    doc["added_focus"]["cognitive"]["error"] = "x"
    session_store.save("uid-a", sid, doc)
    ge = _get_focus(sid, "cognitive").json()
    check("error + retryable flag", ge["status"] == "error" and ge.get("ready_for_generate") is True, ge)
    # now actually generate → ready
    _generate(sid, "cognitive")
    gr = _get_focus(sid, "cognitive").json()
    check("ready + full plan", gr["status"] == "ready" and "week" in gr.get("plan", {}), list(gr)[:8])


# ── 7. /session + /session/current carry status only (no full module) ───────
def test_session_views_metadata_only():
    print("\n── /session + /session/current carry add-on status only")
    sid = _start()
    _ready(sid)
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    entry = next(x for x in g["focus"]["added_focus_areas"] if x["focus_key"] == "cognitive")
    check("status ready", entry["status"] == "ready", entry)
    check("no plan/plan_response/brain_state in entry",
          not any(k in entry for k in ("plan", "plan_response", "plan_internal", "brain_state")), entry)
    cur = client.get("/api/v1/session/current", headers=_hdr()).json()
    check("/session/current focus identical", cur["focus"] == g["focus"])


# ── 8. remaining excludes interviewing/interview_complete/generating/ready ──
def test_remaining_excludes_active_statuses():
    print("\n── remaining excludes active statuses; error stays addable")
    sid = _start()  # primary = language
    doc = session_store.load("uid-a", sid)
    doc.setdefault("added_focus", {})
    doc["added_focus"]["movement_and_physical"] = {"focus_key": "movement_and_physical",
        "focus_label": "x", "status": "interviewing", "module_id": "m1"}
    doc["added_focus"]["cognitive"] = {"focus_key": "cognitive",
        "focus_label": "x", "status": "interview_complete", "module_id": "m2"}
    doc["added_focus"]["social_and_emotional"] = {"focus_key": "social_and_emotional",
        "focus_label": "x", "status": "generating", "module_id": "m3", "generation_started_at": _iso_ago(10)}
    session_store.save("uid-a", sid, doc)
    rem = {x["focus_key"] for x in client.get(f"/api/v1/session/{sid}/focus-areas", headers=_hdr()).json()["remaining"]}
    check("interviewing excluded", "movement_and_physical" not in rem, rem)
    check("interview_complete excluded", "cognitive" not in rem, rem)
    check("generating excluded", "social_and_emotional" not in rem, rem)
    # flip one to error → re-addable
    doc["added_focus"]["cognitive"]["status"] = "error"
    session_store.save("uid-a", sid, doc)
    rem2 = {x["focus_key"] for x in client.get(f"/api/v1/session/{sid}/focus-areas", headers=_hdr()).json()["remaining"]}
    check("error focus stays addable (in remaining)", "cognitive" in rem2, rem2)


# ── 9. primary plan byte-stable through hardening paths ─────────────────────
def test_primary_byte_stable_through_hardening():
    print("\n── primary plan byte-stable across stale-recovery + retry + trim")
    sid = _start()
    _finish_primary_and_plan(sid)
    doc_b = session_store.load("uid-a", sid)
    plans_before = copy.deepcopy(doc_b["plans"])
    pid = doc_b["current_plan_id"]

    _complete_focus_intake(sid, "social_and_emotional")
    # force a stale-generating then recover
    d = session_store.load("uid-a", sid)
    d["added_focus"]["social_and_emotional"]["status"] = "generating"
    d["added_focus"]["social_and_emotional"]["generation_started_at"] = _iso_ago(ADDON_GENERATION_STALE_SECONDS + 60)
    session_store.save("uid-a", sid, d)
    _generate(sid, "social_and_emotional")           # recover → ready (+ trim)
    _generate(sid, "social_and_emotional")           # cached

    doc_a = session_store.load("uid-a", sid)
    check("doc['plans'] byte-identical", doc_a["plans"] == plans_before)
    check("current_plan_id unchanged", doc_a["current_plan_id"] == pid)


# ── 10. trimmed ready add-on keeps a lean regeneration-capable context ──────
def test_trim_after_ready():
    print("\n── ready trim: lean regen context kept, bulky dropped, full module served")
    sid = _start()
    m = _ready(sid)
    doc = session_store.load("uid-a", sid)
    e = doc["added_focus"]["cognitive"]
    import json as _json
    bs = e.get("brain_state") or {}
    raw_bytes = len(_json.dumps(e, default=str))

    # (2) bulky scheduling artifacts removed
    for bulky in ("weekly_schedule", "week1_schedule", "bridge_plans",
                  "weekly_slot_allocation", "_gate_report"):
        check(f"bulky dropped: {bulky}", bulky not in bs, bulky)
    check("interview is lean summary (no band_state)", "band_state" not in (e.get("interview") or {}), e.get("interview"))
    # Slice 2f-2 (Option A): activity_banks RETAINED for LLM-free swap.
    check("activity_banks retained for swap", bool(bs.get("activity_banks")), list(bs))

    # (1)(3) brain_state retained with regeneration-capable context for Slice 2e
    check("brain_state retained (not dropped)", isinstance(e.get("brain_state"), dict), list(e))
    for need in ("child", "qna", "dev_age", "concern_profile", "safety_profile",
                 "selected_domain_keys", "family_guidance_floor"):
        check(f"regen context kept: {need}", need in bs, need)
    check("selected_domain_keys == [cognitive]", bs.get("selected_domain_keys") == ["cognitive"], bs.get("selected_domain_keys"))
    check("qna has the cognitive answers", bool((bs.get("qna") or {}).get("cognitive")), list((bs.get("qna") or {})))

    # smaller than the full raw generation state (~137 KB). With activity_banks
    # retained for swap (Slice 2f-2), the trimmed entry is still well below raw.
    check("trimmed entry smaller than raw (< 110 KB)", raw_bytes < 110000, raw_bytes)

    # (3b) the lean context can actually regenerate via run_plan_pipeline (Slice-2e shape)
    from api.pipeline import run_plan_pipeline
    import copy as _copy
    regen, _ = run_plan_pipeline(brain_state=_copy.deepcopy(bs), admin_debug=False)
    check("lean context regenerates activity_banks", bool(regen.get("activity_banks")), list(regen.get("activity_banks") or {}))
    check("lean context regenerates weekly_schedule", bool(regen.get("weekly_schedule")))

    # preserved display/history/report fields
    for keep in ("plan_response", "plan_internal", "dev_age_summary", "plan_period"):
        check(f"preserved: {keep}", keep in e, keep)
    check("preserved: module_id + generated_at", e.get("module_id") and e.get("generated_at"))
    check("generation_started_at cleared", "generation_started_at" not in e)

    # (4) GET /focus still returns the full module
    gr = _get_focus(sid, "cognitive").json()
    check("GET /focus full module after trim", "week" in gr.get("plan", {}) and len(
        [c for d in gr["plan"]["week"] for c in d["activities"]]) > 0)
    # (5) cached /generate after trim still works
    check("cached /generate after trim still works",
          _generate(sid, "cognitive").json()["module_id"] == m["module_id"])


def run_all():
    test_ready_idempotent_cached()
    test_error_retry_clears()
    test_generating_fresh_409()
    test_generating_stale_recovers()
    test_no_duplicate_module()
    test_get_focus_all_states()
    test_session_views_metadata_only()
    test_remaining_excludes_active_statuses()
    test_primary_byte_stable_through_hardening()
    test_trim_after_ready()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ focus hardening tests FAILED")
        sys.exit(1)
    print("✅ All focus hardening tests PASSED")


if __name__ == "__main__":
    run_all()
