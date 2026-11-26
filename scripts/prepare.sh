#!/usr/bin/env bash
# Prepare GCP project(s): enable APIs, create BigQuery audit dataset, and configure AI Log Router Sink.
set -euo pipefail

PROJECT_ID=""
LINK_PROJECTS=""
REGION="asia-northeast3"
DATASET="ge_analytics"
SERVICE_NAME="gcp-ai-usage-caps-api"
DRY_RUN=0

usage() {
  cat <<'HELP'
Usage: scripts/prepare.sh --project PROJECT_ID [--link-projects A,B] [--region REGION] [--dataset NAME] [--dry-run]
HELP
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ID="${2:-}"; shift 2 ;;
    --link-projects) LINK_PROJECTS="${2:-}"; shift 2 ;;
    --region) REGION="${2:-}"; shift 2 ;;
    --dataset) DATASET="${2:-}"; shift 2 ;;
    --service-name) SERVICE_NAME="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage 0 ;;
    # Ignore deploy-only flags when forwarded from install.sh
    --org-id|--billing-account|--mode) shift 2 ;;
    --grant-org-roles|--spend-caps|--no-realtime) shift ;;
    *) echo "Unknown option: $1" >&2; usage 2 ;;
  esac
done

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

command -v gcloud >/dev/null || { echo "error: gcloud CLI is required" >&2; exit 1; }
command -v bq >/dev/null || { echo "error: bq CLI is required" >&2; exit 1; }

if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
fi
[[ -n "$PROJECT_ID" && "$PROJECT_ID" != "(unset)" ]] || { echo "error: pass --project PROJECT_ID" >&2; exit 1; }

prepare_one() {
  local pid="$1"
  echo "==> Preparing project: ${pid} (dataset=${DATASET}, region=${REGION})"
  run gcloud services enable \
    bigquery.googleapis.com \
    logging.googleapis.com \
    cloudresourcemanager.googleapis.com \
    iam.googleapis.com \
    cloudbilling.googleapis.com \
    serviceusage.googleapis.com \
    --project="$pid"

  if ! bq --project_id="$pid" show --dataset "${pid}:${DATASET}" >/dev/null 2>&1; then
    run bq --project_id="$pid" mk --dataset --location="$REGION" \
      --description="AI telemetry logs for GCP AI Usage & Spend Caps API" \
      "${pid}:${DATASET}"
  fi

  local sink_name="${SERVICE_NAME}-ai-logs"
  local log_filter='log_id("businessaicode.googleapis.com/inference_response") OR (log_id("cloudaudit.googleapis.com/data_access") AND protoPayload.serviceName="aiplatform.googleapis.com") OR log_id("discoveryengine.googleapis.com/gemini_enterprise_user_activity") OR log_id("discoveryengine.googleapis.com/gen_ai_user_message")'
  local dest="bigquery.googleapis.com/projects/${pid}/datasets/${DATASET}"

  if gcloud logging sinks describe "$sink_name" --project="$pid" >/dev/null 2>&1; then
    run gcloud logging sinks update "$sink_name" "$dest" \
      --project="$pid" --log-filter="$log_filter" --use-partitioned-tables
  else
    run gcloud logging sinks create "$sink_name" "$dest" \
      --project="$pid" --log-filter="$log_filter" --use-partitioned-tables
  fi

  if [[ "$DRY_RUN" != "1" ]]; then
    local writer
    writer="$(gcloud logging sinks describe "$sink_name" --project="$pid" --format='value(writerIdentity)')"
    if [[ -n "$writer" ]]; then
      bq show --format=prettyjson "${pid}:${DATASET}" >/dev/null 2>&1 || true
      gcloud projects add-iam-policy-binding "$pid" \
        --member="$writer" --role="roles/bigquery.dataEditor" --condition=None >/dev/null
    fi
  fi
}

prepare_one "$PROJECT_ID"
if [[ -n "$LINK_PROJECTS" ]]; then
  IFS=',' read -r -a EXTRA_LIST <<<"$LINK_PROJECTS"
  for extra in "${EXTRA_LIST[@]}"; do
    extra="$(echo "$extra" | tr -d '[:space:]')"
    [[ -n "$extra" && "$extra" != "$PROJECT_ID" ]] && prepare_one "$extra"
  done
fi

echo "==> Preparation complete."
