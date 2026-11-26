"""Spend cap policy, per-user cap status, safety gates (G1-G7), auto/manual enforcement, and Block/Unblock."""

from __future__ import annotations

import copy
import datetime as dt
import logging
import math
import threading

from .. import config, identity, timeutil
from ..gcp import GcpError
from ..httpkit import ApiError, now_iso, paginate_list
from . import audit, enforcers, pricing as pricing_svc, usage as usage_svc, webhooks
from .base import iso, to_int

LOG = logging.getLogger("usage_caps_api.caps")

POLICY_DOC = "policy"
USER_CAPS_DOC = "user_caps"
RUN_V2 = "https://run.googleapis.com/v2"
_WRITE_LOCK = threading.Lock()


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def effective_policy(ctx) -> dict:
    """Global cap policy = config defaults merged with persisted overrides in policy.json."""
    base = copy.deepcopy(ctx.config.get("caps") or {})
    doc, _ = ctx.store.load(POLICY_DOC)
    overrides = doc.get("policy") if isinstance(doc.get("policy"), dict) else {}
    for k, v in overrides.items():
        if v is not None:
            base[k] = copy.deepcopy(v)
    return base


def _validate_services(raw, *, default=None, allow_empty: bool = False) -> list[str]:
    if raw is None:
        return list(default if default is not None else config.VALID_SERVICES)
    if not isinstance(raw, list):
        raise ApiError("bad_request", "Field 'services' must be a list of service names.")
    out = []
    for item in raw:
        s = str(item or "").strip().lower()
        if s not in config.VALID_SERVICES:
            raise ApiError("bad_request", "Invalid service '%s'. Valid services: %s."
                           % (s, ", ".join(config.VALID_SERVICES)))
        if s not in out:
            out.append(s)
    if not out and not allow_empty:
        raise ApiError("bad_request", "At least one service must be specified.")
    return out


def _opt_nonneg_float(val, field: str, hi: float = 1e9) -> float | None:
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or val < 0 or val > hi:
        raise ApiError("bad_request", "Field '%s' must be a number between 0 and %g." % (field, hi))
    return float(val)


def _opt_nonneg_int(val, field: str, hi: int = 10 ** 12) -> int | None:
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, int) or val < 0 or val > hi:
        raise ApiError("bad_request", "Field '%s' must be an integer between 0 and %d." % (field, hi))
    return int(val)


def _period_info(ctx, policy: dict, now: dt.datetime | None = None) -> dict:
    tz = policy.get("reset_timezone") or ctx.tz_name
    return timeutil.period_window(
        period=policy.get("period") or "monthly",
        window=policy.get("window") or "calendar",
        now=now or utcnow(),
        tz=tz,
        weekday=int(policy.get("reset_weekday") or 0),
        at=str(policy.get("reset_time") or "00:00"),
        day=int(policy.get("reset_day") or 1),
        cycle_days=int(policy.get("cycle_days") or 30),
        cycle_start=str(policy.get("cycle_start") or ""),
    )


# ---------------------------------------------------------------------------
# GET /v1/caps/policy & PUT /v1/caps/policy
# ---------------------------------------------------------------------------

def public_policy_view(ctx, policy: dict, now: dt.datetime | None = None) -> dict:
    win = _period_info(ctx, policy, now)
    return {
        "object": "cap_policy",
        "cost_cap": float(policy.get("cost_cap") or 0.0),
        "token_cap": int(policy.get("token_cap") or 0),
        "request_cap": int(policy.get("request_cap") or 0),
        "currency": str(policy.get("currency") or "USD").upper(),
        "period": policy.get("period") or "monthly",
        "window": policy.get("window") or "calendar",
        "reset_timezone": policy.get("reset_timezone") or ctx.tz_name,
        "reset_day": int(policy.get("reset_day") or 1),
        "reset_weekday": int(policy.get("reset_weekday") or 0),
        "reset_time": str(policy.get("reset_time") or "00:00"),
        "cycle_days": int(policy.get("cycle_days") or 30),
        "cycle_start": str(policy.get("cycle_start") or ""),
        "enforcement_mode": policy.get("enforcement_mode") or "manual",
        "enforcement_backend": policy.get("enforcement_backend") or "iam_deny",
        "default_block_services": list(policy.get("default_block_services") or config.VALID_SERVICES),
        "webhook_thresholds": list(policy.get("webhook_thresholds") or [80, 100]),
        "change_budget_min": int(policy.get("change_budget_min") or 10),
        "change_budget_ratio": float(policy.get("change_budget_ratio") or 0.2),
        "surge_guard_min": int(policy.get("surge_guard_min") or 10),
        "surge_guard_ratio": float(policy.get("surge_guard_ratio") or 0.5),
        "current_window": {
            "start": win["start_iso"],
            "end": win["period_end_iso"],
            "next_reset": win["next_reset_iso"],
        },
    }


def get_policy(ctx) -> dict:
    return public_policy_view(ctx, effective_policy(ctx))


