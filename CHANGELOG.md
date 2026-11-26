# Changelog

## 1.2.0 — 동적 모델 카탈로그 · 로그 기반 비용(공식 SKU) · Vertex AI 토큰/귀속 · 멀티 프로젝트 캡
*Dynamic model catalog · log-only cost with official SKU prices · Vertex AI tokens & attribution · multi-project caps*

### 제거 (Removed)
- **하드코딩된 모델 목록 전면 제거** — 코드·설정·문서·예제 어디에도 내장 모델 ID/시드 목록이 없습니다. 모델은 실시간 Model Garden과 로그에서만 옵니다.
  *All hardcoded model lists and seed models removed (code, config, docs, examples).*
- **Cloud Billing Export 의존성 제거** — 결제 내보내기 테이블을 읽지 않으며 설정·사전점검 항목(`bq.billing_export`)·환경변수가 없어졌습니다.
  *Cloud Billing export is no longer read or required.*

### 추가 (Added)
- **동적 모델 카탈로그**: `GET /v1/models` = 실시간 Model Garden(설정된 publisher + 로그에서 관측된 publisher, 제외된 모델은 `lifecycle.delisted_at`로 유지) ∪ 모든 서비스 로그에서 관측된 모델(`first_seen`/`last_seen`, 서비스·프로젝트·사용자 수). 필터 `source=observed|model_garden`, `used=true`. publisher는 이름 패턴으로 추측하지 않습니다.
- **신규 모델 추적 웹훅**: `model.first_seen`(로그에서 처음 사용), `model.catalog_changed`(Model Garden `added`/`delisted`). 웹훅 이벤트 총 15종.
- **`GET /v1/usage/models` 확장**: `catalog_status`(`model_garden`|`observed_only`), `group_by=traffic`, 모델별 `unattributed_requests`/`unattributed_tokens`, `by_traffic`, `cost_basis`.
- **Vertex AI 토큰 출처**: 추론 요청 메타데이터 로그 `aiplatform.googleapis.com/inference_request`(Gemini `generateContent`/`streamGenerateContent`, Claude `rawPredict`/`streamRawPredict`). 프로젝트별 활성화 API: `GET /v1/projects/{project_id}/vertex-logging`, `POST /v1/projects/{project_id}/vertex-logging/enable`, `POST /v1/vertex-logging/enable`(일괄, 기본 dryRun).
- **Vertex AI 사용자 귀속**: 포털 커스텀 헤더 라벨 `x-goog-agent-platform-observability-label-user`(정확) → Data Access 감사 로그 상관(같은 프로젝트·모델·분 ±1, 비례 배분, `audit_correlated`) → 프로젝트 `unattributed`(버리지 않음). 추론 로그 미활성 시 감사 로그 요청 수(`token_sources=requests_only`). 모든 사용량 응답에 `telemetry{attribution, token_sources, unattributed_projects}`.
- **공식 SKU 단가 티어**: Cloud Billing Catalog에서 입력·출력(사고 토큰 포함)·캐시 읽기·캐시 쓰기·장문 컨텍스트·Flex/Priority/Batch 티어와 SKU 이름을 매일 동기화. Provisioned Throughput 정책(`on_demand`|`zero`). SKU 파서는 카탈로그·로그의 모델 ID로 모델 계열을 학습. `GET /v1/pricing`(SKU 단가표), 모든 응답에 `pricing_source{method, billing_export: not_used, catalog_synced_at, ...}`.
- **멀티 프로젝트 캡·차단**: `cap_scope` `total`|`per_project`(정책 및 사용자 규칙 `scope`), 사용자 `project_caps`, 정책 `project_defaults`, `metered_surfaces`. 사용자 캡 행에 `projects[]`, `status_scope`, `blocked_projects`, `over_scope`, `multi_project`. 초과한 프로젝트만 정지(`plan.suspend_projects`), IAM Deny v2 부착점별 관리, `block`/`unblock`의 `projects: [...]`. 프로젝트별 임계치 웹훅(`scope: project`, `project`). 모델 규칙 위반은 기본적으로 위반 프로젝트에서만 차단(`models.violation_block_scope: project|all`).
- **`GET /v1/users/{email}/projects`**(`include=iam`): 사용자가 사용·배정된 모든 관리 프로젝트의 사용량·비용·모델·캡 상태.
- **차지백**: (사용자, 프로젝트) 단위 정수 센트 사실값 + 프로젝트별 `unattributed` 섹션, 모든 서비스 포함.
- **설정/환경변수**: `CAP_SCOPE`, `VERTEX_USER_HEADER_LABEL`, `METERED_SURFACES`, `MODEL_VIOLATION_BLOCK_SCOPE`, `MODEL_PUBLISHERS`, `LONG_CONTEXT_THRESHOLD_TOKENS`, `PROVISIONED_THROUGHPUT_COST`; Terraform `pricing_sync_schedule`(기본 매일 03:00).
- **문서**: `docs/cost-methodology.md`(비용 산정 방식, 고객 보고용) 신규; README·아키텍처·차단 엔진·모델 거버넌스·사전점검·멀티 프로젝트·런북·웹훅 문서 갱신. Python 예제에 `pricing()`, `user_projects()`, `vertex_logging_status()`, `enable_vertex_logging()`, 프로젝트 한정 `block_user/unblock_user(projects=...)` 추가.

