# Changelog

All notable changes to Genex Parent Copilot are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

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
