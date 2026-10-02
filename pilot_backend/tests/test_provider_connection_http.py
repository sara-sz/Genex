"""0.5B — the provider connection surface over REAL request handling.

Not service calls. Every assertion here goes through the WSGI application, so
what is proven is what an actual HTTP caller can and cannot do: the role
checks, the constant refusal bodies, and the fact that no identifier a client
supplies can be turned into access.

The service suite (`test_provider_connection.py`) pins the state machine and
the emulator suite pins contention. This pins the EDGE.
"""

from __future__ import annotations

import io
import json

import pytest

from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth import DevAuthVerifier, VerifiedToken
from pilot_backend.config import PilotSettings
from pilot_backend.domain.enums import EntityStatus, ProviderDiscipline
from pilot_backend.fixtures.secure_topology import (
    CAREGIVER_ALPHA_SUBJECT,
    CAREGIVER_BETA_SUBJECT,
    PROVIDER_ALPHA_SUBJECT,
    PROVIDER_BETA_SUBJECT,
    build_secure_topology,
)
from pilot_backend.integration.parent_source import InMemoryParentSessionSource
from pilot_backend.persistence import FakeDocumentStore, FirestoreRepositories
from pilot_backend.provisioning import provision_provider_record
from pilot_backend.transport import build_application

from .test_integration_identity import call
from .test_secure_foundation import SENTINEL_EMAIL

HANNAH_SUBJECT = "fictional-subject-provider-hannah"

#: The constant refusal body. Every 0.5B denial renders exactly this, so a
#: caller cannot read the REASON out of the response.
FORBIDDEN = {"error": "not permitted"}


@pytest.fixture()
def http():
    """The pilot application, a topology, and a provisioned Hannah."""
    settings = PilotSettings.from_env({
        "PILOT_ENVIRONMENT": "dev",
        "PILOT_ALLOWED_ORIGINS": "http://localhost:3000",
        "PILOT_DEV_AUTH": "1",
    })
    repos = FirestoreRepositories(FakeDocumentStore())
    topo = build_secure_topology(repos)
    hannah = provision_provider_record(
        repos, auth_subject=HANNAH_SUBJECT,
        practice_id=topo.practice.practice_id,
        discipline=ProviderDiscipline.SLP,
        display_name="Provider-Hannah").provider

    verifier = DevAuthVerifier("dev")
    for token, subject in {
        "token-caregiver-alpha": CAREGIVER_ALPHA_SUBJECT,
        "token-caregiver-beta": CAREGIVER_BETA_SUBJECT,
        "token-provider-alpha": PROVIDER_ALPHA_SUBJECT,
        "token-provider-beta": PROVIDER_BETA_SUBJECT,
        "token-hannah": HANNAH_SUBJECT,
    }.items():
        verifier.add(token, VerifiedToken(subject=subject, email=SENTINEL_EMAIL))

    recorder = AuditRecorder(repos.audit_events, environment="dev")
    logs = []
    app = build_application(
        settings=settings, repos=repos, verifier=verifier, recorder=recorder,
        parent_source=InMemoryParentSessionSource(), log_sink=logs)

    class Bundle:
        pass

    bundle = Bundle()
    bundle.app, bundle.repos, bundle.topo = app, repos, topo
    bundle.hannah, bundle.logs = hannah, logs
    bundle.child = topo.child_alpha.child_id
    bundle.other_child = topo.child_beta.child_id
    return bundle


# -- helpers ---------------------------------------------------------------

def invite(http, *, child=None, provider=None, token="token-caregiver-alpha"):
    child = child or http.child
    provider = provider or http.hannah.provider_id
    return call(http.app,
                f"/pilot/children/{child}/provider-connections/{provider}",
                method="POST", bearer=f"Bearer {token}")


def act(http, connection_id, action, token):
    return call(http.app,
                f"/pilot/provider-connections/{connection_id}/{action}",
                method="POST", bearer=f"Bearer {token}")


def access_check(http, child, token):
    return call(http.app, f"/pilot/children/{child}/access-check",
                bearer=f"Bearer {token}")[0]


def connected(http, token):
    status, body, _ = call(http.app, "/pilot/me/children",
                           bearer=f"Bearer {token}")
    return status, body


def hannah_connected(http):
    """Invite + accept, returning the connection id."""
    connection_id = invite(http)[1]["connection_id"]
    assert act(http, connection_id, "accept", "token-hannah")[0] == 200
    return connection_id


# ===========================================================================
# /pilot/me resolves an authenticated Provider
# ===========================================================================

def test_pilot_me_resolves_the_authenticated_provider(http):
    status, body, _ = call(http.app, "/pilot/me", bearer="Bearer token-hannah")
    assert status == 200
    assert body["role"] == "provider"
    assert body["provider_id"] == http.hannah.provider_id
    assert body["practice_id"] == http.hannah.practice_id
    # Identity only. No caseload, no subject, no name.
    for banned in ("auth_subject", "subject", "email", "display_name",
                   "children", "child_ids"):
        assert banned not in body, banned


