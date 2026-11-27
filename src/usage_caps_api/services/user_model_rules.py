"""Per-user model rules: allow/deny lists and per-model rate/volume limits, enforced by detection + IAM Deny block.

Google Cloud cannot restrict *which model* an individual user may call (IAM Deny conditions cannot target a model and
Organization Policy works per project/folder/org). This module closes that gap the same way per-key limits of
other AI vendor admin APIs feel to a customer portal:

  1. Rules: a workspace `default` rule, optional per-group rules (resolved by group priority) and per-user
     overrides (user > group > default):
       {mode: allow_list|deny_list|unrestricted|inherit, models: [patterns], surfaces: [...],
        limits: [{model, rpm, tpm, daily_requests, daily_tokens, monthly_tokens, monthly_cost}],
        on_violation: block_service|notify, block_services: [...], block_minutes, until, note}
  2. Detection: real-time (Pub/Sub push of Antigravity inference logs -> check_event, sub-minute) and the scheduled
     safety net (enforce cycle -> evaluate_all over today's and the current cap period's usage on every surface).
  3. Action (default `block_service`, per the customer requirement "block first, always notify"): the user is added to
     the IAM Deny v2 managed policy for the violating surface(s) (same engine as spend-cap blocks, ~1-2 min to apply),
     a `user.model_violation` webhook + audit entry is emitted, and the block lifts automatically at `until`
     (window reset / block_minutes) or on POST /v1/users/{email}/unblock.
  4. Multi-project: by default (`models.violation_block_scope: project`) the block applies only to the managed
     project(s) where the violating request ran - a user assigned to several projects keeps working elsewhere.
     Set `all` to block the user in every managed project.

Model patterns are matched against the model id recorded in the log; publisher-qualified patterns
(`anthropic/*`) use the publisher from the log resource / Model Garden, never a name-based guess.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import secrets

from .. import config, identity, timeutil
from ..httpkit import ApiError, iso_z, now_iso, paginate_list
from . import audit, model_registry as reg, webhooks

LOG = logging.getLogger("usage_caps_api.user_model_rules")

RULES_DOC = "user_model_rules"
MODES = ("allow_list", "deny_list", "unrestricted", "inherit")
LIMIT_METRICS = ("rpm", "tpm", "daily_requests", "daily_tokens", "monthly_requests", "monthly_tokens", "monthly_cost")
REALTIME_METRICS = ("rpm", "tpm")
DEFAULT_RATE_BLOCK_MINUTES = 15
MAX_VIOLATIONS = 2000
MAX_ACTIONED = 5000
MAX_RULE_MODELS = 200
MAX_LIMITS = 50


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _services(raw, field: str) -> list[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ApiError("bad_request", "Field '%s' must be a list of: %s." % (field, ", ".join(config.VALID_SERVICES)))
    out = []
    for s in raw:
        s = config.normalize_service(s)
        if s not in config.VALID_SERVICES:
            raise ApiError("bad_request", "Invalid service '%s' in '%s'." % (s, field))
        if s not in out:
            out.append(s)
    return out


def _num(val, field: str, *, integer: bool = True):
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, (int, float)) or (integer and not isinstance(val, int)) \
            or not math.isfinite(val) or val < 0 or val > 1e13:
        raise ApiError("bad_request", "Limit '%s' must be a non-negative %s." % (field, "integer" if integer else "number"))
    return int(val) if integer else float(val)


def validate_rule(ctx, body: dict, *, is_default: bool = False) -> dict:
    if not isinstance(body, dict):
        raise ApiError("bad_request", "Request body must be a JSON object.")
    mode = str(body.get("mode") or ("unrestricted" if is_default else "inherit")).strip().lower()
    if mode not in MODES or (is_default and mode == "inherit"):
        raise ApiError("bad_request", "Field 'mode' must be one of: %s." % ", ".join(
            [m for m in MODES if not (is_default and m == "inherit")]))
    models = body.get("models")
    if models is None:
        models = []
    if not isinstance(models, list) or len(models) > MAX_RULE_MODELS:
        raise ApiError("bad_request", "Field 'models' must be a list (max %d)." % MAX_RULE_MODELS)
    clean_models = []
    for m in models:
        reg.parse_model_ref(m)
        m = str(m).strip().lower()
        if m not in clean_models:
            clean_models.append(m)
    force = body.get("force", False)
    if not isinstance(force, bool):
        raise ApiError("bad_request", "Field 'force' must be a boolean.")
    if mode == "allow_list" and not clean_models and not force:
        raise ApiError("lockout_prevented", "An allow_list rule with no models blocks every model; pass force=true.")
    if mode == "deny_list" and not clean_models:
        raise ApiError("bad_request", "A deny_list rule needs at least one model.")
    limits_raw = body.get("limits")
    if limits_raw is None:
        limits_raw = []
    if not isinstance(limits_raw, list) or len(limits_raw) > MAX_LIMITS:
        raise ApiError("bad_request", "Field 'limits' must be a list (max %d)." % MAX_LIMITS)
    limits = []
    for item in limits_raw:
        if not isinstance(item, dict):
            raise ApiError("bad_request", "Each limit must be an object.")
        pattern = str(item.get("model") or "*").strip().lower()
        if pattern != "*":
            reg.parse_model_ref(pattern)
        lim = {"model": pattern}
        for metric in LIMIT_METRICS:
            v = _num(item.get(metric), metric, integer=(metric != "monthly_cost"))
            if v is not None:
                lim[metric] = v
        if len(lim) == 1:
            raise ApiError("bad_request", "Limit for '%s' must set at least one of: %s." % (pattern, ", ".join(LIMIT_METRICS)))
        limits.append(lim)
    on_violation = str(body.get("on_violation") or (ctx.config.get("models") or {}).get("violation_action")
                       or "block_service").strip().lower()
    if on_violation not in config.MODEL_VIOLATION_ACTIONS:
        raise ApiError("bad_request", "Field 'on_violation' must be block_service or notify.")
    block_minutes = body.get("block_minutes")
    if block_minutes is not None:
        block_minutes = _num(block_minutes, "block_minutes")
        if block_minutes > 525600:
            raise ApiError("bad_request", "Field 'block_minutes' must be <= 525600.")
    until = body.get("until")
    if until is not None and until != "":
        if not isinstance(until, str) or timeutil.parse_rfc3339(until) is None:
            raise ApiError("bad_request", "Field 'until' must be an RFC3339 timestamp.")
    else:
        until = None
    return {
        "mode": mode,
        "models": clean_models,
        "surfaces": _services(body.get("surfaces"), "surfaces") or list(config.VALID_SERVICES),
        "limits": limits,
        "on_violation": on_violation,
        "block_services": _services(body.get("block_services"), "block_services"),
        "block_minutes": block_minutes,
        "until": until,
        "note": str(body.get("note") or "")[:300],
        "updated_at": now_iso(),
        "updated_by": getattr(ctx.caller, "id", None),
    }


# ---------------------------------------------------------------------------
# Rule resolution & pure evaluation
# ---------------------------------------------------------------------------

def load_rules(ctx) -> dict:
    from . import groups as groups_svc
    doc, _ = ctx.store.load(RULES_DOC)
    groups = groups_svc.load_groups(ctx)
    return {
        "default": doc.get("default") if isinstance(doc.get("default"), dict) else None,
        "rules": {k: v for k, v in (doc.get("rules") or {}).items() if isinstance(v, dict)},
        "group_rules": {k: v for k, v in (doc.get("group_rules") or {}).items() if isinstance(v, dict) and k in groups},
        "membership": groups_svc.membership(groups),
        "actioned": dict(doc.get("actioned") or {}),
    }


def _expired(rule: dict | None, now: dt.datetime) -> bool:
    if not rule or not rule.get("until"):
        return False
    until = timeutil.parse_rfc3339(rule["until"])
    return until is not None and now >= until


def _has_rules(state: dict) -> bool:
    return bool(state.get("default") or state.get("rules") or state.get("group_rules"))


def effective_rule(state: dict, email: str, now: dt.datetime | None = None) -> tuple[dict | None, str]:
    """Resolution: user rule > first group rule (group priority) > workspace default.
    A rule with mode=inherit keeps the base allow/deny list and overrides only limits / actions."""
    now = now or utcnow()
    user = state["rules"].get(email)
    if user and not _expired(user, now) and user.get("mode") != "inherit":
        return user, "user"
    default = state.get("default")
    base, base_src = (default, "default") if (default and not _expired(default, now)) else (None, "none")
    for gid in (state.get("membership") or {}).get(email, []):
        grule = (state.get("group_rules") or {}).get(gid)
        if not grule or _expired(grule, now):
            continue
        if grule.get("mode") == "inherit":
            merged = dict(base or {"mode": "unrestricted", "models": [], "surfaces": list(config.VALID_SERVICES)})
            for k in ("limits", "on_violation", "block_services", "block_minutes"):
                if grule.get(k):
                    merged[k] = grule[k]
            base, base_src = merged, "group:%s+%s" % (gid, base_src)
        else:
            base, base_src = grule, "group:" + gid
        break
    if user and not _expired(user, now) and user.get("mode") == "inherit":
        merged = dict(base or {"mode": "unrestricted", "models": [], "surfaces": list(config.VALID_SERVICES)})
        for k in ("limits", "on_violation", "block_services", "block_minutes"):
            if user.get(k):
                merged[k] = user[k]
        return merged, "user+" + (base_src if base_src != "none" else "default")
    if base is not None:
        return base, base_src
    return None, "none"


def access_violation(rule: dict, model: str, surface: str, publisher: str | None = None) -> str | None:
    """Returns 'model_not_allowed' / 'model_denied' / None for one model use on one surface."""
    if not rule or surface not in (rule.get("surfaces") or config.VALID_SERVICES):
        return None
    mode = rule.get("mode")
    models = rule.get("models") or []
    if mode == "allow_list":
        if not any(_pattern_hit(model, p, publisher) for p in models):
            return "model_not_allowed"
    elif mode == "deny_list":
        if any(_pattern_hit(model, p, publisher) for p in models):
            return "model_denied"
    return None


def _pattern_hit(model: str, pattern: str, publisher: str | None = None) -> bool:
    if pattern.startswith("publishers/"):
        try:
            ppub, ppat, _ = reg.parse_model_ref(pattern)
        except ApiError:
            return False
    elif "/" in pattern:
        ppub, ppat = pattern.split("/", 1)
    else:
        ppub, ppat = None, pattern
    if ppub:
        actual = publisher or reg.guess_publisher(model)
        if actual is None or actual != ppub:
            return False
    return reg.matches(model, ppat)


def limit_violations(rule: dict, stats: dict, *, metrics=LIMIT_METRICS) -> list[dict]:
    """stats: {model: {minute_requests, minute_tokens, day_requests, day_tokens, month_requests, month_tokens,
    month_cost}} -> list of exceeded limits."""
    out = []
    for lim in (rule or {}).get("limits") or []:
        pattern = lim.get("model") or "*"
        matched = [m for m in stats if pattern == "*" or _pattern_hit(m, pattern)]
        if not matched:
            continue
        agg = {}
        for m in matched:
            for k, v in stats[m].items():
                if v is not None:
                    agg[k] = (agg.get(k) or 0) + v
        checks = (("rpm", "minute_requests"), ("tpm", "minute_tokens"), ("daily_requests", "day_requests"),
                  ("daily_tokens", "day_tokens"), ("monthly_requests", "month_requests"),
                  ("monthly_tokens", "month_tokens"), ("monthly_cost", "month_cost"))
        for metric, key in checks:
            if metric not in metrics or metric not in lim or agg.get(key) is None:
                continue
            if agg[key] >= lim[metric] > 0 or (lim[metric] == 0 and agg[key] > 0):
                out.append({"type": "limit_exceeded", "metric": metric, "model_pattern": pattern,
                            "limit": lim[metric], "observed": round(agg[key], 6), "models": sorted(matched)})
    return out


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _window_until(ctx, violation: dict, rule: dict, now: dt.datetime, period_win: dict | None) -> str | None:
    minutes = rule.get("block_minutes")
    if minutes is None:
        minutes = int((ctx.config.get("models") or {}).get("violation_block_minutes") or 0)
    if violation["type"] in ("model_not_allowed", "model_denied"):
        return iso_z(now + dt.timedelta(minutes=minutes)) if minutes else None
    metric = violation.get("metric")
    if metric in REALTIME_METRICS:
        return iso_z(now + dt.timedelta(minutes=minutes or DEFAULT_RATE_BLOCK_MINUTES))
    if metric and metric.startswith("daily"):
        tz = timeutil.get_zone(ctx.tz_name) or dt.timezone.utc
        local = now.astimezone(tz)
        nxt = (local + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return iso_z(nxt.astimezone(dt.timezone.utc))
    if period_win:
        return period_win.get("next_reset_iso")
    return None


def _violation_key(email: str, v: dict, window_id: str) -> str:
    target = v.get("model") or v.get("model_pattern") or "*"
    return "%s|%s|%s|%s|%s" % (email, v["type"], v.get("metric") or "-", target, window_id)


def _protected(ctx, email: str) -> bool:
    prot = {str(p).strip().lower() for p in (ctx.config.get("models") or {}).get("protected_principals") or [] if p}
    hub = ctx.config.get("hub") or {}
    for key in ("runtime_service_account", "enforcer_service_account"):
        sa = str(hub.get(key) or "").strip().lower()
        if sa:
            prot.add(sa)
    prot |= set(identity._STATE.get("protected") or ())  # pylint: disable=protected-access
    if email in prot:
        return True
    domain = email.split("@", 1)[-1] if "@" in email else ""
    return bool(domain) and ("*@" + domain) in prot


def _block_scope(ctx) -> str:
    val = str((ctx.config.get("models") or {}).get("violation_block_scope") or "project").lower()
    return val if val in ("project", "all") else "project"


def _merge_projects(a, b):
    if a is None or b is None:
        return None
    return sorted(set(a) | set(b))


def apply_violations(ctx, found: list[dict], *, trigger: str, dry_run: bool = False,
                     period_win: dict | None = None, population: int = 0) -> dict:
    """found: [{email, rule, violations: [...], surface_hits: set, window_id, counts}] -> blocks + notifications."""
    from . import caps as caps_svc
    now = utcnow()
    policy = caps_svc.effective_policy(ctx)
    backend = policy.get("enforcement_backend") or "iam_deny"
    state = load_rules(ctx)
    actioned = state["actioned"]
    caps_doc, _ = ctx.store.load(caps_svc.USER_CAPS_DOC)
    caps_rules = caps_svc._load_user_caps_state(ctx)["rules"]  # pylint: disable=protected-access (group-aware)
    budget_min = max(1, int(policy.get("change_budget_min") or 10))
    budget = max(budget_min, int(math.ceil(float(policy.get("change_budget_ratio") or 0.2) * max(1, population))))
    to_block: dict[str, dict] = {}
    records = []
    deferred = []
    for item in found:
        email = item["email"]
        rule = item["rule"] or {}
        for v in item["violations"]:
            key = _violation_key(email, v, item["window_id"])
            seen = actioned.get(key)
            count = int(v.get("requests") or v.get("observed") or 0)
            if seen and (v["type"] not in ("model_not_allowed", "model_denied") or count <= int(seen.get("count") or 0)):
                continue
            action = rule.get("on_violation") or "block_service"
            reason = None
            if action == "block_service":
                if backend == "notify_only":
                    action, reason = "notify", "enforcement_backend=notify_only"
                elif _protected(ctx, email) or (caps_rules.get(email) or {}).get("type") == "allow":
                    action, reason = "notify", "protected principal / allow override"
            v_projects = sorted({p for p in (v.get("projects") or []) if p and p != "unknown"})
            rec = {
                "object": "model_violation",
                "id": "mv_" + secrets.token_hex(8),
                "email": email,
                "type": v["type"],
                "model": v.get("model"),
                "publisher": v.get("publisher"),
                "surface": v.get("surface"),
                "projects": v_projects,
                "metric": v.get("metric"),
                "limit": v.get("limit"),
                "observed": v.get("observed"),
                "rule_source": item.get("rule_source"),
                "action": action,
                "action_note": reason,
                "trigger": trigger,
                "detected_at": iso_z(now),
                "window_id": item["window_id"],
            }
            if action == "block_service":
                services = rule.get("block_services") or sorted(item.get("surface_hits") or []) or \
                    list(policy.get("default_block_services") or config.VALID_SERVICES)
                until = _window_until(ctx, v, rule, now, period_win)
                b_projects = v_projects if (_block_scope(ctx) == "project" and v_projects) else None
                active = (caps_doc.get("blocks") or {}).get(email)
                if isinstance(active, dict) and not _expired(active, now) and \
                        set(services) <= set(active.get("services") or []) and \
                        (active.get("projects") is None or (b_projects is not None and
                                                            set(b_projects) <= set(active.get("projects") or []))):
                    rec["action"], rec["action_note"] = "already_blocked", active.get("source") or "manual"
                    records.append((key, count, rec))
                    continue
                cur = to_block.get(email)
                if cur is None:
                    if len(to_block) >= budget:
                        deferred.append(email)
                        rec["action"], rec["action_note"] = "deferred", "change budget (G4) reached"
                    else:
                        to_block[email] = {"services": set(services), "until": until, "violations": [rec],
                                           "projects": b_projects}
                else:
                    cur["services"] |= set(services)
                    cur["until"] = None if (cur["until"] is None or until is None) else max(cur["until"], until)
                    cur["projects"] = _merge_projects(cur.get("projects"), b_projects)
                    cur["violations"].append(rec)
                rec["block_services"] = sorted(services)
                rec["block_projects"] = b_projects or "all"
                rec["block_until"] = until
            records.append((key, count, rec))
    result = {"violations": [r for _, _, r in records if r["action"] != "already_blocked"],
              "blocked": sorted(to_block), "deferred": sorted(set(deferred)),
              "dry_run": dry_run}
    if dry_run or not records:
        return result

    def save_rules(doc):
        led = dict(doc.get("actioned") or {})
        for key, count, rec in records:
            if rec["action"] != "deferred":
                led[key] = {"count": count, "at": rec["detected_at"]}
        if len(led) > MAX_ACTIONED:
            for k in list(led.keys())[:len(led) - MAX_ACTIONED]:
                del led[k]
        doc["actioned"] = led
        hist = list(doc.get("violations") or [])
        hist.extend(r for _, _, r in records if r["action"] != "already_blocked")
        doc["violations"] = hist[-MAX_VIOLATIONS:]
        return doc

    ctx.store.mutate(RULES_DOC, save_rules)
    enforcement = None
    if to_block:
        with caps_svc._WRITE_LOCK:  # pylint: disable=protected-access
            def save_blocks(doc):
                blocks = dict(doc.get("blocks") or {})
                for email, b in to_block.items():
                    existing = blocks.get(email)
                    if existing and existing.get("source") != "model_policy":
                        merged = set(existing.get("services") or []) | b["services"]
                        blocks[email] = dict(existing, services=sorted(merged),
                                             projects=_merge_projects(existing.get("projects"), b.get("projects")))
                        continue
                    until = b["until"]
                    projects = b.get("projects")
                    if existing and existing.get("source") == "model_policy":
                        b["services"] |= set(existing.get("services") or [])
                        projects = _merge_projects(existing.get("projects"), projects)
                        if existing.get("until") is None or until is None:
                            until = None
                        else:
                            until = max(existing["until"], until)
                    first = b["violations"][0]
                    blocks[email] = {
                        "email": email,
                        "services": sorted(b["services"]),
                        "projects": projects,
                        "until": until,
                        "reason": "model policy violation: %s %s" % (first["type"], first.get("model") or
                                                                     first.get("metric") or ""),
                        "source": "model_policy",
                        "violation_ids": [r["id"] for r in b["violations"]][:20],
                        "updated_at": now_iso(),
                        "updated_by": "system:model-rules",
                    }
                doc["blocks"] = blocks
                return doc
            doc = ctx.store.mutate(caps_svc.USER_CAPS_DOC, save_blocks)
            try:
                enforcement = caps_svc._reconcile_direct_change(ctx, policy, doc)  # pylint: disable=protected-access
            except Exception as err:  # pylint: disable=broad-except
                enforcement = {"applied": False, "error": type(err).__name__}
            caps_svc._invalidate_caps_caches(ctx)  # pylint: disable=protected-access
    result["enforcement"] = enforcement
    for _, _, rec in records:
        if rec["action"] == "already_blocked":
            continue
        audit.write_audit(ctx, "user_model_rule.violation", target=rec["email"], details={
            k: rec.get(k) for k in ("type", "model", "publisher", "surface", "projects", "metric", "limit", "observed",
                                    "action", "block_services", "block_projects", "block_until", "trigger")})
        if (ctx.config.get("models") or {}).get("violation_notify", True):
            webhooks.dispatch_event(ctx, "user.model_violation", dict(rec, enforcement_applied=bool(
                enforcement and enforcement.get("applied")) if rec["action"] == "block_service" else False))
    for email, b in to_block.items():
        webhooks.dispatch_event(ctx, "user.blocked", {
            "email": email, "services": sorted(b["services"]), "until": b["until"], "trigger": "model_policy",
            "projects": b.get("projects") or "all",
            "reasons": sorted({r["type"] for r in b["violations"]}),
        })
    return result


# ---------------------------------------------------------------------------
# Detection: scheduled & real-time
# ---------------------------------------------------------------------------

def _day_window(ctx, now: dt.datetime) -> tuple[dt.datetime, str]:
    tz = timeutil.get_zone(ctx.tz_name) or dt.timezone.utc
    local = now.astimezone(tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc)
    return start, local.strftime("%Y-%m-%d")


def evaluate_all(ctx, *, dry_run: bool = False, now: dt.datetime | None = None, trigger: str = "scheduler") -> dict:
    """Scheduled safety net: evaluates every user's model rules against today's and this period's usage."""
    from . import caps as caps_svc, pricing as pricing_svc, usage as usage_svc
    now = now or utcnow()
    state = load_rules(ctx)
    if not _has_rules(state):
        return {"object": "model_rule_evaluation", "evaluated": 0, "violations": [], "blocked": [], "skipped": "no_rules"}
    policy = caps_svc.effective_policy(ctx)
    period = caps_svc._period_info(ctx, policy, now)  # pylint: disable=protected-access
    day_start, day_id = _day_window(ctx, now)
    need_month = any(any(k.startswith("monthly") for lim in (r or {}).get("limits") or [] for k in lim)
                     for r in [state["default"], *state["rules"].values(), *state["group_rules"].values()] if r)
    day_usage = usage_svc.collect_usage(ctx.child(params={}), day_start, now, fresh=True)
    month_usage = usage_svc.collect_usage(ctx.child(params={}), period["start"], now, fresh=True) if need_month else None
    pricing = pricing_svc.resolve_pricing(ctx, policy) if need_month else None
    people = set(day_usage.users) | set((month_usage.users if month_usage else {}))
    found = []
    for email in sorted(people):
        if not identity.is_person_key(email):
            continue
        rule, source = effective_rule(state, email, now)
        if not rule:
            continue
        violations, hits = [], set()
        rec = day_usage.users.get(email) or {}
        model_projects: dict[str, set] = {}
        model_surfaces: dict[str, set] = {}
        for proj, prec in (rec.get("by_project") or {}).items():
            for model, pmrec in (prec.get("by_model") or {}).items():
                model_projects.setdefault(model, set()).add(proj)
                if pmrec.get("surface"):
                    model_surfaces.setdefault(model, set()).add(pmrec["surface"])
        for model, mrec in (rec.get("by_model") or {}).items():
            surface = mrec.get("surface") or "antigravity"
            model_surfaces.setdefault(model, set()).add(surface)
            kind = access_violation(rule, model, surface, mrec.get("publisher"))
            if kind:
                violations.append({"type": kind, "model": model, "publisher": mrec.get("publisher"),
                                   "surface": surface, "projects": sorted(model_projects.get(model) or ()),
                                   "requests": mrec.get("requests", 0), "observed": mrec.get("requests", 0)})
                hits.add(surface)
        stats = {}
        for model, mrec in (rec.get("by_model") or {}).items():
            stats.setdefault(model, {})
            stats[model].update(day_requests=mrec.get("requests", 0), day_tokens=mrec.get("total_tokens", 0))
        if month_usage is not None:
            mrec_all = month_usage.users.get(email) or {}
            for proj, prec in (mrec_all.get("by_project") or {}).items():
                for model, pmrec in (prec.get("by_model") or {}).items():
                    model_projects.setdefault(model, set()).add(proj)
                    if pmrec.get("surface"):
                        model_surfaces.setdefault(model, set()).add(pmrec["surface"])
            for model, mrec in (mrec_all.get("by_model") or {}).items():
                if mrec.get("surface"):
                    model_surfaces.setdefault(model, set()).add(mrec["surface"])
                s = stats.setdefault(model, {})
                s.update(month_requests=mrec.get("requests", 0), month_tokens=mrec.get("total_tokens", 0))
                if pricing is not None:
                    cost, _ = pricing_svc.estimate_cost({"by_model": {model: mrec}}, pricing)
                    s["month_cost"] = cost
        for lv in limit_violations(rule, stats, metrics=[m for m in LIMIT_METRICS if m not in REALTIME_METRICS]):
            lv["projects"] = sorted({p for m in lv["models"] for p in (model_projects.get(m) or ())})
            violations.append(lv)
            for m in lv["models"]:
                hits |= (model_surfaces.get(m) or {"antigravity"})
        if violations:
            window_id = day_id
            found.append({"email": email, "rule": rule, "rule_source": source, "violations": violations,
                          "surface_hits": hits, "window_id": window_id})
    res = apply_violations(ctx, found, trigger=trigger, dry_run=dry_run, period_win=period, population=len(people))

    def mark(doc):
        doc["last_eval"] = {"at": iso_z(now), "trigger": trigger, "dry_run": dry_run, "users": len(people),
                            "violations": len(res["violations"]), "blocked": len(res["blocked"]),
                            "telemetry_ok": day_usage.ok}
        return doc
    if not dry_run:
        ctx.store.mutate(RULES_DOC, mark)
    return dict({"object": "model_rule_evaluation", "evaluated": len(people), "telemetry_ok": day_usage.ok}, **res)


