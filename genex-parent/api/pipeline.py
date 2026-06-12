"""
api/pipeline.py — Thin orchestration layer for the Genex brain pipeline.

This module is the only place in the API layer that imports from genex_core.
It wraps brain calls and manages the API-level interview state (band-based
adaptive question progression) that mirrors what app.py does with Streamlit
session_state.

Stage coverage per step:
  Step 2  (this file): stages 1–3 — init, routing, domain selection, questions
  Step 3  : stage 4   — answer recording
  Step 4  : stages 5–11 — scoring, bridge, activities, safety, tiers, schedule, gate

Do NOT import from app.py or Streamlit.
Do NOT modify genex_core files.
"""

from typing import Any, Dict, List, Optional, Tuple

from genex_core.interview_engine import (
    build_domain_questions,
    choose_focus_domains,
    init_state_from_profile,
    record_answer,
    score_answer,
    normalize_answer,
)
from genex_core.scoring import finalize_domain_dev_age
from genex_core.support_tiers import (
    determine_family_guidance_floor,
    build_v22_plan_for_category,
)
from genex_core.activity_engine import generate_category_activity_bank
from genex_core.scheduler import allocate_weekly_slots, build_weekly_schedule
from genex_core.final_plan_gate import validate_and_repair_final_plan

# ── Constants ──────────────────────────────────────────────────────────────

DOMAIN_LABELS: Dict[str, str] = {
    "language_and_communication": "Talking and Communicating",
    "movement_and_physical": "Movement & Physical",
    "social_and_emotional": "Social & Emotional",
    "cognitive": "Learning & Cognitive",
}

# Band pass threshold — matches app.py _band_score() logic (score >= 0.5 passes)
_BAND_PASS_THRESHOLD = 0.5

# Max consecutive band failures before stopping a domain — matches app.py
_MAX_CONSEC_FAILS = 2


# ── Stage 1-3: session start ───────────────────────────────────────────────

