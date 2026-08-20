# Therapist API — Parent Question / Note / Update (Phase 1B.3A)

**Fictional, in-memory development behavior. No Firestore, no Firebase Auth, no
Cloud Run, no frontend connected, no real data.** The atomic critical section is
`repo.run_in_transaction` (snapshot/restore rollback), shaped to map onto a
future Firestore transaction.

First two slices of the **collaboration workflow**: the parent **write**
(Phase 1B.3A) and the parent's **read of their own submissions** (Phase 1B.3B).

## One-way, not a conversation

A parent submits an item **into** the child's shared care workspace. The
therapist reads it. That is the entire interaction.

```
Parent submits  ->  Therapist sees  ->  (later) Reviewed
                                    ->  (later) Discuss Next Session
```

This is **not** chat, messaging, replies, threads, therapist responses or
notifications. There is deliberately **no** `reply_to_note_id`, `thread_id`,
`conversation_id`, message status, read receipt, typing state, therapist reply,
parent reply or realtime channel — in the domain model, the request, the
response, or any route — and none may be added. `ParentNote`'s own docstring has
said *"No therapist replies / no chat"* since the domain was first written; this
phase is the write side of that same object.

## Three intents, one object

| Type | Parent intent |
|---|---|
| `question` | wants the care team to know a question |
| `note` | wants to record context or an observation |
| `update` | reports how something went, changed or progressed |

They differ by **intent only**. All three share this route, this domain object,
this authorization, this audit machinery and the existing therapist read
endpoint. There is no per-type collection, endpoint or permission — anything
else would make the type a routing concept rather than a human one.

The canonical enum was already exactly right:

```python
class ParentNoteType(str, Enum):
    QUESTION = "question"
    NOTE     = "note"
    UPDATE   = "update"
```

No aliases were introduced.

## Endpoint — an operation, not a path

```
POST /api/v1/children/{child_id}/notes
Headers: Idempotency-Key: <client-generated-key>   (required)
```

POST shares the **existing** notes path, which already served the therapist GET.
OpenAPI therefore stays at **22 paths** and grows from 22 to **23 operations**.
No `/questions`, `/updates`, `/messages`, `/chat` or `/collaboration/messages`
resource exists.

### Request

```json
{
  "note_type": "question",
  "body": "Is it ok if she signs instead of saying the word?",
  "linked_assignment_id": "assign_maya_bubbles"
}
```

`note_type` and `body` are required; `linked_assignment_id` is optional.
`body` is the **canonical existing** `ParentNote` text field — no `message`,
`content` or `text` alias was invented.

Everything else is system-owned and cannot be supplied: `id`, `parent_id`,
`child_id`, `review_status`, `session_preparation_status`, `created_at`,
`linked_activity_title`, `environment`, `schema_version`. A client sending them
is ignored, and a test pins that the system's values win.

### Response

```json
{
  "note": {
    "note_id": "pn_…", "note_type": "question",
    "body": "Is it ok if she signs instead of saying the word?",
    "created_at": "…",
    "review_status": "new", "session_preparation_status": "none",
    "linked_activity_title": "Bubble requesting"
  },
  "idempotent_replay": false
}
```

Explicit allow-list. The activity appears as the **title the parent already
sees** — never `linked_assignment_id`, `weekly_plan_id`, `activity_version_id`,
`activity_template_id`, `display_order`, provenance, audit or idempotency
internals, therapist ids, or the internal operation token.

`review_status` / `session_preparation_status` are returned so a parent UI can
say *"your therapist has not read this yet"*.

## System-owned initial review state

On creation the system sets the canonical existing defaults:

```
review_status              = new
session_preparation_status = none
```

meaning *the therapist has not reviewed this* and *the therapist has not marked
it for discussion*. The parent cannot choose either.

**They are INDEPENDENT.** Two fields, two enums, never collapsed. A future
Reviewed changes `review_status`; a future Discuss Next Session changes
`session_preparation_status`; neither implies the other. Independence is
structural — `reviewed` is not a member of `SessionPreparationStatus` and
`discuss_at_next_session` is not a member of `ParentNoteReviewStatus`.

**Neither transition is implemented in this phase.**

## Optional activity link

A note may be child-level (no link) or reference **one** activity.

When a link is given, the transaction re-reads authoritative state and requires
the assignment to exist, belong to this child, and be **CURRENT** — an activity
the family is working on now. A note about a retired activity would attach live
context to something the plan no longer contains.

