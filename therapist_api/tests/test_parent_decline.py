"""Idempotent parent decline of a modify proposal (Phase 1B.2B.2).

Every proposal is created through the REAL therapist modify endpoint, so each
test exercises the true lifecycle (therapist proposes -> parent declines). The
defining property under test is what decline does NOT do: no replacement
assignment, and the original plan item is left exactly as the family knows it.
All identities and data are fictional.
"""

from __future__ import annotations

import json
import threading

from app.auth.interface import AuthenticatedUser
from app.domain.ids import operation_identity, operation_scoped_id
from app.domain.roles import UserRole
from app.fixtures import load_fixtures
from app.repository import collections as C
from app.repository.memory import InMemoryRepository
from app.services import decline_service as D
from tests.conftest import ELENA, HANNAH, OMAR, PRIYA, UNCONNECTED, read_slice_client
from tests.test_modify_proposal import body
from tests.test_parent_acceptance import (
    MAYA_BUBBLES,
    MAYA_MODIFY_PATH,
    _talking_activity,
)


def _c():
    return read_slice_client()


def _repo(c):
    return c.app.state.repo


def _propose(client, key="prop-1"):
    b = {**body(), "activity": _talking_activity()}
    r = client.post(MAYA_MODIFY_PATH, headers={**HANNAH, "Idempotency-Key": key}, json=b)
    assert r.status_code == 200, r.text
    return r.json()


def _decline(client, headers, key, proposal_id, child="child_maya",
             proposal_version=1, assignment_version=2):
    h = dict(headers)
    if key is not None:
        h["Idempotency-Key"] = key
    return client.post(
        f"/api/v1/children/{child}/proposals/{proposal_id}/decline",
        headers=h,
        json={"expected_proposal_version": proposal_version,
              "expected_assignment_version": assignment_version},
    )


def _accept(client, headers, key, proposal_id, proposal_version=1, assignment_version=2):
    return client.post(
        f"/api/v1/children/child_maya/proposals/{proposal_id}/accept",
        headers={**headers, "Idempotency-Key": key},
        json={"expected_proposal_version": proposal_version,
              "expected_assignment_version": assignment_version},
    )


def _set_connection(client, conn_id, **fields):
    repo = _repo(client)
    conn = repo.query(C.CONNECTIONS, id=conn_id)[0]
    conn.update(fields)
    repo.set(C.CONNECTIONS, conn_id, conn)


def _replacements(repo, child_id="child_maya"):
    return [a for a in repo.query(C.PLAN_ASSIGNMENTS, child_id=child_id)
            if a.get("replaces_assignment_id") or a.get("source_proposal_id")]


def _declined_events(repo):
    return [e for e in repo.query(C.AUDIT_EVENTS)
            if e["event_type"] == "plan_change_proposal_declined"]


