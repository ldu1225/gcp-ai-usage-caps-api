# Live E2E report

- Date (UTC): 2026-11-28 07:00:21
- API: https://gcp-ai-usage-caps-api-44628256247.asia-northeast3.run.app
- Project: caps-blank-hub-1517 (location global)
- Test identity: caps-e2e-tester@caps-blank-hub-1517.iam.gserviceaccount.com
- Model: gemini-4-neon
- Result: 25 / 26 steps passed

| t (s) | Step | Result | Detail |
|---:|---|---|---|
| 1.3 | API reachable and authenticated | PASS | version=1.4.0 storage=gcs |
| 1.8 | Inference request metadata logging enabled | PASS | state=ENABLED |
| 16.3 | Test identity can call the model before any cap | PASS | HTTP 200 |
| 16.9 | Cap policy set to auto / iam_deny for the test | PASS | original={'enforcement_mode': 'manual', 'enforcement_backend': 'iam_deny', 'surge_guard_min': 10, 'change_budget_min': 10} |
| 26.5 | A1 user request cap set | PASS | request_cap=8 (baseline 5 requests this period) |
| 42.0 | A2 calls under/over the cap succeed (cap is enforced after logs arrive) | PASS | HTTP [200, 200, 200, 200] |
| 70.5 | A3 usage visible from logs | PASS | requests=10 after 28s |
| 87.5 | A4 status over_cap with utilization >= 100 pct | FAIL | status=blocked utilization=125.0% cost=0.00243 |
| 102.6 | A5 enforce suspends the identity | PASS | result=job_started gates=[('G1_POLICY_READABLE', 'pass'), ('G2_ANY_DETERMINATE', 'pass'), ('G3_TELEMETRY_HEALTHY', 'pass'), ('G4_CHANGE_BUDGET', 'pass'), ('G5_NON_USER_GRANT_ABSENT', 'pass'), ('G6_DENY_CAPACITY', 'pass'), ('G7_SURGE_GUARD', 'pass')] |
| 102.6 | A6 IAM Deny entry present | PASS | status=suspended blocked_services=['antigravity', 'gemini_enterprise', 'gemini_platform'] |
| 148.6 | A7 model call denied (HTTP 403) | PASS | after 46s (last HTTP 403) |
| 150.0 | A8 unblock accepted | PASS | status=active |
| 276.0 | A9 model call allowed again | PASS | after 126s (last HTTP 200) |
| 276.5 | B1 group created with the identity as member | PASS | group=e2e-1128065617 members=1 |
| 281.3 | B2 shared-pool group cap set at current usage | PASS | request_cap=12 |
| 282.3 | B3 group pool over cap | PASS | pooled_requests=12 utilization=100.0% |
| 289.4 | B4 enforce suspends the member | PASS | result=job_started |
| 289.4 | B5 reason is the group pool | PASS | reasons=['group:e2e-1128065617'] status=over_cap |
| 385.1 | B6 model call denied (HTTP 403) | PASS | after 96s |
| 386.2 | B7 exclusion (exception) applied | PASS | enforcement_applied=True |
| 509.6 | B8 model call allowed again after exclusion | PASS | after 124s |
| 511.6 | C1 group block applied | PASS | members_blocked=['caps-e2e-tester@caps-blank-hub-1517.iam.gserviceaccount.com'] |
| 511.7 | C2 model call denied (HTTP 403) | PASS | after 0s |
| 512.5 | C3 user unblock cannot override group block | PASS | remaining_group_blocks=['e2e-1128065617'] |
| 513.2 | C4 group unblock accepted | PASS | still_blocked=[] |
| 516.2 | C5 model call allowed again | PASS | after 3s |

## Latencies (seconds)

- log_latency_s: [28.5]
- deny_propagation_s: [46.0, 95.6, 0.1]
- restore_propagation_s: [126.0, 123.5, 3.0]
