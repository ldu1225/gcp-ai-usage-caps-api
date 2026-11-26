"""Readiness categories `project` (existence, ancestry, billing) and `apis` (Service Usage batchGet)."""

from __future__ import annotations

import urllib.parse

from ..gcp import GcpError
from .readiness_core import (BILLING_V1, CRM_V3, SERVICEUSAGE_V1, check, cmd_enable, disabled_service,
                             reason_of, skipped)

# (service, level, impact features, short description). level: required | optional | surface
HUB_APIS = (
    ("run.googleapis.com", "required", ["deployment"], "Cloud Run API service + Enforcer Job"),
    ("artifactregistry.googleapis.com", "required", ["deployment"], "container image registry"),
    ("cloudbuild.googleapis.com", "optional", ["deployment"], "image build (falls back to local Docker build)"),
    ("storage.googleapis.com", "required", ["state", "deployment"], "GCS state bucket (policies, API key hashes)"),
    ("secretmanager.googleapis.com", "required", ["deployment"], "bootstrap admin key secret"),
    ("pubsub.googleapis.com", "optional", ["realtime"], "real-time usage event push (5-min scheduler fallback)"),
    ("cloudscheduler.googleapis.com", "optional", ["safety_net", "pricing_sync"],
     "5-minute enforcement safety net & daily pricing sync"),
    ("iam.googleapis.com", "required", ["caps_enforcement", "deployment"], "service accounts & IAM Deny v2"),
    ("iamcredentials.googleapis.com", "required", ["realtime", "deployment"], "OIDC tokens for push/scheduler"),
    ("cloudresourcemanager.googleapis.com", "required", ["scope", "iam"], "project/folder/org discovery & IAM"),
    ("serviceusage.googleapis.com", "required", ["readiness"], "API enablement & quota project"),
    ("logging.googleapis.com", "required", ["usage", "audit"], "Cloud Logging inference logs & audit trail"),
    ("bigquery.googleapis.com", "required", ["usage", "chargeback"], "Log Router dataset & billing export queries"),
    ("cloudbilling.googleapis.com", "optional", ["pricing", "spend_caps"], "billing info & official SKU prices"),
    ("billingbudgets.googleapis.com", "optional", ["spend_caps"], "project hard spend caps (Billing Budgets)"),
    ("orgpolicy.googleapis.com", "required", ["model_governance", "readiness"],
     "model governance (constraints/vertexai.allowedModels) & org policy preflight"),
    ("cloudquotas.googleapis.com", "required", ["quota_management"], "quota management (QuotaInfo/QuotaPreference)"),
    ("monitoring.googleapis.com", "required", ["quota_management"], "quota usage time series"),
    ("accesscontextmanager.googleapis.com", "optional", ["readiness"], "VPC Service Controls perimeter lookup"),
    ("aiplatform.googleapis.com", "required", ["vertex_ai", "model_governance"],
     "Vertex AI model catalog & Data Access audit logs"),
    ("discoveryengine.googleapis.com", "surface", ["gemini_enterprise"], "Gemini Enterprise apps"),
    ("businessaicode.googleapis.com", "surface", ["antigravity"], "Antigravity (AI developer tools)"),
)

MANAGED_CORE = ("logging.googleapis.com", "iam.googleapis.com", "cloudresourcemanager.googleapis.com",
                "serviceusage.googleapis.com", "orgpolicy.googleapis.com", "cloudquotas.googleapis.com")


def managed_apis(run) -> tuple:
    """API expectations for a managed (spoke) project."""
    out = [
        ("logging.googleapis.com", "required", ["usage"], "inference/audit logs and Log Router sinks"),
        ("iam.googleapis.com", "required", ["caps_enforcement"], "IAM Deny v2 attachment"),
        ("cloudresourcemanager.googleapis.com", "required", ["iam", "scope"], "IAM policy & ancestry"),
        ("serviceusage.googleapis.com", "required", ["readiness"], "API enablement state"),
        ("orgpolicy.googleapis.com", "required", ["model_governance"], "project model access policy"),
        ("cloudquotas.googleapis.com", "required", ["quota_management"], "project quota management"),
        ("monitoring.googleapis.com", "optional", ["quota_management"], "quota usage time series"),
        ("bigquery.googleapis.com", "optional" if run.central else "required", ["usage"],
         "per-project audit dataset" if not run.central else "not needed with the central hub dataset"),
        ("aiplatform.googleapis.com", "surface", ["vertex_ai"], "Vertex AI usage"),
        ("discoveryengine.googleapis.com", "surface", ["gemini_enterprise"], "Gemini Enterprise apps"),
        ("businessaicode.googleapis.com", "surface", ["antigravity"], "Antigravity (AI developer tools)"),
    ]
    return tuple(out)


