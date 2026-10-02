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
| ~~4~~ | **Universal `auth_subject` write-time uniqueness** — **CLOSED in 0.5B.** `ProviderProvisioningService` commits the `AuthSubjectIdentityClaim` and the `Provider` in ONE transaction on the same deterministic key, and `providers.create` is called from nowhere else in the deployed codebase. | Closed and emulator-proven in both orderings. Retained in this table struck through rather than deleted, so the lineage of what was open and when stays readable. See the detail section below. |
| ~~3~~ | **Goal approval must become atomic** — **CLOSED in 0.5C.** `approve_clinical_goal` and `approve_caregiver_goal` commit the goal and its first `GoalVersion` in ONE transaction via `_commit_goal_with_version`, so neither exists unless both do. | Closed and emulator-proven under fault injection at both ends of the transaction, plus eight-way concurrency and a crash among concurrent writers. Struck through rather than deleted so the lineage of what was open, and when, stays readable. |

| 5 | **`plan_snapshot.source_document` verbatim pass-through is NOT approved as a real-PHI projection** — opened in 0.5D. Approved for FICTIONAL STAGING / browser-demo use only. See the detail section below. | Bounded today by the only thing that can reach it: staging holds fictional data exclusively, and no Parent system is wired to it — `PILOT_PARENT_SESSION_BUCKET` is unset and the deployment entrypoint refuses to start if it is set — so no real plan document can enter the pass-through. |

Items 1, 2 and 5 must be resolved, reviewed and tested **before** the first
real patient record. Neither 1 nor 2 may be satisfied by pointing the pilot at
Parent 2.3 infrastructure.

### Blocker 5 — OPENED in 0.5D: minimum-necessary review of the pass-through

**Status: approved for FICTIONAL STAGING and browser-demo use only. NOT
approved as a real-PHI production projection. Remains a PRE-PHI review item.**

`GET /pilot/children/{child_id}/current-cycle` returns
`plan_snapshot.source_document`: the captured parent-facing weekly plan, handed
back parsed and **verbatim**, with no schema imposed by this layer.

#### Why it was built this way

`WeeklyPlanSnapshot.resolved_plan_document` is a JSON string because it is an
opaque capture of another system's document, and the frozen docstring states
the reason: "Parent's plan shape is not ours to version, and a codec that
validated its fields would start failing the moment Parent changed one."

Nothing in the pilot persists an activity title, instruction, domain,
material, routine or per-activity date — a fact 0.5D established by
inspection. So a Parent weekly UI cannot be built without either passing the
document through or inventing a pilot-owned schema that contradicts that
sentence. The pass-through was the founder-approved choice, and the payload
carries `schema: "opaque_source_document"` and `is_canonical: false` so no
client can acquire the shape by accident and come to depend on it.

#### Why that is not yet a PHI-safe projection

This is the one read model in the pilot where **this layer cannot state what
it is disclosing.** Every other projection names its fields, so "minimum
necessary" is reviewable by reading the serialiser. Here the field set is
whatever the producing system put in the document, which means:

- the disclosed set cannot be enumerated at review time, only at runtime;
- a future Parent change could introduce a field — a free-text caregiver note,
  a clinician remark, an extra identifier — that this endpoint would forward
  silently, with no code change on this side and no test failing;
- `WeeklyPlanSnapshot.VISIBILITY` is `PARENT_VISIBLE` on the stated grounds
  that "the family already has this content". That is sound for the CAREGIVER
  who authored it and is **not** an argument for any other reader.

The frozen no-free-text guarantee is therefore narrower than it looks. 0.5C's
structural gate pins `_observation_payload` to exactly eight structured fields
so no Parent free text can leave the server *through the observation route*.
That gate says nothing about this one, and a free-text field arriving inside
`source_document` would bypass it entirely.

#### What must happen before real PHI

1. Decide whether the Parent weekly plan document is PHI in this context, and
   on what basis a provider may read a caregiver-authored plan.
2. Replace the pass-through with an **explicit allowlist** of the fields the
   UI actually renders, applied on this side, so the disclosed set is
   enumerable at review time — a deny-unknown-fields posture mirroring
   `read_json_body`'s treatment of request bodies.
3. Add a structural gate for the RESPONSE direction equivalent to the one
   0.5C added for the request direction.
4. Re-run the minimum-necessary review against that allowlist.

Until all four are done, this endpoint must not serve a real plan document.

### Blocker 3 — CLOSED in 0.5C

0.4B/C minted the goal id, built the first `GoalVersion` pointing at it, then
issued TWO separate writes ordered version-first. A crash between them left an
orphan `GoalVersion`. That was founder-reviewed and accepted for the fictional
freeze — the orphan is inert, unreachable from every read path, and the failure
mode is a dead row rather than a goal whose `current_version_id` names nothing.
The standing objection was narrower and still correct: a clinical record store
must not accumulate unreferenced clinical text even when nothing can reach it.

Both writes now commit in one transaction.

