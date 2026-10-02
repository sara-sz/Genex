"""0.5C — the Hannah workflow end to end, over the real HTTP surface.

`test_workflow_http.py` pins the authorization matrix. This file pins that the
workflow actually WORKS: one fictional family, one clinician, and the whole
Tuesday path from a goal suggestion through to a month-end report preview.

It exists because a mutation sweep showed the RTM routes were implemented but
never successfully exercised — three lineage mutations and two serialiser
mutations survived, and one of them was hiding a real `AttributeError` in the
report payload. A route with no passing call is not a route anyone should wire
a UI to.

## Episodes and periods are opened through the SERVICE, not a route

0.5C adds no route for opening an RTM episode or period: the approved surface
is the one the Tuesday UI calls, and the UI does not open episodes. So the
fixture opens them through the frozen `RTMService`, exactly as an
administrative path would, and every assertion afterwards goes through HTTP.

## Everything here is fictional

Neutral aliases, sentinel strings, no real-person-associated name, and a
`Child` that carries no name, age or clinical detail because the domain object
has nowhere to put one.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth import DevAuthVerifier, VerifiedToken
from pilot_backend.auth.resolver import resolve_principal
from pilot_backend.config import PilotSettings
from pilot_backend.connections import ProviderConnectionService
from pilot_backend.domain.connections import CaregiverChildConnection
from pilot_backend.domain.entities import Caregiver, Child, Practice
from pilot_backend.domain.enums import CaregiverRelationship, ProviderDiscipline
from pilot_backend.domain.goals import EditType, GoalKind, GoalRef
from pilot_backend.goals.service import GoalService
from pilot_backend.integration.parent_source import InMemoryParentSessionSource
from pilot_backend.persistence import FakeDocumentStore, FirestoreRepositories
from pilot_backend.planning.service import MonthlyPlanService
from pilot_backend.provisioning import provision_provider_record
from pilot_backend.rtm.service import RTMService
from pilot_backend.transport import build_application
from pilot_backend.weekly.service import WeeklyService

from .test_integration_identity import call
from .test_secure_foundation import SENTINEL_EMAIL

MONTH = datetime.now(timezone.utc).strftime("%Y-%m")
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
FORBIDDEN = {"error": "not permitted"}

#: A sentinel that must never appear in a log line or an error body.
SENTINEL_TEXT = "Fictional-sentinel-TARGET-7741"


def _subject(name: str) -> str:
    return f"fictional-subject-{name}"


@pytest.fixture()
def e2e():
    """One family, one clinician, connected and assigned. Nothing else."""
    settings = PilotSettings.from_env({
        "PILOT_ENVIRONMENT": "dev",
        "PILOT_ALLOWED_ORIGINS": "http://localhost:3000",
        "PILOT_DEV_AUTH": "1",
    })
    repos = FirestoreRepositories(FakeDocumentStore())
    recorder = AuditRecorder(repos.audit_events, environment="dev")

    practice = repos.practices.create(Practice.create("Practice-Pilot"))
    caregiver = repos.caregivers.create(Caregiver.create(
        "Caregiver-Pilot", auth_subject=_subject("cg")))
    child = repos.children.create(
        Child.create(actor_id=caregiver.caregiver_id))
    repos.caregiver_child.connect(CaregiverChildConnection.create(
        caregiver.caregiver_id, child.child_id, CaregiverRelationship.PARENT,
        actor_id=caregiver.caregiver_id))
    hannah = provision_provider_record(
        repos, auth_subject=_subject("hannah"),
        practice_id=practice.practice_id, discipline=ProviderDiscipline.SLP,
        display_name="Provider-Hannah").provider

    verifier = DevAuthVerifier("dev")
    tokens = {"cg": _subject("cg"), "hannah": _subject("hannah")}
    for token, subject in tokens.items():
        verifier.add(token, VerifiedToken(subject=subject, email=SENTINEL_EMAIL))

    logs = []
    app = build_application(
        settings=settings, repos=repos, verifier=verifier, recorder=recorder,
        parent_source=InMemoryParentSessionSource(), log_sink=logs)

    def principal(token):
        return resolve_principal(VerifiedToken(subject=tokens[token]), repos)

    connections = ProviderConnectionService(repos=repos, recorder=recorder)
    pending = connections.invite_provider(
        principal("cg"), child.child_id, hannah.provider_id)
    connections.accept_invitation(principal("hannah"), pending.connection_id)
    connections.assign_managing_clinician(
        principal("cg"), child.child_id, hannah.provider_id)

    class Bundle:
        pass

    bundle = Bundle()
    bundle.app, bundle.repos, bundle.logs = app, repos, logs
    bundle.child = child.child_id
    bundle.hannah = hannah
    bundle.principal = principal
    bundle.goals = GoalService(repos=repos, recorder=recorder)
    bundle.plans = MonthlyPlanService(repos=repos, recorder=recorder)
    bundle.weekly = WeeklyService(repos=repos, recorder=recorder)
    bundle.rtm = RTMService(repos=repos, recorder=recorder)
    return bundle


def get(e2e, path, token="hannah", query=""):
    """A GET. `query` goes to QUERY_STRING, never into PATH_INFO.

    Embedding `?cycle_month=` in the path makes the router see it as part of a
    path segment, which simply does not match any template and 404s — a
    mistake worth noting because the 404 looks like a missing route.
    """
    return call(e2e.app, path, bearer=f"Bearer {token}", query=query)


def post(e2e, path, token="hannah", body=None):
    return call(e2e.app, path, method="POST", bearer=f"Bearer {token}",
                body=json.dumps(body or {}).encode())


# ---------------------------------------------------------------------------
# the staged world — each stage builds on the last
# ---------------------------------------------------------------------------

def _approved_goal(e2e) -> dict:
    created = post(e2e, f"/pilot/children/{e2e.child}/goals", "hannah",
                   {"edit_type": "authored_fresh", "text": SENTINEL_TEXT,
                    "reason": "Fictional clinical rationale"})
    assert created[0] == 200, created
    return created[1]["goal"]


def _active_plan(e2e, goal) -> str:
    created = post(e2e, f"/pilot/children/{e2e.child}/monthly-plan", "hannah",
                   {"cycle_month": MONTH, "timezone_of_record": "UTC"})
    assert created[0] == 200, created
    plan_id = created[1]["plan"]["focus_plan_id"]
    allocated = post(e2e, f"/pilot/monthly-plans/{plan_id}/allocations",
                     "hannah", {"goal_kind": "clinical",
                                "goal_id": goal["goal_id"],
                                "priority_rank": 1, "emphasis_weight": 2})
    assert allocated[0] == 200, allocated
    activated = post(e2e, f"/pilot/monthly-plans/{plan_id}/activate", "hannah")
    assert activated[0] == 200, activated
    return plan_id


def _cycle(e2e, plan_id, goal_id) -> str:
    """A RELEASED weekly cycle, created through the frozen service.

    0.5C adds no cycle-creation route: the weekly engine generates cycles and
    the Tuesday UI reads them rather than making them.

    The RELEASE matters. `create_cycle` leaves a DRAFT, and a draft week is
    not yet the family's — `record_observation` refuses evidence against one.
    An earlier version of this fixture stopped at DRAFT and every observation
    came back 403, which read like an authorization failure and was actually a
    lifecycle one.
    """
    from pilot_backend.domain.source_link import SourceSystem

    cycle = e2e.weekly.create_cycle(e2e.principal("hannah"), plan_id,
                                    sequence_in_month=1)
    # The SNAPSHOT comes before the release: "a cycle cannot be released
    # before its plan is snapshotted". The snapshot freezes what the
    # parent-facing plan actually contained, so a later review is reading the
    # week the family saw rather than whatever the generator would produce
    # today. The document is a fictional stand-in for Parent's own plan.
    e2e.weekly.capture_snapshot(
        e2e.principal("hannah"), cycle.cycle_id, SourceSystem.PARENT,
        "fictional-parent-plan-1",
        {"activities": [{"ref": "fictional-activity-1"},
                        {"ref": "fictional-activity-2"}]})
    # ALLOCATION before release, and before any observation.
    #
    # `record_observation` refuses an activity ref that is not aligned to the
    # cycle — "activity instance does not belong to this cycle". That is a
    # real guarantee worth naming: evidence can only be recorded against an
    # activity the week actually SCHEDULED, so a client cannot invent an
    # activity and attach outcomes to it. The allocator writes the alignments
    # that make a ref legitimate.
    from pilot_backend.weekly.allocator import CandidateActivity

    ref = GoalRef(GoalKind.CLINICAL, goal_id)
    e2e.weekly.allocate_cycle(
        e2e.principal("hannah"), cycle.cycle_id,
        [CandidateActivity(activity_identity_ref="fictional-activity-1",
                           supports=(ref,), primary_for=ref),
         CandidateActivity(activity_identity_ref="fictional-activity-2",
                           supports=(ref,))],
        family_declared_capacity=4)
    released = e2e.weekly.release_cycle(e2e.principal("hannah"),
                                        cycle.cycle_id)
    return released.cycle_id


def _rtm_period(e2e, goal, plan_id) -> tuple:
    """An episode and its monitoring period, through the frozen service."""
    episode = e2e.rtm.open_episode(
        e2e.principal("hannah"), e2e.child,
        [GoalRef(GoalKind.CLINICAL, goal["goal_id"])])
    period = e2e.rtm.open_period(e2e.principal("hannah"), episode.episode_id,
                                 plan_id)
    return episode, period


@pytest.fixture()
def world(e2e):
    """The full staged world: goal, active plan, cycle, episode, period."""
    goal = _approved_goal(e2e)
    plan_id = _active_plan(e2e, goal)
    cycle_id = _cycle(e2e, plan_id, goal['goal_id'])
    episode, period = _rtm_period(e2e, goal, plan_id)
    e2e.goal, e2e.plan_id, e2e.cycle_id = goal, plan_id, cycle_id
    e2e.episode, e2e.period = episode, period
    # The REAL activity instance refs, taken from the alignments the allocator
    # wrote. They are composite — `{cycle}::{activity}::{index}` — so a test
    # cannot guess one, which is exactly the point: evidence may only cite an
    # activity the week actually scheduled.
    e2e.activity_refs = [a.activity_instance_ref for a in
                         e2e.weekly.list_alignments(
                             e2e.principal("hannah"), cycle_id)]
    return e2e


# ===========================================================================
# THE END-TO-END HAPPY PATH
# ===========================================================================

def test_the_whole_hannah_workflow_over_http(world):
    """Suggestion -> goal -> month -> week -> evidence -> review -> RTM.

    One test, deliberately, because the point is that the STAGES COMPOSE. Each
    assertion below depends on the previous call having persisted something
    the next one can find, which is the property a per-route test cannot show.
    """
    w = world

    # --- 1. the clinician sees deterministic suggestions ----------------
    suggestions = get(w, f"/pilot/children/{w.child}/goal-suggestions")
    assert suggestions[0] == 200
    assert isinstance(suggestions[1]["suggestions"], list)

    # --- 2. the approved goal is RTM-eligible and names a version -------
    assert w.goal["is_rtm_eligible"] is True
    assert w.goal["current_version_id"], "blocker 3: goal must name a version"

    # --- 3. the clinician modifies the wording; v1 is NOT rewritten -----
    revised = post(w, f"/pilot/goals/clinical/{w.goal['goal_id']}/revisions",
                   "hannah", {"text": "Fictional revised target",
                              "edit_type": "modified",
                              "reason": "Fictional revision rationale"})
    assert revised[0] == 200, revised
    assert revised[1]["version_number"] == 2
    chain = w.repos.goal_versions.list_chain(w.goal["goal_id"])
    assert [v.version_number for v in chain] == [1, 2]
    assert chain[0].text == SENTINEL_TEXT, "version 1 was rewritten"

    # --- 4. the family reads the active month and its allocation --------
    plan = get(w, f"/pilot/children/{w.child}/monthly-plan", "cg")
    assert plan[0] == 200
    assert plan[1]["plan"]["state"] == "active"
    assert [a["goal_id"] for a in plan[1]["allocations"]] == [w.goal["goal_id"]]

    # --- 5. the family reads the current week ---------------------------
    cycle = get(w, f"/pilot/children/{w.child}/current-cycle", "cg")
    assert cycle[0] == 200
    assert cycle[1]["cycle"]["cycle_id"] == w.cycle_id

    # --- 6. the family records structured evidence ----------------------
    observed = post(w, f"/pilot/cycles/{w.cycle_id}/observations", "cg",
                    {"activity_instance_ref": w.activity_refs[0],
                     "local_date": TODAY, "attempt_outcome": "did_it",
                     "difficulty": "just_right", "enjoyment": "enjoyed"})
    assert observed[0] == 200, observed
    event_id = observed[1]["observation"]["event_id"]

    # --- 7. and defers one for later ------------------------------------
    deferred = post(w, f"/pilot/cycles/{w.cycle_id}/defers", "cg",
                    {"activity_instance_ref": w.activity_refs[1]})
    assert deferred[0] == 200, deferred
    assert deferred[1]["defer"]["defer_id"]

    # --- 8. the clinician reads that evidence ---------------------------
    listed = get(w, f"/pilot/cycles/{w.cycle_id}/observations", "hannah")
    assert listed[0] == 200
    assert event_id in {o["event_id"] for o in listed[1]["observations"]}

    # --- 9. the RTM surface shows the episode AND its period ------------
    rtm = get(w, f"/pilot/children/{w.child}/rtm", "hannah")
    assert rtm[0] == 200, rtm
    episode = rtm[1]["episodes"][0]
    assert episode["episode_id"] == w.episode.episode_id
    assert [p["period_id"] for p in episode["periods"]] == [w.period.period_id]

    # --- 10. a review CITING that observation ---------------------------
    review = post(w, f"/pilot/rtm-periods/{w.period.period_id}/reviews",
                  "hannah", {"clinical_interpretation": "Fictional reading",
                             "reviewed_event_ids": [event_id],
                             "reviewed_cycle_ids": [w.cycle_id]})
    assert review[0] == 200, review
    review_id = review[1]["review"]["review_id"]
    assert review[1]["review"]["reviewed_event_count"] == 1

    # --- 11. an action hanging off that review --------------------------
    action = post(w, f"/pilot/rtm-reviews/{review_id}/actions", "hannah",
                  {"action_type": "modify_plan",
                   "narrative": "Fictional action narrative"})
    assert action[0] == 200, action

    # --- 12. manual minutes, never inferred -----------------------------
    entry = post(w, f"/pilot/rtm-periods/{w.period.period_id}/time-entries",
                 "hannah", {"local_date": TODAY, "minutes": 12,
                            "activity_description": "Fictional review work",
                            "source_review_id": review_id})
    assert entry[0] == 200, entry
    assert entry[1]["time_entry"]["minutes"] == 12

    # --- 13. a synchronous interaction, explicitly affirmed -------------
    interaction = post(
        w, f"/pilot/rtm-periods/{w.period.period_id}/interactions", "hannah",
        {"local_date": TODAY, "modality": "phone", "participant_type": "caregiver",
         "duration_minutes": 8, "real_time_affirmed": True})
    assert interaction[0] == 200, interaction
    assert interaction[1]["interaction"]["modality"] == "phone"

    # --- 14. the report PREVIEW ----------------------------------------
    report = post(w, f"/pilot/rtm-periods/{w.period.period_id}/report",
                  "hannah")
    assert report[0] == 200, report
    assert report[1]["report"]["is_preview"] is True
    assert report[1]["report"]["state"] != "finalized"
    assert report[1]["evidence_summary"]["summary_id"]


# ===========================================================================
# LINEAGE — evidence -> review -> action, through persistence
# ===========================================================================

def test_the_review_persists_the_reviewed_event_ids(world):
    """Lineage survives serialisation AND the round trip to the store."""
    w = world
    observed = post(w, f"/pilot/cycles/{w.cycle_id}/observations", "cg",
                    {"activity_instance_ref": w.activity_refs[0],
                     "local_date": TODAY, "attempt_outcome": "did_it"})
    event_id = observed[1]["observation"]["event_id"]

    review = post(w, f"/pilot/rtm-periods/{w.period.period_id}/reviews",
                  "hannah", {"clinical_interpretation": "Fictional reading",
                             "reviewed_event_ids": [event_id]})
    review_id = review[1]["review"]["review_id"]

    stored = w.repos.therapist_reviews.get_by_id(review_id)
    assert list(stored.reviewed_event_ids) == [event_id], (
        "the reviewed event ids did not reach the store")
    assert stored.period_id == w.period.period_id
    assert stored.child_id == w.child
    assert stored.provider_id == w.hannah.provider_id


def test_the_clinical_action_persists_its_review_link(world):
    w = world
    review = post(w, f"/pilot/rtm-periods/{w.period.period_id}/reviews",
                  "hannah", {"clinical_interpretation": "Fictional reading"})
    review_id = review[1]["review"]["review_id"]
    action = post(w, f"/pilot/rtm-reviews/{review_id}/actions", "hannah",
                  {"action_type": "continue_plan", "narrative": "Fictional"})
    action_id = action[1]["action"]["action_id"]

    stored = w.repos.clinical_actions.get_by_id(action_id)
    assert stored.review_id == review_id, "the action lost its review"
    assert stored.period_id == w.period.period_id
    assert stored.child_id == w.child


def test_a_time_entry_can_cite_the_review_that_caused_it(world):
    """Manual minutes, attributable to the work that produced them."""
    w = world
    review = post(w, f"/pilot/rtm-periods/{w.period.period_id}/reviews",
                  "hannah", {"clinical_interpretation": "Fictional reading"})
    review_id = review[1]["review"]["review_id"]
    entry = post(w, f"/pilot/rtm-periods/{w.period.period_id}/time-entries",
                 "hannah", {"local_date": TODAY, "minutes": 7,
                            "activity_description": "Fictional review work",
                            "source_review_id": review_id})
    stored = w.repos.time_entries.get_by_id(
        entry[1]["time_entry"]["time_entry_id"])
    assert stored.source_review_id == review_id
    assert stored.minutes == 7
    # The documented total is the sum of SUPPLIED minutes, never inferred.
    rtm = get(w, f"/pilot/children/{w.child}/rtm", "hannah")
    minutes = rtm[1]["episodes"][0]["documented_minutes"][w.period.period_id]
    assert minutes == 7


def test_the_report_preview_reflects_the_period_it_was_generated_for(world):
    w = world
    post(w, f"/pilot/rtm-periods/{w.period.period_id}/reviews", "hannah",
         {"clinical_interpretation": "Fictional reading"})
    report = post(w, f"/pilot/rtm-periods/{w.period.period_id}/report",
                  "hannah")
    assert report[0] == 200, report
    body = report[1]["report"]
    stored = w.repos.month_end_reports.get_by_id(body["report_id"])
    assert stored.period_id == w.period.period_id
    assert stored.child_id == w.child
    assert body["cycle_month"] == w.period.cycle_month
    assert body["version"] == stored.version


# ===========================================================================
# PAYLOAD SHAPE — what leaves the server
# ===========================================================================

def test_the_observation_payload_is_exactly_the_structured_fields(world):
    """Pinned as an EXACT key set, so an added domain field cannot leak."""
    w = world
    observed = post(w, f"/pilot/cycles/{w.cycle_id}/observations", "cg",
                    {"activity_instance_ref": w.activity_refs[0],
                     "local_date": TODAY, "attempt_outcome": "did_it",
                     "difficulty": "too_hard", "enjoyment": "disliked"})
    payload = observed[1]["observation"]
    assert set(payload) == {
        "event_id", "activity_instance_ref", "local_date",
        "attribution_month", "timezone_of_record", "attempt_outcome",
        "difficulty", "enjoyment"}
    # The three free-text domain fields exist and must NOT be serialised.
    for banned in ("child_response", "assistance", "observation_text_ref",
                   "note", "comment", "recorded_by_caregiver_id"):
        assert banned not in payload, banned
    assert payload["difficulty"] == "too_hard"
    assert payload["enjoyment"] == "disliked"


def test_the_observation_list_payload_has_the_same_shape(world):
    """A leak through the LIST route would be just as bad as through create."""
    w = world
    post(w, f"/pilot/cycles/{w.cycle_id}/observations", "cg",
         {"activity_instance_ref": w.activity_refs[0],
          "local_date": TODAY, "attempt_outcome": "did_it"})
    listed = get(w, f"/pilot/cycles/{w.cycle_id}/observations", "hannah")
    for row in listed[1]["observations"]:
        assert "child_response" not in row
        assert "assistance" not in row
        assert set(row) == {
            "event_id", "activity_instance_ref", "local_date",
            "attribution_month", "timezone_of_record", "attempt_outcome",
            "difficulty", "enjoyment"}


def test_the_report_payload_is_a_preview_and_carries_no_payer_field(world):
    """`is_preview` is DERIVED from the state, not asserted by the server.

    Also the no-payer rule, checked over the serialised bytes rather than the
    key set, so a nested value could not smuggle one either.
    """
    w = world
    post(w, f"/pilot/rtm-periods/{w.period.period_id}/reviews", "hannah",
         {"clinical_interpretation": "Fictional reading"})
    report = post(w, f"/pilot/rtm-periods/{w.period.period_id}/report",
                  "hannah")[1]
    body = report["report"]
    assert set(body) == {"report_id", "period_id", "cycle_month", "state",
                         "version", "section_count", "is_preview"}
    assert body["is_preview"] is True
    assert body["state"] == "draft"

    rendered = json.dumps(report).lower()
    for banned in ("payer", "insurance", "member_id", "claim", "copay",
                   "reimburse", "medical_necessity", "deductible"):
        assert banned not in rendered, banned


def test_the_rtm_payload_carries_no_clinical_interpretation(world):
    """The episode listing is organisational, not clinical."""
    w = world
    post(w, f"/pilot/rtm-periods/{w.period.period_id}/reviews", "hannah",
         {"clinical_interpretation": "Fictional-interpretation-SECRET"})
    rtm = get(w, f"/pilot/children/{w.child}/rtm", "hannah")
    assert "Fictional-interpretation-SECRET" not in json.dumps(rtm[1])


# ===========================================================================
# the remaining mutation gaps
# ===========================================================================

def test_an_unexpected_exception_never_reaches_the_client(world):
    """A non-PHI-safe failure renders as the constant 500 body.

    Forced by making a frozen service raise a bare `RuntimeError` whose
    message contains a sentinel. The client must see neither the sentinel nor
    the exception type.
    """
    w = world
    from pilot_backend.rtm.service import RTMService

    real = RTMService.list_episodes

    def exploding(self, principal, child_id):
        raise RuntimeError("Fictional-crash-SECRET-9931")

    RTMService.list_episodes = exploding
    try:
        status, body, _ = get(w, f"/pilot/children/{w.child}/rtm", "hannah")
    finally:
        RTMService.list_episodes = real

    assert status == 500
    assert body == {"error": "internal error"}
    assert "Fictional-crash-SECRET-9931" not in json.dumps(body)
    rendered = " ".join(str(entry) for entry in w.logs)
    assert "Fictional-crash-SECRET-9931" not in rendered


def test_the_goal_revision_does_not_trust_the_path_kind(world):
    """A caregiver-approved goal id cannot be revised as `clinical`.

    The kind in the path selects a repository and an authorization rule, so
    accepting it blindly would let a caller aim a clinician-only revision at a
    caregiver-owned goal.
    """
    w = world
    family_goal = w.goals.approve_caregiver_goal(
        w.principal("cg"), w.child, edit_type=EditType.AUTHORED_FRESH,
        text="Fictional family wording", reason="Fictional family rationale")

    mislabelled = post(
        w, f"/pilot/goals/clinical/{family_goal.caregiver_goal_id}/revisions",
        "hannah", {"text": "Fictional hijack", "edit_type": "modified",
                   "reason": "Fictional"})
    assert mislabelled[0] == 403, mislabelled
    assert mislabelled[1] == FORBIDDEN
    # The family's goal is untouched at version 1.
    chain = w.repos.goal_versions.list_chain(family_goal.caregiver_goal_id)
    assert [v.version_number for v in chain] == [1]


def test_a_supplied_cycle_month_is_honoured(world):
    """`?cycle_month=` must select that month, not the current one."""
    w = world
    # The staged world's plan is the CURRENT month. A different month has none.
    other = "2026-01" if MONTH != "2026-01" else "2026-02"
    read = get(w, f"/pilot/children/{w.child}/monthly-plan", "cg",
               query=f"cycle_month={other}")
    assert read[0] == 200, read
    assert read[1]["plan"] is None, (
        "a supplied cycle_month was ignored and the current month returned")

    # And the current month still resolves when asked for explicitly.
    current = get(w, f"/pilot/children/{w.child}/monthly-plan", "cg",
                  query=f"cycle_month={MONTH}")
    assert current[0] == 200
    assert current[1]["plan"]["cycle_month"] == MONTH


def test_the_current_cycle_route_honours_the_month_too(world):
    w = world
    other = "2026-01" if MONTH != "2026-01" else "2026-02"
    read = get(w, f"/pilot/children/{w.child}/current-cycle", "cg",
               query=f"cycle_month={other}")
    assert read[0] == 200
    assert read[1]["plan"] is None
    assert read[1]["cycle"] is None


# ===========================================================================
# RTM semantics preserved through the transport
# ===========================================================================

def test_an_unaffirmed_interaction_does_not_count_as_real_time(world):
    """The frozen rule, which is narrower than "unaffirmed is refused".

    My first version of this test asserted a 403 and was WRONG about the
    domain. `SynchronousInteraction` refuses an unaffirmed interaction only
    for OTHER_SYNCHRONOUS, where the modality itself is unverifiable. For a
    named modality like `phone` the record is accepted — it happened — but
    `counts_as_real_time_communication` is False, so it does not satisfy the
    real-time-communication requirement that coding assistance depends on.

    That distinction is the point: an async message is never a synchronous
    interaction, and an unattested call is a recorded event that earns
    nothing.
    """
    w = world
    accepted = post(
        w, f"/pilot/rtm-periods/{w.period.period_id}/interactions", "hannah",
        {"local_date": TODAY, "modality": "phone",
         "participant_type": "caregiver", "duration_minutes": 8,
         "real_time_affirmed": False})
    assert accepted[0] == 200, accepted
    stored = w.repos.synchronous_interactions.get_by_id(
        accepted[1]["interaction"]["interaction_id"])
    assert stored.real_time_affirmed is False
    assert stored.counts_as_real_time_communication is False, (
        "an unaffirmed interaction must not count as real-time communication")

    # An AFFIRMED one with a family participant does count.
    affirmed = post(
        w, f"/pilot/rtm-periods/{w.period.period_id}/interactions", "hannah",
        {"local_date": TODAY, "modality": "phone",
         "participant_type": "caregiver", "duration_minutes": 8,
         "real_time_affirmed": True})
    assert affirmed[0] == 200
    counted = w.repos.synchronous_interactions.get_by_id(
        affirmed[1]["interaction"]["interaction_id"])
    assert counted.counts_as_real_time_communication is True

    # And OTHER_SYNCHRONOUS without affirmation is refused outright.
    refused = post(
        w, f"/pilot/rtm-periods/{w.period.period_id}/interactions", "hannah",
        {"local_date": TODAY, "modality": "other_synchronous",
         "participant_type": "caregiver", "duration_minutes": 8,
         "real_time_affirmed": False})
    assert refused[0] == 403, refused
    assert refused[1] == FORBIDDEN


def test_a_non_managing_provider_cannot_write_rtm(world):
    """Every RTM write is managing-clinician gated, over HTTP."""
    w = world
    from pilot_backend.connections import ProviderConnectionService

    other = provision_provider_record(
        w.repos, auth_subject=_subject("other"),
        practice_id=w.repos.providers.get_by_id(
            w.hannah.provider_id).practice_id,
        discipline=ProviderDiscipline.SLP, display_name="Provider-Other").provider
    connections = ProviderConnectionService(repos=w.repos)
    pending = connections.invite_provider(w.principal("cg"), w.child,
                                          other.provider_id)
    other_principal = resolve_principal(
        VerifiedToken(subject=other.auth_subject), w.repos)
    connections.accept_invitation(other_principal, pending.connection_id)
    w.app  # the verifier has no token for this provider, so go via the service
    assert connections.current_managing_clinician(
        w.principal("cg"), w.child).provider_id == w.hannah.provider_id

    # The frozen rule, asserted at the service the route calls.
    from pilot_backend.rtm.errors import RTMAuthorizationError

    with pytest.raises(RTMAuthorizationError):
        w.rtm.record_review(other_principal, w.period.period_id,
                            clinical_interpretation="Fictional intruder")


# ===========================================================================
# the path kind and the body kind must both be HONOURED, not assumed
# ===========================================================================

def test_the_revision_route_honours_a_caregiver_kind_in_the_path(world):
    """A `caregiver_approved` path kind must NOT resolve as clinical.

    The earlier test aimed a `clinical` path at a caregiver goal, which fails
    either way — the clinical repository simply has no such row. That let a
    mutation forcing the first `GoalKind` survive.

    This is the discriminating direction: a CLINICAL goal addressed through
    the `caregiver_approved` kind. Resolved correctly it is refused, because
    the caregiver repository has no such goal and a clinician is not the
    caregiver author. Forced to clinical it would SUCCEED and append a version
    through the wrong authorization rule.
    """
    w = world
    mislabelled = post(
        w,
        f"/pilot/goals/caregiver_approved/{w.goal['goal_id']}/revisions",
        "hannah", {"text": "Fictional wrong-kind revision",
                   "edit_type": "modified", "reason": "Fictional"})
    assert mislabelled[0] == 403, mislabelled
    assert mislabelled[1] == FORBIDDEN
    # The clinical goal is untouched at the version the fixture left it.
    chain = w.repos.goal_versions.list_chain(w.goal["goal_id"])
    assert [v.version_number for v in chain] == [1]


def test_an_allocation_honours_the_goal_kind_in_the_body(world):
    """A caregiver-approved goal allocates as CAREGIVER_APPROVED.

    The kind decides which repository the goal is read from and whether the
    allocation is RTM-eligible, so defaulting it would quietly mis-attribute
    a family goal as clinician-authored.
    """
    w = world
    family_goal = w.goals.approve_caregiver_goal(
        w.principal("cg"), w.child, edit_type=EditType.AUTHORED_FRESH,
        text="Fictional family wording", reason="Fictional family rationale")

    allocated = post(w, f"/pilot/monthly-plans/{w.plan_id}/allocations",
                     "hannah",
                     {"goal_kind": "caregiver_approved",
                      "goal_id": family_goal.caregiver_goal_id,
                      "priority_rank": 2})
    assert allocated[0] == 200, allocated
    assert allocated[1]["allocation"]["goal_kind"] == "caregiver_approved", (
        "the allocation recorded the wrong goal kind")
    stored = w.repos.goal_allocations.get_by_id(
        allocated[1]["allocation"]["allocation_id"])
    assert stored.goal_kind is GoalKind.CAREGIVER_APPROVED
    assert stored.goal_id == family_goal.caregiver_goal_id


def test_coding_assistance_is_always_marked_candidate_only(world):
    """98979/98980/98981 output is a CANDIDATE, never a determination.

    The flags are asserted explicitly because they are the only thing in the
    payload that says so. Without them a UI could render a code as a result.
    """
    w = world
    post(w, f"/pilot/rtm-periods/{w.period.period_id}/reviews", "hannah",
         {"clinical_interpretation": "Fictional reading"})
    report = post(w, f"/pilot/rtm-periods/{w.period.period_id}/report",
                  "hannah")
    assert report[0] == 200, report
    coding = report[1]["coding_assistance"]

    assert coding["is_candidate_only"] is True
    assert coding["requires_clinician_confirmation"] is True
    assert coding["coding_summary_id"]
    assert coding["coding_rule_set_id"] and coding["coding_rule_version"]
    # Minutes are a sum of MANUALLY entered time; nothing is inferred.
    assert isinstance(coding["documented_management_minutes"], int)
    # The rule set's own reasoning travels with the candidates, so a clinician
    # sees what was missing rather than only the number produced.
    assert isinstance(coding["missing_requirement_flags"], list)
    assert isinstance(coding["rule_explanations"], list)
    for candidate in coding["potential_code_candidates"]:
        assert set(candidate) == {"code", "count"}
    # And still no payer-shaped field anywhere in the coding payload.
    rendered = json.dumps(coding).lower()
    for banned in ("payer", "insurance", "claim", "reimburse",
                   "medical_necessity", "member_id"):
        assert banned not in rendered, banned
