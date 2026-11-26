"""Readiness categories `iam` (runtime / enforcer / deployer permissions), `enforcement` (IAM Deny v2 state, Pub/Sub,
Scheduler, Cloud Run) and `quotas` (Cloud Quotas API reachability).

Permission evaluation has two modes (Run.identity_mode):
- `caller`   inside Cloud Run / the Enforcer Job: the caller IS the runtime identity, so
             `projects.testIamPermissions` gives the exact answer (HTTP 400 on an unknown permission is handled by
             re-testing permissions one by one, like the reference preflight);
- `bindings` from the CLI / installer: the caller is the deployer. The runtime and enforcer service accounts are
             evaluated from the allow policies of the project and its ancestors against a role -> permission table,
             and the deployer's own permissions are tested with testIamPermissions (`iam.deployer.permissions`).
Missing permissions map to least-privilege predefined roles and copy-paste gcloud commands; organization-only roles
(Organization Policy Administrator, Deny Admin/Reviewer) produce organization-level commands.
"""

from __future__ import annotations

import urllib.parse

from .. import config
from ..gcp import GcpError
from . import enforcers
from .readiness_core import (CRM_V1, CRM_V3, DOCS, IAM_V2, PUBSUB_V1, QUOTAS_V1, RUN_V2, SCHEDULER_V1, check,
                             cmd_bind_folder, cmd_bind_org, cmd_bind_project, cmd_enable, disabled_service,
                             sa_member, skipped)

# Project-level custom role holding only aiplatform.endpoints.fetchPublisherModelConfig (created by deploy.sh /
# Terraform in every project); `{project}` is replaced by the project id in grant commands.
VERTEX_LOGGING_READER_ID = "gcpAiCapsVertexLoggingReader"
VERTEX_LOGGING_READER = "projects/{project}/roles/" + VERTEX_LOGGING_READER_ID
VERTEX_FETCH = "aiplatform.endpoints.fetchPublisherModelConfig"
VERTEX_SET = "aiplatform.endpoints.setPublisherModelConfig"

