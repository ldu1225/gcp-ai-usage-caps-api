#!/usr/bin/env bash
# Deploy the hub: Cloud Run API service, Enforcer Job, GCS state bucket, bootstrap key, ONE real-time Pub/Sub topic fed
# by a Log Router sink in EVERY managed project (Antigravity inference_response + Gemini Platform inference_request), Cloud
# Scheduler safety net + daily OIDC pricing sync, and least-privilege grants for the runtime (API) and enforcer service
# accounts on the hub AND every managed project (or at folder/org level), including the one-permission custom role
# gcpAiCapsVertexLoggingReader (aiplatform.endpoints.fetchPublisherModelConfig) for the runtime.
#
# Optional runtime settings are read from the environment (or flags) and passed only when set: ENFORCEMENT_BACKEND
# (default iam_deny), CAP_SCOPE, VERTEX_USER_HEADER_LABEL, METERED_SURFACES, MODEL_VIOLATION_BLOCK_SCOPE,
# MODEL_PUBLISHERS, LONG_CONTEXT_THRESHOLD_TOKENS, PROVISIONED_THROUGHPUT_COST; PRICING_SYNC_SCHEDULE /
# PRICING_SYNC_TIME_ZONE tune the pricing job.
#
# Adapts automatically to the readiness preflight (run first; nothing is created when it reports blockers):
#   domain restricted sharing      -> --no-invoker-iam-check instead of --allow-unauthenticated
#   run.managed.requireInvokerIam  -> keeps the invoker check (allUsers when allowed)
#   run.allowedIngress             -> --ingress=<allowed value>
#   run.allowedVPCEgress           -> --network/--subnet --vpc-egress=all-traffic (needs --vpc-network/--vpc-subnet)
#   Binary Authorization           -> --binary-authorization=<policy>
#   cloudbuild.allowedWorkerPools  -> --worker-pool or local Docker build
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
source "${ROOT_DIR}/scripts/lib/common.sh"

usage() {
  cat <<HELP
Usage: scripts/deploy.sh --project HUB [--managed-projects A,B] [options]

${COMMON_FLAGS_HELP}
HELP
  exit "${1:-0}"
}

init_defaults
parse_common_args "$@"
finalize_args
run_preflight
load_project_arrays
detect_org_id

PROJECT_NUMBER="${PREFLIGHT_PROJECT_NUMBER:-}"
if [[ -z "$PROJECT_NUMBER" ]]; then
  PROJECT_NUMBER="$(gcloud projects describe "$HUB" --format='value(projectNumber)' 2>/dev/null || echo '000000000000')"
fi
AR_REPO="ai-usage-caps"
IMAGE="${PREBUILT_IMAGE:-${REGION}-docker.pkg.dev/${HUB}/${AR_REPO}/${SERVICE_NAME}:latest}"
STATE_BUCKET="${HUB}-${SERVICE_NAME}-state"
ENFORCER_JOB="${SERVICE_NAME}-enforcer"
ENFORCER_JOB_REF="projects/${HUB}/locations/${REGION}/jobs/${ENFORCER_JOB}"
RUN_URL="https://${SERVICE_NAME}-${PROJECT_NUMBER}.${REGION}.run.app"
BOOT_SECRET="${SERVICE_NAME}-bootstrap-key"
SUB_REF="projects/${HUB}/subscriptions/${SUB}"
API_MEMBER="serviceAccount:${API_SA}"
ENFORCER_MEMBER="serviceAccount:${ENFORCER_SA}"

