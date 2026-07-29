# Therapist API — Idempotent Modify-Activity Proposal (Phase 1B.2A)

**Fictional, in-memory development behavior. No Firestore transaction exists
yet** (atomic critical section via `repo.run_in_transaction`, which now also
rolls back all writes on any exception). **No frontend is connected. No real
therapist, parent, or child data is used. Parent acceptance/decline is NOT
implemented in this phase** — the proposal enters `pending_parent_acceptance`
and the original assignment remains the active family activity.

## Endpoint

```
POST /api/v1/children/{child_id}/weekly-plan/assignments/{assignment_id}/proposals/modify
Headers: Idempotency-Key: <client-generated-key>   (required)
Body:    { expected_assignment_version, activity{…}, change_reason, save_scope }
```

`activity` carries the full modified payload (title, `developmental_domain`
snake key, `milestone_id`, skill_focus, duration_minutes, difficulty, materials[],
materials_type, setup, parent_instructions[], what_to_say[], how_to_help[],
success_signals[], variations[], routine_tags[], theme_tags[],
safety_risk_flags[]). **No chronological age range** — activities match by
milestone id + developmental domain only.

### Success (200) — abbreviated

```json
{
  "proposal": {
    "proposal_id": "prop_…", "proposal_type": "modify",
    "proposal_status": "pending_parent_acceptance",
    "child_id": "child_noah", "weekly_plan_id": "wp_noah",
    "current_assignment_id": "assign_noah_turntake",
    "original_activity_template_id": "tmpl_turn_taking_ball",
    "original_activity_version_id": "ver_turn_taking_v1",
    "proposed_activity_version_id": "ver_…", "created_by_user_id": "ther_hannah",
    "created_at": "…", "version": 1
  },
  "proposed_activity_version": { "…": "immutable derived version" },
  "current_assignment": { "…": "original", "plan_approval_status": "approved",
                          "pending_proposal_id": "prop_…", "version": 2 },
  "child_summary": { "child_id": "child_noah", "plan_review_count": 0,
                     "pending_proposal_count": 1 },
  "audit_event_id": "aud_…", "idempotent_replay": false
}
```

An exact replay (same key + same operation + same body) returns the same logical
result with `"idempotent_replay": true` — no second proposal, activity version,
or audit event, and the assignment version is not incremented again.

## Core product behavior (before parent acceptance)

- Original assignment stays **current + active**; `plan_approval_status`
  **unchanged** (e.g. `approved` stays `approved` — never reset to
  `needs_plan_review`; the pending proposal is **not** a plan-review item, so
  `plan_review_count` does not increase).
- Original activity **template and version are unchanged** (never mutated in
  place). The proposed modification is a **separate immutable ActivityVersion**.
- The `PlanChangeProposal` references **both** the original and proposed versions.
- The current assignment gains `pending_proposal_id`; **no** replacement,
  archival, retirement, or second current assignment occurs. The family keeps
  seeing the original activity.

## Derived ActivityVersion provenance

Immutable, `is_derived=true`, `created_by_type=therapist`, with
`created_by_user_id/display_name`, `original_activity_template_id`,
`original_activity_version_id`, `modified_by_user_id/display_name`, `save_scope`,
`created_at`, plus the full activity payload. The derived version links to the
same canonical template. Milestone is validated to exist and to match the
selected developmental domain.

## Derived-version visibility (`save_scope`)

`save_scope` is **enforced**, not merely recorded. Sharing a canonical
`activity_template_id` never grants visibility: a derived version is filtered out
of the generic activity-template / version-history endpoints unless the rule for
its scope is satisfied. **No `save_scope` publishes globally** — there is no
public, marketplace, or shared-library visibility in this phase.

| `save_scope` | Visible in the generic catalog to |
|---|---|
| `child_only` | the creating therapist **and** only while they hold an **active** connection to the associated child |
| `therapist_library` | the owning therapist only (`created_by_user_id`) |
| `submitted_for_genex_review` | the submitting therapist only — private submission metadata; no Genex-review role or publication path exists yet |

Ownership alone is never sufficient for `child_only`. Because `ActivityVersion`
carries no `child_id`, the child association is resolved through the canonical
relationships that do — the `PlanChangeProposal.proposed_activity_version_id`
that proposed it, else a `PlanAssignment.activity_version_id` already pointing at
it. **If no child association can be resolved, the version is hidden**, including
from its creator.