def update_policy(ctx) -> dict:
    body = ctx.body or {}
    updates: dict = {}
    if "cost_cap" in body:
        updates["cost_cap"] = _opt_nonneg_float(body["cost_cap"], "cost_cap")
    if "token_cap" in body:
        updates["token_cap"] = _opt_nonneg_int(body["token_cap"], "token_cap")
    if "request_cap" in body:
        updates["request_cap"] = _opt_nonneg_int(body["request_cap"], "request_cap", hi=10 ** 9)
    if "currency" in body:
        cur = str(body["currency"] or "").strip().upper()
        if not config.CURRENCY_RE.match(cur):
            raise ApiError("bad_request", "Field 'currency' must be a 3-letter ISO currency code.")
        updates["currency"] = cur
    if "period" in body:
        if body["period"] not in timeutil.PERIODS:
            raise ApiError("bad_request", "Field 'period' must be one of: %s." % ", ".join(timeutil.PERIODS))
        updates["period"] = body["period"]
    if "window" in body:
        if body["window"] not in timeutil.WINDOWS:
            raise ApiError("bad_request", "Field 'window' must be 'calendar' or 'rolling'.")
        updates["window"] = body["window"]
    if "reset_timezone" in body:
        tz = str(body["reset_timezone"] or "").strip()
        if tz and not timeutil.valid_timezone(tz):
            raise ApiError("bad_request", "Field 'reset_timezone' must be a valid IANA timezone.")
        updates["reset_timezone"] = tz
    if "reset_day" in body:
        rd = _opt_nonneg_int(body["reset_day"], "reset_day", hi=31)
        if rd is None or rd < 1:
            raise ApiError("bad_request", "Field 'reset_day' must be between 1 and 31.")
        updates["reset_day"] = rd
    if "reset_weekday" in body:
        rw = _opt_nonneg_int(body["reset_weekday"], "reset_weekday", hi=6)
        updates["reset_weekday"] = rw
    if "reset_time" in body:
        rt = str(body["reset_time"] or "").strip()
        if not timeutil.RESET_TIME_RE.match(rt):
            raise ApiError("bad_request", "Field 'reset_time' must be HH:MM (00:00-23:59).")
        updates["reset_time"] = rt
    if "cycle_days" in body:
        cd = _opt_nonneg_int(body["cycle_days"], "cycle_days", hi=92)
        if cd is None or cd < 1:
            raise ApiError("bad_request", "Field 'cycle_days' must be between 1 and 92.")
        updates["cycle_days"] = cd
    if "cycle_start" in body:
        cs = str(body["cycle_start"] or "").strip()
        if cs:
            timeutil.parse_date(cs, "cycle_start")
        updates["cycle_start"] = cs
    if "enforcement_mode" in body:
        if body["enforcement_mode"] not in config.ENFORCEMENT_MODES:
            raise ApiError("bad_request", "Field 'enforcement_mode' must be 'manual' or 'auto'.")
        updates["enforcement_mode"] = body["enforcement_mode"]
    if "enforcement_backend" in body:
        if body["enforcement_backend"] not in config.ENFORCEMENT_BACKENDS:
            raise ApiError("bad_request", "Field 'enforcement_backend' must be 'iam_deny' or 'notify_only'.")
        updates["enforcement_backend"] = body["enforcement_backend"]
    if "default_block_services" in body:
        updates["default_block_services"] = _validate_services(body["default_block_services"])
    if "webhook_thresholds" in body:
        raw_t = body["webhook_thresholds"]
        if not isinstance(raw_t, list) or len(raw_t) > 10:
            raise ApiError("bad_request", "Field 'webhook_thresholds' must be a list of up to 10 integers (1-100).")
        clean_t = sorted({int(x) for x in raw_t if isinstance(x, int) and not isinstance(x, bool) and 1 <= x <= 100})
        if len(clean_t) != len(set(raw_t)):
            raise ApiError("bad_request", "All webhook_thresholds must be integers between 1 and 100.")
        updates["webhook_thresholds"] = clean_t
    if "change_budget_min" in body:
        cbm = _opt_nonneg_int(body["change_budget_min"], "change_budget_min", hi=10000)
        if cbm is None or cbm < 1:
            raise ApiError("bad_request", "Field 'change_budget_min' must be >= 1.")
        updates["change_budget_min"] = cbm
    if "change_budget_ratio" in body:
        cbr = _opt_nonneg_float(body["change_budget_ratio"], "change_budget_ratio", hi=1.0)
        updates["change_budget_ratio"] = cbr
    if "surge_guard_min" in body:
        sgm = _opt_nonneg_int(body["surge_guard_min"], "surge_guard_min", hi=10000)
        if sgm is None or sgm < 1:
            raise ApiError("bad_request", "Field 'surge_guard_min' must be >= 1.")
        updates["surge_guard_min"] = sgm
    if "surge_guard_ratio" in body:
        sgr = _opt_nonneg_float(body["surge_guard_ratio"], "surge_guard_ratio", hi=1.0)
        updates["surge_guard_ratio"] = sgr

    def save(doc):
        pol = dict(doc.get("policy") or {})
        pol.update(updates)
        doc["policy"] = pol
        doc["updated_at"] = now_iso()
        doc["updated_by"] = ctx.caller.id
        return pol

    ctx.store.mutate(POLICY_DOC, save)
    if getattr(ctx.app, "cache", None) is not None:
        ctx.app.cache.clear("caps_status")
    audit.write_audit(ctx, "caps.policy.updated", target="global", details={"fields": sorted(updates.keys())})
    return public_policy_view(ctx, effective_policy(ctx))


# ---------------------------------------------------------------------------
# Pure cap check & Safety Gates G1-G7
# ---------------------------------------------------------------------------

def cap_check(tokens: int, requests: int, token_cap: int, request_cap: int,
              cost: float | None = None, cost_cap: float = 0.0) -> tuple[bool, float | None, list[str]]:
    """Returns (over_cap, utilization_ratio, reasons). Caps of 0 are disabled."""
    reasons = []
    ratios = [0.0]
    if token_cap > 0:
        ratios.append(tokens / float(token_cap))
        if tokens >= token_cap:
            reasons.append("tokens")
    if request_cap > 0:
        ratios.append(requests / float(request_cap))
        if requests >= request_cap:
            reasons.append("requests")
    if cost_cap and cost_cap > 0:
        if cost is not None:
            ratios.append(cost / float(cost_cap))
            if cost >= cost_cap:
                reasons.append("cost")
        elif token_cap <= 0 and request_cap <= 0:
            return False, None, []
    return bool(reasons), round(max(ratios), 4), reasons


def _rule_expired(rule: dict | None, now: dt.datetime) -> bool:
    if not rule or not rule.get("until"):
        return False
    until = timeutil.parse_rfc3339(rule.get("until"))
    return until is not None and now >= until


def _suspension_ended(susp: dict | None, now: dt.datetime) -> bool:
    if not susp or not susp.get("until"):
        return False
    until = timeutil.parse_rfc3339(susp.get("until"))
    return until is not None and now >= until


