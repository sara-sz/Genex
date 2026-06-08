"""
genex_core/admin_debug_view.py
--------------------------------
V22 admin/debug view helpers.

Provides render functions for use in app.py when ADMIN_DEBUG env var is set.
All debug information is gated behind the ADMIN_DEBUG flag — never shown to
parents in production.

Includes:
  - render_activity_debug_card()  : full _debug sub-dict for an activity
  - render_bridge_plan_debug()    : active bridge steps for a category
  - render_feedback_debug()       : raw feedback entries + signals
  - render_concern_profile_debug(): routing confidence, LLM augmentation
  - render_validation_debug()     : blocked activities and their warnings
  - render_state_snapshot()       : full state summary for development
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional


def is_admin_debug() -> bool:
    """Return True if ADMIN_DEBUG env var is set to a truthy value."""
    val = os.environ.get("ADMIN_DEBUG", "").strip().lower()
    return val in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Activity card debug view
# ---------------------------------------------------------------------------

def render_activity_debug_card(activity: Dict[str, Any]) -> str:
    """Return a formatted debug string for an activity card.

    Shows _debug sub-dict + validation warnings.
    """
    debug = activity.get("_debug", {})
    warnings = activity.get("validation_warnings", [])

    lines = ["── ACTIVITY DEBUG ──"]
    lines.append(f"title         : {activity.get('title', '?')}")
    lines.append(f"activity_type : {activity.get('activity_type', '?')}")
    lines.append(f"cycle_week    : {activity.get('cycle_week', '?')}")
    lines.append(f"activity_family: {debug.get('activity_family', activity.get('activity_family', '?'))}")
    lines.append(f"bridge_step   : {debug.get('bridge_step', '?')[:80]}")
    lines.append(f"milestone     : {debug.get('milestone', '?')[:80]}")
    lines.append(f"bridge_step_no: {debug.get('bridge_step_number', '?')}")
    lines.append(f"source        : {debug.get('source', '?')}")
    lines.append(f"llm_model     : {debug.get('llm_model', 'deterministic_fallback')}")
    lines.append(f"prompt_chars  : {debug.get('prompt_chars', '?')}")

    if warnings:
        lines.append(f"⚠ validation  : {', '.join(warnings)}")

    return "\n".join(lines)


def get_activity_debug_dict(activity: Dict[str, Any]) -> Dict[str, Any]:
    """Return a clean debug dict for JSON serialization."""
    return {
        "title": activity.get("title"),
        "activity_type": activity.get("activity_type"),
        "cycle_week": activity.get("cycle_week"),
        "_debug": activity.get("_debug", {}),
        "validation_warnings": activity.get("validation_warnings", []),
    }


# ---------------------------------------------------------------------------
# Bridge plan debug view
# ---------------------------------------------------------------------------

def render_bridge_plan_debug(
    state: Dict[str, Any],
    category_key: str,
) -> str:
    """Return formatted debug info for the active bridge plan of a category."""
    plan = state.get("bridge_plans", {}).get(category_key, {})
    bridges = plan.get("active_bridge_steps", [])
    mode = plan.get("planning_mode", "?")

    lines = [f"── BRIDGE PLAN DEBUG: {category_key} ──"]
    lines.append(f"planning_mode : {mode}")
    lines.append(f"bridge_count  : {len(bridges)}")

    for i, b in enumerate(bridges):
        lines.append(f"\n  Bridge {i+1}:")
        lines.append(f"    milestone   : {b.get('milestone', '?')[:70]}")
        lines.append(f"    months      : {b.get('months', '?')}")
        lines.append(f"    bridge_step#: {b.get('bridge_step_number', '?')}")
        lines.append(f"    bridge_step : {str(b.get('bridge_step', ''))[:70]}")
        lines.append(f"    family      : {b.get('activity_family', '?')}")
        lines.append(f"    prev_step   : {'YES' if b.get('previous_bridge_step') else 'none'}")
        lines.append(f"    initial_plan: {b.get('initial_plan', '?')}")
        if b.get("_fallback_active"):
            lines.append(f"    ⚠ FALLBACK ACTIVE (orig: {b.get('_original_bridge_step','?')[:40]})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Feedback debug view
# ---------------------------------------------------------------------------

def render_feedback_debug(
    state: Dict[str, Any],
    category_key: str,
) -> str:
    """Return formatted debug info for feedback entries in a category."""
    from genex_core.feedback_engine import detect_mastery_signal  # lazy

    cat_fb = state.get("activity_feedback", {}).get(category_key, {})
    lines = [f"── FEEDBACK DEBUG: {category_key} ──"]

    if not cat_fb:
        lines.append("  (no feedback recorded)")
        return "\n".join(lines)

    for title, entries in cat_fb.items():
        sig = detect_mastery_signal(entries)
        lines.append(f"\n  {title[:50]}")
        lines.append(f"    entries : {len(entries)}")
        lines.append(f"    signal  : {sig or 'none'}")
        for e in entries[-3:]:  # last 3 only
            lines.append(
                f"    [{e.get('cycle_week','?')}w {e.get('day','?')}] "
                f"diff={e.get('difficulty','?')} perf={e.get('performance','?')} "
                f"eng={e.get('engagement','?')}"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Concern profile debug view
# ---------------------------------------------------------------------------

def render_concern_profile_debug(state: Dict[str, Any]) -> str:
    """Return formatted debug info for concern routing."""
    profile = state.get("concern_profile", {})
    lines = ["── CONCERN PROFILE DEBUG ──"]
    lines.append(f"routing_confidence  : {profile.get('routing_confidence', '?')}")
    lines.append(f"llm_augmented       : {profile.get('llm_augmented', False)}")
    lines.append(f"needs_clarification : {profile.get('needs_clarification', False)}")
    lines.append(f"cognitive_suppressed: {profile.get('cognitive_strength_suppressed', False)}")

    lines.append("\ndomain_weights:")
    for k, v in profile.get("domain_weights", {}).items():
        lines.append(f"  {k:<35} {round(float(v), 3)}")

    if profile.get("llm_result"):
        lr = profile["llm_result"]
        lines.append(f"\nllm_result:")
        lines.append(f"  domains    : {lr.get('selected_domain_displays')}")
        lines.append(f"  confidence : {lr.get('confidence')}")
        lines.append(f"  reason     : {lr.get('reason','')[:80]}")
        lines.append(f"  low_conf   : {lr.get('low_confidence')}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Validation debug view
# ---------------------------------------------------------------------------

def render_validation_debug(
    blocked_activities: List[Dict[str, Any]],
    category_key: str,
) -> str:
    """Return formatted debug info for blocked (invalid) activities."""
    lines = [f"── VALIDATION DEBUG: {category_key} ──"]
    lines.append(f"blocked_count: {len(blocked_activities)}")

    for i, act in enumerate(blocked_activities):
        lines.append(f"\n  Blocked {i+1}: {act.get('title', 'untitled')[:50]}")
        for w in act.get("validation_warnings", []):
            lines.append(f"    ✗ {w}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Full state snapshot
# ---------------------------------------------------------------------------

def render_state_snapshot(state: Dict[str, Any]) -> str:
    """Render a compact state snapshot for debugging full pipeline runs."""
    from genex_core.progress_tracker import get_progress_summary  # lazy

    child = state.get("child", {})
    lines = ["══ STATE SNAPSHOT (ADMIN DEBUG) ══"]
    lines.append(f"app_version   : {state.get('app_version', '?')}")
    lines.append(f"engine        : {state.get('engine_version', '?')}")
    lines.append(f"chrono_months : {child.get('chronological_months', '?')}")
    lines.append(f"cycle_week    : {state.get('cycle_week', 1)}")

    dev_age = state.get("dev_age", {})
    if dev_age:
        lines.append("\ndev_age by domain:")
        for k, v in dev_age.items():
            lines.append(f"  {k:<35} {v}m")

    alloc = state.get("weekly_slot_allocation", {})
    if alloc:
        lines.append(f"\nslot_allocation mode: {alloc.get('planning_mode', '?')}")
        for k, v in alloc.get("target_minutes_by_category", {}).items():
            lines.append(f"  {k:<35} {v}min")

    progress = get_progress_summary(state)
    lines.append(f"\nactive_bridges:")
    for k, n in progress["active_bridges"].items():
        lines.append(f"  {k:<35} {n}")

    lines.append(f"\nmastered_milestones:")
    for k, lst in progress["mastered_milestones"].items():
        lines.append(f"  {k}: {len(lst)}")

    banks = state.get("activity_banks", {})
    if banks:
        lines.append("\nactivity_banks:")
        for k, bank in banks.items():
            n = len(bank.get("activities", []))
            status = bank.get("status", "?")
            lines.append(f"  {k:<35} {n} activities [{status}]")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Streamlit-friendly helper (returns dict for st.json / st.expander)
# ---------------------------------------------------------------------------

def get_v22_qa_diagnostic(state: Dict[str, Any]) -> Dict[str, Any]:
    """Return a QA-focused diagnostic dict for ADMIN_DEBUG=1.

    Covers the four areas Sara checks during local QA:
      1. Focus domains selected
      2. Questions per domain — total and parent_explanation status
      3. Activity bank — size, bridge families, duplicate titles
      4. Week 1 schedule — activity count, unique titles, duplicates
    """
    from genex_core.interview_engine import build_milestone_questions  # lazy
    from genex_core.activity_engine import (  # lazy
        _family_bucket,
        _FAMILY_VARIANTS,
        _BUCKET_VARIANTS,
    )

    diag: Dict[str, Any] = {}

    # ── 1. Focus domains ────────────────────────────────────────────────────
    allocation = state.get("weekly_slot_allocation", {})
    focus_domains = allocation.get("supported_categories", [])
    diag["focus_domains"] = focus_domains
    diag["focus_domain_count"] = len(focus_domains)

    # ── 2. Questions per domain ─────────────────────────────────────────────
    q_info: Dict[str, Any] = {}
    for dk in focus_domains:
        try:
            qs = build_milestone_questions(state, dk, max_questions_total=15)
            missing = [
                q.get("milestone", "?")[:60]
                for q in qs
                if not q.get("parent_explanation", "").strip()
            ]
            q_info[dk] = {
                "total_questions": len(qs),
                "with_parent_explanation": len(qs) - len(missing),
                "missing_parent_explanation": missing,
            }
        except Exception as exc:
            q_info[dk] = {"error": str(exc)}
    diag["questions_per_domain"] = q_info

    # ── 3. Activity banks ────────────────────────────────────────────────────
    bank_info: Dict[str, Any] = {}
    banks = state.get("activity_banks", {})
    for dk, bank in banks.items():
        acts = bank.get("activities", [])
        title_counts: Dict[str, int] = {}
        instr_counts: Dict[str, int] = {}
        families_seen: List[str] = []
        for a in acts:
            t = a.get("title", "")
            instr = a.get("instructions", "")
            title_counts[t] = title_counts.get(t, 0) + 1
            instr_counts[instr] = instr_counts.get(instr, 0) + 1
            fam = a.get("_debug", {}).get("activity_family", a.get("activity_family", "?"))
            bucket = _family_bucket(fam, dk)
            in_fv = fam in _FAMILY_VARIANTS
            in_bv = bucket in _BUCKET_VARIANTS
            bv_size = len(_BUCKET_VARIANTS.get(bucket, []))
            entry = f"{fam} | bucket={bucket} | FV={in_fv} | BV={in_bv}({bv_size})"
            if entry not in families_seen:
                families_seen.append(entry)

        dup_titles = {t: c for t, c in title_counts.items() if c > 1}
        dup_instrs = {
            instr[:60]: c for instr, c in instr_counts.items() if c > 1
        }
        bank_info[dk] = {
            "total_activities": len(acts),
            "active_bridges": bank.get("active_bridges", 0),
            "core_variants_per_bridge": bank.get("core_variants_per_bridge", "?"),
            "duplicate_titles": dup_titles,
            "duplicate_instructions": dup_instrs,
            "bridge_families": families_seen,
        }
    diag["activity_banks"] = bank_info

    # ── 4. Week 1 schedule ───────────────────────────────────────────────────
    schedule = state.get("weekly_schedule", {})
    cycle_week = schedule.get("cycle_week", state.get("cycle_week", 1))
    days = schedule.get("days", {})
    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    sched_display: Dict[str, Any] = {}
    all_w1_titles: List[str] = []
    same_day_dup_days: List[str] = []

    for d in weekdays:
        items = days.get(d, {}).get("items", [])
        day_titles = [i.get("title", "?") for i in items]
        sched_display[d] = day_titles
        all_w1_titles.extend(day_titles)
        # Same-day duplicate check
        if len(day_titles) != len(set(t.lower() for t in day_titles)):
            same_day_dup_days.append(d)

    title_counts_w1: Dict[str, int] = {}
    for t in all_w1_titles:
        title_counts_w1[t] = title_counts_w1.get(t, 0) + 1
    week1_dup_titles = {t: c for t, c in title_counts_w1.items() if c > 1}
    easier_stretch_in_w1 = [
        t for t in all_w1_titles
        if t.startswith("Easier:") or t.startswith("Stretch:")
    ]

    diag["week1_schedule"] = sched_display
    diag["week1_cycle_week"] = cycle_week
    diag["week1_total_activity_slots"] = len(all_w1_titles)
    diag["week1_unique_titles"] = len(set(all_w1_titles))
    diag["week1_duplicate_titles_across_week"] = week1_dup_titles
    diag["week1_same_day_duplicate_days"] = same_day_dup_days
    diag["week1_easier_stretch_variants_present"] = easier_stretch_in_w1

    return diag


def get_debug_payload(
    state: Dict[str, Any],
    category_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a JSON-safe debug payload for use in st.json() or logging."""
    payload: Dict[str, Any] = {
        "cycle_week": state.get("cycle_week", 1),
        "concern_profile": {
            k: v for k, v in state.get("concern_profile", {}).items()
            if k != "llm_result"  # summarize separately
        },
    }

    if category_key:
        bridge_plans = state.get("bridge_plans", {})
        plan = bridge_plans.get(category_key, {})
        payload["bridge_plan"] = {
            "planning_mode": plan.get("planning_mode"),
            "active_bridge_count": len(plan.get("active_bridge_steps", [])),
            "bridges": [
                {
                    "milestone": b.get("milestone", "")[:70],
                    "months": b.get("months"),
                    "activity_family": b.get("activity_family"),
                    "bridge_step_number": b.get("bridge_step_number"),
                    "fallback_active": b.get("_fallback_active", False),
                }
                for b in plan.get("active_bridge_steps", [])
            ],
        }

        cat_fb = state.get("activity_feedback", {}).get(category_key, {})
        payload["feedback"] = {
            title: {"entry_count": len(entries)}
            for title, entries in cat_fb.items()
        }

    return payload
