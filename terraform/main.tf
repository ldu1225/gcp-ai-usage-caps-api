# GCP AI Usage & Spend Caps API - hub-and-spoke deployment
#
# ONE hub project (var.project_id) hosts:
# - Cloud Run v2 Service: headless M2M REST API (API key + Google OIDC auth, zero UI)
# - Cloud Run v2 Job (`<service>-enforcer`): runs `python -m usage_caps_api.enforce_job` as its own service account,
#   the ONLY identity granted organization-level `roles/iam.denyAdmin`
# - GCS bucket: private, versioned JSON document store (policies, hashed API keys, webhooks, pricing, readiness)
# - ONE central BigQuery dataset (default) receiving a Log Router sink from EVERY managed project
# - ONE Pub/Sub topic + push subscription fed by a Pub/Sub sink in EVERY managed project (real-time usage events)
# - Cloud Scheduler: 5-minute enforcement safety net + daily pricing sync
#
# MANY managed projects (var.managed_projects; or folder/organization scope via var.scope_type/var.scope_ids):
# - Log Router sinks (BigQuery -> central dataset, Pub/Sub -> hub topic), writer identities granted on the hub only
# - least-privilege roles for the runtime and enforcer service accounts (or folder/org-level grants)
# - optional Vertex AI Data Access audit logs (google_project_iam_audit_config)
# - Vertex AI inference request metadata logging (var.enable_vertex_inference_logging, local-exec REST call) and the
#   one-permission custom role gcpAiCapsVertexLoggingReader for the runtime service account
#
# Existing resources are adopted instead of duplicated: var.reuse_dataset = "PROJECT:DATASET" reads the dataset with a
# data source and only adds sinks/grants. Run `scripts/preflight.sh` first to detect adoptable datasets and sinks.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0.0, < 8.0.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  api_sa_member      = "serviceAccount:${google_service_account.api_sa.email}"
  enforcer_sa_member = "serviceAccount:${google_service_account.enforcer_sa.email}"
  quota_project      = var.quota_project_id != "" ? var.quota_project_id : var.project_id
  settings_bucket    = var.settings_bucket_name != "" ? var.settings_bucket_name : "${var.project_id}-${var.service_name}-state"
  dataset_location   = var.dataset_location != "" ? var.dataset_location : var.region
  run_url            = "https://${var.service_name}-${data.google_project.this.number}.${var.region}.run.app"
  public_base_url    = var.public_base_url != "" ? trimsuffix(var.public_base_url, "/") : local.run_url
  enforcer_job_name  = "${var.service_name}-enforcer"
  enforcer_job_ref   = "projects/${var.project_id}/locations/${var.region}/jobs/${local.enforcer_job_name}"
  usage_topic_name   = "${var.service_name}-usage-events"
  usage_sub_id       = "${var.service_name}-usage-events-push"
  usage_sub_ref      = "projects/${var.project_id}/subscriptions/${local.usage_sub_id}"

  # Hub-and-spoke topology ---------------------------------------------------------------------------------------
  managed_projects = [for p in distinct(concat(var.managed_projects, var.linked_projects)) : p if p != var.project_id]
  sink_projects    = concat([var.project_id], local.managed_projects)
  reuse            = var.reuse_dataset != ""
  reuse_parts      = local.reuse ? split(":", var.reuse_dataset) : ["", ""]
  dataset_id       = local.reuse ? local.reuse_parts[1] : var.audit_dataset_id
  central          = local.reuse || var.central_dataset
  audit_project    = local.reuse ? local.reuse_parts[0] : (var.central_dataset ? (var.audit_project_id != "" ? var.audit_project_id : var.project_id) : "")
  # Projects whose dataset this configuration creates (none when an existing dataset is adopted).
  dataset_projects = var.create_audit_sink && !local.reuse ? (local.central ? [local.audit_project] : local.sink_projects) : []
  sink_destination = { for p in local.sink_projects : p => (local.central ? local.audit_project : p) }
  scope_grants     = var.grant_scope_roles && contains(["folder", "organization"], var.scope_type)

  required_apis = concat([
    "bigquery.googleapis.com",
    "logging.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "run.googleapis.com",
    "storage.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbilling.googleapis.com",
    "serviceusage.googleapis.com",
    "cloudscheduler.googleapis.com",
    "pubsub.googleapis.com",
    "orgpolicy.googleapis.com",
    "cloudquotas.googleapis.com",
    "monitoring.googleapis.com",
    "aiplatform.googleapis.com",
  ], var.spend_caps_enabled ? ["billingbudgets.googleapis.com"] : [])

  managed_apis = concat([
    "logging.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "serviceusage.googleapis.com",
    "orgpolicy.googleapis.com",
    "cloudquotas.googleapis.com",
    "monitoring.googleapis.com",
  ], local.central ? [] : ["bigquery.googleapis.com"])

  api_roles = [
    "roles/browser",
    "roles/bigquery.dataViewer",
    "roles/bigquery.jobUser",
    "roles/logging.viewer",
    "roles/logging.privateLogViewer",
    "roles/logging.logWriter",
    "roles/serviceusage.serviceUsageConsumer",
    "roles/monitoring.viewer",
    "roles/cloudquotas.admin",
    "roles/discoveryengine.editor",
  ]

  enforcer_roles = [
    "roles/bigquery.dataViewer",
    "roles/bigquery.jobUser",
    "roles/logging.viewer",
    "roles/logging.privateLogViewer",
    "roles/logging.logWriter",
    "roles/serviceusage.serviceUsageConsumer",
  ]

  api_managed_roles = concat([
    "roles/browser",
    "roles/logging.viewer",
    "roles/logging.privateLogViewer",
    "roles/monitoring.viewer",
    "roles/cloudquotas.admin",
    "roles/discoveryengine.editor",
    "roles/serviceusage.serviceUsageConsumer",
  ], local.central ? [] : ["roles/bigquery.dataViewer"])

  enforcer_managed_roles = concat([
    "roles/logging.viewer",
    "roles/logging.privateLogViewer",
  ], local.central ? [] : ["roles/bigquery.dataViewer"])

  # Organization-only roles: Deny Admin (enforcer), Deny Reviewer + Organization Policy Administrator (API, model
  # governance writes of constraints/vertexai.allowedModels).
  org_role_grants = {
    "enforcer|roles/iam.denyAdmin"    = local.enforcer_sa_member
    "api|roles/iam.denyReviewer"      = local.api_sa_member
    "api|roles/orgpolicy.policyAdmin" = local.api_sa_member
  }

  managed_api_pairs = var.enable_managed_apis ? {
    for pair in setproduct(local.managed_projects, local.managed_apis) : "${pair[0]}|${pair[1]}" => { project = pair[0], service = pair[1] }
  } : {}
  api_managed_pairs = var.grant_managed_roles && !local.scope_grants ? {
    for pair in setproduct(local.managed_projects, local.api_managed_roles) : "${pair[0]}|${pair[1]}" => { project = pair[0], role = pair[1] }
  } : {}
  enforcer_managed_pairs = var.grant_managed_roles && !local.scope_grants ? {
    for pair in setproduct(local.managed_projects, local.enforcer_managed_roles) : "${pair[0]}|${pair[1]}" => { project = pair[0], role = pair[1] }
  } : {}
  scope_role_pairs = local.scope_grants ? merge(
    { for pair in setproduct(var.scope_ids, local.api_managed_roles) : "api|${pair[0]}|${pair[1]}" => { id = pair[0], role = pair[1], member = local.api_sa_member } },
    { for pair in setproduct(var.scope_ids, local.enforcer_managed_roles) : "enforcer|${pair[0]}|${pair[1]}" => { id = pair[0], role = pair[1], member = local.enforcer_sa_member } },
  ) : {}

  ai_log_filter = join(" OR ", [
    "log_id(\"businessaicode.googleapis.com/inference_response\")",
    "log_id(\"aiplatform.googleapis.com/inference_request\")",
    "(log_id(\"cloudaudit.googleapis.com/data_access\") AND protoPayload.serviceName=\"aiplatform.googleapis.com\")",
    "log_id(\"discoveryengine.googleapis.com/gemini_enterprise_user_activity\")",
    "log_id(\"discoveryengine.googleapis.com/gen_ai_user_message\")",
  ])

  # Real-time usage events: Antigravity inference responses + Vertex AI inference request metadata.
  realtime_filter = join(" OR ", [
    "log_id(\"businessaicode.googleapis.com/inference_response\")",
    "log_id(\"aiplatform.googleapis.com/inference_request\")",
  ])

  vertex_logging_reader_role_id = "gcpAiCapsVertexLoggingReader"
  vertex_reader_projects        = var.create_vertex_logging_reader_role ? toset(concat([var.project_id], var.grant_managed_roles ? local.managed_projects : [])) : toset([])

  common_env = {
    HOST                          = "0.0.0.0"
    PROJECT_ID                    = var.project_id
    SCAN_PROJECTS                 = join(",", local.managed_projects)
    SCAN_ALL_PROJECTS             = "false"
    QUOTA_PROJECT_ID              = var.quota_project_id
    AUDIT_DATASET_ID              = local.dataset_id
    AUDIT_PROJECT_ID              = local.audit_project
    SCOPE_TYPE                    = var.scope_type
    SCOPE_IDS                     = join(",", var.scope_ids)
    BILLING_ACCOUNT_ID            = var.billing_account_id
    WORKSPACE_TIMEZONE            = var.workspace_timezone
    SETTINGS_BUCKET               = google_storage_bucket.state.name
    PUBLIC_BASE_URL               = local.public_base_url
    OIDC_ALLOWED_SERVICE_ACCOUNTS = join(",", var.oidc_allowed_service_accounts)
    HUB_REGION                    = var.region
    SERVICE_NAME                  = var.service_name
    RUNTIME_SERVICE_ACCOUNT       = google_service_account.api_sa.email
    ENFORCER_SERVICE_ACCOUNT      = google_service_account.enforcer_sa.email
    ORG_ID                        = var.organization_id
    VPC_NETWORK                   = var.vpc_network
    VPC_SUBNET                    = var.vpc_subnet
    VPC_EGRESS                    = var.vpc_network != "" ? var.vpc_egress : ""
    ENFORCEMENT_BACKEND           = var.enforcement_backend
    ENFORCEMENT_MODE              = var.enforcement_mode
    DENY_ATTACHMENT               = var.deny_attachment
    DENY_POLICY_ID                = var.deny_policy_id
    ENFORCEMENT_JOB               = local.enforcer_job_ref
    USAGE_EVENTS_TOPIC            = var.realtime_enforcement ? local.usage_topic_name : ""
    USAGE_EVENTS_SUBSCRIPTION     = var.realtime_enforcement ? local.usage_sub_ref : ""
    SPEND_CAPS_ENABLED            = var.spend_caps_enabled ? "true" : "false"
    CAP_CURRENCY                  = var.cap_currency
    QUOTA_CONTACT_EMAIL           = var.quota_contact_email
    MODEL_VIOLATION_ACTION        = var.model_violation_action
    CAP_SCOPE                     = var.cap_scope
    VERTEX_USER_HEADER_LABEL      = var.vertex_user_header_label
    METERED_SURFACES              = join(",", var.metered_surfaces)
    MODEL_VIOLATION_BLOCK_SCOPE   = var.model_violation_block_scope
    VERTEX_LOGGING_ENDPOINT       = var.vertex_logging_endpoint
  }
}

