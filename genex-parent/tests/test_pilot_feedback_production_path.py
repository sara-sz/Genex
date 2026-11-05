"""
tests/test_pilot_feedback_production_path.py — P0-A2, the production shape

THE WHOLE CHAIN, BOTH REAL SERVICES, NO PARENT PLAN ANYWHERE.

Every other suite proves one side. This one drives the shape production will
have:

    Pilot  GET  /pilot/children/{child_id}/this-week      <- the only source
      ->   cycle_id, activity_ref, local_date                of activity identity
    Parent POST /api/v1/session/{sid}/pilot-feedback       <- pending delivery
      ->   feedback_id, observation_body
    Pilot  POST /pilot/cycles/{cycle_id}/observations      <- posted VERBATIM
      ->   observation.event_id                               (the receipt)
    Parent POST …/pilot-feedback-deliveries/{fid}/confirm  <- delivered

Both ends are the real thing: the real Pilot WSGI app over a real released
week it authored, and the real Parent FastAPI app over a real session
document. Nothing is faked and nothing is reshaped between the two — the dict
Parent returns is the dict posted to the Pilot.

The Parent session used here has NO intake, NO plan and NO activities, which
is the point: a provider-connected family's week lives in the Pilot, and a
client must never have to invent a Parent plan id, activity UUID or weekday to
report that a child practised.

Dependency-pure: in-process on both sides. No GCS, no Firestore, no network.
"""

from __future__ import annotations

import os
import pathlib
import sys

# ── the Parent app, exactly as the other Parent app-consumer suites set it up
os.environ.update(FIREBASE_PROJECT_ID="genex-test", LOCAL_SESSION_FALLBACK="1",
                  REQUIRE_BETA_CODE="true", BETA_ACCESS_CODE="genex",
                  ALLOWED_ORIGINS="http://localhost:3000", ACTIVITY_MODEL="")
os.environ.pop("GCS_BUCKET", None)
os.environ.pop("CONCERN_ROUTER_MODEL", None)
# The session store is NOT cleared: this suite owns a uid of its own, so it
# cannot collide with another suite's sessions and has no business deleting
# them.

import pytest  # noqa: E402

# The Pilot package is a sibling namespace, resolved from this file so the
# suite behaves the same wherever it is invoked from.
_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

pytest.importorskip("pilot_backend.transport.wsgi_app",
                    reason="the Pilot package is not present on this branch")

import firebase_admin  # noqa: E402

firebase_admin._apps["[DEFAULT]"] = object()
from firebase_admin import auth as fa  # noqa: E402

_TOKENS = {"parent-tok": {"uid": "uid-prodpath", "email": "p@x.com"}}
fa.verify_id_token = lambda t, *a, **k: (
    _TOKENS[t] if t in _TOKENS
    else (_ for _ in ()).throw(fa.InvalidIdTokenError("bad")))

from fastapi.testclient import TestClient  # noqa: E402

from api import progress as progress_lib  # noqa: E402
from api import session_store  # noqa: E402
from api.main import app  # noqa: E402

# The REAL Week 1 release chain, reused rather than rebuilt: a suite that
# assembled its own week could agree with itself about a handle neither system
# ever minted. `Chain` runs the product's own release path over the real
# StaticActivityBank and pins its calendar, so `this_week()` is the same code
# the `/pilot/children/{id}/this-week` route calls — on a deterministic date
# rather than on whenever CI happens to run.
from pilot_backend.audit.recorder import AuditRecorder  # noqa: E402
from pilot_backend.auth import DevAuthVerifier, VerifiedToken  # noqa: E402
from pilot_backend.config import PilotSettings  # noqa: E402
from pilot_backend.integration.parent_source import (  # noqa: E402
    InMemoryParentSessionSource,
)
from pilot_backend.tests.test_integration_identity import call  # noqa: E402
from pilot_backend.tests.test_week_one_release import Chain  # noqa: E402
from pilot_backend.transport import build_application  # noqa: E402

client = TestClient(app)
H = {"Authorization": "Bearer parent-tok"}

#: The only fields a provider-connected client ever sends to Parent. Asserted
#: as a set in `test_no_parent_plan_identity_appears_anywhere`, so a field
#: creeping back in fails a test rather than a deployment.
PILOT_FEEDBACK_FIELDS = frozenset({
    "pilot_cycle_id", "pilot_activity_ref", "activity_date",
    "enjoyment", "difficulty", "completion",
})


# ── the fictional provider-connected family ───────────────────────────────

