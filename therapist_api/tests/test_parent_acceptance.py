"""Idempotent parent acceptance of a modify proposal (Phase 1B.2B.1).

Every proposal here is created through the REAL therapist modify endpoint, so
each test exercises the true lifecycle (therapist proposes -> parent accepts)
rather than a hand-built fixture state. All identities and data are fictional.
"""

from __future__ import annotations

import json
import threading

from app.auth.interface import AuthenticatedUser
from app.domain.ids import operation_identity
from app.domain.roles import UserRole
from app.fixtures import load_fixtures
from app.repository import collections as C
from app.repository.memory import InMemoryRepository
from app.services import acceptance_service as A
from tests.conftest import ELENA, HANNAH, OMAR, PRIYA, UNCONNECTED, read_slice_client
from tests.test_modify_proposal import act_social, body

# Maya's bubbles assignment is approved + current with no pending proposal —
# the clean starting point for a therapist proposal the parent can then accept.
MAYA_BUBBLES = "assign_maya_bubbles"
MAYA_MODIFY_PATH = (
    f"/api/v1/children/child_maya/weekly-plan/assignments/{MAYA_BUBBLES}/proposals/modify"
)


def _c():
    return read_slice_client()


def _repo(c):
    return c.app.state.repo


def _talking_activity(**over):
    """A modify payload whose milestone matches Maya's bubbles domain."""
    a = {**act_social(), "developmental_domain": "talking_and_communicating",
         "milestone_id": "mile_request_items", "title": "Bubble requesting (adapted)"}
    a.update(over)
    return a


def _propose(client, key="prop-1"):
    """Therapist creates a pending modify proposal on Maya's bubbles slot."""
    b = {**body(), "activity": _talking_activity()}
    r = client.post(MAYA_MODIFY_PATH, headers={**HANNAH, "Idempotency-Key": key}, json=b)
    assert r.status_code == 200, r.text
    return r.json()


def _accept(client, headers, key, proposal_id, child="child_maya",
            proposal_version=1, assignment_version=2):
    h = dict(headers)
    if key is not None:
        h["Idempotency-Key"] = key
    return client.post(
        f"/api/v1/children/{child}/proposals/{proposal_id}/accept",
        headers=h,
        json={"expected_proposal_version": proposal_version,
              "expected_assignment_version": assignment_version},
    )


def _set_connection(client, conn_id, **fields):
    repo = _repo(client)
    conn = repo.query(C.CONNECTIONS, id=conn_id)[0]
    conn.update(fields)
    repo.set(C.CONNECTIONS, conn_id, conn)


