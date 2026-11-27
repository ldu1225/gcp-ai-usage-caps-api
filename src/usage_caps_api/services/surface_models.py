"""Surface-level model controls: Gemini Enterprise app model selection and Antigravity authorized models.

Gemini Enterprise (Discovery Engine v1, read & write):
    GET   https://{host}/v1/projects/{p}/locations/{l}/collections/default_collection/engines
    PATCH https://{host}/v1/{engine name}?updateMask=modelConfigs
  `modelConfigs` is a map {modelId: MODEL_ENABLED | MODEL_DISABLED}. With updateMask=modelConfigs the request map
  REPLACES the whole map, so every change re-reads the engine and merges only the requested keys (concurrent console
  edits are preserved). Projects without any Gemini Enterprise app report `not_applicable` instead of failing.

Antigravity / AI Developer Tools (Discovery Engine v1alpha, read-only):
    GET https://{host}/v1alpha/projects/{p}/locations/{l}/devToolsConfig  -> modelConfig.allowedModels
  Google Cloud currently restricts UpdateDevToolsConfig to the Cloud Console, so writes return 409 `console_required`
  with a console deep link. Per-user Antigravity model control is enforced by per-user model rules (detect + block).
"""

from __future__ import annotations

import logging
import re

from .. import config
from ..gcp import GcpError
from ..httpkit import ApiError, now_iso, paginate_list
from . import audit, operations, webhooks
from .base import discovery_host

LOG = logging.getLogger("usage_caps_api.surface_models")

COLLECTION = "default_collection"
MODEL_ON, MODEL_OFF = "MODEL_ENABLED", "MODEL_DISABLED"
MODEL_STATE_OUT = {MODEL_ON: "on", MODEL_OFF: "off"}
MODEL_STATE_IN = {"on": MODEL_ON, "off": MODEL_OFF}
MODEL_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}\Z")
ENGINE_NAME_RE = re.compile(r"^projects/([A-Za-z0-9\-]+)/locations/([a-z0-9\-]+)/collections/([A-Za-z0-9_\-]+)/"
                            r"engines/([A-Za-z0-9][A-Za-z0-9_\-]{0,62})\Z")
ENGINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,62}\Z")
INTRANET = "APP_TYPE_INTRANET"
DEV_TOOLS_LOCATIONS = ("global", "us", "eu")
MAX_MODELS_PER_PATCH = 64
CONSOLE_DEVTOOLS_URL = "https://console.cloud.google.com/gemini-enterprise?project=%s"


def ge_locations(ctx) -> list[str]:
    locs = list(ctx.ws.get("discovery_engine_locations") or ["global"])
    for loc in DEV_TOOLS_LOCATIONS:
        if loc not in locs:
            locs.append(loc)
    return [loc for loc in locs if config.LOCATION_RE.match(loc or "")]


def _clean_models(raw) -> dict[str, str]:
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(k, str) and MODEL_KEY_RE.match(k) and v in MODEL_STATE_OUT:
                out[k] = MODEL_STATE_OUT[v]
    return out


def _is_ge_app(engine: dict) -> bool:
    return isinstance(engine, dict) and (engine.get("appType") == INTRANET or "modelConfigs" in engine
                                         or "features" in engine)


def _app_view(engine: dict, project: str, location: str) -> dict | None:
    m = ENGINE_NAME_RE.match(engine.get("name") or "")
    if not m:
        return None
    engine_id = m.group(4)
    return {
        "object": "gemini_enterprise_app",
        "id": "%s/%s/%s" % (project, location, engine_id),
        "path_id": "%s~%s~%s" % (project, location, engine_id),
        "project_id": project,
        "location": location,
        "engine_id": engine_id,
        "display_name": str(engine.get("displayName") or engine_id)[:200],
        "models": _clean_models(engine.get("modelConfigs")),
        "update_time": engine.get("updateTime"),
    }


