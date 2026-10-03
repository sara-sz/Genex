"""0.5D — the two read-model enrichments the browser experience needed.

Reuses the 0.5C staged world verbatim, so these tests exercise the SAME
fixture the frozen end-to-end path does. Nothing here adds a route.

## What 0.5D deliberately did NOT do

The Parent weekly UI wanted resolved activity display fields — title,
instructions, domain, materials, a per-activity date. None of those is
persisted anywhere. `WeeklyPlanSnapshot` keeps one opaque JSON string, and its
docstring states why giving it a schema would be a claim the pilot cannot
keep. So 0.5D passes that document through VERBATIM and says so in the
payload, rather than normalising it into a pilot-owned shape.

Two fields in the frozen 0.5C surface are permanently empty for the same
underlying reason — `cycle.state` and `alignment.scheduled_local_date` are
read with `getattr(obj, name, default)` against objects that never had them.
0.5D does not remove them (a client may already read them) but it PINS their
emptiness, so nobody mistakes the gap for a bug in their own code.
"""

from __future__ import annotations

import json

from pilot_backend.domain.goals import GoalKind, GoalRef

# `world` depends on `e2e`, so both have to be imported — a fixture pulled in
# by name does not drag its own dependencies with it.
from .test_workflow_e2e import (  # noqa: F401
    MONTH,
    SENTINEL_TEXT,
    e2e,
    get,
    post,
    world,
)

FORBIDDEN = {"error": "not permitted"}


# ===========================================================================
# A. the goal read model
# ===========================================================================

def test_the_goal_list_carries_the_current_wording(world):
    status, body, _ = get(world, f"/pilot/children/{world.child}/goals")
    assert status == 200, body
    goal = body["goals"][0]
    assert goal["text"] == SENTINEL_TEXT
    assert goal["current_version_id"] == world.goal["current_version_id"]


def test_the_wording_comes_from_the_version_not_the_goal(world):
    """The text must be resolved through `current_version_id`, not stored.

    Revising the goal mints a NEW immutable version and repoints the goal at
    it. If the read were duplicating text onto `ClinicalGoal`, the list would
    keep serving the old wording after a revision.
    """
    revised = "Fictional-sentinel-REVISED-9920"
    status, body, _ = post(
        world,
        f"/pilot/goals/clinical/{world.goal['goal_id']}/revisions",
        "hannah", {"text": revised, "edit_type": "modified",
                   "reason": "Fictional revision rationale"})
    assert status == 200, body
    new_version_id = body["version_id"]
    assert new_version_id != world.goal["current_version_id"]

    status, body, _ = get(world, f"/pilot/children/{world.child}/goals")
    goal = body["goals"][0]
    assert goal["text"] == revised, "the list served a stale wording"
    assert goal["current_version_id"] == new_version_id


def test_no_goal_version_provenance_leaks_into_the_payload(world):
    """`reason` can carry clinical rationale. None of the history ships."""
    status, body, _ = get(world, f"/pilot/children/{world.child}/goals")
    goal = body["goals"][0]
    assert set(goal) == {"goal_kind", "goal_id", "current_version_id",
                         "status", "is_rtm_eligible", "text"}
    for forbidden in ("edit_type", "reason", "actor_id", "actor_role",
                      "derived_from_suggestion_id", "supersedes_version_id",
                      "version_number", "created_at"):
        assert forbidden not in goal


def test_the_approval_response_reads_the_persisted_wording_back(world):
    """What was PERSISTED, not what was submitted.

    `_approved_text` may not keep the submitted string verbatim, so echoing
    the request body here would be a guess about the service's behaviour.
    """
    submitted = "Fictional-sentinel-SECOND-GOAL-4412"
    status, body, _ = post(world, f"/pilot/children/{world.child}/goals",
                        "hannah", {"edit_type": "authored_fresh",
                                   "text": submitted,
                                   "reason": "Fictional rationale"})
    assert status == 200, body
    goal = body["goal"]
    assert "text" in goal

    # And it agrees with the authorized service read for the same goal.
    expected = world.goals.current_text(
        world.principal("hannah"),
        GoalRef(GoalKind.CLINICAL, goal["goal_id"]))
    assert goal["text"] == expected




