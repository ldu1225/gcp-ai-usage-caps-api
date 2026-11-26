"""Readiness category `orgpolicy`: effective Organization Policies that decide whether (and how) the product works.

Ported from the reference `preflight_org_policies` (scripts/lib/common.sh) and extended for hub-and-spoke:
resource locations (hub region & dataset location verdict), domain restricted sharing (legacy + managed),
Cloud Run invoker IAM / ingress / VPC egress / Binary Authorization, CMEK, restricted services (gcp.restrictServiceUsage
and legacy serviceuser.services), Cloud Build worker pools, already-satisfied constraints, the model-governance
constraint `vertexai.allowedModels` (current values and inheritance), and organization custom constraints that target
resource types this product creates.

Every finding also records an "adaptation" (Run.adaptations) the install scripts use to adapt automatically, e.g.
`no_invoker_iam_check` under DRS or `vpc_egress_required` under run.allowedVPCEgress.
"""

from __future__ import annotations

from .readiness_core import (DOCS, ORGPOLICY_V2, check, cmd_enable, loc_verdict, normalize_policy, skipped,
                             suggest_regions)
from .readiness_project import HUB_APIS, managed_apis

LIST_HUB = ("gcp.resourceLocations", "iam.allowedPolicyMemberDomains", "run.allowedIngress", "run.allowedVPCEgress",
            "run.allowedBinaryAuthorizationPolicies", "gcp.restrictNonCmekServices", "gcp.restrictServiceUsage",
            "serviceuser.services", "cloudbuild.allowedWorkerPools", "vertexai.allowedModels")
BOOLEAN_HUB = ("storage.publicAccessPrevention", "storage.uniformBucketLevelAccess",
               "iam.disableServiceAccountKeyCreation", "iam.automaticIamGrantsForDefaultServiceAccounts")
MANAGED_CONSTRAINTS = ("run.managed.requireInvokerIam", "iam.managed.allowedPolicyMembers")
LIST_MANAGED = ("gcp.resourceLocations", "iam.allowedPolicyMemberDomains", "gcp.restrictNonCmekServices",
                "gcp.restrictServiceUsage", "serviceuser.services", "vertexai.allowedModels")
MANAGED_MANAGED = ("iam.managed.allowedPolicyMembers",)

CMEK_SERVICES_HUB = {"storage.googleapis.com", "bigquery.googleapis.com", "secretmanager.googleapis.com",
                     "artifactregistry.googleapis.com", "run.googleapis.com", "logging.googleapis.com",
                     "cloudbuild.googleapis.com", "pubsub.googleapis.com"}
CMEK_SERVICES_DATASET = {"bigquery.googleapis.com"}

# Resource types this product creates or modifies (custom constraints that target them may block install/operation).
PRODUCT_RESOURCE_TYPES = ("run.googleapis.com/", "iam.googleapis.com/", "bigquery.googleapis.com/",
                          "logging.googleapis.com/", "pubsub.googleapis.com/", "storage.googleapis.com/",
                          "cloudscheduler.googleapis.com/", "secretmanager.googleapis.com/",
                          "artifactregistry.googleapis.com/", "orgpolicy.googleapis.com/",
                          "cloudquotas.googleapis.com/", "discoveryengine.googleapis.com/")

