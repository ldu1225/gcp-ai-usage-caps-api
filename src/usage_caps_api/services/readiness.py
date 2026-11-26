"""Environment readiness (preflight) engine for the hub-and-spoke control plane.

One hub project hosts the Cloud Run API, the Enforcer Job, GCS state, Pub/Sub and Scheduler, and governs many managed
projects. Customers install into environments that may already contain parts of the product (datasets, sinks, deny
policies, topics) or lack prerequisites (APIs, billing, IAM, Gemini Enterprise apps) and enforce restrictive controls
(organization policies, VPC Service Controls, VPC-only egress, domain restricted sharing, CMEK). This engine reads all
of that - strictly read-only, pure REST through `ctx.gcp` - and produces one report:

    {object: 'readiness_report', generated_at, overall: ready|ready_with_warnings|blocked,
     summary: {pass, warn, fail, skip, info, total, blocking, blocking_unfixable},
     hub: {...}, projects: [{project_id, project_number, role, parent, organization_id, checks: [Check]}],
     cross_project: [Check], capabilities: {project_id: {...}}, adaptations: {...}, remediation_script: str}

Every check degrades to `skip` with a reason when a resource cannot be read; nothing raises out of a category.

Public API (routed by app.py):
    run_readiness(ctx, *, projects=None, categories=None) -> dict
    get_readiness(ctx)            GET  /v1/admin/readiness        (?refresh=true, ?project=P)
    run_readiness_handler(ctx)    POST /v1/admin/readiness/run    ({projects?: [...], categories?: [...]})
    capabilities(ctx, project_id) -> dict  per-surface availability for the model / quota APIs
"""

from __future__ import annotations

import logging
import time

from .. import config
from ..httpkit import ApiError, now_iso
from . import readiness_data as data_mod
from . import readiness_iam as iam_mod
from . import readiness_network as net_mod
from . import readiness_orgpolicy as org_mod
from . import readiness_project as proj_mod
from . import readiness_surfaces as surf_mod
from . import scope as scope_svc
from .readiness_core import CATEGORIES, Run, check, loc_verdict

LOG = logging.getLogger("usage_caps_api.readiness")

DOC_NAME = "readiness"
MAX_PROJECTS = 200
DEFAULT_CACHE_TTL = 900
LIGHT_CATEGORIES = ("apis", "orgpolicy", "surfaces", "iam", "quotas")
STATUS_ORDER = {"fail": 0, "warn": 1, "skip": 2, "info": 3, "pass": 4}
_TRUE = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Project resolution
# ---------------------------------------------------------------------------

def resolve_projects(ctx) -> tuple[list[str], dict]:
    """(hub + managed projects, scope info). Works with live Gcp and with the CLI StaticTokenGcp."""
    info = {"scope_type": ctx.ws.get("scope_type") or "projects", "truncated": False, "errors": []}
    try:
        projects = list(ctx.all_projects)
    except Exception as err:  # pylint: disable=broad-except
        LOG.warning("readiness project resolution failed: %s", type(err).__name__)
        projects = [ctx.project] + [p for p in ctx.ws.get("projects") or [] if config.valid_project(p)]
    spec = scope_svc.scope_spec(ctx)
    if spec is not None:
        found = None
        try:
            found = scope_svc.discover(ctx)
            if found is None:  # not "live" (e.g. CLI token client): walk the scope directly
                found = scope_svc._walk_scope(ctx, spec)  # pylint: disable=protected-access
        except Exception as err:  # pylint: disable=broad-except
            info["errors"].append(getattr(err, "kind", type(err).__name__))
        for item in (found or {}).get("projects") or []:
            pid = str(item.get("id") or "")
            if config.valid_project(pid) and pid not in projects:
                projects.append(pid)
        info["truncated"] = bool((found or {}).get("truncated"))
        info["errors"] += list((found or {}).get("errors") or [])
    seen, out = set(), []
    for p in projects:
        if p not in seen:
            seen.add(p)
            out.append(p)
    if ctx.project in out:
        out.remove(ctx.project)
    out.insert(0, ctx.project)
    return out, info


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _guard(run, fn, *args):
    """Runs one category function; an unexpected exception becomes a skip check (never crashes the run)."""
    try:
        res = fn(run, *args)
        if res is None:
            return []
        return res if isinstance(res, list) else [res]
    except Exception as err:  # pylint: disable=broad-except
        LOG.warning("readiness category %s crashed: %s", getattr(fn, "__name__", "?"), type(err).__name__)
        return [check("internal.%s" % getattr(fn, "__name__", "category"), "config", "skip",
                      "내부 점검 오류", "Internal check error",
                      "The check could not complete (%s); other checks are unaffected." % type(err).__name__)]


