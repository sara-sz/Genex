# Therapist API — Therapist Parent-Note Review (Phase 1B.3C)

**Fictional, in-memory development behavior. No Firestore, no Firebase Auth, no
Cloud Run, no frontend connected, no real data.** The atomic critical section is
`repo.run_in_transaction` (snapshot/restore rollback), shaped to map onto a
future Firestore transaction.

Third slice of the **collaboration workflow**. The parent write (Phase 1B.3A)
and the parent's read of their own submissions (Phase 1B.3B) are frozen; this
phase adds the first **therapist action on a parent-submitted item**.

## Purpose

An authorized therapist may mark one parent-submitted Question, Note or Update
as reviewed:

```
review_status:   new  ->  reviewed
```

That is the entire state change.

## What Reviewed means, and what it does not

Reviewed means exactly one thing: *the therapist has reviewed this
parent-submitted item*.

It does **not** mean the therapist replied, answered, agreed, resolved the item,
discussed it in session, marked it for the next session, or that the parent
received a response. There is still no reply, thread, conversation or message
state anywhere in this service, and none may be added. The route is deliberately
**not** `/reply`, `/respond`, `/answer`, `/resolve`, `/read` or `/seen`: none of
those is what happened.

```
Parent submits  ->  Therapist sees  ->  Reviewed              ✅ this phase
                                    ->  (later) Discuss Next Session
```

## Endpoint — an explicit named action

```
POST /api/v1/children/{child_id}/notes/{note_id}/review
```

A POST action command, matching the existing `/approve`, `/accept` and
`/decline` routes — **not** a PATCH. This API expresses lifecycle transitions as
explicit named commands rather than field edits, so there is no route through
which a client submits a `review_status` value at all.

### Request

**No request body, and no request model.** The command carries no
parent-authored content and no parameters: the child and the note are path
segments, and the only other input is the `Idempotency-Key` header. An empty
body model would add a schema component that means nothing — and with no model
at all there is literally nothing for a client to inject `review_status`,
`session_preparation_status`, `body` or an author id into.

`Idempotency-Key` is **required at runtime**: a missing or whitespace-only
header is rejected with `missing_idempotency_key` (400) before any authorization
or state work.

### Response — `NoteReviewResponse`

```
note_id
review_status
session_preparation_status
idempotent_replay
```

An allow-list, not a projection. Only what a therapist UI needs to render the
outcome. `session_preparation_status` is echoed **precisely so a caller can see
that reviewing did not disturb the other workflow dimension**. No audit id, no
idempotency internals, no operation identity, no parent-authored content, no
other family's data.

## Authorization

Therapist-only, in the same order as every other therapist command:

1. `access.resolve_therapist` — role first. A parent principal receives
   `forbidden` (403). A parent can never review a note, including their own.
2. `access.require_full_access` — the existence-blind child check. An unknown
   child, another therapist's caseload, and a pending / not-activated / paused /
   ended connection all produce the **same** `not_found` (404). An **active**
   `TherapistChildConnection` is required.

The note itself is then resolved **under the requested child** by an
authoritative repository query scoped to both `id` and `child_id`. A note
belonging to another child, an unknown id, and the id of some other object
entirely (a private therapist note, a proposal, an assignment) all raise the
same existence-blind 404 — the query is scoped to `parent_notes`, so a non-note
id simply does not resolve.

## Content is immutable; workflow state is not

`ParentNote` remains append-only in the sense that matters: the parent's
submission is immutable and the note's existence is permanent. This action
changes none of

```
id · child_id · parent_id · note_type · body · linked_assignment_id ·
linked_activity_title · created_at · environment · schema_version
```

`review_status` and `session_preparation_status` are different in kind: they are
system/therapist-owned workflow metadata describing what the *therapist* has
done. Advancing one of them is not editing the parent's note.

The service pins the untouchable field list as `IMMUTABLE_NOTE_FIELDS`, which
also includes `session_preparation_status` — therapist-owned, but owned by a
**different** action.

### Why the stored document is mutated rather than rebuilt

`repo.set` replaces a whole document. Rebuilding the record as
`ParentNote(**doc).model_dump()` would re-materialize every field — refilling
defaults for anything absent and re-coercing representations — which is exactly
how an immutable parent-authored field gets silently rewritten by a workflow
action that had no business touching it.

So the transaction takes the authoritative stored dict, assigns **one** key, and
writes it back. Every other key is carried across by identity.

