"""Schema-adaptive BigQuery SQL builder and INFORMATION_SCHEMA table discovery."""

from __future__ import annotations

import logging
import re

from .cache import TTLCache
from .config import COLUMN_SEGMENT_RE, valid_dataset, valid_project, valid_table

LOG = logging.getLogger("usage_caps_api.schema")

SHARD_RE = re.compile(r"^(.+)_(\d{8})$")
SEGMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d{1,3})\])?$")
FILTER_PROJECT_RE = re.compile(r"^[a-z0-9.:\-]+$")
TYPE_ALIASES = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL", "RECORD": "STRUCT"}
TABLES_TTL = 120
SCHEMA_TTL = 300


def norm_type(field_type: str | None) -> str:
    ft = (field_type or "").upper()
    return TYPE_ALIASES.get(ft, ft)


def quote_ident(name: str) -> str:
    if not COLUMN_SEGMENT_RE.match(name or ""):
        raise ValueError("invalid SQL identifier")
    return "`%s`" % name


def table_ref(project: str, dataset: str, table: str) -> str:
    if not valid_project(project) or not valid_dataset(dataset):
        raise ValueError("invalid table reference")
    if table.endswith("_*"):
        if not valid_table(table[:-2]):
            raise ValueError("invalid table reference")
    elif not valid_table(table):
        raise ValueError("invalid table reference")
    return "`%s.%s.%s`" % (project, dataset, table)


class Node:
    __slots__ = ("name", "type", "mode", "children")

    def __init__(self, name: str, ftype: str, mode: str, children=None):
        self.name = name
        self.type = norm_type(ftype)
        self.mode = (mode or "NULLABLE").upper()
        self.children = children or {}


class _DictField:
    def __init__(self, d: dict):
        self.name = d.get("name")
        self.field_type = d.get("type", "STRING")
        self.mode = d.get("mode", "NULLABLE")
        self.fields = [_DictField(x) for x in d.get("fields", [])]


class TableSchema:
    """Case-insensitive lookup tree over a BigQuery table schema."""

    def __init__(self, fields, partition_field: str | None = None, ingestion_partitioned: bool = False):
        self.tree = self._build(fields)
        self.partition_field = partition_field
        self.ingestion_partitioned = ingestion_partitioned

    @classmethod
    def from_dicts(cls, fields: list[dict], **kwargs) -> "TableSchema":
        return cls([_DictField(f) for f in fields], **kwargs)

    def _build(self, fields) -> dict[str, Node]:
        tree: dict[str, Node] = {}
        for field in fields or []:
            name = field.name
            if not COLUMN_SEGMENT_RE.match(name or ""):
                continue
            children = self._build(getattr(field, "fields", None) or [])
            tree[name.lower()] = Node(name, field.field_type, field.mode, children)
        return tree

    def node(self, path: str) -> Node | None:
        cur = self.tree
        node = None
        for part in path.split("."):
            node = cur.get(part.lower())
            if node is None:
                return None
            cur = node.children
        return node

    def has(self, path: str) -> bool:
        return self.node(path) is not None

    def first_timestamp(self) -> str | None:
        for node in self.tree.values():
            if node.type == "TIMESTAMP" and node.mode != "REPEATED":
                return node.name
        return None


def _parse_candidate(candidate: str):
    segs = []
    for raw in candidate.split("."):
        m = SEGMENT_RE.match(raw)
        if not m:
            return None
        segs.append((m.group(1), int(m.group(2)) if m.group(2) is not None else None))
    return segs


def _json_path(parts) -> str | None:
    path = "$"
    for name, idx in parts:
        if not COLUMN_SEGMENT_RE.match(name):
            return None
        path += "." + name
        if idx is not None:
            path += "[%d]" % idx
    return path


def build_expr(alias: str, schema: TableSchema | None, candidate: str):
    """Returns (sql, type) for one candidate path, or None if missing in schema."""
    if schema is None or not COLUMN_SEGMENT_RE.match(alias or ""):
        return None
    segs = _parse_candidate(candidate)
    if not segs:
        return None
    children = schema.tree
    resolved = []
    for i, (name, idx) in enumerate(segs):
        node = children.get(name.lower())
        if node is None:
            return None
        resolved.append((node.name, idx, node))
        is_last = i == len(segs) - 1
        if node.type == "JSON" or (node.type == "STRING" and not is_last):
            rest = segs[i + 1:]
            base = "%s.%s" % (alias, ".".join(quote_ident(n) for n, _, _ in resolved))
            if not rest:
                return ("JSON_VALUE(%s)" % base if node.type == "JSON" else base), "STRING"
            path = _json_path(rest)
            if path is None:
                return None
            return "JSON_VALUE(%s, '%s')" % (base, path), "STRING"
        if not is_last:
            if node.type != "STRUCT":
                return None
            children = node.children
    leaf = resolved[-1][2]
    if leaf.type == "STRUCT":
        return None
    repeated = any(n.mode == "REPEATED" or ix is not None for _, ix, n in resolved)
    if not repeated:
        return "%s.%s" % (alias, ".".join(quote_ident(n) for n, _, _ in resolved)), leaf.type
    root_name = resolved[0][0]
    inner = [(n, 0 if (ix is None and nd.mode == "REPEATED") else ix) for n, ix, nd in resolved[1:]]
    path = "$" + "".join((".%s%s" % (n, "[%d]" % ix if ix is not None else "")) for n, ix in inner)
    return "JSON_VALUE(TO_JSON_STRING(%s.%s), '%s')" % (alias, quote_ident(root_name), path), "STRING"


