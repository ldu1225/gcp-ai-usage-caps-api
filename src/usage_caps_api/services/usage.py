"""Per-user usage & cost aggregation across Antigravity, Vertex AI (Agent Platform), and Gemini Enterprise.

Sources:
- Antigravity: BigQuery sink `businessaicode_googleapis_com_inference_response` + Cloud Logging fallback/gap/tail
- Vertex AI / Agent Platform: Cloud Audit Data Access (`cloudaudit_googleapis_com_data_access`, aiplatform.googleapis.com)
- Gemini Enterprise: `discoveryengine_googleapis_com_gen_ai_user_message` & `gemini_enterprise_user_activity`
"""

from __future__ import annotations

import datetime as dt
import logging
import re

from .. import config, identity, timeutil
from ..gcp import GcpError
from ..httpkit import ApiError, paginate_list
from ..schema import expr, quote_ident
from . import pricing as pricing_svc
from .base import (AGY_SINK, AUDIT_DATA_ACCESS, GE_ACTIVITY, GE_MESSAGE, bucket_sql, human_sql, iso,
                   range_params, to_int)

LOG = logging.getLogger("usage_caps_api.usage")

LOGGING_V2 = "https://logging.googleapis.com/v2"
AGY_PAYLOAD_COL = "jsonpayload_v1_inferenceresponselog"
MODEL_CALL_METHODS = r"(GenerateContent|StreamGenerateContent|Predict|RawPredict|StreamRawPredict|ChatCompletions)"
ASSIST_METHODS = r"(?i)^(StreamAssist|Assist|AnswerQuery|ConverseConversation)$"
_LOGNAME_PROJECT_RE = re.compile(r"^projects/([^/]+)/")
_LABEL_SAFE_PUNCT = frozenset(".-:/+=~!$&'()*,;%")


def entry_project(log_name: str | None, default: str | None = None) -> str | None:
    m = _LOGNAME_PROJECT_RE.match(log_name or "") if isinstance(log_name, str) else None
    return m.group(1) if m else default


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
        "ts": "%s.%s" % (alias, quote_ident(src.time_column())),
    }


