"""
genex_core/interview_engine.py
------------------------------
Child profile state, concern router, and milestone question builder.
Extracted from genex_interview_activity_v11.ipynb — logic unchanged.
Input() calls removed; Streamlit UI handles user input.
"""

import re
from typing import Any, Dict, List, Optional

from genex_core.config import (
    DOMAIN_CONFIG,
    ANSWER_SCORES,
    VALID_ANSWERS,
    SUBDOMAIN_KEYWORD_MAP,
    POSITIVE_ROUTING_HINTS,
    MOTOR_EMERGING_SUBDOMAINS,
)
from genex_core.milestones import (
    get_category_questions,
    get_cdc_ages,
    get_subdomain_to_category,
    get_category_to_subdomains,
)
from genex_core.safety import build_safety_profile


# ------------------------------------------------------------------
# State initializer
# ------------------------------------------------------------------
def new_state() -> Dict[str, Any]:
    """Initialize the working state dictionary for one case."""
    return {
        "child": {},
        "concern_profile": {},
        "delay_estimates": {},
        "qna": {},
        "dev_age": {},
        "activity_banks": {},
        "weekly_slot_allocation": {},
        "weekly_schedule": {},
        "safety_profile": {},
        "family_guidance_floor": {},
    }


def init_state_from_profile(
    name: str,
    chronological_months: int,
    diagnosis: str,
    concern: str,
    daily_time_min: int,
) -> Dict[str, Any]:
    """Initialize state from a filled Streamlit profile form."""
    state = new_state()
    state["child"] = {
        "name": name,
        "chronological_months": int(chronological_months),
        "diagnosis": diagnosis,
        "concern": concern,
        "daily_time_min": int(daily_time_min),
    }
    state["concern_profile"] = concern_router(state["child"])
    state["safety_profile"] = build_safety_profile(state["child"])
    return state


# ------------------------------------------------------------------
# Answer normalization
# ------------------------------------------------------------------
def normalize_answer(answer_text: str) -> str:
    """Normalize a raw parent answer into the allowed answer set."""
    t = str(answer_text).strip().lower().replace(" ", "_")
    if t in VALID_ANSWERS:
        return t
    if t in {"notsure", "unsure", "maybe"}:
        return "not_sure"
    return "not_sure"


def score_answer(norm_answer: str) -> float:
    return ANSWER_SCORES.get(norm_answer, 0.0)


# ------------------------------------------------------------------
# Concern router
# ------------------------------------------------------------------
def _apply_concern_propagation(subdomain_weights: Dict[str, float]) -> Dict[str, float]:
    """Lightly propagate strong signals into related subdomains."""
    weights = dict(subdomain_weights)

    if weights.get("speech_intelligibility", 0) >= 0.60:
        weights["expressive_language"] = max(weights.get("expressive_language", 0), 0.70)

    if weights.get("expressive_language", 0) >= 0.60 and weights.get("gestural_communication", 0) >= 0.40:
        weights["early_vocalization_and_babbling"] = max(
            weights.get("early_vocalization_and_babbling", 0), 0.40
        )

    if weights.get("emotional_regulation", 0) >= 0.60:
        weights["concepts_and_following_directions"] = max(
            weights.get("concepts_and_following_directions", 0), 0.40
        )

    if weights.get("gross_motor_mobility_and_coordination", 0) >= 0.60:
        weights["postural_control_and_transitions"] = max(
            weights.get("postural_control_and_transitions", 0), 0.40
        )

    if weights.get("adaptive_feeding_cues", 0) >= 0.60:
        weights["self_help_motor_skills"] = max(weights.get("self_help_motor_skills", 0), 0.30)

    return weights


def _pattern_match_weight(pattern_text: str) -> float:
    pattern_text = str(pattern_text).lower()
    if any(hint in pattern_text for hint in POSITIVE_ROUTING_HINTS):
        return 0.18
    return 0.35


