# Changelog

All notable changes to Genex Parent Copilot are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [v0.3-auth-staging] — Genex Parent Copilot — 2026-05

Privacy-first / HIPAA-aligned beta baseline. Built on branch `parent-auth-baseline`.
**Not yet deployed to production.** Deployed as `genex-parent-staging` for testing only.
The v0.2 pilot service (`genex-parent`) remains live and unchanged.

### Auth
- Modular auth layer (`auth.py`) with two modes:
  `AUTH_MODE=mock` for local dev (in-memory, no cloud setup needed) and
  `AUTH_MODE=identity_platform` for staging/prod (Google Cloud Identity Platform)
- Email/password registration, login, password-reset email
- Allowlist-gated registration — only pre-approved emails may create an account
- `allowlist.py` reads from `gs://{GCS_BUCKET}/config/allowlist.json` with 5-minute cache
- Shared password gate (`_password_gate`) removed
- Sign-out button in sidebar

### Privacy and data
- Child first name stored only in browser session state (`child_display_name`)
  — never sent to GCS, never passed to genex_core or OpenAI
- genex_core receives `"your child"` as the name field so it appears in any AI prompt
- GCS session paths changed from `sessions/{child_name}_{ts}.json`
  to `sessions/{user_id}/{session_id}.json`
- GCS feedback paths changed from `feedback/feedback_{child_name}_{ts}.json`
  to `feedback/{user_id}/{session_id}_feedback.json`
- Stored session JSON now uses privacy schema:
  `user_id, session_id, child_id, age_months, diagnosis_or_condition, concern,
   answers, generated_plan, feedback, created_at, app_version, engine_version,
   consent_given, consent_timestamp`
- Stored feedback JSON: no child name; uses `user_id`, `session_id`
- Download filename no longer includes child name

### Consent
- Privacy notice and consent checkbox at registration
- `consent_given` and `consent_timestamp` stored in every session JSON
- Persistent footer on all screens: "not a diagnostic tool" disclaimer + Privacy Notice link
- Full `screen_privacy_policy()` accessible from footer and registration

### New screens
- `screen_login()` — email/password, links to register and reset
- `screen_register()` — allowlist check, privacy consent, account creation
- `screen_reset_password()` — sends password reset email
- `screen_privacy_policy()` — full privacy notice (accessible without login)

### Utilities
- `delete_user_data.py` — deletes all GCS files for a user_id; prompts for confirmation
- `review_feedback.py` — updated for v0.3 schema; handles both v0.2 and v0.3 files;
  recursive folder search for user_id subdirectory layout
- `config/allowlist.json` — local template; production copy lives in GCS

### Not changed
- `genex_core/` — not modified (name stripping happens at the app.py boundary)
- All interview, plan, and doctor notes screens — logic unchanged, display name updated
- `assets/style.css` — unchanged
- `Dockerfile` — unchanged
- `data/` — unchanged

### Not in v0.3
- No PWA conversion
- No database
- No activity completion tracking
- No chatbot, document upload, photos, voice
- Does not claim HIPAA compliance — "privacy-first / HIPAA-aligned beta baseline"

---

## [v0.2] — Genex Parent Copilot — 2026-05

First complete parent-facing version deployed to Google Cloud Run for a 5-family private pilot.
This app is separate from Genex Advisor Alpha (genex-alpha) and shares the same genex_core engine
without modifications.

### Child profile
- Child first name (display only — not stored as PHI)
- Age in months (calculated from date of birth or entered directly)
- Primary diagnosis or condition (free text)
- Main developmental concern (free text)

### Developmental interview
- Parent-led adaptive band-by-band milestone interview
- Answer choices: Yes, usually / Sometimes / Only with help / Not yet / Not sure
- 2-consecutive-fail stopping rule per domain (identical logic to Advisor Alpha)
- Progress bar across all questions
- Back navigation within a domain
- No tier labels or scores shown to parents at any point

### Weekly Planner
- Auto-generated 7-day schedule after interview completes
- One-time "plan is ready" banner on first load
- Editable plan: remove individual activities, add from curated activity bank
- Activity cards expand to show: why it helps, how to do it, what you need
- Weekend activities distinguished from weekday activities
- Plan persists across reruns via session state

### Doctor's Note
- Auto-generated structured summary of developmental observations
- Safe framing — no clinical language, no tier labels
- Formatted for easy copy-paste into a medical appointment note

### Feedback
- Free-text feedback field
- Saved to GCS as JSON (feedback only — no full session data in feedback record)

### Session storage
- Full session JSON saved to GCS bucket: `genex-parent-sessions-genex-mvp-2026`
- Local /tmp fallback when GCS is unavailable (e.g. local dev)
- Session ID generated per run (UUID)

### Deployment
- Google Cloud Run (us-central1, 1 vCPU, 1 GiB, min 0 / max 2 instances)
- Artifact Registry: `us-central1-docker.pkg.dev/genex-mvp-2026/genex-parent/genex-parent:latest`
- GCS session + feedback persistence: `genex-parent-sessions-genex-mvp-2026`
- Shared-password gate (`PARENT_PASSWORD` from Secret Manager) — active for pilot
- `OPENAI_API_KEY` from Secret Manager — used only for activity wording
- Public URL (`allUsers` → `roles/run.invoker`) active during pilot

### genex_core (shared, unmodified)
- Exact byte-for-byte copy of genex_core from genex-alpha v0.1
- Not modified for this release
- CDC milestone bank: `data/cdc_milestones.xlsx`

### Not in v0.2
- No user accounts or per-parent authentication beyond shared password
- No PHI — child first name and age in months only; names not persisted to GCS
- No database — JSON files in GCS only
- No activity completion tracking
- No progress tracking across sessions
- No chatbot, document upload, photos, or voice
- No admin dashboard
- No automated test suite

---

## Planned for v0.3 (post-pilot)
- Activity completion tracking ("Mark as done")
- Progress view across weeks
- Decisions pending pilot feedback
