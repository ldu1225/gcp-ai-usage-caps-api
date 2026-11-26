"""Stdlib-only Google Cloud REST client used by the preflight CLI before deployment.

`StaticTokenGcp` is a drop-in for the parts of `gcp.Gcp` the readiness engine uses (`request`, `paginate`,
`run_parallel`) when google-auth / requests are not installed (Cloud Shell, CI runners, bare workstations).

Access token source (first match):
  1. environment variable GCP_ACCESS_TOKEN (or CLOUDSDK_AUTH_ACCESS_TOKEN);
  2. `gcloud auth print-access-token` (re-run when the token is older than ~45 minutes or a call returns 401).

Errors are classified exactly like `gcp.Gcp` (GcpError kinds not_found / denied / conflict / precondition /
unavailable / invalid / http / timeout / network / credentials). The token is never logged.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .gcp import GcpError, _google_error, _kind_for_status, log_safe  # pylint: disable=protected-access

LOG = logging.getLogger("usage_caps_api.tokengcp")

TOKEN_MAX_AGE_SECONDS = 45 * 60
USER_AGENT = "gcp-ai-usage-caps-api-preflight/1.0"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib API
        return None


class StaticTokenGcp:
    """Minimal urllib-based REST client with a static (or gcloud-refreshed) OAuth access token."""

    def __init__(self, token: str | None = None, *, max_workers: int = 16, environ=None, gcloud_bin: str | None = None):
        self._env = os.environ if environ is None else environ
        self._token = token or self._env.get("GCP_ACCESS_TOKEN") or self._env.get("CLOUDSDK_AUTH_ACCESS_TOKEN") or ""
        self._static = bool(self._token)
        self._token_at = time.monotonic() if self._token else 0.0
        self._gcloud = gcloud_bin or shutil.which("gcloud") or ""
        self._lock = threading.Lock()
        self._opener = urllib.request.build_opener(_NoRedirect())
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="caps-q")
        self.subpool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="caps-sub")

    # -- credentials ----------------------------------------------------------------
    def _fetch_gcloud_token(self) -> str:
        if not self._gcloud:
            raise GcpError("credentials", message="no GCP_ACCESS_TOKEN and gcloud is not installed")
        try:
            res = subprocess.run([self._gcloud, "auth", "print-access-token", "--quiet"], capture_output=True,
                                 text=True, timeout=60, stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError) as err:
            raise GcpError("credentials", message=type(err).__name__)
        token = (res.stdout or "").strip().splitlines()[-1].strip() if (res.stdout or "").strip() else ""
        if res.returncode != 0 or not token:
            raise GcpError("credentials", message="gcloud auth print-access-token failed (run: gcloud auth login)")
        return token

    def token(self, force: bool = False) -> str:
        with self._lock:
            stale = time.monotonic() - self._token_at > TOKEN_MAX_AGE_SECONDS
            if self._token and not force and (self._static or not stale):
                return self._token
            if force and self._static and not self._gcloud:
                return self._token
            self._token = self._fetch_gcloud_token()
            self._static = False
            self._token_at = time.monotonic()
            return self._token

    def credentials_ok(self) -> bool:
        try:
            return bool(self.token())
        except GcpError:
            return False

    # -- REST -----------------------------------------------------------------------
    def request(self, method: str, url: str, *, quota_project: str = "", params=None, json_body=None, data=None,
                headers=None, timeout: int = 30, expect_json: bool = True):
        """Same contract as gcp.Gcp.request: returns (payload, headers) or raises GcpError."""
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            url = url + ("&" if "?" in url else "?") + query
        body = None
        hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif data is not None:
            body = data if isinstance(data, bytes) else str(data).encode("utf-8")
        if headers:
            hdrs.update(headers)
        if quota_project:
            hdrs["x-goog-user-project"] = quota_project
        status, raw, resp_headers = 0, b"", {}
        for attempt in (1, 2):
            hdrs["Authorization"] = "Bearer " + self.token(force=attempt == 2)
            req = urllib.request.Request(url, data=body, method=method.upper(), headers=hdrs)
            try:
                with self._opener.open(req, timeout=timeout) as resp:
                    status, raw, resp_headers = resp.status, resp.read(), dict(resp.headers.items())
            except urllib.error.HTTPError as err:
                status = err.code
                try:
                    raw = err.read()
                except Exception:  # pylint: disable=broad-except
                    raw = b""
                resp_headers = dict(err.headers.items()) if err.headers else {}
            except (socket.timeout, TimeoutError):
                raise GcpError("timeout", message="request timed out")
            except urllib.error.URLError as err:
                if isinstance(getattr(err, "reason", None), (socket.timeout, TimeoutError)):
                    raise GcpError("timeout", message="request timed out")
                raise GcpError("network", message=type(err).__name__)
            except OSError as err:
                raise GcpError("network", message=type(err).__name__)
            if status == 401 and attempt == 1 and self._gcloud:
                continue
            break
        payload = None
        if expect_json and raw:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                payload = None
        if status >= 300:
            message, reason = _google_error(payload)
            parsed = urllib.parse.urlsplit(url)
            LOG.debug("upstream error method=%s host=%s path=%s status=%s reason=%s message=%s", method,
                      parsed.hostname, parsed.path[:160], status, reason, log_safe(message))
            raise GcpError(_kind_for_status(status, reason), status=status, message=message, reason=reason)
        if not expect_json:
            return raw, resp_headers
        return (payload if payload is not None else {}), resp_headers

    def paginate(self, url: str, *, quota_project: str = "", items_key: str, params=None, max_pages: int = 10,
                 page_size: int | None = None, method: str = "GET", json_body=None, token_param: str = "pageToken"):
        items = []
        params = dict(params or {})
        body = dict(json_body or {})
        if page_size:
            if method == "GET":
                params["pageSize"] = page_size
            else:
                body["pageSize"] = page_size
        token = None
        for _ in range(max_pages):
            if token:
                if method == "GET":
                    params[token_param] = token
                else:
                    body[token_param] = token
            payload, _ = self.request(method, url, quota_project=quota_project, params=params or None,
                                      json_body=body if method != "GET" else None)
            items.extend(payload.get(items_key) or [])
            token = payload.get("nextPageToken")
            if not token:
                return items, False
        return items, True

    def query(self, *args, **kwargs):  # pragma: no cover - BigQuery jobs are not used by the preflight
        raise GcpError("invalid", message="BigQuery queries are not available in the token client")

    def run_parallel(self, tasks: dict, pool=None) -> dict:
        """Same semantics as gcp.Gcp.run_parallel (two pools; deeper nesting runs inline)."""
        current = threading.current_thread().name
        if pool is None:
            pool = self.subpool if current.startswith("caps-q") else self.pool
        inline = current.startswith("caps-sub") or (pool is self.pool and current.startswith("caps-q"))
        results = {}
        if inline or len(tasks) <= 1:
            for name, fn in tasks.items():
                try:
                    results[name] = (True, fn())
                except Exception as err:  # pylint: disable=broad-except
                    results[name] = (False, err)
            return results
        futures = {pool.submit(fn): name for name, fn in tasks.items()}
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                results[name] = (True, fut.result())
            except Exception as err:  # pylint: disable=broad-except
                results[name] = (False, err)
        return results

    def close(self):
        self.pool.shutdown(wait=False)
        self.subpool.shutdown(wait=False)
