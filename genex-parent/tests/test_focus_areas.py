"""
tests/test_focus_areas.py — Beta 2.2 Slice 2a: focus-areas listing + scaffold

Read-only listing of primary / added / remaining focus areas, plus the recomputed
focus block in GET /session and /session/current. No intake, no generation, no LLM.

In-process TestClient; Firebase mocked; local /tmp store; ACTIVITY_MODEL empty.

Run: PYTHONPATH=. python3 tests/test_focus_areas.py
"""

import copy
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
_TOKENS = {
    "token-user-a": {"uid": "uid-a", "email": "a@example.com"},
    "token-user-b": {"uid": "uid-b", "email": "b@example.com"},
}
firebase_auth.verify_id_token = lambda t, *a, **k: _TOKENS[t] if t in _TOKENS else (_ for _ in ()).throw(
    firebase_auth.InvalidIdTokenError("bad"))

from fastapi.testclient import TestClient  # noqa: E402
from api.main import app  # noqa: E402
from api import session_store  # noqa: E402

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


def _start(concern="speech delay and learning difficulty", token="token-user-a"):
    r = client.post("/api/v1/session/start", headers=_hdr(token), json={
        "child_name": "C", "age_years": 3, "age_months": 0, "age_in_months": 36,
        "diagnosis_or_condition": "No known diagnosis / not sure", "parent_concern": concern,
        "daily_time_minutes": 10, "timezone": "UTC", "beta_access_code": "genex"})
    return r.json()["session_id"]


def _fa(sid, token="token-user-a"):
    return client.get(f"/api/v1/session/{sid}/focus-areas", headers=_hdr(token))


def _get(sid, token="token-user-a"):
    return client.get(f"/api/v1/session/{sid}", headers=_hdr(token)).json()


# ── guards ───────────────────────────────────────────────────────────────────
def test_guards():
    print("\n── focus-areas guards")
    sid = _start()
    check("no token → 401", client.get(f"/api/v1/session/{sid}/focus-areas").status_code == 401)
    check("wrong user → 403", _fa(sid, token="token-user-b").status_code == 403)
    check("unknown session → 404",
          client.get("/api/v1/session/nope/focus-areas", headers=_hdr()).status_code == 404)


# ── listing: primary / remaining / recommended ──────────────────────────────
def test_listing_initial():
    print("\n── focus-areas: initial listing (no add-ons)")
    sid = _start("speech delay and learning difficulty")  # primary language, cognitive rec
    r = _fa(sid)
    check("→ 200", r.status_code == 200, r.text[:160])
    b = r.json()
    check("primary = language", b["primary"]["focus_key"] == "language_and_communication", b["primary"])
    check("primary label friendly", b["primary"]["label"] == "Speech & Communication")
    check("added is empty", b["added"] == [], b["added"])
    rem = {x["focus_key"]: x["recommended"] for x in b["remaining"]}
    check("3 remaining (all minus primary)", set(rem) == {"movement_and_physical", "cognitive", "social_and_emotional"}, rem)
    check("cognitive recommended", rem.get("cognitive") is True, rem)
    check("movement not recommended", rem.get("movement_and_physical") is False, rem)
    check("social not recommended", rem.get("social_and_emotional") is False, rem)
    check("remaining uses focus_key key", all("focus_key" in x and "label" in x for x in b["remaining"]))


# ── GET /session + /session/current recompute the focus block ───────────────
def test_focus_block_in_session_views():
    print("\n── GET /session + /session/current focus block (recomputed)")
    sid = _start("speech delay and learning difficulty")
    g = _get(sid)
    f = g["focus"]
    check("focus block present", f.get("primary_focus_key") == "language_and_communication", f)
    check("added_focus_areas == []", f["added_focus_areas"] == [], f["added_focus_areas"])
    rem = {x["key"]: x["recommended"] for x in f["remaining_focus_areas"]}
    check("3 remaining in focus block", len(rem) == 3 and "language_and_communication" not in rem, rem)
    check("cognitive recommended in block", rem.get("cognitive") is True, rem)
    cur = client.get("/api/v1/session/current", headers=_hdr()).json()
    check("/session/current focus block identical", cur.get("focus") == f, cur.get("focus"))


# ── added_focus scaffold present + occupied areas excluded ──────────────────
def test_added_focus_excludes_from_remaining():
    print("\n── added_focus scaffold + occupancy rules")
    sid = _start("speech delay and learning difficulty")
    doc = session_store.load("uid-a", sid)
    check("new session has added_focus = {}", doc.get("added_focus") == {}, doc.get("added_focus"))

    # Inject a READY add-on for cognitive + an ERROR add-on for movement.
    doc.setdefault("added_focus", {})
    doc["added_focus"]["cognitive"] = {"focus_key": "cognitive",
        "focus_label": "Learning, Attention & Thinking", "status": "ready", "module_id": "m1"}
    doc["added_focus"]["movement_and_physical"] = {"focus_key": "movement_and_physical",
        "focus_label": "Fine & Gross Motor & Daily Skills", "status": "error"}
    session_store.save("uid-a", sid, doc)

    b = _fa(sid).json()
    rem = {x["focus_key"] for x in b["remaining"]}
    added = {x["focus_key"]: x["status"] for x in b["added"]}
    check("ready cognitive excluded from remaining", "cognitive" not in rem, rem)
    check("error movement STILL in remaining (re-addable)", "movement_and_physical" in rem, rem)
    check("social still remaining", "social_and_emotional" in rem, rem)
    check("added lists cognitive (ready) + movement (error)",
          added.get("cognitive") == "ready" and added.get("movement_and_physical") == "error", added)
    # GET /session focus block reflects the same
    f = _get(sid)["focus"]
    rem2 = {x["key"] for x in f["remaining_focus_areas"]}
    check("GET /session remaining excludes ready cognitive", "cognitive" not in rem2, rem2)
    check("GET /session added_focus_areas has 2 entries", len(f["added_focus_areas"]) == 2, f["added_focus_areas"])


# ── read-only: primary plan + doc not mutated by listing ────────────────────
def test_read_only_no_mutation():
    print("\n── focus-areas / GET are read-only (no plan/doc mutation)")
    sid = _start("speech delay and learning difficulty")
    before = copy.deepcopy(session_store.load("uid-a", sid))
    for _ in range(3):
        _fa(sid)
        _get(sid)
        client.get("/api/v1/session/current", headers=_hdr())
    after = session_store.load("uid-a", sid)
    check("session doc unchanged by listing", after == before)
    check("plans dict untouched", after.get("plans") == before.get("plans"))
    check("added_focus still {}", after.get("added_focus") == {})


def run_all():
    test_guards()
    test_listing_initial()
    test_focus_block_in_session_views()
    test_added_focus_excludes_from_remaining()
    test_read_only_no_mutation()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ focus-areas tests FAILED")
        sys.exit(1)
    print("✅ All focus-areas tests PASSED")


if __name__ == "__main__":
    run_all()
