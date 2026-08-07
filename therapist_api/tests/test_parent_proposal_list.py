"""Parent-safe proposal list + shared decision eligibility (Phase 1B.2B.4).

`GET /api/v1/children/{child_id}/proposals` is role-aware: a therapist keeps the
existing `Page` envelope, an authorized parent receives a lighter discovery-only
projection.

The substantive correction under test is that `can_accept` / `can_decline` are
computed by the SHARED eligibility evaluator rather than from
`status == pending_parent_acceptance`, so the list and detail never advertise an
action that the write endpoints would reject. All data is fictional.
"""

from __future__ import annotations

import copy

from app.repository import collections as C
from app.services import eligibility
from tests.conftest import ELENA, HANNAH, OMAR, PRIYA, UNCONNECTED, read_slice_client
from tests.test_modify_proposal import body
from tests.test_parent_acceptance import MAYA_BUBBLES, MAYA_MODIFY_PATH, _talking_activity

LIST_URL = "/api/v1/children/child_maya/proposals"
FIXTURE_PROPOSAL = "prop_maya_modify_bubbles"      # pending but NOT actionable

# Field names that must never appear anywhere in a parent list response.
FORBIDDEN = (
    "save_scope", "is_derived", "immutable", "created_by_user_id", "created_by_type",
    "modified_by_user_id", "original_activity_template_id",
    "original_activity_version_id", "proposed_activity_version_id",
    "current_assignment_id", "weekly_plan_id", "therapist_id",
    "contact_email", "organization", "idempotency", "idempotency_key_hash",
    "audit", "audit_event_id", "request_id", "environment", "schema_version",
    "submitted_for_genex_review", "therapist_library", "marketplace",
    "proposal_version", "expected_assignment_version", "version",
    "parent_instructions", "what_to_say", "how_to_help", "success_signals",
    "setup", "variations", "materials",
)


def _c():
    return read_slice_client()


def _repo(c):
    return c.app.state.repo


def _propose(client, key="prop-1", version=1, title=None):
    activity = _talking_activity(**({"title": title} if title else {}))
    r = client.post(MAYA_MODIFY_PATH, headers={**HANNAH, "Idempotency-Key": key},
                    json={**body(), "activity": activity,
                          "expected_assignment_version": version})
    assert r.status_code == 200, r.text
    return r.json()


def _list(client, headers, child="child_maya"):
    return client.get(f"/api/v1/children/{child}/proposals", headers=headers)


def _detail(client, headers, proposal_id, child="child_maya"):
    return client.get(f"/api/v1/children/{child}/proposals/{proposal_id}", headers=headers)


def _item(payload, proposal_id):
    return next(i for i in payload["items"] if i["proposal_id"] == proposal_id)


def _set_connection(client, conn_id, **fields):
    repo = _repo(client)
    conn = repo.query(C.CONNECTIONS, id=conn_id)[0]
    conn.update(fields)
    repo.set(C.CONNECTIONS, conn_id, conn)


# ── 1-11: parent list basics ────────────────────────────────────────────────
def test_parent_can_list_own_childs_proposals():
    c = _c()
    created = _propose(c)
    pid = created["proposal"]["proposal_id"]

    r = _list(c, ELENA)
    assert r.status_code == 200, r.text
    out = r.json()

    assert set(out.keys()) == {"items", "total", "next_cursor"}       # (3)
    assert out["total"] == len(out["items"]) == 2                     # new + fixture
    assert out["next_cursor"] is None
    ids = [i["proposal_id"] for i in out["items"]]
    assert pid in ids and FIXTURE_PROPOSAL in ids

    item = _item(out, pid)
    assert item["proposal_type"] == "modify"                          # (8)
    assert item["proposal_status"] == "pending_parent_acceptance"
    assert item["created_at"] and item["decided_at"] is None
    assert item["child"] == {"child_id": "child_maya", "display_name": "Maya"}   # (4)
    assert item["therapist"] == {"display_name": "Hannah Lieberknecht, MA, SLP"}  # (7)
    assert item["change_reason"] == "Adjust based on session."         # (9)
    assert item["proposed_activity"] == {                              # (6)
        "title": "Bubble requesting (adapted)",
        "developmental_domain": "Talking & Communicating",
        "milestone_display_name": "Requests a desired item",
    }