TITLES = {
    "gcp.resourceLocations": ("리소스 위치 제한", "Resource location restriction"),
    "iam.allowedPolicyMemberDomains": ("도메인 제한 공유(DRS)", "Domain restricted sharing"),
    "run.managed.requireInvokerIam": ("Cloud Run 호출자 IAM 강제", "Cloud Run invoker IAM required"),
    "run.allowedIngress": ("Cloud Run 인그레스 제한", "Cloud Run allowed ingress"),
    "run.allowedVPCEgress": ("Cloud Run VPC 송신 강제", "Cloud Run VPC egress"),
    "run.allowedBinaryAuthorizationPolicies": ("Binary Authorization 강제", "Binary Authorization required"),
    "gcp.restrictNonCmekServices": ("CMEK 강제", "CMEK required"),
    "gcp.restrictServiceUsage": ("서비스 사용 제한", "Restricted service usage"),
    "serviceuser.services": ("서비스 사용 제한(레거시)", "Restricted services (legacy)"),
    "cloudbuild.allowedWorkerPools": ("Cloud Build 워커 풀 제한", "Cloud Build worker pools"),
    "vertexai.allowedModels": ("Gemini Platform 허용 모델 정책", "Gemini Platform allowed models policy"),
    "storage.publicAccessPrevention": ("버킷 공개 차단", "Storage public access prevention"),
    "storage.uniformBucketLevelAccess": ("균일 버킷 접근", "Uniform bucket-level access"),
    "iam.disableServiceAccountKeyCreation": ("서비스 계정 키 생성 금지", "Service account key creation disabled"),
    "iam.automaticIamGrantsForDefaultServiceAccounts": ("기본 SA 자동 권한 금지",
                                                        "No automatic grants for default service accounts"),
}


def _cid(constraint: str) -> str:
    return "orgpolicy.%s" % constraint


def _fetch(run, pid: str, constraints) -> dict:
    """{constraint: (normalized|None, raw|None, err|None)} read in parallel."""
    resource = "projects/%s" % pid
    tasks = {c: (lambda c=c: run.effective_policy(resource, c)) for c in constraints}
    res = run.parallel(tasks)
    out = {}
    for c in constraints:
        ok, value = res.get(c, (False, None))
        out[c] = value if ok and isinstance(value, tuple) else (None, None, value)
    return out


def _unsupported(err) -> bool:
    """Unknown constraint (managed constraint not rolled out, typo): treat as 'not set'."""
    return getattr(err, "kind", "") in ("not_found", "invalid")


def orgpolicy_checks(run, pid: str) -> list[dict]:
    hub = run.is_hub(pid)
    constraints = (LIST_HUB + BOOLEAN_HUB + MANAGED_CONSTRAINTS) if hub else (LIST_MANAGED + MANAGED_MANAGED)
    res = _fetch(run, pid, constraints)
    readable = {c: v for c, v in res.items() if v[2] is None or _unsupported(v[2])}
    if not readable:
        err = next((v[2] for v in res.values() if v[2] is not None), None)
        run.set_fact(pid, "orgpolicy_readable", False)
        cmds = []
        if run.api_enabled(pid, "orgpolicy.googleapis.com") is False:
            cmds.append(cmd_enable(pid, ["orgpolicy.googleapis.com"]))
        return [skipped("orgpolicy.read", "orgpolicy", "조직 정책 조회", "Organization policies readable", err,
                        what="effective organization policies (roles/orgpolicy.policyViewer)", commands=cmds,
                        impact=["readiness", "model_governance"], docs_url=DOCS["orgpolicy"])]
    run.set_fact(pid, "orgpolicy_readable", True)

    def pol(name):
        norm, raw, err = res.get(name, (None, None, None))
        if err is not None and not _unsupported(err):
            return None, err
        return norm or {"set": False, "allowed": [], "denied": [], "all": "", "enforced": False}, None

    out: list[dict] = []
    out.extend(_resource_locations(run, pid, pol))
    out.extend(_drs(run, pid, pol))
    if hub:
        out.extend(_cloud_run(run, pid, pol))
        out.extend(_cloudbuild(run, pid, pol))
        out.extend(_satisfied(run, pid, pol))
    out.extend(_cmek(run, pid, pol))
    out.extend(_service_usage(run, pid, pol))
    out.extend(_allowed_models(run, pid, pol))
    return out


# -- 1. resource locations ----------------------------------------------------------

