output "api_url" {
  value       = google_cloud_run_v2_service.api.uri
  description = "Base URL of the Headless GCP AI Usage & Spend Caps API."
}

output "openapi_url" {
  value       = "${google_cloud_run_v2_service.api.uri}/v1/openapi.yaml"
  description = "OpenAPI 3.1 specification endpoint."
}

output "api_service_account" {
  value       = google_service_account.api_sa.email
  description = "Runtime service account of the Cloud Run API service."
}

output "enforcer_service_account" {
  value       = google_service_account.enforcer_sa.email
  description = "Dedicated service account of the Cloud Run Enforcer Job (grants IAM Deny Admin)."
}

output "enforcer_job_name" {
  value       = google_cloud_run_v2_job.enforcer.name
  description = "Cloud Run v2 Job name running enforcement cycles."
}

output "state_bucket" {
  value       = google_storage_bucket.state.name
  description = "Private versioned GCS bucket storing policies, hashed API keys, and webhooks."
}

output "org_iam_commands" {
  value = var.grant_org_roles ? [] : [
    "gcloud organizations add-iam-policy-binding ${var.organization_id != "" ? var.organization_id : "ORGANIZATION_ID"} --member=serviceAccount:${google_service_account.enforcer_sa.email} --role=roles/iam.denyAdmin --condition=None",
    "gcloud organizations add-iam-policy-binding ${var.organization_id != "" ? var.organization_id : "ORGANIZATION_ID"} --member=serviceAccount:${google_service_account.api_sa.email} --role=roles/iam.denyReviewer --condition=None",
  ]
  description = "Commands for an Organization Admin to enable IAM Deny v2 enforcement when grant_org_roles=false."
}
