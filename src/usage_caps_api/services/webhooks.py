"""Outbound signed webhooks for cap threshold alerts and block/unblock notifications.

Deliveries are signed with HMAC-SHA256:
  X-Webhook-Id: wh_...
  X-Webhook-Event: cap.threshold_reached | cap.exceeded | user.blocked | user.unblocked | ...
  X-Webhook-Timestamp: <unix seconds>
  X-Signature-256: sha256=<hex HMAC-SHA256(secret, "{timestamp}.{body}")>
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import urllib.request

from .. import config
from ..httpkit import ApiError, now_iso

LOG = logging.getLogger("usage_caps_api.webhooks")

WEBHOOKS_DOC = "webhooks"
WEBHOOK_ID_RE = re.compile(r"^wh_[0-9a-f]{12}$")
SUPPORTED_EVENTS = (
    "cap.threshold_reached",
    "cap.exceeded",
    "user.blocked",
    "user.unblocked",
    "project_cap.enforced",
    "project_cap.lifted",
    "model.access_changed",
    "model.lifecycle_notice",
    "model.first_seen",
    "model.catalog_changed",
    "user.model_violation",
    "rate_limit.threshold_reached",
    "quota_request.created",
    "quota_request.updated",
    "readiness.degraded",
    "group.cap_threshold_reached",
    "group.cap_exceeded",
    "group.blocked",
    "group.unblocked",
    "group.membership_changed",
)
SUPPORTED_EVENTS_SET = frozenset(SUPPORTED_EVENTS)
MAX_WEBHOOKS = 50
DELIVERY_TIMEOUT_SECONDS = 5


def sign_payload(secret: str, timestamp: str, body: bytes) -> str:
    msg = ("%s." % timestamp).encode("ascii") + body
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return "sha256=" + digest


def public_webhook_view(rec: dict) -> dict:
    return {
        "id": rec.get("id"),
        "name": rec.get("name"),
        "url": rec.get("url"),
        "events": list(rec.get("events") or []),
        "enabled": rec.get("enabled") is not False,
        "created_at": rec.get("created_at"),
        "created_by": rec.get("created_by"),
    }


def list_webhooks(ctx) -> dict:
    doc, _ = ctx.store.load(WEBHOOKS_DOC)
    items = [public_webhook_view(w) for w in (doc.get("webhooks") or []) if isinstance(w, dict) and w.get("id")]
    return {"object": "list", "data": items, "total": len(items)}


def create_webhook(ctx) -> dict:
    body = ctx.body or {}
    url = str(body.get("url") or "").strip()
    if not config.valid_https_url(url, check_dns=False):
        raise ApiError("bad_request", "Field 'url' must be a valid public HTTPS URL.")
    name = str(body.get("name") or url)[:120].strip()
    events = body.get("events")
    if events is None:
        events = list(SUPPORTED_EVENTS)
    if not isinstance(events, list) or not events:
        raise ApiError("bad_request", "Field 'events' must be a non-empty list of event names.")
    clean_events = []
    for ev in events:
        s = str(ev or "").strip()
        if s not in SUPPORTED_EVENTS_SET:
            raise ApiError("bad_request", "Unsupported webhook event '%s'. Valid events: %s."
                           % (s, ", ".join(SUPPORTED_EVENTS)))
        if s not in clean_events:
            clean_events.append(s)
    enabled = body.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ApiError("bad_request", "Field 'enabled' must be a boolean.")

    wh_id = "wh_" + secrets.token_hex(6)
    secret = "whsec_" + secrets.token_urlsafe(24)
    record = {
        "id": wh_id,
        "name": name,
        "url": url,
        "events": clean_events,
        "enabled": enabled,
        "secret": secret,
        "created_at": now_iso(),
        "created_by": ctx.caller.id,
    }

    def add(doc):
        items = [w for w in (doc.get("webhooks") or []) if isinstance(w, dict)]
        if len(items) >= MAX_WEBHOOKS:
            raise ApiError("bad_request", "Maximum number of webhooks (%d) reached." % MAX_WEBHOOKS)
        items.append(record)
        doc["webhooks"] = items
        return record

    ctx.store.mutate(WEBHOOKS_DOC, add)
    from . import audit
    audit.write_audit(ctx, "webhook.created", target=wh_id, details={"url": url, "events": clean_events})
    # Return secret once on creation (never returned on GET).
    return dict(public_webhook_view(record), secret=secret)


def delete_webhook(ctx) -> dict:
    wh_id = str(ctx.path_params.get("webhook_id") or "").strip()
    if not WEBHOOK_ID_RE.match(wh_id):
        raise ApiError("bad_request", "Invalid webhook id.")

    def remove(doc):
        items = [w for w in (doc.get("webhooks") or []) if isinstance(w, dict)]
        kept = [w for w in items if w.get("id") != wh_id]
        if len(kept) == len(items):
            raise ApiError("not_found", "Webhook '%s' not found." % wh_id)
        doc["webhooks"] = kept
        return True

    ctx.store.mutate(WEBHOOKS_DOC, remove)
    from . import audit
    audit.write_audit(ctx, "webhook.deleted", target=wh_id)
    return {"id": wh_id, "deleted": True}


def _default_sender(url: str, body: bytes, headers: dict) -> int:
    if not config.valid_https_url(url, check_dns=True):
        raise ValueError("webhook URL failed public IP check")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=DELIVERY_TIMEOUT_SECONDS) as resp:  # nosec B310
        return int(getattr(resp, "status", 200))


def dispatch_event(ctx, event_type: str, payload: dict, *, sender=None) -> list[dict]:
    """Delivers an event to every enabled webhook subscribed to `event_type`. Never raises."""
    if event_type not in SUPPORTED_EVENTS_SET:
        return []
    doc, _ = ctx.store.load(WEBHOOKS_DOC)
    hooks = [w for w in (doc.get("webhooks") or [])
             if isinstance(w, dict) and w.get("enabled") is not False and event_type in (w.get("events") or ())]
    if not hooks:
        return []
    ts = str(int(time.time()))
    envelope = {
        "id": "evt_" + secrets.token_hex(8),
        "event": event_type,
        "timestamp": now_iso(),
        "data": payload,
    }
    raw = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    deliver = sender or getattr(ctx.app, "webhook_sender", None) or _default_sender
    results = []
    for hook in hooks:
        sig = sign_payload(str(hook.get("secret") or ""), ts, raw)
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "GcpAiUsageCapsApi-Webhook/1.0",
            "X-Webhook-Id": str(hook.get("id") or ""),
            "X-Webhook-Event": event_type,
            "X-Webhook-Timestamp": ts,
            "X-Signature-256": sig,
        }
        try:
            status = deliver(hook["url"], raw, headers)
            ok = 200 <= int(status) < 300
            results.append({"webhook_id": hook["id"], "status": status, "delivered": ok})
        except Exception as err:  # pylint: disable=broad-except
            LOG.warning("webhook delivery failed id=%s event=%s error=%s", hook.get("id"), event_type, type(err).__name__)
            results.append({"webhook_id": hook["id"], "status": None, "delivered": False, "error": type(err).__name__})
    return results


def notify_threshold_once(ctx, *, email: str, period_start: str, threshold_pct: int,
                          utilization_pct: float, usage: dict, limit: dict, project: str | None = None) -> bool:
    """Emits `cap.threshold_reached` (or `cap.exceeded` at >=100%) at most once per (user, [project,] period,
    threshold). `project` is set for per-project caps (user assigned to several projects)."""
    dedupe_key = "%s|%s|%d" % (email.lower(), period_start or "all", int(threshold_pct))
    if project:
        dedupe_key += "|" + project
    already = {"sent": False}

    def mark(doc):
        sent = dict(doc.get("notified") or {})
        if dedupe_key in sent:
            already["sent"] = True
            return False
        # Prune oldest keys to keep doc bounded.
        if len(sent) > 5000:
            for k in list(sent.keys())[:1000]:
                del sent[k]
        sent[dedupe_key] = now_iso()
        doc["notified"] = sent
        return True

    try:
        ctx.store.mutate(WEBHOOKS_DOC, mark)
    except Exception:
        return False
    if already["sent"]:
        return False
    event_name = "cap.exceeded" if int(threshold_pct) >= 100 else "cap.threshold_reached"
    dispatch_event(ctx, event_name, {
        "email": email,
        "period_start": period_start,
        "threshold_pct": int(threshold_pct),
        "utilization_pct": round(utilization_pct, 2),
        "scope": "project" if project else "total",
        "project": project,
        "usage": usage,
        "limit": limit,
    })
    return True


def notify_once(ctx, dedupe_key: str, event_type: str, payload: dict) -> bool:
    """Dispatches `event_type` at most once per `dedupe_key` (bounded dedupe ledger shared with cap thresholds)."""
    key = "%s|%s" % (event_type, dedupe_key)
    already = {"sent": False}

    def mark(doc):
        sent = dict(doc.get("notified") or {})
        if key in sent:
            already["sent"] = True
            return False
        if len(sent) > 5000:
            for k in list(sent.keys())[:1000]:
                del sent[k]
        sent[key] = now_iso()
        doc["notified"] = sent
        return True

    try:
        ctx.store.mutate(WEBHOOKS_DOC, mark)
    except Exception:  # pylint: disable=broad-except
        return False
    if already["sent"]:
        return False
    dispatch_event(ctx, event_type, payload)
    return True
