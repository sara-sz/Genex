"""pilot_backend/weekly/allocator.py — two-stage, deterministic, offline.

Same observations and candidates in, same placements out, every time. No
model, no prompt, no network — guarded by the same AST import scan that
guards the 0.4B/C suggestion engine.

## Stage 1 — the coverage floor

Every ACTIVE allocation should receive at least `min_coverage_per_cycle`
meaningful opportunities, taken in `priority_rank` order. The default is 1.

A multi-goal activity may satisfy the floor for SEVERAL goals at once, because
it genuinely is one opportunity that serves them both. So stage 1 prefers the
candidate that closes the most still-open floors — not to be clever, but
because duplicating an activity per goal would inflate the family's week to
make a report look tidy.

If a floor cannot be met, the result is a `CoverageGap`. The allocator will
NOT invent an unsuitable activity, and it will not quietly reach past a
caregiver's Save for Later to fill a hole (section 15): a goal whose only
remaining candidates are suppressed yields `DEFERRED_CONSTRAINT`, and only an
explicit clinician override — which lives on `DeferRecord`, not here — can
release one early.

## Stage 2 — emphasis

Remaining capacity is distributed by RELATIVE weight using the
highest-averages rule: at each step give the next slot to the goal with the
largest `weight / (placed + 1)`.

That choice is deliberate. Percentages would need rounding and would break the
moment a clinician adds a third goal or weights two goals equally; 3 and 2 are
a ratio, not 60% and 40%. Highest-averages handles arbitrary N and arbitrary
positive weights, needs no rounding rule, and is exactly reproducible. Ties
break on `priority_rank` then goal key, so the order is total and never
depends on input order.

Nothing here hard-codes 60/40, 70/30, or a goal count.

## Instance refs are derived, not minted

A placement's `activity_instance_ref` is
`{cycle_id}::{activity_identity_ref}::{occurrence}`. Deterministic so the same
inputs produce byte-identical placements and a test can assert on them; opaque
so it carries no content. It identifies ONE placement in ONE cycle, which is
what alignment attaches to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from ..domain.alignment import AlignmentRole, AlignmentSource, CoverageGapReason
from ..domain.goals import GoalRef
from ..domain.monthly_plan import MonthlyGoalAllocation
from ..domain.planning_policy import CURRENT_PLANNING_POLICY, PlanningPolicyVersion

#: Bumped whenever placement or gap rules change. Stored on every alignment
#: and every gap, so a past cycle stays explainable after the rules move on.
ALLOCATION_RULE_VERSION = "weekly-allocation-rules-2026.10"
ALLOCATION_ENGINE_VERSION = "weekly-allocation-engine-2026.10"


class AllocationError(ValueError):
    """The allocator refused its inputs. PHI-safe."""

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class CandidateActivity:
    """One activity the planner may place, and the goals it supports.

    `supports` is the set of goals this activity MEANINGFULLY serves — an
    explicit, reviewed statement, not a guess. `primary_for` names the goal it
    serves most centrally, if any; it is descriptive and never a weight.
    """

    activity_identity_ref: str
    supports: Tuple[GoalRef, ...]
    primary_for: Optional[GoalRef] = None
    milestone_refs: Tuple[str, ...] = ()
    #: Opaque difficulty tier used by adaptation to pick an easier or harder
    #: variant of the same activity family. Never a child-facing score.
    difficulty_tier: int = 1
    #: Groups variants of the same activity. Adaptation swaps within a family.
    activity_family_ref: str = ""

    def __post_init__(self) -> None:
        if not (self.activity_identity_ref or "").strip():
            raise AllocationError("a candidate requires an activity identity")
        if not self.supports:
            raise AllocationError(
                "a candidate must support at least one goal; an activity that "
                "serves no allocated goal is not a planning candidate")
        object.__setattr__(self, "supports", tuple(self.supports))
        object.__setattr__(self, "milestone_refs", tuple(self.milestone_refs))
        if self.primary_for is not None and self.primary_for not in self.supports:
            raise AllocationError("primary_for must be one of the supported goals")
        if not self.activity_family_ref:
            object.__setattr__(self, "activity_family_ref",
                               self.activity_identity_ref)

    def role_for(self, ref: GoalRef) -> AlignmentRole:
        """PRIMARY for the goal it centres on, SUPPORTING for the others.

        Neither is a fraction. A SUPPORTING alignment still contributes
        exactly one attributed attempt — see `weekly/counting.py`.
        """
        return (AlignmentRole.PRIMARY if ref == self.primary_for
                else AlignmentRole.SUPPORTING)

    def supports_goal(self, ref: GoalRef) -> bool:
        return ref in self.supports


@dataclass(frozen=True)
class PlannedActivity:
    """One placement: an activity instance and everything it is aligned to."""

    activity_instance_ref: str
    activity_identity_ref: str
    #: Goal -> role. One placement, several attributions, ONE opportunity.
    aligned_goals: Tuple[Tuple[GoalRef, AlignmentRole], ...]
    alignment_source: AlignmentSource
    milestone_refs: Tuple[str, ...] = ()
    placed_in_stage: int = 1
    difficulty_tier: int = 1
    activity_family_ref: str = ""

    @property
    def goal_count(self) -> int:
        return len(self.aligned_goals)


@dataclass(frozen=True)
class PlannedGap:
    """A floor the allocator could not meet. Becomes a `CoverageGap` row."""

    goal_ref: GoalRef
    reason: CoverageGapReason
    capacity_available: int
    capacity_required: int
    detail: str = ""


@dataclass(frozen=True)
class AllocationResult:
    placements: Tuple[PlannedActivity, ...] = ()
    gaps: Tuple[PlannedGap, ...] = ()
    rule_version: str = ALLOCATION_RULE_VERSION

    @property
    def placed_count(self) -> int:
        """Distinct opportunities. NOT the sum of per-goal attributions."""
        return len(self.placements)

    def attributions_for(self, ref: GoalRef) -> int:
        return sum(1 for p in self.placements
                   if any(g == ref for g, _ in p.aligned_goals))


def _instance_ref(cycle_id: str, identity_ref: str, occurrence: int) -> str:
    return f"{cycle_id}::{identity_ref}::{occurrence}"


def _goal_sort_key(allocation: MonthlyGoalAllocation) -> Tuple[int, str]:
    return (allocation.priority_rank, allocation.goal_ref.as_key())


def allocate(cycle_id: str,
             allocations: Sequence[MonthlyGoalAllocation],
             candidates: Sequence[CandidateActivity],
             *,
             capacity: int,
             suppressed_identity_refs: Sequence[str] = (),
             policy: PlanningPolicyVersion = CURRENT_PLANNING_POLICY,
             is_partial_week: bool = False) -> AllocationResult:
    """Place activities for one cycle. Pure, deterministic, offline.

    `allocations` must already be the rows EFFECTIVE for this cycle — see
    `effective_allocations`. `suppressed_identity_refs` are activity
    identities under an unexpired caregiver defer; the allocator treats them
    as unavailable and never overrides one.
    """
    if capacity < 0:
        raise AllocationError("capacity cannot be negative")

    active = sorted([a for a in allocations if a.is_active], key=_goal_sort_key)
    suppressed = frozenset(suppressed_identity_refs)
    available = [c for c in candidates
                 if c.activity_identity_ref not in suppressed]
    # Deterministic candidate order, independent of how the caller listed them.
    available.sort(key=lambda c: c.activity_identity_ref)

    placements: List[PlannedActivity] = []
    gaps: List[PlannedGap] = []
    used_identities: set = set()
    covered: Dict[str, int] = {a.goal_ref.as_key(): 0 for a in active}

    def remaining() -> int:
        return capacity - len(placements)

    def place(candidate: CandidateActivity, goals: Sequence[GoalRef],
              stage: int) -> None:
        occurrence = sum(1 for p in placements
                         if p.activity_identity_ref == candidate.activity_identity_ref)
        aligned = tuple((g, candidate.role_for(g))
                        for g in sorted(goals, key=lambda r: r.as_key()))
        placements.append(PlannedActivity(
            activity_instance_ref=_instance_ref(
                cycle_id, candidate.activity_identity_ref, occurrence),
            activity_identity_ref=candidate.activity_identity_ref,
            aligned_goals=aligned,
            alignment_source=AlignmentSource.GENEX_RULE,
            milestone_refs=candidate.milestone_refs,
            placed_in_stage=stage,
            difficulty_tier=candidate.difficulty_tier,
            activity_family_ref=candidate.activity_family_ref,
        ))
        used_identities.add(candidate.activity_identity_ref)
        for goal in goals:
            covered[goal.as_key()] = covered.get(goal.as_key(), 0) + 1

    # ---------------- stage 1: the coverage floor -----------------------
    #
    # Iterate until no further floor can be closed. Each pass picks the
    # candidate closing the MOST open floors, so one multi-goal activity can
    # satisfy several goals rather than being duplicated per goal.
    def open_floors() -> List[MonthlyGoalAllocation]:
        return [a for a in active
                if covered[a.goal_ref.as_key()] < a.min_coverage_per_cycle]

    while open_floors() and remaining() > 0:
        wanted = open_floors()
        wanted_refs = {a.goal_ref for a in wanted}
        best: Optional[Tuple[Tuple[int, int, str], CandidateActivity,
                             Tuple[GoalRef, ...]]] = None
        for candidate in available:
            if candidate.activity_identity_ref in used_identities:
                continue
            closes = tuple(r for r in candidate.supports if r in wanted_refs)
            if not closes:
                continue
            # Most floors closed wins; then the best priority rank it serves;
            # then the identity ref, so the order is total.
            best_rank = min(a.priority_rank for a in wanted
                            if a.goal_ref in closes)
            key = (-len(closes), best_rank, candidate.activity_identity_ref)
            if best is None or key < best[0]:
                best = (key, candidate, closes)
        if best is None:
            break
        place(best[1], best[2], stage=1)

    # Anything still open is a gap, classified by WHY.
    for allocation in open_floors():
        ref = allocation.goal_ref
        serving = [c for c in candidates if c.supports_goal(ref)]
        unsuppressed = [c for c in serving
                        if c.activity_identity_ref not in suppressed]
        if not serving:
            reason = CoverageGapReason.NO_SUITABLE_ACTIVITY
        elif not unsuppressed:
            # Section 15: prefer a gap over silently defeating a defer.
            reason = CoverageGapReason.DEFERRED_CONSTRAINT
        elif is_partial_week:
            reason = CoverageGapReason.PARTIAL_WEEK
        else:
            reason = CoverageGapReason.INSUFFICIENT_CAPACITY
        gaps.append(PlannedGap(
            goal_ref=ref,
            reason=reason,
            capacity_available=max(0, remaining()),
            capacity_required=allocation.min_coverage_per_cycle
            - covered[ref.as_key()],
            detail=f"rank_{allocation.priority_rank}",
        ))

    # ---------------- stage 2: relative emphasis ------------------------
    #
    # Highest-averages: the next slot goes to the goal with the largest
    # weight / (placed + 1). No percentages, no rounding rule, any N, any
    # positive weights.
    while remaining() > 0:
        pool = [c for c in available
                if c.activity_identity_ref not in used_identities]
        if not pool:
            break
        ranked = sorted(
            active,
            key=lambda a: (-(a.emphasis_weight / (covered[a.goal_ref.as_key()] + 1)),
                           a.priority_rank, a.goal_ref.as_key()))
        progressed = False
        for allocation in ranked:
            ref = allocation.goal_ref
            serving = [c for c in pool if c.supports_goal(ref)]
            if not serving:
                continue
            # Prefer the candidate serving the most ALLOCATED goals, so a
            # multi-goal activity is not wasted; then by identity ref.
            active_refs = {a.goal_ref for a in active}
            choice = min(serving,
                         key=lambda c: (-len([r for r in c.supports
                                              if r in active_refs]),
                                        c.activity_identity_ref))
            place(choice, [r for r in choice.supports if r in active_refs],
                  stage=2)
            progressed = True
            break
        if not progressed:
            break

    return AllocationResult(placements=tuple(placements), gaps=tuple(gaps),
                            rule_version=ALLOCATION_RULE_VERSION)


def effective_allocations(allocations: Sequence[MonthlyGoalAllocation],
                          cycle_sequence: int) -> Tuple[MonthlyGoalAllocation, ...]:
    """The allocation rows in force for a given cycle.

    Section 26. A clinician reprioritising mid-month writes a SUCCESSOR with
    `effective_from_cycle`; both rows are retained. Cycle 2 must use what was
    in force at cycle 2 even after cycle 3's decision lands, so this selects
    the latest row per goal whose `effective_from_cycle` has arrived — never
    simply "the active ones", which would retroactively rewrite history.
    """
    if cycle_sequence < 1:
        raise AllocationError("cycle sequences start at 1")
    by_goal: Dict[str, MonthlyGoalAllocation] = {}
    for allocation in sorted(allocations,
                             key=lambda a: (a.effective_from_cycle,
                                            a.allocation_id)):
        if allocation.effective_from_cycle > cycle_sequence:
            continue
        if allocation.status.value in ("retired", "paused"):
            by_goal.pop(allocation.goal_ref.as_key(), None)
            continue
        by_goal[allocation.goal_ref.as_key()] = allocation
    return tuple(sorted(by_goal.values(), key=_goal_sort_key))


def suppressed_identities(defer_records: Sequence, cycle_sequence: int
                          ) -> Tuple[str, ...]:
    """Activity identities a caregiver defer suppresses for this cycle.

    A defer that was explicitly overridden by a clinician — with an actor and
    a stated reason, recorded on the `DeferRecord` — no longer suppresses.
    There is no system path to that override.
    """
    return tuple(sorted({
        record.activity_identity_ref for record in defer_records
        if record.suppresses(cycle_sequence) and not record.was_overridden
    }))
