"""pilot_runtime/integration/parent_gcs_source.py — the real Parent adapter.

Implements the 0.5A `ParentSessionSource` port over Parent 2.3's existing
session store. A translation layer and nothing more: Parent is not modified,
not imported, and never written to.

## The contract this reads, verbatim from Parent

`genex-parent/api/session_store.py` builds its blob name as

    sessions/{uid}/{session_id}.json

and `api/main.py::_require_session` then applies two rules: 404 when the blob
is absent, 403 when `doc["owner_uid"] != uid`. Both are reproduced. Nothing
about the layout is guessed — the path and the field name are read off the
Parent source, and a test pins the exact template so a Parent change that moved
it would fail here rather than silently return "no such session" forever.

## READ ONLY, structurally

This class exposes ONE public method and it is a read. There is no `save`,
`upload`, `delete`, `copy` or `patch`, and the port it satisfies declares none
either, so the capability is absent from the contract rather than merely
unused. `Blob.upload_from_string` and `.delete()` exist on the objects handled
here; nothing calls them, and a test asserts no method of this class names a
destructive or mutating operation.

The bucket is opened through `client.bucket(name)`, which performs no network
call and creates nothing. A non-existent bucket surfaces on the first read as a
translated error, never as a created bucket.

## Only two fields cross the boundary, out of a document full of clinical text

A Parent session document holds the intake answers, the generated plan, the
diagnosis and concern free-text, feedback, notes and completion history. This
adapter reads `owner_uid` and nothing else — the rest is discarded with the
parsed dict when the method returns.

That is not merely a matter of discipline. `ParentSessionFacts` has exactly two
fields, so there is nowhere for clinical content to go even if a future edit
tried to carry it. 0.5A bridges IDENTITY: no diagnosis, concern, plan,
feedback, note or answer reaches pilot Firestore, and a `Child` has no field
that could hold one.

## Nothing about a session reaches a log line

No logger is constructed, no `print` is made, and the SDK's exception text is
never re-raised or recorded. A GCS error can quote the full object path — which
contains the account uid and the session id — so `ParentSessionSourceError`
carries a constant message and declares `PHI_SAFE_MESSAGE`, exactly as
`DocumentStoreError` does for Firestore.

The session id is NOT echoed even into the error message, because an operator
reading logs does not need it and the pilot's logging guard would reject it.

## Absent and not-owned stay indistinguishable

A session outside the caller's own prefix is simply invisible: the blob name is
built from the requesting subject, so there is no request shape that reaches
another account's namespace. When the document IS readable but names a
different `owner_uid`, the facts are returned unjudged and the service refuses
— one decision point, one caller-visible outcome. See `parent_source.py`.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from pilot_backend.integration.parent_source import ParentSessionFacts

#: Parent's layout. Mirrored, not invented — see the module docstring.
SESSION_BLOB_TEMPLATE = "sessions/{uid}/{session_id}.json"

#: The document field Parent records ownership in.
OWNER_FIELD = "owner_uid"

#: Environment variable naming the Parent session bucket.
BUCKET_ENV_VAR = "PILOT_PARENT_SESSION_BUCKET"


class ParentSessionSourceError(Exception):
    """The Parent session store could not be read.

    PHI-safe by declaration and by construction: the message is a constant and
    never includes the object path, the session id, the account uid or the SDK's
    own text. A storage error is distinct from "no such session", which is
    `None` and not an error at all.
    """

    PHI_SAFE_MESSAGE = True


def session_blob_name(uid: str, session_id: str) -> str:
    """Parent's blob name for one session.

    Both components are validated rather than interpolated blindly: a
    `session_id` containing `/` or `..` would otherwise address a different
    prefix entirely, which is path traversal against the one control that keeps
    accounts apart.
    """
    for label, value in (("uid", uid), ("session id", session_id)):
        text = (value or "").strip()
        if not text:
            raise ParentSessionSourceError(f"a {label} is required")
        if "/" in text or "\\" in text or ".." in text:
            raise ParentSessionSourceError(
                f"a {label} must not contain a path separator")
    return SESSION_BLOB_TEMPLATE.format(
        uid=uid.strip(), session_id=session_id.strip())


class GcsParentSessionSource:
    """Read-only `ParentSessionSource` over Parent 2.3's GCS session store."""

    def __init__(self, client, bucket_name: str) -> None:
        if not (bucket_name or "").strip():
            raise ParentSessionSourceError("a bucket name is required")
        self._client = client
        self._bucket_name = bucket_name.strip()

    # -- the only operation ------------------------------------------------

    def fetch_session_facts(self, session_id: str, *, requesting_subject: str
                            ) -> Optional[ParentSessionFacts]:
        """Identity facts for one session in the CALLER's own namespace.

        `requesting_subject` is the verified token subject and is used only to
        build the blob name. It is never substituted for the document's own
        `owner_uid`: that value is returned verbatim so the service — the single
        decision point — can refuse a document that disagrees with where it is
        filed.
        """
        blob_name = session_blob_name(requesting_subject, session_id)

        try:
            bucket = self._client.bucket(self._bucket_name)
            blob = bucket.blob(blob_name)
            if not blob.exists():
                # Genuine not-found, or a session in another namespace. One
                # outcome for both, deliberately.
                return None
            payload = blob.download_as_text()
        except Exception as exc:  # noqa: BLE001 - translated, never propagated
            # The SDK's text can quote the full object path, which contains the
            # account uid and the session id. It is dropped entirely.
            raise ParentSessionSourceError(
                "the Parent session store could not be read") from None

        try:
            document = json.loads(payload)
        except (ValueError, TypeError):
            # A malformed document is a storage fault, not a missing session:
            # returning None would quietly invite a SECOND canonical child for
            # a session that does exist.
            raise ParentSessionSourceError(
                "a Parent session document could not be parsed") from None

        if not isinstance(document, dict):
            raise ParentSessionSourceError(
                "a Parent session document was not an object")

        owner = document.get(OWNER_FIELD)
        if not isinstance(owner, str) or not owner.strip():
            # Parent writes `owner_uid` on every session. Its absence means the
            # contract has moved, and guessing the caller owns it would be the
            # one guess that must never be made.
            raise ParentSessionSourceError(
                "a Parent session document carried no owner")

        # Two fields. Everything else in `document` — answers, plan, diagnosis,
        # concern, feedback, notes, history — goes out of scope here, unread.
        return ParentSessionFacts(session_id=session_id.strip(),
                                  owner_uid=owner.strip())


def build_parent_session_source(*, bucket_name: Optional[str] = None,
                                client=None) -> GcsParentSessionSource:
    """Composition root for the Parent boundary.

    The bucket is explicit or comes from `PILOT_PARENT_SESSION_BUCKET`. There
    is no default and no fallback: a misconfigured deployment must fail to
    start rather than silently read the wrong project's sessions.

    Parent's own store falls back to `/tmp` when GCS is unconfigured. That
    behaviour is NOT mirrored — a pilot that silently read sessions from local
    disk would be a different system of record, and the dual-store boundary is
    an open decision (see SECURITY.md).
    """
    name = bucket_name if bucket_name is not None else os.environ.get(
        BUCKET_ENV_VAR, "")
    if not (name or "").strip():
        raise ParentSessionSourceError(
            f"{BUCKET_ENV_VAR} must name the Parent session bucket")

    if client is None:
        from google.cloud import storage  # lazy, like Parent's own store

        client = storage.Client()
    return GcsParentSessionSource(client, name)
