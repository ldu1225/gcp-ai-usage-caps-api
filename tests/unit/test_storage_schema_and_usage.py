"""Unit tests for FileStore optimistic concurrency, schema-adaptive BigQuery SQL, multi-surface usage, and config."""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest

from tests.unit.support import build_test_app, make_test_config
from usage_caps_api import auth, config, identity, schema
from usage_caps_api.services import base as svc_base, usage
from usage_caps_api.store import DocStore, FileBackend, StoreConflict


class StorageSchemaAndUsageTests(unittest.TestCase):
    def test_file_store_atomic_save_and_optimistic_conflict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = FileBackend(tmpdir, durable=True)
            doc, gen0 = backend.load("policy")
            self.assertEqual(doc, {})
            self.assertEqual(gen0, 0)

            gen1 = backend.save("policy", {"cost_cap": 100.0}, gen0)
            self.assertNotEqual(gen1, 0)
            mode = os.stat(os.path.join(tmpdir, "policy.json")).st_mode & 0o777
            self.assertEqual(mode, 0o600)

            # Saving with stale generation raises StoreConflict
            with self.assertRaises(StoreConflict):
                backend.save("policy", {"cost_cap": 200.0}, gen0)

            # DocStore.mutate succeeds cleanly with retry semantics
            ds = DocStore(backend)
            ds.mutate("policy", lambda d: d.update(cost_cap=250.0))
            loaded, _ = ds.load("policy", fresh=True)
            self.assertEqual(loaded["cost_cap"], 250.0)

    def test_schema_adaptive_sql_builder_and_project_filter(self):
        s = schema.TableSchema.from_dicts([
            {"name": "timestamp", "type": "TIMESTAMP"},
            {"name": "logName", "type": "STRING"},
            {"name": "labels", "type": "STRUCT", "fields": [
                {"name": "user_id", "type": "STRING"},
                {"name": "model_id", "type": "STRING"},
            ]},
            {"name": "jsonpayload_v1_inferenceresponselog", "type": "STRUCT", "fields": [
                {"name": "metadata", "type": "STRUCT", "fields": [
                    {"name": "totalTokenCount", "type": "INTEGER"},
                    {"name": "promptTokenCount", "type": "INTEGER"},
                ]},
            ]},
        ])
        src = schema.TableSource("central-audit", "ge_analytics", "businessaicode_googleapis_com_inference_response",
                                 s, sharded=True, filter_projects=["proj-a", "proj-b"])
        f = usage.agy_fields(src)
        self.assertIn("`labels`.`user_id`", f["user"])
        self.assertIn("jsonpayload_v1_inferenceresponselog", f["total"])
        self.assertIsNone(f["cached"])  # optional column not present yet -> None instead of broken SQL!

        cte = src.cte("s")
        self.assertIn("_TABLE_SUFFIX BETWEEN @sfx_start AND @sfx_end", cte)
        self.assertIn("'proj-a', 'proj-b'", cte)

    def test_bigquery_multi_surface_usage_aggregation(self):
        cfg = make_test_config(caps={"usage_source": "bigquery"})
        app, fake_gcp = build_test_app(cfg=cfg)
        agy_schema = schema.TableSchema.from_dicts([
            {"name": "timestamp", "type": "TIMESTAMP"},
            {"name": "logName", "type": "STRING"},
            {"name": "labels", "type": "STRUCT", "fields": [
                {"name": "user_id", "type": "STRING"},
                {"name": "model_id", "type": "STRING"},
            ]},
            {"name": "jsonpayload_v1_inferenceresponselog", "type": "STRUCT", "fields": [
                {"name": "metadata", "type": "STRUCT", "fields": [
                    {"name": "totalTokenCount", "type": "INT64"},
                    {"name": "promptTokenCount", "type": "INT64"},
                    {"name": "candidatesTokenCount", "type": "INT64"},
                    {"name": "cachedContentTokenCount", "type": "INT64"},
                ]},
            ]},
        ])
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
        ge_schema = schema.TableSchema.from_dicts([
            {"name": "timestamp", "type": "TIMESTAMP"},
            {"name": "logName", "type": "STRING"},
            {"name": "jsonPayload", "type": "STRUCT", "fields": [
                {"name": "userIamPrincipal", "type": "STRING"},
            ]},
        ])
        sources = {
            svc_base.AGY_SINK: schema.TableSource("test-ai-project", "ge_analytics", svc_base.AGY_SINK, agy_schema),
            svc_base.AUDIT_DATA_ACCESS: schema.TableSource("test-ai-project", "ge_analytics", svc_base.AUDIT_DATA_ACCESS, da_schema),
            svc_base.GE_ACTIVITY: schema.TableSource("test-ai-project", "ge_analytics", svc_base.GE_ACTIVITY, ge_schema),
        }
        app.registry.resolve = lambda proj, ds, prefix, **kw: sources.get(prefix)
        now = dt.datetime.now(dt.timezone.utc)
        fake_gcp.query_handlers.append((
            lambda sql, params, mod: "businessaicode_googleapis_com_inference_response" in sql,
            [{"email": "dana@company.com", "model": "gemini-test-pro", "project": "test-ai-project",
              "requests": 5, "tokens": 500_000, "prompt_tokens": 300_000, "candidate_tokens": 200_000,
              "cached_tokens": 50_000, "principal": "user:dana@company.com", "last_ts": now}],
        ))
        fake_gcp.query_handlers.append((
            lambda sql, params, mod: "aiplatform.googleapis.com" in sql,
            [{"email": "dana@company.com", "model": "gemini-test-flash", "project": "test-ai-project",
              "requests": 12, "last_ts": now}],
        ))
        fake_gcp.query_handlers.append((
            lambda sql, params, mod: "gemini_enterprise_user_activity" in sql,
            [{"email": "dana@company.com", "project": "test-ai-project", "requests": 7, "last_ts": now}],
        ))

        caller = auth.Caller("r", "r", "api_key", frozenset(["usage:read"]), "r")
        ctx = svc_base.Ctx(app, app.config, caller)
        res = usage.list_user_usage(ctx)
        self.assertEqual(res["total"], 1)
        dana = res["data"][0]
        self.assertEqual(dana["email"], "dana@company.com")
        self.assertEqual(dana["surfaces"], ["antigravity", "gemini_enterprise", "vertex_ai"])
        self.assertEqual(dana["requests"], 24)  # 5 + 12 + 7
        self.assertEqual(dana["total_tokens"], 500_000)

    def test_config_refuses_local_insecure_on_cloud_run_and_identity_rejects_sa(self):
        with self.assertRaises(ValueError):
            config.load_config(environ={"K_SERVICE": "prod-api", "ALLOW_LOCAL_INSECURE": "true"})
        self.assertIsNone(identity.person_key("my-bot@proj.iam.gserviceaccount.com"))
        self.assertEqual(identity.person_key("user:Alice@Company.COM"), "alice@company.com")


if __name__ == "__main__":
    unittest.main()
