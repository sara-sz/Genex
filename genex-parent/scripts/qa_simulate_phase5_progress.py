#!/usr/bin/env python3
"""
scripts/qa_simulate_phase5_progress.py — Beta 2.3 Phase 5 QA simulation (DEV/STAGING ONLY)

Lets us + Lovable verify the full star/badge/check-in/cup flow WITHOUT waiting real
calendar days, by seeding deterministic progress state (explicit local dates). This is
a QA/testing tool — NOT a product endpoint. It never runs against Production:

  * refuses any target whose bucket name or API URL contains "prod"
  * seeded docs are clearly labelled  doc["qa_simulation"] = True  (ignored by product code)
  * --local uses the in-process app + local session store (no network, no bucket)
  * --seed-staging writes ONLY to the dev bucket via the authed gcloud CLI

Modes:
  python3 scripts/qa_simulate_phase5_progress.py --local
      Deterministic pass/fail run of every scenario in-process (CI-friendly).
  python3 scripts/qa_simulate_phase5_progress.py --seed-staging
      Creates real, loadable QA sessions in genex-api-dev-sessions-genex-mvp-2026
      (one disposable QA Firebase user per scenario) and prints session_id + login so
      Lovable can open each UI state. Requires FIREBASE_API_KEY + gcloud auth.

Scenarios: fresh, first_step, many_attempts_one_day, three_day_badge, checkin_ready,
yes_cup_awarded, sometimes_no_cup, not_yet_no_cup, duplicate_yes_no_duplicate_cup,
recheck_after_more_practice.
"""
import argparse
import json
import os
import subprocess
import sys
import uuid

# A real cup-eligible CDC movement milestone whose observable_text resolves.
MID = "mv1:cdc:movement_and_physical:15:d5451f44be0b"
DOMAIN = "movement_and_physical"
SHORT = "takes a few steps on her own"
DEV_BUCKET = "genex-api-dev-sessions-genex-mvp-2026"
STAGING_URL = "https://genex-api-staging-67icluiswq-uc.a.run.app"

SCENARIOS = ["fresh", "first_step", "many_attempts_one_day", "three_day_badge", "checkin_ready",
             "yes_cup_awarded", "sometimes_no_cup", "not_yet_no_cup",
             "duplicate_yes_no_duplicate_cup", "recheck_after_more_practice"]
SCENARIO_DESC = {
    "fresh": "No progress — empty Progress screen",
    "first_step": "One did_it → 1 star + First Step badge + 1 milestone practice dot",
    "many_attempts_one_day": "5 attempts same day → 5 stars, First Step only (no streak)",
    "three_day_badge": "3 consecutive practice days → First Step + 3-Day Rhythm badge",
    "checkin_ready": "5 did_it across 3 days → milestone check-in READY",
    "yes_cup_awarded": "Check-in answered Yes, usually → cup awarded",
    "sometimes_no_cup": "Check-in answered Sometimes → no cup, deferred",
    "not_yet_no_cup": "Check-in answered Not yet → no cup, deferred",
    "duplicate_yes_no_duplicate_cup": "Yes answered twice → exactly one cup",
    "recheck_after_more_practice": "After Not yet + more practice → check-in ready again",
}


def is_prod_target(bucket: str, api_url: str) -> bool:
    """True if the target looks like Production (used to hard-refuse)."""
    return "prod" in (bucket or "").lower() or "prod" in (api_url or "").lower()


def assert_not_prod(bucket: str, api_url: str) -> None:
    if is_prod_target(bucket, api_url):
        raise SystemExit(f"REFUSING: target looks like Production (bucket={bucket!r} url={api_url!r}). Dev/staging only.")


# ── Deterministic scenario data (minimal attempts/completions with explicit dates) ──
def _att(date, status="did_it", conf="high", i=0):
    return {"local_date": date, "date_confidence": conf, "status": status,
            "idempotency_key": f"att|{date}|{status}|{i}"}


