# DESIGN ONLY — not applied. Dev instantiation of the therapist environment.

module "dev" {
  source = "../../modules/therapist_environment"

  environment       = "dev"
  project_id        = "genex-provider-dev-2026"
  region            = "us-central1"
  cloud_run_service = "genex-api-therapist-dev"
  service_account_id = "therapist-api-dev-sa"

  image_digest = "sha256:REPLACE_WITH_TESTED_DEV_DIGEST"

  allowed_origins = [
    "https://genex-therapist-dev.lovable.app",
    "http://localhost:5173",
    "http://localhost:3000",
  ]

  min_instance_count               = 0
  max_instance_count               = 2
  require_authenticated_invocation = false # dev may be reachable by the dev frontend
  seed_enabled                     = true
  debug_panels                     = true
  registration_policy              = "open-dev"
  budget_amount_usd                = 25
}
