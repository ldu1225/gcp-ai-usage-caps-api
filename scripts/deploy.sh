#!/usr/bin/env bash
# Deploy headless Cloud Run API service, Enforcer Job, GCS state bucket, Pub/Sub real-time pipeline, and Cloud Scheduler.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_ID=""
LINK_PROJECTS=""
REGION="asia-northeast3"
DATASET="ge_analytics"
SERVICE_NAME="gcp-ai-usage-caps-api"
ORG_ID=""
GRANT_ORG_ROLES=0
BILLING_ACCOUNT=""
SPEND_CAPS="false"
MODE="manual"
REALTIME=1
DRY_RUN=0

usage() {
  cat <<'HELP'
Usage: scripts/deploy.sh --project PROJECT_ID [options]
  --project ID              Primary GCP project ID
  --link-projects ID1,ID2   Additional GCP projects monitored together
  --region REGION           Cloud Run region (default: asia-northeast3)
  --service-name NAME       Cloud Run service name (default: gcp-ai-usage-caps-api)
  --dataset NAME            BigQuery audit dataset (default: ge_analytics)
  --org-id ID               Numeric GCP organization ID
  --grant-org-roles         Grant roles/iam.denyAdmin & roles/iam.denyReviewer at org level
  --billing-account ID      Billing account ID for project hard spend caps
  --spend-caps              Enable Cloud Billing Budget API v1 project hard spend caps
  --mode manual|auto        Default enforcement mode (default: manual)
  --no-realtime             Skip Pub/Sub real-time push subscription
  --dry-run                 Print commands without making changes
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
    --org-id) ORG_ID="${2:-}"; shift 2 ;;
    --grant-org-roles) GRANT_ORG_ROLES=1; shift ;;
    --billing-account) BILLING_ACCOUNT="${2:-}"; shift 2 ;;
    --spend-caps) SPEND_CAPS="true"; shift ;;
    --mode) MODE="${2:-}"; shift 2 ;;
    --no-realtime) REALTIME=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage 0 ;;
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
if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
fi
[[ -n "$PROJECT_ID" && "$PROJECT_ID" != "(unset)" ]] || { echo "error: pass --project PROJECT_ID" >&2; exit 1; }

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)' 2>/dev/null || echo '000000000000')"
AR_REPO="ai-usage-caps"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE_NAME}:latest"
API_SA_ID="ai-usage-caps-api"
ENFORCER_SA_ID="ai-usage-caps-enforcer"
API_SA="${API_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
ENFORCER_SA="${ENFORCER_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
STATE_BUCKET="${PROJECT_ID}-${SERVICE_NAME}-state"
ENFORCER_JOB="${SERVICE_NAME}-enforcer"
ENFORCER_JOB_REF="projects/${PROJECT_ID}/locations/${REGION}/jobs/${ENFORCER_JOB}"
RUN_URL="https://${SERVICE_NAME}-${PROJECT_NUMBER}.${REGION}.run.app"
BOOT_SECRET="${SERVICE_NAME}-bootstrap-key"

echo "==> 1/7 Enabling Cloud Run, Artifact Registry, Cloud Build, Pub/Sub, Scheduler & Secret Manager APIs"
run gcloud services enable \
  run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
  storage.googleapis.com pubsub.googleapis.com cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  --project="$PROJECT_ID"

echo "==> 2/7 Creating least-privilege Service Accounts & GCS state bucket"
gcloud iam service-accounts describe "$API_SA" --project="$PROJECT_ID" >/dev/null 2>&1 || \
  run gcloud iam service-accounts create "$API_SA_ID" --display-name="GCP AI Usage Caps API runtime" --project="$PROJECT_ID"
gcloud iam service-accounts describe "$ENFORCER_SA" --project="$PROJECT_ID" >/dev/null 2>&1 || \
  run gcloud iam service-accounts create "$ENFORCER_SA_ID" --display-name="GCP AI Usage Caps Enforcer Job" --project="$PROJECT_ID"

for role in roles/bigquery.dataViewer roles/bigquery.jobUser roles/logging.viewer roles/logging.privateLogViewer roles/logging.logWriter roles/serviceusage.serviceUsageConsumer; do
  run gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${API_SA}" --role="$role" --condition=None >/dev/null
  run gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${ENFORCER_SA}" --role="$role" --condition=None >/dev/null
done

