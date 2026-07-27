"""Idempotent modify-activity proposal creation tests (fictional dev data)."""

from __future__ import annotations

import threading

from app.auth.interface import AuthenticatedUser
from app.domain.roles import UserRole
from app.fixtures import load_fixtures
from app.repository import collections as C
from app.repository.memory import InMemoryRepository
from app.services import proposal_service as P
from tests.conftest import ELENA, HANNAH, UNCONNECTED, read_slice_client

# Clean target: Noah's turntake (approved, no pending). Domain = Social & Emotional.
NOAH_AP = "/api/v1/children/child_noah/weekly-plan/assignments/assign_noah_turntake/proposals/modify"
MAYA_BUB_AP = "/api/v1/children/child_maya/weekly-plan/assignments/assign_maya_bubbles/proposals/modify"


def act_social(**over):
    a = {
        "title": "Turn-taking (adapted)", "developmental_domain": "social_and_emotional",
        "milestone_id": "mile_turn_taking", "skill_focus": "waiting a turn", "duration_minutes": 8,
        "difficulty": "just_right", "materials": ["soft ball"], "materials_type": "home_items",
        "setup": "Sit facing each other.", "parent_instructions": ["Name each turn."],
        "what_to_say": ["my turn", "your turn"], "how_to_help": ["Pause and wait."],
        "success_signals": ["Child waits for a turn."], "variations": ["Use a car."],
        "routine_tags": ["play"], "theme_tags": [], "safety_risk_flags": [],
    }
    a.update(over)
    return a


def body(version=1, **over):
    b = {"expected_assignment_version": version, "activity": act_social(),
         "change_reason": "Adjust based on session.", "save_scope": "child_only"}
    b.update(over)
    return b


def _c():
    return read_slice_client()


def _repo(c):
    return c.app.state.repo


def _post(c, path, headers, key, b):
    h = dict(headers)
    if key is not None:
        h["Idempotency-Key"] = key
    return c.post(path, headers=h, json=b)


# 1-14: happy path + preservation + read-after-write
def test_create_modify_proposal_full():
    c = _c()
    ov = c.get("/api/v1/children/child_noah", headers=HANNAH).json()
    assert ov["pending_proposal_count"] == 0 and ov["plan_review_count"] == 0
    r = _post(c, NOAH_AP, HANNAH, "n1", body())
    assert r.status_code == 200
    b = r.json()
    p = b["proposal"]
    assert p["proposal_status"] == "pending_parent_acceptance"       # (2)
    assert p["proposal_type"] == "modify"
    v = b["proposed_activity_version"]
    assert v["is_derived"] is True and v["created_by_type"] == "therapist"   # (7)(8)
    assert v["original_activity_version_id"] == "ver_turn_taking_v1"
    assert v["original_activity_template_id"] == "tmpl_turn_taking_ball"
    assert v["modified_by_user_id"] == "ther_hannah" and v["milestone_id"] == "mile_turn_taking"
    ca = b["current_assignment"]
    assert ca["activity_version_id"] == "ver_turn_taking_v1"          # (4) original version
    assert ca["plan_approval_status"] == "approved"                  # (11) unchanged
    assert ca["assignment_status"] == "current"                     # (3) still current
    assert ca["version"] == 2                                        # (10) +1
    assert ca["pending_proposal_id"] == p["proposal_id"]             # (9)
    assert b["child_summary"]["plan_review_count"] == 0             # (12)
    assert b["child_summary"]["pending_proposal_count"] == 1        # (13)
    # (5)(6) original template + version unchanged
    repo = _repo(c)
    tmpl = repo.query(C.ACTIVITY_TEMPLATES, id="tmpl_turn_taking_ball")[0]
    assert tmpl["title"] == "Turn-taking with a ball"
    orig = repo.query(C.ACTIVITY_VERSIONS, id="ver_turn_taking_v1")[0]
    assert orig["title"] == "Turn-taking with a ball" and orig["is_derived"] is False
    # (14) weekly-plan read: original assignment + pending proposal, not the proposed version
    wp = c.get("/api/v1/children/child_noah/weekly-plan", headers=HANNAH).json()
    a0 = wp["assignments"][0]
    assert a0["activity_version_id"] == "ver_turn_taking_v1"
    assert a0["plan_approval_status"] == "approved" and a0["pending_proposal_id"] == p["proposal_id"]
    assert a0["pending_proposal"]["proposal_status"] == "pending_parent_acceptance"
    # overview reflects pending, not needs-review
    ov2 = c.get("/api/v1/children/child_noah", headers=HANNAH).json()
    assert ov2["pending_proposal_count"] == 1 and ov2["plan_review_count"] == 0


