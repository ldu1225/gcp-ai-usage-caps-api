"""Readiness categories `bigquery` and `logging`: datasets, existing AI log tables (adoption), freshness, billing
export auto-detection, Log Router sinks (project + aggregated org/folder sinks, adoption, writer access, disabled
sinks), the `_Default` sink, and Data Access audit log configuration for Vertex AI.

Adoption-first: when a customer environment already routes the needed logs somewhere (an existing dataset holding
`businessaicode_googleapis_com_inference_response*` tables, an aggregated organization sink, a Pub/Sub sink), the checks
recommend reusing it with the exact config values / install flags instead of creating duplicates.
"""

from __future__ import annotations

import time
import urllib.parse

from .readiness_core import (BQ_V2, CRM_V1, CRM_V3, DOCS, LOGGING_V2, PUBSUB_V1, check, cmd_enable, q, reason_of,
                             skipped)

AI_TABLES = {
    "antigravity": ("businessaicode_googleapis_com_inference_response",),
    "vertex_ai": ("cloudaudit_googleapis_com_data_access",),
    "gemini_enterprise": ("discoveryengine_googleapis_com_gen_ai_user_message",
                          "discoveryengine_googleapis_com_gemini_enterprise_user_activity"),
}
NEEDED_LOGS = {
    "antigravity": ("businessaicode.googleapis.com/inference_response",),
    "vertex_ai": ("cloudaudit.googleapis.com/data_access",),
    "gemini_enterprise": ("discoveryengine.googleapis.com/gen_ai_user_message",
                          "discoveryengine.googleapis.com/gemini_enterprise_user_activity"),
}
BILLING_PREFIXES = ("gcp_billing_export_resource_v1_", "gcp_billing_export_v1_")
FRESH_DAYS = 7
MAX_DATASETS = 50
MAX_SINKS_ACCESS = 10
WRITER_DATASET_ROLES = ("WRITER", "OWNER", "roles/bigquery.dataEditor", "roles/bigquery.dataOwner",
                        "roles/bigquery.admin")
WRITER_PROJECT_ROLES = ("roles/bigquery.dataEditor", "roles/bigquery.dataOwner", "roles/bigquery.admin",
                        "roles/editor", "roles/owner")
PUBLISHER_ROLES = ("roles/pubsub.publisher", "roles/pubsub.editor", "roles/pubsub.admin", "roles/editor",
                   "roles/owner")

AI_LOG_FILTER = (
    'log_id("businessaicode.googleapis.com/inference_response") OR '
    '(log_id("cloudaudit.googleapis.com/data_access") AND protoPayload.serviceName="aiplatform.googleapis.com") OR '
    'log_id("discoveryengine.googleapis.com/gemini_enterprise_user_activity") OR '
    'log_id("discoveryengine.googleapis.com/gen_ai_user_message")'
)
REALTIME_FILTER = 'log_id("businessaicode.googleapis.com/inference_response")'


def sink_name(run) -> str:
    return "%s-ai-logs" % run.service_name


def dataset_projects(run) -> list[str]:
    """Projects that host the audit dataset: the central audit project, or every project (legacy mode)."""
    return [run.dataset_project] if run.central else list(run.projects)


# ---------------------------------------------------------------------------
# BigQuery helpers
# ---------------------------------------------------------------------------

def _list_datasets(run, pid: str):
    items, _ = run.paginate("%s/projects/%s/datasets" % (BQ_V2, urllib.parse.quote(pid, safe="")), "datasets",
                            params={"maxResults": 200}, max_pages=2, project_hint=pid)
    names = [str((d.get("datasetReference") or {}).get("datasetId") or "") for d in items]
    return [n for n in names if n]


def _list_tables(run, pid: str, dataset: str) -> list[str]:
    items, _ = run.paginate("%s/projects/%s/datasets/%s/tables" % (BQ_V2, urllib.parse.quote(pid, safe=""),
                                                                   urllib.parse.quote(dataset, safe="")),
                            "tables", params={"maxResults": 1000}, max_pages=3, project_hint=pid)
    return [str((t.get("tableReference") or {}).get("tableId") or "") for t in items if t.get("tableReference")]