def _resource_locations(run, pid, pol) -> list[dict]:
    title = TITLES["gcp.resourceLocations"]
    p, err = pol("gcp.resourceLocations")
    cid = _cid("gcp.resourceLocations")
    if err is not None:
        return [skipped(cid, "orgpolicy", *title, err, what="constraints/gcp.resourceLocations")]
    run.set_fact(pid, "resource_locations", p)
    checks = []  # (what, location, fix flag)
    ds_loc = _dataset_location(run, pid)
    if run.is_hub(pid):
        checks.append(("Cloud Run/GCS/Secret Manager/Artifact Registry region", run.region, "--region <allowed region>"))
    if ds_loc and (run.is_hub(pid) and run.dataset_project == pid or (not run.central and not run.is_hub(pid))):
        checks.append(("BigQuery audit dataset location", ds_loc, "--dataset-location <allowed location>"))
    if not p["set"] or not checks:
        detail = "No location restriction." if not p["set"] else "No regional resources are created in this project."
        return [check(cid, "orgpolicy", "pass" if not p["set"] else "info", *title, detail)]
    blocked, unsure, okay = [], [], []
    for what, loc, fix in checks:
        verdict = loc_verdict(loc, p)
        if verdict is False:
            blocked.append((what, loc, fix))
        elif verdict is None:
            unsure.append((what, loc, fix))
        else:
            okay.append((what, loc))
    allowed_txt = ", ".join(p.get("raw_allowed", [])[:10]) or "-"
    data = {"allowed": p.get("raw_allowed", [])[:50], "denied": p.get("raw_denied", [])[:50],
            "checked": [{"what": w, "location": l} for w, l, _ in checks]}
    if blocked:
        sugg = suggest_regions(p)
        detail = "; ".join("%s '%s' is not allowed" % (w, l) for w, l, _ in blocked)
        detail += " (allowed: %s%s). Re-run with %s." % (
            allowed_txt, ("; usable e.g. " + ", ".join(sugg)) if sugg else "", " and ".join(f for _, _, f in blocked))
        return [check(cid, "orgpolicy", "fail", *title, detail, impact=["deployment", "usage"], data=data,
                      docs_url=DOCS["orgpolicy"])]
    if unsure:
        detail = "; ".join("%s '%s' cannot be confirmed" % (w, l) for w, l, _ in unsure)
        detail += " (allowed: %s). If creation fails, re-run with %s." % (allowed_txt,
                                                                           " / ".join(f for _, _, f in unsure))
        return [check(cid, "orgpolicy", "warn", *title, detail, impact=["deployment"], data=data)]
    return [check(cid, "orgpolicy", "pass", *title,
                  "Allowed: " + ", ".join("%s %s" % (w, l) for w, l in okay) + ".", data=data)]


def _dataset_location(run, pid) -> str:
    """Actual location of the configured dataset in `pid`, else the planned location (hub region)."""
    target = run.dataset_project if run.central else pid
    if target != pid:
        return ""
    meta, err = run.dataset_meta(pid, run.dataset)
    if err is None and isinstance(meta, dict) and meta.get("location"):
        return str(meta["location"]).lower()
    return run.region


# -- 2. domain restricted sharing ---------------------------------------------------------

