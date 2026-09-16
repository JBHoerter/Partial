from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .handoff import format_handoff
from .privacy import redact
from .store import Store

MAX_BODY = 16 * 1024 * 1024
SESSION_TTL = 12 * 3600
LOGIN_LIMIT = 10
LOGIN_WINDOW = 60
COOKIE_NAME = "partial_session"
STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app": ("app.html", "text/html; charset=utf-8"),
    "/app/": ("app.html", "text/html; charset=utf-8"),
    "/app.html": ("app.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/static/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_SAFE_SESSION_KEYS = (
    "id", "repo_id", "native_id", "agent", "title", "branch",
    "parent_session_id", "model", "status", "started_at", "updated_at",
)
_ID64_RE = re.compile(r"[0-9a-f]{64}")
_ID32_RE = re.compile(r"[0-9a-f]{32}")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self';"
       " img-src 'self' data:; frame-ancestors 'none';"
       " base-uri 'none'; form-action 'self'")


class ApiError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_loopback(host: str) -> bool:
    if host.lower() in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _safe_session(s: dict) -> dict:
    return {k: s.get(k) for k in _SAFE_SESSION_KEYS}


def _validate_public_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("public URL must be an http(s) URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError(
            "public URL must not contain userinfo, query, or fragment")
    if parts.path not in ("", "/"):
        raise ValueError("public URL must not contain a path")
    return url.rstrip("/")


class _Context:
    def __init__(self, store: Store, host: str, port: int,
                 token: str, public_url: str | None, demo: bool):
        self.store = store
        self.demo = demo
        self.token = token
        self.sessions: dict[str, float] = {}
        self.login_hits: dict[str, list[float]] = {}
        self.allowed_hosts: set[str] = set()
        self.allowed_origins: set[str] = set()
        self.secure_cookie = False
        self.lock = threading.Lock()
        if _is_loopback(host):
            for h in ("127.0.0.1", "localhost", "[::1]"):
                self.allowed_hosts.add(f"{h}:{port}")
                self.allowed_origins.add(f"http://{h}:{port}")
        if public_url:
            parts = urlsplit(public_url)
            self.allowed_hosts.add(parts.netloc.lower())
            self.allowed_origins.add(
                f"{parts.scheme}://{parts.netloc}".lower())
            self.secure_cookie = parts.scheme == "https"
        if not _is_loopback(host) and not public_url:
            self.allowed_hosts.add(f"{host}:{port}")

    def check_login_rate(self, ip: str) -> bool:
        now = time.monotonic()
        with self.lock:
            self.login_hits = {
                k: [t for t in v if now - t < LOGIN_WINDOW]
                for k, v in self.login_hits.items()
                if any(now - t < LOGIN_WINDOW for t in v)}
            if ip not in self.login_hits \
                    and len(self.login_hits) >= 4096:
                return False
            hits = self.login_hits.setdefault(ip, [])
            if len(hits) >= LOGIN_LIMIT:
                return False
            hits.append(now)
            return True

    def new_session(self) -> str:
        now = time.monotonic()
        with self.lock:
            self.sessions = {
                k: v for k, v in self.sessions.items() if v > now}
            if len(self.sessions) >= 4096:
                raise ApiError(429, "workspace session limit reached")
            tok = secrets.token_urlsafe(32)
            self.sessions[tok] = now + SESSION_TTL
            return tok

    def session_valid(self, tok: str) -> bool:
        with self.lock:
            exp = self.sessions.get(tok)
            if exp is None:
                return False
            if exp < time.monotonic():
                del self.sessions[tok]
                return False
            return True

    def revoke_session(self, tok: str) -> None:
        with self.lock:
            self.sessions.pop(tok, None)


def _safe_event(e: dict) -> dict:
    return {
        "id": e.get("id"), "session_id": e.get("session_id"),
        "kind": e.get("kind"), "timestamp": e.get("timestamp"),
        "text": e.get("text"), "tool_name": e.get("tool_name"),
        "data": e.get("data") or {},
    }


def _safe_checkpoint(c: dict, *, full: bool) -> dict:
    out = {
        "id": c.get("id"), "repo_id": c.get("repo_id"),
        "commit_sha": c.get("commit_sha"), "branch": c.get("branch"),
        "message": c.get("message"), "author": c.get("author"),
        "created_at": c.get("created_at"),
        "session_ids": c.get("session_ids") or [],
    }
    if full:
        out["files"] = c.get("files") or []
        out["diff"] = c.get("diff")
        out["links"] = c.get("links") or []
    return out


