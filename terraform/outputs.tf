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
    "gcloud organizations add-iam-policy-binding ${var.organization_id != "" ? var.organization_id : "ORGANIZATION_ID"} --member=serviceAccount:${google_service_account.api_sa.email} --role=roles/orgpolicy.policyAdmin --condition=None",
  ]
  description = "Commands for an Organization Admin (IAM Deny v2 enforcement + model governance org policies) when grant_org_roles=false."
}

output "managed_projects" {
  value       = local.managed_projects
  description = "Managed (spoke) projects governed by the hub."
}

output "audit_dataset" {
  value       = local.central ? "${local.audit_project}:${local.dataset_id}" : "<project>:${local.dataset_id}"
  description = "Central audit dataset (or the per-project dataset pattern)."
}

output "ai_log_sink_writers" {
  value       = { for p, s in google_logging_project_sink.ai_logs_bq : p => s.writer_identity }
  description = "Writer identity of the AI log sink in each project (granted BigQuery Data Editor on the destination dataset)."
}

output "usage_event_sink_writers" {
  value       = { for p, s in google_logging_project_sink.usage_events : p => s.writer_identity }
  description = "Writer identity of the real-time Pub/Sub sink in each project (granted Pub/Sub Publisher on the hub topic)."
}

output "bootstrap_admin_key_command" {
  description = "Prints the bootstrap admin API key (Secret Manager)."
  value       = "gcloud secrets versions access latest --secret=${local.bootstrap_secret} --project=${var.project_id}"
}