def concern_router(child: Dict[str, Any]) -> Dict[str, Any]:
    """Convert diagnosis + concern text into structured subdomain and domain weights."""
    diagnosis = str(child.get("diagnosis", "") or "")
    concern = str(child.get("concern", "") or "")
    combined_text = f"{diagnosis} | {concern}".lower()

    subdomain_to_category = get_subdomain_to_category()
    subdomain_weights = {s: 0.0 for s in subdomain_to_category.keys()}
    matched_patterns = {s: [] for s in subdomain_to_category.keys()}

    for subdomain, patterns in SUBDOMAIN_KEYWORD_MAP.items():
        matches = []
        for pat in patterns:
            if re.search(pat, combined_text):
                matches.append(pat)
        if matches:
            weight = min(1.0, sum(_pattern_match_weight(pat) for pat in matches))
            subdomain_weights[subdomain] = max(subdomain_weights.get(subdomain, 0.0), weight)
            matched_patterns[subdomain] = matches

    subdomain_weights = _apply_concern_propagation(subdomain_weights)

    domain_weights = {k: 0.0 for k in DOMAIN_CONFIG.keys()}
    for subdomain, weight in subdomain_weights.items():
        category_key = subdomain_to_category.get(subdomain)
        if category_key in domain_weights:
            domain_weights[category_key] = max(domain_weights[category_key], float(weight))

    top_subdomains = [
        {"subdomain": s, "weight": round(w, 2)}
        for s, w in sorted(subdomain_weights.items(), key=lambda kv: kv[1], reverse=True)
        if w > 0
    ][:8]

    return {
        "combined_text": combined_text,
        "subdomain_weights": subdomain_weights,
        "domain_weights": domain_weights,
        "matched_patterns": matched_patterns,
        "top_subdomains": top_subdomains,
    }


def ensure_concern_profile(state: Dict[str, Any]) -> Dict[str, Any]:
    """Compute and cache the concern profile if needed."""
    if not state.get("concern_profile"):
        state["concern_profile"] = concern_router(state["child"])
    if not state.get("safety_profile"):
        state["safety_profile"] = build_safety_profile(state["child"])
    return state["concern_profile"]


# ------------------------------------------------------------------
# Milestone question builder
# ------------------------------------------------------------------
def _age_proximity_score(month: int, approx_dev_months: int, window_months: int) -> float:
    denom = max(window_months / 2, 1)
    score = 1.0 - abs(month - approx_dev_months) / denom
    return float(max(0.0, min(1.0, score)))