def check_event(ctx, item: dict, recent_items: list[dict]) -> dict | None:
    """Real-time check for one Antigravity inference event. recent_items: the user's events seen in the last minutes."""
    now = utcnow()
    state = load_rules(ctx)
    if not _has_rules(state):
        return None
    email = item["person"]
    rule, source = effective_rule(state, email, now)
    if not rule:
        return None
    from . import caps as caps_svc
    caps_doc, _ = ctx.store.load(caps_svc.USER_CAPS_DOC)
    surface = item.get("surface") or "antigravity"
    project = item.get("project")
    blk = (caps_doc.get("blocks") or {}).get(email)
    if isinstance(blk, dict) and surface in (blk.get("services") or []) and not _expired(blk, now) and \
            (blk.get("projects") is None or project in (blk.get("projects") or [])):
        return None
    violations = []
    kind = access_violation(rule, item["model"], surface, item.get("publisher"))
    if kind:
        violations.append({"type": kind, "model": item["model"], "publisher": item.get("publisher"),
                           "surface": surface, "projects": [project] if project else [], "requests": 1,
                           "observed": 1, "event_key": item.get("key")})
    if any(m in lim for lim in rule.get("limits") or [] for m in REALTIME_METRICS):
        floor = item["ts"] - dt.timedelta(seconds=60)
        window = [it for it in recent_items if it["ts"] >= floor]
        if not any(it.get("key") == item.get("key") for it in window):
            window.append(item)
        stats = {}
        for it in window:
            s = stats.setdefault(it["model"], {"minute_requests": 0, "minute_tokens": 0})
            s["minute_requests"] += 1
            s["minute_tokens"] += int(it.get("total_tokens") or 0)
        violations += [dict(v, surface=surface, projects=[project] if project else [])
                       for v in limit_violations(rule, stats, metrics=REALTIME_METRICS)]
    if not violations:
        return None
    _, day_id = _day_window(ctx, now)
    window_id = day_id if not any(v.get("metric") in REALTIME_METRICS for v in violations) else \
        now.strftime("%Y-%m-%dT%H:%M")
    found = [{"email": email, "rule": rule, "rule_source": source, "violations": violations,
              "surface_hits": {surface}, "window_id": window_id}]
    if kind:
        # Count-based dedupe: every new disallowed request after an admin unblock is a new violation.
        state_led = state["actioned"].get(_violation_key(email, violations[0], window_id)) or {}
        violations[0]["requests"] = violations[0]["observed"] = int(state_led.get("count") or 0) + 1
    return apply_violations(ctx, found, trigger="usage_event", population=1)


