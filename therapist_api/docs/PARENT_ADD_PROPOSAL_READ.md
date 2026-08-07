# Therapist API — Parent-Safe Add Activity Reads (Phase 1B.2E)

**Fictional, in-memory development behavior. No Firestore, no Firebase Auth, no
Cloud Run, no frontend connected, no real data.**

**Read-only phase.** Nothing here writes to any collection. Add accept/decline,
Save for Later, Replace and Remove remain unimplemented.

## What a parent needs to understand about an Add

A Modify asks *"what changes?"* and is answered by a before/after comparison.
An Add replaces nothing, so that comparison would be meaningless — and
fabricating an "original activity" would describe a change that is not being
proposed. The four questions an Add actually raises are:

1. **Which weekday** does the therapist want to add something to?
2. **What is already scheduled** on that day?
3. **What new activity** is proposed?
4. **Why** does the therapist recommend it?

```
Monday currently has:
  • Bubble requesting
  • Turn-taking with a ball

Your therapist recommends ADDING:
  • Articulation practice

Nothing already on Monday is removed or changed.
```

See [ADD_ACTIVITY_PROPOSAL.md](ADD_ACTIVITY_PROPOSAL.md) for creation, and
[PARENT_PROPOSAL_READ.md](PARENT_PROPOSAL_READ.md) / [PARENT_PROPOSAL_LIST.md](PARENT_PROPOSAL_LIST.md)
for the Modify equivalents.

## Add is readable, not actionable

Add accept/decline endpoints do not exist yet, so every Add reports:

```json
"decision": { "can_accept": false, "can_decline": false,
              "needs_parent_attention": false }
```

Advertising `can_accept: true` would offer a button with nothing behind it. The
eligibility evaluator still **validates** the Add's state (see below) rather than
short-circuiting — those are the checks a future Add decision will need, and
running them now means an Add that could never be acted on is already reported as
such rather than becoming actionable the day the endpoints land.

## Endpoints — no new routes

Both existing role-aware routes gained type-aware parent behavior. OpenAPI stays
at **22 paths**.

```
GET /api/v1/children/{child_id}/proposals
GET /api/v1/children/{child_id}/proposals/{proposal_id}
```

### List item (ADD)

`ParentProposalListItem` gained ONE field, `destination`, which is **null for a
MODIFY** — so the frozen Modify list item is unchanged in value.

```json
{
  "proposal_id": "prop_…", "proposal_type": "add",
  "proposal_status": "pending_parent_acceptance",
  "created_at": "…", "decided_at": null,
  "child": { "child_id": "child_maya", "display_name": "Maya" },
  "therapist": { "display_name": "Hannah Lieberknecht, MA, SLP" },
  "destination": { "scheduled_day": 0, "day_label": "Monday" },
  "proposed_activity": { "title": "…", "developmental_domain": "…",
                         "milestone_display_name": "…" },
  "change_reason": "…",
  "decision": { "needs_parent_attention": false,
                "can_accept": false, "can_decline": false }
}
```

The list stays a **lightweight discovery surface**: a title-level teaser only, and
deliberately **no** `existing_day_activities`. Repeating the whole day on every
item would make the list heavier than the detail it exists to preview.

### Detail (ADD) — `ParentAddProposalDecisionDetail`

```json
{
  "proposal": { "proposal_id": "…", "proposal_type": "add",
                "proposal_status": "…", "proposal_version": 1,
                "created_at": "…", "decided_at": null },
  "child": { "…": "…" },
  "therapist": { "display_name": "…" },
  "destination": { "scheduled_day": 0, "day_label": "Monday" },
  "existing_day_activities": [
    { "title": "Bubble requesting", "developmental_domain": "Talking & Communicating",
      "milestone_display_name": "Requests a desired item", "duration_minutes": null }
  ],
  "proposed_activity": { "…full parent-safe activity content…" },
  "change_reason": "…",
  "decision": { "can_accept": false, "can_decline": false,
                "needs_parent_attention": false, "accepted_or_declined_at": null }
}
```

Deliberately **absent**: `original_activity` (nothing is replaced),
`decision_context` / `expected_assignment_version` (no assignment is touched), and
`resulting_assignment_id` (accepting an Add is not implemented).

`proposed_activity` reuses the already-approved `ParentActivityView`, so a
standalone Add version whose `activity_template_id` is **null** renders exactly
like any other activity — the null linkage is internal and never surfaces.

## Existing-day activity context

Only CURRENT assignments for
`(child_id, proposal.weekly_plan_id, proposal.destination_scheduled_day)` appear.
Retired/replaced rows never do.

**Ordering:** `display_order` ascending, with the assignment id as the final
deterministic tie-break. The **array order** is the product signal —
`display_order` itself is never exposed, and neither is the assignment id, plan
id, activity-version id, assignment status, approval state or pending link.

Zero existing activities is valid: `existing_day_activities: []`.

### Current state vs proposal state

This is a genuine distinction, not an implementation detail:

| | Represents | Changes when |
|---|---|---|
| the **proposal** | what the therapist recommended | never — it is the recommendation |
| `existing_day_activities` | what the family's plan holds **now** | any time the day changes |

The day list is read from canonical state **at request time**. No snapshot is
stored on the proposal for the read. If another activity lands on that weekday
after the proposal was created, the next detail read reflects it while the
proposal record stays byte-identical.

