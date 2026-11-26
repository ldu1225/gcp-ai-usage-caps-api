"""Model access policies across every managed project (Vertex AI Model Garden via Organization Policy).

Mechanism: list constraint `constraints/vertexai.allowedModels` (Organization Policy API v2).
    GET    https://orgpolicy.googleapis.com/v2/{scope}/policies/vertexai.allowedModels
    GET    https://orgpolicy.googleapis.com/v2/{scope}/policies/vertexai.allowedModels:getEffectivePolicy
    POST   https://orgpolicy.googleapis.com/v2/{scope}/policies              (create)
    PATCH  https://orgpolicy.googleapis.com/v2/{scope}/policies/vertexai.allowedModels  (update, spec.etag)
    DELETE https://orgpolicy.googleapis.com/v2/{scope}/policies/vertexai.allowedModels  (inherit from parent)
  where {scope} is projects/{id}, folders/{id} or organizations/{id}.

Values are `publishers/{publisher}/models/{model}:{action}` (action predict | deploy | tune). The constraint does not
support wildcards and is limited to 500 values, so this API stores the customer's *intent* (patterns such as
`gemini-2.5-*`) and expands it against the model catalog at write time; `POST /v1/model-access/reconcile` (also run by
the scheduled enforcer) re-expands intents when new model versions appear.

OpenAI analogue: project model permissions (allow_list / deny_list). Differences: GCP enforces per project/folder/org
(not per user), propagation takes minutes, and Gemini Enterprise / Antigravity have separate surface controls.
"""

from __future__ import annotations

import datetime as dt
import logging

from .. import config
from ..gcp import GcpError
from ..httpkit import ApiError, now_iso, paginate_list
from . import audit, model_registry as reg, operations, webhooks

LOG = logging.getLogger("usage_caps_api.model_access")

ORGPOLICY = "https://orgpolicy.googleapis.com/v2"
CONSTRAINT = "vertexai.allowedModels"
INTENT_DOC = "model_policies"
MODES = ("allow_list", "deny_list", "unrestricted", "deny_all", "inherit")
SCOPE_COLLECTIONS = ("projects", "folders", "organizations")
DEFAULT_PREVIEW_DAYS = 30
MAX_BULK_SCOPES = 300


# ---------------------------------------------------------------------------
# Scope handling
# ---------------------------------------------------------------------------

def allowed_scope_types(ctx) -> list[str]:
    scopes = [s for s in ((ctx.config.get("models") or {}).get("policy_scopes") or ["projects"])
              if s in SCOPE_COLLECTIONS]
    return scopes or ["projects"]


def managed_folder_org_ids(ctx) -> dict[str, set[str]]:
    out = {"folders": set(), "organizations": set()}
    st = ctx.ws.get("scope_type")
    for sid in ctx.ws.get("scope_ids") or []:
        if st == "folder":
            out["folders"].add(str(sid))
        elif st == "organization":
            out["organizations"].add(str(sid))
    org = str((ctx.config.get("hub") or {}).get("org_id") or "")
    if org:
        out["organizations"].add(org)
    for fid in (ctx.config.get("models") or {}).get("managed_folders") or []:
        out["folders"].add(str(fid))
    return out


def scope_ref(ctx, scope_type: str, scope_id: str, *, for_write: bool = False) -> str:
    stype = str(scope_type or "").strip().lower()
    sid = str(scope_id or "").strip().lower()
    if stype not in SCOPE_COLLECTIONS:
        raise ApiError("bad_request", "Scope type must be one of: %s." % ", ".join(SCOPE_COLLECTIONS))
    if for_write and stype not in allowed_scope_types(ctx):
        raise ApiError("forbidden", "Writing model policies at '%s' level is disabled (models.policy_scopes=%s)."
                       % (stype, ",".join(allowed_scope_types(ctx))))
    if for_write and not (ctx.config.get("models") or {}).get("allow_org_policy_writes", True):
        raise ApiError("forbidden", "Model policy writes are disabled (models.allow_org_policy_writes=false).")
    if stype == "projects":
        if not config.valid_project(sid):
            raise ApiError("bad_request", "Invalid project id '%s'." % scope_id)
        if sid not in ctx.all_projects:
            raise ApiError("forbidden", "Project '%s' is not managed by this control plane." % sid)
    else:
        if not config.SCOPE_ID_RE.match(sid):
            raise ApiError("bad_request", "Folder/organization ids are numeric.")
        if sid not in managed_folder_org_ids(ctx)[stype]:
            raise ApiError("forbidden", "%s/%s is not a managed scope of this control plane." % (stype, sid))
    return "%s/%s" % (stype, sid)


