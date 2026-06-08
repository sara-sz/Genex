"""
genex_core/support_tiers.py
---------------------------
Support tier assignment, bridge plan building, and family guidance floor.

V22 update:
- select_next_milestones() now delegates to bridge_selector.select_next_milestones()
  (uses bridge_step_number=1 rows, V22 scoring rules).
- build_v22_plan_for_category() is the new V22 entry point; wraps
  bridge_selector.build_bridge_plan_for_category() with tier context.
- determine_family_guidance_floor() privacy fix: no longer includes child name
  in summary text.
- No-clear-gap mode ("parent_concern_support_no_clear_gap") is handled by
  bridge_selector and surfaced here via planning_mode in the returned plan dict.
- All pre-V22 public APIs (compute_support_metrics, get_support_tier, etc.) unchanged.
"""

from typing import Any, Dict, List, Optional

from genex_core.config import DOMAIN_CONFIG
from genex_core.interview_engine import ensure_concern_profile
from genex_core.milestones import get_category_to_subdomains
from genex_core.scoring import get_effective_dev_age, compute_language_scoring_profile


def get_category_concern_peak(state: Dict[str, Any], category_key: str) -> float:
    concern_profile = ensure_concern_profile(state)
    subdomains = get_category_to_subdomains().get(category_key, [])
    if not subdomains:
        return 0.0
    return max(
        float(concern_profile["subdomain_weights"].get(s, 0.0))
        for s in subdomains
    )


def compute_support_metrics(state: Dict[str, Any], category_key: str) -> Dict[str, Any]:
    """Compute the continuous support score and assign support tier. Fully deterministic."""
    child = state["child"]
    concern_profile = ensure_concern_profile(state)

    chrono = min(child["chronological_months"], 60)
    raw_dev_age = state["dev_age"].get(category_key, chrono)
    effective_dev_age = get_effective_dev_age(state, category_key)
    if effective_dev_age is None:
        effective_dev_age = raw_dev_age

    delay_est = state["delay_estimates"].get(category_key, {}).get("delay_months", 0)
    concern_domain_weight = float(concern_profile["domain_weights"].get(category_key, 0.0))
    concern_subdomain_peak = float(get_category_concern_peak(state, category_key))

    gap = max(0, chrono - int(effective_dev_age))

    light_gap_threshold = max(2, round(0.10 * chrono))
    primary_gap_threshold = max(5, round(0.20 * chrono))

    light_delay_threshold = max(4, round(0.10 * chrono))
    primary_delay_threshold = max(6, round(0.20 * chrono))

    gap_component = min(1.5, gap / max(primary_gap_threshold, 1))
    delay_component = min(1.5, delay_est / max(primary_delay_threshold, 1))
    concern_component = max(concern_domain_weight, concern_subdomain_peak * 0.85)

    support_score = (
        0.55 * gap_component
        + 0.25 * delay_component
        + 0.20 * concern_component
    )

    if gap >= primary_gap_threshold or support_score >= 0.95:
        tier = "needs_special_support"
    elif (
        support_score >= 0.42
        or (gap >= light_gap_threshold and concern_component >= 0.35)
        or (delay_est >= light_delay_threshold and concern_component >= 0.35)
    ):
        tier = "monitor_and_enrich"
    else:
        tier = "no_special_support"

    result = {
        "chronological_months": chrono,
        "raw_dev_age_months": int(raw_dev_age) if raw_dev_age is not None else None,
        "effective_dev_age_months": int(effective_dev_age) if effective_dev_age is not None else None,
        "estimated_dev_age_months": int(effective_dev_age) if effective_dev_age is not None else None,
        "milestone_gap_months": gap,
        "estimated_delay_months": delay_est,
        "concern_domain_weight": concern_domain_weight,
        "concern_subdomain_peak": concern_subdomain_peak,
        "light_gap_threshold": light_gap_threshold,
        "primary_gap_threshold": primary_gap_threshold,
        "light_delay_threshold": light_delay_threshold,
        "primary_delay_threshold": primary_delay_threshold,
        "support_score": round(float(support_score), 3),
        "tier": tier,
    }

    if category_key == "language_and_communication":
        result["language_scoring_profile"] = compute_language_scoring_profile(state)

    return result


def get_support_tier(state: Dict[str, Any], category_key: str) -> str:
    return compute_support_metrics(state, category_key)["tier"]


def no_special_support_needed(state: Dict[str, Any], category_key: str) -> bool:
    return get_support_tier(state, category_key) == "no_special_support"


