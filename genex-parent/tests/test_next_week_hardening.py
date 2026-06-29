"""
tests/test_next_week_hardening.py — Beta 2.2 Slice 2e-3: integrated next-week hardening

Restore / reports / history / customization / idempotency / byte-stability after an
integrated next-week, plus a realistic mixed-answer profile. The swap/add tests
(test 8) assert the DESIRED behaviour (add-on-domain cards customizable like normal
plan activities); they fail until the integrated brain_state carries all active-domain
banks. ACTIVITY_MODEL empty.

Run: PYTHONPATH=. python3 tests/test_next_week_hardening.py
"""

import copy
import os
import sys
from collections import Counter

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


def _hdr():
    return {"Authorization": "Bearer token-user-a"}


def _start_and_plan(answer="yes"):
    sid = client.post("/api/v1/session/start", headers=_hdr(), json={
        "child_name": "C", "age_years": 3, "age_months": 0, "age_in_months": 36,
        "diagnosis_or_condition": "No known diagnosis / not sure",
        "parent_concern": "speech delay and trouble talking",
        "daily_time_minutes": 20, "timezone": "UTC", "beta_access_code": "genex"}).json()["session_id"]
    _answer_all(sid, None, answer)
    plan = client.post(f"/api/v1/session/{sid}/plan", headers=_hdr()).json()
    return sid, plan


def _answer_all(sid, fk, answer="yes"):
    """Answer the primary (fk=None) or add-on interview. `answer` may be a callable
    (idx)->str for mixed profiles, or a fixed string."""
    i = 0
    if fk is None:
        g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
        q = g.get("current_question")
        post = lambda qid, a: client.post(f"/api/v1/session/{sid}/answer", headers=_hdr(),
                                          json={"question_id": qid, "answer": a}).json()
    else:
        st = client.post(f"/api/v1/session/{sid}/focus/{fk}/start", headers=_hdr()).json()
        q = st.get("current_question")
        post = lambda qid, a: client.post(f"/api/v1/session/{sid}/focus/{fk}/answer", headers=_hdr(),
                                          json={"question_id": qid, "answer": a}).json()
    while q:
        a = answer(i) if callable(answer) else answer
        i += 1
        r = post(q["question_id"], a)
        if r.get("status") == "interview_complete":
            break
        q = r.get("current_question")


def _ready_addon(sid, fk, answer="yes"):
    _answer_all(sid, fk, answer)
    return client.post(f"/api/v1/session/{sid}/focus/{fk}/generate", headers=_hdr()).json()


def _make_eligible(sid, plan):
    doc = session_store.load("uid-a", sid)
    doc["plans"][plan["plan_period"]["plan_id"]]["plan_period"]["plan_end_date"] = _PAST_SUNDAY
    session_store.save("uid-a", sid, doc)


def _next_week(sid):
    return client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr())


def _cards(plan):
    return [c for d in plan["week"] for c in d["activities"]]


def _setup_integrated(addons=("cognitive",)):
    sid, plan = _start_and_plan()
    for fk in addons:
        _ready_addon(sid, fk)
    _make_eligible(sid, plan)
    w2 = _next_week(sid).json()
    return sid, plan, w2


# ── 1. /session/current returns the integrated Week-2 cleanly ───────────────
def test_session_current_integrated():
    print("\n── /session/current returns integrated Week-2")
    sid, plan, w2 = _setup_integrated()
    cur = client.get("/api/v1/session/current", headers=_hdr()).json()
    check("status plan_ready", cur.get("status") == "plan_ready", cur.get("status"))
    check("current_plan_id == Week-2", cur.get("current_plan_id") == w2["plan_period"]["plan_id"])
    doms = {c["domain"] for c in _cards(cur["plan"])}
    check("both domains visible on restore", {"language_and_communication", "cognitive"} <= doms, doms)
    check("plan_period is_integrated", cur["plan"]["plan_period"].get("is_integrated") is True)
    check("cards carry focus provenance",
          all(c.get("focus_key") and c.get("focus_label") for c in _cards(cur["plan"])))


