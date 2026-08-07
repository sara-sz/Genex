# Therapist API — Parent-Safe Proposal Detail Read (Phase 1B.2B.3)

**Fictional, in-memory development behavior. No Firestore, no Firebase Auth, no
Cloud Run, no frontend, no real data.** Read-only: this endpoint mutates nothing.

A parent-safe **proposal list** is implemented in Phase 1B.2B.4 — see
[PARENT_PROPOSAL_LIST.md](PARENT_PROPOSAL_LIST.md).

**This page describes the MODIFY projection, which is unchanged.** Phase 1B.2E
added a separate, type-aware ADD projection on the same route — see
[PARENT_ADD_PROPOSAL_READ.md](PARENT_ADD_PROPOSAL_READ.md). An Add has no original
activity, so it gets its own model rather than being forced into the
original-vs-proposed comparison below; the two are disjoint on required fields.

A cross-child parent inbox is still absent. Remove Activity proposals, therapist
cancellation and proposal expiry also remain unimplemented.

**Decision-flag alignment (Phase 1B.2B.4).** `can_accept` / `can_decline` below are
now computed by the shared read-only evaluator in `app/services/eligibility.py`,
not from `proposal_status == pending_parent_acceptance` alone. A pending proposal
blocked by a write guard therefore reports `false` / `false` here, so this screen
never offers an action that would 409 on submission.

## Route — extended, not added

```
GET /api/v1/children/{child_id}/proposals/{proposal_id}
```

The **existing** route is now role-aware. No new route was created, so the
OpenAPI path count stays at **21**.

| Principal | Response model |
|---|---|
| Therapist (existing authorized access) | `ProposalView` — **unchanged** |
| Authorized parent of that child | `ParentProposalDecisionDetail` — new |

OpenAPI documents the 200 response as `anyOf: [ProposalView,
ParentProposalDecisionDetail]`. The two shapes are **disjoint on their required
fields** — `ProposalView` requires top-level `proposal_id`/`child_id`, the parent
model requires nested `proposal`/`child`/`decision` objects — so neither payload
can validate as the other and be silently coerced.

`ParentProposalDecisionDetail` is a **dedicated projection**, not a serialized
therapist model with fields stripped afterwards. Because FastAPI rebuilds the
response body from the declared `response_model`, a field absent from the parent
schema **cannot** reach a parent even if the service tried to supply it — the
schema definition is the privacy boundary, and a test pins its exact field set.

## Parent authorization matrix

| Request | Result |
|---|---|
| Authorized parent, matching child + proposal | 200 parent view |
| Therapist with existing authorized access | 200 `ProposalView`, unchanged |
| Another parent | **404** |
| Parent of another child | **404** |
| Unknown child / unknown proposal | **404** |
| Proposal belonging to another child | **404** |
| Connection pending / paused / ended | **404** |
| Unconnected therapist | **404** (existing behavior) |
| Unauthenticated | **401** (existing fail-closed behavior) |

A parent may read only when they own the child **and** the therapist-child
connection is **active** — the same policy the accept/decline writes use
(`access.require_parent_child_access`). A parent is never given 403 merely because
a resource belongs to another family; that would confirm it exists.

## Parent-safe response contract

```json
{
  "proposal": {
    "proposal_id": "prop_…", "proposal_type": "modify",
    "proposal_status": "pending_parent_acceptance", "proposal_version": 1,
    "created_at": "…", "decided_at": null
  },
  "child": { "child_id": "child_maya", "display_name": "Maya" },
  "therapist": { "display_name": "Hannah Lieberknecht, MA, SLP" },
  "decision_context": {
    "change_reason": "Adjust based on session.",
    "expected_assignment_version": 2
  },
  "original_activity": { "…": "parent-facing activity content" },
  "proposed_activity": { "…": "parent-facing activity content" },
  "decision": {
    "can_accept": true, "can_decline": true,
    "accepted_or_declined_at": null, "resulting_assignment_id": null
  }
}
```

Each activity carries exactly: `title`, `developmental_domain` (canonical display
label, e.g. `"Talking & Communicating"`), `milestone_id`,
`milestone_display_name`, `skill_focus`, `duration_minutes`, `difficulty`,
`materials`, `materials_type`, `setup`, `parent_instructions`, `what_to_say`,
`how_to_help`, `success_signals`, `variations`, `routine_tags`, `theme_tags`,
`safety_risk_flags`.

### Feeding accept / decline

The response supplies both versions the write endpoints require, so a parent
client needs **no therapist endpoint**:

