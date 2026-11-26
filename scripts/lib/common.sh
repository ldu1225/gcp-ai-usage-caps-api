#!/usr/bin/env bash
# Shared helpers for prepare.sh / deploy.sh / install.sh / teardown.sh (hub-and-spoke install).
#
# Topology: ONE hub project hosts the Cloud Run API, the Enforcer Job, GCS state, Pub/Sub and Scheduler; MANY managed
# projects (--managed-projects, or --scope-type folder|organization --scope-ids) are governed from it. By default every
# managed project routes its AI logs into ONE central BigQuery dataset in the hub (--central-dataset); the legacy
# per-project datasets stay available with --per-project-datasets.
#
# Every helper is idempotent (describe -> create|update) and honours DRY_RUN=1 (prints "[dry-run] <command>").
# shellcheck disable=SC2034  # variables are consumed by the sourcing scripts

log()  { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

run() {
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

# Like run, but discards the command's stdout when it really runs (the dry-run echo stays visible).
run_quiet() {
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "[dry-run] $*"
  else
    "$@" >/dev/null
  fi
}

# Runs a command quietly (no dry-run echo) and returns its status; used for idempotency probes.
probe() { "$@" >/dev/null 2>&1; }

trim() { local s="$1"; s="${s#"${s%%[![:space:]]*}"}"; s="${s%"${s##*[![:space:]]}"}"; printf '%s' "$s"; }

# split_csv "a, b ,c" -> prints one item per line (empty items dropped)
split_csv() {
  local item
  local -a items=()
  IFS=',' read -r -a items <<<"${1:-}"
  for item in ${items[@]+"${items[@]}"}; do
    item="$(trim "$item")"
    [[ -n "$item" ]] && printf '%s\n' "$item"
  done
  return 0
}

# ---------------------------------------------------------------------------
# Argument parsing shared by every script (unknown flags are errors; flags of the other scripts are accepted so
# install.sh can forward one command line to prepare.sh and deploy.sh).
# ---------------------------------------------------------------------------
init_defaults() {
  PROJECT_ID=""
  MANAGED_PROJECTS=""
  SCOPE_TYPE="projects"
  SCOPE_IDS=""
  REGION="asia-northeast3"
  SERVICE_NAME="gcp-ai-usage-caps-api"
  DATASET="ge_analytics"
  DATASET_LOCATION=""
  DATASET_MODE="central"
  AUDIT_PROJECT=""
  REUSE_DATASET=""
  REUSE_SINKS=0
  AGGREGATED_SINK=0
  ENABLE_DATA_ACCESS_LOGS=0
  ORG_ID=""
  GRANT_ORG_ROLES=0
  BILLING_ACCOUNT=""
  SPEND_CAPS="false"
  MODE="manual"
  REALTIME=1
  VPC_NETWORK=""
  VPC_SUBNET=""
  VPC_EGRESS=""
  WORKER_POOL=""
  PREBUILT_IMAGE=""
  QUOTA_CONTACT_EMAIL=""
  MODEL_VIOLATION_ACTION="block_service"
  DENY_ATTACHMENT="projects"
  SKIP_PREFLIGHT=0
  SKIP_PREFLIGHT_BLOCKERS=0
  KEEP_LOG_SINKS=0
  DRY_RUN=0
  PREFLIGHT_LANG="${CAPS_API_LANG:-ko}"
  # v1.2: Gemini Platform inference request metadata logging (token source of Gemini Platform usage), enabled by prepare.sh on
  # every project unless ENABLE_VERTEX_INFERENCE_LOGGING=false / --no-vertex-inference-logging.
  ENABLE_VERTEX_INFERENCE_LOGGING="${ENABLE_VERTEX_INFERENCE_LOGGING:-true}"
  VERTEX_LOGGING_ENDPOINT="${VERTEX_LOGGING_ENDPOINT:-https://aiplatform.googleapis.com/v1beta1}"
  SERVICE_ACCOUNT_SUBJECTS="${SERVICE_ACCOUNT_SUBJECTS:-}"
  # Optional runtime settings passed to Cloud Run only when set (environment or flags).
  ENFORCEMENT_BACKEND="${ENFORCEMENT_BACKEND:-iam_deny}"
  CAP_SCOPE="${CAP_SCOPE:-}"
  VERTEX_USER_HEADER_LABEL="${VERTEX_USER_HEADER_LABEL:-}"
  METERED_SURFACES="${METERED_SURFACES:-}"
  MODEL_VIOLATION_BLOCK_SCOPE="${MODEL_VIOLATION_BLOCK_SCOPE:-}"
  MODEL_PUBLISHERS="${MODEL_PUBLISHERS:-}"
  LONG_CONTEXT_THRESHOLD_TOKENS="${LONG_CONTEXT_THRESHOLD_TOKENS:-}"
  PROVISIONED_THROUGHPUT_COST="${PROVISIONED_THROUGHPUT_COST:-}"
  PRICING_SYNC_SCHEDULE="${PRICING_SYNC_SCHEDULE:-17 3 * * *}"
  PRICING_SYNC_TIME_ZONE="${PRICING_SYNC_TIME_ZONE:-Etc/UTC}"
}

COMMON_FLAGS_HELP='Common options (accepted by prepare.sh, deploy.sh, install.sh, teardown.sh):
  --project HUB                 Hub (main) project hosting the API, Enforcer Job, state, Pub/Sub, Scheduler
  --managed-projects A,B        Managed (spoke) projects governed by the hub (alias: --link-projects)
  --scope-type folder|organization --scope-ids ID[,ID]
                                Govern every project under folders / an organization (discovered by preflight)
  --aggregated-sink             With --scope-type: one folder/org-level sink (includeChildren) instead of per-project
  --region REGION               Hub region (default asia-northeast3)
  --service-name NAME           Cloud Run service name (default gcp-ai-usage-caps-api)
  --dataset NAME                Audit dataset id (default ge_analytics)
  --dataset-location LOC        BigQuery location (default: --region)
  --central-dataset             (default) ONE dataset in the hub; sinks of every project write into it
  --audit-project P             Central dataset project (default: the hub)
  --per-project-datasets        Legacy: a dataset in every project
  --reuse-dataset P:D           Adopt an existing dataset (no dataset is created)
  --reuse-sinks                 Adopt existing sinks detected by preflight instead of creating ours
  --enable-data-access-logs     Merge Gemini Platform DATA_READ/DATA_WRITE audit configs into every project (never clobbers)
  --org-id ID                   Organization id (default: detected)
  --grant-org-roles             Grant org-level roles (Deny Admin/Reviewer, Org Policy Admin; folder/org roles)
  --deny-attachment projects|scope
  --billing-account ID          Billing account (project spend caps)   --spend-caps   Enable spend caps
  --mode manual|auto            Default cap enforcement mode           --no-realtime  No Pub/Sub pipeline
  --vpc-network N --vpc-subnet S [--vpc-egress all-traffic|private-ranges-only]
  --worker-pool projects/P/locations/R/workerPools/W   --image IMAGE (prebuilt, skip build)
  --quota-contact-email EMAIL   Contact for quota increase requests
  --model-violation-action ACT  Default block_service
  --no-vertex-inference-logging Do not enable Gemini Platform inference request metadata logging (default: enabled on
                                every project; env ENABLE_VERTEX_INFERENCE_LOGGING=false)
  --enforcement-backend iam_deny|notify_only   (env ENFORCEMENT_BACKEND, default iam_deny)
  --cap-scope total|per_project --metered-surfaces A,B --vertex-user-header-label L
  --model-violation-block-scope project|all    Optional runtime settings (also read from the same-named env vars)
  --skip-preflight              Do not run the readiness preflight
  --skip-preflight-blockers     Continue even when preflight reports blockers
  --dry-run                     Print commands without changing anything
  -h, --help'

need_value() { [[ $# -ge 2 && -n "${2:-}" && "${2:0:2}" != "--" ]] || die "option $1 needs a value"; }

parse_common_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --project) need_value "$@"; PROJECT_ID="$2"; shift 2 ;;
      --managed-projects|--link-projects) need_value "$@"; MANAGED_PROJECTS="$2"; shift 2 ;;
      --scope-type) need_value "$@"; SCOPE_TYPE="$2"; shift 2 ;;
      --scope-ids) need_value "$@"; SCOPE_IDS="$2"; shift 2 ;;
      --aggregated-sink) AGGREGATED_SINK=1; shift ;;
      --region) need_value "$@"; REGION="$2"; shift 2 ;;
      --service-name) need_value "$@"; SERVICE_NAME="$2"; shift 2 ;;
      --dataset) need_value "$@"; DATASET="$2"; shift 2 ;;
      --dataset-location) need_value "$@"; DATASET_LOCATION="$2"; shift 2 ;;
      --central-dataset) DATASET_MODE="central"; shift ;;
      --per-project-datasets) DATASET_MODE="per_project"; shift ;;
      --audit-project) need_value "$@"; AUDIT_PROJECT="$2"; DATASET_MODE="central"; shift 2 ;;
      --reuse-dataset) need_value "$@"; REUSE_DATASET="$2"; DATASET_MODE="central"; shift 2 ;;
      --reuse-sinks) REUSE_SINKS=1; shift ;;
      --enable-data-access-logs) ENABLE_DATA_ACCESS_LOGS=1; shift ;;
      --org-id) need_value "$@"; ORG_ID="$2"; shift 2 ;;
      --grant-org-roles) GRANT_ORG_ROLES=1; shift ;;
      --deny-attachment) need_value "$@"; DENY_ATTACHMENT="$2"; shift 2 ;;
      --billing-account) need_value "$@"; BILLING_ACCOUNT="$2"; shift 2 ;;
      --spend-caps) SPEND_CAPS="true"; shift ;;
      --mode) need_value "$@"; MODE="$2"; shift 2 ;;
      --no-realtime) REALTIME=0; shift ;;
      --vpc-network) need_value "$@"; VPC_NETWORK="$2"; shift 2 ;;
      --vpc-subnet) need_value "$@"; VPC_SUBNET="$2"; shift 2 ;;
      --vpc-egress) need_value "$@"; VPC_EGRESS="$2"; shift 2 ;;
      --worker-pool) need_value "$@"; WORKER_POOL="$2"; shift 2 ;;
      --image) need_value "$@"; PREBUILT_IMAGE="$2"; shift 2 ;;
      --quota-contact-email) need_value "$@"; QUOTA_CONTACT_EMAIL="$2"; shift 2 ;;
      --model-violation-action) need_value "$@"; MODEL_VIOLATION_ACTION="$2"; shift 2 ;;
      --enable-vertex-inference-logging) ENABLE_VERTEX_INFERENCE_LOGGING="true"; shift ;;
      --no-vertex-inference-logging) ENABLE_VERTEX_INFERENCE_LOGGING="false"; shift ;;
      --enforcement-backend) need_value "$@"; ENFORCEMENT_BACKEND="$2"; shift 2 ;;
      --cap-scope) need_value "$@"; CAP_SCOPE="$2"; shift 2 ;;
      --metered-surfaces) need_value "$@"; METERED_SURFACES="$2"; shift 2 ;;
      --vertex-user-header-label) need_value "$@"; VERTEX_USER_HEADER_LABEL="$2"; shift 2 ;;
      --model-violation-block-scope) need_value "$@"; MODEL_VIOLATION_BLOCK_SCOPE="$2"; shift 2 ;;
      --skip-preflight) SKIP_PREFLIGHT=1; shift ;;
      --skip-preflight-blockers) SKIP_PREFLIGHT_BLOCKERS=1; shift ;;
      --keep-log-sinks) KEEP_LOG_SINKS=1; shift ;;
      --lang) need_value "$@"; PREFLIGHT_LANG="$2"; shift 2 ;;
      --dry-run) DRY_RUN=1; shift ;;
      -h|--help) usage 0 ;;
      *) echo "Unknown option: $1" >&2; usage 2 ;;
    esac
  done
}