class UsageResult:
    """Aggregated per-user usage across one or more log sources."""

    def __init__(self, source: str):
        self.source = source
        self.ok = False
        self.records = 0
        self.truncated = False
        self.users: dict[str, dict] = {}
        self.daily: dict[tuple[str, dt.date], dict] = {}
        self.project_last: dict[str, dt.datetime] = {}
        self.principals: dict[str, str] = {}

    def user(self, person: str) -> dict:
        return self.users.setdefault(person, {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "total_tokens": 0,
            "by_model": {},
            "by_surface": {},
            "by_project": {},
            "last_active": None,
        })

    @staticmethod
    def _add_counters(bucket: dict, req: int, total: int, inp: int, out_tok: int, cached: int):
        bucket["requests"] = bucket.get("requests", 0) + req
        bucket["input_tokens"] = bucket.get("input_tokens", 0) + inp
        bucket["output_tokens"] = bucket.get("output_tokens", 0) + out_tok
        bucket["cached_tokens"] = bucket.get("cached_tokens", 0) + cached
        bucket["total_tokens"] = bucket.get("total_tokens", 0) + total

    def add_record(self, person: str, *, surface: str, project: str | None, model: str | None,
                   requests: int, total: int, prompt: int, cand: int, cached: int, ts=None,
                   raw_principal: str | None = None):
        if raw_principal and identity.is_workforce_key(person) and person not in self.principals:
            if identity.person_key(raw_principal) == person:
                self.principals[person] = raw_principal.strip()
        inp = max(0, prompt)
        c_tok = min(max(0, cached), inp)
        out_tok = max(total - inp, max(0, cand), 0)
        tot = max(total, inp + out_tok)
        u = self.user(person)
        self._add_counters(u, requests, tot, inp, out_tok, c_tok)
        if model:
            mrec = u["by_model"].setdefault(model, {"surface": surface, "requests": 0, "input_tokens": 0,
                                                    "output_tokens": 0, "cached_tokens": 0, "total_tokens": 0})
            self._add_counters(mrec, requests, tot, inp, out_tok, c_tok)
        srec = u["by_surface"].setdefault(surface, {"requests": 0, "input_tokens": 0,
                                                    "output_tokens": 0, "cached_tokens": 0, "total_tokens": 0})
        self._add_counters(srec, requests, tot, inp, out_tok, c_tok)
        proj_key = project or "unknown"
        prec = u["by_project"].setdefault(proj_key, {"requests": 0, "input_tokens": 0, "output_tokens": 0,
                                                     "cached_tokens": 0, "total_tokens": 0, "by_model": {}})
        self._add_counters(prec, requests, tot, inp, out_tok, c_tok)
        if model:
            pm = prec["by_model"].setdefault(model, {"requests": 0, "input_tokens": 0, "output_tokens": 0,
                                                     "cached_tokens": 0, "total_tokens": 0})
            self._add_counters(pm, requests, tot, inp, out_tok, c_tok)
        if ts is not None:
            if u["last_active"] is None or ts > u["last_active"]:
                u["last_active"] = ts

    def add_daily(self, person: str, bucket_date: dt.date, *, requests: int, tokens: int):
        cur = self.daily.setdefault((person, bucket_date), {"requests": 0, "total_tokens": 0})
        cur["requests"] += requests
        cur["total_tokens"] += tokens

    def merge(self, other: "UsageResult"):
        self.records += other.records
        self.truncated = self.truncated or other.truncated
        for person, rec in other.users.items():
            for proj, prec in (rec.get("by_project") or {}).items():
                if prec.get("by_model"):
                    for model, mrec in prec["by_model"].items():
                        surf = ((rec.get("by_model") or {}).get(model) or {}).get("surface") or "antigravity"
                        self.add_record(
                            person, surface=surf, project=proj, model=model,
                            requests=mrec["requests"], total=mrec["total_tokens"],
                            prompt=mrec["input_tokens"], cand=mrec["output_tokens"],
                            cached=mrec["cached_tokens"], ts=rec.get("last_active"),
                        )
                else:
                    surf = next(iter((rec.get("by_surface") or {}).keys()), "antigravity")
                    self.add_record(
                        person, surface=surf, project=proj, model=None,
                        requests=prec["requests"], total=prec["total_tokens"],
                        prompt=prec["input_tokens"], cand=prec["output_tokens"],
                        cached=prec["cached_tokens"], ts=rec.get("last_active"),
                    )
        for (person, bdate), drec in other.daily.items():
            self.add_daily(person, bdate, requests=drec["requests"], tokens=drec["total_tokens"])
        for p, raw in other.principals.items():
            self.principals.setdefault(p, raw)


# ---------------------------------------------------------------------------
# Antigravity usage: BigQuery sink + Cloud Logging gap/tail fallback
# ---------------------------------------------------------------------------

