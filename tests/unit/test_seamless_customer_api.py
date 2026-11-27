"""Regression tests for seamless customer integration fixes:
1. /v1/caps/projects structured billing_sync metadata, caching, ?fresh=true, and Cloud Billing v1 deploy fallback.
2. Immediate effective blocked_projects + enforcement_status='propagating' -> 'enforced' during async Enforcer Job rollout.
3. /v1/pricing canonical model IDs (model, model_ids) + *_per_1m_tokens fields + dot/hyphen ?model= lookup.
4. openapi.yaml completeness (all 9 API key scopes, SafetyGate/EnforceResult, PricingRow, BillingSync, enforcement_status).
5. Parallel 3-surface collect_usage (with uid attribution parity) + usage/caps_status/model-access TTL caching & invalidation.
"""

from __future__ import annotations

import datetime as dt
import os
import unittest

import yaml

from tests.unit.governance_fakes import GovernanceFakeGcp
from tests.unit.support import (LiveTestServer, build_test_app, make_inference_entry,
                                make_test_config, make_vertex_entry)
from usage_caps_api import auth, config, identity
from usage_caps_api.gcp import GcpError
from usage_caps_api.services import base as svc_base, caps as caps_svc, usage as usage_svc

ADMIN = "gcpai_boot_secret_1234567890"
HUB = "test-ai-project"
TEAM_B = "team-b-project"


