"""
tests/test_feedback_provenance.py — Beta 2.2 Slice 2e-1: add-on-aware feedback

/feedback now resolves metadata + provenance for BOTH primary plans and ready add-on
modules. Records gain source/focus_key/focus_label/domain_label/module_id/
original_activity_id/plan_period_id/cycle_week. translate_feedback_to_activity_feedback
optionally includes add-on feedback (same output shape). Additive only: primary plan +
add-on modules byte-stable (feedback only appends to doc["feedback"]). No next-week
generation change, no genex_core change, no LLM. ACTIVITY_MODEL empty.

Run: PYTHONPATH=. python3 tests/test_feedback_provenance.py
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
_TOKENS = {"token-user-a": {"uid": "uid-a", "email": "a@example.com"}}
firebase_auth.verify_id_token = lambda t, *a, **k: _TOKENS[t] if t in _TOKENS else (_ for _ in ()).throw(
    firebase_auth.InvalidIdTokenError("bad"))

from fastapi.testclient import TestClient  # noqa: E402
from api.main import app  # noqa: E402
from api import session_store  # noqa: E402
from api.pipeline import translate_feedback_to_activity_feedback  # noqa: E402

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
    return client.post("/api/v1/session/start", headers=_hdr(), json={
        "child_name": "C", "age_years": 3, "age_months": 0, "age_in_months": 36,
        "diagnosis_or_condition": "No known diagnosis / not sure", "parent_concern": concern,
        "daily_time_minutes": 20, "timezone": "UTC", "beta_access_code": "genex"}).json()["session_id"]


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


def _ready_addon(sid, fk="cognitive"):
    st = client.post(f"/api/v1/session/{sid}/focus/{fk}/start", headers=_hdr()).json()
    q = st.get("current_question")
    while q:
        r = client.post(f"/api/v1/session/{sid}/focus/{fk}/answer", headers=_hdr(),
                        json={"question_id": q["question_id"], "answer": "yes"}).json()
        if r.get("status") == "interview_complete":
            break
        q = r.get("current_question")
    return client.post(f"/api/v1/session/{sid}/focus/{fk}/generate", headers=_hdr()).json()


def _get_focus(sid, fk="cognitive"):
    return client.get(f"/api/v1/session/{sid}/focus/{fk}", headers=_hdr()).json()


def _addon_cards(m):
    return [c for d in m["plan"]["week"] for c in d["activities"]]


def _addon_cards_with_day(m):
    """Yield (card, day) so feedback can pass the card's real day (resolution filters by day)."""
    return [(c, d["day"]) for d in m["plan"]["week"] for c in d["activities"]]


def _day_of(module, activity_id):
    for d in module["plan"]["week"]:
        for c in d["activities"]:
            if c["id"] == activity_id:
                return d["day"]
    return "Monday"


def _feedback(sid, plan_id, card):
    return client.post(f"/api/v1/session/{sid}/feedback", headers=_hdr(), json={
        "plan_id": plan_id, "activity_id": card["id"], "day": card.get("day") or "Monday",
        "activity_date": card["activity_date"], "enjoyment": "loved_it",
        "difficulty": "just_right", "completion": "did_it", "discuss_with_care_team": False})


def _fb_for(card, day):
    # build a feedback body using a plan_response card (which has no 'day' field) + its day
    return {"id": card["id"], "activity_date": card["activity_date"], "day": day}


def _last_record(sid):
    return session_store.load("uid-a", sid)["feedback"][-1]