def test_pilot_me_never_echoes_a_client_asserted_role(http):
    """A body and a query string are both ignored on every identity route."""
    forged = json.dumps({"role": "provider",
                         "provider_id": "prov_forged"}).encode()
    status, body, _ = call(http.app, "/pilot/me", method="GET",
                           bearer="Bearer token-caregiver-alpha",
                           body=forged, query="role=provider")
    assert status == 200
    assert body["role"] == "caregiver"
    assert "provider_id" not in body


# ===========================================================================
# the happy Hannah path, end to end over HTTP
# ===========================================================================

def test_the_full_hannah_flow_over_http(http):
    """Caregiver invites by opaque id, Hannah accepts, then is assigned."""
    status, body, _ = invite(http)
    assert status == 200
    assert body["status"] == "pending"
    assert body["initiated_by"] == "caregiver"
    connection_id = body["connection_id"]

    # PENDING grants nothing.
    assert access_check(http, http.child, "token-hannah") == 403

    assert act(http, connection_id, "accept", "token-hannah")[1]["status"] == "active"
    assert access_check(http, http.child, "token-hannah") == 200

    # ACTIVE is not ownership.
    read = call(http.app, f"/pilot/children/{http.child}/managing-clinician",
                bearer="Bearer token-caregiver-alpha")[1]
    assert read["provider_id"] is None

    assigned = call(
        http.app,
        f"/pilot/children/{http.child}/managing-clinician/{http.hannah.provider_id}",
        method="POST", bearer="Bearer token-caregiver-alpha")
    assert assigned[0] == 200
    assert assigned[1]["provider_id"] == http.hannah.provider_id


# ===========================================================================
# role separation
# ===========================================================================

@pytest.mark.parametrize("action", ["accept", "decline"])
def test_a_caregiver_cannot_act_as_the_provider(http, action):
    """Accepting and declining are the clinician's decisions, not the family's."""
    connection_id = invite(http)[1]["connection_id"]
    status, body, _ = act(http, connection_id, action, "token-caregiver-alpha")
    assert status == 403
    assert body == FORBIDDEN
    assert http.repos.provider_child.get_by_id(
        connection_id).status.value == "pending"


@pytest.mark.parametrize("action", ["pause", "resume", "revoke", "end"])
def test_a_provider_cannot_act_as_the_caregiver(http, action):
    """Pausing and revoking are the family's decisions, not the clinician's."""
    connection_id = hannah_connected(http)
    status, body, _ = act(http, connection_id, action, "token-hannah")
    assert status == 403
    assert body == FORBIDDEN
    assert http.repos.provider_child.get_by_id(
        connection_id).status.value == "active"


def test_a_provider_cannot_invite_themselves(http):
    """There is no provider-initiated path. Deferred, and unreachable."""
    status, body, _ = invite(http, token="token-hannah")
    assert status == 403
    assert body == FORBIDDEN


def test_a_provider_cannot_assign_a_managing_clinician(http):
    """0.5B makes this the caregiver's decision."""
    hannah_connected(http)
    status, body, _ = call(
        http.app,
        f"/pilot/children/{http.child}/managing-clinician/{http.hannah.provider_id}",
        method="POST", bearer="Bearer token-hannah")
    assert status == 403
    assert body == FORBIDDEN
    assert http.repos.managing_clinicians.list_for_child(http.child) == []


def test_an_unknown_action_is_refused_not_404(http):
    """The action vocabulary is not enumerable.

    A 404 for an unknown action and a 403 for a known-but-forbidden one would
    let a caller map the lifecycle verbs from status codes alone.
    """
    connection_id = invite(http)[1]["connection_id"]
    status, body, _ = act(http, connection_id, "escalate", "token-hannah")
    assert status == 403
    assert body == FORBIDDEN


# ===========================================================================
# cross-family and cross-provider isolation
# ===========================================================================

def test_a_foreign_caregiver_cannot_manage_another_childs_connection(http):
    connection_id = hannah_connected(http)

    # Invite onto a child they do not hold.
    assert invite(http, token="token-caregiver-beta")[0] == 403
    # Act on a connection belonging to another family's child.
    for action in ("pause", "revoke", "end"):
        status, body, _ = act(http, connection_id, action, "token-caregiver-beta")
        assert status == 403, action
        assert body == FORBIDDEN
    # Read another family's connections and managing clinician.
    assert call(http.app, f"/pilot/children/{http.child}/provider-connections",
                bearer="Bearer token-caregiver-beta")[0] == 403
    assert call(http.app, f"/pilot/children/{http.child}/managing-clinician",
                bearer="Bearer token-caregiver-beta")[0] == 403
    assert http.repos.provider_child.get_by_id(
        connection_id).status.value == "active"