def test_list_item_is_lighter_than_detail():
    """(10)(11) no activity instructions and no concurrency versions."""
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    blob = _list(c, ELENA).text

    for marker in FORBIDDEN:
        assert marker not in blob, f"parent list leaked {marker!r}"

    item = _item(_list(c, ELENA).json(), pid)
    assert set(item.keys()) == {
        "proposal_id", "proposal_type", "proposal_status", "created_at",
        "decided_at", "child", "therapist", "proposed_activity", "change_reason",
        "decision",
        # Phase 1B.2E: ADD only. Null for MODIFY, asserted just below, so this
        # Modify item is unchanged in value.
        "destination",
    }
    assert item["destination"] is None
    assert set(item["proposed_activity"].keys()) == {
        "title", "developmental_domain", "milestone_display_name",
    }
    # the detail endpoint DOES carry the versions the list withholds
    detail = _detail(c, ELENA, pid).json()
    assert detail["proposal"]["proposal_version"] == 1
    assert detail["decision_context"]["expected_assignment_version"] == 2


def test_parent_list_schema_key_sets_are_pinned():
    """(50) the response model is the privacy boundary — pin it at source."""
    from app.api import schemas as S

    assert set(S.ParentProposalListResponse.model_fields) == {
        "items", "total", "next_cursor"}
    assert set(S.ParentProposalListItem.model_fields) == {
        "proposal_id", "proposal_type", "proposal_status", "created_at",
        "decided_at", "child", "therapist", "proposed_activity", "change_reason",
        "decision",
        "destination"}                      # Phase 1B.2E: ADD only, null for MODIFY
    assert set(S.ParentProposalActivitySummary.model_fields) == {
        "title", "developmental_domain", "milestone_display_name"}
    assert set(S.ParentProposalDecisionSummary.model_fields) == {
        "needs_parent_attention", "can_accept", "can_decline"}
    assert set(S.ParentDestinationDay.model_fields) == {"scheduled_day", "day_label"}
    for model in (S.ParentProposalListResponse, S.ParentProposalListItem,
                  S.ParentProposalActivitySummary, S.ParentProposalDecisionSummary,
                  S.ParentDestinationDay):
        for marker in FORBIDDEN:
            assert marker not in model.model_fields, f"{model.__name__} declares {marker}"


# ── 12-16: actionable eligibility ───────────────────────────────────────────
def test_real_proposal_is_actionable_in_list_and_detail():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]

    item = _item(_list(c, ELENA).json(), pid)
    assert item["decision"] == {                                       # (13)(14)(15)
        "needs_parent_attention": True, "can_accept": True, "can_decline": True}

    detail = _detail(c, ELENA, pid).json()                             # (16)
    assert detail["decision"]["can_accept"] is True
    assert detail["decision"]["can_decline"] is True


# ── 17-23: the ineligible pending fixture ───────────────────────────────────
def test_fixture_proposal_is_pending_but_not_actionable():
    """The fixture's assignment is change_pending_parent — readable, not actionable."""
    c = _c()
    out = _list(c, ELENA).json()
    item = _item(out, FIXTURE_PROPOSAL)                                # (17)

    assert item["proposal_status"] == "pending_parent_acceptance"      # (18) unchanged
    assert item["decision"] == {                                       # (19)(20)(21)
        "needs_parent_attention": False, "can_accept": False, "can_decline": False}

    detail = _detail(c, ELENA, FIXTURE_PROPOSAL).json()                # (22)
    assert detail["proposal"]["proposal_status"] == "pending_parent_acceptance"
    assert detail["decision"]["can_accept"] is False
    assert detail["decision"]["can_decline"] is False

    # (23) the fixture itself was not altered to make it actionable
    repo = _repo(c)
    assignment = repo.query(C.PLAN_ASSIGNMENTS, id="assign_maya_turntake")[0]
    assert assignment["plan_approval_status"] == "change_pending_parent"
    assert repo.query(C.PLAN_CHANGE_PROPOSALS, id=FIXTURE_PROPOSAL)[0][
        "status"] == "pending_parent_acceptance"


