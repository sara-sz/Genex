"""
genex_core/progress_tracker.py
--------------------------------
V22 progress tracking — advance / fallback / rotate hooks.

Scope for v0.4 (staging): in-session hooks only.
- advance_milestone(): mark a target milestone as mastered, move to next.
- apply_fallback(): activate previous_bridge_step for a category.
- apply_theme_rotation(): bump cycle_week to trigger theme rotation.
- get_progress_summary(): return current cycle state for display.

No persistent storage in v0.4 (all state lives in session state dict).
Longitudinal history (across sessions) is a v0.5+ feature.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from genex_core.config import DOMAIN_CONFIG

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Advance: milestone mastered → move to next
# ---------------------------------------------------------------------------

def advance_milestone(
    state: Dict[str, Any],
    category_key: str,
    mastered_milestone: str,
) -> Dict[str, Any]:
    """Mark a milestone as mastered and remove it from active bridge steps.

    - Adds it to state["mastered_milestones"][category_key].
    - Removes it from state["bridge_plans"][category_key]["active_bridge_steps"].
    - activity_engine will need to be called again to generate new activities.

    Returns: {advanced: bool, mastered: str, remaining_bridges: int}
    """
    # Track mastered milestones
    mastered = state.setdefault("mastered_milestones", {})
    cat_mastered = mastered.setdefault(category_key, [])
    if mastered_milestone not in cat_mastered:
        cat_mastered.append(mastered_milestone)

    # Remove from active bridge steps
    bridge_plans = state.get("bridge_plans", {})
    plan = bridge_plans.get(category_key, {})
    active = plan.get("active_bridge_steps", [])
    before = len(active)

    updated = [
        b for b in active
        if b.get("milestone", "").strip().lower() != mastered_milestone.strip().lower()
    ]
    plan["active_bridge_steps"] = updated
    remaining = len(updated)

    logger.info(
        "[progress_tracker] Milestone advanced: %s in %s (%d → %d active bridges)",
        mastered_milestone, category_key, before, remaining,
    )

    # Flag that activity bank needs regeneration
    state.setdefault("needs_regeneration", {})[category_key] = True

    return {
        "advanced": True,
        "mastered": mastered_milestone,
        "category_key": category_key,
        "remaining_bridges": remaining,
    }


# ---------------------------------------------------------------------------
# Fallback: too hard → use previous_bridge_step
# ---------------------------------------------------------------------------

def apply_fallback(
    state: Dict[str, Any],
    category_key: str,
    target_milestone: str,
) -> Dict[str, Any]:
    """Activate the previous_bridge_step for a target milestone.

    Looks up the bridge plan for this milestone and swaps the bridge_step
    field to previous_bridge_step (if one exists).

    Returns: {applied: bool, fallback_step: str | None, message: str}
    """
    from genex_core.table_loader import get_bridge_df  # lazy

    bridge_plans = state.get("bridge_plans", {})
    plan = bridge_plans.get(category_key, {})
    active = plan.get("active_bridge_steps", [])

    for bridge in active:
        if bridge.get("milestone", "").strip().lower() != target_milestone.strip().lower():
            continue

        prev_step = bridge.get("previous_bridge_step", "").strip()
        if not prev_step or prev_step in {"", "none", "nan"}:
            return {
                "applied": False,
                "fallback_step": None,
                "message": f"No previous_bridge_step available for '{target_milestone}'.",
            }

        # Swap bridge_step → previous_bridge_step
        old_step = bridge.get("bridge_step", "")
        bridge["bridge_step"] = prev_step
        bridge["_fallback_active"] = True
        bridge["_original_bridge_step"] = old_step

        logger.info(
            "[progress_tracker] Fallback applied for %s: %s → %s",
            target_milestone, old_step[:60], prev_step[:60],
        )

        state.setdefault("needs_regeneration", {})[category_key] = True

        return {
            "applied": True,
            "fallback_step": prev_step,
            "original_step": old_step,
            "message": f"Easier bridge step activated for '{target_milestone}'.",
        }

    return {
        "applied": False,
        "fallback_step": None,
        "message": f"Milestone '{target_milestone}' not found in active bridge steps.",
    }


# ---------------------------------------------------------------------------
# Theme rotation: child resists → bump cycle week
# ---------------------------------------------------------------------------

def apply_theme_rotation(
    state: Dict[str, Any],
    category_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Bump cycle_week to trigger theme rotation for the next schedule build.

    cycle_week 1 → 2 (repeat)
    cycle_week 2 → 3 (new themes)
    cycle_week 3 → 4 (another new theme set)
    cycle_week 4 → 1 (full reset — new cycle)

    If category_key is provided, also flags that category's bank for regeneration.
    Returns: {rotated: bool, from_week: int, to_week: int}
    """
    current = int(state.get("cycle_week", 1))
    next_week = (current % 4) + 1  # 1→2, 2→3, 3→4, 4→1

    state["cycle_week"] = next_week

    if category_key:
        state.setdefault("needs_regeneration", {})[category_key] = True

    logger.info(
        "[progress_tracker] Theme rotation: cycle_week %d → %d%s",
        current, next_week,
        f" ({category_key})" if category_key else "",
    )

    return {
        "rotated": True,
        "from_week": current,
        "to_week": next_week,
    }


