"""Monthly user x project chargeback report with exact integer-cent allocation (logs x official SKU prices).

- Usage: log token counts on every surface (Antigravity, Gemini Platform incl. audit-correlated attribution, Gemini
  Enterprise requests) - the Cloud Billing export is never read.
- Price: official Cloud Billing Catalog SKU prices (or contract overrides), see services/pricing.py.
- A user assigned to several projects gets one fact per (user, project); all user, project and grand totals are summed
  from the exact same integer-cent facts, guaranteeing zero rounding drift across rollups.
- Gemini Platform usage that could not be attributed to a person is reported per project under `unattributed` so project
  totals stay complete.
"""

from __future__ import annotations

import datetime as dt
import decimal
import math
import re

from .. import identity, timeutil
from ..httpkit import ApiError, paginate_list
from . import pricing as pricing_svc
from . import usage as usage_svc

MONTH_RE = re.compile(r"([0-9]{4})-([0-9]{2})")
_CENT = decimal.Decimal("1")


def to_cents(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = decimal.Decimal(repr(float(value))) * 100
    except (TypeError, ValueError, decimal.InvalidOperation):
        return None
    if not amount.is_finite():
        return None
    return int(amount.quantize(_CENT, rounding=decimal.ROUND_HALF_UP))


def money(cents: int | None) -> float | None:
    return None if cents is None else round(cents / 100.0, 2)


def allocate(total: int, weights: list, fallback=None) -> list[int]:
    """Splits integer `total` cents across `weights` using largest-remainder so parts sum to `total`."""
    count = len(weights)
    if count == 0:
        return []
    if not total:
        return [0] * count

    def clean(vals):
        out = []
        for v in vals:
            try:
                f = float(v)
            except (TypeError, ValueError):
                f = 0.0
            out.append(f if (f > 0 and math.isfinite(f)) else 0.0)
        return out

    w = clean(weights)
    if sum(w) <= 0 and fallback is not None:
        w = clean(fallback)
    whole = sum(w)
    if whole <= 0:
        return [total] + [0] * (count - 1)
    raw = [total * x / whole for x in w]
    parts = [int(math.floor(r)) for r in raw]
    order = sorted(range(count), key=lambda i: (-(raw[i] - parts[i]), i))
    for i in order[:total - sum(parts)]:
        parts[i] += 1
    return parts


def parse_month(raw: str | None, tz_name: str, now: dt.datetime | None = None) -> str:
    cur = (now or dt.datetime.now(dt.timezone.utc)).astimezone(timeutil.get_zone(tz_name)).strftime("%Y-%m")
    text = str(raw or "").strip()
    if not text:
        return cur
    m = MONTH_RE.fullmatch(text)
    if not m or not 1 <= int(m.group(2)) <= 12 or int(m.group(1)) < 2020:
        raise ApiError("bad_request", "Parameter 'month' must be in YYYY-MM format.")
    month = "%04d-%02d" % (int(m.group(1)), int(m.group(2)))
    if month > cur:
        raise ApiError("bad_request", "Parameter 'month' must not be in the future.")
    return month


def billing_users_report(ctx) -> dict:
    """GET /v1/billing/users?month=YYYY-MM&project=&limit=&cursor="""
    from . import caps as caps_svc
    policy = caps_svc.effective_policy(ctx)
    tz_name = policy.get("reset_timezone") or ctx.tz_name
    month = parse_month(ctx.params.get("month"), tz_name)
    start_utc, end_utc = timeutil.month_bounds(month, tz_name)
    now = dt.datetime.now(dt.timezone.utc)
    query_end = min(end_utc, now)
    complete = now >= end_utc

    project_f = ctx.selected_project
    if project_f != "all":
        _ = ctx.projects  # validates project is within managed workspace scope

    pricing = pricing_svc.resolve_pricing(ctx, policy)
    usage_params = {"project": project_f} if project_f != "all" else {}
    usage = usage_svc.collect_usage(ctx.child(params=usage_params), start_utc, query_end)
    mode = (ctx.config.get("caps") or {}).get("chargeback_allocation_mode") or "credit_share"
    credit_per_user = float((ctx.config.get("caps") or {}).get("credit_per_user_month") or 0.0)
    prefixes = tuple((ctx.config.get("caps") or {}).get("credit_model_prefixes") or pricing_svc.CREDIT_MODEL_PREFIXES)

    facts: list[dict] = []
    partial_users: set[str] = set()
    for email, rec in sorted(usage.users.items()):
        if project_f != "all":
            proj_rec = (rec.get("by_project") or {}).get(project_f)
            if not proj_rec:
                continue
            rec = dict(proj_rec, by_project={project_f: proj_rec})
        gemini_cost, other_cost, known, is_partial = pricing_svc.split_cost(rec, pricing, prefixes)
        if is_partial:
            partial_users.add(email)
        total_cost = gemini_cost + other_cost
        cost_cents = to_cents(total_cost) if (known and pricing.configured) else None
        if cost_cents is not None:
            if mode == "list" or pricing.currency != "USD":
                credit_cents = 0
                chargeable_cents = cost_cents
            else:
                credit_used = min(gemini_cost, credit_per_user)
                credit_cents = min(cost_cents, to_cents(credit_used) or 0)
                chargeable_cents = max(0, cost_cents - credit_cents)
        else:
            credit_cents = None
            chargeable_cents = None

        by_proj = list((rec.get("by_project") or {}).items())
        if not by_proj:
            by_proj = [("unknown", rec)]

        proj_gemini, proj_total = [], []
        for _proj, prec in by_proj:
            g, o, _, _ = pricing_svc.split_cost(prec, pricing, prefixes)
            proj_gemini.append(g)
            proj_total.append(g + o)

        c_parts = allocate(credit_cents, proj_gemini, fallback=proj_total) if credit_cents is not None else [None] * len(by_proj)
        ch_parts = allocate(chargeable_cents, proj_total, fallback=[p["total_tokens"] for _, p in by_proj]) if chargeable_cents is not None else [None] * len(by_proj)

        for idx, (proj, prec) in enumerate(by_proj):
            cc = c_parts[idx]
            ch = ch_parts[idx]
            facts.append({
                "email": email,
                "project": proj,
                "requests": prec["requests"],
                "input_tokens": prec["input_tokens"],
                "output_tokens": prec["output_tokens"],
                "cached_tokens": prec["cached_tokens"],
                "total_tokens": prec["total_tokens"],
                "cost_cents": (cc + ch) if (cc is not None and ch is not None) else None,
                "credit_cents": cc,
                "chargeable_cents": ch,
            })

    # Rollup users and projects from the exact same integer-cent facts.
    users_acc: dict[str, dict] = {}
    projects_acc: dict[str, dict] = {}
    grand = {"requests": 0, "total_tokens": 0, "cost_cents": 0, "credit_cents": 0, "chargeable_cents": 0}
    unknown_users: set[str] = set()

    for f in facts:
        for acc_map, key in ((users_acc, f["email"]), (projects_acc, f["project"])):
            a = acc_map.setdefault(key, {
                "requests": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
                "total_tokens": 0, "cost_cents": 0, "credit_cents": 0, "chargeable_cents": 0,
                "by_project": [], "users": set(),
            })
            a["requests"] += f["requests"]
            a["input_tokens"] += f["input_tokens"]
            a["output_tokens"] += f["output_tokens"]
            a["cached_tokens"] += f["cached_tokens"]
            a["total_tokens"] += f["total_tokens"]
            if f["cost_cents"] is not None:
                a["cost_cents"] += f["cost_cents"]
                a["credit_cents"] += f["credit_cents"]
                a["chargeable_cents"] += f["chargeable_cents"]
            if acc_map is users_acc:
                a["by_project"].append({
                    "project": f["project"],
                    "requests": f["requests"],
                    "total_tokens": f["total_tokens"],
                    "cost": money(f["cost_cents"]),
                    "credit": money(f["credit_cents"]),
                    "chargeable": money(f["chargeable_cents"]),
                })
            else:
                a["users"].add(f["email"])
        grand["requests"] += f["requests"]
        grand["total_tokens"] += f["total_tokens"]
        if f["cost_cents"] is not None:
            grand["cost_cents"] += f["cost_cents"]
            grand["credit_cents"] += f["credit_cents"]
            grand["chargeable_cents"] += f["chargeable_cents"]
        else:
            unknown_users.add(f["email"])

    # Invariant check: user and project rollups must agree with grand total to the cent.
    assert sum(u["chargeable_cents"] for u in users_acc.values()) == grand["chargeable_cents"]
    assert sum(p["chargeable_cents"] for p in projects_acc.values()) == grand["chargeable_cents"]

    user_rows = []
    for email, a in users_acc.items():
        unk = email in unknown_users
        urec = usage.users.get(email) or {}
        user_rows.append({
            "by_model": usage_svc.model_rows(urec.get("by_model"), pricing),
            "email": email,
            "uid": identity.uid_for(email),
            "requests": a["requests"],
            "input_tokens": a["input_tokens"],
            "output_tokens": a["output_tokens"],
            "cached_tokens": a["cached_tokens"],
            "total_tokens": a["total_tokens"],
            "cost": None if unk else money(a["cost_cents"]),
            "cost_partial": email in partial_users,
            "credit_applied": None if unk else money(a["credit_cents"]),
            "chargeable": None if unk else money(a["chargeable_cents"]),
            "projects": sorted(a["by_project"], key=lambda x: (-(x["chargeable"] or 0), x["project"])),
        })
    user_rows.sort(key=lambda r: (-(r["chargeable"] if r["chargeable"] is not None else -1.0), -r["total_tokens"], r["email"]))

    project_rows = [
        {
            "project": proj,
            "active_users": len(a["users"]),
            "requests": a["requests"],
            "total_tokens": a["total_tokens"],
            "cost": money(a["cost_cents"]),
            "credit_applied": money(a["credit_cents"]),
            "chargeable": money(a["chargeable_cents"]),
        }
        for proj, a in projects_acc.items()
    ]
    project_rows.sort(key=lambda r: (-(r["chargeable"] or 0), r["project"]))

    unattributed = []
    for proj, prec in sorted(usage.unattributed.items()):
        if project_f != "all" and proj != project_f:
            continue
        cost, partial = pricing_svc.estimate_cost(prec, pricing)
        unattributed.append({"project": proj, "requests": int(prec.get("requests") or 0),
                             "total_tokens": int(prec.get("total_tokens") or 0),
                             "estimated_cost": money(to_cents(cost)) if cost is not None else None,
                             "cost_partial": partial, "reasons": dict(prec.get("reasons") or {}),
                             "by_model": usage_svc.model_rows(prec.get("by_model"), pricing)})
    page = paginate_list(user_rows, ctx.params, default_limit=100, max_limit=500)
    page["unattributed"] = unattributed
    page["pricing_source"] = pricing_svc.source_summary(pricing)
    page["telemetry"] = usage_svc.usage_meta(usage)
    page["month"] = month
    page["currency"] = pricing.currency
    page["allocation_mode"] = mode
    page["complete"] = complete
    page["by_project"] = project_rows
    page["totals"] = {
        "users": len(user_rows),
        "projects": len(project_rows),
        "requests": grand["requests"],
        "total_tokens": grand["total_tokens"],
        "cost": money(grand["cost_cents"]),
        "credit_applied": money(grand["credit_cents"]),
        "chargeable": money(grand["chargeable_cents"]),
    }
    return page