def _page(args: dict, default: int, max_limit: int) -> tuple[int, int]:
    try:
        limit = int(args.get("limit", [default])[0])
        offset = int(args.get("offset", [0])[0])
    except (TypeError, ValueError):
        raise ApiError(400, "invalid limit/offset")
    if limit < 1 or offset < 0 or offset > 100000:
        raise ApiError(400, "invalid limit/offset")
    return min(limit, max_limit), offset


class _CtxRef:
    def __init__(self):
        self.ctx = None

    def __getattr__(self, key):
        return getattr(self.ctx, key)


def _make_handler(ctx):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"partial/{__version__}"
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.request.settimeout(10)

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body=b"", ctype="application/json",
                headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def _json(self, code, obj, headers=None):
            h = {"Cache-Control": "no-store"}
            h.update(headers or {})
            self._send(code, json.dumps(obj).encode(), headers=h)

        def _error(self, code, msg):
            self.close_connection = True
            self._json(code, {"error": str(redact(msg))[:400]})

        def _check_host(self):
            hosts = self.headers.get_all("Host") or []
            if len(hosts) != 1:
                raise ApiError(400, "exactly one Host header required")
            host = hosts[0].lower()
            if host not in ctx.allowed_hosts:
                raise ApiError(403, "host not allowed")

        def _check_framing(self):
            if self.headers.get("Transfer-Encoding"):
                raise ApiError(400, "transfer-encoding not supported")
            lengths = self.headers.get_all("Content-Length") or []
            if len(set(lengths)) > 1:
                raise ApiError(
                    400, "conflicting Content-Length headers")

        def _check_origin(self):
            origin = self.headers.get("Origin")
            if origin is not None:
                if origin.lower() not in ctx.allowed_origins:
                    raise ApiError(403, "origin not allowed")
                return True
            return False

        def _bearer_ok(self) -> bool:
            auth = self.headers.get("Authorization") or ""
            if not auth.startswith("Bearer "):
                return False
            return secrets.compare_digest(
                auth[7:].encode(), ctx.token.encode())

        def _cookie_token(self) -> str | None:
            raw = self.headers.get("Cookie")
            if not raw:
                return None
            try:
                jar = cookies.SimpleCookie(raw)
            except cookies.CookieError:
                return None
            morsel = jar.get(COOKIE_NAME)
            return morsel.value if morsel else None

        def _authed(self) -> bool:
            if ctx.demo:
                return True
            if self._bearer_ok():
                return True
            tok = self._cookie_token()
            return bool(tok and ctx.session_valid(tok))

        def _read_body(self) -> bytes:
            lengths = self.headers.get_all("Content-Length") or []
            raw = lengths[0] if lengths else None
            if raw is None:
                return b""
            if not raw.strip().isdigit():
                raise ApiError(400, "invalid Content-Length")
            n = int(raw)
            if n > MAX_BODY:
                raise ApiError(413, "request body too large")
            return self.rfile.read(n)

        def _read_json(self, body: bytes):
            ctype = (self.headers.get("Content-Type") or "")
            if ctype.split(";")[0].strip().lower() != "application/json":
                raise ApiError(400, "expected application/json")
            try:
                text = body.decode("utf-8") if body else ""
            except UnicodeDecodeError:
                raise ApiError(400, "body must be valid UTF-8")
            try:
                obj = json.loads(text) if text.strip() else None
            except json.JSONDecodeError:
                raise ApiError(400, "invalid JSON body")
            return obj

        def _guard_write(self, browser_origin):
            if ctx.demo:
                raise ApiError(403, "demo workspace is read-only")
            if not browser_origin and not self._bearer_ok():
                raise ApiError(401, "origin required")

        def _dispatch(self, method):
            try:
                self._check_framing()
                self._check_host()
                url = urlsplit(self.path)
                path = url.path
                qs = parse_qs(url.query)
                if not path.startswith("/api/"):
                    if method in ("GET", "HEAD"):
                        return self._serve_static(path)
                    raise ApiError(404, "not found")
                browser_origin = self._check_origin()
                if method == "POST":
                    self._guard_write(browser_origin)
                    body = self._read_body()
                    return self._post(path, body)
                if method not in ("GET", "HEAD"):
                    raise ApiError(404, "not found")
                if path == "/api/health":
                    return self._json(200, {"ok": True,
                                            "version": __version__})
                if not self._authed():
                    raise ApiError(401, "authentication required")
                return self._get(path, qs)
            except ApiError as exc:
                self._error(exc.code, exc.message)
            except (ValueError, KeyError) as exc:
                code = 404 if isinstance(exc, KeyError) else 400
                self._error(code, str(exc))
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            except Exception:
                self._error(500, "internal server error")

        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("HEAD")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def _serve_static(self, path):
            entry = _STATIC_FILES.get(path)
            if entry is None:
                raise ApiError(404, "not found")
            name, ctype = entry
            body = (STATIC_DIR / name).read_bytes()
            self._send(200, body, ctype=ctype)

        def _get(self, path, qs):
            if path == "/api/me":
                return self._json(200, {
                    "authenticated": True, "demo": ctx.demo,
                    "version": __version__})
            if path == "/api/overview":
                s = ctx.store.stats()
                return self._json(200, {
                    "repositories": s["repositories"],
                    "sessions": s["sessions"],
                    "checkpoints": s["checkpoints"]})
            if path == "/api/integrations":
                return self._json(200, _integrations())
            if path == "/api/repos":
                items = [{
                    "id": r["id"], "name": r["name"],
                    "remote": r["remote"],
                    "created_at": r["created_at"],
                } for r in ctx.store.list_repos()]
                return self._json(200, {"items": items})
            if path == "/api/sessions":
                limit, offset = _page(qs, 50, 100)
                rows = ctx.store.list_sessions(
                    repo_id=_qs(qs, "repo"), agent=_qs(qs, "agent"),
                    q=_qs(qs, "q"), branch=_qs(qs, "branch"),
                    limit=limit + 1, offset=offset)
                items = [_safe_session(s) for s in rows[:limit]]
                return self._json(200, {
                    "items": items, "has_more": len(rows) > limit})
            if path == "/api/checkpoints":
                limit, offset = _page(qs, 50, 100)
                rows = ctx.store.list_checkpoints(
                    repo_id=_qs(qs, "repo"), branch=_qs(qs, "branch"),
                    limit=limit + 1, offset=offset)
                items = [_safe_checkpoint(c, full=False)
                         for c in rows[:limit]]
                return self._json(200, {
                    "items": items, "has_more": len(rows) > limit})
            if path == "/api/search":
                q = _qs(qs, "q") or ""
                limit, offset = _page(qs, 50, 100)
                rows = ctx.store.search_events(
                    q, repo_id=_qs(qs, "repo"), agent=_qs(qs, "agent"),
                    limit=limit + 1, offset=offset)
                items = []
                for r in rows[:limit]:
                    if not r.get("text"):
                        r["text"] = "Tool activity matched"
                    items.append(r)
                return self._json(200, {
                    "items": items, "has_more": len(rows) > limit})
            if path == "/api/export":
                repo = _qs(qs, "repo")
                bundle = ctx.store.export_bundle(repo_id=repo)
                if repo and not bundle["repositories"]:
                    raise ApiError(404, "repository not found")
                return self._json(
                    200, bundle,
                    headers={"Content-Disposition": "attachment;"
                             ' filename="partial-export.json"'})
            if path == "/api/bundles":
                raise ApiError(404, "not found")
            m = _match(path, "/api/repos/")
            if m:
                if not _ID64_RE.fullmatch(m):
                    raise ApiError(404, "repository not found")
                repo = ctx.store.get_repo(m)
                if repo is None:
                    raise ApiError(404, "repository not found")
                return self._json(200, {
                    "repository": {
                        "id": repo["id"], "name": repo["name"],
                        "remote": repo["remote"],
                        "created_at": repo["created_at"],
                    },
                    "branches": ctx.store.repo_branches(m)})
            m = _match(path, "/api/sessions/", suffix="/handoff")
            if m:
                if not _ID64_RE.fullmatch(m):
                    raise ApiError(404, "session not found")
                sess = ctx.store.get_session(m)
                if sess is None:
                    raise ApiError(404, "session not found")
                for e in sess["events"]:
                    e["data"] = ctx.store.strip_paths(
                        e.get("data") or {}, sess["repo_id"])
                body = format_handoff(sess).encode()
                return self._send(
                    200, body,
                    ctype="text/markdown; charset=utf-8",
                    headers={
                        "Cache-Control": "no-store",
                        "Content-Disposition": "attachment;"
                        ' filename="partial-handoff.md"',
                    })
            m = _match(path, "/api/sessions/")
            if m:
                return self._session_detail(m, qs)
            m = _match(path, "/api/checkpoints/", suffix="/reviews")
            if m:
                if not _ID32_RE.fullmatch(m) or \
                        ctx.store.get_checkpoint(m) is None:
                    raise ApiError(404, "checkpoint not found")
                return self._json(200, {
                    "items": ctx.store.list_reviews(m)})
            m = _match(path, "/api/checkpoints/")
            if m:
                if not _ID32_RE.fullmatch(m):
                    raise ApiError(404, "checkpoint not found")
                cp = ctx.store.get_checkpoint(m)
                if cp is None:
                    raise ApiError(404, "checkpoint not found")
                sessions = [_safe_session(s) for s in
                            ctx.store.sessions_for_checkpoint(m)]
                return self._json(200, {
                    "checkpoint": _safe_checkpoint(cp, full=True),
                    "sessions": sessions})
            raise ApiError(404, "not found")

        def _session_detail(self, sid, qs):
            if not _ID64_RE.fullmatch(sid):
                raise ApiError(404, "session not found")
            row = ctx.store.get_session_meta(sid)
            if row is None:
                raise ApiError(404, "session not found")
            limit, offset = _page(qs, 200, 500)
            rows = ctx.store.session_events_page(
                row["id"], limit=limit + 1, offset=offset,
                kind=_qs(qs, "kind"))
            events = []
            for e in rows[:limit]:
                e["data"] = ctx.store.strip_paths(
                    e.get("data") or {}, row["repo_id"])
                events.append(_safe_event(e))
            children = [_safe_session(s)
                        for s in ctx.store.child_sessions(row["id"])]
            return self._json(200, {
                "session": _safe_session(row),
                "events": events,
                "has_more": len(rows) > limit,
                "checkpoints": ctx.store.checkpoints_for_session(
                    row["id"]),
                "children": children})

        def _post(self, path, body):
            if path == "/api/login":
                return self._login(body)
            if path == "/api/logout":
                self._read_json(body)
                return self._logout()
            if not self._authed():
                raise ApiError(401, "authentication required")
            if path == "/api/bundles":
                obj = self._read_json(body)
                if not isinstance(obj, dict):
                    raise ApiError(400, "expected a bundle object")
                result = ctx.store.import_bundle(obj)
                return self._json(200, {"ok": True, "counts": result})
            m = _match(path, "/api/checkpoints/", suffix="/reviews")
            if m:
                if not _ID32_RE.fullmatch(m) or \
                        ctx.store.get_checkpoint(m) is None:
                    raise ApiError(404, "checkpoint not found")
                obj = self._read_json(body)
                if not isinstance(obj, dict):
                    raise ApiError(400, "expected an object")
                author = obj.get("author")
                body = obj.get("body")
                if not isinstance(author, str) or not isinstance(
                        body, str):
                    raise ApiError(400, "author and body required")
                if not author.strip() or len(author.strip()) > 80 \
                        or not body.strip() \
                        or len(body.strip()) > 10000:
                    raise ApiError(
                        400, "author 1..80, body 1..10000 chars")
                try:
                    review = ctx.store.add_review(
                        m, author, body)
                except KeyError:
                    raise ApiError(404, "checkpoint not found")
                return self._json(201, {"item": review})
            raise ApiError(404, "not found")

        def _login(self, body):
            ip = self.client_address[0]
            if not ctx.check_login_rate(ip):
                raise ApiError(429, "too many login attempts")
            obj = self._read_json(body)
            token = obj.get("token") if isinstance(obj, dict) else None
            if not isinstance(token, str) or not secrets.compare_digest(
                    token.encode(), ctx.token.encode()):
                raise ApiError(401, "invalid token")
            sess = ctx.new_session()
            cookie = (f"{COOKIE_NAME}={sess}; HttpOnly;"
                      " SameSite=Strict; Path=/;"
                      f" Max-Age={SESSION_TTL}")
            if ctx.secure_cookie:
                cookie += "; Secure"
            self._json(200, {"ok": True},
                       headers={"Set-Cookie": cookie})

        def _logout(self):
            tok = self._cookie_token()
            if tok:
                ctx.revoke_session(tok)
            cookie = (f"{COOKIE_NAME}=; HttpOnly; SameSite=Strict;"
                      " Path=/; Max-Age=0")
            if ctx.secure_cookie:
                cookie += "; Secure"
            self._json(200, {"ok": True},
                       headers={"Set-Cookie": cookie})

    return Handler


