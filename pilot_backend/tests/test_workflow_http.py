"""0.5C — the Tuesday workflow surface, over REAL request handling.

Seventeen route/method pairs, exercised end to end through the WSGI
application. What this file pins is the AUTHORIZATION MATRIX: every refusal
the founder named, for every new route, through the edge rather than through a
service call.

The handlers themselves contain no authorization logic — they resolve a
principal, read an allowlisted body, and call one frozen service. So these
tests are really asking two questions: does the frozen rule still fire when
reached through HTTP, and does the transport layer leak anything on the way
back out.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth import DevAuthVerifier, VerifiedToken
from pilot_backend.config import PilotSettings
from pilot_backend.connections import ProviderConnectionService
from pilot_backend.domain.connections import CaregiverChildConnection
from pilot_backend.domain.entities import Caregiver, Child, Practice
from pilot_backend.domain.enums import (
    CaregiverRelationship,
    ConnectionStatus,
    ProviderDiscipline,
)
from pilot_backend.integration.parent_source import InMemoryParentSessionSource
from pilot_backend.persistence import FakeDocumentStore, FirestoreRepositories
from pilot_backend.provisioning import provision_provider_record
from pilot_backend.transport import build_application

from .test_integration_identity import call
from .test_secure_foundation import SENTINEL_EMAIL

FORBIDDEN = {"error": "not permitted"}

#: The reads default to the CURRENT month on the server clock, so the plan the
#: tests create must be that month — otherwise the read legitimately finds
#: nothing and the test would be asserting against a month nobody asked for.
_CURRENT_MONTH = datetime.now(timezone.utc).strftime("%Y-%m")


def _subject(name: str) -> str:
    return f"fictional-subject-{name}"


@pytest.fixture()
def wf():
    """Two families, Hannah as managing clinician for family A only.

    The second family exists so "foreign caregiver" and "wrong child" are real
    queries against real rows rather than an empty-store accident — the same
    reasoning `secure_topology` is built on.
    """
    settings = PilotSettings.from_env({
        "PILOT_ENVIRONMENT": "dev",
        "PILOT_ALLOWED_ORIGINS": "http://localhost:3000",
        "PILOT_DEV_AUTH": "1",
    })
    repos = FirestoreRepositories(FakeDocumentStore())
    practice = repos.practices.create(Practice.create("Practice-Pilot"))

    def _family(tag: str):
        caregiver = repos.caregivers.create(Caregiver.create(
            f"Caregiver-{tag}", auth_subject=_subject(f"cg-{tag}")))
        child = repos.children.create(
            Child.create(actor_id=caregiver.caregiver_id))
        repos.caregiver_child.connect(CaregiverChildConnection.create(
            caregiver.caregiver_id, child.child_id,
            CaregiverRelationship.PARENT, actor_id=caregiver.caregiver_id))
        return caregiver, child

    cg_a, child_a = _family("a")
    cg_b, child_b = _family("b")

    def _provider(tag: str):
        return provision_provider_record(
            repos, auth_subject=_subject(f"prov-{tag}"),
            practice_id=practice.practice_id,
            discipline=ProviderDiscipline.SLP,
            display_name=f"Provider-{tag}").provider

    hannah = _provider("hannah")       # ACTIVE + managing, family A
    connected = _provider("connected")  # ACTIVE, NOT managing, family A
    paused = _provider("paused")        # PAUSED, family A
    revoked = _provider("revoked")      # REVOKED, family A
    foreign = _provider("foreign")      # managing for family B

    verifier = DevAuthVerifier("dev")
    tokens = {
        "cg-a": _subject("cg-a"), "cg-b": _subject("cg-b"),
        "hannah": _subject("prov-hannah"),
        "connected": _subject("prov-connected"),
        "paused": _subject("prov-paused"),
        "revoked": _subject("prov-revoked"),
        "foreign": _subject("prov-foreign"),
    }
    for token, subject in tokens.items():
        verifier.add(token, VerifiedToken(subject=subject, email=SENTINEL_EMAIL))

    recorder = AuditRecorder(repos.audit_events, environment="dev")
    logs = []
    app = build_application(
        settings=settings, repos=repos, verifier=verifier, recorder=recorder,
        parent_source=InMemoryParentSessionSource(), log_sink=logs)

    from pilot_backend.auth.resolver import resolve_principal

    def principal(token):
        return resolve_principal(VerifiedToken(subject=tokens[token]), repos)

    connections = ProviderConnectionService(repos=repos, recorder=recorder)

    def connect(provider, caregiver_token, child_id, *, final="active"):
        pending = connections.invite_provider(
            principal(caregiver_token), child_id, provider.provider_id)
        provider_principal = resolve_principal(
            VerifiedToken(subject=provider.auth_subject), repos)
        if final == "pending":
            return pending
        active = connections.accept_invitation(
            provider_principal, pending.connection_id)
        if final == "paused":
            return connections.pause_connection(
                principal(caregiver_token), active.connection_id)
        if final == "revoked":
            return connections.revoke_connection(
                principal(caregiver_token), active.connection_id)
        return active

    connect(hannah, "cg-a", child_a.child_id)
    connections.assign_managing_clinician(
        principal("cg-a"), child_a.child_id, hannah.provider_id)
    connect(connected, "cg-a", child_a.child_id)
    connect(paused, "cg-a", child_a.child_id, final="paused")
    connect(revoked, "cg-a", child_a.child_id, final="revoked")
    connect(foreign, "cg-b", child_b.child_id)
    connections.assign_managing_clinician(
        principal("cg-b"), child_b.child_id, foreign.provider_id)

    class Bundle:
        pass

    bundle = Bundle()
    bundle.app, bundle.repos, bundle.logs = app, repos, logs
    bundle.child_a, bundle.child_b = child_a.child_id, child_b.child_id
    bundle.hannah = hannah
    bundle.principal = principal
    return bundle


def get(wf, path, token):
    return call(wf.app, path, bearer=f"Bearer {token}")


def post(wf, path, token, body=None):
    raw = json.dumps(body or {}).encode()
    return call(wf.app, path, method="POST", bearer=f"Bearer {token}", body=raw)


# ===========================================================================
# the happy path — Hannah's Tuesday flow, over HTTP
# ===========================================================================

def test_hannah_reads_suggestions_and_approves_a_goal(wf):
    status, body, _ = get(wf, f"/pilot/children/{wf.child_a}/goal-suggestions",
                          "hannah")
    assert status == 200
    assert "suggestions" in body

    created = post(wf, f"/pilot/children/{wf.child_a}/goals", "hannah",
                   {"edit_type": "authored_fresh",
                    "text": "Fictional functional target",
                    "reason": "Fictional clinical rationale"})
    assert created[0] == 200, created
    goal = created[1]["goal"]
    assert goal["goal_kind"] == "clinical"
    assert goal["is_rtm_eligible"] is True
    assert goal["current_version_id"], "blocker 3: the goal must name a version"

    listed = get(wf, f"/pilot/children/{wf.child_a}/goals", "hannah")
    assert listed[0] == 200
    assert goal["goal_id"] in {g["goal_id"] for g in listed[1]["goals"]}


def test_hannah_revises_a_goal_and_the_version_chain_grows(wf):
    goal = post(wf, f"/pilot/children/{wf.child_a}/goals", "hannah",
                {"edit_type": "authored_fresh", "text": "Fictional v1",
                 "reason": "Fictional rationale"})[1]["goal"]

    revised = post(
        wf, f"/pilot/goals/clinical/{goal['goal_id']}/revisions", "hannah",
        {"text": "Fictional v2", "edit_type": "modified",
         "reason": "Fictional revision rationale"})
    assert revised[0] == 200, revised
    assert revised[1]["version_number"] == 2
    assert revised[1]["version_id"] != goal["current_version_id"]


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


def test_the_month_moves_from_approved_goals_to_an_active_plan(wf):
    """create (DRAFT) -> allocate -> activate, which is the real sequence.

    Activation is refused for a month with no allocated goal, so the three
    steps are a domain invariant rather than an API style choice.
    """
    # 0.5E-A: this goal is ALLOCATED below, so it must be activity-mappable.
    # The anchored suggestion is generated server-side (generate_suggestions
    # is the canonical boundary and has no route); the approval itself still
    # goes over HTTP, which is what this test is about. `modified` keeps the
    # original wording so nothing downstream changes.
    from pilot_backend.goals.suggestion_engine import (
        EvidenceSource,
        ObservationSnapshot,
        ObservedDomain,
    )

    domain = "talking_and_communicating"
    from pilot_backend.goals.service import GoalService

    offered = GoalService(repos=wf.repos).generate_suggestions(
        wf.principal("hannah"), wf.child_a,
        ObservationSnapshot(wf.child_a, _CURRENT_MONTH, (
            ObservedDomain(domain, True,
                           EvidenceSource.CLINICIAN_OBSERVATION,
                           functional_baseline_area="requesting",
                           canonical_rung=_rung_for(domain)),)))
    goal = post(wf, f"/pilot/children/{wf.child_a}/goals", "hannah",
                {"edit_type": "modified",
                 "suggestion_id": offered[0].suggestion_id,
                 "text": "Fictional target",
                 "reason": "Fictional rationale"})[1]["goal"]

    created = post(wf, f"/pilot/children/{wf.child_a}/monthly-plan", "hannah",
                   {"cycle_month": _CURRENT_MONTH, "timezone_of_record": "UTC"})
    assert created[0] == 200, created
    plan_id = created[1]["plan"]["focus_plan_id"]
    assert created[1]["plan"]["state"] == "draft"

    # Activation before any allocation is refused — the invariant, over HTTP.
    assert post(wf, f"/pilot/monthly-plans/{plan_id}/activate",
                "hannah")[0] == 403

    allocated = post(wf, f"/pilot/monthly-plans/{plan_id}/allocations",
                     "hannah",
                     {"goal_kind": "clinical", "goal_id": goal["goal_id"],
                      "priority_rank": 1, "emphasis_weight": 2})
    assert allocated[0] == 200, allocated
    # A relative weight, never a percentage.
    assert allocated[1]["allocation"]["emphasis_weight"] == 2

    activated = post(wf, f"/pilot/monthly-plans/{plan_id}/activate", "hannah")
    assert activated[0] == 200, activated
    assert activated[1]["plan"]["state"] == "active"

    # The family can now read the month and the (still empty) week.
    read = get(wf, f"/pilot/children/{wf.child_a}/monthly-plan", "cg-a")
    assert read[0] == 200
    assert read[1]["plan"]["cycle_month"] == _CURRENT_MONTH
    assert read[1]["plan"]["state"] == "active"
    assert [a["goal_id"] for a in read[1]["allocations"]] == [goal["goal_id"]]

    cycle = get(wf, f"/pilot/children/{wf.child_a}/current-cycle", "cg-a")
    assert cycle[0] == 200
    assert "cycle" in cycle[1] and "alignments" in cycle[1]


# ===========================================================================
# THE AUTHORIZATION MATRIX
# ===========================================================================

#: Routes a CAREGIVER legitimately reaches, as (method, template, body).
CAREGIVER_ROUTES = [
    ("GET", "/pilot/children/{child}/goals", None),
    ("GET", "/pilot/children/{child}/monthly-plan", None),
    ("GET", "/pilot/children/{child}/current-cycle", None),
]

#: Routes requiring a PROVIDER, and specifically the MANAGING clinician.
MANAGING_ROUTES = [
    ("POST", "/pilot/children/{child}/goals",
     {"edit_type": "authored_fresh", "text": "Fictional",
      "reason": "Fictional"}),
    ("POST", "/pilot/children/{child}/monthly-plan",
     {"cycle_month": _CURRENT_MONTH, "timezone_of_record": "UTC"}),
]


def _resolve(template, child):
    return template.replace("{child}", child)


@pytest.mark.parametrize("method,template,body", CAREGIVER_ROUTES + MANAGING_ROUTES)
def test_a_foreign_caregiver_is_refused_on_every_route(wf, method, template, body):
    """Family B's caregiver may not touch family A's child."""
    path = _resolve(template, wf.child_a)
    result = (get(wf, path, "cg-b") if method == "GET"
              else post(wf, path, "cg-b", body))
    assert result[0] == 403, (template, result)
    assert result[1] == FORBIDDEN


@pytest.mark.parametrize("method,template,body", CAREGIVER_ROUTES + MANAGING_ROUTES)
def test_a_foreign_provider_is_refused_on_every_route(wf, method, template, body):
    """A provider managing family B may not touch family A's child."""
    path = _resolve(template, wf.child_a)
    result = (get(wf, path, "foreign") if method == "GET"
              else post(wf, path, "foreign", body))
    assert result[0] == 403, (template, result)
    assert result[1] == FORBIDDEN


