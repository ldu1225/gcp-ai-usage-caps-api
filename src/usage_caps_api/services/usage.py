"""Per-user / per-project / per-model usage aggregation from logs only (no Cloud Billing export).

Sources (all are Cloud Logging logs, read from the Log Router BigQuery sink or directly from Cloud Logging):

- Antigravity: `businessaicode.googleapis.com/inference_response` - one entry per request with the person
  (`labels.user_id`), the model (`labels.model_id`) and Gemini `usageMetadata`-style token counts.
- Gemini Platform tokens: `aiplatform.googleapis.com/inference_request` (inference request metadata logs,
  enabled per project with `setPublisherModelConfig`). Resource labels carry project / location / publisher / model
  (`model_user_id`); the payload carries `generateContentDetails.usageMetadata` (Gemini) or
  `rawPredictDetails.usageMetadata` (Claude), `trafficType` and captured custom header labels.
  These logs only carry a service-account principal, so the human caller is attributed in this order:
    1. custom header label `x-goog-agent-platform-observability-label-<caps.vertex_user_header_label>` set by the
       customer portal (value = the user's email or the API `uid`) -> exact;
    2. Cloud Audit Data Access entries (`principalEmail`) for the same project + model in the same minute (+-1 min),
       split proportionally to each caller's request count -> `audit_correlated`;
    3. otherwise the tokens are kept as project-level `unattributed` usage (never silently dropped).
  When inference request logs are not enabled the Data Access audit log still gives per-user request counts.
- Gemini Enterprise: `discoveryengine.googleapis.com/gemini_enterprise_user_activity` / `gen_ai_user_message`
  (per-seat licensed product: requests are tracked, tokens are not billed per request).

Every model identifier found in any log is recorded in `UsageResult.models_seen` so new models are tracked the moment
they are used, without any built-in model list.
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import re
import urllib.parse

from .. import config, identity, timeutil
from ..gcp import GcpError
from ..httpkit import ApiError, paginate_list
from ..schema import expr, quote_ident
from . import pricing as pricing_svc
from .base import (AGY_SINK, AUDIT_DATA_ACCESS, GE_ACTIVITY, GE_MESSAGE, VERTEX_INFERENCE_SINK, human_sql, iso,
                   to_int)

LOG = logging.getLogger("usage_caps_api.usage")

LOGGING_V2 = "https://logging.googleapis.com/v2"
AGY_PAYLOAD_COL = "jsonpayload_v1_inferenceresponselog"
DEFAULT_AGY_LOG = "businessaicode.googleapis.com%2Finference_response"
DEFAULT_VERTEX_LOG = "aiplatform.googleapis.com%2Finference_request"
AUDIT_DATA_ACCESS_LOG = "cloudaudit.googleapis.com%2Fdata_access"
HEADER_LABEL_PREFIX = "x-goog-agent-platform-observability-label-"
MODEL_CALL_METHODS = r"(GenerateContent|StreamGenerateContent|Predict|RawPredict|StreamRawPredict|ChatCompletions)"
_LOGNAME_PROJECT_RE = re.compile(r"^projects/([^/]+)/")
_LABEL_SAFE_PUNCT = frozenset(".-:/+=~!$&'()*,;%")
_MODEL_RES_RE = re.compile(r"publishers/([a-z0-9][a-z0-9\-]{0,62})/models/([^/:@\s]+)", re.IGNORECASE)
_MODEL_ONLY_RE = re.compile(r"models/([^/:@\s]+)", re.IGNORECASE)
_SAFE_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._@\-]{0,159}$")
_HEADER_SAFE_RE = re.compile(r"[^a-z0-9_\-]")

# Counter schema shared by every bucket (user / model / surface / project / unattributed).
PUBLIC_COUNTERS = ("requests", "input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens",
                   "thoughts_tokens", "total_tokens")
LONG_COUNTERS = ("long_requests", "long_input_tokens", "long_output_tokens", "long_cached_tokens")
COUNTERS = PUBLIC_COUNTERS + LONG_COUNTERS
TRAFFIC_COUNTERS = ("requests", "input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens",
                    "total_tokens")
TRAFFIC_TYPES = {"ON_DEMAND": "on_demand", "ON_DEMAND_FLEX": "flex", "ON_DEMAND_PRIORITY": "priority",
                 "PROVISIONED_THROUGHPUT": "provisioned", "BATCH": "batch"}
ATTRIBUTION_KINDS = ("log_identity", "header_label", "audit_correlated", "unattributed")
# Extra per-row metrics carried in `counts["extra"]` (read by /v1/usage/facts; ignored by the classic aggregates).
MODALITIES = ("text", "image", "audio", "video", "document")
EXTRA_COUNTERS = tuple("%s_%s_tokens" % (m, d) for m in MODALITIES for d in ("input", "output")) + (
    "cache_write_5m_tokens", "cache_write_1h_tokens", "latency_ms_sum", "latency_samples")
_DURATION_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)s\s*$")


def duration_ms(raw) -> int:
    """'28.479840432s' (protobuf Duration JSON) -> 28480 ms; 0 when missing or malformed."""
    m = _DURATION_RE.match(str(raw or ""))
    return int(round(float(m.group(1)) * 1000)) if m else 0


def modality_tokens(details, direction: str) -> dict:
    """[{modality: TEXT, tokenCount: n}, ...] -> {"text_input_tokens": n, ...}."""
    out: dict = {}
    for d in details if isinstance(details, list) else []:
        if not isinstance(d, dict):
            continue
        m = str(d.get("modality") or "").strip().lower()
        if m in MODALITIES:
            key = "%s_%s_tokens" % (m, direction)
            out[key] = out.get(key, 0) + to_int(d.get("tokenCount"))
    return out


def entry_project(log_name: str | None, default: str | None = None) -> str | None:
    m = _LOGNAME_PROJECT_RE.match(log_name or "") if isinstance(log_name, str) else None
    return m.group(1) if m else default


def log_id_of(log_name: str | None) -> str:
    if not isinstance(log_name, str) or "/logs/" not in log_name:
        return ""
    return urllib.parse.unquote(log_name.split("/logs/", 1)[1])


def clean_model(raw, publisher: str | None = None) -> tuple[str | None, str | None]:
    """Normalizes a logged model reference to (publisher, model_id) without any model allow-list.

    Accepts bare IDs, `publishers/{p}/models/{m}[@version][:action]` and full Vertex resource paths. Unknown / new
    models pass through unchanged (lower-cased) so they are tracked as soon as they appear in a log."""
    if not isinstance(raw, str):
        return publisher, None
    text = raw.strip()
    if not text:
        return publisher, None
    m = _MODEL_RES_RE.search(text)
    if m:
        publisher = publisher or m.group(1).lower()
        text = m.group(2)
    else:
        m2 = _MODEL_ONLY_RE.search(text)
        if m2:
            text = m2.group(1)
    mid = text.split(":", 1)[0].strip().lower()
    if not _SAFE_MODEL_RE.match(mid):
        mid = re.sub(r"[^a-z0-9._@\-]", "-", mid)[:160].strip("-") or None
    pub = (publisher or "").strip().lower() or None
    return pub, mid


def normalize_traffic(raw) -> str:
    key = str(raw or "").strip().upper()
    if not key:
        return "on_demand"
    return TRAFFIC_TYPES.get(key, key.lower()[:40])


def payload_paths(schema, path: str) -> list[str]:
    typed = sorted(node.name for name, node in ((schema.tree or {}).items() if schema else ())
                   if name.startswith("jsonpayload_"))
    typed.sort(key=lambda n: n.lower() != AGY_PAYLOAD_COL)
    return ["%s.%s" % (col, path) for col in typed] + ["jsonPayload.%s" % path]


def agy_fields(src, alias: str = "t") -> dict:
    s = src.schema
    user = expr(alias, s, "labels.user_id", cast="STRING")

    def tok(name: str):
        return expr(alias, s, *payload_paths(s, "metadata.%s" % name), cast="INT64")

    return {
        "user": identity.sql_person_key(user) if user else None,
        "principal": user,
        "model": expr(alias, s, "labels.model_id", "labels.model", cast="STRING"),
        "client": expr(alias, s, "labels.client_name", cast="STRING"),
        "total": tok("totalTokenCount"),
        "prompt": tok("promptTokenCount"),
        "candidates": tok("candidatesTokenCount"),
        "cached": tok("cachedContentTokenCount"),
        "thoughts": tok("thoughtsTokenCount"),
        "tool_prompt": tok("toolUsePromptTokenCount"),
        "ts": "%s.%s" % (alias, quote_ident(src.time_column())),
    }


def _new_counters() -> dict:
    return {k: 0 for k in COUNTERS}


def _add(dst: dict, src: dict):
    for k in COUNTERS:
        if src.get(k):
            dst[k] = dst.get(k, 0) + int(src[k])
    for t, tr in (src.get("by_traffic") or {}).items():
        cur = dst.setdefault("by_traffic", {}).setdefault(t, {k: 0 for k in TRAFFIC_COUNTERS})
        for k in TRAFFIC_COUNTERS:
            cur[k] += int(tr.get(k) or 0)
    for k, v in (src.get("extra") or {}).items():
        if v:
            ex = dst.setdefault("extra", {})
            ex[k] = ex.get(k, 0) + int(v)


def split_int(total: int, weights: list[float]) -> list[int]:
    """Largest-remainder integer split (parts always sum to `total`)."""
    n = len(weights)
    if n == 0:
        return []
    whole = float(sum(w for w in weights if w > 0))
    if total == 0 or whole <= 0:
        return [total] + [0] * (n - 1)
    raw = [total * (w if w > 0 else 0) / whole for w in weights]
    parts = [int(r) for r in raw]
    order = sorted(range(n), key=lambda i: (-(raw[i] - parts[i]), i))
    for i in order[:total - sum(parts)]:
        parts[i] += 1
    return parts


def token_counts(*, prompt: int, cand: int, cached: int, total: int, thoughts: int = 0, cache_write: int = 0,
                 requests: int = 1, long: dict | None = None, traffic: str | None = None) -> dict:
    """Builds a normalized counter dict. Input = all prompt tokens (cached + cache-write included);
    output = candidates + thinking tokens (thinking is billed at the output rate)."""
    inp = max(0, int(prompt or 0))
    c_tok = min(max(0, int(cached or 0)), inp)
    cw = min(max(0, int(cache_write or 0)), max(0, inp - c_tok))
    th = max(0, int(thoughts or 0))
    out = max(int(total or 0) - inp, max(0, int(cand or 0)) + th, 0)
    tot = max(int(total or 0), inp + out)
    rec = {"requests": max(0, int(requests or 0)), "input_tokens": inp, "output_tokens": out, "cached_tokens": c_tok,
           "cache_write_tokens": cw, "thoughts_tokens": min(th, out), "total_tokens": tot}
    for k in LONG_COUNTERS:
        rec[k] = max(0, int((long or {}).get(k) or 0))
    if traffic:
        rec["by_traffic"] = {traffic: {k: rec[k] for k in TRAFFIC_COUNTERS}}
    return rec


class UsageResult:
    """Aggregated usage across one or more log sources."""

    def __init__(self, source: str):
        self.source = source
        self.ok = False
        self.records = 0
        self.truncated = False
        self.users: dict[str, dict] = {}
        self.daily: dict[tuple[str, dt.date], dict] = {}
        self.project_last: dict[str, dt.datetime] = {}
        self.principals: dict[str, str] = {}
        self.unattributed: dict[str, dict] = {}     # project -> {"by_model": {model: rec}, counters...}
        self.models_seen: dict[str, dict] = {}      # model -> {publisher, surfaces, projects, users, requests, ...}
        self.attribution: dict[str, int] = {k: 0 for k in ATTRIBUTION_KINDS}
        self.token_sources: dict[str, str] = {}     # surface -> "logs" | "requests_only" | "unavailable"
        self.computed_at: dt.datetime | None = None  # when the logs were read (cache snapshots keep the original time)

    def user(self, person: str) -> dict:
        rec = self.users.get(person)
        if rec is None:
            rec = _new_counters()
            rec.update(by_model={}, by_surface={}, by_project={}, last_active=None)
            self.users[person] = rec
        return rec

    def _see_model(self, model: str, publisher: str | None, surface: str, project: str | None,
                   person: str | None, requests: int, tokens: int, ts):
        m = self.models_seen.setdefault(model, {"publisher": None, "surfaces": set(), "projects": set(),
                                                "users": set(), "requests": 0, "total_tokens": 0,
                                                "first_seen": None, "last_seen": None})
        if publisher and not m["publisher"]:
            m["publisher"] = publisher
        m["surfaces"].add(surface)
        if project:
            m["projects"].add(project)
        if person:
            m["users"].add(person)
        m["requests"] += requests
        m["total_tokens"] += tokens
        if isinstance(ts, dt.datetime):
            if m["first_seen"] is None or ts < m["first_seen"]:
                m["first_seen"] = ts
            if m["last_seen"] is None or ts > m["last_seen"]:
                m["last_seen"] = ts

    def add_counts(self, person: str, counts: dict, *, surface: str, project: str | None, model: str | None,
                   publisher: str | None = None, ts=None, raw_principal: str | None = None,
                   attribution: str = "log_identity"):
        if raw_principal and identity.is_workforce_key(person) and person not in self.principals:
            if identity.person_key(raw_principal) == person:
                self.principals[person] = raw_principal.strip()
        u = self.user(person)
        _add(u, counts)
        self.attribution[attribution] = self.attribution.get(attribution, 0) + int(counts.get("requests") or 0)
        if model:
            mrec = u["by_model"].setdefault(model, dict(_new_counters(), surface=surface, publisher=publisher))
            if publisher and not mrec.get("publisher"):
                mrec["publisher"] = publisher
            _add(mrec, counts)
            self._see_model(model, publisher, surface, project, person, int(counts.get("requests") or 0),
                            int(counts.get("total_tokens") or 0), ts)
        srec = u["by_surface"].setdefault(surface, _new_counters())
        _add(srec, counts)
        proj_key = project or "unknown"
        prec = u["by_project"].setdefault(proj_key, dict(_new_counters(), by_model={}))
        _add(prec, counts)
        if model:
            pm = prec["by_model"].setdefault(model, dict(_new_counters(), surface=surface, publisher=publisher))
            _add(pm, counts)
        if isinstance(ts, dt.datetime):
            if u["last_active"] is None or ts > u["last_active"]:
                u["last_active"] = ts

    def add_record(self, person: str, *, surface: str, project: str | None, model: str | None,
                   requests: int, total: int, prompt: int, cand: int, cached: int, ts=None,
                   raw_principal: str | None = None, publisher: str | None = None, traffic: str | None = None,
                   thoughts: int = 0, cache_write: int = 0, long: dict | None = None,
                   attribution: str = "log_identity"):
        counts = token_counts(prompt=prompt, cand=cand, cached=cached, total=total, thoughts=thoughts,
                              cache_write=cache_write, requests=requests, long=long, traffic=traffic)
        self.add_counts(person, counts, surface=surface, project=project, model=model, publisher=publisher, ts=ts,
                        raw_principal=raw_principal, attribution=attribution)

    def add_unattributed(self, project: str | None, model: str | None, counts: dict, *, surface: str,
                         publisher: str | None = None, ts=None, reason: str = "no_user_identity"):
        proj = project or "unknown"
        prec = self.unattributed.setdefault(proj, dict(_new_counters(), by_model={}, reasons={}))
        _add(prec, counts)
        prec["reasons"][reason] = prec["reasons"].get(reason, 0) + int(counts.get("requests") or 0)
        self.attribution["unattributed"] += int(counts.get("requests") or 0)
        if model:
            mrec = prec["by_model"].setdefault(model, dict(_new_counters(), surface=surface, publisher=publisher))
            _add(mrec, counts)
            self._see_model(model, publisher, surface, proj, None, int(counts.get("requests") or 0),
                            int(counts.get("total_tokens") or 0), ts)

    def add_daily(self, person: str, bucket_date: dt.date, *, requests: int, tokens: int):
        cur = self.daily.setdefault((person, bucket_date), {"requests": 0, "total_tokens": 0})
        cur["requests"] += requests
        cur["total_tokens"] += tokens

    def merge(self, other: "UsageResult"):
        self.records += other.records
        self.truncated = self.truncated or other.truncated
        for person, rec in other.users.items():
            u = self.user(person)
            _add(u, rec)
            for model, mrec in (rec.get("by_model") or {}).items():
                dst = u["by_model"].setdefault(model, dict(_new_counters(), surface=mrec.get("surface"),
                                                           publisher=mrec.get("publisher")))
                _add(dst, mrec)
            for surf, srec in (rec.get("by_surface") or {}).items():
                _add(u["by_surface"].setdefault(surf, _new_counters()), srec)
            for proj, prec in (rec.get("by_project") or {}).items():
                dst = u["by_project"].setdefault(proj, dict(_new_counters(), by_model={}))
                _add(dst, prec)
                for model, pm in (prec.get("by_model") or {}).items():
                    dm = dst["by_model"].setdefault(model, dict(_new_counters(), surface=pm.get("surface"),
                                                                publisher=pm.get("publisher")))
                    _add(dm, pm)
            la = rec.get("last_active")
            if isinstance(la, dt.datetime) and (u["last_active"] is None or la > u["last_active"]):
                u["last_active"] = la
        for proj, prec in other.unattributed.items():
            dst = self.unattributed.setdefault(proj, dict(_new_counters(), by_model={}, reasons={}))
            _add(dst, prec)
            for r, n in (prec.get("reasons") or {}).items():
                dst["reasons"][r] = dst["reasons"].get(r, 0) + n
            for model, mrec in (prec.get("by_model") or {}).items():
                dm = dst["by_model"].setdefault(model, dict(_new_counters(), surface=mrec.get("surface"),
                                                            publisher=mrec.get("publisher")))
                _add(dm, mrec)
        for model, m in other.models_seen.items():
            cur = self.models_seen.setdefault(model, {"publisher": None, "surfaces": set(), "projects": set(),
                                                      "users": set(), "requests": 0, "total_tokens": 0,
                                                      "first_seen": None, "last_seen": None})
            cur["publisher"] = cur["publisher"] or m.get("publisher")
            cur["surfaces"] |= set(m.get("surfaces") or ())
            cur["projects"] |= set(m.get("projects") or ())
            cur["users"] |= set(m.get("users") or ())
            cur["requests"] += m.get("requests", 0)
            cur["total_tokens"] += m.get("total_tokens", 0)
            for k, fn in (("first_seen", min), ("last_seen", max)):
                vals = [v for v in (cur.get(k), m.get(k)) if isinstance(v, dt.datetime)]
                cur[k] = fn(vals) if vals else None
        for k, n in other.attribution.items():
            self.attribution[k] = self.attribution.get(k, 0) + n
        for (person, bdate), drec in other.daily.items():
            self.add_daily(person, bdate, requests=drec["requests"], tokens=drec["total_tokens"])
        for p, raw in other.principals.items():
            self.principals.setdefault(p, raw)
        for p, last in other.project_last.items():
            if p not in self.project_last or last > self.project_last[p]:
                self.project_last[p] = last
        self.token_sources.update(other.token_sources)


def _window_params(start: dt.datetime, end: dt.datetime) -> dict:
    return {
        "w_start": start,
        "w_end": end,
        "sfx_start": (start - dt.timedelta(days=1)).strftime("%Y%m%d"),
        "sfx_end": (end + dt.timedelta(days=1)).strftime("%Y%m%d"),
    }


def _long_threshold(ctx) -> int:
    return int((ctx.config.get("pricing") or {}).get("long_context_threshold_tokens") or 200000)


def _add_daily_ts(usage: UsageResult, person: str, ts, rng, tz, requests: int, tokens: int):
    if rng is None or not isinstance(ts, dt.datetime):
        return
    idx = rng.bucket_index(ts.astimezone(tz).date())
    if idx is not None:
        usage.add_daily(person, rng.buckets()[idx], requests=requests, tokens=tokens)


# ---------------------------------------------------------------------------
# Antigravity usage: BigQuery sink + Cloud Logging gap/tail fallback
# ---------------------------------------------------------------------------

def _sources_of(src) -> list:
    if src is None:
        return []
    return src.each() if hasattr(src, "each") else [src]


def _bq_antigravity_one(ctx, src, start: dt.datetime, end: dt.datetime, *, email: str | None = None, rng=None) -> UsageResult:
    f = agy_fields(src)
    if not f["user"]:
        raise GcpError("invalid", message="sink table lacks labels.user_id")
    null = "CAST(NULL AS STRING)"
    where = "%s IS NOT NULL" % f["user"]
    params: dict = dict(_window_params(start, end), long_ctx=_long_threshold(ctx))
    if email:
        where += " AND %s = @email" % f["user"]
        params["email"] = email.lower()
    lname = expr("t", src.schema, "logName", cast="STRING")
    proj_sql = "REGEXP_EXTRACT(%s, r'^projects/([^/]+)/')" % lname if lname else null
    prompt_sql = "IFNULL(%s, 0) + IFNULL(%s, 0)" % (f["prompt"] or "0", f["tool_prompt"] or "0")
    base = (
        "WITH %s,\nu AS (SELECT %s AS ts, %s AS email, %s AS principal, %s AS model, "
        "IFNULL(%s, 0) AS total, %s AS prompt, IFNULL(%s, 0) AS cand, IFNULL(%s, 0) AS cached, "
        "IFNULL(%s, 0) AS thoughts, %s AS project FROM s t WHERE %s)"
        % (src.cte("s", start="w_start", end="w_end"), f["ts"], f["user"], f["principal"] or null,
           f["model"] or null, f["total"] or "0", prompt_sql, f["candidates"] or "0",
           f["cached"] or "0", f["thoughts"] or "0", proj_sql, where)
    )
    main_sql = (
        base + "\nSELECT email, model, project, COUNT(*) AS requests, SUM(total) AS tokens, "
        "SUM(prompt) AS prompt_tokens, SUM(cand) AS candidate_tokens, SUM(cached) AS cached_tokens, "
        "SUM(thoughts) AS thoughts_tokens, COUNTIF(prompt > @long_ctx) AS long_requests, "
        "SUM(IF(prompt > @long_ctx, prompt, 0)) AS long_input_tokens, "
        "SUM(IF(prompt > @long_ctx, GREATEST(total - prompt, cand + thoughts, 0), 0)) AS long_output_tokens, "
        "SUM(IF(prompt > @long_ctx, cached, 0)) AS long_cached_tokens, "
        "ANY_VALUE(principal) AS principal, MIN(ts) AS first_ts, MAX(ts) AS last_ts\n"
        "FROM u GROUP BY email, model, project"
    )
    tasks = {"main": lambda: ctx.query(main_sql, params, "usage")}
    if rng is not None:
        from .base import bucket_sql
        b_params = dict(params, tz=rng.tz_name, start_date=rng.start, bucket_days=rng.bucket_days)
        b_sql = base + "\nSELECT %s AS bucket, email, COUNT(*) AS requests, SUM(total) AS tokens FROM u GROUP BY bucket, email" % bucket_sql("ts")
        tasks["bucket"] = lambda: ctx.query(b_sql, b_params, "usage")
    res = ctx.run(tasks)
    if not res["main"][0]:
        raise res["main"][1]
    usage = UsageResult("bigquery")
    for row in res["main"][1]:
        if not row.get("email"):
            continue
        req = to_int(row.get("requests"))
        usage.records += req
        rp, rl = row.get("project"), row.get("last_ts")
        if isinstance(rp, str) and isinstance(rl, dt.datetime):
            if rp not in usage.project_last or rl > usage.project_last[rp]:
                usage.project_last[rp] = rl
        person = identity.person_key(row["email"])
        if person is None:
            continue
        pub, model = clean_model(row.get("model"))
        usage.add_record(
            person, surface="antigravity", project=rp if isinstance(rp, str) else src.project,
            model=model or "unspecified", publisher=pub, requests=req,
            total=to_int(row.get("tokens")), prompt=to_int(row.get("prompt_tokens")),
            cand=to_int(row.get("candidate_tokens")), cached=to_int(row.get("cached_tokens")),
            thoughts=to_int(row.get("thoughts_tokens")),
            long={k: to_int(row.get(k)) for k in LONG_COUNTERS},
            ts=rl, raw_principal=row.get("principal"),
        )
        if model and isinstance(row.get("first_ts"), dt.datetime):
            usage._see_model(model, pub, "antigravity", rp, person, 0, 0, row["first_ts"])  # pylint: disable=protected-access
    if rng is not None and res.get("bucket", (False, None))[0]:
        for row in res["bucket"][1]:
            person = identity.person_key(row.get("email"))
            b = row.get("bucket")
            if isinstance(b, dt.datetime):
                b = b.date()
            if person and isinstance(b, dt.date):
                usage.add_daily(person, b, requests=to_int(row.get("requests")), tokens=to_int(row.get("tokens")))
    usage.token_sources["antigravity"] = "logs"
    usage.ok = True
    return usage


def _bq_antigravity(ctx, src, start: dt.datetime, end: dt.datetime, *, email: str | None = None, rng=None) -> UsageResult:
    parts = _sources_of(src)
    if len(parts) == 1:
        return _bq_antigravity_one(ctx, parts[0], start, end, email=email, rng=rng)
    res = ctx.run({s.label: (lambda one=s: _bq_antigravity_one(ctx, one, start, end, email=email, rng=rng)) for s in parts})
    merged = UsageResult("bigquery")
    merged.token_sources["antigravity"] = "logs"
    first_err = None
    any_ok = False
    for s in parts:
        ok, val = res.get(s.label, (False, None))
        if ok:
            any_ok = True
            merged.merge(val)
        else:
            first_err = first_err or val
            ctx.warn_error(s.label, val, "Antigravity BigQuery usage")
    if not any_ok:
        raise first_err or GcpError("unavailable", message="Antigravity BigQuery usage failed")
    merged.ok = True
    return merged


def _user_label_regex(email: str) -> str:
    parts = [ch if (ch.isalnum() or ch in "@_") else "[%s]" % ch for ch in email.lower()]
    return "(?i)^(user:)?%s$" % "".join(parts)


def _principal_label_regex(key: str) -> str:
    parts = []
    for ch in key:
        if ch.isalnum() or ch in "@_":
            parts.append(ch)
        elif ch in _LABEL_SAFE_PUNCT:
            parts.append("[%s]" % ch)
        else:
            raise GcpError("invalid", message="unsupported principal")
    return "(?i)^%s$" % "".join(parts)


def _log_ts(val: dt.datetime) -> str:
    val = val.astimezone(dt.timezone.utc)
    return val.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if val.microsecond else (iso(val) or "")


def _logging_project(ctx, project: str, log_id: str, start: dt.datetime, end: dt.datetime, email: str | None = None,
                     extra_filter: str = ""):
    flt = 'logName="projects/%s/logs/%s" AND timestamp>="%s" AND timestamp<"%s"' % (
        project, log_id, _log_ts(start), _log_ts(end))
    if extra_filter:
        flt += " AND " + extra_filter
    if email:
        if identity.is_workforce_key(email):
            flt += ' AND labels.user_id=~"%s"' % _principal_label_regex(email)
        elif config.valid_email(email):
            flt += ' AND labels.user_id=~"%s"' % _user_label_regex(email)
        else:
            raise GcpError("invalid", message="invalid email")
    limit = int((ctx.config.get("caps") or {}).get("max_log_entries") or 20000)
    token, fetched, entries, truncated = None, 0, [], False
    want = urllib.parse.unquote(log_id)
    while True:
        body: dict = {"resourceNames": ["projects/%s" % project], "filter": flt, "orderBy": "timestamp desc",
                      "pageSize": min(1000, max(1, limit - fetched))}
        if token:
            body["pageToken"] = token
        data = ctx.rest("POST", "%s/entries:list" % LOGGING_V2, json_body=body, timeout=45)
        batch = [e for e in ((data or {}).get("entries") or []) if isinstance(e, dict)
                 and log_id_of(e.get("logName")) == want]
        entries.extend(batch)
        fetched += len((data or {}).get("entries") or [])
        token = (data or {}).get("nextPageToken")
        if not token:
            break
        if fetched >= limit:
            truncated = True
            break
    return entries, truncated


def _agy_log_id(ctx) -> str:
    log_id = (ctx.config.get("caps") or {}).get("inference_log") or DEFAULT_AGY_LOG
    if not config.LOG_NAME_RE.match(log_id):
        raise GcpError("invalid", message="invalid inference_log")
    return log_id


def parse_agy_entry(entry: dict, long_ctx: int) -> dict | None:
    labels = entry.get("labels") if isinstance(entry.get("labels"), dict) else {}
    raw_principal = labels.get("user_id")
    if not isinstance(raw_principal, str) or not raw_principal.strip():
        return None
    payload = entry.get("jsonPayload") if isinstance(entry.get("jsonPayload"), dict) else {}
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    prompt = to_int(meta.get("promptTokenCount")) + to_int(meta.get("toolUsePromptTokenCount"))
    cand = to_int(meta.get("candidatesTokenCount"))
    thoughts = to_int(meta.get("thoughtsTokenCount"))
    total = to_int(meta.get("totalTokenCount"))
    cached = to_int(meta.get("cachedContentTokenCount"))
    is_long = prompt > long_ctx
    pub, model = clean_model(labels.get("model_id") or labels.get("model"))
    counts = token_counts(prompt=prompt, cand=cand, cached=cached, total=total, thoughts=thoughts, long={
        "long_requests": 1 if is_long else 0, "long_input_tokens": prompt if is_long else 0,
        "long_output_tokens": max(total - prompt, cand + thoughts, 0) if is_long else 0,
        "long_cached_tokens": cached if is_long else 0})
    return {"raw_principal": raw_principal, "person": identity.person_key(raw_principal), "publisher": pub,
            "model": model or "unspecified", "counts": counts,
            "ts": timeutil.parse_rfc3339(entry.get("timestamp")),
            "project": entry_project(entry.get("logName"))}


def _logging_antigravity(ctx, start: dt.datetime, end: dt.datetime, *, email: str | None = None,
                         rng=None, ranges: dict | None = None) -> UsageResult:
    log_id = _agy_log_id(ctx)
    projects = list(ctx.projects)
    if ranges is not None:
        projects = [p for p in projects if p in ranges]
    else:
        ranges = {p: (start, end) for p in projects}
    usage = UsageResult("logging")
    tz = timeutil.get_zone(ctx.tz_name)
    long_ctx = _long_threshold(ctx)
    tasks = {p: (lambda pr=p: _logging_project(ctx, pr, log_id, ranges[pr][0], ranges[pr][1], email=email))
             for p in projects}
    res = ctx.run(tasks)
    for p in projects:
        ok, val = res.get(p, (False, None))
        if not ok:
            if p == ctx.project:
                raise val
            continue
        entries, truncated = val
        usage.truncated = usage.truncated or truncated
        for entry in entries:
            item = parse_agy_entry(entry, long_ctx)
            if item is None:
                continue
            usage.records += 1
            if item["person"] is None:
                continue
            usage.add_counts(item["person"], item["counts"], surface="antigravity", project=item["project"] or p,
                             model=item["model"], publisher=item["publisher"], ts=item["ts"],
                             raw_principal=item["raw_principal"])
            _add_daily_ts(usage, item["person"], item["ts"], rng, tz, 1, item["counts"]["total_tokens"])
    usage.token_sources["antigravity"] = "logs"
    usage.ok = True
    return usage


def antigravity_usage(ctx, start: dt.datetime, end: dt.datetime, *, email: str | None = None,
                      rng=None, fresh: bool = False) -> UsageResult:
    """Reads Antigravity usage from BigQuery sink (with fresh Cloud Logging tail) or Cloud Logging."""
    mode = (ctx.config.get("caps") or {}).get("usage_source") or "auto"
    src = ctx.audit_source(AGY_SINK, optional=True) if mode in ("auto", "bigquery") else None
    if src is not None:
        try:
            usage = _bq_antigravity(ctx, src, start, end, email=email, rng=rng)
            ctx.add_source(src.label)
            if fresh and mode == "auto":
                # Tail read from Cloud Logging for the newest entries not yet flushed to BigQuery.
                tail_ranges = {}
                for p in ctx.projects:
                    last = usage.project_last.get(p)
                    lo = (last + dt.timedelta(microseconds=1)) if (last and last >= start) else start
                    if lo < end:
                        tail_ranges[p] = (lo, end)
                if tail_ranges:
                    try:
                        extra = _logging_antigravity(ctx, start, end, email=email, rng=rng, ranges=tail_ranges)
                        usage.merge(extra)
                        ctx.add_source("api:logging.entries")
                    except GcpError:
                        pass
            return usage
        except GcpError as err:
            if mode == "bigquery":
                ctx.warn_error(src.label, err, "Antigravity BigQuery usage")
                return UsageResult("unavailable")
    if mode in ("auto", "logging"):
        try:
            usage = _logging_antigravity(ctx, start, end, email=email, rng=rng)
            ctx.add_source("api:logging.entries")
            return usage
        except GcpError as err:
            ctx.warn_error("api:logging.entries", err, "Antigravity Cloud Logging usage")
    return UsageResult("unavailable")


# ---------------------------------------------------------------------------
# Gemini Platform: inference request metadata logs + Data Access audit correlation
# ---------------------------------------------------------------------------

def header_label_key(ctx) -> str:
    raw = str((ctx.config.get("caps") or {}).get("vertex_user_header_label") or "user").strip().lower()
    return _HEADER_SAFE_RE.sub("", raw)[:40] or "user"


def _header_values(headers: dict, name: str) -> list[str]:
    if not isinstance(headers, dict):
        return []
    for key, val in headers.items():
        if str(key).lower().replace("_", "-") != name:
            continue
        if isinstance(val, dict):
            val = val.get("values") if "values" in val else val.get("value")
        if isinstance(val, str):
            return [val]
        if isinstance(val, list):
            return [str(v) for v in val if isinstance(v, (str, int))]
    return []


def resolve_user_label(value, directory: dict) -> str | None:
    """Header label value -> person key. Accepts an email / workforce principal or the API `uid`."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    person = identity.person_key(raw)
    if person:
        return person
    return directory.get(raw.lower())


