"""
tests/test_completion_history.py — Beta 2.3 Phase 1

Durable completion history: snapshot + provenance (primary/add-on/original/swapped/
parent-added), immutable completion records (no forward-mutable refs), survival across
plan replacement, and re-earn on a later local date.

Run: PYTHONPATH=. python3 tests/test_completion_history.py
"""
import os, sys, copy
os.environ.update(FIREBASE_PROJECT_ID="genex-test", LOCAL_SESSION_FALLBACK="1",
    REQUIRE_BETA_CODE="true", BETA_ACCESS_CODE="genex", ALLOWED_ORIGINS="http://localhost:3000",
    ACTIVITY_MODEL="")
os.environ.pop("GCS_BUCKET", None); os.environ.pop("CONCERN_ROUTER_MODEL", None)
import shutil; shutil.rmtree("/tmp/genex_api_sessions", ignore_errors=True)
import firebase_admin; firebase_admin._apps["[DEFAULT]"] = object()
from firebase_admin import auth as fa
_T = {"tok": {"uid": "uid-a", "email": "a@x.com"}}
fa.verify_id_token = lambda t, *a, **k: _T[t] if t in _T else (_ for _ in ()).throw(fa.InvalidIdTokenError("bad"))
from fastapi.testclient import TestClient
from api.main import app
from api import session_store
client = TestClient(app); H = {"Authorization": "Bearer tok"}
_p = _f = 0
def check(l, ok, d=""):
    global _p, _f
    if ok: _p += 1; print(f"  ✓ {l}")
    else: _f += 1; print(f"  ✗ FAIL: {l} — {d}")
def P(p, j=None): return client.post(p, headers=H, json=j)
def G(p): return client.get(p, headers=H)
def doc(sid): return session_store.load("uid-a", sid)

# Immutable/forward-ref fields that must NOT appear on a completion record.
_FORBIDDEN = {"badge_event_id", "badge_event_ids", "checkin_event_id", "cup_event_id", "cup_id"}

def _start(concern="ADHD, lack of attention", diag="ADHD"):
    sid = P("/api/v1/session/start", {"child_name": "R", "age_years": 4, "age_months": 0,
        "age_in_months": 48, "diagnosis_or_condition": diag, "parent_concern": concern,
        "daily_time_minutes": 10, "timezone": "America/Los_Angeles", "beta_access_code": "genex"}).json()["session_id"]
    q = G(f"/api/v1/session/{sid}").json().get("current_question")
    while q:
        a = P(f"/api/v1/session/{sid}/answer", {"question_id": q["question_id"], "answer": "with_help"}).json()
        if a.get("status") == "interview_complete": break
        q = a.get("current_question")
    P(f"/api/v1/session/{sid}/plan")
    return sid
def _add_focus(sid, fk):
    q = P(f"/api/v1/session/{sid}/focus/{fk}/start").json().get("current_question")
    while q:
        r = P(f"/api/v1/session/{sid}/focus/{fk}/answer", {"question_id": q["question_id"], "answer": "with_help"}).json()
        if r.get("status") == "interview_complete": break
        q = r.get("current_question")
    P(f"/api/v1/session/{sid}/focus/{fk}/generate")
def _complete(sid, card, day):
    pid = card["plan_id"] if card["source"] == "primary" else card["module_id"]
    return P(f"/api/v1/session/{sid}/feedback", {"plan_id": pid, "activity_id": card["activity_id"],
        "day": day, "activity_date": card["activity_date"], "enjoyment": "loved_it",
        "difficulty": "just_right", "completion": "did_it"}).json()
def _find(plan, fk, src):
    for d in plan["week"]:
        for a in d["activities"]:
            if a.get("focus_key") == fk and a.get("source") == src:
                return d["day"], a
    return None, None


def test_snapshot_and_immutable_primary():
    print("\n── primary completion: snapshot + immutable record")
    sid = _start()
    plan = G("/api/v1/session/current").json()["plan"]
    card = next(a for d in plan["week"] for a in d["activities"])
    day = next(d["day"] for d in plan["week"] for a in d["activities"] if a["activity_id"] == card["activity_id"])
    _complete(sid, card, day)
    c = doc(sid)["completions"][0]
    check("snapshot has title+instructions+domain", bool(c["snapshot"]["title"]) and "domain" in c["snapshot"], c["snapshot"])
    check("provenance original / generated", c["provenance"] == "original" and c["generated_or_manual"] == "generated", c["provenance"])
    check("timing fields present", c["completed_at_utc"] and c["completion_tz"] == "America/Los_Angeles"
          and c["local_completion_date"] and c["week_start"] and c["week_end"])
    check("scheduled_date kept separately", c["scheduled_date"] == card["activity_date"])
    check("star_event_id linked", bool(c["star_event_id"]))
    check("schema_version + valid", c["schema_version"] == 1 and c["valid_completion"] is True)
    check("NO forward-mutable refs on completion", not (_FORBIDDEN & set(c.keys())), _FORBIDDEN & set(c.keys()))
    check("milestone block present (nullable)", "milestone" in c and "milestone_id" in c["milestone"])


