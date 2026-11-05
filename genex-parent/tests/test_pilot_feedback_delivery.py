"""
tests/test_pilot_feedback_delivery.py — P0-A2 Parent→Pilot feedback handoff

The repo-side half of the handoff. The Pilot call itself is made by the
browser, because the Pilot resolves the caregiver from its own verified token
and the two Firebase directories are deliberately unfederated — so what is
provable HERE is everything except the network call.

Two DISJOINT request shapes:

  POST …/feedback         standalone Parent feedback. Unchanged.
  POST …/pilot-feedback   an activity from a PILOT released week: a cycle, an
                          opaque handle and a practice date. NO Parent plan,
                          activity UUID or weekday — and this suite proves a
                          session with no plan at all can use it.

Dependency-pure: local session fallback, no GCS, no Firestore, no Pilot.

Proofs:
   1  the Pilot route needs NO Parent plan, activity id, day, or even a plan
   2  it refuses Parent plan identity outright, rather than ignoring it
   3  the three Pilot fields are each required; the ref shape is checked
   4  standalone feedback is unchanged — stars, completions, no ledger row
   5  the legacy fingerprint is byte-identical to the pre-P0-A2 hash
   6  a linked record mints exactly one pending row in the same write
   7  the Parent-side identity is the Pilot triple, never the handle as a UUID
   8  enjoyment maps loved_it/it_was_okay/not_really → enjoyed/neutral/disliked
   9  local_date is the ACTIVITY date, never the submission date
  10  the observation body carries no note, no caregiver id, no internal ref
  11  feedback_id → source_feedback_id, supersedes → supersedes_source_…
  12  an exact replay returns the same lineage and the SAME single row
  13  a revision keeps BOTH rows and supersedes within the same context
  14  a DIFFERENT cycle never supersedes the same handle and date
  15  a DIFFERENT practice date never supersedes
  16  standalone and Pilot-linked records never supersede each other
  17  no star, attempt or completion is manufactured for a linked record
  18  confirm requires a receipt and moves pending → delivered
  19  confirm with the SAME receipt is a replay; the first one stands
  20  confirm with a DIFFERENT receipt is a conflict
  21  a linked record with no delivery row FAILS CLOSED
  22  confirm forbids clinical values and caregiver identity
  23  confirm 404s an unknown record and 409s an unlinked one
  24  both delivery surfaces require session ownership
  25  the pending read hides the note, Parent internals and Pilot internals
  26  Pilot-linked feedback never enters Parent next-week adaptation
  27  a Pilot-linked did_it counts as ENGAGEMENT and never as Parent-plan
      progress: no completed_activities entry, no card completed, no
      progress_summary change — while standalone feedback still does all three

Run: PYTHONPATH=. python3 tests/test_pilot_feedback_delivery.py
"""
import hashlib
import json
import os
import sys

os.environ.update(FIREBASE_PROJECT_ID="genex-test", LOCAL_SESSION_FALLBACK="1",
    REQUIRE_BETA_CODE="true", BETA_ACCESS_CODE="genex", ALLOWED_ORIGINS="http://localhost:3000",
    ACTIVITY_MODEL="")
os.environ.pop("GCS_BUCKET", None); os.environ.pop("CONCERN_ROUTER_MODEL", None)
import shutil; shutil.rmtree("/tmp/genex_api_sessions", ignore_errors=True)
import firebase_admin; firebase_admin._apps["[DEFAULT]"] = object()
from firebase_admin import auth as fa
_T = {"tok": {"uid": "uid-a", "email": "a@x.com"},
      "tok-b": {"uid": "uid-b", "email": "b@x.com"}}
fa.verify_id_token = lambda t, *a, **k: _T[t] if t in _T else (_ for _ in ()).throw(fa.InvalidIdTokenError("bad"))
from fastapi.testclient import TestClient
from api.main import app
from api import pipeline
from api import progress as progress_lib
from api import session_store

client = TestClient(app)
H = {"Authorization": "Bearer tok"}
HB = {"Authorization": "Bearer tok-b"}
_p = _f = 0


def check(label, ok, detail=""):
    global _p, _f
    if ok:
        _p += 1; print(f"  ✓ {label}")
    else:
        _f += 1; print(f"  ✗ FAIL: {label} — {detail}")


def P(path, body=None, headers=H):
    return client.post(path, headers=headers, json=body)