def short(service: str) -> str:
    return service.split(".", 1)[0]


# ---------------------------------------------------------------------------
# project
# ---------------------------------------------------------------------------

def project_checks(run, pid: str) -> list[dict]:
    """project.exists, project.ancestry, project.billing."""
    out: list[dict] = []
    payload, err = run.try_call("GET", "%s/projects/%s" % (CRM_V3, urllib.parse.quote(pid, safe="")),
                                project_hint=pid)
    if err is not None:
        status = "fail"
        detail = ("Project %s does not exist or the caller cannot read it (%s). Every other check depends on it."
                  % (pid, reason_of(err)))
        if err.kind not in ("denied", "not_found"):
            status = "skip"
            detail = "Project %s could not be read: %s." % (pid, reason_of(err))
        out.append(check("project.exists", "project", status, "프로젝트 존재/접근 확인", "Project exists & accessible",
                         detail, impact=["all"], blocking=True,
                         commands=["gcloud projects describe %s" % pid]))
        run.set_fact(pid, "exists", False if status == "fail" else None)
    else:
        state = str((payload or {}).get("state") or "")
        name = str((payload or {}).get("name") or "")
        number = name[9:] if name.startswith("projects/") else ""
        parent = str((payload or {}).get("parent") or "")
        if number:
            run.set_fact(pid, "number", number)
        run.set_fact(pid, "parent", parent)
        run.set_fact(pid, "display_name", str((payload or {}).get("displayName") or pid)[:120])
        if state and state != "ACTIVE":
            out.append(check("project.exists", "project", "fail", "프로젝트 상태", "Project state",
                             "Project %s is %s (must be ACTIVE)." % (pid, state), impact=["all"],
                             commands=["gcloud projects undelete %s" % pid] if state == "DELETE_REQUESTED" else []))
            run.set_fact(pid, "exists", False)
        else:
            out.append(check("project.exists", "project", "pass", "프로젝트 존재/접근 확인", "Project exists & accessible",
                             "Project %s is ACTIVE (number %s)." % (pid, number or "?"),
                             data={"project_number": number, "parent": parent}))
            run.set_fact(pid, "exists", True)

    anc = run.ancestry(pid)
    if anc is None:
        out.append(skipped("project.ancestry", "project", "조직/폴더 계층", "Organization / folder ancestry", "denied",
                           what="the project ancestry (resourcemanager.projects.get)"))
    else:
        org = next((a["id"] for a in anc if a["type"] == "organization"), "")
        folders = [a["id"] for a in anc if a["type"] == "folder"]
        if org:
            out.append(check("project.ancestry", "project", "pass", "조직/폴더 계층", "Organization / folder ancestry",
                             "Organization %s%s." % (org, (", folders " + " > ".join(reversed(folders))) if folders else ""),
                             data={"organization_id": org, "folders": folders}))
        else:
            out.append(check("project.ancestry", "project", "warn", "조직/폴더 계층", "Organization / folder ancestry",
                             "Project %s has no organization. IAM Deny Admin/Reviewer and Organization Policy "
                             "Administrator can only be granted on an organization, so per-user IAM Deny "
                             "enforcement (use enforcement_backend=notify_only) and org-policy model governance "
                             "are unavailable." % pid,
                             impact=["caps_enforcement", "model_governance"], data={"organization_id": "",
                                                                                     "folders": folders}))

    out.append(_billing_check(run, pid))
    return out


