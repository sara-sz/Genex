# Therapist API — Add Activity Proposal Creation (Phase 1B.2D)

**Fictional, in-memory development behavior. No Firestore, no Firebase Auth, no
Cloud Run, no frontend connected, no real data.** The atomic critical section is
`repo.run_in_transaction` (snapshot/restore rollback), shaped to map onto a
future Firestore transaction.

This phase implements **therapist-side creation only**. Parent Add visibility,
Add acceptance, Add decline, Save for Later, Replace and Remove are **not
implemented**.

## Founder product contract

> **Add Activity means ADD ANOTHER activity to the selected weekday.**

Genex already generates an activity for most days of the week. An Add restricted
to an *empty* weekday would therefore be nearly unusable — and an Add that
displaced what was already there would silently be a Replace. Therapist intent
must be explicit.

Consequently:

- The destination weekday does **not** need to be empty.
- Multiple CURRENT assignments may already sit on that day.
- **Multiple pending Add proposals may target the same day.**
- Existing activities remain **completely unchanged** while an Add is pending.
- **No `display_order` is reserved** at proposal time.
- **No `PlanAssignment` is created** at proposal time.

See [ASSIGNMENT_DISPLAY_ORDER.md](ASSIGNMENT_DISPLAY_ORDER.md) for the ordering
foundation this builds on, and [MODIFY_PROPOSAL.md](MODIFY_PROPOSAL.md) for the
sibling verb.

## Endpoint

```
POST /api/v1/children/{child_id}/weekly-plan/proposals/add
Headers: Idempotency-Key: <client-generated-key>   (required)
```

```json
{
  "scheduled_day": 3,
  "expected_weekly_plan_id": "wp_maya",
  "activity": { "...": "same validated content Modify uses" },
  "change_reason": "Additional communication practice for this week.",
  "save_scope": "child_only"
}
```

The route hangs off the **weekly plan**, not an assignment, because Add targets a
day. There is deliberately **no** `expected_assignment_version` and **no**
`display_order` in the request — neither concept applies before acceptance.
`activity` is the same canonical `ModifyActivityInput` model, so milestone/domain
consistency and `save_scope` obey identical rules.

Success is **200**, matching Modify creation. OpenAPI grows 21 → **22 paths**.

### Success (200) — abbreviated

```json
{
  "proposal": {
    "proposal_id": "prop_…", "proposal_type": "add",
    "proposal_status": "pending_parent_acceptance",
    "child_id": "child_noah", "weekly_plan_id": "wp_noah",
    "destination_scheduled_day": 3,
    "proposed_activity_version_id": "ver_…",
    "created_by_user_id": "ther_hannah", "version": 1,
    "resulting_assignment_id": null
  },
  "proposed_activity_version": { "…": "…" },
  "child_summary": { "…": "…" },
  "audit_event_id": "aud_…", "idempotent_replay": false
}
```

There is **no `current_assignment` block** — Add touches no assignment. The
response never contains a `display_order`, a reserved position, a real
`resulting_assignment_id`, the raw idempotency key, its hash, or the internal
destination token.

## Proposal model

`PlanChangeProposal` gains one field:

```python
destination_scheduled_day: Optional[int] = None   # ADD only; 0=Mon .. 6=Sun
```

Deliberately **not** added: `destination_weekly_plan_id`, `display_order`,
`reserved_display_order`, `placeholder_assignment_id`, `PlanSlot`, `slot_id`,
`activity_index`.

| Field | ADD | MODIFY |
|---|---|---|
| `proposal_type` | `add` | `modify` |
| `weekly_plan_id` | required | required |
| `destination_scheduled_day` | required, 0–6 | **null** |
| `proposed_activity_version_id` | required | required |
| `target_assignment_id` | **null** | the assignment |
| `original_activity_template_id` | **null** | present |
| `original_activity_version_id` | **null** | present |
| `resulting_assignment_id` | **null** | set on acceptance |
| `status` | `pending_parent_acceptance` | `pending_parent_acceptance` |
| `version` | `1` | `1` |