def evaluate_gates_and_plan(users: list[dict], *, deny_readable: bool, telemetry_ok: bool, telemetry_records: int,
                            telemetry_truncated: bool, policy: dict, deny_meta: dict,
                            now: dt.datetime | None = None) -> tuple[list[dict], dict]:
    """Evaluates Safety Gates G1-G7 and produces the enforcement plan."""
    now = now or utcnow()
    backend = policy.get("enforcement_backend") or "iam_deny"
    mode = policy.get("enforcement_mode") or "manual"
    population = len(users)
    determinate = sum(1 for u in users if u.get("status") != "unknown")
    gates: list[dict] = []

    # G1: Deny policy readability & ownership at all attachment points
    g1_ok = True if backend == "notify_only" else bool(deny_readable)
    gates.append({
        "id": "G1_POLICY_READABLE",
        "status": "pass" if g1_ok else "fail",
        "detail": ("Notify-only mode: no IAM policy read or written." if backend == "notify_only" else
                   ("Managed IAM Deny v2 policies read at %d attachment point(s)." % len(deny_meta.get("attachments") or [])
                    if g1_ok else "Managed IAM Deny v2 policy could not be read or a foreign policy was detected.")),
    })

    # G2: Determinate population check
    g2_ok = population == 0 or determinate > 0
    gates.append({
        "id": "G2_ANY_DETERMINATE",
        "status": "pass" if g2_ok else "fail",
        "detail": "%d of %d users have a determinate state." % (determinate, population),
    })

    # G3: Telemetry health (warns & withholds usage-based restores; period-end restores always pass)
    g3_ok = bool(telemetry_ok) and telemetry_records > 0 and not telemetry_truncated
    gates.append({
        "id": "G3_TELEMETRY_HEALTHY",
        "status": "pass" if g3_ok else "warn",
        "detail": ("%d usage log records in current window." % telemetry_records if g3_ok else
                   "Telemetry unavailable, empty, or truncated; usage-based restores are withheld (period resets still restore)."),
    })

    suspend_list, block_list, restore_list, review_list = [], [], [], []
    withheld_restores = False
    for u in users:
        email = u["email"]
        rule = u.get("rule")
        mblock = u.get("manual_block")
        rtype = (rule or {}).get("type")
        currently_denied = bool(u.get("blocked_services"))
        over = u.get("over_cap")
        susp = u.get("suspension")

        if mblock or rtype == "block":
            src_block = mblock or rule or {}
            desired_svcs = set(src_block.get("services") or policy.get("default_block_services") or config.VALID_SERVICES)
            if set(u.get("blocked_services") or ()) != desired_svcs:
                block_list.append(email)
            continue
        if rtype == "allow":
            if currently_denied:
                restore_list.append(email)
            continue
        if not currently_denied and over is True:
            if mode == "auto":
                suspend_list.append(email)
            else:
                review_list.append(email)
        elif currently_denied and over is not True:
            if u.get("expired_block") or _suspension_ended(susp, now) or (over is False and g3_ok):
                restore_list.append(email)
            elif not g3_ok:
                withheld_restores = True

    severity = {u["email"]: float(u.get("utilization_pct") or 0.0) for u in users}

    # G7 (IAM Deny Surge Guard): holds mass automatic suspensions caused by a telemetry/pricing spike
    surge_min = max(1, int(policy.get("surge_guard_min") or 10))
    surge_ratio = float(policy.get("surge_guard_ratio") or 0.5)
    g7_triggered = bool(backend == "iam_deny" and suspend_list and len(suspend_list) >= surge_min
                        and len(suspend_list) >= surge_ratio * max(1, population))
    if g7_triggered:
        review_list.extend(suspend_list)
        suspend_list = []

    # G4 (Change Budget): bounds new suspensions per cycle; restores are never bounded
    budget_min = max(1, int(policy.get("change_budget_min") or 10))
    budget_ratio = float(policy.get("change_budget_ratio") or 0.2)
    budget = max(budget_min, int(math.ceil(budget_ratio * population)))
    planned_adds = len(block_list) + len(suspend_list)
    g4_ok = planned_adds <= budget
    deferred = []
    if not g4_ok:
        suspend_list.sort(key=lambda em: -severity.get(em, 0.0))
        keep_b = block_list[:budget]
        room = budget - len(keep_b)
        deferred = block_list[len(keep_b):] + suspend_list[room:]
        block_list, suspend_list = keep_b, suspend_list[:room]
    gates.append({
        "id": "G4_CHANGE_BUDGET",
        "status": "pass" if g4_ok else "warn",
        "detail": "%d planned blocks/suspensions within budget of %d per run." % (planned_adds, budget) if g4_ok else
                  "%d planned blocks/suspensions exceed budget %d; %d deferred to next run." % (planned_adds, budget, len(deferred)),
    })

    # G5: Non-user grant coverage (IAM Deny covers groups, domains, and inherited roles)
    gates.append({
        "id": "G5_NON_USER_GRANT_ABSENT",
        "status": "pass",
        "detail": ("IAM Deny v2 blocks access granted via groups, domains, Workforce Identity, and inherited roles."
                   if backend == "iam_deny" else "Notify-only mode: no access modified."),
    })

    # G6 (IAM Deny 2,500 principal capacity limit per attachment point)
    capacity_held = []
    if backend == "iam_deny":
        cur_count = int(deny_meta.get("current_principals") or 0)
        fresh_adds = [em for em in block_list + suspend_list]
        after = max(0, cur_count - len(restore_list)) + len(fresh_adds)
        if after > enforcers.DENY_PRINCIPAL_LIMIT and fresh_adds:
            excess = after - enforcers.DENY_PRINCIPAL_LIMIT
            order = sorted(suspend_list, key=lambda em: severity.get(em, 0.0)) + list(reversed(block_list))
            capacity_held = order[:min(len(fresh_adds), excess)]
            suspend_list = [em for em in suspend_list if em not in capacity_held]
            block_list = [em for em in block_list if em not in capacity_held]
            gates.append({
                "id": "G6_DENY_CAPACITY",
                "status": "warn",
                "detail": "%d suspension(s) held because attachment point would exceed %d denied principals."
                          % (len(capacity_held), enforcers.DENY_PRINCIPAL_LIMIT),
            })
        elif after >= enforcers.DENY_WARN_AT:
            gates.append({
                "id": "G6_DENY_CAPACITY",
                "status": "warn",
                "detail": "%d denied principals (warning threshold %d, hard limit %d)."
                          % (after, enforcers.DENY_WARN_AT, enforcers.DENY_PRINCIPAL_LIMIT),
            })
        else:
            gates.append({
                "id": "G6_DENY_CAPACITY",
                "status": "pass",
                "detail": "%d denied principals per attachment point (limit %d)." % (after, enforcers.DENY_PRINCIPAL_LIMIT),
            })

    if backend == "iam_deny":
        gates.append({
            "id": "G7_SURGE_GUARD",
            "status": "warn" if g7_triggered else "pass",
            "detail": ("Surge guard held automatic suspensions (>= %d users and >= %.0f%% of population)."
                       % (surge_min, surge_ratio * 100) if g7_triggered else
                       "Surge guard passed (threshold: >= %d users and >= %.0f%%)." % (surge_min, surge_ratio * 100)),
        })

    failed = [g for g in gates if g["status"] == "fail"]
    plan = {
        "suspend": suspend_list,
        "block": block_list,
        "restore": restore_list,
        "review": review_list,
        "deferred": deferred,
        "capacity_held": capacity_held,
        "withheld_restores": withheld_restores,
        "change_budget": budget,
        "aborted": bool(failed),
        "abort_reason": "; ".join("%s: %s" % (g["id"], g["detail"]) for g in failed) if failed else None,
    }
    return gates, plan


