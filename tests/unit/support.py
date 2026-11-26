"""Shared offline test fakes and live loopback HTTP server helper."""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import sys
import threading
import urllib.error
import urllib.request

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from usage_caps_api import config  # noqa: E402
from usage_caps_api.app import App  # noqa: E402
from usage_caps_api.gcp import GcpError, QueryResult  # noqa: E402
from usage_caps_api.httpkit import Server  # noqa: E402
from usage_caps_api.store import DocStore, MemoryBackend  # noqa: E402


class FakeGcp:
    """In-memory fake for Gcp REST & BigQuery calls."""

    def __init__(self):
        self.rest_calls: list[tuple] = []
        self.query_calls: list[tuple] = []
        self.rest_handlers = []      # list of (predicate, callable_or_value)
        self.query_handlers = []     # list of (predicate, rows_or_callable)
        self.deny_policies: dict[str, dict] = {}
        self.budgets: list[dict] = []
        self.log_entries: list[dict] = []
        self._etag_seq = 1

    def next_etag(self) -> str:
        self._etag_seq += 1
        return "etag-%d" % self._etag_seq

    def request(self, method: str, url: str, *, quota_project: str = "", params=None, json_body=None,
                data=None, headers=None, timeout: int = 30, expect_json: bool = True):
        self.rest_calls.append((method, url, params, copy.deepcopy(json_body)))
        for pred, handler in self.rest_handlers:
            if pred(method, url, params, json_body):
                res = handler(method, url, params, json_body) if callable(handler) else handler
                return copy.deepcopy(res), {}

        # Default Cloud Logging inference entries & audit writes
        if "logging.googleapis.com/v2/entries:write" in url:
            return {}, {}
        if "logging.googleapis.com/v2/entries:list" in url:
            return {"entries": copy.deepcopy(self.log_entries)}, {}

        # Default IAM Deny v2 in-memory simulation
        if "iam.googleapis.com/v2/policies/" in url and "/denypolicies" in url:
            if method == "GET":
                pol = self.deny_policies.get(url)
                if pol is None:
                    raise GcpError("not_found", status=404, message="not found")
                return copy.deepcopy(pol), {}
            if method == "POST":
                pid = (params or {}).get("policyId")
                pol_url = "%s/%s" % (url, pid)
                stored = copy.deepcopy(json_body or {})
                stored["etag"] = self.next_etag()
                self.deny_policies[pol_url] = stored
                return {"name": "op-create", "done": True, "response": stored}, {}
            if method == "PUT":
                cur = self.deny_policies.get(url)
                if cur is None:
                    raise GcpError("not_found", status=404, message="not found")
                if (json_body or {}).get("etag") != cur.get("etag"):
                    raise GcpError("precondition", status=412, message="etag mismatch")
                stored = copy.deepcopy(json_body or {})
                stored["etag"] = self.next_etag()
                self.deny_policies[url] = stored
                return {"name": "op-update", "done": True, "response": stored}, {}
            if method == "DELETE":
                cur = self.deny_policies.get(url)
                if cur is None:
                    raise GcpError("not_found", status=404, message="not found")
                if (params or {}).get("etag") and params["etag"] != cur.get("etag"):
                    raise GcpError("precondition", status=412, message="etag mismatch")
                del self.deny_policies[url]
                return {"name": "op-delete", "done": True}, {}

        # Default Cloud Billing Catalog / Budget / CRM endpoints
        if "cloudbilling.googleapis.com/v1/services" in url and "/skus" not in url:
            return {"services": [{"name": "services/C7E2-9256-1C43", "serviceId": "C7E2-9256-1C43",
                                  "displayName": "Agent Platform"}]}, {}
        if "cloudbilling.googleapis.com/v1/billingAccounts/" in url:
            return {"currencyCode": "USD"}, {}
        if "cloudresourcemanager.googleapis.com/v3/projects/" in url:
            return {"name": "projects/123456789012"}, {}
        if "billingbudgets.googleapis.com/v1/billingAccounts/" in url:
            if method == "GET" and url.endswith("/budgets"):
                return {"budgets": copy.deepcopy(self.budgets)}, {}
            if method == "POST" and url.endswith("/budgets"):
                acct = url.split("/billingAccounts/", 1)[1].split("/budgets", 1)[0]
                b = copy.deepcopy(json_body or {})
                b["name"] = "billingAccounts/%s/budgets/b-%d" % (acct, len(self.budgets) + 1)
                b["etag"] = self.next_etag()
                b.setdefault("spendCap", {})["outputState"] = b.get("spendCap", {}).get("inputState", "CONFIGURED")
                self.budgets.append(b)
                return copy.deepcopy(b), {}
            if method == "PATCH":
                bname = url.split("/v1/", 1)[1]
                for b in self.budgets:
                    if b.get("name") == bname:
                        if "amount" in (json_body or {}):
                            b["amount"] = copy.deepcopy(json_body["amount"])
                        if "spendCap" in (json_body or {}):
                            b.setdefault("spendCap", {}).update(json_body["spendCap"])
                            b["spendCap"]["outputState"] = b["spendCap"].get("inputState")
                        return copy.deepcopy(b), {}
                raise GcpError("not_found", status=404, message="budget not found")

        raise GcpError("not_found", status=404, message="unmocked url: %s" % url)

    def paginate(self, url: str, *, quota_project: str = "", items_key: str, params=None,
                 max_pages: int = 10, page_size=None, method: str = "GET", json_body=None, token_param: str = "pageToken"):
        payload, _ = self.request(method, url, quota_project=quota_project, params=params, json_body=json_body)
        return list((payload or {}).get(items_key) or []), False

    def query(self, sql: str, params=None, *, quota_project: str = "", module: str = "test",
              max_bytes: int = 10 ** 9, max_rows=None, timeout: int = 30) -> QueryResult:
        self.query_calls.append((sql, copy.deepcopy(params), module))
        for pred, handler in self.query_handlers:
            if pred(sql, params, module):
                rows = handler(sql, params, module) if callable(handler) else handler
                return QueryResult(copy.deepcopy(rows), [], 0, len(rows))
        raise GcpError("not_found", status=404, message="no table")

    def run_parallel(self, tasks: dict, pool=None) -> dict:
        out = {}
        for name, fn in tasks.items():
            try:
                out[name] = (True, fn())
            except Exception as err:  # pylint: disable=broad-except
                out[name] = (False, err)
        return out


