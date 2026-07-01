"""
tests/test_app_config.py — Beta 2.3 dev/prod release-pipeline test

GET /api/v1/app/config returns static, read-only client config (a Progress-tab
rewards preview card). Auth-gated like the rest of /api/v1. No storage, session,
plan, generation, feedback, or genex_core involvement — purely additive.

Run: PYTHONPATH=. python3 tests/test_app_config.py
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

import firebase_admin  # noqa: E402
firebase_admin._apps["[DEFAULT]"] = object()
from firebase_admin import auth as firebase_auth  # noqa: E402
_TOKENS = {"token-user-a": {"uid": "uid-a", "email": "a@example.com"}}
firebase_auth.verify_id_token = lambda t, *a, **k: _TOKENS[t] if t in _TOKENS else (_ for _ in ()).throw(
    firebase_auth.InvalidIdTokenError("bad"))

from fastapi.testclient import TestClient  # noqa: E402
from api.main import app  # noqa: E402

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


def _hdr():
    return {"Authorization": "Bearer token-user-a"}


def test_requires_auth():
    print("\n── /api/v1/app/config is auth-gated")
    r = client.get("/api/v1/app/config")            # no token
    check("no token → 401/403", r.status_code in (401, 403), r.status_code)
    r = client.get("/api/v1/app/config", headers={"Authorization": "Bearer bad"})
    check("bad token → 401/403", r.status_code in (401, 403), r.status_code)


def test_response_shape():
    print("\n── response shape + values")
    r = client.get("/api/v1/app/config", headers=_hdr())
    check("200 OK", r.status_code == 200, r.status_code)
    body = r.json()
    check("app_version == beta-2.3-devprod-test", body.get("app_version") == "beta-2.3-devprod-test", body.get("app_version"))

    p = body.get("progress_rewards_preview")
    check("progress_rewards_preview is an object", isinstance(p, dict), type(p).__name__)
    check("enabled is True (bool)", p.get("enabled") is True, p.get("enabled"))
    check("title non-empty str", isinstance(p.get("title"), str) and p.get("title").strip(), p.get("title"))
    check("message non-empty str", isinstance(p.get("message"), str) and p.get("message").strip(), p.get("message"))

    items = p.get("items")
    check("items is a list of 4", isinstance(items, list) and len(items) == 4, items)
    check("each item has str icon + str label",
          all(isinstance(it, dict) and isinstance(it.get("icon"), str) and it.get("icon")
              and isinstance(it.get("label"), str) and it.get("label") for it in (items or [])),
          items)
    check("labels are Stars/Badges/Milestones/Cups",
          [it.get("label") for it in (items or [])] == ["Stars", "Badges", "Milestones", "Cups"],
          [it.get("label") for it in (items or [])])


def test_read_only_stable():
    print("\n── endpoint is read-only / deterministic")
    r1 = client.get("/api/v1/app/config", headers=_hdr()).json()
    r2 = client.get("/api/v1/app/config", headers=_hdr()).json()
    check("identical across calls (no state)", r1 == r2)
    # exact top-level keys only (no accidental extra fields)
    check("top-level keys exactly {app_version, progress_rewards_preview}",
          set(r1.keys()) == {"app_version", "progress_rewards_preview"}, list(r1.keys()))


def run_all():
    test_requires_auth()
    test_response_shape()
    test_read_only_stable()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ app config tests FAILED")
        sys.exit(1)
    print("✅ All app config tests PASSED")


if __name__ == "__main__":
    run_all()
