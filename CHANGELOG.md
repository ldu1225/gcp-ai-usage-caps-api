# Changelog

## [1.3.1] - 2026-11-27
*고객사 포털 연동 마찰 제거 · 즉시 반영되는 차단 상태 · 정규화된 단가 · 응답 속도 개선*
*Seamless customer-portal integration: immediate block visibility, canonical pricing, structured billing sync, faster reads*

### 추가 (Added)
- **차단 전파 상태**: 사용자 캡 응답(`GET /v1/caps/users`, `GET /v1/caps/users/{email}`, block/unblock)에 `enforcement_status`(`none` | `propagating` | `enforced`), `pending_blocked_services`, `pending_blocked_projects`, `projects[].enforcement_status` 추가. Enforcer Job이 IAM Deny v2를 쓰기 전에도 `blocked_services`/`blocked_projects`가 즉시 유효 값으로 반환됩니다.
  *Effective block state is returned immediately while the Enforcer Job rolls out IAM Deny v2.*
- **`billing_sync`**: `GET /v1/caps/projects`에 구조화된 결제 동기화 상태(`status`: `ok` | `disabled` | `not_configured` | `permission_required` | `error`, `message`, `billing_account`, `required_role`, `remediation`). 기존 `sync_error`는 유지.
- **`/v1/pricing` 정규 필드**: 행마다 `model`(카탈로그 정규 id), `model_ids`, `input_per_1m_tokens`, `output_per_1m_tokens`, `cached_input_per_1m_tokens`, `cache_write_per_1m_tokens`. `?model=`은 하이픈/점 표기 id와 SKU 키를 모두 허용. 기존 짧은 키는 유지.
- **OpenAPI 3.1 보강**: `POST /v1/admin/api-keys` 9개 scope 전체, `SafetyGate`, `EnforcePlan`, `EnforceResult`, `UserBlockResponse`, `UserUnblockResponse`, `PricingRow`, `BillingSync`, `ProjectSpendCapRow` 스키마, `?fresh` 파라미터.

### 변경 (Changed)
- **응답 속도**: 3개 서피스 사용량 병렬 수집 + 스탬피드 방지 TTL 캐시(`usage`, `caps_status`, 프로젝트 캡, Org Policy 원본). 모든 쓰기 작업에서 자동 무효화되며 `?fresh=true`로 우회. 실측(warm): `/v1/usage/users` 5.50s → 0.09s, `/v1/caps/users` 3.80s → 0.15s, `/v1/model-access` 1.13s → 0.07s, `/v1/caps/projects` 1.23s → 0.07s.
- **`?fresh=true`의 IAM Deny 직접 재조회**: 별도 컨테이너인 Enforcer Job이 쓴 Deny 정책을 API가 즉시 읽도록 `fresh`를 `DenyPolicyClient`까지 전달하고, 캡 변경 시 `deny_policy` 캐시를 비웁니다.
- **`scripts/deploy.sh`**: `gcloud billing accounts add-iam-policy-binding`이 IAM 정책 버전 불일치로 실패하면 Cloud Billing v1 REST `getIamPolicy`/`setIamPolicy`로 `roles/billing.costsManager`를 부여합니다.

### 테스트 (Tests)
- `tests/unit/test_seamless_customer_api.py` 추가. 오프라인 테스트 163개 통과.

### 문서 (Docs)
- `docs/model-governance.md`: 모델 접근 Org Policy의 실환경 집행 범위와 전파 시간 안내 추가(global 엔드포인트 미반영, 리전별 반영 지연, 즉시 차단에는 IAM Deny 병행 권장).

## [1.3.0] - 2026-11-26
*그룹을 일급 정책 주체로 · Gemini Platform 명칭 변경 · 문서와 다이어그램 재설계*
*Groups as first-class policy subjects · Gemini Platform naming · redesigned docs and diagrams*

### 추가 (Added)
- **서비스 계정 정책 대상(옵션)**: `SERVICE_ACCOUNT_SUBJECTS=true`(Terraform `service_account_subjects`) 시 서비스 계정에 사용량 귀속(추론 로그 principal), 캡, 규칙, IAM Deny(`principal://iam.googleapis.com/projects/-/serviceAccounts/EMAIL`) 적용. 허브 런타임/집행 SA는 항상 보호. 기본값 off.
  *Opt-in service-account subjects; hub service accounts always protected.*
- **실환경 E2E 검증 도구**: `scripts/e2e_live.sh`, `scripts/e2e_live_test.py`, `docs/live-e2e-test.md` (사용자 캡, 그룹 공유 풀, 그룹 차단에서 실제 403 및 복구 확인, 지연시간 리포트).
  *Live E2E verifier with real 403 checks and latency report.*