### 변경 (Changed)
- 비용 = 로그 토큰 수 × 공식 SKU 목록가(또는 계약 override). SKU가 없는 모델은 추측 단가로 채우지 않고 `cost_basis=unpriced`, `cost_partial=true`로 표시.
- 사전점검: `logging.vertex_inference_logging` 점검 및 `PREFLIGHT_VERTEX_LOGGING_DISABLED` 출력, 로그 싱크 필터에 `inference_request` 포함 여부 확인.

### ⚠️ Breaking changes
- 환경변수 **`BILLING_PROJECT_ID`, `BILLING_DATASET_ID`, `BILLING_TABLE_ID` 제거**(및 `PREFLIGHT_BILLING_*`). 설정해도 무시됩니다. `BILLING_ACCOUNT_ID`는 프로젝트 하드캡(Budget API)용으로 유지.
- **`GET /v1/models`는 더 이상 내장 시드 모델을 반환하지 않습니다.** Model Garden 조회가 실패하고 로그 관측도 없으면 목록이 비어 있을 수 있습니다(`catalog.errors` 확인, `POST /v1/models/sync`).
- **`Model.source` enum이 `model_garden` | `observed`** 로 바뀌었습니다(이전 값 사용 클라이언트는 수정 필요).
- 모델 규칙 위반 차단의 기본 범위가 **위반이 발생한 프로젝트**로 바뀌었습니다. 이전 동작(모든 프로젝트)은 `MODEL_VIOLATION_BLOCK_SCOPE=all`.
- `user.blocked` 웹훅에 `projects`(목록 또는 `"all"`), `cap.*` 웹훅에 `scope`/`project` 필드가 추가되었습니다(추가 필드 — 엄격한 스키마 검증 클라이언트는 확인 필요).

### Migration
1. 배포 환경에서 `BILLING_*` 변수(계정 ID 제외)를 삭제합니다.
2. `scripts/prepare.sh`를 다시 실행하면(기본 `ENABLE_VERTEX_INFERENCE_LOGGING=true`) 모든 프로젝트에서 Vertex AI 추론 요청 로그가 켜집니다. 또는 `POST /v1/vertex-logging/enable {"projects":["*"],"dryRun":true}`로 확인 후 `dryRun:false`. 기존 싱크를 채택했다면 필터에 `log_id("aiplatform.googleapis.com/inference_request")`를 추가합니다.
3. 포털의 Vertex AI 호출에 `x-goog-agent-platform-observability-label-user` 헤더를 추가합니다.
4. `POST /v1/internal/pricing/sync`를 1회 실행하고 `GET /v1/pricing`으로 단가표를 확인합니다.
5. 모델 ID를 하드코딩한 포털 코드·정책은 `GET /v1/models` 응답 또는 publisher 와일드카드(`google/*`, `anthropic/*`)로 바꿉니다.

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
