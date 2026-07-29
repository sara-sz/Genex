# Therapist API — Idempotent Parent Decline (Phase 1B.2B.2)

**Fictional, in-memory development behavior. No Firestore, no Firebase Auth, no
Cloud Run, no frontend, no real data.** The atomic critical section is
`repo.run_in_transaction` (snapshot/restore rollback), shaped to map onto a
future Firestore transaction.

A parent-safe **proposal detail read** is implemented in Phase 1B.2B.3 — see
[PARENT_PROPOSAL_READ.md](PARENT_PROPOSAL_READ.md). It supplies both versions this
endpoint requires, so a parent client needs no therapist endpoint. A parent
proposal *list* is still absent. Therapist cancellation, proposal expiry,
add/remove/generic-replace proposals and note writes remain unimplemented.

Decline is the mirror of [acceptance](PARENT_ACCEPTANCE.md), and its defining
property is what it does **not** do: **no replacement assignment is created**, and
the original plan item is left exactly as the family already knows it.

## Endpoint

```
POST /api/v1/children/{child_id}/proposals/{proposal_id}/decline
Headers: Idempotency-Key: <client-generated-key>   (required)
Body:    { "expected_proposal_version": 1, "expected_assignment_version": 2 }
```

Both expected versions are **required** — decline mutates both the proposal and
the assignment, so the client must prove it saw the current state of each.

### Success (200) — abbreviated

```json
{
  "proposal": {
    "proposal_id": "prop_…", "proposal_type": "modify",
    "proposal_status": "declined", "version": 2,
    "decided_by_user_id": "par_elena", "decided_by_role": "parent",
    "decided_at": "…", "resulting_assignment_id": null
  },
  "current_assignment": {
    "assignment_id": "assign_maya_bubbles", "assignment_status": "current",
    "activity_version_id": "ver_bubbles_v1", "plan_approval_status": "approved",
    "practice_status": "did_it", "pending_proposal_id": null, "version": 3,
    "replaced_by_assignment_id": null, "replaced_at": null
  },
  "child_summary": { "child_id": "child_maya", "plan_review_count": 0,
                     "pending_proposal_count": 0 },
  "audit_event_id": "aud_…", "idempotent_replay": false
}
```

`resulting_assignment_id` is **always null** for a decline. There is no
replacement assignment in the response because none is created.

## Parent authorization matrix

Identical to acceptance:

| Principal / state | Result |
|---|---|
| Parent of the child, active connection, pending modify proposal | decline (200) |
| Therapist principal (any) | 403 `forbidden` |
| Parent of a different child | 404 (existence-blind) |
| Unknown child / unknown proposal / proposal-child mismatch | 404 (existence-blind) |
| Connection pending / paused / ended | 404 (existence-blind) |
| Unauthenticated | 401 (existing fail-closed behavior) |

Unauthorized and unknown resources are indistinguishable — nothing discloses
whether a child, assignment or proposal exists.

## Required pre-decline state

**Proposal:** type `modify`; status `pending_parent_acceptance`;
`target_assignment_id` present; `proposed_activity_version_id` present; version
matches `expected_proposal_version`.

**Original assignment:** same child and current weekly plan;
`assignment_status = current`; `plan_approval_status = approved`;
`activity_version_id` equals the proposal's `original_activity_version_id`;
`pending_proposal_id` equals the proposal id; version matches
`expected_assignment_version`.

**Proposed ActivityVersion:** exists, `immutable`, and not active on any current
assignment.

## Decline state transition

`pending_parent_acceptance` → `declined`, version +1, with
`decided_by_user_id`, `decided_by_role = parent` and `decided_at` recorded.
`resulting_assignment_id` stays `null`. The declined proposal remains stored for
history — it is never deleted.

## Original-assignment preservation

Exactly one field of substance changes: the pending link is released.

| Field | After decline |
|---|---|
| `assignment_status` | **`current`** (unchanged) |
| `plan_approval_status` | **`approved`** (unchanged) |
| `practice_status` | **unchanged** |
| `activity_version_id` | **original version** (unchanged) |
| `pending_proposal_id` | `null` |
| `replaced_by_assignment_id` | `null` (unchanged) |
| `replaced_at` | `null` (unchanged) |
| `version` | +1 exactly once |
| `updated_at` | set |

The family keeps seeing the same activity they already had. Because the slot is
free again, the therapist may propose a new change afterwards.

## No replacement assignment

**Zero** replacement assignments are created. The transaction additionally
asserts, after mutating, that no assignment carries
`source_proposal_id == proposal_id` or `replaces_assignment_id == original id`.
The proposed derived ActivityVersion is preserved unchanged as history and is
**never activated** by a decline.

## Exactly-one-current invariant

Enforced **inside the transaction**, both directions — and unlike acceptance, the
expected occupant is the same record on both sides:

- **Before** mutating: the slot must contain exactly the original as its only
  current assignment.
- **After** mutating: the slot must *still* contain exactly that same original.

Violation raises 409 **`current_assignment_conflict`** — a decline-specific code
rather than acceptance's `replacement_assignment_conflict`, because the invariant
being protected is "the original remains the single current assignment", not
"exactly one replacement was installed". The whole transaction rolls back.

## Deterministic action identity

