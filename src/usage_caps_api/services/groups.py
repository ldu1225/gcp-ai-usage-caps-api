"""User groups: first-class policy subjects with the same capabilities as individual users.

A group is a named set of people (explicit `members` plus optional members synced from a Google Group through the
Cloud Identity API) minus `excluded` people. Everything that can be configured for a user can be configured for a
group:

  - Spend caps   PUT /v1/caps/groups/{id}  type=limit (pool=per_member | shared), allow, block
  - Block        POST /v1/groups/{id}/block | /unblock (IAM Deny v2, all or selected managed projects)
  - Model rules  PUT /v1/groups/{id}/model-rules (allow/deny lists, per-model limits)
  - Exceptions   POST /v1/groups/{id}/exclusions (members that the group policy never applies to)
  - Usage        GET /v1/usage/groups[/{id}]

Resolution order (most specific wins):
    user rule  >  group rule (lowest `priority`, then group id)  >  global policy / default rule
A user-level `allow` rule also overrides a group block. Shared-pool caps are evaluated in addition to each member's
own cap: when the pooled usage of all effective members reaches the group cap, every member that has no user-level
`allow` rule is suspended (all managed projects, or only the over-cap projects for per-project pools).

Group policies are never written into the per-user records. `expand_state` derives the per-person view on the fly,
so removing a member, adding an exclusion or deleting a group rule takes effect immediately and is fully reversible.
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import re

from .. import config, identity, timeutil
from ..gcp import GcpError
from ..httpkit import ApiError, now_iso, paginate_list
from . import audit, webhooks

LOG = logging.getLogger("usage_caps_api.groups")

GROUPS_DOC = "groups"
GROUP_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
_GROUP_EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]{1,64}@[a-z0-9.\-]{1,253}\.[a-z]{2,63}$")
MAX_GROUPS = 500
MAX_MEMBERS = 5000
DEFAULT_PRIORITY = 100
POOLS = ("per_member", "shared")
CI_V1 = "https://cloudidentity.googleapis.com/v1"
SYNC_INTERVAL = dt.timedelta(hours=1)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

def load_groups(ctx) -> dict[str, dict]:
    doc, _ = ctx.store.load(GROUPS_DOC)
    return {gid: g for gid, g in (doc.get("groups") or {}).items() if isinstance(g, dict) and GROUP_ID_RE.match(gid)}


def effective_members(group: dict) -> list[str]:
    excluded = set(group.get("excluded") or [])
    people = set(group.get("members") or []) | set(group.get("synced_members") or [])
    return sorted(p for p in people if p not in excluded)


def membership(groups: dict[str, dict]) -> dict[str, list[str]]:
    """person -> [group ids ordered by (priority, id)]"""
    order = sorted(groups.items(), key=lambda kv: (int(kv[1].get("priority", DEFAULT_PRIORITY)), kv[0]))
    out: dict[str, list[str]] = {}
    for gid, g in order:
        for person in effective_members(g):
            out.setdefault(person, []).append(gid)
    return out


def members_by_group(groups: dict[str, dict]) -> dict[str, list[str]]:
    return {gid: effective_members(g) for gid, g in groups.items()}


def _expired(rec: dict | None, now: dt.datetime) -> bool:
    if not rec or not rec.get("until"):
        return False
    until = timeutil.parse_rfc3339(rec.get("until"))
    return until is not None and now >= until


def is_shared_pool(rule: dict | None) -> bool:
    return bool(rule and rule.get("type") == "limit" and rule.get("pool") == "shared")


def expand_state(ctx, state: dict, now: dt.datetime | None = None) -> dict:
    """Returns a copy of the user-caps state in which group rules / blocks are materialized per member.

    - A member without an active user rule inherits the first (by priority) group rule that is per-member
      (limit with pool=per_member, allow, or block). The derived rule carries `source: "group:<id>"`.
    - A member without an active user-level block inherits the first group block, unless the member has a
      user-level `allow` rule.
    - Shared-pool group limits are returned in `_group_pools` and evaluated by the caps engine.
    Idempotent: an already expanded state is returned unchanged."""
    if state.get("_groups_expanded"):
        return state
    now = now or utcnow()
    groups = load_groups(ctx)
    mem = membership(groups)
    grules = {g: r for g, r in (state.get("group_rules") or {}).items()
              if isinstance(r, dict) and g in groups and not _expired(r, now)}
    gblocks = {g: b for g, b in (state.get("group_blocks") or {}).items()
               if isinstance(b, dict) and g in groups and not _expired(b, now)}
    rules = dict(state.get("rules") or {})
    blocks = dict(state.get("blocks") or {})
    for person, gids in mem.items():
        urule = rules.get(person)
        if isinstance(urule, dict) and _expired(urule, now):
            urule = None
        if urule is None:
            for g in gids:
                gr = grules.get(g)
                if gr and not is_shared_pool(gr):
                    rules[person] = dict(copy.deepcopy(gr), email=person, source="group:" + g, group=g)
                    break
        user_allow = isinstance(urule, dict) and urule.get("type") == "allow"
        ublock = blocks.get(person)
        if isinstance(ublock, dict) and _expired(ublock, now):
            ublock = None
        if ublock is None and not user_allow:
            for g in gids:
                if g in gblocks:
                    blocks[person] = dict(copy.deepcopy(gblocks[g]), email=person, source="group:" + g, group=g)
                    break
    pools = {g: r for g, r in grules.items() if is_shared_pool(r)}
    return dict(state, rules=rules, blocks=blocks, _groups_expanded=True, _membership=mem,
                _group_members=members_by_group(groups), _group_pools=pools)



# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _gid(ctx) -> str:
    gid = str(ctx.path_params.get("group_id") or "").strip().lower()
    if not GROUP_ID_RE.match(gid):
        raise ApiError("bad_request", "Path parameter 'group_id' must match ^[a-z][a-z0-9-]{1,62}$.")
    return gid


def _require_group(ctx, gid: str) -> dict:
    g = load_groups(ctx).get(gid)
    if g is None:
        raise ApiError("not_found", "Group '%s' does not exist." % gid)
    return g


def _people(raw, field: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_MEMBERS:
        raise ApiError("bad_request", "Field '%s' must be a list of up to %d emails / workforce principals."
                       % (field, MAX_MEMBERS))
    out = []
    for item in raw:
        key = identity.person_key(item)
        if not key:
            raise ApiError("bad_request", "Invalid person '%s' in '%s' (email or workforce principal expected)."
                           % (str(item)[:80], field))
        if key not in out:
            out.append(key)
    return out


def _priority(raw) -> int:
    if raw is None:
        return DEFAULT_PRIORITY
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0 or raw > 100000:
        raise ApiError("bad_request", "Field 'priority' must be an integer between 0 and 100000 (lower wins).")
    return raw


def _google_group(raw) -> str | None:
    if raw in (None, ""):
        return None
    val = str(raw).strip().lower()
    if not _GROUP_EMAIL_RE.match(val):
        raise ApiError("bad_request", "Field 'google_group' must be a Google Group email address.")
    return val


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def _group_view(ctx, gid: str, g: dict, *, caps_doc: dict | None = None, rules_doc: dict | None = None) -> dict:
    if caps_doc is None:
        from . import caps as caps_svc
        caps_doc, _ = ctx.store.load(caps_svc.USER_CAPS_DOC)
    if rules_doc is None:
        from . import user_model_rules
        rules_doc, _ = ctx.store.load(user_model_rules.RULES_DOC)
    eff = effective_members(g)
    return {
        "object": "group",
        "id": gid,
        "name": g.get("name") or gid,
        "description": g.get("description") or "",
        "priority": int(g.get("priority", DEFAULT_PRIORITY)),
        "members": sorted(g.get("members") or []),
        "google_group": g.get("google_group"),
        "synced_members": sorted(g.get("synced_members") or []),
        "synced_at": g.get("synced_at"),
        "sync_error": g.get("sync_error"),
        "excluded": sorted(g.get("excluded") or []),
        "effective_members": eff,
        "member_count": len(eff),
        "cap_rule": (caps_doc.get("group_rules") or {}).get(gid),
        "block": (caps_doc.get("group_blocks") or {}).get(gid),
        "model_rule": (rules_doc.get("group_rules") or {}).get(gid),
        "created_at": g.get("created_at"),
        "updated_at": g.get("updated_at"),
        "updated_by": g.get("updated_by"),
    }


# ---------------------------------------------------------------------------
# Group CRUD: /v1/groups
# ---------------------------------------------------------------------------

def list_groups(ctx) -> dict:
    """GET /v1/groups?member=&limit=&cursor="""
    from . import caps as caps_svc, user_model_rules
    groups = load_groups(ctx)
    caps_doc, _ = ctx.store.load(caps_svc.USER_CAPS_DOC)
    rules_doc, _ = ctx.store.load(user_model_rules.RULES_DOC)
    rows = [_group_view(ctx, gid, g, caps_doc=caps_doc, rules_doc=rules_doc)
            for gid, g in sorted(groups.items(), key=lambda kv: (int(kv[1].get("priority", DEFAULT_PRIORITY)), kv[0]))]
    member = identity.person_key(ctx.params.get("member") or "") if ctx.params.get("member") else None
    if ctx.params.get("member") and not member:
        raise ApiError("bad_request", "Parameter 'member' must be a valid email or workforce principal.")
    if member:
        rows = [r for r in rows if member in r["effective_members"]]
    return paginate_list(rows, ctx.params, default_limit=100, max_limit=500)


def create_group(ctx) -> dict:
    """POST /v1/groups {id, name?, description?, priority?, members?, google_group?, excluded?}"""
    body = ctx.body or {}
    gid = str(body.get("id") or "").strip().lower()
    if not GROUP_ID_RE.match(gid):
        raise ApiError("bad_request", "Field 'id' is required and must match ^[a-z][a-z0-9-]{1,62}$.")
    rec = {
        "id": gid,
        "name": str(body.get("name") or gid)[:120],
        "description": str(body.get("description") or "")[:500],
        "priority": _priority(body.get("priority")),
        "members": _people(body.get("members"), "members"),
        "google_group": _google_group(body.get("google_group")),
        "synced_members": [],
        "synced_at": None,
        "sync_error": None,
        "excluded": _people(body.get("excluded"), "excluded"),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "updated_by": ctx.caller.id,
    }

    def save(doc):
        groups = dict(doc.get("groups") or {})
        if gid in groups:
            raise ApiError("conflict", "Group '%s' already exists." % gid, status=409)
        if len(groups) >= MAX_GROUPS:
            raise ApiError("bad_request", "At most %d groups are supported." % MAX_GROUPS)
        groups[gid] = rec
        doc["groups"] = groups
        return rec

    ctx.store.mutate(GROUPS_DOC, save)
    sync = None
    if rec["google_group"]:
        sync = _sync_one(ctx, gid)
    audit.write_audit(ctx, "group.created", target="group:" + gid,
                      details={"members": len(rec["members"]), "google_group": rec["google_group"]})
    out = _group_view(ctx, gid, load_groups(ctx)[gid])
    if sync is not None:
        out["sync"] = sync
    return out


def get_group(ctx) -> dict:
    """GET /v1/groups/{group_id}"""
    gid = _gid(ctx)
    return _group_view(ctx, gid, _require_group(ctx, gid))


def patch_group(ctx) -> dict:
    """PATCH /v1/groups/{group_id} {name?, description?, priority?, members? (replace), google_group?, excluded?}"""
    gid = _gid(ctx)
    before = _require_group(ctx, gid)
    body = ctx.body or {}
    updates: dict = {}
    if "name" in body:
        updates["name"] = str(body.get("name") or gid)[:120]
    if "description" in body:
        updates["description"] = str(body.get("description") or "")[:500]
    if "priority" in body:
        updates["priority"] = _priority(body.get("priority"))
    if "members" in body:
        updates["members"] = _people(body.get("members"), "members")
    if "excluded" in body:
        updates["excluded"] = _people(body.get("excluded"), "excluded")
    if "google_group" in body:
        updates["google_group"] = _google_group(body.get("google_group"))
        if not updates["google_group"]:
            updates.update(synced_members=[], synced_at=None, sync_error=None)
    if not updates:
        raise ApiError("bad_request", "No updatable fields supplied.")

    def save(doc):
        groups = dict(doc.get("groups") or {})
        if gid not in groups or not isinstance(groups.get(gid), dict):
            raise ApiError("not_found", "Group '%s' does not exist." % gid)
        g = dict(groups[gid])
        g.update(updates, updated_at=now_iso(), updated_by=ctx.caller.id)
        groups[gid] = g
        doc["groups"] = groups
        return g

    after = ctx.store.mutate(GROUPS_DOC, save)
    if updates.get("google_group") and updates["google_group"] != before.get("google_group"):
        _sync_one(ctx, gid)
        after = load_groups(ctx)[gid]
    enforcement = _after_membership_change(ctx, gid, before, after)
    audit.write_audit(ctx, "group.updated", target="group:" + gid, details={"fields": sorted(updates)})
    out = _group_view(ctx, gid, load_groups(ctx)[gid])
    out["enforcement"] = enforcement
    return out


def delete_group(ctx) -> dict:
    """DELETE /v1/groups/{group_id}: removes the group and every group policy (caps, block, model rule)."""
    from . import caps as caps_svc, user_model_rules
    gid = _gid(ctx)
    before = _require_group(ctx, gid)

    def drop(doc):
        groups = dict(doc.get("groups") or {})
        groups.pop(gid, None)
        doc["groups"] = groups
        return doc

    with caps_svc._WRITE_LOCK:  # pylint: disable=protected-access
        ctx.store.mutate(GROUPS_DOC, drop)

        def drop_caps(doc):
            for section in ("group_rules", "group_blocks"):
                sec = dict(doc.get(section) or {})
                sec.pop(gid, None)
                doc[section] = sec
            doc["suspensions"] = _drop_group_suspensions(doc.get("suspensions") or {}, effective_members(before), gid)
            return doc
        doc = ctx.store.mutate(caps_svc.USER_CAPS_DOC, drop_caps)

        def drop_rules(rdoc):
            sec = dict(rdoc.get("group_rules") or {})
            sec.pop(gid, None)
            rdoc["group_rules"] = sec
            return rdoc
        ctx.store.mutate(user_model_rules.RULES_DOC, drop_rules)
        enforcement = _reconcile(ctx, doc, {p: {gid} for p in effective_members(before)})
    audit.write_audit(ctx, "group.deleted", target="group:" + gid,
                      details={"members": len(effective_members(before)), "enforcement": enforcement})
    return {"object": "group", "id": gid, "deleted": True, "enforcement": enforcement}


def _edit_list(ctx, field: str, event: str) -> dict:
    gid = _gid(ctx)
    before = _require_group(ctx, gid)
    body = ctx.body or {}
    add = _people(body.get("add"), "add")
    remove = _people(body.get("remove"), "remove")
    if not add and not remove:
        raise ApiError("bad_request", "Provide at least one person in 'add' or 'remove'.")

    def save(doc):
        groups = dict(doc.get("groups") or {})
        if gid not in groups or not isinstance(groups.get(gid), dict):
            raise ApiError("not_found", "Group '%s' does not exist." % gid)
        g = dict(groups[gid])
        cur = [p for p in (g.get(field) or []) if p not in set(remove)]
        for p in add:
            if p not in cur:
                cur.append(p)
        if len(cur) > MAX_MEMBERS:
            raise ApiError("bad_request", "A group supports at most %d entries in '%s'." % (MAX_MEMBERS, field))
        g[field] = sorted(cur)
        g.update(updated_at=now_iso(), updated_by=ctx.caller.id)
        groups[gid] = g
        doc["groups"] = groups
        return g

    after = ctx.store.mutate(GROUPS_DOC, save)
    enforcement = _after_membership_change(ctx, gid, before, after)
    added = sorted(set(effective_members(after)) - set(effective_members(before)))
    removed = sorted(set(effective_members(before)) - set(effective_members(after)))
    audit.write_audit(ctx, event, target="group:" + gid,
                      details={"add": add, "remove": remove, "effective_added": added, "effective_removed": removed,
                               "enforcement": enforcement})
    if added or removed:
        webhooks.dispatch_event(ctx, "group.membership_changed", {
            "group": gid, "added": added, "removed": removed, "trigger": field, "actor": ctx.caller.id})
    out = _group_view(ctx, gid, load_groups(ctx)[gid])
    out.update(effective_added=added, effective_removed=removed, enforcement=enforcement)
    return out


def update_members(ctx) -> dict:
    """POST /v1/groups/{group_id}/members {add?: [...], remove?: [...]}"""
    return _edit_list(ctx, "members", "group.members.updated")


def update_exclusions(ctx) -> dict:
    """POST /v1/groups/{group_id}/exclusions {add?: [...], remove?: [...]}: people the group policy never applies to."""
    return _edit_list(ctx, "excluded", "group.exclusions.updated")


def get_user_groups(ctx) -> dict:
    """GET /v1/users/{email}/groups: groups of a person (effective, excluded) and the resulting policy sources."""
    from . import caps as caps_svc, user_model_rules
    email = identity.person_key(ctx.path_params.get("email") or "")
    if not email:
        raise ApiError("bad_request", "Path parameter 'email' must be a valid email or workforce principal.")
    groups = load_groups(ctx)
    mem = membership(groups)
    state = caps_svc._load_user_caps_state(ctx)  # pylint: disable=protected-access
    raw_doc, _ = ctx.store.load(caps_svc.USER_CAPS_DOC)
    rules_state = user_model_rules.load_rules(ctx)
    m_rule, m_source = user_model_rules.effective_rule(rules_state, email)
    rows = []
    for gid in mem.get(email, []):
        g = groups[gid]
        rows.append({"id": gid, "name": g.get("name") or gid, "priority": int(g.get("priority", DEFAULT_PRIORITY)),
                     "via": "member" if email in (g.get("members") or []) else "google_group",
                     "cap_rule": (raw_doc.get("group_rules") or {}).get(gid),
                     "block": (raw_doc.get("group_blocks") or {}).get(gid)})
    excluded_from = sorted(gid for gid, g in groups.items() if email in (g.get("excluded") or []))
    rule = state["rules"].get(email)
    blk = state["blocks"].get(email)
    return {
        "object": "user_groups",
        "email": email,
        "groups": rows,
        "excluded_from": excluded_from,
        "effective_cap_rule_source": (rule or {}).get("source") or ("user" if rule else "policy"),
        "effective_block_source": ((blk or {}).get("source") or "manual") if blk else None,
        "effective_model_rule_source": m_source,
        "effective_model_rule": m_rule,
        "shared_pools": sorted(g for g in state.get("_group_pools") or {} if email in
                               (state.get("_group_members") or {}).get(g, [])),
    }


# ---------------------------------------------------------------------------
# Google Group sync (Cloud Identity API)
# ---------------------------------------------------------------------------

def _ci_members(ctx, group_email: str) -> list[str]:
    look = ctx.rest("GET", CI_V1 + "/groups:lookup", params={"groupKey.id": group_email}) or {}
    name = look.get("name")
    if not name or not str(name).startswith("groups/"):
        raise GcpError("not_found", status=404, message="Google Group not found")
    people: list[str] = []
    token = None
    try:
        for _ in range(50):
            params = {"pageSize": 500}
            if token:
                params["pageToken"] = token
            page = ctx.rest("GET", "%s/%s/memberships:searchTransitiveMemberships" % (CI_V1, name), params=params) or {}
            for m in page.get("memberships") or []:
                for key in m.get("preferredMemberKey") or []:
                    people.append(str((key or {}).get("id") or ""))
            token = page.get("nextPageToken")
            if not token:
                break
    except GcpError:
        people, token = [], None
        for _ in range(50):
            params = {"pageSize": 500, "view": "FULL"}
            if token:
                params["pageToken"] = token
            page = ctx.rest("GET", "%s/%s/memberships" % (CI_V1, name), params=params) or {}
            for m in page.get("memberships") or []:
                if str(m.get("type") or "USER").upper() not in ("USER", "TYPE_UNSPECIFIED"):
                    continue
                people.append(str((m.get("preferredMemberKey") or {}).get("id") or ""))
            token = page.get("nextPageToken")
            if not token:
                break
    out = []
    for p in people:
        key = identity.person_key(p)
        if key and not key.endswith(".gserviceaccount.com") and key != group_email and key not in out:
            out.append(key)
    return sorted(out)[:MAX_MEMBERS]


def _sync_one(ctx, gid: str) -> dict:
    groups = load_groups(ctx)
    g = groups.get(gid)
    if not g or not g.get("google_group"):
        return {"group": gid, "synced": False, "reason": "no_google_group"}
    before = dict(g)
    try:
        synced = _ci_members(ctx, g["google_group"])
        err = None
    except GcpError as exc:
        synced, err = None, exc.kind

    def save(doc):
        gs = dict(doc.get("groups") or {})
        cur = dict(gs.get(gid) or {})
        if not cur:
            return cur
        if synced is not None:
            cur["synced_members"] = synced
            cur["sync_error"] = None
        else:
            cur["sync_error"] = err
        cur["synced_at"] = now_iso()
        gs[gid] = cur
        doc["groups"] = gs
        return cur

    after = ctx.store.mutate(GROUPS_DOC, save)
    if synced is None:
        audit.write_audit(ctx, "group.sync_failed", target="group:" + gid, details={"error": err})
        return {"group": gid, "synced": False, "error": err,
                "hint": "Enable the Cloud Identity API and grant the runtime service account the Groups Reader "
                        "role (Google Admin console > Account > Admin roles)."}
    added = sorted(set(effective_members(after)) - set(effective_members(before)))
    removed = sorted(set(effective_members(before)) - set(effective_members(after)))
    enforcement = _after_membership_change(ctx, gid, before, after) if (added or removed) else None
    if added or removed:
        audit.write_audit(ctx, "group.synced", target="group:" + gid,
                          details={"added": added, "removed": removed, "enforcement": enforcement})
        webhooks.dispatch_event(ctx, "group.membership_changed", {
            "group": gid, "added": added, "removed": removed, "trigger": "google_group_sync"})
    return {"group": gid, "synced": True, "members": len(synced), "added": added, "removed": removed,
            "enforcement": enforcement}


def sync_group(ctx) -> dict:
    """POST /v1/groups/{group_id}/sync"""
    gid = _gid(ctx)
    _require_group(ctx, gid)
    return dict(_sync_one(ctx, gid), object="group_sync")


def sync_all(ctx, *, force: bool = False) -> dict:
    """Hourly Google Group sync (governance cycle)."""
    now = utcnow()
    out = {"checked": 0, "changed": 0, "failed": 0}
    for gid, g in load_groups(ctx).items():
        if not g.get("google_group"):
            continue
        last = timeutil.parse_rfc3339(g.get("synced_at")) if g.get("synced_at") else None
        if not force and last is not None and now - last < SYNC_INTERVAL:
            continue
        out["checked"] += 1
        res = _sync_one(ctx, gid)
        if not res.get("synced"):
            out["failed"] += 1
        elif res.get("added") or res.get("removed"):
            out["changed"] += 1
    return out


# ---------------------------------------------------------------------------
# Enforcement hooks
# ---------------------------------------------------------------------------

def _drop_group_suspensions(susps: dict, people, gid: str) -> dict:
    """Removes suspensions that exist only because of group `gid`'s shared pool."""
    out = dict(susps)
    tag = "group:" + gid
    for p in people:
        s = out.get(p)
        if isinstance(s, dict):
            reasons = [r for r in (s.get("reasons") or []) if isinstance(r, str)]
            if reasons and all(r == tag for r in reasons if r.startswith("group:")) and \
                    all(r.startswith("group:") for r in reasons):
                out.pop(p, None)
    return out


