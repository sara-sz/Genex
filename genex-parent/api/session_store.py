"""
api/session_store.py — Session persistence for the Genex API.

GCS is the single durable source of truth. Memory is a read-through cache only.

Fallback policy (controlled by LOCAL_SESSION_FALLBACK env var):
  LOCAL_SESSION_FALLBACK=0 (default, staging/production)
    - GCS is the only durable store.
    - If GCS save fails  → SessionSaveError is raised → caller returns HTTP 500.
    - If GCS load errors → SessionLoadError is raised → caller returns HTTP 500.
    - If GCS load returns 404 (blob not found) → None returned → caller returns HTTP 404.
    - Local filesystem is never touched.

  LOCAL_SESSION_FALLBACK=1 (local dev / testing without GCS)
    - GCS is tried first when GCS_BUCKET is set.
    - If GCS save fails or GCS_BUCKET is unset → falls back to /tmp/genex_api_sessions/.
    - If GCS load fails or GCS_BUCKET is unset → falls back to local files.
    - This mode must NEVER be set in staging or production.

Startup check:
  At import time, if GCS_BUCKET is not set and LOCAL_SESSION_FALLBACK=0, a
  RuntimeError is raised. This surfaces the misconfiguration immediately rather
  than silently failing on the first request.

GCS path: sessions/{uid}/{session_id}.json

Session document shape:
  {
    "session_id":            str,
    "owner_uid":             str,
    "created_at":            ISO-8601 str,
    "status":                "questions" | "interview_complete" | "plan_ready",
    "age_in_months":         int,
    "daily_time_minutes":    int,
    "timezone":              str,    # IANA tz from Lovable, e.g. "America/Los_Angeles"
    "diagnosis_or_condition": str,   # original frontend value, for audit
    "beta_authorized":       bool,   # True once the beta code was accepted at start
    "beta_authorized_at":    str | None,  # ISO-8601 timestamp, or None
    "brain_state":           dict,   # raw state dict from genex_core
    "interview":             dict,   # API-layer interview tracking state
    "feedback":              list,
    "plan_generated":        bool,
    "current_plan_id":       str | None,   # plan_id of the most recent /plan call
    "plans": {                             # keyed by plan_id; supports weekly refresh
      "<plan_id>": {
        "plan_period":   dict,   # planning period metadata
        "plan_response": dict,   # parent-facing plan (cached for fast reload)
        "plan_internal": dict,   # rich internal metadata (never sent to frontend)
      }
    },
    "plan_customizations": {               # Beta 2.1 current-week overlay, keyed by plan_id
      "<plan_id>": dict,                   # see api/customization.py; never mutates plans[*]
    },
    "added_focus": {                       # Beta 2.2 add-on focus modules, keyed by focus_key
      "<focus_key>": dict,                 # see api/focus_selector.py; separate from plans[*]
    }
  }

  child_name is NEVER stored. GCS files are name-blind by design.
  The beta access code is NEVER stored — only the beta_authorized boolean is.
"""

import json
import os
import threading
from datetime import datetime, timezone as timezone_module
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Config ─────────────────────────────────────────────────────────────────

GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET", "").strip()
_LOCAL_FALLBACK = os.environ.get("LOCAL_SESSION_FALLBACK", "0").strip() == "1"
_LOCAL_SESSION_DIR = Path("/tmp/genex_api_sessions")

# Startup misconfiguration guard
if not GCS_BUCKET_NAME and not _LOCAL_FALLBACK:
    raise RuntimeError(
        "Session storage is not configured. "
        "Set GCS_BUCKET to your GCS bucket name, "
        "or set LOCAL_SESSION_FALLBACK=1 for local development only."
    )

if _LOCAL_FALLBACK:
    import warnings
    warnings.warn(
        "LOCAL_SESSION_FALLBACK=1 is set. "
        "Sessions may be written to the local filesystem. "
        "This must NOT be used in staging or production.",
        stacklevel=2,
    )


# ── Exceptions ─────────────────────────────────────────────────────────────

class SessionSaveError(Exception):
    """Raised when a session cannot be saved durably."""


class SessionLoadError(Exception):
    """Raised when GCS returns an error (distinct from a simple not-found)."""


class PreconditionFailedError(Exception):
    """Raised when a generation-guarded save loses the compare-and-swap (the object
    changed since it was read). The caller (mutate_session) reloads and retries."""


class SessionContentionError(SessionSaveError):
    """Raised when a compare-and-swap write cannot succeed within max_retries."""


# ── Memory cache ───────────────────────────────────────────────────────────

_cache: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()


