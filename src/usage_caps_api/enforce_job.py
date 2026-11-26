"""Cloud Run Job entrypoint for periodic & event-triggered IAM Deny v2 cap enforcement.

    python -m usage_caps_api.enforce_job [--dry-run] [--trigger scheduler|usage|admin]

Runs as its own dedicated service account (the ONLY identity holding organization-level
`roles/iam.denyAdmin`), evaluates global & per-user spend caps, checks safety gates G1-G7,
applies IAM Deny v2 changes, fires deduplicated webhooks, and prints one PII-free structured
JSON summary line to stdout for Cloud Logging.

Exit codes:
    0  Succeeded (applied, nothing to do, dry run, or cleanly aborted by safety gates)
    1  Failed (an IAM change failed or upstream error occurred; Cloud Run marks execution failed)
    2  Configuration error (workspace unconfigured, invalid arguments, or missing GCS store on Cloud Run Job)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time

from . import __version__, auth, config
from .app import App, _configure_logging
from .gcp import GcpError
from .httpkit import ApiError
from .services import base as svc_base, caps

LOG = logging.getLogger("usage_caps_api.enforce_job")

EXIT_OK, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2
TRIGGERS = ("scheduler", "usage", "admin")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def scrub(text: str, limit: int = 300) -> str:
    return _EMAIL_RE.sub("<email>", str(text or ""))[:limit]


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="python -m usage_caps_api.enforce_job",
                                     description="Run one GCP AI spend cap enforcement cycle.")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate plan and gates without applying IAM changes")
    parser.add_argument("--trigger", choices=TRIGGERS, default="scheduler", help="Execution trigger label")
    return parser.parse_args(argv)


def job_caller() -> auth.Caller:
    return auth.Caller(
        id="enforcement-job",
        name="Cloud Run Enforcement Job",
        auth_type="job",
        scopes=frozenset(auth.ALL_SCOPES),
        rate_key="job:enforcer",
    )


def check_ready(app: App, environ: dict):
    if environ.get("CLOUD_RUN_JOB") and app.store.kind != "gcs":
        raise RuntimeError("SETTINGS_BUCKET (GCS storage) is required when running as a Cloud Run Job.")
    if not config.workspace_configured(app.config):
        raise RuntimeError("Workspace PROJECT_ID is not configured.")


def summarize(out: dict) -> dict:
    plan = out.get("plan") if isinstance(out.get("plan"), dict) else {}
    changes = [c for c in (out.get("changes") or []) if isinstance(c, dict)]

    def count_applied(action: str) -> int:
        return sum(1 for c in changes if c.get("action") == action and c.get("status") == "applied")

    return {
        "mode": out.get("mode"),
        "backend": out.get("backend"),
        "enforcementMode": out.get("enforcement_mode"),
        "executed": bool(out.get("executed")),
        "result": out.get("result") or "success",
        "suspended": count_applied("suspend"),
        "blocked": count_applied("block"),
        "restored": count_applied("restore"),
        "failed": sum(1 for c in changes if c.get("status") == "failed"),
        "deferred": len(plan.get("deferred") or []) + len(plan.get("capacity_held") or []),
        "review": len(plan.get("review") or []),
        "aborted": bool(plan.get("aborted")),
    }


def main(argv=None, *, app: App | None = None, environ=None) -> int:
    started = time.monotonic()
    _configure_logging()
    env = os.environ if environ is None else environ
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return EXIT_OK if not exc.code else EXIT_CONFIG

    summary: dict = {
        "trigger": args.trigger,
        "mode": "dry_run" if args.dry_run else "apply",
        "version": __version__,
        "execution": env.get("CLOUD_RUN_EXECUTION") or None,
    }
    try:
        app = app or App(environ=env)
        check_ready(app, env)
        ctx = svc_base.Ctx(app, app.config, job_caller())
        ctx.deny_quota_patience = True
        out = caps.enforce_cycle(ctx, trigger=args.trigger, dry_run=args.dry_run, use_job_if_configured=False)
        summary.update(summarize(out))
        from .services import governance_cycle
        summary["governance"] = governance_cycle.run(ctx, dry_run=args.dry_run, trigger=args.trigger)
        exit_code = EXIT_FAILED if (summary.get("failed") or summary.get("result") == "failed") else EXIT_OK
    except RuntimeError as err:
        summary.update(result="configuration_error", error=scrub(str(err)))
        exit_code = EXIT_CONFIG
    except ApiError as err:
        summary.update(result="error", error=scrub(err.message))
        exit_code = EXIT_CONFIG if err.code in ("bad_request", "not_configured") else EXIT_FAILED
    except GcpError as err:
        summary.update(result="error", error="GCP API error (%s)" % err.kind)
        exit_code = EXIT_FAILED
    except Exception as err:  # pylint: disable=broad-except
        LOG.exception("enforcement job crashed")
        summary.update(result="error", error=type(err).__name__)
        exit_code = EXIT_FAILED

    summary["exitCode"] = exit_code
    summary["durationMs"] = int((time.monotonic() - started) * 1000)
    summary["severity"] = "ERROR" if exit_code else ("WARNING" if summary.get("aborted") else "INFO")
    summary["message"] = "GCP AI spend cap enforcement job finished: %s" % summary.get("result")
    sys.stdout.write(json.dumps(summary, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