class SeamlessCustomerApiTests(unittest.TestCase):
    def test_project_spend_caps_billing_sync_permission_required_cache_and_fresh(self):
        cfg = make_test_config(
            workspace={"billing": {"account_id": "012345-6789AB-CDEF01"}},
            caps={"spend_caps_enabled": True},
            hub={"runtime_service_account": "ai-usage-caps-api@test-ai-project.iam.gserviceaccount.com"},
        )
        app, fake_gcp = build_test_app(cfg=cfg)

        def deny_budgets(method, url, params, body):
            raise GcpError("denied", status=403, message="Permission denied")

        fake_gcp.rest_handlers.append((lambda m, u, p, b: "billingbudgets.googleapis.com" in u, deny_budgets))

        with LiveTestServer(app) as srv:
            st, body1, _ = srv.request("GET", "/v1/caps/projects", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertEqual(body1["sync_error"], "Could not read Cloud Billing budgets (denied).")
            bs = body1["billing_sync"]
            self.assertEqual(bs["status"], "permission_required")
            self.assertEqual(bs["required_role"], "roles/billing.costsManager")
            self.assertEqual(bs["billing_account"], "012345-6789AB-CDEF01")
            self.assertIn("ai-usage-caps-api@test-ai-project.iam.gserviceaccount.com", bs["remediation"])

            budget_calls_1 = sum(1 for _, u, _, _ in fake_gcp.rest_calls if "billingbudgets.googleapis.com" in u)
            self.assertEqual(budget_calls_1, 1)

            # Second call hits cache -> zero additional Budget API calls
            st, body2, _ = srv.request("GET", "/v1/caps/projects", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertEqual(body2["billing_sync"]["status"], "permission_required")
            budget_calls_2 = sum(1 for _, u, _, _ in fake_gcp.rest_calls if "billingbudgets.googleapis.com" in u)
            self.assertEqual(budget_calls_2, 1)

            # ?fresh=true bypasses cache
            fake_gcp.rest_handlers.clear()
            st, body3, _ = srv.request("GET", "/v1/caps/projects?fresh=true", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertIsNone(body3["sync_error"])
            self.assertEqual(body3["billing_sync"]["status"], "ok")
            budget_calls_3 = sum(1 for _, u, _, _ in fake_gcp.rest_calls if "billingbudgets.googleapis.com" in u)
            self.assertEqual(budget_calls_3, 2)

        # Verify deploy scripts include Cloud Billing v1 REST fallback
        with open(os.path.join(config.repo_root(), "scripts", "lib", "common.sh"), encoding="utf-8") as fh:
            common_sh = fh.read()
        with open(os.path.join(config.repo_root(), "scripts", "deploy.sh"), encoding="utf-8") as fh:
            deploy_sh = fh.read()
        self.assertIn("grant_billing_account_role", common_sh)
        self.assertIn("cloudbilling.googleapis.com/v1/billingAccounts/", common_sh)
        self.assertIn("grant_billing_account_role", deploy_sh)

    def test_async_enforcer_job_block_exposes_immediate_blocked_projects_and_propagating_status(self):
        job_ref = "projects/test-ai-project/locations/asia-northeast3/jobs/gcp-ai-caps-enforcer"
        cfg = make_test_config(
            workspace={"projects": [TEAM_B]},
            caps={"enforcement_job": job_ref, "enforcement_mode": "auto"},
        )
        app, fake_gcp = build_test_app(cfg=cfg, gcp=GovernanceFakeGcp())
        job_runs = []

        def run_job(method, url, params, body):
            job_runs.append(url)
            return {"name": "projects/test-ai-project/locations/asia-northeast3/operations/op-1",
                    "metadata": {"name": job_ref + "/executions/exec-1"}}

        fake_gcp.rest_handlers.append((lambda m, u, p, b: "run.googleapis.com/v2/" in u and u.endswith(":run"), run_job))

        with LiveTestServer(app) as srv:
            # 1) Block alice ONLY on team-b-project for gemini_platform (Enforcer Job runs asynchronously!)
            st, blk, _ = srv.request(
                "POST", "/v1/users/alice%40company.com/block",
                body={"projects": [TEAM_B], "services": ["gemini_platform"], "reason": "spoke-cap"},
                token=ADMIN,
            )
            self.assertEqual(st, 200, blk)
            self.assertEqual(blk["enforcement"]["mode"], "enforcer_job")
            self.assertEqual(len(job_runs), 1)
            self.assertEqual(fake_gcp.deny_policies, {})  # IAM Deny not yet written!

            # 2) Immediate GET /v1/caps/users/{email} and /v1/caps/users reflect blocked_projects + propagating!
            st, cap, _ = srv.request("GET", "/v1/caps/users/alice%40company.com", token=ADMIN)
            self.assertEqual(st, 200, cap)
            self.assertEqual(cap["status"], "blocked")
            self.assertEqual(cap["status_scope"], "projects")
            self.assertEqual(cap["blocked_projects"], {TEAM_B: ["gemini_platform"]})
            self.assertEqual(cap["blocked_services"], [])
            self.assertEqual(cap["enforcement_status"], "propagating")
            self.assertEqual(cap["pending_blocked_projects"], {TEAM_B: ["gemini_platform"]})
            self.assertEqual(cap["pending_blocked_services"], [])
            p_row = next(p for p in cap["projects"] if p["project"] == TEAM_B)
            self.assertEqual(p_row["blocked_services"], ["gemini_platform"])
            self.assertEqual(p_row["enforcement_status"], "propagating")

            st, page, _ = srv.request("GET", "/v1/caps/users", token=ADMIN)
            self.assertEqual(st, 200)
            row = next(u for u in page["data"] if u["email"] == "alice@company.com")
            self.assertEqual(row["blocked_projects"], {TEAM_B: ["gemini_platform"]})
            self.assertEqual(row["enforcement_status"], "propagating")

            # 3) Internal planner STILL sees live IAM Deny is out of sync and schedules alice in plan["block"]
            caller = auth.Caller("job", "job", "oidc", frozenset(auth.ALL_SCOPES_SET), "job")
            ctx = svc_base.Ctx(app, app.config, caller)
            payload, _ = caps_svc.compute_caps_status(ctx, fresh=True)
            self.assertIn("alice@company.com", payload["plan"]["block"])

            # 4) Simulate an EXTERNAL Cloud Run Enforcer Job process writing IAM Deny directly in GCP
            # (without touching this API server's in-memory cache!)
            job_app, _ = build_test_app(cfg=cfg, gcp=fake_gcp)
            job_app.store = app.store
            job_ctx = svc_base.Ctx(job_app, job_app.config, caller)
            enf = caps_svc.enforce_cycle(job_ctx, trigger="job", dry_run=False, use_job_if_configured=False)
            self.assertEqual(enf["result"], "success")

            # 5) ?fresh=true bypasses both caps_status AND deny_policy caches -> transitions propagating -> enforced!
            st, cap_after, _ = srv.request("GET", "/v1/caps/users/alice%40company.com?fresh=true", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertEqual(cap_after["blocked_projects"], {TEAM_B: ["gemini_platform"]})
            self.assertEqual(cap_after["enforcement_status"], "enforced")
            self.assertEqual(cap_after["pending_blocked_projects"], {})
            p_row_after = next(p for p in cap_after["projects"] if p["project"] == TEAM_B)
            self.assertEqual(p_row_after["blocked_services"], ["gemini_platform"])
            self.assertEqual(p_row_after["enforcement_status"], "enforced")

            # 6) Unseen active user defaults to enforcement_status='none'
            st, nobody, _ = srv.request("GET", "/v1/caps/users/nobody%40company.com", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertEqual(nobody["enforcement_status"], "none")
            self.assertEqual(nobody["pending_blocked_services"], [])
            self.assertEqual(nobody["pending_blocked_projects"], {})

    def test_pricing_endpoint_returns_canonical_model_ids_and_per_1m_tokens_fields(self):
        cfg = make_test_config(pricing={"source": "auto"})
        app, fake_gcp = build_test_app(cfg=cfg, gcp=GovernanceFakeGcp())
        app.store.mutate("pricing", lambda d: d.update({
            "synced_at": "2026-11-27T00:00:00Z",
            "models": {
                "claude-haiku-4.5": {
                    "source": "cloud_billing_catalog",
                    "input": 1.0,
                    "output": 5.0,
                    "cached": 0.1,
                    "cache_write": 1.25,
                    "tiers": {},
                    "skus": {"input": "services/A/skus/1"},
                },
            },
        }) or d)
        app.store.mutate("model_catalog", lambda d: d.update({
            "models": {
                "claude-haiku-4-5": {"id": "claude-haiku-4-5", "publisher": "anthropic"},
                "claude-haiku-4-5-old": {"id": "claude-haiku-4-5-old", "publisher": "anthropic", "delisted_at": "2026-01-01"},
            },
            "observed": {
                "claude-haiku-4-5@20251001": {"first_seen": "2026-11-01T00:00:00Z"},
            },
        }) or d)

        with LiveTestServer(app) as srv:
            # Query by hyphenated Model Garden ID (matching GET /v1/models)
            st, body, _ = srv.request("GET", "/v1/pricing?model=claude-haiku-4-5", token=ADMIN)
            self.assertEqual(st, 200, body)
            self.assertEqual(len(body["data"]), 1)
            row = body["data"][0]
            self.assertEqual(row["model"], "claude-haiku-4-5")
            self.assertEqual(row["model_key"], "claude-haiku-4.5")
            self.assertEqual(row["model_ids"], ["claude-haiku-4-5", "claude-haiku-4-5@20251001"])
            self.assertEqual(row["input"], 1.0)
            self.assertEqual(row["output"], 5.0)
            self.assertEqual(row["cached"], 0.1)
            self.assertEqual(row["cache_write"], 1.25)
            self.assertEqual(row["input_per_1m_tokens"], 1.0)
            self.assertEqual(row["output_per_1m_tokens"], 5.0)
            self.assertEqual(row["cached_input_per_1m_tokens"], 0.1)
            self.assertEqual(row["cache_write_per_1m_tokens"], 1.25)

            # Query by normalized dotted SKU key still works too
            st, body_dot, _ = srv.request("GET", "/v1/pricing?model=claude-haiku-4.5", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertEqual(len(body_dot["data"]), 1)
            self.assertEqual(body_dot["data"][0]["model"], "claude-haiku-4-5")

            # Override rows also expose canonical fields
            st, body_ov, _ = srv.request("GET", "/v1/pricing?model=gemini-test-pro", token=ADMIN)
            self.assertEqual(st, 200)
            ov = next(r for r in body_ov["data"] if r["source"] == "override")
            self.assertEqual(ov["model_key"], "gemini-test-pro")
            self.assertEqual(ov["input_per_1m_tokens"], ov["input"])
            self.assertEqual(ov["output_per_1m_tokens"], ov["output"])

    def test_openapi_spec_documents_all_9_scopes_and_response_schemas(self):
        with open(os.path.join(config.repo_root(), "openapi.yaml"), encoding="utf-8") as fh:
            spec = yaml.safe_load(fh)
        key_props = spec["paths"]["/v1/admin/api-keys"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]
        self.assertEqual(set(key_props["scopes"]["items"]["enum"]), set(auth.ALL_SCOPES))

        enf_ref = spec["paths"]["/v1/caps/enforce"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        self.assertEqual(enf_ref, "#/components/schemas/EnforceResult")
        schemas = spec["components"]["schemas"]
        self.assertEqual(schemas["EnforceResult"]["properties"]["gates"]["items"]["$ref"],
                         "#/components/schemas/SafetyGate")
        self.assertEqual(set(schemas["SafetyGate"]["properties"]), {"id", "status", "detail"})
        self.assertIn("enforcement_status", schemas["UserCap"]["properties"])
        self.assertIn("pending_blocked_projects", schemas["UserCap"]["properties"])
        self.assertIn("enforcement_status", schemas["ProjectCapStatus"]["properties"])
        self.assertIn("input_per_1m_tokens", schemas["PricingRow"]["properties"])
        self.assertIn("model_ids", schemas["PricingRow"]["properties"])
        self.assertIn("billing_sync", spec["paths"]["/v1/caps/projects"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"])

    def test_parallel_surface_fetch_uid_attribution_and_ttl_caching(self):
        class SpyFakeGcp(GovernanceFakeGcp):
            def __init__(self):
                super().__init__()
                self.parallel_batches: list[set[str]] = []

            def run_parallel(self, tasks: dict, pool=None) -> dict:
                self.parallel_batches.append(set(tasks.keys()))
                return super().run_parallel(tasks, pool=pool)

        spy = SpyFakeGcp()
        now = dt.datetime.now(dt.timezone.utc).replace(second=15, microsecond=0)
        kim_uid = identity.uid_for("kim@company.com")
        spy.log_entries = [
            # Antigravity entry teaches kim@company.com -> kim_uid
            make_inference_entry("kim@company.com", model="gemini-test-pro", prompt=1000, cand=200, ts=now, insert_id="a1"),
            # Gemini Platform entry uses ONLY kim_uid in header label (must still resolve to kim@company.com!)
            make_vertex_entry(user=kim_uid, model="gemini-test-pro", prompt=3000, cand=500, ts=now, insert_id="v1"),
        ]
        app, _ = build_test_app(gcp=spy)

        with LiveTestServer(app) as srv:
            # 1) Cold GET /v1/usage/users fetches antigravity + gemini_platform in ONE parallel batch
            st, u1, _ = srv.request("GET", "/v1/usage/users", token=ADMIN)
            self.assertEqual(st, 200, u1)
            self.assertTrue(any({"antigravity", "gemini_platform"} <= batch for batch in spy.parallel_batches),
                            f"Expected antigravity and gemini_platform in same parallel batch, got {spy.parallel_batches}")
            kim = next(r for r in u1["data"] if r["email"] == "kim@company.com")
            self.assertEqual(kim["requests"], 2)
            self.assertEqual(sorted(kim["surfaces"]), ["antigravity", "gemini_platform"])

            # 2) Repeat GET /v1/usage/users hits usage TTL cache (0 new Cloud Logging calls)
            log_calls_after_1 = sum(1 for _, u, _, _ in spy.rest_calls if "logging.googleapis.com" in u)
            st, u2, _ = srv.request("GET", "/v1/usage/users", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertEqual(u2["data"][0]["requests"], 2)
            log_calls_after_2 = sum(1 for _, u, _, _ in spy.rest_calls if "logging.googleapis.com" in u)
            self.assertEqual(log_calls_after_2, log_calls_after_1)

            # 3) ?fresh=true bypasses cache and updates cached snapshot
            spy.log_entries.append(
                make_inference_entry("kim@company.com", model="gemini-test-pro", prompt=500, cand=100, ts=now, insert_id="a2")
            )
            st, u3, _ = srv.request("GET", "/v1/usage/users?fresh=true", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertEqual(next(r for r in u3["data"] if r["email"] == "kim@company.com")["requests"], 3)
            # Subsequent non-fresh call immediately sees refreshed count=3 without hitting Logging again
            log_calls_after_3 = sum(1 for _, u, _, _ in spy.rest_calls if "logging.googleapis.com" in u)
            st, u4, _ = srv.request("GET", "/v1/usage/users", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertEqual(next(r for r in u4["data"] if r["email"] == "kim@company.com")["requests"], 3)
            self.assertEqual(sum(1 for _, u, _, _ in spy.rest_calls if "logging.googleapis.com" in u), log_calls_after_3)

            # 4) GET /v1/caps/users hits caps_status cache and invalidates immediately on PUT /v1/caps/users/{email}
            st, c1, _ = srv.request("GET", "/v1/caps/users", token=ADMIN)
            self.assertEqual(st, 200)
            calls_c1 = len(spy.rest_calls)
            st, c2, _ = srv.request("GET", "/v1/caps/users", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertEqual(len(spy.rest_calls), calls_c1)

            st, _, _ = srv.request("PUT", "/v1/caps/users/kim%40company.com",
                                   body={"type": "limit", "cost_cap": 42.0}, token=ADMIN)
            self.assertEqual(st, 200)
            st, c3, _ = srv.request("GET", "/v1/caps/users/kim%40company.com", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertEqual(c3["limit"]["cost_cap"], 42.0)

            # 5) GET /v1/model-access hits cache and invalidates immediately on PUT
            st, ma1, _ = srv.request("GET", "/v1/model-access", token=ADMIN)
            self.assertEqual(st, 200)
            org_calls_1 = sum(1 for _, u, _, _ in spy.rest_calls if "orgpolicy.googleapis.com" in u)
            st, ma2, _ = srv.request("GET", "/v1/model-access", token=ADMIN)
            self.assertEqual(st, 200)
            org_calls_2 = sum(1 for _, u, _, _ in spy.rest_calls if "orgpolicy.googleapis.com" in u)
            self.assertEqual(org_calls_2, org_calls_1)

            st, _, _ = srv.request("PUT", "/v1/model-access/projects/test-ai-project",
                                   body={"mode": "deny_list", "models": ["claude-test-sonnet"]}, token=ADMIN)
            self.assertEqual(st, 200)
            st, ma3, _ = srv.request("GET", "/v1/model-access", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertEqual(ma3["data"][0]["mode"], "deny_list")


if __name__ == "__main__":
    unittest.main()
