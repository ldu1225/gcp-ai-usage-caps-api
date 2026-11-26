"""Model catalog: OpenAI / Anthropic style `GET /v1/models` and `GET /v1/models/{model_id}`.

Sources (merged, later wins on conflicts):
  1. A small built-in seed catalog so the API is useful offline and before the first sync.
  2. Live Vertex AI Model Garden publisher discovery (v1beta1 `publishers/{publisher}/models`) for every configured
     publisher (google, anthropic, meta, mistralai, ...), cached in the `model_catalog` document.
  3. Lifecycle overrides from configuration (`models.lifecycle_overrides`).
  4. Pricing (services/pricing.py) and, on request, observed per-model usage.

Model IDs are the bare Model Garden IDs (`gemini-2.5-pro`, `claude-sonnet-4-5`). The fully qualified resource form
`publishers/{publisher}/models/{model}` is what organization policy `constraints/vertexai.allowedModels` uses, with an
action suffix (`:predict`, `:deploy`, `:tune`).
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import logging
import re

from ..gcp import GcpError
from ..httpkit import ApiError, now_iso, paginate_list
from . import models_catalog as mc
from . import pricing as pricing_svc

LOG = logging.getLogger("usage_caps_api.model_registry")

CATALOG_DOC = "model_catalog"
PUBLISHER_MODELS_URL = "https://aiplatform.googleapis.com/v1beta1/publishers/%s/models"
MAX_PUBLISHER_PAGES = 5
ACTIONS = ("predict", "deploy", "tune")
PUBLISHER_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}$")
MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._@\-]{0,127}$")
PATTERN_RE = re.compile(r"^[a-z0-9*?][a-z0-9._@\-*?]{0,127}$")
RESOURCE_RE = re.compile(r"^publishers/([a-z0-9][a-z0-9\-]{0,62})/models/([a-z0-9*?][a-z0-9._@\-*?]{0,127})(?::([a-z]+))?$")
LAUNCH_STAGES = {
    "GA": "ga", "PUBLIC_PREVIEW": "preview", "PRIVATE_PREVIEW": "private_preview",
    "EXPERIMENTAL": "experimental", "DEPRECATED": "deprecated", "LAUNCH_STAGE_UNSPECIFIED": None,
}

# Built-in seed catalog (id, publisher, display name, family, surfaces, hidden helper?)
_CHAT = ("vertex_ai", "gemini_enterprise", "antigravity")
SEED_MODELS = (
    ("gemini-2.5-pro", "google", "Gemini 2.5 Pro", "gemini", _CHAT, False),
    ("gemini-2.5-flash", "google", "Gemini 2.5 Flash", "gemini", _CHAT, False),
    ("gemini-2.5-flash-lite", "google", "Gemini 2.5 Flash-Lite", "gemini", _CHAT, False),
    ("gemini-2.0-flash-001", "google", "Gemini 2.0 Flash", "gemini", ("vertex_ai",), False),
    ("gemini-2.0-flash-lite-001", "google", "Gemini 2.0 Flash-Lite", "gemini", ("vertex_ai",), False),
    ("gemini-2.5-flash-image", "google", "Gemini 2.5 Flash Image", "gemini", ("vertex_ai", "gemini_enterprise"), False),
    ("text-embedding-005", "google", "Text Embedding 005", "embedding", ("vertex_ai",), False),
    ("imagen-4.0-generate-001", "google", "Imagen 4", "imagen", ("vertex_ai",), False),
    ("veo-3.0-generate-001", "google", "Veo 3", "veo", ("vertex_ai",), False),
    ("claude-opus-4-1", "anthropic", "Claude Opus 4.1", "claude", _CHAT, False),
    ("claude-sonnet-4-5", "anthropic", "Claude Sonnet 4.5", "claude", _CHAT, False),
    ("claude-haiku-4-5", "anthropic", "Claude Haiku 4.5", "claude", _CHAT, False),
    ("claude-sonnet-4", "anthropic", "Claude Sonnet 4", "claude", ("vertex_ai",), False),
    ("claude-opus-4", "anthropic", "Claude Opus 4", "claude", ("vertex_ai",), False),
    ("count-tokens", "anthropic", "Claude count-tokens helper", "helper", ("vertex_ai",), True),
    ("llama-4-maverick-17b-128e-instruct-maas", "meta", "Llama 4 Maverick (MaaS)", "llama", ("vertex_ai",), False),
    ("llama-3.3-70b-instruct-maas", "meta", "Llama 3.3 70B (MaaS)", "llama", ("vertex_ai",), False),
    ("mistral-small-2503", "mistralai", "Mistral Small 3.1", "mistral", ("vertex_ai",), False),
    ("codestral-2501", "mistralai", "Codestral 25.01", "mistral", ("vertex_ai",), False),
)

_PUBLISHER_GUESS = (
    (re.compile(r"^(gemini|gemma|imagen|veo|chirp|lyria|text-embedding|text-multilingual|multimodalembedding)"), "google"),
    (re.compile(r"^(claude|count-tokens)"), "anthropic"),
    (re.compile(r"^llama"), "meta"),
    (re.compile(r"^(mistral|codestral|ministral|mixtral)"), "mistralai"),
    (re.compile(r"^jamba"), "ai21"),
    (re.compile(r"^deepseek"), "deepseek-ai"),
    (re.compile(r"^qwen"), "qwen"),
    (re.compile(r"^gpt-oss"), "openai"),
)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def guess_publisher(model_id: str) -> str | None:
    mid = (model_id or "").lower()
    for rx, pub in _PUBLISHER_GUESS:
        if rx.match(mid):
            return pub
    return None


def parse_model_ref(value: str) -> tuple[str | None, str, str | None]:
    """Parses `gemini-2.5-pro`, `google/gemini-2.5-pro`, `publishers/google/models/gemini-2.5-pro:predict` or
    a full Vertex resource path into (publisher, model_id, action). Raises ApiError on malformed input."""
    raw = str(value or "").strip().lower()
    if not raw:
        raise ApiError("bad_request", "Model identifier must not be empty.")
    if "/publishers/" in raw:
        raw = "publishers/" + raw.split("/publishers/", 1)[1]
    m = RESOURCE_RE.match(raw)
    if m:
        pub, mid, action = m.group(1), m.group(2), m.group(3)
        if action and action not in ACTIONS:
            raise ApiError("bad_request", "Model action '%s' must be one of: %s." % (action, ", ".join(ACTIONS)))
        return pub, mid, action
    action = None
    if ":" in raw:
        raw, action = raw.rsplit(":", 1)
        if action not in ACTIONS:
            raise ApiError("bad_request", "Model action '%s' must be one of: %s." % (action, ", ".join(ACTIONS)))
    if raw.count("/") == 1:
        pub, mid = raw.split("/", 1)
        if not PUBLISHER_RE.match(pub) or not PATTERN_RE.match(mid):
            raise ApiError("bad_request", "Invalid model identifier '%s'." % value)
        return pub, mid, action
    if not PATTERN_RE.match(raw):
        raise ApiError("bad_request", "Invalid model identifier '%s'." % value)
    return guess_publisher(raw.replace("*", "").replace("?", "")), raw, action


def model_key(model_id: str) -> str:
    """Normalized comparison key (strips versions/dates, `@001`, preview suffixes)."""
    return mc.normalize_model_id(model_id or "") or (model_id or "").lower()


def matches(model_id: str, pattern: str) -> bool:
    """True when a concrete model id matches a pattern (fnmatch on raw and normalized forms)."""
    mid = (model_id or "").lower()
    pat = (pattern or "").lower()
    if "/" in pat:
        try:
            _, pat, _ = parse_model_ref(pat)
        except ApiError:
            return False
    if "/" in mid:
        mid = mid.rsplit("/", 1)[-1]
    mid = mid.split(":", 1)[0]
    if not pat:
        return False
    if pat == "*":
        return True
    if fnmatch.fnmatchcase(mid, pat):
        return True
    if "*" in pat or "?" in pat:
        return fnmatch.fnmatchcase(model_key(mid), pat)
    return model_key(mid) == model_key(pat)


# ---------------------------------------------------------------------------
# Catalog storage & discovery
# ---------------------------------------------------------------------------

def _seed_entries() -> dict[str, dict]:
    out = {}
    for mid, pub, name, family, surfaces, hidden in SEED_MODELS:
        out[mid] = {
            "id": mid, "publisher": pub, "display_name": name, "family": family,
            "surfaces": list(surfaces), "launch_stage": None, "version_id": None,
            "version_state": None, "hidden": hidden, "source": "seed", "supported_actions": ["predict"],
        }
    return out


def _family(mid: str) -> str:
    return (mid.split("-", 1)[0] if mid else "other") or "other"


def _surfaces_for(pub: str, mid: str) -> list[str]:
    if pub == "google" and re.match(r"^gemini-(2\.5|[3-9])", mid) and not re.search(r"embedding|tts|live|audio", mid):
        return list(_CHAT)
    if pub == "anthropic" and re.match(r"^claude-(opus|sonnet|haiku)-4", mid):
        return list(_CHAT)
    return ["vertex_ai"]


def parse_publisher_model(publisher: str, raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "")
    mid = name.rsplit("/", 1)[-1].strip().lower()
    if not mid or not MODEL_ID_RE.match(mid):
        return None
    stage_raw = str(raw.get("launchStage") or "").upper()
    actions = []
    sa = raw.get("supportedActions") if isinstance(raw.get("supportedActions"), dict) else {}
    if sa.get("deploy") or sa.get("multiDeployVertex") or sa.get("deployGke"):
        actions.append("deploy")
    if sa.get("openFineTuningPipeline") or sa.get("openFineTuningPipelines"):
        actions.append("tune")
    actions.insert(0, "predict")
    return {
        "id": mid,
        "publisher": publisher,
        "display_name": mc.display_from_key(mc.normalize_model_id(mid)) or mid,
        "family": _family(mid),
        "surfaces": _surfaces_for(publisher, mid),
        "launch_stage": LAUNCH_STAGES.get(stage_raw, stage_raw.lower() or None),
        "version_id": raw.get("versionId"),
        "version_state": (str(raw.get("versionState") or "").replace("VERSION_STATE_", "").lower() or None),
        "hidden": False,
        "source": "model_garden",
        "supported_actions": actions,
    }


def discover(ctx) -> dict:
    """Fetches the live Model Garden catalog for every configured publisher. Never raises; returns a summary."""
    publishers = [p for p in ((ctx.config.get("models") or {}).get("publishers") or []) if PUBLISHER_RE.match(str(p))]
    tasks = {}
    for pub in publishers:
        def fetch(p=pub):
            items, token = [], None
            for _ in range(MAX_PUBLISHER_PAGES):
                params = {"pageSize": 200, "listAllVersions": "false"}
                if token:
                    params["pageToken"] = token
                payload = ctx.rest("GET", PUBLISHER_MODELS_URL % p, params=params, timeout=20) or {}
                items.extend(payload.get("publisherModels") or [])
                token = payload.get("nextPageToken")
                if not token:
                    break
            return items
        tasks[pub] = fetch
    outcomes = ctx.run(tasks) if tasks else {}
    models: dict[str, dict] = {}
    errors = {}
    for pub, (ok, val) in outcomes.items():
        if not ok:
            errors[pub] = getattr(val, "kind", "error")
            continue
        for raw in val or []:
            entry = parse_publisher_model(pub, raw)
            if entry:
                models[entry["id"]] = entry
    fetched_at = now_iso()

    def save(doc):
        if models:
            doc["models"] = models
        doc["fetched_at"] = fetched_at
        doc["errors"] = errors
        return doc

    ctx.store.mutate(CATALOG_DOC, save)
    return {"discovered": len(models), "publishers": publishers, "errors": errors, "fetched_at": fetched_at}


def _stale(doc: dict, ttl: int) -> bool:
    from ..timeutil import parse_rfc3339
    at = parse_rfc3339(doc.get("fetched_at")) if doc.get("fetched_at") else None
    return at is None or (utcnow() - at).total_seconds() > ttl


def load_catalog(ctx, *, refresh: bool = False, allow_discovery: bool = True) -> dict[str, dict]:
    """Returns {model_id: entry} merging seed + discovered + config lifecycle overrides."""
    doc, _ = ctx.store.load(CATALOG_DOC)
    ttl = int((ctx.config.get("models") or {}).get("catalog_ttl_seconds") or 3600)
    if allow_discovery and (refresh or (not doc.get("models") and _stale(doc, ttl) and not doc.get("errors"))):
        try:
            discover(ctx)
            doc, _ = ctx.store.load(CATALOG_DOC)
        except Exception as err:  # pylint: disable=broad-except
            LOG.warning("model discovery failed: %s", type(err).__name__)
    out = _seed_entries()
    for mid, entry in (doc.get("models") or {}).items():
        if isinstance(entry, dict) and MODEL_ID_RE.match(mid):
            base = out.get(mid, {})
            merged = dict(base, **{k: v for k, v in entry.items() if v not in (None, [], "")})
            out[mid] = merged
    for ov in (ctx.config.get("models") or {}).get("lifecycle_overrides") or []:
        if isinstance(ov, dict) and ov.get("model") in out:
            for k in ("deprecation_date", "shutdown_date", "replacement", "launch_stage"):
                if ov.get(k):
                    out[ov["model"]][k] = ov[k]
    return out


def resolve(catalog: dict, value: str) -> tuple[str, str, str | None]:
    """Resolves a user supplied model reference to (publisher, model_id, action); publisher falls back to catalog/guess."""
    pub, mid, action = parse_model_ref(value)
    entry = catalog.get(mid)
    if entry is None:
        key = model_key(mid)
        entry = next((e for e in catalog.values() if model_key(e["id"]) == key), None)
        if entry is not None:
            mid = entry["id"]
    pub = pub or (entry or {}).get("publisher") or guess_publisher(mid)
    if not pub:
        raise ApiError("bad_request", "Cannot determine the publisher of model '%s'; use 'publisher/model'." % value)
    return pub, mid, action


def expand(catalog: dict, patterns: list[str]) -> list[tuple[str, str]]:
    """Expands model patterns (`gemini-2.5-*`, `anthropic/*`, concrete IDs) to concrete (publisher, model_id) pairs.

    Organization policy does not support wildcards, so patterns are expanded against the catalog at write time.
    Concrete IDs that are not in the catalog are kept as-is (customers may target models the catalog has not seen)."""
    out: list[tuple[str, str]] = []
    seen = set()
    for raw in patterns or []:
        pub, mid, _ = parse_model_ref(raw)
        if "*" in mid or "?" in mid:
            for entry in catalog.values():
                if entry.get("hidden"):
                    continue
                if pub and entry["publisher"] != pub:
                    continue
                if matches(entry["id"], mid):
                    key = (entry["publisher"], entry["id"])
                    if key not in seen:
                        seen.add(key)
                        out.append(key)
            continue
        try:
            rpub, rmid, _ = resolve(catalog, raw)
        except ApiError:
            raise
        key = (rpub, rmid)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


# ---------------------------------------------------------------------------
# Views & handlers
# ---------------------------------------------------------------------------

def model_view(entry: dict, pricing: pricing_svc.Pricing | None, *, access: dict | None = None,
               usage: dict | None = None) -> dict:
    mid = entry["id"]
    pin = pout = pcached = None
    priced = False
    if pricing is not None and pricing.configured and not mc.is_non_token_model(mid):
        a, b, priced = pricing.price(mid)
        if priced or a or b:
            pin, pout = a, b
            pcached, _ = pricing.cached_price(mid, a)
    stage = entry.get("launch_stage")
    if entry.get("shutdown_date") or entry.get("deprecation_date"):
        stage = stage or "deprecated"
    out = {
        "object": "model",
        "id": mid,
        "owned_by": entry.get("publisher"),
        "publisher": entry.get("publisher"),
        "display_name": entry.get("display_name") or mid,
        "family": entry.get("family"),
        "resource_name": "publishers/%s/models/%s" % (entry.get("publisher"), mid),
        "policy_values": ["publishers/%s/models/%s:%s" % (entry.get("publisher"), mid, a)
                          for a in (entry.get("supported_actions") or ["predict"])],
        "surfaces": list(entry.get("surfaces") or ["vertex_ai"]),
        "lifecycle": {
            "stage": stage,
            "version_id": entry.get("version_id"),
            "version_state": entry.get("version_state"),
            "deprecation_date": entry.get("deprecation_date"),
            "shutdown_date": entry.get("shutdown_date"),
            "replacement": entry.get("replacement"),
        },
        "pricing": ({
            "currency": pricing.currency,
            "input_per_1m_tokens": pin,
            "output_per_1m_tokens": pout,
            "cached_input_per_1m_tokens": pcached,
            "source": pricing.source,
        } if pin is not None or pout is not None else None),
        "source": entry.get("source"),
    }
    if access is not None:
        out["access"] = access
    if usage is not None:
        out["usage"] = usage
    return out


def _observed_usage(ctx, days: int = 30) -> dict[str, dict]:
    from . import usage as usage_svc
    end = utcnow()
    start = end - dt.timedelta(days=days)
    res = usage_svc.collect_usage(ctx, start, end)
    agg: dict[str, dict] = {}
    for _email, rec in res.users.items():
        for model, mrec in (rec.get("by_model") or {}).items():
            a = agg.setdefault(model, {"requests": 0, "total_tokens": 0, "users": 0, "window_days": days})
            a["requests"] += mrec.get("requests", 0)
            a["total_tokens"] += mrec.get("total_tokens", 0)
            a["users"] += 1
    return agg


def list_models(ctx) -> dict:
    """GET /v1/models?publisher=&surface=&stage=&q=&include=usage,access&project=&limit=&cursor="""
    from . import caps as caps_svc
    refresh = str(ctx.params.get("refresh") or "").lower() in ("1", "true")
    catalog = load_catalog(ctx, refresh=refresh)
    pricing = pricing_svc.resolve_pricing(ctx, caps_svc.effective_policy(ctx))
    publisher = (ctx.params.get("publisher") or "").strip().lower()
    surface = (ctx.params.get("surface") or "").strip().lower()
    stage = (ctx.params.get("stage") or "").strip().lower()
    q = (ctx.params.get("q") or "").strip().lower()
    include = {s.strip() for s in (ctx.params.get("include") or "").split(",") if s.strip()}
    show_hidden = str(ctx.params.get("include_helpers") or "").lower() in ("1", "true")
    usage = _observed_usage(ctx) if "usage" in include else {}
    access_eval = None
    if "access" in include:
        from . import model_access
        project = (ctx.params.get("project") or ctx.project).strip().lower()
        access_eval = model_access.access_evaluator(ctx, project, catalog)
    rows = []
    for mid in sorted(catalog):
        entry = catalog[mid]
        if entry.get("hidden") and not show_hidden:
            continue
        if publisher and entry.get("publisher") != publisher:
            continue
        if surface and surface not in (entry.get("surfaces") or []):
            continue
        if stage and (entry.get("launch_stage") or "") != stage:
            continue
        if q and q not in mid and q not in str(entry.get("display_name") or "").lower():
            continue
        u = None
        if usage:
            u = usage.get(mid) or next((v for k, v in usage.items() if model_key(k) == model_key(mid)), None)
        rows.append(model_view(entry, pricing, access=access_eval(entry) if access_eval else None, usage=u))
    page = paginate_list(rows, ctx.params, default_limit=100, max_limit=500)
    doc, _ = ctx.store.load(CATALOG_DOC)
    page["catalog"] = {"fetched_at": doc.get("fetched_at"), "errors": doc.get("errors") or {}}
    return page


def get_model(ctx) -> dict:
    """GET /v1/models/{model_id} (also accepts `publisher:model` via ?publisher=)."""
    from . import caps as caps_svc
    from . import model_access
    raw = ctx.path_params.get("model_id") or ""
    if ctx.params.get("publisher"):
        raw = "%s/%s" % (ctx.params["publisher"], raw)
    catalog = load_catalog(ctx)
    try:
        pub, mid, _ = resolve(catalog, raw)
    except ApiError:
        pub, mid = None, str(raw).lower()
    entry = catalog.get(mid)
    if entry is None:
        raise ApiError("not_found", "Model '%s' was not found in the catalog. Run POST /v1/models/sync to refresh." % mid)
    pricing = pricing_svc.resolve_pricing(ctx, caps_svc.effective_policy(ctx))
    projects = ctx.all_projects
    access = {}
    for project in projects[:50]:
        access[project] = model_access.access_evaluator(ctx, project, catalog)(entry)
    view = model_view(entry, pricing, access=None)
    view["access_by_project"] = access
    return view


def get_model_access(ctx) -> dict:
    """GET /v1/models/{model_id}/access: decision trace per managed project, GE app and per-user rules."""
    from . import model_access, user_model_rules
    catalog = load_catalog(ctx)
    pub, mid, _ = resolve(catalog, ctx.path_params.get("model_id") or "")
    entry = catalog.get(mid) or {"id": mid, "publisher": pub, "surfaces": ["vertex_ai"], "supported_actions": ["predict"]}
    projects = {p: model_access.access_evaluator(ctx, p, catalog)(entry) for p in ctx.all_projects[:50]}
    users = user_model_rules.users_affected_by_model(ctx, mid)
    return {"object": "model_access", "model": mid, "publisher": pub, "projects": projects, "user_rules": users}


def sync_models(ctx) -> dict:
    """POST /v1/models/sync: refreshes the Model Garden catalog now."""
    from . import audit
    summary = discover(ctx)
    audit.write_audit(ctx, "models.catalog.synced", target="catalog", details=summary)
    return dict({"object": "model_catalog_sync"}, **summary)


def usage_by_model(ctx) -> dict:
    """GET /v1/usage/models?start=&end=&group_by=user|project|surface&project=&surface="""
    from . import caps as caps_svc
    from . import usage as usage_svc
    from .. import config, timeutil
    from .base import iso  # noqa: F401
    rng = ctx.range or timeutil.parse_range(ctx.params, ctx.tz_name, ctx.ws.get("default_range_days", 14))
    surface = (ctx.params.get("surface") or "").strip().lower()
    if surface and surface not in ("all", *config.VALID_SERVICES):
        raise ApiError("bad_request", "Parameter 'surface' must be one of: all, %s." % ", ".join(config.VALID_SERVICES))
    group_by = (ctx.params.get("group_by") or "").strip().lower()
    if group_by and group_by not in ("user", "project", "surface"):
        raise ApiError("bad_request", "Parameter 'group_by' must be user, project or surface.")
    policy = caps_svc.effective_policy(ctx)
    pricing = pricing_svc.resolve_pricing(ctx, policy)
    res = usage_svc.collect_usage(ctx, rng.start_ts(), rng.end_ts(), surface=surface or None)
    models: dict[str, dict] = {}
    for email, rec in res.users.items():
        for proj, prec in (rec.get("by_project") or {}).items():
            for model, mrec in (prec.get("by_model") or {}).items():
                surf = ((rec.get("by_model") or {}).get(model) or {}).get("surface") or "antigravity"
                m = models.setdefault(model, {"model": model, "surface": surf, "requests": 0, "input_tokens": 0,
                                              "output_tokens": 0, "cached_tokens": 0, "total_tokens": 0,
                                              "users": set(), "groups": {}})
                for k in ("requests", "input_tokens", "output_tokens", "cached_tokens", "total_tokens"):
                    m[k] += mrec.get(k, 0)
                m["users"].add(email)
                if group_by:
                    gkey = {"user": email, "project": proj, "surface": surf}[group_by]
                    g = m["groups"].setdefault(gkey, {"key": gkey, "requests": 0, "input_tokens": 0, "output_tokens": 0,
                                                      "cached_tokens": 0, "total_tokens": 0})
                    for k in ("requests", "input_tokens", "output_tokens", "cached_tokens", "total_tokens"):
                        g[k] += mrec.get(k, 0)
    rows = []
    for model, m in models.items():
        cost, partial = pricing_svc.estimate_cost({"by_model": {model: m}}, pricing)
        row = {k: v for k, v in m.items() if k not in ("users", "groups")}
        row.update(object="model_usage", active_users=len(m["users"]), estimated_cost=cost, cost_partial=partial,
                   currency=pricing.currency if cost is not None else None)
        if group_by:
            groups = []
            for g in m["groups"].values():
                gc, _ = pricing_svc.estimate_cost({"by_model": {model: g}}, pricing)
                groups.append(dict(g, estimated_cost=gc))
            groups.sort(key=lambda r: (-(r["estimated_cost"] or 0), -r["total_tokens"], r["key"]))
            row["group_by"] = group_by
            row["groups"] = groups
        rows.append(row)
    rows.sort(key=lambda r: (-(r["estimated_cost"] if r["estimated_cost"] is not None else -1), -r["total_tokens"], r["model"]))
    page = paginate_list(rows, ctx.params, default_limit=100, max_limit=500)
    page["range"] = rng.meta()
    page["telemetry_ok"] = res.ok
    return page


def safe_discover_error(err: Exception) -> str:
    return getattr(err, "kind", type(err).__name__) if isinstance(err, (GcpError, ApiError)) else type(err).__name__