def _match(path: str, prefix: str, suffix: str = "") -> str | None:
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    if suffix:
        if not rest.endswith(suffix):
            return None
        rest = rest[: -len(suffix)]
    if "/" in rest or not rest:
        return None
    return rest


def _qs(qs: dict, key: str) -> str | None:
    v = qs.get(key)
    return v[0] if v else None


def _integrations() -> dict:
    return {
        "items": [
            {
                "agent": "devin",
                "name": "Devin",
                "capture": "native hooks (.devin/hooks.v1.json)",
                "setup": [
                    "python3 -m pip install .",
                    "partial enable --agent devin",
                ],
                "note": "Hooks are installed in the repository; run"
                        " Devin as usual after enabling.",
            },
            {
                "agent": "claude",
                "name": "Claude Code",
                "capture": "hook wrapper via generated settings file",
                "setup": [
                    "partial enable --agent claude",
                    "partial run claude",
                ],
                "note": "partial run claude invokes claude with the"
                        " generated hook settings.",
            },
            {
                "agent": "codex",
                "name": "Codex",
                "capture": "codex exec --json stream",
                "setup": [
                    "partial enable --agent codex",
                    "partial run codex 'your task'",
                ],
                "note": "Events are ingested from the exec JSON"
                        " stream; transcript files can also be imported"
                        " via partial import --agent codex.",
            },
            {
                "agent": "chatgpt",
                "name": "ChatGPT",
                "capture": "conversation export file import",
                "setup": [
                    "partial import --agent chatgpt <export.json>",
                ],
                "note": "ChatGPT is import-only; export a conversation"
                        " and import the JSON file.",
            },
        ],
    }