# (permission, least-privilege role, level, feature, where) — where: all | hub | dataset
RUNTIME_PERMISSIONS = (
    ("resourcemanager.projects.get", "roles/browser", "required", "scope", "all"),
    ("resourcemanager.projects.getIamPolicy", "roles/browser", "optional", "readiness", "all"),
    ("serviceusage.services.get", "roles/serviceusage.serviceUsageConsumer", "optional", "readiness", "all"),
    ("logging.logEntries.list", "roles/logging.viewer", "required", "usage", "all"),
    ("logging.privateLogEntries.list", "roles/logging.privateLogViewer", "required", "usage", "all"),
    ("bigquery.jobs.create", "roles/bigquery.jobUser", "required", "usage", "hub"),
    ("bigquery.tables.getData", "roles/bigquery.dataViewer", "required", "usage", "dataset"),
    ("iam.denypolicies.get", "roles/iam.denyReviewer", "optional", "caps_enforcement", "all"),
    ("orgpolicy.policy.get", "roles/orgpolicy.policyViewer", "optional", "model_governance", "all"),
    ("orgpolicy.policies.create", "roles/orgpolicy.policyAdmin", "optional", "model_governance", "all"),
    ("orgpolicy.policies.update", "roles/orgpolicy.policyAdmin", "optional", "model_governance", "all"),
    ("orgpolicy.policies.delete", "roles/orgpolicy.policyAdmin", "optional", "model_governance", "all"),
    ("cloudquotas.quotas.get", "roles/cloudquotas.viewer", "optional", "quota_management", "all"),
    ("cloudquotas.quotas.update", "roles/cloudquotas.admin", "optional", "quota_management", "all"),
    ("monitoring.timeSeries.list", "roles/monitoring.viewer", "optional", "quota_management", "all"),
    ("discoveryengine.engines.get", "roles/discoveryengine.viewer", "optional", "gemini_enterprise", "all"),
    ("discoveryengine.engines.update", "roles/discoveryengine.editor", "optional", "gemini_enterprise", "all"),
    # Gemini Platform inference request metadata logging state (readiness + GET /v1/projects/{p}/vertex-logging). No
    # read-only predefined role is documented to hold it, so the installer creates a one-permission custom role.
    ("aiplatform.endpoints.fetchPublisherModelConfig", VERTEX_LOGGING_READER, "optional", "vertex_inference_logging",
     "all"),
)
ENFORCER_PERMISSIONS = (
    ("iam.denypolicies.get", "roles/iam.denyAdmin", "required", "caps_enforcement", "all"),
    ("iam.denypolicies.create", "roles/iam.denyAdmin", "required", "caps_enforcement", "all"),
    ("iam.denypolicies.update", "roles/iam.denyAdmin", "required", "caps_enforcement", "all"),
    ("iam.denypolicies.delete", "roles/iam.denyAdmin", "required", "caps_enforcement", "all"),
    ("logging.logEntries.list", "roles/logging.viewer", "required", "usage", "all"),
    ("logging.privateLogEntries.list", "roles/logging.privateLogViewer", "required", "usage", "all"),
)
DEPLOYER_HUB = (
    ("serviceusage.services.enable", "roles/serviceusage.serviceUsageAdmin"),
    ("resourcemanager.projects.getIamPolicy", "roles/resourcemanager.projectIamAdmin"),
    ("resourcemanager.projects.setIamPolicy", "roles/resourcemanager.projectIamAdmin"),
    ("iam.serviceAccounts.create", "roles/iam.serviceAccountAdmin"),
    ("iam.serviceAccounts.setIamPolicy", "roles/iam.serviceAccountAdmin"),
    ("iam.serviceAccounts.actAs", "roles/iam.serviceAccountUser"),
    ("storage.buckets.create", "roles/storage.admin"),
    ("secretmanager.secrets.create", "roles/secretmanager.admin"),
    ("run.services.create", "roles/run.admin"),
    ("run.services.setIamPolicy", "roles/run.admin"),
    ("artifactregistry.repositories.create", "roles/artifactregistry.admin"),
    ("cloudbuild.builds.create", "roles/cloudbuild.builds.editor"),
    ("bigquery.datasets.create", "roles/bigquery.admin"),
    ("logging.sinks.create", "roles/logging.configWriter"),
    ("pubsub.topics.create", "roles/pubsub.admin"),
    ("cloudscheduler.jobs.create", "roles/cloudscheduler.admin"),
)
DEPLOYER_MANAGED = (
    ("serviceusage.services.enable", "roles/serviceusage.serviceUsageAdmin"),
    ("resourcemanager.projects.getIamPolicy", "roles/resourcemanager.projectIamAdmin"),
    ("resourcemanager.projects.setIamPolicy", "roles/resourcemanager.projectIamAdmin"),
    ("logging.sinks.create", "roles/logging.configWriter"),
    ("logging.sinks.get", "roles/logging.configWriter"),
)
# Optional installer permissions on every project: enabling Gemini Platform inference request metadata logging
# (prepare.sh) and creating the gcpAiCapsVertexLoggingReader custom role (deploy.sh). Missing ones only warn: the
# installer skips those steps and prints the commands.
DEPLOYER_OPTIONAL = (
    ("aiplatform.endpoints.setPublisherModelConfig", "roles/aiplatform.admin"),
    ("aiplatform.endpoints.fetchPublisherModelConfig", "roles/aiplatform.admin"),
    ("iam.roles.create", "roles/iam.roleAdmin"),
)
SCOPE_PERMISSIONS = ("iam.denypolicies.get", "iam.denypolicies.create", "iam.denypolicies.update",
                     "iam.denypolicies.delete")

ORG_ONLY_ROLES = {"roles/orgpolicy.policyAdmin", "roles/iam.denyAdmin", "roles/iam.denyReviewer"}
ROLE_COVERS = {
    "roles/cloudquotas.admin": {"roles/cloudquotas.viewer"},
    "roles/discoveryengine.editor": {"roles/discoveryengine.viewer"},
    "roles/orgpolicy.policyAdmin": {"roles/orgpolicy.policyViewer"},
    "roles/iam.denyAdmin": {"roles/iam.denyReviewer"},
    "roles/logging.privateLogViewer": set(),
}

# Role -> permissions (subset relevant to this product) used in `bindings` mode.
_ALL_PROJECT = {"resourcemanager.projects.get", "resourcemanager.projects.getIamPolicy", "serviceusage.services.get", "logging.logEntries.list",
                "logging.privateLogEntries.list", "bigquery.jobs.create", "bigquery.tables.getData",
                "monitoring.timeSeries.list", "discoveryengine.engines.get", "discoveryengine.engines.update",
                "cloudquotas.quotas.get", "cloudquotas.quotas.update", "orgpolicy.policy.get", VERTEX_FETCH, VERTEX_SET}