# ── Internal helpers ───────────────────────────────────────────────────────

def _blob_name(uid: str, session_id: str) -> str:
    return f"sessions/{uid}/{session_id}.json"


def _gcs_save_raw(uid: str, session_id: str, doc: Dict[str, Any]) -> None:
    """
    Write doc to GCS. Raises on any error (network, auth, quota, etc.).
    Does NOT fall back to local — that decision belongs to the caller.
    """
    from google.cloud import storage  # lazy import
    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET_NAME)
    blob = bucket.blob(_blob_name(uid, session_id))
    blob.upload_from_string(
        json.dumps(doc, indent=2, default=str),
        content_type="application/json",
    )


def _gcs_load_raw(uid: str, session_id: str) -> Optional[Dict[str, Any]]:
    """
    Load doc from GCS.
    Returns None if the blob does not exist (genuine not-found).
    Raises SessionLoadError on any other GCS error (network, auth, etc.).
    """
    try:
        from google.cloud import storage  # lazy import
        client = storage.Client()
        bucket = client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(_blob_name(uid, session_id))
        if not blob.exists():
            return None  # Genuine not-found — caller returns 404
        return json.loads(blob.download_as_text())
    except SessionLoadError:
        raise
    except Exception as exc:
        raise SessionLoadError(
            f"GCS read error for sessions/{uid}/{session_id}.json: {exc}"
        ) from exc


def _local_save(session_id: str, doc: Dict[str, Any]) -> None:
    """Write doc to local fallback dir. Only called when LOCAL_SESSION_FALLBACK=1."""
    _LOCAL_SESSION_DIR.mkdir(parents=True, exist_ok=True)
    path = _LOCAL_SESSION_DIR / f"{session_id}.json"
    path.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")


def _local_load(session_id: str) -> Optional[Dict[str, Any]]:
    """Load doc from local fallback dir. Only called when LOCAL_SESSION_FALLBACK=1."""
    path = _LOCAL_SESSION_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[session_store] Local load failed for {session_id}: {exc}")
        return None


# ── Public API ─────────────────────────────────────────────────────────────

def new_session_doc(
    session_id: str,
    owner_uid: str,
    age_in_months: int,
    daily_time_minutes: int,
    diagnosis_or_condition: str,
    brain_state: Dict[str, Any],
    interview: Dict[str, Any],
    timezone: str = "UTC",
    beta_authorized: bool = False,
) -> Dict[str, Any]:
    """
    Build a new session document. child_name is never a field here.
    diagnosis_or_condition stores the original frontend value for audit.
    timezone is the IANA timezone string from Lovable; used by /plan to anchor
    the planning week to Monday–Sunday in the parent's local timezone.
    beta_authorized records that the shared beta code was accepted at session
    start. The code itself is never stored.
    """
    now_iso = datetime.now(timezone_module.utc).isoformat()
    return {
        "session_id": session_id,
        "owner_uid": owner_uid,
        "created_at": now_iso,
        "status": "questions",
        "age_in_months": age_in_months,
        "daily_time_minutes": daily_time_minutes,
        "timezone": timezone,
        "diagnosis_or_condition": diagnosis_or_condition,
        "beta_authorized": beta_authorized,
        "beta_authorized_at": now_iso if beta_authorized else None,
        "brain_state": brain_state,
        "interview": interview,
        "feedback": [],
        "plan_generated": False,
        # plan history — populated by /plan; supports future weekly refresh
        "current_plan_id": None,
        "plans": {},
        # Beta 2.1 current-week customization overlay, keyed by plan_id.
        # Empty by default; old sessions without this key still work (readers use .get()).
        "plan_customizations": {},
        # Beta 2.2 added focus modules, keyed by focus_key. Empty by default;
        # populated by the "Add another focus area" flow (later slices).
        "added_focus": {},
    }


def save(uid: str, session_id: str, doc: Dict[str, Any]) -> str:
    """
    Save session document durably and update the memory cache.

    Returns: "gcs" or "local" (local only when LOCAL_SESSION_FALLBACK=1).
    Raises: SessionSaveError if the durable save fails and no fallback is permitted.

    In staging/production (LOCAL_SESSION_FALLBACK=0):
      - Only GCS is attempted. Any GCS error raises SessionSaveError → HTTP 500.
      - "local" is never returned.

    In local dev (LOCAL_SESSION_FALLBACK=1):
      - GCS is tried first when GCS_BUCKET is set.
      - Falls back to /tmp/genex_api_sessions/ if GCS fails or is unconfigured.
    """
    if GCS_BUCKET_NAME:
        try:
            _gcs_save_raw(uid, session_id, doc)
            with _lock:
                _cache[session_id] = doc
            return "gcs"
        except Exception as exc:
            if _LOCAL_FALLBACK:
                print(f"[session_store] GCS save failed, using local fallback: {exc}")
                _local_save(session_id, doc)
                with _lock:
                    _cache[session_id] = doc
                return "local"
            raise SessionSaveError(
                f"GCS save failed for sessions/{uid}/{session_id}.json: {exc}"
            ) from exc
    else:
        # GCS_BUCKET not set — LOCAL_SESSION_FALLBACK must be 1 (enforced at startup)
        _local_save(session_id, doc)
        with _lock:
            _cache[session_id] = doc
        return "local"


