# Genex API Contract — Phase 1
Version: June 2026  
Status: Approved for implementation — corrections applied 2026-06-12.

---

## What the Brain Actually Does (Pipeline Stages)

| # | Stage | Module | Key Function |
|---|-------|--------|-------------|
| 1 | Profile init | `interview_engine.py` | `init_state_from_profile()` |
| 2 | Concern routing | `interview_engine.py` | `concern_router()`, `choose_focus_domains()` |
| 3 | Question building | `interview_engine.py` | `build_domain_questions()` |
| 4 | Answer recording | `interview_engine.py` | `record_answer()` |
| 5 | Scoring | `scoring.py`, `delay_engine.py` | `finalize_domain_dev_age()` |
| 6 | Bridge planning | `bridge_selector.py` | `build_bridge_plan_for_category()` |
| 7 | Activity generation | `activity_engine.py` | `generate_category_activity_bank()` |
| 8 | Safety filtering | `safety.py` | `apply_safety_constraints_to_activities()` |
| 9 | Support tier | `support_tiers.py` | `determine_family_guidance_floor()` |
| 10 | Weekly scheduling | `scheduler.py` | `build_weekly_schedule()` |
| 11 | Final gate | `final_plan_gate.py` | `validate_and_repair_final_plan()` |
| 12 | Persistence | `storage.py` | `save_json()` |

The central data structure is a `state: Dict` built up across stages 1–11 and persisted at stage 12. The FastAPI wrapper orchestrates these calls across multiple HTTP requests, using GCS as the source of truth for session state.

**Note on follow-up questions:** `get_followup_schema()` and `normalize_followup_answer()` exist in `genex_core/interview_engine.py` and are imported in `app.py` lines 78–79, but are **never called anywhere in the active codebase**. `record_answer()` is always invoked with 4 arguments — no `followup_key`. Zero test coverage. Latent/dead code. Not part of this API.

---

## Authentication

All endpoints except `GET /health` require a Firebase ID token.

**Header:**
```
Authorization: Bearer <firebase_id_token>
```

**Server-side verification flow (runs on every protected request):**
1. Extract the `Authorization` header → return `401` if missing or malformed.
2. Call `firebase_admin.auth.verify_id_token(token)` → decoded token containing `uid` and `email`.
3. Check `email` is in the `ALLOWED_EMAILS` env var (comma-separated) → return `403` if absent.
4. Bind `uid` to the request context — used for session ownership and all GCS path scoping.

**Session ownership enforcement:** Every session read/write uses GCS path `sessions/{uid}/{session_id}.json`. On every session-bearing endpoint, after loading the session, verify the stored `owner_uid` matches the request `uid`. Return `403 session_not_owned_by_user` on mismatch.

**Environment variables required on the server:**
| Var | Purpose |
|-----|---------|
| `FIREBASE_PROJECT_ID` | Firebase project; used to initialise `firebase_admin` |
| `ALLOWED_EMAILS` | Comma-separated allowlist, e.g. `sara@example.com,test@example.com` |
| `GCS_BUCKET` | GCS bucket name for session persistence |
| `OPENAI_API_KEY` | Required by activity engine and concern router |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins, e.g. `https://preview--dear-journey.lovable.app` |
| `ADMIN_DEBUG` | `0` (default) — set to `1` to include `gate_report` in `/plan` response |

**Lovable frontend responsibilities:**
- Sign the parent in via Firebase Authentication (Google or email/link).
- Retrieve the ID token: `await user.getIdToken()`.
- Attach it as `Authorization: Bearer <token>` on every API request.
- Firebase SDK refreshes tokens automatically before the 1-hour expiry.
- On `401`: re-authenticate and retry once.
- On `403`: show "Access not available yet" — parent is not on the allowlist.
- Store `child_name` in local React state only. Do not send it back to the API after `/session/start`. On page reload, restore from `localStorage` if needed — the API will not return it.

---

## Child Name Privacy Rule

`child_name` is sent by Lovable in the `/session/start` request body as a display aid only. The API handles it as follows:

