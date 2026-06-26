"""
tests/test_next_week_integrated.py — Beta 2.2 Slice 2e-2: integrated next-week

POST /plan/next-week now generates ONE integrated weekly plan across all active focus
areas (primary + ready add-ons) by reconstructing each add-on's Week-1 schedule from
its retained bank, unioning with the primary Week-1, and repeat-adapting with merged
feedback via the frozen cycle_week=2 builder. Stored as a normal plan under
doc["plans"]; current-week primary plan + add-on modules + overlays stay byte-stable.
No genex_core change, no new LLM path. ACTIVITY_MODEL empty.

Run: PYTHONPATH=. python3 tests/test_next_week_integrated.py
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


def _start_and_plan(concern="speech delay and trouble talking"):
    sid = client.post("/api/v1/session/start", headers=_hdr(), json={
        "child_name": "C", "age_years": 3, "age_months": 0, "age_in_months": 36,
        "diagnosis_or_condition": "No known diagnosis / not sure", "parent_concern": concern,
        "daily_time_minutes": 20, "timezone": "UTC", "beta_access_code": "genex"}).json()["session_id"]
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    q = g.get("current_question")
    while q:
        a = client.post(f"/api/v1/session/{sid}/answer", headers=_hdr(),
                        json={"question_id": q["question_id"], "answer": "yes"}).json()
        if a.get("status") == "interview_complete":
            break
        q = a.get("current_question")
    plan = client.post(f"/api/v1/session/{sid}/plan", headers=_hdr()).json()
    return sid, plan


def _ready_addon(sid, fk):
    st = client.post(f"/api/v1/session/{sid}/focus/{fk}/start", headers=_hdr()).json()
    q = st.get("current_question")
    while q:
        r = client.post(f"/api/v1/session/{sid}/focus/{fk}/answer", headers=_hdr(),
                        json={"question_id": q["question_id"], "answer": "yes"}).json()
        if r.get("status") == "interview_complete":
            break
        q = r.get("current_question")
    return client.post(f"/api/v1/session/{sid}/focus/{fk}/generate", headers=_hdr()).json()


def _make_eligible(sid, plan):
    doc = session_store.load("uid-a", sid)
    doc["plans"][plan["plan_period"]["plan_id"]]["plan_period"]["plan_end_date"] = _PAST_SUNDAY
    session_store.save("uid-a", sid, doc)


def _next_week(sid):
    return client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr())


def _domains(plan):
    return {c["domain"] for d in plan["week"] for c in d["activities"]}


def _cards(plan):
    return [c for d in plan["week"] for c in d["activities"]]


# ── 1. primary-only next-week unchanged ─────────────────────────────────────
def test_primary_only_unchanged():
    print("\n── primary-only next-week: behaviour unchanged")
    sid, plan = _start_and_plan()
    _make_eligible(sid, plan)
    r = _next_week(sid)
    check("→ 200", r.status_code == 200, r.text[:160])
    w2 = r.json()
    check("cycle_week 2", w2["plan_period"].get("cycle_week") == 2)
    check("NOT integrated (no marker)", w2["plan_period"].get("is_integrated") is None, w2["plan_period"])
    check("only primary domain present", _domains(w2) <= {"language_and_communication"}, _domains(w2))


# ── 2. primary + one ready add-on → both domains ────────────────────────────
def test_one_addon_integrated():
    print("\n── integrated next-week includes primary + one add-on domain")
    sid, plan = _start_and_plan()
    _ready_addon(sid, "cognitive")
    _make_eligible(sid, plan)
    r = _next_week(sid)
    check("→ 200", r.status_code == 200, r.text[:200])
    w2 = r.json()
    check("integrated marker set", w2["plan_period"].get("is_integrated") is True, w2["plan_period"])
    check("active_focus_areas = primary + cognitive",
          set(w2["plan_period"]["active_focus_areas"]) == {"language_and_communication", "cognitive"},
          w2["plan_period"].get("active_focus_areas"))
    doms = _domains(w2)
    check("both domains present", {"language_and_communication", "cognitive"} <= doms, doms)
    check("stored under doc['plans'] as normal plan",
          w2["plan_period"]["plan_id"] in session_store.load("uid-a", sid)["plans"])


# ── 3. primary + multiple ready add-ons → all active focuses ────────────────
def test_multiple_addons_integrated():
    print("\n── integrated next-week includes all ready add-on focuses")
    sid, plan = _start_and_plan()
    _ready_addon(sid, "cognitive")
    _ready_addon(sid, "movement_and_physical")
    _make_eligible(sid, plan)
    w2 = _next_week(sid).json()
    afa = set(w2["plan_period"]["active_focus_areas"])
    check("active = primary + cognitive + movement",
          afa == {"language_and_communication", "cognitive", "movement_and_physical"}, afa)
    doms = _domains(w2)
    check("all three domains in plan", {"language_and_communication", "cognitive", "movement_and_physical"} <= doms, doms)


# ── 4. non-ready add-ons excluded ───────────────────────────────────────────
def test_non_ready_excluded():
    print("\n── interviewing/interview_complete add-on is NOT integrated")
    sid, plan = _start_and_plan()
    _ready_addon(sid, "cognitive")
    # start a second focus but leave it interviewing (not generated)
    client.post(f"/api/v1/session/{sid}/focus/movement_and_physical/start", headers=_hdr())
    _make_eligible(sid, plan)
    w2 = _next_week(sid).json()
    afa = set(w2["plan_period"]["active_focus_areas"])
    check("movement (interviewing) excluded", "movement_and_physical" not in afa, afa)
    check("cognitive (ready) included", "cognitive" in afa, afa)


# ── 5. ready add-on without retained bank → skipped gracefully ──────────────
def test_old_addon_without_bank_skipped():
    print("\n── ready add-on lacking activity_banks is skipped (no crash)")
    sid, plan = _start_and_plan()
    _ready_addon(sid, "cognitive")
    # simulate a pre-2f-2 ready add-on: strip its activity_banks
    doc = session_store.load("uid-a", sid)
    doc["added_focus"]["cognitive"]["brain_state"].pop("activity_banks", None)
    session_store.save("uid-a", sid, doc)
    _make_eligible(sid, plan)
    r = _next_week(sid)
    check("→ 200 (no crash)", r.status_code == 200, r.text[:200])
    w2 = r.json()
    check("cognitive in skipped_focus_areas", "cognitive" in w2["plan_period"].get("skipped_focus_areas", []),
          w2["plan_period"])
    check("cognitive NOT in active_focus_areas", "cognitive" not in w2["plan_period"].get("active_focus_areas", []))
    check("plan still produced (primary domain present)", "language_and_communication" in _domains(w2))


# ── 6. merged feedback (primary + add-on) influences the plan ───────────────
def test_merged_feedback_influences():
    print("\n── add-on feedback flows into merged activity_feedback (repeat cues)")
    sid, plan = _start_and_plan()
    m = _ready_addon(sid, "cognitive")
    # log "too_easy / did_it" feedback on every add-on activity → expect 'harder' cues
    for d in m["plan"]["week"]:
        for c in d["activities"]:
            client.post(f"/api/v1/session/{sid}/feedback", headers=_hdr(), json={
                "plan_id": m["module_id"], "activity_id": c["id"], "day": d["day"],
                "activity_date": c["activity_date"], "enjoyment": "loved_it",
                "difficulty": "too_easy", "completion": "did_it"})
    _make_eligible(sid, plan)
    w2 = _next_week(sid).json()
    cog_cards = [c for c in _cards(w2) if c["domain"] == "cognitive"]
    check("integrated plan has cognitive cards", len(cog_cards) > 0, len(cog_cards))
    modes = {c.get("repeat_mode") for c in cog_cards}
    check("add-on feedback produced 'harder' repeat cues", "harder" in modes, modes)


# ── 7. every integrated card has focus/domain provenance ────────────────────
def test_provenance_on_cards():
    print("\n── every integrated card carries focus/domain provenance")
    sid, plan = _start_and_plan()
    _ready_addon(sid, "cognitive")
    _make_eligible(sid, plan)
    w2 = _next_week(sid).json()
    cards = _cards(w2)
    check("cards present", len(cards) > 0, len(cards))
    ok = all(
        c.get("focus_key") == c.get("domain") and c.get("domain") and c.get("domain_label")
        and c.get("focus_label") and c.get("source") == "primary"
        and c.get("focus_origin") in ("primary", "added")
        for c in cards
    )
    check("focus_key/focus_label/domain/domain_label/source/focus_origin on all cards", ok,
          cards[0] if cards else None)
    cog = [c for c in cards if c["domain"] == "cognitive"]
    check("cognitive cards flagged focus_origin=added", all(c["focus_origin"] == "added" for c in cog), cog[:1])


# ── 8,9,10. byte-stability: primary plan, add-on modules, overlays ──────────
def test_byte_stability():
    print("\n── current-week primary plan + add-on modules + overlays byte-stable")
    sid, plan = _start_and_plan()
    m = _ready_addon(sid, "cognitive")
    # add a customization to the add-on so its overlay is non-empty
    aid = m["plan"]["week"][0]["activities"][0]["id"]
    client.post(f"/api/v1/session/{sid}/focus/cognitive/activity/{aid}/remove", headers=_hdr())
    base_id = plan["plan_period"]["plan_id"]
    _make_eligible(sid, plan)
    doc_b = session_store.load("uid-a", sid)
    base_plan_before = copy.deepcopy(doc_b["plans"][base_id])
    addon_before = copy.deepcopy(doc_b["added_focus"]["cognitive"])
    pc_before = copy.deepcopy(doc_b.get("plan_customizations") or {})

    w2 = _next_week(sid).json()
    doc_a = session_store.load("uid-a", sid)
    check("base (current-week) plan byte-identical", doc_a["plans"][base_id] == base_plan_before)
    check("add-on module byte-identical (incl. its overlay)", doc_a["added_focus"]["cognitive"] == addon_before)
    check("primary plan_customizations byte-identical", (doc_a.get("plan_customizations") or {}) == pc_before)
    check("current_plan_id advanced to Week 2", doc_a["current_plan_id"] == w2["plan_period"]["plan_id"])
    check("base plan still in plans (history)", base_id in doc_a["plans"])


# ── 11. Monday gating intact ────────────────────────────────────────────────
def test_gating_intact():
    print("\n── Week-2 Monday gating still enforced (with add-ons present)")
    sid, plan = _start_and_plan()
    _ready_addon(sid, "cognitive")
    # do NOT make eligible → today is before available_from
    r = _next_week(sid)
    check("→ 409 next_week_not_ready", r.status_code == 409 and r.json()["detail"]["code"] == "next_week_not_ready", r.text[:160])
    # the add-on + base must be untouched by the rejected attempt
    doc = session_store.load("uid-a", sid)
    check("no Week-2 plan created", len(doc["plans"]) == 1, list(doc["plans"]))


# ── 12. idempotency / no duplicate ──────────────────────────────────────────
def test_idempotent():
    print("\n── integrated next-week is idempotent (no duplicate)")
    sid, plan = _start_and_plan()
    _ready_addon(sid, "cognitive")
    _make_eligible(sid, plan)
    r1 = _next_week(sid).json()
    r2 = _next_week(sid).json()
    r3 = _next_week(sid).json()
    check("same Week-2 plan_id across calls",
          r1["plan_period"]["plan_id"] == r2["plan_period"]["plan_id"] == r3["plan_period"]["plan_id"])
    doc = session_store.load("uid-a", sid)
    check("exactly 2 plans (Week1 + Week2)", len(doc["plans"]) == 2, list(doc["plans"]))


def run_all():
    test_primary_only_unchanged()
    test_one_addon_integrated()
    test_multiple_addons_integrated()
    test_non_ready_excluded()
    test_old_addon_without_bank_skipped()
    test_merged_feedback_influences()
    test_provenance_on_cards()
    test_byte_stability()
    test_gating_intact()
    test_idempotent()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ integrated next-week tests FAILED")
        sys.exit(1)
    print("✅ All integrated next-week tests PASSED")


if __name__ == "__main__":
    run_all()