# ── 1. primary feedback → source=primary, metadata preserved ────────────────
def test_primary_feedback_source():
    print("\n── primary feedback: source=primary + metadata preserved")
    sid = _start()
    plan = _finish_primary_and_plan(sid)
    pid = session_store.load("uid-a", sid)["current_plan_id"]
    day0 = plan["week"][0]
    card = day0["activities"][0]
    r = client.post(f"/api/v1/session/{sid}/feedback", headers=_hdr(), json={
        "plan_id": pid, "activity_id": card["id"], "day": day0["day"],
        "activity_date": card["activity_date"], "enjoyment": "loved_it",
        "difficulty": "just_right", "completion": "did_it", "discuss_with_care_team": False})
    check("→ 200", r.status_code == 200, r.text[:160])
    check("metadata_found", r.json()["metadata_found"] is True)
    rec = _last_record(sid)
    check("source primary", rec["source"] == "primary", rec.get("source"))
    check("domain preserved (existing behavior)", rec.get("domain") == card["domain"], (rec.get("domain"), card["domain"]))
    check("focus_key == domain", rec.get("focus_key") == card["domain"])
    check("domain_label set", bool(rec.get("domain_label")))
    check("module_id None for primary", rec.get("module_id") is None)
    check("cycle_week 1", rec.get("cycle_week") == 1)
    check("plan_period_id set", rec.get("plan_period_id") == pid)


# ── 2. add-on feedback → source=addon + provenance ──────────────────────────
def test_addon_feedback_provenance():
    print("\n── add-on feedback: source=addon + focus/module provenance")
    sid = _start()
    _finish_primary_and_plan(sid)
    m = _ready_addon(sid, "cognitive")
    card, day = _addon_cards_with_day(m)[0]
    r = client.post(f"/api/v1/session/{sid}/feedback", headers=_hdr(), json={
        "plan_id": m["module_id"], "activity_id": card["id"], "day": day,
        "activity_date": card["activity_date"], "enjoyment": "loved_it",
        "difficulty": "just_right", "completion": "did_it", "discuss_with_care_team": False})
    check("→ 200", r.status_code == 200, r.text[:160])
    check("metadata_found", r.json()["metadata_found"] is True)
    rec = _last_record(sid)
    check("source addon", rec["source"] == "addon", rec.get("source"))
    check("focus_key cognitive", rec.get("focus_key") == "cognitive", rec.get("focus_key"))
    check("focus_label friendly", rec.get("focus_label") == "Learning, Attention & Thinking", rec.get("focus_label"))
    check("module_id set", rec.get("module_id") == m["module_id"])
    check("domain cognitive", rec.get("domain") == "cognitive", rec.get("domain"))
    check("domain_label set", bool(rec.get("domain_label")))
    check("plan_period_id == add-on period", rec.get("plan_period_id") == m["plan_period"]["plan_id"])
    check("original_activity_id None (not swapped)", rec.get("original_activity_id") is None)


# ── 3. add-on swapped feedback resolves replacement internal ────────────────
def test_addon_swapped_feedback():
    print("\n── add-on swapped activity feedback resolves replacement internal")
    sid = _start()
    _finish_primary_and_plan(sid)
    m = _ready_addon(sid, "cognitive")
    aid = _addon_cards(m)[0]["id"]
    sugg = client.get(f"/api/v1/session/{sid}/focus/cognitive/activity/{aid}/swap-suggestions", headers=_hdr()).json()["suggestions"]
    swr = client.post(f"/api/v1/session/{sid}/focus/cognitive/activity/{aid}/swap", headers=_hdr(),
                      json={"suggestion_id": sugg[0]["suggestion_id"]}).json()
    repl_id = swr["replacement_activity_id"]
    gf = _get_focus(sid)
    repl = next(c for c in _addon_cards(gf) if c["id"] == repl_id)
    r = client.post(f"/api/v1/session/{sid}/feedback", headers=_hdr(), json={
        "plan_id": m["module_id"], "activity_id": repl_id, "day": "Monday",
        "activity_date": repl["activity_date"], "enjoyment": "loved_it",
        "difficulty": "just_right", "completion": "did_it"})
    check("→ 200 metadata_found", r.status_code == 200 and r.json()["metadata_found"] is True, r.text[:160])
    rec = _last_record(sid)
    check("source addon", rec["source"] == "addon")
    check("domain resolved from replacement_internal", rec.get("domain") == "cognitive", rec.get("domain"))
    check("original_activity_id == swapped original", rec.get("original_activity_id") == aid, (rec.get("original_activity_id"), aid))


