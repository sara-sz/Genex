"""
genex_core/interview_engine.py
------------------------------
Child profile state, V22 concern router, follow-up schemas,
performance barrier interpretation, and milestone question builder.

V22 changes:
  - concern_router: cognitive-strength suppression via POSITIVE_ROUTING_HINTS
  - Added: get_followup_schema(), normalize_followup_answer(),
           derive_performance_interpretation()
  - record_answer() now stores followup_key + performance_barrier fields
  - Domain selection: respects explicit parent concern; if two explicit domains
    are mentioned, selects both. Never selects Cognitive/Adaptive when parent
    states cognition is a strength.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from genex_core.config import (
    ANSWER_SCORES,
    DOMAIN_CONFIG,
    FOLLOWUP_LABEL_TO_KEY,
    FOLLOWUP_SCHEMAS,
    MOTOR_EMERGING_SUBDOMAINS,
    PERFORMANCE_BARRIER_SCORING,
    POSITIVE_ROUTING_HINTS,
    SUBDOMAIN_KEYWORD_MAP,
    VALID_ANSWERS,
    VALID_PARENT_ANSWERS,
)
from genex_core.milestones import (
    get_cdc_ages,
    get_category_questions,
    get_category_to_subdomains,
    get_subdomain_to_category,
)
from genex_core.safety import build_safety_profile


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

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
        "planned_categories": [],
        "engine_version": "v22",
    }


def init_state_from_profile(
    name: str,
    chronological_months: int,
    diagnosis: str,
    concern: str,
    daily_time_min: int,
) -> Dict[str, Any]:
    """Initialize state from a Streamlit profile form (child name stays in state only)."""
    state = new_state()
    state["child"] = {
        "name": name,                            # display only — never sent to LLM or saved
        "chronological_months": int(chronological_months),
        "diagnosis": str(diagnosis or ""),
        "concern": str(concern or ""),
        "daily_time_min": int(daily_time_min),
    }
    state["concern_profile"] = concern_router(state["child"])
    state["safety_profile"] = build_safety_profile(state["child"])
    return state


# ---------------------------------------------------------------------------
# Answer normalization
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# V22 follow-up schema helpers
# ---------------------------------------------------------------------------

def get_followup_schema(answer_norm: str) -> Optional[Dict[str, Any]]:
    """Return the follow-up sub-question schema for an answer, or None."""
    return FOLLOWUP_SCHEMAS.get(str(answer_norm).strip())


def followup_label_from_key(answer_norm: str, followup_key: str) -> str:
    """Return the display label for a followup_key, or empty string."""
    schema = get_followup_schema(answer_norm)
    if not schema or not followup_key:
        return ""
    for key, label in schema["choices"]:
        if key == followup_key:
            return label
    return ""


def normalize_followup_answer(answer_norm: str, raw_text: str) -> str:
    """Map raw follow-up text to a schema key, with fuzzy fallback."""
    answer_norm = str(answer_norm).strip()
    schema = get_followup_schema(answer_norm)
    if not schema:
        return ""

    raw = str(raw_text).strip().lower()
    if not raw:
        return ""

    # Numbered index selection (1-based)
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(schema["choices"]):
            return schema["choices"][idx][0]

    # Exact key
    raw_norm = raw.replace(" ", "_").replace("/", "_").replace("-", "_")
    choice_keys = {k for k, _ in schema["choices"]}
    if raw_norm in choice_keys:
        return raw_norm

    # Exact label
    labels = FOLLOWUP_LABEL_TO_KEY.get(answer_norm, {})
    if raw in labels:
        return labels[raw]

    # Fuzzy keyword matching
    if answer_norm == "sometimes":
        if "distract" in raw:
            return "distracted"
        if "refus" in raw or "upset" in raw or "tantrum" in raw:
            return "upset_or_refuses"
        if "situation" in raw or "setting" in raw:
            return "only_some_situations"
        if "not sure" in raw or "unsure" in raw:
            return "not_sure"
        return "not_consistent_yet"

    if answer_norm == "with_help":
        if "physical" in raw or "hand" in raw:
            return "physical_help"
        if "reminder" in raw or "prompt" in raw or "step" in raw:
            return "reminders_prompting"
        if "emotion" in raw or "encouragement" in raw or "support" in raw:
            return "emotional_support"
        if "show" in raw or "demonstrat" in raw or "model" in raw:
            return "showing_first"
        return "not_sure"

    if answer_norm == "no":
        if "refus" in raw or "upset" in raw:
            return "upset_or_refuses"
        if "distract" in raw:
            return "distracted_before_doing"
        if "try" in raw or "even when" in raw:
            return "does_not_do_even_when_we_try"
        if "not sure" in raw or "unsure" in raw:
            return "not_sure_why"
        return "not_able_yet"

    return ""


def derive_performance_interpretation(
    answer_norm: str,
    followup_key: str = "",
) -> Dict[str, Any]:
    """Map (answer_norm, followup_key) to skill_ability + performance_barrier."""
    answer_norm = normalize_answer(answer_norm)
    followup_key = str(followup_key or "").strip()

    mapping = PERFORMANCE_BARRIER_SCORING.get((answer_norm, followup_key))
    if mapping is None:
        mapping = PERFORMANCE_BARRIER_SCORING.get((answer_norm, ""))
    if mapping is None:
        mapping = {
            "skill_ability": "unclear",
            "performance_barrier": "unclear",
            "scoring_norm_answer": (
                answer_norm if answer_norm in VALID_PARENT_ANSWERS else "not_sure"
            ),
        }

    scoring_norm = mapping["scoring_norm_answer"]
    return {
        "followup_key": followup_key,
        "followup_label": followup_label_from_key(answer_norm, followup_key),
        "skill_ability": mapping["skill_ability"],
        "performance_barrier": mapping["performance_barrier"],
        "scoring_norm_answer": scoring_norm,
        "scoring_score": ANSWER_SCORES.get(scoring_norm, ANSWER_SCORES["not_sure"]),
    }


# ---------------------------------------------------------------------------
# V22 concern router
# ---------------------------------------------------------------------------

def _apply_concern_propagation(
    subdomain_weights: Dict[str, float],
) -> Dict[str, float]:
    """Lightly propagate strong signals into related subdomains."""
    w = dict(subdomain_weights)

    if w.get("speech_intelligibility", 0) >= 0.60:
        w["expressive_language"] = max(w.get("expressive_language", 0), 0.70)

    if (w.get("expressive_language", 0) >= 0.60
            and w.get("gestural_communication", 0) >= 0.40):
        w["early_vocalization_and_babbling"] = max(
            w.get("early_vocalization_and_babbling", 0), 0.40
        )

    if w.get("emotional_regulation", 0) >= 0.60:
        w["concepts_and_following_directions"] = max(
            w.get("concepts_and_following_directions", 0), 0.40
        )

    if w.get("gross_motor_mobility_and_coordination", 0) >= 0.60:
        w["postural_control_and_transitions"] = max(
            w.get("postural_control_and_transitions", 0), 0.40
        )

    if w.get("adaptive_feeding_cues", 0) >= 0.60:
        w["self_help_motor_skills"] = max(w.get("self_help_motor_skills", 0), 0.30)

    return w


def _pattern_match_weight(pattern_text: str) -> float:
    """Base weight per matched pattern.  Reduced for positive-hint phrases."""
    if any(hint in str(pattern_text).lower() for hint in POSITIVE_ROUTING_HINTS):
        return 0.18
    return 0.35


def _has_cognitive_strength_signal(combined_text: str) -> bool:
    """Return True if the parent text explicitly states cognition is a strength."""
    strength_phrases = [
        "good cognition", "very bright", "bright child", "smart",
        "understands well", "great understanding", "good comprehension",
        "cognitively strong", "no cognitive concern",
    ]
    return any(phrase in combined_text for phrase in strength_phrases)


def concern_router(child: Dict[str, Any]) -> Dict[str, Any]:
    """Convert diagnosis + concern text into structured subdomain and domain weights.

    V22 rules:
    - Deterministic keyword routing (no LLM here).
    - If parent states cognition is a strength, suppress cognitive domain weight.
    - Returns routing_confidence so concern_router_llm.py can decide to escalate.
    """
    diagnosis = str(child.get("diagnosis", "") or "")
    concern = str(child.get("concern", "") or "")
    combined_text = f"{diagnosis} | {concern}".lower()

    subdomain_to_category = get_subdomain_to_category()
    subdomain_weights: Dict[str, float] = {s: 0.0 for s in subdomain_to_category}
    matched_patterns: Dict[str, list] = {s: [] for s in subdomain_to_category}

    for subdomain, patterns in SUBDOMAIN_KEYWORD_MAP.items():
        hits = [pat for pat in patterns if re.search(pat, combined_text)]
        if hits:
            weight = min(1.0, sum(_pattern_match_weight(pat) for pat in hits))
            subdomain_weights[subdomain] = max(
                subdomain_weights.get(subdomain, 0.0), weight
            )
            matched_patterns[subdomain] = hits

    subdomain_weights = _apply_concern_propagation(subdomain_weights)

    domain_weights: Dict[str, float] = {k: 0.0 for k in DOMAIN_CONFIG}
    for subdomain, weight in subdomain_weights.items():
        cat_key = subdomain_to_category.get(subdomain)
        if cat_key in domain_weights:
            domain_weights[cat_key] = max(domain_weights[cat_key], float(weight))

    # V22: suppress cognitive domain when parent explicitly says it's a strength
    if _has_cognitive_strength_signal(combined_text):
        domain_weights["cognitive"] = min(
            domain_weights.get("cognitive", 0.0), 0.20
        )

    top_subdomains = [
        {"subdomain": s, "weight": round(w, 2)}
        for s, w in sorted(
            subdomain_weights.items(), key=lambda kv: kv[1], reverse=True
        )
        if w > 0
    ][:8]

    # Confidence: how clearly the text maps to known subdomains
    total_weight = sum(domain_weights.values())
    routing_confidence = min(1.0, total_weight / 0.60) if total_weight > 0 else 0.0

    return {
        "combined_text": combined_text,
        "subdomain_weights": subdomain_weights,
        "domain_weights": domain_weights,
        "matched_patterns": matched_patterns,
        "top_subdomains": top_subdomains,
        "routing_confidence": round(routing_confidence, 2),
        "cognitive_strength_suppressed": _has_cognitive_strength_signal(combined_text),
    }


def ensure_concern_profile(state: Dict[str, Any]) -> Dict[str, Any]:
    """Compute and cache the concern profile if not already present."""
    if not state.get("concern_profile"):
        state["concern_profile"] = concern_router(state["child"])
    return state["concern_profile"]


# ---------------------------------------------------------------------------
# Domain selection helpers (V22)
# ---------------------------------------------------------------------------

def rank_focus_domains(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rank likely focus domains for shorter onboarding (V22 triage).

    Hybrid signal: concern-router domain weights + delay estimate (if available).
    Respects: explicit parent concern, cognitive-strength suppression.
    """
    concern_profile = ensure_concern_profile(state)
    child = state.get("child", {})
    chrono = max(2, min(int(child.get("chronological_months", 0) or 0), 60))

    ranked = []
    for category_key, cfg in DOMAIN_CONFIG.items():
        domain_weight = float(
            concern_profile.get("domain_weights", {}).get(category_key, 0.0)
        )
        delay_months = float(
            state.get("delay_estimates", {})
            .get(category_key, {})
            .get("delay_months", 0.0)
        )
        delay_signal = min(1.0, delay_months / max(6.0, round(0.20 * chrono)))
        triage_score = round(0.65 * domain_weight + 0.35 * delay_signal, 3)

        ranked.append({
            "category_key": category_key,
            "display": cfg["display"],
            "triage_score": triage_score,
            "concern_signal": round(domain_weight, 3),
            "delay_signal": round(delay_signal, 3),
        })

    return sorted(
        ranked,
        key=lambda x: (x["triage_score"], x["concern_signal"], x["delay_signal"]),
        reverse=True,
    )