def test_ineligible_flags_match_the_write_guards():
    """The advertised flags agree with what the write endpoint actually does."""
    c = _c()
    item = _item(_list(c, ELENA).json(), FIXTURE_PROPOSAL)
    assert item["decision"]["can_accept"] is False

    # ...and a real accept attempt is indeed rejected.
    r = c.post(f"/api/v1/children/child_maya/proposals/{FIXTURE_PROPOSAL}/accept",
               headers={**ELENA, "Idempotency-Key": "guard-1"},
               json={"expected_proposal_version": 1, "expected_assignment_version": 1})
    assert r.status_code == 409, r.text


# ── 24-29: decided proposals ────────────────────────────────────────────────
def test_accepted_and_declined_items_have_no_write_flags():
    c = _c()
    accept_pid = _propose(c, key="a-1")["proposal"]["proposal_id"]
    c.post(f"/api/v1/children/child_maya/proposals/{accept_pid}/accept",
           headers={**ELENA, "Idempotency-Key": "acc"},
           json={"expected_proposal_version": 1, "expected_assignment_version": 2})

    out = _list(c, ELENA).json()
    accepted = _item(out, accept_pid)
    assert accepted["proposal_status"] == "accepted"                   # (24)
    assert accepted["decision"] == {                                   # (25)
        "needs_parent_attention": False, "can_accept": False, "can_decline": False}
    assert accepted["decided_at"]                                      # (26)

    # a second proposal, declined
    c2 = _c()
    decline_pid = _propose(c2, key="d-1")["proposal"]["proposal_id"]
    c2.post(f"/api/v1/children/child_maya/proposals/{decline_pid}/decline",
            headers={**ELENA, "Idempotency-Key": "dec"},
            json={"expected_proposal_version": 1, "expected_assignment_version": 2})
    declined = _item(_list(c2, ELENA).json(), decline_pid)
    assert declined["proposal_status"] == "declined"                   # (27)
    assert declined["decision"] == {                                   # (28)
        "needs_parent_attention": False, "can_accept": False, "can_decline": False}
    assert declined["decided_at"]                                      # (29)


# ── 30-34: sorting ──────────────────────────────────────────────────────────
def test_actionable_sorts_before_ineligible_pending_and_decided():
    """(30)(31)(32)(33) group order, newest-first, stable id tie-break."""
    c = _c()
    actionable = _propose(c, key="s-1")["proposal"]["proposal_id"]
    # decline it, then create a second actionable one on the freed slot
    c.post(f"/api/v1/children/child_maya/proposals/{actionable}/decline",
           headers={**ELENA, "Idempotency-Key": "s-dec"},
           json={"expected_proposal_version": 1, "expected_assignment_version": 2})
    newest = _propose(c, key="s-2", version=3, title="Newest change")[
        "proposal"]["proposal_id"]

    out = _list(c, ELENA).json()
    order = [i["proposal_id"] for i in out["items"]]
    groups = {i["proposal_id"]: (
        i["decision"]["needs_parent_attention"],
        i["proposal_status"]) for i in out["items"]}

    # actionable first
    assert order[0] == newest and groups[newest][0] is True
    # ineligible pending (the fixture) before the decided one
    assert order.index(FIXTURE_PROPOSAL) < order.index(actionable)
    assert groups[FIXTURE_PROPOSAL] == (False, "pending_parent_acceptance")
    assert groups[actionable] == (False, "declined")


def test_repeated_requests_return_identical_ordering():
    """(34) deterministic — never dependent on dict iteration order."""
    c = _c()
    _propose(c, key="r-1")
    orders = [[i["proposal_id"] for i in _list(c, ELENA).json()["items"]]
              for _ in range(5)]
    assert all(o == orders[0] for o in orders), orders