# ---------------------------------------------------------------------------
# Status computation across all known users
# ---------------------------------------------------------------------------

def _load_user_caps_state(ctx) -> dict:
    doc, _ = ctx.store.load(USER_CAPS_DOC)
    return {
        "rules": dict(doc.get("rules") or {}),
        "blocks": dict(doc.get("blocks") or {}),
        "suspensions": dict(doc.get("suspensions") or {}),
        "last_run": doc.get("last_run"),
        "usage_events": doc.get("usage_events"),
    }


def compute_caps_status(ctx, *, fresh: bool = False, now: dt.datetime | None = None,
                        deny_client: enforcers.DenyPolicyClient | None = None) -> tuple[dict, dict]:
    """Computes full per-user cap status, safety gates, and enforcement plan."""
    now = now or utcnow()
    policy = effective_policy(ctx)
    win = _period_info(ctx, policy, now)
    state = _load_user_caps_state(ctx)
    backend = policy.get("enforcement_backend") or "iam_deny"

    tasks = {
        "usage": lambda: usage_svc.antigravity_usage(ctx, win["start"], win["end"], fresh=fresh),
        "pricing": lambda: pricing_svc.resolve_pricing(ctx, policy),
    }
    if backend == "iam_deny":
        tasks["deny"] = lambda: enforcers.read_state(ctx, client=deny_client)
    res = ctx.run(tasks)

    usage_res = res["usage"][1] if res["usage"][0] else usage_svc.UsageResult("unavailable")
    pricing = res["pricing"][1] if res["pricing"][0] else pricing_svc.Pricing(currency=policy.get("currency") or "USD")
    deny_state = res["deny"][1] if ("deny" in res and res["deny"][0]) else enforcers.DenyState([])

    deny_by_person = deny_state.services_by_person if backend == "iam_deny" else {}
    active_rules = {em: r for em, r in state["rules"].items() if isinstance(r, dict) and not _rule_expired(r, now)}
    active_blocks = {em: b for em, b in state["blocks"].items() if isinstance(b, dict) and not _rule_expired(b, now)}
    suspensions = state["suspensions"]

    all_people = sorted(set(usage_res.users.keys()) | set(active_rules.keys()) | set(active_blocks.keys()) |
                        set(suspensions.keys()) | set(deny_by_person.keys()))
    def_cost = float(policy.get("cost_cap") or 0.0)
    def_tok = int(policy.get("token_cap") or 0)
    def_req = int(policy.get("request_cap") or 0)
    def_svcs = list(policy.get("default_block_services") or config.VALID_SERVICES)

    rows = []
    for email in all_people:
        if not identity.is_person_key(email):
            continue
        rule = active_rules.get(email)
        mblock = active_blocks.get(email)
        susp = suspensions.get(email)
        urec = usage_res.users.get(email) or {
            "requests": 0, "input_tokens": 0, "output_tokens": 0,
            "cached_tokens": 0, "total_tokens": 0, "by_model": {}, "last_active": None,
        }
        est_cost, cost_partial = pricing_svc.estimate_cost(urec, pricing) if usage_res.ok else (None, False)
        if rule and rule.get("type") == "limit":
            c_cap = float(rule["cost_cap"]) if rule.get("cost_cap") is not None else def_cost
            t_cap = int(rule["token_cap"]) if rule.get("token_cap") is not None else def_tok
            r_cap = int(rule["request_cap"]) if rule.get("request_cap") is not None else def_req
        else:
            c_cap, t_cap, r_cap = def_cost, def_tok, def_req

        if usage_res.ok:
            over, ratio_val, reasons = cap_check(urec["total_tokens"], urec["requests"], t_cap, r_cap, est_cost, c_cap)
        else:
            over, ratio_val, reasons = None, None, []

        util_pct = round(ratio_val * 100.0, 2) if ratio_val is not None else None
        blocked_svcs = sorted(deny_by_person.get(email) or ())
        if backend == "notify_only" and (mblock or (rule and rule.get("type") == "block")):
            blocked_svcs = sorted((mblock or rule).get("services") or def_svcs)

        # Determine clean status: blocked | suspended | over_cap | active
        if mblock or (rule and rule.get("type") == "block") or (blocked_svcs and not susp):
            status = "blocked"
        elif blocked_svcs or (susp and not _suspension_ended(susp, now)):
            status = "suspended"
        elif over is True and not (rule and rule.get("type") == "allow"):
            status = "over_cap"
        else:
            status = "active"

        rem_cost = round(max(0.0, c_cap - est_cost), 6) if (c_cap > 0 and est_cost is not None) else None
        rem_tok = max(0, t_cap - urec["total_tokens"]) if t_cap > 0 else None
        rem_req = max(0, r_cap - urec["requests"]) if r_cap > 0 else None
        restore_at = None
        if status == "suspended":
            restore_at = (susp or {}).get("until") or win["next_reset_iso"]
        elif status == "blocked":
            restore_at = (mblock or {}).get("until") or (rule or {}).get("until")

        raw_block = state["blocks"].get(email) if isinstance(state["blocks"].get(email), dict) else None
        rows.append({
            "email": email,
            "uid": identity.uid_for(email),
            "status": status,
            "block_source": ((mblock or {}).get("source") or "manual") if mblock else
                            ("rule" if (rule and rule.get("type") == "block") else
                             ("auto_cap" if status == "suspended" else None)),
            "expired_block": bool(raw_block and _rule_expired(raw_block, now) and not mblock),
            "blocked_services": blocked_svcs,
            "over_cap": over,
            "over_cap_reasons": reasons,
            "utilization_pct": util_pct,
            "limit": {
                "cost_cap": c_cap if c_cap > 0 else None,
                "token_cap": t_cap if t_cap > 0 else None,
                "request_cap": r_cap if r_cap > 0 else None,
                "currency": pricing.currency,
            },
            "usage": {
                "requests": urec["requests"],
                "input_tokens": urec["input_tokens"],
                "output_tokens": urec["output_tokens"],
                "cached_tokens": urec["cached_tokens"],
                "total_tokens": urec["total_tokens"],
                "estimated_cost": est_cost,
                "cost_partial": cost_partial,
                "currency": pricing.currency if est_cost is not None else None,
                "last_active": iso(urec.get("last_active")),
            },
            "remaining": {
                "cost": rem_cost,
                "tokens": rem_tok,
                "requests": rem_req,
            },
            "restore_at": restore_at,
            "rule": rule,
            "manual_block": mblock,
            "suspension": susp,
        })

    deny_meta = {
        "attachments": [st.attachment for st in deny_state.attachments],
        "current_principals": len(deny_state.principals),
        "unreadable": sum(1 for st in deny_state.attachments if st.error is not None),
        "foreign": sum(1 for st in deny_state.attachments if st.foreign),
    }
    gates, plan = evaluate_gates_and_plan(
        rows,
        deny_readable=deny_state.readable,
        telemetry_ok=usage_res.ok,
        telemetry_records=usage_res.records,
        telemetry_truncated=usage_res.truncated,
        policy=policy,
        deny_meta=deny_meta,
        now=now,
    )
    rows.sort(key=lambda r: (-(r["utilization_pct"] if r["utilization_pct"] is not None else -1.0), r["email"]))
    payload = {
        "policy": public_policy_view(ctx, policy, now),
        "period": {
            "start": win["start_iso"],
            "end": win["period_end_iso"],
            "next_reset": win["next_reset_iso"],
            "timezone": win["timezone"],
        },
        "users": rows,
        "gates": gates,
        "plan": plan,
        "last_run": state.get("last_run"),
    }
    internal = {
        "policy": policy,
        "window": win,
        "usage": usage_res,
        "pricing": pricing,
        "deny_state": deny_state,
        "state": state,
    }
    return payload, internal