# ── 4. add-on added activity feedback resolves added internal ───────────────
def test_addon_added_feedback():
    print("\n── add-on added activity feedback resolves added internal")
    sid = _start()
    _finish_primary_and_plan(sid)
    m = _ready_addon(sid, "cognitive")
    sg = client.get(f"/api/v1/session/{sid}/focus/cognitive/activity-suggestions", headers=_hdr()).json()["suggestions"][0]
    days = {d["day"]: d["date"] for d in m["plan"]["week"]}
    target_day = list(days.keys())[0]
    ar = client.post(f"/api/v1/session/{sid}/focus/cognitive/activities/add", headers=_hdr(),
                     json={"suggestion_id": sg["suggestion_id"], "day": target_day}).json()
    added_id = ar["activity_id"]
    r = client.post(f"/api/v1/session/{sid}/feedback", headers=_hdr(), json={
        "plan_id": m["module_id"], "activity_id": added_id, "day": target_day,
        "activity_date": ar["activity_date"], "enjoyment": "loved_it",
        "difficulty": "just_right", "completion": "did_it"})
    check("→ 200 metadata_found", r.status_code == 200 and r.json()["metadata_found"] is True, r.text[:160])
    rec = _last_record(sid)
    check("source addon", rec["source"] == "addon")
    check("domain cognitive (added internal)", rec.get("domain") == "cognitive", rec.get("domain"))
    check("module_id set", rec.get("module_id") == m["module_id"])


# ── 5. remove/save do NOT create feedback records ───────────────────────────
def test_no_fake_feedback_records():
    print("\n── remove/save do not fabricate feedback records")
    sid = _start()
    _finish_primary_and_plan(sid)
    m = _ready_addon(sid, "cognitive")
    n_before = len(session_store.load("uid-a", sid).get("feedback", []))
    aid = _addon_cards(m)[0]["id"]
    client.post(f"/api/v1/session/{sid}/focus/cognitive/activity/{aid}/remove", headers=_hdr())
    aid2 = _addon_cards(m)[1]["id"]
    client.post(f"/api/v1/session/{sid}/focus/cognitive/activity/{aid2}/save-for-later", headers=_hdr())
    n_after = len(session_store.load("uid-a", sid).get("feedback", []))
    check("no feedback records created by remove/save", n_after == n_before, (n_before, n_after))


# ── 6. feedback_summary / reports include add-on feedback safely ────────────
def test_summary_includes_addon():
    print("\n── feedback_summary + report include add-on feedback safely")
    sid = _start()
    _finish_primary_and_plan(sid)
    m = _ready_addon(sid, "cognitive")
    card, day = _addon_cards_with_day(m)[0]
    client.post(f"/api/v1/session/{sid}/feedback", headers=_hdr(), json={
        "plan_id": m["module_id"], "activity_id": card["id"], "day": day,
        "activity_date": card["activity_date"], "enjoyment": "loved_it",
        "difficulty": "just_right", "completion": "did_it",
        "discuss_with_care_team": True, "care_team_tags": ["doctor"], "note": "did great"})
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    fs = g["feedback_summary"]
    check("summary counts the add-on completion", fs["completed"] >= 1, fs)
    check("cognitive in domains_practised", "cognitive" in fs["domains_practised"], fs)
    check("flagged_for_care_team counted", fs["flagged_for_care_team"] >= 1, fs)
    # report renders without error and is non-empty
    rr = client.post(f"/api/v1/session/{sid}/report", headers=_hdr(), json={"report_type": "doctor"})
    check("report 200 non-empty", rr.status_code == 200 and len(rr.json().get("body", "")) > 0, rr.text[:120])


