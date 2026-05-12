"""
genex_core/storage.py
---------------------
Save session and feedback JSON to Cloud Storage (GCS) when the GCS_BUCKET
environment variable is set.  Falls back to the local filesystem otherwise.

On Cloud Run:
  - GCS_BUCKET is set via --set-env-vars at deploy time
  - The Cloud Run service account needs roles/storage.objectCreator on the bucket
  - Container-local writes go to /tmp/sessions (ephemeral, used as fallback only)

Locally:
  - If GCS_BUCKET is not set, files are written to the SESSION_DIR path
  - google-cloud-storage is imported lazily so the app works without GCP creds locally
"""

import json
import os
from pathlib import Path
from typing import Any, Dict

# Set this env var to enable GCS persistence.
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET", "").strip()


def _gcs_client():
    """Lazy import — only pulls in the GCS library when actually needed."""
    from google.cloud import storage  # noqa: PLC0415
    return storage.Client()


def save_json(
    data: Dict[str, Any],
    blob_name: str,
    local_fallback_dir: Path,
) -> str:
    """
    Persist *data* as JSON.

    Parameters
    ----------
    data              : dict to serialise
    blob_name         : GCS object name  (e.g. "sessions/emma_20260504_120000.json")
                        Also used as the filename stem for local fallback.
    local_fallback_dir: directory to write to when GCS is not configured or fails

    Returns
    -------
    "gcs"    — written to Cloud Storage
    "local"  — written to local filesystem
    "failed" — both attempts failed (logged; never raises)
    """
    content = json.dumps(data, indent=2, default=str)

    # ── Cloud Storage path ──────────────────────────────────────────────────
    if GCS_BUCKET_NAME:
        try:
            client = _gcs_client()
            bucket = client.bucket(GCS_BUCKET_NAME)
            blob = bucket.blob(blob_name)
            blob.upload_from_string(content, content_type="application/json")
            return "gcs"
        except Exception as exc:
            # Log and fall through to local write
            print(f"[storage] GCS write failed — {exc}")

    # ── Local filesystem fallback ───────────────────────────────────────────
    try:
        local_fallback_dir.mkdir(parents=True, exist_ok=True)
        fname = Path(blob_name).name          # strip any "sessions/" prefix
        (local_fallback_dir / fname).write_text(content, encoding="utf-8")
        return "local"
    except Exception as exc:
        print(f"[storage] Local write also failed — {exc}")
        return "failed"