def list_ge_apps(ctx, projects: list[str] | None = None) -> tuple[list[dict], dict]:
    """Lists Gemini Enterprise apps across projects/locations. Returns (apps, per-project status)."""
    projects = projects or ctx.all_projects
    tasks = {}
    for p in projects:
        for loc in ge_locations(ctx):
            url = "%s/v1/projects/%s/locations/%s/collections/%s/engines" % (discovery_host(loc), p, loc, COLLECTION)
            tasks[(p, loc)] = (lambda u=url: ctx.gcp.paginate(u, quota_project=ctx.quota_project, items_key="engines",
                                                              max_pages=5))
    outcomes = ctx.run(tasks) if tasks else {}
    apps, status = [], {}
    for (p, loc), (ok, val) in outcomes.items():
        st = status.setdefault(p, {"apps": 0, "errors": {}})
        if not ok:
            kind = getattr(val, "kind", "error")
            if kind != "not_found":
                st["errors"][loc] = kind
            continue
        items = val[0] if isinstance(val, tuple) else val
        for eng in items or []:
            if _is_ge_app(eng):
                view = _app_view(eng, p, loc)
                if view:
                    apps.append(view)
                    st["apps"] += 1
    for p, st in status.items():
        st["available"] = st["apps"] > 0 if not st["errors"] or st["apps"] else None
    apps.sort(key=lambda a: a["id"])
    return apps, status


def ge_model_states(ctx, project: str) -> dict:
    """{app_id: {model: on|off}} for one project (cached briefly) or {} when the project has no GE app."""
    def load(c):
        apps, _ = list_ge_apps(c, [project])
        return {a["id"]: a["models"] for a in apps}
    return ctx.memo("ge_models", project, load, ttl=60)


def _parse_app_id(raw: str) -> tuple[str, str, str]:
    parts = str(raw or "").split("~") if "~" in str(raw or "") else str(raw or "").split("/")
    if len(parts) != 3:
        raise ApiError("bad_request", "App id must be 'PROJECT~LOCATION~ENGINE_ID' (or PROJECT/LOCATION/ENGINE_ID).")
    project, location, engine_id = (p.strip() for p in parts)
    if not config.valid_project(project.lower()) or not config.LOCATION_RE.match(location) or not ENGINE_ID_RE.match(engine_id):
        raise ApiError("bad_request", "Invalid Gemini Enterprise app id.")
    return project.lower(), location, engine_id


def _require_managed(ctx, project: str):
    if project not in ctx.all_projects:
        raise ApiError("forbidden", "Project '%s' is not managed by this control plane." % project)


def list_apps_handler(ctx) -> dict:
    """GET /v1/gemini-enterprise/apps?project="""
    project = (ctx.params.get("project") or "").strip().lower()
    projects = [project] if project else None
    if project:
        _require_managed(ctx, project)
    apps, status = list_ge_apps(ctx, projects)
    page = paginate_list(apps, ctx.params, default_limit=100, max_limit=500)
    page["projects"] = {p: {"available": s.get("available"), "apps": s["apps"], "errors": s["errors"],
                            "status": "available" if s.get("available") else
                            ("not_applicable" if s.get("available") is False else "unknown")}
                        for p, s in status.items()}
    return page


def _read_engine(ctx, project: str, location: str, engine_id: str) -> dict:
    name = "projects/%s/locations/%s/collections/%s/engines/%s" % (project, location, COLLECTION, engine_id)
    try:
        engine = ctx.rest("GET", "%s/v1/%s" % (discovery_host(location), name), timeout=30) or {}
    except GcpError as err:
        if err.kind == "not_found":
            raise ApiError("not_found", "Gemini Enterprise app '%s' not found." % engine_id)
        raise ApiError("upstream_error", "Could not read Gemini Enterprise app (%s)." % err.kind)
    m = ENGINE_NAME_RE.match(engine.get("name") or "")
    if not m or m.group(2) != location or m.group(4) != engine_id:
        raise ApiError("not_found", "Gemini Enterprise app '%s' not found." % engine_id)
    return engine


def get_app_models(ctx) -> dict:
    """GET /v1/gemini-enterprise/apps/{app_id}/models"""
    project, location, engine_id = _parse_app_id(ctx.path_params.get("app_id"))
    _require_managed(ctx, project)
    engine = _read_engine(ctx, project, location, engine_id)
    view = _app_view(engine, project, location) or {}
    return dict(view, object="gemini_enterprise_app_models")


def merge_models(current: dict, updates: dict) -> dict:
    merged = {k: v for k, v in (current or {}).items() if isinstance(k, str) and v in MODEL_STATE_OUT}
    for key, value in updates.items():
        if value == "default":
            merged.pop(key, None)
        else:
            merged[key] = MODEL_STATE_IN[value]
    return merged


def _clean_updates(raw) -> dict[str, str]:
    if not isinstance(raw, dict) or not raw:
        raise ApiError("bad_request", "Field 'models' must be a non-empty object {modelId: on|off|default}.")
    if len(raw) > MAX_MODELS_PER_PATCH:
        raise ApiError("bad_request", "At most %d model changes per request." % MAX_MODELS_PER_PATCH)
    out = {}
    for k, v in raw.items():
        key = str(k or "").strip().lower()
        val = str(v or "").strip().lower()
        if not MODEL_KEY_RE.match(key):
            raise ApiError("bad_request", "Invalid model id '%s'." % k)
        if val not in ("on", "off", "default"):
            raise ApiError("bad_request", "Model state for '%s' must be on, off or default." % key)
        out[key] = val
    return out