def _norm_project(raw, fallback: str | None = None) -> str | None:
    if isinstance(raw, str):
        val = raw.split("/", 1)[1] if raw.startswith("projects/") else raw
        val = val.strip().lower()
        if config.valid_project(val):
            return val
    return fallback


def parse_vertex_entry(entry: dict, *, header_label: str, long_ctx: int) -> dict | None:
    """Parses one `aiplatform.googleapis.com/inference_request` entry. Returns None for failed requests."""
    if not isinstance(entry, dict):
        return None
    payload = entry.get("jsonPayload") if isinstance(entry.get("jsonPayload"), dict) else {}
    if str(entry.get("severity") or "").upper() == "ERROR" or payload.get("error"):
        return None
    res_labels = ((entry.get("resource") or {}).get("labels") or {}) if isinstance(entry.get("resource"), dict) else {}
    project = _norm_project(res_labels.get("resource_container"), entry_project(entry.get("logName")))
    pub, model = clean_model(res_labels.get("model_user_id") or res_labels.get("model_id") or res_labels.get("model"),
                             res_labels.get("publisher"))
    if not model:
        return None
    gmeta = ((payload.get("generateContentDetails") or {}).get("usageMetadata") or {}) \
        if isinstance(payload.get("generateContentDetails"), dict) else {}
    cmeta = ((payload.get("rawPredictDetails") or {}).get("usageMetadata") or {}) \
        if isinstance(payload.get("rawPredictDetails"), dict) else {}
    if gmeta:
        prompt = to_int(gmeta.get("promptTokenCount")) + to_int(gmeta.get("toolUsePromptTokenCount"))
        cand = to_int(gmeta.get("candidatesTokenCount"))
        thoughts = to_int(gmeta.get("thoughtsTokenCount"))
        cached = to_int(gmeta.get("cachedContentTokenCount"))
        cache_write = 0
        total = to_int(gmeta.get("totalTokenCount"))
    elif cmeta:
        def g(*names):
            for n in names:
                if n in cmeta:
                    return to_int(cmeta.get(n))
            return 0
        uncached = g("input_tokens", "inputTokens")
        cached = g("cache_read_input_tokens", "cacheReadInputTokens")
        cache_write = g("cache_creation_input_tokens", "cacheCreationInputTokens")
        cand = g("output_tokens", "outputTokens")
        prompt = uncached + cached + cache_write
        thoughts = 0
        total = prompt + cand
    else:
        return None
    extra: dict = {}
    if gmeta:
        extra.update(modality_tokens(gmeta.get("promptTokensDetails"), "input"))
        extra.update(modality_tokens(gmeta.get("candidatesTokensDetails"), "output"))
    elif cmeta:
        cc = cmeta.get("cache_creation") if isinstance(cmeta.get("cache_creation"), dict) else {}
        extra["cache_write_5m_tokens"] = to_int(cc.get("ephemeral_5m_input_tokens"))
        extra["cache_write_1h_tokens"] = to_int(cc.get("ephemeral_1h_input_tokens"))
    lat = duration_ms(payload.get("requestDuration"))
    if lat:
        extra.update(latency_ms_sum=lat, latency_samples=1)
    is_long = prompt > long_ctx
    traffic = normalize_traffic(payload.get("trafficType"))
    counts = token_counts(prompt=prompt, cand=cand, cached=cached, total=total, thoughts=thoughts,
                          cache_write=cache_write, traffic=traffic, long={
                              "long_requests": 1 if is_long else 0, "long_input_tokens": prompt if is_long else 0,
                              "long_output_tokens": max(total - prompt, cand + thoughts, 0) if is_long else 0,
                              "long_cached_tokens": cached if is_long else 0})
    headers = payload.get("httpHeaders") if isinstance(payload.get("httpHeaders"), dict) else {}
    user_vals = _header_values(headers, HEADER_LABEL_PREFIX + header_label)
    principal = payload.get("principal")
    if isinstance(principal, dict):
        principal = (principal.get("id") or principal.get("email") or principal.get("principalEmail")
                     or principal.get("serviceAccount"))
    ts = timeutil.parse_rfc3339(payload.get("requestStartTime")) or timeutil.parse_rfc3339(entry.get("timestamp"))
    counts["extra"] = {k: v for k, v in extra.items() if v}
    labels = entry.get("labels") if isinstance(entry.get("labels"), dict) else {}
    return {"project": project, "publisher": pub, "model": model, "counts": counts, "traffic": traffic,
            "user_label": user_vals[0] if user_vals else None,
            "principal": principal if isinstance(principal, str) else None,
            "location": str(res_labels.get("location") or "").strip().lower() or None,
            "app": str(labels.get("app") or "").strip()[:120] or None,
            "ts": ts, "key": str(entry.get("insertId") or "")}