def G(path, headers=H):
    return client.get(path, headers=headers)


CYCLE = "wcyc_fictional_week_one"
CYCLE_B = "wcyc_fictional_week_two"
REF = "pact1:" + "a" * 32
REF2 = "pact1:" + "b" * 32
DATE = "2026-10-13"
DATE2 = "2026-10-14"


def start_session(headers=H):
    """A Parent session and NOTHING else. No intake, no plan, no activities.

    This is the provider-connected shape: the family's week comes from the
    Pilot, so nothing in the Parent planning system has to exist first.
    """
    return P("/api/v1/session/start", {"child_name": "R", "age_years": 4,
        "age_months": 0, "age_in_months": 48, "diagnosis_or_condition": "ADHD",
        "parent_concern": "ADHD, lack of attention", "daily_time_minutes": 10,
        "timezone": "America/Los_Angeles", "beta_access_code": "genex"},
        headers=headers).json()["session_id"]


def planned_session(headers=H):
    """A session with a real Parent plan, for the standalone-mode proofs."""
    sid = start_session(headers)
    q = G(f"/api/v1/session/{sid}", headers=headers).json().get("current_question")
    while q:
        a = P(f"/api/v1/session/{sid}/answer",
              {"question_id": q["question_id"], "answer": "with_help"},
              headers=headers).json()
        if a.get("status") == "interview_complete":
            break
        q = a.get("current_question")
    P(f"/api/v1/session/{sid}/plan", headers=headers)
    plan = G("/api/v1/session/current", headers=headers).json()["plan"]
    card = next(a for d in plan["week"] for a in d["activities"]
                if a["source"] == "primary")
    day = next(d["day"] for d in plan["week"] for a in d["activities"]
               if a["activity_id"] == card["activity_id"])
    return sid, {"plan_id": card["plan_id"], "activity_id": card["activity_id"],
                 "day": day, "activity_date": card["activity_date"]}


def pfb(sid, headers=H, **over):
    body = {"pilot_cycle_id": CYCLE, "pilot_activity_ref": REF,
            "activity_date": DATE, "enjoyment": "loved_it",
            "difficulty": "just_right", "completion": "did_it"}
    body.update(over)
    return P(f"/api/v1/session/{sid}/pilot-feedback", body, headers=headers)


def fb(sid, base, headers=H, **over):
    body = {"plan_id": base["plan_id"], "activity_id": base["activity_id"],
            "day": base["day"], "activity_date": base["activity_date"],
            "enjoyment": "loved_it", "difficulty": "just_right",
            "completion": "did_it"}
    body.update(over)
    return P(f"/api/v1/session/{sid}/feedback", body, headers=headers)


def doc(sid, uid="uid-a"):
    return session_store.load(uid, sid)


def ev(d, kind):
    return [e for e in d.get("events", []) if e["type"] == kind]


print("\n" + "=" * 50)
print("P0-A2 — Parent→Pilot feedback handoff")
print("=" * 50)

# ── 1–3. the provider-connected shape needs no Parent planning ─────────────
print("\n── a provider-connected submission carries NO Parent plan identity")
bare = start_session()
d = doc(bare)
check("the session genuinely has no plan",
      not d.get("current_plan_id") and not d.get("plans"),
      repr(d.get("current_plan_id")))
check("standalone feedback on it 409s — there is no plan to report on",
      P(f"/api/v1/session/{bare}/feedback",
        {"plan_id": "x", "activity_id": "y", "day": "Monday",
         "activity_date": DATE, "enjoyment": "loved_it",
         "difficulty": "just_right", "completion": "did_it"}).status_code == 409)
r = pfb(bare)
check("…but Pilot-linked feedback is accepted", r.status_code == 200,
      r.text[:200])
linked_fid = r.json()["feedback_id"]

for bad in ({"plan_id": "plan-1"}, {"activity_id": "uuid-1"},
            {"day": "Tuesday"}, {"local_submission_date": DATE},
            {"owner_uid": "uid-b"}, {"feedback_mode": "standalone"},
            {"activity_instance_ref": "actinst_real"}):
    r = pfb(bare, **bad)
    check(f"refused Parent/internal field: {sorted(bad)[0]}",
          r.status_code == 422, f"{r.status_code} {r.text[:80]}")

