# Therapist API — Canonical Current Weekly Plan (Phase 1B.2F.0)

**Fictional, in-memory development behavior. No Firestore, no Firebase Auth, no
Cloud Run, no frontend connected, no real data.**

## Definition

> A child's **current weekly plan** is *exactly one* `WeeklyPlan` for that child
> whose lifecycle `status` is `CURRENT`.

Nothing else makes a plan current.

## Lifecycle

```python
class WeeklyPlanStatus(str, Enum):
    CURRENT   = "current"      # the family's presently active weekly plan
    COMPLETED = "completed"    # a past week, retained for history
    DRAFT     = "draft"        # prepared, not yet active
```

`WeeklyPlan.status` defaults to `CURRENT`, so every previously constructed plan —
and every stored record written before the field existed — keeps its meaning.

**`week_start_date` remains display metadata.** It is stored and echoed; it never
determines lifecycle.

## What this replaces, and why it mattered

Seven services previously answered "which plan is current?" with:

```python
plans = repo.query(C.WEEKLY_PLANS, child_id=child_id)
current_plan_id = plans[0]["id"] if plans else None
```

That rule is not merely under-determined — it is **wrong in the direction that
matters**. `query` iterates the collection in insertion order, so `plans[0]` is
the **first-seeded** plan. Once a child has a second plan, a newly created one is
appended and can therefore *never* become current: currency is pinned to whatever
was stored earliest, permanently. On Firestore it degrades further, because a
query without `order_by` has no defined order at all — the same call becomes
non-deterministic.

Two derived alternatives were considered and **rejected**:

| Candidate | Why rejected |
|---|---|
| "the week containing today" | Makes behavior depend on the wall clock, and would classify every fictional fixture plan as historical (fixtures start `2026-07-27`). |
| `max(week_start_date)` | Means *latest*, not *current*. A drafted future week would become current the moment it is written — precisely the stale-write hazard this exists to prevent. |

So currency is an explicit stored fact, exactly as `AssignmentStatus` and
`ProposalStatus` already are in this domain.

## Resolver

`app/services/weekly_plan.py` — pure and read-only.

```python
current_weekly_plan(repo, child_id)     -> Optional[dict]
current_weekly_plan_id(repo, child_id)  -> Optional[str]
current_weekly_plans(repo, child_id)    -> List[dict]     # normally length 1
```

`current_weekly_plan` returns `None` when **zero** plans are `CURRENT` **and when
more than one is**. An ambiguous lifecycle is never resolved by picking one.

Independent of insertion order, query order and `week_start_date`. Nothing here
mutates, promotes, completes or creates a plan.

## Fail-closed behavior at each call site

| Site | Zero or several CURRENT plans → |
|---|---|
| `approval_service` (approve assignment) | 409 `invalid_plan_approval_transition` |
| `proposal_service` (Modify creation) | 409 `invalid_modify_proposal_transition` |
| `acceptance_service` (Modify accept) | 409 `invalid_parent_accept_transition` |
| `decline_service` (Modify decline) | 409 `invalid_parent_decline_transition` |
| `add_proposal_service` (Add creation) | 409 `weekly_plan_conflict` |
| `eligibility` (read) | `INELIGIBLE` — no action is offered |
| `read_service.get_weekly_plan` (read) | the existing **empty-plan** response shape |

No new error code was introduced: every site already had a typed error for "not
in the current weekly plan", and `None` falls through to it. **No expected
lifecycle ambiguity produces a 500.**

For the zero-plan case this is byte-for-byte the previous behavior (`plans[0]`
already yielded `None`). The correction is the *multiple*-current case, which
previously picked one silently and now fails closed.

### Read behavior in detail

`GET /children/{child_id}/weekly-plan` with no unambiguous current plan returns
its long-standing empty-plan envelope — `weekly_plan_id: ""`,
`week_start_date: ""` — and **never** reveals that several plans claim `CURRENT`.

Note the deliberate asymmetry: the plan *envelope* empties while the child's
assignments still list, because assignments in that response are queried by child
and were never plan-scoped. That is the pre-existing zero-plan behavior,
preserved rather than redesigned.

## Fixtures

```
wp_maya_prev   child_maya   2026-07-20   COMPLETED   (seeded FIRST, no assignments)
wp_maya        child_maya   2026-07-27   CURRENT
wp_eli         child_eli    2026-07-27   CURRENT
wp_noah        child_noah   2026-07-27   CURRENT
```

`wp_maya_prev` is seeded **before** the current plan on purpose: it is both older
and stored first, so a positional lookup would select it. Its presence turns the
whole existing suite into a proof of the resolver — reverting to `plans[0]` fails
**178** tests.

No second `CURRENT` fixture exists. Tests needing duplicate-current ambiguity
construct it explicitly.

## Public API — unchanged

`WeeklyPlan.status` is an **internal** domain lifecycle field. It is deliberately
not exposed anywhere:

- `WeeklyPlanResponse` — unchanged (`child_id`, `weekly_plan_id`,
  `week_start_date`, `assignments`)
- parent-safe schemas — unchanged
- `PlanChangeProposal` — unchanged
- routes — unchanged; OpenAPI stays at **22 paths**, and `WeeklyPlanStatus` does
  not appear in the schema components

## Scope — identification only

**This checkpoint identifies lifecycle. It does NOT implement lifecycle
transitions.** There is currently no:

- activate-draft endpoint
- complete-current-plan endpoint
- automatic weekly rollover
- scheduled transition
- Firestore uniqueness enforcement

Those belong to future persistence / plan-lifecycle work.

## Future persistence note (documented, not implemented)

Firestore cannot express a conventional unique constraint saying "only one
`CURRENT` WeeklyPlan per child". When persistence arrives, any plan-lifecycle
change must therefore happen **inside a transaction** that:

1. reads the child's current plan(s),
2. rejects an already-ambiguous state,
3. marks the prior `CURRENT` plan appropriately, and
4. marks the intended plan `CURRENT`.

The read side stays safe regardless, because the resolver fails closed on
ambiguity rather than picking a winner.

No Firestore resources, indexes or rules are provisioned by this checkpoint.
