# GCP AI Usage & Spend Caps API

> **English documentation**: [README.en.md](./README.en.md) | **OpenAPI 3.1 Spec**: [`openapi.yaml`](./openapi.yaml) (`GET /v1/openapi.yaml`)

이미 사내 자체 **AI 통합 관리 포털**(OpenAI, Anthropic Claude 등 연동)을 운영 중인 엔터프라이즈 고객사를 위해, Google Cloud AI(**Antigravity**, **Vertex AI / Agent Platform**, **Gemini Enterprise**)의

1. **유저별 사용량·토큰·추정 비용 및 월별 차지백 조회 API**
2. **유저별/프로젝트별 Spend Cap 설정 및 초과 시 자동/수동 차단(Block/Unblock) API**

만을 가볍게 제공하는 **Headless(UI 없는) M2M 전용 REST API 서버**입니다.

---

## 아키텍처

```mermaid
flowchart LR
    Portal["사내 AI 통합 관리 포털<br/>(Claude / OpenAI 연동됨)"] -->|Bearer API Key / OIDC| API["Cloud Run<br/>Headless API (/v1/...)"]
    API -->|Outbound Webhook<br/>HMAC-SHA256| Portal

    subgraph GCP["Google Cloud Platform"]
        Sink["Cloud Logging Sink<br/>(Antigravity / Vertex AI / GE)"] --> BQ["BigQuery<br/>Audit Dataset"]
        Sink -->|실시간 추론 로그| PS["Pub/Sub Push<br/>(/v1/internal/usage-event)"]
        PS -->|Google OIDC| API
        API --> BQ
        API --> Billing["Billing Export &<br/>Catalog Pricing"]
        API --> GCS["GCS Private Bucket<br/>(policy / caps / hashed keys)"]
        API -->|즉시/초과 트리거| Job["Cloud Run Enforcer Job<br/>(Dedicated SA: iam.denyAdmin)"]
        Sched["Cloud Scheduler<br/>(5분 주기 안전망 & 기간 리셋)"] --> Job
        Job -->|IAM Deny v2<br/>서비스별 다중 룰| IAM["Org / Folder / Project<br/>IAM Deny Policy"]
        API -->|프로젝트 하드캡| Budget["Cloud Billing Budget API v1<br/>(Spend Cap)"]
    end
```

### 왜 Headless 전용인가요?
- 웹 대시보드 프론트엔드, AI Analyst, Model Armor, NotebookLM, Agent Registry 등을 모두 제외하여 **5개 최소 Python 의존성**, **비루트 슬림 컨테이너**, **1초 미만 콜드스타트**로 유지합니다.
- 외부 DB(Cloud SQL, Firestore 등) 없이 버전 관리가 켜진 **비공개 GCS 버킷**(`ifGenerationMatch` 낙관적 동시성)으로 멀티 인스턴스 상태를 안전하게 동기화합니다.
- **최소 권한 분리**: API 런타임 SA는 읽기 권한(`roles/iam.denyReviewer`)만 가지며, 조직 수준 차단 권한(`roles/iam.denyAdmin`)은 오직 격리된 **Cloud Run Enforcer Job 전용 SA**에만 부여합니다.

---

## OpenAI / Claude Admin API와의 주요 차이점

사내 포털에 Google Cloud AI를 연동할 때 OpenAI / Anthropic Admin API와 구조적으로 다른 점을 미리 고려해야 합니다.