for missing in ("pilot_cycle_id", "pilot_activity_ref", "activity_date",
                "enjoyment", "difficulty", "completion"):
    body = {"pilot_cycle_id": CYCLE, "pilot_activity_ref": REF,
            "activity_date": DATE, "enjoyment": "loved_it",
            "difficulty": "just_right", "completion": "did_it"}
    body.pop(missing)
    check(f"required: {missing}",
          P(f"/api/v1/session/{bare}/pilot-feedback", body).status_code == 422)
check("an internal-looking ref is refused",
      pfb(bare, pilot_activity_ref="actinst_internal").status_code == 422)
check("a non-canonical ref digest is refused",
      pfb(bare, pilot_activity_ref="pact1:" + "A" * 32).status_code == 422)

# ── 4. standalone is unchanged ─────────────────────────────────────────────
print("\n── standalone Parent feedback is unchanged")
sid, card = planned_session()
r = fb(sid, card, note="she loved the bubbles")
body = r.json()
check("accepted", r.status_code == 200, r.text[:160])
check("a star was awarded", body.get("star_awarded") is True, repr(body))
check("a completion was created", bool(body.get("completion_id")))
check("an attempt was created", bool(body.get("attempt_id")))
check("the response has no pilot_delivery key at all",
      "pilot_delivery" not in body, sorted(body))
check("supersedes_feedback_id is returned (additive, null here)",
      "supersedes_feedback_id" in body, sorted(body))
d = doc(sid)
check("no delivery row was created", not d.get("pilot_deliveries"),
      repr(d.get("pilot_deliveries")))
check("no delivery event was emitted", not ev(d, "pilot_delivery_pending"))
check("the record declares itself standalone",
      d["feedback"][-1]["feedback_mode"] == "standalone")
check("it carries no Pilot fields",
      not any(k.startswith("pilot_") for k in d["feedback"][-1]),
      [k for k in d["feedback"][-1] if k.startswith("pilot_")])
standalone_fid = body["feedback_id"]

# ── 5. the legacy fingerprint did not move ─────────────────────────────────
print("\n── backward compatibility of the dedupe fingerprint")
legacy = hashlib.sha256("|".join([
    "fb1", "sess", "act", DATE, "did_it", "loved_it", "just_right",
    "False", "", "", "",
]).encode("utf-8")).hexdigest()
now_key = progress_lib.feedback_req_key(
    "sess", "act", DATE, "did_it", "loved_it", "just_right",
    False, None, None, "")
check("an unlinked key is byte-identical to the pre-P0-A2 hash",
      now_key == legacy, f"{now_key} != {legacy}")
check("adding the Pilot link changes the key",
      progress_lib.feedback_req_key(
          "sess", "act", DATE, "did_it", "loved_it", "just_right",
          False, None, None, "", pilot_cycle_id=CYCLE,
          pilot_activity_ref=REF) != legacy)

# ── 6–7. one pending row, and the Parent-side identity ─────────────────────
print("\n── the linked record, its identity and its queued delivery")
d = doc(bare)
rec = next(f for f in d["feedback"] if f["feedback_id"] == linked_fid)
identity = progress_lib.pilot_feedback_identity(CYCLE, REF, DATE)
check("the record declares itself pilot_linked",
      rec["feedback_mode"] == "pilot_linked")
check("it stores the real Pilot triple",
      (rec["pilot_cycle_id"], rec["pilot_activity_ref"], rec["activity_date"])
      == (CYCLE, REF, DATE))
check("the Parent-side activity identity is derived from that triple",
      rec["activity_id"] == rec["pilot_feedback_identity"] == identity,
      repr(rec["activity_id"]))
check("the handle is NOT reinterpreted as a Parent activity id",
      rec["activity_id"] != REF and not rec["activity_id"].startswith("pact1:"))
# The identity must move when ANY of the three components moves. Asserted
# component by component: a derivation that quietly dropped one would give two
# different practices one Parent-side activity identity, and every reader that
# groups by it would then be reading them as the same thing.
_base = progress_lib.pilot_feedback_identity(CYCLE, REF, DATE)
check("the identity is sensitive to the cycle",
      progress_lib.pilot_feedback_identity(CYCLE_B, REF, DATE) != _base)
check("the identity is sensitive to the activity",
      progress_lib.pilot_feedback_identity(CYCLE, REF2, DATE) != _base)
