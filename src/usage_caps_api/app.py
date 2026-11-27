"""Headless HTTP Application: /v1 routing, M2M auth & scope checks, rate limiting, and OpenAPI serving."""

from __future__ import annotations

import logging
import os
import signal
import sys

from . import __version__, auth, config, identity, timeutil
from .cache import TTLCache
from .gcp import Gcp
from .httpkit import ApiError, BaseHandler, Router, Server, now_iso, parse_json_object, parse_query
from .schema import SchemaRegistry
from .services import (audit, base as svc_base, caps, chargeback, groups, model_access, model_registry, operations,
                       pricing_sync, quotas, readiness, spend_caps, surface_models, usage, usage_events,
                       user_model_rules, vertex_logging, webhooks)
from .store import StoreConflict, StoreError, make_doc_store

LOG = logging.getLogger("usage_caps_api.app")

ROUTER = Router()


def _configure_logging():
    level = getattr(logging, (os.environ.get("LOG_LEVEL") or "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )


# ---------------------------------------------------------------------------
# System & Admin Key handlers
# ---------------------------------------------------------------------------

def _health(ctx) -> dict:
    pol = caps.effective_policy(ctx)
    doc, _ = ctx.store.load(caps.USER_CAPS_DOC)
    base = {
        "status": "ok",
        "service": "gcp-ai-usage-caps-api",
        "version": __version__,
        "timestamp": now_iso(),
    }
    if ctx.caller is not None:
        base.update({
            "workspace_configured": config.workspace_configured(ctx.config),
            "storage": {"backend": ctx.store.kind, "durable": ctx.store.durable},
            "enforcement": {
                "mode": pol.get("enforcement_mode"),
                "backend": pol.get("enforcement_backend"),
                "deny_policy_id": pol.get("deny_policy_id"),
                "last_run": doc.get("last_run"),
            },
            "spend_caps_enabled": spend_caps.enabled(ctx.config),
        })
    return base


def _admin_list_keys(ctx) -> dict:
    keys = auth.list_api_keys(ctx.store)
    return {"object": "list", "data": keys, "total": len(keys)}


def _admin_create_key(ctx) -> dict:
    body = ctx.body or {}
    created = auth.create_api_key(
        ctx.store,
        name=body.get("name"),
        scopes=body.get("scopes"),
        created_by=ctx.caller.id,
        expires_at=body.get("expires_at"),
    )
    audit.write_audit(ctx, "api_key.created", target=created["id"],
                      details={"name": created["name"], "scopes": created["scopes"]})
    return created


def _admin_revoke_key(ctx) -> dict:
    key_id = ctx.path_params.get("key_id") or ""
    revoked = auth.revoke_api_key(ctx.store, key_id, revoked_by=ctx.caller.id)
    audit.write_audit(ctx, "api_key.revoked", target=key_id)
    return revoked


def _notify_readiness(ctx, report: dict) -> dict:
    """Emits `readiness.degraded` (once per overall state per day) when the environment is not fully ready."""
    if isinstance(report, dict) and report.get("overall") in ("blocked", "ready_with_warnings") and \
            not report.get("cached"):
        webhooks.notify_once(ctx, "%s|%s" % (report["overall"], now_iso()[:10]), "readiness.degraded", {
            "overall": report["overall"], "summary": report.get("summary"), "generated_at": report.get("generated_at"),
        })
    return report


def _readiness_get(ctx) -> dict:
    return _notify_readiness(ctx, readiness.get_readiness(ctx))


def _readiness_run(ctx) -> dict:
    return _notify_readiness(ctx, readiness.run_readiness_handler(ctx))


def _project_capabilities(ctx) -> dict:
    project = (ctx.path_params.get("project_id") or "").strip().lower()
    if project not in ctx.all_projects:
        raise ApiError("forbidden", "Project '%s' is not managed by this control plane." % project)
    return readiness.capabilities(ctx, project)


# ---------------------------------------------------------------------------
# Route table (/v1/...)
# ---------------------------------------------------------------------------

ROUTER.route("GET", "/v1/health", public=True, optional_auth=True, rate="general")(_health)
ROUTER.route("GET", "/v1/usage/users", scope="usage:read", range=True, rate="general")(usage.list_user_usage)
ROUTER.route("GET", "/v1/usage/users/{email}", scope="usage:read", range=True, rate="general")(usage.get_user_usage)
ROUTER.route("GET", "/v1/billing/users", scope="usage:read", rate="general")(chargeback.billing_users_report)

ROUTER.route("GET", "/v1/caps/policy", scope="caps:read", rate="general")(caps.get_policy)
ROUTER.route("PUT", "/v1/caps/policy", scope="caps:write", rate="write")(caps.update_policy)
ROUTER.route("GET", "/v1/caps/users", scope="caps:read", rate="general")(caps.list_user_caps)
ROUTER.route("GET", "/v1/caps/users/{email}", scope="caps:read", rate="general")(caps.get_user_cap)
ROUTER.route("PUT", "/v1/caps/users/{email}", scope="caps:write", rate="write")(caps.put_user_cap)
ROUTER.route("DELETE", "/v1/caps/users/{email}", scope="caps:write", rate="write")(caps.delete_user_cap)
ROUTER.route("POST", "/v1/caps/enforce", scope="caps:write", rate="enforce")(caps.enforce_handler)

ROUTER.route("POST", "/v1/users/{email}/block", scope="users:write", rate="write")(caps.block_user)
ROUTER.route("POST", "/v1/users/{email}/unblock", scope="users:write", rate="write")(caps.unblock_user)
ROUTER.route("GET", "/v1/users/{email}/projects", scope="usage:read", range=True,
             rate="general")(usage.get_user_projects)

# -- Groups (first-class policy subjects, same capabilities as users) ---------
ROUTER.route("GET", "/v1/groups", scope=("caps:read", "users:write"), rate="general")(groups.list_groups)
ROUTER.route("POST", "/v1/groups", scope="users:write", rate="write")(groups.create_group)
ROUTER.route("GET", "/v1/groups/{group_id}", scope=("caps:read", "users:write"), rate="general")(groups.get_group)
ROUTER.route("PATCH", "/v1/groups/{group_id}", scope="users:write", rate="write")(groups.patch_group)
ROUTER.route("DELETE", "/v1/groups/{group_id}", scope="users:write", rate="write")(groups.delete_group)
ROUTER.route("POST", "/v1/groups/{group_id}/members", scope="users:write", rate="write")(groups.update_members)
ROUTER.route("POST", "/v1/groups/{group_id}/exclusions", scope="users:write", rate="write")(groups.update_exclusions)
ROUTER.route("POST", "/v1/groups/{group_id}/sync", scope="users:write", rate="enforce")(groups.sync_group)
ROUTER.route("POST", "/v1/groups/{group_id}/block", scope="users:write", rate="write")(groups.block_group)
ROUTER.route("POST", "/v1/groups/{group_id}/unblock", scope="users:write", rate="write")(groups.unblock_group)
ROUTER.route("GET", "/v1/groups/{group_id}/model-rules", scope="models:read", rate="general")(groups.get_group_model_rule)
ROUTER.route("PUT", "/v1/groups/{group_id}/model-rules", scope="models:write", rate="write")(groups.put_group_model_rule)
ROUTER.route("DELETE", "/v1/groups/{group_id}/model-rules", scope="models:write",
             rate="write")(groups.delete_group_model_rule)
ROUTER.route("GET", "/v1/caps/groups", scope="caps:read", rate="general")(groups.list_group_caps)
ROUTER.route("GET", "/v1/caps/groups/{group_id}", scope="caps:read", rate="general")(groups.get_group_cap)
ROUTER.route("PUT", "/v1/caps/groups/{group_id}", scope="caps:write", rate="write")(groups.put_group_cap)
ROUTER.route("DELETE", "/v1/caps/groups/{group_id}", scope="caps:write", rate="write")(groups.delete_group_cap)
ROUTER.route("GET", "/v1/usage/groups", scope="usage:read", range=True, rate="general")(groups.list_group_usage)
ROUTER.route("GET", "/v1/usage/groups/{group_id}", scope="usage:read", range=True, rate="general")(groups.get_group_usage)
ROUTER.route("GET", "/v1/users/{email}/groups", scope=("usage:read", "caps:read"), rate="general")(groups.get_user_groups)

ROUTER.route("GET", "/v1/caps/projects", scope="caps:read", rate="general")(spend_caps.list_project_caps)
ROUTER.route("PUT", "/v1/caps/projects/{project_id}", scope="caps:write", rate="write")(spend_caps.put_project_cap)
ROUTER.route("POST", "/v1/caps/projects/{project_id}/lift", scope="caps:write", rate="write")(spend_caps.lift_project_cap)

ROUTER.route("GET", "/v1/audit-logs", scope=("caps:read", "admin"), rate="general")(audit.list_audit_logs)

ROUTER.route("POST", "/v1/internal/usage-event", scope=(auth.INTERNAL_SCOPE, "admin"), rate="usage_event")(usage_events.handle_usage_event)
ROUTER.route("POST", "/v1/internal/pricing/sync", scope=(auth.INTERNAL_SCOPE, "admin"), rate="enforce")(pricing_sync.sync_pricing)
ROUTER.route("GET", "/v1/pricing", scope=("usage:read", "models:read"), rate="general")(pricing_sync.get_pricing)
ROUTER.route("GET", "/v1/projects/{project_id}/vertex-logging", scope=("usage:read", "admin"),
             rate="general")(vertex_logging.get_handler)
ROUTER.route("POST", "/v1/projects/{project_id}/vertex-logging/enable", scope="admin",
             rate="write")(vertex_logging.enable_handler)
ROUTER.route("POST", "/v1/vertex-logging/enable", scope="admin", rate="enforce")(vertex_logging.bulk_enable_handler)

ROUTER.route("GET", "/v1/admin/api-keys", scope="admin", rate="general")(_admin_list_keys)
ROUTER.route("POST", "/v1/admin/api-keys", scope="admin", rate="write")(_admin_create_key)
ROUTER.route("DELETE", "/v1/admin/api-keys/{key_id}", scope="admin", rate="write")(_admin_revoke_key)

ROUTER.route("GET", "/v1/admin/webhooks", scope="admin", rate="general")(webhooks.list_webhooks)
ROUTER.route("POST", "/v1/admin/webhooks", scope="admin", rate="write")(webhooks.create_webhook)
ROUTER.route("DELETE", "/v1/admin/webhooks/{webhook_id}", scope="admin", rate="write")(webhooks.delete_webhook)

# -- Models: catalog, usage, access decision ---------------------------------
ROUTER.route("GET", "/v1/models", scope="models:read", rate="general")(model_registry.list_models)
ROUTER.route("POST", "/v1/models/sync", scope="models:write", rate="enforce")(model_registry.sync_models)
ROUTER.route("GET", "/v1/models/{model_id}", scope="models:read", rate="general")(model_registry.get_model)
ROUTER.route("GET", "/v1/models/{model_id}/access", scope="models:read", rate="general")(model_registry.get_model_access)
ROUTER.route("GET", "/v1/usage/models", scope=("usage:read", "models:read"), range=True,
             rate="general")(model_registry.usage_by_model)

# -- Model access policies (Gemini Platform, Org Policy vertexai.allowedModels) -----
ROUTER.route("GET", "/v1/model-access", scope="models:read", rate="general")(model_access.list_policies)
ROUTER.route("POST", "/v1/model-access/bulk-apply", scope="models:write", rate="enforce")(model_access.bulk_apply)
ROUTER.route("POST", "/v1/model-access/reconcile", scope="models:write", rate="enforce")(model_access.reconcile_handler)
ROUTER.route("GET", "/v1/model-access/{scope_type}/{scope_id}", scope="models:read", rate="general")(model_access.get_policy)
ROUTER.route("PUT", "/v1/model-access/{scope_type}/{scope_id}", scope="models:write", rate="write")(model_access.put_policy)
ROUTER.route("DELETE", "/v1/model-access/{scope_type}/{scope_id}", scope="models:write",
             rate="write")(model_access.delete_policy)
ROUTER.route("POST", "/v1/model-access/{scope_type}/{scope_id}/preview", scope="models:read",
             rate="enforce")(model_access.preview_policy)
ROUTER.route("POST", "/v1/model-access/{scope_type}/{scope_id}/models/{model_id}/allow", scope="models:write",
             rate="write")(model_access.allow_model)
ROUTER.route("POST", "/v1/model-access/{scope_type}/{scope_id}/models/{model_id}/restrict", scope="models:write",
             rate="write")(model_access.restrict_model)

# -- Surface model controls ----------------------------------------------------
ROUTER.route("GET", "/v1/gemini-enterprise/apps", scope="models:read", rate="general")(surface_models.list_apps_handler)
ROUTER.route("POST", "/v1/gemini-enterprise/apps/bulk-models", scope="models:write",
             rate="enforce")(surface_models.bulk_patch_app_models)
ROUTER.route("GET", "/v1/gemini-enterprise/apps/{app_id}/models", scope="models:read",
             rate="general")(surface_models.get_app_models)
ROUTER.route("PATCH", "/v1/gemini-enterprise/apps/{app_id}/models", scope="models:write",
             rate="write")(surface_models.patch_app_models)
ROUTER.route("GET", "/v1/antigravity/models", scope="models:read", rate="general")(surface_models.agy_models_handler)
ROUTER.route("PUT", "/v1/antigravity/models", scope="models:write", rate="write")(surface_models.agy_models_write_handler)

# -- Per-user model rules & violations -----------------------------------------
ROUTER.route("GET", "/v1/model-rules", scope="models:read", rate="general")(user_model_rules.list_user_rules)
ROUTER.route("GET", "/v1/model-rules/default", scope="models:read", rate="general")(user_model_rules.get_default_rule)
ROUTER.route("PUT", "/v1/model-rules/default", scope="models:write", rate="write")(user_model_rules.put_default_rule)
ROUTER.route("DELETE", "/v1/model-rules/default", scope="models:write",
             rate="write")(user_model_rules.delete_default_rule)
ROUTER.route("POST", "/v1/model-rules/evaluate", scope="models:write", rate="enforce")(user_model_rules.evaluate_handler)
ROUTER.route("GET", "/v1/model-violations", scope="models:read", rate="general")(user_model_rules.list_violations)
ROUTER.route("GET", "/v1/users/{email}/model-rules", scope="models:read", rate="general")(user_model_rules.get_user_rule)
ROUTER.route("PUT", "/v1/users/{email}/model-rules", scope="models:write", rate="write")(user_model_rules.put_user_rule)
ROUTER.route("DELETE", "/v1/users/{email}/model-rules", scope="models:write",
             rate="write")(user_model_rules.delete_user_rule)

# -- Rate limits & quotas -----------------------------------------------------
ROUTER.route("GET", "/v1/rate-limits", scope="quotas:read", rate="general")(quotas.list_all_limits)
ROUTER.route("GET", "/v1/rate-limits/users/{email}", scope="quotas:read",
             rate="general")(user_model_rules.get_user_rate_limits)
ROUTER.route("PUT", "/v1/rate-limits/users/{email}", scope="quotas:write",
             rate="write")(user_model_rules.put_user_rate_limits)
ROUTER.route("DELETE", "/v1/rate-limits/users/{email}", scope="quotas:write",
             rate="write")(user_model_rules.delete_user_rate_limits)
ROUTER.route("GET", "/v1/projects/{project_id}/rate-limits", scope="quotas:read", rate="general")(quotas.list_project_limits)
ROUTER.route("GET", "/v1/projects/{project_id}/rate-limits/{limit_id}", scope="quotas:read",
             rate="general")(quotas.get_project_limit)
ROUTER.route("PATCH", "/v1/projects/{project_id}/rate-limits/{limit_id}", scope="quotas:write",
             rate="write")(quotas.patch_project_limit)
ROUTER.route("GET", "/v1/quota-requests", scope="quotas:read", rate="general")(quotas.list_requests)
ROUTER.route("POST", "/v1/quota-requests", scope="quotas:write", rate="write")(quotas.create_request)
ROUTER.route("GET", "/v1/quota-requests/{request_id}", scope="quotas:read", rate="general")(quotas.get_request)
ROUTER.route("POST", "/v1/quota-requests/{request_id}/cancel", scope="quotas:write", rate="write")(quotas.cancel_request)

# -- Environment readiness (preflight) ------------------------------------------
ROUTER.route("GET", "/v1/admin/readiness", scope="admin", rate="general")(_readiness_get)
ROUTER.route("POST", "/v1/admin/readiness/run", scope="admin", rate="enforce")(_readiness_run)
ROUTER.route("GET", "/v1/projects/{project_id}/capabilities", scope=("models:read", "quotas:read", "caps:read"),
             rate="general")(_project_capabilities)

# -- Operations ---------------------------------------------------------------
ROUTER.route("GET", "/v1/operations", scope=("models:read", "quotas:read", "caps:read"),
             rate="general")(operations.list_operations)
ROUTER.route("GET", "/v1/operations/{operation_id}", scope=("models:read", "quotas:read", "caps:read"),
             rate="general")(operations.get_operation)


# ---------------------------------------------------------------------------
# App container & HTTP Handler
# ---------------------------------------------------------------------------

class App:
    def __init__(self, *, environ=None, cfg=None, gcp=None, store=None, verifier=None, webhook_sender=None):
        self.environ = os.environ if environ is None else environ
        self.config = cfg if cfg is not None else config.load_config(self.environ)
        hub_cfg = self.config.get("hub") or {}
        identity.configure(service_account_subjects=bool(hub_cfg.get("service_account_subjects")),
                           protected=(hub_cfg.get("runtime_service_account"), hub_cfg.get("enforcer_service_account"),
                                      auth.runtime_service_account(self.environ)))
        self.gcp = gcp if gcp is not None else Gcp()
        self.registry = SchemaRegistry(self.gcp)
        self.cache = TTLCache(1024)
        self.store = store if store is not None else make_doc_store(self.config, self.gcp, self.environ)
        self.authenticator = auth.Authenticator(self.store, self.config, verifier=verifier, environ=self.environ)
        self.limiter = auth.RateLimiter()
        self.webhook_sender = webhook_sender
        self._openapi_text: str | None = None

    def openapi_yaml(self) -> str:
        if self._openapi_text is None:
            path = os.path.join(config.repo_root(), "openapi.yaml")
            if not os.path.isfile(path):
                raise ApiError("not_found", "OpenAPI specification file not found.")
            with open(path, "r", encoding="utf-8") as fh:
                self._openapi_text = fh.read()
        return self._openapi_text

    def make_handler(self):
        app = self

        class Handler(BaseHandler):
            def handle_api(self, method: str, path: str, query: str):
                if path == "/v1/openapi.yaml":
                    if method != "GET":
                        raise ApiError("method_not_allowed", "Method not allowed.", status=405,
                                       headers=[("Allow", "GET")])
                    self.send_text(200, app.openapi_yaml(), content_type="application/yaml; charset=utf-8")
                    return

                fn, opts, path_params = ROUTER.resolve(method, path)
                params = parse_query(query)
                is_public = bool(opts.get("public"))
                optional_auth = bool(opts.get("optional_auth"))
                caller = None
                try:
                    if not is_public or optional_auth:
                        caller = app.authenticator.authenticate(
                            self.headers, self.client_ip(), path, allow_optional=is_public,
                        )
                    if caller is not None:
                        req_scope = opts.get("scope")
                        if req_scope and not caller.has_scope(req_scope):
                            needed = req_scope if isinstance(req_scope, str) else " or ".join(req_scope)
                            raise ApiError("insufficient_scope",
                                           "Missing required scope '%s'." % needed, status=403)
                        app.limiter.check(caller.rate_key, opts.get("rate") or "general")
                    else:
                        app.limiter.check("anon:" + (self.client_ip() or "unknown"), "general")

                    body = {}
                    if method in ("POST", "PUT", "PATCH", "DELETE"):
                        raw = self.read_body_bytes()
                        if raw:
                            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                            if ctype and ctype != "application/json":
                                raise ApiError("bad_request", "Content-Type must be application/json.")
                            body = parse_json_object(raw)

                    rng = None
                    if opts.get("range"):
                        ws = app.config["workspace"]
                        rng = timeutil.parse_range(params, ws.get("timezone") or "UTC", ws.get("default_range_days", 14))

                    fwd_headers = {name: self.headers.get(name) for name in ("If-Match", "Idempotency-Key")}
                    ctx = svc_base.Ctx(app, app.config, caller, params=params, path_params=path_params, body=body, rng=rng,
                                       headers=fwd_headers)
                    result = fn(ctx)
                except StoreConflict:
                    raise ApiError("conflict", "Concurrent state update conflict; retry the request.", status=409)
                except StoreError:
                    raise ApiError("unavailable", "State storage is temporarily unavailable; retry shortly.", status=503)
                if isinstance(result, dict) and ctx.warnings:
                    result = dict(result, warnings=ctx.warnings)
                created = path in ("/v1/admin/api-keys", "/v1/admin/webhooks", "/v1/groups") or (
                    path == "/v1/quota-requests" and isinstance(result, dict) and result.get("object") == "quota_request"
                    and result.get("status") != "dry_run" and not result.get("idempotent_replay"))
                status = 201 if (method == "POST" and created) else 200
                self.send_json(status, result)

        return Handler


def run_server():
    _configure_logging()
    app = App()
    host = app.config["server"]["host"]
    port = int(app.config["server"]["port"])
    httpd = Server((host, port), app.make_handler())

    def _shutdown(signum, _frame):
        LOG.info("shutting down signal=%s", signum)
        httpd.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    LOG.info("gcp-ai-usage-caps-api v%s listening on %s:%d storage=%s",
             __version__, host, port, app.store.kind)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


if __name__ == "__main__":
    sys.exit(run_server() or 0)
