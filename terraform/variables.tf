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

variable "managed_projects" {
  type        = list(string)
  default     = []
  description = "Managed (spoke) projects governed by the hub: each gets AI log sinks (BigQuery + Pub/Sub) and runtime/enforcer role bindings."
}

variable "linked_projects" {
  type        = list(string)
  default     = []
  description = "Deprecated alias of managed_projects (merged)."
}

variable "enable_managed_apis" {
  type        = bool
  default     = true
  description = "Enable logging/iam/cloudresourcemanager/serviceusage/orgpolicy/cloudquotas/monitoring on every managed project."
}

variable "grant_managed_roles" {
  type        = bool
  default     = true
  description = "Grant least-privilege runtime/enforcer roles on every managed project (ignored when grant_scope_roles applies)."
}

variable "grant_scope_roles" {
  type        = bool
  default     = false
  description = "With scope_type folder/organization: grant the managed-project roles once on the folders/organization instead."
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
  description = "Central audit dataset project (default: the hub project when central_dataset = true)."
}

variable "central_dataset" {
  type        = bool
  default     = true
  description = "ONE dataset (in audit_project_id or the hub) receives the sinks of every project; false = legacy per-project datasets."
}

variable "reuse_dataset" {
  type        = string
  default     = ""
  description = "Adopt an existing dataset \"PROJECT:DATASET\" (read with a data source; not created or managed)."
  validation {
    condition     = var.reuse_dataset == "" || can(regex("^[a-z][a-z0-9.:-]{4,99}:[A-Za-z0-9_]{1,1024}$", var.reuse_dataset))
    error_message = "reuse_dataset must be PROJECT:DATASET."
  }
}

variable "enable_data_access_logs" {
  type        = bool
  default     = false
  description = "Enable Vertex AI (aiplatform.googleapis.com) ADMIN_READ/DATA_READ/DATA_WRITE audit logs on the hub and every managed project."
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
  type        = bool
  default     = false
  description = "Grant organization-only roles: Deny Admin (enforcer), Deny Reviewer + Organization Policy Administrator (API)."
}

variable "vpc_network" {
  type        = string
  default     = ""
  description = "Direct VPC egress network for Cloud Run (required when run.allowedVPCEgress is enforced)."
}

variable "vpc_subnet" {
  type        = string
  default     = ""
  description = "Subnet (same region, Private Google Access on) for Direct VPC egress."
}

variable "vpc_egress" {
  type        = string
  default     = "all-traffic"
  description = "all-traffic | private-ranges-only"
  validation {
    condition     = contains(["all-traffic", "private-ranges-only"], var.vpc_egress)
    error_message = "vpc_egress must be all-traffic or private-ranges-only."
  }
}

variable "no_invoker_iam_check" {
  type        = bool
  default     = false
  description = "Disable the Cloud Run invoker IAM check instead of granting allUsers (domain restricted sharing)."
}

variable "binary_authorization_policy" {
  type        = string
  default     = ""
  description = "Binary Authorization policy name required by run.allowedBinaryAuthorizationPolicies (e.g. default)."
}

variable "quota_contact_email" {
  type        = string
  default     = ""
  description = "Contact email for Cloud Quotas increase requests (QuotaPreference)."
}

variable "model_violation_action" {
  type        = string
  default     = "block_service"
  description = "Default action when a user calls a disallowed model."
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