def _has_enforced_policy(ctx, gid: str) -> bool:
    from . import caps as caps_svc
    doc, _ = ctx.store.load(caps_svc.USER_CAPS_DOC)
    rule = (doc.get("group_rules") or {}).get(gid) or {}
    return gid in (doc.get("group_blocks") or {}) or rule.get("type") in ("block", "allow") or is_shared_pool(rule)


def _reconcile(ctx, doc: dict, group_restores: dict, *, deny_client=None) -> dict | None:
    from . import caps as caps_svc
    policy = caps_svc.effective_policy(ctx)
    try:
        return caps_svc._reconcile_direct_change(ctx, policy, doc, deny_client=deny_client,  # pylint: disable=protected-access
                                                 group_restores=group_restores)
    except GcpError as err:
        return {"applied": False, "error": err.kind}


def _after_membership_change(ctx, gid: str, before: dict, after: dict) -> dict | None:
    """Applies membership changes to IAM Deny immediately when the group carries an enforced policy."""
    from . import caps as caps_svc
    removed = sorted(set(effective_members(before)) - set(effective_members(after)))
    added = sorted(set(effective_members(after)) - set(effective_members(before)))
    if not (removed or added) or not _has_enforced_policy(ctx, gid):
        return None
    with caps_svc._WRITE_LOCK:  # pylint: disable=protected-access
        def save(doc):
            doc["suspensions"] = _drop_group_suspensions(doc.get("suspensions") or {}, removed, gid)
            return doc
        doc = ctx.store.mutate(caps_svc.USER_CAPS_DOC, save)
        res = _reconcile(ctx, doc, {p: {gid} for p in removed})
    caps_svc._invalidate_caps_caches(ctx)  # pylint: disable=protected-access
    return res


