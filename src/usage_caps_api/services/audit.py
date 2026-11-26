"""Structured audit logging to Cloud Logging and bounded store mirror for GET /v1/audit-logs."""

from __future__ import annotations

import logging
import secrets

from ..gcp import GcpError
from ..httpkit import now_iso, paginate_list

LOG = logging.getLogger("usage_caps_api.audit")

LOGGING_V2 = "https://logging.googleapis.com/v2"
LOCAL_AUDIT_DOC = "audit_local"
MAX_LOCAL_ENTRIES = 2000


def write_audit(ctx, action: str, *, target: str | None = None, details: dict | None = None) -> bool:
    """Writes a structured audit entry to Cloud Logging (best-effort) and the store ring buffer."""
    ts = now_iso()
    actor = getattr(getattr(ctx, "caller", None), "id", None) or "system"
    auth_type = getattr(getattr(ctx, "caller", None), "auth_type", None) or "internal"
    entry = {
        "id": "aud_" + secrets.token_hex(8),
        "timestamp": ts,
        "action": action,
        "actor": actor,
        "auth_type": auth_type,
        "target": target,
        "details": dict(details or {}),
    }

    # Persist to store ring buffer so GET /v1/audit-logs is fast and works offline.
    try:
        def append(doc):
            items = [e for e in (doc.get("entries") or []) if isinstance(e, dict)]
            items.append(entry)
            doc["entries"] = items[-MAX_LOCAL_ENTRIES:]
        ctx.store.mutate(LOCAL_AUDIT_DOC, append)
    except Exception as err:  # pylint: disable=broad-except
        LOG.warning("local audit buffer write failed: %s", type(err).__name__)

    # Mirror to Cloud Logging when running against live GCP.
    log_name = ((ctx.config.get("caps") or {}).get("audit_log_name") or "gcp-ai-usage-caps-audit")
    body = {
        "logName": "projects/%s/logs/%s" % (ctx.project, log_name),
        "resource": {"type": "global", "labels": {"project_id": ctx.project}},
        "entries": [{"severity": "NOTICE", "jsonPayload": dict(entry, tool="gcp-ai-usage-caps-api")}],
    }
    try:
        ctx.rest("POST", "%s/entries:write" % LOGGING_V2, json_body=body, timeout=10)
        return True
    except GcpError as err:
        LOG.debug("Cloud Logging audit write skipped kind=%s", err.kind)
        return False
    except Exception:  # pylint: disable=broad-except
        return False


def _read_cloud_logging(ctx, limit: int = 200) -> list[dict]:
    log_name = ((ctx.config.get("caps") or {}).get("audit_log_name") or "gcp-ai-usage-caps-audit")
    flt = 'logName="projects/%s/logs/%s"' % (ctx.project, log_name)
    try:
        data = ctx.rest(
            "POST", "%s/entries:list" % LOGGING_V2,
            json_body={"resourceNames": ["projects/%s" % ctx.project], "filter": flt,
                       "orderBy": "timestamp desc", "pageSize": min(500, max(1, limit))},
            timeout=15,
        )
    except Exception:  # pylint: disable=broad-except
        return []
    out = []
    for item in (data or {}).get("entries") or []:
        payload = item.get("jsonPayload") if isinstance(item.get("jsonPayload"), dict) else {}
        out.append({
            "id": payload.get("id") or item.get("insertId") or "",
            "timestamp": payload.get("timestamp") or item.get("timestamp"),
            "action": payload.get("action") or "unknown",
            "actor": payload.get("actor") or "unknown",
            "auth_type": payload.get("auth_type") or "unknown",
            "target": payload.get("target"),
            "details": payload.get("details") if isinstance(payload.get("details"), dict) else {},
        })
    return out


def list_audit_logs(ctx) -> dict:
    """GET /v1/audit-logs?action=&target=&actor=&limit=&cursor="""
    doc, _ = ctx.store.load(LOCAL_AUDIT_DOC)
    local_entries = list(reversed([e for e in (doc.get("entries") or []) if isinstance(e, dict)]))
    cloud_entries = []
    if getattr(ctx.app, "gcp", None) is not None and type(ctx.app.gcp).__name__ == "Gcp":
        cloud_entries = _read_cloud_logging(ctx, limit=500)
        if cloud_entries:
            ctx.add_source("api:logging.entries")

    # Merge and deduplicate by id, newest first.
    seen: set[str] = set()
    merged: list[dict] = []
    for e in local_entries + cloud_entries:
        eid = str(e.get("id") or "")
        key = eid or ("%s|%s|%s" % (e.get("timestamp"), e.get("action"), e.get("target")))
        if key in seen:
            continue
        seen.add(key)
        merged.append(e)
    merged.sort(key=lambda e: str(e.get("timestamp") or ""), reverse=True)

    action_f = (ctx.params.get("action") or "").strip()
    target_f = (ctx.params.get("target") or "").strip().lower()
    actor_f = (ctx.params.get("actor") or "").strip().lower()
    if action_f:
        merged = [e for e in merged if e.get("action") == action_f]
    if target_f:
        merged = [e for e in merged if str(e.get("target") or "").lower() == target_f]
    if actor_f:
        merged = [e for e in merged if str(e.get("actor") or "").lower() == actor_f]

    return paginate_list(merged, ctx.params, default_limit=50, max_limit=500)
