"""Offline tests of the environment readiness (preflight) engine, the preflight CLI and the install scripts."""

from __future__ import annotations

import copy
import io
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
import urllib.parse
from contextlib import redirect_stdout
from unittest import mock

from tests.unit import support  # noqa: F401  (adds src/ to sys.path)

from usage_caps_api import config, preflight  # noqa: E402
from usage_caps_api.gcp import GcpError  # noqa: E402
from usage_caps_api.httpkit import ApiError  # noqa: E402
from usage_caps_api.services import readiness  # noqa: E402
from usage_caps_api.services.base import Ctx  # noqa: E402
from usage_caps_api.services.readiness_core import loc_match, loc_verdict, normalize_policy  # noqa: E402
from usage_caps_api.tokengcp import StaticTokenGcp  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
HUB, SPOKE = "hub-proj", "spoke-proj"
NUMBERS = {HUB: "111111111111", SPOKE: "222222222222"}
ORG = "999"
RUNTIME = "ai-usage-caps-api@hub-proj.iam.gserviceaccount.com"
ENFORCER = "ai-usage-caps-enforcer@hub-proj.iam.gserviceaccount.com"
ALL_SERVICES = {
    "run.googleapis.com", "artifactregistry.googleapis.com", "cloudbuild.googleapis.com", "storage.googleapis.com",
    "secretmanager.googleapis.com", "pubsub.googleapis.com", "cloudscheduler.googleapis.com", "iam.googleapis.com",
    "iamcredentials.googleapis.com", "cloudresourcemanager.googleapis.com", "serviceusage.googleapis.com",
    "logging.googleapis.com", "bigquery.googleapis.com", "cloudbilling.googleapis.com",
    "billingbudgets.googleapis.com", "orgpolicy.googleapis.com", "cloudquotas.googleapis.com",
    "monitoring.googleapis.com", "accesscontextmanager.googleapis.com", "aiplatform.googleapis.com",
    "discoveryengine.googleapis.com", "businessaicode.googleapis.com",
}
RUNTIME_ROLES = ["roles/browser", "roles/serviceusage.serviceUsageConsumer", "roles/logging.viewer",
                 "roles/logging.privateLogViewer", "roles/bigquery.jobUser", "roles/bigquery.dataViewer",
                 "roles/cloudquotas.admin", "roles/monitoring.viewer", "roles/discoveryengine.editor"]
ALL_PERMS_GRANTED = None  # sentinel: testIamPermissions grants everything asked
AI_FILTER = ('log_id("businessaicode.googleapis.com/inference_response") OR (log_id("cloudaudit.googleapis.com/'
             'data_access") AND protoPayload.serviceName="aiplatform.googleapis.com") OR log_id("discoveryengine.'
             'googleapis.com/gemini_enterprise_user_activity") OR log_id("discoveryengine.googleapis.com/'
             'gen_ai_user_message")')


def _now_ms() -> str:
    return str(int(time.time() * 1000))


def green_project(pid: str, hub: bool) -> dict:
    """A fully ready project model."""
    writer = "serviceAccount:service-%s@gcp-sa-logging.iam.gserviceaccount.com" % NUMBERS[pid]
    sinks = [
        {"name": "_Required", "destination": "logging.googleapis.com/projects/%s/locations/global/buckets/_Required" % pid},
        {"name": "_Default", "destination": "logging.googleapis.com/projects/%s/locations/global/buckets/_Default" % pid,
         "exclusions": []},
        {"name": "gcp-ai-usage-caps-api-ai-logs", "filter": AI_FILTER, "writerIdentity": writer,
         "destination": "bigquery.googleapis.com/projects/%s/datasets/ge_analytics" % HUB},
        {"name": "gcp-ai-usage-caps-api-usage-events",
         "filter": 'log_id("businessaicode.googleapis.com/inference_response")', "writerIdentity": writer,
         "destination": "pubsub.googleapis.com/projects/%s/topics/gcp-ai-usage-caps-api-usage-events" % HUB},
    ]
    bindings = [{"role": r, "members": ["serviceAccount:%s" % RUNTIME]} for r in RUNTIME_ROLES]
    bindings += [{"role": r, "members": ["serviceAccount:%s" % ENFORCER]}
                 for r in ("roles/logging.viewer", "roles/logging.privateLogViewer")]
    model = {
        "number": NUMBERS[pid],
        "state": "ACTIVE",
        "parent": "folders/10",
        "ancestry": [{"resourceId": {"type": "project", "id": pid}}, {"resourceId": {"type": "folder", "id": "10"}},
                     {"resourceId": {"type": "organization", "id": ORG}}],
        "billing": {"billingEnabled": True, "billingAccountName": "billingAccounts/0X0X0X-0X0X0X-0X0X0X"},
        "services": set(ALL_SERVICES),
        "iam": {"bindings": bindings, "auditConfigs": [
            {"service": "aiplatform.googleapis.com",
             "auditLogConfigs": [{"logType": "DATA_READ"}, {"logType": "DATA_WRITE"}]}]},
        "sinks": sinks,
        "datasets": {},
        "engines": {"global": [{"name": "projects/%s/locations/global/collections/default_collection/engines/app1" % pid,
                                "displayName": "Corp GE", "appType": "APP_TYPE_INTRANET"}]},
        "devtools": {"global": {"devToolsEnabled": True}},
        "logs": True,
        "granted": ALL_PERMS_GRANTED,
    }
    if hub:
        model["datasets"] = {
            "ge_analytics": {"location": "asia-northeast3", "tables": {
                "businessaicode_googleapis_com_inference_response": _now_ms(),
                "cloudaudit_googleapis_com_data_access": _now_ms(),
                "discoveryengine_googleapis_com_gen_ai_user_message": _now_ms()},
                "access": [{"role": "WRITER", "userByEmail": "service-%s@gcp-sa-logging.iam.gserviceaccount.com"
                            % n} for n in NUMBERS.values()]},
            "billing_export": {"location": "asia-northeast3",
                               "tables": {"gcp_billing_export_resource_v1_0X0X0X": _now_ms()}, "access": []},
        }
        model["topic_iam"] = {"bindings": [{"role": "roles/pubsub.publisher", "members": [
            "serviceAccount:service-%s@gcp-sa-logging.iam.gserviceaccount.com" % n for n in NUMBERS.values()]}]}
    return model