# ---------------------------------------------------------------------------
# Progress summary
# ---------------------------------------------------------------------------

def get_progress_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    """Return a snapshot of the current progress state for display / debug.

    Returns:
        cycle_week          : int
        mastered_milestones : {category_key → List[str]}
        needs_regeneration  : {category_key → bool}
        active_bridges      : {category_key → int}  (count of active bridge steps)
        feedback_count      : {category_key → int}  (activities with feedback)
    """
    cycle_week = int(state.get("cycle_week", 1))
    mastered = state.get("mastered_milestones", {})
    needs_regen = state.get("needs_regeneration", {})

    active_bridges: Dict[str, int] = {}
    feedback_count: Dict[str, int] = {}

    for cat_key in DOMAIN_CONFIG:
        bridges = (
            state.get("bridge_plans", {})
            .get(cat_key, {})
            .get("active_bridge_steps", [])
        )
        active_bridges[cat_key] = len(bridges)

        fb = state.get("activity_feedback", {}).get(cat_key, {})
        feedback_count[cat_key] = len(fb)

    return {
        "cycle_week": cycle_week,
        "mastered_milestones": mastered,
        "needs_regeneration": needs_regen,
        "active_bridges": active_bridges,
        "feedback_count": feedback_count,
    }


# ---------------------------------------------------------------------------
# Apply feedback-engine signal to state
# ---------------------------------------------------------------------------

def apply_signal(
    state: Dict[str, Any],
    category_key: str,
    signal: str,
    activity_title: str = "",
) -> Dict[str, Any]:
    """Apply a mastery signal from feedback_engine to state.

    This is the bridge between feedback_engine.recommend_next_cycle_action()
    and progress_tracker mutation functions.

    Returns action result dict.
    """
    if signal == "advance":
        if activity_title:
            return advance_milestone(state, category_key, activity_title)
        return {"action": "advance", "note": "No activity_title provided; no mutation."}

    if signal == "fallback":
        if activity_title:
            return apply_fallback(state, category_key, activity_title)
        return {"action": "fallback", "note": "No activity_title provided; no mutation."}

    if signal == "rotate":
        return apply_theme_rotation(state, category_key)

    if signal == "harder":
        # Harder is handled by scheduler._v22_repeat_adapt_item at schedule-build time
        # Just flag the category for adapt-mode
        state.setdefault("adapt_mode", {})[category_key] = "harder"
        return {"action": "harder", "applied": True}

    if signal == "repeat":
        state["cycle_week"] = min(int(state.get("cycle_week", 1)) + 1, 2)
        return {"action": "repeat", "next_week": state["cycle_week"]}

    if signal == "new_cycle":
        state["cycle_week"] = 1
        # Clear needs_regeneration so fresh banks are built
        state["needs_regeneration"] = {}
        return {"action": "new_cycle", "next_week": 1}

    return {"action": "unknown", "signal": signal}