def _classify(tables: list[str]) -> dict:
    """{kind: latest_table_id} for AI log tables (sharded `_YYYYMMDD` or partitioned)."""
    out = {}
    for kind, prefixes in AI_TABLES.items():
        hits = [t for t in tables if any(t == p or t.startswith(p + "_") for p in prefixes)]
        if hits:
            out[kind] = sorted(hits)[-1]
    return out


def scan_project_tables(run, pid: str):
    """({dataset: {kind: latest_table}}, {dataset: [billing tables]}, err). Memoized per project."""
    cached = run.fact(pid, "ai_tables")
    if cached is not None:
        return cached, run.fact(pid, "billing_tables") or {}, None
    try:
        datasets = _list_datasets(run, pid)
    except Exception as err:  # pylint: disable=broad-except
        return None, None, err
    datasets = sorted(datasets, key=lambda n: (0 if (n == run.dataset or "billing" in n.lower()) else 1, n))
    datasets = datasets[:MAX_DATASETS]
    res = run.parallel({ds: (lambda d=ds: _list_tables(run, pid, d)) for ds in datasets})
    ai, billing = {}, {}
    for ds in datasets:
        ok, tables = res.get(ds, (False, None))
        if not ok or not tables:
            continue
        kinds = _classify(tables)
        if kinds:
            ai[ds] = kinds
        bills = sorted(t for t in tables if t.startswith(BILLING_PREFIXES))
        if bills:
            billing[ds] = bills
    run.set_fact(pid, "ai_tables", ai)
    run.set_fact(pid, "billing_tables", billing)
    run.set_fact(pid, "datasets", datasets)
    return ai, billing, None


def _table_meta(run, pid: str, dataset: str, table: str):
    return run.try_call("GET", "%s/projects/%s/datasets/%s/tables/%s" % (
        BQ_V2, urllib.parse.quote(pid, safe=""), urllib.parse.quote(dataset, safe=""),
        urllib.parse.quote(table, safe="")), project_hint=pid)


# ---------------------------------------------------------------------------
# bigquery category (per project)
# ---------------------------------------------------------------------------

def bigquery_checks(run, pid: str) -> list[dict]:
    out = []
    hosts_dataset = pid in dataset_projects(run)
    if hosts_dataset:
        out.append(dataset_exists_check(run, pid))
    out.extend(_ai_tables_checks(run, pid, hosts_dataset))
    if run.is_hub(pid):
        out.append(billing_export_check(run))
    return out


def dataset_exists_check(run, pid: str) -> dict:
    title = ("감사 로그 BigQuery 데이터세트", "Audit BigQuery dataset")
    meta, err = run.dataset_meta(pid, run.dataset)
    cmd = "bq --project_id=%s mk --dataset --location=%s %s:%s" % (pid, run.region, pid, run.dataset)
    if err is not None:
        if err.kind == "not_found":
            run.set_fact(pid, "dataset_exists", False)
            return check("bq.dataset.exists", "bigquery", "warn", *title,
                         "Dataset %s:%s does not exist yet; prepare.sh creates it in %s (or adopt an existing dataset "
                         "with --reuse-dataset PROJECT:DATASET)." % (pid, run.dataset, run.region),
                         impact=["usage"], commands=[cmd], auto_fixable=True)
        cmds = [cmd_enable(pid, ["bigquery.googleapis.com"])] if run.api_enabled(pid, "bigquery.googleapis.com") is False else []
        return skipped("bq.dataset.exists", "bigquery", *title, err, what="dataset %s:%s" % (pid, run.dataset),
                       commands=cmds)
    loc = str((meta or {}).get("location") or "")
    run.set_fact(pid, "dataset_exists", True)
    run.set_fact(pid, "dataset_location", loc)
    kms = ((meta or {}).get("defaultEncryptionConfiguration") or {}).get("kmsKeyName")
    return check("bq.dataset.exists", "bigquery", "pass", *title,
                 "Dataset %s:%s exists in %s%s." % (pid, run.dataset, loc or "?", " (CMEK)" if kms else ""),
                 data={"location": loc, "cmek": bool(kms)})