# ── 1-21: happy path ────────────────────────────────────────────────────────
def test_parent_declines_and_original_is_preserved():
    c = _c()
    created = _propose(c)
    pid = created["proposal"]["proposal_id"]
    proposed_version_id = created["proposed_activity_version"]["id"]

    # practice status before the decision, to prove it is untouched
    repo = _repo(c)
    practice_before = repo.query(C.PLAN_ASSIGNMENTS, id=MAYA_BUBBLES)[0]["practice_status"]

    r = _decline(c, ELENA, "dec-1", pid)
    assert r.status_code == 200, r.text
    out = r.json()

    p = out["proposal"]
    assert p["proposal_status"] == "declined"            # (2)
    assert p["version"] == 2                              # (3) +1 exactly once
    assert p["decided_by_user_id"] == "par_elena"         # (4)
    assert p["decided_by_role"] == "parent" and p["decided_at"]
    assert p["resulting_assignment_id"] is None           # (5)

    a = out["current_assignment"]
    assert a["assignment_id"] == MAYA_BUBBLES
    assert a["assignment_status"] == "current"            # (6)
    assert a["plan_approval_status"] == "approved"        # (7)
    assert a["practice_status"] == practice_before        # (8) unchanged
    assert a["activity_version_id"] == "ver_bubbles_v1"   # (9) original
    assert a["version"] == 3                              # (10) 1 -> 2 -> 3
    assert a["pending_proposal_id"] is None               # (11)
    assert a["replaced_by_assignment_id"] is None
    assert a["replaced_at"] is None
    assert out["idempotent_replay"] is False

    # (12) no replacement anywhere
    assert _replacements(repo) == []

    # (13) exactly one current assignment in the slot — still the original
    in_slot = [x for x in repo.query(C.PLAN_ASSIGNMENTS, child_id="child_maya")
               if x["scheduled_day"] == a["scheduled_day"]
               and x["assignment_status"] == "current"]
    assert [x["id"] for x in in_slot] == [MAYA_BUBBLES]

    # (14)(15) proposed version preserved, immutable, and NOT activated
    pv = repo.query(C.ACTIVITY_VERSIONS, id=proposed_version_id)[0]
    assert pv["immutable"] is True and pv["is_derived"] is True
    assert not [x for x in repo.query(C.PLAN_ASSIGNMENTS, child_id="child_maya")
                if x["activity_version_id"] == proposed_version_id]

    # (16)(17) source records untouched
    tmpl = repo.query(C.ACTIVITY_TEMPLATES, id="tmpl_bubbles")[0]
    assert tmpl["title"] == "Bubble requesting"
    orig_v = repo.query(C.ACTIVITY_VERSIONS, id="ver_bubbles_v1")[0]
    assert orig_v["title"] == "Bubble requesting" and orig_v["is_derived"] is False

    # (18)(19) counts
    assert out["child_summary"]["pending_proposal_count"] == 1   # Maya's fixture one
    assert out["child_summary"]["plan_review_count"] == 0


def test_weekly_plan_still_shows_the_original():
    """(20) read-after-write: nothing about the plan item changed for the family."""
    c = _c()
    created = _propose(c)
    pid = created["proposal"]["proposal_id"]
    _decline(c, ELENA, "dec-2", pid)

    plan = c.get("/api/v1/children/child_maya/weekly-plan", headers=HANNAH).json()
    item = next(a for a in plan["assignments"] if a["assignment_id"] == MAYA_BUBBLES)
    assert item["activity_version_id"] == "ver_bubbles_v1"
    assert item["plan_approval_status"] == "approved"
    assert item["pending_proposal_id"] is None
    assert item["pending_proposal"] is None
    # no replacement appears in the plan
    assert len([a for a in plan["assignments"]
                if a["activity_version_id"] == created["proposed_activity_version"]["id"]]) == 0


def test_proposal_read_shows_declined_with_no_resulting_assignment():
    """(21) therapist proposal read reflects the decision immediately."""
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    _decline(c, ELENA, "dec-3", pid)

    view = c.get(f"/api/v1/children/child_maya/proposals/{pid}", headers=HANNAH).json()
    assert view["proposal_status"] == "declined"
    assert view["resulting_assignment_id"] is None
    assert view["decided_by_user_id"] == "par_elena"
    assert view["decided_by_role"] == "parent" and view["decided_at"]


def test_children_counts_after_decline():
    c = _c()
    before = next(x for x in c.get("/api/v1/children", headers=HANNAH).json()["items"]
                  if x["child_id"] == "child_maya")
    pid = _propose(c)["proposal"]["proposal_id"]
    mid = next(x for x in c.get("/api/v1/children", headers=HANNAH).json()["items"]
               if x["child_id"] == "child_maya")
    assert mid["pending_proposal_count"] == before["pending_proposal_count"] + 1

    _decline(c, ELENA, "dec-4", pid)
    after = next(x for x in c.get("/api/v1/children", headers=HANNAH).json()["items"]
                 if x["child_id"] == "child_maya")
    assert after["pending_proposal_count"] == mid["pending_proposal_count"] - 1
    assert after["plan_review_count"] == before["plan_review_count"]

    overview = c.get("/api/v1/children/child_maya", headers=HANNAH).json()
    assert overview["plan_review_count"] == before["plan_review_count"]


