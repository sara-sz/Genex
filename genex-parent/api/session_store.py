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
    }
  }

  child_name is NEVER stored. GCS files are name-blind by design.
"""

import json
import os
import threading
from datetime import datetime, timezone as timezone_module
from pathlib import Path
from typing import Any, Dict, Optional

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
) -> Dict[str, Any]:
    """
    Build a new session document. child_name is never a field here.
    diagnosis_or_condition stores the original frontend value for audit.
    timezone is the IANA timezone string from Lovable; used by /plan to anchor
    the planning week to Monday–Sunday in the parent's local timezone.
    """
    return {
        "session_id": session_id,
        "owner_uid": owner_uid,
        "created_at": datetime.now(timezone_module.utc).isoformat(),
        "status": "questions",
        "age_in_months": age_in_months,
        "daily_time_minutes": daily_time_minutes,
        "timezone": timezone,
        "diagnosis_or_condition": diagnosis_or_condition,
        "brain_state": brain_state,
        "interview": interview,
        "feedback": [],
        "plan_generated": False,
        # plan history — populated by /plan; supports future weekly refresh
        "current_plan_id": None,
        "plans": {},
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


def load(uid: str, session_id: str) -> Optional[Dict[str, Any]]:
    """
    Load a session document.

    Load order:
      1. Memory cache (fast path — no I/O)
      2. GCS when GCS_BUCKET is set
      3. Local fallback only when LOCAL_SESSION_FALLBACK=1

    Returns: session document dict, or None if not found anywhere.
    Raises: SessionLoadError if GCS returns an error other than not-found
            AND LOCAL_SESSION_FALLBACK=0.

    IMPORTANT: caller must check doc["owner_uid"] == uid and raise 403 on mismatch.
    This function does not enforce ownership — it returns whatever it finds.
    """
    # 1. Memory cache
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


def evict(session_id: str) -> None:
    """Remove a session from the memory cache."""
    with _lock:
        _cache.pop(session_id, None)