# Validates arguments and derives HUB / dataset / service-account variables.
finalize_args() {
  command -v gcloud >/dev/null || die "gcloud CLI is required"
  if [[ -z "$PROJECT_ID" ]]; then
    PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
  fi
  [[ -n "$PROJECT_ID" && "$PROJECT_ID" != "(unset)" ]] || die "pass --project HUB_PROJECT_ID"
  [[ "$PROJECT_ID" =~ ^[a-z][a-z0-9.:-]{4,99}$ ]] || die "invalid --project: $PROJECT_ID"
  case "$SCOPE_TYPE" in projects|folder|organization) ;; *) die "--scope-type must be folder or organization" ;; esac
  if [[ "$SCOPE_TYPE" != "projects" && -z "$SCOPE_IDS" ]]; then
    die "--scope-type $SCOPE_TYPE needs --scope-ids"
  fi
  [[ -z "$SCOPE_IDS" || "$SCOPE_IDS" =~ ^[0-9,[:space:]]+$ ]] || die "--scope-ids must be numeric ids"
  [[ "$DATASET" =~ ^[A-Za-z0-9_]{1,1024}$ ]] || die "invalid --dataset"
  [[ -z "$ORG_ID" || "$ORG_ID" =~ ^[0-9]+$ ]] || die "--org-id must be numeric"
  case "$DENY_ATTACHMENT" in projects|scope) ;; *) die "--deny-attachment must be projects or scope" ;; esac
  case "$MODE" in manual|auto) ;; *) die "--mode must be manual or auto" ;; esac
  case "$VPC_EGRESS" in ""|all-traffic|private-ranges-only) ;; *) die "--vpc-egress must be all-traffic or private-ranges-only" ;; esac
  case "$ENFORCEMENT_BACKEND" in iam_deny|notify_only) ;; *) die "--enforcement-backend / ENFORCEMENT_BACKEND must be iam_deny or notify_only" ;; esac
  case "$ENABLE_VERTEX_INFERENCE_LOGGING" in true|false|1|0) ;; *) die "ENABLE_VERTEX_INFERENCE_LOGGING must be true or false" ;; esac
  [[ "$ENABLE_VERTEX_INFERENCE_LOGGING" == "1" ]] && ENABLE_VERTEX_INFERENCE_LOGGING="true"
  [[ "$ENABLE_VERTEX_INFERENCE_LOGGING" == "0" ]] && ENABLE_VERTEX_INFERENCE_LOGGING="false"
  case "$CAP_SCOPE" in ""|total|per_project) ;; *) die "--cap-scope / CAP_SCOPE must be total or per_project" ;; esac
  case "$MODEL_VIOLATION_BLOCK_SCOPE" in ""|project|all) ;; *) die "--model-violation-block-scope must be project or all" ;; esac
  case "$PROVISIONED_THROUGHPUT_COST" in ""|on_demand|zero) ;; *) die "PROVISIONED_THROUGHPUT_COST must be on_demand or zero" ;; esac
  [[ -z "$METERED_SURFACES" || "$METERED_SURFACES" =~ ^[a-z_,[:space:]]+$ ]] || die "invalid --metered-surfaces"
  [[ -z "$MODEL_PUBLISHERS" || "$MODEL_PUBLISHERS" =~ ^[a-z0-9,[:space:]-]+$ ]] || die "invalid MODEL_PUBLISHERS"
  [[ -z "$VERTEX_USER_HEADER_LABEL" || "$VERTEX_USER_HEADER_LABEL" =~ ^[A-Za-z0-9_-]{1,40}$ ]] || die "invalid --vertex-user-header-label"
  [[ -z "$LONG_CONTEXT_THRESHOLD_TOKENS" || "$LONG_CONTEXT_THRESHOLD_TOKENS" =~ ^[0-9]{1,9}$ ]] || die "LONG_CONTEXT_THRESHOLD_TOKENS must be an integer"
  [[ "$VERTEX_LOGGING_ENDPOINT" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?/v1beta1$ ]] || die "VERTEX_LOGGING_ENDPOINT must be https://HOST/v1beta1"
  if [[ -n "$VPC_NETWORK" && -z "$VPC_SUBNET" ]] || [[ -z "$VPC_NETWORK" && -n "$VPC_SUBNET" ]]; then
    die "--vpc-network and --vpc-subnet go together"
  fi
  HUB="$PROJECT_ID"
  DATASET_LOCATION="${DATASET_LOCATION:-$REGION}"
  if [[ -n "$REUSE_DATASET" ]]; then
    [[ "$REUSE_DATASET" =~ ^([a-z][a-z0-9.:-]{4,99}):([A-Za-z0-9_]{1,1024})$ ]] || die "--reuse-dataset must be PROJECT:DATASET"
    AUDIT_PROJECT="${BASH_REMATCH[1]}"
    DATASET="${BASH_REMATCH[2]}"
  fi
  if [[ "$DATASET_MODE" == "central" ]]; then
    AUDIT_PROJECT="${AUDIT_PROJECT:-$HUB}"
  else
    AUDIT_PROJECT=""
  fi
  API_SA_ID="ai-usage-caps-api"
  ENFORCER_SA_ID="ai-usage-caps-enforcer"
  API_SA="${API_SA_ID}@${HUB}.iam.gserviceaccount.com"
  ENFORCER_SA="${ENFORCER_SA_ID}@${HUB}.iam.gserviceaccount.com"
  AI_SINK="${SERVICE_NAME}-ai-logs"
  TOPIC="${SERVICE_NAME}-usage-events"
  USAGE_SINK="${SERVICE_NAME}-usage-events"
  SUB="${SERVICE_NAME}-usage-events-push"
  ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
}