def _minute(ts) -> dt.datetime | None:
    if not isinstance(ts, dt.datetime):
        return None
    ts = ts.astimezone(dt.timezone.utc)
    return ts.replace(second=0, microsecond=0)


def _vertex_fields(src) -> dict:
    s = src.schema

    def p(*paths, cast="INT64"):
        cands = []
        for path in paths:
            cands.extend(payload_paths(s, path))
        return expr("t", s, *cands, cast=cast)

    hdr = "httpHeaders.%s" % (HEADER_LABEL_PREFIX.replace("-", "_") + "{label}")
    return {
        "project": expr("t", s, "resource.labels.resource_container", cast="STRING"),
        "publisher": expr("t", s, "resource.labels.publisher", cast="STRING"),
        "model": expr("t", s, "resource.labels.model_user_id", "resource.labels.model_id", cast="STRING"),
        "start": p("requestStartTime", cast="TIMESTAMP"),
        "traffic": p("trafficType", cast="STRING"),
        "severity": expr("t", s, "severity", cast="STRING"),
        "error": p("error.code", cast="STRING"),
        "g_prompt": p("generateContentDetails.usageMetadata.promptTokenCount"),
        "g_tool": p("generateContentDetails.usageMetadata.toolUsePromptTokenCount"),
        "g_cand": p("generateContentDetails.usageMetadata.candidatesTokenCount"),
        "g_thoughts": p("generateContentDetails.usageMetadata.thoughtsTokenCount"),
        "g_cached": p("generateContentDetails.usageMetadata.cachedContentTokenCount"),
        "g_total": p("generateContentDetails.usageMetadata.totalTokenCount"),
        "c_in": p("rawPredictDetails.usageMetadata.input_tokens", "rawPredictDetails.usageMetadata.inputTokens"),
        "c_out": p("rawPredictDetails.usageMetadata.output_tokens", "rawPredictDetails.usageMetadata.outputTokens"),
        "c_cr": p("rawPredictDetails.usageMetadata.cache_read_input_tokens",
                  "rawPredictDetails.usageMetadata.cacheReadInputTokens"),
        "c_cw": p("rawPredictDetails.usageMetadata.cache_creation_input_tokens",
                  "rawPredictDetails.usageMetadata.cacheCreationInputTokens"),
        "principal": p("principal.id", "principal.email", "principal", cast="STRING"),
        "location": expr("t", s, "resource.labels.location", cast="STRING"),
        "app": expr("t", s, "labels.app", cast="STRING"),
        "duration": p("requestDuration", cast="STRING"),
        "c_cw5m": p("rawPredictDetails.usageMetadata.cache_creation.ephemeral_5m_input_tokens"),
        "c_cw1h": p("rawPredictDetails.usageMetadata.cache_creation.ephemeral_1h_input_tokens"),
        "mod_in": _repeated_path(s, "generateContentDetails.usageMetadata.promptTokensDetails"),
        "mod_out": _repeated_path(s, "generateContentDetails.usageMetadata.candidatesTokensDetails"),
        "header_tpl": hdr,
        "ts": "t.%s" % quote_ident(src.time_column()),
        "log": expr("t", s, "logName", cast="STRING"),
    }