class FakeCloud:
    """URL-routed in-memory model of the Google Cloud APIs read by the readiness engine."""

    def __init__(self):
        self.projects = {HUB: green_project(HUB, True), SPOKE: green_project(SPOKE, False)}
        self.org_policies: dict[tuple, dict] = {}
        self.orgpolicy_error: GcpError | None = None
        self.perimeters: list[dict] = []
        self.org_iam = {"bindings": [
            {"role": "roles/orgpolicy.policyAdmin", "members": ["serviceAccount:%s" % RUNTIME]},
            {"role": "roles/iam.denyReviewer", "members": ["serviceAccount:%s" % RUNTIME]},
            {"role": "roles/iam.denyAdmin", "members": ["serviceAccount:%s" % ENFORCER]},
        ]}
        self.org_sinks: list[dict] = []
        self.hub_resources = True
        self.calls: list[tuple] = []
        self.unknown_permissions: set[str] = set()

    # -- client contract -----------------------------------------------------------
    def request(self, method, url, *, quota_project="", params=None, json_body=None, data=None, headers=None,
                timeout=30, expect_json=True):
        self.calls.append((method, url, copy.deepcopy(params), copy.deepcopy(json_body)))
        return copy.deepcopy(self._route(method, url, params or {}, json_body or {})), {}

    def paginate(self, url, *, quota_project="", items_key, params=None, max_pages=10, page_size=None, method="GET",
                 json_body=None, token_param="pageToken"):
        payload, _ = self.request(method, url, quota_project=quota_project, params=params, json_body=json_body)
        return list(payload.get(items_key) or []), False

    def query(self, *a, **k):
        raise GcpError("not_found", status=404, message="no bigquery in readiness")

    def run_parallel(self, tasks, pool=None):
        out = {}
        for name, fn in tasks.items():
            try:
                out[name] = (True, fn())
            except Exception as err:  # pylint: disable=broad-except
                out[name] = (False, err)
        return out

    # -- routing -------------------------------------------------------------------
    @staticmethod
    def _nf(what="not found"):
        return GcpError("not_found", status=404, message=what)

    def _proj(self, pid):
        if pid not in self.projects:
            raise GcpError("denied", status=403, message="permission denied or project missing")
        return self.projects[pid]

    def _route(self, method, url, params, body):  # noqa: C901 - a router
        u = urllib.parse.urlsplit(url)
        host, path = u.hostname or "", urllib.parse.unquote(u.path)
        m = re.match(r"^/v3/projects/([^/:]+)$", path)
        if host.startswith("cloudresourcemanager") and m:
            p = self._proj(m.group(1))
            return {"name": "projects/%s" % p["number"], "projectId": m.group(1), "state": p["state"],
                    "parent": p["parent"], "displayName": m.group(1)}
        m = re.match(r"^/v1/projects/([^/:]+):getAncestry$", path)
        if m:
            return {"ancestor": self._proj(m.group(1))["ancestry"]}
        m = re.match(r"^/v1/projects/([^/:]+):getIamPolicy$", path)
        if m:
            return self._proj(m.group(1))["iam"]
        if re.match(r"^/v1/organizations/[0-9]+:getIamPolicy$", path):
            return self.org_iam
        if re.match(r"^/v3/folders/[0-9]+:getIamPolicy$", path):
            return {"bindings": []}
        m = re.match(r"^/v1/projects/([^/:]+):testIamPermissions$", path)
        if m:
            p = self._proj(m.group(1))
            asked = list(body.get("permissions") or [])
            if any(a in self.unknown_permissions for a in asked):
                raise GcpError("invalid", status=400, message="Permission %s is not valid" % asked[0])
            granted = p.get("granted")
            return {"permissions": asked if granted is None else [a for a in asked if a in granted]}
        m = re.match(r"^/v1/projects/([^/]+)/billingInfo$", path)
        if m:
            return self._proj(m.group(1))["billing"]
        m = re.match(r"^/v1/projects/([^/]+)/services:batchGet$", path)
        if host.startswith("serviceusage") and m:
            p = self._proj(m.group(1))
            out = []
            for name in params.get("names") or []:
                svc = name.rsplit("/", 1)[-1]
                out.append({"name": name, "config": {"name": svc},
                            "state": "ENABLED" if svc in p["services"] else "DISABLED"})
            return {"services": out}
        if host.startswith("orgpolicy"):
            return self._orgpolicy(path)
        if host.startswith("accesscontextmanager"):
            if path == "/v1/accessPolicies":
                return {"accessPolicies": [{"name": "accessPolicies/77"}]} if self.perimeters else {}
            if path == "/v1/accessPolicies/77/servicePerimeters":
                return {"servicePerimeters": self.perimeters}
        if host.startswith("bigquery"):
            return self._bigquery(path)
        if host.startswith("logging"):
            return self._logging(method, path, body)
        if "discoveryengine" in host:
            m = re.match(r"^/v1/projects/([^/]+)/locations/([^/]+)/collections/default_collection/engines$", path)
            if m:
                engines = self._proj(m.group(1))["engines"].get(m.group(2))
                if engines is None:
                    raise self._nf()
                return {"engines": engines}
            m = re.match(r"^/v1alpha/projects/([^/]+)/locations/([^/]+)/devToolsConfig$", path)
            if m:
                cfg = self._proj(m.group(1))["devtools"].get(m.group(2))
                if cfg is None:
                    raise self._nf()
                return cfg
        if "aiplatform" in host and "/publishers/google/models" in path:
            return {"publisherModels": [{"name": "publishers/google/models/gemini-2.5-pro"}]}
        if host.startswith("cloudquotas"):
            m = re.match(r"^/v1/projects/([^/]+)/locations/global/services/[^/]+/quotaInfos$", path)
            if m:
                p = self._proj(m.group(1))
                if "cloudquotas.googleapis.com" not in p["services"]:
                    raise GcpError("denied", status=403, message="Cloud Quotas API has not been used in project "
                                   "before or it is disabled. SERVICE_DISABLED cloudquotas.googleapis.com")
                return {"quotaInfos": [{"quotaId": "x"}]}
        if host.startswith("iam") and "/denypolicies" in path:
            if path.endswith("/denypolicies"):
                return {"policies": [{"name": "policies/x/denypolicies/gcp-ai-caps-managed"}]}
            return {"name": "gcp-ai-caps-managed", "rules": [{"denyRule": {"deniedPrincipals": ["principal://a"]}}]}
        if host.startswith("pubsub"):
            if path.endswith(":getIamPolicy"):
                return self.projects[HUB].get("topic_iam") or {}
            if not self.hub_resources:
                raise self._nf()
            if "/subscriptions/" in path:
                return {"pushConfig": {"pushEndpoint": "https://x.run.app/v1/internal/usage-event"}}
            return {"name": path}
        if host.startswith("cloudscheduler") or host.startswith("run."):
            if not self.hub_resources:
                raise self._nf()
            return {"state": "ENABLED", "uri": "https://x.run.app", "ingress": "INGRESS_TRAFFIC_ALL"}
        if host.startswith("compute"):
            if "/subnetworks/" in path:
                return {"network": "projects/%s/global/networks/corp" % HUB, "privateIpGoogleAccess": True,
                        "ipCidrRange": "10.0.0.0/24"}
            if path.endswith("/routers"):
                return {"items": [{"name": "r1", "network": "projects/x/global/networks/corp",
                                   "nats": [{"name": "nat1"}]}]}
        if host.startswith("dns"):
            return {"managedZones": []}
        raise self._nf("unmocked %s %s" % (method, url))

    def _orgpolicy(self, path):
        if self.orgpolicy_error is not None:
            raise self.orgpolicy_error
        if path.endswith("/customConstraints"):
            return {"customConstraints": []}
        m = re.match(r"^/v2/projects/([^/]+)/policies/([^/:]+):getEffectivePolicy$", path)
        if m:
            return self.org_policies.get((m.group(1), m.group(2))) or {"name": path, "spec": {}}
        m = re.match(r"^/v2/projects/([^/]+)/policies/([^/:]+)$", path)
        if m:
            raise self._nf()
        raise self._nf()

    def _bigquery(self, path):
        m = re.match(r"^/bigquery/v2/projects/([^/]+)/datasets$", path)
        if m:
            p = self._proj(m.group(1))
            return {"datasets": [{"datasetReference": {"datasetId": d}} for d in p["datasets"]]}
        m = re.match(r"^/bigquery/v2/projects/([^/]+)/datasets/([^/]+)$", path)
        if m:
            ds = self._proj(m.group(1))["datasets"].get(m.group(2))
            if ds is None:
                raise self._nf()
            return {"location": ds["location"], "access": ds.get("access") or []}
        m = re.match(r"^/bigquery/v2/projects/([^/]+)/datasets/([^/]+)/tables$", path)
        if m:
            ds = self._proj(m.group(1))["datasets"].get(m.group(2))
            if ds is None:
                raise self._nf()
            return {"tables": [{"tableReference": {"tableId": t}} for t in ds["tables"]]}
        m = re.match(r"^/bigquery/v2/projects/([^/]+)/datasets/([^/]+)/tables/([^/]+)$", path)
        if m:
            ds = self._proj(m.group(1))["datasets"].get(m.group(2))
            if ds is None or m.group(3) not in ds["tables"]:
                raise self._nf()
            return {"lastModifiedTime": ds["tables"][m.group(3)], "location": ds["location"]}
        raise self._nf()

    def _logging(self, method, path, body):
        m = re.match(r"^/v2/projects/([^/]+)/sinks$", path)
        if m:
            return {"sinks": self._proj(m.group(1))["sinks"]}
        if re.match(r"^/v2/(folders|organizations)/[0-9]+/sinks$", path):
            return {"sinks": self.org_sinks if "organizations" in path else []}
        if re.match(r"^/v2/projects/([^/]+)/exclusions$", path):
            return {"exclusions": []}
        if path == "/v2/entries:list":
            pid = body["resourceNames"][0].split("/", 1)[1]
            return {"entries": [{"timestamp": "now"}]} if self._proj(pid)["logs"] else {}
        raise self._nf()


