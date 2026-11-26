"""Runs scripts/e2e_live_test.py against the real HTTP API with a simulated Gemini Platform: every allowed call writes
an inference_request log entry, and calls return 403 while the IAM Deny policy denies the test service account."""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import os
import tempfile
import unittest
from unittest import mock

from tests.unit.governance_fakes import GovernanceFakeGcp
from tests.unit.support import LiveTestServer, build_test_app, make_test_config, make_vertex_entry
from usage_caps_api import config, identity
from usage_caps_api.services import usage_events

ADMIN = "gcpai_boot_secret_1234567890"
SA = "caps-e2e-tester@test-ai-project.iam.gserviceaccount.com"
SA_URI = "principal://iam.googleapis.com/projects/-/serviceAccounts/" + SA


def _load_script():
    path = os.path.join(config.repo_root(), "scripts", "e2e_live_test.py")
    spec = importlib.util.spec_from_file_location("e2e_live_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class E2EScriptHarnessTests(unittest.TestCase):
    def setUp(self):
        usage_events.reset()
        cfg = make_test_config(**{"hub": {"service_account_subjects": True},
                                  "caps": {"enforcement_mode": "manual"}})
        self.app, self.gcp = build_test_app(cfg=cfg, gcp=GovernanceFakeGcp())
        self.srv = LiveTestServer(self.app).__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        self.addCleanup(identity.configure)
        self.seq = 0

        def logging_cfg(method, url, params, body):
            if url.endswith(":fetchPublisherModelConfig"):
                return {"publisherModelConfig": {"inferenceEventLoggingConfig": {"state": "ENABLED"}}}
            return {"name": "operations/1"}
        self.gcp.rest_handlers.append((lambda m, u, p, b: "PublisherModelConfig" in u, logging_cfg))

    def _denied(self) -> bool:
        for pol in self.gcp.deny_policies.values():
            for rule in pol.get("rules") or []:
                if SA_URI in ((rule.get("denyRule") or {}).get("deniedPrincipals") or []):
                    return True
        return False

    def _call_model(self):
        if self._denied():
            return 403
        self.seq += 1
        self.gcp.log_entries.append(make_vertex_entry(user=SA, principal=SA, ts=dt.datetime.now(dt.timezone.utc),
                                                      insert_id="e2e-%d" % self.seq))
        return 200

    def test_script_passes_every_step(self):
        mod = _load_script()
        report = os.path.join(tempfile.mkdtemp(), "report.md")
        args = argparse.Namespace(api_url=self.srv.base_url, admin_key=ADMIN, project="test-ai-project",
                                  location="global", test_sa=SA, model="gemini-test-pro", cap=3, log_wait=5,
                                  propagation_wait=5, poll_seconds=0, skip_group=False, quick=False,
                                  stop_on_fail=False, report=report)
        run = mod.Run(args)
        with mock.patch.object(mod.Run, "call_model", lambda _self: self._call_model()), \
                mock.patch.object(mod.time, "sleep", lambda _s: None):
            self.assertTrue(mod.phase_preflight(run))
            mod.phase_user_cap(run)
            mod.phase_group(run)
            for _, fn in reversed(run.cleanup):
                fn()
            ok = mod.write_report(run, report)
        failed = [s for s in run.steps if s["result"] != "PASS"]
        self.assertEqual(failed, [])
        self.assertTrue(ok)
        with open(report, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("A7 model call denied (HTTP 403)", text)
        self.assertIn("C3 user unblock cannot override group block", text)
        # Cleanup restored the original policy and removed test objects.
        self.assertEqual(self.srv.request("GET", "/v1/caps/policy", token=ADMIN)[1]["enforcement_mode"], "manual")
        self.assertEqual(self.srv.request("GET", "/v1/groups", token=ADMIN)[1]["data"], [])
        self.assertFalse(self._denied())


if __name__ == "__main__":
    unittest.main()
