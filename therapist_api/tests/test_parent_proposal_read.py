"""Parent-safe proposal decision detail (Phase 1B.2B.3).

`GET /api/v1/children/{child_id}/proposals/{proposal_id}` is role-aware: a
therapist keeps the existing `ProposalView` unchanged, while an authorized parent
receives a narrower, dedicated projection carrying exactly the two versions its
accept/decline calls need.

Read-only throughout — several tests assert the endpoint mutates nothing. All
identities and data are fictional.
"""

from __future__ import annotations

import copy

from app.repository import collections as C
from tests.conftest import ELENA, HANNAH, OMAR, PRIYA, UNCONNECTED, read_slice_client
from tests.test_modify_proposal import body
from tests.test_parent_acceptance import MAYA_BUBBLES, MAYA_MODIFY_PATH, _talking_activity

# Fields that exist on the therapist projection and must NEVER reach a parent.
THERAPIST_ONLY_MARKERS = (
    "save_scope", "is_derived", "immutable", "created_by_user_id",
    "created_by_display_name", "created_by_type", "modified_by_user_id",
    "modified_by_display_name", "original_activity_template_id",
    "original_activity_version_id", "proposed_activity_version_id",
    "therapist_id", "weekly_plan_id", "current_assignment_id",
    "idempotency", "idempotency_key_hash", "audit", "audit_event_id",
    "request_id", "environment", "schema_version",
    "submitted_for_genex_review", "therapist_library", "marketplace",
    "activity_template_id", "developmental_domain_key",
)


def _c():
    return read_slice_client()


def _repo(c):
    return c.app.state.repo


def _propose(client, key="prop-1"):
    r = client.post(MAYA_MODIFY_PATH, headers={**HANNAH, "Idempotency-Key": key},
                    json={**body(), "activity": _talking_activity()})
    assert r.status_code == 200, r.text
    return r.json()


def _url(proposal_id, child="child_maya"):
    return f"/api/v1/children/{child}/proposals/{proposal_id}"


def _read(client, headers, proposal_id, child="child_maya"):
    return client.get(_url(proposal_id, child), headers=headers)


def _set_connection(client, conn_id, **fields):
    repo = _repo(client)
    conn = repo.query(C.CONNECTIONS, id=conn_id)[0]
    conn.update(fields)
    repo.set(C.CONNECTIONS, conn_id, conn)


# ── 1-14: pending proposal happy path ───────────────────────────────────────
def test_parent_can_read_pending_proposal():
    c = _c()
    created = _propose(c)
    pid = created["proposal"]["proposal_id"]

    r = _read(c, ELENA, pid)
    assert r.status_code == 200, r.text
    out = r.json()

    # (2) child-safe information
    assert out["child"] == {"child_id": "child_maya", "display_name": "Maya"}
    # (3) therapist display name (name + credentials, no id/contact)
    assert out["therapist"]["display_name"] == "Hannah Lieberknecht, MA, SLP"
    assert list(out["therapist"].keys()) == ["display_name"]
    # (4) change reason
    assert out["decision_context"]["change_reason"] == "Adjust based on session."
    # (5)(6) both versions the write endpoints need
    assert out["proposal"]["proposal_version"] == 1
    assert out["decision_context"]["expected_assignment_version"] == 2
    # (9)(10) decision flags
    assert out["decision"]["can_accept"] is True
    assert out["decision"]["can_decline"] is True
    assert out["decision"]["accepted_or_declined_at"] is None
    assert out["decision"]["resulting_assignment_id"] is None
    assert out["proposal"]["proposal_status"] == "pending_parent_acceptance"
    assert out["proposal"]["proposal_type"] == "modify"
    assert out["proposal"]["decided_at"] is None
    assert out["proposal"]["created_at"]