def test_sort_key_is_deterministic_and_groups_correctly():
    """Unit-level: the pure sort key orders groups then newest then id."""
    newer = {"id": "prop_b", "created_at": "2026-07-02", "status": "pending_parent_acceptance"}
    older = {"id": "prop_a", "created_at": "2026-07-01", "status": "pending_parent_acceptance"}
    decided = {"id": "prop_c", "created_at": "2026-07-03", "status": "accepted"}

    keys = [
        eligibility.sort_key(newer, eligibility.ELIGIBLE),
        eligibility.sort_key(older, eligibility.ELIGIBLE),
        eligibility.sort_key(newer, eligibility.INELIGIBLE),
        eligibility.sort_key(decided, eligibility.INELIGIBLE),
    ]
    assert keys == sorted(keys), "actionable-newest < actionable-older < pending < decided"
    # identical inputs give identical keys
    assert eligibility.sort_key(newer, eligibility.ELIGIBLE) == keys[0]


# ── 35-41: authorization and existence-blindness ────────────────────────────
NOT_FOUND_BODY = {"error": "not_found", "detail": "Not found."}


def test_other_parent_and_unknown_child_are_404():
    c = _c()
    _propose(c)
    bodies = [
        _list(c, OMAR).json(),                          # (35) another parent
        _list(c, ELENA, child="child_zzz").json(),      # (36) unknown child
        _list(c, ELENA, child="child_eli").json(),      # parent of another child
    ]
    assert _list(c, OMAR).status_code == 404
    assert _list(c, ELENA, child="child_zzz").status_code == 404
    assert all(b == NOT_FOUND_BODY for b in bodies), bodies   # (41) identical


def test_pending_paused_ended_connection_404():
    """(37)(38)(39)"""
    for idx, status in enumerate(
        ("pending_parent_acceptance", "paused_by_parent", "ended")
    ):
        c = _c()
        _propose(c, key=f"conn-{idx}")
        _set_connection(c, "conn_maya", status=status)
        r = _list(c, ELENA)
        assert r.status_code == 404, f"{status}: {r.status_code}"
        assert r.json() == NOT_FOUND_BODY


def test_unauthenticated_request_401():
    """(40)"""
    assert _c().get(LIST_URL).status_code == 401


# ── 42-49: privacy ──────────────────────────────────────────────────────────
def test_no_private_notes_other_children_or_unrelated_versions():
    c = _c()
    _propose(c)
    blob = _list(c, ELENA).text

    assert "AAC backup" not in blob                                    # (45)
    for other in ("child_eli", "child_noah", "child_theo", "child_sana",
                  "Omar", "Priya", "Unconnected"):
        assert other not in blob, other                                 # (46)
    # (47)(48)(49) unrelated / library / review versions never appear
    for title in ("Bubble requesting (Hannah's library copy)",
                  "Bubble requesting (submitted for Genex review)",
                  "Bubble requesting (draft, unattached)",
                  "Bubble requesting (Sana)", "Bubble requesting (Amara)",
                  "Bubble requesting (Rue)"):
        assert title not in blob, title
    for vid in ("ver_bubbles_v1", "ver_lib_hannah_v1", "ver_review_hannah_v1"):
        assert vid not in blob, vid                                     # (43)


def test_generic_catalog_visibility_unchanged_by_the_list():
    c = _c()
    _propose(c)
    _list(c, ELENA)
    assert c.get("/api/v1/activity-templates", headers=ELENA).status_code == 403
    detail = c.get("/api/v1/activity-templates/tmpl_bubbles", headers=UNCONNECTED).json()
    ids = [v["activity_version_id"] for v in detail["versions"]]
    assert "ver_lib_hannah_v1" not in ids and "ver_review_hannah_v1" not in ids


