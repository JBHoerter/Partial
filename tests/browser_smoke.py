import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

from partial.demo import create_demo_store
from partial.git import create_checkpoint
from partial.models import Event, scoped_session_id
from partial.server import create_server
from partial.store import Store

TOKEN = "smoke-" + "t" * 40
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
    evil = ("<img src=x onerror=window.__pwned=1>"
            " marker-dangerous-text")
    store.ingest(
        rid,
        [_ev("evil1", "evil-s", evil)],
        worktree=str(root))
    return store, rid, cp["id"]


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
        cls.auth_store, cls.rid, cls.cpid = build_auth_store(
            Path(cls.tmp.name) / "auth.db")
        cls.demo_store = create_demo_store()
        cls.auth = Server(cls.auth_store)
        cls.demo = Server(cls.demo_store, demo=True)
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

    def login(self, page, base):
        page.goto(base + "/app")
        page.wait_for_selector("#login:not([hidden])")
        page.fill("#login-token", TOKEN)
        page.click("#login-form button[type=submit]")
        page.wait_for_selector("#shell:not([hidden])")

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
        page.fill("#login-token", "definitely-wrong")
        page.click("#login-form button[type=submit]")
        page.wait_for_selector("#login-error:not(:empty)")
        self.assertFalse(page.locator("#shell").is_visible())
        page.fill("#login-token", TOKEN)
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
        page.fill("input[aria-label='Reviewer name']", "Smoke Tester")
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

    def test_99_no_console_errors(self):
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
