# October pilot backend — security status (BACKEND 0.2)

Status of the secure backend foundation. **Code supporting a control is not the
control being in place.** Everything below is stated as one of:

| Label | Meaning |
|---|---|
| `IMPLEMENTED_IN_CODE` | Present in this repository and proven by tests in CI |
| `EXTERNAL_GO_LIVE_BLOCKER` | Requires action outside this repository; blocks real PHI |
| `NOT_IMPLEMENTED` | Deliberately absent in 0.2 |

No real PHI may be processed by this system until every
`EXTERNAL_GO_LIVE_BLOCKER` below is separately verified and signed off.

---

## Implemented in code (BACKEND 0.2)

| Control | Where |
|---|---|
| Environment-aware, fail-closed configuration; no defaults for prod values | `config/settings.py` |
| Production startup refuses missing/dev-looking/legacy resources | `config/settings.py` |
| Bearer-token verification behind an Identity Platform port | `auth/interface.py`, `auth/verifiers.py` |
| Production verification always checks revocation | `auth/verifiers.py` |
| Dev auth impossible in prod — three independent defences | settings, constructor, builder |
| Fail-closed default verifier (production authenticates nobody today) | `auth/verifiers.py` |
| Verified auth subject → application identity; never email, never a claim | `auth/resolver.py` |
| Deny-by-default relationship authorization | `authz/policy.py` |
| 401 vs 403 mapped centrally from denial reason | `authz/decisions.py` |
| Firestore-shaped persistence over a storage port; explicit codecs | `persistence/` |
| No delete anywhere — repositories or storage port | `persistence/` |
| Pilot store structurally separate from the Parent 2.3 GCS session store | `persistence/collections.py`, `config/settings.py` |
| PHI-bearing AI egress default OFF; requires flag **and** BAA reference | `aipolicy/gate.py` |
| Allowlisted structured logging; third-party exception text never logged | `observability/safe_logging.py` |
| Generic audit events with key-allowlisted, non-PHI metadata | `audit/` |
| Finalize-then-amend record integrity foundation | `revision/` |
| Public route allowlist (`/health` only); no debug or admin endpoint | `apisurface/surface.py` |
| HTTP composition proof over a real socket; stdlib WSGI, no new dependency | `transport/wsgi_app.py` |
| Authorization proven to precede repository access (counting repository) | `tests/test_http_composition.py` |

---

## PRE-PHI INTEGRATION BLOCKERS

Deferred by the HIPAA/security review as class **B** — permitted to follow the
BACKEND 0.2 freeze, but **required before any real PHI**. These are carried
debt, not closed items.

| # | Blocker | Why it is deferrable now |
|---|---|---|
| 1 | **Real Firebase Admin / Identity Platform adapter** — a `TokenDecoder` implementation over `firebase_admin.auth.verify_id_token(..., check_revoked=True)` | The port and all production rules (revocation, verified-email, claim translation, fail-closed selection) exist and are tested. Only the SDK call is missing, and there is no Identity Platform project to call. Production currently resolves to `FailClosedAuthVerifier`, so the gap denies rather than admits. |
| 2 | **Real Firestore adapter + emulator tests** — a `DocumentStore` implementation over `google.cloud.firestore.Client`, exercised against the Firestore emulator | Repositories, codecs, collections and ordering are complete and tested against the port. The adapter is a five-method translation. No production database exists to connect to. |

Both must be implemented, reviewed and tested **before** the first real patient
record. Neither may be satisfied by pointing the pilot at Parent 2.3
infrastructure.

---

## EXTERNAL_GO_LIVE_BLOCKER

None of these can be satisfied by code in this repository. Each must be
verified independently before any real patient data is processed.

### Cloud environment
- `EXTERNAL_GO_LIVE_BLOCKER` — separate `genex-dev` / `genex-prod` GCP projects
- `EXTERNAL_GO_LIVE_BLOCKER` — Google Cloud BAA scope verification
- `EXTERNAL_GO_LIVE_BLOCKER` — dedicated production runtime service account
- `EXTERNAL_GO_LIVE_BLOCKER` — least-privilege IAM
- `EXTERNAL_GO_LIVE_BLOCKER` — Secret Manager production configuration

### Identity
- `EXTERNAL_GO_LIVE_BLOCKER` — Identity Platform enabled in production
- `EXTERNAL_GO_LIVE_BLOCKER` — production verified-email configuration
- `EXTERNAL_GO_LIVE_BLOCKER` — admin/therapist MFA

