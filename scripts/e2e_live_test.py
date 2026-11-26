#!/usr/bin/env python3
"""Live end-to-end verification against a real Google Cloud project (for example `duleetest`).

What it proves, with real Gemini Platform calls and real IAM Deny v2 policies:

  A. User cap      a test identity exceeds its request cap -> POST /v1/caps/enforce suspends it -> model calls
                   return 403 -> POST /v1/users/{id}/unblock restores access.
  B. Group pool    the identity is a member of a group with a shared-pool cap that is exhausted -> enforce suspends
                   it (reason group:<id>) -> 403 -> adding it to the group's exclusions restores access immediately.
  C. Group block   POST /v1/groups/{id}/block -> 403 -> POST /v1/groups/{id}/unblock -> access restored.

The test identity is a dedicated service account (the API must run with SERVICE_ACCOUNT_SUBJECTS=true). Calls are made
with an impersonated access token of that service account and carry the per-user header label, so attribution is
exact. Every step, latency (log ingestion, IAM Deny propagation, restore) and API response is written to a Markdown
report. The original cap policy is restored and every test object is removed at the end (also on failure).

Requirements: Python 3.10+, the gcloud CLI logged in as an operator who can impersonate the test service account
(roles/iam.serviceAccountTokenCreator on it). No third-party Python packages are needed.

Example:
  python3 scripts/e2e_live_test.py --api-url http://127.0.0.1:8080 --admin-key "$BOOTSTRAP_ADMIN_KEY" \
      --project duleetest --test-sa caps-e2e-tester@duleetest.iam.gserviceaccount.com
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HEADER_LABEL = "x-goog-agent-platform-observability-label-user"


class Run:
    def __init__(self, args):
        self.a = args
        self.steps: list[dict] = []
        self.started = time.time()
        self._sa_token = None
        self._sa_token_at = 0.0
        self.model = args.model
        self.cleanup: list = []

    # -- reporting -----------------------------------------------------------------
    def step(self, name: str, ok: bool, detail: str = "", **data):
        rec = {"step": name, "result": "PASS" if ok else "FAIL", "detail": detail, "t": round(time.time() - self.started, 1)}
        rec.update(data)
        self.steps.append(rec)
        print("[%s] %-48s %s %s" % (rec["result"], name, detail, json.dumps(data, default=str) if data else ""), flush=True)
        if not ok and self.a.stop_on_fail:
            raise SystemExit(2)
        return ok

    def info(self, msg: str):
        print("      " + msg, flush=True)

    # -- API -----------------------------------------------------------------------
    def api(self, method: str, path: str, body=None, expect=(200, 201)):
        url = self.a.api_url.rstrip("/") + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + self.a.admin_key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode() or "{}"
                return resp.status, json.loads(raw)
        except urllib.error.HTTPError as err:
            raw = err.read().decode() or "{}"
            try:
                return err.code, json.loads(raw)
            except ValueError:
                return err.code, {"raw": raw[:500]}

    def q(self, value: str) -> str:
        return urllib.parse.quote(value, safe="@")

    # -- model calls as the test identity ------------------------------------------
    def sa_token(self) -> str:
        if self._sa_token and time.time() - self._sa_token_at < 1800:
            return self._sa_token
        out = subprocess.run(["gcloud", "auth", "print-access-token", "--impersonate-service-account=" + self.a.test_sa],
                             capture_output=True, text=True, check=False)
        if out.returncode != 0:
            raise SystemExit("Cannot impersonate %s: %s" % (self.a.test_sa, out.stderr.strip()[-400:]))
        self._sa_token = out.stdout.strip().splitlines()[-1]
        self._sa_token_at = time.time()
        return self._sa_token

    def call_model(self) -> int:
        loc = self.a.location
        host = "aiplatform.googleapis.com" if loc == "global" else "%s-aiplatform.googleapis.com" % loc
        url = "https://%s/v1/projects/%s/locations/%s/publishers/google/models/%s:generateContent" % (
            host, self.a.project, loc, self.model)
        body = {"contents": [{"role": "user", "parts": [{"text": "Reply with the single word OK."}]}],
                "generationConfig": {"maxOutputTokens": 8}}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
            "Authorization": "Bearer " + self.sa_token(), "Content-Type": "application/json",
            HEADER_LABEL: self.a.test_sa})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp.read()
                return resp.status
        except urllib.error.HTTPError as err:
            if err.code not in (403, 429):
                self.info("model call HTTP %d: %s" % (err.code, err.read().decode()[:300]))
            return err.code
        except urllib.error.URLError as err:
            self.info("model call network error: %s" % err)
            return 0

    def wait_model(self, want: int, timeout: int, label: str) -> tuple[bool, float, int]:
        t0 = time.time()
        last = None
        while time.time() - t0 < timeout:
            last = self.call_model()
            if last == want:
                return True, round(time.time() - t0, 1), last
            time.sleep(self.a.poll_seconds)
        return False, round(time.time() - t0, 1), last or 0

    # -- helpers -------------------------------------------------------------------
    def user_cap(self) -> dict:
        st, body = self.api("GET", "/v1/caps/users/%s" % self.q(self.a.test_sa))
        return body if st == 200 else {}

    def wait_usage(self, minimum: int) -> tuple[bool, float, int]:
        t0 = time.time()
        seen = 0
        while time.time() - t0 < self.a.log_wait:
            seen = int(((self.user_cap().get("usage") or {}).get("requests")) or 0)
            if seen >= minimum:
                return True, round(time.time() - t0, 1), seen
            time.sleep(20)
        return False, round(time.time() - t0, 1), seen

    def pick_model(self):
        if self.model:
            return
        st, page = self.api("GET", "/v1/models?publisher=google&limit=500")
        rows = page.get("data") or [] if st == 200 else []
        def ok(m):
            mid = str(m.get("id") or "")
            lc = m.get("lifecycle") if isinstance(m.get("lifecycle"), dict) else {}
            stage = str(lc.get("stage") or m.get("launch_stage") or "").upper()
            return ("flash" in mid and mid.startswith("gemini-")
                    and not any(x in mid for x in ("image", "tts", "audio", "live", "embedding", "omni", "preview", "exp", "lite"))
                    and stage in ("GA", ""))

        cands = sorted((m for m in rows if ok(m)), key=lambda m: str(m.get("id")), reverse=True)
        if not cands:
            def fallback_ok(m):
                mid = str(m.get("id") or "")
                lc = m.get("lifecycle") if isinstance(m.get("lifecycle"), dict) else {}
                stage = str(lc.get("stage") or m.get("launch_stage") or "").upper()
                return ("flash" in mid and not any(x in mid for x in ("image", "tts", "audio", "live", "embedding", "omni"))
                        and stage in ("GA", ""))
            cands = sorted((m for m in rows if fallback_ok(m)), key=lambda m: str(m.get("id")), reverse=True)
        if not cands:
            raise SystemExit("Could not pick a model from GET /v1/models; pass --model <model-id>.")
        self.model = cands[0]["id"]

    def enforce(self) -> dict:
        st, run = self.api("POST", "/v1/caps/enforce", {"dryRun": False})
        return run if st == 200 else {"error": run, "status": st}


def phase_preflight(r: Run) -> bool:
    st, health = r.api("GET", "/v1/health")
    r.step("API reachable and authenticated", st == 200 and "storage" in health,
           "version=%s storage=%s" % (health.get("version"), (health.get("storage") or {}).get("backend")))
    if st != 200:
        return False
    st, vl = r.api("GET", "/v1/projects/%s/vertex-logging" % r.a.project)
    if st == 200 and not vl.get("enabled"):
        st2, res = r.api("POST", "/v1/projects/%s/vertex-logging/enable" % r.a.project, {})
        r.step("Inference request metadata logging enabled", st2 == 200 and bool(res.get("enabled")),
               str(res.get("action") or res)[:120])
        r.info("logging was just enabled; the first entries can take a few minutes to appear")
    else:
        r.step("Inference request metadata logging enabled", st == 200 and bool(vl.get("enabled")),
               "state=%s" % vl.get("state"))
    r.pick_model()
    r.info("model under test: %s (location %s)" % (r.model, r.a.location))
    code = r.call_model()
    r.step("Test identity can call the model before any cap", code == 200, "HTTP %d" % code)
    st, pol = r.api("GET", "/v1/caps/policy")
    r.original_policy = {k: pol.get(k) for k in ("enforcement_mode", "enforcement_backend", "surge_guard_min",
                                                  "change_budget_min")}
    st, _ = r.api("PUT", "/v1/caps/policy", {"enforcement_mode": "auto", "enforcement_backend": "iam_deny"})
    r.cleanup.append(("restore policy", lambda: r.api("PUT", "/v1/caps/policy", {
        k: v for k, v in r.original_policy.items() if v is not None})))
    r.step("Cap policy set to auto / iam_deny for the test", st == 200, "original=%s" % r.original_policy)
    # Clean slate for the test identity.
    r.api("DELETE", "/v1/caps/users/%s" % r.q(r.a.test_sa))
    r.api("POST", "/v1/users/%s/unblock" % r.q(r.a.test_sa), {"reason": "e2e clean slate"})
    return code == 200


def phase_user_cap(r: Run):
    sa = r.q(r.a.test_sa)
    base = int(((r.user_cap().get("usage") or {}).get("requests")) or 0)
    cap = base + r.a.cap
    st, res = r.api("PUT", "/v1/caps/users/%s" % sa, {"type": "limit", "request_cap": cap, "note": "e2e live test"})
    r.cleanup.append(("delete user cap", lambda: r.api("DELETE", "/v1/caps/users/%s" % sa)))
    r.step("A1 user request cap set", st == 200, "request_cap=%d (baseline %d requests this period)" % (cap, base))
    codes = [r.call_model() for _ in range(r.a.cap + 1)]
    r.step("A2 calls under/over the cap succeed (cap is enforced after logs arrive)",
           all(c == 200 for c in codes), "HTTP %s" % codes)
    ok, secs, seen = r.wait_usage(base + len(codes))
    r.step("A3 usage visible from logs", ok, "requests=%d after %.0fs" % (seen, secs), log_latency_s=secs)
    snap = r.user_cap()
    r.step("A4 status over_cap with utilization >= 100 pct", snap.get("status") == "over_cap",
           "status=%s utilization=%s%% cost=%s" % (snap.get("status"), snap.get("utilization_pct"),
                                                   (snap.get("usage") or {}).get("estimated_cost")))
    run = r.enforce()
    snap = r.user_cap()
    # The 5-minute scheduler or the real-time Pub/Sub path may already have suspended the identity.
    r.step("A5 enforce suspends the identity", r.a.test_sa in ((run.get("plan") or {}).get("suspend") or [])
           or snap.get("status") == "suspended",
           "result=%s gates=%s" % (run.get("result"), [(g["id"], g["status"]) for g in run.get("gates") or []]))
    r.step("A6 IAM Deny entry present", snap.get("status") == "suspended" and bool(snap.get("blocked_services")),
           "status=%s blocked_services=%s" % (snap.get("status"), snap.get("blocked_services")))
    ok, secs, last = r.wait_model(403, r.a.propagation_wait, "deny")
    r.step("A7 model call denied (HTTP 403)", ok, "after %.0fs (last HTTP %d)" % (secs, last), deny_propagation_s=secs)
    r.api("DELETE", "/v1/caps/users/%s" % sa)   # avoid re-suspension by the 5-minute scheduler
    st, res = r.api("POST", "/v1/users/%s/unblock" % sa, {"reason": "e2e restore"})
    r.step("A8 unblock accepted", st == 200 and res.get("status") == "active", "status=%s" % res.get("status"))
    ok, secs, last = r.wait_model(200, r.a.propagation_wait, "restore")
    r.step("A9 model call allowed again", ok, "after %.0fs (last HTTP %d)" % (secs, last), restore_propagation_s=secs)


def phase_group(r: Run):
    sa = r.a.test_sa
    gid = "e2e-%s" % dt.datetime.now(dt.timezone.utc).strftime("%m%d%H%M%S")
    st, g = r.api("POST", "/v1/groups", {"id": gid, "name": "E2E live test", "members": [sa]})
    r.cleanup.append(("delete group", lambda: r.api("DELETE", "/v1/groups/%s" % gid)))
    r.step("B1 group created with the identity as member", st == 201, "group=%s members=%s" % (gid, g.get("member_count")))
    used = int(((r.user_cap().get("usage") or {}).get("requests")) or 0)
    st, res = r.api("PUT", "/v1/caps/groups/%s" % gid, {"type": "limit", "pool": "shared", "request_cap": max(1, used)})
    r.step("B2 shared-pool group cap set at current usage", st == 200, "request_cap=%d" % max(1, used))
    st, gc = r.api("GET", "/v1/caps/groups/%s" % gid)
    r.step("B3 group pool over cap", st == 200 and gc.get("over_cap") is True,
           "pooled_requests=%s utilization=%s%%" % ((gc.get("usage") or {}).get("requests"), gc.get("utilization_pct")))
    run = r.enforce()
    snap = r.user_cap()
    r.step("B4 enforce suspends the member", sa in ((run.get("plan") or {}).get("suspend") or [])
           or snap.get("status") == "suspended", "result=%s" % run.get("result"))
    r.step("B5 reason is the group pool", "group:%s" % gid in (snap.get("over_cap_reasons") or []),
           "reasons=%s status=%s" % (snap.get("over_cap_reasons"), snap.get("status")))
    ok, secs, last = r.wait_model(403, r.a.propagation_wait, "deny")
    r.step("B6 model call denied (HTTP 403)", ok, "after %.0fs" % secs, deny_propagation_s=secs)
    st, res = r.api("POST", "/v1/groups/%s/exclusions" % gid, {"add": [sa]})
    r.step("B7 exclusion (exception) applied", st == 200 and sa in (res.get("excluded") or []),
           "enforcement_applied=%s" % ((res.get("enforcement") or {}).get("applied")))
    ok, secs, last = r.wait_model(200, r.a.propagation_wait, "restore")
    r.step("B8 model call allowed again after exclusion", ok, "after %.0fs" % secs, restore_propagation_s=secs)
    r.api("DELETE", "/v1/caps/groups/%s" % gid)
    if r.a.quick:
        return
    r.api("POST", "/v1/groups/%s/exclusions" % gid, {"remove": [sa]})
    st, res = r.api("POST", "/v1/groups/%s/block" % gid, {"services": ["gemini_platform"], "reason": "e2e"})
    r.step("C1 group block applied", st == 200 and sa in (res.get("members_blocked") or []),
           "members_blocked=%s" % res.get("members_blocked"))
    ok, secs, last = r.wait_model(403, r.a.propagation_wait, "deny")
    r.step("C2 model call denied (HTTP 403)", ok, "after %.0fs" % secs, deny_propagation_s=secs)
    st, res = r.api("POST", "/v1/users/%s/unblock" % r.q(sa), {})
    r.step("C3 user unblock cannot override group block", res.get("remaining_group_blocks") == [gid],
           "remaining_group_blocks=%s" % res.get("remaining_group_blocks"))
    st, res = r.api("POST", "/v1/groups/%s/unblock" % gid, {"reason": "e2e"})
    r.step("C4 group unblock accepted", st == 200, "still_blocked=%s" % res.get("members_still_blocked"))
    ok, secs, last = r.wait_model(200, r.a.propagation_wait, "restore")
    r.step("C5 model call allowed again", ok, "after %.0fs" % secs, restore_propagation_s=secs)


def write_report(r: Run, path: str):
    passed = sum(1 for s in r.steps if s["result"] == "PASS")
    lines = ["# Live E2E report", "",
             "- Date (UTC): %s" % dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
             "- API: %s" % r.a.api_url, "- Project: %s (location %s)" % (r.a.project, r.a.location),
             "- Test identity: %s" % r.a.test_sa, "- Model: %s" % r.model,
             "- Result: %d / %d steps passed" % (passed, len(r.steps)), "",
             "| t (s) | Step | Result | Detail |", "|---:|---|---|---|"]
    for s in r.steps:
        lines.append("| %s | %s | %s | %s |" % (s["t"], s["step"], s["result"], str(s["detail"]).replace("|", "/")))
    lat = {k: [s[k] for s in r.steps if k in s] for k in ("log_latency_s", "deny_propagation_s", "restore_propagation_s")}
    lines += ["", "## Latencies (seconds)", ""] + ["- %s: %s" % (k, v) for k, v in lat.items()]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\nReport written to %s (%d/%d passed)" % (path, passed, len(r.steps)))
    return passed == len(r.steps)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--api-url", default=os.environ.get("CAPS_API_URL", "http://127.0.0.1:8080"))
    p.add_argument("--admin-key", default=os.environ.get("CAPS_ADMIN_KEY") or os.environ.get("BOOTSTRAP_ADMIN_KEY"))
    p.add_argument("--project", default=os.environ.get("PROJECT_ID", "duleetest"))
    p.add_argument("--location", default=os.environ.get("E2E_LOCATION", "global"))
    p.add_argument("--test-sa", default=os.environ.get("E2E_TEST_SA"))
    p.add_argument("--model", default=os.environ.get("E2E_MODEL"), help="model id; default = newest GA flash model from GET /v1/models")
    p.add_argument("--cap", type=int, default=3, help="requests allowed before the cap is exceeded")
    p.add_argument("--log-wait", type=int, default=900, help="max seconds to wait for usage to appear in logs")
    p.add_argument("--propagation-wait", type=int, default=600, help="max seconds to wait for IAM Deny changes")
    p.add_argument("--poll-seconds", type=int, default=15)
    p.add_argument("--skip-group", action="store_true")
    p.add_argument("--quick", action="store_true", help="skip phase C (group block)")
    p.add_argument("--stop-on-fail", action="store_true")
    p.add_argument("--report", default="e2e-report-%s.md" % dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    a = p.parse_args()
    if not a.admin_key:
        p.error("--admin-key (or CAPS_ADMIN_KEY) is required")
    a.test_sa = a.test_sa or "caps-e2e-tester@%s.iam.gserviceaccount.com" % a.project
    r = Run(a)
    try:
        if phase_preflight(r):
            phase_user_cap(r)
            if not a.skip_group:
                phase_group(r)
    except KeyboardInterrupt:
        r.step("interrupted", False)
    finally:
        for name, fn in reversed(r.cleanup):
            try:
                fn()
                r.info("cleanup: %s" % name)
            except Exception as err:  # pylint: disable=broad-except
                r.info("cleanup failed (%s): %s" % (name, err))
    return 0 if write_report(r, a.report) else 1


if __name__ == "__main__":
    sys.exit(main())
