"""Therapist marks a parent note DISCUSS NEXT SESSION (Phase 1B.3D).

Fictional, in-memory, dev-only. No Firestore, no Firebase Auth, no Cloud Run, no
frontend, no real data. The atomic critical section is `repo.run_in_transaction`
(snapshot/restore rollback), shaped to map onto a future Firestore transaction.

## What Discuss Next Session means, and what it does not

    session_preparation_status:  none  ->  discuss_at_next_session

It means exactly one thing: *the therapist wants this parent-submitted item
surfaced for discussion during a future session*. It does NOT mean the therapist
replied, answered, resolved the question, that the parent was notified, that a
session was scheduled, that the discussion happened, or that the item is
complete. There is no reply, thread, conversation or message state anywhere in
this service, and none may be added.

## Two independent dimensions, and this service owns exactly one

`review_status` and `session_preparation_status` are two fields with two enums,
never collapsed. The frozen review service advances the FIRST and never assigns
the second; this service advances the SECOND and never assigns the first.

    review_status:               new  |  reviewed          <- not this service
    session_preparation_status:  none |  discuss_at_next_session | discussed

So all four combinations are valid and reachable, and the two commands compose in
either order to the same final state:

    new      / none  --discuss-->  new      / discuss_at_next_session
    reviewed / none  --discuss-->  reviewed / discuss_at_next_session

Marking for next session must never implicitly mark Reviewed, and this module
must never import or call the review service. `review_status` is listed in
`IMMUTABLE_NOTE_FIELDS` below precisely so the test suite can pin that.

## Content is immutable; workflow state is not

`ParentNote` is append-only in the sense that matters: the parent's SUBMISSION is
immutable and the note's existence is permanent. Nobody may edit, delete,
withdraw, retract or replace it, and this service changes none of

    id · child_id · parent_id · note_type · body · linked_assignment_id ·
    linked_activity_title · created_at · environment · schema_version

plus `review_status`, which is therapist-owned but owned by a DIFFERENT action.

## Why this mutates the stored dict rather than rebuilding the model

`repo.set` replaces a whole document. Rebuilding the record as
`ParentNote(**doc).model_dump()` would re-materialize every field — refilling
defaults for anything absent and re-coercing representations — which is exactly
how `review_status`, or an immutable parent-authored field, gets silently
rewritten by a workflow action that had no business touching it.

So the transaction takes the authoritative stored dict, assigns ONE key, and
writes it back. Every other key is carried across by identity. `review_status` is
not read-modify-written, not defaulted and not normalized: it is simply never
assigned.

## One-way only

This phase implements a single transition. There is deliberately NO unflag, NO
clear, NO `discuss_at_next_session -> none` reverse, and NO transition into
`discussed`.

`DISCUSSED` remains a declared domain value with no writer. A note already in
`discussed` has moved PAST next-session preparation, so re-flagging it would be a
reverse/re-open semantic this phase does not have: it fails closed with
`invalid_session_preparation_transition` (409) rather than silently succeeding or
quietly moving the note backwards. That is the smallest canonical error in the
existing `ApprovalError` family and needs no new HTTP plumbing.

## Idempotent twice over

* **Key-bound** — a replayed `Idempotency-Key` returns the stored result and
  performs no second transition; the same key aimed at a different note, child
  or action is a 409 conflict.
* **Semantic** — a note already marked `discuss_at_next_session` returns success
  under a brand-new key too. Flagging is a destination, not a counter: a note
  transitions to `discuss_at_next_session` at most once, emits exactly one
  transition audit event ever, and there is deliberately no `flag_count` and no
  record of repeat flags.

A monotonic single-field transition needs no `version` and no `updated_at`: the
read and the write happen inside one critical section, so there is no lost update
to detect, and `updated_at` would put a mutable timestamp on a record whose
content is immutable. Neither field was added.

## Read integration

No new read surface. `GET /children/{child_id}/next-session` already selects
parent notes by `session_preparation_status == discuss_at_next_session` at
request time, so it picks up a real transition immediately — that endpoint
previously reflected only seeded fixture state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..auth.interface import AuthenticatedUser
from ..domain.audit_state import plain
from ..domain.enums import PrincipalRole, SessionPreparationStatus
from ..domain.ids import (
    canonical_request_hash,
    derived_id,
    idempotency_doc_id,
    key_hash,
)
from ..domain.read_models import AuditEvent, IdempotencyRecord
from ..repository import collections as C
from ..repository.interface import CollaborationRepository
from . import access
from .approval_service import (
    ApprovalError,
    IdempotencyKeyConflict,
    MissingIdempotencyKey,
)

ACTION = "mark_parent_note_for_next_session"
EVENT_TYPE = "parent_note_marked_for_next_session"

#: The only transition this service performs.
FROM_STATUS = SessionPreparationStatus.NONE
TO_STATUS = SessionPreparationStatus.DISCUSS_AT_NEXT_SESSION
#: Declared, writer-less, and PAST this transition — see the module docstring.
TERMINAL_STATUS = SessionPreparationStatus.DISCUSSED

#: Fields this action must never touch. Pinned by tests.
IMMUTABLE_NOTE_FIELDS = (
    "id",
    "child_id",
    "parent_id",
    "note_type",
    "body",
    "linked_assignment_id",
    "linked_activity_title",
    "created_at",
    "environment",
    "schema_version",
    # Therapist-owned, but owned by a DIFFERENT action — see the review service.
    "review_status",
)


class InvalidSessionPreparationTransition(ApprovalError):
    """The note's session dimension cannot move to discuss_at_next_session."""

    code = "invalid_session_preparation_transition"
    http_status = 409


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result(note: dict, replay: bool) -> dict:
    """Therapist-safe confirmation of the action. Allow-list, not a projection.

    Only what a therapist UI needs to render the outcome: which note, and both
    workflow dimensions — `review_status` included precisely so a caller can SEE
    that flagging did not disturb it. No audit id, no idempotency internals, no
    operation identity, no parent-authored content, no other family's data.
    """
    return {
        "note_id": note["id"],
        "session_preparation_status": plain(note["session_preparation_status"]),
        "review_status": plain(note["review_status"]),
        "idempotent_replay": replay,
    }