def _surfaces(run, pid):
    checks, _ = surf_mod.surface_checks(run, pid)
    return checks


def _project_run(run, pid: str) -> dict:
    checks: list[dict] = []
    if run.wants("project"):
        checks += _guard(run, proj_mod.project_checks, pid)
    else:
        run.project_number(pid)
    exists = run.fact(pid, "exists")
    if exists is False:
        return _project_entry(run, pid, checks)
    if run.wants("apis"):
        checks += _guard(run, proj_mod.api_checks, pid)
    groups = []
    for cat, fn in (("orgpolicy", org_mod.orgpolicy_checks), ("vpcsc", net_mod.vpcsc_checks),
                    ("bigquery", data_mod.bigquery_checks), ("logging", data_mod.logging_checks),
                    ("surfaces", _surfaces), ("iam", iam_mod.iam_checks), ("quotas", iam_mod.quota_checks)):
        if run.wants(cat):
            groups.append((cat, fn))
    res = run.parallel({cat: (lambda f=fn: _guard(run, f, pid)) for cat, fn in groups})
    for cat, _ in groups:
        ok, value = res.get(cat, (False, None))
        checks += value if ok and isinstance(value, list) else []
    return _project_entry(run, pid, checks)


def _project_entry(run, pid: str, checks: list[dict]) -> dict:
    anc = run.fact(pid, "ancestry") or []
    parent = run.fact(pid, "parent") or ""
    ptype, pidv = "", ""
    if "/" in parent:
        ptype, pidv = parent.split("/", 1)
        ptype = {"folders": "folder", "organizations": "organization"}.get(ptype, ptype)
    elif len(anc) > 1:
        ptype, pidv = anc[1]["type"], anc[1]["id"]
    return {
        "project_id": pid,
        "project_number": run.fact(pid, "number") or "",
        "role": run.role(pid),
        "parent": {"type": ptype or None, "id": pidv or None},
        "organization_id": run.fact(pid, "org") or None,
        "checks": checks,
    }


def _config_checks(run, scope_info: dict) -> list[dict]:
    out = []
    if not config.workspace_configured(run.cfg):
        out.append(check("config.workspace", "config", "fail", "허브 프로젝트 설정", "Hub project configured",
                         "workspace.project_id (PROJECT_ID) is not set to a real project.", impact=["all"]))
    else:
        out.append(check("config.workspace", "config", "pass", "허브 프로젝트 설정", "Hub project configured",
                         "Hub %s governs %d managed project(s)." % (run.hub, len(run.projects) - 1),
                         data={"hub": run.hub, "managed": run.projects[1:][:500]}))
    if scope_info.get("scope_type") in ("folder", "organization"):
        status = "warn" if (scope_info.get("truncated") or scope_info.get("errors")) else "pass"
        out.append(check("config.scope", "config", status, "폴더/조직 스코프 탐색", "Folder/organization scope discovery",
                         "Discovered %d project(s)%s%s." % (
                             len(run.projects), ", truncated" if scope_info.get("truncated") else "",
                             (", errors: " + ", ".join(sorted(set(scope_info["errors"])))) if scope_info.get("errors")
                             else ""), data=scope_info))
    out.append(check("config.dataset_mode", "config", "info", "로그 데이터세트 구성", "Log dataset topology",
                     ("Central dataset %s:%s receives sinks from every managed project." % (run.dataset_project,
                                                                                            run.dataset))
                     if run.central else
                     ("Per-project datasets (%s in each project). The central hub dataset (--central-dataset, "
                      "AUDIT_PROJECT_ID=%s) is recommended for many projects." % (run.dataset, run.hub)),
                     data={"central": run.central, "dataset_project": run.dataset_project, "dataset": run.dataset}))
    if not (run.runtime_sa_configured and run.enforcer_sa_configured):
        out.append(check("config.service_accounts", "config", "info", "서비스 계정 설정", "Service accounts",
                         "Using installer default service accounts (runtime %s, enforcer %s)." % (
                             run.runtime_sa, run.enforcer_sa),
                         data={"runtime": run.runtime_sa, "enforcer": run.enforcer_sa}))
    if len(run.projects) >= MAX_PROJECTS:
        out.append(check("config.project_limit", "config", "warn", "점검 프로젝트 수 제한", "Project limit",
                         "Only the first %d projects were checked." % MAX_PROJECTS))
    return out