def test_parent_sees_original_and_proposed_activity_details():
    """(7)(8) both activities, with canonical domain + milestone display name."""
    c = _c()
    created = _propose(c)
    out = _read(c, ELENA, created["proposal"]["proposal_id"]).json()

    original = out["original_activity"]
    assert original["title"] == "Bubble requesting"          # the fixture original
    assert original["developmental_domain"] == "Talking & Communicating"
    assert original["milestone_id"] == "mile_request_items"
    assert original["milestone_display_name"] == "Requests a desired item"

    proposed = out["proposed_activity"]
    assert proposed["title"] == "Bubble requesting (adapted)"
    assert proposed["developmental_domain"] == "Talking & Communicating"
    assert proposed["milestone_display_name"] == "Requests a desired item"
    assert proposed["skill_focus"] == "waiting a turn"
    assert proposed["duration_minutes"] == 8
    assert proposed["difficulty"] == "just_right"
    assert proposed["materials"] == ["soft ball"]
    assert proposed["setup"] == "Sit facing each other."
    assert proposed["parent_instructions"] == ["Name each turn."]
    assert proposed["what_to_say"] == ["my turn", "your turn"]
    assert proposed["how_to_help"] == ["Pause and wait."]
    assert proposed["success_signals"] == ["Child waits for a turn."]
    assert proposed["variations"] == ["Use a car."]
    assert original.keys() == proposed.keys()


def test_no_therapist_private_or_internal_fields_leak():
    """(11)(12)(13)(14) nothing therapist-only, no save_scope, no audit internals."""
    c = _c()
    created = _propose(c)
    proposed_version_id = created["proposed_activity_version"]["id"]
    r = _read(c, ELENA, created["proposal"]["proposal_id"])
    blob = r.text

    for marker in THERAPIST_ONLY_MARKERS:
        assert marker not in blob, f"parent response leaked {marker!r}"

    out = r.json()
    assert set(out.keys()) == {
        "proposal", "child", "therapist", "decision_context",
        "original_activity", "proposed_activity", "decision",
    }
    # (14) no raw ActivityVersion ids and no unrelated version history
    assert proposed_version_id not in blob
    assert "ver_bubbles_v1" not in blob
    assert "ver_bubbles_hannah_v1" not in blob
    assert "ver_lib_hannah_v1" not in blob


def test_parent_schema_field_sets_are_pinned():
    """The response model IS the privacy boundary — pin it at source level.

    FastAPI rebuilds the payload from the declared `response_model`, so a field
    absent from these schemas cannot reach a parent even if the service tried to
    supply it. That makes the schema definition the actual guard, and this test
    the thing that fails if someone widens it.
    """
    from app.api import schemas as S

    assert set(S.ParentActivityView.model_fields) == {
        "title", "developmental_domain", "milestone_id", "milestone_display_name",
        "skill_focus", "duration_minutes", "difficulty", "materials",
        "materials_type", "setup", "parent_instructions", "what_to_say",
        "how_to_help", "success_signals", "variations", "routine_tags",
        "theme_tags", "safety_risk_flags",
    }
    assert set(S.ParentProposalSummary.model_fields) == {
        "proposal_id", "proposal_type", "proposal_status", "proposal_version",
        "created_at", "decided_at",
    }
    assert set(S.ParentTherapistSummary.model_fields) == {"display_name"}
    assert set(S.ParentChildSummary.model_fields) == {"child_id", "display_name"}
    assert set(S.ParentDecisionContext.model_fields) == {
        "change_reason", "expected_assignment_version",
    }
    assert set(S.ParentDecisionFlags.model_fields) == {
        "can_accept", "can_decline", "accepted_or_declined_at",
        "resulting_assignment_id",
    }
    assert set(S.ParentProposalDecisionDetail.model_fields) == {
        "proposal", "child", "therapist", "decision_context",
        "original_activity", "proposed_activity", "decision",
    }
    # No parent schema may declare a therapist-only or internal field.
    for model in (S.ParentActivityView, S.ParentProposalSummary,
                  S.ParentTherapistSummary, S.ParentChildSummary,
                  S.ParentDecisionContext, S.ParentDecisionFlags,
                  S.ParentProposalDecisionDetail):
        for marker in THERAPIST_ONLY_MARKERS:
            assert marker not in model.model_fields, f"{model.__name__} declares {marker}"


def test_parent_response_excludes_private_notes_and_other_children():
    c = _c()
    created = _propose(c)
    blob = _read(c, ELENA, created["proposal"]["proposal_id"]).text
    # (50) therapist private notes
    assert "AAC backup" not in blob and "private" not in blob.lower()
    # other children / families
    for other in ("child_eli", "child_noah", "child_theo", "child_sana", "Omar", "Priya"):
        assert other not in blob


