"""Readiness categories `vpcsc` (VPC Service Controls) and `network` (hub VPC egress, Private Google Access, NAT, DNS).

VPC Service Controls are evaluated through Access Context Manager (read-only): every access policy of the project's
organization is listed with its service perimeters (enforced `status` and dry-run `spec`). For each project the
regular perimeter containing `projects/<number>` is found; for every managed project the hub->managed path is checked:
when the two projects sit in different perimeters (or one inside, one outside) and the boundary restricts services the
product calls, the run reports a cross-project failure with ready-to-paste ingress/egress rules (runtime & enforcer
service accounts for API calls, Log Router writer identities for sinks), unless a perimeter bridge or matching rules
already exist.
"""

from __future__ import annotations

import urllib.parse

from .readiness_core import ACM_V1, COMPUTE_V1, DNS_V1, DOCS, check, skipped

# Google APIs the product calls across projects (runtime/enforcer SAs) that a perimeter can restrict.
PRODUCT_SERVICES = ("logging.googleapis.com", "bigquery.googleapis.com", "iam.googleapis.com",
                    "orgpolicy.googleapis.com", "cloudquotas.googleapis.com", "discoveryengine.googleapis.com",
                    "aiplatform.googleapis.com", "pubsub.googleapis.com", "cloudresourcemanager.googleapis.com",
                    "monitoring.googleapis.com", "serviceusage.googleapis.com", "businessaicode.googleapis.com")
# Services the hub itself needs inside its own perimeter.
HUB_SERVICES = PRODUCT_SERVICES + ("storage.googleapis.com", "run.googleapis.com", "secretmanager.googleapis.com",
                                   "artifactregistry.googleapis.com", "cloudbuild.googleapis.com",
                                   "cloudscheduler.googleapis.com", "iamcredentials.googleapis.com",
                                   "cloudbilling.googleapis.com", "billingbudgets.googleapis.com")
SINK_SERVICES = ("bigquery.googleapis.com", "pubsub.googleapis.com")
ANY_IDENTITY_TYPES = ("ANY_IDENTITY", "ANY_SERVICE_ACCOUNT")


# ---------------------------------------------------------------------------
# Access Context Manager readers
# ---------------------------------------------------------------------------

def org_perimeters(run, org: str):
    """(perimeters, err) for every access policy of `org`. Each perimeter:
    {name, title, policy, type, status, spec, dry_run}."""
    key = "org:" + org
    cached = run.fact(key, "perimeters")
    if cached is not None:
        return cached, run.fact(key, "perimeters_err")
    try:
        policies, _ = run.paginate("%s/accessPolicies" % ACM_V1, "accessPolicies",
                                   params={"parent": "organizations/%s" % org}, page_size=100, max_pages=3)
    except Exception as err:  # pylint: disable=broad-except
        run.set_fact(key, "perimeters", [])
        run.set_fact(key, "perimeters_err", err)
        return [], err
    tasks = {}
    for pol in policies:
        name = str(pol.get("name") or "")
        if name.startswith("accessPolicies/"):
            tasks[name] = (lambda n=name: run.paginate("%s/%s/servicePerimeters" % (ACM_V1, n), "servicePerimeters",
                                                       page_size=100, max_pages=5)[0])
    res = run.parallel(tasks)
    out, first_err = [], None
    for pname, (ok, value) in res.items():
        if not ok:
            first_err = first_err or value
            continue
        for per in value or []:
            out.append({
                "name": str(per.get("name") or ""),
                "title": str(per.get("title") or str(per.get("name") or "").rsplit("/", 1)[-1]),
                "policy": pname.split("/", 1)[-1],
                "type": str(per.get("perimeterType") or "PERIMETER_TYPE_REGULAR"),
                "status": per.get("status") if isinstance(per.get("status"), dict) else {},
                "spec": per.get("spec") if isinstance(per.get("spec"), dict) else {},
                "dry_run": bool(per.get("useExplicitDryRunSpec")),
            })
    err = first_err if (not out and first_err is not None) else None
    run.set_fact(key, "perimeters", out)
    run.set_fact(key, "perimeters_err", err)
    return out, err


