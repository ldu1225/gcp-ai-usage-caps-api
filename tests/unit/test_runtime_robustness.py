"""Regression tests for the runtime review findings (drop into tests/unit/test_runtime_robustness.py).

All offline: FakeGcp / LiveTestServer from tests/unit/support.py, no live GCP calls."""

from __future__ import annotations

import datetime as dt
import unittest

from tests.unit.support import (FakeGcp, LiveTestServer, build_test_app, make_inference_entry, make_test_config,
                                make_vertex_entry)
from usage_caps_api import auth, config
from usage_caps_api.gcp import GcpError, QueryResult
from usage_caps_api.services import base as svc_base, caps

try:  # the BigQuery-backed paths need google-cloud-bigquery (requirements.txt); skip cleanly without it
    import google.cloud.bigquery  # noqa: F401  # pylint: disable=unused-import
    HAS_BIGQUERY = True
except ImportError:  # pragma: no cover - bare interpreters (e.g. macOS system python)
    HAS_BIGQUERY = False
NEEDS_BQ = unittest.skipUnless(HAS_BIGQUERY, "google-cloud-bigquery not installed")

TOKEN = "gcpai_boot_secret_1234567890"
ADMIN = auth.Caller("admin", "admin", "api_key", frozenset(auth.ALL_SCOPES), "k")


def _schema_fields(spec):
    from google.cloud.bigquery import SchemaField as F  # noqa: N814

    def build(items):
        out = []
        for item in items:
            if isinstance(item, tuple) and isinstance(item[1], list):
                out.append(F(item[0], "RECORD", fields=build(item[1])))
            else:
                out.append(F(item[0], item[1]))
        return out
    return build(spec)


class _Tbl:
    def __init__(self, schema):
        self.schema, self.time_partitioning, self.location = schema, None, "asia-northeast3"


class _Client:
    def __init__(self, schema=None, error=None):
        self.schema, self.error = schema, error

    def get_table(self, ref):
        if self.error is not None:
            raise self.error
        return _Tbl(self.schema)


class PartialTelemetryDoesNotRestoreTests(unittest.TestCase):
    """F1: one surface unreadable + another healthy must not release an over-cap user."""

    def test_antigravity_outage_withholds_restore(self):
        class G(FakeGcp):
            agy_broken = False

            def request(self, method, url, **kw):
                flt = str((kw.get("json_body") or {}).get("filter"))
                if self.agy_broken and "entries:list" in url and "businessaicode" in flt:
                    raise GcpError("unavailable", status=503, message="backend error")
                return super().request(method, url, **kw)

        cfg = make_test_config(caps={"cost_cap": 10.0, "enforcement_mode": "auto", "enforcement_backend": "iam_deny"})
        gcp = G()
        app, _ = build_test_app(cfg=cfg, gcp=gcp)
        ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
        gcp.log_entries += [make_inference_entry("carol@company.com", prompt=400_000, cand=1_000_000, ts=ts),
                            make_vertex_entry(user="bob@company.com", ts=ts)]
        r1 = caps.enforce_cycle(svc_base.Ctx(app, app.config, ADMIN), trigger="admin", dry_run=False,
                                use_job_if_configured=False)
        self.assertEqual(r1["plan"]["suspend"], ["carol@company.com"])
        gcp.agy_broken = True
        app.cache.clear()
        r2 = caps.enforce_cycle(svc_base.Ctx(app, app.config, ADMIN), trigger="scheduler", dry_run=True,
                                use_job_if_configured=False)
        self.assertEqual(r2["plan"]["restore"], [])
        self.assertTrue(r2["plan"]["withheld_restores"])


