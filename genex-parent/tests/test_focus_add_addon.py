"""
tests/test_focus_add_addon.py — Beta 2.2 Slice 2f-3: add activity to an add-on day

Add a bank activity to a specific day/date of a READY add-on module, LLM-free, using
the add-on's retained per-focus activity_bank. Added activity is stored in the add-on
overlay (customizations.added_activities), constrained to the add-on plan_period
(today→Sunday). Stored plan_response + the entire primary plan/customizations stay
byte-identical. Added cards keep add-on provenance. ACTIVITY_MODEL empty → bank-only.

In-process TestClient; Firebase mocked; local /tmp store.

Run: PYTHONPATH=. python3 tests/test_focus_add_addon.py
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


def _get_focus(sid, fk="cognitive"):
    return client.get(f"/api/v1/session/{sid}/focus/{fk}", headers=_hdr()).json()


def _cards(module):
    return [c for d in module["plan"]["week"] for c in d["activities"]]


def _suggestions(sid, fk="cognitive", token="token-user-a"):
    return client.get(f"/api/v1/session/{sid}/focus/{fk}/activity-suggestions", headers=_hdr(token))


def _add(sid, fk, body, token="token-user-a"):
    return client.post(f"/api/v1/session/{sid}/focus/{fk}/activities/add", headers=_hdr(token), json=body)


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


def _module_days(module):
    return {d["day"]: d["date"] for d in module["plan"]["week"]}


# ── 1. suggestions from retained bank ───────────────────────────────────────
def test_suggestions():
    print("\n── activity-suggestions from the retained add-on bank")
    sid = _start()
    _ready(sid)
    r = _suggestions(sid)
    check("→ 200", r.status_code == 200, r.text[:160])
    sugg = r.json()["suggestions"]
    check("at least one suggestion", len(sugg) >= 1, len(sugg))
    check("suggestion has id + title", all(s.get("suggestion_id") and "title" in s for s in sugg))


# ── 2,3,4. add to a specific day → appears in GET /focus with provenance ────
def test_add_to_day_with_provenance():
    print("\n── add to a specific day, appears in GET /focus with provenance")
    sid = _start()
    m = _ready(sid)
    days = _module_days(m)
    target_day = list(days.keys())[0]
    target_date = days[target_day]
    sg = _suggestions(sid).json()["suggestions"][0]
    n_before = len(_cards(m))
    r = _add(sid, "cognitive", {"suggestion_id": sg["suggestion_id"], "day": target_day})
    check("→ 200 added", r.status_code == 200 and r.json()["added"] is True, r.text[:160])
    b = r.json()
    check("response day matches", b["day"] == target_day, b)
    check("response activity_date matches", b["activity_date"] == target_date, (b.get("activity_date"), target_date))
    aid = b["activity_id"]
    after = _get_focus(sid)
    cards_after = _cards(after)
    check("one more activity", len(cards_after) == n_before + 1, (len(cards_after), n_before))
    added_card = next((c for c in cards_after if c["id"] == aid), None)
    check("added card present in GET /focus", added_card is not None)
    # under the correct day/date
    day_block = next(d for d in after["plan"]["week"] if d["day"] == target_day)
    check("added card under the correct day", any(c["id"] == aid for c in day_block["activities"]), [c["id"] for c in day_block["activities"]])
    check("added card title matches suggestion", added_card["title"] == sg["title"])
    # provenance
    check("provenance source addon", added_card.get("source") == "addon")
    check("provenance focus_key", added_card.get("focus_key") == "cognitive")
    check("provenance focus_label", added_card.get("focus_label") == "Learning, Attention & Thinking")
    check("provenance module_id", added_card.get("module_id") == m["module_id"])
    check("provenance activity_date", added_card.get("activity_date") == target_date)
    check("summary added_count 1", after["plan_customization_summary"]["added_count"] == 1, after["plan_customization_summary"])


# ── 5,6. stored plan_response byte-stable; overlay stores added_activities ──
def test_plan_response_stable_overlay_stores():
    print("\n── stored plan_response byte-stable; added in overlay")
    sid = _start()
    m = _ready(sid)
    pr_before = copy.deepcopy(session_store.load("uid-a", sid)["added_focus"]["cognitive"]["plan_response"])
    day = list(_module_days(m).keys())[0]
    sg = _suggestions(sid).json()["suggestions"][0]
    _add(sid, "cognitive", {"suggestion_id": sg["suggestion_id"], "day": day})
    e = session_store.load("uid-a", sid)["added_focus"]["cognitive"]
    check("stored plan_response byte-identical", e["plan_response"] == pr_before)
    aa = e["customizations"]["added_activities"]
    check("added_activities has 1 entry", len(aa) == 1, len(aa))
    check("entry has activity/internal/day/date", all(k in aa[0] for k in ("activity", "internal", "day", "date")))


# ── 7,8. idempotent same day; different day separate ───────────────────────
def test_idempotent_and_multi_day():
    print("\n── same suggestion+day idempotent; different day separate")
    sid = _start()
    m = _ready(sid)
    days = list(_module_days(m).keys())
    sg = _suggestions(sid).json()["suggestions"][0]["suggestion_id"]
    r1 = _add(sid, "cognitive", {"suggestion_id": sg, "day": days[0]}).json()
    r2 = _add(sid, "cognitive", {"suggestion_id": sg, "day": days[0]}).json()
    check("same suggestion+day → same activity_id (idempotent)", r1["activity_id"] == r2["activity_id"])
    e = session_store.load("uid-a", sid)["added_focus"]["cognitive"]
    check("only one added entry after repeat", len(e["customizations"]["added_activities"]) == 1)
    if len(days) >= 2:
        r3 = _add(sid, "cognitive", {"suggestion_id": sg, "day": days[1]}).json()
        check("same suggestion, different day → different id", r3["activity_id"] != r1["activity_id"])
        e2 = session_store.load("uid-a", sid)["added_focus"]["cognitive"]
        check("two added entries across two days", len(e2["customizations"]["added_activities"]) == 2)
        after = _get_focus(sid)
        b0 = next(d for d in after["plan"]["week"] if d["day"] == days[0])
        b1 = next(d for d in after["plan"]["week"] if d["day"] == days[1])
        check("each added card on its own day",
              any(c["id"] == r1["activity_id"] for c in b0["activities"]) and
              any(c["id"] == r3["activity_id"] for c in b1["activities"]))


# ── 9. outside-date/day guard ───────────────────────────────────────────────
def test_outside_date_guard():
    print("\n── outside plan_period day/date is rejected")
    sid = _start()
    m = _ready(sid)
    sg = _suggestions(sid).json()["suggestions"][0]["suggestion_id"]
    # Monday is before today (today is Tue–Sun in this run's partial week) → not in range
    pp = m["plan"]["plan_period"]
    out_day = next((d for d in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
                    if d not in pp["days_included"]), None)
    if out_day:
        r = _add(sid, "cognitive", {"suggestion_id": sg, "day": out_day})
        check(f"out-of-range day '{out_day}' → 400 invalid_day",
              r.status_code == 400 and r.json()["detail"]["code"] == "invalid_day", r.text[:160])
    else:
        check("(full week — no out-of-range day to test; skipped)", True)
    # bogus date
    r2 = _add(sid, "cognitive", {"suggestion_id": sg, "activity_date": "1999-01-01"})
    check("out-of-range date → 400 invalid_date",
          r2.status_code == 400 and r2.json()["detail"]["code"] == "invalid_date", r2.text[:160])
    # add by valid activity_date works
    valid_date = list(_module_days(m).values())[0]
    r3 = _add(sid, "cognitive", {"suggestion_id": sg, "activity_date": valid_date})
    check("valid activity_date → 200", r3.status_code == 200 and r3.json()["activity_date"] == valid_date, r3.text[:160])


# ── 10. guards ──────────────────────────────────────────────────────────────
def test_guards():
    print("\n── unknown focus / not ready / unknown suggestion / stale module")
    sid = _start()
    client.post(f"/api/v1/session/{sid}/focus/cognitive/start", headers=_hdr())
    check("suggestions not ready → 409", _suggestions(sid).status_code == 409)
    check("add not ready → 409 focus_not_ready",
          _add(sid, "cognitive", {"suggestion_id": "x"}).json().get("detail") == "focus_not_ready")
    m = _ready(sid)
    sg = _suggestions(sid).json()["suggestions"][0]["suggestion_id"]
    check("unknown focus → 404 unknown_focus", _add(sid, "telepathy", {"suggestion_id": sg}).json().get("detail") == "unknown_focus")
    check("not started → 404 focus_not_started", _add(sid, "movement_and_physical", {"suggestion_id": sg}).json().get("detail") == "focus_not_started")
    check("bad suggestion → 404 suggestion_not_found", _add(sid, "cognitive", {"suggestion_id": "nope"}).json().get("detail") == "suggestion_not_found")
    rs = _add(sid, "cognitive", {"suggestion_id": sg, "module_id": "wrong"})
    check("stale module → 409 stale_module", rs.status_code == 409 and rs.json().get("detail") == "stale_module", rs.text[:140])
    check("correct module_id → 200", _add(sid, "cognitive", {"suggestion_id": sg, "module_id": m["module_id"]}).status_code == 200)
    # auth
    check("suggestions no token → 401", client.get(f"/api/v1/session/{sid}/focus/cognitive/activity-suggestions").status_code == 401)
    check("add no token → 401", client.post(f"/api/v1/session/{sid}/focus/cognitive/activities/add", json={"suggestion_id": sg}).status_code == 401)
    check("add wrong user → 403", _add(sid, "cognitive", {"suggestion_id": sg}, token="token-user-b").status_code == 403)


# ── 11. LLM-free ────────────────────────────────────────────────────────────
def test_no_llm():
    print("\n── add is bank-only (no LLM)")
    check("ACTIVITY_MODEL empty", os.environ.get("ACTIVITY_MODEL") == "")
    sid = _start()
    _ready(sid)
    sg = _suggestions(sid).json()["suggestions"][0]["suggestion_id"]
    check("add succeeds offline", _add(sid, "cognitive", {"suggestion_id": sg}).status_code == 200)


# ── 12. primary plan + customizations byte-stable ───────────────────────────
def test_primary_byte_stable():
    print("\n── primary plan + customizations untouched by add-on add")
    sid = _start()
    plan = _finish_primary_and_plan(sid)
    pid = session_store.load("uid-a", sid)["current_plan_id"]
    # primary add to populate plan_customizations
    psug = client.get(f"/api/v1/session/{sid}/plan/{pid}/activity-suggestions", headers=_hdr()).json()["suggestions"]
    if psug:
        client.post(f"/api/v1/session/{sid}/plan/{pid}/activities/add", headers=_hdr(), json={"suggestion_id": psug[0]["suggestion_id"]})
    doc_b = session_store.load("uid-a", sid)
    plans_before = copy.deepcopy(doc_b["plans"])
    pc_before = copy.deepcopy(doc_b["plan_customizations"])

    m = _ready(sid, "movement_and_physical")
    suggs = _suggestions(sid, "movement_and_physical").json()["suggestions"]
    if suggs:
        day = list(_module_days(m).keys())[0]
        _add(sid, "movement_and_physical", {"suggestion_id": suggs[0]["suggestion_id"], "day": day})
    else:
        # Full-week generation can consume the whole bank → no add suggestions. Dirty
        # the add-on overlay with a bank-free remove so the byte-stability check is
        # still meaningful regardless of the calendar/bank state.
        aid = _cards(m)[0]["id"]
        client.post(f"/api/v1/session/{sid}/focus/movement_and_physical/activity/{aid}/remove", headers=_hdr())

    doc_a = session_store.load("uid-a", sid)
    check("primary doc['plans'] byte-identical", doc_a["plans"] == plans_before)
    check("primary plan_customizations byte-identical", doc_a["plan_customizations"] == pc_before)
    check("current_plan_id unchanged", doc_a["current_plan_id"] == pid)


def run_all():
    test_suggestions()
    test_add_to_day_with_provenance()
    test_plan_response_stable_overlay_stores()
    test_idempotent_and_multi_day()
    test_outside_date_guard()
    test_guards()
    test_no_llm()
    test_primary_byte_stable()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ focus add tests FAILED")
        sys.exit(1)
    print("✅ All focus add tests PASSED")


if __name__ == "__main__":
    run_all()
