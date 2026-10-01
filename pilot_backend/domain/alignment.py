"""pilot_backend/domain/alignment.py — which activity serves which goal.

    ActivityGoalAlignment  immutable attribution of one scheduled instance
    CoverageGap            immutable record that a goal could not be covered
    CapacityLedger         what the family can carry, and what was placed

## Alignment attaches to the SCHEDULED INSTANCE, never the template

`activity_instance_ref` identifies one placement in one cycle. The reusable
activity template is `activity_identity_ref` and is carried only as
provenance.

The distinction is load-bearing. "Take turns requesting cars" scheduled on
Tuesday of week 1 and again on Friday of week 3 are two opportunities with two
attributions, two outcomes and possibly two different goal sets after a
clinician reprioritises. Aligning the template would collapse them into one
statement that is true of neither.

It is also what makes history immutable: a template can be re-aligned for a
future cycle without rewriting what a past cycle actually did (section 26).

## One activity, several goals, ONE opportunity

A multi-goal activity is still a single thing the family does once. Duplicating
it so two goal streams each "get one" would inflate the family's week to make a
report look tidy.

So the counting rule is split in two, and the split is the whole defence
against double counting:

    ObservationEvent      is the unit of ATTEMPT COUNT
    ActivityGoalAlignment is the unit of ATTRIBUTION

One activity supporting goals A and B, attempted once:

    total attempts            = 1
    goal-A attributed attempts = 1
    goal-B attributed attempts = 1
    1 + 1                      = 2, and that number means NOTHING

`weekly/counting.py` computes totals from distinct event ids and never by
summing per-goal streams. `PRIMARY` and `SUPPORTING` describe the activity's
role for that goal; neither is a fraction and neither scales a count.

## A CoverageGap is a planner condition

It records that the planner could not place a meaningful opportunity. It is
never evidence about the child or the caregiver — nobody failed. The type
carries `is_planner_condition = True` and the reasons are a closed enum so
that no caller can write "parent didn't do it" into one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

from .entities import SCHEMA_VERSION, utc_now
from .enums import Visibility
from .goals import GoalKind, GoalRef
from .ids import new_alignment_id, new_capacity_ledger_id, new_coverage_gap_id


class AlignmentError(ValueError):
    """Invalid alignment, gap or capacity record. PHI-safe."""

    PHI_SAFE_MESSAGE = True


class AlignmentRole(str, Enum):
    """How centrally this activity serves this goal.

    Descriptive, NOT a weight and NOT a fraction. A SUPPORTING alignment still
    contributes exactly one attributed attempt when the activity is attempted;
    it does not contribute half of one.
    """

    PRIMARY = "primary"
    SUPPORTING = "supporting"


class AlignmentSource(str, Enum):
    """Who decided this activity serves this goal."""

    GENEX_RULE = "genex_rule"
    CLINICIAN_ASSIGNED = "clinician_assigned"
    #: Carried forward from the alignment of a previous cycle's instance.
    INHERITED_FROM_VERSION = "inherited_from_version"


class CoverageGapReason(str, Enum):
    """Why the planner could not cover a goal this cycle.

    Every member is a statement about the PLAN. There is deliberately no
    member for non-adherence, child performance or caregiver behaviour, so a
    gap cannot be recorded as any of those.
    """

    PARTIAL_WEEK = "partial_week"
    INSUFFICIENT_CAPACITY = "insufficient_capacity"
    NO_SUITABLE_ACTIVITY = "no_suitable_activity"
    CLINICIAN_DIRECTED = "clinician_directed"
    GOAL_PAUSED = "goal_paused"
    CAPACITY_CONSUMED_BY_CLINICIAN_ADD = "capacity_consumed_by_clinician_add"
    #: The only suitable activity is under a caregiver defer. Section 15: a
    #: system-only coverage floor must not silently defeat a defer signal.
    DEFERRED_CONSTRAINT = "deferred_constraint"


@dataclass(frozen=True)
class ActivityGoalAlignment:
    """One scheduled activity instance serving one goal. Immutable."""

    alignment_id: str
    cycle_id: str
    child_id: str
    #: The SCHEDULED INSTANCE — one placement in one cycle.
    activity_instance_ref: str
    #: The reusable template this instance came from. Provenance only; never
    #: the unit of alignment, and never a join key for attribution.
    activity_identity_ref: str
    goal_kind: GoalKind
    goal_id: str
    role: AlignmentRole
    alignment_source: AlignmentSource
    rationale: str = ""
    milestone_refs: Tuple[str, ...] = ()
    rule_version: str = ""
    #: The allocation in force when this alignment was written, so a later
    #: reprioritisation cannot make a past cycle unexplainable.
    allocation_id: Optional[str] = None
    assigned_by_actor_id: Optional[str] = None
    created_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.SYSTEM_AUDIT

    def __post_init__(self) -> None:
        for label, value in (("cycle_id", self.cycle_id),
                             ("child_id", self.child_id),
                             ("activity_instance_ref", self.activity_instance_ref)):
            if not (value or "").strip():
                raise AlignmentError(f"an alignment requires {label}")
        object.__setattr__(self, "milestone_refs", tuple(self.milestone_refs))

    @property
    def goal_ref(self) -> GoalRef:
        return GoalRef(self.goal_kind, self.goal_id)

    @staticmethod
    def create(cycle_id: str, child_id: str, activity_instance_ref: str,
               ref: GoalRef, *, activity_identity_ref: str,
               role: AlignmentRole, alignment_source: AlignmentSource,
               rationale: str = "", milestone_refs: Tuple[str, ...] = (),
               rule_version: str = "", allocation_id: Optional[str] = None,
               assigned_by_actor_id: Optional[str] = None,
               now: Optional[datetime] = None) -> "ActivityGoalAlignment":
        return ActivityGoalAlignment(
            alignment_id=new_alignment_id(),
            cycle_id=cycle_id,
            child_id=child_id,
            activity_instance_ref=activity_instance_ref,
            activity_identity_ref=activity_identity_ref,
            goal_kind=ref.kind,
            goal_id=ref.goal_id,
            role=role,
            alignment_source=alignment_source,
            rationale=rationale,
            milestone_refs=tuple(milestone_refs),
            rule_version=rule_version,
            allocation_id=allocation_id,
            assigned_by_actor_id=assigned_by_actor_id,
            created_at=now or utc_now(),
        )


@dataclass(frozen=True)
class CoverageGap:
    """The planner could not cover this goal this cycle. Immutable.

    A PLANNER CONDITION. Never non-adherence, never a child failure, never a
    caregiver failure. `is_planner_condition` is a constant on the type so the
    property is readable at the call site rather than depending on the reader
    knowing the enum.
    """

    gap_id: str
    cycle_id: str
    child_id: str
    goal_kind: GoalKind
    goal_id: str
    reason: CoverageGapReason
    capacity_available: int = 0
    capacity_required: int = 0
    rule_version: str = ""
    detail: str = ""
    created_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.SYSTEM_AUDIT

    #: Read by reporting and by the adaptation engine. A gap never counts as
    #: evidence about a person.
    is_planner_condition = True
    not_a_failure = True

    def __post_init__(self) -> None:
        if not isinstance(self.reason, CoverageGapReason):
            raise AlignmentError("reason must be a CoverageGapReason")
        if self.capacity_available < 0 or self.capacity_required < 0:
            raise AlignmentError("capacity figures cannot be negative")

    @property
    def goal_ref(self) -> GoalRef:
        return GoalRef(self.goal_kind, self.goal_id)

    @staticmethod
    def create(cycle_id: str, child_id: str, ref: GoalRef,
               reason: CoverageGapReason, *,
               capacity_available: int = 0, capacity_required: int = 0,
               rule_version: str = "", detail: str = "",
               now: Optional[datetime] = None) -> "CoverageGap":
        return CoverageGap(
            gap_id=new_coverage_gap_id(),
            cycle_id=cycle_id,
            child_id=child_id,
            goal_kind=ref.kind,
            goal_id=ref.goal_id,
            reason=reason,
            capacity_available=capacity_available,
            capacity_required=capacity_required,
            rule_version=rule_version,
            detail=detail,
            created_at=now or utc_now(),
        )


@dataclass(frozen=True)
class CapacityLedger:
    """What the family can carry this cycle, and what was placed into it.

    Family capacity is FINITE and declared by the family. A clinician adding
    an activity consumes it like anything else — section 18. Pretending
    otherwise is how a plan becomes something nobody can actually do.

    `overage` is recorded rather than resolved by removing something the
    family already received. A released cycle keeps what it was given; the
    NEXT cycle rebalances.
    """

    ledger_id: str
    cycle_id: str
    child_id: str
    family_declared_capacity: int
    allocated_by_planner: int = 0
    clinician_added: int = 0
    overage_reason: str = ""
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.SYSTEM_AUDIT

    def __post_init__(self) -> None:
        if self.family_declared_capacity < 0:
            raise AlignmentError("declared capacity cannot be negative")
        if self.allocated_by_planner < 0 or self.clinician_added < 0:
            raise AlignmentError("placed activity counts cannot be negative")

    @property
    def total_placed(self) -> int:
        return self.allocated_by_planner + self.clinician_added

    @property
    def overage(self) -> int:
        """How far past the family's declared capacity this cycle went.

        DERIVED, never stored. A stored overage can disagree with the counts
        it summarises, and then two fields in one record describe different
        weeks — the restated-constant failure 0.2 ruled against.
        """
        return max(0, self.total_placed - self.family_declared_capacity)

    @property
    def remaining(self) -> int:
        return max(0, self.family_declared_capacity - self.total_placed)

    @property
    def is_over_capacity(self) -> bool:
        return self.overage > 0

    @staticmethod
    def create(cycle_id: str, child_id: str, family_declared_capacity: int, *,
               allocated_by_planner: int = 0, clinician_added: int = 0,
               overage_reason: str = "",
               now: Optional[datetime] = None) -> "CapacityLedger":
        stamp = now or utc_now()
        return CapacityLedger(
            ledger_id=new_capacity_ledger_id(),
            cycle_id=cycle_id,
            child_id=child_id,
            family_declared_capacity=family_declared_capacity,
            allocated_by_planner=allocated_by_planner,
            clinician_added=clinician_added,
            overage_reason=overage_reason,
            created_at=stamp, updated_at=stamp,
        )

    def with_planner_allocation(self, count: int, *,
                                now: Optional[datetime] = None) -> "CapacityLedger":
        from dataclasses import replace

        return replace(self, allocated_by_planner=count,
                       updated_at=now or utc_now())

    def with_clinician_add(self, count: int = 1, *, reason: str = "",
                           now: Optional[datetime] = None) -> "CapacityLedger":
        """Record a clinician-added activity against family capacity."""
        from dataclasses import replace

        added = self.clinician_added + count
        projected = self.allocated_by_planner + added
        overage_reason = self.overage_reason
        if projected > self.family_declared_capacity and not overage_reason:
            overage_reason = reason or "clinician_added_beyond_declared_capacity"
        return replace(self, clinician_added=added,
                       overage_reason=overage_reason,
                       updated_at=now or utc_now())
