"""pilot_backend/domain/weekly_cycle.py — the monthly layer's view of a week.

    WeeklyCycle         one week of the monthly sequence
    WeeklyPlanLink      the binding to a Parent-side plan, by EXTERNAL id
    WeeklyPlanSnapshot  what the parent-facing plan contained, frozen

## This is NOT the Parent weekly plan

`WeeklyCycle` does not replace, wrap or modify the frozen Parent weekly plan.
It is the monthly layer's own record of "week N of this focus plan", and it
carries no activity list, no schedule and no plan content.

The Parent plan is reached only through `WeeklyPlanLink.external_plan_id`,
which is an EXTERNAL identifier and never canonical — the same rule 0.4A set
for Parent session ids. Nothing here joins on it, and no pilot record is keyed
by it.

## Why the snapshot exists

Parent's customization overlay is unversioned: a plan the family sees today
can read differently tomorrow, and the change leaves no record of what was
replaced. That is fine for a weekly display and unusable as clinical evidence.

`WeeklyPlanSnapshot` captures the RESOLVED document — what the parent-facing
plan actually contained at the moment of capture — as an immutable copy. When
a therapist later asks "what was this family actually given in week 1?", the
answer is a stored document rather than a re-derivation that the overlay may
have since changed.

Snapshots are immutable and create-only. There is no update path, by design:
a snapshot that could be rewritten answers nothing.

## Cycles may cross a month boundary

A week is seven days; a month is not a whole number of weeks. `Oct 26 – Nov 1`
belongs to October's planning sequence and `spans_month_boundary` says so, but
an observation on Nov 1 is attributed to November — see `domain/observation.py`.
The cycle owns the plan; the local date owns the attribution. Conflating them
is how a month-end count double-counts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Mapping, Optional, Tuple

from .entities import SCHEMA_VERSION, utc_now
from .enums import Visibility
from .ids import (
    new_weekly_cycle_id,
    new_weekly_plan_link_id,
    new_weekly_plan_snapshot_id,
)
from .monthly_plan import MonthlyPlanError, month_bounds, validate_cycle_month
from .source_link import SourceSystem


class WeeklyCycleError(ValueError):
    """Invalid weekly-cycle construction or transition. PHI-safe."""

    PHI_SAFE_MESSAGE = True


class PartialReason(str, Enum):
    """Why a cycle is shorter than a full week.

    An enum rather than free text for the 0.4A reason: an unrecognised reason
    string would create a category nobody validates, and a partial week is an
    input to coverage decisions rather than a note.
    """

    MONTH_STARTS_MIDWEEK = "month_starts_midweek"
    MONTH_ENDS_MIDWEEK = "month_ends_midweek"
    PLAN_ACTIVATED_MIDWEEK = "plan_activated_midweek"


class GenerationReason(str, Enum):
    """Why this cycle was generated."""

    FIRST_CYCLE_OF_MONTH = "first_cycle_of_month"
    SEQUENTIAL = "sequential"
    REGENERATED_AFTER_INTERVENTION = "regenerated_after_intervention"


@dataclass(frozen=True)
class WeeklyCycle:
    """One week of one monthly focus plan."""

    cycle_id: str
    owning_focus_plan_id: str
    child_id: str
    sequence_in_month: int
    starts_on: str                        # ISO local date
    ends_on: str                          # ISO local date
    is_partial: bool = False
    partial_reason: Optional[PartialReason] = None
    spans_month_boundary: bool = False
    predecessor_cycle_id: Optional[str] = None
    generation_reason: GenerationReason = GenerationReason.SEQUENTIAL
    engine_version: str = ""
    #: Set when the parent-facing plan for this cycle has been released. Once
    #: set, therapist content changes must not rewrite it — see
    #: `planning/weekly_service.py` and section 17 of the 0.4D/E contract.
    released_to_parent_at: Optional[datetime] = None
    adaptation_record_id: Optional[str] = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.PARENT_VISIBLE

    def __post_init__(self) -> None:
        if self.sequence_in_month < 1:
            raise WeeklyCycleError("sequence_in_month starts at 1")
        if not (self.owning_focus_plan_id or "").strip():
            raise WeeklyCycleError("a weekly cycle requires an owning focus plan")
        if self.start_date > self.end_date:
            raise WeeklyCycleError("a cycle cannot end before it starts")
        if self.is_partial and self.partial_reason is None:
            raise WeeklyCycleError("a partial cycle must say why it is partial")
        if self.partial_reason is not None and not self.is_partial:
            raise WeeklyCycleError("a partial reason requires is_partial")

    @property
    def start_date(self) -> date:
        return date.fromisoformat(self.starts_on)

    @property
    def end_date(self) -> date:
        return date.fromisoformat(self.ends_on)

    @property
    def day_count(self) -> int:
        return (self.end_date - self.start_date).days + 1

    @property
    def is_released(self) -> bool:
        return self.released_to_parent_at is not None

    def local_dates(self) -> Tuple[str, ...]:
        """Every local date the cycle covers, in order."""
        start = self.start_date
        return tuple((start + timedelta(days=offset)).isoformat()
                     for offset in range(self.day_count))

    def covers(self, local_date: str) -> bool:
        return self.starts_on <= local_date <= self.ends_on

    @staticmethod
    def create(focus_plan_id: str, child_id: str, *, sequence_in_month: int,
               starts_on: str, ends_on: str,
               is_partial: bool = False,
               partial_reason: Optional[PartialReason] = None,
               predecessor_cycle_id: Optional[str] = None,
               generation_reason: GenerationReason = GenerationReason.SEQUENTIAL,
               engine_version: str = "",
               now: Optional[datetime] = None) -> "WeeklyCycle":
        stamp = now or utc_now()
        start, end = date.fromisoformat(starts_on), date.fromisoformat(ends_on)
        return WeeklyCycle(
            cycle_id=new_weekly_cycle_id(),
            owning_focus_plan_id=focus_plan_id,
            child_id=child_id,
            sequence_in_month=sequence_in_month,
            starts_on=starts_on,
            ends_on=ends_on,
            is_partial=is_partial,
            partial_reason=partial_reason,
            spans_month_boundary=(start.year, start.month) != (end.year, end.month),
            predecessor_cycle_id=predecessor_cycle_id,
            generation_reason=generation_reason,
            engine_version=engine_version,
            created_at=stamp, updated_at=stamp,
        )

    def release(self, *, now: Optional[datetime] = None) -> "WeeklyCycle":
        if self.is_released:
            raise WeeklyCycleError("this cycle has already been released")
        stamp = now or utc_now()
        return replace(self, released_to_parent_at=stamp, updated_at=stamp)

    def with_adaptation(self, record_id: str, *,
                        now: Optional[datetime] = None) -> "WeeklyCycle":
        return replace(self, adaptation_record_id=record_id,
                       updated_at=now or utc_now())


def plan_cycle_bounds(cycle_month: str, sequence_in_month: int, *,
                      week_length: int = 7) -> Tuple[str, str, bool,
                                                     Optional[PartialReason]]:
    """Bounds of cycle N of a month, clipped to the month at the START only.

    Cycle 1 begins on the first of the month and is SHORT when the month does
    not start on the chosen week boundary — that is the partial-week case in
    sections 24 and 25. Later cycles are full weeks and are allowed to run past
    the month end, which is what `spans_month_boundary` and the local-date
    attribution rule in `domain/observation.py` exist to handle.

    Returns (starts_on, ends_on, is_partial, partial_reason).
    """
    if sequence_in_month < 1:
        raise WeeklyCycleError("sequence_in_month starts at 1")
    if week_length < 1:
        raise WeeklyCycleError("a week must be at least one day")
    first, last = month_bounds(validate_cycle_month(cycle_month))

    # Cycle 1 runs from the 1st to the end of that calendar week (Sunday),
    # so a month beginning on a Thursday yields a four-day opening cycle and a
    # month beginning on a Sunday yields a one-day cycle.
    days_to_week_end = (6 - first.weekday()) % 7
    first_cycle_end = first + timedelta(days=days_to_week_end)

    if sequence_in_month == 1:
        partial = first_cycle_end != first + timedelta(days=week_length - 1)
        return (first.isoformat(), first_cycle_end.isoformat(), partial,
                PartialReason.MONTH_STARTS_MIDWEEK if partial else None)

    start = first_cycle_end + timedelta(days=1 + week_length * (sequence_in_month - 2))
    if start > last:
        raise WeeklyCycleError(
            f"cycle {sequence_in_month} starts after {cycle_month} ends")
    return (start.isoformat(),
            (start + timedelta(days=week_length - 1)).isoformat(), False, None)


def starter_cycle_bounds(first_plan_date: str, sequence_in_month: int, *,
                         week_length: int = 7
                         ) -> Tuple[str, str, bool, Optional[PartialReason]]:
    """Cycle bounds anchored to the child's FIRST PLAN DATE. The Genex rule.

    0.6A-2, restoring a FROZEN Genex invariant that this layer declared and
    never wired: `PartialReason.PLAN_ACTIVATED_MIDWEEK` has existed in this
    module since 0.4D and was referenced nowhere.

    ## The rule, as frozen in `genex-parent/api/planning_period.py`

        plan_start_date = the first-plan date itself
        plan_end_date   = the SUNDAY of that local week
        plan_type       = "starter_partial_week" unless it starts on a Monday
        week 2 onward   = Monday through Sunday

    So onboarding on Wednesday Oct 7 2026 yields Oct 7-11, not Oct 5-11.

    ## Why this is NOT `plan_cycle_bounds`

    `plan_cycle_bounds` anchors cycle 1 to the first of the MONTH and clips it
    to that week's Sunday. The shape is identical; the anchor is not. For a
    brand-new child the month's first day is not when the family entered the
    care loop, so anchoring there BACKDATES activities to before onboarding —
    which is exactly the defect this function exists to prevent.

    Both are kept. `plan_cycle_bounds` remains correct for a month-anchored
    sequence and is unchanged; this one is correct for a child's first plan.

    Cycle 1 is clipped at its START only. Later cycles are whole Monday weeks
    and may run past the month end, which is what `spans_month_boundary` and
    the local-date attribution rule already handle.
    """
    if sequence_in_month < 1:
        raise WeeklyCycleError("sequence_in_month starts at 1")
    if week_length < 1:
        raise WeeklyCycleError("a week must be at least one day")
    try:
        start = date.fromisoformat(first_plan_date)
    except ValueError:
        raise WeeklyCycleError(
            "a first plan date must be an ISO local date") from None

    # The Sunday of the first plan's own local week. `weekday()` is Monday=0,
    # so a Sunday start yields a ZERO-day offset — a one-day starter week,
    # which is the frozen rule's own stated case.
    days_to_sunday = (6 - start.weekday()) % 7
    starter_end = start + timedelta(days=days_to_sunday)

    if sequence_in_month == 1:
        partial = days_to_sunday != week_length - 1
        return (start.isoformat(), starter_end.isoformat(), partial,
                PartialReason.PLAN_ACTIVATED_MIDWEEK if partial else None)

    # Week 2 begins the Monday after the starter week's Sunday; every later
    # cycle is a whole week from there.
    week_start = starter_end + timedelta(
        days=1 + week_length * (sequence_in_month - 2))
    return (week_start.isoformat(),
            (week_start + timedelta(days=week_length - 1)).isoformat(),
            False, None)


def starter_sequence_for(first_plan_date: str, local_date: str, *,
                         week_length: int = 7) -> int:
    """Which starter-anchored cycle covers `local_date`.

    Returns 1 for any date inside the starter week. Refuses a date BEFORE the
    first plan: there is no cycle then, and answering 1 would place activities
    before the family entered the care loop.
    """
    if local_date < first_plan_date:
        raise WeeklyCycleError(
            "a local date before the first plan belongs to no cycle")
    sequence = 1
    while True:
        starts_on, ends_on, _partial, _reason = starter_cycle_bounds(
            first_plan_date, sequence, week_length=week_length)
        if starts_on <= local_date <= ends_on:
            return sequence
        sequence += 1
        if sequence > 400:  # pragma: no cover - a year of weeks is the bound
            raise WeeklyCycleError("no cycle covers this local date")


@dataclass(frozen=True)
class WeeklyPlanLink:
    """Binds a cycle to a plan in a source system, by external id."""

    link_id: str
    cycle_id: str
    source_system: SourceSystem
    #: EXTERNAL identifier. Provenance, never a canonical key, never joined on.
    external_plan_id: str
    linked_at: datetime = field(default_factory=utc_now)
    #: Local dates this external plan is understood to cover. Stored because a
    #: source plan's own week need not align with the monthly layer's cycle.
    coverage_local_dates: Tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.SYSTEM_AUDIT

    def __post_init__(self) -> None:
        if not (self.external_plan_id or "").strip():
            raise WeeklyCycleError("a plan link requires an external plan id")
        object.__setattr__(self, "coverage_local_dates",
                           tuple(self.coverage_local_dates))

    @staticmethod
    def create(cycle_id: str, source_system: SourceSystem,
               external_plan_id: str, *,
               coverage_local_dates: Tuple[str, ...] = (),
               now: Optional[datetime] = None) -> "WeeklyPlanLink":
        return WeeklyPlanLink(
            link_id=new_weekly_plan_link_id(),
            cycle_id=cycle_id,
            source_system=source_system,
            external_plan_id=external_plan_id,
            linked_at=now or utc_now(),
            coverage_local_dates=tuple(coverage_local_dates),
        )


@dataclass(frozen=True)
class WeeklyPlanSnapshot:
    """What the parent-facing plan contained. Immutable, create-only.

    `resolved_plan_document` is stored as a JSON string rather than a nested
    map because it is an OPAQUE capture of another system's document. Giving it
    a schema here would be a claim this layer cannot keep: Parent's plan shape
    is not ours to version, and a codec that validated its fields would start
    failing the moment Parent changed one.
    """

    snapshot_id: str
    cycle_id: str
    resolved_plan_document: str
    source_system: SourceSystem
    source_plan_id: str
    source_generated_at: Optional[datetime] = None
    captured_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    #: The family already has this content; the snapshot is a copy of it.
    VISIBILITY = Visibility.PARENT_VISIBLE

    def __post_init__(self) -> None:
        if not (self.resolved_plan_document or "").strip():
            raise WeeklyCycleError("a snapshot requires the resolved document")
        try:
            json.loads(self.resolved_plan_document)
        except ValueError:
            raise WeeklyCycleError(
                "resolved_plan_document must be a JSON document") from None

    def document(self) -> Any:
        """The captured document, parsed. Parsing never mutates the record."""
        return json.loads(self.resolved_plan_document)

    @staticmethod
    def capture(cycle_id: str, source_system: SourceSystem, source_plan_id: str,
                document: Mapping[str, Any], *,
                source_generated_at: Optional[datetime] = None,
                now: Optional[datetime] = None) -> "WeeklyPlanSnapshot":
        """Freeze a resolved plan document.

        `sort_keys` so the serialization is deterministic: two captures of the
        same document must compare equal, or "did the plan change?" becomes a
        question about dict ordering.
        """
        return WeeklyPlanSnapshot(
            snapshot_id=new_weekly_plan_snapshot_id(),
            cycle_id=cycle_id,
            resolved_plan_document=json.dumps(document, sort_keys=True,
                                              separators=(",", ":")),
            source_system=source_system,
            source_plan_id=source_plan_id,
            source_generated_at=source_generated_at,
            captured_at=now or utc_now(),
        )
