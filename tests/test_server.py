from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from helpers import RepoTestCase, commit, git

from partial.demo import create_demo_store
from partial.git import create_checkpoint
from partial.models import Event, scoped_session_id
from partial.server import ApiError, _Context, create_server
from partial.store import Store

TOKEN = "test-token-" + "x" * 40
PASSWORD = "correct horse battery"


class ServerCase(RepoTestCase):
    demo = False

    def setUp(self):
        super().setUp()
        self.store = Store(self.home / "partial.db")
        row = self.store.register_repo(self.repo)
        self.repo_id = row["id"]
        self.sid_native = "sess-1"
        evs = [
            Event(id="e" + "0" * 40, session_id=self.sid_native,
                  agent="devin", kind="session_start",
                  timestamp="2026-09-16T09:00:00.000000Z"),
            Event(id="e" + "1" * 40, session_id=self.sid_native,
                  agent="devin", kind="prompt",
                  timestamp="2026-09-16T09:00:01.000000Z",
                  text="add 100% coverage for tokens_limit"),
            Event(id="e" + "2" * 40, session_id=self.sid_native,
                  agent="devin", kind="tool",
                  timestamp="2026-09-16T09:00:02.000000Z",
                  tool_name="edit",
                  data={"tool_input": {"file_path": "app.py",
                                       "new_string": "x=1"}}),
            Event(id="e" + "3" * 40, session_id=self.sid_native,
                  agent="devin", kind="response",
                  timestamp="2026-09-16T09:00:03.000000Z",
                  text="done with tokens_limit"),
        ]
        self.store.ingest(self.repo_id, evs, worktree=str(self.repo),
                          branch="main")
        self.session_id = scoped_session_id(
            self.repo_id, "devin", self.sid_native)
        self.write_file("app.py", "x = 1\n")
        git(self.repo, "add", "--", "app.py")
        commit(self.repo, "add app")
        cp = create_checkpoint(
            self.store, self.repo_id,
            session_ids=[self.session_id], worktree=str(self.repo))
        self.checkpoint_id = cp["id"]
        self.server = create_server(
            self.store, host="127.0.0.1", port=0, token=TOKEN,
            demo=self.demo)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.port}"
        if not self.demo:
            self._bootstrap()

    def _bootstrap(self):
        status, data, _, hdrs = self.req(
            "POST", "/api/setup",
            {"email": "owner@example.test", "name": "Owner",
             "password": PASSWORD, "bootstrap_token": TOKEN},
            {"Origin": self.origin})
        self.assertEqual(status, 200, data)
        cookie = dict(
            (k.lower(), v) for k, v in hdrs).get("set-cookie")
        self.owner_cookie = cookie.split(";")[0]
        status, me, _, _ = self.req(
            "GET", "/api/me",
            headers={"Cookie": self.owner_cookie})
        self.assertEqual(status, 200, me)
        self.ws_id = me["workspace"]["id"]
        status, data, _, _ = self.req(
            "POST", f"/api/workspaces/{self.ws_id}/tokens",
            {"name": "ci", "role": "member"},
            {"Origin": self.origin, "Cookie": self.owner_cookie})
        self.assertEqual(status, 201, data)
        self.api_token = data["item"]["token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def req(self, method, path, body=None, headers=None, ctype=None):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else \
                json.dumps(body).encode()
            h.setdefault("Content-Type", ctype or "application/json")
        try:
            conn.request(method, path, body=data, headers=h)
        except (BrokenPipeError, ConnectionResetError):
            pass
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        parsed = None
        if (resp.getheader("Content-Type") or "").startswith(
                "application/json"):
            parsed = json.loads(raw) if raw else None
        return resp.status, parsed, raw, resp.getheaders()

    def bearer(self):
        return {"Authorization": f"Bearer {self.api_token}"}

    def login(self):
        status, data, _, hdrs = self.req(
            "POST", "/api/login",
            {"email": "owner@example.test", "password": PASSWORD},
            {"Origin": self.origin})
        self.assertEqual(status, 200)
        cookie = dict(
            (k.lower(), v) for k, v in hdrs).get("set-cookie")
        self.assertIn("partial_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        return cookie.split(";")[0]


class AuthTests(ServerCase):
    def test_health_public(self):
        status, data, _, _ = self.req("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_me_requires_auth(self):
        status, _, _, _ = self.req("GET", "/api/me")
        self.assertEqual(status, 401)
        status, data, _, _ = self.req("GET", "/api/me",
                                      headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertTrue(data["authenticated"])
        self.assertFalse(data["demo"])

    def test_bootstrap_token_not_a_bearer(self):
        status, _, _, _ = self.req(
            "GET", "/api/me",
            headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 401)

    def test_login_cookie_flow_and_logout(self):
        status, _, _, _ = self.req(
            "POST", "/api/login",
            {"email": "owner@example.test", "password": "wrong"},
            {"Origin": self.origin})
        self.assertEqual(status, 401)
        cookie = self.login()
        status, data, _, _ = self.req(
            "GET", "/api/me", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertTrue(data["authenticated"])
        status, _, _, _ = self.req(
            "POST", "/api/logout", {},
            {"Origin": self.origin, "Cookie": cookie})
        self.assertEqual(status, 200)
        status, _, _, _ = self.req(
            "GET", "/api/me", headers={"Cookie": cookie})
        self.assertEqual(status, 401)

    def test_login_rate_limit(self):
        for _ in range(10):
            self.req("POST", "/api/login",
                     {"email": "x@x.test", "password": "nope-nope"},
                     {"Origin": self.origin})
        status, _, _, _ = self.req(
            "POST", "/api/login",
            {"email": "x@x.test", "password": "nope-nope"},
            {"Origin": self.origin})
        self.assertEqual(status, 429)

    def test_origin_and_host_checks(self):
        status, _, _, _ = self.req(
            "POST", "/api/bundles", {},
            {"Origin": "http://evil.example",
             **self.bearer()})
        self.assertEqual(status, 403)
        status, _, _, _ = self.req(
            "POST", "/api/bundles", {},
            {"Origin": self.origin, **self.bearer()})
        self.assertEqual(status, 400)
        status, _, _, _ = self.req(
            "POST", "/api/bundles", {}, self.bearer())
        self.assertEqual(status, 400)
        status, _, _, _ = self.req(
            "POST", "/api/bundles", {})
        self.assertEqual(status, 401)
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/me", headers={
            "Host": "evil.example",
            "Authorization": f"Bearer {TOKEN}"})
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 403)
        conn.close()

    def test_origin_checked_on_get(self):
        status, _, _, _ = self.req(
            "GET", "/api/me",
            headers={"Origin": "http://evil.example",
                     **self.bearer()})
        self.assertEqual(status, 403)
        status, _, _, _ = self.req(
            "GET", "/api/me",
            headers={"Origin": self.origin, **self.bearer()})
        self.assertEqual(status, 200)

    def test_persistent_connection_after_post(self):
        cookie = self.login()
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/logout", body=b"{}", headers={
            "Content-Type": "application/json",
            "Origin": self.origin, "Cookie": cookie})
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 200)
        conn.request("GET", "/api/me", headers={"Cookie": cookie})
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 401)
        conn.close()

    def test_method_and_traversal(self):
        status, _, _, _ = self.req("PUT", "/api/sessions",
                                   headers=self.bearer())
        self.assertEqual(status, 404)
        status, _, _, _ = self.req(
            "GET", "/static/../server.py", headers=self.bearer())
        self.assertEqual(status, 404)
        status, _, _, _ = self.req(
            "GET", "/static/server.py", headers=self.bearer())
        self.assertEqual(status, 404)

    def test_body_validation(self):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/api/bundles")
        conn.putheader("Authorization", f"Bearer {TOKEN}")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "1")
        conn.putheader("Content-Length", "2")
        conn.endheaders(b"{}")
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 400)
        conn.close()

        status, _, _, _ = self.req(
            "POST", "/api/bundles", b"x" * (16 * 1024 * 1024 + 1),
            self.bearer())
        self.assertEqual(status, 413)
        status, _, _, _ = self.req(
            "POST", "/api/bundles", b"not json", self.bearer())
        self.assertEqual(status, 400)
        status, _, _, _ = self.req(
            "POST", "/api/bundles", b"{}", self.bearer(),
            ctype="text/plain")
        self.assertEqual(status, 400)

    def test_security_headers(self):
        status, _, _, hdrs = self.req("GET", "/api/me",
                                      headers=self.bearer())
        h = dict((k.lower(), v) for k, v in hdrs)
        self.assertIn("default-src 'self'", h["content-security-policy"])
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertEqual(h["referrer-policy"], "no-referrer")
        self.assertEqual(h["cache-control"], "no-store")
        status, _, raw, hdrs = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Keep the context", raw)
        status, _, raw, _ = self.req("GET", "/static/app.js")
        self.assertEqual(status, 200)

    def test_no_origin_mutations_rejected(self):
        for path, body in (
            ("/api/login", {"email": "owner@example.test",
                            "password": PASSWORD}),
            ("/api/setup", {"email": "x@x.test", "name": "X",
                            "password": "x" * 12,
                            "bootstrap_token": TOKEN}),
            ("/api/invites/accept", {"token": "t"}),
        ):
            status, _, _, _ = self.req("POST", path, body)
            self.assertEqual(status, 403, path)
        cookie = self.login()
        status, _, _, _ = self.req(
            "POST", "/api/logout", {}, {"Cookie": cookie})
        self.assertEqual(status, 401)
        status, _, _, _ = self.req(
            "GET", "/api/me", headers={"Cookie": cookie})
        self.assertEqual(status, 200)

    def test_bearer_no_origin_write_and_invalid_bearer(self):
        bundle = self.store.export_bundle()
        status, data, _, _ = self.req(
            "POST", "/api/bundles", bundle, self.bearer())
        self.assertEqual(status, 200)
        status, _, _, _ = self.req(
            "POST", "/api/bundles", bundle,
            {"Authorization": "Bearer " + "z" * 40})
        self.assertEqual(status, 401)
        status, _, _, _ = self.req(
            "POST", "/api/bundles", bundle,
            {"Authorization": "Bearer " + "z" * 600})
        self.assertEqual(status, 401)

    def test_scoped_token_admin_paths_403(self):
        status, _, _, _ = self.req(
            "POST", "/api/invites/accept", {"token": "x"},
            {"Origin": self.origin, **self.bearer()})
        self.assertEqual(status, 403)
        status, data, _, _ = self.req(
            "POST", f"/api/workspaces/{self.ws_id}/tokens",
            {"name": "t2", "role": "member"},
            {"Origin": self.origin, "Cookie": self.owner_cookie})
        self.assertEqual(status, 201)
        status, _, _, _ = self.req(
            "DELETE", f"/api/tokens/{data['item']['id']}", {},
            self.bearer())
        self.assertEqual(status, 403)

    def test_login_password_bounds(self):
        for bad in ({"x": 1}, ["a"], "x" * 2000, 12345):
            status, _, _, _ = self.req(
                "POST", "/api/login",
                {"email": "owner@example.test", "password": bad},
                {"Origin": self.origin})
            self.assertEqual(status, 401, bad)

    def test_change_password_type_errors(self):
        cookie = self.login()
        for body in (
            {"current_password": {"x": 1},
             "new_password": "y" * 12},
            {"current_password": PASSWORD,
             "new_password": {"x": 1}},
            {"current_password": PASSWORD, "new_password": "short"},
        ):
            status, _, _, _ = self.req(
                "POST", "/api/account/password", body,
                {"Origin": self.origin, "Cookie": cookie})
            self.assertIn(status, (400, 401), body)
        status, _, _, _ = self.req(
            "GET", "/api/me", headers={"Cookie": cookie})
        self.assertEqual(status, 200)

    def test_public_bind_guard(self):
        with self.assertRaises(ValueError):
            create_server(self.store, host="0.0.0.0", port=0,
                          token=TOKEN)