def _repeated_path(s, payload_path: str) -> str | None:
    """SQL path of a REPEATED STRUCT<modality, tokenCount> payload field (no repeated ancestors), or None."""
    if s is None:
        return None
    for cand in payload_paths(s, payload_path):
        cur, names, ok, node = s.tree, [], True, None
        parts = cand.split(".")
        for i, part in enumerate(parts):
            node = cur.get(part.lower())
            if node is None or (node.mode == "REPEATED" and i != len(parts) - 1):
                ok = False
                break
            names.append(node.name)
            cur = node.children
        if ok and node is not None and node.mode == "REPEATED" and "modality" in node.children \
                and "tokencount" in node.children:
            return "t." + ".".join(quote_ident(n) for n in names)
    return None


def _modality_sum_sql(path: str | None, modality: str) -> str:
    if not path:
        return "0"
    return ("(SELECT IFNULL(SUM(SAFE_CAST(d.tokenCount AS INT64)), 0) FROM UNNEST(%s) d "
            "WHERE UPPER(CAST(d.modality AS STRING)) = '%s')" % (path, modality.upper()))


def _refresh_vertex_schema(ctx, src) -> bool:
    """Re-fetches BigQuery table schema live when Cloud Logging auto-evolves inference_request columns."""
    registry = getattr(getattr(ctx, "app", None), "registry", None)
    if registry is None or not hasattr(registry, "get_schema_and_location"):
        return False
    try:
        fresh_schema, loc = registry.get_schema_and_location(
            src.project, src.dataset, getattr(src, "sample_table", src.table),
            quota_project=ctx.quota_project, fresh=True,
        )
        if fresh_schema is not None:
            src.schema = fresh_schema
            if loc:
                src.location = loc
            return True
    except Exception:  # pylint: disable=broad-except
        return False
    return False


