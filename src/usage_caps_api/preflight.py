"""Preflight CLI: environment readiness report without starting the server.

    python3 -m usage_caps_api.preflight --project HUB [--managed-projects a,b] \
        [--scope-type folder|organization --scope-ids 123,456] [--region R] [--dataset D] [--audit-project P] \
        [--vpc-network N --vpc-subnet S] [--runtime-sa EMAIL] [--enforcer-sa EMAIL] \
        [--format text|json|markdown|env] [--lang ko|en] [--categories apis,orgpolicy,...] \
        [--strict|--no-strict] [--install-mode] [--output FILE] [--env-file FILE] [--config FILE]

Credentials: Application Default Credentials through google-auth when installed and working, otherwise a static
access token (GCP_ACCESS_TOKEN or `gcloud auth print-access-token`).

Exit codes: 0 ready (or warnings with --no-strict), 1 warnings only, 2 blocked, 3 the check could not run
(no credentials / invalid arguments). With --install-mode, failures that prepare.sh / deploy.sh fix automatically
(auto_fixable) do not count as blocking.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys

from . import config
from .cache import TTLCache
from .schema import SchemaRegistry
from .services.base import Ctx
from .services.readiness import run_readiness
from .services.readiness_core import CATEGORIES
from .store import DocStore, MemoryBackend

TAGS = {"pass": "[OK]  ", "warn": "[WARN]", "fail": "[FAIL]", "skip": "[SKIP]", "info": "[INFO]"}
OVERALL_TEXT = {
    "ready": ("준비 완료", "READY"),
    "ready_with_warnings": ("경고와 함께 준비됨", "READY WITH WARNINGS"),
    "blocked": ("차단됨 - 조치 필요", "BLOCKED - action required"),
}
_SAFE_ENV_RE = re.compile(r"[^A-Za-z0-9_.:/,@=+-]")


class PreflightApp:
    """Minimal application object compatible with services.base.Ctx (no HTTP server)."""

    def __init__(self, cfg: dict, gcp):
        self.config = cfg
        self.environ = {}
        self.gcp = gcp
        self.registry = SchemaRegistry(gcp)
        self.cache = TTLCache(512)
        self.store = DocStore(MemoryBackend())


def _csv(raw) -> list[str]:
    return [x.strip() for x in str(raw or "").split(",") if x.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m usage_caps_api.preflight",
                                description="Environment readiness preflight for the GCP AI Usage Caps API.")
    p.add_argument("--project", required=True, help="hub (main) project id")
    p.add_argument("--managed-projects", "--link-projects", dest="managed", default="",
                   help="comma-separated managed (spoke) project ids")
    p.add_argument("--scope-type", choices=("projects", "folder", "organization"), default=None)
    p.add_argument("--scope-ids", default="", help="comma-separated numeric folder/organization ids")
    p.add_argument("--region", default=None, help="hub region (Cloud Run, GCS, Artifact Registry)")
    p.add_argument("--dataset", default=None, help="BigQuery audit dataset id")
    p.add_argument("--audit-project", default=None, help="central audit dataset project (default: the hub)")
    p.add_argument("--central-dataset", action="store_true", help="use the hub as the central audit project (default)")
    p.add_argument("--per-project-datasets", action="store_true", help="legacy: one audit dataset per project")
    p.add_argument("--vpc-network", default=None)
    p.add_argument("--vpc-subnet", default=None)
    p.add_argument("--vpc-egress", default=None, choices=("all-traffic", "private-ranges-only"))
    p.add_argument("--runtime-sa", default=None, help="runtime (API) service account email")
    p.add_argument("--enforcer-sa", default=None, help="Enforcer Job service account email")
    p.add_argument("--service-name", default=None)
    p.add_argument("--org-id", default=None)
    p.add_argument("--quota-project", default=None, help="project billed for API reads (default: hub)")
    p.add_argument("--deny-attachment", choices=("projects", "scope"), default=None)
    p.add_argument("--enforcement-backend", choices=config.ENFORCEMENT_BACKENDS, default=None)
    p.add_argument("--discovery-locations", default="", help="extra Discovery Engine locations (comma-separated)")
    p.add_argument("--config", default=None, help="optional YAML config file merged over the defaults")
    p.add_argument("--categories", default="", help="comma-separated subset of: %s" % ",".join(CATEGORIES))
    p.add_argument("--format", choices=("text", "json", "markdown", "env"), default="text")
    p.add_argument("--lang", choices=("ko", "en"), default="ko")
    p.add_argument("--strict", dest="strict", action="store_true", default=None,
                   help="exit 1 on warnings (default from readiness.strict)")
    p.add_argument("--no-strict", dest="strict", action="store_false")
    p.add_argument("--install-mode", action="store_true",
                   help="auto-fixable failures (APIs, sinks, grants done by the installer) are not blocking")
    p.add_argument("--client", choices=("auto", "adc", "token"), default="auto")
    p.add_argument("--output", default=None, help="write the formatted report to FILE")
    p.add_argument("--env-file", default=None, help="also write PREFLIGHT_* shell variables to FILE")
    p.add_argument("--no-script", action="store_true", help="omit the remediation script from text output")
    p.add_argument("--verbose", action="store_true", help="also print details of passing checks")
    return p


def build_config(args) -> dict:
    cfg = copy.deepcopy(config.DEFAULTS)
    if args.config:
        layer = config._load_yaml_file(args.config)  # pylint: disable=protected-access
        cfg = config.deep_merge(cfg, layer)
    ws, hub, caps = cfg["workspace"], cfg["hub"], cfg["caps"]
    ws["project_id"] = args.project.strip().lower()
    ws["scan_all_projects"] = False
    managed = [p.lower() for p in _csv(args.managed) if config.valid_project(p.lower())]
    if managed:
        ws["projects"] = managed
    if args.scope_type:
        ws["scope_type"] = args.scope_type
    if args.scope_ids:
        ws["scope_ids"] = [s for s in _csv(args.scope_ids) if config.SCOPE_ID_RE.match(s)]
    if args.dataset:
        ws["audit_dataset"] = args.dataset
    if args.central_dataset or not getattr(args, "per_project_datasets", False):
        ws["audit_project"] = ws["project_id"]
    if args.audit_project:
        ws["audit_project"] = args.audit_project.strip().lower()
    if args.quota_project:
        ws["quota_project_id"] = args.quota_project.strip().lower()
    if args.discovery_locations:
        ws["discovery_engine_locations"] = [l.lower() for l in _csv(args.discovery_locations)
                                            if config.LOCATION_RE.match(l.lower())] or ["global"]
    for attr, key in (("region", "region"), ("vpc_network", "vpc_network"), ("vpc_subnet", "vpc_subnet"),
                      ("vpc_egress", "vpc_egress"), ("runtime_sa", "runtime_service_account"),
                      ("enforcer_sa", "enforcer_service_account"), ("service_name", "service_name")):
        val = getattr(args, attr)
        if val:
            hub[key] = val.strip()
    if args.org_id and config.SCOPE_ID_RE.match(args.org_id.strip()):
        hub["org_id"] = args.org_id.strip()
    if args.deny_attachment:
        caps["deny_attachment"] = args.deny_attachment
    if args.enforcement_backend:
        caps["enforcement_backend"] = args.enforcement_backend
    cfg["storage"]["backend"] = "memory"
    return config.finalize(cfg)


def make_gcp(mode: str, quota_project: str):
    """(client, label). ADC via google-auth when importable and working, else the static-token client."""
    if mode in ("auto", "adc"):
        try:
            import google.auth  # type: ignore  # noqa: F401
            import requests  # type: ignore  # noqa: F401
            from .gcp import Gcp
            client = Gcp(max_workers=16)
            client.auth_headers(quota_project)
            return client, "adc"
        except Exception:  # pylint: disable=broad-except
            if mode == "adc":
                raise
    from .tokengcp import StaticTokenGcp
    return StaticTokenGcp(max_workers=16), "token"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _title(c: dict, lang: str) -> str:
    return c.get("title_ko") if lang == "ko" else c.get("title_en")


def format_text(report: dict, lang: str = "ko", *, script: bool = True, verbose: bool = False) -> str:
    ko = lang == "ko"
    out = []
    hub = report.get("hub") or {}
    out.append("=" * 78)
    out.append(("GCP AI Usage Caps API - 환경 사전 점검 (Preflight)" if ko else
                "GCP AI Usage Caps API - Environment readiness preflight"))
    out.append("%s: %s (%s)   %s: %s   %s: %s" % (
        "허브" if ko else "Hub", hub.get("project_id"), hub.get("region"),
        "관리 프로젝트" if ko else "Managed projects", hub.get("managed_project_count"),
        "데이터세트" if ko else "Dataset", "%s (%s)" % (hub.get("audit_dataset"), hub.get("dataset_mode"))))
    out.append("=" * 78)
    for proj in report.get("projects") or []:
        out.append("")
        out.append("== %s [%s]%s" % (proj["project_id"], proj["role"],
                                     ("  org %s" % proj["organization_id"]) if proj.get("organization_id") else ""))
        for c in proj.get("checks") or []:
            out.extend(_check_lines(c, lang, verbose=verbose))
    cross = report.get("cross_project") or []
    if cross:
        out.append("")
        out.append("== %s" % ("프로젝트 간 / 전역 점검" if ko else "Cross-project / global checks"))
        for c in cross:
            out.extend(_check_lines(c, lang, project=c.get("project"), verbose=verbose))
    s = report.get("summary") or {}
    out.append("")
    out.append("-" * 78)
    label = OVERALL_TEXT.get(report.get("overall"), ("?", "?"))
    out.append("%s: %s" % ("종합 결과" if ko else "Overall", label[0] if ko else label[1]))
    out.append("OK %d / WARN %d / FAIL %d / SKIP %d / INFO %d   (%s %d, %s %d)" % (
        s.get("pass", 0), s.get("warn", 0), s.get("fail", 0), s.get("skip", 0), s.get("info", 0),
        "차단" if ko else "blocking", s.get("blocking", 0),
        "자동조치불가" if ko else "not auto-fixable", s.get("blocking_unfixable", 0)))
    adapt = report.get("adaptations") or {}
    if adapt:
        out.append("%s: %s" % ("설치 자동 대응" if ko else "Installer adaptations",
                               ", ".join("%s=%s" % (k, v) for k, v in sorted(adapt.items()))))
    if script and report.get("remediation_script") and "Nothing to fix" not in report["remediation_script"]:
        out.append("")
        out.append("-- %s --" % ("조치 스크립트 (검토 후 실행)" if ko else "Remediation script (review before running)"))
        out.append(report["remediation_script"].rstrip())
    out.append("")
    return "\n".join(out)


def _check_lines(c: dict, lang: str, project: str | None = None, verbose: bool = False) -> list[str]:
    head = "  %s %s%s" % (TAGS.get(c["status"], "[?]"), _title(c, lang), (" @%s" % project) if project else "")
    lines = [head + "  (%s)" % c["id"]]
    if c.get("detail") and (verbose or c["status"] != "pass"):
        lines.append("         %s" % c["detail"])
    if c["status"] in ("fail", "warn"):
        for cmd in ((c.get("remediation") or {}).get("commands") or [])[:3]:
            first = cmd.splitlines()[0] if cmd else ""
            lines.append("         $ %s%s" % (first, " ..." if "\n" in cmd else ""))
    return lines


def format_markdown(report: dict, lang: str = "ko") -> str:
    ko = lang == "ko"
    s = report.get("summary") or {}
    label = OVERALL_TEXT.get(report.get("overall"), ("?", "?"))
    out = ["# %s" % ("환경 사전 점검 결과" if ko else "Environment readiness report"), "",
           "- **%s**: %s" % ("종합" if ko else "Overall", label[0] if ko else label[1]),
           "- OK %d / WARN %d / FAIL %d / SKIP %d / INFO %d" % (s.get("pass", 0), s.get("warn", 0), s.get("fail", 0),
                                                                s.get("skip", 0), s.get("info", 0)), ""]

    def table(checks, with_project=False):
        rows = ["| %s | ID | %s |%s" % ("상태" if ko else "Status", "항목" if ko else "Check",
                                        " Project |" if with_project else ""),
                "|---|---|---|%s" % ("---|" if with_project else "")]
        for c in checks:
            title = _title(c, lang).replace("|", "/")
            detail = (c.get("detail") or "").replace("|", "/").replace("\n", " ")
            rows.append("| %s | `%s` | **%s** - %s |%s" % (c["status"].upper(), c["id"], title, detail,
                                                          (" %s |" % (c.get("project") or "")) if with_project else ""))
        return rows
    for proj in report.get("projects") or []:
        out += ["## %s (%s)" % (proj["project_id"], proj["role"]), ""] + table(proj.get("checks") or []) + [""]
    if report.get("cross_project"):
        out += ["## %s" % ("프로젝트 간 점검" if ko else "Cross-project checks"), ""]
        out += table(report["cross_project"], with_project=True) + [""]
    out += ["## %s" % ("조치 스크립트" if ko else "Remediation script"), "", "```bash",
            report.get("remediation_script", "").rstrip(), "```", ""]
    return "\n".join(out)


def env_lines(report: dict) -> list[str]:
    """PREFLIGHT_* shell assignments consumed by scripts/prepare.sh and scripts/deploy.sh."""
    adapt = report.get("adaptations") or {}
    s = report.get("summary") or {}
    hub = report.get("hub") or {}
    values = {
        "PREFLIGHT_OVERALL": report.get("overall", ""),
        "PREFLIGHT_BLOCKING": s.get("blocking", 0),
        "PREFLIGHT_BLOCKING_UNFIXABLE": s.get("blocking_unfixable", 0),
        "PREFLIGHT_WARNINGS": s.get("warn", 0),
        "PREFLIGHT_DRS": 1 if adapt.get("drs") else 0,
        "PREFLIGHT_NO_INVOKER_IAM_CHECK": 1 if adapt.get("no_invoker_iam_check") else 0,
        "PREFLIGHT_INVOKER_IAM_REQUIRED": 1 if adapt.get("invoker_iam_required") else 0,
        "PREFLIGHT_ALLOW_UNAUTHENTICATED": 0 if adapt.get("allow_unauthenticated") is False else 1,
        "PREFLIGHT_INGRESS": adapt.get("ingress") or "",
        "PREFLIGHT_VPC_EGRESS_REQUIRED": 1 if adapt.get("vpc_egress_required") else 0,
        "PREFLIGHT_BINAUTHZ": adapt.get("binary_authorization") or "",
        "PREFLIGHT_DOCKER_BUILD": 1 if adapt.get("docker_build") else 0,
        "PREFLIGHT_VPCSC_RESTRICTED": ",".join(adapt.get("vpcsc_restricted") or []),
        "PREFLIGHT_ORG_ID": hub.get("organization_id") or "",
        "PREFLIGHT_PROJECT_NUMBER": hub.get("project_number") or "",
        "PREFLIGHT_MANAGED_PROJECTS": ",".join(p["project_id"] for p in report.get("projects") or []
                                               if p.get("role") == "managed"),
    }
    adopt_ds, adopt_sinks, billing = "", [], {}
    for proj in report.get("projects") or []:
        for c in proj.get("checks") or []:
            data = c.get("data") or {}
            if c["id"] == "bq.ai_tables.discovered" and data.get("adopt") and not adopt_ds:
                adopt_ds = data["adopt"].get("install_flag", "").replace("--reuse-dataset ", "")
            if c["id"] == "logging.sink.bigquery":
                for a in data.get("adopt") or []:
                    if str(a.get("scope", "")).startswith("projects/"):
                        adopt_sinks.append("%s:%s" % (proj["project_id"], a.get("sink")))
            if c["id"] == "bq.billing_export" and (data.get("adopt") or {}).get("env"):
                billing = data["adopt"]["env"]
    values["PREFLIGHT_ADOPT_DATASET"] = adopt_ds
    values["PREFLIGHT_ADOPT_SINKS"] = ",".join(dict.fromkeys(adopt_sinks))
    values["PREFLIGHT_BILLING_PROJECT_ID"] = billing.get("BILLING_PROJECT_ID", "")
    values["PREFLIGHT_BILLING_DATASET_ID"] = billing.get("BILLING_DATASET_ID", "")
    values["PREFLIGHT_BILLING_TABLE_ID"] = billing.get("BILLING_TABLE_ID", "")
    return ["%s=%s" % (k, _SAFE_ENV_RE.sub("", str(v))) for k, v in values.items()]


def exit_code(report: dict, *, strict: bool, install_mode: bool) -> int:
    s = report.get("summary") or {}
    blocking = s.get("blocking_unfixable", 0) if install_mode else s.get("blocking", 0)
    if blocking:
        return 2
    if s.get("warn") or s.get("fail"):
        return 1 if strict else 0
    return 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not config.valid_project(args.project.strip().lower()):
        print("error: invalid --project", file=sys.stderr)
        return 3
    categories = _csv(args.categories) or None
    if categories and any(c not in CATEGORIES for c in categories):
        print("error: --categories must be a subset of %s" % ",".join(CATEGORIES), file=sys.stderr)
        return 3
    try:
        cfg = build_config(args)
    except ValueError as err:
        print("error: %s" % err, file=sys.stderr)
        return 3
    try:
        gcp, label = make_gcp(args.client, config.quota_project(cfg))
    except Exception as err:  # pylint: disable=broad-except
        print("error: Google credentials unavailable (%s). Run 'gcloud auth login' or set GCP_ACCESS_TOKEN."
              % type(err).__name__, file=sys.stderr)
        return 3
    if label == "token" and not gcp.credentials_ok():
        print("error: no Google access token. Run 'gcloud auth login' (and 'gcloud auth application-default login') "
              "or set GCP_ACCESS_TOKEN.", file=sys.stderr)
        return 3
    ctx = Ctx(PreflightApp(cfg, gcp), cfg, None)
    try:
        report = run_readiness(ctx, categories=categories)
    except Exception as err:  # pylint: disable=broad-except
        print("error: preflight failed: %s" % getattr(err, "message", type(err).__name__), file=sys.stderr)
        return 3
    report["client"] = label
    strict = bool((cfg.get("readiness") or {}).get("strict", True)) if args.strict is None else args.strict
    if args.format == "json":
        text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    elif args.format == "markdown":
        text = format_markdown(report, args.lang)
    elif args.format == "env":
        text = "\n".join(env_lines(report)) + "\n"
    else:
        text = format_text(report, args.lang, script=not args.no_script, verbose=args.verbose)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    if args.env_file:
        with open(args.env_file, "w", encoding="utf-8") as fh:
            fh.write("\n".join(env_lines(report)) + "\n")
        os.chmod(args.env_file, 0o600)
    return exit_code(report, strict=strict, install_mode=args.install_mode)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