def _comp(date, utc, i=0, source="cdc", cup=True):
    return {"valid_completion": True, "completion_id": f"cmp_{date}_{i}",
            "idempotency_key": f"cmp|{date}|{i}", "local_completion_date": date,
            "date_confidence": "high", "completed_at_utc": utc,
            "snapshot": {"domain": DOMAIN},
            "milestone": {"milestone_id": MID, "milestone_source": source, "short_label": SHORT,
                          "canonical_age_months": 15, "cup_eligible": cup}}


def _base_doc(session_id, owner_uid, scenario):
    return {"session_id": session_id, "owner_uid": owner_uid, "timezone": "America/Los_Angeles",
            "created_at": "2026-07-01T00:00:00Z", "status": "plan_ready", "current_plan_id": None,
            "plans": {}, "added_focus": {}, "qa_simulation": True, "qa_scenario": scenario,
            "attempts": [], "completions": [], "events": []}


def build_scenario_doc(session_id, owner_uid, scenario):
    """Return a fully-seeded session doc for a scenario (pre-response state)."""
    d = _base_doc(session_id, owner_uid, scenario)
    if scenario == "fresh":
        return d
    if scenario == "first_step":
        d["attempts"] = [_att("2026-07-06")]
        d["completions"] = [_comp("2026-07-06", "2026-07-06T18:00:00Z")]
        return d
    if scenario == "many_attempts_one_day":
        d["attempts"] = [_att("2026-07-06", "did_it", i=0), _att("2026-07-06", "wasnt_ready_yet", i=1),
                         _att("2026-07-06", "didnt_want_to_try", i=2), _att("2026-07-06", "did_it", i=3),
                         _att("2026-07-06", "wasnt_ready_yet", i=4)]
        d["completions"] = [_comp("2026-07-06", "2026-07-06T18:00:00Z", 0),
                            _comp("2026-07-06", "2026-07-06T19:00:00Z", 3)]
        return d
    if scenario == "three_day_badge":
        for i, dt in enumerate(["2026-07-06", "2026-07-07", "2026-07-08"]):
            d["attempts"].append(_att(dt, i=i))
            d["completions"].append(_comp(dt, f"{dt}T18:00:00Z", i))
        return d
    # checkin_ready and all response scenarios start from 5 did_it across 3 days
    dates = ["2026-07-06", "2026-07-06", "2026-07-07", "2026-07-07", "2026-07-08"]
    for i, dt in enumerate(dates):
        d["attempts"].append(_att(dt, i=i))
        d["completions"].append(_comp(dt, f"{dt}T1{i}:00:00Z", i))
    if scenario == "recheck_after_more_practice":
        # Represent the READY-AGAIN state directly (single deterministic write, no POST):
        # a Not yet was answered at 5/3, then +3 did_it across +2 more dates happened.
        d["completions"] += _extra_recheck_completions()
        d["checkin_responses"] = [{
            "schema_version": 1, "response_id": "resp_seed_notyet", "checkin_id": None,
            "milestone_id": MID, "owner_uid": owner_uid, "response": "not_yet",
            "response_label": "Not yet", "domain_key": DOMAIN, "short_label": SHORT,
            "practices_completed_at": 5, "distinct_days_at": 3,
            "created_at_utc": "2026-07-08T20:00:00Z", "local_date": "2026-07-08",
            "rule_version": "cr1", "supersedes_response_id": None,
        }]
        d["checkin_response_index"] = {"seed": "resp_seed_notyet"}
    return d


def _extra_recheck_completions():
    """+3 did_it across +2 new distinct dates (satisfies the re-check rule)."""
    return [_comp("2026-07-10", "2026-07-10T18:00:00Z", 90),
            _comp("2026-07-11", "2026-07-11T18:00:00Z", 91),
            _comp("2026-07-11", "2026-07-11T19:00:00Z", 92)]


# ── Expected-state assertions over a /progress payload ────────────────────────
def _badge_ids(p): return [b["badge_id"] for b in p["badges"]]
def _practice_total(p): return sum(m["practices_completed_true"] for c in p["categories_in_practice"] for m in c["milestones"])
def _cup_count(p): return sum(g["total"] for g in p["cups_by_domain"])
def _checkin_mids(p): return [c["milestone_id"] for c in p["checkins_ready"]]