def _central_dataset_checks(run) -> list[dict]:
    """Central audit project outside the governed projects: dataset + location policy."""
    if not run.central or run.audit_project in run.projects:
        return []
    out = []
    if run.wants("bigquery"):
        item = data_mod.dataset_exists_check(run, run.audit_project)
        item["project"] = run.audit_project
        out.append(item)
    if run.wants("orgpolicy"):
        loc = run.fact(run.audit_project, "dataset_location") or run.region
        pol, _, err = run.effective_policy("projects/%s" % run.audit_project, "gcp.resourceLocations")
        title = ("중앙 데이터세트 위치 정책", "Central dataset location policy")
        if err is not None:
            item = check("orgpolicy.gcp.resourceLocations", "orgpolicy", "skip", *title,
                         "Could not read the location policy of %s." % run.audit_project)
        elif pol and pol.get("set") and loc_verdict(loc, pol) is False:
            item = check("orgpolicy.gcp.resourceLocations", "orgpolicy", "fail", *title,
                         "Dataset location %s is not allowed in %s." % (loc, run.audit_project), impact=["usage"])
        else:
            item = check("orgpolicy.gcp.resourceLocations", "orgpolicy", "pass", *title,
                         "Location %s allowed in %s." % (loc, run.audit_project))
        item["project"] = run.audit_project
        out.append(item)
    return out


def _cross_checks(run, scope_info: dict) -> list[dict]:
    out = []
    if run.wants("config"):
        out += _config_checks(run, scope_info)
    out += _central_dataset_checks(run)
    if run.wants("orgpolicy"):
        org = run.org_of(run.hub)
        out += _guard(run, org_mod.custom_constraints_check, org)
    if run.wants("vpcsc"):
        for pid in run.projects[1:]:
            out += _guard(run, net_mod.cross_project_check, pid)
        out += _guard(run, net_mod.denials_check)
    if run.wants("bigquery"):
        out += _guard(run, data_mod.location_consistency_check)
    if run.wants("enforcement"):
        out += _guard(run, iam_mod.deny_state_checks)
    if run.wants("iam"):
        out += _guard(run, iam_mod.scope_permission_checks)
    return out


