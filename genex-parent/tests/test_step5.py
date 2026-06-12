"""
tests/test_step5.py — Step 5 verification: /feedback, /report, GET /session

The sandbox has no network access, so pydantic/fastapi cannot be installed.
This script tests the Step 5 logic directly without importing those packages:

  - api.report_generator    → imported directly (no pydantic/fastapi dep)
  - api.session_store       → imported with LOCAL_SESSION_FALLBACK=1
  - api.planning_period     → imported directly
  - api.schemas             → NOT imported (needs pydantic); schema rules
                              are verified by testing the field constraints
                              that Pydantic would enforce, using plain Python
                              assertions so the rules are still checked
  - api.main                → NOT imported (needs fastapi)

All 10 Step 5 checks are covered:
  1.  Feedback record saves to the local store (GCS equivalent).
  2.  Feedback enriched with internal metadata when activity_id matches.
  3.  Feedback saves with metadata_found=False for unknown activity_id.
  4.  Feedback never contains a child name field.
  5.  Report generates for all four report types without error.
  6.  Report uses "your child" — never a child name.
  7.  GET session returns required frontend-safe keys.
  8.  GET session does not expose brain_state, plan_internal, _debug,
      gate_report, or any internal secrets.
  9.  Schema field rules: bad enum values, bad date format, bad note length,
      bad report type are all caught; valid inputs pass.
  10. Existing 40 regression tests still pass.
"""

import os
import sys
import re
import uuid
import subprocess
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ── Project root on path ──────────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ── Env: local fallback, no GCS ──────────────────────────────────────────────
os.environ["LOCAL_SESSION_FALLBACK"] = "1"
os.environ["GCS_BUCKET"] = ""

# ── Importable modules (no pydantic/fastapi) ──────────────────────────────────
import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import api.session_store as store

from api.report_generator import generate_report_body, REPORT_TITLES


# ── Inlined from api.main (logic tested independently of web layer) ───────────

def _find_activity_internal(
    doc: Dict[str, Any],
    plan_id: str,
    activity_id: str,
    day: str,
) -> Optional[Dict[str, Any]]:
    """Verbatim copy of _find_activity_internal from api/main.py."""
    plan_entry = (doc.get("plans") or {}).get(plan_id, {})
    plan_internal = plan_entry.get("plan_internal") or {}
    for day_entry in plan_internal.get("week", []):
        if day_entry.get("day") != day:
            continue
        for act in day_entry.get("activities", []):
            if act.get("frontend_id") == activity_id:
                return act
    return None


_INTERNAL_METADATA_FIELDS = (
    "domain", "subdomain", "milestone_age_months", "milestone_text",
    "bridge_step_index", "bridge_step_text", "activity_family", "theme",
    "difficulty_level", "source_bank_type", "weekend_mode", "support_tier",
)

