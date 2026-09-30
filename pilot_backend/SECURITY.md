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
| Claims AND record commit in ONE transaction — crash-consistent | `persistence/document_store.py`, `pilot_runtime/persistence/firestore_store.py` |
| Raw external-id resolver is private; only an authorized wrapper is public | `identity/service.py` |

### Approved product decision — idempotent exact repeat

An exact-repeat `SourceSystemLink` request returns the existing active link
rather than raising. Founder-approved as intentional, and **not** a uniqueness
weakening, because it holds only when:

- it resolves to the exact same active canonical mapping (same child, same
  source system, same external identity);
- it cannot change `child_id`, `source_system` or external-identity ownership;
- it cannot bypass authorization — the child-access gate runs first, every time;
- any *different* competing mapping still fails closed;
- the behaviour stays test-covered
  (`test_an_exact_repeat_is_idempotent_not_a_conflict`).

A retry is not an ambiguity. Anything that is not a byte-identical repeat is
refused.

### Crash consistency

Uniqueness claims and the authoritative record are written in a single
Firestore transaction, so a process death between them persists nothing.
Verified by fault injection against the real emulator
(`test_a_crash_before_the_record_write_persists_nothing_in_firestore`).

Two constraints are now part of the `DocumentStore` port rather than of one
implementation: all reads must precede all writes, and `set` is refused inside
a transaction because its existence guarantee would require a read after a
write. Generation counters are therefore read OUTSIDE the transaction — a
stale generation can only cause a collision, which is the refusal we want, and
keeping queries out avoids read-lock contention (measured: 194s → 2.3s for the
identity emulator suite).

### External-identity resolution

`_resolve_child_for_external` is private. The only public path is
`resolve_authorized_child(principal, …)`, which resolves and authorizes as one
operation and cannot return a child id without the gate. Unknown and
unauthorized external identities produce the SAME error, so the method cannot
become an oracle for which Parent sessions are bound to which children. A
structural test asserts every public service method takes `principal` as its
first argument.

### Forward architecture notes recorded in 0.4A (NOT implemented)

- **RTM clinician change.** An open RTM episode must NOT silently transfer when
  the managing clinician changes. The episode must be explicitly closed and a
  new one opened under the new clinician. Formal transfer semantics deferred.
- **`RTMTechnology`** must support `regulatory_status = UNDER_REVIEW` and must
  not imply FDA approval, clearance, registration or classification. No device
  eligibility conclusion is implied.

- **Updated October RTM overlay** (carry-forward): `ClinicalGoal` →
  `RTMEpisode` → `RTMMonitoringPeriod`, referencing `MonthlyFocusPlan` and
  `ObservationEvent`s, with `TherapistReview` → `ClinicalAction`, `TimeEntry`,
  `SynchronousInteraction`, `RTMTechnology`, `RTMEvidenceSummary` and
  `CodingAssistanceSummary`. **`PayerVerification` removed.** `MonitoringDay`
  remains deferred.
- **`PayerVerification` is REMOVED from the October RTM scope.** Founder
  decision, intentional data minimisation. For the October pilot Genex will
  not collect or store insurance/member information, verify benefits,
  determine coverage, perform eligibility checks, call payer APIs, submit
  claims, integrate with a clearinghouse or an EMR for billing, determine
  reimbursement amounts, or guarantee payment. No payer, member, plan,
  eligibility, claim or reimbursement field is to be added.

- **`CodingAssistanceSummary` is carried forward to a later slice** (not
  0.4A, not 0.4F-as-previously-scoped). Narrow, deterministic, rules-based,
  versioned, explainable, regenerable, clinician-confirmed, and **never
  LLM-decided**. Limited to RTM treatment-management code candidates
  **98979 / 98980 / 98981**; device-supply RTM codes are excluded while the
  technology/device regulatory question is unresolved. Factual inputs only:
  calendar month, manually entered treatment-management minutes, documented
  synchronous interactions and their modality/date, documentation
  completeness, and therapist review/actions. Never infer undocumented time,
  never treat asynchronous messaging as synchronous, never assume payer
  behaviour. Output wording: "Potential CPT code candidate based on
  Genex-documented evidence — clinician confirmation required." Never
  "billable", "claim approved", "eligible for reimbursement" or "guaranteed
  reimbursement". The treating SLP/practice remains responsible for RTM
  appropriateness, medical necessity, coding requirements, final CPT
  selection, modifiers, payer/benefit verification, billing, claim submission
  and payer follow-up. No reimbursement-dollar calculation is planned.
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