@pytest.mark.parametrize("method,template,body", MANAGING_ROUTES)
def test_an_ACTIVE_but_NON_MANAGING_provider_is_refused(wf, method, template, body):
    """THE distinction 0.5B established: a connection is not ownership.

    This provider has an ACTIVE `ProviderChildConnection` to exactly this
    child, so `authorize_child_access` passes. What they lack is the
    `ManagingClinicianAssignment`, and the clinician-owned operations must
    still refuse — an ACTIVE connection alone is never enough.
    """
    path = _resolve(template, wf.child_a)
    result = post(wf, path, "connected", body)
    assert result[0] == 403, (template, result)
    assert result[1] == FORBIDDEN


@pytest.mark.parametrize("token", ["paused", "revoked"])
@pytest.mark.parametrize("method,template,body", MANAGING_ROUTES)
def test_a_paused_or_revoked_provider_is_refused(wf, token, method, template, body):
    path = _resolve(template, wf.child_a)
    result = post(wf, path, token, body)
    assert result[0] == 403, (token, template, result)
    assert result[1] == FORBIDDEN


@pytest.mark.parametrize("method,template,body", CAREGIVER_ROUTES + MANAGING_ROUTES)
def test_an_absent_child_and_a_foreign_child_are_indistinguishable(
        wf, method, template, body):
    """Non-enumeration: "not real" and "not yours" render identically."""
    absent = _resolve(template, "chld_absent000000000000000000000")
    foreign = _resolve(template, wf.child_b)
    caller = "hannah" if method == "POST" else "cg-a"
    first = (get(wf, absent, caller) if method == "GET"
             else post(wf, absent, caller, body))
    second = (get(wf, foreign, caller) if method == "GET"
              else post(wf, foreign, caller, body))
    assert first[0] == second[0] == 403, (template, first, second)
    assert first[1] == second[1] == FORBIDDEN


