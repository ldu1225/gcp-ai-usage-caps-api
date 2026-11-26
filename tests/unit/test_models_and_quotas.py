"""Model governance, per-user model rules, and quota management — fully offline end-to-end tests."""

from __future__ import annotations

import base64
import datetime as dt
import json
import unittest

from tests.unit.governance_fakes import GovernanceFakeGcp
from tests.unit.support import LiveTestServer, build_test_app, make_inference_entry, make_test_config
from usage_caps_api.services import usage_events

ADMIN = "gcpai_boot_secret_1234567890"
POLICY = "projects/%s/policies/vertexai.allowedModels"


def push(entry: dict) -> dict:
    return {"message": {"data": base64.b64encode(json.dumps(entry).encode("utf-8")).decode("ascii")}}


class GovernanceTestBase(unittest.TestCase):
    def setUp(self):
        usage_events.reset()
        self.hooks = []

        def sender(url, raw, headers):
            self.hooks.append(json.loads(raw.decode("utf-8")))
            return 200

        cfg = make_test_config(workspace={"projects": ["team-b-project"]},
                               quotas={"contact_email": "cloud-admin@company.com"})
        self.app, self.gcp = build_test_app(cfg=cfg, gcp=GovernanceFakeGcp(), webhook_sender=sender)
        from usage_caps_api.services import webhooks as wh

        def add_hook(doc):
            doc["webhooks"] = [{"id": "wh_000000000001", "name": "portal", "url": "https://portal.example.com/hook",
                                "events": list(wh.SUPPORTED_EVENTS), "enabled": True, "secret": "whsec_test"}]
            return doc
        self.app.store.mutate("webhooks", add_hook)

    def events(self, name):
        return [h for h in self.hooks if h["event"] == name]

    def denied_principals(self):
        out = set()
        for pol in self.gcp.deny_policies.values():
            for rule in pol.get("rules") or []:
                out |= set((rule.get("denyRule") or {}).get("deniedPrincipals") or [])
        return out