def test_provenance_addon_swapped_added():
    print("\n── provenance: add-on, swapped, parent-added")
    sid = _start()
    fk = "language_and_communication"
    _add_focus(sid, fk)
    plan = G("/api/v1/session/current").json()["plan"]
    pid = plan["week"][0]["activities"][0]["plan_id"] or next(a["plan_id"] for d in plan["week"] for a in d["activities"] if a["source"]=="primary")
    # add-on original
    dA, ac = _find(plan, fk, "addon")
    _complete(sid, ac, dA)
    # swap a primary card, then complete the replacement
    dS, pc = _find(plan, "cognitive", "primary")
    ss = G(f"/api/v1/session/{sid}/plan/{pc['plan_id']}/activity/{pc['activity_id']}/swap-suggestions").json()["suggestions"]
    P(f"/api/v1/session/{sid}/plan/{pc['plan_id']}/activity/{pc['activity_id']}/swap", {"suggestion_id": ss[0]["suggestion_id"]})
    # add a parent activity
    dayAdd = G("/api/v1/session/current").json()["plan"]["week"][-1]["day"]
    sug = G(f"/api/v1/session/{sid}/plan/{pc['plan_id']}/activity-suggestions?domain=cognitive").json()["suggestions"]
    P(f"/api/v1/session/{sid}/plan/{pc['plan_id']}/activities/add", {"suggestion_id": sug[0]["suggestion_id"], "day": dayAdd})
    plan2 = G("/api/v1/session/current").json()["plan"]
    # complete the swapped replacement + the parent-added card
    dSw, sw = _find(plan2, "cognitive", "primary")  # first visible primary cognitive (likely the swapped one)
    _complete(sid, sw, dSw)
    # locate the parent-added card by its overlay id, then complete it on its day
    added_ids = {it["activity"]["id"] for it in doc(sid).get("plan_customizations", {}).get(pid, {}).get("added_activities", [])}
    added = None; added_day = None
    for dd in plan2["week"]:
        for a in dd["activities"]:
            if a["activity_id"] in added_ids:
                added, added_day = a, dd["day"]; break
        if added: break
    if added:
        _complete(sid, added, added_day)
    provs = {c["provenance"] for c in doc(sid)["completions"]}
    srcs = {c["source"] for c in doc(sid)["completions"]}
    check("add-on completion recorded (source addon)", "addon" in srcs, srcs)
    check("addon completion has module_id, no plan-only", any(c["module_id"] and c["source"]=="addon" for c in doc(sid)["completions"]))
    check("swapped provenance captured", "swapped" in provs, provs)
    check("swapped has source_activity_id", any(c["provenance"]=="swapped" and c["source_activity_id"] for c in doc(sid)["completions"]))
    check("parent_added provenance captured (if a card was added)", ("parent_added" in provs) or (added is None), provs)


def test_survives_plan_replacement():
    print("\n── completion history survives plan replacement / new week")
    sid = _start()
    plan = G("/api/v1/session/current").json()["plan"]
    card = next(a for d in plan["week"] for a in d["activities"])
    day = next(d["day"] for d in plan["week"] for a in d["activities"] if a["activity_id"] == card["activity_id"])
    _complete(sid, card, day)
    before = copy.deepcopy(doc(sid)["completions"])
    check("1 completion before replacement", len(before) == 1)
    # simulate a plan replacement: wipe plans/current_plan_id (history must persist)
    d = doc(sid)
    d["plans"] = {}; d["current_plan_id"] = None
    session_store.save("uid-a", sid, d)
    after = doc(sid)["completions"]
    check("completion history intact after plan wipe", after == before, (len(before), len(after)))
    check("title still readable from snapshot", after[0]["snapshot"]["title"] == before[0]["snapshot"]["title"])
    # /progress still reports the star from history (not the plan)
    prog = G(f"/api/v1/session/{sid}/progress").json()
    check("all-time star survives plan replacement", prog["stars"]["all_time"] == 1, prog["stars"])


def test_reearn_on_later_date():
    print("\n── same activity re-earns a star on a later local date")
    sid = _start()
    plan = G("/api/v1/session/current").json()["plan"]
    card = next(a for d in plan["week"] for a in d["activities"])
    day = next(d["day"] for d in plan["week"] for a in d["activities"] if a["activity_id"] == card["activity_id"])
    _complete(sid, card, day)
    # simulate a completion recorded on a different local date by editing the stored record's date key
    d = doc(sid)
    from api import progress as pg
    later = "2026-12-25"
    c2 = copy.deepcopy(d["completions"][0])
    c2["completion_id"] = "cmp_manual2"
    c2["local_completion_date"] = later
    c2["idempotency_key"] = pg.completion_idem_key(sid, card["activity_id"], later)
    d["completions"].append(c2)
    session_store.save("uid-a", sid, d)
    prog = G(f"/api/v1/session/{sid}/progress").json()
    check("two stars for same activity across two dates", prog["stars"]["all_time"] == 2, prog["stars"])


def run_all():
    test_snapshot_and_immutable_primary()
    test_provenance_addon_swapped_added()
    test_survives_plan_replacement()
    test_reearn_on_later_date()
    print(f"\n{'='*50}\nResults: {_p} passed, {_f} failed")
    if _f: print("❌ completion history FAILED"); sys.exit(1)
    print("✅ All completion history tests PASSED")

if __name__ == "__main__":
    run_all()