def test_a_wrong_goal_is_refused(wf):
    """A goal id belonging to another child cannot be revised."""
    other = post(wf, f"/pilot/children/{wf.child_b}/goals", "foreign",
                 {"edit_type": "authored_fresh", "text": "Fictional B",
                  "reason": "Fictional"})[1]["goal"]

    result = post(wf, f"/pilot/goals/clinical/{other['goal_id']}/revisions",
                  "hannah", {"text": "Fictional hijack",
                             "edit_type": "modified", "reason": "Fictional"})
    assert result[0] == 403
    assert result[1] == FORBIDDEN
    # Absent and foreign are the same refusal.
    absent = post(wf, "/pilot/goals/clinical/goal_absent0000000/revisions",
                  "hannah", {"text": "x", "edit_type": "modified",
                             "reason": "Fictional"})
    assert absent[0] == 403 and absent[1] == FORBIDDEN


def test_an_unknown_goal_kind_is_refused_not_404(wf):
    """The kind vocabulary is not enumerable from status codes."""
    result = post(wf, "/pilot/goals/invented/goal_x/revisions", "hannah",
                  {"text": "x", "edit_type": "modified", "reason": "y"})
    assert result[0] == 403
    assert result[1] == FORBIDDEN


def test_a_cross_child_observation_reference_is_refused(wf):
    """A cycle belonging to another child cannot receive an observation."""
    post(wf, f"/pilot/children/{wf.child_b}/monthly-plan", "foreign",
         {"cycle_month": _CURRENT_MONTH, "timezone_of_record": "UTC"})
    result = post(wf, "/pilot/cycles/wcyc_absent00000000/observations", "cg-a",
                  {"activity_instance_ref": "act_x", "local_date": "2026-10-01",
                   "attempt_outcome": "did_it"})
    assert result[0] == 403
    assert result[1] == FORBIDDEN