def _bq_vertex_inference_one(ctx, src, start: dt.datetime, end: dt.datetime) -> list[dict]:
    f = _vertex_fields(src)
    if not f["model"]:
        raise GcpError("invalid", message="inference_request sink table lacks resource.labels.model_user_id")
    label = header_label_key(ctx).replace("-", "_")

    def _header_sql(s_obj, f_obj):
        hcol = f_obj["header_tpl"].format(label=label)
        hpaths = payload_paths(s_obj, hcol)
        return expr("t", s_obj, *[c + ".values[0]" for c in hpaths], *[c + ".values" for c in hpaths],
                    *hpaths, cast="STRING")

    user_sql = _header_sql(src.schema, f)
    # Cloud Logging BigQuery sinks infer schema incrementally from the first observed row (e.g. a 404 error or
    # Gemini-only call before Claude/header rows arrive). Refresh live when token or header columns are missing.
    if (not f["g_prompt"] and not f["g_total"]) or (not f["c_in"] and not f["c_out"]) or not user_sql:
        if _refresh_vertex_schema(ctx, src):
            f = _vertex_fields(src)
            if not f["model"]:
                raise GcpError("invalid", message="inference_request sink table lacks resource.labels.model_user_id")
            user_sql = _header_sql(src.schema, f)
    null = "CAST(NULL AS STRING)"
    log_proj = "REGEXP_EXTRACT(%s, r'^projects/([^/]+)/')" % f["log"] if f["log"] else null
    proj = f["project"] or log_proj
    z = lambda col: "IFNULL(%s, 0)" % (col or "0")  # noqa: E731
    prompt = "(%s + %s + %s + %s + %s)" % (z(f["g_prompt"]), z(f["g_tool"]), z(f["c_in"]), z(f["c_cr"]), z(f["c_cw"]))
    cand = "(%s + %s)" % (z(f["g_cand"]), z(f["c_out"]))
    thoughts = z(f["g_thoughts"])
    cached = "(%s + %s)" % (z(f["g_cached"]), z(f["c_cr"]))
    cwrite = z(f["c_cw"])
    total = "GREATEST(%s, %s + %s + %s)" % (z(f["g_total"]), prompt, cand, thoughts)
    ok = []
    if f["severity"]:
        ok.append("IFNULL(%s, '') != 'ERROR'" % f["severity"])
    if f["error"]:
        ok.append("%s IS NULL" % f["error"])
    where = " AND ".join(ok) if ok else "TRUE"
    start_ts = "IFNULL(%s, %s)" % (f["start"], f["ts"]) if f["start"] else f["ts"]
    extra_cols = [("%s_%s_tokens" % (m, d), _modality_sum_sql(f["mod_in" if d == "input" else "mod_out"], m))
                  for m in MODALITIES for d in ("input", "output")]
    extra_cols += [("cache_write_5m_tokens", z(f["c_cw5m"])), ("cache_write_1h_tokens", z(f["c_cw1h"]))]
    lat_sql = ("IFNULL(SAFE_CAST(REGEXP_EXTRACT(%s, r'^([0-9.]+)s$') AS FLOAT64) * 1000, 0)" % f["duration"]
               if f["duration"] else "0")
    extra_cols += [("latency_ms_sum", "CAST(ROUND(%s) AS INT64)" % lat_sql),
                   ("latency_samples", "IF(%s > 0, 1, 0)" % lat_sql)]
    sql = (
        "WITH %s,\nv AS (SELECT %s AS ts, %s AS raw_ts, %s AS project, %s AS log_project, %s AS publisher, %s AS model, "
        "%s AS user_label, %s AS principal, IFNULL(%s, 'ON_DEMAND') AS traffic, %s AS prompt, %s AS cand, "
        "%s AS thoughts, %s AS cached, %s AS cwrite, %s AS total, LOWER(%s) AS location, %s AS app, %s "
        "FROM s t WHERE %s AND %s IS NOT NULL)\n"
        "SELECT TIMESTAMP_TRUNC(ts, MINUTE) AS minute, project, log_project, publisher, model, user_label, principal, "
        "traffic, location, app, COUNT(*) AS requests, SUM(prompt) AS prompt_tokens, SUM(cand) AS candidate_tokens, "
        "SUM(thoughts) AS thoughts_tokens, SUM(cached) AS cached_tokens, SUM(cwrite) AS cache_write_tokens, "
        "SUM(total) AS tokens, COUNTIF(prompt > @long_ctx) AS long_requests, "
        "SUM(IF(prompt > @long_ctx, prompt, 0)) AS long_input_tokens, "
        "SUM(IF(prompt > @long_ctx, GREATEST(total - prompt, cand + thoughts, 0), 0)) AS long_output_tokens, "
        "SUM(IF(prompt > @long_ctx, cached, 0)) AS long_cached_tokens, %s, MAX(ts) AS last_ts, MAX(raw_ts) AS max_log_ts\n"
        "FROM v GROUP BY minute, project, log_project, publisher, model, user_label, principal, traffic, location, app"
        % (src.cte("s", start="w_start", end="w_end"), start_ts, f["ts"], proj, log_proj, f["publisher"] or null, f["model"],
           user_sql or null, f["principal"] or null, f["traffic"] or "CAST(NULL AS STRING)", prompt, cand, thoughts,
           cached, cwrite, total, f["location"] or null, f["app"] or null,
           ", ".join("%s AS x_%s" % (sql_e, name) for name, sql_e in extra_cols), where, f["model"],
           ", ".join("SUM(x_%s) AS %s" % (name, name) for name, _ in extra_cols))
    )
    rows = ctx.query(sql, dict(_window_params(start, end), long_ctx=_long_threshold(ctx)), "usage")
    out = []
    for r in rows:
        pub, model = clean_model(r.get("model"), r.get("publisher"))
        if not model:
            continue
        traffic = normalize_traffic(r.get("traffic"))
        counts = token_counts(prompt=to_int(r.get("prompt_tokens")), cand=to_int(r.get("candidate_tokens")),
                              cached=to_int(r.get("cached_tokens")), total=to_int(r.get("tokens")),
                              thoughts=to_int(r.get("thoughts_tokens")), cache_write=to_int(r.get("cache_write_tokens")),
                              requests=to_int(r.get("requests")), traffic=traffic,
                              long={k: to_int(r.get(k)) for k in LONG_COUNTERS})
        minute = r.get("minute")
        proj_id = _norm_project(r.get("project"), _norm_project(r.get("log_project"), src.project))
        log_ts = r.get("max_log_ts") if isinstance(r.get("max_log_ts"), dt.datetime) else (
            r.get("last_ts") if isinstance(r.get("last_ts"), dt.datetime) else minute)
        counts["extra"] = {k: to_int(r.get(k)) for k in EXTRA_COUNTERS if to_int(r.get(k))}
        out.append({"project": proj_id, "publisher": pub, "model": model, "counts": counts,
                    "traffic": traffic, "user_label": r.get("user_label"), "principal": r.get("principal"),
                    "location": (str(r.get("location") or "").strip().lower() or None),
                    "app": (str(r.get("app") or "").strip()[:120] or None),
                    "ts": r.get("last_ts") if isinstance(r.get("last_ts"), dt.datetime) else minute,
                    "log_ts": log_ts,
                    "minute": _minute(minute) if isinstance(minute, dt.datetime) else _minute(r.get("last_ts"))})
    return out


def _bq_vertex_inference(ctx, src, start: dt.datetime, end: dt.datetime) -> list[dict]:
    parts = _sources_of(src)
    if len(parts) == 1:
        return _bq_vertex_inference_one(ctx, parts[0], start, end)
    res = ctx.run({s.label: (lambda one=s: _bq_vertex_inference_one(ctx, one, start, end)) for s in parts})
    out, first_err, any_ok = [], None, False
    for s in parts:
        ok, val = res.get(s.label, (False, None))
        if ok:
            any_ok = True
            out.extend(val)
        else:
            first_err = first_err or val
            ctx.warn_error(s.label, val, "Gemini Platform inference request logs")
    if not any_ok:
        raise first_err or GcpError("unavailable", message="Gemini Platform inference request logs failed")
    return out


def _bq_vertex_audit_one(ctx, dsrc, start: dt.datetime, end: dt.datetime) -> list[dict] | None:
    s = dsrc.schema
    principal = expr("t", s, "protopayload_auditlog.authenticationInfo.principalEmail", cast="STRING")
    service = expr("t", s, "protopayload_auditlog.serviceName", cast="STRING")
    method = expr("t", s, "protopayload_auditlog.methodName", cast="STRING")
    resource = expr("t", s, "protopayload_auditlog.resourceName", cast="STRING")
    lname = expr("t", s, "logName", cast="STRING")
    ts = "t.%s" % quote_ident(dsrc.time_column())
    if not (principal and service and method):
        return None
    model_sql = "REGEXP_EXTRACT(%s, r'models/([^/:@]+)')" % resource if resource else "CAST(NULL AS STRING)"
    pub_sql = "REGEXP_EXTRACT(%s, r'publishers/([^/]+)/')" % resource if resource else "CAST(NULL AS STRING)"
    proj_sql = "REGEXP_EXTRACT(%s, r'^projects/([^/]+)/')" % lname if lname else "CAST(NULL AS STRING)"
    sql = (
        "WITH %s,\nv AS (SELECT %s AS ts, LOWER(%s) AS email, %s AS model, %s AS publisher, %s AS project "
        "FROM da_src t WHERE %s = 'aiplatform.googleapis.com' AND REGEXP_CONTAINS(%s, r'%s'))\n"
        "SELECT TIMESTAMP_TRUNC(ts, MINUTE) AS minute, email, model, publisher, project, COUNT(*) AS requests, "
        "MAX(ts) AS last_ts FROM v WHERE email IS NOT NULL AND %s GROUP BY minute, email, model, publisher, project"
        % (dsrc.cte("da_src", start="w_start", end="w_end"), ts, principal, model_sql, pub_sql, proj_sql,
           service, method, MODEL_CALL_METHODS,
           "email LIKE '%@%'" if identity.service_account_subjects() else human_sql("email"))
    )
    rows = ctx.query(sql, _window_params(start, end), "usage")
    out = []
    for r in rows:
        pub, model = clean_model(r.get("model"), r.get("publisher"))
        minute = r.get("minute") if isinstance(r.get("minute"), dt.datetime) else r.get("last_ts")
        out.append({"email": r.get("email"), "project": r.get("project") or dsrc.project, "model": model,
                    "publisher": pub, "requests": to_int(r.get("requests")), "minute": _minute(minute),
                    "ts": r.get("last_ts") if isinstance(r.get("last_ts"), dt.datetime) else minute})
    return out


def _bq_vertex_audit(ctx, dsrc, start: dt.datetime, end: dt.datetime) -> list[dict] | None:
    parts = _sources_of(dsrc)
    if len(parts) == 1:
        return _bq_vertex_audit_one(ctx, parts[0], start, end)
    res = ctx.run({s.label: (lambda one=s: _bq_vertex_audit_one(ctx, one, start, end)) for s in parts})
    out, first_err, any_ok = [], None, False
    for s in parts:
        ok, val = res.get(s.label, (False, None))
        if ok:
            if val is not None:
                any_ok = True
                out.extend(val)
        else:
            first_err = first_err or val
            ctx.warn_error(s.label, val, "Gemini Platform Data Access audit logs")
    if not any_ok:
        if first_err is not None:
            raise first_err
        return None
    return out


def parse_audit_entry(entry: dict) -> dict | None:
    proto = entry.get("protoPayload") if isinstance(entry.get("protoPayload"), dict) else {}
    if proto.get("serviceName") != "aiplatform.googleapis.com":
        return None
    if not re.search(MODEL_CALL_METHODS, str(proto.get("methodName") or "")):
        return None
    email = ((proto.get("authenticationInfo") or {}).get("principalEmail") or "")
    pub, model = clean_model(proto.get("resourceName"))
    ts = timeutil.parse_rfc3339(entry.get("timestamp"))
    return {"email": str(email).lower(), "project": entry_project(entry.get("logName")), "model": model,
            "publisher": pub, "requests": 1, "minute": _minute(ts), "ts": ts}


