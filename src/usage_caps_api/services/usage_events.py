"""Real-time Pub/Sub push usage event handler (POST /v1/internal/usage-event).

A Cloud Logging sink routes Antigravity inference entries (`businessaicode.googleapis.com/inference_response`)
to Pub/Sub, which pushes each entry to this endpoint with a Google OIDC token.
When a user crosses a webhook threshold or reaches their spend cap in `auto` mode, an outbound webhook
and/or immediate enforcement cycle (or Cloud Run Enforcer Job) is triggered within seconds.
"""

from __future__ import annotations

import base64
import binascii
import collections
import datetime as dt
import json
import logging
import threading
import urllib.parse

from .. import identity, timeutil
from ..auth import INTERNAL_SCOPE
from ..gcp import GcpError
from ..httpkit import ApiError
from . import caps as caps_svc, pricing as pricing_svc, usage as usage_svc, user_model_rules, webhooks
from .base import iso, to_int

LOG = logging.getLogger("usage_caps_api.usage_events")

MAX_DATA_CHARS = 60 * 1024
SNAPSHOT_TTL = dt.timedelta(seconds=60)
MIN_TRIGGER_INTERVAL = dt.timedelta(seconds=30)
SEEN_TTL = dt.timedelta(minutes=30)
SEEN_MAX = 20000