Every other case — unknown id, another child's assignment, a retired one — raises
the **same existence-blind 404** as an unknown child, so a parent cannot probe
for assignments that are not theirs. A blank string means *no link*, not a broken
one.

`linked_activity_title` is **denormalized on `ParentNote`** and is what the
therapist read renders, so it is resolved from the assignment's ActivityVersion
at write time. Storing only the id would show the therapist an anonymous
reference.

## Authorization

Reuses the existing parent policy unchanged.

| Principal / state | Result |
|---|---|
| Parent of the child, active connection | 200 |
| Therapist (any) | 403 `forbidden` |
| Parent of a different child | 404 (existence-blind) |
| Unknown child | 404 |
| Connection pending / paused / ended | 404 |
| Unauthenticated | 401 |

Nothing discloses whether another family's child, note or assignment exists.

## Creation transaction

One transaction creates exactly **one** `ParentNote`, **one** audit event and
**one** idempotency record. It creates no reply, conversation, thread, therapist
note, notification, task, assignment or proposal, and mutates no weekly plan,
`PlanAssignment`, `ActivityVersion`, proposal, progress record or therapist
private note — verified by deep-comparing ten collections.

`ParentNote` carries no `version` and no `updated_at`: it is a create-only,
append-only record, which is what one-way sharing means. No optimistic-
concurrency field was invented for creation.

## Idempotency

Replay is checked **before** state validation, as everywhere else.

Bound to: key hash · actor · `action = create_parent_note` · child · note type ·
canonical body · optional linked assignment.

- same key + same request → stored result, `idempotent_replay: true`, no second
  note, audit event, idempotency record or timestamp;
- same key + changed type, text or link → 409 `idempotency_key_conflict`;
- **different keys → distinct notes**, even with identical text. A parent may
  legitimately submit the same words twice; a second thought worded identically
  is a real second note. Ids therefore bind the key hash via
  `operation_identity`, because a note has no version to make a repeat request
  unreachable.

The internal token `"<child_id>:<assignment_id|child>"` names the operation
target for `IdempotencyRecord`. Internal only — never an assignment id, never in
a semantic audit field, never returned.

## Audit

One immutable `parent_note_created` event, subject `parent_note`.

`before_state` records that no note existed; `after_state` records the note id,
type, linked assignment, both initial statuses and the authoring role.
`AuditEvent.assignment_id` carries the **linked assignment** — a genuine semantic
reference here, unlike the internal token.

Deliberately **not** named `message_sent`, `chat_started` or
`therapist_note_created`. Audit fields appear in no parent or therapist response.

## Rollback

An injected failure after the note and audit event are written leaves the note
absent, the audit event absent, the idempotency record absent and every other
collection byte-identical.

## Therapist read integration

**No new therapist route.** The existing endpoints consume these notes as they
always did:

```
GET /api/v1/children/{child_id}/notes     (per child)
GET /api/v1/notes                         (cross-child inbox)
```

The therapist sees `note_type`, `body`, both statuses and
`linked_activity_title` through the existing `ParentNoteView` — enough to
distinguish a question from a note from an update, and to read what the parent
wrote. Those GETs remain read-only.

A newly created note has `session_preparation_status: none`, so it correctly does
**not** appear in `GET /children/{id}/next-session` until a therapist marks it —
a transition that does not exist yet.

No therapist mutation is implemented: no Reviewed, no Discuss Next Session, no
reply, no comment, no private-note write.

## Errors

`missing_idempotency_key` (400) · `forbidden` (403) · `not_found` (404) ·
`idempotency_key_conflict` (409) · `invalid_request` (422). No expected state
produces a 500.

`invalid_request` covers an unsupported `note_type` and an empty or
whitespace-only body. **No maximum body length is imposed** — the project has no
shared text limit, and inventing one here would be a product decision this phase
was not asked to make.

## Fixtures

The three fictional seed notes (`pn_maya_1`, `pn_maya_2`, `pn_noah_1`) are
unchanged, and a test pins that a parent write leaves them byte-identical. New
test data is fictional.

# Parent-safe read of the parent's own notes (Phase 1B.3B)

A parent who has submitted items can read back **their own**. This closes the
loop the write phase deliberately left half-open.

## Authorship isolation — the substantive guarantee

```
GET /api/v1/children/{child_id}/notes
```

The **same** route, now role-aware. OpenAPI stays at **22 paths and 23
operations** — no new path, no new operation.

