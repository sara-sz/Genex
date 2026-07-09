"""
tests/test_categories_milestones.py — Beta 2.3 Phase 2

`categories_in_practice`: milestone practice grouped by domain, sourced ONLY from
did_it completions (+ reliable current-plan milestones at 0). Non-did_it earns stars
but never fills a practice dot. No cups / no check-ins. practices capped at 5.

Unit tests over api.progress.compute_categories_in_practice + HTTP integration.
Run: PYTHONPATH=. python3 tests/test_categories_milestones.py
"""
import os, sys
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


def comp(mid, domain, date, short="Saying 50 Words", source="cdc", conf="high", idem=None, valid=True):
    return {"valid_completion": valid, "idempotency_key": idem or f"{mid}|{date}",
            "local_completion_date": date, "date_confidence": conf,
            "snapshot": {"domain": domain},
            "milestone": {"milestone_id": mid, "milestone_source": source, "short_label": short,
                          "canonical_age_months": 30, "cup_eligible": source == "cdc"}}


# ── unit tests over compute_categories_in_practice ──────────────────────────
def test_didit_counts_and_aggregation():
    print("\n── did_it completions count; same milestone aggregates; domains group")
    comps = [
        comp("mv1:cdc:language_and_communication:30:aaa", "language_and_communication", "2026-07-06", idem="k1"),
        comp("mv1:cdc:language_and_communication:30:aaa", "language_and_communication", "2026-07-08", idem="k2"),  # same milestone, diff activity/date
        comp("mv1:cdc:social_and_emotional:30:bbb", "social_and_emotional", "2026-07-08", short="Taking Turns", idem="k3"),
    ]
    cats = pg.compute_categories_in_practice(comps, [])
    check("2 domain groups", len(cats) == 2, [c["domain_key"] for c in cats])
    lang = next(c for c in cats if c["domain_key"] == "language_and_communication")
    check("same canonical milestone aggregated into 1 row", len(lang["milestones"]) == 1, lang["milestones"])
    check("aggregated practices == 2", lang["milestones"][0]["practices_completed"] == 2)
    check("distinct_practice_days == 2", lang["milestones"][0]["distinct_practice_days"] == 2)
    soc = next(c for c in cats if c["domain_key"] == "social_and_emotional")
    check("separate domain group with its milestone", soc["milestones"][0]["short_label"] == "Taking Turns")
    check("domain_label present (parent-facing)", lang["domain_label"] and soc["domain_label"])


def test_two_milestones_same_domain():
    print("\n── different milestones in same domain → one group, two rows")
    comps = [
        comp("mv1:cdc:language_and_communication:30:aaa", "language_and_communication", "2026-07-08", short="Saying 50 Words"),
        comp("mv1:cdc:language_and_communication:24:ccc", "language_and_communication", "2026-07-08", short="Points to pictures"),
    ]
    cats = pg.compute_categories_in_practice(comps, [])
    check("1 domain group", len(cats) == 1)
    check("2 milestone rows", len(cats[0]["milestones"]) == 2, cats[0]["milestones"])


def test_cap_at_5_true_preserved():
    print("\n── practices cap at 5 for UI; true count preserved")
    comps = [comp("mv1:cdc:cognitive:30:zzz", "cognitive", f"2026-07-{d:02d}", idem=f"k{d}") for d in range(1, 8)]  # 7
    m = pg.compute_categories_in_practice(comps, [])[0]["milestones"][0]
    check("practices_completed capped at 5", m["practices_completed"] == 5, m["practices_completed"])
    check("practices_completed_true == 7", m["practices_completed_true"] == 7, m["practices_completed_true"])
    check("practices_target == 5", m["practices_target"] == 5)
    check("practice_ready True at >=5 (no check-in)", m["practice_ready"] is True and m["check_in_ready"] is False)


def test_unreliable_excluded():
    print("\n── missing id / bridge source / no domain → excluded (no fake rows)")
    comps = [
        comp(None, "cognitive", "2026-07-08"),                                  # no milestone_id
        comp("mv1:bridge:cognitive:30:ddd", "cognitive", "2026-07-08", source="bridge"),  # bridge, not cdc
        comp("mv1:cdc:cognitive:30:eee", "", "2026-07-08"),                     # no domain
    ]
    cats = pg.compute_categories_in_practice(comps, [])
    check("no category rows fabricated from unreliable data", cats == [], cats)


def test_active_plan_zero_practice():
    print("\n── reliable current-plan milestone with 0 completions appears at 0")
    active = [{"milestone_id": "mv1:cdc:social_and_emotional:30:bbb", "milestone_source": "cdc",
               "short_label": "Taking Turns", "domain": "social_and_emotional", "canonical_age_months": 30,
               "cup_eligible": True}]
    cats = pg.compute_categories_in_practice([], active)
    check("appears as a 0-practice row", len(cats) == 1 and cats[0]["milestones"][0]["practices_completed"] == 0, cats)
    # unreliable active milestone (no id) is NOT seeded
    cats2 = pg.compute_categories_in_practice([], [{"milestone_id": None, "domain": "cognitive"}])
    check("unreliable active milestone not seeded", cats2 == [])


