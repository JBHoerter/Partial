import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from helpers import RepoTestCase

from partial.accounts import Accounts, AccountsError
from partial.models import scoped_session_id
from partial.store import Store
from partial.server import create_server

BOOT = "b" * 40
PW = "correct horse battery"
PW2 = "second user passphrase"


REPO_ID = "11" * 32
SESSION_ID = scoped_session_id(REPO_ID, "devin", "n1")
EVENT_ID = "33" * 32
CHECKPOINT_ID = "44" * 16


def fixture_bundle(title="alpha", text="secret-a"):
    return {
        "version": 1,
        "repositories": [{
            "id": REPO_ID, "root": "/repo/x", "name": "x",
            "remote": None,
            "created_at": "2026-01-01T00:00:00+00:00"}],
        "sessions": [{
            "id": SESSION_ID, "repo_id": REPO_ID,
            "native_id": "n1", "agent": "devin", "title": title,
            "branch": "main", "parent_session_id": None,
            "model": "m", "status": "idle",
            "started_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:01:00+00:00"}],
        "events": [{
            "id": EVENT_ID, "session_id": SESSION_ID,
            "kind": "prompt", "agent": "devin",
            "timestamp": "2026-01-01T00:00:01+00:00",
            "text": text, "tool_name": None, "data": {}}],
        "checkpoints": [{
            "id": CHECKPOINT_ID, "repo_id": REPO_ID,
            "commit_sha": "a" * 40, "branch": "main",
            "message": "cp-" + title, "author": "a",
            "created_at": "2026-01-01T00:02:00+00:00",
            "files": ["x.py"], "diff": "d-" + title,
            "session_ids": [SESSION_ID], "links": [],
            "reviews": []}],
    }


class AccountsCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.home = Path(self.dir) / "home"
        self.store = Store(self.home / "partial.db")
        self.accounts = Accounts(self.home, self.home / "partial.db")
        self.server = create_server(
            self.store, host="127.0.0.1", port=0, token=BOOT)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.dir, ignore_errors=True)

    def req(self, method, path, obj=None, headers=None, raw=None):
        body = raw if raw is not None else (
            json.dumps(obj).encode() if obj is not None else None)
        h = dict(headers or {})
        if obj is not None and "Content-Type" not in h:
            h["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path, data=body, headers=h, method=method)
        try:
            with urllib.request.urlopen(request) as r:
                payload = r.read()
                try:
                    return r.status, json.loads(payload), r.headers
                except json.JSONDecodeError:
                    return r.status, payload, r.headers
        except urllib.error.HTTPError as e:
            data = e.read()
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                obj = None
            return e.code, obj, e.headers

    def setup_owner(self, email="owner@x.test", name="Owner",
                    password=PW):
        status, d, h = self.req("POST", "/api/setup", {
            "email": email, "name": name, "password": password,
            "bootstrap_token": BOOT}, {"Origin": self.base})
        self.assertEqual(status, 200, d)
        return h["Set-Cookie"].split(";")[0]

    def login(self, email, password=PW):
        status, d, h = self.req("POST", "/api/login", {
            "email": email, "password": password},
            {"Origin": self.base})
        if status != 200:
            return None
        return h["Set-Cookie"].split(";")[0]

    def ch(self, cookie, ws=None, extra=None):
        h = {"Cookie": cookie, "Origin": self.base}
        if ws:
            h["X-Partial-Workspace"] = ws
        h.update(extra or {})
        return h

    def bh(self, token, ws=None):
        h = {"Authorization": f"Bearer {token}"}
        if ws:
            h["X-Partial-Workspace"] = ws
        return h

    def ws_of(self, cookie):
        status, me, _h = self.req(
            "GET", "/api/me", None, {"Cookie": cookie})
        assert status == 200
        return me["workspace"]["id"]

    def create_ws(self, cookie, name):
        status, d, _h = self.req(
            "POST", "/api/workspaces", {"name": name},
            self.ch(cookie))
        self.assertEqual(status, 201, d)
        return d["item"]["id"]

    def invite_token(self, cookie, ws, email, role):
        status, d, _h = self.req(
            "POST", f"/api/workspaces/{ws}/invites",
            {"email": email, "role": role}, self.ch(cookie))
        self.assertEqual(status, 201, d)
        return d["item"]["token"]

    def mint(self, cookie, ws, name="t", role="member"):
        status, d, _h = self.req(
            "POST", f"/api/workspaces/{ws}/tokens",
            {"name": name, "role": role}, self.ch(cookie))
        self.assertEqual(status, 201, d)
        return d["item"]


class IsolationTests(AccountsCase):
    def test_two_workspaces_same_ids_isolated(self):
        ca = self.setup_owner()
        ws1 = self.ws_of(ca)
        token_b = self.invite_token(ca, ws1, "b@x.test", "member")
        status, d, _h = self.req("POST", "/api/invites/accept", {
            "token": token_b, "email": "b@x.test", "name": "Bee",
            "password": PW2}, {"Origin": self.base})
        self.assertEqual(status, 200, d)
        cb = self.login("b@x.test", PW2)
        self.assertIsNotNone(cb)
        ws2 = self.create_ws(cb, "bee space")

        status, d, _h = self.req(
            "POST", "/api/bundles", fixture_bundle("alpha"),
            self.ch(ca))
        self.assertEqual(status, 200)
        status, d, _h = self.req(
            "POST", "/api/bundles", fixture_bundle("beta", "secret-b"),
            self.ch(cb, ws2))
        self.assertEqual(status, 200)

        status, d, _h = self.req(
            "GET", "/api/sessions", None, self.ch(ca))
        self.assertEqual(d["items"][0]["title"], "alpha")
        status, d, _h = self.req(
            "GET", "/api/sessions", None, self.ch(cb, ws2))
        self.assertEqual(d["items"][0]["title"], "beta")

        for path in ("/api/sessions", "/api/search?q=secret-b",
                     "/api/export", "/api/checkpoints/" + CHECKPOINT_ID,
                     "/api/sessions/" + SESSION_ID + "/handoff"):
            status, _d, _h = self.req(
                "GET", path, None, self.ch(ca, ws2))
            self.assertEqual(status, 404, path)
        status, _d, _h = self.req(
            "POST", "/api/bundles", fixture_bundle(),
            self.ch(ca, ws2))
        self.assertEqual(status, 404)
        status, _d, _h = self.req(
            "POST", "/api/checkpoints/" + CHECKPOINT_ID + "/reviews",
            {"body": "x"}, self.ch(ca, ws2))
        self.assertEqual(status, 404)
        status, d, _h = self.req(
            "GET", "/api/search?q=secret-b", None, self.ch(ca))
        self.assertEqual(d["items"], [])

    def test_token_scoped_to_one_workspace(self):
        ca = self.setup_owner()
        ws1 = self.ws_of(ca)
        ws2 = self.create_ws(ca, "second")
        item = self.mint(ca, ws1)
        tok = item["token"]
        status, me, _h = self.req(
            "GET", "/api/me", None, self.bh(tok))
        self.assertEqual([w["id"] for w in me["workspaces"]], [ws1])
        status, _d, _h = self.req(
            "GET", "/api/sessions", None, self.bh(tok, ws2))
        self.assertEqual(status, 404)
        status, _d, _h = self.req(
            "GET", "/api/sessions", None, self.bh(tok, ws1))
        self.assertEqual(status, 200)

    def test_revoked_and_expired_tokens(self):
        ca = self.setup_owner()
        ws1 = self.ws_of(ca)
        item = self.mint(ca, ws1)
        status, _d, _h = self.req(
            "DELETE", f"/api/tokens/{item['id']}", {},
            self.ch(ca))
        self.assertEqual(status, 200)
        status, _d, _h = self.req(
            "GET", "/api/me", None, self.bh(item["token"]))
        self.assertEqual(status, 401)
        expired = self.mint(ca, ws1, "old")
        conn = self.accounts._connect()
        try:
            conn.execute(
                "UPDATE api_tokens SET expires_at=? WHERE id=?",
                (time.time() - 1, expired["id"]))
            conn.commit()
        finally:
            conn.close()
        status, _d, _h = self.req(
            "GET", "/api/me", None, self.bh(expired["token"]))
        self.assertEqual(status, 401)

    def test_viewer_cannot_write(self):
        ca = self.setup_owner()
        ws1 = self.ws_of(ca)
        tok = self.invite_token(ca, ws1, "v@x.test", "viewer")
        self.req("POST", "/api/invites/accept", {
            "token": tok, "email": "v@x.test", "name": "V",
            "password": PW2}, {"Origin": self.base})
        cv = self.login("v@x.test", PW2)
        self.assertIsNotNone(cv)
        status, _d, _h = self.req(
            "GET", "/api/sessions", None, self.ch(cv))
        self.assertEqual(status, 200)
        status, _d, _h = self.req(
            "POST", "/api/bundles", fixture_bundle(), self.ch(cv))
        self.assertEqual(status, 403)
        status, _d, _h = self.req(
            "POST", "/api/checkpoints/" + CHECKPOINT_ID + "/reviews",
            {"body": "x"}, self.ch(cv))
        self.assertEqual(status, 403)
        status, _d, _h = self.req(
            "POST", f"/api/workspaces/{ws1}/invites",
            {"email": "e@x.test", "role": "member"}, self.ch(cv))
        self.assertEqual(status, 403)

    def test_role_downgrade_immediate(self):
        ca = self.setup_owner()
        ws1 = self.ws_of(ca)
        tok = self.invite_token(ca, ws1, "m@x.test", "member")
        self.req("POST", "/api/invites/accept", {
            "token": tok, "email": "m@x.test", "name": "M",
            "password": PW2}, {"Origin": self.base})
        cm = self.login("m@x.test", PW2)
        status, me, _h = self.req(
            "GET", "/api/me", None, {"Cookie": ca})
        uid_m = next(
            u["user_id"] for u in self.req(
                "GET", f"/api/workspaces/{ws1}/members", None,
                self.ch(ca))[1]["items"] if u["email"] == "m@x.test")
        status, _d, _h = self.req(
            "PATCH", f"/api/workspaces/{ws1}/members/{uid_m}",
            {"role": "viewer"}, self.ch(ca))
        self.assertEqual(status, 200)
        status, _d, _h = self.req(
            "POST", "/api/bundles", fixture_bundle(), self.ch(cm))
        self.assertEqual(status, 403)
        status, _d, _h = self.req(
            "DELETE", f"/api/workspaces/{ws1}/members/{uid_m}",
            {}, self.ch(ca))
        self.assertEqual(status, 200)
        status, _d, _h = self.req(
            "GET", "/api/sessions", None, self.ch(cm))
        self.assertEqual(status, 404)

    def test_last_owner_protected(self):
        ca = self.setup_owner()
        ws1 = self.ws_of(ca)
        status, me, _h = self.req(
            "GET", "/api/me", None, {"Cookie": ca})
        uid = me["user"]["id"]
        for method in ("PATCH", "DELETE"):
            obj = {"role": "member"} if method == "PATCH" else {}
            status, _d, _h = self.req(
                method, f"/api/workspaces/{ws1}/members/{uid}",
                obj, self.ch(ca))
            self.assertEqual(status, 409, method)

    def test_setup_concurrent_single_winner(self):
        results = []

        def attempt(email):
            status, _d, _h = self.req("POST", "/api/setup", {
                "email": email, "name": "N", "password": PW,
                "bootstrap_token": BOOT}, {"Origin": self.base})
            results.append(status)

        threads = [threading.Thread(
            target=attempt, args=(f"u{i}@x.test",))
            for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [200, 409, 409, 409])

    def test_legacy_data_bound_to_first_owner(self):
        self.store.import_bundle(fixture_bundle("legacy"))
        ca = self.setup_owner()
        ws1 = self.ws_of(ca)
        status, d, _h = self.req(
            "GET", "/api/sessions", None, self.ch(ca))
        self.assertEqual(d["items"][0]["title"], "legacy")
        tok = self.invite_token(ca, ws1, "c@x.test", "member")
        self.req("POST", "/api/invites/accept", {
            "token": tok, "email": "c@x.test", "name": "C",
            "password": PW2}, {"Origin": self.base})
        cc = self.login("c@x.test", PW2)
        ws2 = self.create_ws(cc, "other")
        status, d, _h = self.req(
            "GET", "/api/sessions", None, self.ch(cc, ws2))
        self.assertEqual(d["items"], [])

    def test_invite_rules(self):
        ca = self.setup_owner()
        ws1 = self.ws_of(ca)
        tok = self.invite_token(ca, ws1, "n@x.test", "member")
        status, _d, _h = self.req("POST", "/api/invites/accept", {
            "token": tok, "email": "wrong@x.test", "name": "N",
            "password": PW2}, {"Origin": self.base})
        self.assertEqual(status, 400)
        status, _d, _h = self.req("POST", "/api/invites/accept", {
            "token": "bogus", "email": "n@x.test", "name": "N",
            "password": PW2}, {"Origin": self.base})
        self.assertEqual(status, 400)
        status, _d, _h = self.req("POST", "/api/invites/accept", {
            "token": tok, "email": "n@x.test", "name": "N",
            "password": PW2}, {"Origin": self.base})
        self.assertEqual(status, 200)
        status, _d, _h = self.req("POST", "/api/invites/accept", {
            "token": tok, "email": "n@x.test", "name": "N",
            "password": PW2}, {"Origin": self.base})
        self.assertEqual(status, 400)
        tok2 = self.invite_token(ca, ws1, "x@x.test", "member")
        conn = self.accounts._connect()
        try:
            conn.execute(
                "UPDATE invites SET expires_at=? WHERE token_hash=?",
                (time.time() - 1,
                 hashlib.sha256(tok2.encode()).hexdigest()))
            conn.commit()
        finally:
            conn.close()
        status, _d, _h = self.req("POST", "/api/invites/accept", {
            "token": tok2, "email": "x@x.test", "name": "X",
            "password": PW2}, {"Origin": self.base})
        self.assertEqual(status, 400)

    def test_existing_user_invite_requires_login(self):
        ca = self.setup_owner()
        ws1 = self.ws_of(ca)
        ws2 = self.create_ws(ca, "ws2")
        tok = self.invite_token(ca, ws1, "d@x.test", "member")
        self.req("POST", "/api/invites/accept", {
            "token": tok, "email": "d@x.test", "name": "D",
            "password": PW2}, {"Origin": self.base})
        tok2 = self.invite_token(ca, ws2, "d@x.test", "member")
        status, _d, _h = self.req("POST", "/api/invites/accept", {
            "token": tok2, "email": "d@x.test", "name": "D",
            "password": "different password"}, {"Origin": self.base})
        self.assertEqual(status, 409)
        cd = self.login("d@x.test", PW2)
        status, _d, _h = self.req("POST", "/api/invites/accept", {
            "token": tok2, "email": "d@x.test", "name": "D",
            "password": "x"}, self.ch(cd))
        self.assertEqual(status, 200)
        status, me, _h = self.req(
            "GET", "/api/me", None, {"Cookie": cd})
        self.assertEqual(len(me["workspaces"]), 2)

    def test_no_secrets_in_dtos(self):
        ca = self.setup_owner()
        ws1 = self.ws_of(ca)
        self.mint(ca, ws1)
        for path in ("/api/me", "/api/workspaces",
                     f"/api/workspaces/{ws1}/members",
                     f"/api/workspaces/{ws1}/tokens",
                     f"/api/workspaces/{ws1}/audit"):
            status, d, _h = self.req(
                "GET", path, None, {"Cookie": ca})
            self.assertEqual(status, 200, path)
            text = json.dumps(d)
            self.assertNotIn("password", text)
            self.assertNotIn("hash", text)
            self.assertNotIn(BOOT, text)

    def test_sessions_survive_restart(self):
        self.setup_owner()
        cookie = self.login("owner@x.test")
        self.assertIsNotNone(cookie)
        raw = cookie.split("=", 1)[1]
        accounts2 = Accounts(self.home, self.home / "partial.db")
        p = accounts2.authenticate(raw)
        self.assertIsNotNone(p)
        self.assertEqual(p.email, "owner@x.test")

    def test_password_change_revokes(self):
        ca = self.setup_owner()
        ws1 = self.ws_of(ca)
        item = self.mint(ca, ws1)
        status, _d, _h = self.req(
            "POST", "/api/account/password",
            {"current_password": PW, "new_password": PW2},
            self.ch(ca))
        self.assertEqual(status, 200)
        status, _d, _h = self.req(
            "GET", "/api/me", None, {"Cookie": ca})
        self.assertEqual(status, 401)
        status, _d, _h = self.req(
            "GET", "/api/me", None, self.bh(item["token"]))
        self.assertEqual(status, 401)
        self.assertIsNone(self.login("owner@x.test", PW))
        self.assertIsNotNone(self.login("owner@x.test", PW2))

    def test_token_cannot_administer(self):
        ca = self.setup_owner()
        ws1 = self.ws_of(ca)
        for role in ("admin", "owner"):
            status, _d, _h = self.req(
                "POST", f"/api/workspaces/{ws1}/tokens",
                {"name": "x", "role": role}, self.ch(ca))
            self.assertEqual(status, 400, role)
        item = self.mint(ca, ws1, "adm", "member")
        for method, path, obj in (
                ("POST", "/api/workspaces", {"name": "x"}),
                ("POST", f"/api/workspaces/{ws1}/invites",
                 {"email": "e@x.test", "role": "member"}),
                ("PATCH", f"/api/workspaces/{ws1}/members/x",
                 {"role": "viewer"}),
                ("GET", f"/api/workspaces/{ws1}/members", None),
                ("GET", f"/api/workspaces/{ws1}/audit", None)):
            status, _d, _h = self.req(
                method, path, obj, self.bh(item["token"]))
            self.assertEqual(status, 403, (method, path))

    def test_injection_inputs(self):
        ca = self.setup_owner()
        status, _d, _h = self.req("POST", "/api/login", {
            "email": "' OR '1'='1", "password": "x" * 12},
            {"Origin": self.base})
        self.assertIn(status, (400, 401))
        status, d, _h = self.req(
            "GET", "/api/search?q=%25%20OR%201%3D1--", None,
            self.ch(ca))
        self.assertEqual(status, 200)

    def test_db_file_permissions(self):
        home2 = Path(self.dir) / "existing-home"
        home2.mkdir()
        os.chmod(home2, 0o755)
        Accounts(home2, home2 / "partial.db")
        self.assertEqual(stat.S_IMODE(os.stat(home2).st_mode), 0o755)
        self.assertEqual(
            stat.S_IMODE(os.stat(home2 / "accounts.db").st_mode),
            0o600)
        os.chmod(home2 / "accounts.db", 0o644)
        Accounts(home2, home2 / "partial.db")
        self.assertEqual(
            stat.S_IMODE(os.stat(home2 / "accounts.db").st_mode),
            0o600)

    def test_browser_only_class_boundary(self):
        ca = self.setup_owner()
        ws1 = self.ws_of(ca)
        item = self.mint(ca, ws1)
        p = self.accounts.authenticate(
            item["token"], api_token=True)
        self.assertIsNotNone(p.token_id)
        calls = (
            lambda: self.accounts.create_workspace(p, "x"),
            lambda: self.accounts.invite(
                p, ws1, "e@x.test", "member"),
            lambda: self.accounts.members(p, ws1),
            lambda: self.accounts.change_member(
                p, ws1, "u", "member"),
            lambda: self.accounts.remove_member(p, ws1, "u"),
            lambda: self.accounts.create_api_token(
                p, ws1, "t", "member"),
            lambda: self.accounts.list_api_tokens(p, ws1),
            lambda: self.accounts.revoke_api_token(p, "t"),
            lambda: self.accounts.change_password(p, PW, PW2),
            lambda: self.accounts.list_audit(p, ws1),
            lambda: self.accounts.accept_invite(
                token="t", email="e@x.test", name="E",
                password=PW2, principal=p),
        )
        for call in calls:
            with self.assertRaises(AccountsError) as cm:
                call()
            self.assertEqual(cm.exception.code, 403)
        scoped = self.accounts.workspaces(p)
        self.assertEqual([w["id"] for w in scoped], [ws1])

    def test_scoped_token_cannot_revoke_other_workspace(self):
        ca = self.setup_owner()
        ws1 = self.ws_of(ca)
        ws2 = self.create_ws(ca, "ws2")
        t1 = self.mint(ca, ws1)
        t2 = self.mint(ca, ws2)
        status, _d, _h = self.req(
            "DELETE", f"/api/tokens/{t2['id']}", {},
            self.bh(t1["token"]))
        self.assertEqual(status, 403)
        status, _d, _h = self.req(
            "DELETE", f"/api/tokens/{t1['id']}", {},
            self.bh(t1["token"]))
        self.assertEqual(status, 403)
        status, _d, _h = self.req(
            "GET", "/api/me", None, self.bh(t2["token"]))
        self.assertEqual(status, 200)

    def test_scoped_token_cannot_accept_invite(self):
        ca = self.setup_owner()
        ws1 = self.ws_of(ca)
        ws2 = self.create_ws(ca, "ws2")
        tok = self.invite_token(ca, ws2, "new@x.test", "member")
        t1 = self.mint(ca, ws1)
        status, _d, _h = self.req("POST", "/api/invites/accept", {
            "token": tok, "email": "new@x.test"},
            {"Origin": self.base,
             "Authorization": f"Bearer {t1['token']}"})
        self.assertEqual(status, 403)
        status, _d, _h = self.req("POST", "/api/invites/accept", {
            "token": tok, "email": "new@x.test", "name": "N",
            "password": PW2}, {"Origin": self.base})
        self.assertEqual(status, 200)

    def test_stale_inviter_rejected(self):
        ca = self.setup_owner()
        ws1 = self.ws_of(ca)
        tok = self.invite_token(ca, ws1, "a@x.test", "admin")
        status, _d, _h = self.req("POST", "/api/invites/accept", {
            "token": tok, "email": "a@x.test", "name": "A",
            "password": PW2}, {"Origin": self.base})
        self.assertEqual(status, 200)
        cadm = self.login("a@x.test", PW2)
        self.assertIsNotNone(cadm)
        stale = self.invite_token(cadm, ws1, "e@x.test", "member")
        members = self.req(
            "GET", f"/api/workspaces/{ws1}/members", None,
            self.ch(ca))[1]["items"]
        uid_a = next(
            m["user_id"] for m in members
            if m["email"] == "a@x.test")
        status, _d, _h = self.req(
            "PATCH", f"/api/workspaces/{ws1}/members/{uid_a}",
            {"role": "member"}, self.ch(ca))
        self.assertEqual(status, 200)
        status, _d, _h = self.req("POST", "/api/invites/accept", {
            "token": stale, "email": "e@x.test", "name": "E",
            "password": PW2}, {"Origin": self.base})
        self.assertEqual(status, 400)
        members = self.req(
            "GET", f"/api/workspaces/{ws1}/members", None,
            self.ch(ca))[1]["items"]
        self.assertNotIn(
            "e@x.test", [m["email"] for m in members])

    def test_existing_member_invite_conflict(self):
        ca = self.setup_owner()
        ws1 = self.ws_of(ca)
        tok = self.invite_token(ca, ws1, "b@x.test", "member")
        status, _d, _h = self.req("POST", "/api/invites/accept", {
            "token": tok, "email": "b@x.test", "name": "B",
            "password": PW2}, {"Origin": self.base})
        self.assertEqual(status, 200)
        tok2 = self.invite_token(ca, ws1, "b@x.test", "member")
        cb = self.login("b@x.test", PW2)
        status, _d, _h = self.req("POST", "/api/invites/accept", {
            "token": tok2, "email": "b@x.test"}, self.ch(cb))
        self.assertEqual(status, 409)
        members = self.req(
            "GET", f"/api/workspaces/{ws1}/members", None,
            self.ch(ca))[1]["items"]
        self.assertEqual(
            [m["email"] for m in members].count("b@x.test"), 1)
        conn = self.accounts._connect()
        try:
            row = conn.execute(
                "SELECT used_at FROM invites WHERE token_hash=?",
                (hashlib.sha256(tok2.encode()).hexdigest(),)
            ).fetchone()
            self.assertIsNone(row["used_at"])
        finally:
            conn.close()

    def test_session_cap_and_expired_pruning(self):
        self.setup_owner()
        status, me, _h = self.req("GET", "/api/me", None,
                                  {"Cookie": self.login(
                                      "owner@x.test")})
        uid = me["user"]["id"]
        conn = self.accounts._connect()
        try:
            for i in range(32):
                conn.execute(
                    "INSERT INTO auth_sessions(token_hash,user_id,"
                    "expires_at) VALUES(?,?,?)",
                    (f"{i:064x}", uid, time.time() + 3600))
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(AccountsError) as cm:
            self.accounts.login("owner@x.test", PW)
        self.assertEqual(cm.exception.code, 429)
        conn = self.accounts._connect()
        try:
            conn.execute(
                "UPDATE auth_sessions SET expires_at=0"
                " WHERE user_id=?", (uid,))
            conn.commit()
        finally:
            conn.close()
        raw, _p = self.accounts.login("owner@x.test", PW)
        self.assertTrue(raw)
        conn = self.accounts._connect()
        try:
            n = conn.execute(
                "SELECT COUNT(*) c FROM auth_sessions"
                " WHERE user_id=?", (uid,)).fetchone()["c"]
            self.assertEqual(n, 1)
        finally:
            conn.close()

    def test_owner_same_role_noop(self):
        ca = self.setup_owner()
        ws1 = self.ws_of(ca)
        status, me, _h = self.req(
            "GET", "/api/me", None, {"Cookie": ca})
        uid = me["user"]["id"]
        status, _d, _h = self.req(
            "PATCH", f"/api/workspaces/{ws1}/members/{uid}",
            {"role": "owner"}, self.ch(ca))
        self.assertEqual(status, 200)

    def test_viewer_mints_viewer_token_only(self):
        ca = self.setup_owner()
        ws1 = self.ws_of(ca)
        tok = self.invite_token(ca, ws1, "v@x.test", "viewer")
        self.req("POST", "/api/invites/accept", {
            "token": tok, "email": "v@x.test", "name": "V",
            "password": PW2}, {"Origin": self.base})
        cv = self.login("v@x.test", PW2)
        self.assertIsNotNone(cv)
        status, _d, _h = self.req(
            "POST", f"/api/workspaces/{ws1}/tokens",
            {"name": "m", "role": "member"}, self.ch(cv))
        self.assertEqual(status, 403)
        item = self.mint(cv, ws1, "vt", "viewer")
        status, _d, _h = self.req(
            "GET", "/api/sessions", None, self.bh(item["token"]))
        self.assertEqual(status, 200)
        status, _d, _h = self.req(
            "POST", "/api/bundles", fixture_bundle(),
            self.bh(item["token"]))
        self.assertEqual(status, 403)

    def test_api_requires_auth_before_init(self):
        status, _d, _h = self.req("GET", "/api/sessions")
        self.assertEqual(status, 401)
        status, _d, _h = self.req(
            "GET", "/api/me", None,
            {"Authorization": f"Bearer {BOOT}"})
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
