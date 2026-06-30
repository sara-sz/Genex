"""
api/main.py — Genex FastAPI application

Current endpoints:
  GET  /health                              — public health check
  POST /api/v1/session/start                — create session, run stages 1-3
  POST /api/v1/session/{id}/answer          — record answer, return next question
  POST /api/v1/session/{id}/plan            — run stages 5-11, return weekly plan
  POST /api/v1/session/{id}/feedback        — save activity feedback
  POST /api/v1/session/{id}/report          — generate care team report
  GET  /api/v1/session/{id}                 — reload saved session
  GET  /api/v1/_auth_check                  — auth smoke test (ADMIN_DEBUG=1 only)

Coming in later steps:
  POST /api/v1/session/{id}/weekly-refresh  — generate next-week plan

Do NOT touch genex_core/, app.py, tests/, requirements.txt, or Dockerfile.
"""

import os
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from api.adapters import (
    DOMAIN_LABELS,
    adapt_weekly_plan,
    apply_addon_provenance,
    apply_integrated_provenance,
    build_balanced_current_week,
    build_plan_internal,
    normalize_diagnosis_for_brain,
    sanitize_concern,
)
from api.auth import AuthUser, require_auth, verify_beta_code
from api.focus_selector import FOCUS_LABELS, focus_view
from api.customization import (
    add_suggestions,
    build_card_from_bank,
    choose_add_day,
    empty_overlay,
    ensure_overlay,
    find_bank_activity_by_suggestion_id,
    find_overlay_internal,
    get_overlay,
    match_plan_day,
    overlay_summary,
    plan_day_labels,
    plan_has_activity,
    resolve_customization_target,
    resolve_plan_response,
    swap_suggestions,
    _add_unique,
)
from api.pipeline import (
    get_current_question,
    get_expected_question_id,
    reconstruct_addon_week1_schedule,
    rebalance_week1_across_domains,
    run_focus_intake_start,
    run_integrated_next_week,
    run_plan_pipeline,
    run_record_answer,
    run_refresh_pipeline,
    run_session_start,
    translate_feedback_to_activity_feedback,
)
from api.planning_period import (
    _local_date,
    activity_date_for_day,
    compute_next_week_period,
    compute_plan_period,
    next_week_available_from,
)
from api.report_generator import REPORT_TITLES, generate_report_body
from api.schemas import (
    AddActivityRequest,
    AddonActivityRequest,
    AddonAddActivityRequest,
    AddonSwapRequest,
    AnswerRequest,
    FeedbackRequest,
    InterviewCompleteResponse,
    NextQuestionResponse,
    ReportRequest,
    SessionStartRequest,
    SessionStartResponse,
    SwapRequest,
)
from api.session_store import (
    SessionLoadError,
    SessionSaveError,
    find_latest_for_uid as store_find_latest_for_uid,
    load as store_load,
    new_session_doc,
    save as store_save,
)

# ── App ────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Genex API",
    version="v22",
    docs_url="/docs",
    redoc_url="/redoc",
)

_ADMIN_DEBUG = os.environ.get("ADMIN_DEBUG", "0").strip() == "1"

# Primary /plan in-flight guard (freeze-blocker fix): a synchronous generation runs
# ~80–126s. If a retry arrives while one is in flight, do NOT start a second
# run_plan_pipeline. A marker older than this threshold is treated as stale (crashed
# request) and regeneration is allowed. Chosen > Cloud Run's 600s request timeout so a
# still-running request is never mistaken for stale (mirrors the add-on /generate rule).
PLAN_GENERATION_STALE_SECONDS = 900  # 15 minutes

# ── CORS ───────────────────────────────────────────────────────────────────

_raw_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000").strip()
_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Shared session helper ──────────────────────────────────────────────────

def _require_session(uid: str, session_id: str, force_remote: bool = False) -> Dict[str, Any]:
    """
    Load a session document and enforce ownership.
    Used by every endpoint that operates on an existing session.

    force_remote=True reads authoritative durable state (bypassing the per-instance
    memory cache) — used only by the primary /plan in-flight guard. Default False
    keeps existing behavior for all other endpoints.

    Raises:
      404 if the session does not exist in memory/GCS.
      403 if the session exists but belongs to a different uid.
      500 if GCS returns an error (SessionLoadError).
    """
    try:
        doc = store_load(uid, session_id, force_remote=force_remote)
    except SessionLoadError as exc:
        raise HTTPException(status_code=500, detail=f"Session storage error: {exc}")

    if doc is None:
        raise HTTPException(status_code=404, detail="session_not_found")

    if doc.get("owner_uid") != uid:
        raise HTTPException(status_code=403, detail="session_not_owned_by_user")

    return doc


# ── Public endpoints ───────────────────────────────────────────────────────

@app.get("/health", tags=["public"])
async def health():
    """Public health check. No auth required. Used by Cloud Run probes."""
    return {"ok": True, "service": "genex-api", "version": "v22"}


# ── Auth smoke-test stub — ADMIN_DEBUG=1 only ──────────────────────────────

if _ADMIN_DEBUG:
    @app.get("/api/v1/_auth_check", tags=["internal"])
    async def auth_check(auth: AuthUser = Depends(require_auth)):
        """
        Protected stub for verifying auth wiring. Only available when ADMIN_DEBUG=1.
        Expected test outcomes:
          No Authorization header        → 401
          Invalid / expired token        → 401
          Valid token, email not in list → 403
          Valid token, in list           → 200 {"ok": true, "uid": "...", "email": "..."}
        """
        return {"ok": True, "uid": auth.uid, "email": auth.email}


# ── Session endpoints ──────────────────────────────────────────────────────