def make_ctx(cloud: FakeCloud, **overrides):
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["workspace"].update({"project_id": HUB, "projects": [SPOKE], "scan_all_projects": False,
                             "audit_project": HUB,
                             "billing": {"project_id": HUB, "dataset": "billing_export",
                                         "table": "gcp_billing_export_resource_v1_0X0X0X", "account_id": ""}})
    cfg["storage"]["backend"] = "memory"
    cfg["quotas"]["contact_email"] = "ops@example.com"
    cfg = config.deep_merge(cfg, overrides)
    cfg = config.finalize(cfg)
    app = preflight.PreflightApp(cfg, cloud)
    return Ctx(app, cfg, None)


def find(report, cid, project=None):
    """All checks with id `cid` (optionally of one project; cross-project checks match by their project field)."""
    out = []
    for proj in report["projects"]:
        if project in (None, proj["project_id"]):
            out += [c for c in proj["checks"] if c["id"] == cid]
    out += [c for c in report["cross_project"] if c["id"] == cid and project in (None, c.get("project"))]
    return out


def one(report, cid, project=None):
    items = find(report, cid, project)
    if not items:
        raise AssertionError("check %s (%s) missing" % (cid, project))
    return items[0]


def non_pass(report):
    return [(p, c["id"], c["status"], c["detail"][:160]) for p, c in readiness._all_checks(report)
            if c["status"] in ("warn", "fail", "skip")]