# ---------------------------------------------------------------------------
# Public REST handlers: GET/PUT/DELETE /v1/caps/users*
# ---------------------------------------------------------------------------

def _public_user_cap(row: dict) -> dict:
    out = dict(row)
    out.pop("suspension", None)
    out.pop("manual_block", None)
    out.pop("expired_block", None)
    return out


def list_user_caps(ctx) -> dict:
    """GET /v1/caps/users?status=&email=&limit=&cursor="""
    payload, _ = compute_caps_status(ctx)
    rows = [_public_user_cap(r) for r in payload["users"]]
    status_f = (ctx.params.get("status") or "").strip().lower()
    if status_f:
        if status_f not in ("active", "over_cap", "suspended", "blocked"):
            raise ApiError("bad_request", "Parameter 'status' must be active, over_cap, suspended, or blocked.")
        rows = [r for r in rows if r["status"] == status_f]
    email_f = (ctx.params.get("email") or "").strip().lower()
    if email_f:
        rows = [r for r in rows if r["email"] == email_f]
    page = paginate_list(rows, ctx.params, default_limit=100, max_limit=500)
    page["period"] = payload["period"]
    page["summary"] = {
        "total": len(payload["users"]),
        "active": sum(1 for r in payload["users"] if r["status"] == "active"),
        "over_cap": sum(1 for r in payload["users"] if r["status"] == "over_cap"),
        "suspended": sum(1 for r in payload["users"] if r["status"] == "suspended"),
        "blocked": sum(1 for r in payload["users"] if r["status"] == "blocked"),
    }
    return page


def get_user_cap_snapshot(ctx, email: str) -> dict:
    payload, _ = compute_caps_status(ctx)
    for row in payload["users"]:
        if row["email"] == email:
            return dict(_public_user_cap(row), period=payload["period"])
    pol = payload["policy"]
    return {
        "email": email,
        "uid": identity.uid_for(email),
        "status": "active",
        "blocked_services": [],
        "over_cap": False,
        "over_cap_reasons": [],
        "utilization_pct": 0.0,
        "limit": {
            "cost_cap": pol["cost_cap"] if pol["cost_cap"] > 0 else None,
            "token_cap": pol["token_cap"] if pol["token_cap"] > 0 else None,
            "request_cap": pol["request_cap"] if pol["request_cap"] > 0 else None,
            "currency": pol["currency"],
        },
        "usage": {
            "requests": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
            "total_tokens": 0, "estimated_cost": 0.0, "cost_partial": False,
            "currency": pol["currency"], "last_active": None,
        },
        "remaining": {
            "cost": pol["cost_cap"] if pol["cost_cap"] > 0 else None,
            "tokens": pol["token_cap"] if pol["token_cap"] > 0 else None,
            "requests": pol["request_cap"] if pol["request_cap"] > 0 else None,
        },
        "restore_at": None,
        "rule": None,
        "period": payload["period"],
    }


def get_user_cap(ctx) -> dict:
    """GET /v1/caps/users/{email}"""
    email = identity.person_key(ctx.path_params.get("email") or "")
    if not email:
        raise ApiError("bad_request", "Path parameter 'email' must be a valid email or workforce principal.")
    return dict(get_user_cap_snapshot(ctx, email), object="user_cap")


