"""pilot_backend/integration/week_one_release.py — approved goal -> Week 1.

0.6A-2. ONE provider action that composes the EXISTING transitions:

    ClinicalGoal validation    goals.get_goal / goal_anchor / is_activity_mappable
    monthly plan               planning.active_plan | create_plan
    goal allocation            planning.allocate_goal
    monthly activation         planning.activate_plan
    Week 1 cycle               weekly.create_cycle
    candidates                 activity_bank.candidates_for_goal (static bank)
    allocation                 weekly.allocate_cycle            <- capacity here
    immutable snapshot         weekly.capture_snapshot
    release                    weekly.release_cycle

## IT COMPOSES; IT DOES NOT BYPASS

Every state change above is an existing service method with its own
authorization, its own claim and its own refusals. This module adds no planning
model, no second allocator and no new idempotency subsystem — it orders the
calls and refuses when one of them says no.

The founder's rule was that the low-level transitions must not be exposed
individually to the browser merely to make this possible. They are not: the
route calls this orchestrator, and this orchestrator calls them.

## CAPACITY IS A REQUIRED INPUT AND IS NEVER DEFAULTED

`family_declared_capacity` has no default here, no fallback and no inferred
value. It means what `CapacityLedger` says it means — what the FAMILY reports it
can carry — recorded by the provider for planning. It is NOT a recommended
frequency, a prescribed frequency, a clinical dosage or a required number of
sessions, and nothing in this module treats it as one: it is passed straight to
the existing allocator as capacity and straight to the existing ledger.

Absent or non-positive capacity REFUSES. A default would quietly author a
clinical frequency, which is exactly what the founder forbade.

Provenance uses the narrowest existing mechanism: `allocate_cycle` already emits
an audit event carrying `declared_capacity` together with the acting principal,
and `audit/events.py` already allowlists that field. No model change.

## THE GOAL GATE

A week may be generated only from an APPROVED ClinicalGoal that is
active, carries a current `GoalVersion`, has a `ClinicalGoalAnchor`, and whose
anchor is activity-mappable. A `GoalSuggestion` is never an input: a suggestion
is an offer, and planning from one would mean a family received activities for a
target no clinician accepted.

## NO ACTIVITY CONTENT IS AUTHORED HERE

Content comes only from the reviewed static bank, reached through the goal's own
anchor `family_bindings`. No runtime model, no Parent activity engine, no
generic fallback, and no cross-family substitution — `require_all_families_served`
refuses before anything is written if a bound family has no reviewed content.

## REPLAY

Re-running after release returns the EXISTING released week rather than building
a second one, and a different capacity cannot rewrite it. The underlying
guarantees are the existing ones: `create_cycle` refuses a duplicate sequence,
`allocate_cycle` holds a `ClaimKind.WEEKLY_ALLOCATION` claim and refuses a
second allocation, `capture_snapshot` refuses a second snapshot, and
`release_cycle` refuses a second release.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..domain.goals import GoalKind, GoalRef, GoalStatus
from ..domain.source_link import SourceSystem
from ..domain.weekly_plan_document import (
    build_document,
    parent_week_view,
)
from .activity_bank import candidates_for_goal, require_all_families_served

#: Week 1. This slice releases the FIRST cycle of the active month only; Week 2+
#: is 0.6A-3 and `generate_next_cycle` already exists for it.
WEEK_ONE = 1

#: The source system recorded on the snapshot. THERAPIST because the Pilot — the
#: therapist platform — now AUTHORS this week. v1 snapshots captured PARENT's
#: own plan; this is not that, and recording PARENT would misattribute it.
SNAPSHOT_SOURCE_SYSTEM = SourceSystem.THERAPIST


class WeekOneReleaseError(Exception):
    """Week 1 could not be released. PHI-safe: never quotes activity content."""

    PHI_SAFE_MESSAGE = True


class CapacityRequired(WeekOneReleaseError):
    """No family-declared weekly capacity was supplied.

    REFUSED rather than defaulted. The number of activities a week contains is a
    direct function of this value, so inventing one would author a clinical
    frequency that no family declared and no clinician approved.
    """


class GoalNotReleasable(WeekOneReleaseError):
    """The goal cannot drive activity generation.

    Not active, no current version, no canonical anchor, or an anchor whose
    activity families are not reconciled. Fail closed: a week built on one of
    these would give a family activities for a target the system cannot defend.
    """


@dataclass(frozen=True)
class ReleasableGoal:
    """One approved goal that passed the gate, with everything release needs.

    A named record rather than a tuple: four positional values that all happen
    to be objects is exactly where a later reader swaps two of them.
    """

    goal: Any
    version_id: str
    text: str
    anchor: Any

    @property
    def goal_id(self) -> str:
        return self.goal.clinical_goal_id

    @property
    def ref(self) -> GoalRef:
        return GoalRef(GoalKind.CLINICAL, self.goal_id)


@dataclass(frozen=True)
class WeekOneOutcome:
    """What one release request produced or found already present."""

    child_id: str
    focus_plan_id: str
    cycle_id: str
    goal_ids: Tuple[str, ...]
    activity_count: int
    #: False when Week 1 was already released — the existing week is returned.
    created: bool
    released_at: Optional[datetime]
    snapshot_id: str
    #: The Parent-facing projection, derived from the frozen snapshot.
    parent_week: Dict[str, Any]


class WeekOneReleaseService:
    """Compose the existing transitions into one provider action."""

    def __init__(self, *, repos: Any, goals: Any, plans: Any, weekly: Any,
                 activity_bank: Any) -> None:
        self._repos = repos
        self._goals = goals
        self._plans = plans
        self._weekly = weekly
        self._bank = activity_bank

    # -- the goal gate ----------------------------------------------------

    def releasable_goals(self, principal, child_id: str
                         ) -> Tuple["ReleasableGoal", ...]:
        """Every approved ClinicalGoal that may drive activities.

        Ordered by goal id, so the whole operation is deterministic. Side-effect
        free, so a reviewer can inspect the gate without releasing anything.

        The version is read as `current_version_id` plus `current_text`, both
        existing authorized reads — this module never reaches into
        `goal_versions` itself.
        """
        found: List[ReleasableGoal] = []
        for goal in self._goals.list_clinical_goals(principal, child_id):
            if goal.status is not GoalStatus.ACTIVE:
                continue
            version_id = (goal.current_version_id or "").strip()
            if not version_id:
                # A goal with no current version has no approved wording, so
                # there is nothing a family could be shown.
                continue
            ref = GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id)
            anchor = self._goals.goal_anchor(principal, ref)
            if anchor is None or not anchor.is_activity_mappable:
                # Unanchored or unmappable. Skipped here and refused below if
                # NOTHING is releasable — an unmappable goal must not block a
                # mappable sibling, the same rule F-B v2 follows.
                continue
            found.append(ReleasableGoal(
                goal=goal, version_id=version_id,
                text=self._goals.current_text(principal, ref), anchor=anchor))
        return tuple(sorted(found, key=lambda row: row.goal_id))

    # -- the operation ----------------------------------------------------

    def release_week_one(self, principal, child_id: str, *,
                         family_declared_capacity: Optional[int],
                         timezone_of_record: str = "UTC",
                         cycle_month: Optional[str] = None,
                         request_id: str = "") -> WeekOneOutcome:
        """Create, allocate, snapshot and release Week 1. Or return it.

        Authorization is delegated: every service call below gates on active
        child access and on the planner/managing-clinician rule. The transport
        additionally requires PROVIDER role before this is reached.
        """
        capacity = self._require_capacity(family_declared_capacity)
        goals = self.releasable_goals(principal, child_id)
        if not goals:
            raise GoalNotReleasable(
                "this child has no active, anchored, activity-mappable "
                "clinician-approved goal")

        plan = self._resolve_plan(principal, child_id, goals,
                                  timezone_of_record=timezone_of_record,
                                  cycle_month=cycle_month,
                                  request_id=request_id)
        cycle = self._resolve_cycle(principal, plan, request_id=request_id)

        if cycle.is_released:
            # REPLAY. The existing released week is returned unchanged, and the
            # capacity on THIS request is not consulted at all — a released
            # cycle's content is frozen, and a later capacity applies to a
            # future cycle rather than to history.
            return self._existing_outcome(principal, plan, cycle, goals)

        templates_by_id = self._candidate_templates(goals)
        placements = self._allocate(principal, cycle, goals, templates_by_id,
                                    capacity=capacity, request_id=request_id)
        return self._snapshot_and_release(
            principal, plan, cycle, goals, placements, templates_by_id,
            request_id=request_id)

    # -- steps -------------------------------------------------------------

    @staticmethod
    def _require_capacity(value: Optional[int]) -> int:
        """The family's declared weekly capacity, or a refusal. No default."""
        if value is None:
            raise CapacityRequired(
                "the family's declared weekly activity capacity is required")
        if isinstance(value, bool) or not isinstance(value, int):
            raise CapacityRequired(
                "the declared weekly capacity must be an integer")
        if value < 1:
            # Zero would release an empty week, which is not a plan. Refused
            # rather than treated as "skip this week".
            raise CapacityRequired(
                "the declared weekly capacity must be at least one activity")
        return value

    def _resolve_plan(self, principal, child_id: str, goals, *,
                      timezone_of_record: str, cycle_month: Optional[str],
                      request_id: str):
        """The active monthly plan, created and activated if there is none.

        Uses the EXISTING transitions: `create_plan` drafts, `allocate_goal`
        puts each approved goal on the month, `activate_plan` wins the
        child-month claim. Nothing is skipped and no state is set directly.
        """
        # The month is resolved FIRST, because `active_plan` is scoped to one
        # child-MONTH: a plan active for September is not the plan this week
        # belongs to, and asking without a month would make the answer depend on
        # whatever the service happened to pick.
        month = cycle_month or self._current_cycle_month()
        existing = self._plans.active_plan(principal, child_id, month)
        if existing is not None:
            self._ensure_allocated(principal, existing, goals,
                                   request_id=request_id)
            return existing

        plan = self._plans.create_plan(
            principal, child_id, month, timezone_of_record,
            request_id=request_id)
        self._ensure_allocated(principal, plan, goals, request_id=request_id)
        # `activate_plan` refuses a plan with no active allocation, so the
        # ordering here is required rather than stylistic.
        return self._plans.activate_plan(principal, plan.focus_plan_id,
                                         request_id=request_id)

    def _ensure_allocated(self, principal, plan, goals, *,
                          request_id: str) -> None:
        """Allocate each approved goal, skipping any already on the plan.

        `priority_rank` follows the deterministic goal order. NO second goal is
        manufactured: `default_goal_count=2` is the default OFFER, not a
        requirement, and a child with one approved goal gets a one-goal month.

        `emphasis_weight` and `min_coverage_per_cycle` are left unset so the
        plan's own recorded POLICY supplies them — passing explicit values here
        would freeze today's defaults into a plan that is supposed to carry its
        policy version.
        """
        allocated = {
            allocation.goal_ref.goal_id
            for allocation in self._plans.list_allocations(
                principal, plan.focus_plan_id, include_inactive=True)
        }
        rank = len(allocated)
        for row in goals:
            if row.goal_id in allocated:
                continue
            rank += 1
            self._plans.allocate_goal(
                principal, plan.focus_plan_id, row.ref,
                priority_rank=rank, request_id=request_id)

    def _resolve_cycle(self, principal, plan, *, request_id: str):
        """Week 1 of the active plan, created if it does not exist yet."""
        for cycle in self._weekly.list_cycles(principal, plan.focus_plan_id):
            if cycle.sequence_in_month == WEEK_ONE:
                return cycle
        return self._weekly.create_cycle(
            principal, plan.focus_plan_id, sequence_in_month=WEEK_ONE,
            request_id=request_id)

    def _candidate_templates(self, goals) -> Dict[str, Any]:
        """Reviewed templates for every family the goals' anchors bind.

        `require_all_families_served` runs FIRST, so a goal binding a family
        with no reviewed content refuses before any week is written rather than
        producing a thin week nobody notices.
        """
        families: List[str] = []
        for row in goals:
            families.extend(
                binding.family_ref
                for binding in row.anchor.rung.family_bindings)
        require_all_families_served(self._bank, families)
        return {template.activity_template_id: template
                for template in self._bank.templates_for_families(families)}

    def _allocate(self, principal, cycle, goals, templates_by_id, *,
                  capacity: int, request_id: str):
        """Build candidates per goal and run the EXISTING allocator.

        One `CandidateActivity` per reviewed template per goal, built by
        `candidates_for_goal` — which orders by the template's content digest,
        so the selection is a function of the authored content and of capacity,
        never of dict iteration order.
        """
        candidates: List[Any] = []
        for row in goals:
            bound = {binding.family_ref
                     for binding in row.anchor.rung.family_bindings}
            templates = [template for template in templates_by_id.values()
                         if template.activity_family_ref in bound]
            candidates.extend(candidates_for_goal(templates, row.ref))
        if not candidates:
            raise GoalNotReleasable(
                "no reviewed activity content serves this child's goals")

        result = self._weekly.allocate_cycle(
            principal, cycle.cycle_id, candidates,
            family_declared_capacity=capacity, request_id=request_id)
        if not result.placements:
            # Capacity was positive and candidates existed, so an empty result
            # means the allocator refused every placement. Releasing an empty
            # week would tell a family there is nothing to do this week.
            raise GoalNotReleasable(
                "the allocator placed no activity for this cycle")
        return result.placements

    def _snapshot_and_release(self, principal, plan, cycle, goals, placements,
                              templates_by_id, *, request_id: str):
        """Freeze the content, then release. Snapshot FIRST, always.

        `release_cycle` itself refuses a cycle with no snapshot, so this
        ordering is enforced by the existing service rather than only here.
        """
        goal_text = {row.goal_id: row.text for row in goals}
        goal_versions = {row.goal_id: row.version_id for row in goals}

        document = build_document(
            cycle=cycle, placements=placements,
            templates_by_id=templates_by_id,
            goal_text_by_id=goal_text,
            goal_version_by_id=goal_versions)
        snapshot = self._weekly.capture_snapshot(
            principal, cycle.cycle_id, SNAPSHOT_SOURCE_SYSTEM,
            cycle.cycle_id, document, request_id=request_id)
        released = self._weekly.release_cycle(
            principal, cycle.cycle_id, request_id=request_id)

        return WeekOneOutcome(
            child_id=cycle.child_id,
            focus_plan_id=plan.focus_plan_id,
            cycle_id=cycle.cycle_id,
            goal_ids=tuple(row.goal_id for row in goals),
            activity_count=len(placements),
            created=True,
            released_at=released.released_to_parent_at,
            snapshot_id=snapshot.snapshot_id,
            parent_week=parent_week_view(
                snapshot.document(),
                released_at=released.released_to_parent_at))

    def _existing_outcome(self, principal, plan, cycle, goals) -> WeekOneOutcome:
        """The already-released week, rebuilt from its FROZEN snapshot."""
        snapshots = self._repos.weekly_plan_snapshots.list_for_cycle(
            cycle.cycle_id)
        if not snapshots:  # pragma: no cover - release requires a snapshot
            raise WeekOneReleaseError(
                "a released cycle has no plan snapshot")
        snapshot = snapshots[0]
        document = snapshot.document()
        return WeekOneOutcome(
            child_id=cycle.child_id,
            focus_plan_id=plan.focus_plan_id,
            cycle_id=cycle.cycle_id,
            goal_ids=tuple(row.goal_id for row in goals),
            activity_count=len(document.get("activities") or ()),
            created=False,
            released_at=cycle.released_to_parent_at,
            snapshot_id=snapshot.snapshot_id,
            parent_week=parent_week_view(
                document, released_at=cycle.released_to_parent_at))

    # -- the Parent read ---------------------------------------------------

    def this_week(self, principal, child_id: str) -> Optional[Dict[str, Any]]:
        """The released Week 1 for a child, or None. Authorized read.

        Returns None — not a draft — when nothing has been released. A draft
        cycle exists as soon as a provider starts the operation, and showing it
        would put a partially allocated week in front of a family.

        Derived from the SNAPSHOT, so a later goal revision cannot change what a
        released week says.
        """
        plan = self._plans.active_plan(principal, child_id,
                                       self._current_cycle_month())
        if plan is None:
            return None
        for cycle in self._weekly.list_cycles(principal, plan.focus_plan_id):
            if cycle.sequence_in_month != WEEK_ONE or not cycle.is_released:
                continue
            snapshots = self._repos.weekly_plan_snapshots.list_for_cycle(
                cycle.cycle_id)
            if not snapshots:  # pragma: no cover - release requires one
                return None
            return parent_week_view(snapshots[0].document(),
                                    released_at=cycle.released_to_parent_at)
        return None

    @staticmethod
    def _current_cycle_month() -> str:
        """The calendar month to plan, when the caller does not name one."""
        from ..domain.entities import utc_now  # noqa: WPS433

        stamp = utc_now()
        return f"{stamp.year:04d}-{stamp.month:02d}"


__all__ = [
    "CapacityRequired",
    "ReleasableGoal",
    "GoalNotReleasable",
    "SNAPSHOT_SOURCE_SYSTEM",
    "WEEK_ONE",
    "WeekOneOutcome",
    "WeekOneReleaseError",
    "WeekOneReleaseService",
]