if ! gcloud storage buckets describe "gs://${STATE_BUCKET}" --project="$PROJECT_ID" >/dev/null 2>&1; then
  run gcloud storage buckets create "gs://${STATE_BUCKET}" \
    --project="$PROJECT_ID" --location="$REGION" --uniform-bucket-level-access --public-access-prevention
  run gcloud storage buckets update "gs://${STATE_BUCKET}" --versioning
fi
run gcloud storage buckets add-iam-policy-binding "gs://${STATE_BUCKET}" --member="serviceAccount:${API_SA}" --role="roles/storage.objectAdmin" >/dev/null
run gcloud storage buckets add-iam-policy-binding "gs://${STATE_BUCKET}" --member="serviceAccount:${ENFORCER_SA}" --role="roles/storage.objectAdmin" >/dev/null

echo "==> 3/7 Ensuring bootstrap Admin API Key in Secret Manager (${BOOT_SECRET})"
GENERATED_KEY=""
if ! gcloud secrets describe "$BOOT_SECRET" --project="$PROJECT_ID" >/dev/null 2>&1; then
  if [[ "$DRY_RUN" != "1" ]]; then
    GENERATED_KEY="gcpai_$(python3 -c 'import secrets; print(secrets.token_hex(6) + "_" + secrets.token_urlsafe(32))')"
    printf '%s' "$GENERATED_KEY" | gcloud secrets create "$BOOT_SECRET" --data-file=- --project="$PROJECT_ID" >/dev/null
  else
    echo "[dry-run] create secret $BOOT_SECRET"
  fi
fi
run gcloud secrets add-iam-policy-binding "$BOOT_SECRET" --project="$PROJECT_ID" \
  --member="serviceAccount:${API_SA}" --role="roles/secretmanager.secretAccessor" >/dev/null

echo "==> 4/7 Building headless container image (${IMAGE})"
gcloud artifacts repositories describe "$AR_REPO" --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1 || \
  run gcloud artifacts repositories create "$AR_REPO" --repository-format=docker --location="$REGION" --project="$PROJECT_ID"
run gcloud builds submit "$ROOT" --tag="$IMAGE" --project="$PROJECT_ID"

ENV_VARS="PROJECT_ID=${PROJECT_ID},AUDIT_DATASET_ID=${DATASET},SETTINGS_BUCKET=${STATE_BUCKET},PUBLIC_BASE_URL=${RUN_URL},RUNTIME_SERVICE_ACCOUNT=${API_SA},ENFORCEMENT_BACKEND=iam_deny,ENFORCEMENT_MODE=${MODE},ENFORCEMENT_JOB=${ENFORCER_JOB_REF},SPEND_CAPS_ENABLED=${SPEND_CAPS}"
[[ -n "$LINK_PROJECTS" ]] && ENV_VARS="${ENV_VARS},SCAN_PROJECTS=${LINK_PROJECTS}"
[[ -n "$BILLING_ACCOUNT" ]] && ENV_VARS="${ENV_VARS},BILLING_ACCOUNT_ID=${BILLING_ACCOUNT}"

echo "==> 5/7 Deploying Cloud Run API service & Enforcer Job"
run gcloud run deploy "$SERVICE_NAME" \
  --project="$PROJECT_ID" --region="$REGION" --image="$IMAGE" \
  --service-account="$API_SA" --allow-unauthenticated \
  --set-env-vars="$ENV_VARS" \
  --set-secrets="BOOTSTRAP_ADMIN_KEY=${BOOT_SECRET}:latest" \
  --cpu=1 --memory=512Mi --max-instances=5

if gcloud run jobs describe "$ENFORCER_JOB" --region="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  run gcloud run jobs update "$ENFORCER_JOB" \
    --project="$PROJECT_ID" --region="$REGION" --image="$IMAGE" \
    --service-account="$ENFORCER_SA" --command="python3" --args="-m,usage_caps_api.enforce_job" \
    --set-env-vars="${ENV_VARS},PYTHONPATH=/app/src" --max-retries=0 --task-timeout=600s
else
  run gcloud run jobs create "$ENFORCER_JOB" \
    --project="$PROJECT_ID" --region="$REGION" --image="$IMAGE" \
    --service-account="$ENFORCER_SA" --command="python3" --args="-m,usage_caps_api.enforce_job" \
    --set-env-vars="${ENV_VARS},PYTHONPATH=/app/src" --max-retries=0 --task-timeout=600s
fi

run gcloud run jobs add-iam-policy-binding "$ENFORCER_JOB" --region="$REGION" --project="$PROJECT_ID" \
  --member="serviceAccount:${API_SA}" --role="roles/run.jobsExecutor" >/dev/null