def put_user_cap(ctx) -> dict:
    """PUT /v1/caps/users/{email}: sets an individual spend cap or allow/block override."""
    email = identity.person_key(ctx.path_params.get("email") or "")
    if not email:
        raise ApiError("bad_request", "Path parameter 'email' must be a valid email or workforce principal.")
    body = ctx.body or {}
    rtype = str(body.get("type") or "limit").strip().lower()
    if rtype not in ("limit", "allow", "block"):
        raise ApiError("bad_request", "Field 'type' must be 'limit', 'allow', or 'block'.")
    until = body.get("until")
    if until is not None and until != "":
        if timeutil.parse_rfc3339(until) is None:
            raise ApiError("bad_request", "Field 'until' must be an RFC3339 timestamp.")
    else:
        until = None

    policy = effective_policy(ctx)
    rule: dict = {
        "email": email,
        "type": rtype,
        "until": until,
        "note": str(body.get("note") or body.get("reason") or "")[:200],
        "updated_at": now_iso(),
        "updated_by": ctx.caller.id,
    }
    if rtype == "limit":
        c = _opt_nonneg_float(body.get("cost_cap"), "cost_cap")
        t = _opt_nonneg_int(body.get("token_cap"), "token_cap")
        r = _opt_nonneg_int(body.get("request_cap"), "request_cap", hi=10 ** 9)
        if c is None and t is None and r is None:
            raise ApiError("bad_request", "At least one of cost_cap, token_cap, or request_cap is required for type='limit'.")
        rule.update(cost_cap=c, token_cap=t, request_cap=r)
        if "services" in body:
            rule["services"] = _validate_services(body["services"])
    elif rtype == "block":
        rule["services"] = _validate_services(body.get("services"), default=policy.get("default_block_services"))

    def save(doc):
        rules = dict(doc.get("rules") or {})
        rules[email] = rule
        doc["rules"] = rules
        return rule

    ctx.store.mutate(USER_CAPS_DOC, save)
    audit.write_audit(ctx, "caps.user.updated", target=email, details={"type": rtype, "rule": rule})
    return {"object": "user_cap_rule", "email": email, "rule": rule}


def delete_user_cap(ctx) -> dict:
    """DELETE /v1/caps/users/{email}: deletes per-user override so user reverts to global policy."""
    email = identity.person_key(ctx.path_params.get("email") or "")
    if not email:
        raise ApiError("bad_request", "Path parameter 'email' must be a valid email or workforce principal.")

    def remove(doc):
        rules = dict(doc.get("rules") or {})
        existed = email in rules
        rules.pop(email, None)
        doc["rules"] = rules
        return existed

    existed = ctx.store.mutate(USER_CAPS_DOC, remove)
    if not existed:
        raise ApiError("not_found", "No individual spend cap rule found for '%s'." % email)
    audit.write_audit(ctx, "caps.user.deleted", target=email)
    return {"email": email, "deleted": True}


# ---------------------------------------------------------------------------
# Immediate User Block / Unblock: POST /v1/users/{email}/block & /unblock
# ---------------------------------------------------------------------------

def _desired_deny_map(ctx, policy: dict, state: dict, deny_state: enforcers.DenyState,
                      usage_res: usage_svc.UsageResult | None = None,
                      add_suspensions: list[str] = (), add_blocks: list[str] = (),
                      restores: list[str] = ()) -> dict[str, frozenset[str]]:
    """Computes desired {deny_principal_uri: frozenset(services)} for IAM Deny v2."""
    now = utcnow()
    def_svcs = frozenset(policy.get("default_block_services") or config.VALID_SERVICES)
    rules = {em: r for em, r in (state.get("rules") or {}).items() if isinstance(r, dict) and not _rule_expired(r, now)}
    blocks = {em: b for em, b in (state.get("blocks") or {}).items() if isinstance(b, dict) and not _rule_expired(b, now)}
    logged_principals = (usage_res.principals if usage_res is not None else {}) or {}

    current_by_person: dict[str, set[str]] = {
        em: set(svcs) for em, svcs in deny_state.services_by_person.items()
    }
    for em in restores:
        current_by_person.pop(em, None)
    for em in add_suspensions:
        rule = rules.get(em) or {}
        current_by_person[em] = set(rule.get("services") or def_svcs)
    for em in add_blocks:
        src = blocks.get(em) or rules.get(em) or {}
        current_by_person[em] = set(src.get("services") or def_svcs)
    for em, rule in rules.items():
        if rule.get("type") == "block":
            current_by_person[em] = set(rule.get("services") or def_svcs)
        elif rule.get("type") == "allow" and em not in blocks:
            current_by_person.pop(em, None)
    for em, blk in blocks.items():
        current_by_person[em] = set(blk.get("services") or def_svcs)

    out: dict[str, frozenset[str]] = {}
    for person, svcs in current_by_person.items():
        if not svcs:
            continue
        raw = logged_principals.get(person) or person
        uri = identity.deny_principal(raw)
        if uri:
            out[uri] = frozenset(svcs)
    return out


def _trigger_enforcer_job(ctx) -> dict | None:
    job_name = str((ctx.config.get("caps") or {}).get("enforcement_job") or "").strip()
    if not (job_name and config.ENFORCEMENT_JOB_RE.match(job_name)):
        return None
    op = ctx.rest("POST", "%s/%s:run" % (RUN_V2, job_name), json_body={})
    meta = (op or {}).get("metadata") if isinstance((op or {}).get("metadata"), dict) else {}
    return {"job": job_name, "execution": meta.get("name"), "operation": (op or {}).get("name")}


def block_user(ctx, *, deny_client: enforcers.DenyPolicyClient | None = None) -> dict:
    """POST /v1/users/{email}/block {services?: [...], reason?: str, until?: rfc3339}"""
    email = identity.person_key(ctx.path_params.get("email") or "")
    if not email:
        raise ApiError("bad_request", "Path parameter 'email' must be a valid email or workforce principal.")
    body = ctx.body or {}
    policy = effective_policy(ctx)
    services = _validate_services(body.get("services"), default=policy.get("default_block_services"))
    reason = str(body.get("reason") or body.get("note") or "")[:200]
    until = body.get("until")
    if until is not None and until != "":
        if timeutil.parse_rfc3339(until) is None:
            raise ApiError("bad_request", "Field 'until' must be an RFC3339 timestamp.")
    else:
        until = None

    with _WRITE_LOCK:
        blk = {
            "email": email,
            "services": services,
            "until": until,
            "reason": reason,
            "updated_at": now_iso(),
            "updated_by": ctx.caller.id,
        }

        def save(doc):
            blocks = dict(doc.get("blocks") or {})
            blocks[email] = blk
            doc["blocks"] = blocks
            return doc

        doc = ctx.store.mutate(USER_CAPS_DOC, save)
        enforcement = _reconcile_direct_change(ctx, policy, doc, deny_client=deny_client)

    audit.write_audit(ctx, "user.blocked", target=email,
                      details={"services": services, "reason": reason, "until": until, "enforcement": enforcement})
    webhooks.dispatch_event(ctx, "user.blocked", {
        "email": email, "services": services, "reason": reason, "until": until,
        "trigger": "manual", "actor": ctx.caller.id,
    })
    return {
        "object": "user_block",
        "email": email,
        "status": "blocked",
        "blocked_services": services,
        "reason": reason or None,
        "until": until,
        "enforcement": enforcement,
    }


