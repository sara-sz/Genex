# Therapist API — Parent Add Activity Decisions (Phase 1B.2F)

**Fictional, in-memory development behavior. No Firestore, no Firebase Auth, no
Cloud Run, no frontend connected, no real data.**

Completes the Add lifecycle: a therapist recommends an additional activity
([ADD_ACTIVITY_PROPOSAL.md](ADD_ACTIVITY_PROPOSAL.md)), a parent reads it
([PARENT_ADD_PROPOSAL_READ.md](PARENT_ADD_PROPOSAL_READ.md)), and now a parent
**decides** on it.

## Add is not Modify

| | MODIFY | ADD |
|---|---|---|
| Effect of accept | replaces one assignment with a derived version | **appends** one new activity |
| Original assignment | retired (`current` → `replaced`), version +1 | **untouched** |
| `display_order` | replacement **inherits** the original's | **allocated** as `max(order) + 1` |
| Existing siblings | unchanged | unchanged |
| Requires `expected_assignment_version` | **yes** | **no** — it touches no assignment |

## Endpoints — no new routes

```
POST /api/v1/children/{child_id}/proposals/{proposal_id}/accept
POST /api/v1/children/{child_id}/proposals/{proposal_id}/decline
Headers: Idempotency-Key: <client-generated-key>   (required)
Body:    { "expected_proposal_version": 1 }        (ADD)
```

Both services dispatch on `ProposalType` **inside the transaction**, so the type
cannot change between a client's earlier GET and the decision. MODIFY keeps its
frozen behaviour; REPLACE, REMOVE and anything unrecognised fail closed. OpenAPI
stays at **22 paths**.

### Request contract

`expected_assignment_version` is now optional **at the transport layer only**:

- **MODIFY still requires it.** The service raises a typed `invalid_request`
  (**422**) if it is missing — the same status FastAPI produced when the field was
  declared required, now with the project's error envelope. Modify's optimistic
  concurrency is unchanged and pinned by regression tests.
- **ADD must not supply it.** An Add touches no existing assignment; fabricating
  a version number would assert a concurrency check that has no subject. Sending
  it as explicit `null` behaves identically to omitting it.

OpenAPI effect: `AcceptProposalRequest.required` and
`DeclineProposalRequest.required` are now `["expected_proposal_version"]`;
`expected_assignment_version` remains a documented, nullable property.

### Response contract

Add decisions get their own models rather than being forced into the Modify
shapes — a Modify accept reports a `retired_assignment` and a
`replacement_assignment`, and an Add has neither.

```
accept   ->  anyOf[ AcceptProposalResponse,  AddAcceptResponse  ]
decline  ->  anyOf[ DeclineProposalResponse, AddDeclineResponse ]
```

Each pair is **disjoint on required fields** — `added_assignment` vs
`retired_assignment`/`replacement_assignment`, and `destination_scheduled_day` vs
`current_assignment` — so no response can validate as the wrong member and be
silently reshaped. A test pins this.

`AddDeclineResponse` deliberately has **no** `added_assignment` key at all rather
than a null placeholder: a decline adds nothing, and a null would imply the
concept applies.

## Only a CURRENT-plan Add is actionable

A historical Add stays **readable** (frozen at 0.6.1) but is **not decidable**.
Accepting one would create a live assignment inside a week the family has
finished; declining one is refused for the same reason rather than allowed
because it "would be harmless" — a stale proposal is not a live decision. The
proposal is **never** silently migrated into the current week.

The guard uses only the canonical resolver from
[CURRENT_WEEKLY_PLAN.md](CURRENT_WEEKLY_PLAN.md) — never `plans[0]`, never
`max(week_start_date)`, never today's date.

| Destination plan state | Accept | Decline |
|---|---|---|
| the single `CURRENT` plan | ✅ | ✅ |
| `COMPLETED` | 409 `weekly_plan_conflict` | 409 `weekly_plan_conflict` |
| `DRAFT` | 409 | 409 |
| zero `CURRENT` plans | 409 | 409 |
| several `CURRENT` plans | 409 | 409 |
| plan of another child, or missing | 409 | 409 |

A rejected stale decision writes **nothing**: no assignment, no proposal change,
no audit event and no idempotency record. Verified by a full six-collection
snapshot comparison.

## `display_order` allocation

Inside the **same** transaction that creates the assignment:

```python
day = current_assignments_for_day(tx, child_id, weekly_plan_id, destination_day)
# reject duplicates first — an ambiguous day cannot be allocated into
next_display_order = 0 if not day else max(a.display_order for a in day) + 1
```

| Existing day | Allocated |
|---|---|
| `[]` | **0** |
| `[0]` | **1** |
| `[0, 1]` | **2** |
| `[0, 3, 7]` | **8** |

Deliberately **not** `len(day)` — gaps are legal and never compacted, so `[0,3,7]`
must give 8, not 3 (which would collide). Also never: reserved earlier, computed
at proposal creation, computed outside the transaction, taken from the count, or
derived from assignment-id sorting.

Existing siblings are never renumbered, reordered, retired or version-bumped. A
post-condition re-reads the day and rolls the whole decision back unless it gained
exactly the one new assignment with every existing position unchanged.

## Accept

Validated inside the transaction, re-reading authoritative state — never trusting
a prior GET: parent authorization · proposal exists and belongs to the child ·
type is ADD · status pending · `expected_proposal_version` matches · destination
day present and 0–6 · proposed ActivityVersion exists and is immutable · plan
exists, belongs to the child, and **is** the canonical current plan · destination
day has unique orders · no prior resulting assignment.