def decode_push(body: dict) -> dict | None:
    msg = body.get("message") if isinstance(body, dict) else None
    if not isinstance(msg, dict):
        return None
    data = msg.get("data")
    if not isinstance(data, str) or not data or len(data) > MAX_DATA_CHARS:
        return None
    try:
        entry = json.loads(base64.b64decode(data, validate=True).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return entry if isinstance(entry, dict) else None


def entry_usage(entry: dict, log_id: str, projects: list[str]) -> dict | None:
    name = entry.get("logName")
    if not isinstance(name, str) or "/logs/" not in name:
        return None
    project = usage_svc.entry_project(name)
    if not project or project not in projects:
        return None
    if urllib.parse.unquote(name.split("/logs/", 1)[1]) != urllib.parse.unquote(str(log_id or "")):
        return None
    labels = entry.get("labels") if isinstance(entry.get("labels"), dict) else {}
    person = identity.person_key(labels.get("user_id"))
    if person is None:
        return None
    ts = timeutil.parse_rfc3339(entry.get("timestamp")) or timeutil.parse_rfc3339(entry.get("receiveTimestamp"))
    if ts is None:
        return None
    payload = entry.get("jsonPayload") if isinstance(entry.get("jsonPayload"), dict) else {}
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    total = max(0, to_int(meta.get("totalTokenCount")))
    prompt = max(0, to_int(meta.get("promptTokenCount")))
    cand = max(0, to_int(meta.get("candidatesTokenCount")))
    cached = max(0, to_int(meta.get("cachedContentTokenCount")))
    model = str(labels.get("model_id") or labels.get("model") or "unknown").strip()[:200]
    key = (str(entry.get("insertId") or "").strip() or str(labels.get("request_id") or "").strip()
           or "%s|%s|%s|%d" % (project, person, iso(ts), total))
    return {
        "person": person,
        "project": project,
        "ts": ts,
        "key": key,
        "model": model,
        "total_tokens": total,
        "input_tokens": prompt,
        "output_tokens": max(total - prompt, cand, 0),
        "cached_tokens": min(cached, prompt),
    }


def _aggregate_items(items: list[dict]) -> dict:
    rec = {"requests": 0, "total_tokens": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "by_model": {}}
    for it in items:
        rec["requests"] += 1
        rec["total_tokens"] += it["total_tokens"]
        rec["input_tokens"] += it["input_tokens"]
        rec["output_tokens"] += it["output_tokens"]
        rec["cached_tokens"] += it["cached_tokens"]
        m = rec["by_model"].setdefault(it["model"], {"requests": 0, "total_tokens": 0, "input_tokens": 0,
                                                     "output_tokens": 0, "cached_tokens": 0})
        m["requests"] += 1
        m["total_tokens"] += it["total_tokens"]
        m["input_tokens"] += it["input_tokens"]
        m["output_tokens"] += it["output_tokens"]
        m["cached_tokens"] += it["cached_tokens"]
    return rec


class RealtimeTracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.snap_lock = threading.Lock()
        self.run_lock = threading.Lock()
        self.seen: collections.OrderedDict = collections.OrderedDict()
        self.snap = None
        self.last_trigger: dt.datetime | None = None

    def remember(self, item: dict, now: dt.datetime):
        with self.lock:
            if item["key"] not in self.seen:
                self.seen[item["key"]] = item
            floor = now - SEEN_TTL
            while self.seen and (len(self.seen) > SEEN_MAX or next(iter(self.seen.values()))["ts"] < floor):
                self.seen.popitem(last=False)

    def unseen(self, person: str, after: dt.datetime | None, since: dt.datetime | None, item: dict) -> list[dict]:
        with self.lock:
            items = [it for it in self.seen.values() if it["person"] == person]
        if not any(it["key"] == item["key"] for it in items):
            items.append(item)
        return [it for it in items if (after is None or it["ts"] > after) and (since is None or it["ts"] >= since)]

    def snapshot(self, ctx, now: dt.datetime, force: bool = False):
        with self.snap_lock:
            if force or self.snap is None or (now - self.snap["at"]) >= SNAPSHOT_TTL:
                payload, internal = caps_svc.compute_caps_status(ctx.child(), now=now)
                self.snap = {"at": now, "payload": payload, "internal": internal}
            return self.snap

    def drop_snapshot(self):
        with self.snap_lock:
            self.snap = None


TRACKER = RealtimeTracker()


def reset():
    global TRACKER  # pylint: disable=global-statement
    TRACKER = RealtimeTracker()


def _ack(action: str, **extra) -> dict:
    return dict({"acknowledged": True, "action": action}, **extra)


def handle_usage_event(ctx) -> dict:
    """POST /v1/internal/usage-event: processes one Pub/Sub push inference log message."""
    if not (ctx.caller.has_scope(INTERNAL_SCOPE) or ctx.caller.has_scope("admin")):
        raise ApiError("forbidden", "Only authorized Pub/Sub OIDC service accounts or admins may post usage events.")
    now = caps_svc.utcnow()
    entry = decode_push(ctx.body)
    if entry is None:
        return _ack("ignored", reason="invalid_payload")
    log_id = (ctx.config.get("caps") or {}).get("inference_log") or "businessaicode.googleapis.com%2Finference_response"
    item = entry_usage(entry, log_id, ctx.all_projects)
    if item is None:
        return _ack("ignored", reason="not_matching_inference_entry")

    TRACKER.remember(item, now)

    # Per-user model rules (allow/deny lists, RPM/TPM): evaluated on every event, independent of spend caps.
    try:
        recent = TRACKER.unseen(item["person"], None, now - dt.timedelta(minutes=2), item)
        model_res = user_model_rules.check_event(ctx, item, recent)
    except Exception as err:  # pylint: disable=broad-except
        LOG.warning("model rule check failed: %s", type(err).__name__)
        model_res = None
    if model_res and model_res.get("blocked"):
        TRACKER.drop_snapshot()
        return _ack("model_violation_blocked", violations=len(model_res.get("violations") or []))

    try:
        snap = TRACKER.snapshot(ctx, now)
    except (GcpError, ApiError):
        return _ack("ignored", reason="status_unavailable")

    payload = snap["payload"]
    internal = snap["internal"]
    policy = internal["policy"]
    win = internal["window"]
    pricing = internal["pricing"]
    usage_res = internal["usage"]

    person = item["person"]
    row = next((u for u in payload["users"] if u["email"] == person), None)
    if row and row["status"] in ("blocked", "suspended"):
        return _ack("already_" + row["status"])
    if row and (row.get("rule") or {}).get("type") == "allow":
        return _ack("allowed")

    last_counted = (usage_res.users.get(person) or {}).get("last_active")
    unseen_items = TRACKER.unseen(person, last_counted, win["start"], item)
    added = _aggregate_items(unseen_items)
    added_cost, _ = pricing_svc.estimate_cost(added, pricing)

    base_tokens = (row["usage"]["total_tokens"] if row else 0) + added["total_tokens"]
    base_req = (row["usage"]["requests"] if row else 0) + added["requests"]
    base_cost_raw = row["usage"]["estimated_cost"] if row else (0.0 if pricing.configured else None)
    base_cost = None if base_cost_raw is None else round(float(base_cost_raw) + float(added_cost or 0.0), 6)

    lim = row["limit"] if row else {
        "cost_cap": float(policy.get("cost_cap") or 0.0) or None,
        "token_cap": int(policy.get("token_cap") or 0) or None,
        "request_cap": int(policy.get("request_cap") or 0) or None,
        "currency": pricing.currency,
    }
    over, ratio_val, _reasons = caps_svc.cap_check(
        base_tokens, base_req,
        int(lim.get("token_cap") or 0), int(lim.get("request_cap") or 0),
        base_cost, float(lim.get("cost_cap") or 0.0),
    )

    # Fire deduplicated threshold webhooks in real time
    if ratio_val is not None:
        util_pct = ratio_val * 100.0
        for thr in sorted(int(t) for t in (policy.get("webhook_thresholds") or [80, 100])):
            if util_pct >= thr:
                webhooks.notify_threshold_once(
                    ctx, email=person, period_start=win["start_iso"] or "all",
                    threshold_pct=thr, utilization_pct=util_pct,
                    usage={"requests": base_req, "total_tokens": base_tokens, "estimated_cost": base_cost},
                    limit=lim,
                )

    if not over:
        return _ack("under_cap")
    if (policy.get("enforcement_mode") or "manual") != "auto":
        return _ack("over_cap_manual_mode")
    if (policy.get("enforcement_backend") or "iam_deny") == "notify_only":
        return _ack("over_cap_notify_only")

    with TRACKER.run_lock:
        if TRACKER.last_trigger is not None and (now - TRACKER.last_trigger) < MIN_TRIGGER_INTERVAL:
            raise ApiError("unavailable", "Enforcement throttle active; retry shortly.", status=503)
        TRACKER.last_trigger = now
        res = caps_svc.enforce_cycle(ctx, trigger="usage_event", dry_run=False, use_job_if_configured=True)
        TRACKER.drop_snapshot()
        return _ack("enforcement_triggered", enforcement_result=res.get("result"))