def _capability(run, pid: str) -> dict:
    surfaces = run.fact(pid, "surfaces") or {}

    def surf(key):
        val = surfaces.get(key)
        return dict(val) if isinstance(val, dict) else {"available": None, "reason": "not_checked", "apps": [],
                                                         "locations": []}
    models_cfg = run.cfg.get("models") or {}
    granted = run.fact(pid, "runtime_granted")
    org_writable = None
    if granted is not None:
        org_writable = {"orgpolicy.policies.create", "orgpolicy.policies.update"} <= set(granted)
    if run.api_enabled(pid, "orgpolicy.googleapis.com") is False or not models_cfg.get("allow_org_policy_writes", True):
        org_writable = False
    if run.fact(pid, "org") == "" and run.fact(pid, "ancestry") is not None:
        org_writable = False if org_writable is None else org_writable
    deny = None
    enf = run.fact(pid, "enforcer_granted")
    if run.enforcement_backend != "iam_deny":
        deny = False
    elif enf is not None:
        deny = {"iam.denypolicies.create", "iam.denypolicies.update"} <= set(enf)
    if run.fact(pid, "ancestry") is not None and not run.fact(pid, "org"):
        deny = False
    quotas_api = run.fact(pid, "quotas_api")
    if run.api_enabled(pid, "cloudquotas.googleapis.com") is False:
        quotas_api = False
    return {
        "project_id": pid,
        "role": run.role(pid),
        "gemini_platform": surf("gemini_platform"),
        "gemini_enterprise": surf("gemini_enterprise"),
        "antigravity": surf("antigravity"),
        "org_policy_writable": org_writable,
        "quotas_api": quotas_api,
        "deny_writable": deny,
        "allowed_models_policy": run.fact(pid, "allowed_models"),
    }


def _all_checks(report: dict):
    for proj in report.get("projects") or []:
        for c in proj.get("checks") or []:
            yield proj["project_id"], c
    for c in report.get("cross_project") or []:
        yield c.get("project") or "", c


def summarize(report: dict) -> dict:
    counts = {s: 0 for s in ("pass", "warn", "fail", "skip", "info")}
    blocking = unfixable = 0
    for _, c in _all_checks(report):
        counts[c["status"]] = counts.get(c["status"], 0) + 1
        if c["status"] == "fail" and c.get("blocking", True):
            blocking += 1
            if not c.get("auto_fixable"):
                unfixable += 1
    counts["total"] = sum(counts[s] for s in ("pass", "warn", "fail", "skip", "info"))
    counts["blocking"] = blocking
    counts["blocking_unfixable"] = unfixable
    return counts


def overall_of(summary: dict) -> str:
    if summary.get("blocking"):
        return "blocked"
    if summary.get("warn") or summary.get("fail"):
        return "ready_with_warnings"
    return "ready"


def remediation_script(report: dict) -> str:
    """Bash script with every fix command of failed/warned checks (deduplicated, grouped by check)."""
    lines = ["#!/usr/bin/env bash",
             "# Remediation commands generated by the GCP AI Usage Caps API readiness preflight (%s)." %
             report.get("generated_at", ""),
             "# Review before running. Lines starting with '#' are manual steps (perimeter/org admin).",
             "set -euo pipefail", ""]
    seen = set()
    items = sorted(_all_checks(report), key=lambda pc: (STATUS_ORDER.get(pc[1]["status"], 9), pc[1]["id"], pc[0]))
    for project, c in items:
        if c["status"] not in ("fail", "warn"):
            continue
        cmds = [cmd for cmd in (c.get("remediation") or {}).get("commands") or [] if cmd not in seen]
        if not cmds:
            continue
        lines.append("# [%s] %s%s - %s" % (c["status"].upper(), c["id"], (" (%s)" % project) if project else "",
                                            c.get("title_en", "")))
        for cmd in cmds:
            seen.add(cmd)
            lines.append(cmd)
        lines.append("")
    if len(lines) == 5:
        lines.append("# Nothing to fix.")
    return "\n".join(lines).rstrip() + "\n"


