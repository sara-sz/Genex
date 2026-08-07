# Therapist API — Parent-Safe Proposal List (Phase 1B.2B.4)

**Fictional, in-memory development behavior. No Firestore, no Firebase Auth, no
Cloud Run, no frontend, no real data.** Read-only: this endpoint and the
eligibility evaluator mutate nothing.

**Single child only.** There is no cross-child parent inbox and no notifications.
Remove Activity proposals, therapist cancellation and proposal expiry remain
unimplemented.

**Phase 1B.2E** made this list type-aware: ADD proposals now appear alongside
MODIFY, carrying an extra `destination` block and always non-actionable — see
[PARENT_ADD_PROPOSAL_READ.md](PARENT_ADD_PROPOSAL_READ.md). `destination` is
**null for every MODIFY**, so nothing described below changed in value. REPLACE
and REMOVE stay fail-closed.

## Route — extended, not added

```
GET /api/v1/children/{child_id}/proposals
```

The **existing** route is now role-aware. No new route was created, so the OpenAPI
path count stays at **21**.

| Principal | Response model |
|---|---|
| Therapist (existing authorized access) | `Page` — **unchanged** |
| Authorized parent of that child | `ParentProposalListResponse` — new |

OpenAPI documents the 200 as `anyOf: [Page, ParentProposalListResponse]`, with all
four new schemas registered in `components.schemas`. Both role-aware proposal
routes now emit `anyOf`:

| Route | 200 `anyOf` |
|---|---|
| `…/proposals` | `Page`, `ParentProposalListResponse` |
| `…/proposals/{proposal_id}` | `ProposalView`, `ParentProposalDecisionDetail` |

Note that `Page.items` is `List[dict]`, so the two list shapes are **not** disjoint
by required fields the way the detail models are. The route returns an actual
`ParentProposalListResponse` **instance**, which pydantic's smart union matches by
exact type — verified by a test asserting the parent items keep their nested
`decision` / `child` / `therapist` objects rather than being flattened.

## Parent-safe list response

```json
{
  "items": [
    {
      "proposal_id": "prop_…",
      "proposal_type": "modify",
      "proposal_status": "pending_parent_acceptance",
      "created_at": "…",
      "decided_at": null,
      "child": { "child_id": "child_maya", "display_name": "Maya" },
      "therapist": { "display_name": "Hannah Lieberknecht, MA, SLP" },
      "proposed_activity": {
        "title": "Bubble requesting (adapted)",
        "developmental_domain": "Talking & Communicating",
        "milestone_display_name": "Requests a desired item"
      },
      "change_reason": "Adjust based on session.",
      "decision": {
        "needs_parent_attention": true,
        "can_accept": true,
        "can_decline": true
      }
    }
  ],
  "total": 1,
  "next_cursor": null
}
```

**Deliberately lighter than the detail view.** The list carries **no** activity
instructions and **no** optimistic-concurrency versions — a client must open
`GET …/proposals/{proposal_id}` before submitting a decision, so it always sends
the freshest `proposal_version` and `expected_assignment_version`. The list is for
discovery and navigation only.

## Sorting

Deterministic, never dependent on fixture insertion or dictionary order:

1. **Actionable** — `needs_parent_attention == true`
2. **Pending but not currently actionable**
3. **Decided** — accepted, declined, cancelled, anything else
4. Within each group: **newest `created_at` first**
5. Tie-break: **`proposal_id` ascending**

A test asserts five consecutive requests return byte-identical ordering, and a
unit test pins the pure `sort_key` grouping.

## Actual decision eligibility — the correction

`proposal_status == pending_parent_acceptance` is **not** sufficient to know
whether a parent can act. The accept/decline writes enforce further guards, so a
genuinely pending proposal can still 409 on submission. Advertising
`can_accept: true` for such a proposal would offer the family a button that cannot
work.

`app/services/eligibility.py` provides one pure, read-only evaluator used by
**both** the parent list and the parent proposal **detail**. For all three flags
to be true it requires:

**Proposal** — type `modify`; status `pending_parent_acceptance`;
`target_assignment_id`, `original_activity_version_id` and
`proposed_activity_version_id` all present.

**Original assignment** — same child; same weekly plan as the proposal; that plan
is the child's current one; `assignment_status = current`;
`plan_approval_status = approved`; `activity_version_id` equals the proposal's
original; `pending_proposal_id` equals the proposal id.

**Connection** — active.

**Linked records** — original and proposed ActivityVersions exist; the proposed
version is not already active on another current assignment.

**Slot invariant** — exactly one current assignment in the affected weekly-plan
slot, and it is the proposal's original assignment.

Any failure yields `can_accept = can_decline = needs_parent_attention = false`.
The evaluator fails closed on missing or inconsistent records — returning
INELIGIBLE rather than raising, so one bad record cannot break a whole list. It
never reports **why** a proposal is ineligible; that reasoning stays internal.

It deliberately **duplicates** the write guards rather than importing them: the
write services own the authoritative check inside their transaction and must not
be influenced by a read path. If the two ever diverge, a submission fails closed
with a typed 409 — the safe direction.

