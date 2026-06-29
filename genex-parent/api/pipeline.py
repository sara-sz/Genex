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
    extra_plans: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Dict[str, str]]]:
    """Translate API feedback (doc["feedback"]) → brain activity_feedback.

    Output shape (what scheduler._v22_build_week2_schedule reads):
        { category_key: { activity_title: {difficulty, performance, engagement} } }

    Titles are recovered from each plan_response by activity_id, since the feedback
    record stores activity_id + domain but not the card title. Records that cannot be
    mapped to a known card title are skipped (domain-level signals are not invented).

    Beta 2.2 Slice 2e-1: `extra_plans` optionally adds ready add-on modules so add-on
    feedback is included alongside the primary base plan. Each item is
    {"plan_id": <module_id>, "plan_response": <resolved add-on plan_response>}. When
    omitted, behaviour is byte-identical to the original primary-only translation
    (the current /plan/next-week caller passes nothing — unchanged until 2e-2).
    """
    id_to_card: Dict[str, Tuple[str, str]] = {}

    def _index(plan_response: Optional[Dict[str, Any]]) -> None:
        for day in (plan_response or {}).get("week", []):
            for act in day.get("activities", []):
                aid = act.get("id")
                if aid:
                    id_to_card[aid] = (act.get("title", ""), act.get("domain", ""))

    _index(base_plan_response)
    allowed_plan_ids: set = {base_plan_id} if base_plan_id else set()
    for p in (extra_plans or []):
        _index(p.get("plan_response"))
        if p.get("plan_id"):
            allowed_plan_ids.add(p["plan_id"])

    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for f in feedback_list or []:
        # Restrict to the plans we are advancing from (primary base + any add-ons).
        # When allowed_plan_ids is empty (no base_plan_id, no extras), keep all.
        if allowed_plan_ids and f.get("plan_id") not in ({None} | allowed_plan_ids):
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


# ── Beta 2.2 Slice 2e-2: integrated next-week across active focus areas ──────
# API-layer composition of FROZEN scheduler functions. No genex_core changes, no LLM:
# add-on Week-1 schedules are reconstructed from the retained add-on activity_banks,
# unioned with the primary Week-1 schedule, then repeat-adapted by the same frozen
# Week-2 builder used by run_refresh_pipeline.

def reconstruct_addon_week1_schedule(
    addon_brain_state: Dict[str, Any], focus_key: str
) -> Optional[Dict[str, Any]]:
    """Re-derive a ready add-on's Week-1 scheduler schedule from its RETAINED bank.

    Works on a deep copy so the stored add-on brain_state is never mutated. Returns a
    weekly_schedule dict whose day items are filtered to `focus_key` only, or None when
    the add-on has no retained activity_banks (old pre-2f-2 module → skip gracefully).
    LLM-free: the bank already exists; only allocation + scheduling are re-run.
    """
    bs = addon_brain_state or {}
    if not (bs.get("activity_banks") or {}):
        return None
    state = copy.deepcopy(bs)
    state.pop("weekly_slot_allocation", None)   # force a fresh allocation
    state["cycle_week"] = 1
    allocate_weekly_slots(state)
    build_weekly_schedule(state)                # cycle_week=1 → Week-1-shaped schedule
    sched = state.get("weekly_schedule") or {}

    filtered_days: Dict[str, Any] = {}
    for day, info in (sched.get("days") or {}).items():
        items = [it for it in info.get("items", []) if it.get("category_key") == focus_key]
        filtered_days[day] = {**info, "items": items}
    return {**sched, "days": filtered_days}


_WEEKDAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


