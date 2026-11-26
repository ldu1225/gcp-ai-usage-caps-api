# 실환경 E2E 검증 가이드 (Live E2E Test)

이 문서는 실제 Google Cloud 프로젝트(예: `duleetest`)에서 **한도 초과 시 실제로 차단(HTTP 403)되는지**를
처음부터 끝까지 자동으로 검증하는 절차입니다. 실제 Gemini Platform(Gemini Enterprise Agent Platform, 구 Vertex AI)
모델 호출과 실제 IAM Deny v2 정책을 사용합니다.

## 1. 무엇을 검증하나

| 단계 | 시나리오 | 통과 기준 |
|---|---|---|
| A | 사용자 요청 수 한도 초과 | 로그 반영 후 `over_cap` → `POST /v1/caps/enforce` → `suspended` → 모델 호출 403 → `unblock` 후 200 |
| B | 그룹 공유 풀(shared pool) 한도 초과 | 그룹 풀 `over_cap` → 멤버 `suspended`(사유 `group:<id>`) → 403 → 예외(exclusions) 등록 즉시 200 |
| C | 그룹 차단 | `POST /v1/groups/{id}/block` → 403 → 개인 unblock으로는 해제 불가 확인 → 그룹 unblock 후 200 |

각 단계의 소요 시간(로그 반영 지연, IAM Deny 전파 시간, 복구 시간)과 API 응답이 Markdown 리포트로 저장됩니다.
테스트가 끝나면(실패 시에도) 원래 캡 정책을 복원하고 테스트용 그룹/캡을 모두 삭제합니다.

## 2. 테스트 신원 (테스트 전용 서비스 계정)

사람 계정을 실제로 차단하지 않도록 **테스트 전용 서비스 계정**(`caps-e2e-tester@<project>.iam.gserviceaccount.com`)을
사용자처럼 다룹니다. 이를 위해 API를 `SERVICE_ACCOUNT_SUBJECTS=true`로 실행합니다.

- 서비스 계정은 기본적으로 정책 대상이 아니며, 이 옵션을 켠 경우에만 사용량 귀속, 캡, 규칙, IAM Deny 대상이 됩니다.
- IAM Deny 주체 형식: `principal://iam.googleapis.com/projects/-/serviceAccounts/<email>`
- 허브의 런타임/집행 서비스 계정은 옵션과 무관하게 항상 보호됩니다.
- 모델 호출에는 사용자 헤더 라벨 `x-goog-agent-platform-observability-label-user: <서비스 계정 이메일>`이 포함되어
  사용량이 정확히 귀속됩니다.

## 3. 사전 요구사항

- `gcloud` CLI 로그인(`gcloud auth login`) 및 ADC 로그인(`gcloud auth application-default login`)
- 대상 프로젝트에서 IAM 정책을 수정할 수 있는 권한(Owner 또는 Project IAM Admin). 참고로 Owner 역할에도
  `roles/iam.denyAdmin`은 포함되지 않으므로 setup 단계에서 별도로 부여합니다.
- Python 3.10 이상, `curl`

## 4. 실행 (로컬 API 서버, 권장)

```bash
git clone https://github.com/ldu1225/gcp-ai-usage-caps-api.git
cd gcp-ai-usage-caps-api

export PROJECT_ID=duleetest
# 선택: 모델 지정 (미지정 시 GET /v1/models 의 최신 GA flash 계열 모델을 자동 선택)
# export E2E_MODEL=<model-id>

scripts/e2e_live.sh all
```

`all`은 다음을 순서대로 수행합니다.

1. `setup`: API 활성화, 테스트 서비스 계정 생성, `roles/aiplatform.user` 부여, 운영자에게 가장(impersonation) 권한 및
   로컬 API 실행에 필요한 역할(`roles/iam.denyAdmin`, `roles/logging.viewer`, `roles/logging.privateLogViewer`,
   `roles/aiplatform.admin`, `roles/serviceusage.serviceUsageConsumer`, `roles/iam.securityReviewer`) 부여
2. `server`: 운영자 ADC로 로컬 API 실행(파일 저장소, Cloud Logging 직접 조회, `SERVICE_ACCOUNT_SUBJECTS=true`)
3. `run`: `scripts/e2e_live_test.py` 실행 후 리포트 저장(`e2e-report-<project>-<시각>.md`)

단계별로 나눠 실행할 수도 있습니다.

```bash
scripts/e2e_live.sh setup
scripts/e2e_live.sh server          # 터미널 1 (포그라운드)
scripts/e2e_live.sh run             # 터미널 2
scripts/e2e_live.sh run --quick     # C 단계(그룹 차단) 생략
```

## 5. 실행 (배포된 Cloud Run API 대상)

배포 시 `SERVICE_ACCOUNT_SUBJECTS=true`(Terraform: `service_account_subjects = true`)로 설정해야 합니다.

```bash
export PROJECT_ID=duleetest
export CAPS_API_URL=https://<service>.run.app        # Cloud Run IAM 인증이 필요하면 아래 proxy 사용
export CAPS_ADMIN_KEY=gcpai_...                      # admin 스코프 API 키
scripts/e2e_live.sh setup
scripts/e2e_live.sh run
```

Cloud Run이 IAM 호출자 인증을 요구하는 경우:

```bash
gcloud run services proxy gcp-ai-usage-caps-api --project "$PROJECT_ID" --region <region> --port 8080 &
export CAPS_API_URL=http://127.0.0.1:8080
```

배포 환경에서는 5분 주기 스케줄러나 Pub/Sub 실시간 경로가 먼저 차단할 수 있으며, 스크립트는 이 경우도 통과로 처리합니다.

## 6. 예상 소요 시간

| 구간 | 일반적인 값 |
|---|---|
| 추론 로그가 API에 반영되는 시간 | 1~5분 (Cloud Logging 수집 지연) |
| IAM Deny 적용 후 403까지 | 약 1~2분 (최대 7분까지 대기) |
| 해제 후 200 복구까지 | 약 1~2분 |
| 전체 | 약 15~25분 |

## 7. 결과 해석과 문제 해결

| 실패 단계 | 원인 후보 | 조치 |
|---|---|---|
| Inference request metadata logging enabled | 권한 부족 | 운영자에게 `roles/aiplatform.admin` 부여 후 재시도 |
| A3 usage visible from logs | 로깅 미활성화 직후, 로그 지연 | `--log-wait 1500`으로 재실행, `GET /v1/projects/<p>/vertex-logging` 확인 |
| A5 / B4 enforce | 안전 게이트 실패(G1: deny 정책 읽기 불가) | 운영자에게 `roles/iam.denyAdmin` 부여 확인 |
| A7 / B6 / C2 403 미발생 | 전파 지연 | `--propagation-wait 900`으로 재실행, `gcloud iam policies list --attachment-point=cloudresourcemanager.googleapis.com/projects/<p> --kind=denypolicies` 로 정책 확인 |
| 모델 호출 404 | 모델 ID/리전 불일치 | `E2E_MODEL`, `E2E_LOCATION` 지정 |

정리: `scripts/e2e_live.sh cleanup` 은 테스트 서비스 계정을 deny 정책에서 제거하고 계정을 삭제합니다.