## Ineligible pending behavior

No new public status is invented. An ineligible pending proposal keeps
`proposal_status: "pending_parent_acceptance"` and simply reports all three
decision flags as `false`.

The existing fixture `prop_maya_modify_bubbles` is exactly this case — its
assignment is `change_pending_parent`, and its `original_activity_version_id` is
unset — so it appears safely in the list as pending with all flags false. The
fixture was **not** modified to make it actionable, and a test asserts both that
its flags are false *and* that a real accept attempt against it returns 409, so the
advertised flags and the write guards demonstrably agree.

| State | status | `needs_parent_attention` | `can_accept` | `can_decline` | `decided_at` |
|---|---|---|---|---|---|
| Pending, actionable | `pending_parent_acceptance` | true | true | true | null |
| Pending, blocked | `pending_parent_acceptance` | false | false | false | null |
| Accepted | `accepted` | false | false | false | set |
| Declined | `declined` | false | false | false | set |

## Activity visibility

Each item exposes only three fields from the **proposed** ActivityVersion:
`title`, `developmental_domain`, `milestone_display_name`. This is not a catalog
query: the generic activity-template rules are untouched, parents remain **403**
on `/activity-templates`, and `therapist_library` / `submitted_for_genex_review` /
other children's `child_only` versions gain no exposure. No original/proposed
version ids appear.

## Excluded fields

Never present in a parent list: `save_scope`, `is_derived`, `immutable`,
`created_by_*`, `modified_by_*`, `original_activity_template_id`,
`original_activity_version_id`, `proposed_activity_version_id`,
`current_assignment_id`, `weekly_plan_id`, `therapist_id`, parent or therapist
email, organization identifiers, idempotency-key hashes, idempotency records,
audit events, request ids, collection names, publication/marketplace state,
`submitted_for_genex_review` metadata, therapist-library ownership metadata,
therapist private notes, other children or families, unrelated ActivityVersion
histories, bearer tokens, raw idempotency keys — **and** `proposal_version` /
`expected_assignment_version`, which belong only to the detail endpoint.

Explicit allow-list schemas, not therapist objects with fields removed. A test
pins every parent schema's exact field set at source level, because FastAPI
rebuilds the body from the declared `response_model` — the schema *is* the privacy
boundary.

## Malformed-item behavior

If a proposal's linked records are missing or belong to another child, the item is
**excluded** from the list rather than partially rendered:

- no partial item is emitted,
- no reason is disclosed,
- `total` counts only the safe items,
- no 500 is produced.

Excluded when: the proposed ActivityVersion is missing; the referenced assignment
is missing; or the referenced assignment belongs to another child. Note the
distinction from *ineligibility* — an ineligible-but-safe proposal is **included**
with false flags; only an unrenderable one is dropped.

## List / detail consistency

For every safely returned proposal, `proposal_id`, `proposal_type`,
`proposal_status`, `decided_at`, therapist display name, proposed activity title,
`can_accept` and `can_decline` all match between list and detail — asserted for
every item in the list, not just one.

The detail endpoint remains the source for `proposal_version`,
`expected_assignment_version`, the complete original and proposed activities, and
decision submission.

## Existence-blindness

Unknown child, another parent's child, a child of another family, and pending /
paused / ended connections all return the project's canonical body, byte-identical:

```json
{ "error": "not_found", "detail": "Not found." }
```

**One intentional change to existing behavior:** a parent listing another family's
child previously received **403**; it is now **404**. Returning 403 would confirm
that the child exists, which §4 forbids. One assertion in
`tests/test_modify_proposal.py::test_read_proposal_endpoints` was updated
accordingly — it asserted parent-denial behavior, not therapist behavior.

Nothing reveals whether proposals exist, how many were excluded, why an item is
ineligible, or which linked record is missing.

## Read-only guarantee

A test deep-copies ten collections (`PLAN_ASSIGNMENTS`, `PLAN_CHANGE_PROPOSALS`,
`ACTIVITY_VERSIONS`, `ACTIVITY_TEMPLATES`, `AUDIT_EVENTS`, `IDEMPOTENCY_RECORDS`,
`CONNECTIONS`, `CHILDREN`, `MILESTONES`, `THERAPIST_PROFILES`), performs repeated
parent and therapist list/detail requests **plus** direct evaluator calls, and
asserts every collection is byte-identical afterwards. No audit event, no
idempotency record, and no change to any assignment, proposal, ActivityVersion or
connection — including no version, `pending_proposal_id`, timestamp, practice or
approval change.

## Error contract

`not_found`(404) for existence-blind authorization and resource failures ·
`forbidden`(403) where a role cannot use an operation at all · `invalid_request`
(422) for malformed input. A malformed individual proposal is **excluded**, never
surfaced as a public error. No stack traces, collection names, internal class
names, out-of-schema ids, tokens or secrets.

## Not implemented yet (later gated phases)

Cross-child parent inbox or notifications, Add / Remove Activity proposals,
therapist cancellation, proposal expiry, note and private-note writes, Firebase
Auth, Firestore, Cloud Run, frontend integration, production config, real users or
data.