# ── 1-23: happy path and replacement ────────────────────────────────────────
def test_parent_accepts_and_replacement_is_created():
    c = _c()
    created = _propose(c)
    pid = created["proposal"]["proposal_id"]
    proposed_version_id = created["proposed_activity_version"]["id"]

    r = _accept(c, ELENA, "acc-1", pid)
    assert r.status_code == 200, r.text
    out = r.json()

    p = out["proposal"]
    assert p["proposal_status"] == "accepted"          # (2)
    assert p["version"] == 2                            # (3) +1 exactly once
    assert p["decided_by_user_id"] == "par_elena"       # (4)
    assert p["decided_by_role"] == "parent" and p["decided_at"]
    assert p["resulting_assignment_id"] == out["replacement_assignment"]["assignment_id"]

    old = out["retired_assignment"]
    assert old["assignment_id"] == MAYA_BUBBLES
    assert old["assignment_status"] == "replaced"      # (5)
    assert old["version"] == 3                          # (6) 1 -> 2 (propose) -> 3
    assert old["pending_proposal_id"] is None          # (7)
    assert old["activity_version_id"] == "ver_bubbles_v1"   # (8) untouched
    assert old["replaced_by_assignment_id"] == out["replacement_assignment"]["assignment_id"]
    assert old["replaced_at"]

    new = out["replacement_assignment"]
    assert new["activity_version_id"] == proposed_version_id      # (10)
    assert new["replaces_assignment_id"] == MAYA_BUBBLES          # (11)
    assert new["source_proposal_id"] == pid                       # (11)
    assert new["scheduled_day"] == old["scheduled_day"]           # (12) same slot
    assert new["weekly_plan_id"] == old["weekly_plan_id"]
    assert new["assignment_status"] == "current"                  # (13)
    assert new["plan_approval_status"] == "approved"              # (14)
    assert new["practice_status"] == "not_tried"                  # (15) reset
    assert new["pending_proposal_id"] is None
    assert new["version"] == 1
    assert out["idempotent_replay"] is False

    # (9) exactly one replacement created
    repo = _repo(c)
    replacements = [a for a in repo.query(C.PLAN_ASSIGNMENTS, child_id="child_maya")
                    if a.get("replaces_assignment_id") == MAYA_BUBBLES]
    assert len(replacements) == 1

    # (16) exactly one current assignment in that slot
    in_slot = [a for a in repo.query(C.PLAN_ASSIGNMENTS, child_id="child_maya")
               if a["scheduled_day"] == old["scheduled_day"]
               and a["assignment_status"] == "current"]
    assert [a["id"] for a in in_slot] == [new["assignment_id"]]

    # (17)(18)(19) source records untouched
    tmpl = repo.query(C.ACTIVITY_TEMPLATES, id="tmpl_bubbles")[0]
    assert tmpl["title"] == "Bubble requesting"
    orig_v = repo.query(C.ACTIVITY_VERSIONS, id="ver_bubbles_v1")[0]
    assert orig_v["title"] == "Bubble requesting" and orig_v["is_derived"] is False
    prop_v = repo.query(C.ACTIVITY_VERSIONS, id=proposed_version_id)[0]
    assert prop_v["immutable"] is True and prop_v["is_derived"] is True

    # (20)(21) counts
    assert out["child_summary"]["pending_proposal_count"] == 1   # Maya's fixture one remains
    assert out["child_summary"]["plan_review_count"] == 0


def test_weekly_plan_shows_only_the_replacement():
    """(22) read-after-write: the retired original is not a current plan item."""
    c = _c()
    created = _propose(c)
    pid = created["proposal"]["proposal_id"]
    new_id = _accept(c, ELENA, "acc-2", pid).json()["replacement_assignment"]["assignment_id"]

    plan = c.get("/api/v1/children/child_maya/weekly-plan", headers=HANNAH).json()
    ids = [a["assignment_id"] for a in plan["assignments"]]
    assert new_id in ids and MAYA_BUBBLES not in ids
    replacement = next(a for a in plan["assignments"] if a["assignment_id"] == new_id)
    assert replacement["activity_version_id"] == created["proposed_activity_version"]["id"]
    assert replacement["plan_approval_status"] == "approved"
    assert replacement["pending_proposal_id"] is None


def test_proposal_read_shows_accepted_and_resulting_assignment():
    """(23) proposal detail reflects the decision immediately."""
    c = _c()
    created = _propose(c)
    pid = created["proposal"]["proposal_id"]
    new_id = _accept(c, ELENA, "acc-3", pid).json()["replacement_assignment"]["assignment_id"]

    view = c.get(f"/api/v1/children/child_maya/proposals/{pid}", headers=HANNAH).json()
    assert view["proposal_status"] == "accepted"
    assert view["resulting_assignment_id"] == new_id
    assert view["decided_by_user_id"] == "par_elena"
    assert view["decided_by_role"] == "parent" and view["decided_at"]


def test_children_counts_after_acceptance():
    """Therapist overview: pending count drops, plan-review unchanged."""
    c = _c()
    before = c.get("/api/v1/children", headers=HANNAH).json()["items"]
    before_maya = next(x for x in before if x["child_id"] == "child_maya")

    created = _propose(c)
    pid = created["proposal"]["proposal_id"]
    mid = c.get("/api/v1/children", headers=HANNAH).json()["items"]
    mid_maya = next(x for x in mid if x["child_id"] == "child_maya")
    assert mid_maya["pending_proposal_count"] == before_maya["pending_proposal_count"] + 1

    _accept(c, ELENA, "acc-4", pid)
    after = c.get("/api/v1/children", headers=HANNAH).json()["items"]
    after_maya = next(x for x in after if x["child_id"] == "child_maya")
    assert after_maya["pending_proposal_count"] == mid_maya["pending_proposal_count"] - 1
    assert after_maya["plan_review_count"] == before_maya["plan_review_count"]

    overview = c.get("/api/v1/children/child_maya", headers=HANNAH).json()
    assert overview["plan_review_count"] == before_maya["plan_review_count"]