def _ai_tables_checks(run, pid: str, hosts_dataset: bool) -> list[dict]:
    title = ("기존 AI 로그 테이블 탐지", "Existing AI log tables")
    ai, _, err = scan_project_tables(run, pid)
    if err is not None:
        return [skipped("bq.ai_tables.discovered", "bigquery", *title, err,
                        what="BigQuery datasets of %s (bigquery.datasets.get / tables.list)" % pid)]
    out = []
    configured = ai.get(run.dataset) if hosts_dataset else None
    others = {ds: kinds for ds, kinds in ai.items() if not (hosts_dataset and ds == run.dataset)}
    data = {"datasets": {ds: sorted(k) for ds, k in ai.items()}}
    if configured:
        out.append(check("bq.ai_tables.discovered", "bigquery", "pass", *title,
                         "Configured dataset %s:%s already holds AI log tables (%s)." % (
                             pid, run.dataset, ", ".join(sorted(configured))), data=data))
    elif others:
        best = sorted(others.items(), key=lambda kv: (-len(kv[1]), kv[0]))[0][0]
        adopt = {"workspace.audit_dataset": best}
        if run.central or not run.is_hub(pid):
            adopt["workspace.audit_project"] = pid
        flag = "--reuse-dataset %s:%s" % (pid, best)
        data["adopt"] = {"config": adopt, "install_flag": flag,
                         "env": {"AUDIT_DATASET_ID": best, "AUDIT_PROJECT_ID": pid}}
        out.append(check("bq.ai_tables.discovered", "bigquery", "warn" if hosts_dataset else "info", *title,
                         "AI log tables already exist in %s:%s (%s)%s. Reuse it instead of creating a duplicate: set "
                         "%s (install flag %s)." % (
                             pid, best, ", ".join(sorted(others[best])),
                             "" if not hosts_dataset else ", not in the configured dataset %s" % run.dataset,
                             ", ".join("%s=%s" % kv for kv in adopt.items()), flag),
                         impact=["usage"], data=data))
        run.set_fact(pid, "adopt_dataset", "%s:%s" % (pid, best))
    else:
        out.append(check("bq.ai_tables.discovered", "bigquery", "info", *title,
                         "No AI log tables found in %s (scanned %d dataset(s))." % (
                             pid, len(run.fact(pid, "datasets") or [])), data=data))
    if hosts_dataset:
        tables = configured or {}
        if not tables and others:
            # Freshness of the adoptable dataset tells whether reuse brings recent data.
            best = run.fact(pid, "adopt_dataset", "").split(":", 1)[-1]
            tables = others.get(best, {})
            ds_name = best
        else:
            ds_name = run.dataset
        out.extend(_freshness(run, pid, ds_name, tables))
    return out


def _freshness(run, pid: str, dataset: str, tables: dict) -> list[dict]:
    out = []
    now_ms = time.time() * 1000
    for kind in AI_TABLES:
        cid = "bq.freshness.%s" % kind
        title = ("%s 로그 최신성" % kind, "%s log freshness" % kind)
        table = tables.get(kind)
        if not table:
            out.append(check(cid, "bigquery", "info", *title,
                             "No %s table in %s:%s yet (it appears with the first routed log)." % (kind, pid, dataset),
                             impact=[kind]))
            continue
        meta, err = _table_meta(run, pid, dataset, table)
        if err is not None:
            out.append(skipped(cid, "bigquery", *title, err, what="table %s.%s" % (dataset, table)))
            continue
        try:
            modified = float((meta or {}).get("lastModifiedTime") or 0)
        except (TypeError, ValueError):
            modified = 0.0
        age_days = (now_ms - modified) / 86400000.0 if modified else None
        data = {"table": "%s:%s.%s" % (pid, dataset, table), "age_days": round(age_days, 2) if age_days is not None else None}
        if age_days is not None and age_days <= FRESH_DAYS:
            out.append(check(cid, "bigquery", "pass", *title, "%s updated %.1f day(s) ago." % (table, age_days),
                             data=data))
        else:
            out.append(check(cid, "bigquery", "warn", *title,
                             "%s has no data newer than %d days: the sink may be broken, disabled, missing writer "
                             "access, or the surface is unused." % (table, FRESH_DAYS), impact=[kind], data=data))
    return out


