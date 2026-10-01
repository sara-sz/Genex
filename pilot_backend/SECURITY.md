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
| 4 | **Universal `auth_subject` write-time uniqueness** (added 0.5A, PARTIALLY CLOSED) — `AuthSubjectIdentityClaim` makes the CAREGIVER path write-time unique and race-safe, and a bootstrap against a subject an existing provider holds is refused. **Provider creation does not participate.** `providers.create` keys its document on the random `provider_id`, so two providers can share a subject, and a provider can bind a subject a caregiver already claimed. | The caregiver path — the only self-service identity path, and the one real people will use — is closed and emulator-proven. Provider records are created exclusively by fixtures today; there is no provider self-registration endpoint and no provisioning service, so the gap is reachable only by code inside this repository. See the detail section below for why the fix is not small. |
| 3 | **Goal approval must become atomic** (added 0.4B/C) — `GoalService.approve_clinical_goal` and `approve_caregiver_goal` write the `GoalVersion` FIRST, then the goal that names it as `current_version_id`. A crash between the two writes leaves an **orphan `GoalVersion`**. | Founder-reviewed and explicitly accepted for the fictional 0.4B/C freeze. The orphan is INERT: no goal references it, no allocation can name it, no snapshot can reach it, and it is invisible to every read path — `list_chain` is keyed on a `goal_id` that does not exist. The failure mode is a dead row, never a goal whose `current_version_id` points at nothing. **Required before real PHI**, because a clinical record store must not accumulate unreferenced clinical text even when it is unreachable. |

Items 1 and 2 must be implemented, reviewed and tested **before** the first
real patient record. Neither may be satisfied by pointing the pilot at Parent
2.3 infrastructure.

### Blocker 3 — what "make it atomic" will require

Not a reordering. Writing the goal first and the version second only moves the
window: it produces a goal whose `current_version_id` names a document that
does not exist, which is strictly worse than an inert orphan — an unreadable
goal rather than an unreachable version.

Atomicity needs both writes inside one transaction, and that collides with the
same port constraint activation hit: `DocumentStore` REFUSES `set` inside a
transaction (0.4A), so the goal's `current_version_id` cannot be filled in
after the version is created. The likely shape is to mint both identifiers up
front and `create` both documents in a single transaction, since `create` IS
permitted there — the same restructuring `MonthlyPlanService.activate_plan`
performed, rather than any weakening of the port's existence guarantee.

Tracked here rather than in ordinary carried debt because it is a PRE-PHI
hardening requirement, not a preference.

### Blocker 4 — exactly what 0.5A closed, and what it did not

**CLOSED: caregiver self-bootstrap `auth_subject` write-time uniqueness.**
`AuthSubjectIdentityClaim` keys a document on `sha256(auth_subject)[:32]`, and
`bootstrap_caregiver` acquires it in the SAME transaction that creates the
`Caregiver`. Concurrent bootstraps collide on one document, exactly one wins,
and the losers converge on the winner's caregiver. A caregiver that predates the
primitive gains a claim by create-only backfill rather than a duplicate
identity. Proven against the real Firestore emulator, not `FakeDocumentStore`.

**STILL OPEN: universal claim enforcement for Provider creation / provisioning.**

A deterministic-key claim is only a mutex for writers that TAKE it, and the
provider side does not:

| Path | Acquires a claim? | Resulting state |
|---|---|---|
| `providers.create` twice with one subject | No | `AmbiguousAuthSubject` on every later resolution, permanently |
| `providers.create` for a subject a caregiver already claimed | No | `resolve_principal` refuses: "resolves to both a caregiver and a provider record" |
| `Provider.with_auth_subject` | No | **Latent only** — no repository method persists a late binding; `update_status` is the sole provider mutation |

The caregiver bootstrap refuses a subject an existing provider holds, so the
ordering *caregiver-after-provider* IS guarded. The reverse is not.

Why 0.5A does not fix it: enforcement has to move into
`FirestoreProviderRepository.create`, which is frozen 0.1 code, and every
fixture and emulator test that provisions a provider would have to acquire a
claim transactionally. That is a change to provider provisioning, not a small
correctness patch, and 0.5A's own paths are correct without it. Expanding the
slice to cover it was explicitly declined.