ROLE_PERMISSIONS = {
    "roles/owner": _ALL_PROJECT,
    "roles/editor": _ALL_PROJECT - {"logging.privateLogEntries.list"},
    "roles/viewer": {"resourcemanager.projects.get", "resourcemanager.projects.getIamPolicy",
                     "serviceusage.services.get", "logging.logEntries.list",
                     "monitoring.timeSeries.list", "discoveryengine.engines.get", "cloudquotas.quotas.get",
                     "orgpolicy.policy.get"},
    "roles/browser": {"resourcemanager.projects.get", "resourcemanager.projects.getIamPolicy"},
    "roles/iam.securityReviewer": {"resourcemanager.projects.getIamPolicy", "iam.denypolicies.get"},
    "roles/serviceusage.serviceUsageConsumer": {"serviceusage.services.get"},
    "roles/serviceusage.serviceUsageViewer": {"serviceusage.services.get"},
    "roles/logging.viewer": {"logging.logEntries.list", "resourcemanager.projects.get"},
    "roles/logging.privateLogViewer": {"logging.logEntries.list", "logging.privateLogEntries.list"},
    "roles/logging.admin": {"logging.logEntries.list", "logging.privateLogEntries.list"},
    "roles/bigquery.jobUser": {"bigquery.jobs.create", "resourcemanager.projects.get"},
    "roles/bigquery.user": {"bigquery.jobs.create"},
    "roles/bigquery.dataViewer": {"bigquery.tables.getData"},
    "roles/bigquery.dataEditor": {"bigquery.tables.getData"},
    "roles/bigquery.dataOwner": {"bigquery.tables.getData"},
    "roles/bigquery.admin": {"bigquery.tables.getData", "bigquery.jobs.create"},
    "roles/iam.denyReviewer": {"iam.denypolicies.get"},
    "roles/iam.denyAdmin": {"iam.denypolicies.get", "iam.denypolicies.create", "iam.denypolicies.update",
                            "iam.denypolicies.delete"},
    "roles/orgpolicy.policyViewer": {"orgpolicy.policy.get"},
    "roles/orgpolicy.policyAdmin": {"orgpolicy.policy.get", "orgpolicy.policies.create", "orgpolicy.policies.update",
                                    "orgpolicy.policies.delete"},
    "roles/cloudquotas.viewer": {"cloudquotas.quotas.get"},
    "roles/cloudquotas.admin": {"cloudquotas.quotas.get", "cloudquotas.quotas.update"},
    "roles/monitoring.viewer": {"monitoring.timeSeries.list"},
    "roles/monitoring.editor": {"monitoring.timeSeries.list"},
    "roles/discoveryengine.viewer": {"discoveryengine.engines.get"},
    "roles/discoveryengine.editor": {"discoveryengine.engines.get", "discoveryengine.engines.update"},
    "roles/discoveryengine.admin": {"discoveryengine.engines.get", "discoveryengine.engines.update"},
    # Gemini Platform Administrator / User hold the publisher model config permissions (User: per Google's
    # request-response logging guidance; not exhaustively verified for fetch).
    "roles/aiplatform.admin": {VERTEX_FETCH, VERTEX_SET},
    "roles/aiplatform.user": {VERTEX_FETCH, VERTEX_SET},
}


def _role_permissions(role: str) -> set:
    """Permissions of a role for `bindings` mode, including the installer's project/org custom role."""
    if role.endswith("/roles/" + VERTEX_LOGGING_READER_ID):
        return {VERTEX_FETCH}
    return ROLE_PERMISSIONS.get(role, set())


# ---------------------------------------------------------------------------
# Permission evaluation
# ---------------------------------------------------------------------------

def test_permissions(run, resource_url: str, perms: list[str], hint: str = ""):
    """(granted:set, valid:set) via <resource>:testIamPermissions; unknown permissions (HTTP 400) are retested
    individually and dropped. Raises GcpError on other failures."""
    try:
        payload = run.call("POST", resource_url, json_body={"permissions": list(perms)}, project_hint=hint)
        return set((payload or {}).get("permissions") or []), set(perms)
    except GcpError as err:
        if err.kind != "invalid":
            raise
    granted, valid = set(), set()
    for perm in perms:
        try:
            payload = run.call("POST", resource_url, json_body={"permissions": [perm]}, project_hint=hint)
            valid.add(perm)
            granted |= set((payload or {}).get("permissions") or [])
        except GcpError as sub:
            if sub.kind != "invalid":
                raise
    return granted, valid


def _member_roles(policies: list[dict], member: str) -> set[str]:
    roles = set()
    low = member.lower()
    for pol in policies:
        for b in (pol or {}).get("bindings") or []:
            if b.get("condition"):
                continue
            if low in [m.lower() for m in b.get("members") or []]:
                roles.add(str(b.get("role") or ""))
    return roles