@NEEDS_BQ
class CentralDatasetNoDoubleCountTests(unittest.TestCase):
    """F2: AUDIT_PROJECT_ID (central dataset) must not re-read the whole window from Cloud Logging."""

    def test_vertex_tokens_counted_once(self):
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0) - dt.timedelta(hours=1)
        schema = _schema_fields([
            ("timestamp", "TIMESTAMP"), ("logName", "STRING"), ("severity", "STRING"),
            ("resource", [("labels", [("resource_container", "STRING"), ("publisher", "STRING"),
                                      ("model_user_id", "STRING"), ("location", "STRING")])]),
            ("jsonPayload", [("requestStartTime", "TIMESTAMP"), ("trafficType", "STRING"),
                             ("generateContentDetails", [("usageMetadata", [("promptTokenCount", "INTEGER"),
                                                                           ("candidatesTokenCount", "INTEGER"),
                                                                           ("totalTokenCount", "INTEGER")])]),
                             ("rawPredictDetails", [("usageMetadata", [("input_tokens", "INTEGER"),
                                                                      ("output_tokens", "INTEGER")])]),
                             ("httpHeaders", [("x_goog_agent_platform_observability_label_user", "STRING")])]),
        ])

        class G(FakeGcp):
            def bq_client(self, qp):
                return _Client(schema)

            def query(self, sql, params=None, **kw):
                if "INFORMATION_SCHEMA.TABLES" in sql:
                    rows = ([{"table_name": "aiplatform_googleapis_com_inference_request", "creation_time": now}]
                            if "hub-project-1" in sql else [])
                    return QueryResult(rows, [], 0, len(rows))
                if "TIMESTAMP_TRUNC(ts, MINUTE)" in sql:
                    return QueryResult([dict(
                        minute=now, project="team-proj-a", log_project="team-proj-a", publisher="google",
                        model="gemini-test-pro", user_label="bob@company.com", principal=None, traffic="ON_DEMAND",
                        location="global", app=None, requests=1, prompt_tokens=1000, candidate_tokens=500,
                        thoughts_tokens=0, cached_tokens=0, cache_write_tokens=0, tokens=1500, long_requests=0,
                        long_input_tokens=0, long_output_tokens=0, long_cached_tokens=0, last_ts=now,
                        max_log_ts=now)], [], 0, 1)
                return QueryResult([], [], 0, 0)

        cfg = make_test_config(workspace={"projects": ["team-proj-a"], "audit_project": "hub-project-1"},
                               caps={"usage_source": "auto"})
        gcp = G()
        gcp.log_entries.append(make_vertex_entry(project="team-proj-a", user="bob@company.com", prompt=1000,
                                                 cand=500, ts=now))
        app, _ = build_test_app(cfg=cfg, gcp=gcp)
        with LiveTestServer(app) as srv:
            st, body, _ = srv.request("GET", "/v1/usage/users", token=TOKEN)
        self.assertEqual(st, 200)
        bob = next(u for u in body["data"] if u["email"] == "bob@company.com")
        self.assertEqual((bob["requests"], bob["total_tokens"]), (1, 1500))


@NEEDS_BQ
class SchemaLookupErrorsAreClassifiedTests(unittest.TestCase):
    """F3: tables.get failures (NotFound race, Forbidden, RefreshError) -> GcpError -> Cloud Logging fallback + warning."""

    def test_get_table_not_found_falls_back_to_logging(self):
        from google.api_core import exceptions as gexc

        class G(FakeGcp):
            def bq_client(self, qp):
                return _Client(error=gexc.NotFound("Not found: Table"))

            def query(self, sql, params=None, **kw):
                if "INFORMATION_SCHEMA.TABLES" in sql:
                    return QueryResult([{"table_name": "businessaicode_googleapis_com_inference_response",
                                         "creation_time": dt.datetime(2026, 11, 27, tzinfo=dt.timezone.utc)}], [], 0, 1)
                return super().query(sql, params, **kw)

        cfg = make_test_config(caps={"usage_source": "auto"})
        gcp = G()
        gcp.log_entries.append(make_inference_entry("alice@company.com", ts=dt.datetime.now(dt.timezone.utc)))
        app, _ = build_test_app(cfg=cfg, gcp=gcp)
        with LiveTestServer(app) as srv:
            st, body, _ = srv.request("GET", "/v1/usage/users", token=TOKEN)
        self.assertEqual(st, 200)
        self.assertEqual([u["email"] for u in body["data"]], ["alice@company.com"])