### 0.4A carried debt

- **`FakeDocumentStore` is not thread-safe** — accepted as documented debt by
  founder decision. Its transaction support is snapshot-and-rollback under a
  lock, which gives all-or-nothing for fault-injection tests but simulates no
  contention. **It must never be used to make a concurrency claim**; the only
  evidence about racing writers comes from the real emulator suite.
- **Transactional `set` is unavailable** (Firestore forbids a read after a
  write). Any later slice needing a read-modify-write inside a transaction
  must restructure rather than weaken the port's existence guarantee.
- **Generation counters are read outside the transaction.** Correct — a stale
  read can only cause a collision, which is the intended refusal — but it
  means a heavily contended key does one extra read per attempt.

## 0.4B/C — goal layer and monthly focus plan (added)

### Goals

- **Three concepts, three types.** `GoalSuggestion` (Genex-authored candidate),
  `ClinicalGoal` (clinician-approved, RTM-eligible) and
  `CaregiverApprovedGoal` (caregiver-approved, never RTM-eligible) are separate
  classes in separate collections. A single type with an `approved_by_role`
  flag would work until one function forgot to check it, at which point a
  caregiver-approved goal becomes clinical evidence silently.
  `require_clinical_goal_ref` is the structural gate; `_authorize_for_ref` is
  the behavioural one.
- **Genex suggests; a human approves.** `generate_suggestions` writes
  suggestions and nothing else. No goal exists without a recorded human action
  naming the suggestion it came from, or declaring `AUTHORED_FRESH`.
- **Only the ACTIVE managing clinician may author, revise or retire a
  `ClinicalGoal`.** Being connected to the child is not sufficient, and this
  is tested with a provider who genuinely passes the 0.2 access gate — an
  earlier version of the test used an unconnected provider and was vacuous.
- **Only the approving caregiver may edit their own goal**, likewise tested
  with a second caregiver holding a real ACTIVE connection.
- **Versions are immutable and append-only.** A wording change writes a new
  `GoalVersion` with a mandatory reason and a `supersedes_version_id`. Nothing
  deletes; `set_goal_status` stamps PAUSED or RETIRED and keeps the row.
- **A suggestion leaves OFFERED exactly once.** A repeated approval is a
  conflict, not a second goal.
- **Accepting verbatim stores the suggestion's OWN template**, not text the
  caller echoed back — otherwise "accepted verbatim" is a claim the record
  cannot support.

### The suggestion engine is deterministic and offline

- **No model, no network.** No client is imported, and two CI gates assert it:
  an AST scan of the engine's imports, and a transitive scan of every module
  under `goals/`, `planning/` and `domain/`. Both run in the DEPENDENCY-PURE
  job, where no HTTP library or cloud SDK is installed at all.
- **Wording is chosen from a fixed catalogue, never composed from input.** No
  observation, area name, level or reference can reach the stored text.
- **Stored suggestion text is name-blind.** Every template carries `{child}`;
  substitution happens at presentation time in whichever system actually holds
  a name. `pilot_backend` has held none since 0.1.
- **Parent invariants are enforced structurally, not by convention:**
  chronological age is not a parameter of the engine at all; `EvidenceSource`
  has no diagnosis member, so a diagnosis has no representable slot;
  `answered=False` is skipped rather than defaulted to a level or an age; and
  Sensory evidence is never invented — the engine consumes supplied
  observations and fabricates nothing.
- **`generation_mode` is stamped `"deterministic"`** on every suggestion, so a
  future LLM-assisted WORDING mode would be a visible, auditable difference in
  the stored record rather than a silent change of meaning.

### Monthly focus plan

- **Direction, not content.** A `MonthlyFocusPlan` holds no activity list;
  weekly planning stays adaptive and does not exist in this slice.