def billing_export_check(run) -> dict:
    title = ("결제 내보내기(Billing export) 테이블", "Billing export table")
    bil = run.ws.get("billing") or {}
    bproject = (bil.get("project_id") or "").strip().lower() or run.hub
    bdataset = (bil.get("dataset") or "").strip()
    btable = (bil.get("table") or "").strip()
    if bdataset and btable:
        meta, err = _table_meta(run, bproject, bdataset, btable)
        if err is None:
            loc = str((meta or {}).get("location") or "")
            run.set_fact(run.hub, "billing_location", loc)
            return check("bq.billing_export", "bigquery", "pass", *title,
                         "Configured billing export %s:%s.%s is readable (%s)." % (bproject, bdataset, btable, loc),
                         data={"location": loc})
        return check("bq.billing_export", "bigquery", "warn", *title,
                     "Configured billing export %s:%s.%s cannot be read (%s): costs fall back to list prices."
                     % (bproject, bdataset, btable, reason_of(err)), impact=["chargeback", "pricing"])
    candidates = [bproject] + ([run.hub] if bproject != run.hub else [])
    found, fallback, errors = None, None, []
    for project in candidates:
        _, billing, err = scan_project_tables(run, project)
        if err is not None:
            errors.append(err)
            continue
        for ds in sorted(billing, key=lambda n: (0 if "billing" in n.lower() else 1, n)):
            for table in billing[ds]:
                if table.startswith(BILLING_PREFIXES[0]) and not found:
                    found = (project, ds, table)
                elif table.startswith(BILLING_PREFIXES[1]) and not fallback:
                    fallback = (project, ds, table)
    pick = found or fallback
    if pick:
        project, ds, table = pick
        adopt = {"workspace.billing.project_id": project, "workspace.billing.dataset": ds,
                 "workspace.billing.table": table}
        return check("bq.billing_export", "bigquery", "info", *title,
                     "Detected %s billing export %s:%s.%s; set %s to use exact costs." % (
                         "detailed (resource)" if pick is found else "standard", project, ds, table,
                         ", ".join("%s=%s" % kv for kv in adopt.items())),
                     data={"adopt": {"config": adopt, "env": {"BILLING_PROJECT_ID": project, "BILLING_DATASET_ID": ds,
                                                              "BILLING_TABLE_ID": table}}})
    if errors and len(errors) == len(candidates):
        return skipped("bq.billing_export", "bigquery", *title, errors[0], what="BigQuery datasets of %s" % bproject)
    return check("bq.billing_export", "bigquery", "warn", *title,
                 "No Cloud Billing export table found in %s: costs are estimated from list prices (credits, "
                 "discounts and Gemini Enterprise seat SKUs are not reflected)." % ", ".join(candidates),
                 impact=["chargeback", "pricing"], docs_url=DOCS["billing_export"])


def location_consistency_check(run) -> dict | None:
    """bq.dataset.location_consistency (cross-project)."""
    locs = {}
    for pid in dataset_projects(run):
        loc = run.fact(pid, "dataset_location")
        if loc:
            locs[pid] = loc.lower()
    title = ("데이터세트 위치 일관성", "Dataset location consistency")
    bloc = (run.fact(run.hub, "billing_location") or "").lower()
    distinct = sorted(set(locs.values()))
    if len(distinct) > 1:
        return check("bq.dataset.location_consistency", "bigquery", "warn", *title,
                     "Audit datasets live in different locations (%s). Queries spanning projects cannot join tables "
                     "across locations; use the central hub dataset (--central-dataset) or one location." %
                     ", ".join("%s=%s" % kv for kv in sorted(locs.items())), impact=["usage"], data={"locations": locs})
    if distinct and bloc and bloc != distinct[0]:
        return check("bq.dataset.location_consistency", "bigquery", "info", *title,
                     "Audit dataset location %s differs from the billing export location %s: usage and billing are "
                     "queried separately (no cross-location joins)." % (distinct[0], bloc),
                     data={"locations": locs, "billing_location": bloc})
    if distinct:
        return check("bq.dataset.location_consistency", "bigquery", "pass", *title,
                     "All audit datasets are in %s." % distinct[0], data={"locations": locs})
    return None


