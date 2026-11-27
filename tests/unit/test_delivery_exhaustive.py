"""Exhaustive customer-delivery verification suite.

Covers:
  1. Complete 96/96 OpenAPI <-> Router parity and full unauthenticated / wrong-scope / exact-scope RBAC matrix
  2. Strict HTTP / JSON / pagination / query parameter fuzzing (zero 500s)
  3. SSRF URL validation (IPv4/IPv6/IPv4-mapped/CGNAT/trailing-dot/userinfo/port) and HTTP 302 redirect blocking
  4. Catastrophe prevention: empty bulk target lists (`scopes: []`, `apps: []`, `projects: []`) never expand to `*`
  5. Regressions for every delivery audit bug (caps, quotas, idempotency conflict, model_access, surface_models,
     model_registry, chargeback ?project=, user_model_rules, auth expired status, RateLimiter bound, cli, spend_caps)
  6. Multi-threaded CAS concurrency (MemoryBackend, FileBackend, simulated GCS 412/429) & 409/503 HTTP mapping
  7. Workforce Identity Federation principal full lifecycle
  8. Multi-currency (KRW/EUR), timezone (Asia/Seoul), and settlement period/window invariants
  9. Adversarial `/v1/internal/usage-event` Pub/Sub push payloads
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import http.server
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout

from tests.unit.governance_fakes import GovernanceFakeGcp
from tests.unit.support import (FakeOidcVerifier, LiveTestServer, make_inference_entry, make_test_config,
                                make_vertex_entry)
from usage_caps_api import auth, cli, config, httpkit, identity, timeutil
from usage_caps_api.app import ROUTER, App
from usage_caps_api.gcp import GcpError
from usage_caps_api.services import chargeback, model_registry, spend_caps, usage_events, user_model_rules, webhooks
from usage_caps_api.store import DocStore, FileBackend, GcsBackend, MemoryBackend, StoreConflict, StoreError

ADMIN = "boot-delivery-secret-999"


def _sample_path(pattern: str) -> str:
    repl = {
        "email": "alice@company.com",
        "group_id": "eng-team",
        "project_id": "test-ai-project",
        "key_id": "0123456789ab",
        "webhook_id": "wh_0123456789ab",
        "model_id": "gemini-test-pro",
        "scope_type": "projects",
        "scope_id": "test-ai-project",
        "app_id": "test-ai-project~global~corp-assistant",
        "limit_id": "rl_0123456789abcdef",
        "request_id": "qr_0123456789abcdef",
        "operation_id": "op_0123456789abcdef",
    }
    out = pattern
    for k, v in repl.items():
        out = out.replace("{" + k + "}", v)
    return out


def _make_app(gcp=None, cfg=None, store=None, webhook_sender=None, verifier=None):
    usage_events.reset()
    gcp = gcp if gcp is not None else GovernanceFakeGcp()
    cfg = cfg if cfg is not None else make_test_config(
        workspace={"projects": ["team-b-project"]},
        auth={"oidc_allowed_service_accounts": ["scheduler@test.iam.gserviceaccount.com"],
              "oidc_internal_service_accounts": ["pubsub@test.iam.gserviceaccount.com"]},
        hub={"runtime_service_account": "runtime-sa@test-ai-project.iam.gserviceaccount.com",
             "enforcer_service_account": "enforcer-sa@test-ai-project.iam.gserviceaccount.com",
             "service_account_subjects": True},
        quotas={"contact_email": "cloud-admin@company.com"},
    )
    store = store if store is not None else DocStore(MemoryBackend())
    return App(
        environ={"BOOTSTRAP_ADMIN_KEY": ADMIN},
        cfg=cfg,
        gcp=gcp,
        store=store,
        verifier=verifier or FakeOidcVerifier(),
        webhook_sender=webhook_sender or (lambda url, raw, hdrs: 200),
    )


class DeliveryRbacAndParityTests(unittest.TestCase):
    def test_openapi_matches_router_96_operations_and_rbac_matrix(self):
        import yaml  # type: ignore

        app = _make_app()
        spec = yaml.safe_load(app.openapi_yaml())
        openapi_ops = set()
        for path, methods in (spec.get("paths") or {}).items():
            for m in methods:
                if m.lower() in ("get", "post", "put", "patch", "delete"):
                    openapi_ops.add((m.upper(), path))
        router_ops = set(ROUTER.routes()) | {("GET", "/v1/openapi.yaml")}
        self.assertEqual(router_ops, openapi_ops, "Router and openapi.yaml operation drift detected!")
        self.assertEqual(len(router_ops), 96)

        # Create one scoped key per non-admin scope
        scope_keys = {}
        for sc in auth.ALL_SCOPES:
            if sc != "admin":
                scope_keys[sc] = auth.create_api_key(app.store, name="k-" + sc, scopes=[sc])["key"]

        with LiveTestServer(app) as srv:
            # Public endpoints
            st, body, _ = srv.request("GET", "/v1/health")
            self.assertEqual(st, 200)
            self.assertNotIn("storage", body)
            st, body, _ = srv.request("GET", "/v1/health", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertIn("storage", body)
            st, text, _ = srv.request("GET", "/v1/openapi.yaml")
            self.assertEqual(st, 200)
            self.assertIn("openapi: 3.1", str(text))

            # Iterate every single route in ROUTER
            all_routes = list(ROUTER._exact.items()) + [((rt.method, rt.pattern), (rt.fn, rt.opts))
                                                        for rt in ROUTER._dynamic]
            self.assertEqual(len(all_routes), 95)
            for (method, pattern), (_fn, opts) in all_routes:
                path = _sample_path(pattern)
                if opts.get("public"):
                    continue
                # 1) Unauthenticated -> MUST be 401
                st, err, _ = srv.request(method, path, body={} if method in ("POST", "PUT", "PATCH") else None)
                self.assertEqual(st, 401, "Expected 401 unauthenticated for %s %s, got %s %s" % (method, pattern, st, err))

                # 2) Disjoint-scope key -> MUST be 403 insufficient_scope
                req_scope = opts.get("scope")
                req_set = {req_scope} if isinstance(req_scope, str) else set(req_scope or ())
                wrong_scope = next(s for s in auth.ALL_SCOPES if s != "admin" and s not in req_set)
                st, err, _ = srv.request(method, path, token=scope_keys[wrong_scope],
                                         body={} if method in ("POST", "PUT", "PATCH") else None)
                self.assertEqual(st, 403, "Expected 403 wrong scope (%s) for %s %s, got %s %s"
                                 % (wrong_scope, method, pattern, st, err))
                self.assertEqual(err["error"]["code"], "insufficient_scope")

                # 3) Exact required scope (or admin for admin/internal-only) -> passes auth & scope gate (not 401 / insufficient_scope)
                valid_scope = next((s for s in req_set if s in scope_keys), None)
                valid_tok = scope_keys[valid_scope] if valid_scope else ADMIN
                st, resp, _ = srv.request(method, path, token=valid_tok,
                                          body={} if method in ("POST", "PUT", "PATCH") else None)
                self.assertNotEqual(st, 401, "Valid scope rejected 401 on %s %s" % (method, pattern))
                if st == 403 and isinstance(resp, dict):
                    self.assertNotEqual((resp.get("error") or {}).get("code"), "insufficient_scope",
                                        "Valid scope hit insufficient_scope on %s %s" % (method, pattern))
                self.assertNotEqual(st, 500, "500 Internal Server Error on %s %s: %s" % (method, pattern, resp))


def _raw_request(srv: LiveTestServer, method: str, path: str, *, raw_body: bytes, headers: dict):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(srv.base_url + path, data=raw_body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8"), dict(resp.headers.items())
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8"), dict(err.headers.items()) if err.headers else {}


class DeliveryFuzzAndBoundaryTests(unittest.TestCase):
    def test_strict_json_cursor_limit_and_project_fuzzing(self):
        app = _make_app()
        with LiveTestServer(app) as srv:
            # Non-standard JSON constants (NaN, Infinity) and malformed/non-object JSON
            for raw in (b"{bad", b"[]", b'"str"', b"123", b'{"cost_cap": NaN}', b'{"cost_cap": Infinity}',
                        b'{"cost_cap": -Infinity}'):
                st, err, _ = _raw_request(srv, "PUT", "/v1/caps/policy", raw_body=raw,
                                          headers={"Authorization": "Bearer " + ADMIN,
                                                   "Content-Type": "application/json"})
                self.assertEqual(st, 400, "Expected 400 for %r, got %s %s" % (raw, st, err))

            # Wrong Content-Type
            st, _, _ = _raw_request(srv, "PUT", "/v1/caps/policy", raw_body=b'{"cost_cap": 10}',
                                    headers={"Authorization": "Bearer " + ADMIN, "Content-Type": "text/plain"})
            self.assertEqual(st, 400)

            # Invalid cursors (bool, float, negative, huge, non-dict, corrupted base64)
            bad_cursors = [
                "not-valid-base64!!!",
                base64.urlsafe_b64encode(b'[]').decode("ascii").rstrip("="),
                base64.urlsafe_b64encode(b'{"o":true}').decode("ascii").rstrip("="),
                base64.urlsafe_b64encode(b'{"o":false}').decode("ascii").rstrip("="),
                base64.urlsafe_b64encode(b'{"o":1.9}').decode("ascii").rstrip("="),
                base64.urlsafe_b64encode(b'{"o":-1}').decode("ascii").rstrip("="),
                base64.urlsafe_b64encode(b'{"o":999999999999}').decode("ascii").rstrip("="),
                base64.urlsafe_b64encode(b'{"o":"5"}').decode("ascii").rstrip("="),
            ]
            for c in bad_cursors:
                st, err, _ = srv.request("GET", "/v1/groups?cursor=" + urllib.parse.quote(c), token=ADMIN)
                self.assertEqual(st, 400, "Cursor %r was accepted: %s" % (c, err))

            # Invalid limits
            for lim in ("0", "-5", "999999", "abc", "1.5"):
                st, _, _ = srv.request("GET", "/v1/groups?limit=" + lim, token=ADMIN)
                self.assertEqual(st, 400)
            with self.assertRaises(httpkit.ApiError):
                httpkit.parse_limit(True)

            # Invalid ?project= must return 400 (never silently fallback to "all")
            for ep in ("/v1/usage/users?project=BAD_PROJ!!!",
                       "/v1/usage/models?project=BAD_PROJ!!!",
                       "/v1/billing/users?project=BAD_PROJ!!!",
                       "/v1/models?include=access&project=BAD_PROJ!!!"):
                st, err, _ = srv.request("GET", ep, token=ADMIN)
                self.assertEqual(st, 400, "Expected 400 for %s, got %s %s" % (ep, st, err))

            # Unmanaged valid project on /v1/models?include=access -> 403
            st, err, _ = srv.request("GET", "/v1/models?include=access&project=unmanaged-victim-proj", token=ADMIN)
            self.assertEqual(st, 403, err)

            # Method Not Allowed -> 405 with Allow header
            for method, path, want_allow in (("POST", "/v1/openapi.yaml", "GET"),
                                             ("DELETE", "/v1/health", "GET"),
                                             ("PUT", "/v1/groups", "GET")):
                st, _, hdrs = srv.request(method, path, token=ADMIN, body={})
                self.assertEqual(st, 405)
                self.assertIn(want_allow, hdrs.get("Allow", ""))


class DeliverySsrfAndSecurityTests(unittest.TestCase):
    def test_valid_https_url_blocks_all_ssrf_vectors(self):
        blocked = [
            "http://example.com/hook",
            "ftp://example.com/hook",
            "https://169.254.169.254/computeMetadata/v1/",
            "https://127.0.0.1/hook",
            "https://[::1]/hook",
            "https://[::ffff:169.254.169.254]/hook",
            "https://[::ffff:127.0.0.1]/hook",
            "https://[::ffff:10.0.0.1]/hook",
            "https://10.0.0.1/hook",
            "https://172.16.0.1/hook",
            "https://192.168.1.1/hook",
            "https://100.64.0.1/hook",
            "https://0.0.0.0/hook",
            "https://metadata.google.internal/computeMetadata/v1/",
            "https://metadata.google.internal./computeMetadata/v1/",
            "https://localhost/hook",
            "https://localhost./hook",
            "https://svc.cluster.local./hook",
            "https://internal.corp.internal./hook",
            "https://user:pass@hooks.example.com/hook",
            "https://hooks.example.com:99999/hook",
            "https://hooks.example.com/\r\nX-Injected: 1",
            "https://singlelabel/hook",
            12345,
        ]
        for url in blocked:
            self.assertFalse(config.valid_https_url(url, check_dns=False), "SSRF URL NOT blocked: %r" % (url,))
        self.assertTrue(config.valid_https_url("https://hooks.example.com/v1/events", check_dns=False))

    def test_webhook_sender_blocks_http_redirects(self):
        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/computeMetadata/v1/")
                self.end_headers()

            def log_message(self, fmt, *args):
                return

        srv = http.server.HTTPServer(("127.0.0.1", 0), RedirectHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            url = "http://127.0.0.1:%d/hook" % srv.server_address[1]
            req = __import__("urllib.request").request.Request(url, data=b"{}", method="POST")
            with self.assertRaises(ValueError) as ctx_err:
                webhooks._NO_REDIRECT_OPENER.open(req, timeout=3)
            self.assertIn("redirect blocked", str(ctx_err.exception))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_webhook_non_string_fields_rejected(self):
        app = _make_app()
        with LiveTestServer(app) as srv:
            st, _, _ = srv.request("POST", "/v1/admin/webhooks", token=ADMIN, body={"url": 12345})
            self.assertEqual(st, 400)
            st, _, _ = srv.request("POST", "/v1/admin/webhooks", token=ADMIN,
                                   body={"url": "https://hooks.example.com/ok", "name": ["bad"]})
            self.assertEqual(st, 400)
            st, _, _ = srv.request("POST", "/v1/admin/webhooks", token=ADMIN,
                                   body={"url": "https://metadata.google.internal./v1"})
            self.assertEqual(st, 400)


class DeliveryBugRegressionsTests(unittest.TestCase):
    def test_empty_bulk_lists_never_expand_to_wildcard(self):
        gcp = GovernanceFakeGcp()
        gcp.add_ge_app("test-ai-project", "corp-assistant", {"gemini-test-pro": "MODEL_ENABLED"})
        app = _make_app(gcp=gcp)
        with LiveTestServer(app) as srv:
            # 1) model-access bulk-apply scopes: []
            st, _, _ = srv.request("POST", "/v1/model-access/bulk-apply", token=ADMIN,
                                   body={"scopes": [], "mode": "deny_all", "force": True})
            self.assertEqual(st, 400)
            self.assertEqual(gcp.org_policies, {})

            # 2) gemini-enterprise bulk-models apps: []
            st, _, _ = srv.request("POST", "/v1/gemini-enterprise/apps/bulk-models", token=ADMIN,
                                   body={"apps": [], "models": {"gemini-test-pro": "off"}})
            self.assertEqual(st, 400)
            self.assertEqual(gcp.engines["test-ai-project"][0]["modelConfigs"]["gemini-test-pro"], "MODEL_ENABLED")

            # 3) vertex-logging bulk enable projects: []
            st, _, _ = srv.request("POST", "/v1/vertex-logging/enable", token=ADMIN,
                                   body={"projects": [], "dryRun": False})
            self.assertEqual(st, 400)

    def test_caps_webhook_thresholds_unhashable_and_bool(self):
        app = _make_app()
        with LiveTestServer(app) as srv:
            for bad in ([{"a": 1}], [[80]], [1, True], [False], [0], [101], ["80"]):
                st, err, _ = srv.request("PUT", "/v1/caps/policy", token=ADMIN, body={"webhook_thresholds": bad})
                self.assertEqual(st, 400, "Expected 400 for webhook_thresholds=%r, got %s %s" % (bad, st, err))

    def test_quotas_validation_and_idempotency_conflict(self):
        gcp = GovernanceFakeGcp()
        gcp.add_quota("test-ai-project", "GenerateContentRequestsPerMinute",
                      "aiplatform.googleapis.com/generate_content_requests",
                      [({"region": "us-central1", "base_model": "gemini-test-pro"}, 1000)])
        app = _make_app(gcp=gcp)
        with LiveTestServer(app) as srv:
            st, rows, _ = srv.request("GET", "/v1/projects/test-ai-project/rate-limits", token=ADMIN)
            self.assertEqual(st, 200)
            lid = rows["data"][0]["id"]

            # Non-string limit_id -> 400 (was 500 TypeError)
            st, _, _ = srv.request("POST", "/v1/quota-requests", token=ADMIN,
                                   body={"project_id": "test-ai-project", "limit_id": 12345, "requested_value": 2000})
            self.assertEqual(st, 400)

            # Non-dict dimensions -> 400
            st, _, _ = srv.request("POST", "/v1/quota-requests", token=ADMIN,
                                   body={"project_id": "test-ai-project", "quota_id": "GenerateContentRequestsPerMinute",
                                         "dimensions": "us-central1", "requested_value": 2000})
            self.assertEqual(st, 400)

            # String "false" for force -> 400
            st, _, _ = srv.request("PATCH", "/v1/projects/test-ai-project/rate-limits/" + lid, token=ADMIN,
                                   body={"value": 500, "force": "false"})
            self.assertEqual(st, 400)

            # Idempotency-Key same params -> 200 replay; DIFFERENT params -> 409 conflict
            body = {"project_id": "test-ai-project", "limit_id": lid, "requested_value": 2000,
                    "justification": "Scaling up production workload for Q4 launch"}
            st, req, _ = srv.request("POST", "/v1/quota-requests", token=ADMIN, body=body,
                                     headers={"Idempotency-Key": "idem-key-1"})
            self.assertEqual(st, 201, req)
            st, replay, _ = srv.request("POST", "/v1/quota-requests", token=ADMIN, body=body,
                                        headers={"Idempotency-Key": "idem-key-1"})
            self.assertEqual(st, 200)
            self.assertTrue(replay.get("idempotent_replay"))
            diff_body = dict(body, requested_value=3000)
            st, conflict, _ = srv.request("POST", "/v1/quota-requests", token=ADMIN, body=diff_body,
                                          headers={"Idempotency-Key": "idem-key-1"})
            self.assertEqual(st, 409, conflict)

    def test_model_access_if_match_and_surface_models_strict_types(self):
        gcp = GovernanceFakeGcp()
        gcp.add_ge_app("test-ai-project", "corp-assistant", {"gemini-test-pro": "MODEL_ENABLED"})
        app = _make_app(gcp=gcp)
        with LiveTestServer(app) as srv:
            # If-Match when no policy exists yet -> 412
            st, _, _ = srv.request("PUT", "/v1/model-access/projects/test-ai-project", token=ADMIN,
                                   headers={"If-Match": '"nonexistent-etag"'},
                                   body={"mode": "deny_list", "models": ["claude-test-sonnet"]})
            self.assertEqual(st, 412)
            # Non-list models / non-bool force -> 400
            st, _, _ = srv.request("PUT", "/v1/model-access/projects/test-ai-project", token=ADMIN,
                                   body={"mode": "allow_list", "models": False})
            self.assertEqual(st, 400)
            st, _, _ = srv.request("PUT", "/v1/model-access/projects/test-ai-project", token=ADMIN,
                                   body={"mode": "deny_all", "force": "false"})
            self.assertEqual(st, 400)

            # GE app view exposes path_id and validates dryRun bool strictly
            st, apps, _ = srv.request("GET", "/v1/gemini-enterprise/apps", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertEqual(apps["data"][0]["path_id"], "test-ai-project~global~corp-assistant")
            st, _, _ = srv.request("PATCH", "/v1/gemini-enterprise/apps/%s/models" % apps["data"][0]["path_id"],
                                   token=ADMIN, body={"models": {"gemini-test-pro": "off"}, "dryRun": "false"})
            self.assertEqual(st, 400)

            # Antigravity PUT with non-string project -> 400 (was 500 AttributeError)
            st, _, _ = srv.request("PUT", "/v1/antigravity/models", token=ADMIN, body={"project": 123})
            self.assertEqual(st, 400)
            st, err, _ = srv.request("PUT", "/v1/antigravity/models", token=ADMIN, body={"project": "test-ai-project"})
            self.assertEqual(st, 409)
            self.assertEqual(err["error"]["code"], "console_required")

    def test_chargeback_project_filter_and_model_traffic_reconciliation(self):
        gcp = GovernanceFakeGcp()
        now = dt.datetime.now(dt.timezone.utc)
        # Same user, same model, across TWO projects AND TWO surfaces (Antigravity + Gemini Platform)
        gcp.log_entries.extend([
            make_inference_entry("alice@company.com", project="test-ai-project", model="gemini-test-pro",
                                 prompt=1_000_000, cand=100_000, ts=now),
            make_inference_entry("alice@company.com", project="team-b-project", model="gemini-test-pro",
                                 prompt=2_000_000, cand=200_000, ts=now),
            make_vertex_entry(user="alice@company.com", project="test-ai-project", model="gemini-test-pro",
                              prompt=500_000, cand=50_000, ts=now),
        ])
        app = _make_app(gcp=gcp)
        with LiveTestServer(app) as srv:
            # 1) Chargeback ?project=team-b-project returns ONLY team-b-project facts
            st, rep, _ = srv.request("GET", "/v1/billing/users?project=team-b-project", token=ADMIN)
            self.assertEqual(st, 200, rep)
            self.assertEqual([p["project"] for p in rep["by_project"]], ["team-b-project"])
            self.assertEqual(rep["totals"]["total_tokens"], 2_200_000)
            self.assertEqual(rep["data"][0]["total_tokens"], 2_200_000)

            # 2) GET /v1/usage/models?group_by=traffic reconciles Antigravity + Gemini Platform into on_demand
            st, mrep, _ = srv.request("GET", "/v1/usage/models?group_by=traffic", token=ADMIN)
            self.assertEqual(st, 200, mrep)
            row = next(r for r in mrep["data"] if r["model"] == "gemini-test-pro")
            sum_group_tokens = sum(g["total_tokens"] for g in row["groups"])
            self.assertEqual(sum_group_tokens, row["total_tokens"],
                             "group_by=traffic dropped unbucketed Antigravity tokens!")

            # 3) GET /v1/usage/users/{email}?fresh=true propagates fresh=True to current_cap
            st, u1, _ = srv.request("GET", "/v1/usage/users/alice@company.com", token=ADMIN)
            self.assertEqual(st, 200)
            t1 = u1["current_cap"]["usage"]["total_tokens"]
            gcp.log_entries.append(make_inference_entry("alice@company.com", project="test-ai-project",
                                                        model="gemini-test-pro", prompt=300_000, cand=100_000, ts=now))
            st, u2, _ = srv.request("GET", "/v1/usage/users/alice@company.com?fresh=true", token=ADMIN)
            self.assertEqual(st, 200)
            self.assertEqual(u2["current_cap"]["usage"]["total_tokens"], t1 + 400_000)

            # 4) Monthly-only model limit violation attributes exact project & surface from earlier in month
            yesterday = now - dt.timedelta(days=2)
            if yesterday.month == now.month:
                gcp.log_entries.append(make_vertex_entry(user="bob@company.com", project="team-b-project",
                                                         model="claude-test-sonnet", prompt=100_000, cand=10_000,
                                                         ts=yesterday))
                st, _, _ = srv.request("PUT", "/v1/rate-limits/users/bob@company.com", token=ADMIN,
                                       body={"limits": [{"model": "claude-test-sonnet", "monthly_tokens": 50_000}],
                                             "on_violation": "block_service"})
                self.assertEqual(st, 200)
                st, ev, _ = srv.request("POST", "/v1/model-rules/evaluate", token=ADMIN, body={"dryRun": True})
                self.assertEqual(st, 200, ev)
                bob_vs = [v for v in ev["violations"] if v["email"] == "bob@company.com"]
                self.assertTrue(bob_vs)
                self.assertEqual(bob_vs[0]["projects"], ["team-b-project"])
                self.assertIn("gemini_platform", bob_vs[0]["block_services"])

    def test_auth_expired_key_status_rate_limiter_bound_and_cli_errors(self):
        app = _make_app()
        with LiveTestServer(app) as srv:
            # Non-string / control-char key names rejected
            for bad in (123, {"a": 1}, "bad\nname", "   "):
                st, _, _ = srv.request("POST", "/v1/admin/api-keys", token=ADMIN,
                                       body={"name": bad, "scopes": ["usage:read"]})
                self.assertEqual(st, 400)

            # Expired key shows status="expired" in list and fails auth with 401
            past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
            st, created, _ = srv.request("POST", "/v1/admin/api-keys", token=ADMIN,
                                         body={"name": "expired-test", "scopes": ["usage:read"], "expires_at": past})
            self.assertEqual(st, 201)
            self.assertEqual(created["status"], "expired")
            st, listed, _ = srv.request("GET", "/v1/admin/api-keys", token=ADMIN)
            self.assertEqual(st, 200)
            rec = next(k for k in listed["data"] if k["id"] == created["id"])
            self.assertEqual(rec["status"], "expired")
            st, _, _ = srv.request("GET", "/v1/usage/users", token=created["key"])
            self.assertEqual(st, 401)

        # RateLimiter memory bound under denied spam
        rl = auth.RateLimiter({"write": 1})
        rl.MAX_BUCKETS = 20
        for i in range(60):
            rl.allow("spam-%d" % i, "write")
            rl.allow("spam-%d" % i, "write")  # denied call
        self.assertLessEqual(len(rl._buckets), 25)

        # CLI clean ApiError exit code 1 with JSON stderr (no traceback)
        err_buf = io.StringIO()
        out_buf = io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = cli.main(["keys", "create", "--name", "bad", "--scopes", "nonexistent:scope"], app=app)
        self.assertEqual(rc, 1)
        parsed = json.loads(err_buf.getvalue())
        self.assertEqual(parsed["error"]["code"], "bad_request")

    def test_protected_control_plane_sa_and_spend_caps_gemini_platform_name(self):
        app = _make_app()
        # Control plane runtime & enforcer SAs are rejected as subjects by identity.person_key AND protected in rules
        self.assertIsNone(identity.person_key("runtime-sa@test-ai-project.iam.gserviceaccount.com"))
        self.assertIsNone(identity.person_key("enforcer-sa@test-ai-project.iam.gserviceaccount.com"))
        # Normal SA IS allowed when service_account_subjects=True
        self.assertEqual(identity.person_key("tester@test-ai-project.iam.gserviceaccount.com"),
                         "tester@test-ai-project.iam.gserviceaccount.com")
        from usage_caps_api.services.base import Ctx
        ctx = Ctx(app, app.config, auth.Caller("admin", "Admin", "bootstrap", frozenset(auth.ALL_SCOPES), "boot"))
        self.assertTrue(user_model_rules._protected(ctx, "runtime-sa@test-ai-project.iam.gserviceaccount.com"))
        self.assertTrue(user_model_rules._protected(ctx, "enforcer-sa@test-ai-project.iam.gserviceaccount.com"))

        # spend_caps matches new "Gemini Platform" display name too
        gcp = GovernanceFakeGcp()
        gcp.rest_handlers.append((
            lambda m, u, p, b: "cloudbilling.googleapis.com/v1/services" in u and "/skus" not in u,
            {"services": [{"name": "services/AAAA-BBBB-CCCC", "displayName": "Gemini Platform"}]},
        ))
        app2 = _make_app(gcp=gcp)
        ctx2 = Ctx(app2, app2.config, auth.Caller("admin", "Admin", "bootstrap", frozenset(auth.ALL_SCOPES), "boot"))
        self.assertEqual(spend_caps.service_name(ctx2), "services/AAAA-BBBB-CCCC")


class DeliveryConcurrencyAndStoreErrorTests(unittest.TestCase):
    def test_parallel_cas_mutations_file_and_memory_zero_lost_updates(self):
        for label, backend_fn in (("memory", lambda d: MemoryBackend()), ("file", lambda d: FileBackend(d))):
            with tempfile.TemporaryDirectory() as tmp:
                store = DocStore(backend_fn(tmp))
                errors = []

                def worker(idx):
                    try:
                        def inc(doc):
                            items = list(doc.get("items") or [])
                            items.append(idx)
                            doc["items"] = items
                        store.mutate("concurrent_test", inc, attempts=25)
                    except Exception as exc:  # pylint: disable=broad-except
                        errors.append(exc)

                threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                self.assertEqual(errors, [], "Errors on %s backend: %r" % (label, errors))
                doc, _ = store.load("concurrent_test", fresh=True)
                self.assertEqual(sorted(doc["items"]), list(range(16)), "Lost update on %s backend!" % label)

    def test_gcs_backend_retries_429_and_412_and_app_maps_store_errors(self):
        # 1) GcsBackend treats 429 and 412 as retryable StoreConflict
        calls = {"post": 0}

        class FakeGcsHttp:
            def request(self, method, url, **kwargs):
                if method == "GET":
                    raise GcpError("not_found", status=404)
                calls["post"] += 1
                if calls["post"] == 1:
                    raise GcpError("unavailable", status=429, message="rate limited")
                if calls["post"] == 2:
                    raise GcpError("precondition", status=412, message="stale gen")
                return {"generation": "42"}, {}

        gcs_store = DocStore(GcsBackend(FakeGcsHttp(), "bkt", "pfx", lambda: "proj"))
        gcs_store.mutate("policy", lambda d: d.update(ok=True), attempts=5)
        self.assertEqual(calls["post"], 3)

        # 2) Exhausted StoreConflict -> HTTP 409; StoreError -> HTTP 503 (never 500)
        class AlwaysConflictBackend(MemoryBackend):
            def save(self, name, doc, generation):
                raise StoreConflict()

        app_conflict = _make_app(store=DocStore(AlwaysConflictBackend()))
        with LiveTestServer(app_conflict) as srv:
            st, err, _ = srv.request("PUT", "/v1/caps/policy", token=ADMIN, body={"cost_cap": 10})
            self.assertEqual(st, 409, err)
            self.assertEqual(err["error"]["code"], "conflict")

        class BrokenLoadBackend(MemoryBackend):
            def load(self, name):
                raise StoreError("gcs unavailable")

        app_err = _make_app(store=DocStore(BrokenLoadBackend()))
        with LiveTestServer(app_err) as srv:
            st, err, _ = srv.request("GET", "/v1/groups", token=ADMIN)
            self.assertEqual(st, 503, err)
            self.assertEqual(err["error"]["code"], "unavailable")

    def test_group_concurrent_delete_does_not_resurrect_ghost(self):
        app = _make_app()
        with LiveTestServer(app) as srv:
            st, _, _ = srv.request("POST", "/v1/groups", token=ADMIN, body={"id": "ghost-test", "name": "Ghost"})
            self.assertEqual(st, 201)
            st, _, _ = srv.request("DELETE", "/v1/groups/ghost-test", token=ADMIN)
            self.assertEqual(st, 200)
            st, _, _ = srv.request("PATCH", "/v1/groups/ghost-test", token=ADMIN, body={"name": "Resurrected"})
            self.assertEqual(st, 404)
            st, lst, _ = srv.request("GET", "/v1/groups", token=ADMIN)
            self.assertEqual(lst["total"], 0)


class DeliveryWorkforceCurrencyAndEventsTests(unittest.TestCase):
    def test_workforce_principal_full_lifecycle(self):
        wf = "principal://iam.googleapis.com/locations/global/workforcePools/corp-idp/subject/emp-42"
        enc = urllib.parse.quote(wf, safe="")
        gcp = GovernanceFakeGcp()
        gcp.log_entries.append(make_inference_entry(wf, prompt=200_000, cand=50_000))
        app = _make_app(gcp=gcp)
        with LiveTestServer(app) as srv:
            # Cap rule
            st, res, _ = srv.request("PUT", "/v1/caps/users/" + enc, token=ADMIN,
                                     body={"type": "limit", "cost_cap": 50.0})
            self.assertEqual(st, 200, res)
            # Group membership
            st, grp, _ = srv.request("POST", "/v1/groups", token=ADMIN,
                                     body={"id": "wf-group", "members": [wf]})
            self.assertEqual(st, 201, grp)
            self.assertEqual(grp["member_count"], 1)
            # Block -> preserves exact camelCase workforcePools URI in IAM Deny
            st, blk, _ = srv.request("POST", "/v1/users/%s/block" % enc, token=ADMIN,
                                     body={"services": ["antigravity"]})
            self.assertEqual(st, 200, blk)
            pols = list(gcp.deny_policies.values())
            self.assertTrue(pols)
            denied_principals = pols[0]["rules"][0]["denyRule"]["deniedPrincipals"]
            self.assertIn(wf, denied_principals)
            # Unblock -> cleans IAM Deny
            st, ublk, _ = srv.request("POST", "/v1/users/%s/unblock" % enc, token=ADMIN, body={})
            self.assertEqual(st, 200, ublk)
            self.assertEqual(gcp.deny_policies, {})
            # Model rule
            st, mr, _ = srv.request("PUT", "/v1/users/%s/model-rules" % enc, token=ADMIN,
                                    body={"mode": "deny_list", "models": ["claude-test-sonnet"]})
            self.assertEqual(st, 200, mr)

    def test_multicurrency_krw_timezone_and_settlement_windows(self):
        gcp = GovernanceFakeGcp()
        now = dt.datetime.now(dt.timezone.utc)
        gcp.log_entries.extend([
            make_inference_entry("u1@company.com", project="test-ai-project", model="gemini-test-pro",
                                 prompt=1_000_000, cand=100_000, ts=now),
            make_inference_entry("u1@company.com", project="team-b-project", model="claude-test-sonnet",
                                 prompt=1_000_000, cand=100_000, ts=now),
        ])
        cfg = make_test_config(
            workspace={"projects": ["team-b-project"], "timezone": "Asia/Seoul"},
            caps={"currency": "KRW", "reset_timezone": "Asia/Seoul"},
            pricing={"source": "manual", "currency": "KRW",
                     "model_overrides": [
                         {"model": "gemini-test-pro", "input": 1750.0, "output": 14000.0},
                         {"model": "claude-test-sonnet", "input": 4200.0, "output": 21000.0},
                     ]},
        )
        app = _make_app(gcp=gcp, cfg=cfg)
        with LiveTestServer(app) as srv:
            st, rep, _ = srv.request("GET", "/v1/billing/users", token=ADMIN)
            self.assertEqual(st, 200, rep)
            self.assertEqual(rep["currency"], "KRW")
            # Exact cent-level rollup invariant across users, projects, and grand total
            u_sum = sum(chargeback.to_cents(u["chargeable"]) for u in rep["data"])
            p_sum = sum(chargeback.to_cents(p["chargeable"]) for p in rep["by_project"])
            g_tot = chargeback.to_cents(rep["totals"]["chargeable"])
            self.assertEqual(u_sum, g_tot)
            self.assertEqual(p_sum, g_tot)
            self.assertGreater(g_tot, 0)

        # All 5 period types x calendar/rolling in Asia/Seoul
        ref = dt.datetime(2026, 11, 27, 15, 30, tzinfo=dt.timezone.utc)  # 2026-11-28 00:30 KST
        for period in timeutil.PERIODS:
            for window in timeutil.WINDOWS:
                win = timeutil.period_window(period, window, now=ref, tz="Asia/Seoul", cycle_days=14,
                                             cycle_start="2026-11-01")
                self.assertLessEqual(win["start"], ref)
                self.assertEqual(win["timezone"], "Asia/Seoul")

    def test_adversarial_usage_event_payloads_never_500(self):
        app = _make_app()
        with LiveTestServer(app) as srv:
            adversarial = [
                {},
                {"message": "not-a-dict"},
                {"message": {"data": ""}},
                {"message": {"data": "!!!not-base64!!!"}},
                {"message": {"data": base64.b64encode(b"not json").decode("ascii")}},
                {"message": {"data": base64.b64encode(b"[1,2,3]").decode("ascii")}},
                {"message": {"data": base64.b64encode(json.dumps({"logName": 12345}).encode()).decode("ascii")}},
                {"message": {"data": base64.b64encode(json.dumps({"logName": "projects/unmanaged-p/logs/x"}).encode()).decode("ascii")}},
                {"message": {"data": base64.b64encode(json.dumps({
                    "logName": "projects/test-ai-project/logs/businessaicode.googleapis.com%2Finference_response",
                    "labels": {"user_id": "not-an-email"},
                }).encode()).decode("ascii")}},
                {"message": {"data": "A" * 70000}},
            ]
            for payload in adversarial:
                st, body, _ = srv.request("POST", "/v1/internal/usage-event", token=ADMIN, body=payload)
                self.assertEqual(st, 200, "Adversarial usage-event failed: %s %s" % (st, body))
                self.assertTrue(body["acknowledged"])
                self.assertEqual(body["action"], "ignored")

    def test_live_catalog_prices_high_fx_currencies_and_policy_fx_sync(self):
        from usage_caps_api.services import base as svc_base, pricing_sync

        rate_krw = 1343.255
        services_resp = {"services": [{"serviceId": "svc-ai", "displayName": "Gemini Platform"}]}

        def _sku(desc: str, usd_per_m: float, cur: str):
            mult = rate_krw if cur == "KRW" else 1.0
            per_m = usd_per_m * mult
            units = int(per_m)
            nanos = int(round((per_m - units) * 1e9))
            return {
                "name": "services/svc-ai/skus/" + desc.replace(" ", "_").lower(),
                "description": desc,
                "pricingInfo": [{
                    "effectiveTime": "2026-11-26T08:00:00Z",
                    "currencyConversionRate": mult,
                    "pricingExpression": {
                        "usageUnit": "count",
                        "usageUnitDescription": "1000000 tokens",
                        "baseUnitConversionFactor": 1_000_000,
                        "tieredRates": [{"startUsageAmount": 0, "unitPrice": {"units": str(units), "nanos": nanos}}],
                    },
                }],
            }

        class CatalogFakeGcp(GovernanceFakeGcp):
            def request(self, method, url, *, quota_project=None, params=None, json_body=None, data=None,
                        headers=None, timeout=30, expect_json=True):
                if url.endswith("/v1/services"):
                    return services_resp, {}
                if "/v1/services/svc-ai/skus" in url:
                    cur = (params or {}).get("currencyCode") or "USD"
                    return {"skus": [
                        _sku("Gemini Test Pro Input Token Count", 3.0, cur),
                        _sku("Gemini Test Pro Output Token Count", 15.0, cur),
                        _sku("Gemini Bogus Huge Input Token Count", 800.0, cur),
                    ]}, {}
                return super().request(method, url, quota_project=quota_project, params=params,
                                       json_body=json_body, data=data, headers=headers,
                                       timeout=timeout, expect_json=expect_json)

        gcp = CatalogFakeGcp()
        cfg = make_test_config(pricing={"source": "auto", "currency": "USD", "model_overrides": []})
        app = _make_app(gcp=gcp, cfg=cfg)
        app.store.mutate("model_catalog", lambda d: d.setdefault("models", {}).update({
            "gemini-test-pro": {}, "gemini-bogus-huge": {},
        }))
        caller = auth.Caller("admin", "admin", "api_key", frozenset(["*"]), "test")
        ctx = svc_base.Ctx(app, app.config, caller)

        # 1) Direct live_catalog_prices(KRW) must keep $3/$15 models (> 500 KRW/1M) while rejecting $800/1M bogus SKU.
        prices_krw, usd_rate, _eff = pricing_sync.live_catalog_prices(ctx, "KRW")
        self.assertAlmostEqual(usd_rate, rate_krw, places=3)
        self.assertIn("gemini-test-pro", prices_krw)
        self.assertAlmostEqual(prices_krw["gemini-test-pro"]["input"], 3.0 * rate_krw, delta=1.0)
        self.assertAlmostEqual(prices_krw["gemini-test-pro"]["output"], 15.0 * rate_krw, delta=1.0)
        self.assertNotIn("gemini-bogus-huge", prices_krw)

        # 2) Policy currency KRW + POST /v1/internal/pricing/sync persists KRW FX and serves KRW prices via /v1/pricing.
        with LiveTestServer(app) as srv:
            st, pol, _ = srv.request("PUT", "/v1/caps/policy", token=ADMIN, body={"currency": "KRW"})
            self.assertEqual(st, 200, pol)
            st, syn, _ = srv.request("POST", "/v1/internal/pricing/sync", token=ADMIN, body={})
            self.assertEqual(st, 200, syn)
            self.assertIn("KRW", syn["currencies_synced"])
            st, pr, _ = srv.request("GET", "/v1/pricing", token=ADMIN)
            self.assertEqual(st, 200, pr)
            self.assertEqual(pr["currency"], "KRW")
            row = next((r for r in pr["data"] if r["model_key"] == "gemini-test-pro"), None)
            self.assertIsNotNone(row, pr)
            self.assertAlmostEqual(row["input"], 3.0 * rate_krw, delta=1.0)
            self.assertAlmostEqual(row["output"], 15.0 * rate_krw, delta=1.0)

    def test_bulk_result_preserves_boolean_dry_run_and_planned_count(self):
        gcp = GovernanceFakeGcp()
        app = _make_app(gcp=gcp)
        with LiveTestServer(app) as srv:
            # 1) model-access bulk-apply with dryRun=True (when scopes change -> planned >= 1, dry_run is strict bool True)
            st, res, _ = srv.request("POST", "/v1/model-access/bulk-apply", token=ADMIN, body={
                "scopes": ["projects/*"], "mode": "deny_list", "models": ["claude-test-sonnet"], "dryRun": True,
            })
            self.assertEqual(st, 200, res)
            self.assertIs(res["dry_run"], True)
            self.assertEqual(res["planned"], 2)
            self.assertEqual(res["applied"], 0)

            # 2) model-access bulk-apply with dryRun=True when scopes are unchanged (planned == 0, dry_run STILL True!)
            st, res2, _ = srv.request("POST", "/v1/model-access/bulk-apply", token=ADMIN, body={
                "scopes": ["projects/*"], "mode": "inherit", "dryRun": True,
            })
            self.assertEqual(st, 200, res2)
            self.assertIs(res2["dry_run"], True)
            self.assertIsInstance(res2["planned"], int)

            # 3) GE apps bulk-models with dryRun=True
            st, ge_res, _ = srv.request("POST", "/v1/gemini-enterprise/apps/bulk-models", token=ADMIN, body={
                "apps": ["*"], "models": {"gemini-test-pro": "off"}, "dryRun": True,
            })
            self.assertEqual(st, 200, ge_res)
            self.assertIs(ge_res["dry_run"], True)
            self.assertIsInstance(ge_res["planned"], int)

            # 4) Bulk quota-requests with dryRun=True and bad dryRun type -> 400
            st, bad_q, _ = srv.request("POST", "/v1/quota-requests", token=ADMIN, body={
                "projects": ["*"], "quota_id": "q1", "requested_value": 100,
                "justification": "test", "contact_email": "ops@company.com", "dryRun": "yes",
            })
            self.assertEqual(st, 400, bad_q)
            st, q_res, _ = srv.request("POST", "/v1/quota-requests", token=ADMIN, body={
                "projects": ["*"],
                "quota_id": "GenerateContentRequestsPerMinutePerProjectPerRegionPerBaseModel",
                "model": "gemini-test-pro", "location": "us-central1",
                "requested_value": 100, "justification": "test", "contact_email": "ops@company.com",
                "dryRun": True,
            })
            self.assertEqual(st, 200, q_res)
            self.assertIs(q_res["dry_run"], True)
            self.assertIsInstance(q_res["planned"], int)


if __name__ == "__main__":
    unittest.main()
