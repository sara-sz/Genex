"""P0-B2a follow-ups: placement audit, bridge-question scope, every answer
combination, the successor-plan rule, and clinician-review items.

Same dependency-pure world as `test_p0b2a_developmental_recalibration`: a NEW
fictional child per test, the real bank and the real generated ladder.
"""

from __future__ import annotations

import itertools
import json

import pytest

from pilot_backend.audit.events import ALLOWED_METADATA_KEYS
from pilot_backend.domain.developmental_ladder import (
    P0B2A_RULE_VERSION,
    BaselineStatus,
    DevelopmentalPlacement,
    LadderError,
    PlacementSource,
    ProposalBasis,
    clinician_review_item,
    developmental_placement_id,
)
from pilot_backend.domain.goals import EditType
from pilot_backend.integration.developmental_recalibration import (
    PlacementRefused,
    RecalibrationError,
    placement_audit_event_id,
)

from .test_evidence_driven_generation_v2 import World, _projection, mixed
from .test_p0b2a_developmental_recalibration import (
    CHILD,
    LADDER,
    NOT_READY,
    TOO_EASY,
    TOO_HARD,
    W1,
    W2,
    Recal,
)

ACTION = "developmental_placement_confirmed"
SUCCESSOR_DAY = "2026-12-14"


def placement_events(r):
    return [d for _i, d in r.repos.store.list_all("pilot_audit_events")
            if d.get("action") == ACTION]


# ===========================================================================
# 1. placement audit event
# ===========================================================================


def test_first_placement_writes_exactly_one_audit_event(monkeypatch):
    r = Recal(monkeypatch, answers=("yes", "sometimes", None), release=False)
    placement = r.recal.place(r.provider, r.plan.focus_plan_id, "step_3")
    (event,) = placement_events(r)
    assert event["event_id"] == placement_audit_event_id(placement.placement_id)
    assert event["child_id"] == CHILD
    assert event["actor_application_id"] == r.provider.application_id
    assert event["actor_role"] == "provider"
    m = event["metadata"]
    assert m["focus_plan_id"] == r.plan.focus_plan_id
    assert m["placement_id"] == placement.placement_id
    assert m["ladder_ref"] == LADDER.ladder_ref
    assert m["rule_version"] == P0B2A_RULE_VERSION
    assert m["starting_node_ref"] == LADDER.by_step_key("step_3").node_ref
    assert m["starting_node_index"] == "2"
    assert m["placement_source"] == "confirmed_baseline_proposal"
    assert m["proposal_basis"] == "baseline"
    assert event["occurred_at"]
    assert set(m) <= ALLOWED_METADATA_KEYS
    blob = json.dumps(event).lower()
    for n in LADDER.nodes:                      # no bridge wording, no answers
        assert n.wording.lower() not in blob
    for banned in ("sometimes", "not_yet", "month", "milk"):
        assert banned not in blob.replace("cycle_month", ""), banned


def test_an_adjusted_placement_is_audited_as_clinician_choice(monkeypatch):
    r = Recal(monkeypatch, answers=("yes", "sometimes", None), release=False)
    r.recal.place(r.provider, r.plan.focus_plan_id, "step_4")
    (event,) = placement_events(r)
    assert event["metadata"]["placement_source"] == "clinician_choice"
    assert event["metadata"]["proposed_node_ref"] == \
        LADDER.by_step_key("step_3").node_ref


def test_replay_and_conflicting_placement_add_no_event(monkeypatch):
    r = Recal(monkeypatch, release=False)
    r.recal.place(r.provider, r.plan.focus_plan_id, "step_3")
    r.recal.place(r.provider, r.plan.focus_plan_id, "step_3")
    with pytest.raises(PlacementRefused):
        r.recal.place(r.provider, r.plan.focus_plan_id, "step_4")
    assert len(placement_events(r)) == 1
    assert len(list(r.repos.store.list_all(
        "pilot_developmental_placements"))) == 1