# 1. Enable required Google Cloud APIs (hub + managed projects)
resource "google_project_service" "apis" {
  for_each           = toset(local.required_apis)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_project_service" "managed_apis" {
  for_each           = local.managed_api_pairs
  project            = each.value.project
  service            = each.value.service
  disable_on_destroy = false
}

# 2. Dedicated Least-Privilege Service Accounts (API vs Enforcer Job separation)
resource "google_service_account" "api_sa" {
  account_id   = var.api_service_account_id
  display_name = "GCP AI Usage Caps API runtime"
  description  = "Read/query runtime identity for headless GCP AI Usage & Spend Caps API"
  project      = var.project_id
}

resource "google_service_account" "enforcer_sa" {
  account_id   = var.enforcer_service_account_id
  display_name = "GCP AI Usage Caps Enforcer Job"
  description  = "Dedicated Cloud Run Job identity holding IAM Deny Admin for spend cap enforcement"
  project      = var.project_id
}

resource "google_project_iam_member" "api_roles" {
  for_each = toset(local.api_roles)
  project  = var.project_id
  role     = each.value
  member   = local.api_sa_member
}

resource "google_project_iam_member" "enforcer_roles" {
  for_each = toset(local.enforcer_roles)
  project  = var.project_id
  role     = each.value
  member   = local.enforcer_sa_member
}

