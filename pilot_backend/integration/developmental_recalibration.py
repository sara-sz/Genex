"""pilot_backend/integration/developmental_recalibration.py — P0-B2a.

P0-B2a — constrained developmental recalibration (NOT full P0-B2).

Three provider/caregiver operations around the five-node ladder, plus the
read the release composition uses to find a plan's CURRENT node:

    caregiver  baseline_questions / submit_baseline   the three bridge items
    provider   start_view                             proposal + envelope
    provider   place                                  confirm / choose a step
    release    current_node                           placement folded over
                                                      the plan's movements

## What each audience may see

The caregiver sees each bridge's own workbook wording, the prompt, and the
four answers — no step number, no node ref, no age, nothing back but "recorded".
The clinician sees the five steps in behaviour wording, which ones have
reviewed activities, which pair may move automatically, the proposal and
whether the baseline was consistent — never a card mapping, taxonomy id or
hash (`step_key` is `step_1`..`step_5`).

## Plans without a placement

Keep P0-B1 behaviour exactly. Nothing here infers or backfills a node for an
existing plan or child.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..domain.developmental_ladder import (
    BASELINE_PROMPT,
    P0B2A_RULE_VERSION,
    BaselineStatus,
    BridgeAnswer,
    BridgeBaselineAnswers,
    DevelopmentalLadder,
    DevelopmentalPlacement,
    LadderError,
    LadderNode,
    MovementOutcome,
    ProposalBasis,
    StartReason,
    clinician_review_item,
    developmental_placement_id,
    placement_source_for,
    propose_placement,
    propose_start,
)
from ..domain.goals import GoalKind, GoalRef, GoalStatus, SuggestionStatus
from ..domain.monthly_plan import MonthlyPlanState
from ..domain.roles import ActorRole
from ..repository.interface import DuplicateRecord


class RecalibrationError(Exception):
    """A P0-B2a operation was refused. PHI-safe; rendered as the constant 403."""

    PHI_SAFE_MESSAGE = True


class LadderNotApplicable(RecalibrationError):
    """No single approved goal targets the ladder's anchor milestone."""


class PlacementRefused(RecalibrationError):
    """The plan cannot take (or change) a developmental placement."""


class PlacementStateMismatch(RecalibrationError):
    """A placed plan's movement history is inconsistent. Fail closed."""


#: Clinician-facing wording for each baseline status / reason. Plain words:
#: no ages, no node refs.
_BASELINE_TEXT = {
    StartReason.STARTS_AT_BRIDGE: "Consistent caregiver answers",
    StartReason.ALL_YES: "Consistent caregiver answers (all three: Yes)",
    StartReason.NOT_SURE: "Caregiver was not sure — please confirm a step",
    StartReason.OUT_OF_ORDER: "Answers were out of order — please confirm a step",
    StartReason.INCOMPLETE: "Answers were incomplete — please confirm a step",
    StartReason.NO_BASELINE: "No caregiver answers yet — please choose a step",
}

_REVIEW_TEXT = {
    "wasnt_ready": "Caregiver said twice the child wasn't ready — "
                   "no automatic change; please review",
    "conflicting_signals": "Mixed feedback (easier and harder, or "
                           "disengagement) — no automatic change; please review",
    "transition_not_enabled": "Feedback suggests a step change that is not "
                              "automatic yet — please review",
    "outside_envelope": "Feedback points beyond the planned steps — "
                        "please review",
    "evidence_already_used": "This feedback was already used — please review",
}


@dataclass(frozen=True)
class _LadderGoal:
    goal_id: str
    ref: GoalRef


