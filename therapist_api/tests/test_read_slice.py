"""Read-slice endpoint + authorization tests (fictional dev data)."""

from __future__ import annotations

from tests.conftest import ELENA, HANNAH, UNCONNECTED, read_slice_client

client = read_slice_client()


# ── health / config ─────────────────────────────────────────────────────────
def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["service"] == "genex-api-therapist"


def test_app_config():
    r = client.get("/api/v1/app/config")
    assert r.status_code == 200 and r.json()["api_version"] == "v1"


# ── /me ──────────────────────────────────────────────────────────────────────
def test_me_therapist():
    r = client.get("/api/v1/me", headers=HANNAH)
    assert r.status_code == 200
    b = r.json()
    assert b["display_name"] == "Hannah Lieberknecht"
    assert b["credentials"] == "MA, SLP"
    assert b["organization"] == "TalkShop"


def test_me_requires_auth():
    assert client.get("/api/v1/me").status_code == 401


# ── connected children ───────────────────────────────────────────────────────
def test_connected_children_list():
    b = client.get("/api/v1/children", headers=HANNAH).json()
    assert b["total"] == 5
    by_id = {i["child_id"]: i for i in b["items"]}
    assert by_id["child_maya"]["connection_status"] == "active"
    assert by_id["child_amara"]["connection_status"] == "pending_parent_acceptance"
    assert by_id["child_sana"]["connection_status"] == "paused_by_parent"
    # restricted children expose no domains/counts
    assert by_id["child_amara"]["active_practice_domains"] == []
    assert by_id["child_amara"]["plan_review_count"] == 0


def test_connected_child_counts():
    by_id = {i["child_id"]: i for i in client.get("/api/v1/children", headers=HANNAH).json()["items"]}
    # Maya: 0 needs-review, 1 new note, 1 pending proposal
    assert by_id["child_maya"]["plan_review_count"] == 0
    assert by_id["child_maya"]["new_parent_note_count"] == 1
    assert by_id["child_maya"]["pending_proposal_count"] == 1
    # Eli: 1 needs-review
    assert by_id["child_eli"]["plan_review_count"] == 1
    # Noah: 1 new note
    assert by_id["child_noah"]["new_parent_note_count"] == 1


# ── child detail / restricted ────────────────────────────────────────────────
def test_active_child_overview():
    r = client.get("/api/v1/children/child_maya", headers=HANNAH)
    assert r.status_code == 200
    b = r.json()
    assert b["display_name"] == "Maya"
    assert "Talking & Communicating" in b["active_practice_domains"]
    assert b["connection_summary"]["restricted"] is False


def test_pending_child_overview_restricted_404():
    assert client.get("/api/v1/children/child_amara", headers=HANNAH).status_code == 404


def test_paused_child_overview_restricted_404():
    assert client.get("/api/v1/children/child_sana", headers=HANNAH).status_code == 404


def test_restricted_connection_details_available():
    r = client.get("/api/v1/children/child_amara/connection", headers=HANNAH)
    assert r.status_code == 200
    b = r.json()
    assert b["connection_status"] == "pending_parent_acceptance"
    assert b["restricted"] is True
    assert b["activation_reminder_simulated"] is True


def test_paused_connection_details_available():
    b = client.get("/api/v1/children/child_sana/connection", headers=HANNAH).json()
    assert b["connection_status"] == "paused_by_parent" and b["restricted"] is True


# ── weekly plan ──────────────────────────────────────────────────────────────
def test_weekly_plan_split_states_and_provenance():
    b = client.get("/api/v1/children/child_maya/weekly-plan", headers=HANNAH).json()
    assert b["child_id"] == "child_maya"
    a = {x["assignment_id"]: x for x in b["assignments"]}
    bub = a["assign_maya_bubbles"]
    # separate states, never collapsed
    assert bub["plan_approval_status"] == "approved"
    assert bub["practice_status"] == "did_it"
    assert bub["assignment_status"] == "current"
    assert bub["provenance"]["created_by_type"] == "genex"
    tt = a["assign_maya_turntake"]
    assert tt["plan_approval_status"] == "change_pending_parent"
    assert tt["pending_proposal_id"] == "prop_maya_modify_bubbles"


def test_weekly_plan_restricted_404():
    assert client.get("/api/v1/children/child_amara/weekly-plan", headers=HANNAH).status_code == 404


def test_plan_review_count_from_approval_status():
    b = client.get("/api/v1/children/child_eli/weekly-plan", headers=HANNAH).json()
    statuses = [a["plan_approval_status"] for a in b["assignments"]]
    assert "needs_plan_review" in statuses


