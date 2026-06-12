#!/usr/bin/env bash
# scripts/run_step5_live.sh
# ──────────────────────────────────────────────────────────────────────────────
# Step 5.5 live API smoke test runner.
#
# Uses FastAPI's TestClient (in-process ASGI) so uvicorn does NOT need to be
# running. Firebase auth is bypassed via dependency_overrides. No GCS needed.
#
# Prerequisites (must be installed in the active Python env):
#   fastapi uvicorn pydantic firebase-admin google-cloud-storage
#   openai pandas openpyxl python-dotenv requests
#
# Usage:
#   cd /Users/sara/Projects/Genex/genex-parent
#   bash scripts/run_step5_live.sh
#
# To run with a real GCS bucket (optional — uses GOOGLE_APPLICATION_CREDENTIALS):
#   GCS_BUCKET=your-bucket bash scripts/run_step5_live.sh
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail
cd "$(dirname "$0")/.."

echo "══════════════════════════════════════════════════════════════"
echo "  Step 5.5 — Live API smoke test"
echo "══════════════════════════════════════════════════════════════"
echo ""

# Env vars (override any already set in shell)
export LOCAL_SESSION_FALLBACK="${LOCAL_SESSION_FALLBACK:-1}"
export GCS_BUCKET="${GCS_BUCKET:-}"
export ALLOWED_EMAILS="${ALLOWED_EMAILS:-soltanizadehsara@protonmail.com}"
export FIREBASE_PROJECT_ID="${FIREBASE_PROJECT_ID:-genex-smoke-test}"
export ADMIN_DEBUG="${ADMIN_DEBUG:-1}"

echo "  LOCAL_SESSION_FALLBACK = $LOCAL_SESSION_FALLBACK"
echo "  GCS_BUCKET             = ${GCS_BUCKET:-(empty — local fallback)}"
echo "  ALLOWED_EMAILS         = $ALLOWED_EMAILS"
echo "  ADMIN_DEBUG            = $ADMIN_DEBUG"
echo ""

python3 tests/test_step5_live.py
