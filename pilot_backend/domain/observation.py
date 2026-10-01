"""pilot_backend/domain/observation.py — what actually happened. No inference.

    ObservationEvent          one caregiver-recorded attempt, immutable
    ParentCustomizationSignal one plan edit the family made, immutable
    DeferRecord               Save for Later

## Four concepts that must never collapse into one

    scheduled   the planner placed an aligned activity
    attempted   the caregiver attempted it       -> ObservationEvent exists
    completed   the outcome was did_it           -> attempt_outcome
    improved    a clinician judged progress      -> NOT IN THIS LAYER

`did_it` means a caregiver pressed "did it". It does not mean mastery,
generalisation, independence or improvement, and nothing in this module
computes any of those. `ObservationEvent` carries no score, no progress field
and no mastery flag — there is nowhere for an inference to be written down.

`wasnt_ready_yet` and `didnt_want_to_try` are likewise observations, not
verdicts. They are distinct from each other because they are different facts
about a moment, and flattening them into "did not complete" throws away the
only part a clinician can act on.

## Attribution is by LOCAL DATE, never by cycle

A weekly cycle may span a month boundary: `Oct 26 – Nov 1` belongs to
October's planning sequence, but an attempt on Nov 1 belongs to November.

    owning_cycle_id   which plan the attempt came from
    attribution_month which month it counts toward, from the LOCAL date

Both are stored because they answer different questions, and `attribution_month`
is DERIVED from `local_date` at construction rather than passed in, so the two
cannot disagree. The timezone is the `MonthlyFocusPlan`'s timezone of record
and is required — there is no UTC fallback, because an hour's drift moves a
day across a month boundary and the monthly layer counts days into months.

## A customization signal is not performance

A family deferring, swapping or simplifying an activity is telling us about
the PLAN. Recording it as evidence about the child would turn "this week was
too busy" into "the child could not do it".
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import Enum
from typing import Optional

from .entities import SCHEMA_VERSION, utc_now
from .enums import Visibility
from .ids import (
    new_customization_signal_id,
    new_defer_record_id,
    new_observation_event_id,
)
from .monthly_plan import validate_timezone
from .roles import ActorRole


class ObservationError(ValueError):
    """Invalid observation, signal or defer record. PHI-safe."""

    PHI_SAFE_MESSAGE = True


class AttemptOutcome(str, Enum):
    """What happened when the activity was attempted.

    Three distinct facts, deliberately not collapsible into a boolean. None of
    them is a clinical judgement.
    """

    DID_IT = "did_it"
    WASNT_READY_YET = "wasnt_ready_yet"
    DIDNT_WANT_TO_TRY = "didnt_want_to_try"


class Difficulty(str, Enum):
    TOO_EASY = "too_easy"
    JUST_RIGHT = "just_right"
    TOO_HARD = "too_hard"


class Enjoyment(str, Enum):
    ENJOYED = "enjoyed"
    NEUTRAL = "neutral"
    DISLIKED = "disliked"


class TimezoneSource(str, Enum):
    """Where the timezone used for attribution came from.

    Recorded because attribution depends on it. `PLAN_OF_RECORD` is the only
    value the service writes; the others exist so a future import path has to
    declare what it used rather than leaving it implicit.
    """

    PLAN_OF_RECORD = "plan_of_record"
    CLIENT_SUPPLIED = "client_supplied"
    IMPORTED = "imported"


def attribution_month_for(local_date: str) -> str:
    """The `YYYY-MM` a local date counts toward.

    Derived, never supplied. A caller that could pass both a date and a month
    could pass a pair that disagree, and then the same attempt counts in two
    months or none.
    """
    try:
        parsed = date.fromisoformat(local_date)
    except ValueError:
        raise ObservationError("local_date must be an ISO date") from None
    return f"{parsed.year:04d}-{parsed.month:02d}"


@dataclass(frozen=True)
class ObservationEvent:
    """One caregiver-recorded attempt. Immutable. No clinical inference."""

    event_id: str
    child_id: str
    owning_cycle_id: str
    #: Derived from `local_date`. Never passed in independently.
    attribution_month: str
    local_date: str
    occurred_at: datetime
    timezone_of_record: str
    tz_source: TimezoneSource
    activity_instance_ref: str
    attempt_outcome: AttemptOutcome
    difficulty: Optional[Difficulty] = None
    enjoyment: Optional[Enjoyment] = None
    assistance: str = ""
    child_response: str = ""
    #: A REFERENCE to free text, never the text. Observation prose is clinical
    #: content and does not belong in a record this layer indexes and counts.
    observation_text_ref: str = ""
    source_feedback_id: str = ""
    recorded_by_caregiver_id: str = ""
    created_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.PARENT_VISIBLE

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_outcome, AttemptOutcome):
            raise ObservationError("attempt_outcome must be an AttemptOutcome")
        if self.attribution_month != attribution_month_for(self.local_date):
            raise ObservationError(
                "attribution_month must be derived from local_date")
        if self.occurred_at.tzinfo is None:
            raise ObservationError("refusing a naive observation timestamp")
        for label, value in (("child_id", self.child_id),
                             ("owning_cycle_id", self.owning_cycle_id),
                             ("activity_instance_ref", self.activity_instance_ref)):
            if not (value or "").strip():
                raise ObservationError(f"an observation requires {label}")

    @property
    def was_attempted(self) -> bool:
        """An event existing IS the attempt. Every outcome is an attempt."""
        return True

    @property
    def was_completed(self) -> bool:
        """Completed means `did_it`. It does NOT mean improved or mastered."""
        return self.attempt_outcome is AttemptOutcome.DID_IT

    @staticmethod
    def record(child_id: str, cycle_id: str, activity_instance_ref: str, *,
               local_date: str, occurred_at: datetime,
               timezone_of_record: str,
               attempt_outcome: AttemptOutcome,
               tz_source: TimezoneSource = TimezoneSource.PLAN_OF_RECORD,
               difficulty: Optional[Difficulty] = None,
               enjoyment: Optional[Enjoyment] = None,
               assistance: str = "", child_response: str = "",
               observation_text_ref: str = "", source_feedback_id: str = "",
               recorded_by_caregiver_id: str = "",
               now: Optional[datetime] = None) -> "ObservationEvent":
        zone = validate_timezone(timezone_of_record)
        return ObservationEvent(
            event_id=new_observation_event_id(),
            child_id=child_id,
            owning_cycle_id=cycle_id,
            attribution_month=attribution_month_for(local_date),
            local_date=local_date,
            occurred_at=occurred_at,
            timezone_of_record=zone,
            tz_source=tz_source,
            activity_instance_ref=activity_instance_ref,
            attempt_outcome=attempt_outcome,
            difficulty=difficulty,
            enjoyment=enjoyment,
            assistance=assistance,
            child_response=child_response,
            observation_text_ref=observation_text_ref,
            source_feedback_id=source_feedback_id,
            recorded_by_caregiver_id=recorded_by_caregiver_id,
            created_at=now or utc_now(),
        )


class CustomizationSignalType(str, Enum):
    """How the family changed their own plan.

    Evidence about the PLAN, never about the child. Each member is something
    a person did to a schedule.
    """

    DEFERRED = "deferred"
    REMOVED_FROM_WEEK = "removed_from_week"
    SWAPPED = "swapped"
    MADE_EASIER = "made_easier"
    MADE_HARDER = "made_harder"
    ADDED_BY_PARENT = "added_by_parent"
    #: Section 22. A parent declining a therapist-proposed change is a
    #: decision about a proposal — not a non-attempt, not a difficulty
    #: report, not an activity failure and not a clinical failure.
    PARENT_DECLINED_CHANGE = "parent_declined_change"


@dataclass(frozen=True)
class ParentCustomizationSignal:
    """One family edit to the plan. Immutable. Never child performance."""

    signal_id: str
    cycle_id: str
    child_id: str
    activity_instance_ref: str
    signal_type: CustomizationSignalType
    actor_id: str
    #: Where in the source system's overlay this came from. Provenance only.
    source_overlay_ref: str = ""
    created_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.SYSTEM_AUDIT

    #: Read by the adaptation engine. A plan edit is never a failure.
    not_a_failure = True

    def __post_init__(self) -> None:
        if not isinstance(self.signal_type, CustomizationSignalType):
            raise ObservationError("signal_type must be a CustomizationSignalType")
        if not (self.actor_id or "").strip():
            raise ObservationError("a customization signal requires an actor")

    @staticmethod
    def create(cycle_id: str, child_id: str, activity_instance_ref: str,
               signal_type: CustomizationSignalType, *, actor_id: str,
               source_overlay_ref: str = "",
               now: Optional[datetime] = None) -> "ParentCustomizationSignal":
        return ParentCustomizationSignal(
            signal_id=new_customization_signal_id(),
            cycle_id=cycle_id,
            child_id=child_id,
            activity_instance_ref=activity_instance_ref,
            signal_type=signal_type,
            actor_id=actor_id,
            source_overlay_ref=source_overlay_ref,
            created_at=now or utc_now(),
        )


@dataclass(frozen=True)
class DeferRecord:
    """Save for Later. A suppression window, not a retirement.

    The approved rule: a defer suppresses the activity for the NEXT weekly
    cycle. After that window the activity is ELIGIBLE again — and eligible is
    not the same as recommended. It may return if it fits; it is never
    permanently retired, and nothing here deletes it from the catalogue.

    `suppression_until_cycle` is a sequence number rather than a date, because
    the rule is expressed in cycles and a partial week is still a cycle.
    """

    defer_id: str
    child_id: str
    #: The instance the family deferred.
    activity_instance_ref: str
    #: The reusable activity behind it. This is what suppression matches on,
    #: because re-scheduling the same activity under a new instance id next
    #: week would otherwise walk straight past the defer.
    activity_identity_ref: str
    deferred_by_actor_id: str
    deferred_by_role: ActorRole
    from_cycle_id: str
    from_cycle_sequence: int
    suppression_until_cycle: int
    created_at: datetime = field(default_factory=utc_now)
    became_eligible_at: Optional[datetime] = None
    #: Set ONLY when a clinician explicitly overrode the suppression, with
    #: provenance. A system-only coverage floor must never fill this in.
    override_reason: str = ""
    overridden_by_actor_id: Optional[str] = None
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.SYSTEM_AUDIT

    not_a_failure = True

    def __post_init__(self) -> None:
        if self.from_cycle_sequence < 1:
            raise ObservationError("cycle sequences start at 1")
        if self.suppression_until_cycle <= self.from_cycle_sequence:
            raise ObservationError(
                "a defer must suppress at least the following cycle")
        if not (self.activity_identity_ref or "").strip():
            raise ObservationError(
                "a defer requires the activity identity it suppresses")

    def suppresses(self, cycle_sequence: int) -> bool:
        """Whether this defer suppresses the activity in the given cycle.

        The window is (from, until] — the cycle it was deferred FROM is
        already over, and the default `until = from + 1` suppresses exactly
        the next one.
        """
        return self.from_cycle_sequence < cycle_sequence <= self.suppression_until_cycle

    @staticmethod
    def create(child_id: str, activity_instance_ref: str,
               activity_identity_ref: str, *, actor_id: str,
               actor_role: ActorRole, from_cycle_id: str,
               from_cycle_sequence: int, suppress_for_cycles: int = 1,
               now: Optional[datetime] = None) -> "DeferRecord":
        if suppress_for_cycles < 1:
            raise ObservationError("a defer must suppress at least one cycle")
        return DeferRecord(
            defer_id=new_defer_record_id(),
            child_id=child_id,
            activity_instance_ref=activity_instance_ref,
            activity_identity_ref=activity_identity_ref,
            deferred_by_actor_id=actor_id,
            deferred_by_role=actor_role,
            from_cycle_id=from_cycle_id,
            from_cycle_sequence=from_cycle_sequence,
            suppression_until_cycle=from_cycle_sequence + suppress_for_cycles,
            created_at=now or utc_now(),
        )

    def with_clinician_override(self, *, actor_id: str, reason: str,
                                now: Optional[datetime] = None) -> "DeferRecord":
        """Record an explicit clinician override of the suppression.

        Requires a stated reason and an actor. There is no system path to
        this method — section 15 — so a coverage floor cannot reach it.
        """
        if not (reason or "").strip():
            raise ObservationError("overriding a defer requires a stated reason")
        if not (actor_id or "").strip():
            raise ObservationError("overriding a defer requires an actor")
        return replace(self, override_reason=reason,
                       overridden_by_actor_id=actor_id,
                       became_eligible_at=now or utc_now())

    @property
    def was_overridden(self) -> bool:
        return bool(self.override_reason and self.overridden_by_actor_id)