def _ancestor_policies(run, pid: str) -> list[dict]:
    out = []
    for anc in run.ancestry(pid) or []:
        if anc["type"] == "organization":
            pol, err = run.try_call("POST", "%s/organizations/%s:getIamPolicy" % (CRM_V1, anc["id"]),
                                    json_body={"options": {"requestedPolicyVersion": 3}})
        elif anc["type"] == "folder":
            pol, err = run.try_call("POST", "%s/folders/%s:getIamPolicy" % (CRM_V3, anc["id"]),
                                    json_body={"options": {"requestedPolicyVersion": 3}})
        else:
            continue
        if err is None and pol:
            out.append(pol)
    return out


def _bindings_granted(run, pid: str, member: str):
    """(granted perms, roles, err) for `member` from project + ancestor allow policies."""
    pol, err = run.iam_policy(pid)
    if err is not None:
        return None, None, err
    policies = [pol or {}] + _ancestor_policies(run, pid)
    roles = _member_roles(policies, member)
    granted = set()
    for role in roles:
        granted |= _role_permissions(role)
    return granted, roles, None


def _applicable(run, pid: str, table) -> list[tuple]:
    out = []
    for perm, role, level, feature, where in table:
        if where == "hub" and not run.is_hub(pid):
            continue
        if where == "dataset" and pid not in ([run.dataset_project] if run.central else run.projects):
            continue
        out.append((perm, role, level, feature))
    return out


def _minimal_roles(roles: list[str]) -> list[str]:
    out = []
    for r in roles:
        if any(r in ROLE_COVERS.get(other, set()) for other in roles if other != r):
            continue
        if r not in out:
            out.append(r)
    return out


def cmd_create_vertex_reader(pid: str) -> str:
    return ("gcloud iam roles create %s --project=%s --title=\"GCP AI Caps Vertex logging reader\" "
            "--permissions=%s --stage=GA" % (VERTEX_LOGGING_READER_ID, pid, VERTEX_FETCH))


def _grant_commands(run, pid: str, member: str, roles: list[str]) -> list[str]:
    cmds = []
    for role in roles:
        if role in ORG_ONLY_ROLES:
            cmds.append(cmd_bind_org(run.org_of(pid) or "ORGANIZATION_ID", member, role))
        elif role.endswith("/roles/" + VERTEX_LOGGING_READER_ID):
            cmds.append(cmd_create_vertex_reader(pid))
            cmds.append(cmd_bind_project(pid, member, role))
        else:
            cmds.append(cmd_bind_project(pid, member, role))
    return list(dict.fromkeys(cmds))


def _verdict(run, pid: str, cid: str, title: tuple, who: str, member: str, needed: list[tuple], granted: set,
             valid: set | None, mode: str, roles_found=None) -> dict:
    missing = [(p, r, lvl, f) for p, r, lvl, f in needed if (valid is None or p in valid) and p not in granted]
    req = [m for m in missing if m[2] == "required"]
    data = {"identity": who, "mode": mode, "missing": [m[0] for m in missing],
            "roles": sorted(roles_found) if roles_found is not None else None}
    if not missing:
        return check(cid, "iam", "pass", *title, "%s has every permission needed on %s (%s)." % (who, pid, mode),
                     data=data)
    roles = [r.replace("{project}", pid) for r in _minimal_roles([m[1] for m in missing])]
    data["missing_roles"] = roles
    cmds = _grant_commands(run, pid, member, roles)
    features = sorted({m[3] for m in missing})
    detail = "%s lacks on %s: %s (features: %s). Grant: %s." % (
        who, pid, ", ".join(m[0] for m in missing), ", ".join(features), ", ".join(roles))
    if any(r in ORG_ONLY_ROLES for r in roles):
        detail += " Organization Policy Administrator / Deny Admin / Deny Reviewer can only be granted on the organization."
    status = "fail" if req else "warn"
    return check(cid, "iam", status, *title, detail, impact=features, commands=cmds, auto_fixable=True, data=data,
                 blocking=bool(req) if status == "fail" else None)


