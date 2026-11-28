"""Per-user cost = log token counts x official Cloud Billing Catalog SKU prices (no Cloud Billing export).

Precedence (first hit wins per model):
1. Manual overrides (`policy.model_prices` / `pricing.model_overrides`) - contract / discounted prices.
2. Official Cloud Billing Catalog SKU prices (`cloudbilling.googleapis.com/v1/services/{id}/skus`), synced daily into
   the `pricing` document by `POST /v1/internal/pricing/sync` (or fetched live once and cached).

Per-model price entries carry every official SKU dimension the logs can be matched against:
`input`, `output` (thinking tokens are billed as output), `cached` (context-cache read), `cache_write`
(Claude prompt-cache write) and `tiers` for long-context (> N tokens), Flex, Priority and Batch prices.
Usage records carry the matching counters (`long_*`, `by_traffic`) so each token is priced at its SKU.

The Cloud Billing export is never read; the reported amount is an *estimate at list (or override) price* that
excludes credits, committed-use discounts and taxes - see README "비용 산정 방식".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..gcp import GcpError
from . import models_catalog as mc
from .base import to_float, to_int

LOG = logging.getLogger("usage_caps_api.pricing")

CATALOG_V1 = "https://cloudbilling.googleapis.com/v1"
CACHE_SECONDS = 6 * 3600
MAX_USD_PER_SINGLE_TOKEN = 0.0005  # $500 / 1M tokens sanity bound
DIRECTIONS = ("input", "output", "cached", "cache_write")
TIER_VARIANTS = ("flex", "priority", "batch")

# SKU words that mean "not a plain text-token SKU" (media, tuning, grounding, storage ...).
NON_TEXT_WORDS = {
    "offpeak", "peak", "live", "provisioned", "tuning", "tuned", "audio", "image", "images", "video", "cyber", "bidi",
    "embedding", "embeddings", "grounding", "search", "contributor", "deferred", "offline", "lyria", "music", "song",
    "clip", "clips", "track", "tracks", "veo", "imagen", "chirp", "storage", "hour", "hours",
}
LONG_CONTEXT_RE = re.compile(
    r"(?i)(?:>|\u2265|over|above)\s*\d|long context|long input|long output"
    r"|from\s+(?:[1-9]\d{4,})\s+to\s+\d"
)
THRESHOLD_RE = re.compile(r"(?i)(?:>|\u2265|>=|over|above|<=|\u2264|up to)\s*(\d+(?:\.\d+)?)\s*([km])")
RANGE_START_RE = re.compile(r"(?i)from\s+([1-9]\d{4,})\s+to\s+\d")
WORD_RE = re.compile(r"[a-z0-9.]+")
CREDIT_MODEL_PREFIXES = ("gemini",)
_MODEL_PATH_RE = re.compile(r"^(?:publishers/[^/]+/models/|models/)")
TRAFFIC_TO_TIER = {"flex": "flex", "priority": "priority", "batch": "batch"}


def credit_eligible(model: str, prefixes=CREDIT_MODEL_PREFIXES) -> bool:
    key = _MODEL_PATH_RE.sub("", str(model or "").strip().lower())
    return bool(key) and any(key.startswith(str(pref).lower()) for pref in prefixes if pref)


def parse_threshold(description: str) -> int | None:
    m = THRESHOLD_RE.search(description or "")
    if m:
        mult = 1000 if m.group(2).lower() == "k" else 1_000_000
        return int(float(m.group(1)) * mult)
    rm = RANGE_START_RE.search(description or "")
    if rm:
        val = int(rm.group(1))
        return val - 1 if val % 1000 == 1 else val
    return None


def classify_sku(description, service=None, unit=None, pricing_unit=None):
    """Maps one official SKU to (model_key, direction, product, tier, threshold) or None.

    direction: input | output | cached | cache_write; tier: None (standard) | long_context | flex | priority | batch
    | <variant>_long_context."""
    if not description or not isinstance(description, str):
        return None
    low = description.lower()
    words = set(WORD_RE.findall(low))
    if words & NON_TEXT_WORDS:
        return None
    info = mc.parse_sku(description, service)
    if not info or mc.is_non_token_model(info["key"]):
        return None
    if "cach" in low:
        tokenish = "token" in low or "token" in (pricing_unit or "").lower() or (unit or "").lower() == "count"
        if not tokenish:
            return None
        direction = "cache_write" if ("write" in words or "creation" in words or "writes" in words) else "cached"
    else:
        measure = mc.classify_measure(description, unit, pricing_unit)
        if measure == "input_tokens":
            direction = "input"
        elif measure == "output_tokens":
            direction = "output"
        else:
            return None
    variant = next((v for v in TIER_VARIANTS if v in words), None)
    is_long = bool(LONG_CONTEXT_RE.search(low))
    tier = ("%s_long_context" % variant if variant else "long_context") if is_long else variant
    return info["key"], direction, info["product"], tier, (parse_threshold(description) if is_long else None)


# Backwards compatible helpers (standard-tier input/output/cached only).
def token_sku(description, service=None, unit=None, pricing_unit=None):
    hit = classify_sku(description, service, unit, pricing_unit)
    if not hit or hit[3] is not None or hit[1] not in ("input", "output"):
        return None
    return hit[0], hit[1], hit[2]


def cached_token_sku(description, service=None, unit=None, pricing_unit=None):
    hit = classify_sku(description, service, unit, pricing_unit)
    if not hit or hit[3] is not None or hit[1] != "cached":
        return None
    return hit[0], "cached", hit[2]


def money(unit: dict | None) -> float:
    return int((unit or {}).get("units") or 0) + ((unit or {}).get("nanos") or 0) / 1e9


def first_positive_money(tiers) -> float:
    for tier in tiers or []:
        val = money((tier or {}).get("unitPrice"))
        if val > 0:
            return val
    return 0.0


@dataclass
class PriceTable:
    currency: str
    prices: dict = field(default_factory=dict)  # key -> {"input","output","cached","cache_write","tiers","source"}
    fx: dict | None = None
    counts: dict = field(default_factory=dict)
    aliases: dict = field(default_factory=dict)
    synced_at: str | None = None

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
    provisioned: str = "on_demand"   # on_demand (price PT tokens at on-demand SKU) | zero (prepaid commitment)

    @property
    def configured(self) -> bool:
        if any(pin > 0 or pout > 0 for _, pin, pout in self.models):
            return True
        if self.source == "auto" and self.table is not None and self.table.prices:
            return True
        return self.input_per_million > 0 or self.output_per_million > 0

    def _override(self, model: str | None):
        key = str(model or "").lower()
        for prefix, pin, pout in self.models:
            if key and (key == prefix or key.startswith(prefix)):
                return pin, pout
        return None

    def entry(self, model: str | None) -> dict | None:
        if self.source == "auto" and self.table is not None and model:
            return self.table.lookup(model)
        return None

    def price(self, model: str | None) -> tuple[float, float, bool]:
        ov = self._override(model)
        if ov is not None:
            return ov[0], ov[1], True
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
        if self._override(model) is None:
            hit = self.entry(model) or {}
            if hit.get("cached") is not None:
                return float(hit["cached"]), True
        return input_price, False

    def rates(self, model: str | None, tier: str | None = None) -> dict:
        """{input, output, cached, cache_write, priced, tier_used} for one model and optional SKU tier."""
        pin, pout, priced = self.price(model)
        pcached, _ = self.cached_price(model, pin)
        hit = self.entry(model) if self._override(model) is None else None
        pcw = float(hit["cache_write"]) if (hit and hit.get("cache_write") is not None) else pin
        out = {"input": pin, "output": pout, "cached": pcached, "cache_write": pcw, "priced": priced, "tier_used": None}
        if tier and hit:
            t = (hit.get("tiers") or {}).get(tier)
            if t:
                out["input"] = float(t.get("input", out["input"]) or out["input"])
                out["output"] = float(t.get("output", out["output"]) or out["output"])
                out["cached"] = float(t.get("cached", out["cached"]) or out["cached"])
                out["cache_write"] = float(t.get("cache_write", out["cache_write"]) or out["cache_write"])
                out["tier_used"] = tier
        return out


def _bucket_cost(c: dict, r: dict) -> float:
    inp = max(0, to_int(c.get("input_tokens")))
    cached = min(max(0, to_int(c.get("cached_tokens"))), inp)
    cw = min(max(0, to_int(c.get("cache_write_tokens"))), inp - cached)
    out = max(0, to_int(c.get("output_tokens")))
    return ((inp - cached - cw) * r["input"] + cached * r["cached"] + cw * r["cache_write"] + out * r["output"]) / 1e6


def model_cost(model: str | None, agg: dict, pricing: Pricing) -> tuple[float, bool, int]:
    """(cost, priced, tokens) for one model aggregate, honouring traffic-type tiers and long-context tiers."""
    tokens = to_int(agg.get("total_tokens") if "total_tokens" in agg else agg.get("tokens"))
    if "input_tokens" not in agg and ("promptTokens" in agg or "candidateTokens" in agg):
        agg = {"input_tokens": agg.get("promptTokens"), "output_tokens": agg.get("candidateTokens"),
               "cached_tokens": agg.get("cachedTokens"), "total_tokens": tokens}
    base = pricing.rates(model)
    if "output_tokens" in agg:
        agg = dict(agg)
        agg["output_tokens"] = max(to_int(agg.get("output_tokens")), tokens - to_int(agg.get("input_tokens")), 0)
    bucket_keys = ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens")
    traffic = agg.get("by_traffic") or {}
    if traffic:
        buckets = {t: {k: max(0, to_int(v.get(k))) for k in bucket_keys} for t, v in traffic.items()}
        # Rows from surfaces without trafficType (e.g. Antigravity mixed with Gemini Platform on the same model)
        # contribute to top-level `agg` totals but not to `by_traffic`. Price the unbucketed remainder at on_demand
        # so multi-surface usage never wipes out earlier surface cost.
        rem = {k: max(0, to_int(agg.get(k)) - sum(b[k] for b in buckets.values())) for k in bucket_keys}
        if any(rem.values()):
            od = buckets.setdefault("on_demand", {k: 0 for k in bucket_keys})
            for k, v in rem.items():
                od[k] += v
    else:
        buckets = {"on_demand": {k: max(0, to_int(agg.get(k))) for k in bucket_keys}}
    total = 0.0
    long_c = {k: to_int(agg.get("long_" + k)) for k in ("input_tokens", "output_tokens", "cached_tokens")}
    if any(long_c.values()):
        std = buckets.get("on_demand") or next(iter(buckets.values()))
        for k, v in long_c.items():
            take = min(v, to_int(std.get(k)))
            std[k] = to_int(std.get(k)) - take
            long_c[k] = take
        lr = pricing.rates(model, "long_context")
        total += _bucket_cost(long_c, lr)
    for t, c in buckets.items():
        if t == "provisioned" and pricing.provisioned == "zero":
            continue
        tier = TRAFFIC_TO_TIER.get(t)
        r = pricing.rates(model, tier) if tier else base
        total += _bucket_cost(c, r)
    return total, base["priced"], tokens


COST_COMPONENTS = (("input", "input"), ("cached_input", "cached"), ("cache_write", "cache_write"), ("output", "output"))


def _sku_ref(pricing: Pricing, model: str | None, tier: str | None, direction: str) -> tuple[str | None, str | None]:
    """(sku_id, sku_name) of the official Catalog SKU behind one price, or (None, None) for overrides / fallbacks."""
    if pricing._override(model) is not None:  # pylint: disable=protected-access
        return None, None
    hit = pricing.entry(model) or {}
    skus = hit.get("skus") or {}
    name = skus.get("%s.%s" % (tier, direction)) if tier else skus.get(direction)
    if not name:
        return None, None
    return str(name).rsplit("/", 1)[-1], str(name)


def cost_lines(model: str | None, agg: dict, pricing: Pricing | None) -> list[dict]:
    """Per-SKU cost lines for one model aggregate. Uses exactly the arithmetic of `model_cost`, so
    `sum(line.cost) == model_cost(...)[0]` (list / override price x log token counts; no billing data)."""
    if pricing is None or not pricing.configured or (model and mc.is_non_token_model(model)):
        return []
    tokens = to_int(agg.get("total_tokens") if "total_tokens" in agg else agg.get("tokens"))
    base = pricing.rates(model)
    agg = dict(agg)
    agg["output_tokens"] = max(to_int(agg.get("output_tokens")), tokens - to_int(agg.get("input_tokens")), 0)
    bucket_keys = ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens")
    traffic = agg.get("by_traffic") or {}
    if traffic:
        buckets = {t: {k: max(0, to_int(v.get(k))) for k in bucket_keys} for t, v in traffic.items()}
        rem = {k: max(0, to_int(agg.get(k)) - sum(b[k] for b in buckets.values())) for k in bucket_keys}
        if any(rem.values()):
            od = buckets.setdefault("on_demand", {k: 0 for k in bucket_keys})
            for k, v in rem.items():
                od[k] += v
    else:
        buckets = {"on_demand": {k: max(0, to_int(agg.get(k))) for k in bucket_keys}}
    plan: list[tuple[str, str | None, dict, dict]] = []
    long_c = {k: to_int(agg.get("long_" + k)) for k in ("input_tokens", "output_tokens", "cached_tokens")}
    if any(long_c.values()):
        std = buckets.get("on_demand") or next(iter(buckets.values()))
        for k, v in long_c.items():
            take = min(v, to_int(std.get(k)))
            std[k] = to_int(std.get(k)) - take
            long_c[k] = take
        plan.append(("long_context", "long_context", long_c, pricing.rates(model, "long_context")))
    for t, c in buckets.items():
        if t == "provisioned" and pricing.provisioned == "zero":
            continue
        tier = TRAFFIC_TO_TIER.get(t)
        plan.append((t, tier, c, pricing.rates(model, tier) if tier else base))
    lines = []
    for tier_name, tier_key, c, r in plan:
        inp = max(0, to_int(c.get("input_tokens")))
        cached = min(max(0, to_int(c.get("cached_tokens"))), inp)
        cw = min(max(0, to_int(c.get("cache_write_tokens"))), inp - cached)
        qty = {"input": inp - cached - cw, "cached_input": cached, "cache_write": cw,
               "output": max(0, to_int(c.get("output_tokens")))}
        for component, rate_key in COST_COMPONENTS:
            n = qty[component]
            if n <= 0:
                continue
            unit = float(r.get(rate_key) or 0.0)
            used_tier = tier_key if (tier_key and r.get("tier_used") == tier_key) else None
            sku_id, sku_name = _sku_ref(pricing, model, used_tier, rate_key)
            lines.append({"tier": tier_name, "component": component, "sku_id": sku_id, "sku": sku_name,
                          "quantity": {"value": n, "unit": "tokens"}, "unit_price_per_1m": round(unit, 6),
                          "cost": round(n * unit / 1e6, 9), "currency": pricing.currency})
    return lines


def estimate_cost(rec: dict, pricing: Pricing | None) -> tuple[float | None, bool]:
    """Returns (estimated_cost, partial) for a usage record with per-model token counts.

    - Uncached input tokens are priced at the input SKU; context-cache reads at the cached SKU; Claude cache writes at
      the cache-write SKU (falling back to input).
    - Output & thinking tokens (`max(total - prompt, candidates + thoughts)`) are priced at the output SKU.
    - Flex / Priority / Batch traffic uses the matching SKU tier; prompts above the long-context threshold use the
      long-context SKU tier; Provisioned Throughput traffic follows `pricing.provisioned_throughput_cost`.
    """
    if pricing is None or not pricing.configured:
        return None, False
    parts = list((rec.get("by_model") or {}).items()) or [(None, rec)]
    total = 0.0
    partial = False
    priced_tokens = 0
    used_tokens = 0
    for model, agg in parts:
        if model and mc.is_non_token_model(model):
            continue
        cost, priced, tokens = model_cost(model, agg, pricing)
        used_tokens += tokens
        if cost > 0 or priced:
            priced_tokens += tokens
        if not priced and tokens > 0:
            partial = True
        total += cost
    if used_tokens > 0 and priced_tokens == 0:
        return None, True
    return round(total, 6), partial


def cost_basis(model: str | None, agg: dict, pricing: Pricing | None) -> str:
    if (agg or {}).get("surface") == "gemini_enterprise":
        return "license"
    if model and mc.is_non_token_model(model):
        return "not_token_priced"
    if pricing is None:
        return "unpriced"
    if pricing._override(model) is not None:  # pylint: disable=protected-access
        return "override"
    if pricing.entry(model):
        return "official_sku"
    if pricing.source != "auto" and (pricing.input_per_million or pricing.output_per_million):
        return "default_rate"
    return "unpriced" if to_int((agg or {}).get("total_tokens")) > 0 else "no_tokens"


def source_summary(pricing: Pricing | None) -> dict:
    table = pricing.table if pricing else None
    return {
        "method": "log_token_counts_x_official_sku_prices",
        "billing_export": "not_used",
        "source": pricing.source if pricing else None,
        "catalog_synced_at": table.synced_at if table else None,
        "official_sku_models": (table.counts or {}).get("catalog", 0) if table else 0,
        "override_models": len(pricing.models) if pricing else 0,
        "provisioned_throughput": pricing.provisioned if pricing else None,
        "currency": pricing.currency if pricing else None,
    }


def split_cost(rec: dict, pricing: Pricing | None, prefixes=CREDIT_MODEL_PREFIXES):
    """Splits cost into (credit_eligible_cost, paid_other_cost, known, partial)."""
    eligible, other = {}, {}
    for model, agg in (rec.get("by_model") or {}).items():
        (eligible if credit_eligible(model, prefixes) else other)[model] = agg
    costs, any_known, any_part, partial = [], False, False, False
    for part in (eligible, other):
        if not part:
            costs.append(0.0)
            continue
        any_part = True
        cost, p_partial = estimate_cost({"by_model": part}, pricing)
        partial = partial or p_partial
        if cost is None:
            cost = 0.0
        else:
            any_known = True
        costs.append(cost)
    known = any_known if any_part else True
    return costs[0], costs[1], known, partial


def _scale(entry: dict, rate: float) -> dict:
    out = {d: float(entry[d]) * rate for d in DIRECTIONS if to_float(entry.get(d)) is not None}
    tiers = {}
    for t, te in (entry.get("tiers") or {}).items():
        if isinstance(te, dict):
            st = {d: float(te[d]) * rate for d in DIRECTIONS if to_float(te.get(d)) is not None}
            if te.get("threshold_tokens"):
                st["threshold_tokens"] = int(te["threshold_tokens"])
            if st:
                tiers[t] = st
    if tiers:
        out["tiers"] = tiers
    if entry.get("skus"):
        out["skus"] = entry["skus"]
    return out


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
        if isinstance(item, dict):
            entry = _scale(item, rate)
            if entry:
                out[key] = entry
    return out, rate, as_of, doc.get("synced_at")


def build_price_table(ctx, currency: str | None = None) -> PriceTable:
    from . import pricing_sync
    p_cfg = ctx.config.get("pricing") or {}
    cap_currency = (currency or p_cfg.get("currency") or "USD").upper()
    catalog, usd_rate, effective, synced_at = {}, None, None, None
    synced = synced_prices(ctx, cap_currency)
    if synced is not None:
        catalog, usd_rate, effective, synced_at = synced
        ctx.add_source("pricing:cloud_billing_catalog(synced)")
    elif getattr(ctx.app, "gcp", None) is not None and type(ctx.app.gcp).__name__ == "Gcp":
        try:
            catalog, usd_rate, effective = pricing_sync.live_catalog_prices(ctx, cap_currency)
            ctx.add_source("api:cloudbilling.services.skus")
        except GcpError as err:
            ctx.warn_error("api:cloudbilling.services.skus", err, "Cloud Billing Catalog prices")
    prices = {k: dict(v, source="official_sku") for k, v in (catalog or {}).items()}
    fx = ({"from": "USD", "to": cap_currency, "rate": round(usd_rate, 6), "as_of": effective}
          if (usd_rate and cap_currency != "USD") else None)
    table = PriceTable(currency=cap_currency, prices=prices, fx=fx, synced_at=synced_at,
                       counts={"catalog": len(prices)})
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
            entry = {d: float(item[d]) * ovr_factor for d in DIRECTIONS if to_float(item.get(d)) is not None}
            if mkey and entry:
                table.prices[mkey] = dict(entry, source="override")
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
    prov = str(p_cfg.get("provisioned_throughput_cost") or "on_demand").lower()
    return Pricing(
        currency=currency,
        input_per_million=float(p_cfg.get("input_price_per_million") or 0),
        output_per_million=float(p_cfg.get("output_price_per_million") or 0),
        models=tuple(models),
        cached_models=tuple(cached_models),
        source=source,
        table=table,
        provisioned=prov if prov in ("on_demand", "zero") else "on_demand",
    )
