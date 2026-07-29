# Therapist API — Idempotent Parent Acceptance (Phase 1B.2B.1)

**Fictional, in-memory development behavior. No Firestore, no Firebase Auth, no
Cloud Run, no frontend, no real data.** The atomic critical section is
`repo.run_in_transaction` (snapshot/restore rollback), shaped to map onto a
future Firestore transaction.

Parent **decline** is implemented separately in Phase 1B.2B.2 — see
[PARENT_DECLINE.md](PARENT_DECLINE.md). Therapist cancellation, proposal expiry,
add/remove/generic-replace proposals and note writes remain unimplemented.

## Endpoint

```
POST /api/v1/children/{child_id}/proposals/{proposal_id}/accept
Headers: Idempotency-Key: <client-generated-key>   (required)
Body:    { "expected_proposal_version": 1, "expected_assignment_version": 2 }
```

Both expected versions are **required** — acceptance replaces a live plan item,
so the client must prove it saw the current state of *both* records.

### Success (200) — abbreviated

```json
{
  "proposal": {
    "proposal_id": "prop_…", "proposal_type": "modify",
    "proposal_status": "accepted", "version": 2,
    "decided_by_user_id": "par_elena", "decided_by_role": "parent",
    "decided_at": "…", "resulting_assignment_id": "assign_…"
  },
  "retired_assignment": {
    "assignment_id": "assign_maya_bubbles", "assignment_status": "replaced",
    "replaced_by_assignment_id": "assign_…", "pending_proposal_id": null,
    "version": 3
  },
  "replacement_assignment": {
    "assignment_id": "assign_…", "replaces_assignment_id": "assign_maya_bubbles",
    "source_proposal_id": "prop_…",
    "activity_version_id": "<proposed_activity_version_id>",
    "assignment_status": "current", "plan_approval_status": "approved",
    "practice_status": "not_tried", "pending_proposal_id": null, "version": 1
  },
  "child_summary": { "child_id": "child_maya", "plan_review_count": 0,
                     "pending_proposal_count": 0 },
  "audit_event_id": "aud_…", "idempotent_replay": false
}
```

`practice_status` uses the existing canonical `PracticeStatus.NOT_TRIED`
(`"not_tried"`) — the replacement is a different activity that has not been
tried yet.

## Parent authorization matrix

| Principal / state | Result |
|---|---|
| Parent of the child, active connection, pending modify proposal | accept (200) |
| Therapist principal (any) | 403 `forbidden` |
| Parent of a different child | 404 (existence-blind) |
| Unknown child / unknown proposal / proposal-child mismatch | 404 (existence-blind) |
| Connection pending / paused / ended | 404 (existence-blind) |
| Unauthenticated | 401 (existing fail-closed behavior) |

A parent may accept only their **own** child's pending proposal, and only while
the therapist-child connection is **active** — there is no live collaboration to
accept into otherwise. Unauthorized and unknown resources are indistinguishable.

## Required pre-acceptance state

**Proposal:** type `modify`; status `pending_parent_acceptance`;
`proposed_activity_version_id` present; `target_assignment_id` present; version
matches `expected_proposal_version`.

**Original assignment:** same child and current weekly plan;
`assignment_status = current`; `plan_approval_status = approved`;
`activity_version_id` equals the proposal's `original_activity_version_id`;
`pending_proposal_id` equals the proposal id; version matches
`expected_assignment_version`.

**Proposed ActivityVersion:** exists, `immutable`, and not already active on
another current assignment.

## Original-assignment retirement

The original is **kept for history**, never deleted or rewritten:

- `assignment_status` `current` → `replaced`
- `pending_proposal_id` → `null`
- `replaced_by_assignment_id` → the replacement, `replaced_at` set
- `version` +1 exactly once
- `activity_version_id`, `plan_approval_status`, practice history all **unchanged**

## Replacement-assignment design

Exactly one new `PlanAssignment`: new stable id, same `child_id`,
`weekly_plan_id` and `scheduled_day` (slot preserved), `activity_version_id` =
the proposed version, `replaces_assignment_id` and `source_proposal_id` set,
`assignment_status = current`, `plan_approval_status = approved`,
`practice_status = not_tried`, `pending_proposal_id = null`, `version = 1`,
`created_at`/`updated_at` set.

It does **not** re-enter therapist plan review: the therapist authored the change
and the parent accepted it, so `plan_review_count` does not increase.

## Exactly-one-current invariant

Enforced **inside the transaction**, not by response shaping or UI filtering:

- **Before** mutating, the slot must contain exactly the original as its only
  current assignment.
- **After** mutating, the slot must contain exactly the replacement.

Either check failing raises 409 `replacement_assignment_conflict` and the whole
transaction rolls back. `GET …/weekly-plan` additionally lists only `current`
assignments, so a retired original can never reappear as a second active item.

