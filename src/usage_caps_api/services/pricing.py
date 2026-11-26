"""Three-tier automatic token pricing and per-user cost estimation.

Precedence:
1. Manual overrides (policy/config `model_overrides`)
2. Billing export effective prices (`SUM(cost)/SUM(usage.amount)` over last 30 days)
3. Official synced Cloud Billing Catalog prices (`pricing.json`) or live Catalog API lookup
"""

from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass, field

from .. import timeutil
from ..gcp import GcpError
from ..schema import expr
from . import models_catalog as mc
from .base import range_params, to_float, to_int

LOG = logging.getLogger("usage_caps_api.pricing")

CATALOG_V1 = "https://cloudbilling.googleapis.com/v1"
BILLING_DAYS = 30
CACHE_SECONDS = 6 * 3600
MAX_CATALOG_PAGES = 8
MAX_USD_PER_SINGLE_TOKEN = 0.0005  # $500 / 1M tokens

VARIANT_WORDS = {
    "batch", "flex", "priority", "offpeak", "peak", "caching", "cached", "cache", "live",
    "provisioned", "tuning", "tuned", "audio", "image", "images", "video", "cyber", "bidi",
    "embedding", "embeddings", "grounding", "search", "contributor", "deferred", "offline",
    "lyria", "music", "song", "clip", "clips", "track", "tracks", "veo", "imagen", "chirp",
}
CACHED_VARIANT_WORDS = VARIANT_WORDS - {"caching", "cached", "cache"} | {"storage", "hour", "hours"}
LONG_CONTEXT_RE = re.compile(r"(?i)(?:>|\u2265|over|above)\s*\d|long context|long input|long output")
WORD_RE = re.compile(r"[a-z0-9.]+")
CREDIT_MODEL_PREFIXES = ("gemini",)
_MODEL_PATH_PREFIXES = ("publishers/google/models/", "models/")


def credit_eligible(model: str, prefixes=CREDIT_MODEL_PREFIXES) -> bool:
    key = str(model or "").strip().lower()
    for p in _MODEL_PATH_PREFIXES:
        if key.startswith(p):
            key = key[len(p):]
    return bool(key) and any(key.startswith(str(pref).lower()) for pref in prefixes if pref)


def token_sku(description, service=None, unit=None, pricing_unit=None):
    if not description or not isinstance(description, str):
        return None
    low = description.lower()
    if set(WORD_RE.findall(low)) & VARIANT_WORDS or LONG_CONTEXT_RE.search(low):
        return None
    info = mc.parse_sku(description, service)
    if not info or mc.is_non_token_model(info["key"]):
        return None
    measure = mc.classify_measure(description, unit, pricing_unit)
    if measure == "input_tokens":
        return info["key"], "input", info["product"]
    if measure == "output_tokens":
        return info["key"], "output", info["product"]
    return None


def cached_token_sku(description, service=None, unit=None, pricing_unit=None):
    if not description or not isinstance(description, str):
        return None
    low = description.lower()
    if "cach" not in low or set(WORD_RE.findall(low)) & CACHED_VARIANT_WORDS or LONG_CONTEXT_RE.search(low):
        return None
    info = mc.parse_sku(description, service)
    if not info or mc.is_non_token_model(info["key"]):
        return None
    if mc.classify_measure(description, unit, pricing_unit) != "cached_tokens":
        return None
    return info["key"], "cached", info["product"]


def money(unit: dict | None) -> float:
    return int((unit or {}).get("units") or 0) + ((unit or {}).get("nanos") or 0) / 1e9


def first_positive_money(tiers) -> float:
    for tier in tiers or []:
        val = money((tier or {}).get("unitPrice"))
        if val > 0:
            return val
    return 0.0


