"""
genex_core/bridge_selector.py
------------------------------
V22 bridge milestone selection.

Rules:
- Initial plans ALWAYS use bridge_step_number = 1 rows only.
- previous_bridge_step is read from the table and stored in the bridge dict
  for future fallback use, but is NEVER used to generate activities in an
  initial plan.
- select_next_milestones() identifies the 1-2 developmental targets
  (not-yet-confirmed milestones just above the child's current level).
- build_bridge_plan_for_category() assembles the active_bridge_steps list
  that activity_engine.py uses to generate activities.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from genex_core.config import (
    ANSWER_SCORES,
    DOMAIN_CONFIG,
    V22_MAX_MILESTONES_PER_DOMAIN,
    V22_MIN_MILESTONES_PER_DOMAIN,
)
from genex_core.table_loader import (
    get_bridge_step1_df,
    get_bridge_df,
    get_family_description,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _v22_answer_norm(value: Any) -> str:
    v = str(value or "not_asked").strip().lower()
    if v in {"yes", "sometimes", "with_help", "with help", "no", "not_sure", "not sure"}:
        return v.replace(" ", "_")
    return "not_asked" if v in {"", "none", "nan"} else v


def _confirmed_floor_from_answers(
    state: Dict[str, Any],
    category_key: str,
) -> Optional[int]:
    """Find the highest age band where the child clearly has all milestones.

    Uses scoring_norm_answer (which accounts for performance barriers).
    Returns None if no clear floor is established.
    """
    qna_list = state.get("qna", {}).get(category_key, [])
    if not qna_list:
        return None

    # Group by age band
    by_age: Dict[int, List[str]] = {}
    for item in qna_list:
        age = int(item.get("months", 0) or 0)
        # Prefer scoring_norm_answer (V22 barrier-adjusted) over raw norm_answer
        ans = _v22_answer_norm(
            item.get("scoring_norm_answer") or item.get("norm_answer") or "not_asked"
        )
        by_age.setdefault(age, []).append(ans)

    # Find highest age where all answers are "yes"
    confirmed_ages = []
    for age, answers in by_age.items():
        if all(a == "yes" for a in answers):
            confirmed_ages.append(age)

    return max(confirmed_ages) if confirmed_ages else None


def _min_reasonable_target_month(
    state: Dict[str, Any],
    category_key: str,
    chrono_months: int,
    working_age: int,
) -> int:
    """Return the minimum month we should target.

    Prevents regression to very young milestones unless the parent explicitly
    mentions regression/skill loss.
    """
    qna_list = state.get("qna", {}).get(category_key, [])
    if not qna_list:
        return max(2, working_age - 12)

    # If any milestone was answered "yes", floor = highest confirmed "yes" age
    yes_ages = [
        int(item.get("months", 0))
        for item in qna_list
        if _v22_answer_norm(
            item.get("scoring_norm_answer") or item.get("norm_answer") or ""
        ) == "yes"
    ]
    if yes_ages:
        # Don't go below the highest confirmed age
        return max(yes_ages)

    return max(2, working_age - 12)


def _has_regression_concern(state: Dict[str, Any], category_key: str) -> bool:
    """Return True if parent text mentions skill regression or loss."""
    child = state.get("child", {})
    txt = " ".join(
        str(child.get(k, ""))
        for k in ["diagnosis", "condition", "concern", "parent_concern", "concerns"]
    ).lower()
    if category_key == "language_and_communication":
        return bool(re.search(
            r"\b(regress|lost words|lost speech|stopped talking|language loss|speech loss)\b",
            txt,
        ))
    return bool(re.search(r"\b(regress|lost skill|lost ability)\b", txt))


def _parent_has_concern_for_category(
    state: Dict[str, Any],
    category_key: str,
) -> bool:
    """Return True if the concern profile has meaningful signal for this category."""
    profile = state.get("concern_profile", {})
    weight = profile.get("domain_weights", {}).get(category_key, 0.0)
    return float(weight) >= 0.10


# ---------------------------------------------------------------------------
# select_next_milestones  (V22 — replaces v11 / v21 versions)
# ---------------------------------------------------------------------------

def select_next_milestones(
    state: Dict[str, Any],
    category_key: str,
    max_milestones: int = V22_MAX_MILESTONES_PER_DOMAIN,
    min_milestones: int = V22_MIN_MILESTONES_PER_DOMAIN,
) -> Dict[str, Any]:
    """Select 1–max_milestones developmental targets for a category.

    Returns a dict with keys:
        milestones  : List[Dict]  — selected target milestone rows
        mode        : str         — planning mode
        message     : str         — human-readable summary
        source      : str         — "table" or "concern_support"
    """
    child = state.get("child", {})
    chrono = max(2, min(int(child.get("chronological_months", 0) or 0), 60))
    dev_age = state.get("dev_age", {}).get(category_key)
    working_age = int(dev_age) if dev_age is not None else chrono

    bridge1_df = get_bridge_step1_df()
    if "category_key" not in bridge1_df.columns:
        return _no_targets("no category_key column in table")

    cat_df = bridge1_df[bridge1_df["category_key"] == category_key].copy()
    if cat_df.empty:
        return _no_targets(f"no bridge_step_1 rows for {category_key}")

    # Regression guard: don't go below confirmed floor unless regression mentioned
    floor = _confirmed_floor_from_answers(state, category_key)
    min_target = _min_reasonable_target_month(state, category_key, chrono, working_age)
    allow_below_floor = _has_regression_concern(state, category_key)

    # Find milestones just ABOVE the child's current level
    # Target window: working_age to working_age + 18 months
    lo = working_age if not allow_below_floor else max(2, working_age - 6)
    hi = min(chrono + 18, 66)

    # Start with milestones the child has NOT clearly confirmed
    qna_list = state.get("qna", {}).get(category_key, [])
    confirmed_milestones = {
        _norm(item.get("milestone", ""))
        for item in qna_list
        if _v22_answer_norm(
            item.get("scoring_norm_answer") or item.get("norm_answer") or ""
        ) == "yes"
    }

    # Prefer milestones where child answered "no", "sometimes", "with_help"
    attempted_not_yes = {
        _norm(item.get("milestone", ""))
        for item in qna_list
        if _v22_answer_norm(
            item.get("scoring_norm_answer") or item.get("norm_answer") or ""
        ) in {"no", "sometimes", "with_help"}
    }

    candidates = cat_df[
        (cat_df["months"] >= lo) & (cat_df["months"] <= hi)
    ].copy()

    if not allow_below_floor and floor is not None:
        candidates = candidates[candidates["months"] > floor]

    if candidates.empty:
        # Widen window
        candidates = cat_df[cat_df["months"] >= max(2, lo - 6)].copy()

    if candidates.empty:
        # No-clear-gap path: use concern-support mode
        if _parent_has_concern_for_category(state, category_key):
            return _concern_support_targets(state, category_key, cat_df, chrono)
        return _no_targets(f"no target milestones found for {category_key} at {working_age}m")

    # Score candidates
    concern_profile = state.get("concern_profile", {})
    subdomain_weights = concern_profile.get("subdomain_weights", {})

    def _score_row(row: pd.Series) -> float:
        age_dist = abs(int(row["months"]) - working_age)
        age_score = max(0.0, 1.0 - age_dist / 24.0)
        sub_weight = float(subdomain_weights.get(str(row.get("subdomain", "")), 0.0))
        attempted_bonus = 0.3 if _norm(row.get("milestone", "")) in attempted_not_yes else 0.0
        return age_score * 0.55 + sub_weight * 0.30 + attempted_bonus * 0.15

    candidates["_score"] = candidates.apply(_score_row, axis=1)
    candidates = candidates.sort_values(
        ["_score", "months"], ascending=[False, True]
    )

    # Remove already-confirmed milestones
    candidates = candidates[
        ~candidates["milestone"].apply(lambda m: _norm(m) in confirmed_milestones)
    ]

    if candidates.empty:
        if _parent_has_concern_for_category(state, category_key):
            return _concern_support_targets(state, category_key, cat_df, chrono)
        return _no_targets(f"all candidates already confirmed for {category_key}")

    selected_rows = candidates.head(max_milestones)
    milestones = _rows_to_milestone_dicts(selected_rows)

    if len(milestones) < min_milestones:
        if _parent_has_concern_for_category(state, category_key):
            return _concern_support_targets(state, category_key, cat_df, chrono)

    return {
        "milestones": milestones,
        "mode": "standard",
        "message": f"Selected {len(milestones)} target milestone(s) for {DOMAIN_CONFIG.get(category_key, {}).get('display', category_key)}.",
        "source": "table",
    }


def _concern_support_targets(
    state: Dict[str, Any],
    category_key: str,
    cat_df: pd.DataFrame,
    chrono: int,
) -> Dict[str, Any]:
    """No-clear-gap path: use age-appropriate milestones near concern area."""
    concern_profile = state.get("concern_profile", {})
    child = state.get("child", {})
    concern_text = str(child.get("concern", "") or "")
    subdomain_weights = concern_profile.get("subdomain_weights", {})

    # Pick milestones close to chrono age, weighted by concern subdomains
    window = cat_df[
        (cat_df["months"] >= max(2, chrono - 6)) &
        (cat_df["months"] <= min(66, chrono + 6))
    ].copy()

    if window.empty:
        window = cat_df.copy()

    def _score(row: pd.Series) -> float:
        return float(subdomain_weights.get(str(row.get("subdomain", "")), 0.0))

    window["_score"] = window.apply(_score, axis=1)
    window = window.sort_values(["_score", "months"], ascending=[False, True])
    selected = window.head(V22_MAX_MILESTONES_PER_DOMAIN)

    if selected.empty:
        return _no_targets(f"concern_support: no rows near {chrono}m for {category_key}")

    # Find the concern mention for the parent message
    concern_snippet = concern_text[:80] if concern_text else "your concern"

    return {
        "milestones": _rows_to_milestone_dicts(selected),
        "mode": "parent_concern_support_no_clear_gap",
        "message": (
            "No clear developmental gap was found in this quick screen, but because "
            f"you mentioned {concern_snippet!r}, Genex will create a light support plan "
            "around the closest age-appropriate milestones related to that concern."
        ),
        "source": "concern_support",
    }


def _no_targets(reason: str) -> Dict[str, Any]:
    return {
        "milestones": [],
        "mode": "no_targets",
        "message": reason,
        "source": "none",
    }


def _rows_to_milestone_dicts(df: pd.DataFrame) -> List[Dict[str, Any]]:
    result = []
    for _, row in df.iterrows():
        result.append({
            "months": int(row["months"]) if pd.notna(row.get("months")) else 0,
            "category_key": str(row.get("category_key", "")),
            "subdomain": str(row.get("subdomain", "") or ""),
            "milestone": str(row.get("milestone", "") or ""),
            "parent_explanation": str(row.get("parent_explanation", "") or ""),
            "bridge_step": str(row.get("bridge_step", "") or ""),
            "bridge_step_number": int(row["bridge_step_number"])
                if pd.notna(row.get("bridge_step_number")) else 1,
            "activity_family": str(row.get("activity_family", "") or ""),
            "previous_bridge_step": str(row.get("previous_bridge_step", "") or ""),
            "previous_anchor_age": (
                int(row["previous_anchor_age"])
                if pd.notna(row.get("previous_anchor_age")) else None
            ),
        })
    return result


# ---------------------------------------------------------------------------
# build_bridge_plan_for_category  (V22)
# ---------------------------------------------------------------------------

def build_bridge_plan_for_category(
    state: Dict[str, Any],
    category_key: str,
    target_milestones: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build the bridge plan (active_bridge_steps) for one category.

    - Uses only bridge_step_number = 1 rows for initial plans.
    - previous_bridge_step is stored in each bridge dict but NOT used
      to generate activities here.  feedback_engine.py uses it later.

    Returns:
        active_bridge_steps : List[Dict]  — bridges to generate activities for
        planning_mode       : str
        target_milestones   : List[Dict]
    """
    if target_milestones is None:
        result = select_next_milestones(state, category_key)
        target_milestones = result.get("milestones", [])
        planning_mode = result.get("mode", "standard")
    else:
        planning_mode = "standard"

    if not target_milestones:
        return {
            "active_bridge_steps": [],
            "planning_mode": planning_mode,
            "target_milestones": [],
        }

    active_bridges: List[Dict[str, Any]] = []
    bridge1_df = get_bridge_step1_df()
    full_df = get_bridge_df()

    for target in target_milestones:
        milestone_norm = _norm(target.get("milestone", ""))
        target_months = int(target.get("months", 0) or 0)
        family = str(target.get("activity_family", "") or "")

        # Get bridge_step_1 row for this milestone
        matches = bridge1_df[
            (bridge1_df["months"] == target_months) &
            (bridge1_df["milestone"].apply(lambda m: _norm(m)) == milestone_norm)
        ]

        if matches.empty:
            # Fuzzy match by subdomain + activity_family
            sub = str(target.get("subdomain", "") or "")
            matches = bridge1_df[
                (bridge1_df["category_key"] == category_key) &
                (bridge1_df["subdomain"] == sub) &
                (bridge1_df["months"] == target_months)
            ]

        if matches.empty:
            # Use the target dict directly as the bridge
            bridge = dict(target)
            bridge["bridge_step_number"] = 1
        else:
            bridge = _rows_to_milestone_dicts(matches.head(1))[0]

        # Enrich with family description
        bridge["activity_family_description"] = get_family_description(
            bridge.get("activity_family", "")
        )
        # Store previous_bridge_step for future fallback — NOT used for initial plan
        bridge["_previous_bridge_step_available"] = bool(
            bridge.get("previous_bridge_step", "")
        )
        # Tag as initial plan
        bridge["initial_plan"] = True
        bridge["planning_mode"] = planning_mode

        active_bridges.append(bridge)

    return {
        "active_bridge_steps": active_bridges,
        "planning_mode": planning_mode,
        "target_milestones": target_milestones,
    }


# ---------------------------------------------------------------------------
# Active bridge selector  (V22 — used by activity_engine for fallback)
# ---------------------------------------------------------------------------

def select_active_bridge(
    state: Dict[str, Any],
    category_key: str,
    target: Dict[str, Any],
) -> Dict[str, Any]:
    """Select the active bridge step for a specific target milestone.

    For initial plans: always returns bridge_step_number = 1.
    For repeat cycles with negative feedback: feedback_engine calls this
    to get previous_bridge_step rows (not used here for initial plans).
    """
    milestone_norm = _norm(target.get("milestone", ""))
    target_months = int(target.get("months", 0) or 0)

    bridge1_df = get_bridge_step1_df()
    matches = bridge1_df[
        (bridge1_df["months"] == target_months) &
        (bridge1_df["milestone"].apply(lambda m: _norm(m)) == milestone_norm)
    ]

    if matches.empty:
        return dict(target)

    bridge = _rows_to_milestone_dicts(matches.head(1))[0]
    bridge["activity_family_description"] = get_family_description(
        bridge.get("activity_family", "")
    )
    return bridge