Creates exactly **one** `PlanAssignment`: the proposal's plan and destination day,
the proposed ActivityVersion, the allocated `display_order`, status `CURRENT`,
`plan_approval_status = approved` (the therapist authored it and the parent
accepted it), version 1, `source_proposal_id` set. **No replacement lineage is
fabricated** — `replaces_assignment_id` and `replaced_by_assignment_id` stay null.
`activity_template_id` is null, because an Add activity is standalone
therapist-authored work rather than a version of a catalog template.

The proposal becomes `accepted` with `decided_at`, `decided_by_*`, version +1 and
`resulting_assignment_id` set.

## Decline

Same current-plan guard. On success the proposal becomes `declined` with
`decided_at`, version +1 and `resulting_assignment_id` left **null**.

**Zero** assignments are created. The destination day's assignment set and order
map must be byte-identical afterwards, and no assignment anywhere may claim the
proposal as its source — both asserted as post-conditions. The proposed
ActivityVersion is preserved unchanged as historical proposal content and is
never activated.

## Eligibility

`_evaluate_add` is now real, and mirrors the write guards. `ELIGIBLE`
(`true/true/true`) only for a **pending** Add whose destination day is valid, whose
proposed version exists and is immutable, whose plan belongs to the child and **is
the canonical current plan**, and whose destination day has unique orders.

Everything else fails closed: historical/`COMPLETED`, `DRAFT`, zero or multiple
`CURRENT` plans, accepted, declined, malformed, missing version, invalid
destination, duplicate day order.

Still a pure read: allocates no `display_order`, reserves no position, creates
nothing, writes no audit or idempotency record.

## Parent reads around a decision

| State | Flags |
|---|---|
| pending, current plan | `true / true / true` |
| accepted | all false, `accepted_or_declined_at` set |
| declined | all false, `accepted_or_declined_at` set |
| historical | readable, all false |

An actionable Add now joins the actionable sort group alongside an actionable
Modify; ordering within the group stays newest-first with a deterministic id
tie-break, and no type is privileged. A decided proposal sorts last.

Parent responses gained **no** internal fields. Still excluded: `display_order`,
`resulting_assignment_id`, `weekly_plan_id`, `activity_version_id`,
`activity_template_id`, assignment ids, `source_proposal_id`, `save_scope`,
provenance, audit, idempotency and the internal destination token.

## Idempotency

Replay is checked **before** state validation, as everywhere else.

Bound to: key hash · actor · action · child · proposal · the canonical request
body (including `expected_proposal_version`).

- same key + same request → stored result, `idempotent_replay: true`, **no**
  second assignment, audit event, idempotency record, proposal-version bump or
  `display_order` allocation;
- same key + changed request → 409 `idempotency_key_conflict`;
- a **different** key against an already-decided proposal → 409
  `proposal_already_decided`, creating nothing.

Document ids derive from `operation_identity(...)`, which includes the SHA-256
hash of the key. There is no assignment to key on at decision time, so the
internal destination token `"<weekly_plan_id>:<day>"` names the day — never
treated as an assignment id, never in `AuditEvent.assignment_id`, never returned.

## Concurrency

- **Two same-day Adds accepted sequentially** → positions `n` and `n+1`.
  Reversing the decision order reverses which proposal gets which — creation time
  reserves nothing.
- **Four concurrent acceptances of different proposals on one day** → all succeed
  with **unique** orders (`[0,1,2,3,4]` from a base of `[0]`); no sibling lost.
- **Same proposal, same key, 8 concurrent** → one logical acceptance, one
  assignment, one audit event, one acceptance idempotency record.
- **Same proposal, different keys, concurrent** → exactly **one** succeeds; the
  others get the canonical decided-state conflict. No double allocation.

Proven only for the current in-memory repository semantics (`RLock` +
snapshot/restore). **A future Firestore implementation must perform the equivalent
read-max-then-write allocation inside one Firestore transaction**; a query
followed by a separate write would reintroduce the collision.

## Audit

`add_activity_proposal_accepted` — before: pending, type add, destination day,
plan, the day's assignment ids and order map, `resulting_assignment_id: null`.
After: accepted, resulting assignment id, allocated `display_order`, the day's new
map, `existing_assignments_unchanged: true`, and explicitly
`retired_assignment_id: null` / `replaced_assignment_id: null`. **Never described
as a replacement.** `AuditEvent.assignment_id` is the created assignment.

`add_activity_proposal_declined` — before: pending, destination day. After:
declined, `plan_assignment_created: false`, `resulting_assignment_id: null`, the
day's map identical to before. `AuditEvent.assignment_id` is null.

## Transaction and rollback

Accept commits assignment + proposal + audit event + idempotency record as one
unit; decline commits proposal + audit + idempotency record. An injected failure
after the intermediate writes leaves the proposal `pending`, no assignment, no
audit event, no idempotency record and every existing assignment and
`display_order` unchanged — verified for both verbs.

## Errors

`missing_idempotency_key` (400) · `forbidden` (403) · `not_found` (404) ·
`weekly_plan_conflict` (409) · `proposal_already_decided` (409) ·
`proposal_version_conflict` (409) · `idempotency_key_conflict` (409) ·
`duplicate_assignment_display_order` (409) ·
`invalid_parent_accept_transition` / `invalid_parent_decline_transition` (409) ·
`invalid_request` (422). No expected state produces a 500, and decision routes
never repair corrupted state.

## Not implemented

Save for Later · Replace · Remove · parent Question/Note/Update · therapist
Reviewed · therapist Discuss Next Session · therapist private notes · weekly-plan
lifecycle transitions · Firestore · Firebase Auth · deployment · frontend
integration.