def block_group(ctx, *, deny_client=None) -> dict:
    """POST /v1/groups/{group_id}/block {services?, projects?, reason?, until?}"""
    from . import caps as caps_svc
    gid = _gid(ctx)
    g = _require_group(ctx, gid)
    body = ctx.body or {}
    policy = caps_svc.effective_policy(ctx)
    services = caps_svc._validate_services(body.get("services"), default=policy.get("default_block_services"))  # pylint: disable=protected-access
    projects = caps_svc._validate_projects(ctx, body.get("projects"))  # pylint: disable=protected-access
    reason = str(body.get("reason") or body.get("note") or "")[:200]
    until = body.get("until") or None
    if until is not None and timeutil.parse_rfc3339(until) is None:
        raise ApiError("bad_request", "Field 'until' must be an RFC3339 timestamp.")
    blk = {"group": gid, "services": services, "projects": projects, "until": until, "reason": reason,
           "updated_at": now_iso(), "updated_by": ctx.caller.id}
    with caps_svc._WRITE_LOCK:  # pylint: disable=protected-access
        def save(doc):
            sec = dict(doc.get("group_blocks") or {})
            sec[gid] = blk
            doc["group_blocks"] = sec
            return doc
        doc = ctx.store.mutate(caps_svc.USER_CAPS_DOC, save)
        enforcement = _reconcile(ctx, doc, {}, deny_client=deny_client)
    caps_svc._invalidate_caps_caches(ctx)  # pylint: disable=protected-access
    members = effective_members(g)
    expanded = expand_state(ctx, caps_svc._load_user_caps_state(ctx))  # pylint: disable=protected-access
    applied_to = sorted(p for p in members if (expanded["blocks"].get(p) or {}).get("source") == "group:" + gid)
    skipped = sorted(set(members) - set(applied_to))
    audit.write_audit(ctx, "group.blocked", target="group:" + gid,
                      details={"services": services, "projects": projects, "reason": reason, "until": until,
                               "members": len(members), "enforcement": enforcement})
    webhooks.dispatch_event(ctx, "group.blocked", {
        "group": gid, "services": services, "projects": projects or "all", "reason": reason, "until": until,
        "members": applied_to, "actor": ctx.caller.id})
    return {"object": "group_block", "group": gid, "status": "blocked", "blocked_services": services,
            "projects": projects, "scope": "projects" if projects else "all", "reason": reason or None,
            "until": until, "members_blocked": applied_to,
            "members_not_affected": skipped, "enforcement": enforcement}


