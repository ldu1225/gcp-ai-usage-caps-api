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

VALID_SERVICES = ("antigravity", "gemini_platform", "gemini_enterprise")
# `gemini_platform` = Gemini Platform (formerly Vertex AI / Agent Platform). The legacy surface id
# `vertex_ai` is still accepted on input and in stored documents and is normalized to `gemini_platform`.
SERVICE_ALIASES = {"vertex_ai": "gemini_platform", "vertex": "gemini_platform", "agent_platform": "gemini_platform"}


def normalize_service(value):
    s = str(value or "").strip().lower()
    return SERVICE_ALIASES.get(s, s)


def normalize_services(values) -> list:
    out = []
    for v in values or ():
        n = normalize_service(v)
        if n and n not in out:
            out.append(n)
    return out
ENFORCEMENT_MODES = ("manual", "auto")
ENFORCEMENT_BACKENDS = ("iam_deny", "notify_only")
MODEL_VIOLATION_ACTIONS = ("block_service", "notify")
MODEL_POLICY_SCOPE_TYPES = ("projects", "folders", "organizations")
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
        # Only the billing *account id* is used (project hard spend caps via the Budget API). Costs are never read
        # from a Cloud Billing export: usage comes from logs, prices from the official Cloud Billing Catalog SKUs.
        "billing": {
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
        "oidc_internal_service_accounts": [],
        "allow_local_insecure": False,
    },
    "caps": {
        "usage_source": "auto",
        "inference_log": "businessaicode.googleapis.com%2Finference_response",
        "vertex_inference_log": "aiplatform.googleapis.com%2Finference_request",
        "vertex_user_header_label": "user",
        "vertex_logging_endpoint": "https://aiplatform.googleapis.com/v1beta1",
        "cap_scope": "total",
        "project_defaults": {},
        "metered_surfaces": list(VALID_SERVICES),
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
        "catalog_services": ["Gemini Platform", "Vertex AI", "Agent Platform", "Gemini API", "Generative Language"],
        "include_partner_models": True,
        "excluded_sku_patterns": list(MANDATORY_EXCLUDED_SKU_PATTERNS),
        "fx_sources": ["https://open.er-api.com/v6/latest/USD"],
        "long_context_threshold_tokens": 200000,
        "provisioned_throughput_cost": "on_demand",
    },
    # Facts about the central (hub) deployment that governs every managed project.
    "hub": {
        "region": "asia-northeast3",
        "service_name": "gcp-ai-usage-caps-api",
        "runtime_service_account": "",
        "enforcer_service_account": "",
        # Opt-in: treat service accounts as policy subjects (usage, caps, rules, IAM Deny). The hub's own runtime and
        # enforcer service accounts are always protected.
        "service_account_subjects": False,
        "org_id": "",
        "vpc_network": "",
        "vpc_subnet": "",
        "vpc_egress": "",
        "usage_events_topic": "",
        "state_bucket": "",
    },
    # Model governance (catalog, access policies, per-user model rules).
    "models": {
        "violation_action": "block_service",
        "violation_block_minutes": 0,
        "violation_notify": True,
        "violation_block_scope": "project",
        "policy_scopes": ["projects"],
        "allow_org_policy_writes": True,
        "default_actions": ["predict"],
        "always_allow": ["publishers/anthropic/models/count-tokens"],
        # Model Garden *publishers* (vendors) scanned for the live catalog. This is NOT a model list: models are
        # discovered live, and any publisher seen in usage logs is added to the scan automatically.
        "publishers": ["google", "anthropic", "meta", "mistralai", "ai21", "deepseek-ai", "qwen", "openai"],
        "catalog_location": "global",
        "catalog_ttl_seconds": 3600,
        "max_policy_values": 500,
        "protected_principals": [],
    },
    # Quota / rate-limit management through the Cloud Quotas API.
    "quotas": {
        "service": "aiplatform.googleapis.com",
        "contact_email": "",
        "headroom_warn_pct": 80,
        "max_projects_per_call": 50,
    },
    # Environment readiness diagnostics (preflight).
    "readiness": {
        "cache_ttl_seconds": 900,
        "strict": True,
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


def _is_public_address(ip) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(
        ip.is_global
        and not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                 or ip.is_multicast or ip.is_unspecified)
    )


