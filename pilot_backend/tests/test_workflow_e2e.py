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
from pilot_backend.domain.weekly_plan_document import parent_activity_ref
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

#: 0.5E-A canonical provenance, so suggestions generated here go through the
#: real anchored path and the goals they produce are allocatable. No anchor
#: row is ever written by hand: `generate_suggestions` persists the
#: SuggestionCanonicalAnchor and `approve_clinical_goal` copies it onto the
#: goal inside the same transaction.
def _rung_for(domain, *, months=24, family=None):
    from pilot_backend.domain.canonical_rung import (
        ActivityFamilyBinding,
        CanonicalRung,
    )

    return CanonicalRung.build(
        domain_key=domain, source_rung_months=months,
        milestone_text=f"Fictional canonical rung for {domain}",
        subdomain=f"{domain}_track",
        family_bindings=[ActivityFamilyBinding(family or f"{domain}_family",
                                               (domain,))],
        track_subdomains=(f"{domain}_track",),
        taxonomy_version="activity_family_taxonomy_v1",
        baseline_version="parent-2.4-functional-baseline-v1")


def _anchored_suggestion(e2e):
    """A canonical anchored suggestion, generated SERVER-SIDE.

    0.5E-A. `generate_suggestions` has no HTTP route — it is the canonical
    boundary and the only writer of `SuggestionCanonicalAnchor` — so the
    fixture reaches it through the frozen service, exactly as an
    administrative path would. The approval that follows still goes over HTTP.
    """
    from pilot_backend.goals.suggestion_engine import (
        EvidenceSource,
        ObservationSnapshot,
        ObservedDomain,
    )

    domain = "talking_and_communicating"
    offered = e2e.goals.generate_suggestions(
        e2e.principal("hannah"), e2e.child,
        ObservationSnapshot(e2e.child, MONTH, (
            ObservedDomain(domain, True,
                           EvidenceSource.CLINICIAN_OBSERVATION,
                           functional_baseline_area="requesting",
                           canonical_rung=_rung_for(domain)),)))
    return offered[0]