# ── 2. no stale add-on UI for already-integrated domains ────────────────────
def test_no_stale_addon_ui():
    print("\n── focus-areas stays sane (no stale re-add of active domains)")
    sid, plan, w2 = _setup_integrated()
    fa = client.get(f"/api/v1/session/{sid}/focus-areas", headers=_hdr()).json()
    added = {x["focus_key"]: x["status"] for x in fa["added"]}
    remaining = {x["focus_key"] for x in fa["remaining"]}
    check("cognitive still listed as added/ready (history)", added.get("cognitive") == "ready", added)
    check("cognitive NOT offered in remaining (not re-addable)", "cognitive" not in remaining, remaining)
    check("primary not in remaining", "language_and_communication" not in remaining, remaining)


# ── 3. report + feedback_summary with integrated + add-on feedback ──────────
def test_reports_after_integration():
    print("\n── reports/progress work after integration")
    sid, plan, w2 = _setup_integrated()
    # feedback on an integrated cognitive card + a language card
    for c in _cards(w2)[:3]:
        day = next(d["day"] for d in w2["week"] for cc in d["activities"] if cc["id"] == c["id"])
        client.post(f"/api/v1/session/{sid}/feedback", headers=_hdr(), json={
            "plan_id": w2["plan_period"]["plan_id"], "activity_id": c["id"], "day": day,
            "activity_date": c["activity_date"], "enjoyment": "loved_it",
            "difficulty": "just_right", "completion": "did_it", "discuss_with_care_team": True,
            "care_team_tags": ["doctor"]})
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    fs = g["feedback_summary"]
    check("feedback_summary completed >= 1", fs["completed"] >= 1, fs)
    check("domains_practised non-empty", len(fs["domains_practised"]) >= 1, fs)
    rr = client.post(f"/api/v1/session/{sid}/report", headers=_hdr(), json={"report_type": "doctor"})
    check("report 200 non-empty", rr.status_code == 200 and len(rr.json().get("body", "")) > 0, rr.text[:120])


# ── 4. GET /focus old add-on module still readable as history ───────────────
def test_old_addon_module_history():
    print("\n── old add-on module remains readable as history/context")
    sid, plan, w2 = _setup_integrated()
    gf = client.get(f"/api/v1/session/{sid}/focus/cognitive", headers=_hdr()).json()
    check("GET /focus cognitive → ready with plan", gf["status"] == "ready" and "week" in gf.get("plan", {}), gf.get("status"))
    check("module plan_period present (current-week history)", "plan_period" in gf)


# ── 5. focus-areas state sane after integration ─────────────────────────────
def test_focus_areas_sane():
    print("\n── focus-areas listing consistent after integration")
    sid, plan, w2 = _setup_integrated()
    fa = client.get(f"/api/v1/session/{sid}/focus-areas", headers=_hdr()).json()
    keys = {x["focus_key"] for x in fa["added"]} | {x["focus_key"] for x in fa["remaining"]} | {fa["primary"]["focus_key"]}
    check("all 4 focus areas accounted for", keys == {"language_and_communication", "cognitive", "movement_and_physical", "social_and_emotional"}, keys)


# ── 6. primary customization on integrated primary-domain card ──────────────
def test_primary_customization_integrated():
    print("\n── primary customization works on integrated primary-domain card")
    sid, plan, w2 = _setup_integrated()
    pid = w2["plan_period"]["plan_id"]
    lang = next(c for c in _cards(w2) if c["domain"] == "language_and_communication")
    sg = client.get(f"/api/v1/session/{sid}/plan/{pid}/activity/{lang['id']}/swap-suggestions", headers=_hdr()).json()["suggestions"]
    check("language card has swap suggestions", len(sg) >= 1, len(sg))
    check("suggestions are language domain", all(s["domain"] == "language_and_communication" for s in sg), {s["domain"] for s in sg})
    r = client.post(f"/api/v1/session/{sid}/plan/{pid}/activity/{lang['id']}/swap", headers=_hdr(), json={"suggestion_id": sg[0]["suggestion_id"]})
    check("primary swap → 200", r.status_code == 200 and r.json()["swapped"] is True, r.text[:120])