def mark_note_for_next_session(
    repo: CollaborationRepository,
    user: AuthenticatedUser,
    child_id: str,
    note_id: str,
    idempotency_key: Optional[str],
    environment: str = "dev",
    request_id: Optional[str] = None,
) -> dict:
    """A therapist marks one parent note for next-session discussion."""
    key = (idempotency_key or "").strip()
    if not key:
        raise MissingIdempotencyKey("Idempotency-Key header is required.")

    # Authorization first, exactly as every other therapist command: role
    # (403 for a parent), then the existence-blind child check (404 for unknown,
    # another caseload, or a pending/paused/ended connection).
    therapist = access.resolve_therapist(repo, user)
    access.require_full_access(repo, therapist["id"], child_id)

    req_hash = canonical_request_hash(user.uid, ACTION, child_id, note_id, {})
    rec_id = idempotency_doc_id(key)

    # The audit id is derived from the NOTE, not from the key. The one-event
    # guarantee is enforced by the `already_flagged` guard below — this id is
    # DEFENCE IN DEPTH: a note may be flagged at most once, so even if the guard
    # were ever bypassed, the single transition event would land on the same
    # deterministic document rather than accumulating duplicates.
    transition_audit_id = derived_id("aud", EVENT_TYPE, note_id)

    def op(tx: CollaborationRepository) -> dict:
        # 1. Idempotency BEFORE state validation, matching every other write.
        if tx.exists(C.IDEMPOTENCY_RECORDS, rec_id):
            rec = tx.get(C.IDEMPOTENCY_RECORDS, rec_id)
            if rec["request_hash"] == req_hash:
                result = dict(rec["result"])
                result["idempotent_replay"] = True
                return result
            raise IdempotencyKeyConflict("Idempotency-Key reused for a different request.")

        # 2. Resolve the note UNDER the requested child. A note belonging to
        #    another child, an unknown id, or the id of some other object
        #    entirely (a private therapist note, a proposal, an assignment) all
        #    raise the same existence-blind 404 — `query` is scoped to
        #    PARENT_NOTES, so a non-note id simply does not resolve.
        rows = tx.query(C.PARENT_NOTES, id=note_id, child_id=child_id)
        if not rows:
            raise access.ChildNotFound(note_id)
        note = rows[0]

        now = _now()
        session_before = plain(note["session_preparation_status"])

        # 3. A note already PAST this transition fails closed rather than moving
        #    backwards. Checked before the no-op branch so `discussed` can never
        #    be mistaken for "already flagged".
        if session_before == TERMINAL_STATUS.value:
            raise InvalidSessionPreparationTransition(
                "Note has already been discussed; it cannot be marked for the next session."
            )

        already_flagged = session_before == TO_STATUS.value

        if not already_flagged:
            if session_before != FROM_STATUS.value:
                # Fail closed on any unrecognized stored value rather than
                # guessing what it meant.
                raise InvalidSessionPreparationTransition(
                    "Note is not in a state that can be marked for the next session."
                )
            review_before = plain(note["review_status"])

            # 4. THE transition. One key assigned on the authoritative stored
            #    dict — the model is deliberately NOT reconstructed, so every
            #    other field (including review_status) is carried across
            #    untouched rather than re-derived.
            note["session_preparation_status"] = TO_STATUS
            tx.set(C.PARENT_NOTES, note_id, note)

            # 5. Exactly one immutable transition audit event. Named for what
            #    happened to the note — NOT discussion_completed / note_resolved
            #    / reply_sent / parent_notified / session_scheduled, none of
            #    which this action performs.
            #
            #    The therapist is the ACTOR; the note's author is unchanged and
            #    is recorded separately, so the two roles stay distinguishable.
            audit = AuditEvent(
                id=transition_audit_id,
                event_type=EVENT_TYPE,
                actor_uid=user.uid,
                actor_role=PrincipalRole.THERAPIST,
                subject_type="parent_note",
                subject_id=note_id,
                child_id=child_id,
                assignment_id=note.get("linked_assignment_id"),
                idempotency_key_hash=key_hash(key),
                before_state={
                    "parent_note_id": note_id,
                    "session_preparation_status": session_before,
                    # Recorded on BOTH sides so the record itself proves the
                    # review dimension did not move.
                    "review_status": review_before,
                    "note_author_parent_id": note["parent_id"],
                    "marked_by_therapist_id": therapist["id"],
                },
                after_state={
                    "parent_note_id": note_id,
                    "session_preparation_status": plain(note["session_preparation_status"]),
                    "review_status": plain(note["review_status"]),
                    "note_author_parent_id": note["parent_id"],
                    "marked_by_therapist_id": therapist["id"],
                },
                request_id=request_id,
                occurred_at=now,
                created_at=now,
                environment=environment,
            )
            tx.set(C.AUDIT_EVENTS, transition_audit_id, audit.model_dump())

        # 6. An already-flagged note is a SUCCESSFUL no-op, not a conflict.
        #    Flagging is a destination: the caller asked for the note to be
        #    marked for the next session, and it is. No second transition, no
        #    second audit event, no counter — and nothing about the note is
        #    rewritten.
        result = _result(note, replay=False)

        tx.set(C.IDEMPOTENCY_RECORDS, rec_id, IdempotencyRecord(
            id=rec_id, idempotency_key_hash=key_hash(key), actor_user_id=user.uid,
            action=ACTION, child_id=child_id,
            assignment_id=note_id,        # the operation target IS the note
            request_hash=req_hash, status="completed", result=result,
            # Null when this call was a semantic no-op: no event was emitted by
            # THIS request, and pointing at another request's event would
            # misattribute it.
            audit_event_id=None if already_flagged else transition_audit_id,
            created_at=now, environment=environment,
        ).model_dump())
        return result

    return repo.run_in_transaction(op)