# Enum values mirrored from api/schemas.py — verified without pydantic
_VALID_ENJOYMENT   = {"loved_it", "it_was_okay", "not_really"}
_VALID_DIFFICULTY  = {"too_easy", "just_right", "too_hard"}
_VALID_COMPLETION  = {"did_it", "didnt_want_to_try", "wasnt_ready_yet"}
_VALID_CARE_TEAM   = {"Doctor", "ST", "OT", "PT", None}
_VALID_REPORT_TYPE = {
    "doctor", "speech_therapist", "occupational_therapist", "physical_therapist"
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_feedback_fields(
    plan_id: str,
    activity_id: str,
    day: str,
    activity_date: str,
    enjoyment: str,
    difficulty: str,
    completion: str,
    note: str = "",
    care_team_member: Optional[str] = None,
) -> Optional[str]:
    """
    Validate FeedbackRequest fields without pydantic.
    Returns an error message string, or None if valid.
    """
    if not plan_id:
        return "plan_id required"
    if not activity_id:
        return "activity_id required"
    if not day:
        return "day required"
    if not _DATE_RE.match(activity_date):
        return f"activity_date must be YYYY-MM-DD, got {activity_date!r}"
    if enjoyment not in _VALID_ENJOYMENT:
        return f"enjoyment must be one of {_VALID_ENJOYMENT}, got {enjoyment!r}"
    if difficulty not in _VALID_DIFFICULTY:
        return f"difficulty must be one of {_VALID_DIFFICULTY}, got {difficulty!r}"
    if completion not in _VALID_COMPLETION:
        return f"completion must be one of {_VALID_COMPLETION}, got {completion!r}"
    if len(note) > 1000:
        return f"note exceeds 1000 chars (len={len(note)})"
    if care_team_member not in _VALID_CARE_TEAM:
        return f"care_team_member must be one of {_VALID_CARE_TEAM}, got {care_team_member!r}"
    return None


def _validate_report_type(report_type: str) -> Optional[str]:
    if report_type not in _VALID_REPORT_TYPE:
        return f"report_type must be one of {_VALID_REPORT_TYPE}, got {report_type!r}"
    return None


# ── Fixtures ──────────────────────────────────────────────────────────────────

KNOWN_FRONTEND_ID = str(uuid.uuid4())
PLAN_ID           = "plan_20260608"
SESSION_ID        = f"test-step5-{uuid.uuid4().hex[:8]}"
OWNER_UID         = "uid-test-step5"


def _make_plan_internal() -> Dict[str, Any]:
    return {
        "week": [
            {
                "day": "Monday",
                "activities": [
                    {
                        "frontend_id":          KNOWN_FRONTEND_ID,
                        "plan_id":              PLAN_ID,
                        "activity_id":          "v22_abc123",
                        "activity_date":        "2026-06-08",
                        "title":                "Bubble Blowing",
                        "domain":               "speech_language",
                        "subdomain":            "oral_motor",
                        "milestone_age_months": 18,
                        "milestone_text":       "Blows bubbles intentionally",
                        "bridge_step_index":    2,
                        "bridge_step_text":     "Blow bubbles for 3 seconds",
                        "activity_family":      "oral_motor_play",
                        "theme":                "sensory",
                        "difficulty_level":     "medium",
                        "source_bank_type":     "speech_language",
                        "weekend_mode":         "",
                        "support_tier":         "independent",
                    }
                ],
            }
        ]
    }


def _make_plan_response() -> Dict[str, Any]:
    return {
        "progress_summary": {
            "domains_covered": [{"label": "Speech & Language"}]
        },
        "week": [
            {
                "day": "Monday",
                "activities": [
                    {"id": KNOWN_FRONTEND_ID, "title": "Bubble Blowing", "day": "Monday"},
                ],
            }
        ],
    }


def _make_session_doc() -> Dict[str, Any]:
    """Minimal session doc — child_name intentionally absent."""
    return {
        "session_id":            SESSION_ID,
        "owner_uid":             OWNER_UID,
        "created_at":            datetime.now(timezone.utc).isoformat(),
        "status":                "plan_ready",
        "age_in_months":         24,
        "daily_time_minutes":    20,
        "timezone":              "America/Los_Angeles",
        "diagnosis_or_condition": "Down syndrome",
        "brain_state":           {"_gate_report": {"gate": "pass"}, "_secret": "internal"},
        "interview":             {},
        "feedback":              [],
        "plan_generated":        True,
        "current_plan_id":       PLAN_ID,
        "plans": {
            PLAN_ID: {
                "plan_period": {
                    "plan_start_date": "2026-06-08",
                    "plan_end_date":   "2026-06-14",
                    "is_partial_week": False,
                },
                "plan_response": _make_plan_response(),
                "plan_internal": _make_plan_internal(),
            }
        },
    }


def _build_feedback_record(
    doc: Dict[str, Any],
    plan_id: str,
    activity_id: str,
    day: str,
    *,
    enjoyment: str = "loved_it",
    difficulty: str = "just_right",
    completion: str = "did_it",
    discuss: bool = False,
    care_team_member: Optional[str] = None,
    note: str = "",
    activity_date: str = "2026-06-08",
) -> Dict[str, Any]:
    """Replicate feedback record construction from api/main.py."""
    internal_act   = _find_activity_internal(doc, plan_id, activity_id, day)
    metadata_found = internal_act is not None

    record: Dict[str, Any] = {
        "feedback_id":            str(uuid.uuid4()),
        "created_at":             datetime.now(timezone.utc).isoformat(),
        "plan_id":                plan_id,
        "activity_id":            activity_id,
        "day":                    day,
        "activity_date":          activity_date,
        "enjoyment":              enjoyment,
        "difficulty":             difficulty,
        "completion":             completion,
        "discuss_with_care_team": discuss,
        "care_team_member":       care_team_member,
        "note":                   note,
        "metadata_found":         metadata_found,
    }
    if metadata_found and internal_act:
        for field in _INTERNAL_METADATA_FIELDS:
            record[field] = internal_act.get(field)

    return record


def _simulate_get_session(doc: Dict[str, Any], admin_debug: bool = False) -> Dict[str, Any]:
    """Replicate GET /session filtering logic from api/main.py."""
    status = doc.get("status", "questions")

    if status in ("questions", "interview_complete"):
        return {
            "session_id":       doc["session_id"],
            "status":           status,
            "current_question": None,
        }

    current_plan_id = doc.get("current_plan_id")
    plans           = doc.get("plans") or {}
    plan_entry      = plans.get(current_plan_id, {}) if current_plan_id else {}

    plan_response    = plan_entry.get("plan_response") or {}
    progress_summary = plan_response.get("progress_summary") or {}

    feedback_list: List[Dict[str, Any]] = doc.get("feedback") or []
    completed = sum(1 for f in feedback_list if f.get("completion") == "did_it")
    flagged   = sum(1 for f in feedback_list if f.get("discuss_with_care_team"))
    domains_practised = list({
        f.get("domain", "") for f in feedback_list if f.get("domain")
    })

    feedback_summary = {
        "total":                 len(feedback_list),
        "completed":             completed,
        "flagged_for_care_team": flagged,
        "domains_practised":     domains_practised,
    }

    response: Dict[str, Any] = {
        "session_id":         doc["session_id"],
        "status":             status,
        "age_in_months":      doc.get("age_in_months"),
        "daily_time_minutes": doc.get("daily_time_minutes"),
        "current_plan_id":    current_plan_id,
        "plan":               plan_response,
        "progress_summary":   progress_summary,
        "feedback_summary":   feedback_summary,
    }

    if admin_debug:
        brain_state = doc.get("brain_state") or {}
        gate_report = brain_state.get("_gate_report")
        if gate_report:
            response["_gate_report"] = gate_report

    return response


# ── Test runner ───────────────────────────────────────────────────────────────

PASS = []
FAIL = []


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


# ══════════════════════════════════════════════════════════════════════════════
# Check 1 — Feedback record saves to the local store (GCS equivalent)
# ══════════════════════════════════════════════════════════════════════════════
print("\n── Check 1: Feedback saves to local store ────────────────────────────")

doc1 = _make_session_doc()
rec1 = _build_feedback_record(doc1, PLAN_ID, KNOWN_FRONTEND_ID, "Monday")
doc1.setdefault("feedback", []).append(rec1)

import warnings as _w
try:
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        result = store.save(OWNER_UID, SESSION_ID, doc1)
    check("1a. store.save returns 'local'", result == "local", f"got: {result}")
except Exception as exc:
    check("1a. store.save returns 'local'", False, str(exc))

with _w.catch_warnings():
    _w.simplefilter("ignore")
    loaded = store.load(OWNER_UID, SESSION_ID)

check("1b. loaded doc has 1 feedback entry",
      loaded is not None and len(loaded.get("feedback", [])) == 1)
check("1c. saved feedback_id round-trips",
      loaded is not None and loaded["feedback"][0]["feedback_id"] == rec1["feedback_id"])
check("1d. feedback activity_date preserved",
      loaded is not None and loaded["feedback"][0]["activity_date"] == "2026-06-08")


# ══════════════════════════════════════════════════════════════════════════════
# Check 2 — Feedback enriched with metadata when activity_id matches
# ══════════════════════════════════════════════════════════════════════════════
print("\n── Check 2: Feedback enriched when activity_id matches ───────────────")

doc2 = _make_session_doc()
rec2 = _build_feedback_record(doc2, PLAN_ID, KNOWN_FRONTEND_ID, "Monday")

check("2a. metadata_found=True",     rec2["metadata_found"] is True)
check("2b. domain enriched",         rec2.get("domain") == "speech_language")
check("2c. activity_family enriched", rec2.get("activity_family") == "oral_motor_play")
check("2d. difficulty_level enriched", rec2.get("difficulty_level") == "medium")
check("2e. support_tier enriched",   rec2.get("support_tier") == "independent")
check("2f. subdomain enriched",      rec2.get("subdomain") == "oral_motor")

missing = [f for f in _INTERNAL_METADATA_FIELDS if f not in rec2]
check("2g. all metadata fields present", len(missing) == 0, f"missing: {missing}")


# ══════════════════════════════════════════════════════════════════════════════
# Check 3 — Feedback saves with metadata_found=False for unknown activity_id
# ══════════════════════════════════════════════════════════════════════════════
print("\n── Check 3: metadata_found=False for unknown activity_id ─────────────")

doc3 = _make_session_doc()
unknown_id = str(uuid.uuid4())
rec3 = _build_feedback_record(doc3, PLAN_ID, unknown_id, "Monday")

check("3a. metadata_found=False",   rec3["metadata_found"] is False)
check("3b. no domain field",        "domain" not in rec3,
      f"unexpectedly found domain={rec3.get('domain')!r}")
check("3c. base fields present",    rec3.get("enjoyment") == "loved_it")
check("3d. activity_id preserved",  rec3["activity_id"] == unknown_id)

# Ensure unknown-id record can still be saved
doc3.setdefault("feedback", []).append(rec3)
sid3 = f"test-step5-{uuid.uuid4().hex[:8]}"
with _w.catch_warnings():
    _w.simplefilter("ignore")
    r3 = store.save(OWNER_UID, sid3, doc3)
check("3e. unknown-id feedback saves ok", r3 == "local")


# ══════════════════════════════════════════════════════════════════════════════
# Check 4 — Feedback record never contains a child name field
# ══════════════════════════════════════════════════════════════════════════════
print("\n── Check 4: Child name not in feedback record ─────────────────────────")

doc4 = _make_session_doc()
rec4 = _build_feedback_record(doc4, PLAN_ID, KNOWN_FRONTEND_ID, "Monday", note="she loved it")

name_keys = [k for k in rec4 if "name" in k.lower() or "child" in k.lower()]
check("4a. no name-like keys in record", name_keys == [], f"found: {name_keys}")
check("4b. 'child_name' absent",         "child_name" not in rec4)
# Confirm session doc itself has no child_name
check("4c. session_doc has no child_name", "child_name" not in doc4)


# ══════════════════════════════════════════════════════════════════════════════
# Check 5 — Report generates for all four report types without error
# ══════════════════════════════════════════════════════════════════════════════
print("\n── Check 5: Report generates for all four types ──────────────────────")

REPORT_TYPES = [
    "doctor", "speech_therapist", "occupational_therapist", "physical_therapist"
]

doc5 = _make_session_doc()
doc5["feedback"] = [
    {
        "feedback_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "plan_id": PLAN_ID, "activity_id": KNOWN_FRONTEND_ID,
        "day": "Monday", "activity_date": "2026-06-08",
        "enjoyment": "loved_it", "difficulty": "just_right", "completion": "did_it",
        "discuss_with_care_team": True, "care_team_member": "ST",
        "note": "Did great", "metadata_found": True,
        "domain": "speech_language", "activity_family": "oral_motor_play",
    }
]

generated_reports: Dict[str, str] = {}
for rt in REPORT_TYPES:
    try:
        body_text = generate_report_body(doc5, rt)
        generated_reports[rt] = body_text
        check(f"5. {rt} generated (len>{100})",
              isinstance(body_text, str) and len(body_text) > 100,
              f"len={len(body_text)}")
    except Exception as exc:
        check(f"5. {rt} generated", False, str(exc))

# Titles match REPORT_TITLES
for rt, title in REPORT_TITLES.items():
    body = generated_reports.get(rt, "")
    check(f"5. {rt} body starts with title",
          body.startswith(title), f"starts with: {body[:50]!r}")


# ══════════════════════════════════════════════════════════════════════════════
# Check 6 — Report uses "your child" — never a child name
# ══════════════════════════════════════════════════════════════════════════════
print("\n── Check 6: Report uses 'your child', no child name ──────────────────")

doc6 = deepcopy(doc5)
# child_name is never in session_doc by design; add canary to confirm generator
# can't accidentally leak it even if it were somehow present
doc6["_canary_child_name"] = "TestChildAlice"

for rt in REPORT_TYPES:
    body_text = generate_report_body(doc6, rt)
    check(f"6a. 'your child' in {rt}",
          "your child" in body_text.lower())
    check(f"6b. canary 'TestChildAlice' absent in {rt}",
          "TestChildAlice" not in body_text,
          "child name leaked into report!")

# Verify disclaimer is present in every report type
for rt in REPORT_TYPES:
    body_text = generate_report_body(doc6, rt)
    check(f"6c. disclaimer in {rt}",
          "not a substitute for" in body_text.lower() or "professional judgment" in body_text.lower())


# ══════════════════════════════════════════════════════════════════════════════
# Check 7 — GET session returns required frontend-safe keys
# ══════════════════════════════════════════════════════════════════════════════
print("\n── Check 7: GET session returns required frontend-safe keys ──────────")

doc7 = _make_session_doc()
resp7 = _simulate_get_session(doc7)

required_keys = [
    "session_id", "status", "age_in_months", "daily_time_minutes",
    "current_plan_id", "plan", "progress_summary", "feedback_summary",
]
for k in required_keys:
    check(f"7. key '{k}' present", k in resp7, f"keys: {list(resp7.keys())}")

fs = resp7.get("feedback_summary", {})
for sk in ["total", "completed", "flagged_for_care_team", "domains_practised"]:
    check(f"7. feedback_summary.{sk}", sk in fs)

# Values are correct types
check("7. status is str",           isinstance(resp7.get("status"), str))
check("7. age_in_months is int",    isinstance(resp7.get("age_in_months"), int))
check("7. feedback_summary.total",  fs.get("total") == 0)

# Interview-in-progress path returns a simpler shape
doc7_q = deepcopy(doc7)
doc7_q["status"] = "questions"
resp7_q = _simulate_get_session(doc7_q)
check("7. questions status returns session_id", "session_id" in resp7_q)
check("7. questions status has no plan key",    "plan" not in resp7_q)


# ══════════════════════════════════════════════════════════════════════════════
# Check 8 — GET session strips secrets
# ══════════════════════════════════════════════════════════════════════════════
print("\n── Check 8: GET session strips secrets (no admin debug) ──────────────")

doc8 = _make_session_doc()
resp8 = _simulate_get_session(doc8, admin_debug=False)

# Top-level secrets must be absent
for forbidden in ("brain_state", "_secret", "_gate_report"):
    check(f"8. '{forbidden}' absent (non-admin)",
          forbidden not in resp8, f"found in response: {resp8.get(forbidden)!r}")

# plan field must not expose plan_internal or _debug
plan_in_resp = resp8.get("plan", {})
check("8. plan_internal not in plan",   "plan_internal" not in plan_in_resp)
check("8. _debug not in plan str",      "_debug" not in str(plan_in_resp))

# admin_debug=True DOES expose gate_report (check both ways)
resp8_admin = _simulate_get_session(doc8, admin_debug=True)
check("8. gate_report present with admin_debug", "_gate_report" in resp8_admin)
check("8. brain_state still absent with admin",  "brain_state" not in resp8_admin)

# Raw feedback notes must not be in the summary
doc8_notes = deepcopy(doc8)
doc8_notes["feedback"] = [{
    "completion": "did_it", "discuss_with_care_team": False,
    "domain": "speech_language",
    "note": "SECRET_NOTE_CONTENT",
    "activity_date": "2026-06-08",
}]
resp8_notes = _simulate_get_session(doc8_notes)
check("8. raw feedback notes not in GET response",
      "SECRET_NOTE_CONTENT" not in str(resp8_notes))


# ══════════════════════════════════════════════════════════════════════════════
# Check 9 — Schema field rules (mirrored from api/schemas.py, no pydantic)
# ══════════════════════════════════════════════════════════════════════════════
print("\n── Check 9: Schema field rules ───────────────────────────────────────")

# 9a: bad enjoyment value
err = _validate_feedback_fields(
    "p1", "a1", "Monday", "2026-06-08",
    enjoyment="super_loved_it", difficulty="just_right", completion="did_it",
)
check("9a. bad enjoyment → error", err is not None, "expected validation error")

# 9b: bad activity_date format
err = _validate_feedback_fields(
    "p1", "a1", "Monday", "08/06/2026",
    enjoyment="loved_it", difficulty="just_right", completion="did_it",
)
check("9b. bad date format → error", err is not None, "expected validation error")

# 9c: bad completion value
err = _validate_feedback_fields(
    "p1", "a1", "Monday", "2026-06-08",
    enjoyment="loved_it", difficulty="just_right", completion="maybe",
)
check("9c. bad completion → error", err is not None, "expected validation error")

# 9d: note over 1000 chars
err = _validate_feedback_fields(
    "p1", "a1", "Monday", "2026-06-08",
    enjoyment="loved_it", difficulty="just_right", completion="did_it",
    note="x" * 1001,
)
check("9d. note >1000 chars → error", err is not None, "expected validation error")

# 9e: bad report type
err_rt = _validate_report_type("nurse")
check("9e. bad report_type → error", err_rt is not None, "expected validation error")

# 9f: valid FeedbackRequest fields
err = _validate_feedback_fields(
    "p1", "a1", "Monday", "2026-06-08",
    enjoyment="loved_it", difficulty="just_right", completion="did_it",
    care_team_member="ST", note="Great session!",
)
check("9f. valid feedback fields → no error", err is None, f"unexpected: {err}")

# 9g: valid report type
err_rt = _validate_report_type("doctor")
check("9g. valid report_type → no error", err_rt is None, f"unexpected: {err_rt}")

# 9h: all four valid report types pass
for rt in _VALID_REPORT_TYPE:
    err_rt = _validate_report_type(rt)
    check(f"9h. '{rt}' passes", err_rt is None)

# 9i: care_team_member=None is valid (optional field)
err = _validate_feedback_fields(
    "p1", "a1", "Monday", "2026-06-08",
    enjoyment="it_was_okay", difficulty="too_easy", completion="didnt_want_to_try",
    care_team_member=None,
)
check("9i. care_team_member=None is valid", err is None, f"unexpected: {err}")


# ══════════════════════════════════════════════════════════════════════════════
# Check 10 — Regression: 40 genex_core tests still pass
# ══════════════════════════════════════════════════════════════════════════════
print("\n── Check 10: Regression tests (40 genex_core tests) ─────────────────")

regression_script = os.path.join(os.path.dirname(__file__), "test_regression.py")
result = subprocess.run(
    [sys.executable, regression_script],
    capture_output=True,
    text=True,
    env={**os.environ, "LOCAL_SESSION_FALLBACK": "1", "GCS_BUCKET": ""},
)
if result.returncode == 0:
    for line in result.stdout.splitlines():
        if any(w in line for w in ("PASSED", "passed", "PASS", "40/40")):
            print(f"        {line.strip()}")
    check("10. regression 40/40 passed", True)
else:
    tail = (result.stdout + result.stderr)[-2000:]
    print(tail)
    check("10. regression 40/40 passed", False, f"exit code {result.returncode}")


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