# ---------------------------------------------------------------------------
# logging category (per project)
# ---------------------------------------------------------------------------

def _covers(sink: dict, log_id: str) -> bool:
    flt = str(sink.get("filter") or "")
    if any((not ex.get("disabled")) and (log_id in str(ex.get("filter") or "") or
                                         log_id.replace("/", "%2F") in str(ex.get("filter") or ""))
           for ex in sink.get("exclusions") or []):
        return False
    if not flt.strip():
        return True
    return log_id in flt or log_id.replace("/", "%2F") in flt


def _dest_kind(dest: str) -> str:
    if dest.startswith("bigquery.googleapis.com/"):
        return "bigquery"
    if dest.startswith("pubsub.googleapis.com/"):
        return "pubsub"
    if dest.startswith("logging.googleapis.com/"):
        return "bucket"
    if dest.startswith("storage.googleapis.com/"):
        return "storage"
    return "other"


def _dest_parts(dest: str) -> tuple[str, str]:
    """(project, dataset|topic) from a sink destination."""
    parts = dest.split("/")
    try:
        project = parts[parts.index("projects") + 1]
    except (ValueError, IndexError):
        return "", ""
    return project, parts[-1]


def _project_sinks(run, pid: str):
    items, _ = run.paginate("%s/projects/%s/sinks" % (LOGGING_V2, urllib.parse.quote(pid, safe="")), "sinks",
                            page_size=200, max_pages=3, project_hint=pid)
    return items


def _aggregated_sinks(run, pid: str) -> list[dict]:
    """Org/folder sinks with includeChildren that apply to `pid` (best effort; unreadable ancestors are skipped)."""
    out = []
    for anc in run.ancestry(pid) or []:
        if anc["type"] not in ("folder", "organization"):
            continue
        coll = "folders" if anc["type"] == "folder" else "organizations"
        try:
            items, _ = run.paginate("%s/%s/%s/sinks" % (LOGGING_V2, coll, anc["id"]), "sinks", page_size=200,
                                    max_pages=2)
        except Exception:  # pylint: disable=broad-except
            continue
        for s in items:
            if s.get("includeChildren"):
                s = dict(s)
                s["_scope"] = "%s/%s" % (coll, anc["id"])
                out.append(s)
    return out


def logging_checks(run, pid: str) -> list[dict]:
    out = []
    try:
        sinks = _project_sinks(run, pid)
    except Exception as err:  # pylint: disable=broad-except
        out.append(skipped("logging.sinks", "logging", "Log Router 싱크", "Log Router sinks", err,
                           what="Log Router sinks of %s (logging.sinks.list)" % pid))
        sinks = None
    aggregated = _aggregated_sinks(run, pid)
    if sinks is not None:
        out.extend(_sink_checks(run, pid, sinks, aggregated))
        out.append(_default_sink_check(run, pid, sinks))
    out.append(audit_config_check(run, pid))
    return out