def test_activity_template_untouched_after_acceptance():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    _accept(c, ELENA, "acc-5", pid)
    detail = c.get("/api/v1/activity-templates/tmpl_bubbles", headers=HANNAH).json()
    assert detail["title"] == "Bubble requesting" and detail["immutable"] is True
    version_ids = [v["activity_version_id"] for v in detail["versions"]]
    assert "ver_bubbles_v1" in version_ids


# ── 24-31: authorization ────────────────────────────────────────────────────
def test_therapist_principal_forbidden():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    r = _accept(c, HANNAH, "az-1", pid)
    assert r.status_code == 403 and r.json()["error"] == "forbidden"


def test_unconnected_therapist_forbidden():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    assert _accept(c, UNCONNECTED, "az-2", pid).status_code == 403
    assert _accept(c, PRIYA, "az-3", pid).status_code == 403


def test_other_parent_is_existence_blind_404():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    r = _accept(c, OMAR, "az-4", pid)
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_unknown_child_and_proposal_404():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    assert _accept(c, ELENA, "az-5", pid, child="child_zzz").status_code == 404
    assert _accept(c, ELENA, "az-6", "prop_zzz").status_code == 404


def test_proposal_belonging_to_another_child_404():
    """Maya's parent cannot accept via a child id that does not own the proposal."""
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    assert _accept(c, ELENA, "az-7", pid, child="child_eli").status_code == 404


def test_pending_paused_ended_connection_404():
    for idx, status in enumerate(
        ("pending_parent_acceptance", "paused_by_parent", "ended")
    ):
        c = _c()
        pid = _propose(c)["proposal"]["proposal_id"]
        _set_connection(c, "conn_maya", status=status)
        r = _accept(c, ELENA, f"az-conn-{idx}", pid)
        assert r.status_code == 404, f"{status}: {r.status_code}"


def test_unauthenticated_request_401():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    r = c.post(f"/api/v1/children/child_maya/proposals/{pid}/accept",
               headers={"Idempotency-Key": "az-8"},
               json={"expected_proposal_version": 1, "expected_assignment_version": 2})
    assert r.status_code == 401


# ── 32-41: versions and state ───────────────────────────────────────────────
def _assert_no_mutation(client, proposal_id, expect_proposal_version=1):
    repo = _repo(client)
    p = repo.query(C.PLAN_CHANGE_PROPOSALS, id=proposal_id)[0]
    assert p["status"] == "pending_parent_acceptance"
    assert p["version"] == expect_proposal_version
    a = repo.query(C.PLAN_ASSIGNMENTS, id=MAYA_BUBBLES)[0]
    assert a["assignment_status"] == "current" and a["version"] == 2
    assert not [x for x in repo.query(C.PLAN_ASSIGNMENTS, child_id="child_maya")
                if x.get("replaces_assignment_id")]
    assert not [e for e in repo.query(C.AUDIT_EVENTS)
                if e["event_type"] == "plan_change_proposal_accepted"]


def test_stale_proposal_version_conflict_no_mutation():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    r = _accept(c, ELENA, "v-1", pid, proposal_version=9)
    assert r.status_code == 409 and r.json()["error"] == "proposal_version_conflict"
    _assert_no_mutation(c, pid)


def test_stale_assignment_version_conflict_no_mutation():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    r = _accept(c, ELENA, "v-2", pid, assignment_version=99)
    assert r.status_code == 409 and r.json()["error"] == "assignment_version_conflict"
    _assert_no_mutation(c, pid)


def test_already_accepted_with_new_key_conflicts():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    assert _accept(c, ELENA, "v-3", pid).status_code == 200
    r = _accept(c, ELENA, "v-4-different-key", pid, proposal_version=2, assignment_version=3)
    assert r.status_code == 409 and r.json()["error"] == "proposal_already_decided"


def test_non_modify_proposal_cannot_be_accepted():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    repo = _repo(c)
    p = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]
    p["proposal_type"] = "add"
    repo.set(C.PLAN_CHANGE_PROPOSALS, pid, p)
    r = _accept(c, ELENA, "v-5", pid)
    assert r.status_code == 409 and r.json()["error"] == "invalid_parent_accept_transition"