Pinned by tests that assert the CURRENT state, so closing the gap breaks them
and forces this section to be updated in the same change:
`test_provider_creation_does_not_acquire_a_subject_claim`,
`test_a_provider_can_still_take_a_subject_a_caregiver_holds`,
`test_no_repository_method_persists_a_late_subject_binding` and
`test_caregiver_creation_is_the_only_claimed_identity_path`.

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
- **Approval writes the goal version before the goal.** Promoted by founder
  review to **PRE-PHI INTEGRATION BLOCKER 3** — see that table above. It does
  not block the fictional 0.4B/C freeze; it must be made atomic before real
  PHI. Listed here too so a reader of the debt section is not left thinking it
  is merely a preference.
- **`list_for_cycle` filters in Python after a single-field query**, because
  the `DocumentStore` port exposes equality on one field and deliberately
  promises no composite index.
- **All earlier pilot, Parent and Therapist carried debt remains.**

---

## 0.4D/E — weekly allocation, evidence and adaptation (added)

### The weekly layer is not the Parent weekly plan

`WeeklyCycle` is the MONTHLY layer's own record of "week N of this focus
plan". It carries no activity list, no schedule and no plan content. The
Parent plan is reached only through `WeeklyPlanLink.external_plan_id`, which
is an EXTERNAL identifier and never canonical — the rule 0.4A set for Parent
session ids, restated. No pilot record is keyed by it and nothing joins on it.

`WeeklyPlanSnapshot` exists because Parent's customization overlay is
UNVERSIONED: a plan the family sees today can read differently tomorrow with
no record of what was replaced. That is fine for a weekly display and unusable
as clinical evidence. The snapshot stores the resolved document as an opaque,
immutable JSON capture — opaque because Parent's plan shape is not ours to
version, and a codec that validated its fields would start failing the moment
Parent changed one. The repository is create-only: a snapshot that could be
rewritten answers nothing.

### Double counting is prevented structurally, not by discipline

    ObservationEvent      is the unit of ATTEMPT COUNT
    ActivityGoalAlignment is the unit of ATTRIBUTION

One activity serving two goals, attempted once, is ONE opportunity: total
attempts 1, goal-A attributed 1, goal-B attributed 1, and `1 + 1 = 2` is a
number that means nothing. `weekly/counting.py` computes totals from DISTINCT
event ids and never consults the per-goal streams; `sum_of_goal_attempts` is
exposed under that deliberately awkward name so nobody reaches for it by
accident, and `CoverageSummary` carries the overlapping event ids explicitly.

A CI step walks the AST of `summarize` and asserts the total is computed
exactly once, from `seen_events` and not from `per_goal`. MonthEndReport is
out of scope until F/G; the arithmetic it will need is proven now rather than
re-derived later under deadline.

### Alignment attaches to the scheduled INSTANCE

Never to the reusable template. The same activity placed in week 1 and week 3
is two opportunities with two attributions and possibly two different goal
sets after a reprioritisation. It is also what makes history immutable: the
alignment repository is create-only, so a future cycle can be re-aligned
without rewriting what a past cycle did.

### Allocation is two-stage and hard-codes no split

Stage 1 fills each goal's `min_coverage_per_cycle` in `priority_rank` order,
preferring the candidate that closes the MOST open floors so a multi-goal
activity is not duplicated per goal. Stage 2 distributes remaining capacity by
the HIGHEST-AVERAGES rule — the next slot goes to the largest
`weight / (placed + 1)`.

That choice is deliberate: percentages would need a rounding rule and would
break the moment a clinician adds a third goal or weights two equally. 3 and 2
are a ratio, not 60/40. Highest-averages handles arbitrary N and arbitrary
positive weights, needs no rounding, and is exactly reproducible. Nothing
hard-codes a goal count or a split.

### A CoverageGap is a planner condition

It records that the planner could not place a meaningful opportunity. The
reasons are a closed enum with no member for non-adherence, child performance
or caregiver behaviour, and the type carries `is_planner_condition` and
`not_a_failure` as constants. A gap is never evidence about a person.

### Save for Later is honoured over the coverage floor

A defer suppresses the activity for the NEXT cycle. After that it is ELIGIBLE
again — eligible is not recommended, and it is never retired.

When a goal's only candidates are suppressed, the allocator writes
`DEFERRED_CONSTRAINT` rather than reaching past the caregiver's signal. The
only route to early reuse is `DeferRecord.with_clinician_override`, which
requires an actor and a stated reason and is reachable only through
`WeeklyService.override_defer` behind the managing-clinician gate. A test
asserts the allocator module never references the override at all, so a
system-only coverage floor cannot defeat a defer.

### Released plans are recorded against, never rewritten

