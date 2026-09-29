"""pilot_runtime/persistence/firestore_store.py — the real Firestore adapter.

Implements the BACKEND 0.2 `DocumentStore` port over the official
`google-cloud-firestore` client, targeting Firestore **Native mode**. The
repositories, codecs and collection names above it are unchanged; this is a
translation layer and nothing more.

## Timestamp strategy — application-generated, not Firestore sentinels

This adapter deliberately does NOT use `firestore.SERVER_TIMESTAMP`. Two
reasons, and the second is the load-bearing one:

  * the codecs store timestamps as explicit ISO-8601 strings with an offset,
    and demand an exact key set and type on decode. A sentinel writes a
    Firestore timestamp, which decodes to a different type and breaks
    round-trip equality — the very property the strict codec exists to give;
  * a sentinel's value is not known until it is read back, so a write would
    have to be followed by a read to learn what was recorded. For an audit
    event that is a second round trip on the write path and a window in which
    the value is unknown to the process that just created it.

The audit requirement is that a CALLER cannot supply the timestamp, and that
holds: `AuditEvent.build` and the domain entities stamp `datetime.now(utc)` on
the server, with no request parameter reaching them. The timestamp is
server-generated; the server in question is the application, not the database.
This is tested against the emulator.

## SDK exceptions are translated, never propagated

Every `google.api_core` exception is caught at this boundary and re-raised as
`DocumentStoreError`, whose message names the collection and the operation and
never includes the SDK's text. A Firestore error can quote a document path, a
field value or a query, and this package's callers log exception messages only
for classes declaring `PHI_SAFE_MESSAGE` — so the SDK's own text must not
become one.

`NotFound` on a read is the exception to the rule, and it is not an error at
all: `get` returns None, matching the port.

## No delete

The port exposes no delete and neither does this class. `DocumentReference`
has a `.delete()` method; nothing here calls it, and a test asserts no public
method of this adapter names a destructive operation.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Dict, List, Mapping, Optional, Tuple

from google.api_core import exceptions as gcloud_exceptions
from google.cloud import firestore

from pilot_backend.persistence.collections import PILOT_COLLECTION_PREFIX
from pilot_backend.persistence.document_store import DocumentStoreError

#: Environment variable the Firestore client honours to reach an emulator.
#: Reading it is how this module can assert it is ABSENT in production.
EMULATOR_ENV_VAR = "FIRESTORE_EMULATOR_HOST"


def emulator_host_from(env: Mapping[str, str]) -> str:
    """The configured emulator host, or empty string. Explicit, never implicit."""
    return (env.get(EMULATOR_ENV_VAR) or "").strip()


@contextmanager
def _scoped_emulator_host(host: str):
    """Apply `FIRESTORE_EMULATOR_HOST` for one client construction, then restore.

    A no-op when `host` is empty, so the production path never touches the
    process environment at all.
    """
    endpoint = (host or "").strip()
    if not endpoint:
        yield
        return

    previous = os.environ.get(EMULATOR_ENV_VAR)
    os.environ[EMULATOR_ENV_VAR] = endpoint
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(EMULATOR_ENV_VAR, None)
        else:
            os.environ[EMULATOR_ENV_VAR] = previous


class FirestoreDocumentStore:
    """`DocumentStore` over a real Firestore client.

    The client is injected rather than constructed here, so tests can hand in
    an emulator-backed client and production can hand in a configured one
    without this class knowing which it has.
    """

    def __init__(self, client: "firestore.Client") -> None:
        if client is None:
            raise DocumentStoreError("FirestoreDocumentStore requires a client")
        self._client = client

    # -- guard -------------------------------------------------------------

    @staticmethod
    def _check_collection(collection: str) -> str:
        """Refuse any collection outside the pilot namespace.

        The repositories already resolve names through
        `pilot_backend.persistence.collections.collection_for`, which cannot
        compose a name from caller input. This is the second, independent
        check at the point where a real write would actually happen — the
        place where a mistake becomes data in someone else's collection.
        """
        name = (collection or "").strip()
        if not name.startswith(PILOT_COLLECTION_PREFIX):
            raise DocumentStoreError(
                f"refusing to address a collection outside the pilot namespace: {name}")
        if "/" in name:
            raise DocumentStoreError("collection names must not contain a path separator")
        return name

    def _doc(self, collection: str, doc_id: str):
        key = (doc_id or "").strip()
        if not key:
            raise DocumentStoreError("document id must not be empty")
        return self._client.collection(self._check_collection(collection)).document(key)

    # -- DocumentStore -----------------------------------------------------

    def create(self, collection: str, doc_id: str, data: Mapping[str, object]) -> None:
        """Create-if-absent. `AlreadyExists` becomes the port's duplicate error."""
        reference = self._doc(collection, doc_id)
        try:
            reference.create(dict(data))
        except gcloud_exceptions.AlreadyExists:
            raise DocumentStoreError(f"document already exists in {collection}") from None
        except gcloud_exceptions.GoogleAPIError:
            raise DocumentStoreError(f"create failed in {collection}") from None

    def get(self, collection: str, doc_id: str) -> Optional[Mapping[str, object]]:
        """Return the document or None. Absence is not an error."""
        reference = self._doc(collection, doc_id)
        try:
            snapshot = reference.get()
        except gcloud_exceptions.NotFound:
            return None
        except gcloud_exceptions.GoogleAPIError:
            raise DocumentStoreError(f"read failed in {collection}") from None
        if not snapshot.exists:
            return None
        return dict(snapshot.to_dict() or {})

    def set(self, collection: str, doc_id: str, data: Mapping[str, object]) -> None:
        """Whole-document overwrite of an EXISTING document.

        The port's `set` means "replace what is there", so a missing document
        is an error rather than an implicit insert. Firestore's `set()` is an
        upsert and would silently create one, which would let a status update
        against a wrong id manufacture a record instead of failing. The
        existence check restores the port's semantics.
        """
        reference = self._doc(collection, doc_id)
        try:
            if not reference.get().exists:
                raise DocumentStoreError(f"document does not exist in {collection}")
            reference.set(dict(data))
        except DocumentStoreError:
            raise
        except gcloud_exceptions.GoogleAPIError:
            raise DocumentStoreError(f"write failed in {collection}") from None

    def query_equals(self, collection: str, field: str,
                     value: object) -> List[Tuple[str, Mapping[str, object]]]:
        """Equality query, ordered by document id.

        Firestore does not promise an order for an unordered query, and the
        relationship-authorization path depends on deterministic listings, so
        the sort is applied here rather than assumed. Sorting by document id
        in the client also avoids requiring a composite index for every
        (field, __name__) pair, which would otherwise be an operational
        prerequisite the emulator would not reveal.
        """
        name = self._check_collection(collection)
        try:
            stream = (self._client.collection(name)
                      .where(filter=firestore.FieldFilter(field, "==", value))
                      .stream())
            rows = [(snapshot.id, dict(snapshot.to_dict() or {})) for snapshot in stream]
        except gcloud_exceptions.GoogleAPIError:
            raise DocumentStoreError(f"query failed in {collection}") from None
        return sorted(rows, key=lambda row: row[0])

    def list_all(self, collection: str) -> List[Tuple[str, Mapping[str, object]]]:
        name = self._check_collection(collection)
        try:
            rows = [(snapshot.id, dict(snapshot.to_dict() or {}))
                    for snapshot in self._client.collection(name).stream()]
        except gcloud_exceptions.GoogleAPIError:
            raise DocumentStoreError(f"list failed in {collection}") from None
        return sorted(rows, key=lambda row: row[0])


