# Therapist API — Private Therapist Note Write (Phase 1B.3E)

**Fictional, in-memory development behavior. No Firestore, no Firebase Auth, no
Cloud Run, no frontend connected, no real data.** The atomic critical section is
`repo.run_in_transaction` (snapshot/restore rollback), shaped to map onto a
future Firestore transaction.

Fifth and final slice of the **collaboration workflow**. The parent write
(1B.3A), the parent's read of their own submissions (1B.3B), therapist Reviewed
(1B.3C) and Discuss Next Session (1B.3D) are frozen. This phase completes
`GET /children/{id}/next-session` end-to-end: its `private_note_items` half
previously reflected only seeded fixture data, because nothing could write a
private note at runtime.

## Purpose

An authorized therapist writes their own clinical/workflow note about a child:

```
POST /api/v1/children/{child_id}/private-notes
```

## What a private note is, and what it is not

A note authored **by** the therapist **for** the therapist.

It is **not** a parent message, chat, reply, response to a `ParentNote`, shared
note, RTM work entry or billing documentation. Writing one notifies nobody,
creates no `ParentNote`, and changes no `ParentNote` state — neither
`review_status` nor `session_preparation_status`.

## The privacy boundary — the central guarantee

**Private means private to the AUTHOR**, and that is enforced in the
authoritative repository query, not in a projection. The frozen
`read_service.get_private_notes` filters on **both** `child_id` **and**
`therapist_id`, so another therapist's note never enters the result set to be
filtered out afterwards.

This phase preserves that by making the **authenticated** therapist the stored
author: `therapist_id` is resolved server-side from the principal and can never
be supplied, so a therapist cannot author a note attributed to someone else.

No parent-facing surface reads `private_therapist_notes` at all. Every
parent-safe projection is built from `ParentNote`. Tests prove the note never
appears in parent own-note history, the therapist `ParentNote` surface, the
cross-child `ParentNote` inbox, or any parent-reachable response — and that a
second therapist **with an active connection to the same child** still cannot
see it, so child access is demonstrably not what gates visibility.

## Request — an allow-list

```json
{ "body": "...", "marked_for_next_session": false }
```

`PrivateNoteCreateRequest` accepts exactly two fields. `id`, `child_id`,
`therapist_id`, `created_at`, `environment` and `schema_version` are absent BY
DESIGN — the child comes from the authorized path and the author from the
authenticated principal. A client sending any of them is simply ignored by the
model, and tests pin that a forged `therapist_id`, `child_id`, `id` or
`created_at` takes no effect.

`Idempotency-Key` is **required at runtime**: a missing or whitespace-only header
is rejected with `missing_idempotency_key` (400) before any state work.

### Response — `PrivateNoteCreateResponse`

```
note: { note_id, child_id, body, marked_for_next_session, created_at }
idempotent_replay
```

The body is echoed to its own author, who just typed it and whom the frozen
`GET /private-notes` already serves — so this creates no new exposure surface.
No `therapist_id`, audit id, idempotency internals, request hash, environment or
schema version.

## Authorization

Therapist-only, in the same order as every other therapist command:

1. `access.resolve_therapist` — role first. A parent receives `forbidden` (403),
   and authorization precedes body validation, so a parent sending a blank body
   still gets 403 and never a hint about the note's existence.
2. `access.require_full_access` — the existence-blind child check. An unknown
   child, another therapist's caseload, and **pending / paused / ended**
   connections all produce the **same** `not_found` (404). Write and the frozen
   read fail closed identically for all three non-active states.

## Append-only for the pilot

Once created, a private note is immutable. There is no edit, update, delete,
withdraw, archive or restore, and no route to reach any of those — the API still
has zero PUT/PATCH/DELETE. The `/private-notes` path exposes exactly `GET` and
`POST`.

The service writes the collection in exactly one place, creating a fresh id; it
never performs a read-modify-write on an existing note. A therapist who wants to
say something different writes another note, and the earlier one is provably
untouched.

## `marked_for_next_session` is chosen at creation

```
false (default)  ->  private history only
true             ->  private history AND the existing next-session surface
```

There is deliberately **no post-creation toggle**, in either direction. This is
NOT a second session-preparation workflow: `ParentNote.session_preparation_status`
is a separate, independently-owned dimension with its own named action (1B.3D),
and nothing here touches it.

Because the flag participates in the canonical request hash, replaying a key with
a different flag value is a conflict rather than a silent re-interpretation.

## Body validation follows the established convention

