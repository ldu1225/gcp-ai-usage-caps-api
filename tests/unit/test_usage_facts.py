"""v1.4.0: /v1/usage/facts, /v1/usage/exports, per-SKU cost lines, freshness metadata, include=groups."""

from __future__ import annotations

import datetime as dt
import json
import unittest

from tests.unit.support import build_test_app, make_test_config

from usage_caps_api import auth
from usage_caps_api.httpkit import ApiError, RawResponse
from usage_caps_api.services import base as svc_base
from usage_caps_api.services import pricing as pricing_svc
from usage_caps_api.services import usage, usage_facts

UTC = dt.timezone.utc


def _ctx(app, params=None, path_params=None):
    caller = auth.Caller("r", "r", "api_key", frozenset(["usage:read", "admin"]), "r")
    return svc_base.Ctx(app, app.config, caller, params=params or {}, path_params=path_params or {})


def _agy(email, ts, *, prompt, cand, cached=0, thoughts=0, traj="t1", exp="gemini-test-pro-high"):
    return {
        "logName": "projects/test-ai-project/logs/businessaicode.googleapis.com%2Finference_response",
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "insertId": "agy-%s-%s" % (traj, ts.timestamp()),
        "resource": {"type": "businessaicode.googleapis.com/BAICInstance", "labels": {"location": "us"}},
        "labels": {"user_id": "user:" + email, "model": "gemini-test-pro", "client_name": "antigravity_cli",
                   "client_version": "1.5.0", "trajectory_id": traj},
        "jsonPayload": {"experience": exp, "metadata": {
            "promptTokenCount": prompt, "candidatesTokenCount": cand, "cachedContentTokenCount": cached,
            "thoughtsTokenCount": thoughts, "totalTokenCount": prompt + cand + thoughts}},
    }


def _vertex(email, ts, *, prompt_text, prompt_image, cand, location="asia-northeast3", app="portal"):
    return {
        "logName": "projects/test-ai-project/logs/aiplatform.googleapis.com%2Finference_request",
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "insertId": "vx-%s" % ts.timestamp(),
        "resource": {"type": "aiplatform.googleapis.com/PublisherModel", "labels": {
            "location": location, "model_user_id": "gemini-test-pro", "publisher": "google",
            "resource_container": "test-ai-project"}},
        "labels": {"app": app, "gen_ai.operation.name": "generate_content"},
        "jsonPayload": {
            "requestStartTime": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "requestDuration": "1.5s",
            "principal": {"id": "portal-sa@test-ai-project.iam.gserviceaccount.com", "type": "SERVICE_ACCOUNT"},
            "httpHeaders": {"x-goog-agent-platform-observability-label-user": {"values": [email]}},
            "trafficType": "ON_DEMAND",
            "generateContentDetails": {"usageMetadata": {
                "promptTokenCount": prompt_text + prompt_image, "candidatesTokenCount": cand,
                "totalTokenCount": prompt_text + prompt_image + cand, "trafficType": "ON_DEMAND",
                "promptTokensDetails": [{"modality": "TEXT", "tokenCount": prompt_text},
                                        {"modality": "IMAGE", "tokenCount": prompt_image}],
                "candidatesTokensDetails": [{"modality": "TEXT", "tokenCount": cand}]}},
        },
    }


