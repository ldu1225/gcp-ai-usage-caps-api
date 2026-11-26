"""v1.2 regression suite: no built-in model list, log-only usage (incl. Vertex AI inference request logs with per-user
attribution), official-SKU pricing without Billing Export, and multi-project users (per-project caps & blocks)."""

from __future__ import annotations

import datetime as dt
import json
import os
import unittest

from tests.unit.governance_fakes import GovernanceFakeGcp
from tests.unit.support import (LiveTestServer, build_test_app, make_audit_entry, make_inference_entry,
                                make_test_config, make_vertex_entry)
from usage_caps_api import auth, config
from usage_caps_api.services import base as svc_base, model_registry, pricing, pricing_sync, usage, usage_events

ADMIN = "gcpai_boot_secret_1234567890"
HUB = "test-ai-project"
TEAM_B = "team-b-project"


def _ctx(app, **kw):
    caller = auth.Caller("t", "t", "api_key", frozenset(auth.ALL_SCOPES_SET), "t")
    return svc_base.Ctx(app, app.config, caller, **kw)


class _Base(unittest.TestCase):
    cfg_overrides: dict = {}

    def setUp(self):
        usage_events.reset()
        self.hooks = []

        def sender(url, raw, headers):
            self.hooks.append(json.loads(raw.decode("utf-8")))
            return 200

        cfg = make_test_config(**config.deep_merge({"workspace": {"projects": [TEAM_B]}}, self.cfg_overrides))
        self.app, self.gcp = build_test_app(cfg=cfg, gcp=GovernanceFakeGcp(), webhook_sender=sender)
        from usage_caps_api.services import webhooks as wh

        def add_hook(doc):
            doc["webhooks"] = [{"id": "wh_000000000001", "name": "portal", "url": "https://portal.example.com/hook",
                                "events": list(wh.SUPPORTED_EVENTS), "enabled": True, "secret": "whsec_test"}]
            return doc
        self.app.store.mutate("webhooks", add_hook)

    def events(self, name):
        return [h for h in self.hooks if h["event"] == name]

    def denied_at(self, project):
        out = set()
        for url, pol in self.gcp.deny_policies.items():
            if ("projects%%2F%s/" % project) in url:
                for rule in pol.get("rules") or []:
                    out |= set((rule.get("denyRule") or {}).get("deniedPrincipals") or [])
        return out