# 15-20: idempotent replay
def test_idempotent_replay():
    c = _c()
    r1 = _post(c, NOAH_AP, HANNAH, "rk", body())
    assert r1.status_code == 200 and r1.json()["idempotent_replay"] is False
    r2 = _post(c, NOAH_AP, HANNAH, "rk", body())
    assert r2.status_code == 200 and r2.json()["idempotent_replay"] is True       # (16)
    assert r2.json()["proposal"]["proposal_id"] == r1.json()["proposal"]["proposal_id"]   # (17)
    assert r2.json()["proposed_activity_version"]["id"] == r1.json()["proposed_activity_version"]["id"]  # (18)
    assert r2.json()["current_assignment"]["version"] == 2                        # (19)
    repo = _repo(c)
    assert len(repo.query(C.PLAN_CHANGE_PROPOSALS, child_id="child_noah")) == 1   # one proposal
    assert len([v for v in repo.query(C.ACTIVITY_VERSIONS)
                if v.get("is_derived") and v["activity_template_id"] == "tmpl_turn_taking_ball"]) == 1
    assert len(repo.query(C.AUDIT_EVENTS)) == 1                                    # (20)


# 21-22: same-key conflicts
def test_same_key_different_body_conflict():
    c = _c()
    assert _post(c, NOAH_AP, HANNAH, "bk", body()).status_code == 200
    r = _post(c, NOAH_AP, HANNAH, "bk", body(change_reason="different"))
    assert r.status_code == 409 and r.json()["error"] == "idempotency_key_conflict"


def test_same_key_different_assignment_conflict():
    c = _c()
    assert _post(c, NOAH_AP, HANNAH, "dk", body()).status_code == 200
    # reuse key for a different assignment (maya bubbles) with a matching-domain activity
    b = body(); b["activity"] = {**act_social(), "developmental_domain": "talking_and_communicating",
                                 "milestone_id": "mile_request_items"}
    r = _post(c, MAYA_BUB_AP, HANNAH, "dk", b)
    assert r.status_code == 409 and r.json()["error"] == "idempotency_key_conflict"


# 23-24: missing / blank key
def test_missing_key_400():
    r = _post(_c(), NOAH_AP, HANNAH, None, body())
    assert r.status_code == 400 and r.json()["error"] == "missing_idempotency_key"


def test_blank_key_400():
    r = _post(_c(), NOAH_AP, HANNAH, "  ", body())
    assert r.status_code == 400 and r.json()["error"] == "missing_idempotency_key"


# 25: stale version
def test_stale_version_conflict_no_side_effects():
    c = _c()
    r = _post(c, NOAH_AP, HANNAH, "vk", body(version=9))
    assert r.status_code == 409 and r.json()["error"] == "assignment_version_conflict"
    repo = _repo(c)
    assert len(repo.query(C.PLAN_CHANGE_PROPOSALS, child_id="child_noah")) == 0
    assert len([v for v in repo.query(C.ACTIVITY_VERSIONS) if v.get("is_derived") and v["activity_template_id"] == "tmpl_turn_taking_ball"]) == 0
    assert len(repo.query(C.AUDIT_EVENTS)) == 0 and len(repo.query(C.IDEMPOTENCY_RECORDS)) == 0


# 26-29: invalid source states
def test_needs_review_cannot_be_modified():
    c = _c()
    b = body(); b["activity"] = {**act_social(), "developmental_domain": "talking_and_communicating", "milestone_id": "mile_request_items"}
    r = _post(c, "/api/v1/children/child_eli/weekly-plan/assignments/assign_eli_bubbles/proposals/modify", HANNAH, "e1", b)
    assert r.status_code == 409 and r.json()["error"] == "invalid_modify_proposal_transition"


