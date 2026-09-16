"""Therapist marks a parent-submitted note REVIEWED (Phase 1B.3C).

Fictional, in-memory, dev-only. No Firestore, no Firebase Auth, no Cloud Run, no
frontend, no real data. The atomic critical section is `repo.run_in_transaction`
(snapshot/restore rollback), shaped to map onto a future Firestore transaction.

## What Reviewed means, and what it does not

    review_status:  new  ->  reviewed

It means exactly one thing: *the therapist has reviewed this parent-submitted
item*. It does NOT mean the therapist replied, answered, agreed, resolved the
item, discussed it in session, marked it for the next session, or that the
parent received a response. There is still no reply, thread, conversation or
message state anywhere in this service, and none may be added.

## Content is immutable; workflow state is not

`ParentNote` is append-only in the sense that matters: the parent's SUBMISSION
is immutable and the note's existence is permanent. Nobody may edit, delete,
withdraw, retract or replace it, and this service changes none of

    id · child_id · parent_id · note_type · body · linked_assignment_id ·
    linked_activity_title · created_at · environment · schema_version

`review_status` and `session_preparation_status` are different in kind: they are
SYSTEM/THERAPIST-owned workflow metadata describing what the *therapist* has
done. Advancing one of them is not editing the parent's note.

## Why this mutates the stored dict rather than rebuilding the model

`repo.set` replaces a whole document. Rebuilding the record as
`ParentNote(**doc).model_dump()` would re-materialize every field — refilling
defaults for anything absent and re-coercing representations — which is exactly
how an immutable parent-authored field gets silently rewritten by a workflow
action that had no business touching it.

So the transaction takes the authoritative stored dict, assigns ONE key, and
writes it back. Every other key is carried across by identity. `session_
preparation_status` is not read-modify-written, not defaulted and not
normalized: it is simply never assigned.

## The two dimensions stay independent

`review_status` and `session_preparation_status` are two fields with two enums,
never collapsed. `reviewed` is not a member of `SessionPreparationStatus` and no
`SessionPreparationStatus` value is a member of `ParentNoteReviewStatus`. This
phase advances the first and must never touch the second — Discuss Next Session
is a separate, later, deliberate therapist action.

## Idempotent twice over

* **Key-bound** — a replayed `Idempotency-Key` returns the stored result and
  performs no second transition; the same key aimed at a different note, child
  or action is a 409 conflict.
* **Semantic** — an already-REVIEWED note returns success under a brand-new key
  too. Reviewing is a destination, not a counter: a note transitions to REVIEWED
  at most once, emits exactly one transition audit event ever, and there is
  deliberately no `review_count` and no record of repeat reviews.

A monotonic single-field transition needs no `version` and no `updated_at`: the
read and the write happen inside one critical section, so there is no lost
update to detect, and `updated_at` would put a mutable timestamp on a record
whose content is immutable. Neither field was added.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..auth.interface import AuthenticatedUser
from ..domain.audit_state import plain
from ..domain.enums import ParentNoteReviewStatus, PrincipalRole
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
from .approval_service import IdempotencyKeyConflict, MissingIdempotencyKey

ACTION = "mark_parent_note_reviewed"
EVENT_TYPE = "parent_note_reviewed"

#: The only transition this service performs.
FROM_STATUS = ParentNoteReviewStatus.NEW
TO_STATUS = ParentNoteReviewStatus.REVIEWED

#: The parent-authored fields this action must never touch. Pinned by tests.
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
    # Therapist-owned, but owned by a DIFFERENT action — see Discuss Next Session.
    "session_preparation_status",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result(note: dict, replay: bool) -> dict:
    """Therapist-safe confirmation of the action. Allow-list, not a projection.

    Only what a therapist UI needs to render the outcome: which note, and both
    workflow dimensions — the second one included precisely so a caller can SEE
    that reviewing did not disturb it. No audit id, no idempotency internals, no
    operation identity, no parent-authored content, no other family's data.
    """
    return {
        "note_id": note["id"],
        "review_status": plain(note["review_status"]),
        "session_preparation_status": plain(note["session_preparation_status"]),
        "idempotent_replay": replay,
    }


def mark_note_reviewed(
    repo: CollaborationRepository,
    user: AuthenticatedUser,
    child_id: str,
    note_id: str,
    idempotency_key: Optional[str],
    environment: str = "dev",
    request_id: Optional[str] = None,
) -> dict:
    """A therapist marks one parent note reviewed. Raises typed errors."""
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
    # guarantee is enforced by the `already_reviewed` guard below — this id is
    # DEFENCE IN DEPTH: a note may be reviewed at most once, so even if the
    # guard were ever bypassed, the single transition event would land on the
    # same deterministic document rather than accumulating duplicates.
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
        already_reviewed = plain(note["review_status"]) == TO_STATUS.value

        if not already_reviewed:
            before_review = plain(note["review_status"])
            session_before = plain(note["session_preparation_status"])

            # 3. THE transition. One key assigned on the authoritative stored
            #    dict — the model is deliberately NOT reconstructed, so every
            #    other field (including session_preparation_status) is carried
            #    across untouched rather than re-derived.
            note["review_status"] = TO_STATUS
            tx.set(C.PARENT_NOTES, note_id, note)

            # 4. Exactly one immutable transition audit event. Named for what
            #    happened to the note — NOT message_read / message_seen /
            #    reply_sent / note_resolved / discussion_completed, none of
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
                    "review_status": before_review,
                    # Recorded on BOTH sides so the record itself proves the
                    # session dimension did not move.
                    "session_preparation_status": session_before,
                    "note_author_parent_id": note["parent_id"],
                    "reviewed_by_therapist_id": therapist["id"],
                },
                after_state={
                    "parent_note_id": note_id,
                    "review_status": plain(note["review_status"]),
                    "session_preparation_status": plain(note["session_preparation_status"]),
                    "note_author_parent_id": note["parent_id"],
                    "reviewed_by_therapist_id": therapist["id"],
                },
                request_id=request_id,
                occurred_at=now,
                created_at=now,
                environment=environment,
            )
            tx.set(C.AUDIT_EVENTS, transition_audit_id, audit.model_dump())

        # 5. An already-reviewed note is a SUCCESSFUL no-op, not a conflict.
        #    Reviewing is a destination: the caller asked for the note to be
        #    reviewed, and it is. No second transition, no second audit event,
        #    no counter — and nothing about the note is rewritten.
        result = _result(note, replay=False)

        tx.set(C.IDEMPOTENCY_RECORDS, rec_id, IdempotencyRecord(
            id=rec_id, idempotency_key_hash=key_hash(key), actor_user_id=user.uid,
            action=ACTION, child_id=child_id,
            assignment_id=note_id,        # the operation target IS the note
            request_hash=req_hash, status="completed", result=result,
            # Null when this call was a semantic no-op: no event was emitted by
            # THIS request, and pointing at another request's event would
            # misattribute it.
            audit_event_id=None if already_reviewed else transition_audit_id,
            created_at=now, environment=environment,
        ).model_dump())
        return result

    return repo.run_in_transaction(op)