- `child_name` is **not stored in GCS**. GCS session files use `"your child"` wherever a name would appear.
- `child_name` is **not passed to `init_state_from_profile()`**. The API passes the string `"your child"` as the `name` argument. Question texts will say "Can your child walk..." — this is intentional and acceptable.
- `child_name` is **not included in any OpenAI prompt**. Concern text (`parent_concern`) is passed to the brain as typed by the parent; it may naturally contain the child's name, but that is the parent's own text and is acceptable.
- `child_name` is **not returned in any API response** after `/session/start`. On reload (`GET /session`), `child_display_name` is absent from the response body.

**Reload implication:** Lovable must store `child_name` in `localStorage` (or equivalent) alongside `session_id` after a successful `/session/start`. On page reload, restore it from local storage. The API is intentionally name-blind after the initial request.

**If this rule needs to change** (e.g., to restore name on a new device), that requires an explicit decision to store the name in GCS and re-evaluate the OpenAI prompt exposure. Do not change this rule without discussion.

---

## Session State Model — GCS as Source of Truth

GCS is the source of truth. Memory is an optional speed cache only. A container restart must not lose a parent's in-progress session.

**Save rule:** Every protected endpoint that mutates session state must save to GCS before returning a response.

```
POST /session/start     → init state → save to GCS → return first question
POST /answer            → load → mutate state → save to GCS → return next question or complete
POST /plan              → load → run pipeline → save to GCS → return plan
POST /feedback          → load → append feedback → save to GCS → return ok
POST /report            → load (read-only) → generate report text → return (no GCS write needed)
GET  /session/{id}      → load from memory or GCS → return current state
```

**Load rule (every session-bearing endpoint):**
1. Check memory cache by `session_id`.
2. If cache miss, load from GCS at `sessions/{uid}/{session_id}.json`.
3. Verify `owner_uid` matches the request `uid`.
4. Continue with state dict.

**Memory cache:** Use as a read-through cache for speed. No TTL constraint needed for correctness — correctness is guaranteed by GCS. Cache entries can be evicted freely.

**GCS session document structure** (what is saved):
```json
{
  "session_id": "uuid-v4",
  "owner_uid": "firebase-uid",
  "created_at": "2026-06-12T10:00:00Z",
  "status": "questions | interview_complete | plan_ready",
  "age_in_months": 29,
  "daily_time_minutes": 10,
  "diagnosis_or_condition": "Down syndrome",
  "brain_state": { },
  "feedback": [],
  "plan_generated": false
}
```

`child_name` does not appear in this document. `brain_state` is the raw `state` dict from the brain. `feedback` accumulates entries from `/feedback` calls.

---

## Diagnosis Dropdown — Exact Frontend Values

The Lovable dropdown sends these exact string values. The API must accept them verbatim.

| Frontend dropdown label (sent as-is) | Backend behaviour |
|---|---|
| `"No known diagnosis / not sure"` | Passed to brain as-is; adapter treats it identically to no-diagnosis routing |
| `"Down syndrome"` | Passed to brain as-is |
| `"ADHD"` | Passed to brain as-is |
| `"Autism spectrum"` | Passed to brain as-is |
| `"Other"` | Passed to brain as-is |
| `"Prefer not to say"` | Passed to brain as-is |

Dravet syndrome: **not a valid API value**. Not in the dropdown. Not in this table. Any request sending `"Dravet syndrome"` returns `422 Unprocessable Entity`.

The brain's internal concern router handles routing for all of the above. `adapters.py` does not need to translate these strings — they are passed directly to `init_state_from_profile()` as `diagnosis`.

---

## Proposed API Endpoints

### `GET /health`
No auth required.
```json
// Response 200
{"ok": true, "service": "genex-api", "version": "v22"}
```

---

### `POST /api/v1/session/start`
**Auth required.**

Creates a new session. Runs pipeline stages 1–3 (profile init, concern routing, domain selection, first question batch). Saves session to GCS. Returns first question.