def _restricted(section: dict) -> list[str]:
    svcs = [str(s) for s in section.get("restrictedServices") or []]
    if "*" in svcs:
        return sorted(set(svcs + list(HUB_SERVICES)) - {"*"})
    return svcs


def locate(perimeters: list[dict], number: str) -> dict:
    """{'enforced': perimeter|None, 'dry_run': perimeter|None, 'bridges': [names]} for projects/<number>."""
    member = "projects/%s" % number
    found = {"enforced": None, "dry_run": None, "bridges": []}
    for per in perimeters:
        in_status = member in (per["status"].get("resources") or [])
        in_spec = member in (per["spec"].get("resources") or [])
        if per["type"] == "PERIMETER_TYPE_BRIDGE":
            if in_status:
                found["bridges"].append(per["name"])
            continue
        if in_status and found["enforced"] is None:
            found["enforced"] = per
        if per["dry_run"] and in_spec and found["dry_run"] is None:
            found["dry_run"] = per
    return found


def _summary(per: dict | None) -> dict | None:
    if not per:
        return None
    st = per["status"]
    return {"name": per["name"], "title": per["title"], "policy": per["policy"],
            "restricted_services": _restricted(st),
            "vpc_accessible_services": st.get("vpcAccessibleServices") or {},
            "ingress_rules": len(st.get("ingressPolicies") or []),
            "egress_rules": len(st.get("egressPolicies") or [])}


# ---------------------------------------------------------------------------
# Per-project VPC-SC checks
# ---------------------------------------------------------------------------

def vpcsc_checks(run, pid: str) -> list[dict]:
    title = ("VPC 서비스 제어 경계", "VPC Service Controls perimeter")
    cid = "vpcsc.perimeter"
    number = run.project_number(pid)
    org = run.org_of(pid)
    if not number:
        return [skipped(cid, "vpcsc", *title, "project number unknown", what="the project number")]
    if not org:
        run.set_fact(pid, "perimeter", None)
        return [check(cid, "vpcsc", "pass", *title, "No organization: the project cannot be in a service perimeter.")]
    perimeters, err = org_perimeters(run, org)
    if err is not None:
        run.set_fact(pid, "perimeter_unknown", True)
        return [skipped(cid, "vpcsc", *title, err, what="Access Context Manager perimeters of organization %s "
                        "(roles/accesscontextmanager.policyReader on the organization)" % org, docs_url=DOCS["vpcsc"])]
    loc = locate(perimeters, number)
    run.set_fact(pid, "perimeter", loc["enforced"])
    run.set_fact(pid, "bridges", loc["bridges"])
    out = []
    used = HUB_SERVICES if run.is_hub(pid) else PRODUCT_SERVICES
    if loc["enforced"] is None:
        out.append(check(cid, "vpcsc", "pass", *title, "Project %s is not inside an enforced service perimeter." % pid,
                         data={"perimeter": None, "bridges": loc["bridges"]}))
    else:
        summ = _summary(loc["enforced"])
        overlap = [s for s in used if s in summ["restricted_services"]]
        status = "warn" if overlap else "info"
        detail = ("Project %s is inside perimeter '%s' (policy %s). Restricted services used by this product: %s."
                  % (pid, summ["title"], summ["policy"], ", ".join(s.split(".")[0] for s in overlap) or "none"))
        if overlap and run.is_hub(pid):
            detail += (" Callers outside the perimeter (portals, the installer's workstation) need an ingress rule or "
                       "access level; Cloud Run in the perimeter reaches restricted services normally.")
            run.adapt("vpcsc_restricted", overlap)
        out.append(check(cid, "vpcsc", status, *title, detail, impact=["usage", "deployment"] if overlap else [],
                         data={"perimeter": summ, "restricted_used": overlap}, docs_url=DOCS["vpcsc"]))
        if run.is_hub(pid):
            out.extend(_accessible_services(run, pid, summ))
    if loc["dry_run"] is not None:
        spec = loc["dry_run"]["spec"]
        overlap = [s for s in used if s in _restricted(spec)]
        out.append(check("vpcsc.dry_run", "vpcsc", "info", "VPC-SC 드라이런 경계", "VPC-SC dry-run perimeter",
                         "Project %s is in the dry-run configuration of perimeter '%s' (restricts %s). Violations are "
                         "only logged now but will block once enforced." % (pid, loc["dry_run"]["title"],
                                                                            ", ".join(overlap) or "no used services"),
                         data={"perimeter": loc["dry_run"]["name"], "restricted_used": overlap}))
    return out


