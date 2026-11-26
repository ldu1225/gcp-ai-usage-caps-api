"""Gemini Platform inference request metadata logging (`aiplatform.googleapis.com/inference_request`) per managed project.

This log is the only log-based source of per-request Gemini Platform token counts (Gemini `generateContent` /
`streamGenerateContent` and Claude `rawPredict` / `streamRawPredict`). It is OFF by default and has no organization-
level switch, so the hub enables it project by project (v1beta1 `setPublisherModelConfig` with
`inferenceEventLoggingConfig.state = ENABLED`; reads use `fetchPublisherModelConfig`).

Required permissions on each managed project: `aiplatform.endpoints.setPublisherModelConfig` and
`aiplatform.endpoints.fetchPublisherModelConfig` (e.g. roles/aiplatform.admin, or a custom role).
Entries are ingested into Cloud Logging (Cloud Logging ingestion/storage pricing applies) and routed to the hub's
BigQuery dataset / Pub/Sub topic by the sinks created by scripts/prepare.sh.
"""

from __future__ import annotations

import logging
import urllib.parse

from .. import config
from ..gcp import GcpError
from ..httpkit import ApiError, now_iso
from . import audit

LOG = logging.getLogger("usage_caps_api.vertex_logging")

V1BETA1 = "https://aiplatform.googleapis.com/v1beta1"


def _endpoint(ctx) -> str:
    raw = str(((ctx.config.get("caps") or {}).get("vertex_logging_endpoint")) or V1BETA1).rstrip("/")
    return raw if raw.startswith("https://") and raw.endswith("/v1beta1") else V1BETA1


def _state_of(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    for cand in (payload, payload.get("publisherModelConfig") or {}, payload.get("config") or {}):
        cfg = cand.get("inferenceEventLoggingConfig") if isinstance(cand, dict) else None
        if isinstance(cfg, dict) and cfg.get("state"):
            return str(cfg["state"]).upper()
    return None


def status(ctx, project: str) -> dict:
    if not config.valid_project(project):
        raise ApiError("bad_request", "Invalid project id.")
    url = "%s/projects/%s:fetchPublisherModelConfig" % (_endpoint(ctx), urllib.parse.quote(project, safe=""))
    try:
        payload = ctx.rest("GET", url, timeout=20)
    except GcpError as err:
        if err.kind == "not_found":
            # Fresh projects return 404 until setPublisherModelConfig is called for the first time.
            return {"project": project, "enabled": False, "state": "STATE_UNSPECIFIED",
                    "readable": True, "error": None}
        return {"project": project, "enabled": None, "state": None, "readable": False, "error": err.kind}
    state = _state_of(payload)
    return {"project": project, "enabled": state == "ENABLED", "state": state or "STATE_UNSPECIFIED",
            "readable": True, "error": None}


def enable(ctx, project: str, *, dry_run: bool = False) -> dict:
    cur = status(ctx, project)
    if cur.get("enabled"):
        return dict(cur, action="none", detail="Inference request metadata logging already enabled.")
    if dry_run:
        return dict(cur, action="would_enable")
    url = "%s/projects/%s:setPublisherModelConfig" % (_endpoint(ctx), urllib.parse.quote(project, safe=""))
    body = {"publisherModelConfig": {"inferenceEventLoggingConfig": {"state": "ENABLED"}}}
    try:
        ctx.rest("POST", url, json_body=body, timeout=30)
    except GcpError as err:
        if err.kind == "conflict":   # 409 ALREADY_EXISTS on identical writes
            return dict(cur, enabled=True, state="ENABLED", action="none", detail="Already enabled (409).")
        return dict(cur, action="failed", error=err.kind)
    audit.write_audit(ctx, "vertex_logging.enabled", target=project, details={"at": now_iso()})
    return dict(cur, enabled=True, state="ENABLED", action="enabled", error=None,
                note="Takes a few minutes to take effect; entries appear within ~12 s (up to 5 min) of a request.")


def get_handler(ctx) -> dict:
    """GET /v1/projects/{project_id}/vertex-logging"""
    project = (ctx.path_params.get("project_id") or "").strip().lower()
    if project not in ctx.all_projects:
        raise ApiError("not_found", "Project '%s' is not a managed project of this hub." % project)
    return dict({"object": "vertex_inference_logging"}, **status(ctx, project))


def enable_handler(ctx) -> dict:
    """POST /v1/projects/{project_id}/vertex-logging/enable {dryRun?: bool}"""
    project = (ctx.path_params.get("project_id") or "").strip().lower()
    if project not in ctx.all_projects:
        raise ApiError("not_found", "Project '%s' is not a managed project of this hub." % project)
    dry_run = (ctx.body or {}).get("dryRun", False)
    if not isinstance(dry_run, bool):
        raise ApiError("bad_request", "Field 'dryRun' must be a boolean.")
    return dict({"object": "vertex_inference_logging"}, **enable(ctx, project, dry_run=dry_run))


def bulk_enable_handler(ctx) -> dict:
    """POST /v1/vertex-logging/enable {projects?: ["*"] | [ids], dryRun?: bool}"""
    body = ctx.body or {}
    dry_run = body.get("dryRun", True)
    if not isinstance(dry_run, bool):
        raise ApiError("bad_request", "Field 'dryRun' must be a boolean.")
    raw = body.get("projects") or ["*"]
    if not isinstance(raw, list):
        raise ApiError("bad_request", "Field 'projects' must be a list.")
    managed = list(ctx.all_projects)
    targets = managed if "*" in raw else [p for p in (str(x).strip().lower() for x in raw) if p in managed]
    unknown = [] if "*" in raw else [x for x in raw if str(x).strip().lower() not in managed]
    cap = int((ctx.config.get("quotas") or {}).get("max_projects_per_call") or 50)
    results = {}
    tasks = {p: (lambda pr=p: enable(ctx, pr, dry_run=dry_run)) for p in targets[:cap]}
    for p, (ok, val) in (ctx.run(tasks) if tasks else {}).items():
        results[p] = val if ok else {"project": p, "action": "failed", "error": getattr(val, "kind", "error")}
    return {"object": "vertex_inference_logging_bulk", "dry_run": dry_run,
            "data": [results[p] for p in targets[:cap] if p in results],
            "skipped_over_limit": targets[cap:], "unknown_projects": unknown}
