# Therapist API — Idempotent Weekly-Plan Approval (Phase 1B.1)

**Fictional, in-memory development behavior. No Firestore transaction exists
yet** (the atomic critical section is `repo.run_in_transaction`, a re-entrant
lock that maps to a future Firestore transaction). **No frontend is connected.
No real therapist or patient data is used.** This is the first Therapist API
write operation; only weekly-plan approval is implemented.

## Endpoint

```
POST /api/v1/children/{child_id}/weekly-plan/assignments/{assignment_id}/approve
Headers: Idempotency-Key: <client-generated-key>   (required)
Body:    { "expected_assignment_version": 1 }
```

Authenticated via the fictional dev-auth adapter (therapist principal).

### Success (200)

```json
{
  "assignment": {
    "assignment_id": "assign_eli_bubbles",
    "child_id": "child_eli",
    "weekly_plan_id": "wp_eli",
    "scheduled_day": 1,
    "plan_approval_status": "approved",
    "practice_status": "not_tried",
    "assignment_status": "current",
    "activity_template_id": "tmpl_bubbles",
    "activity_version_id": "ver_bubbles_v1",
    "version": 2,
    "updated_at": "2026-07-27T18:00:00+00:00"
  },
  "child_summary": { "child_id": "child_eli", "plan_review_count": 0 },
  "audit_event_id": "aud_…",
  "idempotent_replay": false
}
```

A replay with the **same** Idempotency-Key + same operation + same body returns
the same result with `"idempotent_replay": true` — no second mutation, no second
audit event.

## State transition

Only `needs_plan_review → approved`. Invalid (→ `409
invalid_plan_approval_transition`): approving from `approved`,
`change_pending_parent`, `replaced`, `archived`, or an assignment not in the
child's current weekly plan. Approval / practice / assignment states are
**separate**; a display string is never used as state.

## Optimistic concurrency

`PlanAssignment.version` is a stable integer. A successful approval requires
`expected_assignment_version == current version`, then increments it **exactly
once**, sets `updated_at`, writes **one** audit event, and stores the idempotency
result — all in one atomic critical section. A version mismatch returns `409
assignment_version_conflict` and changes **nothing** (no mutation, no audit, no
idempotency record).

## Idempotency

An `IdempotencyRecord` binds: `idempotency_key_hash`, `actor_user_id`, `action`,
`child_id`, `assignment_id`, canonical `request_hash`, `status`, stored `result`,
`audit_event_id`, `created_at`. The record's document id is deterministic from
the key (Firestore-mappable). Behavior:

| Case | Result |
|---|---|
| Same key + same actor/action/target/body | replay original result; no re-mutation, no new audit |
| Same key + any difference (actor/action/target/body) | `409 idempotency_key_conflict` |
| Missing / blank key | `400 missing_idempotency_key` |
| Failed authorization or validation | **no** success idempotency record stored |

The raw key is never stored — only a SHA-256 hash.

## Atomic repository operation

The route never mutates fixtures directly. The approval runs through
service → `repo.run_in_transaction(fn)`, which performs, atomically: resource
lookup → current-plan validation → state-transition validation → expected-version
check → idempotency check → assignment mutation → audit-event creation →
idempotency-result storage → child plan-review-count. The in-memory
implementation holds a re-entrant lock so two concurrent requests cannot approve
twice (verified by a threaded test: exactly one approval, one audit event,
version incremented once).

## Audit event

Exactly one immutable `AuditEvent` per successful first execution:
`event_type=plan_assignment_approved`, `actor_uid`, `actor_role`, `therapist_id`,
`child_id`, `weekly_plan_id`, `assignment_id`, `idempotency_key_hash` (safe hash,
never the raw key or a token), `before_state`, `after_state`, `occurred_at`,
`request_id`. Audit events are append-only and are not editable/deletable through
the API. There is no public audit endpoint; tests inspect via the repository.

## Read-after-write

After approval the existing read endpoints reflect the mutation immediately (one
data source, no separate static summary): `GET /api/v1/children` (plan-review
count decreases; zero when it was the last unapproved current assignment), `GET
/api/v1/children/{child_id}/weekly-plan` (status `approved`, new `version`), and
`GET /api/v1/children/{child_id}` (no stale Needs Review).

## Error contract (stable codes)

| HTTP | error | when |
|---|---|---|
| 400 | `missing_idempotency_key` | header missing/blank |
| 403 | `forbidden` | parent principal / wrong role |
| 404 | `not_found` | unknown child, unknown assignment, child mismatch, or unauthorized (existence-blind) |
| 409 | `idempotency_key_conflict` | key reused with a different request |
| 409 | `assignment_version_conflict` | `expected_assignment_version` mismatch |
| 409 | `invalid_plan_approval_transition` | invalid source state / not a current-plan item |

Errors are `{ "error": "<code>", "detail": "<message>" }` — no stack traces, no
internal fixture names, no authorization disclosure.

## Authorization matrix (approval)

| Principal / connection | Result |
|---|---|
| Therapist, **active** connection, current review-needed assignment | approve (200) |
| Parent principal | 403 `forbidden` |
| Unconnected therapist | 404 (existence-blind) |
| Pending / paused / ended connection | 404 (existence-blind) |
| Unknown child / unknown assignment / child mismatch | 404 (existence-blind) |

## Not implemented yet (later gated phases)

Add/Modify/Replace/Remove proposals, parent accept/decline, parent-note status
writes, private-note writes, Firebase Auth, Firestore, Cloud Run, frontend
integration, production configuration, real users/data.