def unblock_group(ctx, *, deny_client=None) -> dict:
    """POST /v1/groups/{group_id}/unblock {reason?}"""
    from . import caps as caps_svc
    gid = _gid(ctx)
    g = _require_group(ctx, gid)
    reason = str((ctx.body or {}).get("reason") or "")[:200]
    holder = {"had_block": False, "had_rule_block": False}
    with caps_svc._WRITE_LOCK:  # pylint: disable=protected-access
        def save(doc):
            sec = dict(doc.get("group_blocks") or {})
            holder["had_block"] = sec.pop(gid, None) is not None
            doc["group_blocks"] = sec
            rules = dict(doc.get("group_rules") or {})
            if (rules.get(gid) or {}).get("type") == "block":
                rules.pop(gid, None)
                holder["had_rule_block"] = True
            doc["group_rules"] = rules
            doc["suspensions"] = _drop_group_suspensions(doc.get("suspensions") or {}, effective_members(g), gid)
            return doc
        doc = ctx.store.mutate(caps_svc.USER_CAPS_DOC, save)
        enforcement = _reconcile(ctx, doc, {p: {gid} for p in effective_members(g)}, deny_client=deny_client)
    caps_svc._invalidate_caps_caches(ctx)  # pylint: disable=protected-access
    if not (holder["had_block"] or holder["had_rule_block"]):
        LOG.info("group %s unblock: no active group block", gid)
    expanded = expand_state(ctx, caps_svc._load_user_caps_state(ctx))  # pylint: disable=protected-access
    still = sorted(p for p in effective_members(g) if expanded["blocks"].get(p) or
                   (expanded["rules"].get(p) or {}).get("type") == "block")
    audit.write_audit(ctx, "group.unblocked", target="group:" + gid,
                      details={"reason": reason, "members": len(effective_members(g)), "still_blocked": still,
                               "enforcement": enforcement})
    webhooks.dispatch_event(ctx, "group.unblocked", {"group": gid, "reason": reason, "actor": ctx.caller.id,
                                                     "members_still_blocked": still})
    return {"object": "group_unblock", "group": gid, "status": "active",
            "had_block": holder["had_block"] or holder["had_rule_block"],
            "members_still_blocked": still, "reason": reason or None, "enforcement": enforcement}


