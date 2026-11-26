"""P0-B2a — constrained developmental recalibration (NOT full P0-B2).

Dependency-pure: `FirestoreRepositories` over `FakeDocumentStore`, the REAL
reviewed bank and the REAL generated five-node ladder artifact. The goal comes
from F-B v2 generation + verbatim approval; Week 1 from the real release path;
observations from the real caregiver recording path; Week N+1 from the same
release entry point the HTTP route calls.

A NEW fictional child per test. No live record is touched.

Steps (Therapist Dev keys):
    step_1  lower endpoint (no reviewed pool)
    step_2  Bridge 1       vocabulary pool
    step_3  Bridge 2       vocabulary pool
    step_4  Bridge 3       two-word pool
    step_5  anchor endpoint two-word pool
Only step_3 <-> step_4 moves automatically.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from pilot_backend.auth import DevAuthVerifier, VerifiedToken
from pilot_backend.config import PilotSettings
from pilot_backend.domain import entities as entities_module
from pilot_backend.domain.adaptation_decision import (
    ActivityAdaptationDecision,
    AdaptationBranch,
    DecisionReason,
)
from pilot_backend.domain.developmental_ladder import (
    P0B2A_RULE_VERSION,
    BaselineStatus,
    BridgeAnswer as A,
    DevelopmentalMovement,
    Direction,
    LadderError,
    MovementOutcome,
    MovementReason,
    PlacementSource,
    StartReason,
    developmental_movement_id,
    evaluate_movement,
    propose_start,
)
from pilot_backend.domain.goals import EditType
from pilot_backend.domain.observation import AttemptOutcome, Difficulty, Enjoyment
from pilot_backend.integration.developmental_recalibration import (
    DevelopmentalRecalibrationService,
    LadderNotApplicable,
    PlacementRefused,
    RecalibrationError,
)
from pilot_backend.integration.parent_source import InMemoryParentSessionSource
from pilot_backend.integration.week_one_release import (
    AdaptiveWeekStateMismatch,
    ClinicalBoundaryRefused,
    WeekOneReleaseService,
)
from pilot_backend.planning.service import MonthlyPlanService
from pilot_backend.transport import build_application
from pilot_backend.weekly.service import WeeklyService
from pilot_runtime.integration.static_activity_bank import StaticActivityBank
from pilot_runtime.integration.static_developmental_ladder import (
    DEFAULT_ARTIFACT_PATH,
    StaticLadderError,
    load_developmental_ladder,
)

from .test_evidence_driven_generation_v2 import World, _projection, mixed
from .test_first_plan_timezone_gate import _pin_wall_clock
from .test_integration_identity import call
from .test_p0b1_adaptive_week_two import CHILD, Clock, _source

TZ = "America/New_York"
MONTH = "2026-11"
W1, W2, W3 = "2026-11-17", "2026-11-23", "2026-11-30"
CAPACITY = 3
BANK = StaticActivityBank()
LADDER = load_developmental_ladder(activity_bank=BANK)
FAMILY_IDS = ("expressive_vocabulary_growth", "two_word_phrases",
              "expressive_first_words", "book_object_naming",
              "sentence_building")


def pool(step):
    return set(LADDER.by_step_key(step).pool_template_ids)


class Recal:
    """A NEW fictional child with an approved 24m goal and (optionally) a
    clinician-placed draft plan, then Week 1 released on Tue 2026-11-17."""

    def __init__(self, monkeypatch, *, start=None, answers=None,
                 ladder=LADDER, release=True):
        self.clock = Clock(W1)
        monkeypatch.setattr(entities_module, "utc_now", self.clock)
        _pin_wall_clock(monkeypatch, self.clock)
        self.world = World(source=_source(),
                           projection=_projection(states_30=mixed(),
                                                  state_24="emerging"))
        self.repos, self.goals = self.world.repos, self.world.goals
        self.provider = self.world.principal()
        self.caregiver = self.world.principal("cg-subject")
        self.plans = MonthlyPlanService(repos=self.repos,
                                        recorder=self.world.recorder,
                                        now=self.clock)
        self.weekly = WeeklyService(repos=self.repos,
                                    recorder=self.world.recorder,
                                    now=self.clock)
        self.ladder = ladder
        self.service = WeekOneReleaseService(
            repos=self.repos, goals=self.goals, plans=self.plans,
            weekly=self.weekly, activity_bank=BANK, now=self.clock,
            ladder=ladder)
        self.recal = DevelopmentalRecalibrationService(
            repos=self.repos, goals=self.goals, plans=self.plans,
            weekly=self.weekly, ladder=ladder, now=self.clock)
        generated = self.world.generate()
        self.goal = self.goals.approve_clinical_goal(
            self.provider, CHILD, edit_type=EditType.ACCEPTED_VERBATIM,
            suggestion_id=generated.generated[0].suggestion_ids[0])
        verifier = DevAuthVerifier("dev")
        for token, subject in (("cg", "cg-subject"), ("pr", "pr-subject"),
                               ("pr2", "pr2-subject")):
            verifier.add(token, VerifiedToken(
                subject=subject, email="fictional@example.invalid"))
        self.app = build_application(
            settings=PilotSettings.from_env({
                "PILOT_ENVIRONMENT": "dev",
                "PILOT_ALLOWED_ORIGINS": "http://localhost:3000",
                "PILOT_DEV_AUTH": "1"}),
            repos=self.repos, verifier=verifier, recorder=self.world.recorder,
            parent_source=InMemoryParentSessionSource(), log_sink=[],
            activity_bank=BANK, developmental_ladder=ladder)
        if answers is not None:
            self.recal.submit_baseline(self.caregiver, CHILD, answers)
        self.plan = self.plans.create_plan(self.provider, CHILD, MONTH, TZ)
        self.plans.allocate_goal(self.provider, self.plan.focus_plan_id,
                                 self.goal.ref, priority_rank=1)
        if start is not None:
            self.placement = self.recal.place(
                self.provider, self.plan.focus_plan_id, start)
        if release:
            self.plans.activate_plan(self.provider, self.plan.focus_plan_id)
            self.week_one = self.release(W1)
        self._fb = 0

    # -- product path -------------------------------------------------------

    def release(self, day):
        self.clock.set(day)
        return self.service.release_week_one(
            self.provider, CHILD, family_declared_capacity=CAPACITY,
            cycle_month=MONTH, local_date=day)

    def refs(self, outcome):
        return {a["title"]: a["activity_ref"]
                for d in outcome.parent_week["days"] for a in d["activities"]}

    def observe(self, outcome, title, day, *, outcome_kind=AttemptOutcome.DID_IT,
                difficulty=None, enjoyment=None, supersedes=""):
        self.clock.set(day)
        instance = self.weekly.resolve_parent_activity_ref(
            self.caregiver, outcome.cycle_id, self.refs(outcome)[title])
        self._fb += 1
        fid = f"fb_p0b2a_{self._fb:03d}"
        self.weekly.record_observation(
            self.caregiver, outcome.cycle_id, instance, local_date=day,
            attempt_outcome=outcome_kind, difficulty=difficulty,
            enjoyment=enjoyment, source_feedback_id=fid,
            supersedes_source_feedback_id=supersedes)
        return fid

    def twice(self, outcome, days=(W1, "2026-11-18"), title=None, **kw):
        title = title or sorted(self.refs(outcome))[0]
        for day in days:
            self.observe(outcome, title, day, **kw)
        return title

    # -- reads --------------------------------------------------------------

    def placed(self, outcome):
        snap = self.repos.weekly_plan_snapshots.list_for_cycle(
            outcome.cycle_id)[0]
        return {a["activity_template_id"] for a in snap.document()["activities"]}

    def snapshot_doc(self, outcome):
        snap = self.repos.weekly_plan_snapshots.list_for_cycle(
            outcome.cycle_id)[0]
        return json.dumps(snap.document(), sort_keys=True, default=str)

    def movement(self, outcome):
        return self.repos.developmental_movements.find_for_cycle(
            outcome.cycle_id)

    def movement_docs(self):
        return [d for _i, d in self.repos.store.list_all(
            "pilot_developmental_movements")]

    def http(self, method, path, token="pr", body=None):
        status, raw, _ = call(self.app, path, method=method,
                              bearer=f"Bearer {token}",
                              body=json.dumps(body).encode()
                              if body is not None else b"")
        return status, (json.loads(raw) if isinstance(raw, (bytes, str)) else raw)


TOO_EASY = dict(difficulty=Difficulty.TOO_EASY)
TOO_HARD = dict(difficulty=Difficulty.TOO_HARD)
NOT_READY = dict(outcome_kind=AttemptOutcome.WASNT_READY_YET)
DIDNT = dict(outcome_kind=AttemptOutcome.DIDNT_WANT_TO_TRY)


# ===========================================================================
# 1. the ladder and its envelope
# ===========================================================================


def test_the_ladder_is_the_approved_five_nodes_with_one_automatic_pair():
    assert [n.step_key for n in LADDER.nodes] == [f"step_{i}" for i in range(1, 6)]
    assert [(n.node_type, n.step_number) for n in LADDER.nodes] == [
        ("endpoint", 2), ("bridge", 1), ("bridge", 2), ("bridge", 3),
        ("endpoint", 4)]
    assert not LADDER.nodes[0].has_reviewed_pool, "the lower endpoint has 0 cards"
    assert [n.pool_family_ref for n in LADDER.nodes[1:]] == [
        "expressive_vocabulary_growth"] * 2 + ["two_word_phrases"] * 2
    assert len(pool("step_2")) == 12 and len(pool("step_4")) == 6
    pairs = sorted((LADDER.index_of(a), LADDER.index_of(b))
                   for a, b in LADDER.transitions)
    assert pairs == [(2, 3), (3, 2)], "ONLY Bridge 2 <-> Bridge 3"
    assert LADDER.anchor_rung_ref == "rung1:feb590cf2788978b383c11062ce67c1b"


def _tampered(tmp_path, mutate, *, redigest=True):
    body = json.loads(DEFAULT_ARTIFACT_PATH.read_text())
    mutate(body)
    if redigest:
        without = {k: v for k, v in body.items() if k != "artifact_digest"}
        body["artifact_digest"] = hashlib.sha256(json.dumps(
            without, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode()).hexdigest()
    path = tmp_path / "ladder.json"
    path.write_text(json.dumps(body))
    return path


@pytest.mark.parametrize("name,mutate", [
    ("enable_b1_b2", lambda b: b["enabled_transitions"].append(
        {"from": b["nodes"][1]["node_ref"], "to": b["nodes"][2]["node_ref"]})),
    ("skip_b1_b3", lambda b: b["enabled_transitions"].append(
        {"from": b["nodes"][1]["node_ref"], "to": b["nodes"][3]["node_ref"]})),
    ("drop_b2_b3", lambda b: b["enabled_transitions"].pop()),
    ("give_lower_pool", lambda b: b["nodes"][0].update(
        pool_family_ref="expressive_first_words", pool_template_ids=["x"])),
    ("drop_a_node", lambda b: b["nodes"].pop(0)),
    ("reorder", lambda b: b["nodes"].reverse()),
], ids=["b1b2", "skip", "drop", "lowerpool", "four", "reorder"])
def test_a_tampered_ladder_is_refused(tmp_path, name, mutate):
    with pytest.raises(LadderError):
        load_developmental_ladder(_tampered(tmp_path, mutate),
                                  activity_bank=BANK)


def test_a_hand_edit_without_redigest_and_a_stale_pool_are_refused(tmp_path):
    with pytest.raises(StaticLadderError):
        load_developmental_ladder(_tampered(
            tmp_path, lambda b: b["nodes"][1].update(wording="x"),
            redigest=False))
    stale = _tampered(tmp_path, lambda b: b["nodes"][3][
        "pool_template_ids"].pop())
    with pytest.raises(LadderError):
        # The rule would still hold; the BANK cross-check catches the stale pool.
        load_developmental_ladder(stale, activity_bank=BANK)


def test_a_move_can_never_skip_a_node():
    """Every reachable evaluation is one adjacent step; a record with a jump
    cannot be constructed."""
    up = ActivityAdaptationDecision(
        "atpl_x", AdaptationBranch.NO_CHANGE,
        DecisionReason.NOT_YET_SUPPORTED_REPEATED_TOO_EASY,
        signal="repeated_too_easy", trigger_event_ids=("e1", "e2"),
        considered_event_ids=("e1", "e2"))
    down = replace(up, reason=DecisionReason.NOT_YET_SUPPORTED_REPEATED_TOO_HARD,
                   signal="repeated_too_hard")
    for n in LADDER.nodes:
        for d in (up, down):
            ev = evaluate_movement(LADDER, n.node_ref, [d])
            assert abs(LADDER.index_of(ev.node_after_ref) - n.index) <= 1
            assert ev.moved == ((n.index, d is up) in {(2, True), (3, False)})
    good = dict(movement_id=developmental_movement_id("c2"), child_id="c",
                focus_plan_id="p", placement_id="pl", from_cycle_id="c1",
                to_cycle_id="c2", rule_version=P0B2A_RULE_VERSION,
                ladder_ref=LADDER.ladder_ref, goal_id="g",
                outcome=MovementOutcome.MOVED,
                reason=MovementReason.REPEATED_TOO_EASY, direction="up",
                node_before_ref=LADDER.nodes[1].node_ref, node_before_index=1,
                node_after_ref=LADDER.nodes[2].node_ref, node_after_index=2,
                pool_family_before="a", pool_family_after="b",
                evidence_event_ids=("e1", "e2"), trigger_activity_refs=("t",))
    DevelopmentalMovement(**good)
    for bad in ({"node_after_ref": LADDER.nodes[3].node_ref,
                 "node_after_index": 3},                       # skip
                {"direction": "down"},                         # wrong direction
                {"reason": MovementReason.REPEATED_TOO_HARD},  # hard moves down
                {"evidence_event_ids": ("e1",)},               # one observation
                {"outcome": MovementOutcome.NO_MOVE,
                 "reason": MovementReason.NO_TRIGGER}):        # non-move moved
        with pytest.raises(LadderError):
            DevelopmentalMovement(**{**good, **bad})


def test_evidence_already_used_never_moves_again():
    up = ActivityAdaptationDecision(
        "atpl_x", AdaptationBranch.NO_CHANGE,
        DecisionReason.NOT_YET_SUPPORTED_REPEATED_TOO_EASY,
        signal="repeated_too_easy", trigger_event_ids=("e1", "e2"),
        considered_event_ids=("e1", "e2"))
    b2 = LADDER.by_step_key("step_3").node_ref
    assert evaluate_movement(LADDER, b2, [up]).moved
    ev = evaluate_movement(LADDER, b2, [up], consumed_event_ids=("e2",))
    assert ev.outcome is MovementOutcome.REQUIRES_CLINICIAN_REVIEW
    assert ev.reason is MovementReason.EVIDENCE_ALREADY_USED
    assert ev.node_after_ref == b2


# ===========================================================================
# 2. baseline -> proposed starting step
# ===========================================================================

Y, S, N, U = A.YES, A.SOMETIMES, A.NOT_YET, A.NOT_SURE


@pytest.mark.parametrize("answers,status,reason,step", [
    ((S, None, None), "consistent", StartReason.STARTS_AT_BRIDGE, "step_2"),
    ((N, None, None), "consistent", StartReason.STARTS_AT_BRIDGE, "step_2"),
    ((Y, S, None), "consistent", StartReason.STARTS_AT_BRIDGE, "step_3"),
    ((Y, N, N), "consistent", StartReason.STARTS_AT_BRIDGE, "step_3"),
    ((Y, Y, N), "consistent", StartReason.STARTS_AT_BRIDGE, "step_4"),
    ((Y, Y, Y), "consistent", StartReason.ALL_YES, "step_5"),
    ((S, U, None), "consistent", StartReason.STARTS_AT_BRIDGE, "step_2"),
    ((U, None, None), "needs", StartReason.NOT_SURE, None),
    ((Y, U, None), "needs", StartReason.NOT_SURE, None),
    ((Y, Y, U), "needs", StartReason.NOT_SURE, None),
    ((N, Y, None), "needs", StartReason.OUT_OF_ORDER, None),
    ((S, N, Y), "needs", StartReason.OUT_OF_ORDER, None),
    ((Y, None, Y), "needs", StartReason.INCOMPLETE, None),
    (None, "absent", StartReason.NO_BASELINE, None),
], ids=lambda v: "-".join(a.value if a else "_" for a in v)
    if isinstance(v, tuple) else str(v))
def test_baseline_rule(answers, status, reason, step):
    p = propose_start(LADDER, answers)
    assert p.status.value.startswith(status)
    assert p.reason is reason
    assert (LADDER.node(p.node_ref).step_key if p.node_ref else None) == step


def test_the_baseline_never_proposes_the_lower_endpoint():
    import itertools
    for combo in itertools.product([Y, S, N, U, None], repeat=3):
        p = propose_start(LADDER, combo)
        assert p.node_ref != LADDER.nodes[0].node_ref


def test_the_caregiver_questions_are_bridge_wording_with_no_age_or_step(monkeypatch):
    r = Recal(monkeypatch, release=False)
    status, body = r.http("GET", f"/pilot/children/{CHILD}/bridge-baseline",
                          token="cg")
    assert status == 200
    b = body["baseline"]
    assert b["prompt"] == "Right now, does this describe your child?"
    assert [o["label"] for o in b["options"]] == [
        "Yes", "Sometimes", "Not yet", "Not sure"]
    assert [q["text"] for q in b["questions"]] == [
        LADDER.node(ref).wording for ref in LADDER.question_node_refs]
    blob = json.dumps(body).lower()
    for banned in ("node1:", "ldr1:", "rung1:", "step_", "month", "bridge 1",
                   "age", *FAMILY_IDS):
        assert banned not in blob.replace("baseline", ""), banned
    status, body = r.http("POST", f"/pilot/children/{CHILD}/bridge-baseline",
                          token="cg", body={"answers": ["yes", "sometimes", None]})
    assert status == 200 and set(body) == {"recorded", "request_id"}
    # The provider cannot submit the family's answers; malformed is refused.
    assert r.http("POST", f"/pilot/children/{CHILD}/bridge-baseline",
                  body={"answers": ["yes", "yes", "yes"]})[0] == 403
    assert r.http("POST", f"/pilot/children/{CHILD}/bridge-baseline", token="cg",
                  body={"answers": ["yes", "perhaps", None]})[0] == 403
    assert r.http("POST", f"/pilot/children/{CHILD}/bridge-baseline", token="cg",
                  body={"answers": ["yes"]})[0] == 403


# ===========================================================================
# 3. placement (Therapist Dev plan setup)
# ===========================================================================


def test_the_start_view_shows_behaviour_wording_and_nothing_internal(monkeypatch):
    r = Recal(monkeypatch, answers=(Y, S, None), release=False)
    status, body = r.http("GET", f"/pilot/children/{CHILD}/developmental-start")
    assert status == 200, body
    view = body["developmental_start"]
    assert [s["wording"] for s in view["steps"]] == [n.wording for n in LADDER.nodes]
    assert [s["selectable"] for s in view["steps"]] == [False, True, True, True, True]
    assert view["proposed_step_key"] == "step_3"
    assert view["baseline"]["status"] == "consistent"
    assert view["automatic_moves"] == [{"between": ["step_3", "step_4"]}]
    blob = json.dumps(body)
    for banned in ("node1:", "ldr1:", "rung1:", "atpl", "sha256", *FAMILY_IDS):
        assert banned not in blob, banned
    # Caregiver and non-managing provider are refused.
    assert r.http("GET", f"/pilot/children/{CHILD}/developmental-start",
                  token="cg")[0] == 403
    assert r.http("GET", f"/pilot/children/{CHILD}/developmental-start",
                  token="pr2")[0] == 403


@pytest.mark.parametrize("answers,step,source,status", [
    ((Y, S, None), "step_3", PlacementSource.CONFIRMED_BASELINE_PROPOSAL,
     BaselineStatus.CONSISTENT),
    ((Y, S, None), "step_4", PlacementSource.CLINICIAN_CHOICE,
     BaselineStatus.CONSISTENT),
    ((N, Y, None), "step_2", PlacementSource.CLINICIAN_CHOICE,
     BaselineStatus.NEEDS_CLINICIAN_CONFIRMATION),
    (None, "step_5", PlacementSource.CLINICIAN_CHOICE, BaselineStatus.ABSENT),
], ids=["confirm", "override", "inconsistent", "nobaseline"])
def test_placement_records_its_source(monkeypatch, answers, step, source, status):
    r = Recal(monkeypatch, answers=answers, release=False)
    code, body = r.http(
        "POST", f"/pilot/monthly-plans/{r.plan.focus_plan_id}/developmental-placement",
        body={"step_key": step})
    assert code == 200, body
    assert body["placement"]["starting_step_key"] == step
    assert body["placement"]["source"] == source.value
    stored = r.repos.developmental_placements.find_for_plan(r.plan.focus_plan_id)
    assert stored.baseline_status is status
    assert stored.envelope_node_refs == LADDER.node_refs
    assert "node1:" not in json.dumps(body)


def test_placement_refusals(monkeypatch):
    r = Recal(monkeypatch, release=False)
    route = f"/pilot/monthly-plans/{r.plan.focus_plan_id}/developmental-placement"
    # the lower endpoint has no reviewed activities; never the 7 candidates
    assert r.http("POST", route, body={"step_key": "step_1"})[0] == 403
    assert r.http("POST", route, body={"step_key": "step_9"})[0] == 403
    assert r.http("POST", route, token="cg", body={"step_key": "step_3"})[0] == 403
    assert r.http("POST", route, token="pr2", body={"step_key": "step_3"})[0] == 403
    assert r.http("POST", route, body={"step_key": "step_3"})[0] == 200
    assert r.http("POST", route, body={"step_key": "step_3"})[0] == 200, "idempotent"
    assert r.http("POST", route, body={"step_key": "step_4"})[0] == 403, \
        "a different step never overwrites"
    assert len(list(r.repos.store.list_all("pilot_developmental_placements"))) == 1


def test_an_active_plan_cannot_be_placed(monkeypatch):
    r = Recal(monkeypatch)          # activated + Week 1 released, unplaced
    with pytest.raises(PlacementRefused):
        r.recal.place(r.provider, r.plan.focus_plan_id, "step_3")


def test_no_ladder_configured_refuses_every_p0b2a_route(monkeypatch):
    r = Recal(monkeypatch, ladder=None, release=False)
    assert r.http("GET", f"/pilot/children/{CHILD}/bridge-baseline", token="cg")[0] == 403
    assert r.http("GET", f"/pilot/children/{CHILD}/developmental-start")[0] == 403
    assert r.http("POST", f"/pilot/monthly-plans/{r.plan.focus_plan_id}"
                  "/developmental-placement", body={"step_key": "step_3"})[0] == 403


# ===========================================================================
# 4. the two automatic moves
# ===========================================================================


def test_week_one_of_a_placed_plan_uses_only_the_start_pool(monkeypatch):
    for step in ("step_2", "step_3", "step_4", "step_5"):
        r = Recal(monkeypatch, start=step)
        assert r.placed(r.week_one) <= pool(step), step
        assert len(r.placed(r.week_one)) == CAPACITY


def test_bridge2_too_easy_twice_moves_to_bridge3_and_switches_pool(monkeypatch):
    r = Recal(monkeypatch, start="step_3")
    week_one_before = r.snapshot_doc(r.week_one)
    r.twice(r.week_one, **TOO_EASY)
    w2 = r.release(W2)
    m = r.movement(w2)
    assert m.outcome is MovementOutcome.MOVED
    assert m.reason is MovementReason.REPEATED_TOO_EASY and m.direction == "up"
    assert (m.node_before_index, m.node_after_index) == (2, 3)
    assert (m.pool_family_before, m.pool_family_after) == (
        "expressive_vocabulary_growth", "two_word_phrases")
    assert len(m.evidence_event_ids) == 2 and m.rule_version == P0B2A_RULE_VERSION
    placed = r.placed(w2)
    assert placed <= pool("step_4") and len(placed) == CAPACITY, \
        "selected ONLY from the new node's reviewed pool"
    bank_ids = {t.activity_template_id for t in BANK.templates_for_families(
        ["two_word_phrases"])}
    assert placed <= bank_ids, "reviewed bank content only"
    assert r.snapshot_doc(r.week_one) == week_one_before, "Week 1 is immutable"
    # The P0-B1 decision is still written, unchanged in schema.
    decision = r.repos.adaptation_decisions.find_for_cycle(w2.cycle_id)
    assert decision is not None and set(decision.selected_template_ids) == placed


def test_bridge3_too_hard_twice_moves_to_bridge2(monkeypatch):
    r = Recal(monkeypatch, start="step_4")
    r.twice(r.week_one, **TOO_HARD)
    w2 = r.release(W2)
    m = r.movement(w2)
    assert m.moved and m.direction == "down"
    assert (m.node_before_index, m.node_after_index) == (3, 2)
    assert m.pool_family_after == "expressive_vocabulary_growth"
    assert r.placed(w2) <= pool("step_3")


def test_up_then_down_chains_through_movement_records(monkeypatch):
    r = Recal(monkeypatch, start="step_3")
    r.twice(r.week_one, **TOO_EASY)
    w2 = r.release(W2)
    r.twice(w2, days=(W2, "2026-11-25"), **TOO_HARD)
    w3 = r.release(W3)
    m2, m3 = r.movement(w2), r.movement(w3)
    assert m3.node_before_ref == m2.node_after_ref
    assert (m3.node_before_index, m3.node_after_index) == (3, 2)
    assert r.placed(w3) <= pool("step_3")
    assert not set(m2.evidence_event_ids) & set(m3.evidence_event_ids)


# ===========================================================================
# 5. every refused transition — no move, clinician review, same pool
# ===========================================================================


@pytest.mark.parametrize("start,signal,reason", [
    ("step_2", TOO_EASY, MovementReason.TRANSITION_NOT_ENABLED),   # B1->B2
    ("step_3", TOO_HARD, MovementReason.TRANSITION_NOT_ENABLED),   # B2->B1
    ("step_4", TOO_EASY, MovementReason.TRANSITION_NOT_ENABLED),   # B3->end
    ("step_5", TOO_HARD, MovementReason.TRANSITION_NOT_ENABLED),   # end->B3
    ("step_2", TOO_HARD, MovementReason.TRANSITION_NOT_ENABLED),   # B1->18m
    ("step_5", TOO_EASY, MovementReason.OUTSIDE_ENVELOPE),         # above end
], ids=["b1up", "b2down", "b3up", "enddown", "b1down", "endup"])
def test_refused_transitions(monkeypatch, start, signal, reason):
    r = Recal(monkeypatch, start=start)
    r.twice(r.week_one, **signal)
    w2 = r.release(W2)
    m = r.movement(w2)
    assert m.outcome is MovementOutcome.REQUIRES_CLINICIAN_REVIEW
    assert m.reason is reason
    assert m.node_after_ref == m.node_before_ref == LADDER.by_step_key(start).node_ref
    assert len(m.evidence_event_ids) == 2
    assert r.placed(w2) <= pool(start), "stays in the same reviewed pool"
    # P0-B1 carry is unchanged: the signalled activity is offered again.
    assert r.placed(w2) & r.placed(r.week_one)
    # The clinician sees the exception in plain words.
    view = r.recal.start_view(r.provider, CHILD)
    assert view["placements"][0]["notes"]
    assert view["placements"][0]["current_step_key"] == start


def test_the_unreviewed_lower_endpoint_candidates_are_never_used(monkeypatch):
    """B1 + too_hard x2 points at the lower endpoint, which has only 7
    UNREVIEWED candidates: no move, and no template outside the bank."""
    r = Recal(monkeypatch, start="step_2")
    r.twice(r.week_one, **TOO_HARD)
    w2 = r.release(W2)
    all_bank = {t.activity_template_id for t in BANK.templates_for_families(
        BANK.served_families())}
    assert r.placed(w2) <= all_bank
    assert "expressive_first_words" not in json.dumps(
        [r.snapshot_doc(w2), [d for d in r.movement_docs()]])


# ===========================================================================
# 6. evidence threshold (P0-B1's, unchanged) and exceptions
# ===========================================================================


def _no_move(r, w2, reason=MovementReason.NO_TRIGGER):
    m = r.movement(w2)
    assert m.outcome is MovementOutcome.NO_MOVE and m.reason is reason, m
    assert m.node_after_index == m.node_before_index == 2
    assert r.placed(w2) <= pool("step_3")


def test_one_observation_does_not_move(monkeypatch):
    r = Recal(monkeypatch, start="step_3")
    r.observe(r.week_one, sorted(r.refs(r.week_one))[0], W1, **TOO_EASY)
    _no_move(r, r.release(W2))


def test_two_on_the_same_day_do_not_move(monkeypatch):
    r = Recal(monkeypatch, start="step_3")
    r.twice(r.week_one, days=(W1, W1), **TOO_EASY)
    _no_move(r, r.release(W2))


def test_a_superseded_pair_does_not_move(monkeypatch):
    r = Recal(monkeypatch, start="step_3")
    title = sorted(r.refs(r.week_one))[0]
    first = r.observe(r.week_one, title, W1, **TOO_EASY)
    r.observe(r.week_one, title, W1, difficulty=Difficulty.JUST_RIGHT,
              supersedes=first)
    r.observe(r.week_one, title, "2026-11-18", **TOO_EASY)
    _no_move(r, r.release(W2))


def test_two_activities_one_rating_each_do_not_pool(monkeypatch):
    r = Recal(monkeypatch, start="step_3")
    a, b = sorted(r.refs(r.week_one))[:2]
    r.observe(r.week_one, a, W1, **TOO_EASY)
    r.observe(r.week_one, b, "2026-11-18", **TOO_EASY)
    _no_move(r, r.release(W2))


def test_wasnt_ready_twice_is_a_clinician_exception(monkeypatch):
    r = Recal(monkeypatch, start="step_3")
    r.twice(r.week_one, **NOT_READY)
    m = r.movement(r.release(W2))
    assert m.outcome is MovementOutcome.REQUIRES_CLINICIAN_REVIEW
    assert m.reason is MovementReason.WASNT_READY and not m.moved


@pytest.mark.parametrize("second", [TOO_HARD, DIDNT], ids=["hard", "diseng"])
def test_conflicting_signals_are_a_clinician_exception(monkeypatch, second):
    r = Recal(monkeypatch, start="step_3")
    a, b = sorted(r.refs(r.week_one))[:2]
    r.twice(r.week_one, title=a, **TOO_EASY)
    r.twice(r.week_one, title=b, **second)
    w2 = r.release(W2)
    m = r.movement(w2)
    assert m.outcome is MovementOutcome.REQUIRES_CLINICIAN_REVIEW
    assert m.reason is MovementReason.CONFLICTING_SIGNALS
    assert len(m.evidence_event_ids) == 4
    assert r.placed(w2) <= pool("step_3")


def test_disengagement_stays_on_the_node_and_changes_theme(monkeypatch):
    r = Recal(monkeypatch, start="step_3")
    r.twice(r.week_one, **DIDNT)
    w2 = r.release(W2)
    _no_move(r, w2, MovementReason.DISENGAGEMENT_SAME_NODE)
    decision = r.repos.adaptation_decisions.find_for_cycle(w2.cycle_id)
    assert AdaptationBranch.CHANGE_THEME in decision.branches
    assert not r.placed(w2) & r.placed(r.week_one), "different cards"


# ===========================================================================
# 7. idempotency and resume
# ===========================================================================


def test_replay_never_moves_twice(monkeypatch):
    r = Recal(monkeypatch, start="step_3")
    r.twice(r.week_one, **TOO_EASY)
    w2 = r.release(W2)
    again = r.release(W2)
    assert again.created is False and again.cycle_id == w2.cycle_id
    assert len(r.movement_docs()) == 1
    assert r.recal.current_node(r.placement, None).step_key == "step_4"


def test_an_interrupted_release_resumes_with_the_same_movement(monkeypatch):
    r = Recal(monkeypatch, start="step_3")
    r.twice(r.week_one, **TOO_EASY)
    real = r.weekly.capture_snapshot

    def boom(*a, **k):
        raise RuntimeError("interrupted before the snapshot")

    r.weekly.capture_snapshot = boom
    with pytest.raises(RuntimeError):
        r.release(W2)
    (stored,) = r.movement_docs()
    r.weekly.capture_snapshot = real
    r.clock.set("2026-11-24")           # a later retry: stamp would differ
    w2 = r.release(W2)
    assert w2.created is True
    assert r.movement_docs() == [stored], "resume reused the stored movement"
    assert r.movement(w2).moved


def test_a_tampered_stored_movement_refuses_the_resume(monkeypatch):
    r = Recal(monkeypatch, start="step_3")
    r.twice(r.week_one, **TOO_EASY)
    real = r.weekly.capture_snapshot
    r.weekly.capture_snapshot = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("x"))
    with pytest.raises(RuntimeError):
        r.release(W2)
    r.weekly.capture_snapshot = real
    ((doc_id, doc),) = list(r.repos.store.list_all("pilot_developmental_movements"))
    r.repos.store.set("pilot_developmental_movements", doc_id,
                      {**doc, "evidence_event_ids": ["forged1", "forged2"]})
    with pytest.raises(AdaptiveWeekStateMismatch):
        r.release(W2)


# ===========================================================================
# 8. plans without placement keep P0-B1, with no inference or backfill
# ===========================================================================


def test_an_unplaced_plan_keeps_p0b1_exactly(monkeypatch):
    r = Recal(monkeypatch)              # ladder configured, NO placement
    assert set(r.refs(r.week_one)) == {"My Turn", "More Bubbles",
                                       "All Done and Stop"}
    r.twice(r.week_one, **TOO_EASY)
    w2 = r.release(W2)
    assert r.movement_docs() == [], "nothing inferred, nothing backfilled"
    assert list(r.repos.store.list_all("pilot_developmental_placements")) == []
    decision = r.repos.adaptation_decisions.find_for_cycle(w2.cycle_id)
    assert AdaptationBranch.NO_CHANGE in decision.branches


def test_a_placed_plan_without_a_ladder_fails_closed(monkeypatch):
    r = Recal(monkeypatch, start="step_3")
    service = WeekOneReleaseService(
        repos=r.repos, goals=r.goals, plans=r.plans, weekly=r.weekly,
        activity_bank=BANK, now=r.clock)          # no ladder
    r.clock.set(W2)
    with pytest.raises(ClinicalBoundaryRefused):
        service.release_week_one(r.provider, CHILD,
                                 family_declared_capacity=CAPACITY,
                                 cycle_month=MONTH, local_date=W2)


# ===========================================================================
# 9. Parent privacy
# ===========================================================================

PARENT_BANNED = ("node1:", "ldr1:", "rung1:", "step_", "moved", "move up",
                 "move down", "bridge", "adaptation_focus", "developmental",
                 "recalibrat", "month", "too_easy", "requires_clinician_review",
                 "placement", "envelope")


def test_the_parent_view_of_a_moved_week_carries_no_ladder_internals(monkeypatch):
    r = Recal(monkeypatch, start="step_3")
    r.twice(r.week_one, **TOO_EASY)
    r.release(W2)
    status, body = r.http("GET", f"/pilot/children/{CHILD}/this-week", token="cg")
    assert status == 200 and body["week"]
    blob = json.dumps(body).lower()
    for banned in PARENT_BANNED:
        assert banned not in blob, banned
    for n in LADDER.nodes[:4]:     # the bridge / lower-endpoint wordings
        assert n.wording.lower() not in blob
    # The frozen snapshot itself holds no node metadata either.
    snap = r.snapshot_doc(r.release(W2))
    for banned in ("node1:", "ldr1:", "developmental", "placement"):
        assert banned not in snap


def test_the_release_never_serves_a_step_without_a_reviewed_pool():
    """Defence in depth behind placement + the transition rule: even if a
    current node with NO reviewed pool were ever reached, the release refuses
    instead of falling back to other reviewed (or any unreviewed) content."""
    templates = {t.activity_template_id: t
                 for t in BANK.templates_for_families(BANK.served_families())}
    with pytest.raises(ClinicalBoundaryRefused):
        WeekOneReleaseService._node_pool(LADDER.nodes[0], templates)
    b3 = LADDER.by_step_key("step_4")
    assert set(WeekOneReleaseService._node_pool(b3, templates)) == pool("step_4")
    # A pool the bank no longer fully serves is refused, not shrunk.
    partial = {k: v for k, v in templates.items()
               if k != sorted(pool("step_4"))[0]}
    with pytest.raises(ClinicalBoundaryRefused):
        WeekOneReleaseService._node_pool(b3, partial)
