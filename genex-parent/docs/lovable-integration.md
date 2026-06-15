# Genex API — Lovable Frontend Integration Guide

**API staging URL:** `https://genex-api-staging-67icluiswq-uc.a.run.app`  
**Last updated:** 2026-06-12  
**Commit:** `8b3fd20`

---

## Part 1 — CORS Redeploy (run this first)

The deploy script now supports `https://lovable.dev` in its default `ALLOWED_ORIGINS`.
Run the following from `genex-parent/`:

```bash
SKIP_BUILD=1 bash scripts/deploy_api_staging.sh
```

That will redeploy using the existing image with the updated origins:
`http://localhost:3000,http://localhost:5173,https://lovable.dev`

**Post-deploy verification (run from your terminal):**

```bash
API="https://genex-api-staging-67icluiswq-uc.a.run.app"

# 1. /health → 200
curl -s "$API/health" | python3 -m json.tool
# Expected: {"ok": true, "service": "genex-api", "version": "v22"}

# 2. Protected route without token → 401
curl -s -o /dev/null -w "%{http_code}" "$API/api/v1/session/fake/plan"
# Expected: 401

# 3. Check ALLOWED_ORIGINS in Cloud Run console
#    Console → Cloud Run → genex-api-staging → Edit & Deploy → Variables
#    ALLOWED_ORIGINS should include https://lovable.dev
#    ADMIN_DEBUG should be 0
#    LOCAL_SESSION_FALLBACK should be 0
```

---

## Part 2 — Firebase Web SDK Config

Lovable needs the Firebase **web** config (not the Admin SDK config used server-side).
This is safe to expose in browser code — it identifies your project, not secrets.