# ── progress ─────────────────────────────────────────────────────────────────
def test_progress():
    b = client.get("/api/v1/children/child_maya/progress", headers=HANNAH).json()
    assert b["child_id"] == "child_maya"
    assert sum(b["practice_status_counts"].values()) == 2


# ── parent notes: split states + inbox aggregation ───────────────────────────
def test_child_notes_split_states():
    b = client.get("/api/v1/children/child_maya/notes", headers=HANNAH).json()
    notes = {n["note_id"]: n for n in b["items"]}
    n2 = notes["pn_maya_2"]
    assert n2["review_status"] == "reviewed"
    assert n2["session_preparation_status"] == "discuss_at_next_session"
    assert n2["note_type"] == "question"
    assert n2["linked_activity_title"] == "Turn-taking with a ball"


def test_inbox_aggregates_active_children_only():
    b = client.get("/api/v1/notes", headers=HANNAH).json()
    child_ids = {n["child_id"] for n in b["items"]}
    # only active children (maya, noah have notes); never restricted/paused
    assert child_ids <= {"child_maya", "child_eli", "child_noah"}
    assert "child_sana" not in child_ids and "child_amara" not in child_ids


# ── next session ─────────────────────────────────────────────────────────────
def test_next_session_aggregation():
    b = client.get("/api/v1/children/child_maya/next-session", headers=HANNAH).json()
    pn_ids = {n["note_id"] for n in b["parent_note_items"]}
    ptn_ids = {n["note_id"] for n in b["private_note_items"]}
    assert "pn_maya_2" in pn_ids           # discuss_at_next_session
    assert "ptn_maya_1" in ptn_ids         # private note marked_for_next_session


# ── private notes authorization ──────────────────────────────────────────────
def test_private_notes_visible_to_owning_therapist():
    b = client.get("/api/v1/children/child_maya/private-notes", headers=HANNAH).json()
    assert any(n["note_id"] == "ptn_maya_1" for n in b["items"])


def test_parent_cannot_read_private_notes():
    # Parent principal -> forbidden (403) via role check.
    assert client.get("/api/v1/children/child_maya/private-notes", headers=ELENA).status_code == 403


def test_parent_cannot_read_therapist_endpoints():
    assert client.get("/api/v1/me", headers=ELENA).status_code == 403
    assert client.get("/api/v1/children", headers=ELENA).status_code == 403


# ── unconnected therapist ────────────────────────────────────────────────────
def test_unconnected_therapist_sees_no_children():
    b = client.get("/api/v1/children", headers=UNCONNECTED).json()
    assert b["total"] == 0


def test_unconnected_therapist_denied_child():
    # Existence-blind: 404, not 403.
    assert client.get("/api/v1/children/child_maya", headers=UNCONNECTED).status_code == 404
    assert client.get("/api/v1/children/child_maya/private-notes", headers=UNCONNECTED).status_code == 404


# ── activity templates / versions provenance ─────────────────────────────────
def test_activity_templates_and_derived_version():
    b = client.get("/api/v1/activity-templates", headers=HANNAH).json()
    tmpl = {t["activity_template_id"]: t for t in b["items"]}
    bubbles = tmpl["tmpl_bubbles"]
    assert bubbles["immutable"] is True
    # derived version preserves original template linkage
    versions = {v["activity_version_id"]: v for v in bubbles["versions"]}
    derived = versions["ver_bubbles_hannah_v1"]
    assert derived["is_derived"] is True
    assert derived["original_activity_version_id"] == "ver_bubbles_v1"


def test_activity_template_detail_unknown_404():
    assert client.get("/api/v1/activity-templates/tmpl_zzz", headers=HANNAH).status_code == 404


# ── milestones: no age gating ────────────────────────────────────────────────
def test_milestones_no_age_gating():
    b = client.get("/api/v1/milestones", headers=HANNAH).json()
    assert b["total"] >= 3
    for m in b["items"]:
        assert m["age_gated"] is False
        # source age band retained for provenance only
        assert "domain" in m and "milestone_id" in m


def test_unknown_and_unauthorized_ids_are_404_not_403():
    # unknown id
    assert client.get("/api/v1/children/nope", headers=HANNAH).status_code == 404
    # unauthorized-but-existing id (restricted) also 404 for the full-content route
    assert client.get("/api/v1/children/child_sana", headers=HANNAH).status_code == 404
