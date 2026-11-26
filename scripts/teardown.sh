#!/usr/bin/env bash
# Tear down the hub deployment and the per-project sinks it created in every managed project.
# Keeps data: BigQuery datasets and the GCS state bucket are preserved; IAM Deny policies are left to the API
# (DELETE /v1/caps/... or the Enforcer) so blocked users are never silently unblocked by a teardown.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
source "${ROOT_DIR}/scripts/lib/common.sh"

usage() {
  cat <<HELP
Usage: scripts/teardown.sh --project HUB [--managed-projects A,B] [--scope-type folder|organization --scope-ids ID
                           --aggregated-sink] [--region REGION] [--service-name NAME] [--keep-log-sinks] [--dry-run]

Removes: Scheduler jobs, Pub/Sub push subscription & topic, Cloud Run service & Enforcer Job, the real-time Pub/Sub
sinks and (unless --keep-log-sinks) the BigQuery AI log sinks in the hub and every managed project.
HELP
  exit "${1:-0}"
}

init_defaults
parse_common_args "$@"
finalize_args
load_project_arrays

del() { run "$@" --quiet 2>/dev/null || true; }

log "Removing hub resources of ${SERVICE_NAME} in ${HUB} (${REGION})"
del gcloud scheduler jobs delete "${SERVICE_NAME}-enforce-schedule" --location="$REGION" --project="$HUB"
del gcloud scheduler jobs delete "${SERVICE_NAME}-pricing-sync" --location="$REGION" --project="$HUB"
del gcloud pubsub subscriptions delete "$SUB" --project="$HUB"

log "Removing per-project sinks (${#ALL_ARR[@]} project(s))"
for pid in "${ALL_ARR[@]}"; do
  del gcloud logging sinks delete "$USAGE_SINK" --project="$pid"
  if [[ "$KEEP_LOG_SINKS" != "1" ]]; then
    del gcloud logging sinks delete "$AI_SINK" --project="$pid"
  fi
done
if [[ "$AGGREGATED_SINK" == "1" && "$SCOPE_TYPE" != "projects" && "$KEEP_LOG_SINKS" != "1" ]]; then
  for sid in ${SCOPE_ID_ARR[@]+"${SCOPE_ID_ARR[@]}"}; do
    if [[ "$SCOPE_TYPE" == "folder" ]]; then
      del gcloud logging sinks delete "$AI_SINK" --folder="$sid"
    else
      del gcloud logging sinks delete "$AI_SINK" --organization="$sid"
    fi
  done
fi

del gcloud pubsub topics delete "$TOPIC" --project="$HUB"
del gcloud run jobs delete "${SERVICE_NAME}-enforcer" --region="$REGION" --project="$HUB"
del gcloud run services delete "$SERVICE_NAME" --region="$REGION" --project="$HUB"

echo "==> Teardown complete (BigQuery datasets and the GCS state bucket were preserved)."
echo "    IAM grants of ${API_SA} / ${ENFORCER_SA} remain; remove them with 'gcloud projects remove-iam-policy-binding'"
echo "    (and 'gcloud organizations remove-iam-policy-binding' for org roles) if the service accounts are deleted."
echo "    Gemini Platform inference request metadata logging stays ENABLED (there is no DELETE; set state DISABLED with"
echo "    setPublisherModelConfig to stop it) and the custom role ${VERTEX_READER_ROLE_ID} remains in each project"
echo "    ('gcloud iam roles delete ${VERTEX_READER_ROLE_ID} --project=P')."