# ---------------------------------------------------------------------------
# Group spend caps: /v1/caps/groups
# ---------------------------------------------------------------------------

def validate_group_rule(ctx, gid: str, body: dict) -> dict:
    from . import caps as caps_svc
    rtype = str(body.get("type") or "limit").strip().lower()
    if rtype not in ("limit", "allow", "block"):
        raise ApiError("bad_request", "Field 'type' must be 'limit', 'allow', or 'block'.")
    until = body.get("until") or None
    if until is not None and timeutil.parse_rfc3339(until) is None:
        raise ApiError("bad_request", "Field 'until' must be an RFC3339 timestamp.")
    rule = {"group": gid, "type": rtype, "until": until, "note": str(body.get("note") or "")[:200],
            "updated_at": now_iso(), "updated_by": ctx.caller.id}
    if rtype == "limit":
        c = caps_svc._opt_nonneg_float(body.get("cost_cap"), "cost_cap")  # pylint: disable=protected-access
        t = caps_svc._opt_nonneg_int(body.get("token_cap"), "token_cap")  # pylint: disable=protected-access
        r = caps_svc._opt_nonneg_int(body.get("request_cap"), "request_cap", hi=10 ** 9)  # pylint: disable=protected-access
        pool = str(body.get("pool") or "per_member").strip().lower()
        if pool not in POOLS:
            raise ApiError("bad_request", "Field 'pool' must be 'per_member' or 'shared'.")
        scope = body.get("scope")
        if scope is not None and scope not in caps_svc.CAP_SCOPES:
            raise ApiError("bad_request", "Field 'scope' must be 'total' or 'per_project'.")
        pcaps = caps_svc._validate_project_caps(ctx, body.get("project_caps"), "project_caps")  # pylint: disable=protected-access
        if c is None and t is None and r is None and not pcaps:
            raise ApiError("bad_request", "At least one of cost_cap, token_cap, request_cap or project_caps is "
                                          "required for type='limit'.")
        rule.update(cost_cap=c, token_cap=t, request_cap=r, pool=pool, scope=scope, project_caps=pcaps)
        if "services" in body:
            rule["services"] = caps_svc._validate_services(body["services"])  # pylint: disable=protected-access
    elif rtype == "block":
        policy = caps_svc.effective_policy(ctx)
        rule["services"] = caps_svc._validate_services(body.get("services"),  # pylint: disable=protected-access
                                                       default=policy.get("default_block_services"))
        rule["projects"] = caps_svc._validate_projects(ctx, body.get("projects"))  # pylint: disable=protected-access
    return rule