def test_assignment_no_longer_current_conflicts():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    repo = _repo(c)
    a = repo.query(C.PLAN_ASSIGNMENTS, id=MAYA_BUBBLES)[0]
    a["assignment_status"] = "retired"
    repo.set(C.PLAN_ASSIGNMENTS, MAYA_BUBBLES, a)
    r = _accept(c, ELENA, "v-6", pid)
    assert r.status_code == 409 and r.json()["error"] == "invalid_parent_accept_transition"


def test_pending_proposal_id_mismatch_conflicts():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    repo = _repo(c)
    a = repo.query(C.PLAN_ASSIGNMENTS, id=MAYA_BUBBLES)[0]
    a["pending_proposal_id"] = "prop_someone_else"
    repo.set(C.PLAN_ASSIGNMENTS, MAYA_BUBBLES, a)
    r = _accept(c, ELENA, "v-7", pid)
    assert r.status_code == 409 and r.json()["error"] == "proposal_assignment_mismatch"


def test_proposal_assignment_mismatch_conflicts():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    repo = _repo(c)
    p = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]
    p["target_assignment_id"] = "assign_does_not_exist"
    repo.set(C.PLAN_CHANGE_PROPOSALS, pid, p)
    r = _accept(c, ELENA, "v-8", pid)
    assert r.status_code == 409 and r.json()["error"] == "proposal_assignment_mismatch"


def test_missing_proposed_version_conflicts():
    c = _c()
    created = _propose(c)
    pid = created["proposal"]["proposal_id"]
    repo = _repo(c)
    repo._data[C.ACTIVITY_VERSIONS].pop(created["proposed_activity_version"]["id"])
    r = _accept(c, ELENA, "v-9", pid)
    assert r.status_code == 409 and r.json()["error"] == "invalid_parent_accept_transition"


def test_duplicate_display_order_in_day_fails_atomically():
    """A day whose current assignments share a display_order blocks acceptance.

    Since Phase 1B.2C.1 a weekday may hold SEVERAL current activities, so a second
    assignment is no longer itself a conflict — an ambiguous ORDER is. The clone
    copies display_order=0, which is the inconsistency the guard must catch.
    """
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    repo = _repo(c)
    original = repo.query(C.PLAN_ASSIGNMENTS, id=MAYA_BUBBLES)[0]
    clone = dict(original)
    clone["id"] = "assign_maya_bubbles_duplicate"
    clone["pending_proposal_id"] = None          # same day, same display_order
    repo.set(C.PLAN_ASSIGNMENTS, clone["id"], clone)

    r = _accept(c, ELENA, "v-10", pid)
    assert r.status_code == 409 and r.json()["error"] == "duplicate_assignment_display_order"
    _assert_no_mutation(c, pid)


def test_second_current_assignment_on_the_day_does_not_block_acceptance():
    """The replaced behavior: a distinct-order same-day activity is now fine."""
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    repo = _repo(c)
    original = repo.query(C.PLAN_ASSIGNMENTS, id=MAYA_BUBBLES)[0]
    sibling = dict(original)
    sibling["id"] = "assign_maya_bubbles_sibling"
    sibling["pending_proposal_id"] = None
    sibling["display_order"] = 1                  # distinct position on the day
    repo.set(C.PLAN_ASSIGNMENTS, sibling["id"], sibling)

    r = _accept(c, ELENA, "v-10b", pid)
    assert r.status_code == 200, r.text
    # the sibling is untouched and the replacement took the original's position
    after_sibling = repo.query(C.PLAN_ASSIGNMENTS, id="assign_maya_bubbles_sibling")[0]
    assert after_sibling["display_order"] == 1
    assert after_sibling["assignment_status"] == "current"
    assert after_sibling["version"] == sibling["version"]
    assert r.json()["replacement_assignment"]["display_order"] == 0


# ── 42-54: idempotency ──────────────────────────────────────────────────────
def test_missing_and_blank_key_400():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    assert _accept(c, ELENA, None, pid).status_code == 400
    r = _accept(c, ELENA, "   ", pid)
    assert r.status_code == 400 and r.json()["error"] == "missing_idempotency_key"