def test_activity_template_unchanged_and_derived_version_not_published():
    c = _c()
    created = _propose(c)
    pid = created["proposal"]["proposal_id"]
    proposed_version_id = created["proposed_activity_version"]["id"]
    _decline(c, ELENA, "dec-5", pid)

    detail = c.get("/api/v1/activity-templates/tmpl_bubbles", headers=HANNAH).json()
    assert detail["title"] == "Bubble requesting" and detail["immutable"] is True
    # decline does not publish the derived version to an unrelated therapist
    other = c.get("/api/v1/activity-templates/tmpl_bubbles", headers=UNCONNECTED).json()
    assert proposed_version_id not in [v["activity_version_id"] for v in other["versions"]]


# ── 22-29: authorization ────────────────────────────────────────────────────
def test_therapist_principal_forbidden():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    r = _decline(c, HANNAH, "az-1", pid)
    assert r.status_code == 403 and r.json()["error"] == "forbidden"
    assert _decline(c, UNCONNECTED, "az-2", pid).status_code == 403
    assert _decline(c, PRIYA, "az-3", pid).status_code == 403


def test_other_parent_is_existence_blind_404():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    r = _decline(c, OMAR, "az-4", pid)
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_unknown_child_proposal_and_mismatch_404():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    assert _decline(c, ELENA, "az-5", pid, child="child_zzz").status_code == 404
    assert _decline(c, ELENA, "az-6", "prop_zzz").status_code == 404
    assert _decline(c, ELENA, "az-7", pid, child="child_eli").status_code == 404


def test_pending_paused_ended_connection_404():
    for idx, status in enumerate(
        ("pending_parent_acceptance", "paused_by_parent", "ended")
    ):
        c = _c()
        pid = _propose(c)["proposal"]["proposal_id"]
        _set_connection(c, "conn_maya", status=status)
        r = _decline(c, ELENA, f"az-conn-{idx}", pid)
        assert r.status_code == 404, f"{status}: {r.status_code}"


def test_unauthenticated_request_401():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    r = c.post(f"/api/v1/children/child_maya/proposals/{pid}/decline",
               headers={"Idempotency-Key": "az-8"},
               json={"expected_proposal_version": 1, "expected_assignment_version": 2})
    assert r.status_code == 401


# ── 30-40: versions and state ───────────────────────────────────────────────
def _assert_no_mutation(client, proposal_id):
    repo = _repo(client)
    p = repo.query(C.PLAN_CHANGE_PROPOSALS, id=proposal_id)[0]
    assert p["status"] == "pending_parent_acceptance" and p["version"] == 1
    a = repo.query(C.PLAN_ASSIGNMENTS, id=MAYA_BUBBLES)[0]
    assert a["assignment_status"] == "current" and a["version"] == 2
    assert a["pending_proposal_id"] == proposal_id
    assert _replacements(repo) == []
    assert _declined_events(repo) == []


def test_stale_proposal_version_conflict_no_mutation():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    r = _decline(c, ELENA, "v-1", pid, proposal_version=9)
    assert r.status_code == 409 and r.json()["error"] == "proposal_version_conflict"
    _assert_no_mutation(c, pid)


def test_stale_assignment_version_conflict_no_mutation():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    r = _decline(c, ELENA, "v-2", pid, assignment_version=99)
    assert r.status_code == 409 and r.json()["error"] == "assignment_version_conflict"
    _assert_no_mutation(c, pid)


def test_already_declined_with_new_key_conflicts():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    assert _decline(c, ELENA, "v-3", pid).status_code == 200
    r = _decline(c, ELENA, "v-4-new-key", pid, proposal_version=2, assignment_version=3)
    assert r.status_code == 409 and r.json()["error"] == "proposal_already_decided"


def test_already_accepted_cannot_be_declined():
    """(33) an accepted proposal is decided; decline must refuse it."""
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    assert _accept(c, ELENA, "acc-x", pid).status_code == 200
    r = _decline(c, ELENA, "v-5", pid, proposal_version=2, assignment_version=3)
    assert r.status_code == 409 and r.json()["error"] == "proposal_already_decided"


def test_non_modify_proposal_cannot_be_declined():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    repo = _repo(c)
    p = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]
    p["proposal_type"] = "remove"
    repo.set(C.PLAN_CHANGE_PROPOSALS, pid, p)
    r = _decline(c, ELENA, "v-6", pid)
    assert r.status_code == 409 and r.json()["error"] == "invalid_parent_decline_transition"


