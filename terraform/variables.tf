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
  description = "Enable Gemini Platform (aiplatform.googleapis.com) ADMIN_READ/DATA_READ/DATA_WRITE audit logs on the hub and every managed project."
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
  description = "Public base URL (or comma-separated dual Cloud Run URLs) for OIDC token audience validation (defaults to Cloud Run URL)."
}

variable "oidc_allowed_service_accounts" {
  type        = list(string)
  default     = []
  description = "External GCP service accounts permitted to call the API with OIDC Bearer tokens."
}

variable "oidc_internal_service_accounts" {
  type        = list(string)
  default     = []
  description = "Additional GCP service accounts allowed to call /v1/internal/* (e.g. custom Pub/Sub push or Cloud Scheduler SAs)."
}

variable "grant_billing_roles" {
  type        = bool
  default     = false
  description = "Grant roles/billing.costsManager on billing_account_id to the API runtime SA (requires roles/billing.admin on the billing account; needed for project hard spend caps)."
}

variable "bootstrap_admin_key_secret" {
  type        = string
  default     = ""
  description = "Existing Secret Manager secret holding BOOTSTRAP_ADMIN_KEY. Empty (default): a key is generated and stored in <service_name>-bootstrap-key."
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

# v1.2 -------------------------------------------------------------------------------------------------------------
# Costs = log token counts x official Cloud Billing Catalog SKU prices (no Cloud Billing export variables anymore).

variable "enable_vertex_inference_logging" {
  type        = bool
  default     = true
  description = "Enable Gemini Platform inference request metadata logging (aiplatform.googleapis.com/inference_request, the per-request Gemini Platform token source) on the hub and every managed project. No native Terraform resource exists (hashicorp/terraform-provider-google#30642), so a terraform_data local-exec calls the v1beta1 setPublisherModelConfig REST method with curl (needs bash + curl where terraform runs, and aiplatform.endpoints.setPublisherModelConfig for the Terraform identity). Cloud Logging ingestion/storage pricing applies to the entries."
}

variable "vertex_logging_endpoint" {
  type        = string
  default     = "https://aiplatform.googleapis.com/v1beta1"
  description = "v1beta1 Gemini Platform (formerly Gemini Platform) endpoint used to enable inference request metadata logging (e.g. a Private Service Connect endpoint under VPC Service Controls)."
  validation {
    condition     = can(regex("^https://[A-Za-z0-9.-]+(:[0-9]+)?/v1beta1$", var.vertex_logging_endpoint))
    error_message = "vertex_logging_endpoint must look like https://HOST/v1beta1."
  }
}

variable "create_vertex_logging_reader_role" {
  type        = bool
  default     = true
  description = "Create the one-permission custom role gcpAiCapsVertexLoggingReader (aiplatform.endpoints.fetchPublisherModelConfig) in the hub and every managed project and grant it to the runtime service account (readiness + GET /v1/projects/{p}/vertex-logging). Import existing roles created by scripts/deploy.sh instead of recreating them."
}

variable "cap_scope" {
  type        = string
  default     = ""
  description = "Optional CAP_SCOPE: total | per_project (empty = application default)."
  validation {
    condition     = contains(["", "total", "per_project"], var.cap_scope)
    error_message = "cap_scope must be empty, total or per_project."
  }
}

variable "vertex_user_header_label" {
  type        = string
  default     = ""
  description = "Optional VERTEX_USER_HEADER_LABEL: custom request header label carrying the end user in Gemini Platform inference request logs."
}

variable "metered_surfaces" {
  type        = list(string)
  default     = []
  description = "Optional METERED_SURFACES (e.g. [\"antigravity\", \"gemini_platform\", \"gemini_enterprise\"]); empty = application default."
}

variable "model_violation_block_scope" {
  type        = string
  default     = ""
  description = "Optional MODEL_VIOLATION_BLOCK_SCOPE: project | all (empty = application default)."
  validation {
    condition     = contains(["", "project", "all"], var.model_violation_block_scope)
    error_message = "model_violation_block_scope must be empty, project or all."
  }
}

variable "service_account_subjects" {
  type        = bool
  default     = false
  description = "Treat service accounts as policy subjects (usage attribution from the inference log principal, caps, rules, IAM Deny). The hub runtime and enforcer service accounts are always protected."
}