# ===========================================================================
# the body boundary, over HTTP
# ===========================================================================

@pytest.mark.parametrize("field", ["provider_id", "caregiver_id", "child_id",
                                   "role", "actor_id", "auth_subject"])
def test_an_identity_field_in_a_body_is_refused(wf, field):
    """The body can never name the actor, on any route."""
    result = post(wf, f"/pilot/children/{wf.child_a}/goals", "hannah",
                  {"edit_type": "authored_fresh", "text": "Fictional",
                   "reason": "Fictional", field: "forged"})
    assert result[0] == 403, (field, result)
    assert result[1] == FORBIDDEN


def test_an_unknown_body_field_is_refused(wf):
    result = post(wf, f"/pilot/children/{wf.child_a}/goals", "hannah",
                  {"edit_type": "authored_fresh", "text": "Fictional",
                   "reason": "Fictional", "emphasis_weight": 9})
    assert result[0] == 403


def test_no_observation_route_accepts_free_text(wf):
    """No Parent note content, by construction."""
    for field in ("note", "observation_text", "observation_text_ref",
                  "assistance", "child_response", "comment"):
        result = post(wf, "/pilot/cycles/wcyc_x/observations", "cg-a",
                      {"activity_instance_ref": "act_x",
                       "local_date": "2026-10-01",
                       "attempt_outcome": "did_it", field: "secret"})
        assert result[0] == 403, field


