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

import copy
from typing import Any, Dict, List, Optional, Tuple

from genex_core.interview_engine import (
    build_domain_questions,
    choose_focus_domains,
    init_state_from_profile,
    record_answer,
    score_answer,
    normalize_answer,
)
from api.focus_selector import build_focus_block, select_focus
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


# Beta 2.2 focus selection lives in api/focus_selector.py (API layer; genex_core
# stays frozen). build_focus_block / select_focus are imported above.


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

    # Stage 2: domain selection — Beta 2.2: ONE primary focus only (shorter intake,
    # faster plan). The other 3 focus areas stay available for the parent to add later.
    # Primary is chosen by the API-layer focus selector (keyword + confirmed priority);
    # if it detects nothing, fall back to the genex_core single-domain pick.
    primary_key, detected = select_focus(diagnosis_for_brain, sanitized_concern)
    if not primary_key:
        fallback = choose_focus_domains(brain_state, max_domains=1)
        primary_key = fallback[0] if fallback else "language_and_communication"
    domain_keys = [primary_key]
    # Persist the selected focus so the plan pipeline builds ONLY the primary domain.
    brain_state["selected_domain_keys"] = list(domain_keys)
    brain_state["focus"] = build_focus_block(primary_key, detected)

    # Stage 3: question building (shared with add-on focus intake, Beta 2.2 Slice 2b).
    interview = _build_interview_for_domains(brain_state, domain_keys)
    return brain_state, interview


def _build_interview_for_domains(
    brain_state: Dict[str, Any], domain_keys: List[str]
) -> Dict[str, Any]:
    """Build the API-layer interview tracking dict for the given domain(s).

    One pass per domain, all questions upfront, grouped into age bands — mirrors
    app.py. Max questions per domain: 7 for a single domain, 5 for two. Shared by
    run_session_start (primary intake) and run_focus_intake_start (add-on intake).
    """
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

    return {
        "domain_keys": list(domain_keys),
        "domain_idx": 0,
        "max_q_per_domain": max_q_per_domain,
        "band_state": band_state,
        "questions_answered_total": 0,
        "total_questions_estimate": total_questions,
        "status": "in_progress",
    }