def _sink_checks(run, pid: str, sinks: list[dict], aggregated: list[dict]) -> list[dict]:
    out = []
    all_sinks = [dict(s, _scope="projects/%s" % pid) for s in sinks if s.get("name") not in ("_Required", "_Default")]
    all_sinks += aggregated
    expected = "bigquery.googleapis.com/projects/%s/datasets/%s" % (run.dataset_project if run.central else pid,
                                                                   run.dataset)
    bq_cover, ps_cover, disabled = {}, [], []
    for s in all_sinks:
        dest = str(s.get("destination") or "")
        kind = _dest_kind(dest)
        if s.get("disabled"):
            if any(_covers(s, lid) for ids in NEEDED_LOGS.values() for lid in ids):
                disabled.append(s)
            continue
        if kind == "bigquery":
            for k, ids in NEEDED_LOGS.items():
                if any(_covers(s, lid) for lid in ids):
                    bq_cover.setdefault(k, []).append(s)
        elif kind == "pubsub" and _covers(s, NEEDED_LOGS["antigravity"][0]):
            ps_cover.append(s)

    title = ("AI 로그 BigQuery 싱크", "AI logs BigQuery sink")
    adopt = []
    for k, lst in bq_cover.items():
        for s in lst:
            dest = str(s.get("destination") or "")
            if dest.rstrip("/") != expected:
                proj, ds = _dest_parts(dest)
                adopt.append({"sink": s.get("name"), "scope": s["_scope"], "kind": k, "destination": dest,
                              "config": {"workspace.audit_project": proj, "workspace.audit_dataset": ds}})
    missing = [k for k in NEEDED_LOGS if k not in bq_cover]
    data = {"covered": {k: [s.get("name") for s in v] for k, v in bq_cover.items()}, "missing": missing,
            "adopt": adopt, "expected_destination": expected}
    create_cmd = ("gcloud logging sinks create %s %s --project=%s --log-filter=%s --use-partitioned-tables" %
                  (sink_name(run), expected, pid, q(AI_LOG_FILTER)))
    if not missing and not adopt:
        out.append(check("logging.sink.bigquery", "logging", "pass", *title,
                         "Every needed AI log is routed to %s." % expected, data=data))
    elif not missing:
        out.append(check("logging.sink.bigquery", "logging", "info", *title,
                         "AI logs are already routed by existing sink(s) to another destination (%s). Adopt them "
                         "(--reuse-sinks, and point workspace.audit_project/audit_dataset there) instead of creating a "
                         "duplicate sink." % "; ".join("%s -> %s" % (a["sink"], a["destination"]) for a in adopt[:5]),
                         data=data))
    else:
        detail = "No sink routes %s logs of %s to BigQuery." % (", ".join(missing), pid)
        if adopt:
            detail += " Existing sinks cover: %s." % "; ".join("%s (%s)" % (a["sink"], a["kind"]) for a in adopt[:5])
        detail += " prepare.sh creates %s -> %s." % (sink_name(run), expected)
        out.append(check("logging.sink.bigquery", "logging", "warn", *title, detail, impact=["usage"] + missing,
                         commands=[create_cmd], auto_fixable=True, data=data, docs_url=DOCS["sinks"]))
    if adopt:
        run.set_fact(pid, "adopt_sinks", adopt)

    ps_title = ("실시간 Pub/Sub 싱크", "Real-time Pub/Sub sink")
    topic_path = "pubsub.googleapis.com/projects/%s/topics/%s" % (run.hub, run.topic)
    if ps_cover:
        out.append(check("logging.sink.pubsub", "logging", "pass", *ps_title,
                         "Antigravity inference logs are pushed to Pub/Sub by %s." % ", ".join(
                             "%s -> %s" % (s.get("name"), s.get("destination")) for s in ps_cover[:3]),
                         data={"sinks": [{"name": s.get("name"), "destination": s.get("destination"),
                                          "scope": s["_scope"]} for s in ps_cover]}))
    else:
        out.append(check("logging.sink.pubsub", "logging", "info", *ps_title,
                         "No Pub/Sub sink for Antigravity inference logs in %s: real-time enforcement falls back to "
                         "the 5-minute scheduler. deploy.sh creates one publishing to the hub topic." % pid,
                         impact=["realtime"], auto_fixable=True,
                         commands=["gcloud logging sinks create %s-usage-events %s --project=%s --log-filter=%s" % (
                             run.service_name, topic_path, pid, q(REALTIME_FILTER))]))
    if disabled:
        out.append(check("logging.sink.disabled", "logging", "warn", "비활성화된 AI 로그 싱크", "Disabled AI log sinks",
                         "Sink(s) %s would route AI logs but are disabled." % ", ".join(
                             str(s.get("name")) for s in disabled[:5]), impact=["usage"],
                         commands=["gcloud logging sinks update %s --no-disabled --project=%s" % (s.get("name"), pid)
                                   for s in disabled[:5] if s["_scope"].startswith("projects/")]))
    out.extend(_writer_access(run, pid, [s for lst in bq_cover.values() for s in lst], ps_cover))
    return out


