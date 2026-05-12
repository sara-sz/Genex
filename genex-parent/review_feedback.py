#!/usr/bin/env python3
"""
review_feedback.py — Genex Parent Copilot Pilot Feedback Reviewer
------------------------------------------------------------------
Reads downloaded feedback JSON files from a folder and writes a CSV summary.

Usage:
    python3 review_feedback.py <folder_path>

    <folder_path>  path to the folder containing .json feedback files
                   (default: current directory)

Output:
    feedback_summary.csv  written to the current directory

Example:
    # Download all feedback files first:
    mkdir -p ~/pilot-feedback
    gcloud storage cp "gs://genex-parent-sessions-genex-mvp-2026/feedback/*.json" ~/pilot-feedback/

    # Then run:
    python3 review_feedback.py ~/pilot-feedback/
"""

import sys
import json
import csv
import os
from pathlib import Path
from datetime import datetime


# ── Column order in the output CSV ────────────────────────────────────────────
OUTPUT_FIELDS = [
    "file",
    "timestamp",
    "session_id",
    "child_age_months",
    "diagnosis",
    "concern",
    "feedback_text",
    "rating",
    "plan_days_count",
    "domains_covered",
]


def _safe(value, default=""):
    """Return value as string, or default if None/missing."""
    if value is None:
        return default
    return str(value).strip()


def parse_feedback_file(path: Path) -> dict:
    """Parse a single feedback JSON file into a flat row dict."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return {"file": path.name, "_error": str(e)}

    # Top-level feedback fields
    row = {
        "file": path.name,
        "timestamp": _safe(data.get("timestamp") or data.get("created_at")),
        "session_id": _safe(data.get("session_id")),
        "feedback_text": _safe(data.get("feedback") or data.get("feedback_text")),
        "rating": _safe(data.get("rating")),
    }

    # Child profile — may be nested under "child" or "profile" or at top level
    child = data.get("child") or data.get("profile") or data
    row["child_age_months"] = _safe(child.get("age_months") or child.get("age"))
    row["diagnosis"]        = _safe(child.get("diagnosis") or child.get("condition"))
    row["concern"]          = _safe(child.get("concern") or child.get("main_concern"))

    # Weekly plan stats — may be nested under "schedule" or "weekly_schedule"
    schedule = data.get("schedule") or data.get("weekly_schedule") or {}
    days = schedule.get("days") or {}
    if days:
        row["plan_days_count"] = len(days)
        # Collect unique domains across all activities
        domains = set()
        for day_data in days.values():
            for item in (day_data.get("items") or []):
                cat = item.get("category") or item.get("category_key") or ""
                if cat:
                    domains.add(cat)
        row["domains_covered"] = "; ".join(sorted(domains))
    else:
        row["plan_days_count"] = ""
        row["domains_covered"] = ""

    return row


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    if not folder.exists():
        print(f"Error: folder '{folder}' does not exist.")
        sys.exit(1)

    json_files = sorted(folder.glob("*.json"))
    if not json_files:
        print(f"No .json files found in '{folder}'.")
        sys.exit(0)

    rows = [parse_feedback_file(p) for p in json_files]

    # Separate clean rows from error rows
    clean  = [r for r in rows if "_error" not in r]
    errors = [r for r in rows if "_error" in r]

    # Write CSV
    out_path = Path("feedback_summary.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(clean)

    print(f"✓ Wrote {len(clean)} row(s) to {out_path.resolve()}")

    if errors:
        print(f"\n⚠ {len(errors)} file(s) could not be parsed:")
        for r in errors:
            print(f"  {r['file']}: {r['_error']}")

    # Quick console summary
    if clean:
        print("\n── Quick summary ──────────────────────────────────────")
        ratings = [r["rating"] for r in clean if r["rating"]]
        if ratings:
            numeric = [float(r) for r in ratings if r.replace(".", "", 1).isdigit()]
            if numeric:
                avg = sum(numeric) / len(numeric)
                print(f"Ratings:      {numeric}  (avg {avg:.1f})")
        has_text = [r for r in clean if r["feedback_text"]]
        print(f"Has feedback: {len(has_text)} / {len(clean)} families")
        print()
        for r in clean:
            age    = f"{r['child_age_months']} mo" if r["child_age_months"] else "age unknown"
            rating = f"★ {r['rating']}" if r["rating"] else "no rating"
            text   = r["feedback_text"][:80] + "…" if len(r["feedback_text"]) > 80 else r["feedback_text"]
            print(f"  [{r['file']}]  {age} | {r['diagnosis'] or 'no diagnosis'} | {rating}")
            if text:
                print(f"    \"{text}\"")
        print("───────────────────────────────────────────────────────")


if __name__ == "__main__":
    main()
