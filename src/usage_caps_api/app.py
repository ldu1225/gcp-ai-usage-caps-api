"""Headless HTTP Application: /v1 routing, M2M auth & scope checks, rate limiting, and OpenAPI serving."""

from __future__ import annotations

import logging
import os
import signal
import sys

from . import __version__, auth, config, timeutil
from .cache import TTLCache
from .gcp import Gcp
from .httpkit import ApiError, BaseHandler, Router, Server, now_iso, parse_json_object, parse_query
from .schema import SchemaRegistry
from .services import (audit, base as svc_base, caps, chargeback, pricing_sync, spend_caps, usage,
                       usage_events, webhooks)
from .store import make_doc_store

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
        name=body.get("name") or "",
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

ROUTER.route("GET", "/v1/caps/projects", scope="caps:read", rate="general")(spend_caps.list_project_caps)
ROUTER.route("PUT", "/v1/caps/projects/{project_id}", scope="caps:write", rate="write")(spend_caps.put_project_cap)
ROUTER.route("POST", "/v1/caps/projects/{project_id}/lift", scope="caps:write", rate="write")(spend_caps.lift_project_cap)

ROUTER.route("GET", "/v1/audit-logs", scope=("caps:read", "admin"), rate="general")(audit.list_audit_logs)

ROUTER.route("POST", "/v1/internal/usage-event", scope=(auth.INTERNAL_SCOPE, "admin"), rate="usage_event")(usage_events.handle_usage_event)
ROUTER.route("POST", "/v1/internal/pricing/sync", scope=(auth.INTERNAL_SCOPE, "admin"), rate="enforce")(pricing_sync.sync_pricing)

ROUTER.route("GET", "/v1/admin/api-keys", scope="admin", rate="general")(_admin_list_keys)
ROUTER.route("POST", "/v1/admin/api-keys", scope="admin", rate="write")(_admin_create_key)
ROUTER.route("DELETE", "/v1/admin/api-keys/{key_id}", scope="admin", rate="write")(_admin_revoke_key)

ROUTER.route("GET", "/v1/admin/webhooks", scope="admin", rate="general")(webhooks.list_webhooks)
ROUTER.route("POST", "/v1/admin/webhooks", scope="admin", rate="write")(webhooks.create_webhook)
ROUTER.route("DELETE", "/v1/admin/webhooks/{webhook_id}", scope="admin", rate="write")(webhooks.delete_webhook)


# ---------------------------------------------------------------------------
# App container & HTTP Handler
# ---------------------------------------------------------------------------

class App:
    def __init__(self, *, environ=None, cfg=None, gcp=None, store=None, verifier=None, webhook_sender=None):
        self.environ = os.environ if environ is None else environ
        self.config = cfg if cfg is not None else config.load_config(self.environ)
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
                if method in ("POST", "PUT", "DELETE"):
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

                ctx = svc_base.Ctx(app, app.config, caller, params=params, path_params=path_params, body=body, rng=rng)
                result = fn(ctx)
                if isinstance(result, dict) and ctx.warnings:
                    result = dict(result, warnings=ctx.warnings)
                status = 201 if (method == "POST" and path in ("/v1/admin/api-keys", "/v1/admin/webhooks")) else 200
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