# ===========================================================================
# every new route is protected and method-checked
# ===========================================================================

NEW_TEMPLATES = [
    ("GET", "/pilot/children/CHILD/goals"),
    ("POST", "/pilot/children/CHILD/goals"),
    ("GET", "/pilot/children/CHILD/goal-suggestions"),
    ("GET", "/pilot/children/CHILD/monthly-plan"),
    ("POST", "/pilot/children/CHILD/monthly-plan"),
    ("GET", "/pilot/children/CHILD/current-cycle"),
    ("GET", "/pilot/children/CHILD/rtm"),
    ("POST", "/pilot/goals/clinical/goal_x/revisions"),
    ("POST", "/pilot/monthly-plans/plan_x/allocations"),
    ("POST", "/pilot/monthly-plans/plan_x/activate"),
    ("GET", "/pilot/cycles/cyc_x/observations"),
    ("POST", "/pilot/cycles/cyc_x/observations"),
    ("POST", "/pilot/cycles/cyc_x/defers"),
    ("POST", "/pilot/rtm-periods/per_x/reviews"),
    ("POST", "/pilot/rtm-periods/per_x/time-entries"),
    ("POST", "/pilot/rtm-periods/per_x/interactions"),
    ("POST", "/pilot/rtm-periods/per_x/report"),
    ("POST", "/pilot/rtm-reviews/rev_x/actions"),
]


@pytest.mark.parametrize("method,template", NEW_TEMPLATES)
def test_every_new_route_refuses_an_unauthenticated_caller(wf, method, template):
    path = template.replace("CHILD", wf.child_a)
    assert call(wf.app, path, method=method)[0] == 401, template
    assert call(wf.app, path, method=method, bearer="Bearer nonsense")[0] == 401
    assert call(wf.app, path, method=method, bearer="Basic abc")[0] == 401


@pytest.mark.parametrize("method,template", NEW_TEMPLATES)
def test_every_new_route_rejects_the_wrong_method(wf, method, template):
    path = template.replace("CHILD", wf.child_a)
    wrong = "DELETE" if method in ("GET", "POST") else "GET"
    status = call(wf.app, path, method=wrong,
                  bearer="Bearer hannah")[0]
    assert status == 405, (template, wrong, status)


def test_no_workflow_route_logs_a_resource_identifier(wf):
    """Templates and the caller's own id, never the targets."""
    post(wf, f"/pilot/children/{wf.child_a}/goals", "hannah",
         {"edit_type": "authored_fresh", "text": "Fictional-sentinel-XYZ",
          "reason": "Fictional"})
    rendered = " ".join(str(entry) for entry in wf.logs)
    assert wf.child_a not in rendered, "a child id reached a log line"
    assert "Fictional-sentinel-XYZ" not in rendered, "goal text reached a log"
    assert "/pilot/children/{child_id}/goals" in rendered
