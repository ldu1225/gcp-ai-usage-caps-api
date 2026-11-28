#!/usr/bin/env bash
# GCP AI Usage & Spend Caps API: One-Click Installer (hub-and-spoke)
#
# Run directly in Google Cloud Shell (or macOS/Linux):
#
#   1) Single hub project:
#      bash <(curl -fsSL https://github.com/ldu1225/gcp-ai-usage-caps-api/raw/main/scripts/install.sh) \
#        --project HUB_PROJECT
#
#   2) Hub governing managed projects (central dataset in the hub, real-time Pub/Sub, org roles):
#      bash <(curl -fsSL https://github.com/ldu1225/gcp-ai-usage-caps-api/raw/main/scripts/install.sh) \
#        --project HUB_PROJECT --managed-projects PROJECT_B,PROJECT_C --grant-org-roles --enable-data-access-logs
#
#   3) Every project under a folder:
#      ... --project HUB_PROJECT --scope-type folder --scope-ids 123456789 --aggregated-sink --grant-org-roles
#
#   4) Readiness check only (creates nothing):
#      ... --preflight-only --project HUB_PROJECT --managed-projects PROJECT_B
#
#   5) Log collection setup only (datasets + sinks + Data Access logs):
#      ... --prepare-only --project HUB_PROJECT --managed-projects PROJECT_B
set -euo pipefail

REPO_URL="${CAPS_API_REPO_URL:-https://github.com/ldu1225/gcp-ai-usage-caps-api.git}"
INSTALL_DIR="${CAPS_API_INSTALL_DIR:-$HOME/gcp-ai-usage-caps-api}"

show_help() {
  cat <<'HELP'
GCP AI Usage & Spend Caps API - One-Click Installer (hub-and-spoke)

Usage:
  scripts/install.sh --project HUB_PROJECT [--managed-projects A,B] [options]

Installer options:
  --preflight-only          Only run the environment readiness check (nothing is created)
  --prepare-only            Only enable APIs, create/adopt the dataset, sinks, Data Access logs and Gemini Platform
                            inference request metadata logging (--no-vertex-inference-logging to skip it)

Run scripts/prepare.sh --help for every common option (scope, datasets, adoption, VPC, org roles, ...).
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
    if [[ -n "$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=no 2>/dev/null)" && -z "${CAPS_API_FORCE_UPDATE:-}" ]]; then
      echo "error: $INSTALL_DIR has local changes. Commit or stash them, set CAPS_API_FORCE_UPDATE=1 to discard them," >&2
      echo "       or set CAPS_API_INSTALL_DIR to another directory." >&2
      exit 1
    fi
    echo "==> Updating gcp-ai-usage-caps-api in $INSTALL_DIR"
    git -C "$INSTALL_DIR" fetch --depth 1 --quiet "$REPO_URL" main
    git -C "$INSTALL_DIR" reset --hard --quiet FETCH_HEAD
  elif [[ -e "$INSTALL_DIR" ]]; then
    echo "error: $INSTALL_DIR exists but is not a git checkout of gcp-ai-usage-caps-api." >&2
    echo "       Move it away or set CAPS_API_INSTALL_DIR to another directory." >&2
    exit 1
  else
    echo "==> Cloning gcp-ai-usage-caps-api into $INSTALL_DIR"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  fi
  chmod +x "$INSTALL_DIR/scripts/"*.sh
  exec "$INSTALL_DIR/scripts/install.sh" "$@"
fi

ROOT_DIR="$LOCAL_ROOT"
# shellcheck source=lib/common.sh
source "${ROOT_DIR}/scripts/lib/common.sh"

usage() { show_help "${1:-0}"; }

PREPARE_ONLY=0
PREFLIGHT_ONLY=0
FORWARD_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --prepare-only) PREPARE_ONLY=1 ;;
    --preflight-only) PREFLIGHT_ONLY=1 ;;
    *) FORWARD_ARGS+=("$arg") ;;
  esac
done

# Validate the forwarded command line once and run the preflight ONCE for both prepare.sh and deploy.sh.
init_defaults
parse_common_args ${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}
finalize_args

if [[ "$PREFLIGHT_ONLY" == "1" ]]; then
  ARGS=()
  while IFS= read -r line; do ARGS+=("$line"); done < <(preflight_args)
  exec "${ROOT_DIR}/scripts/preflight.sh" "${ARGS[@]}"
fi

run_preflight
if [[ -n "${CAPS_API_PREFLIGHT_ENV:-}" ]]; then
  export CAPS_API_PREFLIGHT_ENV
fi

"${ROOT_DIR}/scripts/prepare.sh" ${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"} --skip-preflight
if [[ "$PREPARE_ONLY" == "1" ]]; then
  exit 0
fi
exec "${ROOT_DIR}/scripts/deploy.sh" ${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"} --skip-preflight
