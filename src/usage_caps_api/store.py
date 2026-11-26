"""Optimistic-concurrency JSON document persistence (GCS, local file, or in-memory).

Documents stored under `<prefix>/<name>.json`:
- policy.json       Global spend cap policy overrides
- user_caps.json    Per-user cap rules, block/unblock overrides, suspension records, realtime heartbeat
- project_caps.json Per-project spend cap configurations
- api_keys.json     Salted SHA-256 hashes of M2M API keys (never plaintext)
- webhooks.json     Outbound webhook subscriptions and per-period threshold notification dedupe
- pricing.json      Synced official Cloud Billing Catalog prices & FX rates
- audit_local.json  Bounded local mirror of recent audit events for fast & offline reads
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.parse

from . import config

LOG = logging.getLogger("usage_caps_api.store")

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,48}$")
READ_TTL_SECONDS = 15.0


class StoreConflict(Exception):
    pass


class StoreError(Exception):
    pass


class MemoryBackend:
    backend = "memory"
    durable = False

    def __init__(self):
        self._lock = threading.Lock()
        self._docs: dict[str, tuple[dict, int]] = {}

    @property
    def location(self) -> str:
        return "memory"

    def load(self, name: str) -> tuple[dict, int]:
        with self._lock:
            doc, gen = self._docs.get(name, ({}, 0))
            return copy.deepcopy(doc), gen

    def save(self, name: str, doc: dict, generation: int) -> int:
        with self._lock:
            _, cur = self._docs.get(name, ({}, 0))
            if cur != generation:
                raise StoreConflict()
            self._docs[name] = (copy.deepcopy(doc), generation + 1)
            return generation + 1


class FileBackend:
    backend = "file"

    def __init__(self, directory: str, durable: bool = True):
        self.directory = directory
        self.durable = durable
        self._lock = threading.Lock()

    @property
    def location(self) -> str:
        return self.directory

    def _path(self, name: str) -> str:
        if not NAME_RE.match(name):
            raise ValueError("invalid document name")
        return os.path.join(self.directory, name + ".json")

    def _generation(self, path: str) -> int:
        try:
            return os.stat(path).st_mtime_ns
        except FileNotFoundError:
            return 0

    def load(self, name: str) -> tuple[dict, int]:
        path = self._path(name)
        gen = self._generation(path)
        if not gen:
            return {}, 0
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as err:
            raise StoreError("store file unreadable: %s" % type(err).__name__)
        return (doc if isinstance(doc, dict) else {}), gen

    def save(self, name: str, doc: dict, generation: int) -> int:
        path = self._path(name)
        with self._lock:
            os.makedirs(self.directory, mode=0o700, exist_ok=True)
            if self._generation(path) != generation:
                raise StoreConflict()
            fd, tmp = tempfile.mkstemp(prefix=".%s-" % name, suffix=".json", dir=self.directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh, ensure_ascii=False, indent=2, sort_keys=True)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return self._generation(path)


class GcsBackend:
    backend = "gcs"
    durable = True

    def __init__(self, gcp, bucket: str, prefix: str, quota_project_fn):
        self.gcp = gcp
        self.bucket = bucket
        self.prefix = (prefix or "gcp-ai-usage-caps-api").strip("/")
        self.quota_project_fn = quota_project_fn

    @property
    def location(self) -> str:
        return "gs://%s/%s" % (self.bucket, self.prefix)

    def _obj(self, name: str) -> str:
        if not NAME_RE.match(name):
            raise ValueError("invalid document name")
        return "%s/%s.json" % (self.prefix, name) if self.prefix else (name + ".json")

    def load(self, name: str) -> tuple[dict, int]:
        from .gcp import GcpError
        obj = self._obj(name)
        url = "https://storage.googleapis.com/storage/v1/b/%s/o/%s" % (
            urllib.parse.quote(self.bucket, safe=""),
            urllib.parse.quote(obj, safe=""),
        )
        try:
            content, headers = self.gcp.request(
                "GET", url, quota_project=self.quota_project_fn(),
                params={"alt": "media"}, expect_json=False,
            )
        except GcpError as err:
            if err.kind == "not_found":
                return {}, 0
            raise StoreError("GCS object unreadable (%s)" % err.kind)
        gen = int(headers.get("x-goog-generation") or 0)
        try:
            doc = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise StoreError("GCS object is not valid JSON")
        return (doc if isinstance(doc, dict) else {}), gen

    def save(self, name: str, doc: dict, generation: int) -> int:
        from .gcp import GcpError
        obj = self._obj(name)
        url = "https://storage.googleapis.com/upload/storage/v1/b/%s/o" % urllib.parse.quote(self.bucket, safe="")
        body = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        try:
            payload, _ = self.gcp.request(
                "POST", url, quota_project=self.quota_project_fn(),
                params={"uploadType": "media", "name": obj, "ifGenerationMatch": str(generation)},
                data=body, headers={"Content-Type": "application/json"},
            )
        except GcpError as err:
            if err.kind == "precondition":
                raise StoreConflict()
            raise StoreError("GCS object write failed (%s)" % err.kind)
        return int(payload.get("generation") or 0)


class DocStore:
    """High-level named document store with TTL read cache and optimistic-concurrency retries."""

    def __init__(self, backend, read_ttl: float = READ_TTL_SECONDS):
        self.backend = backend
        self.read_ttl = read_ttl
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[dict, int, float]] = {}

    @property
    def kind(self) -> str:
        return self.backend.backend

    @property
    def durable(self) -> bool:
        return bool(self.backend.durable)

    @property
    def location(self) -> str:
        return self.backend.location

    def clear_cache(self, name: str | None = None):
        with self._lock:
            if name is None:
                self._cache.clear()
            else:
                self._cache.pop(name, None)

    def load(self, name: str, fresh: bool = False) -> tuple[dict, int]:
        if not NAME_RE.match(name):
            raise ValueError("invalid document name: %s" % name)
        now = time.monotonic()
        if not fresh and self.kind != "memory":
            with self._lock:
                hit = self._cache.get(name)
            if hit and now - hit[2] < self.read_ttl:
                return copy.deepcopy(hit[0]), hit[1]
        doc, gen = self.backend.load(name)
        with self._lock:
            self._cache[name] = (copy.deepcopy(doc), gen, now)
        return copy.deepcopy(doc), gen

    def mutate(self, name: str, fn, attempts: int = 5):
        """Loads fresh doc, calls fn(doc), and saves with optimistic concurrency retry."""
        for _ in range(attempts):
            doc, gen = self.load(name, fresh=True)
            result = fn(doc)
            try:
                new_gen = self.backend.save(name, doc, gen)
                with self._lock:
                    self._cache[name] = (copy.deepcopy(doc), new_gen, time.monotonic())
                return result
            except StoreConflict:
                self.clear_cache(name)
                continue
        raise StoreConflict("Concurrent update conflict on document '%s'" % name)


def make_doc_store(cfg: dict, gcp, environ=None) -> DocStore:
    env = os.environ if environ is None else environ
    st = cfg.get("storage") or {}
    backend = st.get("backend") or "auto"
    on_cloud_run = bool(env.get("K_SERVICE") or env.get("CLOUD_RUN_JOB"))
    if backend == "auto":
        if st.get("gcs_bucket"):
            backend = "gcs"
        else:
            backend = "memory" if on_cloud_run else "file"
    if backend == "gcs":
        bucket = st.get("gcs_bucket") or ""
        if not bucket:
            raise ValueError("storage.backend is 'gcs' but storage.gcs_bucket is empty")
        return DocStore(GcsBackend(gcp, bucket, st.get("gcs_prefix") or "gcp-ai-usage-caps-api",
                                   lambda: config.quota_project(cfg)))
    if backend == "file":
        path = st.get("file_path") or "data"
        if not os.path.isabs(path):
            path = os.path.join(config.repo_root(), path)
        return DocStore(FileBackend(path, durable=not on_cloud_run))
    return DocStore(MemoryBackend())
