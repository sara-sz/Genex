"""
genex_core/summaries.py
-----------------------
Summary builder, domain result rows, doctor-visit prep notes, and feedback data.
"""

import re
from typing import Any, Dict, List, Optional

import pandas as pd

from genex_core.config import DOMAIN_CONFIG
from genex_core.interview_engine import ensure_concern_profile
from genex_core.scoring import get_effective_dev_age
from genex_core.support_tiers import (
    compute_support_metrics,
    get_support_tier,
    get_category_concern_peak,
    is_family_guidance_category,
    TIER_SUPPORT_LEVEL,
)


def support_level_from_tier(tier: str) -> str:
    """Return a parent-friendly support level label. Never uses clinical severity terms."""
    return TIER_SUPPORT_LEVEL.get(tier, "No extra support indicated")


def build_domain_results(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build a list of per-domain result dicts for display and doctor-prep."""
    child = state["child"]
    soft_floor = state.get("family_guidance_floor", {})
    concern_profile = ensure_concern_profile(state)
    rows = []

    for category_key, cfg in DOMAIN_CONFIG.items():
        delay_info = state["delay_estimates"].get(category_key, {})
        raw_dev_age = state["dev_age"].get(category_key)
        effective_dev_age = get_effective_dev_age(state, category_key)
        metrics = compute_support_metrics(state, category_key)

        chrono = min(child.get("chronological_months", 0), 60)
        milestone_gap = None if effective_dev_age is None else max(0, chrono - effective_dev_age)

        bank = state.get("activity_banks", {}).get(category_key, {})
        support_tier = metrics["tier"]
        planning_tier = support_tier

        if soft_floor.get("enabled") and soft_floor.get("category_key") == category_key:
            planning_tier = "enrich_and_observe"

        rows.append({
            "category_key": category_key,
            "category": cfg["display"],
            "raw_dev_age_months": raw_dev_age,
            "effective_dev_age_months": effective_dev_age,
            "milestone_gap_months": milestone_gap,
            "support_tier": support_tier,
            "planning_tier": planning_tier,
            "support_score": metrics["support_score"],
            "support_level": support_level_from_tier(planning_tier),
            "concern_domain_weight": round(concern_profile["domain_weights"].get(category_key, 0.0), 2),
            "activity_bank_summary": bank.get("summary", ""),
            "weekly_target_minutes": state.get("weekly_slot_allocation", {})
                                          .get("target_minutes_by_category", {})
                                          .get(category_key, 0),
        })

    return rows


def build_summary_df(state: Dict[str, Any]) -> pd.DataFrame:
    """Build the full summary DataFrame (mirrors notebook's summarizer_agent)."""
    rows = build_domain_results(state)
    return pd.DataFrame(rows)


def build_doctor_visit_prep(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build structured doctor-visit prep notes from the interview results.
    Deterministic — no AI involved. Intended to help parents communicate with clinicians.
    """
    child = state["child"]
    domain_results = build_domain_results(state)
    concern_profile = ensure_concern_profile(state)

    name = child.get("name", "your child")
    chrono = child.get("chronological_months", "?")
    diagnosis = child.get("diagnosis", "None reported")
    concern = child.get("concern", "")

    # Domains needing special support
    priority_domains = [
        r for r in domain_results
        if r["support_tier"] == "needs_special_support"
    ]
    monitor_domains = [
        r for r in domain_results
        if r["support_tier"] in ("monitor_and_enrich", "enrich_and_observe")
    ]
    on_track_domains = [
        r for r in domain_results
        if r["support_tier"] == "no_special_support"
    ]

    # Sample milestone questions asked and answers
    qa_highlights = []
    for category_key in DOMAIN_CONFIG:
        answers = state.get("qna", {}).get(category_key, [])
        category_display = DOMAIN_CONFIG[category_key]["display"]
        not_yet = [
            a for a in answers
            if a.get("norm_answer") in ("no", "not_sure")
        ]
        for a in not_yet[:2]:
            qa_highlights.append({
                "domain": category_display,
                "milestone": a.get("milestone", ""),
                "months_expected": a.get("months", ""),
                "answer": a.get("norm_answer", ""),
            })

    # Top concern subdomains
    top_subdomains = [
        x["subdomain"].replace("_", " ").title()
        for x in concern_profile.get("top_subdomains", [])[:4]
    ]

    # Questions to ask the doctor
    questions_for_doctor = []
    if priority_domains:
        for r in priority_domains:
            questions_for_doctor.append(
                f"{r['category']}: parent answers suggest this may be an area where extra support "
                f"could help. Consider discussing this with the child's pediatrician, developmental "
                f"specialist, or therapist."
            )
    if monitor_domains:
        for r in monitor_domains:
            questions_for_doctor.append(
                f"{r['category']}: worth keeping an eye on — you may want to mention this at "
                f"your next well-child visit."
            )
    if any(
        a.get("norm_answer") == "no"
        for cat in state.get("qna", {}).values()
        for a in cat
        if int(a.get("months", 0)) <= int(chrono) - 6
    ):
        questions_for_doctor.append(
            "Some milestones from 6 or more months ago have not yet been achieved. "
            "You may want to ask your pediatrician whether a developmental screening would be helpful."
        )
    if not questions_for_doctor:
        questions_for_doctor.append(
            "Development appears broadly on track based on this interview. "
            "Are there any areas you'd like to keep an eye on given the family's concerns?"
        )

    return {
        "child_name": name,
        "chronological_months": chrono,
        "diagnosis": diagnosis,
        "parent_concern_summary": concern,
        "top_concern_subdomains": top_subdomains,
        "priority_domains": priority_domains,
        "monitor_domains": monitor_domains,
        "on_track_domains": on_track_domains,
        "milestone_qa_highlights": qa_highlights,
        "questions_for_doctor": questions_for_doctor,
        "disclaimer": (
            "This report was generated by Genex, a developmental support tool. "
            "It is NOT a clinical diagnosis and does NOT replace assessment by a qualified "
            "pediatrician, developmental pediatrician, speech-language pathologist, "
            "occupational therapist, or other specialist."
        ),
    }


def get_focus_areas(state: Dict[str, Any], max_items: int = 3) -> List[str]:
    """Return the top category display names that need support."""
    domain_results = build_domain_results(state)
    work = [
        r for r in domain_results
        if r["planning_tier"] in ("needs_special_support", "monitor_and_enrich", "enrich_and_observe")
    ]
    work_sorted = sorted(
        work,
        key=lambda r: (
            {"needs_special_support": 2, "monitor_and_enrich": 1, "enrich_and_observe": 1}.get(
                r["planning_tier"], 0
            ),
            r.get("milestone_gap_months") or 0,
        ),
        reverse=True,
    )
    return [r["category"] for r in work_sorted[:max_items]]
