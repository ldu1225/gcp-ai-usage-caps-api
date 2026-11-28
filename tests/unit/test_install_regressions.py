"""Install-path regressions found by live installs into brand-new and existing projects."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from usage_caps_api import gcp as gcp_mod
from usage_caps_api.tokengcp import StaticTokenGcp


def _service_disabled(project: str, service: str = "cloudresourcemanager.googleapis.com") -> dict:
    return {"error": {
        "code": 403, "status": "PERMISSION_DENIED",
        "message": f"{service} has not been used in project {project} before or it is disabled.",
        "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED",
                     "domain": "googleapis.com",
                     "metadata": {"service": service, "consumer": f"projects/{project}", "containerInfo": project}}],
    }}


class QuotaProjectRejectedTest(unittest.TestCase):
    def test_service_disabled_on_quota_project(self):
        self.assertTrue(gcp_mod.quota_project_rejected(_service_disabled("new-hub"), "new-hub"))

    def test_other_consumer_is_not_the_quota_project(self):
        self.assertFalse(gcp_mod.quota_project_rejected(_service_disabled("spoke"), "new-hub"))

    def test_plain_permission_denied_is_not_retried(self):
        payload = {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "denied"}}
        self.assertFalse(gcp_mod.quota_project_rejected(payload, "new-hub"))
        self.assertFalse(gcp_mod.quota_project_rejected(_service_disabled("new-hub"), ""))


class _Resp:
    def __init__(self, status: int, body: dict):
        self.status = status
        self._raw = json.dumps(body).encode("utf-8")
        self.headers = {}

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TokenClientBlankHubTest(unittest.TestCase):
    """Preflight on a brand-new hub: x-goog-user-project=<hub> is rejected until the installer enables its APIs."""

    def test_retries_without_quota_header_and_remembers(self):
        client = StaticTokenGcp("tok", environ={}, gcloud_bin="")
        seen = []

        def fake_open(req, timeout=None):
            quota = req.get_header("X-goog-user-project")
            seen.append(quota)
            if quota == "new-hub":
                raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {},
                                             io.BytesIO(json.dumps(_service_disabled("new-hub")).encode()))
            return _Resp(200, {"projectId": "new-hub", "state": "ACTIVE"})

        with mock.patch.object(client._opener, "open", side_effect=fake_open):
            payload, _ = client.request("GET", "https://cloudresourcemanager.googleapis.com/v3/projects/new-hub",
                                        quota_project="new-hub")
            self.assertEqual(payload["projectId"], "new-hub")
            self.assertEqual(seen, ["new-hub", None])
            client.request("GET", "https://cloudresourcemanager.googleapis.com/v3/projects/new-hub",
                           quota_project="new-hub")
            self.assertEqual(seen, ["new-hub", None, None])
        client.close()

    def test_real_denial_still_raises(self):
        client = StaticTokenGcp("tok", environ={}, gcloud_bin="")

        def fake_open(req, timeout=None):
            body = {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "caller lacks permission"}}
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(json.dumps(body).encode()))

        with mock.patch.object(client._opener, "open", side_effect=fake_open) as opened:
            with self.assertRaises(gcp_mod.GcpError) as ctx:
                client.request("GET", "https://cloudresourcemanager.googleapis.com/v3/projects/p", quota_project="p")
            self.assertEqual(ctx.exception.kind, "denied")
            self.assertEqual(opened.call_count, 1)
        client.close()


if __name__ == "__main__":
    unittest.main()