def apply_app_models(ctx, project: str, location: str, engine_id: str, updates: dict, *, dry_run: bool) -> dict:
    engine = _read_engine(ctx, project, location, engine_id)
    before_raw = engine.get("modelConfigs") if isinstance(engine.get("modelConfigs"), dict) else {}
    before = {k: v for k, v in before_raw.items() if v in MODEL_STATE_OUT}
    after = merge_models(before_raw, updates)
    result = {
        "object": "gemini_enterprise_model_change",
        "app_id": "%s/%s/%s" % (project, location, engine_id),
        "before": _clean_models(before),
        "after": _clean_models(after),
        "dry_run": dry_run,
    }
    if after == before:
        return dict(result, status="unchanged")
    if dry_run:
        return dict(result, status="dry_run")
    try:
        updated = ctx.rest("PATCH", "%s/v1/%s" % (discovery_host(location), engine["name"]),
                           params={"updateMask": "modelConfigs"}, json_body={"modelConfigs": after}, timeout=45) or {}
    except GcpError as err:
        if err.kind == "denied":
            raise ApiError("forbidden", "The service account lacks discoveryengine.engines.update on '%s'." % project)
        raise ApiError("upstream_error", "Gemini Enterprise update failed (%s)." % err.kind)
    models = updated.get("modelConfigs") if isinstance(updated.get("modelConfigs"), dict) else after
    return dict(result, status="updated", after=_clean_models(models))


def _parse_dry_run(body: dict) -> bool:
    raw = body.get("dryRun", body.get("dry_run", False))
    if not isinstance(raw, bool):
        raise ApiError("bad_request", "Field 'dryRun' must be a boolean.")
    return raw


def patch_app_models(ctx) -> dict:
    """PATCH /v1/gemini-enterprise/apps/{app_id}/models {models: {id: on|off|default}, dryRun?}"""
    project, location, engine_id = _parse_app_id(ctx.path_params.get("app_id"))
    _require_managed(ctx, project)
    body = ctx.body or {}
    updates = _clean_updates(body.get("models"))
    dry_run = _parse_dry_run(body)
    res = apply_app_models(ctx, project, location, engine_id, updates, dry_run=dry_run)
    if res["status"] == "updated":
        if getattr(ctx.app, "cache", None) is not None:
            ctx.app.cache.clear("ge_models")
        op = operations.create(ctx, "ge_models.update", target=res["app_id"],
                               detail="Gemini Enterprise app model selection updated.", result={"after": res["after"]})
        res["operation"] = op
        audit.write_audit(ctx, "ge_models.updated", target=res["app_id"],
                          details={"changes": updates, "before": res["before"], "after": res["after"]})
        webhooks.dispatch_event(ctx, "model.access_changed", {
            "surface": "gemini_enterprise", "target": res["app_id"], "changes": updates, "actor": ctx.caller.id,
        })
    return res


def bulk_patch_app_models(ctx) -> dict:
    """POST /v1/gemini-enterprise/apps/bulk-models {apps: ["*"] | [app ids], models: {...}, dryRun?}"""
    body = ctx.body or {}
    updates = _clean_updates(body.get("models"))
    dry_run = _parse_dry_run(body)
    raw_targets = body.get("apps")
    if raw_targets is None:
        targets = ["*"]
    else:
        if not isinstance(raw_targets, list) or not raw_targets:
            raise ApiError("bad_request", "Field 'apps' must be a non-empty list of app ids or ['*'].")
        targets = raw_targets
    if "*" in targets:
        apps, _ = list_ge_apps(ctx)
        ids = [(a["project_id"], a["location"], a["engine_id"]) for a in apps]
    else:
        ids = [_parse_app_id(t) for t in targets]
        for p, _, _ in ids:
            _require_managed(ctx, p)
    tasks = {i: (lambda i=i: apply_app_models(ctx, i[0], i[1], i[2], updates, dry_run=dry_run)) for i in ids}
    outcomes = ctx.run(tasks) if tasks else {}
    results = []
    for i in ids:
        ok, val = outcomes.get(i, (False, None))
        if ok:
            results.append(val)
        else:
            results.append({"app_id": "/".join(i), "status": "failed",
                            "error": getattr(val, "code", getattr(val, "kind", "error"))})
    counts = {s: sum(1 for r in results if r.get("status") == s) for s in ("updated", "unchanged", "dry_run", "failed")}
    if counts["updated"]:
        if getattr(ctx.app, "cache", None) is not None:
            ctx.app.cache.clear("ge_models")
        audit.write_audit(ctx, "ge_models.bulk_updated", target="*", details={"changes": updates, **counts})
        webhooks.dispatch_event(ctx, "model.access_changed", {
            "surface": "gemini_enterprise", "target": "bulk", "changes": updates, "apps": counts["updated"],
            "actor": ctx.caller.id,
        })
    return {"object": "bulk_result", "results": results, **counts}