def _split_scope(scope: str) -> tuple[str, str]:
    stype, sid = scope.split("/", 1)
    return stype, sid


def managed_scopes(ctx) -> list[str]:
    scopes = ["projects/%s" % p for p in ctx.all_projects]
    ids = managed_folder_org_ids(ctx)
    scopes += ["folders/%s" % f for f in sorted(ids["folders"])]
    scopes += ["organizations/%s" % o for o in sorted(ids["organizations"])]
    return scopes


# ---------------------------------------------------------------------------
# Org Policy REST
# ---------------------------------------------------------------------------

def _policy_name(scope: str) -> str:
    return "%s/policies/%s" % (scope, CONSTRAINT)


def read_policy(ctx, scope: str) -> dict | None:
    try:
        return ctx.rest("GET", "%s/%s" % (ORGPOLICY, _policy_name(scope)), timeout=20) or {}
    except GcpError as err:
        if err.kind == "not_found":
            return None
        raise


def read_effective(ctx, scope: str) -> dict | None:
    try:
        return ctx.rest("GET", "%s/%s:getEffectivePolicy" % (ORGPOLICY, _policy_name(scope)), timeout=20) or {}
    except GcpError as err:
        if err.kind == "not_found":
            return None
        raise


def _upstream(err: GcpError, scope: str) -> ApiError:
    if err.kind == "denied":
        return ApiError("forbidden", "The control-plane service account cannot access Organization Policy on %s "
                        "(grant roles/orgpolicy.policyAdmin, enable orgpolicy.googleapis.com, check VPC-SC)." % scope)
    return ApiError("upstream_error", "Organization Policy request failed on %s (%s)." % (scope, err.kind))


def parse_policy(policy: dict | None) -> dict:
    """Normalizes an Org Policy v2 resource into {mode, allowed_values, denied_values, inherit_from_parent, etag}."""
    if not policy:
        return {"mode": "inherit", "allowed_values": [], "denied_values": [], "inherit_from_parent": True,
                "etag": None, "update_time": None, "exists": False}
    spec = policy.get("spec") if isinstance(policy.get("spec"), dict) else {}
    rules = spec.get("rules") if isinstance(spec.get("rules"), list) else []
    allowed, denied = [], []
    allow_all = deny_all = False
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("allowAll"):
            allow_all = True
        if rule.get("denyAll"):
            deny_all = True
        vals = rule.get("values") if isinstance(rule.get("values"), dict) else {}
        allowed += [str(v) for v in vals.get("allowedValues") or []]
        denied += [str(v) for v in vals.get("deniedValues") or []]
    if spec.get("reset"):
        mode = "inherit"
    elif deny_all:
        mode = "deny_all"
    elif allowed and denied:
        mode = "mixed"
    elif allowed:
        mode = "allow_list"
    elif denied:
        mode = "deny_list"
    elif allow_all:
        mode = "unrestricted"
    else:
        mode = "inherit" if not rules else "unrestricted"
    return {
        "mode": mode,
        "allowed_values": allowed,
        "denied_values": denied,
        "inherit_from_parent": bool(spec.get("inheritFromParent")),
        "etag": spec.get("etag") or policy.get("etag"),
        "update_time": spec.get("updateTime"),
        "exists": True,
    }


def _value_parts(value: str) -> tuple[str | None, str, str | None]:
    try:
        return reg.parse_model_ref(value)
    except ApiError:
        return None, "", None


def value_matches(value: str, publisher: str, model_id: str, action: str) -> bool:
    vpub, vmid, vact = _value_parts(value)
    if not vmid:
        return False
    if vpub and publisher and vpub != publisher:
        return False
    if vact and vact != action:
        return False
    return reg.matches(model_id, vmid)


def decide(parsed_effective: dict, publisher: str, model_id: str, action: str = "predict") -> tuple[str, str]:
    """Evaluates an effective policy for one model action -> (allowed|denied, reason)."""
    mode = parsed_effective.get("mode")
    if mode in ("inherit", "unrestricted") and not parsed_effective.get("denied_values"):
        return "allowed", "no restriction"
    if mode == "deny_all":
        return "denied", "deny_all"
    for v in parsed_effective.get("denied_values") or []:
        if value_matches(v, publisher, model_id, action):
            return "denied", "denied value %s" % v
    allowed = parsed_effective.get("allowed_values") or []
    if allowed:
        for v in allowed:
            if value_matches(v, publisher, model_id, action):
                return "allowed", "allowed value %s" % v
        return "denied", "not in allow list"
    return "allowed", "not denied"