def _accessible_services(run, pid, summ) -> list[dict]:
    vas = summ.get("vpc_accessible_services") or {}
    if not vas.get("enableRestriction"):
        return []
    allowed = [str(s) for s in vas.get("allowedServices") or []]
    if "RESTRICTED-SERVICES" in allowed:
        allowed = sorted(set(allowed) | set(summ["restricted_services"]))
    missing = [s for s in HUB_SERVICES if s not in allowed and "*" not in allowed]
    title = ("VPC 접근 가능 서비스 제한", "VPC accessible services restriction")
    uses_vpc = bool(run.vpc_network and run.vpc_subnet) or run.adaptations.get("vpc_egress_required")
    if not missing:
        return [check("vpcsc.accessible_services", "vpcsc", "pass", *title, "All services used are VPC-accessible.")]
    return [check("vpcsc.accessible_services", "vpcsc", "warn" if uses_vpc else "info", *title,
                  "The perimeter limits which services are reachable from its VPC networks; not allowed: %s. With "
                  "Direct VPC egress (all-traffic) those calls from Cloud Run fail." % ", ".join(missing[:12]),
                  impact=["usage"] if uses_vpc else [], data={"not_accessible": missing})]


# ---------------------------------------------------------------------------
# Cross-project hub <-> managed path
# ---------------------------------------------------------------------------

def _rule_identities(rules: list, side: str) -> tuple[set, bool]:
    ids, any_id = set(), False
    for rule in rules or []:
        frm = (rule or {}).get(side) or {}
        for ident in frm.get("identities") or []:
            ids.add(str(ident).lower())
        if str(frm.get("identityType") or "") in ANY_IDENTITY_TYPES:
            any_id = True
    return ids, any_id


def _yaml_ops(services) -> list[str]:
    lines = ["    operations:"]
    for svc in services:
        lines += ["    - serviceName: %s" % svc, "      methodSelectors:", "      - method: \"*\""]
    return lines


def ingress_yaml(identities, sources_project_number: str | None, services, target_number: str) -> str:
    lines = ["- ingressFrom:", "    identities:"] + ["    - %s" % i for i in identities] + ["    sources:"]
    if sources_project_number:
        lines.append("    - resource: projects/%s" % sources_project_number)
    else:
        lines.append("    - accessLevel: \"*\"")
    lines.append("  ingressTo:")
    lines += _yaml_ops(services)
    lines += ["    resources:", "    - projects/%s" % target_number]
    return "\n".join(lines) + "\n"


def egress_yaml(identities, services, target_number: str) -> str:
    lines = ["- egressFrom:", "    identities:"] + ["    - %s" % i for i in identities] + ["  egressTo:"]
    lines += _yaml_ops(services)
    lines += ["    resources:", "    - projects/%s" % target_number]
    return "\n".join(lines) + "\n"


