"""Firestore adapter unit tests — the failure modes an emulator won't produce.

The emulator proves the happy paths and the store semantics. It will not
readily produce `PermissionDenied`, `DeadlineExceeded`, `ServiceUnavailable`
or `ResourceExhausted`, and those are exactly the errors whose SDK text is
most likely to name a project, a document path or an IAM principal.

So this file drives the real `FirestoreDocumentStore` over a fake client that
raises each of them on demand, and asserts that nothing the SDK said survives
the boundary.
"""

from __future__ import annotations

import pytest
from google.api_core import exceptions as gcloud_exceptions

from pilot_backend.persistence.document_store import DocumentStore, DocumentStoreError
from pilot_runtime.persistence.firestore_store import (
    EMULATOR_ENV_VAR,
    FirestoreDocumentStore,
    emulator_host_from,
)

from .test_sentinels import ALL_SENTINELS, SENTINEL_NOTE, SENTINEL_SECRET

LEAKY = f"project genex-real-prod doc {SENTINEL_NOTE} key {SENTINEL_SECRET}"


class FakeSnapshot:
    def __init__(self, data=None) -> None:
        self._data = data
        self.exists = data is not None
        self.id = "doc-1"

    def to_dict(self):
        return dict(self._data or {})


class FakeDocument:
    def __init__(self, raises=None, data=None) -> None:
        self._raises = raises
        self._data = data

    def _maybe_raise(self):
        if self._raises is not None:
            raise self._raises

    def create(self, data):
        self._maybe_raise()

    def get(self):
        self._maybe_raise()
        return FakeSnapshot(self._data)

    def set(self, data):
        self._maybe_raise()


class FakeCollection:
    def __init__(self, raises=None, data=None, rows=None) -> None:
        self._raises = raises
        self._data = data
        self._rows = rows or []

    def document(self, doc_id):
        return FakeDocument(self._raises, self._data)

    def where(self, filter=None):  # noqa: A002 - mirrors the SDK signature
        return self

    def stream(self):
        if self._raises is not None:
            raise self._raises
        return iter(self._rows)


class FakeClient:
    def __init__(self, raises=None, data=None, rows=None) -> None:
        self._raises = raises
        self._data = data
        self._rows = rows or []

    def collection(self, name):
        return FakeCollection(self._raises, self._data, self._rows)


def store_raising(error) -> FirestoreDocumentStore:
    return FirestoreDocumentStore(FakeClient(raises=error))


SDK_ERRORS = [
    gcloud_exceptions.PermissionDenied(LEAKY),
    gcloud_exceptions.DeadlineExceeded(LEAKY),
    gcloud_exceptions.ServiceUnavailable(LEAKY),
    gcloud_exceptions.ResourceExhausted(LEAKY),
    gcloud_exceptions.InvalidArgument(LEAKY),
    gcloud_exceptions.FailedPrecondition(LEAKY),
    gcloud_exceptions.Aborted(LEAKY),
]


# ===========================================================================
# the port is satisfied
# ===========================================================================

def test_the_adapter_satisfies_the_document_store_port():
    assert isinstance(FirestoreDocumentStore(FakeClient()), DocumentStore)


def test_a_client_is_required():
    with pytest.raises(DocumentStoreError):
        FirestoreDocumentStore(None)


# ===========================================================================
# SDK exception translation
# ===========================================================================

@pytest.mark.parametrize("error", SDK_ERRORS, ids=lambda e: type(e).__name__)
@pytest.mark.parametrize("operation", ["create", "get", "set", "query", "list"])
def test_every_sdk_error_is_translated_and_its_text_discarded(error, operation):
    store = store_raising(error)
    calls = {
        "create": lambda: store.create("pilot_practices", "d1", {"a": "1"}),
        "get": lambda: store.get("pilot_practices", "d1"),
        "set": lambda: store.set("pilot_practices", "d1", {"a": "1"}),
        "query": lambda: store.query_equals("pilot_practices", "a", "1"),
        "list": lambda: store.list_all("pilot_practices"),
    }
    with pytest.raises(DocumentStoreError) as raised:
        calls[operation]()

    message = str(raised.value)
    for sentinel in ALL_SENTINELS:
        assert sentinel not in message
    assert "genex-real-prod" not in message
    assert raised.value.__cause__ is None, "the SDK exception must be dropped"


def test_already_exists_becomes_the_ports_duplicate_error():
    store = store_raising(gcloud_exceptions.AlreadyExists(LEAKY))
    with pytest.raises(DocumentStoreError) as raised:
        store.create("pilot_practices", "d1", {"a": "1"})
    assert "already exists" in str(raised.value)
    assert SENTINEL_NOTE not in str(raised.value)


def test_not_found_on_read_returns_none_rather_than_raising():
    store = store_raising(gcloud_exceptions.NotFound(LEAKY))
    assert store.get("pilot_practices", "d1") is None


def test_a_missing_document_reads_as_none():
    assert FirestoreDocumentStore(FakeClient(data=None)).get("pilot_practices", "d1") is None


