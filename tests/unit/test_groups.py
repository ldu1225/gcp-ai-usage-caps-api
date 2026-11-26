"""Groups are first-class policy subjects: caps (per-member and shared pool), block/unblock, exclusions, member
removal, model rules, Google Group sync and usage roll-ups - with the same multi-project semantics as users."""

from __future__ import annotations

import base64
import datetime as dt
import json
import unittest

from tests.unit.governance_fakes import GovernanceFakeGcp
from tests.unit.support import LiveTestServer, build_test_app, make_inference_entry, make_test_config
from usage_caps_api import config
from usage_caps_api.services import usage_events

ADMIN = "gcpai_boot_secret_1234567890"
HUB = "test-ai-project"
TEAM_B = "team-b-project"


def _q(email: str) -> str:
    return email.replace("@", "%40")


class _GroupBase(unittest.TestCase):
    cfg_overrides = {"caps": {"enforcement_mode": "auto", "change_budget_min": 20, "surge_guard_min": 50},
                     "pricing": {"model_overrides": [{"model": "gemini-test-pro", "input": 1.0, "output": 10.0}]}}

    def setUp(self):
        usage_events.reset()
        self.hooks = []

        def sender(url, raw, headers):
            self.hooks.append(json.loads(raw.decode("utf-8")))
            return 200

        cfg = make_test_config(**config.deep_merge({"workspace": {"projects": [TEAM_B]}}, self.cfg_overrides))
        self.app, self.gcp = build_test_app(cfg=cfg, gcp=GovernanceFakeGcp(), webhook_sender=sender)
        from usage_caps_api.services import webhooks as wh

        def add_hook(doc):
            doc["webhooks"] = [{"id": "wh_000000000001", "name": "portal", "url": "https://portal.example.com/hook",
                                "events": list(wh.SUPPORTED_EVENTS), "enabled": True, "secret": "whsec_test"}]
            return doc
        self.app.store.mutate("webhooks", add_hook)
        self.srv = LiveTestServer(self.app).__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)

    def req(self, method, path, body=None):
        return self.srv.request(method, path, token=ADMIN, body=body)[:2]

    def events(self, name):
        return [h for h in self.hooks if h["event"] == name]

    def denied_at(self, project):
        out = set()
        for url, pol in self.gcp.deny_policies.items():
            if ("projects%%2F%s/" % project) in url:
                for rule in pol.get("rules") or []:
                    out |= set((rule.get("denyRule") or {}).get("deniedPrincipals") or [])
        return out

    def is_denied(self, email, project=HUB):
        return any(p.endswith("/" + email) or p.endswith(":" + email) for p in self.denied_at(project))

    def spend(self, *emails, project=HUB):
        """Each listed user spends $1.50 (500k input + 100k output tokens of gemini-test-pro)."""
        now = dt.datetime.now(dt.timezone.utc)
        for i, em in enumerate(emails):
            self.gcp.log_entries.append(make_inference_entry(em, project=project, prompt=500_000, cand=100_000, ts=now,
                                                             insert_id="%s-%s-%d" % (em, project, i)))

    def group(self, gid, members, **extra):
        st, g = self.req("POST", "/v1/groups", dict({"id": gid, "members": members}, **extra))
        self.assertEqual(st, 201, g)
        return g


class GroupCrudTests(_GroupBase):
    def test_create_list_members_exclusions_and_validation(self):
        g = self.group("eng", ["a@company.com", "b@company.com"], name="Engineering", priority=10)
        self.assertEqual((g["object"], g["member_count"], g["priority"]), ("group", 2, 10))
        self.assertEqual(self.req("POST", "/v1/groups", {"id": "eng"})[0], 409)
        self.assertEqual(self.req("POST", "/v1/groups", {"id": "Bad Id"})[0], 400)
        self.assertEqual(self.req("POST", "/v1/groups", {"id": "x1", "members": ["not-an-email"]})[0], 400)
        st, g = self.req("POST", "/v1/groups/eng/members", {"add": ["c@company.com"], "remove": ["a@company.com"]})
        self.assertEqual(st, 200, g)
        self.assertEqual(g["effective_members"], ["b@company.com", "c@company.com"])
        self.assertEqual((g["effective_added"], g["effective_removed"]), (["c@company.com"], ["a@company.com"]))
        st, g = self.req("POST", "/v1/groups/eng/exclusions", {"add": ["c@company.com"]})
        self.assertEqual(g["effective_members"], ["b@company.com"])
        self.assertEqual(g["excluded"], ["c@company.com"])
        self.assertTrue(self.events("group.membership_changed"))
        self.group("all-staff", ["b@company.com"], priority=200)
        st, page = self.req("GET", "/v1/groups?member=b%40company.com")
        self.assertEqual([r["id"] for r in page["data"]], ["eng", "all-staff"])  # priority order
        st, ug = self.req("GET", "/v1/users/b%40company.com/groups")
        self.assertEqual([r["id"] for r in ug["groups"]], ["eng", "all-staff"])
        st, ug = self.req("GET", "/v1/users/c%40company.com/groups")
        self.assertEqual((ug["groups"], ug["excluded_from"]), ([], ["eng"]))
        st, g = self.req("PATCH", "/v1/groups/eng", {"priority": 300, "description": "x"})
        self.assertEqual((st, g["priority"]), (200, 300))
        self.assertEqual(self.req("GET", "/v1/groups/nope")[0], 404)
        st, d = self.req("DELETE", "/v1/groups/eng")
        self.assertTrue(d["deleted"])
        self.assertEqual(self.req("GET", "/v1/groups/eng")[0], 404)

    def test_scopes(self):
        st, key = self.req("POST", "/v1/admin/api-keys", {"name": "ro", "scopes": ["caps:read"]})
        token = key["key"] if "key" in key else key.get("secret")
        st, _, _ = self.srv.request("GET", "/v1/groups", token=token)
        self.assertEqual(st, 200)
        st, _, _ = self.srv.request("POST", "/v1/groups", token=token, body={"id": "zz"})
        self.assertEqual(st, 403)