def effective_for(ctx, scope: str) -> dict:
    def load(c):
        try:
            return {"ok": True, "parsed": parse_policy(read_effective(c, scope))}
        except GcpError as err:
            return {"ok": False, "error": err.kind}
    return ctx.memo("model_policy_eff", scope, load, ttl=60)


def access_evaluator(ctx, project: str, catalog: dict | None = None):
    """Returns fn(model_entry) -> per-surface access decision for one project."""
    from . import surface_models, user_model_rules  # noqa: F401
    eff = effective_for(ctx, "projects/%s" % project)
    try:
        ge_states = surface_models.ge_model_states(ctx, project)
    except Exception:  # pylint: disable=broad-except
        ge_states = None
    try:
        agy = surface_models.agy_config(ctx, project)
    except Exception:  # pylint: disable=broad-except
        agy = {}

    def evaluate(entry: dict) -> dict:
        pub, mid = entry.get("publisher") or reg.guess_publisher(entry["id"]), entry["id"]
        out = {"project_id": project}
        if eff.get("ok"):
            decision, reason = decide(eff["parsed"], pub, mid, "predict")
            out["vertex_ai"] = {"decision": decision, "reason": reason, "enforcement": "org_policy",
                                "policy_mode": eff["parsed"]["mode"]}
        else:
            out["vertex_ai"] = {"decision": "unknown", "reason": eff.get("error"), "enforcement": "org_policy"}
        if ge_states is None:
            out["gemini_enterprise"] = {"decision": "unknown", "enforcement": "app_model_config"}
        elif not ge_states:
            out["gemini_enterprise"] = {"decision": "not_applicable", "reason": "no Gemini Enterprise app in project",
                                        "enforcement": "app_model_config"}
        else:
            apps = {}
            for app_id, models in ge_states.items():
                state = models.get(mid)
                apps[app_id] = "disabled" if state == "off" else ("enabled" if state == "on" else "product_default")
            out["gemini_enterprise"] = {"decision": "per_app", "apps": apps, "enforcement": "app_model_config"}
        agy_locs = {loc: e for loc, e in (agy or {}).items() if e.get("available")}
        if not agy_locs:
            out["antigravity"] = {"decision": "not_applicable" if agy else "unknown",
                                  "enforcement": "console_only"}
        else:
            locs = {}
            for loc, e in agy_locs.items():
                allowed = (e.get("allowed_models") or []) + (e.get("allowed_third_party_models") or [])
                locs[loc] = ("allowed" if not allowed or any(reg.matches(mid, a) for a in allowed) else "denied")
            out["antigravity"] = {"decision": "per_location", "locations": locs, "enforcement": "console_only",
                                  "write_mode": "console_required"}
        return out
    return evaluate


# ---------------------------------------------------------------------------
# Intent storage & spec building
# ---------------------------------------------------------------------------

def _intent(ctx, scope: str) -> dict | None:
    doc, _ = ctx.store.load(INTENT_DOC)
    rec = (doc.get("scopes") or {}).get(scope)
    return dict(rec) if isinstance(rec, dict) else None


def _save_intent(ctx, scope: str, intent: dict | None):
    def save(d):
        scopes = dict(d.get("scopes") or {})
        if intent is None:
            scopes.pop(scope, None)
        else:
            scopes[scope] = intent
        d["scopes"] = scopes
        return d
    ctx.store.mutate(INTENT_DOC, save)


def _validate_actions(raw) -> list[str]:
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise ApiError("bad_request", "Field 'actions' must be a non-empty list of: %s." % ", ".join(reg.ACTIONS))
    out = []
    for a in raw:
        a = str(a or "").strip().lower()
        if a not in reg.ACTIONS:
            raise ApiError("bad_request", "Invalid action '%s'. Valid: %s." % (a, ", ".join(reg.ACTIONS)))
        if a not in out:
            out.append(a)
    return out


def parse_request(ctx, body: dict) -> dict:
    mode = str(body.get("mode") or "").strip().lower()
    if mode not in MODES:
        raise ApiError("bad_request", "Field 'mode' must be one of: %s." % ", ".join(MODES))
    models = body.get("models") or []
    if not isinstance(models, list) or len(models) > 1000:
        raise ApiError("bad_request", "Field 'models' must be a list of model ids or patterns (max 1000).")
    if mode in ("allow_list", "deny_list") and not models and mode == "deny_list":
        raise ApiError("bad_request", "deny_list requires at least one model.")
    actions = _validate_actions(body.get("actions")) or list(
        (ctx.config.get("models") or {}).get("default_actions") or ["predict"])
    inherit = body.get("inherit_from_parent", False)
    if not isinstance(inherit, bool):
        raise ApiError("bad_request", "Field 'inherit_from_parent' must be a boolean.")
    for m in models:
        reg.parse_model_ref(m)
    return {"mode": mode, "models": [str(m).strip().lower() for m in models], "actions": actions,
            "inherit_from_parent": inherit, "force": bool(body.get("force", False)),
            "reason": str(body.get("reason") or "")[:300]}