class UsageFactsTest(unittest.TestCase):
    def setUp(self):
        self.app, self.gcp = build_test_app(cfg=make_test_config())
        now = dt.datetime.now(UTC)
        self.h0 = now.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=2)
        self.gcp.log_entries = [
            _agy("alice@company.com", self.h0 + dt.timedelta(minutes=5), prompt=10_000, cand=2_000, cached=4_000,
                 thoughts=300, traj="t1"),
            _agy("alice@company.com", self.h0 + dt.timedelta(minutes=40), prompt=6_000, cand=1_000, traj="t2"),
            _agy("alice@company.com", self.h0 + dt.timedelta(minutes=70), prompt=3_000, cand=500, traj="t2",
                 exp="gemini-test-pro-medium"),
            _vertex("bob@company.com", self.h0 + dt.timedelta(minutes=10), prompt_text=8_000, prompt_image=1_290,
                    cand=700),
            _vertex("bob@company.com", self.h0 + dt.timedelta(minutes=80), prompt_text=2_000, prompt_image=0,
                    cand=300, location="global"),
        ]

    def facts(self, **params):
        return usage_facts.list_facts(_ctx(self.app, params))

    def test_day_facts_match_users_endpoint_totals(self):
        users = usage.list_user_usage(_ctx(self.app))
        facts = self.facts(granularity="day")
        self.assertEqual(facts["summary"]["total_requests"], users["summary"]["total_requests"])
        self.assertEqual(facts["summary"]["total_tokens"], users["summary"]["total_tokens"])
        self.assertAlmostEqual(facts["summary"]["total_list_cost"], users["summary"]["total_estimated_cost"], places=6)
        self.assertEqual(facts["group_by"], ["user", "surface", "project", "model"])
        self.assertIsNotNone(facts["data_refreshed_at"])
        self.assertIsNotNone(users["data_refreshed_at"])
        self.assertIn("data_refreshed_at", users["telemetry"])

    def test_hour_buckets_sum_to_day_and_ids_are_stable(self):
        hours = self.facts(granularity="hour")
        days = self.facts(granularity="day")
        for key in ("requests", "input_tokens", "output_tokens", "cached_tokens", "total_tokens"):
            self.assertEqual(sum(r[key] for r in hours["data"]), sum(r[key] for r in days["data"]), key)
        again = self.facts(granularity="hour")
        self.assertEqual([r["fact_id"] for r in hours["data"]], [r["fact_id"] for r in again["data"]])
        self.assertEqual([r["revision"] for r in hours["data"]], [r["revision"] for r in again["data"]])
        self.assertEqual(len({r["fact_id"] for r in hours["data"]}), len(hours["data"]))
        for r in hours["data"]:
            start = dt.datetime.fromisoformat(r["bucket_start"].replace("Z", "+00:00"))
            self.assertEqual(start.minute, 0)
            self.assertEqual(r["uncached_input_tokens"], r["input_tokens"] - r["cached_tokens"] - r["cache_write_tokens"])

    def test_antigravity_dimensions_client_reasoning_location_sessions(self):
        res = self.facts(granularity="day", group_by="user,client,reasoning_level,location", surface="antigravity")
        rows = {r["reasoning_level"]: r for r in res["data"]}
        self.assertEqual(set(rows), {"high", "medium"})
        high = rows["high"]
        self.assertEqual(high["email"], "alice@company.com")
        self.assertEqual(high["principal_type"], "user")
        self.assertEqual(high["client"], "antigravity_cli")
        self.assertEqual(high["client_versions"], ["1.5.0"])
        self.assertEqual(high["location"], "us")
        self.assertEqual(high["requests"], 2)
        self.assertEqual(high["sessions"], 2)
        self.assertEqual(high["thoughts_tokens"], 300)
        self.assertEqual(high["surfaces"], ["antigravity"])
        self.assertEqual(high["attribution"], {"log_identity": 2})

    def test_gemini_platform_modality_location_app_latency(self):
        res = self.facts(granularity="day", group_by="user,location,app", surface="gemini_platform")
        rows = {r["location"]: r for r in res["data"]}
        seoul = rows["asia-northeast3"]
        self.assertEqual(seoul["email"], "bob@company.com")
        self.assertEqual(seoul["app"], "portal")
        self.assertEqual(seoul["by_modality"]["image"], {"input_tokens": 1_290, "output_tokens": 0})
        self.assertEqual(seoul["by_modality"]["text"], {"input_tokens": 8_000, "output_tokens": 700})
        self.assertEqual(seoul["latency_ms_avg"], 1500)
        self.assertEqual(seoul["attribution"], {"header_label": 1})
        self.assertEqual(seoul["by_traffic"]["on_demand"]["requests"], 1)
        self.assertIn("global", rows)

    def test_billing_lines_sum_to_list_cost(self):
        res = self.facts(granularity="day")
        priced = [r for r in res["data"] if r["list_cost"]]
        self.assertTrue(priced)
        for r in priced:
            # list_cost is rounded to 6 decimals; lines keep 9
            self.assertAlmostEqual(sum(ln["cost"] for ln in r["billing"]), r["list_cost"], delta=1e-6)
            for ln in r["billing"]:
                self.assertEqual(ln["quantity"]["unit"], "tokens")
                self.assertIn(ln["component"], ("input", "cached_input", "cache_write", "output"))
        no_billing = self.facts(granularity="day", include="none")
        self.assertNotIn("billing", no_billing["data"][0])

    def test_cost_lines_match_model_cost_with_tiers(self):
        pricing = pricing_svc.Pricing(models=(("gemini-test-pro", 1.25, 10.0),), cached_models=(("gemini-test-pro", 0.3125),),
                                      source="manual")
        agg = {"input_tokens": 300_000, "output_tokens": 50_000, "cached_tokens": 40_000, "cache_write_tokens": 0,
               "total_tokens": 350_000, "long_input_tokens": 250_000, "long_output_tokens": 10_000,
               "by_traffic": {"on_demand": {"input_tokens": 300_000, "output_tokens": 50_000, "cached_tokens": 40_000,
                                            "cache_write_tokens": 0, "total_tokens": 350_000}}}
        total, _, _ = pricing_svc.model_cost("gemini-test-pro", agg, pricing)
        lines = pricing_svc.cost_lines("gemini-test-pro", agg, pricing)
        self.assertAlmostEqual(sum(ln["cost"] for ln in lines), total, places=6)
        self.assertTrue(all(ln["sku_id"] is None for ln in lines))  # override price -> no Catalog SKU

    def test_pagination_and_validation(self):
        res = self.facts(granularity="hour", limit="1")
        self.assertEqual(len(res["data"]), 1)
        self.assertTrue(res["has_more"])
        nxt = self.facts(granularity="hour", limit="1", cursor=res["next_cursor"])
        self.assertNotEqual(nxt["data"][0]["fact_id"], res["data"][0]["fact_id"])
        with self.assertRaises(ApiError):
            self.facts(granularity="minute")
        with self.assertRaises(ApiError):
            self.facts(group_by="user,colour")
        with self.assertRaises(ApiError):
            self.facts(granularity="hour", start="2026-01-01", end="2026-03-01")
        with self.assertRaises(ApiError):
            self.facts(limit="1001")

    def test_exports_list_and_jsonl_download(self):
        lst = usage_facts.list_exports(_ctx(self.app))
        ids = [e["id"] for e in lst["data"]]
        day = self.h0.date().isoformat()
        eid = "usage-facts-hourly-%s" % day
        self.assertIn(eid, ids)
        entry = next(e for e in lst["data"] if e["id"] == eid)
        self.assertEqual(entry["download_path"], "/v1/usage/exports/%s/download" % eid)
        out = usage_facts.download_export(_ctx(self.app, path_params={"export_id": eid}))
        self.assertIsInstance(out, RawResponse)
        self.assertTrue(out.content_type.startswith("application/x-ndjson"))
        rows = [json.loads(ln) for ln in out.text.splitlines() if ln]
        self.assertTrue(rows)
        self.assertTrue(all(r["granularity"] == "hour" and r["object"] == "usage_fact" for r in rows))
        headers = dict(out.headers)
        self.assertEqual(headers["X-Usage-Export-Id"], eid)
        self.assertEqual(len(headers["X-Usage-Export-Revision"]), 16)
        with self.assertRaises(ApiError):
            usage_facts.download_export(_ctx(self.app, path_params={"export_id": "../etc/passwd"}))

    def test_users_include_groups_and_principal_type(self):
        doc_ctx = _ctx(self.app)
        from usage_caps_api.services import groups as groups_svc
        doc_ctx.store.mutate(groups_svc.GROUPS_DOC, lambda d: d.update(groups={"eng": {
            "name": "Engineering", "members": ["alice@company.com"]}}))
        res = usage.list_user_usage(_ctx(self.app, {"include": "groups"}))
        alice = next(r for r in res["data"] if r["email"] == "alice@company.com")
        self.assertEqual(alice["principal_type"], "user")
        self.assertEqual(alice["groups"], [{"id": "eng", "name": "Engineering"}])


