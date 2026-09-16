# Therapist API — Discuss Next Session (Phase 1B.3D)

**Fictional, in-memory development behavior. No Firestore, no Firebase Auth, no
Cloud Run, no frontend connected, no real data.** The atomic critical section is
`repo.run_in_transaction` (snapshot/restore rollback), shaped to map onto a
future Firestore transaction.

Fourth slice of the **collaboration workflow**. The parent write (1B.3A), the
parent's read of their own submissions (1B.3B) and therapist Reviewed (1B.3C)
are frozen; this phase adds the second — and final planned — therapist action on
a parent-submitted item.

## Purpose

An authorized therapist may surface one parent-submitted Question, Note or Update
for discussion in a future session:

```
session_preparation_status:   none  ->  discuss_at_next_session
```

That is the entire state change.

## What Discuss Next Session means, and what it does not

It means exactly one thing: *the therapist wants this parent-submitted item
surfaced for discussion during a future session*.

It does **not** mean the therapist replied, answered, resolved the question, that
the parent was notified, that a session was scheduled, that the discussion
happened, or that the item is complete. There is no reply, thread, conversation,
message-status or notification state anywhere in this service, and none may be
added.

```
Parent submits  ->  Therapist sees  ->  Reviewed                 ✅ 1B.3C
                                    ->  Discuss Next Session     ✅ this phase
```

## Endpoint — an explicit named action

```
POST /api/v1/children/{child_id}/notes/{note_id}/discuss-next-session
```

A POST action command, matching the existing `/approve`, `/accept`, `/decline`
and `/review` routes — **not** a PATCH, and deliberately **not** a generic
note-update endpoint. This API expresses lifecycle transitions as explicit named
commands rather than field edits, so there is no route through which a client
submits a `session_preparation_status` value at all.

### Request

**No request body, and no request model.** The command carries no
parent-authored content and no parameters: the child and the note are path
segments, and the only other input is the `Idempotency-Key` header. With no model
at all there is literally nothing for a client to inject
`session_preparation_status`, `review_status`, `body` or an author id into.

`Idempotency-Key` is **required at runtime**: a missing or whitespace-only header
is rejected with `missing_idempotency_key` (400) before any authorization or
state work.

### Response — `NoteSessionPreparationResponse`

```
note_id
session_preparation_status
review_status
idempotent_replay
```

An allow-list, not a projection. `review_status` is echoed **precisely so a
caller can see that flagging did not disturb the other workflow dimension**. No
audit id, no idempotency internals, no operation identity, no parent-authored
content, no other family's data.

## Authorization

Therapist-only, in the same order as every other therapist command:

1. `access.resolve_therapist` — role first. A parent principal receives
   `forbidden` (403), **including the note's own author**.
2. `access.require_full_access` — the existence-blind child check. An unknown
   child, another therapist's caseload, and a pending / not-activated / paused /
   ended connection all produce the **same** `not_found` (404). An **active**
   `TherapistChildConnection` is required.

The note is then resolved **under the requested child** by an authoritative
repository query scoped to both `id` and `child_id`. A note belonging to another
child, an unknown id, and the id of some other object entirely (an assignment, a
weekly plan, a proposal, a child) all raise the same existence-blind 404 — the
query is scoped to `parent_notes`, so a non-note id simply does not resolve.

## One-way, and the DISCUSSED boundary

This phase implements a **single** transition. There is deliberately no unflag,
no clear, no `discuss_at_next_session -> none` reverse, and no transition into
`discussed`.

`SessionPreparationStatus.DISCUSSED` remains a declared domain value **with no
writer**. A note already in `discussed` has moved *past* next-session
preparation, so re-flagging it would be a reverse/re-open semantic this phase
does not have. It fails closed:

| Stored state | Result |
|---|---|
| `none` | transition to `discuss_at_next_session` |
| `discuss_at_next_session` | successful semantic no-op |
| `discussed` | `invalid_session_preparation_transition` (409) |
| anything unrecognized | `invalid_session_preparation_transition` (409) |