def _bq_antigravity(ctx, src, start: dt.datetime, end: dt.datetime, *, email: str | None = None, rng=None) -> UsageResult:
    f = agy_fields(src)
    if not f["user"]:
        raise GcpError("invalid", message="sink table lacks labels.user_id")
    null = "CAST(NULL AS STRING)"
    where = "%s IS NOT NULL" % f["user"]
    params: dict = {
        "w_start": start,
        "w_end": end,
        "sfx_start": (start - dt.timedelta(days=1)).strftime("%Y%m%d"),
        "sfx_end": (end + dt.timedelta(days=1)).strftime("%Y%m%d"),
    }
    if email:
        where += " AND %s = @email" % f["user"]
        params["email"] = email.lower()
    lname = expr("t", src.schema, "logName", cast="STRING")
    proj_sql = "REGEXP_EXTRACT(%s, r'^projects/([^/]+)/')" % lname if lname else null
    base = (
        "WITH %s,\nu AS (SELECT %s AS ts, %s AS email, %s AS principal, %s AS model, "
        "IFNULL(%s, 0) AS total, IFNULL(%s, 0) AS prompt, IFNULL(%s, 0) AS cand, IFNULL(%s, 0) AS cached, "
        "%s AS project FROM s t WHERE %s)"
        % (src.cte("s", start="w_start", end="w_end"), f["ts"], f["user"], f["principal"] or null,
           f["model"] or null, f["total"] or "0", f["prompt"] or "0", f["candidates"] or "0",
           f["cached"] or "0", proj_sql, where)
    )
    main_sql = (
        base + "\nSELECT email, model, project, COUNT(*) AS requests, SUM(total) AS tokens, "
        "SUM(prompt) AS prompt_tokens, SUM(cand) AS candidate_tokens, SUM(cached) AS cached_tokens, "
        "ANY_VALUE(principal) AS principal, MAX(ts) AS last_ts\nFROM u GROUP BY email, model, project"
    )
    tasks = {"main": lambda: ctx.query(main_sql, params, "usage")}
    if rng is not None:
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
        usage.add_record(
            person, surface="antigravity", project=rp if isinstance(rp, str) else ctx.project,
            model=row.get("model") or "unknown", requests=req,
            total=to_int(row.get("tokens")), prompt=to_int(row.get("prompt_tokens")),
            cand=to_int(row.get("candidate_tokens")), cached=to_int(row.get("cached_tokens")),
            ts=rl, raw_principal=row.get("principal"),
        )
    if rng is not None and res.get("bucket", (False, None))[0]:
        for row in res["bucket"][1]:
            person = identity.person_key(row.get("email"))
            b = row.get("bucket")
            if isinstance(b, dt.datetime):
                b = b.date()
            if person and isinstance(b, dt.date):
                usage.add_daily(person, b, requests=to_int(row.get("requests")), tokens=to_int(row.get("tokens")))
    usage.ok = True
    return usage


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


def _logging_project(ctx, project: str, log_id: str, start: dt.datetime, end: dt.datetime, email: str | None = None):
    flt = 'logName="projects/%s/logs/%s" AND timestamp>="%s" AND timestamp<"%s"' % (
        project, log_id, _log_ts(start), _log_ts(end))
    if email:
        if identity.is_workforce_key(email):
            flt += ' AND labels.user_id=~"%s"' % _principal_label_regex(email)
        elif config.valid_email(email):
            flt += ' AND labels.user_id=~"%s"' % _user_label_regex(email)
        else:
            raise GcpError("invalid", message="invalid email")
    limit = int((ctx.config.get("caps") or {}).get("max_log_entries") or 20000)
    token, fetched, entries, truncated = None, 0, [], False
    while True:
        body: dict = {"resourceNames": ["projects/%s" % project], "filter": flt, "orderBy": "timestamp desc",
                      "pageSize": min(1000, max(1, limit - fetched))}
        if token:
            body["pageToken"] = token
        data = ctx.rest("POST", "%s/entries:list" % LOGGING_V2, json_body=body, timeout=45)
        batch = (data or {}).get("entries") or []
        entries.extend(batch)
        fetched += len(batch)
        token = (data or {}).get("nextPageToken")
        if not token:
            break
        if fetched >= limit:
            truncated = True
            break
    return entries, truncated


def _logging_antigravity(ctx, start: dt.datetime, end: dt.datetime, *, email: str | None = None,
                         rng=None, ranges: dict | None = None) -> UsageResult:
    log_id = (ctx.config.get("caps") or {}).get("inference_log") or "businessaicode.googleapis.com%2Finference_response"
    if not config.LOG_NAME_RE.match(log_id):
        raise GcpError("invalid", message="invalid inference_log")
    projects = list(ctx.projects)
    if ranges is not None:
        projects = [p for p in projects if p in ranges]
    else:
        ranges = {p: (start, end) for p in projects}
    usage = UsageResult("logging")
    tz = timeutil.get_zone(ctx.tz_name)
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
            labels = entry.get("labels") or {}
            raw_principal = labels.get("user_id")
            if not isinstance(raw_principal, str) or not raw_principal.strip():
                continue
            usage.records += 1
            person = identity.person_key(raw_principal)
            if person is None:
                continue
            meta = ((entry.get("jsonPayload") or {}).get("metadata") or {})
            total = to_int(meta.get("totalTokenCount"))
            prompt = to_int(meta.get("promptTokenCount"))
            cand = to_int(meta.get("candidatesTokenCount"))
            cached = to_int(meta.get("cachedContentTokenCount"))
            model = labels.get("model_id") or labels.get("model") or "unknown"
            ts = timeutil.parse_rfc3339(entry.get("timestamp"))
            usage.add_record(
                person, surface="antigravity", project=entry_project(entry.get("logName"), p),
                model=model, requests=1, total=total, prompt=prompt, cand=cand, cached=cached,
                ts=ts, raw_principal=raw_principal,
            )
            if rng is not None and ts is not None:
                idx = rng.bucket_index(ts.astimezone(tz).date())
                if idx is not None:
                    usage.add_daily(person, rng.buckets()[idx], requests=1, tokens=total)
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
# Vertex AI (Agent Platform) & Gemini Enterprise activity from BigQuery audit logs
# ---------------------------------------------------------------------------