## The two dimensions stay independent

`review_status` and `session_preparation_status` are two fields with two enums,
never collapsed. `reviewed` is not a member of `SessionPreparationStatus`, and no
`SessionPreparationStatus` value is a member of `ParentNoteReviewStatus`.

`session_preparation_status` is **not read-modify-written, not defaulted and not
normalized: it is simply never assigned.** No production code anywhere in `app/`
assigns it, which the test suite verifies by AST over the real application tree
rather than by substring scan — a naive `] =` scan cannot distinguish an
assignment from the far more common `] ==` comparison.

Discuss Next Session is a separate, later, deliberate therapist action.

## Idempotent twice over

* **Key-bound** — a replayed `Idempotency-Key` returns the stored result with
  `idempotent_replay: true` and performs no second transition. The same key
  aimed at a different note, child or action is `idempotency_key_conflict` (409).
* **Semantic** — an already-reviewed note returns success under a brand-new key
  too. Reviewing is a **destination, not a counter**: a note transitions to
  reviewed at most once, emits exactly one transition audit event ever, and there
  is deliberately no `review_count` and no record of repeat reviews. An
  already-reviewed note is a successful no-op, not a conflict.

A monotonic single-field transition needs no `version` and no `updated_at`: the
read and the write happen inside one critical section, so there is no lost update
to detect, and `updated_at` would put a mutable timestamp on a record whose
content is immutable. Neither field was added.

## Audit

Exactly **one** immutable transition event, `parent_note_reviewed`, named for
what happened to the note — not `message_read`, `message_seen`, `reply_sent`,
`note_resolved` or `discussion_completed`, none of which this action performs.

The event id is derived from the **note**, not from the idempotency key. The
one-event guarantee is enforced by the already-reviewed guard; the deterministic
id is defence in depth, so even if the guard were bypassed the single transition
event would land on the same document rather than accumulating duplicates.

The therapist is the **actor**; the note's author is unchanged and is recorded
separately (`note_author_parent_id` alongside `reviewed_by_therapist_id`), so the
two roles stay distinguishable. `session_preparation_status` is recorded on
**both** sides of the event, so the record itself proves the session dimension
did not move.

The idempotency record stores `audit_event_id: null` when the call was a semantic
no-op: no event was emitted by *that* request, and pointing at another request's
event would misattribute it.

## Rollback

The whole operation runs inside `repo.run_in_transaction`. On any exception the
store is restored from the pre-transaction snapshot, so a failed review leaves
the note, the audit collection and the idempotency collection exactly as they
were. No partial transition is observable.

## Read integration — no new read surface

Both existing read paths reflect the new status naturally, because both already
projected `review_status`:

* **Parent history** — `GET /api/v1/children/{child_id}/notes` (parent branch)
  shows the parent their own note as `reviewed`. The parent sees that the
  therapist reviewed it; they receive no reply, because there is none.
* **Therapist child notes and cross-child inbox** —
  `GET /api/v1/children/{child_id}/notes` (therapist branch) and
  `GET /api/v1/notes` reflect the transition immediately.

Reading is still completely read-only in both directions: a read never marks a
note reviewed, and reviewing creates no read receipt or viewing metadata.

`GET /api/v1/children/{child_id}/next-session` is unchanged, because it keys off
`session_preparation_status`, which this phase never touches.

## Errors

`missing_idempotency_key` (400) · `forbidden` (403) · `not_found` (404) ·
`idempotency_key_conflict` (409). The route additionally declares 401 for an
unauthenticated caller. There is no 422, because there is no request body. No
expected state produces a 500.

## OpenAPI

This phase adds **one path and one operation**: 22 → **23 paths**, 23 → **24
operations**, OpenAPI 3.1.0. The frozen therapist and parent-safe contracts —
`WeeklyPlanResponse`, the proposal schemas, the note create and history schemas —
are unchanged. The five existing test files that pin the frozen path and
operation counts were updated to the new totals and to nothing else.

## Fixtures

The three fictional seed notes are unchanged: `pn_maya_1` (new / none),
`pn_maya_2` (already reviewed / discuss-at-next-session, which exercises both the
semantic no-op and the independence guarantee), and `pn_noah_1` (another child,
another caseload). New test data is fictional.

## Not implemented in this checkpoint

* Discuss Next Session transition
* Discussed transition
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
4. Therapist Discuss Next Session ← next
5. Therapist private notes

Save for Later, Replace and Remove remain deferred.