def _drs(run, pid, pol) -> list[dict]:
    title = TITLES["iam.allowedPolicyMemberDomains"]
    cid = _cid("iam.allowedPolicyMemberDomains")
    legacy, err1 = pol("iam.allowedPolicyMemberDomains")
    managed, err2 = pol("iam.managed.allowedPolicyMembers")
    if err1 is not None and err2 is not None:
        return [skipped(cid, "orgpolicy", *title, err1, what="constraints/iam.allowedPolicyMemberDomains")]
    on_legacy = bool(legacy and (legacy["allowed"] or legacy["all"] == "DENY"))
    on_managed = bool(managed and managed["enforced"])
    if not (on_legacy or on_managed):
        return [check(cid, "orgpolicy", "pass", *title, "Domain restricted sharing is not enforced.")]
    run.set_fact(pid, "drs", True)
    which = ", ".join(n for n, on in (("iam.allowedPolicyMemberDomains", on_legacy),
                                      ("iam.managed.allowedPolicyMembers", on_managed)) if on)
    if run.is_hub(pid):
        run.adapt("drs", True)
        run.adapt("no_invoker_iam_check", True)
        run.adapt("allow_unauthenticated", False)
        return [check(cid, "orgpolicy", "warn", *title,
                      "%s enforced: an allUsers run.invoker binding (--allow-unauthenticated) is impossible. The "
                      "installer deploys with --no-invoker-iam-check instead; the API itself still requires an API "
                      "key or a Google OIDC token. If granting Log Router writer identities across projects is "
                      "refused, ask the organization admin for an exception on the hub dataset/topic." % which,
                      impact=["deployment"], docs_url=DOCS["drs"],
                      data={"adapt": {"no_invoker_iam_check": True, "allow_unauthenticated": False}}),
                ]
    return [check(cid, "orgpolicy", "info", *title,
                  "%s enforced on %s: only principals from allowed customers can be granted roles; the hub service "
                  "accounts belong to the organization and Google-managed writer identities are used for sinks."
                  % (which, pid))]


# -- 3-6. Cloud Run constraints (hub) -------------------------------------------------------