class FakeOidcVerifier:
    def __init__(self, tokens: dict[str, str] | None = None):
        self.tokens = dict(tokens or {})
        self.calls: list[tuple[str, list[str]]] = []

    def verify(self, token: str, audiences: list[str]) -> str:
        self.calls.append((token, list(audiences)))
        if token not in self.tokens:
            raise ValueError("invalid fake token")
        return self.tokens[token]


def make_test_config(**overrides) -> dict:
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["workspace"]["project_id"] = "test-ai-project"
    cfg["workspace"]["scan_all_projects"] = False
    cfg["storage"]["backend"] = "memory"
    cfg["auth"]["public_base_url"] = "https://caps-api.example.com"
    cfg["caps"]["usage_source"] = "logging"
    cfg["pricing"]["source"] = "manual"
    cfg["pricing"]["model_overrides"] = [
        {"model": "gemini-2.5-pro", "input": 1.25, "output": 10.0, "cached": 0.3125},
        {"model": "claude-sonnet-4.5", "input": 3.0, "output": 15.0, "cached": 0.3},
    ]
    return config.finalize(config.deep_merge(cfg, overrides))


def make_inference_entry(email: str, *, project: str = "test-ai-project", model: str = "gemini-2.5-pro",
                         prompt: int = 100_000, cand: int = 20_000, cached: int = 0, total: int | None = None,
                         ts: dt.datetime | None = None, insert_id: str | None = None) -> dict:
    when = (ts or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tot = total if total is not None else (prompt + cand)
    entry = {
        "logName": "projects/%s/logs/businessaicode.googleapis.com%%2Finference_response" % project,
        "timestamp": when,
        "labels": {"user_id": "user:" + email, "model_id": model},
        "jsonPayload": {
            "metadata": {
                "promptTokenCount": prompt,
                "candidatesTokenCount": cand,
                "cachedContentTokenCount": cached,
                "totalTokenCount": tot,
            }
        },
    }
    if insert_id:
        entry["insertId"] = insert_id
    return entry


class LiveTestServer:
    """Starts an App on an ephemeral loopback port for real HTTP testing."""

    def __init__(self, app: App):
        self.app = app
        self.httpd = Server(("127.0.0.1", 0), app.make_handler())
        self.port = self.httpd.server_address[1]
        self.base_url = "http://127.0.0.1:%d" % self.port
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *args):
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, method: str, path: str, *, body=None, token: str | None = None, headers: dict | None = None):
        hdrs = dict(headers or {})
        if token:
            hdrs["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.base_url + path, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                ctype = resp.headers.get("Content-Type") or ""
                parsed = json.loads(raw) if "json" in ctype else raw
                return resp.status, parsed, dict(resp.headers.items())
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8")
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = raw
            return err.code, parsed, dict(err.headers.items()) if err.headers else {}


def build_test_app(*, cfg=None, gcp=None, verifier=None, bootstrap_key: str = "gcpai_boot_secret_1234567890",
                   webhook_sender=None) -> tuple[App, FakeGcp]:
    fake_gcp = gcp or FakeGcp()
    store = DocStore(MemoryBackend())
    env = {"BOOTSTRAP_ADMIN_KEY": bootstrap_key} if bootstrap_key else {}
    app = App(
        environ=env,
        cfg=cfg or make_test_config(),
        gcp=fake_gcp,
        store=store,
        verifier=verifier or FakeOidcVerifier(),
        webhook_sender=webhook_sender,
    )
    return app, fake_gcp
