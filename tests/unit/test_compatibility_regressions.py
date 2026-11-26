"""Regression tests for bare-metal, multi-location, macOS, and live GCP payload compatibility fixes."""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import os
import unittest
from unittest import mock

from tests.unit.support import build_test_app, make_test_config
from usage_caps_api import auth, config, identity, schema
from usage_caps_api.gcp import GcpError
from usage_caps_api.services import base as svc_base, enforcers, usage, vertex_logging


def _load_e2e_module():
    path = os.path.join(config.repo_root(), "scripts", "e2e_live_test.py")
    spec = importlib.util.spec_from_file_location("e2e_live_test_reg", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ctx(app):
    caller = auth.Caller("admin", "admin", "api_key", frozenset(["admin", "usage:read", "caps:read", "caps:write"]), "a")
    return svc_base.Ctx(app, app.config, caller)


class CompatibilityRegressionTests(unittest.TestCase):
    def tearDown(self):
        identity.configure()

    def test_vertex_logging_404_on_bare_metal_project_is_readable_disabled(self):
        app, gcp = build_test_app()
        def err_404(method, url, params, body):
            raise GcpError("not_found", status=404, message="GenAiConfig of projects/test-ai-project is not found.")
        gcp.rest_handlers.append((lambda m, u, p, b: u.endswith(":fetchPublisherModelConfig"), err_404))
        st = vertex_logging.status(_ctx(app), "test-ai-project")
        self.assertEqual(st["enabled"], False)
        self.assertEqual(st["state"], "STATE_UNSPECIFIED")
        self.assertTrue(st["readable"])
        self.assertIsNone(st["error"])

    def test_parse_vertex_entry_principal_id_and_numeric_resource_container(self):
        identity.configure(service_account_subjects=True)
        entry = {
            "insertId": "live-1",
            "logName": "projects/duleetest/logs/aiplatform.googleapis.com%2Finference_request",
            "timestamp": "2026-11-26T14:27:20Z",
            "severity": "INFO",
            "resource": {
                "type": "aiplatform.googleapis.com/PublisherModel",
                "labels": {
                    "location": "global",
                    "publisher": "google",
                    "model_user_id": "gemini-2.5-flash",
                    "resource_container": "projects/484712896449",
                },
            },
            "jsonPayload": {
                "requestStartTime": "2026-11-26T14:27:19Z",
                "trafficType": "ON_DEMAND",
                "principal": {
                    "id": "caps-e2e-tester@duleetest.iam.gserviceaccount.com",
                    "type": "SERVICE_ACCOUNT",
                },
                "generateContentDetails": {
                    "usageMetadata": {
                        "promptTokenCount": 7,
                        "candidatesTokenCount": 1,
                        "thoughtsTokenCount": 27,
                        "totalTokenCount": 35,
                    }
                },
            },
        }
        parsed = usage.parse_vertex_entry(entry, header_label="user", long_ctx=200000)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["project"], "duleetest")
        self.assertEqual(parsed["principal"], "caps-e2e-tester@duleetest.iam.gserviceaccount.com")
        self.assertEqual(parsed["counts"]["output_tokens"], 28)

        # Attribution without custom header resolves SA via log_identity
        attributed = list(usage.attribute_vertex([parsed], [], {}))
        self.assertEqual(len(attributed), 1)
        person, kind, _, counts = attributed[0]
        self.assertEqual(person, "caps-e2e-tester@duleetest.iam.gserviceaccount.com")
        self.assertEqual(kind, "log_identity")
        self.assertEqual(counts["total_tokens"], 35)

    def test_bq_vertex_fields_supports_principal_id_and_json_http_headers(self):
        s = schema.TableSchema.from_dicts([
            {"name": "timestamp", "type": "TIMESTAMP"},
            {"name": "logName", "type": "STRING"},
            {"name": "resource", "type": "STRUCT", "fields": [
                {"name": "labels", "type": "STRUCT", "fields": [
                    {"name": "resource_container", "type": "STRING"},
                    {"name": "publisher", "type": "STRING"},
                    {"name": "model_user_id", "type": "STRING"},
                ]},
            ]},
            {"name": "jsonPayload", "type": "JSON"},
        ])
        src = schema.TableSource("proj-a", "ge_analytics", svc_base.VERTEX_INFERENCE_SINK, s, location="US")
        f = usage._vertex_fields(src)
        self.assertIn("$.principal.id", f["principal"])
        hpaths = usage.payload_paths(s, f["header_tpl"].format(label="user"))
        user_sql = schema.expr("t", s, *[c + ".values[0]" for c in hpaths], *[c + ".values" for c in hpaths],
                               *hpaths, cast="STRING")
        self.assertIn(".values[0]", user_sql)

    def test_multi_location_bq_queries_each_table_source_independently(self):
        cfg = make_test_config(
            workspace={"project_id": "proj-us", "projects": ["proj-seoul"], "audit_dataset": "ge_analytics",
                       "audit_project": "", "scan_all_projects": False},
            caps={"usage_source": "bigquery"},
        )
        app, gcp = build_test_app(cfg=cfg)
        ge_act_schema = schema.TableSchema.from_dicts([
            {"name": "timestamp", "type": "TIMESTAMP"},
            {"name": "logName", "type": "STRING"},
            {"name": "jsonPayload", "type": "STRUCT", "fields": [{"name": "userIamPrincipal", "type": "STRING"}]},
        ])
        ge_msg_schema = schema.TableSchema.from_dicts([
            {"name": "timestamp", "type": "TIMESTAMP"},
            {"name": "logName", "type": "STRING"},
            {"name": "jsonPayload", "type": "STRUCT", "fields": [{"name": "user", "type": "STRING"}]},
        ])
        src_us = schema.TableSource("proj-us", "ge_analytics", svc_base.GE_ACTIVITY, ge_act_schema, location="US")
        src_kr = schema.TableSource("proj-seoul", "ge_analytics", svc_base.GE_MESSAGE, ge_msg_schema, location="asia-northeast3")

        def resolve(proj, ds, prefix, **kw):
            if proj == "proj-us" and prefix == svc_base.GE_ACTIVITY:
                return src_us
            if proj == "proj-seoul" and prefix == svc_base.GE_MESSAGE:
                return src_kr
            return None

        app.registry.resolve = resolve
        now = dt.datetime.now(dt.timezone.utc)
        queries_seen = []

        def handle_q(sql, params, mod):
            queries_seen.append(sql)
            # Must NEVER UNION ALL across US and asia-northeast3!
            self.assertNotIn("UNION ALL", sql)
            if "proj-us" in sql:
                return [{"email": "alice@company.com", "project": "proj-us", "requests": 4, "last_ts": now}]
            if "proj-seoul" in sql:
                return [{"email": "alice@company.com", "project": "proj-seoul", "requests": 6, "last_ts": now}]
            return []

        gcp.query_handlers.append((lambda sql, p, m: True, handle_q))
        ctx = _ctx(app)
        res = usage.collect_usage(ctx, now - dt.timedelta(days=1), now + dt.timedelta(minutes=5),
                                  surfaces=["gemini_enterprise"])
        self.assertTrue(res.ok)
        self.assertEqual(len(queries_seen), 2)
        self.assertEqual(res.users["alice@company.com"]["requests"], 10)
        self.assertEqual(set(res.users["alice@company.com"]["by_project"].keys()), {"proj-us", "proj-seoul"})

    def test_auto_mode_skips_logging_data_access_when_bq_audit_exists_and_filters_methods(self):
        cfg = make_test_config(caps={"usage_source": "auto"})
        app, gcp = build_test_app(cfg=cfg)
        da_schema = schema.TableSchema.from_dicts([
            {"name": "timestamp", "type": "TIMESTAMP"},
            {"name": "logName", "type": "STRING"},
            {"name": "protopayload_auditlog", "type": "STRUCT", "fields": [
                {"name": "serviceName", "type": "STRING"},
                {"name": "methodName", "type": "STRING"},
                {"name": "resourceName", "type": "STRING"},
                {"name": "authenticationInfo", "type": "STRUCT", "fields": [
                    {"name": "principalEmail", "type": "STRING"},
                ]},
            ]},
        ])
        da_src = schema.TableSource("test-ai-project", "ge_analytics", svc_base.AUDIT_DATA_ACCESS, da_schema)
        app.registry.resolve = lambda proj, ds, prefix, **kw: da_src if prefix == svc_base.AUDIT_DATA_ACCESS else None
        now = dt.datetime.now(dt.timezone.utc)
        gcp.query_handlers.append((
            lambda sql, p, m: "aiplatform.googleapis.com" in sql,
            [{"email": "bob@company.com", "project": "test-ai-project", "model": "gemini-2.5-flash",
              "publisher": "google", "requests": 2, "minute": now.replace(second=0, microsecond=0), "last_ts": now}],
        ))
        ctx = _ctx(app)
        usage.vertex_usage(ctx, now - dt.timedelta(hours=1), now + dt.timedelta(minutes=5))
        log_filters = [
            (body or {}).get("filter", "")
            for _, url, _, body in gcp.rest_calls
            if "logging.googleapis.com/v2/entries:list" in url
        ]
        # Must query inference_request in Cloud Logging, but NEVER data_access (BQ already provided it!)
        self.assertTrue(any("inference_request" in f for f in log_filters))
        self.assertFalse(any("data_access" in f for f in log_filters))
        # Warning URL uses /vertex-logging/enable (not :enable)
        self.assertTrue(any("/vertex-logging/enable" in w["message"] for w in ctx.warnings))

        # In pure logging mode, Data Access filter MUST include methodName filter and GE skips BigQuery
        cfg_log = make_test_config(caps={"usage_source": "logging"})
        app2, gcp2 = build_test_app(cfg=cfg_log)
        app2.registry.resolve = mock.Mock(side_effect=AssertionError("BigQuery must not be called in logging mode"))
        ctx2 = _ctx(app2)
        usage.collect_usage(ctx2, now - dt.timedelta(hours=1), now + dt.timedelta(minutes=5))
        da_filters = [
            (body or {}).get("filter", "")
            for _, url, _, body in gcp2.rest_calls
            if "logging.googleapis.com/v2/entries:list" in url and "data_access" in (body or {}).get("filter", "")
        ]
        self.assertEqual(len(da_filters), 1)
        self.assertIn("protoPayload.methodName=~", da_filters[0])

    def test_e2e_pick_model_reads_lifecycle_stage_and_skips_preview_omni(self):
        mod = _load_e2e_module()
        args = argparse.Namespace(api_url="http://127.0.0.1:0", admin_key="k", project="p",
                                  location="global", test_sa="sa@p.iam.gserviceaccount.com", model=None)
        run = mod.Run(args)
        sample_models = {"data": [
            {"id": "gemini-omni-1.2-flash", "lifecycle": {"stage": "ga"}},
            {"id": "gemini-3.5-flash-preview", "lifecycle": {"stage": "preview"}},
            {"id": "gemini-2.5-flash-lite", "lifecycle": {"stage": "ga"}},
            {"id": "gemini-2.5-flash", "lifecycle": {"stage": "ga"}},
            {"id": "gemini-2.0-flash", "lifecycle": {"stage": "ga"}},
        ]}
        with mock.patch.object(mod.Run, "api", return_value=(200, sample_models)):
            run.pick_model()
        self.assertEqual(run.model, "gemini-2.5-flash")

    def test_deny_policy_client_caches_reads_and_invalidates_on_write(self):
        app, gcp = build_test_app()
        ctx = _ctx(app)
        ap = "cloudresourcemanager.googleapis.com/projects/test-ai-project"
        pid = "gcp-ai-caps-managed"
        client = enforcers.DenyPolicyClient(ctx, sleep=lambda _s: None)

        # First read hits GCP (404 -> cached None); second read served from cache!
        self.assertIsNone(client.get(ap, pid))
        self.assertIsNone(client.get(ap, pid))
        get_calls = [c for c in gcp.rest_calls if c[0] == "GET" and "denypolicies" in c[1]]
        self.assertEqual(len(get_calls), 1)

        # Create updates cache with policy + etag -> next read hits cache with new policy!
        pol = enforcers.build_policy({"principal://iam.googleapis.com/projects/-/serviceAccounts/a@p.iam.gserviceaccount.com":
                                      frozenset(["gemini_platform"])})
        res = client.create(ap, pid, pol)
        self.assertTrue(res["done"])
        cached = client.get(ap, pid)
        self.assertIsNotNone(cached)
        self.assertTrue(cached.get("etag"))
        get_calls_after = [c for c in gcp.rest_calls if c[0] == "GET" and "denypolicies" in c[1]]
        self.assertEqual(len(get_calls_after), 1)

        # Delete updates cache back to None
        res_del = client.delete(ap, pid, etag=cached["etag"])
        self.assertTrue(res_del["done"])
        self.assertIsNone(client.get(ap, pid))

    def test_e2e_live_sh_guards(self):
        path = os.path.join(config.repo_root(), "scripts", "e2e_live.sh")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('SCAN_ALL_PROJECTS="${SCAN_ALL_PROJECTS:-false}"', text)
        self.assertIn("organizations add-iam-policy-binding", text)
        self.assertNotIn("state.json", text)
        self.assertIn("SERVER_PID=$!", text)
        self.assertNotIn("trap 'kill $pid", text)


if __name__ == "__main__":
    unittest.main()
