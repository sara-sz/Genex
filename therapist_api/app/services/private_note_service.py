"""A therapist writes a PRIVATE note (Phase 1B.3E).

Fictional, in-memory, dev-only. No Firestore, no Firebase Auth, no Cloud Run, no
frontend, no real data. The atomic critical section is `repo.run_in_transaction`
(snapshot/restore rollback), shaped to map onto a future Firestore transaction.

## What a private note is, and what it is not

A therapist's own clinical/workflow note about a child. It is authored BY the
therapist FOR the therapist.

It is **not** a parent message, chat, reply, response to a `ParentNote`, shared
note, RTM work entry or billing documentation. Writing one notifies nobody,
creates no `ParentNote`, and changes no `ParentNote` state — not `review_status`
and not `session_preparation_status`.

## The privacy boundary

`PrivateTherapistNote` is visible ONLY to the authoring therapist, and that is
enforced in the authoritative repository query, not in a projection: the frozen
`read_service.get_private_notes` filters on BOTH `child_id` and `therapist_id`,
so another therapist's note never enters the result set to be filtered out
afterwards. This service preserves that by making the AUTHENTICATED therapist
the stored author — `therapist_id` is resolved server-side from the principal
and can never be supplied, so a therapist cannot author a note attributed to
someone else.

No parent-facing surface reads this collection at all. Parent-safe projections
are built from `ParentNote`, never from `PrivateTherapistNote`.

## Append-only for the pilot

Once created, a private note is immutable: no edit, update, delete, withdraw,
archive or restore, and no route to reach any of those — the API still has zero
PUT/PATCH/DELETE. A therapist who wants to say something different writes
another note.

## `marked_for_next_session` is chosen at creation

    false (default)  ->  private history only
    true             ->  private history AND the existing next-session surface

There is deliberately **no post-creation toggle**, in either direction. This is
NOT a second session-preparation workflow: `ParentNote.session_preparation_status`
is a separate, independently-owned dimension with its own named action, and
nothing here touches it.

Because the flag participates in the canonical request hash, replaying a key
with a different flag value is a conflict rather than a silent re-interpretation.

## Body validation follows the established convention

Empty or whitespace-only is rejected — a blank note carries nothing. **No maximum
length is imposed**: the project has no shared text limit (verified: there is no
`max_length` anywhere in `app/`), and `parent_note_service` records the same
reasoning. Inventing one here would be a product decision this phase was not
asked to make.

## The audit deliberately does NOT store the note body

Provenance needs to answer *who wrote what kind of thing, about whom, when* —
not *what the clinical text said*. The existing audit design already works this
way: no service writes `body` into `before_state`/`after_state`. Duplicating
private clinical free text into an append-only, broadly-readable audit stream
would widen exposure for no provenance gain, so the event records ids, the
session flag, roles and metadata only.

The body therefore lives in exactly one durable place — the note record itself —
plus the response echoed to its own author, which the frozen
`GET /children/{id}/private-notes` already returns to that same principal.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..auth.interface import AuthenticatedUser
from ..domain.enums import PrincipalRole
from ..domain.ids import (
    canonical_request_hash,
    idempotency_doc_id,
    key_hash,
    operation_identity,
    operation_scoped_id,
)
from ..domain.read_models import AuditEvent, IdempotencyRecord, PrivateTherapistNote
from ..repository import collections as C
from ..repository.interface import CollaborationRepository
from . import access
from .approval_service import IdempotencyKeyConflict, MissingIdempotencyKey
from .proposal_service import InvalidRequest

ACTION = "create_private_therapist_note"
EVENT_TYPE = "private_therapist_note_created"

#: Default when the therapist does not choose. A note is ordinary unless said so.
DEFAULT_MARKED_FOR_NEXT_SESSION = False

#: Fields the client may supply. Everything else is server-derived. Pinned by tests.
CLIENT_SUPPLIED_FIELDS = ("body", "marked_for_next_session")

#: Fields the server owns outright — a client value for any of these is ignored.
SERVER_DERIVED_FIELDS = (
    "id",
    "child_id",
    "therapist_id",
    "created_at",
    "environment",
    "schema_version",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def private_note_target_token(child_id: str) -> str:
    """Internal idempotency operation target for a private-note write.

    `IdempotencyRecord` requires an operation target, but a private note has no
    assignment and no proposal — it is about the child. Internal only: never an
    assignment id, never written to a semantic audit field, never returned by any
    API.
    """
    return f"{child_id}:private"


def create_private_note(
    repo: CollaborationRepository,
    user: AuthenticatedUser,
    child_id: str,
    idempotency_key: Optional[str],
    body: str,
    marked_for_next_session: Optional[bool] = None,
    environment: str = "dev",
    request_id: Optional[str] = None,
) -> dict:
    """A therapist writes one private note. Raises typed errors."""
    key = (idempotency_key or "").strip()
    if not key:
        raise MissingIdempotencyKey("Idempotency-Key header is required.")

    # Authorization first, exactly as every other therapist command: role
    # (403 for a parent), then the existence-blind child check (404 for unknown,
    # another caseload, or a pending/paused/ended connection).
    therapist = access.resolve_therapist(repo, user)
    access.require_full_access(repo, therapist["id"], child_id)

    # Emptiness IS validated — a blank note carries nothing. No maximum length is
    # imposed; see the module docstring.
    if not (body or "").strip():
        raise InvalidRequest("body must not be empty.")

    marked = bool(DEFAULT_MARKED_FOR_NEXT_SESSION
                  if marked_for_next_session is None else marked_for_next_session)

    target_token = private_note_target_token(child_id)
    # The flag participates in the request hash, so replaying a key with a
    # different flag is a conflict rather than a silent re-interpretation.
    req_hash = canonical_request_hash(
        user.uid, ACTION, child_id, target_token,
        {"body": body, "marked_for_next_session": marked},
    )
    rec_id = idempotency_doc_id(key)

    # Ids bind the Idempotency-Key's HASH. A private note carries no version and
    # nothing about it increments, so the SAME therapist could legitimately write
    # the same text about the same child twice — a second observation worded
    # identically is a real second note. Seeding on the request hash alone would
    # silently overwrite the first; binding the key keeps distinct writes
    # distinct while an exact retry still replays.
    identity = operation_identity(
        key, user.uid, ACTION, child_id,
        proposal_id="",                        # private notes are not proposals
        assignment_id=target_token,            # internal token, NOT an assignment id
    )
    note_id = operation_scoped_id("ptn", identity)
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

        now = _now()

        # 2. The note. `therapist_id` is the AUTHENTICATED therapist, resolved
        #    server-side — a client cannot author a note attributed to anyone
        #    else, and `child_id` comes from the authorized path, not the body.
        note = PrivateTherapistNote(
            id=note_id,
            child_id=child_id,
            therapist_id=therapist["id"],
            body=body,
            marked_for_next_session=marked,
            created_at=now,
            environment=environment,
        )
        tx.set(C.PRIVATE_THERAPIST_NOTES, note_id, note.model_dump())

        # 3. Exactly one immutable creation audit event. Named for what happened
        #    — NOT message_sent / chat_started / parent_notified / reply_sent,
        #    none of which this action performs.
        #
        #    NO `body`. Provenance records who wrote what kind of thing, about
        #    whom, when — never the clinical text. See the module docstring.
        before_state = {
            "private_note_id": None,
            "note_exists": False,
            "child_id": child_id,
        }
        after_state = {
            "private_note_id": note_id,
            "note_exists": True,
            "child_id": child_id,
            "marked_for_next_session": marked,
            "authored_by_role": PrincipalRole.THERAPIST.value,
            "authored_by_therapist_id": therapist["id"],
            "visibility": "therapist_private",
        }
        audit = AuditEvent(
            id=audit_id,
            event_type=EVENT_TYPE,
            actor_uid=user.uid,
            actor_role=PrincipalRole.THERAPIST,
            subject_type="private_therapist_note",
            subject_id=note_id,
            child_id=child_id,
            idempotency_key_hash=key_hash(key),
            before_state=before_state,
            after_state=after_state,
            request_id=request_id,
            occurred_at=now,
            created_at=now,
            environment=environment,
        )
        tx.set(C.AUDIT_EVENTS, audit_id, audit.model_dump())

        # 4. Therapist-safe confirmation. The author already has this text, and
        #    the frozen private-notes GET returns it to this same principal, so
        #    echoing it creates no new exposure surface.
        result = {
            "note": {
                "note_id": note_id,
                "child_id": child_id,
                "body": body,
                "marked_for_next_session": marked,
                "created_at": now,
            },
            "idempotent_replay": False,
        }

        tx.set(C.IDEMPOTENCY_RECORDS, rec_id, IdempotencyRecord(
            id=rec_id, idempotency_key_hash=key_hash(key), actor_user_id=user.uid,
            action=ACTION, child_id=child_id,
            assignment_id=target_token,     # internal token, never an assignment id
            request_hash=req_hash, status="completed", result=result,
            audit_event_id=audit_id,
            created_at=now, environment=environment,
        ).model_dump())
        return result

    return repo.run_in_transaction(op)