# ===========================================================================
# B. the current-cycle read model
# ===========================================================================

def test_the_current_cycle_carries_the_snapshot_verbatim(world):
    status, body, _ = get(world, f"/pilot/children/{world.child}/current-cycle")
    assert status == 200, body
    snapshot = body["plan_snapshot"]
    assert snapshot is not None

    # The document is the EXACT document the fixture captured — same keys,
    # same nesting, nothing renamed, nothing dropped, nothing added.
    assert snapshot["source_document"] == {
        "activities": [{"ref": "fictional-activity-1"},
                       {"ref": "fictional-activity-2"}]}


def test_the_snapshot_declares_itself_non_canonical_in_the_payload(world):
    """The disclaimer travels WITH the data, not only in documentation.

    A client that acquires this shape by accident must be able to see, in the
    response, that the pilot makes no promise about it.
    """
    status, body, _ = get(world, f"/pilot/children/{world.child}/current-cycle")
    snapshot = body["plan_snapshot"]
    assert snapshot["is_canonical"] is False
    assert snapshot["schema"] == "opaque_source_document"


def test_the_snapshot_carries_the_provenance_needed_to_interpret_it(world):
    status, body, _ = get(world, f"/pilot/children/{world.child}/current-cycle")
    snapshot = body["plan_snapshot"]
    assert snapshot["source_system"] == "parent"
    assert snapshot["source_plan_id"] == "fictional-parent-plan-1"
    assert snapshot["cycle_id"] == world.cycle_id
    assert snapshot["captured_at"]
    assert set(snapshot) == {
        "snapshot_id", "cycle_id", "schema", "is_canonical", "source_system",
        "source_plan_id", "source_generated_at", "captured_at",
        "source_document"}


def test_the_alignments_stay_separate_from_the_snapshot(world):
    """ActivityGoalAlignment is the pilot's OWN record and must not be merged.

    The snapshot says what the family saw; the alignment says which goal an
    activity was scheduled to serve. Merging them would make a pilot-owned
    guarantee look like part of Parent's opaque document.
    """
    status, body, _ = get(world, f"/pilot/children/{world.child}/current-cycle")
    assert body["alignments"], "the fixture allocated two activities"
    for alignment in body["alignments"]:
        assert set(alignment) == {"alignment_id", "activity_instance_ref",
                                  "goal_kind", "goal_id",
                                  "scheduled_local_date"}
    # No alignment field appears inside the opaque document, and no document
    # key leaked into an alignment.
    assert "alignments" not in body["plan_snapshot"]["source_document"]


def test_no_alignment_rationale_or_actor_leaks(world):
    """`rationale`, `milestone_refs` and `assigned_by_actor_id` stay internal.

    `ActivityGoalAlignment.VISIBILITY` is SYSTEM_AUDIT, so the exposed subset
    has to stay the minimum the UI needs. 0.5D must not have grown it.
    """
    status, body, _ = get(world, f"/pilot/children/{world.child}/current-cycle")
    serialised = json.dumps(body["alignments"])
    for forbidden in ("rationale", "milestone_refs", "assigned_by_actor_id",
                      "rule_version", "alignment_source", "activity_identity_ref"):
        assert forbidden not in serialised


def test_a_cycle_with_no_snapshot_reports_null_not_an_error(world):
    """Absent is a normal state, so it must not be a refusal or a 500."""
    status, body, _ = get(world, f"/pilot/children/{world.child}/current-cycle",
                       query="cycle_month=1999-01")
    assert status == 200, body
    assert body["plan"] is None
    assert body["plan_snapshot"] is None
    assert body["alignments"] == []
    assert body["coverage_gaps"] == []






# ===========================================================================
# the two permanently-empty 0.5C fields, pinned
# ===========================================================================

def test_cycle_state_is_always_null_and_that_is_pinned(world):
    """`WeeklyCycle` has no `state`; the serialiser defaults it to None.

    Pinned rather than removed: a client may already read the key. If a future
    slice gives `WeeklyCycle` a real state, this test fails and forces the
    frontend contract to be updated deliberately.
    """
    from pilot_backend.domain.weekly_cycle import WeeklyCycle

    assert not hasattr(WeeklyCycle, "state")
    status, body, _ = get(world, f"/pilot/children/{world.child}/current-cycle")
    assert body["cycle"]["state"] is None


