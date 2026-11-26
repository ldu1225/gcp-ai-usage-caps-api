# GCP AI Usage & Spend Caps API - Headless Cloud Run + BigQuery Sink + Pub/Sub + Cloud Scheduler
#
# Architecture:
# - Cloud Run v2 Service: Headless M2M REST API (API Key + Google OIDC auth, zero UI)
# - Cloud Run v2 Job (`<service>-enforcer`): Runs `python -m usage_caps_api.enforce_job` as its own
#   dedicated service account — the ONLY identity granted org-level `roles/iam.denyAdmin`.
# - GCS Bucket: Private, versioned JSON document store (policy, user_caps, api_keys, webhooks, pricing).
# - BigQuery Dataset + Log Router Sink: Captures Antigravity, Vertex AI Data Access, and Gemini Enterprise logs.
# - Pub/Sub Topic + Push Subscription: Routes Antigravity inference logs in real time to `/v1/internal/usage-event`.
# - Cloud Scheduler: Periodic 5-minute safety net & period-reset enforcement + daily pricing sync.

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
  api_sa_member     = "serviceAccount:${google_service_account.api_sa.email}"
  enforcer_sa_member = "serviceAccount:${google_service_account.enforcer_sa.email}"
  quota_project     = var.quota_project_id != "" ? var.quota_project_id : var.project_id
  billing_project   = var.billing_project_id != "" ? var.billing_project_id : var.project_id
  settings_bucket   = var.settings_bucket_name != "" ? var.settings_bucket_name : "${var.project_id}-${var.service_name}-state"
  dataset_location  = var.dataset_location != "" ? var.dataset_location : var.region
  run_url           = "https://${var.service_name}-${data.google_project.this.number}.${var.region}.run.app"
  public_base_url   = var.public_base_url != "" ? trimsuffix(var.public_base_url, "/") : local.run_url
  enforcer_job_name = "${var.service_name}-enforcer"
  enforcer_job_ref  = "projects/${var.project_id}/locations/${var.region}/jobs/${local.enforcer_job_name}"
  usage_topic_name  = "${var.service_name}-usage-events"
  usage_sub_id      = "${var.service_name}-usage-events-push"
  usage_sub_ref     = "projects/${var.project_id}/subscriptions/${local.usage_sub_id}"

  required_apis = concat([
    "bigquery.googleapis.com",
    "logging.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
    "storage.googleapis.com",
    "cloudbilling.googleapis.com",
    "serviceusage.googleapis.com",
    "cloudscheduler.googleapis.com",
    "pubsub.googleapis.com",
  ], var.spend_caps_enabled ? ["billingbudgets.googleapis.com"] : [])

  api_roles = [
    "roles/bigquery.dataViewer",
    "roles/bigquery.jobUser",
    "roles/logging.viewer",
    "roles/logging.privateLogViewer",
    "roles/logging.logWriter",
    "roles/serviceusage.serviceUsageConsumer",
  ]

  enforcer_roles = [
    "roles/bigquery.dataViewer",
    "roles/bigquery.jobUser",
    "roles/logging.viewer",
    "roles/logging.privateLogViewer",
    "roles/logging.logWriter",
    "roles/serviceusage.serviceUsageConsumer",
  ]

  ai_log_filter = join(" OR ", [
    "log_id(\"businessaicode.googleapis.com/inference_response\")",
    "(log_id(\"cloudaudit.googleapis.com/data_access\") AND protoPayload.serviceName=\"aiplatform.googleapis.com\")",
    "log_id(\"discoveryengine.googleapis.com/gemini_enterprise_user_activity\")",
    "log_id(\"discoveryengine.googleapis.com/gen_ai_user_message\")",
  ])

  common_env = {
    HOST                          = "0.0.0.0"
    PROJECT_ID                    = var.project_id
    SCAN_PROJECTS                 = join(",", var.linked_projects)
    QUOTA_PROJECT_ID              = var.quota_project_id
    AUDIT_DATASET_ID              = var.audit_dataset_id
    AUDIT_PROJECT_ID              = var.audit_project_id
    SCOPE_TYPE                    = var.scope_type
    SCOPE_IDS                     = join(",", var.scope_ids)
    BILLING_PROJECT_ID            = var.billing_project_id
    BILLING_DATASET_ID            = var.billing_dataset_id
    BILLING_TABLE_ID              = var.billing_table_id
    BILLING_ACCOUNT_ID            = var.billing_account_id
    WORKSPACE_TIMEZONE            = var.workspace_timezone
    SETTINGS_BUCKET               = google_storage_bucket.state.name
    PUBLIC_BASE_URL               = local.public_base_url
    OIDC_ALLOWED_SERVICE_ACCOUNTS = join(",", var.oidc_allowed_service_accounts)
    RUNTIME_SERVICE_ACCOUNT       = google_service_account.api_sa.email
    ENFORCEMENT_BACKEND           = var.enforcement_backend
    ENFORCEMENT_MODE              = var.enforcement_mode
    DENY_ATTACHMENT               = var.deny_attachment
    DENY_POLICY_ID                = var.deny_policy_id
    ENFORCEMENT_JOB               = local.enforcer_job_ref
    USAGE_EVENTS_SUBSCRIPTION     = var.realtime_enforcement ? local.usage_sub_ref : ""
    SPEND_CAPS_ENABLED            = var.spend_caps_enabled ? "true" : "false"
    CAP_CURRENCY                  = var.cap_currency
  }
}

