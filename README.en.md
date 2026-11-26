# GCP AI Usage & Spend Caps API

> **한국어 문서**: [README.md](./README.md) | **OpenAPI 3.1 Spec**: [`openapi.yaml`](./openapi.yaml) (`GET /v1/openapi.yaml`)

A lightweight, **Headless (no-UI) M2M REST API server** built for enterprises that already operate an internal AI management portal (integrated with OpenAI / Anthropic Claude) and need clean programmatic access to Google Cloud AI (**Antigravity**, **Vertex AI / Agent Platform**, **Gemini Enterprise**):

1. **Per-User Usage, Token & Cost Reporting API** (with monthly user/project integer-cent chargeback)
2. **Per-User & Per-Project Spend Cap Management and Auto/Manual Block & Unblock API**

---

## Architecture

```mermaid
flowchart LR
    Portal["Enterprise AI Management Portal<br/>(OpenAI / Claude integrated)"] -->|Bearer API Key / OIDC| API["Cloud Run<br/>Headless API (/v1/...)"]
    API -->|Outbound Webhook<br/>HMAC-SHA256| Portal

    subgraph GCP["Google Cloud Platform"]
        Sink["Cloud Logging Sink<br/>(Antigravity / Vertex AI / GE)"] --> BQ["BigQuery<br/>Audit Dataset"]
        Sink -->|Real-time inference logs| PS["Pub/Sub Push<br/>(/v1/internal/usage-event)"]
        PS -->|Google OIDC| API
        API --> BQ
        API --> Billing["Billing Export &<br/>Catalog Pricing"]
        API --> GCS["Private GCS Bucket<br/>(policy / caps / hashed keys)"]
        API -->|Immediate / over-cap trigger| Job["Cloud Run Enforcer Job<br/>(Dedicated SA: iam.denyAdmin)"]
        Sched["Cloud Scheduler<br/>(5-min safety net & period reset)"] --> Job
        Job -->|IAM Deny v2<br/>Per-service rules| IAM["Org / Folder / Project<br/>IAM Deny Policy"]
        API -->|Project hard caps| Budget["Cloud Billing Budget API v1<br/>(Spend Cap)"]
    end
```

### Design Principles
- **Headless & Ultra-Light**: No web UI, AI Analyst, Model Armor, NotebookLM, or Agent Registry. Just 5 Python dependencies, non-root slim image, sub-second cold start.
- **Zero External Database**: Uses a private, versioned GCS bucket with `ifGenerationMatch` optimistic concurrency for multi-instance Cloud Run consistency (and atomic local files / memory for dev & tests).
- **Strict Privilege Separation**: The API service account only reads policies (`roles/iam.denyReviewer`); organization-level `roles/iam.denyAdmin` is granted exclusively to the isolated **Cloud Run Enforcer Job** service account.

---

## Key Differences vs OpenAI / Anthropic Admin APIs

| Dimension | OpenAI / Anthropic Admin API | Google Cloud AI (This API) |
|---|---|---|
| **Blocking Mechanism** | Immediate API key revocation (ms latency) | **IAM Deny v2** policy enforcement. Blocks access across groups, domains, inherited roles, and Workforce Identity pools, with **~1-2 minute eventual consistency propagation** (in-flight requests finish). |
| **Per-Service Selective Block** | Key-level all-or-nothing | Up to 3 clean rules inside one managed policy (`gcp-ai-caps-*`) allowing per-user selective blocking of **`antigravity`**, **`vertex_ai`**, and **`gemini_enterprise`**. |
| **Per-User Cost Estimation** | Provider billing API reports per-key spend | GCP Billing Export has no user dimension. Per-user cost is computed from observed per-model tokens (uncached input, cached input, output/thinking) using `Billing Export effective rate -> Official Catalog -> Manual overrides`, with **integer-cent allocation** ensuring zero rounding drift in monthly chargeback. |
| **Real-Time Cap Detection** | Synchronous gateway enforcement | **Pub/Sub Push (`/v1/internal/usage-event`)** triggers enforcement within seconds + **Cloud Scheduler (5-min)** safety net + **automatic restore** on settlement period reset. |
| **Project Hard Spend Caps** | Instant workspace pause/resume | **Cloud Billing Budget API v1 Spend Cap** (USD billing accounts, Agent Platform). Lifting an `ENFORCED` cap via `POST /v1/caps/projects/{id}/lift` takes up to **~1 hour** for Google Cloud to resume traffic. |