Consequently a `child_only` derived version is **not** visible through the
generic endpoints to: an unconnected therapist; a therapist connected only to a
different child; a therapist whose connection to the relevant child is pending,
paused, or ended; a parent principal (403 on these routes, unchanged); or any
other unauthorized principal. Filtering is applied in the service layer, so a
hidden version is never loaded into any reachable view, and the response gives no
count, placeholder, or id revealing that rows were withheld.

The creating therapist continues to reach the version through the **authorized
child routes** — the weekly plan's `pending_proposal` summary and
`GET /children/{id}/proposals/{proposal_id}` — while the active connection lasts.
Canonical (non-derived) Genex versions are unaffected and remain visible to every
authorized therapist.

## Authorization matrix

| Principal / connection / state | Result |
|---|---|
| Therapist, active connection, **approved** current assignment, no pending proposal, version matches | create proposal (200) |
| Parent principal | 403 `forbidden` |
| Unconnected therapist / pending / paused / ended | 404 (existence-blind) |
| Unknown child / assignment / child mismatch | 404 (existence-blind) |
| Assignment `needs_plan_review` / `replaced` / `archived` / not current-plan | 409 `invalid_modify_proposal_transition` |
| Assignment already has a pending proposal | 409 `pending_proposal_exists` |
| `expected_assignment_version` mismatch | 409 `assignment_version_conflict` (no side effects) |
| Unknown milestone / milestone-domain mismatch | 422 `milestone_domain_mismatch` |

## Optimistic concurrency & atomicity

Success requires `expected_assignment_version == current`, then (in one atomic
critical section): create exactly one immutable ActivityVersion + one
PlanChangeProposal + one AuditEvent, set `pending_proposal_id`, increment the
assignment version **once**, update `updated_at`, leave `plan_approval_status`
unchanged, and store one idempotency result. **On any failure, all collections
roll back** (in-memory snapshot/restore; maps to a future Firestore transaction).
A version mismatch creates no version/proposal/audit/idempotency record.

## Idempotency

Reuses the `IdempotencyRecord` abstraction: binds key hash, actor, action, child,
assignment, canonical request hash, resulting proposal/activity-version/audit ids,
status, created_at. Same key + same request → replay; same key + any difference →
409 `idempotency_key_conflict`; missing/blank key → 400 `missing_idempotency_key`;
failed authz/validation → no success record. Raw key never stored (hash only).

### Concurrent requests

The idempotency check runs **before** the pending-proposal guard inside the
critical section, which decides how simultaneous requests resolve:

- **Same key**, same actor/endpoint/child/assignment/body: exactly one request
  executes; every concurrent duplicate **replays the original result** with
  `idempotent_replay=true`, referencing the same `proposal_id` and
  `proposed_activity_version_id`. A concurrent duplicate never returns
  `pending_proposal_exists`.
- **Different keys** targeting the same assignment: exactly one request creates
  the pending proposal; the others **conflict** with 409
  `pending_proposal_exists` once it is attached.

Either way exactly one proposal, one derived activity version, one audit event,
and one assignment-version increment result. Both cases are covered by
barrier-based tests that force the race rather than relying on timing.

### Deterministic id generation — tracked consideration

Derived proposal/version document ids are seeded from the canonical request hash,
which **excludes** the raw idempotency key. Two different keys carrying a
byte-identical request would therefore map to the same document id. This is
currently unreachable: `expected_assignment_version` is part of the hash and
increments on every successful write, so a genuine second proposal always hashes
differently. **Revisit when parent accept/decline lands** — that flow clears
`pending_proposal_id` and so removes the guard that makes a repeat request
reachable at the same assignment version. Not redesigned in this phase. The raw
idempotency key is never stored anywhere, only its hash.

## Audit event

Exactly one immutable `AuditEvent` `event_type=plan_change_proposal_created` with
proposal/type, actor, therapist, child, weekly_plan, assignment,
`idempotency_key_hash`, `occurred_at`, `request_id`. No raw tokens/keys/secrets.
Append-only; no public audit endpoint (the structured state below is internal and
is not exposed through any API path).

### Structured before/after assignment state

`before_state` and `after_state` are **structured JSON mappings, not status
strings** (see `app/domain/audit_state.py`). Every plan-assignment write records
the same canonical assignment shape on both sides, so the two are comparable
key-by-key and the event alone shows exactly what changed:

```
assignment_version, pending_proposal_id, plan_approval_status,
assignment_status, current_activity_version_id
```