def test_assignment_no_longer_current_conflicts():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    repo = _repo(c)
    a = repo.query(C.PLAN_ASSIGNMENTS, id=MAYA_BUBBLES)[0]
    a["assignment_status"] = "retired"
    repo.set(C.PLAN_ASSIGNMENTS, MAYA_BUBBLES, a)
    r = _decline(c, ELENA, "v-7", pid)
    assert r.status_code == 409 and r.json()["error"] == "invalid_parent_decline_transition"


def test_pending_proposal_id_mismatch_conflicts():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    repo = _repo(c)
    a = repo.query(C.PLAN_ASSIGNMENTS, id=MAYA_BUBBLES)[0]
    a["pending_proposal_id"] = "prop_someone_else"
    repo.set(C.PLAN_ASSIGNMENTS, MAYA_BUBBLES, a)
    r = _decline(c, ELENA, "v-8", pid)
    assert r.status_code == 409 and r.json()["error"] == "proposal_assignment_mismatch"


def test_proposal_assignment_mismatch_conflicts():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    repo = _repo(c)
    p = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]
    p["target_assignment_id"] = "assign_does_not_exist"
    repo.set(C.PLAN_CHANGE_PROPOSALS, pid, p)
    r = _decline(c, ELENA, "v-9", pid)
    assert r.status_code == 409 and r.json()["error"] == "proposal_assignment_mismatch"


def test_missing_proposed_version_conflicts():
    c = _c()
    created = _propose(c)
    pid = created["proposal"]["proposal_id"]
    _repo(c)._data[C.ACTIVITY_VERSIONS].pop(created["proposed_activity_version"]["id"])
    r = _decline(c, ELENA, "v-10", pid)
    assert r.status_code == 409 and r.json()["error"] == "invalid_parent_decline_transition"


def test_duplicate_display_order_in_day_fails_atomically():
    """Ambiguous ordering on the day blocks decline; a second activity alone does not."""
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    repo = _repo(c)
    original = repo.query(C.PLAN_ASSIGNMENTS, id=MAYA_BUBBLES)[0]
    clone = dict(original)
    clone["id"] = "assign_maya_bubbles_duplicate"
    clone["pending_proposal_id"] = None          # same day, same display_order
    repo.set(C.PLAN_ASSIGNMENTS, clone["id"], clone)

    r = _decline(c, ELENA, "v-11", pid)
    assert r.status_code == 409 and r.json()["error"] == "duplicate_assignment_display_order"
    _assert_no_mutation(c, pid)


def test_second_current_assignment_on_the_day_does_not_block_decline():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    repo = _repo(c)
    original = repo.query(C.PLAN_ASSIGNMENTS, id=MAYA_BUBBLES)[0]
    sibling = dict(original)
    sibling["id"] = "assign_maya_bubbles_sibling"
    sibling["pending_proposal_id"] = None
    sibling["display_order"] = 1
    repo.set(C.PLAN_ASSIGNMENTS, sibling["id"], sibling)

    r = _decline(c, ELENA, "v-11b", pid)
    assert r.status_code == 200, r.text
    after_sibling = repo.query(C.PLAN_ASSIGNMENTS, id="assign_maya_bubbles_sibling")[0]
    assert after_sibling["display_order"] == 1 and after_sibling["version"] == sibling["version"]
    assert r.json()["current_assignment"]["display_order"] == 0


# ── 41-53: idempotency ──────────────────────────────────────────────────────
def test_missing_and_blank_key_400():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    assert _decline(c, ELENA, None, pid).status_code == 400
    r = _decline(c, ELENA, "   ", pid)
    assert r.status_code == 400 and r.json()["error"] == "missing_idempotency_key"