check("the identity is sensitive to the PRACTICE DATE",
      progress_lib.pilot_feedback_identity(CYCLE, REF, DATE2) != _base)
check("it is deterministic",
      progress_lib.pilot_feedback_identity(CYCLE, REF, DATE) == _base)
for incomplete in ((("", REF, DATE)), ((CYCLE, "", DATE)), ((CYCLE, REF, ""))):
    try:
        progress_lib.pilot_feedback_identity(*incomplete)
        check("an incomplete triple cannot produce an identity", False,
              repr(incomplete))
    except progress_lib.PilotPayloadError:
        check("an incomplete triple cannot produce an identity", True)
check("no Parent plan identity was invented",
      "plan_id" not in rec and "day" not in rec,
      [k for k in rec if k in ("plan_id", "day")])
check("no plan metadata was manufactured",
      rec["metadata_found"] is False
      and not any(k in rec for k in ("domain", "activity_family", "module_id",
                                     "source", "focus_key", "plan_period_id")),
      [k for k in rec if k in ("domain", "module_id", "source")])
check("the submission date is still recorded, for audit",
      bool(rec["local_submission_date"]))
check("exactly one delivery row", len(d["pilot_deliveries"]) == 1)
check("the index points at it",
      d["pilot_delivery_index"][linked_fid]
      == d["pilot_deliveries"][0]["delivery_id"])
check("it is pending", d["pilot_deliveries"][0]["status"] == "pending")
check("one pilot_feedback_recorded event",
      len(ev(d, "pilot_feedback_recorded")) == 1)
check("one pending event", len(ev(d, "pilot_delivery_pending")) == 1)
check("the legacy feedback_recorded type was NOT used",
      not ev(d, "feedback_recorded"))
pend_ev = ev(d, "pilot_delivery_pending")[0]
check("the pending event carries references, not clinical values",
      set(pend_ev["metadata"]) == {"feedback_id", "pilot_cycle_id",
                                   "supersedes_feedback_id"},
      repr(pend_ev["metadata"]))

# ── 8–11. the one authoritative mapping ────────────────────────────────────
print("\n── the authoritative mapping (dependency-pure)")
for parent_word, pilot_word in (("loved_it", "enjoyed"), ("it_was_okay", "neutral"),
                                ("not_really", "disliked")):
    payload = progress_lib.pilot_observation_payload({
        "feedback_id": "fb_x", "pilot_cycle_id": CYCLE, "pilot_activity_ref": REF,
        "activity_date": DATE, "local_submission_date": "2026-10-16",
        "completion": "did_it", "difficulty": "just_right", "enjoyment": parent_word,
        "note": "do not travel", "owner_uid": "uid-a",
        "care_team_tags": ["st"],
    })
    check(f"{parent_word} → {pilot_word}",
          payload["body"]["enjoyment"] == pilot_word, repr(payload["body"]))

payload = progress_lib.pilot_observation_payload({
    "feedback_id": "fb_x", "supersedes_feedback_id": "fb_w",
    "pilot_cycle_id": CYCLE, "pilot_activity_ref": REF,
    "activity_date": DATE, "local_submission_date": "2026-10-16",
    "completion": "wasnt_ready_yet", "difficulty": "too_hard", "enjoyment": "not_really",
    "note": "do not travel", "owner_uid": "uid-a", "activity_id": "uuid-1",
})
b = payload["body"]
check("local_date is the ACTIVITY date, not the submission date",
      b["local_date"] == DATE, repr(b["local_date"]))
check("completion passes through as attempt_outcome",
      b["attempt_outcome"] == "wasnt_ready_yet", repr(b["attempt_outcome"]))
check("feedback_id → source_feedback_id", b["source_feedback_id"] == "fb_x")
check("supersedes_feedback_id → supersedes_source_feedback_id",
      b.get("supersedes_source_feedback_id") == "fb_w", repr(b))
check("the body is exactly the Pilot's seven fields",
      set(b) == {"activity_ref", "local_date", "attempt_outcome", "difficulty",
                 "enjoyment", "source_feedback_id", "supersedes_source_feedback_id"},
      repr(sorted(b)))
flat = repr(payload).lower()
check("no note travels", "do not travel" not in flat)
check("no caregiver identity travels", "uid-a" not in flat)
check("no Parent internal activity identity travels", "uuid-1" not in flat)
check("no internal activity_instance_ref field exists",
      "activity_instance_ref" not in flat)
