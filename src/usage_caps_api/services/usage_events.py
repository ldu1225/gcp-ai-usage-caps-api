"""Real-time Pub/Sub push usage event handler (POST /v1/internal/usage-event).

A Cloud Logging sink (one per managed project, or one aggregated folder/organization sink) routes inference log
entries to Pub/Sub, which pushes each entry to this endpoint with a Google OIDC token:

- Antigravity `businessaicode.googleapis.com/inference_response` (person in `labels.user_id`)
- Vertex AI `aiplatform.googleapis.com/inference_request` (person from the custom header label; entries without a
  header label are handled by the scheduled cycle, which correlates them with Data Access audit logs)

Per event: per-user model rules (allow/deny list, RPM/TPM) are checked first (default action: block + notify), then
the user's total cap and the cap of the project the request ran in. Threshold webhooks fire in real time; in `auto`
mode an over-cap triggers an immediate enforcement cycle (or the Cloud Run Enforcer Job).
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

from .. import timeutil
from ..auth import INTERNAL_SCOPE
from ..gcp import GcpError
from ..httpkit import ApiError
from . import caps as caps_svc, pricing as pricing_svc, usage as usage_svc, user_model_rules, webhooks
from .base import iso

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


def _item(person, project, ts, key, model, publisher, surface, counts) -> dict:
    return {
        "person": person, "project": project, "ts": ts, "key": key, "model": model, "publisher": publisher,
        "surface": surface, "counts": counts,
        "total_tokens": counts["total_tokens"], "input_tokens": counts["input_tokens"],
        "output_tokens": counts["output_tokens"], "cached_tokens": counts["cached_tokens"],
    }


def entry_usage(entry: dict, log_id: str, projects: list[str], *, vertex_log_id: str | None = None,
                header_label: str = "user", directory: dict | None = None, long_ctx: int = 200000) -> dict | None:
    """Normalizes one pushed log entry (Antigravity or Vertex AI) into a usage item, or None when not applicable."""
    name = entry.get("logName")
    if not isinstance(name, str) or "/logs/" not in name:
        return None
    project = usage_svc.entry_project(name)
    if not project or project not in projects:
        return None
    lid = usage_svc.log_id_of(name)
    if lid == urllib.parse.unquote(str(log_id or "")):
        parsed = usage_svc.parse_agy_entry(entry, long_ctx)
        if parsed is None or parsed["person"] is None:
            return None
        ts = parsed["ts"] or timeutil.parse_rfc3339(entry.get("receiveTimestamp"))
        if ts is None:
            return None
        key = (str(entry.get("insertId") or "").strip()
               or str(((entry.get("labels") or {}).get("request_id")) or "").strip()
               or "%s|%s|%s|%d" % (project, parsed["person"], iso(ts), parsed["counts"]["total_tokens"]))
        return _item(parsed["person"], project, ts, key, parsed["model"], parsed["publisher"], "antigravity",
                     parsed["counts"])
    if vertex_log_id and lid == urllib.parse.unquote(vertex_log_id):
        parsed = usage_svc.parse_vertex_entry(entry, header_label=header_label, long_ctx=long_ctx)
        if parsed is None:
            return None
        person = usage_svc.resolve_user_label(parsed.get("user_label"), directory or {})
        if person is None or parsed["ts"] is None:
            return None
        key = parsed["key"] or "%s|%s|%s|%d" % (project, person, iso(parsed["ts"]), parsed["counts"]["total_tokens"])
        return _item(person, parsed["project"] or project, parsed["ts"], key, parsed["model"], parsed["publisher"],
                     "vertex_ai", parsed["counts"])
    return None


def _aggregate_items(items: list[dict]) -> dict:
    rec = usage_svc._new_counters()  # pylint: disable=protected-access
    rec["by_model"] = {}
    for it in items:
        counts = it.get("counts") or usage_svc.token_counts(
            prompt=it.get("input_tokens", 0), cand=it.get("output_tokens", 0), cached=it.get("cached_tokens", 0),
            total=it.get("total_tokens", 0))
        usage_svc._add(rec, counts)  # pylint: disable=protected-access
        m = rec["by_model"].setdefault(it["model"], dict(usage_svc._new_counters(),  # pylint: disable=protected-access
                                                         surface=it.get("surface"), publisher=it.get("publisher")))
        usage_svc._add(m, counts)  # pylint: disable=protected-access
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

    def unseen(self, person: str, after: dt.datetime | None, since: dt.datetime | None, item: dict,
               surface: str | None = None) -> list[dict]:
        with self.lock:
            items = [it for it in self.seen.values() if it["person"] == person
                     and (surface is None or it.get("surface") == surface)]
        if not any(it["key"] == item["key"] for it in items) and (surface is None or item.get("surface") == surface):
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


def _last_counted(usage_res, person: str, surface: str):
    rec = usage_res.users.get(person) or {}
    if not (rec.get("by_surface") or {}).get(surface):
        return None
    return rec.get("last_active")


def handle_usage_event(ctx) -> dict:
    """POST /v1/internal/usage-event: processes one Pub/Sub push inference log message."""
    if not (ctx.caller.has_scope(INTERNAL_SCOPE) or ctx.caller.has_scope("admin")):
        raise ApiError("forbidden", "Only authorized Pub/Sub OIDC service accounts or admins may post usage events.")
    now = caps_svc.utcnow()
    entry = decode_push(ctx.body)
    if entry is None:
        return _ack("ignored", reason="invalid_payload")
    caps_cfg = ctx.config.get("caps") or {}
    log_id = caps_cfg.get("inference_log") or usage_svc.DEFAULT_AGY_LOG
    vertex_log = caps_cfg.get("vertex_inference_log") or usage_svc.DEFAULT_VERTEX_LOG
    directory = usage_svc.user_directory(ctx) if usage_svc.log_id_of(entry.get("logName")) != \
        urllib.parse.unquote(log_id) else {}
    item = entry_usage(entry, log_id, ctx.all_projects, vertex_log_id=vertex_log,
                       header_label=usage_svc.header_label_key(ctx), directory=directory,
                       long_ctx=int((ctx.config.get("pricing") or {}).get("long_context_threshold_tokens") or 200000))
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
    if row and row["status"] in ("blocked", "suspended") and row.get("status_scope", "all") == "all":
        return _ack("already_" + row["status"])
    prow = next((p for p in (row or {}).get("projects") or [] if p["project"] == item["project"]), None)
    if prow and prow["status"] in ("blocked", "suspended"):
        return _ack("already_" + prow["status"], project=item["project"])
    if row and (row.get("rule") or {}).get("type") == "allow":
        return _ack("allowed")

    last_counted = _last_counted(usage_res, person, item["surface"])
    unseen_items = TRACKER.unseen(person, last_counted, win["start"], item, surface=item["surface"])
    added = _aggregate_items(unseen_items)
    added_cost, _ = pricing_svc.estimate_cost(added, pricing)

    over_any = False
    thresholds = sorted(int(t) for t in (policy.get("webhook_thresholds") or [80, 100]))

    # 1) User total cap (cap_scope=total)
    lim = row["limit"] if row else {
        "cost_cap": float(policy.get("cost_cap") or 0.0) or None,
        "token_cap": int(policy.get("token_cap") or 0) or None,
        "request_cap": int(policy.get("request_cap") or 0) or None,
        "currency": pricing.currency,
    }
    if (row or {}).get("cap_scope", policy.get("cap_scope") or "total") == "total":
        base_tokens = (row["usage"]["total_tokens"] if row else 0) + added["total_tokens"]
        base_req = (row["usage"]["requests"] if row else 0) + added["requests"]
        base_cost_raw = row["usage"]["estimated_cost"] if row else (0.0 if pricing.configured else None)
        base_cost = None if base_cost_raw is None else round(float(base_cost_raw) + float(added_cost or 0.0), 6)
        over, ratio_val, _reasons = caps_svc.cap_check(
            base_tokens, base_req, int(lim.get("token_cap") or 0), int(lim.get("request_cap") or 0),
            base_cost, float(lim.get("cost_cap") or 0.0))
        if ratio_val is not None:
            for thr in thresholds:
                if ratio_val * 100.0 >= thr:
                    webhooks.notify_threshold_once(
                        ctx, email=person, period_start=win["start_iso"] or "all", threshold_pct=thr,
                        utilization_pct=ratio_val * 100.0,
                        usage={"requests": base_req, "total_tokens": base_tokens, "estimated_cost": base_cost},
                        limit=lim)
        over_any = over_any or bool(over)

    # 2) Cap of the project this request ran in (user assigned to several projects / per-project caps)
    p_lim = (prow or {}).get("limit")
    if p_lim is None:
        src = ((row or {}).get("rule") or {}).get("project_caps", {}).get(item["project"]) if (row or {}).get("rule") else None
        src = src or (policy.get("project_defaults") or {}).get(item["project"])
        if src:
            p_lim = {"cost_cap": src.get("cost_cap"), "token_cap": src.get("token_cap"),
                     "request_cap": src.get("request_cap"), "currency": pricing.currency}
        elif (policy.get("cap_scope") or "total") == "per_project":
            p_lim = {"cost_cap": float(policy.get("cost_cap") or 0.0) or None,
                     "token_cap": int(policy.get("token_cap") or 0) or None,
                     "request_cap": int(policy.get("request_cap") or 0) or None, "currency": pricing.currency}
    if p_lim:
        p_items = [it for it in unseen_items if it.get("project") == item["project"]]
        p_added = _aggregate_items(p_items)
        p_added_cost, _ = pricing_svc.estimate_cost(p_added, pricing)
        pu = (prow or {}).get("usage") or {"total_tokens": 0, "requests": 0, "estimated_cost": 0.0}
        p_tokens = int(pu.get("total_tokens") or 0) + p_added["total_tokens"]
        p_req = int(pu.get("requests") or 0) + p_added["requests"]
        p_cost_raw = pu.get("estimated_cost") if pu.get("estimated_cost") is not None else (0.0 if pricing.configured else None)
        p_cost = None if p_cost_raw is None else round(float(p_cost_raw) + float(p_added_cost or 0.0), 6)
        p_over, p_ratio, _ = caps_svc.cap_check(p_tokens, p_req, int(p_lim.get("token_cap") or 0),
                                                int(p_lim.get("request_cap") or 0), p_cost,
                                                float(p_lim.get("cost_cap") or 0.0))
        if p_ratio is not None:
            for thr in thresholds:
                if p_ratio * 100.0 >= thr:
                    webhooks.notify_threshold_once(
                        ctx, email=person, period_start=win["start_iso"] or "all", threshold_pct=thr,
                        utilization_pct=p_ratio * 100.0,
                        usage={"requests": p_req, "total_tokens": p_tokens, "estimated_cost": p_cost},
                        limit=p_lim, project=item["project"])
        over_any = over_any or bool(p_over)

    if not over_any:
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
