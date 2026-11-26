"""Readiness category `surfaces`: which AI surfaces exist per project.

- Gemini Enterprise: Discovery Engine engines (apps) in the configured Discovery Engine locations plus global/us/eu.
  No app => `gemini_enterprise.available=false` (info, not a failure: GE features answer not_applicable).
- Antigravity (AI developer tools): Discovery Engine v1alpha `devToolsConfig` in global/us/eu (+ configured locations)
  and recent `businessaicode.googleapis.com/inference_response` logs (Metadata logging on).
- Vertex AI: aiplatform API state per project, and Model Garden publisher models reachability (global with regional
  fallback) once per run.
"""

from __future__ import annotations

import datetime as dt
import urllib.parse

from .. import config
from .base import discovery_host
from .readiness_core import AIPLATFORM, DOCS, LOGGING_V2, check, disabled_service, reason_of

DEFAULT_GE_LOCATIONS = ("global", "us", "eu")
LOG_LOOKBACK_DAYS = 7


def _locations(run) -> list[str]:
    locs = list(DEFAULT_GE_LOCATIONS)
    for loc in run.ws.get("discovery_engine_locations") or []:
        loc = str(loc or "").lower()
        if config.LOCATION_RE.match(loc) and loc not in locs:
            locs.append(loc)
    return locs


def _soft_absent(err) -> bool:
    """Errors meaning 'nothing here' rather than 'cannot tell'."""
    return getattr(err, "kind", "") in ("not_found", "invalid") or bool(disabled_service(err))


def gemini_enterprise(run, pid: str) -> tuple[dict, dict]:
    """(check, capability) for Gemini Enterprise in `pid`."""
    title = ("Gemini Enterprise 앱", "Gemini Enterprise apps")
    cid = "surface.gemini_enterprise"
    locs = _locations(run)
    if run.api_enabled(pid, "discoveryengine.googleapis.com") is False:
        cap = {"available": False, "reason": "api_disabled", "apps": [], "locations": []}
        return check(cid, "surfaces", "info", *title,
                     "discoveryengine.googleapis.com is not enabled on %s: no Gemini Enterprise app; GE features are "
                     "not_applicable here." % pid, impact=["gemini_enterprise"], data=cap), cap
    tasks = {}
    for loc in locs:
        url = "%s/v1/projects/%s/locations/%s/collections/default_collection/engines" % (
            discovery_host(loc), urllib.parse.quote(pid, safe=""), loc)
        tasks[loc] = (lambda u=url: run.try_call("GET", u, params={"pageSize": 100}, project_hint=pid))
    res = run.parallel(tasks)
    apps, unknown, found_locs = [], [], []
    for loc in locs:
        ok, value = res.get(loc, (False, None))
        payload, err = value if ok and isinstance(value, tuple) else (None, value)
        if err is not None:
            if not _soft_absent(err):
                unknown.append((loc, err))
            continue
        engines = [e for e in (payload or {}).get("engines") or [] if isinstance(e, dict)]
        typed = any("appType" in e for e in engines)
        for e in engines:
            if typed and e.get("appType") not in ("APP_TYPE_INTRANET",):
                continue
            apps.append({"engine_id": str(e.get("name") or "").rsplit("/", 1)[-1], "location": loc,
                         "display_name": str(e.get("displayName") or "")[:120],
                         "name": str(e.get("name") or "")})
        if any(a["location"] == loc for a in apps):
            found_locs.append(loc)
    if apps:
        cap = {"available": True, "reason": "apps_found", "apps": apps[:50], "locations": found_locs}
        return check(cid, "surfaces", "pass", *title,
                     "%d Gemini Enterprise app(s) in %s (%s)." % (len(apps), pid, ", ".join(found_locs)),
                     data=cap), cap
    if unknown and len(unknown) == len(locs):
        cap = {"available": None, "reason": "unreadable", "apps": [], "locations": []}
        return check(cid, "surfaces", "skip", *title,
                     "Gemini Enterprise apps of %s could not be read: %s (grant roles/discoveryengine.viewer)."
                     % (pid, reason_of(unknown[0][1])), data=cap), cap
    cap = {"available": False, "reason": "no_apps", "apps": [], "locations": []}
    note = (" (%s unreadable)" % ", ".join(l for l, _ in unknown)) if unknown else ""
    return check(cid, "surfaces", "info", *title,
                 "No Gemini Enterprise app in %s (%s)%s: GE seat/feature/model governance is not_applicable; create an "
                 "app in the console to use it." % (pid, ", ".join(locs), note), impact=["gemini_enterprise"],
                 data=cap, docs_url=DOCS["gemini_enterprise"]), cap