def expr(alias: str, schema: TableSchema | None, *candidates: str, cast: str | None = None) -> str | None:
    """First candidate path present in schema, safely cast to the target BigQuery type."""
    for cand in candidates:
        built = build_expr(alias, schema, cand)
        if built is None:
            continue
        sql, actual = built
        if not cast or actual == cast:
            return sql
        if cast in ("INT64", "FLOAT64", "BOOL", "TIMESTAMP", "DATE"):
            return "SAFE_CAST(%s AS %s)" % (sql, cast)
        return "CAST(%s AS %s)" % (sql, cast)
    return None


def project_filter_sql(schema: TableSchema | None, projects: list[str] | tuple[str, ...] | None, alias: str = "t") -> str:
    """Filters a central audit dataset table to the allowed workspace projects using logName."""
    clean = sorted({p.lower() for p in (projects or ()) if isinstance(p, str) and FILTER_PROJECT_RE.match(p.lower())})
    if not clean or schema is None:
        return ""
    lname = expr(alias, schema, "logName", cast="STRING")
    if not lname:
        return ""
    quoted = ", ".join("'%s'" % p for p in clean)
    return "REGEXP_EXTRACT(%s, r'^projects/([^/]+)/') IN (%s)" % (lname, quoted)


class TableSource:
    def __init__(self, project: str, dataset: str, table: str, schema: TableSchema,
                 *, sharded: bool = False, created=None, filter_projects=None, location: str | None = None,
                 sample_table: str | None = None):
        self.project = project
        self.dataset = dataset
        self.table = table
        self.sample_table = sample_table or table
        self.schema = schema
        self.sharded = sharded
        self.created = created
        self.filter_projects = list(filter_projects) if filter_projects else None
        self.location = location

    def each(self) -> list["TableSource"]:
        return [self]

    @property
    def label(self) -> str:
        return "bigquery:%s.%s.%s" % (self.project, self.dataset, self.table)

    def time_column(self) -> str:
        for cand in ("timestamp", "usage_start_time", "receiveTimestamp"):
            node = self.schema.node(cand)
            if node is not None and node.type == "TIMESTAMP":
                return node.name
        return self.schema.first_timestamp() or "timestamp"

    def cte(self, name: str, *, start: str = "start_ts", end: str = "end_ts",
            sfx_start: str = "sfx_start", sfx_end: str = "sfx_end") -> str:
        ref = table_ref(self.project, self.dataset, self.table + "_*" if self.sharded else self.table)
        ts = quote_ident(self.time_column())
        clauses = ["t.%s >= @%s" % (ts, start), "t.%s < @%s" % (ts, end)]
        if self.sharded:
            clauses.append("_TABLE_SUFFIX BETWEEN @%s AND @%s" % (sfx_start, sfx_end))
        elif self.schema.ingestion_partitioned:
            clauses.append("_PARTITIONTIME >= TIMESTAMP_TRUNC(@%s, DAY)" % start)
        pfilt = project_filter_sql(self.schema, self.filter_projects, alias="t")
        if pfilt:
            clauses.append(pfilt)
        return "%s AS (SELECT * FROM %s t WHERE %s)" % (quote_ident(name), ref, " AND ".join(clauses))