for bad in ({}, {"feedback_id": "fb_x"},
            {"feedback_id": "fb_x", "pilot_cycle_id": CYCLE,
             "pilot_activity_ref": REF, "activity_date": ""}):
    try:
        progress_lib.pilot_observation_payload(bad)
        check("an unlinked record cannot produce an observation", False, repr(bad))
    except progress_lib.PilotPayloadError:
        check("an unlinked/incomplete record cannot produce an observation", True)

# ── 12. exact replay ──────────────────────────────────────────────────────
print("\n── an exact replay recovers the whole handoff and adds nothing")
first_view = next(f for f in doc(bare)["pilot_deliveries"])
r = pfb(bare)
rb = r.json()
check("replay flagged", rb.get("idempotent_replay") is True, repr(rb)[:200])
check("the same feedback_id", rb["feedback_id"] == linked_fid)
check("the same lineage", rb.get("supersedes_feedback_id") is None)
check("the same delivery row",
      rb["pilot_delivery"]["delivery_id"] == first_view["delivery_id"])
d = doc(bare)
check("still exactly one delivery row", len(d["pilot_deliveries"]) == 1)
check("still exactly one pending event", len(ev(d, "pilot_delivery_pending")) == 1)

# ── 13–16. revision identity stays inside its source context ──────────────
print("\n── revision identity is the same cycle + handle + practice date")
r = pfb(bare, completion="didnt_want_to_try", enjoyment="not_really")
rev = r.json()
rev_fid = rev["feedback_id"]
check("a new feedback_id", rev_fid != linked_fid)
check("it supersedes the earlier record in the SAME context",
      rev.get("supersedes_feedback_id") == linked_fid,
      repr(rev.get("supersedes_feedback_id")))
check("the observation mirrors the supersession",
      rev["pilot_delivery"]["observation_body"]
      .get("supersedes_source_feedback_id") == linked_fid)
d = doc(bare)
check("two delivery rows now exist", len(d["pilot_deliveries"]) == 2)
check("the earlier row was not rewritten",
      d["pilot_deliveries"][0]["delivery_id"] == first_view["delivery_id"])
check("the earlier feedback record is preserved",
      any(f["feedback_id"] == linked_fid for f in d["feedback"]))

r = pfb(bare, pilot_cycle_id=CYCLE_B).json()
check("a DIFFERENT cycle does not supersede the same handle and date",
      r["supersedes_feedback_id"] is None, repr(r["supersedes_feedback_id"]))
cycle_b_fid = r["feedback_id"]
r = pfb(bare, activity_date=DATE2).json()
check("a DIFFERENT practice date does not supersede",
      r["supersedes_feedback_id"] is None, repr(r["supersedes_feedback_id"]))
_by_id = {f["feedback_id"]: f for f in doc(bare)["feedback"]}
check("and the two days are two STORED activity identities",
      _by_id[r["feedback_id"]]["activity_id"] != _by_id[linked_fid]["activity_id"],
      repr(_by_id[r["feedback_id"]]["activity_id"]))
r = pfb(bare, pilot_activity_ref=REF2).json()
check("a DIFFERENT activity does not supersede",
      r["supersedes_feedback_id"] is None, repr(r["supersedes_feedback_id"]))
check("the identities differ across cycles",
      progress_lib.pilot_feedback_identity(CYCLE, REF, DATE)
      != progress_lib.pilot_feedback_identity(CYCLE_B, REF, DATE))

print("\n── the two modes never supersede each other")
# A standalone record whose Parent activity_id IS the derived identity, on the
# same dates: the only remaining way the two could be confused.
csid, ccard = planned_session()
ident = progress_lib.pilot_feedback_identity(CYCLE, REF, ccard["activity_date"])
# BOTH kinds of record now exist in one session, on the same dates — the only
# remaining way the two modes could be confused for one another.
standalone_in_planned = fb(csid, ccard).json()["feedback_id"]
r = pfb(csid, activity_date=ccard["activity_date"]).json()
pilot_in_planned = r["feedback_id"]
check("a Pilot-linked record does not supersede the standalone one beside it",
      r["supersedes_feedback_id"] is None, repr(r["supersedes_feedback_id"]))
r2 = fb(csid, ccard, enjoyment="not_really").json()
check("the standalone revision supersedes the STANDALONE record",
      r2["supersedes_feedback_id"] == standalone_in_planned,
      repr(r2["supersedes_feedback_id"]))
