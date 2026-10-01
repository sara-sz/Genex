"""0.5A — the real Parent GCS session adapter.

Driven through a fake that implements the `google.cloud.storage` surface the
adapter actually touches: `client.bucket()`, `bucket.blob()`, `blob.exists()`
and `blob.download_as_text()`. The adapter under test is the real one; only the
transport is substituted, the same relationship `test_firestore_adapter.py` has
to `FirestoreDocumentStore`.

Documents here are shaped like real Parent sessions — including the clinical
fields a real one carries — using FICTIONAL sentinel values. That is the point:
the suite proves those fields are not read, not returned and not logged, which
cannot be shown with a two-key document.
"""

from __future__ import annotations

import json

import pytest

from pilot_backend.integration.parent_source import (
    ParentSessionFacts,
    ParentSessionSource,
)
from pilot_runtime.integration.parent_gcs_source import (
    BUCKET_ENV_VAR,
    OWNER_FIELD,
    SESSION_BLOB_TEMPLATE,
    GcsParentSessionSource,
    ParentSessionSourceError,
    build_parent_session_source,
    session_blob_name,
)

from .test_sentinels import ALL_SENTINELS

BUCKET = "fictional-parent-sessions"
UID_A = "fictional-uid-alpha"
UID_B = "fictional-uid-beta"
SESSION_A = "fictional-session-alpha-0001"

#: A realistically-shaped Parent session document. Every clinical value is a
#: sentinel, so a leak is detectable rather than merely unlikely.
SENTINEL_CHILD_NAME = "ZZSENTINEL-CHILDNAME-Quillwood"
SENTINEL_CONCERN = "ZZSENTINEL-CONCERN-not-speaking-in-sentences"
SENTINEL_DIAGNOSIS = "ZZSENTINEL-DIAGNOSIS-fictional-condition"
SENTINEL_NOTE = "ZZSENTINEL-NOTE-refuses-solids-at-dinner"

LOCAL_SENTINELS = (SENTINEL_CHILD_NAME, SENTINEL_CONCERN,
                   SENTINEL_DIAGNOSIS, SENTINEL_NOTE)


def parent_session_document(*, owner_uid=UID_A, session_id=SESSION_A) -> dict:
    """The shape Parent actually stores, with fictional clinical content."""
    return {
        "session_id": session_id,
        OWNER_FIELD: owner_uid,
        "timezone": "America/New_York",
        "child_name": SENTINEL_CHILD_NAME,
        "child_age_months": 31,
        "primary_concern": SENTINEL_CONCERN,
        "diagnosis": SENTINEL_DIAGNOSIS,
        "answers": {"q1": "yes", "q2": "not_yet"},
        "plan": {"current_week_plan": [{"day": 1, "activities": ["a", "b"]}]},
        "feedback": [{"activity": "a", "note": SENTINEL_NOTE}],
        "completion_history": [{"activity": "a", "stars": 3}],
    }


# ===========================================================================
# the fake transport
# ===========================================================================

class _FakeBlob:
    def __init__(self, bucket, name: str) -> None:
        self._bucket = bucket
        self.name = name
        #: Every read is recorded, so a test can assert WHICH path was asked
        #: for — the single control that keeps accounts apart.
        bucket.touched.append(name)

    def exists(self) -> bool:
        if self._bucket.exists_raises is not None:
            raise self._bucket.exists_raises
        return self.name in self._bucket.objects

    def download_as_text(self) -> str:
        if self._bucket.download_raises is not None:
            raise self._bucket.download_raises
        self._bucket.downloaded.append(self.name)
        return self._bucket.objects[self.name]


class _FakeBucket:
    def __init__(self, name: str) -> None:
        self.name = name
        self.objects: dict = {}
        self.touched: list = []
        self.downloaded: list = []
        self.exists_raises = None
        self.download_raises = None

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self, name)


class _FakeStorageClient:
    """Only what the adapter touches. `bucket()` makes no network call."""

    def __init__(self) -> None:
        self.buckets: dict = {}
        self.bucket_calls: list = []

    def bucket(self, name: str) -> _FakeBucket:
        self.bucket_calls.append(name)
        return self.buckets.setdefault(name, _FakeBucket(name))


@pytest.fixture()
def gcs():
    client = _FakeStorageClient()
    bucket = client.bucket(BUCKET)
    source = GcsParentSessionSource(client, BUCKET)

    class Bundle:
        pass

    bundle = Bundle()
    bundle.client, bundle.bucket, bundle.source = client, bucket, source
    return bundle


