"""
tests/test_focus_generate.py — Beta 2.2 Slice 2c: date-aware add-on generation

Generate a single-focus, current-week (today→Sunday) add-on module from a completed
focus intake. Module is stored/labeled separately under doc["added_focus"][focus_key];
the primary plan + doc["plans"] stay byte-identical. Cards carry additive provenance.
No future-week integration, no Lovable UI. ACTIVITY_MODEL empty → offline generation.

In-process TestClient; Firebase mocked; local /tmp store.

Run: PYTHONPATH=. python3 tests/test_focus_generate.py
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
from api.planning_period import compute_plan_period, WEEK_DAY_NAMES  # noqa: E402

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


def _start(concern="speech delay and trouble talking", token="token-user-a"):
    r = client.post("/api/v1/session/start", headers=_hdr(token), json={
        "child_name": "C", "age_years": 3, "age_months": 0, "age_in_months": 36,
        "diagnosis_or_condition": "No known diagnosis / not sure", "parent_concern": concern,
        "daily_time_minutes": 20, "timezone": "UTC", "beta_access_code": "genex"})
    return r.json()["session_id"]


def _start_focus(sid, fk):
    return client.post(f"/api/v1/session/{sid}/focus/{fk}/start", headers=_hdr())


def _answer_focus(sid, fk, qid, ans="yes"):
    return client.post(f"/api/v1/session/{sid}/focus/{fk}/answer", headers=_hdr(),
                       json={"question_id": qid, "answer": ans})


def _generate(sid, fk, token="token-user-a"):
    return client.post(f"/api/v1/session/{sid}/focus/{fk}/generate", headers=_hdr(token))


def _get_focus(sid, fk, token="token-user-a"):
    return client.get(f"/api/v1/session/{sid}/focus/{fk}", headers=_hdr(token))


def _complete_focus_intake(sid, fk):
    st = _start_focus(sid, fk).json()
    q = st.get("current_question")
    while q:
        r = _answer_focus(sid, fk, q["question_id"]).json()
        if r.get("status") == "interview_complete":
            break
        q = r.get("current_question")


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


# ── 1. cannot generate before intake complete ───────────────────────────────
def test_generate_requires_complete_intake():
    print("\n── cannot generate before intake complete")
    sid = _start()
    _start_focus(sid, "cognitive")  # interviewing, not complete
    r = _generate(sid, "cognitive")
    check("→ 409", r.status_code == 409, r.text[:160])
    check("detail focus_intake_not_complete", r.json().get("detail") == "focus_intake_not_complete", r.json())
    # never started → 404
    r2 = _generate(sid, "social_and_emotional")
    check("not started → 404 focus_not_started", r2.status_code == 404 and r2.json().get("detail") == "focus_not_started", r2.text[:120])
    # primary → 409 focus_is_primary
    r3 = _generate(sid, "language_and_communication")
    check("primary → 409 focus_is_primary", r3.status_code == 409 and r3.json().get("detail") == "focus_is_primary", r3.text[:120])


# ── 2,3,5. completed intake generates single-domain labeled module ──────────
def test_generate_module():
    print("\n── completed intake → ready single-domain labeled module")
    sid = _start()
    _complete_focus_intake(sid, "cognitive")
    r = _generate(sid, "cognitive")
    check("→ 200", r.status_code == 200, r.text[:200])
    m = r.json()
    check("status ready", m["status"] == "ready", m.get("status"))
    check("source addon", m["source"] == "addon", m.get("source"))
    check("focus_label friendly", m["focus_label"] == "Learning, Attention & Thinking", m.get("focus_label"))
    check("module_id present", bool(m.get("module_id")))
    check("plan present with week", "plan" in m and "week" in m["plan"], list(m.get("plan", {}))[:6])
    check("dev_age_summary present", m.get("dev_age_summary", {}).get("focus_key") == "cognitive", m.get("dev_age_summary"))
    # single-domain: every card's domain is cognitive
    domains = {c["domain"] for d in m["plan"]["week"] for c in d["activities"]}
    check("module is single-domain (cognitive only)", domains <= {"cognitive"}, domains)
    check("has at least one activity", sum(len(d["activities"]) for d in m["plan"]["week"]) > 0)


# ── 4. date-aware: today → Sunday only, no past days ────────────────────────
def test_date_aware_current_week():
    print("\n── module is date-aware (today → Sunday only)")
    sid = _start()
    _complete_focus_intake(sid, "cognitive")
    m = _generate(sid, "cognitive").json()
    pp = m["plan"]["plan_period"]
    expected = compute_plan_period("UTC")  # same tz the session used
    today_idx = WEEK_DAY_NAMES.index(expected["days_included"][0])
    check("plan_start_date == today", pp["plan_start_date"] == expected["plan_start_date"], (pp["plan_start_date"], expected["plan_start_date"]))
    check("days_included == today→Sunday", pp["days_included"] == WEEK_DAY_NAMES[today_idx:], pp["days_included"])
    check("plan_end_date is Sunday of this week", pp["plan_end_date"] == expected["plan_end_date"], pp["plan_end_date"])
    week_days = [d["day"] for d in m["plan"]["week"]]
    check("every shown day is in today→Sunday", all(d in pp["days_included"] for d in week_days), week_days)
    check("no past day shown", all(WEEK_DAY_NAMES.index(d) >= today_idx for d in week_days), week_days)


# ── 6. provenance fields on every add-on card ───────────────────────────────
def test_provenance_fields():
    print("\n── every add-on card carries provenance")
    sid = _start()
    _complete_focus_intake(sid, "cognitive")
    m = _generate(sid, "cognitive").json()
    mid = m["module_id"]
    pp_id = m["plan"]["plan_period"]["plan_id"]
    cards = [c for d in m["plan"]["week"] for c in d["activities"]]
    check("module has cards", len(cards) > 0, len(cards))
    ok = all(
        c.get("source") == "addon" and c.get("focus_key") == "cognitive"
        and c.get("focus_label") == "Learning, Attention & Thinking"
        and c.get("module_id") == mid and c.get("plan_period_id") == pp_id
        and c.get("week_start_date") and c.get("activity_date")
        and c.get("domain") == "cognitive" and c.get("domain_label")
        for c in cards
    )
    check("all cards: source/focus_key/focus_label/module_id/plan_period_id/dates/domain", ok,
          cards[0] if cards else None)


# ── 7. primary plan + doc["plans"] byte-identical before/after generation ───
def test_primary_plan_byte_stable():
    print("\n── primary plan + doc['plans'] + current_plan_id byte-stable")
    sid = _start()
    _finish_primary_and_plan(sid)
    doc_b = session_store.load("uid-a", sid)
    plans_before = copy.deepcopy(doc_b["plans"])
    pid = doc_b["current_plan_id"]
    pr_before = copy.deepcopy(doc_b["plans"][pid]["plan_response"])
    pi_before = copy.deepcopy(doc_b["plans"][pid].get("plan_internal"))

    _complete_focus_intake(sid, "social_and_emotional")
    gen = _generate(sid, "social_and_emotional")
    check("add-on generated after primary plan", gen.status_code == 200 and gen.json()["status"] == "ready", gen.text[:160])

    doc_a = session_store.load("uid-a", sid)
    check("doc['plans'] byte-identical", doc_a["plans"] == plans_before)
    check("primary plan_response byte-identical", doc_a["plans"][pid]["plan_response"] == pr_before)
    check("primary plan_internal byte-identical", doc_a["plans"][pid].get("plan_internal") == pi_before)
    check("current_plan_id unchanged", doc_a["current_plan_id"] == pid, doc_a["current_plan_id"])
    # add-on module is NOT in doc["plans"]
    check("add-on not written to doc['plans']", all(
        p.get("plan_response", {}).get("source") != "addon" for p in doc_a["plans"].values()))


# ── 8,13. GET /focus/{focus_key} reports each state + returns full module ────
def test_get_focus_states():
    print("\n── GET /focus/{focus_key} states + full module")
    sid = _start()
    # before start → 404
    check("before start → 404", _get_focus(sid, "cognitive").status_code == 404)
    _start_focus(sid, "cognitive")
    check("interviewing state", _get_focus(sid, "cognitive").json()["status"] == "interviewing")
    _complete_focus_intake(sid, "cognitive")
    g_ic = _get_focus(sid, "cognitive").json()
    check("interview_complete state", g_ic["status"] == "interview_complete" and g_ic["ready_for_generate"] is True, g_ic.get("status"))
    _generate(sid, "cognitive")
    g_ready = _get_focus(sid, "cognitive").json()
    check("ready state", g_ready["status"] == "ready", g_ready.get("status"))
    check("GET returns full module plan", "plan" in g_ready and "week" in g_ready["plan"], list(g_ready)[:8])
    check("GET ready has dev_age_summary + generated_at",
          bool(g_ready.get("dev_age_summary")) and bool(g_ready.get("generated_at")))
    check("unknown focus → 404 unknown_focus",
          _get_focus(sid, "telepathy").status_code == 404)


# ── 9. idempotent: ready returns cached module, no regeneration ─────────────
def test_generate_idempotent():
    print("\n── ready is idempotent (cached, no regeneration)")
    sid = _start()
    _complete_focus_intake(sid, "cognitive")
    m1 = _generate(sid, "cognitive").json()
    m2 = _generate(sid, "cognitive").json()
    check("same module_id", m1["module_id"] == m2["module_id"], (m1["module_id"], m2["module_id"]))
    check("same generated_at (not regenerated)", m1["generated_at"] == m2["generated_at"], (m1["generated_at"], m2["generated_at"]))
    check("plan identical across calls", m1["plan"] == m2["plan"])


# ── 10. generating status does not duplicate work ───────────────────────────
def test_generating_no_duplicate():
    print("\n── generating → 409 (no duplicate work)")
    sid = _start()
    _complete_focus_intake(sid, "cognitive")
    doc = session_store.load("uid-a", sid)
    doc["added_focus"]["cognitive"]["status"] = "generating"
    session_store.save("uid-a", sid, doc)
    r = _generate(sid, "cognitive")
    check("→ 409 focus_already_generating", r.status_code == 409 and r.json().get("detail") == "focus_already_generating", r.text[:160])
    # GET reports generating
    check("GET reports generating", _get_focus(sid, "cognitive").json()["status"] == "generating")


# ── 11. error state can retry ───────────────────────────────────────────────
def test_error_retry():
    print("\n── error state is retryable")
    sid = _start()
    _complete_focus_intake(sid, "cognitive")
    doc = session_store.load("uid-a", sid)
    doc["added_focus"]["cognitive"]["status"] = "error"
    doc["added_focus"]["cognitive"]["error"] = "boom"
    session_store.save("uid-a", sid, doc)
    check("GET error retryable flag", _get_focus(sid, "cognitive").json().get("ready_for_generate") is True)
    r = _generate(sid, "cognitive")
    check("retry generates → ready", r.status_code == 200 and r.json()["status"] == "ready", r.text[:160])
    doc2 = session_store.load("uid-a", sid)
    check("error cleared after success", "error" not in doc2["added_focus"]["cognitive"])


# ── 12,14. /session views: status only, not full module; remaining excludes ─
def test_session_views_no_bloat():
    print("\n── /session + /session/current carry status only (no full module)")
    sid = _start()
    _complete_focus_intake(sid, "cognitive")
    _generate(sid, "cognitive")
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    af = g["focus"]["added_focus_areas"]
    entry = next(x for x in af if x["focus_key"] == "cognitive")
    check("added_focus_areas shows ready status", entry["status"] == "ready", entry)
    check("added_focus_areas entry has no plan_response", "plan_response" not in entry and "plan" not in entry, entry)
    check("added_focus_areas entry has no brain_state", "brain_state" not in entry)
    rem = {x["key"] for x in g["focus"]["remaining_focus_areas"]}
    check("ready focus excluded from remaining", "cognitive" not in rem, rem)
    cur = client.get("/api/v1/session/current", headers=_hdr()).json()
    check("/session/current focus identical", cur["focus"] == g["focus"])
    # focus-areas listing also excludes ready
    fa = client.get(f"/api/v1/session/{sid}/focus-areas", headers=_hdr()).json()
    check("focus-areas: cognitive in added", any(a["focus_key"] == "cognitive" for a in fa["added"]))
    check("focus-areas: cognitive not in remaining", "cognitive" not in {x["focus_key"] for x in fa["remaining"]})


# ── 15. auth / ownership / no-token guards ──────────────────────────────────
def test_guards():
    print("\n── auth / ownership guards")
    sid = _start()
    check("generate no token → 401",
          client.post(f"/api/v1/session/{sid}/focus/cognitive/generate").status_code == 401)
    check("GET focus no token → 401",
          client.get(f"/api/v1/session/{sid}/focus/cognitive").status_code == 401)
    check("generate wrong user → 403", _generate(sid, "cognitive", token="token-user-b").status_code == 403)
    check("GET focus wrong user → 403", _get_focus(sid, "cognitive", token="token-user-b").status_code == 403)
    check("generate unknown session → 404",
          client.post("/api/v1/session/nope/focus/cognitive/generate", headers=_hdr()).status_code == 404)
    check("generate unknown focus → 404 unknown_focus", (lambda r: r.status_code == 404 and r.json().get("detail") == "unknown_focus")(_generate(sid, "telepathy")))


def run_all():
    test_generate_requires_complete_intake()
    test_generate_module()
    test_date_aware_current_week()
    test_provenance_fields()
    test_primary_plan_byte_stable()
    test_get_focus_states()
    test_generate_idempotent()
    test_generating_no_duplicate()
    test_error_retry()
    test_session_views_no_bloat()
    test_guards()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ focus generate tests FAILED")
        sys.exit(1)
    print("✅ All focus generate tests PASSED")


if __name__ == "__main__":
    run_all()
