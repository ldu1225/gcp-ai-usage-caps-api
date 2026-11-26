"""Official Cloud Billing Catalog token price & FX synchronization."""

from __future__ import annotations

import json
import logging
import statistics
import urllib.request

from .. import config
from ..gcp import GcpError
from ..httpkit import now_iso
from . import models_catalog as mc
from . import pricing
from .base import to_float

LOG = logging.getLogger("usage_caps_api.pricing_sync")

MAX_SERVICE_PAGES = 4
MAX_SKU_PAGES = 8
MAX_TOKEN_USD_PER_MILLION = 500.0


def list_catalog_services(ctx) -> list[dict]:
    out, token = [], None
    for _ in range(MAX_SERVICE_PAGES):
        params = {"pageSize": 5000}
        if token:
            params["pageToken"] = token
        data = ctx.rest("GET", "%s/services" % pricing.CATALOG_V1, params=params, timeout=60)
        for svc in (data or {}).get("services") or []:
            sid, name = svc.get("serviceId"), svc.get("displayName")
            if isinstance(sid, str) and isinstance(name, str) and sid and name:
                out.append({"id": sid[:40], "name": name[:120]})
        token = (data or {}).get("nextPageToken")
        if not token:
            break
    return out


def select_model_services(services: list[dict], cfg: dict) -> list[dict]:
    p_cfg = cfg.get("pricing") or {}
    patterns = [s.lower() for s in (p_cfg.get("catalog_services") or []) if s]
    out, seen = [], set()
    for svc in services or []:
        low = svc["name"].lower()
        match = any(pat in low for pat in patterns)
        if not match and p_cfg.get("include_partner_models", True) and mc.is_model_service(svc["name"]):
            info = mc.parse_sku(svc["name"])
            match = bool(info and not info.get("product"))
        if match and svc["id"] not in seen:
            seen.add(svc["id"])
            out.append(svc)
    return out


def _service_skus(ctx, service_id: str, currency: str = "USD"):
    token = None
    for _ in range(MAX_SKU_PAGES):
        params = {"currencyCode": currency, "pageSize": 5000}
        if token:
            params["pageToken"] = token
        data = ctx.rest("GET", "%s/services/%s/skus" % (pricing.CATALOG_V1, service_id), params=params, timeout=60)
        for sku in (data or {}).get("skus") or []:
            yield sku
        token = (data or {}).get("nextPageToken")
        if not token:
            break


def catalog_entries(skus, service: str | None = None):
    for sku in skus or []:
        info = ((sku.get("pricingInfo") or [None])[0]) or {}
        pexpr = info.get("pricingExpression") or {}
        rate = to_float(info.get("currencyConversionRate"))
        if pexpr.get("usageUnit") != "count":
            continue
        desc = sku.get("description") or ""
        hit = pricing.token_sku(desc, service, pexpr.get("usageUnit"), pexpr.get("usageUnitDescription"))
        if not hit:
            hit = pricing.cached_token_sku(desc, service, pexpr.get("usageUnit"), pexpr.get("usageUnitDescription"))
        tiers = pexpr.get("tieredRates") or []
        if not hit or not tiers:
            yield None, None, None, None, rate, info.get("effectiveTime")
            continue
        price = pricing.first_positive_money(tiers) / float(pexpr.get("baseUnitConversionFactor") or 1)
        usd_per_token = price / float(rate if rate and rate > 0 else 1.0)
        if usd_per_token <= 0 or usd_per_token > pricing.MAX_USD_PER_SINGLE_TOKEN:
            yield None, None, None, None, rate, info.get("effectiveTime")
            continue
        key, direction, prod = hit
        rank = (1 if prod else 0, 0 if "global" in desc.lower() else 1)
        yield key, direction, rank, price, rate, info.get("effectiveTime")


def live_catalog_prices(ctx, currency: str = "USD"):
    services = select_model_services(list_catalog_services(ctx), ctx.config)
    candidates: dict = {}
    usd_rate, effective = None, None
    for svc in services:
        for key, direction, rank, price, rate, eff in catalog_entries(_service_skus(ctx, svc["id"], currency), svc["name"]):
            if usd_rate is None and rate:
                usd_rate, effective = rate, eff
            if key and price and price > 0:
                candidates.setdefault((key, direction), []).append((rank, price))
    out: dict = {}
    for (key, direction), items in candidates.items():
        best = min(r for r, _ in items)
        out.setdefault(key, {})[direction] = round(statistics.median(sorted(p for r, p in items if r == best)) * 1e6, 6)
    return out, usd_rate, effective


def sync_pricing(ctx) -> dict:
    """POST /v1/internal/pricing/sync: refreshes official Cloud Billing Catalog prices & FX in pricing.json."""
    now = now_iso()
    services = select_model_services(list_catalog_services(ctx), ctx.config)
    candidates: dict = {}
    for svc in services:
        try:
            for key, direction, rank, price, _rate, _eff in catalog_entries(_service_skus(ctx, svc["id"], "USD"), svc["name"]):
                if key and price and price > 0:
                    candidates.setdefault((key, direction), []).append((rank, price))
        except GcpError as err:
            LOG.warning("catalog sync skipped service=%s kind=%s", svc["name"], err.kind)
    models_out: dict = {}
    for (key, direction), items in candidates.items():
        best = min(r for r, _ in items)
        per_m = round(statistics.median(sorted(p for r, p in items if r == best)) * 1e6, 6)
        if 0 < per_m <= MAX_TOKEN_USD_PER_MILLION:
            models_out.setdefault(key, {"source": "cloud_billing_catalog"})[direction] = per_m

    # Fetch FX for configured non-USD currencies
    cur = ((ctx.config.get("pricing") or {}).get("currency") or "USD").upper()
    rates: dict = {}
    if cur != "USD" and services:
        try:
            data = ctx.rest("GET", "%s/services/%s/skus" % (pricing.CATALOG_V1, services[0]["id"]),
                            params={"currencyCode": cur, "pageSize": 5}, timeout=30)
            for sku in (data or {}).get("skus") or []:
                info = ((sku.get("pricingInfo") or [None])[0]) or {}
                r = to_float(info.get("currencyConversionRate"))
                if r and r > 0:
                    rates[cur] = {"rate": round(r, 6), "source": "cloud_billing_catalog", "as_of": info.get("effectiveTime") or now}
                    break
        except GcpError:
            pass
    if cur != "USD" and cur not in rates:
        for url in (ctx.config.get("pricing") or {}).get("fx_sources") or []:
            if not config.valid_https_url(url, check_dns=True):
                continue
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "GcpAiUsageCapsApi/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
                    payload = json.loads(resp.read(512 * 1024).decode("utf-8"))
                r = to_float((payload.get("rates") or {}).get(cur))
                if r and r > 0:
                    rates[cur] = {"rate": round(r, 6), "source": url, "as_of": now}
                    break
            except Exception:
                continue

    def save(doc):
        doc["synced_at"] = now
        doc["synced_by"] = ctx.caller.id
        doc["catalog_services"] = services
        doc["models"] = models_out
        doc["fx"] = {"base": "USD", "rates": rates, "updated_at": now}
        return doc

    ctx.store.mutate("pricing", save)
    if getattr(ctx.app, "cache", None) is not None:
        ctx.app.cache.clear("token_prices")
    return {
        "synced_at": now,
        "services_scanned": len(services),
        "models_synced": len(models_out),
        "currencies_synced": sorted(rates.keys()),
    }
