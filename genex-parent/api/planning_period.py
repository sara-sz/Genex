"""
api/planning_period.py — Planning period computation for the Genex API.

A Genex week is Monday through Sunday in the parent's local timezone.
The parent's timezone is provided by Lovable using:
  Intl.DateTimeFormat().resolvedOptions().timeZone  →  e.g. "America/Los_Angeles"

First plan behaviour:
  - Starts Monday → full week, plan_type="weekly", is_partial_week=False
  - Starts Thursday → partial plan (Thu–Sun), plan_type="starter_partial_week"
  - Starts Sunday → one-day period (Sun only), plan_type="starter_partial_week"

The scheduler generates a full Mon–Fri schedule (and possibly Sat/Sun).
adapt_weekly_plan() filters to only expose days in days_included that the
scheduler actually produced — no weekend days are fabricated.

Do NOT import from app.py or Streamlit.
"""

import uuid
from datetime import date, datetime, timedelta, timezone as dt_timezone
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
    _ZONEINFO_AVAILABLE = True
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore
    _ZONEINFO_AVAILABLE = False

# Day name order within a Genex week (Monday = index 0, Sunday = index 6)
WEEK_DAY_NAMES: List[str] = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]


def _local_date(tz_str: str, now_utc: datetime) -> date:
    """
    Return today's date in the given IANA timezone.
    Falls back to UTC if the timezone string is invalid or zoneinfo is unavailable.
    """
    if _ZONEINFO_AVAILABLE and tz_str:
        try:
            return now_utc.astimezone(ZoneInfo(tz_str)).date()
        except Exception:
            pass
    return now_utc.date()


def compute_plan_period(
    timezone_str: str,
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Compute the planning period for a new plan.

    Args:
      timezone_str : IANA timezone string from Lovable frontend,
                     e.g. "America/Los_Angeles". Falls back to UTC on error.
      now_utc      : override for testing; defaults to datetime.now(UTC).

    Returns a dict:
      plan_id         — uuid4 string (stable identifier for this plan version)
      plan_type       — "starter_partial_week" | "weekly"
      timezone        — IANA timezone string as-received from Lovable
      generated_at    — ISO-8601 UTC timestamp
      week_start_date — YYYY-MM-DD, Monday of the current local week
      plan_start_date — YYYY-MM-DD, today in local timezone (first day to show)
      plan_end_date   — YYYY-MM-DD, Sunday of the current local week
      days_included   — ordered list of day names from today through Sunday
      is_partial_week — True if plan does not start on Monday
    """
    if now_utc is None:
        now_utc = datetime.now(dt_timezone.utc)

    today = _local_date(timezone_str, now_utc)
    today_idx = today.weekday()  # Monday=0 … Sunday=6

    week_start = today - timedelta(days=today_idx)    # Monday of this week
    week_end = week_start + timedelta(days=6)          # Sunday of this week

    days_included: List[str] = WEEK_DAY_NAMES[today_idx:]  # today through Sunday
    is_partial = today_idx != 0

    return {
        "plan_id": str(uuid.uuid4()),
        "plan_type": "starter_partial_week" if is_partial else "weekly",
        "timezone": timezone_str,
        "generated_at": now_utc.isoformat(),
        "week_start_date": week_start.isoformat(),
        "plan_start_date": today.isoformat(),
        "plan_end_date": week_end.isoformat(),
        "days_included": days_included,
        "is_partial_week": is_partial,
    }


def next_week_available_from(base_plan_period: Dict[str, Any]) -> str:
    """Return the date (YYYY-MM-DD) on which the next week becomes available.

    Week 2 starts on the Monday after the base (Week 1) plan ends. The base plan
    ends on its plan_end_date (the Sunday of Week 1's calendar week). The Monday
    on/after the day after that Sunday is when Week 2 may be created.
    """
    base_end = date.fromisoformat(base_plan_period["plan_end_date"])
    day_after = base_end + timedelta(days=1)
    # Snap to the Monday on/after day_after (no-op when day_after is already Monday).
    week2_start = day_after + timedelta(days=(7 - day_after.weekday()) % 7)
    return week2_start.isoformat()


def compute_next_week_period(
    base_plan_period: Dict[str, Any],
    timezone_str: str,
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Compute the Week-2 plan period anchored to the Monday after Week 1 ends.

    Week 2 is always a full Monday–Sunday week (is_partial_week=False), regardless
    of whether Week 1 was partial. Carries additive markers (cycle_week=2,
    plan_type="next_week", base_plan_id, available_from) so Lovable can identify
    and gate the refresh.
    """
    if now_utc is None:
        now_utc = datetime.now(dt_timezone.utc)

    week2_start = date.fromisoformat(next_week_available_from(base_plan_period))
    week2_end = week2_start + timedelta(days=6)  # Sunday

    return {
        "plan_id": str(uuid.uuid4()),
        "plan_type": "next_week",
        "timezone": timezone_str,
        "generated_at": now_utc.isoformat(),
        "week_start_date": week2_start.isoformat(),
        "plan_start_date": week2_start.isoformat(),
        "plan_end_date": week2_end.isoformat(),
        "days_included": list(WEEK_DAY_NAMES),   # full Mon–Sun
        "is_partial_week": False,
        "cycle_week": 2,
        "base_plan_id": base_plan_period.get("plan_id") or "",
        "available_from": week2_start.isoformat(),
    }


def activity_date_for_day(week_start_date_str: str, day_name: str) -> str:
    """
    Return the ISO-8601 calendar date for a named weekday within the week
    whose Monday is week_start_date_str.

    Examples:
      activity_date_for_day("2026-06-15", "Monday")   → "2026-06-15"
      activity_date_for_day("2026-06-15", "Thursday")  → "2026-06-18"
      activity_date_for_day("2026-06-15", "Sunday")    → "2026-06-21"
    """
    try:
        day_idx = WEEK_DAY_NAMES.index(day_name)
    except ValueError:
        return week_start_date_str  # unknown day: fall back to Monday
    week_start = date.fromisoformat(week_start_date_str)
    return (week_start + timedelta(days=day_idx)).isoformat()
