# Therapist API — Ordered Activities Within a Weekday (Phase 1B.2C.1)

**Fictional, in-memory development behavior. No Firestore, no Firebase Auth, no
Cloud Run, no frontend, no real data.**

## Founder product decision

**A weekday may contain multiple CURRENT activities.**

Genex generates an activity for most days of the week. An "Add Activity"
recommendation that could only fill an *empty* weekday would therefore be
largely unusable — and, worse, a therapist adding to an occupied day would
silently behave like a Replace. Therapist intent must be explicit, so the plan
model has to represent several activities on one day, ordered.

This phase introduces that foundation **only**. Add / Replace / Remove proposal
behavior and Save for Later are **not** implemented here.

## `display_order`

```python
display_order: int = Field(default=0, ge=0)   # on PlanAssignment
```

- **Zero-based**, matching the existing `scheduled_day` convention.
- **Scoped to `(child_id, weekly_plan_id, scheduled_day)`** — it orders
  activities *within one day*, nothing more.
- **Presentation ordering, not identity.** It is not a slot id, not immutable,
  and a proposal never reserves one. A future reorder may rewrite it.
- **Backward compatible:** the default of `0` keeps every previously constructed
  assignment valid; all four fictional fixtures are explicitly `display_order=0`.
- **Non-negative**, enforced by the model (`ge=0`).

## The new CURRENT-assignment invariant

**Replaced:**

> ~~At most one CURRENT assignment exists for `(child_id, weekly_plan_id, scheduled_day)`.~~

**With:**

> Among CURRENT assignments sharing `(child_id, weekly_plan_id, scheduled_day)`,
> every `display_order` is **unique**.

Several CURRENT assignments may therefore share a weekday. **RETIRED / REPLACED
rows do not participate** — a retired assignment may keep an order value that a
current one now uses.

A duplicate among current same-day assignments is an internal inconsistency and
**fails closed** with a typed 409 `duplicate_assignment_display_order` rather
than producing an ambiguously ordered day. It discloses no other family's data.

Enforced by three small pure helpers in `acceptance_service` (imported by
`decline_service`, mirrored read-only in `eligibility`):

| Helper | Purpose |
|---|---|
| `_current_in_slot(tx, assignment)` | the day's CURRENT assignments |
| `_order_map(assignments)` | `{assignment_id: display_order}` for before/after comparison |
| `_has_duplicate_display_order(assignments)` | uniqueness check |

None mutates state.

## Weekly-plan sorting

```python
key=lambda a: (a["scheduled_day"], a.get("display_order", 0), a["id"])
```

Earlier weekdays first; within a day, ascending `display_order`; `assignment_id`
only as a final determinism tie-break so a duplicate-order bug degrades to stable
output rather than flapping. Never dependent on fixture insertion or dictionary
order — five consecutive reads return byte-identical ordering.

## Modify acceptance

The lifecycle is unchanged. The one substantive addition:

> **The replacement inherits `display_order` from the original.**

Accepting a change to the *third* activity of a day must not move it to the
front. Guards, inside the transaction:

**Before** — the original is **among** the day's current assignments (others may
share the day), and all current orders are unique.

**After** — the day's current set is exactly `before − original + replacement`;
the replacement carries the original's `display_order`; every unrelated same-day
assignment retains its exact order; no duplicate orders exist.

Unchanged: proposal status, lineage, idempotency, audit event type, proposal
versioning, parent authorization, response semantics beyond the additive field.

## Modify decline

**Before** — the original is among the day's current assignments; orders unique.

**After** — the day is *byte-identical*: the same set of current assignments,
each at the same `display_order`. No replacement is created; no unrelated
same-day assignment changes.

Unchanged: decline idempotency, proposal status, audit type, authorization.

## Parent eligibility

The read-only evaluator now requires the target to be **among** the day's current
assignments rather than its only occupant. A second activity on the same day no
longer makes a Modify proposal ineligible; a duplicate current `display_order`
fails closed.

`ProposalType.ADD` remains **ineligible and effectively invisible** to
parent-safe reads, because Add behavior does not exist yet.

## Gaps, compaction and limits

- **Gaps are allowed.** A future Remove may leave `[0, 2]`; that is valid.
- **No automatic compaction or renumbering.** Compaction would rewrite untouched
  assignments — extra versions, extra audit noise — for a purely cosmetic gain.
  Ordering only needs to be *relative*.
- **No maximum activities per day** in this fictional alpha. No invariant
  requires one.

## Audit behavior

Audit event types and structures are unchanged. `current_assignment_count_in_slot`
**keeps its name** for backward compatibility even though the count may now
exceed one; assertions are relational rather than absolute:

- Modify acceptance — `after == before`
- Modify decline — `after == before`

No extra audit event is created by this migration.

## Not implemented yet

**Add Activity** proposal creation, parent-safe Add reads, and parent Add
accept/decline. When Add acceptance arrives it will append using
`max(display_order for that day) + 1`, allocated **inside** the transaction so
two concurrent acceptances cannot collide.

Firestore transactional allocation of the next position — along with a composite
index on `(child_id, weekly_plan_id, scheduled_day, display_order)` — is deferred
until persistence lands. Also unimplemented: Replace, Remove, Save for Later,
reordering endpoint, parent cross-child inbox, note writes.