The terminal check runs **before** the already-flagged branch, so `discussed` can
never be mistaken for "already flagged" and silently succeed. Failing closed
means nothing moves in either direction: the note, the audit collection and the
idempotency collection are untouched.

`InvalidSessionPreparationTransition` subclasses the existing `ApprovalError`
family (`code` + `http_status`), matching `invalid_plan_approval_transition` and
`invalid_parent_accept_transition`. It needs no new HTTP plumbing.

## Independence from `review_status`

`review_status` and `session_preparation_status` are two fields with two enums,
never collapsed. The frozen review service advances the first and never assigns
the second; this service advances the second and **never assigns the first**.
`review_status` is listed in this service's `IMMUTABLE_NOTE_FIELDS` precisely so
the suite can pin that.

All four combinations are valid and reachable, and the two commands compose in
**either order** to the same final state:

```
new      / none  --discuss-->  new      / discuss_at_next_session
reviewed / none  --discuss-->  reviewed / discuss_at_next_session

new/none --discuss--> --review--> reviewed / discuss_at_next_session
new/none --review-->  --discuss--> reviewed / discuss_at_next_session
```

Marking for next session must never implicitly mark Reviewed, and marking
Reviewed must never implicitly flag. Neither module imports the other, which is
asserted by AST rather than by convention.

## Content is immutable; workflow state is not

`ParentNote` remains append-only in the sense that matters: the parent's
submission is immutable. This action changes none of

```
id · child_id · parent_id · note_type · body · linked_assignment_id ·
linked_activity_title · created_at · environment · schema_version
```

plus `review_status`, which is therapist-owned but owned by a **different**
action.

### Why the stored document is mutated rather than rebuilt

`repo.set` replaces a whole document. Rebuilding the record as
`ParentNote(**doc).model_dump()` would re-materialize every field — refilling
defaults for anything absent and re-coercing representations — which is exactly
how `review_status`, or an immutable parent-authored field, gets silently
rewritten by a workflow action that had no business touching it.

So the transaction takes the authoritative stored dict, assigns **one** key, and
writes it back. Every other key is carried across by identity. `review_status` is
not read-modify-written, not defaulted and not normalized: it is simply never
assigned.

## Idempotent twice over

* **Key-bound** — a replayed `Idempotency-Key` returns the stored result with
  `idempotent_replay: true` and performs no second transition. The same key aimed
  at a different note, child or action is `idempotency_key_conflict` (409);
  command identity includes the action, so a key used for `/review` cannot be
  reused here.
* **Semantic** — a note already marked `discuss_at_next_session` returns success
  under a brand-new key too. Flagging is a **destination, not a counter**: a note
  transitions at most once, emits exactly one transition audit event ever, and
  there is deliberately no `flag_count` and no record of repeat flags.

A monotonic single-field transition needs no `version` and no `updated_at`: the
read and the write happen inside one critical section, so there is no lost update
to detect, and `updated_at` would put a mutable timestamp on a record whose
content is immutable. Neither field was added.

## Audit

Exactly **one** immutable transition event, `parent_note_marked_for_next_session`,
named for what happened to the note — not `discussion_completed`,
`session_scheduled`, `parent_notified`, `reply_sent` or `note_resolved`, none of
which this action performs.

The event id is derived from the **note**, not from the idempotency key. The
one-event guarantee is enforced by the already-flagged guard; the deterministic id
is defence in depth, so even if the guard were bypassed the single transition
event would land on the same document rather than accumulating duplicates.

The therapist is the **actor**; the note's author is unchanged and is recorded
separately (`note_author_parent_id` alongside `marked_by_therapist_id`), so the
two roles stay distinguishable. `review_status` is recorded on **both** sides of
the event, so the record itself proves the review dimension did not move. Only a
safe hash of the idempotency key is stored, never the raw key.