Once `released_to_parent_at` is set and a snapshot exists, a CURRENT_PLAN
intervention that would REPLACE or REMOVE content raises
`ReleasedPlanImmutable`. Endorsement and guidance are still recorded as
intent. FUTURE_CYCLE interventions feed next-cycle generation directly and
need no parent acceptance, because nothing has been shown yet. Real
proposal-and-acceptance wiring is 0.5.

A cycle also cannot be released before its plan is snapshotted: releasing
uncaptured content would leave nothing to compare a later change against.

### Family capacity is finite, including for clinicians

A clinician-added activity consumes capacity like anything else. If that
pushes a released cycle past the declared capacity, `CapacityLedger` records
the overage and the reason; nothing the family already received is removed.
`overage` is DERIVED from the counts it summarises rather than stored, so two
fields in one record cannot describe different weeks.

### Adaptation is deterministic, offline and conservative

    too_hard | wasnt_ready_yet | didnt_want_to_try  -> EASIER_OR_MORE_SUPPORT
    too_easy AND did_it                             -> HARDER_OR_PROGRESSED
    anything else                                   -> MAINTAIN

`MAINTAIN` is the default. Progression requires positive evidence and is never
a fallback: a bare `did_it` progresses nothing, because completion is not
mastery. Support wins over progression for the same activity. `JUST_RIGHT`
maps to no signal at all — unmappable feedback stays unmapped rather than
being invented into a domain-level signal.

No model, no prompt, no network. A CI step walks the transitive import graph
of `pilot_backend/weekly` for a banned set including `requests`, `socket`,
`openai`, `anthropic`, `google` and `grpc`, in a job where no HTTP library or
cloud SDK is installed.

### Clinician decisions stay separable from child performance

`SignalKind` is namespaced by origin — `child_`, `plan_`, `clinician_`,
`parent_declined_` — and every signal stores its `SignalSource`. A
clinician-directed change and a child struggling produce different next weeks
and different conversations, and `AdaptationRecord.has_performance_evidence`
is False when the whole difference is explained by decisions.

`not_a_failure` is invariant on `AdaptationRecord`: constructing one with it
False raises. A parent declining a therapist-proposed change is its own
category and is explicitly not a non-attempt, a difficulty report, an activity
failure or a clinical failure.

### Attribution is by LOCAL date

A cycle may span a month boundary. `owning_cycle_id` says which plan an
attempt came from; `attribution_month` says which month it counts toward, and
it is DERIVED from `local_date` at construction so the two cannot disagree.
The timezone is the `MonthlyFocusPlan`'s timezone of record and is required —
no UTC fallback, because an hour's drift moves a day across a month boundary
and the monthly layer counts days into months.

### Write-time uniqueness, strengthened

`ClaimKind.WEEKLY_CYCLE` on `(focus_plan_id, sequence_in_month)` and
`ClaimKind.WEEKLY_ALLOCATION` on `(cycle_id)`. Both were read-then-write
guards in a first pass — the 0.3 auth-subject defect one layer up — and both
are now claims.

The allocation claim is STRONGER than the 0.4C plan claim: every write it
guards is a `create`, so claim, alignments, gaps and ledger all commit in ONE
transaction. There is no `set` and therefore no boundary to recover across. A
crash persists nothing and consumes no generation. The transactional-set
prohibition is untouched.

### 0.4D/E carried debt

- **Candidate activities are supplied by the caller.** 0.4D/E takes a
  fictional candidate list; there is no activity catalogue, no suitability
  model and no Parent activity integration. Which activities exist, and which
  genuinely support which goal, is 0.5 work.
- **`CandidateActivity.supports` is trusted.** The allocator treats the
  supplied goal set as a reviewed statement of meaningful support. Nothing
  here validates that an activity really serves a goal — that judgement is
  clinical and belongs to the alignment source, which is recorded.
- **Per-cycle reads are single-field queries filtered in Python**, because the
  `DocumentStore` port exposes equality on one field and promises no composite
  index. Unchanged from 0.4B/C and acceptable at pilot volume.
- **`WeeklyPlanSnapshot.resolved_plan_document` is opaque.** Deliberate, but
  it means the pilot cannot detect a MEANINGFUL change inside a captured
  document — only that two captures differ.
- **No RTM, month-end or coding object exists**, and a scope test asserts it.
- **All earlier pilot, Parent and Therapist carried debt remains**, including
  PRE-PHI INTEGRATION BLOCKER 3 (atomic goal approval), which 0.4D/E does not
  touch.
