"""
genex_core/feedback_engine.py
-------------------------------
V22 feedback schema + mastery/fallback/rotate signal detection.

Scope for v0.4 (staging): feedback hooks only — no longitudinal storage,
no full progress history.  Signals are derived from the current cycle's
feedback entries and returned as action recommendations for the caller
(app.py / progress_tracker.py).

Mastery signal rules (from V22 spec):
  done_independently x3  → advance  (milestone confirmed, move to next)
  too_easy x2            → harder   (use make_harder variant next session)
  too_hard x2-3          → fallback (use previous_bridge_step)
  resisted x2            → rotate   (theme rotation — same bridge, new theme)

Feedback storage:
  state["activity_feedback"][category_key][title] = List[Dict]
  Each entry: {difficulty, performance, engagement, cycle_week, day}
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from genex_core.config import ACTIVITY_FEEDBACK_OPTIONS

logger = logging.getLogger(__name__)

# Signal thresholds
_ADVANCE_INDEPENDENT_THRESHOLD = 3   # done_independently x3 → advance
_HARDER_EASY_THRESHOLD = 2           # too_easy x2 → harder
_FALLBACK_HARD_THRESHOLD = 2         # too_hard x2 → fallback
_ROTATE_RESISTED_THRESHOLD = 2       # resisted x2 → rotate theme


# ---------------------------------------------------------------------------
# Feedback recording
# ---------------------------------------------------------------------------

def record_activity_feedback(
    state: Dict[str, Any],
    category_key: str,
    activity_title: str,
    difficulty: str,
    performance: str,
    engagement: str,
    cycle_week: int = 1,
    day: str = "",
) -> Dict[str, Any]:
    """Record one feedback entry for an activity.

    Returns the stored entry dict.
    Validates against ACTIVITY_FEEDBACK_OPTIONS before storing.
    """
    valid_diff = ACTIVITY_FEEDBACK_OPTIONS.get("difficulty", [])
    valid_perf = ACTIVITY_FEEDBACK_OPTIONS.get("performance", [])
    valid_eng = ACTIVITY_FEEDBACK_OPTIONS.get("engagement", [])

    if difficulty not in valid_diff:
        logger.warning("[feedback_engine] Unknown difficulty value: %s", difficulty)
    if performance not in valid_perf:
        logger.warning("[feedback_engine] Unknown performance value: %s", performance)
    if engagement not in valid_eng:
        logger.warning("[feedback_engine] Unknown engagement value: %s", engagement)

    entry = {
        "difficulty": difficulty,
        "performance": performance,
        "engagement": engagement,
        "cycle_week": cycle_week,
        "day": day,
    }

    fb = state.setdefault("activity_feedback", {})
    cat_fb = fb.setdefault(category_key, {})
    cat_fb.setdefault(activity_title, []).append(entry)

    return entry


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------

def get_feedback_entries(
    state: Dict[str, Any],
    category_key: str,
    activity_title: str,
) -> List[Dict[str, Any]]:
    """Return all feedback entries for one activity."""
    return (
        state.get("activity_feedback", {})
        .get(category_key, {})
        .get(activity_title, [])
    )


def detect_mastery_signal(
    entries: List[Dict[str, Any]],
) -> Optional[str]:
    """Detect signal from a list of feedback entries for one activity.

    Returns:
        "advance"  — milestone confirmed, move to next bridge step / next target
        "harder"   — use make_harder variant next session
        "fallback" — use previous_bridge_step (too hard)
        "rotate"   — same bridge, new theme/materials (child resisted)
        None       — no clear signal yet
    """
    if not entries:
        return None

    independent_count = sum(
        1 for e in entries if e.get("performance") == "done_independently"
    )
    too_easy_count = sum(
        1 for e in entries if e.get("difficulty") == "too_easy"
    )
    too_hard_count = sum(
        1 for e in entries if e.get("difficulty") == "too_hard"
        or e.get("performance") == "couldnt_do_it"
    )
    resisted_count = sum(
        1 for e in entries if e.get("engagement") in {"resisted_it", "didnt_like_it"}
    )

    # Priority order: advance > harder > fallback > rotate
    if independent_count >= _ADVANCE_INDEPENDENT_THRESHOLD:
        return "advance"
    if too_easy_count >= _HARDER_EASY_THRESHOLD:
        return "harder"
    if too_hard_count >= _FALLBACK_HARD_THRESHOLD:
        return "fallback"
    if resisted_count >= _ROTATE_RESISTED_THRESHOLD:
        return "rotate"

    return None


def get_activity_signal(
    state: Dict[str, Any],
    category_key: str,
    activity_title: str,
) -> Optional[str]:
    """Convenience: get mastery signal for a specific activity by title."""
    entries = get_feedback_entries(state, category_key, activity_title)
    return detect_mastery_signal(entries)


# ---------------------------------------------------------------------------
# Category-level signal summary
# ---------------------------------------------------------------------------

def get_category_feedback_summary(
    state: Dict[str, Any],
    category_key: str,
) -> Dict[str, Any]:
    """Summarize feedback signals across all activities in a category.

    Returns:
        activity_signals : {title → signal or None}
        has_advance      : bool
        has_fallback     : bool
        has_rotate       : bool
        has_harder       : bool
        dominant_signal  : str | None  (most common non-None signal)
    """
    cat_fb = state.get("activity_feedback", {}).get(category_key, {})
    activity_signals: Dict[str, Optional[str]] = {}

    signal_counts: Dict[str, int] = {}
    for title, entries in cat_fb.items():
        sig = detect_mastery_signal(entries)
        activity_signals[title] = sig
        if sig:
            signal_counts[sig] = signal_counts.get(sig, 0) + 1

    dominant = max(signal_counts, key=signal_counts.get) if signal_counts else None

    return {
        "activity_signals": activity_signals,
        "has_advance": bool(signal_counts.get("advance", 0)),
        "has_fallback": bool(signal_counts.get("fallback", 0)),
        "has_rotate": bool(signal_counts.get("rotate", 0)),
        "has_harder": bool(signal_counts.get("harder", 0)),
        "dominant_signal": dominant,
        "total_activities_with_feedback": len(cat_fb),
    }


# ---------------------------------------------------------------------------
# Cycle-next recommendation
# ---------------------------------------------------------------------------

def recommend_next_cycle_action(
    state: Dict[str, Any],
    category_key: str,
) -> Dict[str, Any]:
    """At the end of a cycle, recommend what to do next for this category.

    Returns:
        action       : "advance" | "fallback" | "rotate" | "harder" | "repeat" | "new_cycle"
        reason       : str
        cycle_week   : int  (current)
        next_week    : int  (recommended)
        details      : Dict (signal breakdown)
    """
    summary = get_category_feedback_summary(state, category_key)
    current_week = int(state.get("cycle_week", 1))
    dominant = summary["dominant_signal"]

    if dominant == "advance":
        return {
            "action": "advance",
            "reason": "Child is doing activities independently. Ready for next milestone.",
            "cycle_week": current_week,
            "next_week": 1,
            "details": summary,
        }

    if dominant == "fallback":
        return {
            "action": "fallback",
            "reason": "Activities are too hard. Will use an easier bridge step next cycle.",
            "cycle_week": current_week,
            "next_week": 1,
            "details": summary,
        }

    if dominant == "rotate":
        return {
            "action": "rotate",
            "reason": "Child is resisting activities. Will try different themes next week.",
            "cycle_week": current_week,
            "next_week": min(current_week + 1, 4),
            "details": summary,
        }

    if dominant == "harder":
        return {
            "action": "harder",
            "reason": "Activities feel too easy. Will add challenge next session.",
            "cycle_week": current_week,
            "next_week": current_week,
            "details": summary,
        }

    # No dominant signal — continue cycle
    if current_week < 2:
        return {
            "action": "repeat",
            "reason": "Continue with repeat-adapt week.",
            "cycle_week": current_week,
            "next_week": current_week + 1,
            "details": summary,
        }

    return {
        "action": "new_cycle",
        "reason": "Cycle complete. Starting new cycle with same milestones.",
        "cycle_week": current_week,
        "next_week": 1,
        "details": summary,
    }