# ── 7. remove/save works on integrated add-on-domain card ───────────────────
def test_remove_save_addon_domain_card():
    print("\n── remove/save works on integrated add-on-domain card")
    sid, plan, w2 = _setup_integrated()
    pid = w2["plan_period"]["plan_id"]
    cog = next(c for c in _cards(w2) if c["domain"] == "cognitive")
    r = client.post(f"/api/v1/session/{sid}/plan/{pid}/activity/{cog['id']}/remove", headers=_hdr())
    check("remove add-on-domain card → 200 removed", r.status_code == 200 and r.json()["removed"] is True, r.text[:120])
    cur = client.get("/api/v1/session/current", headers=_hdr()).json()
    check("removed card hidden from resolved plan", cog["id"] not in {c["id"] for c in _cards(cur["plan"])})


# ── 8. swap/add on integrated add-on-domain card (DESIRED behaviour) ────────
def test_swap_add_addon_domain_card():
    print("\n── [AUDIT] swap/add on integrated add-on-domain card should span all domains")
    sid, plan, w2 = _setup_integrated()
    pid = w2["plan_period"]["plan_id"]
    cog = next(c for c in _cards(w2) if c["domain"] == "cognitive")
    sg = client.get(f"/api/v1/session/{sid}/plan/{pid}/activity/{cog['id']}/swap-suggestions", headers=_hdr()).json()["suggestions"]
    check("cognitive card → cognitive swap suggestions (not wrong-domain)",
          len(sg) >= 1 and all(s["domain"] == "cognitive" for s in sg), {s["domain"] for s in sg})
    add_cog = client.get(f"/api/v1/session/{sid}/plan/{pid}/activity-suggestions?domain=cognitive", headers=_hdr()).json()["suggestions"]
    check("add domain=cognitive returns cognitive suggestions", len(add_cog) >= 1 and all(s["domain"] == "cognitive" for s in add_cog), {s["domain"] for s in add_cog})
    # The product path is domain-filtered (parent picks a focus) — each active domain
    # must be reachable. (Unfiltered add-suggestions keeps the existing primary
    # behaviour: first `limit` across banks in order, so it may be primary-first.)
    add_lang = client.get(f"/api/v1/session/{sid}/plan/{pid}/activity-suggestions?domain=language_and_communication", headers=_hdr()).json()["suggestions"]
    check("add can suggest from EACH active domain (filtered)",
          len(add_lang) >= 1 and all(s["domain"] == "language_and_communication" for s in add_lang)
          and len(add_cog) >= 1, {"lang": len(add_lang), "cog": len(add_cog)})


# ── 9. realistic mixed-answer profile populates multiple domains ────────────
def test_mixed_answer_profile():
    print("\n── mixed-answer profile populates multiple domains + balances")
    # mixed answers (rotate) lower dev-age in some areas so add-on banks populate
    mix = lambda i: ["no", "with_help", "sometimes", "yes"][i % 4]
    sid = client.post("/api/v1/session/start", headers=_hdr(), json={
        "child_name": "C", "age_years": 3, "age_months": 0, "age_in_months": 36,
        "diagnosis_or_condition": "No known diagnosis / not sure",
        "parent_concern": "speech delay, trouble paying attention, clumsy, shy with peers",
        "daily_time_minutes": 20, "timezone": "UTC", "beta_access_code": "genex"}).json()["session_id"]
    _answer_all(sid, None, mix)
    plan = client.post(f"/api/v1/session/{sid}/plan", headers=_hdr()).json()
    populated = []
    for fk in ["cognitive", "movement_and_physical", "social_and_emotional"]:
        m = _ready_addon(sid, fk, mix)
        n = sum(len(d["activities"]) for d in m["plan"]["week"])
        if n > 0:
            populated.append(fk)
    check("at least 2 add-on domains populate with mixed answers", len(populated) >= 2, populated)
    _make_eligible(sid, plan)
    w2 = _next_week(sid).json()
    WD = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    base = max(len(d["activities"]) for d in plan["week"])
    wds = [d for d in w2["week"] if d["day"] in WD]
    check("no weekday exceeds baseline (budget held)", all(len(d["activities"]) <= base for d in wds), [(d["day"], len(d["activities"])) for d in wds])
    cnt = Counter(c["domain"] for d in wds for c in d["activities"])
    check("multiple domains represented in integrated week", len(cnt) >= 2, dict(cnt))
    check("weekday distribution balanced (diff ≤ 1)", (max(cnt.values()) - min(cnt.values())) <= 1, dict(cnt))