### Data
- `EXTERNAL_GO_LIVE_BLOCKER` — production Firestore creation and configuration
- `EXTERNAL_GO_LIVE_BLOCKER` — Firestore IAM
- `EXTERNAL_GO_LIVE_BLOCKER` — daily Firestore backups
- `EXTERNAL_GO_LIVE_BLOCKER` — restore test actually performed
- `EXTERNAL_GO_LIVE_BLOCKER` — GCS public-access prevention
- `EXTERNAL_GO_LIVE_BLOCKER` — GCS uniform bucket-level access
- `EXTERNAL_GO_LIVE_BLOCKER` — GCS soft delete / recovery

### Third parties
- `EXTERNAL_GO_LIVE_BLOCKER` — OpenAI BAA / approved PHI configuration.
  Until verified, `aipolicy` denies by default and must stay denied.
- `EXTERNAL_GO_LIVE_BLOCKER` — Lovable PHI-path verification
- `EXTERNAL_GO_LIVE_BLOCKER` — Cloud Logging retention and configuration

### Legal / operational
- `EXTERNAL_GO_LIVE_BLOCKER` — legal / provider BAA
- `EXTERNAL_GO_LIVE_BLOCKER` — provider services agreement
- `EXTERNAL_GO_LIVE_BLOCKER` — caregiver pilot language

---

## NOT_IMPLEMENTED in 0.2 (deliberate)

- A real Firestore client binding, and a real Identity Platform token decoder.
  See PRE-PHI INTEGRATION BLOCKERS above — deferred deliberately, not forgotten.
- A product API. `transport/wsgi_app.py` serves exactly two routes and exists
  to prove the security chain composes over HTTP; it returns no clinical
  content and must not grow product endpoints.
- Any business/clinical workflow, and therefore any clinical record. The audit
  actions and revision machinery exist; nothing emits them yet.
- RTM in every form — no episode, monitoring event, review, clinical action,
  time entry, synchronous interaction, aggregation or CPT logic. Asserted by test.
- Third-party telemetry of any kind. Asserted by test.
- Any administrative role or break-glass path.

---

## Standing invariants

- Authentication identity (`auth_subject`) is never application identity.
- `Child` carries no PHI and has no owner field; ownership lives in connections.
- Relationships end via status and `ended_at`; nothing is ever deleted.
- `BETA_ACCESS_CODE` is not an authorization primitive and is not read by `authz`.
- Frozen Parent 2.4 and Therapist 0.7.4 runtimes are not modified by this phase.

---

## 0.4A — longitudinal identity (added)

| Control | Where |
|---|---|
| Canonical child identity remains `chld_*`; external ids never canonical | `domain/source_link.py` |
| Write-time uniqueness via deterministic claim documents | `domain/identity_claims.py` |
| One ACTIVE link per (child, source_system) | `identity/service.py` |
| One ACTIVE canonical child per (source_system, external_id) | `identity/service.py` |
| Ambiguity fails closed; resolution never picks a winner | `identity/service.py` |
| Exactly one active managing clinician, claim-enforced | `identity/service.py` |
| Managing clinician validated against an ACTIVE ProviderChildConnection | `identity/service.py` |
| Practice of record taken from the connection, not the provider | `identity/service.py` |
| No delete on any identity record or repository | `persistence/firestore_repos.py` |
| Audit excludes the external identifier by construction | `audit/events.py` allowlist |

### Forward architecture notes recorded in 0.4A (NOT implemented)

- **RTM clinician change.** An open RTM episode must NOT silently transfer when
  the managing clinician changes. The episode must be explicitly closed and a
  new one opened under the new clinician. Formal transfer semantics deferred.
- **`RTMTechnology`** must support `regulatory_status = UNDER_REVIEW` and must
  not imply FDA approval, clearance, registration or classification.
- **`PayerVerification`** should capture payer, plan/product if known,
  verification date, method/source, verified_by, status/outcome, and
  notes/reference. It never guarantees reimbursement.
- **`MonitoringDay` remains DEFERRED** pending an explicit, versioned
  clinical/regulatory qualification rule. Only `distinct_observed_local_dates`
  may be computed, and never described as qualifying or billable.
- **Planning policy defaults** approved: primary weight 3, secondary weight 2.
  Relative values, versioned, configurable — never percentages, never in the
  domain schema.
- **Supporting `ActivityGoalAlignment`** may satisfy minimum goal coverage when
  the alignment is meaningful and explicit; repeated all-supporting coverage
  should surface as a quality flag.
- **Parent Save-for-Later** default suppression is one subsequent weekly cycle,
  with clinician override possible later.