class DevelopmentalRecalibrationService:

    def __init__(self, *, repos: Any, goals: Any, plans: Any, weekly: Any,
                 ladder: Optional[DevelopmentalLadder], now: Any = None
                 ) -> None:
        from ..domain.entities import utc_now  # noqa: WPS433

        self._repos = repos
        self._goals = goals
        self._plans = plans
        self._weekly = weekly
        self._ladder = ladder
        self._now = now or utc_now

    # -- shared ------------------------------------------------------------

    def _require_ladder(self) -> DevelopmentalLadder:
        if self._ladder is None:
            raise LadderNotApplicable("no developmental ladder is configured")
        return self._ladder

    def ladder_goal(self, principal, child_id: str) -> _LadderGoal:
        """The ONE approved, anchored goal whose anchor is the ladder's.

        P0-B2a is single-goal: a child with any OTHER releasable goal is
        refused rather than having one goal's step move another goal's week.
        """
        ladder = self._require_ladder()
        found: List[_LadderGoal] = []
        others = 0
        for goal in self._goals.list_clinical_goals(principal, child_id):
            if goal.status is not GoalStatus.ACTIVE or \
                    not (goal.current_version_id or "").strip():
                continue
            ref = GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id)
            anchor = self._goals.goal_anchor(principal, ref)
            if anchor is None or not anchor.is_activity_mappable:
                continue
            if anchor.rung.rung_ref == ladder.anchor_rung_ref:
                bound = {b.family_ref for b in anchor.rung.family_bindings}
                if not set(ladder.anchor_family_refs) <= bound:
                    raise LadderNotApplicable(
                        "the goal's anchor does not bind the ladder families")
                found.append(_LadderGoal(goal.clinical_goal_id, ref))
            else:
                others += 1
        if len(found) != 1 or others:
            raise LadderNotApplicable(
                "developmental placement needs exactly one approved goal at "
                "the ladder's milestone")
        return found[0]

    # -- caregiver ---------------------------------------------------------

    def bridge_applicable(self, child_id: str) -> bool:
        """Do the bridge questions apply to this child? Deterministic.

        The child's TARGET set is the canonical anchors of its active approved
        clinical goals or, when it has none yet, of its currently OFFERED goal
        suggestions. The questions apply iff that set is exactly {the ladder's
        anchor}. Anything else — no target, an unanchored target, another
        milestone, or this milestone plus another — is not this Pilot ladder,
        and the questions are never asked. Callers authorize first.
        """
        ladder = self._ladder
        if ladder is None:
            return False
        goals = [g for g in self._repos.clinical_goals.list_for_child(child_id)
                 if g.status is GoalStatus.ACTIVE
                 and (g.current_version_id or "").strip()]
        anchors = set()
        if goals:
            for g in goals:
                anchor = self._repos.clinical_goal_anchors.find(
                    g.clinical_goal_id)
                anchors.add(anchor.rung.rung_ref if anchor else None)
        else:
            for sug in self._repos.goal_suggestions.list_for_child(child_id):
                if sug.status is not SuggestionStatus.OFFERED:
                    continue
                anchor = self._repos.suggestion_anchors.find(
                    sug.suggestion_id)
                anchors.add(anchor.rung.rung_ref if anchor else None)
        return anchors == {ladder.anchor_rung_ref}

    def baseline_questions(self, principal, child_id: str) -> Dict[str, Any]:
        ladder = self._require_ladder()
        if principal.role is not ActorRole.CAREGIVER:
            raise RecalibrationError("the bridge questions are the family's")
        self._goals._authorize(principal, child_id)
        if not self.bridge_applicable(child_id):
            # The ONLY thing a caregiver learns about a non-matching child.
            return {"applicable": False}
        latest = self._latest_answers(child_id, ladder)
        return {
            "applicable": True,
            "submitted": latest is not None,
            "prompt": BASELINE_PROMPT,
            "options": [{"value": a.value, "label": label} for a, label in (
                (BridgeAnswer.YES, "Yes"),
                (BridgeAnswer.SOMETIMES, "Sometimes"),
                (BridgeAnswer.NOT_YET, "Not yet"),
                (BridgeAnswer.NOT_SURE, "Not sure"))],
            "questions": [{"key": f"q{i}", "text": ladder.node(ref).wording}
                          for i, ref in enumerate(
                              ladder.question_node_refs, start=1)],
        }

    def submit_baseline(self, principal, child_id: str,
                        answers: Sequence[Optional[str]]
                        ) -> BridgeBaselineAnswers:
        ladder = self._require_ladder()
        if principal.role is not ActorRole.CAREGIVER:
            raise RecalibrationError("only a caregiver answers the baseline")
        self._goals._authorize(principal, child_id)
        if not self.bridge_applicable(child_id):
            raise RecalibrationError("the bridge questions do not apply")
        if len(answers) != 3:
            raise RecalibrationError("exactly three answers are expected")
        try:
            parsed = tuple(BridgeAnswer(a) if a else None for a in answers)
        except ValueError:
            raise RecalibrationError("an answer is not recognised") from None
        record = BridgeBaselineAnswers.create(
            child_id=child_id, ladder=ladder, answers=parsed,
            actor_id=principal.application_id, now=self._now())
        return self._repos.bridge_baseline_answers.create(record)

    # -- provider ----------------------------------------------------------

    def _latest_answers(self, child_id: str, ladder: DevelopmentalLadder):
        latest = self._repos.bridge_baseline_answers.latest_for_child(child_id)
        if latest is None or latest.ladder_ref != ladder.ladder_ref or \
                tuple(latest.question_node_refs) != ladder.question_node_refs:
            # Answers to a different envelope are not evidence for this one.
            return None
        return latest

    def _require_provider(self, principal, child_id: str) -> None:
        if principal.role is not ActorRole.PROVIDER:
            raise RecalibrationError("only the managing clinician places")
        self._goals._authorize(principal, child_id)
        self._goals._require_managing_clinician(principal, child_id)

    @staticmethod
    def _step(node: LadderNode) -> Dict[str, Any]:
        return {"step_key": node.step_key, "wording": node.wording,
                "has_reviewed_activities": node.has_reviewed_pool,
                "selectable": node.has_reviewed_pool}

    def start_view(self, principal, child_id: str) -> Dict[str, Any]:
        """What Therapist Dev plan setup shows. No refs, families or ages."""
        ladder = self._require_ladder()
        self._require_provider(principal, child_id)
        self.ladder_goal(principal, child_id)
        latest = self._latest_answers(child_id, ladder)
        previous = self._previous_placement(child_id, excluding_plan_id="")
        proposal = propose_placement(
            ladder, baseline_answers=latest.parsed() if latest else None,
            previous_final_node_ref=(self.current_node(previous, None).node_ref
                                     if previous else None))
        automatic = sorted({tuple(sorted((ladder.node(a).index,
                                          ladder.node(b).index)))
                            for a, b in ladder.transitions})
        placements = []
        for plan in self._plans.list_plans(principal, child_id):
            placement = self._repos.developmental_placements.find_for_plan(
                plan.focus_plan_id)
            if placement is None:
                continue
            current = self.current_node(placement, None)
            placements.append({
                "focus_plan_id": plan.focus_plan_id,
                "cycle_month": plan.cycle_month,
                "starting_step_key": ladder.node(
                    placement.starting_node_ref).step_key,
                "current_step_key": current.step_key,
                "source": placement.source.value,
                "notes": self._review_notes(placement),
                "review_items": self._review_items(placement)})
        return {
            "steps": [self._step(n) for n in ladder.nodes],
            "automatic_moves": [
                {"between": [ladder.nodes[i].step_key,
                             ladder.nodes[j].step_key]}
                for i, j in automatic],
            "proposal_basis": proposal.basis.value,
            "baseline": {
                "status": proposal.baseline.status.value,
                "summary": _BASELINE_TEXT[proposal.baseline.reason],
                "answers": ([{"question": ladder.node(ref).wording,
                              "answer": ans or None}
                             for ref, ans in zip(latest.question_node_refs,
                                                 latest.answers)]
                            if latest else []),
            },
            "proposed_step_key": (ladder.node(proposal.node_ref).step_key
                                  if proposal.node_ref else None),
            "placements": placements,
        }

    def _review_notes(self, placement) -> List[Dict[str, str]]:
        notes = []
        for m in self._repos.developmental_movements.list_for_placement(
                placement.placement_id):
            if m.outcome is MovementOutcome.REQUIRES_CLINICIAN_REVIEW:
                cycle = self._repos.weekly_cycles.get_by_id(m.to_cycle_id)
                notes.append({"week_starts_on": cycle.starts_on,
                              "note": _REVIEW_TEXT[m.reason.value]})
        return sorted(notes, key=lambda n: n["week_starts_on"])

    def _review_items(self, placement) -> List[Dict[str, Any]]:
        """Provider-only structured review items (no free text, no family)."""
        ladder = self._require_ladder()
        items = []
        for m in self._repos.developmental_movements.list_for_placement(
                placement.placement_id):
            item = clinician_review_item(m, ladder)
            if item is None:
                continue
            items.append({
                "movement_id": item.movement_id,
                "cycle_id": item.to_cycle_id,
                "current_step_key": item.current_step_key,
                "reason": item.reason.value,
                "attempted_direction": item.attempted_direction or None,
                "evidence_event_ids": list(item.evidence_event_ids),
            })
        return items

    def _previous_placement(self, child_id: str, *, excluding_plan_id: str):
        """The child's most recent placement on ANOTHER plan, or None."""
        rows = [p for p in self._repos.developmental_placements.list_for_child(
            child_id) if p.focus_plan_id != excluding_plan_id]
        return rows[-1] if rows else None

    def place(self, principal, focus_plan_id: str, step_key: str
              ) -> DevelopmentalPlacement:
        """Hannah confirms the proposal or chooses another allowed step.

        Draft plans only, before any week exists, so no released (or even
        drafted) week can ever be re-interpreted. Create-only on a derived id:
        an identical retry returns the stored placement; a different step for
        an already placed plan is refused, never overwritten.
        """
        ladder = self._require_ladder()
        plan = self._plans.get_plan(principal, focus_plan_id)
        self._require_provider(principal, plan.child_id)
        goal = self.ladder_goal(principal, plan.child_id)
        try:
            node = ladder.by_step_key(step_key)
        except LadderError:
            raise PlacementRefused("unknown step") from None
        if not node.has_reviewed_pool:
            raise PlacementRefused("that step has no reviewed activities yet")

        existing = self._repos.developmental_placements.find_for_plan(
            focus_plan_id)
        if existing is not None:
            if existing.starting_node_ref == node.node_ref:
                return existing
            raise PlacementRefused("this plan's starting step is already set")
        if plan.state is not MonthlyPlanState.DRAFT:
            raise PlacementRefused("a starting step is set before activation")
        if self._weekly.list_cycles(principal, focus_plan_id):
            raise PlacementRefused("a plan with weeks cannot be placed")

        latest = self._latest_answers(plan.child_id, ladder)
        previous = self._previous_placement(plan.child_id,
                                            excluding_plan_id=focus_plan_id)
        proposal = propose_placement(
            ladder, baseline_answers=latest.parsed() if latest else None,
            previous_final_node_ref=(self.current_node(previous, None).node_ref
                                     if previous else None))
        placement = DevelopmentalPlacement(
            placement_id=developmental_placement_id(focus_plan_id),
            child_id=plan.child_id, focus_plan_id=focus_plan_id,
            goal_id=goal.goal_id, ladder_ref=ladder.ladder_ref,
            envelope_node_refs=ladder.node_refs,
            starting_node_ref=node.node_ref, starting_node_index=node.index,
            proposed_node_ref=proposal.node_ref or "",
            baseline_answers_id=latest.answers_id if latest else "",
            baseline_status=proposal.baseline.status,
            source=placement_source_for(proposal, node.node_ref),
            proposal_basis=proposal.basis,
            previous_placement_id=(previous.placement_id
                                   if proposal.basis is ProposalBasis.PREVIOUS_PLAN
                                   else ""),
            rule_version=P0B2A_RULE_VERSION,
            placed_by_actor_id=principal.application_id,
            created_at=self._now())
        event = placement_audit_event(placement, principal)

        def _write(store):
            # Placement AND its audit event, atomically, with NO read inside
            # the transaction. Both documents are CREATE-ONLY under ids derived
            # from the plan, so exactly one commit can win; a replay or an
            # identical racing request collides instead of writing a second
            # row or a second event. (A read-then-write here deadlocked every
            # concurrent writer on the emulator's pessimistic locks.)
            tx = type(self._repos)(store)
            tx.developmental_placements.create(placement)
            tx.audit_events.append(event)

        try:
            self._repos.store.run_in_transaction(_write)
            return placement
        except Exception:  # noqa: BLE001 - resolved by reading the outcome
            raced = self._repos.developmental_placements.find_for_plan(
                focus_plan_id)
            if raced is None:
                # Nothing was saved (e.g. contention). PHI-safe refusal; an
                # identical retry is safe.
                raise PlacementRefused(
                    "the starting step could not be saved; try again") from None
            if raced.starting_node_ref == node.node_ref:
                return raced
            raise PlacementRefused(
                "this plan's starting step is already set") from None

    # -- the release composition's read -------------------------------------

    def verified_placement(self, plan, goal_ids: Sequence[str]
                           ) -> Optional[DevelopmentalPlacement]:
        """The plan's placement, verified against the loaded ladder, or None.

        None means P0-B1 behaviour, unchanged. A placement that no longer
        matches the configured envelope, or a plan whose goals are not the
        placed single goal, fails closed.
        """
        placement = self._repos.developmental_placements.find_for_plan(
            plan.focus_plan_id)
        if placement is None:
            return None
        ladder = self._require_ladder()
        if placement.ladder_ref != ladder.ladder_ref or \
                tuple(placement.envelope_node_refs) != ladder.node_refs:
            raise PlacementStateMismatch(
                "the plan's envelope is not the configured ladder")
        if list(goal_ids) != [placement.goal_id]:
            raise PlacementStateMismatch(
                "a placed plan must release exactly its placed goal")
        return placement

    def current_node(self, placement, before_sequence: Optional[int]
                     ) -> LadderNode:
        """The placement's start folded over the plan's movement records.

        `before_sequence` limits the fold to weeks before it (None = all).
        Every adaptive week of a placed plan has a movement record, written in
        its decision's transaction; a missing or discontinuous one refuses.
        """
        ladder = self._require_ladder()
        node = ladder.node(placement.starting_node_ref)
        cycles = sorted(self._repos.weekly_cycles.list_for_plan(
            placement.focus_plan_id), key=lambda c: c.sequence_in_month)
        for cycle in cycles:
            if cycle.sequence_in_month <= 1:
                continue
            if before_sequence is not None and \
                    cycle.sequence_in_month >= before_sequence:
                break
            movement = self._repos.developmental_movements.find_for_cycle(
                cycle.cycle_id)
            if movement is None:
                if cycle.is_released:
                    raise PlacementStateMismatch(
                        "a placed plan's week has no developmental record")
                continue
            if movement.node_before_ref != node.node_ref or \
                    movement.placement_id != placement.placement_id:
                raise PlacementStateMismatch(
                    "a developmental record does not follow its predecessor")
            node = ladder.node(movement.node_after_ref)
        return node

    def consumed_evidence(self, placement, *, excluding_cycle_id: str
                          ) -> Tuple[str, ...]:
        """Every observation an EARLIER move already used."""
        return tuple(sorted({
            e for m in self._repos.developmental_movements.list_for_placement(
                placement.placement_id)
            if m.moved and m.to_cycle_id != excluding_cycle_id
            for e in m.evidence_event_ids}))