@app.post(
    "/api/v1/session/start",
    response_model=SessionStartResponse,
    tags=["session"],
)
async def session_start(
    body: SessionStartRequest,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """
    Create a new session and return the first interview question.

    Runs pipeline stages 1-3:
      1. init_state_from_profile() — profile init + concern routing
      2. choose_focus_domains()    — 1-2 domains selected from concern signal
      3. build_domain_questions()  — adaptive question bank built per domain

    Privacy rules:
      - child_name is sanitised out of parent_concern before any storage or OpenAI use.
      - The brain receives name="your child" — never the actual child name.
      - GCS session document contains no child name.

    Beta access:
      When REQUIRE_BETA_CODE is enabled, body.beta_access_code must match the
      configured BETA_ACCESS_CODE (case-insensitive, space-trimmed) or this
      returns 403. The code is never stored; the session records only a
      beta_authorized flag. Subsequent endpoints do not re-check the code —
      they rely on the Firebase token and session ownership.

    Saves session to GCS before returning. Raises 500 if the save fails.
    """
    # Beta gate — reject before doing any pipeline work.
    verify_beta_code(body.beta_access_code)

    diagnosis_for_brain = normalize_diagnosis_for_brain(body.diagnosis_or_condition)
    sanitized_concern_text = sanitize_concern(body.parent_concern, body.child_name)

    try:
        brain_state, interview = run_session_start(
            age_in_months=body.age_in_months,
            diagnosis_for_brain=diagnosis_for_brain,
            sanitized_concern=sanitized_concern_text,
            daily_time_minutes=body.daily_time_minutes,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Pipeline error: {exc}")

    session_id = str(uuid.uuid4())
    doc = new_session_doc(
        session_id=session_id,
        owner_uid=auth.uid,
        age_in_months=body.age_in_months,
        daily_time_minutes=body.daily_time_minutes,
        diagnosis_or_condition=body.diagnosis_or_condition,
        brain_state=brain_state,
        interview=interview,
        timezone=body.timezone,
        beta_authorized=True,  # passed the beta gate above; code itself is never stored
    )
    # Beta 2.2: surface the primary-focus metadata at the top level for the frontend.
    doc["focus"] = brain_state.get("focus") or {}

    try:
        store_save(auth.uid, session_id, doc)
    except SessionSaveError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save session: {exc}")

    current_q = get_current_question(interview)
    if current_q is None:
        raise HTTPException(
            status_code=500,
            detail="No questions generated for this profile. Please try again.",
        )

    return SessionStartResponse(
        session_id=session_id,
        status="questions",
        domains=interview["domain_keys"],
        total_questions_estimate=interview["total_questions_estimate"],
        current_question=current_q,
    )


@app.post(
    "/api/v1/session/{session_id}/answer",
    tags=["session"],
)
async def session_answer(
    session_id: str,
    body: AnswerRequest,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """
    Record one answer and return the next question or interview_complete.

    Rules:
      - Session must exist and belong to the authenticated user.
      - Answer must be one of: yes | sometimes | with_help | no | not_sure
      - No follow-up questions. record_answer() is called with 4 args only.
      - Band-based adaptive stopping mirrors app.py exactly:
          score >= 0.5 → band passes, continue to next band
          2 consecutive band failures → domain done, advance to next domain
      - Session is saved to GCS before returning.
      - If the interview is already complete, returns 409 (idempotency guard).
    """
    doc = _require_session(auth.uid, session_id)

    interview = doc["interview"]
    brain_state = doc["brain_state"]

    # Guard: interview already finished
    if interview.get("status") == "complete":
        raise HTTPException(
            status_code=409,
            detail="Interview is already complete. Call /plan to generate the weekly plan.",
        )

    # Validate that the incoming question_id matches what we expect next
    expected_qid = get_expected_question_id(interview)
    if expected_qid is None:
        raise HTTPException(
            status_code=409,
            detail="No pending question found. Interview may already be complete.",
        )
    if body.question_id != expected_qid:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unexpected question_id {body.question_id!r}. "
                f"Expected {expected_qid!r}. "
                "Answers must be submitted in order."
            ),
        )

    # Record the answer — updates brain_state["qna"] and advances interview state
    try:
        brain_state, interview, interview_complete = run_record_answer(
            brain_state=brain_state,
            interview=interview,
            question_id=body.question_id,
            norm_answer=body.answer,  # already validated by Pydantic Literal
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Answer recording error: {exc}")

    # Write back to the session document
    doc["brain_state"] = brain_state
    doc["interview"] = interview
    if interview_complete:
        doc["status"] = "interview_complete"

    # Save to GCS before responding
    try:
        store_save(auth.uid, session_id, doc)
    except SessionSaveError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save session: {exc}")

    # Return next question or completion
    if interview_complete:
        return InterviewCompleteResponse(
            status="interview_complete",
            ready_for_plan=True,
            questions_answered=interview["questions_answered_total"],
        )

    next_q = get_current_question(interview)
    if next_q is None:
        # Should not happen — run_record_answer would have set interview_complete
        raise HTTPException(status_code=500, detail="No next question available.")

    return NextQuestionResponse(
        status="next_question",
        current_question=next_q,
    )


@app.post(
    "/api/v1/session/{session_id}/plan",
    tags=["session"],
)
async def session_plan(
    session_id: str,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """
    Run stages 5–11 and return the frontend-ready weekly plan.

    Stages called (exact mirror of app.py):
      5  finalize_domain_dev_age()          — scoring
      6  build_v22_plan_for_category()      — bridge planning
      7  generate_category_activity_bank()  — activity generation
      8  (safety filtering inside stage 7)
      9  determine_family_guidance_floor()  — support tier
      10 allocate_weekly_slots() + build_weekly_schedule()
      11 validate_and_repair_final_plan()   — final gate

    Planning period:
      The plan is anchored to Monday–Sunday in the parent's local timezone
      (stored as doc["timezone"] from session/start).
      If the parent starts mid-week, the response only includes days from
      today through Sunday — past days of the current week are omitted.
      If the scheduler has no activities for a given day (e.g. Saturday when
      only weekdays were generated), that day is silently excluded.

    Storage:
      plan_response and plan_internal are stored inside doc["plans"][plan_id]
      together with the plan_period. doc["current_plan_id"] points to the
      latest plan. This structure supports future weekly refresh without
      overwriting previous plan history.

    Privacy: child name is never in brain_state (passed as "your child" at
    session start) so no name will appear in the plan response or GCS doc.

    gate_report is stored in brain_state only when ADMIN_DEBUG=1.
    It is never included in the parent-facing response regardless of ADMIN_DEBUG.

    Raises:
      409 if the interview is not yet complete.
      409 if the plan has already been generated (idempotency guard).
      500 if plan generation or GCS save fails.
    """
    # Read authoritative durable state (not the per-instance cache) so the
    # idempotency + in-flight guard decisions are correct across Cloud Run instances.
    doc = _require_session(auth.uid, session_id, force_remote=True)

    # Guard: interview must be complete
    if doc.get("status") not in ("interview_complete", "plan_ready"):
        raise HTTPException(
            status_code=409,
            detail=(
                "Interview is not complete. "
                "Answer all questions before generating the plan."
            ),
        )

    # Idempotency: return cached plan if already generated for this session
    current_plan_id = doc.get("current_plan_id")
    if current_plan_id and current_plan_id in (doc.get("plans") or {}):
        return doc["plans"][current_plan_id]["plan_response"]

    # ── In-flight guard ────────────────────────────────────────────────────
    # If a generation is already running for this session and is not stale, do NOT
    # start a second run_plan_pipeline — return 409 so the frontend polls instead.
    started_at = doc.get("plan_generation_started_at")
    if started_at:
        started_dt = _parse_iso(started_at)
        is_stale = (
            started_dt is None
            or (datetime.now(timezone.utc) - started_dt).total_seconds()
            > PLAN_GENERATION_STALE_SECONDS
        )
        if not is_stale:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "plan_generating",
                    "message": "Your plan is being generated. Keep this screen open — it can take a couple of minutes.",
                    "started_at": started_at,
                    "poll": "GET /api/v1/session/current",
                },
            )
        # else: stale marker (crashed request) → fall through and regenerate.

    # Mark generation in-flight + persist FIRST, so concurrent retries see it and do
    # not duplicate work. Status stays "interview_complete"; the marker drives polling.
    doc["plan_generation_started_at"] = datetime.now(timezone.utc).isoformat()
    try:
        store_save(auth.uid, session_id, doc)
    except SessionSaveError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save session: {exc}")

    brain_state = doc["brain_state"]

    # Compute planning period in the parent's local timezone
    timezone_str: str = doc.get("timezone") or "UTC"
    plan_period = compute_plan_period(timezone_str)

    # Run pipeline stages 5–11 in a worker thread so the ~80–126s blocking generation
    # does NOT block the event loop — concurrent /session/current polls and /plan
    # retries on this instance are served immediately during generation.
    try:
        brain_state, gate_report = await run_in_threadpool(
            run_plan_pipeline,
            brain_state=brain_state,
            admin_debug=_ADMIN_DEBUG,
        )
    except Exception as exc:
        # Clear the in-flight marker so the parent can retry.
        doc.pop("plan_generation_started_at", None)
        try:
            store_save(auth.uid, session_id, doc)
        except SessionSaveError:
            pass
        raise HTTPException(
            status_code=500,
            detail=f"Plan generation failed: {exc}",
        )

    weekly_schedule = brain_state.get("weekly_schedule", {})

    # Adapt to frontend JSON (parent-facing, filtered to planning period, no internals)
    plan_response = adapt_weekly_plan(
        session_id=session_id,
        age_in_months=doc["age_in_months"],
        daily_time_minutes=doc["daily_time_minutes"],
        weekly_schedule=weekly_schedule,
        plan_period=plan_period,
    )

    # Capture rich internal metadata for GCS (never returned to frontend)
    plan_internal = build_plan_internal(
        session_id=session_id,
        brain_state=brain_state,
        weekly_schedule=weekly_schedule,
        plan_period=plan_period,
        daily_time_minutes=doc["daily_time_minutes"],
    )

    # Attach gate_report to brain_state when ADMIN_DEBUG=1 (never in response)
    if _ADMIN_DEBUG and gate_report:
        brain_state["_gate_report"] = gate_report

    # Store plan in history keyed by plan_id; update current pointer
    plan_id = plan_period["plan_id"]
    doc["brain_state"] = brain_state
    doc["status"] = "plan_ready"
    doc["plan_generated"] = True          # kept for backwards-compat checks
    doc["current_plan_id"] = plan_id
    doc.pop("plan_generation_started_at", None)   # clear the in-flight marker
    doc.setdefault("plans", {})[plan_id] = {
        "plan_period": plan_period,
        "plan_response": plan_response,
        "plan_internal": plan_internal,
    }

    # Save to GCS before responding
    try:
        store_save(auth.uid, session_id, doc)
    except SessionSaveError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Plan generated but failed to save: {exc}",
        )

    return plan_response


# ── Weekly refresh endpoint (Step 5B) — Week-2 repeat-adapt ─────────────────

@app.post(
    "/api/v1/session/{session_id}/plan/next-week",
    tags=["session"],
)
async def session_plan_next_week(
    session_id: str,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """
    Create the next week's plan (Week 2) from the current Week-1 plan using the
    brain's repeat-adapt engine. Parent-triggered; no background job.

    Behavior:
      - Same Firebase auth + session ownership as other session routes.
      - Requires a current plan to advance from (else 409).
      - Builds Week 2 via cycle_week=2 repeat-adapt — reuses Week-1 activities with
        harder/easier/repeat cues from feedback. No new OpenAI calls.
      - Stores the new plan under a NEW plan_id in doc["plans"]; Week 1 is left
        untouched. current_plan_id is updated only after the new plan is saved.
      - Idempotent: if the current plan is already a Week-2 refresh, or a Week-2
        was already built from the current base plan, the existing plan is
        returned instead of creating a duplicate.

    Returns the same response shape as POST /plan (Lovable reuses the renderer).
    plan_period additionally carries cycle_week=2, plan_type="next_week",
    base_plan_id.
    """
    doc = _require_session(auth.uid, session_id)

    plans: Dict[str, Any] = doc.get("plans") or {}
    base_plan_id = doc.get("current_plan_id")
    if not base_plan_id or base_plan_id not in plans:
        raise HTTPException(
            status_code=409,
            detail="No current plan to advance from. Generate the first plan first.",
        )

    base_entry = plans[base_plan_id]
    base_period = base_entry.get("plan_period") or {}

    # ── Idempotency / duplicate-prevention guards ──────────────────────────
    # 1. If the current plan is already a Week-2 refresh, return it unchanged
    #    (Beta 2.0 MVP advances at most one week — no Week 3).
    if int(base_period.get("cycle_week", 1) or 1) >= 2:
        return base_entry["plan_response"]
    # 2. If a Week-2 plan was already built from this base, return that one.
    for entry in plans.values():
        if (entry.get("plan_period") or {}).get("base_plan_id") == base_plan_id:
            return entry["plan_response"]

    # ── Eligibility guard: Week 2 only becomes available after Week 1 ends ──
    # Available from the Monday after the base plan's plan_end_date (Week 1's
    # Sunday). Before that, do not create Week 2 — return a clear 409 (consistent
    # with the API's other "not in the right state" responses) and no plan.
    available_from = next_week_available_from(base_period)
    today_local = _local_date(
        doc.get("timezone") or "UTC", datetime.now(timezone.utc)
    ).isoformat()
    if today_local < available_from:  # ISO date strings compare chronologically
        raise HTTPException(
            status_code=409,
            detail={
                "code": "next_week_not_ready",
                "message": "Week 2 will be available after your current week ends.",
                "available_from": available_from,
                "current_plan_end_date": base_period.get("plan_end_date", ""),
            },
        )

    # ── Require a complete stored Week-1 brain_state ───────────────────────
    brain_state = doc.get("brain_state") or {}
    if not (brain_state.get("weekly_schedule") or {}).get("days"):
        raise HTTPException(
            status_code=409,
            detail="Stored plan state is incomplete; cannot build the next week.",
        )

    # ── Beta 2.2 Slice 2e-2: active focus areas = primary + ready add-ons ─────
    primary_focus_key = (doc.get("focus") or {}).get("primary_focus_key", "") or (
        (brain_state.get("selected_domain_keys") or [""])[0]
    )
    ready_addons = [
        (fk, e) for fk, e in (doc.get("added_focus") or {}).items()
        if (e or {}).get("status") == "ready"
    ]

    # Default markers for the primary-only path (no integration).
    active_focus_areas: List[str] = [primary_focus_key] if primary_focus_key else []
    skipped_focus_areas: List[str] = []

    try:
        if not ready_addons:
            # ── Primary-only: unchanged Beta 2.1 behaviour (byte-identical) ──
            activity_feedback = translate_feedback_to_activity_feedback(
                doc.get("feedback") or [],
                base_entry.get("plan_response") or {},
                base_plan_id,
            )
            refresh_state = run_refresh_pipeline(brain_state, activity_feedback)
            integrated = False
        else:
            # ── Integrated: one weekly plan across all active focus areas ────
            addon_schedules: List[Dict[str, Any]] = []
            addon_brain_states: List[Dict[str, Any]] = []
            extra_plans: List[Dict[str, Any]] = []
            included_focus: List[str] = []
            for fk, e in ready_addons:
                addon_bs = e.get("brain_state") or {}
                sched = reconstruct_addon_week1_schedule(addon_bs, fk)
                if sched is None or not (sched.get("days") or {}):
                    skipped_focus_areas.append(fk)  # old add-on w/o retained bank → skip
                    continue
                addon_schedules.append(sched)
                addon_brain_states.append(addon_bs)  # for the bank union (2e-3)
                included_focus.append(fk)
                extra_plans.append({
                    "plan_id": e.get("module_id"),
                    "plan_response": resolve_plan_response(
                        e.get("plan_response") or {}, e.get("customizations")
                    ),
                })
            active_focus_areas = ([primary_focus_key] if primary_focus_key else []) + included_focus

            # 2e-2b: budget-balanced fill (not concatenation) — keep the primary's
            # baseline daily slot count, distribute slots across all active focuses.
            merged_week1 = rebalance_week1_across_domains(
                brain_state.get("weekly_schedule") or {}, addon_schedules, active_focus_areas
            )
            activity_feedback = translate_feedback_to_activity_feedback(
                doc.get("feedback") or [],
                base_entry.get("plan_response") or {},
                base_plan_id,
                extra_plans=extra_plans,
            )
            # 2e-3: union add-on banks so swap/add work on add-on-domain cards.
            refresh_state = run_integrated_next_week(
                brain_state, merged_week1, activity_feedback, active_focus_areas,
                addon_brain_states=addon_brain_states,
            )
            integrated = True
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Next-week plan generation failed: {exc}",
        )

    weekly_schedule = refresh_state.get("weekly_schedule", {})

    # New plan period anchored to the Monday after Week 1 ends (full Mon–Sun week,
    # is_partial_week=False) with additive markers (cycle_week=2, plan_type,
    # base_plan_id, available_from).
    plan_period = compute_next_week_period(base_period, doc.get("timezone") or "UTC")
    if integrated:
        # Additive metadata so Lovable can see the integration (does not change shape).
        plan_period["active_focus_areas"] = active_focus_areas
        plan_period["skipped_focus_areas"] = skipped_focus_areas
        plan_period["is_integrated"] = True

    plan_response = adapt_weekly_plan(
        session_id=session_id,
        age_in_months=doc["age_in_months"],
        daily_time_minutes=doc["daily_time_minutes"],
        weekly_schedule=weekly_schedule,
        plan_period=plan_period,
    )
    if integrated:
        # Per-activity focus/domain provenance on the integrated plan (additive).
        apply_integrated_provenance(plan_response, primary_focus_key)
    plan_internal = build_plan_internal(
        session_id=session_id,
        brain_state=refresh_state,
        weekly_schedule=weekly_schedule,
        plan_period=plan_period,
        daily_time_minutes=doc["daily_time_minutes"],
    )

    # Persist only after the new plan is fully built. Week 1 stays as-is in history.
    new_plan_id = plan_period["plan_id"]
    doc["brain_state"] = refresh_state
    doc["current_plan_id"] = new_plan_id
    doc.setdefault("plans", {})[new_plan_id] = {
        "plan_period": plan_period,
        "plan_response": plan_response,
        "plan_internal": plan_internal,
    }

    try:
        store_save(auth.uid, session_id, doc)
    except SessionSaveError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Next-week plan built but failed to save: {exc}",
        )

    return plan_response