EXPECT = {
    "fresh":        lambda p: (p["stars"]["all_time"] == 0 and _badge_ids(p) == [] and _checkin_mids(p) == [] and _cup_count(p) == 0, f"stars={p['stars']['all_time']} badges={_badge_ids(p)} cups={_cup_count(p)}"),
    "first_step":   lambda p: (p["stars"]["all_time"] == 1 and "first_step" in _badge_ids(p) and _practice_total(p) == 1 and _checkin_mids(p) == [], f"stars={p['stars']['all_time']} badges={_badge_ids(p)} practice={_practice_total(p)}"),
    "many_attempts_one_day": lambda p: (p["stars"]["all_time"] == 5 and _badge_ids(p) == ["first_step"] and _practice_total(p) == 2, f"stars={p['stars']['all_time']} badges={_badge_ids(p)} practice={_practice_total(p)}"),
    "three_day_badge": lambda p: ("three_day_rhythm" in _badge_ids(p) and "first_step" in _badge_ids(p), f"badges={_badge_ids(p)}"),
    "checkin_ready": lambda p: (MID in _checkin_mids(p) and _cup_count(p) == 0, f"checkins={_checkin_mids(p)} cups={_cup_count(p)}"),
    "yes_cup_awarded": lambda p: (_cup_count(p) == 1 and MID not in _checkin_mids(p) and any(w["type"] == "cup" for w in p["latest_wins"]), f"cups={_cup_count(p)} checkins={_checkin_mids(p)} wins={[w['type'] for w in p['latest_wins']]}"),
    "sometimes_no_cup": lambda p: (_cup_count(p) == 0 and MID not in _checkin_mids(p) and _practice_total(p) == 5, f"cups={_cup_count(p)} checkins={_checkin_mids(p)} practice={_practice_total(p)}"),
    "not_yet_no_cup": lambda p: (_cup_count(p) == 0 and MID not in _checkin_mids(p) and _practice_total(p) == 5, f"cups={_cup_count(p)} checkins={_checkin_mids(p)}"),
    "duplicate_yes_no_duplicate_cup": lambda p: (_cup_count(p) == 1, f"cups={_cup_count(p)}"),
    "recheck_after_more_practice": lambda p: (MID in _checkin_mids(p) and _cup_count(p) == 0, f"checkins={_checkin_mids(p)} cups={_cup_count(p)}"),
}