def _writer_access(run, pid: str, bq_sinks: list[dict], ps_sinks: list[dict]) -> list[dict]:
    title = ("싱크 작성자 권한", "Sink writer identity access")
    seen, problems, checked, unreadable = set(), [], 0, 0
    cmds = []
    for s in bq_sinks + ps_sinks:
        key = (s.get("name"), s.get("_scope"))
        if key in seen or checked >= MAX_SINKS_ACCESS:
            continue
        seen.add(key)
        writer = str(s.get("writerIdentity") or "")
        dest = str(s.get("destination") or "")
        if not writer:
            continue
        email = writer.split(":", 1)[-1].lower()
        proj, target = _dest_parts(dest)
        if not proj:
            continue
        checked += 1
        if _dest_kind(dest) == "bigquery":
            meta, err = run.dataset_meta(proj, target)
            if err is not None:
                unreadable += 1
                continue
            ok = any(str(a.get("userByEmail") or a.get("iamMember", "")).lower().endswith(email) and
                     a.get("role") in WRITER_DATASET_ROLES for a in (meta or {}).get("access") or [])
            if not ok:
                pol, perr = run.iam_policy(proj)
                if perr is None:
                    ok = any(b.get("role") in WRITER_PROJECT_ROLES and writer.lower() in
                             [m.lower() for m in b.get("members") or []] for b in (pol or {}).get("bindings") or [])
            if not ok:
                problems.append("%s -> %s:%s" % (s.get("name"), proj, target))
                cmds.append("bq add-iam-policy-binding --member=%s --role=roles/bigquery.dataEditor %s:%s"
                            % (writer, proj, target))
        else:
            pol, err = run.try_call("GET", "%s/projects/%s/topics/%s:getIamPolicy" % (
                PUBSUB_V1, urllib.parse.quote(proj, safe=""), urllib.parse.quote(target, safe="")), project_hint=proj)
            if err is not None:
                unreadable += 1
                continue
            ok = any(b.get("role") in PUBLISHER_ROLES and writer.lower() in [m.lower() for m in b.get("members") or []]
                     for b in (pol or {}).get("bindings") or [])
            if not ok:
                problems.append("%s -> topic %s/%s" % (s.get("name"), proj, target))
                cmds.append("gcloud pubsub topics add-iam-policy-binding %s --project=%s --member=%s "
                            "--role=roles/pubsub.publisher" % (target, proj, writer))
    if not checked:
        return []
    if problems:
        return [check("logging.sink.writer_access", "logging", "warn", *title,
                      "Writer identities lack write access on their destination: %s. Logs are dropped silently." %
                      "; ".join(problems), impact=["usage", "realtime"], commands=cmds, auto_fixable=True)]
    if unreadable == checked:
        return [check("logging.sink.writer_access", "logging", "skip", *title,
                      "Destination IAM of %d sink(s) could not be read." % checked)]
    return [check("logging.sink.writer_access", "logging", "pass", *title,
                  "Writer identities of %d sink(s) can write to their destinations." % (checked - unreadable))]


def _mentions_needed_log(flt: str) -> bool:
    """True when an exclusion filter names one of the AI log IDs (plain or URL-encoded) or AI log services."""
    for ids in NEEDED_LOGS.values():
        for lid in ids:
            if lid in flt or lid.replace("/", "%2F") in flt:
                return True
    return "businessaicode.googleapis.com" in flt or "discoveryengine.googleapis.com" in flt