# ── 10. idempotency ─────────────────────────────────────────────────────────
def test_idempotency():
    print("\n── integrated next-week idempotent")
    sid, plan, w2 = _setup_integrated()
    r2 = _next_week(sid).json()
    check("same Week-2 plan_id", w2["plan_period"]["plan_id"] == r2["plan_period"]["plan_id"])
    check("exactly 2 plans", len(session_store.load("uid-a", sid)["plans"]) == 2)


# ── 11,12. old primary plan + add-on modules byte-stable ────────────────────
def test_byte_stability():
    print("\n── old primary plan + add-on modules byte-stable through integration")
    sid, plan = _start_and_plan()
    m = _ready_addon(sid, "cognitive")
    base_id = plan["plan_period"]["plan_id"]
    _make_eligible(sid, plan)
    doc_b = session_store.load("uid-a", sid)
    base_before = copy.deepcopy(doc_b["plans"][base_id])
    addon_before = copy.deepcopy(doc_b["added_focus"]["cognitive"])
    _next_week(sid)
    doc_a = session_store.load("uid-a", sid)
    check("old primary plan byte-identical", doc_a["plans"][base_id] == base_before)
    check("old add-on module byte-identical", doc_a["added_focus"]["cognitive"] == addon_before)


# ── 13. bank union does NOT change the generated schedule ───────────────────
def test_bank_union_does_not_change_schedule():
    print("\n── bank union affects only activity_banks, not the Week-2 schedule")
    from api.pipeline import run_integrated_next_week
    primary_bs = {"activity_banks": {"language_and_communication": {"activities": [{"title": "L1"}]}}}
    merged = {"days": {
        "Monday": {"items": [{"category_key": "language_and_communication", "title": "L1", "_source_activity": {}}],
                   "total_minutes": 5, "is_weekend": False}}}
    fb = {}
    s_no = run_integrated_next_week(primary_bs, merged, fb, ["language_and_communication"])
    s_yes = run_integrated_next_week(
        primary_bs, merged, fb, ["language_and_communication", "cognitive"],
        addon_brain_states=[{"activity_banks": {"cognitive": {"activities": [{"title": "C1"}]}}}])
    check("Week-2 weekly_schedule identical with/without union",
          s_no["weekly_schedule"] == s_yes["weekly_schedule"])
    check("union adds the cognitive bank (and only that)",
          "cognitive" in s_yes["activity_banks"] and "cognitive" not in (s_no.get("activity_banks") or {}))


def run_all():
    test_bank_union_does_not_change_schedule()
    test_session_current_integrated()
    test_no_stale_addon_ui()
    test_reports_after_integration()
    test_old_addon_module_history()
    test_focus_areas_sane()
    test_primary_customization_integrated()
    test_remove_save_addon_domain_card()
    test_swap_add_addon_domain_card()
    test_mixed_answer_profile()
    test_idempotency()
    test_byte_stability()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ next-week hardening tests FAILED")
        sys.exit(1)
    print("✅ All next-week hardening tests PASSED")


if __name__ == "__main__":
    run_all()