def run_focus_intake_start(
    primary_brain_state: Dict[str, Any], focus_key: str
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Beta 2.2 Slice 2b — start a focused add-on intake for ONE chosen focus area.

    Builds a FRESH, independent brain_state from the SAME child profile (age,
    diagnosis, sanitized concern, daily time) as the primary, then asks that single
    domain's normal focused questions (≤7). The primary brain_state/interview and
    the stored plan are never touched — this returns a separate state to be stored
    under doc["added_focus"][focus_key]. No LLM calls; genex_core stays frozen.
    """
    child = (primary_brain_state or {}).get("child") or {}
    brain_state = init_state_from_profile(
        name="your child",          # child name never enters the brain
        chronological_months=int(child.get("chronological_months") or 0),
        diagnosis=child.get("diagnosis") or "",
        concern=child.get("concern") or "",
        daily_time_min=int(child.get("daily_time_min") or 0),
    )
    brain_state["selected_domain_keys"] = [focus_key]
    interview = _build_interview_for_domains(brain_state, [focus_key])
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
        "helper_text": q.get("parent_explanation", "") or "",
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

    # activity_banks isn't built yet at plan time, so re-derive the focus domains.
    # Beta 2.2: prefer the single primary focus persisted at session start; fall back
    # to a 1-domain selection so the plan builds ONLY the primary focus (not 2).
    if not domain_keys:
        domain_keys = (
            brain_state.get("selected_domain_keys")
            or choose_focus_domains(brain_state, max_domains=1)
        )

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


# ── Weekly refresh (Step 5B): Week-2 repeat-adapt ───────────────────────────
# Parent-triggered. Reuses the brain's cycle-week-aware scheduler to repeat Week-1
# activities with harder/easier/repeat cues derived conservatively from feedback.
# No new OpenAI calls — repeat-adapt reuses the existing Week-1 activity banks.

def _aggregate_signal(records: List[Dict[str, Any]]) -> Dict[str, str]:
    """Collapse one activity's API feedback records into a single brain signal dict.

    Conservative precedence (never over-challenge):
      any too_hard / wasn't ready / didn't want to try  → easier  (reduce demand)
      else any too_easy with a 'did_it'                 → harder  (add challenge)
      else                                              → same    (repeat)

    Returns {difficulty, performance, engagement} in the brain's vocabulary, the
    shape scheduler._v22_repeat_adapt_item consumes.
    """
    difficulties = [r.get("difficulty") for r in records]
    completions = [r.get("completion") for r in records]
    enjoyments = [r.get("enjoyment") for r in records]

    any_hard = "too_hard" in difficulties
    any_not_ready = "wasnt_ready_yet" in completions
    any_refused = "didnt_want_to_try" in completions
    any_easy = "too_easy" in difficulties
    any_did = "did_it" in completions
    any_resisted = ("not_really" in enjoyments) or any_refused

    if any_hard or any_not_ready or any_refused:
        # Too hard, not ready, or refused → reduce demand / offer more support.
        difficulty, performance = "too_hard", "couldnt_do_it"
    elif any_easy and any_did:
        # Clearly too easy and the child did it → add a stretch next week.
        difficulty, performance = "too_easy", "done_independently"
    else:
        # Mixed / just-right / no clear signal → repeat as-is (no mastery claim).
        difficulty, performance = "just_right", ""

    engagement = "resisted_it" if any_resisted else ""
    return {"difficulty": difficulty, "performance": performance, "engagement": engagement}


def translate_feedback_to_activity_feedback(
    feedback_list: List[Dict[str, Any]],
    base_plan_response: Dict[str, Any],
    base_plan_id: Optional[str],
) -> Dict[str, Dict[str, Dict[str, str]]]:
    """Translate API feedback (doc["feedback"]) → brain activity_feedback.

    Output shape (what scheduler._v22_build_week2_schedule reads):
        { category_key: { activity_title: {difficulty, performance, engagement} } }

    Titles are recovered from the base (Week-1) plan_response by activity_id, since
    the feedback record stores activity_id + domain but not the card title. Only
    feedback for the base plan is used; records that cannot be mapped to a Week-1
    card title are skipped (domain-level signals are intentionally not invented).
    """
    id_to_card: Dict[str, Tuple[str, str]] = {}
    for day in (base_plan_response or {}).get("week", []):
        for act in day.get("activities", []):
            aid = act.get("id")
            if aid:
                id_to_card[aid] = (act.get("title", ""), act.get("domain", ""))

    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for f in feedback_list or []:
        # Only consider feedback for the base plan we are advancing from.
        if base_plan_id and f.get("plan_id") not in (None, base_plan_id):
            continue
        title, domain = id_to_card.get(f.get("activity_id"), ("", f.get("domain", "")))
        if not title or not domain:
            continue
        grouped.setdefault((domain, title), []).append(f)

    activity_feedback: Dict[str, Dict[str, Dict[str, str]]] = {}
    for (domain, title), recs in grouped.items():
        activity_feedback.setdefault(domain, {})[title] = _aggregate_signal(recs)
    return activity_feedback


def run_refresh_pipeline(
    brain_state: Dict[str, Any],
    activity_feedback: Dict[str, Dict[str, Dict[str, str]]],
) -> Dict[str, Any]:
    """Build a Week-2 repeat-adapt schedule from the persisted Week-1 brain_state.

    Works on a deep copy so the caller's Week-1 brain_state is never corrupted
    (the caller persists the returned state only on success). No LLM calls.

    Raises ValueError if there is no Week-1 schedule to repeat.
    """
    state = copy.deepcopy(brain_state or {})
    week1 = state.get("weekly_schedule") or {}
    if not week1.get("days"):
        raise ValueError("No Week-1 weekly_schedule available to build the next week.")

    state["week1_schedule"] = week1            # explicit, stable base for the builder
    state["activity_feedback"] = activity_feedback or {}
    state["cycle_week"] = 2
    build_weekly_schedule(state)               # sets state["weekly_schedule"] = Week 2
    return state
