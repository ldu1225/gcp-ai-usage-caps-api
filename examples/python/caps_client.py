"""Minimal Python client for the GCP AI Usage & Spend Caps API (stdlib only).

There is NO built-in model list anywhere in the API or in this client. Model IDs are whatever
`GET /v1/models` (live Model Garden + models observed in usage logs) returns - never hardcode them; use publisher
wildcards (`google/*`, `anthropic/*`), family patterns (`<family>-*`) or IDs taken from that response.

    from caps_client import CapsClient
    c = CapsClient("https://gcp-ai-usage-caps-api-123.asia-northeast3.run.app", api_key="gcpai_...")

    used = c.list_models(used="true")                     # models actually observed in logs
    model_id = used[0]["id"]                              # always take IDs from the catalog
    c.set_model_access("projects", "team-a-prod", mode="allow_list", models=["google/*"], dry_run=True)
    c.set_default_model_rule(mode="allow_list", models=["google/*"])          # block-first by default
    c.request_quota(project_id="team-a-prod", quota_id="GenerateContentRequestsPerMinutePerProjectPerBaseModel",
                    model=model_id, location="us-central1", requested_value=5000,
                    justification="Company-wide coding assistant rollout", contact_email="cloud@company.com")

    c.pricing(model=model_id)                             # official Cloud Billing Catalog SKU prices in use
    c.user_projects("alice@company.com", include="iam")   # multi-project view (usage + per-project caps)
    c.block_user("alice@company.com", services=["gemini_platform"], projects=["team-b-prod"])   # project-scoped
    c.enable_vertex_logging("team-a-prod", dry_run=True)  # Vertex AI token source (inference_request log)

Errors raise CapsApiError(status, type, code, message) mirroring the OpenAI/Anthropic error envelope.
429 responses are retried with the `Retry-After` header (bounded).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class CapsApiError(Exception):
    def __init__(self, status: int, err_type: str, code: str, message: str):
        super().__init__("%s %s: %s" % (status, code, message))
        self.status, self.type, self.code, self.message = status, err_type, code, message


class CapsClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: int = 60, max_retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries

    # -- transport ------------------------------------------------------------
    def request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
                headers: dict | None = None):
        query = ""
        if params:
            clean = {k: ("true" if v is True else "false" if v is False else v) for k, v in params.items() if v is not None}
            query = "?" + urllib.parse.urlencode(clean)
        hdrs = {"Authorization": "Bearer " + self.api_key, "Accept": "application/json"}
        hdrs.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(self.base_url + path + query, data=data, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as err:
                raw = err.read().decode("utf-8", "replace")
                if err.code == 429 and attempt < self.max_retries:
                    time.sleep(min(30, int(err.headers.get("Retry-After") or 2 ** attempt)))
                    continue
                try:
                    e = json.loads(raw).get("error") or {}
                except ValueError:
                    e = {}
                raise CapsApiError(err.code, e.get("type", "api_error"), e.get("code", "http_%d" % err.code),
                                   e.get("message", raw[:300]))
        raise CapsApiError(429, "rate_limit_error", "rate_limited", "retries exhausted")

    def paginate(self, path: str, **params):
        cursor = None
        while True:
            page = self.request("GET", path, params=dict(params, cursor=cursor))
            yield from page.get("data") or []
            if not page.get("has_more"):
                return
            cursor = page.get("next_cursor")

    @staticmethod
    def _email(email: str) -> str:
        return urllib.parse.quote(email, safe="")

    # -- usage & caps -----------------------------------------------------------
    def list_user_usage(self, **params):
        return list(self.paginate("/v1/usage/users", **params))

    def user_usage(self, email: str, **params):
        return self.request("GET", "/v1/usage/users/" + self._email(email), params=params)

    def usage_by_model(self, **params):
        return list(self.paginate("/v1/usage/models", **params))

    def set_user_cap(self, email: str, **cap):
        return self.request("PUT", "/v1/caps/users/" + self._email(email), body=cap)

    def block_user(self, email: str, services=None, reason: str = "", until: str | None = None, projects=None):
        """Blocks `services` for a user; `projects=[...]` limits the block to those managed projects (default: all)."""
        body = {"services": services, "reason": reason, "until": until}
        if projects is not None:
            body["projects"] = list(projects)
        return self.request("POST", "/v1/users/%s/block" % self._email(email), body=body)

    def unblock_user(self, email: str, services=None, reason: str = "", projects=None):
        """Unblocks a user everywhere, or only in `projects=[...]` (other project blocks stay in place)."""
        body = {"reason": reason}
        if services is not None:
            body["services"] = services
        if projects is not None:
            body["projects"] = list(projects)
        return self.request("POST", "/v1/users/%s/unblock" % self._email(email), body=body)

    def user_projects(self, email: str, **params):
        """GET /v1/users/{email}/projects - every managed project the user used / is bound to (`include="iam"`)."""
        return self.request("GET", "/v1/users/%s/projects" % self._email(email), params=params)

    def pricing(self, model: str | None = None):
        """GET /v1/pricing - official Cloud Billing Catalog SKU prices (and overrides) used for cost estimates."""
        return self.request("GET", "/v1/pricing", params={"model": model} if model else None)

    # -- Vertex AI inference request logging (token source) ----------------------
    def vertex_logging_status(self, project_id: str):
        return self.request("GET", "/v1/projects/%s/vertex-logging" % project_id)

    def enable_vertex_logging(self, project_id: str, dry_run: bool = False):
        return self.request("POST", "/v1/projects/%s/vertex-logging/enable" % project_id, body={"dryRun": dry_run})

    def enable_vertex_logging_bulk(self, projects=("*",), dry_run: bool = True):
        return self.request("POST", "/v1/vertex-logging/enable", body={"projects": list(projects), "dryRun": dry_run})

    # -- models -------------------------------------------------------------------
    def list_models(self, **params):
        return list(self.paginate("/v1/models", **params))

    def get_model(self, model_id: str):
        return self.request("GET", "/v1/models/" + urllib.parse.quote(model_id, safe=""))

    def get_model_access(self, scope_type: str, scope_id: str):
        return self.request("GET", "/v1/model-access/%s/%s" % (scope_type, scope_id))

    def set_model_access(self, scope_type: str, scope_id: str, *, mode: str, models=None, actions=None,
                         dry_run: bool = False, force: bool = False, etag: str | None = None, reason: str = ""):
        body = {"mode": mode, "models": models or [], "dryRun": dry_run, "force": force, "reason": reason}
        if actions:
            body["actions"] = actions
        return self.request("PUT", "/v1/model-access/%s/%s" % (scope_type, scope_id), body=body,
                            headers={"If-Match": etag} if etag else None)

    def allow_model(self, scope_type: str, scope_id: str, model_id: str, dry_run: bool = False):
        return self.request("POST", "/v1/model-access/%s/%s/models/%s/allow" % (scope_type, scope_id, model_id),
                            body={"dryRun": dry_run})

    def restrict_model(self, scope_type: str, scope_id: str, model_id: str, dry_run: bool = False):
        return self.request("POST", "/v1/model-access/%s/%s/models/%s/restrict" % (scope_type, scope_id, model_id),
                            body={"dryRun": dry_run})

    def bulk_model_access(self, *, mode: str, models, scopes=("projects/*",), dry_run: bool = True):
        return self.request("POST", "/v1/model-access/bulk-apply",
                            body={"scopes": list(scopes), "mode": mode, "models": list(models), "dryRun": dry_run})

    def set_default_model_rule(self, **rule):
        return self.request("PUT", "/v1/model-rules/default", body=rule)

    def set_user_model_rule(self, email: str, **rule):
        return self.request("PUT", "/v1/users/%s/model-rules" % self._email(email), body=rule)

    def model_violations(self, **params):
        return list(self.paginate("/v1/model-violations", **params))

    # -- rate limits & quotas ---------------------------------------------------
    def rate_limits(self, **params):
        return list(self.paginate("/v1/rate-limits", **params))

    def request_quota(self, *, idempotency_key: str | None = None, **body):
        return self.request("POST", "/v1/quota-requests", body=body,
                            headers={"Idempotency-Key": idempotency_key or str(uuid.uuid4())})

    def quota_request(self, request_id: str):
        return self.request("GET", "/v1/quota-requests/" + request_id)

    def wait_operation(self, operation_id: str, *, poll_seconds: int = 30, timeout_seconds: int = 1800):
        deadline = time.time() + timeout_seconds
        while True:
            op = self.request("GET", "/v1/operations/" + operation_id)
            if op.get("status") != "propagating" or time.time() > deadline:
                return op
            time.sleep(poll_seconds)

    # -- readiness ------------------------------------------------------------------
    def readiness(self, refresh: bool = False):
        return self.request("GET", "/v1/admin/readiness", params={"refresh": refresh})
