"""Idempotent weekly-plan approval write tests (fictional dev data)."""

from __future__ import annotations

import threading

import pytest

from app.fixtures import load_fixtures
from app.repository import collections as C
from app.repository.memory import InMemoryRepository
from app.services import approval_service as A
from app.auth.interface import AuthenticatedUser
from app.domain.roles import UserRole
from tests.conftest import ELENA, HANNAH, UNCONNECTED, read_slice_client

ELI_AP = "/api/v1/children/child_eli/weekly-plan/assignments/assign_eli_bubbles/approve"
KEY = {"Idempotency-Key": "key-eli-1"}


def _c():
    """Fresh app+client (isolated in-memory state per test)."""
    return read_slice_client()


def _repo(c):
    return c.app.state.repo


def _children(c, headers=HANNAH):
    return {i["child_id"]: i for i in c.get("/api/v1/children", headers=headers).json()["items"]}


def _approve(c, headers, key, version, path=ELI_AP):
    h = dict(headers)
    if key is not None:
        h["Idempotency-Key"] = key
    return c.post(path, headers=h, json={"expected_assignment_version": version})


# 1-4, 6-7: happy path + read-after-write
def test_approve_success_and_transition_and_version_and_count():
    c = _c()
    assert _children(c)["child_eli"]["plan_review_count"] == 1
    r = _approve(c, HANNAH, "k1", 1)
    assert r.status_code == 200
    b = r.json()
    assert b["assignment"]["plan_approval_status"] == "approved"   # (2) transition
    assert b["assignment"]["version"] == 2                          # (3) increment once
    assert b["child_summary"]["plan_review_count"] == 0            # (4)+(5) last item -> 0
    assert b["idempotent_replay"] is False
    # (6) weekly-plan read reflects approval + version
    wp = c.get("/api/v1/children/child_eli/weekly-plan", headers=HANNAH).json()
    a0 = wp["assignments"][0]
    assert a0["plan_approval_status"] == "approved" and a0["version"] == 2
    # (7) children-list read reflects approval
    assert _children(c)["child_eli"]["plan_review_count"] == 0


# 8-11: idempotent replay
def test_idempotent_replay():
    c = _c()
    r1 = _approve(c, HANNAH, "kX", 1)
    assert r1.status_code == 200 and r1.json()["idempotent_replay"] is False
    aud1 = r1.json()["audit_event_id"]
    r2 = _approve(c, HANNAH, "kX", 1)                 # (8) exact replay succeeds
    assert r2.status_code == 200
    assert r2.json()["idempotent_replay"] is True     # (9)
    assert r2.json()["assignment"]["version"] == 2    # (10) not incremented twice
    assert r2.json()["audit_event_id"] == aud1        # (11) one audit event
    assert len(_repo(c).query(C.AUDIT_EVENTS)) == 1


# 12: same key, different assignment -> 409
def test_same_key_different_assignment_conflict():
    c = _c()
    assert _approve(c, HANNAH, "dupkey", 1).status_code == 200  # eli
    # reuse key for a DIFFERENT valid+accessible assignment (maya turntake exists)
    r = _approve(c, HANNAH, "dupkey", 1,
                 path="/api/v1/children/child_maya/weekly-plan/assignments/assign_maya_turntake/approve")
    assert r.status_code == 409 and r.json()["error"] == "idempotency_key_conflict"


# 13: same key, different body/version -> 409
def test_same_key_different_body_conflict():
    c = _c()
    assert _approve(c, HANNAH, "bkey", 1).status_code == 200
    r = _approve(c, HANNAH, "bkey", 999)  # different expected_version -> different hash
    assert r.status_code == 409 and r.json()["error"] == "idempotency_key_conflict"


# 14-15: missing / blank key -> 400
def test_missing_key_400():
    r = _approve(_c(), HANNAH, None, 1)
    assert r.status_code == 400 and r.json()["error"] == "missing_idempotency_key"


def test_blank_key_400():
    r = _approve(_c(), HANNAH, "   ", 1)
    assert r.status_code == 400 and r.json()["error"] == "missing_idempotency_key"


# 16: stale expected version -> 409, no side effects
def test_stale_version_conflict_no_side_effects():
    c = _c()
    r = _approve(c, HANNAH, "vkey", 5)  # current version is 1
    assert r.status_code == 409 and r.json()["error"] == "assignment_version_conflict"
    # unchanged + no audit + no idempotency record stored
    assert _children(c)["child_eli"]["plan_review_count"] == 1
    assert len(_repo(c).query(C.AUDIT_EVENTS)) == 0
    assert len(_repo(c).query(C.IDEMPOTENCY_RECORDS)) == 0


# 17: already approved + new key -> 409
def test_already_approved_new_key_conflict():
    c = _c()
    assert _approve(c, HANNAH, "a1", 1).status_code == 200
    r = _approve(c, HANNAH, "a2", 2)  # new key, but already approved
    assert r.status_code == 409 and r.json()["error"] == "invalid_plan_approval_transition"


# 18-20: invalid source states
def _set_status(c, assignment_id, status):
    repo = _repo(c)
    a = repo.query(C.PLAN_ASSIGNMENTS, id=assignment_id)[0]
    a["plan_approval_status"] = status
    repo.set(C.PLAN_ASSIGNMENTS, assignment_id, a)


def test_archived_cannot_be_approved():
    c = _c(); _set_status(c, "assign_eli_bubbles", "archived")
    assert _approve(c, HANNAH, "arc", 1).json()["error"] == "invalid_plan_approval_transition"


