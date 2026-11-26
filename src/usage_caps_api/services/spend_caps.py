"""Project-level Google Cloud Spend Caps via Cloud Billing Budget API v1.

Manages Cloud Billing spend cap budgets for workspace projects targeting Gemini Platform
(`services/C7E2-9256-1C43`). Includes all safety guards from `ai-control-plane`:
- Workspace project allowlist (never pauses projects outside workspace scope)
- USD billing account guard (no stale FX conversion)
- 60-char display name check (`GCP AI Cap: <project_id>`)
- Strict ownership verification (`_not_owned`: exact project number + Agent Platform service + spendCap present)
- Explicit administrator lift (`AWAITING_NEXT_PERIOD`) for ENFORCED caps.
"""

from __future__ import annotations

import decimal
import logging
import re
import urllib.parse

from .. import config
from ..gcp import GcpError
from ..httpkit import ApiError, now_iso
from . import audit, chargeback, webhooks

LOG = logging.getLogger("usage_caps_api.spend_caps")

PROJECT_CAPS_DOC = "project_caps"
BUDGETS_V1 = "https://billingbudgets.googleapis.com/v1"
BILLING_V1 = "https://cloudbilling.googleapis.com/v1"
CRM_V3 = "https://cloudresourcemanager.googleapis.com/v3"

DISPLAY_PREFIX = "GCP AI Cap: "
MAX_DISPLAY_NAME = 60
SERVICE_NAMES = ("Agent Platform", "Gemini Enterprise Agent Platform", "Vertex AI")
SERVICE_ID = "C7E2-9256-1C43"
THRESHOLDS = (0.5, 0.8, 1.0)
CONFIGURED = "CONFIGURED"
ENFORCED = "ENFORCED"
AWAITING_NEXT_PERIOD = "AWAITING_NEXT_PERIOD"
STATES = ("STATE_UNSPECIFIED", CONFIGURED, ENFORCED, AWAITING_NEXT_PERIOD)
MAX_AMOUNT = 1e9
LOOKUP_TTL = 24 * 3600

SERVICE_RE = re.compile(r"services/[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}")
BUDGET_NAME_RE = re.compile(r"billingAccounts/([0-9A-Fa-f]{6}-[0-9A-Fa-f]{6}-[0-9A-Fa-f]{6})/budgets/[A-Za-z0-9_\-]{1,128}")
PROJECT_NAME_RE = re.compile(r"projects/([0-9]{1,30})")
CURRENCY_RE = re.compile(r"[A-Z]{3}")

NOT_OWNED = "Budget display name matches, but filter does not exclusively cover this project and Agent Platform."


def enabled(cfg: dict) -> bool:
    return ((cfg or {}).get("caps") or {}).get("spend_caps_enabled") is True


def account_id(cfg: dict) -> str:
    raw = (((cfg or {}).get("workspace") or {}).get("billing") or {}).get("account_id") or ""
    val = raw.strip().upper() if isinstance(raw, str) else ""
    return val if config.ACCOUNT_RE.match(val) else ""


def _gate(cfg: dict) -> str | None:
    if not enabled(cfg):
        return "Google Cloud Billing Spend Caps are disabled (set SPEND_CAPS_ENABLED=true)."
    if not account_id(cfg):
        return "Billing account ID (BILLING_ACCOUNT_ID) is required for project spend caps."
    return None


def display_name(project: str) -> str:
    return DISPLAY_PREFIX + project