class NoHardcodedModelsTests(_Base):
    def test_source_has_no_builtin_model_list(self):
        self.assertFalse(hasattr(model_registry, "SEED_MODELS"))
        self.assertFalse(hasattr(model_registry, "_PUBLISHER_GUESS"))
        root = os.path.join(config.repo_root(), "src", "usage_caps_api")
        import re
        rx = re.compile(r"(gemini|claude)-\d")
        for dirpath, _, files in os.walk(root):
            for f in files:
                if f.endswith(".py"):
                    with open(os.path.join(dirpath, f), encoding="utf-8") as fh:
                        self.assertIsNone(rx.search(fh.read()), "hardcoded model id in %s" % f)

    def test_catalog_is_live_plus_observed_and_new_models_fire_first_seen(self):
        self.gcp.publisher_models = {"google": [], "anthropic": []}
        now = dt.datetime.now(dt.timezone.utc)
        # A model that exists nowhere in code / Model Garden, used on Vertex AI by a brand-new publisher.
        self.gcp.log_entries = [
            make_vertex_entry(model="zeta-omni-9", publisher="newvendor", user="dana@company.com", ts=now),
            make_inference_entry("erin@company.com", model="gemini-test-ultra", ts=now),
        ]
        self.app.store.mutate("model_catalog", lambda d: d.update(observed={}) or d)  # baseline already taken
        with LiveTestServer(self.app) as srv:
            st, page, _ = srv.request("GET", "/v1/models", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertEqual(page["data"], [])  # nothing built in
            st, _, _ = srv.request("GET", "/v1/usage/users", token=ADMIN)
            self.assertEqual(st, 200)
            st, page, _ = srv.request("GET", "/v1/models?source=observed", token=ADMIN)
            ids = {m["id"]: m for m in page["data"]}
            self.assertIn("zeta-omni-9", ids)
            self.assertEqual(ids["zeta-omni-9"]["publisher"], "newvendor")
            self.assertEqual(ids["zeta-omni-9"]["surfaces"], ["vertex_ai"])
            self.assertEqual(ids["zeta-omni-9"]["resource_name"], "publishers/newvendor/models/zeta-omni-9")
            self.assertIn("gemini-test-ultra", ids)
            first = sorted(e["data"]["model"] for e in self.events("model.first_seen"))
            self.assertEqual(first, ["gemini-test-ultra", "zeta-omni-9"])
            srv.request("GET", "/v1/usage/users", token=ADMIN)
            self.assertEqual(len(self.events("model.first_seen")), 2)  # once per model
            # Publishers seen in logs are scanned on the next Model Garden sync.
            st, sync, _ = srv.request("POST", "/v1/models/sync", token=ADMIN, body={})
            self.assertIn("newvendor", sync["publishers"])
            st, um, _ = srv.request("GET", "/v1/usage/models", token=ADMIN)
            row = next(r for r in um["data"] if r["model"] == "zeta-omni-9")
            self.assertEqual(row["catalog_status"], "observed_only")
            self.assertEqual(row["publisher"], "newvendor")

    def test_model_garden_delisting_and_catalog_changed_webhook(self):
        ctx = _ctx(self.app)
        model_registry.discover(ctx)
        self.gcp.publisher_models["google"] = [m for m in self.gcp.publisher_models["google"]
                                               if not m["name"].endswith("gemini-test-flash-lite")]
        self.gcp.publisher_models["google"].append({"name": "publishers/google/models/gemini-brand-new",
                                                    "launchStage": "PUBLIC_PREVIEW"})
        res = model_registry.discover(ctx)
        self.assertEqual(res["added"], ["gemini-brand-new"])
        self.assertEqual(res["delisted"], ["gemini-test-flash-lite"])
        cat = model_registry.load_catalog(ctx, allow_discovery=False)
        self.assertIsNotNone(cat["gemini-test-flash-lite"]["delisted_at"])
        self.assertEqual(len(self.events("model.catalog_changed")), 1)


class ObservationBaselineTests(_Base):
    def test_first_observation_is_a_silent_baseline(self):
        self.gcp.log_entries = [make_inference_entry("erin@company.com", model="gemini-test-ultra")]
        ctx = _ctx(self.app)
        now = dt.datetime.now(dt.timezone.utc)
        usage.collect_usage(ctx, now - dt.timedelta(hours=1), now + dt.timedelta(minutes=1))
        self.assertEqual(self.events("model.first_seen"), [])
        self.assertIn("gemini-test-ultra", self.app.store.load("model_catalog")[0]["observed"])
        self.gcp.log_entries.append(make_inference_entry("erin@company.com", model="gemini-test-mega"))
        usage.collect_usage(ctx, now - dt.timedelta(hours=1), now + dt.timedelta(minutes=1))
        self.assertEqual([e["data"]["model"] for e in self.events("model.first_seen")], ["gemini-test-mega"])


class VertexAttributionTests(_Base):
    def test_header_label_audit_correlation_unattributed_and_claude(self):
        now = dt.datetime.now(dt.timezone.utc).replace(second=10, microsecond=0)
        uid = __import__("usage_caps_api.identity", fromlist=["uid_for"]).uid_for("kim@company.com")
        self.app.store.mutate("user_directory", lambda d: d.update(uids={uid: "kim@company.com"}) or d)
        self.gcp.log_entries = [
            # exact: portal sent the header label (email or uid)
            make_vertex_entry(user="lee@company.com", prompt=1000, cand=200, thoughts=50, ts=now, insert_id="v1"),
            make_vertex_entry(user=uid, prompt=500, cand=100, ts=now, insert_id="v2"),
            # no header: 2 requests split 3:1 across same-minute audit callers
            make_vertex_entry(prompt=4000, cand=400, ts=now, insert_id="v3"),
            make_audit_entry("park@company.com", ts=now), make_audit_entry("park@company.com", ts=now),
            make_audit_entry("park@company.com", ts=now), make_audit_entry("choi@company.com", ts=now),
            # no header + no audit in that project/minute -> unattributed (kept, never dropped)
            make_vertex_entry(project=TEAM_B, model="claude-test-sonnet", publisher="anthropic", ts=now,
                              claude={"input_tokens": 100, "cache_read_input_tokens": 300,
                                      "cache_creation_input_tokens": 50, "output_tokens": 80},
                              principal="app@x.iam.gserviceaccount.com", insert_id="v4"),
            # failed request is ignored
            make_vertex_entry(user="lee@company.com", error=True, ts=now, insert_id="v5"),
        ]
        ctx = _ctx(self.app)
        res = usage.collect_usage(ctx, now - dt.timedelta(hours=1), now + dt.timedelta(minutes=5),
                                  surfaces=["vertex_ai"])
        self.assertTrue(res.ok)
        lee = res.users["lee@company.com"]
        self.assertEqual((lee["requests"], lee["input_tokens"], lee["output_tokens"], lee["thoughts_tokens"]),
                         (1, 1000, 250, 50))
        self.assertEqual(res.users["kim@company.com"]["input_tokens"], 500)
        self.assertEqual(res.users["park@company.com"]["input_tokens"], 3000)
        self.assertEqual(res.users["choi@company.com"]["input_tokens"], 1000)
        un = res.unattributed[TEAM_B]
        self.assertEqual(un["input_tokens"], 450)       # 100 + 300 cache read + 50 cache write
        self.assertEqual(un["cached_tokens"], 300)
        self.assertEqual(un["cache_write_tokens"], 50)
        self.assertEqual(un["reasons"], {"service_account": 1})
        self.assertEqual(res.attribution["header_label"], 2)
        self.assertEqual(res.attribution["unattributed"], 1)
        self.assertEqual(res.token_sources["vertex_ai"], "logs")

    def test_inference_logs_missing_falls_back_to_audit_request_counts(self):
        now = dt.datetime.now(dt.timezone.utc)
        self.gcp.log_entries = [make_audit_entry("park@company.com", ts=now)]
        ctx = _ctx(self.app)
        res = usage.collect_usage(ctx, now - dt.timedelta(hours=1), now + dt.timedelta(minutes=1),
                                  surfaces=["vertex_ai"])
        self.assertEqual(res.token_sources.get("vertex_ai"), "requests_only")
        self.assertEqual(res.users["park@company.com"]["requests"], 1)
        self.assertTrue(any(w["code"] == "inference_logs_missing" for w in ctx.warnings))

    def test_vertex_logging_status_and_enable(self):
        state = {"v": None}

        def handler(method, url, params, body):
            if url.endswith(":fetchPublisherModelConfig"):
                return {"publisherModelConfig": {"inferenceEventLoggingConfig": {"state": state["v"]}}} if state["v"] else {}
            state["v"] = body["publisherModelConfig"]["inferenceEventLoggingConfig"]["state"]
            return {"name": "operations/1"}
        self.gcp.rest_handlers.append((lambda m, u, p, b: "PublisherModelConfig" in u, handler))
        with LiveTestServer(self.app) as srv:
            st, cur, _ = srv.request("GET", "/v1/projects/%s/vertex-logging" % HUB, token=ADMIN)
            self.assertEqual((st, cur["enabled"]), (200, False))
            st, dry, _ = srv.request("POST", "/v1/vertex-logging/enable", token=ADMIN, body={"projects": ["*"]})
            self.assertTrue(all(r["action"] == "would_enable" for r in dry["data"]))
            st, res, _ = srv.request("POST", "/v1/projects/%s/vertex-logging/enable" % HUB, token=ADMIN, body={})
            self.assertEqual((st, res["action"], res["state"]), (200, "enabled", "ENABLED"))
            st, cur, _ = srv.request("GET", "/v1/projects/%s/vertex-logging" % HUB, token=ADMIN)
            self.assertTrue(cur["enabled"])
            st, _, _ = srv.request("GET", "/v1/projects/not-managed-x/vertex-logging", token=ADMIN)
            self.assertEqual(st, 404)


class OfficialSkuPricingTests(_Base):
    cfg_overrides = {"pricing": {"source": "auto", "model_overrides": []}}

    def _catalog(self):
        def sku(desc, price_nanos, units=0, sid="s"):
            return {"name": "services/AAAA-BBBB-CCCC/skus/%s" % sid, "skuId": sid, "description": desc,
                    "pricingInfo": [{"pricingExpression": {"usageUnit": "count", "usageUnitDescription": "count",
                                                           "baseUnitConversionFactor": 1,
                                                           "tieredRates": [{"unitPrice": {"units": units,
                                                                                          "nanos": price_nanos}}]},
                                     "currencyConversionRate": 1, "effectiveTime": "2026-11-01T00:00:00Z"}]}
        per = lambda usd_per_m: int(usd_per_m * 1000)  # nanos per token  # noqa: E731
        skus = [
            sku("Gemini Test Pro Input Text Token Count", per(2.0), sid="in"),
            sku("Gemini Test Pro Output Text Token Count", per(12.0), sid="out"),
            sku("Gemini Test Pro Cached Input Text Token Count", per(0.5), sid="cache"),
            sku("Gemini Test Pro Long Input Text Token Count (> 200K)", per(4.0), sid="lin"),
            sku("Gemini Test Pro Long Output Text Token Count (> 200K)", per(18.0), sid="lout"),
            sku("Gemini Test Pro Flex Input Text Token Count", per(1.0), sid="fin"),
            sku("Gemini Test Pro Flex Output Text Token Count", per(6.0), sid="fout"),
            sku("Gemini Test Pro Priority Input Text Token Count", per(3.6), sid="pin"),
            sku("Gemini Test Pro Batch Input Text Token Count", per(1.0), sid="bin"),
            sku("Gemini Test Pro Input Audio Token Count", per(9.0), sid="audio"),
            sku("Code Assist Gemini Test Pro Input Token Count", per(99.0), sid="ca"),
            sku("Claude Test Sonnet Input Token Count", per(3.0), sid="cin"),
            sku("Claude Test Sonnet Output Token Count", per(15.0), sid="cout"),
            sku("Claude Test Sonnet Prompt Caching Read Token Count", per(0.3), sid="cr"),
            sku("Claude Test Sonnet Prompt Caching Write Token Count", per(3.75), sid="cw"),
        ]
        self.gcp.rest_handlers.append((lambda m, u, p, b: "cloudbilling.googleapis.com/v1/services/" in u
                                       and u.endswith("/skus"), {"skus": skus}))

    def test_sync_tiers_and_cost_without_billing_export(self):
        self._catalog()
        ctx = _ctx(self.app)
        model_registry.discover(ctx)   # model names come from the live catalog, never from code
        out = pricing_sync.sync_pricing(ctx)
        self.assertGreaterEqual(out["models_synced"], 2)
        doc = self.app.store.load("pricing")[0]
        gp = doc["models"]["gemini-test-pro"]
        self.assertEqual((gp["input"], gp["output"], gp["cached"]), (2.0, 12.0, 0.5))
        self.assertEqual(gp["tiers"]["long_context"]["input"], 4.0)
        self.assertEqual(gp["tiers"]["long_context"]["threshold_tokens"], 200000)
        self.assertEqual(gp["tiers"]["flex"]["output"], 6.0)
        self.assertEqual(gp["tiers"]["priority"]["input"], 3.6)
        self.assertEqual(gp["tiers"]["batch"]["input"], 1.0)
        self.assertEqual(gp["skus"]["input"], "services/AAAA-BBBB-CCCC/skus/in")
        cl = doc["models"]["claude-test-sonnet"]
        self.assertEqual((cl["cached"], cl["cache_write"]), (0.3, 3.75))
        pr = pricing.resolve_pricing(ctx)
        # 1M on-demand in / 100k out, 1M flex in / 100k flex out, one 300k long-context request
        rec = {"by_model": {"gemini-test-pro": {
            "input_tokens": 2_300_000, "output_tokens": 210_000, "cached_tokens": 0, "total_tokens": 2_510_000,
            "long_input_tokens": 300_000, "long_output_tokens": 10_000,
            "by_traffic": {"on_demand": {"input_tokens": 1_300_000, "output_tokens": 110_000},
                           "flex": {"input_tokens": 1_000_000, "output_tokens": 100_000}}}}}
        cost, partial = pricing.estimate_cost(rec, pr)
        expected = (1.0 * 2.0 + 0.1 * 12.0) + (0.3 * 4.0 + 0.01 * 18.0) + (1.0 * 1.0 + 0.1 * 6.0)
        self.assertFalse(partial)
        self.assertAlmostEqual(cost, round(expected, 6), places=6)
        claude = {"by_model": {"claude-test-sonnet": {"input_tokens": 1_000_000, "cached_tokens": 500_000,
                                                      "cache_write_tokens": 100_000, "output_tokens": 0,
                                                      "total_tokens": 1_000_000}}}
        self.assertAlmostEqual(pricing.estimate_cost(claude, pr)[0], 0.4 * 3.0 + 0.5 * 0.3 + 0.1 * 3.75, places=6)
        with LiveTestServer(self.app) as srv:
            st, table, _ = srv.request("GET", "/v1/pricing?model=gemini-test-pro", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertEqual(table["pricing_source"]["billing_export"], "not_used")
            self.assertEqual(table["data"][0]["tiers"]["flex"]["input"], 1.0)

    def test_new_model_family_learned_from_logs_is_priced(self):
        self._catalog()
        self.gcp.rest_handlers.insert(0, (lambda m, u, p, b: u.endswith("/skus"), {"skus": [
            {"name": "services/X/skus/z1", "description": "Zeta Omni 9 Input Token Count",
             "pricingInfo": [{"pricingExpression": {"usageUnit": "count", "baseUnitConversionFactor": 1,
                                                    "tieredRates": [{"unitPrice": {"nanos": 5000}}]}}]},
            {"name": "services/X/skus/z2", "description": "Zeta Omni 9 Output Token Count",
             "pricingInfo": [{"pricingExpression": {"usageUnit": "count", "baseUnitConversionFactor": 1,
                                                    "tieredRates": [{"unitPrice": {"nanos": 20000}}]}}]}]}))
        ctx = _ctx(self.app)
        self.app.store.mutate("model_catalog", lambda d: d.update(observed={"zeta-omni-9": {"publisher": "newvendor"}}) or d)
        pricing_sync.sync_pricing(ctx)
        z = self.app.store.load("pricing")[0]["models"]["zeta-omni-9"]
        self.assertEqual((z["input"], z["output"]), (5.0, 20.0))

    def test_config_has_no_billing_export_settings(self):
        cfg = config.load_config(environ={"BILLING_DATASET_ID": "x", "BILLING_TABLE_ID": "y",
                                          "BILLING_ACCOUNT_ID": "012345-6789AB-CDEF01"})
        self.assertEqual(cfg["workspace"]["billing"], {"account_id": "012345-6789AB-CDEF01"})
        self.assertFalse(hasattr(svc_base.Ctx, "billing_source"))


class MultiProjectUserTests(_Base):
    cfg_overrides = {"caps": {"enforcement_mode": "auto", "change_budget_min": 5},
                     "pricing": {"model_overrides": [{"model": "gemini-test-pro", "input": 1.0, "output": 10.0}]}}

    def _usage(self):
        now = dt.datetime.now(dt.timezone.utc)
        # sam works in both projects: $1.50 in the hub project, $15.00 in team-b
        self.gcp.log_entries = [
            make_inference_entry("sam@company.com", project=HUB, model="gemini-test-pro",
                                 prompt=500_000, cand=100_000, ts=now, insert_id="a1"),
            make_inference_entry("sam@company.com", project=TEAM_B, model="gemini-test-pro",
                                 prompt=5_000_000, cand=1_000_000, ts=now, insert_id="b1"),
        ]

    def test_user_projects_endpoint_and_per_project_breakdown(self):
        self._usage()
        with LiveTestServer(self.app) as srv:
            st, up, _ = srv.request("GET", "/v1/users/sam%40company.com/projects", token=ADMIN)
            self.assertEqual(st, 200, up)
            self.assertTrue(up["multi_project"])
            rows = {r["project"]: r for r in up["data"]}
            self.assertEqual(set(rows), {HUB, TEAM_B})
            self.assertAlmostEqual(rows[TEAM_B]["estimated_cost"], 15.0)
            self.assertAlmostEqual(rows[HUB]["estimated_cost"], 1.5)
            st, lst, _ = srv.request("GET", "/v1/usage/users", token=ADMIN)
            sam = lst["data"][0]
            self.assertTrue(sam["multi_project"])
            self.assertEqual(sam["projects"], sorted([HUB, TEAM_B]))
            self.assertEqual(lst["summary"]["multi_project_users"], 1)

    def test_per_project_cap_suspends_only_the_overspent_project(self):
        self._usage()
        with LiveTestServer(self.app) as srv:
            st, pol, _ = srv.request("PUT", "/v1/caps/policy", token=ADMIN,
                                     body={"cap_scope": "per_project", "cost_cap": 10.0})
            self.assertEqual((st, pol["cap_scope"]), (200, "per_project"))
            st, cap, _ = srv.request("GET", "/v1/caps/users/sam%40company.com", token=ADMIN)
            self.assertEqual(cap["status"], "over_cap")
            self.assertEqual(cap["over_scope"], [TEAM_B])
            st, run, _ = srv.request("POST", "/v1/caps/enforce", token=ADMIN, body={"dryRun": False})
            self.assertEqual(st, 200, run)
            self.assertEqual(run["plan"]["suspend_projects"], {"sam@company.com": [TEAM_B]})
            self.assertTrue(any("sam@company.com" in p for p in self.denied_at(TEAM_B)))
            self.assertFalse(any("sam@company.com" in p for p in self.denied_at(HUB)))
            st, cap, _ = srv.request("GET", "/v1/caps/users/sam%40company.com", token=ADMIN)
            self.assertEqual((cap["status"], cap["status_scope"]), ("suspended", "projects"))
            prow = {p["project"]: p for p in cap["projects"]}
            self.assertEqual(prow[TEAM_B]["status"], "suspended")
            self.assertEqual(prow[HUB]["status"], "active")
            self.assertTrue(any(e["data"].get("project") == TEAM_B for e in self.events("cap.exceeded")))
            blocked = [e for e in self.events("user.blocked") if e["data"]["email"] == "sam@company.com"]
            self.assertEqual(blocked[-1]["data"]["projects"], [TEAM_B])

    def test_total_cap_and_user_project_caps(self):
        self._usage()
        with LiveTestServer(self.app) as srv:
            st, res, _ = srv.request("PUT", "/v1/caps/users/sam%40company.com", token=ADMIN, body={
                "type": "limit", "cost_cap": 100.0, "project_caps": {HUB: {"cost_cap": 1.0}}})
            self.assertEqual(st, 200, res)
            st, cap, _ = srv.request("GET", "/v1/caps/users/sam%40company.com", token=ADMIN)
            self.assertEqual(cap["over_scope"], [HUB])
            self.assertEqual({p["project"]: p["limit"]["source"] if p["limit"] else None for p in cap["projects"]},
                             {HUB: "user_project_cap", TEAM_B: None})
            st, err, _ = srv.request("PUT", "/v1/caps/users/sam%40company.com", token=ADMIN, body={
                "type": "limit", "project_caps": {"unmanaged-proj-x": {"cost_cap": 1.0}}})
            self.assertEqual(st, 400)

    def test_project_scoped_manual_block_and_unblock(self):
        with LiveTestServer(self.app) as srv:
            st, res, _ = srv.request("POST", "/v1/users/sam%40company.com/block", token=ADMIN,
                                     body={"projects": [TEAM_B], "services": ["vertex_ai"]})
            self.assertEqual((st, res["scope"]), (200, "projects"), res)
            self.assertTrue(any("sam@company.com" in p for p in self.denied_at(TEAM_B)))
            self.assertFalse(any("sam@company.com" in p for p in self.denied_at(HUB)))
            st, res, _ = srv.request("POST", "/v1/users/sam%40company.com/block", token=ADMIN, body={})
            self.assertTrue(any("sam@company.com" in p for p in self.denied_at(HUB)))
            st, res, _ = srv.request("POST", "/v1/users/sam%40company.com/unblock", token=ADMIN,
                                     body={"projects": [HUB]})
            self.assertEqual(st, 200, res)
            self.assertEqual(res["blocked_projects"], [TEAM_B])
            self.assertFalse(any("sam@company.com" in p for p in self.denied_at(HUB)))
            self.assertTrue(any("sam@company.com" in p for p in self.denied_at(TEAM_B)))
            st, res, _ = srv.request("POST", "/v1/users/sam%40company.com/unblock", token=ADMIN, body={})
            self.assertEqual(res["status"], "active")
            self.assertFalse(any("sam@company.com" in p for p in self.denied_at(TEAM_B)))
            st, err, _ = srv.request("POST", "/v1/users/sam%40company.com/block", token=ADMIN,
                                     body={"projects": ["unmanaged-proj-x"]})
            self.assertEqual(st, 400)

    def test_model_violation_blocks_only_the_violating_project(self):
        import base64
        with LiveTestServer(self.app) as srv:
            srv.request("PUT", "/v1/model-rules/default", token=ADMIN, body={"mode": "deny_list", "models": ["claude-*"]})
            e = make_inference_entry("sam@company.com", project=TEAM_B, model="claude-test-sonnet", insert_id="m1")
            body = {"message": {"data": base64.b64encode(json.dumps(e).encode()).decode()}}
            st, ack, _ = srv.request("POST", "/v1/internal/usage-event", token=ADMIN, body=body)
            self.assertEqual(ack["action"], "model_violation_blocked")
            self.assertTrue(any("sam@company.com" in p for p in self.denied_at(TEAM_B)))
            self.assertFalse(any("sam@company.com" in p for p in self.denied_at(HUB)))
            v = self.events("user.model_violation")[0]["data"]
            self.assertEqual((v["projects"], v["block_projects"]), ([TEAM_B], [TEAM_B]))

    def test_realtime_vertex_event_with_header_label(self):
        import base64
        self.app.store.mutate("policy", lambda d: d.update(policy={"cost_cap": 0.01}) or d)
        e = make_vertex_entry(project=TEAM_B, user="sam@company.com", prompt=100_000, cand=10_000, insert_id="rv1")
        body = {"message": {"data": base64.b64encode(json.dumps(e).encode()).decode()}}
        with LiveTestServer(self.app) as srv:
            st, ack, _ = srv.request("POST", "/v1/internal/usage-event", token=ADMIN, body=body)
            self.assertEqual(st, 200, ack)
            self.assertIn(ack["action"], ("enforcement_triggered", "under_cap", "over_cap_manual_mode"))
            self.assertNotEqual(ack.get("reason"), "not_matching_inference_entry")


if __name__ == "__main__":
    unittest.main()