r3 = pfb(csid, activity_date=ccard["activity_date"],
         completion="wasnt_ready_yet").json()
check("the Pilot revision supersedes the PILOT record",
      r3["supersedes_feedback_id"] == pilot_in_planned,
      repr(r3["supersedes_feedback_id"]))
prior = progress_lib.find_prior_feedback(
    doc(csid)["feedback"], mode=progress_lib.FEEDBACK_MODE_STANDALONE,
    activity_id=ident, local_date=ccard["activity_date"],
    activity_date=ccard["activity_date"])
check("the matcher refuses to return a pilot record to a standalone query",
      prior is None, repr(prior))

# ── 17. nothing is manufactured ───────────────────────────────────────────
print("\n── no reward or completion record is manufactured")
d = doc(bare)
check("no attempts at all", not d.get("attempts"), repr(d.get("attempts")))
check("no completions at all", not d.get("completions"))
check("no stars were awarded", not ev(d, "star_awarded"))
check("no activity_completed events", not ev(d, "activity_completed"))
check("the response reports no star/attempt/completion ids",
      not ({"star_awarded", "attempt_id", "completion_id"} & set(rev)),
      sorted(rev))

# ── 18–21. confirmation ───────────────────────────────────────────────────
print("\n── confirmation requires a receipt and is idempotent")
CONF = f"/api/v1/session/{bare}/pilot-feedback-deliveries"
check("a confirm with no body is refused",
      P(f"{CONF}/{linked_fid}/confirm", {}).status_code == 422)
check("a blank receipt is refused",
      P(f"{CONF}/{linked_fid}/confirm",
        {"pilot_observation_event_id": "   "}).status_code == 422)
r = P(f"{CONF}/{linked_fid}/confirm", {"pilot_observation_event_id": "obsv_1"})
cb = r.json()
check("accepted with a receipt", r.status_code == 200, r.text[:160])
check("status is delivered", cb.get("status") == "delivered", repr(cb))
check("the receipt is stored", cb.get("pilot_observation_event_id") == "obsv_1")
first_time = cb.get("delivered_at_utc")
check("a delivery time was stamped", bool(first_time))
d = doc(bare)
check("exactly one confirmed event", len(ev(d, "pilot_delivery_confirmed")) == 1)
check("the confirmed event carries references only",
      set(ev(d, "pilot_delivery_confirmed")[0]["metadata"])
      == {"feedback_id", "pilot_cycle_id", "pilot_observation_event_id"})

r = P(f"{CONF}/{linked_fid}/confirm", {"pilot_observation_event_id": "obsv_1"})
cb2 = r.json()
check("the SAME receipt replays", r.status_code == 200
      and cb2.get("idempotent_replay") is True, f"{r.status_code} {cb2}")
check("the first delivery time stands", cb2.get("delivered_at_utc") == first_time)
r = P(f"{CONF}/{linked_fid}/confirm", {"pilot_observation_event_id": "obsv_OTHER"})
check("a DIFFERENT receipt is a conflict", r.status_code == 409, r.text[:120])
check("it says why", "receipt_conflict" in r.text, r.text[:120])
d = doc(bare)
row = next(x for x in d["pilot_deliveries"] if x["feedback_id"] == linked_fid)
check("the first receipt was not overwritten",
      row["pilot_observation_event_id"] == "obsv_1")
check("the first delivery time was not moved",
      row["delivered_at_utc"] == first_time)
check("still exactly one confirmed event", len(ev(d, "pilot_delivery_confirmed")) == 1)

print("\n── a linked record with no delivery row fails closed")
corrupt = session_store.load("uid-a", bare)
victim = next(x for x in corrupt["pilot_deliveries"]
              if x["feedback_id"] == cycle_b_fid)
corrupt["pilot_deliveries"] = [x for x in corrupt["pilot_deliveries"]
                               if x["delivery_id"] != victim["delivery_id"]]
corrupt["pilot_delivery_index"].pop(cycle_b_fid)
session_store.save("uid-a", bare, corrupt)
r = P(f"{CONF}/{cycle_b_fid}/confirm", {"pilot_observation_event_id": "obsv_z"})
check("it is an invariant conflict, not a silent repair",
      r.status_code == 409, f"{r.status_code} {r.text[:120]}")