def run_session_start(
    age_in_months: int,
    diagnosis_for_brain: str,
    sanitized_concern: str,
    daily_time_minutes: int,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Run pipeline stages 1–3:
      1. init_state_from_profile()  — profile init + concern routing (stage 1-2)
      2. choose_focus_domains()     — domain selection (stage 2)
      3. build_domain_questions()   — question building (stage 3)

    The child name is never passed to the brain. "your child" is used instead.
    Question texts from build_domain_questions() already say "Can your child...".

    Returns:
      brain_state : the genex_core state dict (to be stored in the session doc)
      interview   : the API-layer interview tracking dict (also stored in session doc)
    """
    # Stage 1-2: init + concern routing (concern_router is called inside init)
    brain_state = init_state_from_profile(
        name="your child",          # child name never enters the brain
        chronological_months=age_in_months,
        diagnosis=diagnosis_for_brain,
        concern=sanitized_concern,
        daily_time_min=daily_time_minutes,
    )

    # Stage 2: domain selection
    domain_keys = choose_focus_domains(brain_state)

    # Stage 3: question building — one pass per domain, all questions upfront
    # Max questions per domain mirrors app.py: 7 for 1 domain, 5 for 2 domains
    max_q_per_domain = 7 if len(domain_keys) == 1 else 5

    band_state: Dict[str, Any] = {}
    total_questions = 0

    for dk in domain_keys:
        questions = build_domain_questions(
            brain_state, dk, max_questions_total=max_q_per_domain
        )

        # Group questions into age bands (mirrors app.py band loop)
        bands: Dict[str, List[Dict[str, Any]]] = {}
        for q in questions:
            key = str(q["months"])
            bands.setdefault(key, []).append(q)

        band_months = sorted(bands.keys(), key=int)
        total_questions += len(questions)

        band_state[dk] = {
            "band_months": band_months,     # sorted list of month-keys, e.g. ["18","24","30"]
            "bands": bands,                  # month-key → list of question dicts
            "band_idx": 0,                   # which band we are in
            "band_q_idx": 0,                 # which question within the current band
            "consec_fails": 0,               # consecutive band failures
            "current_band_norm_answers": {}, # question_id → norm_answer (for band scoring)
        }

    interview: Dict[str, Any] = {
        "domain_keys": domain_keys,
        "domain_idx": 0,
        "max_q_per_domain": max_q_per_domain,
        "band_state": band_state,
        "questions_answered_total": 0,
        "total_questions_estimate": total_questions,
        "status": "in_progress",
    }

    return brain_state, interview


# ── Stage 4: answer recording ──────────────────────────────────────────────

def run_record_answer(
    brain_state: Dict[str, Any],
    interview: Dict[str, Any],
    question_id: str,
    norm_answer: str,
) -> Tuple[Dict[str, Any], Dict[str, Any], bool]:
    """
    Record one answer (stage 4). Applies the band-based adaptive stopping rule.

    Mirrors app.py logic:
      - After all questions in a band are answered, score the band.
      - Band score = mean of score_answer(norm_answer) for each question.
      - Band passes if score >= 0.5.
      - On pass: move to next band, reset consecutive failures.
      - On fail: increment consecutive failures.
      - Stop domain when: 2 consecutive failures OR all bands exhausted.
      - Advance to next domain when current domain is stopped.
      - Interview complete when all domains are done.

    Returns:
      (brain_state, interview, interview_complete)
    """
    domain_keys = interview["domain_keys"]
    domain_idx = interview["domain_idx"]
    domain = domain_keys[domain_idx]
    bs = interview["band_state"][domain]

    band_months = bs["band_months"]
    band_idx = bs["band_idx"]
    band_q_idx = bs["band_q_idx"]

    if not band_months or band_idx >= len(band_months):
        # Shouldn't happen — interview should have been marked complete
        interview["status"] = "complete"
        return brain_state, interview, True

    current_month_key = band_months[band_idx]
    current_band_qs = bs["bands"][current_month_key]

    if band_q_idx >= len(current_band_qs):
        # Band already exhausted — shouldn't happen in normal flow
        interview["status"] = "complete"
        return brain_state, interview, True

    # Find the question dict matching question_id
    current_q = current_band_qs[band_q_idx]

    # Record answer in the brain state (4-arg call, no followup_key)
    record_answer(brain_state, domain, current_q, norm_answer)

    # Track answer for band scoring
    bs["current_band_norm_answers"][question_id] = norm_answer

    # Advance question pointer within band
    bs["band_q_idx"] = band_q_idx + 1
    interview["questions_answered_total"] += 1

    # Check if we have completed all questions in this band
    band_complete = bs["band_q_idx"] >= len(current_band_qs)

    if band_complete:
        # Score the band
        band_score = _score_band(current_band_qs, bs["current_band_norm_answers"])
        passed = band_score >= _BAND_PASS_THRESHOLD

        if passed:
            bs["consec_fails"] = 0
        else:
            bs["consec_fails"] += 1

        # Move to next band
        bs["band_idx"] = band_idx + 1
        bs["band_q_idx"] = 0
        bs["current_band_norm_answers"] = {}

        # Check domain stopping condition
        domain_done = (
            bs["consec_fails"] >= _MAX_CONSEC_FAILS
            or bs["band_idx"] >= len(band_months)
        )

        if domain_done:
            interview["domain_idx"] += 1

    # Check interview completion
    interview_complete = interview["domain_idx"] >= len(domain_keys)
    if interview_complete:
        interview["status"] = "complete"

    return brain_state, interview, interview_complete


def _score_band(
    questions: List[Dict[str, Any]],
    norm_answers: Dict[str, str],
) -> float:
    """
    Compute band score as mean of score_answer() for each question.
    Mirrors app.py _band_score(). Defaults to "no" (0.0) for missing answers.
    """
    if not questions:
        return 0.0
    scores = [
        score_answer(norm_answers.get(q["question_id"], "no"))
        for q in questions
    ]
    return sum(scores) / len(scores)


# ── Question retrieval ─────────────────────────────────────────────────────

def get_current_question(interview: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Return the next question to ask as a dict, or None if the interview is complete.

    Returns a dict with keys: question_id, question_text, domain, domain_label,
    progress_index, progress_total_estimate.
    """
    if interview.get("status") == "complete":
        return None

    domain_keys = interview["domain_keys"]
    domain_idx = interview["domain_idx"]

    if domain_idx >= len(domain_keys):
        return None

    domain = domain_keys[domain_idx]
    bs = interview["band_state"][domain]
    band_months = bs["band_months"]
    band_idx = bs["band_idx"]

    if band_idx >= len(band_months):
        return None

    current_month_key = band_months[band_idx]
    current_band_qs = bs["bands"][current_month_key]
    band_q_idx = bs["band_q_idx"]

    if band_q_idx >= len(current_band_qs):
        return None

    q = current_band_qs[band_q_idx]

    return {
        "question_id": q["question_id"],
        "question_text": q["question_text"],   # already "Can your child ... right now?"
        "domain": domain,
        "domain_label": DOMAIN_LABELS.get(domain, domain),
        "progress_index": interview["questions_answered_total"],
        "progress_total_estimate": interview["total_questions_estimate"],
    }


def get_expected_question_id(interview: Dict[str, Any]) -> Optional[str]:
    """Return the question_id we expect to receive next, for validation."""
    q = get_current_question(interview)
    return q["question_id"] if q else None


# ── Stages 5–11: plan generation ──────────────────────────────────────────

def run_plan_pipeline(
    brain_state: Dict[str, Any],
    admin_debug: bool = False,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """
    Run pipeline stages 5–11 against an interview-complete brain_state.

    Exactly mirrors app.py lines 838–858. No logic is duplicated —
    every call delegates to the existing genex_core functions.

    Stage mapping:
      5  scoring        finalize_domain_dev_age()
      6  bridge plan    build_v22_plan_for_category()
      7  activity gen   generate_category_activity_bank()
      8  safety filter  (called inside generate_category_activity_bank)
      9  support tier   determine_family_guidance_floor()
      10 scheduling     allocate_weekly_slots() + build_weekly_schedule()
      11 final gate     validate_and_repair_final_plan()

    Returns:
      (brain_state, gate_report)
      gate_report is None unless admin_debug=True.
    """
    domain_keys: List[str] = list(brain_state.get("activity_banks", {}).keys())

    # Need to re-derive domain_keys from interview since activity_banks not yet built.
    # Use the domains stored by run_session_start via choose_focus_domains.
    # They are available in brain_state["concern_profile"] but more reliably
    # we re-run choose_focus_domains (it is deterministic and cheap).
    if not domain_keys:
        domain_keys = choose_focus_domains(brain_state)

    # Stage 5: scoring — compute developmental age per domain
    for dk in domain_keys:
        finalize_domain_dev_age(brain_state, dk)

    # Stage 9: support tier (must precede bridge planning which reads tier)
    determine_family_guidance_floor(brain_state)

    # Stage 6: bridge planning — one plan per domain
    brain_state.setdefault("bridge_plans", {})
    for dk in domain_keys:
        plan = build_v22_plan_for_category(brain_state, dk)
        brain_state["bridge_plans"][dk] = plan

    # Stage 7+8: activity generation + safety filtering (safety is inside activity_engine)
    brain_state.setdefault("activity_banks", {})
    for dk in domain_keys:
        bank = generate_category_activity_bank(brain_state, dk)
        brain_state["activity_banks"][dk] = bank

    # Stage 10: weekly scheduling
    brain_state["cycle_week"] = 1
    allocate_weekly_slots(brain_state)
    build_weekly_schedule(brain_state)

    # Stage 11: final gate — validate and repair
    gate_domains = list(brain_state.get("activity_banks", {}).keys())
    repaired, gate_report = validate_and_repair_final_plan(
        profile=brain_state.get("child", {}),
        selected_domains=gate_domains,
        question_domains=gate_domains,
        weekly_plan=brain_state.get("weekly_schedule", {}),
        candidate_bank=brain_state.get("activity_banks", {}),
    )
    brain_state["weekly_schedule"] = repaired

    return brain_state, (gate_report if admin_debug else None)