- **Cloud Identity API**를 허브 선택 API로 추가(그룹 동기화), 사전점검·VPC-SC 서비스 목록 반영.
- **그룹(Groups)**: 사용자와 동일한 기능(지출 캡, 차단, 모델 규칙, 예외, 사용량)을 갖는 정책 주체. 유효 멤버 = `members` + Google Group 동기화 멤버 - `excluded`. 해석 순서: 사용자 규칙 > 그룹 규칙(`priority` 낮은 순, 같으면 그룹 id) > 전역 정책 / 기본 규칙. 사용자 `allow`는 그룹 차단보다 우선. 그룹 정책은 개인 레코드에 복사되지 않고 매번 계산되므로 멤버 제거·예외·규칙 삭제가 즉시 반영되고 되돌릴 수 있습니다.
  *Groups with the same capabilities as users; resolution user > group (by priority) > global policy / default; user-level allow overrides group blocks; group policies are derived on the fly.*
- **그룹 엔드포인트**:
  - `GET /v1/groups`, `POST /v1/groups`, `GET /v1/groups/{group_id}`, `PATCH /v1/groups/{group_id}`, `DELETE /v1/groups/{group_id}`
  - `POST /v1/groups/{group_id}/members`, `POST /v1/groups/{group_id}/exclusions`, `POST /v1/groups/{group_id}/sync`
  - `POST /v1/groups/{group_id}/block`, `POST /v1/groups/{group_id}/unblock`
  - `GET /v1/groups/{group_id}/model-rules`, `PUT /v1/groups/{group_id}/model-rules`, `DELETE /v1/groups/{group_id}/model-rules`
  - `GET /v1/caps/groups`, `GET /v1/caps/groups/{group_id}`, `PUT /v1/caps/groups/{group_id}`, `DELETE /v1/caps/groups/{group_id}` (`pool`: `per_member` | `shared`, `scope`: `total` | `per_project`, `project_caps`)
  - `GET /v1/usage/groups`, `GET /v1/usage/groups/{group_id}` (그룹 사용량 집계, 멤버별 내역, 시계열)
  - `GET /v1/users/{email}/groups` (소속 그룹, 제외된 그룹, 유효 캡·차단·모델 규칙 출처, 공유 풀)
- **공유 풀 캡**: 유효 멤버 전체 사용량 합계에 하나의 캡. 개인 캡에 추가로 평가되며, 초과 시 사용자 `allow`가 없는 멤버를 모든 관리 프로젝트(프로젝트별 풀은 초과 프로젝트만)에서 정지.
- **그룹 Webhook**: `group.cap_threshold_reached`, `group.cap_exceeded`, `group.blocked`, `group.unblocked`, `group.membership_changed` (웹훅 이벤트 총 20종).
- **Google Group 동기화**: Cloud Identity API(하위 그룹 포함 `searchTransitiveMemberships`, 실패 시 직접 멤버십으로 대체), 매시 자동 + 수동 `sync`. 런타임 서비스 계정에 Groups Reader 관리자 역할 필요. 서비스 계정은 제외.
- **사용자 캡 응답 필드**: `rule_source`(`user` | `group:<id>` | `policy`), `groups`, `group_pools`, `block_source`(`manual` | `group:<id>` | `rule` | `auto_cap`). `GET /v1/caps/users` 응답에 `group_pools` 요약.
- 그룹 해제·그룹 캡 삭제·그룹 삭제 시 **그 그룹 때문에만 생긴 정지만** 해제하고 개인 차단·개인 캡 정지는 유지(`members_still_blocked`).

### 변경 (Changed)
- **BREAKING: 서비스 id `vertex_ai` -> `gemini_platform`.** 응답, 웹훅, `services`, `metered_surfaces`, `default_block_services`, `surface` 파라미터가 `gemini_platform`을 사용합니다. 이전 id `vertex_ai`(및 `vertex`, `agent_platform`)는 입력과 저장 문서에서 계속 별칭으로 허용되어 자동 변환되지만, 응답 값을 문자열로 비교하는 클라이언트는 수정이 필요합니다.
  *Surface id renamed `vertex_ai` -> `gemini_platform` (breaking for clients comparing response values); the legacy id is still accepted as an alias.*
- **제품 명칭**: Vertex AI는 Google에 의해 Gemini Enterprise Agent Platform(약칭 Gemini Platform)으로 변경되었습니다. 모든 문서와 설명이 이 명칭을 사용합니다. 실제 API 식별자(`aiplatform.googleapis.com`, `aiplatform.googleapis.com/inference_request`, `vertexai.allowedModels`, `/v1/projects/{project_id}/vertex-logging`, 설정 키 `vertex_inference_log`)는 그대로입니다.
- **`VERTEX_LOGGING_ENDPOINT`**: 추론 요청 로그 설정 API 엔드포인트 재정의(`https://host[:port]/v1beta1` 형식만 허용, 기본 `https://aiplatform.googleapis.com/v1beta1`).
- **문서·다이어그램 재설계**: README(한국어 기본, 영어판) 전면 개편, 상표 로고 없는 새 배너, 손으로 작성한 SVG 다이어그램(`architecture.svg`, `enforcement-flow.svg`, `policy-resolution.svg`, `multi-project.svg`), 그룹 문서(차단 엔진, Webhook, 모델 거버넌스, 보안, 운영 런북), 전체 엔드포인트 표. 문서에서 특정 AI 벤더 비교를 중립적인 표현으로 바꿨습니다.