def test_exact_replay_after_acceptance_succeeds():
    """Replay must NOT re-validate the now-decided state (44-50)."""
    c = _c()
    created = _propose(c)
    pid = created["proposal"]["proposal_id"]

    first = _accept(c, ELENA, "idem-1", pid)
    assert first.status_code == 200 and first.json()["idempotent_replay"] is False

    second = _accept(c, ELENA, "idem-1", pid)
    assert second.status_code == 200, second.text
    assert second.json()["idempotent_replay"] is True            # (44)(45)(46)
    assert (second.json()["replacement_assignment"]["assignment_id"]
            == first.json()["replacement_assignment"]["assignment_id"])   # (47)
    assert second.json()["proposal"]["version"] == 2              # (49) not 3
    assert second.json()["retired_assignment"]["version"] == 3    # (49)

    repo = _repo(c)
    replacements = [a for a in repo.query(C.PLAN_ASSIGNMENTS, child_id="child_maya")
                    if a.get("replaces_assignment_id")]
    assert len(replacements) == 1                                  # (48)
    accepted_events = [e for e in repo.query(C.AUDIT_EVENTS)
                       if e["event_type"] == "plan_change_proposal_accepted"]
    assert len(accepted_events) == 1                               # (50)
    assert repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]["version"] == 2


def test_same_key_different_body_conflicts():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    assert _accept(c, ELENA, "idem-2", pid).status_code == 200
    r = _accept(c, ELENA, "idem-2", pid, proposal_version=2, assignment_version=3)
    assert r.status_code == 409 and r.json()["error"] == "idempotency_key_conflict"


def test_same_key_different_proposal_conflicts():
    c = _c()
    first = _propose(c, key="prop-a")["proposal"]["proposal_id"]
    assert _accept(c, ELENA, "idem-3", first).status_code == 200
    # A second proposal on the freshly created replacement slot.
    repo = _repo(c)
    replacement = [a for a in repo.query(C.PLAN_ASSIGNMENTS, child_id="child_maya")
                   if a.get("replaces_assignment_id") == MAYA_BUBBLES][0]
    path = (f"/api/v1/children/child_maya/weekly-plan/assignments/"
            f"{replacement['id']}/proposals/modify")
    b = {**body(), "activity": _talking_activity(title="Second change")}
    r2 = c.post(path, headers={**HANNAH, "Idempotency-Key": "prop-b"}, json=b)
    assert r2.status_code == 200, r2.text
    second = r2.json()["proposal"]["proposal_id"]

    r = _accept(c, ELENA, "idem-3", second, proposal_version=1, assignment_version=2)
    assert r.status_code == 409 and r.json()["error"] == "idempotency_key_conflict"


def test_different_keys_produce_different_candidate_ids():
    """(53) the operation identity binds the idempotency key hash."""
    args = ("dev-elena", A.ACTION, "child_maya", "prop_x", "assign_x")
    a = operation_identity("key-one", *args)
    b = operation_identity("key-two", *args)
    assert a != b
    assert operation_identity("key-one", *args) == a          # stable for a retry
    # and the raw key never appears in the identity
    assert "key-one" not in a


def _replacement_id_for_key(key):
    """Run one acceptance in a fresh repo; return the replacement assignment id.

    Everything except the idempotency key is identical between runs — same
    actor, action, child, proposal and original assignment — so any difference
    in the resulting id can only come from the key participating in it.
    """
    from app.services import proposal_service as P

    repo = InMemoryRepository(); load_fixtures(repo)
    created = P.create_modify_proposal(
        repo, DEV_THERAPIST, child_id="child_maya", assignment_id=MAYA_BUBBLES,
        idempotency_key="seed", expected_assignment_version=1,
        activity=_talking_activity())
    out = A.accept_proposal(
        repo, DEV_PARENT, child_id="child_maya",
        proposal_id=created["proposal"]["proposal_id"], idempotency_key=key,
        expected_proposal_version=1, expected_assignment_version=2)
    return created["proposal"]["proposal_id"], out["replacement_assignment"]["assignment_id"]