def test_replaced_cannot_be_approved():
    c = _c(); _set_status(c, "assign_eli_bubbles", "replaced")
    assert _approve(c, HANNAH, "rep", 1).json()["error"] == "invalid_plan_approval_transition"


def test_change_pending_cannot_be_approved():
    c = _c()
    r = _approve(c, HANNAH, "cp", 1,
                 path="/api/v1/children/child_maya/weekly-plan/assignments/assign_maya_turntake/approve")
    assert r.status_code == 409 and r.json()["error"] == "invalid_plan_approval_transition"


# 21: assignment outside current plan -> 409
def test_outside_current_plan():
    c = _c()
    repo = _repo(c)
    a = repo.query(C.PLAN_ASSIGNMENTS, id="assign_eli_bubbles")[0]
    a["weekly_plan_id"] = "wp_not_current"  # no longer the child's current plan
    repo.set(C.PLAN_ASSIGNMENTS, "assign_eli_bubbles", a)
    assert _approve(c, HANNAH, "op", 1).json()["error"] == "invalid_plan_approval_transition"


# 22: assignment-child mismatch -> 404 (existence-blind)
def test_assignment_child_mismatch_404():
    c = _c()
    r = _approve(c, HANNAH, "mm", 1,
                 path="/api/v1/children/child_maya/weekly-plan/assignments/assign_eli_bubbles/approve")
    assert r.status_code == 404 and r.json()["error"] == "not_found"


# 23-26: authorization
def test_unconnected_therapist_404():
    r = _approve(_c(), UNCONNECTED, "u1", 1)
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_parent_403():
    r = _approve(_c(), ELENA, "p1", 1)
    assert r.status_code == 403 and r.json()["error"] == "forbidden"


def test_pending_connection_404():
    c = _c()
    r = _approve(c, HANNAH, "pc", 1,
                 path="/api/v1/children/child_amara/weekly-plan/assignments/assign_eli_bubbles/approve")
    assert r.status_code == 404


def test_paused_connection_404():
    c = _c()
    r = _approve(c, HANNAH, "ps", 1,
                 path="/api/v1/children/child_sana/weekly-plan/assignments/assign_eli_bubbles/approve")
    assert r.status_code == 404


# 27: unknown child + unknown assignment existence-blind 404
def test_unknown_child_and_assignment_404():
    c = _c()
    assert _approve(c, HANNAH, "x1", 1,
                    path="/api/v1/children/child_zzz/weekly-plan/assignments/assign_eli_bubbles/approve").status_code == 404
    assert _approve(c, HANNAH, "x2", 1,
                    path="/api/v1/children/child_eli/weekly-plan/assignments/assign_zzz/approve").status_code == 404


# 28: concurrency — two requests cannot create two approvals / two audit events
def test_concurrent_cannot_double_approve():
    repo = InMemoryRepository()
    load_fixtures(repo)
    user = AuthenticatedUser(uid="dev-hannah", environment="dev", role=UserRole.SLP)
    results = {}

    def worker(name, key):
        try:
            r = A.approve_assignment(repo, user, child_id="child_eli",
                                     assignment_id="assign_eli_bubbles", idempotency_key=key,
                                     expected_assignment_version=1)
            results[name] = ("ok", r["idempotent_replay"])
        except A.ApprovalError as e:
            results[name] = ("err", e.code)

    t1 = threading.Thread(target=worker, args=("a", "cc-a"))
    t2 = threading.Thread(target=worker, args=("b", "cc-b"))
    t1.start(); t2.start(); t1.join(); t2.join()
    outcomes = sorted(results.values())
    # exactly one real approval; the other sees already-approved (different key)
    assert ("ok", False) in results.values()
    assert ("err", "invalid_plan_approval_transition") in results.values()
    # exactly one audit event; assignment incremented exactly once
    assert len(repo.query(C.AUDIT_EVENTS)) == 1
    assert repo.query(C.PLAN_ASSIGNMENTS, id="assign_eli_bubbles")[0]["version"] == 2


# 29: failed write leaves state unchanged (covered by version-conflict) + explicit audit check
def test_failed_write_leaves_state_unchanged():
    c = _c()
    before = c.get("/api/v1/children/child_eli/weekly-plan", headers=HANNAH).json()["assignments"][0]
    _approve(c, HANNAH, "f1", 42)  # version conflict
    after = c.get("/api/v1/children/child_eli/weekly-plan", headers=HANNAH).json()["assignments"][0]
    assert before == after
    assert len(_repo(c).query(C.AUDIT_EVENTS)) == 0


# 30: no Parent API / genex_core import introduced (belt-and-suspenders alongside test_isolation)
def test_no_parent_or_genex_core_import_in_new_modules():
    import ast
    import pathlib
    for name in ("services/approval_service.py", "api/schemas.py", "api/routes.py"):
        path = pathlib.Path(__file__).resolve().parent.parent / "app" / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert not a.name.startswith("genex_core")
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                assert (node.module or "").split(".")[0] not in ("genex_core", "api")


# audit event content is safe (no raw key / token)
def test_audit_event_is_safe_and_immutable_shape():
    c = _c()
    r = _approve(c, HANNAH, "raw-secret-key", 1)
    aud = _repo(c).query(C.AUDIT_EVENTS)[0]
    assert aud["event_type"] == "plan_assignment_approved"
    assert aud["before_state"] == "needs_plan_review" and aud["after_state"] == "approved"
    assert "raw-secret-key" not in str(aud)          # raw key never stored
    assert aud["idempotency_key_hash"] and len(aud["idempotency_key_hash"]) == 64