### Migration
1. 포털에서 서비스 id `vertex_ai`를 비교하는 코드를 `gemini_platform`으로 바꿉니다(요청에는 당분간 `vertex_ai`도 허용).
2. Google Group 동기화를 쓰려면 허브 프로젝트에서 `cloudidentity.googleapis.com`을 활성화하고 런타임 서비스 계정에 Groups Reader 역할을 부여합니다.
3. 새 그룹 이벤트를 받으려면 기존 웹훅의 `events`에 `group.*` 이벤트를 추가하거나 새 웹훅을 등록합니다.

## [1.2.0]
*동적 모델 카탈로그 · 로그 기반 비용(공식 SKU) · Gemini Platform(구 Vertex AI) 토큰/귀속 · 멀티 프로젝트 캡*
*Dynamic model catalog · log-only cost with official SKU prices · Gemini Platform (formerly Vertex AI) tokens & attribution · multi-project caps*

### 제거 (Removed)
- **하드코딩된 모델 목록 전면 제거** — 코드·설정·문서·예제 어디에도 내장 모델 ID/시드 목록이 없습니다. 모델은 실시간 Model Garden과 로그에서만 옵니다.
  *All hardcoded model lists and seed models removed (code, config, docs, examples).*
- **Cloud Billing Export 의존성 제거** — 결제 내보내기 테이블을 읽지 않으며 설정·사전점검 항목(`bq.billing_export`)·환경변수가 없어졌습니다.
  *Cloud Billing export is no longer read or required.*

### 추가 (Added)
- **동적 모델 카탈로그**: `GET /v1/models` = 실시간 Model Garden(설정된 publisher + 로그에서 관측된 publisher, 제외된 모델은 `lifecycle.delisted_at`로 유지) ∪ 모든 서비스 로그에서 관측된 모델(`first_seen`/`last_seen`, 서비스·프로젝트·사용자 수). 필터 `source=observed|model_garden`, `used=true`. publisher는 이름 패턴으로 추측하지 않습니다.
- **신규 모델 추적 웹훅**: `model.first_seen`(로그에서 처음 사용), `model.catalog_changed`(Model Garden `added`/`delisted`). 웹훅 이벤트 총 15종.
- **`GET /v1/usage/models` 확장**: `catalog_status`(`model_garden`|`observed_only`), `group_by=traffic`, 모델별 `unattributed_requests`/`unattributed_tokens`, `by_traffic`, `cost_basis`.
- **Gemini Platform 토큰 출처**: 추론 요청 메타데이터 로그 `aiplatform.googleapis.com/inference_request`(Gemini `generateContent`/`streamGenerateContent`, 파트너 모델 `rawPredict`/`streamRawPredict`). 프로젝트별 활성화 API: `GET /v1/projects/{project_id}/vertex-logging`, `POST /v1/projects/{project_id}/vertex-logging/enable`, `POST /v1/vertex-logging/enable`(일괄, 기본 dryRun).
- **Gemini Platform 사용자 귀속**: 포털 커스텀 헤더 라벨 `x-goog-agent-platform-observability-label-user`(정확) → Data Access 감사 로그 상관(같은 프로젝트·모델·분 ±1, 비례 배분, `audit_correlated`) → 프로젝트 `unattributed`(버리지 않음). 추론 로그 미활성 시 감사 로그 요청 수(`token_sources=requests_only`). 모든 사용량 응답에 `telemetry{attribution, token_sources, unattributed_projects}`.
- **공식 SKU 단가 티어**: Cloud Billing Catalog에서 입력·출력(사고 토큰 포함)·캐시 읽기·캐시 쓰기·장문 컨텍스트·Flex/Priority/Batch 티어와 SKU 이름을 매일 동기화. Provisioned Throughput 정책(`on_demand`|`zero`). SKU 파서는 카탈로그·로그의 모델 ID로 모델 계열을 학습. `GET /v1/pricing`(SKU 단가표), 모든 응답에 `pricing_source{method, billing_export: not_used, catalog_synced_at, ...}`.
- **멀티 프로젝트 캡·차단**: `cap_scope` `total`|`per_project`(정책 및 사용자 규칙 `scope`), 사용자 `project_caps`, 정책 `project_defaults`, `metered_surfaces`. 사용자 캡 행에 `projects[]`, `status_scope`, `blocked_projects`, `over_scope`, `multi_project`. 초과한 프로젝트만 정지(`plan.suspend_projects`), IAM Deny v2 부착점별 관리, `block`/`unblock`의 `projects: [...]`. 프로젝트별 임계치 웹훅(`scope: project`, `project`). 모델 규칙 위반은 기본적으로 위반 프로젝트에서만 차단(`models.violation_block_scope: project|all`).
- **`GET /v1/users/{email}/projects`**(`include=iam`): 사용자가 사용·배정된 모든 관리 프로젝트의 사용량·비용·모델·캡 상태.
- **차지백**: (사용자, 프로젝트) 단위 정수 센트 사실값 + 프로젝트별 `unattributed` 섹션, 모든 서비스 포함.
- **설정/환경변수**: `CAP_SCOPE`, `VERTEX_USER_HEADER_LABEL`, `METERED_SURFACES`, `MODEL_VIOLATION_BLOCK_SCOPE`, `MODEL_PUBLISHERS`, `LONG_CONTEXT_THRESHOLD_TOKENS`, `PROVISIONED_THROUGHPUT_COST`; Terraform `pricing_sync_schedule`(기본 매일 03:00).
- **문서**: `docs/cost-methodology.md`(비용 산정 방식, 고객 보고용) 신규; README·아키텍처·차단 엔진·모델 거버넌스·사전점검·멀티 프로젝트·런북·웹훅 문서 갱신. Python 예제에 `pricing()`, `user_projects()`, `vertex_logging_status()`, `enable_vertex_logging()`, 프로젝트 한정 `block_user/unblock_user(projects=...)` 추가.

