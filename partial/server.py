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
from .accounts import Accounts, AccountsError, Principal
from .handoff import format_handoff
from .models import now_iso, sha256_hex
from .privacy import redact
from .store import RUN_KINDS, Store

MAX_BODY = 16 * 1024 * 1024
SESSION_TTL = 12 * 3600
LOGIN_LIMIT = 10
LOGIN_WINDOW = 60
COOKIE_NAME = "partial_session"
WS_HEADER = "X-Partial-Workspace"
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
        self.accounts = None if demo else Accounts(
            store.path.parent, store.path)
        self.login_hits: dict[str, list[float]] = {}
        self.job_sems: dict[str, threading.Semaphore] = {}
        self.cleaned_stores: set[str] = set()
        self.active_runs: set[str] = set()
        # Bounded history of run ids this process created, so stale-run
        # recovery can tell "our worker is gone" from "a live foreign
        # process owns this row" without a second persisted marker.
        self.known_runs: set[str] = set()
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

    def job_sem(self, ws_id: str) -> threading.Semaphore:
        with self.lock:
            if len(self.job_sems) >= 1024 \
                    and ws_id not in self.job_sems:
                raise ApiError(429, "too many workspaces")
            return self.job_sems.setdefault(
                ws_id, threading.Semaphore(4))

    def track_run(self, run_id: str) -> None:
        with self.lock:
            if len(self.active_runs) >= 4096 \
                    and run_id not in self.active_runs:
                raise ApiError(429, "too many workflow runs")
            self.active_runs.add(run_id)
            if len(self.known_runs) < 65536:
                self.known_runs.add(run_id)

    def untrack_run(self, run_id: str) -> None:
        with self.lock:
            self.active_runs.discard(run_id)

    def job_cleanup(self, store: Store) -> None:
        key = str(store.path)
        with self.lock:
            if key in self.cleaned_stores:
                return
            self.cleaned_stores.add(key)
        try:
            self._recover_runs(store)
        except Exception:
            pass

    def _recover_runs(self, store: Store) -> None:
        # Safe local-process recovery.  The store's canonical
        # owner-marker check interrupts a 'running' row only when its
        # recorded owner pid is provably dead (or the row predates
        # owner tracking); a row owned by a live foreign process is
        # never touched.  Afterwards, rows this process started but
        # no longer tracks are interrupted here: their worker is gone
        # even though the process itself is alive.
        try:
            store.interrupt_running_runs()
        except Exception:
            pass
        for run in store.list_runs(limit=10000):
            if run.get("status") != "running":
                continue
            det = run.get("details")
            if not isinstance(det, dict):
                det = {}
            rid = run.get("id")
            with self.lock:
                recover = rid in self.known_runs \
                    and rid not in self.active_runs
            if not recover:
                continue
            report = dict(run.get("report") or {})
            report["error"] = (
                "interrupted: the worker process exited before this"
                " run finished")
            try:
                store.save_run(
                    rid, run["kind"], run.get("repo_id"),
                    "interrupted", run.get("source_ids") or [],
                    report, det)
            except Exception:
                pass


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


def _safe_run(run: dict) -> dict:
    out = dict(run)
    det = out.get("details")
    if isinstance(det, dict):
        out["details"] = {
            k: v for k, v in det.items()
            if k not in ("run_owner", "runner", "pid")}
    return out


def _require_external_ai(store: Store) -> None:
    if store.memory_setting("external_ai_enabled") != "true":
        raise ApiError(
            403, "external AI disabled; an owner must enable it"
                 " under Memory settings")
    from .ai import OpenAIProvider
    if not OpenAIProvider().configured:
        raise ApiError(400, "AI provider is not configured")


def _page(args: dict, default: int, max_limit: int) -> tuple[int, int]:
    try:
        limit = int(args.get("limit", [default])[0])
        offset = int(args.get("offset", [0])[0])
    except (TypeError, ValueError):
        raise ApiError(400, "invalid limit/offset")
    if limit < 1 or offset < 0 or offset > 100000:
        raise ApiError(400, "invalid limit/offset")
    return min(limit, max_limit), offset