def test_exact_replay_after_decline_succeeds():
    """(43)-(49) replay must NOT re-validate the now-declined state."""
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]

    first = _decline(c, ELENA, "idem-1", pid)
    assert first.status_code == 200 and first.json()["idempotent_replay"] is False

    second = _decline(c, ELENA, "idem-1", pid)
    assert second.status_code == 200, second.text
    assert second.json()["idempotent_replay"] is True             # (43)(44)(45)
    assert second.json()["audit_event_id"] == first.json()["audit_event_id"]   # (46)
    assert second.json()["proposal"]["version"] == 2               # (47) not 3
    assert second.json()["current_assignment"]["version"] == 3     # (48)

    repo = _repo(c)
    assert len(_declined_events(repo)) == 1                         # (49)
    assert repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]["version"] == 2
    assert repo.query(C.PLAN_ASSIGNMENTS, id=MAYA_BUBBLES)[0]["version"] == 3
    assert _replacements(repo) == []


def test_same_key_different_body_conflicts():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    assert _decline(c, ELENA, "idem-2", pid).status_code == 200
    r = _decline(c, ELENA, "idem-2", pid, proposal_version=2, assignment_version=3)
    assert r.status_code == 409 and r.json()["error"] == "idempotency_key_conflict"


def test_same_key_different_proposal_conflicts():
    c = _c()
    first = _propose(c, key="prop-a")["proposal"]["proposal_id"]
    assert _decline(c, ELENA, "idem-3", first).status_code == 200
    # After a decline the original is free again, so a second proposal is possible.
    b = {**body(), "activity": _talking_activity(title="Second attempt")}
    r2 = c.post(MAYA_MODIFY_PATH, headers={**HANNAH, "Idempotency-Key": "prop-b"},
                json={**b, "expected_assignment_version": 3})
    assert r2.status_code == 200, r2.text
    second = r2.json()["proposal"]["proposal_id"]

    r = _decline(c, ELENA, "idem-3", second, proposal_version=1, assignment_version=4)
    assert r.status_code == 409 and r.json()["error"] == "idempotency_key_conflict"


def test_different_keys_produce_different_candidate_audit_ids():
    """(52) the decline action identity binds the idempotency key hash."""
    args = ("dev-elena", D.ACTION, "child_maya", "prop_x", "assign_x")
    a = operation_scoped_id("aud", operation_identity("key-one", *args))
    b = operation_scoped_id("aud", operation_identity("key-two", *args))
    assert a != b
    assert operation_scoped_id("aud", operation_identity("key-one", *args)) == a
    assert "key-one" not in a


def _audit_id_for_key(key):
    """Run one decline in a fresh repo; return (proposal_id, audit_event_id)."""
    from app.services import proposal_service as P

    repo = InMemoryRepository(); load_fixtures(repo)
    created = P.create_modify_proposal(
        repo, DEV_THERAPIST, child_id="child_maya", assignment_id=MAYA_BUBBLES,
        idempotency_key="seed", expected_assignment_version=1,
        activity=_talking_activity())
    out = D.decline_proposal(
        repo, DEV_PARENT, child_id="child_maya",
        proposal_id=created["proposal"]["proposal_id"], idempotency_key=key,
        expected_proposal_version=1, expected_assignment_version=2)
    return created["proposal"]["proposal_id"], out["audit_event_id"]


def test_service_binds_audit_id_to_the_idempotency_key():
    """The SERVICE must use the key-bound identity, not just `ids.py`."""
    proposal_a, audit_a = _audit_id_for_key("client-key-A")
    proposal_b, audit_b = _audit_id_for_key("client-key-B")
    assert proposal_a == proposal_b
    assert audit_a != audit_b
    _, audit_a_again = _audit_id_for_key("client-key-A")
    assert audit_a_again == audit_a


def test_decline_audit_id_differs_from_acceptance_for_the_same_key():
    """The action name participates, so the two decisions cannot collide."""
    args = ("dev-elena", "child_maya", "prop_x", "assign_x")
    accept_id = operation_scoped_id(
        "aud", operation_identity("k", args[0], "accept_plan_change_proposal", *args[1:]))
    decline_id = operation_scoped_id(
        "aud", operation_identity("k", args[0], D.ACTION, *args[1:]))
    assert accept_id != decline_id