def effective_prices(rows):
    """Computes ({model_key: {"input", "output"}}, currency, usd_rate) from billing rows."""
    buckets: dict = {}
    currency, rates = None, []
    for row in rows or []:
        hit = token_sku(row.get("sku"), row.get("service"), row.get("unit"), row.get("pricing_unit"))
        amount = to_float(row.get("amount")) or 0.0
        cost = to_float(row.get("cost")) or 0.0
        if not hit or amount <= 0 or cost <= 0:
            continue
        key, direction, prod = hit
        rank = 1 if prod else 0
        agg = buckets.setdefault((key, direction, rank), [0.0, 0.0])
        agg[0] += cost
        agg[1] += amount
        currency = currency or row.get("currency")
        rate = to_float(row.get("fx"))
        if rate:
            rates.append(rate)
    best_rank: dict = {}
    for key, direction, rank in buckets:
        cur = best_rank.get((key, direction))
        if cur is None or rank < cur:
            best_rank[(key, direction)] = rank
    out: dict = {}
    for (key, direction), rank in best_rank.items():
        cost, amount = buckets[(key, direction, rank)]
        out.setdefault(key, {})[direction] = cost / amount * 1e6
    return out, currency, (statistics.mean(rates) if rates else None)


def billing_price_rows(ctx, src) -> list[dict]:
    f = mc.billing_fields(src)
    if not (f["sku"] and f["cost"] and f["amount"]):
        return []
    rate = expr("b", src.schema, "currency_conversion_rate", cast="FLOAT64") or "CAST(NULL AS FLOAT64)"
    null = "CAST(NULL AS STRING)"
    rng = timeutil.parse_range({}, ctx.tz_name, BILLING_DAYS)
    sql = (
        "WITH %s\nSELECT %s AS sku, %s AS service, %s AS unit, %s AS pricing_unit, "
        "SUM(IFNULL(%s, 0)) AS amount, SUM(IFNULL(%s, 0)) AS cost, "
        "ANY_VALUE(%s) AS currency, AVG(%s) AS fx\n"
        "FROM bil b WHERE %s AND %s\nGROUP BY sku, service, unit, pricing_unit"
        % (src.cte("bil", start="start_ts"), f["sku"], f["service"] or null, f["unit"] or null,
           f["pricing_unit"] or null, f["amount"], f["cost"], f["currency"] or null, rate,
           mc.ai_clause(f), mc.excluded_clause(f))
    )
    params = range_params(rng)
    params["excluded"] = ctx.excluded_patterns
    return ctx.query(sql, params, "pricing")


@dataclass
class PriceTable:
    currency: str
    prices: dict = field(default_factory=dict)  # key -> {"input", "output", "cached"?, "source"}
    fx: dict | None = None
    counts: dict = field(default_factory=dict)
    aliases: dict = field(default_factory=dict)

    def lookup(self, model: str | None) -> dict | None:
        key = mc.normalize_model_id(str(model or ""))
        if not key:
            return None
        if key in self.prices:
            return self.prices[key]
        target = (self.aliases or {}).get(key)
        if target and target in self.prices:
            return self.prices[target]
        best = None
        for cand in self.prices:
            if key.startswith(cand + "-") and re.match(r"^[0-9][0-9\-]*$", key[len(cand) + 1:]):
                if best is None or len(cand) > len(best):
                    best = cand
        return self.prices[best] if best else None


@dataclass(frozen=True)
class Pricing:
    currency: str = "USD"
    input_per_million: float = 0.0
    output_per_million: float = 0.0
    models: tuple = ()         # ((prefix, input, output), ...) longest first
    cached_models: tuple = ()  # ((prefix, cached), ...) longest first
    source: str = "auto"
    table: PriceTable | None = None

    @property
    def configured(self) -> bool:
        if any(pin > 0 or pout > 0 for _, pin, pout in self.models):
            return True
        if self.source == "auto" and self.table is not None and self.table.prices:
            return True
        return self.input_per_million > 0 or self.output_per_million > 0

    def price(self, model: str | None) -> tuple[float, float, bool]:
        key = str(model or "").lower()
        for prefix, pin, pout in self.models:
            if key and (key == prefix or key.startswith(prefix)):
                return pin, pout, True
        if self.source == "auto" and self.table is not None and model:
            hit = self.table.lookup(model)
            if hit:
                pin, pout = float(hit.get("input") or 0), float(hit.get("output") or 0)
                return pin, pout, bool(pin and pout)
            return 0.0, 0.0, False
        has_def = self.input_per_million > 0 or self.output_per_million > 0
        return self.input_per_million, self.output_per_million, has_def

    def cached_price(self, model: str | None, input_price: float) -> tuple[float, bool]:
        key = str(model or "").lower()
        for prefix, pcached in self.cached_models:
            if key and (key == prefix or key.startswith(prefix)):
                return pcached, True
        if self.source == "auto" and self.table is not None and model:
            hit = self.table.lookup(model) or {}
            if hit.get("cached") is not None:
                return float(hit["cached"]), True
        return input_price, False


