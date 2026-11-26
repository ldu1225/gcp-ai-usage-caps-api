#!/usr/bin/env bash
# Offline test runner: unit tests + shell syntax checks + OpenAPI presence verification.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> [1/3] Running offline unit tests"
PYTHONPATH="$ROOT/src" python3 -m unittest discover -s tests/unit -v

echo "==> [2/3] Checking shell script syntax (bash -n)"
for f in scripts/install.sh scripts/prepare.sh scripts/deploy.sh scripts/teardown.sh scripts/test.sh; do
  bash -n "$f"
done
echo "    OK: all shell scripts parsed cleanly"

echo "==> [3/3] Verifying OpenAPI spec file"
test -s openapi.yaml
grep -q "openapi: 3.1.0" openapi.yaml
echo "    OK: openapi.yaml present (OpenAPI 3.1.0)"
echo "==> ALL OFFLINE CHECKS PASSED"