def _logging_vertex(ctx, start: dt.datetime, end: dt.datetime, *,
                    need_audit: bool = True,
                    ranges: dict | None = None) -> tuple[list[dict], list[dict], bool]:
    log_id = (ctx.config.get("caps") or {}).get("vertex_inference_log") or DEFAULT_VERTEX_LOG
    if not config.LOG_NAME_RE.match(log_id):
        raise GcpError("invalid", message="invalid vertex_inference_log")
    label = header_label_key(ctx)
    long_ctx = _long_threshold(ctx)
    projects = list(ctx.projects)
    if ranges is not None:
        projects = [p for p in projects if p in ranges]
    else:
        ranges = {p: (start, end) for p in projects}
    da_filter = ('protoPayload.serviceName="aiplatform.googleapis.com" AND '
                 'protoPayload.methodName=~"%s"' % MODEL_CALL_METHODS)
    tasks = {}
    for p in projects:
        lo, hi = ranges[p]
        tasks[("inf", p)] = (lambda pr=p, a=lo, b=hi: _logging_project(ctx, pr, log_id, a, b))
        if need_audit:
            tasks[("aud", p)] = (lambda pr=p, a=lo, b=hi: _logging_project(
                ctx, pr, AUDIT_DATA_ACCESS_LOG, a, b, extra_filter=da_filter))
    res = ctx.run(tasks)
    inf, aud, truncated, any_ok = [], [], False, False
    for p in projects:
        ok, val = res.get(("inf", p), (False, None))
        if ok:
            any_ok = True
            entries, trunc = val
            truncated = truncated or trunc
            for e in entries:
                item = parse_vertex_entry(e, header_label=label, long_ctx=long_ctx)
                if item:
                    item["project"] = item["project"] or p
                    item["minute"] = _minute(item["ts"])
                    inf.append(item)
        elif p == ctx.project and isinstance(val, GcpError):
            raise val
        if need_audit:
            ok, val = res.get(("aud", p), (False, None))
            if ok:
                entries, trunc = val
                truncated = truncated or trunc
                for e in entries:
                    item = parse_audit_entry(e)
                    if item:
                        item["project"] = item["project"] or p
                        aud.append(item)
    if not any_ok:
        raise GcpError("unavailable", message="inference_request log unreadable")
    return inf, aud, truncated


def attribute_vertex(inf_rows: list[dict], audit_rows: list[dict], directory: dict):
    """Yields (person|None, attribution, row, counts). Splits unlabelled rows across same-minute audit callers."""
    index: dict[tuple, dict[str, int]] = {}
    for a in audit_rows or []:
        person = identity.person_key(a.get("email"))
        if not person or not a.get("minute") or not a.get("model"):
            continue
        key = (a.get("project"), a["model"], a["minute"])
        bucket = index.setdefault(key, {})
        bucket[person] = bucket.get(person, 0) + int(a.get("requests") or 0)
    for row in inf_rows or []:
        person = resolve_user_label(row.get("user_label"), directory)
        if person:
            yield person, "header_label", row, row["counts"]
            continue
        if identity.service_account_subjects() and identity.is_service_account(row.get("principal")):
            sa = identity.person_key(row.get("principal"))
            if sa:
                yield sa, "log_identity", row, row["counts"]
                continue
        weights: dict[str, int] = {}
        m = row.get("minute")
        if m is not None:
            for delta in (0, -1, 1):
                found = index.get((row.get("project"), row["model"], m + dt.timedelta(minutes=delta)))
                if found:
                    weights = dict(found)
                    break
        if not weights:
            yield None, "unattributed", row, row["counts"]
            continue
        people = sorted(weights)
        w = [weights[pp] for pp in people]
        shares = [dict(by_traffic={}) for _ in people]
        for k in COUNTERS:
            for i, part in enumerate(split_int(int(row["counts"].get(k) or 0), w)):
                shares[i][k] = part
        for t, tr in (row["counts"].get("by_traffic") or {}).items():
            for k in TRAFFIC_COUNTERS:
                for i, part in enumerate(split_int(int(tr.get(k) or 0), w)):
                    shares[i]["by_traffic"].setdefault(t, {kk: 0 for kk in TRAFFIC_COUNTERS})[k] = part
        for k, v in (row["counts"].get("extra") or {}).items():
            for i, part in enumerate(split_int(int(v or 0), w)):
                if part:
                    shares[i].setdefault("extra", {})[k] = part
        for pp, share in zip(people, shares):
            yield pp, "audit_correlated", row, share


def user_directory(ctx, extra: set | None = None) -> dict:
    """{uid: person_key} for every person the API knows (usage, caps rules, blocks, model rules)."""
    out: dict[str, str] = {}
    try:
        doc, _ = ctx.store.load("user_directory")
        out.update({k: v for k, v in (doc.get("uids") or {}).items() if isinstance(v, str)})
        caps_doc, _ = ctx.store.load("user_caps")
        for section in ("rules", "blocks", "suspensions"):
            for em in (caps_doc.get(section) or {}):
                if identity.is_person_key(em):
                    out[identity.uid_for(em)] = em
    except Exception:  # pylint: disable=broad-except
        pass
    for em in extra or ():
        if identity.is_person_key(em):
            out[identity.uid_for(em)] = em
    return out


def remember_people(ctx, people) -> None:
    new = {identity.uid_for(p): p for p in people or () if identity.is_person_key(p)}
    if not new:
        return
    try:
        doc, _ = ctx.store.load("user_directory")
        known = doc.get("uids") or {}
        if all(known.get(k) == v for k, v in new.items()):
            return

        def save(d):
            uids = dict(d.get("uids") or {})
            uids.update(new)
            if len(uids) > 200000:
                uids = dict(list(uids.items())[-200000:])
            d["uids"] = uids
            return d
        ctx.store.mutate("user_directory", save)
    except Exception:  # pylint: disable=broad-except
        LOG.debug("user directory update skipped")


def _vertex_audit_only(ctx, audit_rows: list[dict], *, email: str | None, rng) -> UsageResult:
    usage = UsageResult("audit_only")
    tz = timeutil.get_zone(ctx.tz_name)
    for r in audit_rows:
        person = identity.person_key(r.get("email"))
        if not person or (email and person != email):
            continue
        usage.records += r["requests"]
        counts = token_counts(prompt=0, cand=0, cached=0, total=0, requests=r["requests"])
        usage.add_counts(person, counts, surface="gemini_platform", project=r.get("project") or ctx.project,
                         model=r.get("model") or "unspecified", publisher=r.get("publisher"), ts=r.get("ts"))
        _add_daily_ts(usage, person, r.get("ts"), rng, tz, r["requests"], 0)
    usage.token_sources["gemini_platform"] = "requests_only"
    usage.ok = True
    return usage


def _fetch_vertex_raw(ctx, start: dt.datetime, end: dt.datetime, *,
                      fresh: bool = False) -> tuple[list[dict] | None, list[dict] | None, bool]:
    """Fetches raw Gemini Platform inference request rows and Data Access audit rows (I/O only)."""
    mode = (ctx.config.get("caps") or {}).get("usage_source") or "auto"
    inf_rows = audit_rows = None
    truncated = False
    src = None
    bq_ok = False
    if mode in ("auto", "bigquery"):
        src = ctx.audit_source(VERTEX_INFERENCE_SINK, optional=True)
        dsrc = ctx.audit_source(AUDIT_DATA_ACCESS, optional=True)
        tasks = {}
        if src is not None:
            tasks["inf"] = lambda: _bq_vertex_inference(ctx, src, start, end)
        if dsrc is not None:
            tasks["aud"] = lambda: _bq_vertex_audit(ctx, dsrc, start, end)
        res = ctx.run(tasks) if tasks else {}
        if "inf" in res:
            if res["inf"][0]:
                inf_rows = res["inf"][1]
                bq_ok = True
                ctx.add_source(src.label)
            else:
                ctx.warn_error(src.label, res["inf"][1], "Gemini Platform inference request logs")
        if "aud" in res:
            if res["aud"][0] and res["aud"][1] is not None:
                audit_rows = res["aud"][1]
                ctx.add_source(dsrc.label)
            elif not res["aud"][0]:
                ctx.warn_error(dsrc.label, res["aud"][1], "Gemini Platform Data Access audit logs")
    if (inf_rows is None or (bq_ok and not inf_rows and mode == "auto")) and mode in ("auto", "logging"):
        try:
            log_inf, log_audit, truncated = _logging_vertex(ctx, start, end, need_audit=(audit_rows is None))
            if log_inf or inf_rows is None:
                inf_rows = log_inf
            audit_rows = audit_rows if audit_rows is not None else log_audit
            ctx.add_source("api:logging.entries")
        except GcpError as err:
            if mode == "logging":
                ctx.warn_error("api:logging.entries", err, "Gemini Platform inference request logs")
            if not bq_ok:
                inf_rows = None
    elif bq_ok and inf_rows and mode == "auto":
        # Merge Cloud Logging for (a) projects whose per-project BQ table doesn't exist yet (e.g. newly onboarded)
        # and (b) fresh=True tail entries not yet flushed by the BigQuery streaming sink.
        audit_proj = ((ctx.config.get("workspace") or {}).get("audit_project_id") or "").strip()
        bq_covered = set(ctx.projects) if audit_proj else {s.project for s in _sources_of(src)}
        last_by_proj: dict[str, dt.datetime] = {}
        for r in inf_rows:
            rp = r.get("project")
            rt = r.get("log_ts") or r.get("ts")
            if isinstance(rp, str) and isinstance(rt, dt.datetime):
                if rp not in last_by_proj or rt > last_by_proj[rp]:
                    last_by_proj[rp] = rt
        tail_ranges: dict[str, tuple[dt.datetime, dt.datetime]] = {}
        for p in ctx.projects:
            if p not in bq_covered:
                tail_ranges[p] = (start, end)
            elif fresh:
                last = last_by_proj.get(p)
                lo = (last + dt.timedelta(microseconds=1)) if (last and last >= start) else start
                if lo < end:
                    tail_ranges[p] = (lo, end)
        if tail_ranges:
            try:
                tail_inf, tail_aud, tail_trunc = _logging_vertex(
                    ctx, start, end, need_audit=(audit_rows is None), ranges=tail_ranges,
                )
                if tail_inf:
                    inf_rows.extend(tail_inf)
                    ctx.add_source("api:logging.entries")
                if audit_rows is None and tail_aud:
                    audit_rows = tail_aud
                truncated = truncated or tail_trunc
            except GcpError:
                pass
    return inf_rows, audit_rows, truncated


def _build_vertex_usage(ctx, inf_rows: list[dict] | None, audit_rows: list[dict] | None, truncated: bool, *,
                        email: str | None = None, rng=None, directory: dict | None = None) -> UsageResult:
    """Attributes raw Gemini Platform inference/audit rows into a UsageResult (CPU-only)."""
    if not inf_rows:
        # No inference request entries while audit logs show Gemini Platform calls -> logging is not enabled / routed.
        if audit_rows:
            ctx.warn("gemini_platform", "inference_logs_missing",
                     "Gemini Platform inference request metadata logging is not enabled or not routed: per-user request "
                     "counts come from Data Access audit logs but token counts and costs are unavailable. Run "
                     "POST /v1/projects/{project}/vertex-logging/enable or scripts/prepare.sh.")
            return _vertex_audit_only(ctx, audit_rows, email=email, rng=rng)
        if inf_rows is None:
            out = UsageResult("unavailable")
            out.token_sources["gemini_platform"] = "unavailable"
            return out
        empty = UsageResult("vertex")
        empty.token_sources["gemini_platform"] = "logs"
        empty.ok = True
        return empty
    directory = directory if directory is not None else user_directory(
        ctx, {identity.person_key(a.get("email")) for a in (audit_rows or []) if a.get("email")})
    usage = UsageResult("vertex")
    usage.truncated = truncated
    tz = timeutil.get_zone(ctx.tz_name)
    for person, kind, row, counts in attribute_vertex(inf_rows, audit_rows or [], directory):
        usage.records += int(counts.get("requests") or 0)
        proj = row.get("project") or ctx.project
        if person is None:
            usage.add_unattributed(proj, row["model"], counts, surface="gemini_platform", publisher=row.get("publisher"),
                                   ts=row.get("ts"), reason="service_account" if row.get("principal") else
                                   "no_user_identity")
            continue
        if email and person != email:
            continue
        usage.add_counts(person, counts, surface="gemini_platform", project=proj, model=row["model"],
                         publisher=row.get("publisher"), ts=row.get("ts"), attribution=kind)
        _add_daily_ts(usage, person, row.get("ts"), rng, tz, int(counts.get("requests") or 0),
                      int(counts.get("total_tokens") or 0))
    usage.token_sources["gemini_platform"] = "logs"
    usage.ok = True
    return usage