def cross_project_check(run, managed: str) -> dict | None:
    """vpcsc.cross_project for one managed project (None when there is nothing to say)."""
    title = ("허브-관리 프로젝트 간 VPC-SC 경로", "Hub <-> managed project VPC-SC path")
    cid = "vpcsc.cross_project"
    if run.fact(run.hub, "perimeter_unknown") or run.fact(managed, "perimeter_unknown"):
        item = check(cid, "vpcsc", "skip", *title,
                     "Perimeters could not be read for %s or the hub; the hub->%s path cannot be verified. Watch for "
                     "VPC-SC denials (vpcsc.denials)." % (managed, managed))
        item["project"] = managed
        return item
    hub_per = run.fact(run.hub, "perimeter")
    man_per = run.fact(managed, "perimeter")
    hub_name = hub_per["name"] if hub_per else None
    man_name = man_per["name"] if man_per else None
    if hub_name == man_name:
        if hub_name is None:
            return None
        item = check(cid, "vpcsc", "pass", *title, "Hub and %s share perimeter '%s'." % (managed, hub_per["title"]))
        item["project"] = managed
        return item
    restricted = set()
    if man_per:
        restricted |= set(_restricted(man_per["status"]))
    if hub_per:
        restricted |= set(_restricted(hub_per["status"]))
    services = [s for s in PRODUCT_SERVICES if s in restricted]
    sink_services = [s for s in SINK_SERVICES if hub_per and s in _restricted(hub_per["status"])]
    if not services and not sink_services:
        item = check(cid, "vpcsc", "pass", *title,
                     "Hub and %s are in different perimeters, but no service this product uses is restricted." % managed)
        item["project"] = managed
        return item
    shared_bridge = set(run.fact(run.hub, "bridges") or []) & set(run.fact(managed, "bridges") or [])
    if shared_bridge:
        item = check(cid, "vpcsc", "pass", *title,
                     "Hub and %s are connected by perimeter bridge %s." % (managed, sorted(shared_bridge)[0]))
        item["project"] = managed
        return item
    sas = ["serviceAccount:%s" % run.runtime_sa, "serviceAccount:%s" % run.enforcer_sa]
    ingress_ok = egress_ok = True
    if man_per:
        ids, any_id = _rule_identities(man_per["status"].get("ingressPolicies"), "ingressFrom")
        ingress_ok = any_id or all(s.lower() in ids for s in sas)
    if hub_per:
        ids, any_id = _rule_identities(hub_per["status"].get("egressPolicies"), "egressFrom")
        egress_ok = any_id or all(s.lower() in ids for s in sas)
    hub_num = run.project_number(run.hub)
    man_num = run.project_number(managed)
    writer = "serviceAccount:service-%s@gcp-sa-logging.iam.gserviceaccount.com" % (man_num or "MANAGED_NUMBER")
    commands, yaml_blocks = [], {}
    if man_per and services:
        y = ingress_yaml(sas, hub_num if hub_per else None, services, man_num or "MANAGED_NUMBER")
        yaml_blocks["managed_ingress"] = y
        fname = "vpcsc-ingress-%s.yaml" % managed
        commands.append("cat > %s <<'YAML'\n%sYAML" % (fname, y))
        commands.append("# Merge with the existing rules first (--set-ingress-policies REPLACES them):\n"
                        "# gcloud access-context-manager perimeters update %s --policy=%s --set-ingress-policies=%s"
                        % (man_per["name"].rsplit("/", 1)[-1], man_per["policy"], fname))
    if hub_per and services:
        y = egress_yaml(sas, services, man_num or "MANAGED_NUMBER")
        yaml_blocks["hub_egress"] = y
        fname = "vpcsc-egress-hub-to-%s.yaml" % managed
        commands.append("cat > %s <<'YAML'\n%sYAML" % (fname, y))
        commands.append("# gcloud access-context-manager perimeters update %s --policy=%s --set-egress-policies=%s"
                        % (hub_per["name"].rsplit("/", 1)[-1], hub_per["policy"], fname))
    if hub_per and sink_services:
        y = ingress_yaml([writer], man_num if man_per else None, sink_services, hub_num or "HUB_NUMBER")
        yaml_blocks["hub_ingress_sink_writer"] = y
        fname = "vpcsc-ingress-sinks-%s.yaml" % managed
        commands.append("cat > %s <<'YAML'\n%sYAML" % (fname, y))
    data = {"hub_perimeter": hub_name, "managed_perimeter": man_name, "restricted_services": services,
            "sink_services": sink_services, "yaml": yaml_blocks}
    if ingress_ok and egress_ok:
        item = check(cid, "vpcsc", "warn", *title,
                     "Hub (%s) and %s (%s) are in different perimeters restricting %s; existing ingress/egress rules "
                     "mention the product identities (or ANY identity) - verify they cover these services and the "
                     "Log Router writer identity %s." % (hub_name or "none", managed, man_name or "none",
                                                         ", ".join(services or sink_services), writer.split(":", 1)[1]),
                     impact=["usage", "caps_enforcement", "model_governance"], commands=commands, data=data,
                     docs_url=DOCS["vpcsc"])
    else:
        item = check(cid, "vpcsc", "fail", *title,
                     "Hub (%s) and %s (%s) are separated by VPC Service Controls restricting %s. The hub runtime and "
                     "enforcer service accounts cannot read logs, apply deny/model policies or quotas there, and the "
                     "Log Router sink cannot write into the hub dataset, until a perimeter administrator adds the "
                     "ingress/egress rules below (or a perimeter bridge)." % (hub_name or "no perimeter", managed,
                                                                              man_name or "no perimeter",
                                                                              ", ".join(services or sink_services)),
                     impact=["usage", "caps_enforcement", "model_governance", "quota_management"],
                     commands=commands, data=data, docs_url=DOCS["vpcsc"])
    item["project"] = managed
    return item