# Least-privilege role sets.
API_HUB_ROLES="roles/browser roles/bigquery.dataViewer roles/bigquery.jobUser roles/logging.viewer roles/logging.privateLogViewer roles/logging.logWriter roles/serviceusage.serviceUsageConsumer roles/monitoring.viewer roles/cloudquotas.admin roles/discoveryengine.editor"
ENFORCER_HUB_ROLES="roles/bigquery.dataViewer roles/bigquery.jobUser roles/logging.viewer roles/logging.privateLogViewer roles/logging.logWriter roles/serviceusage.serviceUsageConsumer"
API_MANAGED_ROLES="roles/browser roles/logging.viewer roles/logging.privateLogViewer roles/monitoring.viewer roles/cloudquotas.admin roles/discoveryengine.editor roles/serviceusage.serviceUsageConsumer"
ENFORCER_MANAGED_ROLES="roles/logging.viewer roles/logging.privateLogViewer"
if [[ "$DATASET_MODE" == "per_project" ]]; then
  API_MANAGED_ROLES="${API_MANAGED_ROLES} roles/bigquery.dataViewer"
  ENFORCER_MANAGED_ROLES="${ENFORCER_MANAGED_ROLES} roles/bigquery.dataViewer"
fi

log "1/9 Enabling hub APIs (${HUB})"
HUB_REQUIRED="run.googleapis.com artifactregistry.googleapis.com storage.googleapis.com secretmanager.googleapis.com iam.googleapis.com iamcredentials.googleapis.com cloudresourcemanager.googleapis.com serviceusage.googleapis.com logging.googleapis.com bigquery.googleapis.com orgpolicy.googleapis.com cloudquotas.googleapis.com monitoring.googleapis.com aiplatform.googleapis.com cloudidentity.googleapis.com"
HUB_OPTIONAL="cloudbuild.googleapis.com cloudscheduler.googleapis.com cloudbilling.googleapis.com accesscontextmanager.googleapis.com"
[[ "$REALTIME" == "1" ]] && HUB_OPTIONAL="${HUB_OPTIONAL} pubsub.googleapis.com"
[[ "$SPEND_CAPS" == "true" ]] && HUB_OPTIONAL="${HUB_OPTIONAL} billingbudgets.googleapis.com"
enable_apis "$HUB" "$HUB_REQUIRED" "$HUB_OPTIONAL"

log "2/9 Service accounts & hub roles"
probe gcloud iam service-accounts describe "$API_SA" --project="$HUB" || \
  run gcloud iam service-accounts create "$API_SA_ID" --display-name="GCP AI Usage Caps API runtime" --project="$HUB"
probe gcloud iam service-accounts describe "$ENFORCER_SA" --project="$HUB" || \
  run gcloud iam service-accounts create "$ENFORCER_SA_ID" --display-name="GCP AI Usage Caps Enforcer Job" --project="$HUB"
for role in $API_HUB_ROLES; do grant_project_role "$HUB" "$API_MEMBER" "$role"; done
for role in $ENFORCER_HUB_ROLES; do grant_project_role "$HUB" "$ENFORCER_MEMBER" "$role"; done
ensure_vertex_reader_role "$HUB" "$API_MEMBER"
if [[ "$DATASET_MODE" == "central" && "$AUDIT_PROJECT" != "$HUB" ]]; then
  grant_project_role "$AUDIT_PROJECT" "$API_MEMBER" "roles/bigquery.dataViewer"
  grant_project_role "$AUDIT_PROJECT" "$ENFORCER_MEMBER" "roles/bigquery.dataViewer"
fi

log "3/9 Managed project roles (${#MANAGED_ARR[@]} project(s)${SCOPE_IDS:+, scope ${SCOPE_TYPE} ${SCOPE_IDS}})"
if [[ "$SCOPE_TYPE" != "projects" && "$GRANT_ORG_ROLES" == "1" ]]; then
  VERTEX_READER_ORG_ROLE=""
  [[ -n "$ORG_ID" ]] && ensure_vertex_reader_org_role "$ORG_ID"
  for sid in ${SCOPE_ID_ARR[@]+"${SCOPE_ID_ARR[@]}"}; do
    for role in $API_MANAGED_ROLES ${VERTEX_READER_ORG_ROLE}; do
      if [[ "$SCOPE_TYPE" == "folder" ]]; then grant_folder_role "$sid" "$API_MEMBER" "$role"
      else grant_org_role "$sid" "$API_MEMBER" "$role"; fi
    done
    for role in $ENFORCER_MANAGED_ROLES; do
      if [[ "$SCOPE_TYPE" == "folder" ]]; then grant_folder_role "$sid" "$ENFORCER_MEMBER" "$role"
      else grant_org_role "$sid" "$ENFORCER_MEMBER" "$role"; fi
    done
  done
  if [[ -z "$VERTEX_READER_ORG_ROLE" ]]; then
    for pid in ${MANAGED_ARR[@]+"${MANAGED_ARR[@]}"}; do ensure_vertex_reader_role "$pid" "$API_MEMBER"; done
  fi
