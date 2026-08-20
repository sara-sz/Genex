"""Parent Question / Note / Update writes (Phase 1B.3A).

Fictional, in-memory, dev-only. No Firestore, no Firebase Auth, no Cloud Run, no
frontend, no real data. The atomic critical section is `repo.run_in_transaction`
(snapshot/restore rollback), shaped to map onto a future Firestore transaction.

## One-way, not a conversation

A `ParentNote` is a parent-authored item sent **into** the child's shared care
workspace. The therapist reads it. That is the whole interaction.

There is deliberately **no** `reply_to_note_id`, `thread_id`, `conversation_id`,
message status, read receipt, typing state, therapist reply, parent reply or
realtime channel — and none may be added. The domain object already says so in
its own docstring; this service is the write side of that same object.

The intended sequence, of which only the first step exists:

    Parent submits  ->  Therapist sees  ->  (later) Reviewed
                                        ->  (later) Discuss Next Session

## Three intents, one object

QUESTION / NOTE / UPDATE differ by **parent intent only**. They share this
route, this domain object, this authorization, this audit machinery and the
existing therapist read endpoint. There is no per-type collection, endpoint or
permission — anything else would make the type a routing concept rather than a
human one.

## What the system owns

`review_status` and `session_preparation_status` are set by the SYSTEM to their
canonical initial values (`NEW` / `NONE`) and can never be supplied by a parent:
they describe what the *therapist* has done, and at creation the therapist has
done nothing. They are also **independent** — a future Reviewed must not imply
Discuss Next Session, or vice versa — so they are stored as two fields and never
collapsed. Neither transition is implemented here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..auth.interface import AuthenticatedUser
from ..domain.audit_state import plain
from ..domain.enums import (
    AssignmentStatus,
    ParentNoteReviewStatus,
    ParentNoteType,
    PrincipalRole,
    SessionPreparationStatus,
)
from ..domain.ids import (
    canonical_request_hash,
    idempotency_doc_id,
    key_hash,
    operation_identity,
    operation_scoped_id,
)
from ..domain.read_models import AuditEvent, IdempotencyRecord, ParentNote
from ..repository import collections as C
from ..repository.interface import CollaborationRepository
from . import access
from .approval_service import IdempotencyKeyConflict, MissingIdempotencyKey
from .proposal_service import InvalidRequest

ACTION = "create_parent_note"
EVENT_TYPE = "parent_note_created"

#: Set by the system at creation — the therapist has done nothing yet.
INITIAL_REVIEW_STATUS = ParentNoteReviewStatus.NEW
INITIAL_SESSION_PREPARATION_STATUS = SessionPreparationStatus.NONE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def note_target_token(child_id: str, linked_assignment_id: Optional[str]) -> str:
    """Internal idempotency operation target for a note write.

    `IdempotencyRecord` requires an operation target, but a child-level note has
    no assignment. The token names what the note is about — the child, plus the
    linked activity when there is one. Internal only: never an assignment id,
    never written to a semantic audit field, never returned by any API.
    """
    return f"{child_id}:{linked_assignment_id or 'child'}"


def create_parent_note(
    repo: CollaborationRepository,
    user: AuthenticatedUser,
    child_id: str,
    idempotency_key: Optional[str],
    note_type: str,
    body: str,
    linked_assignment_id: Optional[str] = None,
    environment: str = "dev",
    request_id: Optional[str] = None,
) -> dict:
    """A parent submits one Question, Note or Update. Raises typed errors."""
    key = (idempotency_key or "").strip()
    if not key:
        raise MissingIdempotencyKey("Idempotency-Key header is required.")

    # Authorization first: parent role (403 for a therapist), then the
    # existence-blind child check (404 for unknown, another family, or a
    # pending/paused/ended connection).
    parent = access.resolve_parent(repo, user)
    access.require_parent_child_access(repo, parent["id"], child_id)

    # Static request validation — not state validation, so it precedes the
    # idempotency lookup exactly as the other write services validate their
    # enum-valued inputs.
    if note_type not in {t.value for t in ParentNoteType}:
        raise InvalidRequest(f"Unsupported note_type '{note_type}'.")
    # No maximum length is imposed: the project has no shared text limit, and
    # inventing one here would be a product decision this phase has not been
    # asked to make. Emptiness IS validated — a blank note carries nothing.
    if not (body or "").strip():
        raise InvalidRequest("body must not be empty.")

    linked_assignment_id = (linked_assignment_id or "").strip() or None

    target_token = note_target_token(child_id, linked_assignment_id)
    req_hash = canonical_request_hash(
        user.uid, ACTION, child_id, target_token,
        {"note_type": note_type, "body": body,
         "linked_assignment_id": linked_assignment_id},
    )
    rec_id = idempotency_doc_id(key)

    # Ids bind the Idempotency-Key's HASH. A note carries no version and nothing
    # about it increments, so the SAME parent could legitimately submit the same
    # text about the same activity twice — a second thought worded identically is
    # a real second note. Seeding on the request hash alone would silently
    # overwrite the first; binding the key keeps distinct submissions distinct
    # while an exact retry still replays.
    identity = operation_identity(
        key, user.uid, ACTION, child_id,
        proposal_id="",                        # notes are not proposals
        assignment_id=target_token,            # internal token, NOT an assignment id
    )
    note_id = operation_scoped_id("pn", identity)
    audit_id = operation_scoped_id("aud", identity)

    def op(tx: CollaborationRepository) -> dict:
        # 1. Idempotency BEFORE any state validation, matching every other write.
        if tx.exists(C.IDEMPOTENCY_RECORDS, rec_id):
            rec = tx.get(C.IDEMPOTENCY_RECORDS, rec_id)
            if rec["request_hash"] == req_hash:
                result = dict(rec["result"])
                result["idempotent_replay"] = True
                return result
            raise IdempotencyKeyConflict("Idempotency-Key reused for a different request.")

        # 2. Optional activity link, validated against authoritative state.
        #
        #    A note may be child-level. When it does name an activity, that
        #    activity must be one the family is CURRENTLY working on — a note
        #    about a retired activity would attach live context to something the
        #    plan no longer contains. Anything else (unknown, another child's,
        #    retired) raises the same existence-blind 404, so a parent cannot
        #    probe for assignments that are not theirs.
        linked_title = None
        if linked_assignment_id is not None:
            rows = tx.query(C.PLAN_ASSIGNMENTS, id=linked_assignment_id)
            if not rows or rows[0].get("child_id") != child_id:
                raise access.ChildNotFound(linked_assignment_id)
            assignment = rows[0]
            if plain(assignment["assignment_status"]) != AssignmentStatus.CURRENT.value:
                raise access.ChildNotFound(linked_assignment_id)
            # `linked_activity_title` is denormalized on ParentNote and is what
            # the therapist read renders, so it must be resolved at write time —
            # a link with no title would show the therapist an anonymous
            # reference.
            versions = tx.query(C.ACTIVITY_VERSIONS, id=assignment["activity_version_id"])
            linked_title = versions[0].get("title", "") if versions else ""

        if tx.exists(C.PARENT_NOTES, note_id):
            raise InvalidRequest("A note already exists for this operation.")

        now = _now()

        # 3. Exactly ONE ParentNote. Author, child, timestamps and both review
        #    dimensions are system-determined — never client-supplied.
        note = ParentNote(
            id=note_id,
            child_id=child_id,
            parent_id=parent["id"],
            note_type=ParentNoteType(note_type),
            body=body,
            review_status=INITIAL_REVIEW_STATUS,
            session_preparation_status=INITIAL_SESSION_PREPARATION_STATUS,
            linked_assignment_id=linked_assignment_id,
            linked_activity_title=linked_title,
            created_at=now,
            environment=environment,
        )
        tx.set(C.PARENT_NOTES, note_id, note.model_dump())

        # 4. Exactly one immutable audit event. `before_state` records that no
        #    note existed; `after_state` records what was created. Deliberately
        #    NOT named message_sent / chat_started / therapist_note_created —
        #    this is a parent submitting an item, not a conversation turn.
        before_state = {
            "parent_note_id": None,
            "note_exists": False,
            "child_id": child_id,
            "linked_assignment_id": linked_assignment_id,
        }
        after_state = {
            "parent_note_id": note_id,
            "note_exists": True,
            "child_id": child_id,
            "note_type": note_type,
            "linked_assignment_id": linked_assignment_id,
            "review_status": INITIAL_REVIEW_STATUS.value,
            "session_preparation_status": INITIAL_SESSION_PREPARATION_STATUS.value,
            "authored_by_role": PrincipalRole.PARENT.value,
        }
        audit = AuditEvent(
            id=audit_id, event_type=EVENT_TYPE, actor_uid=user.uid,
            actor_role=PrincipalRole.PARENT, subject_type="parent_note",
            subject_id=note_id, child_id=child_id,
            # The linked activity is a semantic assignment reference here, unlike
            # the internal token, so it belongs in this field.
            assignment_id=linked_assignment_id,
            idempotency_key_hash=key_hash(key),
            before_state=before_state, after_state=after_state,
            request_id=request_id, occurred_at=now, created_at=now,
            environment=environment,
        )
        tx.set(C.AUDIT_EVENTS, audit_id, audit.model_dump())

        result = {
            "note": {
                "note_id": note_id,
                "note_type": note_type,
                "body": body,
                "created_at": now,
                "review_status": INITIAL_REVIEW_STATUS.value,
                "session_preparation_status": INITIAL_SESSION_PREPARATION_STATUS.value,
                # Parent-safe activity context: the TITLE the parent already
                # sees, never the assignment, plan or version ids.
                "linked_activity_title": linked_title,
            },
            "idempotent_replay": False,
        }

        tx.set(C.IDEMPOTENCY_RECORDS, rec_id, IdempotencyRecord(
            id=rec_id, idempotency_key_hash=key_hash(key), actor_user_id=user.uid,
            action=ACTION, child_id=child_id,
            assignment_id=target_token,   # internal operation target
            request_hash=req_hash, status="completed", result=result,
            audit_event_id=audit_id, created_at=now, environment=environment,
        ).model_dump())
        return result

    return repo.run_in_transaction(op)
