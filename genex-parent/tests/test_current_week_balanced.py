"""
tests/test_current_week_balanced.py — Beta 2.2: balanced current-week view

GET /session(/current) now includes an additive read-only `current_week_plan` that
merges the resolved primary plan + ready add-on modules and rebalances today→Sunday to
the original daily budget (no stacking). Past days stay primary-only. Stored primary
plan + add-on modules + overlays are never mutated; the existing `plan` field is
unchanged. Cards carry routing provenance. No genex_core / new endpoint / new LLM.

Run: PYTHONPATH=. python3 tests/test_current_week_balanced.py
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
from api.adapters import build_balanced_current_week  # noqa: E402
from collections import Counter  # noqa: E402

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


def _hdr():
    return {"Authorization": "Bearer token-user-a"}


def _start_plan(daily=10, concern="ADHD, lack of attention, trouble paying attention", ans="with_help"):
    sid = client.post("/api/v1/session/start", headers=_hdr(), json={
        "child_name": "Bob", "age_years": 4, "age_months": 0, "age_in_months": 48,
        "diagnosis_or_condition": "ADHD", "parent_concern": concern,
        "daily_time_minutes": daily, "timezone": "UTC", "beta_access_code": "genex"}).json()["session_id"]
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    q = g.get("current_question")
    while q:
        a = client.post(f"/api/v1/session/{sid}/answer", headers=_hdr(),
                        json={"question_id": q["question_id"], "answer": ans}).json()
        if a.get("status") == "interview_complete":
            break
        q = a.get("current_question")
    client.post(f"/api/v1/session/{sid}/plan", headers=_hdr())
    return sid


def _add(sid, fk, ans="with_help"):
    st = client.post(f"/api/v1/session/{sid}/focus/{fk}/start", headers=_hdr()).json()
    q = st.get("current_question")
    while q:
        r = client.post(f"/api/v1/session/{sid}/focus/{fk}/answer", headers=_hdr(),
                        json={"question_id": q["question_id"], "answer": ans}).json()
        if r.get("status") == "interview_complete":
            break
        q = r.get("current_question")
    client.post(f"/api/v1/session/{sid}/focus/{fk}/generate", headers=_hdr())


def _cwp(sid):
    return client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()["current_week_plan"]


def _perday(plan):
    return [(d["day"], len(d["activities"])) for d in plan["week"]]


# ── 1. no add-on → current_week_plan matches the primary plan ───────────────
def test_no_addon_matches_primary():
    print("\n── no add-on → current_week_plan == primary activities")
    sid = _start_plan()
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    prim_ids = [a["id"] for d in g["plan"]["week"] for a in d["activities"]]
    cwp_ids = [a["activity_id"] for d in g["current_week_plan"]["week"] for a in d["activities"]]
    check("current_week_plan present + is_balanced", g["current_week_plan"].get("is_balanced") is True)
    check("same activities as primary (ids match)", prim_ids == cwp_ids, (prim_ids[:2], cwp_ids[:2]))
    check("active_focus_areas == [primary]", g["current_week_plan"]["active_focus_areas"] == ["cognitive"])


# ── 2. one add-on, 10 min → ~2/day total (not 4) ────────────────────────────
def test_one_addon_two_per_day():
    print("\n── one add-on → balanced 2/day total (not stacked 4)")
    sid = _start_plan(daily=10)
    _add(sid, "language_and_communication")
    cwp = _cwp(sid)
    pd = _perday(cwp)
    check("every day has 2 activities (budget held)", all(n == 2 for _, n in pd), pd)
    doms = Counter(a["focus_key"] for d in cwp["week"] for a in d["activities"])
    check("both focuses represented", {"cognitive", "language_and_communication"} <= set(doms), dict(doms))
    check("balanced split (diff ≤ 1)", max(doms.values()) - min(doms.values()) <= 1, dict(doms))
    # Compatibility shim: legacy 'plan' now MIRRORS the balanced display plan
    # (primary + add-on, budget-balanced) so 'plan'-rendering clients show add-ons.
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    pdoms = Counter(a["focus_key"] for d in g["plan"]["week"] for a in d["activities"])
    check("legacy 'plan' mirrors balanced (2/day, both focuses)",
          all(len(d["activities"]) == 2 for d in g["plan"]["week"])
          and {"cognitive", "language_and_communication"} <= set(pdoms), dict(pdoms))
    check("legacy 'plan' == current_week_plan", g["plan"]["week"] == cwp["week"])


# ── 3. multiple add-ons → still ~2/day, balanced across week ────────────────
def test_multi_addon_balanced():
    print("\n── two add-ons → still 2/day, balanced across the week")
    sid = _start_plan(daily=10)
    _add(sid, "language_and_communication")
    _add(sid, "movement_and_physical")
    cwp = _cwp(sid)
    pd = _perday(cwp)
    check("still 2/day (no inflation to 6)", all(n <= 2 for _, n in pd) and max(n for _, n in pd) == 2, pd)
    doms = Counter(a["focus_key"] for d in cwp["week"] for a in d["activities"])
    check("3 active focuses represented across the week", len(set(doms)) >= 3, dict(doms))
    check("weekly distribution balanced (diff ≤ 1)", max(doms.values()) - min(doms.values()) <= 1, dict(doms))


# ── 4. provenance on every visible card ─────────────────────────────────────
def test_provenance():
    print("\n── every balanced card carries routing provenance")
    sid = _start_plan(daily=10)
    _add(sid, "language_and_communication")
    cwp = _cwp(sid)
    cards = [a for d in cwp["week"] for a in d["activities"]]
    def ok(c):
        base = (c.get("source") in ("primary", "addon") and c.get("focus_origin") in ("primary", "added")
                and c.get("focus_key") and c.get("focus_label") and c.get("domain") and c.get("domain_label")
                and c.get("activity_id") and c.get("activity_date"))
        route = (c["plan_id"] is not None and c["module_id"] is None) if c["source"] == "primary" \
            else (c["module_id"] is not None and c["plan_id"] is None)
        return base and route
    check("all cards have full provenance + routing target", all(ok(c) for c in cards), next((c for c in cards if not ok(c)), None))
    prim = [c for c in cards if c["source"] == "primary"]
    add = [c for c in cards if c["source"] == "addon"]
    check("primary cards route to plan_id", prim and all(c["plan_id"] for c in prim))
    check("add-on cards route to module_id + focus_key", add and all(c["module_id"] and c["focus_key"] == "language_and_communication" for c in add))


# ── 5,6,7,8. byte-stability of stored plan / modules / overlays ─────────────
def test_byte_stability():
    print("\n── building the balanced view never mutates stored data")
    sid = _start_plan(daily=10)
    _add(sid, "language_and_communication")
    # add a customization so an overlay exists on both primary and add-on
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    pid = g["current_plan_id"]
    paid = g["plan"]["week"][0]["activities"][0]["id"]
    client.post(f"/api/v1/session/{sid}/plan/{pid}/activity/{paid}/remove", headers=_hdr())
    m = client.get(f"/api/v1/session/{sid}/focus/language_and_communication", headers=_hdr()).json()
    aaid = m["plan"]["week"][0]["activities"][0]["id"]
    client.post(f"/api/v1/session/{sid}/focus/language_and_communication/activity/{aaid}/remove", headers=_hdr())
    before = copy.deepcopy(session_store.load("uid-a", sid))
    # read the balanced view several times
    for _ in range(3):
        client.get(f"/api/v1/session/{sid}", headers=_hdr())
        client.get("/api/v1/session/current", headers=_hdr())
    after = session_store.load("uid-a", sid)
    check("stored doc['plans'] byte-identical", after["plans"] == before["plans"])
    check("stored added_focus byte-identical", after["added_focus"] == before["added_focus"])
    check("stored plan_customizations byte-identical", after.get("plan_customizations") == before.get("plan_customizations"))


# ── 9. past days stay primary-only / unchanged ──────────────────────────────
def test_past_days_unchanged():
    print("\n── past days keep the primary activities (unit-level)")
    # Synthetic primary (Mon→Sun, 2/day) + add-on (Tue→Sun, 2/day); today = Wednesday.
    def mk(domain, days, n=2):
        return {"plan_period": {"plan_start_date": days[0][1], "plan_end_date": days[-1][1]},
                "week": [{"day": d, "date": dt, "activities": [
                    {"id": f"{domain[:3]}-{dt}-{i}", "domain": domain, "title": f"{domain} {dt} {i}",
                     "activity_date": dt} for i in range(n)]} for d, dt in days]}
    pdays = [("Monday", "2026-06-29"), ("Tuesday", "2026-06-30"), ("Wednesday", "2026-07-01"),
             ("Thursday", "2026-07-02")]
    adays = [("Tuesday", "2026-06-30"), ("Wednesday", "2026-07-01"), ("Thursday", "2026-07-02")]
    primary = mk("cognitive", pdays)
    addon = {"focus_key": "language_and_communication", "focus_label": "Speech & Communication",
             "module_id": "mod-1", "plan_response": mk("language_and_communication", adays)}
    cwp = build_balanced_current_week(
        session_id="s", primary_plan_response=primary, primary_plan_id="p1",
        primary_focus_key="cognitive", addon_modules=[addon], today_iso="2026-07-01",
        daily_time_minutes=10, age_in_months=48)
    by = {d["date"]: d["activities"] for d in cwp["week"]}
    # Mon + Tue are past → primary-only, unchanged (2 cognitive each)
    check("Mon (past) primary-only", all(a["focus_key"] == "cognitive" for a in by["2026-06-29"]) and len(by["2026-06-29"]) == 2)
    check("Tue (past) primary-only (add-on NOT injected into a past day)",
          all(a["focus_key"] == "cognitive" for a in by["2026-06-30"]) and len(by["2026-06-30"]) == 2)
    # Wed + Thu (today/future) → balanced 1 cognitive + 1 language
    for dt in ("2026-07-01", "2026-07-02"):
        fks = {a["focus_key"] for a in by[dt]}
        check(f"{dt} (today/future) balanced both focuses", fks == {"cognitive", "language_and_communication"} and len(by[dt]) == 2, [a["focus_key"] for a in by[dt]])


# ── 10. compatibility shim: legacy `plan` mirrors balanced display plan ─────
def test_plan_shim_compat():
    print("\n── compatibility shim: legacy `plan` == balanced display plan")
    sid = _start_plan(daily=10)

    # No add-ons → `plan` is effectively the primary plan (same cards), with
    # provenance + is_balanced; current_week_plan present and matches.
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    prim_ids = [a["activity_id"] for d in g["plan"]["week"] for a in d["activities"]]
    cwp_ids = [a["activity_id"] for d in g["current_week_plan"]["week"] for a in d["activities"]]
    check("(no add-on) plan == current_week_plan", g["plan"]["week"] == g["current_week_plan"]["week"])
    check("(no add-on) plan is_balanced + provenance", g["plan"].get("is_balanced") is True
          and all(a.get("source") == "primary" and a.get("activity_id") for d in g["plan"]["week"] for a in d["activities"]))

    # Add an add-on → `plan` now includes BOTH primary + add-on cards.
    _add(sid, "language_and_communication")
    for ep in (f"/api/v1/session/{sid}", "/api/v1/session/current"):
        g = client.get(ep, headers=_hdr()).json()
        cards = [a for d in g["plan"]["week"] for a in d["activities"]]
        srcs = {a["source"] for a in cards}
        fks = {a["focus_key"] for a in cards}
        check(f"[{ep}] plan has primary + add-on cards", {"primary", "addon"} <= srcs, srcs)
        check(f"[{ep}] plan covers both focuses", {"cognitive", "language_and_communication"} <= fks, fks)
        check(f"[{ep}] plan == current_week_plan", g["plan"]["week"] == g["current_week_plan"]["week"])
        check(f"[{ep}] add-on cards route to module_id (source-based customization)",
              all(c["module_id"] and c["plan_id"] is None for c in cards if c["source"] == "addon"))
        check(f"[{ep}] primary cards route to plan_id",
              all(c["plan_id"] and c["module_id"] is None for c in cards if c["source"] == "primary"))


def run_all():
    test_no_addon_matches_primary()
    test_plan_shim_compat()
    test_one_addon_two_per_day()
    test_multi_addon_balanced()
    test_provenance()
    test_byte_stability()
    test_past_days_unchanged()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ current-week balanced tests FAILED")
        sys.exit(1)
    print("✅ All current-week balanced tests PASSED")


if __name__ == "__main__":
    run_all()
