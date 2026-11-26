"""pilot_backend/domain/developmental_ladder.py — P0-B2a.

P0-B2a — constrained developmental recalibration. NOT full P0-B2.

The Pilot's five-node developmental neighbourhood, the bridge-baseline starting
rule, the one-node movement rule, and the three immutable records they write.
Pure: no store, no clock (except record stamps), no model, no network. No
workbook content lives here either — every node arrives from the generated,
drift-gated ladder artifact through a port.

## The envelope

    index 0   lower endpoint      (no reviewed pool today)
    index 1   Bridge 1            pool P
    index 2   Bridge 2            pool P        (same reviewed family as B1)
    index 3   Bridge 3            pool Q
    index 4   anchor endpoint     pool Q        (same reviewed family as B3)

Nothing exists outside it, so "outside the envelope" is not a direction this
module can express: `neighbour()` returns None and the move is refused.

## Which moves are automatic (the ONLY rule)

An adjacent pair is automatic iff BOTH nodes have a non-empty reviewed pool AND
the two pools are disjoint — a move must really change what the family
practises, and only into reviewed content. Today that is Bridge 2 <-> Bridge 3
alone. The rule is recomputed here from the artifact's pools and the artifact's
own list must agree, so neither can widen the other.

A later bridge-specific card mapping (disjoint `pool_template_ids` for B1/B2,
or for B3/endpoint, or reviewed cards for the lower endpoint) unlocks those
pairs by REGENERATING the artifact. `DevelopmentalMovement` is generic over
node refs and indexes, so no record schema changes when that happens.

## Movement never skips a node

`DevelopmentalMovement.__post_init__` refuses a MOVED record whose indexes are
not exactly one apart, and a non-moving record whose node changed. A move that
skips a node cannot be constructed, so it cannot be stored.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import (
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from .adaptation_decision import (
    ActivityAdaptationDecision,
    AdaptationBranch,
    DecisionReason,
)
from .entities import SCHEMA_VERSION, utc_now

LADDER_ARTIFACT_SCHEMA = "pilot-developmental-ladder-v1"
NODE_SCHEME = "node1:"
NODE_HASH_TAG = "genex-ladder-node-v1"
LADDER_SCHEME = "ldr1:"
LADDER_HASH_TAG = "genex-ladder-envelope-v1"

#: Recorded on every placement and movement. A new rule is a new version.
P0B2A_RULE_VERSION = "developmental-recalibration-rules-2026.11-p0b2a"

BRIDGE_BASELINE_PREFIX = "bbas"
DEVELOPMENTAL_PLACEMENT_PREFIX = "dplc"
DEVELOPMENTAL_MOVEMENT_PREFIX = "dmov"

#: The baseline prompt, shown with each bridge's own workbook wording.
BASELINE_PROMPT = "Right now, does this describe your child?"
ENVELOPE_SIZE = 5
QUESTION_COUNT = 3


class LadderError(ValueError):
    """A ladder, proposal or record that would be wrong. PHI-safe."""

    PHI_SAFE_MESSAGE = True


# ---------------------------------------------------------------------------
# The rule shared by the generator and the runtime
# ---------------------------------------------------------------------------


def enabled_transitions(nodes: Sequence[Tuple[str, Tuple[str, ...]]]
                        ) -> List[Tuple[str, str]]:
    """Directed adjacent pairs that may move automatically.

    `nodes` is `(node_ref, pool_template_ids)` in ladder order. A pair is
    enabled iff both pools are non-empty and disjoint. Adjacent only, by
    construction: there is no loop over non-neighbours.
    """
    out: List[Tuple[str, str]] = []
    for (a, pa), (b, pb) in zip(nodes, nodes[1:]):
        if pa and pb and not set(pa) & set(pb):
            out.append((a, b))
            out.append((b, a))
    return out


def ladder_ref_for(node_refs: Sequence[str],
                   _transitions: Iterable[Tuple[str, str]] = ()) -> str:
    """The envelope's identity: its ordered node refs, nothing else.

    Transitions are deliberately NOT part of it, so a later mapping that only
    unlocks a pair keeps every existing placement's envelope identity.
    """
    payload = "\x1f".join((LADDER_HASH_TAG, *node_refs))
    return LADDER_SCHEME + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


class Direction(str, Enum):
    UP = "up"
    DOWN = "down"


@dataclass(frozen=True)
class LadderNode:
    node_ref: str
    index: int
    node_type: str                      # "bridge" | "endpoint"
    step_number: Optional[int]
    wording: str
    rung_ref: str
    is_anchor_milestone: bool
    pool_family_ref: Optional[str]
    pool_template_ids: Tuple[str, ...]

    @property
    def has_reviewed_pool(self) -> bool:
        return bool(self.pool_family_ref) and bool(self.pool_template_ids)

    @property
    def step_key(self) -> str:
        """The opaque, human-safe key a UI uses. Never a hash, never an age."""
        return f"step_{self.index + 1}"


@dataclass(frozen=True)
class DevelopmentalLadder:
    ladder_ref: str
    anchor_rung_ref: str
    anchor_family_refs: Tuple[str, ...]
    nodes: Tuple[LadderNode, ...]
    question_node_refs: Tuple[str, ...]
    transitions: FrozenSet[Tuple[str, str]]

    def __post_init__(self) -> None:
        refs = [n.node_ref for n in self.nodes]
        if len(self.nodes) != ENVELOPE_SIZE:
            raise LadderError("the Pilot envelope has exactly five nodes")
        if [n.index for n in self.nodes] != list(range(ENVELOPE_SIZE)):
            raise LadderError("ladder indexes must be contiguous from zero")
        if len(set(refs)) != len(refs) or not all(
                r.startswith(NODE_SCHEME) for r in refs):
            raise LadderError("ladder node refs must be unique node1: refs")
        if self.ladder_ref != ladder_ref_for(refs):
            raise LadderError("the ladder ref does not match its nodes")
        recomputed = frozenset(enabled_transitions(
            [(n.node_ref, n.pool_template_ids) for n in self.nodes]))
        if frozenset(self.transitions) != recomputed:
            # The artifact may not enable anything the rule does not, and may
            # not silently disable something it does.
            raise LadderError("the ladder's transitions disagree with its rule")
        for a, b in self.transitions:
            if abs(self.index_of(a) - self.index_of(b)) != 1:
                raise LadderError("a transition must join adjacent nodes")
        for n in self.nodes:
            if n.has_reviewed_pool and n.pool_family_ref not in \
                    self.anchor_family_refs:
                raise LadderError("a node pool lies outside the anchor families")
        questions = [n.node_ref for n in self.nodes
                     if n.is_anchor_milestone and n.node_type == "bridge"]
        if tuple(questions) != tuple(self.question_node_refs) or \
                len(questions) != QUESTION_COUNT:
            raise LadderError("baseline questions must be the anchor's bridges")
        last = self.nodes[-1]
        if not (last.is_anchor_milestone and last.node_type == "endpoint"):
            raise LadderError("the envelope must end at the anchor endpoint")

    # -- lookups -----------------------------------------------------------

    def node(self, node_ref: str) -> LadderNode:
        for n in self.nodes:
            if n.node_ref == node_ref:
                return n
        raise LadderError("the node is outside the envelope")

    def by_step_key(self, step_key: str) -> LadderNode:
        for n in self.nodes:
            if n.step_key == step_key:
                return n
        raise LadderError("unknown step")

    def index_of(self, node_ref: str) -> int:
        return self.node(node_ref).index

    @property
    def node_refs(self) -> Tuple[str, ...]:
        return tuple(n.node_ref for n in self.nodes)

    @property
    def endpoint(self) -> LadderNode:
        return self.nodes[-1]

    def neighbour(self, node_ref: str, direction: Direction
                  ) -> Optional[LadderNode]:
        """The ONE adjacent node in that direction, or None at the edge."""
        i = self.index_of(node_ref) + (1 if direction is Direction.UP else -1)
        return self.nodes[i] if 0 <= i < len(self.nodes) else None

    def is_enabled(self, from_ref: str, to_ref: str) -> bool:
        return (from_ref, to_ref) in self.transitions

    def selectable_starts(self) -> Tuple[LadderNode, ...]:
        """Steps a clinician may choose: those with reviewed activities."""
        return tuple(n for n in self.nodes if n.has_reviewed_pool)


# ---------------------------------------------------------------------------
# Bridge baseline -> proposed starting node
# ---------------------------------------------------------------------------


class BridgeAnswer(str, Enum):
    YES = "yes"
    SOMETIMES = "sometimes"
    NOT_YET = "not_yet"
    NOT_SURE = "not_sure"


class BaselineStatus(str, Enum):
    CONSISTENT = "consistent"
    NEEDS_CLINICIAN_CONFIRMATION = "needs_clinician_confirmation"
    ABSENT = "absent"


class StartReason(str, Enum):
    STARTS_AT_BRIDGE = "starts_at_bridge"
    ALL_YES = "all_yes"
    NOT_SURE = "not_sure_at_or_before_start"
    OUT_OF_ORDER = "out_of_order"
    INCOMPLETE = "incomplete"
    NO_BASELINE = "no_baseline"


@dataclass(frozen=True)
class StartProposal:
    status: BaselineStatus
    reason: StartReason
    #: The proposed starting node, or None when a clinician must choose.
    node_ref: Optional[str]


def propose_start(ladder: DevelopmentalLadder,
                  answers: Optional[Sequence[Optional[BridgeAnswer]]]
                  ) -> StartProposal:
    """The founder's starting rule. Pure and total.

    Walk the three bridges in order. Yes continues; Sometimes or Not yet
    starts at that bridge; Not sure at or before the start, a missing answer
    before the start, or a Yes AFTER the start (out of order) needs clinician
    confirmation and proposes nothing. All three Yes -> the anchor endpoint.
    Answers after the start other than Yes do not matter.
    """
    if answers is None:
        return StartProposal(BaselineStatus.ABSENT, StartReason.NO_BASELINE,
                             None)
    if len(answers) != QUESTION_COUNT:
        raise LadderError("exactly three bridge answers are expected")
    needs = BaselineStatus.NEEDS_CLINICIAN_CONFIRMATION
    for i, answer in enumerate(answers):
        if answer is None:
            return StartProposal(needs, StartReason.INCOMPLETE, None)
        if answer is BridgeAnswer.NOT_SURE:
            return StartProposal(needs, StartReason.NOT_SURE, None)
        if answer is BridgeAnswer.YES:
            continue
        if any(later is BridgeAnswer.YES for later in answers[i + 1:]):
            return StartProposal(needs, StartReason.OUT_OF_ORDER, None)
        return StartProposal(BaselineStatus.CONSISTENT,
                             StartReason.STARTS_AT_BRIDGE,
                             ladder.question_node_refs[i])
    return StartProposal(BaselineStatus.CONSISTENT, StartReason.ALL_YES,
                         ladder.endpoint.node_ref)


# ---------------------------------------------------------------------------
# Movement evaluation (pure)
# ---------------------------------------------------------------------------


class MovementOutcome(str, Enum):
    MOVED = "moved"
    NO_MOVE = "no_move"
    REQUIRES_CLINICIAN_REVIEW = "requires_clinician_review"


class MovementReason(str, Enum):
    NO_TRIGGER = "no_trigger"
    DISENGAGEMENT_SAME_NODE = "disengagement_same_node"
    REPEATED_TOO_EASY = "repeated_too_easy"
    REPEATED_TOO_HARD = "repeated_too_hard"
    WASNT_READY = "wasnt_ready"
    CONFLICTING_SIGNALS = "conflicting_signals"
    TRANSITION_NOT_ENABLED = "transition_not_enabled"
    OUTSIDE_ENVELOPE = "outside_envelope"
    EVIDENCE_ALREADY_USED = "evidence_already_used"


_OUTCOME_REASONS = {
    MovementOutcome.MOVED: frozenset({MovementReason.REPEATED_TOO_EASY,
                                      MovementReason.REPEATED_TOO_HARD}),
    MovementOutcome.NO_MOVE: frozenset({
        MovementReason.NO_TRIGGER, MovementReason.DISENGAGEMENT_SAME_NODE}),
    MovementOutcome.REQUIRES_CLINICIAN_REVIEW: frozenset({
        MovementReason.WASNT_READY, MovementReason.CONFLICTING_SIGNALS,
        MovementReason.TRANSITION_NOT_ENABLED,
        MovementReason.OUTSIDE_ENVELOPE,
        MovementReason.EVIDENCE_ALREADY_USED}),
}


@dataclass(frozen=True)
class MovementEvaluation:
    outcome: MovementOutcome
    reason: MovementReason
    direction: str                      # "up" | "down" | ""
    node_before_ref: str
    node_after_ref: str
    evidence_event_ids: Tuple[str, ...]
    trigger_activity_refs: Tuple[str, ...]

    @property
    def moved(self) -> bool:
        return self.outcome is MovementOutcome.MOVED


def _pairs(decisions: Iterable[ActivityAdaptationDecision]) -> Tuple[
        Tuple[str, ...], Tuple[str, ...]]:
    ds = sorted(decisions, key=lambda d: d.activity_identity_ref)
    return (tuple(sorted({e for d in ds for e in d.trigger_event_ids})),
            tuple(d.activity_identity_ref for d in ds))


def evaluate_movement(ladder: DevelopmentalLadder, current_ref: str,
                      decisions: Sequence[ActivityAdaptationDecision], *,
                      consumed_event_ids: Iterable[str] = ()
                      ) -> MovementEvaluation:
    """P0-B1 decisions for last week -> at most ONE adjacent step.

    The evidence threshold is P0-B1's, unchanged and not re-implemented: a
    NOT_YET_SUPPORTED decision exists only for the last two CURRENT
    observations of ONE activity on DIFFERENT practice dates, never pooled
    across activities, with superseded rows excluded. So one rating, two on
    one day, a superseded pair, or two activities with one rating each never
    reach here as a trigger.
    """
    ladder.node(current_ref)  # refuses a node outside the envelope
    up = [d for d in decisions
          if d.reason is DecisionReason.NOT_YET_SUPPORTED_REPEATED_TOO_EASY]
    down = [d for d in decisions
            if d.reason is DecisionReason.NOT_YET_SUPPORTED_REPEATED_TOO_HARD]
    not_ready = [d for d in decisions if d.reason
                 is DecisionReason.NOT_YET_SUPPORTED_REPEATED_WASNT_READY]
    disengaged = [d for d in decisions
                  if d.branch is AdaptationBranch.CHANGE_THEME]

    def stay(outcome, reason, triggers=(), direction=""):
        ev, acts = _pairs(triggers)
        return MovementEvaluation(outcome, reason, direction, current_ref,
                                  current_ref, ev, acts)

    review = MovementOutcome.REQUIRES_CLINICIAN_REVIEW
    if not_ready:
        return stay(review, MovementReason.WASNT_READY,
                    not_ready + up + down)
    if (up and down) or ((up or down) and disengaged):
        return stay(review, MovementReason.CONFLICTING_SIGNALS,
                    up + down + disengaged)
    if not up and not down:
        if disengaged:
            return stay(MovementOutcome.NO_MOVE,
                        MovementReason.DISENGAGEMENT_SAME_NODE)
        return stay(MovementOutcome.NO_MOVE, MovementReason.NO_TRIGGER)

    direction = Direction.UP if up else Direction.DOWN
    triggers = up or down
    reason = (MovementReason.REPEATED_TOO_EASY if up
              else MovementReason.REPEATED_TOO_HARD)
    evidence, activities = _pairs(triggers)
    if set(evidence) & set(consumed_event_ids):
        return stay(review, MovementReason.EVIDENCE_ALREADY_USED, triggers,
                    direction.value)
    target = ladder.neighbour(current_ref, direction)
    if target is None:
        return stay(review, MovementReason.OUTSIDE_ENVELOPE, triggers,
                    direction.value)
    if not ladder.is_enabled(current_ref, target.node_ref):
        return stay(review, MovementReason.TRANSITION_NOT_ENABLED, triggers,
                    direction.value)
    return MovementEvaluation(MovementOutcome.MOVED, reason, direction.value,
                              current_ref, target.node_ref, evidence,
                              activities)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def _derived(prefix: str, *parts: str) -> str:
    if not all((p or "").strip() for p in parts):
        raise LadderError("a derived id needs every part")
    digest = hashlib.sha256("|".join((prefix, *parts)).encode("utf-8"))
    return f"{prefix}_{digest.hexdigest()[:32]}"


def developmental_placement_id(focus_plan_id: str) -> str:
    """One placement per clinical plan, ever (create-only on a derived id)."""
    return _derived(DEVELOPMENTAL_PLACEMENT_PREFIX, focus_plan_id)


def developmental_movement_id(to_cycle_id: str) -> str:
    """One movement record per adaptive week, ever."""
    return _derived(DEVELOPMENTAL_MOVEMENT_PREFIX, to_cycle_id)


@dataclass(frozen=True)
class BridgeBaselineAnswers:
    """A caregiver's answers to the three bridge questions. Append-only."""

    answers_id: str
    child_id: str
    ladder_ref: str
    question_node_refs: Tuple[str, ...]
    #: Aligned to `question_node_refs`; "" = unanswered.
    answers: Tuple[str, ...]
    submitted_by_actor_id: str
    created_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "question_node_refs",
                           tuple(self.question_node_refs))
        object.__setattr__(self, "answers", tuple(self.answers))
        if len(self.question_node_refs) != QUESTION_COUNT or \
                len(self.answers) != QUESTION_COUNT:
            raise LadderError("a baseline holds exactly three answers")
        valid = {a.value for a in BridgeAnswer} | {""}
        if not set(self.answers) <= valid:
            raise LadderError("an answer is not Yes/Sometimes/Not yet/Not sure")
        if not self.answers_id.startswith(BRIDGE_BASELINE_PREFIX + "_"):
            raise LadderError("malformed baseline id")

    def parsed(self) -> Tuple[Optional[BridgeAnswer], ...]:
        return tuple(BridgeAnswer(a) if a else None for a in self.answers)

    @staticmethod
    def create(*, child_id: str, ladder: DevelopmentalLadder,
               answers: Sequence[Optional[BridgeAnswer]], actor_id: str,
               now: Optional[datetime] = None) -> "BridgeBaselineAnswers":
        return BridgeBaselineAnswers(
            answers_id=f"{BRIDGE_BASELINE_PREFIX}_{uuid.uuid4().hex}",
            child_id=child_id, ladder_ref=ladder.ladder_ref,
            question_node_refs=ladder.question_node_refs,
            answers=tuple(a.value if a else "" for a in answers),
            submitted_by_actor_id=actor_id, created_at=now or utc_now())