def denials_check(run) -> dict | None:
    """vpcsc.denials: VPC-SC denials actually observed while running the readiness reads."""
    if not run.vpcsc_denials:
        return None
    items = run.vpcsc_denials[:20]
    caller_only = getattr(run, "identity_mode", "caller") != "caller"
    return check("vpcsc.denials", "vpcsc", "warn" if caller_only else "fail", "VPC-SC 차단 관측",
                 "VPC-SC denials observed",
                 "Requests were refused by VPC Service Controls during the readiness run: %s. The identity running "
                 "the check (and the product runtime) needs an ingress rule or access level."
                 % "; ".join("%s %s" % (d["service"], d["project"]) for d in items),
                 impact=["usage", "deployment"], data={"denials": items}, docs_url=DOCS["vpcsc"])


# ---------------------------------------------------------------------------
# network (hub)
# ---------------------------------------------------------------------------

def _parse_subnet(run) -> tuple[str, str, str]:
    """(host_project, region, subnet_name) from hub.vpc_subnet (short name or full resource path)."""
    raw = run.vpc_subnet.strip()
    if "projects/" in raw:
        parts = raw.split("/")
        try:
            host = parts[parts.index("projects") + 1]
            region = parts[parts.index("regions") + 1] if "regions" in parts else run.region
            name = parts[-1]
            return host, region, name
        except (ValueError, IndexError):
            pass
    return run.hub, run.region, raw


def _network_name(value: str) -> str:
    return str(value or "").rstrip("/").rsplit("/", 1)[-1]


def network_checks(run) -> list[dict]:
    out = []
    required = bool(run.adaptations.get("vpc_egress_required"))
    title = ("Cloud Run VPC 서브넷", "Cloud Run VPC subnet")
    if not (run.vpc_network and run.vpc_subnet):
        if required:
            out.append(check("network.vpc.subnet", "network", "fail", *title,
                             "Organization policy requires VPC egress but hub.vpc_network/vpc_subnet are not "
                             "configured. Pass --vpc-network/--vpc-subnet (subnet in %s with Private Google Access)."
                             % run.region, impact=["deployment"], docs_url=DOCS["run_egress"]))
        else:
            out.append(check("network.vpc.subnet", "network", "info", *title,
                             "No VPC configured: Cloud Run uses direct Google-managed egress (webhooks and Google APIs "
                             "reach the internet directly)."))
        return out
    host, region, name = _parse_subnet(run)
    url = "%s/projects/%s/regions/%s/subnetworks/%s" % (COMPUTE_V1, urllib.parse.quote(host, safe=""), region,
                                                       urllib.parse.quote(name, safe=""))
    sub, err = run.try_call("GET", url, project_hint=host)
    if err is not None:
        out.append(skipped("network.vpc.subnet", "network", *title, err,
                           what="subnetwork %s/%s/%s (compute.subnetworks.get; compute API on the host project)"
                           % (host, region, name)))
    else:
        problems = []
        if region != run.region:
            problems.append("subnet region %s differs from the Cloud Run region %s" % (region, run.region))
        if _network_name(sub.get("network")) != _network_name(run.vpc_network):
            problems.append("subnet belongs to network %s, not %s" % (_network_name(sub.get("network")),
                                                                     _network_name(run.vpc_network)))
        pga = bool(sub.get("privateIpGoogleAccess"))
        if not pga:
            problems.append("Private Google Access is off")
        data = {"host_project": host, "region": region, "subnet": name, "private_google_access": pga,
                "cidr": sub.get("ipCidrRange")}
        if problems:
            cmds = []
            if not pga:
                cmds.append("gcloud compute networks subnets update %s --region=%s --project=%s "
                            "--enable-private-ip-google-access" % (name, region, host))
            all_traffic = required or run.vpc_egress == "all-traffic"
            out.append(check("network.vpc.subnet", "network", "fail" if all_traffic else "warn", *title,
                             "Subnet %s: %s. With all-traffic egress Cloud Run cannot reach Google APIs." %
                             (name, "; ".join(problems)), impact=["usage", "deployment"], commands=cmds, data=data,
                             auto_fixable=False, docs_url=DOCS["pga"]))
        else:
            out.append(check("network.vpc.subnet", "network", "pass", *title,
                             "Subnet %s (%s) in %s has Private Google Access." % (name, sub.get("ipCidrRange"), region),
                             data=data))
    out.append(_nat_check(run, host, region))
    dns = _dns_check(run, host)
    if dns:
        out.append(dns)
    return out


