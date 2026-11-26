#!/usr/bin/env bash
# Offline test runner: unit tests + shell syntax checks + OpenAPI presence verification.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> [1/3] Running offline unit tests"
PYTHONPATH="$ROOT/src" python3 -m unittest discover -s tests/unit -v

echo "==> [2/3] Checking shell script syntax (bash -n)"
for f in scripts/*.sh scripts/lib/*.sh; do
  bash -n "$f"
done
echo "    OK: all shell scripts parsed cleanly"

echo "==> [3/3] Verifying OpenAPI spec file"
test -s openapi.yaml
grep -q "openapi: 3.1.0" openapi.yaml
if python3 -c "import openapi_spec_validator, yaml" 2>/dev/null; then
  python3 -c "from openapi_spec_validator import validate; from openapi_spec_validator.readers import read_from_filename; validate(read_from_filename('openapi.yaml')[0])"
  echo "    OK: openapi.yaml is a valid OpenAPI 3.1 document (openapi-spec-validator)"
else
  echo "    OK: openapi.yaml present (install requirements-dev.txt for full schema validation)"
fi
python3 -m compileall -q src >/dev/null
echo "    OK: all Python modules compile"
echo "==> ALL OFFLINE CHECKS PASSED"
