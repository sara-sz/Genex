"""
genex_core/scheduler.py
-----------------------
Weekly slot allocation and V22 schedule builder.

V22 update:
- build_weekly_schedule() is now cycle-week-aware (reads state["cycle_week"], default 1).
- Week 1 (cycle_week=1): unique core activities from the activity bank.
- Week 2 (cycle_week=2): repeat-adapt — same activities as Week 1, marked with
  repeat guidance (make_harder / make_easier depending on feedback signal).
- Weeks 3-4 (cycle_week=3|4): theme rotation — re-generates activity bank
  with week=cycle_week so _variant_theme() returns offset themes for novelty.
- All public APIs unchanged (allocate_weekly_slots, build_weekly_schedule).
- Weekend builder unchanged.
"""

from typing import Any, Dict, List, Optional

from genex_core.config import DOMAIN_CONFIG, V22_CYCLE_DAYS, V22_WEEK1_DAYS
from genex_core.support_tiers import get_support_tier, determine_family_guidance_floor
from genex_core.scoring import get_effective_dev_age
from genex_core.safety import is_context_dependent_bonus_activity


# ---------------------------------------------------------------------------
# Slot allocation (unchanged from pre-V22)
# ---------------------------------------------------------------------------

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
        # Use all focus domains (not just the top-ranked one) so multi-domain
        # profiles (e.g. language + movement) both get scheduled time.
        from genex_core.interview_engine import choose_focus_domains  # lazy to avoid circular
        focus_domains = choose_focus_domains(state) or [soft_floor["category_key"]]
        per_cat_minutes = max(5, weekly_minutes // max(1, len(focus_domains)))
        target_minutes_by_category = {ck: per_cat_minutes for ck in focus_domains}
        # Trim to weekly_minutes exactly
        total = sum(target_minutes_by_category.values())
        if total > weekly_minutes and focus_domains:
            target_minutes_by_category[focus_domains[0]] -= (total - weekly_minutes)
        allocation = {
            "daily_time_min": daily_time_min,
            "weekly_minutes": weekly_minutes,
            "supported_categories": focus_domains,
            "gap_by_category": {ck: 0 for ck in focus_domains},
            "target_minutes_by_category": target_minutes_by_category,
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


# ---------------------------------------------------------------------------
# Activity picking helpers (shared across weeks)
# ---------------------------------------------------------------------------

def _pick_activity_that_fits(
    activities: List[Dict[str, Any]],
    used_indices: set,
    remaining_minutes: int,
    used_keys: Optional[set] = None,
    used_families: Optional[set] = None,
    used_roots: Optional[set] = None,
    hard_block_families: Optional[set] = None,
) -> Optional[Dict[str, Any]]:
    """Return the shortest activity that fits in remaining_minutes.

    used_indices        — set of list positions already committed (mutated in-place).
    used_keys           — set of lowercase titles to pre-filter (hard block).
    hard_block_families — set of activity_family strings that must not appear today
                          (hard block — same-day dedup; never relaxed).
    used_families       — set of activity_family strings already used this week
                          (soft preference — week-level dedup; falls back if pool empty).
    used_roots          — set of normalized title roots already used this week
                          (soft preference — prevents near-duplicate titles).
    """
    if used_keys is None:
        used_keys = set()
    if hard_block_families is None:
        hard_block_families = set()
    if used_families is None:
        used_families = set()
    if used_roots is None:
        used_roots = set()
    candidates = []
    for idx, activity in enumerate(activities):
        if idx in used_indices:
            continue
        title_key = activity.get("title", "").strip().lower()
        if title_key in used_keys:
            continue
        # Hard block: never place same activity_family twice on the same day
        if hard_block_families and activity.get("activity_family") in hard_block_families:
            continue
        if activity.get("is_extended_activity", False):
            continue
        if is_context_dependent_bonus_activity(activity):
            continue
        duration = int(activity.get("duration_min", activity.get("duration_minutes", 5) or 5))
        if duration > remaining_minutes:
            continue
        candidates.append((duration, idx, activity))

    if not candidates:
        return None

    # Prefer candidates whose family and normalized root are not yet used this week.
    # Fall back to any fitting candidate only if all are "used" (bank exhausted).
    def _candidate_root(a):
        import re as _r
        t = a.get("title", "").lower()
        t = _r.sub(r"[^a-z0-9\s]", " ", t)
        t = _r.sub(
            r"\b(supported|slow|quick|simple|easy|gentle|basic|little|tiny|short|fun|"
            r"my|your|our|a|the|an)\b", "", t)
        t = _r.sub(
            r"\b(game|activity|practice|challenge|time|session|version|exercise)\b", "", t)
        return _r.sub(r"\s+", " ", t).strip()

    preferred = [
        (d, i, a) for d, i, a in candidates
        if (not a.get("activity_family") or a.get("activity_family") not in used_families)
        and _candidate_root(a) not in used_roots
    ]
    pool = preferred if preferred else candidates
    pool = sorted(pool, key=lambda x: x[0])
    duration, idx, activity = pool[0]
    used_indices.add(idx)
    return activity


def _get_core_activities(bank: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return only core-type activities from a bank (excludes extended/bonus)."""
    activities = bank.get("activities", [])
    core = [
        a for a in activities
        if not a.get("is_extended_activity", False)
        and not is_context_dependent_bonus_activity(a)
    ]
    return core


# ---------------------------------------------------------------------------
# V22: Week-cycle helpers
# ---------------------------------------------------------------------------

def _v22_repeat_adapt_item(item: Dict[str, Any], feedback: Dict[str, Any]) -> Dict[str, Any]:
    """Enrich a Week-1 schedule item for Week-2 repeat-adapt.

    Looks at feedback signals (too_easy → add make_harder cue,
    too_hard → add make_easier cue, just_right → repeat as-is).
    feedback: {category_key → {activity_title → {difficulty, performance, engagement}}}
    """
    category_key = item.get("category_key", "")
    title = item.get("title", "")

    fb = feedback.get(category_key, {}).get(title, {})
    difficulty = fb.get("difficulty", "just_right")
    performance = fb.get("performance", "")

    adapted = dict(item)
    adapted["cycle_week"] = 2
    adapted["is_repeat"] = True

    source_activity = item.get("_source_activity", {})

    if difficulty == "too_easy" or performance == "done_independently":
        cue = source_activity.get("make_harder") or item.get("make_harder", "")
        if cue:
            adapted["repeat_guidance"] = f"Try the harder version this week: {cue}"
            adapted["repeat_mode"] = "harder"
        else:
            adapted["repeat_guidance"] = "Try to make this slightly more challenging."
            adapted["repeat_mode"] = "harder"

    elif difficulty == "too_hard" or performance == "couldnt_do_it":
        cue = source_activity.get("make_easier") or item.get("make_easier", "")
        if cue:
            adapted["repeat_guidance"] = f"Try the easier version this week: {cue}"
            adapted["repeat_mode"] = "easier"
        else:
            adapted["repeat_guidance"] = "Offer more support this week — try a simpler version."
            adapted["repeat_mode"] = "easier"

    else:
        adapted["repeat_guidance"] = "Repeat this activity. Look for more confidence or independence."
        adapted["repeat_mode"] = "same"

    return adapted


def _v22_build_week2_schedule(
    state: Dict[str, Any],
    week1_schedule: Dict[str, Any],
) -> Dict[str, Any]:
    """Build Week 2 by repeating Week 1 activities with adapt cues."""
    feedback = state.get("activity_feedback", {})
    days_w1 = week1_schedule.get("days", {})
    days_w2 = {}

    for day_name, day_info in days_w1.items():
        new_items = []
        for item in day_info.get("items", []):
            new_items.append(_v22_repeat_adapt_item(item, feedback))
        days_w2[day_name] = {
            "items": new_items,
            "total_minutes": day_info.get("total_minutes", 0),
            "is_weekend": day_info.get("is_weekend", False),
            "cycle_week": 2,
        }

    return {
        "status": week1_schedule.get("status", "success"),
        "summary": "Week 2: Repeat and adapt activities from Week 1.",
        "daily_time_min": week1_schedule.get("daily_time_min"),
        "target_minutes_by_category": week1_schedule.get("target_minutes_by_category", {}),
        "assigned_minutes_by_category": week1_schedule.get("assigned_minutes_by_category", {}),
        "days": days_w2,
        "cycle_week": 2,
        "weekly_bonus_activity": week1_schedule.get("weekly_bonus_activity"),
    }


def _v22_ensure_theme_rotated_bank(
    state: Dict[str, Any],
    category_key: str,
    cycle_week: int,
) -> Dict[str, Any]:
    """Return a theme-rotated activity bank for Weeks 3-4.

    Calls generate_category_activity_bank with week=cycle_week so
    _variant_theme() uses offset=2 for novel themes/materials.
    Result is cached in state["activity_banks_w{cycle_week}"].
    """
    cache_key = f"activity_banks_w{cycle_week}"
    cached = state.get(cache_key, {}).get(category_key)
    if cached:
        return cached

    from genex_core.activity_engine import generate_category_activity_bank  # lazy
    bank = generate_category_activity_bank(state, category_key, week=cycle_week)
    state.setdefault(cache_key, {})[category_key] = bank
    return bank


# ---------------------------------------------------------------------------
# V22: Weekend builder (unchanged)
# ---------------------------------------------------------------------------

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
            duration = int(activity.get("duration_min", activity.get("duration_minutes", 5) or 5))
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

        short_candidates = [
            a for a in activities
            if int(a.get("duration_min", a.get("duration_minutes", 5) or 5)) <= 5
        ]
        chosen = short_candidates[0] if short_candidates else min(
            activities,
            key=lambda a: int(a.get("duration_min", a.get("duration_minutes", 5) or 5)),
        )
        chosen_duration = int(chosen.get("duration_min", chosen.get("duration_minutes", 5) or 5))

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
                day_info["total_minutes"] = sum(
                    int(x.get("duration_min", 0)) for x in day_items
                )
                break

    return days


def _build_weekend_days(state: Dict[str, Any]) -> Dict[str, Any]:
    """Build Saturday and Sunday from weekend-appropriate activity pool."""
    WEEKEND_BUDGET = 25
    MAX_ITEMS_PER_DAY = 2

    candidates = []
    for category_key, cfg in DOMAIN_CONFIG.items():
        bank = state.get("activity_banks", {}).get(category_key, {})
        for activity in bank.get("activities", []):
            duration = int(activity.get("duration_min", activity.get("duration_minutes", 5) or 5))
            is_playdate = is_context_dependent_bonus_activity(activity)
            is_extended = bool(activity.get("is_extended_activity", False))
            is_long = duration > 10

            if not (is_playdate or is_extended or is_long):
                continue

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


# ---------------------------------------------------------------------------
# V22: Main schedule builder (cycle-week-aware)
# ---------------------------------------------------------------------------

def build_weekly_schedule(state: Dict[str, Any]) -> Dict[str, Any]:
    """Build the 7-day weekly schedule.

    V22 cycle-week behavior:
      cycle_week=1  → unique core activities (standard)
      cycle_week=2  → repeat-adapt (same activities + adapt cues from feedback)
      cycle_week=3  → theme rotation (new themes, same bridge/family)
      cycle_week=4  → theme rotation (another new theme set)

    Weekdays (Mon–Fri): short focused activities within daily_time_min.
    Weekend (Sat–Sun): extended/playdate activities, up to 25 min/day.
    """
    cycle_week = int(state.get("cycle_week", 1))

    # Week 2: repeat Week 1 schedule with adapt cues
    if cycle_week == 2:
        week1 = state.get("week1_schedule") or state.get("weekly_schedule")
        if week1 and week1.get("days"):
            schedule = _v22_build_week2_schedule(state, week1)
            state["weekly_schedule"] = schedule
            return schedule
        # No week 1 stored — fall through to fresh week 1 build

    # Weeks 3-4: regenerate banks with theme rotation
    if cycle_week in (3, 4):
        for category_key in list(state.get("activity_banks", {}).keys()):
            rotated_bank = _v22_ensure_theme_rotated_bank(state, category_key, cycle_week)
            # Temporarily swap in theme-rotated bank for schedule building
            state.setdefault("_rotated_banks", {})[category_key] = rotated_bank
        # Swap banks temporarily
        _orig_banks = state.get("activity_banks", {})
        state["activity_banks"] = {
            k: state["_rotated_banks"].get(k, v)
            for k, v in _orig_banks.items()
        }

    # Standard week-build (used for weeks 1, 3, 4)
    # Guard against pre-initialised empty dict (init_state_from_profile sets {} by default)
    if not state.get("weekly_slot_allocation"):
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
            "cycle_week": cycle_week,
        }
        state["weekly_schedule"] = schedule
        return schedule

    assigned_minutes_by_category = {k: 0 for k in target_minutes_by_category.keys()}
    used_activity_indices = {k: set() for k in target_minutes_by_category.keys()}
    # Week-level title dedup per category — prevents the same title appearing on
    # multiple days within the same category (belt-and-suspenders on top of index dedup).
    used_activity_keys: Dict[str, set] = {k: set() for k in target_minutes_by_category.keys()}
    # Week-level normalized-root dedup per category — prevents near-duplicates
    # ("Squat and Reach" / "Supported Squat-and-Reach Game") from both appearing.
    used_activity_roots: Dict[str, set] = {k: set() for k in target_minutes_by_category.keys()}
    # Week-level activity_family dedup — at most one card per activity_family per week
    # per category (prevents "Undress the Teddy" + "Pull It Off!" both appearing).
    used_activity_families: Dict[str, set] = {k: set() for k in target_minutes_by_category.keys()}
    day_names = list(days.keys())

    import re as _sched_re

    def _sched_norm_root(t: str) -> str:
        t = t.lower()
        t = _sched_re.sub(r"[^a-z0-9\s]", " ", t)
        t = _sched_re.sub(
            r"\b(supported|slow|quick|simple|easy|gentle|basic|little|tiny|short|fun|"
            r"my|your|our|a|the|an)\b",
            "",
            t,
        )
        t = _sched_re.sub(
            r"\b(game|activity|practice|challenge|time|session|version|exercise)\b",
            "",
            t,
        )
        return _sched_re.sub(r"\s+", " ", t).strip()

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

                # Week 1: only core-type activities (no easier_backup / harder_stretch).
                # Easier/Stretch variants exist for Week-2 repeat-adapt only.
                if cycle_week == 1:
                    activities = [
                        a for a in bank.get("activities", [])
                        if a.get("_debug", {}).get("activity_type", "core") == "core"
                    ]
                else:
                    activities = bank.get("activities", [])

                if not activities:
                    continue

                # Combine week-level title set with titles already on today's schedule
                # (cross-category same-day uniqueness check).
                day_titles_placed = {
                    item.get("title", "").strip().lower()
                    for item in days[day_name]["items"]
                }
                # Also block same-day family duplicates across categories.
                day_families_placed = {
                    item.get("activity_family", "")
                    for item in days[day_name]["items"]
                    if item.get("activity_family")
                }
                combined_blocked = used_activity_keys[category_key] | day_titles_placed

                activity = _pick_activity_that_fits(
                    activities=activities,
                    used_indices=used_activity_indices[category_key],
                    remaining_minutes=remaining_day_minutes,
                    used_keys=combined_blocked,
                    hard_block_families=day_families_placed,
                    used_families=used_activity_families[category_key],
                    used_roots=used_activity_roots[category_key],
                )

                if activity is None:
                    continue

                # Update week-level trackers.
                _atitle = activity.get("title", "").strip().lower()
                _afam = activity.get("activity_family", "")
                _aroot = _sched_norm_root(_atitle)
                used_activity_keys[category_key].add(_atitle)
                if _afam:
                    used_activity_families[category_key].add(_afam)
                used_activity_roots[category_key].add(_aroot)

                duration = int(activity.get("duration_min", activity.get("duration_minutes", 5) or 5))
                slot = {
                    "category_key": category_key,
                    "category": DOMAIN_CONFIG[category_key]["display"],
                    "title": activity.get("title"),
                    "instructions": activity.get("instructions"),
                    "duration_min": duration,
                    "materials": activity.get("materials", "common household items"),
                    "level": activity.get("level", "current_or_next"),
                    "goal": activity.get("goal", get_support_tier(state, category_key)),
                    # V22 extras for feedback engine
                    # Note: activity_engine stores these as "easier"/"harder"; safety-replaced
                    # cards use "make_easier"/"make_harder".  Normalise here so the app
                    # always reads consistent keys.
                    "why": activity.get("why", ""),
                    # Normalise success: cards use either 'success_criteria' or 'success'
                    "success": activity.get("success_criteria") or activity.get("success", ""),
                    "make_easier": activity.get("make_easier") or activity.get("easier", ""),
                    "make_harder": activity.get("make_harder") or activity.get("harder", ""),
                    # Normalise avoid/group_play: cards use what_to_avoid / group_play_line
                    "avoid": activity.get("what_to_avoid") or activity.get("avoid", ""),
                    "group_play": activity.get("group_play_line") or activity.get("group_play", ""),
                    "feedback_options": activity.get("feedback_options", {}),
                    "activity_family": _afam,
                    "bridge_step": activity.get("bridge_step", ""),
                    "cycle_week": cycle_week,
                    # Store source for Week-2 repeat-adapt
                    "_source_activity": activity,
                }
                days[day_name]["items"].append(slot)
                days[day_name]["total_minutes"] += duration
                assigned_minutes_by_category[category_key] += duration
                progress_made = True
                break

    # Fill-up pass: top up any weekday that is still below daily_time_min.
    # Two explicit passes per day:
    #   Pass 1 (strict)  — respect week-level title/family/root uniqueness per category.
    #   Pass 2 (relaxed) — allow cross-day repeats; only block same-day duplicates.
    #                      Reset used_indices so the picker can revisit all cards.
    # This guarantees no silent rest days when the bank has enough cards to fill the day.
    def _do_fill_slot(day_name: str, strict: bool) -> bool:
        """Try to place one activity on day_name.  Returns True if placed."""
        remaining = daily_time_min - days[day_name]["total_minutes"]
        if remaining <= 0:
            return False
        day_titles_placed = {
            item.get("title", "").strip().lower()
            for item in days[day_name]["items"]
        }
        day_families_placed = {
            item.get("activity_family", "")
            for item in days[day_name]["items"]
            if item.get("activity_family")
        }
        for ck in list(target_minutes_by_category.keys()):
            bank = state["activity_banks"].get(ck, {})
            if cycle_week == 1:
                pool = [
                    a for a in bank.get("activities", [])
                    if a.get("_debug", {}).get("activity_type", "core") == "core"
                ]
            else:
                pool = bank.get("activities", [])
            if not pool:
                continue
            if strict:
                blocked = used_activity_keys[ck] | day_titles_placed
                idx_set = used_activity_indices[ck]
                week_fam_blocked = used_activity_families[ck]
                root_blocked = used_activity_roots[ck]
            else:
                blocked = day_titles_placed   # cross-day repeats allowed
                idx_set = set()               # revisit any card in the bank
                week_fam_blocked = set()
                root_blocked = set()
            activity = _pick_activity_that_fits(
                activities=pool,
                used_indices=idx_set,
                remaining_minutes=remaining,
                used_keys=blocked,
                hard_block_families=day_families_placed,   # hard: never same family today
                used_families=week_fam_blocked,            # soft: prefer variety across week
                used_roots=root_blocked,
            )
            if activity is None and not strict:
                # Last-resort relaxation: allow same-day family repeat rather than
                # repeating a title from another day.  Bank is genuinely exhausted of
                # cross-family options for this day.
                activity = _pick_activity_that_fits(
                    activities=pool,
                    used_indices=idx_set,
                    remaining_minutes=remaining,
                    used_keys=blocked,
                    hard_block_families=set(),   # lift family block as last resort
                    used_families=week_fam_blocked,
                    used_roots=root_blocked,
                )
            if activity is None:
                continue
            _ftitle = activity.get("title", "").strip().lower()
            _ffam = activity.get("activity_family", "")
            _froot = _sched_norm_root(_ftitle)
            used_activity_keys[ck].add(_ftitle)
            if _ffam:
                used_activity_families[ck].add(_ffam)
            used_activity_roots[ck].add(_froot)
            duration = int(activity.get("duration_min", activity.get("duration_minutes", 5) or 5))
            slot = {
                "category_key": ck,
                "category": DOMAIN_CONFIG[ck]["display"],
                "title": activity.get("title"),
                "instructions": activity.get("instructions"),
                "duration_min": duration,
                "materials": activity.get("materials", "common household items"),
                "level": activity.get("level", "current_or_next"),
                "goal": activity.get("goal", get_support_tier(state, ck)),
                "why": activity.get("why", ""),
                # Normalise success: cards use either 'success_criteria' or 'success'
                "success": activity.get("success_criteria") or activity.get("success", ""),
                "make_easier": activity.get("make_easier") or activity.get("easier", ""),
                "make_harder": activity.get("make_harder") or activity.get("harder", ""),
                # Normalise avoid/group_play: cards use what_to_avoid / group_play_line
                "avoid": activity.get("what_to_avoid") or activity.get("avoid", ""),
                "group_play": activity.get("group_play_line") or activity.get("group_play", ""),
                "feedback_options": activity.get("feedback_options", {}),
                "activity_family": _ffam,
                "bridge_step": activity.get("bridge_step", ""),
                "cycle_week": cycle_week,
                "_source_activity": activity,
            }
            days[day_name]["items"].append(slot)
            days[day_name]["total_minutes"] += duration
            assigned_minutes_by_category[ck] += duration
            return True
        return False

    for day_name in day_names:
        # Strict pass: preserve week-level uniqueness
        while days[day_name]["total_minutes"] < daily_time_min:
            if not _do_fill_slot(day_name, strict=True):
                break
        # Relaxed pass: allow cross-day repeats when pool is exhausted
        while days[day_name]["total_minutes"] < daily_time_min:
            if not _do_fill_slot(day_name, strict=False):
                break

    days = _ensure_minimum_presence_for_monitor_categories(state, days)

    # Restore original banks if we swapped for theme rotation
    if cycle_week in (3, 4) and "_rotated_banks" in state:
        state["activity_banks"] = {
            k: v for k, v in state["activity_banks"].items()
        }  # already has rotated; keep as-is for this cycle
        del state["_rotated_banks"]

    weekend_days = _build_weekend_days(state)
    days.update(weekend_days)

    cycle_label = {1: "Week 1 (new activities)", 2: "Week 2 (repeat & adapt)",
                   3: "Week 3 (fresh themes)", 4: "Week 4 (new themes)"}
    summary_label = cycle_label.get(cycle_week, f"Cycle week {cycle_week}")

    summary_text = f"{summary_label} — schedule built from activity banks."
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
        "cycle_week": cycle_week,
        "weekly_bonus_activity": weekend_days["Saturday"]["items"][0]
            if weekend_days["Saturday"]["items"] else None,
    }

    # Store week 1 for future week 2 repeat-adapt reference
    if cycle_week == 1:
        state["week1_schedule"] = schedule

    state["weekly_schedule"] = schedule
    return schedule