def runtime_permission_check(run, pid: str) -> dict:
    title = ("런타임 서비스 계정 권한", "Runtime service account permissions")
    cid = "iam.runtime.permissions"
    needed = _applicable(run, pid, RUNTIME_PERMISSIONS)
    if run.enforcement_backend != "iam_deny":
        needed = [n for n in needed if not n[0].startswith("iam.denypolicies")]
    member = sa_member(run.runtime_sa)
    if run.identity_mode == "caller":
        try:
            granted, valid = test_permissions(run, "%s/projects/%s:testIamPermissions" % (
                CRM_V1, urllib.parse.quote(pid, safe="")), [n[0] for n in needed], hint=pid)
        except GcpError as err:
            return skipped(cid, "iam", *title, err, what="projects.testIamPermissions on %s" % pid)
        run.set_fact(pid, "runtime_granted", granted)
        return _verdict(run, pid, cid, title, run.runtime_sa, member, needed, granted, valid, "testIamPermissions")
    granted, roles, err = _bindings_granted(run, pid, member)
    if err is not None:
        return skipped(cid, "iam", *title, err, what="IAM policy of %s" % pid)
    run.set_fact(pid, "runtime_granted", granted)
    if not roles and not run.runtime_sa_configured:
        item = _verdict(run, pid, cid, title, run.runtime_sa, member, needed, granted, None, "planned")
        if item["status"] != "pass":
            item["status"] = "info"
            item.pop("blocking", None)
            item["detail"] = ("Runtime service account %s has no grants on %s yet; deploy.sh grants: %s." % (
                run.runtime_sa, pid, ", ".join(item["data"].get("missing_roles") or [])))
        return item
    return _verdict(run, pid, cid, title, run.runtime_sa, member, needed, granted, None, "allow-policy bindings",
                    roles_found=roles)


def enforcer_permission_check(run, pid: str) -> dict | None:
    if run.enforcement_backend != "iam_deny" or run.deny_attachment == "scope":
        return None
    title = ("집행(Enforcer) 서비스 계정 권한", "Enforcer service account permissions")
    cid = "iam.enforcer.permissions"
    member = sa_member(run.enforcer_sa)
    needed = _applicable(run, pid, ENFORCER_PERMISSIONS)
    granted, roles, err = _bindings_granted(run, pid, member)
    if err is not None:
        return skipped(cid, "iam", *title, err, what="IAM policy of %s and its ancestors" % pid)
    run.set_fact(pid, "enforcer_granted", granted)
    if not roles and not run.enforcer_sa_configured:
        item = _verdict(run, pid, cid, title, run.enforcer_sa, member, needed, granted, None, "planned")
        if item["status"] != "pass":
            item["status"] = "info"
            item.pop("blocking", None)
            item["detail"] = ("Enforcer service account %s has no grants on %s yet; deploy.sh grants project roles, "
                              "and roles/iam.denyAdmin on the organization with --grant-org-roles." % (run.enforcer_sa, pid))
        return item
    item = _verdict(run, pid, cid, title, run.enforcer_sa, member, needed, granted, None, "allow-policy bindings",
                    roles_found=roles)
    if item["status"] == "fail":
        # Missing Deny Admin disables per-user enforcement but not the rest of the product.
        item["blocking"] = False
    return item


def deployer_permission_check(run, pid: str) -> dict | None:
    if run.on_cloud_run:
        return None
    title = ("설치 실행 계정 권한", "Deployer (caller) permissions")
    cid = "iam.deployer.permissions"
    table = DEPLOYER_HUB if run.is_hub(pid) else DEPLOYER_MANAGED
    asked = list(dict.fromkeys([p for p, _ in table] + [p for p, _ in DEPLOYER_OPTIONAL]))
    try:
        granted, valid = test_permissions(run, "%s/projects/%s:testIamPermissions" % (
            CRM_V1, urllib.parse.quote(pid, safe="")), asked, hint=pid)
    except GcpError as err:
        return skipped(cid, "iam", *title, err, what="projects.testIamPermissions on %s" % pid)
    missing = [(p, r) for p, r in table if p in valid and p not in granted]
    missing_opt = [(p, r) for p, r in DEPLOYER_OPTIONAL if p in valid and p not in granted]
    if not missing and not missing_opt:
        return check(cid, "iam", "pass", *title, "The caller can run the installer on %s." % pid)
    if not missing:
        roles = list(dict.fromkeys(r for _, r in missing_opt))
        return check(cid, "iam", "warn", *title,
                     "The caller lacks optional permissions on %s: %s. The installer cannot enable Gemini Platform inference "
                     "request metadata logging and/or create the %s custom role there (it warns and continues). "
                     "Grant: %s." % (pid, ", ".join(p for p, _ in missing_opt), VERTEX_LOGGING_READER_ID,
                                      ", ".join(roles)),
                     impact=["vertex_inference_logging"],
                     commands=[cmd_bind_project(pid, "user:YOUR_ACCOUNT", r) for r in roles],
                     data={"missing": [], "missing_optional": [p for p, _ in missing_opt], "missing_roles": roles,
                           "self_grant": "resourcemanager.projects.setIamPolicy" in granted})
    roles = list(dict.fromkeys(r for _, r in missing + missing_opt))
    can_self_grant = "resourcemanager.projects.setIamPolicy" in granted
    detail = "The caller lacks on %s: %s. %s" % (
        pid, ", ".join(p for p, _ in missing + missing_opt),
        "It holds Project IAM Admin, so it can grant itself: %s." % ", ".join(roles) if can_self_grant else
        "Ask a project owner for roles/owner (or roles/editor + roles/resourcemanager.projectIamAdmin), or: %s."
        % ", ".join(roles))
    return check(cid, "iam", "warn" if can_self_grant else "fail", *title, detail, impact=["deployment"],
                 commands=[cmd_bind_project(pid, "user:YOUR_ACCOUNT", r) for r in roles],
                 data={"missing": [p for p, _ in missing], "missing_optional": [p for p, _ in missing_opt],
                       "missing_roles": roles, "self_grant": can_self_grant})