def _recent_inference_logs(run, pid: str):
    """True / False / None: inference_response logs in the last LOG_LOOKBACK_DAYS days."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=LOG_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {"resourceNames": ["projects/%s" % pid],
            "filter": 'log_id("businessaicode.googleapis.com/inference_response") AND timestamp>="%s"' % since,
            "orderBy": "timestamp desc", "pageSize": 1}
    payload, err = run.try_call("POST", "%s/entries:list" % LOGGING_V2, json_body=body, project_hint=pid)
    if err is not None:
        return None
    return bool((payload or {}).get("entries"))


def antigravity(run, pid: str) -> tuple[list[dict], dict]:
    title = ("Antigravity (AI 개발자 도구)", "Antigravity (AI developer tools)")
    cid = "surface.antigravity"
    api = run.api_enabled(pid, "businessaicode.googleapis.com")
    locs = _locations(run)
    tasks = {loc: (lambda l=loc: run.try_call("GET", "%s/v1alpha/projects/%s/locations/%s/devToolsConfig" % (
        discovery_host(l), urllib.parse.quote(pid, safe=""), l), project_hint=pid)) for loc in locs}
    if run.api_enabled(pid, "discoveryengine.googleapis.com") is False:
        tasks = {}
    tasks["__logs__"] = lambda: _recent_inference_logs(run, pid)
    res = run.parallel(tasks)
    ok, logs = res.pop("__logs__", (False, None))
    logs = logs if ok else None
    configs, enabled_locs, unknown = [], [], 0
    for loc in locs:
        if loc not in res:
            continue
        okl, value = res[loc]
        payload, err = value if okl and isinstance(value, tuple) else (None, value)
        if err is not None:
            if not _soft_absent(err):
                unknown += 1
            continue
        on = bool((payload or {}).get("devToolsEnabled"))
        configs.append({"location": loc, "dev_tools_enabled": on})
        if on:
            enabled_locs.append(loc)
    if enabled_locs or logs:
        available, reason = True, ("dev_tools_enabled" if enabled_locs else "inference_logs")
    elif api is False and not configs:
        available, reason = False, "api_disabled"
    elif configs or api is not None:
        available, reason = False, "dev_tools_disabled" if configs else "not_configured"
    else:
        available, reason = None, "unreadable"
    if available is False and unknown and not configs:
        available, reason = None, "unreadable"
    cap = {"available": available, "reason": reason, "apps": [], "locations": enabled_locs,
           "dev_tools_configs": configs, "inference_logs_recent": logs, "api_enabled": api}
    out = []
    if available:
        out.append(check(cid, "surfaces", "pass", *title,
                         "Antigravity is in use on %s (%s)." % (pid, "AI developer tools enabled in " +
                                                                ", ".join(enabled_locs) if enabled_locs else
                                                                "recent inference logs"), data=cap))
        if logs is False:
            out.append(check("surface.antigravity.metadata_logging", "surfaces", "warn",
                             "Antigravity 메타데이터 로깅", "Antigravity metadata logging",
                             "No businessaicode.googleapis.com/inference_response log in %d days on %s: per-user "
                             "Antigravity usage/caps need Gemini Enterprise > Settings > AI developer tools > "
                             "Compliance > Metadata logging (off by default)." % (LOG_LOOKBACK_DAYS, pid),
                             impact=["antigravity", "usage"], docs_url=DOCS["antigravity_logging"]))
    elif available is None:
        out.append(check(cid, "surfaces", "skip", *title,
                         "Antigravity settings of %s could not be read (roles/discoveryengine.viewer)." % pid, data=cap))
    else:
        out.append(check(cid, "surfaces", "info", *title,
                         "Antigravity is not used on %s (%s): Antigravity caps/model views are not_applicable." % (
                             pid, reason), impact=["antigravity"], data=cap))
    return out, cap


def vertex_ai(run, pid: str) -> tuple[dict, dict]:
    title = ("Vertex AI 사용 가능", "Vertex AI available")
    api = run.api_enabled(pid, "aiplatform.googleapis.com")
    if api is None:
        cap = {"available": None, "reason": "unknown", "apps": [], "locations": []}
        return check("surface.vertex_ai", "surfaces", "skip", *title,
                     "API state of %s unknown." % pid, data=cap), cap
    if not api:
        cap = {"available": False, "reason": "api_disabled", "apps": [], "locations": []}
        return check("surface.vertex_ai", "surfaces", "info", *title,
                     "aiplatform.googleapis.com is not enabled on %s: Vertex AI usage/model governance is not_applicable."
                     % pid, impact=["vertex_ai"], data=cap), cap
    locs = list(run.fact(run.hub, "vertex_locations") or ["global"])
    cap = {"available": True, "reason": "api_enabled", "apps": [], "locations": locs}
    return check("surface.vertex_ai", "surfaces", "pass", *title, "aiplatform.googleapis.com is enabled on %s." % pid,
                 data=cap), cap


def vertex_models_check(run) -> dict:
    """surface.vertex_ai.models (cross-project): Model Garden publisher models reachable."""
    title = ("Vertex AI 모델 카탈로그 접근", "Vertex AI model catalog reachable")
    _, err = run.try_call("GET", "%s/v1beta1/publishers/google/models" % AIPLATFORM, params={"pageSize": 1})
    if err is None:
        run.set_fact(run.hub, "vertex_locations", ["global"])
        return check("surface.vertex_ai.models", "surfaces", "pass", *title, "Global publisher model catalog reachable.")
    regional = "https://%s-aiplatform.googleapis.com" % run.region
    _, err2 = run.try_call("GET", "%s/v1beta1/publishers/google/models" % regional, params={"pageSize": 1})
    if err2 is None:
        run.set_fact(run.hub, "vertex_locations", [run.region])
        return check("surface.vertex_ai.models", "surfaces", "warn", *title,
                     "Global endpoint unreachable (%s); regional endpoint %s works: the model catalog uses %s." % (
                         reason_of(err), run.region, run.region), impact=["model_governance"],
                     data={"catalog_location": run.region})
    return check("surface.vertex_ai.models", "surfaces", "warn", *title,
                 "Publisher model catalog unreachable globally (%s) and in %s (%s): the model catalog falls back to the "
                 "static list." % (reason_of(err), run.region, reason_of(err2)), impact=["model_governance"])


def surface_checks(run, pid: str) -> tuple[list[dict], dict]:
    """(checks, capabilities-subset) for one project."""
    ge_check, ge_cap = gemini_enterprise(run, pid)
    agy_checks, agy_cap = antigravity(run, pid)
    vx_check, vx_cap = vertex_ai(run, pid)
    caps = {"gemini_enterprise": ge_cap, "antigravity": agy_cap, "vertex_ai": vx_cap}
    run.set_fact(pid, "surfaces", caps)
    return [vx_check, ge_check] + agy_checks, caps
