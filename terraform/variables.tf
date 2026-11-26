variable "project_id" {
  type        = string
  description = "Primary GCP project ID hosting the API service."
}

variable "region" {
  type        = string
  default     = "asia-northeast3"
  description = "Cloud Run and Cloud Scheduler region."
}

variable "service_name" {
  type        = string
  default     = "gcp-ai-usage-caps-api"
  description = "Cloud Run service name prefix."
}

variable "container_image" {
  type        = string
  description = "Container image URI (e.g. asia-northeast3-docker.pkg.dev/PROJECT/repo/gcp-ai-usage-caps-api:latest)."
}

variable "api_service_account_id" {
  type    = string
  default = "ai-usage-caps-api"
}

variable "enforcer_service_account_id" {
  type    = string
  default = "ai-usage-caps-enforcer"
}

variable "linked_projects" {
  type        = list(string)
  default     = []
  description = "Additional GCP project IDs monitored together in this API."
}

variable "scope_type" {
  type        = string
  default     = "projects"
  description = "Workspace project scope: projects | folder | organization."
}

variable "scope_ids" {
  type        = list(string)
  default     = []
  description = "Numeric folder or organization IDs when scope_type is folder or organization."
}

variable "audit_dataset_id" {
  type    = string
  default = "ge_analytics"
}

variable "audit_project_id" {
  type        = string
  default     = ""
  description = "Optional central audit project hosting an aggregated org/folder Log Sink dataset."
}

variable "dataset_location" {
  type    = string
  default = ""
}

variable "create_audit_sink" {
  type    = bool
  default = true
}

variable "quota_project_id" {
  type    = string
  default = ""
}

variable "billing_project_id" {
  type    = string
  default = ""
}

variable "billing_dataset_id" {
  type    = string
  default = ""
}

variable "billing_table_id" {
  type    = string
  default = ""
}

variable "billing_account_id" {
  type        = string
  default     = ""
  description = "Billing account ID (XXXXXX-XXXXXX-XXXXXX) for project-level Google Cloud Spend Caps."
}

variable "settings_bucket_name" {
  type    = string
  default = ""
}

variable "workspace_timezone" {
  type    = string
  default = "Asia/Seoul"
}

variable "public_base_url" {
  type        = string
  default     = ""
  description = "Public base URL for OIDC token audience validation (defaults to Cloud Run URL)."
}

variable "oidc_allowed_service_accounts" {
  type        = list(string)
  default     = []
  description = "External GCP service accounts permitted to call the API with OIDC Bearer tokens."
}

variable "bootstrap_admin_key_secret" {
  type        = string
  default     = ""
  description = "Optional Secret Manager secret name holding initial BOOTSTRAP_ADMIN_KEY."
}

variable "allow_unauthenticated_edge" {
  type        = bool
  default     = true
  description = "Allow external HTTPS portal calls to reach Cloud Run (the API enforces Bearer API Key / OIDC auth)."
}

variable "ingress" {
  type    = string
  default = "INGRESS_TRAFFIC_ALL"
}

variable "min_instances" {
  type    = number
  default = 0
}

variable "max_instances" {
  type    = number
  default = 5
}

variable "cpu" {
  type    = string
  default = "1"
}

variable "memory" {
  type    = string
  default = "512Mi"
}

variable "enforcement_backend" {
  type    = string
  default = "iam_deny"
}

variable "enforcement_mode" {
  type    = string
  default = "manual"
}

variable "deny_attachment" {
  type    = string
  default = "projects"
}

variable "deny_policy_id" {
  type    = string
  default = "gcp-ai-caps-managed"
}

variable "organization_id" {
  type        = string
  default     = ""
  description = "Numeric GCP organization ID (required for org-level IAM Deny Admin/Reviewer grants)."
}

variable "grant_org_roles" {
  type    = bool
  default = false
}

variable "realtime_enforcement" {
  type    = bool
  default = true
}

variable "spend_caps_enabled" {
  type    = bool
  default = false
}

variable "cap_currency" {
  type    = string
  default = "USD"
}

variable "enforcement_schedule" {
  type    = string
  default = "*/5 * * * *"
}

variable "pricing_sync_schedule" {
  type    = string
  default = "0 3 * * *"
}