@pytest.fixture
def parent_session():
    """A Parent session with NOTHING in it. No intake, no plan, no activities."""
    sid = client.post("/api/v1/session/start", headers=H, json={
        "child_name": "Fictional", "age_years": 4, "age_months": 0,
        "age_in_months": 48, "diagnosis_or_condition": "ADHD",
        "parent_concern": "ADHD, lack of attention", "daily_time_minutes": 10,
        "timezone": "America/Los_Angeles", "beta_access_code": "genex",
    }).json()["session_id"]
    doc = session_store.load("uid-prodpath", sid)
    # Stated as an assertion rather than assumed: every claim below about not
    # needing Parent planning depends on there being none.
    assert not doc.get("current_plan_id") and not doc.get("plans")
    return sid


@pytest.fixture
def pilot():
    """A real released Pilot Week 1, plus the real Pilot HTTP app over it.

    The week is produced by the product's own release path. The app is built by
    the product's own composition root over the SAME repositories, so the
    observation POST below is a real request to a real route — not a service
    call dressed up as one.
    """
    chain = Chain()
    chain.approve()
    chain.release()

    week = chain.this_week()
    assert week is not None, "the chain did not produce a released week"
    day = next(d for d in week["days"] if d["activities"])
    activity = day["activities"][0]
    assert activity["activity_ref"].startswith("pact1:"), activity
    # The internal placement ref is not on this surface at all, so a client
    # physically cannot send one.
    assert "activity_instance_ref" not in repr(week)

    verifier = DevAuthVerifier("dev")
    verifier.add("cg", VerifiedToken(subject="cg-subject"))
    verifier.add("hannah", VerifiedToken(subject="pr-subject"))
    app = build_application(
        settings=PilotSettings.from_env({
            "PILOT_ENVIRONMENT": "dev",
            "PILOT_ALLOWED_ORIGINS": "http://localhost:3000",
            "PILOT_DEV_AUTH": "1"}),
        repos=chain.repos, verifier=verifier,
        recorder=AuditRecorder(chain.repos.audit_events, environment="dev"),
        parent_source=InMemoryParentSessionSource(), log_sink=[])

    class Bundle:
        pass

    bundle = Bundle()
    bundle.chain, bundle.app = chain, app
    bundle.cycle_id = week["week"]["cycle_id"]
    bundle.activity_ref = activity["activity_ref"]
    bundle.local_date = day["local_date"]
    bundle.caregiver = chain.world.principal("cg-subject")
    bundle.released = {"cycle_id": bundle.cycle_id,
                       "activity_ref": bundle.activity_ref,
                       "local_date": bundle.local_date}
    return bundle


@pytest.fixture
def released(pilot):
    """Just the three values a client ever learns from `this-week`."""
    return pilot.released


def _submit(sid, released, **over):
    body = {"pilot_cycle_id": released["cycle_id"],
            "pilot_activity_ref": released["activity_ref"],
            "activity_date": released["local_date"],
            "enjoyment": "loved_it", "difficulty": "just_right",
            "completion": "did_it"}
    body.update(over)
    assert set(body) <= PILOT_FEEDBACK_FIELDS | {"note"}, sorted(body)
    return client.post(f"/api/v1/session/{sid}/pilot-feedback",
                       headers=H, json=body)


def _deliver(pilot, view):
    """Post the Parent-built observation body VERBATIM to the real Pilot route.

    The dict Parent returned is the request body. Nothing is rebuilt, reordered
    or renamed in between — which is the only way to prove a client that does
    exactly what the contract says will succeed.
    """
    import json as _json
    return call(pilot.app,
                f"/pilot/cycles/{view['pilot_cycle_id']}/observations",
                method="POST", bearer="Bearer cg",
                body=_json.dumps(view["observation_body"]).encode())[:2]


def _observations(pilot):
    return pilot.chain.weekly.list_observations(pilot.caregiver,
                                                pilot.cycle_id)


def _confirm(sid, feedback_id, receipt):
    return client.post(
        f"/api/v1/session/{sid}/pilot-feedback-deliveries/{feedback_id}"
        f"/confirm", headers=H, json={"pilot_observation_event_id": receipt})


def _row(sid, feedback_id):
    doc = session_store.load("uid-prodpath", sid)
    row_id = (doc.get("pilot_delivery_index") or {})[feedback_id]
    return next(r for r in doc["pilot_deliveries"]
                if r["delivery_id"] == row_id)


# ── the chain ─────────────────────────────────────────────────────────────