def _nat_check(run, host: str, region: str) -> dict:
    title = ("Cloud NAT (외부 웹훅 송신)", "Cloud NAT for outbound webhooks")
    url = "%s/projects/%s/regions/%s/routers" % (COMPUTE_V1, urllib.parse.quote(host, safe=""), region)
    payload, err = run.try_call("GET", url, project_hint=host)
    if err is not None:
        return skipped("network.nat", "network", *title, err, what="Cloud Routers in %s/%s" % (host, region))
    net = _network_name(run.vpc_network)
    for router in (payload or {}).get("items") or []:
        if _network_name(router.get("network")) == net and router.get("nats"):
            return check("network.nat", "network", "pass", *title,
                         "Cloud NAT %s on router %s serves network %s." % (
                             router["nats"][0].get("name"), router.get("name"), net))
    return check("network.nat", "network", "warn", *title,
                 "No Cloud NAT on network %s in %s: with all-traffic egress, outbound webhooks to customer portals "
                 "and FX rate fetches over the internet fail (Google APIs still work through Private Google Access)."
                 % (net, region), impact=["webhooks", "pricing"], docs_url=DOCS["nat"],
                 commands=["gcloud compute routers create caps-nat-router --network=%s --region=%s --project=%s"
                           % (net, region, host),
                           "gcloud compute routers nats create caps-nat --router=caps-nat-router --region=%s "
                           "--project=%s --auto-allocate-nat-external-ips --nat-all-subnet-ip-ranges" % (region, host)])


def _dns_check(run, host: str) -> dict | None:
    per = run.fact(run.hub, "perimeter")
    if not per:
        return None
    title = ("restricted.googleapis.com 비공개 DNS", "Private DNS for restricted.googleapis.com")
    payload, err = run.try_call("GET", "%s/projects/%s/managedZones" % (DNS_V1, urllib.parse.quote(host, safe="")),
                                project_hint=host)
    if err is not None:
        return skipped("network.private_dns", "network", *title, err, what="Cloud DNS managed zones of %s" % host)
    net = _network_name(run.vpc_network)
    for zone in (payload or {}).get("managedZones") or []:
        if str(zone.get("dnsName") or "") == "googleapis.com." and zone.get("visibility") == "private":
            nets = [_network_name(n.get("networkUrl")) for n in
                    ((zone.get("privateVisibilityConfig") or {}).get("networks") or [])]
            if net in nets:
                return check("network.private_dns", "network", "pass", *title,
                             "Private zone %s maps googleapis.com for network %s." % (zone.get("name"), net))
    return check("network.private_dns", "network", "warn", *title,
                 "The hub is inside a VPC-SC perimeter; route *.googleapis.com to restricted.googleapis.com "
                 "(199.36.153.4/30) with a private Cloud DNS zone on %s so VPC egress stays inside the perimeter." % net,
                 impact=["usage"], docs_url="https://cloud.google.com/vpc-service-controls/docs/set-up-private-connectivity")
