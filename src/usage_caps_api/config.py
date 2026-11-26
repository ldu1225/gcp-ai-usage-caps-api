"""Layered configuration loading, validation, and environment variable overrides.

Load order (later wins): DEFAULTS < config.yaml < config.local.yaml < environment overrides.
"""

from __future__ import annotations

import copy
import ipaddress
import logging
import os
import re
import socket
import urllib.parse

from .identity import EMAIL_RE
from .timeutil import PERIODS, RESET_TIME_RE, WINDOWS, valid_timezone

LOG = logging.getLogger("usage_caps_api.config")

PROJECT_RE = re.compile(r"^[a-z][a-z0-9.\-:]{4,99}$")
DATASET_RE = re.compile(r"^[A-Za-z0-9_]{1,1024}$")
TABLE_RE = re.compile(r"^[A-Za-z0-9_\-$]{1,1024}$")
COLUMN_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,299}$")
SCOPE_ID_RE = re.compile(r"^[0-9]{1,30}$")
LOCATION_RE = re.compile(r"^[a-z][a-z0-9\-]{1,30}$")
LOG_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-%/]{1,256}$")
MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.\-_]{0,120}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
ACCOUNT_RE = re.compile(r"^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$")
DENY_POLICY_ID_RE = re.compile(r"^gcp-ai-caps-[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?$")
ENFORCEMENT_JOB_RE = re.compile(
    r"^(projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/locations/[a-z][a-z0-9-]{1,30}/jobs/"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?)?$"
)
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

VALID_SERVICES = ("antigravity", "vertex_ai", "gemini_enterprise")
ENFORCEMENT_MODES = ("manual", "auto")
ENFORCEMENT_BACKENDS = ("iam_deny", "notify_only")
MANDATORY_EXCLUDED_SKU_PATTERNS = ("code assist", "duet ai")
PLACEHOLDER_PROJECTS = ("your-gcp-project-id", "demo-project", "my-project")

DEFAULTS = {
    "server": {
        "host": "127.0.0.1",
        "port": 8080,
        "cache_ttl_seconds": 120,
    },
    "workspace": {
        "project_id": "your-gcp-project-id",
        "projects": [],
        "scan_all_projects": True,
        "quota_project_id": "",
        "timezone": "UTC",
        "default_range_days": 14,
        "audit_dataset": "ge_analytics",
        "audit_project": "",
        "scope_type": "projects",
        "scope_ids": [],
        "scope_max_projects": 300,
        "scope_filter": "none",
        "discovery_engine_locations": ["global"],
        "billing": {
            "project_id": "",
            "dataset": "",
            "table": "",
            "account_id": "",
        },
    },
    "storage": {
        "backend": "auto",
        "file_path": "data",
        "gcs_bucket": "",
        "gcs_prefix": "gcp-ai-usage-caps-api",
    },
    "bigquery": {
        "max_bytes_billed": 20000000000,
    },
    "auth": {
        "public_base_url": "",
        "oidc_allowed_service_accounts": [],
        "oidc_default_scopes": ["usage:read", "caps:read", "caps:write"],
        "allow_local_insecure": False,
    },
    "caps": {
        "usage_source": "auto",
        "inference_log": "businessaicode.googleapis.com%2Finference_response",
        "audit_log_name": "gcp-ai-usage-caps-audit",
        "max_log_entries": 20000,
        "enforcement_mode": "manual",
        "enforcement_backend": "iam_deny",
        "deny_policy_id": "gcp-ai-caps-managed",
        "deny_attachment": "projects",
        "default_block_services": list(VALID_SERVICES),
        "period": "monthly",
        "window": "calendar",
        "reset_timezone": "",
        "reset_weekday": 0,
        "reset_day": 1,
        "reset_time": "00:00",
        "cycle_days": 30,
        "cycle_start": "",
        "cost_cap": 0.0,
        "token_cap": 0,
        "request_cap": 0,
        "currency": "USD",
        "webhook_thresholds": [80, 100],
        "change_budget_min": 10,
        "change_budget_ratio": 0.2,
        "surge_guard_min": 10,
        "surge_guard_ratio": 0.5,
        "spend_caps_enabled": False,
        "enforcement_job": "",
        "usage_events_subscription": "",
        "credit_per_user_month": 10.0,
        "credit_model_prefixes": ["gemini"],
        "chargeback_allocation_mode": "credit_share",
    },
    "pricing": {
        "source": "auto",
        "currency": "USD",
        "input_price_per_million": 0.0,
        "output_price_per_million": 0.0,
        "model_overrides": [],
        "model_aliases": [],
        "catalog_services": ["Vertex AI", "Agent Platform", "Gemini API", "Generative Language"],
        "include_partner_models": True,
        "excluded_sku_patterns": list(MANDATORY_EXCLUDED_SKU_PATTERNS),
        "fx_sources": ["https://open.er-api.com/v6/latest/USD"],
    },
}


def repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def valid_project(value) -> bool:
    return isinstance(value, str) and bool(PROJECT_RE.match(value))


def valid_dataset(value) -> bool:
    return isinstance(value, str) and bool(DATASET_RE.match(value))


def valid_table(value) -> bool:
    return isinstance(value, str) and bool(TABLE_RE.match(value))


def valid_email(value) -> bool:
    return isinstance(value, str) and len(value) <= 320 and bool(EMAIL_RE.match(value.strip()))


def workspace_configured(cfg: dict) -> bool:
    pid = ((cfg or {}).get("workspace") or {}).get("project_id") or ""
    return valid_project(pid) and pid not in PLACEHOLDER_PROJECTS


def quota_project(cfg: dict) -> str:
    ws = (cfg or {}).get("workspace") or {}
    return ws.get("quota_project_id") or ws.get("project_id") or ""


def is_public_ip(host: str) -> bool:
    """Checks that host does not resolve to loopback, private, link-local, or reserved IPs (SSRF guard)."""
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def valid_https_url(url: str, *, check_dns: bool = False) -> bool:
    if not isinstance(url, str) or len(url) > 2048 or CONTROL_RE.search(url):
        return False
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host in ("localhost", "metadata.google.internal") or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    except ValueError:
        if "." not in host:
            return False
    if check_dns and not is_public_ip(host):
        return False
    return True


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _parse_bool(raw: str) -> bool:
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def _load_yaml_file(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        import yaml  # type: ignore
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except ImportError:
        LOG.debug("PyYAML not installed; skipping YAML file %s and using defaults + env", path)
        return {}
    except Exception as err:
        raise ValueError("Invalid YAML in %s: %s" % (path, err))


def _apply_env(cfg: dict, env: dict) -> dict:
    out = copy.deepcopy(cfg)

    def get(name: str):
        val = env.get(name)
        return val.strip() if isinstance(val, str) and val.strip() != "" else None

    if get("HOST"):
        out["server"]["host"] = get("HOST")
    if get("PORT"):
        out["server"]["port"] = int(get("PORT"))
    if get("CACHE_TTL_SECONDS"):
        out["server"]["cache_ttl_seconds"] = max(0, int(get("CACHE_TTL_SECONDS")))

    ws = out["workspace"]
    if get("PROJECT_ID"):
        ws["project_id"] = get("PROJECT_ID").lower()
    if get("SCAN_PROJECTS"):
        ws["projects"] = [p.lower() for p in _split_csv(get("SCAN_PROJECTS")) if valid_project(p.lower())]
    if env.get("SCAN_ALL_PROJECTS") is not None and env.get("SCAN_ALL_PROJECTS") != "":
        ws["scan_all_projects"] = _parse_bool(env["SCAN_ALL_PROJECTS"])
    if get("QUOTA_PROJECT_ID"):
        ws["quota_project_id"] = get("QUOTA_PROJECT_ID").lower()
    if get("WORKSPACE_TIMEZONE") and valid_timezone(get("WORKSPACE_TIMEZONE")):
        ws["timezone"] = get("WORKSPACE_TIMEZONE")
    if get("AUDIT_DATASET_ID"):
        ws["audit_dataset"] = get("AUDIT_DATASET_ID")
    if get("AUDIT_PROJECT_ID"):
        ws["audit_project"] = get("AUDIT_PROJECT_ID").lower()
    if get("SCOPE_TYPE") in ("projects", "folder", "organization"):
        ws["scope_type"] = get("SCOPE_TYPE")
    if get("SCOPE_IDS"):
        ws["scope_ids"] = [s for s in _split_csv(get("SCOPE_IDS")) if SCOPE_ID_RE.match(s)]
    if get("SCOPE_MAX_PROJECTS"):
        ws["scope_max_projects"] = max(1, min(5000, int(get("SCOPE_MAX_PROJECTS"))))
    if get("SCOPE_FILTER") in ("none", "gemini_enterprise"):
        ws["scope_filter"] = get("SCOPE_FILTER")
    if get("DISCOVERY_ENGINE_LOCATIONS"):
        ws["discovery_engine_locations"] = [loc.lower() for loc in _split_csv(get("DISCOVERY_ENGINE_LOCATIONS"))
                                            if LOCATION_RE.match(loc.lower())]
    bil = ws["billing"]
    if get("BILLING_PROJECT_ID"):
        bil["project_id"] = get("BILLING_PROJECT_ID").lower()
    if get("BILLING_DATASET_ID"):
        bil["dataset"] = get("BILLING_DATASET_ID")
    if get("BILLING_TABLE_ID"):
        bil["table"] = get("BILLING_TABLE_ID")
    if get("BILLING_ACCOUNT_ID"):
        acct = get("BILLING_ACCOUNT_ID").upper()
        if ACCOUNT_RE.match(acct):
            bil["account_id"] = acct

    st = out["storage"]
    if get("STORAGE_BACKEND") in ("auto", "gcs", "file", "memory"):
        st["backend"] = get("STORAGE_BACKEND")
    if get("SETTINGS_BUCKET"):
        st["gcs_bucket"] = get("SETTINGS_BUCKET")
    if get("SETTINGS_GCS_PREFIX"):
        st["gcs_prefix"] = get("SETTINGS_GCS_PREFIX").strip("/")
    if get("STORAGE_FILE_PATH"):
        st["file_path"] = get("STORAGE_FILE_PATH")

    if get("BIGQUERY_MAX_BYTES_BILLED"):
        out["bigquery"]["max_bytes_billed"] = max(10_000_000, int(get("BIGQUERY_MAX_BYTES_BILLED")))

    auth = out["auth"]
    if get("PUBLIC_BASE_URL"):
        auth["public_base_url"] = get("PUBLIC_BASE_URL").rstrip("/")
    if get("OIDC_ALLOWED_SERVICE_ACCOUNTS"):
        auth["oidc_allowed_service_accounts"] = [e.lower() for e in _split_csv(get("OIDC_ALLOWED_SERVICE_ACCOUNTS"))
                                                 if valid_email(e)]
    if env.get("ALLOW_LOCAL_INSECURE") is not None and env.get("ALLOW_LOCAL_INSECURE") != "":
        auth["allow_local_insecure"] = _parse_bool(env["ALLOW_LOCAL_INSECURE"])

    caps = out["caps"]
    if get("USAGE_SOURCE") in ("auto", "bigquery", "logging"):
        caps["usage_source"] = get("USAGE_SOURCE")
    if get("ENFORCEMENT_MODE") in ENFORCEMENT_MODES:
        caps["enforcement_mode"] = get("ENFORCEMENT_MODE")
    if get("ENFORCEMENT_BACKEND") in ENFORCEMENT_BACKENDS:
        caps["enforcement_backend"] = get("ENFORCEMENT_BACKEND")
    if get("DENY_POLICY_ID") and DENY_POLICY_ID_RE.match(get("DENY_POLICY_ID")):
        caps["deny_policy_id"] = get("DENY_POLICY_ID")
    if get("DENY_ATTACHMENT") in ("projects", "scope"):
        caps["deny_attachment"] = get("DENY_ATTACHMENT")
    if get("ENFORCEMENT_JOB") and ENFORCEMENT_JOB_RE.match(get("ENFORCEMENT_JOB")):
        caps["enforcement_job"] = get("ENFORCEMENT_JOB")
    if get("USAGE_EVENTS_SUBSCRIPTION"):
        caps["usage_events_subscription"] = get("USAGE_EVENTS_SUBSCRIPTION")
    if env.get("SPEND_CAPS_ENABLED") is not None and env.get("SPEND_CAPS_ENABLED") != "":
        caps["spend_caps_enabled"] = _parse_bool(env["SPEND_CAPS_ENABLED"])
    if get("CAP_CURRENCY") and CURRENCY_RE.match(get("CAP_CURRENCY").upper()):
        caps["currency"] = get("CAP_CURRENCY").upper()
        out["pricing"]["currency"] = get("CAP_CURRENCY").upper()

    return out


def finalize(cfg: dict) -> dict:
    out = copy.deepcopy(cfg)
    excluded = [str(p).strip().lower() for p in (out.get("pricing") or {}).get("excluded_sku_patterns") or [] if p]
    for mandatory in MANDATORY_EXCLUDED_SKU_PATTERNS:
        if mandatory not in excluded:
            excluded.append(mandatory)
    out["pricing"]["excluded_sku_patterns"] = excluded
    if not valid_timezone(out["workspace"].get("timezone")):
        out["workspace"]["timezone"] = "UTC"
    if out["caps"].get("period") not in PERIODS:
        out["caps"]["period"] = "monthly"
    if out["caps"].get("window") not in WINDOWS:
        out["caps"]["window"] = "calendar"
    if not RESET_TIME_RE.match(str(out["caps"].get("reset_time") or "")):
        out["caps"]["reset_time"] = "00:00"
    return out


def load_config(environ=None, config_path: str | None = None) -> dict:
    env = os.environ if environ is None else environ
    cfg = copy.deepcopy(DEFAULTS)
    root = repo_root()
    files = [config_path] if config_path else [
        os.path.join(root, "config.yaml"),
        os.path.join(root, "config.local.yaml"),
    ]
    for path in files:
        if path:
            layer = _load_yaml_file(path)
            if layer:
                cfg = deep_merge(cfg, layer)
    cfg = _apply_env(cfg, env)
    if env.get("K_SERVICE") and cfg["auth"].get("allow_local_insecure"):
        raise ValueError("ALLOW_LOCAL_INSECURE is refused on Cloud Run")
    return finalize(cfg)