def build_values(ctx, catalog: dict, req: dict) -> tuple[list[str], list[tuple[str, str]]]:
    pairs = reg.expand(catalog, req["models"])
    values = []
    for pub, mid in pairs:
        for action in req["actions"]:
            v = "publishers/%s/models/%s:%s" % (pub, mid, action)
            if v not in values:
                values.append(v)
    if req["mode"] == "allow_list" and pairs:
        for helper in (ctx.config.get("models") or {}).get("always_allow") or []:
            hpub, hmid, hact = reg.parse_model_ref(helper)
            hv = "publishers/%s/models/%s:%s" % (hpub, hmid, hact or "predict")
            if hpub == "anthropic" and not any(p == "anthropic" for p, _ in pairs):
                continue
            if hv not in values:
                values.append(hv)
    limit = int((ctx.config.get("models") or {}).get("max_policy_values") or 500)
    if len(values) > limit:
        raise ApiError("unprocessable", "The policy would contain %d values; Organization Policy allows at most %d. "
                       "Narrow the patterns or actions." % (len(values), limit))
    return values, pairs


def build_spec(req: dict, values: list[str], etag: str | None) -> dict | None:
    mode = req["mode"]
    if mode == "inherit":
        return None
    if mode == "unrestricted":
        rules = [{"allowAll": True}]
    elif mode == "deny_all":
        rules = [{"denyAll": True}]
    elif mode == "allow_list":
        rules = [{"values": {"allowedValues": values}}]
    else:
        rules = [{"values": {"deniedValues": values}}]
    spec = {"rules": rules}
    if req["inherit_from_parent"] and mode in ("allow_list", "deny_list"):
        spec["inheritFromParent"] = True
    if etag:
        spec["etag"] = etag
    return spec


def policy_view(ctx, scope: str, *, parsed: dict | None = None, effective: dict | None = None,
                intent: dict | None = None, catalog: dict | None = None) -> dict:
    stype, sid = _split_scope(scope)
    view = {
        "object": "model_access_policy",
        "scope": scope,
        "scope_type": stype,
        "scope_id": sid,
        "constraint": "constraints/" + CONSTRAINT,
    }
    if parsed is not None:
        models = []
        for v in (parsed.get("allowed_values") if parsed["mode"] == "allow_list" else parsed.get("denied_values")) or []:
            pub, mid, act = _value_parts(v)
            if mid:
                models.append({"publisher": pub, "model": mid, "action": act or "*", "value": v})
        view.update(mode=parsed["mode"], models=models, allowed_values=parsed["allowed_values"],
                    denied_values=parsed["denied_values"], inherit_from_parent=parsed["inherit_from_parent"],
                    etag=parsed["etag"], update_time=parsed["update_time"], policy_exists=parsed["exists"])
    if effective is not None:
        view["effective"] = {"mode": effective["mode"], "allowed_values": effective["allowed_values"],
                             "denied_values": effective["denied_values"]}
    if intent is not None:
        view["intent"] = {k: intent.get(k) for k in ("mode", "models", "actions", "inherit_from_parent", "updated_at",
                                                     "updated_by", "reason")}
        if catalog is not None and parsed is not None and intent.get("mode") in ("allow_list", "deny_list"):
            try:
                expected, _ = build_values(ctx, catalog, dict(intent, force=False))
                current = parsed["allowed_values"] if intent["mode"] == "allow_list" else parsed["denied_values"]
                view["drift"] = sorted(set(expected) ^ set(current))
            except ApiError:
                view["drift"] = None
    view["managed_by_api"] = intent is not None
    return view


# ---------------------------------------------------------------------------
# Impact preview
# ---------------------------------------------------------------------------

