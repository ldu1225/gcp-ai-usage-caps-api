"""Model catalog: OpenAI / Anthropic style `GET /v1/models` and `GET /v1/models/{model_id}` - fully dynamic.

There is NO built-in model list. The catalog is the union of:
  1. Live Vertex AI Model Garden discovery (v1beta1 `publishers/{publisher}/models`) for every configured publisher
     plus every publisher observed in logs. Models that disappear from Model Garden are kept with `delisted_at`.
  2. Every model observed in usage logs on any surface (Antigravity `labels.model_id`, Vertex AI inference request
     `resource.labels.model_user_id`, Data Access audit `resourceName`, Gemini Enterprise activity) - recorded with
     first_seen / last_seen, the surfaces / projects it was used on and the number of users. A model used for the
     first time triggers the `model.first_seen` webhook, so brand-new models are tracked the day they are used
     (the very first observation after installation is recorded as a silent baseline to avoid a webhook storm).
  3. Lifecycle overrides from configuration (`models.lifecycle_overrides`).
  4. Official SKU pricing (services/pricing.py) and, on request, live per-model usage.

Model IDs are the bare Model Garden IDs. The fully qualified resource form `publishers/{publisher}/models/{model}` is
what organization policy `constraints/vertexai.allowedModels` uses, with an action suffix (`:predict`, `:deploy`,
`:tune`). Publishers are never guessed from model-name patterns: they come from Model Garden or from the log resource.
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
MAX_PUBLISHER_PAGES = 10
MAX_OBSERVED = 5000
MAX_OBSERVED_PROJECTS = 100
OBSERVE_WRITE_INTERVAL = dt.timedelta(minutes=15)
ACTIONS = ("predict", "deploy", "tune")
PUBLISHER_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}$")
MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._@\-]{0,159}$")
PATTERN_RE = re.compile(r"^[a-z0-9*?][a-z0-9._@\-*?]{0,159}$")
RESOURCE_RE = re.compile(r"^publishers/([a-z0-9][a-z0-9\-]{0,62})/models/([a-z0-9*?][a-z0-9._@\-*?]{0,159})(?::([a-z]+))?$")
LAUNCH_STAGES = {
    "GA": "ga", "PUBLIC_PREVIEW": "preview", "PRIVATE_PREVIEW": "private_preview",
    "EXPERIMENTAL": "experimental", "DEPRECATED": "deprecated", "LAUNCH_STAGE_UNSPECIFIED": None,
}

# Publisher hints learned from the last loaded catalog (Model Garden + logs); never from name patterns.
_PUBLISHER_HINTS: dict[str, str] = {}


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def guess_publisher(model_id: str, catalog: dict | None = None) -> str | None:
    """Publisher of a model as reported by Model Garden or the log resource (None when never seen)."""
    mid = (model_id or "").strip().lower()
    if not mid:
        return None
    if catalog:
        entry = catalog.get(mid)
        if entry is None:
            key = model_key(mid)
            entry = next((e for e in catalog.values() if model_key(e["id"]) == key), None)
        if entry and entry.get("publisher"):
            return entry["publisher"]
    if mid in _PUBLISHER_HINTS:
        return _PUBLISHER_HINTS[mid]
    key = model_key(mid)
    return next((p for m, p in _PUBLISHER_HINTS.items() if model_key(m) == key), None)


def parse_model_ref(value: str) -> tuple[str | None, str, str | None]:
    """Parses `model`, `publisher/model`, `publishers/{p}/models/{m}:predict` or a full Vertex resource path into
    (publisher, model_id, action). Raises ApiError on malformed input."""
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
    return guess_publisher(raw.replace("*", "").replace("?", "")) if not ("*" in raw or "?" in raw) else None, raw, action


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
# Catalog storage, discovery & observation
# ---------------------------------------------------------------------------

def _family(mid: str) -> str:
    return (re.split(r"[-_.@]", mid, 1)[0] if mid else "other") or "other"


def parse_publisher_model(publisher: str, raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "")
    mid = name.rsplit("/", 1)[-1].strip().lower()
    if not mid or not MODEL_ID_RE.match(mid):
        return None
    m = re.match(r"^publishers/([^/]+)/models/", name)
    if m and PUBLISHER_RE.match(m.group(1).lower()):
        publisher = m.group(1).lower()
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
        "available_surfaces": ["vertex_ai"],
        "launch_stage": LAUNCH_STAGES.get(stage_raw, stage_raw.lower() or None),
        "version_id": raw.get("versionId"),
        "version_state": (str(raw.get("versionState") or "").replace("VERSION_STATE_", "").lower() or None),
        "open_source_category": raw.get("openSourceCategory"),
        "source": "model_garden",
        "supported_actions": actions,
    }


def publishers_to_scan(ctx, doc: dict | None = None) -> list[str]:
    """Configured publishers + every publisher seen in logs / previous discovery (dynamic, de-duplicated)."""
    cfg = [str(p).strip().lower() for p in ((ctx.config.get("models") or {}).get("publishers") or [])]
    if doc is None:
        doc, _ = ctx.store.load(CATALOG_DOC)
    seen = [str((o or {}).get("publisher") or "") for o in (doc.get("observed") or {}).values()]
    seen += [str((o or {}).get("publisher") or "") for o in (doc.get("models") or {}).values()]
    out = []
    for p in cfg + sorted(set(seen)):
        if p and PUBLISHER_RE.match(p) and p not in out:
            out.append(p)
    return out


def discover(ctx) -> dict:
    """Fetches the live Model Garden catalog for every known publisher. Never raises; returns a summary."""
    doc0, _ = ctx.store.load(CATALOG_DOC)
    publishers = publishers_to_scan(ctx, doc0)
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
    ok_publishers = set()
    for pub, (ok, val) in outcomes.items():
        if not ok:
            errors[pub] = getattr(val, "kind", "error")
            continue
        ok_publishers.add(pub)
        for raw in val or []:
            entry = parse_publisher_model(pub, raw)
            if entry:
                models[entry["id"]] = entry
    fetched_at = now_iso()
    changes = {"added": [], "delisted": []}

    def save(doc):
        prev = doc.get("models") or {}
        merged = {}
        for mid, entry in models.items():
            old = prev.get(mid) or {}
            merged[mid] = dict(entry, first_listed=old.get("first_listed") or fetched_at)
            if not old or old.get("delisted_at"):
                changes["added"].append(mid)
        for mid, old in prev.items():
            if mid in merged:
                continue
            if old.get("publisher") in ok_publishers and not old.get("delisted_at"):
                changes["delisted"].append(mid)
                merged[mid] = dict(old, delisted_at=fetched_at)
            else:
                merged[mid] = old
        if models or not prev:
            doc["models"] = merged
        doc["fetched_at"] = fetched_at
        doc["errors"] = errors
        doc["publishers"] = publishers
        return doc

    ctx.store.mutate(CATALOG_DOC, save)
    if doc0.get("models") and (changes["added"] or changes["delisted"]):
        from . import webhooks
        webhooks.dispatch_event(ctx, "model.catalog_changed", {
            "added": sorted(changes["added"])[:200], "delisted": sorted(changes["delisted"])[:200],
            "fetched_at": fetched_at})
    return {"discovered": len(models), "publishers": publishers, "errors": errors, "fetched_at": fetched_at,
            "added": sorted(changes["added"]), "delisted": sorted(changes["delisted"])}


def _iso(v):
    return v.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(v, dt.datetime) else v


def record_observed(ctx, usage) -> list[str]:
    """Persists every model seen in usage logs; fires `model.first_seen` for models never seen before."""
    from ..timeutil import parse_rfc3339
    seen = getattr(usage, "models_seen", None) or {}
    if not seen:
        return []
    doc, _ = ctx.store.load(CATALOG_DOC)
    baseline = doc.get("observed") is None   # first observation after install: record silently (no webhook storm)
    observed = doc.get("observed") or {}
    need = baseline
    for model, m in seen.items():
        cur = observed.get(model)
        if cur is None:
            need = True
            break
        last = parse_rfc3339(cur.get("last_seen")) if cur.get("last_seen") else None
        if isinstance(m.get("last_seen"), dt.datetime) and (last is None or m["last_seen"] - last > OBSERVE_WRITE_INTERVAL):
            need = True
            break
        if not set(m.get("surfaces") or ()) <= set(cur.get("surfaces") or ()):
            need = True
            break
        if not set(m.get("projects") or ()) <= set(cur.get("projects") or ()) and \
                len(cur.get("projects") or ()) < MAX_OBSERVED_PROJECTS:
            need = True
            break
    if not need:
        return []
    new_models: list[dict] = []
    now = now_iso()

    def save(d):
        obs = dict(d.get("observed") or {})
        garden = d.get("models") or {}
        for model, m in seen.items():
            cur = dict(obs.get(model) or {})
            first = cur.get("first_seen")
            if not cur:
                new_models.append({"model": model, "publisher": m.get("publisher"),
                                   "surfaces": sorted(m.get("surfaces") or ()),
                                   "projects": sorted(m.get("projects") or ())[:20],
                                   "users": len(m.get("users") or ()), "first_seen": _iso(m.get("first_seen")) or now,
                                   "in_model_garden": model in garden})
            cur["publisher"] = cur.get("publisher") or m.get("publisher") or (garden.get(model) or {}).get("publisher")
            cur["surfaces"] = sorted(set(cur.get("surfaces") or ()) | set(m.get("surfaces") or ()))
            cur["projects"] = sorted(set(cur.get("projects") or ()) | set(m.get("projects") or ()))[:MAX_OBSERVED_PROJECTS]
            cur["users_seen"] = max(int(cur.get("users_seen") or 0), len(m.get("users") or ()))
            cand_first = _iso(m.get("first_seen"))
            cur["first_seen"] = min(x for x in (first, cand_first, now) if x)
            cand_last = _iso(m.get("last_seen")) or now
            cur["last_seen"] = max(x for x in (cur.get("last_seen"), cand_last) if x)
            cur["recorded_at"] = cur.get("recorded_at") or now
            obs[model] = cur
        if len(obs) > MAX_OBSERVED:
            for k in sorted(obs, key=lambda k: obs[k].get("last_seen") or "")[:len(obs) - MAX_OBSERVED]:
                del obs[k]
        d["observed"] = obs
        return d

    ctx.store.mutate(CATALOG_DOC, save)
    if new_models and baseline:
        from . import audit
        audit.write_audit(ctx, "model.observed_baseline", target="catalog",
                          details={"models": sorted(nm["model"] for nm in new_models)[:500]})
        return []
    if new_models:
        from . import audit, webhooks
        for nm in new_models[:50]:
            audit.write_audit(ctx, "model.first_seen", target=nm["model"], details=nm)
            webhooks.dispatch_event(ctx, "model.first_seen", nm)
    return [nm["model"] for nm in new_models]


def _stale(doc: dict, ttl: int) -> bool:
    from ..timeutil import parse_rfc3339
    at = parse_rfc3339(doc.get("fetched_at")) if doc.get("fetched_at") else None
    return at is None or (utcnow() - at).total_seconds() > ttl


def _helper_ids(ctx) -> set[str]:
    out = set()
    for v in (ctx.config.get("models") or {}).get("always_allow") or []:
        try:
            _, mid, _ = parse_model_ref(v)
            out.add(mid)
        except ApiError:
            continue
    return out


def load_catalog(ctx, *, refresh: bool = False, allow_discovery: bool = True) -> dict[str, dict]:
    """Returns {model_id: entry} = Model Garden (live) U models observed in logs U config lifecycle overrides."""
    doc, _ = ctx.store.load(CATALOG_DOC)
    ttl = int((ctx.config.get("models") or {}).get("catalog_ttl_seconds") or 3600)
    if allow_discovery and (refresh or (_stale(doc, ttl) and not (doc.get("errors") and doc.get("models")))):
        try:
            discover(ctx)
            doc, _ = ctx.store.load(CATALOG_DOC)
        except Exception as err:  # pylint: disable=broad-except
            LOG.warning("model discovery failed: %s", type(err).__name__)
    helpers = _helper_ids(ctx)
    out: dict[str, dict] = {}
    for mid, entry in (doc.get("models") or {}).items():
        if isinstance(entry, dict) and MODEL_ID_RE.match(mid):
            out[mid] = dict(entry, observed=None, hidden=mid in helpers)
    for mid, obs in (doc.get("observed") or {}).items():
        if not (isinstance(obs, dict) and MODEL_ID_RE.match(mid)):
            continue
        view = {"surfaces": obs.get("surfaces") or [], "projects": len(obs.get("projects") or []),
                "users_seen": obs.get("users_seen"), "first_seen": obs.get("first_seen"),
                "last_seen": obs.get("last_seen")}
        if mid in out:
            out[mid]["observed"] = view
            if not out[mid].get("publisher") and obs.get("publisher"):
                out[mid]["publisher"] = obs["publisher"]
        else:
            out[mid] = {
                "id": mid, "publisher": obs.get("publisher"),
                "display_name": mc.display_from_key(mc.normalize_model_id(mid)) or mid, "family": _family(mid),
                "available_surfaces": [], "launch_stage": None, "version_id": None, "version_state": None,
                "source": "observed", "supported_actions": ["predict"], "observed": view, "hidden": mid in helpers,
            }
    for ov in (ctx.config.get("models") or {}).get("lifecycle_overrides") or []:
        if isinstance(ov, dict) and ov.get("model") in out:
            for k in ("deprecation_date", "shutdown_date", "replacement", "launch_stage"):
                if ov.get(k):
                    out[ov["model"]][k] = ov[k]
    for mid, entry in out.items():
        surfaces = set(entry.get("available_surfaces") or ()) | set((entry.get("observed") or {}).get("surfaces") or ())
        entry["surfaces"] = sorted(surfaces)
        if entry.get("publisher"):
            _PUBLISHER_HINTS[mid] = entry["publisher"]
    return out


def resolve(catalog: dict, value: str) -> tuple[str, str, str | None]:
    """Resolves a user supplied model reference to (publisher, model_id, action) using the live catalog."""
    pub, mid, action = parse_model_ref(value)
    entry = catalog.get(mid)
    if entry is None:
        key = model_key(mid)
        entry = next((e for e in catalog.values() if model_key(e["id"]) == key), None)
        if entry is not None:
            mid = entry["id"]
    pub = pub or (entry or {}).get("publisher") or guess_publisher(mid, catalog)
    if not pub:
        raise ApiError("bad_request", "Cannot determine the publisher of model '%s' (not in Model Garden and never "
                                      "observed); use 'publisher/model'." % value)
    return pub, mid, action


def expand(catalog: dict, patterns: list[str]) -> list[tuple[str, str]]:
    """Expands model patterns (`<family>-*`, `anthropic/*`, concrete IDs) to concrete (publisher, model_id) pairs.

    Organization policy does not support wildcards, so patterns are expanded against the live catalog at write time
    (and re-expanded hourly so newly released models matching the pattern are picked up automatically).
    Concrete IDs that are not in the catalog are kept as-is when the publisher is given."""
    out: list[tuple[str, str]] = []
    seen = set()
    for raw in patterns or []:
        pub, mid, _ = parse_model_ref(raw)
        if "*" in mid or "?" in mid:
            for entry in catalog.values():
                if entry.get("hidden") or entry.get("delisted_at") or not entry.get("publisher"):
                    continue
                if pub and entry["publisher"] != pub:
                    continue
                if matches(entry["id"], mid):
                    key = (entry["publisher"], entry["id"])
                    if key not in seen:
                        seen.add(key)
                        out.append(key)
            continue
        rpub, rmid, _ = resolve(catalog, raw)
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
    price = None
    if pricing is not None and pricing.configured and not mc.is_non_token_model(mid):
        r = pricing.rates(mid)
        if r["priced"] or r["input"] or r["output"]:
            hit = pricing.entry(mid) or {}
            price = {
                "currency": pricing.currency,
                "input_per_1m_tokens": r["input"],
                "output_per_1m_tokens": r["output"],
                "cached_input_per_1m_tokens": r["cached"],
                "cache_write_per_1m_tokens": r["cache_write"],
                "tiers": hit.get("tiers") or {},
                "source": hit.get("source") or ("override" if r["priced"] else pricing.source),
                "skus": hit.get("skus") or {},
            }
    stage = entry.get("launch_stage")
    if entry.get("shutdown_date") or entry.get("deprecation_date") or entry.get("delisted_at"):
        stage = stage or "deprecated"
    pub = entry.get("publisher")
    out = {
        "object": "model",
        "id": mid,
        "owned_by": pub,
        "publisher": pub,
        "display_name": entry.get("display_name") or mid,
        "family": entry.get("family"),
        "resource_name": "publishers/%s/models/%s" % (pub, mid) if pub else None,
        "policy_values": ["publishers/%s/models/%s:%s" % (pub, mid, a)
                          for a in (entry.get("supported_actions") or ["predict"])] if pub else [],
        "surfaces": list(entry.get("surfaces") or []),
        "available_surfaces": list(entry.get("available_surfaces") or []),
        "lifecycle": {
            "stage": stage,
            "version_id": entry.get("version_id"),
            "version_state": entry.get("version_state"),
            "first_listed": entry.get("first_listed"),
            "delisted_at": entry.get("delisted_at"),
            "deprecation_date": entry.get("deprecation_date"),
            "shutdown_date": entry.get("shutdown_date"),
            "replacement": entry.get("replacement"),
        },
        "observed": entry.get("observed"),
        "pricing": price,
        "source": entry.get("source"),
        "helper": bool(entry.get("hidden")),
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
    res = usage_svc.collect_usage(ctx.child(params={}), start, end)
    agg: dict[str, dict] = {}
    for model, m in res.models_seen.items():
        agg[model] = {"requests": m.get("requests", 0), "total_tokens": m.get("total_tokens", 0),
                      "users": len(m.get("users") or ()), "projects": len(m.get("projects") or ()),
                      "surfaces": sorted(m.get("surfaces") or ()), "window_days": days}
    return agg


def list_models(ctx) -> dict:
    """GET /v1/models?publisher=&surface=&stage=&source=&q=&include=usage,access&project=&limit=&cursor="""
    from . import caps as caps_svc
    refresh = str(ctx.params.get("refresh") or "").lower() in ("1", "true")
    catalog = load_catalog(ctx, refresh=refresh)
    pricing = pricing_svc.resolve_pricing(ctx, caps_svc.effective_policy(ctx))
    publisher = (ctx.params.get("publisher") or "").strip().lower()
    surface = (ctx.params.get("surface") or "").strip().lower()
    stage = (ctx.params.get("stage") or "").strip().lower()
    source = (ctx.params.get("source") or "").strip().lower()
    if source and source not in ("model_garden", "observed"):
        raise ApiError("bad_request", "Parameter 'source' must be model_garden or observed.")
    q = (ctx.params.get("q") or "").strip().lower()
    include = {s.strip() for s in (ctx.params.get("include") or "").split(",") if s.strip()}
    show_hidden = str(ctx.params.get("include_helpers") or "").lower() in ("1", "true")
    used_only = str(ctx.params.get("used") or "").lower() in ("1", "true")
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
        if source and entry.get("source") != source:
            continue
        if used_only and not entry.get("observed"):
            continue
        if q and q not in mid and q not in str(entry.get("display_name") or "").lower():
            continue
        u = None
        if usage:
            u = usage.get(mid) or next((v for k, v in usage.items() if model_key(k) == model_key(mid)), None)
        rows.append(model_view(entry, pricing, access=access_eval(entry) if access_eval else None, usage=u))
    page = paginate_list(rows, ctx.params, default_limit=100, max_limit=500)
    doc, _ = ctx.store.load(CATALOG_DOC)
    page["catalog"] = {"fetched_at": doc.get("fetched_at"), "errors": doc.get("errors") or {},
                       "publishers": doc.get("publishers") or publishers_to_scan(ctx, doc),
                       "model_garden_models": sum(1 for e in catalog.values() if e.get("source") == "model_garden"),
                       "observed_only_models": sum(1 for e in catalog.values() if e.get("source") == "observed")}
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
        _pub, mid, _ = resolve(catalog, raw)
    except ApiError:
        mid = str(raw).lower().rsplit("/", 1)[-1]
    entry = catalog.get(mid)
    if entry is None:
        raise ApiError("not_found", "Model '%s' was not found in Model Garden or in any usage log. Run "
                                    "POST /v1/models/sync to refresh." % mid)
    pricing = pricing_svc.resolve_pricing(ctx, caps_svc.effective_policy(ctx))
    access = {}
    for project in ctx.all_projects[:50]:
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
    """POST /v1/models/sync: refreshes the Model Garden catalog now (observed models are kept)."""
    from . import audit
    summary = discover(ctx)
    audit.write_audit(ctx, "models.catalog.synced", target="catalog", details=summary)
    return dict({"object": "model_catalog_sync"}, **summary)


def usage_by_model(ctx) -> dict:
    """GET /v1/usage/models?start=&end=&group_by=user|project|surface|traffic&project=&surface=&model="""
    from . import caps as caps_svc
    from . import usage as usage_svc
    from .. import config, timeutil
    rng = ctx.range or timeutil.parse_range(ctx.params, ctx.tz_name, ctx.ws.get("default_range_days", 14))
    surface = (ctx.params.get("surface") or "").strip().lower()
    if surface and surface not in ("all", *config.VALID_SERVICES):
        raise ApiError("bad_request", "Parameter 'surface' must be one of: all, %s." % ", ".join(config.VALID_SERVICES))
    group_by = (ctx.params.get("group_by") or "").strip().lower()
    if group_by and group_by not in ("user", "project", "surface", "traffic"):
        raise ApiError("bad_request", "Parameter 'group_by' must be user, project, surface or traffic.")
    model_f = (ctx.params.get("model") or "").strip().lower()
    policy = caps_svc.effective_policy(ctx)
    pricing = pricing_svc.resolve_pricing(ctx, policy)
    res = usage_svc.collect_usage(ctx, rng.start_ts(), rng.end_ts(), surface=surface or None)
    catalog = load_catalog(ctx, allow_discovery=False)
    models: dict[str, dict] = {}

    def bucket(model: str, mrec: dict) -> dict:
        m = models.get(model)
        if m is None:
            m = dict(usage_svc._new_counters(), model=model, publisher=mrec.get("publisher"),  # pylint: disable=protected-access
                     surface=mrec.get("surface") or "antigravity", users=set(), projects=set(), groups={},
                     unattributed_requests=0, unattributed_tokens=0)
            models[model] = m
        if not m.get("publisher") and mrec.get("publisher"):
            m["publisher"] = mrec["publisher"]
        return m

    def add_group(m, gkey, mrec):
        g = m["groups"].setdefault(gkey, dict(usage_svc._new_counters(), key=gkey))  # pylint: disable=protected-access
        usage_svc._add(g, mrec)  # pylint: disable=protected-access

    for email, rec in res.users.items():
        for proj, prec in (rec.get("by_project") or {}).items():
            for model, mrec in (prec.get("by_model") or {}).items():
                m = bucket(model, mrec)
                usage_svc._add(m, mrec)  # pylint: disable=protected-access
                m["users"].add(email)
                m["projects"].add(proj)
                if group_by == "traffic":
                    for t, tr in (mrec.get("by_traffic") or {"on_demand": mrec}).items():
                        add_group(m, t, tr)
                elif group_by:
                    add_group(m, {"user": email, "project": proj, "surface": mrec.get("surface") or "antigravity"}[group_by], mrec)
    for proj, prec in res.unattributed.items():
        for model, mrec in (prec.get("by_model") or {}).items():
            m = bucket(model, mrec)
            usage_svc._add(m, mrec)  # pylint: disable=protected-access
            m["projects"].add(proj)
            m["unattributed_requests"] += int(mrec.get("requests") or 0)
            m["unattributed_tokens"] += int(mrec.get("total_tokens") or 0)
            if group_by == "project":
                add_group(m, proj, mrec)
            elif group_by == "user":
                add_group(m, "(unattributed)", mrec)
            elif group_by == "surface":
                add_group(m, mrec.get("surface") or "vertex_ai", mrec)
            elif group_by == "traffic":
                for t, tr in (mrec.get("by_traffic") or {"on_demand": mrec}).items():
                    add_group(m, t, tr)
    rows = []
    for model, m in models.items():
        if model_f and not matches(model, model_f):
            continue
        cost, partial = pricing_svc.estimate_cost({"by_model": {model: m}}, pricing)
        seen = res.models_seen.get(model) or {}
        entry = catalog.get(model) or {}
        row = {k: m[k] for k in ("model", "publisher", "surface") if k in m}
        row.update(usage_svc._public_counts(m))  # pylint: disable=protected-access
        row.update(object="model_usage", active_users=len(m["users"]), projects=sorted(m["projects"]),
                   estimated_cost=cost, cost_partial=partial,
                   currency=pricing.currency if cost is not None else None,
                   cost_basis=pricing_svc.cost_basis(model, m, pricing),
                   by_traffic=usage_svc.traffic_view(m),
                   unattributed_requests=m["unattributed_requests"], unattributed_tokens=m["unattributed_tokens"],
                   surfaces=sorted(seen.get("surfaces") or ()),
                   first_seen_in_range=_iso(seen.get("first_seen")), last_seen=_iso(seen.get("last_seen")),
                   catalog_status=("model_garden" if entry.get("source") == "model_garden" else "observed_only"),
                   lifecycle_stage=entry.get("launch_stage"))
        if group_by:
            groups = []
            for g in m["groups"].values():
                gc, _ = pricing_svc.estimate_cost({"by_model": {model: g}}, pricing)
                gr = {"key": g["key"]}
                gr.update(usage_svc._public_counts(g))  # pylint: disable=protected-access
                gr["estimated_cost"] = gc
                groups.append(gr)
            groups.sort(key=lambda r: (-(r["estimated_cost"] or 0), -r["total_tokens"], r["key"]))
            row["group_by"] = group_by
            row["groups"] = groups
        rows.append(row)
    rows.sort(key=lambda r: (-(r["estimated_cost"] if r["estimated_cost"] is not None else -1), -r["total_tokens"], r["model"]))
    page = paginate_list(rows, ctx.params, default_limit=100, max_limit=500)
    page["range"] = rng.meta()
    page["telemetry_ok"] = res.ok
    page["telemetry"] = usage_svc.usage_meta(res)
    page["pricing_source"] = pricing_svc.source_summary(pricing)
    return page


def safe_discover_error(err: Exception) -> str:
    return getattr(err, "kind", type(err).__name__) if isinstance(err, (GcpError, ApiError)) else type(err).__name__