| 항목 | OpenAI / Anthropic Admin API | Google Cloud AI (본 API) |
|---|---|---|
| **차단 메커니즘** | API Key 즉시 비활성화 (밀리초 반영) | **IAM Deny v2 정책** 기반 차단. Google 그룹·도메인·상속 권한·Workforce Identity까지 일괄 차단되나 **최종 일관성(Eventual Consistency)으로 약 1~2분(드물게 수 분) 반영 지연**이 발생하며, 이미 진행 중인 요청은 완료됩니다. |
| **서비스별 선택 차단** | 키 단위 일괄 차단 | 단일 관리형 IAM Deny 정책(`gcp-ai-caps-*`) 내 최대 3개 룰로 유저별 **`antigravity`**, **`vertex_ai`**, **`gemini_enterprise`** 정밀 선택 차단·해제 지원 |
| **유저별 비용 산출** | 공급사 과금 API에서 키/유저별 실청구액 제공 | GCP Cloud Billing Export에는 유저 차원이 없음. 본 API는 **관측된 유저·모델별 토큰(입력/캐시/출력·Thinking)**에 `Billing Export 실효 단가 -> 공식 Catalog -> 수동 오버라이드` 순으로 단가를 적용해 산출하고, 월별 차지백은 **정수 센트(integer-cent)** 배분으로 합계 오차 0을 보장합니다. |
| **실시간 캡 초과 감지** | API 게이트웨이 동기식 차단 | **Pub/Sub Push(`/v1/internal/usage-event`)** 실시간 추론 로그 감지 + **Cloud Scheduler(5분 주기)** 안전망 + 정산 기간 만료 시 **자동 복구(Auto-Reset)** |
| **프로젝트 하드캡** | 워크스페이스 월간 한도 즉시 중단/재개 | **Cloud Billing Budget API v1 Spend Cap** 연동 (USD 빌링 계정, Agent Platform 대상). 한도 도달 시 Google이 자동 중단(`ENFORCED`)하며, `POST /v1/caps/projects/{id}/lift`로 조기 해제 시 **재개까지 최대 약 1시간** 소요됩니다. |

---

## 안전 게이트 (Safety Gates G1~G7)

자동 차단 오동작(텔레메트리 지연, 단가 오류, 외부 정책 충돌 등)으로 전 직원이 차단되는 사고를 방지하기 위해 매 집행 사이클마다 7단계 안전 게이트를 검증합니다.

| 게이트 | 역할 | 동작 |
|---|---|---|
| **G1_POLICY_READABLE** | 모든 부착 지점의 관리형 IAM Deny 정책 가독성 및 외래(foreign) 정책 여부 검증 | 실패 시 전체 집행 중단 |
| **G2_ANY_DETERMINATE** | 대상 유저 상태 판별 가능 여부 검증 | 실패 시 전체 집행 중단 |
| **G3_TELEMETRY_HEALTHY** | 로그 조회 성공·레코드 존재·미절단 여부 검증 | 경고 시 사용량 기반 자동 복구 보류 (기간 만료 리셋 복구는 항상 허용) |
| **G4_CHANGE_BUDGET** | 단일 사이클 최대 차단 인원 제한 (`max(min, ratio * population)`) | 초과분은 다음 실행으로 이연 (복구는 무제한) |
| **G5_NON_USER_GRANT_ABSENT** | 그룹/도메인/상속 권한 커버리지 확인 | `iam_deny` 백엔드에서 그룹·상속 권한까지 차단 보장 |
| **G6_DENY_CAPACITY** | IAM Deny 리소스당 2,500명 한도 검증 (2,000명 경고) | 한도 초과 신규 차단은 심각도 낮은 순으로 보류 |
| **G7_SURGE_GUARD** | 자동 차단 급증 가드 (기본 10명 이상 & 전체 50% 이상 동시 초과) | 발동 시 자동 차단을 보류하고 수동 검토로 전환 |

---

## 인증 (M2M 전용)

1. **Scoped API Key (`Authorization: Bearer gcpai_<id>_<secret>`)**
   - 평문 저장 금지: 생성 시 1회만 반환되며 저장소에는 키별 랜덤 `salt` + `SHA-256` 해시만 저장합니다.
   - Scope 분리: `usage:read`, `caps:read`, `caps:write`, `users:write`, `admin`
2. **GCP Service Account OIDC (`Authorization: Bearer <RS256 JWT>`)**
   - Cloud Scheduler, Pub/Sub Push, 또는 GCP 내부 포털 서버의 Google 서명 OIDC 토큰 검증 (`aud == PUBLIC_BASE_URL`, `OIDC_ALLOWED_SERVICE_ACCOUNTS` 화이트리스트).

---

## 핵심 API 엔드포인트 요약 (`/v1/...`)

전체 요청/응답 스키마는 [`openapi.yaml`](./openapi.yaml) 또는 실행 중인 서버의 `GET /v1/openapi.yaml`에서 확인할 수 있습니다.