class ApiTests(ServerCase):
    def test_repos_no_root(self):
        status, data, _, _ = self.req("GET", "/api/repos",
                                      headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 1)
        self.assertNotIn("root", data["items"][0])
        status, data, _, _ = self.req(
            "GET", f"/api/repos/{self.repo_id}",
            headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertNotIn("root", data["repository"])
        self.assertIn("main", data["branches"])
        status, _, _, _ = self.req(
            "GET", f"/api/repos/{'f' * 64}", headers=self.bearer())
        self.assertEqual(status, 404)

    def test_sessions_list_and_detail(self):
        status, data, _, _ = self.req(
            "GET", "/api/sessions?agent=devin&limit=1&offset=0",
            headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 1)
        self.assertFalse(data["has_more"])
        s = data["items"][0]
        self.assertNotIn("worktree", s)
        status, data, _, _ = self.req(
            "GET", f"/api/sessions/{self.session_id}?limit=2",
            headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(len(data["events"]), 2)
        self.assertTrue(data["has_more"])
        self.assertEqual(data["checkpoints"], [self.checkpoint_id])
        status, data, _, _ = self.req(
            "GET", f"/api/sessions/{self.session_id}?limit=2&offset=2",
            headers=self.bearer())
        self.assertFalse(data["has_more"])
        status, _, _, _ = self.req(
            "GET", "/api/sessions/notanid", headers=self.bearer())
        self.assertEqual(status, 404)

    def test_session_branch_and_kind_filters(self):
        status, data, _, _ = self.req(
            "GET", "/api/sessions?branch=main", headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 1)
        status, data, _, _ = self.req(
            "GET", "/api/sessions?branch=nope",
            headers=self.bearer())
        self.assertEqual(data["items"], [])
        status, data, _, _ = self.req(
            "GET", f"/api/sessions/{self.session_id}?kind=tool",
            headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(len(data["events"]), 1)
        self.assertEqual(data["events"][0]["kind"], "tool")
        status, _, _, _ = self.req(
            "GET", f"/api/sessions/{self.session_id}?kind=bogus",
            headers=self.bearer())
        self.assertEqual(status, 400)
        status, data, _, _ = self.req(
            "GET", f"/api/sessions/{self.session_id}?kind=tool",
            headers=self.bearer())
        self.assertEqual(
            data["events"][0]["data"]["tool_input"]["file_path"],
            "app.py")

    def test_kind_filter_beyond_first_page(self):
        evs = [
            Event(id=f"e{i:04d}", session_id="big", agent="devin",
                  kind="prompt",
                  timestamp=f"2026-09-16T10:{i // 60:02d}:{i % 60:02d}Z",
                  text=f"filler {i}")
            for i in range(205)
        ]
        evs.append(Event(
            id="etool", session_id="big", agent="devin", kind="tool",
            timestamp="2026-09-16T11:00:00Z", tool_name="edit",
            data={"tool_input": {
                "file_path": str(self.repo / "app.py")}}))
        self.store.ingest(self.repo_id, evs, worktree=str(self.repo))
        sid = scoped_session_id(self.repo_id, "devin", "big")
        status, data, _, _ = self.req(
            "GET", f"/api/sessions/{sid}?kind=tool",
            headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(len(data["events"]), 1)
        self.assertFalse(data["has_more"])
        self.assertEqual(
            data["events"][0]["data"]["tool_input"]["file_path"],
            "app.py")

    def test_handoff_attachment(self):
        status, _, raw, hdrs = self.req(
            "GET", f"/api/sessions/{self.session_id}/handoff",
            headers=self.bearer())
        self.assertEqual(status, 200)
        h = dict((k.lower(), v) for k, v in hdrs)
        self.assertIn("partial-handoff.md",
                      h["content-disposition"])
        self.assertIn(b"tokens_limit", raw)

    def test_session_detail_and_handoff_strip_absolute_paths(self):
        wt = str(Path(self.repo).resolve())
        self.store.ingest(self.repo_id, [
            Event(id="epath", session_id=self.sid_native,
                  agent="devin", kind="session_start",
                  timestamp="2026-09-16T09:00:04.000000Z",
                  data={
                      "cwd": wt,
                      "hook": {
                          "transcript_path":
                              "/home/u/.claude/projects/x/t.jsonl"},
                  }),
        ], worktree=wt)
        status, data, _, _ = self.req(
            "GET", f"/api/sessions/{self.session_id}",
            headers=self.bearer())
        self.assertEqual(status, 200)
        blob = json.dumps(data)
        self.assertNotIn(wt, blob)
        self.assertNotIn("/home/u", blob)
        meta = [e for e in data["events"] if e["id"] == "epath"][0]
        self.assertEqual(meta["data"]["cwd"], ".")
        self.assertEqual(
            meta["data"]["hook"]["transcript_path"], "t.jsonl")
        status, _, raw, _ = self.req(
            "GET", f"/api/sessions/{self.session_id}/handoff",
            headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertNotIn(wt.encode(), raw)
        self.assertNotIn(b"/home/u", raw)
        self.assertIn(b"t.jsonl", raw)

    def test_checkpoints(self):
        status, data, _, _ = self.req(
            "GET", "/api/checkpoints", headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 1)
        self.assertNotIn("diff", data["items"][0])
        status, data, _, _ = self.req(
            "GET", "/api/checkpoints?q=no%20match",
            headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(data["items"], [])
        status, data, _, _ = self.req(
            "GET", f"/api/checkpoints/{self.checkpoint_id}",
            headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertIn("diff", data["checkpoint"])
        self.assertEqual(data["sessions"][0]["id"], self.session_id)
        self.assertEqual(data["sessions"][0]["event_count"], 4)
        self.assertEqual(
            data["sessions"][0]["checkpoint_count"], 1)
        self.assertFalse(data["sessions"][0]["is_subagent"])

    def test_repos_summary_fields(self):
        status, data, _, _ = self.req(
            "GET", "/api/repos", headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertNotIn("root", item)
        self.assertEqual(item["id"], self.repo_id)
        self.assertEqual(item["session_count"], 1)
        self.assertEqual(item["checkpoint_count"], 1)
        self.assertEqual(item["branch_count"], 1)
        self.assertEqual(item["agents"], ["devin"])
        self.assertTrue(item["last_activity"])
        self.assertEqual(
            item["latest_session"]["id"], self.session_id)
        self.assertEqual(item["latest_session"]["title"],
                         "add 100% coverage for tokens_limit")
        self.assertEqual(
            item["latest_checkpoint"]["id"], self.checkpoint_id)
        self.assertTrue(item["latest_checkpoint"]["commit_sha"])
        self.assertIsNone(item["indexed"])
        status, data, _, _ = self.req(
            "GET", f"/api/repos/{self.repo_id}",
            headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertNotIn("root", data["repository"])
        stats = data["stats"]
        self.assertEqual(stats["session_count"], 1)
        self.assertEqual(stats["checkpoint_count"], 1)
        self.assertEqual(stats["branch_count"], 1)
        self.assertEqual(stats["agents"], ["devin"])
        self.assertTrue(stats["last_activity"])
        self.assertEqual(
            stats["latest_session"]["id"], self.session_id)
        self.assertEqual(
            stats["latest_checkpoint"]["id"], self.checkpoint_id)
        self.assertIsNone(stats["indexed"])
        conn = self.store._connect()
        try:
            conn.execute(
                "INSERT INTO repository_indexes(repo_id,commit_sha,"
                "indexed_at) VALUES(?,?,?)",
                (self.repo_id, "c" * 40, "2026-01-03T00:00:00Z"))
            conn.commit()
        finally:
            conn.close()
        status, data, _, _ = self.req(
            "GET", f"/api/repos/{self.repo_id}",
            headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(data["stats"]["indexed"], {
            "commit_sha": "c" * 40,
            "indexed_at": "2026-01-03T00:00:00Z"})

    def test_sessions_list_summary_fields(self):
        status, data, _, _ = self.req(
            "GET", "/api/sessions", headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 1)
        s = data["items"][0]
        self.assertEqual(s["event_count"], 4)
        self.assertEqual(s["checkpoint_count"], 1)
        self.assertEqual(s["child_count"], 0)
        self.assertFalse(s["is_subagent"])
        self.assertIsNone(s["parent_session_id"])
        # A session recorded with a parent surfaces as a sub-agent
        # row and bumps the parent's child count.
        self.store.ingest(self.repo_id, [
            Event(id="k1", session_id="child-1", agent="devin",
                  kind="prompt",
                  timestamp="2026-09-16T09:05:00.000000Z",
                  text="child task",
                  parent_session_id=self.sid_native),
        ], worktree=str(self.repo))
        status, data, _, _ = self.req(
            "GET", "/api/sessions?limit=10", headers=self.bearer())
        self.assertEqual(status, 200)
        by_id = {x["id"]: x for x in data["items"]}
        child_id = scoped_session_id(
            self.repo_id, "devin", "child-1")
        self.assertTrue(by_id[child_id]["is_subagent"])
        self.assertEqual(
            by_id[child_id]["parent_session_id"], self.session_id)
        self.assertEqual(by_id[child_id]["event_count"], 1)
        self.assertEqual(by_id[child_id]["child_count"], 0)
        self.assertEqual(
            by_id[self.session_id]["child_count"], 1)
        status, data, _, _ = self.req(
            "GET", f"/api/sessions/{self.session_id}",
            headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(data["session"]["event_count"], 4)
        self.assertEqual(data["session"]["checkpoint_count"], 1)
        self.assertEqual(data["session"]["child_count"], 1)
        self.assertFalse(data["session"]["is_subagent"])
        child = data["children"][0]
        self.assertEqual(child["id"], child_id)
        self.assertTrue(child["is_subagent"])
        self.assertEqual(child["event_count"], 1)
        self.assertEqual(child["checkpoint_count"], 0)

    def test_checkpoints_summary_fields(self):
        status, data, _, _ = self.req(
            "GET", "/api/checkpoints", headers=self.bearer())
        self.assertEqual(status, 200)
        c = data["items"][0]
        self.assertNotIn("diff", c)
        self.assertNotIn("files", c)
        self.assertEqual(c["file_count"], 1)
        self.assertEqual(c["session_count"], 1)
        self.assertEqual(c["agents"], ["devin"])
        self.assertEqual(c["additions"], 1)
        self.assertEqual(c["deletions"], 0)
        # create_checkpoint records an attribution report; every line
        # is unobserved here, so the AI share and coverage are 0%.
        self.assertEqual(c["ai_percentage"], 0.0)
        self.assertEqual(c["coverage_percentage"], 0.0)
        status, data, _, _ = self.req(
            "GET", f"/api/checkpoints/{self.checkpoint_id}",
            headers=self.bearer())
        self.assertEqual(status, 200)
        cp = data["checkpoint"]
        self.assertEqual(cp["file_count"], 1)
        self.assertEqual(cp["session_count"], 1)
        self.assertEqual(cp["agents"], ["devin"])
        self.assertEqual(cp["additions"], 1)
        self.assertEqual(cp["deletions"], 0)
        self.assertEqual(cp["ai_percentage"], 0.0)
        self.assertIn("token_usage", cp)
        self.assertIsNone(cp["token_usage"]["input_tokens"])
        self.assertEqual(cp["token_usage"]["sessions"], 1)

    def test_checkpoint_detail_token_usage_aggregates(self):
        self.store.ingest(self.repo_id, [
            Event(id="u1", session_id=self.sid_native, agent="devin",
                  kind="usage",
                  timestamp="2026-09-16T09:00:04.000000Z",
                  data={"usage_scope": "delta", "usage_id": "u1",
                        "usage": {"input_tokens": 12,
                                  "output_tokens": 4}}),
        ])
        status, data, _, _ = self.req(
            "GET", f"/api/checkpoints/{self.checkpoint_id}",
            headers=self.bearer())
        self.assertEqual(status, 200, data)
        u = data["checkpoint"]["token_usage"]
        self.assertEqual(u["input_tokens"], 12)
        self.assertEqual(u["output_tokens"], 4)
        self.assertEqual(u["sessions"], 1)
        self.assertTrue(u["complete"])
        # A malformed usage record drops that session from the
        # aggregate but must not turn the detail read into a 500.
        self.store.ingest(self.repo_id, [
            Event(id="u9", session_id=self.sid_native, agent="devin",
                  kind="usage",
                  timestamp="2026-09-16T09:00:05.000000Z",
                  data={"usage_scope": "delta",
                        "usage": {"input_tokens": -1}}),
        ])
        status, data, _, _ = self.req(
            "GET", f"/api/checkpoints/{self.checkpoint_id}",
            headers=self.bearer())
        self.assertEqual(status, 200, data)
        u = data["checkpoint"]["token_usage"]
        self.assertIsNone(u["input_tokens"])
        self.assertEqual(u["sessions"], 0)
        self.assertFalse(u["complete"])

    def test_search(self):
        status, data, _, _ = self.req(
            "GET", "/api/search?q=done", headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 1)
        status, data, _, _ = self.req(
            "GET", "/api/search?q=100%25", headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 4)
        status, data, _, _ = self.req(
            "GET", "/api/search?q=tokens%5Climit",
            headers=self.bearer())
        self.assertEqual(status, 200)
        status, data, _, _ = self.req(
            "GET", "/api/search?q=file_path", headers=self.bearer())
        self.assertEqual(status, 200)
        hit = [i for i in data["items"] if i["kind"] == "tool"]
        self.assertEqual(hit[0]["text"], "Tool activity matched")
        status, data, _, _ = self.req(
            "GET", "/api/search?q=", headers=self.bearer())
        self.assertEqual(status, 400)

    def test_bundle_upload_and_export(self):
        bundle = self.store.export_bundle()
        status, data, _, _ = self.req(
            "POST", "/api/bundles", bundle,
            {"Origin": self.origin, **self.bearer()})
        self.assertEqual(status, 200)
        self.assertIn("counts", data)
        status, data, _, _ = self.req(
            "GET", "/api/export", headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(data["version"], 1)
        fresh = Store(self.tmp / "fresh" / "partial.db")
        fresh.import_bundle(data)
        self.assertEqual(fresh.stats()["sessions"], 1)
        bad = dict(bundle)
        bad["sessions"] = [dict(bundle["sessions"][0],
                                id="f" * 64)]
        status, _, _, _ = self.req(
            "POST", "/api/bundles", bad,
            {"Origin": self.origin, **self.bearer()})
        self.assertEqual(status, 400)

    def test_bundle_type_errors_are_400(self):
        origin = {"Origin": self.origin, **self.bearer()}
        for bad in (
            {"version": 1, "sessions": [{"status": {"x": 1}}]},
            {"version": 1, "sessions": "nope"},
            {"version": 1, "events": [{"data": [1]}]},
            {"version": 1, "checkpoints": [{"id": "ZZ"}]},
        ):
            status, _, _, _ = self.req(
                "POST", "/api/bundles", bad, origin)
            self.assertEqual(status, 400, bad)
        self.assertEqual(self.store.stats()["sessions"], 1)

    def test_bundle_attribution_native_validation_400(self):
        origin = {"Origin": self.origin, **self.bearer()}
        bundle = json.loads(json.dumps(self.store.export_bundle()))
        cases = []
        bad = json.loads(json.dumps(bundle))
        bad["attribution"][0]["report"]["files"][0]["lines"] = [42]
        cases.append(bad)
        bad = json.loads(json.dumps(bundle))
        bad["attribution"][0]["report"]["excluded"] = [
            {"path": "ghost.py", "reason": "x"}]
        cases.append(bad)
        bad = {
            "version": 1,
            "sessions": [{
                "id": "9" * 64, "repo_id": self.repo_id,
                "native_id": "newsess", "agent": "devin"}],
            "native_sessions": [{
                "session_id": "9" * 64, "agent": "chatgpt",
                "native_id": "newsess", "format": "native-id"}],
        }
        cases.append(bad)
        bad = json.loads(json.dumps(bundle))
        for f in bad["attribution"][0]["report"]["files"]:
            for l in f["lines"]:
                l["kind"] = "agent"
                l["session_id"] = "8" * 64
                l["evidence"] = "tool-pair"
        cases.append(bad)
        for b in cases:
            status, data, _, _ = self.req(
                "POST", "/api/bundles", b, origin)
            self.assertEqual(status, 400, data)
        self.assertEqual(self.store.stats()["sessions"], 1)
        self.assertIsNone(self.store.get_session_meta("9" * 64))

    def test_reviews(self):
        status, data, _, _ = self.req(
            "GET", f"/api/checkpoints/{self.checkpoint_id}/reviews",
            headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(data["items"], [])
        cookie = self.login()
        status, data, _, _ = self.req(
            "POST", f"/api/checkpoints/{self.checkpoint_id}/reviews",
            {"author": " Reviewer ", "body": "  looks good  "},
            {"Origin": self.origin, "Cookie": cookie})
        self.assertEqual(status, 201)
        self.assertEqual(data["item"]["author"], "Owner")
        self.assertEqual(data["item"]["body"], "looks good")
        status, _, _, _ = self.req(
            "POST", f"/api/checkpoints/{self.checkpoint_id}/reviews",
            {"author": "x", "body": ""},
            {"Origin": self.origin, "Cookie": cookie})
        self.assertEqual(status, 400)
        status, _, _, _ = self.req(
            "POST", f"/api/checkpoints/{'f' * 32}/reviews",
            {"author": "x", "body": "y"},
            {"Origin": self.origin, "Cookie": cookie})
        self.assertEqual(status, 404)
        status, data, _, _ = self.req(
            "GET", f"/api/checkpoints/{self.checkpoint_id}/reviews",
            headers=self.bearer())
        self.assertEqual(len(data["items"]), 1)


class ContextBoundsTests(unittest.TestCase):
    def _ctx(self):
        store = Store(Path(tempfile.mkdtemp()) / "s.db")
        return _Context(store, "127.0.0.1", 4310, TOKEN, None, False)

    def test_login_hits_pruned_and_bounded(self):
        ctx = self._ctx()
        with unittest.mock.patch(
                "time.monotonic", return_value=1000.0):
            ctx.login_hits["old-ip"] = [1000.0 - 3600]
            self.assertTrue(ctx.check_login_rate("new-ip"))
            self.assertNotIn("old-ip", ctx.login_hits)
        with unittest.mock.patch(
                "time.monotonic", return_value=2000.0):
            ctx.login_hits = {
                f"ip-{i}": [2000.0] for i in range(4096)}
            self.assertFalse(ctx.check_login_rate("fresh-ip"))
            self.assertTrue(ctx.check_login_rate("ip-0"))


class MemoryApiTests(ServerCase):
    def _index(self):
        from partial.memory import Memory
        Memory(self.store).index(None)

    def test_memory_search_and_document(self):
        self._index()
        status, data, _, _ = self.req(
            "GET", "/api/memory/status", headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertTrue(data["fts5"])
        self.assertFalse(data["external_ai_enabled"])
        status, data, _, _ = self.req(
            "GET", "/api/memory/search?q=tokens_limit",
            headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertTrue(data["items"])
        did = data["items"][0]["id"]
        status, data, _, _ = self.req(
            "GET", f"/api/memory/documents/{did}",
            headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(data["document"]["id"], did)
        status, data, _, _ = self.req(
            "GET", "/api/memory/context?q=tokens_limit",
            headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "lexical")

    def test_memory_index_member_only(self):
        status, data, _, _ = self.req(
            "POST", "/api/memory/index", {},
            {"Origin": self.origin, **self.bearer()})
        self.assertEqual(status, 200)
        status, data, _, _ = self.req(
            "POST", f"/api/workspaces/{self.ws_id}/tokens",
            {"name": "v", "role": "viewer"},
            {"Origin": self.origin, "Cookie": self.owner_cookie})
        self.assertEqual(status, 201, data)
        vtoken = data["item"]["token"]
        status, data, _, _ = self.req(
            "POST", "/api/memory/index", {},
            {"Authorization": f"Bearer {vtoken}"})
        self.assertEqual(status, 403)
        status, data, _, _ = self.req(
            "GET", "/api/memory/search?q=tokens_limit",
            headers={"Authorization": f"Bearer {vtoken}"})
        self.assertEqual(status, 200)

    def test_decisions_api_and_roles(self):
        self._index()
        status, data, _, _ = self.req(
            "GET", "/api/memory/search?q=tokens_limit",
            headers=self.bearer())
        doc_id = data["items"][0]["id"]
        status, data, _, _ = self.req(
            "POST", "/api/decisions",
            {"repo_id": self.repo_id, "title": "cap page size",
             "body": "limit 100", "source_ids": [doc_id]},
            {"Origin": self.origin, **self.bearer()})
        self.assertEqual(status, 201, data)
        status, data, _, _ = self.req(
            "GET", f"/api/decisions?repo={self.repo_id}",
            headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(data["items"][0]["title"], "cap page size")
        status, data, _, _ = self.req(
            "POST", "/api/decisions",
            {"repo_id": self.repo_id, "title": "x",
             "body": "y", "source_ids": ["0" * 64]},
            {"Origin": self.origin, **self.bearer()})
        self.assertEqual(status, 400)
        status, data, _, _ = self.req(
            "POST", "/api/decisions",
            {"repo_id": "0" * 64, "title": "x", "body": "y"},
            {"Origin": self.origin, **self.bearer()})
        self.assertEqual(status, 404)

    def test_dispatch_and_workflows_api(self):
        status, data, _, _ = self.req(
            "GET", "/api/dispatch", headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertIn("markdown", data)
        status, data, _, _ = self.req(
            "POST", "/api/workflows",
            {"kind": "ask", "query": "what changed", "run": False},
            {"Origin": self.origin, **self.bearer()})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "planned")
        rid = data["id"]
        status, data, _, _ = self.req(
            "GET", f"/api/workflows/{rid}", headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(data["run"]["status"], "planned")
        status, data, _, _ = self.req(
            "POST", "/api/workflows",
            {"kind": "ask", "query": "q", "run": True},
            {"Origin": self.origin, **self.bearer()})
        self.assertEqual(status, 403)

    def test_memory_settings_owner_only(self):
        status, data, _, _ = self.req(
            "POST", "/api/memory/settings",
            {"external_ai_enabled": True},
            {"Origin": self.origin, **self.bearer()})
        # member token cannot administer; also provider unconfigured
        self.assertIn(status, (400, 403))

    def test_usage_endpoints(self):
        status, data, _, _ = self.req(
            "GET", f"/api/sessions/{self.session_id}/usage",
            headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertIn("basis", data["usage"])
        status, data, _, _ = self.req(
            "GET", f"/api/checkpoints/{self.checkpoint_id}/usage",
            headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(len(data["sessions"]), 1)

    def test_projects_api(self):
        status, data, _, _ = self.req(
            "POST", "/api/projects", {"name": "web"},
            {"Origin": self.origin, **self.bearer()})
        self.assertEqual(status, 201, data)
        pid = data["item"]["id"]
        status, data, _, _ = self.req(
            "POST", f"/api/projects/{pid}/attach",
            {"repo_id": self.repo_id},
            {"Origin": self.origin, **self.bearer()})
        self.assertEqual(status, 200)
        self.assertIn(self.repo_id, data["item"]["repos"])
        status, data, _, _ = self.req(
            "GET", "/api/projects", headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 1)
        status, data, _, _ = self.req(
            "POST", f"/api/projects/{pid}/attach",
            {"repo_id": "0" * 64},
            {"Origin": self.origin, **self.bearer()})
        self.assertEqual(status, 404)

    def test_workspace_isolation_memory(self):
        cookie = self.login()
        status, data, _, _ = self.req(
            "POST", "/api/workspaces", {"name": "other"},
            {"Origin": self.origin, "Cookie": cookie})
        self.assertEqual(status, 201, data)
        ws2 = data["item"]["id"]
        status, data, _, _ = self.req(
            "GET", "/api/memory/search?q=tokens_limit",
            headers={"Cookie": cookie, "X-Partial-Workspace": ws2})
        self.assertEqual(status, 200)
        self.assertEqual(data["items"], [])
        status, data, _, _ = self.req(
            "POST", "/api/decisions",
            {"repo_id": self.repo_id, "title": "x", "body": "y"},
            {"Origin": self.origin, "Cookie": cookie,
             "X-Partial-Workspace": ws2})
        self.assertEqual(status, 404)
        status, data, _, _ = self.req(
            "GET", f"/api/workflows/{'0' * 64}",
            headers={"Cookie": cookie, "X-Partial-Workspace": ws2})
        self.assertEqual(status, 404)


class _FakeProvider:
    configured = True

    def __init__(self, *args, **kwargs):
        self.embed_calls = []
        self.complete_calls = []
        self.error = None

    def embed(self, texts):
        self.embed_calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    def complete(self, system, user):
        self.complete_calls.append((system, user))
        if self.error is not None:
            raise self.error
        return {"answer": "bounded answer", "citations": [],
                "uncertainties": []}


def _dead_pid():
    pid = 400000
    while True:
        try:
            os.kill(pid, 0)
            pid += 1
        except ProcessLookupError:
            return pid
        except PermissionError:
            pid += 1


class SecurityAuditTests(ServerCase):
    def _token(self, role, name="scoped"):
        status, data, _, _ = self.req(
            "POST", f"/api/workspaces/{self.ws_id}/tokens",
            {"name": name, "role": role},
            {"Origin": self.origin, "Cookie": self.owner_cookie})
        self.assertEqual(status, 201, data)
        return data["item"]["token"]

    def _index(self):
        from partial.memory import Memory
        Memory(self.store).index(None)

    def _enable_ai(self):
        cookie = self.login()
        status, data, _, _ = self.req(
            "POST", "/api/memory/settings",
            {"external_ai_enabled": True},
            {"Origin": self.origin, "Cookie": cookie})
        self.assertEqual(status, 200, data)

    def _wait_status(self, rid, *, timeout=10):
        deadline = time.time() + timeout
        data = None
        while time.time() < deadline:
            status, data, _, _ = self.req(
                "GET", f"/api/workflows/{rid}",
                headers=self.bearer())
            self.assertEqual(status, 200, data)
            if data["run"]["status"] != "running":
                return data["run"]
            time.sleep(0.05)
        self.fail(f"run {rid} still running")

    def test_get_search_context_never_semantic(self):
        self._index()
        with unittest.mock.patch(
                "partial.ai.OpenAIProvider") as provider:
            inst = provider.return_value
            status, data, _, _ = self.req(
                "GET",
                "/api/memory/search?q=tokens_limit&semantic=true",
                headers=self.bearer())
            self.assertEqual(status, 200, data)
            self.assertEqual(data["mode"], "lexical")
            status, data, _, _ = self.req(
                "GET",
                "/api/memory/context?q=tokens_limit&semantic=1",
                headers=self.bearer())
            self.assertEqual(status, 200, data)
            self.assertEqual(data["mode"], "lexical")
            inst.embed.assert_not_called()
            inst.complete.assert_not_called()

    def test_get_search_limit_validation(self):
        for bad in ("abc", "0", "-1", "101", "1.5"):
            status, _, _, _ = self.req(
                "GET", f"/api/memory/search?q=tokens_limit&limit={bad}",
                headers=self.bearer())
            self.assertEqual(status, 400, bad)

    def test_post_search_strict_validation(self):
        origin = {"Origin": self.origin, **self.bearer()}
        for bad in (
            {"semantic": False},
            {"query": 42},
            {"query": ""},
            {"query": "  "},
            {"query": "x" * 501},
            {"query": "q", "repo_id": "nothex"},
            {"query": "q", "kind": "bogus"},
            {"query": "q", "limit": "10"},
            {"query": "q", "limit": True},
            {"query": "q", "limit": 0},
            {"query": "q", "limit": 101},
            {"query": "q", "semantic": 1},
            {"query": "q", "semantic": "yes"},
        ):
            status, _, _, _ = self.req(
                "POST", "/api/memory/search", bad, origin)
            self.assertEqual(status, 400, bad)
        status, _, _, _ = self.req(
            "POST", "/api/memory/search",
            {"query": "q", "repo_id": "0" * 64}, origin)
        self.assertEqual(status, 404)

    def test_post_search_semantic_gating(self):
        self._index()
        origin = {"Origin": self.origin, **self.bearer()}
        body = {"query": "tokens_limit", "semantic": True}
        status, _, _, _ = self.req(
            "POST", "/api/memory/search", body, origin)
        self.assertEqual(status, 403)
        with unittest.mock.patch(
                "partial.ai.OpenAIProvider", _FakeProvider):
            self._enable_ai()
            status, data, _, _ = self.req(
                "POST", "/api/memory/index", {"semantic": True},
                origin)
            self.assertEqual(status, 200, data)
            status, data, _, _ = self.req(
                "POST", "/api/memory/search", body, origin)
            self.assertEqual(status, 200, data)
            self.assertEqual(data["mode"], "hybrid")
            self.assertTrue(data["items"])

    def test_post_search_lexical_no_provider_call(self):
        self._index()
        with unittest.mock.patch(
                "partial.ai.OpenAIProvider") as provider:
            inst = provider.return_value
            status, data, _, _ = self.req(
                "POST", "/api/memory/search",
                {"query": "tokens_limit", "semantic": False},
                {"Origin": self.origin, **self.bearer()})
            self.assertEqual(status, 200, data)
            self.assertEqual(data["mode"], "lexical")
            inst.embed.assert_not_called()
            inst.complete.assert_not_called()

    def test_memory_settings_owner_only_provider_gate(self):
        cookie = self.login()
        member = self._token("member", "m")
        viewer = self._token("viewer", "v")
        for headers in (
            {"Authorization": f"Bearer {member}"},
            {"Authorization": f"Bearer {viewer}"},
            {"Origin": self.origin,
             "Authorization": f"Bearer {member}"},
        ):
            status, _, _, _ = self.req(
                "POST", "/api/memory/settings",
                {"external_ai_enabled": True}, headers)
            self.assertEqual(status, 403, headers)
        with unittest.mock.patch.dict(os.environ):
            os.environ.pop("PARTIAL_OPENAI_API_KEY", None)
            for body in ({"external_ai_enabled": True},
                         {"external_ai_enabled": "yes"}):
                status, _, _, _ = self.req(
                    "POST", "/api/memory/settings", body,
                    {"Origin": self.origin, "Cookie": cookie})
                self.assertEqual(status, 400, body)
        with unittest.mock.patch(
                "partial.ai.OpenAIProvider", _FakeProvider):
            status, data, _, _ = self.req(
                "POST", "/api/memory/settings",
                {"external_ai_enabled": True},
                {"Origin": self.origin, "Cookie": cookie})
            self.assertEqual(status, 200, data)
            status, data, _, _ = self.req(
                "POST", "/api/memory/settings",
                {"external_ai_enabled": False},
                {"Origin": self.origin, "Cookie": cookie})
            self.assertEqual(status, 200, data)
            self.assertFalse(data["external_ai_enabled"])

    def test_viewer_role_enforcement(self):
        self._index()
        vtoken = self._token("viewer", "viewer")
        vh = {"Authorization": f"Bearer {vtoken}"}
        for path in (
            "/api/memory/status",
            "/api/memory/search?q=tokens_limit",
            "/api/memory/context?q=tokens_limit",
            "/api/decisions",
            "/api/dispatch",
            "/api/workflows",
            "/api/projects",
        ):
            status, _, _, _ = self.req("GET", path, headers=vh)
            self.assertEqual(status, 200, path)
        for path, body in (
            ("/api/memory/index", {}),
            ("/api/memory/search", {"query": "q"}),
            ("/api/memory/settings",
             {"external_ai_enabled": False}),
            ("/api/decisions",
             {"repo_id": self.repo_id, "title": "t", "body": "b"}),
            ("/api/workflows", {"kind": "ask", "query": "q"}),
            ("/api/projects", {"name": "p"}),
            ("/api/bundles", {"version": 1}),
        ):
            status, _, _, _ = self.req(
                "POST", path, body,
                {"Origin": self.origin, **vh})
            self.assertEqual(status, 403, path)

    def test_workflow_post_validation_no_run_created(self):
        origin = {"Origin": self.origin, **self.bearer()}
        self.assertEqual(self.store.list_runs(), [])
        for bad, want in (
            ({"kind": "bogus", "query": "q"}, 400),
            ({"query": "q"}, 400),
            ({"kind": "ask", "query": "q", "repo_id": "zz"}, 404),
            ({"kind": "ask", "query": "q",
              "repo_id": "0" * 64}, 404),
            ({"kind": "ask", "query": 12}, 400),
            ({"kind": "ask", "query": "q", "run": "yes"}, 400),
            ({"kind": "ask", "query": "q", "run": 1}, 400),
            ({"kind": "ask", "query": "q", "semantic": 1}, 400),
            ({"kind": "ask", "query": "q", "since": 5}, 400),
            ({"kind": "dispatch", "until": {"x": 1}}, 400),
            ({"kind": "dispatch", "branch": ["main"]}, 400),
            ({"kind": "ask", "query": "q",
              "agents": ["codex"]}, 400),
            ({"kind": "ask", "query": "q",
              "agents": "codex"}, 400),
            ({"kind": "ask", "query": "q", "run": True}, 403),
        ):
            status, _, _, _ = self.req(
                "POST", "/api/workflows", bad, origin)
            self.assertEqual(status, want, bad)
            self.assertEqual(self.store.list_runs(), [], bad)

    def test_workflow_run_completes_and_hides_internals(self):
        self._index()
        with unittest.mock.patch(
                "partial.ai.OpenAIProvider", _FakeProvider):
            self._enable_ai()
            status, data, _, _ = self.req(
                "POST", "/api/workflows",
                {"kind": "ask", "query": "tokens_limit",
                 "run": True},
                {"Origin": self.origin, **self.bearer()})
            self.assertEqual(status, 202, data)
            run = self._wait_status(data["id"])
            self.assertIn(run["status"], ("completed", "error"))
            self.assertNotIn("run_owner", run["details"])
            self.assertNotIn("runner", run["details"])
            self.assertNotIn("pid", run["details"])

    def test_workflow_exception_persists_terminal_error(self):
        self.store.set_memory_setting("external_ai_enabled", "true")
        with unittest.mock.patch(
                "partial.ai.OpenAIProvider", _FakeProvider), \
                unittest.mock.patch(
                    "partial.workflows.run_workflow",
                    side_effect=RuntimeError("kaput")):
            status, data, _, _ = self.req(
                "POST", "/api/workflows",
                {"kind": "ask", "query": "q", "run": True},
                {"Origin": self.origin, **self.bearer()})
            self.assertEqual(status, 202, data)
            run = self._wait_status(data["id"])
            self.assertEqual(run["status"], "error")
            self.assertIn("kaput", run["report"]["error"])

    def test_workflow_bounded_concurrency_and_release(self):
        self.store.set_memory_setting("external_ai_enabled", "true")
        gate = threading.Event()
        entered = []
        elock = threading.Lock()

        def slow(store, kind, **kw):
            with elock:
                entered.append(kw.get("run_id"))
            gate.wait(15)
            return {"id": kw.get("run_id"), "status": "completed"}

        origin = {"Origin": self.origin, **self.bearer()}
        with unittest.mock.patch(
                "partial.ai.OpenAIProvider", _FakeProvider), \
                unittest.mock.patch(
                    "partial.workflows.run_workflow", slow):
            ids = []
            for _ in range(4):
                status, data, _, _ = self.req(
                    "POST", "/api/workflows",
                    {"kind": "ask", "query": "q", "run": True},
                    origin)
                self.assertEqual(status, 202, data)
                ids.append(data["id"])
            deadline = time.time() + 10
            while len(entered) < 4 and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(len(entered), 4)
            status, _, _, _ = self.req(
                "POST", "/api/workflows",
                {"kind": "ask", "query": "q", "run": True},
                origin)
            self.assertEqual(status, 429)
            gate.set()
            for rid in ids:
                run = self._wait_status(rid)
                self.assertNotEqual(run["status"], "running")
            status, data, _, _ = self.req(
                "POST", "/api/workflows",
                {"kind": "ask", "query": "q", "run": True},
                origin)
            self.assertEqual(status, 202, data)
            self._wait_status(data["id"])

    def test_stale_run_recovery_only_local_dead(self):
        ctx = self.server.partial_context
        dead = _dead_pid()
        (rid_dead, rid_live, rid_none, rid_own,
         rid_ldead, rid_llive) = (
            "a" * 64, "b" * 64, "c" * 64, "d" * 64,
            "5" * 64, "4" * 64)
        # save_run always stamps this process's private run_owner and
        # strips caller-supplied ownership keys, so foreign and legacy
        # markers are written directly to simulate older databases.
        for rid in (rid_dead, rid_live, rid_none, rid_own,
                    rid_ldead, rid_llive):
            self.store.save_run(
                rid, "ask", self.repo_id, "running", [], {}, {})
        ctx.track_run(rid_own)
        ctx.untrack_run(rid_own)
        conn = self.store._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE workflow_runs SET details=? WHERE id=?",
                    (json.dumps({"run_owner": f"{dead}-foreign"}),
                     rid_dead))
                conn.execute(
                    "UPDATE workflow_runs SET details=? WHERE id=?",
                    (json.dumps(
                        {"run_owner": f"{os.getpid()}-other"}),
                     rid_live))
                # Legacy pre-run_owner server runner/pid markers.
                conn.execute(
                    "UPDATE workflow_runs SET details=? WHERE id=?",
                    (json.dumps({"runner": "old-server", "pid": dead}),
                     rid_ldead))
                conn.execute(
                    "UPDATE workflow_runs SET details=? WHERE id=?",
                    (json.dumps({"runner": "old-server",
                                 "pid": os.getpid()}),
                     rid_llive))
        finally:
            conn.close()
        ctx.cleaned_stores.clear()
        status, data, _, _ = self.req(
            "GET", "/api/workflows", headers=self.bearer())
        self.assertEqual(status, 200, data)
        self.assertEqual(
            self.store.get_run(rid_dead)["status"], "interrupted")
        self.assertEqual(
            self.store.get_run(rid_own)["status"], "interrupted")
        self.assertEqual(
            self.store.get_run(rid_ldead)["status"], "interrupted")
        self.assertEqual(
            self.store.get_run(rid_live)["status"], "running")
        self.assertEqual(
            self.store.get_run(rid_llive)["status"], "running")
        self.assertEqual(
            self.store.get_run(rid_none)["status"], "running")
        # API responses never expose internal ownership identifiers.
        for item in data["items"]:
            det = item.get("details") or {}
            for key in ("run_owner", "runner", "pid"):
                self.assertNotIn(key, det, item.get("id"))

    def test_cross_workspace_404_resources(self):
        self._index()
        status, data, _, _ = self.req(
            "GET", "/api/memory/search?q=tokens_limit",
            headers=self.bearer())
        doc_id = data["items"][0]["id"]
        self.store.save_run(
            "e" * 64, "ask", self.repo_id, "completed", [],
            {"answer": "x"}, {})
        cookie = self.login()
        status, data, _, _ = self.req(
            "POST", "/api/workspaces", {"name": "ws2"},
            {"Origin": self.origin, "Cookie": cookie})
        self.assertEqual(status, 201, data)
        ws2 = data["item"]["id"]
        h2 = {"Cookie": cookie, "X-Partial-Workspace": ws2}
        for path in (
            f"/api/memory/documents/{doc_id}",
            f"/api/workflows/{'e' * 64}",
            f"/api/repos/{self.repo_id}",
        ):
            status, _, _, _ = self.req("GET", path, headers=h2)
            self.assertEqual(status, 404, path)
        status, data, _, _ = self.req(
            "GET", "/api/projects", headers=h2)
        self.assertEqual(status, 200)
        self.assertEqual(data["items"], [])
        status, _, _, _ = self.req(
            "POST", "/api/workflows",
            {"kind": "ask", "query": "q",
             "repo_id": self.repo_id},
            {"Origin": self.origin, **h2})
        self.assertEqual(status, 404)
        status, data, _, _ = self.req(
            "POST", "/api/projects", {"name": "p2"},
            {"Origin": self.origin, **h2})
        self.assertEqual(status, 201, data)
        pid = data["item"]["id"]
        status, _, _, _ = self.req(
            "POST", f"/api/projects/{pid}/attach",
            {"repo_id": self.repo_id},
            {"Origin": self.origin, **h2})
        self.assertEqual(status, 404)
        status, _, _, _ = self.req(
            "GET", "/api/memory/status",
            headers={"Cookie": cookie,
                     "X-Partial-Workspace": "f" * 32})
        self.assertEqual(status, 404)
        status, _, _, _ = self.req(
            "GET", "/api/memory/status",
            headers={**self.bearer(),
                     "X-Partial-Workspace": ws2})
        self.assertEqual(status, 404)

    def test_errors_bounded_no_traceback(self):
        with unittest.mock.patch.object(
                Store, "list_runs",
                side_effect=RuntimeError("secret-boom")):
            status, data, _, _ = self.req(
                "GET", "/api/workflows", headers=self.bearer())
        self.assertEqual(status, 500)
        self.assertEqual(data["error"], "internal server error")
        self.assertNotIn("Traceback", json.dumps(data))
        status, data, _, _ = self.req(
            "POST", "/api/decisions",
            {"repo_id": self.repo_id, "title": "t",
             "body": "b", "source_ids": [1, 2, 3]},
            {"Origin": self.origin, **self.bearer()})
        self.assertEqual(status, 400, data)
        self.assertLessEqual(len(data["error"]), 400)


class DemoTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.demo_store = create_demo_store()
        self.server = create_server(
            self.demo_store, host="127.0.0.1", port=0,
            token="demo-disabled-00000000000000000000", demo=True)
        self.port = self.server.server_address[1]
        threading.Thread(
            target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        raw = resp.read()
        parsed = json.loads(raw) if raw and \
            (resp.getheader("Content-Type") or "").startswith(
                "application/json") else None
        conn.close()
        return resp.status, parsed, raw

    def test_demo_isolated_and_readonly(self):
        self.assertNotEqual(
            Path(self.demo_store.path).parent, self.home)
        status, data, _ = self.req("GET", "/api/me")
        self.assertEqual(status, 200)
        self.assertTrue(data["demo"])
        status, data, _ = self.req("GET", "/api/overview")
        self.assertEqual(status, 200)
        self.assertEqual(data["repositories"], 2)
        self.assertEqual(data["sessions"], 5)
        self.assertEqual(data["checkpoints"], 3)
        status, data, _ = self.req("GET", "/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 5)
        status, _, _ = self.req(
            "POST", "/api/bundles", {},
            {"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 403)
        status, _, _ = self.req(
            "POST", "/api/login", {"token": "x" * 40},
            {"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