def test_a_provider_cannot_accept_another_providers_invitation(http):
    connection_id = invite(http)[1]["connection_id"]
    status, body, _ = act(http, connection_id, "accept", "token-provider-beta")
    assert status == 403
    assert body == FORBIDDEN
    assert http.repos.provider_child.get_by_id(
        connection_id).status.value == "pending"
    # And the interloper gained nothing.
    assert access_check(http, http.child, "token-provider-beta") == 403


def test_a_provider_cannot_decline_another_providers_invitation(http):
    connection_id = invite(http)[1]["connection_id"]
    assert act(http, connection_id, "decline", "token-provider-beta")[0] == 403
    assert http.repos.provider_child.get_by_id(
        connection_id).status.value == "pending"


# ===========================================================================
# the opaque provider id is not an oracle
# ===========================================================================

def test_the_provider_id_does_not_reveal_existence_or_status(http):
    """Absent, retired and inactive-practice ids are indistinguishable.

    Status code AND body must match. If they differed, a caller could walk the
    `prov_` space and learn which clinicians exist — which is exactly what
    having no provider directory is meant to prevent.
    """
    retired = provision_provider_record(
        http.repos, auth_subject="fictional-subject-provider-retired",
        practice_id=http.topo.practice.practice_id,
        discipline=ProviderDiscipline.SLP,
        display_name="Provider-Retired").provider
    http.repos.providers.update_status(
        retired.provider_id, EntityStatus.INACTIVE)

    outcomes = set()
    for provider_id in ("prov_absent_0000000000000000000000000",
                        retired.provider_id,
                        "prov_" + "f" * 32):
        status, body, _ = invite(http, provider=provider_id)
        outcomes.add((status, json.dumps(body, sort_keys=True)))
    assert len(outcomes) == 1, outcomes
    assert outcomes.pop()[0] == 403


def test_a_connection_id_does_not_reveal_existence(http):
    """An absent connection and someone else's are the same refusal."""
    real = hannah_connected(http)
    absent = act(http, "pcxn_absent00000000000000000000000", "pause",
                 "token-caregiver-alpha")
    foreign = act(http, real, "pause", "token-caregiver-beta")
    assert absent[0] == foreign[0] == 403
    assert absent[1] == foreign[1] == FORBIDDEN


def test_a_child_id_does_not_reveal_existence_on_the_connection_routes(http):
    """`/pilot/children/{absent}/...` matches `/pilot/children/{not-mine}/...`."""
    absent = call(http.app,
                  "/pilot/children/chld_absent0000000000000000000/provider-connections",
                  bearer="Bearer token-caregiver-alpha")
    not_mine = call(http.app,
                    f"/pilot/children/{http.other_child}/provider-connections",
                    bearer="Bearer token-caregiver-alpha")
    assert absent[0] == not_mine[0] == 403
    assert absent[1] == not_mine[1] == FORBIDDEN


# ===========================================================================
# no inactive state grants access, over HTTP
# ===========================================================================

def test_pausing_removes_child_access_and_resuming_restores_it(http):
    connection_id = hannah_connected(http)
    assert access_check(http, http.child, "token-hannah") == 200

    assert act(http, connection_id, "pause",
               "token-caregiver-alpha")[1]["status"] == "paused"
    assert access_check(http, http.child, "token-hannah") == 403
    assert connected(http, "token-hannah")[1]["children"] == []

    assert act(http, connection_id, "resume",
               "token-caregiver-alpha")[1]["status"] == "active"
    assert access_check(http, http.child, "token-hannah") == 200


def test_revoking_removes_child_access(http):
    connection_id = hannah_connected(http)
    assert act(http, connection_id, "revoke",
               "token-caregiver-alpha")[1]["status"] == "revoked"
    assert access_check(http, http.child, "token-hannah") == 403
    assert connected(http, "token-hannah")[1]["children"] == []


def test_declining_removes_child_access(http):
    connection_id = invite(http)[1]["connection_id"]
    assert act(http, connection_id, "decline",
               "token-hannah")[1]["status"] == "declined"
    assert access_check(http, http.child, "token-hannah") == 403


# ===========================================================================
# the stale-assignment defect, over HTTP
# ===========================================================================

def test_pausing_ends_managing_status_and_resuming_does_not_restore_it(http):
    connection_id = hannah_connected(http)
    call(http.app,
         f"/pilot/children/{http.child}/managing-clinician/{http.hannah.provider_id}",
         method="POST", bearer="Bearer token-caregiver-alpha")

    act(http, connection_id, "pause", "token-caregiver-alpha")
    act(http, connection_id, "resume", "token-caregiver-alpha")

    read = call(http.app, f"/pilot/children/{http.child}/managing-clinician",
                bearer="Bearer token-caregiver-alpha")[1]
    assert read["provider_id"] is None, "a pause/resume restored ownership"
    assert access_check(http, http.child, "token-hannah") == 200


