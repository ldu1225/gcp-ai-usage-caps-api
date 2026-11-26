"""M2M authentication and authorization: scoped API keys (hashed) and GCP Service Account OIDC.

Security guarantees:
- API keys are NEVER stored in plaintext. Only a per-key random salt + SHA-256 digest is persisted.
- Key comparison uses constant-time `hmac.compare_digest`.
- Scopes: `usage:read`, `caps:read`, `caps:write`, `users:write`, `models:read`, `models:write`, `quotas:read`,
  `quotas:write`, `admin` (`admin` implies all scopes).
- GCP OIDC verifies RS256 Google ID tokens (`accounts.google.com`) against `PUBLIC_BASE_URL` audience
  and checks allowlisted service accounts (plus the Cloud Run runtime SA for Pub/Sub & Scheduler).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass

from .httpkit import ApiError, now_iso
from .timeutil import parse_rfc3339

LOG = logging.getLogger("usage_caps_api.auth")

API_KEYS_DOC = "api_keys"
KEY_PREFIX = "gcpai"
KEY_ID_RE = re.compile(r"^[0-9a-f]{8,16}$")
ALL_SCOPES = ("usage:read", "caps:read", "caps:write", "users:write",
              "models:read", "models:write", "quotas:read", "quotas:write", "admin")
ALL_SCOPES_SET = frozenset(ALL_SCOPES)
INTERNAL_SCOPE = "internal:pubsub"

GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
FORWARDING_HEADERS = ("X-Forwarded-For", "Forwarded", "X-Real-IP")


@dataclass(frozen=True)
class Caller:
    id: str
    name: str
    auth_type: str        # api_key | oidc | bootstrap | local
    scopes: frozenset
    rate_key: str

    def has_scope(self, required: str | tuple[str, ...] | None) -> bool:
        if required is None:
            return True
        if "admin" in self.scopes:
            return True
        if isinstance(required, str):
            return required in self.scopes
        return any(s in self.scopes for s in required)


# ---------------------------------------------------------------------------
# API Key hashing & lifecycle
# ---------------------------------------------------------------------------

def _hash_secret(salt: str, secret: str) -> str:
    return hashlib.sha256(("%s:%s" % (salt, secret)).encode("utf-8")).hexdigest()


def parse_api_key(raw: str) -> tuple[str, str] | None:
    """Parses `gcpai_<key_id>_<secret>` into (key_id, secret), else None."""
    if not isinstance(raw, str) or len(raw) > 256:
        return None
    parts = raw.split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_PREFIX or not KEY_ID_RE.match(parts[1]) or len(parts[2]) < 16:
        return None
    return parts[1], parts[2]


def validate_scopes(scopes) -> list[str]:
    if not isinstance(scopes, list) or not scopes:
        raise ApiError("bad_request", "Field 'scopes' must be a non-empty list of valid scopes.")
    clean = []
    for item in scopes:
        s = str(item or "").strip()
        if s not in ALL_SCOPES_SET:
            raise ApiError("bad_request", "Unknown scope '%s'. Valid scopes: %s." % (s, ", ".join(ALL_SCOPES)))
        if s not in clean:
            clean.append(s)
    return clean


def create_api_key(store, *, name: str, scopes: list[str], created_by: str = "system",
                   expires_at: str | None = None) -> dict:
    """Creates a new scoped API key. Plaintext `key` is returned ONCE and never stored."""
    clean_name = str(name or "").strip()[:120]
    if not clean_name:
        raise ApiError("bad_request", "Field 'name' is required.")
    clean_scopes = validate_scopes(scopes)
    if expires_at is not None and expires_at != "":
        if parse_rfc3339(expires_at) is None:
            raise ApiError("bad_request", "Field 'expires_at' must be an RFC3339 timestamp.")
    else:
        expires_at = None

    key_id = secrets.token_hex(6)  # 12 hex chars
    secret = secrets.token_urlsafe(32)
    salt = secrets.token_hex(16)
    digest = _hash_secret(salt, secret)
    plaintext = "%s_%s_%s" % (KEY_PREFIX, key_id, secret)
    masked_prefix = "%s_%s_%s..." % (KEY_PREFIX, key_id, secret[:4])
    created_at = now_iso()

    record = {
        "id": key_id,
        "name": clean_name,
        "prefix": masked_prefix,
        "scopes": clean_scopes,
        "salt": salt,
        "hash": digest,
        "created_at": created_at,
        "created_by": created_by[:200],
        "expires_at": expires_at,
        "revoked_at": None,
        "revoked_by": None,
        "last_used_at": None,
    }

    def add(doc):
        keys = [k for k in (doc.get("keys") or []) if isinstance(k, dict)]
        if len(keys) >= 500:
            raise ApiError("bad_request", "Maximum number of API keys (500) reached.")
        keys.append(record)
        doc["keys"] = keys
        return record

    store.mutate(API_KEYS_DOC, add)
    return dict(public_key_view(record), key=plaintext)


def public_key_view(rec: dict) -> dict:
    return {
        "id": rec.get("id"),
        "name": rec.get("name"),
        "prefix": rec.get("prefix"),
        "scopes": list(rec.get("scopes") or []),
        "created_at": rec.get("created_at"),
        "created_by": rec.get("created_by"),
        "expires_at": rec.get("expires_at"),
        "revoked_at": rec.get("revoked_at"),
        "last_used_at": rec.get("last_used_at"),
        "status": "revoked" if rec.get("revoked_at") else "active",
    }


def list_api_keys(store) -> list[dict]:
    doc, _ = store.load(API_KEYS_DOC)
    keys = [public_key_view(k) for k in (doc.get("keys") or []) if isinstance(k, dict) and k.get("id")]
    keys.sort(key=lambda k: k.get("created_at") or "", reverse=True)
    return keys


def revoke_api_key(store, key_id: str, *, revoked_by: str = "system") -> dict:
    if not isinstance(key_id, str) or not KEY_ID_RE.match(key_id):
        raise ApiError("bad_request", "Invalid API key id.")

    def do_revoke(doc):
        for k in doc.get("keys") or []:
            if isinstance(k, dict) and k.get("id") == key_id:
                if not k.get("revoked_at"):
                    k["revoked_at"] = now_iso()
                    k["revoked_by"] = revoked_by[:200]
                return public_key_view(k)
        raise ApiError("not_found", "API key '%s' not found." % key_id)

    return store.mutate(API_KEYS_DOC, do_revoke)


# ---------------------------------------------------------------------------
# Google OIDC ID Token Verification (Cloud Scheduler / Pub/Sub / Internal GCP SA)
# ---------------------------------------------------------------------------

class _CachingCertRequest:
    def __init__(self, ttl_seconds: int = 3600):
        from google.auth.transport import requests as ga_requests  # type: ignore
        self._inner = ga_requests.Request()
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._cached: dict = {}

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        if method == "GET" and url == GOOGLE_CERTS_URL:
            with self._lock:
                hit = self._cached.get(url)
                if hit and hit[0] > time.monotonic():
                    return hit[1]
            resp = self._inner(url, method=method, body=body, headers=headers, timeout=timeout or 10, **kwargs)
            if getattr(resp, "status", None) == 200:
                _ = resp.data
                with self._lock:
                    self._cached[url] = (time.monotonic() + self._ttl, resp)
            return resp
        return self._inner(url, method=method, body=body, headers=headers, timeout=timeout, **kwargs)


def _jwt_alg(token: str) -> str:
    try:
        seg = token.split(".")[0]
        seg += "=" * (-len(seg) % 4)
        return str(json.loads(base64.urlsafe_b64decode(seg.encode("ascii"))).get("alg") or "")
    except Exception:
        return ""


class GoogleOidcVerifier:
    def __init__(self):
        self._request = None
        self._lock = threading.Lock()

    def _get_request(self):
        with self._lock:
            if self._request is None:
                self._request = _CachingCertRequest()
            return self._request

    def verify(self, token: str, audiences: list[str]) -> str:
        audiences = [a for a in audiences if a]
        if not audiences:
            raise ValueError("no audience configured")
        if not token or len(token) > 8192 or token.count(".") != 2:
            raise ValueError("malformed token")
        if _jwt_alg(token) != "RS256":
            raise ValueError("unexpected algorithm")
        from google.oauth2 import id_token  # type: ignore
        claims = id_token.verify_token(
            token, self._get_request(), audience=audiences,
            certs_url=GOOGLE_CERTS_URL, clock_skew_in_seconds=10,
        )
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise ValueError("unexpected issuer")
        if claims.get("email_verified") is not True:
            raise ValueError("email not verified")
        email = (claims.get("email") or "").strip().lower()
        if not email or "@" not in email:
            raise ValueError("missing email claim")
        return email


_RUNTIME_SA = {"value": None, "checked": False}
_RUNTIME_SA_LOCK = threading.Lock()


def runtime_service_account(environ=None) -> str | None:
    env = os.environ if environ is None else environ
    explicit = (env.get("RUNTIME_SERVICE_ACCOUNT") or "").strip().lower()
    if explicit and "@" in explicit:
        return explicit
    with _RUNTIME_SA_LOCK:
        if _RUNTIME_SA["checked"]:
            return _RUNTIME_SA["value"]
        _RUNTIME_SA["checked"] = True
        if not env.get("K_SERVICE"):
            return None
        try:
            import urllib.request
            req = urllib.request.Request(
                "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email",
                headers={"Metadata-Flavor": "Google"},
            )
            with urllib.request.urlopen(req, timeout=2) as resp:  # nosec B310
                val = resp.read(320).decode("utf-8").strip().lower()
            _RUNTIME_SA["value"] = val if "@" in val else None
        except Exception:
            _RUNTIME_SA["value"] = None
        return _RUNTIME_SA["value"]


def is_loopback(peer: str) -> bool:
    try:
        addr = ipaddress.ip_address((peer or "").split("%")[0])
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return addr.is_loopback


# ---------------------------------------------------------------------------
# Authenticator
# ---------------------------------------------------------------------------

class Authenticator:
    """Resolves Bearer API Keys, Google OIDC Service Account JWTs, or bootstrap admin key."""

    def __init__(self, store, cfg: dict, verifier: GoogleOidcVerifier | None = None, environ=None):
        self.store = store
        self.cfg = cfg
        self.verifier = verifier or GoogleOidcVerifier()
        self.environ = os.environ if environ is None else environ
        # Hash bootstrap admin key in memory only; never keep plaintext around.
        raw_boot = (self.environ.get("BOOTSTRAP_ADMIN_KEY") or "").strip()
        self._bootstrap_salt = secrets.token_hex(16) if raw_boot else ""
        self._bootstrap_hash = _hash_secret(self._bootstrap_salt, raw_boot) if raw_boot else ""
        self._last_used_touch: dict[str, float] = {}
        self._touch_lock = threading.Lock()

    def _touch_last_used(self, key_id: str):
        now = time.monotonic()
        with self._touch_lock:
            if now - self._last_used_touch.get(key_id, 0.0) < 300.0:
                return
            self._last_used_touch[key_id] = now
        try:
            def mark(doc):
                for k in doc.get("keys") or []:
                    if isinstance(k, dict) and k.get("id") == key_id:
                        k["last_used_at"] = now_iso()
                        break
            self.store.mutate(API_KEYS_DOC, mark)
        except Exception:
            pass

    def _verify_api_key(self, token: str) -> Caller:
        if self._bootstrap_hash:
            cand = _hash_secret(self._bootstrap_salt, token)
            if hmac.compare_digest(cand.encode("ascii"), self._bootstrap_hash.encode("ascii")):
                return Caller(id="bootstrap-admin", name="Bootstrap Admin Key", auth_type="bootstrap",
                              scopes=frozenset(ALL_SCOPES), rate_key="key:bootstrap")
        parsed = parse_api_key(token)
        if parsed is None:
            raise ApiError("unauthenticated", "Invalid API key format.")
        key_id, secret = parsed
        doc, _ = self.store.load(API_KEYS_DOC)
        hit = None
        for rec in doc.get("keys") or []:
            if isinstance(rec, dict) and rec.get("id") == key_id:
                hit = rec
                break
        if hit is None:
            raise ApiError("unauthenticated", "API key is invalid.")
        expected = str(hit.get("hash") or "")
        actual = _hash_secret(str(hit.get("salt") or ""), secret)
        if not expected or not hmac.compare_digest(actual.encode("ascii"), expected.encode("ascii")):
            raise ApiError("unauthenticated", "API key is invalid.")
        if hit.get("revoked_at"):
            raise ApiError("unauthenticated", "API key has been revoked.")
        exp = parse_rfc3339(hit.get("expires_at"))
        if exp is not None and dt.datetime.now(dt.timezone.utc) >= exp:
            raise ApiError("unauthenticated", "API key has expired.")
        self._touch_last_used(key_id)
        scopes = frozenset(s for s in (hit.get("scopes") or []) if s in ALL_SCOPES_SET)
        return Caller(id="key:" + key_id, name=str(hit.get("name") or key_id), auth_type="api_key",
                      scopes=scopes, rate_key="key:" + key_id)

    def _verify_oidc(self, token: str, path: str) -> Caller:
        auth_cfg = self.cfg.get("auth") or {}
        base = str(auth_cfg.get("public_base_url") or "").rstrip("/")
        if not base:
            raise ApiError("unauthenticated", "OIDC authentication requires PUBLIC_BASE_URL to be configured.")
        allowed = {str(e).strip().lower() for e in (auth_cfg.get("oidc_allowed_service_accounts") or []) if e}
        own_sa = runtime_service_account(self.environ)
        if own_sa:
            allowed.add(own_sa)
        if not allowed:
            raise ApiError("unauthenticated", "No OIDC service accounts are allowed for this deployment.")
        try:
            email = self.verifier.verify(token, [base + path, base, base + "/"])
        except Exception as err:
            LOG.warning("OIDC token rejected reason=%s", type(err).__name__)
            raise ApiError("unauthenticated", "Invalid or expired Google OIDC token.")
        if email not in allowed:
            LOG.warning("OIDC token rejected: service account not in allowlist")
            raise ApiError("forbidden", "This service account is not authorized to call this API.")
        scopes = set(s for s in (auth_cfg.get("oidc_default_scopes") or []) if s in ALL_SCOPES_SET)
        internal = {str(e).strip().lower() for e in (auth_cfg.get("oidc_internal_service_accounts") or []) if e}
        if own_sa and email == own_sa:
            scopes.update({"caps:write", "caps:read", "usage:read", INTERNAL_SCOPE})
        elif email in internal or not own_sa:
            # Pub/Sub push / Scheduler identities explicitly allowed to call /v1/internal/* (or legacy deployments
            # without a known runtime SA).
            scopes.add(INTERNAL_SCOPE)
        return Caller(id=email, name=email, auth_type="oidc", scopes=frozenset(scopes), rate_key="oidc:" + email)

    def authenticate(self, headers, peer: str, path: str, *, allow_optional: bool = False) -> Caller | None:
        auth = headers.get("Authorization") or ""
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        if token:
            # JWTs have two dots; API keys start with gcpai_ or match bootstrap key.
            if token.count(".") == 2 and not token.startswith(KEY_PREFIX + "_"):
                return self._verify_oidc(token, path)
            return self._verify_api_key(token)
        # Optional local loopback dev bypass (explicit opt-in only, strictly refused on Cloud Run).
        if (self.cfg.get("auth") or {}).get("allow_local_insecure") and not self.environ.get("K_SERVICE"):
            forwarded = any(headers.get(h) for h in FORWARDING_HEADERS)
            if is_loopback(peer) and not forwarded:
                return Caller(id="local-dev@localhost", name="Local Dev", auth_type="local",
                              scopes=frozenset(ALL_SCOPES_SET | {INTERNAL_SCOPE}), rate_key="local:" + (peer or "127.0.0.1"))
        if allow_optional:
            return None
        raise ApiError("unauthenticated", "Missing Authorization: Bearer <token> header.")


# ---------------------------------------------------------------------------
# Per-caller in-memory token bucket rate limiter
# ---------------------------------------------------------------------------

RATE_LIMITS = {
    "general": 300,
    "write": 60,
    "enforce": 20,
    "usage_event": 6000,
}


class RateLimiter:
    def __init__(self, limits: dict | None = None):
        self.limits = dict(limits or RATE_LIMITS)
        self._lock = threading.Lock()
        self._buckets: dict = {}
        self._last_prune = time.monotonic()

    def allow(self, key: str, bucket: str) -> tuple[bool, int]:
        limit = self.limits.get(bucket, 300)
        rate = limit / 60.0
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get((key, bucket), (float(limit), now))
            tokens = min(float(limit), tokens + (now - last) * rate)
            if tokens < 1.0:
                self._buckets[(key, bucket)] = (tokens, now)
                return False, max(1, int(math.ceil((1.0 - tokens) / rate)))
            self._buckets[(key, bucket)] = (tokens - 1.0, now)
            if now - self._last_prune > 60:
                self._last_prune = now
                stale = [k for k, (_, t) in self._buckets.items() if now - t > 600]
                for k in stale:
                    del self._buckets[k]
            return True, 0

    def check(self, key: str, bucket: str):
        allowed, retry = self.allow(key, bucket)
        if not allowed:
            raise ApiError("rate_limited", "Rate limit exceeded. Retry after %d seconds." % retry,
                           headers=[("Retry-After", str(retry))])