class GroupCapTests(_GroupBase):
    def test_per_member_cap_inherited_and_user_rule_wins(self):
        self.group("eng", ["a@company.com", "b@company.com"])
        self.spend("a@company.com", "b@company.com")
        st, r = self.req("PUT", "/v1/caps/groups/eng", {"type": "limit", "cost_cap": 1.0, "pool": "per_member"})
        self.assertEqual(st, 200, r)
        self.req("PUT", "/v1/caps/users/b%40company.com", {"type": "limit", "cost_cap": 100.0})
        st, a = self.req("GET", "/v1/caps/users/a%40company.com")
        self.assertEqual((a["status"], a["rule_source"], a["limit"]["cost_cap"]), ("over_cap", "group:eng", 1.0))
        self.assertEqual(a["groups"], ["eng"])
        st, b = self.req("GET", "/v1/caps/users/b%40company.com")
        self.assertEqual((b["status"], b["rule_source"]), ("active", "user"))
        st, run = self.req("POST", "/v1/caps/enforce", {"dryRun": False})
        self.assertEqual(run["plan"]["suspend"], ["a@company.com"])
        self.assertTrue(self.is_denied("a@company.com"))
        self.assertFalse(self.is_denied("b@company.com"))
        st, gc = self.req("GET", "/v1/caps/groups/eng")
        self.assertEqual((gc["pool"], gc["member_status"]["suspended"], gc["member_status"]["active"]),
                         ("per_member", 1, 1))
        # Deleting the group cap returns members to the global policy (no cap) -> restored on the next cycle.
        st, _ = self.req("DELETE", "/v1/caps/groups/eng")
        st, run = self.req("POST", "/v1/caps/enforce", {"dryRun": False})
        self.assertIn("a@company.com", run["plan"]["restore"])
        self.assertFalse(self.is_denied("a@company.com"))

    def test_shared_pool_suspends_members_except_excluded_and_allowed(self):
        self.group("team", ["a@company.com", "b@company.com", "c@company.com", "d@company.com"])
        self.req("POST", "/v1/groups/team/exclusions", {"add": ["c@company.com"]})
        self.spend("a@company.com", "b@company.com", "c@company.com")
        self.req("PUT", "/v1/caps/users/d%40company.com", {"type": "allow", "note": "exec"})
        st, r = self.req("PUT", "/v1/caps/groups/team", {"type": "limit", "cost_cap": 2.0, "pool": "shared"})
        self.assertEqual(st, 200, r)
        st, gc = self.req("GET", "/v1/caps/groups/team")
        self.assertEqual(gc["usage"]["estimated_cost"], 3.0)  # c is excluded from the pool
        self.assertTrue(gc["over_cap"])
        self.assertEqual(gc["utilization_pct"], 150.0)
        st, run = self.req("POST", "/v1/caps/enforce", {"dryRun": False})
        self.assertEqual(sorted(run["plan"]["suspend"]), ["a@company.com", "b@company.com"])
        self.assertTrue(self.is_denied("a@company.com") and self.is_denied("b@company.com"))
        self.assertTrue(self.is_denied("a@company.com", TEAM_B))
        self.assertFalse(self.is_denied("c@company.com"))
        self.assertFalse(self.is_denied("d@company.com"))
        st, a = self.req("GET", "/v1/caps/users/a%40company.com")
        self.assertEqual(a["status"], "suspended")
        self.assertIn("group:team", a["over_cap_reasons"])
        self.assertTrue(self.events("group.cap_exceeded"))
        # Removing b from the group lifts b's pool suspension immediately.
        st, g = self.req("POST", "/v1/groups/team/members", {"remove": ["b@company.com"]})
        self.assertEqual(st, 200, g)
        self.assertFalse(self.is_denied("b@company.com"))
        self.assertTrue(self.is_denied("a@company.com"))
        # Excluding a lifts a immediately as well.
        self.req("POST", "/v1/groups/team/exclusions", {"add": ["a@company.com"]})
        self.assertFalse(self.is_denied("a@company.com"))
        st, a = self.req("GET", "/v1/caps/users/a%40company.com")
        self.assertEqual(a["status"], "active")

    def test_shared_pool_per_project(self):
        self.group("pp", ["a@company.com", "b@company.com"])
        self.spend("a@company.com", "b@company.com", project=TEAM_B)
        self.spend("a@company.com", project=HUB)
        self.req("PUT", "/v1/caps/groups/pp", {"type": "limit", "pool": "shared",
                                               "project_caps": {TEAM_B: {"cost_cap": 2.0}}})
        st, run = self.req("POST", "/v1/caps/enforce", {"dryRun": False})
        self.assertEqual(run["plan"]["suspend_projects"], {"a@company.com": [TEAM_B], "b@company.com": [TEAM_B]})
        self.assertTrue(self.is_denied("a@company.com", TEAM_B))
        self.assertFalse(self.is_denied("a@company.com", HUB))