**Request:**
```json
{
  "child_name": "Maya",
  "age_years": 2,
  "age_months": 5,
  "age_in_months": 29,
  "diagnosis_or_condition": "Down syndrome",
  "parent_concern": "She's working on walking independently and saying first words.",
  "daily_time_minutes": 10
}
```

Field notes:
- `child_name`: display aid only — stored in Lovable local state, never in GCS, never passed to brain as the actual name. The brain receives `"your child"`.
- `age_years` and `age_months`: Lovable collects these separately and computes `age_in_months = age_years * 12 + age_months`.
- `age_in_months`: the API validates that `age_in_months == age_years * 12 + age_months`. Returns `422` if inconsistent.
- `diagnosis_or_condition`: must exactly match one of the 6 valid frontend values listed above.
- `daily_time_minutes`: accepted values are `5`, `10`, `15`, `20` (or any integer ≥ 5).

**Response 200:**
```json
{
  "session_id": "uuid-v4",
  "status": "questions",
  "domains": ["movement_and_physical", "language_and_communication"],
  "total_questions_estimate": 6,
  "current_question": {
    "question_id": "movement_and_physical_18_0",
    "question_text": "Can your child walk across the room without holding furniture or an adult's hand?",
    "domain": "movement_and_physical",
    "domain_label": "Movement & Physical",
    "progress_index": 0,
    "progress_total_estimate": 6
  }
}
```

Notes:
- `total_questions_estimate` is approximate — the adaptive stopping rule may end early.
- `progress_total_estimate` is for progress dots only, never shown as "Question X of Y".
- `child_display_name` is **not** in this response. Lovable stores `child_name` from the request and uses it locally.
- The session is saved to GCS before this response is returned.

---

### `POST /api/v1/session/{session_id}/answer`
**Auth required.** Session must be owned by the authenticated user.

Records one answer (pipeline stage 4). Saves updated session to GCS. Returns next question or completion status.

**Request:**
```json
{
  "question_id": "movement_and_physical_18_0",
  "answer": "sometimes"
}
```

Valid `answer` values:

| Frontend label | API value |
|---|---|
| Yes, usually | `"yes"` |
| Sometimes | `"sometimes"` |
| Only with help | `"with_help"` |
| Not yet | `"no"` |
| Not sure | `"not_sure"` |

`record_answer()` is always called with 4 arguments. There is no `followup_key` in this API.

**Response — next question:**
```json
{
  "status": "next_question",
  "current_question": {
    "question_id": "movement_and_physical_24_1",
    "question_text": "Can your child jump with both feet leaving the ground at the same time?",
    "domain": "movement_and_physical",
    "domain_label": "Movement & Physical",
    "progress_index": 1,
    "progress_total_estimate": 6
  }
}
```

**Response — interview complete:**
```json
{
  "status": "interview_complete",
  "ready_for_plan": true,
  "questions_answered": 6
}
```

The session is saved to GCS before every response from this endpoint.

---

### `POST /api/v1/session/{session_id}/plan`
**Auth required.** Session must be owned by the authenticated user.

Runs pipeline stages 5–11 (scoring, bridge planning, activity generation, safety filtering, support tiers, scheduling, final gate). Saves completed plan to GCS. Returns frontend-ready weekly plan.

**Request:** Empty body — all state is server-side.
```json
{}
```

