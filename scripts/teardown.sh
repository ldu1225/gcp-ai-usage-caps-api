#!/usr/bin/env bash
# Tear down Cloud Run service, Enforcer Job, Scheduler jobs, and Pub/Sub resources (keeps BigQuery & GCS data).
set -euo pipefail

PROJECT_ID=""
REGION="asia-northeast3"
SERVICE_NAME="gcp-ai-usage-caps-api"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ID="${2:-}"; shift 2 ;;
    --region) REGION="${2:-}"; shift 2 ;;
    --service-name) SERVICE_NAME="${2:-}"; shift 2 ;;
    *) echo "Usage: scripts/teardown.sh --project PROJECT_ID [--region REGION] [--service-name NAME]" >&2; exit 2 ;;
  esac
done

[[ -n "$PROJECT_ID" ]] || { echo "error: --project PROJECT_ID is required" >&2; exit 1; }

echo "==> Removing Cloud Run service, Enforcer Job, Scheduler, and Pub/Sub for ${SERVICE_NAME} in ${PROJECT_ID}"
gcloud scheduler jobs delete "${SERVICE_NAME}-enforce-schedule" --location="$REGION" --project="$PROJECT_ID" --quiet 2>/dev/null || true
gcloud scheduler jobs delete "${SERVICE_NAME}-pricing-sync" --location="$REGION" --project="$PROJECT_ID" --quiet 2>/dev/null || true
gcloud pubsub subscriptions delete "${SERVICE_NAME}-usage-events-push" --project="$PROJECT_ID" --quiet 2>/dev/null || true
gcloud logging sinks delete "${SERVICE_NAME}-usage-events" --project="$PROJECT_ID" --quiet 2>/dev/null || true
gcloud pubsub topics delete "${SERVICE_NAME}-usage-events" --project="$PROJECT_ID" --quiet 2>/dev/null || true
gcloud run jobs delete "${SERVICE_NAME}-enforcer" --region="$REGION" --project="$PROJECT_ID" --quiet 2>/dev/null || true
gcloud run services delete "${SERVICE_NAME}" --region="$REGION" --project="$PROJECT_ID" --quiet 2>/dev/null || true

echo "==> Teardown complete (BigQuery audit dataset and GCS state bucket were preserved)."
