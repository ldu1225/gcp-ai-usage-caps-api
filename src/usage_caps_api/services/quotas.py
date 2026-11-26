"""Rate limits & quota management across every managed project (Cloud Quotas API + Cloud Monitoring).

Admin-API analogue: per-project, per-model RPM/TPM rate limits (read / lower) and limit increase requests.

Google Cloud mechanics:
  * Limits:     Cloud Quotas API `QuotaInfo` (cloudquotas.googleapis.com/v1 .../services/{service}/quotaInfos), with
                dimensions such as `region` and `base_model` (e.g. generate_content requests per minute per project
                per base model). Current value = default + approved preferences.
  * Usage:      Cloud Monitoring `serviceruntime.googleapis.com/quota/rate/net_usage` (consumer_quota) -> peak usage
                and headroom %.
  * Changes:    `QuotaPreference` create/patch. Decreases (capping a project below its limit, like a vendor admin API's project
                rate-limit override) apply quickly; increases are reviewed by Google (pending -> approved /
                partially_approved / denied, hours to days) and are tracked as quota requests with webhooks.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
import secrets

from .. import config
from ..gcp import GcpError
from ..httpkit import ApiError, iso_z, now_iso, paginate_list
from . import audit, operations, webhooks

LOG = logging.getLogger("usage_caps_api.quotas")

CLOUDQUOTAS = "https://cloudquotas.googleapis.com/v1"
MONITORING = "https://monitoring.googleapis.com/v3"
REQUESTS_DOC = "quota_requests"
REQUEST_ID_RE = re.compile(r"^qr_[0-9a-f]{16}$")
LIMIT_ID_RE = re.compile(r"^rl_[0-9a-f]{16}$")
AI_QUOTA_RE = re.compile(r"(?i)(generate_?content|prediction|predict|token|base_model|maas|anthropic|llama|gemini|"
                         r"online_prediction|global_)")
MAX_REQUESTS = 2000
STATUSES = ("pending", "approved", "partially_approved", "denied", "applied", "cancelled", "failed")
TERMINAL = ("approved", "denied", "applied", "cancelled", "failed")
SAFETY_CHECKS = ("QUOTA_DECREASE_BELOW_USAGE", "QUOTA_DECREASE_PERCENTAGE_TOO_HIGH")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def service_name(ctx) -> str:
    return str((ctx.config.get("quotas") or {}).get("service") or "aiplatform.googleapis.com")


def _require_managed(ctx, project: str) -> str:
    project = str(project or "").strip().lower()
    if not config.valid_project(project):
        raise ApiError("bad_request", "Invalid project id '%s'." % project)
    if project not in ctx.all_projects:
        raise ApiError("forbidden", "Project '%s' is not managed by this control plane." % project)
    return project


def limit_id(project: str, quota_id: str, dims: dict) -> str:
    raw = "%s|%s|%s" % (project, quota_id, "|".join("%s=%s" % kv for kv in sorted((dims or {}).items())))
    return "rl_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _int(value):
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _metric_kind(info: dict) -> str:
    text = ("%s %s %s" % (info.get("quotaId"), info.get("metric"), info.get("metricDisplayName"))).lower()
    unit = str(info.get("metricUnit") or "").lower()
    tokens = "token" in text
    per_day = "per_day" in text or "perday" in text or str(info.get("refreshInterval") or "").lower() == "day"
    if tokens:
        return "tokens_per_day" if per_day else "tokens_per_minute"
    if "request" in text or "prediction" in text or unit.endswith("1/min/{project}"):
        return "requests_per_day" if per_day else "requests_per_minute"
    return "other"


# ---------------------------------------------------------------------------
# Reading limits, preferences and usage
# ---------------------------------------------------------------------------

def _quota_infos(ctx, project: str) -> list[dict]:
    url = "%s/projects/%s/locations/global/services/%s/quotaInfos" % (CLOUDQUOTAS, project, service_name(ctx))
    items, _ = ctx.gcp.paginate(url, quota_project=ctx.quota_project, items_key="quotaInfos", max_pages=20,
                                page_size=500)
    return items


def _preferences(ctx, project: str) -> list[dict]:
    url = "%s/projects/%s/locations/global/quotaPreferences" % (CLOUDQUOTAS, project)
    items, _ = ctx.gcp.paginate(url, quota_project=ctx.quota_project, items_key="quotaPreferences", max_pages=10,
                                params={"filter": 'service="%s"' % service_name(ctx)})
    return items


def _usage_peaks(ctx, project: str, hours: int = 24) -> dict:
    """{(quota_metric, location, base_model|None): {"peak_per_minute": n, "sum": n}} from Cloud Monitoring."""
    end = utcnow()
    start = end - dt.timedelta(hours=hours)
    params = {
        "filter": ('metric.type="serviceruntime.googleapis.com/quota/rate/net_usage" AND '
                   'resource.type="consumer_quota" AND resource.labels.service="%s"' % service_name(ctx)),
        "interval.startTime": iso_z(start),
        "interval.endTime": iso_z(end),
        "aggregation.alignmentPeriod": "60s",
        "aggregation.perSeriesAligner": "ALIGN_SUM",
        "view": "FULL",
    }
    url = "%s/projects/%s/timeSeries" % (MONITORING, project)
    series, _ = ctx.gcp.paginate(url, quota_project=ctx.quota_project, items_key="timeSeries", params=params,
                                 max_pages=10)
    out: dict = {}
    for ts in series or []:
        mlabels = ((ts.get("metric") or {}).get("labels") or {})
        rlabels = ((ts.get("resource") or {}).get("labels") or {})
        qm = mlabels.get("quota_metric") or ""
        loc = rlabels.get("location") or "global"
        base_model = next((v for k, v in mlabels.items() if "base_model" in k), None)
        key = (qm, loc, base_model)
        agg = out.setdefault(key, {"peak_per_minute": 0, "sum": 0})
        for p in ts.get("points") or []:
            val = (p.get("value") or {})
            n = _int(val.get("int64Value")) if val.get("int64Value") is not None else int(float(val.get("doubleValue") or 0))
            n = n or 0
            agg["sum"] += n
            agg["peak_per_minute"] = max(agg["peak_per_minute"], n)
    return out


def _flatten(ctx, project: str, infos: list[dict], prefs: list[dict], usage: dict | None, ai_only: bool) -> list[dict]:
    pref_by_key = {}
    for p in prefs or []:
        key = (p.get("quotaId"), tuple(sorted((p.get("dimensions") or {}).items())))
        pref_by_key[key] = p
    rows = []
    for info in infos or []:
        qid = info.get("quotaId") or ""
        if ai_only and not AI_QUOTA_RE.search("%s %s" % (qid, info.get("metric") or "")) and \
                "base_model" not in (info.get("dimensions") or []):
            continue
        kind = _metric_kind(info)
        for di in info.get("dimensionsInfos") or [{"dimensions": {}, "details": {}}]:
            dims = dict(di.get("dimensions") or {})
            value = _int((di.get("details") or {}).get("value"))
            location = dims.get("region") or "global"
            model = dims.get("base_model")
            pref = pref_by_key.get((qid, tuple(sorted(dims.items()))))
            peak = None
            if usage is not None:
                hit = usage.get((info.get("metric"), location, model)) or usage.get((info.get("metric"), location, None))
                if hit:
                    peak = hit["sum"] if kind.endswith("per_day") else hit["peak_per_minute"]
            headroom = None
            if value and value > 0 and peak is not None:
                headroom = round(max(0.0, 100.0 * (1 - float(peak) / float(value))), 2)
            elig = info.get("quotaIncreaseEligibility") or {}
            rows.append({
                "object": "rate_limit",
                "id": limit_id(project, qid, dims),
                "project_id": project,
                "service": info.get("service") or service_name(ctx),
                "quota_id": qid,
                "metric": info.get("metric"),
                "metric_kind": kind,
                "display_name": info.get("quotaDisplayName") or info.get("metricDisplayName") or qid,
                "unit": info.get("metricUnit"),
                "refresh_interval": info.get("refreshInterval"),
                "model": model,
                "location": location,
                "dimensions": dims,
                "is_default": not dims,
                "limit": value,
                "usage_peak": peak,
                "usage_window_hours": 24 if usage is not None else None,
                "utilization_pct": (round(100.0 - headroom, 2) if headroom is not None else None),
                "headroom_pct": headroom,
                "increase_eligible": elig.get("isEligible"),
                "ineligibility_reason": elig.get("ineligibilityReason"),
                "is_fixed": bool(info.get("isFixed")),
                "preference": ({
                    "name": pref.get("name"),
                    "preferred_value": _int((pref.get("quotaConfig") or {}).get("preferredValue")),
                    "granted_value": _int((pref.get("quotaConfig") or {}).get("grantedValue")),
                    "reconciling": bool(pref.get("reconciling")),
                    "state_detail": (pref.get("quotaConfig") or {}).get("stateDetail"),
                    "update_time": pref.get("updateTime"),
                } if pref else None),
            })
    return rows


def project_limits(ctx, project: str, *, with_usage: bool = True, ai_only: bool = True) -> tuple[list[dict], dict]:
    """Returns (rate limit rows, warnings) for one project; cached briefly."""
    def load(c):
        warn = {}
        tasks = {"infos": lambda: _quota_infos(c, project), "prefs": lambda: _preferences(c, project)}
        if with_usage:
            tasks["usage"] = lambda: _usage_peaks(c, project)
        res = c.run(tasks)
        if not res["infos"][0]:
            err = res["infos"][1]
            kind = getattr(err, "kind", "error")
            raise ApiError("forbidden" if kind == "denied" else "upstream_error",
                           "Cloud Quotas API read failed for %s (%s). Enable cloudquotas.googleapis.com and grant "
                           "roles/cloudquotas.viewer." % (project, kind))
        prefs = res["prefs"][1] if res["prefs"][0] else []
        if not res["prefs"][0]:
            warn["preferences"] = getattr(res["prefs"][1], "kind", "error")
        usage = None
        if with_usage:
            if res["usage"][0]:
                usage = res["usage"][1]
            else:
                warn["usage"] = getattr(res["usage"][1], "kind", "error")
        return _flatten(c, project, res["infos"][1], prefs, usage, ai_only), warn
    return ctx.memo("quota_limits", (project, with_usage, ai_only), load, ttl=120)


def _filter_rows(ctx, rows: list[dict]) -> list[dict]:
    model = (ctx.params.get("model") or "").strip().lower()
    location = (ctx.params.get("location") or "").strip().lower()
    kind = (ctx.params.get("metric") or ctx.params.get("metric_kind") or "").strip().lower()
    if model:
        rows = [r for r in rows if (r.get("model") or "").lower() == model or
                (model == "default" and r.get("model") is None)]
    if location:
        rows = [r for r in rows if (r.get("location") or "").lower() == location]
    if kind:
        rows = [r for r in rows if r.get("metric_kind") == kind or (r.get("quota_id") or "").lower() == kind]
    if str(ctx.params.get("near_limit") or "").lower() in ("1", "true"):
        warn = float((ctx.config.get("quotas") or {}).get("headroom_warn_pct") or 80)
        rows = [r for r in rows if r.get("utilization_pct") is not None and r["utilization_pct"] >= warn]
    return rows


def _flags(ctx) -> tuple[bool, bool]:
    with_usage = str(ctx.params.get("include_usage") or "true").lower() not in ("0", "false")
    ai_only = str(ctx.params.get("all") or "").lower() not in ("1", "true")
    return with_usage, ai_only


# ---------------------------------------------------------------------------
# Handlers: limits
# ---------------------------------------------------------------------------

def list_all_limits(ctx) -> dict:
    """GET /v1/rate-limits?project=&model=&location=&metric=&near_limit=&all=&include_usage="""
    with_usage, ai_only = _flags(ctx)
    project = (ctx.params.get("project") or "").strip().lower()
    projects = [_require_managed(ctx, project)] if project else ctx.all_projects
    cap = int((ctx.config.get("quotas") or {}).get("max_projects_per_call") or 50)
    tasks = {p: (lambda p=p: project_limits(ctx, p, with_usage=with_usage, ai_only=ai_only)) for p in projects[:cap]}
    outcomes = ctx.run(tasks) if tasks else {}
    rows, errors = [], {}
    for p in projects[:cap]:
        ok, val = outcomes.get(p, (False, None))
        if ok:
            rows.extend(val[0])
            if val[1]:
                errors[p] = val[1]
        else:
            errors[p] = getattr(val, "message", getattr(val, "kind", "error"))
    rows = _filter_rows(ctx, rows)
    rows.sort(key=lambda r: (-(r["utilization_pct"] if r["utilization_pct"] is not None else -1), r["project_id"],
                             r["quota_id"], r.get("model") or "", r.get("location") or ""))
    page = paginate_list(rows, ctx.params, default_limit=100, max_limit=1000)
    page["errors"] = errors
    if len(projects) > cap:
        page["truncated_projects"] = len(projects) - cap
    return page


def list_project_limits(ctx) -> dict:
    """GET /v1/projects/{project_id}/rate-limits"""
    project = _require_managed(ctx, ctx.path_params.get("project_id"))
    with_usage, ai_only = _flags(ctx)
    rows, warn = project_limits(ctx, project, with_usage=with_usage, ai_only=ai_only)
    rows = _filter_rows(ctx, rows)
    page = paginate_list(rows, ctx.params, default_limit=100, max_limit=1000)
    page["warnings_detail"] = warn
    return page


def _find_limit(ctx, project: str, lid: str) -> dict:
    if not LIMIT_ID_RE.match(lid or ""):
        raise ApiError("bad_request", "Invalid rate limit id.")
    for ai_only in (True, False):
        rows, _ = project_limits(ctx, project, with_usage=True, ai_only=ai_only)
        for r in rows:
            if r["id"] == lid:
                return r
    raise ApiError("not_found", "Rate limit '%s' not found in project '%s'." % (lid, project))


def get_project_limit(ctx) -> dict:
    """GET /v1/projects/{project_id}/rate-limits/{limit_id}"""
    project = _require_managed(ctx, ctx.path_params.get("project_id"))
    return _find_limit(ctx, project, ctx.path_params.get("limit_id"))


def patch_project_limit(ctx) -> dict:
    """PATCH /v1/projects/{project_id}/rate-limits/{limit_id} {value, justification?, contact_email?, force?, dryRun?}

    value <= current limit -> quota decrease (cap), applied quickly; value > current -> quota increase request."""
    project = _require_managed(ctx, ctx.path_params.get("project_id"))
    row = _find_limit(ctx, project, ctx.path_params.get("limit_id"))
    body = dict(ctx.body or {})
    if "value" not in body:
        raise ApiError("bad_request", "Field 'value' is required.")
    body["requested_value"] = body.pop("value")
    return _submit(ctx, row, body)


# ---------------------------------------------------------------------------
# Quota requests (increase / decrease)
# ---------------------------------------------------------------------------

def _status_from_pref(rec: dict, pref: dict) -> tuple[str, int | None, str | None]:
    cfg = pref.get("quotaConfig") or {}
    granted = _int(cfg.get("grantedValue"))
    requested = rec["requested_value"]
    detail = cfg.get("stateDetail")
    if pref.get("reconciling"):
        return "pending", granted, detail
    if granted is None:
        return "pending", granted, detail
    if rec["kind"] == "decrease":
        return ("applied" if granted <= requested else "pending"), granted, detail
    if granted >= requested:
        return "approved", granted, detail
    prev = rec.get("previous_value") or 0
    if granted > prev:
        return "partially_approved", granted, detail
    return ("denied" if detail else "pending"), granted, detail


def _public(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k not in ("idempotency_key",)}


def _load_requests(ctx) -> list[dict]:
    doc, _ = ctx.store.load(REQUESTS_DOC)
    return [r for r in doc.get("requests") or [] if isinstance(r, dict)]


def _save_request(ctx, rec: dict):
    def save(doc):
        items = [r for r in doc.get("requests") or [] if isinstance(r, dict) and r.get("id") != rec["id"]]
        items.append(rec)
        doc["requests"] = items[-MAX_REQUESTS:]
        return doc
    ctx.store.mutate(REQUESTS_DOC, save)


def _resolve_target(ctx, project: str, body: dict) -> dict:
    lid = body.get("limit_id")
    if lid:
        return _find_limit(ctx, project, lid)
    qid = str(body.get("quota_id") or "").strip()
    if not qid:
        raise ApiError("bad_request", "Provide 'limit_id' or 'quota_id' (+ optional 'model' / 'location').")
    model = (str(body.get("model") or "").strip().lower() or None)
    location = (str(body.get("location") or "").strip().lower() or None)
    rows, _ = project_limits(ctx, project, with_usage=True, ai_only=False)
    cands = [r for r in rows if r["quota_id"] == qid]
    if not cands:
        raise ApiError("not_found", "Quota '%s' not found for service %s in %s." % (qid, service_name(ctx), project))
    exact = [r for r in cands if r.get("model") == model and (location is None or r.get("location") == location)]
    if exact:
        return exact[0]
    # No explicit dimension entry yet: derive dimensions from the quota's default row.
    base = next((r for r in cands if r["is_default"]), cands[0])
    dims = {}
    if location and location != "global":
        dims["region"] = location
    if model:
        dims["base_model"] = model
    return dict(base, id=limit_id(project, qid, dims), dimensions=dims, model=model, location=location or "global",
                is_default=not dims, preference=None)


def _submit(ctx, row: dict, body: dict) -> dict:
    project = row["project_id"]
    requested = body.get("requested_value")
    if requested is None and body.get("increase_pct") is not None:
        pct = body["increase_pct"]
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or pct <= 0 or pct > 10000:
            raise ApiError("bad_request", "Field 'increase_pct' must be a positive number.")
        requested = int((row.get("limit") or 0) * (1 + pct / 100.0))
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 0 or requested > 10 ** 15:
        raise ApiError("bad_request", "Field 'requested_value' must be a non-negative integer.")
    if row.get("is_fixed"):
        raise ApiError("unprocessable", "Quota '%s' is a fixed system limit and cannot be changed." % row["quota_id"])
    current = row.get("limit")
    kind = "decrease" if (current is not None and requested <= current) else "increase"
    if kind == "increase" and row.get("increase_eligible") is False:
        raise ApiError("quota_exceeds_maximum", "Quota increase not eligible: %s." %
                       (row.get("ineligibility_reason") or "ineligible"))
    contact = str(body.get("contact_email") or (ctx.config.get("quotas") or {}).get("contact_email") or "").strip()
    if kind == "increase" and not config.valid_email(contact):
        raise ApiError("bad_request", "Field 'contact_email' (or quotas.contact_email config) is required for increases;"
                       " Google may contact this address about the request.")
    justification = str(body.get("justification") or "").strip()[:1000]
    if kind == "increase" and len(justification) < 10:
        raise ApiError("bad_request", "Field 'justification' (>= 10 characters) is required for quota increases.")
    dry_run = body.get("dryRun", body.get("dry_run", False))
    if not isinstance(dry_run, bool):
        raise ApiError("bad_request", "Field 'dryRun' must be a boolean.")
    force = bool(body.get("force", False))
    if kind == "decrease" and row.get("usage_peak") is not None and requested < row["usage_peak"] and not force:
        raise ApiError("unprocessable", "Requested value %d is below the observed 24h peak usage %d; this will cause "
                       "429 errors. Pass force=true to confirm." % (requested, row["usage_peak"]))
    idem = ctx.headers.get("idempotency-key") or body.get("idempotency_key")
    if idem:
        for rec in _load_requests(ctx):
            if rec.get("idempotency_key") == idem and rec.get("project_id") == project:
                return dict(_public(rec), idempotent_replay=True)
    for rec in _load_requests(ctx):
        if rec.get("limit_id") == row["id"] and rec.get("status") == "pending" and not force:
            raise ApiError("quota_request_pending", "A pending quota request (%s) already exists for this limit; "
                           "cancel it or pass force=true to supersede." % rec["id"])
    preview = {
        "object": "quota_request",
        "project_id": project,
        "service": row["service"],
        "quota_id": row["quota_id"],
        "limit_id": row["id"],
        "dimensions": row.get("dimensions") or {},
        "model": row.get("model"),
        "location": row.get("location"),
        "metric_kind": row.get("metric_kind"),
        "kind": kind,
        "previous_value": current,
        "requested_value": requested,
    }
    if dry_run:
        return dict(preview, status="dry_run")
    pref_body = {
        "service": row["service"],
        "quotaId": row["quota_id"],
        "quotaConfig": {"preferredValue": str(requested)},
        "dimensions": row.get("dimensions") or {},
    }
    if justification:
        pref_body["justification"] = justification
    if contact:
        pref_body["contactEmail"] = contact
    params = {}
    if kind == "decrease" and force:
        params["ignoreSafetyChecks"] = list(SAFETY_CHECKS)
    existing = (row.get("preference") or {}).get("name")
    try:
        if existing:
            pref_body["name"] = existing
            params.update({"updateMask": "quotaConfig.preferredValue,justification,contactEmail",
                           "allowMissing": "true"})
            pref = ctx.rest("PATCH", "%s/%s" % (CLOUDQUOTAS, existing), params=params, json_body=pref_body,
                            timeout=60) or {}
        else:
            pref_id = "gcpai-%s" % secrets.token_hex(6)
            params["quotaPreferenceId"] = pref_id
            pref = ctx.rest("POST", "%s/projects/%s/locations/global/quotaPreferences" % (CLOUDQUOTAS, project),
                            params=params, json_body=pref_body, timeout=60) or {}
    except GcpError as err:
        if err.kind == "denied":
            raise ApiError("forbidden", "The service account lacks cloudquotas.quotas.update on %s "
                           "(grant roles/cloudquotas.admin)." % project)
        if err.kind == "invalid":
            raise ApiError("unprocessable", "Cloud Quotas rejected the request: %s" % (err.message or "invalid")[:300])
        raise ApiError("upstream_error", "Cloud Quotas request failed (%s)." % err.kind)
    now = now_iso()
    rec = dict(preview, id="qr_" + secrets.token_hex(8), justification=justification or None,
               contact_email=contact or None, preference_name=pref.get("name") or existing,
               created_at=now, updated_at=now, created_by=ctx.caller.id, idempotency_key=idem, history=[])
    status, granted, detail = _status_from_pref(rec, pref) if pref else ("pending", None, None)
    rec.update(status=status, granted_value=granted, status_detail=detail)
    rec["history"].append({"at": now, "status": status, "granted_value": granted})
    op = operations.create(ctx, "quota.%s" % kind, target=rec["preference_name"] or project,
                           detail=("Quota decrease submitted; applied shortly." if kind == "decrease" else
                                   "Quota increase submitted to Google Cloud for review (hours to days)."),
                           result={"quota_request_id": rec["id"]}, verify={"quota_request_id": rec["id"]})
    rec["operation_id"] = op["id"]
    _save_request(ctx, rec)
    if getattr(ctx.app, "cache", None) is not None:
        ctx.app.cache.clear("quota_limits")
    audit.write_audit(ctx, "quota_request.created", target=rec["id"], details={
        k: rec.get(k) for k in ("project_id", "quota_id", "dimensions", "kind", "previous_value", "requested_value",
                                "status")})
    webhooks.dispatch_event(ctx, "quota_request.created", _public(rec))
    return dict(_public(rec), operation=op)


def create_request(ctx) -> dict:
    """POST /v1/quota-requests {project_id | projects[], limit_id | quota_id+model+location, requested_value |
    increase_pct, justification, contact_email, dryRun?, force?}"""
    body = ctx.body or {}
    projects = body.get("projects")
    if projects is not None:
        if not isinstance(projects, list) or not projects:
            raise ApiError("bad_request", "Field 'projects' must be a non-empty list (or ['*']).")
        targets = ctx.all_projects if "*" in projects else [_require_managed(ctx, p) for p in projects]
        if body.get("limit_id"):
            raise ApiError("bad_request", "Use 'quota_id' (+ model/location) with 'projects'; limit ids are per project.")
        results = []
        for p in targets:
            try:
                row = _resolve_target(ctx, p, body)
                results.append(_submit(ctx, row, dict(body)))
            except ApiError as err:
                results.append({"project_id": p, "status": "failed", "error": err.code, "message": err.message[:300]})
        return {"object": "bulk_result", "results": results,
                "submitted": sum(1 for r in results if r.get("status") not in ("failed", "dry_run")),
                "failed": sum(1 for r in results if r.get("status") == "failed")}
    project = _require_managed(ctx, body.get("project_id"))
    row = _resolve_target(ctx, project, body)
    return _submit(ctx, row, dict(body))


def refresh_request(ctx, rec: dict) -> dict:
    if rec.get("status") in TERMINAL or not rec.get("preference_name"):
        return rec
    try:
        pref = ctx.rest("GET", "%s/%s" % (CLOUDQUOTAS, rec["preference_name"]), timeout=30) or {}
    except GcpError as err:
        LOG.warning("quota preference read failed kind=%s", err.kind)
        return rec
    status, granted, detail = _status_from_pref(rec, pref)
    if status != rec.get("status") or granted != rec.get("granted_value"):
        now = now_iso()
        rec = dict(rec, status=status, granted_value=granted, status_detail=detail, updated_at=now)
        rec["history"] = list(rec.get("history") or []) + [{"at": now, "status": status, "granted_value": granted}]
        _save_request(ctx, rec)
        if rec.get("operation_id") and status in TERMINAL + ("partially_approved",):
            operations.update(ctx, rec["operation_id"], status="succeeded" if status in ("approved", "applied") else
                              ("failed" if status == "denied" else "succeeded"), result={"quota_request_id": rec["id"],
                                                                                          "status": status})
        audit.write_audit(ctx, "quota_request.status_changed", target=rec["id"],
                          details={"status": status, "granted_value": granted})
        webhooks.dispatch_event(ctx, "quota_request.updated", _public(rec))
    return rec


def refresh_pending(ctx, limit: int = 50) -> dict:
    """Refreshes pending quota requests (called by the scheduled enforcer job)."""
    pending = [r for r in _load_requests(ctx) if r.get("status") not in TERMINAL][:limit]
    changed = 0
    for rec in pending:
        new = refresh_request(ctx, rec)
        if new.get("status") != rec.get("status"):
            changed += 1
    return {"checked": len(pending), "changed": changed}


def list_requests(ctx) -> dict:
    """GET /v1/quota-requests?status=&project=&refresh=true"""
    recs = list(reversed(_load_requests(ctx)))
    status = (ctx.params.get("status") or "").strip().lower()
    project = (ctx.params.get("project") or "").strip().lower()
    if project:
        recs = [r for r in recs if r.get("project_id") == project]
    if str(ctx.params.get("refresh") or "").lower() in ("1", "true"):
        recs = [refresh_request(ctx, r) for r in recs[:50]] + recs[50:]
    if status:
        if status not in STATUSES:
            raise ApiError("bad_request", "Parameter 'status' must be one of: %s." % ", ".join(STATUSES))
        recs = [r for r in recs if r.get("status") == status]
    return paginate_list([_public(r) for r in recs], ctx.params, default_limit=100, max_limit=500)


def _get(ctx, rid: str) -> dict:
    if not REQUEST_ID_RE.match(rid or ""):
        raise ApiError("bad_request", "Invalid quota request id.")
    rec = next((r for r in _load_requests(ctx) if r.get("id") == rid), None)
    if rec is None:
        raise ApiError("not_found", "Quota request '%s' not found." % rid)
    return rec


def get_request(ctx) -> dict:
    """GET /v1/quota-requests/{request_id} (live status refresh)."""
    return _public(refresh_request(ctx, _get(ctx, ctx.path_params.get("request_id"))))


def cancel_request(ctx) -> dict:
    """POST /v1/quota-requests/{request_id}/cancel: resets the preference to the previously granted value."""
    rec = refresh_request(ctx, _get(ctx, ctx.path_params.get("request_id")))
    if rec.get("status") in TERMINAL:
        raise ApiError("conflict", "Quota request is already %s." % rec["status"])
    target = rec.get("granted_value") if rec.get("granted_value") is not None else rec.get("previous_value")
    if target is None:
        raise ApiError("conflict", "Cannot determine the value to restore for this request.")
    body = {"name": rec["preference_name"], "service": rec["service"], "quotaId": rec["quota_id"],
            "quotaConfig": {"preferredValue": str(target)}, "dimensions": rec.get("dimensions") or {}}
    try:
        ctx.rest("PATCH", "%s/%s" % (CLOUDQUOTAS, rec["preference_name"]),
                 params={"updateMask": "quotaConfig.preferredValue"}, json_body=body, timeout=60)
    except GcpError as err:
        raise ApiError("upstream_error", "Cancelling the quota request failed (%s)." % err.kind)
    now = now_iso()
    rec = dict(rec, status="cancelled", updated_at=now)
    rec["history"] = list(rec.get("history") or []) + [{"at": now, "status": "cancelled", "granted_value": target}]
    _save_request(ctx, rec)
    audit.write_audit(ctx, "quota_request.cancelled", target=rec["id"], details={"restored_value": target})
    webhooks.dispatch_event(ctx, "quota_request.updated", _public(rec))
    return _public(rec)


def _verify_quota(ctx, op: dict):
    rid = (op.get("verify") or {}).get("quota_request_id")
    if not rid:
        return None
    rec = next((r for r in _load_requests(ctx) if r.get("id") == rid), None)
    if rec is None:
        return None
    rec = refresh_request(ctx, rec)
    if rec.get("status") in ("approved", "applied", "partially_approved"):
        return "succeeded"
    if rec.get("status") in ("denied", "failed"):
        return "failed"
    return None


operations.VERIFIERS["quota"] = _verify_quota


def check_thresholds(ctx, *, min_interval_minutes: int = 60) -> dict:
    """Emits `rate_limit.threshold_reached` (once per limit per day) for limits above quotas.headroom_warn_pct."""
    doc, _ = ctx.store.load(REQUESTS_DOC)
    last = doc.get("last_threshold_check")
    from ..timeutil import parse_rfc3339
    at = parse_rfc3339(last) if last else None
    if at is not None and (utcnow() - at).total_seconds() < min_interval_minutes * 60:
        return {"skipped": "interval"}
    warn = float((ctx.config.get("quotas") or {}).get("headroom_warn_pct") or 80)
    hits = 0
    cap = int((ctx.config.get("quotas") or {}).get("max_projects_per_call") or 50)
    for p in ctx.all_projects[:cap]:
        try:
            rows, _ = project_limits(ctx, p, with_usage=True, ai_only=True)
        except ApiError:
            continue
        for r in rows:
            if r.get("utilization_pct") is not None and r["utilization_pct"] >= warn:
                if webhooks.notify_once(ctx, "%s|%s" % (r["id"], utcnow().strftime("%Y-%m-%d")),
                                        "rate_limit.threshold_reached", r):
                    hits += 1

    def mark(d):
        d["last_threshold_check"] = now_iso()
        return d
    ctx.store.mutate(REQUESTS_DOC, mark)
    return {"notified": hits}
