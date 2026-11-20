"""pilot_backend/domain/clinical_period.py — P0-C clinical validity of a plan.

A clinician-approved plan governs weekly cycles for a CLINICAL period anchored
on the clinician's ACTIVATION of that plan — never on the calendar month, and
never on a family's signup.

## The anchor: `MonthlyFocusPlan.activated_at`

Of the fields the model already stores, only `activated_at` records the
clinician's approval of the plan:

* `activate_plan` is gated by `_require_plan_author`, i.e. the child's
  managing clinician, and `MonthlyFocusPlan.activate` is the only writer of
  `activated_at`;
* `starts_on` / `ends_on` are the CALENDAR month's first and last day
  (`month_bounds(cycle_month)`), fixed at creation, unrelated to approval;
* cycle 1's `starts_on` is the first RELEASE date, which may follow
  activation by days when a plan is activated through the monthly-plan routes;
* `created_at` is a draft, not an approval.

The period's first day is `activated_at` as a LOCAL date in the plan's own
`timezone_of_record` (validated at creation, never defaulted). No schema field
is added: every active plan already carries both values, and a closed or draft
plan has no clinical period at all.

## The length

`CLINICAL_PERIOD_DAYS` (28) local days, first day inclusive — "about four
weeks" from activation. One constant for every plan, not a planning-policy
field, because the policy registry pins ALLOCATION defaults and this is a
pilot-wide governance rule.

## Containment, not just the start

A weekly cycle may belong to a plan only if EVERY local date of the cycle lies
inside the plan's period. Requiring only the start to be inside would let the
last week run up to six days past what the clinician approved; the stricter
rule never schedules an activity outside the approved period. For a plan
activated on Tue 2026-11-17 the period is 11-17 .. 12-14, so Weeks 1-4
(11-17 .. 12-13) fit and a Week 5 starting Mon 12-14 does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from .monthly_plan import validate_timezone

try:  # pragma: no cover - import guard mirrors monthly_plan
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

#: Clinical validity of one clinician-approved plan, in local days.
CLINICAL_PERIOD_DAYS = 28


class ClinicalPeriodError(ValueError):
    """A clinical period could not be derived. PHI-safe."""

    PHI_SAFE_MESSAGE = True


def local_date_in(timezone_of_record: str, instant: datetime) -> str:
    """The ISO local date of `instant` in a validated IANA zone.

    Refuses a naive instant rather than guessing its zone: a guess is exactly
    how a family's evening becomes the next calendar day.
    """
    if instant.tzinfo is None:
        raise ClinicalPeriodError("refusing a naive instant")
    zone = ZoneInfo(validate_timezone(timezone_of_record))
    return instant.astimezone(zone).date().isoformat()


@dataclass(frozen=True)
class ClinicalPeriod:
    """The inclusive local-date span a clinician-approved plan governs."""

    starts_on: str
    ends_on: str

    def contains(self, local_date: str) -> bool:
        return self.starts_on <= local_date <= self.ends_on

    def contains_span(self, starts_on: str, ends_on: str) -> bool:
        return self.starts_on <= starts_on and ends_on <= self.ends_on


def clinical_period_for(plan) -> Optional[ClinicalPeriod]:
    """The plan's clinical period, or None when it has none.

    Only an ACTIVE plan has one: a draft was never approved, and a closed plan
    was withdrawn. An active plan always carries `activated_at`; a missing one
    is refused rather than defaulted, because a period invented from another
    date would be a clinical approval nobody gave.
    """
    if not plan.is_active:
        return None
    if plan.activated_at is None:
        raise ClinicalPeriodError("an active plan has no activation instant")
    first = date.fromisoformat(
        local_date_in(plan.timezone_of_record, plan.activated_at))
    last = first + timedelta(days=CLINICAL_PERIOD_DAYS - 1)
    return ClinicalPeriod(starts_on=first.isoformat(), ends_on=last.isoformat())


__all__ = [
    "CLINICAL_PERIOD_DAYS",
    "ClinicalPeriod",
    "ClinicalPeriodError",
    "clinical_period_for",
    "local_date_in",
]