Empty or whitespace-only is rejected with `invalid_request` (**422**) — the same
canonical shape the frozen parent-note write uses, not a new code invented here.
Meaningful text is stored verbatim: no trimming, normalising or re-encoding.

**No maximum length is imposed.** Verified rather than assumed: there is no
`max_length`, `min_length`, `MAX_BODY*` or `MAX_LEN*` anywhere in `app/`, and
`parent_note_service` records the same reasoning — the project has no shared text
limit, and inventing one here would be a product decision this phase was not
asked to make. A test pins the absence by AST so the fact cannot silently change.

## Idempotency

* **Replay** — same key + identical request returns the stored result with
  `idempotent_replay: true`; no duplicate note, no duplicate audit event.
* **Conflict (409 `idempotency_key_conflict`)** — same key with a different
  body, a different `marked_for_next_session`, a different child, or a different
  action (e.g. a key already used for `/review`).

Ids bind the Idempotency-Key's **hash** via `operation_identity` /
`operation_scoped_id`, matching `parent_note_service`. A private note carries no
version and nothing about it increments, so the same therapist could legitimately
write the same text about the same child twice — a second observation worded
identically is a real second note. Seeding on the request hash alone would
silently overwrite the first; binding the key keeps distinct writes distinct
while an exact retry still replays. The note id is therefore deterministic for a
given operation.

## Audit — deliberately WITHOUT the note body

Exactly one immutable `private_therapist_note_created` event per real creation.

It records `private_note_id`, `note_exists`, `child_id`,
`marked_for_next_session`, `authored_by_role`, `authored_by_therapist_id` and
`visibility: therapist_private`, plus actor, role, subject, request id,
environment and a **hashed** idempotency key.

It does **not** record `body`. Provenance needs to answer *who wrote what kind of
thing, about whom, when* — not *what the clinical text said*. The existing audit
design already works this way (no service writes `body` into
`before_state`/`after_state`), so nothing had to change architecturally.
Duplicating private clinical free text into an append-only, broadly readable
audit stream would widen exposure for no provenance gain.

**The body therefore lives in exactly two places**: the note record itself, and
the response echoed to its own author — which is also stored in that request's
idempotency record so a replay returns an identical result, exactly as the frozen
parent-note write does. Tests sweep the entire audit collection to prove no event
contains the text.

## Rollback

Note, audit event and idempotency record are written inside one transaction. An
injected failure leaves no note, no audit event, no idempotency record and every
watched collection byte-identical — which also means the key remains reusable for
a later successful attempt. Concurrency: six threads on one key produce exactly
one note and one audit event; six distinct keys produce six notes.

A write grows exactly three collections — `private_therapist_notes`,
`audit_events`, `idempotency_records` — and nothing else.

## Read integration — no new read surface

* **`GET /children/{child_id}/private-notes`** returns the new note to its author
  and still contains the seeded fixtures.
* **`GET /children/{child_id}/next-session`** includes it in `private_note_items`
  when `marked_for_next_session` is true, and excludes it when false or omitted.
  `parent_note_items` is unchanged by a private write.

POST shares the existing `/private-notes` path, so OpenAPI gains an **operation,
not a path**: 24 paths unchanged, 25 → **26 operations**, 58 → **61 schemas**
(`PrivateNoteCreateRequest`, `PrivateNoteCreated`, `PrivateNoteCreateResponse`).
GETs remain read-only: reading never creates, and writing never leaves a read
receipt.

## Not implemented in this checkpoint

* Private-note **edit** / update
* Private-note **delete** / withdraw / archive / restore
* Post-creation **mark / unmark toggle** (either direction)
* Parent visibility of private notes
* Parent notification
* Therapist reply / chat / threading
* `DISCUSSED` transition
* Save for Later
* Replace
* Remove
* Firestore
* Firebase Auth
* RTM (no minutes, monitoring days, work entries, codes or billing
  documentation; later RTM work may *reference* a clinical action, but the two
  systems are not coupled here)
* Frontend integration
* Real data

# Sequence

1. **Parent Question / Note / Update write** ✅ Phase 1B.3A
2. **Parent reads own submitted notes** ✅ Phase 1B.3B
3. **Therapist Reviewed** ✅ Phase 1B.3C
4. **Therapist Discuss Next Session** ✅ Phase 1B.3D
5. **Therapist private-note writes** ✅ Phase 1B.3E

The core therapist collaboration backend is complete. Save for Later, Replace and
Remove remain deferred.