def _vertex_ai_usage(ctx, start: dt.datetime, end: dt.datetime, *, email: str | None = None, rng=None) -> UsageResult:
    dsrc = ctx.audit_source(AUDIT_DATA_ACCESS, optional=True)
    if dsrc is None:
        return UsageResult("unavailable")
    s = dsrc.schema
    principal = expr("t", s, "protopayload_auditlog.authenticationInfo.principalEmail", cast="STRING")
    service = expr("t", s, "protopayload_auditlog.serviceName", cast="STRING")
    method = expr("t", s, "protopayload_auditlog.methodName", cast="STRING")
    resource = expr("t", s, "protopayload_auditlog.resourceName", cast="STRING")
    lname = expr("t", s, "logName", cast="STRING")
    ts = "t.%s" % quote_ident(dsrc.time_column())
    if not (principal and service and method):
        return UsageResult("unavailable")
    where = "email IS NOT NULL AND %s" % human_sql("email")
    params: dict = {
        "w_start": start,
        "w_end": end,
        "sfx_start": (start - dt.timedelta(days=1)).strftime("%Y%m%d"),
        "sfx_end": (end + dt.timedelta(days=1)).strftime("%Y%m%d"),
    }
    if email:
        where += " AND email = @email"
        params["email"] = email.lower()
    model_sql = "REGEXP_EXTRACT(%s, r'models/([^/:@]+)')" % resource if resource else "CAST(NULL AS STRING)"
    proj_sql = "REGEXP_EXTRACT(%s, r'^projects/([^/]+)/')" % lname if lname else "CAST(NULL AS STRING)"
    base = (
        "WITH %s,\nv AS (SELECT %s AS ts, LOWER(%s) AS email, %s AS model, %s AS project "
        "FROM da_src t WHERE %s = 'aiplatform.googleapis.com' AND REGEXP_CONTAINS(%s, r'%s'))"
        % (dsrc.cte("da_src", start="w_start", end="w_end"), ts, principal, model_sql, proj_sql,
           service, method, MODEL_CALL_METHODS)
    )
    sql = base + "\nSELECT email, model, project, COUNT(*) AS requests, MAX(ts) AS last_ts FROM v WHERE %s GROUP BY email, model, project" % where
    usage = UsageResult("bigquery")
    try:
        rows = ctx.query(sql, params, "usage")
        ctx.add_source(dsrc.label)
    except GcpError as err:
        ctx.warn_error(dsrc.label, err, "Vertex AI audit logs")
        return usage
    for r in rows:
        person = identity.person_key(r.get("email"))
        if not person:
            continue
        req = to_int(r.get("requests"))
        usage.records += req
        usage.add_record(
            person, surface="vertex_ai", project=r.get("project") or ctx.project,
            model=r.get("model") or "vertex-ai-model", requests=req,
            total=0, prompt=0, cand=0, cached=0, ts=r.get("last_ts"),
        )
    if rng is not None:
        b_params = dict(params, tz=rng.tz_name, start_date=rng.start, bucket_days=rng.bucket_days)
        b_sql = base + "\nSELECT %s AS bucket, email, COUNT(*) AS requests FROM v WHERE %s GROUP BY bucket, email" % (bucket_sql("ts"), where)
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
    usage.ok = True
    return usage


