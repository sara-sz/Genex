# DESIGN ONLY — not applied.

output "cloud_run_service" {
  value = google_cloud_run_v2_service.api.name
}

output "service_account_email" {
  value = google_service_account.therapist_api.email
}

output "artifact_registry" {
  value = google_artifact_registry_repository.images.name
}

output "firestore_database" {
  value = google_firestore_database.db.name
}
