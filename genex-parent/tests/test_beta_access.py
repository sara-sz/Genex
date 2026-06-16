"""
tests/test_beta_access.py — offline smoke checks for beta-access-code gating.

Runs the FastAPI app in-process with TestClient. Firebase is mocked (no network,
no credentials) and sessions use the local /tmp fallback (no GCS). No OpenAI is
required: question building and the all-"yes" plan path are deterministic.

Covers:
  1. missing Firebase token                              → 401
  2. valid user, missing/wrong beta code on /session/start → 403
  3. valid user + correct code "genex"                   → session starts
  4. valid user + "GENEX" / "  Genex  "                  → session starts
  5. after start, /answer and /plan work WITHOUT the code
  6. a different user cannot access someone else's session → 403

Run: PYTHONPATH=. python3 tests/test_beta_access.py
"""

import os
import sys

# ── Environment must be set BEFORE importing the api package ────────────────
os.environ["FIREBASE_PROJECT_ID"] = "genex-test"
os.environ["LOCAL_SESSION_FALLBACK"] = "1"   # use /tmp, no GCS
os.environ.pop("GCS_BUCKET", None)
os.environ["REQUIRE_BETA_CODE"] = "true"
os.environ["BETA_ACCESS_CODE"] = "genex"
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:3000")
os.environ.setdefault("ACTIVITY_MODEL", "")        # deterministic fallback
os.environ.pop("CONCERN_ROUTER_MODEL", None)

# Make firebase_admin think it is already initialised so _init_firebase() is a
# no-op and never touches Application Default Credentials.
import firebase_admin  # noqa: E402
firebase_admin._apps["[DEFAULT]"] = object()

from firebase_admin import auth as firebase_auth  # noqa: E402

# Map fake bearer tokens → decoded claims. require_auth calls verify_id_token.
_TOKENS = {
    "token-user-a": {"uid": "uid-a", "email": "parenta@example.com"},
    "token-user-b": {"uid": "uid-b", "email": "parentb@example.com"},
}


def _fake_verify_id_token(token, *args, **kwargs):
    if token in _TOKENS:
        return _TOKENS[token]
    raise firebase_auth.InvalidIdTokenError("unknown test token")


firebase_auth.verify_id_token = _fake_verify_id_token

from fastapi.testclient import TestClient  # noqa: E402
from api.main import app  # noqa: E402

client = TestClient(app)

_VALID_START = {
    "child_name": "TestChild",
    "age_years": 3,
    "age_months": 0,
    "age_in_months": 36,
    "diagnosis_or_condition": "No known diagnosis / not sure",
    "parent_concern": "speech delay",
    "daily_time_minutes": 10,
    "timezone": "America/Los_Angeles",
}


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _start_payload(**overrides):
    p = dict(_VALID_START)
    p.update(overrides)
    return p


# ── Result tracking ─────────────────────────────────────────────────────────
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


# ── 1. Missing Firebase token → 401 ─────────────────────────────────────────
def test_missing_token_401():
    print("\n── 1. Missing Firebase token → 401")
    r = client.post("/api/v1/session/start", json=_start_payload(beta_access_code="genex"))
    check("no Authorization header → 401", r.status_code == 401, f"got {r.status_code}: {r.text[:200]}")


# ── 2. Valid user, missing/wrong beta code → 403 ────────────────────────────
def test_missing_or_wrong_code_403():
    print("\n── 2. Valid user, missing/wrong beta code → 403")
    r_missing = client.post("/api/v1/session/start",
                            headers=_hdr("token-user-a"), json=_start_payload())
    check("valid token, no beta code → 403", r_missing.status_code == 403,
          f"got {r_missing.status_code}: {r_missing.text[:200]}")

    r_wrong = client.post("/api/v1/session/start",
                          headers=_hdr("token-user-a"),
                          json=_start_payload(beta_access_code="wrongcode"))
    check("valid token, wrong beta code → 403", r_wrong.status_code == 403,
          f"got {r_wrong.status_code}: {r_wrong.text[:200]}")