def _pool_rows(payload: dict) -> dict:
    return {p["group"]: p for p in payload.get("group_pools") or []}


def _group_cap_view(gid: str, g: dict, payload: dict, raw_doc: dict) -> dict:
    members = set(effective_members(g))
    rows = [u for u in payload["users"] if u["email"] in members]
    counts = {s: sum(1 for u in rows if u["status"] == s) for s in ("active", "over_cap", "suspended", "blocked")}
    counts["active"] += len(members) - len(rows)
    cost_vals = [u["usage"]["estimated_cost"] for u in rows if u["usage"]["estimated_cost"] is not None]
    pooled = {"requests": sum(u["usage"]["requests"] for u in rows),
              "total_tokens": sum(u["usage"]["total_tokens"] for u in rows),
              "estimated_cost": round(sum(cost_vals), 6) if cost_vals else (0.0 if not rows else None)}
    pool = _pool_rows(payload).get(gid)
    rule = (raw_doc.get("group_rules") or {}).get(gid)
    return {
        "object": "group_cap",
        "group": gid,
        "name": g.get("name") or gid,
        "priority": int(g.get("priority", DEFAULT_PRIORITY)),
        "rule": rule,
        "block": (raw_doc.get("group_blocks") or {}).get(gid),
        "pool": (rule or {}).get("pool") if (rule or {}).get("type") == "limit" else None,
        "member_count": len(members),
        "member_status": counts,
        "usage": pool["usage"] if pool else pooled,
        "limit": pool["limit"] if pool else None,
        "remaining": pool["remaining"] if pool else None,
        "utilization_pct": pool["utilization_pct"] if pool else None,
        "over_cap": pool["over_cap"] if pool else None,
        "over_projects": pool["over_projects"] if pool else [],
        "members": [{"email": u["email"], "status": u["status"], "rule_source": u.get("rule_source"),
                     "utilization_pct": u.get("utilization_pct"), "estimated_cost": u["usage"]["estimated_cost"]}
                    for u in rows],
        "period": payload["period"],
    }


def list_group_caps(ctx) -> dict:
    """GET /v1/caps/groups"""
    from . import caps as caps_svc
    fresh = str(ctx.params.get("fresh") or "").lower() in ("1", "true")
    payload, _ = caps_svc.compute_caps_status(ctx, fresh=fresh)
    raw_doc, _ = ctx.store.load(caps_svc.USER_CAPS_DOC)
    rows = [_group_cap_view(gid, g, payload, raw_doc) for gid, g in
            sorted(load_groups(ctx).items(), key=lambda kv: (int(kv[1].get("priority", DEFAULT_PRIORITY)), kv[0]))]
    page = paginate_list(rows, ctx.params, default_limit=100, max_limit=500)
    page["period"] = payload["period"]
    return page


def get_group_cap(ctx) -> dict:
    """GET /v1/caps/groups/{group_id}"""
    from . import caps as caps_svc
    gid = _gid(ctx)
    g = _require_group(ctx, gid)
    fresh = str(ctx.params.get("fresh") or "").lower() in ("1", "true")
    payload, _ = caps_svc.compute_caps_status(ctx, fresh=fresh)
    raw_doc, _ = ctx.store.load(caps_svc.USER_CAPS_DOC)
    return _group_cap_view(gid, g, payload, raw_doc)


def put_group_cap(ctx) -> dict:
    """PUT /v1/caps/groups/{group_id}"""
    from . import caps as caps_svc
    gid = _gid(ctx)
    g = _require_group(ctx, gid)
    rule = validate_group_rule(ctx, gid, ctx.body or {})
    with caps_svc._WRITE_LOCK:  # pylint: disable=protected-access
        prev = {}

        def save(doc):
            sec = dict(doc.get("group_rules") or {})
            prev["rule"] = sec.get(gid)
            sec[gid] = rule
            doc["group_rules"] = sec
            return doc
        doc = ctx.store.mutate(caps_svc.USER_CAPS_DOC, save)
        enforcement = None
        if rule["type"] in ("block", "allow") or (prev.get("rule") or {}).get("type") in ("block", "allow"):
            enforcement = _reconcile(ctx, doc, {p: {gid} for p in effective_members(g)})
    caps_svc._invalidate_caps_caches(ctx)  # pylint: disable=protected-access
    audit.write_audit(ctx, "caps.group.updated", target="group:" + gid, details={"type": rule["type"], "rule": rule})
    return {"object": "group_cap_rule", "group": gid, "rule": rule, "enforcement": enforcement}


