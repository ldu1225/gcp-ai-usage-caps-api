"""Principal identity normalization for Google Accounts and Workforce Identity Federation.

A person is either:
- a Google Account (Cloud Identity / Google Workspace email), formatted as `user:EMAIL` in allow
  policies and `principal://goog/subject/EMAIL` in IAM deny policies; or
- a Workforce Identity Federation principal URI:
  `principal://iam.googleapis.com/locations/global/workforcePools/POOL_ID/subject/SUBJECT`.

Service accounts are not subjects by default. When `hub.service_account_subjects` is enabled (env
SERVICE_ACCOUNT_SUBJECTS=true) a service account is treated like a user: usage is attributed to it from the
inference log principal, it can have caps / rules / blocks and it is denied as
`principal://iam.googleapis.com/projects/-/serviceAccounts/EMAIL`. Use this for portal back ends, batch jobs and for
end-to-end verification with a dedicated test service account. Groups, domains and principalSets are never subjects.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+$")
GOOGLE_USER = "google_user"
WORKFORCE = "workforce"
SERVICE_ACCOUNT = "service_account"
OTHER = "other"

WORKFORCE_PREFIX = "principal://iam.googleapis.com/locations/global/workforcepools/"
WORKFORCE_URI_PREFIX = "principal://iam.googleapis.com/locations/global/workforcePools/"
GOOGLE_SUBJECT_PREFIX = "principal://goog/subject/"
SA_PRINCIPAL_PREFIX = "principal://iam.googleapis.com/projects/-/serviceAccounts/"
_SA_EMAIL_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{4,61}@[a-z0-9\-.]+\.gserviceaccount\.com$")
_STATE = {"service_account_subjects": False, "protected": frozenset()}


def configure(*, service_account_subjects: bool = False, protected=()) -> None:
    """Process-wide identity options (set once from config at App start). `protected` service accounts (the hub's
    runtime / enforcer identities) are never subjects."""
    _STATE["service_account_subjects"] = bool(service_account_subjects)
    _STATE["protected"] = frozenset(str(p).strip().lower() for p in protected or () if p)


def service_account_subjects() -> bool:
    return bool(_STATE["service_account_subjects"])
WORKFORCE_RE = re.compile(
    r"^principal://iam\.googleapis\.com/locations/global/workforcePools/([A-Za-z0-9][A-Za-z0-9\-]{1,62})"
    r"/subject/([^\s\x00-\x1f\x7f]{1,512})$",
    re.IGNORECASE,
)
_USER_PREFIX_RE = re.compile(r"^user:", re.IGNORECASE)
_SA_PREFIX_RE = re.compile(r"^serviceaccount:", re.IGNORECASE)
UID_SALT = "gcp-ai-caps-uid-v1"


@dataclass(frozen=True)
class Principal:
    kind: str
    key: str
    display: str

    @property
    def is_person(self) -> bool:
        """True for every principal that can be a policy subject (users, workforce, opt-in service accounts)."""
        if self.kind == SERVICE_ACCOUNT:
            return bool(_STATE["service_account_subjects"]) and self.key not in _STATE["protected"]
        return self.kind in (GOOGLE_USER, WORKFORCE)


def _workforce(value: str) -> Principal | None:
    m = WORKFORCE_RE.match(value)
    if not m:
        return None
    pool, subject = m.group(1).lower(), m.group(2)
    return Principal(WORKFORCE, "%s%s/subject/%s" % (WORKFORCE_PREFIX, pool, subject.lower()), subject)


def parse(value) -> Principal | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.lower().startswith("principal://"):
        if raw.lower().startswith(SA_PRINCIPAL_PREFIX.lower()):
            email = raw[len(SA_PRINCIPAL_PREFIX):].strip().lower()
            if _SA_EMAIL_RE.match(email):
                return Principal(SERVICE_ACCOUNT, email, email)
            return Principal(OTHER, raw, raw)
        if raw.lower().startswith(GOOGLE_SUBJECT_PREFIX):
            email = raw[len(GOOGLE_SUBJECT_PREFIX):].strip().lower()
            if EMAIL_RE.match(email) and not email.endswith(".gserviceaccount.com"):
                return Principal(GOOGLE_USER, email, email)
            return Principal(OTHER, raw, raw)
        found = _workforce(raw)
        return found if found is not None else Principal(OTHER, raw, raw)
    if _SA_PREFIX_RE.match(raw):
        email = raw.split(":", 1)[1].strip().lower()
        return Principal(SERVICE_ACCOUNT, email, email)
    candidate = _USER_PREFIX_RE.sub("", raw).strip()
    if ":" in candidate.partition("@")[0]:
        return Principal(OTHER, raw, raw)
    email = candidate.lower()
    if email.endswith(".gserviceaccount.com") and "@" in email:
        return Principal(SERVICE_ACCOUNT, email, email)
    if EMAIL_RE.match(email):
        return Principal(GOOGLE_USER, email, email)
    return Principal(OTHER, raw, raw)


def person_key(value) -> str | None:
    found = parse(value)
    return found.key if (found is not None and found.is_person) else None


def is_person_key(key) -> bool:
    found = parse(key)
    return bool(found is not None and found.is_person and found.key == key)


def is_workforce_key(key) -> bool:
    return isinstance(key, str) and key.lower().startswith(WORKFORCE_PREFIX) and WORKFORCE_RE.match(key) is not None


def is_service_account(principal) -> bool:
    return isinstance(principal, str) and principal.lower().endswith(".gserviceaccount.com")


def is_human(principal) -> bool:
    if not isinstance(principal, str) or "@" not in principal:
        return False
    low = principal.lower()
    return not low.endswith(".gserviceaccount.com") and not low.startswith("principal://")


def _workforce_uri(found: Principal) -> str:
    pool = found.key[len(WORKFORCE_PREFIX):].split("/subject/", 1)[0]
    return "%s%s/subject/%s" % (WORKFORCE_URI_PREFIX, pool, found.display)


def allow_member(value) -> str | None:
    found = parse(value)
    if found is None or not found.is_person:
        return None
    if found.kind == SERVICE_ACCOUNT:
        return "serviceAccount:%s" % found.key
    return "user:%s" % found.key if found.kind == GOOGLE_USER else _workforce_uri(found)


def deny_principal(value) -> str | None:
    found = parse(value)
    if found is None or not found.is_person:
        return None
    if found.kind == SERVICE_ACCOUNT:
        return SA_PRINCIPAL_PREFIX + found.key
    return GOOGLE_SUBJECT_PREFIX + found.key if found.kind == GOOGLE_USER else _workforce_uri(found)


def uid_for(email: str) -> str:
    return hashlib.sha256(((email or "").strip().lower() + UID_SALT).encode("utf-8")).hexdigest()[:16]


def sql_person_key(user_expr: str) -> str:
    return "LOWER(REGEXP_REPLACE(TRIM(%s), r'(?i)^user:', ''))" % user_expr
