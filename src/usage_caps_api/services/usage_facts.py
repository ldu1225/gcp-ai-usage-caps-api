"""Ledger-style usage facts and daily JSONL exports (v1.4.0).

GET /v1/usage/facts                          hour/day buckets x any combination of dimensions, upsert-safe `fact_id`
GET /v1/usage/exports                        one export per UTC day (list -> download, like a file-based cost feed)
GET /v1/usage/exports/{export_id}/download   JSONL, one hourly fact per line at the finest grain

Same sources and attribution as /v1/usage/users (logs only; cost = log token counts x official SKU list price or
contract override). Dimensions below come straight from fields present in the logs:

- surface / project / model / traffic   all surfaces (traffic = Gemini Platform `trafficType`)
- location                              `resource.labels.location` (all surfaces)
- client, client_version                Antigravity `labels.client_name` / `labels.client_version`
- app                                   Gemini Platform request label `labels.app`; Gemini Enterprise app (engine) id
- agent                                 Gemini Enterprise `response.agentInfo.agent` (+ request agent id fallbacks)
- reasoning_level                       Antigravity `experience` suffix (`-low|-medium|-high`)
Per-modality tokens, cache-write TTL split and latency come from Gemini Platform `usageMetadata` / `requestDuration`;
`sessions` counts distinct Antigravity `labels.trajectory_id`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re

from .. import identity, timeutil
from ..gcp import GcpError
from ..httpkit import ApiError, RawResponse, paginate_list
from ..schema import expr, quote_ident
from . import pricing as pricing_svc
from . import usage
from .base import AGY_SINK, GE_ACTIVITY, GE_MESSAGE, human_sql, iso, to_int

LOG = logging.getLogger("usage_caps_api.usage_facts")

DIMENSIONS = ("user", "surface", "project", "model", "location", "client", "app", "agent", "traffic", "reasoning_level")
DEFAULT_GROUP_BY = ("user", "surface", "project", "model")
EXPORT_GROUP_BY = DIMENSIONS
GRANULARITIES = {"hour": dt.timedelta(hours=1), "day": dt.timedelta(days=1)}
MAX_WINDOW = {"hour": dt.timedelta(days=31), "day": dt.timedelta(days=400)}
SETTLE_DELAY = dt.timedelta(minutes=15)       # Cloud Logging -> BigQuery sink delivery delay we treat as "not final"
EXPORT_ID_RE = re.compile(r"^usage-facts-hourly-(\d{4}-\d{2}-\d{2})$")
REASONING_RE = re.compile(r"-(minimal|low|medium|high|max)$")
LONG_RENAMES = {"long_requests": "long_context_requests", "long_input_tokens": "long_context_input_tokens",
                "long_output_tokens": "long_context_output_tokens", "long_cached_tokens": "long_context_cached_tokens"}
UTC = dt.timezone.utc


# ---------------------------------------------------------------------------
# Window parsing
# ---------------------------------------------------------------------------

def _floor(ts: dt.datetime, granularity: str) -> dt.datetime:
    ts = ts.astimezone(UTC)
    if granularity == "day":
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    return ts.replace(minute=0, second=0, microsecond=0)


def _ceil(ts: dt.datetime, granularity: str) -> dt.datetime:
    fl = _floor(ts, granularity)
    return fl if fl == ts.astimezone(UTC) else fl + GRANULARITIES[granularity]


def _parse_time(params: dict, key: str) -> dt.datetime | None:
    raw = (params.get(key) or "").strip()
    if not raw:
        return None
    val = timeutil.parse_rfc3339(raw)
    if val is None:
        raise ApiError("bad_request", "Parameter '%s' must be an RFC 3339 timestamp (e.g. 2026-11-27T00:00:00Z)." % key)
    return val.astimezone(UTC)


def _parse_date(params: dict, key: str) -> dt.date | None:
    raw = (params.get(key) or "").strip()
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        raise ApiError("bad_request", "Parameter '%s' must be a date in YYYY-MM-DD format." % key)


def parse_window(params: dict, granularity: str, now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    """[start, end) in UTC, aligned to bucket boundaries. start_time/end_time (RFC 3339, end exclusive) win over
    start/end (dates, end inclusive). Default: the last 7 UTC days including today."""
    now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    start, end = _parse_time(params, "start_time"), _parse_time(params, "end_time")
    if start is None and end is None:
        d0, d1 = _parse_date(params, "start"), _parse_date(params, "end")
        if d1 is None:
            d1 = now.date()
        if d0 is None:
            d0 = d1 - dt.timedelta(days=6)
        start = dt.datetime.combine(d0, dt.time.min, tzinfo=UTC)
        end = dt.datetime.combine(d1 + dt.timedelta(days=1), dt.time.min, tzinfo=UTC)
    else:
        end = end or now
        start = start or (end - dt.timedelta(days=1))
    start, end = _floor(start, granularity), _ceil(end, granularity)
    if end <= start:
        raise ApiError("bad_request", "The time window is empty (end must be after start).")
    if end - start > MAX_WINDOW[granularity]:
        raise ApiError("bad_request", "The time window is limited to %d days for granularity=%s."
                       % (MAX_WINDOW[granularity].days, granularity))
    return start, end


def parse_group_by(raw: str | None) -> tuple[str, ...]:
    if not raw or not str(raw).strip():
        return DEFAULT_GROUP_BY
    out: list[str] = []
    for part in str(raw).split(","):
        p = part.strip().lower()
        if not p:
            continue
        if p not in DIMENSIONS:
            raise ApiError("bad_request", "Parameter 'group_by' accepts: %s." % ", ".join(DIMENSIONS))
        if p not in out:
            out.append(p)
    return tuple(out) or DEFAULT_GROUP_BY


# ---------------------------------------------------------------------------
# Atoms: one usage slice (hour bucket x all dimensions) from any surface
# ---------------------------------------------------------------------------

def _agent_id(raw) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    return text.rsplit("/", 1)[-1][:200] or None


def _reasoning(experience) -> str | None:
    m = REASONING_RE.search(str(experience or "").strip().lower())
    return m.group(1) if m else None


def _atom(ts, surface: str, person: str | None, attribution: str, counts: dict, *, project=None, model=None,
          publisher=None, location=None, client=None, client_version=None, app=None, agent=None, agent_name=None,
          traffic=None, reasoning=None, sessions=None) -> dict | None:
    if not isinstance(ts, dt.datetime):
        return None
    return {"ts": _floor(ts, "hour"), "surface": surface, "person": person, "attribution": attribution,
            "counts": counts, "project": project, "model": model, "publisher": publisher,
            "location": (str(location).strip().lower() or None) if location else None,
            "client": client or None, "client_version": client_version or None, "app": app or None,
            "agent": agent or None, "agent_name": agent_name or None, "traffic": traffic,
            "reasoning_level": reasoning, "sessions": set(sessions or ())}


def _agy_bq_one(ctx, src, start, end) -> list[dict]:
    s = src.schema
    f = usage.agy_fields(src)
    if not f["user"]:
        raise GcpError("invalid", message="sink table lacks labels.user_id")
    null = "CAST(NULL AS STRING)"
    lname = expr("t", s, "logName", cast="STRING")
    cols = {
        "location": expr("t", s, "resource.labels.location", cast="STRING"),
        "client_version": expr("t", s, "labels.client_version", cast="STRING"),
        "experience": expr("t", s, *usage.payload_paths(s, "experience"), cast="STRING"),
        "trajectory": expr("t", s, "labels.trajectory_id", cast="STRING"),
    }
    prompt_sql = "IFNULL(%s, 0) + IFNULL(%s, 0)" % (f["prompt"] or "0", f["tool_prompt"] or "0")
    sql = (
        "WITH %s,\nu AS (SELECT %s AS ts, %s AS email, %s AS principal, %s AS model, IFNULL(%s, 0) AS total, "
        "%s AS prompt, IFNULL(%s, 0) AS cand, IFNULL(%s, 0) AS cached, IFNULL(%s, 0) AS thoughts, %s AS project, "
        "%s AS client, %s AS client_version, LOWER(%s) AS location, %s AS experience, %s AS trajectory "
        "FROM s t WHERE %s IS NOT NULL)\n"
        "SELECT TIMESTAMP_TRUNC(ts, HOUR) AS hour, email, model, project, client, client_version, location, experience, "
        "COUNT(*) AS requests, SUM(total) AS tokens, SUM(prompt) AS prompt_tokens, SUM(cand) AS candidate_tokens, "
        "SUM(cached) AS cached_tokens, SUM(thoughts) AS thoughts_tokens, COUNTIF(prompt > @long_ctx) AS long_requests, "
        "SUM(IF(prompt > @long_ctx, prompt, 0)) AS long_input_tokens, "
        "SUM(IF(prompt > @long_ctx, GREATEST(total - prompt, cand + thoughts, 0), 0)) AS long_output_tokens, "
        "SUM(IF(prompt > @long_ctx, cached, 0)) AS long_cached_tokens, ANY_VALUE(principal) AS principal, "
        "ARRAY_AGG(DISTINCT trajectory IGNORE NULLS LIMIT 5000) AS sessions, MAX(ts) AS last_ts\n"
        "FROM u GROUP BY hour, email, model, project, client, client_version, location, experience"
        % (src.cte("s", start="w_start", end="w_end"), f["ts"], f["user"], f["principal"] or null, f["model"] or null,
           f["total"] or "0", prompt_sql, f["candidates"] or "0", f["cached"] or "0", f["thoughts"] or "0",
           "REGEXP_EXTRACT(%s, r'^projects/([^/]+)/')" % lname if lname else null, f["client"] or null,
           cols["client_version"] or null, cols["location"] or null, cols["experience"] or null,
           cols["trajectory"] or null, f["user"])
    )
    rows = ctx.query(sql, dict(usage._window_params(start, end), long_ctx=usage._long_threshold(ctx)), "usage")  # pylint: disable=protected-access
    out = []
    for r in rows:
        person = identity.person_key(r.get("email"))
        if person is None:
            continue
        pub, model = usage.clean_model(r.get("model"))
        counts = usage.token_counts(prompt=to_int(r.get("prompt_tokens")), cand=to_int(r.get("candidate_tokens")),
                                    cached=to_int(r.get("cached_tokens")), total=to_int(r.get("tokens")),
                                    thoughts=to_int(r.get("thoughts_tokens")), requests=to_int(r.get("requests")),
                                    long={k: to_int(r.get(k)) for k in usage.LONG_COUNTERS})
        a = _atom(r.get("hour") or r.get("last_ts"), "antigravity", person, "log_identity", counts,
                  project=r.get("project") or src.project, model=model or "unspecified", publisher=pub,
                  location=r.get("location"), client=r.get("client"), client_version=r.get("client_version"),
                  reasoning=_reasoning(r.get("experience")), sessions=r.get("sessions") or ())
        if a:
            out.append(a)
    return out


def _agy_logging(ctx, ranges: dict) -> tuple[list[dict], bool]:
    log_id = usage._agy_log_id(ctx)  # pylint: disable=protected-access
    long_ctx = usage._long_threshold(ctx)  # pylint: disable=protected-access
    res = ctx.run({p: (lambda pr=p: usage._logging_project(ctx, pr, log_id, ranges[pr][0], ranges[pr][1]))  # pylint: disable=protected-access
                   for p in ranges})
    out, truncated = [], False
    for p in ranges:
        ok, val = res.get(p, (False, None))
        if not ok:
            if p == ctx.project and isinstance(val, GcpError):
                raise val
            continue
        entries, trunc = val
        truncated = truncated or trunc
        for e in entries:
            item = usage.parse_agy_entry(e, long_ctx)
            if not item or item["person"] is None:
                continue
            labels = e.get("labels") if isinstance(e.get("labels"), dict) else {}
            payload = e.get("jsonPayload") if isinstance(e.get("jsonPayload"), dict) else {}
            res_labels = ((e.get("resource") or {}).get("labels") or {}) if isinstance(e.get("resource"), dict) else {}
            traj = labels.get("trajectory_id")
            a = _atom(item["ts"], "antigravity", item["person"], "log_identity", item["counts"],
                      project=item["project"] or p, model=item["model"], publisher=item["publisher"],
                      location=res_labels.get("location"), client=labels.get("client_name"),
                      client_version=labels.get("client_version"), reasoning=_reasoning(payload.get("experience")),
                      sessions=[traj] if traj else ())
            if a:
                out.append(a)
    return out, truncated


def antigravity_atoms(ctx, start, end, *, fresh: bool = False) -> tuple[list[dict], bool, str]:
    """(atoms, truncated, token_source) - BigQuery sink first (with Cloud Logging tail when fresh), else Cloud Logging."""
    mode = (ctx.config.get("caps") or {}).get("usage_source") or "auto"
    src = ctx.audit_source(AGY_SINK, optional=True) if mode in ("auto", "bigquery") else None
    if src is not None:
        parts = usage._sources_of(src)  # pylint: disable=protected-access
        res = ctx.run({s.label: (lambda one=s: _agy_bq_one(ctx, one, start, end)) for s in parts})
        atoms, any_ok = [], False
        for s in parts:
            ok, val = res.get(s.label, (False, None))
            if ok:
                any_ok = True
                atoms.extend(val)
                ctx.add_source(s.label)
            else:
                ctx.warn_error(s.label, val, "Antigravity BigQuery usage facts")
        if any_ok:
            if mode == "auto":
                last: dict[str, dt.datetime] = {}
                for a in atoms:
                    if a["project"] and (a["project"] not in last or a["ts"] > last[a["project"]]):
                        last[a["project"]] = a["ts"]
                # Cloud Logging for (a) managed projects without a per-project sink table yet (always, full window) and
                # (b) when fresh, the newest hour of covered projects (hour-floored atoms: drop the BigQuery copy).
                covered = set(ctx.projects) if ctx.central_audit_project else {s.project for s in parts}
                tail = {}
                for p in ctx.projects:
                    if p not in covered:
                        tail[p] = (start, end)
                        continue
                    if not fresh:
                        continue
                    lo = max(start, last[p]) if p in last else start
                    if lo < end:
                        tail[p] = (lo, end)
                if tail:
                    try:
                        extra, _ = _agy_logging(ctx, tail)
                        atoms = [a for a in atoms if not (a["project"] in tail and a["ts"] >= tail[a["project"]][0])]
                        atoms.extend(extra)
                        ctx.add_source("api:logging.entries")
                    except GcpError as err:
                        if any(p not in covered for p in tail):
                            ctx.warn_error("api:logging.entries", err, "Antigravity Cloud Logging usage facts")
            return atoms, False, "logs"
        if mode == "bigquery":
            return [], False, "unavailable"
    if mode in ("auto", "logging"):
        try:
            atoms, truncated = _agy_logging(ctx, {p: (start, end) for p in ctx.projects})
            ctx.add_source("api:logging.entries")
            return atoms, truncated, "logs"
        except GcpError as err:
            ctx.warn_error("api:logging.entries", err, "Antigravity Cloud Logging usage facts")
    return [], False, "unavailable"


def vertex_atoms(ctx, start, end, *, fresh: bool = False) -> tuple[list[dict], bool, str]:
    inf_rows, audit_rows, truncated = usage._fetch_vertex_raw(ctx, start, end, fresh=fresh)  # pylint: disable=protected-access
    atoms: list[dict] = []
    if not inf_rows:
        if audit_rows:
            for r in audit_rows:
                person = identity.person_key(r.get("email"))
                if not person:
                    continue
                counts = usage.token_counts(prompt=0, cand=0, cached=0, total=0, requests=r["requests"])
                a = _atom(r.get("ts") or r.get("minute"), "gemini_platform", person, "log_identity", counts,
                          project=r.get("project") or ctx.project, model=r.get("model") or "unspecified",
                          publisher=r.get("publisher"))
                if a:
                    atoms.append(a)
            return atoms, truncated, "requests_only"
        return atoms, truncated, ("logs" if inf_rows is not None else "unavailable")
    directory = usage.user_directory(ctx, {identity.person_key(a.get("email")) for a in (audit_rows or [])
                                           if a.get("email")})
    for person, kind, row, counts in usage.attribute_vertex(inf_rows, audit_rows or [], directory):
        a = _atom(row.get("minute") or row.get("ts"), "gemini_platform", person, kind, counts,
                  project=row.get("project") or ctx.project, model=row["model"], publisher=row.get("publisher"),
                  location=row.get("location"), app=row.get("app"), traffic=row.get("traffic"))
        if a:
            atoms.append(a)
    return atoms, truncated, "logs"


def _ge_bq_one(ctx, src, is_activity: bool, start, end) -> list[dict]:
    s = src.schema
    user_col = (expr("t", s, "jsonPayload.useriamprincipal", "jsonPayload.userIamPrincipal", cast="STRING")
                if is_activity else expr("t", s, "jsonPayload.user", cast="STRING"))
    if not user_col:
        return []
    null = "CAST(NULL AS STRING)"
    model_col = expr("t", s, "jsonPayload.response.modelInfo.model", "jsonPayload.model", "jsonPayload.modelId",
                     "jsonPayload.model_id", "jsonPayload.modelName", "jsonPayload.llmModel", cast="STRING")
    agent_cols = [c for c in (
        expr("t", s, "jsonPayload.response.agentInfo.agent", cast="STRING"),
        expr("t", s, "resource.labels.agent_id", cast="STRING"),
        expr("t", s, "jsonPayload.request.agentId", cast="STRING"),
        expr("t", s, "jsonPayload.request.agentsSpec.agentSpecs.agentId", cast="STRING"),
        expr("t", s, "jsonPayload.request.userEvent.agentspaceInfo.agentInfo.agentId", cast="STRING"),
    ) if c]
    agent_sql = ("COALESCE(%s)" % ", ".join("NULLIF(%s, '')" % c for c in agent_cols)) if agent_cols else null
    agent_name = expr("t", s, "jsonPayload.response.agentInfo.displayName", "jsonPayload.request.agent.displayName",
                      cast="STRING") or null
    name_cols = [c for c in (expr("t", s, "jsonPayload.logMetadata.name", cast="STRING"),
                             expr("t", s, "jsonPayload.request.name", cast="STRING")) if c]
    app_sql = ("REGEXP_EXTRACT(COALESCE(%s), r'/engines/([^/]+)')" % ", ".join(name_cols)) if name_cols else null
    location = expr("t", s, "resource.labels.location", cast="STRING") or null
    lname = expr("t", s, "logName", cast="STRING")
    proj_sql = "REGEXP_EXTRACT(%s, r'^projects/([^/]+)/')" % lname if lname else null
    sql = (
        "WITH %s,\ng AS (SELECT %s AS ts, LOWER(%s) AS email, %s AS model, %s AS project, %s AS agent, "
        "%s AS agent_name, %s AS app, LOWER(%s) AS location FROM ge_src t)\n"
        "SELECT TIMESTAMP_TRUNC(ts, HOUR) AS hour, email, model, project, agent, ANY_VALUE(agent_name) AS agent_name, "
        "app, location, COUNT(*) AS requests FROM g WHERE email IS NOT NULL AND %s "
        "GROUP BY hour, email, model, project, agent, app, location"
        % (src.cte("ge_src", start="w_start", end="w_end"), "t.%s" % quote_ident(src.time_column()), user_col,
           model_col or null, proj_sql, agent_sql, agent_name, app_sql, location, human_sql("email"))
    )
    out = []
    for r in ctx.query(sql, usage._window_params(start, end), "usage"):  # pylint: disable=protected-access
        person = identity.person_key(r.get("email"))
        if not person:
            continue
        pub, model = usage.clean_model(r.get("model"))
        counts = usage.token_counts(prompt=0, cand=0, cached=0, total=0, requests=to_int(r.get("requests")))
        a = _atom(r.get("hour"), "gemini_enterprise", person, "log_identity", counts,
                  project=r.get("project") or src.project, model=model, publisher=pub, location=r.get("location"),
                  app=r.get("app"), agent=_agent_id(r.get("agent")), agent_name=r.get("agent_name"))
        if a:
            out.append(a)
    return out


def gemini_enterprise_atoms(ctx, start, end) -> tuple[list[dict], str]:
    mode = (ctx.config.get("caps") or {}).get("usage_source") or "auto"
    if mode == "logging":
        return [], "unavailable"
    chosen, seen = [], set()
    for one in usage._sources_of(ctx.audit_source(GE_ACTIVITY, optional=True)):  # pylint: disable=protected-access
        chosen.append((one, True))
        seen.add(one.project)
    for one in usage._sources_of(ctx.audit_source(GE_MESSAGE, optional=True)):  # pylint: disable=protected-access
        if one.project not in seen:
            chosen.append((one, False))
            seen.add(one.project)
    if not chosen:
        return [], "unavailable"
    res = ctx.run({one.label: (lambda s=one, a=act: _ge_bq_one(ctx, s, a, start, end)) for one, act in chosen})
    atoms, any_ok = [], False
    for one, _ in chosen:
        ok, val = res.get(one.label, (False, None))
        if ok:
            any_ok = True
            atoms.extend(val)
            ctx.add_source(one.label)
        else:
            ctx.warn_error(one.label, val, "Gemini Enterprise activity logs")
    return atoms, ("requests_only" if any_ok else "unavailable")


def collect_atoms(ctx, start, end, *, surfaces=None, fresh: bool = False) -> dict:
    wanted = set(surfaces or ("antigravity", "gemini_platform", "gemini_enterprise"))
    tasks = {}
    if "antigravity" in wanted:
        tasks["antigravity"] = lambda: antigravity_atoms(ctx, start, end, fresh=fresh)
    if "gemini_platform" in wanted:
        tasks["gemini_platform"] = lambda: vertex_atoms(ctx, start, end, fresh=fresh)
    if "gemini_enterprise" in wanted:
        tasks["gemini_enterprise"] = lambda: gemini_enterprise_atoms(ctx, start, end)
    res = ctx.run(tasks)
    atoms, token_sources, truncated = [], {}, False
    for name in ("antigravity", "gemini_platform", "gemini_enterprise"):
        if name not in res:
            continue
        ok, val = res[name]
        if not ok:
            ctx.warn_error(name, val, "usage facts")
            token_sources[name] = "unavailable"
            continue
        if name == "gemini_enterprise":
            got, src_kind = val
        else:
            got, trunc, src_kind = val
            truncated = truncated or trunc
        atoms.extend(got)
        token_sources[name] = src_kind
    return {"atoms": atoms, "token_sources": token_sources, "truncated": truncated}


# ---------------------------------------------------------------------------
# Aggregation into facts
# ---------------------------------------------------------------------------

def _dim_value(atom: dict, dim: str):
    if dim == "user":
        return atom["person"]
    return atom.get(dim)


def principal_type(person: str | None) -> str | None:
    if not person:
        return None
    if identity.is_workforce_key(person):
        return "workforce"
    if identity.is_service_account(person):
        return "service_account"
    return "user"


def aggregate(atoms: list[dict], granularity: str, group_by: tuple[str, ...]) -> dict:
    facts: dict[tuple, dict] = {}
    for a in atoms:
        bucket = _floor(a["ts"], granularity)
        key = (bucket,) + tuple(_dim_value(a, d) for d in group_by)
        f = facts.get(key)
        if f is None:
            f = {"bucket": bucket, "dims": {d: _dim_value(a, d) for d in group_by}, "counts": usage._new_counters(),  # pylint: disable=protected-access
                 "extra": {}, "by_model": {}, "sessions": set(), "attribution": {}, "surfaces": set(),
                 "client_versions": set(), "agent_names": set()}
            facts[key] = f
        c = a["counts"]
        usage._add(f["counts"], c)  # pylint: disable=protected-access
        for k, v in (c.get("extra") or {}).items():
            f["extra"][k] = f["extra"].get(k, 0) + int(v or 0)
        req = int(c.get("requests") or 0)
        f["attribution"][a["attribution"]] = f["attribution"].get(a["attribution"], 0) + req
        f["surfaces"].add(a["surface"])
        f["sessions"] |= a["sessions"]
        if a.get("client_version"):
            f["client_versions"].add(a["client_version"])
        if a.get("agent_name"):
            f["agent_names"].add(a["agent_name"])
        mkey = a.get("model") or "unspecified"
        m = f["by_model"].setdefault(mkey, dict(usage._new_counters(), surface=a["surface"],  # pylint: disable=protected-access
                                                publisher=a.get("publisher")))
        usage._add(m, c)  # pylint: disable=protected-access
    return facts


def _revision(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def fact_id(granularity: str, bucket: dt.datetime, dims: dict) -> str:
    key = "v1|%s|%s|%s" % (granularity, iso(bucket), "|".join("%s=%s" % (d, dims[d] if dims[d] is not None else "")
                                                             for d in sorted(dims)))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def format_fact(f: dict, granularity: str, pricing, *, computed_at: str, include_billing: bool) -> dict:
    dims = f["dims"]
    c = f["counts"]
    person = dims.get("user")
    row: dict = {"object": "usage_fact", "fact_id": fact_id(granularity, f["bucket"], dims),
                 "bucket_start": iso(f["bucket"]), "bucket_end": iso(f["bucket"] + GRANULARITIES[granularity]),
                 "granularity": granularity}
    if "user" in dims:
        row.update(email=person, uid=identity.uid_for(person) if person else None,
                   principal_type=principal_type(person))
    for d in DIMENSIONS:
        if d != "user" and d in dims:
            row[d] = dims[d]
    if "client" in dims and f["client_versions"]:
        row["client_versions"] = sorted(f["client_versions"])
    if "agent" in dims and f["agent_names"]:
        row["agent_names"] = sorted(f["agent_names"])
    if "surface" not in dims:
        row["surfaces"] = sorted(f["surfaces"])
    if "model" not in dims:
        row["models"] = sorted(f["by_model"])
    for k in usage.PUBLIC_COUNTERS:
        row[k] = int(c.get(k) or 0)
    row["uncached_input_tokens"] = max(0, row["input_tokens"] - row["cached_tokens"] - row["cache_write_tokens"])
    for k, new in LONG_RENAMES.items():
        row[new] = int(c.get(k) or 0)
    ex = f["extra"]
    row["cache_write_5m_tokens"] = int(ex.get("cache_write_5m_tokens") or 0)
    row["cache_write_1h_tokens"] = int(ex.get("cache_write_1h_tokens") or 0)
    by_mod = {}
    for m in usage.MODALITIES:
        i, o = int(ex.get("%s_input_tokens" % m) or 0), int(ex.get("%s_output_tokens" % m) or 0)
        if i or o:
            by_mod[m] = {"input_tokens": i, "output_tokens": o}
    row["by_modality"] = by_mod
    row["by_traffic"] = usage.traffic_view(c)
    samples = int(ex.get("latency_samples") or 0)
    row["latency_ms_avg"] = int(round(ex.get("latency_ms_sum", 0) / samples)) if samples else None
    row["sessions"] = len(f["sessions"]) if f["sessions"] else None
    row["attribution"] = dict(sorted(f["attribution"].items()))
    priced_models = {m: rec for m, rec in f["by_model"].items() if rec.get("surface") != "gemini_enterprise"}
    bases = sorted({pricing_svc.cost_basis(m, rec, pricing) for m, rec in f["by_model"].items()})
    cost, partial = (pricing_svc.estimate_cost({"by_model": priced_models}, pricing) if priced_models else (None, False))
    row["list_cost"] = cost
    row["cost_partial"] = partial
    row["currency"] = pricing.currency if (pricing is not None and cost is not None) else None
    row["cost_basis"] = bases[0] if len(bases) == 1 else "mixed"
    lines = []
    for m, rec in sorted(priced_models.items()):
        for ln in pricing_svc.cost_lines(m, rec, pricing):
            lines.append(dict(ln, model=m))
    if include_billing:
        row["billing"] = lines
    row["revision"] = _revision({k: v for k, v in row.items() if k not in ("fact_id",)})
    row["computed_at"] = computed_at
    return row


# ---------------------------------------------------------------------------
# Compute (cached) + handlers
# ---------------------------------------------------------------------------

def _filters(ctx) -> dict:
    p = ctx.params
    surface = (p.get("surface") or "").strip().lower()
    if surface in ("", "all"):
        surface = ""
    elif surface not in ("antigravity", "gemini_platform", "gemini_enterprise"):
        raise ApiError("bad_request", "Parameter 'surface' must be one of: all, antigravity, gemini_platform, "
                       "gemini_enterprise.")
    email = (p.get("email") or "").strip().lower()
    if email and not identity.is_person_key(email):
        raise ApiError("bad_request", "Parameter 'email' must be a valid email or workforce principal.")
    return {"surface": surface, "email": email, "project": (p.get("project") or "").strip().lower(),
            "model": (p.get("model") or "").strip().lower()}


def compute_facts(ctx, start, end, granularity: str, group_by: tuple[str, ...], *, filters: dict | None = None,
                  fresh: bool = False, include_billing: bool = True) -> dict:
    filters = filters or {}
    surfaces = [filters["surface"]] if filters.get("surface") else None

    ran = []

    def compute():
        ran.append(True)
        sub = ctx.child()
        got = collect_atoms(sub, start, end, surfaces=surfaces, fresh=fresh)
        return got, set(sub.sources), list(sub.warnings), iso(dt.datetime.now(UTC))

    ckey = ("usage_facts_atoms", ctx.version, tuple(ctx.projects), iso(start), iso(end),
            tuple(sorted(surfaces or ())))
    cache = getattr(ctx.app, "cache", None)
    ttl = usage._usage_cache_ttl(ctx)  # pylint: disable=protected-access
    if cache is not None and ttl > 0 and not fresh:
        (got, sources, warnings, computed_at), _ = cache.get_or_compute(ckey, ttl, compute)
    else:
        got, sources, warnings, computed_at = compute()
        if cache is not None and ttl > 0:
            cache.set(ckey, (got, sources, warnings, computed_at), ttl)
    for s in sources:
        ctx.add_source(s)
    for w in warnings:
        ctx.warn(w["source"], w["code"], w["message"])

    atoms = [a for a in got["atoms"] if start <= a["ts"] < end]
    if filters.get("email"):
        atoms = [a for a in atoms if a["person"] == filters["email"]]
    if filters.get("project"):
        atoms = [a for a in atoms if (a["project"] or "") == filters["project"]]
    if filters.get("model"):
        atoms = [a for a in atoms if (a["model"] or "") == filters["model"]]

    from . import caps as caps_svc
    policy = caps_svc.effective_policy(ctx)
    pricing = pricing_svc.resolve_pricing(ctx, policy)
    facts = aggregate(atoms, granularity, group_by)
    rows = [format_fact(f, granularity, pricing, computed_at=computed_at, include_billing=include_billing)
            for f in facts.values()]
    rows.sort(key=lambda r: (r["bucket_start"], str(r.get("email") or "~"), str(r.get("surface") or ""),
                             str(r.get("project") or ""), str(r.get("model") or ""), r["fact_id"]))
    attribution: dict = {}
    for a in atoms:
        attribution[a["attribution"]] = attribution.get(a["attribution"], 0) + int(a["counts"].get("requests") or 0)
    costs = [r["list_cost"] for r in rows if r["list_cost"] is not None]
    computed_dt = timeutil.parse_rfc3339(computed_at) or dt.datetime.now(UTC)
    return {
        "rows": rows,
        "range": {"start_time": iso(start), "end_time": iso(end), "granularity": granularity, "timezone": "UTC"},
        "group_by": list(group_by),
        "currency": pricing.currency if pricing is not None else None,
        "pricing_source": pricing_svc.source_summary(pricing),
        "data_refreshed_at": computed_at,
        "data_complete_through": iso(min(end, _floor(computed_dt - SETTLE_DELAY, "hour"))),
        "summary": {"facts": len(rows), "total_requests": sum(r["requests"] for r in rows),
                    "total_tokens": sum(r["total_tokens"] for r in rows),
                    "total_list_cost": round(sum(costs), 6) if costs else None},
        "telemetry": {"attribution": attribution, "token_sources": got["token_sources"],
                      "truncated": bool(got["truncated"]), "cache_hit": not ran},
    }


def _bool(raw, default: bool) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes")


def list_facts(ctx) -> dict:
    """GET /v1/usage/facts"""
    granularity = (ctx.params.get("granularity") or "day").strip().lower()
    if granularity not in GRANULARITIES:
        raise ApiError("bad_request", "Parameter 'granularity' must be 'hour' or 'day'.")
    start, end = parse_window(ctx.params, granularity)
    group_by = parse_group_by(ctx.params.get("group_by"))
    include = {p.strip().lower() for p in str(ctx.params.get("include") or "billing").split(",") if p.strip()}
    res = compute_facts(ctx, start, end, granularity, group_by, filters=_filters(ctx),
                        fresh=_bool(ctx.params.get("fresh"), False), include_billing="billing" in include)
    page = paginate_list(res.pop("rows"), ctx.params, default_limit=500, max_limit=1000)
    page.update(res)
    return page


# -- Daily JSONL exports -------------------------------------------------------

def _export_day(export_id: str) -> dt.date:
    m = EXPORT_ID_RE.match(export_id or "")
    if not m:
        raise ApiError("not_found", "Unknown export id. Expected usage-facts-hourly-YYYY-MM-DD.", status=404)
    try:
        return dt.date.fromisoformat(m.group(1))
    except ValueError:
        raise ApiError("not_found", "Unknown export id.", status=404)


def _export_entry(day: dt.date, now: dt.datetime) -> dict:
    start = dt.datetime.combine(day, dt.time.min, tzinfo=UTC)
    end = start + dt.timedelta(days=1)
    eid = "usage-facts-hourly-%s" % day.isoformat()
    return {"object": "usage_export", "id": eid, "period_start": iso(start), "period_end": iso(end),
            "granularity": "hour", "group_by": list(EXPORT_GROUP_BY), "format": "jsonl",
            "status": "complete" if end <= now - SETTLE_DELAY else "partial",
            "download_path": "/v1/usage/exports/%s/download" % eid}


def list_exports(ctx) -> dict:
    """GET /v1/usage/exports?after=&before= (RFC 3339 or YYYY-MM-DD; default the last 7 UTC days)."""
    now = dt.datetime.now(UTC)

    def day_of(key: str):
        raw = (ctx.params.get(key) or "").strip()
        if not raw:
            return None
        ts = timeutil.parse_rfc3339(raw) if "T" in raw else None
        if ts is not None:
            return ts.astimezone(UTC).date()
        try:
            return dt.date.fromisoformat(raw[:10])
        except ValueError:
            raise ApiError("bad_request", "Parameter '%s' must be RFC 3339 or YYYY-MM-DD." % key)

    before = day_of("before") or (now.date() + dt.timedelta(days=1))   # exclusive
    after = day_of("after") or (before - dt.timedelta(days=7))
    if before <= after:
        raise ApiError("bad_request", "Parameter 'before' must be after 'after'.")
    if (before - after).days > 400:
        raise ApiError("bad_request", "The export listing window is limited to 400 days.")
    days = [after + dt.timedelta(days=i) for i in range((before - after).days) if after + dt.timedelta(days=i) <= now.date()]
    page = paginate_list([_export_entry(d, now) for d in days], ctx.params, default_limit=100, max_limit=400)
    page["range"] = {"after": after.isoformat(), "before": before.isoformat(), "timezone": "UTC"}
    return page


def download_export(ctx):
    """GET /v1/usage/exports/{export_id}/download -> application/x-ndjson, one hourly fact per line."""
    day = _export_day(ctx.path_params.get("export_id") or "")
    now = dt.datetime.now(UTC)
    if day > now.date():
        raise ApiError("not_found", "This export period has not started yet.", status=404)
    start = dt.datetime.combine(day, dt.time.min, tzinfo=UTC)
    res = compute_facts(ctx, start, start + dt.timedelta(days=1), "hour", EXPORT_GROUP_BY,
                        fresh=_bool(ctx.params.get("fresh"), False), include_billing=True)
    entry = _export_entry(day, now)
    lines = [json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for r in res["rows"]]
    body = ("\n".join(lines) + "\n") if lines else ""
    revision = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    headers = [("X-Usage-Export-Id", entry["id"]), ("X-Usage-Export-Status", entry["status"]),
               ("X-Usage-Export-Revision", revision), ("X-Data-Refreshed-At", res["data_refreshed_at"] or ""),
               ("Content-Disposition", 'attachment; filename="%s.jsonl"' % entry["id"])]
    return RawResponse(body, "application/x-ndjson; charset=utf-8", headers)
