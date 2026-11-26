"""Unit tests for Cloud Billing Budget API v1 project-level Spend Caps."""

from __future__ import annotations

import unittest

from tests.unit.support import LiveTestServer, build_test_app, make_test_config


class ProjectSpendCapsTests(unittest.TestCase):
    def test_project_spend_cap_create_update_enforced_and_lift(self):
        cfg = make_test_config(
            workspace={"billing": {"account_id": "012345-6789AB-CDEF01"}},
            caps={"spend_caps_enabled": True},
        )
        app, fake_gcp = build_test_app(cfg=cfg)
        token = "gcpai_boot_secret_1234567890"

        with LiveTestServer(app) as srv:
            # 1) Create hard spend cap ($500/mo) on workspace project
            st, body, _ = srv.request("PUT", "/v1/caps/projects/test-ai-project",
                                      body={"monthly_usd": 500.0, "action": "hard_cap", "apply": True},
                                      token=token)
            self.assertEqual(st, 200)
            self.assertEqual(body["apply_result"]["status"], "created")
            self.assertEqual(len(fake_gcp.budgets), 1)

            # 2) Project outside workspace is refused (outside_workspace)
            st, body, _ = srv.request("PUT", "/v1/caps/projects/other-team-project",
                                      body={"monthly_usd": 100.0, "action": "hard_cap", "apply": True},
                                      token=token)
            self.assertEqual(st, 200)
            self.assertEqual(body["apply_result"]["status"], "outside_workspace")

            # 3) Update amount ($800/mo)
            st, body, _ = srv.request("PUT", "/v1/caps/projects/test-ai-project",
                                      body={"monthly_usd": 800.0, "action": "hard_cap", "apply": True},
                                      token=token)
            self.assertEqual(st, 200)
            self.assertEqual(body["apply_result"]["status"], "updated")

            # 4) Simulate Google Cloud marking the spend cap ENFORCED -> update blocked, lift succeeds!
            fake_gcp.budgets[0]["spendCap"]["outputState"] = "ENFORCED"
            st, body, _ = srv.request("PUT", "/v1/caps/projects/test-ai-project",
                                      body={"monthly_usd": 900.0, "action": "hard_cap", "apply": True},
                                      token=token)
            self.assertEqual(st, 200)
            self.assertEqual(body["apply_result"]["status"], "enforced")

            # Lift the ENFORCED cap
            st, lift_res, _ = srv.request("POST", "/v1/caps/projects/test-ai-project/lift", body={}, token=token)
            self.assertEqual(st, 200)
            self.assertEqual(lift_res["status"], "lifted")
            self.assertEqual(fake_gcp.budgets[0]["spendCap"]["inputState"], "AWAITING_NEXT_PERIOD")

            # 5) GET /v1/caps/projects reports current status
            st, listed, _ = srv.request("GET", "/v1/caps/projects", token=token)
            self.assertEqual(st, 200)
            by_pid = {r["project_id"]: r for r in listed["data"]}
            self.assertEqual(by_pid["test-ai-project"]["cloud_billing_spend_cap"]["state"], "AWAITING_NEXT_PERIOD")
            self.assertFalse(by_pid["other-team-project"]["in_workspace"])
            self.assertIsNone(by_pid["other-team-project"]["cloud_billing_spend_cap"])


if __name__ == "__main__":
    unittest.main()