# ── 7. translate includes primary + add-on (same output shape) ──────────────
def test_translate_includes_both():
    print("\n── translate_feedback_to_activity_feedback includes primary + add-on")
    sid = _start()
    plan = _finish_primary_and_plan(sid)
    pid = session_store.load("uid-a", sid)["current_plan_id"]
    pcard = plan["week"][0]["activities"][0]
    client.post(f"/api/v1/session/{sid}/feedback", headers=_hdr(), json={
        "plan_id": pid, "activity_id": pcard["id"], "day": plan["week"][0]["day"],
        "activity_date": pcard["activity_date"], "enjoyment": "loved_it",
        "difficulty": "too_easy", "completion": "did_it"})
    m = _ready_addon(sid, "cognitive")
    acard = _addon_cards(m)[0]
    client.post(f"/api/v1/session/{sid}/feedback", headers=_hdr(), json={
        "plan_id": m["module_id"], "activity_id": acard["id"], "day": "Monday",
        "activity_date": acard["activity_date"], "enjoyment": "not_really",
        "difficulty": "too_hard", "completion": "wasnt_ready_yet"})
    doc = session_store.load("uid-a", sid)
    base_pr = doc["plans"][pid]["plan_response"]
    addon_pr = _get_focus(sid)["plan"]
    # primary-only (legacy 3-arg) → has primary domain, NOT cognitive
    legacy = translate_feedback_to_activity_feedback(doc["feedback"], base_pr, pid)
    check("legacy 3-arg unchanged: primary domain present", pcard["domain"] in legacy, list(legacy))
    check("legacy 3-arg unchanged: add-on domain absent", "cognitive" not in legacy, list(legacy))
    # with extra_plans → both
    both = translate_feedback_to_activity_feedback(
        doc["feedback"], base_pr, pid,
        extra_plans=[{"plan_id": m["module_id"], "plan_response": addon_pr}])
    check("with extras: primary domain present", pcard["domain"] in both, list(both))
    check("with extras: add-on cognitive present", "cognitive" in both, list(both))
    check("output shape unchanged (category→title→signal)",
          all(isinstance(v, dict) and all(set(sig) >= {"difficulty", "performance", "engagement"}
              for sig in v.values()) for v in both.values()), both)


# ── 8,9. primary plan + add-on module byte-stable (only feedback appends) ───
def test_byte_stability():
    print("\n── primary plan + add-on module byte-stable across feedback")
    sid = _start()
    _finish_primary_and_plan(sid)
    m = _ready_addon(sid, "cognitive")
    doc_b = session_store.load("uid-a", sid)
    plans_before = copy.deepcopy(doc_b["plans"])
    pid = doc_b["current_plan_id"]
    addon_before = copy.deepcopy(doc_b["added_focus"]["cognitive"])
    fb_before = len(doc_b.get("feedback", []))
    card = _addon_cards(m)[0]
    client.post(f"/api/v1/session/{sid}/feedback", headers=_hdr(), json={
        "plan_id": m["module_id"], "activity_id": card["id"], "day": "Monday",
        "activity_date": card["activity_date"], "enjoyment": "loved_it",
        "difficulty": "just_right", "completion": "did_it"})
    doc_a = session_store.load("uid-a", sid)
    check("primary doc['plans'] byte-identical", doc_a["plans"] == plans_before)
    check("current_plan_id unchanged", doc_a["current_plan_id"] == pid)
    check("add-on module byte-identical (no entry mutation)", doc_a["added_focus"]["cognitive"] == addon_before)
    check("feedback list grew by 1", len(doc_a["feedback"]) == fb_before + 1)


def run_all():
    test_primary_feedback_source()
    test_addon_feedback_provenance()
    test_addon_swapped_feedback()
    test_addon_added_feedback()
    test_no_fake_feedback_records()
    test_summary_includes_addon()
    test_translate_includes_both()
    test_byte_stability()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ feedback provenance tests FAILED")
        sys.exit(1)
    print("✅ All feedback provenance tests PASSED")


if __name__ == "__main__":
    run_all()
