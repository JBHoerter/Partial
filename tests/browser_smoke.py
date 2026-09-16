import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

from partial.demo import create_demo_store
from partial.git import create_checkpoint
from partial.models import Event, scoped_session_id
from partial.provenance import Provenance
from partial.server import create_server
from partial.store import Store

TOKEN = "smoke-" + "t" * 40
EMAIL = "owner@smoke.test"
PASSWORD = "smoke passphrase"
TS = "2026-09-16T10:00:00Z"


def _ev(eid, sid, text, branch=None):
    return Event(id=eid, session_id=sid, agent="devin", kind="prompt",
                 timestamp=TS, text=text,
                 data={"branch": branch} if branch else {})


def build_auth_store(path):
    store = Store(path)
    root = Path(tempfile.mkdtemp(prefix="partial-smoke-repo-"))
    for args in (
        ["init", "-b", "main"],
        ["-c", "user.name=T", "-c", "user.email=t@e",
         "commit", "--allow-empty", "-m", "init"],
    ):
        subprocess.run(["git", "-C", str(root), *args],
                       check=True, capture_output=True)
    (root / "f.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(root), "add", "--", "f.py"],
                   check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=T",
         "-c", "user.email=t@e", "commit", "-m", "add f"],
        check=True, capture_output=True)
    repo = store.register_repo(root)
    rid = repo["id"]
    for i in range(55):
        branch = "special-b" if i == 54 else "main"
        evs = [_ev(f"e{i}", f"s{i}", f"filler session {i}")]
        store.ingest(rid, evs, worktree=str(root), branch=branch)
    sid0 = scoped_session_id(rid, "devin", "s0")
    cp = create_checkpoint(store, rid, session_ids=[sid0],
                           worktree=str(root))
    rd = {"id": rid, "root": str(root),
          "common_dir": str(root / ".git"), "remote": ""}
    prov = Provenance(store)
    payload = {"session_id": "s0", "tool_name": "edit",
               "tool_input": {"file_path": "f.py"},
               "tool_use_id": "call-1",
               "tool_response": {"success": True}}
    prov.before_tool(rd, "devin", payload)
    (root / "f.py").write_text("x = 1\ny = 2\n")
    prov.after_tool(rd, "devin", payload)
    (root / "f.py").write_text("x = 1\ny = 2\nz = 3\n")
    (root / "h.py").write_text("mystery = 1\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"],
                   check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=T",
         "-c", "user.email=t@e", "commit", "-m", "attributed"],
        check=True, capture_output=True)
    cp2 = create_checkpoint(store, rid, session_ids=[sid0],
                            worktree=str(root))
    evil = ("<img src=x onerror=window.__pwned=1>"
            " marker-dangerous-text")
    store.ingest(
        rid,
        [_ev("evil1", "evil-s", evil)],
        worktree=str(root))
    return store, rid, cp["id"], cp2["id"]