def build_milestone_questions(
    state: Dict[str, Any],
    category_key: str,
    window_months: int = 24,
    target_questions_per_band: int = 3,
    max_bands: int = 3,
    max_questions_total: int = 12,
) -> List[Dict[str, Any]]:
    """
    Build a focused set of milestone questions centered on estimated developmental range,
    always including a ceiling probe at the child's chronological age.

    Band selection:
      1. Up to max_bands bands centered around the estimated developmental age
         (catches delay and confirms the floor).
      2. One mandatory ceiling-probe band at the child's chronological age
         (catches children who are on track or advanced in a given domain,
         and finds the true developmental ceiling when delay is suspected).
      3. One concern-aware injection (replaces the farthest base band if a
         strong concern subdomain falls outside the selected range).

    Increasing max_questions_total to 12 (was 9) accommodates the extra ceiling band
    without cutting off the core bands.
    """
    child = state["child"]
    concern_profile = ensure_concern_profile(state)
    subdomain_to_category = get_subdomain_to_category()
    cdc_ages = get_cdc_ages()

    chrono_months = min(child["chronological_months"], 60)
    delay_months = state["delay_estimates"].get(category_key, {}).get("delay_months", 12)

    approx_dev_months = max(2, chrono_months - delay_months)

    # Always pull the full range from estimated floor up to chronological age.
    # This ensures the ceiling-probe band is always available in the subset.
    min_months = max(2, approx_dev_months - window_months // 2)
    max_months = chrono_months  # hard ceiling: never skip the child's actual age band

    subset = get_category_questions(category_key, min_months=min_months, max_months=max_months)
    if subset.empty:
        subset = get_category_questions(
            category_key, min_months=min(cdc_ages), max_months=max(cdc_ages)
        )

    if subset.empty:
        return []

    subset = subset.copy()
    if "subdomain" not in subset.columns:
        subset["subdomain"] = "unspecified"

    category_concern_weight = concern_profile["domain_weights"].get(category_key, 0.0)

    subset["age_score"] = subset["months"].map(
        lambda m: _age_proximity_score(int(m), approx_dev_months, window_months)
    )
    subset["subdomain_weight"] = subset["subdomain"].map(
        lambda s: concern_profile["subdomain_weights"].get(str(s), 0.0)
    )
    subset["row_score"] = (
        0.60 * subset["age_score"]
        + 0.30 * subset["subdomain_weight"]
        + 0.10 * float(category_concern_weight)
    )

    available_months = sorted(subset["months"].unique().tolist())

    # ── Step 1: core bands around estimated dev age ─────────────────────────
    base_months = sorted(
        available_months,
        key=lambda m: (abs(m - approx_dev_months), m)
    )[:max_bands]
    selected_months = list(base_months)

    # ── Step 2: mandatory ceiling-probe band at chronological age ───────────
    # Pick the available band closest to chrono_months.
    ceiling_band = min(available_months, key=lambda m: abs(m - chrono_months))
    if ceiling_band not in selected_months:
        selected_months.append(ceiling_band)

    # ── Step 3: concern-aware injection ─────────────────────────────────────
    strong_rows = subset[subset["subdomain_weight"] >= 0.55].sort_values(
        ["subdomain_weight", "row_score", "months"], ascending=[False, False, True]
    )
    if category_concern_weight >= 0.50 and not strong_rows.empty:
        concern_month = int(strong_rows.iloc[0]["months"])
        if concern_month not in selected_months and len(selected_months) > 1:
            # Replace the farthest base band (never remove the ceiling probe)
            base_only = [m for m in selected_months if m != ceiling_band]
            if base_only:
                farthest_base = max(base_only, key=lambda m: abs(m - approx_dev_months))
                selected_months.remove(farthest_base)
                selected_months.append(concern_month)

    selected_months = sorted(set(selected_months))

    # ── Build question list ──────────────────────────────────────────────────
    questions = []
    for month in selected_months:
        month_rows = subset[subset["months"] == month].sort_values(
            ["row_score", "milestone"], ascending=[False, True]
        )
        month_rows = month_rows.head(target_questions_per_band)

        for _, row in month_rows.iterrows():
            questions.append({
                "question_id": row["question_id"],
                "months": int(row["months"]),
                "milestone": row["milestone"],
                "subdomain": str(row.get("subdomain", "unspecified")),
                "selection_score": round(float(row.get("row_score", 0.0)), 3),
                "question_text": (
                    f"Can {child['name']} {row['milestone']} right now?"
                ),
            })

    questions = sorted(questions, key=lambda q: (q["months"], q["selection_score"] * -1))
    return questions[:max_questions_total]


# ------------------------------------------------------------------
# Record a single answered question into state
# ------------------------------------------------------------------
def record_answer(
    state: Dict[str, Any],
    category_key: str,
    question: Dict[str, Any],
    raw_answer: str,
) -> Dict[str, Any]:
    """Record one answered milestone question into state['qna']."""
    if "qna" not in state:
        state["qna"] = {}
    if category_key not in state["qna"]:
        state["qna"][category_key] = []

    norm = normalize_answer(raw_answer)
    answered_item = {
        "question_id": question["question_id"],
        "months": question["months"],
        "milestone": question["milestone"],
        "subdomain": question.get("subdomain", "unspecified"),
        "raw_answer": raw_answer,
        "norm_answer": norm,
        "score": score_answer(norm),
        "answer_status": "ok",
    }
    state["qna"][category_key].append(answered_item)
    return answered_item
