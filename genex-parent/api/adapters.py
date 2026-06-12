"""
api/adapters.py — Input sanitisation and output normalisation for the Genex API.

Responsibilities:
  - normalize_diagnosis_for_brain(): map exact Lovable dropdown values to what
    init_state_from_profile() expects. Never passes unknown values to the brain.
  - sanitize_concern(): strip the child's name from the parent concern text before
    it is stored in GCS or passed to the brain (which may call OpenAI).
  - adapt_weekly_plan(): convert brain weekly_schedule into the frontend-ready plan
    response, including API-layer weekend cards when the scheduler has not produced
    Saturday/Sunday activities.
  - build_plan_internal(): capture rich internal metadata from each activity slot
    for GCS storage, powering future continuous planning and feedback loops.

Weekend card generation (API layer only — genex_core not touched):
  The current scheduler generates Monday–Friday activities.
  Saturday and Sunday cards are derived at the API layer from already-approved
  weekday activities so that parents who have more time on weekends still get a
  useful plan for those days.

  Saturday → "family_practice": repeat-and-generalise framing, spread across domains.
  Sunday   → "playdate_sibling_practice" when the source slot has group_play content;
             otherwise "light_review".

  Weekend cards preserve all domain, milestone, bridge-step, safety, and success
  metadata from their source. They are marked with:
    source_bank_type: "weekend_practice"
    weekend_mode:     "family_practice" | "playdate_sibling_practice" | "light_review"

Do NOT import from app.py or Streamlit.
"""

import copy
import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from api.planning_period import activity_date_for_day, WEEK_DAY_NAMES

# ── Diagnosis mapping ──────────────────────────────────────────────────────

# The exact set of values the Lovable dropdown sends.
# Any other value should be rejected at the Pydantic schema level (422).
VALID_FRONTEND_DIAGNOSES: frozenset = frozenset({
    "No known diagnosis / not sure",
    "Down syndrome",
    "ADHD",
    "Autism spectrum",
    "Other",
    "Prefer not to say",
})

# Values that mean "no diagnosis" for the brain.
# init_state_from_profile() accepts str(diagnosis or "") == "" cleanly —
# the concern_router does keyword-only matching and "" produces no diagnosis hits.
# The Streamlit app passes "not specified" for blank free-text input, but since
# we have structured dropdown values, "" is cleaner and equivalent.
_NO_DIAGNOSIS_VALUES: frozenset = frozenset({
    "No known diagnosis / not sure",
    "Prefer not to say",
})


def normalize_diagnosis_for_brain(frontend_value: str) -> str:
    """
    Map a Lovable frontend dropdown value to the string the brain expects.

    Returns:
      - "" (empty string) for "No known diagnosis / not sure" and "Prefer not to say"
      - The original value unchanged for "Down syndrome", "ADHD",
        "Autism spectrum", "Other"

    The brain's concern_router() does keyword matching on the combined
    "{diagnosis} | {concern}" text. An empty diagnosis string is the correct
    representation of no diagnosis — it contributes no keyword matches.

    Raises:
      ValueError if the value is not in VALID_FRONTEND_DIAGNOSES. This should
      never occur in practice because the Pydantic schema rejects unknown values
      with 422 before this function is called.
    """
    if frontend_value not in VALID_FRONTEND_DIAGNOSES:
        raise ValueError(
            f"Unknown diagnosis value {frontend_value!r}. "
            f"Valid values: {sorted(VALID_FRONTEND_DIAGNOSES)}"
        )
    if frontend_value in _NO_DIAGNOSIS_VALUES:
        return ""
    return frontend_value


# ── Concern text sanitisation ──────────────────────────────────────────────