class ModelCatalogAndAccessTests(GovernanceTestBase):
    def test_catalog_list_retrieve_and_sync(self):
        with LiveTestServer(self.app) as srv:
            st, page, _ = srv.request("GET", "/v1/models?publisher=anthropic", token=ADMIN)
            self.assertEqual(st, 200)
            ids = [m["id"] for m in page["data"]]
            self.assertIn("claude-test-sonnet", ids)
            self.assertNotIn("count-tokens", ids)  # helper models hidden by default
            self.assertTrue(all(m["object"] == "model" and m["owned_by"] == "anthropic" for m in page["data"]))

            st, model, _ = srv.request("GET", "/v1/models/gemini-next-pro-preview", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertEqual(model["lifecycle"]["stage"], "preview")
            self.assertEqual(model["resource_name"], "publishers/google/models/gemini-next-pro-preview")
            self.assertIn("test-ai-project", model["access_by_project"])
            self.assertEqual(model["access_by_project"]["test-ai-project"]["vertex_ai"]["decision"], "allowed")

            st, priced, _ = srv.request("GET", "/v1/models/gemini-test-pro", token=ADMIN)
            self.assertEqual(priced["pricing"]["input_per_1m_tokens"], 1.25)

            st, err, _ = srv.request("GET", "/v1/models/not-a-real-model-xyz", token=ADMIN)
            self.assertEqual(st, 404)
            self.assertEqual(err["error"]["code"], "not_found")

            st, sync, _ = srv.request("POST", "/v1/models/sync", token=ADMIN, body={})
            self.assertEqual(st, 200)
            self.assertGreaterEqual(sync["discovered"], 6)

    def test_allow_list_patterns_lockout_etag_and_restrict_allow_cycle(self):
        with LiveTestServer(self.app) as srv:
            base = "/v1/model-access/projects/test-ai-project"
            # Lockout prevention
            st, err, _ = srv.request("PUT", base, token=ADMIN, body={"mode": "allow_list", "models": []})
            self.assertEqual(st, 422)
            self.assertEqual(err["error"]["code"], "lockout_prevented")

            # Dry run never writes
            st, dry, _ = srv.request("PUT", base, token=ADMIN, body={"mode": "allow_list", "models": ["gemini-test-*"],
                                                                   "dryRun": True})
            self.assertEqual(st, 200)
            self.assertEqual(dry["status"], "dry_run")
            self.assertNotIn(POLICY % "test-ai-project", self.gcp.org_policies)
            self.assertIn("impact", dry)

            st, res, _ = srv.request("PUT", base, token=ADMIN, body={"mode": "allow_list", "models": ["gemini-test-*"],
                                                                   "reason": "Gemini only"})
            self.assertEqual(st, 200, res)
            self.assertEqual(res["status"], "applied")
            self.assertTrue(res["operation"]["id"].startswith("op_"))
            values = self.gcp.org_policies[POLICY % "test-ai-project"]["spec"]["rules"][0]["values"]["allowedValues"]
            self.assertIn("publishers/google/models/gemini-test-pro:predict", values)
            self.assertIn("publishers/google/models/gemini-test-flash-lite:predict", values)
            self.assertFalse(any("anthropic" in v for v in values))  # no claude helper without claude models

            st, pol, _ = srv.request("GET", base, token=ADMIN)
            self.assertEqual(pol["mode"], "allow_list")
            self.assertTrue(pol["managed_by_api"])
            self.assertEqual(pol["drift"], [])

            # If-Match mismatch -> 412
            st, err, _ = srv.request("PUT", base, token=ADMIN, headers={"If-Match": "stale"},
                                     body={"mode": "allow_list", "models": ["gemini-test-pro"]})
            self.assertEqual(st, 412)

            # Allow a Claude model (adds model + count-tokens helper)
            st, res, _ = srv.request("POST", base + "/models/claude-test-haiku/allow", token=ADMIN, body={})
            self.assertEqual(st, 200, res)
            values = self.gcp.org_policies[POLICY % "test-ai-project"]["spec"]["rules"][0]["values"]["allowedValues"]
            self.assertIn("publishers/anthropic/models/claude-test-haiku:predict", values)
            self.assertIn("publishers/anthropic/models/count-tokens:predict", values)

            # Restricting a pattern-covered model is refused with guidance
            st, err, _ = srv.request("POST", base + "/models/gemini-test-flash/restrict", token=ADMIN, body={})
            self.assertEqual(st, 409)

            st, res, _ = srv.request("POST", base + "/models/claude-test-haiku/restrict", token=ADMIN, body={})
            self.assertEqual(st, 200)
            values = self.gcp.org_policies[POLICY % "test-ai-project"]["spec"]["rules"][0]["values"]["allowedValues"]
            self.assertNotIn("publishers/anthropic/models/claude-test-haiku:predict", values)

            # Effective access reflected in the catalog
            st, model, _ = srv.request("GET", "/v1/models/claude-test-sonnet", token=ADMIN)
            self.assertEqual(model["access_by_project"]["test-ai-project"]["vertex_ai"]["decision"], "denied")

            # Operation polling
            st, op, _ = srv.request("GET", "/v1/operations/%s" % res["operation"]["id"], token=ADMIN)
            self.assertEqual(st, 200)
            self.assertIn(op["status"], ("propagating", "succeeded"))

            # Delete -> inherit
            st, res, _ = srv.request("DELETE", base, token=ADMIN)
            self.assertEqual(res["status"], "applied")
            self.assertNotIn(POLICY % "test-ai-project", self.gcp.org_policies)

            audit_actions = [e["action"] for e in srv.request("GET", "/v1/audit-logs", token=ADMIN)[1]["data"]]
            self.assertIn("model_policy.updated", audit_actions)
            self.assertTrue(self.events("model.access_changed"))

    def test_deny_list_bulk_apply_scope_guard_and_value_limit(self):
        with LiveTestServer(self.app) as srv:
            st, res, _ = srv.request("POST", "/v1/model-access/bulk-apply", token=ADMIN, body={
                "scopes": ["projects/*"], "mode": "deny_list", "models": ["anthropic/*"]})
            self.assertEqual(st, 200, res)
            self.assertEqual(res["applied"], 2)
            for p in ("test-ai-project", "team-b-project"):
                denied = self.gcp.org_policies[POLICY % p]["spec"]["rules"][0]["values"]["deniedValues"]
                self.assertIn("publishers/anthropic/models/claude-test-sonnet:predict", denied)

            st, err, _ = srv.request("PUT", "/v1/model-access/projects/unmanaged-proj", token=ADMIN,
                                     body={"mode": "deny_list", "models": ["claude-test-sonnet"]})
            self.assertEqual(st, 403)
            st, err, _ = srv.request("PUT", "/v1/model-access/folders/123", token=ADMIN,
                                     body={"mode": "deny_list", "models": ["claude-test-sonnet"]})
            self.assertEqual(st, 403)

            many = ["google/gemini-x-%d" % i for i in range(200)]
            st, err, _ = srv.request("PUT", "/v1/model-access/projects/test-ai-project", token=ADMIN,
                                     body={"mode": "deny_list", "models": many, "actions": ["predict", "deploy", "tune"]})
            self.assertEqual(st, 422)

            st, lst, _ = srv.request("GET", "/v1/model-access", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertEqual({p["scope"] for p in lst["data"]}, {"projects/test-ai-project", "projects/team-b-project"})

    def test_orgpolicy_permission_denied_is_reported(self):
        self.gcp.orgpolicy_denied.add("projects/team-b-project")
        with LiveTestServer(self.app) as srv:
            st, err, _ = srv.request("PUT", "/v1/model-access/projects/team-b-project", token=ADMIN,
                                     body={"mode": "deny_list", "models": ["claude-test-sonnet"]})
            self.assertEqual(st, 403)
            self.assertIn("orgpolicy", err["error"]["message"])

    def test_scopes_are_enforced(self):
        with LiveTestServer(self.app) as srv:
            st, key, _ = srv.request("POST", "/v1/admin/api-keys", token=ADMIN,
                                     body={"name": "portal-read", "scopes": ["models:read", "quotas:read"]})
            self.assertEqual(st, 201)
            st, _, _ = srv.request("GET", "/v1/models", token=key["key"])
            self.assertEqual(st, 200)
            st, err, _ = srv.request("PUT", "/v1/model-access/projects/test-ai-project", token=key["key"],
                                     body={"mode": "deny_list", "models": ["claude-test-sonnet"]})
            self.assertEqual(st, 403)
            self.assertEqual(err["error"]["code"], "insufficient_scope")


class SurfaceModelTests(GovernanceTestBase):
    def test_gemini_enterprise_merge_patch_and_not_applicable(self):
        self.gcp.add_ge_app("test-ai-project", "corp-assistant", {"gemini-test-pro": "MODEL_ENABLED"})
        with LiveTestServer(self.app) as srv:
            st, apps, _ = srv.request("GET", "/v1/gemini-enterprise/apps", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertEqual(len(apps["data"]), 1)
            self.assertEqual(apps["projects"]["team-b-project"]["status"], "not_applicable")

            app_id = "test-ai-project~global~corp-assistant"
            st, res, _ = srv.request("PATCH", "/v1/gemini-enterprise/apps/%s/models" % app_id, token=ADMIN,
                                     body={"models": {"claude-test-sonnet": "off"}})
            self.assertEqual(st, 200, res)
            self.assertEqual(res["status"], "updated")
            self.assertEqual(res["after"], {"gemini-test-pro": "on", "claude-test-sonnet": "off"})

            st, model, _ = srv.request("GET", "/v1/models/claude-test-sonnet", token=ADMIN)
            ge = model["access_by_project"]["test-ai-project"]["gemini_enterprise"]
            self.assertEqual(ge["apps"]["test-ai-project/global/corp-assistant"], "disabled")
            self.assertEqual(model["access_by_project"]["team-b-project"]["gemini_enterprise"]["decision"],
                             "not_applicable")

            st, bulk, _ = srv.request("POST", "/v1/gemini-enterprise/apps/bulk-models", token=ADMIN,
                                      body={"apps": ["*"], "models": {"claude-test-sonnet": "default"}})
            self.assertEqual(bulk["updated"], 1)

    def test_antigravity_read_only(self):
        self.gcp.devtools[("test-ai-project", "global")] = {
            "devToolsEnabled": True, "modelConfig": {"allowedModels": ["gemini-test-pro"]}}
        with LiveTestServer(self.app) as srv:
            st, page, _ = srv.request("GET", "/v1/antigravity/models?project=test-ai-project", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertEqual(page["data"][0]["allowed_models"], ["gemini-test-pro"])
            self.assertEqual(page["data"][0]["write_mode"], "console_required")
            st, err, _ = srv.request("PUT", "/v1/antigravity/models", token=ADMIN, body={"models": ["x"]})
            self.assertEqual(st, 409)
            self.assertEqual(err["error"]["code"], "console_required")


class UserModelRuleTests(GovernanceTestBase):
    def test_realtime_violation_blocks_first_and_notifies(self):
        with LiveTestServer(self.app) as srv:
            st, rule, _ = srv.request("PUT", "/v1/model-rules/default", token=ADMIN,
                                      body={"mode": "allow_list", "models": ["gemini-*"]})
            self.assertEqual(st, 200, rule)
            self.assertEqual(rule["rule"]["on_violation"], "block_service")  # default = block first

            ok = make_inference_entry("dev@company.com", model="gemini-test-pro", insert_id="e1")
            st, ack, _ = srv.request("POST", "/v1/internal/usage-event", token=ADMIN, body=push(ok))
            self.assertNotEqual(ack["action"], "model_violation_blocked")

            bad = make_inference_entry("dev@company.com", model="claude-test-sonnet", insert_id="e2")
            st, ack, _ = srv.request("POST", "/v1/internal/usage-event", token=ADMIN, body=push(bad))
            self.assertEqual(st, 200)
            self.assertEqual(ack["action"], "model_violation_blocked")
            self.assertTrue(any("dev@company.com" in p for p in self.denied_principals()))
            viol = self.events("user.model_violation")
            self.assertEqual(len(viol), 1)
            self.assertEqual(viol[0]["data"]["type"], "model_not_allowed")
            self.assertEqual(viol[0]["data"]["action"], "block_service")
            self.assertEqual(viol[0]["data"]["block_services"], ["antigravity"])

            st, cap, _ = srv.request("GET", "/v1/caps/users/dev%40company.com", token=ADMIN)
            self.assertEqual(cap["status"], "blocked")
            self.assertEqual(cap["block_source"], "model_policy")

            # Further events while blocked do not re-block or spam
            bad2 = make_inference_entry("dev@company.com", model="claude-test-sonnet", insert_id="e3")
            srv.request("POST", "/v1/internal/usage-event", token=ADMIN, body=push(bad2))
            self.assertEqual(len(self.events("user.model_violation")), 1)

            # Admin unblock, then a new disallowed request blocks again
            st, _, _ = srv.request("POST", "/v1/users/dev%40company.com/unblock", token=ADMIN, body={})
            self.assertEqual(st, 200)
            self.assertFalse(any("dev@company.com" in p for p in self.denied_principals()))
            bad3 = make_inference_entry("dev@company.com", model="claude-test-sonnet", insert_id="e4")
            st, ack, _ = srv.request("POST", "/v1/internal/usage-event", token=ADMIN, body=push(bad3))
            self.assertEqual(ack["action"], "model_violation_blocked")

            st, vio, _ = srv.request("GET", "/v1/model-violations?email=dev@company.com", token=ADMIN)
            self.assertEqual(len(vio["data"]), 2)

    def test_user_override_notify_only_and_protected_principal(self):
        self.app.config["models"]["protected_principals"] = ["ceo@company.com"]
        with LiveTestServer(self.app) as srv:
            srv.request("PUT", "/v1/model-rules/default", token=ADMIN, body={"mode": "deny_list", "models": ["claude-*"]})
            st, _, _ = srv.request("PUT", "/v1/users/intern%40company.com/model-rules", token=ADMIN,
                                   body={"mode": "deny_list", "models": ["claude-*"], "on_violation": "notify"})
            self.assertEqual(st, 200)
            for i, who in enumerate(("intern@company.com", "ceo@company.com")):
                e = make_inference_entry(who, model="claude-test-sonnet", insert_id="n%d" % i)
                st, ack, _ = srv.request("POST", "/v1/internal/usage-event", token=ADMIN, body=push(e))
                self.assertNotEqual(ack["action"], "model_violation_blocked")
            self.assertEqual(self.denied_principals(), set())
            actions = sorted(h["data"]["action"] for h in self.events("user.model_violation"))
            self.assertEqual(actions, ["notify", "notify"])

    def test_rpm_limit_realtime_and_daily_tokens_scheduled(self):
        now = dt.datetime.now(dt.timezone.utc)
        with LiveTestServer(self.app) as srv:
            st, _, _ = srv.request("PUT", "/v1/rate-limits/users/heavy%40company.com", token=ADMIN,
                                   body={"limits": [{"model": "gemini-*", "rpm": 3}]})
            self.assertEqual(st, 200)
            acks = []
            for i in range(3):
                e = make_inference_entry("heavy@company.com", model="gemini-test-flash", insert_id="r%d" % i, ts=now)
                acks.append(srv.request("POST", "/v1/internal/usage-event", token=ADMIN, body=push(e))[1]["action"])
            self.assertEqual(acks[-1], "model_violation_blocked")
            blk = self.app.store.load("user_caps")[0]["blocks"]["heavy@company.com"]
            self.assertIsNotNone(blk["until"])  # rate blocks lift automatically

            # Scheduled daily token limit for another user via the evaluation endpoint
            srv.request("PUT", "/v1/users/bulk%40company.com/model-rules", token=ADMIN,
                        body={"mode": "inherit", "limits": [{"model": "*", "daily_tokens": 1000}]})
            self.gcp.log_entries = [make_inference_entry("bulk@company.com", model="gemini-test-pro", prompt=5000,
                                                         cand=1000, ts=now)]
            st, dry, _ = srv.request("POST", "/v1/model-rules/evaluate", token=ADMIN, body={"dryRun": True})
            self.assertEqual(st, 200, dry)
            self.assertEqual(dry["blocked"], ["bulk@company.com"])
            self.assertFalse(any("bulk@company.com" in p for p in self.denied_principals()))
            st, run, _ = srv.request("POST", "/v1/model-rules/evaluate", token=ADMIN, body={"dryRun": False})
            self.assertEqual(run["blocked"], ["bulk@company.com"])
            self.assertTrue(any("bulk@company.com" in p for p in self.denied_principals()))
            # Idempotent: a second run does not re-notify
            before = len(self.events("user.model_violation"))
            srv.request("POST", "/v1/model-rules/evaluate", token=ADMIN, body={"dryRun": False})
            self.assertEqual(len(self.events("user.model_violation")), before)

    def test_expired_model_block_is_restored_by_enforcement_cycle(self):
        with LiveTestServer(self.app) as srv:
            srv.request("PUT", "/v1/users/temp%40company.com/model-rules", token=ADMIN,
                        body={"mode": "deny_list", "models": ["claude-*"], "block_minutes": 5})
            e = make_inference_entry("temp@company.com", model="claude-test-haiku", insert_id="t1")
            srv.request("POST", "/v1/internal/usage-event", token=ADMIN, body=push(e))
            self.assertTrue(any("temp@company.com" in p for p in self.denied_principals()))

            def expire(doc):
                doc["blocks"]["temp@company.com"]["until"] = "2000-01-01T00:00:00Z"
                return doc
            self.app.store.mutate("user_caps", expire)
            st, run, _ = srv.request("POST", "/v1/caps/enforce", token=ADMIN, body={"dryRun": False})
            self.assertEqual(st, 200, run)
            self.assertIn("temp@company.com", run["plan"]["restore"])
            self.assertFalse(any("temp@company.com" in p for p in self.denied_principals()))
            self.assertNotIn("temp@company.com", self.app.store.load("user_caps")[0].get("blocks") or {})
            self.assertIn("governance", run)


class FixRegressionTests(GovernanceTestBase):
    def test_notify_only_unblock_removes_existing_deny_entry(self):
        with LiveTestServer(self.app) as srv:
            st, _, _ = srv.request("POST", "/v1/users/eve%40company.com/block", token=ADMIN, body={})
            self.assertEqual(st, 200)
            self.assertTrue(any("eve@company.com" in p for p in self.denied_principals()))
            srv.request("PUT", "/v1/caps/policy", token=ADMIN, body={"enforcement_backend": "notify_only"})
            st, res, _ = srv.request("POST", "/v1/users/eve%40company.com/unblock", token=ADMIN, body={})
            self.assertEqual(st, 200, res)
            self.assertFalse(any("eve@company.com" in p for p in self.denied_principals()))

    def test_lifecycle_and_project_capabilities(self):
        from usage_caps_api.services import base as svc_base, governance_cycle
        from usage_caps_api import auth
        self.app.config["models"]["lifecycle_overrides"] = [
            {"model": "gemini-test-flash-lite", "shutdown_date": (dt.date.today() + dt.timedelta(days=30)).isoformat(),
             "replacement": "gemini-next-flash"}]
        ctx = svc_base.Ctx(self.app, self.app.config, auth.Caller(id="t", name="t", auth_type="job",
                                                                 scopes=frozenset(auth.ALL_SCOPES), rate_key="t"))
        from usage_caps_api.services import model_registry
        model_registry.discover(ctx)  # live catalog (no built-in model list)
        self.assertEqual(governance_cycle.notify_model_lifecycle(ctx)["notified"], 1)
        self.assertEqual(governance_cycle.notify_model_lifecycle(ctx)["notified"], 0)  # deduped
        self.assertEqual(self.events("model.lifecycle_notice")[0]["data"]["replacement"], "gemini-next-flash")
        with LiveTestServer(self.app) as srv:
            st, _, _ = srv.request("GET", "/v1/projects/not-managed-1/capabilities", token=ADMIN)
            self.assertEqual(st, 403)

    def test_local_insecure_caller_has_all_scopes(self):
        from usage_caps_api import auth
        self.assertIn("models:write", auth.ALL_SCOPES_SET | {auth.INTERNAL_SCOPE})


class QuotaTests(GovernanceTestBase):
    def setUp(self):
        super().setUp()
        metric = "aiplatform.googleapis.com/generate_content_requests_per_minute_per_project_per_base_model"
        for p in ("test-ai-project", "team-b-project"):
            self.gcp.add_quota(p, "GenerateContentRequestsPerMinutePerProjectPerBaseModel", metric, [
                ({}, 200), ({"region": "us-central1", "base_model": "gemini-test-pro"}, 1000)])
        self.gcp.timeseries["test-ai-project"] = [{
            "metric": {"labels": {"quota_metric": metric, "base_model": "gemini-test-pro"}},
            "resource": {"labels": {"location": "us-central1"}},
            "points": [{"value": {"int64Value": "850"}}, {"value": {"int64Value": "300"}}],
        }]

    def test_rate_limits_headroom_and_filters(self):
        with LiveTestServer(self.app) as srv:
            st, page, _ = srv.request("GET", "/v1/rate-limits?model=gemini-test-pro", token=ADMIN)
            self.assertEqual(st, 200, page)
            row = next(r for r in page["data"] if r["project_id"] == "test-ai-project")
            self.assertEqual(row["limit"], 1000)
            self.assertEqual(row["usage_peak"], 850)
            self.assertEqual(row["headroom_pct"], 15.0)
            self.assertEqual(row["metric_kind"], "requests_per_minute")
            st, near, _ = srv.request("GET", "/v1/rate-limits?near_limit=true", token=ADMIN)
            self.assertEqual([r["id"] for r in near["data"]], [row["id"]])
            st, one, _ = srv.request("GET", "/v1/projects/test-ai-project/rate-limits/%s" % row["id"], token=ADMIN)
            self.assertEqual(one["id"], row["id"])
            st, err, _ = srv.request("GET", "/v1/projects/unmanaged-x1/rate-limits", token=ADMIN)
            self.assertEqual(st, 403)

    def test_increase_request_lifecycle_idempotency_and_cancel(self):
        with LiveTestServer(self.app) as srv:
            rows = srv.request("GET", "/v1/projects/test-ai-project/rate-limits?model=gemini-test-pro", token=ADMIN)[1]
            lid = rows["data"][0]["id"]
            st, err, _ = srv.request("POST", "/v1/quota-requests", token=ADMIN, body={
                "project_id": "test-ai-project", "limit_id": lid, "requested_value": 5000, "justification": "short"})
            self.assertEqual(st, 400)  # justification too short

            body = {"project_id": "test-ai-project", "limit_id": lid, "requested_value": 5000,
                    "justification": "Launch of internal coding assistant to 2,000 engineers"}
            st, req, _ = srv.request("POST", "/v1/quota-requests", token=ADMIN, body=body,
                                     headers={"Idempotency-Key": "abc-123"})
            self.assertEqual(st, 201, req)
            self.assertEqual(req["status"], "pending")
            self.assertEqual(req["kind"], "increase")
            self.assertEqual(req["previous_value"], 1000)
            pref = self.gcp.quota_prefs[req["preference_name"]]
            self.assertEqual(pref["dimensions"], {"region": "us-central1", "base_model": "gemini-test-pro"})
            self.assertEqual(pref["contactEmail"], "cloud-admin@company.com")

            st, replay, _ = srv.request("POST", "/v1/quota-requests", token=ADMIN, body=body,
                                        headers={"Idempotency-Key": "abc-123"})
            self.assertTrue(replay["idempotent_replay"])
            st, err, _ = srv.request("POST", "/v1/quota-requests", token=ADMIN, body=body)
            self.assertEqual(st, 409)
            self.assertEqual(err["error"]["code"], "quota_request_pending")

            self.gcp.grant(req["preference_name"], 3000)
            st, got, _ = srv.request("GET", "/v1/quota-requests/%s" % req["id"], token=ADMIN)
            self.assertEqual(got["status"], "partially_approved")
            self.assertEqual(got["granted_value"], 3000)
            self.assertTrue(self.events("quota_request.updated"))

            st, cancelled, _ = srv.request("POST", "/v1/quota-requests/%s/cancel" % req["id"], token=ADMIN, body={})
            self.assertEqual(cancelled["status"], "cancelled")

    def test_decrease_guard_bulk_and_dry_run(self):
        with LiveTestServer(self.app) as srv:
            rows = srv.request("GET", "/v1/projects/test-ai-project/rate-limits?model=gemini-test-pro", token=ADMIN)[1]
            lid = rows["data"][0]["id"]
            st, err, _ = srv.request("PATCH", "/v1/projects/test-ai-project/rate-limits/%s" % lid, token=ADMIN,
                                     body={"value": 500})
            self.assertEqual(st, 422)  # below observed peak 850
            st, dec, _ = srv.request("PATCH", "/v1/projects/test-ai-project/rate-limits/%s" % lid, token=ADMIN,
                                     body={"value": 500, "force": True})
            self.assertEqual(st, 200, dec)
            self.assertEqual(dec["kind"], "decrease")
            st, dry, _ = srv.request("POST", "/v1/quota-requests", token=ADMIN, body={
                "projects": ["*"], "quota_id": "GenerateContentRequestsPerMinutePerProjectPerBaseModel",
                "model": "claude-test-sonnet", "location": "us-east5", "requested_value": 300,
                "justification": "Enable Claude for the platform team", "dryRun": True})
            self.assertEqual(st, 200, dry)
            self.assertEqual(len(dry["results"]), 2)
            self.assertTrue(all(r["status"] == "dry_run" for r in dry["results"]))
            self.assertEqual(dry["results"][0]["dimensions"], {"region": "us-east5", "base_model": "claude-test-sonnet"})


class OpenApiCoverageTests(unittest.TestCase):
    def test_every_route_is_documented(self):
        import os
        from usage_caps_api import app as app_mod
        from usage_caps_api.config import repo_root
        with open(os.path.join(repo_root(), "openapi.yaml"), encoding="utf-8") as fh:
            spec = fh.read()
        missing = []
        for method, path in app_mod.ROUTER.routes():
            if ("\n  %s:\n" % path) not in spec:
                missing.append("%s %s" % (method, path))
                continue
            block = "\n" + spec.split("\n  %s:\n" % path, 1)[1].split("\n  /", 1)[0].split("\ncomponents:", 1)[0]
            if ("\n    %s:" % method.lower()) not in block:
                missing.append("%s %s" % (method, path))
        self.assertEqual(missing, [], "Undocumented routes: %s" % missing)


    def test_spec_is_valid_openapi_31(self):
        import os
        from usage_caps_api.config import repo_root
        path = os.path.join(repo_root(), "openapi.yaml")
        try:
            import yaml  # type: ignore
        except ImportError:
            self.skipTest("PyYAML not installed (pip install -r requirements-dev.txt)")
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        self.assertEqual(doc["openapi"], "3.1.0")
        try:
            from openapi_spec_validator import validate  # type: ignore
        except ImportError:
            return
        validate(doc)


if __name__ == "__main__":
    unittest.main()