class PlacementSource(str, Enum):
    #: The clinician confirmed the consistent baseline's proposal.
    CONFIRMED_BASELINE_PROPOSAL = "confirmed_baseline_proposal"
    #: The clinician chose the step (no, inconsistent, or overridden baseline).
    CLINICIAN_CHOICE = "clinician_choice"


@dataclass(frozen=True)
class DevelopmentalPlacement:
    """The clinician-confirmed starting step and envelope for ONE plan."""

    placement_id: str
    child_id: str
    focus_plan_id: str
    goal_id: str
    ladder_ref: str
    envelope_node_refs: Tuple[str, ...]
    starting_node_ref: str
    starting_node_index: int
    proposed_node_ref: str              # "" when the baseline proposed none
    baseline_answers_id: str            # "" when there was no baseline
    baseline_status: BaselineStatus
    source: PlacementSource
    rule_version: str
    placed_by_actor_id: str
    created_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "envelope_node_refs",
                           tuple(self.envelope_node_refs))
        if self.placement_id != developmental_placement_id(self.focus_plan_id):
            raise LadderError("placement id must derive from its plan")
        if self.rule_version != P0B2A_RULE_VERSION:
            raise LadderError("unexpected recalibration rule version")
        if len(self.envelope_node_refs) != ENVELOPE_SIZE:
            raise LadderError("a placement records the five-node envelope")
        if self.ladder_ref != ladder_ref_for(self.envelope_node_refs):
            raise LadderError("the envelope does not match its ladder ref")
        if self.envelope_node_refs[self.starting_node_index] != \
                self.starting_node_ref:
            raise LadderError("the starting node is not at its index")
        if self.source is PlacementSource.CONFIRMED_BASELINE_PROPOSAL and not (
                self.baseline_status is BaselineStatus.CONSISTENT
                and self.proposed_node_ref == self.starting_node_ref):
            raise LadderError(
                "only a consistent baseline's own proposal can be confirmed")
        if self.proposed_node_ref and \
                self.proposed_node_ref not in self.envelope_node_refs:
            raise LadderError("the proposal lies outside the envelope")


