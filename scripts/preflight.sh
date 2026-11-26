#!/usr/bin/env bash
# Environment readiness preflight (read-only): checks projects, APIs, organization policies, VPC Service Controls,
# network, BigQuery/logging (incl. adoptable datasets and sinks), AI surfaces, IAM, enforcement state and quotas for
# the hub and every managed project, and prints a remediation script.
#
#   scripts/preflight.sh --project HUB [--managed-projects A,B] [--scope-type folder --scope-ids 123] \
#       [--region R] [--dataset D] [--audit-project P] [--vpc-network N --vpc-subnet S] [--format text|json|markdown|env]
#
# Exit codes: 0 ready, 1 warnings (0 with --no-strict), 2 blocked, 3 could not run.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v python3 >/dev/null || { echo "error: python3 is required" >&2; exit 3; }
export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m usage_caps_api.preflight "$@"
