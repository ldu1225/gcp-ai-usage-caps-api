"""Scheduled governance cycle run after every spend-cap enforcement cycle (Cloud Run Enforcer Job / API).

Steps (each isolated; one failure never stops the others):
  0. Google Group membership sync (hourly, Cloud Identity API).
  1. Per-user model rules safety net (detect disallowed model use / exceeded per-model limits -> block + notify).
  2. Quota request status refresh (pending -> approved / partially_approved / denied) + webhooks.
  3. Rate-limit headroom thresholds (hourly) -> `rate_limit.threshold_reached` webhooks.
  4. Model access intent reconcile (hourly): re-expands patterns such as `<family>-*` when new models appear in the live catalog.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..httpkit import now_iso
from ..timeutil import parse_rfc3339

LOG = logging.getLogger("usage_caps_api.governance_cycle")

STATE_DOC = "governance_cycle"
RECONCILE_INTERVAL = dt.timedelta(hours=1)


def _step(name: str, fn) -> dict:
    try:
        out = fn()
        if isinstance(out, dict):
            keep = {k: v for k, v in out.items() if k in ("evaluated", "blocked", "deferred", "checked", "changed",
                                                          "notified", "applied", "skipped", "telemetry_ok", "dry_run")}
            if "violations" in out:
                keep["violations"] = len(out["violations"])
            return dict(keep, ok=True)
        return {"ok": True}
    except Exception as err:  # pylint: disable=broad-except
        LOG.warning("governance step %s failed: %s", name, type(err).__name__)
        return {"ok": False, "error": getattr(err, "code", None) or getattr(err, "kind", None) or type(err).__name__}


def run(ctx, *, dry_run: bool = False, trigger: str = "scheduler") -> dict:
    from . import groups, model_access, quotas, user_model_rules
    out = {"model_rules": _step("model_rules", lambda: user_model_rules.evaluate_all(ctx, dry_run=dry_run,
                                                                                     trigger=trigger))}
    if dry_run:
        return out
    out["group_sync"] = _step("group_sync", lambda: groups.sync_all(ctx))
    out["quota_requests"] = _step("quota_requests", lambda: quotas.refresh_pending(ctx))
    out["rate_limit_thresholds"] = _step("rate_limit_thresholds", lambda: quotas.check_thresholds(ctx))
    out["project_caps"] = _step("project_caps", lambda: notify_enforced_project_caps(ctx))
    out["model_lifecycle"] = _step("model_lifecycle", lambda: notify_model_lifecycle(ctx))
    doc, _ = ctx.store.load(STATE_DOC)
    last = parse_rfc3339(doc.get("last_reconcile")) if doc.get("last_reconcile") else None
    now = dt.datetime.now(dt.timezone.utc)
    if last is None or now - last >= RECONCILE_INTERVAL:
        out["model_access_reconcile"] = _step("model_access_reconcile", lambda: model_access.reconcile(ctx))

        def mark(d):
            d["last_reconcile"] = now_iso()
            return d
        ctx.store.mutate(STATE_DOC, mark)
    return out


def notify_enforced_project_caps(ctx) -> dict:
    """`project_cap.enforced` once per project per day while Google Cloud reports the spend cap as ENFORCED."""
    from . import spend_caps, webhooks
    if not spend_caps.enabled(ctx.config):
        return {"skipped": "spend_caps_disabled"}
    live = spend_caps.read_live_caps(ctx)
    if not live.get("ok"):
        return {"skipped": "unreadable"}
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    hits = 0
    for project, item in (live.get("items") or {}).items():
        if item.get("output_state") == spend_caps.ENFORCED:
            if webhooks.notify_once(ctx, "%s|%s" % (project, day), "project_cap.enforced", {
                    "project_id": project, "budget": item.get("budget"), "amount": item.get("amount"),
                    "currency": item.get("currency")}):
                hits += 1
    return {"notified": hits}


LIFECYCLE_NOTICE_DAYS = 90


def notify_model_lifecycle(ctx) -> dict:
    """`model.lifecycle_notice` once per model/date when a model has a deprecation or shutdown date within 90 days
    (dates come from Model Garden discovery or `models.lifecycle_overrides`)."""
    from . import model_registry, webhooks
    from ..timeutil import parse_date
    catalog = model_registry.load_catalog(ctx, allow_discovery=False)
    today = dt.datetime.now(dt.timezone.utc).date()
    hits = 0
    for mid, entry in catalog.items():
        for field in ("deprecation_date", "shutdown_date"):
            raw = entry.get(field)
            if not raw:
                continue
            try:
                when = parse_date(str(raw), field)
            except Exception:  # pylint: disable=broad-except
                continue
            days = (when - today).days
            if days <= LIFECYCLE_NOTICE_DAYS:
                if webhooks.notify_once(ctx, "%s|%s|%s" % (mid, field, raw), "model.lifecycle_notice", {
                        "model": mid, "publisher": entry.get("publisher"), "event": field, "date": str(raw),
                        "days_remaining": days, "replacement": entry.get("replacement")}):
                    hits += 1
    return {"notified": hits}