def build_firestore_client(*, project_id: str, database: str = "",
                           emulator_host: str = "") -> "firestore.Client":
    """Construct a Firestore client explicitly.

    No credentials are embedded and none are read from a key file. In
    production the client uses ambient credentials from the runtime service
    account — a deployment contract, not something this code supplies.

    `emulator_host` is threaded through explicitly rather than left to the
    ambient `FIRESTORE_EMULATOR_HOST`, so a test must ask for the emulator and
    production can assert it was never asked for. The client library still
    reads the environment variable itself; the composition root refuses to
    build a production store when it is set.
    """
    if not (project_id or "").strip():
        raise DocumentStoreError("Firestore requires an explicit project id")

    kwargs = {"project": project_id}
    if (database or "").strip():
        kwargs["database"] = database

    try:
        # The emulator endpoint is applied for the DURATION OF CONSTRUCTION
        # only, then the process environment is restored.
        #
        # An earlier version set `FIRESTORE_EMULATOR_HOST` permanently, which
        # was wrong in two ways. In tests it leaked between cases. In a real
        # process it would be far worse: a single process-global variable,
        # set as a side effect of building one client, silently redirecting
        # EVERY subsequent Firestore client to an emulator — while the
        # composition root's guard against exactly that variable would have
        # already passed at startup.
        #
        # The variable cannot simply be avoided: the emulator speaks plaintext
        # and passing `client_options={"api_endpoint": ...}` makes the client
        # attempt TLS and fail the handshake. The supported mechanism is this
        # variable; scoping it is what makes using it safe. The constructed
        # client keeps its emulator channel afterwards, which is verified by
        # test.
        with _scoped_emulator_host(emulator_host):
            return firestore.Client(**kwargs)
    except Exception:
        # Credential resolution, transport setup and project inference all
        # raise from here, and their messages name environments, file paths
        # and sometimes the metadata server. Translated like every other SDK
        # failure so nothing untranslated reaches a caller or a log.
        raise DocumentStoreError(
            "Firestore client could not be constructed for the configured project"
        ) from None
