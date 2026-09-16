from __future__ import annotations

import http.client
import json
import tempfile
import threading
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

    def test_checkpoints(self):
        status, data, _, _ = self.req(
            "GET", "/api/checkpoints", headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 1)
        self.assertNotIn("diff", data["items"][0])
        status, data, _, _ = self.req(
            "GET", f"/api/checkpoints/{self.checkpoint_id}",
            headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertIn("diff", data["checkpoint"])
        self.assertEqual(data["sessions"][0]["id"], self.session_id)

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
        self.assertEqual(data["sessions"], 4)
        self.assertEqual(data["checkpoints"], 3)
        status, data, _ = self.req("GET", "/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 4)
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