# ---------------------------------------------------------------------------
# REST handlers
# ---------------------------------------------------------------------------

def _email(ctx) -> str:
    email = identity.person_key(ctx.path_params.get("email") or "")
    if not email:
        raise ApiError("bad_request", "Path parameter 'email' must be a valid email or workforce principal.")
    return email


def _rule_view(rule: dict | None) -> dict | None:
    return dict(rule) if rule else None


def get_default_rule(ctx) -> dict:
    """GET /v1/model-rules/default"""
    state = load_rules(ctx)
    return {"object": "model_rule", "scope": "default", "rule": _rule_view(state["default"])}


def put_default_rule(ctx) -> dict:
    """PUT /v1/model-rules/default"""
    rule = validate_rule(ctx, ctx.body or {}, is_default=True)

    def save(doc):
        doc["default"] = rule
        return doc
    ctx.store.mutate(RULES_DOC, save)
    audit.write_audit(ctx, "user_model_rule.default_updated", target="default", details=rule)
    return {"object": "model_rule", "scope": "default", "rule": rule}


def delete_default_rule(ctx) -> dict:
    """DELETE /v1/model-rules/default"""
    def save(doc):
        existed = doc.get("default") is not None
        doc["default"] = None
        return existed
    existed = ctx.store.mutate(RULES_DOC, save)
    if not existed:
        raise ApiError("not_found", "No default model rule is configured.")
    audit.write_audit(ctx, "user_model_rule.default_deleted", target="default")
    return {"object": "model_rule", "scope": "default", "deleted": True}