@dataclass(frozen=True)
class DevelopmentalMovement:
    """Why ONE adaptive week sits at its node. Immutable, create-only.

    Written for EVERY adaptive week of a placed plan — moved or not — in the
    same transaction as the P0-B1 decision, so the current node is a fold over
    these records and a resume can verify each one.
    """

    movement_id: str
    child_id: str
    focus_plan_id: str
    placement_id: str
    from_cycle_id: str
    to_cycle_id: str
    rule_version: str
    ladder_ref: str
    goal_id: str
    outcome: MovementOutcome
    reason: MovementReason
    direction: str
    node_before_ref: str
    node_before_index: int
    node_after_ref: str
    node_after_index: int
    pool_family_before: str
    pool_family_after: str
    evidence_event_ids: Tuple[str, ...]
    trigger_activity_refs: Tuple[str, ...]
    created_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("evidence_event_ids", "trigger_activity_refs"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if self.movement_id != developmental_movement_id(self.to_cycle_id):
            raise LadderError("movement id must derive from its cycle")
        if self.from_cycle_id == self.to_cycle_id:
            raise LadderError("a movement links two different cycles")
        if self.rule_version != P0B2A_RULE_VERSION:
            raise LadderError("unexpected recalibration rule version")
        if self.reason not in _OUTCOME_REASONS[self.outcome]:
            raise LadderError("reason is not valid for the outcome")
        step = self.node_after_index - self.node_before_index
        if self.outcome is MovementOutcome.MOVED:
            # THE no-skip guard: exactly one step, in the recorded direction.
            if abs(step) != 1 or self.node_after_ref == self.node_before_ref:
                raise LadderError("a move must be exactly one adjacent step")
            want = "up" if step > 0 else "down"
            if self.direction != want:
                raise LadderError("the direction disagrees with the step")
            if (self.reason is MovementReason.REPEATED_TOO_EASY) != (step > 0):
                raise LadderError("too easy moves up; too hard moves down")
            if len(self.evidence_event_ids) < 2:
                raise LadderError("a move cites at least two observations")
        else:
            if step != 0 or self.node_after_ref != self.node_before_ref:
                raise LadderError("a non-move must keep the node")

    @property
    def moved(self) -> bool:
        return self.outcome is MovementOutcome.MOVED

    @staticmethod
    def create(*, placement: DevelopmentalPlacement,
               ladder: DevelopmentalLadder, from_cycle_id: str,
               to_cycle_id: str, evaluation: MovementEvaluation,
               now: Optional[datetime] = None) -> "DevelopmentalMovement":
        before = ladder.node(evaluation.node_before_ref)
        after = ladder.node(evaluation.node_after_ref)
        return DevelopmentalMovement(
            movement_id=developmental_movement_id(to_cycle_id),
            child_id=placement.child_id,
            focus_plan_id=placement.focus_plan_id,
            placement_id=placement.placement_id,
            from_cycle_id=from_cycle_id, to_cycle_id=to_cycle_id,
            rule_version=P0B2A_RULE_VERSION, ladder_ref=ladder.ladder_ref,
            goal_id=placement.goal_id, outcome=evaluation.outcome,
            reason=evaluation.reason, direction=evaluation.direction,
            node_before_ref=before.node_ref, node_before_index=before.index,
            node_after_ref=after.node_ref, node_after_index=after.index,
            pool_family_before=before.pool_family_ref or "",
            pool_family_after=after.pool_family_ref or "",
            evidence_event_ids=evaluation.evidence_event_ids,
            trigger_activity_refs=evaluation.trigger_activity_refs,
            created_at=now or utc_now())


def ladder_from_artifact(body: Mapping) -> DevelopmentalLadder:
    """Build (and so fully validate) a ladder from an artifact body."""
    try:
        nodes = tuple(LadderNode(
            node_ref=str(n["node_ref"]), index=int(n["index"]),
            node_type=str(n["node_type"]),
            step_number=(None if n["step_number"] is None
                         else int(n["step_number"])),
            wording=str(n["wording"]), rung_ref=str(n["rung_ref"]),
            is_anchor_milestone=bool(n["is_anchor_milestone"]),
            pool_family_ref=n["pool_family_ref"] or None,
            pool_template_ids=tuple(n["pool_template_ids"]))
            for n in body["nodes"])
        return DevelopmentalLadder(
            ladder_ref=str(body["ladder_ref"]),
            anchor_rung_ref=str(body["anchor_rung_ref"]),
            anchor_family_refs=tuple(body["anchor_family_refs"]),
            nodes=nodes,
            question_node_refs=tuple(body["baseline_question_node_refs"]),
            transitions=frozenset((t["from"], t["to"])
                                  for t in body["enabled_transitions"]))
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, LadderError):
            raise
        raise LadderError("the ladder artifact is malformed") from exc


__all__ = [
    "BASELINE_PROMPT",
    "BaselineStatus",
    "BridgeAnswer",
    "BridgeBaselineAnswers",
    "DevelopmentalLadder",
    "DevelopmentalMovement",
    "DevelopmentalPlacement",
    "Direction",
    "LADDER_ARTIFACT_SCHEMA",
    "LadderError",
    "LadderNode",
    "MovementEvaluation",
    "MovementOutcome",
    "MovementReason",
    "P0B2A_RULE_VERSION",
    "PlacementSource",
    "StartProposal",
    "StartReason",
    "developmental_movement_id",
    "developmental_placement_id",
    "enabled_transitions",
    "evaluate_movement",
    "ladder_from_artifact",
    "ladder_ref_for",
    "propose_start",
]
