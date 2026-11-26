"""Unit tests for M2M API Key hashing/scopes, GCP OIDC Bearer verification, and CLI."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout

from tests.unit.support import FakeOidcVerifier, LiveTestServer, build_test_app, make_test_config
from usage_caps_api import auth, cli


class AuthAndCliTests(unittest.TestCase):
    def test_api_key_stored_hashed_never_plaintext_and_scopes_enforced(self):
        app, _ = build_test_app()
        created = auth.create_api_key(app.store, name="portal-reader", scopes=["usage:read"])
        plaintext = created["key"]
        self.assertTrue(plaintext.startswith("gcpai_"))

        # Verify plaintext secret is never persisted in store document
        raw_doc, _ = app.store.load(auth.API_KEYS_DOC)
        serialized = json.dumps(raw_doc)
        self.assertNotIn(plaintext, serialized)
        self.assertNotIn(plaintext.split("_", 2)[2], serialized)

        with LiveTestServer(app) as srv:
            # Unauthenticated -> 401
            st, body, _ = srv.request("GET", "/v1/usage/users")
            self.assertEqual(st, 401)
            self.assertEqual(body["error"]["code"], "unauthenticated")

            # usage:read key allowed on /v1/usage/users
            st, body, _ = srv.request("GET", "/v1/usage/users", token=plaintext)
            self.assertEqual(st, 200)
            self.assertEqual(body["object"], "list")

            # usage:read key forbidden on /v1/caps/policy (needs caps:read)
            st, body, _ = srv.request("GET", "/v1/caps/policy", token=plaintext)
            self.assertEqual(st, 403)
            self.assertEqual(body["error"]["code"], "insufficient_scope")

            # Revoke key via admin bootstrap key
            st, rev, _ = srv.request("DELETE", "/v1/admin/api-keys/%s" % created["id"],
                                     token="gcpai_boot_secret_1234567890")
            self.assertEqual(st, 200)
            self.assertEqual(rev["status"], "revoked")

            # Revoked key immediately rejected -> 401
            st, body, _ = srv.request("GET", "/v1/usage/users", token=plaintext)
            self.assertEqual(st, 401)

    def test_expired_api_key_rejected(self):
        app, _ = build_test_app()
        created = auth.create_api_key(
            app.store, name="expired-key", scopes=["usage:read"], expires_at="2020-01-01T00:00:00Z",
        )
        with LiveTestServer(app) as srv:
            st, body, _ = srv.request("GET", "/v1/usage/users", token=created["key"])
            self.assertEqual(st, 401)
            self.assertIn("expired", body["error"]["message"].lower())

    def test_gcp_oidc_bearer_verification_and_allowlist(self):
        verifier = FakeOidcVerifier({
            "hdr.valid.sig": "portal-sa@test-ai-project.iam.gserviceaccount.com",
            "hdr.rogue.sig": "rogue-sa@other-project.iam.gserviceaccount.com",
        })
        cfg = make_test_config(auth={
            "public_base_url": "https://caps-api.example.com",
            "oidc_allowed_service_accounts": ["portal-sa@test-ai-project.iam.gserviceaccount.com"],
            "oidc_default_scopes": ["usage:read", "caps:read", "caps:write"],
        })
        app, _ = build_test_app(cfg=cfg, verifier=verifier)

        with LiveTestServer(app) as srv:
            # Allowlisted OIDC SA works
            st, body, _ = srv.request("GET", "/v1/caps/policy", token="hdr.valid.sig")
            self.assertEqual(st, 200)
            self.assertEqual(body["object"], "cap_policy")

            # Unlisted OIDC SA -> 403
            st, body, _ = srv.request("GET", "/v1/caps/policy", token="hdr.rogue.sig")
            self.assertEqual(st, 403)

            # Invalid JWT -> 401
            st, body, _ = srv.request("GET", "/v1/caps/policy", token="hdr.bad.sig")
            self.assertEqual(st, 401)

    def test_cli_keys_create_list_revoke(self):
        app, _ = build_test_app()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["keys", "create", "--name", "cli-test", "--scopes", "usage:read,caps:read"], app=app)
        self.assertEqual(rc, 0)
        created = json.loads(buf.getvalue())
        self.assertIn("key", created)
        key_id = created["id"]

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["keys", "list"], app=app)
        self.assertEqual(rc, 0)
        listed = json.loads(buf.getvalue())
        self.assertEqual(len(listed), 1)
        self.assertNotIn("key", listed[0])

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["keys", "revoke", key_id], app=app)
        self.assertEqual(rc, 0)
        revoked = json.loads(buf.getvalue())
        self.assertEqual(revoked["status"], "revoked")


if __name__ == "__main__":
    unittest.main()