def list_user_rules(ctx) -> dict:
    """GET /v1/model-rules?email="""
    state = load_rules(ctx)
    rows = [{"object": "model_rule", "scope": "user", "email": em, "rule": r} for em, r in sorted(state["rules"].items())]
    q = (ctx.params.get("email") or "").strip().lower()
    if q:
        rows = [r for r in rows if r["email"] == q]
    page = paginate_list(rows, ctx.params, default_limit=100, max_limit=500)
    page["default"] = state["default"]
    return page


def get_user_rule(ctx) -> dict:
    """GET /v1/users/{email}/model-rules: own rule, effective rule, today's per-model usage and recent violations."""
    from . import usage as usage_svc
    email = _email(ctx)
    state = load_rules(ctx)
    rule, source = effective_rule(state, email)
    now = utcnow()
    day_start, _ = _day_window(ctx, now)
    today = []
    try:
        res = usage_svc.collect_usage(ctx.child(params={}), day_start, now, email=email)
        for model, mrec in sorted(((res.users.get(email) or {}).get("by_model") or {}).items()):
            surface = mrec.get("surface") or "antigravity"
            today.append({"model": model, "surface": surface, "requests": mrec.get("requests", 0),
                          "total_tokens": mrec.get("total_tokens", 0),
                          "status": access_violation(rule, model, surface) or "allowed"})
    except Exception:  # pylint: disable=broad-except
        today = None
    doc, _ = ctx.store.load(RULES_DOC)
    recent = [v for v in reversed(doc.get("violations") or []) if v.get("email") == email][:50]
    return {"object": "user_model_rules", "email": email, "rule": state["rules"].get(email),
            "effective_rule": rule, "effective_source": source, "today": today, "recent_violations": recent}