def unblock_user(ctx, *, deny_client: enforcers.DenyPolicyClient | None = None) -> dict:
    """POST /v1/users/{email}/unblock {services?: [...], reason?: str}"""
    email = identity.person_key(ctx.path_params.get("email") or "")
    if not email:
        raise ApiError("bad_request", "Path parameter 'email' must be a valid email or workforce principal.")
    body = ctx.body or {}
    policy = effective_policy(ctx)
    target_services = _validate_services(body.get("services"), allow_empty=True) if "services" in body else None
    reason = str(body.get("reason") or body.get("note") or "")[:200]

    with _WRITE_LOCK:
        remaining_holder: dict = {"services": []}

        def save(doc):
            blocks = dict(doc.get("blocks") or {})
            rules = dict(doc.get("rules") or {})
            susps = dict(doc.get("suspensions") or {})
            existing_blk = blocks.get(email)
            existing_rule = rules.get(email)
            if target_services is not None and existing_blk:
                rem = [s for s in (existing_blk.get("services") or []) if s not in set(target_services)]
                if rem:
                    blocks[email] = dict(existing_blk, services=rem, updated_at=now_iso(), updated_by=ctx.caller.id)
                    remaining_holder["services"] = rem
                else:
                    blocks.pop(email, None)
                    susps.pop(email, None)
            elif target_services is not None and existing_rule and existing_rule.get("type") == "block":
                rem = [s for s in (existing_rule.get("services") or []) if s not in set(target_services)]
                if rem:
                    rules[email] = dict(existing_rule, services=rem, updated_at=now_iso(), updated_by=ctx.caller.id)
                    remaining_holder["services"] = rem
                else:
                    rules.pop(email, None)
                    susps.pop(email, None)
            else:
                blocks.pop(email, None)
                if existing_rule and existing_rule.get("type") == "block":
                    rules.pop(email, None)
                susps.pop(email, None)
            doc["blocks"] = blocks
            doc["rules"] = rules
            doc["suspensions"] = susps
            return doc

        doc = ctx.store.mutate(USER_CAPS_DOC, save)
        enforcement = _reconcile_direct_change(
            ctx, policy, doc, deny_client=deny_client,
            force_unblock=email if not remaining_holder["services"] else None,
        )

    status = "blocked" if remaining_holder["services"] else "active"
    audit.write_audit(ctx, "user.unblocked", target=email,
                      details={"unblocked_services": target_services or list(config.VALID_SERVICES),
                               "remaining_services": remaining_holder["services"],
                               "reason": reason, "enforcement": enforcement})
    webhooks.dispatch_event(ctx, "user.unblocked", {
        "email": email, "unblocked_services": target_services or list(config.VALID_SERVICES),
        "remaining_services": remaining_holder["services"], "reason": reason,
        "trigger": "manual", "actor": ctx.caller.id,
    })
    return {
        "object": "user_unblock",
        "email": email,
        "status": status,
        "blocked_services": remaining_holder["services"],
        "reason": reason or None,
        "enforcement": enforcement,
    }


def _reconcile_direct_change(ctx, policy: dict, state_doc: dict, *,
                             deny_client: enforcers.DenyPolicyClient | None = None,
                             force_unblock: str | None = None) -> dict:
    backend = policy.get("enforcement_backend") or "iam_deny"
    if backend == "notify_only" and not force_unblock:
        return {"backend": "notify_only", "applied": False, "detail": "Notify-only mode; no IAM change applied."}
    if backend == "notify_only":
        # Break-glass: notify_only never adds principals, but an unblock still removes any existing deny entries.
        try:
            deny_state = enforcers.read_state(ctx, client=deny_client)
        except Exception:  # pylint: disable=broad-except
            return {"backend": "notify_only", "applied": False, "error": "deny_policy_unreadable"}
        if not deny_state.readable or force_unblock not in deny_state.services_by_person:
            return {"backend": "notify_only", "applied": False, "detail": "No IAM deny entry to remove."}
        desired = {}
        for person, svcs in deny_state.services_by_person.items():
            if person != force_unblock and svcs:
                uri = identity.deny_principal(person)
                if uri:
                    desired[uri] = frozenset(svcs)
        res = enforcers.apply(ctx, deny_state, desired, dry_run=False, client=deny_client)
        return {"backend": "notify_only", "applied": bool(res.get("applied")), "items": res.get("items") or [],
                "note": "Existing IAM deny entry removed (break-glass unblock in notify_only mode)."}
    # If an external Cloud Run Enforcer Job is configured and no explicit direct client is injected, trigger it.
    if deny_client is None and (ctx.config.get("caps") or {}).get("enforcement_job"):
        try:
            job_res = _trigger_enforcer_job(ctx)
            if job_res:
                return {"backend": "iam_deny", "applied": True, "mode": "enforcer_job", **job_res}
        except GcpError as err:
            return {"backend": "iam_deny", "applied": False, "error": err.kind}
    deny_state = enforcers.read_state(ctx, client=deny_client)
    if not deny_state.readable:
        return {"backend": "iam_deny", "applied": False, "error": "deny_policy_unreadable"}
    desired = _desired_deny_map(
        ctx, policy, state_doc, deny_state,
        restores=[force_unblock] if force_unblock else (),
    )
    res = enforcers.apply(ctx, deny_state, desired, dry_run=False, client=deny_client)
    return {
        "backend": "iam_deny",
        "applied": bool(res.get("applied")),
        "violations": res.get("violations") or [],
        "items": res.get("items") or [],
        "note": "IAM Deny v2 changes propagate within ~1-2 minutes (eventually consistent).",
    }


# ---------------------------------------------------------------------------
# Enforcement Cycle: POST /v1/caps/enforce & enforce_job.py
# ---------------------------------------------------------------------------

