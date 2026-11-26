"""Week 1 partial-release resumability.

Before this fix, a Week 1 release interrupted after its allocation committed
(snapshot not saved), or after its snapshot committed (release not marked),
was STUCK: the retry re-ran `allocate_cycle`, which refused "already
allocated", and the route answered 403 forever while the family saw nothing.

Week 1 now resumes through the SAME two existing steps Week 2+ uses (P0-B1):
`WeeklyService.allocation_state` (None -> allocate; an allocation EXACTLY what
this request would write -> reuse; anything else -> refuse, nothing written)
and `_resume_snapshot_and_release` (reuse a snapshot only if byte-for-byte the
document this request builds; else capture; release if unreleased; identical
racers tolerated). The result is the SAME Week 1 an uninterrupted release
produces — or a refusal.

## The snapshot is Week 1's clinical freeze / commitment boundary

Founder decision (goal revised between an interruption and its retry):

* BEFORE a snapshot exists (state 3: cycle + allocation, no snapshot), no
  family-facing commitment exists yet. The retry recomputes the normal Week 1
  from the LATEST active clinician-approved goal version, and resumes ONLY if
  the persisted allocation still EXACTLY matches what it now expects; the
  snapshot is then frozen with the latest approved wording/version. A
  revision that changes the expected allocation (anchor-derived candidates,
  placements, coverage, selection) is refused and nothing is written.
* ONCE a snapshot exists (state 4), it IS the frozen clinical artifact: any
  revision that makes the rebuilt snapshot differ is refused by the existing
  byte-equivalence check.
"""

from __future__ import annotations

import json

import pytest

from pilot_backend.domain.source_link import SourceSystem
from pilot_backend.domain.weekly_plan_document import parent_activity_ref
from pilot_backend.integration.week_one_release import (
    AdaptiveWeekStateMismatch,
    GoalNotReleasable,
)
from pilot_backend.weekly.errors import InProgressCapacityMismatch, InProgressStateMismatch

from .test_first_plan_timezone_gate import CHILD, LA, Fresh
from .test_p0b1_adaptive_week_two import Injected, _concurrent_first, _fail_once

CAPACITY = 3
DAY = "2026-11-17T15:00:00"


def _world(monkeypatch):
    f = Fresh(monkeypatch, at=DAY)
    _s, body = f.create_plan("2026-11", LA)
    f.allocate_and_activate(body["plan"]["focus_plan_id"])
    return f


def _docs(f, collection, **match):
    return [d for _i, d in f.repos.store.list_all(collection)
            if all(d.get(k) == v for k, v in match.items())]


def _counts(f):
    cycles = _docs(f, "pilot_weekly_cycles", child_id=CHILD)
    ids = [c["cycle_id"] for c in cycles]
    return {
        "cycles": len(cycles),
        "ledgers": sum(len(_docs(f, "pilot_capacity_ledgers", cycle_id=i)) for i in ids),
        "alignments": sum(len(_docs(f, "pilot_activity_goal_alignments", cycle_id=i)) for i in ids),
        "snapshots": sum(len(_docs(f, "pilot_weekly_plan_snapshots", cycle_id=i)) for i in ids),
        "releases": sum(1 for e in _docs(f, "pilot_audit_events", action="weekly_cycle_released")
                        if e.get("resource_id") in ids),
        "allocation_claims": len([c for c in _docs(f, "pilot_identity_claims")
                                  if c.get("kind") == "weekly_allocation" and c.get("holder_ref") in ids]),
        "snapshot_claims": len([c for c in _docs(f, "pilot_identity_claims")
                                if c.get("kind") == "weekly_snapshot" and c.get("holder_ref") in ids]),
    }


ONE_OF_EVERYTHING = {"cycles": 1, "ledgers": 1, "alignments": CAPACITY, "snapshots": 1,
                     "releases": 1, "allocation_claims": 1, "snapshot_claims": 1}


