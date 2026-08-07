"""Weekday labels for parent-facing plan reads (Phase 1B.2E).

`scheduled_day` / `destination_scheduled_day` are zero-based integers, 0 = Monday.
An integer is the right storage form, but a parent reading "the therapist wants to
add something to day 1" would have to know the convention. These labels are the
presentation half of that, kept in one place so list and detail cannot disagree.

Display strings only — never a stored value, never an identifier, and never used
to decide anything. Ordering and every invariant continue to key off the integer.
"""

from __future__ import annotations

from typing import Optional

#: 0 = Monday .. 6 = Sunday, matching `PlanAssignment.scheduled_day`.
WEEKDAY_LABELS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

MIN_WEEKDAY = 0
MAX_WEEKDAY = len(WEEKDAY_LABELS) - 1


def is_valid_weekday(day: object) -> bool:
    """True for an int in 0..6. Booleans are rejected — `True` is not Tuesday."""
    if not isinstance(day, int) or isinstance(day, bool):
        return False
    return MIN_WEEKDAY <= day <= MAX_WEEKDAY


def day_label(day: object) -> Optional[str]:
    """Presentable weekday name, or None when `day` is not a valid weekday.

    Returns None rather than raising: callers are read paths that must fail closed
    by dropping an item, never by breaking a whole list for one bad record.
    """
    return WEEKDAY_LABELS[day] if is_valid_weekday(day) else None
