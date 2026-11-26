# Changelog

## 1.1.0 — Model governance, quotas, preflight, hub-and-spoke

### Added
- **Model governance**: `GET /v1/models`, `/v1/models/{id}`, `/v1/models/{id}/access`, `/v1/models/sync`, `/v1/usage/models`.
- **Model access policies** (Org Policy `constraints/vertexai.allowedModels`) for projects/folders/organization: get/put/delete,
  preview (impact), allow/restrict single model, bulk-apply to `projects/*`, hourly pattern reconcile, If-Match etags,
  lockout prevention, 500-value guard, operations.
- **Gemini Enterprise** app model selection (merge-safe PATCH, bulk) and **Antigravity** authorized models (read-only,
  `409 console_required` on write).
- **Per-user model rules** (default + per user): allow/deny lists, per-model RPM/TPM/daily/monthly limits; real-time and
  scheduled detection; default action `block_service` via IAM Deny v2 with `user.model_violation` webhook; auto-lift.
- **Rate limits & quotas**: Cloud Quotas + Monitoring headroom, decreases, quota increase requests submitted directly to
  Google Cloud (single/bulk, Idempotency-Key, status tracking, cancel, webhooks).
- **Environment readiness (preflight)**: CLI, installer integration and `GET/POST /v1/admin/readiness` covering project,
  APIs, organization policies, VPC Service Controls (incl. cross-perimeter), network, BigQuery/log sink adoption, audit
  logs, surfaces, IAM, IAM Deny capacity, Pub/Sub/Scheduler/Cloud Run, quotas.
- Hub-and-spoke installer & Terraform (`managed_projects`, folder/org scope, central dataset, reuse of existing datasets
  and sinks, Data Access logs, VPC egress, DRS adaptation).
- New scopes `models:read|write`, `quotas:read|write`; HTTP `PATCH`; new error codes; 7 new webhook events.
- Docs: architecture, multi-project deployment, preflight checklist, enforcement logic, model governance, webhooks,
  runbook, security; Python client example.

### Changed
- Expired blocks are restored by the next enforcement cycle; `block_source` exposed on user caps.
- OpenAPI spec is now validated (OpenAPI 3.1) and every route is covered by a test.

### Migration
- Terraform resources moved from `count` to `for_each`; see docs/multi-project-deployment.md for `terraform state mv`.

## 1.0.0 — Initial release
- Usage & cost per user, spend caps with IAM Deny v2 (G1–G7 gates), block/unblock, project hard caps, webhooks, audit.
