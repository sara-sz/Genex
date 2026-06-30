"""
tests/test_plan_customization.py — Beta 2.1 Step 2A

Customization overlay foundation: pure resolver/helper unit tests + GET /session
and feedback-enrichment integration via in-process TestClient. No mutation
endpoints exist yet (this slice is helpers + wiring only).

Run: PYTHONPATH=. python3 tests/test_plan_customization.py
"""

import os
import sys

os.environ["FIREBASE_PROJECT_ID"] = "genex-test"
os.environ["LOCAL_SESSION_FALLBACK"] = "1"
os.environ.pop("GCS_BUCKET", None)
os.environ["REQUIRE_BETA_CODE"] = "true"
os.environ["BETA_ACCESS_CODE"] = "genex"
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:3000")
os.environ.setdefault("ACTIVITY_MODEL", "")
os.environ.pop("CONCERN_ROUTER_MODEL", None)

import firebase_admin  # noqa: E402
firebase_admin._apps["[DEFAULT]"] = object()
from firebase_admin import auth as firebase_auth  # noqa: E402

_TOKENS = {"token-user-a": {"uid": "uid-a", "email": "a@example.com"}}
firebase_auth.verify_id_token = lambda t, *a, **k: _TOKENS[t] if t in _TOKENS else (_ for _ in ()).throw(
    firebase_auth.InvalidIdTokenError("bad"))

from fastapi.testclient import TestClient  # noqa: E402
from api.main import app  # noqa: E402
from api import session_store  # noqa: E402
from api.customization import (  # noqa: E402
    empty_overlay,
    get_overlay,
    is_overlay_empty,
    resolve_plan_response,
    find_overlay_internal,
    is_current_plan,
)

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


# A small synthetic plan_response for pure resolver tests.
def _sample_plan():
    return {
        "session_id": "s", "plan_period": {"plan_id": "p1"},
        "age_in_months": 36, "daily_time_minutes": 10, "daily_card_count": 2,
        "progress_summary": {"activity_count": 3},
        "week": [
            {"day": "Monday", "date": "2026-06-22", "activities": [
                {"id": "a1", "title": "Naming Walk", "domain": "language_and_communication",
                 "instructions": "Walk and name things.", "instructions_steps": ["Walk.", "Name things."]},
                {"id": "a2", "title": "Sock Sort", "domain": "movement_and_physical",
                 "repeat_mode": "harder", "is_repeat": True},
            ]},
            {"day": "Tuesday", "date": "2026-06-23", "activities": [
                {"id": "a3", "title": "Song Fill-In", "domain": "language_and_communication"},
            ]},
        ],
    }


def _all_ids(plan):
    return [a["id"] for d in plan["week"] for a in d["activities"]]


# ── Unit: identity behavior ──────────────────────────────────────────────────
def test_resolve_identity():
    print("\n── resolver identity (no/empty overlay)")
    pr = _sample_plan()
    check("None overlay → same object", resolve_plan_response(pr, None) is pr)
    check("empty overlay → same object", resolve_plan_response(pr, empty_overlay()) is pr)
    # saved_for_later alone does not change the plan → still identity
    ov = empty_overlay(); ov["saved_for_later_activity_ids"] = ["a1"]
    check("saved-only overlay → same object (identity)", resolve_plan_response(pr, ov) is pr)
    check("is_overlay_empty(None) True", is_overlay_empty(None))
    check("is_overlay_empty(empty) True", is_overlay_empty(empty_overlay()))


# ── Unit: removed / override / added ─────────────────────────────────────────
def test_resolve_removed():
    print("\n── resolver hides removed activity; original not mutated")
    pr = _sample_plan()
    ov = empty_overlay(); ov["removed_activity_ids"] = ["a2"]
    res = resolve_plan_response(pr, ov)
    check("a2 hidden in resolved", "a2" not in _all_ids(res), _all_ids(res))
    check("a1 + a3 still present", set(_all_ids(res)) == {"a1", "a3"}, _all_ids(res))
    check("original plan still has a2 (not mutated)", "a2" in _all_ids(pr), _all_ids(pr))
    check("resolved is a different object", res is not pr)


def test_resolve_override():
    print("\n── resolver replaces overridden activity")
    pr = _sample_plan()
    repl = {"id": "a2-swap", "title": "Ball Roll", "domain": "movement_and_physical"}
    ov = empty_overlay()
    ov["activity_overrides"] = {"a2": {"mode": "swapped", "replacement_activity": repl,
                                       "replacement_internal": {"domain": "movement_and_physical"}}}
    res = resolve_plan_response(pr, ov)
    ids = _all_ids(res)
    check("a2 replaced by a2-swap", "a2" not in ids and "a2-swap" in ids, ids)
    check("original unchanged", _all_ids(pr) == ["a1", "a2", "a3"], _all_ids(pr))