# 1. Enable required Google Cloud APIs
resource "google_project_service" "apis" {
  for_each           = toset(local.required_apis)
  project            = var.project_id
  service            = each.value
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

# Optional Organization-level IAM Deny roles (Deny Reviewer for API, Deny Admin ONLY for Enforcer Job)
resource "google_organization_iam_member" "api_deny_reviewer" {
  count  = var.grant_org_roles && var.organization_id != "" ? 1 : 0
  org_id = var.organization_id
  role   = "roles/iam.denyReviewer"
  member = local.api_sa_member
}

resource "google_organization_iam_member" "enforcer_deny_admin" {
  count  = var.grant_org_roles && var.organization_id != "" ? 1 : 0
  org_id = var.organization_id
  role   = "roles/iam.denyAdmin"
  member = local.enforcer_sa_member
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

# 4. BigQuery Audit Dataset + AI Log Router Sink
resource "google_bigquery_dataset" "audit" {
  count                       = var.create_audit_sink ? 1 : 0
  dataset_id                  = var.audit_dataset_id
  project                     = var.project_id
  location                    = local.dataset_location
  description                 = "AI telemetry logs for GCP AI Usage & Spend Caps API"
  delete_contents_on_destroy  = false
  depends_on                  = [google_project_service.apis]
}

resource "google_logging_project_sink" "ai_logs_bq" {
  count                  = var.create_audit_sink ? 1 : 0
  name                   = "${var.service_name}-ai-logs"
  project                = var.project_id
  destination            = "bigquery.googleapis.com/projects/${var.project_id}/datasets/${google_bigquery_dataset.audit[0].dataset_id}"
  filter                 = local.ai_log_filter
  unique_writer_identity = true
  bigquery_options {
    use_partitioned_tables = true
  }
  depends_on = [google_bigquery_dataset.audit]
}

resource "google_bigquery_dataset_iam_member" "sink_writer" {
  count      = var.create_audit_sink ? 1 : 0
  project    = var.project_id
  dataset_id = google_bigquery_dataset.audit[0].dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_logging_project_sink.ai_logs_bq[0].writer_identity
}

# 5. Headless Cloud Run v2 API Service
resource "google_cloud_run_v2_service" "api" {
  name     = var.service_name
  location = var.region
  project  = var.project_id
  ingress  = var.ingress

  template {
    service_account = google_service_account.api_sa.email

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
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

# Allow external M2M API Key callers at Cloud Run edge if configured (application still enforces Bearer API key / OIDC)
resource "google_cloud_run_v2_service_iam_member" "public_edge" {
  count    = var.allow_unauthenticated_edge ? 1 : 0
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

  template {
    task_count = 1
    template {
      service_account = google_service_account.enforcer_sa.email
      max_retries     = 0
      timeout         = "600s"

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
resource "google_pubsub_topic" "usage_events" {
  count      = var.realtime_enforcement ? 1 : 0
  name       = local.usage_topic_name
  project    = var.project_id
  depends_on = [google_project_service.apis]
}

resource "google_logging_project_sink" "usage_events" {
  count                  = var.realtime_enforcement ? 1 : 0
  name                   = local.usage_topic_name
  project                = var.project_id
  destination            = "pubsub.googleapis.com/projects/${var.project_id}/topics/${google_pubsub_topic.usage_events[0].name}"
  filter                 = "log_id(\"businessaicode.googleapis.com/inference_response\")"
  unique_writer_identity = true
}

resource "google_pubsub_topic_iam_member" "usage_sink_pub" {
  count   = var.realtime_enforcement ? 1 : 0
  project = var.project_id
  topic   = google_pubsub_topic.usage_events[0].name
  role    = "roles/pubsub.publisher"
  member  = google_logging_project_sink.usage_events[0].writer_identity
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