def put(bundle, uid: str, session_id: str, document: dict) -> str:
    name = SESSION_BLOB_TEMPLATE.format(uid=uid, session_id=session_id)
    bundle.bucket.objects[name] = json.dumps(document, indent=2)
    return name


# ===========================================================================
# the Parent contract, pinned
# ===========================================================================

def test_the_blob_template_matches_parent_2_3_exactly():
    """Read off `genex-parent/api/session_store.py::_blob_name`.

    Pinned rather than merely used: if Parent ever moves the layout, this fails
    loudly instead of the adapter returning "no such session" forever.
    """
    assert SESSION_BLOB_TEMPLATE == "sessions/{uid}/{session_id}.json"
    assert OWNER_FIELD == "owner_uid"
    assert session_blob_name(UID_A, SESSION_A) == (
        f"sessions/{UID_A}/{SESSION_A}.json")


def test_the_adapter_satisfies_the_port(gcs):
    assert isinstance(gcs.source, ParentSessionSource)


def test_reading_an_owned_session_returns_only_identity_facts(gcs):
    put(gcs, UID_A, SESSION_A, parent_session_document())

    facts = gcs.source.fetch_session_facts(SESSION_A, requesting_subject=UID_A)

    assert facts == ParentSessionFacts(session_id=SESSION_A, owner_uid=UID_A)
    assert set(ParentSessionFacts.__dataclass_fields__) == {
        "session_id", "owner_uid"}


def test_no_clinical_field_crosses_the_boundary(gcs):
    """The document is full of clinical content; two fields come back."""
    put(gcs, UID_A, SESSION_A, parent_session_document())

    facts = gcs.source.fetch_session_facts(SESSION_A, requesting_subject=UID_A)

    blob = repr(facts)
    for sentinel in (*LOCAL_SENTINELS, *ALL_SENTINELS):
        assert sentinel not in blob, sentinel
    for field in ("child_name", "child_age_months", "primary_concern",
                  "diagnosis", "answers", "plan", "feedback",
                  "completion_history", "timezone"):
        assert not hasattr(facts, field), field


def test_the_lookup_is_scoped_to_the_requesting_subject(gcs):
    """The blob name is built from the caller. Another prefix is unreachable."""
    put(gcs, UID_B, SESSION_A, parent_session_document(owner_uid=UID_B))

    assert gcs.source.fetch_session_facts(
        SESSION_A, requesting_subject=UID_A) is None

    # And the only path ever asked for was the caller's own.
    assert gcs.bucket.touched == [f"sessions/{UID_A}/{SESSION_A}.json"]
    assert all(UID_B not in name for name in gcs.bucket.touched)


def test_an_absent_session_returns_none_without_downloading(gcs):
    assert gcs.source.fetch_session_facts(
        "fictional-session-absent", requesting_subject=UID_A) is None
    assert gcs.bucket.downloaded == []


def test_absent_and_foreign_are_indistinguishable_at_the_adapter(gcs):
    """Non-enumerating: both are `None`, with no second outcome to observe."""
    put(gcs, UID_B, SESSION_A, parent_session_document(owner_uid=UID_B))

    foreign = gcs.source.fetch_session_facts(SESSION_A,
                                             requesting_subject=UID_A)
    absent = gcs.source.fetch_session_facts("fictional-session-absent",
                                            requesting_subject=UID_A)
    assert foreign is absent is None


def test_a_misfiled_document_is_returned_unjudged_for_the_service_to_refuse(gcs):
    """Parent's own defence in depth, reproduced.

    A document under one account's prefix that names a different owner is a real
    inconsistency. The adapter does NOT normalise it to the requesting subject
    — it reports what the document says, and the service refuses.
    """
    put(gcs, UID_A, SESSION_A, parent_session_document(owner_uid=UID_B))

    facts = gcs.source.fetch_session_facts(SESSION_A, requesting_subject=UID_A)

    assert facts is not None
    assert facts.owner_uid == UID_B, "the adapter rewrote the owner"
    assert not facts.is_owned_by(UID_A)


# ===========================================================================
# read-only, structurally
# ===========================================================================