def scope_permission_checks(run) -> list[dict]:
    """iam.scope.permissions on folders/organizations when caps.deny_attachment='scope' (cross-project)."""
    if run.deny_attachment != "scope" or run.ws.get("scope_type") not in ("folder", "organization"):
        return []
    out = []
    coll = "folders" if run.ws.get("scope_type") == "folder" else "organizations"
    for sid in [str(s) for s in run.ws.get("scope_ids") or [] if config.SCOPE_ID_RE.match(str(s))][:10]:
        title = ("스코프 거부 정책 권한", "Scope deny policy permissions")
        url = ("%s/folders/%s:testIamPermissions" % (CRM_V3, sid)) if coll == "folders" else (
            "%s/organizations/%s:testIamPermissions" % (CRM_V1, sid))
        member = sa_member(run.enforcer_sa)
        if run.identity_mode == "caller":
            try:
                granted, valid = test_permissions(run, url, list(SCOPE_PERMISSIONS))
            except GcpError as err:
                item = skipped("iam.scope.permissions", "iam", *title, err, what="%s/%s testIamPermissions" % (coll, sid))
                item["project"] = "%s/%s" % (coll, sid)
                out.append(item)
                continue
            missing = [p for p in SCOPE_PERMISSIONS if p in valid and p not in granted]
        else:
            granted = set()
            pol, err = run.try_call("POST", url.replace(":testIamPermissions", ":getIamPolicy"),
                                    json_body={"options": {"requestedPolicyVersion": 3}})
            if err is not None:
                item = skipped("iam.scope.permissions", "iam", *title, err, what="%s/%s IAM policy" % (coll, sid))
                item["project"] = "%s/%s" % (coll, sid)
                out.append(item)
                continue
            policies = [pol or {}]
            if coll == "folders":
                org = run.org_of(run.hub)
                if org:
                    opol, oerr = run.try_call("POST", "%s/organizations/%s:getIamPolicy" % (CRM_V1, org),
                                              json_body={"options": {"requestedPolicyVersion": 3}})
                    if oerr is None and opol:
                        policies.append(opol)
            for role in _member_roles(policies, member):
                granted |= _role_permissions(role)
            missing = [p for p in SCOPE_PERMISSIONS if p not in granted]
        cmd = (cmd_bind_folder(sid, member, "roles/iam.denyAdmin") if coll == "folders"
               else cmd_bind_org(sid, member, "roles/iam.denyAdmin"))
        if missing:
            item = check("iam.scope.permissions", "iam", "fail", *title,
                         "The enforcer cannot manage deny policies on %s/%s (missing %s). Deny Admin is granted on the "
                         "organization (it is inherited by folders)." % (coll, sid, ", ".join(missing)),
                         impact=["caps_enforcement"], commands=[cmd_bind_org(run.org_of(run.hub) or sid,
                                                                             member, "roles/iam.denyAdmin")
                                                                if coll == "folders" else cmd],
                         blocking=False, auto_fixable=True)
        else:
            item = check("iam.scope.permissions", "iam", "pass", *title,
                         "Deny policies can be managed on %s/%s." % (coll, sid))
        item["project"] = "%s/%s" % (coll, sid)
        out.append(item)
    return out


def iam_checks(run, pid: str) -> list[dict]:
    out = [runtime_permission_check(run, pid)]
    for item in (enforcer_permission_check(run, pid), deployer_permission_check(run, pid)):
        if item:
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# enforcement
# ---------------------------------------------------------------------------