def test_the_alignment_date_is_always_empty_and_that_is_pinned(world):
    """`ActivityGoalAlignment` has no `scheduled_local_date`.

    There is NO per-activity date persisted anywhere in the pilot, which is
    why 0.5D could not supply one. Only the cycle's own window is real.
    """
    from pilot_backend.domain.alignment import ActivityGoalAlignment

    assert "scheduled_local_date" not in {
        f.name for f in ActivityGoalAlignment.__dataclass_fields__.values()}
    status, body, _ = get(world, f"/pilot/children/{world.child}/current-cycle")
    for alignment in body["alignments"]:
        assert alignment["scheduled_local_date"] == ""
    # The real week window, which IS persisted.
    assert body["cycle"]["starts_on"]
    assert body["cycle"]["ends_on"]


# ===========================================================================
# the new leak surface 0.5D creates
# ===========================================================================

def test_the_goal_wording_never_reaches_a_log_line(world):
    """0.5D puts clinical wording into a RESPONSE for the first time.

    Before this slice no goal text crossed the transport, so no log line could
    have carried one. Now that it does, the log sink has to be checked
    explicitly — a response field and a log field are one careless f-string
    apart.
    """
    status, body, _ = get(world, f"/pilot/children/{world.child}/goals")
    assert status == 200
    assert body["goals"][0]["text"] == SENTINEL_TEXT

    serialised = json.dumps(world.logs)
    assert SENTINEL_TEXT not in serialised, "goal wording reached a log line"


def test_the_snapshot_document_never_reaches_a_log_line(world):
    """Same risk for the pass-through document, which is larger and opaque."""
    status, body, _ = get(world, f"/pilot/children/{world.child}/current-cycle")
    assert status == 200
    assert body["plan_snapshot"]["source_document"]

    serialised = json.dumps(world.logs)
    for marker in ("fictional-activity-1", "fictional-parent-plan-1"):
        assert marker not in serialised, f"{marker} reached a log line"


# ===========================================================================
# C. the goal-suggestions route, which returned 500 in 0.5C
#
# 0.5C had no test with a suggestion PRESENT, so `_suggestion_payload` was
# never run against a real `GoalSuggestion` and four of its eight fields were
# reading attributes that do not exist. The first one crashed the route.
#
# These tests supply a real suggestion, which is the only thing that would
# have caught it.
# ===========================================================================

def _seeded_suggestion(world):
    """One real suggestion, through the frozen engine."""
    from pilot_backend.goals.suggestion_engine import (
        EvidenceSource,
        ObservationSnapshot,
        ObservedDomain,
    )

    snapshot = ObservationSnapshot(
        child_id=world.child,
        cycle_month=MONTH,
        domains=(ObservedDomain(
            domain_key="talking_and_communicating", answered=True,
            evidence_source=EvidenceSource.EXPLICIT_SELECTION,
            functional_baseline_area="fictional-baseline-area-1",
            observed_level="fictional-level-1",
            explicitly_selected=True),),
    )
    return world.goals.generate_suggestions(
        world.principal("cg"), world.child, snapshot)


def test_the_suggestions_route_serves_a_real_suggestion(world):
    """The regression test for the 500. In 0.5C this returned 500."""
    created = _seeded_suggestion(world)
    assert created, "the engine produced no suggestion to serve"

    status, body, _ = get(world,
                          f"/pilot/children/{world.child}/goal-suggestions")
    assert status == 200, body
    # Every seeded suggestion is SERVED. Checked by id rather than by count:
    # 0.5E-A's `world` fixture already holds the anchored suggestion its
    # clinical goal was approved from, so the list is a superset.
    served = {s["suggestion_id"] for s in body["suggestions"]}
    assert {c.suggestion_id for c in created} <= served


def test_the_suggestion_payload_carries_real_wording_not_an_empty_string(world):
    _seeded_suggestion(world)
    status, body, _ = get(world,
                          f"/pilot/children/{world.child}/goal-suggestions")
    suggestion = body["suggestions"][0]

    # The TEMPLATE keeps its real name, so no client reads it as finished prose.
    assert suggestion["family_facing_text_template"]
    assert "text" not in suggestion