def _set(c, aid, **fields):
    repo = _repo(c); a = repo.query(C.PLAN_ASSIGNMENTS, id=aid)[0]; a.update(fields); repo.set(C.PLAN_ASSIGNMENTS, aid, a)


def test_archived_cannot_be_modified():
    c = _c(); _set(c, "assign_noah_turntake", plan_approval_status="archived")
    assert _post(c, NOAH_AP, HANNAH, "ar", body()).json()["error"] == "invalid_modify_proposal_transition"


def test_replaced_cannot_be_modified():
    c = _c(); _set(c, "assign_noah_turntake", plan_approval_status="replaced")
    assert _post(c, NOAH_AP, HANNAH, "rp", body()).json()["error"] == "invalid_modify_proposal_transition"


def test_outside_current_plan():
    c = _c(); _set(c, "assign_noah_turntake", weekly_plan_id="wp_bogus")
    assert _post(c, NOAH_AP, HANNAH, "op", body()).json()["error"] == "invalid_modify_proposal_transition"


# 30: existing pending proposal
def test_existing_pending_proposal_conflict():
    c = _c()
    # Maya's turntake is change_pending_parent with a pending proposal -> both guards; force approved to isolate pending guard
    _set(c, "assign_maya_turntake", plan_approval_status="approved")  # keep pending_proposal_id
    b = body(); b["activity"] = act_social()
    r = _post(c, "/api/v1/children/child_maya/weekly-plan/assignments/assign_maya_turntake/proposals/modify", HANNAH, "pp", b)
    assert r.status_code == 409 and r.json()["error"] == "pending_proposal_exists"


# 31-36: authorization / existence-blind
def test_child_assignment_mismatch_404():
    c = _c()
    r = _post(c, "/api/v1/children/child_noah/weekly-plan/assignments/assign_maya_bubbles/proposals/modify", HANNAH, "mm", body())
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_unconnected_404():
    assert _post(_c(), NOAH_AP, UNCONNECTED, "u1", body()).status_code == 404


def test_parent_403():
    r = _post(_c(), NOAH_AP, ELENA, "p1", body())
    assert r.status_code == 403 and r.json()["error"] == "forbidden"


def test_pending_connection_404():
    c = _c()
    r = _post(c, "/api/v1/children/child_amara/weekly-plan/assignments/assign_noah_turntake/proposals/modify", HANNAH, "pc", body())
    assert r.status_code == 404


def test_paused_connection_404():
    c = _c()
    r = _post(c, "/api/v1/children/child_sana/weekly-plan/assignments/assign_noah_turntake/proposals/modify", HANNAH, "ps", body())
    assert r.status_code == 404


def test_unknown_child_and_assignment_404():
    c = _c()
    assert _post(c, "/api/v1/children/child_zzz/weekly-plan/assignments/assign_noah_turntake/proposals/modify", HANNAH, "x1", body()).status_code == 404
    assert _post(c, "/api/v1/children/child_noah/weekly-plan/assignments/assign_zzz/proposals/modify", HANNAH, "x2", body()).status_code == 404


# 37-38: milestone / domain
def test_invalid_milestone():
    c = _c()
    b = body(); b["activity"] = act_social(milestone_id="mile_does_not_exist")
    r = _post(c, NOAH_AP, HANNAH, "im", b)
    assert r.status_code == 422 and r.json()["error"] == "milestone_domain_mismatch"


def test_milestone_domain_mismatch_no_mutation():
    c = _c()
    b = body(); b["activity"] = act_social(developmental_domain="talking_and_communicating")  # milestone is social
    r = _post(c, NOAH_AP, HANNAH, "dm", b)
    assert r.status_code == 422 and r.json()["error"] == "milestone_domain_mismatch"
    repo = _repo(c)
    assert _repo(c).query(C.PLAN_ASSIGNMENTS, id="assign_noah_turntake")[0]["version"] == 1
    assert len(repo.query(C.PLAN_CHANGE_PROPOSALS, child_id="child_noah")) == 0


