#!/usr/bin/env python3
"""
review_feedback.py — Genex Parent Copilot Pilot Feedback Reviewer
------------------------------------------------------------------
Reads downloaded feedback JSON files from a folder and writes a CSV summary.
Supports both v0.2 schema (child_name field) and v0.3+ schema (user_id / no name).

Usage:
    python3 review_feedback.py <folder_path>

    <folder_path>  path to the folder containing .json feedback files
                   (default: current directory)

Output:
    feedback_summary.csv  written to the current directory

Download feedback files from GCS first:
    # v0.2 schema (flat feedback/ folder):
    mkdir -p ~/pilot-feedback
    gcloud storage cp "gs://genex-parent-sessions-genex-mvp-2026/feedback/*.json" ~/pilot-feedback/

    # v0.3+ schema (user_id subdirectories):
    mkdir -p ~/pilot-feedback
    gcloud storage cp -r "gs://genex-parent-sessions-genex-mvp-2026/feedback/" ~/pilot-feedback/
    # Then point the script at ~/pilot-feedback/feedback/ or just ~/pilot-feedback/

Then run:
    python3 review_feedback.py ~/pilot-feedback/
"""

import csv
import json
import sys
from pathlib import Path

# ── Output columns ─────────────────────────────────────────────────────────
OUTPUT_FIELDS = [
    "file",
    "app_version",
    "submitted_at",
    "user_id",
    "session_id",
    "age_months",
    "diagnosis_or_condition",
    "overall_usefulness",
    "activity_relevance",
    "language_clarity",
    "what_helped",
    "what_to_change",
    "general",
]


def _safe(value, default="") -> str:
    if value is None:
        return default
    return str(value).strip()


def parse_feedback_file(path: Path) -> dict:
    """Parse one feedback JSON file into a flat row. Handles v0.2 and v0.3 schemas."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        return {"file": path.name, "_error": str(exc)}

    ratings  = data.get("ratings") or {}
    comments = data.get("comments") or {}

    row = {
        "file":        path.name,
        "app_version": _safe(data.get("app_version")),
        "submitted_at": _safe(
            data.get("submitted_at") or data.get("created_at") or data.get("timestamp")
        ),
        # v0.3 fields
        "user_id":    _safe(data.get("user_id")),
        "session_id": _safe(data.get("session_id")),
        # Child data — no name stored in v0.3
        "age_months":             _safe(data.get("age_months")),
        "diagnosis_or_condition": _safe(
            data.get("diagnosis_or_condition") or data.get("diagnosis")
        ),
        # Ratings
        "overall_usefulness": _safe(
            ratings.get("overall_usefulness") or ratings.get("overall")
        ),
        "activity_relevance": _safe(
            ratings.get("activity_relevance") or ratings.get("activity_rating")
        ),
        "language_clarity": _safe(
            ratings.get("language_clarity") or ratings.get("language_rating")
        ),
        # Comments
        "what_helped":    _safe(comments.get("what_helped")),
        "what_to_change": _safe(comments.get("what_to_change")),
        "general":        _safe(comments.get("general")),
    }
    return row


def find_json_files(folder: Path) -> list:
    """Recursively find all .json files (handles flat and user_id subdirectory layouts)."""
    return sorted(folder.rglob("*.json"))


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    if not folder.exists():
        print(f"Error: folder '{folder}' does not exist.")
        sys.exit(1)

    json_files = find_json_files(folder)
    if not json_files:
        print(f"No .json files found in '{folder}'.")
        sys.exit(0)

    rows   = [parse_feedback_file(p) for p in json_files]
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
        versions = set(r["app_version"] for r in clean if r["app_version"])
        if versions:
            print(f"App versions: {', '.join(sorted(versions))}")

        has_text = [r for r in clean if r["what_helped"] or r["what_to_change"] or r["general"]]
        print(f"Has comments: {len(has_text)} / {len(clean)} families")
        print()

        for r in clean:
            age   = f"{r['age_months']} mo" if r["age_months"] else "age unknown"
            diag  = r["diagnosis_or_condition"] or "no diagnosis"
            score = r["overall_usefulness"] or "no rating"
            uid   = r["user_id"][:8] + "…" if len(r["user_id"]) > 8 else r["user_id"] or "anon"
            print(f"  [{r['file']}]")
            print(f"    user: {uid} | {age} | {diag} | overall: {score}")
            if r["what_helped"]:
                preview = r["what_helped"][:80] + ("…" if len(r["what_helped"]) > 80 else "")
                print(f"    helped: \"{preview}\"")
            if r["what_to_change"]:
                preview = r["what_to_change"][:80] + ("…" if len(r["what_to_change"]) > 80 else "")
                print(f"    change: \"{preview}\"")

        print("───────────────────────────────────────────────────────")


if __name__ == "__main__":
    main()
