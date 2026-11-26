#!/usr/bin/env bash
# Prepare the hub and every managed project for log collection (idempotent, adoptive of existing resources):
#   0. readiness preflight (stops before creating anything when blocked)
#   1. enable APIs per project (per-API fallback)
#   2. ONE central audit dataset in the hub (default), an adopted dataset (--reuse-dataset), or legacy per-project
#      datasets (--per-project-datasets)
#   3. a Log Router sink in EVERY project (or one folder/org aggregated sink) routing AI logs to that dataset; each
#      writer identity gets BigQuery Data Editor on the destination dataset only
#   4. optional Vertex AI Data Access audit logs (--enable-data-access-logs), merged without clobbering auditConfigs
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
source "${ROOT_DIR}/scripts/lib/common.sh"

usage() {
  cat <<HELP
Usage: scripts/prepare.sh --project HUB [--managed-projects A,B] [options]

${COMMON_FLAGS_HELP}
HELP
  exit "${1:-0}"
}

init_defaults
parse_common_args "$@"
finalize_args
command -v bq >/dev/null || die "bq CLI is required"

run_preflight
load_project_arrays

HUB_PREP_REQUIRED="serviceusage.googleapis.com cloudresourcemanager.googleapis.com iam.googleapis.com logging.googleapis.com bigquery.googleapis.com orgpolicy.googleapis.com cloudquotas.googleapis.com monitoring.googleapis.com aiplatform.googleapis.com"
HUB_PREP_OPTIONAL="accesscontextmanager.googleapis.com cloudbilling.googleapis.com"
MANAGED_REQUIRED="serviceusage.googleapis.com cloudresourcemanager.googleapis.com iam.googleapis.com logging.googleapis.com orgpolicy.googleapis.com cloudquotas.googleapis.com"
MANAGED_OPTIONAL="monitoring.googleapis.com"
[[ "$DATASET_MODE" == "per_project" ]] && MANAGED_REQUIRED="${MANAGED_REQUIRED} bigquery.googleapis.com"

log "1. Enabling APIs (hub ${HUB} + managed projects)"
enable_apis "$HUB" "$HUB_PREP_REQUIRED" "$HUB_PREP_OPTIONAL"
for pid in ${MANAGED_ARR[@]+"${MANAGED_ARR[@]}"}; do
  enable_apis "$pid" "$MANAGED_REQUIRED" "$MANAGED_OPTIONAL"
done

log "2. Audit dataset (${DATASET_MODE})"
if [[ "$DATASET_MODE" == "central" ]]; then
  if [[ -n "$REUSE_DATASET" ]]; then
    info "adopting existing dataset ${AUDIT_PROJECT}:${DATASET} (--reuse-dataset); nothing is created"
    if [[ "$DRY_RUN" != "1" ]] && ! probe bq --project_id="$AUDIT_PROJECT" show --dataset "${AUDIT_PROJECT}:${DATASET}"; then
      die "--reuse-dataset ${AUDIT_PROJECT}:${DATASET} cannot be read"
    fi
  else
    ensure_dataset "$AUDIT_PROJECT" "$DATASET" "$DATASET_LOCATION"
  fi
else
  for pid in "${ALL_ARR[@]}"; do
    ensure_dataset "$pid" "$DATASET" "$DATASET_LOCATION"
  done
fi

log "3. Log Router sinks -> BigQuery"
if [[ "$AGGREGATED_SINK" == "1" && "$SCOPE_TYPE" != "projects" && "$DATASET_MODE" == "central" ]]; then
  for sid in ${SCOPE_ID_ARR[@]+"${SCOPE_ID_ARR[@]}"}; do
    ensure_aggregated_bq_sink "$SCOPE_TYPE" "$sid" "$AUDIT_PROJECT" "$DATASET"
  done
  # The hub may sit outside the folder/org scope: give it its own sink.
  ensure_bq_sink "$HUB" "$AUDIT_PROJECT" "$DATASET"
else
  for pid in "${ALL_ARR[@]}"; do
    if [[ "$DATASET_MODE" == "central" ]]; then
      ensure_bq_sink "$pid" "$AUDIT_PROJECT" "$DATASET"
    else
      ensure_bq_sink "$pid" "$pid" "$DATASET"
    fi
  done
fi

log "4. Vertex AI Data Access audit logs"
if [[ "$ENABLE_DATA_ACCESS_LOGS" == "1" ]]; then
  for pid in "${ALL_ARR[@]}"; do
    enable_data_access_logs "$pid"
  done
else
  info "unchanged (pass --enable-data-access-logs to merge DATA_READ/DATA_WRITE for aiplatform.googleapis.com)"
fi

echo ""
echo "==> Preparation complete."
if [[ "$DATASET_MODE" == "central" ]]; then
  echo "    Central dataset: ${AUDIT_PROJECT}:${DATASET}  (deploy with AUDIT_PROJECT_ID=${AUDIT_PROJECT})"
else
  echo "    Per-project datasets: <project>:${DATASET}"
fi