# Central dataset hosted outside the hub: read access for both identities.
resource "google_project_iam_member" "audit_project_readers" {
  for_each = local.central && local.audit_project != var.project_id ? {
    api      = local.api_sa_member
    enforcer = local.enforcer_sa_member
  } : {}
  project = local.audit_project
  role    = "roles/bigquery.dataViewer"
  member  = each.value
}

# Managed projects: least-privilege roles on EVERY managed project ...
resource "google_project_iam_member" "api_managed_roles" {
  for_each = local.api_managed_pairs
  project  = each.value.project
  role     = each.value.role
  member   = local.api_sa_member
}

resource "google_project_iam_member" "enforcer_managed_roles" {
  for_each = local.enforcer_managed_pairs
  project  = each.value.project
  role     = each.value.role
  member   = local.enforcer_sa_member
}

# ... or once on the governed folders / organization (var.grant_scope_roles).
resource "google_folder_iam_member" "scope_roles" {
  for_each = var.scope_type == "folder" ? local.scope_role_pairs : {}
  folder   = "folders/${each.value.id}"
  role     = each.value.role
  member   = each.value.member
}

resource "google_organization_iam_member" "scope_roles" {
  for_each = var.scope_type == "organization" ? local.scope_role_pairs : {}
  org_id   = each.value.id
  role     = each.value.role
  member   = each.value.member
}