class ReadinessEngineTests(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("K_SERVICE", None)
        os.environ.pop("CLOUD_RUN_JOB", None)

    def tearDown(self):
        self._env.stop()

    def run_report(self, cloud, **overrides):
        return readiness.run_readiness(make_ctx(cloud, **overrides))

    def test_all_green_environment_is_ready(self):
        report = self.run_report(FakeCloud())
        self.assertEqual(non_pass(report), [])
        self.assertEqual(report["overall"], "ready")
        self.assertEqual(report["object"], "readiness_report")
        self.assertEqual([p["role"] for p in report["projects"]], ["hub", "managed"])
        self.assertEqual(report["projects"][1]["project_number"], NUMBERS[SPOKE])
        self.assertEqual(report["projects"][0]["organization_id"], ORG)
        self.assertEqual(report["projects"][0]["parent"], {"type": "folder", "id": "10"})
        cap = report["capabilities"][SPOKE]
        self.assertTrue(cap["gemini_enterprise"]["available"])
        self.assertTrue(cap["antigravity"]["available"])
        self.assertTrue(cap["vertex_ai"]["available"])
        self.assertTrue(cap["org_policy_writable"])
        self.assertTrue(cap["deny_writable"])
        self.assertTrue(cap["quotas_api"])
        self.assertIn("Nothing to fix", report["remediation_script"])
        for _, c in readiness._all_checks(report):
            self.assertIn(c["status"], ("pass", "warn", "fail", "skip", "info"))
            for key in ("id", "category", "title_ko", "title_en", "detail", "impact", "remediation", "auto_fixable"):
                self.assertIn(key, c)
        self.assertNotIn("@example.com", json.dumps(report["remediation_script"]))

    def test_api_disabled_fails_with_enable_command(self):
        cloud = FakeCloud()
        cloud.projects[SPOKE]["services"].discard("logging.googleapis.com")
        cloud.projects[SPOKE]["services"].discard("businessaicode.googleapis.com")
        cloud.projects[HUB]["services"].discard("cloudscheduler.googleapis.com")
        report = self.run_report(cloud)
        c = one(report, "api.logging.enabled", SPOKE)
        self.assertEqual(c["status"], "fail")
        self.assertTrue(c["auto_fixable"])
        self.assertIn("gcloud services enable logging.googleapis.com --project=spoke-proj", c["remediation"]["commands"])
        self.assertIn("usage", c["impact"])
        self.assertEqual(one(report, "api.businessaicode.enabled", SPOKE)["status"], "info")
        self.assertEqual(one(report, "api.cloudscheduler.enabled", HUB)["status"], "warn")
        self.assertEqual(report["overall"], "blocked")
        self.assertEqual(report["summary"]["blocking_unfixable"], 0)
        self.assertIn("gcloud services enable logging.googleapis.com --project=spoke-proj", report["remediation_script"])
        self.assertEqual(preflight.exit_code(report, strict=True, install_mode=True), 1)
        self.assertEqual(preflight.exit_code(report, strict=True, install_mode=False), 2)

    def test_resource_locations_denying_hub_region_blocks(self):
        cloud = FakeCloud()
        cloud.org_policies[(HUB, "gcp.resourceLocations")] = {
            "spec": {"rules": [{"values": {"allowedValues": ["in:us-locations", "in:eu-locations"]}}]}}
        report = self.run_report(cloud)
        c = one(report, "orgpolicy.gcp.resourceLocations", HUB)
        self.assertEqual(c["status"], "fail")
        self.assertIn("asia-northeast3", c["detail"])
        self.assertIn("us-central1", c["detail"])
        self.assertEqual(report["overall"], "blocked")
        self.assertGreaterEqual(report["summary"]["blocking_unfixable"], 1)
        self.assertEqual(preflight.exit_code(report, strict=False, install_mode=True), 2)

    def test_drs_adapts_deploy_flags(self):
        cloud = FakeCloud()
        cloud.org_policies[(HUB, "iam.allowedPolicyMemberDomains")] = {
            "spec": {"rules": [{"values": {"allowedValues": ["C0abc123"]}}]}}
        report = self.run_report(cloud)
        c = one(report, "orgpolicy.iam.allowedPolicyMemberDomains", HUB)
        self.assertEqual(c["status"], "warn")
        self.assertTrue(report["adaptations"]["no_invoker_iam_check"])
        self.assertFalse(report["adaptations"]["allow_unauthenticated"])
        env = dict(line.split("=", 1) for line in preflight.env_lines(report))
        self.assertEqual(env["PREFLIGHT_NO_INVOKER_IAM_CHECK"], "1")
        self.assertEqual(env["PREFLIGHT_ALLOW_UNAUTHENTICATED"], "0")
        self.assertEqual(report["overall"], "ready_with_warnings")

    def test_drs_with_required_invoker_iam_and_vpc_egress(self):
        cloud = FakeCloud()
        cloud.org_policies[(HUB, "iam.managed.allowedPolicyMembers")] = {"spec": {"rules": [{"enforce": True}]}}
        cloud.org_policies[(HUB, "run.managed.requireInvokerIam")] = {"spec": {"rules": [{"enforce": True}]}}
        cloud.org_policies[(HUB, "run.allowedVPCEgress")] = {
            "spec": {"rules": [{"values": {"allowedValues": ["all-traffic"]}}]}}
        report = self.run_report(cloud)
        self.assertTrue(report["adaptations"]["invoker_iam_required"])
        self.assertFalse(report["adaptations"]["no_invoker_iam_check"])
        self.assertEqual(one(report, "orgpolicy.run.allowedVPCEgress", HUB)["status"], "fail")
        self.assertEqual(one(report, "network.vpc.subnet", HUB)["status"], "fail")
        report2 = self.run_report(cloud, hub={"vpc_network": "corp", "vpc_subnet": "corp-sub"})
        self.assertEqual(one(report2, "orgpolicy.run.allowedVPCEgress", HUB)["status"], "warn")
        self.assertEqual(one(report2, "network.vpc.subnet", HUB)["status"], "pass")
        self.assertEqual(one(report2, "network.nat", HUB)["status"], "pass")

    def test_vpcsc_different_perimeters_cross_project_fail(self):
        cloud = FakeCloud()
        cloud.perimeters = [
            {"name": "accessPolicies/77/servicePerimeters/hub_perim", "title": "hub-perim",
             "status": {"resources": ["projects/%s" % NUMBERS[HUB]],
                        "restrictedServices": ["logging.googleapis.com", "bigquery.googleapis.com"]}},
            {"name": "accessPolicies/77/servicePerimeters/spoke_perim", "title": "spoke-perim",
             "status": {"resources": ["projects/%s" % NUMBERS[SPOKE]], "restrictedServices": ["logging.googleapis.com"]}},
        ]
        report = self.run_report(cloud)
        c = one(report, "vpcsc.cross_project", SPOKE)
        self.assertEqual(c["status"], "fail")
        self.assertIn("logging.googleapis.com", c["data"]["restricted_services"])
        script = "\n".join(c["remediation"]["commands"])
        self.assertIn("ingressFrom", script)
        self.assertIn("serviceAccount:%s" % RUNTIME, script)
        self.assertIn("projects/%s" % NUMBERS[SPOKE], script)
        self.assertIn("egressTo", script)
        self.assertEqual(one(report, "vpcsc.perimeter", HUB)["status"], "warn")
        self.assertEqual(report["overall"], "blocked")
        # A bridge between the two perimeters resolves it.
        cloud.perimeters.append({"name": "accessPolicies/77/servicePerimeters/bridge", "perimeterType":
                                 "PERIMETER_TYPE_BRIDGE", "status": {"resources": [
                                     "projects/%s" % NUMBERS[HUB], "projects/%s" % NUMBERS[SPOKE]]}})
        self.assertEqual(one(self.run_report(cloud), "vpcsc.cross_project", SPOKE)["status"], "pass")

    def test_existing_dataset_with_ai_tables_recommends_adoption(self):
        cloud = FakeCloud()
        hub = cloud.projects[HUB]
        hub["datasets"]["old_ai_logs"] = hub["datasets"].pop("ge_analytics")
        report = self.run_report(cloud)
        self.assertEqual(one(report, "bq.dataset.exists", HUB)["status"], "warn")
        c = one(report, "bq.ai_tables.discovered", HUB)
        self.assertEqual(c["status"], "warn")
        self.assertEqual(c["data"]["adopt"]["install_flag"], "--reuse-dataset hub-proj:old_ai_logs")
        self.assertEqual(c["data"]["adopt"]["config"]["workspace.audit_dataset"], "old_ai_logs")
        env = dict(line.split("=", 1) for line in preflight.env_lines(report))
        self.assertEqual(env["PREFLIGHT_ADOPT_DATASET"], "hub-proj:old_ai_logs")
        self.assertEqual(one(report, "bq.freshness.antigravity", HUB)["status"], "pass")

    def test_stale_tables_warn(self):
        cloud = FakeCloud()
        old = str(int((time.time() - 30 * 86400) * 1000))
        cloud.projects[HUB]["datasets"]["ge_analytics"]["tables"]["businessaicode_googleapis_com_inference_response"] = old
        report = self.run_report(cloud)
        self.assertEqual(one(report, "bq.freshness.antigravity", HUB)["status"], "warn")

    def test_existing_sink_routing_elsewhere_is_adopted(self):
        cloud = FakeCloud()
        sinks = cloud.projects[SPOKE]["sinks"]
        sinks[2] = {"name": "corp-ai-export", "filter": AI_FILTER, "writerIdentity": "serviceAccount:w@x.com",
                    "destination": "bigquery.googleapis.com/projects/sec-proj/datasets/ai_audit"}
        report = self.run_report(cloud)
        c = one(report, "logging.sink.bigquery", SPOKE)
        self.assertEqual(c["status"], "info")
        self.assertEqual(c["data"]["adopt"][0]["sink"], "corp-ai-export")
        self.assertEqual(c["data"]["adopt"][0]["config"]["workspace.audit_project"], "sec-proj")
        env = dict(line.split("=", 1) for line in preflight.env_lines(report))
        self.assertIn("spoke-proj:corp-ai-export", env["PREFLIGHT_ADOPT_SINKS"])

    def test_missing_sink_and_org_sink_coverage(self):
        cloud = FakeCloud()
        cloud.projects[SPOKE]["sinks"] = cloud.projects[SPOKE]["sinks"][:2]
        report = self.run_report(cloud)
        c = one(report, "logging.sink.bigquery", SPOKE)
        self.assertEqual(c["status"], "warn")
        self.assertTrue(any("gcloud logging sinks create gcp-ai-usage-caps-api-ai-logs" in x
                            for x in c["remediation"]["commands"]))
        cloud.org_sinks = [{"name": "org-ai", "includeChildren": True, "filter": AI_FILTER,
                            "writerIdentity": "serviceAccount:o@x.com",
                            "destination": "bigquery.googleapis.com/projects/%s/datasets/ge_analytics" % HUB}]
        report = self.run_report(cloud)
        self.assertEqual(one(report, "logging.sink.bigquery", SPOKE)["status"], "pass")

    def test_no_gemini_enterprise_engines_capability_false(self):
        cloud = FakeCloud()
        cloud.projects[SPOKE]["engines"] = {}
        report = self.run_report(cloud)
        c = one(report, "surface.gemini_enterprise", SPOKE)
        self.assertEqual(c["status"], "info")
        cap = report["capabilities"][SPOKE]["gemini_enterprise"]
        self.assertIs(cap["available"], False)
        self.assertEqual(cap["reason"], "no_apps")
        self.assertNotEqual(report["overall"], "blocked")

    def test_antigravity_devtools_present_is_available(self):
        cloud = FakeCloud()
        cloud.projects[SPOKE]["logs"] = False
        cloud.projects[SPOKE]["devtools"] = {"us": {"devToolsEnabled": True}}
        report = self.run_report(cloud)
        cap = report["capabilities"][SPOKE]["antigravity"]
        self.assertTrue(cap["available"])
        self.assertEqual(cap["locations"], ["us"])
        self.assertEqual(one(report, "surface.antigravity.metadata_logging", SPOKE)["status"], "warn")
        cloud.projects[SPOKE]["devtools"] = {}
        cloud.projects[SPOKE]["services"].discard("businessaicode.googleapis.com")
        report = self.run_report(cloud)
        self.assertIs(report["capabilities"][SPOKE]["antigravity"]["available"], False)

    def test_missing_iam_permissions_fail_with_role_mapping(self):
        cloud = FakeCloud()
        cloud.projects[SPOKE]["iam"]["bindings"] = [{"role": "roles/logging.viewer",
                                                     "members": ["serviceAccount:%s" % RUNTIME]}]
        report = self.run_report(cloud, hub={"runtime_service_account": RUNTIME})
        c = one(report, "iam.runtime.permissions", SPOKE)
        self.assertEqual(c["status"], "fail")
        self.assertIn("roles/logging.privateLogViewer", c["data"]["missing_roles"])
        self.assertIn("roles/cloudquotas.admin", c["data"]["missing_roles"])
        self.assertNotIn("roles/cloudquotas.viewer", c["data"]["missing_roles"])
        self.assertIn("gcloud projects add-iam-policy-binding spoke-proj --member=serviceAccount:%s "
                      "--role=roles/logging.privateLogViewer --condition=None" % RUNTIME, c["remediation"]["commands"])

    def test_caller_mode_test_iam_permissions_with_unknown_permission(self):
        cloud = FakeCloud()
        cloud.unknown_permissions = {"discoveryengine.engines.update"}
        cloud.projects[HUB]["granted"] = {"resourcemanager.projects.get", "logging.logEntries.list"}
        with mock.patch.dict(os.environ, {"K_SERVICE": "caps"}):
            report = self.run_report(cloud)
        c = one(report, "iam.runtime.permissions", HUB)
        self.assertEqual(c["status"], "fail")
        self.assertEqual(c["data"]["mode"], "testIamPermissions")
        self.assertNotIn("discoveryengine.engines.update", c["data"]["missing"])
        self.assertIn("bigquery.jobs.create", c["data"]["missing"])
        self.assertEqual(find(report, "iam.deployer.permissions"), [])
        self.assertIs(report["capabilities"][HUB]["org_policy_writable"], False)

    def test_permission_denied_on_org_policy_is_skip(self):
        cloud = FakeCloud()
        cloud.orgpolicy_error = GcpError("denied", status=403, message="caller lacks orgpolicy.policy.get")
        report = self.run_report(cloud)
        for pid in (HUB, SPOKE):
            c = one(report, "orgpolicy.read", pid)
            self.assertEqual(c["status"], "skip")
            self.assertIn("permission denied", c["detail"])
        self.assertEqual(one(report, "orgpolicy.customConstraints")["status"], "skip")
        self.assertNotEqual(report["overall"], "blocked")

    def test_data_access_logs_missing_warns(self):
        cloud = FakeCloud()
        cloud.projects[SPOKE]["iam"]["auditConfigs"] = []
        report = self.run_report(cloud)
        c = one(report, "logging.audit_config.aiplatform", SPOKE)
        self.assertEqual(c["status"], "warn")
        self.assertIn("--enable-data-access-logs", c["remediation"]["commands"][0])
        cloud.org_iam["auditConfigs"] = [{"service": "allServices", "auditLogConfigs": [
            {"logType": "DATA_READ"}, {"logType": "DATA_WRITE"}]}]
        report = self.run_report(cloud)
        c = one(report, "logging.audit_config.aiplatform", SPOKE)
        self.assertEqual(c["status"], "pass")
        self.assertEqual(c["data"]["source"], "organization/%s" % ORG)

    def test_unreachable_everything_never_crashes(self):
        class Broken(FakeCloud):
            def request(self, method, url, **kw):
                raise GcpError("unavailable", status=503, message="backend down")
        report = readiness.run_readiness(make_ctx(Broken()))
        self.assertIn(report["overall"], ("ready", "ready_with_warnings", "blocked"))
        self.assertGreater(report["summary"]["skip"], 5)

    def test_billing_disabled_and_masked(self):
        cloud = FakeCloud()
        cloud.projects[HUB]["billing"] = {"billingEnabled": False}
        self.assertEqual(one(self.run_report(cloud), "project.billing", HUB)["status"], "fail")

        class NoBillingVisibility(FakeCloud):
            def request(self, method, url, **kw):
                if "billingInfo" in url:
                    raise GcpError("denied", status=403, message="The caller does not have permission")
                return super().request(method, url, **kw)
        report = self.run_report(NoBillingVisibility())
        self.assertEqual(one(report, "project.billing", HUB)["status"], "warn")
        self.assertEqual(report["summary"]["skip"], 0)

    def test_category_filter_and_validation(self):
        ctx = make_ctx(FakeCloud())
        report = readiness.run_readiness(ctx, categories=["apis"])
        cats = {c["category"] for _, c in readiness._all_checks(report)}
        self.assertEqual(cats, {"apis"})
        with self.assertRaises(ApiError):
            readiness.run_readiness(ctx, categories=["nope"])
        with self.assertRaises(ApiError):
            readiness.run_readiness(ctx, projects=["other-proj"])


class ReadinessHandlerTests(unittest.TestCase):
    def test_get_caches_and_refresh_and_project_filter(self):
        cloud = FakeCloud()
        ctx = make_ctx(cloud)
        first = readiness.get_readiness(ctx)
        self.assertFalse(first["cached"])
        n = len(cloud.calls)
        second = readiness.get_readiness(ctx)
        self.assertTrue(second["cached"])
        self.assertEqual(len(cloud.calls), n)
        ctx.params = {"refresh": "true", "project": SPOKE}
        third = readiness.get_readiness(ctx)
        self.assertFalse(third["cached"])
        self.assertGreater(len(cloud.calls), n)
        self.assertEqual([p["project_id"] for p in third["projects"]], [SPOKE])
        self.assertEqual(list(third["capabilities"]), [SPOKE])
        ctx.params = {"project": "unknown-proj"}
        with self.assertRaises(ApiError):
            readiness.get_readiness(ctx)

    def test_run_handler_validates_body(self):
        ctx = make_ctx(FakeCloud())
        ctx.body = {"projects": "spoke-proj"}
        with self.assertRaises(ApiError):
            readiness.run_readiness_handler(ctx)
        ctx.body = {"projects": [SPOKE], "categories": ["surfaces", "apis"]}
        out = readiness.run_readiness_handler(ctx)
        self.assertEqual([p["project_id"] for p in out["projects"]], [HUB, SPOKE])
        doc, _ = ctx.store.load("readiness")
        self.assertEqual(doc, {})  # partial runs do not replace the cache
        ctx.body = {}
        readiness.run_readiness_handler(ctx)
        doc, _ = ctx.store.load("readiness")
        self.assertEqual(doc["report"]["object"], "readiness_report")

    def test_capabilities_from_cache_and_probe(self):
        cloud = FakeCloud()
        cloud.projects[SPOKE]["engines"] = {}
        ctx = make_ctx(cloud)
        probe = readiness.capabilities(ctx, SPOKE)
        self.assertEqual(probe["source"], "probe")
        self.assertIs(probe["gemini_enterprise"]["available"], False)
        for key in ("vertex_ai", "gemini_enterprise", "antigravity", "org_policy_writable", "quotas_api",
                    "deny_writable"):
            self.assertIn(key, probe)
        readiness.get_readiness(ctx)
        cached = readiness.capabilities(ctx, SPOKE)
        self.assertEqual(cached["source"], "cache")
        self.assertEqual(cached["object"], "capabilities")
        with self.assertRaises(ApiError):
            readiness.capabilities(ctx, "BAD PROJECT")


class OrgPolicyLogicTests(unittest.TestCase):
    def test_location_matching(self):
        self.assertTrue(loc_match("asia-northeast3", "in:asia-locations"))
        self.assertTrue(loc_match("asia-northeast3", "in:asia-northeast3-locations"))
        self.assertFalse(loc_match("asia-northeast3", "in:us-locations"))
        self.assertTrue(loc_match("us", "in:us-locations"))
        pol = normalize_policy({"spec": {"rules": [{"values": {"deniedValues": ["in:asia-locations"]}}]}})
        self.assertIs(loc_verdict("asia-northeast3", pol), False)
        self.assertIs(loc_verdict("us-central1", pol), True)
        self.assertIs(loc_verdict("x", normalize_policy({"spec": {"rules": [{"denyAll": True}]}})), False)
        self.assertIs(loc_verdict("asia-northeast3", normalize_policy({})), True)


class TokenClientTests(unittest.TestCase):
    def test_request_sends_quota_header_and_classifies_errors(self):
        client = StaticTokenGcp("tok-123", environ={})
        seen = {}

        class Resp(io.BytesIO):
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(req, timeout=None):
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            seen["url"] = req.full_url
            if "missing" in req.full_url:
                import urllib.error
                raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(
                    b'{"error": {"message": "nope", "status": "NOT_FOUND"}}'))
            return Resp(b'{"ok": true}')
        client._opener.open = fake_open  # pylint: disable=protected-access
        payload, _ = client.request("GET", "https://x.googleapis.com/v1/a", quota_project="hub-proj",
                                    params={"names": ["a", "b"]})
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(seen["headers"]["x-goog-user-project"], "hub-proj")
        self.assertEqual(seen["headers"]["authorization"], "Bearer tok-123")
        self.assertIn("names=a&names=b", seen["url"])
        with self.assertRaises(GcpError) as cm:
            client.request("GET", "https://x.googleapis.com/v1/missing", quota_project="")
        self.assertEqual(cm.exception.kind, "not_found")
        client.close()

    def test_no_token_no_gcloud(self):
        client = StaticTokenGcp(environ={}, gcloud_bin="")
        client._gcloud = ""  # pylint: disable=protected-access
        self.assertFalse(client.credentials_ok())
        client.close()