# ── 15-18: decision integration ─────────────────────────────────────────────
def test_versions_from_get_feed_accept_directly():
    """(15)(17) the GET response is sufficient to accept, with no therapist call."""
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    view = _read(c, ELENA, pid).json()

    r = c.post(f"{_url(pid)}/accept", headers={**ELENA, "Idempotency-Key": "acc-1"},
               json={"expected_proposal_version": view["proposal"]["proposal_version"],
                     "expected_assignment_version":
                         view["decision_context"]["expected_assignment_version"]})
    assert r.status_code == 200, r.text
    assert r.json()["proposal"]["proposal_status"] == "accepted"


def test_versions_from_get_feed_decline_directly():
    """(16)(18)"""
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    view = _read(c, ELENA, pid).json()

    r = c.post(f"{_url(pid)}/decline", headers={**ELENA, "Idempotency-Key": "dec-1"},
               json={"expected_proposal_version": view["proposal"]["proposal_version"],
                     "expected_assignment_version":
                         view["decision_context"]["expected_assignment_version"]})
    assert r.status_code == 200, r.text
    assert r.json()["proposal"]["proposal_status"] == "declined"


# ── 19-24: accepted proposal ────────────────────────────────────────────────
def test_parent_can_read_accepted_proposal():
    c = _c()
    created = _propose(c)
    pid = created["proposal"]["proposal_id"]
    accepted = c.post(f"{_url(pid)}/accept", headers={**ELENA, "Idempotency-Key": "acc-2"},
                      json={"expected_proposal_version": 1,
                            "expected_assignment_version": 2}).json()
    replacement_id = accepted["replacement_assignment"]["assignment_id"]

    out = _read(c, ELENA, pid).json()
    assert out["proposal"]["proposal_status"] == "accepted"        # (20)
    assert out["decision"]["can_accept"] is False                  # (21)
    assert out["decision"]["can_decline"] is False                 # (22)
    assert out["proposal"]["decided_at"]                           # (23)
    assert out["decision"]["accepted_or_declined_at"] == out["proposal"]["decided_at"]
    assert out["decision"]["resulting_assignment_id"] == replacement_id   # (24)
    # decision history remains readable
    assert out["original_activity"]["title"] and out["proposed_activity"]["title"]


# ── 25-30: declined proposal ────────────────────────────────────────────────
def test_parent_can_read_declined_proposal():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    c.post(f"{_url(pid)}/decline", headers={**ELENA, "Idempotency-Key": "dec-2"},
           json={"expected_proposal_version": 1, "expected_assignment_version": 2})

    out = _read(c, ELENA, pid).json()
    assert out["proposal"]["proposal_status"] == "declined"        # (26)
    assert out["decision"]["can_accept"] is False                  # (27)
    assert out["decision"]["can_decline"] is False                 # (28)
    assert out["proposal"]["decided_at"]                           # (29)
    assert out["decision"]["resulting_assignment_id"] is None      # (30)
    assert out["original_activity"]["title"] and out["proposed_activity"]["title"]


# ── 31-39: authorization and existence-blindness ────────────────────────────
# The project's canonical existence-blind envelope, as emitted by the
# ChildNotFound handler in app/main.py. Every unauthorized-or-unknown outcome
# must be byte-identical to this.
NOT_FOUND_BODY = {"error": "not_found", "detail": "Not found."}


def test_other_parent_and_unknown_resources_are_404():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    cases = {
        "other parent": _read(c, OMAR, pid),                       # (31)
        "unknown child": _read(c, ELENA, pid, child="child_zzz"),   # (32)
        "unknown proposal": _read(c, ELENA, "prop_zzz"),            # (33)
        "child mismatch": _read(c, ELENA, pid, child="child_eli"),  # (34)
    }
    for label, r in cases.items():
        assert r.status_code == 404, f"{label}: {r.status_code}"
        assert r.json()["error"] == "not_found", label


def test_pending_paused_ended_connection_404():
    """(35)(36)(37)"""
    for idx, status in enumerate(
        ("pending_parent_acceptance", "paused_by_parent", "ended")
    ):
        c = _c()
        pid = _propose(c, key=f"pk-{idx}")["proposal"]["proposal_id"]
        _set_connection(c, "conn_maya", status=status)
        r = _read(c, ELENA, pid)
        assert r.status_code == 404, f"{status}: {r.status_code}"