def test_errors_are_phi_safe_by_declaration():
    assert getattr(DocumentStoreError, "PHI_SAFE_MESSAGE", False) is True


# ===========================================================================
# collection namespace guard
# ===========================================================================

@pytest.mark.parametrize("collection", [
    "sessions", "users", "genex-api-dev-sessions-genex-mvp-2026",
    "", "   ", "pilot_practices/nested", "../pilot_practices",
])
def test_non_pilot_collections_are_refused(collection):
    store = FirestoreDocumentStore(FakeClient(data={"a": "1"}))
    for call in (lambda: store.create(collection, "d1", {}),
                 lambda: store.get(collection, "d1"),
                 lambda: store.set(collection, "d1", {}),
                 lambda: store.query_equals(collection, "a", "1"),
                 lambda: store.list_all(collection)):
        with pytest.raises(DocumentStoreError):
            call()


def test_empty_document_ids_are_refused():
    store = FirestoreDocumentStore(FakeClient(data={"a": "1"}))
    for doc_id in ("", "   ", None):
        with pytest.raises(DocumentStoreError):
            store.get("pilot_practices", doc_id)


# ===========================================================================
# set() is not an upsert
# ===========================================================================

def test_set_refuses_a_missing_document():
    """Firestore's set() would create one; the port's set() must not."""
    store = FirestoreDocumentStore(FakeClient(data=None))
    with pytest.raises(DocumentStoreError) as raised:
        store.set("pilot_practices", "d1", {"a": "1"})
    assert "does not exist" in str(raised.value)


# ===========================================================================
# ordering
# ===========================================================================

def test_query_results_are_sorted_by_document_id():
    rows = [FakeSnapshot({"n": i}) for i in range(3)]
    for snapshot, doc_id in zip(rows, ["c", "a", "b"]):
        snapshot.id = doc_id
    store = FirestoreDocumentStore(FakeClient(rows=rows))
    assert [doc_id for doc_id, _ in
            store.query_equals("pilot_practices", "n", 1)] == ["a", "b", "c"]
    assert [doc_id for doc_id, _ in store.list_all("pilot_practices")] == ["a", "b", "c"]


# ===========================================================================
# no destructive surface
# ===========================================================================

def test_no_public_method_names_a_destructive_operation():
    store = FirestoreDocumentStore(FakeClient())
    banned = ("delete", "remove", "purge", "drop", "destroy", "erase", "truncate")
    for attribute in dir(store):
        if attribute.startswith("_"):
            continue
        assert not any(word in attribute.lower() for word in banned), attribute


def test_the_adapter_source_never_calls_delete():
    import ast
    import inspect

    import pilot_runtime.persistence.firestore_store as module

    tree = ast.parse(inspect.getsource(module))
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert "delete" not in called


# ===========================================================================
# emulator wiring is explicit
# ===========================================================================

def test_emulator_host_is_read_explicitly_not_assumed():
    assert emulator_host_from({}) == ""
    assert emulator_host_from({EMULATOR_ENV_VAR: " 127.0.0.1:8080 "}) == "127.0.0.1:8080"


def test_building_a_client_requires_a_project_id():
    from pilot_runtime.persistence.firestore_store import build_firestore_client

    for project in ("", "   "):
        with pytest.raises(DocumentStoreError):
            build_firestore_client(project_id=project)


def test_building_a_client_never_leaves_the_emulator_variable_set():
    """Regression: the helper used to mutate the process environment forever.

    A permanently-set FIRESTORE_EMULATOR_HOST would silently redirect every
    later Firestore client in the process — after the composition root's guard
    against that very variable had already passed at startup.
    """
    import os

    from pilot_runtime.persistence.firestore_store import build_firestore_client

    os.environ.pop(EMULATOR_ENV_VAR, None)
    try:
        build_firestore_client(project_id="demo-genex-pilot",
                               emulator_host="127.0.0.1:1")
    except Exception:
        pass
    assert EMULATOR_ENV_VAR not in os.environ, "construction leaked a global env var"


def test_a_pre_existing_emulator_variable_is_restored_not_clobbered():
    import os

    from pilot_runtime.persistence.firestore_store import build_firestore_client

    os.environ[EMULATOR_ENV_VAR] = "127.0.0.1:9999"
    try:
        try:
            build_firestore_client(project_id="demo-genex-pilot",
                                   emulator_host="127.0.0.1:1")
        except Exception:
            pass
        assert os.environ[EMULATOR_ENV_VAR] == "127.0.0.1:9999"
    finally:
        os.environ.pop(EMULATOR_ENV_VAR, None)


def test_the_production_path_never_touches_the_process_environment():
    import os

    from pilot_runtime.persistence.firestore_store import build_firestore_client

    os.environ.pop(EMULATOR_ENV_VAR, None)
    try:
        build_firestore_client(project_id="genex-pilot-prod")
    except Exception:
        pass
    assert EMULATOR_ENV_VAR not in os.environ