def test_reconnecting_does_not_restore_managing_status_over_http(http):
    """The stale-assignment defect, through the real request path."""
    first = hannah_connected(http)
    call(http.app,
         f"/pilot/children/{http.child}/managing-clinician/{http.hannah.provider_id}",
         method="POST", bearer="Bearer token-caregiver-alpha")
    act(http, first, "revoke", "token-caregiver-alpha")

    second = hannah_connected(http)
    assert second != first
    assert access_check(http, http.child, "token-hannah") == 200

    read = call(http.app, f"/pilot/children/{http.child}/managing-clinician",
                bearer="Bearer token-caregiver-alpha")[1]
    assert read["provider_id"] is None, (
        "reconnecting restored managing-clinician status without an "
        "explicit assignment")
    # The caseload agrees: connected, not owning.
    rows = connected(http, "token-hannah")[1]["children"]
    assert [row["is_managing_clinician"] for row in rows] == [False]


# ===========================================================================
# /pilot/me/children isolation
# ===========================================================================

def test_me_children_isolates_by_authenticated_provider(http):
    hannah_connected(http)
    status, mine = connected(http, "token-hannah")
    assert status == 200
    assert {row["child_id"] for row in mine["children"]} == {http.child}

    # Provider-Beta is connected to Child-Beta in the topology and must see
    # only that — a non-empty-but-different caseload, so this cannot pass by
    # both lists being empty.
    status, theirs = connected(http, "token-provider-beta")
    assert status == 200
    assert {row["child_id"] for row in theirs["children"]} == {http.other_child}
    assert http.child not in {row["child_id"] for row in theirs["children"]}


def test_me_children_accepts_no_provider_id_from_the_request(http):
    """Neither a query parameter nor a body can redirect the caseload."""
    hannah_connected(http)
    forged = json.dumps({"provider_id": http.hannah.provider_id}).encode()
    status, body, _ = call(
        http.app, "/pilot/me/children", bearer="Bearer token-provider-beta",
        body=forged, query=f"provider_id={http.hannah.provider_id}")
    assert status == 200
    assert {row["child_id"] for row in body["children"]} == {http.other_child}


def test_me_children_gives_a_caregiver_the_caregiver_shape(http):
    """Role dispatch picks the path; it does not blend the two payloads."""
    status, body = connected(http, "token-caregiver-alpha")
    assert status == 200
    assert "child_ids" in body
    assert "children" not in body


# ===========================================================================
# every new route is protected
# ===========================================================================

@pytest.mark.parametrize("method,path", [
    ("POST", "/pilot/children/CHILD/provider-connections/PROVIDER"),
    ("GET", "/pilot/children/CHILD/provider-connections"),
    ("POST", "/pilot/provider-connections/CONN/accept"),
    ("GET", "/pilot/children/CHILD/managing-clinician"),
    ("POST", "/pilot/children/CHILD/managing-clinician/prov_x"),
    ("POST", "/pilot/children/CHILD/managing-clinician/end"),
])
def test_every_connection_route_refuses_an_unauthenticated_caller(
        http, method, path):
    resolved = path.replace("CHILD", http.child).replace(
        "PROVIDER", http.hannah.provider_id).replace("CONN", "pcxn_x")
    assert call(http.app, resolved, method=method)[0] == 401
    assert call(http.app, resolved, method=method,
                bearer="Bearer nonsense")[0] == 401
    assert call(http.app, resolved, method=method,
                bearer="Basic abc")[0] == 401


def test_no_connection_route_logs_a_resource_identifier(http):
    """The log line carries the route TEMPLATE and the caller, not the targets.

    What IS logged is the authenticated caller's own opaque `actor_id` and
    role — established 0.5A provenance, and an allowlisted audit key. A log
    that could not say who made a request would be useless for exactly the
    incident it exists to support.

    What must NOT appear is any identifier the caller SUPPLIED or that names
    the subject of the action: the child id, the connection id, and above all
    the raw auth subject. Those are what turn a log into a record of which
    children exist and who is connected to them.
    """
    connection_id = hannah_connected(http)
    act(http, connection_id, "pause", "token-caregiver-alpha")
    rendered = " ".join(str(entry) for entry in http.logs)

    assert connection_id not in rendered, "a connection id reached a log line"
    assert http.child not in rendered, "a child id reached a log line"
    assert HANNAH_SUBJECT not in rendered, "a raw auth subject reached a log line"
    # The template is there, so the request IS traceable without the ids.
    assert "/pilot/provider-connections/{connection_id}/{action}" in rendered
    # And the caller is identified by their own opaque id, which is the point.
    assert "actor_role" in rendered
