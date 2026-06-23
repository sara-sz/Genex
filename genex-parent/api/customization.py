"""
api/customization.py — Beta 2.1 current-week customization overlay (foundation).

Product rule: only the CURRENT plan/week is customizable; previous weeks stay
preserved and read-only. Customizations are stored as a SEPARATE overlay per
plan_id and never mutate the original generated plan:

    doc["plan_customizations"][plan_id] = {
        "removed_activity_ids": [activity_id, ...],
        "saved_for_later_activity_ids": [activity_id, ...],
        "activity_overrides": {
            "<activity_id>": {
                "mode": "easier" | "harder" | "swapped",
                "replacement_activity": { ...full resolved card incl. new id... },
                "replacement_internal": { ...domain/subdomain/family/...  },
                "created_at": "...", "reason": "parent_request"
            }
        },
        "added_activities": [
            {"activity": {...card incl. id...}, "internal": {...}, "day": "...", "created_at": "..."}
        ],
    }

This module is read-only/pure: it builds and resolves overlays but performs no
storage or auth. Mutation endpoints (remove/save/easier/harder/swap/add) are a
later slice; this foundation only exposes helpers + the resolver used by
GET /session and the overlay-aware feedback lookup.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional


def empty_overlay() -> Dict[str, Any]:
    """Return a fresh, empty overlay structure."""
    return {
        "removed_activity_ids": [],
        "saved_for_later_activity_ids": [],
        "activity_overrides": {},
        "added_activities": [],
    }


def get_overlay(doc: Dict[str, Any], plan_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return the overlay dict for plan_id, or None if none exists.

    Safe for old sessions that never had a plan_customizations key.
    """
    if not plan_id:
        return None
    return (doc.get("plan_customizations") or {}).get(plan_id)


def _overlay_changes_plan(overlay: Optional[Dict[str, Any]]) -> bool:
    """True only if the overlay would alter the resolved plan's activities.

    saved_for_later alone does NOT change the rendered plan, so an overlay that
    only contains saved IDs is treated as identity for resolution purposes.
    """
    if not overlay:
        return False
    return bool(
        overlay.get("removed_activity_ids")
        or overlay.get("activity_overrides")
        or overlay.get("added_activities")
    )


def is_overlay_empty(overlay: Optional[Dict[str, Any]]) -> bool:
    """True if the overlay has no customization data of any kind."""
    if not overlay:
        return True
    return not (
        overlay.get("removed_activity_ids")
        or overlay.get("saved_for_later_activity_ids")
        or overlay.get("activity_overrides")
        or overlay.get("added_activities")
    )


def resolve_plan_response(
    plan_response: Dict[str, Any],
    overlay: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return the current plan_response with the overlay applied.

    Identity-safe: when there is no overlay (or it does not change the plan), the
    ORIGINAL plan_response object is returned unchanged — byte-compatible with
    Beta 2.0. When the overlay changes the plan, a deep copy is modified and the
    original is never mutated.

    Resolution (current week only):
      - hide activities whose id is in removed_activity_ids,
      - replace activities that have an override with a replacement_activity,
      - append added_activities to their day (creating the day block if needed).

    All other plan_response fields (session_id, plan_period, age_in_months,
    daily_time_minutes, daily_card_count, progress_summary) and all per-card
    fields (instructions_steps, repeat_*, etc.) are preserved untouched.
    """
    if not plan_response or not _overlay_changes_plan(overlay):
        return plan_response

    resolved = copy.deepcopy(plan_response)
    removed = set(overlay.get("removed_activity_ids") or [])
    overrides = overlay.get("activity_overrides") or {}
    week: List[Dict[str, Any]] = resolved.get("week") or []

    # Hide removed + apply overrides, preserving day order and untouched cards.
    for day_entry in week:
        new_acts: List[Dict[str, Any]] = []
        for act in day_entry.get("activities", []):
            aid = act.get("id")
            if aid in removed:
                continue
            ov = overrides.get(aid)
            if ov and ov.get("replacement_activity"):
                new_acts.append(ov["replacement_activity"])
            else:
                new_acts.append(act)
        day_entry["activities"] = new_acts

    # Append parent-added activities to their day.
    added = overlay.get("added_activities") or []
    if added:
        day_index = {d.get("day"): d for d in week}
        for item in added:
            card = item.get("activity")
            if not card:
                continue
            day = item.get("day") or ""
            target = day_index.get(day)
            if target is None:
                target = {"day": day, "date": item.get("date", ""), "activities": []}
                week.append(target)
                day_index[day] = target
            target["activities"].append(card)

    resolved["week"] = week
    return resolved


def find_overlay_internal(
    overlay: Optional[Dict[str, Any]],
    activity_id: str,
) -> Optional[Dict[str, Any]]:
    """Return overlay-supplied internal metadata for a swapped/added activity.

    Used by the feedback enrichment fallback so domain/subdomain/care-team report
    routing keeps working for activities that exist only in the overlay (and thus
    have no entry in the frozen plan_internal).
    """
    if not overlay or not activity_id:
        return None

    # Swapped override → replacement_internal keyed by the ORIGINAL activity_id
    # and (when present) by the replacement card's own id.
    for orig_id, ov in (overlay.get("activity_overrides") or {}).items():
        if not ov:
            continue
        repl = ov.get("replacement_activity") or {}
        if activity_id in (orig_id, repl.get("id")):
            internal = ov.get("replacement_internal")
            if internal:
                return internal

    # Parent-added activity → its own internal block, keyed by the card id.
    for item in (overlay.get("added_activities") or []):
        card = item.get("activity") or {}
        if card.get("id") == activity_id:
            return item.get("internal")

    return None


def is_current_plan(doc: Dict[str, Any], plan_id: Optional[str]) -> bool:
    """Guard helper for mutation endpoints: only the current plan is editable.

    Returns True iff plan_id is the session's current_plan_id.
    """
    return bool(plan_id) and doc.get("current_plan_id") == plan_id


def plan_has_activity(plan_response: Dict[str, Any], activity_id: str) -> bool:
    """True if activity_id is one of the originally generated cards in plan_response.

    Customization targets the generated activities (by their stable plan_response
    `id`). Used for the 404 unknown-activity guard.
    """
    if not plan_response or not activity_id:
        return False
    for day_entry in plan_response.get("week", []):
        for act in day_entry.get("activities", []):
            if act.get("id") == activity_id:
                return True
    return False


def ensure_overlay(doc: Dict[str, Any], plan_id: str) -> Dict[str, Any]:
    """Return the (mutable) overlay for plan_id, creating an empty one if absent.

    The caller mutates the returned dict and persists the doc. Old sessions without
    a plan_customizations key get one initialised here.
    """
    pc = doc.setdefault("plan_customizations", {})
    if plan_id not in pc:
        pc[plan_id] = empty_overlay()
    return pc[plan_id]


def _add_unique(lst: List[str], value: str) -> bool:
    """Append value to lst if not already present. Returns True if it was added."""
    if value in lst:
        return False
    lst.append(value)
    return True


def overlay_summary(overlay: Optional[Dict[str, Any]], plan_id: Optional[str]) -> Dict[str, Any]:
    """Small additive summary of a plan's customizations for GET /session."""
    ov = overlay or {}
    removed = ov.get("removed_activity_ids") or []
    saved = ov.get("saved_for_later_activity_ids") or []
    overrides = ov.get("activity_overrides") or {}
    added = ov.get("added_activities") or []
    return {
        "plan_id": plan_id,
        "removed_count": len(removed),
        "saved_for_later_count": len(saved),
        "has_customizations": bool(removed or saved or overrides or added),
    }