check("it says which invariant", "row_missing" in r.text, r.text[:120])
after = doc(bare)
check("nothing was written", cycle_b_fid not in after["pilot_delivery_index"])
check("no row was minted",
      not any(x["feedback_id"] == cycle_b_fid
              for x in after["pilot_deliveries"]))

# ── 22–23. the confirm body and the refusals ──────────────────────────────
print("\n── confirmation cannot carry clinical values or identity")
for bad in ({"completion": "did_it"}, {"enjoyment": "loved_it"},
            {"difficulty": "too_easy"}, {"activity_date": DATE},
            {"recorded_by_caregiver_id": "pilot-uid-1"},
            {"owner_uid": "uid-b"}, {"status": "delivered"},
            {"supersedes_feedback_id": "fb_other"}):
    body = {"pilot_observation_event_id": "obsv_x"}
    body.update(bad)
    r = P(f"{CONF}/{rev_fid}/confirm", body)
    check(f"refused: {sorted(bad)[0]}", r.status_code == 422,
          f"{r.status_code} {r.text[:80]}")
d = doc(bare)
check("the clinical values are exactly as the parent gave them",
      [f["completion"] for f in d["feedback"] if f["feedback_id"] == rev_fid]
      == ["didnt_want_to_try"])

print("\n── confirmation refusals")
r = P(f"{CONF}/fb_does_not_exist/confirm", {"pilot_observation_event_id": "o"})
check("an unknown feedback id → 404", r.status_code == 404, r.text[:120])
sa_conf = f"/api/v1/session/{sid}/pilot-feedback-deliveries"
r = P(f"{sa_conf}/{standalone_fid}/confirm",
      {"pilot_observation_event_id": "o"})
check("a standalone record → 409", r.status_code == 409, r.text[:120])
check("it says why", "not_pilot_linked" in r.text, r.text[:120])
r = P(f"{CONF}/{linked_fid}/confirm", {"pilot_observation_event_id": "o"},
      headers=HB)
check("another caregiver's session is refused", r.status_code in (403, 404),
      str(r.status_code))
r = client.post(f"{CONF}/{linked_fid}/confirm",
                json={"pilot_observation_event_id": "o"})
check("an unauthenticated confirm is refused", r.status_code in (401, 403),
      str(r.status_code))
check("another caregiver cannot read the queue",
      G(CONF, headers=HB).status_code in (403, 404))
check("another caregiver cannot post pilot feedback",
      pfb(bare, headers=HB).status_code in (403, 404))

# ── 25. the pending read ──────────────────────────────────────────────────
print("\n── the pending read exposes delivery facts and nothing else")
r = pfb(bare, activity_date="2026-10-15", note="another private note")
third_fid = r.json()["feedback_id"]
pend = G(CONF).json()
check("the delivered row is not pending",
      all(x["feedback_id"] != linked_fid for x in pend["deliveries"]),
      repr([x["feedback_id"] for x in pend["deliveries"]]))
row = next(x for x in pend["deliveries"] if x["feedback_id"] == third_fid)
check("the exposed keys are exactly the delivery contract",
      set(row) == {"delivery_id", "feedback_id", "supersedes_feedback_id",
                   "pilot_cycle_id", "pilot_activity_ref", "activity_date",
                   "attempt_outcome", "difficulty", "enjoyment", "status",
                   "observation_body"},
      repr(sorted(row)))
flat = repr(pend).lower()
check("no note is exposed", "private note" not in flat)
check("no Parent plan internals are exposed",
      not any(k in flat for k in ("plan_id", "module_id", "provenance",
                                  "care_team", "snapshot", "milestone",
                                  "local_submission_date")))
check("no Pilot internal identity is exposed", "activity_instance_ref" not in flat)
check("no Parent-side derived identity is exposed", "pfbi_" not in flat)
check("the delivered filter shows the delivered one",
      G(f"{CONF}?status=delivered").json()["count"] == 1)
check("the all filter shows every row",
      G(f"{CONF}?status=all").json()["count"]
      == len(doc(bare)["pilot_deliveries"]))
check("a bogus filter is refused", G(f"{CONF}?status=whatever").status_code == 422)