def test_unplaced_plans_have_no_placement_event(monkeypatch):
    r = Recal(monkeypatch)                      # activated, released, unplaced
    r.release(W2)
    assert placement_events(r) == []


@pytest.mark.parametrize("source,proposed,start", [
    (PlacementSource.CONFIRMED_BASELINE_PROPOSAL, 2, 3),   # adjusted as confirmed
    (PlacementSource.CLINICIAN_CHOICE, 2, 2),              # confirmed as adjusted
    (PlacementSource.CONFIRMED_BASELINE_PROPOSAL, None, 2),
    (PlacementSource.CONFIRMED_PREVIOUS_PLAN_STEP, 2, 2),  # baseline basis
], ids=["adj-as-confirmed", "kept-as-adjusted", "no-proposal", "wrong-basis"])
def test_a_mislabelled_placement_cannot_exist(source, proposed, start):
    with pytest.raises(LadderError):
        DevelopmentalPlacement(
            placement_id=developmental_placement_id("mfpl_x"), child_id="c",
            focus_plan_id="mfpl_x", goal_id="g", ladder_ref=LADDER.ladder_ref,
            envelope_node_refs=LADDER.node_refs,
            starting_node_ref=LADDER.nodes[start].node_ref,
            starting_node_index=start,
            proposed_node_ref=(LADDER.nodes[proposed].node_ref
                               if proposed is not None else ""),
            baseline_answers_id="", baseline_status=BaselineStatus.CONSISTENT,
            source=source, proposal_basis=ProposalBasis.BASELINE,
            previous_placement_id="", rule_version=P0B2A_RULE_VERSION,
            placed_by_actor_id="prov_x")


# ===========================================================================
# 2. bridge questions only for this Pilot ladder's target
# ===========================================================================


class Unapproved(Recal):
    """A child whose F-B v2 suggestion exists but is NOT approved yet."""

    def __init__(self, monkeypatch, *, state_24="emerging", states_30=None):
        import pilot_backend.domain.entities as entities_module
        from .test_first_plan_timezone_gate import _pin_wall_clock
        from .test_p0b1_adaptive_week_two import Clock, _source
        from pilot_backend.integration.developmental_recalibration import (
            DevelopmentalRecalibrationService,
        )
        self.clock = Clock(W1)
        monkeypatch.setattr(entities_module, "utc_now", self.clock)
        _pin_wall_clock(monkeypatch, self.clock)
        self.world = World(source=_source(), projection=_projection(
            states_30=states_30 or mixed(), state_24=state_24))
        self.repos, self.goals = self.world.repos, self.world.goals
        self.provider = self.world.principal()
        self.caregiver = self.world.principal("cg-subject")
        self.recal = DevelopmentalRecalibrationService(
            repos=self.repos, goals=self.goals, plans=None, weekly=None,
            ladder=LADDER, now=self.clock)
        self.generated = self.world.generate()


def test_questions_apply_to_an_offered_ladder_suggestion(monkeypatch):
    u = Unapproved(monkeypatch)
    assert u.generated.target_band_months == 24
    view = u.recal.baseline_questions(u.caregiver, CHILD)
    assert view["applicable"] is True and len(view["questions"]) == 3


def test_questions_never_appear_for_another_milestone(monkeypatch):
    """24m demonstrated -> F-B v2 targets a 30m milestone: not this ladder."""
    u = Unapproved(monkeypatch, state_24="demonstrated",
                   states_30=mixed(**{
                       "name things in a book when you point and ask what is this":
                       "emerging"}))
    assert u.generated.target_band_months != 24
    assert u.recal.baseline_questions(u.caregiver, CHILD) == {"applicable": False}
    with pytest.raises(RecalibrationError):
        u.recal.submit_baseline(u.caregiver, CHILD, ["yes", None, None])
    assert list(u.repos.store.list_all("pilot_bridge_baseline_answers")) == []