**Response 200:**
```json
{
  "session_id": "uuid",
  "age_in_months": 29,
  "daily_time_minutes": 10,
  "daily_card_count": 2,
  "week": [
    {
      "day": "Monday",
      "activities": [
        {
          "id": "slot-uuid",
          "title": "Snack Choice Words",
          "domain": "language_and_communication",
          "domain_label": "Talking and Communicating",
          "duration_label": "5–15 min",
          "why": "Snack-time choices create natural low-pressure language moments.",
          "instructions": "Hold up two snacks, one in each hand...",
          "materials": "2 small snack options",
          "success_criteria": "Your child communicates a choice using a word, sound, gesture, or look.",
          "make_easier": "Present only one snack and wait for any response before offering a piece.",
          "make_harder": "After handing it over, ask 'more cracker?' before the next piece.",
          "group_play": "Two children each name one snack per round and share the choosing.",
          "avoid": "Avoid giving the snack before your child responds — the wait is the opportunity."
        }
      ]
    },
    {
      "day": "Tuesday",
      "activities": ["..."]
    }
  ],
  "progress_summary": {
    "domains_covered": [
      {"key": "language_and_communication", "label": "Talking and Communicating"},
      {"key": "movement_and_physical",      "label": "Movement & Physical"}
    ],
    "activity_count": 10,
    "days": 5,
    "estimated_weekly_minutes": 50
  }
}
```

Notes:
- `child_display_name` is **not** in this response. Lovable uses its locally stored child name for display.
- `doctor_note` is **not** in this response. Reports are generated on demand via `POST /report`.
- `id` per activity is a deterministic UUID from `session_id + day + slot_index` — stable across reloads.
- `domain_label` uses parent-friendly labels (see domain label mapping below).
- `gate_report` is not included unless `ADMIN_DEBUG=1` server-side.
- Activity generation takes 10–20 seconds (OpenAI calls) — Lovable must show its loading screen for the full duration of this call.
- `daily_card_count` follows `_max_cards_per_day()`: 5 min → 1, 10–29 min → 2, 30+ min → 3.
- Session is saved to GCS (with plan included) before this response is returned.

**Domain label mapping:**
| `domain` (brain key) | `domain_label` (frontend) |
|---|---|
| `language_and_communication` | Talking and Communicating |
| `movement_and_physical` | Movement & Physical |
| `social_and_emotional` | Social & Emotional |
| `cognitive` | Learning & Cognitive |

---

### `POST /api/v1/session/{session_id}/feedback`
**Auth required.** Session must be owned by the authenticated user.

Saves activity feedback. Appends to the session's `feedback` list in GCS.

**Request:**
```json
{
  "activity_id": "slot-uuid",
  "day": "Monday",
  "enjoyment": "loved_it",
  "difficulty": "just_right",
  "completion": "did_it",
  "discuss_with_care_team": false,
  "care_team_member": null,
  "note": ""
}
```

Valid `enjoyment`: `"loved_it"` | `"it_was_okay"` | `"not_really"`  
Valid `difficulty`: `"too_easy"` | `"just_right"` | `"too_hard"`  
Valid `completion`: `"did_it"` | `"didnt_want_to_try"` | `"wasnt_ready_yet"`  
Valid `care_team_member`: `"Doctor"` | `"ST"` | `"OT"` | `"PT"` | `null`

Lovable uses "care team" not "provider" in all UI copy.

**Response 200:**
```json
{
  "ok": true,
  "activities_done_today": 1,
  "flagged_for_care_team": false
}
```

Session is saved to GCS (with feedback appended) before this response is returned.

---

### `POST /api/v1/session/{session_id}/report`
**Auth required.** Session must be owned by the authenticated user.

Generates a care team report on demand. No GCS write — read-only against the session state.

Supports the Reports page in the Lovable UX, which offers four choices: Doctor, Speech Therapist, Occupational Therapist, Physical Therapist.

**Request:**
```json
{
  "report_type": "doctor"
}
```

Valid `report_type` values: `"doctor"` | `"speech_therapist"` | `"occupational_therapist"` | `"physical_therapist"`

**Response 200:**
```json
{
  "session_id": "uuid",
  "report_type": "doctor",
  "title": "Doctor Report",
  "body": "Your child (29 months) has been working on language and motor development at home using the Genex program. Over the past week, activities have focused on Talking and Communicating and Movement & Physical skills..."
}
```

`title` values by `report_type`:
| `report_type` | `title` |
|---|---|
| `doctor` | Doctor Report |
| `speech_therapist` | Speech Therapist Report |
| `occupational_therapist` | Occupational Therapist Report |
| `physical_therapist` | Physical Therapist Report |

