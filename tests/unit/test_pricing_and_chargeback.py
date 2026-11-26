"""Unit tests for automatic pricing, cached/output token cost estimation, and integer-cent chargeback."""

from __future__ import annotations

import datetime as dt
import unittest

from tests.unit.support import build_test_app, make_inference_entry
from usage_caps_api import auth
from usage_caps_api.services import base as svc_base, chargeback, models_catalog as mc, pricing


class PricingAndChargebackTests(unittest.TestCase):
    def test_sku_parsing_and_model_normalization(self):
        hit = pricing.token_sku("Gemini 2.5 Pro Input Token Count", "Vertex AI", "count", "1000000 tokens")
        self.assertEqual(hit, ("gemini-2.5-pro", "input", None))

        hit_cached = pricing.cached_token_sku("Gemini 2.5 Pro Cached Input Token Count", "Vertex AI", "count")
        self.assertEqual(hit_cached, ("gemini-2.5-pro", "cached", None))

        # Batch and non-token media models excluded from standard online token pricing
        self.assertIsNone(pricing.token_sku("Gemini 2.5 Pro Batch Input Token Count", "Vertex AI", "count"))
        self.assertIsNone(pricing.token_sku("Veo 3 Video Generation", "Vertex AI", "s"))
        self.assertEqual(mc.normalize_model_id("publishers/google/models/gemini-3.0-pro-preview"), "gemini-3-pro")

    def test_estimate_cost_with_prompt_cache_and_thinking_tokens(self):
        p = pricing.Pricing(
            currency="USD",
            source="manual",
            models=(("gemini-2.5-pro", 2.0, 10.0),),
            cached_models=(("gemini-2.5-pro", 0.5),),
        )
        # 1M prompt (400k cached, 600k uncached), 1.5M total -> 500k output/thinking tokens
        # Expected: 0.6 * 2.0 + 0.4 * 0.5 + 0.5 * 10.0 = 1.2 + 0.2 + 5.0 = $6.40
        rec = {
            "by_model": {
                "gemini-2.5-pro": {
                    "input_tokens": 1_000_000,
                    "cached_tokens": 400_000,
                    "output_tokens": 200_000,
                    "total_tokens": 1_500_000,
                }
            }
        }
        cost, partial = pricing.estimate_cost(rec, p)
        self.assertFalse(partial)
        self.assertAlmostEqual(cost, 6.4, places=6)

    def test_integer_cent_allocate_sums_exactly(self):
        parts = chargeback.allocate(100, [1.0, 1.0, 1.0])
        self.assertEqual(sum(parts), 100)
        self.assertEqual(parts, [34, 33, 33])

    def test_monthly_billing_report_user_project_rollup_invariant(self):
        app, fake_gcp = build_test_app()
        now = dt.datetime.now(dt.timezone.utc)
        month_str = now.strftime("%Y-%m")
        # Alice uses Gemini + Claude across two projects; Bob uses Gemini in one project
        fake_gcp.log_entries = [
            make_inference_entry("alice@company.com", project="proj-a", model="gemini-2.5-pro",
                                 prompt=2_000_000, cand=500_000, ts=now),
            make_inference_entry("alice@company.com", project="proj-b", model="claude-sonnet-4.5",
                                 prompt=1_000_000, cand=400_000, ts=now),
            make_inference_entry("bob@company.com", project="proj-a", model="gemini-2.5-pro",
                                 prompt=1_000_000, cand=100_000, ts=now),
        ]
        caller = auth.Caller("test", "test", "api_key", frozenset(["usage:read"]), "test")
        ctx = svc_base.Ctx(app, app.config, caller, params={"month": month_str})
        report = chargeback.billing_users_report(ctx)

        self.assertEqual(report["month"], month_str)
        self.assertEqual(report["currency"], "USD")
        self.assertEqual(report["total"], 2)
        user_sum = round(sum(u["chargeable"] for u in report["data"]), 2)
        proj_sum = round(sum(p["chargeable"] for p in report["by_project"]), 2)
        self.assertEqual(user_sum, report["totals"]["chargeable"])
        self.assertEqual(proj_sum, report["totals"]["chargeable"])
        # Alice's Gemini ($7.50) is covered by $10 free credit; her Claude ($9.00) is fully chargeable!
        alice = next(u for u in report["data"] if u["email"] == "alice@company.com")
        self.assertEqual(alice["credit_applied"], 7.5)
        self.assertEqual(alice["chargeable"], 9.0)


if __name__ == "__main__":
    unittest.main()