def _gemini_enterprise_usage(ctx, start: dt.datetime, end: dt.datetime, *, email: str | None = None, rng=None) -> UsageResult:
    asrc = ctx.audit_source(GE_ACTIVITY, optional=True)
    msrc = ctx.audit_source(GE_MESSAGE, optional=True)
    src = asrc or msrc
    if src is None:
        return UsageResult("unavailable")
    s = src.schema
    user_col = (expr("t", s, "jsonPayload.useriamprincipal", "jsonPayload.userIamPrincipal", cast="STRING")
                if src is asrc else expr("t", s, "jsonPayload.user", cast="STRING"))
    if not user_col:
        return UsageResult("unavailable")
    lname = expr("t", s, "logName", cast="STRING")
    proj_sql = "REGEXP_EXTRACT(%s, r'^projects/([^/]+)/')" % lname if lname else "CAST(NULL AS STRING)"
    ts = "t.%s" % quote_ident(src.time_column())
    where = "email IS NOT NULL AND %s" % human_sql("email")
    params: dict = {
        "w_start": start,
        "w_end": end,
        "sfx_start": (start - dt.timedelta(days=1)).strftime("%Y%m%d"),
        "sfx_end": (end + dt.timedelta(days=1)).strftime("%Y%m%d"),
    }
    if email:
        where += " AND email = @email"
        params["email"] = email.lower()
    base = (
        "WITH %s,\ng AS (SELECT %s AS ts, LOWER(%s) AS email, %s AS project FROM ge_src t)"
        % (src.cte("ge_src", start="w_start", end="w_end"), ts, user_col, proj_sql)
    )
    sql = base + "\nSELECT email, project, COUNT(*) AS requests, MAX(ts) AS last_ts FROM g WHERE %s GROUP BY email, project" % where
    usage = UsageResult("bigquery")
    try:
        rows = ctx.query(sql, params, "usage")
        ctx.add_source(src.label)
    except GcpError as err:
        ctx.warn_error(src.label, err, "Gemini Enterprise activity logs")
        return usage
    for r in rows:
        person = identity.person_key(r.get("email"))
        if not person:
            continue
        req = to_int(r.get("requests"))
        usage.records += req
        usage.add_record(
            person, surface="gemini_enterprise", project=r.get("project") or ctx.project,
            model=None, requests=req, total=0, prompt=0, cand=0, cached=0, ts=r.get("last_ts"),
        )
    if rng is not None:
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
    usage.ok = True
    return usage


def collect_usage(ctx, start: dt.datetime, end: dt.datetime, *, email: str | None = None,
                  rng=None, surface: str | None = None, fresh: bool = False) -> UsageResult:
    """Collects unified per-user usage across Antigravity, Vertex AI, and Gemini Enterprise."""
    tasks = {}
    if surface in (None, "", "all", "antigravity"):
        tasks["antigravity"] = lambda: antigravity_usage(ctx, start, end, email=email, rng=rng, fresh=fresh)
    if surface in (None, "", "all", "vertex_ai"):
        tasks["vertex_ai"] = lambda: _vertex_ai_usage(ctx, start, end, email=email, rng=rng)
    if surface in (None, "", "all", "gemini_enterprise"):
        tasks["gemini_enterprise"] = lambda: _gemini_enterprise_usage(ctx, start, end, email=email, rng=rng)
    res = ctx.run(tasks)
    combined = UsageResult("combined")
    any_ok = False
    for name, (ok, val) in res.items():
        if ok and isinstance(val, UsageResult) and val.ok:
            any_ok = True
            combined.merge(val)
    combined.ok = any_ok
    return combined


# ---------------------------------------------------------------------------
# Public REST API handlers: GET /v1/usage/users & GET /v1/usage/users/{email}
# ---------------------------------------------------------------------------

