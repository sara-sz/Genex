# Genex Privacy & Compliance Baseline

**Status: Privacy-first / HIPAA-aligned beta baseline**
**Not HIPAA compliant** — this document records the steps taken toward a
responsible privacy foundation. Full HIPAA compliance requires additional
controls beyond what is listed here.

> **Summary:** Google Cloud BAA reviewed and accepted under sara@getgenex.com
> for project genex-mvp-2026. Covered-service baseline includes Cloud Run,
> Cloud Storage, Secret Manager, and Identity Platform. Genex is not claiming
> HIPAA compliance at this stage. Current status: privacy-first /
> HIPAA-aligned beta baseline.

---

## Google Cloud HIPAA Business Associate Addendum (BAA)

| Field            | Value                          |
|------------------|--------------------------------|
| Status           | ✅ Accepted                    |
| Accepted by      | sara@getgenex.com              |
| Accepted on      | May 13, 2026                   |
| Project          | genex-mvp-2026                 |
| Found at         | Cloud Console → IAM & Admin → Privacy & Security → Legal & Compliance |

**What this means:**
Google is now a Business Associate of Genex under HIPAA for the following
covered services used in this project:
- Google Cloud Run
- Google Cloud Storage
- Google Secret Manager
- Google Cloud Identity Platform

**What this does NOT mean:**
- Genex is not HIPAA compliant
- The BAA alone does not satisfy all HIPAA requirements
- Additional controls required before handling PHI: per-user encryption,
  audit logging, access controls, breach notification procedures, and a
  documented compliance program

---

## Data Privacy Measures (v0.3-auth-staging)

| Measure                              | Status         | Notes                                      |
|--------------------------------------|----------------|--------------------------------------------|
| Child name not stored in GCS         | ✅ Implemented | Verified with privacy_check.py sentinel    |
| Child name not sent to OpenAI        | ✅ Implemented | Engine receives "your child"               |
| Per-user GCS paths (user_id)         | ✅ Implemented | sessions/{uid}/{sid}.json                  |
| Consent timestamp stored             | ✅ Implemented | consent_given + consent_timestamp in JSON  |
| Privacy notice in app                | ✅ Implemented | screen_privacy_policy() + footer link      |
| Allowlist-gated registration         | ✅ Implemented | config/allowlist.json via GCS              |
| Email/password auth                  | ✅ Implemented | Identity Platform — staging tested         |
| Persistent accounts                  | ✅ Implemented | Verified: sign out → reopen → sign in ✅   |
| Password reset email                 | ✅ Implemented | Verified: reset email received ✅          |
| GCS session storage (staging)        | ✅ Implemented | Verified: sessions + feedback in GCS ✅    |
| Audit logging                        | ❌ Not yet     | Planned post-pilot                         |
| Per-user encryption at rest          | ❌ Not yet     | Planned post-pilot                         |
| Formal HIPAA compliance program      | ❌ Not yet     | Required before clinical partnerships      |

---

## Identity Platform Setup

| Field            | Value                          |
|------------------|--------------------------------|
| Status           | ✅ Complete (staging)          |
| Completed by     | sara@getgenex.com              |
| Completed on     | May 13, 2026                   |
| Project          | genex-mvp-2026                 |
| Target service   | genex-parent-staging only      |
| Live pilot       | Unaffected (genex-parent v0.2) |

Steps completed:
- [x] Enable Identity Platform API in Cloud Console
- [x] Configure email/password provider (passwordless login disabled)
- [x] Store FIREBASE_API_KEY in Secret Manager
- [x] Grant staging service account access to secret
- [x] Redeploy genex-parent-staging with AUTH_MODE=identity_platform
- [x] Test persistent accounts — sign out, reopen browser, sign back in ✅
- [x] Test password reset email delivery ✅

---

## What Remains Before 100-Parent Beta

1. ~~Identity Platform connected to staging and tested~~ ✅ Done
2. ~~Password reset email delivery confirmed~~ ✅ Done
3. ~~Consent persistence verified in session JSON~~ ✅ Done
4. ~~Internal tier labels sanitized in saved plan~~ ✅ Done
5. 5-family pilot on v0.2 completed and feedback collected
6. staging promoted to production (genex-parent v0.3)
7. v0.2 pilot service retired
8. Privacy policy published at getgenex.com
9. Data deletion workflow tested end-to-end (delete_user_data.py)

---

## What Remains Before Clinical Partnerships

1. All items above
2. Formal HIPAA risk assessment
3. Documented policies: access control, breach notification, data retention
4. Audit logging enabled
5. BAAs with any additional vendors (OpenAI, etc.)
6. Legal review of privacy policy and terms of service

---

---

## Compliance Summary

- Google Cloud BAA reviewed and accepted under sara@getgenex.com for project genex-mvp-2026.
- Covered-service baseline includes Cloud Run, Cloud Storage, Secret Manager, and Identity Platform.
- Genex is not claiming HIPAA compliance at this stage.
- Current status: privacy-first / HIPAA-aligned beta baseline.

---

*This document should be updated whenever a compliance-relevant change is made.*
*Last updated: May 13, 2026*