run gcloud run jobs add-iam-policy-binding "$ENFORCER_JOB" --region="$REGION" --project="$PROJECT_ID" \
  --member="serviceAccount:${API_SA}" --role="roles/run.viewer" >/dev/null

echo "==> 6/7 Configuring Cloud Scheduler safety net (every 5 min) & real-time Pub/Sub push"
SCHED_JOB="${SERVICE_NAME}-enforce-schedule"
if ! gcloud scheduler jobs describe "$SCHED_JOB" --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  run gcloud scheduler jobs create http "$SCHED_JOB" \
    --project="$PROJECT_ID" --location="$REGION" \
    --schedule="*/5 * * * *" --uri="https://run.googleapis.com/v2/${ENFORCER_JOB_REF}:run" \
    --http-method=POST --message-body="{}" --headers="Content-Type=application/json" \
    --oauth-service-account-email="$API_SA"
fi

if [[ "$REALTIME" == "1" ]]; then
  TOPIC="${SERVICE_NAME}-usage-events"
  SUB="${SERVICE_NAME}-usage-events-push"
  gcloud pubsub topics describe "$TOPIC" --project="$PROJECT_ID" >/dev/null 2>&1 || \
    run gcloud pubsub topics create "$TOPIC" --project="$PROJECT_ID"
  if ! gcloud logging sinks describe "$TOPIC" --project="$PROJECT_ID" >/dev/null 2>&1; then
    run gcloud logging sinks create "$TOPIC" "pubsub.googleapis.com/projects/${PROJECT_ID}/topics/${TOPIC}" \
      --project="$PROJECT_ID" --log-filter='log_id("businessaicode.googleapis.com/inference_response")'
  fi
  if [[ "$DRY_RUN" != "1" ]]; then
    PS_WRITER="$(gcloud logging sinks describe "$TOPIC" --project="$PROJECT_ID" --format='value(writerIdentity)')"
    gcloud pubsub topics add-iam-policy-binding "$TOPIC" --project="$PROJECT_ID" \
      --member="$PS_WRITER" --role="roles/pubsub.publisher" >/dev/null
  fi
  run gcloud iam service-accounts add-iam-policy-binding "$API_SA" --project="$PROJECT_ID" \
    --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com" \
    --role="roles/iam.serviceAccountTokenCreator" >/dev/null
  if ! gcloud pubsub subscriptions describe "$SUB" --project="$PROJECT_ID" >/dev/null 2>&1; then
    run gcloud pubsub subscriptions create "$SUB" --project="$PROJECT_ID" --topic="$TOPIC" \
      --push-endpoint="${RUN_URL}/v1/internal/usage-event" \
      --push-auth-service-account="$API_SA" \
      --push-auth-token-audience="${RUN_URL}/v1/internal/usage-event" \
      --ack-deadline=60 --min-retry-delay=10s --max-retry-delay=60s
  fi
fi

echo "==> 7/7 Organization IAM Deny v2 roles"
if [[ "$GRANT_ORG_ROLES" == "1" && -n "$ORG_ID" ]]; then
  run gcloud organizations add-iam-policy-binding "$ORG_ID" --member="serviceAccount:${ENFORCER_SA}" --role="roles/iam.denyAdmin" --condition=None >/dev/null
  run gcloud organizations add-iam-policy-binding "$ORG_ID" --member="serviceAccount:${API_SA}" --role="roles/iam.denyReviewer" --condition=None >/dev/null
else
  echo "    To enable IAM Deny v2 enforcement, have an Organization Admin run:"
  echo "      gcloud organizations add-iam-policy-binding ${ORG_ID:-ORGANIZATION_ID} --member=serviceAccount:${ENFORCER_SA} --role=roles/iam.denyAdmin --condition=None"
  echo "      gcloud organizations add-iam-policy-binding ${ORG_ID:-ORGANIZATION_ID} --member=serviceAccount:${API_SA} --role=roles/iam.denyReviewer --condition=None"
fi

echo ""
echo "=================================================================="
echo " Deployment Complete!"
echo " API URL:     ${RUN_URL}"
echo " OpenAPI 3.1: ${RUN_URL}/v1/openapi.yaml"
if [[ -n "$GENERATED_KEY" ]]; then
  echo " Bootstrap Admin API Key (saved in Secret Manager '${BOOT_SECRET}'):"
  echo "   ${GENERATED_KEY}"
fi
echo "=================================================================="