## Type-aware visibility

Visibility is **explicitly allow-listed by type**, never open by default.

| Proposal type | Parent list | Parent detail |
|---|---|---|
| `modify` | visible (frozen contract) | `ParentProposalDecisionDetail` |
| `add` | visible, non-actionable | `ParentAddProposalDecisionDetail` |
| `replace`, `remove`, anything unknown | **excluded** | **404** |

`REPLACE` and `REMOVE` stay fail-closed until their own phases give them a
parent-safe projection.

## Eligibility

`evaluate_parent_decision` now dispatches on proposal type. The guards must not
be shared: a MODIFY is assignment-centric (target assignment, pending link,
version match) while an ADD is day-centric. Copying Modify's target-assignment
guards onto an Add would reject every valid Add.

`_evaluate_add` validates: status pending · destination day is a valid 0–6
weekday · proposed ActivityVersion exists · weekly plan present · the destination
day has no duplicate `display_order`. It then returns **INELIGIBLE regardless**.

`_evaluate_modify` is the frozen logic, unchanged.

## Weekly-plan currency — chosen behavior

**Matches the existing Modify convention: readable as history, non-actionable.**

Modify already behaves this way — `proposal_is_safe_to_show` does not check plan
currency, while `evaluate_parent_decision` does — so an old-plan Modify appears in
the list with all flags false rather than disappearing. Add follows suit:

- the plan must **resolve** and belong to the child (the day's activities are read
  from it), but need not be the **current** plan;
- an old-plan Add stays readable, with decision flags false.

Fail-closing Add on a stale plan would make the two types behave differently for
no product reason.

## Authorization

Reuses the existing existence-blind parent-child policy, unchanged.

| Principal / state | Result |
|---|---|
| Parent of the child, active connection | list includes safe ADD items; detail returns the ADD projection |
| Parent of a different child | 404 |
| Unknown child / unknown proposal / proposal-child mismatch | 404 |
| Connection pending / paused / ended | 404 |
| Unauthenticated | 401 |

Unknown and unauthorized responses are byte-identical:
`{"error": "not_found", "detail": "Not found."}`.

## Privacy allow-list

Explicit allow-list models; the response model **is** the boundary. Never exposed:
`save_scope`, `is_derived`, `immutable`, `activity_template_id`, creator/modifier
ids, `original_activity_*`, `proposed_activity_version_id`, `activity_version_id`,
`assignment_id`, `weekly_plan_id`, `therapist_id`, `display_order`,
`assignment_status`, `plan_approval_status`, `pending_proposal_id`, replaced/
replaces linkage, `practice_status`, audit records, idempotency records, key
hashes, the internal destination target token, request ids, `environment`,
`schema_version`, therapist-library / `submitted_for_genex_review` state, therapist
private notes, retired assignments, and any other family's data.

## Malformed Add proposals

Fail closed **per item**, never with a 500:

| Condition | List | Detail |
|---|---|---|
| missing / out-of-range destination day | item excluded | 404 |
| missing or dangling proposed ActivityVersion | item excluded | 404 |
| child mismatch | item excluded | 404 |
| unresolvable plan, or a plan belonging to another child | item excluded | 404 |
| duplicate CURRENT `display_order` on the destination day | item excluded | 404 |

`total` counts only the safe items returned, and nothing discloses that an item
was dropped or why.

## List sorting

Unchanged and deterministic: actionable first, then pending-but-not-actionable,
then decided; newest first within a group; proposal id as the final tie-break.

Because an Add is non-actionable it sorts into the **second** group — an actionable
Modify still outranks it. Add is never artificially marked actionable to move it up.

## Read-only guarantee

Parent ADD list and detail GETs create and modify nothing. Verified by deep-
comparing `PLAN_ASSIGNMENTS`, `PLAN_CHANGE_PROPOSALS`, `ACTIVITY_VERSIONS`,
`ACTIVITY_TEMPLATES`, `AUDIT_EVENTS`, `IDEMPOTENCY_RECORDS`, `CONNECTIONS` and
`CHILDREN` before and after repeated reads: no audit event, no idempotency record,
no proposal-version change, no assignment change, no activity change, no
`display_order` change, and byte-identical repeated responses.

## Modify and therapist contracts

**Modify parent detail is unchanged** — still `original_activity`,
`proposed_activity`, `decision_context` (with `expected_assignment_version`),
`proposal_version` and its existing decision flags. It keeps its schema name;
Add got a distinct model rather than renaming a frozen one.

The two detail models are **disjoint on required fields** — `original_activity` /
`decision_context` versus `destination` / `existing_day_activities` — so the
`anyOf` union cannot validate either response as the other and silently reshape
it. A test pins this.

**Therapist views are untouched.** A therapist still receives `Page` /
`ProposalView` with `destination_scheduled_day`, null target/original fields and
the proposed version id. Parent schemas never replace therapist schemas.

## OpenAPI

3.1.0, **22 paths, no new route**.
`GET /children/{child_id}/proposals/{proposal_id}` documents `anyOf` over
`ProposalView`, `ParentProposalDecisionDetail` and
`ParentAddProposalDecisionDetail`. `ParentProposalListItem` documents the optional
`destination`.

## Not implemented yet

Add accept/decline · Save for Later · Replace · Remove · reordering endpoint ·
parent cross-child inbox · note writes · therapist cancellation · proposal expiry.