def choose_focus_domains(
    state: Dict[str, Any],
    max_domains: int = 2,
) -> List[str]:
    """Choose up to max_domains focus domains for onboarding.

    Rules:
    - If parent explicitly mentions two domains, select both.
    - If parent says cognition is a strength, do not select cognitive unless
      there is a direct cognitive concern that overrides.
    - Daily time controls activity slots, not domain selection.
    """
    ranked = rank_focus_domains(state)
    if not ranked:
        return list(DOMAIN_CONFIG.keys())[:max_domains]

    # Select the top-scoring domain unconditionally
    selected = [ranked[0]["category_key"]]

    # Add second domain only if it has meaningful signal
    if len(ranked) > 1 and max_domains >= 2:
        second = ranked[1]
        if second["triage_score"] >= 0.15:
            selected.append(second["category_key"])

    return selected


# ---------------------------------------------------------------------------
# Milestone question selection (V22)
# ---------------------------------------------------------------------------

MAX_ONBOARDING_DOMAINS = 2
TARGET_QUESTIONS_PER_BAND = 3
MAX_BANDS = 3
MAX_QUESTIONS_TOTAL = 9


def _age_proximity_score(month: int, target_month: int, window: int = 6) -> float:
    dist = abs(month - target_month)
    if dist == 0:
        return 1.0
    if dist <= window:
        return 1.0 - (dist / (window + 1))
    return 0.0