# ── 51-56: malformed records are excluded, not disclosed ────────────────────
def test_malformed_proposals_are_excluded_silently():
    """(51)(52)(53)(54)(55)(56)"""
    # (52) missing proposed ActivityVersion
    c = _c()
    created = _propose(c, key="m-1")
    pid = created["proposal"]["proposal_id"]
    _repo(c)._data[C.ACTIVITY_VERSIONS].pop(created["proposed_activity_version"]["id"])
    out = _list(c, ELENA)
    assert out.status_code == 200                                       # (55) no 500
    payload = out.json()
    assert pid not in [i["proposal_id"] for i in payload["items"]]
    assert payload["total"] == len(payload["items"])                     # (54)
    assert "excluded" not in out.text and "missing" not in out.text      # (56)

    # (51) missing original assignment
    c = _c()
    pid = _propose(c, key="m-2")["proposal"]["proposal_id"]
    _repo(c)._data[C.PLAN_ASSIGNMENTS].pop(MAYA_BUBBLES)
    payload = _list(c, ELENA).json()
    assert pid not in [i["proposal_id"] for i in payload["items"]]
    assert payload["total"] == len(payload["items"])

    # (53) proposal referencing another child's assignment
    c = _c()
    pid = _propose(c, key="m-3")["proposal"]["proposal_id"]
    repo = _repo(c)
    p = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]
    p["target_assignment_id"] = "assign_eli_bubbles"
    repo.set(C.PLAN_CHANGE_PROPOSALS, pid, p)
    payload = _list(c, ELENA).json()
    assert pid not in [i["proposal_id"] for i in payload["items"]]
    assert "child_eli" not in _list(c, ELENA).text


# ── 57-62: list / detail consistency ────────────────────────────────────────
def test_list_and_detail_agree_on_every_shared_field():
    c = _c()
    _propose(c, key="c-1")
    payload = _list(c, ELENA).json()

    for item in payload["items"]:
        detail = _detail(c, ELENA, item["proposal_id"]).json()
        assert detail["proposal"]["proposal_id"] == item["proposal_id"]        # (57)
        assert detail["proposal"]["proposal_type"] == item["proposal_type"]
        assert detail["proposal"]["proposal_status"] == item["proposal_status"]  # (58)
        assert detail["proposal"]["decided_at"] == item["decided_at"]
        assert detail["therapist"]["display_name"] == item["therapist"]["display_name"]  # (59)
        assert detail["proposed_activity"]["title"] == item["proposed_activity"]["title"]  # (60)
        assert detail["decision"]["can_accept"] == item["decision"]["can_accept"]   # (61)
        assert detail["decision"]["can_decline"] == item["decision"]["can_decline"]  # (62)


# ── 63-68: therapist backward compatibility ─────────────────────────────────
def test_therapist_list_contract_is_unchanged():
    c = _c()
    created = _propose(c)
    pid = created["proposal"]["proposal_id"]

    r = _list(c, HANNAH)
    assert r.status_code == 200                                          # (63)
    out = r.json()
    assert set(out.keys()) == {"items", "total", "next_cursor"}           # (66) envelope
    assert out["next_cursor"] is None
    item = next(i for i in out["items"] if i["proposal_id"] == pid)
    # (64) therapist item keeps its full flat field set, including save_scope
    assert item["save_scope"] == "child_only"
    assert item["child_id"] == "child_maya"
    assert item["proposed_activity_version_id"] == created["proposed_activity_version"]["id"]
    assert item["version"] == 1
    # (65) existing ordering: newest created_at first, id tie-break
    created_ats = [i["created_at"] for i in out["items"]]
    assert created_ats == sorted(created_ats, reverse=True)
    # parent-safe sections must not appear
    for section in ("decision", "proposed_activity", "therapist", "child"):
        assert section not in item


def test_unconnected_therapist_list_remains_existence_blind():
    """(68)"""
    c = _c()
    _propose(c)
    for headers in (UNCONNECTED, PRIYA):
        r = _list(c, headers)
        assert r.status_code == 404 and r.json() == NOT_FOUND_BODY


def test_therapist_and_parent_list_items_are_disjoint():
    c = _c()
    _propose(c)
    t = _list(c, HANNAH).json()["items"][0]
    p = _list(c, ELENA).json()["items"][0]
    assert set(t.keys()) & set(p.keys()) == {"proposal_id", "proposal_type",
                                            "proposal_status", "created_at",
                                            "decided_at", "change_reason"}
    # ...but the therapist-only identifiers never appear in the parent item
    for marker in ("save_scope", "child_id", "weekly_plan_id",
                   "proposed_activity_version_id", "version"):
        assert marker in t and marker not in p


