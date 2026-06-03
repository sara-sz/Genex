# Genex Parent Copilot — Roadmap

*Last updated: May 13, 2026*

---

## Current State

| Service               | Version | Status                          |
|-----------------------|---------|---------------------------------|
| genex-parent          | v0.2    | Live — 5-family pilot in progress |
| genex-parent-staging  | v0.3    | Staging — fully verified, ready to promote |

**Do not promote v0.3 to production until the 5-family pilot is complete.**

---

## Before 100-Parent Beta

Complete these in order after the 5-family pilot wraps up.

### 1. Collect and review 5-family pilot feedback
- Review session JSON and feedback files from `genex-parent-sessions-genex-mvp-2026`
- Note UX issues, content quality, and any privacy or safety concerns
- Decide what changes (if any) are needed before the 100-parent beta

### 2. Publish public privacy policy at getgenex.com/privacy
- Must be live before any parent outside your direct circle uses the app
- App already links to this URL — it must resolve
- Content should cover: data collected, storage, AI usage, deletion rights, contact

### 3. Verify data deletion end-to-end
Run `delete_user_data.py` against a staging test account and confirm all of the
following are removed:
- `sessions/{user_id}/` in GCS
- `feedback/{user_id}/` in GCS
- `consent/{user_id}.json` in GCS
- Identity Platform user account (document manual deletion steps or automate)

Update `delete_user_data.py` or add a deletion runbook if any of the above
are not currently covered.

### 4. Promote staging to production
```bash
cd ~/Projects/Genex/genex-parent
gcloud run deploy genex-parent \
  --source . \
  --region=us-central1 \
  --set-env-vars AUTH_MODE=identity_platform,GCS_BUCKET=genex-parent-sessions-genex-mvp-2026 \
  --set-secrets FIREBASE_API_KEY=FIREBASE_API_KEY:latest,OPENAI_API_KEY=OPENAI_API_KEY:latest \
  --project=genex-mvp-2026
```
Then confirm the production URL is live and test one full flow.

### 5. Retire v0.2
- Stop or delete the old shared-access-code service revision
- Archive v0.2 pilot session data if needed

### 6. Set up custom domain
Map `app.getgenex.com` to the production Cloud Run service:
- Cloud Run → Manage Custom Domains → Add mapping for `app.getgenex.com`
- Add the DNS records provided to your domain registrar
- Confirm HTTPS is working before sending the URL to parents

### 7. Add parent emails to the allowlist manually
Before sharing the app link, add each parent's email:
```bash
cd ~/Projects/Genex/genex-parent
python3 manage_allowlist.py add parent1@gmail.com parent2@gmail.com
python3 manage_allowlist.py upload
```
**Never commit real parent emails to GitHub. Upload to GCS only.**

### 8. Send onboarding email via Kit
Include in the email:
- App link (`https://app.getgenex.com` once domain is live)
- "Use the same email address you registered with"
- Privacy note (link to getgenex.com/privacy)
- Add to Home Screen instructions:
  - **iPhone:** Open link in Safari → tap Share → Add to Home Screen
  - **Android:** Open link in Chrome → tap menu (⋮) → Add to Home Screen

---

## Post-Beta Roadmap

### True PWA support
- Add `manifest.json` and service worker for proper install prompt and splash screen
- Planned for the React/Next.js rebuild — do not attempt in Streamlit

### Kit → Genex allowlist automation

**Goal:** When a parent signs up for beta on getgenex.com and is added to Kit,
their email should automatically be added to the GCS allowlist, and Kit should
send them the app link and onboarding instructions.

**Status:** Roadmap — do not build until after 5-family pilot feedback.

**Architecture:**
1. Parent submits email on getgenex.com
2. Kit adds subscriber to the beta list / tag / form
3. Kit webhook sends subscriber email to a Google Cloud Function
4. Cloud Function validates a shared secret/token (rejects unauthorized requests)
5. Cloud Function normalizes the email: lowercase + strip whitespace
6. Cloud Function reads the current GCS allowlist JSON
7. If email is not already present, adds it (duplicate-safe)
8. Cloud Function writes updated allowlist back to GCS
9. On GCS write failure, logs error and returns 500 so Kit can retry
10. Cloud Function logs: timestamp, hashed email, action result (added / duplicate / failed)
11. Kit sends welcome email containing:
    - Genex app link
    - Privacy notice summary + link to getgenex.com/privacy
    - "Sign in with the same email you used to sign up"
    - Add to Home Screen instructions (iPhone + Android)

**Constraints:**
- Email address only added to allowlist — no other subscriber data stored in GCS
- No child data involved at any point
- Shared secret stored in Secret Manager, not hardcoded
- Allowlist remains the single source of truth in GCS

---

## Before Clinical Partnerships

1. All 100-parent beta items above
2. Formal HIPAA risk assessment
3. Documented policies: access control, breach notification, data retention
4. Audit logging enabled (Cloud Logging or equivalent)
5. Per-user encryption at rest
6. BAAs with additional vendors (OpenAI, etc.)
7. Legal review of privacy policy and terms of service

---

*Keep real parent emails out of GitHub at all times.*
*The production allowlist lives in GCS only: `gs://genex-parent-sessions-genex-mvp-2026/config/allowlist.json`*