# Organization-only roles (Deny Admin ONLY for the Enforcer Job; Deny Reviewer + Org Policy Admin for the API)
resource "google_organization_iam_member" "org_roles" {
  for_each = var.grant_org_roles && var.organization_id != "" ? local.org_role_grants : {}
  org_id   = var.organization_id
  role     = split("|", each.key)[1]
  member   = each.value
}

# 3. Private Versioned GCS State Bucket
resource "google_storage_bucket" "state" {
  name                        = local.settings_bucket
  project                     = var.project_id
  location                    = local.dataset_location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      num_newer_versions = 50
      with_state         = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_storage_bucket_iam_member" "api_state_rw" {
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectAdmin"
  member = local.api_sa_member
}

resource "google_storage_bucket_iam_member" "enforcer_state_rw" {
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectAdmin"
  member = local.enforcer_sa_member
}

# 4. Audit dataset(s) + AI Log Router sinks from every project
data "google_bigquery_dataset" "reused" {
  count      = local.reuse ? 1 : 0
  project    = local.audit_project
  dataset_id = local.dataset_id
}

resource "google_bigquery_dataset" "audit" {
  for_each                   = toset(local.dataset_projects)
  dataset_id                 = local.dataset_id
  project                    = each.value
  location                   = local.dataset_location
  description                = "AI telemetry logs for GCP AI Usage & Spend Caps API"
  delete_contents_on_destroy = false
  depends_on                 = [google_project_service.apis, google_project_service.managed_apis]
}