class GroupBlockTests(_GroupBase):
    def test_group_block_unblock_exclusion_and_user_level_blocks(self):
        self.group("ops", ["x@company.com", "y@company.com", "z@company.com"])
        self.req("POST", "/v1/users/x%40company.com/block", {"reason": "own"})
        self.req("PUT", "/v1/caps/users/z%40company.com", {"type": "allow"})
        st, res = self.req("POST", "/v1/groups/ops/block", {"projects": [TEAM_B], "reason": "audit"})
        self.assertEqual(st, 200, res)
        self.assertEqual(res["members_blocked"], ["y@company.com"])  # x has its own block, z has allow
        self.assertTrue(self.is_denied("y@company.com", TEAM_B))
        self.assertFalse(self.is_denied("y@company.com", HUB))
        self.assertFalse(self.is_denied("z@company.com", TEAM_B))
        self.assertTrue(self.events("group.blocked"))
        st, cap = self.req("GET", "/v1/caps/users/y%40company.com")
        self.assertEqual((cap["status"], cap["block_source"]), ("blocked", "group:ops"))
        # A user unblock cannot override a group block; the response explains how.
        st, res = self.req("POST", "/v1/users/y%40company.com/unblock", {})
        self.assertEqual((res["status"], res["remaining_group_blocks"]), ("blocked", ["ops"]))
        self.assertTrue(self.is_denied("y@company.com", TEAM_B))
        # Exception: excluding y removes the group block for y only.
        self.req("POST", "/v1/groups/ops/exclusions", {"add": ["y@company.com"]})
        self.assertFalse(self.is_denied("y@company.com", TEAM_B))
        self.req("POST", "/v1/groups/ops/exclusions", {"remove": ["y@company.com"]})
        self.assertTrue(self.is_denied("y@company.com", TEAM_B))
        st, res = self.req("POST", "/v1/groups/ops/unblock", {"reason": "done"})
        self.assertEqual(st, 200, res)
        self.assertEqual(res["members_still_blocked"], ["x@company.com"])
        self.assertFalse(self.is_denied("y@company.com", TEAM_B))
        self.assertTrue(self.is_denied("x@company.com", HUB))  # user-level block untouched
        self.assertTrue(self.events("group.unblocked"))

    def test_group_block_rule_and_new_member_is_blocked_immediately(self):
        self.group("banned", ["p@company.com"])
        st, r = self.req("PUT", "/v1/caps/groups/banned", {"type": "block", "services": ["gemini_platform"]})
        self.assertEqual(st, 200, r)
        self.assertTrue(self.is_denied("p@company.com"))
        self.req("POST", "/v1/groups/banned/members", {"add": ["q@company.com"]})
        self.assertTrue(self.is_denied("q@company.com"))
        self.req("POST", "/v1/groups/banned/members", {"remove": ["p@company.com"]})
        self.assertFalse(self.is_denied("p@company.com"))
        st, _ = self.req("DELETE", "/v1/groups/banned")
        self.assertFalse(self.is_denied("q@company.com"))