def put_user_rule(ctx) -> dict:
    """PUT /v1/users/{email}/model-rules"""
    email = _email(ctx)
    rule = validate_rule(ctx, ctx.body or {})

    def save(doc):
        rules = dict(doc.get("rules") or {})
        rules[email] = rule
        doc["rules"] = rules
        return doc
    ctx.store.mutate(RULES_DOC, save)
    audit.write_audit(ctx, "user_model_rule.updated", target=email, details=rule)
    return {"object": "model_rule", "scope": "user", "email": email, "rule": rule}


def delete_user_rule(ctx) -> dict:
    """DELETE /v1/users/{email}/model-rules"""
    email = _email(ctx)

    def save(doc):
        rules = dict(doc.get("rules") or {})
        existed = rules.pop(email, None) is not None
        doc["rules"] = rules
        return existed
    if not ctx.store.mutate(RULES_DOC, save):
        raise ApiError("not_found", "No model rule found for '%s'." % email)
    audit.write_audit(ctx, "user_model_rule.deleted", target=email)
    return {"object": "model_rule", "scope": "user", "email": email, "deleted": True}


def get_user_rate_limits(ctx) -> dict:
    """GET /v1/rate-limits/users/{email}: the per-model limits slice of the user's effective model rule."""
    email = _email(ctx)
    state = load_rules(ctx)
    rule, source = effective_rule(state, email)
    return {"object": "user_rate_limits", "email": email, "limits": (rule or {}).get("limits") or [],
            "source": source, "on_violation": (rule or {}).get("on_violation")}