class Server:
    def __init__(self, store, token=TOKEN, demo=False):
        self.srv = create_server(store, host="127.0.0.1", port=0,
                                 token=token, demo=demo)
        self.thread = threading.Thread(
            target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def no_x_scroll(page):
    return page.evaluate(
        "document.body.scrollWidth <= window.innerWidth"
        " && document.documentElement.scrollWidth <= window.innerWidth")


class Smoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.auth_store, cls.rid, cls.cpid, cls.acpid = \
            build_auth_store(Path(cls.tmp.name) / "auth.db")
        cls.demo_store = create_demo_store()
        cls.auth = Server(cls.auth_store)
        cls.demo = Server(cls.demo_store, demo=True)
        import urllib.request
        req = urllib.request.Request(
            cls.auth.base + "/api/setup",
            data=json.dumps({
                "email": EMAIL, "name": "Smoke Owner",
                "password": PASSWORD, "bootstrap_token": TOKEN,
            }).encode(),
            headers={"Content-Type": "application/json",
                     "Origin": cls.auth.base},
            method="POST")
        with urllib.request.urlopen(req) as r:
            assert r.status == 200
        cls.errors = []
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.auth.stop()
        cls.demo.stop()
        cls.tmp.cleanup()

    def page(self, width=1280):
        ctx = self.browser.new_context(
            viewport={"width": width, "height": 800})
        page = ctx.new_page()

        def on_console(m):
            if m.type == "error" \
                    and "Failed to load resource" not in m.text:
                self.errors.append(m.text)
        page.on("console", on_console)
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        return ctx, page

    def login(self, page, base, email=EMAIL, password=PASSWORD):
        acc = self.auth.srv.partial_context.accounts
        raw, _p = acc.login(email, password)
        page.context.add_cookies([{
            "name": "partial_session", "value": raw, "url": base}])
        page.goto(base + "/app")
        page.wait_for_selector("#shell:not([hidden])")

    def api(self, method, path, obj=None, cookie=None, ws=None):
        h = {"Content-Type": "application/json",
             "Origin": self.auth.base}
        if cookie:
            h["Cookie"] = "partial_session=" + cookie
        if ws:
            h["X-Partial-Workspace"] = ws
        req = urllib.request.Request(
            self.auth.base + path,
            data=json.dumps(obj).encode() if obj is not None else None,
            headers=h, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return e.code, {}

    def test_01_demo_visibility_and_pages(self):
        ctx, page = self.page()
        page.goto(self.demo.base + "/app#/overview")
        page.wait_for_selector("text=Every change has a story")
        self.assertFalse(page.locator("#login").is_visible())
        self.assertTrue(page.locator("#demo-banner").is_visible())
        self.assertFalse(page.locator("#logout-btn").is_visible())
        page.screenshot(path="/tmp/partial-final-overview.png",
                        full_page=True)

        page.goto(self.demo.base + "/app#/sessions")
        page.wait_for_selector("table.list")
        page.locator('a[href^="#/sessions/"]',
                     has_text="Add cursor pagination").click()
        page.wait_for_selector(".timeline")
        page.locator("details.toolbox summary").first.click()
        self.assertTrue(page.locator("details.toolbox pre").first
                        .is_visible())
        page.screenshot(path="/tmp/partial-final-session.png",
                        full_page=True)

        page.goto(self.demo.base + "/app#/checkpoints")
        page.wait_for_selector("table.list")
        page.locator('a[href^="#/checkpoints/"]').first.click()
        page.wait_for_selector("details.diff-file")
        page.screenshot(path="/tmp/partial-final-checkpoint.png",
                        full_page=True)

        page.goto(self.demo.base + "/app#/repos")
        page.wait_for_selector("table.list")
        for text in ("Agents", "Sessions", "Checkpoints", "Branches",
                     "Last activity"):
            self.assertTrue(page.locator("th", has_text=text).count())
        page.get_by_role("link", name="orbit-api").click()
        page.wait_for_selector(".repo-head")
        self.assertTrue(page.locator("text=sub-agent").count())
        page.get_by_role("button", name="Checkpoints").click()
        page.wait_for_selector("text=100% AI")
        self.assertTrue(page.locator("text=+1 / −1").count())
        page.locator("tr", has_text="Add cursor pagination") \
            .locator('a[href^="#/checkpoints/"]').click()
        page.wait_for_selector("details.diff-file")
        self.assertTrue(page.locator("text=agent +1/−1").count())
        page.get_by_role("button", name="Sessions (2)").click()
        page.wait_for_selector("text=Review the pagination edge cases")
        self.assertTrue(page.locator("text=sub-agent").count())
        ctx.close()

    def test_02_mobile_no_overflow(self):
        ctx, page = self.page(375)
        for frag in ("overview", "repos", "sessions", "checkpoints",
                     "integrations"):
            page.goto(self.demo.base + f"/app#/{frag}")
            page.wait_for_timeout(400)
            self.assertTrue(no_x_scroll(page),
                            f"overflow on {frag}")
        page.goto(self.demo.base + "/app#/sessions")
        page.wait_for_selector("table.list")
        page.locator('a[href^="#/sessions/"]').first.click()
        page.wait_for_selector(".timeline")
        self.assertTrue(no_x_scroll(page), "overflow on session")
        page.screenshot(path="/tmp/partial-final-mobile.png")
        page.goto(self.demo.base + "/app#/checkpoints")
        page.wait_for_selector("table.list")
        page.locator('a[href^="#/checkpoints/"]').first.click()
        page.wait_for_selector("details.diff-file")
        self.assertTrue(no_x_scroll(page), "overflow on checkpoint")
        ctx.close()

        ctx, page = self.page(375)
        page.goto(self.auth.base + "/app")
        page.wait_for_selector("#login:not([hidden])")
        self.assertTrue(no_x_scroll(page), "overflow on login")
        page.screenshot(path="/tmp/partial-final-login.png")
        ctx.close()

    def test_03_login_logout_visibility(self):
        ctx, page = self.page()
        page.goto(self.auth.base + "/app#/sessions")
        page.wait_for_selector("#login:not([hidden])")
        self.assertFalse(page.locator("#shell").is_visible())
        page.fill("#login-email", EMAIL)
        page.fill("#login-password", "definitely-wrong")
        page.click("#login-form button[type=submit]")
        page.wait_for_selector("#login-error:not(:empty)")
        self.assertFalse(page.locator("#shell").is_visible())
        page.fill("#login-password", PASSWORD)
        page.click("#login-form button[type=submit]")
        page.wait_for_selector("table.list")
        self.assertFalse(page.locator("#login").is_visible())
        self.assertTrue(page.locator("#shell").is_visible())
        page.click("#logout-btn")
        page.wait_for_selector("#login:not([hidden])")
        self.assertFalse(page.locator("#shell").is_visible())
        self.assertEqual(
            page.evaluate("document.getElementById('view')"
                          ".textContent"), "")
        ctx.close()

    def test_04_filters_pagination_branch(self):
        ctx, page = self.page()
        self.login(page, self.auth.base)
        page.goto(self.auth.base + "/app#/sessions?branch=special-b")
        page.wait_for_selector("table.list")
        self.assertIn("filler session 54",
                      page.locator("#view").inner_text())
        page.goto(self.auth.base + "/app#/sessions?offset=50")
        page.wait_for_selector("table.list")
        self.assertIn("filler session 54",
                      page.locator("#view").inner_text())
        prev = page.locator(".pager button", has_text="Prev")
        self.assertTrue(prev.is_enabled())
        prev.click()
        page.wait_for_selector("text=filler session 0")

        repo_hash = f"#/repos/{self.rid}?tab=sessions"
        page.goto(self.auth.base + f"/app{repo_hash}")
        page.wait_for_selector("table.list")
        nxt = page.locator(".pager button", has_text="Next")
        self.assertTrue(nxt.is_enabled())
        nxt.click()
        page.wait_for_selector("text=filler session 54")
        ctx.close()

    def test_05_review_persists(self):
        ctx, page = self.page()
        self.login(page, self.auth.base)
        cpid = self.cpid
        page.goto(self.auth.base + f"/app#/checkpoints/{cpid}")
        page.wait_for_selector("textarea[aria-label='Review note']")
        page.fill("textarea[aria-label='Review note']",
                  "browser smoke note")
        page.click("button:has-text('Add note')")
        page.wait_for_selector(".notice-ok")
        items = self.auth_store.list_reviews(cpid)
        self.assertTrue(any(r["body"] == "browser smoke note"
                            for r in items))
        page.reload()
        page.wait_for_selector("text=browser smoke note")
        ctx.close()

    def test_06_bundle_download_upload(self):
        ctx, page = self.page()
        self.login(page, self.auth.base)
        resp = page.context.request.get(self.auth.base + "/api/export")
        self.assertTrue(resp.ok)
        bundle_file = Path(self.tmp.name) / "bundle.json"
        bundle_file.write_bytes(resp.body())
        page.goto(self.auth.base + "/app#/integrations")
        page.wait_for_selector("input[aria-label='Bundle file']")
        page.set_input_files("input[aria-label='Bundle file']",
                             str(bundle_file))
        page.click("button:has-text('Upload bundle')")
        page.wait_for_selector(".notice-ok")
        ctx.close()

    def test_07_stale_navigation_aborted(self):
        ctx, page = self.page()
        self.login(page, self.auth.base)
        release = threading.Event()

        def slow(route):
            release.wait(5)
            route.continue_()
        page.route("**/api/repos", slow)
        page.goto(self.auth.base + "/app#/repos")
        page.wait_for_selector(".loading")
        page.goto(self.auth.base + "/app#/overview")
        release.set()
        page.wait_for_selector("text=Every change has a story")
        page.wait_for_timeout(600)
        self.assertEqual(
            page.locator(".view h2", has_text="Repositories").count(),
            0)
        page.unroute("**/api/repos")
        ctx.close()

    def test_08_dangerous_text_renders_safely(self):
        ctx, page = self.page()
        self.login(page, self.auth.base)
        page.goto(self.auth.base + "/app#/sessions?q=dangerous")
        page.wait_for_selector("table.list")
        page.locator('a[href^="#/sessions/"]').first.click()
        page.wait_for_selector("text=marker-dangerous-text")
        self.assertIsNone(page.evaluate("window.__pwned || null"))
        self.assertEqual(
            page.locator('#view img[src="x"]').count(), 0)
        ctx.close()

    def test_09_clipboard_and_keyboard(self):
        ctx, page = self.page()
        self.login(page, self.auth.base)
        page.goto(self.auth.base + "/app#/sessions")
        page.wait_for_selector("table.list")
        page.locator('a[href^="#/sessions/"]').first.click()
        page.wait_for_selector(".timeline")
        page.locator("#view button.ghost",
                     has_text="Copy").first.click()
        page.wait_for_selector(
            "#view button.ghost:has-text('opied'),"
            " #view button.ghost:has-text('failed')")
        page.goto(self.auth.base + "/app#/checkpoints")
        page.wait_for_selector("table.list")
        page.locator('a[href^="#/checkpoints/"]').first.click()
        page.wait_for_selector("details.diff-file")
        summ = page.locator("details.diff-file summary.fname").first
        det = page.locator("details.diff-file").first
        self.assertTrue(det.evaluate("e => e.open"))
        summ.focus()
        page.keyboard.press("Enter")
        self.assertFalse(det.evaluate("e => e.open"))
        ctx.close()

    def test_10_workspaces_invite_isolation(self):
        ctx, page = self.page()
        self.login(page, self.auth.base)
        page.goto(self.auth.base + "/app#/settings")
        page.wait_for_selector("input[aria-label='Workspace name']")
        page.fill("input[aria-label='Workspace name']", "second-ws")
        page.locator(
            "#view button:has-text('Create workspace')").click()
        page.wait_for_selector(".notice-ok")
        sel = page.locator("#ws-select")
        self.assertEqual(sel.locator("option").count(), 2)
        sel.select_option(label="second-ws")
        page.wait_for_selector("text=Every change has a story")
        self.assertIn("No sessions captured",
                      page.locator("#view").inner_text())
        self.assertEqual(
            page.evaluate(
                "localStorage.getItem('partial_workspace_id')"),
            sel.input_value())
        sel.select_option(index=0)
        page.wait_for_selector("text=Every change has a story")

        page.goto(self.auth.base + "/app#/settings")
        page.wait_for_selector("input[aria-label='Invitee email']")
        page.fill("input[aria-label='Invitee email']",
                  "viewer@smoke.test")
        page.select_option("select[aria-label='Invite role']",
                           "viewer")
        page.locator(
            "#view button:has-text('Create invitation')").click()
        page.wait_for_selector(".notice-ok")
        invite_token = page.locator(
            "#view .cmdline code").last.inner_text()

        page.click("#logout-btn")
        page.wait_for_selector("#login:not([hidden])")
        page.click("#invite-toggle")
        page.fill("#invite-token", invite_token)
        page.fill("#invite-email", "viewer@smoke.test")
        page.fill("#invite-name", "Smoke Viewer")
        page.fill("#invite-password", "viewer passphrase")
        page.click("#invite-form button[type=submit]")
        page.wait_for_selector(
            "text=Invitation accepted")
        self.login(page, self.auth.base, "viewer@smoke.test",
                   "viewer passphrase")
        page.goto(
            self.auth.base + f"/app#/checkpoints/{self.cpid}")
        page.wait_for_selector("text=Review notes")
        self.assertEqual(
            page.locator("textarea[aria-label='Review note']")
            .count(), 0)
        self.assertIn("read-only",
                      page.locator("#view").inner_text())
        page.goto(self.auth.base + "/app#/integrations")
        page.wait_for_selector("input[aria-label='Bundle file']")
        self.assertTrue(page.locator(
            "input[aria-label='Bundle file']").is_disabled())
        self.assertEqual(
            page.locator("button:has-text('Upload bundle')")
            .is_enabled(), False)
        ctx.close()

    def test_11_token_shown_once_not_stored(self):
        ctx, page = self.page()
        self.login(page, self.auth.base)
        page.goto(self.auth.base + "/app#/settings")
        page.wait_for_selector("input[aria-label='Token name']")
        page.fill("input[aria-label='Token name']", "ephemeral")
        page.locator(
            "#view button:has-text('Create token')").click()
        page.wait_for_selector(".notice-ok")
        token = page.locator("#view .cmdline code").first.inner_text()
        self.assertTrue(token.startswith("ptk_"))
        stored = page.evaluate(
            "JSON.stringify(localStorage)")
        self.assertNotIn(token, stored)
        page.goto(self.auth.base + "/app#/overview")
        page.wait_for_selector("text=Every change has a story")
        self.assertNotIn(token, page.locator("#view").inner_text())
        ctx.close()

    def test_12_existing_user_accepts_invite(self):
        acc = self.auth.srv.partial_context.accounts
        raw_o, _p = acc.login(EMAIL, PASSWORD)
        status, me = self.api("GET", "/api/me", cookie=raw_o)
        self.assertEqual(status, 200)
        ws1 = me["workspace"]["id"]
        status, d = self.api(
            "POST", f"/api/workspaces/{ws1}/invites",
            {"email": "two@smoke.test", "role": "member"},
            cookie=raw_o)
        self.assertEqual(status, 201, d)
        status, d = self.api(
            "POST", "/api/invites/accept",
            {"token": d["item"]["token"], "email": "two@smoke.test",
             "name": "Two", "password": "two passphrase"})
        self.assertEqual(status, 200, d)
        raw2, _p = acc.login("two@smoke.test", "two passphrase")
        status, d = self.api(
            "POST", "/api/workspaces", {"name": "two-space"},
            cookie=raw2)
        self.assertEqual(status, 201, d)
        ws_b = d["item"]["id"]
        status, d = self.api(
            "POST", f"/api/workspaces/{ws_b}/invites",
            {"email": EMAIL, "role": "member"}, cookie=raw2)
        self.assertEqual(status, 201, d)
        tok = d["item"]["token"]

        ctx, page = self.page()
        self.login(page, self.auth.base)
        before = page.locator("#ws-select option").count()
        page.goto(self.auth.base + "/app#/settings")
        page.wait_for_selector(
            "#view input[aria-label='Invitation token']")
        page.fill("#view input[aria-label='Invitation token']", tok)
        page.locator(
            "#view button:has-text('Accept invitation')").click()
        page.wait_for_selector(".notice-ok")
        self.assertEqual(
            page.locator("#view input[aria-label='Invitation token']")
            .input_value(), "")
        labels = page.locator("#ws-select option").all_inner_texts()
        self.assertIn("two-space", labels)
        self.assertEqual(len(labels), before + 1)
        ctx.close()

    def test_13_workspace_switch_upload_race(self):
        acc = self.auth.srv.partial_context.accounts
        raw_o, _p = acc.login(EMAIL, PASSWORD)
        status, me = self.api("GET", "/api/me", cookie=raw_o)
        ws1 = me["workspace"]["id"]
        status, d = self.api(
            "POST", "/api/workspaces", {"name": "race-ws"},
            cookie=raw_o)
        self.assertEqual(status, 201, d)
        ws2 = d["item"]["id"]

        ctx, page = self.page()
        self.login(page, self.auth.base)
        resp = page.context.request.get(
            self.auth.base + "/api/export")
        bundle_file = Path(self.tmp.name) / "race.json"
        bundle_file.write_bytes(resp.body())
        release = threading.Event()
        seen = {}

        def hold(route):
            seen["ws"] = route.request.headers.get(
                "x-partial-workspace")
            release.wait(10)
            try:
                route.continue_()
            except Exception:
                # the page aborts in-flight requests on workspace
                # switch; the header was still captured above
                pass

        page.route("**/api/bundles", hold)
        page.goto(self.auth.base + "/app#/integrations")
        page.wait_for_selector("input[aria-label='Bundle file']")
        page.set_input_files("input[aria-label='Bundle file']",
                             str(bundle_file))
        page.click("button:has-text('Upload bundle')")
        for _ in range(50):
            if "ws" in seen:
                break
            page.wait_for_timeout(100)
        page.locator("#ws-select").select_option(ws2)
        release.set()
        page.wait_for_timeout(600)
        page.unroute("**/api/bundles")
        self.assertEqual(seen.get("ws"), ws1)
        status, d = self.api(
            "GET", "/api/sessions", cookie=raw_o, ws=ws2)
        self.assertEqual(status, 200)
        self.assertEqual(d["items"], [])
        ctx.close()

    def test_14_settings_role_controls(self):
        acc = self.auth.srv.partial_context.accounts
        raw2, _p = acc.login("two@smoke.test", "two passphrase")
        status, me = self.api("GET", "/api/me", cookie=raw2)
        uid_two = me["user"]["id"]
        raw_o, _p = acc.login(EMAIL, PASSWORD)
        status, me = self.api("GET", "/api/me", cookie=raw_o)
        ws1 = me["workspace"]["id"]
        status, d = self.api(
            "PATCH", f"/api/workspaces/{ws1}/members/{uid_two}",
            {"role": "admin"}, cookie=raw_o)
        self.assertEqual(status, 200, d)

        ctx, page = self.page()
        self.login(page, self.auth.base)
        page.goto(self.auth.base + "/app#/settings")
        page.wait_for_selector("select[aria-label='Token role']")
        opts = sorted(page.locator(
            "select[aria-label='Token role'] option")
            .all_inner_texts())
        self.assertEqual(opts, ["member", "viewer"])
        self.assertEqual(page.locator(
            "select[aria-label='Token role']").input_value(),
            "member")
        ctx.close()

        ctx, page = self.page()
        self.login(page, self.auth.base, "two@smoke.test",
                   "two passphrase")
        page.goto(self.auth.base + "/app#/settings")
        page.wait_for_selector("table.list")
        mpanel = page.locator(".panel").filter(
            has=page.locator("h3", has_text="Members")).first
        owner_row = mpanel.locator("tr", has_text=EMAIL)
        self.assertEqual(
            owner_row.locator("select").count(), 0)
        self.assertEqual(
            owner_row.locator("button", has_text="Remove").count(),
            0)
        ctx.close()

    def test_15_attribution_card_and_badges(self):
        ctx, page = self.page()
        self.login(page, self.auth.base)
        page.goto(
            self.auth.base + f"/app#/checkpoints/{self.acpid}")
        page.wait_for_selector(".attr-card")
        text = page.locator(".attr-card").inner_text()
        self.assertIn("AI attribution estimate", text)
        self.assertIn("agent share 33.33%", text)
        self.assertIn("coverage 66.67%", text)
        self.assertNotIn("100%", text)
        self.assertIn("local observation", text)
        labels = page.locator(".attr-badge").all_inner_texts()
        self.assertIn("AI", labels)
        self.assertIn("HU", labels)
        self.assertIn("?", labels)
        det = page.locator(".attr-card details summary")
        det.click()
        rows = page.locator(".attr-card table tr").all_inner_texts()
        joined = "\n".join(rows)
        self.assertIn("f.py", joined)
        self.assertIn("h.py", joined)
        self.assertIn("tool-pair", joined)
        self.assertIn("external", joined)
        ctx.close()

    def test_16_memory_pages(self):
        ctx, page = self.page()
        # demo workspace: memory pre-indexed, read-only
        page.goto(self.demo.base + "/app#/memory?q=pagination")
        page.wait_for_selector("table.list")
        self.assertIn("result(s)", page.locator("#view").inner_text())
        page.locator('#view table a[href="#/memory"]').first.click()
        page.wait_for_selector(".doc-overlay")
        self.assertIn("cursor pagination",
                      page.locator(".doc-overlay").inner_text().lower())
        page.locator(".doc-overlay button").first.click()

        page.goto(self.demo.base + "/app#/graph?q=activity_feed")
        page.wait_for_selector("table.list")
        self.assertIn("activity_feed",
                      page.locator("#view").inner_text())
        page.locator('a[href^="#/graph?symbol="]').first.click()
        page.wait_for_selector("text=analysis")

        page.goto(self.demo.base + "/app#/decisions")
        page.wait_for_selector(
            "text=Cursor pagination for the activity feed")
        self.assertIn("read-only",
                      page.locator("#view").inner_text().lower())

        page.goto(self.demo.base + "/app#/dispatch")
        page.wait_for_selector("pre.code-view")
        self.assertIn("cursor pagination",
                      page.locator("#view").inner_text().lower())
        ctx.close()

        # auth workspace: index via API, then use UI
        ctx, page = self.page()
        self.login(page, self.auth.base)
        acc = self.auth.srv.partial_context.accounts
        raw, _p = acc.login(EMAIL, PASSWORD)
        st, _ = self.api("POST", "/api/memory/index", {},
                         cookie=raw)
        self.assertEqual(st, 200)
        st, _ = self.api("POST", "/api/workflows",
                         {"kind": "ask", "query": "what changed",
                          "run": False},
                         cookie=raw)
        self.assertEqual(st, 200)
        page.goto(self.auth.base + "/app#/memory?q=filler")
        page.wait_for_selector("table.list")
        page.goto(self.auth.base + "/app#/workflows")
        page.wait_for_selector("table.list")
        self.assertIn("planned",
                      page.locator("#view").inner_text())
        page.locator('a[href^="#/workflows/"]').first.click()
        page.wait_for_selector("pre.code-view")
        # decision creation via UI
        page.goto(self.auth.base + "/app#/decisions")
        page.wait_for_selector("text=Record a decision")
        page.fill('input[placeholder="Title"]', "Keep stdlib only")
        page.fill('textarea[placeholder="Body"]', "No new deps.")
        page.click("button:has-text('Record decision')")
        page.wait_for_selector("text=[active] Keep stdlib only")
        ctx.close()

    def test_17_memory_overlay_lifecycle(self):
        acc = self.auth.srv.partial_context.accounts
        raw, _p = acc.login(EMAIL, PASSWORD)
        st, me = self.api("GET", "/api/me", cookie=raw)
        self.assertEqual(st, 200)
        ws1 = me["workspace"]["id"]
        st, _ = self.api("POST", "/api/memory/index", {},
                         cookie=raw)
        self.assertEqual(st, 200)
        # supersede a decision so an archived document exists
        st, d = self.api(
            "POST", "/api/decisions",
            {"repo_id": self.rid, "title": "Old call",
             "body": "first"}, cookie=raw)
        self.assertEqual(st, 201, d)
        old_dec = d["item"]["id"]
        st, d = self.api(
            "POST", "/api/decisions",
            {"repo_id": self.rid, "title": "New call",
             "body": "second", "supersedes": old_dec}, cookie=raw)
        self.assertEqual(st, 201, d)
        conn = self.auth_store._connect()
        try:
            row = conn.execute(
                "SELECT id FROM memory_documents"
                " WHERE archived=1 LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        archived_doc = row["id"]
        # second workspace must exist before login so it appears in
        # the workspace switcher rendered at boot
        st, ws2 = self.api("POST", "/api/workspaces",
                           {"name": "ws-overlay"}, cookie=raw)
        self.assertEqual(st, 201, ws2)
        ws2 = ws2["item"]["id"]

        ctx, page = self.page()
        self.login(page, self.auth.base)
        page.goto(self.auth.base + "/app#/memory?q=filler")
        page.wait_for_selector("table.list")
        text = page.locator("#view").inner_text()
        self.assertIn("does not scan the filesystem", text)
        self.assertTrue(page.locator(
            "button:has-text('Index captured history')")
            .is_visible())
        # semantic toggle only when provider+policy allow it
        self.assertIn("Semantic search unavailable", text)
        self.assertEqual(page.locator(
            "input[aria-label='Semantic search']").count(), 0)
        # kind tabs present
        tabs = page.locator(".tabs [role=tab]")
        self.assertEqual(tabs.count(), 5)
        tabs.filter(has_text="Sessions").click()
        page.wait_for_function(
            "location.hash.includes('kind=session')")
        page.wait_for_selector("table.list")

        # open overlay, check dialog semantics + Escape + focus
        link = page.locator('#view table a[href="#/memory"]').first
        link.click()
        page.wait_for_selector(".doc-overlay")
        ov = page.locator(".doc-overlay")
        self.assertEqual(ov.get_attribute("role"), "dialog")
        self.assertEqual(ov.get_attribute("aria-modal"), "true")
        page.keyboard.press("Escape")
        page.wait_for_selector(".doc-overlay", state="detached")
        self.assertEqual(
            page.evaluate("document.activeElement.tagName"), "A")

        # archived doc still opens by id
        page.fill("input[aria-label='Open document by id']",
                  archived_doc)
        page.click("button:has-text('Open doc')")
        page.wait_for_selector(".doc-overlay")
        self.assertIn("Old call",
                      page.locator(".doc-overlay").inner_text())
        # only one overlay at a time
        page.keyboard.press("Escape")
        page.wait_for_selector(".doc-overlay", state="detached")
        page.locator('#view table a[href="#/memory"]').first.click()
        page.wait_for_selector(".doc-overlay")
        self.assertEqual(page.locator(".doc-overlay").count(), 1)

        # workspace switch removes the overlay
        page.locator("#ws-select").select_option(ws2)
        page.wait_for_selector("text=Every change has a story")
        self.assertEqual(page.locator(".doc-overlay").count(), 0)
        self.assertIn("No sessions captured",
                      page.locator("#view").inner_text())
        # switch back, open overlay, logout removes it
        page.locator("#ws-select").select_option(ws1)
        page.wait_for_selector("text=Every change has a story")
        page.goto(self.auth.base + "/app#/memory?q=filler")
        page.wait_for_selector("table.list")
        page.locator('#view table a[href="#/memory"]').first.click()
        page.wait_for_selector(".doc-overlay")
        page.locator("#logout-btn").dispatch_event("click")
        page.wait_for_selector("#login:not([hidden])")
        self.assertEqual(page.locator(".doc-overlay").count(), 0)
        ctx.close()

    def test_18_memory_error_state(self):
        ctx, page = self.page()
        self.login(page, self.auth.base)
        page.route("**/api/memory/search*",
                   lambda route: route.abort())
        page.goto(self.auth.base + "/app#/memory?q=filler")
        page.wait_for_selector("text=Could not load")
        page.unroute("**/api/memory/search*")
        ctx.close()

    def test_19_graph_neighbors_impact(self):
        # give the demo graph one real edge so tables render
        from partial.memory import _symbol_id
        from partial.models import sha256_hex
        orbit = sha256_hex("partial-demo:orbit-api")
        mod = _symbol_id(orbit, "src/routes/activity.py",
                         "src.routes.activity")
        fn = _symbol_id(orbit, "src/routes/activity.py",
                        "activity_feed")
        conn = self.demo_store._connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO graph_edges(repo_id,"
                "source_id,target_id,kind) VALUES(?,?,?,?)",
                (orbit, fn, mod, "calls"))
            conn.commit()
        finally:
            conn.close()

        ctx, page = self.page()
        page.goto(self.demo.base + "/app#/graph?q=activity_feed")
        page.wait_for_selector("table.list")
        self.assertIn("lexical inventory",
                      page.locator("#view").inner_text())
        page.locator('a[href^="#/graph?symbol="]').first.click()
        page.wait_for_selector("text=analysis")
        text = page.locator("#view").inner_text()
        self.assertIn("Neighbor nodes", text)
        self.assertIn("src.routes.activity", text)
        self.assertIn("calls", text)
        self.assertIn("static analysis only", text)

        # impact on the module shows the caller-side table
        page.goto(self.demo.base + "/app#/graph?q=src.routes")
        page.wait_for_selector("table.list")
        page.locator("button:has-text('impact')").first.click()
        page.wait_for_selector(".impact-panel table.list")
        self.assertIn("activity_feed",
                      page.locator(".impact-panel").inner_text())
        self.assertIn("incoming edges only",
                      page.locator(".impact-panel").inner_text())

        # impact on a symbol with no callers shows an empty state
        page.goto(self.demo.base + "/app#/graph?q=activity_feed")
        page.wait_for_selector("table.list")
        page.locator("button:has-text('impact')").first.click()
        page.wait_for_selector(".impact-panel")
        self.assertIn("No caller-side symbols",
                      page.locator(".impact-panel").inner_text())
        ctx.close()

    def test_20_dispatch_preview(self):
        ctx, page = self.page()
        page.goto(self.demo.base + "/app#/dispatch")
        page.wait_for_selector("pre.code-view")
        text = page.locator("#view").inner_text()
        self.assertIn("Deterministic recap", text)
        self.assertIn("no AI call", text)
        self.assertIn("cursor pagination", text.lower())
        self.assertTrue(page.locator(
            "button:has-text('Copy')").first.is_visible())
        dl = page.locator("a[download]")
        self.assertTrue(dl.is_visible())
        self.assertEqual(dl.get_attribute("download"),
                         "partial-dispatch.md")
        page.locator(
            "summary:has-text('Source checkpoints')").click()
        self.assertTrue(page.locator(
            'a[href^="#/checkpoints/"]').first.is_visible())

        page.goto(self.auth.base + "/app#/dispatch")
        page.wait_for_selector("#login:not([hidden])")
        ctx.close()

        # empty window shows the empty state
        ctx, page = self.page()
        page.goto(self.demo.base +
                  "/app#/dispatch?since=2026-09-01&until=2026-09-02")
        page.wait_for_selector("text=No checkpoints recorded")
        self.assertIn("2026-09-01",
                      page.locator("#view").inner_text())
        ctx.close()

    def test_21_workflow_states(self):
        acc = self.auth.srv.partial_context.accounts
        raw, _p = acc.login(EMAIL, PASSWORD)
        st, _ = self.api("POST", "/api/memory/index", {},
                         cookie=raw)
        self.assertEqual(st, 200)
        # plan review/investigate via API — evidence packet only
        for kind in ("review", "investigate"):
            st, d = self.api(
                "POST", "/api/workflows",
                {"kind": kind, "query": "check it", "run": False},
                cookie=raw)
            self.assertEqual(st, 200, d)
            self.assertEqual(d["status"], "planned")
        # force an error run locally — no provider configured
        from partial.workflows import run_workflow
        try:
            run_workflow(self.auth_store, "ask",
                         query="will fail", run=True)
        except ValueError:
            pass

        ctx, page = self.page()
        self.login(page, self.auth.base)
        page.goto(self.auth.base + "/app#/workflows")
        page.wait_for_selector("table.list")
        text = page.locator("#view").inner_text()
        self.assertIn("planned", text)
        self.assertIn("error", text)
        self.assertIn("no agents run on this server",
                      text.lower())
        # run checkbox is gated without provider/policy
        self.assertTrue(page.locator(
            "input[aria-label='Run with AI provider']")
            .is_disabled())

        # error run detail shows a visible error
        page.locator('a[href^="#/workflows/"]').first.click()
        page.wait_for_selector("#view .notice-err")
        self.assertIn("external AI disabled",
                      page.locator("#view").inner_text())
        self.assertIn("the run failed",
                      page.locator("#view").inner_text())

        # plan a workflow via the UI (no confirmation needed)
        page.goto(self.auth.base + "/app#/workflows")
        page.wait_for_selector("table.list")
        before = page.locator('a[href^="#/workflows/"]').count()
        page.select_option("select[aria-label='Workflow kind']",
                           "investigate")
        page.fill("input[aria-label='Workflow question']",
                  "filler")
        page.locator("#view button:has-text('Submit')").click()
        page.wait_for_selector("#errbar.show")
        self.assertIn("planned",
                      page.locator("#errbar").inner_text())
        links = page.locator('#view a[href^="#/workflows/"]')
        for _ in range(50):
            if links.count() == before + 1:
                break
            page.wait_for_timeout(100)
        self.assertEqual(links.count(), before + 1)
        page.locator('a[href^="#/workflows/"]').first.click()
        page.wait_for_selector("pre.code-view")
        text = page.locator("#view").inner_text()
        self.assertIn("planned", text)
        self.assertIn("no external request was made", text)
        self.assertIn("evidence:", text)
        ctx.close()

    def test_22_project_groups(self):
        ctx, page = self.page()
        self.login(page, self.auth.base)
        page.goto(self.auth.base + "/app#/repos")
        page.wait_for_selector(
            "input[aria-label='New project name']")
        self.assertIn("trust boundary",
                      page.locator("#view").inner_text())
        page.fill("input[aria-label='New project name']",
                  "Smoke Group")
        page.locator(
            "#view button:has-text('Create project')").click()
        page.wait_for_selector("text=Smoke Group")
        page.select_option("select[aria-label='Project']",
                           label="Smoke Group")
        page.locator(
            "#view button:has-text('Attach repository')").click()
        page.wait_for_timeout(400)
        acc = self.auth.srv.partial_context.accounts
        raw, _p = acc.login(EMAIL, PASSWORD)
        st, d = self.api("GET", "/api/projects", cookie=raw)
        self.assertEqual(st, 200)
        grp = [p for p in d["items"] if p["name"] == "Smoke Group"]
        self.assertEqual(len(grp), 1)
        self.assertIn(self.rid, grp[0]["repos"])
        ctx.close()

    def test_23_mobile_memory_pages(self):
        ctx, page = self.page(375)
        for frag in ("memory?q=pagination", "graph?q=activity_feed",
                     "decisions", "dispatch", "workflows"):
            page.goto(self.demo.base + f"/app#/{frag}")
            page.wait_for_timeout(500)
            self.assertTrue(no_x_scroll(page),
                            f"overflow on {frag}")
        # sidebar opens, footer/sign-out reachable, Escape closes
        page.goto(self.demo.base + "/app#/overview")
        page.wait_for_timeout(300)
        page.click("#menubtn")
        self.assertTrue(page.locator("#sidebar.open").is_visible())
        self.assertTrue(page.locator("#sidebar .foot").is_visible())
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        self.assertFalse(
            page.locator("#sidebar").evaluate(
                "e => e.classList.contains('open')"))
        page.screenshot(path="/tmp/partial-final-mobile-memory.png")
        ctx.close()

        # overlay is usable at 375px
        ctx, page = self.page(375)
        page.goto(self.demo.base + "/app#/memory?q=pagination")
        page.wait_for_selector("table.list")
        page.locator('#view table a[href="#/memory"]').first.click()
        page.wait_for_selector(".doc-overlay")
        self.assertTrue(no_x_scroll(page), "overflow on overlay")
        page.keyboard.press("Escape")
        page.wait_for_selector(".doc-overlay", state="detached")
        ctx.close()

    def test_99_no_console_errors(self):
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