def _fingerprint(f):
    """Everything about Week 1 that must not depend on interruption. Random
    ids (cycle, goal, version) are normalised; template ids are content
    digests and therefore comparable across worlds."""
    (cycle,) = f.repos.weekly_cycles.list_for_child(CHILD)
    (snap,) = f.repos.weekly_plan_snapshots.list_for_cycle(cycle.cycle_id)
    (ledger,) = f.repos.capacity_ledgers.list_for_cycle(cycle.cycle_id)
    doc = json.loads(snap.resolved_plan_document)
    # The family handle is a digest of the instance ref (which embeds the random
    # cycle id): prove it is EXACTLY that derivation, then normalise it.
    for row in doc["activities"]:
        assert row["activity_ref"] == parent_activity_ref(row["activity_instance_ref"])
        row["activity_ref"] = "<pact>"
    normal = json.dumps(doc, sort_keys=True).replace(cycle.cycle_id, "<cycle>").replace(
        cycle.owning_focus_plan_id, "<plan>").replace(
        f.goal.clinical_goal_id, "<goal>").replace(f.goal.current_version_id, "<version>")
    alignments = sorted((a.activity_identity_ref, a.role.value, a.alignment_source.value)
                        for a in f.repos.alignments.list_for_cycle(cycle.cycle_id))
    return {
        "cycle": (cycle.sequence_in_month, cycle.starts_on, cycle.ends_on, cycle.is_partial,
                  cycle.predecessor_cycle_id, cycle.is_released),
        "snapshot": (snap.source_system.value, snap.source_plan_id == cycle.cycle_id, normal),
        "ledger": (ledger.family_declared_capacity, ledger.allocated_by_planner),
        "alignments": alignments,
    }


@pytest.fixture
def clean(monkeypatch):
    """The uninterrupted Week 1, as the comparison point."""
    f = _world(monkeypatch)
    status, body = f.release()
    assert status == 200 and body["created"] is True
    return _fingerprint(f), body


INJECTIONS = {
    # 1. cycle creation failed (before = nothing written; after = cycle only)
    "create_cycle:before": ("create_cycle", True),
    "create_cycle:after": ("create_cycle", False),
    # 2. allocation failed and rolled back
    "allocate_cycle:before": ("allocate_cycle", True),
    # 3. cycle + allocation persisted, snapshot NOT saved
    "allocate_cycle:after": ("allocate_cycle", False),
    "capture_snapshot:before": ("capture_snapshot", True),
    # 4. cycle + allocation + snapshot persisted, release NOT marked
    "capture_snapshot:after": ("capture_snapshot", False),
    "release_cycle:before": ("release_cycle", True),
    # (release committed, response lost -> retry is a replay)
    "release_cycle:after": ("release_cycle", False),
}


def _interrupt(f, point):
    name, before = INJECTIONS[point]
    _fail_once(f.weekly, name, before=before)
    with pytest.raises(Injected):
        f.service.release_week_one(f.provider, CHILD, family_declared_capacity=CAPACITY)


def _surface(f):
    return {c: sorted(json.dumps(d, sort_keys=True) for _i, d in f.repos.store.list_all(c))
            for c in ("pilot_weekly_cycles", "pilot_activity_goal_alignments", "pilot_capacity_ledgers",
                      "pilot_weekly_plan_snapshots", "pilot_identity_claims", "pilot_monthly_focus_plans",
                      "pilot_monthly_goal_allocations", "pilot_goal_versions", "pilot_clinical_goals")}


# ===========================================================================
# failure injection: every step, one failure, one retry
# ===========================================================================


@pytest.mark.parametrize("point", sorted(INJECTIONS))
def test_an_interrupted_week_one_resumes_to_the_clean_result(point, clean, monkeypatch):
    expected, clean_body = clean
    f = _world(monkeypatch)
    _interrupt(f, point)

    status, body = f.release()                      # the retry, over the route
    assert status == 200, body
    assert _counts(f) == ONE_OF_EVERYTHING
    (cycle,) = f.repos.weekly_cycles.list_for_child(CHILD)
    assert cycle.is_released and body["cycle_id"] == cycle.cycle_id
    assert f.this_week_cycle() == cycle.cycle_id    # the family sees it
    assert f.current_cycle() == cycle.cycle_id      # the provider sees the same
    assert _fingerprint(f) == expected, "an interrupted Week 1 differs from the clean one"
    assert body["activity_count"] == clean_body["activity_count"]
    # A committed release whose response was lost is a REPLAY, not a second release.
    assert body["created"] is (point != "release_cycle:after")


def test_the_two_formerly_stuck_states_are_what_the_injections_produce(monkeypatch):
    """Guards the injection table itself: state 3 and state 4 really are the
    partial states that used to 403 forever."""
    three = _world(monkeypatch)
    _interrupt(three, "allocate_cycle:after")
    c = _counts(three)
    assert (c["cycles"], c["alignments"], c["snapshots"], c["releases"]) == (1, CAPACITY, 0, 0)
    four = _world(monkeypatch)
    _interrupt(four, "capture_snapshot:after")
    c = _counts(four)
    assert (c["cycles"], c["alignments"], c["snapshots"], c["releases"]) == (1, CAPACITY, 1, 0)