# Managed project list: explicit --managed-projects plus projects resolved by preflight under folder/org scope.
managed_list() {
  local p seen=" ${HUB} "
  {
    split_csv "$MANAGED_PROJECTS"
    split_csv "${PREFLIGHT_MANAGED_PROJECTS:-}"
  } | while IFS= read -r p; do
    [[ "$p" =~ ^[a-z][a-z0-9.:-]{4,99}$ ]] || { warn "skipping invalid project id '$p'"; continue; }
    if [[ "$seen" != *" $p "* ]]; then
      seen="${seen}${p} "
      printf '%s\n' "$p"
    fi
  done
}

# Hub first, then managed projects.
all_projects() {
  printf '%s\n' "$HUB"
  managed_list
}

managed_csv() { managed_list | paste -sd, - ; }

# Materializes MANAGED_ARR / ALL_ARR (hub first) so loops never share stdin with gcloud (bash 3.2 compatible).
load_project_arrays() {
  local p
  MANAGED_ARR=()
  while IFS= read -r p; do
    MANAGED_ARR+=("$p")
  done < <(managed_list)
  ALL_ARR=("$HUB" ${MANAGED_ARR[@]+"${MANAGED_ARR[@]}"})
  SCOPE_ID_ARR=()
  while IFS= read -r p; do
    SCOPE_ID_ARR+=("$p")
  done < <(split_csv "$SCOPE_IDS")
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
PREFLIGHT_KEYS_RE='^PREFLIGHT_[A-Z_]+$'

# Loads PREFLIGHT_* assignments from FILE without sourcing it (values are filtered to a safe charset).
load_preflight_env() {
  local file="$1" key val
  [[ -f "$file" ]] || return 0
  while IFS='=' read -r key val; do
    [[ "$key" =~ $PREFLIGHT_KEYS_RE ]] || continue
    val="$(printf '%s' "$val" | tr -cd 'A-Za-z0-9_.:/,@=+-')"
    printf -v "$key" '%s' "$val"
    export "${key?}"
  done <"$file"
  return 0
}

preflight_args() {
  local -a args=(--project "$HUB" --region "$REGION" --dataset "$DATASET" --service-name "$SERVICE_NAME"
                 --lang "$PREFLIGHT_LANG" --deny-attachment "$DENY_ATTACHMENT")
  [[ -n "$MANAGED_PROJECTS" ]] && args+=(--managed-projects "$MANAGED_PROJECTS")
  [[ "$SCOPE_TYPE" != "projects" ]] && args+=(--scope-type "$SCOPE_TYPE" --scope-ids "$SCOPE_IDS")
  [[ -n "$AUDIT_PROJECT" ]] && args+=(--audit-project "$AUDIT_PROJECT")
  [[ "${DATASET_MODE:-central}" == "per_project" ]] && args+=(--per-project-datasets)
  [[ -n "$VPC_NETWORK" ]] && args+=(--vpc-network "$VPC_NETWORK" --vpc-subnet "$VPC_SUBNET")
  [[ -n "$VPC_EGRESS" ]] && args+=(--vpc-egress "$VPC_EGRESS")
  [[ -n "$ORG_ID" ]] && args+=(--org-id "$ORG_ID")
  args+=(--runtime-sa "$API_SA" --enforcer-sa "$ENFORCER_SA")
  printf '%s\n' "${args[@]}"
}

# run_preflight: readiness check BEFORE anything is created. Stops on blockers that the installer cannot fix itself
# (unless --skip-preflight-blockers). The PREFLIGHT_* adaptations (DRS, invoker IAM, ingress, VPC egress, Binary
# Authorization, Docker build, adoptable datasets/sinks, resolved scope projects) are exported for later steps and
# reused by the next script through CAPS_API_PREFLIGHT_ENV.
run_preflight() {
  if [[ -n "${CAPS_API_PREFLIGHT_ENV:-}" && -f "${CAPS_API_PREFLIGHT_ENV}" ]]; then
    load_preflight_env "$CAPS_API_PREFLIGHT_ENV"
    return 0
  fi
  if [[ "$SKIP_PREFLIGHT" == "1" || -n "${CAPS_API_SKIP_PREFLIGHT:-}" ]]; then
    info "preflight skipped (--skip-preflight)"
    return 0
  fi
  local -a args=()
  local line rc=0 env_file
  while IFS= read -r line; do args+=("$line"); done < <(preflight_args)
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "[dry-run] scripts/preflight.sh ${args[*]} --install-mode --env-file <tmp>"
    return 0
  fi
  command -v python3 >/dev/null || { warn "python3 not found; skipping preflight"; return 0; }
  log "0. Environment readiness preflight (환경 사전 점검) - nothing is created before it passes"
  env_file="$(mktemp "${TMPDIR:-/tmp}/caps-preflight.XXXXXX")"
  PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m usage_caps_api.preflight "${args[@]}" \
    --install-mode --no-strict --env-file "$env_file" || rc=$?
  load_preflight_env "$env_file"
  export CAPS_API_PREFLIGHT_ENV="$env_file"
  case "$rc" in
    0|1) return 0 ;;
    2)
      if [[ "$SKIP_PREFLIGHT_BLOCKERS" == "1" ]]; then
        warn "preflight reported blockers; continuing because --skip-preflight-blockers was given"
        return 0
      fi
      die "preflight found blockers that the installer cannot fix (see [FAIL] above). Nothing was created.
  Fix them (the remediation script above lists the commands) and re-run, or add --skip-preflight-blockers.
  [안내] 사전 점검에서 설치를 막는 항목이 발견되어 아무 리소스도 만들지 않았습니다. 위 [FAIL] 항목을 조치한 뒤 다시 실행하세요." ;;
    *) warn "preflight could not run (exit ${rc}); continuing with runtime fallbacks" ;;
  esac
}