def impact_preview(ctx, scope: str, parsed_new: dict, catalog: dict, days: int = DEFAULT_PREVIEW_DAYS) -> dict:
    """Which users/models in the scope's projects used now-denied models during the last N days."""
    from . import usage as usage_svc
    stype, sid = _split_scope(scope)
    projects = [sid] if stype == "projects" else list(ctx.all_projects)
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=days)
    res = usage_svc.collect_usage(ctx.child(params={}), start, end)
    newly_denied: dict[str, dict] = {}
    for email, rec in res.users.items():
        for proj, prec in (rec.get("by_project") or {}).items():
            if proj not in projects:
                continue
            for model, mrec in (prec.get("by_model") or {}).items():
                pub = (catalog.get(model) or {}).get("publisher") or reg.guess_publisher(model) or ""
                decision, _ = decide(parsed_new, pub, model, "predict")
                if decision == "denied":
                    m = newly_denied.setdefault(model, {"model": model, "users": set(), "requests": 0,
                                                        "total_tokens": 0, "projects": set()})
                    m["users"].add(email)
                    m["projects"].add(proj)
                    m["requests"] += mrec.get("requests", 0)
                    m["total_tokens"] += mrec.get("total_tokens", 0)
    models = [{"model": m["model"], "affected_users": len(m["users"]), "requests": m["requests"],
               "total_tokens": m["total_tokens"], "projects": sorted(m["projects"]),
               "sample_users": sorted(m["users"])[:20]} for m in newly_denied.values()]
    models.sort(key=lambda r: (-r["affected_users"], -r["requests"]))
    allowed_catalog = [e["id"] for e in catalog.values() if not e.get("hidden")
                       and decide(parsed_new, e["publisher"], e["id"])[0] == "allowed"]
    return {"window_days": days, "telemetry_ok": res.ok, "denied_models_in_use": models,
            "affected_users": len({u for m in newly_denied.values() for u in m["users"]}),
            "catalog_models_allowed": len(allowed_catalog), "catalog_models_total":
                sum(1 for e in catalog.values() if not e.get("hidden"))}


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def apply_policy(ctx, scope: str, req: dict, *, dry_run: bool, if_match: str | None = None,
                 catalog: dict | None = None, preview: bool = True) -> dict:
    catalog = catalog if catalog is not None else reg.load_catalog(ctx)
    try:
        current_raw = read_policy(ctx, scope)
    except GcpError as err:
        raise _upstream(err, scope)
    current = parse_policy(current_raw)
    if if_match and current.get("etag") and if_match.strip('"') != str(current["etag"]).strip('"'):
        raise ApiError("precondition_failed", "If-Match does not match the current policy etag '%s'." % current["etag"])
    values, pairs = build_values(ctx, catalog, req) if req["mode"] in ("allow_list", "deny_list") else ([], [])
    if req["mode"] == "allow_list" and not pairs and not req["force"]:
        raise ApiError("lockout_prevented", "An allow_list with no models would block every Model Garden model in %s. "
                       "Pass force=true to confirm, or use mode=deny_all." % scope)
    if req["mode"] == "deny_all" and not req["force"]:
        raise ApiError("lockout_prevented", "mode=deny_all blocks every Model Garden model in %s. Pass force=true." % scope)
    new_parsed = {
        "mode": req["mode"] if req["mode"] != "inherit" else "inherit",
        "allowed_values": values if req["mode"] == "allow_list" else [],
        "denied_values": values if req["mode"] == "deny_list" else [],
        "inherit_from_parent": req["inherit_from_parent"], "etag": None, "update_time": None, "exists": True,
    }
    result = {
        "object": "model_access_change",
        "scope": scope,
        "dry_run": dry_run,
        "before": policy_view(ctx, scope, parsed=current),
        "after": policy_view(ctx, scope, parsed=new_parsed),
        "expanded_models": ["%s/%s" % p for p in pairs],
    }
    if preview:
        try:
            result["impact"] = impact_preview(ctx, scope, new_parsed, catalog)
        except Exception as err:  # pylint: disable=broad-except
            result["impact"] = {"error": type(err).__name__}
    same = (current["mode"] == new_parsed["mode"] and set(current["allowed_values"]) == set(new_parsed["allowed_values"])
            and set(current["denied_values"]) == set(new_parsed["denied_values"])
            and current["inherit_from_parent"] == new_parsed["inherit_from_parent"]) or \
           (req["mode"] == "inherit" and not current["exists"])
    if dry_run:
        result["status"] = "dry_run" if not same else "unchanged"
        return result
    intent = {"mode": req["mode"], "models": req["models"], "actions": req["actions"],
              "inherit_from_parent": req["inherit_from_parent"], "updated_at": now_iso(),
              "updated_by": ctx.caller.id, "reason": req.get("reason")}
    if same:
        _save_intent(ctx, scope, None if req["mode"] == "inherit" else intent)
        result["status"] = "unchanged"
        return result
    name = _policy_name(scope)
    try:
        if req["mode"] == "inherit":
            ctx.rest("DELETE", "%s/%s" % (ORGPOLICY, name), timeout=30)
        else:
            spec = build_spec(req, values, current.get("etag"))
            body = {"name": name, "spec": spec}
            if current["exists"]:
                ctx.rest("PATCH", "%s/%s" % (ORGPOLICY, name), json_body=body, timeout=30)
            else:
                ctx.rest("POST", "%s/%s/policies" % (ORGPOLICY, scope), json_body=body, timeout=30)
    except GcpError as err:
        if err.kind == "denied":
            raise ApiError("forbidden", "The control-plane service account lacks orgpolicy.policies.* on %s "
                           "(grant roles/orgpolicy.policyAdmin)." % scope)
        if err.kind in ("precondition", "conflict"):
            raise ApiError("precondition_failed", "The policy changed concurrently on %s; re-read and retry." % scope)
        if err.kind == "invalid":
            raise ApiError("unprocessable", "Organization Policy rejected the change: %s" % (err.message or "invalid")[:300])
        raise ApiError("upstream_error", "Organization Policy update failed on %s (%s)." % (scope, err.kind))
    _save_intent(ctx, scope, None if req["mode"] == "inherit" else intent)
    if getattr(ctx.app, "cache", None) is not None:
        ctx.app.cache.clear("model_policy_eff")
    op = operations.create(ctx, "model_access.update", target=scope,
                           detail="Organization Policy %s updated; enforcement typically within minutes (up to ~15)."
                                  % CONSTRAINT,
                           verify={"scope": scope, "mode": new_parsed["mode"],
                                   "allowed": new_parsed["allowed_values"], "denied": new_parsed["denied_values"]})
    result["status"] = "applied"
    result["operation"] = op
    audit.write_audit(ctx, "model_policy.updated", target=scope, details={
        "mode": req["mode"], "models": req["models"], "actions": req["actions"], "values": len(values),
        "reason": req.get("reason"), "operation": op["id"]})
    webhooks.dispatch_event(ctx, "model.access_changed", {
        "surface": "vertex_ai", "scope": scope, "mode": req["mode"], "models": req["models"],
        "expanded_models": result["expanded_models"], "operation_id": op["id"], "actor": ctx.caller.id,
    })
    return result