def _cloud_run(run, pid, pol) -> list[dict]:
    out = []
    # 3. invoker IAM
    inv, err = pol("run.managed.requireInvokerIam")
    title = TITLES["run.managed.requireInvokerIam"]
    cid = _cid("run.managed.requireInvokerIam")
    if err is not None:
        out.append(skipped(cid, "orgpolicy", *title, err, what="constraints/run.managed.requireInvokerIam"))
    elif inv["enforced"]:
        run.adapt("invoker_iam_required", True)
        run.adapt("no_invoker_iam_check", False)
        if run.fact(pid, "drs"):
            run.adapt("allow_unauthenticated", False)
            out.append(check(cid, "orgpolicy", "warn", *title,
                             "run.managed.requireInvokerIam + domain restricted sharing: the service can be neither "
                             "public (allUsers) nor deployed with --no-invoker-iam-check. Only callers presenting a "
                             "Google-signed ID token with roles/run.invoker reach the API (OIDC service-account "
                             "callers work; API-key-only portals need an external HTTPS load balancer + IAP or must "
                             "obtain Google ID tokens).", impact=["external_access"],
                             data={"adapt": {"invoker_iam_required": True, "allow_unauthenticated": False}}))
        else:
            run.adapt("allow_unauthenticated", True)
            out.append(check(cid, "orgpolicy", "warn", *title,
                             "run.managed.requireInvokerIam: --no-invoker-iam-check is not allowed; the installer "
                             "grants allUsers roles/run.invoker instead (the API still authenticates every call).",
                             impact=["deployment"], data={"adapt": {"invoker_iam_required": True,
                                                                    "allow_unauthenticated": True}}))
    else:
        out.append(check(cid, "orgpolicy", "pass", *title, "Not enforced."))

    # 4. ingress
    ing, err = pol("run.allowedIngress")
    title = TITLES["run.allowedIngress"]
    cid = _cid("run.allowedIngress")
    if err is not None:
        out.append(skipped(cid, "orgpolicy", *title, err, what="constraints/run.allowedIngress"))
    else:
        allowed, denied, allv = ing["allowed"], ing["denied"], ing["all"]
        restricted = (allowed and "all" not in allowed) or "all" in denied or allv == "DENY"
        if not restricted:
            out.append(check(cid, "orgpolicy", "pass", *title, "External ingress (all) is allowed."))
        else:
            options = [v for v in ("internal-and-cloud-load-balancing", "internal") if v in allowed and v not in denied]
            if allv == "DENY" or not options:
                out.append(check(cid, "orgpolicy", "fail", *title,
                                 "run.allowedIngress allows no usable ingress (%s): the API cannot receive requests. Ask "
                                 "the organization admin for an exception on the hub project." % (", ".join(allowed) or "-"),
                                 impact=["deployment", "external_access"], docs_url=DOCS["run_ingress"]))
            else:
                run.adapt("ingress", options[0])
                out.append(check(cid, "orgpolicy", "warn", *title,
                                 "External ingress is not allowed; the installer deploys with --ingress=%s. External "
                                 "portals must call through an external Application Load Balancer (internal-and-cloud-"
                                 "load-balancing) or from inside the VPC; Pub/Sub push and Cloud Scheduler from the hub "
                                 "project keep working." % options[0], impact=["external_access"],
                                 data={"adapt": {"ingress": options[0]}}, docs_url=DOCS["run_ingress"]))

    # 5. VPC egress
    eg, err = pol("run.allowedVPCEgress")
    title = TITLES["run.allowedVPCEgress"]
    cid = _cid("run.allowedVPCEgress")
    if err is not None:
        out.append(skipped(cid, "orgpolicy", *title, err, what="constraints/run.allowedVPCEgress"))
    elif eg["allowed"] and "private-ranges-only" not in eg["allowed"]:
        run.adapt("vpc_egress_required", True)
        run.set_fact(pid, "vpc_egress_required", True)
        if run.vpc_network and run.vpc_subnet:
            out.append(check(cid, "orgpolicy", "warn", *title,
                             "run.allowedVPCEgress=%s: the installer routes all egress through %s/%s (--vpc-egress="
                             "all-traffic). The subnet needs Private Google Access, and outbound webhooks need Cloud "
                             "NAT (see network.* checks)." % (",".join(eg["allowed"]), run.vpc_network, run.vpc_subnet),
                             impact=["webhooks"], data={"adapt": {"vpc_egress": "all-traffic"}},
                             docs_url=DOCS["run_egress"]))
        else:
            out.append(check(cid, "orgpolicy", "fail", *title,
                             "run.allowedVPCEgress=%s: Cloud Run must send all egress through a VPC, but no "
                             "hub.vpc_network / hub.vpc_subnet is configured. Re-run with --vpc-network NETWORK "
                             "--vpc-subnet SUBNET (subnet with Private Google Access and Cloud NAT)."
                             % ",".join(eg["allowed"]), impact=["deployment"], docs_url=DOCS["run_egress"]))
    else:
        out.append(check(cid, "orgpolicy", "pass", *title, "No VPC egress requirement."))

    # 6. Binary Authorization
    ba, err = pol("run.allowedBinaryAuthorizationPolicies")
    title = TITLES["run.allowedBinaryAuthorizationPolicies"]
    cid = _cid("run.allowedBinaryAuthorizationPolicies")
    if err is not None:
        out.append(skipped(cid, "orgpolicy", *title, err, what="constraints/run.allowedBinaryAuthorizationPolicies"))
    elif ba["allowed"]:
        policy = (ba.get("raw_allowed") or ["default"])[0]
        run.adapt("binary_authorization", policy)
        out.append(check(cid, "orgpolicy", "warn", *title,
                         "Cloud Run revisions must name a Binary Authorization policy; the installer deploys with "
                         "--binary-authorization=%s. If that policy does not admit the image, request an attestation "
                         "or exception from the security team." % policy, impact=["deployment"],
                         data={"adapt": {"binary_authorization": policy}}, docs_url=DOCS["binauthz"]))
    else:
        out.append(check(cid, "orgpolicy", "pass", *title, "Not required."))
    return out


def _cloudbuild(run, pid, pol) -> list[dict]:
    wp, err = pol("cloudbuild.allowedWorkerPools")
    title = TITLES["cloudbuild.allowedWorkerPools"]
    cid = _cid("cloudbuild.allowedWorkerPools")
    if err is not None:
        return [skipped(cid, "orgpolicy", *title, err, what="constraints/cloudbuild.allowedWorkerPools")]
    if wp["allowed"] or wp["all"] == "DENY":
        run.adapt("docker_build", True)
        return [check(cid, "orgpolicy", "warn", *title,
                      "The default Cloud Build pool is not allowed (%s). The installer builds with local Docker "
                      "(Cloud Shell has it), or pass --worker-pool projects/P/locations/R/workerPools/POOL, or a "
                      "prebuilt --image." % (", ".join(wp.get("raw_allowed", [])[:3]) or "deny all"),
                      impact=["deployment"], data={"adapt": {"docker_build": True}})]
    return [check(cid, "orgpolicy", "pass", *title, "Default Cloud Build pool allowed.")]