**Phase 1 implementation note:** All four report types use the same underlying session summary (drawn from the brain's `screen_doctor_note()` logic in `app.py`), with the title and opening framing adjusted per type. Richer per-discipline framing can be added later. The important thing is that the frontend has a clean four-way contract now.

The report body uses `"your child"` throughout — child name is not included.

---

### `GET /api/v1/session/{session_id}`
**Auth required.** Session must be owned by the authenticated user.

Reloads a saved session (page refresh, returning parent).

**Response 200 — plan ready:**
```json
{
  "session_id": "uuid",
  "status": "plan_ready",
  "age_in_months": 29,
  "daily_time_minutes": 10,
  "plan": { }
}
```
`plan` has the same shape as the `POST /plan` response body. `child_display_name` is not present — Lovable restores it from `localStorage`.

**Response 200 — interview in progress:**
```json
{
  "session_id": "uuid",
  "status": "questions",
  "current_question": { }
}
```

**Response 404:** `{"error": "session_not_found"}`  
**Response 403:** `{"error": "session_not_owned_by_user"}`

---

## New Files to Create

```
api/
├── __init__.py      # empty
├── main.py          # FastAPI app, route handlers, CORS config
├── schemas.py       # Pydantic request/response models
├── adapters.py      # brain state → frontend JSON (normalise field names, add slot UUIDs, report text)
├── session_store.py # GCS source of truth + memory read-through cache
├── auth.py          # Firebase ID token verification; ALLOWED_EMAILS check
└── pipeline.py      # Thin orchestration layer calling genex_core functions in order
Dockerfile.api       # Separate container for the API service
requirements.api.txt # fastapi + uvicorn + pydantic + firebase-admin + google-cloud-storage
```

**Existing files — DO NOT TOUCH:**
- All of `genex_core/`
- `app.py` (Streamlit)
- `tests/`
- `requirements.txt`, `Dockerfile` (Streamlit service)

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| **Plan generation is slow** (10–20s, OpenAI calls) | Lovable loading screen must stay open | `/plan` is async; Lovable loading screen already exists |
| **Field name inconsistency** in activity cards (`success` vs `success_criteria`, `make_easier` vs `easier`) | Frontend shows blank fields | `adapters.py` normalises all field names before response |
| **Concern router LLM escalation** at `/session/start` | Extra latency (~2s) | Acceptable; ensure `OPENAI_API_KEY` is set |
| **GCS write failure** mid-request | Session state lost | Raise `500`, log the error; client retries; GCS is durable so transient failures are rare |
| **CORS** — Lovable preview domain is `preview--dear-journey.lovable.app`, deployed domain TBD | Browser blocks API calls | Set `ALLOWED_ORIGINS` env var; include both preview and deployed domains |
| **Firebase token expiry** (1 hour) mid-session | `401` mid-onboarding | Firebase SDK auto-refreshes; Lovable retries once on `401` |
| **`ALLOWED_EMAILS` not set** | All authenticated users get `403` | Document as required deploy step; API should fail loudly at startup if unset |
| **Child name in `parent_concern` text** going to OpenAI | Privacy concern, lower severity | Parent-typed text; acceptable. The formal child name field is never forwarded. |
| **`age_in_months` inconsistency** | Wrong scoring | API validates `age_in_months == age_years * 12 + age_months`; returns `422` on mismatch |

---

## Implementation Steps

### Step 1 — Auth skeleton (implement now)
- Create `api/__init__.py`, `api/main.py`, `api/auth.py`
- Add `GET /health` (no auth)
- Add Firebase auth dependency using `firebase_admin`
- Add `ALLOWED_EMAILS` allowlist check
- Test `401` (no token), `403` (valid token, not in allowlist), `200` (valid token, in allowlist) against a protected stub endpoint

### Step 2 — Session store + `/session/start`
- Create `api/session_store.py` with GCS write-through and memory read-through
- Create `api/pipeline.py` with stage 1–3 orchestration
- Add `POST /api/v1/session/start`
- Confirm GCS write happens before response

### Step 3 — `/answer`
- Add `POST /api/v1/session/{session_id}/answer`
- Confirm 5-option-only flow: yes / sometimes / with_help / no / not_sure
- No follow-up questions

### Step 4 — `/plan`
- Add `POST /api/v1/session/{session_id}/plan`
- Run existing V22 pipeline stages 5–11 and final gate
- Return frontend-ready weekly plan JSON
- Create `api/adapters.py` for field normalisation and deterministic slot UUIDs

### Step 5 — `/feedback` and `/report`
- Add `POST /api/v1/session/{session_id}/feedback`
- Add `POST /api/v1/session/{session_id}/report` (four report types)
- Add `GET /api/v1/session/{session_id}` reload endpoint

### Step 6 — Testing
Run existing regression tests first:
```bash
cd genex-parent && python3 tests/test_regression.py
```
Then API smoke tests via curl for all four required cases:
1. Down syndrome + speech + gross motor, 24 months, 10 min → movement + language, `daily_card_count: 2`, `duration_label: "5–15 min"`
2. ADHD, 60 months, 15 min → 2 cards/day, no same-day repeats, ADHD-relevant activities
3. Speech + OT + PT, 50 months, 10 min → 2 domains represented, not language-only
4. Terry, 50 months, speech concern + all-yes answers → useful language plan still generated

---

## How to Test Locally

```bash
# 1. Install dependencies
pip install fastapi uvicorn pydantic firebase-admin google-cloud-storage --break-system-packages

# 2. Set required env vars
export FIREBASE_PROJECT_ID="your-project-id"
export ALLOWED_EMAILS="sara@example.com"
export GCS_BUCKET="your-gcs-bucket"
export OPENAI_API_KEY="sk-..."
export ALLOWED_ORIGINS="http://localhost:3000"
export ADMIN_DEBUG=0

# 3. Start the API
cd genex-parent
uvicorn api.main:app --reload --port 8001

# 4. Health check (no auth)
curl http://localhost:8001/health

# 5. Auth smoke test — expect 401
curl -s -o /dev/null -w "%{http_code}" \
  http://localhost:8001/api/v1/session/start

# 6. Full session flow (TOKEN = Firebase ID token from CLI or test helper)
TOKEN="<firebase-id-token>"

SESSION=$(curl -s -X POST http://localhost:8001/api/v1/session/start \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "child_name": "Maya",
    "age_years": 2,
    "age_months": 0,
    "age_in_months": 24,
    "diagnosis_or_condition": "Down syndrome",
    "parent_concern": "gross motor delay and speech delay",
    "daily_time_minutes": 10
  }' | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")

echo "Session: $SESSION"

# 7. Answer questions (repeat until status=interview_complete)
curl -s -X POST http://localhost:8001/api/v1/session/$SESSION/answer \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"question_id":"<from_previous_response>","answer":"sometimes"}'

# 8. Generate plan (10–20s)
curl -s -X POST http://localhost:8001/api/v1/session/$SESSION/plan \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{}' | python3 -m json.tool

# 9. Generate report
curl -s -X POST http://localhost:8001/api/v1/session/$SESSION/report \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"report_type":"doctor"}' | python3 -m json.tool
```

---

## Open Questions (Confirm Before Phase 2 Deploy)

1. **Firebase project**: Confirm `FIREBASE_PROJECT_ID` and whether a service account JSON is mounted as a Cloud Run secret, or if Application Default Credentials (ADC) are used. ADC is preferred on Cloud Run.

2. **ALLOWED_EMAILS bootstrap**: Provide the initial comma-separated allowlist to set in the env var at deploy time.

3. **GCS bucket**: Confirm the bucket name for `GCS_BUCKET`. The existing Streamlit service already writes to one — confirm whether the API should use the same bucket or a separate one.

4. **Lovable CORS domains**: Confirm the exact Lovable preview and production domain(s) to add to `ALLOWED_ORIGINS`.