## Deterministic-ID correction

Creation-time ids seed on the canonical request hash, which **excludes** the
idempotency key. That was safe while `pending_proposal_id` stayed set:
`expected_assignment_version` is inside the hash and increments on every write,
so a second same-hash operation on the same assignment was unreachable.

**Acceptance clears `pending_proposal_id`, removing that guarantee.** Once
cleared, a later lifecycle operation could present the same actor, action, child,
proposal and assignment, derive the same document id, and silently overwrite the
earlier record.

So acceptance derives its ids from `operation_identity(...)`, which binds:

```
sha256(idempotency_key)  |  actor_user_id  |  action
                         |  child_id  |  proposal_id  |  original_assignment_id
```

used for the **replacement assignment id** and the **audit-event id** (the
idempotency record keeps its existing key-derived id). Different keys therefore
produce different candidate ids, while an exact same-key retry resolves to the
same id and replays. Only the SHA-256 **hash** of the key is ever used — the raw
key never appears in an id, a stored record, or a log. Existing frozen ids are
untouched.

## Idempotency and replay ordering

The transaction checks the idempotency record **before** validating proposal
state. This ordering is essential: after a successful acceptance the proposal is
`accepted` and `pending_proposal_id` is cleared, so validating first would make
an exact replay fail with `proposal_already_decided` instead of replaying.

1. Authenticate + existence-blind authorize
2. Locate child, proposal, assignment
3. **Idempotency lookup** — matching key + request hash → return the stored
   result with `idempotent_replay: true`; same key + different
   actor/action/target/body → 409 `idempotency_key_conflict`
4. Only for a new key: validate state, then mutate

## Optimistic concurrency

Both expected versions must match. On success the proposal version and the
original assignment version each increment **exactly once**, and the replacement
starts at version 1. A stale version returns 409 with **no** side effects — no
replacement, no audit event, no proposal or assignment mutation, no successful
idempotency record.

**Concurrent accepts:** same key → one execution plus replays of the same result;
different keys → one success and the rest 409 `proposal_already_decided`. Either
way exactly one replacement, one audit event and one version increment result.

## Audit event

Exactly one immutable `AuditEvent`, `event_type = plan_change_proposal_accepted`,
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
  "replacement_assignment_id": null
},
"after_state": {
  "proposal_status": "accepted", "proposal_version": 2,
  "original_assignment_id": "assign_maya_bubbles",
  "original_assignment_version": 3, "original_assignment_status": "replaced",
  "original_pending_proposal_id": null,
  "current_activity_version_id": "ver_bubbles_v1",
  "replacement_assignment_id": "assign_…",
  "replacement_assignment_status": "current",
  "replacement_activity_version_id": "ver_…",
  "replacement_plan_approval_status": "approved",
  "current_assignment_count_in_slot": 1
}
```

The event alone reconstructs the whole replacement: the proposal moved from
pending to accepted (+1 version), the original was retired (+1 version) and
unlinked from its pending proposal, a replacement now carries the proposed
version, and exactly one current assignment remains in the slot. No raw bearer
tokens, raw idempotency keys or secrets are stored. There is no public audit
endpoint.

## Read-after-write

- `GET /children/{id}/weekly-plan` — the **replacement** is the current item with
  the proposed activity version, `approved`, no `pending_proposal_id`; the
  retired original is not listed as a current plan item.
- `GET /children/{id}/proposals/{proposal_id}` — `accepted`, with
  `resulting_assignment_id` and decision metadata (`decided_by_user_id`,
  `decided_by_role`, `decided_at`).
- `GET /children` — `pending_proposal_count` decreases by one;
  `plan_review_count` unchanged.
- `GET /children/{id}` — no false Needs Plan Review, no pending indicator for the
  accepted proposal.
- `GET /activity-templates/{id}` — canonical template and original version
  intact; the derived version keeps its existing `save_scope` visibility rules.

## Error contract (stable codes)

`missing_idempotency_key`(400), `invalid_request`(422), `forbidden`(403),
`not_found`(404), `idempotency_key_conflict`(409),
`proposal_version_conflict`(409), `assignment_version_conflict`(409),
`invalid_parent_accept_transition`(409), `proposal_already_decided`(409),
`proposal_assignment_mismatch`(409), `replacement_assignment_conflict`(409).
Envelope `{ "error", "detail" }` — no stack traces, internal collection names,
raw tokens, raw keys, or disclosure of unauthorized resource existence.

## Not implemented yet (later gated phases)

Parent decline, therapist cancellation, proposal expiry, add/remove/generic
replace proposals, note and private-note writes, Firebase Auth, Firestore, Cloud
Run, frontend integration, production config, real users or data.