def test_the_whole_provider_connected_chain(pilot, parent_session,
                                            released):
    sid = parent_session

    # 1. Parent records the feedback and queues the delivery in one write.
    submitted = _submit(sid, released).json()
    feedback_id = submitted["feedback_id"]
    view = submitted["pilot_delivery"]
    assert submitted["supersedes_feedback_id"] is None
    assert view["status"] == "pending"
    assert view["activity_date"] == released["local_date"]

    # 2. The browser posts what Parent built, unchanged, to the real Pilot.
    status, body = _deliver(pilot, view)
    assert status == 200, body
    receipt = body["observation"]["event_id"]

    # The Pilot resolved the opaque handle to the placement the week scheduled,
    # attributed the attempt to the RELEASED day, and derived the caregiver
    # from its own token.
    stored = [e for e in _observations(pilot) if e.event_id == receipt][0]
    assert stored.local_date == released["local_date"]
    assert stored.source_feedback_id == feedback_id
    assert stored.enjoyment.value == "enjoyed"
    assert stored.attempt_outcome.value == "did_it"
    assert stored.recorded_by_caregiver_id
    assert stored.assistance == stored.child_response == ""

    # 3. The client confirms with the Pilot's own event id.
    confirmed = _confirm(sid, feedback_id, receipt).json()
    assert confirmed["status"] == "delivered"
    assert confirmed["idempotent_replay"] is False
    assert confirmed["pilot_observation_event_id"] == receipt

    row = _row(sid, feedback_id)
    assert row["status"] == "delivered"
    assert row["pilot_observation_event_id"] == receipt
    # The queue is empty, which is the whole point of the ledger.
    pending = client.get(
        f"/api/v1/session/{sid}/pilot-feedback-deliveries", headers=H).json()
    assert pending["count"] == 0, pending


def test_an_exact_parent_retry_is_the_same_feedback_and_the_same_delivery(
        pilot, parent_session, released):
    """A lost response must not become a second observation."""
    sid = parent_session
    first = _submit(sid, released).json()
    again = _submit(sid, released).json()
    assert again["idempotent_replay"] is True
    assert again["feedback_id"] == first["feedback_id"]
    assert (again["pilot_delivery"]["delivery_id"]
            == first["pilot_delivery"]["delivery_id"])
    doc = session_store.load("uid-prodpath", sid)
    assert len(doc["pilot_deliveries"]) == 1
    assert len([f for f in doc["feedback"]
                if progress_lib.feedback_mode_of(f) == "pilot_linked"]) == 1


def test_an_exact_pilot_retry_is_the_same_pilot_event(pilot, parent_session,
                                                      released):
    """The Pilot dedupes on `source_feedback_id`, which Parent minted once."""
    sid = parent_session
    view = _submit(sid, released).json()["pilot_delivery"]
    first_status, first_body = _deliver(pilot, view)
    again_status, again_body = _deliver(pilot, view)
    assert first_status == again_status == 200, (first_body, again_body)
    assert (again_body["observation"]["event_id"]
            == first_body["observation"]["event_id"])
    stored = [e for e in _observations(pilot)
              if e.source_feedback_id == view["feedback_id"]]
    assert len(stored) == 1, stored


def test_confirmation_replays_on_the_same_receipt_and_conflicts_on_another(
        pilot, parent_session, released):
    sid = parent_session
    submitted = _submit(sid, released).json()
    feedback_id = submitted["feedback_id"]
    _, body = _deliver(pilot, submitted["pilot_delivery"])
    receipt = body["observation"]["event_id"]

    first = _confirm(sid, feedback_id, receipt).json()
    replay = _confirm(sid, feedback_id, receipt)
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["delivered_at_utc"] == first["delivered_at_utc"]

    conflict = _confirm(sid, feedback_id, "obsv_not_the_one")
    assert conflict.status_code == 409, conflict.text
    assert "receipt_conflict" in conflict.text
    # The recorded fact did not move.
    row = _row(sid, feedback_id)
    assert row["pilot_observation_event_id"] == receipt
    assert row["delivered_at_utc"] == first["delivered_at_utc"]


def test_a_revision_supersedes_on_both_sides(pilot, parent_session,
                                             released):
    """One practice, corrected. Two records, one current reading."""
    from pilot_backend.domain.observation import current_observations

    sid = parent_session
    first = _submit(sid, released).json()
    _deliver(pilot, first["pilot_delivery"])

    revised = _submit(sid, released, completion="wasnt_ready_yet",
                      enjoyment="not_really", difficulty="too_hard").json()
    assert revised["feedback_id"] != first["feedback_id"]
    assert revised["supersedes_feedback_id"] == first["feedback_id"]
    view = revised["pilot_delivery"]
    assert (view["observation_body"]["supersedes_source_feedback_id"]
            == first["feedback_id"])
    assert view["delivery_id"] != first["pilot_delivery"]["delivery_id"]

    status, body = _deliver(pilot, view)
    assert status == 200, body

    # Parent kept both delivery histories...
    doc = session_store.load("uid-prodpath", sid)
    assert len(doc["pilot_deliveries"]) == 2
    assert {r["feedback_id"] for r in doc["pilot_deliveries"]} == {
        first["feedback_id"], revised["feedback_id"]}

    # ...and the Pilot shows ONE current reading rather than two signals.
    stored = _observations(pilot)
    ids = {e.source_feedback_id for e in stored}
    assert {first["feedback_id"], revised["feedback_id"]} <= ids
    current = {e.source_feedback_id for e in current_observations(stored)}
    assert revised["feedback_id"] in current
    assert first["feedback_id"] not in current


