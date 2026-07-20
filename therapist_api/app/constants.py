"""Shared, non-secret constants for the therapist service.

Nothing in this module is a secret. Project IDs, service names, and origins are
public identifiers. Real secret values live only in Secret Manager (never here,
never in env files committed to git).
"""

from __future__ import annotations

SERVICE_NAME = "genex-api-therapist"
API_VERSION = "v1"

# The two supported deployment environments. Prod ships DARK (no users/seed).
ENVIRONMENTS = ("dev", "prod")

# Canonical GCP + Firebase project per environment (founder-approved naming).
# "provider" = shared infra so the platform can later host SLP/OT/PT/MD/educators.
CANONICAL_PROJECT = {
    "dev": "genex-provider-dev-2026",
    "prod": "genex-provider-prod-2026",
}

# Cloud Run service name per environment ("therapist" = current SLP-first app).
CLOUD_RUN_SERVICE = {
    "dev": "genex-api-therapist-dev",
    "prod": "genex-api-therapist-prod",
}

# Service accounts per environment.
SERVICE_ACCOUNT = {
    "dev": "therapist-api-dev-sa",
    "prod": "therapist-api-prod-sa",
}

# Front-end origins (Lovable). Dev is canonical; Prod receives promoted releases.
DEV_LOVABLE_ORIGIN = "https://genex-therapist-dev.lovable.app"
PROD_LOVABLE_ORIGIN = "https://genex-therapist-prod.lovable.app"
DEV_LOCAL_ORIGINS = ("http://localhost:3000", "http://localhost:5173")

# Registration policy per environment. Prod stays invite-only while dark.
REGISTRATION_POLICY = {
    "dev": "open-dev",
    "prod": "invite-only",
}

# Developmental-domain display taxonomy (correction #6).
# These are the EXACT current Genex parent-facing labels, treated as versioned
# display values. We do NOT force a permanent mapping into the parent backend's
# four canonical domains in this phase — that is a Parent Beta 2.4 design item.
DOMAIN_TAXONOMY_VERSION = "genex-parent-display-v1"
DISPLAY_DOMAINS = (
    "Talking & Communicating",
    "Social & Emotional",
    "Learning & Thinking",
    "Movement & Physical",
    "Daily Living",
    "Sensory",
    "Fine Motor",
    "Gross Motor",
)