class ModalityPricingTest(unittest.TestCase):
    """Audio / image / video tokens are priced at their own official Catalog SKUs (live-verified log shapes)."""

    def test_catalog_classifies_modality_ga_and_long_skus(self):
        c = pricing_svc.classify_sku
        self.assertEqual(c("Gemini 2.5 Flash GA Audio Input - Predictions", "Vertex AI", "count", "count")[:2],
                         ("gemini-2.5-flash", "audio_input"))
        self.assertEqual(c("Gemini 2.5 Flash Image Output - Predictions", "Vertex AI", "count", "count")[:2],
                         ("gemini-2.5-flash-image", "image_output"))
        self.assertEqual(c("Gemini 3.1 Flash Image Global Image Output Flex - Predictions", "Vertex AI", "count",
                           "count")[1:4:2], ("image_output", "flex"))
        self.assertEqual(c("Gemini 2.5 Pro Text Input (Long) - Predictions", "Vertex AI", "count", "count")[3],
                         "long_context")
        self.assertIsNone(c("Gemini 2.5 Flash Input Audio Caching", "Vertex AI", "count", "count"))
        self.assertIsNone(c("Gemini 2.5 Flash Live Audio (AV2A) Output - Predictions", "Vertex AI", "count", "count"))
        self.assertIsNone(c("Gemini 3.8 Flash Cyber Global Audio Input - Online Predictions", "Vertex AI", "count",
                            "count"))

    def _pricing(self):
        table = pricing_svc.PriceTable(currency="USD", prices={
            "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cached": 0.03, "audio_input": 1.00,
                                 "image_input": 0.30, "video_input": 0.30, "source": "official_sku",
                                 "skus": {"input": "services/x/skus/TXT-IN", "audio_input": "services/x/skus/AUD-IN",
                                          "output": "services/x/skus/TXT-OUT"}},
            "gemini-2.5-flash-image": {"image_output": 30.0, "source": "official_sku",
                                       "skus": {"image_output": "services/x/skus/IMG-OUT"}},
        })
        return pricing_svc.Pricing(source="auto", table=table)

    def test_audio_input_priced_at_audio_sku(self):
        p = self._pricing()
        # Live call: promptTokensDetails AUDIO 100 + TEXT 8, candidates TEXT 11
        agg = {"input_tokens": 108, "output_tokens": 11, "cached_tokens": 0, "cache_write_tokens": 0,
               "total_tokens": 119, "extra": {"audio_input_tokens": 100, "text_input_tokens": 8,
                                              "text_output_tokens": 11}}
        cost, priced, _ = pricing_svc.model_cost("gemini-2.5-flash", agg, p)
        self.assertTrue(priced)
        self.assertAlmostEqual(cost, (100 * 1.00 + 8 * 0.30 + 11 * 2.50) / 1e6, places=12)
        lines = {ln["component"]: ln for ln in pricing_svc.cost_lines("gemini-2.5-flash", agg, p)}
        self.assertEqual(lines["audio_input"]["quantity"]["value"], 100)
        self.assertEqual(lines["audio_input"]["sku_id"], "AUD-IN")
        self.assertEqual(lines["input"]["quantity"]["value"], 8)
        self.assertAlmostEqual(sum(ln["cost"] for ln in lines.values()), cost, places=9)

    def test_image_output_priced_at_image_sku(self):
        p = self._pricing()
        # Live call: candidatesTokensDetails IMAGE 1290 + TEXT 8
        agg = {"input_tokens": 12, "output_tokens": 1298, "cached_tokens": 0, "cache_write_tokens": 0,
               "total_tokens": 1310, "extra": {"image_output_tokens": 1290, "text_output_tokens": 8}}
        cost, _, _ = pricing_svc.model_cost("gemini-2.5-flash-image", agg, p)
        self.assertAlmostEqual(cost, 1290 * 30.0 / 1e6, places=12)
        lines = pricing_svc.cost_lines("gemini-2.5-flash-image", agg, p)
        self.assertEqual([(ln["component"], ln["sku_id"]) for ln in lines if ln["component"] == "image_output"],
                         [("image_output", "IMG-OUT")])

    def test_override_prices_stay_modality_agnostic(self):
        p = pricing_svc.Pricing(models=(("gemini-2.5-flash", 0.2, 1.0),), source="manual")
        agg = {"input_tokens": 108, "output_tokens": 11, "total_tokens": 119,
               "extra": {"audio_input_tokens": 100}}
        cost, _, _ = pricing_svc.model_cost("gemini-2.5-flash", agg, p)
        self.assertAlmostEqual(cost, (108 * 0.2 + 11 * 1.0) / 1e6, places=12)

    def test_extra_counters_survive_classic_aggregation(self):
        dst = usage._new_counters()  # pylint: disable=protected-access
        usage._add(dst, {"requests": 1, "extra": {"audio_input_tokens": 100}})  # pylint: disable=protected-access
        usage._add(dst, {"requests": 1, "extra": {"audio_input_tokens": 50, "image_input_tokens": 7}})  # pylint: disable=protected-access
        self.assertEqual(dst["extra"], {"audio_input_tokens": 150, "image_input_tokens": 7})


if __name__ == "__main__":
    unittest.main()