def test_resolve_added():
    print("\n── resolver appends added activity to its day")
    pr = _sample_plan()
    card = {"id": "added-1", "title": "Bonus Naming", "domain": "language_and_communication"}
    ov = empty_overlay()
    ov["added_activities"] = [{"activity": card, "internal": {"domain": "language_and_communication"},
                               "day": "Tuesday", "created_at": "now"}]
    res = resolve_plan_response(pr, ov)
    tue = [d for d in res["week"] if d["day"] == "Tuesday"][0]
    check("added card appended to Tuesday", "added-1" in [a["id"] for a in tue["activities"]], tue)
    check("original Tuesday unchanged",
          [a["id"] for a in [d for d in pr["week"] if d["day"] == "Tuesday"][0]["activities"]] == ["a3"])


def test_resolve_preserves_fields():
    print("\n── resolver preserves instructions_steps, repeat_*, and top-level fields")
    pr = _sample_plan()
    ov = empty_overlay(); ov["removed_activity_ids"] = ["a3"]  # remove a different card
    res = resolve_plan_response(pr, ov)
    a1 = [a for d in res["week"] for a in d["activities"] if a["id"] == "a1"][0]
    a2 = [a for d in res["week"] for a in d["activities"] if a["id"] == "a2"][0]
    check("instructions_steps preserved", a1.get("instructions_steps") == ["Walk.", "Name things."])
    check("repeat_mode preserved", a2.get("repeat_mode") == "harder")
    check("is_repeat preserved", a2.get("is_repeat") is True)
    for k in ("session_id", "plan_period", "age_in_months", "daily_time_minutes",
              "daily_card_count", "progress_summary"):
        check(f"top-level '{k}' preserved", res.get(k) == pr.get(k))


# ── Unit: overlay-internal lookup + guard ────────────────────────────────────
def test_find_overlay_internal():
    print("\n── find_overlay_internal (swapped + added)")
    ov = empty_overlay()
    ov["activity_overrides"] = {"a2": {"mode": "swapped",
        "replacement_activity": {"id": "a2-swap"},
        "replacement_internal": {"domain": "movement_and_physical", "subdomain": "gross_motor"}}}
    ov["added_activities"] = [{"activity": {"id": "added-1"},
                               "internal": {"domain": "language_and_communication"}}]
    check("swap internal by ORIGINAL id",
          find_overlay_internal(ov, "a2")["domain"] == "movement_and_physical")
    check("swap internal by REPLACEMENT id",
          find_overlay_internal(ov, "a2-swap")["domain"] == "movement_and_physical")
    check("added internal by card id",
          find_overlay_internal(ov, "added-1")["domain"] == "language_and_communication")
    check("unknown id → None", find_overlay_internal(ov, "nope") is None)


def test_is_current_plan_guard():
    print("\n── is_current_plan guard helper")
    doc = {"current_plan_id": "p1"}
    check("matches current → True", is_current_plan(doc, "p1"))
    check("non-current → False", not is_current_plan(doc, "p2"))
    check("None plan_id → False", not is_current_plan(doc, None))


# ── Integration: GET /session resolution + feedback enrichment ───────────────
def _start_and_plan():
    r = client.post("/api/v1/session/start", headers=_hdr(), json={
        "child_name": "C", "age_years": 3, "age_months": 0, "age_in_months": 36,
        "diagnosis_or_condition": "No known diagnosis / not sure", "parent_concern": "speech delay",
        "daily_time_minutes": 10, "timezone": "UTC", "beta_access_code": "genex"})
    sid = r.json()["session_id"]; q = r.json()["current_question"]
    while q is not None:
        a = client.post(f"/api/v1/session/{sid}/answer", headers=_hdr(),
                        json={"question_id": q["question_id"], "answer": "yes"}).json()
        if a.get("status") == "interview_complete":
            break
        q = a.get("current_question")
    plan = client.post(f"/api/v1/session/{sid}/plan", headers=_hdr()).json()
    return sid, plan


def test_get_session_no_overlay_identity():
    print("\n── GET /session with no overlay == original plan (Beta 2.0 shape)")
    sid, plan = _start_and_plan()
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    # Existing Beta 2.0 keys must all remain (additive keys like plan_acceptance allowed).
    _beta20_keys = {
        "session_id", "status", "age_in_months", "daily_time_minutes",
        "current_plan_id", "plan", "progress_summary", "feedback_summary"}
    check("existing GET keys preserved (no removal/rename)",
          _beta20_keys.issubset(g.keys()), sorted(g.keys()))
    # Beta 2.2: the legacy `plan` field is the balanced display plan (provenance-
    # stamped; no add-ons → same cards in the same order). Compare activity identity.
    g_ids = [[a.get("id") for a in d["activities"]] for d in g["plan"]["week"]]
    p_ids = [[a.get("id") for a in d["activities"]] for d in plan["week"]]
    check("plan shows original activities", g_ids == p_ids, "plan differs from POST /plan")
    # old session has the key but empty
    doc = session_store.load("uid-a", sid)
    check("doc has plan_customizations (empty)", doc.get("plan_customizations") == {}, doc.get("plan_customizations"))