| 메서드 & 경로 | Scope | 설명 |
|---|---|---|
| `GET /v1/health` | Public | Liveness 및 (인증 시) 스토리지·집행 상태 조회 |
| `GET /v1/openapi.yaml` | Public | OpenAPI 3.1 스펙 서빙 |
| `GET /v1/usage/users` | `usage:read` | 유저별 요청수·입력/출력/캐시 토큰·모델별 breakdown·추정 비용 (`start`, `end`, `granularity`, `project`, `surface`, `email`, `limit`, `cursor`) |
| `GET /v1/usage/users/{email}` | `usage:read` | 특정 유저 상세 사용량 + 일/주 시계열 + 현재 캡 소진율 |
| `GET /v1/billing/users?month=YYYY-MM` | `usage:read` | 월별 유저·프로젝트별 차지백 리포트 (센트 단위 정합성) |
| `GET /v1/caps/policy`, `PUT /v1/caps/policy` | `caps:read` / `caps:write` | 전역 기본 캡 정책 조회/수정 (금액·토큰·요청수·기간·`auto`/`manual`) |
| `GET /v1/caps/users`, `GET /v1/caps/users/{email}` | `caps:read` | 유저별 한도, 사용량, 잔여량, 소진율%, 상태(`active\|over_cap\|suspended\|blocked`) |
| `PUT /v1/caps/users/{email}`, `DELETE /v1/caps/users/{email}` | `caps:write` | 유저별 개별 Spend Cap(`limit`/`allow`/`block`) 설정/해제 |
| `POST /v1/users/{email}/block`, `POST /v1/users/{email}/unblock` | `users:write` | 유저 즉시 차단/해제 (`services`: `antigravity`, `vertex_ai`, `gemini_enterprise` 선택 가능) |
| `POST /v1/caps/enforce` | `caps:write` / OIDC | 정책 즉시 평가 및 집행 (`{"dryRun": true\|false}`) |
| `GET /v1/caps/projects`, `PUT /v1/caps/projects/{project_id}` | `caps:read` / `caps:write` | 프로젝트 단위 월간 Spend Cap 조회 및 설정 |
| `POST /v1/caps/projects/{project_id}/lift` | `caps:write` | `ENFORCED` 상태의 프로젝트 Spend Cap 조기 해제 |
| `GET /v1/audit-logs` | `caps:read` / `admin` | API 호출 및 차단/해제 감사 이력 조회 |
| `POST /v1/internal/usage-event` | OIDC / `admin` | Pub/Sub 실시간 추론 로그 push 수신 |
| `POST /v1/internal/pricing/sync` | OIDC / `admin` | 공식 Cloud Billing Catalog 가격·환율 동기화 |
| `GET/POST /v1/admin/api-keys`, `DELETE /v1/admin/api-keys/{id}` | `admin` | API Key 발급/조회/폐기 |
| `GET/POST /v1/admin/webhooks`, `DELETE /v1/admin/webhooks/{id}` | `admin` | Outbound Webhook 관리 (임계치 80%/100%·차단 알림, HMAC-SHA256 서명) |

---

## 원클릭 배포

### 방법 1: 원클릭 스크립트 (Cloud Shell 권장)

```bash
# 1) 단일 프로젝트 배포 (BigQuery Sink + Pub/Sub + Cloud Run API + Enforcer Job + Scheduler 일괄 구성)
bash <(curl -fsSL https://github.com/ldu1225/gcp-ai-usage-caps-api/raw/main/scripts/install.sh) \
  --project YOUR_PROJECT_ID --region asia-northeast3

# 2) 멀티 프로젝트 허브 + 조직 IAM Deny v2 권한 일괄 부여
bash <(curl -fsSL https://github.com/ldu1225/gcp-ai-usage-caps-api/raw/main/scripts/install.sh) \
  --project HUB_PROJECT --link-projects PROJ_B,PROJ_C \
  --org-id 123456789012 --grant-org-roles --mode auto
```

배포 완료 시 Secret Manager에 자동 저장된 초기 `BOOTSTRAP_ADMIN_KEY`와 API URL이 출력됩니다.

### 방법 2: Terraform

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# terraform.tfvars에 project_id, container_image, organization_id 등 입력
terraform init && terraform plan && terraform apply
```

### 로컬 실행 및 CLI로 API Key 발급

```bash
# 로컬 테스트용 서버 실행
BOOTSTRAP_ADMIN_KEY="gcpai_local_bootstrap_secret_token_123456" \
  PROJECT_ID="my-gcp-project" \
  python3 src/server.py