# ===========================================================================
# fail closed: a mismatch is refused, never repaired
# ===========================================================================


@pytest.mark.parametrize("point", ["allocate_cycle:after", "capture_snapshot:after"])
def test_a_different_capacity_on_retry_is_refused(point, monkeypatch):
    f = _world(monkeypatch)
    _interrupt(f, point)
    before = _surface(f)
    with pytest.raises(InProgressCapacityMismatch):
        f.service.release_week_one(f.provider, CHILD, family_declared_capacity=CAPACITY - 1)
    assert f.http("POST", f"/pilot/children/{CHILD}/weekly-cycles/current/release",
                  body={"family_declared_capacity": 1}) == (403, {"error": "not permitted"})
    assert _surface(f) == before
    assert f.this_week_cycle() is None


def _revise_goal(f, text="{child} will name 12 objects"):
    status, body = f.http("POST", f"/pilot/goals/clinical/{f.goal.clinical_goal_id}/revisions",
                          body={"edit_type": "modified", "text": text,
                                "reason": "clinician revision after the interruption"})
    assert status == 200, body
    return text


def test_state_3_a_wording_revision_that_keeps_the_allocation_resumes_with_the_latest_version(monkeypatch):
    """Founder decision, state 3 (no snapshot yet): the latest approved goal
    version governs the resumed release because the persisted allocation still
    EXACTLY matches what the retry expects."""
    f = _world(monkeypatch)
    old_version = f.goal.current_version_id
    _interrupt(f, "allocate_cycle:after")
    (cycle,) = f.repos.weekly_cycles.list_for_child(CHILD)
    allocation_before = [d for k, d in _surface(f).items()
                         if k in ("pilot_activity_goal_alignments", "pilot_capacity_ledgers")]
    new_text = _revise_goal(f)

    status, body = f.release()
    assert status == 200 and body["created"] is True and body["cycle_id"] == cycle.cycle_id
    assert _counts(f) == ONE_OF_EVERYTHING
    assert [d for k, d in _surface(f).items()
            if k in ("pilot_activity_goal_alignments", "pilot_capacity_ledgers")] == allocation_before, \
        "the persisted allocation was reused, never rewritten"
    (snap,) = f.repos.weekly_plan_snapshots.list_for_cycle(cycle.cycle_id)
    (frozen_goal,) = json.loads(snap.resolved_plan_document)["goals"]
    current = f.goals.get_goal(f.provider, f.goal.ref)
    assert current.current_version_id != old_version
    assert frozen_goal["goal_version_id"] == current.current_version_id, "latest approved version frozen"
    assert frozen_goal["text"] == new_text
    assert f.this_week_cycle() == cycle.cycle_id, "the family sees the released week"
    _s, week = f.http("GET", f"/pilot/children/{CHILD}/this-week", token="cg")
    assert [g["text"] for g in week["week"]["goals"]] == [new_text]


def test_state_3_a_change_that_alters_the_expected_allocation_is_refused(monkeypatch):
    """Founder decision, state 3: a clinical change after the interruption that
    alters the deterministic Week 1 (here the goal is re-anchored so a family
    the persisted allocation USED drops out) is refused; nothing is written."""
    f = _world(monkeypatch)
    _interrupt(f, "allocate_cycle:after")
    (cycle,) = f.repos.weekly_cycles.list_for_child(CHILD)
    templates = f.service._candidate_templates(f.service.releasable_goals(f.provider, CHILD))
    used = {templates[a.activity_identity_ref].activity_family_ref
            for a in f.repos.alignments.list_for_cycle(cycle.cycle_id)}
    ((anchor_id, anchor),) = list(f.repos.store.list_all("pilot_clinical_goal_anchors"))
    families = [b["family_ref"] for b in anchor["rung"]["family_bindings"]]
    dropped = sorted(used)[0]
    assert dropped in families and len(families) > 1
    real_anchor = f.goals.goal_anchor

    def re_anchored(principal, ref):
        a = real_anchor(principal, ref)
        from dataclasses import replace
        rung = replace(a.rung, family_bindings=tuple(b for b in a.rung.family_bindings
                                                     if b.family_ref != dropped))
        return replace(a, rung=rung)

    f.goals.goal_anchor = re_anchored                # the retry sees the new anchor
    before = _surface(f)
    with pytest.raises((InProgressStateMismatch, AdaptiveWeekStateMismatch)):
        f.service.release_week_one(f.provider, CHILD, family_declared_capacity=CAPACITY)
    assert _surface(f) == before, "the interrupted state is byte-identical"
    assert f.this_week_cycle() is None


