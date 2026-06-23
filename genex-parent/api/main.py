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
from typing import Annotated, Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from api.adapters import (
    adapt_weekly_plan,
    build_plan_internal,
    normalize_diagnosis_for_brain,
    sanitize_concern,
)
from api.auth import AuthUser, require_auth, verify_beta_code
from api.customization import (
    find_overlay_internal,
    get_overlay,
    resolve_plan_response,
)
from api.pipeline import (
    get_current_question,
    get_expected_question_id,
    run_plan_pipeline,
    run_record_answer,
    run_refresh_pipeline,
    run_session_start,
    translate_feedback_to_activity_feedback,
)
from api.planning_period import (
    _local_date,
    compute_next_week_period,
    compute_plan_period,
    next_week_available_from,
)
from api.report_generator import REPORT_TITLES, generate_report_body
from api.schemas import (
    AnswerRequest,
    FeedbackRequest,
    InterviewCompleteResponse,
    NextQuestionResponse,
    ReportRequest,
    SessionStartRequest,
    SessionStartResponse,
)
from api.session_store import (
    SessionLoadError,
    SessionSaveError,
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

def _require_session(uid: str, session_id: str) -> Dict[str, Any]:
    """
    Load a session document and enforce ownership.
    Used by every endpoint that operates on an existing session.

    Raises:
      404 if the session does not exist in memory/GCS.
      403 if the session exists but belongs to a different uid.
      500 if GCS returns an error (SessionLoadError).
    """
    try:
        doc = store_load(uid, session_id)
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
    doc = _require_session(auth.uid, session_id)

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

    brain_state = doc["brain_state"]

    # Compute planning period in the parent's local timezone
    timezone_str: str = doc.get("timezone") or "UTC"
    plan_period = compute_plan_period(timezone_str)

    # Run pipeline stages 5–11
    try:
        brain_state, gate_report = run_plan_pipeline(
            brain_state=brain_state,
            admin_debug=_ADMIN_DEBUG,
        )
    except Exception as exc:
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

    # ── Translate feedback (base plan only) → brain signals, then build Week 2 ──
    activity_feedback = translate_feedback_to_activity_feedback(
        doc.get("feedback") or [],
        base_entry.get("plan_response") or {},
        base_plan_id,
    )
    try:
        # run_refresh_pipeline works on a deep copy; brain_state is untouched on error.
        refresh_state = run_refresh_pipeline(brain_state, activity_feedback)
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

    plan_response = adapt_weekly_plan(
        session_id=session_id,
        age_in_months=doc["age_in_months"],
        daily_time_minutes=doc["daily_time_minutes"],
        weekly_schedule=weekly_schedule,
        plan_period=plan_period,
    )
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

    # Look up internal metadata for enrichment. Original cards resolve via the
    # frozen plan_internal; Beta 2.1 swapped/added cards live only in the overlay,
    # so fall back to the overlay's internal block. This keeps domain/subdomain
    # and doctor/ST/OT-PT report routing working for customized activities.
    internal_act = _find_activity_internal(
        doc, body.plan_id, body.activity_id, body.day
    )
    if internal_act is None:
        internal_act = find_overlay_internal(
            get_overlay(doc, body.plan_id), body.activity_id
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

@app.get(
    "/api/v1/session/{session_id}",
    tags=["session"],
)
async def session_get(
    session_id: str,
    auth: Annotated[AuthUser, Depends(require_auth)],
):
    """
    Reload a saved session in frontend-safe form.

    Returns only what the Lovable frontend needs to resume a session:
      - If interview in progress: session_id, status, current_question
      - If plan ready: session_id, status, age/time metadata, current plan,
        progress_summary, feedback_summary

    Never returns: child name, brain_state, plan_internal, gate_report
    (gate_report is included only when ADMIN_DEBUG=1), or any internal
    debug fields.
    """
    doc = _require_session(auth.uid, session_id)

    status = doc.get("status", "questions")

    # ── Interview in progress ─────────────────────────────────────────────
    if status in ("questions", "interview_complete"):
        interview = doc.get("interview") or {}
        current_q = get_current_question(interview) if status == "questions" else None
        return {
            "session_id": session_id,
            "status": status,
            "current_question": current_q,
        }

    # ── Plan ready ────────────────────────────────────────────────────────
    current_plan_id = doc.get("current_plan_id")
    plans = doc.get("plans") or {}
    plan_entry = plans.get(current_plan_id, {}) if current_plan_id else {}

    original_plan_response = plan_entry.get("plan_response") or {}
    plan_period   = plan_entry.get("plan_period") or {}
    # Beta 2.1: resolve the current plan through its customization overlay (if any).
    # Identity-safe — returns the original object unchanged when there is no overlay,
    # so uncustomized sessions are byte-compatible with Beta 2.0. The stored
    # plan_response is never mutated.
    plan_response = resolve_plan_response(
        original_plan_response, get_overlay(doc, current_plan_id)
    )
    progress_summary = plan_response.get("progress_summary") or {}

    # Feedback summary — aggregate counts, no raw notes exposed
    feedback_list: List[Dict[str, Any]] = doc.get("feedback") or []
    total_feedback = len(feedback_list)
    completed = sum(1 for f in feedback_list if f.get("completion") == "did_it")
    flagged   = sum(1 for f in feedback_list if f.get("discuss_with_care_team"))
    domains_practised = list({
        f.get("domain", "") for f in feedback_list if f.get("domain")
    })
    feedback_summary = {
        "total":               total_feedback,
        "completed":           completed,
        "flagged_for_care_team": flagged,
        "domains_practised":   domains_practised,
    }

    # Beta 2.1: additive per-current-plan acceptance state (read from outside the
    # generated plan; never mutates plan_response/plan_internal).
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
    }

    # Include gate_report in plan_period only when ADMIN_DEBUG=1
    if _ADMIN_DEBUG:
        brain_state = doc.get("brain_state") or {}
        gate_report = brain_state.get("_gate_report")
        if gate_report:
            response["_gate_report"] = gate_report

    return response
