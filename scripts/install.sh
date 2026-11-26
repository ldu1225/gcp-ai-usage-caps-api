#!/usr/bin/env bash
# GCP AI Usage & Spend Caps API: One-Click Installer
#
# Run directly in Google Cloud Shell (or macOS/Linux):
#
#   1) Single-project deployment:
#      bash <(curl -fsSL https://github.com/ldu1225/gcp-ai-usage-caps-api/raw/main/scripts/install.sh) \
#        --project PROJECT_ID
#
#   2) Multi-project hub deployment with IAM Deny v2 & real-time Pub/Sub enforcement:
#      bash <(curl -fsSL https://github.com/ldu1225/gcp-ai-usage-caps-api/raw/main/scripts/install.sh) \
#        --project HUB_PROJECT --link-projects PROJECT_B,PROJECT_C --org-id ORG_ID --grant-org-roles
#
#   3) Log collection setup only (BigQuery dataset + Log Router Sink):
#      bash <(curl -fsSL https://github.com/ldu1225/gcp-ai-usage-caps-api/raw/main/scripts/install.sh) \
#        --prepare-only --project PROJECT_ID
set -euo pipefail

REPO_URL="${CAPS_API_REPO_URL:-https://github.com/ldu1225/gcp-ai-usage-caps-api.git}"
INSTALL_DIR="${CAPS_API_INSTALL_DIR:-$HOME/gcp-ai-usage-caps-api}"

show_help() {
  cat <<'HELP'
GCP AI Usage & Spend Caps API - One-Click Installer

Usage:
  scripts/install.sh --project PROJECT_ID [options]

Options:
  --project ID              Primary GCP project ID (default: active gcloud project)
  --link-projects ID1,ID2   Additional GCP projects monitored together
  --region REGION           Cloud Run & BigQuery region (default: asia-northeast3)
  --service-name NAME       Cloud Run service name (default: gcp-ai-usage-caps-api)
  --dataset NAME            BigQuery audit dataset name (default: ge_analytics)
  --org-id ID               Numeric GCP organization ID (for IAM Deny v2 org roles)
  --grant-org-roles         Grant roles/iam.denyAdmin (enforcer) & roles/iam.denyReviewer (api)
  --billing-account ID      Billing account ID (XXXXXX-XXXXXX-XXXXXX) for project spend caps
  --spend-caps              Enable Cloud Billing Budget API v1 project hard spend caps
  --mode manual|auto        Default cap enforcement mode (default: manual)
  --no-realtime             Disable Pub/Sub real-time usage event push pipeline
  --prepare-only            Only configure BigQuery dataset, Log Router Sink & Data Access logs
  --dry-run                 Print commands without making changes
  -h, --help                Show this help
HELP
  exit "${1:-0}"
}

for arg in "$@"; do
  [[ "$arg" == "-h" || "$arg" == "--help" ]] && show_help 0
done

# Detect local checkout vs curl-streamed execution
SCRIPT_PATH="${BASH_SOURCE[0]:-}"
LOCAL_ROOT=""
if [[ -n "$SCRIPT_PATH" && -f "$SCRIPT_PATH" ]]; then
  CANDIDATE="$(cd "$(dirname "$SCRIPT_PATH")/.." 2>/dev/null && pwd || true)"
  if [[ -n "$CANDIDATE" && -f "$CANDIDATE/scripts/deploy.sh" ]]; then
    LOCAL_ROOT="$CANDIDATE"
  fi
fi

if [[ -z "$LOCAL_ROOT" ]]; then
  command -v git >/dev/null || { echo "error: git is required" >&2; exit 1; }
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    echo "==> Updating gcp-ai-usage-caps-api in $INSTALL_DIR"
    git -C "$INSTALL_DIR" fetch --depth 1 --quiet origin main
    git -C "$INSTALL_DIR" reset --hard --quiet FETCH_HEAD
  else
    echo "==> Cloning gcp-ai-usage-caps-api into $INSTALL_DIR"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  fi
  chmod +x "$INSTALL_DIR/scripts/"*.sh
  exec "$INSTALL_DIR/scripts/install.sh" "$@"
fi

PREPARE_ONLY=0
FORWARD_ARGS=()
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--prepare-only" ]]; then
    PREPARE_ONLY=1
    shift
  else
    FORWARD_ARGS+=("$1")
    shift
  fi
done

if [[ "$PREPARE_ONLY" == "1" ]]; then
  exec "$LOCAL_ROOT/scripts/prepare.sh" ${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}
fi

"$LOCAL_ROOT/scripts/prepare.sh" ${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}
exec "$LOCAL_ROOT/scripts/deploy.sh" ${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}