def _verify_model_access(ctx, op: dict):
    v = op.get("verify") or {}
    scope = v.get("scope")
    if not scope:
        return None
    parsed = parse_policy(read_policy(ctx, scope))
    if v.get("mode") == "inherit":
        return "succeeded" if not parsed["exists"] else None
    if set(parsed["allowed_values"]) == set(v.get("allowed") or []) and \
            set(parsed["denied_values"]) == set(v.get("denied") or []):
        return None  # stored correctly; wait for propagation window to report success
    return "failed"


operations.VERIFIERS["model_access"] = _verify_model_access


# ---------------------------------------------------------------------------
# REST handlers
# ---------------------------------------------------------------------------

def _dry_run(ctx) -> bool:
    raw = (ctx.body or {}).get("dryRun", (ctx.body or {}).get("dry_run"))
    if raw is None:
        raw = str(ctx.params.get("dryRun") or ctx.params.get("dry_run") or "").lower() in ("1", "true")
    if not isinstance(raw, bool):
        raise ApiError("bad_request", "Field 'dryRun' must be a boolean.")
    return raw


def list_policies(ctx) -> dict:
    """GET /v1/model-access?scope_type=&limit=&cursor="""
    stype = (ctx.params.get("scope_type") or "").strip().lower()
    scopes = managed_scopes(ctx)
    if stype:
        scopes = [s for s in scopes if s.startswith(stype + "/")]
    page = paginate_list(scopes, ctx.params, default_limit=50, max_limit=200)
    catalog = reg.load_catalog(ctx, allow_discovery=False)
    tasks = {s: (lambda s=s: (parse_policy(read_policy(ctx, s)), _intent(ctx, s))) for s in page["data"]}
    outcomes = ctx.run(tasks) if tasks else {}
    data = []
    for s in page["data"]:
        ok, val = outcomes.get(s, (False, None))
        if ok:
            parsed, intent = val
            data.append(policy_view(ctx, s, parsed=parsed, intent=intent, catalog=catalog))
        else:
            data.append(dict(policy_view(ctx, s), error=getattr(val, "kind", "error")))
    page["data"] = data
    return page


def get_policy(ctx) -> dict:
    """GET /v1/model-access/{scope_type}/{scope_id}"""
    scope = scope_ref(ctx, ctx.path_params.get("scope_type"), ctx.path_params.get("scope_id"))
    catalog = reg.load_catalog(ctx, allow_discovery=False)
    try:
        parsed = parse_policy(read_policy(ctx, scope))
        eff_raw = read_effective(ctx, scope)
    except GcpError as err:
        raise _upstream(err, scope)
    return policy_view(ctx, scope, parsed=parsed, effective=parse_policy(eff_raw) if eff_raw is not None else None,
                       intent=_intent(ctx, scope), catalog=catalog)