def delete_group_cap(ctx) -> dict:
    """DELETE /v1/caps/groups/{group_id}: members fall back to their own rule or the global policy."""
    from . import caps as caps_svc
    gid = _gid(ctx)
    g = _require_group(ctx, gid)
    with caps_svc._WRITE_LOCK:  # pylint: disable=protected-access
        holder = {}

        def remove(doc):
            sec = dict(doc.get("group_rules") or {})
            holder["rule"] = sec.pop(gid, None)
            doc["group_rules"] = sec
            doc["suspensions"] = _drop_group_suspensions(doc.get("suspensions") or {}, effective_members(g), gid)
            return doc
        doc = ctx.store.mutate(caps_svc.USER_CAPS_DOC, remove)
        if holder["rule"] is None:
            raise ApiError("not_found", "No spend cap rule found for group '%s'." % gid)
        enforcement = _reconcile(ctx, doc, {p: {gid} for p in effective_members(g)})
    caps_svc._invalidate_caps_caches(ctx)  # pylint: disable=protected-access
    audit.write_audit(ctx, "caps.group.deleted", target="group:" + gid)
    return {"object": "group_cap_rule", "group": gid, "deleted": True, "enforcement": enforcement}


def pool_status(state: dict, usage_res, pricing, policy: dict, cap_check, estimate_cost) -> dict[str, dict]:
    """Evaluates every shared-pool group cap against the pooled usage of its effective members."""
    out: dict[str, dict] = {}
    pools = state.get("_group_pools") or {}
    members_map = state.get("_group_members") or {}
    currency = pricing.currency
    for gid, rule in pools.items():
        members = members_map.get(gid) or []
        recs = [usage_res.users.get(p) for p in members if usage_res.users.get(p)]
        tokens = sum(int(r.get("total_tokens") or 0) for r in recs)
        requests = sum(int(r.get("requests") or 0) for r in recs)
        costs = [estimate_cost(r, pricing)[0] for r in recs] if usage_res.ok else []
        cost = round(sum(c for c in costs if c is not None), 6) if usage_res.ok and all(c is not None for c in costs) else None
        c_cap = float(rule.get("cost_cap") or 0.0)
        t_cap = int(rule.get("token_cap") or 0)
        r_cap = int(rule.get("request_cap") or 0)
        scope = rule.get("scope") or "total"
        over_t, ratio, reasons = (cap_check(tokens, requests, t_cap, r_cap, cost, c_cap) if (usage_res.ok and
                                                                                            scope == "total")
                                  else (False if usage_res.ok else None, None, []))
        by_proj: dict[str, dict] = {}
        for r in recs:
            for p, prec in (r.get("by_project") or {}).items():
                if p == "unknown":
                    continue
                agg = by_proj.setdefault(p, {"requests": 0, "total_tokens": 0, "recs": []})
                agg["requests"] += int(prec.get("requests") or 0)
                agg["total_tokens"] += int(prec.get("total_tokens") or 0)
                agg["recs"].append(prec)
        pcaps = rule.get("project_caps") or {}
        over_projects, ratios = [], [ratio] if ratio is not None else []
        project_rows = []
        for p in sorted(set(by_proj) | set(pcaps)):
            agg = by_proj.get(p) or {"requests": 0, "total_tokens": 0, "recs": []}
            lim = pcaps.get(p) or ({"cost_cap": c_cap, "token_cap": t_cap, "request_cap": r_cap}
                                   if scope == "per_project" else None)
            pc_vals = [estimate_cost(x, pricing)[0] for x in agg["recs"]] if usage_res.ok else []
            p_cost = round(sum(c for c in pc_vals if c is not None), 6) if usage_res.ok and \
                all(c is not None for c in pc_vals) else None
            if lim and usage_res.ok:
                p_over, p_ratio, p_reasons = cap_check(agg["total_tokens"], agg["requests"],
                                                       int(lim.get("token_cap") or 0), int(lim.get("request_cap") or 0),
                                                       p_cost, float(lim.get("cost_cap") or 0.0))
            else:
                p_over, p_ratio, p_reasons = False, None, []
            if p_over:
                over_projects.append(p)
            if p_ratio is not None:
                ratios.append(p_ratio)
            project_rows.append({"project": p, "requests": agg["requests"], "total_tokens": agg["total_tokens"],
                                 "estimated_cost": p_cost, "over_cap": p_over, "over_cap_reasons": p_reasons,
                                 "utilization_pct": round(p_ratio * 100.0, 2) if p_ratio is not None else None,
                                 "limit": lim})
        util = round(max(ratios) * 100.0, 2) if ratios else None
        out[gid] = {
            "group": gid,
            "members": list(members),
            "member_count": len(members),
            "scope": scope,
            "usage": {"requests": requests, "total_tokens": tokens, "estimated_cost": cost, "currency": currency},
            "limit": {"cost_cap": c_cap or None, "token_cap": t_cap or None, "request_cap": r_cap or None,
                      "currency": currency, "pool": "shared", "scope": scope},
            "remaining": {"cost": round(max(0.0, c_cap - cost), 6) if (c_cap and cost is not None and scope == "total") else None,
                          "tokens": max(0, t_cap - tokens) if (t_cap and scope == "total") else None,
                          "requests": max(0, r_cap - requests) if (r_cap and scope == "total") else None},
            "over_cap": (bool(over_t) or bool(over_projects)) if usage_res.ok else None,
            "over_all": bool(over_t),
            "over_cap_reasons": reasons,
            "over_projects": over_projects,
            "utilization_pct": util,
            "projects": project_rows,
        }
    return out


# ---------------------------------------------------------------------------
# Group model rules: /v1/groups/{id}/model-rules
# ---------------------------------------------------------------------------