def _execute(ctx, projects: list[str] | None, categories: list[str] | None, *, include_hub: bool = True) -> dict:
    started = time.monotonic()
    cats = list(categories or CATEGORIES)
    bad = [c for c in cats if c not in CATEGORIES]
    if bad:
        raise ApiError("bad_request", "Unknown readiness categories: %s (valid: %s)." % (
            ", ".join(bad[:5]), ", ".join(CATEGORIES)))
    all_projects, scope_info = resolve_projects(ctx)
    if projects:
        wanted = []
        for p in projects:
            p = str(p or "").strip().lower()
            if not config.valid_project(p):
                raise ApiError("bad_request", "Invalid project id in 'projects'.")
            if p not in all_projects:
                raise ApiError("bad_request", "Project '%s' is not in the configured workspace scope." % p)
            if p not in wanted:
                wanted.append(p)
        plist = ([ctx.project] if include_hub and ctx.project not in wanted else []) + wanted
    else:
        plist = all_projects
    plist = plist[:MAX_PROJECTS]
    run = Run(ctx, plist, set(cats))
    if run.wants("surfaces"):
        vertex_models = _guard(run, surf_mod.vertex_models_check)
    else:
        vertex_models = []
    res = run.parallel({pid: (lambda p=pid: _project_run(run, p)) for pid in plist})
    entries = []
    for pid in plist:
        ok, value = res.get(pid, (False, None))
        entries.append(value if ok and isinstance(value, dict) else _project_entry(run, pid, [
            check("internal.project", "config", "skip", "내부 점검 오류", "Internal check error",
                  "Project checks could not complete (%s)." % type(value).__name__)]))
    hub_entry = next((e for e in entries if e["project_id"] == run.hub), None)
    if hub_entry is not None:
        if run.wants("network"):
            hub_entry["checks"] += _guard(run, net_mod.network_checks)
        if run.wants("enforcement"):
            hub_entry["checks"] += _guard(run, iam_mod.hub_resource_checks)
        hub_entry["checks"] += vertex_models
    cross = _cross_checks(run, scope_info)
    org = run.org_of(run.hub) if run.hub in plist else ""
    report = {
        "object": "readiness_report",
        "generated_at": now_iso(),
        "categories": cats,
        "hub": {
            "project_id": run.hub,
            "project_number": run.fact(run.hub, "number") or "",
            "region": run.region,
            "service_name": run.service_name,
            "runtime_service_account": run.runtime_sa,
            "enforcer_service_account": run.enforcer_sa,
            "organization_id": org or None,
            "audit_dataset": run.dataset,
            "audit_project": run.audit_project or None,
            "dataset_mode": "central" if run.central else "per_project",
            "vpc_network": run.vpc_network or None,
            "vpc_subnet": run.vpc_subnet or None,
            "identity_mode": run.identity_mode,
            "managed_project_count": max(0, len(plist) - 1),
        },
        "projects": entries,
        "cross_project": cross,
        "capabilities": {pid: _capability(run, pid) for pid in plist},
        "adaptations": dict(run.adaptations),
        "scope": scope_info,
    }
    report["summary"] = summarize(report)
    report["overall"] = overall_of(report["summary"])
    report["remediation_script"] = remediation_script(report)
    report["duration_ms"] = int((time.monotonic() - started) * 1000)
    LOG.info("readiness run projects=%d overall=%s pass=%d warn=%d fail=%d skip=%d duration_ms=%d",
             len(plist), report["overall"], report["summary"]["pass"], report["summary"]["warn"],
             report["summary"]["fail"], report["summary"]["skip"], report["duration_ms"])
    return report


def run_readiness(ctx, *, projects: list[str] | None = None, categories: list[str] | None = None) -> dict:
    """Runs the readiness checks and returns the report (does not persist it)."""
    return _execute(ctx, projects, categories)


# ---------------------------------------------------------------------------
# Cache & HTTP handlers
# ---------------------------------------------------------------------------

def _ttl(ctx) -> int:
    try:
        return int((ctx.config.get("readiness") or {}).get("cache_ttl_seconds") or DEFAULT_CACHE_TTL)
    except (TypeError, ValueError):
        return DEFAULT_CACHE_TTL


def _store_report(ctx, report: dict):
    def mutate(doc):
        doc.clear()
        doc["report"] = report
        doc["stored_at"] = time.time()
    try:
        ctx.store.mutate(DOC_NAME, mutate)
    except Exception as err:  # pylint: disable=broad-except
        LOG.warning("readiness report not cached: %s", type(err).__name__)
        ctx.warn("store", "cache_failed", "The readiness report could not be cached.")