class MultiTableSource:
    """Combines equivalent tables across multiple workspace projects.

    Callers should iterate `each()` (or `by_location()`) so tables in different BigQuery locations
    (e.g. `US` and `asia-northeast3`) or with different Log Router inferred schemas are queried
    independently and merged rather than crashing a single cross-location `UNION ALL`."""

    def __init__(self, sources: list[TableSource]):
        self.sources = list(sources)
        self.project = sources[0].project
        self.dataset = sources[0].dataset
        self.table = sources[0].table
        self.schema = sources[0].schema
        self.sharded = any(s.sharded for s in sources)
        self.created = min((s.created for s in sources if s.created is not None), default=None)
        locs = {s.location for s in sources if s.location}
        self.location = next(iter(locs)) if len(locs) == 1 else None

    def each(self) -> list[TableSource]:
        return list(self.sources)

    def by_location(self) -> list[TableSource | "MultiTableSource"]:
        groups: dict[str, list[TableSource]] = {}
        for s in self.sources:
            key = (s.location or f"__unknown__:{s.project}:{s.dataset}").upper()
            groups.setdefault(key, []).append(s)
        return [g[0] if len(g) == 1 else MultiTableSource(g) for g in groups.values()]

    @property
    def label(self) -> str:
        return "bigquery:%s.%s (%d projects)" % (self.dataset, self.table, len(self.sources))

    def time_column(self) -> str:
        return self.sources[0].time_column()

    def cte(self, name: str, *, start: str = "start_ts", end: str = "end_ts",
            sfx_start: str = "sfx_start", sfx_end: str = "sfx_end") -> str:
        if len(self.sources) == 1:
            return self.sources[0].cte(name, start=start, end=end, sfx_start=sfx_start, sfx_end=sfx_end)
        parts = []
        for src in self.sources:
            ref = table_ref(src.project, src.dataset, src.table + "_*" if src.sharded else src.table)
            ts = quote_ident(src.time_column())
            clauses = ["t.%s >= @%s" % (ts, start), "t.%s < @%s" % (ts, end)]
            if src.sharded:
                clauses.append("_TABLE_SUFFIX BETWEEN @%s AND @%s" % (sfx_start, sfx_end))
            parts.append("SELECT * FROM %s t WHERE %s" % (ref, " AND ".join(clauses)))
        return "%s AS (%s)" % (quote_ident(name), "\nUNION ALL ".join(parts))


class SchemaRegistry:
    """Discovers BigQuery tables and schemas with TTL caching."""

    def __init__(self, gcp):
        self.gcp = gcp
        self._cache = TTLCache(512)

    def clear(self):
        self._cache.clear()

    def list_tables(self, project: str, dataset: str, *, quota_project: str, max_bytes: int) -> dict:
        key = ("tables", project, dataset)
        def load():
            sql = ("SELECT table_name, creation_time FROM `%s.%s.INFORMATION_SCHEMA.TABLES`"
                   % (project, dataset))
            res = self.gcp.query(sql, {}, quota_project=quota_project, module="schema", max_bytes=max_bytes)
            out: dict = {}
            for row in sorted(res.rows, key=lambda r: str(r.get("table_name") or "")):
                tname = row.get("table_name")
                ctime = row.get("creation_time")
                if not tname:
                    continue
                m = SHARD_RE.match(tname)
                if m:
                    prefix, suffix = m.group(1), m.group(2)
                    cur = out.get(prefix)
                    if cur is None:
                        out[prefix] = {"sharded": True, "sample": tname, "latest_suffix": suffix, "created": ctime}
                    elif cur["sharded"]:
                        if suffix > cur["latest_suffix"]:
                            created = min(cur["created"], ctime) if (cur.get("created") and ctime) else ctime
                            out[prefix] = {"sharded": True, "sample": tname, "latest_suffix": suffix, "created": created}
                    # If a non-sharded partitioned table already exists for the same prefix, keep it deterministically
                    # unless the shard is strictly newer (e.g. legacy sharded sink still active).
                    elif ctime and cur.get("created") and ctime > cur["created"]:
                        out[prefix] = {"sharded": True, "sample": tname, "latest_suffix": suffix, "created": cur["created"]}
                else:
                    out[tname] = {"sharded": False, "sample": tname, "latest_suffix": "", "created": ctime}
            return out
        val, _ = self._cache.get_or_compute(key, TABLES_TTL, load)
        return val

    def get_schema_and_location(self, project: str, dataset: str, table: str, *,
                                quota_project: str, fresh: bool = False) -> tuple[TableSchema, str | None]:
        key = ("schema_loc", project, dataset, table)
        def load():
            client = self.gcp.bq_client(quota_project)
            tbl = client.get_table("%s.%s.%s" % (project, dataset, table))
            tp = getattr(tbl, "time_partitioning", None)
            pfield = getattr(tp, "field", None) if tp else None
            ingestion = tp is not None and not pfield
            loc = getattr(tbl, "location", None)
            return TableSchema(tbl.schema, partition_field=pfield, ingestion_partitioned=ingestion), (
                str(loc) if isinstance(loc, str) and loc else None)
        if fresh:
            val = load()
            self._cache.set(key, val, SCHEMA_TTL)
            return val
        val, _ = self._cache.get_or_compute(key, SCHEMA_TTL, load)
        return val

    def get_schema(self, project: str, dataset: str, table: str, *, quota_project: str, fresh: bool = False) -> TableSchema:
        schema, _ = self.get_schema_and_location(project, dataset, table, quota_project=quota_project, fresh=fresh)
        return schema

    def resolve(self, project: str, dataset: str, prefix: str, *, quota_project: str,
                max_bytes: int, filter_projects=None) -> TableSource | None:
        tables = self.list_tables(project, dataset, quota_project=quota_project, max_bytes=max_bytes)
        hit = tables.get(prefix)
        if not hit:
            return None
        schema, loc = self.get_schema_and_location(project, dataset, hit["sample"], quota_project=quota_project)
        return TableSource(project, dataset, prefix, schema, sharded=hit["sharded"],
                           created=hit.get("created"), filter_projects=filter_projects, location=loc,
                           sample_table=hit["sample"])