def test_a_different_released_day_is_a_different_practice(pilot,
                                                          parent_session,
                                                          released):
    """Same activity, next day. Not a revision of yesterday."""
    sid = parent_session
    first = _submit(sid, released).json()
    other_day = _submit(sid, released, activity_date="2026-10-02").json()
    assert other_day["supersedes_feedback_id"] is None
    assert other_day["feedback_id"] != first["feedback_id"]


def test_standalone_and_pilot_linked_feedback_never_supersede_each_other(
        pilot, parent_session, released):
    """The two modes are different clinical records about different things.

    A standalone record cannot be reached by a Pilot-linked query even when the
    Parent activity id IS the derived Pilot identity and the dates coincide —
    the only remaining way the two could be confused.
    """
    sid = parent_session
    pilot_first = _submit(sid, released).json()["feedback_id"]
    identity = progress_lib.pilot_feedback_identity(
        released["cycle_id"], released["activity_ref"],
        released["local_date"])

    doc = session_store.load("uid-prodpath", sid)
    forged = {
        "feedback_id": "fb_forged_standalone",
        "feedback_mode": progress_lib.FEEDBACK_MODE_STANDALONE,
        "activity_id": identity,                       # the same identity
        "activity_date": released["local_date"],       # the same date
        "local_submission_date": released["local_date"],
        "completion": "did_it", "enjoyment": "loved_it",
        "difficulty": "just_right",
    }
    doc["feedback"].append(forged)
    session_store.save("uid-prodpath", sid, doc)

    revised = _submit(sid, released, completion="wasnt_ready_yet").json()
    assert revised["supersedes_feedback_id"] == pilot_first
    assert revised["supersedes_feedback_id"] != "fb_forged_standalone"

    # And the matcher refuses in the other direction too.
    doc = session_store.load("uid-prodpath", sid)
    assert progress_lib.find_prior_feedback(
        doc["feedback"], mode=progress_lib.FEEDBACK_MODE_PILOT,
        pilot_cycle_id=released["cycle_id"],
        pilot_activity_ref=released["activity_ref"],
        activity_date=released["local_date"],
    )["feedback_id"] != "fb_forged_standalone"
    assert progress_lib.find_prior_feedback(
        doc["feedback"], mode=progress_lib.FEEDBACK_MODE_STANDALONE,
        activity_id=identity, local_date=released["local_date"],
        activity_date=released["local_date"],
    )["feedback_id"] == "fb_forged_standalone"


def test_no_parent_plan_identity_appears_anywhere(pilot, parent_session,
                                                  released):
    """The claim the whole slice turns on, asserted over the real trace."""
    sid = parent_session
    submitted = _submit(sid, released).json()
    view = submitted["pilot_delivery"]
    _deliver(pilot, view)

    doc = session_store.load("uid-prodpath", sid)
    assert not doc.get("current_plan_id") and not doc.get("plans")
    assert not doc.get("plan_internal")

    record = next(f for f in doc["feedback"]
                  if f["feedback_id"] == submitted["feedback_id"])
    # No Parent plan identity was stored, and none was invented.
    for absent in ("plan_id", "day", "module_id", "plan_period_id", "source",
                   "focus_key", "domain", "activity_family",
                   "activity_template_id", "original_activity_id"):
        assert absent not in record, absent
    assert record["metadata_found"] is False
    # The Parent-side identity is the Pilot triple, and is NOT the handle.
    assert record["activity_id"] == progress_lib.pilot_feedback_identity(
        released["cycle_id"], released["activity_ref"],
        released["local_date"])
    assert record["activity_id"] != released["activity_ref"]

    # No rewards were manufactured from provenance that does not exist.
    assert not doc.get("attempts") and not doc.get("completions")
    assert not [e for e in doc["events"] if e["type"] == "star_awarded"]

    # And nothing Parent-internal reached the Pilot.
    flat = repr(view["observation_body"])
    assert "uid-prodpath" not in flat
    assert "activity_instance_ref" not in flat
    assert record["activity_id"] not in flat