def build_domain_questions(
    state: Dict[str, Any],
    category_key: str,
    approx_dev_months: Optional[int] = None,
    max_questions_total: int = MAX_QUESTIONS_TOTAL,
    target_questions_per_band: int = TARGET_QUESTIONS_PER_BAND,
    max_bands: int = MAX_BANDS,
) -> List[Dict[str, Any]]:
    """Build a targeted milestone question list for one domain.

    Uses bridge_step_1 milestones only (the developmental targets).
    Question text uses "your child" — never the child's name.
    """
    concern_profile = ensure_concern_profile(state)
    child = state.get("child", {})
    chrono_months = max(2, min(int(child.get("chronological_months", 0) or 0), 60))

    if approx_dev_months is None:
        approx_dev_months = state.get("dev_age", {}).get(category_key, chrono_months)
        if approx_dev_months is None:
            approx_dev_months = chrono_months

    approx_dev_months = int(approx_dev_months)

    available_ages = get_cdc_ages(category_key)
    if not available_ages:
        return []

    window_months = 6

    def _closest_age(target: int) -> int:
        return min(available_ages, key=lambda m: abs(m - target))

    # Core bands around estimated dev age
    base_bands = sorted(
        available_ages, key=lambda m: (abs(m - approx_dev_months), m)
    )[:max_bands]
    selected_ages = list(base_bands)

    # Ceiling probe at chronological age
    ceiling = _closest_age(chrono_months)
    if ceiling not in selected_ages:
        selected_ages.append(ceiling)

    # Concern-aware injection
    subdomain_weights = concern_profile.get("subdomain_weights", {})
    category_concern_weight = concern_profile.get("domain_weights", {}).get(
        category_key, 0.0
    )
    subdomain_to_cat = get_subdomain_to_category()
    strong_subdomains = {
        s for s, w in subdomain_weights.items()
        if w >= 0.55 and subdomain_to_cat.get(s) == category_key
    }

    selected_ages = sorted(set(selected_ages))

    # Build question list from the selected age bands
    questions: List[Dict[str, Any]] = []
    qid = 0
    for age in selected_ages:
        qs = get_category_questions(category_key, age, band_months=3)
        for q in qs:
            # Prioritise subdomains matching concern
            subdomain_boost = 0.3 if q.get("subdomain", "") in strong_subdomains else 0.0
            age_score = _age_proximity_score(q["months"], approx_dev_months, window_months)
            row_score = 0.60 * age_score + 0.30 * subdomain_boost + 0.10 * category_concern_weight

            questions.append({
                "question_id": f"{category_key}_{q['months']}_{qid}",
                "months": q["months"],
                "milestone": q["milestone"],
                "subdomain": q.get("subdomain", "unspecified"),
                "activity_family": q.get("activity_family", ""),
                "bridge_step": q.get("bridge_step", ""),
                "parent_explanation": q.get("parent_explanation", ""),
                "selection_score": round(row_score, 3),
                # Privacy: always "your child", never the child's real name
                "question_text": f"Can your child {q['milestone']} right now?",
            })
            qid += 1

    # Deduplicate by milestone text
    seen: set = set()
    unique: List[Dict] = []
    for q in sorted(questions, key=lambda x: (x["months"], -x["selection_score"])):
        key = q["milestone"].lower()
        if key not in seen:
            seen.add(key)
            unique.append(q)

    return unique[:max_questions_total]