def put_policy(ctx) -> dict:
    """PUT /v1/model-access/{scope_type}/{scope_id} {mode, models, actions?, inherit_from_parent?, force?, dryRun?}"""
    scope = scope_ref(ctx, ctx.path_params.get("scope_type"), ctx.path_params.get("scope_id"), for_write=True)
    req = parse_request(ctx, ctx.body or {})
    return apply_policy(ctx, scope, req, dry_run=_dry_run(ctx), if_match=ctx.headers.get("if-match"))


def delete_policy(ctx) -> dict:
    """DELETE /v1/model-access/{scope_type}/{scope_id}: removes the policy (scope inherits from its parent)."""
    scope = scope_ref(ctx, ctx.path_params.get("scope_type"), ctx.path_params.get("scope_id"), for_write=True)
    req = {"mode": "inherit", "models": [], "actions": ["predict"], "inherit_from_parent": True, "force": False,
           "reason": str((ctx.body or {}).get("reason") or "")[:300]}
    return apply_policy(ctx, scope, req, dry_run=_dry_run(ctx), if_match=ctx.headers.get("if-match"))


def preview_policy(ctx) -> dict:
    """POST /v1/model-access/{scope_type}/{scope_id}/preview: same body as PUT, never writes."""
    scope = scope_ref(ctx, ctx.path_params.get("scope_type"), ctx.path_params.get("scope_id"))
    req = parse_request(ctx, ctx.body or {})
    req["force"] = True
    return apply_policy(ctx, scope, req, dry_run=True)


def _model_toggle(ctx, allow: bool) -> dict:
    scope = scope_ref(ctx, ctx.path_params.get("scope_type"), ctx.path_params.get("scope_id"), for_write=True)
    catalog = reg.load_catalog(ctx, allow_discovery=False)
    pub, mid, _ = reg.resolve(catalog, ctx.path_params.get("model_id") or "")
    model_ref = "%s/%s" % (pub, mid)
    body = ctx.body or {}
    intent = _intent(ctx, scope)
    try:
        current = parse_policy(read_policy(ctx, scope))
    except GcpError as err:
        raise _upstream(err, scope)
    actions = _validate_actions(body.get("actions")) or (intent or {}).get("actions") or list(
        (ctx.config.get("models") or {}).get("default_actions") or ["predict"])
    mode = current["mode"]
    if intent and intent.get("mode") in ("allow_list", "deny_list"):
        mode = intent["mode"]
        models = list(intent.get("models") or [])
    elif mode in ("allow_list", "deny_list"):
        vals = current["allowed_values"] if mode == "allow_list" else current["denied_values"]
        models = []
        for v in vals:
            vpub, vmid, _ = _value_parts(v)
            ref = "%s/%s" % (vpub, vmid)
            if vmid and ref not in models and not (mode == "allow_list" and vmid == "count-tokens"):
                models.append(ref)
    else:
        models = []

    def present(lst):
        return [m for m in lst if reg.matches(mid, m.split("/", 1)[-1]) and
                (("/" not in m) or m.split("/", 1)[0] == pub)]

    if allow:
        if mode == "allow_list":
            if not present(models):
                models.append(model_ref)
        elif mode == "deny_list":
            hits = present(models)
            if any("*" in h or "?" in h for h in hits):
                raise ApiError("conflict", "Model '%s' is denied by pattern(s) %s; edit the policy with PUT instead."
                               % (mid, ", ".join(hits)))
            models = [m for m in models if m not in hits]
            if not models:
                mode = "inherit"
        elif mode == "deny_all":
            mode, models = "allow_list", [model_ref]
        else:
            eff = effective_for(ctx, scope)
            if eff.get("ok") and decide(eff["parsed"], pub, mid)[0] == "denied":
                raise ApiError("conflict", "Model '%s' is denied by an inherited policy above %s; change it at the "
                               "parent folder/organization." % (mid, scope))
            return {"object": "model_access_change", "scope": scope, "status": "unchanged",
                    "detail": "Model is already allowed (no restriction at this scope)."}
    else:
        if mode == "allow_list":
            hits = present(models)
            if any("*" in h or "?" in h for h in hits):
                raise ApiError("conflict", "Model '%s' is allowed by pattern(s) %s; edit the policy with PUT instead."
                               % (mid, ", ".join(hits)))
            models = [m for m in models if m not in hits]
        elif mode == "deny_list":
            if not present(models):
                models.append(model_ref)
        elif mode == "deny_all":
            return {"object": "model_access_change", "scope": scope, "status": "unchanged",
                    "detail": "All models are already denied at this scope."}
        else:
            mode, models = "deny_list", [model_ref]
    req = {"mode": mode, "models": models, "actions": actions,
           "inherit_from_parent": bool((intent or {}).get("inherit_from_parent", current["inherit_from_parent"])),
           "force": bool(body.get("force", False)), "reason": str(body.get("reason") or "")[:300]}
    return apply_policy(ctx, scope, req, dry_run=_dry_run(ctx), catalog=catalog)


