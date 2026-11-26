<p align="center">
  <img src="docs/assets/banner.svg" alt="GCP AI Usage &amp; Spend Caps API" width="100%">
</p>

<p align="center">
  <a href="openapi.yaml"><img alt="OpenAPI 3.1" src="https://img.shields.io/badge/OpenAPI-3.1-0B1F3A"></a>
  <img alt="API 1.3.0" src="https://img.shields.io/badge/API-1.3.0-1A73E8">
  <img alt="Python 3.12" src="https://img.shields.io/badge/Python-3.12-0B1F3A">
  <img alt="Cloud Run" src="https://img.shields.io/badge/runtime-Cloud%20Run-1A73E8">
  <img alt="Terraform" src="https://img.shields.io/badge/IaC-Terraform-0B1F3A">
  <img alt="Tests" src="https://img.shields.io/badge/tests-offline-34A853">
  <img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-0B1F3A">
</p>

<p align="center"><a href="README.md">한국어</a> | <b>English</b></p>

# GCP AI Usage & Spend Caps API

**GCP AI Usage & Spend Caps API** is a headless, machine-to-machine admin API that **measures, caps and enforces** the use of Google Cloud **Antigravity**, **Gemini Platform (Gemini Enterprise Agent Platform, formerly Vertex AI)** and **Gemini Enterprise** **per user, per group and per project**. Install it once in a **hub (main) project** and it governs every managed project; an existing enterprise AI portal only needs REST calls and a webhook receiver to govern Google Cloud AI the same way it governs other AI services. Cost is computed from token counts in logs multiplied by official SKU prices, so no billing data access is required, and blocks are enforced with IAM Deny v2 so they cannot be bypassed.

> [!IMPORTANT]
> **Cost = token counts from logs x official Cloud Billing Catalog SKU prices.** The Cloud Billing export to BigQuery is NOT used and NOT required. Amounts are list-price (or contract-price) estimates; credits, committed-use discounts and taxes are excluded. See [Cost methodology](docs/cost-methodology.md).

> [!NOTE]
> **There is no built-in model list.** The catalog is live Model Garden plus models actually observed in logs. Notations such as `<model-id>` and `<publisher>/*` in this document are placeholders; always use the IDs returned by `GET /v1/models`.

---

## Table of contents