def put_user_rate_limits(ctx) -> dict:
    """PUT /v1/rate-limits/users/{email} {limits: [...], on_violation?, block_minutes?}"""
    email = _email(ctx)
    patch_fields = {k: v for k, v in (ctx.body or {}).items()
                    if k in ("limits", "on_violation", "block_minutes", "block_services")}

    def save(doc):
        rules = dict(doc.get("rules") or {})
        base = dict(rules.get(email) if isinstance(rules.get(email), dict) else {"mode": "inherit"})
        rule = validate_rule(ctx, dict(base, **patch_fields))
        rules[email] = rule
        doc["rules"] = rules
        return rule
    rule = ctx.store.mutate(RULES_DOC, save)
    audit.write_audit(ctx, "user_rate_limit.updated", target=email, details={"limits": rule["limits"]})
    return {"object": "user_rate_limits", "email": email, "limits": rule["limits"], "source": "user",
            "on_violation": rule["on_violation"]}


def delete_user_rate_limits(ctx) -> dict:
    """DELETE /v1/rate-limits/users/{email}: clears the user's own limits (default rule limits apply again)."""
    email = _email(ctx)

    def save(doc):
        rules = dict(doc.get("rules") or {})
        cur = rules.get(email)
        if not isinstance(cur, dict) or not cur.get("limits"):
            return False
        r = dict(cur)
        r["limits"] = []
        if r.get("mode") in (None, "inherit") and not r.get("models"):
            rules.pop(email, None)
        else:
            rules[email] = r
        doc["rules"] = rules
        return True
    if not ctx.store.mutate(RULES_DOC, save):
        raise ApiError("not_found", "No per-user rate limits found for '%s'." % email)
    audit.write_audit(ctx, "user_rate_limit.deleted", target=email)
    return {"object": "user_rate_limits", "email": email, "deleted": True}