def placement_audit_event_id(placement_id: str) -> str:
    import hashlib  # noqa: WPS433
    digest = hashlib.sha256(
        f"developmental_placement_audit|{placement_id}".encode("utf-8"))
    return f"audt_{digest.hexdigest()[:32]}"


def placement_audit_event(placement: DevelopmentalPlacement, principal):
    """The ONE audit event for a first placement. Opaque provenance only."""
    from dataclasses import replace  # noqa: WPS433

    from ..audit.events import AuditAction, AuditEvent, AuditResult  # noqa

    event = AuditEvent.build(
        AuditAction.DEVELOPMENTAL_PLACEMENT_CONFIRMED, AuditResult.SUCCESS,
        "developmental_placement", resource_id=placement.placement_id,
        child_id=placement.child_id,
        actor_application_id=principal.application_id,
        actor_auth_subject=getattr(principal, "auth_subject", None),
        actor_role=principal.role,
        metadata={
            "placement_id": placement.placement_id,
            "focus_plan_id": placement.focus_plan_id,
            "goal_id": placement.goal_id,
            "ladder_ref": placement.ladder_ref,
            "rule_version": placement.rule_version,
            "starting_node_ref": placement.starting_node_ref,
            "starting_node_index": placement.starting_node_index,
            "proposed_node_ref": placement.proposed_node_ref,
            "placement_source": placement.source.value,
            "proposal_basis": placement.proposal_basis.value,
            "baseline_status": placement.baseline_status.value,
            "previous_placement_id": placement.previous_placement_id,
        },
        now=placement.created_at)
    return replace(event, event_id=placement_audit_event_id(
        placement.placement_id))


__all__ = [
    "DevelopmentalRecalibrationService",
    "placement_audit_event",
    "placement_audit_event_id",
    "LadderNotApplicable",
    "PlacementRefused",
    "PlacementStateMismatch",
    "RecalibrationError",
]