| Response field | Write request field |
|---|---|
| `proposal.proposal_version` | `expected_proposal_version` |
| `decision_context.expected_assignment_version` | `expected_assignment_version` |

Tested end to end: values read from this GET are passed straight into
`POST …/accept` and `POST …/decline`, and both succeed.

## Decision flags

Computed from canonical backend state (`ProposalStatus`), never from display
labels:

| Proposal status | `can_accept` | `can_decline` | `decided_at` | `resulting_assignment_id` |
|---|---|---|---|---|
| `pending_parent_acceptance`, **actionable** | `true` | `true` | `null` | `null` |
| `pending_parent_acceptance`, **blocked by a write guard** | `false` | `false` | `null` | `null` |
| `accepted` | `false` | `false` | set | the replacement assignment |
| `declined` | `false` | `false` | set | `null` |
| any other (e.g. `cancelled`) | `false` | `false` | as stored | as stored |

The full guard list is documented in
[PARENT_PROPOSAL_LIST.md](PARENT_PROPOSAL_LIST.md#actual-decision-eligibility--the-correction).

A decided proposal is never returned in a write-enabled state. Original and
proposed activity content stays readable so the family retains decision history.

## Fields intentionally excluded from parent responses

Never present: therapist private notes · notes or data for any other child or
family · other therapists' identifiers · the therapist's id, contact email or
organization · `save_scope` · `submitted_for_genex_review` / `therapist_library`
markers · marketplace or publication state · `is_derived`, `immutable`,
`created_by_*`, `modified_by_*`, `original_activity_*` provenance · raw
ActivityVersion or template ids · `weekly_plan_id`, `therapist_id`,
`current_assignment_id` · idempotency-key hashes, idempotency records ·
audit-event internals · `request_id` · `environment`, `schema_version` ·
repository collection names · bearer tokens · raw idempotency keys.

Provenance and ownership fields are excluded **on principle**, not merely because
they were unnecessary: they describe how the activity was authored and who may
see it, which is therapist workflow rather than parent decision material.

## Activity visibility

Authorization to the child + proposal grants sight of exactly the **two** activity
versions that proposal references — the currently assigned one and the proposed
replacement. This is **not** a catalog read:

- The generic activity-template visibility rules are untouched.
- Parents remain **403** on `GET /activity-templates` and
  `GET /activity-templates/{id}`.
- `child_only`, `therapist_library` and `submitted_for_genex_review` versions gain
  no broader exposure; an unrelated therapist still cannot see the derived version.
- No unrelated ActivityVersion history is returned.

## Existence-blindness

Every unauthorized-or-unknown outcome returns the project's canonical envelope,
byte-identical:

```json
{ "error": "not_found", "detail": "Not found." }
```

That covers: unknown child · another parent's child · unknown proposal · proposal
belonging to another child or family · pending / paused / ended connection ·
missing original assignment · missing original or proposed ActivityVersion ·
a proposal pointing at another child's assignment.

A dangling internal reference is treated as *not found* rather than surfaced as a
409 or 500, because disclosing "this proposal exists but its links are broken"
would itself leak existence.

## Error contract

`not_found`(404) for all existence-blind resource and authorization failures ·
`forbidden`(403) retained for a principal whose role cannot use a route at all
(e.g. a parent on the generic catalog) · `invalid_request`(422) for malformed
input. No 500 for missing linked records. Nothing exposes stack traces, internal
class or collection names, authorization reasoning, tokens or secrets.

`invalid_proposal_read_state`(409) is **not** emitted: every inconsistency we can
currently reach is safer to report as existence-blind 404. The code remains
available if a future state is genuinely safe to reveal.

## Read-only guarantee

The endpoint writes nothing. A test deep-copies eight collections
(`PLAN_ASSIGNMENTS`, `PLAN_CHANGE_PROPOSALS`, `ACTIVITY_VERSIONS`,
`ACTIVITY_TEMPLATES`, `AUDIT_EVENTS`, `IDEMPOTENCY_RECORDS`, `CONNECTIONS`,
`CHILDREN`), performs repeated reads as both parent and therapist, and asserts
every collection is byte-identical afterwards — no audit event, no idempotency
record, no assignment / proposal / ActivityVersion change.

## Not implemented yet (later gated phases)

Parent proposal list or inbox, Add / Remove Activity proposals, therapist
cancellation, proposal expiry, note and private-note writes, Firebase Auth,
Firestore, Cloud Run, frontend integration, production config, real users or data.