The idempotency record stores `audit_event_id: null` when the call was a semantic
no-op: no event was emitted by *that* request, and pointing at another request's
event would misattribute it.

## Rollback

The whole operation runs inside `repo.run_in_transaction`. On any exception the
store is restored from the pre-transaction snapshot, so a failed flag leaves the
note `none`, writes no audit event and persists no idempotency record — which
also means the key remains reusable for a later, successful attempt. No partial
transition is observable.

## Read integration — no new read surface

**`GET /api/v1/children/{child_id}/next-session` becomes real.** It already
selected parent notes by `session_preparation_status == discuss_at_next_session`
at request time, but nothing could set that value, so it previously reflected
only seeded fixture state. It now surfaces genuine runtime transitions, and still
excludes `discussed` notes.

The other existing read paths reflect the new status naturally, because all three
already projected `session_preparation_status`:

* **Therapist child notes** — `GET /api/v1/children/{child_id}/notes`
* **Therapist cross-child inbox** — `GET /api/v1/notes`
* **Parent own-note history** — `GET /api/v1/children/{child_id}/notes` (parent
  branch)

Reading is still completely read-only in both directions: a read never flags a
note, and flagging creates no read receipt, notification or viewing metadata.
Flagging grows exactly two collections — `audit_events` and
`idempotency_records` — and nothing else.

## Errors

`missing_idempotency_key` (400) · `forbidden` (403) · `not_found` (404) ·
`idempotency_key_conflict` (409) · `invalid_session_preparation_transition`
(409). The route additionally declares 401 for an unauthenticated caller. There
is no 422, because there is no request body. No expected state produces a 500.

## OpenAPI

This phase adds **one path and one operation**: 23 → **24 paths**, 24 → **25
operations**, OpenAPI 3.1.0. The frozen therapist and parent-safe contracts are
unchanged. The six existing test files that pin the frozen path and operation
counts were updated to the new totals and to nothing else.

### One narrowed tripwire

`tests/test_note_review.py::test_no_route_can_change_session_preparation_status`
asserted through 1B.3C that **nothing** could change the session dimension. That
is now false by design, so it was renamed to
`test_only_the_named_discuss_action_can_change_session_preparation_status` and
re-pointed at the invariant that remains permanent: the sole writer must be
`services/note_session_service.py`, and the review service must never be among
the writers. Naming the one allowed writer is **stricter** than the old
count-based check — a writer in any other module, or a second session-dimension
service, now fails. The four banned service symbols (`mark_discuss`,
`def discuss`, `def set_session_preparation`, `def mark_for_next_session`) are
kept verbatim; this phase's entry point is `mark_note_for_next_session`, which
matches none of them.

## Fixtures

The three fictional seed notes are unchanged: `pn_maya_1` (new / none — the
transition subject), `pn_maya_2` (reviewed / discuss_at_next_session, which
exercises the semantic no-op and proves the new writer does not displace fixture
state), and `pn_noah_1` (another child, another caseload). New test data is
fictional.

## Not implemented in this checkpoint

* `DISCUSSED` transition (the value stays declared, with no writer)
* Reverse / unflag / clear / re-open
* Session scheduling, session completion, or any appointment concept
* Parent notification
* Therapist reply, chat, thread or conversation
* Therapist private-note **write** (the read surface remains read-only)
* ParentNote edit, delete or withdraw
* Save for Later
* Replace
* Remove
* Firestore
* Firebase Auth
* RTM
* Frontend integration
* Real data

Reply, chat, thread, conversation, message-status and notification semantics
remain not merely unimplemented but actively guarded against.

# Sequence

1. **Parent Question / Note / Update write** ✅ Phase 1B.3A
2. **Parent reads own submitted notes** ✅ Phase 1B.3B
3. **Therapist Reviewed** ✅ Phase 1B.3C
4. **Therapist Discuss Next Session** ✅ Phase 1B.3D
5. Therapist private-note writes ← next

Save for Later, Replace and Remove remain deferred.
