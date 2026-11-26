"""Unit tests for Safety Gates G1-G7, period resets, and real-time Pub/Sub usage event enforcement."""

from __future__ import annotations

import base64
import datetime as dt
import json
import unittest

from tests.unit.support import build_test_app, make_inference_entry, make_test_config
from usage_caps_api import auth, timeutil
from usage_caps_api.services import base as svc_base, caps, usage_events, webhooks


class CapsGatesAndEventsTests(unittest.TestCase):
    def setUp(self):
        usage_events.reset()

    def test_safety_gates_g1_g3_g4_g6_g7_and_period_reset_restore(self):
        policy = {
            "enforcement_backend": "iam_deny",
            "enforcement_mode": "auto",
            "change_budget_min": 2,
            "change_budget_ratio": 0.2,
            "surge_guard_min": 10,
            "surge_guard_ratio": 0.5,
        }
        now = dt.datetime(2026, 11, 15, 0, 5, tzinfo=dt.timezone.utc)

        # G1 failure aborts entire cycle
        gates, plan = caps.evaluate_gates_and_plan(
            [], deny_readable=False, telemetry_ok=True, telemetry_records=10, telemetry_truncated=False,
            policy=policy, deny_meta={"attachments": ["p1"], "current_principals": 0}, now=now,
        )
        self.assertTrue(plan["aborted"])
        self.assertEqual(gates[0]["id"], "G1_POLICY_READABLE")
        self.assertEqual(gates[0]["status"], "fail")

        # G3 unhealthy telemetry withholds usage-based restores, BUT expired period suspension STILL restores!
        users_g3 = [
            {
                "email": "expired@company.com", "status": "suspended", "blocked_services": ["antigravity"],
                "over_cap": False, "utilization_pct": 0.0, "rule": None,
                "suspension": {"until": "2026-11-15T00:00:00Z"},
            },
            {
                "email": "still-active-period@company.com", "status": "suspended", "blocked_services": ["antigravity"],
                "over_cap": False, "utilization_pct": 10.0, "rule": None,
                "suspension": {"until": "2026-12-01T00:00:00Z"},
            },
        ]
        gates, plan = caps.evaluate_gates_and_plan(
            users_g3, deny_readable=True, telemetry_ok=False, telemetry_records=0, telemetry_truncated=False,
            policy=policy, deny_meta={"attachments": ["p1"], "current_principals": 2}, now=now,
        )
        self.assertFalse(plan["aborted"])
        self.assertEqual(plan["restore"], ["expired@company.com"])
        self.assertTrue(plan["withheld_restores"])

        # G4 change budget defers excess least-severe suspensions (budget=2 for 5 users)
        users_g4 = [
            {"email": "u%d@company.com" % i, "status": "over_cap", "blocked_services": [],
             "over_cap": True, "utilization_pct": float(100 + i * 20), "rule": None, "suspension": None}
            for i in range(4)
        ] + [{"email": "ok@company.com", "status": "active", "blocked_services": [],
              "over_cap": False, "utilization_pct": 10.0, "rule": None, "suspension": None}]
        gates, plan = caps.evaluate_gates_and_plan(
            users_g4, deny_readable=True, telemetry_ok=True, telemetry_records=50, telemetry_truncated=False,
            policy=policy, deny_meta={"attachments": ["p1"], "current_principals": 0}, now=now,
        )
        self.assertEqual(plan["suspend"], ["u3@company.com", "u2@company.com"])
        self.assertEqual(plan["deferred"], ["u1@company.com", "u0@company.com"])

        # G7 surge guard triggers when >= 10 users AND >= 50% of population simultaneously over cap
        users_g7 = [
            {"email": "surge%d@company.com" % i, "status": "over_cap", "blocked_services": [],
             "over_cap": True, "utilization_pct": 150.0, "rule": None, "suspension": None}
            for i in range(12)
        ]
        gates, plan = caps.evaluate_gates_and_plan(
            users_g7, deny_readable=True, telemetry_ok=True, telemetry_records=100, telemetry_truncated=False,
            policy=policy, deny_meta={"attachments": ["p1"], "current_principals": 0}, now=now,
        )
        self.assertEqual(plan["suspend"], [])
        self.assertEqual(len(plan["review"]), 12)
        g7 = next(g for g in gates if g["id"] == "G7_SURGE_GUARD")
        self.assertEqual(g7["status"], "warn")

    def test_period_window_boundaries(self):
        now = dt.datetime(2026, 11, 26, 10, 0, tzinfo=dt.timezone.utc)
        win = timeutil.period_window("monthly", "calendar", now=now, tz="UTC", day=1, at="00:00")
        self.assertEqual(win["start_iso"], "2026-11-01T00:00:00Z")
        self.assertEqual(win["next_reset_iso"], "2026-12-01T00:00:00Z")

    def test_realtime_pubsub_usage_event_triggers_webhook_and_auto_enforcement(self):
        delivered: list[tuple[str, dict, dict]] = []

        def fake_sender(url: str, raw: bytes, headers: dict) -> int:
            delivered.append((url, json.loads(raw.decode("utf-8")), headers))
            return 200

        cfg = make_test_config(caps={
            "cost_cap": 10.0,
            "enforcement_mode": "auto",
            "enforcement_backend": "iam_deny",
            "webhook_thresholds": [80, 100],
        })
        app, fake_gcp = build_test_app(cfg=cfg, webhook_sender=fake_sender)
        caller = auth.Caller("pubsub-sa", "PubSub", "oidc", frozenset([auth.INTERNAL_SCOPE, "admin"]), "pubsub")
        ctx = svc_base.Ctx(app, app.config, caller)

        # Register an outbound webhook
        wh = webhooks.create_webhook(svc_base.Ctx(app, app.config, caller, body={
            "name": "portal-hook", "url": "https://portal.example.com/webhooks/gcp-ai",
        }))
        secret = wh["secret"]

        now = dt.datetime.now(dt.timezone.utc)
        # First event: $8.50 (85% of $10 cap) -> fires 80% threshold webhook, does NOT block yet
        e1 = make_inference_entry("carol@company.com", model="gemini-test-pro",
                                  prompt=400_000, cand=800_000, ts=now, insert_id="ev-1")
        # $0.50 input + $8.00 output = $8.50
        push1 = {"message": {"data": base64.b64encode(json.dumps(e1).encode("utf-8")).decode("ascii")}}
        r1 = usage_events.handle_usage_event(svc_base.Ctx(app, app.config, caller, body=push1))
        self.assertEqual(r1["action"], "under_cap")
        self.assertTrue(any(ev["event"] == "cap.threshold_reached" for _, ev, _ in delivered))

        # Verify HMAC-SHA256 signature on delivered webhook!
        _, ev_payload, ev_hdrs = delivered[0]
        raw_bytes = json.dumps(ev_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        expected_sig = webhooks.sign_payload(secret, ev_hdrs["X-Webhook-Timestamp"], raw_bytes)
        self.assertEqual(ev_hdrs["X-Signature-256"], expected_sig)

        # Second event: +$2.50 -> $11.00 (>= 100% cap) -> triggers immediate IAM Deny v2 enforcement!
        e2 = make_inference_entry("carol@company.com", model="gemini-test-pro",
                                  prompt=400_000, cand=200_000, ts=now + dt.timedelta(seconds=5), insert_id="ev-2")
        fake_gcp.log_entries.extend([e1, e2])
        push2 = {"message": {"data": base64.b64encode(json.dumps(e2).encode("utf-8")).decode("ascii")}}
        r2 = usage_events.handle_usage_event(svc_base.Ctx(app, app.config, caller, body=push2))
        self.assertEqual(r2["action"], "enforcement_triggered")
        self.assertEqual(r2["enforcement_result"], "success")
        self.assertTrue(any(ev["event"] == "cap.exceeded" for _, ev, _ in delivered))
        self.assertTrue(any(ev["event"] == "user.blocked" for _, ev, _ in delivered))


if __name__ == "__main__":
    unittest.main()
