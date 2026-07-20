# DESIGN ONLY — not applied. One reusable module for a full therapist environment.

variable "environment" {
  type        = string
  description = "dev | prod"
  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be 'dev' or 'prod'."
  }
}

variable "project_id" {
  type        = string
  description = "Canonical GCP+Firebase project (genex-provider-{dev,prod}-2026)."
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "cloud_run_service" {
  type        = string
  description = "genex-api-therapist-{dev,prod}"
}

variable "service_account_id" {
  type        = string
  description = "therapist-api-{dev,prod}-sa"
}

variable "artifact_registry_repo" {
  type    = string
  default = "genex-therapist"
}

variable "image_digest" {
  type        = string
  description = "Immutable image digest to deploy (promotion unit; never a moving tag)."
}

variable "allowed_origins" {
  type        = list(string)
  description = "CORS origins (no wildcard; dev/prod never mixed)."
}

variable "min_instance_count" {
  type        = number
  description = "Prod is 0 while dark."
  default     = 0
}

variable "max_instance_count" {
  type    = number
  default = 2
}

variable "require_authenticated_invocation" {
  type        = bool
  description = "Prod-dark requires authenticated invocation."
  default     = true
}

variable "seed_enabled" {
  type    = bool
  default = false
}

variable "debug_panels" {
  type    = bool
  default = false
}

variable "registration_policy" {
  type = string
}

variable "budget_amount_usd" {
  type    = number
  default = 25
}