def _default_sink_check(run, pid: str, sinks: list[dict]) -> dict:
    title = ("_Default 싱크/제외 필터", "_Default sink & exclusions")
    default = next((s for s in sinks if s.get("name") == "_Default"), None)
    relevant = run.usage_source in ("auto", "logging")
    issues = []
    if default is None:
        issues.append("_Default sink not found")
    else:
        if default.get("disabled"):
            issues.append("_Default sink is disabled")
        for ex in default.get("exclusions") or []:
            flt = str(ex.get("filter") or "")
            if not ex.get("disabled") and _mentions_needed_log(flt):
                issues.append("exclusion '%s' drops AI logs" % ex.get("name"))
    try:
        legacy, _ = run.paginate("%s/projects/%s/exclusions" % (LOGGING_V2, urllib.parse.quote(pid, safe="")),
                                 "exclusions", page_size=100, max_pages=1, project_hint=pid)
    except Exception:  # pylint: disable=broad-except
        legacy = []
    for ex in legacy:
        flt = str(ex.get("filter") or "")
        if not ex.get("disabled") and _mentions_needed_log(flt):
            issues.append("project exclusion '%s' drops AI logs" % ex.get("name"))
    if not issues:
        return check("logging.default_sink", "logging", "pass", *title,
                     "_Default keeps AI logs in Cloud Logging (needed when usage_source is logging/auto).")
    return check("logging.default_sink", "logging", "warn" if relevant else "info", *title,
                 "%s. Cloud Logging reads (usage_source=%s) miss those logs; BigQuery sinks are unaffected." % (
                     "; ".join(issues), run.usage_source), impact=["usage"] if relevant else [])


def _has_data_access(configs: list, services=("aiplatform.googleapis.com", "allServices")) -> set:
    types = set()
    for cfg in configs or []:
        if cfg.get("service") in services:
            for lc in cfg.get("auditLogConfigs") or []:
                types.add(str(lc.get("logType") or ""))
    return types


def audit_config_check(run, pid: str) -> dict:
    title = ("Vertex AI 데이터 액세스 감사 로그", "Vertex AI Data Access audit logs")
    cid = "logging.audit_config.aiplatform"
    if run.api_enabled(pid, "aiplatform.googleapis.com") is False:
        return check(cid, "logging", "info", *title,
                     "aiplatform.googleapis.com is not enabled on %s; Vertex AI per-user usage is not applicable." % pid)
    pol, err = run.iam_policy(pid)
    if err is not None:
        return skipped(cid, "logging", *title, err, what="the IAM policy of %s (resourcemanager.projects.getIamPolicy)"
                       % pid)
    types = _has_data_access((pol or {}).get("auditConfigs"))
    source = "project"
    if not {"DATA_READ", "DATA_WRITE"} <= types:
        for anc in run.ancestry(pid) or []:
            if anc["type"] == "organization":
                apol, aerr = run.try_call("POST", "%s/organizations/%s:getIamPolicy" % (CRM_V1, anc["id"]),
                                          json_body={"options": {"requestedPolicyVersion": 3}})
            elif anc["type"] == "folder":
                apol, aerr = run.try_call("POST", "%s/folders/%s:getIamPolicy" % (CRM_V3, anc["id"]),
                                          json_body={"options": {"requestedPolicyVersion": 3}})
            else:
                continue
            if aerr is None:
                extra = _has_data_access((apol or {}).get("auditConfigs"))
                if extra - types:
                    types |= extra
                    source = "%s/%s" % (anc["type"], anc["id"])
    data = {"log_types": sorted(t for t in types if t), "source": source}
    if {"DATA_READ", "DATA_WRITE"} <= types:
        return check(cid, "logging", "pass", *title,
                     "DATA_READ and DATA_WRITE audit logs are enabled for Vertex AI (%s)." % source, data=data)
    missing = sorted({"DATA_READ", "DATA_WRITE"} - types)
    return check(cid, "logging", "warn", *title,
                 "Vertex AI Data Access audit logs (%s) are off on %s (also checked folder/org inheritance): per-user "
                 "Vertex AI usage and caps cannot be measured. Run prepare.sh with --enable-data-access-logs (merges "
                 "auditConfigs without clobbering existing ones)." % (", ".join(missing), pid),
                 impact=["vertex_ai", "usage"], auto_fixable=True, data=data, docs_url=DOCS["audit"],
                 commands=["scripts/prepare.sh --project %s --managed-projects %s --enable-data-access-logs" % (
                     run.hub, pid) if not run.is_hub(pid) else
                           "scripts/prepare.sh --project %s --enable-data-access-logs" % run.hub])
