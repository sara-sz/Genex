"""
tests/test_focus_swap_addon.py — Beta 2.2 Slice 2f-2: swap add-on activity

Swap an activity inside a READY add-on module with a safe bank alternative, LLM-free,
using the add-on's retained per-focus activity_bank. Override is stored in the add-on
overlay (doc["added_focus"][focus_key]["customizations"].activity_overrides); the
stored plan_response and the entire primary plan/customizations stay byte-identical.
Swapped cards keep add-on provenance. ACTIVITY_MODEL empty → bank-only, no LLM.

In-process TestClient; Firebase mocked; local /tmp store.

Run: PYTHONPATH=. python3 tests/test_focus_swap_addon.py
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


def _swap_suggestions(sid, fk, aid, token="token-user-a"):
    return client.get(f"/api/v1/session/{sid}/focus/{fk}/activity/{aid}/swap-suggestions", headers=_hdr(token))


def _swap(sid, fk, aid, body, token="token-user-a"):
    return client.post(f"/api/v1/session/{sid}/focus/{fk}/activity/{aid}/swap", headers=_hdr(token), json=body)


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


# ── 1. ready add-on has swap suggestions from the retained bank ─────────────
def test_suggestions_from_bank():
    print("\n── swap-suggestions come from the retained add-on bank")
    sid = _start()
    m = _ready(sid)
    aid = _cards(m)[0]["id"]
    r = _swap_suggestions(sid, "cognitive", aid)
    check("→ 200", r.status_code == 200, r.text[:160])
    b = r.json()
    sugg = b["suggestions"]
    check("at least one suggestion", len(sugg) >= 1, len(sugg))
    check("suggestion has id + title", all(s.get("suggestion_id") and "title" in s for s in sugg))
    visible_titles = {c["title"].strip().lower() for c in _cards(m)}
    check("suggestions exclude currently-visible activities",
          all(s["title"].strip().lower() not in visible_titles for s in sugg), [s["title"] for s in sugg])
    # bank is actually present in the stored entry (Option A retention)
    bs = session_store.load("uid-a", sid)["added_focus"]["cognitive"]["brain_state"]
    check("activity_banks retained in entry", bool(bs.get("activity_banks")), list(bs))


# ── 2,3. swap replaces the activity + keeps provenance ──────────────────────
def test_swap_replaces_with_provenance():
    print("\n── swap replaces the card in GET /focus, with add-on provenance")
    sid = _start()
    m = _ready(sid)
    card0 = _cards(m)[0]
    aid = card0["id"]
    sugg = _swap_suggestions(sid, "cognitive", aid).json()["suggestions"]
    sid_choice = sugg[0]["suggestion_id"]
    r = _swap(sid, "cognitive", aid, {"suggestion_id": sid_choice})
    check("→ 200 swapped", r.status_code == 200 and r.json()["swapped"] is True, r.text[:160])
    repl_id = r.json()["replacement_activity_id"]
    check("replacement id is new", repl_id != aid, (repl_id, aid))
    after = _get_focus(sid)
    ids = [c["id"] for c in _cards(after)]
    check("original id replaced (gone)", aid not in ids, ids)
    check("replacement id present", repl_id in ids, ids)
    repl = next(c for c in _cards(after) if c["id"] == repl_id)
    check("replacement title matches suggestion", repl["title"] == sugg[0]["title"], (repl["title"], sugg[0]["title"]))
    check("provenance source addon", repl.get("source") == "addon", repl.get("source"))
    check("provenance focus_key", repl.get("focus_key") == "cognitive", repl.get("focus_key"))
    check("provenance focus_label", repl.get("focus_label") == "Learning, Attention & Thinking", repl.get("focus_label"))
    check("provenance module_id", repl.get("module_id") == m["module_id"], repl.get("module_id"))
    check("provenance activity_date preserved", repl.get("activity_date") == card0.get("activity_date"), (repl.get("activity_date"), card0.get("activity_date")))
    check("summary swapped_count 1", after["plan_customization_summary"]["swapped_count"] == 1, after["plan_customization_summary"])


# ── 4,5. stored plan_response byte-stable; override stored in overlay ───────
def test_plan_response_stable_overlay_stores():
    print("\n── stored plan_response byte-stable; override lives in overlay")
    sid = _start()
    m = _ready(sid)
    pr_before = copy.deepcopy(session_store.load("uid-a", sid)["added_focus"]["cognitive"]["plan_response"])
    aid = _cards(m)[0]["id"]
    sg = _swap_suggestions(sid, "cognitive", aid).json()["suggestions"][0]["suggestion_id"]
    _swap(sid, "cognitive", aid, {"suggestion_id": sg})
    e = session_store.load("uid-a", sid)["added_focus"]["cognitive"]
    check("stored plan_response byte-identical", e["plan_response"] == pr_before)
    ov = e["customizations"]["activity_overrides"]
    check("override stored under customizations.activity_overrides", aid in ov, list(ov))
    check("override mode swapped", ov[aid]["mode"] == "swapped")
    check("override has replacement_activity + internal", "replacement_activity" in ov[aid] and "replacement_internal" in ov[aid])


# ── 6,7. idempotent same swap; different swap replaces override ─────────────
def test_idempotent_and_replace():
    print("\n── same swap idempotent; different suggestion replaces override")
    sid = _start()
    m = _ready(sid)
    aid = _cards(m)[0]["id"]
    sugg = _swap_suggestions(sid, "cognitive", aid).json()["suggestions"]
    s0 = sugg[0]["suggestion_id"]
    r1 = _swap(sid, "cognitive", aid, {"suggestion_id": s0}).json()
    r2 = _swap(sid, "cognitive", aid, {"suggestion_id": s0}).json()
    check("same suggestion → same replacement id (idempotent)", r1["replacement_activity_id"] == r2["replacement_activity_id"])
    e = session_store.load("uid-a", sid)["added_focus"]["cognitive"]
    check("still exactly one override", len(e["customizations"]["activity_overrides"]) == 1)
    if len(sugg) >= 2:
        s1 = sugg[1]["suggestion_id"]
        r3 = _swap(sid, "cognitive", aid, {"suggestion_id": s1}).json()
        check("different suggestion → new replacement id", r3["replacement_activity_id"] != r1["replacement_activity_id"])
        e2 = session_store.load("uid-a", sid)["added_focus"]["cognitive"]
        check("override replaced (still one)", len(e2["customizations"]["activity_overrides"]) == 1)
        after = _get_focus(sid)
        ids = [c["id"] for c in _cards(after)]
        check("only the latest replacement is visible", r3["replacement_activity_id"] in ids and r1["replacement_activity_id"] not in ids, ids)


# ── 8. guards ───────────────────────────────────────────────────────────────
def test_guards():
    print("\n── guards")
    sid = _start()
    # not ready
    client.post(f"/api/v1/session/{sid}/focus/cognitive/start", headers=_hdr())
    check("suggestions not ready → 409 focus_not_ready",
          _swap_suggestions(sid, "cognitive", "x").status_code == 409)
    check("swap not ready → 409 focus_not_ready",
          _swap(sid, "cognitive", "x", {"suggestion_id": "y"}).json().get("detail") == "focus_not_ready")
    m = _ready(sid)
    aid = _cards(m)[0]["id"]
    sg = _swap_suggestions(sid, "cognitive", aid).json()["suggestions"][0]["suggestion_id"]
    # unknown focus
    check("unknown focus → 404 unknown_focus", _swap(sid, "telepathy", aid, {"suggestion_id": sg}).json().get("detail") == "unknown_focus")
    # not started focus
    check("not started → 404 focus_not_started", _swap(sid, "movement_and_physical", aid, {"suggestion_id": sg}).json().get("detail") == "focus_not_started")
    # unknown activity
    check("unknown activity → 404 activity_not_found", _swap(sid, "cognitive", "no-id", {"suggestion_id": sg}).json().get("detail") == "activity_not_found")
    # bad suggestion
    check("bad suggestion → 404 suggestion_not_found", _swap(sid, "cognitive", aid, {"suggestion_id": "nope"}).json().get("detail") == "suggestion_not_found")
    # stale module
    rs = _swap(sid, "cognitive", aid, {"suggestion_id": sg, "module_id": "wrong"})
    check("stale module → 409 stale_module", rs.status_code == 409 and rs.json().get("detail") == "stale_module", rs.text[:140])
    # correct module_id ok
    check("correct module_id → 200", _swap(sid, "cognitive", aid, {"suggestion_id": sg, "module_id": m["module_id"]}).status_code == 200)
    # auth
    check("suggestions no token → 401", client.get(f"/api/v1/session/{sid}/focus/cognitive/activity/{aid}/swap-suggestions").status_code == 401)
    check("swap no token → 401", client.post(f"/api/v1/session/{sid}/focus/cognitive/activity/{aid}/swap", json={"suggestion_id": sg}).status_code == 401)
    check("swap wrong user → 403", _swap(sid, "cognitive", aid, {"suggestion_id": sg}, token="token-user-b").status_code == 403)


# ── 9. LLM-free ─────────────────────────────────────────────────────────────
def test_no_llm():
    print("\n── swap is bank-only (no LLM)")
    check("ACTIVITY_MODEL empty in this run", os.environ.get("ACTIVITY_MODEL") == "")
    sid = _start()
    m = _ready(sid)
    aid = _cards(m)[0]["id"]
    sg = _swap_suggestions(sid, "cognitive", aid).json()["suggestions"][0]["suggestion_id"]
    check("swap succeeds offline (no OpenAI configured)", _swap(sid, "cognitive", aid, {"suggestion_id": sg}).status_code == 200)


# ── 10. primary plan + primary customizations byte-stable ───────────────────
def test_primary_byte_stable():
    print("\n── primary plan + customizations untouched by add-on swap")
    sid = _start()
    plan = _finish_primary_and_plan(sid)
    pid = session_store.load("uid-a", sid)["current_plan_id"]
    # apply a primary swap so plan_customizations is non-empty
    paid = next(c["id"] for d in plan["week"] for c in d["activities"])
    psug = client.get(f"/api/v1/session/{sid}/plan/{pid}/activity/{paid}/swap-suggestions", headers=_hdr()).json()["suggestions"]
    if psug:
        client.post(f"/api/v1/session/{sid}/plan/{pid}/activity/{paid}/swap", headers=_hdr(), json={"suggestion_id": psug[0]["suggestion_id"]})
    doc_b = session_store.load("uid-a", sid)
    plans_before = copy.deepcopy(doc_b["plans"])
    pc_before = copy.deepcopy(doc_b["plan_customizations"])

    # add-on swap
    m = _ready(sid, "movement_and_physical")
    aid = _cards(m)[0]["id"]
    sg = _swap_suggestions(sid, "movement_and_physical", aid).json()["suggestions"][0]["suggestion_id"]
    _swap(sid, "movement_and_physical", aid, {"suggestion_id": sg})

    doc_a = session_store.load("uid-a", sid)
    check("primary doc['plans'] byte-identical", doc_a["plans"] == plans_before)
    check("primary plan_customizations byte-identical", doc_a["plan_customizations"] == pc_before)
    check("current_plan_id unchanged", doc_a["current_plan_id"] == pid)


def run_all():
    test_suggestions_from_bank()
    test_swap_replaces_with_provenance()
    test_plan_response_stable_overlay_stores()
    test_idempotent_and_replace()
    test_guards()
    test_no_llm()
    test_primary_byte_stable()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ focus swap tests FAILED")
        sys.exit(1)
    print("✅ All focus swap tests PASSED")


if __name__ == "__main__":
    run_all()