else
  for pid in ${MANAGED_ARR[@]+"${MANAGED_ARR[@]}"}; do
    for role in $API_MANAGED_ROLES; do grant_project_role "$pid" "$API_MEMBER" "$role"; done
    for role in $ENFORCER_MANAGED_ROLES; do grant_project_role "$pid" "$ENFORCER_MEMBER" "$role"; done
    ensure_vertex_reader_role "$pid" "$API_MEMBER"
  done
fi

log "4/9 Organization roles (IAM Deny v2 + model governance org policies)"
ORG_ROLE_PAIRS=("${ENFORCER_MEMBER}|roles/iam.denyAdmin" "${API_MEMBER}|roles/iam.denyReviewer" "${API_MEMBER}|roles/orgpolicy.policyAdmin")
if [[ "$GRANT_ORG_ROLES" == "1" && -n "$ORG_ID" ]]; then
  for pair in "${ORG_ROLE_PAIRS[@]}"; do grant_org_role "$ORG_ID" "${pair%%|*}" "${pair#*|}"; done
else
  info "Deny Admin / Deny Reviewer / Organization Policy Administrator can only be granted on an organization."
  info "Have an Organization Admin run (or re-run with --grant-org-roles):"
  for pair in "${ORG_ROLE_PAIRS[@]}"; do
    info "  gcloud organizations add-iam-policy-binding ${ORG_ID:-ORGANIZATION_ID} --member=${pair%%|*} --role=${pair#*|} --condition=None"
  done
fi

log "5/9 GCS state bucket & bootstrap Admin API key"
if ! probe gcloud storage buckets describe "gs://${STATE_BUCKET}" --project="$HUB"; then
  run gcloud storage buckets create "gs://${STATE_BUCKET}" --project="$HUB" --location="$REGION" \
    --uniform-bucket-level-access --public-access-prevention
  run gcloud storage buckets update "gs://${STATE_BUCKET}" --versioning
fi
run_quiet gcloud storage buckets add-iam-policy-binding "gs://${STATE_BUCKET}" --member="$API_MEMBER" --role="roles/storage.objectAdmin"
run_quiet gcloud storage buckets add-iam-policy-binding "gs://${STATE_BUCKET}" --member="$ENFORCER_MEMBER" --role="roles/storage.objectAdmin"
GENERATED_KEY=""
if ! probe gcloud secrets describe "$BOOT_SECRET" --project="$HUB"; then
  if [[ "$DRY_RUN" != "1" ]]; then
    GENERATED_KEY="gcpai_$(python3 -c 'import secrets; print(secrets.token_hex(6) + "_" + secrets.token_urlsafe(32))')"
    printf '%s' "$GENERATED_KEY" | gcloud secrets create "$BOOT_SECRET" --data-file=- --project="$HUB" >/dev/null
  else
    echo "[dry-run] create secret $BOOT_SECRET"
  fi
fi
run_quiet gcloud secrets add-iam-policy-binding "$BOOT_SECRET" --project="$HUB" \
  --member="$API_MEMBER" --role="roles/secretmanager.secretAccessor"

log "6/9 Container image (${IMAGE})"
if [[ -n "$PREBUILT_IMAGE" ]]; then
  info "using prebuilt image ${PREBUILT_IMAGE}"
