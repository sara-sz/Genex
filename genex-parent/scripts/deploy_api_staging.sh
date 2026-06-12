#!/usr/bin/env bash
# scripts/deploy_api_staging.sh
# ──────────────────────────────────────────────────────────────────────────────
# Builds and deploys the Genex FastAPI service to Cloud Run as genex-api-staging.
#
# SAFE: Does NOT touch genex-parent or genex-parent-staging (Streamlit services).
#
# Build strategy: gcloud builds submit (Cloud Build) — no Docker required locally.
#
# Prerequisites:
#   gcloud auth login                        (if not already authenticated)
#   gcloud auth application-default login    (for ADC — needed by Cloud Build)
#   Cloud Build API enabled in genex-mvp-2026
#
# Usage:
#   cd /Users/sara/Projects/Genex/genex-parent
#   bash scripts/deploy_api_staging.sh
#
# To skip the Cloud Build step (re-deploy the existing image without rebuilding):
#   SKIP_BUILD=1 bash scripts/deploy_api_staging.sh
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail
cd "$(dirname "$0")/.."

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT_ID="genex-mvp-2026"
REGION="us-central1"
SERVICE_NAME="genex-api-staging"
IMAGE="us-central1-docker.pkg.dev/${PROJECT_ID}/genex-parent/${SERVICE_NAME}:latest"

GCS_BUCKET="genex-parent-sessions-genex-mvp-2026"
FIREBASE_PROJECT_ID="genex-mvp-2026"

# ACTIVITY_MODEL — OpenAI model name used by genex_core activity_engine.
# This is a model name, not a secret, so it is set as a plain env var.
# Override with: ACTIVITY_MODEL=gpt-4o bash scripts/deploy_api_staging.sh
# genex_core falls back gracefully (warning only) if this is empty.
ACTIVITY_MODEL="${ACTIVITY_MODEL:-gpt-4o-mini}"

# ALLOWED_ORIGINS: comma-separated list of permitted CORS origins.
# Add Lovable preview URL here once known. Do NOT use * in production.
# ⚠️  TEMPORARY: localhost origins for local Lovable dev only.
#     Update this before connecting the real Lovable staging domain.
ALLOWED_ORIGINS="http://localhost:3000,http://localhost:5173"

ALLOWED_EMAILS="soltanizadehsara@protonmail.com,soltanizadehsara@gmail.com"

# ── Safety check: confirm gcloud project ─────────────────────────────────────
echo "══════════════════════════════════════════════════════════════"
echo "  Deploying: ${SERVICE_NAME}"
echo "  Project:   ${PROJECT_ID}"
echo "  Region:    ${REGION}"
echo "  Image:     ${IMAGE}"
echo "══════════════════════════════════════════════════════════════"
echo ""

ACTIVE_PROJECT=$(gcloud config get-value project 2>/dev/null || echo "")
if [[ "${ACTIVE_PROJECT}" != "${PROJECT_ID}" ]]; then
    echo "⚠️  gcloud active project is '${ACTIVE_PROJECT}', expected '${PROJECT_ID}'."
    echo "   Run: gcloud config set project ${PROJECT_ID}"
    exit 1
fi

# ── Step 1: Build image via Cloud Build (no Docker needed locally) ────────────
if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
    echo "── Step 1: Building image with Cloud Build ─────────────────────"
    echo "   (uploads source to GCS, builds remotely, pushes to Artifact Registry)"
    echo ""
    gcloud builds submit \
        --config=cloudbuild.api.yaml \
        --substitutions="_IMAGE=${IMAGE}" \
        .
    echo ""
else
    echo "── Step 1: Skipped (SKIP_BUILD=1) — using existing image ───────"
    echo ""
fi

# ── Step 3: Deploy to Cloud Run ───────────────────────────────────────────────
echo "── Step 3: Deploying to Cloud Run ─────────────────────────────"

# OPENAI_API_KEY is a real secret → injected from Secret Manager.
# ACTIVITY_MODEL is a model name → plain env var (no secret needed).
# Format for --set-secrets: ENV_VAR_NAME=SECRET_NAME:version
# Using "latest" so a secret rotation takes effect on next deploy.
#
# --set-env-vars uses ^|^ as the key=value pair separator (instead of the
# default comma) so that comma-containing values like ALLOWED_EMAILS and
# ALLOWED_ORIGINS are passed through verbatim without gcloud misreading
# the embedded commas as new key=value delimiters.

gcloud run deploy "${SERVICE_NAME}" \
    --image="${IMAGE}" \
    --region="${REGION}" \
    --platform=managed \
    --allow-unauthenticated \
    --min-instances=1 \
    --max-instances=2 \
    --memory=1Gi \
    --cpu=1 \
    --timeout=300 \
    --set-env-vars="^|^GCS_BUCKET=${GCS_BUCKET}|FIREBASE_PROJECT_ID=${FIREBASE_PROJECT_ID}|ALLOWED_EMAILS=${ALLOWED_EMAILS}|LOCAL_SESSION_FALLBACK=0|ADMIN_DEBUG=0|ALLOWED_ORIGINS=${ALLOWED_ORIGINS}|ACTIVITY_MODEL=${ACTIVITY_MODEL}" \
    --set-secrets="OPENAI_API_KEY=OPENAI_API_KEY:latest" \
    --port=8080

echo ""
echo "── Step 4: Fetching service URL ────────────────────────────────"
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
    --region="${REGION}" \
    --format="value(status.url)")

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  ✅ Deployed: ${SERVICE_NAME}"
echo "  URL: ${SERVICE_URL}"
echo ""
echo "  Env vars set (non-secret):"
echo "    GCS_BUCKET             = ${GCS_BUCKET}"
echo "    FIREBASE_PROJECT_ID    = ${FIREBASE_PROJECT_ID}"
echo "    ALLOWED_EMAILS         = ${ALLOWED_EMAILS}"
echo "    LOCAL_SESSION_FALLBACK = 0"
echo "    ADMIN_DEBUG            = 0"
echo "    ALLOWED_ORIGINS        = ${ALLOWED_ORIGINS}"
echo "    ACTIVITY_MODEL         = ${ACTIVITY_MODEL}"
echo "  Secrets injected from Secret Manager:"
echo "    OPENAI_API_KEY  ← OPENAI_API_KEY:latest"
echo ""
echo "  ✅ Service is --allow-unauthenticated (Cloud Run IAM layer is open)."
echo "     Auth is enforced by FastAPI, not Cloud Run IAM:"
echo "       /health              → public (no token required)"
echo "       all other routes     → require Authorization: Bearer <Firebase ID token>"
echo "       allowlist enforced   → 403 if email not in ALLOWED_EMAILS"
echo "       session ownership    → 403 if session belongs to different uid"
echo ""
echo "  ⚠️  CORS allows only: ${ALLOWED_ORIGINS}"
echo "     Add Lovable preview URL before connecting frontend."
echo "══════════════════════════════════════════════════════════════"
echo ""
echo "Next: get a Firebase token and run the deployed smoke test:"
echo "  export GENEX_API_URL=${SERVICE_URL}"
echo "  python3 scripts/get_test_token.py   # prints GENEX_API_TOKEN"
echo "  export GENEX_API_TOKEN=<paste token>"
echo "  python3 tests/test_step6a_deployed.py"