def rebalance_week1_across_domains(
    primary_schedule: Dict[str, Any],
    addon_schedules: List[Dict[str, Any]],
    active_domains: List[str],
) -> Dict[str, Any]:
    """Beta 2.2 Slice 2e-2b — build ONE combined Week-1 that keeps the primary's
    baseline daily slot count but DISTRIBUTES those slots across all active focus
    areas (instead of concatenating, which inflated the daily load).

    Baseline: the per-weekday item count of the primary Week-1 schedule (the parent's
    original daily budget for their chosen time). Each active domain contributes a
    deduplicated pool of activities (from primary + reconstructed add-on schedules).
    The baseline slots are then filled with a WEEKLY-balanced round-robin: each slot
    goes to the active domain with the fewest picks so far (ties broken by
    active_domains order), drawing the next unused activity from that domain's pool.

    Result: same per-day count and ~same weekly total as the original one-domain plan,
    with domains spread ~evenly across the week (difference ≤ 1 when banks allow; a
    domain with too few activities is gracefully under-filled and others absorb the
    remaining slots). Deep-copies the primary schedule; never mutates stored state.
    """
    primary_days = (primary_schedule or {}).get("days", {})
    day_order = [d for d in _WEEKDAY_ORDER if d in primary_days]
    baseline = {d: len((primary_days.get(d) or {}).get("items", [])) for d in day_order}

    # Per-domain deduplicated activity pools (by lowercased title), keyed by category.
    pools: Dict[str, List[Dict[str, Any]]] = {d: [] for d in active_domains}
    seen: Dict[str, set] = {d: set() for d in active_domains}

    def _gather(schedule: Dict[str, Any]) -> None:
        for day in _WEEKDAY_ORDER:
            for item in ((schedule.get("days") or {}).get(day) or {}).get("items", []):
                dom = item.get("category_key", "")
                if dom not in pools:
                    continue
                title = (item.get("title", "") or "").strip().lower()
                if not title or title in seen[dom]:
                    continue
                seen[dom].add(title)
                pools[dom].append(item)

    _gather(primary_schedule or {})
    for sched in addon_schedules:
        _gather(sched or {})

    week_count = {d: 0 for d in active_domains}
    ptr = {d: 0 for d in active_domains}
    new_days: Dict[str, Any] = {}
    for day in day_order:
        items: List[Dict[str, Any]] = []
        for _ in range(baseline[day]):
            candidates = [d for d in active_domains if ptr[d] < len(pools[d])]
            if not candidates:
                break  # all pools exhausted → fewer than baseline (graceful)
            candidates.sort(key=lambda d: (week_count[d], active_domains.index(d)))
            chosen = candidates[0]
            items.append(pools[chosen][ptr[chosen]])
            ptr[chosen] += 1
            week_count[chosen] += 1
        base_info = primary_days.get(day, {})
        new_days[day] = {
            "items": items,
            "total_minutes": base_info.get("total_minutes", 0),
            "is_weekend": False,
        }

    merged = copy.deepcopy(primary_schedule or {})
    merged["days"] = new_days
    return merged


def run_integrated_next_week(
    primary_brain_state: Dict[str, Any],
    merged_week1: Dict[str, Any],
    activity_feedback: Dict[str, Dict[str, Dict[str, str]]],
    active_focus_areas: List[str],
    addon_brain_states: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build ONE integrated Week-2 across all active focus areas from the combined
    Week-1 + merged feedback, using the FROZEN cycle_week=2 repeat-adapt builder.

    Works on a deep copy of the primary brain_state; the caller persists only on
    success. No LLM calls. Raises ValueError if the combined Week-1 is empty.

    Beta 2.2 Slice 2e-3: `addon_brain_states` (the included ready add-ons' retained
    brain_states) are UNIONED into the integrated state's activity_banks AFTER the
    schedule is built, so post-generation customization (swap/add suggestions) can
    draw same-domain activities for add-on-domain cards — i.e. the integrated plan
    behaves like a native multi-domain plan. This does NOT affect the generated
    schedule: the cycle_week=2 repeat-adapt builder reads week1_schedule +
    activity_feedback only and never touches activity_banks.
    """
    if not (merged_week1 or {}).get("days"):
        raise ValueError("No combined Week-1 schedule available to build the next week.")
    state = copy.deepcopy(primary_brain_state or {})
    state["week1_schedule"] = merged_week1
    state["weekly_schedule"] = merged_week1
    state["activity_feedback"] = activity_feedback or {}
    state["cycle_week"] = 2
    state["selected_domain_keys"] = list(active_focus_areas)
    build_weekly_schedule(state)                # → integrated Week-2 (banks not read)

    # Union retained add-on banks so swap/add can serve all active domains later.
    if addon_brain_states:
        banks = dict(state.get("activity_banks") or {})
        for addon_state in addon_brain_states:
            for domain, bank in ((addon_state or {}).get("activity_banks") or {}).items():
                banks.setdefault(domain, bank)  # primary banks win on any key clash
        state["activity_banks"] = banks
    return state