def estimate_cost(rec: dict, pricing: Pricing | None) -> tuple[float | None, bool]:
    """Returns (estimated_cost, partial) for a usage record with per-model token counts.

    - Uncached input tokens (prompt - cached) are priced at input rate.
    - Prompt cache hit tokens (cached) are priced at cached rate (falling back to input rate).
    - Output & thinking tokens (`max(total - prompt, candidate, 0)`) are priced at output rate.
    """
    if pricing is None or not pricing.configured:
        return None, False
    parts = list((rec.get("by_model") or {}).items()) or [(None, rec)]
    total = 0.0
    partial = False
    priced_tokens = 0
    used_tokens = 0
    for model, agg in parts:
        tokens = to_int(agg.get("total_tokens") if "total_tokens" in agg else agg.get("tokens"))
        if model and mc.is_non_token_model(model):
            continue
        prompt = max(0, to_int(agg.get("input_tokens") if "input_tokens" in agg else agg.get("promptTokens")))
        cached = min(max(0, to_int(agg.get("cached_tokens") if "cached_tokens" in agg else agg.get("cachedTokens"))), prompt)
        cand = max(0, to_int(agg.get("output_tokens") if "output_tokens" in agg else agg.get("candidateTokens")))
        output = max(tokens - prompt, cand, 0)
        pin, pout, priced = pricing.price(model)
        pcached, _ = pricing.cached_price(model, pin)
        used_tokens += tokens
        if pin or pout:
            priced_tokens += tokens
        if not priced and tokens > 0:
            partial = True
        total += (prompt - cached) * pin / 1e6 + cached * pcached / 1e6 + output * pout / 1e6
    if used_tokens > 0 and priced_tokens == 0:
        return None, True
    return round(total, 6), partial


def split_cost(rec: dict, pricing: Pricing | None, prefixes=CREDIT_MODEL_PREFIXES):
    """Splits cost into (credit_eligible_gemini_cost, paid_other_cost, known, partial)."""
    gemini, other = {}, {}
    for model, agg in (rec.get("by_model") or {}).items():
        (gemini if credit_eligible(model, prefixes) else other)[model] = agg
    costs, known, partial = [], True, False
    for part in (gemini, other):
        if not part:
            costs.append(0.0)
            continue
        cost, p_partial = estimate_cost({"by_model": part}, pricing)
        partial = partial or p_partial
        if cost is None:
            known = False
            cost = 0.0
        costs.append(cost)
    return costs[0], costs[1], known, partial


def combine(currency: str, billing: dict, billing_currency: str | None, billing_usd_rate: float | None,
            catalog: dict, usd_rate: float | None, effective: str | None) -> tuple[PriceTable, str | None]:
    warning = None
    factor = None
    if billing:
        if not billing_currency or billing_currency == currency:
            factor = 1.0
        elif usd_rate:
            if billing_currency == "USD":
                factor = usd_rate
            elif billing_usd_rate:
                factor = usd_rate / billing_usd_rate
        if factor is None:
            warning = ("Billing export prices in %s could not be converted to %s; using catalog prices."
                       % (billing_currency, currency))
    prices = {k: dict(v, source="catalog") for k, v in (catalog or {}).items()}
    billed = 0
    if factor:
        for k, entry in billing.items():
            merged = dict(prices.get(k) or {})
            merged.update({d: v * factor for d, v in entry.items()})
            merged["source"] = "billing"
            prices[k] = merged
            billed += 1
    fx = {"from": "USD", "to": currency, "rate": round(usd_rate, 6), "as_of": effective} if (usd_rate and currency != "USD") else None
    counts = {"billing": billed, "catalog": sum(1 for e in prices.values() if e.get("source") == "catalog")}
    return PriceTable(currency=currency, prices=prices, fx=fx, counts=counts), warning