def _approved_goal(e2e) -> dict:
    """An ANCHORED clinical goal, approved over HTTP.

    0.5E-A: this goal is allocated into a month and drives the weekly cycle
    the rest of this file exercises, so it has to be activity-mappable.

    `modified` rather than `accepted_verbatim`, deliberately. Both are
    suggestion-derived anchored paths, but accepting verbatim would replace the
    wording with the suggestion's own template — and SENTINEL_TEXT is asserted
    downstream, including by the log-leak checks and by
    `test_frontend_reads.test_the_goal_list_carries_the_current_wording`.
    Keeping `modified` preserves every existing assertion unchanged.
    """
    suggestion = _anchored_suggestion(e2e)
    created = post(e2e, f"/pilot/children/{e2e.child}/goals", "hannah",
                   {"edit_type": "modified",
                    "suggestion_id": suggestion.suggestion_id,
                    "text": SENTINEL_TEXT,
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


def _sequence_covering_today() -> int:
    """The week of THIS month that actually contains `TODAY`.

    Previously hard-coded to 1, which made the whole file date-dependent: the
    observations below are dated `TODAY`, and `record_observation` refuses a
    date the cycle does not cover. Week 1 of a month that starts midweek is a
    PARTIAL week, so for 2026-10 sequence 1 spans 2026-10-01..2026-10-04 and
    every run from the 5th onward got a refusal that surfaced as
    `KeyError: 'observation'` — the rejection body has no such key.

    It passed only because every run of this file so far happened inside the
    first four days of October. Derived here instead, so the fixture builds the
    week the observations belong to on any date.
    """
    from pilot_backend.domain.weekly_cycle import (
        WeeklyCycleError, plan_cycle_bounds)

    for sequence in range(1, 7):
        try:
            starts_on, ends_on, _, _ = plan_cycle_bounds(MONTH, sequence)
        except WeeklyCycleError:
            # A month has 4 to 5 cycles depending on how it falls, and asking
            # for one past the end raises rather than returning empty bounds.
            # Stop at the last real week instead of propagating.
            break
        if starts_on <= TODAY <= ends_on:
            return sequence
    raise AssertionError(
        f"no cycle sequence in {MONTH} covers {TODAY}")


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
                                    sequence_in_month=_sequence_covering_today())
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

    # The SNAPSHOT comes before the release: "a cycle cannot be released
    # before its plan is snapshotted". The snapshot freezes what the
    # parent-facing plan actually contained, so a later review is reading the
    # week the family saw rather than whatever the generator would produce
    # today.
    #
    # P0-A MOVED IT AFTER ALLOCATION, and that ordering is now load-bearing.
    # The document must carry the placements' refs, and `activity_instance_ref`
    # only exists once the allocator has written the alignments. It is a
    # fictional stand-in for a released week, but it now has the SHAPE of one:
    # both the internal ref and the opaque family-facing handle, side by side,
    # which is exactly what makes the handle resolvable back to its placement.
    #
    # The previous stand-in — `{"activities": [{"ref": "..."}]}` — was an
    # opaque capture in Parent's own plan shape, carrying neither ref. The
    # observation route accepted the internal ref directly then, so the
    # snapshot's contents never mattered to it. Under the opaque-handle
    # contract they do.
    alignments = e2e.weekly.list_alignments(e2e.principal("hannah"),
                                            cycle.cycle_id)
    e2e.weekly.capture_snapshot(
        e2e.principal("hannah"), cycle.cycle_id, SourceSystem.PARENT,
        "fictional-parent-plan-1",
        {"cycle_id": cycle.cycle_id,
         "child_id": cycle.child_id,
         "activities": [
             {"activity_instance_ref": a.activity_instance_ref,
              "activity_ref": parent_activity_ref(a.activity_instance_ref)}
             for a in alignments]})
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
    # P0-A: what a FAMILY can name. The released week exposes only the opaque
    # handle, so every caregiver-facing HTTP call below cites one of these and
    # never an entry of `activity_refs` above. Derived with the product's own
    # function rather than copied, so a change to the scheme reaches the tests.
    e2e.parent_activity_refs = [parent_activity_ref(ref)
                                for ref in e2e.activity_refs]
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
    # Cites the OPAQUE handle the released week showed, not the internal ref.
    observed = post(w, f"/pilot/cycles/{w.cycle_id}/observations", "cg",
                    {"activity_ref": w.parent_activity_refs[0],
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
                    {"activity_ref": w.parent_activity_refs[0],
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
                    {"activity_ref": w.parent_activity_refs[0],
                     "local_date": TODAY, "attempt_outcome": "did_it",
                     "difficulty": "too_hard", "enjoyment": "disliked"})
    payload = observed[1]["observation"]
    assert set(payload) == {
        "event_id", "activity_ref", "local_date",
        "attribution_month", "timezone_of_record", "attempt_outcome",
        "difficulty", "enjoyment"}
    # The three free-text domain fields exist and must NOT be serialised.
    # P0-A adds `activity_instance_ref` to that list: the route is
    # caregiver-facing, and the allocator's ref embeds the reviewed template's
    # content digest, so echoing it back would undo what the released week
    # withholds.
    for banned in ("child_response", "assistance", "observation_text_ref",
                   "note", "comment", "recorded_by_caregiver_id",
                   "activity_instance_ref"):
        assert banned not in payload, banned
    assert payload["activity_ref"] == w.parent_activity_refs[0]
    assert payload["difficulty"] == "too_hard"
    assert payload["enjoyment"] == "disliked"


def test_the_observation_list_payload_has_the_same_shape(world):
    """A leak through the LIST route would be just as bad as through create."""
    w = world
    post(w, f"/pilot/cycles/{w.cycle_id}/observations", "cg",
         {"activity_ref": w.parent_activity_refs[0],
          "local_date": TODAY, "attempt_outcome": "did_it"})
    listed = get(w, f"/pilot/cycles/{w.cycle_id}/observations", "hannah")
    for row in listed[1]["observations"]:
        assert "child_response" not in row
        assert "assistance" not in row
        assert "activity_instance_ref" not in row
        assert set(row) == {
            "event_id", "activity_ref", "local_date",
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


# ===========================================================================
# P0-A — PARENT FEEDBACK -> CANONICAL ObservationEvent
#
# The Parent-facing identity contract. The released week exposes only the
# opaque `activity_ref`, so feedback has to name an activity by that handle and
# the server resolves it back to the placement. Before this slice the route
# required the internal `activity_instance_ref` — a value the family is never
# given, and one that EMBEDS the reviewed template's content digest.
#
# Persistence and identity only: no clinical inference, no mastery, no
# adaptation. "Too easy" here is a recorded parent observation and nothing more.
# ===========================================================================


def _observe(w, body, token="cg"):
    """POST feedback as the caregiver. `(status, body)` — headers unused here."""
    status, payload = post(
        w, f"/pilot/cycles/{w.cycle_id}/observations", token, body)[:2]
    return status, payload


def test_the_opaque_activity_ref_resolves_to_the_released_placement(world):
    """The handle the family was shown identifies the right placement."""
    w = world
    handle = w.parent_activity_refs[0]
    status, body = _observe(w, {"activity_ref": handle, "local_date": TODAY,
                                "attempt_outcome": "did_it"})
    assert status == 200, body

    # It resolved to the placement that handle is a digest OF — checked on the
    # STORED event, so this is the persisted linkage and not just the echo.
    stored = w.weekly.list_observations(w.principal("hannah"), w.cycle_id)
    event = [e for e in stored
             if e.event_id == body["observation"]["event_id"]][0]
    assert event.activity_instance_ref == w.activity_refs[0]
    assert parent_activity_ref(event.activity_instance_ref) == handle
    # ...and the response gives back only the handle.
    assert body["observation"]["activity_ref"] == handle


def test_the_raw_internal_instance_ref_is_refused_from_the_parent_route(world):
    """The privacy boundary is one-directional.

    The internal ref is not allowlisted, so a client sending one is refused by
    the body reader before any service call — the refusal does not depend on
    the resolver noticing. A client that somehow learned a real placement ref
    still cannot use it here.
    """
    w = world
    status, body = _observe(w, {"activity_instance_ref": w.activity_refs[0],
                                "local_date": TODAY,
                                "attempt_outcome": "did_it"})
    assert status != 200, body
    # The legitimate handle for that same placement DOES work, so the refusal
    # is about the identifier and not about the activity or the caregiver.
    ok, _ = _observe(w, {"activity_ref": w.parent_activity_refs[0],
                         "local_date": TODAY, "attempt_outcome": "did_it"})
    assert ok == 200


def test_sending_the_handle_under_the_old_field_name_is_also_refused(world):
    """Belt and braces: the handle is only accepted as `activity_ref`."""
    w = world
    status, _ = _observe(w, {"activity_instance_ref":
                             w.parent_activity_refs[0],
                             "local_date": TODAY,
                             "attempt_outcome": "did_it"})
    assert status != 200


def test_an_unknown_or_foreign_activity_ref_fails_closed(world):
    """A well-formed handle that this released week does not contain.

    Covers the cross-cycle and cross-child cases in one: resolution scans the
    released document for THIS cycle, so a handle from any other week — or one
    simply invented with the right scheme — has nowhere to match.
    """
    w = world
    foreign = parent_activity_ref("some-other-cycle::some-activity::0")
    assert foreign.startswith("pact1:")
    status, _ = _observe(w, {"activity_ref": foreign, "local_date": TODAY,
                             "attempt_outcome": "did_it"})
    assert status != 200

    # A syntactically valid but absent handle, and a value from the wrong
    # namespace entirely, are both refused.
    for bad in (parent_activity_ref("invented"), "pact1:" + "0" * 32,
                "not-a-ref", ""):
        refused, _ = _observe(w, {"activity_ref": bad, "local_date": TODAY,
                                  "attempt_outcome": "did_it"})
        assert refused != 200, bad


def test_the_beta_feedback_vocabulary_maps_onto_the_canonical_enums(world):
    """Beta's three triples land on the canonical enum members.

    The mapping is asserted on the PERSISTED event, so a transport that
    accepted a value and dropped it would fail here. `enjoyment` is the only
    one whose wire words differ from the stored names
    (`loved_it`/`it_was_okay`/`not_really` -> enjoyed/neutral/disliked), and
    the Parent client is responsible for that translation; the Pilot route
    speaks the canonical vocabulary.
    """
    w = world
    cases = [
        ("did_it", "too_easy", "enjoyed"),
        ("didnt_want_to_try", "just_right", "neutral"),
        ("wasnt_ready_yet", "too_hard", "disliked"),
    ]
    for index, (outcome, difficulty, enjoyment) in enumerate(cases):
        status, body = _observe(w, {
            "activity_ref": w.parent_activity_refs[0],
            "local_date": TODAY, "attempt_outcome": outcome,
            "difficulty": difficulty, "enjoyment": enjoyment,
            "source_feedback_id": f"fb_vocab_{index}"})
        assert status == 200, body
        event_id = body["observation"]["event_id"]
        stored = [e for e in w.weekly.list_observations(
            w.principal("hannah"), w.cycle_id) if e.event_id == event_id][0]
        assert stored.attempt_outcome.value == outcome
        assert stored.difficulty.value == difficulty
        assert stored.enjoyment.value == enjoyment
        # No clinical field was inferred from any of it.
        assert stored.assistance == ""
        assert stored.child_response == ""
        assert stored.observation_text_ref == ""


def test_the_caregiver_identity_is_server_derived(world):
    """`recorded_by_caregiver_id` comes from the token, never from the body.

    It is also absent from the response, so the route neither accepts nor
    reveals it.
    """
    w = world
    status, body = _observe(w, {"activity_ref": w.parent_activity_refs[0],
                                "local_date": TODAY,
                                "attempt_outcome": "did_it"})
    assert status == 200, body
    assert "recorded_by_caregiver_id" not in body["observation"]

    stored = [e for e in w.weekly.list_observations(
        w.principal("hannah"), w.cycle_id)
        if e.event_id == body["observation"]["event_id"]][0]
    expected = w.principal("cg").application_id
    assert stored.recorded_by_caregiver_id == expected

    # A body attempting to set it is refused rather than silently ignored.
    refused, _ = _observe(w, {"activity_ref": w.parent_activity_refs[0],
                              "local_date": TODAY,
                              "attempt_outcome": "did_it",
                              "recorded_by_caregiver_id": "cg_someone_else"})
    assert refused != 200


def test_a_retried_submission_replays_instead_of_duplicating_evidence(world):
    """IDEMPOTENCY on the upstream feedback id.

    `source_feedback_id` was stored but enforced nowhere, so a retry produced
    TWO events for one attempt. That is not cosmetic: the adaptation layer
    counts consecutive signals, so a retried "too hard" could manufacture a
    threshold the family never reported.
    """
    w = world
    payload = {"activity_ref": w.parent_activity_refs[0],
               "local_date": TODAY, "attempt_outcome": "did_it",
               "difficulty": "too_hard", "source_feedback_id": "fb_retry_1"}

    first_status, first = _observe(w, payload)
    assert first_status == 200, first
    second_status, second = _observe(w, payload)
    assert second_status == 200, second

    # Same event, not a second one — a replay, not an error the client has to
    # interpret.
    assert (second["observation"]["event_id"]
            == first["observation"]["event_id"])
    events = [e for e in w.weekly.list_observations(w.principal("hannah"),
                                                    w.cycle_id)
              if e.source_feedback_id == "fb_retry_1"]
    assert len(events) == 1
    assert events[0].source_feedback_id == "fb_retry_1"

    # A DIFFERENT upstream id is genuinely new evidence.
    other_status, other = _observe(w, dict(payload,
                                           source_feedback_id="fb_retry_2"))
    assert other_status == 200, other
    assert (other["observation"]["event_id"]
            != first["observation"]["event_id"])


def test_without_a_source_feedback_id_each_submission_is_distinct(world):
    """The explicit limit of the guarantee.

    With no upstream id this layer cannot invent an identity the caller did
    not give it, so two submissions are two events. Stated as a test so the
    boundary is visible rather than discovered later: the Parent bridge must
    pass the `fb_...` id to get replay protection.
    """
    w = world
    payload = {"activity_ref": w.parent_activity_refs[0],
               "local_date": TODAY, "attempt_outcome": "did_it"}
    first = _observe(w, payload)[1]["observation"]["event_id"]
    second = _observe(w, payload)[1]["observation"]["event_id"]
    assert first != second


def test_recording_feedback_does_not_mutate_the_released_snapshot(world):
    """Evidence accumulates beside the week; it never edits it."""
    w = world
    before = get(w, f"/pilot/children/{w.child}/current-cycle")[1]
    _observe(w, {"activity_ref": w.parent_activity_refs[0],
                 "local_date": TODAY, "attempt_outcome": "didnt_want_to_try",
                 "difficulty": "too_hard", "enjoyment": "disliked"})
    after = get(w, f"/pilot/children/{w.child}/current-cycle")[1]
    assert after["plan_snapshot"] == before["plan_snapshot"]
    assert after["cycle"] == before["cycle"]


def test_the_feedback_path_touches_no_candidate_or_generated_content(world):
    """P0-A is persistence and identity only.

    Recording feedback must not reach the clinical-review candidate set, create
    an adaptation, or alter a goal. Asserted over the whole store so a new
    write anywhere would surface.
    """
    w = world
    untouched = ("pilot_adaptation_records", "pilot_goal_versions",
                 "pilot_clinical_goals", "pilot_weekly_plan_snapshots",
                 "pilot_capacity_ledgers", "pilot_alignments")
    before = {name: len(list(w.repos.store.list_all(name)))
              for name in untouched}

    status, body = _observe(w, {"activity_ref": w.parent_activity_refs[0],
                                "local_date": TODAY,
                                "attempt_outcome": "did_it",
                                "difficulty": "too_easy",
                                "source_feedback_id": "fb_no_side_effects"})
    assert status == 200, body

    after = {name: len(list(w.repos.store.list_all(name)))
             for name in untouched}
    assert after == before, "feedback caused a write outside the evidence path"
    # The only new row is the observation itself.
    assert any(e.event_id == body["observation"]["event_id"]
               for e in w.weekly.list_observations(w.principal("hannah"),
                                                   w.cycle_id))


# ---------------------------------------------------------------------------
# P0-A gap 2 — exact replay vs CONFLICTING reuse of one upstream id
# ---------------------------------------------------------------------------


def test_reusing_one_feedback_id_for_a_different_observation_conflicts(world):
    """One upstream id must mean ONE observation.

    An exact repeat is a retry and replays. Reusing the id for a different
    activity, date, outcome or rating is a caller defect, and replaying the old
    event would answer 200 to a request the server did not honour — the client
    would believe its new reading was stored.
    """
    w = world
    base = {"activity_ref": w.parent_activity_refs[0], "local_date": TODAY,
            "attempt_outcome": "did_it", "difficulty": "too_hard",
            "enjoyment": "disliked", "source_feedback_id": "fb_conflict_1"}
    first_status, first = _observe(w, base)
    assert first_status == 200, first
    original_id = first["observation"]["event_id"]

    # Each of these differs from the stored observation in exactly one of the
    # compared fields, and each must be refused rather than replayed.
    conflicts = (
        dict(base, attempt_outcome="wasnt_ready_yet"),
        dict(base, difficulty="too_easy"),
        dict(base, enjoyment="enjoyed"),
        dict(base, activity_ref=w.parent_activity_refs[1]),
    )
    for variant in conflicts:
        status, _ = _observe(w, variant)
        assert status != 200, variant

        # And the original is untouched — a refused conflict writes nothing.
        stored = [e for e in w.weekly.list_observations(
            w.principal("hannah"), w.cycle_id) if e.event_id == original_id]
        assert len(stored) == 1
        assert stored[0].attempt_outcome.value == "did_it"
        assert stored[0].difficulty.value == "too_hard"
        assert stored[0].enjoyment.value == "disliked"
        assert stored[0].activity_instance_ref == w.activity_refs[0]

    # Exactly one event still carries that id: no conflict created a second.
    assert len([e for e in w.weekly.list_observations(w.principal("hannah"),
                                                      w.cycle_id)
                if e.source_feedback_id == "fb_conflict_1"]) == 1

    # The genuine exact repeat still replays.
    replay_status, replay = _observe(w, base)
    assert replay_status == 200
    assert replay["observation"]["event_id"] == original_id


def test_a_retry_is_not_made_a_conflict_by_server_timestamps(world):
    """Server-stamped values must be excluded from the identity comparison.

    `occurred_at` and `created_at` are stamped per request, so comparing them
    would make every legitimate retry a conflict — the opposite of the
    guarantee. Two deliveries at different moments must still replay.
    """
    w = world
    payload = {"activity_ref": w.parent_activity_refs[0],
               "local_date": TODAY, "attempt_outcome": "did_it",
               "source_feedback_id": "fb_clock_1"}
    first = _observe(w, payload)[1]["observation"]["event_id"]
    # The fixture's clock advances between calls, so the second delivery
    # carries a different stamp.
    second = _observe(w, payload)[1]["observation"]["event_id"]
    assert first == second

    from pilot_backend.domain.observation import OBSERVATION_IDENTITY_FIELDS

    for stamped in ("occurred_at", "created_at", "attribution_month",
                    "event_id", "timezone_of_record", "tz_source"):
        assert stamped not in OBSERVATION_IDENTITY_FIELDS, stamped


# ---------------------------------------------------------------------------
# P0-A gap 3 — revision / supersession, and the CURRENT observation rule
# ---------------------------------------------------------------------------


def _current(w):
    from pilot_backend.domain.observation import current_observations

    return current_observations(
        w.weekly.list_observations(w.principal("hannah"), w.cycle_id))


def test_one_original_feedback_is_one_current_observation(world):
    w = world
    status, body = _observe(w, {"activity_ref": w.parent_activity_refs[0],
                                "local_date": TODAY,
                                "attempt_outcome": "did_it",
                                "difficulty": "too_hard",
                                "source_feedback_id": "fb_rev_a1"})
    assert status == 200, body
    mine = [e for e in _current(w) if e.source_feedback_id == "fb_rev_a1"]
    assert len(mine) == 1
    assert mine[0].supersedes_source_feedback_id == ""


def test_an_exact_retry_is_still_one_current_observation(world):
    w = world
    payload = {"activity_ref": w.parent_activity_refs[0],
               "local_date": TODAY, "attempt_outcome": "did_it",
               "difficulty": "too_hard", "source_feedback_id": "fb_rev_b1"}
    _observe(w, payload)
    _observe(w, payload)
    assert len([e for e in _current(w)
                if e.source_feedback_id == "fb_rev_b1"]) == 1


def test_a_revision_keeps_both_rows_but_only_one_is_current(world):
    """THE SIGNAL-COUNTING PROTECTION.

    A parent reporting "too hard" and then editing it to "just right" must not
    leave two independent readings behind. Both rows are retained — nothing is
    overwritten — and exactly one is CURRENT.
    """
    w = world
    original = _observe(w, {"activity_ref": w.parent_activity_refs[0],
                            "local_date": TODAY, "attempt_outcome": "did_it",
                            "difficulty": "too_hard",
                            "source_feedback_id": "fb_rev_c1"})
    assert original[0] == 200, original

    revised = _observe(w, {"activity_ref": w.parent_activity_refs[0],
                           "local_date": TODAY, "attempt_outcome": "did_it",
                           "difficulty": "just_right",
                           "source_feedback_id": "fb_rev_c2",
                           "supersedes_source_feedback_id": "fb_rev_c1"})
    assert revised[0] == 200, revised

    everything = w.weekly.list_observations(w.principal("hannah"), w.cycle_id)
    lineage = [e for e in everything
               if e.source_feedback_id in ("fb_rev_c1", "fb_rev_c2")]
    # APPEND-ONLY: two immutable historical rows.
    assert len(lineage) == 2
    original_row = [e for e in lineage
                    if e.source_feedback_id == "fb_rev_c1"][0]
    assert original_row.difficulty.value == "too_hard", "history was rewritten"

    # ...and exactly ONE current reading, the revision.
    current = [e for e in _current(w)
               if e.source_feedback_id in ("fb_rev_c1", "fb_rev_c2")]
    assert len(current) == 1
    assert current[0].source_feedback_id == "fb_rev_c2"
    assert current[0].difficulty.value == "just_right"
    assert current[0].supersedes_source_feedback_id == "fb_rev_c1"

    # The counting consequence, stated directly: one too-hard reading, not two.
    too_hard = [e for e in _current(w)
                if e.difficulty and e.difficulty.value == "too_hard"
                and e.source_feedback_id in ("fb_rev_c1", "fb_rev_c2")]
    assert too_hard == []


def test_a_revision_cannot_supersede_another_activity_or_cycle(world):
    """A revision may only replace feedback about the SAME activity.

    Otherwise a revision about one activity could silently retire another
    activity's reading — changing what the adaptation layer counts while every
    row stayed intact and looked correct.
    """
    w = world
    _observe(w, {"activity_ref": w.parent_activity_refs[0],
                 "local_date": TODAY, "attempt_outcome": "did_it",
                 "difficulty": "too_hard", "source_feedback_id": "fb_rev_d1"})

    # Same cycle, DIFFERENT activity -> refused.
    status, _ = _observe(w, {"activity_ref": w.parent_activity_refs[1],
                             "local_date": TODAY,
                             "attempt_outcome": "did_it",
                             "source_feedback_id": "fb_rev_d2",
                             "supersedes_source_feedback_id": "fb_rev_d1"})
    assert status != 200

    # An upstream id that was never recorded for this child -> refused.
    unknown, _ = _observe(w, {"activity_ref": w.parent_activity_refs[0],
                              "local_date": TODAY,
                              "attempt_outcome": "did_it",
                              "source_feedback_id": "fb_rev_d3",
                              "supersedes_source_feedback_id": "fb_never"})
    assert unknown != 200

    # Self-supersession -> refused.
    itself, _ = _observe(w, {"activity_ref": w.parent_activity_refs[0],
                             "local_date": TODAY,
                             "attempt_outcome": "did_it",
                             "source_feedback_id": "fb_rev_d4",
                             "supersedes_source_feedback_id": "fb_rev_d4"})
    assert itself != 200

    # The original reading survived every refused attempt, and is still current.
    current = [e for e in _current(w)
               if e.source_feedback_id == "fb_rev_d1"]
    assert len(current) == 1
    assert current[0].difficulty.value == "too_hard"


def test_the_supersession_field_survives_the_codec_round_trip(world):
    """The lineage must survive encode/decode, not just live in memory."""
    w = world
    _observe(w, {"activity_ref": w.parent_activity_refs[0],
                 "local_date": TODAY, "attempt_outcome": "did_it",
                 "source_feedback_id": "fb_codec_1"})
    _observe(w, {"activity_ref": w.parent_activity_refs[0],
                 "local_date": TODAY, "attempt_outcome": "wasnt_ready_yet",
                 "source_feedback_id": "fb_codec_2",
                 "supersedes_source_feedback_id": "fb_codec_1"})

    # Read back THROUGH the repository, so this is the decoded document.
    reloaded = [e for e in w.weekly.list_observations(w.principal("hannah"),
                                                      w.cycle_id)
                if e.source_feedback_id == "fb_codec_2"][0]
    assert reloaded.supersedes_source_feedback_id == "fb_codec_1"

    # And it is in the stored document itself, not only on the object.
    raw = [d for _i, d in w.repos.store.list_all("pilot_observation_events")
           if d.get("source_feedback_id") == "fb_codec_2"][0]
    assert raw["supersedes_source_feedback_id"] == "fb_codec_1"


def test_the_supersession_field_is_not_exposed_to_the_parent_payload(world):
    """Lineage is internal provenance; the response shape is unchanged."""
    w = world
    status, body = _observe(w, {"activity_ref": w.parent_activity_refs[0],
                                "local_date": TODAY,
                                "attempt_outcome": "did_it",
                                "source_feedback_id": "fb_shape_1"})
    assert status == 200, body
    assert set(body["observation"]) == {
        "event_id", "activity_ref", "local_date", "attribution_month",
        "timezone_of_record", "attempt_outcome", "difficulty", "enjoyment"}
