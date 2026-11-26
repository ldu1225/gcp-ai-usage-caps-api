"""Lightweight stdlib HTTP server, parameterized router, JSON & pagination helpers.

Pure Python standard library so all unit tests run offline without third-party dependencies.
"""

from __future__ import annotations

import base64
import datetime as _dt
import decimal
import json
import logging
import math
import re
import secrets
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("usage_caps_api.http")

ERROR_STATUS = {
    "bad_request": 400,
    "invalid_request": 400,
    "unauthenticated": 401,
    "forbidden": 403,
    "insufficient_scope": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "conflict": 409,
    "not_configured": 409,
    "console_required": 409,
    "not_applicable": 409,
    "quota_request_pending": 409,
    "precondition_failed": 412,
    "unprocessable": 422,
    "lockout_prevented": 422,
    "quota_exceeds_maximum": 422,
    "rate_limited": 429,
    "upstream_error": 502,
    "unavailable": 503,
    "internal": 500,
}

ERROR_TYPE = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    405: "invalid_request_error",
    409: "conflict_error",
    412: "precondition_failed_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
    502: "upstream_error",
    503: "service_unavailable_error",
    500: "api_error",
}

MAX_BODY_BYTES = 256 * 1024
MAX_QUERY_FIELDS = 64
ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_PARAM_SEGMENT_RE = re.compile(r"^\{([a-zA-Z_][a-zA-Z0-9_]*)\}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ApiError(Exception):
    """Error rendered in OpenAI/Anthropic Admin API style: {"error": {"type", "code", "message"}}."""

    def __init__(self, code: str, message: str, status: int | None = None, headers=None, error_type: str | None = None):
        super().__init__(message)
        self.code = code if code in ERROR_STATUS else "internal"
        self.message = message
        self.status = int(status or ERROR_STATUS[self.code])
        self.error_type = error_type or ERROR_TYPE.get(self.status, "api_error")
        self.headers = list(headers or [])


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def iso_z(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        return value.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, _dt.date):
        return value.isoformat()
    return str(value)


def now_iso() -> str:
    return iso_z(utc_now()) or ""


def _json_default(obj):
    if isinstance(obj, _dt.datetime):
        return iso_z(obj)
    if isinstance(obj, (_dt.date, _dt.time)):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
        value = float(obj)
        return value if math.isfinite(value) else None
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, (bytes, bytearray)):
        return base64.b64encode(bytes(obj)).decode("ascii")
    raise TypeError("Object of type %s is not JSON serializable" % type(obj).__name__)


def sanitize_json(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: sanitize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json(v) for v in value]
    return value


def json_bytes(payload) -> bytes:
    try:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                          default=_json_default, allow_nan=False)
    except ValueError:
        text = json.dumps(sanitize_json(payload), ensure_ascii=False, separators=(",", ":"),
                          default=_json_default, allow_nan=False)
    return text.encode("utf-8")


