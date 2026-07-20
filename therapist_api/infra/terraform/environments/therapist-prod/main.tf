# DESIGN ONLY — not applied. Prod-DARK instantiation of the therapist environment.

module "prod" {
  source = "../../modules/therapist_environment"

  environment       = "prod"
  project_id        = "genex-provider-prod-2026"
  region            = "us-central1"
  cloud_run_service = "genex-api-therapist-prod"
  service_account_id = "therapist-api-prod-sa"

  # Prod deploys ONLY a digest promoted (copied + verified) from dev — never a rebuild.
  image_digest = "sha256:REPLACE_WITH_PROMOTED_VERIFIED_DIGEST"

  allowed_origins = [
    "https://genex-therapist-prod.lovable.app",
  ]

  # DARK posture:
  min_instance_count               = 0     # no warm capacity
  max_instance_count               = 2
  require_authenticated_invocation = true  # authenticated invocation only
  seed_enabled                     = false # no seeds
  debug_panels                     = false # no debug bypass
  registration_policy              = "invite-only"
  budget_amount_usd                = 25
}