else
  probe gcloud artifacts repositories describe "$AR_REPO" --location="$REGION" --project="$HUB" || \
    run gcloud artifacts repositories create "$AR_REPO" --repository-format=docker --location="$REGION" --project="$HUB"
  if [[ -n "$WORKER_POOL" ]]; then
    run gcloud builds submit "$ROOT_DIR" --tag="$IMAGE" --project="$HUB" --region="$REGION" --worker-pool="$WORKER_POOL"
  elif [[ "${PREFLIGHT_DOCKER_BUILD:-0}" == "1" ]]; then
    command -v docker >/dev/null || [[ "$DRY_RUN" == "1" ]] || die "Cloud Build default pool is not allowed and Docker is unavailable: pass --worker-pool or --image"
    run gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
    run docker build -t "$IMAGE" "$ROOT_DIR"
    run docker push "$IMAGE"
  else
    run gcloud builds submit "$ROOT_DIR" --tag="$IMAGE" --project="$HUB"
  fi
fi

# Env vars use a custom delimiter (^|^) because SCAN_PROJECTS / SCOPE_IDS contain commas.
ENV_PAIRS=(
  "PROJECT_ID=${HUB}" "HUB_REGION=${REGION}" "SERVICE_NAME=${SERVICE_NAME}"
  "AUDIT_DATASET_ID=${DATASET}" "SETTINGS_BUCKET=${STATE_BUCKET}" "PUBLIC_BASE_URL=${RUN_URL}"
  "RUNTIME_SERVICE_ACCOUNT=${API_SA}" "ENFORCER_SERVICE_ACCOUNT=${ENFORCER_SA}"
  "ENFORCEMENT_BACKEND=${ENFORCEMENT_BACKEND}" "ENFORCEMENT_MODE=${MODE}" "ENFORCEMENT_JOB=${ENFORCER_JOB_REF}"
  "DENY_ATTACHMENT=${DENY_ATTACHMENT}" "SPEND_CAPS_ENABLED=${SPEND_CAPS}"
  "MODEL_VIOLATION_ACTION=${MODEL_VIOLATION_ACTION}" "SCAN_ALL_PROJECTS=false"
)
MANAGED_CSV="$(IFS=,; echo "${MANAGED_ARR[*]+"${MANAGED_ARR[*]}"}")"
[[ -n "$MANAGED_CSV" ]] && ENV_PAIRS+=("SCAN_PROJECTS=${MANAGED_CSV}")
[[ "$SCOPE_TYPE" != "projects" ]] && ENV_PAIRS+=("SCOPE_TYPE=${SCOPE_TYPE}" "SCOPE_IDS=${SCOPE_IDS// /}")
[[ -n "$AUDIT_PROJECT" ]] && ENV_PAIRS+=("AUDIT_PROJECT_ID=${AUDIT_PROJECT}")
[[ -n "$ORG_ID" ]] && ENV_PAIRS+=("ORG_ID=${ORG_ID}")
[[ -n "$VPC_NETWORK" ]] && ENV_PAIRS+=("VPC_NETWORK=${VPC_NETWORK}" "VPC_SUBNET=${VPC_SUBNET}" "VPC_EGRESS=${VPC_EGRESS:-all-traffic}")
[[ "$REALTIME" == "1" ]] && ENV_PAIRS+=("USAGE_EVENTS_TOPIC=${TOPIC}" "USAGE_EVENTS_SUBSCRIPTION=${SUB_REF}")
[[ -n "$QUOTA_CONTACT_EMAIL" ]] && ENV_PAIRS+=("QUOTA_CONTACT_EMAIL=${QUOTA_CONTACT_EMAIL}")
[[ -n "$BILLING_ACCOUNT" ]] && ENV_PAIRS+=("BILLING_ACCOUNT_ID=${BILLING_ACCOUNT}")
# Optional v1.2 runtime settings (costs = log token counts x Cloud Billing Catalog SKU prices; no billing export).
for var in CAP_SCOPE VERTEX_USER_HEADER_LABEL METERED_SURFACES MODEL_VIOLATION_BLOCK_SCOPE MODEL_PUBLISHERS \
           LONG_CONTEXT_THRESHOLD_TOKENS PROVISIONED_THROUGHPUT_COST VERTEX_LOGGING_ENDPOINT \
           SERVICE_ACCOUNT_SUBJECTS; do
  if [[ -n "${!var}" ]]; then ENV_PAIRS+=("${var}=${!var// /}"); fi