resource "google_logging_project_sink" "ai_logs_bq" {
  for_each               = var.create_audit_sink ? toset(local.sink_projects) : toset([])
  name                   = "${var.service_name}-ai-logs"
  project                = each.value
  destination            = "bigquery.googleapis.com/projects/${local.sink_destination[each.value]}/datasets/${local.dataset_id}"
  filter                 = local.ai_log_filter
  unique_writer_identity = true
  bigquery_options {
    use_partitioned_tables = true
  }
  depends_on = [google_bigquery_dataset.audit, data.google_bigquery_dataset.reused]
}

resource "google_bigquery_dataset_iam_member" "sink_writer" {
  for_each   = google_logging_project_sink.ai_logs_bq
  project    = local.sink_destination[each.key]
  dataset_id = local.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = each.value.writer_identity
}

# Optional: Vertex AI Data Access audit logs in every project (per-user Vertex AI usage). This resource is
# authoritative for the aiplatform.googleapis.com audit config only; other services' configs are untouched.
resource "google_project_iam_audit_config" "vertex_data_access" {
  for_each = var.enable_data_access_logs ? toset(local.sink_projects) : toset([])
  project  = each.value
  service  = "aiplatform.googleapis.com"
  audit_log_config {
    log_type = "ADMIN_READ"
  }
  audit_log_config {
    log_type = "DATA_READ"
  }
  audit_log_config {
    log_type = "DATA_WRITE"
  }
}

# Vertex AI inference request metadata logging (aiplatform.googleapis.com/inference_request): the only log-based
# source of per-request Vertex AI token counts. There is NO native Terraform resource for the project-level
# PublisherModelConfig (hashicorp/terraform-provider-google#30642), so each project is enabled with the v1beta1
# setPublisherModelConfig REST method through a local-exec (bash + curl on the machine running terraform; the access
# token of the provider credentials is passed via the environment and never written to state). An identical write
# returns 409 ALREADY_EXISTS (= already enabled). The setting has no DELETE: destroying this resource leaves logging on.
data "google_client_config" "current" {}

resource "terraform_data" "vertex_inference_logging" {
  for_each = var.enable_vertex_inference_logging ? toset(local.sink_projects) : toset([])
  input    = each.value

  triggers_replace = {
    project  = each.value
    endpoint = var.vertex_logging_endpoint
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    environment = {
      ACCESS_TOKEN = data.google_client_config.current.access_token
      SET_URL      = "${var.vertex_logging_endpoint}/projects/${self.input}:setPublisherModelConfig"
      PROJECT      = self.input
    }
    command = <<-EOT
      set -uo pipefail
      out="$(mktemp)"
      code="$(curl -sS -o "$out" -w '%%{http_code}' --max-time 60 -X POST \
        -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" "$SET_URL" \
        -d '{"publisherModelConfig":{"inferenceEventLoggingConfig":{"state":"ENABLED"}}}' || echo 000)"
      case "$code" in
        2??) echo "$PROJECT: Vertex AI inference request metadata logging ENABLED" ;;
        409) echo "$PROJECT: Vertex AI inference request metadata logging already ENABLED (409)" ;;
        *)
          if grep -Eq 'SERVICE_DISABLED|has not been used|it is disabled' "$out"; then
            echo "warning: $PROJECT: aiplatform.googleapis.com is disabled (Vertex AI unused); logging not enabled" >&2
          else
            echo "error: $PROJECT: setPublisherModelConfig failed (HTTP $code): $(head -c 400 "$out")" >&2
            rm -f "$out"
            exit 1
          fi ;;
      esac
      rm -f "$out"
    EOT
  }

  depends_on = [google_project_service.apis]
}

