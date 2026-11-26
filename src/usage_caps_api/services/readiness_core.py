"""Shared primitives of the environment readiness (preflight) engine.

This module holds everything the category modules (`readiness_project`, `readiness_orgpolicy`,
`readiness_network`, `readiness_data`, `readiness_iam`, `readiness_surfaces`) share:

- `check()` / `skipped()` builders producing the stable Check dictionary of the readiness report;
- `Run`, the per-run state: configuration facts about the hub, the ordered project list, memoized and
  de-duplicated REST calls (so several categories can read the same resource for free), per-project facts,
  observed VPC Service Controls denials and the IAM v2 read budget (5 reads / project / minute);
- error classification helpers (`is_service_disabled`, `is_vpcsc`, `reason_of`);
- organization-policy helpers (`normalize_policy`, `loc_match`, `loc_verdict`) ported from the reference
  `preflight_org_policies` shell implementation;
- gcloud remediation command builders.

Nothing here performs a write against Google Cloud: the readiness engine is strictly read-only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.parse

from .. import config
from ..gcp import GcpError, log_safe

LOG = logging.getLogger("usage_caps_api.readiness")

STATUSES = ("pass", "warn", "fail", "skip", "info")
CATEGORIES = ("project", "apis", "orgpolicy", "vpcsc", "network", "bigquery", "logging", "surfaces", "iam",
              "enforcement", "quotas", "config")
REQUEST_TIMEOUT = 15
MAX_DETAIL = 2000

CRM_V1 = "https://cloudresourcemanager.googleapis.com/v1"
CRM_V3 = "https://cloudresourcemanager.googleapis.com/v3"
SERVICEUSAGE_V1 = "https://serviceusage.googleapis.com/v1"
BILLING_V1 = "https://cloudbilling.googleapis.com/v1"
ORGPOLICY_V2 = "https://orgpolicy.googleapis.com/v2"
ACM_V1 = "https://accesscontextmanager.googleapis.com/v1"
COMPUTE_V1 = "https://compute.googleapis.com/compute/v1"
DNS_V1 = "https://dns.googleapis.com/dns/v1"
BQ_V2 = "https://bigquery.googleapis.com/bigquery/v2"
LOGGING_V2 = "https://logging.googleapis.com/v2"
PUBSUB_V1 = "https://pubsub.googleapis.com/v1"
SCHEDULER_V1 = "https://cloudscheduler.googleapis.com/v1"
RUN_V2 = "https://run.googleapis.com/v2"
IAM_V2 = "https://iam.googleapis.com/v2"
QUOTAS_V1 = "https://cloudquotas.googleapis.com/v1"
AIPLATFORM = "https://aiplatform.googleapis.com"

DEFAULT_API_SA_ID = "ai-usage-caps-api"
DEFAULT_ENFORCER_SA_ID = "ai-usage-caps-enforcer"

# IAM v2 (deny policies) allows only 5 read requests per (quota) project per minute; the readiness run spends at
# most this many so that the API and the Enforcer Job keep their budget.
IAM_V2_READ_BUDGET = 4

DOCS = {
    "orgpolicy": "https://cloud.google.com/resource-manager/docs/organization-policy/org-policy-constraints",
    "vpcsc": "https://cloud.google.com/vpc-service-controls/docs/ingress-egress-rules",
    "drs": "https://cloud.google.com/resource-manager/docs/organization-policy/restricting-domains",
    "run_ingress": "https://cloud.google.com/run/docs/securing/ingress",
    "run_egress": "https://cloud.google.com/run/docs/configuring/vpc-direct-vpc",
    "pga": "https://cloud.google.com/vpc/docs/configure-private-google-access",
    "nat": "https://cloud.google.com/nat/docs/set-up-manage-network-address-translation",
    "audit": "https://cloud.google.com/logging/docs/audit/configure-data-access",
    "sinks": "https://cloud.google.com/logging/docs/export/configure_export_v2",
    "deny": "https://cloud.google.com/iam/docs/deny-overview",
    "iam_quotas": "https://cloud.google.com/iam/quotas",
    "quotas": "https://cloud.google.com/docs/quotas/overview",
    "vertex_inference_logging": "https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/"
                                "inference-metadata-logging",
    "cmek": "https://cloud.google.com/kms/docs/cmek-org-policy",
    "binauthz": "https://cloud.google.com/binary-authorization/docs/run/requiring-binauthz-cloud-run",
    "vertex_models": "https://cloud.google.com/vertex-ai/generative-ai/docs/control-model-access",
    "gemini_enterprise": "https://cloud.google.com/gemini/enterprise/docs",
    "antigravity_logging": "https://cloud.google.com/gemini/enterprise/docs/ai-developer-tools",
}

_VPCSC_RE = re.compile(r"vpcServiceControls|VPC Service Controls|SECURITY_POLICY_VIOLATED|prohibited by organization",
                       re.I)
_DISABLED_RE = re.compile(r"SERVICE_DISABLED|has not been used in project|it is disabled|API has not been used", re.I)
_SERVICE_NAME_RE = re.compile(r"([a-z0-9-]+\.googleapis\.com)")
_PROJECT_NUMBER_RE = re.compile(r"project (?:projects/)?([0-9]{6,30})\b")


# ---------------------------------------------------------------------------
# Check builders
# ---------------------------------------------------------------------------

def check(cid: str, category: str, status: str, title_ko: str, title_en: str, detail: str = "", *,
          impact=None, commands=None, docs_url: str | None = None, auto_fixable: bool = False,
          blocking: bool | None = None, data: dict | None = None) -> dict:
    """Builds one readiness Check dictionary.

    `blocking` only matters for `fail`: a failed check blocks deployment/operation unless `blocking=False`
    (for example a failure that only disables one optional feature)."""
    if status not in STATUSES:
        raise ValueError("invalid readiness status: %s" % status)
    item = {
        "id": cid,
        "category": category,
        "status": status,
        "title_ko": title_ko,
        "title_en": title_en,
        "detail": str(detail or "")[:MAX_DETAIL],
        "impact": [str(i) for i in (impact or [])],
        "remediation": {"commands": [str(c) for c in (commands or []) if c]},
        "auto_fixable": bool(auto_fixable),
    }
    if docs_url:
        item["remediation"]["docs_url"] = docs_url
    if status == "fail":
        item["blocking"] = True if blocking is None else bool(blocking)
    if data:
        item["data"] = data
    return item


def reason_of(err) -> str:
    """Human-readable, email-free reason for a failed read."""
    if err is None:
        return "unknown"
    if isinstance(err, str):
        return err
    kind = getattr(err, "kind", None) or type(err).__name__
    if is_vpcsc(err):
        return "blocked by VPC Service Controls (%s)" % kind
    svc = disabled_service(err)
    if svc:
        return "%s is disabled on the project used for the request (enable it first)" % svc
    if kind == "denied":
        return "permission denied"
    if kind == "credentials":
        return "no Google credentials available"
    msg = log_safe(getattr(err, "message", "") or "", 160)
    return "%s%s" % (kind, (": " + msg) if msg else "")


def skipped(cid: str, category: str, title_ko: str, title_en: str, err, *, what: str = "", impact=None,
            commands=None, docs_url: str | None = None, data: dict | None = None) -> dict:
    """A `skip` check: the condition could not be evaluated (never a crash)."""
    detail = "Could not read %s: %s." % (what or "the required resource", reason_of(err))
    cmds = list(commands or [])
    svc = disabled_service(err) if not isinstance(err, str) else ""
    if svc and svc != "a required API":
        m = _PROJECT_NUMBER_RE.search(getattr(err, "message", "") or "")
        cmd = cmd_enable(m.group(1) if m else "QUOTA_PROJECT", [svc])
        if cmd not in cmds:
            cmds.append(cmd)
    return check(cid, category, "skip", title_ko, title_en, detail, impact=impact, commands=cmds,
                 docs_url=docs_url, data=data)


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

def is_vpcsc(err) -> bool:
    text = "%s %s" % (getattr(err, "message", "") or "", getattr(err, "reason", "") or "")
    return bool(_VPCSC_RE.search(text))


def disabled_service(err) -> str:
    """Name of the disabled API when the error says SERVICE_DISABLED, else ''."""
    text = "%s %s" % (getattr(err, "message", "") or "", getattr(err, "reason", "") or "")
    if not _DISABLED_RE.search(text):
        return ""
    m = _SERVICE_NAME_RE.search(text)
    return m.group(1) if m else "a required API"


def is_service_disabled(err) -> bool:
    return bool(disabled_service(err))


def is_missing(err) -> bool:
    return getattr(err, "kind", "") == "not_found"


# ---------------------------------------------------------------------------
# Organization policy helpers (ported from scripts/lib/common.sh preflight_org_policies)
# ---------------------------------------------------------------------------

def normalize_policy(payload) -> dict:
    """Normalizes an Org Policy v2 (effective) policy.

    Returns {'set', 'allowed', 'denied', 'all' ('ALLOW'|'DENY'|''), 'enforced', 'conditional',
    'inherit_from_parent', 'reset', 'raw_allowed', 'raw_denied'}."""
    out = {"set": False, "allowed": [], "denied": [], "all": "", "enforced": False, "conditional": False,
           "inherit_from_parent": False, "reset": False, "raw_allowed": [], "raw_denied": []}
    if not isinstance(payload, dict):
        return out
    spec = payload.get("spec") if isinstance(payload.get("spec"), dict) else {}
    out["inherit_from_parent"] = bool(spec.get("inheritFromParent"))
    out["reset"] = bool(spec.get("reset"))
    for rule in spec.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        out["set"] = True
        if rule.get("condition"):
            out["conditional"] = True
        values = rule.get("values") if isinstance(rule.get("values"), dict) else {}
        for v in values.get("allowedValues") or []:
            out["raw_allowed"].append(str(v))
            out["allowed"].append(str(v).lower())
        for v in values.get("deniedValues") or []:
            out["raw_denied"].append(str(v))
            out["denied"].append(str(v).lower())
        if rule.get("denyAll"):
            out["all"] = "DENY"
        elif rule.get("allowAll") and out["all"] != "DENY":
            out["all"] = "ALLOW"
        if rule.get("enforce") is True:
            out["enforced"] = True
    return out


GROUPS = {"asia": {"asia"}, "europe": {"eu", "europe"}, "us": {"us", "northamerica"},
          "northamerica": {"northamerica"}, "southamerica": {"southamerica"}, "australia": {"australia"},
          "me": {"me"}, "africa": {"africa"}}
KNOWN_GROUPS = set().union(*GROUPS.values())
MULTI_REGION_GROUP = {"us": "us", "eu": "eu"}


def loc_match(loc: str, value: str):
    """True / False, or None for a value group this check cannot resolve.

    Effective policies list expanded members (us-west3-locations, us-west3-b, us, nam3, ...), so any concrete
    location that is not ours is False."""
    loc = (loc or "").lower()
    value = (value or "").lower()
    name = value[3:] if value.startswith("in:") else value
    if name.startswith("under:"):
        return None
    cont = loc.split("-")[0]
    if name in (loc, loc + "-locations") or name.startswith(loc + "-"):
        return True
    if name.endswith("-locations"):
        group = name[:-len("-locations")]
        if group in GROUPS.get(cont, set()) or group == loc:
            return True
        if loc in MULTI_REGION_GROUP and group == MULTI_REGION_GROUP[loc]:
            return True
        if group in KNOWN_GROUPS or re.match(r"^[a-z]+-[a-z]+\d+$", group) or re.match(r"^[a-z]+\d*$", group) is None:
            return False
        return None if len(group) == 2 else False
    return False


def loc_verdict(loc: str, policy: dict):
    """True (allowed), False (denied) or None (cannot be decided) for `loc` under a normalized policy."""
    allowed, denied, allv = policy.get("allowed") or [], policy.get("denied") or [], policy.get("all") or ""
    if allv == "DENY" or any(loc_match(loc, v) is True for v in denied):
        return False
    if not allowed or allv == "ALLOW":
        return True
    hits = [loc_match(loc, v) for v in allowed]
    if any(h is True for h in hits):
        return True
    return None if any(h is None for h in hits) else False


SUGGESTED_REGIONS = ("asia-northeast3", "asia-northeast1", "asia-southeast1", "us-central1", "us-east1",
                     "europe-west1", "europe-west4")


def suggest_regions(policy: dict, limit: int = 4) -> list[str]:
    return [r for r in SUGGESTED_REGIONS if loc_verdict(r, policy) is True][:limit]


# ---------------------------------------------------------------------------
# Remediation command builders
# ---------------------------------------------------------------------------

def q(value: str) -> str:
    """Shell-quotes a value only when needed (keeps commands readable)."""
    text = str(value)
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./\-]+", text):
        return text
    return "'" + text.replace("'", "'\"'\"'") + "'"


def cmd_enable(project: str, services) -> str:
    return "gcloud services enable %s --project=%s" % (" ".join(services), q(project))


def cmd_bind_project(project: str, member: str, role: str) -> str:
    return "gcloud projects add-iam-policy-binding %s --member=%s --role=%s --condition=None" % (
        q(project), q(member), role)


def cmd_bind_org(org: str, member: str, role: str) -> str:
    return "gcloud organizations add-iam-policy-binding %s --member=%s --role=%s --condition=None" % (
        q(org or "ORGANIZATION_ID"), q(member), role)


def cmd_bind_folder(folder: str, member: str, role: str) -> str:
    return "gcloud resource-manager folders add-iam-policy-binding %s --member=%s --role=%s --condition=None" % (
        q(folder), q(member), role)


def sa_member(email: str) -> str:
    return "serviceAccount:%s" % email


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------

class _Pending:
    __slots__ = ("event", "result")

    def __init__(self):
        self.event = threading.Event()
        self.result = None


class Run:
    """Per-run state shared by every readiness category."""

    def __init__(self, ctx, projects: list[str], categories: set[str], *, environ=None):
        self.ctx = ctx
        self.cfg = ctx.config
        self.ws = ctx.ws
        self.env = os.environ if environ is None else environ
        self.hub = ctx.project
        hub = self.cfg.get("hub") or {}
        caps = self.cfg.get("caps") or {}
        self.region = str(hub.get("region") or "asia-northeast3").lower()
        self.service_name = str(hub.get("service_name") or "gcp-ai-usage-caps-api")
        runtime = str(hub.get("runtime_service_account") or "").strip().lower()
        enforcer = str(hub.get("enforcer_service_account") or "").strip().lower()
        self.runtime_sa_configured = bool(runtime)
        self.enforcer_sa_configured = bool(enforcer)
        self.runtime_sa = runtime or "%s@%s.iam.gserviceaccount.com" % (DEFAULT_API_SA_ID, self.hub)
        self.enforcer_sa = enforcer or "%s@%s.iam.gserviceaccount.com" % (DEFAULT_ENFORCER_SA_ID, self.hub)
        self.org_hint = str(hub.get("org_id") or "").strip()
        self.vpc_network = str(hub.get("vpc_network") or "").strip()
        self.vpc_subnet = str(hub.get("vpc_subnet") or "").strip()
        self.vpc_egress = str(hub.get("vpc_egress") or "").strip()
        self.topic = str(hub.get("usage_events_topic") or "").strip() or "%s-usage-events" % self.service_name
        self.dataset = ctx.audit_dataset
        self.audit_project = ctx.central_audit_project
        self.central = bool(self.audit_project)
        self.dataset_project = self.audit_project or self.hub
        self.deny_attachment = caps.get("deny_attachment") or "projects"
        self.enforcement_backend = caps.get("enforcement_backend") or "iam_deny"
        self.usage_source = caps.get("usage_source") or "auto"
        self.realtime = bool(caps.get("usage_events_subscription") or hub.get("usage_events_topic"))
        self.projects = list(projects)
        self.categories = set(categories)
        self.quota = ctx.quota_project
        self.facts: dict[str, dict] = {p: {} for p in self.projects}
        self.vpcsc_denials: list[dict] = []
        self.adaptations: dict = {}
        self.notes: list[str] = []
        self._lock = threading.Lock()
        self._memo: dict = {}
        self._iam_v2_left = IAM_V2_READ_BUDGET
        self.on_cloud_run = bool(self.env.get("K_SERVICE") or self.env.get("CLOUD_RUN_JOB"))
        # The identity the run tests: inside Cloud Run the caller IS the runtime SA (testIamPermissions);
        # elsewhere (CLI preflight) the caller is the deployer, and the runtime SA is evaluated from IAM bindings.
        self.identity_mode = "caller" if self.on_cloud_run else "bindings"

    # -- project roles --------------------------------------------------------
    def is_hub(self, pid: str) -> bool:
        return pid == self.hub

    def role(self, pid: str) -> str:
        return "hub" if self.is_hub(pid) else "managed"

    def wants(self, category: str) -> bool:
        return category in self.categories

    # -- facts ------------------------------------------------------------------
    def set_fact(self, pid: str, key: str, value):
        with self._lock:
            self.facts.setdefault(pid, {})[key] = value

    def fact(self, pid: str, key: str, default=None):
        with self._lock:
            return self.facts.get(pid, {}).get(key, default)

    def adapt(self, key: str, value):
        with self._lock:
            self.adaptations[key] = value

    def note_vpcsc(self, project: str, service: str, what: str):
        with self._lock:
            item = {"project": project, "service": service, "what": what}
            if item not in self.vpcsc_denials and len(self.vpcsc_denials) < 50:
                self.vpcsc_denials.append(item)

    def take_iam_v2_read(self) -> bool:
        with self._lock:
            if self._iam_v2_left <= 0:
                return False
            self._iam_v2_left -= 1
            return True

    # -- REST -------------------------------------------------------------------
    def call(self, method: str, url: str, *, params=None, json_body=None, timeout: int = REQUEST_TIMEOUT,
             memo: bool = True, project_hint: str = ""):
        """Memoized, in-flight de-duplicated REST call. Raises GcpError on failure (also memoized)."""
        key = None
        if memo:
            key = (method, url, json.dumps(params, sort_keys=True, default=str),
                   json.dumps(json_body, sort_keys=True, default=str))
            with self._lock:
                hit = self._memo.get(key)
                if hit is None:
                    pending = _Pending()
                    self._memo[key] = pending
                    owner = True
                else:
                    pending, owner = hit, False
            if not owner:
                pending.event.wait(timeout + 30)
                ok, value = pending.result or (False, GcpError("timeout", message="readiness memo wait"))
                if ok:
                    return value
                raise value
        try:
            payload, _ = self.ctx.gcp.request(method, url, quota_project=self.quota, params=params,
                                              json_body=json_body, timeout=timeout)
            result = (True, payload if payload is not None else {})
        except GcpError as err:
            if is_vpcsc(err):
                host = urllib.parse.urlsplit(url).hostname or ""
                self.note_vpcsc(project_hint or "", host.split(".")[0].split("-")[-1] + ".googleapis.com",
                                urllib.parse.urlsplit(url).path[:120])
            result = (False, err)
        except Exception as err:  # pylint: disable=broad-except
            LOG.warning("readiness request failed host=%s kind=%s", urllib.parse.urlsplit(url).hostname,
                        type(err).__name__)
            result = (False, GcpError("http", message=type(err).__name__))
        if key is not None:
            pending.result = result
            pending.event.set()
        ok, value = result
        if ok:
            return value
        raise value

    def try_call(self, method: str, url: str, **kwargs):
        """(payload, None) on success, (None, GcpError) on failure."""
        try:
            return self.call(method, url, **kwargs), None
        except GcpError as err:
            return None, err

    def paginate(self, url: str, items_key: str, *, params=None, max_pages: int = 5, page_size: int | None = None,
                 project_hint: str = ""):
        """Follows nextPageToken through memoized calls. Returns (items, truncated). Raises GcpError."""
        items: list = []
        base = dict(params or {})
        if page_size:
            base["pageSize"] = page_size
        token = None
        for _ in range(max_pages):
            p = dict(base)
            if token:
                p["pageToken"] = token
            payload = self.call("GET", url, params=p or None, project_hint=project_hint)
            items.extend([i for i in (payload or {}).get(items_key) or [] if isinstance(i, dict)])
            token = (payload or {}).get("nextPageToken")
            if not token:
                return items, False
        return items, True

    def parallel(self, tasks: dict) -> dict:
        """Runs callables in parallel through the shared worker pools; never raises."""
        if not tasks:
            return {}
        try:
            return self.ctx.run(tasks)
        except Exception as err:  # pylint: disable=broad-except
            LOG.warning("readiness parallel run failed: %s", type(err).__name__)
            out = {}
            for name, fn in tasks.items():
                try:
                    out[name] = (True, fn())
                except Exception as sub:  # pylint: disable=broad-except
                    out[name] = (False, sub)
            return out

    # -- shared resource readers ---------------------------------------------------
    def project_number(self, pid: str) -> str:
        num = self.fact(pid, "number")
        if num:
            return num
        payload, err = self.try_call("GET", "%s/projects/%s" % (CRM_V3, urllib.parse.quote(pid, safe="")),
                                     project_hint=pid)
        if err is None:
            name = str((payload or {}).get("name") or "")
            if name.startswith("projects/") and name[9:].isdigit():
                self.set_fact(pid, "number", name[9:])
                return name[9:]
        return ""

    def ancestry(self, pid: str) -> list[dict] | None:
        """[{'type': 'project'|'folder'|'organization', 'id': ...}, ...] from project up to the org, or None."""
        cached = self.fact(pid, "ancestry")
        if cached is not None:
            return cached
        payload, err = self.try_call("POST", "%s/projects/%s:getAncestry" % (CRM_V1, urllib.parse.quote(pid, safe="")),
                                     json_body={}, project_hint=pid)
        if err is not None:
            return None
        out = []
        for item in (payload or {}).get("ancestor") or []:
            rid = (item or {}).get("resourceId") or {}
            if rid.get("type") and rid.get("id"):
                out.append({"type": str(rid["type"]), "id": str(rid["id"])})
        self.set_fact(pid, "ancestry", out)
        org = next((a["id"] for a in out if a["type"] == "organization"), "")
        self.set_fact(pid, "org", org)
        return out

    def org_of(self, pid: str) -> str:
        anc = self.ancestry(pid)
        if anc is None:
            return self.org_hint if config.SCOPE_ID_RE.match(self.org_hint or "") else ""
        return next((a["id"] for a in anc if a["type"] == "organization"), "")

    def enabled_services(self, pid: str) -> set[str] | None:
        """Enabled services of a project as read by the `apis` category (None when unknown)."""
        return self.fact(pid, "enabled_services")

    def api_enabled(self, pid: str, service: str):
        """True / False / None (unknown)."""
        enabled = self.enabled_services(pid)
        if enabled is None:
            return None
        return service in enabled

    def dataset_meta(self, project: str, dataset: str):
        """(metadata, err) of a BigQuery dataset (memoized)."""
        url = "%s/projects/%s/datasets/%s" % (BQ_V2, urllib.parse.quote(project, safe=""),
                                             urllib.parse.quote(dataset, safe=""))
        return self.try_call("GET", url, project_hint=project)

    def iam_policy(self, pid: str):
        """(project allow policy incl. auditConfigs, err) — CRM v1 getIamPolicy (version 3)."""
        return self.try_call("POST", "%s/projects/%s:getIamPolicy" % (CRM_V1, urllib.parse.quote(pid, safe="")),
                             json_body={"options": {"requestedPolicyVersion": 3}}, project_hint=pid)

    def effective_policy(self, resource: str, constraint: str):
        """(normalized policy | None, raw, err) for constraints/<constraint> on `resource` (projects/P, ...)."""
        url = "%s/%s/policies/%s:getEffectivePolicy" % (ORGPOLICY_V2, resource, constraint)
        payload, err = self.try_call("GET", url, project_hint=resource.split("/", 1)[-1])
        if err is not None:
            return None, None, err
        return normalize_policy(payload), payload, None