def load(uid: str, session_id: str, force_remote: bool = False) -> Optional[Dict[str, Any]]:
    """
    Load a session document.

    Load order (default):
      1. Memory cache (fast path — no I/O)
      2. GCS when GCS_BUCKET is set
      3. Local fallback only when LOCAL_SESSION_FALLBACK=1

    force_remote=True: SKIP the memory cache and read authoritative state from the
    durable store (GCS, then local fallback), refreshing the cache. Used only on the
    primary /plan generation path so the in-flight guard / idempotency decision is not
    made on a stale per-instance cache (Cloud Run runs multiple instances; a marker
    written by one instance is invisible to another's cache). Default False keeps every
    other endpoint's behavior byte-identical.

    Returns: session document dict, or None if not found anywhere.
    Raises: SessionLoadError if GCS returns an error other than not-found
            AND LOCAL_SESSION_FALLBACK=0.

    IMPORTANT: caller must check doc["owner_uid"] == uid and raise 403 on mismatch.
    This function does not enforce ownership — it returns whatever it finds.
    """
    # 1. Memory cache (skipped when force_remote=True)
    if not force_remote:
        with _lock:
            doc = _cache.get(session_id)
        if doc is not None:
            return doc

    # 2. GCS
    if GCS_BUCKET_NAME:
        try:
            doc = _gcs_load_raw(uid, session_id)
        except SessionLoadError:
            if _LOCAL_FALLBACK:
                print(f"[session_store] GCS load error, trying local fallback")
                doc = _local_load(session_id)
            else:
                raise  # Surfaces as HTTP 500 in production

        if doc is not None:
            with _lock:
                _cache[session_id] = doc
            return doc

    # 3. Local fallback (only if LOCAL_SESSION_FALLBACK=1 and GCS not set or returned None)
    if _LOCAL_FALLBACK:
        doc = _local_load(session_id)
        if doc is not None:
            with _lock:
                _cache[session_id] = doc
            return doc

    return None


# ── Concurrency-safe compare-and-swap (Beta 2.3 Phase 1) ─────────────────────
#
# GCS supports optimistic concurrency via `if_generation_match`. We read a doc
# together with its object generation, mutate in memory, and save only if the
# generation is unchanged. If another writer won the race the save raises
# PreconditionFailedError; mutate_session reloads (now seeing the other writer's
# state) and re-runs the mutator, which re-checks its idempotency indexes. This
# guarantees two simultaneous identical /feedback requests cannot create two
# feedback records / two completions / two events / two stars.

def load_with_generation(uid: str, session_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[int]]:
    """Authoritative read (cache-bypassing) returning (doc, generation).
    generation is None for the local-fallback store (no GCS object generation)."""
    if GCS_BUCKET_NAME:
        try:
            from google.cloud import storage  # lazy import
            client = storage.Client()
            bucket = client.bucket(GCS_BUCKET_NAME)
            blob = bucket.get_blob(_blob_name(uid, session_id))  # None if missing; carries generation
            if blob is not None:
                doc = json.loads(blob.download_as_text())
                return doc, blob.generation
        except Exception as exc:
            if not _LOCAL_FALLBACK:
                raise SessionLoadError(
                    f"GCS read error for sessions/{uid}/{session_id}.json: {exc}"
                ) from exc
        # fall through to local when GCS missing/errored and fallback allowed
    if _LOCAL_FALLBACK:
        return _local_load(session_id), None
    return None, None


