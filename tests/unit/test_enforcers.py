"""Unit tests for multi-rule IAM Deny v2 enforcer, foreign policy protection, and invariants."""

from __future__ import annotations

import unittest

from tests.unit.support import build_test_app
from usage_caps_api import auth, identity
from usage_caps_api.services import base as svc_base, enforcers


class EnforcersTests(unittest.TestCase):
    def _ctx(self, app):
        caller = auth.Caller("admin", "admin", "api_key", frozenset(["admin"]), "admin")
        return svc_base.Ctx(app, app.config, caller)

    def test_multi_rule_per_service_blocking_and_unblocking(self):
        app, _ = build_test_app()
        ctx = self._ctx(app)
        p_alice = identity.deny_principal("alice@company.com")
        p_bob = identity.deny_principal("bob@company.com")
        p_wf = identity.deny_principal(
            "principal://iam.googleapis.com/locations/global/workforcePools/corp-pool/subject/User-42"
        )

        # Initial empty state
        st0 = enforcers.read_state(ctx)
        self.assertTrue(st0.readable)
        self.assertEqual(st0.principals, frozenset())

        # Block Alice on antigravity only, Bob on antigravity + vertex_ai, Workforce user on all 3
        desired = {
            p_alice: frozenset(["antigravity"]),
            p_bob: frozenset(["antigravity", "vertex_ai"]),
            p_wf: frozenset(["antigravity", "vertex_ai", "gemini_enterprise"]),
        }
        res = enforcers.apply(ctx, st0, desired)
        self.assertTrue(res["applied"])
        self.assertEqual(res["violations"], [])

        # Re-read state and verify exact per-person services preserved!
        st1 = enforcers.read_state(ctx)
        self.assertTrue(st1.readable)
        by_person = st1.services_by_person
        self.assertEqual(by_person["alice@company.com"], frozenset(["antigravity"]))
        self.assertEqual(by_person["bob@company.com"], frozenset(["antigravity", "vertex_ai"]))
        self.assertEqual(
            by_person["principal://iam.googleapis.com/locations/global/workforcepools/corp-pool/subject/user-42"],
            frozenset(["antigravity", "vertex_ai", "gemini_enterprise"]),
        )

        # Clear all -> deletes the managed deny policy cleanly
        res_del = enforcers.apply(ctx, st1, {})
        self.assertTrue(res_del["applied"])
        self.assertEqual(res_del["items"][0]["op"], "delete")
        st2 = enforcers.read_state(ctx)
        self.assertFalse(st2.attachments[0].exists)

    def test_foreign_policy_detected_and_never_modified(self):
        app, fake_gcp = build_test_app()
        ctx = self._ctx(app)
        ap = enforcers.attachment_points(ctx)[0]
        pid = enforcers.policy_id(app.config)
        url = enforcers._policy_url(ap, pid)

        # Plant a foreign policy with an exceptionPrincipal and unknown permission
        fake_gcp.deny_policies[url] = {
            "etag": "foreign-etag",
            "rules": [{
                "denyRule": {
                    "deniedPrincipals": ["principal://goog/subject/alice@company.com"],
                    "deniedPermissions": ["compute.googleapis.com/instances.delete"],
                    "exceptionPrincipals": ["principal://goog/subject/boss@company.com"],
                }
            }],
        }
        st = enforcers.read_state(ctx)
        self.assertFalse(st.readable)
        self.assertTrue(st.attachments[0].foreign)

        # Apply refuses to touch foreign policy
        res = enforcers.apply(ctx, st, {"principal://goog/subject/bob@company.com": frozenset(["antigravity"])})
        self.assertFalse(res["applied"])
        self.assertEqual(fake_gcp.deny_policies[url]["etag"], "foreign-etag")

    def test_invariants_reject_bad_principals_and_over_limit(self):
        app, _ = build_test_app()
        ctx = self._ctx(app)
        aps = enforcers.attachment_points(ctx)
        bad_desired = {"group:devs@company.com": frozenset(["antigravity"])}
        items = [{"attachment": aps[0], "op": "create", "add": [], "remove": [], "count": 1, "foreign": False, "error": None}]
        violations = enforcers.check_invariants(app.config, items, bad_desired, aps)
        self.assertTrue(any("not valid person deny URIs" in v for v in violations))

        # Exceeding 2,500 principals rejected
        too_many = {"principal://goog/subject/u%d@company.com" % i: frozenset(["antigravity"]) for i in range(2501)}
        violations2 = enforcers.check_invariants(app.config, items, too_many, aps)
        self.assertTrue(any("2500" in v for v in violations2))


if __name__ == "__main__":
    unittest.main()
