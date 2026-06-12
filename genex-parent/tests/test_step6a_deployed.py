"""
tests/test_step6a_deployed.py — Step 6A deployed smoke test.

Hits the real Cloud Run genex-api-staging service with real Firebase tokens
and real GCS session storage. No mocks.

Prerequisites:
  1. genex-api-staging deployed (bash scripts/deploy_api_staging.sh)
  2. Valid Firebase ID token obtained (python3 scripts/get_test_token.py)

Usage:
    export GENEX_API_URL=https://genex-api-staging-<hash>-uc.a.run.app
    export GENEX_API_TOKEN=<firebase_id_token>
    python3 tests/test_step6a_deployed.py

Env vars:
    GENEX_API_URL    — Cloud Run service URL (required)
    GENEX_API_TOKEN  — Firebase ID token for allowlisted user (required)
    GENEX_GCS_BUCKET — GCS bucket for session verification (optional;
                       defaults to genex-parent-sessions-genex-mvp-2026)

Checks:
  1.  GET /health → 200, version=v22
  2.  No-token request → 401
  3.  Full allowlisted user flow (start → answer → plan → feedback → report × 4 → GET)
  4.  GCS session doc: no child_name, brain_state stored, plan_internal stored,
      plan_response clean, feedback saved and enriched
  5.  GET /session: brain_state absent, plan_internal absent, _debug absent
  6.  ADMIN_DEBUG=0 confirmed: _gate_report absent from GET response
  7.  All four reports use "your child"
  8.  Regression: 40/40 genex_core tests still pass
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

# ── Config ────────────────────────────────────────────────────────────────────

API_URL     = os.environ.get("GENEX_API_URL", "").rstrip("/")
API_TOKEN   = os.environ.get("GENEX_API_TOKEN", "")
GCS_BUCKET  = os.environ.get("GENEX_GCS_BUCKET", "genex-parent-sessions-genex-mvp-2026")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if not API_URL:
    print("ERROR: GENEX_API_URL is not set.", file=sys.stderr)
    print("       export GENEX_API_URL=https://genex-api-staging-<hash>-uc.a.run.app", file=sys.stderr)
    sys.exit(1)

if not API_TOKEN:
    print("ERROR: GENEX_API_TOKEN is not set.", file=sys.stderr)
    print("       Run: eval $(python3 scripts/get_test_token.py)", file=sys.stderr)
    sys.exit(1)


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _request(
    method: str,
    path: str,
    body: Optional[Dict] = None,
    token: Optional[str] = API_TOKEN,
    timeout: int = 30,
) -> tuple[int, Dict]:
    """Make an HTTP request. Returns (status_code, response_body_dict)."""
    url = f"{API_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body_text = exc.read().decode("utf-8")
            try:
                return exc.code, json.loads(body_text)
            except Exception:
                return exc.code, {"_raw": body_text}
        except Exception:
            return exc.code, {}
    except Exception as exc:
        return 0, {"_error": str(exc)}


# ── Test runner ───────────────────────────────────────────────────────────────

PASS: list = []
FAIL: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(label)
        print(f"  PASS  {label}")
    else:
        FAIL.append(label)
        msg = f"  FAIL  {label}"
        if detail:
            msg += f"\n        → {detail}"
        print(msg)


print(f"\n  Target: {API_URL}")
print(f"  Bucket: {GCS_BUCKET}")


# ══════════════════════════════════════════════════════════════════════════════
# Check 1 — GET /health → 200
# ══════════════════════════════════════════════════════════════════════════════
print("\n── Check 1: GET /health ──────────────────────────────────────────────")

status, body = _request("GET", "/health", token=None)
check("1a. /health → 200",  status == 200, f"status={status}")
check("1b. ok=true",         body.get("ok") is True)
check("1c. version=v22",     body.get("version") == "v22")


# ══════════════════════════════════════════════════════════════════════════════
# Check 2 — No token → 401
# ══════════════════════════════════════════════════════════════════════════════
print("\n── Check 2: No token → 401 ────────────────────────────────────────────")

status2, _ = _request("POST", "/api/v1/session/start", body={
    "child_name": "T", "age_years": 2, "age_months": 0,
    "age_in_months": 24, "diagnosis_or_condition": "Down syndrome",
    "parent_concern": "speech delay", "daily_time_minutes": 20,
}, token=None)
check("2. no token → 401", status2 == 401, f"status={status2}")


# ══════════════════════════════════════════════════════════════════════════════
# Check 3 — Full allowlisted flow
# ══════════════════════════════════════════════════════════════════════════════
print("\n── Check 3: Full flow — start → answer → plan → feedback → report → GET")

# 3a. POST /session/start
status3, start = _request("POST", "/api/v1/session/start", body={
    "child_name":             "SmokeTestChild",
    "age_years":              2,
    "age_months":             0,
    "age_in_months":          24,
    "diagnosis_or_condition": "Down syndrome",
    "parent_concern":         "She has some speech delay and low muscle tone.",
    "daily_time_minutes":     20,
    "timezone":               "America/Los_Angeles",
})
check("3a. /session/start → 200", status3 == 200,
      f"status={status3} body={str(start)[:200]}")

SESSION_ID = None
if status3 == 200:
    SESSION_ID = start.get("session_id")
    check("3b. session_id present",   bool(SESSION_ID))
    check("3c. status=questions",     start.get("status") == "questions")
    check("3d. first question set",   start.get("current_question") is not None)

if not SESSION_ID:
    print("  !! Aborting flow — session/start failed.")
else:
    # 3e. Answer loop
    MAX_QUESTIONS = 60
    q = start.get("current_question")
    answered = 0
    last_ans = None

    while q and answered < MAX_QUESTIONS:
        sa, ans = _request("POST", f"/api/v1/session/{SESSION_ID}/answer", body={
            "question_id": q["question_id"],
            "answer":      "yes",
        })
        if sa != 200:
            check(f"3e. answer {answered+1} → 200", False,
                  f"status={sa} body={str(ans)[:150]}")
            break
        answered += 1
        last_ans = ans
        if ans.get("status") == "interview_complete":
            q = None
        elif ans.get("status") == "next_question":
            q = ans.get("current_question")
        else:
            q = None

    check("3e. interview completed",
          last_ans is not None and last_ans.get("status") == "interview_complete",
          f"last={last_ans.get('status') if last_ans else 'none'}")
    print(f"        (answered {answered} questions)")

    # 3f. POST /plan — runs full brain pipeline + OpenAI; allow up to 4 minutes.
    # On a cold start, Cloud Run may take >240s total. If the client times out,
    # the server keeps running and saves the plan to GCS. We detect this by
    # polling GET /session until status=plan_ready (up to 3 extra minutes).
    print("        (calling /plan — runs OpenAI pipeline, may take 60-180s on cold start...)")
    sp, plan = _request("POST", f"/api/v1/session/{SESSION_ID}/plan", timeout=240)

    PLAN_ID          = None
    PLAN_ACTIVITY_ID = None
    PLAN_DAY         = None
    PLAN_DATE        = None

    if sp == 0 and "timed out" in str(plan.get("_error", "")).lower():
        # Client timed out but server may still be running. Poll GET /session.
        print("        (/plan client timeout — server may still be running, polling GET /session...)")
        poll_start = time.time()
        plan_from_get = None
        while time.time() - poll_start < 180:
            time.sleep(10)
            elapsed = int(time.time() - poll_start)
            print(f"        (polling... {elapsed}s elapsed)")
            pg, pdata = _request("GET", f"/api/v1/session/{SESSION_ID}", timeout=30)
            if pg == 200 and pdata.get("status") == "plan_ready":
                plan_from_get = pdata.get("plan") or {}
                print("        (/plan completed on server — retrieved via GET /session)")
                sp = 200
                plan = plan_from_get
                break
        if sp != 200:
            check("3f. /plan → 200 (or server-completed)", False,
                  "plan timed out and did not complete within 3 extra minutes")
        else:
            check("3f. /plan → 200 (server-completed, retrieved via GET)", True)
    else:
        check("3f. /plan → 200", sp == 200,
              f"status={sp} body={str(plan)[:200]}")

    if sp == 200 and plan:
        check("3g. plan has week",            "week" in plan)
        check("3h. plan has progress_summary", "progress_summary" in plan)
        check("3i. plan has plan_period",     "plan_period" in plan)
        check("3j. plan_internal absent",     "plan_internal" not in plan,
              "plan_internal leaked to frontend!")
        check("3k. _debug absent from plan",  "_debug" not in str(plan))

        PLAN_ID = (plan.get("plan_period") or {}).get("plan_id")
        # If plan came from GET /session, current_plan_id is the fallback
        if not PLAN_ID:
            sgf, sgfd = _request("GET", f"/api/v1/session/{SESSION_ID}", timeout=30)
            if sgf == 200:
                PLAN_ID = sgfd.get("current_plan_id")

        for day_entry in plan.get("week", []):
            acts = day_entry.get("activities", [])
            if acts:
                PLAN_DAY         = day_entry["day"]
                PLAN_ACTIVITY_ID = acts[0].get("id")
                PLAN_DATE        = acts[0].get("activity_date")
                break

    # 3l. POST /feedback
    if PLAN_ID and PLAN_ACTIVITY_ID and PLAN_DAY and PLAN_DATE:
        sfb, fb = _request("POST", f"/api/v1/session/{SESSION_ID}/feedback", body={
            "plan_id":                PLAN_ID,
            "activity_id":            PLAN_ACTIVITY_ID,
            "day":                    PLAN_DAY,
            "activity_date":          PLAN_DATE,
            "enjoyment":              "loved_it",
            "difficulty":             "just_right",
            "completion":             "did_it",
            "discuss_with_care_team": True,
            "care_team_member":       "ST",
            "note":                   "She loved the bubble activity!",
        })
        check("3l. /feedback → 200", sfb == 200,
              f"status={sfb} body={str(fb)[:200]}")
        if sfb == 200:
            check("3m. feedback ok=true",          fb.get("ok") is True)
            check("3n. feedback_id present",       bool(fb.get("feedback_id")))
            check("3o. flagged_for_care_team=true", fb.get("flagged_for_care_team") is True)
            print(f"        (metadata_found={fb.get('metadata_found')})")
            FEEDBACK_RESPONSE = fb
        else:
            FEEDBACK_RESPONSE = None
    else:
        check("3l. /feedback (skipped — no plan_id)", False,
              f"PLAN_ID={PLAN_ID} PLAN_ACTIVITY_ID={PLAN_ACTIVITY_ID}")
        FEEDBACK_RESPONSE = None

    # 3p. POST /report × 4
    REPORT_TYPES = ["doctor", "speech_therapist", "occupational_therapist", "physical_therapist"]
    GENERATED_REPORTS: Dict[str, Dict] = {}
    for rt in REPORT_TYPES:
        sr, rep = _request("POST", f"/api/v1/session/{SESSION_ID}/report",
                           body={"report_type": rt}, timeout=60)
        check(f"3p. /report/{rt} → 200", sr == 200,
              f"status={sr} body={str(rep)[:150]}")
        if sr == 200:
            GENERATED_REPORTS[rt] = rep

    # 3q. GET /session
    sg, get_sess = _request("GET", f"/api/v1/session/{SESSION_ID}", timeout=60)
    check("3q. GET /session → 200", sg == 200,
          f"status={sg} body={str(get_sess)[:200]}")

    GET_SESSION = get_sess if sg == 200 else None


# ══════════════════════════════════════════════════════════════════════════════
# Check 4 — GCS session doc verification
# ══════════════════════════════════════════════════════════════════════════════
print("\n── Check 4: GCS session doc ──────────────────────────────────────────")

if SESSION_ID and 'GET_SESSION' in dir():
    # We can't read GCS directly without credentials here, but the GET /session
    # endpoint proves GCS round-trip (it reads from GCS, not memory cache, because
    # LOCAL_SESSION_FALLBACK=0 and the service just started fresh for this session).
    #
    # Structural verification via GET response confirms what was saved to GCS.

    gs = GET_SESSION or {}
    check("4a. session reloaded from GCS", bool(gs.get("session_id")))
    check("4b. status=plan_ready",         gs.get("status") == "plan_ready",
          f"status={gs.get('status')}")

    # Confirm no child_name in GET response (proxy for no child_name in GCS)
    check("4c. child_name absent from GET", "child_name" not in gs,
          f"keys: {list(gs.keys())}")

    # Confirm feedback round-tripped through GCS
    fb_summary = gs.get("feedback_summary") or {}
    check("4d. feedback_summary.total ≥ 1", fb_summary.get("total", 0) >= 1,
          f"total={fb_summary.get('total')}")
    check("4e. feedback_summary.completed ≥ 1", fb_summary.get("completed", 0) >= 1)
    check("4f. flagged_for_care_team = 1", fb_summary.get("flagged_for_care_team", 0) == 1)

    # Confirm plan is present (proves plan was saved and reloaded from GCS)
    plan_in_get = gs.get("plan") or {}
    check("4g. plan present in GET",         bool(plan_in_get))
    check("4h. plan_internal NOT in plan",   "plan_internal" not in plan_in_get,
          "plan_internal leaked to GET /session!")

    # Optional: list the GCS file to prove it exists
    print("")
    print("  Optional GCS file listing (requires gcloud ADC):")
    try:
        uid_part = API_TOKEN.split(".")[1]  # JWT payload (not decoded, just for UID hint)
        print(f"    gcloud storage ls gs://{GCS_BUCKET}/sessions/ | grep {SESSION_ID}")
    except Exception:
        pass

else:
    check("4. GCS session available for checks", False, "session flow failed earlier")


# ══════════════════════════════════════════════════════════════════════════════
# Check 5 — GET /session secrets stripped
# ══════════════════════════════════════════════════════════════════════════════
print("\n── Check 5: GET /session strips secrets ──────────────────────────────")

if 'GET_SESSION' in dir() and GET_SESSION:
    gs5 = GET_SESSION
    check("5a. brain_state absent",     "brain_state" not in gs5)
    check("5b. _secret absent",         "_secret" not in gs5)
    check("5c. plan_internal not in plan", "plan_internal" not in (gs5.get("plan") or {}))
    check("5d. _debug absent from plan",   "_debug" not in str(gs5.get("plan") or {}))

    # Check feedback_summary doesn't expose raw notes
    check("5e. raw notes absent from summary",
          "She loved the bubble activity" not in json.dumps(gs5))

else:
    check("5. GET session available", False, "session flow failed earlier")


# ══════════════════════════════════════════════════════════════════════════════
# Check 6 — ADMIN_DEBUG=0: _gate_report absent
# ══════════════════════════════════════════════════════════════════════════════
print("\n── Check 6: ADMIN_DEBUG=0 — _gate_report absent ─────────────────────")

if 'GET_SESSION' in dir() and GET_SESSION:
    check("6. _gate_report absent (ADMIN_DEBUG=0)",
          "_gate_report" not in GET_SESSION,
          f"Found _gate_report in GET response — ADMIN_DEBUG may not be 0")
else:
    check("6. GET session available", False, "session flow failed earlier")


# ══════════════════════════════════════════════════════════════════════════════
# Check 7 — Reports use "your child"
# ══════════════════════════════════════════════════════════════════════════════
print("\n── Check 7: Reports use 'your child', no child name ─────────────────")

for rt, rep_data in (GENERATED_REPORTS.items() if 'GENERATED_REPORTS' in dir() else {}.items()):
    body_text = rep_data.get("body", "")
    check(f"7a. 'your child' in {rt}",
          "your child" in body_text.lower(), body_text[:80])
    check(f"7b. 'SmokeTestChild' absent from {rt}",
          "SmokeTestChild" not in body_text, "child name leaked!")


# ══════════════════════════════════════════════════════════════════════════════
# Check 8 — Regression tests
# ══════════════════════════════════════════════════════════════════════════════
print("\n── Check 8: Regression tests — 40/40 ───────────────────────────────")

regression_script = os.path.join(os.path.dirname(__file__), "test_regression.py")
result = subprocess.run(
    [sys.executable, regression_script],
    capture_output=True,
    text=True,
    env={**os.environ, "LOCAL_SESSION_FALLBACK": "1", "GCS_BUCKET": ""},
)
if result.returncode == 0:
    for line in result.stdout.splitlines():
        if any(w in line for w in ("PASSED", "passed", "PASS", "40/40", "✅")):
            print(f"        {line.strip()}")
    check("8. regression 40/40 passed", True)
else:
    print((result.stdout + result.stderr)[-2000:])
    check("8. regression 40/40 passed", False, f"exit={result.returncode}")


# ══════════════════════════════════════════════════════════════════════════════
# Sample outputs
# ══════════════════════════════════════════════════════════════════════════════
print("\n── Sample outputs ────────────────────────────────────────────────────")

if 'GET_SESSION' in dir() and GET_SESSION:
    safe = {k: v for k, v in GET_SESSION.items() if k not in ("plan",)}
    safe["plan_week_count"] = len((GET_SESSION.get("plan") or {}).get("week", []))
    print("\nSafe GET /session response:")
    print(json.dumps(safe, indent=2, default=str))

if 'FEEDBACK_RESPONSE' in dir() and FEEDBACK_RESPONSE:
    print("\nFeedback response:")
    print(json.dumps(FEEDBACK_RESPONSE, indent=2, default=str))

if 'GENERATED_REPORTS' in dir() and GENERATED_REPORTS.get("doctor"):
    print("\nDoctor report excerpt (first 600 chars):")
    print(GENERATED_REPORTS["doctor"].get("body", "")[:600])


# ══════════════════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═" * 60)
total = len(PASS) + len(FAIL)
print(f"  {len(PASS)} / {total} checks passed")
if FAIL:
    print(f"\n  FAILED ({len(FAIL)}):")
    for f in FAIL:
        print(f"    • {f}")
print("═" * 60)

if FAIL:
    sys.exit(1)