class PreflightCliTests(unittest.TestCase):
    def test_cli_text_json_env_and_exit_codes(self):
        cloud = FakeCloud()
        cloud.projects[SPOKE]["services"].discard("logging.googleapis.com")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(preflight, "make_gcp",
                                                                     return_value=(cloud, "fake")), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("K_SERVICE", None)
            env_file = os.path.join(tmp, "pf.env")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = preflight.main(["--project", HUB, "--managed-projects", SPOKE, "--central-dataset",
                                     "--lang", "en", "--env-file", env_file])
            self.assertEqual(rc, 2)
            text = buf.getvalue()
            self.assertIn("[FAIL]", text)
            self.assertIn("api.logging.enabled", text)
            self.assertIn("BLOCKED", text)
            self.assertIn("gcloud services enable logging.googleapis.com --project=spoke-proj", text)
            with open(env_file, encoding="utf-8") as fh:
                env = dict(line.strip().split("=", 1) for line in fh if line.strip())
            self.assertEqual(env["PREFLIGHT_OVERALL"], "blocked")
            self.assertEqual(env["PREFLIGHT_MANAGED_PROJECTS"], SPOKE)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = preflight.main(["--project", HUB, "--managed-projects", SPOKE, "--central-dataset",
                                     "--install-mode", "--no-strict", "--format", "json"])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(data["hub"]["dataset_mode"], "central")
            buf = io.StringIO()
            with redirect_stdout(buf):
                preflight.main(["--project", HUB, "--managed-projects", SPOKE, "--format", "markdown"])
            self.assertIn("| FAIL | `api.logging.enabled`", buf.getvalue())

    def test_cli_rejects_bad_arguments(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(preflight.main(["--project", "BAD"]), 3)
            self.assertEqual(preflight.main(["--project", HUB, "--categories", "nope"]), 3)


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------

FAKE_GCLOUD = r"""#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as fh:
    fh.write("gcloud " + " ".join(args) + "\n")
joined = " ".join(args)
if args[:3] == ["config", "get-value", "project"]:
    print("hub-proj"); sys.exit(0)
if args[:2] == ["projects", "describe"]:
    print("111111111111"); sys.exit(0)
if args[:3] == ["auth", "list", "--filter=status:ACTIVE"] or args[:2] == ["auth", "list"]:
    print("deployer@example.com"); sys.exit(0)
if "describe" in args or args[:2] == ["projects", "get-iam-policy"]:
    sys.exit(1)
sys.exit(0)
"""
FAKE_BQ = r"""#!/usr/bin/env python3
import os, sys
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as fh:
    fh.write("bq " + " ".join(sys.argv[1:]) + "\n")
sys.exit(1 if "show" in sys.argv else 0)
"""


def _stub_env(tmp):
    bin_dir = os.path.join(tmp, "bin")
    os.makedirs(bin_dir)
    for name, body in (("gcloud", FAKE_GCLOUD), ("bq", FAKE_BQ)):
        path = os.path.join(bin_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(path, 0o755)
    log = os.path.join(tmp, "calls.log")
    open(log, "w", encoding="utf-8").close()
    env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ.get("PATH", ""), FAKE_LOG=log,
               CAPS_API_SKIP_PREFLIGHT="")
    return env, log


class ScriptTests(unittest.TestCase):
    def test_bash_syntax_of_every_script(self):
        scripts = sorted(f for f in os.listdir(os.path.join(ROOT, "scripts")) if f.endswith(".sh"))
        self.assertIn("preflight.sh", scripts)
        for name in scripts:
            res = subprocess.run(["bash", "-n", os.path.join(ROOT, "scripts", name)], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, "%s: %s" % (name, res.stderr))

    def _run(self, script, args, env):
        return subprocess.run(["bash", os.path.join(ROOT, "scripts", script)] + args, capture_output=True, text=True,
                              env=env, timeout=120, cwd=ROOT)

    def test_prepare_dry_run_emits_multi_project_sinks_and_bindings(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, _ = _stub_env(tmp)
            res = self._run("prepare.sh", ["--project", HUB, "--managed-projects", "spoke-a,spoke-b",
                                           "--enable-data-access-logs", "--dry-run"], env)
            self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
            out = res.stdout
            for pid in ("spoke-a", "spoke-b", HUB):
                self.assertRegex(out, r"\[dry-run\] gcloud logging sinks create gcp-ai-usage-caps-api-ai-logs "
                                      r"bigquery\.googleapis\.com/projects/hub-proj/datasets/ge_analytics .*--project=%s"
                                 % pid)
            self.assertIn("bq --project_id=hub-proj mk --dataset", out)
            self.assertNotIn("bq --project_id=spoke-a mk", out)
            self.assertIn("Data Access audit logs", out)
            self.assertIn("roles/bigquery.dataEditor", out)

    def test_prepare_dry_run_legacy_per_project_and_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, _ = _stub_env(tmp)
            res = self._run("prepare.sh", ["--project", HUB, "--link-projects", "spoke-a", "--per-project-datasets",
                                           "--dry-run"], env)
            self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
            self.assertIn("bq --project_id=spoke-a mk --dataset", res.stdout)
            self.assertIn("bigquery.googleapis.com/projects/spoke-a/datasets/ge_analytics", res.stdout)
            res = self._run("prepare.sh", ["--project", HUB, "--managed-projects", "spoke-a", "--reuse-dataset",
                                           "sec-proj:ai_audit", "--dry-run"], env)
            self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
            self.assertIn("bigquery.googleapis.com/projects/sec-proj/datasets/ai_audit", res.stdout)
            self.assertNotIn(" mk --dataset", res.stdout)

    def test_deploy_dry_run_grants_every_managed_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, _ = _stub_env(tmp)
            res = self._run("deploy.sh", ["--project", HUB, "--managed-projects", "spoke-a,spoke-b",
                                          "--org-id", ORG, "--grant-org-roles", "--dry-run"], env)
            self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
            out = res.stdout
            for pid in ("spoke-a", "spoke-b"):
                self.assertIn("gcloud projects add-iam-policy-binding %s --member=serviceAccount:%s "
                              "--role=roles/logging.privateLogViewer" % (pid, RUNTIME), out)
                self.assertIn("gcloud projects add-iam-policy-binding %s --member=serviceAccount:%s "
                              "--role=roles/cloudquotas.admin" % (pid, RUNTIME), out)
                self.assertIn("gcloud logging sinks create gcp-ai-usage-caps-api-usage-events "
                              "pubsub.googleapis.com/projects/hub-proj/topics/gcp-ai-usage-caps-api-usage-events", out)
                self.assertRegex(out, r"usage-events .*--project=%s" % pid)
            self.assertIn("gcloud organizations add-iam-policy-binding %s --member=serviceAccount:%s "
                          "--role=roles/orgpolicy.policyAdmin" % (ORG, RUNTIME), out)
            self.assertIn("--role=roles/iam.denyAdmin", out)
            self.assertIn("SCAN_PROJECTS=spoke-a,spoke-b", out)
            self.assertIn("AUDIT_PROJECT_ID=hub-proj", out)
            self.assertIn("MODEL_VIOLATION_ACTION=block_service", out)
            self.assertIn("--allow-unauthenticated", out)

    def test_deploy_dry_run_adapts_to_preflight_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, _ = _stub_env(tmp)
            pf = os.path.join(tmp, "pf.env")
            with open(pf, "w", encoding="utf-8") as fh:
                fh.write("PREFLIGHT_OVERALL=ready_with_warnings\nPREFLIGHT_NO_INVOKER_IAM_CHECK=1\n"
                         "PREFLIGHT_ALLOW_UNAUTHENTICATED=0\nPREFLIGHT_BINAUTHZ=default\nPREFLIGHT_DOCKER_BUILD=0\n"
                         "PREFLIGHT_INGRESS=\nPREFLIGHT_VPC_EGRESS_REQUIRED=0\nPREFLIGHT_BLOCKING_UNFIXABLE=0\n")
            env["CAPS_API_PREFLIGHT_ENV"] = pf
            res = self._run("deploy.sh", ["--project", HUB, "--managed-projects", "spoke-a", "--dry-run",
                                          "--vpc-network", "corp", "--vpc-subnet", "corp-sub"], env)
            self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
            self.assertIn("--no-invoker-iam-check", res.stdout)
            self.assertNotIn("--allow-unauthenticated", res.stdout)
            self.assertIn("--binary-authorization=default", res.stdout)
            self.assertIn("--network=corp --subnet=corp-sub --vpc-egress=all-traffic", res.stdout)

    def test_install_and_teardown_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, _ = _stub_env(tmp)
            res = self._run("install.sh", ["--project", HUB, "--managed-projects", "spoke-a", "--dry-run"], env)
            self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
            self.assertIn("--project=spoke-a", res.stdout)
            res = self._run("teardown.sh", ["--project", HUB, "--managed-projects", "spoke-a", "--dry-run"], env)
            self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
            self.assertIn("gcloud logging sinks delete gcp-ai-usage-caps-api-usage-events --project=spoke-a", res.stdout)
            self.assertIn("gcloud logging sinks delete gcp-ai-usage-caps-api-ai-logs --project=spoke-a", res.stdout)

    def test_preflight_wrapper_help(self):
        res = subprocess.run(["bash", os.path.join(ROOT, "scripts", "preflight.sh"), "--help"], capture_output=True,
                             text=True, timeout=60, cwd=ROOT)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("--managed-projects", res.stdout)


if __name__ == "__main__":
    unittest.main()
