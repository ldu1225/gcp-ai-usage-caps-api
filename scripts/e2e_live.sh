#!/usr/bin/env bash
# Live end-to-end test helper for one real Google Cloud project (default: duleetest).
#
#   scripts/e2e_live.sh setup     # APIs, test service account, IAM grants (idempotent)
#   scripts/e2e_live.sh server    # run the API locally with your ADC credentials (foreground)
#   scripts/e2e_live.sh run       # run scripts/e2e_live_test.py against the API (local or Cloud Run)
#   scripts/e2e_live.sh all       # setup + local server in the background + run + stop the server
#   scripts/e2e_live.sh cleanup   # remove the managed deny policy entries for the test SA and delete the SA
#
# Environment (all optional):
#   PROJECT_ID=duleetest  E2E_LOCATION=global  E2E_MODEL=<model-id>  PORT=8080
#   CAPS_API_URL=https://...run.app (use a deployed API instead of the local server)
#   CAPS_ADMIN_KEY=gcpai_...        (admin API key; generated for the local server when unset)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_ID="${PROJECT_ID:-duleetest}"
PORT="${PORT:-8080}"
SA_NAME="${E2E_SA_NAME:-caps-e2e-tester}"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
STATE_DIR="${E2E_STATE_DIR:-$HOME/.caps-e2e}"
mkdir -p "$STATE_DIR"
KEY_FILE="$STATE_DIR/admin-key"
if [[ -z "${CAPS_ADMIN_KEY:-}" ]]; then
  [[ -s "$KEY_FILE" ]] || python3 -c "import secrets;print('gcpai_e2e_'+secrets.token_hex(24))" > "$KEY_FILE"
  CAPS_ADMIN_KEY="$(cat "$KEY_FILE")"
fi
export CAPS_ADMIN_KEY
CAPS_API_URL="${CAPS_API_URL:-http://127.0.0.1:${PORT}}"

log() { printf '==> %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }

operator() { gcloud config get-value account 2>/dev/null; }

cmd_setup() {
  command -v gcloud >/dev/null || { echo "gcloud CLI is required" >&2; exit 1; }
  local who; who="$(operator)"
  log "Project ${PROJECT_ID}, operator ${who}"
  log "Enabling APIs"
  gcloud services enable aiplatform.googleapis.com iam.googleapis.com iamcredentials.googleapis.com \
    logging.googleapis.com cloudresourcemanager.googleapis.com serviceusage.googleapis.com \
    --project "$PROJECT_ID" --quiet
  if ! gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1; then
    log "Creating test service account ${SA_EMAIL}"
    gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT_ID" \
      --display-name "Caps API live E2E test identity" --quiet
  fi
  log "Granting roles/aiplatform.user to the test identity (so it can call Gemini Platform models)"
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:${SA_EMAIL}" \
    --role roles/aiplatform.user --condition=None --quiet >/dev/null
  log "Allowing ${who} to impersonate the test identity (roles/iam.serviceAccountTokenCreator)"
  gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" --project "$PROJECT_ID" \
    --member "user:${who}" --role roles/iam.serviceAccountTokenCreator --quiet >/dev/null
  log "Granting the operator the roles the local API needs on ${PROJECT_ID}"
  for role in roles/iam.denyAdmin roles/logging.viewer roles/logging.privateLogViewer roles/aiplatform.admin roles/serviceusage.serviceUsageConsumer \
              roles/iam.securityReviewer; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "user:${who}" --role "$role" \
      --condition=None --quiet >/dev/null || warn "could not grant ${role} (ask a project IAM admin)"
  done
  if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
    warn "No Application Default Credentials. Run: gcloud auth application-default login"
  fi
  log "Setup complete. IAM changes can take 1-2 minutes to propagate."
}

cmd_server() {
  cd "$ROOT"
  if [[ ! -d "$STATE_DIR/venv" ]]; then
    log "Creating virtualenv with runtime requirements"
    python3 -m venv "$STATE_DIR/venv"
    "$STATE_DIR/venv/bin/pip" install -q -r requirements.txt
  fi
  gcloud auth application-default set-quota-project "$PROJECT_ID" >/dev/null 2>&1 || true
  log "Starting API on ${CAPS_API_URL} for ${PROJECT_ID} (Ctrl+C to stop)"
  PROJECT_ID="$PROJECT_ID" QUOTA_PROJECT_ID="$PROJECT_ID" USAGE_SOURCE=logging STORAGE_BACKEND=file \
  STORAGE_FILE_PATH="$STATE_DIR/state.json" ALLOW_LOCAL_INSECURE=false BOOTSTRAP_ADMIN_KEY="$CAPS_ADMIN_KEY" \
  SERVICE_ACCOUNT_SUBJECTS=true ENFORCEMENT_BACKEND=iam_deny PUBLIC_BASE_URL="$CAPS_API_URL" HOST=127.0.0.1 PORT="$PORT" \
  PYTHONPATH="$ROOT/src" exec "$STATE_DIR/venv/bin/python" -m usage_caps_api.app
}

cmd_run() {
  cd "$ROOT"
  python3 scripts/e2e_live_test.py --api-url "$CAPS_API_URL" --admin-key "$CAPS_ADMIN_KEY" --project "$PROJECT_ID" \
    --test-sa "$SA_EMAIL" ${E2E_LOCATION:+--location "$E2E_LOCATION"} ${E2E_MODEL:+--model "$E2E_MODEL"} \
    --report "${E2E_REPORT:-$ROOT/e2e-report-${PROJECT_ID}-$(date +%Y%m%d-%H%M%S).md}" "$@"
}

cmd_all() {
  cmd_setup
  ( cmd_server ) > "$STATE_DIR/server.log" 2>&1 &
  local pid=$!
  trap 'kill $pid 2>/dev/null || true' EXIT
  log "Waiting for the local API (log: $STATE_DIR/server.log)"
  for _ in $(seq 1 60); do
    curl -fsS "$CAPS_API_URL/v1/health" >/dev/null 2>&1 && break
    sleep 2
  done
  cmd_run "$@"
}

cmd_cleanup() {
  log "Removing the test identity from the managed deny policy (if present) via the API"
  curl -fsS -X POST -H "Authorization: Bearer ${CAPS_ADMIN_KEY}" -H "Content-Type: application/json" \
    -d '{"reason":"e2e cleanup"}' "$CAPS_API_URL/v1/users/${SA_EMAIL}/unblock" >/dev/null 2>&1 || \
    warn "API not reachable; remove ${SA_EMAIL} from deny policy gcp-ai-caps-managed manually if needed"
  log "Deleting ${SA_EMAIL}"
  gcloud iam service-accounts delete "$SA_EMAIL" --project "$PROJECT_ID" --quiet || true
}

case "${1:-}" in
  setup) shift; cmd_setup "$@" ;;
  server) shift; cmd_server "$@" ;;
  run) shift; cmd_run "$@" ;;
  all) shift; cmd_all "$@" ;;
  cleanup) shift; cmd_cleanup "$@" ;;
  *) sed -n '2,14p' "$0"; exit 1 ;;
esac
