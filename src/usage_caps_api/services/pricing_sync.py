"""Official Cloud Billing Catalog SKU price & FX synchronization (the only automatic price source).

Every AI-model service in the public Cloud Billing Catalog is scanned (configured service-name patterns plus any
service whose name identifies a model, e.g. partner models in Model Garden). Each SKU description is mapped to a
model key + price dimension (input / output / cached / cache_write) + tier (standard / long_context / flex /
priority / batch) without any built-in model list: model families are learned from the live model catalog and the
models observed in logs, so newly launched models are priced as soon as Google publishes their SKUs.
"""

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


def learn_families(ctx) -> int:
    """Teaches the SKU parser every model family seen in the model catalog / logs (dynamic, no hardcoding)."""
    try:
        doc, _ = ctx.store.load("model_catalog")
    except Exception:  # pylint: disable=broad-except
        return 0
    ids = list((doc.get("models") or {}).keys()) + list((doc.get("observed") or {}).keys())
    return mc.learn_models(ids)


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


def catalog_entries(skus, service: str | None = None, excluded=()):
    """Yields (key, direction, tier, threshold, rank, price_per_token, fx_rate, effective_time, sku_name)."""
    for sku in skus or []:
        info = ((sku.get("pricingInfo") or [None])[0]) or {}
        pexpr = info.get("pricingExpression") or {}
        rate = to_float(info.get("currencyConversionRate"))
        eff = info.get("effectiveTime")
        desc = sku.get("description") or ""
        low = desc.lower()
        if pexpr.get("usageUnit") != "count" or any(p and p in low for p in excluded):
            yield None, None, None, None, None, None, rate, eff, None
            continue
        hit = pricing.classify_sku(desc, service, pexpr.get("usageUnit"), pexpr.get("usageUnitDescription"))
        tiers = pexpr.get("tieredRates") or []
        if not hit or not tiers:
            yield None, None, None, None, None, None, rate, eff, None
            continue
        price = pricing.first_positive_money(tiers) / float(pexpr.get("baseUnitConversionFactor") or 1)
        usd_per_token = price / float(rate if rate and rate > 0 else 1.0)
        if usd_per_token <= 0 or usd_per_token > pricing.MAX_USD_PER_SINGLE_TOKEN:
            yield None, None, None, None, None, None, rate, eff, None
            continue
        key, direction, prod, tier, threshold = hit
        rank = (1 if prod else 0, 0 if "global" in low else 1)
        yield key, direction, tier, threshold, rank, price, rate, eff, sku.get("name") or sku.get("skuId")


def _collapse(candidates: dict, scale: float = 1e6) -> dict:
    """{(key, tier, direction): [(rank, price, threshold, sku)]} -> {key: {input,..., tiers: {...}, skus: {...}}}."""
    out: dict = {}
    for (key, tier, direction), items in candidates.items():
        best = min(r for r, _, _, _ in items)
        chosen = [it for it in items if it[0] == best]
        per_m = round(statistics.median(sorted(p for _, p, _, _ in chosen)) * scale, 6)
        if not 0 < per_m <= MAX_TOKEN_USD_PER_MILLION * (scale / 1e6):
            continue
        entry = out.setdefault(key, {"source": "cloud_billing_catalog"})
        sku = next((s for _, _, _, s in chosen if s), None)
        if tier is None:
            entry[direction] = per_m
            if sku:
                entry.setdefault("skus", {})[direction] = sku
        else:
            te = entry.setdefault("tiers", {}).setdefault(tier, {})
            te[direction] = per_m
            thr = next((t for _, _, t, _ in chosen if t), None)
            if thr:
                te["threshold_tokens"] = thr
            if sku:
                entry.setdefault("skus", {})["%s.%s" % (tier, direction)] = sku
    return out


def _scan(ctx, services, currency: str):
    candidates: dict = {}
    usd_rate, effective = None, None
    excluded = list(ctx.excluded_patterns)
    for svc in services:
        try:
            for key, direction, tier, threshold, rank, price, rate, eff, sku in catalog_entries(
                    _service_skus(ctx, svc["id"], currency), svc["name"], excluded):
                if usd_rate is None and rate:
                    usd_rate, effective = rate, eff
                if key and price and price > 0:
                    candidates.setdefault((key, tier, direction), []).append((rank, price, threshold, sku))
        except GcpError as err:
            LOG.warning("catalog sync skipped service=%s kind=%s", svc["name"], err.kind)
    return candidates, usd_rate, effective


def live_catalog_prices(ctx, currency: str = "USD"):
    learn_families(ctx)
    services = select_model_services(list_catalog_services(ctx), ctx.config)
    candidates, usd_rate, effective = _scan(ctx, services, currency)
    return _collapse(candidates), usd_rate, effective


def sync_pricing(ctx) -> dict:
    """POST /v1/internal/pricing/sync: refreshes official Cloud Billing Catalog SKU prices & FX in `pricing`."""
    now = now_iso()
    learned = learn_families(ctx)
    services = select_model_services(list_catalog_services(ctx), ctx.config)
    candidates, _rate, _eff = _scan(ctx, services, "USD")
    models_out = _collapse(candidates)

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
        from . import webhooks as _wh
        for url in (ctx.config.get("pricing") or {}).get("fx_sources") or []:
            if not config.valid_https_url(url, check_dns=True):
                continue
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "GcpAiUsageCapsApi/1.2"})
                with _wh._NO_REDIRECT_OPENER.open(req, timeout=10) as resp:  # pylint: disable=protected-access
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
        doc["method"] = "official_cloud_billing_catalog_skus"
        return doc

    ctx.store.mutate("pricing", save)
    if getattr(ctx.app, "cache", None) is not None:
        ctx.app.cache.clear("token_prices")
    return {
        "synced_at": now,
        "services_scanned": len(services),
        "models_synced": len(models_out),
        "models_with_tiers": sum(1 for v in models_out.values() if v.get("tiers")),
        "families_learned": learned,
        "currencies_synced": sorted(rates.keys()),
    }


def get_pricing(ctx) -> dict:
    """GET /v1/pricing?model=: the official SKU price table actually used for cost estimates."""
    from . import caps as caps_svc
    pr = pricing.resolve_pricing(ctx, caps_svc.effective_policy(ctx))
    want = (ctx.params.get("model") or "").strip().lower()
    rows = []
    table = pr.table
    for key, entry in sorted(((table.prices if table else {}) or {}).items()):
        if want and want not in key and mc.normalize_model_id(want) != key:
            continue
        rows.append({"model_key": key, "source": entry.get("source"),
                     **{d: entry.get(d) for d in pricing.DIRECTIONS},
                     "tiers": entry.get("tiers") or {}, "skus": entry.get("skus") or {}})
    for prefix, pin, pout in pr.models:
        if not want or want in prefix:
            rows.append({"model_key": prefix, "source": "override", "input": pin, "output": pout,
                         "cached": next((c for p, c in pr.cached_models if p == prefix), None),
                         "cache_write": None, "tiers": {}, "skus": {}})
    return {"object": "pricing_table", "currency": pr.currency, "unit": "per_1m_tokens",
            "pricing_source": pricing.source_summary(pr), "fx": table.fx if table else None, "data": rows}