def test_an_approved_off_ladder_goal_switches_the_questions_off(monkeypatch):
    u = Unapproved(monkeypatch, state_24="demonstrated",
                   states_30=mixed(**{
                       "name things in a book when you point and ask what is this":
                       "emerging"}))
    u.goals.approve_clinical_goal(
        u.provider, CHILD, edit_type=EditType.ACCEPTED_VERBATIM,
        suggestion_id=u.generated.generated[0].suggestion_ids[0])
    assert u.recal.bridge_applicable(CHILD) is False


def test_a_provider_cannot_read_or_answer_the_family_questions(monkeypatch):
    u = Unapproved(monkeypatch)
    with pytest.raises(RecalibrationError):
        u.recal.baseline_questions(u.provider, CHILD)
    with pytest.raises(RecalibrationError):
        u.recal.submit_baseline(u.provider, CHILD, ["yes", None, None])


def _oracle(answers):
    """An INDEPENDENT statement of the founder rule (not the implementation).

    Returns the expected proposed step key, or None for clinician confirmation.
    """
    start = None
    for i, a in enumerate(answers):
        if a in (None, "not_sure"):
            return None                      # unresolved before a start
        if a in ("sometimes", "not_yet"):
            start = i
            break
    if start is None:
        return "step_5"                      # Yes, Yes, Yes
    if "yes" in answers[start + 1:]:
        return None                          # out of order
    return f"step_{start + 2}"               # bridges are steps 2..4


def test_every_answer_combination_matches_the_rule(monkeypatch):
    r = Recal(monkeypatch, release=False)
    values = ("yes", "sometimes", "not_yet", "not_sure", None)
    seen = 0
    from datetime import timedelta
    for combo in itertools.product(values, repeat=3):
        # Real submissions are seconds apart; "latest" is by submission time.
        r.clock.value += timedelta(seconds=1)
        r.recal.submit_baseline(r.caregiver, CHILD, list(combo))
        view = r.recal.start_view(r.provider, CHILD)
        expected = _oracle(combo)
        assert view["proposed_step_key"] == expected, combo
        assert view["baseline"]["status"] == (
            "consistent" if expected else "needs_clinician_confirmation"), combo
        assert view["proposed_step_key"] != "step_1"
        seen += 1
    assert seen == 125


# ===========================================================================
# 3. successor plan — proposed, never carried
# ===========================================================================


def _successor(monkeypatch):
    """Plan 1 placed at Bridge 2, moved to Bridge 3 in Week 2; on Dec 14 the
    clinical period has ended and a successor DRAFT is created."""
    r = Recal(monkeypatch, start="step_3")
    r.twice(r.week_one, **TOO_EASY)
    w2 = r.release(W2)
    assert r.movement(w2).node_after_index == 3
    r.clock.set(SUCCESSOR_DAY)
    successor = r.plans.create_plan(r.provider, CHILD, "2026-12", "America/New_York")
    r.plans.allocate_goal(r.provider, successor.focus_plan_id, r.goal.ref,
                          priority_rank=1)
    return r, successor


def test_the_successor_is_offered_the_previous_final_step(monkeypatch):
    r, _succ = _successor(monkeypatch)
    view = r.recal.start_view(r.provider, CHILD)
    assert view["proposal_basis"] == "previous_plan"
    assert view["proposed_step_key"] == "step_4", "the FINAL derived node"
    assert [p["current_step_key"] for p in view["placements"]] == ["step_4"]


def test_a_successor_is_never_placed_by_itself(monkeypatch):
    """No placement is carried: activating and releasing the successor
    without one writes no placement, no audit, no movement — P0-B1 Week 1."""
    r, succ = _successor(monkeypatch)
    events_before = len(placement_events(r))
    r.plans.activate_plan(r.provider, succ.focus_plan_id)
    week = r.service.release_week_one(
        r.provider, CHILD, family_declared_capacity=3, cycle_month="2026-12",
        local_date=SUCCESSOR_DAY)
    assert week.focus_plan_id == succ.focus_plan_id
    assert r.repos.developmental_placements.find_for_plan(
        succ.focus_plan_id) is None
    assert len(placement_events(r)) == events_before
    assert [m for m in r.movement_docs()
            if m["focus_plan_id"] == succ.focus_plan_id] == []
    # Unplaced => exactly the P0-B1 Week 1 (both anchor families, content-
    # digest order) — the same three activities an unplaced first plan gets.
    assert set(r.refs(week)) == {"My Turn", "More Bubbles", "All Done and Stop"}
    with pytest.raises(PlacementRefused):
        r.recal.place(r.provider, succ.focus_plan_id, "step_4")