# ---------------------------------------------------------------------------
# APIs
# ---------------------------------------------------------------------------
# enable_apis PROJECT "required apis..." "optional apis..."
# Enables in one batch, falls back to per-API enablement; a required API that cannot be enabled aborts, optional ones
# only warn (their features use built-in fallbacks).
enable_apis() {
  local project="$1" required="$2" optional="${3:-}" api
  local -a req=() opt=()
  read -r -a req <<<"$required"
  read -r -a opt <<<"$optional"
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    run gcloud services enable ${req[@]+"${req[@]}"} ${opt[@]+"${opt[@]}"} --project="$project"
    return 0
  fi
  if [[ ${#req[@]} -gt 0 ]] && ! gcloud services enable "${req[@]}" --project="$project" --quiet >/dev/null 2>&1; then
    for api in "${req[@]}"; do
      gcloud services enable "$api" --project="$project" --quiet >/dev/null 2>&1 \
        || die "could not enable required API ${api} on ${project} (organization policy, billing or permission)"
    done
  fi
  if [[ ${#opt[@]} -gt 0 ]] && ! gcloud services enable "${opt[@]}" --project="$project" --quiet >/dev/null 2>&1; then
    for api in "${opt[@]}"; do
      gcloud services enable "$api" --project="$project" --quiet >/dev/null 2>&1 \
        || warn "could not enable ${api} on ${project}; continuing with its fallback"
    done
  fi
}

# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------
grant_project_role() {  # grant_project_role PROJECT MEMBER ROLE
  run_quiet gcloud projects add-iam-policy-binding "$1" --member="$2" --role="$3" --condition=None --quiet \
    || warn "could not grant $3 to $2 on $1 (ask a project owner to run it)"
}

grant_org_role() {  # grant_org_role ORG MEMBER ROLE
  run_quiet gcloud organizations add-iam-policy-binding "$1" --member="$2" --role="$3" --condition=None --quiet \
    || warn "could not grant $3 to $2 on organization $1"
}

grant_folder_role() {  # grant_folder_role FOLDER MEMBER ROLE
  run_quiet gcloud resource-manager folders add-iam-policy-binding "$1" --member="$2" --role="$3" --condition=None \
    --quiet || warn "could not grant $3 to $2 on folder $1"
}

detect_org_id() {
  [[ -n "$ORG_ID" ]] && return 0
  if [[ -n "${PREFLIGHT_ORG_ID:-}" ]]; then
    ORG_ID="$PREFLIGHT_ORG_ID"
    return 0
  fi
  [[ "${DRY_RUN:-0}" == "1" ]] && return 0
  ORG_ID="$(gcloud projects get-ancestors "$HUB" --format='value(id,type)' 2>/dev/null \
    | awk '$2=="organization"{print $1; exit}' || true)"
  return 0
}

# ---------------------------------------------------------------------------
# BigQuery & Log Router
# ---------------------------------------------------------------------------
AI_LOG_FILTER='log_id("businessaicode.googleapis.com/inference_response") OR log_id("aiplatform.googleapis.com/inference_request") OR (log_id("cloudaudit.googleapis.com/data_access") AND protoPayload.serviceName="aiplatform.googleapis.com") OR log_id("discoveryengine.googleapis.com/gemini_enterprise_user_activity") OR log_id("discoveryengine.googleapis.com/gen_ai_user_message")'
REALTIME_FILTER='log_id("businessaicode.googleapis.com/inference_response") OR log_id("aiplatform.googleapis.com/inference_request")'

ensure_dataset() {  # ensure_dataset PROJECT DATASET LOCATION
  local project="$1" dataset="$2" location="$3"
  if probe bq --project_id="$project" show --dataset "${project}:${dataset}"; then
    info "dataset ${project}:${dataset} exists (adopted)"
    return 0
  fi
  run bq --project_id="$project" mk --dataset --location="$location" \
    --description="AI telemetry logs for GCP AI Usage & Spend Caps API" "${project}:${dataset}" \
    || die "could not create dataset ${project}:${dataset} in ${location} (location policy? use --dataset-location or --reuse-dataset)"
}

# Dataset-level WRITER (BigQuery Data Editor) for one principal; project IAM is not touched.
grant_dataset_writer() {  # grant_dataset_writer PROJECT DATASET MEMBER
  local project="$1" dataset="$2" member="$3" file rc=0
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "[dry-run] grant ${member} roles/bigquery.dataEditor (dataset WRITER) on ${project}:${dataset}"
    return 0
  fi
  file="$(mktemp)"
  if ! bq --project_id="$project" show --format=prettyjson "${project}:${dataset}" >"$file" 2>/dev/null; then
    rm -f "$file"
    warn "cannot read ${project}:${dataset} to grant ${member}; run: bq add-iam-policy-binding --member=${member} --role=roles/bigquery.dataEditor ${project}:${dataset}"
    return 0
  fi
  python3 - "$file" "$member" <<'PYACL' || rc=$?
import json, sys
path, member = sys.argv[1], sys.argv[2].strip()
email = member.split(":", 1)[-1].lower()
key = "groupByEmail" if member.lower().startswith("group:") else "userByEmail"
raw = open(path, encoding="utf-8", errors="replace").read()
dataset = json.loads(raw[raw.index("{"):])
access = dataset.setdefault("access", [])
if any((a.get(key) or "").lower() == email and a.get("role") in ("WRITER", "OWNER") for a in access):
    sys.exit(3)
access.append({"role": "WRITER", key: email})
json.dump(dataset, open(path, "w", encoding="utf-8"))
PYACL
  if [[ $rc -eq 0 ]]; then
    bq --project_id="$project" update --source "$file" "${project}:${dataset}" >/dev/null 2>&1 \
      || warn "could not grant ${member} WRITER on ${project}:${dataset}"
  fi
  rm -f "$file"
  return 0
}

sink_writer() {  # sink_writer SINK_SCOPE_FLAG... -> prints writerIdentity
  gcloud logging sinks describe "$@" --format='value(writerIdentity)' 2>/dev/null || true
}

# ensure_bq_sink SOURCE_PROJECT DEST_PROJECT DATASET
ensure_bq_sink() {
  local src="$1" dproject="$2" dataset="$3" dest writer
  dest="bigquery.googleapis.com/projects/${dproject}/datasets/${dataset}"
  if [[ "$REUSE_SINKS" == "1" && ",${PREFLIGHT_ADOPT_SINKS:-}," == *",${src}:"* ]]; then
    info "${src}: adopting existing AI log sink(s) detected by preflight (${PREFLIGHT_ADOPT_SINKS}); not creating ${AI_SINK}"
    return 0
  fi
  if probe gcloud logging sinks describe "$AI_SINK" --project="$src"; then
    run gcloud logging sinks update "$AI_SINK" "$dest" --project="$src" --log-filter="$AI_LOG_FILTER" --quiet
  else
    run gcloud logging sinks create "$AI_SINK" "$dest" --project="$src" --log-filter="$AI_LOG_FILTER" \
      --use-partitioned-tables --quiet
  fi
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    grant_dataset_writer "$dproject" "$dataset" "serviceAccount:<writer-of-${AI_SINK}@${src}>"
    return 0
  fi
  writer="$(sink_writer "$AI_SINK" --project="$src")"
  [[ -n "$writer" ]] && grant_dataset_writer "$dproject" "$dataset" "$writer"
  info "${src}: sink ${AI_SINK} -> ${dproject}:${dataset}"
}

# ensure_aggregated_bq_sink SCOPE_TYPE SCOPE_ID DEST_PROJECT DATASET (folder/organization, includeChildren)
ensure_aggregated_bq_sink() {
  local stype="$1" sid="$2" dproject="$3" dataset="$4" dest flag writer
  dest="bigquery.googleapis.com/projects/${dproject}/datasets/${dataset}"
  flag="--folder=${sid}"
  [[ "$stype" == "organization" ]] && flag="--organization=${sid}"
  if probe gcloud logging sinks describe "$AI_SINK" "$flag"; then
    run gcloud logging sinks update "$AI_SINK" "$dest" "$flag" --log-filter="$AI_LOG_FILTER" --quiet
  else
    run gcloud logging sinks create "$AI_SINK" "$dest" "$flag" --include-children --log-filter="$AI_LOG_FILTER" \
      --use-partitioned-tables --quiet
  fi
  if [[ "${DRY_RUN:-0}" != "1" ]]; then
    writer="$(sink_writer "$AI_SINK" "$flag")"
    [[ -n "$writer" ]] && grant_dataset_writer "$dproject" "$dataset" "$writer"
  else
    grant_dataset_writer "$dproject" "$dataset" "serviceAccount:<writer-of-${AI_SINK}@${stype}/${sid}>"
  fi
}

# ensure_pubsub_sink SOURCE_PROJECT: routes Antigravity inference_response and Gemini Platform inference_request logs to the
# ONE hub topic (an existing sink is updated to the current filter).
ensure_pubsub_sink() {
  local src="$1" dest writer
  dest="pubsub.googleapis.com/projects/${HUB}/topics/${TOPIC}"
  if probe gcloud logging sinks describe "$USAGE_SINK" --project="$src"; then
    run gcloud logging sinks update "$USAGE_SINK" "$dest" --project="$src" --log-filter="$REALTIME_FILTER" --quiet
  else
    run gcloud logging sinks create "$USAGE_SINK" "$dest" --project="$src" --log-filter="$REALTIME_FILTER" --quiet
  fi
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "[dry-run] grant <writer-of-${USAGE_SINK}@${src}> roles/pubsub.publisher on projects/${HUB}/topics/${TOPIC}"
    return 0
  fi
  writer="$(sink_writer "$USAGE_SINK" --project="$src")"
  if [[ -n "$writer" ]]; then
    gcloud pubsub topics add-iam-policy-binding "$TOPIC" --project="$HUB" --member="$writer" \
      --role="roles/pubsub.publisher" --quiet >/dev/null 2>&1 \
      || warn "could not grant roles/pubsub.publisher to ${writer} on ${TOPIC}"
  fi
}

# enable_data_access_logs PROJECT: merges aiplatform.googleapis.com DATA_READ + DATA_WRITE (+ ADMIN_READ) into the
# project's auditConfigs via get-iam-policy / set-iam-policy (etag-protected, retried); never removes existing configs.
enable_data_access_logs() {
  local project="$1" file changed attempt
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "[dry-run] enable Gemini Platform Data Access audit logs (ADMIN_READ, DATA_READ, DATA_WRITE) on ${project} (merge into existing auditConfigs)"
    return 0
  fi
  for attempt in 1 2 3 4; do
    file="$(mktemp)"
    if ! gcloud projects get-iam-policy "$project" --format=json >"$file" 2>/dev/null; then
      rm -f "$file"
      warn "cannot read the IAM policy of ${project}; Data Access audit logs not changed"
      return 0
    fi
    changed="$(python3 - "$file" <<'PYAUDIT'
import json, sys
path = sys.argv[1]
policy = json.load(open(path))
configs = policy.setdefault("auditConfigs", [])
entry = next((c for c in configs if c.get("service") == "aiplatform.googleapis.com"), None)
if entry is None:
    entry = {"service": "aiplatform.googleapis.com", "auditLogConfigs": []}
    configs.append(entry)
present = {c.get("logType") for c in entry.setdefault("auditLogConfigs", [])}
changed = False
for log_type in ("ADMIN_READ", "DATA_READ", "DATA_WRITE"):
    if log_type not in present:
        entry["auditLogConfigs"].append({"logType": log_type})
        changed = True
json.dump(policy, open(path, "w"))
print("yes" if changed else "no")
PYAUDIT
)"
    if [[ "$changed" != "yes" ]]; then
      rm -f "$file"
      info "${project}: Gemini Platform Data Access audit logs already enabled"
      return 0
    fi
    if gcloud projects set-iam-policy "$project" "$file" --quiet >/dev/null 2>&1; then
      rm -f "$file"
      info "${project}: Gemini Platform Data Access audit logs enabled (existing auditConfigs kept)"
      return 0
    fi
    rm -f "$file"
    sleep $((attempt * 2))
  done
  warn "could not update Data Access audit logs on ${project} (concurrent IAM change or permission)"
}

# ---------------------------------------------------------------------------
# Gemini Platform inference request metadata logging (aiplatform.googleapis.com/inference_request)
# ---------------------------------------------------------------------------
# There is no gcloud command and no Terraform resource (hashicorp/terraform-provider-google#30642) for this setting:
# it is the v1beta1 project-level PublisherModelConfig, written with setPublisherModelConfig and read with
# fetchPublisherModelConfig. Identical writes return 409 ALREADY_EXISTS (= already enabled).
VERTEX_LOGGING_BODY='{"publisherModelConfig":{"inferenceEventLoggingConfig":{"state":"ENABLED"}}}'
VERTEX_READER_ROLE_ID="gcpAiCapsVertexLoggingReader"
VERTEX_READER_PERMISSION="aiplatform.endpoints.fetchPublisherModelConfig"

vertex_logging_cost_note() {
  info "Cost note: every Gemini Platform request then writes ONE small metadata entry (model, token counts, latency; no"
  info "prompts/responses) to Cloud Logging in the project that served it. Cloud Logging ingestion/storage pricing"
  info "applies (_Default bucket, 30-day retention) plus the copy routed to BigQuery by ${AI_SINK:-the AI log sink}."
  info "Disable with --no-vertex-inference-logging (Gemini Platform token usage is then not measured)."
}

# _csv_has LIST ITEM -> 0 when ITEM is in the comma-separated LIST
_csv_has() { [[ ",${1}," == *",${2},"* ]]; }

# enable_vertex_inference_logging PROJECT: fetch -> skip when ENABLED -> setPublisherModelConfig (409 tolerated).
# Never aborts the installer: permission, VPC-SC or disabled-API errors only warn (with the copy-paste command).
enable_vertex_inference_logging() {
  local project="$1" fetch_url set_url token out code
  fetch_url="${VERTEX_LOGGING_ENDPOINT}/projects/${project}:fetchPublisherModelConfig"
  set_url="${VERTEX_LOGGING_ENDPOINT}/projects/${project}:setPublisherModelConfig"
  local manual="curl -sS -X POST -H \"Authorization: Bearer \$(gcloud auth print-access-token)\" -H \"Content-Type: application/json\" \"${set_url}\" -d '${VERTEX_LOGGING_BODY}'"
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "[dry-run] curl -sS -X GET -H \"Authorization: Bearer \$(gcloud auth print-access-token)\" \"${fetch_url}\"  # skip when inferenceEventLoggingConfig.state=ENABLED"
    echo "[dry-run] ${manual}  # setPublisherModelConfig (409 = already enabled)"
    return 0
  fi
  command -v curl >/dev/null || { warn "curl not found; enable Gemini Platform inference logging on ${project} manually: ${manual}"; return 0; }
  token="$(gcloud auth print-access-token 2>/dev/null || true)"
  [[ -n "$token" ]] || { warn "no access token (gcloud auth print-access-token); enable on ${project} manually: ${manual}"; return 0; }
  out="$(mktemp)"
  code="$(curl -sS -o "$out" -w '%{http_code}' --max-time 30 -H "Authorization: Bearer ${token}" "$fetch_url" 2>/dev/null || echo 000)"
  if [[ "$code" == "200" ]] && grep -Eq '"state"[[:space:]]*:[[:space:]]*"ENABLED"' "$out"; then
    rm -f "$out"
    info "${project}: Gemini Platform inference request metadata logging already ENABLED"
    return 0
  fi
  code="$(curl -sS -o "$out" -w '%{http_code}' --max-time 60 -X POST -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/json" "$set_url" -d "$VERTEX_LOGGING_BODY" 2>/dev/null || echo 000)"
  case "$code" in
    2??) info "${project}: Gemini Platform inference request metadata logging ENABLED (effective within a few minutes)" ;;
    409) info "${project}: Gemini Platform inference request metadata logging already ENABLED (409)" ;;
    *)
      if grep -Eq 'SERVICE_DISABLED|has not been used|it is disabled' "$out" 2>/dev/null; then
        warn "${project}: aiplatform.googleapis.com is disabled (Gemini Platform unused there); inference logging not enabled"
      elif grep -Eq 'VPC Service Controls|SECURITY_POLICY_VIOLATED' "$out" 2>/dev/null; then
        warn "${project}: blocked by VPC Service Controls (aiplatform.googleapis.com); add an ingress rule for this caller, then: ${manual}"
      elif [[ "$code" == "403" ]]; then
        warn "${project}: permission denied (needs aiplatform.endpoints.setPublisherModelConfig, e.g. roles/aiplatform.admin); run: ${manual}"
      else
        warn "${project}: could not enable Gemini Platform inference logging (HTTP ${code}); run: ${manual}"
      fi ;;
  esac
  rm -f "$out"
  return 0
}

# ensure_vertex_reader_role PROJECT MEMBER: one-permission custom role (fetchPublisherModelConfig) bound to MEMBER so
# the runtime can report the logging state. No read-only predefined role is documented to contain this permission.
ensure_vertex_reader_role() {
  local project="$1" member="$2"
  if ! probe gcloud iam roles describe "$VERTEX_READER_ROLE_ID" --project="$project"; then
    run_quiet gcloud iam roles create "$VERTEX_READER_ROLE_ID" --project="$project" \
      --title="GCP AI Caps Vertex logging reader" \
      --description="Read Gemini Platform inference request logging state (GCP AI Usage Caps API)" \
      --permissions="$VERTEX_READER_PERMISSION" --stage=GA --quiet \
      || run_quiet gcloud iam roles undelete "$VERTEX_READER_ROLE_ID" --project="$project" --quiet \
      || { warn "could not create custom role ${VERTEX_READER_ROLE_ID} on ${project} (needs iam.roles.create); the vertex logging status shows as unreadable"; return 0; }
  fi
  grant_project_role "$project" "$member" "projects/${project}/roles/${VERTEX_READER_ROLE_ID}"
}

# ensure_vertex_reader_org_role ORG: organization-level variant for folder/organization scope grants; sets
# VERTEX_READER_ORG_ROLE (empty when the role cannot be created).
ensure_vertex_reader_org_role() {
  local org="$1"
  VERTEX_READER_ORG_ROLE=""
  if ! probe gcloud iam roles describe "$VERTEX_READER_ROLE_ID" --organization="$org"; then
    run_quiet gcloud iam roles create "$VERTEX_READER_ROLE_ID" --organization="$org" \
      --title="GCP AI Caps Vertex logging reader" \
      --description="Read Gemini Platform inference request logging state (GCP AI Usage Caps API)" \
      --permissions="$VERTEX_READER_PERMISSION" --stage=GA --quiet \
      || { warn "could not create organization custom role ${VERTEX_READER_ROLE_ID} on ${org} (needs roles/iam.organizationRoleAdmin)"; return 0; }
  fi
  VERTEX_READER_ORG_ROLE="organizations/${org}/roles/${VERTEX_READER_ROLE_ID}"
}
