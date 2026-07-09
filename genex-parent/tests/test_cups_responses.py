"""
tests/test_cups_responses.py — Beta 2.3 Phase 5: check-in responses + milestone cups

POST /milestone-checkins/{checkin_id}/response records the parent answer.
yes_usually → exactly one cup (parent-confirmed, never mastered/clinical) added to
cups_by_domain + latest_wins. sometimes/not_yet → no cup, milestone deferred until the
re-check rule (≥3 more did_it across ≥2 more distinct hi-conf dates). Idempotent; cups
never removed; prior responses never mutated. did_it-only; stars/badges unchanged.

Run: PYTHONPATH=. python3 tests/test_cups_responses.py
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
from api import progress as pg
client = TestClient(app); H = {"Authorization": "Bearer tok"}
_p = _f = 0
def check(l, ok, d=""):
    global _p, _f
    if ok: _p += 1; print(f"  ✓ {l}")
    else: _f += 1; print(f"  ✗ FAIL: {l} — {d}")

MID = "mv1:cdc:movement_and_physical:15:d5451f44be0b"
DOMAIN = "movement_and_physical"; SHORT = "takes a few steps on her own"

def comp(date, utc, source="cdc", conf="high", cup=True, short=SHORT, domain=DOMAIN, mid=MID, idem=None):
    return {"valid_completion": True, "completion_id": f"cmp_{idem}", "idempotency_key": idem,
            "local_completion_date": date, "date_confidence": conf, "completed_at_utc": utc,
            "snapshot": {"domain": domain},
            "milestone": {"milestone_id": mid, "milestone_source": source, "short_label": short,
                          "canonical_age_months": 15, "cup_eligible": cup}}

def _session_with_completions(comps):
    """Create a real session (for auth/plan), then inject completions directly."""
    sid = client.post("/api/v1/session/start", headers=H, json={"child_name": "Kashmir", "age_years": 1,
        "age_months": 3, "age_in_months": 15, "diagnosis_or_condition": "Other",
        "parent_concern": "not walking yet", "daily_time_minutes": 10,
        "timezone": "America/Los_Angeles", "beta_access_code": "genex"}).json()["session_id"]
    q = client.get(f"/api/v1/session/{sid}", headers=H).json().get("current_question")
    while q:
        a = client.post(f"/api/v1/session/{sid}/answer", headers=H, json={"question_id": q["question_id"], "answer": "with_help"}).json()
        if a.get("status") == "interview_complete": break
        q = a.get("current_question")
    client.post(f"/api/v1/session/{sid}/plan", headers=H)
    doc = session_store.load("uid-a", sid)
    doc["completions"] = comps
    doc["attempts"] = [{"local_date": c["local_completion_date"], "date_confidence": "high",
                        "idempotency_key": "att_" + c["idempotency_key"]} for c in comps]
    session_store.save("uid-a", sid, doc)
    return sid

def _five_across_three():   # ready
    return [comp("2026-07-06", "2026-07-06T10:00:00Z", idem="a"), comp("2026-07-06", "2026-07-06T11:00:00Z", idem="b"),
            comp("2026-07-07", "2026-07-07T10:00:00Z", idem="c"), comp("2026-07-07", "2026-07-07T11:00:00Z", idem="d"),
            comp("2026-07-08", "2026-07-08T10:00:00Z", idem="e")]
def _cid(sid): return pg.checkin_id_for(sid, MID)
def _resp(sid, cid, val): return client.post(f"/api/v1/session/{sid}/milestone-checkins/{cid}/response", headers=H, json={"response": val})
def _prog(sid): return client.get(f"/api/v1/session/{sid}/progress", headers=H).json()


def test_yes_awards_cup():
    print("\n── yes_usually → cup in cups_by_domain + latest_wins; response persisted")
    sid = _session_with_completions(_five_across_three())
    r = _resp(sid, _cid(sid), "yes_usually")
    check("200", r.status_code == 200, r.status_code)
    j = r.json()
    check("cup_awarded true + cup present", j["cup_awarded"] is True and j["cup"]["cup_id"], j)
    check("cup title has no 'mastered'/clinical", "mastered" not in j["cup"]["title"].lower())
    doc = session_store.load("uid-a", sid)
    cup = doc["cups"][0]
    check("cup persisted with snapshots", cup["prompt_snapshot"] and cup["parent_response"] == "yes_usually"
          and cup["practices_completed_at_confirmation"] == 5 and cup["distinct_practice_days_at_confirmation"] == 3, cup)
    check("cup source parent_confirmed", cup["source"] == "parent_confirmed")
    check("response persisted (append-only)", len(doc["checkin_responses"]) == 1 and doc["checkin_responses"][0]["response"] == "yes_usually")
    check("events: milestone_checkin_answered + cup_awarded", {"milestone_checkin_answered", "cup_awarded"} <= {e["type"] for e in doc["events"]})
    p = _prog(sid)
    check("cups_by_domain has the cup", p["cups_by_domain"] and p["cups_by_domain"][0]["total"] == 1
          and p["cups_by_domain"][0]["cups"][0]["cup_id"] == cup["cup_id"], p["cups_by_domain"])
    check("clean domain_label", p["cups_by_domain"][0]["domain_label"] == "Movement & Daily Skills")
    check("latest_wins includes the cup (type cup)", any(w["type"] == "cup" and w["cup_id"] == cup["cup_id"] for w in p["latest_wins"]), p["latest_wins"])
    check("cup win description = Parent-confirmed milestone", next(w for w in p["latest_wins"] if w["type"] == "cup")["description"] == "Parent-confirmed milestone")
    check("checkins_ready no longer lists the cupped milestone", all(c["milestone_id"] != MID for c in p["checkins_ready"]))
    cat = next(m for c in p["categories_in_practice"] for m in c["milestones"] if m["milestone_id"] == MID)
    check("category no longer check_in_ready (cupped) but practice_ready stays", cat["check_in_ready"] is False and cat["practice_ready"] is True, cat)


def test_duplicate_yes_no_second_cup():
    print("\n── duplicate yes_usually → no second cup (idempotent)")
    sid = _session_with_completions(_five_across_three())
    r1 = _resp(sid, _cid(sid), "yes_usually").json()
    r2 = _resp(sid, _cid(sid), "yes_usually").json()
    check("retry replay / cup_awarded true", r2["cup_awarded"] is True and r2.get("idempotent_replay") is True)
    check("same cup_id", r2["cup"]["cup_id"] == r1["cup"]["cup_id"])
    doc = session_store.load("uid-a", sid)
    check("still exactly 1 cup", len(doc["cups"]) == 1, len(doc["cups"]))
    check("still exactly 1 cup_awarded event", sum(1 for e in doc["events"] if e["type"] == "cup_awarded") == 1)


def test_sometimes_and_not_yet_no_cup():
    print("\n── sometimes/not_yet → persisted, no cup, check-in removed until re-check")
    for val, sid in (("sometimes_emerging", _session_with_completions(_five_across_three())),
                     ("not_yet", _session_with_completions(_five_across_three()))):
        j = _resp(sid, _cid(sid), val).json()
        check(f"{val}: cup_awarded false + supportive message", j["cup_awarded"] is False and j["supportive_message"], j)
        doc = session_store.load("uid-a", sid)
        check(f"{val}: response persisted, 0 cups", len(doc["checkin_responses"]) == 1 and not doc.get("cups"))
        p = _prog(sid)
        check(f"{val}: milestone removed from active checkins_ready", all(c["milestone_id"] != MID for c in p["checkins_ready"]), p["checkins_ready"])
        cat = next(m for c in p["categories_in_practice"] for m in c["milestones"] if m["milestone_id"] == MID)
        check(f"{val}: practice dots NOT reset", cat["practices_completed"] == 5)


def test_recheck_requires_more_practice():
    print("\n── re-check ready only after +3 did_it across +2 distinct dates")
    sid = _session_with_completions(_five_across_three())
    _resp(sid, _cid(sid), "not_yet")
    doc = session_store.load("uid-a", sid)
    # add 2 more did_it on 1 new day → NOT enough (need +3 completions AND +2 days)
    doc["completions"] += [comp("2026-07-09", "2026-07-09T10:00:00Z", idem="f"),
                           comp("2026-07-09", "2026-07-09T11:00:00Z", idem="g")]
    session_store.save("uid-a", sid, doc)
    check("not ready yet (only +2 completions / +1 day)", all(c["milestone_id"] != MID for c in _prog(sid)["checkins_ready"]))
    # add 1 more did_it on another new day → now +3 completions across +2 days
    doc = session_store.load("uid-a", sid)
    doc["completions"] += [comp("2026-07-10", "2026-07-10T10:00:00Z", idem="h")]
    session_store.save("uid-a", sid, doc)
    ready = _prog(sid)["checkins_ready"]
    check("re-check now ready", any(c["milestone_id"] == MID for c in ready), ready)
    # non-did_it never satisfies re-check (attempts don't create completions) — covered by completions-only source
    check("re-check uses did_it completions only", ready and ready[0]["practices_completed_true"] == 8)


def test_changed_response_history_preserved():
    print("\n── changed response preserved (append-only); cup on later yes")
    sid = _session_with_completions(_five_across_three())
    _resp(sid, _cid(sid), "sometimes_emerging")
    doc = session_store.load("uid-a", sid)
    # re-check qualify (+3 completions, +2 days), then answer yes
    doc["completions"] += [comp("2026-07-09", "2026-07-09T10:00:00Z", idem="f"),
                           comp("2026-07-10", "2026-07-10T10:00:00Z", idem="g"),
                           comp("2026-07-11", "2026-07-11T10:00:00Z", idem="h")]
    session_store.save("uid-a", sid, doc)
    j = _resp(sid, _cid(sid), "yes_usually").json()
    check("later yes awards the cup", j["cup_awarded"] is True)
    doc = session_store.load("uid-a", sid)
    check("both responses kept (history)", len(doc["checkin_responses"]) == 2
          and doc["checkin_responses"][0]["response"] == "sometimes_emerging"
          and doc["checkin_responses"][1]["response"] == "yes_usually")
    check("later response supersedes earlier", doc["checkin_responses"][1]["supersedes_response_id"] == doc["checkin_responses"][0]["response_id"])
    # a late not_yet must NOT remove the cup
    _resp(sid, _cid(sid), "not_yet")
    check("cup not removed by later not_yet", len(session_store.load("uid-a", sid)["cups"]) == 1)


def test_no_cup_for_unreliable_or_not_ready():
    print("\n── cup only for ready + reliable milestone")
    # unknown checkin_id → 404
    sid = _session_with_completions(_five_across_three())
    check("unknown checkin_id → 404", _resp(sid, "chk_nonexistent", "yes_usually").status_code == 404)
    # not-ready milestone (only 4 completions) → 409 on yes
    sid2 = _session_with_completions([comp(f"2026-07-0{d}", f"2026-07-0{d}T10:00:00Z", idem=f"k{d}") for d in (5, 6, 7, 8)])
    check("not-ready yes_usually → 409", _resp(sid2, _cid(sid2), "yes_usually").status_code == 409)
    # bridge milestone never becomes a check-in → 404
    bmid = "mv1:bridge:movement_and_physical:15:zzz"
    sid3 = _session_with_completions([comp("2026-07-0" + str(d), f"2026-07-0{d}T10:00:00Z", source="bridge", mid=bmid, idem=f"b{d}") for d in (5, 6, 7, 8, 9)])
    check("bridge milestone checkin → 404 (no readiness)", _resp(sid3, pg.checkin_id_for(sid3, bmid), "yes_usually").status_code == 404)


def test_stars_badges_practice_unaffected():
    print("\n── stars from attempts, badges from practice days, practice from did_it")
    sid = _session_with_completions(_five_across_three())
    _resp(sid, _cid(sid), "yes_usually")
    p = _prog(sid)
    check("stars from attempts", p["stars"]["all_time"] == 5)          # 5 attempts injected
    check("badges present (practice days)", any(b["badge_id"] == "first_step" for b in p["badges"]))
    total = sum(m["practices_completed_true"] for c in p["categories_in_practice"] for m in c["milestones"])
    check("milestone practice still from did_it (5)", total == 5, total)


def run_all():
    test_yes_awards_cup()
    test_duplicate_yes_no_second_cup()
    test_sometimes_and_not_yet_no_cup()
    test_recheck_requires_more_practice()
    test_changed_response_history_preserved()
    test_no_cup_for_unreliable_or_not_ready()
    test_stars_badges_practice_unaffected()
    print(f"\n{'='*50}\nResults: {_p} passed, {_f} failed")
    if _f: print("❌ cups/responses FAILED"); sys.exit(1)
    print("✅ All cups/responses tests PASSED")

if __name__ == "__main__":
    run_all()