def test_service_binds_replacement_id_to_the_idempotency_key():
    """The SERVICE must use the key-bound identity, not just `ids.py`.

    Guards the deterministic-ID correction end to end: once a future lifecycle
    step clears `pending_proposal_id` again, two different client operations
    must not collide on one replacement document.
    """
    proposal_a, id_a = _replacement_id_for_key("client-key-A")
    proposal_b, id_b = _replacement_id_for_key("client-key-B")

    assert proposal_a == proposal_b, "proposal id should be identical across runs"
    assert id_a != id_b, "replacement id must differ when only the key differs"

    # ...and the same key reproduces the same id.
    _, id_a_again = _replacement_id_for_key("client-key-A")
    assert id_a_again == id_a


def test_raw_key_absent_from_records_and_audit():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    _accept(c, ELENA, "raw-parent-secret-99", pid)
    repo = _repo(c)
    for col in (C.AUDIT_EVENTS, C.IDEMPOTENCY_RECORDS, C.PLAN_ASSIGNMENTS,
                C.PLAN_CHANGE_PROPOSALS):
        assert "raw-parent-secret-99" not in str(repo.query(col))
    aud = [e for e in repo.query(C.AUDIT_EVENTS)
           if e["event_type"] == "plan_change_proposal_accepted"][0]
    assert aud["idempotency_key_hash"] and len(aud["idempotency_key_hash"]) == 64


# ── 55-58: concurrency and atomicity ────────────────────────────────────────
DEV_PARENT = AuthenticatedUser(uid="dev-elena", environment="dev", role=UserRole.PARENT)
DEV_THERAPIST = AuthenticatedUser(uid="dev-hannah", environment="dev", role=UserRole.SLP)