def allow_model(ctx) -> dict:
    """POST /v1/model-access/{scope_type}/{scope_id}/models/{model_id}/allow (lift a restriction)."""
    return _model_toggle(ctx, True)


def restrict_model(ctx) -> dict:
    """POST /v1/model-access/{scope_type}/{scope_id}/models/{model_id}/restrict"""
    return _model_toggle(ctx, False)


def bulk_apply(ctx) -> dict:
    """POST /v1/model-access/bulk-apply {scopes: ["projects/*"] | [...], mode, models, actions?, dryRun?, force?}"""
    body = ctx.body or {}
    raw_scopes = body.get("scopes") or ["projects/*"]
    if not isinstance(raw_scopes, list) or len(raw_scopes) > MAX_BULK_SCOPES:
        raise ApiError("bad_request", "Field 'scopes' must be a list (max %d)." % MAX_BULK_SCOPES)
    scopes = []
    for s in raw_scopes:
        s = str(s or "").strip().lower()
        if s == "projects/*":
            scopes += ["projects/%s" % p for p in ctx.all_projects]
            continue
        if "/" not in s:
            raise ApiError("bad_request", "Scope '%s' must look like projects/ID, folders/ID or organizations/ID." % s)
        stype, sid = s.split("/", 1)
        scopes.append(scope_ref(ctx, stype, sid, for_write=True))
    for s in scopes:
        scope_ref(ctx, *_split_scope(s), for_write=True)
    scopes = list(dict.fromkeys(scopes))
    req = parse_request(ctx, body)
    dry_run = _dry_run(ctx)
    catalog = reg.load_catalog(ctx, allow_discovery=False)
    tasks = {s: (lambda s=s: apply_policy(ctx, s, dict(req), dry_run=dry_run, catalog=catalog, preview=False))
             for s in scopes}
    outcomes = ctx.run(tasks) if tasks else {}
    results = []
    for s in scopes:
        ok, val = outcomes.get(s, (False, None))
        if ok:
            results.append({k: val.get(k) for k in ("scope", "status", "operation", "expanded_models")})
        else:
            results.append({"scope": s, "status": "failed", "error": getattr(val, "code", getattr(val, "kind", "error")),
                            "message": getattr(val, "message", "")[:300]})
    counts = {k: sum(1 for r in results if r["status"] == k) for k in ("applied", "unchanged", "dry_run", "failed")}
    audit.write_audit(ctx, "model_policy.bulk_applied", target="bulk",
                      details={"scopes": len(scopes), "mode": req["mode"], "models": req["models"], **counts})
    return {"object": "bulk_result", "dry_run": dry_run, "results": results, **counts}


def reconcile(ctx, *, dry_run: bool = False) -> dict:
    """Re-expands stored intents (new model versions) and applies drift. Used by API and the scheduled enforcer."""
    doc, _ = ctx.store.load(INTENT_DOC)
    catalog = reg.load_catalog(ctx)
    results = []
    for scope, intent in sorted((doc.get("scopes") or {}).items()):
        if not isinstance(intent, dict) or intent.get("mode") not in ("allow_list", "deny_list"):
            continue
        try:
            req = {"mode": intent["mode"], "models": intent.get("models") or [], "actions": intent.get("actions") or
                   ["predict"], "inherit_from_parent": bool(intent.get("inherit_from_parent")), "force": True,
                   "reason": "reconcile"}
            res = apply_policy(ctx, scope, req, dry_run=dry_run, catalog=catalog, preview=False)
            results.append({"scope": scope, "status": res["status"], "operation": res.get("operation")})
        except ApiError as err:
            results.append({"scope": scope, "status": "failed", "error": err.code, "message": err.message[:200]})
    return {"object": "model_access_reconcile", "dry_run": dry_run, "results": results,
            "applied": sum(1 for r in results if r["status"] == "applied")}


def reconcile_handler(ctx) -> dict:
    """POST /v1/model-access/reconcile {dryRun?}"""
    return reconcile(ctx, dry_run=_dry_run(ctx))