# ---------------------------------------------------------------------------
# Record answers
# ---------------------------------------------------------------------------

def record_answer(
    state: Dict[str, Any],
    category_key: str,
    question: Dict[str, Any],
    raw_answer: str,
    followup_key: str = "",
) -> Dict[str, Any]:
    """Record one answered milestone question into state['qna'].

    V22 additions: stores followup_key, skill_ability, performance_barrier,
    and scoring_norm_answer (which may differ from raw answer when a behavioral
    barrier is detected).
    """
    if "qna" not in state:
        state["qna"] = {}
    if category_key not in state["qna"]:
        state["qna"][category_key] = []

    norm = normalize_answer(raw_answer)
    interp = derive_performance_interpretation(norm, followup_key)

    answered_item = {
        "question_id": question.get("question_id", ""),
        "months": question.get("months", 0),
        "milestone": question.get("milestone", ""),
        "subdomain": question.get("subdomain", "unspecified"),
        "activity_family": question.get("activity_family", ""),
        "raw_answer": raw_answer,
        "norm_answer": norm,
        "followup_key": interp["followup_key"],
        "followup_label": interp["followup_label"],
        "skill_ability": interp["skill_ability"],
        "performance_barrier": interp["performance_barrier"],
        "scoring_norm_answer": interp["scoring_norm_answer"],
        "score": interp["scoring_score"],
        "answer_status": "ok",
    }
    state["qna"][category_key].append(answered_item)
    return answered_item


# ---------------------------------------------------------------------------
# Concern profile accessor
# ---------------------------------------------------------------------------

def get_category_concern_peak(state: Dict[str, Any], category_key: str) -> float:
    """Return the highest subdomain weight for a category (used by support_tiers)."""
    profile = ensure_concern_profile(state)
    subdomain_to_cat = get_subdomain_to_category()
    subdomain_weights = profile.get("subdomain_weights", {})
    peak = 0.0
    for subdomain, weight in subdomain_weights.items():
        if subdomain_to_cat.get(subdomain) == category_key:
            peak = max(peak, float(weight))
    return peak


# ---------------------------------------------------------------------------
# Backward-compat aliases (app.py used pre-V22 names)
# ---------------------------------------------------------------------------

build_milestone_questions = build_domain_questions
