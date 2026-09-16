from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .models import (
    AGENTS,
    EVENT_KINDS,
    MAX_IMPORT_BYTES,
    Event,
    canonical_json,
    normalize_timestamp,
    now_iso,
    scoped_session_id,
    sha256_hex,
    validate_event,
)
from .privacy import redact

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repositories(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root TEXT,
    remote TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL REFERENCES repositories(id),
    native_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    title TEXT,
    branch TEXT,
    worktree TEXT,
    parent_session_id TEXT,
    model TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    started_at TEXT,
    updated_at TEXT,
    UNIQUE(repo_id, agent, native_id)
);
CREATE TABLE IF NOT EXISTS events(
    session_id TEXT NOT NULL REFERENCES sessions(id),
    id TEXT NOT NULL,
    kind TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    tool_name TEXT,
    data TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(session_id, id)
);
CREATE TABLE IF NOT EXISTS checkpoints(
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL REFERENCES repositories(id),
    commit_sha TEXT NOT NULL,
    branch TEXT,
    message TEXT,
    author TEXT,
    created_at TEXT NOT NULL,
    files TEXT NOT NULL DEFAULT '[]',
    diff TEXT,
    session_ids TEXT NOT NULL DEFAULT '[]',
    UNIQUE(repo_id, commit_sha)
);
CREATE TABLE IF NOT EXISTS checkpoint_links(
    checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id),
    session_id TEXT NOT NULL,
    method TEXT NOT NULL,
    PRIMARY KEY(checkpoint_id, session_id)
);
CREATE TABLE IF NOT EXISTS pending_paths(
    repo_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    path TEXT NOT NULL,
    worktree TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(repo_id, session_id, path, worktree)
);
CREATE TABLE IF NOT EXISTS reviews(
    id TEXT PRIMARY KEY,
    checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id),
    author TEXT,
    body TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_session_ts
    ON events(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_sessions_repo_updated
    ON sessions(repo_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_checkpoints_repo_created
    ON checkpoints(repo_id, created_at);
CREATE INDEX IF NOT EXISTS idx_checkpoint_links_session
    ON checkpoint_links(session_id);
"""

MUTATING_TOOLS = {
    "write", "edit", "apply_patch", "notebook_edit", "multiedit",
    "edit_file", "write_file", "create_file",
}
_PATH_KEYS = ("file_path", "path", "target_file", "notebook_path", "filename")
_SESSION_STATUSES = ("active", "idle", "ended")
_STATUS_BY_KIND = {
    "session_end": "ended",
    "response": "idle",
    "prompt": "active",
    "session_start": "active",
    "tool": "active",
}
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_HEX32_RE = re.compile(r"[0-9a-f]{32}")
_SHA_RE = re.compile(r"([0-9a-f]{40}|[0-9a-f]{64})")
_MAX_STR = 8192


def default_db_path() -> Path:
    home = os.environ.get("PARTIAL_HOME")
    if home:
        return Path(home) / "partial.db"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "partial" / "partial.db"


_SCP_RE = re.compile(
    r"^(?:([^@/\s]+)@)?([A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9a-fA-F:]+\])"
    r":([^\s]*)$"
)


def _scp_parts(u: str) -> tuple[str, str] | None:
    if "://" in u:
        return None
    m = _SCP_RE.match(u)
    if not m:
        return None
    user, host, path = m.group(1), m.group(2), m.group(3)
    if not path or path.startswith("\\"):
        return None
    if user is None and (len(host) == 1 or path.startswith("/")):
        return None
    return host, path


def normalize_remote(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    scp = _scp_parts(u)
    if scp:
        host, path = scp
    else:
        try:
            parts = urlsplit(u)
        except ValueError:
            return u
        host = parts.hostname or ""
        path = parts.path or ""
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{host.lower()}/{path}" if host else path


def sanitize_remote(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return u
    scp = _scp_parts(u)
    if scp:
        return f"ssh://{scp[0]}/{scp[1]}"
    if "://" not in u:
        return u
    try:
        parts = urlsplit(u)
    except ValueError:
        return ""
    host = parts.hostname or ""
    try:
        if parts.port:
            host = f"{host}:{parts.port}"
    except ValueError:
        pass
    clean = parts._replace(netloc=host, query="", fragment="")
    return clean.geturl()


def _git_identity(path: Path) -> tuple[str, str, str]:
    def run(*args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            raise ValueError(f"not a git repository: {path}")
        return proc.stdout.strip()

    run("rev-parse", "--git-dir")
    common = run("rev-parse", "--path-format=absolute", "--git-common-dir")
    root = run("rev-parse", "--path-format=absolute", "--show-toplevel")
    remote = ""
    proc = subprocess.run(
        ["git", "-C", str(path), "remote", "get-url", "origin"],
        capture_output=True, text=True, timeout=15,
    )
    if proc.returncode == 0:
        remote = proc.stdout.strip()
    return root, common, remote


def repo_id_for(root: str, common_dir: str, remote: str) -> str:
    norm = normalize_remote(remote)
    if norm:
        return sha256_hex("partial/repo/v1\x00remote\x00" + norm)
    return sha256_hex(
        "partial/repo/v1\x00gitdir\x00" + str(Path(common_dir).resolve()))


def _tool_failed(data: dict) -> bool:
    resp = data.get("tool_response")
    if not isinstance(resp, dict):
        return False
    if resp.get("success") is False:
        return True
    if resp.get("is_error") is True:
        return True
    return False


def _event_paths(ev: Event) -> list[str]:
    paths: list[str] = []
    data = ev.data if isinstance(ev.data, dict) else {}
    if _tool_failed(data):
        return []
    ti = data.get("tool_input")
    mutating = bool(
        ev.tool_name and ev.tool_name.lower() in MUTATING_TOOLS)
    if mutating and isinstance(ti, dict):
        for key in _PATH_KEYS:
            v = ti.get(key)
            if isinstance(v, str) and v:
                paths.append(v)
    changes = data.get("changes")
    if isinstance(changes, list):
        for ch in changes:
            if isinstance(ch, dict) and isinstance(ch.get("path"), str) \
                    and ch["path"]:
                paths.append(ch["path"])
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _relativize(path: str, worktree: str) -> str | None:
    root = Path(worktree).resolve()
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    try:
        resolved = p.resolve()
        rel = resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    s = str(rel).replace("\\", "/")
    if not s or s == "." or s.startswith("../") or s == "..":
        return None
    return s


def _like_esc(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _strip_path_value(v: object, roots: list[str]) -> object:
    if not isinstance(v, str) or not v.startswith("/"):
        return v
    for root in roots:
        if v == root:
            return "."
        if v.startswith(root + "/"):
            return v[len(root) + 1:]
    return v


def _strip_event_paths(data: dict, roots: list[str]) -> dict:
    if not roots or not isinstance(data, dict):
        return data
    roots = sorted(set(roots), key=len, reverse=True)
    out = dict(data)
    ti = out.get("tool_input")
    if isinstance(ti, dict):
        ti = dict(ti)
        for k in _PATH_KEYS:
            if k in ti:
                ti[k] = _strip_path_value(ti[k], roots)
        out["tool_input"] = ti
    changes = out.get("changes")
    if isinstance(changes, list):
        out["changes"] = [
            {**ch, "path": _strip_path_value(ch.get("path"), roots)}
            if isinstance(ch, dict) else ch
            for ch in changes
        ]
    return out


def _bounded_str(v: object, name: str, limit: int = _MAX_STR) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or len(v) > limit:
        raise ValueError(f"invalid {name}")
    return v


def _norm_ts_opt(v: object, name: str) -> str | None:
    if v is None:
        return None
    return normalize_timestamp(v)


class Store:
    def __init__(self, path: str | Path | None = None):
        db = default_db_path() if path is None else Path(path)
        self.path = db
        parent = self.path.parent
        created = not parent.exists()
        parent.mkdir(parents=True, exist_ok=True)
        if created:
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
        if not db.exists():
            fd = os.open(str(db), os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(reviews)")}
            if "user_id" not in cols:
                conn.execute(
                    "ALTER TABLE reviews ADD COLUMN user_id TEXT")
            conn.commit()
        finally:
            conn.close()
        self._chmod_state()

    def _chmod_state(self) -> None:
        for name in (
            str(self.path), str(self.path) + "-wal", str(self.path) + "-shm",
        ):
            try:
                if os.path.exists(name):
                    os.chmod(name, 0o600)
            except OSError:
                pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def register_repo(self, path: str | Path) -> dict:
        root, common, remote = _git_identity(Path(path))
        rid = repo_id_for(root, common, remote)
        name = str(redact(Path(root).name or "repo"))
        safe_remote = sanitize_remote(remote)
        if safe_remote is not None:
            safe_remote = str(redact(safe_remote))
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM repositories WHERE id=?", (rid,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO repositories(id,name,root,remote,created_at)"
                    " VALUES(?,?,?,?,?)",
                    (rid, name, root, safe_remote, now_iso()),
                )
                conn.commit()
            elif not row["root"]:
                conn.execute(
                    "UPDATE repositories SET root=? WHERE id=?",
                    (root, rid))
                conn.commit()
            row = conn.execute(
                "SELECT * FROM repositories WHERE id=?", (rid,)
            ).fetchone()
            return dict(row)
        finally:
            conn.close()

    def get_repo(self, repo_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM repositories WHERE id=?", (repo_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def ingest(
        self,
        repo_id: str,
        events: list[Event],
        *,
        worktree: str | None = None,
        branch: str | None = None,
        track_paths: bool = True,
    ) -> list[str]:
        wt = str(Path(worktree).resolve()) if worktree else None
        conn = self._connect()
        inserted: list[str] = []
        try:
            with conn:
                repo = conn.execute(
                    "SELECT id FROM repositories WHERE id=?", (repo_id,)
                ).fetchone()
                if repo is None:
                    raise ValueError(f"unknown repo_id: {repo_id}")
                for ev in events:
                    validate_event(ev)
                    ev.data = redact(ev.data)
                    ev.text = redact(ev.text)
                    if ev.tool_name is not None:
                        ev.tool_name = str(redact(ev.tool_name))
                    if ev.model is not None:
                        ev.model = str(redact(ev.model))
                    safe_branch = str(redact(branch)) if branch else None
                    sid = scoped_session_id(repo_id, ev.agent, ev.session_id)
                    parent_sid = (
                        scoped_session_id(repo_id, ev.agent,
                                          ev.parent_session_id)
                        if ev.parent_session_id else None
                    )
                    existing = conn.execute(
                        "SELECT * FROM sessions WHERE id=?", (sid,)
                    ).fetchone()
                    is_new = existing is None
                    if is_new:
                        conn.execute(
                            "INSERT INTO sessions(id,repo_id,native_id,agent,"
                            "title,branch,worktree,parent_session_id,model,"
                            "status,started_at,updated_at)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                sid, repo_id, ev.session_id, ev.agent,
                                None, safe_branch, wt, parent_sid,
                                ev.model, "active", ev.timestamp,
                                ev.timestamp,
                            ),
                        )
                        existing = conn.execute(
                            "SELECT * FROM sessions WHERE id=?", (sid,)
                        ).fetchone()
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO events(session_id,id,kind,"
                        "timestamp,text,tool_name,data)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (
                            sid, ev.id, ev.kind, ev.timestamp, ev.text,
                            ev.tool_name, canonical_json(ev.data),
                        ),
                    )
                    if not cur.rowcount:
                        continue
                    inserted.append(ev.id)
                    if is_new or existing["updated_at"] is None or \
                            ev.timestamp >= existing["updated_at"]:
                        status = _STATUS_BY_KIND.get(
                            ev.kind, existing["status"])
                        title = existing["title"]
                        if not title and ev.kind == "prompt" and ev.text:
                            title = ev.text.strip().splitlines()[0][:120]
                        conn.execute(
                            "UPDATE sessions SET status=?, title=?,"
                            " updated_at=?, branch=COALESCE(?,branch),"
                            " worktree=COALESCE(?,worktree),"
                            " model=COALESCE(?,model),"
                            " parent_session_id="
                            "COALESCE(parent_session_id,?)"
                            " WHERE id=?",
                            (
                                status, title, ev.timestamp,
                                safe_branch, wt, ev.model, parent_sid,
                                sid,
                            ),
                        )
                    if track_paths and wt and ev.kind == "tool":
                        for p in _event_paths(ev):
                            rel = _relativize(p, wt)
                            if rel:
                                conn.execute(
                                    "INSERT OR IGNORE INTO pending_paths("
                                    "repo_id,session_id,path,worktree,"
                                    "created_at) VALUES(?,?,?,?,?)",
                                    (repo_id, sid, rel, wt, now_iso()),
                                )
            return inserted
        finally:
            conn.close()
            self._chmod_state()

    def resolve_session(self, repo_id: str, ref: str) -> str:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id,repo_id FROM sessions WHERE id=?", (ref,)
            ).fetchone()
            if row is not None:
                if row["repo_id"] != repo_id:
                    raise ValueError(
                        f"session {ref[:12]} belongs to a different"
                        " repository")
                return row["id"]
            rows = conn.execute(
                "SELECT id FROM sessions WHERE repo_id=? AND native_id=?",
                (repo_id, ref),
            ).fetchall()
            if not rows:
                raise ValueError(f"unknown session: {ref}")
            if len(rows) > 1:
                raise ValueError(
                    f"ambiguous session id {ref}: matches"
                    f" {len(rows)} sessions; use the full scoped id")
            return rows[0]["id"]
        finally:
            conn.close()

    def list_repos(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM repositories ORDER BY created_at").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def list_sessions(
        self,
        *,
        repo_id: str | None = None,
        agent: str | None = None,
        q: str | None = None,
        branch: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        sql = "SELECT * FROM sessions WHERE 1=1"
        params: list = []
        if repo_id:
            sql += " AND repo_id=?"
            params.append(repo_id)
        if agent:
            sql += " AND agent=?"
            params.append(agent)
        if agent and agent not in AGENTS:
            raise ValueError(f"unknown agent: {agent}")
        if branch:
            sql += " AND branch=?"
            params.append(branch)
        if q:
            if len(q) > 500:
                raise ValueError("search query too long (max 500)")
            like = f"%{_like_esc(q)}%"
            sql += (" AND (title LIKE ? ESCAPE '\\'"
                    " OR native_id LIKE ? ESCAPE '\\'"
                    " OR branch LIKE ? ESCAPE '\\'"
                    " OR EXISTS(SELECT 1 FROM events e"
                    " WHERE e.session_id=sessions.id"
                    " AND (e.text LIKE ? ESCAPE '\\'"
                    " OR e.data LIKE ? ESCAPE '\\')))")
            params += [like, like, like, like, like]
        sql += " ORDER BY COALESCE(updated_at,'') DESC LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def session_events_page(
        self, session_id: str, *, limit: int, offset: int,
        kind: str | None = None,
    ) -> list[dict]:
        if kind is not None and kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind: {kind}")
        sql = "SELECT * FROM events WHERE session_id=?"
        params: list = [session_id]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY timestamp,rowid LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            out = []
            for e in rows:
                d = dict(e)
                d["data"] = json.loads(d["data"])
                out.append(d)
            return out
        finally:
            conn.close()

    def search_events(
        self, q: str, *, repo_id: str | None = None,
        agent: str | None = None, limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        if not isinstance(q, str) or not q.strip():
            raise ValueError("search query must be a non-empty string")
        if len(q) > 500:
            raise ValueError("search query too long (max 500)")
        if agent is not None and agent not in AGENTS:
            raise ValueError(f"unknown agent: {agent}")
        like = f"%{_like_esc(q.strip())}%"
        sql = (
            "SELECT e.session_id,s.repo_id,s.agent,s.title,"
            "e.id AS event_id,e.kind,"
            "substr(e.text,1,600) AS text,e.timestamp"
            " FROM events e JOIN sessions s ON s.id=e.session_id"
            " WHERE (e.text LIKE ? ESCAPE '\\'"
            " OR e.data LIKE ? ESCAPE '\\'"
            " OR s.title LIKE ? ESCAPE '\\')"
        )
        params: list = [like, like, like]
        if repo_id:
            sql += " AND s.repo_id=?"
            params.append(repo_id)
        if agent:
            sql += " AND s.agent=?"
            params.append(agent)
        sql += " ORDER BY e.timestamp DESC,e.rowid DESC LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def repo_branches(self, repo_id: str) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT branch FROM checkpoints WHERE repo_id=?"
                " AND branch IS NOT NULL UNION SELECT DISTINCT branch"
                " FROM sessions WHERE repo_id=? AND branch IS NOT NULL"
                " ORDER BY branch",
                (repo_id, repo_id),
            ).fetchall()
            return [r["branch"] for r in rows]
        finally:
            conn.close()

    def child_sessions(self, session_id: str) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE parent_session_id=?"
                " ORDER BY updated_at", (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def sessions_for_checkpoint(self, checkpoint_id: str) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT s.* FROM sessions s JOIN checkpoint_links l"
                " ON l.session_id=s.id WHERE l.checkpoint_id=?"
                " ORDER BY s.updated_at", (checkpoint_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def add_review(
        self, checkpoint_id: str, author: str, body: str,
        actor: str | None = None,
    ) -> dict:
        if self.get_checkpoint(checkpoint_id) is None:
            raise KeyError(checkpoint_id)
        author = str(redact(author.strip()))[:80]
        body = str(redact(body.strip()))[:10000]
        if not author or not body:
            raise ValueError("review requires non-empty author and body")
        rid = uuid.uuid4().hex
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO reviews(id,checkpoint_id,author,body,"
                "created_at,user_id) VALUES(?,?,?,?,?,?)",
                (rid, checkpoint_id, author, body, now_iso(), actor),
            )
            conn.commit()
            return self._review_dict(rid)
        finally:
            conn.close()

    def _review_dict(self, rid: str) -> dict:
        conn = self._connect()
        try:
            r = conn.execute(
                "SELECT * FROM reviews WHERE id=?", (rid,)
            ).fetchone()
            return dict(r)
        finally:
            conn.close()

    def list_reviews(self, checkpoint_id: str) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM reviews WHERE checkpoint_id=?"
                " ORDER BY created_at,id", (checkpoint_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _session_events(self, conn, sid: str) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM events WHERE session_id=?"
            " ORDER BY timestamp,rowid", (sid,),
        ).fetchall()
        out = []
        for e in rows:
            d = dict(e)
            d["data"] = json.loads(d["data"])
            out.append(d)
        return out

    def get_session_meta(self, session_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_session(self, session_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if row is None:
                rows = conn.execute(
                    "SELECT * FROM sessions WHERE native_id=?",
                    (session_id,),
                ).fetchall()
                if len(rows) > 1:
                    raise ValueError(
                        f"ambiguous session id {session_id}: matches"
                        f" {len(rows)} sessions; use the full scoped id")
                row = rows[0] if rows else None
            if row is None:
                return None
            sess = dict(row)
            sess["events"] = self._session_events(conn, sess["id"])
            return sess
        finally:
            conn.close()

    def list_checkpoints(
        self,
        *,
        repo_id: str | None = None,
        branch: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        sql = "SELECT * FROM checkpoints WHERE 1=1"
        params: list = []
        if repo_id:
            sql += " AND repo_id=?"
            params.append(repo_id)
        if branch:
            sql += " AND branch=?"
            params.append(branch)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
        conn = self._connect()
        try:
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()
        for r in rows:
            r["files"] = json.loads(r["files"])
            r["session_ids"] = json.loads(r["session_ids"])
        return rows

    def _checkpoint_links(self, conn, checkpoint_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT session_id,method FROM checkpoint_links"
            " WHERE checkpoint_id=? ORDER BY session_id",
            (checkpoint_id,),
        ).fetchall()
        return [dict(l) for l in rows]

    def get_checkpoint(self, checkpoint_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,)
            ).fetchone()
            if row is None:
                return None
            r = dict(row)
            r["files"] = json.loads(r["files"])
            r["session_ids"] = json.loads(r["session_ids"])
            r["links"] = self._checkpoint_links(conn, checkpoint_id)
            return r
        finally:
            conn.close()

    def checkpoints_for_session(self, session_id: str) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT c.id FROM checkpoints c"
                " JOIN checkpoint_links l ON l.checkpoint_id=c.id"
                " WHERE l.session_id=?", (session_id,),
            ).fetchall()
            return [r["id"] for r in rows]
        finally:
            conn.close()

    def save_checkpoint(
        self,
        repo_id: str,
        checkpoint_id: str,
        commit_sha: str,
        *,
        branch: str | None,
        message: str | None,
        author: str | None,
        files: list[str],
        diff: str | None,
        links: list[tuple[str, str]],
        worktree: str | None,
        created_at: str | None = None,
    ) -> dict:
        conn = self._connect()
        try:
            with conn:
                row = conn.execute(
                    "SELECT id,session_ids FROM checkpoints"
                    " WHERE repo_id=? AND commit_sha=?",
                    (repo_id, commit_sha),
                ).fetchone()
                if row is not None:
                    checkpoint_id = row["id"]
                    existing = set(json.loads(row["session_ids"]))
                else:
                    existing = set()
                    conn.execute(
                        "INSERT INTO checkpoints(id,repo_id,commit_sha,"
                        "branch,message,author,created_at,files,diff,"
                        "session_ids) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            checkpoint_id, repo_id, commit_sha, branch,
                            message, author, created_at or now_iso(),
                            canonical_json(files), diff, "[]",
                        ),
                    )
                for sid, method in links:
                    if sid in existing:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO checkpoint_links("
                        "checkpoint_id,session_id,method) VALUES(?,?,?)",
                        (checkpoint_id, sid, method),
                    )
                    existing.add(sid)
                    if worktree:
                        for p in files:
                            conn.execute(
                                "DELETE FROM pending_paths WHERE repo_id=?"
                                " AND session_id=? AND worktree=?"
                                " AND path=?",
                                (repo_id, sid, worktree, p),
                            )
                conn.execute(
                    "UPDATE checkpoints SET session_ids=? WHERE id=?",
                    (canonical_json(sorted(existing)), checkpoint_id),
                )
                out = conn.execute(
                    "SELECT * FROM checkpoints WHERE id=?",
                    (checkpoint_id,),
                ).fetchone()
                r = dict(out)
                r["files"] = json.loads(r["files"])
                r["session_ids"] = json.loads(r["session_ids"])
                r["links"] = self._checkpoint_links(conn, checkpoint_id)
                return r
        finally:
            conn.close()

    def pending_links(
        self, repo_id: str, committed_files: set[str], worktree: str
    ) -> list[tuple[str, str]]:
        if not committed_files:
            return []
        conn = self._connect()
        try:
            placeholders = ",".join("?" for _ in committed_files)
            rows = conn.execute(
                f"SELECT DISTINCT session_id FROM pending_paths"
                f" WHERE repo_id=? AND worktree=?"
                f" AND path IN ({placeholders})",
                (repo_id, worktree, *committed_files),
            ).fetchall()
            return [(r["session_id"], "observed-worktree-overlap")
                    for r in rows]
        finally:
            conn.close()

    def _session_public_rows(self, conn, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            "SELECT id,repo_id,native_id,agent,title,branch,"
            "parent_session_id,model,status,started_at,updated_at"
            f" FROM sessions WHERE id IN ({placeholders})", ids,
        ).fetchall()
        return [dict(r) for r in rows]

    def checkpoint_bundle(self, checkpoint_id: str) -> dict:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown checkpoint: {checkpoint_id}")
            cp = dict(row)
            cp["files"] = json.loads(cp["files"])
            cp["session_ids"] = json.loads(cp["session_ids"])
            links = self._checkpoint_links(conn, checkpoint_id)
            repo = conn.execute(
                "SELECT id,name,remote,created_at FROM repositories"
                " WHERE id=?", (cp["repo_id"],),
            ).fetchone()
            sid_set = list(dict.fromkeys(
                [l["session_id"] for l in links] + cp["session_ids"]))
            seen = set(sid_set)
            frontier = list(sid_set)
            while frontier:
                placeholders = ",".join("?" for _ in frontier)
                children = conn.execute(
                    f"SELECT id FROM sessions WHERE parent_session_id IN"
                    f" ({placeholders})", frontier,
                ).fetchall()
                frontier = [c["id"] for c in children
                            if c["id"] not in seen]
                seen.update(frontier)
                sid_set.extend(frontier)
            sessions = self._session_public_rows(conn, sid_set)
            roots = self._export_roots(conn, [cp["repo_id"]], sid_set)
            events = []
            if sid_set:
                placeholders = ",".join("?" for _ in sid_set)
                rows = conn.execute(
                    "SELECT e.*, s.agent AS agent FROM events e"
                    " JOIN sessions s ON s.id=e.session_id"
                    f" WHERE e.session_id IN ({placeholders})"
                    " ORDER BY e.timestamp,e.rowid", sid_set,
                ).fetchall()
                events = [
                    {**dict(e),
                     "data": _strip_event_paths(
                         json.loads(e["data"]), roots)}
                    for e in rows
                ]
            return {
                "version": SCHEMA_VERSION,
                "repositories": [dict(repo)] if repo else [],
                "sessions": sessions,
                "events": events,
                "checkpoints": [cp],
                "links": [
                    {"checkpoint_id": checkpoint_id,
                     "session_id": l["session_id"],
                     "method": l["method"]}
                    for l in links
                ],
            }
        finally:
            conn.close()

    def export_bundle(self, *, repo_id: str | None = None) -> dict:
        conn = self._connect()
        try:
            if repo_id:
                repos = conn.execute(
                    "SELECT id,name,remote,created_at FROM repositories"
                    " WHERE id=?", (repo_id,),
                ).fetchall()
                sessions = conn.execute(
                    "SELECT id,repo_id,native_id,agent,title,branch,"
                    "parent_session_id,model,status,started_at,updated_at"
                    " FROM sessions WHERE repo_id=?", (repo_id,),
                ).fetchall()
                checkpoints = conn.execute(
                    "SELECT * FROM checkpoints WHERE repo_id=?",
                    (repo_id,),
                ).fetchall()
            else:
                repos = conn.execute(
                    "SELECT id,name,remote,created_at FROM repositories"
                ).fetchall()
                sessions = conn.execute(
                    "SELECT id,repo_id,native_id,agent,title,branch,"
                    "parent_session_id,model,status,started_at,updated_at"
                    " FROM sessions").fetchall()
                checkpoints = conn.execute(
                    "SELECT * FROM checkpoints").fetchall()
            sess_ids = [s["id"] for s in sessions]
            repo_ids = [r["id"] for r in repos]
            roots = self._export_roots(conn, repo_ids, sess_ids)
            events = []
            if sess_ids:
                placeholders = ",".join("?" for _ in sess_ids)
                events = conn.execute(
                    "SELECT e.*, s.agent AS agent FROM events e"
                    " JOIN sessions s ON s.id=e.session_id"
                    f" WHERE e.session_id IN ({placeholders})"
                    " ORDER BY e.timestamp,e.rowid", sess_ids,
                ).fetchall()
            cp_ids = [c["id"] for c in checkpoints]
            links = []
            if cp_ids:
                placeholders = ",".join("?" for _ in cp_ids)
                links = conn.execute(
                    "SELECT checkpoint_id,session_id,method FROM"
                    f" checkpoint_links WHERE checkpoint_id IN"
                    f" ({placeholders})", cp_ids,
                ).fetchall()
            return {
                "version": SCHEMA_VERSION,
                "repositories": [dict(r) for r in repos],
                "sessions": [dict(s) for s in sessions],
                "events": [
                    {**dict(e),
                     "data": _strip_event_paths(
                         json.loads(e["data"]), roots)}
                    for e in events
                ],
                "checkpoints": [
                    {
                        **dict(c),
                        "files": json.loads(c["files"]),
                        "session_ids": json.loads(c["session_ids"]),
                    }
                    for c in checkpoints
                ],
                "links": [dict(l) for l in links],
            }
        finally:
            conn.close()

    def strip_paths(self, data: dict, repo_id: str | None) -> dict:
        conn = self._connect()
        try:
            if repo_id:
                roots = self._export_roots(conn, [repo_id], [])
                rows = conn.execute(
                    "SELECT DISTINCT worktree FROM sessions"
                    " WHERE repo_id=? AND worktree IS NOT NULL",
                    (repo_id,),
                ).fetchall()
                roots += [r["worktree"] for r in rows]
            else:
                roots = [
                    r[0] for r in conn.execute(
                        "SELECT root FROM repositories"
                        " WHERE root IS NOT NULL")
                ]
        finally:
            conn.close()
        return _strip_event_paths(data, roots)

    def _export_roots(
        self, conn, repo_ids: list[str], sess_ids: list[str]
    ) -> list[str]:
        roots: list[str] = []
        if repo_ids:
            ph = ",".join("?" for _ in repo_ids)
            roots += [
                r[0] for r in conn.execute(
                    "SELECT DISTINCT root FROM repositories"
                    f" WHERE root IS NOT NULL AND id IN ({ph})",
                    repo_ids,
                )
            ]
        if sess_ids:
            ph = ",".join("?" for _ in sess_ids)
            roots += [
                r[0] for r in conn.execute(
                    "SELECT DISTINCT worktree FROM sessions"
                    f" WHERE worktree IS NOT NULL AND id IN ({ph})",
                    sess_ids,
                )
            ]
        return [str(Path(r)) for r in roots]

    def import_bundle(self, bundle: dict) -> dict:
        if not isinstance(bundle, dict):
            raise ValueError("bundle must be an object")
        if bundle.get("version") != SCHEMA_VERSION:
            raise ValueError("unsupported bundle version")
        if len(canonical_json(bundle).encode("utf-8")) > MAX_IMPORT_BYTES:
            raise ValueError("bundle exceeds 64 MiB limit")
        for name in ("repositories", "sessions", "events",
                     "checkpoints", "links"):
            seq = bundle.get(name)
            if seq is None:
                continue
            if not isinstance(seq, list) or len(seq) > 1_000_000:
                raise ValueError(f"bundle.{name} must be a bounded list")
            if not all(isinstance(x, dict) for x in seq):
                raise ValueError(f"bundle.{name} entries must be objects")
        repos = bundle.get("repositories")
        if repos is not None and not isinstance(repos, list):
            raise ValueError("bundle.repositories must be a list")
        sessions = bundle.get("sessions") or []
        events = bundle.get("events") or []
        checkpoints = bundle.get("checkpoints") or []
        links = bundle.get("links") or []
        repos = repos or []
        if bundle.get("sessions") is not None and not isinstance(
                bundle["sessions"], list):
            raise ValueError("bundle.sessions must be a list")
        if bundle.get("events") is not None and not isinstance(
                bundle["events"], list):
            raise ValueError("bundle.events must be a list")
        if bundle.get("checkpoints") is not None and not isinstance(
                bundle["checkpoints"], list):
            raise ValueError("bundle.checkpoints must be a list")

        conn = self._connect()
        try:
            with conn:
                for r in repos:
                    rid = r.get("id")
                    if not isinstance(rid, str) or not _HEX64_RE.fullmatch(
                            rid):
                        raise ValueError("invalid repository id in bundle")
                    remote = r.get("remote")
                    if remote is not None and (
                            not isinstance(remote, str)
                            or len(remote) > 2048):
                        raise ValueError("invalid remote in bundle")
                    name = _bounded_str(r.get("name"), "repo name", 256)
                    created = _norm_ts_opt(
                        r.get("created_at"), "repo created_at") or now_iso()
                    conn.execute(
                        "INSERT OR IGNORE INTO repositories(id,name,root,"
                        "remote,created_at) VALUES(?,?,NULL,?,?)",
                        (
                            rid, redact(name or "repo"),
                            sanitize_remote(remote or "") or None, created,
                        ),
                    )
                known_repos = {
                    row["id"]
                    for row in conn.execute("SELECT id FROM repositories")
                }
                bundle_sids = set()
                for s in sessions:
                    for k in ("id", "repo_id", "native_id", "agent"):
                        if not isinstance(s.get(k), str) or not s[k] \
                                or len(s[k]) > _MAX_STR:
                            raise ValueError(f"invalid session field: {k}")
                    if not _HEX64_RE.fullmatch(s["id"]):
                        raise ValueError("invalid session id in bundle")
                    if s["repo_id"] not in known_repos:
                        raise ValueError(
                            "session references unknown repository")
                    if s["agent"] not in AGENTS:
                        raise ValueError(f"unknown agent: {s['agent']}")
                    expected = scoped_session_id(
                        s["repo_id"], s["agent"], s["native_id"])
                    if s["id"] != expected:
                        raise ValueError(
                            "session id does not match scoped"
                            " (repo,agent,native_id) identity")
                    status = s.get("status")
                    if status is None:
                        status = "active"
                    if not isinstance(status, str) \
                            or status not in _SESSION_STATUSES:
                        raise ValueError(
                            f"invalid session status: {status}")
                    started = _norm_ts_opt(
                        s.get("started_at"), "session started_at")
                    updated = _norm_ts_opt(
                        s.get("updated_at"), "session updated_at")
                    parent = s.get("parent_session_id")
                    if parent is not None and (
                            not isinstance(parent, str)
                            or not _HEX64_RE.fullmatch(parent)):
                        raise ValueError("invalid parent_session_id")
                    title = _bounded_str(s.get("title"), "session title")
                    branch = _bounded_str(s.get("branch"), "session branch")
                    model = _bounded_str(s.get("model"), "session model")
                    bundle_sids.add(s["id"])
                    existing = conn.execute(
                        "SELECT * FROM sessions WHERE id=?", (s["id"],)
                    ).fetchone()
                    if existing is None:
                        conn.execute(
                            "INSERT INTO sessions(id,repo_id,native_id,"
                            "agent,title,branch,worktree,"
                            "parent_session_id,model,status,started_at,"
                            "updated_at) VALUES(?,?,?,?,?,?,NULL,?,?,?,?,?)",
                            (
                                s["id"], s["repo_id"], s["native_id"],
                                s["agent"], redact(title) if title else None,
                                redact(branch) if branch else None, parent,
                                redact(model) if model else None, status,
                                started, updated,
                            ),
                        )
                    else:
                        for k in ("repo_id", "native_id", "agent"):
                            if existing[k] != s[k]:
                                raise ValueError(
                                    "session id conflicts with existing"
                                    " session identity")
                        if updated and (not existing["updated_at"]
                                        or updated > existing["updated_at"]):
                            conn.execute(
                                "UPDATE sessions SET"
                                " title=COALESCE(?,title), status=?,"
                                " updated_at=? WHERE id=?",
                                (
                                    redact(title) if title else None,
                                    status, updated, s["id"],
                                ),
                            )
                session_repo = {
                    row["id"]: row["repo_id"]
                    for row in conn.execute(
                        "SELECT id,repo_id FROM sessions")
                }
                session_agent = {
                    row["id"]: row["agent"]
                    for row in conn.execute(
                        "SELECT id,agent FROM sessions")
                }
                for s in sessions:
                    parent = s.get("parent_session_id")
                    if parent and parent in session_repo and \
                            session_repo[parent] != s["repo_id"]:
                        raise ValueError(
                            "session parent references a different"
                            " repository")
                ev_count = 0
                for e in events:
                    for k in ("id", "session_id", "agent", "kind",
                              "timestamp"):
                        if k not in e:
                            raise ValueError(
                                f"event missing required field: {k}")
                    for k in ("id", "session_id", "agent", "kind",
                              "timestamp"):
                        if not isinstance(e[k], str) or not e[k]:
                            raise ValueError(
                                f"event field {k} must be a"
                                " non-empty string")
                    if "text" in e and not isinstance(e["text"], str):
                        raise ValueError("event text must be a string")
                    if "data" in e and not isinstance(e["data"], dict):
                        raise ValueError(
                            "event data must be an object")
                    if e.get("tool_name") is not None and not isinstance(
                            e["tool_name"], str):
                        raise ValueError(
                            "event tool_name must be a string")
                    target_sid = e["session_id"]
                    if target_sid not in session_repo:
                        raise ValueError(
                            "event references unknown session in bundle")
                    if e["agent"] != session_agent[target_sid]:
                        raise ValueError(
                            "event agent does not match target session")
                    evd = Event(
                        id=e["id"],
                        session_id=target_sid,
                        agent=e["agent"],
                        kind=e["kind"],
                        timestamp=e["timestamp"],
                        text=e.get("text") or "",
                        tool_name=e.get("tool_name"),
                        data=e.get("data") or {},
                    )
                    validate_event(evd)
                    evd.data = redact(evd.data)
                    evd.text = redact(evd.text)
                    if evd.tool_name is not None:
                        evd.tool_name = str(redact(evd.tool_name))
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO events(session_id,id,kind,"
                        "timestamp,text,tool_name,data)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (
                            target_sid, evd.id, evd.kind, evd.timestamp,
                            evd.text, evd.tool_name,
                            canonical_json(evd.data),
                        ),
                    )
                    ev_count += cur.rowcount
                cp_count = 0
                for c in checkpoints:
                    for k in ("id", "repo_id", "commit_sha"):
                        if not isinstance(c.get(k), str) or not c[k]:
                            raise ValueError(f"invalid checkpoint field: {k}")
                    cid = c["id"]
                    if not _HEX32_RE.fullmatch(cid):
                        raise ValueError("invalid checkpoint id in bundle")
                    if c["repo_id"] not in known_repos:
                        raise ValueError(
                            "checkpoint references unknown repository")
                    if not _SHA_RE.fullmatch(c["commit_sha"]):
                        raise ValueError("invalid commit_sha in checkpoint")
                    files = c.get("files")
                    if not isinstance(files, list):
                        raise ValueError("checkpoint files must be a list")
                    for f in files:
                        if not isinstance(f, str) or not f \
                                or len(f) > 4096 or f.startswith("/") \
                                or "\\" in f or f.split("/").count(".."):
                            raise ValueError(
                                f"invalid checkpoint file path: {f!r}")
                    sids = c.get("session_ids")
                    if not isinstance(sids, list):
                        raise ValueError(
                            "checkpoint session_ids must be a list")
                    for sref in sids:
                        if not isinstance(sref, str) \
                                or sref not in session_repo:
                            raise ValueError(
                                "checkpoint references unknown session")
                        if session_repo[sref] != c["repo_id"]:
                            raise ValueError(
                                "checkpoint session belongs to a"
                                " different repository")
                    branch = _bounded_str(c.get("branch"), "cp branch")
                    message = _bounded_str(c.get("message"), "cp message")
                    author = _bounded_str(c.get("author"), "cp author")
                    created = _norm_ts_opt(
                        c.get("created_at"), "cp created_at") or now_iso()
                    diff = c.get("diff")
                    if diff is not None and not isinstance(diff, str):
                        raise ValueError("invalid checkpoint diff")
                    clash = conn.execute(
                        "SELECT repo_id,commit_sha FROM checkpoints"
                        " WHERE id=?", (cid,),
                    ).fetchone()
                    if clash is not None and (
                            clash["repo_id"] != c["repo_id"]
                            or clash["commit_sha"] != c["commit_sha"]):
                        raise ValueError(
                            f"checkpoint id {cid} conflicts with existing"
                            " checkpoint identity")
                    same = conn.execute(
                        "SELECT id FROM checkpoints WHERE repo_id=?"
                        " AND commit_sha=?",
                        (c["repo_id"], c["commit_sha"]),
                    ).fetchone()
                    if same is not None and same["id"] != cid:
                        raise ValueError(
                            "checkpoint commit already recorded under a"
                            " different checkpoint id")
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO checkpoints(id,repo_id,"
                        "commit_sha,branch,message,author,created_at,"
                        "files,diff,session_ids) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            cid, c["repo_id"], c["commit_sha"],
                            redact(branch) if branch else None,
                            redact(message) if message else None,
                            redact(author) if author else None, created,
                            canonical_json(files),
                            redact(diff) if diff else None,
                            canonical_json(sorted(sids)),
                        ),
                    )
                    cp_count += cur.rowcount
                    row = conn.execute(
                        "SELECT session_ids FROM checkpoints WHERE id=?",
                        (cid,),
                    ).fetchone()
                    merged = sorted(set(json.loads(row[0])) | set(sids))
                    conn.execute(
                        "UPDATE checkpoints SET session_ids=? WHERE id=?",
                        (canonical_json(merged), cid),
                    )
                known_cps = {
                    row["id"]: row["repo_id"]
                    for row in conn.execute(
                        "SELECT id,repo_id FROM checkpoints")
                }
                for l in links:
                    cid = l.get("checkpoint_id")
                    sid = l.get("session_id")
                    method = l.get("method")
                    if not isinstance(cid, str) or cid not in known_cps:
                        raise ValueError(
                            "link references unknown checkpoint")
                    if not isinstance(sid, str) or sid not in session_repo:
                        raise ValueError(
                            "link references unknown session")
                    if session_repo[sid] != known_cps[cid]:
                        raise ValueError(
                            "link session belongs to a different"
                            " repository than the checkpoint")
                    if not isinstance(method, str) or len(method) > 128:
                        raise ValueError("invalid link method")
                    conn.execute(
                        "INSERT OR IGNORE INTO checkpoint_links("
                        "checkpoint_id,session_id,method) VALUES(?,?,?)",
                        (cid, sid, method),
                    )
                    row = conn.execute(
                        "SELECT session_ids FROM checkpoints WHERE id=?",
                        (cid,),
                    ).fetchone()
                    merged = sorted(set(json.loads(row[0])) | {sid})
                    conn.execute(
                        "UPDATE checkpoints SET session_ids=? WHERE id=?",
                        (canonical_json(merged), cid),
                    )
            return {
                "repositories": len(repos),
                "sessions": len(sessions),
                "events": ev_count,
                "checkpoints": cp_count,
            }
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"bundle integrity violation: {exc}") from exc
        finally:
            conn.close()
            self._chmod_state()

    def stats(self) -> dict:
        conn = self._connect()
        try:
            return {
                "sessions": conn.execute(
                    "SELECT COUNT(*) FROM sessions").fetchone()[0],
                "checkpoints": conn.execute(
                    "SELECT COUNT(*) FROM checkpoints").fetchone()[0],
                "repositories": conn.execute(
                    "SELECT COUNT(*) FROM repositories").fetchone()[0],
            }
        finally:
            conn.close()