def sanitize_concern(concern: str, child_name: Optional[str]) -> str:
    """
    Replace occurrences of the child's name in the parent concern text with
    "your child". This prevents the child's name from being stored in GCS or
    forwarded to OpenAI (via the concern router and activity engine).

    Rules:
      - Case-insensitive match on the exact name token.
      - Possessive form handled first: "Maya's" → "your child's"
      - Plain name: "Maya" → "your child"
      - Word-boundary matching only (avoids partial matches like "Amanda" → "your childnda")

    If child_name is blank or None, the concern is returned unchanged.

    Examples:
      sanitize_concern("Maya is not walking yet and Maya has few words.", "Maya")
      → "your child is not walking yet and your child has few words."

      sanitize_concern("Maya's speech is behind.", "Maya")
      → "your child's speech is behind."
    """
    if not child_name or not child_name.strip():
        return concern

    name = re.escape(child_name.strip())

    # Possessive first (must come before plain-name replacement)
    concern = re.sub(
        rf"\b{name}'s\b",
        "your child's",
        concern,
        flags=re.IGNORECASE,
    )

    # Plain name
    concern = re.sub(
        rf"\b{name}\b",
        "your child",
        concern,
        flags=re.IGNORECASE,
    )

    return concern


# ── Plan response adaptation ───────────────────────────────────────────────

# All scheduler-generated weekday names (for weekend card source collection)
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

# Full seven-day week names
_ALL_DAYS = WEEK_DAY_NAMES  # ["Monday", ..., "Sunday"]

# Domain label mapping — brain key → parent-friendly label
DOMAIN_LABELS: Dict[str, str] = {
    "language_and_communication": "Talking and Communicating",
    "movement_and_physical": "Movement & Physical",
    "social_and_emotional": "Social & Emotional",
    "cognitive": "Learning & Cognitive",
}


def _daily_card_count(daily_time_minutes: int) -> int:
    """Mirror of scheduler._max_cards_per_day() — never import private functions."""
    if daily_time_minutes < 10:
        return 1
    if daily_time_minutes < 30:
        return 2
    return 3


def _deterministic_activity_id(session_id: str, day: str, slot_index: int, title: str) -> str:
    """
    Generate a stable UUID for an activity slot.
    Same session + day + slot_index + title always produces the same UUID.
    Uses uuid5 (SHA-1 namespace hash) so the ID is both deterministic and UUID-shaped.
    """
    key = f"{session_id}:{day}:{slot_index}:{title or ''}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def _normalize_slot(
    slot: Dict[str, Any],
    session_id: str,
    day: str,
    slot_index: int,
) -> Dict[str, Any]:
    """
    Convert one scheduler slot dict into a frontend-ready activity dict.

    The scheduler already normalises most fields (lines 760-769 in scheduler.py):
      success_criteria/success → "success"
      make_easier/easier       → "make_easier"
      make_harder/harder       → "make_harder"
      what_to_avoid/avoid      → "avoid"
      group_play_line/group_play → "group_play"

    This adapter:
      1. Handles any residual alternative names the scheduler may not have caught.
      2. Renames "success" → "success_criteria" for the frontend contract.
      3. Strips all internal fields (_source_activity, level, goal, etc.).
      4. Adds deterministic id, domain, domain_label.
      5. Guarantees every frontend field is a non-null string.
      6. Passes through weekend_mode when present (set by _make_weekend_slot).
    """
    def _get(*keys: str) -> str:
        """Return first non-empty value across candidate keys, or ''."""
        for k in keys:
            v = slot.get(k)
            if v and str(v).strip():
                return str(v).strip()
        return ""

    domain = slot.get("category_key", "")
    title = slot.get("title") or ""

    result: Dict[str, Any] = {
        "id": _deterministic_activity_id(session_id, day, slot_index, title),
        "title": title,
        "domain": domain,
        "domain_label": DOMAIN_LABELS.get(domain, domain),
        "duration_label": "5–15 min",
        # Content fields — the scheduler already normalised most of these;
        # the extra candidate keys below catch any residual naming variants.
        "why": _get("why", "why_this_works", "extended_reason"),
        "instructions": _get("instructions", "how_to_do_it", "steps"),
        "materials": _get("materials", "what_you_need"),
        # scheduler writes "success" (normalised from success_criteria/success);
        # the frontend contract calls this field "success_criteria".
        "success_criteria": _get("success", "success_criteria"),
        "make_easier": _get("make_easier", "easier"),
        "make_harder": _get("make_harder", "harder", "stretch"),
        "group_play": _get("group_play", "group_play_line", "sibling_friend"),
        "avoid": _get("avoid", "what_to_avoid", "safety_avoid"),
    }

    # Pass through weekend_mode if set (populated by _make_weekend_slot)
    wm = slot.get("weekend_mode")
    if wm:
        result["weekend_mode"] = wm

    return result