---

## Safety Gates (G1–G7)

Every enforcement cycle evaluates seven safety gates before modifying IAM Deny v2 policies:

- **G1_POLICY_READABLE**: Verifies managed deny policies are readable and owned (aborts if unreadable or modified externally as a foreign policy).
- **G2_ANY_DETERMINATE**: Ensures determinate user classification when population > 0 (aborts on failure).
- **G3_TELEMETRY_HEALTHY**: Withholds usage-based auto-restores if telemetry is missing or truncated (expired period-reset restores always proceed).
- **G4_CHANGE_BUDGET**: Bounds new suspensions per run (`max(min, ratio * population)`), deferring excess blocks to subsequent cycles (restores are never throttled).
- **G5_NON_USER_GRANT_ABSENT**: Confirms group/domain/inherited grant coverage under `iam_deny`.
- **G6_DENY_CAPACITY**: Enforces IAM's 2,500 denied-principal limit per resource (warns at 2,000, holds excess least-severe suspensions).
- **G7_SURGE_GUARD**: Holds mass automatic suspensions (>= 10 users AND >= 50% of population) for review against telemetry/pricing anomalies.

---

## Authentication (M2M Only)

1. **Scoped API Keys (`Authorization: Bearer gcpai_<id>_<secret>`)**
   - Plaintext keys are shown once on creation and **never stored** (salted SHA-256 hash + constant-time `hmac.compare_digest`).
   - Granular scopes: `usage:read`, `caps:read`, `caps:write`, `users:write`, `admin`.
2. **GCP Service Account OIDC (`Authorization: Bearer <RS256 JWT>`)**
   - Verifies Google-signed RS256 OIDC ID tokens (`accounts.google.com`, `email_verified=true`, audience matching `PUBLIC_BASE_URL`) for Cloud Scheduler, Pub/Sub push, and allowlisted internal GCP service accounts.

---

## API Endpoints (`/v1/...`)

See [`openapi.yaml`](./openapi.yaml) or `GET /v1/openapi.yaml` for the full OpenAPI 3.1 contract.

| Method & Path | Scope | Description |
|---|---|---|
| `GET /v1/health` | Public | Liveness & authenticated runtime/enforcement status |
| `GET /v1/openapi.yaml` | Public | Serve OpenAPI 3.1 YAML specification |
| `GET /v1/usage/users` | `usage:read` | Per-user requests, input/output/cached tokens, per-model breakdown & estimated cost (`start`, `end`, `granularity`, `project`, `surface`, `email`, `limit`, `cursor`) |
| `GET /v1/usage/users/{email}` | `usage:read` | Single-user detailed usage + daily/weekly time series + active cap burn rate |
| `GET /v1/billing/users?month=YYYY-MM` | `usage:read` | Monthly user & project chargeback report (exact cent-level totals) |
| `GET /v1/caps/policy`, `PUT /v1/caps/policy` | `caps:read` / `caps:write` | View or update global default cap policy |
| `GET /v1/caps/users`, `GET /v1/caps/users/{email}` | `caps:read` | Per-user limits, usage, remaining, utilization%, status (`active\|over_cap\|suspended\|blocked`) |
| `PUT /v1/caps/users/{email}`, `DELETE /v1/caps/users/{email}` | `caps:write` | Set or remove individual user Spend Cap / allow / block overrides |
| `POST /v1/users/{email}/block`, `POST /v1/users/{email}/unblock` | `users:write` | Immediately block/unblock a user on selectable services (`antigravity`, `vertex_ai`, `gemini_enterprise`) |
| `POST /v1/caps/enforce` | `caps:write` / OIDC | Evaluate policy (G1–G7) and enforce changes (`{"dryRun": true\|false}`) |
| `GET /v1/caps/projects`, `PUT /v1/caps/projects/{project_id}` | `caps:read` / `caps:write` | View or configure project-level monthly Google Cloud Spend Caps |
| `POST /v1/caps/projects/{project_id}/lift` | `caps:write` | Lift an `ENFORCED` project Spend Cap for the remainder of the month |
| `GET /v1/audit-logs` | `caps:read` / `admin` | Query governance & block/unblock audit trail |
| `POST /v1/internal/usage-event` | OIDC / `admin` | Pub/Sub push receiver for real-time inference logs |
| `POST /v1/internal/pricing/sync` | OIDC / `admin` | Sync official Cloud Billing Catalog prices & FX rates |
| `GET/POST /v1/admin/api-keys`, `DELETE /v1/admin/api-keys/{id}` | `admin` | Issue, list, and revoke scoped M2M API keys |
| `GET/POST /v1/admin/webhooks`, `DELETE /v1/admin/webhooks/{id}` | `admin` | Manage HMAC-SHA256 signed outbound webhooks |