def vertex_usage(ctx, start: dt.datetime, end: dt.datetime, *, email: str | None = None, rng=None,
                 fresh: bool = False, directory: dict | None = None) -> UsageResult:
    """Gemini Platform usage with tokens from inference request logs and per-user attribution (see module docstring)."""
    inf_rows, audit_rows, truncated = _fetch_vertex_raw(ctx, start, end, fresh=fresh)
    return _build_vertex_usage(ctx, inf_rows, audit_rows, truncated, email=email, rng=rng, directory=directory)


# ---------------------------------------------------------------------------
# Gemini Enterprise activity (per-seat licensed: requests + model when the log carries it)
# ---------------------------------------------------------------------------

def _bq_ge_one(ctx, src, is_activity: bool, start: dt.datetime, end: dt.datetime, *,
               email: str | None = None, rng=None) -> UsageResult:
    s = src.schema
    user_col = (expr("t", s, "jsonPayload.useriamprincipal", "jsonPayload.userIamPrincipal", cast="STRING")
                if is_activity else expr("t", s, "jsonPayload.user", cast="STRING"))
    if not user_col:
        return UsageResult("unavailable")
    model_col = expr("t", s, "jsonPayload.model", "jsonPayload.modelId", "jsonPayload.model_id",
                     "jsonPayload.modelName", "jsonPayload.llmModel", cast="STRING")
    lname = expr("t", s, "logName", cast="STRING")
    proj_sql = "REGEXP_EXTRACT(%s, r'^projects/([^/]+)/')" % lname if lname else "CAST(NULL AS STRING)"
    ts = "t.%s" % quote_ident(src.time_column())
    where = "email IS NOT NULL AND %s" % human_sql("email")
    params: dict = _window_params(start, end)
    if email:
        where += " AND email = @email"
        params["email"] = email.lower()
    base = (
        "WITH %s,\ng AS (SELECT %s AS ts, LOWER(%s) AS email, %s AS model, %s AS project FROM ge_src t)"
        % (src.cte("ge_src", start="w_start", end="w_end"), ts, user_col, model_col or "CAST(NULL AS STRING)",
           proj_sql)
    )
    sql = base + "\nSELECT email, model, project, COUNT(*) AS requests, MAX(ts) AS last_ts FROM g WHERE %s GROUP BY email, model, project" % where
    usage = UsageResult("bigquery")
    rows = ctx.query(sql, params, "usage")
    for r in rows:
        person = identity.person_key(r.get("email"))
        if not person:
            continue
        req = to_int(r.get("requests"))
        usage.records += req
        pub, model = clean_model(r.get("model"))
        usage.add_record(
            person, surface="gemini_enterprise", project=r.get("project") or src.project,
            model=model, publisher=pub, requests=req, total=0, prompt=0, cand=0, cached=0, ts=r.get("last_ts"),
        )
    if rng is not None:
        from .base import bucket_sql
        b_params = dict(params, tz=rng.tz_name, start_date=rng.start, bucket_days=rng.bucket_days)
        b_sql = base + "\nSELECT %s AS bucket, email, COUNT(*) AS requests FROM g WHERE %s GROUP BY bucket, email" % (bucket_sql("ts"), where)
        try:
            for r in ctx.query(b_sql, b_params, "usage"):
                person = identity.person_key(r.get("email"))
                b = r.get("bucket")
                if isinstance(b, dt.datetime):
                    b = b.date()
                if person and isinstance(b, dt.date):
                    usage.add_daily(person, b, requests=to_int(r.get("requests")), tokens=0)
        except GcpError:
            pass
    usage.token_sources["gemini_enterprise"] = "requests_only"
    usage.ok = True
    return usage


def _gemini_enterprise_usage(ctx, start: dt.datetime, end: dt.datetime, *, email: str | None = None, rng=None) -> UsageResult:
    mode = (ctx.config.get("caps") or {}).get("usage_source") or "auto"
    if mode == "logging":
        return UsageResult("unavailable")
    asrc = ctx.audit_source(GE_ACTIVITY, optional=True)
    msrc = ctx.audit_source(GE_MESSAGE, optional=True)
    # Prefer GE_ACTIVITY per project; fall back to GE_MESSAGE for projects that only have message logs.
    chosen: list[tuple] = []
    seen_projects: set[str] = set()
    for one in _sources_of(asrc):
        chosen.append((one, True))
        seen_projects.add(one.project)
    for one in _sources_of(msrc):
        if one.project not in seen_projects:
            chosen.append((one, False))
            seen_projects.add(one.project)
    if not chosen:
        return UsageResult("unavailable")
    if len(chosen) == 1:
        one, is_act = chosen[0]
        try:
            res = _bq_ge_one(ctx, one, is_act, start, end, email=email, rng=rng)
            if res.ok:
                ctx.add_source(one.label)
            return res
        except GcpError as err:
            ctx.warn_error(one.label, err, "Gemini Enterprise activity logs")
            return UsageResult("bigquery")
    tasks = {one.label: (lambda s=one, a=is_act: _bq_ge_one(ctx, s, a, start, end, email=email, rng=rng))
             for one, is_act in chosen}
    res_map = ctx.run(tasks)
    merged = UsageResult("bigquery")
    any_ok = False
    for one, _ in chosen:
        ok, val = res_map.get(one.label, (False, None))
        if ok and isinstance(val, UsageResult) and val.ok:
            any_ok = True
            merged.merge(val)
            ctx.add_source(one.label)
        elif not ok:
            ctx.warn_error(one.label, val, "Gemini Enterprise activity logs")
    if any_ok:
        merged.token_sources["gemini_enterprise"] = "requests_only"
        merged.ok = True
    return merged


def _usage_cache_ttl(ctx) -> float:
    srv_ttl = (ctx.config.get("server") or {}).get("cache_ttl_seconds")
    if srv_ttl is None:
        return 120.0
    return max(0.0, float(srv_ttl))


def _collect_usage_uncached(ctx, start: dt.datetime, end: dt.datetime, *, email: str | None = None,
                            rng=None, wanted: set[str] | None = None, fresh: bool = False) -> UsageResult:
    """Fetches all requested AI surfaces in parallel, then attributes Gemini Platform header labels."""
    wanted = set(wanted) if wanted is not None else set(config.VALID_SERVICES)
    tasks = {}
    if "antigravity" in wanted:
        tasks["antigravity"] = lambda: antigravity_usage(ctx, start, end, email=email, rng=rng, fresh=fresh)
    if "gemini_enterprise" in wanted:
        tasks["gemini_enterprise"] = lambda: _gemini_enterprise_usage(ctx, start, end, email=email, rng=rng)
    if "gemini_platform" in wanted:
        tasks["gemini_platform"] = lambda: _fetch_vertex_raw(ctx, start, end, fresh=fresh)
    res = ctx.run(tasks)
    combined = UsageResult("combined")
    any_ok = False
    for name in ("antigravity", "gemini_enterprise"):
        if name not in res:
            continue
        ok, val = res[name]
        if ok and isinstance(val, UsageResult) and val.ok:
            any_ok = True
            combined.merge(val)
    if "gemini_platform" in res:
        ok, val = res["gemini_platform"]
        if ok:
            inf_rows, audit_rows, truncated = val
            extra_people = set(combined.users) | {
                identity.person_key(a.get("email")) for a in (audit_rows or []) if a.get("email")
            }
            vx = _build_vertex_usage(
                ctx, inf_rows, audit_rows, truncated,
                email=email, rng=rng, directory=user_directory(ctx, extra_people),
            )
            if vx.ok:
                any_ok = True
                combined.merge(vx)
            else:
                combined.token_sources.update(vx.token_sources)
        else:
            ctx.warn_error("gemini_platform", val, "Gemini Platform usage")
    combined.ok = any_ok
    combined.computed_at = dt.datetime.now(dt.timezone.utc)
    if any_ok and not email:
        remember_people(ctx, combined.users.keys())
        try:
            from . import model_registry
            model_registry.record_observed(ctx, combined)
        except Exception as err:  # pylint: disable=broad-except
            LOG.debug("model observation skipped: %s", type(err).__name__)
    return combined


def collect_usage(ctx, start: dt.datetime, end: dt.datetime, *, email: str | None = None,
                  rng=None, surface: str | None = None, fresh: bool = False,
                  surfaces: list[str] | tuple[str, ...] | None = None) -> UsageResult:
    """Collects unified usage across Antigravity, Gemini Platform and Gemini Enterprise (logs only).

    Uses stampede-coalesced TTL caching (`server.cache_ttl_seconds`) when `fresh=False`; `fresh=True`
    bypasses the cache, merges live Cloud Logging tail entries, and refreshes the cached snapshot.
    """
    wanted = set(surfaces) if surfaces else set(config.VALID_SERVICES)
    if surface not in (None, "", "all"):
        wanted &= {surface}
    cache = getattr(ctx.app, "cache", None)
    ttl = _usage_cache_ttl(ctx)
    start_m = _minute(start)
    end_m = _minute(end)
    ckey = (
        "usage",
        ctx.version,
        tuple(ctx.projects),
        start_m.isoformat() if start_m else "",
        end_m.isoformat() if end_m else "",
        email or "",
        tuple(sorted(wanted)),
        (rng.start.isoformat(), rng.end.isoformat(), rng.granularity, rng.tz_name) if rng is not None else None,
    )

    def compute():
        sub = ctx.child()
        usage = _collect_usage_uncached(sub, start, end, email=email, rng=rng, wanted=wanted, fresh=fresh)
        return usage, set(sub.sources), list(sub.warnings)

    if cache is not None and ttl > 0:
        if fresh:
            usage, sources, warnings = compute()
            if usage.ok:
                cache.set(ckey, (usage, sources, warnings), ttl)
        else:
            (usage, sources, warnings), _ = cache.get_or_compute(
                ckey, ttl, compute, cacheable=lambda item: bool(item[0].ok),
            )
    else:
        usage, sources, warnings = compute()

    for s in sources:
        ctx.add_source(s)
    for w in warnings:
        ctx.warn(w["source"], w["code"], w["message"])
    return copy.deepcopy(usage)


# ---------------------------------------------------------------------------
# Public REST API handlers: GET /v1/usage/users & GET /v1/usage/users/{email} & GET /v1/users/{email}/projects
# ---------------------------------------------------------------------------

def _public_counts(rec: dict) -> dict:
    return {k: int(rec.get(k) or 0) for k in PUBLIC_COUNTERS}


def traffic_view(rec: dict) -> dict:
    return {t: {k: int(v.get(k) or 0) for k in TRAFFIC_COUNTERS} for t, v in sorted((rec.get("by_traffic") or {}).items())}


