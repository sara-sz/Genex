"""
tests/test_beta_multicode.py — Beta 2.3: multi-code beta access (comma-separated).

BETA_ACCESS_CODE may be a single code OR a comma-separated list; a submitted code is
accepted if it matches ANY configured code (trimmed + lowercased). Single-code behavior
is unchanged. Adding/removing codes is env-only (no backend code change).

Run: PYTHONPATH=. python3 tests/test_beta_multicode.py
"""
import json
import os
import sys

os.environ["FIREBASE_PROJECT_ID"] = "genex-test"
os.environ["LOCAL_SESSION_FALLBACK"] = "1"
os.environ.pop("GCS_BUCKET", None)
os.environ["REQUIRE_BETA_CODE"] = "true"
os.environ["BETA_ACCESS_CODE"] = "genex23,genex-family-beta-22"   # prod-style multi-code
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:3000")
os.environ["ACTIVITY_MODEL"] = ""
os.environ.pop("CONCERN_ROUTER_MODEL", None)

import firebase_admin  # noqa: E402
firebase_admin._apps["[DEFAULT]"] = object()
from firebase_admin import auth as fa  # noqa: E402
fa.verify_id_token = lambda t, *a, **k: {"uid": "uid-a", "email": "a@x.com"} if t == "tok" else (_ for _ in ()).throw(fa.InvalidIdTokenError("bad"))

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from api.main import app  # noqa: E402
from api import auth as auth_mod  # noqa: E402
from api import session_store  # noqa: E402

client = TestClient(app)
_p = _f = 0
def check(l, ok, d=""):
    global _p, _f
    if ok: _p += 1; print(f"  ✓ {l}")
    else: _f += 1; print(f"  ✗ FAIL: {l} — {d}")

_START = {"child_name": "T", "age_years": 3, "age_months": 0, "age_in_months": 36,
          "diagnosis_or_condition": "No known diagnosis / not sure", "parent_concern": "speech delay",
          "daily_time_minutes": 10, "timezone": "America/Los_Angeles"}
def _start(code=None, env=None):
    if env is not None:
        os.environ["BETA_ACCESS_CODE"] = env
    body = dict(_START)
    if code is not None:
        body["beta_access_code"] = code
    return client.post("/api/v1/session/start", headers={"Authorization": "Bearer tok"}, json=body)

def _accepts(env, submitted):
    """Call verify_beta_code directly; True if accepted (no exception)."""
    os.environ["BETA_ACCESS_CODE"] = env
    try:
        auth_mod.verify_beta_code(submitted)
        return True
    except HTTPException:
        return False


# ── unit tests over the parser + verifier ───────────────────────────────────
def test_parser():
    print("\n── _configured_beta_codes parsing")
    os.environ["BETA_ACCESS_CODE"] = "genex"
    check("single → one-element set", auth_mod._configured_beta_codes() == {"genex"})
    os.environ["BETA_ACCESS_CODE"] = "genex23,genex-family-beta-22"
    check("multi → set of both", auth_mod._configured_beta_codes() == {"genex23", "genex-family-beta-22"})
    os.environ["BETA_ACCESS_CODE"] = "  GENEX23 , Genex-Family-Beta-22 ,"
    check("trims + lowercases + drops empties",
          auth_mod._configured_beta_codes() == {"genex23", "genex-family-beta-22"}, auth_mod._configured_beta_codes())


def test_verifier_matrix():
    print("\n── verify_beta_code accept/reject matrix")
    # single-code behavior unchanged
    check("single: genex accepted", _accepts("genex", "genex"))
    check("single: wrong rejected", not _accepts("genex", "wrong"))
    check("single: GENEX (case) accepted", _accepts("genex", "GENEX"))
    # multi
    check("multi: first code accepted", _accepts("genex23,genex-family-beta-22", "genex23"))
    check("multi: second code accepted", _accepts("genex23,genex-family-beta-22", "genex-family-beta-22"))
    check("multi: trims submitted", _accepts("genex23,genex-family-beta-22", "  genex23  "))
    check("multi: case-insensitive", _accepts("genex23,genex-family-beta-22", "GENEX-Family-Beta-22"))
    check("multi: spaces in config tolerated", _accepts(" genex23 , genex-family-beta-22 ", "genex23"))
    # rejections
    check("genex rejected when not configured", not _accepts("genex23,genex-family-beta-22", "genex"))
    check("random code rejected", not _accepts("genex23,genex-family-beta-22", "hunter2"))
    check("empty submitted rejected", not _accepts("genex23,genex-family-beta-22", ""))
    check("None submitted rejected", not _accepts("genex23,genex-family-beta-22", None))
    # enforcement toggle unchanged
    os.environ["REQUIRE_BETA_CODE"] = "false"
    check("no-op when REQUIRE_BETA_CODE=false (any code passes)", _accepts("genex23", "anything"))
    os.environ["REQUIRE_BETA_CODE"] = "true"


# ── HTTP end-to-end with prod-style multi-code config ───────────────────────
def test_http_prod_style():
    print("\n── HTTP /session/start with BETA_ACCESS_CODE='genex23,genex-family-beta-22'")
    check("genex23 → 200", _start("genex23", env="genex23,genex-family-beta-22").status_code == 200)
    check("genex-family-beta-22 → 200", _start("genex-family-beta-22").status_code == 200)
    check("' GeNeX23 ' (trim+case) → 200", _start("  GeNeX23  ").status_code == 200)
    check("dev code 'genex' → 403", _start("genex").status_code == 403)
    check("wrong code → 403", _start("nope").status_code == 403)
    check("missing code → 403", _start(None).status_code == 403)


def test_no_code_persisted():
    print("\n── beta code is never persisted in the session doc")
    r = _start("genex23", env="genex23,genex-family-beta-22")
    sid = r.json()["session_id"]
    doc = session_store.load("uid-a", sid)
    blob = json.dumps(doc)
    check("neither code string appears in the stored doc",
          "genex23" not in blob and "genex-family-beta-22" not in blob, "code leaked into doc")
    check("only a boolean authorization flag is stored", doc.get("beta_authorized") is True and "beta_access_code" not in doc)


def test_existing_session_behavior_unchanged():
    print("\n── after start, /answer + /plan work without resending the code")
    os.environ["BETA_ACCESS_CODE"] = "genex23,genex-family-beta-22"
    r = _start("genex23"); sid = r.json()["session_id"]; q = r.json()["current_question"]
    guard = 0
    while q is not None and guard < 60:
        guard += 1
        a = client.post(f"/api/v1/session/{sid}/answer", headers={"Authorization": "Bearer tok"},
                        json={"question_id": q["question_id"], "answer": "yes"}).json()
        if a.get("status") == "interview_complete": break
        q = a.get("current_question")
    plan = client.post(f"/api/v1/session/{sid}/plan", headers={"Authorization": "Bearer tok"})
    check("/plan → 200 without resending the code", plan.status_code == 200, plan.status_code)


def run_all():
    test_parser()
    test_verifier_matrix()
    test_http_prod_style()
    test_no_code_persisted()
    test_existing_session_behavior_unchanged()
    print(f"\n{'='*50}\nResults: {_p} passed, {_f} failed")
    if _f: print("❌ multi-code beta access FAILED"); sys.exit(1)
    print("✅ All multi-code beta access tests PASSED")

if __name__ == "__main__":
    run_all()
