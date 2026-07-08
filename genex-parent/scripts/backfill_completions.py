#!/usr/bin/env python3
"""
scripts/backfill_completions.py — Beta 2.3 Phase 1 (STAGING ONLY).

Backfill durable completion history + all-time stars from existing doc["feedback"]
records, across every session in the configured store. Idempotent and safe to re-run.

SAFETY: reads GCS_BUCKET from the environment. It MUST be the DEV bucket
(genex-api-dev-sessions-genex-mvp-2026). The script refuses to run against a bucket
whose name contains "prod".

Usage (dry-run first, then apply):
  GCS_BUCKET=genex-api-dev-sessions-genex-mvp-2026 python3 scripts/backfill_completions.py --dry-run
  GCS_BUCKET=genex-api-dev-sessions-genex-mvp-2026 python3 scripts/backfill_completions.py --apply
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.backfill import backfill_doc  # noqa: E402


def _iter_gcs(bucket_name):
    from google.cloud import storage
    client = storage.Client()
    for blob in client.list_blobs(bucket_name, prefix="sessions/"):
        if not blob.name.endswith(".json"):
            continue
        try:
            doc = json.loads(blob.download_as_text())
        except Exception:
            continue
        yield blob, doc


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    bucket = os.environ.get("GCS_BUCKET", "").strip()
    if not bucket:
        print("ERROR: GCS_BUCKET not set.", file=sys.stderr); sys.exit(2)
    if "prod" in bucket.lower():
        print(f"REFUSING: bucket '{bucket}' looks like PRODUCTION. Staging only.", file=sys.stderr); sys.exit(2)

    dry = args.dry_run
    print(f"{'DRY-RUN' if dry else 'APPLY'} backfill over gs://{bucket}/sessions/")
    from google.cloud import storage
    client = storage.Client()

    totals = {"sessions": 0, "feedback_examined": 0, "complete": 0, "partial": 0,
              "unavailable": 0, "stars_proposed": 0, "skipped_existing": 0, "sessions_changed": 0}
    for blob, doc in _iter_gcs(bucket):
        totals["sessions"] += 1
        stats, changed = backfill_doc(doc, dry_run=dry)
        for k in ("feedback_examined", "complete", "partial", "unavailable", "stars_proposed", "skipped_existing"):
            totals[k] += stats[k]
        if changed and not dry:
            blob.upload_from_string(json.dumps(doc, indent=2, default=str), content_type="application/json")
            totals["sessions_changed"] += 1

    print(json.dumps(totals, indent=2))
    print("DRY-RUN complete (no writes)." if dry else f"APPLIED to {totals['sessions_changed']} session(s).")


if __name__ == "__main__":
    main()