def model_rows(by_model: dict, pricing: pricing_svc.Pricing) -> list[dict]:
    out = []
    for model_id, mrec in sorted((by_model or {}).items()):
        m_cost, m_partial = pricing_svc.estimate_cost({"by_model": {model_id: mrec}}, pricing)
        row = {"model": model_id, "publisher": mrec.get("publisher"), "surface": mrec.get("surface") or "antigravity"}
        row.update(_public_counts(mrec))
        row.update(estimated_cost=m_cost, cost_partial=m_partial, by_traffic=traffic_view(mrec),
                   cost_basis=pricing_svc.cost_basis(model_id, mrec, pricing))
        out.append(row)
    out.sort(key=lambda r: (-(r["estimated_cost"] or -1.0), -r["total_tokens"], -r["requests"], r["model"]))
    return out


def _format_user_row(email: str, rec: dict, pricing: pricing_svc.Pricing) -> dict:
    est_cost, partial = pricing_svc.estimate_cost(rec, pricing)
    by_project_list = []
    for proj, prec in sorted((rec.get("by_project") or {}).items()):
        p_cost, p_partial = pricing_svc.estimate_cost(prec, pricing)
        row = {"project": proj}
        row.update(_public_counts(prec))
        row.update(estimated_cost=p_cost, cost_partial=p_partial,
                   models=sorted((prec.get("by_model") or {}).keys()))
        by_project_list.append(row)
    out = {
        "email": email,
        "uid": identity.uid_for(email),
        "principal_type": ("workforce" if identity.is_workforce_key(email) else
                           "service_account" if identity.is_service_account(email) else "user"),
        "surfaces": sorted((rec.get("by_surface") or {}).keys()),
        "projects": [p["project"] for p in by_project_list],
        "multi_project": len(by_project_list) > 1,
    }
    out.update(_public_counts(rec))
    out.update({
        "estimated_cost": est_cost,
        "cost_partial": partial,
        "currency": pricing.currency if est_cost is not None else None,
        "by_model": model_rows(rec.get("by_model"), pricing),
        "by_surface": {s: _public_counts(v) for s, v in sorted((rec.get("by_surface") or {}).items())},
        "by_project": by_project_list,
        "last_active": iso(rec.get("last_active")),
    })
    return out


DATA_SETTLE_DELAY = dt.timedelta(minutes=15)


def freshness(usage: UsageResult) -> dict:
    """data_refreshed_at = when the logs were read; data_complete_through = start of the hour before which Cloud
    Logging / BigQuery sink delivery is treated as settled (heuristic 15-minute delivery delay)."""
    at = usage.computed_at if isinstance(usage.computed_at, dt.datetime) else None
    settled = (at - DATA_SETTLE_DELAY).replace(minute=0, second=0, microsecond=0) if at else None
    return {"data_refreshed_at": iso(at), "data_complete_through": iso(settled)}


def usage_meta(usage: UsageResult) -> dict:
    return {
        **freshness(usage),
        "attribution": dict(usage.attribution),
        "token_sources": dict(usage.token_sources),
        "unattributed_projects": sorted(usage.unattributed.keys()),
        "telemetry_ok": usage.ok,
        "truncated": usage.truncated,
    }


def list_user_usage(ctx) -> dict:
    """GET /v1/usage/users"""
    rng = ctx.range or timeutil.parse_range(ctx.params, ctx.tz_name, ctx.ws.get("default_range_days", 14))
    surface = config.normalize_service(ctx.params.get("surface") or "")
    if surface and surface not in ("all", *config.VALID_SERVICES):
        raise ApiError("bad_request", "Parameter 'surface' must be one of: all, %s." % ", ".join(config.VALID_SERVICES))
    email_f = (ctx.params.get("email") or "").strip().lower()
    if email_f and not identity.is_person_key(email_f):
        raise ApiError("bad_request", "Parameter 'email' must be a valid email or workforce principal.")
    model_f = (ctx.params.get("model") or "").strip().lower()

    from . import caps as caps_svc
    policy = caps_svc.effective_policy(ctx)
    pricing = pricing_svc.resolve_pricing(ctx, policy)
    fresh = str(ctx.params.get("fresh") or "").lower() in ("1", "true")
    usage = collect_usage(ctx, rng.start_ts(), rng.end_ts(), email=email_f or None, surface=surface or None, fresh=fresh)
    rows = [_format_user_row(mail, rec, pricing) for mail, rec in usage.users.items()]
    if model_f:
        rows = [r for r in rows if any(m["model"] == model_f for m in r["by_model"])]
    include = {p.strip().lower() for p in str(ctx.params.get("include") or "").split(",") if p.strip()}
    if "groups" in include:
        from . import groups as groups_svc
        all_groups = groups_svc.load_groups(ctx)
        mem = groups_svc.membership(all_groups)
        for r in rows:
            r["groups"] = [{"id": gid, "name": (all_groups.get(gid) or {}).get("name") or gid}
                           for gid in mem.get(r["email"], [])]
    rows.sort(key=lambda r: (-(r["estimated_cost"] if r["estimated_cost"] is not None else -1.0),
                             -r["total_tokens"], -r["requests"], r["email"]))
    page = paginate_list(rows, ctx.params, default_limit=100, max_limit=1000)
    total_cost = [r["estimated_cost"] for r in rows if r["estimated_cost"] is not None]
    un_cost = [pricing_svc.estimate_cost(p, pricing)[0] for p in usage.unattributed.values()]
    page["range"] = rng.meta()
    page["currency"] = pricing.currency if total_cost else policy.get("currency", "USD")
    page["pricing_source"] = pricing_svc.source_summary(pricing)
    page["summary"] = {
        "active_users": len(rows),
        "multi_project_users": sum(1 for r in rows if r["multi_project"]),
        "total_requests": sum(r["requests"] for r in rows),
        "total_tokens": sum(r["total_tokens"] for r in rows),
        "total_estimated_cost": round(sum(total_cost), 6) if total_cost else None,
        "unattributed_requests": sum(int(p.get("requests") or 0) for p in usage.unattributed.values()),
        "unattributed_tokens": sum(int(p.get("total_tokens") or 0) for p in usage.unattributed.values()),
        "unattributed_estimated_cost": round(sum(c for c in un_cost if c), 6) if any(un_cost) else None,
    }
    page["telemetry"] = usage_meta(usage)
    page.update(freshness(usage))
    return page


def get_user_usage(ctx) -> dict:
    """GET /v1/usage/users/{email}"""
    raw_email = (ctx.path_params.get("email") or "").strip()
    email = identity.person_key(raw_email)
    if not email:
        raise ApiError("bad_request", "Path parameter 'email' must be a valid email or workforce principal.")
    rng = ctx.range or timeutil.parse_range(ctx.params, ctx.tz_name, ctx.ws.get("default_range_days", 14))

    from . import caps as caps_svc
    policy = caps_svc.effective_policy(ctx)
    pricing = pricing_svc.resolve_pricing(ctx, policy)
    fresh = str(ctx.params.get("fresh") or "").lower() in ("1", "true")
    usage = collect_usage(ctx, rng.start_ts(), rng.end_ts(), email=email, rng=rng, fresh=fresh)
    rec = usage.users.get(email) or dict(_new_counters(), by_model={}, by_surface={}, by_project={}, last_active=None)
    row = _format_user_row(email, rec, pricing)
    timeseries = []
    for bucket_date in rng.buckets():
        drec = usage.daily.get((email, bucket_date)) or {"requests": 0, "total_tokens": 0}
        timeseries.append({
            "date": bucket_date.isoformat(),
            "requests": drec["requests"],
            "total_tokens": drec["total_tokens"],
        })
    cap_status = caps_svc.get_user_cap_snapshot(ctx, email, fresh=fresh)
    return {
        "object": "user_usage",
        "range": rng.meta(),
        "user": row,
        "timeseries": timeseries,
        "current_cap": cap_status,
        "telemetry": usage_meta(usage),
        **freshness(usage),
    }


def _iam_projects_for(ctx, email: str, projects: list[str]) -> dict:
    """Optional IAM scan: which managed projects grant this person a role directly (user:EMAIL binding)."""
    member = identity.allow_member(email)
    out = {}
    if not member:
        return out
    tasks = {}
    for p in projects:
        tasks[p] = (lambda pr=p: ctx.rest(
            "POST", "https://cloudresourcemanager.googleapis.com/v3/projects/%s:getIamPolicy" % pr,
            json_body={"options": {"requestedPolicyVersion": 3}}, timeout=20))
    res = ctx.run(tasks) if tasks else {}
    for p in projects:
        ok, val = res.get(p, (False, None))
        if not ok:
            out[p] = {"readable": False, "error": getattr(val, "kind", "error"), "roles": []}
            continue
        roles = sorted({b.get("role") for b in (val or {}).get("bindings") or []
                        if member in (b.get("members") or []) and b.get("role")})
        out[p] = {"readable": True, "roles": roles}
    return out


def get_user_projects(ctx) -> dict:
    """GET /v1/users/{email}/projects?start=&end=&include=iam : every managed project the user used or is bound to."""
    email = identity.person_key((ctx.path_params.get("email") or "").strip())
    if not email:
        raise ApiError("bad_request", "Path parameter 'email' must be a valid email or workforce principal.")
    rng = ctx.range or timeutil.parse_range(ctx.params, ctx.tz_name, ctx.ws.get("default_range_days", 30))
    from . import caps as caps_svc
    policy = caps_svc.effective_policy(ctx)
    pricing = pricing_svc.resolve_pricing(ctx, policy)
    usage = collect_usage(ctx, rng.start_ts(), rng.end_ts(), email=email)
    rec = usage.users.get(email) or {"by_project": {}}
    include = {s.strip() for s in (ctx.params.get("include") or "").split(",") if s.strip()}
    managed = list(ctx.all_projects)
    iam = _iam_projects_for(ctx, email, managed[:int((ctx.config.get("quotas") or {}).get("max_projects_per_call") or 50)]) \
        if "iam" in include else {}
    snapshot = caps_svc.get_user_cap_snapshot(ctx, email)
    proj_status = {p["project"]: p for p in snapshot.get("projects") or []}
    names = sorted(set((rec.get("by_project") or {}).keys()) | {p for p, v in iam.items() if v.get("roles")}
                   | set(proj_status.keys()))
    rows = []
    for proj in names:
        prec = (rec.get("by_project") or {}).get(proj) or dict(_new_counters(), by_model={})
        cost, partial = pricing_svc.estimate_cost(prec, pricing)
        row = {"project": proj, "managed": proj in managed, "used": bool(prec.get("requests"))}
        row.update(_public_counts(prec))
        row.update(estimated_cost=cost, cost_partial=partial, models=model_rows(prec.get("by_model"), pricing))
        if proj in iam:
            row["iam"] = iam[proj]
        if proj in proj_status:
            row["cap"] = {k: proj_status[proj].get(k) for k in ("status", "limit", "utilization_pct",
                                                                  "blocked_services", "remaining")}
        rows.append(row)
    return {"object": "user_projects", "email": email, "uid": identity.uid_for(email), "range": rng.meta(),
            "multi_project": sum(1 for r in rows if r["used"] or (r.get("iam") or {}).get("roles")) > 1,
            "currency": pricing.currency, "data": rows, "telemetry": usage_meta(usage)}
