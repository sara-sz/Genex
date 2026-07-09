"""
tests/test_qa_simulation.py — Beta 2.3 Phase 5 QA simulation add-on (DEV/STAGING ONLY)

Confirms the QA simulation tool is safe: refuses Production targets, only ever writes
to the dev bucket, produces the expected /progress state per scenario, awards exactly
one cup for yes_usually (none for sometimes/not_yet, no dup for repeated yes), and the
QA label never affects product behavior.

Run: PYTHONPATH=. python3 tests/test_qa_simulation.py
"""
import os, sys
os.environ.update(FIREBASE_PROJECT_ID="genex-test", LOCAL_SESSION_FALLBACK="1",
    REQUIRE_BETA_CODE="true", BETA_ACCESS_CODE="genex", ALLOWED_ORIGINS="http://localhost:3000",
    ACTIVITY_MODEL="")
os.environ.pop("GCS_BUCKET", None); os.environ.pop("CONCERN_ROUTER_MODEL", None)
import shutil; shutil.rmtree("/tmp/genex_api_sessions", ignore_errors=True)
import firebase_admin; firebase_admin._apps["[DEFAULT]"] = object()
from firebase_admin import auth as fa
_T = {"tok": {"uid": "qa-uid", "email": "qa@x.com"}}
fa.verify_id_token = lambda t, *a, **k: _T[t] if t in _T else (_ for _ in ()).throw(fa.InvalidIdTokenError("bad"))
from fastapi.testclient import TestClient
from api.main import app
from api import session_store, progress as pg
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import qa_simulate_phase5_progress as qa
client = TestClient(app); H = {"Authorization": "Bearer tok"}
_p = _f = 0
def check(l, ok, d=""):
    global _p, _f
    if ok: _p += 1; print(f"  ✓ {l}")
    else: _f += 1; print(f"  ✗ FAIL: {l} — {d}")


def test_refuses_prod():
    print("\n── simulation refuses Production targets")
    check("prod bucket → is_prod_target True", qa.is_prod_target("genex-api-prod-sessions-genex-mvp-2026", "") is True)
    check("prod url → is_prod_target True", qa.is_prod_target("", "https://genex-api-prod-1003012205867.us-central1.run.app") is True)
    check("dev bucket → is_prod_target False", qa.is_prod_target(qa.DEV_BUCKET, qa.STAGING_URL) is False)
    raised = False
    try:
        qa.assert_not_prod("genex-api-prod-sessions-genex-mvp-2026", "")
    except SystemExit:
        raised = True
    check("assert_not_prod raises on prod bucket", raised)
    # the staging-seed target is the dev bucket only
    check("seed target is the dev bucket", qa.DEV_BUCKET == "genex-api-dev-sessions-genex-mvp-2026")
    check("staging url is not prod", "prod" not in qa.STAGING_URL)


def _seed(sc):
    sid = f"qa-{sc}-x"
    session_store._cache.clear()
    session_store.save("qa-uid", sid, qa.build_scenario_doc(sid, "qa-uid", sc))
    return sid
def _prog(sid): return client.get(f"/api/v1/session/{sid}/progress", headers=H).json()
def _respond(sid, val):
    cid = pg.checkin_id_for(sid, qa.MID)
    return client.post(f"/api/v1/session/{sid}/milestone-checkins/{cid}/response", headers=H, json={"response": val})


def test_qa_label_and_no_product_impact():
    print("\n── seeded docs are QA-labelled and don't alter product behavior")
    sid = _seed("first_step")
    doc = session_store.load("qa-uid", sid)
    check("doc labelled qa_simulation + qa_scenario", doc.get("qa_simulation") is True and doc.get("qa_scenario") == "first_step")
    p = _prog(sid)
    check("progress ignores QA label (normal shape)", "qa_simulation" not in p and p["progress_schema_version"] == 1)


def test_scenarios_match_expected():
    print("\n── each scenario yields the expected /progress")
    for sc in ("fresh", "first_step", "many_attempts_one_day", "three_day_badge", "checkin_ready"):
        sid = _seed(sc)
        ok, detail = qa.EXPECT[sc](_prog(sid))
        check(f"scenario {sc}", ok, detail)


def test_yes_one_cup():
    print("\n── yes_usually scenario creates exactly one cup")
    sid = _seed("checkin_ready")
    _respond(sid, "yes_usually")
    p = _prog(sid)
    check("exactly one cup", sum(g["total"] for g in p["cups_by_domain"]) == 1)
    check("cup in latest_wins", any(w["type"] == "cup" for w in p["latest_wins"]))
    check("milestone removed from checkins_ready", qa.MID not in [c["milestone_id"] for c in p["checkins_ready"]])


def test_sometimes_notyet_no_cup():
    print("\n── sometimes / not_yet create no cup")
    for val in ("sometimes_emerging", "not_yet"):
        sid = _seed("checkin_ready")
        _respond(sid, val)
        p = _prog(sid)
        check(f"{val}: no cup", sum(g["total"] for g in p["cups_by_domain"]) == 0)
        check(f"{val}: milestone deferred (not in checkins_ready)", qa.MID not in [c["milestone_id"] for c in p["checkins_ready"]])
        check(f"{val}: practice dots not reset", sum(m["practices_completed_true"] for c in p["categories_in_practice"] for m in c["milestones"]) == 5)


def test_duplicate_yes_no_dup_cup():
    print("\n── duplicate yes creates no duplicate cup")
    sid = _seed("checkin_ready")
    _respond(sid, "yes_usually"); _respond(sid, "yes_usually")
    check("still one cup", sum(g["total"] for g in _prog(sid)["cups_by_domain"]) == 1)


def test_recheck_requires_more_practice():
    print("\n── re-check only after additional did_it practice")
    sid = _seed("checkin_ready")
    _respond(sid, "not_yet")
    check("deferred right after not_yet", qa.MID not in [c["milestone_id"] for c in _prog(sid)["checkins_ready"]])
    doc = session_store.load("qa-uid", sid)
    doc["completions"] += qa._extra_recheck_completions()
    session_store.save("qa-uid", sid, doc)
    check("ready again after +3 did_it across +2 dates", qa.MID in [c["milestone_id"] for c in _prog(sid)["checkins_ready"]])


def run_all():
    test_refuses_prod()
    test_qa_label_and_no_product_impact()
    test_scenarios_match_expected()
    test_yes_one_cup()
    test_sometimes_notyet_no_cup()
    test_duplicate_yes_no_dup_cup()
    test_recheck_requires_more_practice()
    print(f"\n{'='*50}\nResults: {_p} passed, {_f} failed")
    if _f: print("❌ QA simulation FAILED"); sys.exit(1)
    print("✅ All QA simulation tests PASSED")

if __name__ == "__main__":
    run_all()