def test_state_4_a_goal_revised_after_the_snapshot_was_saved_is_refused(monkeypatch):
    """State 4: the saved snapshot is the frozen clinical artifact; it froze the
    OLD goal version, the rebuilt document carries the new one, so the exact
    snapshot check refuses and the snapshot is left untouched."""
    f = _world(monkeypatch)
    _interrupt(f, "capture_snapshot:after")
    (cycle,) = f.repos.weekly_cycles.list_for_child(CHILD)
    (snapshot_before,) = f.repos.weekly_plan_snapshots.list_for_cycle(cycle.cycle_id)
    _revise_goal(f)
    before = _surface(f)
    with pytest.raises(AdaptiveWeekStateMismatch):
        f.service.release_week_one(f.provider, CHILD, family_declared_capacity=CAPACITY)
    assert f.release() == (403, {"error": "not permitted"})
    assert _surface(f) == before
    (snapshot_after,) = f.repos.weekly_plan_snapshots.list_for_cycle(cycle.cycle_id)
    assert snapshot_after == snapshot_before, "the frozen snapshot is unchanged"
    assert f.this_week_cycle() is None


def test_a_changed_expected_allocation_is_refused(monkeypatch):
    """State 3 with a persisted alignment that is not what this request would
    write (a tampered/foreign row): refused, the row left as it is."""
    f = _world(monkeypatch)
    _interrupt(f, "allocate_cycle:after")
    (cycle,) = f.repos.weekly_cycles.list_for_child(CHILD)
    (doc_id, row), *_ = [(i, d) for i, d in f.repos.store.list_all("pilot_activity_goal_alignments")
                         if d["cycle_id"] == cycle.cycle_id]
    # A VALID but different role: the codec accepts it, so only the resume
    # comparison can catch it.
    flipped = "supporting" if row["role"] == "primary" else "primary"
    f.repos.store.set("pilot_activity_goal_alignments", doc_id, {**row, "role": flipped})
    before = _surface(f)
    with pytest.raises(InProgressStateMismatch):
        f.service.release_week_one(f.provider, CHILD, family_declared_capacity=CAPACITY)
    assert _surface(f) == before


@pytest.mark.parametrize("point", ["allocate_cycle:after", "capture_snapshot:after"])
def test_activity_bank_drift_that_changes_the_week_is_refused(point, monkeypatch):
    """The persisted allocation was computed from a bank that no longer yields
    the same candidates: the recomputation differs, so it refuses."""
    f = _world(monkeypatch)
    _interrupt(f, point)
    real = f.service._candidate_templates

    def drifted(goals):
        templates = dict(real(goals))
        templates.pop(sorted(templates)[0])         # one reviewed card withdrawn
        return templates

    f.service._candidate_templates = drifted
    before = _surface(f)
    with pytest.raises((InProgressStateMismatch, AdaptiveWeekStateMismatch, GoalNotReleasable)):
        f.service.release_week_one(f.provider, CHILD, family_declared_capacity=CAPACITY)
    assert _surface(f) == before


def _rewrite_snapshot(f, **changes):
    (cycle,) = f.repos.weekly_cycles.list_for_child(CHILD)
    ((doc_id, row),) = [(i, d) for i, d in f.repos.store.list_all("pilot_weekly_plan_snapshots")
                        if d["cycle_id"] == cycle.cycle_id]
    f.repos.store.set("pilot_weekly_plan_snapshots", doc_id, {**row, **changes})


def test_a_tampered_snapshot_document_is_refused(monkeypatch):
    f = _world(monkeypatch)
    _interrupt(f, "capture_snapshot:after")
    (cycle,) = f.repos.weekly_cycles.list_for_child(CHILD)
    (snap,) = f.repos.weekly_plan_snapshots.list_for_cycle(cycle.cycle_id)
    doc = json.loads(snap.resolved_plan_document)
    doc["activities"][0]["local_date"] = doc["activities"][1]["local_date"]
    _rewrite_snapshot(f, resolved_plan_document=json.dumps(doc))
    before = _surface(f)
    with pytest.raises(AdaptiveWeekStateMismatch):
        f.service.release_week_one(f.provider, CHILD, family_declared_capacity=CAPACITY)
    assert _surface(f) == before and f.this_week_cycle() is None