Pre-minting was already in place and is what makes a single transaction
possible at all: the version must name the goal and the goal must name the
version, so neither can be written first unless both identifiers exist
beforehand. The ordering INSIDE the transaction is therefore no longer
load-bearing and is retained only as documentation of the dependency.

Two creates and nothing else, so there is no read-after-write, no `set`, and no
boundary to recover across — the same shape as 0.4D/E weekly allocation. No
claim is acquired, because uniqueness is not the property at stake: a goal id
is freshly minted and cannot collide, and a child may legitimately hold many
goals. What was missing was ATOMICITY between two mutually-referencing records.

Immutable `GoalVersion` semantics are preserved exactly. The version is still
appended by a create on its own id, so it can never be rewritten; a transaction
changes when it becomes visible, not whether it can change afterwards. A
revision still APPENDS version 2 and leaves version 1 byte-identical.

No destructive repair exists anywhere: nothing deletes, merges or rewrites an
orphan from before this change. Any that exist in a pre-0.5C store remain inert
and visible.

Proven against the REAL Firestore emulator in
`pilot_runtime/tests/integration/test_goal_atomicity_emulator.py`:

| Scenario | Result |
|---|---|
| clean approval | goal and version both exist, version_number 1, goal names it |
| crash on the GOAL write, version already queued | neither record persists; clean retry succeeds |
| crash on the VERSION write | neither record persists |
| caregiver-approved path, crash on the goal write | neither record persists |
| eight concurrent approvals | eight goals, one version each, no orphan |
| crash among eight concurrent approvals | survivors intact, the dead writer strands nothing |
| immutability | version 1 unchanged after a revision; re-append refused |
| authorization unchanged | ACTIVE connection without the managing-clinician assignment is still refused |

The suite was verified non-vacuous by restoring the 0.4B/C two-write shape:
exactly the three orphan-detecting fault-injection tests fail, and the other
five are correctly unaffected.

### Blocker 4 — CLOSED in 0.5B

**0.5A closed the caregiver half.** `AuthSubjectIdentityClaim` keys a document
on `sha256(auth_subject)[:32]`, and `bootstrap_caregiver` acquires it in the
SAME transaction that creates the `Caregiver`.

**0.5B closes the provider half.** `pilot_backend/provisioning/` commits the
claim and the `Provider` in one transaction on that same key, so concurrent
provisions of one subject collide on one document and exactly one survives.

A deterministic claim is only a mutex for writers that TAKE it, so the
guarantee is not "provisioning acquires the claim" — it is **nothing else
creates a Provider**. Two structural CI gates enforce that, and both were
validated by injecting the bypass they exist to catch:

  * `providers.create` appears only in `provisioning/service.py`, plus two
    allowlisted fixture call sites;
  * nothing reachable from the composition root imports
    `pilot_backend.fixtures` — checked by walking the import graph, not just
    the root module.

Proven against the real Firestore emulator:

| Ordering | Outcome |
|---|---|
| eight concurrent provisions of one subject | one claim, one `Provider`, all eight callers return the same `provider_id` |
| provider provisioned, then caregiver bootstrap | `SubjectAlreadyHeld`, nothing written |
| caregiver bootstrapped, then provider provisioned | `SubjectAlreadyHeld`, nothing written |
| eight threads racing caregiver-vs-provider on one subject | exactly one actor KIND is created; every loser fails closed |
| repeat provision of the same subject | converges on the existing `Provider`, writes nothing |

Five refusals are pinned by test, each found or confirmed by mutation testing:
a caregiver-held subject with no claim, a caregiver-held claim with no
caregiver record, a claim naming a different provider than the record the
subject resolves to, an absent or inactive practice, and a legacy provider
which is ADOPTED by claim backfill rather than twinned. None is repaired by
guessing.

**The remaining scope limit, stated precisely.** Two fixture call sites still
create `Provider` records directly, so a fixture-seeded provider holds no
claim until provisioning touches its subject, at which point the
legacy-backfill branch mints one. This is bounded and test-only:

  * `fixtures/secure_topology.py` is backend-parametrised and also builds
    against frozen `InMemoryRepositories`, which has no claim collection and
    no transaction support — it predates the primitive by four slices;
  * `fixtures/pilot_topology.py` is the BACKEND 0.1 in-memory demo topology.

Routing them through provisioning was attempted and REVERTED: it would require
adding a claim repository and transactions to frozen BACKEND 0.1 code. Making
`provision_provider_record` tolerate a missing claim collection was rejected
outright — a provisioning path that proceeds without its mutex is exactly the
bypass this module removes. Neither fixture is reachable from the deployed
composition, which is what the second gate above asserts.

**There is no provisioning HTTP route, deliberately.** Creating a Provider is
an administrative act performed on someone else's behalf, and `ActorRole`
contains only CAREGIVER and PROVIDER — there is no principal that could
authorize one. An endpoint would therefore have to invent an admin role or
authorize nobody, and the second IS public provider self-registration. Hannah
is provisioned operationally through `ProviderProvisioningService`. A CI gate
asserts no route of that shape, and no discovery, directory or
self-registration shape, exists.

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