def test_get_session_with_overlay_resolves_and_preserves_original():
    print("\n── GET /session with overlay resolves; stored plan_response unchanged")
    sid, plan = _start_and_plan()
    pid = plan["plan_period"]["plan_id"]
    first_act = plan["week"][0]["activities"][0]
    removed_id = first_act["id"]
    added_card = {"id": "ovl-added-1", "title": "Parent Bonus", "domain": "language_and_communication",
                  "instructions": "A small extra.", "instructions_steps": ["Do it."]}

    doc = session_store.load("uid-a", sid)
    original_snapshot = [a["id"] for d in doc["plans"][pid]["plan_response"]["week"] for a in d["activities"]]
    doc.setdefault("plan_customizations", {})[pid] = {
        "removed_activity_ids": [removed_id],
        "saved_for_later_activity_ids": [],
        "activity_overrides": {},
        "added_activities": [{"activity": added_card,
                              "internal": {"domain": "language_and_communication"},
                              "day": plan["week"][0]["day"], "created_at": "now"}],
    }
    session_store.save("uid-a", sid, doc)

    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    res_ids = [a["id"] for d in g["plan"]["week"] for a in d["activities"]]
    check("removed activity hidden in GET /session", removed_id not in res_ids, res_ids)
    check("added activity present in GET /session", "ovl-added-1" in res_ids, res_ids)
    # original stored plan_response untouched
    doc2 = session_store.load("uid-a", sid)
    after_snapshot = [a["id"] for d in doc2["plans"][pid]["plan_response"]["week"] for a in d["activities"]]
    check("stored plan_response NOT mutated", after_snapshot == original_snapshot, after_snapshot)
    check("removed id still in stored original", removed_id in after_snapshot)


def test_feedback_lookup_original_and_overlay():
    print("\n── feedback enrichment: original (plan_internal) + overlay (added) activity")
    sid, plan = _start_and_plan()
    pid = plan["plan_period"]["plan_id"]
    day0 = plan["week"][0]["day"]; date0 = plan["week"][0]["activities"][0]["activity_date"]
    orig_act = plan["week"][0]["activities"][0]

    # Original activity → enriched via frozen plan_internal
    r1 = client.post(f"/api/v1/session/{sid}/feedback", headers=_hdr(), json={
        "plan_id": pid, "activity_id": orig_act["id"], "day": day0, "activity_date": date0,
        "enjoyment": "loved_it", "difficulty": "just_right", "completion": "did_it"})
    check("original-activity feedback metadata_found", r1.json().get("metadata_found") is True, r1.text[:200])

    # Inject an overlay-added activity, then feedback on it → enriched via overlay internal
    added_card = {"id": "ovl-fb-1", "title": "Overlay Card", "domain": "movement_and_physical",
                  "activity_date": date0}
    doc = session_store.load("uid-a", sid)
    doc.setdefault("plan_customizations", {})[pid] = {
        "removed_activity_ids": [], "saved_for_later_activity_ids": [], "activity_overrides": {},
        "added_activities": [{"activity": added_card,
                              "internal": {"domain": "movement_and_physical", "subdomain": "gross_motor",
                                           "activity_family": "ball_play"},
                              "day": day0, "created_at": "now"}]}
    session_store.save("uid-a", sid, doc)
    r2 = client.post(f"/api/v1/session/{sid}/feedback", headers=_hdr(), json={
        "plan_id": pid, "activity_id": "ovl-fb-1", "day": day0, "activity_date": date0,
        "enjoyment": "it_was_okay", "difficulty": "just_right", "completion": "did_it"})
    check("overlay-activity feedback metadata_found", r2.json().get("metadata_found") is True, r2.text[:200])
    # Confirm the saved record carries the overlay domain (report routing works)
    doc2 = session_store.load("uid-a", sid)
    rec = [f for f in doc2["feedback"] if f.get("activity_id") == "ovl-fb-1"][0]
    check("overlay feedback enriched with domain", rec.get("domain") == "movement_and_physical", rec.get("domain"))


def run_all():
    test_resolve_identity()
    test_resolve_removed()
    test_resolve_override()
    test_resolve_added()
    test_resolve_preserves_fields()
    test_find_overlay_internal()
    test_is_current_plan_guard()
    test_get_session_no_overlay_identity()
    test_get_session_with_overlay_resolves_and_preserves_original()
    test_feedback_lookup_original_and_overlay()

    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ plan customization tests FAILED")
        sys.exit(1)
    print("✅ All plan customization tests PASSED")


if __name__ == "__main__":
    run_all()
