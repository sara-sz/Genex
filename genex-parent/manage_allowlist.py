#!/usr/bin/env python3
"""
manage_allowlist.py — Genex Parent Copilot Allowlist Manager
-------------------------------------------------------------
List, add, and sync the registration allowlist.

Usage:
    python3 manage_allowlist.py list
    python3 manage_allowlist.py add email@example.com
    python3 manage_allowlist.py add email1@example.com email2@example.com
    python3 manage_allowlist.py remove email@example.com
    python3 manage_allowlist.py upload          # push local file to GCS
    python3 manage_allowlist.py download        # pull GCS file to local

Local file:  genex-parent/config/allowlist.json
GCS path:    gs://genex-parent-sessions-genex-mvp-2026/config/allowlist.json

Workflow for adding pilot parents:
    1. python3 manage_allowlist.py add parent1@gmail.com parent2@gmail.com
    2. python3 manage_allowlist.py list       (verify)
    3. python3 manage_allowlist.py upload     (push to GCS — no redeployment needed)
    4. The app picks up the new list within 5 minutes automatically.
"""

import json
import sys
from pathlib import Path

LOCAL_FILE  = Path(__file__).parent / "config" / "allowlist.json"
GCS_BUCKET  = "genex-parent-sessions-genex-mvp-2026"
GCS_BLOB    = "config/allowlist.json"


# ── File helpers ───────────────────────────────────────────────────────────

def _load_local() -> list:
    """Load and return the local allowlist (sorted, de-duped, normalised)."""
    if not LOCAL_FILE.exists():
        return []
    try:
        data = json.loads(LOCAL_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return sorted({e.strip().lower() for e in data if isinstance(e, str) and e.strip()})
    except Exception as exc:
        print(f"Error reading {LOCAL_FILE}: {exc}")
    return []


def _save_local(emails: list):
    """Write the normalised list to the local file."""
    LOCAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    normalised = sorted({e.strip().lower() for e in emails if e.strip()})
    LOCAL_FILE.write_text(
        json.dumps(normalised, indent=2),
        encoding="utf-8",
    )
    print(f"✓ Saved {len(normalised)} email(s) to {LOCAL_FILE}")


def _gcs_client():
    try:
        from google.cloud import storage
        return storage.Client()
    except ImportError:
        print("Error: google-cloud-storage is not installed. Run: pip install google-cloud-storage")
        sys.exit(1)
    except Exception as exc:
        print(f"Error: could not connect to GCS — {exc}")
        print("Make sure GOOGLE_APPLICATION_CREDENTIALS is set or you are on GCP.")
        sys.exit(1)


# ── Commands ───────────────────────────────────────────────────────────────

def cmd_list():
    emails = _load_local()
    if not emails:
        print("Allowlist is empty.")
    else:
        print(f"Local allowlist ({len(emails)} email(s)):")
        for e in emails:
            print(f"  {e}")


def cmd_add(new_emails: list):
    if not new_emails:
        print("Usage: python3 manage_allowlist.py add email@example.com ...")
        sys.exit(1)
    current = set(_load_local())
    added   = []
    already = []
    for raw in new_emails:
        e = raw.strip().lower()
        if not e or "@" not in e:
            print(f"  Skipped (invalid): {raw}")
            continue
        if e in current:
            already.append(e)
        else:
            current.add(e)
            added.append(e)
    _save_local(list(current))
    if added:
        print(f"Added: {', '.join(added)}")
    if already:
        print(f"Already present: {', '.join(already)}")
    print(f"\nRun 'python3 manage_allowlist.py upload' to push to GCS.")


def cmd_remove(emails: list):
    if not emails:
        print("Usage: python3 manage_allowlist.py remove email@example.com ...")
        sys.exit(1)
    current = set(_load_local())
    removed = []
    missing = []
    for raw in emails:
        e = raw.strip().lower()
        if e in current:
            current.discard(e)
            removed.append(e)
        else:
            missing.append(e)
    _save_local(list(current))
    if removed:
        print(f"Removed: {', '.join(removed)}")
    if missing:
        print(f"Not found: {', '.join(missing)}")
    print(f"\nRun 'python3 manage_allowlist.py upload' to push to GCS.")


def cmd_upload():
    emails = _load_local()
    if not emails:
        print("Local allowlist is empty — nothing to upload.")
        sys.exit(0)
    client  = _gcs_client()
    bucket  = client.bucket(GCS_BUCKET)
    blob    = bucket.blob(GCS_BLOB)
    content = json.dumps(emails, indent=2)
    blob.upload_from_string(content, content_type="application/json")
    print(f"✓ Uploaded {len(emails)} email(s) to gs://{GCS_BUCKET}/{GCS_BLOB}")
    print("The app will pick up the new list within 5 minutes (cache TTL).")


def cmd_download():
    client  = _gcs_client()
    bucket  = client.bucket(GCS_BUCKET)
    blob    = bucket.blob(GCS_BLOB)
    try:
        content = blob.download_as_text()
        emails  = json.loads(content)
        if not isinstance(emails, list):
            print(f"Error: GCS file is not a JSON array.")
            sys.exit(1)
        _save_local(emails)
        print(f"✓ Downloaded {len(emails)} email(s) from gs://{GCS_BUCKET}/{GCS_BLOB}")
    except Exception as exc:
        print(f"Error downloading from GCS: {exc}")
        sys.exit(1)


# ── Entry point ────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd  = sys.argv[1].strip().lower()
    args = sys.argv[2:]

    if cmd == "list":
        cmd_list()
    elif cmd == "add":
        cmd_add(args)
    elif cmd == "remove":
        cmd_remove(args)
    elif cmd == "upload":
        cmd_upload()
    elif cmd == "download":
        cmd_download()
    else:
        print(f"Unknown command: {cmd}")
        print("Commands: list | add | remove | upload | download")
        sys.exit(1)


if __name__ == "__main__":
    main()
