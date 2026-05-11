# Changelog

All notable changes to Genex are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [v0.1] — Genex Advisor Alpha — 2026-05

First complete end-to-end version deployed to Google Cloud Run for private advisor alpha testing.

### Core engine (deterministic — no AI dependency)
- CDC milestone bank loaded from `data/cdc_milestones.xlsx` covering 2–60 months across four domains: Movement & Physical, Language & Communication, Social & Emotional, Cognitive & Adaptive
- Concern router: maps free-text diagnosis + concern to subdomain weights via regex keyword patterns
- Adaptive band-by-band milestone interview with ceiling probe at chronological age and 2-consecutive-fail stopping rule per domain
- Developmental age scoring per domain from parent answers × weighted answer scores
- Support tier assignment per domain: `needs_special_support`, `monitor_and_enrich`, `enrich_and_observe`, `no_special_support`
- Family guidance floor: prevents over-pathologising when only mild or positive signals are present
- Safety profile: infers activity constraints from diagnosis + concern text (seizures, tone, feeding, vision, hearing, sensory)
- Weekly slot allocation: distributes daily time budget across domains weighted by gap and tier
- 7-day schedule builder: weekday short activities (Mon–Fri within daily budget), weekend extended/playdate-type activities (Sat–Sun, up to 25 min/day)

### Activity generation
- OpenAI GPT-4o-mini used for parent-friendly activity wording when `OPENAI_API_KEY` is set
- Deterministic fallback when key is absent: domain-specific materials inferred from milestone keywords, domain-appropriate instruction templates per skill type (walking, reaching, object permanence, turn-taking, calming, etc.)
- Safety constraints applied to all activities post-generation
- Category activity guardrails prevent cross-domain drift in AI-generated activities

### Streamlit app (8 screens)
- Welcome screen with disclaimer
- Child profile form (nickname + age in months + diagnosis + concern + daily time)
- Adaptive interview screen: band-by-band questions, back navigation, progress bar
- Delay estimate review screen
- Results screen: per-domain support tier cards + summary
- Editable weekly plan: delete activities, add from bank (card picker), confirm, download as text, 7-day display with weekend label
- Feedback screen: free-text + star rating, saved to GCS
- Restart / new case

### Deployment
- Google Cloud Run (us-central1, 1 vCPU, 1 GiB, min 0 / max 2 instances)
- Artifact Registry: `us-central1-docker.pkg.dev/genex-mvp-2026/genex-alpha/genex-alpha:latest`
- GCS session + feedback persistence: `genex-alpha-sessions-genex-mvp-2026`
- Shared-password gate (`ADVISOR_PASSWORD` from Secret Manager) — active for advisor alpha
- `OPENAI_API_KEY` from Secret Manager — used only for activity wording
- Public URL (`allUsers` → `roles/run.invoker`) active during advisor alpha only

### Not in v0.1
- No user accounts or per-advisor authentication
- No PHI — child nickname and age in months only
- No database — JSON files in GCS only
- No admin dashboard
- No automated scoring validation or test suite