# One-permission custom role so the runtime can read the logging state (readiness + GET /v1/projects/{p}/vertex-logging).
resource "google_project_iam_custom_role" "vertex_logging_reader" {
  for_each    = local.vertex_reader_projects
  project     = each.value
  role_id     = local.vertex_logging_reader_role_id
  title       = "GCP AI Caps Vertex logging reader"
  description = "Read Vertex AI inference request logging state (GCP AI Usage Caps API)"
  permissions = ["aiplatform.endpoints.fetchPublisherModelConfig"]
  stage       = "GA"
}

resource "google_project_iam_member" "api_vertex_logging_reader" {
  for_each = google_project_iam_custom_role.vertex_logging_reader
  project  = each.value.project
  role     = each.value.id
  member   = local.api_sa_member
}

# 5. Headless Cloud Run v2 API Service
resource "google_cloud_run_v2_service" "api" {
  name                 = var.service_name
  location             = var.region
  project              = var.project_id
  ingress              = var.ingress
  invoker_iam_disabled = var.no_invoker_iam_check

  dynamic "binary_authorization" {
    for_each = var.binary_authorization_policy != "" ? [1] : []
    content {
      policy = var.binary_authorization_policy
    }
  }

  template {
    service_account = google_service_account.api_sa.email

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    dynamic "vpc_access" {
      for_each = var.vpc_network != "" ? [1] : []
      content {
        egress = var.vpc_egress == "private-ranges-only" ? "PRIVATE_RANGES_ONLY" : "ALL_TRAFFIC"
        network_interfaces {
          network    = var.vpc_network
          subnetwork = var.vpc_subnet
        }
      }
    }

    containers {
      image = var.container_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
      }

      dynamic "env" {
        for_each = { for k, v in local.common_env : k => v if v != "" }
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = var.bootstrap_admin_key_secret != "" ? [1] : []
        content {
          name = "BOOTSTRAP_ADMIN_KEY"
          value_source {
            secret_key_ref {
              secret  = var.bootstrap_admin_key_secret
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_project_iam_member.api_roles,
    google_storage_bucket_iam_member.api_state_rw,
  ]
}

# Public edge (the application still enforces Bearer API key / OIDC). Skipped when domain restricted sharing forbids
# allUsers: set no_invoker_iam_check = true instead (scripts/preflight.sh reports which one applies).
resource "google_cloud_run_v2_service_iam_member" "public_edge" {
  count    = var.allow_unauthenticated_edge && !var.no_invoker_iam_check ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "runtime_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = local.api_sa_member
}

# 6. Dedicated Cloud Run v2 Enforcer Job (IAM Deny v2)
resource "google_cloud_run_v2_job" "enforcer" {
  name                = local.enforcer_job_name
  location            = var.region
  project             = var.project_id
  deletion_protection = false

  dynamic "binary_authorization" {
    for_each = var.binary_authorization_policy != "" ? [1] : []
    content {
      policy = var.binary_authorization_policy
    }
  }

  template {
    task_count = 1
    template {
      service_account = google_service_account.enforcer_sa.email
      max_retries     = 0
      timeout         = "600s"

      dynamic "vpc_access" {
        for_each = var.vpc_network != "" ? [1] : []
        content {
          egress = var.vpc_egress == "private-ranges-only" ? "PRIVATE_RANGES_ONLY" : "ALL_TRAFFIC"
          network_interfaces {
            network    = var.vpc_network
            subnetwork = var.vpc_subnet
          }
        }
      }

      containers {
        image   = var.container_image
        command = ["python3"]
        args    = ["-m", "usage_caps_api.enforce_job"]

        resources {
          limits = {
            cpu    = var.cpu
            memory = var.memory
          }
        }

        dynamic "env" {
          for_each = { for k, v in merge(local.common_env, { PYTHONPATH = "/app/src" }) : k => v if v != "" }
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_project_iam_member.enforcer_roles,
    google_storage_bucket_iam_member.enforcer_state_rw,
  ]
}

resource "google_cloud_run_v2_job_iam_member" "api_job_executor" {
  for_each = toset(["roles/run.jobsExecutor", "roles/run.viewer"])
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.enforcer.name
  role     = each.value
  member   = local.api_sa_member
}

# 7. Real-time Pub/Sub Usage Event Pipeline -> POST /v1/internal/usage-event
#    ONE topic in the hub; a Pub/Sub sink in EVERY project publishes into it.
resource "google_pubsub_topic" "usage_events" {
  count      = var.realtime_enforcement ? 1 : 0
  name       = local.usage_topic_name
  project    = var.project_id
  depends_on = [google_project_service.apis]
}

resource "google_logging_project_sink" "usage_events" {
  for_each               = var.realtime_enforcement ? toset(local.sink_projects) : toset([])
  name                   = local.usage_topic_name
  project                = each.value
  destination            = "pubsub.googleapis.com/projects/${var.project_id}/topics/${google_pubsub_topic.usage_events[0].name}"
  filter                 = local.realtime_filter
  unique_writer_identity = true
}

resource "google_pubsub_topic_iam_member" "usage_sink_pub" {
  for_each = google_logging_project_sink.usage_events
  project  = var.project_id
  topic    = google_pubsub_topic.usage_events[0].name
  role     = "roles/pubsub.publisher"
  member   = each.value.writer_identity
}

resource "google_service_account_iam_member" "pubsub_token_creator" {
  count              = var.realtime_enforcement ? 1 : 0
  service_account_id = google_service_account.api_sa.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
  depends_on         = [google_project_service.apis]
}

resource "google_pubsub_subscription" "usage_events_push" {
  count                      = var.realtime_enforcement ? 1 : 0
  name                       = local.usage_sub_id
  project                    = var.project_id
  topic                      = google_pubsub_topic.usage_events[0].name
  ack_deadline_seconds       = 60
  message_retention_duration = "600s"

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "60s"
  }

  push_config {
    push_endpoint = "${local.run_url}/v1/internal/usage-event"
    oidc_token {
      service_account_email = google_service_account.api_sa.email
      audience              = "${local.public_base_url}/v1/internal/usage-event"
    }
  }

  depends_on = [
    google_cloud_run_v2_service.api,
    google_service_account_iam_member.pubsub_token_creator,
    google_cloud_run_v2_service_iam_member.runtime_invoker,
  ]
}

# 8. Cloud Scheduler Safety Net (every 5 min) + Daily Pricing Sync
resource "google_cloud_scheduler_job" "enforcer_schedule" {
  count            = var.enforcement_schedule != "" ? 1 : 0
  name             = "${var.service_name}-enforce-schedule"
  project          = var.project_id
  region           = var.region
  description      = "Periodic safety net and period-reset spend cap enforcement"
  schedule         = var.enforcement_schedule
  time_zone        = var.workspace_timezone
  attempt_deadline = "180s"

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/${local.enforcer_job_ref}:run"
    body        = base64encode("{}")
    headers     = { "Content-Type" = "application/json" }
    oauth_token {
      service_account_email = google_service_account.api_sa.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [google_project_service.apis, google_cloud_run_v2_job_iam_member.api_job_executor]
}

resource "google_cloud_scheduler_job" "pricing_sync" {
  count            = var.pricing_sync_schedule != "" ? 1 : 0
  name             = "${var.service_name}-pricing-sync"
  project          = var.project_id
  region           = var.region
  description      = "Daily official Cloud Billing Catalog token price & FX sync"
  schedule         = var.pricing_sync_schedule
  time_zone        = var.workspace_timezone
  attempt_deadline = "180s"

  http_target {
    http_method = "POST"
    uri         = "${local.run_url}/v1/internal/pricing/sync"
    body        = base64encode("{}")
    headers     = { "Content-Type" = "application/json" }
    oidc_token {
      service_account_email = google_service_account.api_sa.email
      audience              = "${local.public_base_url}/v1/internal/pricing/sync"
    }
  }

  depends_on = [google_project_service.apis, google_cloud_run_v2_service.api]
}
