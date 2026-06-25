"""
tests/test_focus_primary.py — Beta 2.2 Slice 1: primary-focus-only intake

Verifies the API-layer focus selector + the focus block surfaced in GET /session
and GET /session/current, plus single-primary intake/plan. genex_core is frozen.

In-process TestClient; Firebase mocked; local /tmp store; ACTIVITY_MODEL empty.

Run: PYTHONPATH=. python3 tests/test_focus_primary.py
"""

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
from api.focus_selector import select_focus, build_focus_block  # noqa: E402

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


def _start(concern, age_in_months=36):
    yrs, mos = divmod(age_in_months, 12)
    return client.post("/api/v1/session/start", headers=_hdr(), json={
        "child_name": "C", "age_years": yrs, "age_months": mos, "age_in_months": age_in_months,
        "diagnosis_or_condition": "No known diagnosis / not sure", "parent_concern": concern,
        "daily_time_minutes": 10, "timezone": "UTC", "beta_access_code": "genex"})


# ── Unit: focus examples (primary = EARLIEST addressable concern; priority is
#         only a tie-break / fallback). Includes the original 5 + order-sensitive 7.
def test_five_examples_unit():
    print("\n── selector: required examples (earliest-mention rule)")
    cases = [
        # original 5
        ("speech delay and learning difficulty", "language_and_communication", ["cognitive"]),
        ("seizures, speech regression, not walking steady, not running, fine motor delay",
         "language_and_communication", ["movement_and_physical"]),
        ("lack of attention, socially afraid", "cognitive", ["social_and_emotional"]),
        ("not walking steadily, not running, fine motor delay", "movement_and_physical", []),
        ("learning difficulty", "cognitive", []),
        # order-sensitive corrections — earliest mention wins, NOT fixed priority
        ("lack of attention, learning difficulty, speech delay", "cognitive", ["language_and_communication"]),
        ("learning difficulty and speech delay", "cognitive", ["language_and_communication"]),
        ("socially afraid, lack of attention", "social_and_emotional", ["cognitive"]),
    ]
    for concern, exp_primary, exp_rec in cases:
        primary, detected = select_focus("", concern)
        fb = build_focus_block(primary, detected)
        check(f"[{concern[:32]}] primary={exp_primary.split('_')[0]}",
              fb["primary_focus_key"] == exp_primary, fb["primary_focus_key"])
        for r in exp_rec:
            check(f"   recommends {r.split('_')[0]}", r in fb["recommended_focus_area_keys"], fb["recommended_focus_area_keys"])
        rem_keys = [r["key"] for r in fb["remaining_focus_areas"]]
        check("   3 remaining areas, all available", len(rem_keys) == 3 and exp_primary not in rem_keys, rem_keys)
    # seizures stay out of focus entirely
    _, det = select_focus("", "seizures, speech regression, not walking steady, not running, fine motor delay")
    fb = build_focus_block(*select_focus("", "seizures only"))  # 'seizures only' → no focus detected
    check("seizures alone → not a developmental focus",
          "seizure" not in str(det).lower(), det)


# ── E2E: focus block in GET /session and /session/current ───────────────────
def test_focus_block_in_get_session():
    print("\n── GET /session + /session/current expose the focus block")
    r = _start("speech delay and learning difficulty")
    sid = r.json()["session_id"]
    # focus already present in /session/start? (we expose via GET)
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    check("GET /session has focus block", "focus" in g and g["focus"].get("primary_focus_key"), g.get("focus"))
    f = g["focus"]
    check("primary = Speech & Communication", f["primary_focus_key"] == "language_and_communication", f)
    check("primary_focus_label friendly", f["primary_focus_label"] == "Speech & Communication")
    check("recommended includes cognitive", "cognitive" in f["recommended_focus_area_keys"])
    check("remaining includes movement + social",
          {"movement_and_physical", "social_and_emotional"} <= {r["key"] for r in f["remaining_focus_areas"]})
    check("added_focus_areas == []", f["added_focus_areas"] == [])
    check("all_focus_areas has 4", len(f["all_focus_areas"]) == 4)
    # /session/current returns the same block
    cur = client.get("/api/v1/session/current", headers=_hdr()).json()
    check("/session/current has identical focus block", cur.get("focus") == f, cur.get("focus"))


# ── E2E: single-primary intake (one domain's questions, ≤ 7) ────────────────
def test_single_primary_intake():
    print("\n── multi-concern → only one domain's questions, max 7")
    r = _start("speech delay and learning difficulty")
    sid = r.json()["session_id"]
    doc = session_store.load("uid-a", sid)
    domain_keys = doc["interview"]["domain_keys"]
    check("exactly one interview domain", len(domain_keys) == 1, domain_keys)
    check("that domain is the primary (language)", domain_keys == ["language_and_communication"], domain_keys)
    check("selected_domain_keys persisted == primary", doc["brain_state"].get("selected_domain_keys") == ["language_and_communication"])
    # count questions actually asked
    asked = 0
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    q = g.get("current_question")
    while q is not None and asked < 30:
        asked += 1
        a = client.post(f"/api/v1/session/{sid}/answer", headers=_hdr(),
                        json={"question_id": q["question_id"], "answer": "yes"}).json()
        if a.get("status") == "interview_complete":
            break
        q = a.get("current_question")
    check("question count <= 7", asked <= 7, asked)


# ── E2E: plan builds only the primary domain ────────────────────────────────
def test_plan_single_domain():
    print("\n── plan generation builds ONLY the primary focus domain/bank")
    r = _start("speech delay and learning difficulty")
    sid = r.json()["session_id"]
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    q = g.get("current_question")
    while q is not None:
        a = client.post(f"/api/v1/session/{sid}/answer", headers=_hdr(),
                        json={"question_id": q["question_id"], "answer": "yes"}).json()
        if a.get("status") == "interview_complete":
            break
        q = a.get("current_question")
    plan = client.post(f"/api/v1/session/{sid}/plan", headers=_hdr()).json()
    domains = {a["domain"] for d in plan["week"] for a in d["activities"]}
    check("plan activities are all the primary domain (language)",
          domains <= {"language_and_communication"}, domains)
    doc = session_store.load("uid-a", sid)
    banks = list(doc["brain_state"].get("activity_banks", {}).keys())
    check("only the primary bank was built", banks == ["language_and_communication"], banks)
    # focus block still present post-plan
    g2 = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    check("focus block present after plan_ready", g2.get("focus", {}).get("primary_focus_key") == "language_and_communication")


def run_all():
    test_five_examples_unit()
    test_focus_block_in_get_session()
    test_single_primary_intake()
    test_plan_single_domain()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ focus primary tests FAILED")
        sys.exit(1)
    print("✅ All focus primary tests PASSED")


if __name__ == "__main__":
    run_all()