def test_low_confidence_days():
    print("\n── low-confidence completion counts practice but not distinct days")
    comps = [comp("mv1:cdc:cognitive:30:zzz", "cognitive", "2026-07-08", conf="low", idem="k1")]
    m = pg.compute_categories_in_practice(comps, [])[0]["milestones"][0]
    check("practice counted", m["practices_completed"] == 1)
    check("distinct_practice_days excludes low confidence", m["distinct_practice_days"] == 0, m["distinct_practice_days"])


# ── HTTP integration ────────────────────────────────────────────────────────
def _bootstrap(concern="not walking, motor delay", age=18):
    sid = client.post("/api/v1/session/start", headers=H, json={"child_name": "R", "age_years": age // 12,
        "age_months": age % 12, "age_in_months": age, "diagnosis_or_condition": "Other",
        "parent_concern": concern, "daily_time_minutes": 10, "timezone": "America/Los_Angeles",
        "beta_access_code": "genex"}).json()["session_id"]
    q = client.get(f"/api/v1/session/{sid}", headers=H).json().get("current_question")
    while q:
        a = client.post(f"/api/v1/session/{sid}/answer", headers=H, json={"question_id": q["question_id"], "answer": "with_help"}).json()
        if a.get("status") == "interview_complete": break
        q = a.get("current_question")
    client.post(f"/api/v1/session/{sid}/plan", headers=H)
    plan = client.get("/api/v1/session/current", headers=H).json()["plan"]
    return sid, plan
def _fb(sid, card, day, comp_status="did_it"):
    return client.post(f"/api/v1/session/{sid}/feedback", headers=H, json={"plan_id": card["plan_id"],
        "activity_id": card["activity_id"], "day": day, "activity_date": card["activity_date"],
        "enjoyment": "loved_it", "difficulty": "just_right", "completion": comp_status}).json()
def _prog(sid): return client.get(f"/api/v1/session/{sid}/progress", headers=H).json()


def test_http_didit_vs_non_didit():
    print("\n── HTTP: did_it fills a dot; not_ready/didnt_want earn star but no dot")
    sid, plan = _bootstrap()
    cards = [(d["day"], a) for d in plan["week"] for a in d["activities"] if a["source"] == "primary"]
    (d1, c1), (d2, c2) = cards[0], cards[1]
    _fb(sid, c1, d1, "did_it")
    _fb(sid, c2, d2, "wasnt_ready_yet")
    p = _prog(sid)
    check("stars from attempts == 2 (both earned effort stars)", p["stars"]["all_time"] == 2, p["stars"])
    total_practices = sum(m["practices_completed"] for c in p["categories_in_practice"] for m in c["milestones"])
    check("exactly 1 milestone practice from the did_it (not_ready adds 0)", total_practices == 1, total_practices)
    # duplicate did_it → no double count
    _fb(sid, c1, d1, "did_it")
    p2 = _prog(sid)
    check("duplicate did_it does not double-count practice",
          sum(m["practices_completed"] for c in p2["categories_in_practice"] for m in c["milestones"]) == 1)


def test_http_phase1_fields_intact_no_cups():
    print("\n── HTTP: Phase 1 fields intact; no cups/check-ins created")
    sid, plan = _bootstrap()
    card = next(a for d in plan["week"] for a in d["activities"])
    day = next(d["day"] for d in plan["week"] for a in d["activities"] if a["activity_id"] == card["activity_id"])
    _fb(sid, card, day, "did_it")
    p = _prog(sid)
    for k in ("progress_schema_version", "session_id", "timezone", "week", "stars", "categories_in_practice",
              "badges", "checkins_ready", "cups_by_domain"):
        check(f"field present: {k}", k in p)
    check("weekly circle uses attempts (today practiced)", any(d["is_today"] and d["status"] == "practiced" for d in p["week"]))
    check("NO cups", p["cups_by_domain"] == [])
    check("NO check-ins ready", p["checkins_ready"] == [])
    check("no check_in_ready True anywhere", not any(m["check_in_ready"] for c in p["categories_in_practice"] for m in c["milestones"]))
    # no cup/checkin records were written to the doc
    d = session_store.load("uid-a", sid)
    check("no cups/checkins persisted", not d.get("cups") and not d.get("checkins"))
    check("observable_text resolved for at least one milestone",
          any(m.get("observable_text") for c in p["categories_in_practice"] for m in c["milestones"]))


def run_all():
    test_didit_counts_and_aggregation()
    test_two_milestones_same_domain()
    test_cap_at_5_true_preserved()
    test_unreliable_excluded()
    test_active_plan_zero_practice()
    test_low_confidence_days()
    test_http_didit_vs_non_didit()
    test_http_phase1_fields_intact_no_cups()
    print(f"\n{'='*50}\nResults: {_p} passed, {_f} failed")
    if _f: print("❌ categories/milestones FAILED"); sys.exit(1)
    print("✅ All categories/milestones tests PASSED")

if __name__ == "__main__":
    run_all()