def _obj(body_json) -> dict:
    if not isinstance(body_json, dict):
        raise ApiError(400, "expected a JSON object")
    return body_json


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

        def _bearer_raw(self) -> str | None:
            auth = self.headers.get("Authorization") or ""
            if not auth.startswith("Bearer "):
                return None
            return auth[7:]

        def _cookie_raw(self) -> str | None:
            raw = self.headers.get("Cookie")
            if not raw:
                return None
            try:
                jar = cookies.SimpleCookie(raw)
            except cookies.CookieError:
                return None
            morsel = jar.get(COOKIE_NAME)
            return morsel.value if morsel else None

        def _principal(self) -> Principal | None:
            if ctx.demo:
                return Principal(
                    user_id="demo", email="demo@localhost",
                    name="Demo", workspace_id="demo",
                    token_role="owner")
            raw = self._bearer_raw()
            if raw is not None:
                return ctx.accounts.authenticate(raw, api_token=True)
            ck = self._cookie_raw()
            if ck:
                return ctx.accounts.authenticate(ck)
            return None

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

        def _resolve_ws(self, principal: Principal, *,
                        write=False, admin=False, owner=False):
            if ctx.demo:
                if write or admin or owner:
                    raise ApiError(403, "demo workspace is read-only")
                ctx.job_cleanup(ctx.store)
                return ctx.store, {
                    "id": "demo", "name": "Demo workspace",
                    "role": "owner"}
            ws_id = self.headers.get(WS_HEADER)
            try:
                store, ws = ctx.accounts.workspace_store(
                    principal, ws_id, write=write, admin=admin,
                    owner=owner)
            except AccountsError as exc:
                raise ApiError(exc.code, str(exc))
            ctx.job_cleanup(store)
            return store, ws

        def _guard_write(self, browser_origin, principal):
            if not browser_origin and principal.token_id is None:
                raise ApiError(401, "origin required")

        def _cookie(self, raw: str, max_age: int) -> str:
            c = (f"{COOKIE_NAME}={raw}; HttpOnly; SameSite=Strict;"
                 f" Path=/; Max-Age={max_age}")
            if ctx.secure_cookie:
                c += "; Secure"
            return c

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
                if method in ("POST", "PATCH", "DELETE"):
                    body = self._read_body()
                    return self._write(
                        method, path, body, browser_origin)
                if method not in ("GET", "HEAD"):
                    raise ApiError(404, "not found")
                if path == "/api/health":
                    return self._json(200, {"ok": True,
                                            "version": __version__})
                if path == "/api/auth/status":
                    return self._json(200, {
                        "initialized": (
                            ctx.demo
                            or ctx.accounts.initialized()),
                        "registration": "invite",
                        "demo": ctx.demo})
                principal = self._principal()
                if principal is None:
                    raise ApiError(401, "authentication required")
                return self._get(path, qs, principal)
            except ApiError as exc:
                self._error(exc.code, exc.message)
            except AccountsError as exc:
                self._error(exc.code, str(exc))
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

        def do_PATCH(self):
            self._dispatch("PATCH")

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

        def _get(self, path, qs, principal: Principal):
            if path == "/api/me":
                return self._me(principal)
            if path == "/api/workspaces" or path.startswith(
                    "/api/workspaces/"):
                if ctx.demo:
                    raise ApiError(
                        403, "demo workspace is read-only")
                if principal.token_id is not None:
                    raise ApiError(
                        403, "API tokens cannot administer")
                if path == "/api/workspaces":
                    return self._json(200, {
                        "items": ctx.accounts.workspaces(
                            principal)})
                m = _match(path, "/api/workspaces/",
                           suffix="/members")
                if m:
                    return self._json(200, {
                        "items": ctx.accounts.members(principal, m)})
                m = _match(path, "/api/workspaces/", suffix="/audit")
                if m:
                    return self._json(200, {
                        "items": ctx.accounts.list_audit(
                            principal, m)})
                m = _match(path, "/api/workspaces/", suffix="/tokens")
                if m:
                    return self._json(200, {
                        "items": ctx.accounts.list_api_tokens(
                            principal, m)})
                raise ApiError(404, "not found")
            if path == "/api/integrations":
                self._resolve_ws(principal)
                return self._json(200, _integrations())
            store, _ws = self._resolve_ws(principal)
            if path.startswith("/api/memory/") \
                    or path.startswith("/api/graph/") \
                    or path == "/api/decisions" \
                    or path == "/api/dispatch" \
                    or path == "/api/projects" \
                    or path.startswith("/api/workflows") \
                    or path == "/api/experts":
                return self._memory_get(store, path, qs)
            if path == "/api/overview":
                s = store.stats()
                return self._json(200, {
                    "repositories": s["repositories"],
                    "sessions": s["sessions"],
                    "checkpoints": s["checkpoints"]})
            if path == "/api/repos":
                items = [{
                    "id": r["id"], "name": r["name"],
                    "remote": r["remote"], "created_at": r["created_at"],
                } for r in store.list_repos()]
                return self._json(200, {"items": items})
            if path == "/api/sessions":
                limit, offset = _page(qs, 50, 100)
                rows = store.list_sessions(
                    repo_id=_qs(qs, "repo"), agent=_qs(qs, "agent"),
                    q=_qs(qs, "q"), branch=_qs(qs, "branch"),
                    limit=limit + 1, offset=offset)
                items = [_safe_session(s) for s in rows[:limit]]
                return self._json(200, {
                    "items": items, "has_more": len(rows) > limit})
            if path == "/api/checkpoints":
                limit, offset = _page(qs, 50, 100)
                rows = store.list_checkpoints(
                    repo_id=_qs(qs, "repo"), branch=_qs(qs, "branch"),
                    limit=limit + 1, offset=offset)
                items = [_safe_checkpoint(c, full=False)
                         for c in rows[:limit]]
                return self._json(200, {
                    "items": items, "has_more": len(rows) > limit})
            if path == "/api/search":
                q = _qs(qs, "q") or ""
                limit, offset = _page(qs, 50, 100)
                rows = store.search_events(
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
                bundle = store.export_bundle(repo_id=repo)
                if repo and not bundle["repositories"]:
                    raise ApiError(404, "repository not found")
                return self._json(
                    200, bundle,
                    headers={"Content-Disposition": "attachment;"
                             ' filename="partial-export.json"'})
            m = _match(path, "/api/repos/")
            if m:
                if not _ID64_RE.fullmatch(m):
                    raise ApiError(404, "repository not found")
                repo = store.get_repo(m)
                if repo is None:
                    raise ApiError(404, "repository not found")
                return self._json(200, {
                    "repository": {
                        "id": repo["id"], "name": repo["name"],
                        "remote": repo["remote"],
                        "created_at": repo["created_at"],
                    },
                    "branches": store.repo_branches(m)})
            m = _match(path, "/api/sessions/", suffix="/usage")
            if m:
                if not _ID64_RE.fullmatch(m):
                    raise ApiError(404, "session not found")
                sess = store.get_session(m)
                if sess is None:
                    raise ApiError(404, "session not found")
                from .brain_contract import usage_totals
                return self._json(
                    200, {"usage": usage_totals(sess["events"])})
            m = _match(path, "/api/checkpoints/", suffix="/usage")
            if m:
                if not _ID32_RE.fullmatch(m):
                    raise ApiError(404, "checkpoint not found")
                cp = store.get_checkpoint(m)
                if cp is None:
                    raise ApiError(404, "checkpoint not found")
                from .brain_contract import usage_totals
                seen = set()
                items = []
                for link in cp["links"]:
                    sid = link["session_id"]
                    if sid in seen:
                        continue
                    seen.add(sid)
                    sess = store.get_session(sid)
                    if sess is not None:
                        items.append({"session": sid, **usage_totals(
                            sess["events"])})
                return self._json(200, {"sessions": items})
            m = _match(path, "/api/sessions/", suffix="/native")
            if m:
                if not _ID64_RE.fullmatch(m) or \
                        store.get_session_meta(m) is None:
                    raise ApiError(404, "session not found")
                row = store.get_native(m)
                if row is None:
                    return self._json(200, {"native": None})
                return self._json(200, {"native": {
                    "session_id": row["session_id"],
                    "agent": row["agent"],
                    "native_id": row["native_id"],
                    "format": row["format"],
                    "registered_at": row["registered_at"],
                    "source": row["source"],
                    "has_local_path": bool(row.get("local_path")),
                    "has_archive": bool(row.get("archive")),
                }})
            m = _match(path, "/api/sessions/", suffix="/handoff")
            if m:
                if not _ID64_RE.fullmatch(m):
                    raise ApiError(404, "session not found")
                sess = store.get_session(m)
                if sess is None:
                    raise ApiError(404, "session not found")
                for e in sess["events"]:
                    e["data"] = store.strip_paths(
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
                return self._session_detail(store, m, qs)
            m = _match(path, "/api/checkpoints/", suffix="/reviews")
            if m:
                if not _ID32_RE.fullmatch(m) or \
                        store.get_checkpoint(m) is None:
                    raise ApiError(404, "checkpoint not found")
                return self._json(200, {
                    "items": store.list_reviews(m)})
            m = _match(path, "/api/checkpoints/", suffix="/attribution")
            if m:
                if not _ID32_RE.fullmatch(m) or \
                        store.get_checkpoint(m) is None:
                    raise ApiError(404, "checkpoint not found")
                return self._json(200, {
                    "attribution": store.get_attribution(m)})
            m = _match(path, "/api/checkpoints/")
            if m:
                if not _ID32_RE.fullmatch(m):
                    raise ApiError(404, "checkpoint not found")
                cp = store.get_checkpoint(m)
                if cp is None:
                    raise ApiError(404, "checkpoint not found")
                sessions = [_safe_session(s) for s in
                            store.sessions_for_checkpoint(m)]
                return self._json(200, {
                    "checkpoint": _safe_checkpoint(cp, full=True),
                    "sessions": sessions,
                    "attribution": store.get_attribution(m)})
            raise ApiError(404, "not found")

        def _session_detail(self, store, sid, qs):
            if not _ID64_RE.fullmatch(sid):
                raise ApiError(404, "session not found")
            row = store.get_session_meta(sid)
            if row is None:
                raise ApiError(404, "session not found")
            limit, offset = _page(qs, 200, 500)
            rows = store.session_events_page(
                row["id"], limit=limit + 1, offset=offset,
                kind=_qs(qs, "kind"))
            events = []
            for e in rows[:limit]:
                e["data"] = store.strip_paths(
                    e.get("data") or {}, row["repo_id"])
                events.append(_safe_event(e))
            children = [_safe_session(s)
                        for s in store.child_sessions(row["id"])]
            return self._json(200, {
                "session": _safe_session(row),
                "events": events,
                "has_more": len(rows) > limit,
                "checkpoints": store.checkpoints_for_session(
                    row["id"]),
                "children": children})

        def _memory_get(self, store, path, qs):
            from .memory import Memory
            mem = Memory(store)
            if path == "/api/memory/status":
                conn = store._connect()
                try:
                    docs = conn.execute(
                        "SELECT COUNT(*) c FROM memory_documents"
                    ).fetchone()["c"]
                    idx = [dict(r) for r in conn.execute(
                        "SELECT repo_id,commit_sha,indexed_at"
                        " FROM repository_indexes").fetchall()]
                finally:
                    conn.close()
                from .ai import OpenAIProvider
                provider = OpenAIProvider()
                return self._json(200, {
                    "fts5": getattr(store, "fts_ok", False),
                    "documents": docs,
                    "indexed_repositories": idx,
                    "provider_configured": provider.configured,
                    "external_ai_enabled": store.memory_setting(
                        "external_ai_enabled") == "true",
                    "graph": mem.graph_capabilities(),
                })
            if path == "/api/memory/search":
                raw_limit = _qs(qs, "limit")
                try:
                    limit = int(raw_limit) \
                        if raw_limit is not None else 12
                except (TypeError, ValueError):
                    raise ApiError(400, "invalid limit")
                if not 1 <= limit <= 100:
                    raise ApiError(400, "limit must be 1..100")
                rows = mem.search(
                    _qs(qs, "q") or "", repo_id=_qs(qs, "repo"),
                    kind=_qs(qs, "kind"), limit=limit)
                return self._json(200, {"items": rows,
                                        "mode": "lexical"})
            if path == "/api/memory/context":
                out = mem.context(_qs(qs, "q") or "",
                                  repo_id=_qs(qs, "repo"))
                return self._json(200, out)
            m = _match(path, "/api/memory/documents/")
            if m:
                doc = mem.document(m)
                if doc is None:
                    raise ApiError(404, "document not found")
                return self._json(200, {"document": doc})
            if path == "/api/decisions":
                return self._json(200, {
                    "items": mem.decisions(_qs(qs, "repo"))})
            if path == "/api/graph/search":
                return self._json(200, {"items": mem.graph_search(
                    _qs(qs, "q") or "", repo_id=_qs(qs, "repo"))})
            if path == "/api/graph/neighbors":
                sid = _qs(qs, "id")
                if not sid:
                    raise ApiError(400, "id required")
                return self._json(200, mem.graph_neighbors(
                    sid, repo_id=_qs(qs, "repo")))
            if path == "/api/graph/impact":
                sid = _qs(qs, "id")
                if not sid:
                    raise ApiError(400, "id required")
                return self._json(200, mem.graph_impact(
                    sid, repo_id=_qs(qs, "repo")))
            if path == "/api/dispatch":
                return self._json(200, mem.dispatch(
                    repo_id=_qs(qs, "repo"),
                    branch=_qs(qs, "branch"),
                    since=_qs(qs, "since"), until=_qs(qs, "until")))
            if path == "/api/experts":
                repo = _qs(qs, "repo")
                scope = _qs(qs, "scope")
                if not repo or not scope:
                    raise ApiError(400, "repo and scope required")
                return self._json(200, {"items": mem.experts(
                    scope, repo_id=repo)})
            if path == "/api/workflows":
                return self._json(200, {"items": [_safe_run(r) for r in
                    store.list_runs(repo_id=_qs(qs, "repo"))]})
            m = _match(path, "/api/workflows/")
            if m:
                run = store.get_run(m)
                if run is None:
                    raise ApiError(404, "workflow run not found")
                return self._json(200, {"run": _safe_run(run)})
            if path == "/api/projects":
                return self._json(200, {"items": store.list_projects()})
            raise ApiError(404, "not found")

        def _memory_write(self, store, ws, principal, path, body):
            from .memory import Memory
            from .workflows import run_workflow
            mem = Memory(store)
            if path == "/api/memory/index":
                obj = _obj(self._read_json(body))
                repo_id = obj.get("repo_id")
                semantic = obj.get("semantic", False)
                if type(semantic) is not bool:
                    raise ApiError(
                        400, "semantic must be a boolean")
                if repo_id is not None:
                    if not _ID64_RE.fullmatch(str(repo_id)) \
                            or store.get_repo(repo_id) is None:
                        raise ApiError(404, "repository not found")
                if semantic:
                    _require_external_ai(store)
                    out = mem.index(
                        {"id": repo_id, "root": None}
                        if repo_id is not None else None,
                        semantic=True)
                else:
                    out = mem.index(
                        {"id": repo_id, "root": None}
                        if repo_id is not None else None)
                return self._json(200, out)
            if path == "/api/memory/settings":
                obj = _obj(self._read_json(body))
                enabled = obj.get("external_ai_enabled")
                if not isinstance(enabled, bool):
                    raise ApiError(
                        400, "external_ai_enabled must be a boolean")
                if enabled:
                    from .ai import OpenAIProvider
                    if not OpenAIProvider().configured:
                        raise ApiError(
                            400, "AI provider is not configured; set"
                                 " PARTIAL_OPENAI_API_KEY on the"
                                 " server first")
                store.set_memory_setting(
                    "external_ai_enabled", "true" if enabled else
                    "false")
                return self._json(200, {
                    "external_ai_enabled": enabled})
            if path == "/api/memory/search":
                obj = _obj(self._read_json(body))
                query = obj.get("query")
                if not isinstance(query, str) or not query.strip() \
                        or len(query) > 500:
                    raise ApiError(400, "query must be 1..500 chars")
                repo_id = obj.get("repo_id")
                if repo_id is not None and (
                        not isinstance(repo_id, str)
                        or not _ID64_RE.fullmatch(repo_id)):
                    raise ApiError(400, "invalid repo_id")
                if repo_id is not None \
                        and store.get_repo(repo_id) is None:
                    raise ApiError(404, "repository not found")
                kind = obj.get("kind")
                if kind is not None and kind not in (
                        "code", "session", "checkpoint", "decision"):
                    raise ApiError(400, "invalid kind")
                limit = obj.get("limit", 12)
                if type(limit) is not int or not 1 <= limit <= 100:
                    raise ApiError(400, "limit must be 1..100")
                semantic = obj.get("semantic", False)
                if type(semantic) is not bool:
                    raise ApiError(400, "semantic must be a boolean")
                if semantic:
                    _require_external_ai(store)
                try:
                    rows = mem.search(
                        query, repo_id=repo_id, kind=kind,
                        semantic=semantic, limit=limit)
                except ValueError as exc:
                    raise ApiError(400, str(exc))
                return self._json(200, {
                    "items": rows,
                    "mode": "hybrid" if semantic else "lexical"})
            if path == "/api/decisions":
                obj = _obj(self._read_json(body))
                repo_id = obj.get("repo_id")
                if not isinstance(repo_id, str) \
                        or not _ID64_RE.fullmatch(repo_id) \
                        or store.get_repo(repo_id) is None:
                    raise ApiError(404, "repository not found")
                sources = obj.get("source_ids") or []
                if not isinstance(sources, list):
                    raise ApiError(400, "source_ids must be a list")
                supersedes = obj.get("supersedes")
                if supersedes is not None \
                        and not isinstance(supersedes, str):
                    raise ApiError(400, "supersedes must be a string")
                d = mem.add_decision(
                    repo_id, obj.get("title"), obj.get("body"),
                    sources, author=principal.name,
                    supersedes=obj.get("supersedes"))
                return self._json(201, {"item": d})
            if path == "/api/projects":
                obj = _obj(self._read_json(body))
                p = store.create_project(obj.get("name"))
                return self._json(201, {"item": p})
            m = _match(path, "/api/projects/", suffix="/attach")
            if m:
                obj = _obj(self._read_json(body))
                repo_id = obj.get("repo_id")
                if not isinstance(repo_id, str) \
                        or not _ID64_RE.fullmatch(repo_id):
                    raise ApiError(400, "repo_id required")
                try:
                    p = store.attach_project_repo(m, repo_id)
                except KeyError:
                    raise ApiError(404, "project or repository"
                                    " not found")
                return self._json(200, {"item": p})
            if path == "/api/workflows":
                obj = _obj(self._read_json(body))
                kind = obj.get("kind")
                if kind not in RUN_KINDS:
                    raise ApiError(400, "invalid workflow kind")
                repo_id = obj.get("repo_id")
                if repo_id is not None and (
                        not isinstance(repo_id, str)
                        or not _ID64_RE.fullmatch(repo_id)
                        or store.get_repo(repo_id) is None):
                    raise ApiError(404, "repository not found")
                query = obj.get("query")
                if query is not None and (
                        not isinstance(query, str)
                        or len(query) > 500):
                    raise ApiError(
                        400, "query must be a string <=500 chars")
                run = obj.get("run", False)
                if type(run) is not bool:
                    raise ApiError(400, "run must be a boolean")
                semantic = obj.get("semantic", False)
                if type(semantic) is not bool:
                    raise ApiError(400, "semantic must be a boolean")
                since = obj.get("since")
                until = obj.get("until")
                branch = obj.get("branch")
                for name, val in (("since", since), ("until", until),
                                  ("branch", branch)):
                    if val is not None and (
                            not isinstance(val, str)
                            or len(val) > 200):
                        raise ApiError(
                            400, f"{name} must be a string <=200"
                                 " chars")
                agents = obj.get("agents")
                if agents:
                    raise ApiError(
                        400, "native agents cannot be run from the"
                             " API; use the CLI")
                if run:
                    _require_external_ai(store)
                    sem = ctx.job_sem(str(ws.get("id") or "demo"))
                    if not sem.acquire(blocking=False):
                        raise ApiError(
                            429, "too many running workflows")
                    rid = sha256_hex(
                        "run/v1\0" + str(kind) + "\0"
                        + str(query or "") + "\0" + now_iso())
                    try:
                        ctx.track_run(rid)
                        # save_run stamps the private run_owner marker
                        # itself; no caller-supplied ownership keys.
                        store.save_run(rid, kind, repo_id,
                                       "running", [], {}, {})
                    except Exception:
                        ctx.untrack_run(rid)
                        sem.release()
                        raise

                    def _job():
                        try:
                            run_workflow(
                                store, kind, repo_id=repo_id,
                                query=query or "", run=True,
                                run_id=rid, since=since,
                                until=until, branch=branch)
                        except BaseException as exc:
                            try:
                                store.save_run(
                                    rid, kind, repo_id, "error", [],
                                    {"error": str(redact(
                                        str(exc)))[:500]}, {})
                            except Exception:
                                pass
                        finally:
                            try:
                                cur = store.get_run(rid)
                                if cur is not None and cur.get(
                                        "status") == "running":
                                    rep = dict(
                                        cur.get("report") or {})
                                    rep["error"] = (
                                        "worker exited without a"
                                        " result")
                                    store.save_run(
                                        rid, kind, repo_id, "error",
                                        cur.get("source_ids") or [],
                                        rep,
                                        cur.get("details") or {})
                            except Exception:
                                pass
                            ctx.untrack_run(rid)
                            sem.release()
                    try:
                        threading.Thread(
                            target=_job, daemon=True).start()
                    except Exception:
                        ctx.untrack_run(rid)
                        sem.release()
                        try:
                            store.save_run(
                                rid, kind, repo_id, "error", [],
                                {"error": "worker could not be"
                                          " started"}, {})
                        except Exception:
                            pass
                        raise ApiError(
                            500, "could not start workflow run")
                    return self._json(202, {"id": rid,
                                            "status": "running"})
                out = run_workflow(
                    store, kind, repo_id=repo_id,
                    query=query or "", run=False,
                    since=since, until=until, branch=branch)
                return self._json(200, out)
            raise ApiError(404, "not found")

        def _principal_workspaces(self, principal: Principal):
            if principal.token_id is not None:
                conn = ctx.accounts._connect()
                try:
                    row = conn.execute(
                        "SELECT * FROM workspaces WHERE id=?",
                        (principal.workspace_id,)).fetchone()
                    if row is None:
                        return []
                    return [{"id": row["id"], "name": row["name"],
                             "created_at": row["created_at"],
                             "role": principal.token_role}]
                finally:
                    conn.close()
            return ctx.accounts.workspaces(principal)

        def _me(self, principal: Principal):
            if ctx.demo:
                return self._json(200, {
                    "authenticated": True, "demo": True,
                    "version": __version__,
                    "user": {"id": "demo", "email": "demo@localhost",
                             "name": "Demo"},
                    "workspaces": [{
                        "id": "demo", "name": "Demo workspace",
                        "role": "owner"}],
                    "workspace": {
                        "id": "demo", "name": "Demo workspace",
                        "role": "owner"}})
            wss = self._principal_workspaces(principal)
            if not wss:
                raise ApiError(404, "workspace not found")
            want = self.headers.get(WS_HEADER)
            ws = None
            if principal.token_id is not None:
                if want and want != principal.workspace_id:
                    raise ApiError(404, "workspace not found")
                ws = wss[0]
            elif want:
                ws = next(
                    (w for w in wss if w["id"] == want), None)
                if ws is None:
                    raise ApiError(404, "workspace not found")
            else:
                ws = wss[0]
            return self._json(200, {
                "authenticated": True, "demo": False,
                "version": __version__,
                "user": {"id": principal.user_id,
                         "email": principal.email,
                         "name": principal.name},
                "workspaces": wss, "workspace": ws})

        def _write(self, method, path, body, browser_origin):
            if ctx.demo:
                if body:
                    self._read_json(body)
                raise ApiError(403, "demo workspace is read-only")
            if method == "POST" and path in (
                    "/api/login", "/api/setup", "/api/invites/accept"):
                if not browser_origin:
                    raise ApiError(403, "origin required")
                if path == "/api/login":
                    return self._login(body)
                if path == "/api/setup":
                    return self._setup(body)
                return self._accept_invite(body)
            principal = self._principal()
            if principal is None:
                raise ApiError(401, "authentication required")
            self._guard_write(browser_origin, principal)
            if method == "POST" and path == "/api/logout":
                if body:
                    self._read_json(body)
                return self._logout()
            if method == "POST" and path == "/api/bundles":
                store, _ws = self._resolve_ws(principal, write=True)
                obj = _obj(self._read_json(body))
                result = store.import_bundle(obj)
                return self._json(200, {"ok": True, "counts": result})
            if method == "POST" and path == "/api/memory/settings":
                store, ws = self._resolve_ws(principal, owner=True)
                return self._memory_write(
                    store, ws, principal, path, body)
            if method == "POST" and (
                    path.startswith("/api/memory/")
                    or path in ("/api/decisions", "/api/workflows",
                                "/api/projects")
                    or path.startswith("/api/projects/")):
                store, ws = self._resolve_ws(principal, write=True)
                return self._memory_write(
                    store, ws, principal, path, body)
            if method == "POST" and path == "/api/account/password":
                if principal.token_id is not None:
                    raise ApiError(403, "password change requires"
                                    " browser sign-in")
                obj = _obj(self._read_json(body))
                ctx.accounts.change_password(
                    principal, obj.get("current_password"),
                    obj.get("new_password"))
                return self._json(200, {"ok": True}, headers={
                    "Set-Cookie": self._cookie("", 0)})
            if method == "POST" and path == "/api/workspaces":
                if principal.token_id is not None:
                    raise ApiError(403, "API tokens cannot create"
                                    " workspaces")
                obj = _obj(self._read_json(body))
                ws = ctx.accounts.create_workspace(
                    principal, obj.get("name"))
                return self._json(201, {"item": ws})
            m = _match(path, "/api/workspaces/", suffix="/invites")
            if m and method == "POST":
                if principal.token_id is not None:
                    raise ApiError(
                        403, "API tokens cannot create invites")
                obj = _obj(self._read_json(body))
                item = ctx.accounts.invite(
                    principal, m, obj.get("email"), obj.get("role"))
                return self._json(201, {"item": item})
            m = _match(path, "/api/workspaces/", suffix="/tokens")
            if m and method == "POST":
                if principal.token_id is not None:
                    raise ApiError(
                        403, "API tokens cannot create tokens")
                obj = _obj(self._read_json(body))
                item = ctx.accounts.create_api_token(
                    principal, m, obj.get("name"), obj.get("role"),
                    obj.get("expires_days", 90))
                return self._json(201, {"item": item})
            m = _match2(path, "/api/workspaces/", "/members/")
            if m and method == "PATCH":
                if principal.token_id is not None:
                    raise ApiError(
                        403, "API tokens cannot manage members")
                ws_id, uid = m
                obj = _obj(self._read_json(body))
                ctx.accounts.change_member(
                    principal, ws_id, uid, obj.get("role"))
                return self._json(200, {"ok": True})
            if m and method == "DELETE":
                if principal.token_id is not None:
                    raise ApiError(
                        403, "API tokens cannot manage members")
                ws_id, uid = m
                if body:
                    self._read_json(body)
                ctx.accounts.remove_member(principal, ws_id, uid)
                return self._json(200, {"ok": True})
            m = _match(path, "/api/tokens/")
            if m and method == "DELETE":
                if body:
                    self._read_json(body)
                ctx.accounts.revoke_api_token(principal, m)
                return self._json(200, {"ok": True})
            m = _match(path, "/api/checkpoints/", suffix="/reviews")
            if m and method == "POST":
                store, _ws = self._resolve_ws(principal, write=True)
                if not _ID32_RE.fullmatch(m) or \
                        store.get_checkpoint(m) is None:
                    raise ApiError(404, "checkpoint not found")
                obj = _obj(self._read_json(body))
                body_text = obj.get("body")
                if not isinstance(body_text, str) \
                        or not body_text.strip() \
                        or len(body_text.strip()) > 10000:
                    raise ApiError(400, "body 1..10000 chars")
                review = store.add_review(
                    m, principal.name, body_text,
                    actor=principal.user_id)
                return self._json(201, {"item": review})
            raise ApiError(404, "not found")

        def _login(self, body):
            ip = self.client_address[0]
            if not ctx.check_login_rate(ip):
                raise ApiError(429, "too many login attempts")
            obj = _obj(self._read_json(body))
            try:
                raw, principal = ctx.accounts.login(
                    obj.get("email"), obj.get("password"))
            except AccountsError as exc:
                raise ApiError(exc.code, str(exc))
            self._json(200, {"ok": True}, headers={
                "Set-Cookie": self._cookie(raw, SESSION_TTL)})

        def _setup(self, body):
            if ctx.demo:
                raise ApiError(403, "demo workspace is read-only")
            ip = self.client_address[0]
            if not ctx.check_login_rate(ip):
                raise ApiError(429, "too many requests")
            obj = _obj(self._read_json(body))
            bt = obj.get("bootstrap_token")
            if not isinstance(bt, str) or not secrets.compare_digest(
                    bt.encode(), ctx.token.encode()):
                raise ApiError(401, "invalid bootstrap token")
            ctx.accounts.setup(
                email=obj.get("email"), name=obj.get("name"),
                password=obj.get("password"))
            raw, _p = ctx.accounts.login(
                obj.get("email"), obj.get("password"))
            self._json(200, {"ok": True}, headers={
                "Set-Cookie": self._cookie(raw, SESSION_TTL)})

        def _accept_invite(self, body):
            if ctx.demo:
                raise ApiError(403, "demo workspace is read-only")
            ip = self.client_address[0]
            if not ctx.check_login_rate(ip):
                raise ApiError(429, "too many requests")
            obj = _obj(self._read_json(body))
            principal = self._principal()
            try:
                ctx.accounts.accept_invite(
                    token=obj.get("token"), email=obj.get("email"),
                    name=obj.get("name") or "",
                    password=obj.get("password") or "",
                    principal=principal)
            except AccountsError as exc:
                raise ApiError(exc.code, str(exc))
            self._json(200, {"ok": True})

        def _logout(self):
            raw = self._cookie_raw()
            if raw:
                ctx.accounts.logout(raw)
            self._json(200, {"ok": True}, headers={
                "Set-Cookie": self._cookie("", 0)})

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


def _match2(path: str, prefix: str, mid: str) -> tuple | None:
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    i = rest.find(mid)
    if i <= 0:
        return None
    a, b = rest[:i], rest[i + len(mid):]
    if not a or not b or "/" in a or "/" in b:
        return None
    return a, b


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
        print("Run `partial auth token` to obtain the bootstrap token"
              " for first-time setup.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