@NEEDS_BQ
class AntigravityUncoveredProjectTests(unittest.TestCase):
    """F4: per-project datasets; a managed project without a sink table is still read from Cloud Logging."""

    def test_project_without_table_is_counted(self):
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(minutes=30)
        schema = _schema_fields([
            ("timestamp", "TIMESTAMP"), ("logName", "STRING"), ("labels", [("user_id", "STRING"), ("model_id", "STRING")]),
            ("jsonPayload", [("metadata", [("promptTokenCount", "INTEGER"), ("candidatesTokenCount", "INTEGER"),
                                           ("totalTokenCount", "INTEGER")])])])

        class G(FakeGcp):
            def bq_client(self, qp):
                return _Client(schema)

            def query(self, sql, params=None, **kw):
                if "INFORMATION_SCHEMA.TABLES" in sql:
                    if "`test-ai-project." in sql:
                        return QueryResult([{"table_name": "businessaicode_googleapis_com_inference_response",
                                             "creation_time": now}], [], 0, 1)
                    raise GcpError("not_found", status=404, message="Not found: Dataset")
                if "GROUP BY email, model, project" in sql:
                    return QueryResult([dict(email="alice@company.com", model="gemini-test-pro", project="test-ai-project",
                                             requests=1, tokens=1500, prompt_tokens=1000, candidate_tokens=500,
                                             cached_tokens=0, thoughts_tokens=0, long_requests=0, long_input_tokens=0,
                                             long_output_tokens=0, long_cached_tokens=0,
                                             principal="user:alice@company.com", first_ts=now, last_ts=now)], [], 0, 1)
                return QueryResult([], [], 0, 0)

        cfg = make_test_config(workspace={"projects": ["team-proj-a"]}, caps={"usage_source": "auto"})
        gcp = G()
        gcp.log_entries += [make_inference_entry("alice@company.com", project="test-ai-project", prompt=1000, cand=500, ts=now),
                            make_inference_entry("alice@company.com", project="team-proj-a", prompt=7000, cand=1000, ts=now)]
        app, _ = build_test_app(cfg=cfg, gcp=gcp)
        with LiveTestServer(app) as srv:
            st, body, _ = srv.request("GET", "/v1/usage/users", token=TOKEN)
        alice = body["data"][0]
        self.assertEqual(alice["total_tokens"], 9500)
        self.assertEqual(sorted(alice["projects"]), ["team-proj-a", "test-ai-project"])


class PricingSyncFailureTests(unittest.TestCase):
    """F5: Catalog unreachable -> 502 (not 500); failed SKU scans keep previously synced prices."""

    def _app(self, handler):
        cfg = make_test_config(pricing={"source": "auto", "model_overrides": []})
        g = FakeGcp()
        g.rest_handlers.append((lambda m, u, p, b: "cloudbilling.googleapis.com" in u, handler))
        app, _ = build_test_app(cfg=cfg, gcp=g)
        app.store.mutate("pricing", lambda d: d.update(synced_at="2026-11-27T00:00:00Z", models={
            "gemini-2.5-pro": {"input": 1.25, "output": 10.0, "source": "cloud_billing_catalog"}}))
        return app

    def test_catalog_denied_returns_502(self):
        def denied(*_):
            raise GcpError("denied", status=403, message="VPC Service Controls")
        app = self._app(denied)
        with LiveTestServer(app) as srv:
            st, _, _ = srv.request("POST", "/v1/internal/pricing/sync", token=TOKEN, body={})
        self.assertEqual(st, 502)
        self.assertIn("gemini-2.5-pro", app.store.load("pricing")[0]["models"])

    def test_sku_failures_keep_previous_prices(self):
        def sku_fail(m, u, p, b):
            if "/skus" in u:
                raise GcpError("unavailable", status=503, message="backend error")
            return {"services": [{"serviceId": "C7E2-9256-1C43", "displayName": "Vertex AI"}]}
        app = self._app(sku_fail)
        with LiveTestServer(app) as srv:
            st, _, _ = srv.request("POST", "/v1/internal/pricing/sync", token=TOKEN, body={})
        self.assertEqual(st, 200)
        self.assertIn("gemini-2.5-pro", app.store.load("pricing")[0]["models"])


class ConfigEnvRobustnessTests(unittest.TestCase):
    """F7: malformed numeric env vars never crash-loop; common spellings are accepted."""

    def test_lenient_env(self):
        env = {"PROJECT_ID": "my-hub-project", "BIGQUERY_MAX_BYTES_BILLED": "2e10", "CACHE_TTL_SECONDS": "1.5",
               "SCOPE_MAX_PROJECTS": "all", "ORG_ID": "organizations/123456789012", "SCOPE_TYPE": "folders",
               "SCOPE_IDS": "123"}
        cfg = config.load_config(env, config_path="/nonexistent.yaml")
        self.assertEqual(cfg["bigquery"]["max_bytes_billed"], 20_000_000_000)
        self.assertEqual(cfg["server"]["cache_ttl_seconds"], 1)
        self.assertEqual(cfg["workspace"]["scope_max_projects"], 300)
        self.assertEqual(cfg["hub"]["org_id"], "123456789012")
        self.assertEqual(cfg["workspace"]["scope_type"], "folder")


if __name__ == "__main__":
    unittest.main()