def enforce_cycle(ctx, *, trigger: str = "api", dry_run: bool = False,
                  deny_client: enforcers.DenyPolicyClient | None = None,
                  use_job_if_configured: bool = False) -> dict:
    """Evaluates policy, fires threshold webhooks, checks G1-G7 gates, and applies IAM Deny v2."""
    now = utcnow()
    with _WRITE_LOCK:
        payload, internal = compute_caps_status(ctx, fresh=True, now=now, deny_client=deny_client)
        policy = internal["policy"]
        win = internal["window"]
        plan = payload["plan"]
        gates = payload["gates"]
        backend = policy.get("enforcement_backend") or "iam_deny"

        # 1. Threshold webhooks (80%, 100%, ...) — evaluated even in manual mode!
        thresholds = sorted(int(t) for t in (policy.get("webhook_thresholds") or [80, 100]))
        if not dry_run:
            for u in payload["users"]:
                pct = u.get("utilization_pct")
                if pct is None or (u.get("rule") or {}).get("type") == "allow":
                    continue
                for thr in thresholds:
                    if pct >= thr:
                        webhooks.notify_threshold_once(
                            ctx, email=u["email"], period_start=win["start_iso"] or "all",
                            threshold_pct=thr, utilization_pct=pct, usage=u["usage"], limit=u["limit"],
                        )

        # 2. If dry_run, aborted by safety gates, or notify_only: change nothing.
        changes = []
        if dry_run or plan["aborted"] or backend == "notify_only":
            result = "dry_run" if dry_run else ("aborted" if plan["aborted"] else "notify_only")
            return {
                "object": "enforcement_run",
                "trigger": trigger,
                "mode": "dry_run" if dry_run else "apply",
                "backend": backend,
                "enforcement_mode": policy.get("enforcement_mode"),
                "executed": False,
                "result": result,
                "gates": gates,
                "plan": plan,
                "changes": changes,
            }

        # 3. Delegate to Cloud Run Enforcer Job when requested (API service -> privileged job SA)
        if use_job_if_configured and deny_client is None and (ctx.config.get("caps") or {}).get("enforcement_job"):
            job_res = _trigger_enforcer_job(ctx)
            if job_res:
                audit.write_audit(ctx, "caps.enforce.job_triggered", target=job_res["job"],
                                  details={"trigger": trigger, "execution": job_res.get("execution")})
                return {
                    "object": "enforcement_run",
                    "trigger": trigger,
                    "mode": "apply",
                    "backend": backend,
                    "enforcement_mode": policy.get("enforcement_mode"),
                    "executed": True,
                    "result": "job_started",
                    "job": job_res,
                    "gates": gates,
                    "plan": plan,
                    "changes": [],
                }

        # 4. Direct IAM Deny v2 apply
        desired = _desired_deny_map(
            ctx, policy, internal["state"], internal["deny_state"],
            usage_res=internal["usage"],
            add_suspensions=plan["suspend"],
            add_blocks=plan["block"],
            restores=plan["restore"],
        )
        apply_res = enforcers.apply(ctx, internal["deny_state"], desired, dry_run=False, client=deny_client)
        ok_apply = not apply_res["violations"] and all(
            it.get("status") in ("applied", "pending", "skipped") for it in apply_res["items"]
        )
        action_status = "applied" if ok_apply else "failed"
        for action, emails in (("suspend", plan["suspend"]), ("block", plan["block"]), ("restore", plan["restore"])):
            for em in emails:
                changes.append({"email": em, "action": action, "status": action_status})

        # 5. Persist suspension records & run summary
        users_by_email = {u["email"]: u for u in payload["users"]}
        run_summary = {
            "at": iso(now),
            "trigger": trigger,
            "actor": ctx.caller.id,
            "mode": "apply",
            "result": "success" if ok_apply else "failed",
            "suspended": len(plan["suspend"]) if ok_apply else 0,
            "blocked": len(plan["block"]) if ok_apply else 0,
            "restored": len(plan["restore"]) if ok_apply else 0,
            "review": len(plan["review"]),
            "failed": 0 if ok_apply else len(changes),
        }

        def persist(doc):
            susps = dict(doc.get("suspensions") or {})
            if ok_apply:
                blocks = dict(doc.get("blocks") or {})
                for em in plan["restore"]:
                    blk = blocks.get(em)
                    if isinstance(blk, dict) and _rule_expired(blk, now):
                        blocks.pop(em, None)
                doc["blocks"] = blocks
                for em in plan["suspend"]:
                    u = users_by_email.get(em) or {}
                    susps[em] = {
                        "email": em,
                        "since": iso(now),
                        "until": win["next_reset_iso"],
                        "trigger": "auto",
                        "reasons": list(u.get("over_cap_reasons") or []),
                        "usage": u.get("usage") or {},
                    }
                for em in plan["restore"]:
                    susps.pop(em, None)
            doc["suspensions"] = susps
            doc["last_run"] = run_summary
            return doc

        ctx.store.mutate(USER_CAPS_DOC, persist)

        # 6. Audit & outbound webhooks
        audit.write_audit(ctx, "caps.enforce", target="global", details=run_summary)
        if ok_apply:
            def_svcs = list(policy.get("default_block_services") or config.VALID_SERVICES)
            for em in plan["suspend"]:
                webhooks.dispatch_event(ctx, "user.blocked", {
                    "email": em, "services": def_svcs, "trigger": "auto_cap",
                    "until": win["next_reset_iso"],
                    "reasons": (users_by_email.get(em) or {}).get("over_cap_reasons") or [],
                })
            for em in plan["restore"]:
                webhooks.dispatch_event(ctx, "user.unblocked", {
                    "email": em, "trigger": "auto_reset",
                })

        return {
            "object": "enforcement_run",
            "trigger": trigger,
            "mode": "apply",
            "backend": backend,
            "enforcement_mode": policy.get("enforcement_mode"),
            "executed": True,
            "result": run_summary["result"],
            "gates": gates,
            "plan": plan,
            "changes": changes,
            "deny_apply": apply_res,
        }


def enforce_handler(ctx) -> dict:
    """POST /v1/caps/enforce {"dryRun": true|false}"""
    body = ctx.body or {}
    dry_run = body.get("dryRun", body.get("dry_run", True))
    if not isinstance(dry_run, bool):
        raise ApiError("bad_request", "Field 'dryRun' must be a boolean.")
    trigger = "scheduler" if ctx.caller.auth_type == "oidc" else "api"
    out = enforce_cycle(ctx, trigger=trigger, dry_run=dry_run, use_job_if_configured=True)
    if out.get("result") != "job_started":
        from . import governance_cycle
        out["governance"] = governance_cycle.run(ctx, dry_run=dry_run, trigger=trigger)
    return out