`ActivityVersion.activity_template_id` became `Optional`. A therapist-authored Add
activity is new work, not a version of an existing catalog template. Catalog
listings filter versions **by** template id, so a null never joins one and an Add
activity gains no catalog exposure. Every Modify-derived version still carries its
original template id, and `is_derived` stays `False` for an Add.

## What creation does — and does not — write

On success, inside one transaction:

| Created | Count |
|---|---|
| immutable proposed `ActivityVersion` | 1 |
| `PlanChangeProposal` (type `add`) | 1 |
| structured `AuditEvent` | 1 |
| `IdempotencyRecord` | 1 |
| **`PlanAssignment`** | **0** |

Modified: **zero** existing assignments, **zero** existing `display_order` values,
**zero** existing proposals. A post-condition inside the transaction re-reads the
destination day and rolls back unless its assignment set and order map are
byte-identical.

## Destination-day integrity

At creation the service inspects the CURRENT assignments already on the day —
**not** to check emptiness, but to check that the day is unambiguously ordered:

- unique `display_order` values → proceed, **regardless of how many activities
  exist** (zero, one or many);
- a duplicate among them → fail closed with 409
  `duplicate_assignment_display_order`, create nothing, and **do not attempt to
  repair or renumber** anything.

There is no "empty slot" concept and no `slot_occupied` error.

## Multiple pending Add proposals

Two Add proposals may target the same `(child_id, weekly_plan_id, scheduled_day)`.
There is deliberately **no `pending_add_proposal_exists` conflict**.

Tuesday holds one Genex activity at `display_order 0`. A therapist may create
Proposal A (articulation) and Proposal B (bedtime language), both for Tuesday.
Both stay pending; neither is positioned. If both are later accepted the day
becomes `0` Genex, `1` and `2` for A and B in whichever order acceptance ran.

**That acceptance behavior is not implemented in this phase.**

## Idempotency and the internal operation target

Replay is checked **before** state validation, as everywhere else in this service.

The request is bound to: key hash · actor · `action = create_add_activity_proposal`
· child · weekly plan · scheduled day · canonical activity payload ·
`change_reason` · `save_scope`.

- same key + same canonical request → stored result, `idempotent_replay: true`,
  nothing new created;
- same key + any changed component → 409 `idempotency_key_conflict`;
- **different keys → independent creation attempts**, which may create separate
  proposals on the same day. Sharing a destination is never an idempotency
  conflict.

### Destination target token

`IdempotencyRecord` requires an operation target, but an Add has no assignment at
creation — that is the whole point of the corrected model. Rather than refactor
the record (out of scope), the operation names the day it acts on:

```python
destination_target_token(weekly_plan_id, scheduled_day)  ->  "wp_noah:3"
```

It is an **internal idempotency detail only**. It is never an assignment id, never
written to a semantic audit field such as `AuditEvent.assignment_id`, never
returned by any API, and never visible to a parent or therapist view.

### Why Add ids must bind the key hash

Modify seeds its document ids on the canonical request hash alone. That is safe
**only** because `expected_assignment_version` participates and increments on
every successful write, so a second operation with the same hash is unreachable.

An Add request carries **no version at all**. The same therapist may legitimately
send the same activity to the same day again under a new key. Seeding on the
request hash would derive the *same* proposal id and silently overwrite the first
proposal. Add therefore derives its proposal and version ids from
`operation_identity(...)`, which includes the SHA-256 hash of the key — the same
correction introduced for parent acceptance in Phase 1B.2B.1. The raw key never
appears in an id, a stored record, or a log.

## Audit

One immutable `add_activity_proposal_created` event, subject
`plan_change_proposal`.

`before_state` / `after_state` describe the **destination day**, not an
assignment, because no assignment was read or written:

```
weekly_plan_id · destination_scheduled_day
current_assignment_ids_on_day · current_assignment_count_on_day
display_order_map_on_day · plan_assignment_created: false
```

