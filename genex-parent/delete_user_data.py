#!/usr/bin/env python3
"""
delete_user_data.py — Genex Parent Copilot User Data Deletion
--------------------------------------------------------------
Deletes all GCS session and feedback files for a given user_id.
Use this to honour a parent's data deletion request.

Usage:
    python3 delete_user_data.py <user_id>

    <user_id>  The Firebase UID (shown in Cloud Console → Identity Platform)
               or the user_id field from any of their session/feedback JSON files.

What it deletes:
    gs://{GCS_BUCKET}/sessions/{user_id}/*
    gs://{GCS_BUCKET}/feedback/{user_id}/*

What it does NOT delete:
    The Identity Platform account itself (do that in Cloud Console → Identity Platform
    → Users, or via the Firebase Admin SDK).

Environment:
    GCS_BUCKET   Name of the GCS bucket (set automatically on Cloud Run)

Example:
    GCS_BUCKET=genex-parent-sessions-genex-mvp-2026 \\
        python3 delete_user_data.py abc123uid456

Dry run (list files without deleting):
    python3 delete_user_data.py <user_id> --dry-run
"""

import os
import sys

GCS_BUCKET = os.environ.get("GCS_BUCKET", "genex-parent-sessions-genex-mvp-2026").strip()


def list_user_blobs(client, user_id: str) -> list:
    """Return all blobs under sessions/{user_id}/ and feedback/{user_id}/."""
    bucket = client.bucket(GCS_BUCKET)
    blobs  = []
    for prefix in [f"sessions/{user_id}/", f"feedback/{user_id}/"]:
        blobs.extend(list(client.list_blobs(GCS_BUCKET, prefix=prefix)))
    return blobs


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    user_id  = sys.argv[1].strip()
    dry_run  = "--dry-run" in sys.argv

    if not GCS_BUCKET:
        print("Error: GCS_BUCKET environment variable is not set.")
        sys.exit(1)

    try:
        from google.cloud import storage
        client = storage.Client()
    except Exception as exc:
        print(f"Error: could not initialise GCS client — {exc}")
        print("Make sure GOOGLE_APPLICATION_CREDENTIALS is set or you are running on GCP.")
        sys.exit(1)

    blobs = list_user_blobs(client, user_id)

    if not blobs:
        print(f"No files found for user_id: {user_id}")
        print(f"Checked: gs://{GCS_BUCKET}/sessions/{user_id}/")
        print(f"         gs://{GCS_BUCKET}/feedback/{user_id}/")
        sys.exit(0)

    print(f"\nFiles found for user_id '{user_id}' in gs://{GCS_BUCKET}:")
    for b in blobs:
        print(f"  {b.name}  ({b.size} bytes)")

    if dry_run:
        print(f"\nDry run — {len(blobs)} file(s) would be deleted. Run without --dry-run to delete.")
        sys.exit(0)

    confirm = input(f"\nDelete {len(blobs)} file(s)? [yes/no]: ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        sys.exit(0)

    deleted = 0
    errors  = 0
    for b in blobs:
        try:
            b.delete()
            print(f"  Deleted: {b.name}")
            deleted += 1
        except Exception as exc:
            print(f"  ERROR deleting {b.name}: {exc}")
            errors += 1

    print(f"\nDone. {deleted} file(s) deleted, {errors} error(s).")

    if errors == 0:
        print(
            "\nReminder: this script deletes stored session and feedback files only.\n"
            "To delete the parent's account in Identity Platform, go to:\n"
            "  Cloud Console → Identity Platform → Users → find email → Delete\n"
            "or use the Firebase Admin SDK."
        )


if __name__ == "__main__":
    main()
