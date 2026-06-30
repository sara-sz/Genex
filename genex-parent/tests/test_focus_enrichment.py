"""
tests/test_focus_enrichment.py — Beta 2.2: on-track add-on enrichment

Genex Brain rule: when a parent explicitly adds a focus area, Genex provides activities
even if the child is on track. On-track (dev_age >= chronological) → age-appropriate
PRACTICE activities from the retained bank (API-layer enrichment_focus, frozen
scheduler reused — no genex_core change, no new LLM). Gap case unchanged. Provenance,
plan_period, and daily budget preserved. ACTIVITY_MODEL empty.

Run: PYTHONPATH=. python3 tests/test_focus_enrichment.py
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


def _hdr():
    return {"Authorization": "Bearer token-user-a"}


def _start(daily=10):
    # Bob, ADHD/attention → primary = cognitive; we add language_and_communication.
    return client.post("/api/v1/session/start", headers=_hdr(), json={
        "child_name": "Bob", "age_years": 4, "age_months": 0, "age_in_months": 48,
        "diagnosis_or_condition": "ADHD", "parent_concern": "ADHD, lack of attention",
        "daily_time_minutes": daily, "timezone": "UTC", "beta_access_code": "genex"}).json()["session_id"]


def _addon(sid, fk, answer):
    st = client.post(f"/api/v1/session/{sid}/focus/{fk}/start", headers=_hdr()).json()
    q = st.get("current_question")
    while q:
        r = client.post(f"/api/v1/session/{sid}/focus/{fk}/answer", headers=_hdr(),
                        json={"question_id": q["question_id"], "answer": answer}).json()
        if r.get("status") == "interview_complete":
            break
        q = r.get("current_question")
    client.post(f"/api/v1/session/{sid}/focus/{fk}/generate", headers=_hdr())
    return client.get(f"/api/v1/session/{sid}/focus/{fk}", headers=_hdr()).json()


def _cards(m):
    return [c for d in m["plan"]["week"] for c in d["activities"]]


# ── 1. on-track add-on → ready with age-appropriate enrichment activities ───
def test_on_track_enrichment():
    print("\n── on-track add-on (Bob/Speech all-yes) → enrichment activities")
    sid = _start(daily=10)
    fk = "language_and_communication"
    m = _addon(sid, fk, "yes")
    cards = _cards(m)
    check("status ready", m["status"] == "ready", m.get("status"))
    check("activity_count > 0 (NOT empty)", len(cards) > 0, len(cards))
    by_day = {d["day"]: len(d["activities"]) for d in m["plan"]["week"]}
    check("about 2/day for 10 min", all(n == 2 for n in by_day.values()) and by_day, by_day)
    check("all activities are Speech & Communication", all(c["domain"] == fk for c in cards), {c["domain"] for c in cards})
    check("on_track: true", m.get("on_track") is True, m.get("on_track"))
    check("enrichment_mode: true", m.get("enrichment_mode") is True, m.get("enrichment_mode"))
    check("message present", "looks on track" in (m.get("message") or ""), m.get("message"))
    # plan_period current week today→Sunday
    pp = m["plan"]["plan_period"]
    check("plan_period today→Sunday", pp.get("plan_start_date") and pp.get("plan_end_date"), pp)
    check("every card has activity_date + day-mapped", all(c.get("activity_date") for c in cards))
    check("provenance: source/focus_key/focus_label/module_id",
          all(c.get("source") == "addon" and c.get("focus_key") == fk and c.get("focus_label")
              and c.get("module_id") == m["module_id"] for c in cards), cards[0] if cards else None)
    check("dev_age >= chronological (on-track)",
          m["dev_age_summary"]["dev_age_months"] >= m["dev_age_summary"]["chronological_months"], m["dev_age_summary"])


# ── 2. gap add-on → unchanged (bridge/support; no enrichment flags) ─────────
def test_gap_unchanged():
    print("\n── gap add-on (with_help) → unchanged bridge/support activities")
    sid = _start(daily=10)
    fk = "language_and_communication"
    m = _addon(sid, fk, "with_help")
    cards = _cards(m)
    check("ready with activities", m["status"] == "ready" and len(cards) > 0, len(cards))
    check("on_track: false", m.get("on_track") is False, m.get("on_track"))
    check("enrichment_mode: false", m.get("enrichment_mode") is False, m.get("enrichment_mode"))
    check("no enrichment message", not m.get("message"), m.get("message"))
    check("dev_age < chronological (gap)",
          m["dev_age_summary"]["dev_age_months"] < m["dev_age_summary"]["chronological_months"], m["dev_age_summary"])


# ── 3. daily budget stable: on-track per-day == gap per-day ─────────────────
def test_daily_budget_stable():
    print("\n── enrichment daily count matches the gap-case budget")
    fk = "language_and_communication"
    on = _addon(_start(daily=10), fk, "yes")
    gap = _addon(_start(daily=10), fk, "with_help")
    on_max = max(len(d["activities"]) for d in on["plan"]["week"])
    gap_max = max(len(d["activities"]) for d in gap["plan"]["week"])
    check("on-track max/day == gap max/day", on_max == gap_max == 2, (on_max, gap_max))


# ── 4. integrated next-week includes the on-track add-on focus ──────────────
def test_integrated_includes_ontrack_addon():
    print("\n── integrated next-week includes an on-track add-on focus")
    sid = _start(daily=10)
    # primary (cognitive) with a gap so the primary plan has activities
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    q = g.get("current_question")
    while q:
        a = client.post(f"/api/v1/session/{sid}/answer", headers=_hdr(),
                        json={"question_id": q["question_id"], "answer": "with_help"}).json()
        if a.get("status") == "interview_complete":
            break
        q = a.get("current_question")
    plan = client.post(f"/api/v1/session/{sid}/plan", headers=_hdr()).json()
    # on-track language add-on (all yes → enrichment)
    _addon(sid, "language_and_communication", "yes")
    # advance to integrated next week
    doc = session_store.load("uid-a", sid)
    doc["plans"][plan["plan_period"]["plan_id"]]["plan_period"]["plan_end_date"] = _PAST_SUNDAY
    session_store.save("uid-a", sid, doc)
    w2 = client.post(f"/api/v1/session/{sid}/plan/next-week", headers=_hdr()).json()
    afa = set(w2["plan_period"].get("active_focus_areas", []))
    doms = {c["domain"] for d in w2["week"] for c in d["activities"]}
    check("language in active_focus_areas (on-track add-on included)", "language_and_communication" in afa, afa)
    check("language activities present in integrated week", "language_and_communication" in doms, doms)
    check("primary cognitive also present", "cognitive" in doms, doms)
    WD = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    wd = [d for d in w2["week"] if d["day"] in WD]
    base = max(len(d["activities"]) for d in plan["week"])
    check("integrated week stays within daily budget (no inflation)", all(len(d["activities"]) <= base for d in wd), [(d["day"], len(d["activities"])) for d in wd])


# ── 5. enrichment is bank-only (no LLM) ─────────────────────────────────────
def test_enrichment_no_llm():
    print("\n── enrichment uses the retained bank (no LLM)")
    check("ACTIVITY_MODEL empty", os.environ.get("ACTIVITY_MODEL") == "")
    m = _addon(_start(daily=10), "language_and_communication", "yes")
    check("on-track module generated offline with activities", m["status"] == "ready" and len(_cards(m)) > 0)


def run_all():
    test_on_track_enrichment()
    test_gap_unchanged()
    test_daily_budget_stable()
    test_integrated_includes_ontrack_addon()
    test_enrichment_no_llm()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ focus enrichment tests FAILED")
        sys.exit(1)
    print("✅ All focus enrichment tests PASSED")


if __name__ == "__main__":
    run_all()
