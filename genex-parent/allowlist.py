"""
allowlist.py — Registration allowlist for Genex Parent Copilot
---------------------------------------------------------------
Only email addresses on the allowlist may create an account.
Open registration is disabled by default.

Allowlist sources (in priority order):
  1. GCS:   gs://{GCS_BUCKET}/config/allowlist.json
  2. Local: config/allowlist.json  (development)

Format: ["email@example.com", "another@example.com"]
All emails are normalised to lowercase before comparison.

To add new parents:
  - Update config/allowlist.json locally for dev
  - Update gs://{GCS_BUCKET}/config/allowlist.json for staging/prod
  - No redeployment needed — cache refreshes every 5 minutes

Dev override:
  ALLOW_ALL_EMAILS=true   Skip allowlist check entirely (local dev only).
                          Never set this in staging or production.
"""

import json
import os
from pathlib import Path

import streamlit as st

GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET", "").strip()
ALLOWLIST_BLOB  = "config/allowlist.json"
LOCAL_ALLOWLIST = Path(__file__).parent / "config" / "allowlist.json"

# Safety: ALLOW_ALL_EMAILS must be explicitly set — default is strict (False)
_ALLOW_ALL = os.environ.get("ALLOW_ALL_EMAILS", "false").strip().lower() == "true"


@st.cache_data(ttl=300, show_spinner=False)   # refresh every 5 minutes
def _load_allowlist_cached() -> frozenset:
    """Load and cache the allowlist. TTL=5 min so updates take effect quickly."""
    raw = _read_source()
    return frozenset(e.strip().lower() for e in raw if isinstance(e, str) and e.strip())


def _read_source() -> list:
    """Read the raw email list from GCS or local file."""
    if GCS_BUCKET_NAME:
        try:
            from google.cloud import storage
            client = storage.Client()
            bucket = client.bucket(GCS_BUCKET_NAME)
            blob   = bucket.blob(ALLOWLIST_BLOB)
            return json.loads(blob.download_as_text())
        except Exception as exc:
            print(f"[allowlist] GCS read failed ({exc}) — trying local file")

    if LOCAL_ALLOWLIST.exists():
        try:
            data = json.loads(LOCAL_ALLOWLIST.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
            print(f"[allowlist] Local file is not a JSON array — ignoring")
        except Exception as exc:
            print(f"[allowlist] Local allowlist parse error: {exc}")

    print("[allowlist] WARNING: no allowlist found — all registrations will be rejected")
    return []


def is_allowed(email: str) -> bool:
    """
    Return True if the email may register.
    False if not on the allowlist (or allowlist is empty).
    """
    if _ALLOW_ALL:
        return True
    allowed = _load_allowlist_cached()
    return email.strip().lower() in allowed


def reload():
    """Force a cache refresh — call after uploading a new allowlist to GCS."""
    _load_allowlist_cached.clear()