def list_violations(ctx) -> dict:
    """GET /v1/model-violations?email=&type=&action="""
    doc, _ = ctx.store.load(RULES_DOC)
    rows = list(reversed(doc.get("violations") or []))
    for field in ("email", "type", "action", "surface"):
        val = (ctx.params.get(field) or "").strip().lower()
        if val:
            rows = [r for r in rows if str(r.get(field) or "").lower() == val]
    return paginate_list(rows, ctx.params, default_limit=100, max_limit=500)


def evaluate_handler(ctx) -> dict:
    """POST /v1/model-rules/evaluate {dryRun?: bool (default true)}"""
    body = ctx.body or {}
    dry_run = body.get("dryRun", body.get("dry_run", True))
    if not isinstance(dry_run, bool):
        raise ApiError("bad_request", "Field 'dryRun' must be a boolean.")
    return evaluate_all(ctx, dry_run=dry_run, trigger="api")


def users_affected_by_model(ctx, model: str) -> list[dict]:
    state = load_rules(ctx)
    out = []
    if state["default"]:
        v = access_violation(state["default"], model, "antigravity") or access_violation(state["default"], model, "gemini_platform")
        out.append({"scope": "default", "decision": "denied" if v else "allowed"})
    for email, rule in sorted(state["rules"].items()):
        if rule.get("mode") in ("allow_list", "deny_list"):
            v = access_violation(rule, model, "antigravity") or access_violation(rule, model, "gemini_platform")
            out.append({"scope": "user", "email": email, "decision": "denied" if v else "allowed"})
    for gid, rule in sorted(state["group_rules"].items()):
        if rule.get("mode") in ("allow_list", "deny_list"):
            v = access_violation(rule, model, "antigravity") or access_violation(rule, model, "gemini_platform")
            out.append({"scope": "group", "group": gid, "decision": "denied" if v else "allowed"})
    return out[:200]