def test_the_suggestion_provenance_is_actually_populated(world):
    """The point of the payload: a clinician can see WHY a target was proposed.

    Each of these served "" in 0.5C, because they were read off the suggestion
    instead of off its evidence.
    """
    created = _seeded_suggestion(world)[0]
    status, body, _ = get(world,
                          f"/pilot/children/{world.child}/goal-suggestions")
    # Selected by ID, not by position. 0.5E-A's `world` fixture legitimately
    # already holds the anchored suggestion its clinical goal was approved
    # from, so "the first one" is no longer this test's subject.
    suggestion = next(s for s in body["suggestions"]
                      if s["suggestion_id"] == created.suggestion_id)

    assert suggestion["domain_key"] == "talking_and_communicating"
    assert suggestion["evidence_source"] == "explicit_selection"
    assert suggestion["functional_baseline_area"] == "fictional-baseline-area-1"
    assert suggestion["observed_level"] == "fictional-level-1"
    assert suggestion["explicitly_selected"] is True
    assert suggestion["rule_version"]
    assert suggestion["generator_version"]


def test_the_suggestion_payload_shape_is_pinned(world):
    _seeded_suggestion(world)
    status, body, _ = get(world,
                          f"/pilot/children/{world.child}/goal-suggestions")
    assert set(body["suggestions"][0]) == {
        "suggestion_id", "family_facing_text_template", "status",
        "cycle_month", "suggested_priority_rank", "suggested_emphasis_weight",
        "generator_version", "generation_mode", "domain_key",
        "evidence_source", "milestone_refs", "functional_baseline_area",
        "observed_level", "explicitly_selected", "rule_version"}


def test_the_prior_month_summary_is_never_exposed(world):
    """It points at a prior clinical summary. No screen needs it."""
    _seeded_suggestion(world)
    status, body, _ = get(world,
                          f"/pilot/children/{world.child}/goal-suggestions")
    serialised = json.dumps(body)
    assert "prior_month_summary_id" not in serialised
    assert "policy_version" not in json.dumps(body["suggestions"])


def test_the_suggestions_route_serves_a_list_without_crashing(world):
    """The 0.5C case that passed, kept so the fix did not break it.

    Renamed from `..._an_empty_suggestion_list_...`: 0.5E-A's `world` fixture
    can no longer have an EMPTY list, because its clinical goal must be
    approved from an anchored suggestion to be allocatable. The test's actual
    subject never was emptiness — it was that the route serves its list
    without the `AttributeError` that made it 500 in 0.5C — and that is what
    is asserted here.
    """
    status, body, _ = get(world,
                          f"/pilot/children/{world.child}/goal-suggestions")
    assert status == 200, body
    assert isinstance(body["suggestions"], list)
    for suggestion in body["suggestions"]:
        assert suggestion["family_facing_text_template"]


def test_accepting_verbatim_returns_the_SUGGESTION_text_not_the_body(world):
    """The discriminating case for "read the persisted wording back".

    A mutation sweep showed that echoing the request body passed every other
    test, because for `authored_fresh` the persisted text IS the submitted
    text — so the two behaviours are indistinguishable there.

    `ACCEPTED_VERBATIM` is the case where they differ. `_approved_text`
    deliberately takes the suggestion's OWN template and ignores whatever the
    caller echoed back, because otherwise "accepted verbatim" would be a claim
    the record cannot support. So submitting a DIFFERENT text alongside
    `accepted_verbatim` proves which value the response carries.
    """
    suggestions = _seeded_suggestion(world)
    suggestion = suggestions[0]

    status, body, _ = post(
        world, f"/pilot/children/{world.child}/goals", "hannah",
        {"edit_type": "accepted_verbatim",
         "suggestion_id": suggestion.suggestion_id,
         "text": "Fictional-sentinel-ECHOED-BODY-SHOULD-NOT-APPEAR"})
    assert status == 200, body

    returned = body["goal"]["text"]
    assert returned == suggestion.family_facing_text_template
    assert "ECHOED-BODY-SHOULD-NOT-APPEAR" not in returned, (
        "the response echoed the request body instead of the persisted wording")