# ── 3. Valid user + correct code → session starts ───────────────────────────
def test_correct_code_starts():
    print("\n── 3. Valid user + correct code 'genex' → session starts")
    r = client.post("/api/v1/session/start",
                    headers=_hdr("token-user-a"),
                    json=_start_payload(beta_access_code="genex"))
    check("exact 'genex' → 200", r.status_code == 200, f"got {r.status_code}: {r.text[:200]}")
    if r.status_code == 200:
        check("response has session_id + first question",
              bool(r.json().get("session_id")) and r.json().get("current_question") is not None,
              r.text[:200])


# ── 4. Case-insensitive + space-trimmed codes → session starts ──────────────
def test_case_insensitive_and_trimmed():
    print("\n── 4. Valid user + 'GENEX' / '  Genex  ' → session starts")
    for code in ("GENEX", "Genex", "  genex  "):
        r = client.post("/api/v1/session/start",
                        headers=_hdr("token-user-a"),
                        json=_start_payload(beta_access_code=code))
        check(f"code {code!r} → 200", r.status_code == 200, f"got {r.status_code}: {r.text[:200]}")


# ── 5. After start, /answer and /plan work WITHOUT resending the code ────────
def test_answer_and_plan_no_code():
    print("\n── 5. After start, /answer and /plan work without the beta code")
    r = client.post("/api/v1/session/start",
                    headers=_hdr("token-user-a"),
                    json=_start_payload(beta_access_code="genex"))
    assert r.status_code == 200, f"start failed: {r.status_code} {r.text[:200]}"
    body = r.json()
    sid = body["session_id"]
    q = body["current_question"]

    # Answer every question 'yes' until the interview completes. No beta code is
    # ever sent again — the answer payload schema has no such field.
    completed = False
    guard = 0
    while q is not None and guard < 60:
        guard += 1
        ans = client.post(
            f"/api/v1/session/{sid}/answer",
            headers=_hdr("token-user-a"),
            json={"question_id": q["question_id"], "answer": "yes"},
        )
        if ans.status_code != 200:
            check("/answer → 200 (no code resent)", False, f"got {ans.status_code}: {ans.text[:200]}")
            return
        data = ans.json()
        if data.get("status") == "interview_complete":
            completed = True
            break
        q = data.get("current_question")

    check("/answer flow → interview_complete (no code resent)", completed,
          f"did not complete after {guard} answers")

    r_plan = client.post(f"/api/v1/session/{sid}/plan", headers=_hdr("token-user-a"))
    check("/plan → 200 (no code resent)", r_plan.status_code == 200,
          f"got {r_plan.status_code}: {r_plan.text[:200]}")
    if r_plan.status_code == 200:
        week = r_plan.json().get("week", [])
        check("/plan returns a non-empty week", len(week) >= 1, r_plan.text[:200])


# ── 6. A different user cannot access someone else's session → 403 ──────────
def test_wrong_user_cannot_access():
    print("\n── 6. Different user cannot access someone else's session → 403")
    r = client.post("/api/v1/session/start",
                    headers=_hdr("token-user-a"),
                    json=_start_payload(beta_access_code="genex"))
    assert r.status_code == 200, f"start failed: {r.status_code} {r.text[:200]}"
    sid = r.json()["session_id"]

    r_get_owner = client.get(f"/api/v1/session/{sid}", headers=_hdr("token-user-a"))
    check("owner can GET own session → 200", r_get_owner.status_code == 200,
          f"got {r_get_owner.status_code}: {r_get_owner.text[:200]}")

    r_get_other = client.get(f"/api/v1/session/{sid}", headers=_hdr("token-user-b"))
    check("other user GET → 403", r_get_other.status_code == 403,
          f"got {r_get_other.status_code}: {r_get_other.text[:200]}")

    r_ans_other = client.post(
        f"/api/v1/session/{sid}/answer",
        headers=_hdr("token-user-b"),
        json={"question_id": "anything", "answer": "yes"},
    )
    check("other user /answer → 403", r_ans_other.status_code == 403,
          f"got {r_ans_other.status_code}: {r_ans_other.text[:200]}")


def run_all():
    test_missing_token_401()
    test_missing_or_wrong_code_403()
    test_correct_code_starts()
    test_case_insensitive_and_trimmed()
    test_answer_and_plan_no_code()
    test_wrong_user_cannot_access()

    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ Beta-access smoke checks FAILED")
        sys.exit(1)
    print("✅ All beta-access smoke checks PASSED")


if __name__ == "__main__":
    run_all()