**Where to get it:**
1. Go to [Firebase Console](https://console.firebase.google.com) → project `genex-mvp-2026`
2. Click the gear icon → **Project settings** → **General** tab
3. Scroll to **Your apps** → click your web app (or add one if none exists)
4. Copy the `firebaseConfig` object

The values will look like this (fill in your real values):

```javascript
const firebaseConfig = {
  apiKey:            "AIza...",          // from Firebase Console (also in Secret Manager as FIREBASE_API_KEY)
  authDomain:        "genex-mvp-2026.firebaseapp.com",
  projectId:         "genex-mvp-2026",
  storageBucket:     "genex-mvp-2026.firebasestorage.app",
  messagingSenderId: "...",              // from Firebase Console
  appId:             "1:...:web:...",    // from Firebase Console
};
```

**apiKey shortcut** — if you don't want to open the Console:
```bash
gcloud secrets versions access latest --secret=FIREBASE_API_KEY --project=genex-mvp-2026
```

---

## Part 3 — Lovable Environment Variables

Add these to your Lovable project's environment settings
(**Settings → Environment Variables** in the Lovable editor):

| Variable | Value | Notes |
|---|---|---|
| `VITE_FIREBASE_API_KEY` | `AIza...` | from Secret Manager or Firebase Console |
| `VITE_FIREBASE_AUTH_DOMAIN` | `genex-mvp-2026.firebaseapp.com` | |
| `VITE_FIREBASE_PROJECT_ID` | `genex-mvp-2026` | |
| `VITE_FIREBASE_STORAGE_BUCKET` | `genex-mvp-2026.firebasestorage.app` | |
| `VITE_FIREBASE_MESSAGING_SENDER_ID` | `...` | from Firebase Console |
| `VITE_FIREBASE_APP_ID` | `1:...:web:...` | from Firebase Console |
| `VITE_API_BASE_URL` | `https://genex-api-staging-67icluiswq-uc.a.run.app` | no trailing slash |

**Firebase SDK to install in Lovable:**
```bash
npm install firebase
```

---

## Part 4 — Authentication Pattern

Every API call (except `/health`) must include a Firebase ID token as a Bearer token.

```typescript
import { initializeApp } from "firebase/app";
import { getAuth, signInWithEmailAndPassword, getIdToken } from "firebase/auth";

const firebaseConfig = {
  apiKey:            import.meta.env.VITE_FIREBASE_API_KEY,
  authDomain:        import.meta.env.VITE_FIREBASE_AUTH_DOMAIN,
  projectId:         import.meta.env.VITE_FIREBASE_PROJECT_ID,
  storageBucket:     import.meta.env.VITE_FIREBASE_STORAGE_BUCKET,
  messagingSenderId: import.meta.env.VITE_FIREBASE_MESSAGING_SENDER_ID,
  appId:             import.meta.env.VITE_FIREBASE_APP_ID,
};

const app  = initializeApp(firebaseConfig);
const auth = getAuth(app);

// Sign in (Google sign-in also works — any Firebase auth method)
await signInWithEmailAndPassword(auth, email, password);

// Get a fresh token before each API call (Firebase refreshes automatically)
async function getToken(): Promise<string> {
  const user = auth.currentUser;
  if (!user) throw new Error("Not signed in");
  return getIdToken(user);  // pass true to force refresh if needed
}

// Wrapper for all API calls
const API_BASE = import.meta.env.VITE_API_BASE_URL;

async function apiCall(method: string, path: string, body?: object) {
  const token = await getToken();
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: {
      "Content-Type":  "application/json",
      "Authorization": `Bearer ${token}`,
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw Object.assign(new Error(err.detail ?? res.statusText), { status: res.status });
  }
  return res.json();
}
```

**Auth rules enforced by the API:**
- No token → **401**
- Valid token but email not in allowlist → **403** (`not authorised`)
- Valid token, authorised email, but session belongs to different uid → **403** (`session not found`)

---

## Part 5 — Full API Flow

### Step 1 — Start a session

```typescript
const session = await apiCall("POST", "/api/v1/session/start", {
  child_name:              "Emma",       // stored locally by Lovable only; never sent to OpenAI
  age_years:               3,
  age_months:              6,
  age_in_months:           42,           // must equal age_years * 12 + age_months
  diagnosis_or_condition:  "Down syndrome",
  parent_concern:          "She doesn't wave goodbye yet",
  daily_time_minutes:      20,
  timezone:                Intl.DateTimeFormat().resolvedOptions().timeZone,
});
// Returns: { session_id, status: "questions", domains, total_questions_estimate, current_question }

const SESSION_ID = session.session_id;
// IMPORTANT: store SESSION_ID in Lovable's local state. Do NOT send child_name to any further endpoint.
```

**`diagnosis_or_condition` must be exactly one of:**
- `"No known diagnosis / not sure"`
- `"Down syndrome"`
- `"ADHD"`
- `"Autism spectrum"`
- `"Other"`
- `"Prefer not to say"`

**`daily_time_minutes`** must be ≥ 5.

---

### Step 2 — Answer questions (loop)

```typescript
let question = session.current_question;
// question: { question_id, question_text, domain, domain_label, progress_index, progress_total_estimate }

while (true) {
  const parentAnswer = await showQuestionUI(question);
  // parentAnswer must be one of: "yes" | "sometimes" | "with_help" | "no" | "not_sure"

  const result = await apiCall("POST", `/api/v1/session/${SESSION_ID}/answer`, {
    question_id: question.question_id,
    answer:      parentAnswer,
  });

  if (result.status === "next_question") {
    question = result.current_question;
  } else if (result.status === "interview_complete") {
    // result: { status: "interview_complete", ready_for_plan: true, questions_answered: N }
    break;
  }
}
```

---

### Step 3 — Generate plan (long-running, ~60-180s)

```typescript
// /plan can take 60-180 seconds. Use a generous timeout and show a loading spinner.
let planData: any = null;

try {
  planData = await apiCall("POST", `/api/v1/session/${SESSION_ID}/plan`);
} catch (err: any) {
  if (err.status === undefined) {
    // Network timeout — the server keeps running. Poll GET /session until plan_ready.
    planData = await pollForPlan(SESSION_ID);
  } else {
    throw err;
  }
}

async function pollForPlan(sessionId: string, maxWaitMs = 180_000) {
  const deadline = Date.now() + maxWaitMs;
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 10_000));
    const s = await apiCall("GET", `/api/v1/session/${sessionId}`);
    if (s.status === "plan_ready") return s.plan;  // s.plan is the plan response object
  }
  throw new Error("Plan generation timed out");
}
```

**Exact plan response shape** (what `planData` contains):

```json
{
  "session_id":          "uuid",
  "plan_period": {
    "plan_id":           "uuid",
    "plan_type":         "weekly",
    "timezone":          "America/Los_Angeles",
    "generated_at":      "2026-06-12T...",
    "week_start_date":   "2026-06-09",
    "plan_start_date":   "2026-06-12",
    "plan_end_date":     "2026-06-15",
    "days_included":     ["Thursday", "Friday", "Saturday", "Sunday"],
    "is_partial_week":   true
  },
  "age_in_months":       42,
  "daily_time_minutes":  20,
  "daily_card_count":    2,
  "week": [
    {
      "day":  "Thursday",
      "date": "2026-06-12",
      "activities": [
        {
          "id":               "uuid",
          "title":            "Mirror Faces",
          "domain":           "social_and_emotional",
          "domain_label":     "Social & Emotional",
          "duration_label":   "5–15 min",
          "why":              "...",
          "instructions":     "...",
          "materials":        "mirror",
          "success_criteria": "...",
          "make_easier":      "...",
          "make_harder":      "...",
          "group_play":       "...",
          "avoid":            "...",
          "activity_date":    "2026-06-12"
        }
      ]
    }
  ],
  "progress_summary": {
    "domains_covered": [
      { "key": "social_and_emotional", "label": "Social & Emotional" }
    ],
    "activity_count":           6,
    "days":                     4,
    "estimated_weekly_minutes": 80
  }
}
```

**Key points:**
- There is **no top-level `plan_id`**. The plan ID is at `planData.plan_period.plan_id`. Store this for feedback calls.
- Activities are nested: `planData.week[i].activities[j]` — not a flat list.
- `materials` is a **string**, not an array.
- `duration_label` is the string `"5–15 min"` — there is no `duration_min` integer in the frontend response.
- Weekend days (Saturday/Sunday) are included with `weekend_mode` on each activity card.
- `domain` values are snake_case brain keys, not display labels — use `domain_label` for display.

**Privacy rules for Lovable:**
- Store `child_name` locally (React state or localStorage) for display only.
- **Never** send `child_name` to any POST endpoint after `/session/start`.
- `plan_internal` is not in the response — do not look for it.
- `brain_state` is never in any API response.
- `_gate_report` is absent when `ADMIN_DEBUG=0` (always in production).

---

### Step 4 — Save feedback (optional, per activity)

```typescript
const result = await apiCall("POST", `/api/v1/session/${SESSION_ID}/feedback`, {
  plan_id:                planData.plan_period.plan_id,  // from plan_period, not top-level
  activity_id:            activity.id,        // UUID `id` from the activity card
  day:                    "Thursday",
  activity_date:          "2026-06-12",        // ISO-8601 date the activity was done
  enjoyment:              "loved_it",          // "loved_it" | "it_was_okay" | "not_really"
  difficulty:             "just_right",        // "too_easy" | "just_right" | "too_hard"
  completion:             "did_it",            // "did_it" | "didnt_want_to_try" | "wasnt_ready_yet"
  discuss_with_care_team: false,
  care_team_member:       null,                // "Doctor" | "ST" | "OT" | "PT" | null
  note:                   "",
});
// Returns:
// {
//   "ok": true,
//   "feedback_id": "uuid",
//   "activities_done_today": 1,       // count of completed activities for this date
//   "flagged_for_care_team": false,
//   "metadata_found": true            // true when the activity_id matched plan_internal
// }
```

---

### Step 5 — Generate care-team report

```typescript
const report = await apiCall("POST", `/api/v1/session/${SESSION_ID}/report`, {
  report_type: "doctor",
  // other options: "speech_therapist" | "occupational_therapist" | "physical_therapist"
});
// Returns:
// {
//   "session_id":  "uuid",
//   "report_type": "doctor",
//   "title":       "Letter to Your Doctor",
//   "body":        "...(multi-paragraph plain text, uses 'your child' throughout)..."
// }
```

Reports use "your child" language — child's name is never in the session document.

---

### Step 6 — Reload session

```typescript
const saved = await apiCall("GET", `/api/v1/session/${SESSION_ID}`);
// status values: "questions" | "interview_complete" | "plan_ready"
```

**Shape when `status === "questions"` or `"interview_complete"`:**
```json
{
  "session_id":       "uuid",
  "status":           "questions",
  "current_question": { "question_id": "...", "question_text": "...", ... }
}
```

**Shape when `status === "plan_ready"`:**
```json
{
  "session_id":          "uuid",
  "status":              "plan_ready",
  "age_in_months":       42,
  "daily_time_minutes":  20,
  "current_plan_id":     "uuid",
  "plan":                { ...same shape as the /plan response... },
  "progress_summary":    { "domains_covered": [...], "activity_count": 6, "days": 4, "estimated_weekly_minutes": 80 },
  "feedback_summary": {
    "total":                 3,
    "completed":             2,
    "flagged_for_care_team": 1,
    "domains_practised":     ["social_and_emotional"]
  }
}
```

Note: `brain_state`, `plan_internal`, and `_gate_report` (unless ADMIN_DEBUG=1) are never in this response.

---

## Part 6 — Error Reference

| HTTP status | Meaning | Lovable action |
|---|---|---|
| 200 | Success | Use response |
| 401 | Missing or invalid token | Re-authenticate, retry |
| 403 | Email not in allowlist, or session belongs to different user | Show access-denied screen |
| 404 | Session not found | Show error, restart flow |
| 422 | Request validation error — `detail` array has field-level messages | Log + show form error |
| 500 | Server error | Show generic error, do not retry automatically |
| timeout | /plan took > client timeout | Poll GET /session (see Step 3 above) |

---

## Part 7 — What to NOT build yet

Wait for explicit approval before starting:
- Weekly refresh (`/weekly-refresh` endpoint — not yet implemented)
- Any changes to genex_core, the Streamlit parent app, or live pilot data
- Broad UI rework beyond wiring up the endpoints above

---

## Appendix — Quick CORS test from browser console

Open `https://lovable.dev` in Chrome → DevTools → Console:

```javascript
fetch("https://genex-api-staging-67icluiswq-uc.a.run.app/health")
  .then(r => r.json()).then(console.log)
// Should print: {ok: true, service: "genex-api", version: "v22"}
// If you see a CORS error, the redeploy hasn't propagated yet (wait 1-2 min)
```