def test_unauthenticated_request_401():
    """(38) existing fail-closed behavior."""
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    assert c.get(_url(pid)).status_code == 401


def test_unauthorized_and_unknown_bodies_are_identical():
    """(39) an attacker cannot distinguish 'not yours' from 'does not exist'."""
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    bodies = [
        _read(c, OMAR, pid).json(),                        # exists, not yours
        _read(c, ELENA, "prop_zzz").json(),                # does not exist
        _read(c, ELENA, pid, child="child_zzz").json(),    # unknown child
        _read(c, ELENA, pid, child="child_eli").json(),    # another child's
    ]
    assert all(b == bodies[0] for b in bodies), bodies
    assert bodies[0]["error"] == "not_found"


# ── 40-44: therapist backward compatibility ─────────────────────────────────
THERAPIST_VIEW_KEYS = {
    "proposal_id", "proposal_type", "proposal_status", "child_id", "weekly_plan_id",
    "current_assignment_id", "original_activity_template_id",
    "original_activity_version_id", "proposed_activity_version_id", "change_reason",
    "save_scope", "created_by_user_id", "created_at", "decided_by_user_id",
    "decided_by_role", "decided_at", "resulting_assignment_id", "version",
}


def test_therapist_response_shape_is_unchanged():
    """(40)(41)(44) the therapist projection keeps every field, including save_scope."""
    c = _c()
    created = _propose(c)
    pid = created["proposal"]["proposal_id"]
    r = _read(c, HANNAH, pid)
    assert r.status_code == 200
    out = r.json()
    assert set(out.keys()) == THERAPIST_VIEW_KEYS
    assert out["proposal_id"] == pid and out["child_id"] == "child_maya"
    assert out["save_scope"] == "child_only"          # therapist-only field intact
    assert out["proposed_activity_version_id"] == created["proposed_activity_version"]["id"]
    assert out["version"] == 1
    # parent-safe sections must NOT appear in the therapist response
    for parent_section in ("decision", "decision_context", "original_activity",
                           "proposed_activity", "therapist", "child"):
        assert parent_section not in out


def test_unconnected_therapist_remains_existence_blind():
    """(43)"""
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    for headers in (UNCONNECTED, PRIYA):
        r = _read(c, headers, pid)
        assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_therapist_and_parent_get_different_projections():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    t = _read(c, HANNAH, pid).json()
    p = _read(c, ELENA, pid).json()
    assert set(t.keys()) & set(p.keys()) == set(), "projections must be disjoint"


# ── 45-49: activity visibility and privacy ──────────────────────────────────
def test_parent_sees_only_the_two_versions_this_proposal_references():
    """(45)(47) not a catalog read; unrelated derived versions stay invisible."""
    c = _c()
    created = _propose(c)
    out = _read(c, ELENA, created["proposal"]["proposal_id"]).json()
    activities = [out["original_activity"], out["proposed_activity"]]
    assert len(activities) == 2
    titles = {a["title"] for a in activities}
    # Hannah's other derived versions must not surface anywhere.
    blob = str(out)
    for other_title in ("Bubble requesting (Hannah's library copy)",
                        "Bubble requesting (submitted for Genex review)",
                        "Bubble requesting (draft, unattached)",
                        "Bubble requesting (Sana)", "Bubble requesting (Amara)"):
        assert other_title not in blob
    assert titles == {"Bubble requesting", "Bubble requesting (adapted)"}


def test_generic_catalog_visibility_is_unchanged():
    """(46)(48)(49) this endpoint does not widen template visibility."""
    c = _c()
    created = _propose(c)
    proposed_version_id = created["proposed_activity_version"]["id"]
    _read(c, ELENA, created["proposal"]["proposal_id"])   # exercise the parent read

    # parents are still forbidden from the generic catalog
    assert c.get("/api/v1/activity-templates", headers=ELENA).status_code == 403
    assert c.get("/api/v1/activity-templates/tmpl_bubbles", headers=ELENA).status_code == 403
    # and the derived version is still hidden from an unrelated therapist
    detail = c.get("/api/v1/activity-templates/tmpl_bubbles", headers=UNCONNECTED).json()
    version_ids = [v["activity_version_id"] for v in detail["versions"]]
    assert proposed_version_id not in version_ids
    assert "ver_lib_hannah_v1" not in version_ids