def test_raw_key_absent_from_records_and_audit():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    _decline(c, ELENA, "raw-decline-secret-77", pid)
    repo = _repo(c)
    for col in (C.AUDIT_EVENTS, C.IDEMPOTENCY_RECORDS, C.PLAN_ASSIGNMENTS,
                C.PLAN_CHANGE_PROPOSALS):
        assert "raw-decline-secret-77" not in str(repo.query(col))
    aud = _declined_events(repo)[0]
    assert aud["idempotency_key_hash"] and len(aud["idempotency_key_hash"]) == 64


# ── 54-57: concurrency and atomicity ────────────────────────────────────────
DEV_PARENT = AuthenticatedUser(uid="dev-elena", environment="dev", role=UserRole.PARENT)
DEV_THERAPIST = AuthenticatedUser(uid="dev-hannah", environment="dev", role=UserRole.SLP)


def _race(keys):
    from app.services import proposal_service as P

    repo = InMemoryRepository(); load_fixtures(repo)
    created = P.create_modify_proposal(
        repo, DEV_THERAPIST, child_id="child_maya", assignment_id=MAYA_BUBBLES,
        idempotency_key="seed", expected_assignment_version=1,
        activity=_talking_activity())
    pid = created["proposal"]["proposal_id"]

    results = {}
    barrier = threading.Barrier(len(keys))

    def worker(index, key):
        barrier.wait()
        try:
            r = D.decline_proposal(
                repo, DEV_PARENT, child_id="child_maya", proposal_id=pid,
                idempotency_key=key, expected_proposal_version=1,
                expected_assignment_version=2)
            results[index] = {"ok": True, "replay": r["idempotent_replay"],
                              "audit": r["audit_event_id"]}
        except D.ApprovalError as e:
            results[index] = {"ok": False, "code": e.code}

    threads = [threading.Thread(target=worker, args=(i, k)) for i, k in enumerate(keys)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, repo, pid


def _assert_single_decline(repo, pid):
    assert len(_declined_events(repo)) == 1
    assert _replacements(repo) == []                                    # zero replacements
    proposal = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]
    assert proposal["status"] == "declined" and proposal["version"] == 2
    original = repo.query(C.PLAN_ASSIGNMENTS, id=MAYA_BUBBLES)[0]
    assert original["assignment_status"] == "current" and original["version"] == 3
    assert original["pending_proposal_id"] is None
    in_slot = [a for a in repo.query(C.PLAN_ASSIGNMENTS, child_id="child_maya")
               if a["scheduled_day"] == original["scheduled_day"]
               and a["assignment_status"] == "current"]
    assert [a["id"] for a in in_slot] == [MAYA_BUBBLES]                 # (57)


def test_concurrent_same_key_declines_replay():
    results, repo, pid = _race(["same-key", "same-key"])
    assert all(r["ok"] for r in results.values()), results
    assert sorted(r["replay"] for r in results.values()) == [False, True]
    assert len({r["audit"] for r in results.values()}) == 1
    _assert_single_decline(repo, pid)


def test_concurrent_different_keys_conflict():
    results, repo, pid = _race(["key-a", "key-b"])
    ok = [r for r in results.values() if r["ok"]]
    failed = [r for r in results.values() if not r["ok"]]
    assert len(ok) == 1 and ok[0]["replay"] is False
    assert [r["code"] for r in failed] == ["proposal_already_decided"]
    _assert_single_decline(repo, pid)


def test_failed_transaction_restores_every_collection():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    repo = _repo(c)
    snapshot = {col: len(repo.query(col)) for col in
                (C.PLAN_ASSIGNMENTS, C.PLAN_CHANGE_PROPOSALS, C.AUDIT_EVENTS,
                 C.IDEMPOTENCY_RECORDS, C.ACTIVITY_VERSIONS)}

    r = _decline(c, ELENA, "rollback-1", pid, proposal_version=77)
    assert r.status_code == 409

    for col, n in snapshot.items():
        assert len(repo.query(col)) == n, col
    _assert_no_mutation(c, pid)


# ── 58-62: audit ────────────────────────────────────────────────────────────
def _declined_audit(client):
    return _declined_events(_repo(client))[0]