# 39: concurrency
def test_concurrent_single_proposal():
    repo = InMemoryRepository(); load_fixtures(repo)
    user = AuthenticatedUser(uid="dev-hannah", environment="dev", role=UserRole.SLP)
    baseline_versions = len(repo.query(C.ACTIVITY_VERSIONS))
    results = {}

    def worker(name, key):
        try:
            P.create_modify_proposal(repo, user, child_id="child_noah", assignment_id="assign_noah_turntake",
                                     idempotency_key=key, expected_assignment_version=1, activity=act_social())
            results[name] = "ok"
        except P.ApprovalError as e:
            results[name] = e.code

    t1 = threading.Thread(target=worker, args=("a", "cc-a")); t2 = threading.Thread(target=worker, args=("b", "cc-b"))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert "ok" in results.values()
    assert "pending_proposal_exists" in results.values()
    assert len(repo.query(C.PLAN_CHANGE_PROPOSALS, child_id="child_noah")) == 1
    assert len(repo.query(C.ACTIVITY_VERSIONS)) == baseline_versions + 1
    assert len(repo.query(C.AUDIT_EVENTS)) == 1
    assert repo.query(C.PLAN_ASSIGNMENTS, id="assign_noah_turntake")[0]["version"] == 2


# 40: failed op leaves collections unchanged + explicit rollback of writes
def test_failed_op_leaves_collections_unchanged():
    c = _c(); repo = _repo(c)
    snap = {col: len(repo.query(col)) for col in (C.ACTIVITY_VERSIONS, C.PLAN_CHANGE_PROPOSALS, C.AUDIT_EVENTS, C.IDEMPOTENCY_RECORDS)}
    b = body(); b["activity"] = act_social(milestone_id="nope")
    _post(c, NOAH_AP, HANNAH, "f1", b)  # fails
    for col, n in snap.items():
        assert len(repo.query(col)) == n
    assert repo.query(C.PLAN_ASSIGNMENTS, id="assign_noah_turntake")[0]["version"] == 1


def test_transaction_rolls_back_writes_on_error():
    repo = InMemoryRepository(); load_fixtures(repo)
    before = len(repo.query(C.AUDIT_EVENTS))

    def bad(tx):
        tx.set(C.AUDIT_EVENTS, "aud_temp", {"id": "aud_temp"})
        raise P.InvalidRequest("boom")

    try:
        repo.run_in_transaction(bad)
    except P.InvalidRequest:
        pass
    assert len(repo.query(C.AUDIT_EVENTS)) == before  # write rolled back


# 41: raw key absent from stored records/audit
def test_raw_key_absent():
    c = _c()
    _post(c, NOAH_AP, HANNAH, "raw-secret-42", body())
    repo = _repo(c)
    for col in (C.AUDIT_EVENTS, C.IDEMPOTENCY_RECORDS):
        assert "raw-secret-42" not in str(repo.query(col))
    aud = repo.query(C.AUDIT_EVENTS)[0]
    assert aud["event_type"] == "plan_change_proposal_created"
    assert aud["idempotency_key_hash"] and len(aud["idempotency_key_hash"]) == 64
    assert aud["before_state"] == "approved" and aud["after_state"] == "approved"  # approval unchanged


# 42: no Parent API / genex_core import in new modules
def test_no_parent_or_genex_core_import():
    import ast
    import pathlib
    path = pathlib.Path(__file__).resolve().parent.parent / "app" / "services" / "proposal_service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert not a.name.startswith("genex_core")
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            assert (node.module or "").split(".")[0] not in ("genex_core", "api")


# read-only proposal endpoints
def test_read_proposal_endpoints():
    c = _c()
    r = _post(c, NOAH_AP, HANNAH, "rp1", body())
    pid = r.json()["proposal"]["proposal_id"]
    lst = c.get("/api/v1/children/child_noah/proposals", headers=HANNAH).json()
    assert any(p["proposal_id"] == pid for p in lst["items"])
    det = c.get(f"/api/v1/children/child_noah/proposals/{pid}", headers=HANNAH).json()
    assert det["proposal_status"] == "pending_parent_acceptance" and det["proposal_type"] == "modify"
    # existence-blind unknown proposal
    assert c.get("/api/v1/children/child_noah/proposals/prop_zzz", headers=HANNAH).status_code == 404
    # parent forbidden
    assert c.get("/api/v1/children/child_noah/proposals", headers=ELENA).status_code == 403