def test_parent_cannot_reach_another_childs_derived_activity():
    """(46) a proposal id from another family is not readable via one's own child."""
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    # Omar (Eli's parent) cannot read Maya's proposal through his own child id.
    assert _read(c, OMAR, pid, child="child_eli").status_code == 404
    assert _read(c, OMAR, pid).status_code == 404


# ── 51-55: invalid linked state stays existence-blind ───────────────────────
def test_missing_linked_records_stay_existence_blind():
    """(51)(52)(53)(54) dangling references must not disclose internal state."""
    created_ref = None

    def fresh():
        nonlocal created_ref
        c = _c()
        created_ref = _propose(c)
        return c, created_ref["proposal"]["proposal_id"]

    # (51) missing original assignment
    c, pid = fresh()
    _repo(c)._data[C.PLAN_ASSIGNMENTS].pop(MAYA_BUBBLES)
    r = _read(c, ELENA, pid)
    assert r.status_code == 404 and r.json() == NOT_FOUND_BODY

    # (52) missing original ActivityVersion
    c, pid = fresh()
    _repo(c)._data[C.ACTIVITY_VERSIONS].pop("ver_bubbles_v1")
    r = _read(c, ELENA, pid)
    assert r.status_code == 404 and r.json() == NOT_FOUND_BODY

    # (53) missing proposed ActivityVersion
    c, pid = fresh()
    _repo(c)._data[C.ACTIVITY_VERSIONS].pop(
        created_ref["proposed_activity_version"]["id"])
    r = _read(c, ELENA, pid)
    assert r.status_code == 404 and r.json() == NOT_FOUND_BODY

    # (54) proposal pointing at another child's assignment
    c, pid = fresh()
    repo = _repo(c)
    p = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]
    p["target_assignment_id"] = "assign_eli_bubbles"      # belongs to child_eli
    repo.set(C.PLAN_CHANGE_PROPOSALS, pid, p)
    r = _read(c, ELENA, pid)
    assert r.status_code == 404 and r.json() == NOT_FOUND_BODY


def test_non_modify_proposal_is_still_readable_with_no_write_flags():
    """(55) an unexpected type must not present as write-enabled."""
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    repo = _repo(c)
    p = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]
    p["status"] = "cancelled"
    repo.set(C.PLAN_CHANGE_PROPOSALS, pid, p)
    out = _read(c, ELENA, pid).json()
    assert out["proposal"]["proposal_status"] == "cancelled"
    assert out["decision"]["can_accept"] is False
    assert out["decision"]["can_decline"] is False


# ── 56-60: read-only guarantees ─────────────────────────────────────────────
def test_get_has_no_side_effects():
    """(56)(57)(58)(59)(60) the read mutates nothing at all."""
    c = _c()
    created = _propose(c)
    pid = created["proposal"]["proposal_id"]
    repo = _repo(c)

    before = {col: copy.deepcopy(repo._data.get(col, {})) for col in (
        C.PLAN_ASSIGNMENTS, C.PLAN_CHANGE_PROPOSALS, C.ACTIVITY_VERSIONS,
        C.ACTIVITY_TEMPLATES, C.AUDIT_EVENTS, C.IDEMPOTENCY_RECORDS,
        C.CONNECTIONS, C.CHILDREN,
    )}
    audit_before = len(repo.query(C.AUDIT_EVENTS))
    idem_before = len(repo.query(C.IDEMPOTENCY_RECORDS))

    for _ in range(3):
        assert _read(c, ELENA, pid).status_code == 200
        assert _read(c, HANNAH, pid).status_code == 200

    for col, snapshot in before.items():
        assert repo._data.get(col, {}) == snapshot, f"{col} changed during a GET"
    assert len(repo.query(C.AUDIT_EVENTS)) == audit_before      # (56)
    assert len(repo.query(C.IDEMPOTENCY_RECORDS)) == idem_before  # (57)


# ── 64: isolation ───────────────────────────────────────────────────────────
def test_no_parent_api_or_genex_core_import():
    import ast
    import pathlib
    for name in ("services/read_service.py", "api/routes.py", "api/schemas.py"):
        path = pathlib.Path(__file__).resolve().parent.parent / "app" / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert not a.name.startswith("genex_core"), name
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                assert (node.module or "").split(".")[0] not in ("genex_core",), name
