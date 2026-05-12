#!/usr/bin/env python3
"""
privacy_check.py — Genex Parent Copilot Privacy Leakage Test
-------------------------------------------------------------
Scans local output files to confirm a known test string does NOT appear.
Use this after running through the app with a child name set to a unique
sentinel value to verify the name was never persisted anywhere.

How to use
----------
1. Run the app locally:
       streamlit run app.py

2. In the profile screen, enter this EXACT child name:
       PRIVACY_TEST_CHILD_DO_NOT_STORE

3. Complete the full flow (answer all questions, reach Weekly Plan and Feedback).

4. Submit feedback.

5. Quit the app and run:
       python3 privacy_check.py

The script will scan outputs/ and report any file containing the sentinel string.
A clean pass means: PASSED — the child name was not persisted anywhere.

GCS check (if testing against staging):
---------------------------------------
Download files and scan them:
    mkdir -p /tmp/privacy-check
    gcloud storage cp -r \\
        "gs://genex-parent-sessions-genex-mvp-2026/sessions/" /tmp/privacy-check/
    gcloud storage cp -r \\
        "gs://genex-parent-sessions-genex-mvp-2026/feedback/" /tmp/privacy-check/
    python3 privacy_check.py /tmp/privacy-check/

Or scan in-place with gsutil:
    gcloud storage cat "gs://genex-parent-sessions-genex-mvp-2026/**" 2>/dev/null \\
        | grep -i "PRIVACY_TEST_CHILD_DO_NOT_STORE" \\
        && echo "LEAK FOUND" || echo "CLEAN"
"""

import json
import sys
from pathlib import Path

SENTINEL = "PRIVACY_TEST_CHILD_DO_NOT_STORE"


def scan_folder(folder: Path) -> tuple[list, list]:
    """
    Recursively scan all files in folder for the sentinel string.
    Returns (leaking_files, checked_files).
    """
    leaking = []
    checked = []

    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            checked.append(path)
            if SENTINEL.lower() in text.lower():
                leaking.append(path)
        except Exception as exc:
            print(f"  Could not read {path}: {exc}")

    return leaking, checked


def main():
    scan_root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("outputs")

    if not scan_root.exists():
        print(f"Folder '{scan_root}' does not exist.")
        print("Run the app first and submit feedback, then re-run this script.")
        sys.exit(0)

    print(f"\nScanning: {scan_root.resolve()}")
    print(f"Sentinel: {SENTINEL}\n")

    leaking, checked = scan_folder(scan_root)

    print(f"Files checked: {len(checked)}")
    for f in checked:
        print(f"  {f}")

    print()
    if leaking:
        print("═" * 60)
        print("❌  PRIVACY LEAK DETECTED")
        print("═" * 60)
        print(f"\nSentinel string found in {len(leaking)} file(s):\n")
        for f in leaking:
            print(f"  ⚠  {f}")
            # Show the matching line(s)
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                    if SENTINEL.lower() in line.lower():
                        print(f"     line {i}: {line.strip()[:120]}")
            except Exception:
                pass
        print(
            "\nAction required: the child name is being stored somewhere. "
            "Check save_session_json() and screen_feedback() in app.py."
        )
        sys.exit(1)
    else:
        print("═" * 60)
        print("✅  PASSED — sentinel not found in any output file")
        print("═" * 60)
        print(
            "\nThe child name was not persisted to disk. "
            "Privacy storage check passed."
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