# ── Plan acceptance endpoint (Beta 2.1 Step 2B) ─────────────────────────────

@app.post(
    "/api/v1/session/{session_id}/plan/{plan_id}/accept",
    tags=["session"],
)
async def session_plan_accept(
    session_id: str,
    plan_id: str,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """
    Mark the current weekly plan as accepted by the parent.

    Acceptance is per session_id + plan_id, stored OUTSIDE the generated plan
    (doc["plans"][plan_id]["accepted_at"] / ["accepted_by_uid"]). The
    plan_response and plan_internal are never mutated. Each week has its own
    plan_id, so accepting Week 1 does not accept Week 2.

    Rules:
      - Same Firebase auth + session ownership as other session routes.
      - 404 if plan_id is not in this session's plans.
      - 409 (only_current_plan_can_be_accepted) if plan_id is not the current plan.
      - Idempotent: re-accepting returns the same accepted_at (never overwritten).
    """
    doc = _require_session(auth.uid, session_id)

    plans: Dict[str, Any] = doc.get("plans") or {}
    if plan_id not in plans:
        raise HTTPException(status_code=404, detail="plan_not_found")

    # Only the current plan can be accepted; previous weeks are read-only.
    if plan_id != doc.get("current_plan_id"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "only_current_plan_can_be_accepted",
                "message": "Only the current weekly plan can be accepted.",
            },
        )

    plan_entry = plans[plan_id]
    existing = plan_entry.get("accepted_at")

    if existing:
        # Idempotent: already accepted — do not overwrite or re-save.
        return {
            "session_id": session_id,
            "plan_id": plan_id,
            "accepted": True,
            "accepted_at": existing,
        }

    accepted_at = datetime.now(timezone.utc).isoformat()
    plan_entry["accepted_at"] = accepted_at
    plan_entry["accepted_by_uid"] = auth.uid

    try:
        store_save(auth.uid, session_id, doc)
    except SessionSaveError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save acceptance: {exc}")

    return {
        "session_id": session_id,
        "plan_id": plan_id,
        "accepted": True,
        "accepted_at": accepted_at,
    }


# ── Current-week customization endpoints (Beta 2.1 Step 2C) ─────────────────

def _require_current_plan(doc: Dict[str, Any], plan_id: str) -> Dict[str, Any]:
    """Guard: plan must exist (404) and be the current plan (409). Returns the entry."""
    plans: Dict[str, Any] = doc.get("plans") or {}
    if plan_id not in plans:
        raise HTTPException(status_code=404, detail="plan_not_found")
    if plan_id != doc.get("current_plan_id"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "only_current_plan_can_be_customized",
                "message": "Only the current weekly plan can be customized.",
            },
        )
    return plans[plan_id]


def _require_customizable_activity(
    doc: Dict[str, Any], plan_id: str, activity_id: str
) -> str:
    """Current-plan guard + map the visible activity_id to its canonical overlay
    key. Any activity visible in the resolved plan (original, added, or a swapped
    replacement) is actionable. Returns the canonical key; 404 if not actionable.
    """
    _require_current_plan(doc, plan_id)
    target = resolve_customization_target(doc, plan_id, activity_id)
    if target is None:
        raise HTTPException(status_code=404, detail="activity_not_found")
    return target