def deny_state_checks(run) -> list[dict]:
    """enforcement.deny_policies for each attachment point (bounded by the IAM v2 read budget)."""
    if run.enforcement_backend != "iam_deny":
        return [check("enforcement.deny_policies", "enforcement", "info", "IAM Deny 정책 상태", "IAM Deny policy state",
                      "enforcement_backend=%s: deny policies are not used." % run.enforcement_backend)]

    class _Ctx:  # minimal adapter for enforcers.attachment_points
        def __init__(self, r):
            self.config = r.cfg
            self.all_projects = r.projects
    try:
        points = enforcers.attachment_points(_Ctx(run))
    except Exception:  # pylint: disable=broad-except
        points = []
    pid_ours = enforcers.policy_id(run.cfg)
    out = []
    for ap in points:
        target = ap.split("cloudresourcemanager.googleapis.com/", 1)[-1]
        title = ("IAM Deny 정책 상태", "IAM Deny policy state")
        if not run.take_iam_v2_read():
            item = check("enforcement.deny_policies", "enforcement", "skip", *title,
                         "Not read to preserve the IAM v2 read quota (5 requests/minute); the Enforcer Job reports it.",
                         docs_url=DOCS["iam_quotas"])
            item["project"] = target
            out.append(item)
            continue
        url = "%s/policies/%s/denypolicies" % (IAM_V2, urllib.parse.quote(ap, safe=""))
        payload, err = run.try_call("GET", url, project_hint=target)
        if err is not None:
            item = skipped("enforcement.deny_policies", "enforcement", *title, err,
                           what="deny policies on %s (roles/iam.denyReviewer)" % target)
            item["project"] = target
            out.append(item)
            continue
        policies = [p for p in (payload or {}).get("policies") or [] if isinstance(p, dict)]
        ids = [str(p.get("name") or "").rsplit("/", 1)[-1] for p in policies]
        ours = pid_ours in ids
        foreign_prefixed = [i for i in ids if i.startswith(enforcers.POLICY_PREFIX) and i != pid_ours]
        principals = None
        if ours and run.take_iam_v2_read():
            pol, perr = run.try_call("GET", "%s/%s" % (url, pid_ours), project_hint=target)
            if perr is None:
                principals = sum(len((r.get("denyRule") or {}).get("deniedPrincipals") or [])
                                 for r in (pol or {}).get("rules") or [])
        data = {"attachment_point": ap, "policy_count": len(policies), "managed_policy": ours,
                "foreign_prefixed": foreign_prefixed, "denied_principals": principals,
                "limits": {"policies_per_resource": 500, "rules_per_resource": 500,
                           "principals_per_resource": enforcers.DENY_PRINCIPAL_LIMIT}}
        status, notes = "pass", []
        if foreign_prefixed:
            status = "warn"
            notes.append("other policies with the %s prefix exist (%s): another installation may manage this resource"
                         % (enforcers.POLICY_PREFIX, ", ".join(foreign_prefixed[:3])))
        if len(policies) >= 450:
            status = "warn"
            notes.append("%d deny policies attached (limit 500)" % len(policies))
        if principals is not None and principals >= enforcers.DENY_WARN_AT:
            status = "warn"
            notes.append("%d denied principals (limit %d across the resource)" % (principals,
                                                                                  enforcers.DENY_PRINCIPAL_LIMIT))
        detail = "%d deny policy(ies) on %s; managed policy %s %s." % (
            len(policies), target, pid_ours, "present" if ours else "not created yet")
        if notes:
            detail += " " + "; ".join(notes) + "."
        item = check("enforcement.deny_policies", "enforcement", status, *title, detail, data=data,
                     docs_url=DOCS["deny"])
        item["project"] = target
        out.append(item)
    return out