- **Emphasis defaults are POLICY, not schema.** `PlanningPolicyVersion`
  records primary 3 / secondary 2 / minimum coverage 1 / two goals by default.
  Nothing caps the goal count and nothing requires 3-and-2. Weights are
  RELATIVE, never percentages. Every plan stores the policy version it was
  built with, and an unknown version is refused rather than silently replaced
  by today's defaults.
- **One active plan per (child, cycle_month)**, enforced at write time by
  `ClaimKind.MONTHLY_FOCUS_PLAN` — the same mechanism as 0.4A, proven under
  eight racing threads on the real emulator.
- **Allocations are append-only.** Reprioritising writes a successor with
  `effective_from_cycle` and `supersedes_allocation_id`; the predecessor is
  retained, so which weighting was in force during week 2 stays answerable.
  Both `effective_from_cycle` and `reason` are required with no default.
- **Snapshots freeze the wording at activation.** A November edit cannot reach
  October's record, because October's record is a copy rather than a pointer.
- **A timezone of record is required and validated. There is no UTC
  fallback.** Parent falls back to UTC on an invalid zone, which is acceptable
  for a weekly display and is not acceptable here: a silent hour shift moves a
  day across a month boundary, and this is the layer that counts days into
  months.
- **Closing does not release the claim.** A finished month cannot be reopened
  and rewritten.

### Activation is two steps, and why that is still safe

The port refuses `set` inside a transaction — a deliberate 0.4A constraint,
and the 0.4A note is explicit that a slice hitting it must RESTRUCTURE rather
than relax the port. Activation needs a read-modify-write of an existing plan,
so it is:

    1. transaction: create the uniqueness claim AND every goal snapshot
    2. outside:     set the plan ACTIVE, stamping the claim id

Step 1 is all-or-nothing. A crash between 1 and 2 leaves a claim whose
`holder_ref` is this plan's id; a retry recognises it already holds its own
claim, skips step 1 — so no duplicate snapshots — and completes step 2. A
DIFFERENT plan retrying collides and is refused. Recovery is idempotent for
the rightful holder and a hard refusal for everyone else. Both paths are
covered by fault injection against the real emulator.

### Audit

Eleven actions and sixteen metadata keys added, all opaque ids, short enums or
small integers. Deliberately EXCLUDED and asserted absent: goal text, the
family-facing template, an edit reason, milestone references, observed level,
functional baseline area, and `domain_key` — which developmental domain a
child's goal addresses is a clinical fact, not an operational one. The 0.2
allowlist guard was extended by hand, as in 0.4A, rather than loosened.

### Test harness defect found and fixed in 0.4B/C

The emulator session fixture started the emulator with `stdout=subprocess.PIPE`
and never read the pipe. The emulator logs a line per HTTP/2 connection and a
92-test run produces roughly 150 KB — more than twice a 64 KB pipe buffer.
Once it filled, the emulator blocked in `write()` and stopped serving: client
threads hung inside gRPC and the ten-way raw-claim race reported nine losers
instead of ten. **A harness deadlock presenting as a uniqueness failure.** It
was latent under 79 emulator tests and surfaced at 92. Output now goes to a
file, which preserves the startup diagnostic that identified the Java 21
requirement in 0.3, and both `_race` helpers now fail loudly on a thread that
outlives its join instead of returning a short list.

### 0.4B/C carried debt

- **Clinician-authored goal text is free text.** Genex-GENERATED wording never
  contains a name and that is enforced; a clinician typing a child's name into
  a goal they wrote is clinical free text the pilot does not and cannot
  prevent. Recorded rather than pretended away.
- **`goal_vocabulary.py` MIRRORS `parent_taxonomy.domains`** rather than
  importing it: they are separate top-level namespaces and the pilot CI job
  runs from the repository root. `test_canonical_domains_mirror_parent_taxonomy`
  pins the exact seven keys, so a divergence surfaces there.
- **Approval writes the goal version before the goal.** A crash between them
  leaves an orphan version, which is inert. Making the pair atomic needs a
  transaction, and the goal's `current_version_id` cannot be set without a
  read-modify-write the port forbids inside one — the same restructuring
  question as activation, deferred rather than papered over.
- **`list_for_cycle` filters in Python after a single-field query**, because
  the `DocumentStore` port exposes equality on one field and deliberately
  promises no composite index.
- **All earlier pilot, Parent and Therapist carried debt remains.**