For `plan_change_proposal_created` the after side additionally carries
`proposed_activity_version_id`, `proposal_id`, and
`proposal_status = pending_parent_acceptance` — facts that did not exist before
the write:

```json
"before_state": {
  "assignment_version": 1, "pending_proposal_id": null,
  "plan_approval_status": "approved", "assignment_status": "current",
  "current_activity_version_id": "ver_turn_taking_v1"
},
"after_state": {
  "assignment_version": 2, "pending_proposal_id": "prop_…",
  "plan_approval_status": "approved", "assignment_status": "current",
  "current_activity_version_id": "ver_turn_taking_v1",
  "proposed_activity_version_id": "ver_…", "proposal_id": "prop_…",
  "proposal_status": "pending_parent_acceptance"
}
```

From the event alone it is reconstructable that the original assignment **remained
active** (`assignment_status` unchanged), the original activity version **remained
current** (`current_activity_version_id` unchanged — no replacement occurred), a
**pending proposal was attached** where there was none, and the assignment version
**incremented exactly once**.

`plan_assignment_approved` uses the **same** canonical representation (there is
only one audit-state format): its before/after differ in `plan_approval_status`
(`needs_plan_review` → `approved`) and `assignment_version` (+1), with
`assignment_status`, `current_activity_version_id`, and `pending_proposal_id`
unchanged.

## Read-after-write

- `GET /children` — `pending_proposal_count` increases; `plan_review_count`
  unchanged.
- `GET /children/{id}` — `pending_proposal_count` set; not Needs Plan Review.
- `GET /children/{id}/weekly-plan` — assignment shows the **original** version,
  unchanged approval/practice/assignment status, `pending_proposal_id`, and a
  `pending_proposal` summary; never the proposed version as the active activity.
- `GET /activity-templates/{id}` — original template/version intact; the derived
  version appears in version history **only for principals allowed to see it**
  under the `save_scope` rules above.
- `GET /children/{id}/proposals` and `/proposals/{proposal_id}` — read-only,
  authorization-safe, existence-blind.

## Error contract (stable codes)

`missing_idempotency_key`(400), `invalid_request`(422), `forbidden`(403),
`not_found`(404), `idempotency_key_conflict`(409), `assignment_version_conflict`(409),
`invalid_modify_proposal_transition`(409), `pending_proposal_exists`(409),
`milestone_domain_mismatch`(422). Envelope `{ "error", "detail" }`; no stack
traces, internal ids, or authorization disclosure.

## Parent acceptance (Phase 1B.2B.1)

Parent **acceptance** of a pending modify proposal is now implemented — see
[PARENT_ACCEPTANCE.md](PARENT_ACCEPTANCE.md). Accepting retires the original
assignment (`current` → `replaced`, kept for history), creates exactly one
replacement carrying the proposed activity version in the same plan slot, and
clears `pending_proposal_id`.

Because acceptance clears `pending_proposal_id`, the deterministic-id concern
tracked above **has been addressed for that operation**: acceptance derives its
replacement-assignment and audit-event ids from an operation identity that
includes the SHA-256 hash of the idempotency key. Creation-time ids are
unchanged.

## Parent decline (Phase 1B.2B.2)

Parent **decline** is now implemented — see [PARENT_DECLINE.md](PARENT_DECLINE.md).
Declining clears `pending_proposal_id`, leaves the original assignment `current`
and `approved` on its original activity version, creates **no** replacement, and
preserves the proposed derived version as inactive history. The slot is free
again afterwards, so the therapist may propose a new change.

Decline reuses the same key-bound `operation_identity()` with
`action = "decline_plan_change_proposal"`, so an accept and a decline presented
with the same idempotency key cannot collide on one audit document.

## Parent-safe proposal read (Phase 1B.2B.3)

`GET /children/{id}/proposals/{proposal_id}` is now **role-aware** — see
[PARENT_PROPOSAL_READ.md](PARENT_PROPOSAL_READ.md). The therapist response is
unchanged; an authorized parent receives a narrower dedicated projection carrying
the two versions accept/decline need. Read-only, and the generic
activity-template visibility rules above are untouched.

## Not implemented yet (later gated phases)

Parent proposal list/inbox, therapist cancellation, proposal expiry,
add/remove/generic-replace proposals, note/private-note writes, Firebase Auth,
Firestore, Cloud Run, frontend integration, production config, real users/data.
