"""Multi-project / folder / organization workspace scope discovery via Cloud Resource Manager v3."""

from __future__ import annotations

import collections
import logging
import os
import re

from .. import config
from ..gcp import GcpError

LOG = logging.getLogger("usage_caps_api.scope")

CRM = "https://cloudresourcemanager.googleapis.com/v3"
SCOPE_TYPES = ("folder", "organization")
DEFAULT_MAX_PROJECTS = 300
MAX_PROJECTS = 5000
MAX_DEPTH = 10
MAX_FOLDERS = 2000
PAGE_SIZE = 300
WALK_BATCH = 10
DISCOVERY_TTL = 1800
QUICK_SEARCH_TTL = 1800

_FOLDER_RE = re.compile(r"^folders/[0-9]{1,30}$")
_NUMBER_RE = re.compile(r"^projects/([0-9]{1,30})$")


def scope_ids(ctx) -> tuple[str, ...]:
    raw = ctx.ws.get("scope_ids") or ()
    if isinstance(raw, str):
        raw = raw.split(",")
    return tuple(dict.fromkeys(str(v).strip() for v in raw if config.SCOPE_ID_RE.match(str(v).strip())))


def max_projects(ctx) -> int:
    try:
        val = int(ctx.ws.get("scope_max_projects") or DEFAULT_MAX_PROJECTS)
    except (TypeError, ValueError):
        val = DEFAULT_MAX_PROJECTS
    return max(1, min(MAX_PROJECTS, val))


def scope_spec(ctx):
    st = ctx.ws.get("scope_type")
    if st not in SCOPE_TYPES:
        return None
    ids = scope_ids(ctx)
    if not ids:
        return None
    return st, ids, max_projects(ctx)


def is_live(ctx) -> bool:
    app = getattr(ctx, "app", None)
    return bool(
        config.workspace_configured(ctx.config)
        and hasattr(app, "gcp")
        and type(app.gcp).__name__ == "Gcp"
        and hasattr(app, "cache")
    )


def crm_quick_search(ctx) -> list[str]:
    """Lists up to 25 active projects visible to the service account (projects scope + scan_all_projects)."""
    if not is_live(ctx):
        return []
    key = ("crm_quick_search", ctx.project)

    def load():
        try:
            payload, _ = ctx.gcp.request("GET", "%s/projects:search?pageSize=25" % CRM,
                                         quota_project=ctx.quota_project, timeout=10)
            out = []
            for item in (payload or {}).get("projects") or []:
                if isinstance(item, dict) and item.get("state") == "ACTIVE":
                    pid = str(item.get("projectId") or "").strip().lower()
                    if config.valid_project(pid):
                        out.append(pid)
            return out
        except Exception as err:  # pylint: disable=broad-except
            LOG.debug("CRM quick search skipped: %s", type(err).__name__)
            return []

    val, _ = ctx.app.cache.get_or_compute(key, QUICK_SEARCH_TTL, load)
    return list(val or [])


def discover(ctx) -> dict | None:
    """Breadth-first discovery of active projects under folder/organization scope."""
    spec = scope_spec(ctx)
    if spec is None or not is_live(ctx):
        return None
    key = ("scope_discovery",) + spec
    val, _ = ctx.app.cache.get_or_compute(key, DISCOVERY_TTL, lambda: _walk_scope(ctx, spec))
    return val


def _walk_scope(ctx, spec) -> dict:
    scope_type, ids, limit = spec
    prefix = "folders/" if scope_type == "folder" else "organizations/"
    roots = [prefix + sid for sid in ids]
    found, seen, queued = [], set(), set()
    truncated = False
    folders = 0
    errors: list[str] = []
    queue = collections.deque()
    for root in roots:
        if root not in queued:
            queued.add(root)
            queue.append((root, 0))

    while queue:
        if len(found) >= limit:
            truncated = True
            break
        batch = []
        while queue and len(batch) < WALK_BATCH:
            if queue[0][0].startswith("folders/"):
                if folders >= MAX_FOLDERS:
                    break
                folders += 1
            batch.append(queue.popleft())
        if not batch:
            truncated = True
            break
        res = ctx.run({parent: (lambda p=parent: _list_children(ctx, p)) for parent, _ in batch})
        for parent, depth in batch:
            ok, value = res.get(parent, (False, None))
            if not ok:
                errors.append(getattr(value, "kind", "error"))
                continue
            (items, more_p, err_p), (subs, more_f, err_f) = value
            for err in (err_p, err_f):
                if err is not None:
                    code = getattr(err, "kind", "error")
                    if code not in errors:
                        errors.append(code)
            truncated = truncated or more_p or more_f
            for item in items:
                if item.get("state") != "ACTIVE":
                    continue
                pid = str(item.get("projectId") or "").strip().lower()
                if not config.valid_project(pid) or pid in seen:
                    continue
                if len(found) >= limit:
                    truncated = True
                    break
                seen.add(pid)
                num_m = _NUMBER_RE.match(str(item.get("name") or ""))
                found.append({
                    "id": pid,
                    "name": str(item.get("displayName") or pid)[:120],
                    "parent": str(item.get("parent") or "")[:120] or None,
                    "number": num_m.group(1) if num_m else None,
                })
            for sub in subs:
                if sub in queued:
                    continue
                if depth >= MAX_DEPTH:
                    truncated = True
                    break
                queued.add(sub)
                queue.append((sub, depth + 1))

    LOG.info("scope discovery type=%s projects=%d folders=%d truncated=%s errors=%s",
             scope_type, len(found), folders, truncated, ",".join(errors) or "none")
    return {"projects": found, "truncated": truncated, "folders": folders, "errors": errors}


def _list_children(ctx, parent: str):
    projects = _list(ctx, "projects", parent, 50)
    items, more, err = _list(ctx, "folders", parent, 20)
    names = [item["name"] for item in items if item.get("state") == "ACTIVE"
             and isinstance(item.get("name"), str) and _FOLDER_RE.match(item["name"])]
    return projects, (names, more, err)


def _list(ctx, kind: str, parent: str, max_pages: int):
    try:
        items, more = ctx.gcp.paginate(
            "%s/%s" % (CRM, kind), quota_project=ctx.quota_project, items_key=kind,
            params={"parent": parent}, page_size=PAGE_SIZE, max_pages=max_pages,
        )
    except Exception as err:  # pylint: disable=broad-except
        return [], False, err
    return [i for i in items or () if isinstance(i, dict)], bool(more), None