def request_is_https(headers) -> bool:
    proto = (headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    return proto == "https"


def security_headers(https: bool, request_id: str | None = None):
    headers = [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"),
    ]
    if request_id:
        headers.append(("X-Request-Id", request_id))
    if https:
        headers.append(("Strict-Transport-Security", "max-age=31536000; includeSubDomains"))
    return headers


def parse_query(query: str) -> dict[str, str]:
    try:
        pairs = urllib.parse.parse_qsl(query or "", keep_blank_values=True, max_num_fields=MAX_QUERY_FIELDS)
    except ValueError:
        raise ApiError("bad_request", "Too many query parameters.")
    out: dict[str, str] = {}
    for key, value in pairs:
        if len(key) > 64 or len(value) > 512:
            raise ApiError("bad_request", "Query parameter is too long.")
        out.setdefault(key, value)
    return out


def parse_json_object(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApiError("bad_request", "Request body must be valid JSON.")
    if not isinstance(obj, dict):
        raise ApiError("bad_request", "Request body must be a JSON object.")
    return obj


# ---------------------------------------------------------------------------
# Cursor pagination (OpenAI / Anthropic Admin API style)
# ---------------------------------------------------------------------------

def encode_cursor(offset: int) -> str:
    raw = json.dumps({"o": max(0, int(offset))}, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        obj = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii"))
        offset = int(obj.get("o"))
        if offset < 0:
            raise ValueError("negative offset")
        return offset
    except Exception:
        raise ApiError("bad_request", "Parameter 'cursor' is invalid.")


def parse_limit(raw, default: int = 100, maximum: int = 500) -> int:
    if raw is None or raw == "":
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise ApiError("bad_request", "Parameter 'limit' must be an integer.")
    if val < 1 or val > maximum:
        raise ApiError("bad_request", "Parameter 'limit' must be between 1 and %d." % maximum)
    return val


def paginate_list(items: list, params: dict, *, default_limit: int = 100, max_limit: int = 500) -> dict:
    limit = parse_limit(params.get("limit"), default=default_limit, maximum=max_limit)
    offset = decode_cursor(params.get("cursor"))
    total = len(items)
    page = items[offset:offset + limit]
    has_more = (offset + limit) < total
    next_cursor = encode_cursor(offset + limit) if has_more else None
    return {
        "object": "list",
        "data": page,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "total": total,
    }


# ---------------------------------------------------------------------------
# Parameterized Router
# ---------------------------------------------------------------------------

class _Route:
    def __init__(self, method: str, pattern: str, fn, opts: dict):
        self.method = method
        self.pattern = pattern
        self.fn = fn
        self.opts = opts
        parts = [p for p in pattern.strip("/").split("/") if p]
        self.segments = []
        self.is_exact = True
        for part in parts:
            m = _PARAM_SEGMENT_RE.match(part)
            if m:
                self.segments.append((True, m.group(1)))
                self.is_exact = False
            else:
                self.segments.append((False, part))

    def match(self, raw_segments: list[str]) -> dict[str, str] | None:
        if len(raw_segments) != len(self.segments):
            return None
        params: dict[str, str] = {}
        for raw, (is_param, expected) in zip(raw_segments, self.segments):
            if is_param:
                val = urllib.parse.unquote(raw)
                if not val or len(val) > 600 or _CONTROL_RE.search(val):
                    return None
                params[expected] = val
            else:
                if raw != expected:
                    return None
        return params


class Router:
    """Router supporting exact paths and single-segment {param} templates."""

    def __init__(self):
        self._exact: dict[tuple[str, str], tuple] = {}
        self._dynamic: list[_Route] = []

    def route(self, method: str, path: str, **opts):
        def deco(fn):
            rt = _Route(method.upper(), path, fn, opts)
            if rt.is_exact:
                self._exact[(rt.method, path)] = (fn, opts)
            else:
                self._dynamic.append(rt)
            return fn
        return deco

    def resolve(self, method: str, path: str):
        """Returns (fn, opts, path_params). Raises ApiError(405/404) when unmatched."""
        method = method.upper()
        exact = self._exact.get((method, path))
        if exact is not None:
            return exact[0], exact[1], {}
        raw_segments = [p for p in path.strip("/").split("/") if p]
        for rt in self._dynamic:
            if rt.method != method:
                continue
            params = rt.match(raw_segments)
            if params is not None:
                return rt.fn, rt.opts, params
        # Check whether another method matches this path to return 405 with accurate Allow header.
        allowed = sorted({m for (m, p) in self._exact if p == path} |
                         {rt.method for rt in self._dynamic if rt.match(raw_segments) is not None})
        if allowed:
            raise ApiError("method_not_allowed", "Method '%s' is not allowed for this endpoint." % method,
                           status=405, headers=[("Allow", ", ".join(allowed))])
        raise ApiError("not_found", "Endpoint not found.")

    def routes(self) -> list[tuple[str, str]]:
        out = list(self._exact.keys()) + [(rt.method, rt.pattern) for rt in self._dynamic]
        return sorted(out)


# ---------------------------------------------------------------------------
# HTTP Server & Base Handler
# ---------------------------------------------------------------------------

class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        err = sys.exc_info()[1]
        if isinstance(err, (ConnectionError, TimeoutError)):
            LOG.debug("client connection closed reason=%s", type(err).__name__)
            return
        LOG.error("unhandled server error reason=%s", type(err).__name__ if err else "unknown")


class BaseHandler(BaseHTTPRequestHandler):
    server_version = "GcpAiUsageCapsApi"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def __getattr__(self, name: str):
        if name.startswith("do_"):
            return self._reject_method
        raise AttributeError(name)

    def _reject_method(self):
        self._t0 = getattr(self, "_t0", None) or time.monotonic()
        self._request_id = getattr(self, "_request_id", None) or ("req_" + secrets.token_hex(8))
        self.close_connection = True
        self.send_error_json(ApiError("method_not_allowed", "Method not allowed.", status=405,
                                      headers=[("Allow", ", ".join(ALLOWED_METHODS))]))
        self._log_access(self.command)

    @property
    def is_https(self) -> bool:
        return request_is_https(self.headers)

    def client_ip(self) -> str:
        try:
            return self.client_address[0]
        except (AttributeError, IndexError, TypeError):
            return ""

    def _dispatch(self, method: str):
        self._t0 = time.monotonic()
        self._status = 0
        self._sent = False
        self._request_id = "req_" + secrets.token_hex(8)
        try:
            split = urllib.parse.urlsplit(self.path)
            path = split.path or "/"
            if path in ("/health", "/healthz"):
                self.send_json(200, {"status": "ok"})
            else:
                self.handle_api(method, path, split.query)
        except ApiError as err:
            self.send_error_json(err)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:  # pylint: disable=broad-except
            LOG.exception("unhandled error method=%s path=%s req=%s", method, self._safe_path(), self._request_id)
            self.send_error_json(ApiError("internal", "An internal server error occurred."))
        finally:
            self._log_access(method)

    def handle_api(self, method: str, path: str, query: str):  # pragma: no cover - abstract
        raise ApiError("not_found", "Endpoint not found.")

    def send_response(self, code, message=None):
        self._status = code
        super().send_response(code, message)

    def log_request(self, code="-", size="-"):
        return

    def log_message(self, fmt, *args):
        LOG.debug("http-server " + fmt, *args)

    def _safe_path(self) -> str:
        try:
            return urllib.parse.urlsplit(self.path).path[:200]
        except Exception:
            return "-"

    def _log_access(self, method: str):
        try:
            elapsed = int((time.monotonic() - (self._t0 or time.monotonic())) * 1000)
        except Exception:
            elapsed = 0
        LOG.info("%s %s status=%s ms=%d req=%s", method, self._safe_path(), getattr(self, "_status", 0),
                 elapsed, getattr(self, "_request_id", "-"))

    def _write(self, status: int, body: bytes | None, content_type: str, cache_control: str = "no-store",
               extra_headers=()):
        if getattr(self, "_sent", False):
            return
        self._sent = True
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
        else:
            self.send_header("Content-Length", "0")
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        for key, value in security_headers(self.is_https, getattr(self, "_request_id", None)):
            self.send_header(key, value)
        for key, value in extra_headers:
            self.send_header(key, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def send_json(self, status: int, payload, extra_headers=()):
        self._write(status, json_bytes(payload), "application/json; charset=utf-8", "no-store", extra_headers)

    def send_text(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8",
                  cache_control: str = "public, max-age=300", extra_headers=()):
        self._write(status, text.encode("utf-8"), content_type, cache_control, extra_headers)

    def send_error_json(self, err: ApiError, extra_headers=()):
        payload = {"error": {"type": err.error_type, "code": err.code, "message": err.message}}
        self.send_json(err.status, payload, list(err.headers) + list(extra_headers))

    def read_body_bytes(self, limit: int = MAX_BODY_BYTES) -> bytes:
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            if (self.headers.get("Transfer-Encoding") or "").lower():
                self.close_connection = True
                raise ApiError("bad_request", "Chunked request bodies are not supported.")
            return b""
        try:
            length = int(raw_len)
        except ValueError:
            self.close_connection = True
            raise ApiError("bad_request", "Invalid Content-Length header.")
        if length < 0 or length > limit:
            self.close_connection = True
            raise ApiError("bad_request", "Request body exceeds maximum size of %d bytes." % limit)
        return self.rfile.read(length) if length else b""