1. [Key capabilities](#1-key-capabilities)
2. [Architecture](#2-architecture)
3. [How enforcement actually works](#3-how-enforcement-actually-works)
4. [Groups](#4-groups)
5. [Multi-project users](#5-multi-project-users)
6. [Cost methodology](#6-cost-methodology)
7. [Gemini Platform per-user attribution](#7-gemini-platform-per-user-attribution)
8. [Model governance](#8-model-governance)
9. [Rate limits and quotas](#9-rate-limits-and-quotas)
10. [Webhooks](#10-webhooks)
11. [Environment preflight](#11-environment-preflight)
12. [Installation (one-click, Terraform)](#12-installation-one-click-terraform)
13. [API quick start (curl, Python)](#13-api-quick-start-curl-python)
14. [Full endpoint reference](#14-full-endpoint-reference)
15. [Authentication and scopes](#15-authentication-and-scopes)
16. [Differences from typical AI vendor admin APIs](#16-differences-from-typical-ai-vendor-admin-apis)
17. [Limitations](#17-limitations)
18. [Development and tests](#18-development-and-tests)
19. [Documentation](#19-documentation)

---

## 1. Key capabilities

| Area | Capabilities | What the portal gets |
|---|---|---|
| **Usage and cost** | Requests, input / output / cached / thinking tokens and estimated cost per user, group, project, model and traffic tier; monthly chargeback; unattributed usage reported separately | Per-user usage reports and departmental cost allocation |
| **Spend caps** | User caps, **group caps (shared pool or per member)**, global policy, project hard caps. Cost, tokens or requests; total or per project; automatic period reset | Automatic suspension on overspend (only in the over-cap project) |
| **Block / unblock** | User and group blocks via IAM Deny v2, per service (`antigravity`, `gemini_platform`, `gemini_enterprise`) and per project, optional expiry | Immediate, non-bypassable blocking during incidents |
| **Model governance** | Dynamic model catalog (Model Garden plus observed models), allow / deny per project, folder or organization (Org Policy `vertexai.allowedModels`), Gemini Enterprise app model settings | Policies such as "this team may only use Google models" |
| **User and group model rules** | Allow / deny lists, per-model RPM, TPM, token and cost limits. **Block first, always notify** | Violations blocked in the affected project, plus a webhook |
| **Rate limits and quotas** | RPM / TPM limits, usage and headroom per model and region, decreases, **quota increase requests submitted via API** with status tracking | Quota shortages handled from the portal |
| **Environment preflight** | VPC-SC, Org Policy, enabled APIs, reuse of existing BigQuery datasets and log sinks, presence of Gemini Enterprise apps, IAM, Deny capacity and more | Safe installation in any customer environment |
| **Multi-project hub** | One hub governs a project list, a folder or an organization; users and projects are N:M | Central operations with per-project limits |
| **Groups** | Policy subjects with the same capabilities as users, Google Group sync, exclusions, priority | Policies that follow the org chart |
| **Webhooks** | 20 event types, HMAC-SHA256 signed | Portal notifications and workflows |
| **Audit** | Every API call, block and policy change is audited | Compliance evidence |

**Design principles**

* **Hub-and-spoke**: one main project governs every managed project.
* **Log-based, no billing export**: usage from Cloud Logging, prices from the public Cloud Billing Catalog API.
* **Nothing hardcoded**: model lists, model families and prices are learned from Model Garden, logs and the SKU catalog.
* **Respect existing infrastructure**: existing BigQuery datasets and log sinks are adopted; only missing pieces are created.
* **Safety first**: dry runs, impact previews, lockout prevention, etag concurrency control, safety gates G1-G7, change budgets.
* **Lightweight**: standard-library Python, no UI.

## 2. Architecture

<p align="center"><img src="docs/assets/architecture.svg" alt="Hub-and-spoke architecture: Cloud Run API in the hub project, logs from managed projects, aggregated sink, BigQuery, Pub/Sub, IAM Deny v2" width="100%"></p>

| Component | Location | Role |
|---|---|---|
| Cloud Run API | Hub | REST `/v1`, authentication and scopes, usage queries, policy management, real-time event intake (`/v1/internal/usage-event`) |
| Enforcer (Cloud Run Job) | Hub | Cap evaluation, safety gates, IAM Deny updates, period-reset restores, hourly Google Group sync |
| Cloud Scheduler | Hub | 5-minute enforcement safety net, daily price sync (OIDC) |
| Pub/Sub | Hub | Pushes inference logs from the aggregated sink to the API within seconds |
| BigQuery | Hub | Central dataset of managed-project logs (history) |
| Cloud Storage | Hub | State store (caps, groups, rules, audit, API key hashes) |
| Secret Manager | Hub | Bootstrap admin API key |
| Aggregated log sink | Project, folder or organization | Routes Antigravity, Gemini Platform (`aiplatform.googleapis.com/inference_request`, Data Access audit logs) and Gemini Enterprise logs to the hub |
| Cloud Billing Catalog API | Google | Official SKU list prices (daily). **Billing Export is not used** |
| IAM Deny v2 | Project, folder or organization | Enforcement through one managed deny policy per attachment point |
| Org Policy | Project, folder or organization | Model allow / deny with `vertexai.allowedModels` |
| Cloud Quotas API | Managed projects | Limit queries, decreases and increase requests |
| Cloud Identity | Google Workspace / Cloud Identity | Google Group membership sync |
| Webhooks | Customer portal | HMAC-SHA256 signed event delivery |

See [docs/architecture.md](docs/architecture.md) for components and data flows, and [docs/multi-project-deployment.md](docs/multi-project-deployment.md) for multi-project installation.

## 3. How enforcement actually works

<p align="center"><img src="docs/assets/enforcement-flow.svg" alt="Enforcement flow: inference log, detection, pricing, policy resolution, safety gates, IAM Deny v2, 403, restore at period reset" width="100%"></p>

1. **Log-based detection**: every inference request is logged in the managed project's Cloud Logging; the aggregated sink forwards it to Pub/Sub in the hub (seconds) and to BigQuery. If Pub/Sub is interrupted, Cloud Scheduler re-evaluates everything **every 5 minutes** (safety net).
2. **Pricing**: token counts x official Cloud Billing Catalog SKU prices (input, output, cache, long context, traffic tiers). **The Cloud Billing export is not used.**
3. **Policy resolution**: the effective rule is determined per person: user rule > group rule (by priority) > global policy. Group shared pools are evaluated **in addition** to personal caps ([section 4](#4-groups)).
4. **Safety gates G1-G7**: deny policies readable, decidable usage data, telemetry health, change budget, Deny capacity, surge guard. If any gate signals risk, automatic blocking is halted or deferred.
5. **IAM Deny v2 write**: the user principal is added to one managed deny policy per attachment point (managed project, or folder / organization). If only one project is over its cap, only that project is affected.
6. **Propagation and 403**: IAM Deny typically propagates within **1-2 minutes**; subsequent requests on that service and project receive 403. In-flight requests complete.
7. **Notifications**: `cap.threshold_reached` / `cap.exceeded` at 80% / 100% (`group.cap_*` for group pools), `user.blocked` / `group.blocked` on block.
8. **Automatic restore**: at the next period reset (daily / weekly / monthly) deny entries are removed automatically and `user.unblocked` is sent. Manual blocks and suspensions for other reasons are kept.

With `enforcement_mode: auto` users are suspended immediately; with `manual` only a review plan (`plan`) and notifications are produced. See [docs/enforcement-logic.md](docs/enforcement-logic.md).

## 4. Groups

Groups are first-class policy subjects with **the same capabilities as users** (spend caps, blocks, model rules, exceptions, usage roll-up). A group's **effective members** = explicit `members` + (optionally) members synced from a Google Group - `excluded` people.

<p align="center"><img src="docs/assets/policy-resolution.svg" alt="Policy resolution order: user rule, group rules by priority, global policy. Exclusions, user-level allow, shared pools" width="100%"></p>

### 4.1 Semantics

| Topic | Behavior |
|---|---|
| **Resolution order** | User rule > group rule (lowest `priority` first, then group id) > global policy / default rule |
| **Priority** | `priority` 0-100000, default 100, **lower wins**. A person in several groups follows the first group that has a rule |
| **Exclusions (exceptions)** | People listed in `excluded` are removed from the group's effective members; the group policy never applies to them |
| **User-level allow** | A user `type=allow` rule overrides group blocks and shared-pool suspensions |
| **Per-member caps** (`pool=per_member`, default) | Each member inherits the group cap like a personal rule (when they have no user rule) |
| **Shared pool** (`pool=shared`) | One cap on the pooled usage of all effective members, evaluated **in addition** to each member's own cap. At 100% every member without a user-level allow is suspended (only in the over-cap projects for `scope=per_project` or `project_caps`) |
| **Group block** | `POST /v1/groups/{group_id}/block` - all or selected managed projects, services, optional expiry. Members with their own personal block keep it |
| **Group model rules** | Same body as user model rules. Resolution: user > group > default rule |
| **Effect of member removal** | Group policies are never copied into user records; they are derived on every evaluation. Removing a member, adding an exclusion or deleting a group rule takes effect **immediately**, and suspensions that existed only because of the group are lifted. Personal blocks and personal cap suspensions are kept |
| **Google Group sync** | Members (including nested groups) are synced hourly through the Cloud Identity API; on demand with `POST /v1/groups/{group_id}/sync`. Service accounts are filtered out |

> [!IMPORTANT]
> Google Group sync requires the **Cloud Identity API** (`cloudidentity.googleapis.com`) enabled in the hub project and the **Groups Reader** admin role granted to the runtime service account in the Google Admin console (Account > Admin roles). On failure the group's `sync_error` field records the reason.

User cap responses (`GET /v1/caps/users/{email}`) include `rule_source` (`user` | `group:<id>` | `policy`), `groups`, `group_pools` and `block_source` (`manual` | `group:<id>` | `rule` | `auto_cap`), so the portal can always show who blocked a person and why.

### 4.2 Group API examples

```bash
# 1) Create a group (explicit members + Google Group sync, priority 10)
curl -s -X POST "$BASE/v1/groups" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"id":"eng","name":"Engineering","priority":10,
       "members":["alice@company.com","bob@company.com"],
       "google_group":"eng-all@company.com"}'

# 2) Add / remove members
curl -s -X POST "$BASE/v1/groups/eng/members" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"add":["carol@company.com"],"remove":["bob@company.com"]}'

# 3) Add an exclusion - the eng group policy never applies to this person
curl -s -X POST "$BASE/v1/groups/eng/exclusions" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"add":["cto@company.com"]}'

# 4-a) Shared-pool group cap: $5,000 per month for all of eng combined
curl -s -X PUT "$BASE/v1/caps/groups/eng" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"type":"limit","pool":"shared","cost_cap":5000,"scope":"total","note":"Q4 eng budget"}'

# 4-b) Per-member cap: $50 per month for each intern, applied per project
curl -s -X PUT "$BASE/v1/caps/groups/interns" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"type":"limit","pool":"per_member","cost_cap":50,"scope":"per_project"}'

# 5) Group cap status (pool usage, remaining budget, member status)
curl -s "$BASE/v1/caps/groups" -H "Authorization: Bearer $KEY"

# 6) Block a group (Gemini Platform in team-b-prod only, with expiry) / unblock
curl -s -X POST "$BASE/v1/groups/interns/block" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"services":["gemini_platform"],"projects":["team-b-prod"],"reason":"security review","until":"2026-12-01T00:00:00Z"}'
curl -s -X POST "$BASE/v1/groups/interns/unblock" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"reason":"review complete"}'

# 7) Group model rule: interns may only use Google models (block + notify on violation)
curl -s -X PUT "$BASE/v1/groups/interns/model-rules" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"mode":"allow_list","models":["google/*"]}'

# 8) Usage per group (date range)
curl -s "$BASE/v1/usage/groups?start=2026-11-01&end=2026-11-30" -H "Authorization: Bearer $KEY"

# 9) Groups of one person and the source of each effective policy
curl -s "$BASE/v1/users/alice%40company.com/groups" -H "Authorization: Bearer $KEY"

# 10) Sync the Google Group now
curl -s -X POST "$BASE/v1/groups/eng/sync" -H "Authorization: Bearer $KEY"
```

The `members_still_blocked` field of `POST /v1/groups/{group_id}/unblock` lists members that remain blocked for other reasons, such as a personal block. See the [operations runbook](docs/operations-runbook.md) and the [enforcement engine](docs/enforcement-logic.md).

## 5. Multi-project users

<p align="center"><img src="docs/assets/multi-project.svg" alt="One user assigned to several projects: per-project usage and caps, only the over-cap project is suspended" width="100%"></p>

Usage is always aggregated per **(user, project)**; one user can be assigned to several managed projects (N:M).

| Setting | Where | Meaning |
|---|---|---|
| `cap_scope: total` (default) | Policy / user and group rule `scope` | One cap across all projects |
| `cap_scope: per_project` | Policy / user and group rule `scope` | The same cap applied separately in each project |
| `project_caps` | User and group rules | `{project_id: {cost_cap, token_cap, request_cap}}` - evaluated in addition to the total cap |
| `project_defaults` | Policy | Default per-project caps for every user |
| `metered_surfaces` | Policy | Services counted toward caps (all three by default) |

Exceeding a cap in one project suspends the user **only in that project** (`plan.suspend_projects`). Exceeding the total cap, a global block or a total shared pool applies to every managed project.

```bash
# Policy: $100 per month per project for everyone; team-b-prod default cap $30
curl -s -X PUT "$BASE/v1/caps/policy" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"cost_cap":100,"period":"monthly","enforcement_mode":"auto","cap_scope":"per_project",
       "project_defaults":{"team-b-prod":{"cost_cap":30}}}'

# User rule: $200 total plus per-project limits
curl -s -X PUT "$BASE/v1/caps/users/alice%40company.com" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"type":"limit","cost_cap":200,"scope":"total",
       "project_caps":{"team-a-prod":{"cost_cap":150},"team-b-prod":{"cost_cap":50}}}'

# Every project the user works in (usage, cost, models, per-project cap status, direct IAM bindings)
curl -s "$BASE/v1/users/alice%40company.com/projects?start=2026-11-01&end=2026-11-30&include=iam" -H "Authorization: Bearer $KEY"
```

See [docs/multi-project-deployment.md](docs/multi-project-deployment.md).

## 6. Cost methodology

**Cost = token counts from logs x official Cloud Billing Catalog SKU prices** (or contract price overrides).

| SKU dimension | Tokens | Notes |
|---|---|---|
| Input | input - cache read - cache write | Includes tool-use prompts |
| Output | output + thinking tokens | Thinking tokens billed at output price |
| Cache read | Context cache hits | Falls back to input price (conservative) |
| Cache write | Partner model prompt-cache creation | Falls back to input price |
| Long context | Requests whose prompt exceeds the threshold (default 200,000) | `LONG_CONTEXT_THRESHOLD_TOKENS` |
| Flex / Priority / Batch | Requests whose log `trafficType` is that tier | Falls back to standard price |
| Provisioned Throughput | Requests served by PT | `on_demand` (default) or `zero` |

* Every usage response contains `pricing_source` (`method=log_token_counts_x_official_sku_prices`, `billing_export=not_used`, sync time). `GET /v1/pricing` shows per-model prices with **SKU resource names**.
* Gemini Enterprise is a per-seat product; only request counts are tracked (`cost_basis=license`).
* Models without a SKU are never priced by guesswork; they are marked `cost_basis=unpriced`, `cost_partial=true`.

See [docs/cost-methodology.md](docs/cost-methodology.md) for formulas, currency conversion and worked examples.

## 7. Gemini Platform per-user attribution

Gemini Platform token counts are recorded only in the **inference request metadata log** (`aiplatform.googleapis.com/inference_request`). It must be enabled per project, and the caller is usually the portal's service account, so usage is attributed to people in this order:

| Order | Method | Accuracy | `telemetry.attribution` key |
|---|---|---|---|
| 1 | Header sent by the portal: `x-goog-agent-platform-observability-label-user: <email or uid>` | Exact | `header_label` |
| 2 | Data Access audit log correlation (same project, model and minute; proportional to request counts) | Approximate | `audit_correlated` |
| 3 | Neither available: kept per project as `unattributed` (never dropped) | Exact per project | `unattributed` |

Antigravity logs carry the user identity directly and are counted as `log_identity`.

```bash
# Inference log status / enable for one project / enable for all (default dryRun=true)
curl -s "$BASE/v1/projects/team-a-prod/vertex-logging" -H "Authorization: Bearer $ADMIN"
curl -s -X POST "$BASE/v1/projects/team-a-prod/vertex-logging/enable" -H "Authorization: Bearer $ADMIN" \
  -H "Content-Type: application/json" -d '{"dryRun":false}'
curl -s -X POST "$BASE/v1/vertex-logging/enable" -H "Authorization: Bearer $ADMIN" \
  -H "Content-Type: application/json" -d '{"projects":["*"],"dryRun":false}'
```

Add the header when the portal calls Gemini Platform:

```python
import google.auth, google.auth.transport.requests, requests

PROJECT, LOCATION = "team-a-prod", "global"
MODEL_ID = "<model-id>"                      # pick from GET /v1/models
creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
creds.refresh(google.auth.transport.requests.Request())

def generate(user_email: str, prompt: str) -> dict:
    url = (f"https://aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/{LOCATION}"
           f"/publishers/google/models/{MODEL_ID}:generateContent")
    headers = {
        "Authorization": f"Bearer {creds.token}",
        # Signed-in portal user: the Caps API attributes tokens and cost to this person exactly.
        "x-goog-agent-platform-observability-label-user": user_email,
    }
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    return requests.post(url, headers=headers, json=body, timeout=60).json()

# Partner model calls (rawPredict / streamRawPredict) take the same header.
```

The header suffix can be changed with `VERTEX_USER_HEADER_LABEL` (default `user`), and the logging configuration endpoint can be overridden with `VERTEX_LOGGING_ENDPOINT`. The surface id is `gemini_platform`; the legacy id `vertex_ai` is still accepted as an alias.

## 8. Model governance

| Layer | Mechanism | API |
|---|---|---|
| Catalog | Live Model Garden plus models observed in logs; `model.first_seen` / `model.catalog_changed` webhooks | `/v1/models` |
| Project, folder, organization (Gemini Platform) | Org Policy `vertexai.allowedModels` (patterns expanded against the live catalog, 500-value check, hourly re-expansion) | `/v1/model-access/...` |
| Gemini Enterprise apps | Merge-safe PATCH of app model settings (`not_applicable` when no app exists) | `/v1/gemini-enterprise/apps/...` |
| Antigravity | Read authorized models (writes are console-only: `409 console_required`) | `/v1/antigravity/models` |
| **Users and groups** | Allow / deny lists plus per-model RPM, TPM, daily / monthly token and cost limits. On violation: **immediate service block in the affected project plus a webhook** | `/v1/model-rules/...`, `/v1/users/{email}/model-rules`, `/v1/groups/{group_id}/model-rules` |

Model rules resolve **user > group (by priority) > default rule**. Publishers are never guessed from names; policies use `<publisher>/<model-id>` or `<publisher>/*`. See [docs/model-governance.md](docs/model-governance.md).

## 9. Rate limits and quotas

* Generative AI quotas (per model and region) and 24-hour peak usage and headroom for every managed project.
* Decreases (caps) are applied immediately (`force` is required below the observed peak).
* Increases are **submitted directly to Google Cloud via the API**: single or multiple projects, `Idempotency-Key`, status tracking (`pending`, `approved`, `partially_approved`, `denied`), cancellation and webhooks.
* Per-user RPM / TPM limits (`/v1/rate-limits/users/{email}`) are detected from logs and enforced by blocking.

## 10. Webhooks

HMAC-SHA256 signatures (`X-Signature-256`), per-event deduplication, 20 event types:

| Category | Events |
|---|---|
| Caps | `cap.threshold_reached`, `cap.exceeded`, `project_cap.enforced`, `project_cap.lifted` |
| Users | `user.blocked`, `user.unblocked`, `user.model_violation` |
| Groups | `group.cap_threshold_reached`, `group.cap_exceeded`, `group.blocked`, `group.unblocked`, `group.membership_changed` |
| Models | `model.access_changed`, `model.lifecycle_notice`, `model.first_seen`, `model.catalog_changed` |
| Quotas | `rate_limit.threshold_reached`, `quota_request.created`, `quota_request.updated` |
| Environment | `readiness.degraded` |

See [docs/webhooks.md](docs/webhooks.md) for payloads and signature verification.

## 11. Environment preflight

Every customer environment is different: BigQuery may already exist, Gemini Enterprise apps may be absent, the projects may sit inside a VPC-SC perimeter. **Before anything is created**, the installer runs an exhaustive check, adapts automatically where it can, and stops with remediation commands where it cannot.

| Category | Main checks |
|---|---|
| project | Project existence and state, organization / folder hierarchy, billing link |
| apis | Required and optional APIs per hub / spoke (including the Cloud Identity API) and the features affected when disabled |
| orgpolicy | Resource locations, domain-restricted sharing, Cloud Run ingress / VPC egress, CMEK enforcement, custom constraints, current `vertexai.allowedModels` |
| vpcsc | Project perimeters, dry-run perimeters, hub-spoke perimeter mismatches (with ingress / egress rules), observed VPC-SC denials |
| network | Private Google Access, Cloud NAT (webhook egress), restricted.googleapis.com DNS |
| bigquery | Dataset existence and location, **recommendation to reuse existing AI log tables**, freshness |
| logging | Project / folder / org sinks (**reuse of existing sinks**), writer permissions, Data Access audit logs, Gemini Platform inference request logging |
| surfaces | Gemini Platform model access, **presence of Gemini Enterprise apps**, Antigravity configuration and logs |
| iam | Runtime / enforcer / deployer permissions (minimal roles and commands when missing) |
| enforcement | Existing IAM Deny policies, capacity and conflicts; Pub/Sub, Scheduler, Cloud Run state |
| quotas | Cloud Quotas API, quota request contact |

```bash
# Preflight only (creates nothing)
bash <(curl -fsSL https://github.com/ldu1225/gcp-ai-usage-caps-api/raw/main/scripts/install.sh) \
  --preflight-only --project HUB_PROJECT --managed-projects TEAM_A,TEAM_B

# While running
curl -s "$BASE/v1/admin/readiness?refresh=true" -H "Authorization: Bearer $ADMIN"
```

See [docs/preflight-checklist.md](docs/preflight-checklist.md) for every check.

## 12. Installation (one-click, Terraform)

### 12.1 One-click (Cloud Shell recommended)

```bash
# Hub governing several projects (central BigQuery, real-time Pub/Sub, org roles, Data Access logs)
bash <(curl -fsSL https://github.com/ldu1225/gcp-ai-usage-caps-api/raw/main/scripts/install.sh) \
  --project HUB_PROJECT \
  --managed-projects TEAM_A_PROJECT,TEAM_B_PROJECT \
  --org-id 123456789012 --grant-org-roles \
  --enable-data-access-logs \
  --quota-contact-email cloud-admin@company.com

# Govern every project under a folder (aggregated sink)
bash <(curl -fsSL https://github.com/ldu1225/gcp-ai-usage-caps-api/raw/main/scripts/install.sh) \
  --project HUB_PROJECT --scope-type folder --scope-ids 987654321 --aggregated-sink --grant-org-roles

# Reuse an existing BigQuery dataset / sinks
... --reuse-dataset LOGGING_PROJECT:existing_ai_logs --reuse-sinks
```

Install order: **1. Preflight**, **2. prepare** (APIs, dataset, sinks, audit logs, Gemini Platform inference request logging), **3. deploy** (service accounts, Cloud Run, Job, Pub/Sub, Scheduler, Secret). The installer prints the API URL and the bootstrap admin key (stored in Secret Manager). Skip inference logging with `--no-vertex-inference-logging`.

> [!WARNING]
> `roles/orgpolicy.policyAdmin` (model policy writes) and `roles/iam.denyAdmin` (blocking) **can only be granted at the organization level.** Use `--grant-org-roles` or hand the printed commands to an organization administrator. Google Group sync additionally needs the Groups Reader role in the Google Admin console ([section 4](#4-groups)).

### 12.2 Terraform

```bash
cd terraform && cp terraform.tfvars.example terraform.tfvars   # project_id, managed_projects, org_id ...
terraform init && terraform apply
```

Main variables: `managed_projects`, `central_dataset`, `reuse_dataset`, `enable_data_access_logs`, `enable_vertex_inference_logging`, `vpc_network` / `vpc_subnet` / `vpc_egress`, `quota_contact_email`, `model_violation_action`, `pricing_sync_schedule` (default `0 3 * * *`).

### 12.3 Main environment variables

| Variable | Default | Meaning |
|---|---|---|
| `CAP_SCOPE` | `total` | `total` or `per_project` |
| `METERED_SURFACES` | all three | Services counted toward caps (`antigravity`, `gemini_platform`, `gemini_enterprise`) |
| `VERTEX_USER_HEADER_LABEL` | `user` | Gemini Platform user header label suffix |
| `VERTEX_LOGGING_ENDPOINT` | `https://aiplatform.googleapis.com/v1beta1` | Override of the inference-logging configuration endpoint (`https://host[:port]/v1beta1`) |
| `MODEL_VIOLATION_BLOCK_SCOPE` | `project` | Model-rule violation block scope: `project` or `all` |
| `MODEL_PUBLISHERS` | `google` plus partner publishers | **Publisher list** queried in Model Garden (not a model list) |
| `LONG_CONTEXT_THRESHOLD_TOKENS` | `200000` | Long-context tier threshold |
| `PROVISIONED_THROUGHPUT_COST` | `on_demand` | `on_demand` or `zero` |
| `CAP_CURRENCY` | `USD` | Cap and cost currency (converted with Catalog exchange rates) |
| `SERVICE_ACCOUNT_SUBJECTS` | `false` | Treat service accounts as policy subjects (usage attribution, caps, rules, IAM Deny as `principal://iam.googleapis.com/projects/-/serviceAccounts/EMAIL`). Hub runtime and enforcer SAs are always protected |

### 12.4 Uninstall

`scripts/teardown.sh --project HUB_PROJECT --managed-projects ...` (cleans up sinks in every project; `--keep-log-sinks` keeps them)

## 13. API quick start (curl, Python)

```bash
BASE=https://gcp-ai-usage-caps-api-XXXX.asia-northeast3.run.app
ADMIN=gcpai_...   # bootstrap admin key (Secret Manager)

# Least-privilege key for the portal (plaintext returned once; only a hash is stored)
curl -s -X POST "$BASE/v1/admin/api-keys" -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"name":"ai-portal","scopes":["usage:read","caps:read","caps:write","users:write","models:read","models:write","quotas:read","quotas:write"]}'
KEY=gcpai_...     # key from the response above

# Usage per user (this month)
curl -s "$BASE/v1/usage/users?start=2026-11-01&end=2026-11-30&limit=50" -H "Authorization: Bearer $KEY"

# Cost evidence: official SKU price table
curl -s "$BASE/v1/pricing" -H "Authorization: Bearer $KEY"

# Global cap $100 per month, automatic enforcement
curl -s -X PUT "$BASE/v1/caps/policy" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"cost_cap":100,"period":"monthly","enforcement_mode":"auto"}'

# Immediate block (Antigravity only, all projects)
curl -s -X POST "$BASE/v1/users/alice%40company.com/block" -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" -d '{"services":["antigravity"],"reason":"security review"}'

# All managed projects: deny a partner publisher's models (preview first)
curl -s -X POST "$BASE/v1/model-access/bulk-apply" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"scopes":["projects/*"],"mode":"deny_list","models":["partner-publisher/*"],"dryRun":true}'

# Quota increase request (submitted to Google Cloud immediately)
curl -s -X POST "$BASE/v1/quota-requests" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"project_id":"team-a-prod","quota_id":"GenerateContentRequestsPerMinutePerProjectPerBaseModel","model":"<model-id>","location":"us-central1","requested_value":5000,"justification":"Coding assistant rollout","contact_email":"cloud-admin@company.com"}'
```

```python
from caps_client import CapsClient            # examples/python/caps_client.py (standard library only)

c = CapsClient(BASE, api_key=KEY)
top = c.list_user_usage(start="2026-11-01", end="2026-11-30")[:10]
c.set_user_model_rule("intern@company.com", mode="deny_list", models=["partner-publisher/*"])
print(c.pricing()["pricing_source"])          # {"method": "log_token_counts_x_official_sku_prices", "billing_export": "not_used", ...}

# Groups: use the generic request() method
c.request("POST", "/v1/groups", body={"id": "eng", "priority": 10, "members": ["alice@company.com"]})
c.request("PUT", "/v1/caps/groups/eng", body={"type": "limit", "pool": "shared", "cost_cap": 5000})
print(c.request("GET", "/v1/users/alice%40company.com/groups")["effective_cap_rule_source"])
```

Error format: `{"error": {"type": "invalid_request_error", "code": "lockout_prevented", "message": "..."}}` (the same structure as widely used admin API error formats)

## 14. Full endpoint reference

Full specification: [`openapi.yaml`](openapi.yaml) (OpenAPI 3.1, API version 1.3.0, also served at `GET /v1/openapi.yaml`)

| Area | Method | Path | Scope |
|---|---|---|---|
| System | GET | `/v1/health` | Public (details when authenticated) |
| System | GET | `/v1/openapi.yaml` | Public |
| Usage | GET | `/v1/usage/users` | `usage:read` |
| Usage | GET | `/v1/usage/users/{email}` | `usage:read` |
| Usage | GET | `/v1/users/{email}/projects` | `usage:read` |
| Usage | GET | `/v1/billing/users` | `usage:read` |
| Usage | GET | `/v1/usage/models` | `usage:read` or `models:read` |
| Usage | GET | `/v1/pricing` | `usage:read` or `models:read` |
| Cap policy | GET, PUT | `/v1/caps/policy` | `caps:read` / `caps:write` |
| User caps | GET | `/v1/caps/users` | `caps:read` |
| User caps | GET, PUT, DELETE | `/v1/caps/users/{email}` | `caps:read` / `caps:write` |
| User caps | POST | `/v1/caps/enforce` | `caps:write` |
| User blocks | POST | `/v1/users/{email}/block` | `users:write` |
| User blocks | POST | `/v1/users/{email}/unblock` | `users:write` |
| Groups | GET, POST | `/v1/groups` | GET: `caps:read` or `users:write` / POST: `users:write` |
| Groups | GET, PATCH, DELETE | `/v1/groups/{group_id}` | GET: `caps:read` or `users:write` / changes: `users:write` |
| Groups | POST | `/v1/groups/{group_id}/members` | `users:write` |
| Groups | POST | `/v1/groups/{group_id}/exclusions` | `users:write` |
| Groups | POST | `/v1/groups/{group_id}/sync` | `users:write` |
| Groups | POST | `/v1/groups/{group_id}/block` | `users:write` |
| Groups | POST | `/v1/groups/{group_id}/unblock` | `users:write` |
| Groups | GET, PUT, DELETE | `/v1/groups/{group_id}/model-rules` | `models:read` / `models:write` |
| Group caps | GET | `/v1/caps/groups` | `caps:read` |
| Group caps | GET, PUT, DELETE | `/v1/caps/groups/{group_id}` | `caps:read` / `caps:write` |
| Group usage | GET | `/v1/usage/groups` | `usage:read` |
| Group usage | GET | `/v1/usage/groups/{group_id}` | `usage:read` |
| Groups | GET | `/v1/users/{email}/groups` | `usage:read` or `caps:read` |
| Project caps | GET | `/v1/caps/projects` | `caps:read` |
| Project caps | PUT | `/v1/caps/projects/{project_id}` | `caps:write` |
| Project caps | POST | `/v1/caps/projects/{project_id}/lift` | `caps:write` |
| Models | GET | `/v1/models` | `models:read` |
| Models | POST | `/v1/models/sync` | `models:write` |
| Models | GET | `/v1/models/{model_id}` | `models:read` |
| Models | GET | `/v1/models/{model_id}/access` | `models:read` |
| Model access | GET | `/v1/model-access` | `models:read` |
| Model access | POST | `/v1/model-access/bulk-apply` | `models:write` |
| Model access | POST | `/v1/model-access/reconcile` | `models:write` |
| Model access | GET, PUT, DELETE | `/v1/model-access/{scope_type}/{scope_id}` | `models:read` / `models:write` |
| Model access | POST | `/v1/model-access/{scope_type}/{scope_id}/preview` | `models:read` |
| Model access | POST | `/v1/model-access/{scope_type}/{scope_id}/models/{model_id}/allow` | `models:write` |
| Model access | POST | `/v1/model-access/{scope_type}/{scope_id}/models/{model_id}/restrict` | `models:write` |
| Surface models | GET | `/v1/gemini-enterprise/apps` | `models:read` |
| Surface models | POST | `/v1/gemini-enterprise/apps/bulk-models` | `models:write` |
| Surface models | GET, PATCH | `/v1/gemini-enterprise/apps/{app_id}/models` | `models:read` / `models:write` |
| Surface models | GET, PUT | `/v1/antigravity/models` | `models:read` / `models:write` |
| Model rules | GET | `/v1/model-rules` | `models:read` |
| Model rules | GET, PUT, DELETE | `/v1/model-rules/default` | `models:read` / `models:write` |
| Model rules | POST | `/v1/model-rules/evaluate` | `models:write` |
| Model rules | GET | `/v1/model-violations` | `models:read` |
| Model rules | GET, PUT, DELETE | `/v1/users/{email}/model-rules` | `models:read` / `models:write` |
| Rate limits | GET | `/v1/rate-limits` | `quotas:read` |
| Rate limits | GET, PUT, DELETE | `/v1/rate-limits/users/{email}` | `quotas:read` / `quotas:write` |
| Rate limits | GET | `/v1/projects/{project_id}/rate-limits` | `quotas:read` |
| Rate limits | GET, PATCH | `/v1/projects/{project_id}/rate-limits/{limit_id}` | `quotas:read` / `quotas:write` |
| Quota requests | GET, POST | `/v1/quota-requests` | `quotas:read` / `quotas:write` |
| Quota requests | GET | `/v1/quota-requests/{request_id}` | `quotas:read` |
| Quota requests | POST | `/v1/quota-requests/{request_id}/cancel` | `quotas:write` |
| Gemini Platform logging | GET | `/v1/projects/{project_id}/vertex-logging` | `usage:read` or `admin` |
| Gemini Platform logging | POST | `/v1/projects/{project_id}/vertex-logging/enable` | `admin` |
| Gemini Platform logging | POST | `/v1/vertex-logging/enable` | `admin` |
| Preflight | GET | `/v1/admin/readiness` | `admin` |
| Preflight | POST | `/v1/admin/readiness/run` | `admin` |
| Preflight | GET | `/v1/projects/{project_id}/capabilities` | one of `models:read`, `quotas:read`, `caps:read` |
| Operations | GET | `/v1/operations` | one of `models:read`, `quotas:read`, `caps:read` |
| Operations | GET | `/v1/operations/{operation_id}` | one of `models:read`, `quotas:read`, `caps:read` |
| Audit | GET | `/v1/audit-logs` | `caps:read` or `admin` |
| Admin | GET, POST | `/v1/admin/api-keys` | `admin` |
| Admin | DELETE | `/v1/admin/api-keys/{key_id}` | `admin` |
| Admin | GET, POST | `/v1/admin/webhooks` | `admin` |
| Admin | DELETE | `/v1/admin/webhooks/{webhook_id}` | `admin` |
| Internal | POST | `/v1/internal/usage-event` | OIDC (`internal:pubsub`) or `admin` |
| Internal | POST | `/v1/internal/pricing/sync` | OIDC (`internal:pubsub`) or `admin` |

## 15. Authentication and scopes

* **API keys**: `gcpai_<id>_<secret>`, only a salted SHA-256 hash is stored, scoped, expirable and revocable, shown once at creation. `Authorization: Bearer <key>`.
* **OIDC**: Cloud Scheduler, Pub/Sub and in-cloud servers call with Google ID tokens (allowed service-account list and audience are verified).
* **Rate limits**: per endpoint class (`general`, `write`, `enforce`, `usage_event`).

| Scope | Grants |
|---|---|
| `usage:read` | Usage, chargeback, group usage, prices |
| `caps:read` / `caps:write` | Read / change policy, user, group and project caps; run enforcement |
| `users:write` | Block / unblock users; create, update and delete groups; members, exclusions, sync; group block / unblock |
| `models:read` / `models:write` | Model catalog, model access policies, surface models, user and group model rules |
| `quotas:read` / `quotas:write` | Rate limits and quotas; decreases and increase requests |
| `admin` | API keys, webhooks, preflight, Gemini Platform logging enablement, everything |

* **Two least-privilege service accounts**: API runtime and enforcer are separated. Prompt and response bodies are never stored; only metadata and token counts are used.
* **No billing data access**: no read access to Billing Export tables is needed.
* SSRF-safe webhook delivery and audit logs. See [docs/security.md](docs/security.md).

## 16. Differences from typical AI vendor admin APIs

| Capability | Typical AI vendor admin API | **This API (Google Cloud)** |
|---|---|---|
| Usage granularity | Organization / project / API key | **User and group** plus project / model / service / traffic tier |
| Cost | From the billing system | **Log token counts x official SKU list prices** (no Billing Export, estimate) |
| Price evidence | Pricing page | `GET /v1/pricing` (with SKU resource names, synced daily) |
| Model list | Static list | Live Model Garden plus **models observed in logs**, per-project access decisions |
| New-model detection | Not supported | `model.first_seen`, `model.catalog_changed` webhooks |
| Model allow / deny | Per project or workspace | Project / folder / organization Org Policy + Gemini Enterprise apps + **user and group rules** |
| Rate limit adjustment | Decrease limits | Decrease limits + **per-user RPM / TPM** |
| Quota increase | Usage tiers or sales contact | **Submit with `POST /v1/quota-requests` and track the review** |
| Spend limits | Organization / project budgets | User, group (shared pool or per member) and global caps (total or per project) + project hard caps |
| Blocking | Revoke API key (immediate) | IAM Deny v2, per project possible (**1-2 min propagation**), non-bypassable |
| Group policies | Partial | Google Group sync, exclusions, priority, shared pools |
| Webhooks | Limited | 20 event types, HMAC-signed |

## 17. Limitations

| Item | Detail | Mitigation |
|---|---|---|
| Cost is an estimate | List (or contract) price x tokens; credits, committed-use and negotiated discounts (without override), taxes, grounding / media charges not included | Contract price overrides, reconcile with the monthly invoice |
| Gemini Platform user attribution | The inference log principal is usually a service account; a portal header or Data Access audit logs are needed to reach a person | Exact header attribution, proportional audit-log attribution, per-project `unattributed` |
| Gemini Platform inference log coverage | Only `generateContent` / `streamGenerateContent` and partner model `rawPredict` / `streamRawPredict`; must be enabled per project | Other calls counted as audit-log requests only (`requests_only`) |
| Cloud Logging cost | Ingestion / retention of inference and audit logs is charged to customer projects | `_Default` exclusion filters, keep only the hub sink |
| Time to block | IAM Deny v2 propagates in **1-2 minutes**; in-flight requests complete | Detection in seconds (Pub/Sub) or at most 5 minutes |
| IAM Deny capacity | 2,500 denied principals per attachment point | Warning from 2,000, excess deferred (G6) |
| Per-user model limits | No native GCP mechanism | Detected from logs, blocked per project and service |
| Antigravity authorized models | No API write (console only) | `409 console_required` plus user rules |
| Org Policy | No wildcards, 500 values, propagation takes minutes | Patterns expanded against the live catalog and re-expanded hourly |
| Group sync | Requires the Cloud Identity API and the Groups Reader role; up to 5,000 members per group and 500 groups | Check `sync_error`, run `sync` manually |
| Quota increases | Reviewed by Google (hours to days) | Status tracking, webhooks |
| Gemini Enterprise | Per-seat product, only request counts tracked | `cost_basis=license` |
| Organization roles | Org Policy admin and Deny admin can only be granted at org level | `--grant-org-roles` |

## 18. Development and tests

```bash
pip install -r requirements-dev.txt     # (optional) full OpenAPI schema validation
./scripts/test.sh                        # offline unit and integration tests + script syntax + OpenAPI 3.1 validation
PYTHONPATH=src ALLOW_LOCAL_INSECURE=true STORAGE_BACKEND=file PROJECT_ID=my-hub python3 src/server.py
```

All tests run offline without GCP (fake Org Policy, Cloud Quotas, Discovery Engine, IAM Deny, Logging, Model Garden, Cloud Billing Catalog, Cloud Identity).

### 18.1 Live end-to-end verification

Verifies in a real project that exceeding a cap really results in HTTP 403 (user cap, group shared pool, group block,
unblock and restore, with propagation latencies in a report). A dedicated test service account is used as the subject,
so no human account is blocked.

```bash
export PROJECT_ID=duleetest
scripts/e2e_live.sh all        # setup + local API + verification + report (e2e-report-*.md)
```

Procedure and troubleshooting: [docs/live-e2e-test.md](docs/live-e2e-test.md)

## 19. Documentation

The detailed documents are written in Korean with English summaries.

| Document | Content |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Components, data flows, user attribution, groups, state store |
| [docs/enforcement-logic.md](docs/enforcement-logic.md) | Enforcement engine (IAM Deny v2 attachment points, G1-G7, per-project blocks, groups, restore rules) |
| [docs/cost-methodology.md](docs/cost-methodology.md) | Cost methodology (formulas, SKU dimensions, tiers, currency, overrides, examples) |
| [docs/multi-project-deployment.md](docs/multi-project-deployment.md) | Hub-and-spoke installation, user-project N:M, per-project caps, IAM, Terraform |
| [docs/model-governance.md](docs/model-governance.md) | Dynamic catalog, model policies, user and group model rules, quota API |
| [docs/webhooks.md](docs/webhooks.md) | Events, payloads, signature verification |
| [docs/preflight-checklist.md](docs/preflight-checklist.md) | Every preflight check and its remediation |
| [docs/operations-runbook.md](docs/operations-runbook.md) | Operations and incident response (price sync, inference logs, lifting group blocks, group sync) |
| [docs/security.md](docs/security.md) | Security model, permissions, threat analysis |
| [docs/live-e2e-test.md](docs/live-e2e-test.md) | Live end-to-end verification (real 403 enforcement check) |
| [CHANGELOG.md](CHANGELOG.md) | Release history (including the 1.3.0 breaking change) |
| [openapi.yaml](openapi.yaml) | Full OpenAPI 3.1 specification |