Reuses `operation_identity()` with `action = "decline_plan_change_proposal"`,
binding:

```
sha256(idempotency_key) | actor_user_id | action
                        | child_id | proposal_id | original_assignment_id
```

used for the **decline audit-event id**. No replacement-assignment id is needed
because decline creates no assignment; the idempotency record keeps its existing
key-derived id.

Because the **action name** participates, an accept and a decline presented with
the same idempotency key on the same proposal cannot collide on one audit
document. Different keys produce different candidate ids; an exact same-key retry
resolves to the same id and replays. Only the SHA-256 **hash** of the key is ever
used — the raw key never appears in an id, a stored record, or a log. Acceptance
and proposal-creation ids are unchanged.

## Idempotency and replay ordering

The idempotency record is checked **before** validating proposal state. This is
essential: after a successful decline the proposal is `declined` and
`pending_proposal_id` is `null`, so validating first would make an exact replay
fail with `proposal_already_decided`.

1. Authenticate
2. Existence-blind parent/child authorization
3. Locate proposal and original assignment
4. **Idempotency lookup** — matching key + request hash → return the stored
   result with `idempotent_replay: true` (no state re-validation, and
   `pending_proposal_id` is *not* required to still be set); same key +
   different actor/action/target/body → 409 `idempotency_key_conflict`
5. Only for a new key: validate versions and pending state, then decline

A **new** key used after the proposal is decided returns 409
`proposal_already_decided` — including when the proposal was *accepted* rather
than declined.

## Optimistic concurrency

Both expected versions must match. On success the proposal version and the
assignment version each increment **exactly once**. A stale version returns 409
`proposal_version_conflict` / `assignment_version_conflict` with **no** side
effects — no proposal or assignment mutation, no audit event, no successful
idempotency record, and no replacement assignment.

**Concurrent declines:** same key → one execution plus replays of the same result
(same `audit_event_id`); different keys → one success and the rest 409
`proposal_already_decided`. Either way exactly one audit event, one version
increment per record, and zero replacements.

## Audit event

Exactly one immutable `AuditEvent`, `event_type = plan_change_proposal_declined`,
carrying actor (`actor_role = parent`), therapist, child, weekly plan, original
assignment, subject proposal, `idempotency_key_hash`, `occurred_at`, `created_at`
and `request_id`. Structured JSON-compatible state:

```json
"before_state": {
  "proposal_status": "pending_parent_acceptance", "proposal_version": 1,
  "original_assignment_id": "assign_maya_bubbles",
  "original_assignment_version": 2, "original_assignment_status": "current",
  "original_pending_proposal_id": "prop_…",
  "current_activity_version_id": "ver_bubbles_v1",
  "proposed_activity_version_id": "ver_…",
  "current_assignment_count_in_slot": 1
},
"after_state": {
  "proposal_status": "declined", "proposal_version": 2,
  "original_assignment_id": "assign_maya_bubbles",
  "original_assignment_version": 3, "original_assignment_status": "current",
  "original_pending_proposal_id": null,
  "current_activity_version_id": "ver_bubbles_v1",
  "proposed_activity_version_id": "ver_…",
  "proposed_activity_version_active": false,
  "replacement_assignment_id": null,
  "current_assignment_count_in_slot": 1
}
```

From the event alone: the parent declined, the original assignment remained
`current` on its original activity version, the proposed version was **not**
activated, **no replacement** was created, and the pending reference was cleared.
No raw bearer tokens, raw idempotency keys or secrets are stored. There is no
public audit endpoint.

## Read-after-write

- `GET /children/{id}/weekly-plan` — the original is still the current item, on
  its original activity version, `approved`, `practice_status` unchanged, with
  `pending_proposal_id: null` and no `pending_proposal` summary. No replacement
  appears.
- `GET /children/{id}/proposals/{proposal_id}` (therapist) — `declined`, with
  decision metadata and `resulting_assignment_id: null`.
- `GET /children` — `pending_proposal_count` decreases by one;
  `plan_review_count` unchanged.
- `GET /children/{id}` — no pending-parent-acceptance indicator, no false Needs
  Plan Review.
- `GET /activity-templates/{id}` — canonical template and original version
  intact; the declined derived version keeps its existing `save_scope` visibility
  rules and is **not** published globally by being declined.

## Error contract (stable codes)

`missing_idempotency_key`(400), `invalid_request`(422), `forbidden`(403),
`not_found`(404), `idempotency_key_conflict`(409),
`proposal_version_conflict`(409), `assignment_version_conflict`(409),
`invalid_parent_decline_transition`(409), `proposal_already_decided`(409),
`proposal_assignment_mismatch`(409), **`current_assignment_conflict`**(409).

`proposal_version_conflict`, `proposal_already_decided` and
`proposal_assignment_mismatch` are shared with acceptance, where their meaning is
unchanged. Envelope `{ "error", "detail" }` — no stack traces, internal
collection names, raw tokens, raw keys, or disclosure of unauthorized resource
existence.

## Not implemented yet (later gated phases)

Parent proposal-read endpoint, therapist cancellation, proposal expiry,
add/remove/generic replace proposals, note and private-note writes, Firebase
Auth, Firestore, Cloud Run, frontend integration, production config, real users
or data.