def test_the_adapter_exposes_exactly_one_operation_and_it_is_a_read():
    operations = sorted(
        name for name in dir(GcsParentSessionSource)
        if not name.startswith("_")
        and callable(getattr(GcsParentSessionSource, name)))
    assert operations == ["fetch_session_facts"]


def test_no_method_names_a_mutating_or_destructive_operation():
    """The capability is absent, not merely unused."""
    import ast
    import pathlib

    banned = {"upload_from_string", "upload_from_file", "upload_from_filename",
              "delete", "patch", "copy_blob", "rewrite", "compose",
              "make_public", "create_bucket", "update"}
    path = (pathlib.Path(__file__).resolve().parent.parent
            / "integration" / "parent_gcs_source.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)}
    assert not (called & banned), called & banned


def test_opening_the_bucket_creates_nothing(gcs):
    """`client.bucket(name)` is local; no bucket is created and none is listed."""
    gcs.source.fetch_session_facts("fictional-absent", requesting_subject=UID_A)
    assert gcs.client.bucket_calls[-1] == BUCKET
    assert not hasattr(gcs.client, "created")
    # No listing: an adapter that enumerated a prefix would be an oracle.
    assert not hasattr(gcs.bucket, "listed")


def test_the_adapter_never_lists_blobs():
    import ast
    import pathlib

    path = (pathlib.Path(__file__).resolve().parent.parent
            / "integration" / "parent_gcs_source.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)}
    assert "list_blobs" not in called
    assert "list_buckets" not in called


# ===========================================================================
# path traversal
# ===========================================================================

@pytest.mark.parametrize("session_id", [
    "../../sessions/other/x", "a/b", "..", "a\\b", "x/../../y",
])
def test_a_traversing_session_id_is_refused(gcs, session_id):
    """Path scoping is the only control keeping accounts apart."""
    with pytest.raises(ParentSessionSourceError):
        gcs.source.fetch_session_facts(session_id, requesting_subject=UID_A)
    assert gcs.bucket.touched == []


@pytest.mark.parametrize("uid", ["../other", "a/b", "..", "a\\b"])
def test_a_traversing_subject_is_refused(gcs, uid):
    with pytest.raises(ParentSessionSourceError):
        gcs.source.fetch_session_facts(SESSION_A, requesting_subject=uid)


@pytest.mark.parametrize("session_id,uid", [
    ("", UID_A), ("   ", UID_A), (SESSION_A, ""), (SESSION_A, "   "),
])
def test_an_empty_identifier_is_refused(gcs, session_id, uid):
    with pytest.raises(ParentSessionSourceError):
        gcs.source.fetch_session_facts(session_id, requesting_subject=uid)


# ===========================================================================
# errors are translated and PHI-safe
# ===========================================================================

def test_a_storage_error_is_translated_and_quotes_nothing(gcs):
    """A GCS error can quote the object path — uid and session id included."""
    gcs.bucket.exists_raises = RuntimeError(
        f"403 GET https://storage.googleapis.com/{BUCKET}/sessions/"
        f"{UID_A}/{SESSION_A}.json: caller lacks permission")

    with pytest.raises(ParentSessionSourceError) as caught:
        gcs.source.fetch_session_facts(SESSION_A, requesting_subject=UID_A)

    message = str(caught.value)
    for secret in (UID_A, SESSION_A, BUCKET, "storage.googleapis.com", "403"):
        assert secret not in message, secret


def test_a_download_error_is_translated(gcs):
    put(gcs, UID_A, SESSION_A, parent_session_document())
    gcs.bucket.download_raises = RuntimeError(
        f"connection reset reading sessions/{UID_A}/{SESSION_A}.json")

    with pytest.raises(ParentSessionSourceError) as caught:
        gcs.source.fetch_session_facts(SESSION_A, requesting_subject=UID_A)
    assert UID_A not in str(caught.value)
    assert SESSION_A not in str(caught.value)


def test_the_sdk_exception_is_not_chained(gcs):
    """`from None`, so a traceback printer cannot surface the SDK's text."""
    gcs.bucket.exists_raises = RuntimeError(f"path sessions/{UID_A}/x.json")
    with pytest.raises(ParentSessionSourceError) as caught:
        gcs.source.fetch_session_facts(SESSION_A, requesting_subject=UID_A)
    assert caught.value.__cause__ is None


def test_the_error_type_declares_itself_phi_safe():
    assert ParentSessionSourceError.PHI_SAFE_MESSAGE is True


def test_a_malformed_document_is_an_error_not_a_missing_session(gcs):
    """Returning None would invite a SECOND canonical child for a real session."""
    name = SESSION_BLOB_TEMPLATE.format(uid=UID_A, session_id=SESSION_A)
    gcs.bucket.objects[name] = "{not json at all"
    with pytest.raises(ParentSessionSourceError):
        gcs.source.fetch_session_facts(SESSION_A, requesting_subject=UID_A)


def test_a_non_object_document_is_refused(gcs):
    name = SESSION_BLOB_TEMPLATE.format(uid=UID_A, session_id=SESSION_A)
    gcs.bucket.objects[name] = json.dumps(["not", "an", "object"])
    with pytest.raises(ParentSessionSourceError):
        gcs.source.fetch_session_facts(SESSION_A, requesting_subject=UID_A)


@pytest.mark.parametrize("owner", [None, "", "   ", 42, {"uid": "x"}])
def test_a_document_without_a_usable_owner_is_refused(gcs, owner):
    """Never assume the caller owns it. That is the one guess forbidden here."""
    document = parent_session_document()
    document[OWNER_FIELD] = owner
    if owner is None:
        del document[OWNER_FIELD]
    put(gcs, UID_A, SESSION_A, document)

    with pytest.raises(ParentSessionSourceError):
        gcs.source.fetch_session_facts(SESSION_A, requesting_subject=UID_A)


def test_a_malformed_document_error_quotes_no_content(gcs):
    name = SESSION_BLOB_TEMPLATE.format(uid=UID_A, session_id=SESSION_A)
    gcs.bucket.objects[name] = "{" + SENTINEL_CONCERN
    with pytest.raises(ParentSessionSourceError) as caught:
        gcs.source.fetch_session_facts(SESSION_A, requesting_subject=UID_A)
    assert SENTINEL_CONCERN not in str(caught.value)


# ===========================================================================
# composition
# ===========================================================================

def test_the_builder_requires_an_explicit_bucket(monkeypatch):
    """No default, no fallback: misconfiguration must fail to start."""
    monkeypatch.delenv(BUCKET_ENV_VAR, raising=False)
    with pytest.raises(ParentSessionSourceError):
        build_parent_session_source(client=_FakeStorageClient())


def test_the_builder_reads_the_bucket_from_the_environment(monkeypatch):
    monkeypatch.setenv(BUCKET_ENV_VAR, BUCKET)
    source = build_parent_session_source(client=_FakeStorageClient())
    assert isinstance(source, GcsParentSessionSource)


def test_an_explicit_bucket_beats_the_environment(monkeypatch):
    monkeypatch.setenv(BUCKET_ENV_VAR, "fictional-wrong-bucket")
    client = _FakeStorageClient()
    source = build_parent_session_source(bucket_name=BUCKET, client=client)
    source.fetch_session_facts("fictional-absent", requesting_subject=UID_A)
    assert client.bucket_calls == [BUCKET]


@pytest.mark.parametrize("bucket_name", ["", "   "])
def test_a_blank_bucket_is_refused(bucket_name):
    with pytest.raises(ParentSessionSourceError):
        GcsParentSessionSource(_FakeStorageClient(), bucket_name)


def test_parent_local_tmp_fallback_is_not_mirrored():
    """Parent falls back to /tmp when GCS is unconfigured. The pilot must not.

    A pilot silently reading sessions from local disk would be a different
    system of record, and that boundary is an open decision in SECURITY.md.
    """
    import ast
    import pathlib

    path = (pathlib.Path(__file__).resolve().parent.parent
            / "integration" / "parent_gcs_source.py")
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert "pathlib" not in imported
    assert "tempfile" not in imported
    assert "shutil" not in imported


def test_the_adapter_constructs_no_logger():
    """Nothing about a session reaches a log line, because there is no sink."""
    import ast
    import pathlib

    path = (pathlib.Path(__file__).resolve().parent.parent
            / "integration" / "parent_gcs_source.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert "logging" not in imported
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "print" not in called


def test_pilot_backend_still_imports_no_storage_sdk():
    """Ports not SDKs: the `google.cloud` import lives HERE, not in the core."""
    import ast
    import pathlib

    root = (pathlib.Path(__file__).resolve().parent.parent.parent
            / "pilot_backend")
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if name.split(".")[0] in {"google", "firebase_admin"}:
                    offenders.append((path.name, name))
    assert offenders == [], offenders