| Role | Response |
|---|---|
| therapist | `Page` of `ParentNoteView` — **unchanged**, every parent note for the child |
| parent | `ParentNoteHistoryResponse` — only the items **that parent** submitted |

The parent filter is on the note's **stored `parent_id`**, applied in the query
itself so another author's note never enters the result to be filtered out
afterwards. Authorship is never inferred from child ownership, the connection,
audit events, ordering, ids or the session.

**Why not child ownership?** Because the two are not the same thing. Today
`Child.parent_id` is a single value and `require_parent_child_access` gates on
it, so a child has exactly one authorized parent and the distinction is
invisible. When multi-caregiver access arrives, two people will be authorized for
one child while still owning only their own submissions — and filtering on the
stored author is already correct for that. A note authored by a different parent
for the same child is verified invisible.

The two response models are **disjoint on required fields**: only the parent
envelope requires `child_id`, so the role-aware union cannot validate one
response as the other.

### Parent response

```json
{
  "child_id": "child_maya",
  "items": [
    { "note_id": "pn_…", "note_type": "question",
      "body": "Is it ok if she signs instead of saying the word?",
      "created_at": "…", "review_status": "new",
      "session_preparation_status": "none",
      "linked_activity_title": "Bubble requesting" }
  ],
  "total": 1, "next_cursor": null
}
```

`ParentNoteHistoryItem` has the **same field set as `ParentNoteCreated`**, so a
client renders a just-submitted item and a historical one with one component.
`parent_id` is absent by design: every item is the caller's own, so echoing
author identity would add nothing and only widen the surface.

Never exposed: `parent_id`, `therapist_id`, `linked_assignment_id`,
`assignment_id`, `weekly_plan_id`, `display_order`, `activity_version_id`,
`activity_template_id`, audit, idempotency, `request_hash`, operation identity,
`environment`, `schema_version`, provenance, another author's data, or
therapist-private data.

## Status visibility

The **stored** `review_status` and `session_preparation_status` are shown
verbatim, using canonical enum values. No synthesized labels — no *Seen*, *Read*,
*Replied* or *Answered*. All four `new`/`reviewed` × `none`/`discuss…`
combinations round-trip independently, because the two dimensions remain
independent.

This lets a future parent UI show that an item is submitted, reviewed, or flagged
for discussion **once those therapist actions exist**. No transition logic is
implemented here.

## Point-in-time linked activity

The read uses the note's **stored** `linked_activity_title`. It is never
re-resolved from the current plan.

If the linked activity is later modified, replaced or retired — verified through
the real frozen Modify-accept workflow — the note still appears, still shows the
title captured at submission, never relinks to the replacement, and never exposes
`linked_assignment_id`. A retired link must not make a valid note disappear.

## Ordering

`created_at` descending with the note id as a deterministic tie-break — the
**same key** the frozen therapist reads use, so the two surfaces agree. The
therapist helper itself is untouched. Repeated reads are byte-identical, and the
tie-break does not depend on insertion order.

## Empty state

An authorized parent with zero submitted notes receives **200** and an empty
list. Authorization is about the child relationship; absence of content is a
successful empty state, never a 404.

## Read-only

The GET writes nothing. Ten collections are deep-compared across repeated reads.
There is no `viewed_at`, no `read_at`, no read receipt, no view count, and **no
implicit status change** — reading a `new` note never marks it `reviewed`. Only a
future therapist action may do that.

## Authorization

| Principal / state | Result |
|---|---|
| Parent of the child, active connection | 200 + own items |
| Parent of the child, no items | 200 + empty list |
| Therapist | existing therapist `Page` (unchanged) |
| Parent of a different child | 404 (existence-blind) |
| Unknown child | 404 |
| Connection pending / paused / ended | 404 |
| Unauthenticated | 401 |
| Parent calling `GET /notes` (therapist inbox) | **403** — unchanged |

`GET /api/v1/notes` remains the therapist cross-child inbox. Parents gain no
cross-child aggregation.

## Still append-only, still one-way

This phase adds **GET only**. The API still contains **zero** `PUT`, `PATCH` or
`DELETE` operations, so there is no edit, delete, withdraw or retract for a
`ParentNote` from any principal. The read introduces no reply, thread,
conversation, message state, therapist response or notification.

# Sequence

1. **Parent Question / Note / Update write** ✅ Phase 1B.3A
2. **Parent reads own submitted notes** ✅ Phase 1B.3B
3. Therapist Reviewed ← next
4. Therapist Discuss Next Session
5. Therapist private notes

Save for Later, Replace and Remove remain deferred.