# ── Runners ───────────────────────────────────────────────────────────────────
def run_local():
    """Deterministic in-process run (TestClient + local session store)."""
    os.environ.update(FIREBASE_PROJECT_ID="genex-test", LOCAL_SESSION_FALLBACK="1", REQUIRE_BETA_CODE="true",
                      BETA_ACCESS_CODE="genex", ALLOWED_ORIGINS="http://localhost:3000", ACTIVITY_MODEL="")
    os.environ.pop("GCS_BUCKET", None)
    assert_not_prod(os.environ.get("GCS_BUCKET", ""), "")  # GCS_BUCKET unset → not prod
    import shutil
    shutil.rmtree("/tmp/genex_api_sessions", ignore_errors=True)
    import firebase_admin
    firebase_admin._apps["[DEFAULT]"] = object()
    from firebase_admin import auth as fa
    fa.verify_id_token = lambda t, *a, **k: {"uid": "qa-uid", "email": "qa@x.com"}
    from fastapi.testclient import TestClient
    from api.main import app
    from api import session_store, progress as pg
    client = TestClient(app)
    H = {"Authorization": "Bearer qa"}
    passed = failed = 0
    for sc in SCENARIOS:
        sid = f"qa-{sc}-{uuid.uuid4().hex[:6]}"
        session_store._cache.clear()
        session_store.save("qa-uid", sid, build_scenario_doc(sid, "qa-uid", sc))
        cid = pg.checkin_id_for(sid, MID)
        # scenario actions that hit the real response endpoint (recheck is pre-seeded)
        if sc in ("yes_cup_awarded", "sometimes_no_cup", "not_yet_no_cup", "duplicate_yes_no_duplicate_cup"):
            val = {"yes_cup_awarded": "yes_usually", "sometimes_no_cup": "sometimes_emerging",
                   "not_yet_no_cup": "not_yet", "duplicate_yes_no_duplicate_cup": "yes_usually"}[sc]
            client.post(f"/api/v1/session/{sid}/milestone-checkins/{cid}/response", headers=H, json={"response": val})
            if sc == "duplicate_yes_no_duplicate_cup":
                client.post(f"/api/v1/session/{sid}/milestone-checkins/{cid}/response", headers=H, json={"response": "yes_usually"})
        prog = client.get(f"/api/v1/session/{sid}/progress", headers=H).json()
        ok, detail = EXPECT[sc](prog)
        passed += ok
        failed += (not ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {sc:32s} session={sid}  endpoint=GET /api/v1/session/{sid}/progress")
        print(f"        expected: {SCENARIO_DESC[sc]}")
        print(f"        actual:   {detail}")
    print(f"\nLOCAL SIMULATION: {passed} passed, {failed} failed")
    return failed == 0


def _http(url, data=None, tok=None, method=None):
    import urllib.error
    import urllib.request
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    m = method or ("POST" if data is not None else "GET")
    req = urllib.request.Request(url, data=(json.dumps(data).encode() if data is not None else (b"" if m == "POST" else None)), headers=h, method=m)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


def run_seed_staging():
    """Create loadable QA sessions in the DEV bucket (one disposable QA user per scenario)."""
    key = os.environ.get("FIREBASE_API_KEY", "").strip()
    if not key:
        raise SystemExit("FIREBASE_API_KEY required for --seed-staging")
    assert_not_prod(DEV_BUCKET, STAGING_URL)
    QA_PW = "QaPhase5!" + uuid.uuid4().hex[:8]
    seeded = []
    for sc in SCENARIOS:
        em = f"qa-phase5-{sc}-{uuid.uuid4().hex[:6]}@genex-test.dev"
        d = _http(f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={key}",
                  {"email": em, "password": QA_PW, "returnSecureToken": True})[1]
        tok, uid = d["idToken"], d["localId"]
        sid = str(uuid.uuid4())
        doc = build_scenario_doc(sid, uid, sc)
        blob = f"gs://{DEV_BUCKET}/sessions/{uid}/{sid}.json"
        open("/tmp/qa_seed.json", "w").write(json.dumps(doc, indent=2))
        subprocess.check_call(["gcloud", "storage", "cp", "/tmp/qa_seed.json", blob],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cid = "chk_" + __import__("hashlib").sha256(f"chk1|{sid}|{MID}".encode()).hexdigest()[:16]
        if sc in ("yes_cup_awarded", "sometimes_no_cup", "not_yet_no_cup", "duplicate_yes_no_duplicate_cup"):
            val = {"yes_cup_awarded": "yes_usually", "sometimes_no_cup": "sometimes_emerging",
                   "not_yet_no_cup": "not_yet", "duplicate_yes_no_duplicate_cup": "yes_usually"}[sc]
            _http(f"{STAGING_URL}/api/v1/session/{sid}/milestone-checkins/{cid}/response", {"response": val}, tok)
            if sc == "duplicate_yes_no_duplicate_cup":
                _http(f"{STAGING_URL}/api/v1/session/{sid}/milestone-checkins/{cid}/response", {"response": "yes_usually"}, tok)
        prog = _http(f"{STAGING_URL}/api/v1/session/{sid}/progress", tok=tok)[1]
        ok = EXPECT[sc](prog)[0] if prog.get("progress_schema_version") else False
        seeded.append({"scenario": sc, "session_id": sid, "login_email": em, "password": QA_PW,
                       "represents": SCENARIO_DESC[sc], "verified": ok})
        print(f"[{'OK ' if ok else 'ERR'}] {sc:32s} session={sid} login={em}")
    print("\n=== SEEDED QA SESSIONS (dev bucket only) ===")
    print(json.dumps(seeded, indent=2))
    return seeded


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--local", action="store_true", help="deterministic in-process run")
    g.add_argument("--seed-staging", action="store_true", help="seed loadable QA sessions into the dev bucket")
    args = ap.parse_args()
    if args.local:
        sys.exit(0 if run_local() else 1)
    else:
        run_seed_staging()


if __name__ == "__main__":
    main()