def get_group_model_rule(ctx) -> dict:
    from . import user_model_rules
    gid = _gid(ctx)
    _require_group(ctx, gid)
    doc, _ = ctx.store.load(user_model_rules.RULES_DOC)
    return {"object": "model_rule", "scope": "group", "group": gid,
            "rule": (doc.get("group_rules") or {}).get(gid)}


def put_group_model_rule(ctx) -> dict:
    from . import user_model_rules
    gid = _gid(ctx)
    _require_group(ctx, gid)
    rule = user_model_rules.validate_rule(ctx, ctx.body or {})

    def save(doc):
        sec = dict(doc.get("group_rules") or {})
        sec[gid] = rule
        doc["group_rules"] = sec
        return doc
    ctx.store.mutate(user_model_rules.RULES_DOC, save)
    audit.write_audit(ctx, "group_model_rule.updated", target="group:" + gid, details=rule)
    return {"object": "model_rule", "scope": "group", "group": gid, "rule": rule}


def delete_group_model_rule(ctx) -> dict:
    from . import user_model_rules
    gid = _gid(ctx)
    _require_group(ctx, gid)

    def save(doc):
        sec = dict(doc.get("group_rules") or {})
        existed = sec.pop(gid, None) is not None
        doc["group_rules"] = sec
        return existed
    if not ctx.store.mutate(user_model_rules.RULES_DOC, save):
        raise ApiError("not_found", "No model rule found for group '%s'." % gid)
    audit.write_audit(ctx, "group_model_rule.deleted", target="group:" + gid)
    return {"object": "model_rule", "scope": "group", "group": gid, "deleted": True}


# ---------------------------------------------------------------------------
# Group usage: /v1/usage/groups
# ---------------------------------------------------------------------------

def _aggregate(usage_res, members: list[str], gid: str):
    from . import usage as usage_svc
    agg = usage_svc.UsageResult("group")
    for p in members:
        rec = usage_res.users.get(p)
        if rec:
            tmp = usage_svc.UsageResult("member")
            tmp.users[gid] = rec
            agg.merge(tmp)
    return agg.users.get(gid)


def _group_usage_row(gid: str, g: dict, usage_res, pricing) -> dict:
    from . import pricing as pricing_svc, usage as usage_svc
    members = effective_members(g)
    rec = _aggregate(usage_res, members, gid)
    active = [p for p in members if usage_res.users.get(p)]
    if rec is None:
        row = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0,
               "thoughts_tokens": 0, "total_tokens": 0, "estimated_cost": 0.0 if usage_res.ok else None,
               "cost_partial": False, "currency": pricing.currency, "by_model": [], "by_surface": {},
               "by_project": [], "last_active": None}
    else:
        full = usage_svc._format_user_row(gid, rec, pricing)  # pylint: disable=protected-access
        row = {k: v for k, v in full.items() if k not in ("email", "uid", "surfaces", "projects", "multi_project")}
    members_rows = []
    for p in active:
        cost, _ = pricing_svc.estimate_cost(usage_res.users[p], pricing)
        members_rows.append({"email": p, "requests": int(usage_res.users[p].get("requests") or 0),
                             "total_tokens": int(usage_res.users[p].get("total_tokens") or 0),
                             "estimated_cost": cost})
    members_rows.sort(key=lambda r: (-(r["estimated_cost"] or -1.0), -r["total_tokens"], r["email"]))
    return dict({"object": "group_usage", "group": gid, "name": g.get("name") or gid,
                 "member_count": len(members), "active_members": len(active)}, **row, members=members_rows)


def list_group_usage(ctx) -> dict:
    """GET /v1/usage/groups?start=&end=&surface="""
    from . import caps as caps_svc, pricing as pricing_svc, usage as usage_svc
    rng = ctx.range or timeutil.parse_range(ctx.params, ctx.tz_name, ctx.ws.get("default_range_days", 14))
    surface = config.normalize_service(ctx.params.get("surface") or "")
    if surface and surface not in ("all", *config.VALID_SERVICES):
        raise ApiError("bad_request", "Parameter 'surface' must be one of: all, %s." % ", ".join(config.VALID_SERVICES))
    policy = caps_svc.effective_policy(ctx)
    pricing = pricing_svc.resolve_pricing(ctx, policy)
    usage_res = usage_svc.collect_usage(ctx, rng.start_ts(), rng.end_ts(), surface=surface or None)
    rows = [_group_usage_row(gid, g, usage_res, pricing) for gid, g in sorted(load_groups(ctx).items())]
    rows.sort(key=lambda r: (-(r["estimated_cost"] if r["estimated_cost"] is not None else -1.0), r["group"]))
    page = paginate_list(rows, ctx.params, default_limit=100, max_limit=500)
    page["range"] = rng.meta()
    page["currency"] = pricing.currency
    page["telemetry"] = usage_svc.usage_meta(usage_res)
    return page


def get_group_usage(ctx) -> dict:
    """GET /v1/usage/groups/{group_id}?start=&end=&surface="""
    from . import caps as caps_svc, pricing as pricing_svc, usage as usage_svc
    gid = _gid(ctx)
    g = _require_group(ctx, gid)
    rng = ctx.range or timeutil.parse_range(ctx.params, ctx.tz_name, ctx.ws.get("default_range_days", 14))
    surface = config.normalize_service(ctx.params.get("surface") or "")
    if surface and surface not in ("all", *config.VALID_SERVICES):
        raise ApiError("bad_request", "Parameter 'surface' must be one of: all, %s." % ", ".join(config.VALID_SERVICES))
    policy = caps_svc.effective_policy(ctx)
    pricing = pricing_svc.resolve_pricing(ctx, policy)
    usage_res = usage_svc.collect_usage(ctx, rng.start_ts(), rng.end_ts(), surface=surface or None, rng=rng)
    row = _group_usage_row(gid, g, usage_res, pricing)
    members = effective_members(g)
    timeseries = []
    for bucket_date in rng.buckets():
        req = tok = 0
        for p in members:
            d = usage_res.daily.get((p, bucket_date)) or {}
            req += int(d.get("requests") or 0)
            tok += int(d.get("total_tokens") or 0)
        timeseries.append({"date": bucket_date.isoformat(), "requests": req, "total_tokens": tok})
    return dict(row, range=rng.meta(), timeseries=timeseries, telemetry=usage_svc.usage_meta(usage_res))