def test_audit_before_state_is_complete():
    c = _c()
    created = _propose(c)
    pid = created["proposal"]["proposal_id"]
    _decline(c, ELENA, "aud-1", pid)
    before = _declined_audit(c)["before_state"]
    assert before["proposal_status"] == "pending_parent_acceptance"
    assert before["proposal_version"] == 1
    assert before["original_assignment_id"] == MAYA_BUBBLES
    assert before["original_assignment_version"] == 2
    assert before["original_assignment_status"] == "current"
    assert before["original_pending_proposal_id"] == pid
    assert before["current_activity_version_id"] == "ver_bubbles_v1"
    assert before["proposed_activity_version_id"] == created["proposed_activity_version"]["id"]
    assert before["current_assignment_count_in_slot"] == 1


def test_audit_after_state_is_complete():
    c = _c()
    created = _propose(c)
    pid = created["proposal"]["proposal_id"]
    _decline(c, ELENA, "aud-2", pid)
    after = _declined_audit(c)["after_state"]
    assert after["proposal_status"] == "declined"
    assert after["proposal_version"] == 2
    assert after["original_assignment_id"] == MAYA_BUBBLES
    assert after["original_assignment_version"] == 3
    assert after["original_assignment_status"] == "current"
    assert after["original_pending_proposal_id"] is None
    assert after["current_activity_version_id"] == "ver_bubbles_v1"
    assert after["proposed_activity_version_id"] == created["proposed_activity_version"]["id"]
    assert after["proposed_activity_version_active"] is False
    assert after["replacement_assignment_id"] is None
    assert after["current_assignment_count_in_slot"] == 1


def test_audit_reconstructs_the_decline():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    _decline(c, ELENA, "aud-3", pid)
    aud = _declined_audit(c)
    before, after = aud["before_state"], aud["after_state"]

    # the parent declined
    assert aud["actor_role"] == "parent" and aud["subject_id"] == pid
    assert after["proposal_status"] == "declined"
    assert after["proposal_version"] == before["proposal_version"] + 1
    # the original stayed current on its original activity, +1 version
    assert before["original_assignment_status"] == after["original_assignment_status"] == "current"
    assert after["current_activity_version_id"] == before["current_activity_version_id"]
    assert after["original_assignment_version"] == before["original_assignment_version"] + 1
    # the proposed version was never activated, and nothing replaced anything
    assert after["proposed_activity_version_active"] is False
    assert after["replacement_assignment_id"] is None
    assert after["current_assignment_count_in_slot"] == 1
    # the pending reference was released
    assert before["original_pending_proposal_id"] == pid
    assert after["original_pending_proposal_id"] is None
    # identifying fields
    assert aud["child_id"] == "child_maya" and aud["assignment_id"] == MAYA_BUBBLES
    assert aud["therapist_id"] == "ther_hannah" and aud["weekly_plan_id"] == "wp_maya"


def test_audit_state_is_json_serializable():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    _decline(c, ELENA, "aud-4", pid)
    aud = _declined_audit(c)
    restored = json.loads(json.dumps({"b": aud["before_state"], "a": aud["after_state"]}))
    assert restored["b"] == aud["before_state"] and restored["a"] == aud["after_state"]


# ── 66: isolation ───────────────────────────────────────────────────────────
def test_no_parent_api_or_genex_core_import():
    import ast
    import pathlib
    path = (pathlib.Path(__file__).resolve().parent.parent
            / "app" / "services" / "decline_service.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert not a.name.startswith("genex_core")
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            assert (node.module or "").split(".")[0] not in ("genex_core", "api")


def test_decline_then_repropose_is_possible():
    """A declined slot is free again — the therapist may propose a new change."""
    c = _c()
    first = _propose(c, key="rp-1")["proposal"]["proposal_id"]
    _decline(c, ELENA, "rp-dec", first)

    b = {**body(), "activity": _talking_activity(title="Another attempt")}
    r = c.post(MAYA_MODIFY_PATH, headers={**HANNAH, "Idempotency-Key": "rp-2"},
               json={**b, "expected_assignment_version": 3})
    assert r.status_code == 200, r.text
    assert r.json()["current_assignment"]["pending_proposal_id"] == r.json()["proposal"]["proposal_id"]
    assert r.json()["current_assignment"]["version"] == 4
