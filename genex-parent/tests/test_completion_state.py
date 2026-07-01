"""
tests/test_completion_state.py — Beta 2.2: additive read-only completion exposure

Mark-as-Done persists in doc["feedback"]. This slice ADDITIVELY surfaces the
per-activity completion in the session view so the frontend can restore exact done
cards + Progress from the backend (source of truth) after refresh/sign-in:
  - every visible card in `plan`/`current_week_plan` gets `completed: true/false`
    (+ feedback_id/completed_at/enjoyment/difficulty when done),
  - feedback_summary.completed_activities lists the done activities with routing
    provenance (source, plan_id|module_id, focus_key, title, …).

Works for all six card types (primary + add-on × original/swapped/added). Response
-only: stored plans, add-on modules, and feedback are never mutated. No genex_core.

Run: PYTHONPATH=. python3 tests/test_completion_state.py
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


def P(p, j=None):
    return client.post(p, headers=_hdr(), json=j).json()


def G(p):
    return client.get(p, headers=_hdr()).json()


def _start(concern="ADHD, lack of attention, trouble paying attention"):
    sid = P("/api/v1/session/start", {
        "child_name": "Robin", "age_years": 1, "age_months": 10, "age_in_months": 22,
        "diagnosis_or_condition": "ADHD", "parent_concern": concern,
        "daily_time_minutes": 10, "timezone": "UTC", "beta_access_code": "genex"})["session_id"]
    q = G(f"/api/v1/session/{sid}").get("current_question")
    while q:
        a = P(f"/api/v1/session/{sid}/answer", {"question_id": q["question_id"], "answer": "with_help"})
        if a.get("status") == "interview_complete":
            break
        q = a.get("current_question")
    P(f"/api/v1/session/{sid}/plan")
    return sid


def _add_focus(sid, fk):
    q = P(f"/api/v1/session/{sid}/focus/{fk}/start").get("current_question")
    while q:
        r = P(f"/api/v1/session/{sid}/focus/{fk}/answer", {"question_id": q["question_id"], "answer": "with_help"})
        if r.get("status") == "interview_complete":
            break
        q = r.get("current_question")
    P(f"/api/v1/session/{sid}/focus/{fk}/generate")


def _cur(sid):
    return G("/api/v1/session/current")["plan"]


def _visible(plan, src):
    return [(d["day"], a) for d in plan["week"] for a in d["activities"] if a["source"] == src]


def _classify(sid, pid, fk):
    """Return {card_id: type_label} using stored base/overlay ids."""
    doc = session_store.load("uid-a", sid)
    pbase = {x["id"] for d in doc["plans"][pid]["plan_response"]["week"] for x in d["activities"]}
    pov = doc.get("plan_customizations", {}).get(pid, {})
    padd = {i["activity"]["id"] for i in pov.get("added_activities", [])}
    pswap = {(o or {}).get("replacement_activity", {}).get("id") for o in pov.get("activity_overrides", {}).values()}
    az = doc["added_focus"][fk].get("customizations", {}) or {}
    abase = {x["id"] for d in doc["added_focus"][fk]["plan_response"]["week"] for x in d["activities"]}
    aadd = {i["activity"]["id"] for i in az.get("added_activities", [])}
    aswap = {(o or {}).get("replacement_activity", {}).get("id") for o in az.get("activity_overrides", {}).values()}

    def kind(aid, src):
        if src == "primary":
            return "primary added" if aid in padd else ("primary swapped" if aid in pswap else ("primary original" if aid in pbase else "primary ?"))
        return "addon added" if aid in aadd else ("addon swapped" if aid in aswap else ("addon original" if aid in abase else "addon ?"))
    return kind


def _mark_done(sid, card, day):
    plan_id = card["plan_id"] if card["source"] == "primary" else card["module_id"]
    return P(f"/api/v1/session/{sid}/feedback", {
        "plan_id": plan_id, "activity_id": card["activity_id"], "day": day,
        "activity_date": card["activity_date"], "enjoyment": "loved_it",
        "difficulty": "just_right", "completion": "did_it"})


def test_completion_all_six_types():
    print("\n── completion exposure for all 6 card types (primary + add-on)")
    sid = _start()
    g = G(f"/api/v1/session/{sid}")
    pid = g["current_plan_id"]
    pfk = g["focus"]["primary_focus_key"]
    fk = "language_and_communication"
    _add_focus(sid, fk)

    # Create swapped + added cards for primary and add-on.
    plan = _cur(sid)
    pv = _visible(plan, "primary")
    day_s, c_swapP = pv[0]
    ss = G(f"/api/v1/session/{sid}/plan/{pid}/activity/{c_swapP['activity_id']}/swap-suggestions")["suggestions"]
    P(f"/api/v1/session/{sid}/plan/{pid}/activity/{c_swapP['activity_id']}/swap", {"suggestion_id": ss[0]["suggestion_id"]})
    av = _visible(_cur(sid), "addon")
    day_a, c_swapA = av[0]
    ssm = G(f"/api/v1/session/{sid}/focus/{fk}/activity/{c_swapA['activity_id']}/swap-suggestions")["suggestions"]
    P(f"/api/v1/session/{sid}/focus/{fk}/activity/{c_swapA['activity_id']}/swap", {"suggestion_id": ssm[0]["suggestion_id"]})
    dayAdd = _cur(sid)["week"][-1]["day"]
    sug = G(f"/api/v1/session/{sid}/plan/{pid}/activity-suggestions?domain={pfk}")["suggestions"]
    P(f"/api/v1/session/{sid}/plan/{pid}/activities/add", {"suggestion_id": sug[0]["suggestion_id"], "day": dayAdd})
    sugm = G(f"/api/v1/session/{sid}/focus/{fk}/activity-suggestions")["suggestions"]
    P(f"/api/v1/session/{sid}/focus/{fk}/activities/add", {"suggestion_id": sugm[0]["suggestion_id"], "day": dayAdd})

    # Stable display; classify visible cards and pick one of each of the 6 types.
    kind = _classify(sid, pid, fk)
    plan = _cur(sid)
    targets = {}
    for d in plan["week"]:
        for a in d["activities"]:
            k = kind(a["activity_id"], a["source"])
            targets.setdefault(k, (d["day"], a))
    want = ["primary original", "primary swapped", "primary added",
            "addon original", "addon swapped", "addon added"]
    for k in want:
        check(f"visible card present: {k}", k in targets, list(targets.keys()))

    # Mark each done.
    done_ids = {}
    for k in want:
        if k not in targets:
            continue
        day, card = targets[k]
        r = _mark_done(sid, card, day)
        done_ids[k] = card["activity_id"]
        check(f"mark-done 200/ok: {k}", r.get("ok") is True and r.get("metadata_found") is True, r)

    # Re-read: every marked card shows completed:true; completed_activities has all six.
    v = G("/api/v1/session/current")
    plan_cards = {a["activity_id"]: a for d in v["plan"]["week"] for a in d["activities"]}
    cwp_cards = {a["activity_id"]: a for d in v["current_week_plan"]["week"] for a in d["activities"]}
    for k, aid in done_ids.items():
        c = plan_cards.get(aid)
        check(f"[plan] {k} card completed:true", c is not None and c.get("completed") is True, c)
        check(f"[plan] {k} card has feedback metadata", c is not None and c.get("feedback_id") and c.get("completed_at"))
        cc = cwp_cards.get(aid)
        check(f"[current_week_plan] {k} card completed:true", cc is not None and cc.get("completed") is True)

    ca = v["feedback_summary"].get("completed_activities") or []
    ca_ids = {x["activity_id"] for x in ca}
    for k, aid in done_ids.items():
        check(f"completed_activities includes {k}", aid in ca_ids)
    # provenance in completed_activities
    for x in ca:
        route = (x.get("plan_id") and not x.get("module_id")) if x["source"] == "primary" else (x.get("module_id") and not x.get("plan_id"))
        check(f"completed_activity provenance ({x['source']})",
              route and x.get("focus_key") and x.get("activity_date") and x.get("completed_at"), x)

    # aggregate completed count unchanged in meaning (>= 6 did_it)
    check("feedback_summary.completed >= 6", v["feedback_summary"]["completed"] >= 6, v["feedback_summary"]["completed"])
    # non-completed cards are explicitly completed:false
    check("non-done cards marked completed:false",
          any(a.get("completed") is False for d in v["plan"]["week"] for a in d["activities"]))


def test_completion_persists_and_byte_stable():
    print("\n── completion survives fresh reads; stored data byte-stable")
    sid = _start()
    fk = "language_and_communication"
    _add_focus(sid, fk)
    g = G(f"/api/v1/session/{sid}")
    pid = g["current_plan_id"]
    before = copy.deepcopy(session_store.load("uid-a", sid))
    plan = _cur(sid)
    pc = _visible(plan, "primary")[0]
    ac = _visible(plan, "addon")[0]
    _mark_done(sid, pc[1], pc[0])
    _mark_done(sid, ac[1], ac[0])
    # multiple fresh reads
    ids = [pc[1]["activity_id"], ac[1]["activity_id"]]
    for _ in range(3):
        v = G("/api/v1/session/current")
        cards = {a["activity_id"]: a for d in v["plan"]["week"] for a in d["activities"]}
        check("both stay completed:true across reads", all(cards.get(i, {}).get("completed") is True for i in ids))
        check("completed_activities stays populated", len(v["feedback_summary"].get("completed_activities") or []) >= 2)
    after = session_store.load("uid-a", sid)
    check("stored primary plan_response byte-identical", after["plans"][pid]["plan_response"] == before["plans"][pid]["plan_response"])
    check("stored add-on plan_response byte-identical", after["added_focus"][fk]["plan_response"] == before["added_focus"][fk]["plan_response"])
    # feedback list persisted (source of truth)
    check("stored feedback has the 2 did_it records", sum(1 for f in after.get("feedback", []) if f.get("completion") == "did_it") >= 2)


def test_no_feedback_all_false():
    print("\n── no feedback → cards completed:false, empty completed_activities")
    sid = _start()
    v = G("/api/v1/session/current")
    check("all cards completed:false", all(a.get("completed") is False for d in v["plan"]["week"] for a in d["activities"]))
    check("completed_activities empty", v["feedback_summary"].get("completed_activities") == [])
    check("aggregate completed still 0", v["feedback_summary"]["completed"] == 0)


def run_all():
    test_no_feedback_all_false()
    test_completion_all_six_types()
    test_completion_persists_and_byte_stable()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ completion state tests FAILED")
        sys.exit(1)
    print("✅ All completion state tests PASSED")


if __name__ == "__main__":
    run_all()