def determine_family_guidance_floor(state: Dict[str, Any]) -> Dict[str, Any]:
    """Soft planning floor when all categories are technically no special support.

    V22: summary text no longer includes child name (privacy rule).
    """
    concern_profile = ensure_concern_profile(state)
    child = state["child"]

    supported = [k for k in DOMAIN_CONFIG if get_support_tier(state, k) != "no_special_support"]
    if supported:
        info = {
            "enabled": False,
            "mode": None,
            "category_key": None,
            "planning_tier": None,
            "target_weekly_minutes": 0,
            "summary": "",
        }
        state["family_guidance_floor"] = info
        return info

    ranked = sorted(
        DOMAIN_CONFIG.keys(),
        key=lambda k: (
            float(concern_profile.get("domain_weights", {}).get(k, 0.0)),
            float(get_category_concern_peak(state, k)),
            float(state.get("delay_estimates", {}).get(k, {}).get("delay_months", 0)),
        ),
        reverse=True,
    )
    category_key = ranked[0] if ranked else "language_and_communication"
    category_display = DOMAIN_CONFIG[category_key]["display"]
    daily_time_min = int(child.get("daily_time_min", 10))
    target_weekly_minutes = min(max(15, daily_time_min * 3), daily_time_min * 5)

    # V22 privacy: "your child" — child name never included in summary text
    summary = (
        "Based on the milestone interview, your child does not currently appear to need "
        f"scheduled special support. Because the family expressed concern about {category_display}, "
        "Genex will still provide short age-appropriate enrich-and-observe activities in this category."
    )

    info = {
        "enabled": True,
        "mode": "enrich_and_observe",
        "category_key": category_key,
        "category_display": category_display,
        "planning_tier": "enrich_and_observe",
        "target_weekly_minutes": int(target_weekly_minutes),
        "summary": summary,
    }
    state["family_guidance_floor"] = info
    return info


def is_family_guidance_category(state: Dict[str, Any], category_key: str) -> bool:
    floor = state.get("family_guidance_floor", {})
    return bool(floor.get("enabled") and floor.get("category_key") == category_key)


def select_next_milestones(
    state: Dict[str, Any],
    category_key: str,
    max_milestones: int = 6,
) -> Dict[str, Any]:
    """Select milestones for support planning.

    V22: delegates to bridge_selector.select_next_milestones(), which uses
    bridge_step_number=1 rows, V22 scoring_norm_answer, and the no-clear-gap
    concern-support path.

    Returns dict with keys: status, milestones, mode, message, source.
    """
    from genex_core.bridge_selector import select_next_milestones as _v22_select  # noqa

    child = state.get("child", {})
    dev_age = get_effective_dev_age(state, category_key)
    soft_floor_active = is_family_guidance_category(state, category_key)

    if dev_age is None and not soft_floor_active:
        raise ValueError(f"No developmental age found for {category_key}. Run Q&A first.")

    if no_special_support_needed(state, category_key) and not soft_floor_active:
        return {
            "status": "no_special_support",
            "message": (
                f"Your child does not appear to have a meaningful delay in "
                f"{DOMAIN_CONFIG[category_key]['display']} and may not need special support "
                f"in this category right now."
            ),
            "milestones": [],
            "mode": "no_special_support",
            "source": "tier_check",
        }

    result = _v22_select(state, category_key, max_milestones=max_milestones)
    milestones = result.get("milestones", [])

    # Soft floor override: if bridge_selector found nothing, use enrich-and-observe path
    if not milestones and soft_floor_active:
        result["mode"] = "soft_floor"
        result["message"] = (
            f"No gap milestones found for {DOMAIN_CONFIG.get(category_key, {}).get('display', category_key)}, "
            "but age-appropriate enrichment activities will be provided."
        )

    return {
        "status": "success" if milestones else "no_milestones",
        "milestones": milestones,
        "mode": result.get("mode", "standard"),
        "message": result.get("message", ""),
        "source": result.get("source", "table"),
    }


def build_v22_plan_for_category(
    state: Dict[str, Any],
    category_key: str,
) -> Dict[str, Any]:
    """V22 entry point: build the full bridge plan for one domain category.

    Combines support tier check + bridge_selector.build_bridge_plan_for_category().

    Returns:
        active_bridge_steps : List[Dict]  — bridges for activity_engine
        planning_mode       : str         — "standard" | "parent_concern_support_no_clear_gap" | etc.
        target_milestones   : List[Dict]
        tier                : str         — support tier for this category
        tier_metrics        : Dict        — full compute_support_metrics output
        skipped             : bool        — True if no support needed and no floor active
        skip_reason         : str | None
    """
    from genex_core.bridge_selector import build_bridge_plan_for_category as _v22_build  # noqa

    soft_floor_active = is_family_guidance_category(state, category_key)
    tier_metrics = compute_support_metrics(state, category_key)
    tier = tier_metrics["tier"]

    if tier == "no_special_support" and not soft_floor_active:
        return {
            "active_bridge_steps": [],
            "planning_mode": "skipped",
            "target_milestones": [],
            "tier": tier,
            "tier_metrics": tier_metrics,
            "skipped": True,
            "skip_reason": "no_special_support",
        }

    plan = _v22_build(state, category_key)

    return {
        "active_bridge_steps": plan.get("active_bridge_steps", []),
        "planning_mode": plan.get("planning_mode", "standard"),
        "target_milestones": plan.get("target_milestones", []),
        "tier": tier,
        "tier_metrics": tier_metrics,
        "skipped": False,
        "skip_reason": None,
    }


TIER_DISPLAY = {
    "needs_special_support": "Extra Support Recommended",
    "monitor_and_enrich": "Monitor & Enrich",
    "enrich_and_observe": "Monitor & Enrich",
    "no_special_support": "On Track",
}

TIER_SUPPORT_LEVEL = {
    "needs_special_support": "Extra support recommended",
    "monitor_and_enrich": "Some support may help",
    "enrich_and_observe": "Some support may help",
    "no_special_support": "No extra support indicated",
}

TIER_COLOR = {
    "needs_special_support": "#e85d4a",
    "monitor_and_enrich": "#f5a623",
    "enrich_and_observe": "#7db8f7",
    "no_special_support": "#4caf50",
}