---

## 0.5B — provider identity and the connection lifecycle (added)

### Scope, and what is deliberately absent

One direction only: a caregiver invites a clinician they already know, and the
clinician accepts or declines. DEFERRED, and each one a surface that discloses
which families or clinicians exist: provider-to-family invitation and
redemption, family search, a provider directory or marketplace, email
invitation infrastructure, and public provider self-registration. A CI gate
asserts no route of any of those shapes exists.

### How a caregiver names a clinician without being able to enumerate them

The caregiver supplies the opaque `provider_id`, obtained out of band. There is
no lookup route, no search and no listing, so the id is the entire addressing
contract.

Knowing the id grants nothing. It permits only OFFERING a connection, which
creates a PENDING row conferring no access, and the clinician must then accept.
A guessed id cannot produce access; at most it produces an invitation somebody
has to agree to. Absent, retired and inactive-practice ids raise the SAME
error with the SAME response body, so the endpoint cannot confirm that an id
exists.

### One live connection per (provider, child)

`ClaimKind.PROVIDER_CONNECTION`, keyed on `(provider_id, child_id)` and held
while the connection is PENDING, ACTIVE or PAUSED. A caregiver double-tapping
"connect" produces two writers computing one key, so they collide on one
document and exactly one survives — a read-then-write guard would be right
almost always and wrong exactly when it matters. Declining, revoking and
ending RELEASE the key; pausing keeps it.

### Only ACTIVE authorizes

`authorize_child_access` requires `is_active`, which means status ACTIVE *and*
`ended_at` unset. PENDING, DECLINED, PAUSED, REVOKED and ENDED therefore all
deny with no change to the authz package at all — which is why DECLINED and
PAUSED were added as STATUSES rather than as flags.

DECLINED is terminal and is not ENDED: ending means a relationship existed,
declining means a family refused one that never started. PAUSED is the only
non-terminal non-active state, and it preserves `activated_at` so a resume
restores the SAME row — which matters structurally, because
`ManagingClinicianAssignment.provider_connection_id` points at it.

### The stale-assignment defect, and the atomic cascade that fixes it

Found by inspection of frozen 0.4A code during 0.5B. Revoking a connection
left its `ManagingClinicianAssignment` ACTIVE. It granted nothing at the time,
because every clinical write path calls `authorize_child_access` BEFORE
`_require_managing_clinician` — verified at every call site in goals, rtm and
weekly. But the assignment is keyed on the CHILD, not the connection, so:

    revoke connection          -> assignment stays ACTIVE
    invite the same provider   -> new ACTIVE connection
    -> authorize_child_access passes, the STALE assignment names this
       provider, and managing-clinician status is restored with no explicit
       assignment, citing a connection that was revoked

Pausing, revoking or ending now ends any ACTIVE assignment for that
(provider, child) and releases its claim IN THE SAME TRANSACTION. Acceptance
never assigns a managing clinician; a resume restores access but NOT ownership,
because "you still clinically own this child" is not something to restore
silently after an interruption of unknown length.

### Three Firestore rules the real emulator enforced

Every unit test passed through all three. `FakeDocumentStore` permits them all.

1. `set()` is unavailable inside a transaction, because the repository
   verifies existence with a read and Firestore forbids a read after a write.
   `overwrite` was added to the port and both adapters: a blind
   whole-document write, the only one usable transactionally.
2. All reads must precede all writes.
3. **Conflict detection covers only documents THIS transaction read.** An
   assignment lookup performed before the transaction opened was atomic and
   still lost the race: a concurrent `assign_managing_clinician` was invisible,
   so the revoke committed and left the new assignment ACTIVE.

The third is the one worth remembering: atomicity and isolation are different
properties, and a transaction can write atomically while losing a race it never
read. The fix is symmetric — revoke QUERIES assignments inside its transaction,
and assign RE-READS the connection inside its own, so whichever commits second
retries and then observes the other.

### The invariant, proven by forced interleaving

    No execution ordering leaves an ACTIVE ManagingClinicianAssignment whose
    required ProviderChildConnection is no longer ACTIVE.

`test_connection_race_determinism.py` decides the ordering rather than racing
for it: a one-shot barrier injected at the transaction boundary through the
service's own `repos_factory` seam. Production contains no sleeps and no test
mode, asserted over the AST of `pilot_backend`.

### RTM invariant preserved

`RTMEpisode` pins `managing_provider_id` at open time and
`_require_episode_owner` refuses writes once the active assignment names
someone else. Ending an assignment therefore cannot silently transfer an open
episode: with none active the episode fails closed, and a later assignee is
refused by name. Nothing in 0.5B touches an episode.

### 0.5B carried debt

`InMemoryRepositories` still has no `auth_subject_claims` collection, which is
why two fixture call sites create `Provider` records without a claim. Bounded
and test-only — see the Blocker 4 section above.