# ── Weekend card generation ────────────────────────────────────────────────

def _collect_weekday_slots(weekly_schedule: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Return all scheduler-produced items from Monday–Friday, in day order.
    Used as the source pool for API-layer weekend card generation.
    """
    slots: List[Dict[str, Any]] = []
    for day in _WEEKDAYS:
        day_info = weekly_schedule.get("days", {}).get(day, {})
        for slot in day_info.get("items", []):
            slots.append(slot)
    return slots


def _select_for_weekend(
    all_slots: List[Dict[str, Any]],
    card_count: int,
    prefer_group_play: bool = False,
    exclude_slots: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Select up to `card_count` slots from weekday activities for a weekend day.

    Selection strategy:
      1. If prefer_group_play=True, move slots with non-empty group_play to the front.
      2. Greedily pick by activity_family variety (one per family first).
      3. Fill remaining slots with any not-yet-selected cards.
      4. If exclude_slots given, prefer cards not in that set (for Sat/Sun variety).

    Returns at most card_count slots (may be fewer if pool is small).
    """
    if not all_slots or card_count <= 0:
        return []

    excluded_ids: set = set()
    if exclude_slots:
        for s in exclude_slots:
            dbg = s.get("_source_activity", {}).get("_debug", {})
            aid = dbg.get("activity_id") or id(s)
            excluded_ids.add(aid)

    def _slot_sort_key(s: Dict[str, Any]) -> Tuple[int, int]:
        dbg = s.get("_source_activity", {}).get("_debug", {})
        aid = dbg.get("activity_id") or ""
        in_excluded = 1 if aid in excluded_ids else 0
        has_gp = 0 if (s.get("group_play") or s.get("group_play_line") or
                       s.get("_source_activity", {}).get("group_play")) else 1
        return (in_excluded, 0 if prefer_group_play else has_gp)

    candidates = sorted(all_slots, key=_slot_sort_key)

    selected: List[Dict[str, Any]] = []
    seen_families: set = set()

    # Pass 1: one card per activity_family for variety
    for slot in candidates:
        if len(selected) >= card_count:
            break
        family = (slot.get("activity_family") or
                  slot.get("_source_activity", {}).get("_debug", {}).get("activity_family", ""))
        if family not in seen_families:
            selected.append(slot)
            if family:
                seen_families.add(family)

    # Pass 2: fill remaining from any not yet selected
    for slot in candidates:
        if len(selected) >= card_count:
            break
        if slot not in selected:
            selected.append(slot)

    return selected[:card_count]


def _make_weekend_slot(
    source_slot: Dict[str, Any],
    weekend_mode: str,
) -> Dict[str, Any]:
    """
    Create a weekend variant of an existing weekday slot.

    Deep-copies the source slot so the original weekday data is never mutated.
    Sets slot["weekend_mode"] for downstream use by _normalize_slot() and
    _extract_slot_internal(). Patches _debug["activity_type"] to "weekend_practice"
    so the internal metadata correctly reports source_bank_type="weekend_practice".
    All other content (instructions, safety, domain, milestone, theme) is preserved.
    """
    slot = copy.deepcopy(source_slot)
    slot["weekend_mode"] = weekend_mode
    # Patch activity_type so _extract_slot_internal sees source_bank_type="weekend_practice"
    src = slot.setdefault("_source_activity", {})
    dbg = src.setdefault("_debug", {})
    dbg["activity_type"] = "weekend_practice"
    return slot


def _weekend_mode_for_slot(slot: Dict[str, Any], day: str) -> str:
    """
    Determine the weekend_mode for a given slot and day.

    Saturday always gets "family_practice".
    Sunday gets "playdate_sibling_practice" when the slot has group_play content;
    otherwise "light_review".
    """
    if day == "Saturday":
        return "family_practice"
    # Sunday
    has_gp = bool(
        slot.get("group_play") or
        slot.get("group_play_line") or
        slot.get("_source_activity", {}).get("group_play")
    )
    return "playdate_sibling_practice" if has_gp else "light_review"


def _build_weekend_items(
    all_weekday_slots: List[Dict[str, Any]],
    day: str,
    card_count: int,
    saturday_source: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Build `card_count` weekend slot dicts for the given day.

    For Saturday: selects from all weekday slots, favouring family variety.
    For Sunday: prefers slots with group_play content and avoids repeating
    the same activities used on Saturday (if saturday_source provided).

    Returns a list of slots ready for _normalize_slot() / _extract_slot_internal().
    """
    if day == "Saturday":
        chosen = _select_for_weekend(
            all_weekday_slots,
            card_count,
            prefer_group_play=False,
        )
    else:  # Sunday
        chosen = _select_for_weekend(
            all_weekday_slots,
            card_count,
            prefer_group_play=True,
            exclude_slots=saturday_source,
        )

    return [_make_weekend_slot(s, _weekend_mode_for_slot(s, day)) for s in chosen]


def adapt_weekly_plan(
    session_id: str,
    age_in_months: int,
    daily_time_minutes: int,
    weekly_schedule: Dict[str, Any],
    plan_period: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Convert the brain's weekly_schedule dict into the frontend-ready plan response,
    covering every day in the planning period including Saturday and Sunday.

    Weekday cards come directly from the scheduler output.
    Weekend cards are derived at the API layer from already-approved weekday
    activities when the scheduler has no Saturday/Sunday output:
      Saturday → "family_practice" cards (repeat-and-generalise)
      Sunday   → "playdate_sibling_practice" (if group_play content exists)
                 or "light_review" cards

    activity_date:
      Each activity card includes an ISO-8601 activity_date computed from
      plan_period["week_start_date"] and the day name.

    The response never contains the child's real name.
    """
    days_data = weekly_schedule.get("days", {})
    days_included: List[str] = plan_period.get("days_included", _WEEKDAYS)
    week_start_date: str = plan_period.get("week_start_date", "")
    card_count = _daily_card_count(daily_time_minutes)

    # Collect weekday slots once — used as source for weekend card generation
    all_weekday_slots = _collect_weekday_slots(weekly_schedule)
    saturday_source: Optional[List[Dict[str, Any]]] = None  # tracked for Sunday variety

    week: List[Dict[str, Any]] = []
    total_activities = 0
    days_with_activities = 0
    total_minutes = 0
    domains_seen: Dict[str, bool] = {}

    for day in days_included:
        day_info = days_data.get(day, {})
        items: List[Dict[str, Any]] = day_info.get("items", [])

        # Generate weekend cards when the scheduler has no output for this day
        if not items and day in ("Saturday", "Sunday"):
            items = _build_weekend_items(
                all_weekday_slots,
                day,
                card_count,
                saturday_source=saturday_source if day == "Sunday" else None,
            )
            if day == "Saturday":
                # Remember which source slots were used so Sunday can vary
                saturday_source = [
                    slot.get("_source_activity_original", slot)
                    for slot in items
                ]

        if not items:
            continue  # nothing to show for this day

        date_str = activity_date_for_day(week_start_date, day) if week_start_date else ""
        activities = []

        for idx, slot in enumerate(items):
            adapted = _normalize_slot(slot, session_id, day, idx)
            adapted["activity_date"] = date_str
            activities.append(adapted)
            total_activities += 1
            total_minutes += int(slot.get("duration_min", 5))
            domain_key = slot.get("category_key", "")
            if domain_key:
                domains_seen[domain_key] = True

        week.append({"day": day, "date": date_str, "activities": activities})
        days_with_activities += 1

    domains_covered = [
        {"key": k, "label": DOMAIN_LABELS.get(k, k)}
        for k in domains_seen
    ]

    return {
        "session_id": session_id,
        "plan_period": plan_period,
        "age_in_months": age_in_months,
        "daily_time_minutes": daily_time_minutes,
        "daily_card_count": card_count,
        "week": week,
        "progress_summary": {
            "domains_covered": domains_covered,
            "activity_count": total_activities,
            "days": days_with_activities,
            "estimated_weekly_minutes": total_minutes,
        },
    }


# ── Internal plan metadata (plan_internal) ────────────────────────────────────

def _build_milestone_lookup(
    bridge_plans: Dict[str, Any],
) -> Dict[Tuple[str, str, int], int]:
    """
    Build a lookup: (domain, activity_family, bridge_step_number) → milestone_age_months.

    Iterates active_bridge_steps in every domain's bridge plan, keying on the
    combination of activity_family and bridge_step_number that uniquely identifies
    a step. Used by _extract_slot_internal() to add milestone_age_months to each
    activity card without re-reading bridge_plans on every slot.
    """
    lookup: Dict[Tuple[str, str, int], int] = {}
    for domain, bp in bridge_plans.items():
        for step in bp.get("active_bridge_steps", []):
            family = step.get("activity_family", "")
            step_num = step.get("bridge_step_number")
            months = step.get("months")
            if family and step_num is not None and months is not None:
                lookup[(domain, family, int(step_num))] = int(months)
    return lookup


def _extract_slot_internal(
    slot: Dict[str, Any],
    day: str,
    slot_index: int,
    milestone_lookup: Dict[Tuple[str, str, int], int],
) -> Dict[str, Any]:
    """
    Extract internal metadata from one slot for GCS storage.

    Sources:
      slot["_source_activity"]["_debug"]  — activity_id, subdomain, milestone_text,
                                            bridge_step fields, source_bank_type,
                                            variant, planning_mode
      slot["_source_activity"]            — theme
      slot                                — activity_family, difficulty_level, goal
                                            (support_tier), duration fields, success,
                                            weekend_mode (if set by _make_weekend_slot)
    milestone_age_months is resolved via milestone_lookup keyed on
    (domain, activity_family, bridge_step_number).

    For weekend-derived cards, source_bank_type is "weekend_practice" because
    _make_weekend_slot patches _debug["activity_type"] before this is called.

    Returns a flat dict. Unknown/missing values default to "" or None, never raising.
    """
    src = slot.get("_source_activity") or {}
    dbg = src.get("_debug") or {}

    domain = slot.get("category_key", "")
    activity_family = dbg.get("activity_family") or slot.get("activity_family") or ""
    bridge_step_number = dbg.get("bridge_step_number")
    bridge_step_num_int: Optional[int] = int(bridge_step_number) if bridge_step_number is not None else None

    milestone_age_months: Optional[int] = None
    if domain and activity_family and bridge_step_num_int is not None:
        milestone_age_months = milestone_lookup.get((domain, activity_family, bridge_step_num_int))

    return {
        # Identity
        "day": day,
        "slot_index": slot_index,
        "activity_id": dbg.get("activity_id") or "",
        # Domain breakdown
        "domain": domain,
        "subdomain": dbg.get("subdomain") or "",
        # Milestone / bridge step
        "milestone_text": dbg.get("milestone") or "",
        "milestone_age_months": milestone_age_months,
        "bridge_step_index": bridge_step_num_int,
        "bridge_step_text": dbg.get("bridge_step_1") or "",
        "previous_bridge_step": dbg.get("previous_bridge_step") or "",
        "previous_bridge_status": dbg.get("previous_bridge_status") or "",
        # Activity family & variant
        "activity_family": activity_family,
        "variant": dbg.get("variant"),
        # Difficulty and pedagogy
        "difficulty_level": slot.get("level") or "",
        "theme": src.get("theme") or "",
        "source_bank_type": dbg.get("activity_type") or "",
        "planning_mode": dbg.get("planning_mode") or "",
        "weekend_mode": slot.get("weekend_mode") or "",  # "" for weekday cards
        # Support tier (slot-level "goal" field — e.g. "needs_special_support")
        "support_tier": slot.get("goal") or "",
        # Duration
        "duration_min": slot.get("duration_min"),
        "duration_label": slot.get("duration_label") or "5–15 min",
        # Success criteria preserved verbatim from brain output (not renamed here)
        "success_criteria": slot.get("success") or "",
    }


def build_plan_internal(
    session_id: str,
    brain_state: Dict[str, Any],
    weekly_schedule: Dict[str, Any],
    plan_period: Dict[str, Any],
    daily_time_minutes: int = 20,
) -> Dict[str, Any]:
    """
    Build the plan_internal GCS document capturing rich internal metadata for
    every activity in the plan (weekday and weekend).

    Stored inside doc["plans"][plan_id]["plan_internal"] in GCS.
    NEVER returned in the parent-facing API response.

    Weekend-derived cards are generated with the same logic as adapt_weekly_plan()
    so the two documents are always structurally consistent.

    Per-activity fields for Step 5 feedback linkage:
      plan_id, activity_date, day, slot_index, activity_id,
      source_bank_type ("weekend_practice" for Sat/Sun), weekend_mode

    Structure:
      {
        "session_id":     str,
        "plan_id":        str,
        "generated_at":   ISO-8601 UTC str,
        "dev_age":        { domain → months },
        "support_tier":   family_guidance_floor dict from brain_state,
        "safety_profile": { ... },
        "week": [
          {
            "day": "Thursday",
            "activities": [ { ...all internal fields... }, ... ]
          }, ...
        ]
      }
    """
    bridge_plans: Dict[str, Any] = brain_state.get("bridge_plans") or {}
    milestone_lookup = _build_milestone_lookup(bridge_plans)

    plan_id: str = plan_period.get("plan_id", "")
    week_start_date: str = plan_period.get("week_start_date", "")
    days_included: List[str] = plan_period.get("days_included", _WEEKDAYS)

    days_data = weekly_schedule.get("days", {})
    all_weekday_slots = _collect_weekday_slots(weekly_schedule)
    saturday_source: Optional[List[Dict[str, Any]]] = None

    week: List[Dict[str, Any]] = []

    for day in days_included:
        day_info = days_data.get(day, {})
        items: List[Dict[str, Any]] = day_info.get("items", [])

        # Weekend card generation mirrors adapt_weekly_plan exactly
        if not items and day in ("Saturday", "Sunday"):
            card_count = _daily_card_count(daily_time_minutes)
            items = _build_weekend_items(
                all_weekday_slots,
                day,
                card_count,
                saturday_source=saturday_source if day == "Sunday" else None,
            )
            if day == "Saturday":
                saturday_source = items[:]

        if not items:
            continue

        date_str = activity_date_for_day(week_start_date, day) if week_start_date else ""
        activities = []

        for idx, slot in enumerate(items):
            internal = _extract_slot_internal(slot, day, idx, milestone_lookup)
            title = slot.get("title") or ""
            # frontend_id = the UUID from plan_response (session+day+slot+title deterministic)
            # Used by /feedback to look up this card's internal metadata.
            internal["frontend_id"] = _deterministic_activity_id(session_id, day, idx, title)
            internal["plan_id"] = plan_id
            internal["activity_date"] = date_str
            activities.append(internal)

        week.append({"day": day, "activities": activities})

    # Session-level support tier — the full family_guidance_floor dict is the
    # authoritative brain output; per-card support_tier is the string "goal" field.
    session_support_tier = (
        brain_state.get("family_guidance_floor")
        or brain_state.get("support_tier")
        or None
    )

    return {
        "session_id": session_id,
        "plan_id": plan_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dev_age": brain_state.get("dev_age") or {},
        "support_tier": session_support_tier,
        "safety_profile": brain_state.get("safety_profile") or {},
        "week": week,
    }