done
ENV_VARS="^|^$(IFS='|'; echo "${ENV_PAIRS[*]}")"

# Edge access adapted to organization policies.
RUN_FLAGS=()
if [[ "${PREFLIGHT_NO_INVOKER_IAM_CHECK:-0}" == "1" ]]; then
  RUN_FLAGS+=(--no-invoker-iam-check)
elif [[ "${PREFLIGHT_ALLOW_UNAUTHENTICATED:-1}" == "0" ]]; then
  RUN_FLAGS+=(--no-allow-unauthenticated)
  info "invoker IAM is required and allUsers is not allowed: callers need Google ID tokens with roles/run.invoker"
else
  RUN_FLAGS+=(--allow-unauthenticated)
fi
[[ -n "${PREFLIGHT_INGRESS:-}" ]] && RUN_FLAGS+=("--ingress=${PREFLIGHT_INGRESS}")
[[ -n "${PREFLIGHT_BINAUTHZ:-}" ]] && RUN_FLAGS+=("--binary-authorization=${PREFLIGHT_BINAUTHZ}")
VPC_FLAGS=()
if [[ -n "$VPC_NETWORK" ]]; then
  VPC_FLAGS=("--network=${VPC_NETWORK}" "--subnet=${VPC_SUBNET}" "--vpc-egress=${VPC_EGRESS:-all-traffic}")
elif [[ "${PREFLIGHT_VPC_EGRESS_REQUIRED:-0}" == "1" ]]; then
  die "organization policy run.allowedVPCEgress requires --vpc-network and --vpc-subnet"
fi

log "7/9 Cloud Run API service & Enforcer Job"
run gcloud run deploy "$SERVICE_NAME" \
  --project="$HUB" --region="$REGION" --image="$IMAGE" \
  --service-account="$API_SA" "${RUN_FLAGS[@]}" ${VPC_FLAGS[@]+"${VPC_FLAGS[@]}"} \
  --set-env-vars="$ENV_VARS" \
  --set-secrets="BOOTSTRAP_ADMIN_KEY=${BOOT_SECRET}:latest" \
  --cpu=1 --memory=512Mi --max-instances=5
JOB_VERB="create"
probe gcloud run jobs describe "$ENFORCER_JOB" --region="$REGION" --project="$HUB" && JOB_VERB="update"
JOB_FLAGS=()
[[ -n "${PREFLIGHT_BINAUTHZ:-}" ]] && JOB_FLAGS+=("--binary-authorization=${PREFLIGHT_BINAUTHZ}")
run gcloud run jobs "$JOB_VERB" "$ENFORCER_JOB" \
  --project="$HUB" --region="$REGION" --image="$IMAGE" \
  --service-account="$ENFORCER_SA" --command="python3" --args="-m,usage_caps_api.enforce_job" \
  ${VPC_FLAGS[@]+"${VPC_FLAGS[@]}"} ${JOB_FLAGS[@]+"${JOB_FLAGS[@]}"} \
  --set-env-vars="${ENV_VARS}|PYTHONPATH=/app/src" --max-retries=0 --task-timeout=600s
run_quiet gcloud run jobs add-iam-policy-binding "$ENFORCER_JOB" --region="$REGION" --project="$HUB" \
  --member="$API_MEMBER" --role="roles/run.jobsExecutor"
run_quiet gcloud run jobs add-iam-policy-binding "$ENFORCER_JOB" --region="$REGION" --project="$HUB" \
  --member="$API_MEMBER" --role="roles/run.viewer"
run_quiet gcloud run services add-iam-policy-binding "$SERVICE_NAME" --region="$REGION" --project="$HUB" \
  --member="$API_MEMBER" --role="roles/run.invoker"