# ── 69-75: read-only guarantees ─────────────────────────────────────────────
def test_list_and_eligibility_have_no_side_effects():
    """(69)-(75)"""
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    repo = _repo(c)

    tracked = (C.PLAN_ASSIGNMENTS, C.PLAN_CHANGE_PROPOSALS, C.ACTIVITY_VERSIONS,
               C.ACTIVITY_TEMPLATES, C.AUDIT_EVENTS, C.IDEMPOTENCY_RECORDS,
               C.CONNECTIONS, C.CHILDREN, C.MILESTONES, C.THERAPIST_PROFILES)
    before = {col: copy.deepcopy(repo._data.get(col, {})) for col in tracked}

    for _ in range(3):
        assert _list(c, ELENA).status_code == 200
        assert _list(c, HANNAH).status_code == 200
        assert _detail(c, ELENA, pid).status_code == 200
        # direct evaluator calls must be inert too
        for proposal in repo.query(C.PLAN_CHANGE_PROPOSALS, child_id="child_maya"):
            eligibility.evaluate_parent_decision(repo, "child_maya", proposal)
            eligibility.proposal_is_safe_to_show(repo, "child_maya", proposal)

    for col, snapshot in before.items():
        assert repo._data.get(col, {}) == snapshot, f"{col} changed during a read"


# ── 76-80: write compatibility ──────────────────────────────────────────────
def test_actionable_item_can_be_opened_and_accepted():
    """(76)(77)(79)"""
    c = _c()
    _propose(c, key="w-1")
    item = next(i for i in _list(c, ELENA).json()["items"]
                if i["decision"]["can_accept"])
    detail = _detail(c, ELENA, item["proposal_id"]).json()
    r = c.post(f"/api/v1/children/child_maya/proposals/{item['proposal_id']}/accept",
               headers={**ELENA, "Idempotency-Key": "w-acc"},
               json={"expected_proposal_version": detail["proposal"]["proposal_version"],
                     "expected_assignment_version":
                         detail["decision_context"]["expected_assignment_version"]})
    assert r.status_code == 200, r.text
    assert r.json()["proposal"]["proposal_status"] == "accepted"


def test_actionable_item_can_be_opened_and_declined():
    """(78)(80)"""
    c = _c()
    _propose(c, key="w-2")
    item = next(i for i in _list(c, ELENA).json()["items"]
                if i["decision"]["can_decline"])
    detail = _detail(c, ELENA, item["proposal_id"]).json()
    r = c.post(f"/api/v1/children/child_maya/proposals/{item['proposal_id']}/decline",
               headers={**ELENA, "Idempotency-Key": "w-dec"},
               json={"expected_proposal_version": detail["proposal"]["proposal_version"],
                     "expected_assignment_version":
                         detail["decision_context"]["expected_assignment_version"]})
    assert r.status_code == 200, r.text
    assert r.json()["proposal"]["proposal_status"] == "declined"


def test_list_reflects_the_decision_immediately():
    c = _c()
    pid = _propose(c, key="w-3")["proposal"]["proposal_id"]
    c.post(f"/api/v1/children/child_maya/proposals/{pid}/decline",
           headers={**ELENA, "Idempotency-Key": "w-dec2"},
           json={"expected_proposal_version": 1, "expected_assignment_version": 2})
    item = _item(_list(c, ELENA).json(), pid)
    assert item["proposal_status"] == "declined"
    assert item["decision"]["needs_parent_attention"] is False


# ── 84: isolation ───────────────────────────────────────────────────────────
def test_no_parent_api_or_genex_core_import():
    import ast
    import pathlib
    for name in ("services/eligibility.py", "services/read_service.py",
                 "api/routes.py", "api/schemas.py"):
        path = pathlib.Path(__file__).resolve().parent.parent / "app" / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert not a.name.startswith("genex_core"), name
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                assert (node.module or "").split(".")[0] != "genex_core", name