def save_if_generation_match(
    uid: str, session_id: str, doc: Dict[str, Any], generation: Optional[int]
) -> None:
    """Durably save doc only if the object generation still matches. Raises
    PreconditionFailedError on a lost compare-and-swap. Updates the cache on success."""
    if GCS_BUCKET_NAME:
        from google.cloud import storage  # lazy import
        from google.api_core.exceptions import PreconditionFailed  # type: ignore
        client = storage.Client()
        bucket = client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(_blob_name(uid, session_id))
        try:
            blob.upload_from_string(
                json.dumps(doc, indent=2, default=str),
                content_type="application/json",
                if_generation_match=(generation if generation is not None else 0),
            )
        except PreconditionFailed as exc:
            raise PreconditionFailedError(str(exc)) from exc
        with _lock:
            _cache[session_id] = doc
        return
    # Local fallback: no object generation → plain write (sequential in dev/tests).
    _local_save(session_id, doc)
    with _lock:
        _cache[session_id] = doc


def mutate_with_cas(load_fn, save_fn, mutator, max_retries: int = 6):
    """Generic compare-and-swap loop (dependency-injected load/save for testability).

    mutator(doc) -> (changed: bool, result). When changed is False no save is
    attempted (idempotent replay / no-op). On PreconditionFailedError the doc is
    reloaded and the mutator re-runs (re-checking its idempotency indexes)."""
    import random
    import time
    for _ in range(max_retries):
        doc, generation = load_fn()
        if doc is None:
            return None, False, None  # not-found sentinel; caller pre-validates existence
        changed, result = mutator(doc)
        if not changed:
            return doc, False, result
        try:
            save_fn(doc, generation)
            return doc, True, result
        except PreconditionFailedError:
            time.sleep(random.uniform(0.02, 0.15))
            continue
    raise SessionContentionError("write contention: exceeded max compare-and-swap retries")


def mutate_session(uid: str, session_id: str, mutator, max_retries: int = 6):
    """Compare-and-swap mutate of a stored session. Returns (doc, changed, result).
    `result` is whatever the mutator returns (e.g. the feedback response payload)."""
    return mutate_with_cas(
        load_fn=lambda: load_with_generation(uid, session_id),
        save_fn=lambda doc, gen: save_if_generation_match(uid, session_id, doc, gen),
        mutator=mutator,
        max_retries=max_retries,
    )


def _gcs_list_for_uid(uid: str) -> List[Tuple[str, Dict[str, Any]]]:
    """List all session docs under sessions/{uid}/ in GCS. Read-only."""
    from google.cloud import storage  # lazy import
    client = storage.Client()
    out: List[Tuple[str, Dict[str, Any]]] = []
    for blob in client.list_blobs(GCS_BUCKET_NAME, prefix=f"sessions/{uid}/"):
        if not blob.name.endswith(".json"):
            continue
        try:
            doc = json.loads(blob.download_as_text())
        except Exception:
            continue  # skip unreadable/corrupt blobs, never crash
        if doc.get("owner_uid") == uid:
            sid = doc.get("session_id") or blob.name.rsplit("/", 1)[-1][:-5]
            out.append((sid, doc))
    return out


def _local_list_for_uid(uid: str) -> List[Tuple[str, Dict[str, Any]]]:
    """List local session docs owned by uid. Only used with LOCAL_SESSION_FALLBACK=1."""
    out: List[Tuple[str, Dict[str, Any]]] = []
    if not _LOCAL_SESSION_DIR.exists():
        return out
    for path in _LOCAL_SESSION_DIR.glob("*.json"):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if doc.get("owner_uid") == uid:
            out.append((doc.get("session_id") or path.stem, doc))
    return out


def find_latest_for_uid(uid: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Return (session_id, doc) for the uid's LATEST session by created_at, or None.

    Read-only: lists the durable store (GCS in staging/prod, local fallback in dev),
    filters by owner_uid, and selects the newest created_at. Docs missing created_at
    are handled safely (treated as oldest) and never crash the lookup. Never creates,
    mutates, or caches.
    """
    if not uid:
        return None

    candidates: List[Tuple[str, Dict[str, Any]]] = []
    if GCS_BUCKET_NAME:
        try:
            candidates = _gcs_list_for_uid(uid)
        except Exception as exc:
            if _LOCAL_FALLBACK:
                print(f"[session_store] GCS list error, trying local fallback: {exc}")
                candidates = _local_list_for_uid(uid)
            else:
                raise SessionLoadError(
                    f"GCS list error for sessions/{uid}/: {exc}"
                ) from exc
    else:
        candidates = _local_list_for_uid(uid)

    if not candidates:
        return None

    # Latest by created_at (ISO-8601 strings sort chronologically; missing → "").
    candidates.sort(key=lambda item: item[1].get("created_at") or "")
    return candidates[-1]


def evict(session_id: str) -> None:
    """Remove a session from the memory cache."""
    with _lock:
        _cache.pop(session_id, None)