def _format_user_row(email: str, rec: dict, pricing: pricing_svc.Pricing) -> dict:
    est_cost, partial = pricing_svc.estimate_cost(rec, pricing)
    by_model_list = []
    for model_id, mrec in sorted((rec.get("by_model") or {}).items()):
        m_cost, m_partial = pricing_svc.estimate_cost({"by_model": {model_id: mrec}}, pricing)
        by_model_list.append({
            "model": model_id,
            "surface": mrec.get("surface") or "antigravity",
            "requests": mrec["requests"],
            "input_tokens": mrec["input_tokens"],
            "output_tokens": mrec["output_tokens"],
            "cached_tokens": mrec["cached_tokens"],
            "total_tokens": mrec["total_tokens"],
            "estimated_cost": m_cost,
            "cost_partial": m_partial,
        })
    by_model_list.sort(key=lambda r: (-(r["estimated_cost"] or -1.0), -r["total_tokens"], -r["requests"], r["model"]))
    by_project_list = [
        {
            "project": proj,
            "requests": prec["requests"],
            "input_tokens": prec["input_tokens"],
            "output_tokens": prec["output_tokens"],
            "cached_tokens": prec["cached_tokens"],
            "total_tokens": prec["total_tokens"],
        }
        for proj, prec in sorted((rec.get("by_project") or {}).items())
    ]
    return {
        "email": email,
        "uid": identity.uid_for(email),
        "surfaces": sorted((rec.get("by_surface") or {}).keys()),
        "requests": rec["requests"],
        "input_tokens": rec["input_tokens"],
        "output_tokens": rec["output_tokens"],
        "cached_tokens": rec["cached_tokens"],
        "total_tokens": rec["total_tokens"],
        "estimated_cost": est_cost,
        "cost_partial": partial,
        "currency": pricing.currency if est_cost is not None else None,
        "by_model": by_model_list,
        "by_surface": rec.get("by_surface") or {},
        "by_project": by_project_list,
        "last_active": iso(rec.get("last_active")),
    }


def list_user_usage(ctx) -> dict:
    """GET /v1/usage/users"""
    rng = ctx.range or timeutil.parse_range(ctx.params, ctx.tz_name, ctx.ws.get("default_range_days", 14))
    surface = (ctx.params.get("surface") or "").strip().lower()
    if surface and surface not in ("all", *config.VALID_SERVICES):
        raise ApiError("bad_request", "Parameter 'surface' must be one of: all, %s." % ", ".join(config.VALID_SERVICES))
    email_f = (ctx.params.get("email") or "").strip().lower()
    if email_f and not identity.is_person_key(email_f):
        raise ApiError("bad_request", "Parameter 'email' must be a valid email or workforce principal.")

    from . import caps as caps_svc
    policy = caps_svc.effective_policy(ctx)
    pricing = pricing_svc.resolve_pricing(ctx, policy)
    usage = collect_usage(ctx, rng.start_ts(), rng.end_ts(), email=email_f or None, surface=surface or None)
    rows = [_format_user_row(mail, rec, pricing) for mail, rec in usage.users.items()]
    rows.sort(key=lambda r: (-(r["estimated_cost"] if r["estimated_cost"] is not None else -1.0),
                             -r["total_tokens"], -r["requests"], r["email"]))
    page = paginate_list(rows, ctx.params, default_limit=100, max_limit=500)
    total_cost = [r["estimated_cost"] for r in rows if r["estimated_cost"] is not None]
    page["range"] = rng.meta()
    page["currency"] = pricing.currency if total_cost else policy.get("currency", "USD")
    page["summary"] = {
        "active_users": len(rows),
        "total_requests": sum(r["requests"] for r in rows),
        "total_tokens": sum(r["total_tokens"] for r in rows),
        "total_estimated_cost": round(sum(total_cost), 6) if total_cost else None,
    }
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
    usage = collect_usage(ctx, rng.start_ts(), rng.end_ts(), email=email, rng=rng)
    rec = usage.users.get(email) or {
        "requests": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
        "total_tokens": 0, "by_model": {}, "by_surface": {}, "by_project": {}, "last_active": None,
    }
    row = _format_user_row(email, rec, pricing)
    timeseries = []
    for bucket_date in rng.buckets():
        drec = usage.daily.get((email, bucket_date)) or {"requests": 0, "total_tokens": 0}
        timeseries.append({
            "date": bucket_date.isoformat(),
            "requests": drec["requests"],
            "total_tokens": drec["total_tokens"],
        })
    cap_status = caps_svc.get_user_cap_snapshot(ctx, email)
    return {
        "object": "user_usage",
        "range": rng.meta(),
        "user": row,
        "timeseries": timeseries,
        "current_cap": cap_status,
    }
