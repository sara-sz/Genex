# DESIGN ONLY — not applied. Illustrative resource shape for one environment.

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  common_labels = {
    app         = "genex-therapist"
    environment = var.environment
    managed_by  = "terraform"
  }
}

# --- Required APIs -----------------------------------------------------------
resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "firestore.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
    "cloudbuild.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
    "billingbudgets.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# --- Service account ---------------------------------------------------------
resource "google_service_account" "therapist_api" {
  account_id   = var.service_account_id
  display_name = "Therapist API (${var.environment})"
}

# --- Firestore (Native) — Prod stays empty while dark ------------------------
resource "google_firestore_database" "db" {
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  # Deletion protection on prod.
  deletion_policy = var.environment == "prod" ? "DELETE_PROTECTION_ENABLED" : "DELETE_PROTECTION_DISABLED"
}

# --- Artifact Registry (separate per project) --------------------------------
resource "google_artifact_registry_repository" "images" {
  repository_id = var.artifact_registry_repo
  format        = "DOCKER"
  location      = var.region
  labels        = local.common_labels
}

# --- Cloud Run service -------------------------------------------------------
resource "google_cloud_run_v2_service" "api" {
  name     = var.cloud_run_service
  location = var.region
  ingress  = var.require_authenticated_invocation ? "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER" : "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.therapist_api.email

    scaling {
      min_instance_count = var.min_instance_count # prod = 0 while dark
      max_instance_count = var.max_instance_count
    }

    containers {
      # Immutable digest — never a moving tag (correction #4).
      image = "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_registry_repo}/genex-api-therapist@${var.image_digest}"

      env {
        name  = "ENVIRONMENT"
        value = var.environment
      }
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "FIREBASE_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "FIRESTORE_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "REGION"
        value = var.region
      }
      env {
        name  = "ALLOWED_ORIGINS"
        value = join(",", var.allowed_origins)
      }
      env {
        name  = "SEED_ENABLED"
        value = tostring(var.seed_enabled)
      }
      env {
        name  = "DEBUG_PANELS"
        value = tostring(var.debug_panels)
      }
      env {
        name  = "REGISTRATION_POLICY"
        value = var.registration_policy
      }
      # Secrets are referenced from Secret Manager, never inlined here.
    }
  }

  labels = local.common_labels
}

# Prod-dark: do NOT grant public (allUsers) invoker. Authenticated invocation only.
# (Dev may add a controlled invoker binding in its own environment config.)

# --- Budget alert ------------------------------------------------------------
# (Illustrative — a real budget needs the billing account id, provided out-of-band.)