def _billing_check(run, pid: str) -> dict:
    title = ("결제(Billing) 연결", "Cloud Billing linked")
    payload, err = run.try_call("GET", "%s/projects/%s/billingInfo" % (BILLING_V1, urllib.parse.quote(pid, safe="")),
                                project_hint=pid)
    if err is not None:
        run.set_fact(pid, "billing_enabled", None)
        if err.kind == "denied" and not disabled_service(err):
            return check("project.billing", "project", "warn", *title,
                         "Billing information of %s is not visible to the caller (common with MSP/reseller "
                         "billing). The project itself must still have an active billing account: Cloud Run and "
                         "AI APIs cannot be enabled otherwise." % pid,
                         impact=["deployment"] if run.is_hub(pid) else ["usage"])
        return skipped("project.billing", "project", *title, err, what="billingInfo")
    enabled = (payload or {}).get("billingEnabled")
    account = str((payload or {}).get("billingAccountName") or "").rsplit("/", 1)[-1]
    run.set_fact(pid, "billing_enabled", bool(enabled))
    run.set_fact(pid, "billing_account", account)
    if enabled:
        return check("project.billing", "project", "pass", *title,
                     "Billing is enabled%s." % ((" (account %s)" % account) if account else " (account masked)"),
                     data={"billing_account": account})
    if run.is_hub(pid):
        return check("project.billing", "project", "fail", *title,
                     "Billing is not enabled on the hub project %s: Cloud Run, Artifact Registry, Secret Manager and "
                     "BigQuery cannot be used. Link a billing account (Billing > Link a billing account)." % pid,
                     impact=["deployment"], commands=["gcloud billing projects link %s --billing-account=ACCOUNT_ID" % pid])
    return check("project.billing", "project", "warn", *title,
                 "Billing is not enabled on managed project %s: AI usage there is unlikely and BigQuery datasets "
                 "cannot be created." % pid, impact=["usage"],
                 commands=["gcloud billing projects link %s --billing-account=ACCOUNT_ID" % pid])


# ---------------------------------------------------------------------------
# apis
# ---------------------------------------------------------------------------

def _batch_get(run, pid: str, services: list[str]):
    """{service: 'ENABLED'|'DISABLED'} via services:batchGet (max 30 per call). Raises GcpError."""
    states: dict[str, str] = {}
    for i in range(0, len(services), 30):
        chunk = services[i:i + 30]
        payload = run.call("GET", "%s/projects/%s/services:batchGet" % (SERVICEUSAGE_V1, urllib.parse.quote(pid, safe="")),
                           params={"names": ["projects/%s/services/%s" % (pid, s) for s in chunk]}, project_hint=pid)
        for svc in (payload or {}).get("services") or []:
            name = str(((svc or {}).get("config") or {}).get("name") or str((svc or {}).get("name") or "").rsplit("/", 1)[-1])
            if name:
                states[name] = str((svc or {}).get("state") or "")
    return states


def api_checks(run, pid: str) -> list[dict]:
    expected = HUB_APIS if run.is_hub(pid) else managed_apis(run)
    services = [s for s, _, _, _ in expected]
    try:
        states = _batch_get(run, pid, services)
    except GcpError as err:
        run.set_fact(pid, "enabled_services", None)
        cmds = []
        if disabled_service(err):
            cmds = [cmd_enable(run.quota, ["serviceusage.googleapis.com"])]
        return [skipped("api.state", "apis", "API 활성화 상태", "API enablement state", err,
                        what="Service Usage services:batchGet (roles/serviceusage.serviceUsageViewer)",
                        commands=cmds)]
    enabled = {s for s, st in states.items() if st == "ENABLED"}
    run.set_fact(pid, "enabled_services", enabled)
    out = []
    missing_required, missing_optional = [], []
    for service, level, impact, what in expected:
        cid = "api.%s.enabled" % short(service)
        title_ko = "%s API 활성화" % short(service)
        title_en = "%s enabled" % service
        if service in enabled:
            out.append(check(cid, "apis", "pass", title_ko, title_en, "%s is enabled (%s)." % (service, what),
                             impact=impact))
            continue
        cmd = cmd_enable(pid, [service])
        if level == "required":
            if run.is_hub(pid) or service in MANAGED_CORE or not run.central:
                missing_required.append(service)
                out.append(check(cid, "apis", "fail", title_ko, title_en,
                                 "%s is not enabled on %s project %s. Disabled feature(s): %s (%s). prepare.sh / "
                                 "deploy.sh enable it automatically." % (service, run.role(pid), pid,
                                                                         ", ".join(impact), what),
                                 impact=impact, commands=[cmd], auto_fixable=True))
                continue
        if level in ("required", "optional"):
            missing_optional.append(service)
            out.append(check(cid, "apis", "warn", title_ko, title_en,
                             "%s is not enabled on %s: %s is degraded or uses its fallback." % (service, pid, what),
                             impact=impact, commands=[cmd], auto_fixable=True))
        else:
            out.append(check(cid, "apis", "info", title_ko, title_en,
                             "%s is not enabled on %s: the %s surface is not used in this project (its features "
                             "report not_applicable)." % (service, pid, ", ".join(impact)),
                             impact=impact, data={"surface_unused": True}))
    if missing_required or missing_optional:
        run.set_fact(pid, "missing_apis", missing_required + missing_optional)
    return out
