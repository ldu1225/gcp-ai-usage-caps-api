"""Request context, multi-project audit/billing source resolution, and SQL helpers."""

from __future__ import annotations

import datetime as dt
import decimal
import logging
import os
import threading

from .. import config
from ..gcp import GcpError
from ..httpkit import ApiError, iso_z
from ..schema import MultiTableSource
from ..timeutil import parse_rfc3339

LOG = logging.getLogger("usage_caps_api.services")

AUDIT_DATA_ACCESS = "cloudaudit_googleapis_com_data_access"
GE_MESSAGE = "discoveryengine_googleapis_com_gen_ai_user_message"
GE_ACTIVITY = "discoveryengine_googleapis_com_gemini_enterprise_user_activity"
AGY_SINK = "businessaicode_googleapis_com_inference_response"


def num(value, default=0):
    if value is None:
        return default
    if isinstance(value, decimal.Decimal):
        return float(value)
    return value


def to_int(value) -> int:
    value = num(value, 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def to_float(value) -> float | None:
    value = num(value, None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ratio(numerator, denominator, digits: int = 4) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return round(float(numerator) / float(denominator), digits)


def iso(value) -> str | None:
    return iso_z(value)


def human_sql(col: str) -> str:
    return ("(%s LIKE '%%@%%' AND NOT ENDS_WITH(%s, '.gserviceaccount.com') AND NOT STARTS_WITH(%s, 'principal://'))"
            % (col, col, col))


def bucket_sql(ts: str) -> str:
    return ("DATE_ADD(@start_date, INTERVAL DIV(DATE_DIFF(DATE(%s, @tz), @start_date, DAY), @bucket_days) "
            "* @bucket_days DAY)" % ts)


def range_params(rng) -> dict:
    lo = (rng.start - dt.timedelta(days=1)).strftime("%Y%m%d")
    hi = (rng.end + dt.timedelta(days=1)).strftime("%Y%m%d")
    return {
        "start_ts": rng.start_ts(),
        "end_ts": rng.end_ts(),
        "tz": rng.tz_name,
        "start_date": rng.start,
        "bucket_days": rng.bucket_days,
        "sfx_start": lo,
        "sfx_end": hi,
    }


def discovery_host(location: str) -> str:
    return ("https://discoveryengine.googleapis.com" if location == "global"
            else "https://%s-discoveryengine.googleapis.com" % location)


def discovery_locations(ctx) -> list[str]:
    locs = ctx.ws.get("discovery_engine_locations") or ["global"]
    return [loc for loc in locs if config.LOCATION_RE.match(loc or "")]


class Ctx:
    """Per-request execution context."""

    def __init__(self, app, cfg: dict, caller, *, params=None, path_params=None, body=None, rng=None, version: int = 1):
        self.app = app
        self.config = cfg
        self.caller = caller
        self.params = dict(params or {})
        self.path_params = dict(path_params or {})
        self.body = dict(body or {}) if isinstance(body, dict) else {}
        self.range = rng
        self.version = version
        self.warnings: list[dict] = []
        self.sources: set[str] = set()
        self.deny_quota_patience = False
        self._lock = threading.Lock()
        self._dataset_failed: dict[str, bool] = {}

    def child(self, *, params=None, path_params=None, body=None, rng=None) -> "Ctx":
        return Ctx(
            self.app, self.config, self.caller,
            params=self.params if params is None else params,
            path_params=self.path_params if path_params is None else path_params,
            body=self.body if body is None else body,
            rng=self.range if rng is None else rng,
            version=self.version,
        )

    def absorb(self, other: "Ctx"):
        with self._lock:
            self.sources |= other.sources
            for w in other.warnings:
                if w not in self.warnings:
                    self.warnings.append(w)

    @property
    def gcp(self):
        return self.app.gcp

    @property
    def store(self):
        return self.app.store

    @property
    def registry(self):
        return self.app.registry

    @property
    def ws(self) -> dict:
        return self.config["workspace"]

    @property
    def project(self) -> str:
        return self.ws["project_id"]

    @property
    def quota_project(self) -> str:
        return config.quota_project(self.config)

    @property
    def tz_name(self) -> str:
        return self.ws.get("timezone") or "UTC"

    @property
    def audit_dataset(self) -> str:
        return self.ws.get("audit_dataset") or "ge_analytics"

    @property
    def central_audit_project(self) -> str:
        val = (self.ws.get("audit_project") or "").strip().lower()
        return val if config.valid_project(val) else ""

    @property
    def max_bytes(self) -> int:
        return int((self.config.get("bigquery") or {}).get("max_bytes_billed") or 20_000_000_000)

    @property
    def excluded_patterns(self) -> list[str]:
        return list((self.config.get("pricing") or {}).get("excluded_sku_patterns") or config.MANDATORY_EXCLUDED_SKU_PATTERNS)

    @property
    def selected_project(self) -> str:
        raw = (self.params.get("project") or "all").strip().lower()
        if raw and raw != "all" and config.valid_project(raw):
            return raw
        return "all"

    @property
    def all_projects(self) -> list[str]:
        """All workspace projects: primary + configured + folder/org scope + optional CRM search."""
        from . import scope as scope_svc
        out: list[str] = []
        seen: set[str] = set()

        def add(pid):
            if isinstance(pid, str):
                p = pid.strip().lower()
                if config.valid_project(p) and p not in seen:
                    seen.add(p)
                    out.append(p)

        add(self.project)
        for p in self.ws.get("projects") or []:
            add(p)
        if scope_svc.scope_spec(self) is not None:
            found = scope_svc.discover(self)
            for item in (found or {}).get("projects") or []:
                add(item.get("id"))
        elif self.ws.get("scan_all_projects", True) and scope_svc.is_live(self):
            for p in scope_svc.crm_quick_search(self):
                add(p)
        return out

    @property
    def projects(self) -> list[str]:
        all_p = self.all_projects
        sel = self.selected_project
        if sel != "all":
            if sel not in all_p:
                raise ApiError("bad_request", "Project '%s' is not in the configured workspace scope." % sel)
            return [sel]
        return all_p

    # -- Notices -----------------------------------------------------------------
    def add_source(self, label: str):
        with self._lock:
            self.sources.add(label)

    def warn(self, source: str, code: str, message: str):
        item = {"source": source, "code": code, "message": message}
        with self._lock:
            if item not in self.warnings:
                self.warnings.append(item)

    def warn_error(self, source: str, err: Exception, what: str):
        kind = getattr(err, "kind", "upstream_error")
        self.warn(source, kind, "%s could not be read (%s)." % (what, kind))

    # -- GCP helpers -------------------------------------------------------------
    def rest(self, method: str, url: str, *, params=None, json_body=None, data=None, headers=None,
             timeout: int = 30, expect_json: bool = True):
        payload, _ = self.gcp.request(
            method, url, quota_project=self.quota_project, params=params, json_body=json_body,
            data=data, headers=headers, timeout=timeout, expect_json=expect_json,
        )
        return payload

    def query(self, sql: str, params=None, module: str = "usage", max_rows: int | None = None) -> list[dict]:
        res = self.gcp.query(
            sql, params or {}, quota_project=self.quota_project, module=module,
            max_bytes=self.max_bytes, max_rows=max_rows,
        )
        return res.rows

    def run(self, tasks: dict) -> dict:
        return self.gcp.run_parallel(tasks)

    def memo(self, namespace: str, key, fn, ttl: float | None = None):
        ttl_s = ttl if ttl is not None else float((self.config.get("server") or {}).get("cache_ttl_seconds") or 120)
        cache = getattr(self.app, "cache", None)
        if cache is None or ttl_s <= 0:
            return fn(self)
        full_key = (namespace, self.version, key)
        val, _ = cache.get_or_compute(full_key, ttl_s, lambda: fn(self))
        return val

    # -- Audit & Billing BigQuery table resolution -------------------------------
    def audit_source(self, prefix: str, *, optional: bool = False):
        """Resolves a Log Router BigQuery table across central or per-project audit datasets."""
        dataset = self.audit_dataset
        if not config.valid_dataset(dataset):
            return None
        central = self.central_audit_project
        projects = self.projects
        if central:
            try:
                src = self.registry.resolve(
                    central, dataset, prefix, quota_project=self.quota_project,
                    max_bytes=self.max_bytes, filter_projects=projects,
                )
            except GcpError as err:
                if not optional:
                    self.warn_error("bigquery:%s.%s" % (central, dataset), err, "Audit dataset")
                return None
            return src
        found = []
        for pid in projects:
            if self._dataset_failed.get(pid):
                continue
            try:
                src = self.registry.resolve(pid, dataset, prefix, quota_project=self.quota_project,
                                            max_bytes=self.max_bytes)
                if src is not None:
                    found.append(src)
            except GcpError as err:
                if err.kind in ("not_found", "denied"):
                    self._dataset_failed[pid] = True
                if not optional and pid == self.project:
                    self.warn_error("bigquery:%s.%s" % (pid, dataset), err, "Audit dataset")
        if not found:
            return None
        return found[0] if len(found) == 1 else MultiTableSource(found)

    def billing_source(self):
        bil = self.ws.get("billing") or {}
        project = (bil.get("project_id") or self.project or "").strip().lower()
        dataset = (bil.get("dataset") or "").strip()
        table = (bil.get("table") or "").strip()
        if not (config.valid_project(project) and config.valid_dataset(dataset) and config.valid_table(table)):
            return None
        try:
            return self.registry.resolve(project, dataset, table, quota_project=self.quota_project,
                                         max_bytes=self.max_bytes)
        except GcpError as err:
            self.warn_error("bigquery:%s.%s" % (project, dataset), err, "Billing export")
            return None


def parse_rfc3339_safe(value):
    return parse_rfc3339(value)
