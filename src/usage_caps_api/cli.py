"""CLI for managing M2M API keys and running local dry-run diagnostics.

Examples:
    python -m usage_caps_api.cli keys create --name "enterprise-portal" --scopes usage:read,caps:read,caps:write,users:write
    python -m usage_caps_api.cli keys list
    python -m usage_caps_api.cli keys revoke <key_id>
"""

from __future__ import annotations

import argparse
import json
import sys

from . import auth
from .app import App


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m usage_caps_api.cli",
                                     description="GCP AI Usage & Spend Caps API administration CLI.")
    sub = parser.add_subparsers(dest="command", required=True)

    keys_p = sub.add_parser("keys", help="Manage M2M API keys (stored hashed only)")
    keys_sub = keys_p.add_subparsers(dest="keys_action", required=True)

    create_p = keys_sub.add_parser("create", help="Issue a new scoped API key (plaintext printed once)")
    create_p.add_argument("--name", required=True, help="Human-readable label for the key")
    create_p.add_argument("--scopes", required=True,
                          help="Comma-separated scopes: %s" % ",".join(auth.ALL_SCOPES))
    create_p.add_argument("--expires-at", default=None, help="Optional RFC3339 expiration timestamp")

    keys_sub.add_parser("list", help="List API keys (metadata only, never plaintext)")

    revoke_p = keys_sub.add_parser("revoke", help="Revoke an API key by ID")
    revoke_p.add_argument("key_id", help="API key ID (12 hex characters)")
    return parser


def main(argv=None, *, app: App | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    app = app or App()
    if args.command == "keys":
        if args.keys_action == "create":
            scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
            created = auth.create_api_key(
                app.store, name=args.name, scopes=scopes, created_by="cli", expires_at=args.expires_at,
            )
            print(json.dumps(created, indent=2, ensure_ascii=False))
            return 0
        if args.keys_action == "list":
            print(json.dumps(auth.list_api_keys(app.store), indent=2, ensure_ascii=False))
            return 0
        if args.keys_action == "revoke":
            revoked = auth.revoke_api_key(app.store, args.key_id, revoked_by="cli")
            print(json.dumps(revoked, indent=2, ensure_ascii=False))
            return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