@app.post(
    "/api/v1/session/{session_id}/plan/{plan_id}/activity/{activity_id}/remove",
    tags=["session"],
)
async def session_activity_remove(
    session_id: str,
    plan_id: str,
    activity_id: str,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """
    Hide an activity from the current week. Overlay-only and LLM-free.

    Adds activity_id to the overlay's removed_activity_ids. Never mutates the
    stored plan_response/plan_internal or the activity bank; existing feedback is
    untouched. Idempotent (no duplicate ids). Only the current plan is editable.
    """
    doc = _require_session(auth.uid, session_id)
    target = _require_customizable_activity(doc, plan_id, activity_id)

    overlay = ensure_overlay(doc, plan_id)
    changed = _add_unique(overlay.setdefault("removed_activity_ids", []), target)

    if changed:
        try:
            store_save(auth.uid, session_id, doc)
        except SessionSaveError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to save customization: {exc}")

    return {
        "session_id": session_id,
        "plan_id": plan_id,
        "activity_id": activity_id,
        "removed": True,
        "saved_for_later": target in (overlay.get("saved_for_later_activity_ids") or []),
    }


@app.post(
    "/api/v1/session/{session_id}/plan/{plan_id}/activity/{activity_id}/save-for-later",
    tags=["session"],
)
async def session_activity_save_for_later(
    session_id: str,
    plan_id: str,
    activity_id: str,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """
    Save an activity for later ("I like this, but not this week").

    Adds activity_id to saved_for_later_activity_ids AND hides it from the current
    week via removed_activity_ids. Overlay-only, LLM-free, idempotent (no duplicate
    ids in either list). Never mutates the generated plan or the activity bank.
    Only the current plan is editable.
    """
    doc = _require_session(auth.uid, session_id)
    target = _require_customizable_activity(doc, plan_id, activity_id)

    overlay = ensure_overlay(doc, plan_id)
    a1 = _add_unique(overlay.setdefault("saved_for_later_activity_ids", []), target)
    a2 = _add_unique(overlay.setdefault("removed_activity_ids", []), target)

    if a1 or a2:
        try:
            store_save(auth.uid, session_id, doc)
        except SessionSaveError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to save customization: {exc}")

    return {
        "session_id": session_id,
        "plan_id": plan_id,
        "activity_id": activity_id,
        "removed": True,
        "saved_for_later": True,
    }


# ── Swap activity (Beta 2.1 Step 2D) — bank alternatives, LLM-free ──────────

@app.get(
    "/api/v1/session/{session_id}/plan/{plan_id}/activity/{activity_id}/swap-suggestions",
    tags=["session"],
)
async def session_activity_swap_suggestions(
    session_id: str,
    plan_id: str,
    activity_id: str,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """Return 1–3 safe replacement suggestions for an activity, from the existing
    activity bank (no OpenAI). Current plan only."""
    doc = _require_session(auth.uid, session_id)
    _require_customizable_activity(doc, plan_id, activity_id)
    return {
        "session_id": session_id,
        "plan_id": plan_id,
        "activity_id": activity_id,
        "suggestions": swap_suggestions(doc, plan_id, activity_id),
    }


@app.post(
    "/api/v1/session/{session_id}/plan/{plan_id}/activity/{activity_id}/swap",
    tags=["session"],
)
async def session_activity_swap(
    session_id: str,
    plan_id: str,
    activity_id: str,
    body: SwapRequest,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """Replace an activity with a chosen bank suggestion (overlay-only, LLM-free).

    Stores an activity_overrides entry with the replacement card (new stable id)
    and its replacement_internal. Idempotent for the same suggestion; a different
    suggestion replaces the prior override for that activity.
    """
    doc = _require_session(auth.uid, session_id)
    target = _require_customizable_activity(doc, plan_id, activity_id)

    match = find_bank_activity_by_suggestion_id(doc.get("brain_state") or {}, body.suggestion_id)
    if match is None:
        raise HTTPException(status_code=404, detail="suggestion_not_found")
    domain, bank_activity = match

    card, internal = build_card_from_bank(
        session_id, plan_id, key=target, domain=domain,
        bank_activity=bank_activity, source_bank_type="swap",
    )

    overlay = ensure_overlay(doc, plan_id)
    overrides = overlay.setdefault("activity_overrides", {})
    prev = overrides.get(target) or {}
    prev_repl_id = (prev.get("replacement_activity") or {}).get("id")

    if prev_repl_id != card["id"]:
        overrides[target] = {
            "mode": "swapped",
            "replacement_activity": card,
            "replacement_internal": internal,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reason": "parent_request",
        }
        try:
            store_save(auth.uid, session_id, doc)
        except SessionSaveError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to save swap: {exc}")
        replacement_id = card["id"]
    else:
        replacement_id = prev_repl_id  # idempotent — same suggestion already applied

    return {
        "session_id": session_id,
        "plan_id": plan_id,
        "activity_id": activity_id,
        "swapped": True,
        "replacement_activity_id": replacement_id,
        "plan_customization_summary": overlay_summary(overlay, plan_id),
    }


# ── Add recommended activity (Beta 2.1 Step 2D) — bank add-ons, LLM-free ────

@app.get(
    "/api/v1/session/{session_id}/plan/{plan_id}/activity-suggestions",
    tags=["session"],
)
async def session_activity_add_suggestions(
    session_id: str,
    plan_id: str,
    auth: Annotated[AuthUser, Depends(require_auth)],
    domain: Optional[str] = None,
):
    """Return 1–5 add-on suggestions from the existing bank (optionally filtered by
    domain), excluding activities already in the resolved plan. Current plan only."""
    doc = _require_session(auth.uid, session_id)
    _require_current_plan(doc, plan_id)
    return {
        "session_id": session_id,
        "plan_id": plan_id,
        "domain": domain,
        "suggestions": add_suggestions(doc, plan_id, domain_filter=domain),
    }


@app.post(
    "/api/v1/session/{session_id}/plan/{plan_id}/activities/add",
    tags=["session"],
)
async def session_activity_add(
    session_id: str,
    plan_id: str,
    body: AddActivityRequest,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """Add a recommended bank activity to a day of the current week (overlay-only,
    LLM-free). Day-specific and day-stable:

      - The canonical day is resolved FIRST (validate an explicit day → 400
        invalid_day on no match; auto-pick only when omitted/blank).
      - The added card id is deterministic on session+plan+suggestion+canonical_day,
        so the SAME suggestion on DIFFERENT days are distinct entries (no cross-day
        collision), and the SAME suggestion on the SAME day is idempotent.
      - Adding a new activity never relocates existing added activities; each renders
        on its own stored day.
    """
    doc = _require_session(auth.uid, session_id)
    _require_current_plan(doc, plan_id)

    match = find_bank_activity_by_suggestion_id(doc.get("brain_state") or {}, body.suggestion_id)
    if match is None:
        raise HTTPException(status_code=404, detail="suggestion_not_found")
    domain, bank_activity = match

    # ── Resolve the canonical day FIRST (before building the id) ──────────────
    requested = (body.day or "").strip()
    if requested:
        day = match_plan_day(requested, plan_day_labels(doc, plan_id))
        if day is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_day",
                    "message": "The selected day is not part of the current plan.",
                    "valid_days": plan_day_labels(doc, plan_id),
                },
            )
    else:
        day = choose_add_day(doc, plan_id)

    # ── Day-specific deterministic id → no cross-day collisions ───────────────
    card, internal = build_card_from_bank(
        session_id, plan_id, key=f"add:{body.suggestion_id}:{day}", domain=domain,
        bank_activity=bank_activity, source_bank_type="parent_added",
    )

    overlay = ensure_overlay(doc, plan_id)
    added = overlay.setdefault("added_activities", [])
    existing = next((it for it in added if (it.get("activity") or {}).get("id") == card["id"]), None)

    if existing is None:
        added.append({
            "activity": card,
            "internal": internal,
            "day": day,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        try:
            store_save(auth.uid, session_id, doc)
        except SessionSaveError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to save added activity: {exc}")
    # else: same suggestion + same day already added → idempotent, no change.

    return {
        "session_id": session_id,
        "plan_id": plan_id,
        "added": True,
        "activity_id": card["id"],
        "day": day,
        "plan_customization_summary": overlay_summary(overlay, plan_id),
    }


# ── Feedback helpers ───────────────────────────────────────────────────────

def _find_activity_internal(
    doc: Dict[str, Any],
    plan_id: str,
    activity_id: str,
    day: str,
) -> Optional[Dict[str, Any]]:
    """
    Look up a plan_internal activity record by (plan_id, activity_id, day).

    activity_id must be the UUID `id` from the plan_response card, which is
    stored as `frontend_id` in plan_internal. The day parameter is used to
    break ties when multiple slots share the same source activity_id.

    Returns the matching dict or None if not found.
    """
    plan_entry = (doc.get("plans") or {}).get(plan_id, {})
    plan_internal = plan_entry.get("plan_internal") or {}
    for day_entry in plan_internal.get("week", []):
        if day_entry.get("day") != day:
            continue
        for act in day_entry.get("activities", []):
            if act.get("frontend_id") == activity_id:
                return act
    return None


_INTERNAL_METADATA_FIELDS = (
    "domain", "subdomain", "milestone_age_months", "milestone_text",
    "bridge_step_index", "bridge_step_text", "activity_family", "theme",
    "difficulty_level", "source_bank_type", "weekend_mode", "support_tier",
)


# ── Feedback endpoint ──────────────────────────────────────────────────────

@app.post(
    "/api/v1/session/{session_id}/feedback",
    tags=["session"],
)
async def session_feedback(
    session_id: str,
    body: FeedbackRequest,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """
    Record one activity feedback entry and save it to the GCS session document.

    The feedback record is enriched with internal activity metadata from
    plan_internal when the activity_id (UUID from plan_response) is found.
    If no match is found, feedback is still saved with metadata_found=False.

    activities_done_today counts all feedback entries for the same activity_date
    where completion="did_it", including the record just saved.

    Raises:
      409 if no plan has been generated yet (no plan to give feedback on).
    """
    doc = _require_session(auth.uid, session_id)

    if not doc.get("current_plan_id"):
        raise HTTPException(
            status_code=409,
            detail="No plan has been generated for this session. Call /plan first.",
        )

    # Resolve internal metadata for enrichment + provenance. Beta 2.2 Slice 2e-1:
    # body.plan_id may be a PRIMARY plan_id (doc["plans"]) or a ready ADD-ON module_id
    # (doc["added_focus"][*].module_id). Detect which, then resolve from the right
    # source. Original cards resolve via plan_internal; swapped/added cards via the
    # overlay's internal block — for primary AND add-on alike.
    addon_entry = next(
        (e for e in (doc.get("added_focus") or {}).values()
         if e.get("module_id") == body.plan_id),
        None,
    )

    if addon_entry is not None:
        source = "addon"
        addon_doc, _module_id = _addon_overlay_doc(addon_entry)
        internal_act = _find_activity_internal(addon_doc, _module_id, body.activity_id, body.day)
        if internal_act is None:
            internal_act = find_overlay_internal(get_overlay(addon_doc, _module_id), body.activity_id)
        plan_period = addon_entry.get("plan_period") or {}
        module_id_val: Optional[str] = addon_entry.get("module_id")
        focus_key_fallback = addon_entry.get("focus_key", "")
        focus_label_fallback = addon_entry.get("focus_label", "")
        orig_key = resolve_customization_target(addon_doc, _module_id, body.activity_id)
    else:
        source = "primary"
        internal_act = _find_activity_internal(doc, body.plan_id, body.activity_id, body.day)
        if internal_act is None:
            internal_act = find_overlay_internal(get_overlay(doc, body.plan_id), body.activity_id)
        plan_period = ((doc.get("plans") or {}).get(body.plan_id) or {}).get("plan_period") or {}
        module_id_val = None
        focus_key_fallback = ""
        focus_label_fallback = ""
        orig_key = (
            resolve_customization_target(doc, body.plan_id, body.activity_id)
            if body.plan_id in (doc.get("plans") or {}) else None
        )

    metadata_found = internal_act is not None

    feedback_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()

    # Build the feedback record
    record: Dict[str, Any] = {
        "feedback_id":           feedback_id,
        "created_at":            now_iso,
        "plan_id":               body.plan_id,
        "activity_id":           body.activity_id,
        "day":                   body.day,
        "activity_date":         body.activity_date,
        "enjoyment":             body.enjoyment,
        "difficulty":            body.difficulty,
        "completion":            body.completion,
        "discuss_with_care_team": body.discuss_with_care_team,
        "care_team_member":      body.care_team_member,   # legacy single-select (kept)
        # Beta 2.0 provider tags for report visibility. None for old clients.
        "care_team_tags":        body.care_team_tags,
        "note":                  body.note,
        "metadata_found":        metadata_found,
    }

    # Enrich with internal metadata fields if found
    if metadata_found and internal_act:
        for field in _INTERNAL_METADATA_FIELDS:
            record[field] = internal_act.get(field)

    # ── Beta 2.2 Slice 2e-1: additive provenance (primary + add-on) ──────────
    domain = record.get("domain") or ""
    record["source"] = source
    record["focus_key"] = domain or focus_key_fallback
    record["focus_label"] = FOCUS_LABELS.get(domain, "") or focus_label_fallback
    record["domain_label"] = DOMAIN_LABELS.get(domain, "") if domain else ""
    record["module_id"] = module_id_val
    record["original_activity_id"] = (
        orig_key if (orig_key and orig_key != body.activity_id) else None
    )
    record["plan_period_id"] = plan_period.get("plan_id", "")
    record["cycle_week"] = int(plan_period.get("cycle_week", 1) or 1)

    # Append to feedback list and save
    doc.setdefault("feedback", []).append(record)

    try:
        store_save(auth.uid, session_id, doc)
    except SessionSaveError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save feedback: {exc}")

    # Count activities completed today (including the one just saved)
    activities_done_today = sum(
        1 for f in doc["feedback"]
        if f.get("activity_date") == body.activity_date
        and f.get("completion") == "did_it"
    )

    return {
        "ok": True,
        "feedback_id": feedback_id,
        "activities_done_today": activities_done_today,
        "flagged_for_care_team": body.discuss_with_care_team,
        "metadata_found": metadata_found,
    }


# ── Report endpoint ────────────────────────────────────────────────────────

@app.post(
    "/api/v1/session/{session_id}/report",
    tags=["session"],
)
async def session_report(
    session_id: str,
    body: ReportRequest,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """
    Generate a plain-text care team report.

    Phase 1 reports are template-based (no LLM calls). All four report types
    use the same core summary with different title and opening framing.

    Privacy: "your child" is used throughout. The child's real name is never
    in the session document, so it cannot appear in the report. brain_state,
    plan_internal, gate_report, and debug fields are never included.

    Raises:
      409 if no plan has been generated yet.
    """
    doc = _require_session(auth.uid, session_id)

    if not doc.get("current_plan_id"):
        raise HTTPException(
            status_code=409,
            detail="No plan has been generated for this session. Call /plan first.",
        )

    try:
        body_text = generate_report_body(
            session_doc=doc,
            report_type=body.report_type,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}")

    return {
        "session_id": session_id,
        "report_type": body.report_type,
        "title": REPORT_TITLES.get(body.report_type, "Care Team Report"),
        "body": body_text,
    }


# ── GET session endpoint ───────────────────────────────────────────────────

def _build_session_view(doc: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    """Build the frontend-safe session payload (shared by GET /session/{id} and
    GET /session/current). Read-only — never mutates the doc or starts work.

    Never returns: child name, brain_state, plan_internal, or debug fields
    (gate_report only when ADMIN_DEBUG=1).
    """
    status = doc.get("status", "questions")

    # ── Interview in progress ─────────────────────────────────────────────
    if status in ("questions", "interview_complete"):
        interview = doc.get("interview") or {}
        current_q = get_current_question(interview) if status == "questions" else None
        return {
            "session_id": session_id,
            "status": status,
            "current_question": current_q,
            # Additive: True while a primary /plan generation is in flight, so the
            # frontend can poll this endpoint until status flips to "plan_ready".
            "plan_generating": bool(doc.get("plan_generation_started_at")),
            # Beta 2.2 focus metadata; added/remaining recomputed from added_focus.
            "focus": focus_view(doc.get("focus") or {}, doc.get("added_focus") or {}),
        }

    # ── Plan ready ────────────────────────────────────────────────────────
    current_plan_id = doc.get("current_plan_id")
    plans = doc.get("plans") or {}
    plan_entry = plans.get(current_plan_id, {}) if current_plan_id else {}

    original_plan_response = plan_entry.get("plan_response") or {}
    # Beta 2.1: resolve the current plan through its customization overlay (if any).
    # Identity-safe; the stored plan_response is never mutated.
    plan_response = resolve_plan_response(
        original_plan_response, get_overlay(doc, current_plan_id)
    )
    progress_summary = plan_response.get("progress_summary") or {}

    feedback_list: List[Dict[str, Any]] = doc.get("feedback") or []
    feedback_summary = {
        "total":               len(feedback_list),
        "completed":           sum(1 for f in feedback_list if f.get("completion") == "did_it"),
        "flagged_for_care_team": sum(1 for f in feedback_list if f.get("discuss_with_care_team")),
        "domains_practised":   list({f.get("domain", "") for f in feedback_list if f.get("domain")}),
    }

    accepted_at = plan_entry.get("accepted_at")
    plan_acceptance = {
        "plan_id":     current_plan_id,
        "accepted":    bool(accepted_at),
        "accepted_at": accepted_at,
    }

    response: Dict[str, Any] = {
        "session_id":        session_id,
        "status":            status,
        "age_in_months":     doc.get("age_in_months"),
        "daily_time_minutes": doc.get("daily_time_minutes"),
        "current_plan_id":   current_plan_id,
        "plan":              plan_response,
        "progress_summary":  progress_summary,
        "feedback_summary":  feedback_summary,
        "plan_acceptance":   plan_acceptance,
        "plan_customization_summary": overlay_summary(
            get_overlay(doc, current_plan_id), current_plan_id
        ),
        # Beta 2.2 focus metadata; added/remaining recomputed from added_focus.
        "focus": focus_view(doc.get("focus") or {}, doc.get("added_focus") or {}),
    }

    # Beta 2.2: additive read-only balanced current-week view (primary + ready add-ons,
    # budget-balanced today→Sunday). Stored primary plan + add-on modules are untouched.
    plan_period = plan_response.get("plan_period") or {}
    is_integrated = (
        bool(plan_period.get("is_integrated"))
        or int(plan_period.get("cycle_week", 1) or 1) >= 2
    )
    if is_integrated:
        # The integrated next-week plan already contains every active focus (primary +
        # added), balanced across the week. Re-merging the current-week add-on modules
        # would double-count them, so the balanced view IS the integrated plan as-is.
        current_week_plan = plan_response
    else:
        addon_modules = [
            {
                "focus_key": fk,
                "focus_label": (e or {}).get("focus_label", ""),
                "module_id": (e or {}).get("module_id", ""),
                "plan_response": resolve_plan_response(
                    (e or {}).get("plan_response") or {}, (e or {}).get("customizations")
                ),
            }
            for fk, e in (doc.get("added_focus") or {}).items()
            if (e or {}).get("status") == "ready"
        ]
        today_iso = _local_date(
            doc.get("timezone") or "UTC", datetime.now(timezone.utc)
        ).isoformat()
        current_week_plan = build_balanced_current_week(
            session_id=session_id,
            primary_plan_response=plan_response,
            primary_plan_id=current_plan_id,
            primary_focus_key=(doc.get("focus") or {}).get("primary_focus_key", ""),
            addon_modules=addon_modules,
            today_iso=today_iso,
            daily_time_minutes=doc.get("daily_time_minutes"),
            age_in_months=doc.get("age_in_months"),
        )
    response["current_week_plan"] = current_week_plan

    # Compatibility shim (Beta 2.2 freeze): the legacy `plan` field MIRRORS the balanced
    # display plan, so clients that render `plan` show the correct current-week result
    # (primary + ready add-ons, budget-balanced) even when they don't read
    # current_week_plan. This is response-only — the stored primary plan
    # (doc["plans"]), add-on modules (doc["added_focus"]), and overlays are never
    # mutated, and current_week_plan is still returned. Cards keep routing provenance
    # (source "primary"|"addon" + plan_id|module_id) so customization routes by card.
    # No add-ons (or an integrated week) → this is effectively the primary plan.
    response["plan"] = current_week_plan

    if _ADMIN_DEBUG:
        brain_state = doc.get("brain_state") or {}
        gate_report = brain_state.get("_gate_report")
        if gate_report:
            response["_gate_report"] = gate_report

    return response


# NOTE: /session/current MUST be declared before /session/{session_id} so FastAPI
# does not treat "current" as a session_id path parameter.
@app.get(
    "/api/v1/session/current",
    tags=["session"],
)
async def session_current(
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """
    Resume the authenticated user's latest session (after sign-out/in, localStorage
    loss, or a new device) — without knowing the session_id.

    Read-only: finds the latest session owned by auth.uid (by created_at) and
    returns the SAME payload as GET /session/{session_id}. Never creates, mutates,
    or starts plan generation. 404 (no_existing_session) if the user has none.
    """
    try:
        result = store_find_latest_for_uid(auth.uid)
    except SessionLoadError as exc:
        raise HTTPException(status_code=500, detail=f"Session storage error: {exc}")

    if result is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "no_existing_session",
                "message": "No existing session found for this user.",
            },
        )

    session_id, doc = result
    return _build_session_view(doc, session_id)


@app.get(
    "/api/v1/session/{session_id}",
    tags=["session"],
)
async def session_get(
    session_id: str,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """
    Reload a saved session in frontend-safe form (same payload as /session/current).
    """
    doc = _require_session(auth.uid, session_id)
    return _build_session_view(doc, session_id)


@app.get(
    "/api/v1/session/{session_id}/focus-areas",
    tags=["session"],
)
async def session_focus_areas(
    session_id: str,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """
    List the parent's focus areas (Beta 2.2): the primary, any added focus modules,
    and the remaining/addable areas. Read-only — never creates, mutates, or generates.

    remaining = all 4 focus areas − primary − any focus already occupied (status
    interviewing/generating/ready). `recommended` comes from the original concern
    detection (Slice 1). Returns the parent-friendly labels.
    """
    doc = _require_session(auth.uid, session_id)
    return _focus_areas_payload(session_id, doc)


def _focus_areas_payload(session_id: str, doc: Dict[str, Any]) -> Dict[str, Any]:
    """Build the primary / added / remaining focus-areas payload from the session doc.
    Shared by GET /focus-areas and the cancel endpoint so both return an identical
    shape (the frontend can refresh the picker from either)."""
    fb = doc.get("focus") or {}
    view = focus_view(fb, doc.get("added_focus") or {})
    return {
        "session_id": session_id,
        "primary": {
            "focus_key": fb.get("primary_focus_key", ""),
            "label": fb.get("primary_focus_label", ""),
        },
        "added": view["added_focus_areas"],   # [{focus_key, label, status}]
        "remaining": [
            {"focus_key": r["key"], "label": r["label"], "recommended": r["recommended"]}
            for r in view["remaining_focus_areas"]
        ],
    }


# ── Beta 2.2 Slice 2b: add-on focused intake ────────────────────────────────

def _addon_intake_view(session_id: str, focus_key: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Frontend-safe view of one add-on intake module (never returns brain_state)."""
    status = entry.get("status", "")
    complete = status == "interview_complete"
    return {
        "session_id": session_id,
        "focus_key": focus_key,
        "focus_label": entry.get("focus_label", FOCUS_LABELS.get(focus_key, focus_key)),
        "module_id": entry.get("module_id", ""),
        "status": status,
        "total_questions_estimate": (entry.get("interview") or {}).get(
            "total_questions_estimate", 0
        ),
        "current_question": (
            None if complete else get_current_question(entry.get("interview") or {})
        ),
        "ready_for_generate": complete,
    }


@app.post(
    "/api/v1/session/{session_id}/focus/{focus_key}/start",
    tags=["session"],
)
async def session_focus_start(
    session_id: str,
    focus_key: str,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """
    Start a focused add-on intake for one of the 3 non-primary focus areas (Beta 2.2).

    This is a full mini-onboarding for the chosen domain: it asks that domain's normal
    focused questions (≤7), because the app has not evaluated that area yet. Intake
    state is stored separately under doc["added_focus"][focus_key] — the primary plan,
    primary interview, and doc["plans"] are never touched. No activities are generated
    here and no LLM is called.

    Guards:
      404 unknown_focus      — focus_key is not one of the 4 supported areas
      409 focus_is_primary   — focus_key is the parent's primary focus
      409 focus_already_added— focus is already generating/ready
    Idempotent: if the focus is already interviewing (or intake complete), returns the
    CURRENT intake state without restarting.
    """
    doc = _require_session(auth.uid, session_id)

    if focus_key not in FOCUS_LABELS:
        raise HTTPException(status_code=404, detail="unknown_focus")

    fb = doc.get("focus") or {}
    if focus_key == fb.get("primary_focus_key"):
        raise HTTPException(status_code=409, detail="focus_is_primary")

    added = doc.get("added_focus") or {}
    existing = added.get(focus_key)
    if existing:
        status = existing.get("status")
        if status in ("generating", "ready"):
            raise HTTPException(status_code=409, detail="focus_already_added")
        if status in ("interviewing", "interview_complete"):
            # Idempotent — return the in-progress intake, do NOT restart.
            return _addon_intake_view(session_id, focus_key, existing)
        # status == "error" (or anything else) → fall through and start fresh.

    try:
        addon_brain, addon_interview = run_focus_intake_start(
            doc.get("brain_state") or {}, focus_key
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Focus intake error: {exc}")

    current_q = get_current_question(addon_interview)
    if current_q is None:
        raise HTTPException(
            status_code=500,
            detail="No questions generated for this focus. Please try again.",
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    entry = {
        "focus_key": focus_key,
        "focus_label": FOCUS_LABELS[focus_key],
        "status": "interviewing",
        "module_id": str(uuid.uuid4()),
        "brain_state": addon_brain,       # separate add-on brain_state (never the primary)
        "interview": addon_interview,     # separate add-on interview state
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    doc.setdefault("added_focus", {})[focus_key] = entry

    try:
        store_save(auth.uid, session_id, doc)
    except SessionSaveError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save session: {exc}")

    return _addon_intake_view(session_id, focus_key, entry)


@app.post(
    "/api/v1/session/{session_id}/focus/{focus_key}/answer",
    tags=["session"],
)
async def session_focus_answer(
    session_id: str,
    focus_key: str,
    body: AnswerRequest,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """
    Record one answer for an add-on focus intake (Beta 2.2 Slice 2b).

    Uses the SAME domain-question / band / adaptive-stop logic as primary onboarding,
    scoped to this focus's separate interview state only. Writes only under
    doc["added_focus"][focus_key]; the primary interview, primary plan, and
    doc["plans"] are never touched. No activities generated, no LLM call.

    Returns either status "interviewing" + current_question, or status
    "interview_complete" + ready_for_generate=true.

    Guards:
      404 unknown_focus          — focus_key is not one of the 4 supported areas
      404 focus_not_started      — no add-on intake exists for this focus
      409 focus_not_interviewing — intake is complete/generating/ready/errored
      422 — question_id does not match the expected next question
    """
    doc = _require_session(auth.uid, session_id)

    if focus_key not in FOCUS_LABELS:
        raise HTTPException(status_code=404, detail="unknown_focus")

    entry = (doc.get("added_focus") or {}).get(focus_key)
    if not entry:
        raise HTTPException(status_code=404, detail="focus_not_started")
    if entry.get("status") != "interviewing":
        raise HTTPException(status_code=409, detail="focus_not_interviewing")

    addon_interview = entry["interview"]
    addon_brain = entry["brain_state"]

    expected_qid = get_expected_question_id(addon_interview)
    if expected_qid is None:
        raise HTTPException(status_code=409, detail="focus_not_interviewing")
    if body.question_id != expected_qid:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unexpected question_id {body.question_id!r}. "
                f"Expected {expected_qid!r}. Answers must be submitted in order."
            ),
        )

    try:
        addon_brain, addon_interview, interview_complete = run_record_answer(
            brain_state=addon_brain,
            interview=addon_interview,
            question_id=body.question_id,
            norm_answer=body.answer,  # already validated by Pydantic Literal
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Answer recording error: {exc}")

    entry["brain_state"] = addon_brain
    entry["interview"] = addon_interview
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    if interview_complete:
        entry["status"] = "interview_complete"

    try:
        store_save(auth.uid, session_id, doc)
    except SessionSaveError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save session: {exc}")

    if interview_complete:
        return {
            "session_id": session_id,
            "focus_key": focus_key,
            "status": "interview_complete",
            "ready_for_generate": True,
            "questions_answered": addon_interview["questions_answered_total"],
        }

    next_q = get_current_question(addon_interview)
    if next_q is None:
        raise HTTPException(status_code=500, detail="No next question available.")

    return {
        "session_id": session_id,
        "focus_key": focus_key,
        "status": "interviewing",
        "current_question": next_q,
    }


# ── Beta 2.2 Slice 2d: add-on hardening (stale recovery + size trim) ─────────

# A "generating" add-on is considered stale (crashed / timed-out / interrupted) once
# this many seconds have elapsed since generation_started_at. Chosen > Cloud Run's
# 600s request timeout so an in-flight synchronous /generate is never mistaken for
# stale; after this window a parent can safely re-call /generate to recover.
ADDON_GENERATION_STALE_SECONDS = 900  # 15 minutes


def _parse_iso(ts: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp; return None (never raise) on bad/empty input."""
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _is_generation_stale(entry: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """True if a 'generating' add-on has exceeded the stale threshold (recoverable).

    Uses generation_started_at (set when generation began), falling back to
    updated_at. A missing/unparseable timestamp is treated as stale so a parent is
    never permanently blocked.
    """
    now = now or datetime.now(timezone.utc)
    started = _parse_iso(entry.get("generation_started_at") or entry.get("updated_at") or "")
    if started is None:
        return True
    return (now - started).total_seconds() > ADDON_GENERATION_STALE_SECONDS


# Bulky, fully-regenerable add-on brain_state keys dropped after a module is ready.
# Each is rebuilt by run_plan_pipeline from the retained lean context and/or is already
# captured in plan_response / plan_internal.
#
# Slice 2f-2 (Option A): activity_banks is RETAINED so add-on swap can draw safe
# replacements from the already-generated, already-safety-filtered bank — LLM-free,
# exactly like primary swap. The scheduling artifacts below are still dropped. Net
# stored brain_state ≈ 59 KB (vs ~7 KB at 2d, ~137 KB raw) — still well below raw and
# regeneration-capable for Slice 2e.
ADDON_BULKY_BRAIN_KEYS = (
    "weekly_schedule",         # scheduled week — captured in plan_response
    "week1_schedule",          # duplicate of weekly_schedule
    "bridge_plans",            # bridge steps — rebuilt from dev_age/scoring
    "weekly_slot_allocation",  # scheduling intermediate
    "_gate_report",            # admin-debug only
)


def _trim_ready_addon(entry: Dict[str, Any], focus_key: str) -> None:
    """Slice 2d size optimization — after a module is READY, slim the stored add-on to
    a lean, regeneration-capable context. Mutates `entry` in place.

    Strategy: KEEP brain_state but drop only the bulky, regenerable keys
    (ADDON_BULKY_BRAIN_KEYS). The retained brain_state (child, qna, dev_age,
    concern_profile, safety_profile, selected_domain_keys, family_guidance_floor,
    activity_banks) powers both Slice-2e regeneration and Slice-2f-2 LLM-free swap.
    The interview's heavy per-band question dicts (interview.band_state) are replaced
    with a lean summary.

    Preserved: brain_state (incl. activity_banks for swap), plan_response,
    plan_internal, dev_age_summary, plan_period, focus_key, focus_label, module_id,
    source, status, generated_at, created_at, updated_at, lean interview summary.
    Removed: weekly_schedule, week1_schedule, bridge_plans, weekly_slot_allocation,
    _gate_report; interview.band_state.
    """
    bs = entry.get("brain_state")
    if isinstance(bs, dict):
        for k in ADDON_BULKY_BRAIN_KEYS:
            bs.pop(k, None)
    iv = entry.get("interview") or {}
    entry["interview"] = {
        "status": "complete",
        "domain_keys": iv.get("domain_keys") or [focus_key],
        "questions_answered_total": iv.get("questions_answered_total", 0),
        "total_questions_estimate": iv.get("total_questions_estimate", 0),
    }


# ── Beta 2.2 Slice 2c: current-week date-aware add-on generation ─────────────

def _addon_module_view(session_id: str, focus_key: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Frontend-safe view of one add-on focus module across all states.

    Used by GET /focus/{focus_key} (poll/resume) and as the success payload of
    /generate. NEVER returns brain_state, the raw interview, or plan_internal. The
    full add-on plan (plan_response, with provenance) is returned ONLY here — not in
    the /session views, which carry just {focus_key, label, status}.
    """
    status = entry.get("status", "")
    view: Dict[str, Any] = {
        "session_id": session_id,
        "focus_key": focus_key,
        "focus_label": entry.get("focus_label", FOCUS_LABELS.get(focus_key, focus_key)),
        "module_id": entry.get("module_id", ""),
        "source": "addon",
        "status": status,
    }
    if status == "interviewing":
        view["current_question"] = get_current_question(entry.get("interview") or {})
        view["total_questions_estimate"] = (entry.get("interview") or {}).get(
            "total_questions_estimate", 0
        )
        view["ready_for_generate"] = False
    elif status == "interview_complete":
        view["current_question"] = None
        view["total_questions_estimate"] = (entry.get("interview") or {}).get(
            "total_questions_estimate", 0
        )
        view["ready_for_generate"] = True
    elif status == "generating":
        view["ready_for_generate"] = False
    elif status == "ready":
        # Resolve the stored plan_response through the add-on customization overlay
        # (Slice 2f-1). Identity-safe: an uncustomized module returns the stored
        # plan_response unchanged. The stored plan_response is never mutated.
        overlay = entry.get("customizations")
        view["plan_period"] = entry.get("plan_period") or {}
        view["plan"] = resolve_plan_response(entry.get("plan_response") or {}, overlay)
        view["dev_age_summary"] = entry.get("dev_age_summary") or {}
        view["generated_at"] = entry.get("generated_at", "")
        view["plan_customization_summary"] = overlay_summary(overlay, entry.get("module_id"))
        # Genex Brain enrichment signals (additive): on-track ⇒ age-appropriate practice.
        view["on_track"] = bool(entry.get("on_track"))
        view["enrichment_mode"] = bool(entry.get("enrichment_mode"))
        if entry.get("enrichment_message"):
            view["message"] = entry["enrichment_message"]
        view["ready_for_generate"] = False
    elif status == "error":
        view["error"] = entry.get("error", "")
        view["ready_for_generate"] = True  # retryable — re-call /generate
    return view


@app.post(
    "/api/v1/session/{session_id}/focus/{focus_key}/generate",
    tags=["session"],
)
async def session_focus_generate(
    session_id: str,
    focus_key: str,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """
    Generate the date-aware add-on activity module for a completed focus intake.

    Builds a single-domain plan for `focus_key` from that focus's separate, completed
    add-on brain_state, anchored to the CURRENT week and exposing today→Sunday only.
    The module is stored under doc["added_focus"][focus_key] (status "ready") and is
    the ONLY place this writes — doc["plans"], current_plan_id, and the primary
    plan_response/plan_internal are never touched. Every add-on card carries additive
    provenance (source="addon", focus_key, focus_label, module_id, plan_period_id).

    Idempotency / state (Slice 2d hardened):
      ready                       → returns the cached module, no regeneration.
      interviewing                → 409 focus_intake_not_complete.
      generating (fresh)          → 409 focus_already_generating (no duplicate work).
      generating (stale > 15 min) → recovers: re-generates safely.
      interview_complete | error  → generates (error is retryable).

    Guards: 404 unknown_focus / focus_not_started · 409 focus_is_primary.
    On generation failure the module is marked status="error" (retry by re-calling).
    """
    doc = _require_session(auth.uid, session_id)

    if focus_key not in FOCUS_LABELS:
        raise HTTPException(status_code=404, detail="unknown_focus")

    fb = doc.get("focus") or {}
    if focus_key == fb.get("primary_focus_key"):
        raise HTTPException(status_code=409, detail="focus_is_primary")

    entry = (doc.get("added_focus") or {}).get(focus_key)
    if not entry:
        raise HTTPException(status_code=404, detail="focus_not_started")

    status = entry.get("status")
    if status == "ready":
        return _addon_module_view(session_id, focus_key, entry)  # cached, idempotent
    if status == "interviewing":
        raise HTTPException(status_code=409, detail="focus_intake_not_complete")
    if status == "generating" and not _is_generation_stale(entry):
        # A generation is genuinely in flight — do not duplicate work.
        raise HTTPException(status_code=409, detail="focus_already_generating")
    # Proceed for: interview_complete, error (retry), or a STALE generating (recover).

    # Mark generating + persist first, so concurrent polls/calls see it and do not
    # duplicate generation. generation_started_at anchors stale detection.
    now_start = datetime.now(timezone.utc).isoformat()
    entry["status"] = "generating"
    entry["generation_started_at"] = now_start
    entry["updated_at"] = now_start
    entry.pop("error", None)
    entry.pop("error_at", None)
    try:
        store_save(auth.uid, session_id, doc)
    except SessionSaveError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save session: {exc}")

    try:
        addon_brain = entry["brain_state"]
        timezone_str = doc.get("timezone") or "UTC"
        plan_period = compute_plan_period(timezone_str)  # today→Sunday, current week

        # enrichment_focus: if this added focus is on-track (no gap), still produce
        # age-appropriate practice activities from its bank (Genex Brain rule).
        addon_brain, _ = run_plan_pipeline(
            brain_state=addon_brain, admin_debug=False, enrichment_focus=focus_key,
        )
        weekly_schedule = addon_brain.get("weekly_schedule", {})

        plan_response = adapt_weekly_plan(
            session_id=session_id,
            age_in_months=doc["age_in_months"],
            daily_time_minutes=doc["daily_time_minutes"],
            weekly_schedule=weekly_schedule,
            plan_period=plan_period,
        )
        apply_addon_provenance(
            plan_response,
            focus_key=focus_key,
            focus_label=entry.get("focus_label", FOCUS_LABELS[focus_key]),
            module_id=entry["module_id"],
            plan_period=plan_period,
        )
        plan_internal = build_plan_internal(
            session_id=session_id,
            brain_state=addon_brain,
            weekly_schedule=weekly_schedule,
            plan_period=plan_period,
            daily_time_minutes=doc["daily_time_minutes"],
        )
        dev_age = addon_brain.get("dev_age") or {}
        dev_age_months = dev_age.get(focus_key)
        chrono = (addon_brain.get("child") or {}).get("chronological_months")
        dev_age_summary = {
            "focus_key": focus_key,
            "dev_age_months": dev_age_months,
            "chronological_months": chrono,
            "by_domain": dev_age,
        }
        # On-track ⇒ the module is age-appropriate practice/enrichment (no gap).
        on_track = (
            dev_age_months is not None and chrono is not None
            and dev_age_months >= chrono
        )
        focus_label = entry.get("focus_label", FOCUS_LABELS[focus_key])
        enrichment_message = (
            f"Great news — based on your answers, {focus_label} looks on track right now. "
            "We'll still add age-appropriate activities so you can keep supporting this area at home."
        ) if on_track else ""
    except Exception as exc:
        entry["status"] = "error"
        entry["error"] = str(exc)
        entry["error_at"] = datetime.now(timezone.utc).isoformat()
        try:
            store_save(auth.uid, session_id, doc)
        except SessionSaveError:
            pass
        raise HTTPException(status_code=500, detail=f"Add-on generation failed: {exc}")

    now_iso = datetime.now(timezone.utc).isoformat()
    entry["brain_state"] = addon_brain
    entry["status"] = "ready"
    entry["source"] = "addon"
    entry["plan_period"] = plan_period
    entry["plan_response"] = plan_response
    entry["plan_internal"] = plan_internal
    entry["dev_age_summary"] = dev_age_summary
    entry["on_track"] = on_track
    entry["enrichment_mode"] = on_track   # on-track add-on ⇒ enrichment activities
    entry["enrichment_message"] = enrichment_message
    entry["generated_at"] = now_iso
    entry["updated_at"] = now_iso
    entry.pop("generation_started_at", None)
    # Slice 2d: slim to a lean, regeneration-capable context — drop only the bulky,
    # regenerable brain_state keys + question bands. Everything GET /focus, the cached
    # ready response, reports, and Slice-2e future-week generation need is retained.
    _trim_ready_addon(entry, focus_key)

    try:
        store_save(auth.uid, session_id, doc)
    except SessionSaveError as exc:
        raise HTTPException(status_code=500, detail=f"Module generated but failed to save: {exc}")

    return _addon_module_view(session_id, focus_key, entry)


@app.get(
    "/api/v1/session/{session_id}/focus/{focus_key}",
    tags=["session"],
)
async def session_focus_get(
    session_id: str,
    focus_key: str,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """
    Read one add-on focus module (poll / resume). Returns the current status and, when
    ready, the full date-aware add-on plan (with provenance). Read-only — never starts
    or duplicates generation. States: interviewing | interview_complete | generating |
    ready | error. Guards: 404 unknown_focus / focus_not_started.
    """
    doc = _require_session(auth.uid, session_id)
    if focus_key not in FOCUS_LABELS:
        raise HTTPException(status_code=404, detail="unknown_focus")
    entry = (doc.get("added_focus") or {}).get(focus_key)
    if not entry:
        raise HTTPException(status_code=404, detail="focus_not_started")
    return _addon_module_view(session_id, focus_key, entry)


@app.post(
    "/api/v1/session/{session_id}/focus/{focus_key}/cancel",
    tags=["session"],
)
async def session_focus_cancel(
    session_id: str,
    focus_key: str,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """
    Cancel/abandon an UNFINISHED add-on focus before generation, returning it to
    remaining_focus_areas (e.g. the parent closes the intake modal without finishing).

    Removes doc["added_focus"][focus_key] entirely, so the focus is no longer added/
    occupied and reappears in remaining. Never touches the primary plan, doc["plans"],
    or current_plan_id; never generates.

    Cancellable statuses: interviewing, interview_complete, error, and a STALE
    'generating' (> 15 min — an abandoned/crashed generation, same staleness rule as
    /generate recovery).

    Guards / non-cancellable:
      404 unknown_focus            — focus_key is not one of the 4 supported areas
      409 focus_is_primary         — the primary focus cannot be canceled
      409 focus_already_ready      — a generated module exists (removal is a later feature)
      409 focus_already_generating — a generation is genuinely in flight (not stale)
    Idempotent: if no add-on exists for this focus, returns 200 status="canceled"
    (closing a modal twice must not error).
    """
    doc = _require_session(auth.uid, session_id)

    if focus_key not in FOCUS_LABELS:
        raise HTTPException(status_code=404, detail="unknown_focus")

    fb = doc.get("focus") or {}
    if focus_key == fb.get("primary_focus_key"):
        raise HTTPException(status_code=409, detail="focus_is_primary")

    added = doc.get("added_focus") or {}
    entry = added.get(focus_key)

    if entry is not None:
        status = entry.get("status")
        if status == "ready":
            raise HTTPException(status_code=409, detail="focus_already_ready")
        if status == "generating" and not _is_generation_stale(entry):
            raise HTTPException(status_code=409, detail="focus_already_generating")
        # interviewing | interview_complete | error | stale-generating → cancel.
        added.pop(focus_key, None)
        try:
            store_save(auth.uid, session_id, doc)
        except SessionSaveError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to save session: {exc}")
    # else: already absent → idempotent success (no write needed).

    payload = _focus_areas_payload(session_id, doc)
    payload["focus_key"] = focus_key
    payload["status"] = "canceled"
    return payload


# ── Beta 2.2 Slice 2f-1: add-on activity customization (remove / save) ───────

def _require_ready_addon(
    doc: Dict[str, Any], focus_key: str, body: Optional[AddonActivityRequest]
) -> Dict[str, Any]:
    """Guard for add-on activity customization. Returns the ready add-on entry.

    404 unknown_focus      — focus_key is not one of the 4 supported areas
    404 focus_not_started  — no add-on exists for this focus
    409 focus_not_ready    — the add-on has not been generated yet
    409 stale_module       — body.module_id is given and != entry["module_id"]
    """
    if focus_key not in FOCUS_LABELS:
        raise HTTPException(status_code=404, detail="unknown_focus")
    entry = (doc.get("added_focus") or {}).get(focus_key)
    if not entry:
        raise HTTPException(status_code=404, detail="focus_not_started")
    if entry.get("status") != "ready":
        raise HTTPException(status_code=409, detail="focus_not_ready")
    if body is not None and body.module_id and body.module_id != entry.get("module_id"):
        raise HTTPException(status_code=409, detail="stale_module")
    return entry


def _addon_overlay_doc(entry: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """Build a synthetic single-plan (doc, plan_id) view of one ready add-on so the
    existing primary customization helpers (resolve_customization_target,
    swap_suggestions, …) can be reused UNCHANGED. plan_id == the add-on module_id.

    The overlay is the SAME object as entry["customizations"] (by reference) when it
    exists, so reads see live state; mutations go through _ensure_addon_overlay(entry).
    The brain_state carries the retained activity_banks (Slice 2f-2) used for swaps.
    """
    module_id = entry.get("module_id", "")
    return {
        "brain_state": entry.get("brain_state") or {},
        "plans": {module_id: {
            "plan_response": entry.get("plan_response") or {},
            "plan_internal": entry.get("plan_internal") or {},
        }},
        "plan_customizations": {module_id: entry.get("customizations") or empty_overlay()},
        "current_plan_id": module_id,
        # plan_period carries the add-on's timezone; choose_add_day uses it for "today".
        "timezone": (entry.get("plan_period") or {}).get("timezone") or "UTC",
    }, module_id


def _require_addon_target(entry: Dict[str, Any], activity_id: str) -> str:
    """Map a VISIBLE add-on activity id to its canonical overlay key (original id, or
    the source key of a swapped replacement). 404 activity_not_found if it is not a
    current actionable add-on activity. Mirrors the primary _require_customizable_activity.
    """
    addon_doc, module_id = _addon_overlay_doc(entry)
    target = resolve_customization_target(addon_doc, module_id, activity_id)
    if target is None:
        raise HTTPException(status_code=404, detail="activity_not_found")
    return target


def _stamp_addon_card(card: Dict[str, Any], entry: Dict[str, Any], activity_date: str = "") -> Dict[str, Any]:
    """Stamp add-on provenance onto a bank-built card so a swapped/added add-on card
    stays labeled like the rest of the module (source=addon, focus_key, …)."""
    pp = entry.get("plan_period") or {}
    card["source"] = "addon"
    card["focus_key"] = entry.get("focus_key", "")
    card["focus_label"] = entry.get("focus_label", "")
    card["module_id"] = entry.get("module_id", "")
    card["plan_period_id"] = pp.get("plan_id", "")
    card["week_start_date"] = pp.get("week_start_date", "")
    if activity_date:
        card["activity_date"] = activity_date
    return card


def _ensure_addon_overlay(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Return the (mutable) add-on customization overlay, creating an empty one if
    absent. Stored under doc["added_focus"][focus_key]["customizations"] — fully
    separate from the primary doc["plan_customizations"]."""
    cz = entry.get("customizations")
    if not isinstance(cz, dict):
        cz = empty_overlay()
        entry["customizations"] = cz
    else:
        for k, v in empty_overlay().items():
            cz.setdefault(k, v)
    return cz


def _addon_customize_response(
    session_id: str, focus_key: str, entry: Dict[str, Any],
    activity_id: str, target: str, overlay: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "session_id": session_id,
        "focus_key": focus_key,
        "module_id": entry.get("module_id", ""),
        "activity_id": activity_id,
        "removed": target in (overlay.get("removed_activity_ids") or []),
        "saved_for_later": target in (overlay.get("saved_for_later_activity_ids") or []),
        "plan_customization_summary": overlay_summary(overlay, entry.get("module_id")),
    }


@app.post(
    "/api/v1/session/{session_id}/focus/{focus_key}/activity/{activity_id}/remove",
    tags=["session"],
)
async def session_addon_activity_remove(
    session_id: str,
    focus_key: str,
    activity_id: str,
    auth: Annotated[AuthUser, Depends(require_auth)],
    body: Optional[AddonActivityRequest] = None,
):
    """
    Hide an activity from a READY add-on module's resolved plan (Beta 2.2 Slice 2f-1).

    Overlay-only and LLM-free: adds activity_id to the add-on overlay's
    removed_activity_ids under doc["added_focus"][focus_key]["customizations"]. Never
    mutates entry["plan_response"], doc["plans"], current_plan_id, or the primary
    plan_customizations. Idempotent (no duplicate ids). Only a ready add-on is editable.
    """
    doc = _require_session(auth.uid, session_id)
    entry = _require_ready_addon(doc, focus_key, body)
    target = _require_addon_target(entry, activity_id)

    overlay = _ensure_addon_overlay(entry)
    changed = _add_unique(overlay.setdefault("removed_activity_ids", []), target)

    if changed:
        try:
            store_save(auth.uid, session_id, doc)
        except SessionSaveError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to save customization: {exc}")

    return _addon_customize_response(session_id, focus_key, entry, activity_id, target, overlay)


@app.post(
    "/api/v1/session/{session_id}/focus/{focus_key}/activity/{activity_id}/save-for-later",
    tags=["session"],
)
async def session_addon_activity_save_for_later(
    session_id: str,
    focus_key: str,
    activity_id: str,
    auth: Annotated[AuthUser, Depends(require_auth)],
    body: Optional[AddonActivityRequest] = None,
):
    """
    Save a READY add-on activity for later ("I like this, but not this week").

    Adds activity_id to saved_for_later_activity_ids AND hides it from the current week
    via removed_activity_ids, in the add-on overlay. Overlay-only, LLM-free, idempotent.
    Never mutates entry["plan_response"], doc["plans"], or the primary customizations.
    """
    doc = _require_session(auth.uid, session_id)
    entry = _require_ready_addon(doc, focus_key, body)
    target = _require_addon_target(entry, activity_id)

    overlay = _ensure_addon_overlay(entry)
    a1 = _add_unique(overlay.setdefault("saved_for_later_activity_ids", []), target)
    a2 = _add_unique(overlay.setdefault("removed_activity_ids", []), target)

    if a1 or a2:
        try:
            store_save(auth.uid, session_id, doc)
        except SessionSaveError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to save customization: {exc}")

    return _addon_customize_response(session_id, focus_key, entry, activity_id, target, overlay)


# ── Beta 2.2 Slice 2f-2: add-on activity swap (LLM-free, bank-based) ─────────

@app.get(
    "/api/v1/session/{session_id}/focus/{focus_key}/activity/{activity_id}/swap-suggestions",
    tags=["session"],
)
async def session_addon_activity_swap_suggestions(
    session_id: str,
    focus_key: str,
    activity_id: str,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """Return safe replacement suggestions for a READY add-on activity, drawn from the
    add-on's retained per-focus activity bank (no OpenAI). Reuses the primary
    swap_suggestions logic via a synthetic single-plan view of the add-on module."""
    doc = _require_session(auth.uid, session_id)
    entry = _require_ready_addon(doc, focus_key, None)
    _require_addon_target(entry, activity_id)  # 404 if not a visible add-on activity
    addon_doc, module_id = _addon_overlay_doc(entry)
    return {
        "session_id": session_id,
        "focus_key": focus_key,
        "module_id": module_id,
        "activity_id": activity_id,
        "suggestions": swap_suggestions(addon_doc, module_id, activity_id),
    }


@app.post(
    "/api/v1/session/{session_id}/focus/{focus_key}/activity/{activity_id}/swap",
    tags=["session"],
)
async def session_addon_activity_swap(
    session_id: str,
    focus_key: str,
    activity_id: str,
    body: AddonSwapRequest,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """Replace a READY add-on activity with a chosen bank suggestion (overlay-only,
    LLM-free). Writes only to the add-on overlay's activity_overrides under
    doc["added_focus"][focus_key]["customizations"]; the stored plan_response,
    doc["plans"], current_plan_id, and the primary customizations are never touched.
    The replacement card keeps add-on provenance (source=addon, focus_key, module_id,
    activity_date). Idempotent for the same suggestion_id; a different suggestion
    replaces the prior override for that activity.
    """
    doc = _require_session(auth.uid, session_id)
    entry = _require_ready_addon(doc, focus_key, body)
    target = _require_addon_target(entry, activity_id)

    match = find_bank_activity_by_suggestion_id(entry.get("brain_state") or {}, body.suggestion_id)
    if match is None:
        raise HTTPException(status_code=404, detail="suggestion_not_found")
    domain, bank_activity = match

    module_id = entry.get("module_id", "")
    card, internal = build_card_from_bank(
        session_id, module_id, key=target, domain=domain,
        bank_activity=bank_activity, source_bank_type="swap",
    )
    # Preserve the day of the activity the parent is looking at, then stamp provenance.
    resolved = resolve_plan_response(entry.get("plan_response") or {}, entry.get("customizations"))
    visible = next(
        (c for d in resolved.get("week", []) for c in d.get("activities", [])
         if c.get("id") == activity_id), {}
    )
    _stamp_addon_card(card, entry, activity_date=visible.get("activity_date", ""))

    overlay = _ensure_addon_overlay(entry)
    overrides = overlay.setdefault("activity_overrides", {})
    prev = overrides.get(target) or {}
    prev_repl_id = (prev.get("replacement_activity") or {}).get("id")

    if prev_repl_id != card["id"]:
        overrides[target] = {
            "mode": "swapped",
            "replacement_activity": card,
            "replacement_internal": internal,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reason": "parent_request",
        }
        try:
            store_save(auth.uid, session_id, doc)
        except SessionSaveError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to save swap: {exc}")
        replacement_id = card["id"]
    else:
        replacement_id = prev_repl_id  # idempotent — same suggestion already applied

    return {
        "session_id": session_id,
        "focus_key": focus_key,
        "module_id": module_id,
        "activity_id": activity_id,
        "swapped": True,
        "replacement_activity_id": replacement_id,
        "plan_customization_summary": overlay_summary(overlay, module_id),
    }


# ── Beta 2.2 Slice 2f-3: add another activity to an add-on day ───────────────

def _addon_plan_period_days(entry: Dict[str, Any]) -> Dict[str, str]:
    """Map {day_name: activity_date} for the add-on's current plan_period (today→Sunday)."""
    pp = entry.get("plan_period") or {}
    week_start = pp.get("week_start_date", "")
    return {d: activity_date_for_day(week_start, d) for d in pp.get("days_included", [])}


@app.get(
    "/api/v1/session/{session_id}/focus/{focus_key}/activity-suggestions",
    tags=["session"],
)
async def session_addon_activity_add_suggestions(
    session_id: str,
    focus_key: str,
    auth: Annotated[AuthUser, Depends(require_auth)],
    domain: Optional[str] = None,
):
    """Return add-on bank activities not already in the resolved add-on module
    (optionally filtered by domain). Bank-only, no OpenAI. Ready add-on only."""
    doc = _require_session(auth.uid, session_id)
    entry = _require_ready_addon(doc, focus_key, None)
    addon_doc, module_id = _addon_overlay_doc(entry)
    return {
        "session_id": session_id,
        "focus_key": focus_key,
        "module_id": module_id,
        "domain": domain,
        "suggestions": add_suggestions(addon_doc, module_id, domain_filter=domain),
    }


@app.post(
    "/api/v1/session/{session_id}/focus/{focus_key}/activities/add",
    tags=["session"],
)
async def session_addon_activity_add(
    session_id: str,
    focus_key: str,
    body: AddonAddActivityRequest,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """Add a recommended bank activity to a day of a READY add-on module (overlay-only,
    LLM-free). Writes only to the add-on overlay's added_activities under
    doc["added_focus"][focus_key]["customizations"]; the stored plan_response,
    doc["plans"], current_plan_id, and the primary customizations are never touched.

    Day/date is constrained to the add-on plan_period (today→Sunday). Provide `day`
    or `activity_date`; both omitted → a day is auto-picked. The added card keeps
    add-on provenance. Day-specific deterministic id → same suggestion on the same day
    is idempotent; the same suggestion on a different day is a separate card.
    """
    doc = _require_session(auth.uid, session_id)
    entry = _require_ready_addon(doc, focus_key, body)

    match = find_bank_activity_by_suggestion_id(entry.get("brain_state") or {}, body.suggestion_id)
    if match is None:
        raise HTTPException(status_code=404, detail="suggestion_not_found")
    domain, bank_activity = match

    # ── Resolve the day within the add-on plan_period (today→Sunday) ──────────
    valid = _addon_plan_period_days(entry)  # {day_name: date}
    req_date = (body.activity_date or "").strip()
    req_day = (body.day or "").strip()
    if req_date:
        day = next((d for d, dt in valid.items() if dt == req_date), None)
        if day is None:
            raise HTTPException(status_code=400, detail={
                "code": "invalid_date",
                "message": "The selected date is not within this add-on's current week.",
                "valid_dates": list(valid.values())})
    elif req_day:
        day = match_plan_day(req_day, list(valid.keys()))
        if day is None:
            raise HTTPException(status_code=400, detail={
                "code": "invalid_day",
                "message": "The selected day is not within this add-on's current week.",
                "valid_days": list(valid.keys())})
    else:
        addon_doc, module_id_ = _addon_overlay_doc(entry)
        day = choose_add_day(addon_doc, module_id_)
        if day not in valid:
            day = next(iter(valid), "")
    activity_date = valid.get(day, "")

    module_id = entry.get("module_id", "")
    card, internal = build_card_from_bank(
        session_id, module_id, key=f"add:{body.suggestion_id}:{day}", domain=domain,
        bank_activity=bank_activity, source_bank_type="parent_added",
    )
    _stamp_addon_card(card, entry, activity_date=activity_date)

    overlay = _ensure_addon_overlay(entry)
    added = overlay.setdefault("added_activities", [])
    existing = next((it for it in added if (it.get("activity") or {}).get("id") == card["id"]), None)

    if existing is None:
        added.append({
            "activity": card,
            "internal": internal,
            "day": day,
            "date": activity_date,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        try:
            store_save(auth.uid, session_id, doc)
        except SessionSaveError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to save added activity: {exc}")
    # else: same suggestion + same day already added → idempotent, no change.

    return {
        "session_id": session_id,
        "focus_key": focus_key,
        "module_id": module_id,
        "added": True,
        "activity_id": card["id"],
        "day": day,
        "activity_date": activity_date,
        "plan_customization_summary": overlay_summary(overlay, module_id),
    }