def hub_resource_checks(run) -> list[dict]:
    """Pub/Sub topic & push subscription, Scheduler job, Cloud Run service & job on the hub (info when absent)."""
    hub = urllib.parse.quote(run.hub, safe="")
    svc = run.service_name
    probes = {
        "topic": ("enforcement.pubsub.topic", ("실시간 Pub/Sub 토픽", "Real-time Pub/Sub topic"),
                  "%s/projects/%s/topics/%s" % (PUBSUB_V1, hub, run.topic)),
        "sub": ("enforcement.pubsub.subscription", ("Pub/Sub 푸시 구독", "Pub/Sub push subscription"),
                "%s/projects/%s/subscriptions/%s-usage-events-push" % (PUBSUB_V1, hub, svc)),
        "sched": ("enforcement.scheduler", ("Cloud Scheduler 안전망", "Cloud Scheduler safety net"),
                  "%s/projects/%s/locations/%s/jobs/%s-enforce-schedule" % (SCHEDULER_V1, hub, run.region, svc)),
        "service": ("enforcement.cloudrun.service", ("Cloud Run API 서비스", "Cloud Run API service"),
                    "%s/projects/%s/locations/%s/services/%s" % (RUN_V2, hub, run.region, svc)),
        "job": ("enforcement.cloudrun.job", ("Cloud Run 집행 Job", "Cloud Run Enforcer Job"),
                "%s/projects/%s/locations/%s/jobs/%s-enforcer" % (RUN_V2, hub, run.region, svc)),
    }
    res = run.parallel({k: (lambda u=v[2]: run.try_call("GET", u, project_hint=run.hub)) for k, v in probes.items()})
    out = []
    for key, (cid, title, _) in probes.items():
        ok, value = res.get(key, (False, None))
        payload, err = value if ok and isinstance(value, tuple) else (None, value)
        if err is not None:
            if getattr(err, "kind", "") == "not_found":
                out.append(check(cid, "enforcement", "info", *title,
                                 "Not deployed yet (deploy.sh creates it)." if key != "sched" else
                                 "Not deployed yet; without it enforcement runs only on demand/real-time."))
            else:
                out.append(skipped(cid, "enforcement", *title, err, what=cid.split(".", 1)[1]))
            continue
        data = {}
        detail = "Present."
        status = "pass"
        if key == "sub":
            endpoint = str(((payload or {}).get("pushConfig") or {}).get("pushEndpoint") or "")
            data["push_endpoint_path"] = urllib.parse.urlsplit(endpoint).path
            if not endpoint.endswith("/v1/internal/usage-event"):
                status = "warn"
                detail = "Subscription exists but does not push to /v1/internal/usage-event."
        elif key == "service":
            data = {"uri": (payload or {}).get("uri"), "ingress": (payload or {}).get("ingress"),
                    "invoker_iam_disabled": bool((payload or {}).get("invokerIamDisabled"))}
            detail = "Deployed at %s (ingress %s)." % (data["uri"], data["ingress"])
        elif key == "sched":
            state = str((payload or {}).get("state") or "")
            if state and state != "ENABLED":
                status = "warn"
                detail = "Scheduler job is %s." % state
        out.append(check(cid, "enforcement", status, *title, detail, data=data or None))
    return out


# ---------------------------------------------------------------------------
# quotas
# ---------------------------------------------------------------------------

def quota_checks(run, pid: str) -> list[dict]:
    title = ("Cloud Quotas API 접근", "Cloud Quotas API reachable")
    service = (run.cfg.get("quotas") or {}).get("service") or "aiplatform.googleapis.com"
    url = "%s/projects/%s/locations/global/services/%s/quotaInfos" % (QUOTAS_V1, urllib.parse.quote(pid, safe=""),
                                                                      service)
    payload, err = run.try_call("GET", url, params={"pageSize": 1}, project_hint=pid)
    out = []
    if err is None:
        run.set_fact(pid, "quotas_api", True)
        out.append(check("quotas.api", "quotas", "pass", *title,
                         "QuotaInfos of %s are readable on %s." % (service, pid)))
    else:
        run.set_fact(pid, "quotas_api", False if err.kind in ("denied", "not_found") else None)
        svc = disabled_service(err)
        if svc:
            out.append(check("quotas.api", "quotas", "warn", *title,
                             "%s is disabled: quota management is unavailable for %s." % (svc, pid),
                             impact=["quota_management"], commands=[cmd_enable(pid, ["cloudquotas.googleapis.com"])],
                             auto_fixable=True))
        elif err.kind == "denied":
            out.append(check("quotas.api", "quotas", "warn", *title,
                             "Quota infos of %s are not readable (roles/cloudquotas.viewer, or admin for requests)." % pid,
                             impact=["quota_management"],
                             commands=[cmd_bind_project(pid, sa_member(run.runtime_sa), "roles/cloudquotas.admin")]))
        else:
            out.append(skipped("quotas.api", "quotas", *title, err, what="Cloud Quotas quotaInfos"))
    if run.is_hub(pid):
        email = (run.cfg.get("quotas") or {}).get("contact_email") or ""
        out.append(check("quotas.contact_email", "quotas", "pass" if email else "warn",
                         "쿼터 증설 요청 연락처", "Quota increase contact email",
                         "Configured." if email else
                         "quotas.contact_email (QUOTA_CONTACT_EMAIL) is empty: quota increase requests (QuotaPreference) "
                         "that need Google review cannot be submitted.", impact=[] if email else ["quota_management"]))
    return out