### 변경 (Changed)
- 비용 = 로그 토큰 수 × 공식 SKU 목록가(또는 계약 override). SKU가 없는 모델은 추측 단가로 채우지 않고 `cost_basis=unpriced`, `cost_partial=true`로 표시.
- 사전점검: `logging.vertex_inference_logging` 점검 및 `PREFLIGHT_VERTEX_LOGGING_DISABLED` 출력, 로그 싱크 필터에 `inference_request` 포함 여부 확인.

### Breaking changes
- 환경변수 **`BILLING_PROJECT_ID`, `BILLING_DATASET_ID`, `BILLING_TABLE_ID` 제거**(및 `PREFLIGHT_BILLING_*`). 설정해도 무시됩니다. `BILLING_ACCOUNT_ID`는 프로젝트 하드캡(Budget API)용으로 유지.
- **`GET /v1/models`는 더 이상 내장 시드 모델을 반환하지 않습니다.** Model Garden 조회가 실패하고 로그 관측도 없으면 목록이 비어 있을 수 있습니다(`catalog.errors` 확인, `POST /v1/models/sync`).
- **`Model.source` enum이 `model_garden` | `observed`** 로 바뀌었습니다(이전 값 사용 클라이언트는 수정 필요).
- 모델 규칙 위반 차단의 기본 범위가 **위반이 발생한 프로젝트**로 바뀌었습니다. 이전 동작(모든 프로젝트)은 `MODEL_VIOLATION_BLOCK_SCOPE=all`.
- `user.blocked` 웹훅에 `projects`(목록 또는 `"all"`), `cap.*` 웹훅에 `scope`/`project` 필드가 추가되었습니다(추가 필드 — 엄격한 스키마 검증 클라이언트는 확인 필요).

### Migration
1. 배포 환경에서 `BILLING_*` 변수(계정 ID 제외)를 삭제합니다.
2. `scripts/prepare.sh`를 다시 실행하면(기본 `ENABLE_VERTEX_INFERENCE_LOGGING=true`) 모든 프로젝트에서 Gemini Platform 추론 요청 로그가 켜집니다. 또는 `POST /v1/vertex-logging/enable {"projects":["*"],"dryRun":true}`로 확인 후 `dryRun:false`. 기존 싱크를 채택했다면 필터에 `log_id("aiplatform.googleapis.com/inference_request")`를 추가합니다.
3. 포털의 Gemini Platform 호출에 `x-goog-agent-platform-observability-label-user` 헤더를 추가합니다.
4. `POST /v1/internal/pricing/sync`를 1회 실행하고 `GET /v1/pricing`으로 단가표를 확인합니다.
5. 모델 ID를 하드코딩한 포털 코드·정책은 `GET /v1/models` 응답 또는 publisher 와일드카드(`google/*`, `<publisher>/*`)로 바꿉니다.

## [1.1.0]
*Model governance, quotas, preflight, hub-and-spoke*

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

## [1.0.0]
*Initial release*
- Usage & cost per user, spend caps with IAM Deny v2 (G1–G7 gates), block/unblock, project hard caps, webhooks, audit.