---

## One-Click Deployment

### Option 1: One-Click Installer (Cloud Shell)

```bash
bash <(curl -fsSL https://github.com/ldu1225/gcp-ai-usage-caps-api/raw/main/scripts/install.sh) \
  --project YOUR_PROJECT_ID --region asia-northeast3

# Multi-project hub with organization IAM Deny v2 roles & auto enforcement
bash <(curl -fsSL https://github.com/ldu1225/gcp-ai-usage-caps-api/raw/main/scripts/install.sh) \
  --project HUB_PROJECT --link-projects PROJ_B,PROJ_C \
  --org-id 123456789012 --grant-org-roles --mode auto
```

### Option 2: Terraform

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
terraform init && terraform plan && terraform apply
```

### CLI Key Management

```bash
PYTHONPATH=src python3 -m usage_caps_api.cli keys create \
  --name "enterprise-ai-portal" \
  --scopes "usage:read,caps:read,caps:write,users:write"
```

---

## Integration Examples (`curl` & Python)

### `curl`

```bash
export API_URL="https://gcp-ai-usage-caps-api-xxxx.asia-northeast3.run.app"
export API_KEY="gcpai_xxxxxxxxxxxx_your_secret_key"

# Per-user usage & cost
curl -sS -H "Authorization: Bearer ${API_KEY}" \
  "${API_URL}/v1/usage/users?start=2026-11-01&end=2026-11-26" | jq .

# Set global monthly cap ($100/user, auto mode)
curl -sS -X PUT -H "Authorization: Bearer ${API_KEY}" -H "Content-Type: application/json" \
  "${API_URL}/v1/caps/policy" \
  -d '{"cost_cap": 100.0, "currency": "USD", "period": "monthly", "enforcement_mode": "auto"}' | jq .

# Block user on Antigravity & Vertex AI
curl -sS -X POST -H "Authorization: Bearer ${API_KEY}" -H "Content-Type: application/json" \
  "${API_URL}/v1/users/bob@company.com/block" \
  -d '{"services": ["antigravity", "vertex_ai"], "reason": "Portal budget exceeded"}' | jq .

# Unblock user
curl -sS -X POST -H "Authorization: Bearer ${API_KEY}" -H "Content-Type: application/json" \
  "${API_URL}/v1/users/bob@company.com/unblock" -d '{}' | jq .
```

### Python + Webhook Signature Verification

```python
import hashlib
import hmac
import requests

API_URL = "https://gcp-ai-usage-caps-api-xxxx.asia-northeast3.run.app"
HEADERS = {"Authorization": "Bearer gcpai_xxxxxxxxxxxx_your_secret_key"}

# Query monthly user/project chargeback
report = requests.get(f"{API_URL}/v1/billing/users", headers=HEADERS, params={"month": "2026-11"}, timeout=15).json()
print("Total chargeable:", report["totals"]["chargeable"], report["currency"])

# Verify outbound webhook X-Signature-256 header
def verify_webhook(secret: str, timestamp: str, raw_body: bytes, signature: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("ascii") + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
```

---

## Offline Unit Tests

```bash
./scripts/test.sh
# or
PYTHONPATH=src python3 -m unittest discover -s tests/unit -v
```