log "8/9 Cloud Scheduler: safety net (every 5 min) + daily official SKU pricing sync"
SCHED_JOB="${SERVICE_NAME}-enforce-schedule"
if ! probe gcloud scheduler jobs describe "$SCHED_JOB" --location="$REGION" --project="$HUB"; then
  run gcloud scheduler jobs create http "$SCHED_JOB" \
    --project="$HUB" --location="$REGION" \
    --schedule="*/5 * * * *" --uri="https://run.googleapis.com/v2/${ENFORCER_JOB_REF}:run" \
    --http-method=POST --message-body="{}" --headers="Content-Type=application/json" \
    --oauth-service-account-email="$API_SA" \
    || warn "Cloud Scheduler job not created (API disabled?); enforcement still runs in real time and on demand"
fi
# POST /v1/internal/pricing/sync with a Google OIDC token of the runtime SA (audience = PUBLIC_BASE_URL + path):
# refreshes Cloud Billing Catalog token prices and FX rates once a day.
PRICING_JOB="${SERVICE_NAME}-pricing-sync"
PRICING_URI="${RUN_URL}/v1/internal/pricing/sync"
PRICING_VERB="create"
PRICING_HEADERS=(--headers="Content-Type=application/json")
if probe gcloud scheduler jobs describe "$PRICING_JOB" --location="$REGION" --project="$HUB"; then
  PRICING_VERB="update"
  PRICING_HEADERS=(--update-headers="Content-Type=application/json")
fi
run gcloud scheduler jobs "$PRICING_VERB" http "$PRICING_JOB" \
  --project="$HUB" --location="$REGION" \
  --schedule="$PRICING_SYNC_SCHEDULE" --time-zone="$PRICING_SYNC_TIME_ZONE" --uri="$PRICING_URI" \
  --http-method=POST --message-body="{}" "${PRICING_HEADERS[@]}" \
  --oidc-service-account-email="$API_SA" --oidc-token-audience="$PRICING_URI" --attempt-deadline=180s \
  || warn "pricing sync scheduler not ${PRICING_VERB}d; run it on demand: POST ${PRICING_URI} with an admin API key"

log "9/9 Real-time pipeline: ONE hub topic, a Pub/Sub sink in every project"
if [[ "$REALTIME" == "1" ]]; then
  probe gcloud pubsub topics describe "$TOPIC" --project="$HUB" || run gcloud pubsub topics create "$TOPIC" --project="$HUB"
  for pid in "${ALL_ARR[@]}"; do
    ensure_pubsub_sink "$pid"
  done
  run_quiet gcloud iam service-accounts add-iam-policy-binding "$API_SA" --project="$HUB" \
    --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com" \
    --role="roles/iam.serviceAccountTokenCreator"
  if ! probe gcloud pubsub subscriptions describe "$SUB" --project="$HUB"; then
    run gcloud pubsub subscriptions create "$SUB" --project="$HUB" --topic="$TOPIC" \
      --push-endpoint="${RUN_URL}/v1/internal/usage-event" \
      --push-auth-service-account="$API_SA" \
      --push-auth-token-audience="${RUN_URL}/v1/internal/usage-event" \
      --ack-deadline=60 --min-retry-delay=10s --max-retry-delay=60s
  fi
else
  info "skipped (--no-realtime): the 5-minute scheduler enforces caps"
fi

echo ""
echo "=================================================================="
echo " Deployment Complete!"
echo " Hub:         ${HUB} (${REGION})   Managed projects: ${#MANAGED_ARR[@]}"
echo " API URL:     ${RUN_URL}"
echo " OpenAPI 3.1: ${RUN_URL}/v1/openapi.yaml"
echo " Readiness:   GET ${RUN_URL}/v1/admin/readiness"
if [[ -n "$GENERATED_KEY" ]]; then
  echo " Bootstrap Admin API Key (saved in Secret Manager '${BOOT_SECRET}'):"
  echo "   ${GENERATED_KEY}"
fi
echo "=================================================================="