# ── 26. Parent planning never consumes Pilot-linked evidence ──────────────
print("\n── Pilot-linked feedback never reaches Parent next-week adaptation")
plan_doc = doc(csid)
plan_entry = (plan_doc.get("plans") or {}).get(plan_doc["current_plan_id"], {})
translated = pipeline.translate_feedback_to_activity_feedback(
    plan_doc.get("feedback") or [], plan_entry.get("plan_response") or {},
    plan_doc["current_plan_id"])
linked_records = [f for f in plan_doc["feedback"]
                  if progress_lib.feedback_mode_of(f) == "pilot_linked"]
check("the session really does hold Pilot-linked records",
      len(linked_records) >= 2, str(len(linked_records)))
check("none of their identities appears in the translated signal",
      not any(rec["activity_id"] in repr(translated)
              for rec in linked_records), repr(translated)[:160])
check("the standalone feedback DID translate", bool(translated), repr(translated))

# ── 27. the Parent-progress boundary ──────────────────────────────────────
#
# The founder-approved line: a Pilot-linked `did_it` MAY count as engagement,
# and MUST NOT appear anywhere that means Parent-plan progress.
print("\n── a Pilot-linked did_it is engagement, never Parent-plan progress")
from api.main import _build_session_view

psid, pcard = planned_session()
before = _build_session_view(doc(psid), psid)
before_summary = before["feedback_summary"]
before_progress = json.dumps(before.get("progress_summary"), sort_keys=True)
before_cards = json.dumps(
    [[c.get("activity_id"), c.get("completed")]
     for day in (before.get("plan") or {}).get("week", [])
     for c in day.get("activities", [])], sort_keys=True)

r = pfb(psid, activity_date=pcard["activity_date"], completion="did_it")
check("the Pilot-linked did_it was accepted", r.status_code == 200, r.text[:160])
pilot_identity = progress_lib.pilot_feedback_identity(
    CYCLE, REF, pcard["activity_date"])

after = _build_session_view(doc(psid), psid)
after_summary = after["feedback_summary"]

# MAY: the generic engagement counters.
check("generic feedback total may increase",
      after_summary["total"] == before_summary["total"] + 1,
      f'{before_summary["total"]} → {after_summary["total"]}')
check("generic completed may increase on did_it",
      after_summary["completed"] == before_summary["completed"] + 1,
      f'{before_summary["completed"]} → {after_summary["completed"]}')

# MUST NOT: anything that means Parent-plan progress.
check("completed_activities contains NO pfbi_ identity",
      not any(str(c.get("activity_id") or "").startswith("pfbi_")
              for c in after_summary["completed_activities"]),
      repr([c.get("activity_id")
            for c in after_summary["completed_activities"]]))
check("completed_activities did not grow at all",
      len(after_summary["completed_activities"])
      == len(before_summary["completed_activities"]),
      f'{len(before_summary["completed_activities"])} → '
      f'{len(after_summary["completed_activities"])}')
check("the identity appears nowhere in completed_activities",
      pilot_identity not in json.dumps(after_summary["completed_activities"]))
after_cards = json.dumps(
    [[c.get("activity_id"), c.get("completed")]
     for day in (after.get("plan") or {}).get("week", [])
     for c in day.get("activities", [])], sort_keys=True)
check("no Parent card gained completed=true", after_cards == before_cards,
      after_cards[:160])
check("Parent progress_summary is unchanged",
      json.dumps(after.get("progress_summary"), sort_keys=True)
      == before_progress)
check("no milestone cup or completion was recorded",
      not doc(psid).get("completions") and not doc(psid).get("cups"),
      repr(doc(psid).get("completions")))
# And the standalone path still DOES all of it, so the filter is a boundary
# rather than a blanket off-switch.
sr = fb(psid, pcard, completion="did_it")
final = _build_session_view(doc(psid), psid)
check("a standalone did_it still enters completed_activities",
      any(c.get("activity_id") == pcard["activity_id"]
          for c in final["feedback_summary"]["completed_activities"]),
      repr([c.get("activity_id")
            for c in final["feedback_summary"]["completed_activities"]]))
check("and still stamps its Parent card completed",
      any(c.get("activity_id") == pcard["activity_id"] and c.get("completed")
          for day in (final.get("plan") or {}).get("week", [])
          for c in day.get("activities", [])),
      sr.text[:120])

print("\n" + "=" * 50)
print(f"Results: {_p} passed, {_f} failed")
if _f:
    print("❌ FAILURES"); sys.exit(1)
print("✅ All P0-A2 handoff tests PASSED")