def create_server(
    store: Store,
    *,
    host: str = "127.0.0.1",
    port: int = 4310,
    token: str,
    public_url: str | None = None,
    demo: bool = False,
) -> ThreadingHTTPServer:
    if public_url:
        public_url = _validate_public_url(public_url)
    if demo and not _is_loopback(host):
        raise ValueError("demo mode only binds loopback addresses")
    if not _is_loopback(host):
        if not public_url or not public_url.startswith("https://"):
            raise ValueError(
                "binding a non-loopback address requires"
                " --public-url https://…")
        env_token = os.environ.get("PARTIAL_TOKEN")
        if not env_token or len(env_token) < 32:
            raise ValueError(
                "binding a non-loopback address requires an explicit"
                " PARTIAL_TOKEN (>=32 chars)")
    if not demo and len(token) < 32:
        raise ValueError("server token must be at least 32 characters")
    ref = _CtxRef()
    server = ThreadingHTTPServer(
        (host, port), _make_handler(ref))
    server.daemon_threads = True
    ctx = _Context(
        store, host, server.server_address[1], token,
        public_url, demo)
    ref.ctx = ctx
    server.partial_context = ctx
    return server


def serve(
    store: Store,
    *,
    host: str = "127.0.0.1",
    port: int = 4310,
    public_url: str | None = None,
    demo: bool = False,
) -> None:
    from .auth import get_token
    if demo:
        token = "demo-mode-no-auth-required!!"
    else:
        token = get_token(store.path.parent)
    server = create_server(
        store, host=host, port=port, token=token,
        public_url=public_url, demo=demo)
    actual = server.server_address[1]
    shown = public_url or f"http://{host}:{actual}"
    print(f"Partial workspace: {shown}")
    if demo:
        print("Demo workspace: sample data only (read-only).")
    else:
        print("Run `partial auth token` to obtain your workspace"
              " access token.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