def synced_prices(ctx, currency: str):
    doc, _ = ctx.store.load("pricing")
    if not (doc.get("synced_at") and doc.get("models")):
        return None
    if currency == "USD":
        rate, as_of = 1.0, doc.get("synced_at")
    else:
        item = ((doc.get("fx") or {}).get("rates") or {}).get(currency) or {}
        rate, as_of = to_float(item.get("rate")), item.get("as_of")
        if not rate:
            return None
    out = {}
    for key, item in (doc.get("models") or {}).items():
        if not isinstance(item, dict):
            continue
        entry = {d: float(item[d]) * rate for d in ("input", "output", "cached") if to_float(item.get(d)) is not None}
        if entry:
            out[key] = entry
    return out, rate, as_of


def build_price_table(ctx, currency: str | None = None) -> PriceTable:
    from . import pricing_sync
    p_cfg = ctx.config.get("pricing") or {}
    billing, billing_currency, billing_rate = {}, None, None
    src = ctx.billing_source()
    if src is not None:
        try:
            billing, billing_currency, billing_rate = effective_prices(billing_price_rows(ctx, src))
            ctx.add_source(src.label)
        except GcpError as err:
            ctx.warn_error(src.label, err, "Billing export token prices")
    cap_currency = (currency or p_cfg.get("currency") or billing_currency or "USD").upper()
    catalog, usd_rate, effective = {}, None, None
    synced = synced_prices(ctx, cap_currency)
    if synced is not None:
        catalog, usd_rate, effective = synced
        ctx.add_source("pricing:synced_catalog")
    elif getattr(ctx.app, "gcp", None) is not None and type(ctx.app.gcp).__name__ == "Gcp":
        try:
            catalog, usd_rate, effective = pricing_sync.live_catalog_prices(ctx, cap_currency)
            ctx.add_source("api:cloudbilling.services.skus")
        except GcpError as err:
            ctx.warn_error("api:cloudbilling.services.skus", err, "Cloud Billing Catalog prices")
    table, warning = combine(cap_currency, billing, billing_currency, billing_rate, catalog, usd_rate, effective)
    aliases = {}
    for item in p_cfg.get("model_aliases") or []:
        left, _, right = str(item).partition("=")
        if left and right:
            aliases[mc.normalize_model_id(left)] = mc.normalize_model_id(right)
    table.aliases = aliases
    ovr_factor = 1.0 if cap_currency == "USD" else (usd_rate or None)
    if ovr_factor:
        for item in p_cfg.get("model_overrides") or []:
            if not isinstance(item, dict) or not item.get("model"):
                continue
            mkey = mc.normalize_model_id(item["model"])
            entry = {d: float(item[d]) * ovr_factor for d in ("input", "output", "cached")
                     if to_float(item.get(d)) is not None}
            if mkey and entry:
                table.prices[mkey] = dict(entry, source="override")
    if warning:
        ctx.warn("pricing:fx", "fx_unavailable", warning)
    return table


def resolve_pricing(ctx, policy: dict | None = None) -> Pricing:
    p_cfg = ctx.config.get("pricing") or {}
    pol = policy or {}
    currency = (pol.get("currency") or p_cfg.get("currency") or "USD").upper()
    source = pol.get("pricing_source") or p_cfg.get("source") or "auto"
    raw_overrides = list(pol.get("model_prices") or []) + list(p_cfg.get("model_overrides") or [])
    models, cached_models = [], []
    seen_m, seen_c = set(), set()
    for item in raw_overrides:
        if not isinstance(item, dict) or not item.get("model"):
            continue
        m = str(item["model"]).strip().lower()
        if m not in seen_m:
            seen_m.add(m)
            models.append((m, float(item.get("input") or 0), float(item.get("output") or 0)))
        if item.get("cached") not in (None, "") and m not in seen_c:
            seen_c.add(m)
            cached_models.append((m, float(item["cached"])))
    models.sort(key=lambda x: (-len(x[0]), x[0]))
    cached_models.sort(key=lambda x: (-len(x[0]), x[0]))
    table = None
    if source == "auto":
        try:
            table = ctx.memo("token_prices", currency, lambda c: build_price_table(c, currency), ttl=CACHE_SECONDS)
            if table is not None:
                currency = table.currency
        except GcpError:
            table = None
    return Pricing(
        currency=currency,
        input_per_million=float(p_cfg.get("input_price_per_million") or 0),
        output_per_million=float(p_cfg.get("output_price_per_million") or 0),
        models=tuple(models),
        cached_models=tuple(cached_models),
        source=source,
        table=table,
    )
