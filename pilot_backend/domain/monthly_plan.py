"""pilot_backend/domain/monthly_plan.py — monthly direction, not monthly content.

    MonthlyFocusPlan       what this month is about, and in what context
    MonthlyGoalAllocation  which goals, at what priority and emphasis
    MonthlyGoalSnapshot    what the month was ACTUALLY working toward

## No activities live here

A `MonthlyFocusPlan` sets direction. It deliberately holds no activity list:
weekly plans stay adaptive, and four weeks of activities frozen on day one
would be a schedule pretending to be a plan. Weekly objects arrive in a later
slice and none exists yet.

## Weights are relative, and they are policy, not schema

`emphasis_weight` is a positive relative number, never a percentage. The
product defaults — primary 3, secondary 2 — live in `PlanningPolicyVersion`,
not in this module, precisely so a clinician can reprioritise, add a third
goal, or weight two goals equally without a schema change. Nothing here caps
the number of goals.

## Allocations are append-only

Reprioritising writes a successor row with `effective_from_cycle` and
`supersedes_allocation_id`; the prior allocation is retained. Which weighting
was in force during week 2 stays answerable rather than being overwritten by
week 3's decision.

## Snapshots exist so history cannot be rewritten

At activation, each active allocation is snapshotted with the goal's text AS
IT THEN READ. A clinician editing a goal in November must not silently change
what October was monitored against, and the snapshot is what makes that
impossible rather than merely discouraged.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import Enum
from typing import Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9+ always has it
    ZoneInfo = None  # type: ignore

from .entities import SCHEMA_VERSION, utc_now
from .enums import Visibility
from .goals import GoalKind, GoalRef
from .ids import new_allocation_id, new_focus_plan_id, new_goal_snapshot_id
from .roles import ActorRole

_CYCLE_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class MonthlyPlanState(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    CLOSED = "closed"


class AllocationStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"
    SUPERSEDED = "superseded"


class MonthlyPlanError(ValueError):
    """Invalid monthly-plan construction or transition.

    PHI-safe: names the rule, never goal text or a person.
    """

    PHI_SAFE_MESSAGE = True


class TimezoneError(MonthlyPlanError):
    """The timezone of record is missing or not a real IANA zone.

    A distinct type because this failure must never be resolved by defaulting.
    Parent falls back to UTC on an invalid zone, which is acceptable for a
    weekly display and is NOT acceptable here: a silent hour shift moves a day
    across a month boundary, and the monthly layer is the thing that counts
    days into months.
    """


def validate_timezone(tz_name: str) -> str:
    """Return a validated IANA zone name, or refuse. No UTC fallback."""
    name = (tz_name or "").strip()
    if not name:
        raise TimezoneError("a timezone of record is required")
    if ZoneInfo is None:  # pragma: no cover
        raise TimezoneError("timezone support is unavailable")
    try:
        ZoneInfo(name)
    except Exception:
        raise TimezoneError("not a recognised IANA timezone") from None
    return name


def validate_cycle_month(cycle_month: str) -> str:
    value = (cycle_month or "").strip()
    if not _CYCLE_MONTH.match(value):
        raise MonthlyPlanError("cycle_month must be YYYY-MM")
    return value


def month_bounds(cycle_month: str) -> Tuple[date, date]:
    """First and last calendar day of the cycle month, inclusive."""
    value = validate_cycle_month(cycle_month)
    year, month = int(value[:4]), int(value[5:7])
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


@dataclass(frozen=True)
class MonthlyFocusPlan:
    """One month of direction for one child."""

    focus_plan_id: str
    child_id: str
    cycle_month: str
    #: Validated IANA zone, snapshotted at creation. Never defaulted.
    timezone_of_record: str
    starts_on: str                          # ISO date
    ends_on: str                            # ISO date
    monitoring_focus: str = ""
    routines_context: Tuple[str, ...] = ()
    interests_motivators: Tuple[str, ...] = ()
    support_considerations: str = ""
    expected_practice_cadence: str = ""
    monitoring_dimensions: Tuple[str, ...] = ()
    clinician_guidance: str = ""
    state: MonthlyPlanState = MonthlyPlanState.DRAFT
    policy_version: str = ""
    current_revision_id: Optional[str] = None
    activated_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    #: Claim won at activation, proving the one-plan-per-child-month race.
    activation_claim_id: str = ""
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    created_by_actor_id: Optional[str] = None
    created_by_role: Optional[ActorRole] = None
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.PARENT_VISIBLE

    @property
    def is_active(self) -> bool:
        return self.state is MonthlyPlanState.ACTIVE and self.closed_at is None

    @staticmethod
    def create(child_id: str, cycle_month: str, timezone_of_record: str, *,
               monitoring_focus: str = "",
               routines_context: Tuple[str, ...] = (),
               interests_motivators: Tuple[str, ...] = (),
               support_considerations: str = "",
               expected_practice_cadence: str = "",
               monitoring_dimensions: Tuple[str, ...] = (),
               clinician_guidance: str = "",
               policy_version: str = "",
               actor_id: Optional[str] = None,
               actor_role: Optional[ActorRole] = None,
               now: Optional[datetime] = None) -> "MonthlyFocusPlan":
        if not (child_id or "").strip():
            raise MonthlyPlanError("a monthly focus plan requires a child id")
        month = validate_cycle_month(cycle_month)
        zone = validate_timezone(timezone_of_record)
        first, last = month_bounds(month)
        stamp = now or utc_now()
        return MonthlyFocusPlan(
            focus_plan_id=new_focus_plan_id(),
            child_id=child_id,
            cycle_month=month,
            timezone_of_record=zone,
            starts_on=first.isoformat(),
            ends_on=last.isoformat(),
            monitoring_focus=monitoring_focus,
            routines_context=tuple(routines_context),
            interests_motivators=tuple(interests_motivators),
            support_considerations=support_considerations,
            expected_practice_cadence=expected_practice_cadence,
            monitoring_dimensions=tuple(monitoring_dimensions),
            clinician_guidance=clinician_guidance,
            policy_version=policy_version,
            created_at=stamp, updated_at=stamp,
            created_by_actor_id=actor_id, created_by_role=actor_role,
        )

    def activate(self, *, claim_id: str,
                 now: Optional[datetime] = None) -> "MonthlyFocusPlan":
        if self.state is not MonthlyPlanState.DRAFT:
            raise MonthlyPlanError("only a draft plan can be activated")
        stamp = now or utc_now()
        return replace(self, state=MonthlyPlanState.ACTIVE, activated_at=stamp,
                       activation_claim_id=claim_id, updated_at=stamp)

    def close(self, *, now: Optional[datetime] = None) -> "MonthlyFocusPlan":
        if self.state is not MonthlyPlanState.ACTIVE:
            raise MonthlyPlanError("only an active plan can be closed")
        stamp = now or utc_now()
        return replace(self, state=MonthlyPlanState.CLOSED, closed_at=stamp,
                       updated_at=stamp)


@dataclass(frozen=True)
class MonthlyGoalAllocation:
    """Which goal, at what priority and relative emphasis, from when."""

    allocation_id: str
    focus_plan_id: str
    child_id: str
    goal_kind: GoalKind
    goal_id: str
    priority_rank: int
    emphasis_weight: int
    min_coverage_per_cycle: int = 1
    status: AllocationStatus = AllocationStatus.ACTIVE
    set_by_actor_id: Optional[str] = None
    set_by_role: Optional[ActorRole] = None
    #: Weekly-cycle sequence from which this allocation applies. 1 means "from
    #: the start of the month". Weekly cycles do not exist yet; the field is
    #: here so a mid-month reprioritisation has somewhere truthful to land.
    effective_from_cycle: int = 1
    supersedes_allocation_id: Optional[str] = None
    superseded_by_allocation_id: Optional[str] = None
    reason: str = ""
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.SYSTEM_AUDIT

    @property
    def goal_ref(self) -> GoalRef:
        return GoalRef(self.goal_kind, self.goal_id)

    @property
    def is_active(self) -> bool:
        return self.status is AllocationStatus.ACTIVE

    def __post_init__(self) -> None:
        if self.priority_rank < 1:
            raise MonthlyPlanError("priority rank starts at 1")
        if self.emphasis_weight <= 0:
            raise MonthlyPlanError("emphasis weight must be positive")
        if self.min_coverage_per_cycle < 0:
            raise MonthlyPlanError("minimum coverage cannot be negative")
        if self.effective_from_cycle < 1:
            raise MonthlyPlanError("effective_from_cycle starts at 1")

    @staticmethod
    def create(focus_plan_id: str, child_id: str, ref: GoalRef, *,
               priority_rank: int, emphasis_weight: int,
               min_coverage_per_cycle: int = 1,
               actor_id: Optional[str] = None,
               actor_role: Optional[ActorRole] = None,
               effective_from_cycle: int = 1,
               supersedes_allocation_id: Optional[str] = None,
               reason: str = "",
               now: Optional[datetime] = None) -> "MonthlyGoalAllocation":
        stamp = now or utc_now()
        return MonthlyGoalAllocation(
            allocation_id=new_allocation_id(),
            focus_plan_id=focus_plan_id,
            child_id=child_id,
            goal_kind=ref.kind,
            goal_id=ref.goal_id,
            priority_rank=priority_rank,
            emphasis_weight=emphasis_weight,
            min_coverage_per_cycle=min_coverage_per_cycle,
            set_by_actor_id=actor_id,
            set_by_role=actor_role,
            effective_from_cycle=effective_from_cycle,
            supersedes_allocation_id=supersedes_allocation_id,
            reason=reason,
            created_at=stamp, updated_at=stamp,
        )

    def supersede(self, successor_allocation_id: str, *,
                  now: Optional[datetime] = None) -> "MonthlyGoalAllocation":
        """Mark this allocation replaced. The row is retained, never edited away."""
        if not self.is_active:
            raise MonthlyPlanError("only an active allocation can be superseded")
        stamp = now or utc_now()
        return replace(self, status=AllocationStatus.SUPERSEDED,
                       superseded_by_allocation_id=successor_allocation_id,
                       updated_at=stamp)

    def with_status(self, status: AllocationStatus, *,
                    now: Optional[datetime] = None) -> "MonthlyGoalAllocation":
        return replace(self, status=status, updated_at=now or utc_now())


@dataclass(frozen=True)
class MonthlyGoalSnapshot:
    """What the month was actually working toward. Immutable."""

    snapshot_id: str
    focus_plan_id: str
    child_id: str
    goal_kind: GoalKind
    goal_id: str
    goal_version_id: str
    #: The wording as it read at activation. A later edit cannot reach this.
    goal_text_at_snapshot: str
    priority_rank: int
    emphasis_weight: int
    min_coverage_per_cycle: int
    approved_by_role: ActorRole
    allocation_id: str
    effective_from: datetime = field(default_factory=utc_now)
    snapshot_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.SYSTEM_AUDIT

    @property
    def goal_ref(self) -> GoalRef:
        return GoalRef(self.goal_kind, self.goal_id)

    @staticmethod
    def create(focus_plan_id: str, child_id: str, ref: GoalRef, *,
               goal_version_id: str, goal_text: str, priority_rank: int,
               emphasis_weight: int, min_coverage_per_cycle: int,
               approved_by_role: ActorRole, allocation_id: str,
               effective_from: Optional[datetime] = None,
               now: Optional[datetime] = None) -> "MonthlyGoalSnapshot":
        stamp = now or utc_now()
        if not (goal_text or "").strip():
            raise MonthlyPlanError("a snapshot requires the goal text")
        return MonthlyGoalSnapshot(
            snapshot_id=new_goal_snapshot_id(),
            focus_plan_id=focus_plan_id,
            child_id=child_id,
            goal_kind=ref.kind,
            goal_id=ref.goal_id,
            goal_version_id=goal_version_id,
            goal_text_at_snapshot=goal_text,
            priority_rank=priority_rank,
            emphasis_weight=emphasis_weight,
            min_coverage_per_cycle=min_coverage_per_cycle,
            approved_by_role=approved_by_role,
            allocation_id=allocation_id,
            effective_from=effective_from or stamp,
            snapshot_at=stamp,
        )