def _cached(ctx, max_age: float | None = None):
    try:
        doc, _ = ctx.store.load(DOC_NAME)
    except Exception:  # pylint: disable=broad-except
        return None, None
    report = doc.get("report") if isinstance(doc, dict) else None
    stored = float(doc.get("stored_at") or 0) if isinstance(doc, dict) else 0.0
    if not isinstance(report, dict):
        return None, None
    age = time.time() - stored
    if max_age is not None and age > max_age:
        return None, age
    return report, age


def filter_report(report: dict, project: str | None) -> dict:
    """Restricts a report to one project (projects, project-scoped cross checks, capabilities)."""
    if not project:
        return report
    out = dict(report)
    out["projects"] = [p for p in report.get("projects") or [] if p.get("project_id") == project]
    out["cross_project"] = [c for c in report.get("cross_project") or [] if c.get("project") in (None, "", project)]
    out["capabilities"] = {k: v for k, v in (report.get("capabilities") or {}).items() if k == project}
    out["summary"] = summarize(out)
    out["overall"] = overall_of(out["summary"])
    out["remediation_script"] = remediation_script(out)
    return out


def get_readiness(ctx) -> dict:
    """GET /v1/admin/readiness — cached report when fresher than readiness.cache_ttl_seconds, else a new run.

    Query: refresh=true forces a run; project=P filters the report to one project."""
    project = (ctx.params.get("project") or "").strip().lower() or None
    if project and not config.valid_project(project):
        raise ApiError("bad_request", "Invalid 'project' parameter.")
    refresh = str(ctx.params.get("refresh") or "").strip().lower() in _TRUE
    report, age = (None, None) if refresh else _cached(ctx, _ttl(ctx))
    if report is None:
        report = _execute(ctx, None, None)
        _store_report(ctx, report)
        cached, age = False, 0.0
    else:
        cached = True
    if project and project not in [p.get("project_id") for p in report.get("projects") or []]:
        raise ApiError("bad_request", "Project '%s' is not in the configured workspace scope." % project)
    out = filter_report(report, project)
    out = dict(out)
    out["cached"] = cached
    out["cache_age_seconds"] = int(age or 0)
    return out


def run_readiness_handler(ctx) -> dict:
    """POST /v1/admin/readiness/run — body {projects?: [..], categories?: [..]}; full runs refresh the cache."""
    body = ctx.body or {}
    projects = body.get("projects")
    categories = body.get("categories")
    for name, val in (("projects", projects), ("categories", categories)):
        if val is not None and (not isinstance(val, list) or not all(isinstance(v, str) for v in val) or len(val) > 500):
            raise ApiError("bad_request", "'%s' must be a list of strings." % name)
    report = _execute(ctx, projects or None, categories or None)
    if not projects and not categories:
        _store_report(ctx, report)
    out = dict(report)
    out["cached"] = False
    out["cache_age_seconds"] = 0
    return out


def capabilities(ctx, project_id: str) -> dict:
    """Per-surface availability for one project, from the cached report or lightweight probes.

    Returns {object: 'capabilities', project_id, generated_at, source: 'cache'|'probe',
             gemini_platform|gemini_enterprise|antigravity: {available: bool|None, reason, apps: [...], locations: [...]},
             org_policy_writable: bool|None, quotas_api: bool|None, deny_writable: bool|None,
             allowed_models_policy: {...}|None}"""
    pid = str(project_id or "").strip().lower()
    if not config.valid_project(pid):
        raise ApiError("bad_request", "Invalid project id.")
    report, _ = _cached(ctx, None)
    if report and pid in (report.get("capabilities") or {}):
        cap = dict(report["capabilities"][pid])
        cap.update({"object": "capabilities", "generated_at": report.get("generated_at"), "source": "cache"})
        return cap

    def probe(c):
        rep = _execute(c, [pid], list(LIGHT_CATEGORIES), include_hub=pid == c.project)
        return rep.get("generated_at"), (rep.get("capabilities") or {}).get(pid)
    generated, cap = ctx.memo("readiness_capabilities", pid, probe, ttl=float(_ttl(ctx)))
    cap = dict(cap or {"project_id": pid})
    cap.update({"object": "capabilities", "generated_at": generated, "source": "probe"})
    return cap