def _money(amount: float, currency: str) -> dict:
    cents = chargeback.to_cents(amount) or 0
    return {"currencyCode": currency, "units": str(cents // 100), "nanos": (cents % 100) * 10000000}


def _amount_of(budget: dict) -> tuple[float | None, str | None]:
    spec = (budget.get("amount") or {}).get("specifiedAmount") if isinstance(budget.get("amount"), dict) else None
    if not isinstance(spec, dict):
        return None, None
    code = spec.get("currencyCode")
    code = code if isinstance(code, str) and CURRENCY_RE.fullmatch(code) else None
    try:
        val = decimal.Decimal(int(spec.get("units") or 0)) + decimal.Decimal(int(spec.get("nanos") or 0)) / 10 ** 9
        return float(val), code
    except (TypeError, ValueError, decimal.InvalidOperation):
        return None, code


def _cached(ctx, key, compute):
    cache = getattr(ctx.app, "cache", None)
    if cache is not None:
        hit, val = cache.get(key)
        if hit:
            return val
    val = compute()
    if cache is not None and val is not None:
        cache.set(key, val, LOOKUP_TTL)
    return val


def service_name(ctx) -> str | None:
    def compute():
        items, _ = ctx.gcp.paginate("%s/services" % BILLING_V1, quota_project=ctx.quota_project,
                                    items_key="services", page_size=5000, max_pages=5)
        wanted = [n.casefold() for n in SERVICE_NAMES]
        best = None
        for svc in items or ():
            name = svc.get("name")
            if not isinstance(name, str) or not SERVICE_RE.fullmatch(name):
                continue
            label = " ".join(str(svc.get("displayName") or "").split()).casefold()
            if name == "services/" + SERVICE_ID:
                rank = -1
            elif label in wanted:
                rank = wanted.index(label)
            else:
                continue
            if best is None or rank < best[0]:
                best = (rank, name)
        return best[1] if best else None
    return _cached(ctx, ("spend_caps", "service"), compute)


def project_number(ctx, project: str) -> str | None:
    def compute():
        data = ctx.rest("GET", "%s/projects/%s" % (CRM_V3, urllib.parse.quote(project, safe=""))) or {}
        m = PROJECT_NAME_RE.fullmatch(str(data.get("name") or ""))
        return m.group(1) if m else None
    return _cached(ctx, ("spend_caps", "project_number", project), compute)


def account_currency(ctx, acct: str) -> str | None:
    def compute():
        data = ctx.rest("GET", "%s/billingAccounts/%s" % (BILLING_V1, acct)) or {}
        code = data.get("currencyCode")
        return code if isinstance(code, str) and CURRENCY_RE.fullmatch(code) else None
    return _cached(ctx, ("spend_caps", "currency", acct), compute)


def _own_name(name, acct: str) -> bool:
    m = BUDGET_NAME_RE.fullmatch(name) if isinstance(name, str) else None
    return bool(m and m.group(1).upper() == acct)


def _own_project(budget: dict) -> str | None:
    dn = budget.get("displayName")
    if not isinstance(dn, str) or not dn.startswith(DISPLAY_PREFIX):
        return None
    proj = dn[len(DISPLAY_PREFIX):]
    return proj if config.valid_project(proj) else None


def read_live_caps(ctx) -> dict:
    out: dict = {"ok": False, "error": None, "items": {}, "currency": None}
    reason = _gate(ctx.config)
    if reason:
        out["error"] = reason
        return out
    acct = account_id(ctx.config)
    try:
        found, truncated = ctx.gcp.paginate(
            "%s/billingAccounts/%s/budgets" % (BUDGETS_V1, acct),
            quota_project=ctx.quota_project, items_key="budgets", page_size=100, max_pages=100,
        )
    except GcpError as err:
        out["error"] = "Could not read Cloud Billing budgets (%s)." % err.kind
        return out
    if truncated:
        out["error"] = "Billing account exceeds maximum readable budget pages."
        return out
    items, currency = {}, None
    for b in found or ():
        if not isinstance(b, dict):
            continue
        amt, code = _amount_of(b)
        currency = currency or code
        proj = _own_project(b)
        if proj is None or not _own_name(b.get("name"), acct) or proj in items:
            continue
        cap = b.get("spendCap") if isinstance(b.get("spendCap"), dict) else {}
        flt = b.get("budgetFilter") if isinstance(b.get("budgetFilter"), dict) else {}
        items[proj] = {
            "budget": b["name"],
            "amount": amt,
            "currency": code,
            "input_state": cap.get("inputState") if cap.get("inputState") in STATES else None,
            "output_state": cap.get("outputState") if cap.get("outputState") in STATES else None,
            "etag": b.get("etag"),
            "filter_projects": list(flt.get("projects") or []),
            "filter_services": list(flt.get("services") or []),
            "has_spend_cap": isinstance(b.get("spendCap"), dict),
        }
    out.update(ok=True, items=items, currency=currency)
    return out


def _not_owned(ctx, project: str, cap: dict | None) -> str | None:
    if not isinstance(cap, dict) or cap.get("has_spend_cap") is not True:
        return NOT_OWNED
    num, svc = project_number(ctx, project), service_name(ctx)
    if not num or not svc or cap.get("filter_projects") != ["projects/%s" % num] or cap.get("filter_services") != [svc]:
        return NOT_OWNED
    return None


def create_body(project: str, number: str, service: str, amount: float, currency: str = "USD") -> dict:
    return {
        "displayName": display_name(project),
        "budgetFilter": {
            "projects": ["projects/%s" % number],
            "services": [service],
            "creditTypesTreatment": "EXCLUDE_ALL_CREDITS",
            "calendarPeriod": "MONTH",
        },
        "amount": {"specifiedAmount": _money(amount, currency)},
        "thresholdRules": [{"thresholdPercent": t, "spendBasis": "CURRENT_SPEND"} for t in THRESHOLDS],
        "notificationsRule": {"enableProjectLevelRecipients": True},
        "spendCap": {"inputState": CONFIGURED},
    }


def _apply_one(ctx, project: str, amount: float, *, dry_run: bool = False) -> dict:
    if project not in set(ctx.all_projects):
        return {"project": project, "status": "outside_workspace",
                "message": "Project is outside the configured workspace scope."}
    if len(display_name(project)) > MAX_DISPLAY_NAME:
        return {"project": project, "status": "name_too_long",
                "message": "Project ID exceeds 60-character Cloud Billing budget display name limit."}
    reason = _gate(ctx.config)
    if reason:
        return {"project": project, "status": "skipped", "message": reason}
    live = read_live_caps(ctx)
    if not live["ok"]:
        return {"project": project, "status": "failed", "message": live["error"]}
    existing = live["items"].get(project)
    cur = (existing.get("currency") if existing else None) or live.get("currency")
    if cur not in (None, "USD"):
        return {"project": project, "status": "currency_mismatch",
                "message": "Spend caps require a USD billing account (found %s)." % cur}
    if existing is not None and existing.get("output_state") == ENFORCED:
        return {"project": project, "status": "enforced",
                "message": "Google is actively enforcing this cap. Call POST /v1/caps/projects/%s/lift first." % project,
                "budget": existing.get("budget")}
    if existing is not None and chargeback.to_cents(existing.get("amount")) == chargeback.to_cents(amount):
        return {"project": project, "status": "unchanged", "budget": existing.get("budget"),
                "message": "Cloud Billing spend cap already matches USD %.2f." % amount}
    op = "create" if existing is None else "update"
    if dry_run:
        return {"project": project, "op": op, "status": "dry_run", "amount": amount,
                "budget": existing.get("budget") if existing else None}

    acct = account_id(ctx.config)
    try:
        if op == "create":
            acct_cur = cur or account_currency(ctx, acct)
            if acct_cur != "USD":
                return {"project": project, "op": "create", "status": "currency_mismatch",
                        "message": "Billing account currency is %s (USD required)." % (acct_cur or "unknown")}
            svc = service_name(ctx)
            num = project_number(ctx, project)
            if not svc or not num:
                return {"project": project, "op": "create", "status": "failed",
                        "message": "Could not resolve Agent Platform service ID or project number."}
            created = ctx.rest("POST", "%s/billingAccounts/%s/budgets" % (BUDGETS_V1, acct),
                               json_body=create_body(project, num, svc, amount, "USD")) or {}
            return {"project": project, "op": "create", "status": "created", "amount": amount,
                    "budget": created.get("name")}
        problem = _not_owned(ctx, project, existing)
        if problem:
            return {"project": project, "op": "update", "status": "not_owned", "message": problem}
        mask = ["amount.specifiedAmount"]
        body: dict = {"amount": {"specifiedAmount": _money(amount, "USD")}}
        prev_c = chargeback.to_cents(existing.get("amount"))
        if prev_c is not None and (chargeback.to_cents(amount) or 0) > prev_c:
            mask.append("spendCap.inputState")
            body["spendCap"] = {"inputState": CONFIGURED}
        ctx.rest("PATCH", "%s/%s" % (BUDGETS_V1, existing["budget"]),
                 params={"updateMask": ",".join(mask)}, json_body=body)
        return {"project": project, "op": "update", "status": "updated", "amount": amount,
                "budget": existing["budget"]}
    except GcpError as err:
        return {"project": project, "op": op, "status": "failed",
                "message": "Cloud Billing Budget API error (%s)." % err.kind}


# ---------------------------------------------------------------------------
# Public REST handlers: GET/PUT /v1/caps/projects* & POST .../lift
# ---------------------------------------------------------------------------

def list_project_caps(ctx) -> dict:
    """GET /v1/caps/projects"""
    doc, _ = ctx.store.load(PROJECT_CAPS_DOC)
    stored = dict(doc.get("projects") or {})
    live = read_live_caps(ctx) if _gate(ctx.config) is None else {"ok": False, "items": {}, "currency": None, "error": _gate(ctx.config)}
    projects = sorted(set(ctx.all_projects) | set(stored.keys()) | set((live.get("items") or {}).keys()))
    rows = []
    for pid in projects:
        cfg_item = stored.get(pid)
        live_item = (live.get("items") or {}).get(pid)
        rows.append({
            "project_id": pid,
            "in_workspace": pid in set(ctx.all_projects),
            "configured": cfg_item,
            "cloud_billing_spend_cap": {
                "budget": live_item["budget"],
                "amount": live_item["amount"],
                "currency": live_item["currency"],
                "state": live_item["output_state"],
            } if live_item else None,
        })
    return {
        "object": "list",
        "spend_caps_enabled": enabled(ctx.config),
        "billing_account_configured": bool(account_id(ctx.config)),
        "billing_currency": live.get("currency"),
        "sync_error": live.get("error"),
        "data": rows,
        "total": len(rows),
    }


def put_project_cap(ctx) -> dict:
    """PUT /v1/caps/projects/{project_id} {monthly_usd, action?: hard_cap|notify, apply?: bool, dryRun?: bool}"""
    project = (ctx.path_params.get("project_id") or "").strip().lower()
    if not config.valid_project(project):
        raise ApiError("bad_request", "Path parameter 'project_id' must be a valid GCP project ID.")
    body = ctx.body or {}
    raw_amt = body.get("monthly_usd")
    if isinstance(raw_amt, bool) or not isinstance(raw_amt, (int, float)) or not (0 <= raw_amt <= MAX_AMOUNT):
        raise ApiError("bad_request", "Field 'monthly_usd' must be a number between 0 and 1e9.")
    amount = float(raw_amt)
    action = str(body.get("action") or "hard_cap").strip().lower()
    if action not in ("hard_cap", "notify"):
        raise ApiError("bad_request", "Field 'action' must be 'hard_cap' or 'notify'.")
    do_apply = body.get("apply", True)
    dry_run = body.get("dryRun", body.get("dry_run", False))
    if not isinstance(do_apply, bool) or not isinstance(dry_run, bool):
        raise ApiError("bad_request", "Fields 'apply' and 'dryRun' must be booleans.")

    record = {
        "project_id": project,
        "monthly_usd": amount,
        "action": action,
        "updated_at": now_iso(),
        "updated_by": ctx.caller.id,
    }
    if not dry_run:
        def save(doc):
            projs = dict(doc.get("projects") or {})
            projs[project] = record
            doc["projects"] = projs
            return record
        ctx.store.mutate(PROJECT_CAPS_DOC, save)

    apply_result = None
    if action == "hard_cap" and do_apply:
        apply_result = _apply_one(ctx, project, amount, dry_run=dry_run)

    if not dry_run:
        audit.write_audit(ctx, "caps.project.updated", target=project,
                          details={"monthly_usd": amount, "action": action, "apply_result": apply_result})
    return {
        "object": "project_cap",
        "project_id": project,
        "dry_run": dry_run,
        "configured": record,
        "apply_result": apply_result,
    }


def lift_project_cap(ctx) -> dict:
    """POST /v1/caps/projects/{project_id}/lift: lifts an ENFORCED Google Cloud spend cap."""
    project = (ctx.path_params.get("project_id") or "").strip().lower()
    if not config.valid_project(project):
        raise ApiError("bad_request", "Path parameter 'project_id' must be a valid GCP project ID.")
    reason = _gate(ctx.config)
    if reason:
        raise ApiError("not_configured", reason)
    live = read_live_caps(ctx)
    if not live["ok"]:
        raise ApiError("upstream_error", live["error"] or "Failed to read Cloud Billing budgets.")
    item = live["items"].get(project)
    if item is None:
        raise ApiError("not_found", "No managed spend cap found for project '%s'." % project)
    if item.get("output_state") != ENFORCED:
        return {
            "object": "project_cap_lift",
            "project_id": project,
            "status": "not_enforced",
            "current_state": item.get("output_state"),
            "budget": item.get("budget"),
            "message": "Project spend cap is not currently ENFORCED.",
        }
    try:
        problem = _not_owned(ctx, project, item)
        if problem:
            return {"object": "project_cap_lift", "project_id": project, "status": "not_owned", "message": problem}
        ctx.rest("PATCH", "%s/%s" % (BUDGETS_V1, item["budget"]),
                 params={"updateMask": "spendCap.inputState"},
                 json_body={"spendCap": {"inputState": AWAITING_NEXT_PERIOD}})
    except GcpError as err:
        raise ApiError("upstream_error", "Failed to lift spend cap (%s)." % err.kind)

    audit.write_audit(ctx, "caps.project.lifted", target=project, details={"budget": item["budget"]})
    webhooks.dispatch_event(ctx, "project_cap.lifted", {"project_id": project, "budget": item["budget"], "actor": ctx.caller.id})
    return {
        "object": "project_cap_lift",
        "project_id": project,
        "status": "lifted",
        "budget": item["budget"],
        "message": "Spend cap lifted until next month. Google Cloud resumes Agent Platform usage within ~1 hour.",
    }