def _race(keys):
    """Seed a proposal, then race N accepts released from a barrier."""
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
            r = A.accept_proposal(
                repo, DEV_PARENT, child_id="child_maya", proposal_id=pid,
                idempotency_key=key, expected_proposal_version=1,
                expected_assignment_version=2)
            results[index] = {"ok": True, "replay": r["idempotent_replay"],
                              "replacement": r["replacement_assignment"]["assignment_id"]}
        except A.ApprovalError as e:
            results[index] = {"ok": False, "code": e.code}

    threads = [threading.Thread(target=worker, args=(i, k)) for i, k in enumerate(keys)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, repo, pid


def _assert_single_acceptance(repo, pid):
    replacements = [a for a in repo.query(C.PLAN_ASSIGNMENTS, child_id="child_maya")
                    if a.get("replaces_assignment_id") == MAYA_BUBBLES]
    assert len(replacements) == 1
    events = [e for e in repo.query(C.AUDIT_EVENTS)
              if e["event_type"] == "plan_change_proposal_accepted"]
    assert len(events) == 1
    proposal = repo.query(C.PLAN_CHANGE_PROPOSALS, id=pid)[0]
    assert proposal["status"] == "accepted" and proposal["version"] == 2
    original = repo.query(C.PLAN_ASSIGNMENTS, id=MAYA_BUBBLES)[0]
    assert original["assignment_status"] == "replaced" and original["version"] == 3
    in_slot = [a for a in repo.query(C.PLAN_ASSIGNMENTS, child_id="child_maya")
               if a["scheduled_day"] == original["scheduled_day"]
               and a["assignment_status"] == "current"]
    assert [a["id"] for a in in_slot] == [replacements[0]["id"]]   # (58)
    return replacements[0]["id"]


def test_concurrent_same_key_accepts_replay():
    results, repo, pid = _race(["same-key", "same-key"])
    assert all(r["ok"] for r in results.values()), results
    assert sorted(r["replay"] for r in results.values()) == [False, True]
    assert len({r["replacement"] for r in results.values()}) == 1
    _assert_single_acceptance(repo, pid)


def test_concurrent_different_keys_conflict():
    results, repo, pid = _race(["key-a", "key-b"])
    ok = [r for r in results.values() if r["ok"]]
    failed = [r for r in results.values() if not r["ok"]]
    assert len(ok) == 1 and ok[0]["replay"] is False
    assert [r["code"] for r in failed] == ["proposal_already_decided"]
    replacement_id = _assert_single_acceptance(repo, pid)
    assert ok[0]["replacement"] == replacement_id


def test_failed_transaction_restores_every_collection():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    repo = _repo(c)
    snapshot = {col: len(repo.query(col)) for col in
                (C.PLAN_ASSIGNMENTS, C.PLAN_CHANGE_PROPOSALS, C.AUDIT_EVENTS,
                 C.IDEMPOTENCY_RECORDS, C.ACTIVITY_VERSIONS)}

    r = _accept(c, ELENA, "rollback-1", pid, assignment_version=42)
    assert r.status_code == 409

    for col, n in snapshot.items():
        assert len(repo.query(col)) == n, col
    _assert_no_mutation(c, pid)


# ── 59-63: audit ────────────────────────────────────────────────────────────
def _accepted_audit(client):
    return [e for e in _repo(client).query(C.AUDIT_EVENTS)
            if e["event_type"] == "plan_change_proposal_accepted"][0]


def test_audit_before_state_is_complete():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    _accept(c, ELENA, "aud-1", pid)
    before = _accepted_audit(c)["before_state"]
    assert before["proposal_status"] == "pending_parent_acceptance"
    assert before["proposal_version"] == 1
    assert before["original_assignment_id"] == MAYA_BUBBLES
    assert before["original_assignment_version"] == 2
    assert before["original_assignment_status"] == "current"
    assert before["original_pending_proposal_id"] == pid
    assert before["current_activity_version_id"] == "ver_bubbles_v1"
    assert before["replacement_assignment_id"] is None


def test_audit_after_state_is_complete():
    c = _c()
    created = _propose(c)
    pid = created["proposal"]["proposal_id"]
    out = _accept(c, ELENA, "aud-2", pid).json()
    after = _accepted_audit(c)["after_state"]
    assert after["proposal_status"] == "accepted"
    assert after["proposal_version"] == 2
    assert after["original_assignment_status"] == "replaced"
    assert after["original_assignment_version"] == 3
    assert after["original_pending_proposal_id"] is None
    assert after["replacement_assignment_id"] == out["replacement_assignment"]["assignment_id"]
    assert after["replacement_assignment_status"] == "current"
    assert after["replacement_activity_version_id"] == created["proposed_activity_version"]["id"]
    assert after["replacement_plan_approval_status"] == "approved"
    assert after["current_assignment_count_in_slot"] == 1


def test_audit_reconstructs_the_replacement():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    _accept(c, ELENA, "aud-3", pid)
    aud = _accepted_audit(c)
    before, after = aud["before_state"], aud["after_state"]

    # the proposal moved from pending to accepted, exactly one version step
    assert after["proposal_version"] == before["proposal_version"] + 1
    # the original was retired, exactly one version step, and unlinked
    assert after["original_assignment_version"] == before["original_assignment_version"] + 1
    assert before["original_assignment_status"] == "current"
    assert after["original_assignment_status"] == "replaced"
    assert before["original_pending_proposal_id"] == aud["subject_id"]
    assert after["original_pending_proposal_id"] is None
    # a replacement now carries the proposed version, and it is the only current one
    assert before["replacement_assignment_id"] is None
    assert after["replacement_assignment_id"]
    assert (after["replacement_activity_version_id"]
            != after["current_activity_version_id"])   # genuinely a different activity
    assert after["current_assignment_count_in_slot"] == 1
    # identifying fields
    assert aud["child_id"] == "child_maya" and aud["assignment_id"] == MAYA_BUBBLES
    assert aud["actor_role"] == "parent" and aud["therapist_id"] == "ther_hannah"
    assert aud["weekly_plan_id"] == "wp_maya"


def test_audit_state_is_json_serializable():
    c = _c()
    pid = _propose(c)["proposal"]["proposal_id"]
    _accept(c, ELENA, "aud-4", pid)
    aud = _accepted_audit(c)
    restored = json.loads(json.dumps({"b": aud["before_state"], "a": aud["after_state"]}))
    assert restored["b"] == aud["before_state"] and restored["a"] == aud["after_state"]


# ── 67: isolation ───────────────────────────────────────────────────────────
def test_no_parent_api_or_genex_core_import():
    import ast
    import pathlib
    path = (pathlib.Path(__file__).resolve().parent.parent
            / "app" / "services" / "acceptance_service.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert not a.name.startswith("genex_core")
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            assert (node.module or "").split(".")[0] not in ("genex_core", "api")