@pytest.mark.parametrize("changes", [{"source_plan_id": "another-plan"},
                                     {"source_system": SourceSystem.PARENT.value}])
def test_a_snapshot_with_the_wrong_source_identity_is_refused(changes, monkeypatch):
    f = _world(monkeypatch)
    _interrupt(f, "capture_snapshot:after")
    _rewrite_snapshot(f, **changes)
    before = _surface(f)
    with pytest.raises(AdaptiveWeekStateMismatch):
        f.service.release_week_one(f.provider, CHILD, family_declared_capacity=CAPACITY)
    assert _surface(f) == before


def test_a_racer_that_froze_a_different_snapshot_refuses(monkeypatch):
    f = _world(monkeypatch)

    def different(original, args, kwargs):
        principal, cycle_id, source, plan_id, _document = args
        original(principal, cycle_id, source, plan_id, {"document_schema": "a-different-week"})

    _concurrent_first(f.weekly, "capture_snapshot", mutate=different)
    with pytest.raises(AdaptiveWeekStateMismatch):
        f.service.release_week_one(f.provider, CHILD, family_declared_capacity=CAPACITY)
    (cycle,) = f.repos.weekly_cycles.list_for_child(CHILD)
    assert not f.repos.weekly_cycles.get_by_id(cycle.cycle_id).is_released


# ===========================================================================
# idempotency
# ===========================================================================


@pytest.mark.parametrize("point", ["allocate_cycle:after", "capture_snapshot:after", "release_cycle:before"])
def test_a_retry_after_a_successful_resume_is_a_pure_replay(point, monkeypatch):
    f = _world(monkeypatch)
    _interrupt(f, point)
    s1, first = f.release()
    assert s1 == 200
    after_resume = _surface(f)
    for capacity in (CAPACITY, 1, 7):               # a later capacity mutates nothing
        status, again = f.http("POST", f"/pilot/children/{CHILD}/weekly-cycles/current/release",
                               body={"family_declared_capacity": capacity})
        assert status == 200 and again["created"] is False
        assert (again["cycle_id"], again["week"]) == (first["cycle_id"], first["week"])
    assert _surface(f) == after_resume
    assert _counts(f) == ONE_OF_EVERYTHING


# ===========================================================================
# forced interleaving: two resumptions of the same interrupted week
# ===========================================================================


def test_a_snapshot_frozen_by_an_identical_racer_is_accepted(clean, monkeypatch):
    expected, _clean = clean
    f = _world(monkeypatch)
    _interrupt(f, "allocate_cycle:after")             # state 3
    _concurrent_first(f.weekly, "capture_snapshot")   # resumer A freezes it first
    out = f.service.release_week_one(f.provider, CHILD, family_declared_capacity=CAPACITY)
    assert out.released_at is not None
    assert _counts(f) == ONE_OF_EVERYTHING
    assert _fingerprint(f) == expected


def test_a_week_released_by_an_identical_racer_is_returned_not_rereleased(clean, monkeypatch):
    expected, _clean = clean
    f = _world(monkeypatch)
    _interrupt(f, "capture_snapshot:after")           # state 4
    _concurrent_first(f.weekly, "release_cycle")      # resumer A releases first
    out = f.service.release_week_one(f.provider, CHILD, family_declared_capacity=CAPACITY)
    assert out.created is False
    assert _counts(f) == ONE_OF_EVERYTHING
    assert _fingerprint(f) == expected


def test_two_resumes_holding_stale_reads_release_exactly_once(monkeypatch):
    """Both resumers read the interrupted week as unreleased; the transaction
    check lets exactly one release commit."""
    from pilot_backend.weekly.errors import ReleasedPlanImmutable

    f = _world(monkeypatch)
    _interrupt(f, "capture_snapshot:after")
    (cycle,) = f.repos.weekly_cycles.list_for_child(CHILD)
    stale = f.repos.weekly_cycles.get_by_id(cycle.cycle_id)
    first = f.service.release_week_one(f.provider, CHILD, family_declared_capacity=CAPACITY)
    assert first.released_at is not None
    real_load = f.weekly._load_cycle
    f.weekly._load_cycle = lambda cycle_id: stale    # resumer two's stale view
    try:
        with pytest.raises(ReleasedPlanImmutable):
            f.weekly.release_cycle(f.provider, cycle.cycle_id)
    finally:
        f.weekly._load_cycle = real_load
    assert _counts(f) == ONE_OF_EVERYTHING
