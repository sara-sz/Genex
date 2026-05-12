"""
genex_core/scheduler.py
-----------------------
Weekly slot allocation and schedule builder.
Extracted from genex_interview_activity_v11.ipynb — logic unchanged.
"""

from typing import Any, Dict, List, Optional

from genex_core.config import DOMAIN_CONFIG
from genex_core.support_tiers import get_support_tier, determine_family_guidance_floor
from genex_core.scoring import get_effective_dev_age
from genex_core.safety import is_context_dependent_bonus_activity


def allocate_weekly_slots(state: Dict[str, Any]) -> Dict[str, Any]:
    """Allocate weekly minutes by category based on gaps and tiers."""
    child = state["child"]

    if "daily_time_min" not in state["child"]:
        raise ValueError("Missing daily_time_min in child profile.")

    chrono = min(child["chronological_months"], 60)
    daily_time_min = int(child["daily_time_min"])
    weekly_minutes = daily_time_min * 5

    supported_categories = []
    gap_by_category = {}
    weight_by_category = {}

    for category_key in DOMAIN_CONFIG:
        tier = get_support_tier(state, category_key)
        if tier == "no_special_support":
            continue

        supported_categories.append(category_key)

        dev_age = get_effective_dev_age(state, category_key)
        if dev_age is None:
            dev_age = state["dev_age"].get(category_key, chrono)
        gap = max(0, chrono - dev_age)
        gap_by_category[category_key] = gap

        if tier == "needs_special_support":
            weight_by_category[category_key] = max(1, gap)
        else:
            weight_by_category[category_key] = max(1, gap) * 0.5

    soft_floor = determine_family_guidance_floor(state)

    if not supported_categories and soft_floor.get("enabled"):
        category_key = soft_floor["category_key"]
        target_minutes = min(int(soft_floor.get("target_weekly_minutes", 20)), weekly_minutes)
        allocation = {
            "daily_time_min": daily_time_min,
            "weekly_minutes": weekly_minutes,
            "supported_categories": [category_key],
            "gap_by_category": {category_key: 0},
            "target_minutes_by_category": {category_key: target_minutes},
            "planning_mode": "family_guidance_floor",
        }
        state["weekly_slot_allocation"] = allocation
        return allocation

    if not supported_categories:
        allocation = {
            "daily_time_min": daily_time_min,
            "weekly_minutes": weekly_minutes,
            "supported_categories": [],
            "gap_by_category": {},
            "target_minutes_by_category": {},
            "planning_mode": "none",
        }
        state["weekly_slot_allocation"] = allocation
        return allocation

    base_minutes_per_category = max(5, daily_time_min // 2)
    target_minutes_by_category = {k: base_minutes_per_category for k in supported_categories}

    used_minutes = base_minutes_per_category * len(supported_categories)
    remaining_minutes = max(0, weekly_minutes - used_minutes)

    weights = weight_by_category.copy()
    total_weight = sum(weights.values())

    if total_weight > 0 and remaining_minutes > 0:
        for k in supported_categories:
            extra = round(remaining_minutes * (weights[k] / total_weight))
            target_minutes_by_category[k] += extra

    total_target = sum(target_minutes_by_category.values())
    while total_target > weekly_minutes:
        biggest = max(target_minutes_by_category, key=target_minutes_by_category.get)
        if target_minutes_by_category[biggest] > 5:
            target_minutes_by_category[biggest] -= 1
            total_target -= 1
        else:
            break

    allocation = {
        "daily_time_min": daily_time_min,
        "weekly_minutes": weekly_minutes,
        "supported_categories": supported_categories,
        "gap_by_category": gap_by_category,
        "target_minutes_by_category": target_minutes_by_category,
        "planning_mode": "tiered_support",
    }
    state["weekly_slot_allocation"] = allocation
    return allocation


def _pick_activity_that_fits(
    activities: List[Dict[str, Any]],
    used_indices: set,
    remaining_minutes: int,
) -> Optional[Dict[str, Any]]:
    candidates = []
    for idx, activity in enumerate(activities):
        if idx in used_indices:
            continue
        if activity.get("is_extended_activity", False):
            continue
        if is_context_dependent_bonus_activity(activity):
            continue
        duration = int(activity.get("duration_min", 5))
        if duration <= remaining_minutes:
            candidates.append((duration, idx, activity))

    if not candidates:
        return None

    candidates = sorted(candidates, key=lambda x: x[0])
    duration, idx, activity = candidates[0]
    used_indices.add(idx)
    return activity


def _pick_weekly_bonus_extended_activity(
    state: Dict[str, Any],
    extended_in_schedule_threshold: int = 15,
    bonus_extended_min_min: int = 30,
    bonus_extended_cap_min: int = 20,
) -> Optional[Dict[str, Any]]:
    daily_time_min = int(state["child"]["daily_time_min"])

    if daily_time_min >= extended_in_schedule_threshold:
        return None

    allocation = state.get("weekly_slot_allocation", {})
    gap_by_category = allocation.get("gap_by_category", {})

    candidate_categories = sorted(
        gap_by_category.keys(),
        key=lambda k: gap_by_category[k],
        reverse=True,
    )

    for category_key in candidate_categories:
        bank = state.get("activity_banks", {}).get(category_key, {})
        activities = bank.get("activities", [])

        for activity in activities:
            if not activity.get("is_extended_activity", False):
                continue
            duration = int(activity.get("duration_min", 5))
            if is_context_dependent_bonus_activity(activity):
                duration = max(duration, bonus_extended_min_min)
            if duration > bonus_extended_cap_min:
                continue

            return {
                "category_key": category_key,
                "category": DOMAIN_CONFIG[category_key]["display"],
                "title": activity.get("title"),
                "instructions": activity.get("instructions"),
                "duration_min": duration,
                "materials": activity.get("materials", "common household items"),
                "level": activity.get("level", "current_or_next"),
                "goal": activity.get("goal", get_support_tier(state, category_key)),
                "extended_reason": activity.get("extended_reason", ""),
            }

    return None


def _ensure_minimum_presence_for_monitor_categories(
    state: Dict[str, Any],
    days: Dict[str, Any],
) -> Dict[str, Any]:
    """Repair pass so monitor/enrich categories do not disappear entirely."""
    allocation = state.get("weekly_slot_allocation", {})
    target_minutes_by_category = allocation.get("target_minutes_by_category", {})
    daily_time_min = int(state["child"]["daily_time_min"])

    assigned_minutes = {k: 0 for k in target_minutes_by_category.keys()}
    for day_name, day_info in days.items():
        for item in day_info.get("items", []):
            k = item.get("category_key")
            if k in assigned_minutes:
                assigned_minutes[k] += int(item.get("duration_min", 0))

    for category_key in target_minutes_by_category.keys():
        tier = get_support_tier(state, category_key)
        if tier != "monitor_and_enrich":
            continue
        if assigned_minutes.get(category_key, 0) > 0:
            continue

        bank = state.get("activity_banks", {}).get(category_key, {})
        activities = bank.get("activities", [])
        if not activities:
            continue

        short_candidates = [a for a in activities if int(a.get("duration_min", 5)) <= 5]
        chosen = short_candidates[0] if short_candidates else min(
            activities, key=lambda a: int(a.get("duration_min", 5))
        )
        chosen_duration = int(chosen.get("duration_min", 5))

        placed = False
        for day_name, day_info in days.items():
            remaining = daily_time_min - int(day_info.get("total_minutes", 0))
            if remaining >= chosen_duration:
                day_info["items"].append({
                    "category_key": category_key,
                    "category": DOMAIN_CONFIG[category_key]["display"],
                    "title": chosen.get("title"),
                    "instructions": chosen.get("instructions"),
                    "duration_min": chosen_duration,
                    "materials": chosen.get("materials", "common household items"),
                    "level": chosen.get("level", "current_or_next"),
                    "goal": chosen.get("goal", "monitor_and_enrich"),
                })
                day_info["total_minutes"] += chosen_duration
                assigned_minutes[category_key] += chosen_duration
                placed = True
                break

        if placed:
            continue

        # Swap pass
        current_assigned = {k: 0 for k in target_minutes_by_category.keys()}
        for day_name, day_info in days.items():
            for item in day_info.get("items", []):
                k = item.get("category_key")
                if k in current_assigned:
                    current_assigned[k] += int(item.get("duration_min", 0))

        over_target_categories = [
            k for k in current_assigned.keys()
            if current_assigned[k] > target_minutes_by_category.get(k, 0)
        ]

        for day_name, day_info in days.items():
            day_items = day_info.get("items", [])
            for idx, item in enumerate(day_items):
                existing_key = item.get("category_key")
                existing_duration = int(item.get("duration_min", 0))
                if existing_key not in over_target_categories:
                    continue
                if existing_duration < chosen_duration:
                    continue
                day_items[idx] = {
                    "category_key": category_key,
                    "category": DOMAIN_CONFIG[category_key]["display"],
                    "title": chosen.get("title"),
                    "instructions": chosen.get("instructions"),
                    "duration_min": chosen_duration,
                    "materials": chosen.get("materials", "common household items"),
                    "level": chosen.get("level", "current_or_next"),
                    "goal": chosen.get("goal", "monitor_and_enrich"),
                }
                day_info["total_minutes"] = sum(int(x.get("duration_min", 0)) for x in day_items)
                break

    return days


def _build_weekend_days(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build Saturday and Sunday from the weekend-appropriate activity pool.

    Weekend activities are drawn from three pools (in priority order):
      1. Playdate / park / group / community activities
         (is_context_dependent_bonus_activity == True)
      2. Extended activities (is_extended_activity == True)
      3. Any activity with duration_min > 10

    Time budget: up to 25 min per weekend day, max 2 activities per day.
    Saturday gets the first picks; Sunday gets the next.
    If the pools are empty (rare), weekend days are left empty (rest days).
    """
    WEEKEND_BUDGET = 25  # minutes per weekend day
    MAX_ITEMS_PER_DAY = 2

    candidates = []
    for category_key, cfg in DOMAIN_CONFIG.items():
        bank = state.get("activity_banks", {}).get(category_key, {})
        for activity in bank.get("activities", []):
            duration = int(activity.get("duration_min", 5))
            is_playdate = is_context_dependent_bonus_activity(activity)
            is_extended = bool(activity.get("is_extended_activity", False))
            is_long = duration > 10

            if not (is_playdate or is_extended or is_long):
                continue

            # Priority score: playdate > extended > long
            priority = (2 if is_playdate else 1 if is_extended else 0)
            candidates.append({
                "category_key": category_key,
                "category": cfg["display"],
                "title": activity.get("title", "Activity"),
                "instructions": activity.get("instructions", ""),
                "duration_min": duration,
                "materials": activity.get("materials", "common household items"),
                "level": activity.get("level", "current_or_next"),
                "goal": activity.get("goal", ""),
                "is_playdate_type": is_playdate,
                "_priority": priority,
            })

    # Sort: playdate first, then extended, then by duration ascending
    candidates.sort(key=lambda a: (-a["_priority"], a["duration_min"]))

    saturday_items, sunday_items = [], []
    sat_time = sun_time = 0
    used = set()

    for i, activity in enumerate(candidates):
        duration = activity["duration_min"]
        if len(saturday_items) < MAX_ITEMS_PER_DAY and sat_time + duration <= WEEKEND_BUDGET:
            saturday_items.append({k: v for k, v in activity.items() if not k.startswith("_")})
            used.add(i)
            sat_time += duration
        elif len(sunday_items) < MAX_ITEMS_PER_DAY and sun_time + duration <= WEEKEND_BUDGET:
            sunday_items.append({k: v for k, v in activity.items() if not k.startswith("_")})
            used.add(i)
            sun_time += duration
        if len(saturday_items) >= MAX_ITEMS_PER_DAY and len(sunday_items) >= MAX_ITEMS_PER_DAY:
            break

    return {
        "Saturday": {"items": saturday_items, "total_minutes": sat_time, "is_weekend": True},
        "Sunday": {"items": sunday_items, "total_minutes": sun_time, "is_weekend": True},
    }


def build_weekly_schedule(state: Dict[str, Any]) -> Dict[str, Any]:
    """Build the 7-day weekly schedule from activity banks and slot allocations.

    Weekdays (Mon–Fri): short focused activities within daily_time_min budget.
    Weekend (Sat–Sun):  extended / playdate-type / longer activities (>10 min),
                        up to 25 min per day — separate from the weekday budget.
    """
    if "weekly_slot_allocation" not in state:
        allocate_weekly_slots(state)

    allocation = state["weekly_slot_allocation"]
    daily_time_min = allocation["daily_time_min"]
    target_minutes_by_category = allocation["target_minutes_by_category"]

    DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    days = {d: {"items": [], "total_minutes": 0, "is_weekend": False} for d in DAYS}

    soft_floor = state.get("family_guidance_floor", {})

    if not target_minutes_by_category:
        schedule = {
            "status": "no_special_support",
            "summary": "No categories need a scheduled weekly activity plan right now.",
            "days": days,
        }
        state["weekly_schedule"] = schedule
        return schedule

    assigned_minutes_by_category = {k: 0 for k in target_minutes_by_category.keys()}
    used_activity_indices = {k: set() for k in target_minutes_by_category.keys()}
    day_names = list(days.keys())

    progress_made = True
    while progress_made:
        progress_made = False

        categories_in_priority_order = sorted(
            target_minutes_by_category.keys(),
            key=lambda k: target_minutes_by_category[k] - assigned_minutes_by_category[k],
            reverse=True,
        )

        for day_name in day_names:
            remaining_day_minutes = daily_time_min - days[day_name]["total_minutes"]
            if remaining_day_minutes <= 0:
                continue

            for category_key in categories_in_priority_order:
                remaining_cat_minutes = (
                    target_minutes_by_category[category_key] - assigned_minutes_by_category[category_key]
                )
                if remaining_cat_minutes <= 0:
                    continue

                bank = state["activity_banks"].get(category_key, {})
                activities = bank.get("activities", [])
                if not activities:
                    continue

                activity = _pick_activity_that_fits(
                    activities=activities,
                    used_indices=used_activity_indices[category_key],
                    remaining_minutes=remaining_day_minutes,
                )

                if activity is None:
                    continue

                duration = int(activity.get("duration_min", 5))
                days[day_name]["items"].append({
                    "category_key": category_key,
                    "category": DOMAIN_CONFIG[category_key]["display"],
                    "title": activity.get("title"),
                    "instructions": activity.get("instructions"),
                    "duration_min": duration,
                    "materials": activity.get("materials", "common household items"),
                    "level": activity.get("level", "current_or_next"),
                    "goal": activity.get("goal", get_support_tier(state, category_key)),
                })
                days[day_name]["total_minutes"] += duration
                assigned_minutes_by_category[category_key] += duration
                progress_made = True
                break

    days = _ensure_minimum_presence_for_monitor_categories(state, days)

    # Add Saturday and Sunday with weekend-appropriate activities
    weekend_days = _build_weekend_days(state)
    days.update(weekend_days)

    summary_text = "Weekly schedule built from category activity banks."
    status_text = "success"
    if soft_floor.get("enabled"):
        status_text = "family_guidance_floor"
        summary_text = (
            soft_floor.get("summary", "")
            + " Weekly schedule built as low-intensity enrich-and-observe guidance."
        )

    schedule = {
        "status": status_text,
        "summary": summary_text,
        "daily_time_min": daily_time_min,
        "target_minutes_by_category": target_minutes_by_category,
        "assigned_minutes_by_category": assigned_minutes_by_category,
        "days": days,
        # weekly_bonus_activity kept for backward compatibility with text summary
        "weekly_bonus_activity": weekend_days["Saturday"]["items"][0]
            if weekend_days["Saturday"]["items"] else None,
    }
    state["weekly_schedule"] = schedule
    return schedule