# ---------------------------------------------------------------------------
# Antigravity authorized models (read-only)
# ---------------------------------------------------------------------------

def _clean_model_list(raw) -> list[str]:
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str) and MODEL_KEY_RE.match(item.lower()) and item.lower() not in out:
            out.append(item.lower())
    return out


def agy_config(ctx, project: str) -> dict:
    """{location: {allowed_models, allowed_third_party_models, dev_tools_enabled}} (cached briefly)."""
    def load(c):
        tasks = {}
        for loc in DEV_TOOLS_LOCATIONS:
            url = "%s/v1alpha/projects/%s/locations/%s/devToolsConfig" % (discovery_host(loc), project, loc)
            tasks[loc] = (lambda u=url: c.rest("GET", u, timeout=15))
        out = {}
        for loc, (ok, val) in c.run(tasks).items():
            if not ok:
                out[loc] = {"available": None if getattr(val, "kind", "") != "not_found" else False,
                            "error": getattr(val, "kind", "error")}
                continue
            mcfg = (val or {}).get("modelConfig") if isinstance((val or {}).get("modelConfig"), dict) else {}
            out[loc] = {
                "available": True,
                "dev_tools_enabled": bool((val or {}).get("devToolsEnabled")),
                "allowed_models": _clean_model_list(mcfg.get("allowedModels")),
                "allowed_third_party_models": _clean_model_list(mcfg.get("allowedThirdPartyModels")),
            }
        return out
    return ctx.memo("agy_models", project, load, ttl=120)


def agy_models_handler(ctx) -> dict:
    """GET /v1/antigravity/models?project="""
    project = (ctx.params.get("project") or "").strip().lower()
    projects = [project] if project else ctx.all_projects
    if project:
        _require_managed(ctx, project)
    data = []
    for p in projects[:100]:
        cfg = agy_config(ctx, p)
        for loc, entry in sorted(cfg.items()):
            if entry.get("available") is False:
                continue
            allowed = entry.get("allowed_models") or []
            data.append({
                "object": "antigravity_model_config",
                "project_id": p,
                "location": loc,
                "available": entry.get("available"),
                "mode": ("allow_list" if allowed else "product_default") if entry.get("available") else "unknown",
                "allowed_models": allowed,
                "allowed_third_party_models": entry.get("allowed_third_party_models") or [],
                "write_mode": "console_required",
                "console_url": CONSOLE_DEVTOOLS_URL % p,
                "error": entry.get("error"),
            })
    page = paginate_list(data, ctx.params, default_limit=100, max_limit=500)
    page["note"] = ("Antigravity authorized models are read-only via API (Google Cloud restricts UpdateDevToolsConfig "
                    "to the Cloud Console). Use per-user model rules (PUT /v1/users/{email}/model-rules) for API-driven "
                    "enforcement, which detects disallowed model use and blocks the user's Antigravity access.")
    return page


def agy_models_write_handler(ctx) -> dict:
    """PUT /v1/antigravity/models -> 409 console_required (explicit, documented behaviour)."""
    raw_project = ctx.params.get("project") if ctx.params.get("project") is not None else (ctx.body or {}).get("project")
    if raw_project is None or raw_project == "":
        project = ctx.project
    else:
        if not isinstance(raw_project, str) or not config.valid_project(raw_project.strip().lower()):
            raise ApiError("bad_request", "Field 'project' must be a valid GCP project ID.")
        project = raw_project.strip().lower()
    raise ApiError("console_required",
                   "Antigravity authorized models can only be changed in the Google Cloud Console (%s). "
                   "For API-driven control use per-user model rules." % (CONSOLE_DEVTOOLS_URL % project))


def ge_capability(ctx, project: str) -> dict:
    states = ge_model_states(ctx, project)
    return {"available": bool(states), "apps": sorted(states.keys())}


def stamp() -> str:
    return now_iso()