def _satisfied(run, pid, pol) -> list[dict]:
    out = []
    for name in BOOLEAN_HUB:
        p, err = pol(name)
        if err is not None:
            continue
        title = TITLES[name]
        if p["enforced"]:
            out.append(check(_cid(name), "orgpolicy", "info", *title,
                             "%s is enforced; this product complies (no service account keys, private uniform "
                             "buckets, explicit least-privilege grants)." % name))
    return out


# -- 7. CMEK ------------------------------------------------------------------------------------

def _cmek(run, pid, pol) -> list[dict]:
    p, err = pol("gcp.restrictNonCmekServices")
    title = TITLES["gcp.restrictNonCmekServices"]
    cid = _cid("gcp.restrictNonCmekServices")
    if err is not None:
        return [skipped(cid, "orgpolicy", *title, err, what="constraints/gcp.restrictNonCmekServices")]
    if run.is_hub(pid):
        relevant = set(CMEK_SERVICES_HUB)
    elif not run.central:
        relevant = set(CMEK_SERVICES_DATASET)
    else:
        relevant = set()
    need = sorted(relevant if p["all"] == "DENY" else (set(p["denied"]) & relevant))
    if need:
        run.adapt("cmek_required", need)
        return [check(cid, "orgpolicy", "fail", *title,
                      "Resources of %s cannot be created without customer-managed encryption keys (CMEK). The "
                      "installer does not provision CMEK; ask for a project exception or pre-create CMEK-protected "
                      "resources and adopt them (--reuse-dataset)." % ", ".join(need),
                      impact=["deployment"], docs_url=DOCS["cmek"], data={"services": need})]
    if p["set"]:
        return [check(cid, "orgpolicy", "info", *title, "Set, but does not cover services this product creates here.")]
    return [check(cid, "orgpolicy", "pass", *title, "Not enforced.")]


# -- 8. restricted services ----------------------------------------------------------------------

def _service_usage(run, pid, pol) -> list[dict]:
    expected = HUB_APIS if run.is_hub(pid) else managed_apis(run)
    required = {s for s, lvl, _, _ in expected if lvl == "required"}
    other = {s: what for s, lvl, _, what in expected if lvl != "required"}
    out = []
    for name in ("gcp.restrictServiceUsage", "serviceuser.services"):
        p, err = pol(name)
        title = TITLES[name]
        cid = _cid(name)
        if err is not None:
            out.append(skipped(cid, "orgpolicy", *title, err, what="constraints/%s" % name))
            continue
        if not (p["allowed"] or p["denied"] or p["all"] == "DENY"):
            out.append(check(cid, "orgpolicy", "pass", *title, "No service restriction."))
            continue
        blocked = set(p["denied"])
        if p["all"] == "DENY":
            blocked |= required | set(other)
        elif p["allowed"]:
            blocked |= {a for a in required | set(other) if a not in p["allowed"]}
        req = sorted(blocked & required)
        opt = sorted(blocked & set(other))
        if req:
            out.append(check(cid, "orgpolicy", "fail", *title,
                             "Required APIs are denied by %s: %s. Ask the organization admin to allow them for %s."
                             % (name, ", ".join(req), pid), impact=["deployment"], data={"denied_required": req,
                                                                                       "denied_optional": opt}))
        elif opt:
            out.append(check(cid, "orgpolicy", "warn", *title,
                             "Optional APIs are denied by %s: %s (features degrade: %s)."
                             % (name, ", ".join(opt), "; ".join(other[o] for o in opt)),
                             data={"denied_optional": opt}))
        else:
            out.append(check(cid, "orgpolicy", "pass", *title, "Restricted, but every API this product uses is allowed."))
    return out


