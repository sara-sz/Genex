"""pilot_backend/domain/planning_policy.py — defaults that are POLICY.

Primary emphasis 3, secondary 2, minimum coverage 1, two goals by default.
Those are product decisions with a current value, not truths about child
development — so they live in a named, versioned record rather than as
constants scattered through the allocation code.

## Why this is a version and not four module-level numbers

A number inlined at its use site cannot answer the only question that matters
six months from now: "what weighting was this child's October plan actually
built with?" Every `MonthlyFocusPlan` stores `policy_version`, so the answer is
recorded rather than reconstructed from whatever the constant happens to be
when someone asks.

It also keeps the defaults CHANGEABLE without a schema change. A clinician who
wants three goals, or two goals weighted equally, is exercising clinical
judgement — not violating an invariant. Nothing in the domain caps the number
of goals or requires 3-and-2; `MonthlyGoalAllocation` validates only that a
rank is positive and a weight is positive.

## What is an invariant, and therefore is NOT here

    a weight must be positive          -> MonthlyGoalAllocation.__post_init__
    a rank starts at 1                 -> MonthlyGoalAllocation.__post_init__
    coverage cannot be negative        -> MonthlyGoalAllocation.__post_init__
    a caregiver goal is not RTM-eligible -> GoalRef / require_clinical_goal_ref

Those hold for every policy version that will ever exist. The numbers in this
module hold until the next product decision.

## Weights are relative, never percentages

3 and 2 mean "the primary goal gets half again as much emphasis as the
secondary". They are not 30% and 20%, they do not sum to anything meaningful,
and weekly allocation — which does not exist yet — must treat them as ratios.
Reading them as percentages would silently cap a plan at 50% of a week.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Tuple

from .goal_vocabulary import DOMAIN_TIE_BREAK_ORDER


class PlanningPolicyError(ValueError):
    """An unknown or malformed planning policy. PHI-safe."""

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class PlanningPolicyVersion:
    """A named set of planning defaults, pinned by version id."""

    policy_version: str
    #: Default count offered, NOT a maximum. A clinician may allocate more.
    default_goal_count: int
    #: Relative emphasis by priority rank: rank 1 -> 3, rank 2 -> 2, ...
    emphasis_by_rank: Tuple[int, ...]
    #: Emphasis applied to any rank beyond the tuple. Keeps a third or fourth
    #: goal meaningful instead of silently unweighted.
    default_emphasis_beyond_ranks: int
    #: Every allocated goal appears at least this many times per weekly cycle,
    #: so a secondary goal is never crowded out to zero by a primary one.
    min_coverage_per_cycle: int
    #: Order used only after evidence strength has already decided.
    domain_tie_break_order: Tuple[str, ...]
    #: Human-readable note carried into the annotation and the audit trail.
    rationale: str = ""

    def __post_init__(self) -> None:
        if not (self.policy_version or "").strip():
            raise PlanningPolicyError("a planning policy requires a version id")
        if self.default_goal_count < 1:
            raise PlanningPolicyError("default goal count must be at least 1")
        if not self.emphasis_by_rank:
            raise PlanningPolicyError("a policy must define at least one rank weight")
        if any(w <= 0 for w in self.emphasis_by_rank):
            raise PlanningPolicyError("emphasis weights must be positive")
        if self.default_emphasis_beyond_ranks <= 0:
            raise PlanningPolicyError("the beyond-rank emphasis must be positive")
        if self.min_coverage_per_cycle < 0:
            raise PlanningPolicyError("minimum coverage cannot be negative")

    def emphasis_for_rank(self, priority_rank: int) -> int:
        """Default emphasis for a rank. Ranks past the table share a floor."""
        if priority_rank < 1:
            raise PlanningPolicyError("priority rank starts at 1")
        if priority_rank <= len(self.emphasis_by_rank):
            return self.emphasis_by_rank[priority_rank - 1]
        return self.default_emphasis_beyond_ranks


#: The October pilot policy. Founder-approved product decision, recorded here
#: so a plan built under it stays interpretable after the default changes.
POLICY_2026_10 = PlanningPolicyVersion(
    policy_version="planning-policy-2026.10",
    default_goal_count=2,
    emphasis_by_rank=(3, 2),
    default_emphasis_beyond_ranks=1,
    min_coverage_per_cycle=1,
    domain_tie_break_order=DOMAIN_TIE_BREAK_ORDER,
    rationale=(
        "Two goals is the default offer, not a cap. Primary 3 / secondary 2 is "
        "a relative emphasis ratio, not a percentage split. Minimum coverage 1 "
        "guarantees the secondary goal is never squeezed to zero in a cycle."
    ),
)

CURRENT_PLANNING_POLICY = POLICY_2026_10

_POLICIES: Mapping[str, PlanningPolicyVersion] = {
    POLICY_2026_10.policy_version: POLICY_2026_10,
}


def policy_for(policy_version: str) -> PlanningPolicyVersion:
    """Resolve a recorded policy version, refusing an unknown one.

    Fails closed on purpose. A plan stamped with a policy nobody can produce
    cannot be explained, and silently substituting the current defaults would
    misreport what the plan was actually built with.
    """
    key = (policy_version or "").strip()
    try:
        return _POLICIES[key]
    except KeyError:
        raise PlanningPolicyError(
            f"unknown planning policy version: {key or '<empty>'}") from None


def known_policy_versions() -> Tuple[str, ...]:
    return tuple(sorted(_POLICIES))
