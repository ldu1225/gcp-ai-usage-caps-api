"""Long-running operation records for eventually-consistent changes (Org Policy, IAM Deny, quota preferences).

Google Cloud applies several governance changes asynchronously:
  * Organization Policy (constraints/vertexai.allowedModels): usually minutes, up to ~15 minutes.
  * IAM Deny v2 policies: typically 1-2 minutes (eventually consistent).
  * Cloud Quotas preferences: decreases are applied quickly; increases may need Google review (hours to days).

Every such write returns an `operation` object (long-running operation polling): GET /v1/operations/{id}.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets

from ..httpkit import ApiError, iso_z, now_iso, paginate_list
from ..timeutil import parse_rfc3339

OPERATIONS_DOC = "operations"
OPERATION_ID_RE = re.compile(r"^op_[0-9a-f]{16}$")
MAX_OPERATIONS = 1000
PROPAGATION = {
    "model_access": dt.timedelta(minutes=15),
    "ge_models": dt.timedelta(minutes=2),
    "iam_deny": dt.timedelta(minutes=2),
    "quota": dt.timedelta(hours=48),
}


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def create(ctx, kind: str, *, target: str, detail: str, result: dict | None = None, status: str = "propagating",
           verify: dict | None = None) -> dict:
    """Persists and returns a new operation record."""
    now = utcnow()
    op = {
        "object": "operation",
        "id": "op_" + secrets.token_hex(8),
        "kind": kind,
        "target": target,
        "status": status,
        "detail": detail,
        "created_at": iso_z(now),
        "updated_at": iso_z(now),
        "expected_effective_by": iso_z(now + PROPAGATION.get(kind.split(".", 1)[0], dt.timedelta(minutes=5))),
        "created_by": getattr(ctx.caller, "id", None),
        "result": result or {},
        "verify": verify or {},
    }

    def save(doc):
        items = [o for o in (doc.get("operations") or []) if isinstance(o, dict)]
        items.append(op)
        doc["operations"] = items[-MAX_OPERATIONS:]
        return op

    ctx.store.mutate(OPERATIONS_DOC, save)
    return public_view(op)


def update(ctx, op_id: str, **fields) -> dict | None:
    out = {"op": None}

    def save(doc):
        for o in doc.get("operations") or []:
            if isinstance(o, dict) and o.get("id") == op_id:
                o.update(fields)
                o["updated_at"] = now_iso()
                out["op"] = dict(o)
        return doc

    ctx.store.mutate(OPERATIONS_DOC, save)
    return out["op"]


def public_view(op: dict) -> dict:
    return {k: v for k, v in op.items() if k != "verify"}


def _find(ctx, op_id: str) -> dict | None:
    doc, _ = ctx.store.load(OPERATIONS_DOC)
    return next((dict(o) for o in (doc.get("operations") or []) if isinstance(o, dict) and o.get("id") == op_id), None)


def get_operation(ctx) -> dict:
    """GET /v1/operations/{operation_id}: refreshes propagating operations whose window has elapsed."""
    op_id = str(ctx.path_params.get("operation_id") or "").strip()
    if not OPERATION_ID_RE.match(op_id):
        raise ApiError("bad_request", "Invalid operation id.")
    op = _find(ctx, op_id)
    if op is None:
        raise ApiError("not_found", "Operation '%s' not found." % op_id)
    if op.get("status") == "propagating":
        due = parse_rfc3339(op.get("expected_effective_by"))
        verifier = VERIFIERS.get(op.get("kind", "").split(".", 1)[0])
        verdict = None
        if verifier is not None:
            try:
                verdict = verifier(ctx, op)
            except Exception:  # pylint: disable=broad-except
                verdict = None
        if verdict == "succeeded" or (verdict is None and due is not None and utcnow() >= due):
            op = update(ctx, op_id, status="succeeded") or op
        elif verdict == "failed":
            op = update(ctx, op_id, status="failed") or op
    return public_view(op)


def list_operations(ctx) -> dict:
    """GET /v1/operations?kind=&status="""
    doc, _ = ctx.store.load(OPERATIONS_DOC)
    items = [public_view(o) for o in reversed(doc.get("operations") or []) if isinstance(o, dict)]
    kind = (ctx.params.get("kind") or "").strip()
    status = (ctx.params.get("status") or "").strip()
    if kind:
        items = [o for o in items if str(o.get("kind", "")).startswith(kind)]
    if status:
        items = [o for o in items if o.get("status") == status]
    return paginate_list(items, ctx.params, default_limit=50, max_limit=200)


# kind prefix -> callable(ctx, op) -> "succeeded" | "failed" | None (unknown yet). Registered by services.
VERIFIERS: dict = {}
