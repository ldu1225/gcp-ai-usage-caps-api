"""End-to-end offline HTTP tests covering every /v1 endpoint, pagination, webhooks, and enforce_job."""

from __future__ import annotations

import datetime as dt
import io
import json
import unittest
from contextlib import redirect_stdout

from tests.unit.support import LiveTestServer, build_test_app, make_inference_entry
from usage_caps_api import enforce_job


class HttpEndpointsE2ETests(unittest.TestCase):
    def test_full_api_lifecycle(self):
        delivered_hooks = []

        def fake_sender(url: str, raw: bytes, headers: dict) -> int:
            delivered_hooks.append((url, json.loads(raw.decode("utf-8")), headers))
            return 200

        app, fake_gcp = build_test_app(webhook_sender=fake_sender)
        now = dt.datetime.now(dt.timezone.utc)
        fake_gcp.log_entries = [
            make_inference_entry("alice@company.com", model="gemini-2.5-pro", prompt=1_000_000, cand=500_000, ts=now),
            make_inference_entry("bob@company.com", model="claude-sonnet-4.5", prompt=2_000_000, cand=1_000_000, ts=now),
        ]
        admin_token = "gcpai_boot_secret_1234567890"

        with LiveTestServer(app) as srv:
            # 1) Public health & OpenAPI 3.1 spec
            st, health, _ = srv.request("GET", "/v1/health")
            self.assertEqual(st, 200)
            self.assertEqual(health["status"], "ok")

            st, spec_text, hdrs = srv.request("GET", "/v1/openapi.yaml")
            self.assertEqual(st, 200)
            self.assertIn("openapi: 3.1.0", spec_text)
            self.assertIn("yaml", hdrs.get("Content-Type", ""))

            # 2) Usage list + pagination + single-user detail
            st, u_page1, _ = srv.request("GET", "/v1/usage/users?limit=1", token=admin_token)
            self.assertEqual(st, 200)
            self.assertEqual(len(u_page1["data"]), 1)
            self.assertTrue(u_page1["has_more"])
            self.assertIsNotNone(u_page1["next_cursor"])

            st, u_page2, _ = srv.request("GET", "/v1/usage/users?limit=1&cursor=%s" % u_page1["next_cursor"],
                                         token=admin_token)
            self.assertEqual(st, 200)
            self.assertEqual(len(u_page2["data"]), 1)
            self.assertFalse(u_page2["has_more"])

            st, detail, _ = srv.request("GET", "/v1/usage/users/alice%40company.com", token=admin_token)
            self.assertEqual(st, 200)
            self.assertEqual(detail["user"]["email"], "alice@company.com")
            self.assertTrue(len(detail["timeseries"]) >= 1)
            self.assertIn("current_cap", detail)

            # 3) Update global cap policy ($10/mo, auto)
            st, pol, _ = srv.request("PUT", "/v1/caps/policy",
                                     body={"cost_cap": 10.0, "enforcement_mode": "auto"}, token=admin_token)
            self.assertEqual(st, 200)
            self.assertEqual(pol["cost_cap"], 10.0)
            self.assertEqual(pol["enforcement_mode"], "auto")

            # 4) Register webhook (and verify SSRF private IP blocked!)
            st, bad_wh, _ = srv.request("POST", "/v1/admin/webhooks",
                                        body={"url": "https://127.0.0.1/steal"}, token=admin_token)
            self.assertEqual(st, 400)

            st, wh, _ = srv.request("POST", "/v1/admin/webhooks",
                                    body={"name": "portal", "url": "https://portal.example.com/hook"},
                                    token=admin_token)
            self.assertEqual(st, 201)
            self.assertTrue(wh["secret"].startswith("whsec_"))

            # 5) Set & delete per-user individual spend cap for alice ($50/mo)
            st, rule, _ = srv.request("PUT", "/v1/caps/users/alice%40company.com",
                                      body={"type": "limit", "cost_cap": 50.0, "note": "VIP"}, token=admin_token)
            self.assertEqual(st, 200)
            self.assertEqual(rule["rule"]["cost_cap"], 50.0)

            st, caps_list, _ = srv.request("GET", "/v1/caps/users", token=admin_token)
            self.assertEqual(st, 200)
            by_em = {u["email"]: u for u in caps_list["data"]}
            self.assertEqual(by_em["alice@company.com"]["status"], "active")
            self.assertEqual(by_em["bob@company.com"]["status"], "over_cap")  # Bob spent $21 > $10 cap

            # 6) Enforce dryRun=true -> plans Bob suspension without mutating IAM
            st, dry, _ = srv.request("POST", "/v1/caps/enforce", body={"dryRun": True}, token=admin_token)
            self.assertEqual(st, 200)
            self.assertEqual(dry["result"], "dry_run")
            self.assertEqual(dry["plan"]["suspend"], ["bob@company.com"])

            # 7) Enforce dryRun=false -> suspends Bob via IAM Deny v2 & sends webhook
            st, real, _ = srv.request("POST", "/v1/caps/enforce", body={"dryRun": False}, token=admin_token)
            self.assertEqual(st, 200)
            self.assertEqual(real["result"], "success")
            self.assertEqual(real["changes"][0]["email"], "bob@company.com")

            st, bob_cap, _ = srv.request("GET", "/v1/caps/users/bob%40company.com", token=admin_token)
            self.assertEqual(st, 200)
            self.assertEqual(bob_cap["status"], "suspended")

            # 8) Manual service-selective block & partial unblock for Alice
            st, blk, _ = srv.request("POST", "/v1/users/alice%40company.com/block",
                                     body={"services": ["antigravity", "vertex_ai"], "reason": "security review"},
                                     token=admin_token)
            self.assertEqual(st, 200)
            self.assertEqual(blk["blocked_services"], ["antigravity", "vertex_ai"])

            # Unblock only vertex_ai -> antigravity stays blocked!
            st, part_unblk, _ = srv.request("POST", "/v1/users/alice%40company.com/unblock",
                                            body={"services": ["vertex_ai"]}, token=admin_token)
            self.assertEqual(st, 200)
            self.assertEqual(part_unblk["status"], "blocked")
            self.assertEqual(part_unblk["blocked_services"], ["antigravity"])

            # Full unblock -> active
            st, full_unblk, _ = srv.request("POST", "/v1/users/alice%40company.com/unblock", body={}, token=admin_token)
            self.assertEqual(st, 200)
            self.assertEqual(full_unblk["status"], "active")
            self.assertEqual(full_unblk["blocked_services"], [])

            # 9) Delete Alice's individual cap rule
            st, del_r, _ = srv.request("DELETE", "/v1/caps/users/alice%40company.com", token=admin_token)
            self.assertEqual(st, 200)
            self.assertTrue(del_r["deleted"])

            # 10) Audit log captured all governance actions
            st, audits, _ = srv.request("GET", "/v1/audit-logs?limit=20", token=admin_token)
            self.assertEqual(st, 200)
            actions = {e["action"] for e in audits["data"]}
            self.assertTrue({"caps.policy.updated", "caps.enforce", "user.blocked", "user.unblocked"}.issubset(actions))

            # 11) Delete webhook
            st, del_wh, _ = srv.request("DELETE", "/v1/admin/webhooks/%s" % wh["id"], token=admin_token)
            self.assertEqual(st, 200)
            self.assertTrue(del_wh["deleted"])

            # 12) Cloud Run Enforcer Job dry-run outputs clean PII-free JSON line
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = enforce_job.main(["--dry-run", "--trigger", "scheduler"], app=app, environ={})
            self.assertEqual(rc, 0)
            line = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(line["result"], "dry_run")
            self.assertNotIn("company.com", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
