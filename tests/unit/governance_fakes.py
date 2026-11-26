"""Offline fakes for model governance & quota APIs (Org Policy v2, Model Garden, Discovery Engine, Cloud Quotas,
Cloud Monitoring) layered on top of tests.unit.support.FakeGcp."""

from __future__ import annotations

import copy
import urllib.parse

from tests.unit.support import FakeGcp
from usage_caps_api.gcp import GcpError


class GovernanceFakeGcp(FakeGcp):
    def __init__(self):
        super().__init__()
        self.org_policies: dict[str, dict] = {}       # "projects/p/policies/vertexai.allowedModels" -> policy
        self.publisher_models = {
            "google": [
                {"name": "publishers/google/models/gemini-test-pro", "launchStage": "GA", "versionState": "VERSION_STATE_STABLE"},
                {"name": "publishers/google/models/gemini-test-flash", "launchStage": "GA"},
                {"name": "publishers/google/models/gemini-test-flash-lite", "launchStage": "GA"},
                {"name": "publishers/google/models/gemini-next-pro-preview", "launchStage": "PUBLIC_PREVIEW"},
            ],
            "anthropic": [
                {"name": "publishers/anthropic/models/claude-test-sonnet", "launchStage": "GA"},
                {"name": "publishers/anthropic/models/claude-test-haiku", "launchStage": "GA"},
            ],
        }
        self.engines: dict[str, list[dict]] = {}      # project -> engines (location global)
        self.devtools: dict[tuple[str, str], dict] = {}
        self.quota_infos: dict[str, list[dict]] = {}  # project -> quotaInfos
        self.quota_prefs: dict[str, dict] = {}        # name -> preference
        self.timeseries: dict[str, list[dict]] = {}   # project -> series
        self.orgpolicy_denied: set[str] = set()

    # -- helpers --------------------------------------------------------------
    def _orgpolicy(self, method, url, params, body):
        path = url.split("orgpolicy.googleapis.com/v2/", 1)[1]
        scope = "/".join(path.split("/")[:2])
        if scope in self.orgpolicy_denied:
            raise GcpError("denied", status=403, message="denied")
        if path.endswith(":getEffectivePolicy"):
            name = path[:-len(":getEffectivePolicy")]
            pol = self.org_policies.get(name)
            if pol is None:
                return {"name": name, "spec": {}}
            return copy.deepcopy(pol)
        if method == "GET":
            pol = self.org_policies.get(path)
            if pol is None:
                raise GcpError("not_found", status=404, message="no policy")
            return copy.deepcopy(pol)
        if method == "POST":
            name = body["name"]
            if name in self.org_policies:
                raise GcpError("conflict", status=409, message="exists")
            stored = copy.deepcopy(body)
            stored["spec"]["etag"] = self.next_etag()
            self.org_policies[name] = stored
            return copy.deepcopy(stored)
        if method == "PATCH":
            cur = self.org_policies.get(path)
            if cur is None:
                raise GcpError("not_found", status=404, message="no policy")
            if body["spec"].get("etag") and body["spec"]["etag"] != cur["spec"].get("etag"):
                raise GcpError("precondition", status=412, message="etag")
            stored = copy.deepcopy(body)
            stored["spec"]["etag"] = self.next_etag()
            self.org_policies[path] = stored
            return copy.deepcopy(stored)
        if method == "DELETE":
            if path not in self.org_policies:
                raise GcpError("not_found", status=404, message="no policy")
            del self.org_policies[path]
            return {}
        raise GcpError("invalid", status=400, message="bad")

    def request(self, method, url, *, quota_project="", params=None, json_body=None, data=None, headers=None,
                timeout=30, expect_json=True):
        for pred, handler in self.rest_handlers:
            if pred(method, url, params, json_body):
                return super().request(method, url, quota_project=quota_project, params=params, json_body=json_body)
        if "orgpolicy.googleapis.com/v2/" in url:
            self.rest_calls.append((method, url, params, copy.deepcopy(json_body)))
            return self._orgpolicy(method, url, params, json_body), {}
        if "aiplatform.googleapis.com/v1beta1/publishers/" in url:
            self.rest_calls.append((method, url, params, None))
            pub = url.split("/publishers/", 1)[1].split("/", 1)[0]
            if pub not in self.publisher_models:
                raise GcpError("not_found", status=404, message="publisher")
            return {"publisherModels": copy.deepcopy(self.publisher_models[pub])}, {}
        if "discoveryengine.googleapis.com" in url:
            self.rest_calls.append((method, url, params, copy.deepcopy(json_body)))
            return self._discovery(method, url, params, json_body), {}
        if "cloudquotas.googleapis.com/v1/" in url:
            self.rest_calls.append((method, url, params, copy.deepcopy(json_body)))
            return self._quotas(method, url, params, json_body), {}
        if "monitoring.googleapis.com/v3/projects/" in url:
            project = url.split("/projects/", 1)[1].split("/", 1)[0]
            return {"timeSeries": copy.deepcopy(self.timeseries.get(project, []))}, {}
        return super().request(method, url, quota_project=quota_project, params=params, json_body=json_body,
                               data=data, headers=headers, timeout=timeout, expect_json=expect_json)

    def _discovery(self, method, url, params, body):
        path = urllib.parse.urlsplit(url).path
        parts = path.strip("/").split("/")
        # /v1/projects/{p}/locations/{l}/collections/default_collection/engines[/{id}]
        if parts[0] == "v1alpha" and parts[-1] == "devToolsConfig":
            key = (parts[2], parts[4])
            if key not in self.devtools:
                raise GcpError("not_found", status=404, message="no config")
            return copy.deepcopy(self.devtools[key])
        project, location = parts[2], parts[4]
        engines = self.engines.get(project, []) if location == "global" else []
        if parts[-1] == "engines" and method == "GET":
            return {"engines": copy.deepcopy(engines)}
        engine_id = parts[-1]
        for eng in engines:
            if eng["name"].endswith("/engines/" + engine_id):
                if method == "GET":
                    return copy.deepcopy(eng)
                if method == "PATCH":
                    assert params.get("updateMask") == "modelConfigs"
                    eng["modelConfigs"] = copy.deepcopy(body["modelConfigs"])
                    return copy.deepcopy(eng)
        raise GcpError("not_found", status=404, message="engine")

    def _quotas(self, method, url, params, body):
        path = url.split("cloudquotas.googleapis.com/v1/", 1)[1]
        project = path.split("/")[1]
        if path.endswith("/quotaInfos"):
            if project not in self.quota_infos:
                raise GcpError("denied", status=403, message="SERVICE_DISABLED")
            return {"quotaInfos": copy.deepcopy(self.quota_infos[project])}
        if path.endswith("/quotaPreferences") and method == "GET":
            return {"quotaPreferences": [copy.deepcopy(p) for n, p in self.quota_prefs.items()
                                         if n.startswith("projects/%s/" % project)]}
        if path.endswith("/quotaPreferences") and method == "POST":
            name = "projects/%s/locations/global/quotaPreferences/%s" % (project, params["quotaPreferenceId"])
            pref = copy.deepcopy(body)
            pref["name"] = name
            pref["reconciling"] = True
            pref["quotaConfig"]["grantedValue"] = None
            self.quota_prefs[name] = pref
            return copy.deepcopy(pref)
        if "/quotaPreferences/" in path:
            name = path
            if method == "GET":
                if name not in self.quota_prefs:
                    raise GcpError("not_found", status=404, message="pref")
                return copy.deepcopy(self.quota_prefs[name])
            if method == "PATCH":
                pref = self.quota_prefs.setdefault(name, {"name": name, "quotaConfig": {}})
                pref["quotaConfig"]["preferredValue"] = body["quotaConfig"]["preferredValue"]
                pref["reconciling"] = True
                return copy.deepcopy(pref)
        raise GcpError("not_found", status=404, message="quota path")

    # -- scenario builders ----------------------------------------------------
    def add_ge_app(self, project, engine_id, models=None):
        self.engines.setdefault(project, []).append({
            "name": "projects/%s/locations/global/collections/default_collection/engines/%s" % (project, engine_id),
            "displayName": engine_id, "appType": "APP_TYPE_INTRANET", "modelConfigs": dict(models or {}),
        })

    def add_quota(self, project, quota_id, metric, dims_values, *, eligible=True):
        infos = self.quota_infos.setdefault(project, [])
        infos.append({
            "quotaId": quota_id, "metric": metric, "service": "aiplatform.googleapis.com",
            "quotaDisplayName": quota_id, "metricUnit": "1/min/{project}/{region}/{base_model}",
            "refreshInterval": "minute", "dimensions": ["region", "base_model"],
            "quotaIncreaseEligibility": {"isEligible": eligible},
            "dimensionsInfos": [{"dimensions": d, "details": {"value": str(v)}} for d, v in dims_values],
        })

    def grant(self, name, value):
        pref = self.quota_prefs[name]
        pref["reconciling"] = False
        pref["quotaConfig"]["grantedValue"] = str(value)