class GroupModelRuleTests(_GroupBase):
    def _event(self, email, model, project=TEAM_B, iid="e1"):
        e = make_inference_entry(email, project=project, model=model, insert_id=iid)
        body = {"message": {"data": base64.b64encode(json.dumps(e).encode()).decode()}}
        return self.req("POST", "/v1/internal/usage-event", body)[1]

    def test_group_model_rule_blocks_members_only_and_user_rule_wins(self):
        self.group("contractors", ["sam@company.com", "kim@company.com"])
        st, r = self.req("PUT", "/v1/groups/contractors/model-rules", {"mode": "deny_list", "models": ["claude-*"]})
        self.assertEqual(st, 200, r)
        self.req("PUT", "/v1/users/kim%40company.com/model-rules", {"mode": "unrestricted"})
        st, ug = self.req("GET", "/v1/users/sam%40company.com/groups")
        self.assertEqual(ug["effective_model_rule_source"], "group:contractors")
        self.assertEqual(self._event("sam@company.com", "claude-test-sonnet", iid="s1")["action"],
                         "model_violation_blocked")
        self.assertTrue(self.is_denied("sam@company.com", TEAM_B))
        self.assertNotEqual(self._event("kim@company.com", "claude-test-sonnet", iid="k1")["action"],
                            "model_violation_blocked")
        self.assertNotEqual(self._event("erin@company.com", "claude-test-sonnet", iid="r1")["action"],
                            "model_violation_blocked")
        v = self.events("user.model_violation")[0]["data"]
        self.assertEqual(v["rule_source"], "group:contractors")
        st, r = self.req("GET", "/v1/groups/contractors/model-rules")
        self.assertEqual(r["rule"]["mode"], "deny_list")
        self.assertEqual(self.req("DELETE", "/v1/groups/contractors/model-rules")[0], 200)
        self.assertEqual(self.req("DELETE", "/v1/groups/contractors/model-rules")[0], 404)


class GoogleGroupSyncTests(_GroupBase):
    def test_sync_from_cloud_identity(self):
        members = {"ids": ["alice@company.com", "bob@company.com", "bot@proj.iam.gserviceaccount.com",
                           "eng@company.com"]}

        def handler(method, url, params, body):
            if url.endswith("/groups:lookup"):
                self.assertEqual(params["groupKey.id"], "eng@company.com")
                return {"name": "groups/03abc"}
            if url.endswith("/groups/03abc/memberships:searchTransitiveMemberships"):
                return {"memberships": [{"preferredMemberKey": [{"id": i}]} for i in members["ids"]]}
            raise AssertionError(url)
        self.gcp.rest_handlers.append((lambda m, u, p, b: "cloudidentity.googleapis.com" in u, handler))
        g = self.group("eng", ["carol@company.com"], google_group="eng@company.com")
        self.assertEqual(g["synced_members"], ["alice@company.com", "bob@company.com"])
        self.assertEqual(g["effective_members"], ["alice@company.com", "bob@company.com", "carol@company.com"])
        members["ids"] = ["alice@company.com", "dave@company.com"]
        st, res = self.req("POST", "/v1/groups/eng/sync", {})
        self.assertEqual((st, res["added"], res["removed"]), (200, ["dave@company.com"], ["bob@company.com"]))

    def test_sync_failure_is_reported(self):
        g = self.group("eng", [], google_group="eng@company.com")
        self.assertEqual(g["synced_members"], [])
        self.assertTrue(g["sync_error"])
        st, res = self.req("POST", "/v1/groups/eng/sync", {})
        self.assertFalse(res["synced"])
        self.assertIn("Groups Reader", res["hint"])


class GroupUsageTests(_GroupBase):
    def test_usage_rollup(self):
        self.group("eng", ["a@company.com", "b@company.com"])
        self.spend("a@company.com", "b@company.com", "outsider@company.com")
        self.spend("a@company.com", project=TEAM_B)
        st, page = self.req("GET", "/v1/usage/groups")
        self.assertEqual(st, 200, page)
        row = page["data"][0]
        self.assertEqual((row["group"], row["requests"], row["active_members"]), ("eng", 3, 2))
        self.assertAlmostEqual(row["estimated_cost"], 4.5)
        st, one = self.req("GET", "/v1/usage/groups/eng")
        self.assertEqual({p["project"] for p in one["by_project"]}, {HUB, TEAM_B})
        self.assertEqual(sum(t["requests"] for t in one["timeseries"]), 3)
        self.assertEqual(one["members"][0]["email"], "a@company.com")


class SurfaceAliasTests(_GroupBase):
    def test_legacy_vertex_ai_service_name_is_accepted(self):
        st, res = self.req("POST", "/v1/users/sam%40company.com/block", {"services": ["vertex_ai"]})
        self.assertEqual(st, 200, res)
        self.assertEqual(res["blocked_services"], ["gemini_platform"])
        st, res = self.req("GET", "/v1/usage/users?surface=vertex_ai")
        self.assertEqual(st, 200)


if __name__ == "__main__":
    unittest.main()
