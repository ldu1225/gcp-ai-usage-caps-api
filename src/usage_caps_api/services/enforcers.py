"""IAM Deny v2 enforcement with per-service selective blocking and strict safety invariants.

Manages ONE deny policy per attachment point (projects, folders, or organization) with a fixed
prefix (`gcp-ai-caps-`). Inside that policy, up to 3 rules map cleanly to the selectable AI services:
  - `antigravity`       -> `businessaicode.googleapis.com/locations.generateContent`
  - `vertex_ai`         -> `aiplatform.googleapis.com/endpoints.predict`
  - `gemini_enterprise` -> `discoveryengine.googleapis.com/assistants.assist`

This allows blocking user A on `["antigravity"]` while blocking user B on
`["antigravity", "vertex_ai", "gemini_enterprise"]` inside one managed deny policy, while keeping:
- Foreign policy protection (never modifies policies with unexpected rules, conditions, exceptions,
  non-person principals, or unknown permissions)
- IAM v2 2,500 denied-principal capacity limit enforcement (Gate G6)
- ETag optimistic concurrency retry, LRO polling, and deleted/missing principal graceful handling.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field

from .. import config, identity
from ..gcp import GcpError, log_safe

LOG = logging.getLogger("usage_caps_api.enforcers")

IAM_V2 = "https://iam.googleapis.com/v2"
CRM_SERVICE = "cloudresourcemanager.googleapis.com"
DENY_PRINCIPAL_LIMIT = 2500
DENY_WARN_AT = 2000

SERVICE_PERMISSIONS = {
    "antigravity": "businessaicode.googleapis.com/locations.generateContent",
    "vertex_ai": "aiplatform.googleapis.com/endpoints.predict",
    "gemini_enterprise": "discoveryengine.googleapis.com/assistants.assist",
}
PERMISSION_TO_SERVICE = {v: k for k, v in SERVICE_PERMISSIONS.items()}
KNOWN_PERMISSIONS = frozenset(SERVICE_PERMISSIONS.values())
SERVICE_ORDER = ("antigravity", "vertex_ai", "gemini_enterprise")

DISPLAY_NAME = "GCP AI Usage Caps API (managed)"
POLICY_PREFIX = "gcp-ai-caps-"
DEFAULT_POLICY_ID = "gcp-ai-caps-managed"
OPS = ("create", "update", "delete", "none")
SOURCE = "api:iam.denypolicies"
POLL_SECONDS = 1
DEFAULT_POLLS = 5
REQUEST_TIMEOUT = 30
APPLY_BATCH = 8
RETRY_KINDS = frozenset(("conflict", "precondition"))
TRANSIENT_KINDS = frozenset(("unavailable", "timeout", "network"))
READ_RETRIES = 2
READ_RETRY_SECONDS = 1
QUOTA_WAIT_SECONDS = (20, 40)
MISSING_PRINCIPAL_RE = re.compile(r"principal specified by member \((principal://[^)\s]+)\) was not found")
MAX_MISSING_PRINCIPALS = 25
SCOPE_COLLECTIONS = {"folder": "folders", "organization": "organizations"}
RPC_KINDS = {
    3: "invalid", 4: "timeout", 5: "not_found", 6: "conflict", 7: "denied", 8: "unavailable",
    9: "precondition", 10: "conflict", 13: "unavailable", 14: "unavailable", 16: "denied",
}

_ATTACHMENT_RE = re.compile(
    r"cloudresourcemanager\.googleapis\.com/(?:organizations/[0-9]{1,30}"
    r"|folders/[0-9]{1,30}|projects/[a-z0-9][a-z0-9.:\-]{4,99})"
)
_SEGMENT = r"[A-Za-z0-9._~%:\-]+"
_OPERATION_RE = re.compile(r"policies/%s/%s/%s/operations/%s" % ((_SEGMENT,) * 4))
_RULE_KEYS = frozenset(("denyRule", "description"))
_DENY_LISTS = ("deniedPrincipals", "deniedPermissions")


def policy_id(cfg: dict) -> str:
    val = ((cfg or {}).get("caps") or {}).get("deny_policy_id")
    if isinstance(val, str) and config.DENY_POLICY_ID_RE.fullmatch(val):
        return val
    return DEFAULT_POLICY_ID


def attachment_points(ctx) -> list[str]:
    cfg = ctx.config
    ws = cfg.get("workspace") or {}
    caps = cfg.get("caps") or {}
    collection = SCOPE_COLLECTIONS.get(ws.get("scope_type"))
    if caps.get("deny_attachment") == "scope" and collection:
        raw = ws.get("scope_ids") or ()
        ids = list(dict.fromkeys(str(v).strip() for v in raw if config.SCOPE_ID_RE.match(str(v).strip())))
        if ids:
            return ["%s/%s/%s" % (CRM_SERVICE, collection, sid) for sid in ids]
    out, seen = [], set()
    for pid in ctx.all_projects or ():
        if not config.valid_project(pid):
            continue
        point = "%s/projects/%s" % (CRM_SERVICE, pid)
        if _ATTACHMENT_RE.fullmatch(point) and point not in seen:
            seen.add(point)
            out.append(point)
    return out


def _checked_id(pid: str) -> str:
    if not (isinstance(pid, str) and config.DENY_POLICY_ID_RE.fullmatch(pid)):
        raise ValueError("deny policy id must start with %s" % POLICY_PREFIX)
    return pid


def _collection_url(ap: str) -> str:
    if not (isinstance(ap, str) and _ATTACHMENT_RE.fullmatch(ap)):
        raise ValueError("invalid deny attachment point")
    return "%s/policies/%s/denypolicies" % (IAM_V2, urllib.parse.quote(ap, safe=""))


def _policy_url(ap: str, pid: str) -> str:
    return "%s/%s" % (_collection_url(ap), _checked_id(pid))


def _valid_operation(name) -> bool:
    if not (isinstance(name, str) and len(name) <= 1024 and _OPERATION_RE.fullmatch(name)):
        return False
    return ".." not in name and "%2e" not in name.lower() and "." not in name.split("/")


def _deny_form(value) -> bool:
    if not isinstance(value, str):
        return False
    found = identity.parse(value)
    return bool(found is not None and found.is_person and identity.deny_principal(value) == value)


def normalize_desired_map(desired) -> dict[str, frozenset[str]]:
    """Normalizes {principal_uri: iterable_of_services} into canonical {principal_uri: frozenset(services)}."""
    out: dict[str, frozenset[str]] = {}
    for principal, svcs in (desired or {}).items():
        clean_svcs = frozenset(s for s in (svcs or ()) if s in SERVICE_PERMISSIONS)
        if clean_svcs:
            out[str(principal)] = clean_svcs
    return out


def by_service_map(desired_by_principal: dict[str, frozenset[str]]) -> dict[str, frozenset[str]]:
    """Inverts {principal: {services}} into {service: frozenset(principals)} for non-empty services."""
    grouped: dict[str, set[str]] = {}
    for principal, svcs in (desired_by_principal or {}).items():
        for svc in svcs:
            if svc in SERVICE_PERMISSIONS:
                grouped.setdefault(svc, set()).add(principal)
    return {svc: frozenset(grouped[svc]) for svc in SERVICE_ORDER if grouped.get(svc)}


# ---------------------------------------------------------------------------
# IAM v2 Client
# ---------------------------------------------------------------------------

class DenyPolicyClient:
    def __init__(self, ctx, *, sleep=time.sleep, polls: int = DEFAULT_POLLS, quota_waits=None):
        self.ctx = ctx
        self.sleep = sleep
        self.polls = max(0, int(polls))
        if quota_waits is None:
            quota_waits = QUOTA_WAIT_SECONDS if getattr(ctx, "deny_quota_patience", False) is True else ()
        self.quota_waits = tuple(quota_waits)

    def get(self, ap: str, pid: str) -> dict | None:
        url = _policy_url(ap, pid)
        quota_tries = transient_tries = 0
        while True:
            try:
                payload = self.ctx.rest("GET", url, timeout=REQUEST_TIMEOUT)
            except GcpError as err:
                if err.kind == "not_found":
                    return None
                if err.status == 429 and quota_tries < len(self.quota_waits):
                    self.sleep(self.quota_waits[quota_tries])
                    quota_tries += 1
                    continue
                if err.kind in TRANSIENT_KINDS and transient_tries < READ_RETRIES:
                    transient_tries += 1
                    self.sleep(READ_RETRY_SECONDS * transient_tries)
                    continue
                raise
            return payload if isinstance(payload, dict) else {}

    def create(self, ap: str, pid: str, policy: dict) -> dict:
        op = self.ctx.rest("POST", _collection_url(ap), json_body=policy,
                           params={"policyId": _checked_id(pid)}, timeout=REQUEST_TIMEOUT)
        return self.wait(op)

    def update(self, ap: str, pid: str, policy: dict) -> dict:
        return self.wait(self.ctx.rest("PUT", _policy_url(ap, pid), json_body=policy, timeout=REQUEST_TIMEOUT))

    def delete(self, ap: str, pid: str, etag: str | None = None) -> dict:
        params = {"etag": etag} if etag else None
        return self.wait(self.ctx.rest("DELETE", _policy_url(ap, pid), params=params, timeout=REQUEST_TIMEOUT))

    def wait(self, op) -> dict:
        current = op if isinstance(op, dict) else {}
        name = current.get("name") if isinstance(current.get("name"), str) else None
        polls = 0
        while current.get("done") is not True and current.get("error") is None and polls < self.polls:
            if not _valid_operation(name):
                break
            self.sleep(POLL_SECONDS)
            polls += 1
            try:
                fresh = self.ctx.rest("GET", "%s/%s" % (IAM_V2, name), timeout=REQUEST_TIMEOUT)
            except GcpError as err:
                if err.kind in TRANSIENT_KINDS:
                    continue
                break
            if isinstance(fresh, dict):
                current = fresh
        err = current.get("error")
        resp = current.get("response")
        return {
            "name": name,
            "done": current.get("done") is True,
            "error": err if isinstance(err, dict) else None,
            "response": resp if isinstance(resp, dict) else None,
        }


# ---------------------------------------------------------------------------
# State reading & foreign policy detection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AttachmentState:
    attachment: str
    exists: bool = False
    by_principal: dict = field(default_factory=dict)  # {principal_uri: frozenset(services)}
    etag: str | None = None
    error: str | None = None
    foreign: bool = False

    @property
    def principals(self) -> frozenset[str]:
        return frozenset(self.by_principal.keys())


@dataclass
class DenyState:
    attachments: list[AttachmentState] = field(default_factory=list)

    def _usable(self) -> list[AttachmentState]:
        return [st for st in self.attachments if st.error is None and not st.foreign]

    @property
    def readable(self) -> bool:
        return bool(self.attachments) and all(st.error is None and not st.foreign for st in self.attachments)

    @property
    def principals(self) -> frozenset[str]:
        usable = self._usable()
        return frozenset.intersection(*(st.principals for st in usable)) if usable else frozenset()

    @property
    def services_by_person(self) -> dict[str, frozenset[str]]:
        """{person_key: frozenset(services denied across ALL usable attachment points)}."""
        usable = self._usable()
        if not usable:
            return {}
        out: dict[str, frozenset[str]] = {}
        all_p = set().union(*(st.principals for st in usable))
        for p in all_p:
            pkey = identity.person_key(p)
            if not pkey:
                continue
            svcs = set(SERVICE_ORDER)
            for st in usable:
                svcs &= set(st.by_principal.get(p) or ())
            if svcs:
                out[pkey] = frozenset(svcs)
        return out

    @property
    def by_attachment(self) -> dict[str, AttachmentState]:
        return {st.attachment: st for st in self.attachments}


def _state_of(ap: str, policy: dict) -> AttachmentState:
    etag = policy.get("etag") if isinstance(policy.get("etag"), str) else None
    rules = policy.get("rules") or []
    foreign = not isinstance(rules, list) or len(rules) > len(SERVICE_ORDER)
    by_p: dict[str, set[str]] = {}
    for rule in (rules if isinstance(rules, list) else ()):
        deny = rule.get("denyRule") if isinstance(rule, dict) else None
        if not isinstance(deny, dict) or any(v for k, v in rule.items() if k not in _RULE_KEYS):
            foreign = True
            continue
        r_principals, r_perms = set(), set()
        for k, v in deny.items():
            if k not in _DENY_LISTS:
                foreign = foreign or bool(v)
            elif isinstance(v, list) and all(isinstance(item, str) for item in v):
                (r_principals if k == "deniedPrincipals" else r_perms).update(v)
            elif v:
                foreign = True
        if not r_perms <= KNOWN_PERMISSIONS or not all(_deny_form(p) for p in r_principals):
            foreign = True
            continue
        r_services = {PERMISSION_TO_SERVICE[perm] for perm in r_perms if perm in PERMISSION_TO_SERVICE}
        for p in r_principals:
            by_p.setdefault(p, set()).update(r_services)
    frozen_map = {p: frozenset(svcs) for p, svcs in by_p.items() if svcs}
    return AttachmentState(ap, exists=True, by_principal=frozen_map, etag=etag, foreign=foreign)


def _error_kind(err) -> str:
    if isinstance(err, GcpError):
        return err.kind
    if isinstance(err, ValueError):
        return "invalid"
    return "internal_error"


def _read_one(client: DenyPolicyClient, ap: str, pid: str) -> AttachmentState:
    try:
        pol = client.get(ap, pid)
    except (GcpError, ValueError) as err:
        return AttachmentState(ap, error=_error_kind(err))
    return AttachmentState(ap) if pol is None else _state_of(ap, pol)


def read_state(ctx, *, attachments=None, client: DenyPolicyClient | None = None) -> DenyState:
    try:
        points = list(dict.fromkeys(attachments if attachments is not None else attachment_points(ctx)))
        client = client or DenyPolicyClient(ctx)
        pid = policy_id(ctx.config)
        tasks = {ap: (lambda ap=ap: _read_one(client, ap, pid)) for ap in points}
        results = ctx.run(tasks) if tasks else {}
    except Exception:  # pylint: disable=broad-except
        LOG.exception("deny policy state read failed")
        return DenyState([])
    states = []
    for ap in points:
        ok, val = results.get(ap, (False, None))
        if not (ok and isinstance(val, AttachmentState)):
            val = AttachmentState(ap, error=_error_kind(val))
        states.append(val)
    if any(st.error is None for st in states):
        ctx.add_source(SOURCE)
    return DenyState(states)


# ---------------------------------------------------------------------------
# Policy building, planning, invariant checks, and atomic apply
# ---------------------------------------------------------------------------

def build_policy(desired_by_principal: dict[str, frozenset[str]], etag: str | None = None) -> dict:
    """Builds an IAM Deny v2 policy with one clean rule per non-empty service."""
    grouped = by_service_map(desired_by_principal)
    rules = []
    for svc in SERVICE_ORDER:
        principals = grouped.get(svc)
        if not principals:
            continue
        rules.append({
            "description": "Deny %s access (managed by gcp-ai-usage-caps-api)" % svc,
            "denyRule": {
                "deniedPrincipals": sorted(principals),
                "deniedPermissions": [SERVICE_PERMISSIONS[svc]],
            },
        })
    pol: dict = {"displayName": DISPLAY_NAME, "rules": rules}
    if etag:
        pol["etag"] = etag
    return pol


def plan(state: DenyState, desired_by_principal: dict[str, frozenset[str]]) -> list[dict]:
    desired = normalize_desired_map(desired_by_principal)
    desired_principals = frozenset(desired.keys())
    items = []
    for st in state.attachments:
        item = {
            "attachment": st.attachment,
            "op": "none",
            "add": [],
            "remove": [],
            "count": len(st.principals),
            "foreign": bool(st.foreign),
            "error": st.error or ("foreign_policy" if st.foreign else None),
        }
        if item["error"] is None:
            add = sorted(desired_principals - st.principals)
            remove = sorted(st.principals - desired_principals)
            if not desired:
                item.update(op="delete" if st.exists else "none", remove=remove, count=0)
            elif not st.exists:
                item.update(op="create", add=add, count=len(desired_principals))
            else:
                changed = desired != st.by_principal
                item.update(op="update" if changed else "none", add=add, remove=remove, count=len(desired_principals))
        items.append(item)
    return items


def check_invariants(cfg: dict, items: list[dict], desired_by_principal: dict[str, frozenset[str]],
                     attachments: list[str]) -> list[str]:
    out = []
    pid = policy_id(cfg)
    if not (pid.startswith(POLICY_PREFIX) and config.DENY_POLICY_ID_RE.fullmatch(pid)):
        out.append("Deny policy id '%s' does not have required prefix '%s'." % (pid, POLICY_PREFIX))
    bad_p = sorted({p for p in desired_by_principal if not _deny_form(p)})
    if bad_p:
        out.append("%d principal(s) are not valid person deny URIs." % len(bad_p))
    bad_s = sorted({s for svcs in desired_by_principal.values() for s in svcs if s not in SERVICE_PERMISSIONS})
    if bad_s:
        out.append("Unknown target services: %s." % ", ".join(bad_s))
    if len(desired_by_principal) > DENY_PRINCIPAL_LIMIT:
        out.append("%d principals exceed IAM limit of %d per attachment point."
                   % (len(desired_by_principal), DENY_PRINCIPAL_LIMIT))
    allowed = {a for a in (attachments or ()) if isinstance(a, str)}
    seen = set()
    for item in items or ():
        ap, op = item.get("attachment"), item.get("op")
        if not (isinstance(ap, str) and ap in allowed and _ATTACHMENT_RE.fullmatch(ap)):
            out.append("Attachment point '%s' is outside configured allowlist." % ap)
        if ap in seen:
            out.append("Duplicate attachment point '%s' in plan." % ap)
        seen.add(ap)
        if op not in OPS:
            out.append("Invalid operation '%s' for '%s'." % (op, ap))
        elif op != "none" and item.get("foreign"):
            out.append("Refusing to modify foreign deny policy at '%s'." % ap)
        if op in ("create", "update") and int(item.get("count") or 0) > DENY_PRINCIPAL_LIMIT:
            out.append("Principal count at '%s' exceeds IAM limit of %d." % (ap, DENY_PRINCIPAL_LIMIT))
    return out


def _done(item: dict, status: str, error: str | None = None, missing=None) -> dict:
    out = dict(item)
    out["status"] = status
    out["error"] = error
    if missing:
        out["missing"] = sorted(missing)
    return out


def _op_error_kind(err: dict) -> str:
    try:
        code = int(err.get("code"))
    except (AttributeError, TypeError, ValueError):
        code = None
    return RPC_KINDS.get(code, "operation_failed")


def _missing_principal(kind: str, op: str, message: str | None, desired: dict) -> str | None:
    if kind != "invalid" or op not in ("create", "update"):
        return None
    m = MISSING_PRINCIPAL_RE.search(message or "")
    return m.group(1) if (m and m.group(1) in desired) else None


def _execute(client: DenyPolicyClient, cfg: dict, pid: str, st: AttachmentState, item: dict,
             desired: dict[str, frozenset[str]], allowed: list[str]) -> dict:
    missing: list[str] = []
    attempt = 1
    while True:
        op = item["op"]
        if op != "create" and not st.etag:
            return _done(item, "failed", "missing_etag", missing)
        try:
            if op == "create":
                res = client.create(st.attachment, pid, build_policy(desired))
            elif op == "update":
                res = client.update(st.attachment, pid, build_policy(desired, etag=st.etag))
            else:
                res = client.delete(st.attachment, pid, etag=st.etag)
        except GcpError as err:
            kind, message = err.kind, err.message
            LOG.warning("deny policy %s failed kind=%s message=%s", op, kind, log_safe(message))
        else:
            if res["error"] is None:
                return _done(item, "applied" if res["done"] else "pending", None, missing)
            kind, message = _op_error_kind(res["error"]), res["error"].get("message")
        gone = _missing_principal(kind, op, message, desired)
        if gone is not None and len(missing) < MAX_MISSING_PRINCIPALS:
            missing.append(gone)
            desired = {k: v for k, v in desired.items() if k != gone}
            item = plan(DenyState([st]), desired)[0]
        elif attempt == 2 or not (kind in RETRY_KINDS or (kind == "not_found" and op != "create")):
            return _done(item, "failed", kind, missing)
        else:
            attempt += 1
            st = _read_one(client, st.attachment, pid)
            item = plan(DenyState([st]), desired)[0]
        if check_invariants(cfg, [item], desired, allowed):
            return _done(item, "failed", "invariant_violation", missing)
        if item["error"] or item["op"] == "none":
            return _done(item, "failed" if item["error"] else "skipped", item["error"], missing)


def apply(ctx, state: DenyState, desired_by_principal: dict[str, frozenset[str]], *,
          dry_run: bool = False, client: DenyPolicyClient | None = None, attachments=None) -> dict:
    cfg = ctx.config
    desired = normalize_desired_map(desired_by_principal)
    allowed = list(attachments) if attachments is not None else attachment_points(ctx)
    items = plan(state, desired)
    violations = check_invariants(cfg, items, desired, allowed)
    if violations:
        refused = [_done(i, "skipped", i["error"] or ("invariant_violation" if i["op"] != "none" else None))
                   for i in items]
        return {"items": refused, "applied": False, "violations": violations, "missing": []}
    if dry_run:
        return {"items": [_done(i, "skipped", i["error"]) for i in items], "applied": False,
                "violations": [], "missing": []}
    client = client or DenyPolicyClient(ctx)
    pid = policy_id(cfg)
    by_ap = state.by_attachment
    results = [None] * len(items)
    writes = []
    for idx, item in enumerate(items):
        if item["op"] == "none":
            results[idx] = _done(item, "failed" if item["error"] else "skipped", item["error"])
        else:
            writes.append(idx)
    for start in range(0, len(writes), APPLY_BATCH):
        chunk = writes[start:start + APPLY_BATCH]
        outcome = ctx.run({i: (lambda i=i: _execute(client, cfg, pid, by_ap[items[i]["attachment"]],
                                                    items[i], desired, allowed)) for i in chunk})
        for i in chunk:
            ok, val = outcome.get(i, (False, None))
            results[i] = val if (ok and isinstance(val, dict)) else _done(items[i], "failed", _error_kind(val))
    statuses = [r["status"] for r in results]
    missing = sorted({p for r in results for p in (r.get("missing") or ())})
    return {
        "items": results,
        "applied": any(s in ("applied", "pending") for s in statuses),
        "violations": [],
        "missing": missing,
    }