def is_public_ip(host: str) -> bool:
    """Checks that host does not resolve to loopback, private, link-local, CGNAT, or reserved IPs (SSRF guard)."""
    clean = str(host or "").strip("[]").rstrip(".")
    if not clean:
        return False
    try:
        return _is_public_address(ipaddress.ip_address(clean))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(clean, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            return False
        if not _is_public_address(ip):
            return False
    return True


def valid_https_url(url: str, *, check_dns: bool = False) -> bool:
    if not isinstance(url, str) or len(url) > 2048 or CONTROL_RE.search(url):
        return False
    try:
        parsed = urllib.parse.urlparse(url)
        _ = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    host = parsed.hostname.strip("[]").rstrip(".").lower()
    if not host or host in ("localhost", "metadata.google.internal", "metadata") \
            or host.endswith(".local") or host.endswith(".internal") or host.endswith(".localhost"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        if not _is_public_address(ip):
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
    if get("OIDC_INTERNAL_SERVICE_ACCOUNTS"):
        auth["oidc_internal_service_accounts"] = [e.lower() for e in _split_csv(get("OIDC_INTERNAL_SERVICE_ACCOUNTS"))
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
    if get("CAP_SCOPE") in ("total", "per_project"):
        caps["cap_scope"] = get("CAP_SCOPE")
    if get("VERTEX_USER_HEADER_LABEL"):
        caps["vertex_user_header_label"] = re.sub(r"[^a-z0-9_\-]", "", get("VERTEX_USER_HEADER_LABEL").lower())[:40] or "user"
    if get("VERTEX_LOGGING_ENDPOINT") and re.match(r"^https://[a-z0-9.\-]+(:[0-9]+)?/v1beta1$", get("VERTEX_LOGGING_ENDPOINT")):
        caps["vertex_logging_endpoint"] = get("VERTEX_LOGGING_ENDPOINT")
    if get("METERED_SURFACES"):
        ms = [x for x in _split_csv(get("METERED_SURFACES")) if x in VALID_SERVICES]
        if ms:
            caps["metered_surfaces"] = ms
    if get("CAP_CURRENCY") and CURRENCY_RE.match(get("CAP_CURRENCY").upper()):
        caps["currency"] = get("CAP_CURRENCY").upper()
        out["pricing"]["currency"] = get("CAP_CURRENCY").upper()

    hub = out["hub"]
    for env_name, key in (("HUB_REGION", "region"), ("SERVICE_NAME", "service_name"),
                          ("RUNTIME_SERVICE_ACCOUNT", "runtime_service_account"),
                          ("ENFORCER_SERVICE_ACCOUNT", "enforcer_service_account"),
                          ("VPC_NETWORK", "vpc_network"), ("VPC_SUBNET", "vpc_subnet"),
                          ("VPC_EGRESS", "vpc_egress"), ("USAGE_EVENTS_TOPIC", "usage_events_topic"),
                          ("SETTINGS_BUCKET", "state_bucket")):
        if get(env_name):
            hub[key] = get(env_name)
    if get("SERVICE_ACCOUNT_SUBJECTS"):
        hub["service_account_subjects"] = _parse_bool(get("SERVICE_ACCOUNT_SUBJECTS"))
    if get("ORG_ID") and SCOPE_ID_RE.match(get("ORG_ID")):
        hub["org_id"] = get("ORG_ID")

    models = out["models"]
    if get("MODEL_VIOLATION_ACTION") in MODEL_VIOLATION_ACTIONS:
        models["violation_action"] = get("MODEL_VIOLATION_ACTION")
    if get("MODEL_VIOLATION_BLOCK_MINUTES"):
        models["violation_block_minutes"] = max(0, min(525600, int(get("MODEL_VIOLATION_BLOCK_MINUTES"))))
    if get("MODEL_POLICY_SCOPES"):
        scopes = [s for s in _split_csv(get("MODEL_POLICY_SCOPES")) if s in MODEL_POLICY_SCOPE_TYPES]
        if scopes:
            models["policy_scopes"] = scopes
    if env.get("ALLOW_ORG_POLICY_WRITES") is not None and env.get("ALLOW_ORG_POLICY_WRITES") != "":
        models["allow_org_policy_writes"] = _parse_bool(env["ALLOW_ORG_POLICY_WRITES"])
    if get("MODEL_VIOLATION_BLOCK_SCOPE") in ("project", "all"):
        models["violation_block_scope"] = get("MODEL_VIOLATION_BLOCK_SCOPE")
    if get("MODEL_PUBLISHERS"):
        pubs = [x.lower() for x in _split_csv(get("MODEL_PUBLISHERS")) if re.match(r"^[a-z0-9][a-z0-9\-]{0,62}$", x.lower())]
        if pubs:
            models["publishers"] = pubs
    if get("MODEL_PROTECTED_PRINCIPALS"):
        models["protected_principals"] = [e.lower() for e in _split_csv(get("MODEL_PROTECTED_PRINCIPALS"))]

    pricing_cfg = out["pricing"]
    if get("LONG_CONTEXT_THRESHOLD_TOKENS"):
        pricing_cfg["long_context_threshold_tokens"] = max(1000, int(get("LONG_CONTEXT_THRESHOLD_TOKENS")))
    if get("PROVISIONED_THROUGHPUT_COST") in ("on_demand", "zero"):
        pricing_cfg["provisioned_throughput_cost"] = get("PROVISIONED_THROUGHPUT_COST")

    quotas = out["quotas"]
    if get("QUOTA_CONTACT_EMAIL") and valid_email(get("QUOTA_CONTACT_EMAIL")):
        quotas["contact_email"] = get("QUOTA_CONTACT_EMAIL")

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