# CLI로 포털 연동용 최소 권한 API Key 발급
PYTHONPATH=src python3 -m usage_caps_api.cli keys create \
  --name "enterprise-ai-portal" \
  --scopes "usage:read,caps:read,caps:write,users:write"
```

---

## 연동 예제 (`curl` & Python)

### 1. `curl` 예제

```bash
export API_URL="https://gcp-ai-usage-caps-api-xxxx.asia-northeast3.run.app"
export API_KEY="gcpai_xxxxxxxxxxxx_your_secret_key"

# 1) 유저별 사용량 및 추정 비용 조회
curl -sS -H "Authorization: Bearer ${API_KEY}" \
  "${API_URL}/v1/usage/users?start=2026-11-01&end=2026-11-26&surface=antigravity&limit=50" | jq .

# 2) 월별 유저·프로젝트 차지백 리포트 조회
curl -sS -H "Authorization: Bearer ${API_KEY}" \
  "${API_URL}/v1/billing/users?month=2026-11" | jq .

# 3) 전역 월간 Spend Cap 설정 ($100/월, 자동 차단 모드, 80%/100% 웹훅)
curl -sS -X PUT -H "Authorization: Bearer ${API_KEY}" -H "Content-Type: application/json" \
  "${API_URL}/v1/caps/policy" \
  -d '{
    "cost_cap": 100.0,
    "currency": "USD",
    "period": "monthly",
    "enforcement_mode": "auto",
    "default_block_services": ["antigravity", "vertex_ai"],
    "webhook_thresholds": [80, 100]
  }' | jq .

# 4) 특정 유저 개별 한도 상향 ($300/월)
curl -sS -X PUT -H "Authorization: Bearer ${API_KEY}" -H "Content-Type: application/json" \
  "${API_URL}/v1/caps/users/alice@company.com" \
  -d '{"type": "limit", "cost_cap": 300.0, "note": "ML research team"}' | jq .

# 5) 특정 유저 Antigravity & Vertex AI 즉시 차단
curl -sS -X POST -H "Authorization: Bearer ${API_KEY}" -H "Content-Type: application/json" \
  "${API_URL}/v1/users/bob@company.com/block" \
  -d '{"services": ["antigravity", "vertex_ai"], "reason": "Exceeded portal budget"}' | jq .

# 6) 특정 유저 차단 해제
curl -sS -X POST -H "Authorization: Bearer ${API_KEY}" -H "Content-Type: application/json" \
  "${API_URL}/v1/users/bob@company.com/unblock" \
  -d '{"reason": "Budget top-up approved"}' | jq .

# 7) 정책 즉시 평가 (Dry Run)
curl -sS -X POST -H "Authorization: Bearer ${API_KEY}" -H "Content-Type: application/json" \
  "${API_URL}/v1/caps/enforce" -d '{"dryRun": true}' | jq .
```

### 2. Python 연동 및 Webhook 서명 검증 예제

```python
import hashlib
import hmac
import requests

API_URL = "https://gcp-ai-usage-caps-api-xxxx.asia-northeast3.run.app"
HEADERS = {"Authorization": "Bearer gcpai_xxxxxxxxxxxx_your_secret_key"}

# 유저별 캡 소진율 조회
resp = requests.get(f"{API_URL}/v1/caps/users", headers=HEADERS, params={"status": "over_cap"}, timeout=15)
resp.raise_for_status()
for user in resp.json()["data"]:
    print(f"{user['email']}: {user['utilization_pct']}% ({user['status']})")

# 특정 유저 즉시 차단 (Antigravity만 선택 차단)
block = requests.post(
    f"{API_URL}/v1/users/bob@company.com/block",
    headers=HEADERS,
    json={"services": ["antigravity"], "reason": "Portal quota exceeded"},
    timeout=15,
)
block.raise_for_status()

# 사내 포털에서 Outbound Webhook 서명(X-Signature-256) 검증
def verify_webhook(secret: str, timestamp: str, raw_body: bytes, signature_header: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("ascii") + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)
```

---

## 오프라인 단위 테스트

외부 네트워크나 GCP 자격 증명 없이 전체 단위 테스트를 실행할 수 있습니다.

```bash
./scripts/test.sh
# 또는
PYTHONPATH=src python3 -m unittest discover -s tests/unit -v
```