def test_confirming_the_successor_proposal_is_its_own_approval(monkeypatch):
    r, succ = _successor(monkeypatch)
    placed = r.recal.place(r.provider, succ.focus_plan_id, "step_4")
    first = r.repos.developmental_placements.find_for_plan(r.plan.focus_plan_id)
    assert placed.source is PlacementSource.CONFIRMED_PREVIOUS_PLAN_STEP
    assert placed.proposal_basis is ProposalBasis.PREVIOUS_PLAN
    assert placed.previous_placement_id == first.placement_id
    assert placed.placement_id != first.placement_id
    assert placed.envelope_node_refs == LADDER.node_refs
    assert len(placement_events(r)) == 2, "its own audit event"


def test_adjusting_the_successor_proposal_is_a_clinician_choice(monkeypatch):
    r, succ = _successor(monkeypatch)
    placed = r.recal.place(r.provider, succ.focus_plan_id, "step_3")
    assert placed.source is PlacementSource.CLINICIAN_CHOICE
    assert placed.proposed_node_ref == LADDER.by_step_key("step_4").node_ref
    assert placed.proposal_basis is ProposalBasis.PREVIOUS_PLAN


# ===========================================================================
# 4. clinician-review records carry what the next UI needs
# ===========================================================================


@pytest.mark.parametrize("start,signal,reason,direction", [
    ("step_2", TOO_EASY, "transition_not_enabled", "up"),
    ("step_5", TOO_EASY, "outside_envelope", "up"),
    ("step_3", NOT_READY, "wasnt_ready", None),
], ids=["unsupported", "outside", "notready"])
def test_review_records_carry_the_next_ui_fields(monkeypatch, start, signal,
                                                 reason, direction):
    r = Recal(monkeypatch, start=start)
    r.twice(r.week_one, **signal)
    w2 = r.release(W2)
    movement = r.movement(w2)
    item = clinician_review_item(movement, LADDER)
    assert item.child_id == CHILD
    assert item.current_step_key == start
    assert item.reason.value == reason
    assert (item.attempted_direction or None) == direction
    assert len(item.evidence_event_ids) == 2
    stored = {e.event_id for e in r.repos.observation_events.list_for_cycle(
        r.week_one.cycle_id)}
    assert set(item.evidence_event_ids) <= stored
    (row,) = r.recal.start_view(r.provider, CHILD)["placements"][0]["review_items"]
    assert row["reason"] == reason and row["current_step_key"] == start
    assert row["attempted_direction"] == direction
    # Nothing of it reaches the family.
    status, body = r.http("GET", f"/pilot/children/{CHILD}/this-week", token="cg")
    blob = json.dumps(body)
    assert status == 200 and movement.movement_id not in blob
    for e in item.evidence_event_ids:
        assert e not in blob
    for word in (reason, "review", "step_"):
        assert word not in blob


def test_conflicting_review_record_cites_all_four_observations(monkeypatch):
    r = Recal(monkeypatch, start="step_3")
    a, b = sorted(r.refs(r.week_one))[:2]
    r.twice(r.week_one, title=a, **TOO_EASY)
    r.twice(r.week_one, title=b, **TOO_HARD)
    item = clinician_review_item(r.movement(r.release(W2)), LADDER)
    assert item.reason.value == "conflicting_signals"
    assert len(item.evidence_event_ids) == 4
    assert len(item.trigger_activity_refs) == 2


def test_moves_and_non_moves_are_not_review_items(monkeypatch):
    r = Recal(monkeypatch, start="step_3")
    r.twice(r.week_one, **TOO_EASY)
    assert clinician_review_item(r.movement(r.release(W2)), LADDER) is None