`after_state` adds `display_order_reserved: false`, `proposal_id`,
`proposal_type: "add"`, `proposal_status`, `proposal_version`,
`proposed_activity_version_id`.

The day snapshot is identical on both sides, so the event alone proves nothing was
replaced. `AuditEvent.assignment_id` stays **null**. The event never claims an
assignment was created, a position was reserved, or an existing Genex activity was
replaced.

## Transaction and rollback

`ActivityVersion`, proposal, audit event and idempotency record are written in one
transaction and roll back together. No partial proposal can remain, and no
existing `PlanAssignment` is altered on any path.

Concurrency, proven in-process:

- **same key, concurrent** → one logical creation, one proposal, one record;
- **different keys, same weekday, concurrent** → all succeed, one proposal each,
  **zero** assignments created, and existing `display_order` values remain unique
  and unchanged — because no position is allocated yet.

## Visibility

**Therapist** — the creating therapist reads an Add through the existing routes;
no new GET route was added.

```
GET /api/v1/children/{child_id}/proposals
GET /api/v1/children/{child_id}/proposals/{proposal_id}
```

`ProposalView` gains `destination_scheduled_day` — strictly additive and
**always null for a Modify**, so the frozen Modify response is unchanged in value,
not merely in shape. An unconnected therapist stays existence-blind (404).

**Parent** — Add is **invisible** in this phase:

- parent list **excludes** ADD proposals and `total` does not count them;
- parent detail for an ADD returns the canonical existence-blind **404**, byte-
  identical to an unknown proposal;
- eligibility for ADD is `can_accept: false`, `can_decline: false`,
  `needs_parent_attention: false`;
- an ADD in the store never causes a 500 in the parent list.

Both parent-side guards are stated explicitly on `proposal_type == add` rather
than relying on the incidental null `target_assignment_id` that would already have
dropped it.

## Errors

Reuses `missing_idempotency_key` (400), `forbidden` (403), `not_found` (404),
`idempotency_key_conflict` (409), `duplicate_assignment_display_order` (409),
`invalid_request` (422) and `milestone_domain_mismatch` (422). One new typed
error: **`weekly_plan_conflict` (409)** when `expected_weekly_plan_id` is not the
child's current plan.

Deliberately **not** introduced: `slot_occupied`, `pending_add_proposal_exists` —
an occupied weekday and multiple pending Adds are both valid.

`scheduled_day` range is enforced in the service rather than by a Pydantic bound
so an out-of-range day returns the project's typed `{"error": "invalid_request"}`
envelope instead of FastAPI's generic validation shape.

## Shared assignment-order helpers

Promoted out of `acceptance_service` into `services/assignment_order.py` once Add
creation became the fourth consumer. Day-oriented naming, deliberately no "slot"
semantics:

| Helper | Purpose |
|---|---|
| `current_assignments_for_day(repo, child_id, weekly_plan_id, day)` | the day's CURRENT assignments |
| `current_assignments_sharing_day(repo, assignment)` | same, for a caller holding an assignment |
| `assignment_order_map(assignments)` | `{assignment_id: display_order}` |
| `has_duplicate_display_order(assignments)` | uniqueness check |
| `DuplicateAssignmentDisplayOrder` | the typed 409 |

All pure and read-only; behavior unchanged. `acceptance_service`,
`decline_service`, `eligibility` and Add creation all use them, and
`acceptance_service.DuplicateAssignmentDisplayOrder` still resolves.

**No next-position allocation lives here.** `max(display_order) + 1` belongs inside
the future Add-acceptance transaction, the only place two concurrent acceptances
can be ordered without colliding.

## Not implemented yet

Parent Add visibility · Add acceptance · Add decline · Save for Later · Replace ·
Remove · reordering endpoint · parent cross-child inbox · note writes · therapist
cancellation · proposal expiry.

Firestore persistence is deferred; when it lands, acceptance will need
transactional next-position allocation plus a composite index on
`(child_id, weekly_plan_id, scheduled_day, display_order)`.