# -- 9. vertexai.allowedModels (model governance) ------------------------------------------------

def _allowed_models(run, pid, pol) -> list[dict]:
    p, err = pol("vertexai.allowedModels")
    title = TITLES["vertexai.allowedModels"]
    cid = _cid("vertexai.allowedModels")
    if err is not None:
        return [skipped(cid, "orgpolicy", *title, err, what="constraints/vertexai.allowedModels")]
    local, lerr = run.try_call("GET", "%s/projects/%s/policies/vertexai.allowedModels" % (ORGPOLICY_V2, pid),
                               project_hint=pid)
    has_local = lerr is None and isinstance(local, dict) and bool(local.get("spec") or local.get("dryRunSpec"))
    local_norm = None
    if has_local:
        local_norm = normalize_policy(local)
    data = {
        "effective": {"allowed": p.get("raw_allowed", [])[:100], "denied": p.get("raw_denied", [])[:100],
                      "all": p.get("all") or "", "set": p.get("set")},
        "project_policy": bool(has_local),
        "inherit_from_parent": bool(local_norm and local_norm.get("inherit_from_parent")),
        "set_at_ancestor": bool(p.get("set") and not has_local),
    }
    run.set_fact(pid, "allowed_models", data)
    if not p.get("set"):
        detail = "Not set: every Gemini Platform model is allowed. The model governance API manages this constraint."
    elif data["set_at_ancestor"]:
        detail = ("Effective values come from a folder/organization policy (%d allowed, %d denied, all=%s). Project-"
                  "level policies written by the model governance API merge with it when inheritFromParent=true "
                  "(parent denied values always win) or replace it when false." %
                  (len(data["effective"]["allowed"]), len(data["effective"]["denied"]), data["effective"]["all"] or "-"))
    else:
        detail = ("Project-level policy present (%d allowed, %d denied, inheritFromParent=%s)." %
                  (len(data["effective"]["allowed"]), len(data["effective"]["denied"]), data["inherit_from_parent"]))
    return [check(cid, "orgpolicy", "info", *title, detail, impact=["model_governance"], data=data,
                  docs_url=DOCS["vertex_models"])]


# -- organization custom constraints (cross-project) -----------------------------------------------

def custom_constraints_check(run, org: str) -> dict:
    title = ("조직 커스텀 제약조건", "Organization custom constraints")
    cid = "orgpolicy.customConstraints"
    if not org:
        return check(cid, "orgpolicy", "skip", *title, "No organization: custom constraints do not apply.")
    try:
        items, _ = run.paginate("%s/organizations/%s/customConstraints" % (ORGPOLICY_V2, org), "customConstraints",
                                page_size=100, max_pages=5)
    except Exception as err:  # pylint: disable=broad-except
        return skipped(cid, "orgpolicy", *title, err, what="organizations/%s/customConstraints" % org)
    hits = []
    for item in items:
        types = [str(t) for t in item.get("resourceTypes") or []]
        matched = [t for t in types if t.startswith(PRODUCT_RESOURCE_TYPES)]
        if matched:
            hits.append({"name": str(item.get("name") or "").rsplit("/", 1)[-1], "resource_types": matched,
                         "action": str(item.get("actionType") or ""), "methods": item.get("methodTypes") or []})
    if not hits:
        return check(cid, "orgpolicy", "pass", *title,
                     "%d custom constraint(s); none targets resource types this product creates." % len(items))
    return check(cid, "orgpolicy", "warn", *title,
                 "%d custom constraint(s) target resource types this product creates or updates (%s). If they are "
                 "enforced on the hub/managed projects, creation of those resources may be refused; review their "
                 "conditions." % (len(hits), ", ".join(h["name"] for h in hits[:10])),
                 impact=["deployment"], data={"constraints": hits[:50]})
