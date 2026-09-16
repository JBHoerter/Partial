from __future__ import annotations

import json
import math
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
from .privacy import has_secret_pattern, redact
from . import attribution as _attr
from . import brain_contract
from .brain_contract import EMBEDDING_MODEL

SCHEMA_VERSION = 1
RUN_KINDS = ("ask", "review", "investigate", "dispatch")
RUN_STATUSES = ("planned", "running", "completed", "partial",
                "error", "interrupted", "imported")

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
CREATE TABLE IF NOT EXISTS attribution_files(
    repo_id TEXT NOT NULL,
    worktree TEXT NOT NULL,
    path TEXT NOT NULL,
    base_commit TEXT,
    state TEXT NOT NULL,
    PRIMARY KEY(repo_id,worktree,path)
);
CREATE TABLE IF NOT EXISTS attribution_pending(
    repo_id TEXT NOT NULL,
    worktree TEXT NOT NULL,
    session_id TEXT NOT NULL,
    call_key TEXT NOT NULL,
    path TEXT NOT NULL,
    before_hashes TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    ambiguous INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(repo_id,worktree,session_id,call_key,path)
);
CREATE TABLE IF NOT EXISTS checkpoint_attribution(
    checkpoint_id TEXT PRIMARY KEY REFERENCES checkpoints(id),
    report TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS native_sessions(
    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
    agent TEXT NOT NULL,
    native_id TEXT NOT NULL,
    format TEXT NOT NULL,
    local_path TEXT,
    archive TEXT,
    registered_at TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_settings(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_repos(
    project_id TEXT NOT NULL REFERENCES projects(id),
    repo_id TEXT NOT NULL REFERENCES repositories(id),
    PRIMARY KEY(project_id, repo_id)
);
CREATE INDEX IF NOT EXISTS idx_events_session_ts
    ON events(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_sessions_repo_updated
    ON sessions(repo_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_checkpoints_repo_created
    ON checkpoints(repo_id, created_at);
CREATE INDEX IF NOT EXISTS idx_checkpoint_links_session
    ON checkpoint_links(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_parent
    ON sessions(parent_session_id);
"""

MUTATING_TOOLS = {
    "write", "edit", "apply_patch", "notebook_edit", "multiedit",
    "edit_file", "write_file", "create_file",
}
_PATH_KEYS = ("file_path", "path", "target_file", "notebook_path", "filename")
# Event ``data`` keys whose values denote local filesystem paths.
# Keys are compared after lowercasing and stripping non-alphanumerics,
# so snake_case and camelCase spellings (``file_path``/``filePath``)
# match alike.  ``_strip_event_paths`` rewrites absolute values under
# these keys at any nesting depth so API responses, handoffs, memory
# documents, and exported bundles never carry machine-local paths
# (worktree roots, home directories, provider state dirs such as
# ``~/.claude``).  Relative values and non-path keys pass through
# untouched, so safe relative paths and ordinary text stay useful.
_STRIP_PATH_KEYS = frozenset({
    "cwd", "workdir", "worktree", "workspace", "root", "projectdir",
    "projectroot", "reporoot", "checkoutroot", "directory", "dir",
    "localpath", "transcriptpath", "filepath", "path", "paths",
    "files", "targetfile", "notebookpath", "filename",
})
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
_NATIVE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
NATIVE_AGENTS = ("devin", "claude", "codex")
NATIVE_FORMAT_FOR = {
    "claude": "claude-jsonl",
    "codex": "codex-rollout",
    "devin": "devin-atif",
}
_NATIVE_FORMATS = tuple(NATIVE_FORMAT_FOR.values()) + ("native-id",)

# Per-process owner token stamped on workflow runs saved with status
# "running".  `interrupt_running_runs` only marks a running row
# interrupted when its recorded owner pid is provably dead (or the row
# predates owner tracking), so a second Partial process sharing the same
# store can never mislabel another live process's job as interrupted.
_RUN_OWNER = f"{os.getpid()}-{uuid.uuid4().hex[:16]}"

# Run ``details`` keys reserved for internal process-ownership
# bookkeeping.  ``run_owner`` is the canonical marker stamped by
# ``save_run``; ``runner``/``pid`` are the legacy server marker and
# may still be present in databases written before ``run_owner``
# existed.  None of them are ever trusted from callers or bundles,
# and all are stripped from get/list/export so they cannot leak
# through an API response or a transport bundle.
_RUN_INTERNAL_DETAILS = frozenset({"run_owner", "runner", "pid"})


def _strip_run_details(details: dict) -> dict:
    return {k: v for k, v in details.items()
            if k not in _RUN_INTERNAL_DETAILS}


_SEED_SOURCE_RE = re.compile(r"seed:[^/\x00]{1,190}")
_REVIEW_SOURCE_RE = re.compile(
    r"review:(?:[0-9a-f]{40}|[0-9a-f]{64})"
    r"\.\.(?:[0-9a-f]{40}|[0-9a-f]{64})")
_RUN_PAYLOAD_MAX = 200000


def _run_owner_pid(owner: object) -> int | None:
    if not isinstance(owner, str):
        return None
    head, sep, _token = owner.partition("-")
    if not sep or not head.isdigit():
        return None
    return int(head)


def _pid_alive(pid: int | None) -> bool:
    """Best-effort liveness check for a recorded run-owner pid.

    POSIX ``kill(pid, 0)`` is authoritative; a permission error still
    means the process exists.  On platforms without kill(2) semantics
    we cannot verify liveness cheaply and conservatively report the
    owner as alive, which never mislabels a running job.
    """
    if type(pid) is not int or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name != "posix":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _validate_attribution(conn, entry, known_cps, session_repo) -> dict:
    cid = entry.get("checkpoint_id")
    if not isinstance(cid, str) or cid not in known_cps:
        raise ValueError("attribution references unknown checkpoint")
    rep = entry.get("report")
    if not isinstance(rep, dict):
        raise ValueError("attribution report must be an object")
    if rep.get("version") != 1 or rep.get("method") != _attr.METHOD:
        raise ValueError("unsupported attribution report format")
    row = conn.execute(
        "SELECT files FROM checkpoints WHERE id=?", (cid,)).fetchone()
    cp_files = set(json.loads(row["files"])) if row else set()
    files = rep.get("files")
    if not isinstance(files, list) or len(files) > 10000:
        raise ValueError("attribution files must be a bounded list")
    seen: set = set()
    out_files = []
    for f in files:
        if not isinstance(f, dict):
            raise ValueError("attribution file must be an object")
        path = f.get("path")
        lines = f.get("lines")
        if not isinstance(path, str) or not path \
                or len(path) > 4096 or path not in cp_files:
            raise ValueError(
                "attribution file path not in checkpoint")
        if path in seen:
            raise ValueError("duplicate attribution file path")
        seen.add(path)
        if not isinstance(lines, list) or len(lines) > 20000:
            raise ValueError("attribution lines must be a bounded list")
        norm_lines = []
        for ent in lines:
            if not isinstance(ent, dict):
                raise ValueError("attribution line must be an object")
            norm = {k: ent.get(k) for k in
                    ("side", "line", "kind", "session_id", "evidence")}
            if norm["side"] not in ("old", "new"):
                raise ValueError("invalid attribution line side")
            if type(norm["line"]) is not int \
                    or not 1 <= norm["line"] <= _attr.MAX_LINES:
                raise ValueError("invalid attribution line position")
            if norm["kind"] not in _attr.KINDS \
                    or norm["evidence"] not in _attr.EVIDENCE:
                raise ValueError(
                    "invalid attribution kind or evidence")
            s = norm.get("session_id")
            if norm["kind"] == "agent":
                if not isinstance(s, str) or s not in session_repo \
                        or session_repo[s] != known_cps[cid]:
                    raise ValueError(
                        "attribution references foreign session")
            else:
                norm["session_id"] = None
            norm_lines.append(norm)
        summary = _attr.summarize(norm_lines)
        out_files.append(
            {"path": path, "lines": norm_lines, "summary": summary})
    excluded = rep.get("excluded") or []
    if not isinstance(excluded, list) or len(excluded) > 10000:
        raise ValueError("attribution excluded must be a bounded list")
    out_excl = []
    excl_seen = set()
    for x in excluded:
        if not isinstance(x, dict) \
                or not isinstance(x.get("path"), str) \
                or len(x["path"]) > 4096 \
                or not isinstance(x.get("reason"), str) \
                or len(x["reason"]) > 128:
            raise ValueError("invalid attribution excluded entry")
        if x["path"] not in cp_files or x["path"] in seen \
                or x["path"] in excl_seen:
            raise ValueError(
                "attribution excluded path is invalid for checkpoint")
        excl_seen.add(x["path"])
        out_excl.append({"path": x["path"], "reason": x["reason"]})
    for p in sorted(cp_files - seen - excl_seen):
        out_excl.append({"path": p, "reason": "missing-report"})
    lims = _attr.report(_attr.new_state([]), [])["limitations"]
    return {
        "version": 1, "method": _attr.METHOD,
        "capture_source": "imported-claim",
        "summary": _attr.aggregate(out_files),
        "files": out_files, "excluded": out_excl,
        "limitations": lims,
    }


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


_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _is_path_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    norm = re.sub(r"[^a-z0-9]", "", key.lower())
    return norm in _STRIP_PATH_KEYS


def _strip_path_value(v: object, roots: list[str]) -> object:
    if not isinstance(v, str):
        return v
    for root in roots:
        if v == root:
            return "."
        if v.startswith(root + "/"):
            return v[len(root) + 1:]
    if v.startswith("/") or _WIN_ABS_RE.match(v):
        # Absolute but outside every known root (e.g. a provider's
        # state directory): keep only the basename so no machine-local
        # directory layout leaks, while the name itself stays useful.
        return v.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] \
            or "."
    return v


def _strip_paths_node(v: object, roots: list[str],
                      path_key: bool) -> object:
    if isinstance(v, dict):
        return {k: _strip_paths_node(x, roots, _is_path_key(k))
                for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [
            _strip_path_value(x, roots)
            if path_key and isinstance(x, str)
            else _strip_paths_node(x, roots, False)
            for x in v
        ]
    if path_key:
        return _strip_path_value(v, roots)
    return v


def _strip_event_paths(data: dict, roots: list[str]) -> dict:
    if not isinstance(data, dict):
        return data
    roots = sorted({str(Path(str(r))) for r in roots if r},
                   key=len, reverse=True)
    return _strip_paths_node(data, roots, False)


def unified_diff_delta(diff: object) -> dict:
    """Count added/removed content lines in a stored unified diff.

    Only ``+``/``-`` content lines are counted; the ``+++``/``---``
    file headers are excluded.  Stored diffs may be ``None``, empty,
    or truncated mid-line (checkpoints cap diff size), so this is a
    pure line-prefix count that never fails and never inspects the
    filesystem.
    """
    added = removed = 0
    if not isinstance(diff, str) or not diff:
        return {"additions": 0, "deletions": 0}
    for line in diff.split("\n"):
        if line.startswith("+"):
            if not line.startswith("+++"):
                added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return {"additions": added, "deletions": removed}


_USAGE_FIELDS = (
    "input_tokens", "output_tokens", "cached_input_tokens",
    "cache_creation_input_tokens",
)


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
        self.fts_ok = True
        try:
            conn.executescript(_SCHEMA)
            try:
                conn.executescript(brain_contract.SCHEMA)
            except sqlite3.OperationalError as exc:
                if "fts5" not in str(exc).lower():
                    raise
                self.fts_ok = False
                stmts = [s for s in brain_contract.SCHEMA.split(";")
                         if s.strip() and "fts5" not in s.lower()]
                conn.executescript(";\n".join(stmts))
            conn.execute("BEGIN IMMEDIATE")
            cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(reviews)")}
            if "user_id" not in cols:
                conn.execute(
                    "ALTER TABLE reviews ADD COLUMN user_id TEXT")
            mcols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(memory_documents)")}
            if mcols and "archived" not in mcols:
                conn.execute(
                    "ALTER TABLE memory_documents ADD COLUMN"
                    " archived INTEGER NOT NULL DEFAULT 0")
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
        # Rows carry dashboard summary columns alongside the raw
        # session fields: event_count, checkpoint_count (distinct
        # linked checkpoints), child_count (direct child sessions),
        # and is_subagent (a parent_session_id is recorded).
        sql = (
            "SELECT s.*,"
            " (SELECT COUNT(*) FROM events e"
            "  WHERE e.session_id=s.id) AS event_count,"
            " (SELECT COUNT(DISTINCT l.checkpoint_id)"
            "  FROM checkpoint_links l"
            "  WHERE l.session_id=s.id) AS checkpoint_count,"
            " (SELECT COUNT(*) FROM sessions c"
            "  WHERE c.parent_session_id=s.id) AS child_count"
            " FROM sessions s WHERE 1=1"
        )
        params: list = []
        if repo_id:
            sql += " AND s.repo_id=?"
            params.append(repo_id)
        if agent:
            sql += " AND s.agent=?"
            params.append(agent)
        if agent and agent not in AGENTS:
            raise ValueError(f"unknown agent: {agent}")
        if branch:
            sql += " AND s.branch=?"
            params.append(branch)
        if q:
            if len(q) > 500:
                raise ValueError("search query too long (max 500)")
            like = f"%{_like_esc(q)}%"
            sql += (" AND (s.title LIKE ? ESCAPE '\\'"
                    " OR s.native_id LIKE ? ESCAPE '\\'"
                    " OR s.branch LIKE ? ESCAPE '\\'"
                    " OR EXISTS(SELECT 1 FROM events e"
                    " WHERE e.session_id=s.id"
                    " AND (e.text LIKE ? ESCAPE '\\'"
                    " OR e.data LIKE ? ESCAPE '\\')))")
            params += [like, like, like, like, like]
        sql += (" ORDER BY COALESCE(s.updated_at,'') DESC"
                " LIMIT ? OFFSET ?")
        params += [int(limit), int(offset)]
        conn = self._connect()
        try:
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()
        for r in rows:
            r["is_subagent"] = r.get("parent_session_id") is not None
        return rows

    def session_rollup(self, session_id: str) -> dict:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT"
                " (SELECT COUNT(*) FROM events e"
                "  WHERE e.session_id=s.id) AS event_count,"
                " (SELECT COUNT(DISTINCT l.checkpoint_id)"
                "  FROM checkpoint_links l"
                "  WHERE l.session_id=s.id) AS checkpoint_count,"
                " (SELECT COUNT(*) FROM sessions c"
                "  WHERE c.parent_session_id=s.id) AS child_count"
                " FROM sessions s WHERE s.id=?",
                (session_id,)).fetchone()
            if row is None:
                return {}
            return {
                "event_count": row["event_count"],
                "checkpoint_count": row["checkpoint_count"],
                "child_count": row["child_count"],
            }
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

    @staticmethod
    def _repo_stats(conn, repo_id: str) -> dict:
        """Dashboard summary fields for one repository.

        Aggregates session/checkpoint counts, distinct agents and
        branches, last recorded activity, the latest session and
        checkpoint, and memory index metadata — without ever reading
        the repository ``root`` path or other local-only fields.
        """
        srow = conn.execute(
            "SELECT COUNT(*) AS c,"
            " MAX(COALESCE(updated_at, started_at)) AS last"
            " FROM sessions WHERE repo_id=?", (repo_id,)).fetchone()
        crow = conn.execute(
            "SELECT COUNT(*) AS c, MAX(created_at) AS last"
            " FROM checkpoints WHERE repo_id=?", (repo_id,)).fetchone()
        agents = [r["agent"] for r in conn.execute(
            "SELECT DISTINCT agent FROM sessions WHERE repo_id=?"
            " ORDER BY agent", (repo_id,))]
        branch_count = conn.execute(
            "SELECT COUNT(*) AS c FROM ("
            " SELECT branch FROM checkpoints WHERE repo_id=?"
            "  AND branch IS NOT NULL"
            " UNION"
            " SELECT branch FROM sessions WHERE repo_id=?"
            "  AND branch IS NOT NULL)",
            (repo_id, repo_id)).fetchone()["c"]
        latest_s = conn.execute(
            "SELECT id,native_id,agent,title,status,started_at,"
            "updated_at FROM sessions WHERE repo_id=?"
            " ORDER BY COALESCE(updated_at, started_at, '') DESC, id"
            " LIMIT 1", (repo_id,)).fetchone()
        latest_c = conn.execute(
            "SELECT id,commit_sha,message,branch,author,created_at"
            " FROM checkpoints WHERE repo_id=?"
            " ORDER BY created_at DESC, id LIMIT 1",
            (repo_id,)).fetchone()
        idx = conn.execute(
            "SELECT commit_sha,indexed_at FROM repository_indexes"
            " WHERE repo_id=?", (repo_id,)).fetchone()
        activity = [x for x in (srow["last"], crow["last"]) if x]
        return {
            "session_count": srow["c"],
            "checkpoint_count": crow["c"],
            "branch_count": branch_count,
            "agents": agents,
            "last_activity": max(activity) if activity else None,
            "latest_session": {
                "id": latest_s["id"],
                "native_id": latest_s["native_id"],
                "agent": latest_s["agent"],
                "title": latest_s["title"],
                "status": latest_s["status"],
                "started_at": latest_s["started_at"],
                "updated_at": latest_s["updated_at"],
            } if latest_s is not None else None,
            "latest_checkpoint": {
                "id": latest_c["id"],
                "commit_sha": latest_c["commit_sha"],
                "message": latest_c["message"],
                "branch": latest_c["branch"],
                "author": latest_c["author"],
                "created_at": latest_c["created_at"],
            } if latest_c is not None else None,
            "indexed": {
                "commit_sha": idx["commit_sha"],
                "indexed_at": idx["indexed_at"],
            } if idx is not None else None,
        }

    def repo_summary(self, repo_id: str) -> dict | None:
        """Repository row (safe columns only) plus dashboard stats."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id,name,remote,created_at FROM repositories"
                " WHERE id=?", (repo_id,)).fetchone()
            if row is None:
                return None
            return {**dict(row), **self._repo_stats(conn, repo_id)}
        finally:
            conn.close()

    def list_repo_summaries(self) -> list[dict]:
        """All repositories with dashboard stats, in one connection."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id,name,remote,created_at FROM repositories"
                " ORDER BY created_at,id").fetchall()
            return [
                {**dict(r), **self._repo_stats(conn, r["id"])}
                for r in rows
            ]
        finally:
            conn.close()

    def child_sessions(self, session_id: str) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT s.*,"
                " (SELECT COUNT(*) FROM events e"
                "  WHERE e.session_id=s.id) AS event_count,"
                " (SELECT COUNT(DISTINCT l.checkpoint_id)"
                "  FROM checkpoint_links l"
                "  WHERE l.session_id=s.id) AS checkpoint_count,"
                " (SELECT COUNT(*) FROM sessions c"
                "  WHERE c.parent_session_id=s.id) AS child_count"
                " FROM sessions s WHERE s.parent_session_id=?"
                " ORDER BY s.updated_at", (session_id,),
            ).fetchall()
            out = [dict(r) for r in rows]
            for r in out:
                r["is_subagent"] = \
                    r.get("parent_session_id") is not None
            return out
        finally:
            conn.close()

    def sessions_for_checkpoint(self, checkpoint_id: str) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT s.*,"
                " (SELECT COUNT(*) FROM events e"
                "  WHERE e.session_id=s.id) AS event_count,"
                " (SELECT COUNT(DISTINCT l2.checkpoint_id)"
                "  FROM checkpoint_links l2"
                "  WHERE l2.session_id=s.id) AS checkpoint_count,"
                " (SELECT COUNT(*) FROM sessions c"
                "  WHERE c.parent_session_id=s.id) AS child_count"
                " FROM sessions s JOIN checkpoint_links l"
                " ON l.session_id=s.id WHERE l.checkpoint_id=?"
                " ORDER BY s.updated_at", (checkpoint_id,),
            ).fetchall()
            out = [dict(r) for r in rows]
            for r in out:
                r["is_subagent"] = \
                    r.get("parent_session_id") is not None
            return out
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

    def get_attribution(self, checkpoint_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT report FROM checkpoint_attribution"
                " WHERE checkpoint_id=?", (checkpoint_id,)).fetchone()
            return json.loads(row["report"]) if row else None
        finally:
            conn.close()

    def add_pending_paths(self, repo_id: str, session_id: str,
                          paths: list[str], worktree: str) -> None:
        conn = self._connect()
        try:
            with conn:
                for p in paths:
                    conn.execute(
                        "INSERT OR IGNORE INTO pending_paths(repo_id,"
                        "session_id,path,worktree,created_at)"
                        " VALUES(?,?,?,?,?)",
                        (repo_id, session_id, p, worktree, now_iso()))
        finally:
            conn.close()

    def end_session(self, session_id: str) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE sessions SET status='ended',"
                    " updated_at=? WHERE id=?",
                    (now_iso(), session_id))
        finally:
            conn.close()

    def upsert_native(self, session_id: str, agent: str,
                      native_id: str, fmt: str, *,
                      local_path: str | None, archive: str | None,
                      source: str) -> dict:
        if agent not in NATIVE_AGENTS:
            raise ValueError(f"invalid native agent: {agent!r}")
        if fmt != "native-id" and fmt != NATIVE_FORMAT_FOR.get(agent):
            raise ValueError(f"invalid native format: {fmt!r}")
        conn = self._connect()
        try:
            with conn:
                existing = conn.execute(
                    "SELECT * FROM native_sessions WHERE session_id=?",
                    (session_id,)).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO native_sessions(session_id,agent,"
                        "native_id,format,local_path,archive,"
                        "registered_at,source) VALUES(?,?,?,?,?,?,?,?)",
                        (session_id, agent, native_id, fmt, local_path,
                         archive, now_iso(), source))
                else:
                    if existing["agent"] != agent \
                            or existing["native_id"] != native_id:
                        raise ValueError(
                            "native registration conflicts with"
                            " existing identity")
                    upgrade = existing["format"] == "native-id" \
                        or existing["format"] == fmt
                    if not upgrade:
                        raise ValueError(
                            "cannot change native format from"
                            f" {existing['format']} to {fmt}")
                    conn.execute(
                        "UPDATE native_sessions SET format=?,"
                        " local_path=COALESCE(?,local_path),"
                        " archive=COALESCE(?,archive),"
                        " registered_at=?, source=?"
                        " WHERE session_id=?",
                        (fmt, local_path, archive, now_iso(), source,
                         session_id))
            return self.get_native(session_id)
        finally:
            conn.close()

    def get_native(self, session_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM native_sessions WHERE session_id=?",
                (session_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_native(self, *, repo_id: str | None = None) -> list[dict]:
        sql = ("SELECT n.session_id,n.agent,n.native_id,n.format,"
               "n.registered_at,n.source,"
               "n.local_path IS NOT NULL AS has_local_path,"
               "n.archive IS NOT NULL AS has_archive"
               " FROM native_sessions n JOIN sessions s"
               " ON s.id=n.session_id")
        params: list = []
        if repo_id:
            sql += " WHERE s.repo_id=?"
            params.append(repo_id)
        sql += " ORDER BY n.registered_at,n.session_id"
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def memory_setting(self, key: str) -> str | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM memory_settings WHERE key=?",
                (key,)).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()

    def set_memory_setting(self, key: str, value: str) -> None:
        if not isinstance(key, str) or not re.fullmatch(
                r"[a-z0-9_.-]{1,64}", key):
            raise ValueError("invalid setting key")
        if not isinstance(value, str) or len(value) > 2000:
            raise ValueError("invalid setting value")
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO memory_settings(key,value)"
                    " VALUES(?,?) ON CONFLICT(key)"
                    " DO UPDATE SET value=excluded.value",
                    (key, value))
                conn.commit()
        finally:
            conn.close()

    def save_run(self, run_id: str, kind: str, repo_id: str | None,
                 status: str, source_ids: list[str], report: dict,
                 details: dict) -> None:
        if not isinstance(run_id, str) or not _HEX64_RE.fullmatch(
                run_id):
            raise ValueError("invalid workflow run id")
        if kind not in RUN_KINDS:
            raise ValueError("invalid workflow kind")
        if status not in RUN_STATUSES:
            raise ValueError("invalid workflow status")
        if repo_id is not None and not _HEX64_RE.fullmatch(
                str(repo_id)):
            raise ValueError("invalid workflow repo_id")
        if not isinstance(source_ids, list) or len(source_ids) > 500 \
                or any(not isinstance(x, str)
                       or not _HEX64_RE.fullmatch(x)
                       for x in source_ids):
            raise ValueError("invalid workflow source_ids")
        if not isinstance(report, dict) or not isinstance(details, dict):
            raise ValueError("workflow report/details must be objects")
        # Internal ownership markers are never trusted from the
        # caller; the only persisted marker is the one stamped here.
        details = _strip_run_details(details)
        if status == "running":
            # Stamp the owning process so interrupted-run recovery can
            # tell a dead owner from a live one; never trusted from
            # the caller.
            details["run_owner"] = _RUN_OWNER
        if len(canonical_json(report)) > _RUN_PAYLOAD_MAX \
                or len(canonical_json(details)) > _RUN_PAYLOAD_MAX \
                or len(canonical_json(source_ids)) > _RUN_PAYLOAD_MAX:
            raise ValueError("workflow run payload too large")
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO workflow_runs(id,kind,"
                    "repo_id,status,created_at,updated_at,source_ids,"
                    "report,details)"
                    " VALUES(?,?,?,?,"
                    "COALESCE((SELECT created_at FROM workflow_runs"
                    " WHERE id=?),?),?,?,?,?)",
                    (run_id, kind, repo_id, status, run_id,
                     now_iso(), now_iso(), canonical_json(source_ids),
                     canonical_json(report), canonical_json(details)))
                conn.commit()
        finally:
            conn.close()

    def interrupt_running_runs(self) -> int:
        """Mark dead 'running' workflow runs as interrupted.

        Runs saved with status 'running' carry a per-process owner
        token (``details.run_owner`` = ``"<pid>-<random>"``).  A row is
        only interrupted when its owner pid is verifiably dead, or when
        the row carries no usable owner (legacy/forged records cannot be
        attributed to a live process).  Rows owned by a live local
        process are left untouched so a second Partial process sharing
        this store cannot mislabel its jobs.  Rows written before
        ``run_owner`` existed may carry the legacy server ``pid``
        marker; a live recorded pid is still honoured.
        """
        conn = self._connect()
        try:
            with conn:
                rows = conn.execute(
                    "SELECT id,details FROM workflow_runs"
                    " WHERE status='running'").fetchall()
                count = 0
                for r in rows:
                    try:
                        det = json.loads(r["details"])
                    except (ValueError, TypeError):
                        det = {}
                    if not isinstance(det, dict):
                        det = {}
                    pid = _run_owner_pid(det.get("run_owner"))
                    if pid is None:
                        # Legacy rows may only have the pre-run_owner
                        # server marker ("runner"/"pid").
                        pid = det.get("pid")
                    if _pid_alive(pid):
                        continue
                    clean = _strip_run_details(det)
                    conn.execute(
                        "UPDATE workflow_runs SET status='interrupted',"
                        " report=?, details=?, updated_at=?"
                        " WHERE id=?",
                        (canonical_json({
                            "error": "interrupted: the server or CLI"
                                     " process exited before this run"
                                     " finished"}),
                         canonical_json(clean), now_iso(), r["id"]))
                    count += 1
                conn.commit()
                return count
        finally:
            conn.close()

    def get_run(self, run_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM workflow_runs WHERE id=?",
                (run_id,)).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["source_ids"] = json.loads(d["source_ids"])
            d["report"] = json.loads(d["report"])
            d["details"] = json.loads(d["details"])
            if isinstance(d["details"], dict):
                d["details"] = _strip_run_details(d["details"])
            return d
        finally:
            conn.close()

    def list_runs(self, *, repo_id=None, kind=None,
                  limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM workflow_runs WHERE 1=1"
        params: list = []
        if repo_id:
            sql += " AND repo_id=?"
            params.append(repo_id)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY created_at DESC,id LIMIT ?"
        params.append(int(limit))
        conn = self._connect()
        try:
            out = []
            for r in conn.execute(sql, params).fetchall():
                d = dict(r)
                d["source_ids"] = json.loads(d["source_ids"])
                d["report"] = json.loads(d["report"])
                d["details"] = json.loads(d["details"])
                if isinstance(d["details"], dict):
                    d["details"] = _strip_run_details(d["details"])
                out.append(d)
            return out
        finally:
            conn.close()

    def create_project(self, name: str) -> dict:
        if not isinstance(name, str) or not name.strip() \
                or len(name) > 200:
            raise ValueError("project name must be 1..200 chars")
        pid = uuid.uuid4().hex
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO projects(id,name,created_at)"
                    " VALUES(?,?,?)",
                    (pid, name.strip(), now_iso()))
                conn.commit()
            return {"id": pid, "name": name.strip(),
                    "repos": []}
        finally:
            conn.close()

    def attach_project_repo(self, project_id: str,
                            repo_id: str) -> dict:
        conn = self._connect()
        try:
            with conn:
                if conn.execute(
                        "SELECT 1 FROM projects WHERE id=?",
                        (project_id,)).fetchone() is None:
                    raise KeyError(project_id)
                if conn.execute(
                        "SELECT 1 FROM repositories WHERE id=?",
                        (repo_id,)).fetchone() is None:
                    raise KeyError(repo_id)
                conn.execute(
                    "INSERT OR IGNORE INTO project_repos(project_id,"
                    "repo_id) VALUES(?,?)", (project_id, repo_id))
                conn.commit()
            return self.get_project(project_id)
        finally:
            conn.close()

    def get_project(self, project_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM projects WHERE id=?",
                (project_id,)).fetchone()
            if row is None:
                return None
            p = dict(row)
            p["repos"] = [r["repo_id"] for r in conn.execute(
                "SELECT repo_id FROM project_repos WHERE project_id=?"
                " ORDER BY repo_id", (project_id,)).fetchall()]
            return p
        finally:
            conn.close()

    def list_projects(self) -> list[dict]:
        conn = self._connect()
        try:
            out = []
            for r in conn.execute(
                    "SELECT * FROM projects ORDER BY name").fetchall():
                p = dict(r)
                p["repos"] = [x["repo_id"] for x in conn.execute(
                    "SELECT repo_id FROM project_repos"
                    " WHERE project_id=? ORDER BY repo_id",
                    (p["id"],)).fetchall()]
                out.append(p)
            return out
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
        q: str | None = None,
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
        if q:
            if len(q) > 500:
                raise ValueError("search query too long (max 500)")
            like = f"%{_like_esc(q)}%"
            sql += (" AND (message LIKE ? ESCAPE '\\'"
                    " OR commit_sha LIKE ? ESCAPE '\\'"
                    " OR branch LIKE ? ESCAPE '\\')")
            params += [like, like, like]
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
        conn = self._connect()
        try:
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
            for r in rows:
                r["files"] = json.loads(r["files"])
                r["session_ids"] = json.loads(r["session_ids"])
            self._enrich_checkpoints(conn, rows)
        finally:
            conn.close()
        return rows

    def _checkpoint_links(self, conn, checkpoint_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT session_id,method FROM checkpoint_links"
            " WHERE checkpoint_id=? ORDER BY session_id",
            (checkpoint_id,),
        ).fetchall()
        return [dict(l) for l in rows]

    @staticmethod
    def _enrich_checkpoints(conn, rows: list[dict]) -> None:
        """Attach computed summary fields to checkpoint rows in place.

        Each row gains ``file_count``, ``session_count`` (distinct
        sessions named by ``session_ids`` or ``checkpoint_links``),
        ``agents`` (distinct agents of those sessions), ``additions``/
        ``deletions`` counted from the stored unified diff, and
        ``ai_percentage``/``coverage_percentage`` from the recorded
        attribution report when one exists (``None`` otherwise).
        Expects ``files`` and ``session_ids`` already JSON-decoded.
        """
        if not rows:
            return
        ids = [r["id"] for r in rows]
        marks = ",".join("?" for _ in ids)
        linked: dict[str, set] = {
            r["id"]: set(r.get("session_ids") or []) for r in rows}
        for l in conn.execute(
                "SELECT checkpoint_id,session_id FROM checkpoint_links"
                f" WHERE checkpoint_id IN ({marks})", ids):
            linked[l["checkpoint_id"]].add(l["session_id"])
        sids = set().union(*linked.values()) if linked else set()
        agent_by_sid: dict[str, str] = {}
        if sids:
            smarks = ",".join("?" for _ in sids)
            for s in conn.execute(
                    "SELECT id,agent FROM sessions"
                    f" WHERE id IN ({smarks})", sorted(sids)):
                agent_by_sid[s["id"]] = s["agent"]
        summaries: dict[str, dict] = {}
        for a in conn.execute(
                "SELECT checkpoint_id,report FROM checkpoint_attribution"
                f" WHERE checkpoint_id IN ({marks})", ids):
            try:
                rep = json.loads(a["report"])
            except (ValueError, TypeError):
                continue
            summ = rep.get("summary") if isinstance(rep, dict) else None
            if isinstance(summ, dict):
                summaries[a["checkpoint_id"]] = summ
        for r in rows:
            r["file_count"] = len(r.get("files") or [])
            linked_sids = {
                s for s in (linked.get(r["id"]) or set())
                if s in agent_by_sid}
            r["session_count"] = len(linked_sids)
            r["agents"] = sorted({
                agent_by_sid[s] for s in linked_sids})
            delta = unified_diff_delta(r.get("diff"))
            r["additions"] = delta["additions"]
            r["deletions"] = delta["deletions"]
            summ = summaries.get(r["id"]) or {}
            r["ai_percentage"] = summ.get("agent_percentage")
            r["coverage_percentage"] = summ.get("coverage_percentage")

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
            self._enrich_checkpoints(conn, [r])
            return r
        finally:
            conn.close()

    def checkpoint_token_usage(self, checkpoint_id: str) -> dict | None:
        """Aggregate token usage across unique linked sessions.

        Per-session totals come from ``brain_contract.usage_totals``
        over each session's recorded ``usage`` events; the four token
        fields are summed across sessions that reported them.
        Missing, malformed, or invalid usage data is skipped so a
        detail read never fails on accounting.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT session_ids FROM checkpoints WHERE id=?",
                (checkpoint_id,)).fetchone()
            if row is None:
                return None
            sids = set(json.loads(row["session_ids"]))
            sids.update(
                r["session_id"] for r in conn.execute(
                    "SELECT session_id FROM checkpoint_links"
                    " WHERE checkpoint_id=?", (checkpoint_id,)))
            if sids:
                marks = ",".join("?" for _ in sids)
                sids = {r["id"] for r in conn.execute(
                    f"SELECT id FROM sessions WHERE id IN ({marks})",
                    sorted(sids))}
            sums = {f: 0 for f in _USAGE_FIELDS}
            have = {f: False for f in _USAGE_FIELDS}
            sessions_used = events_used = unclassified = 0
            complete = bool(sids)
            for sid in sorted(sids):
                events = []
                for e in conn.execute(
                        "SELECT id,kind,timestamp,data FROM events"
                        " WHERE session_id=? AND kind='usage'"
                        " ORDER BY timestamp,rowid", (sid,)):
                    try:
                        data = json.loads(e["data"])
                    except (ValueError, TypeError):
                        data = {}
                    events.append({
                        "id": e["id"], "kind": e["kind"],
                        "timestamp": e["timestamp"],
                        "data": data if isinstance(data, dict) else {}})
                try:
                    totals = brain_contract.usage_totals(events)
                except Exception:
                    # Invalid recorded usage must not turn a
                    # checkpoint read into a failure.
                    continue
                sessions_used += 1
                events_used += int(totals.get("events_used") or 0)
                unclassified += int(
                    totals.get("unclassified_events") or 0)
                complete = complete and bool(totals.get("complete"))
                for f in _USAGE_FIELDS:
                    v = totals.get(f)
                    if type(v) is int and v >= 0:
                        sums[f] += v
                        have[f] = True
            out = {
                f: (sums[f] if have[f] else None)
                for f in _USAGE_FIELDS}
            out["basis"] = "session-sums"
            out["sessions"] = sessions_used
            out["events_used"] = events_used
            out["unclassified_events"] = unclassified
            out["complete"] = bool(
                sessions_used and sessions_used == len(sids)
                and complete
                and have["input_tokens"] and have["output_tokens"])
            return out
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
                self._enrich_checkpoints(conn, [r])
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
            cp_ids = {checkpoint_id}
            extra_cps: dict[str, dict] = {}
            # Closure fixpoint: documents cited by exported decisions or
            # workflow runs must import cleanly, so pull in the sessions
            # (with their events) and checkpoints those documents
            # reference, plus everything those checkpoints reference in
            # turn.
            while True:
                mem = self._export_memory(
                    conn, [cp["repo_id"]], checkpoint=cp,
                    session_ids=seen)
                want_sessions: set[str] = set()
                want_cps: set[str] = set()
                for d in mem["documents"]:
                    src = str(d.get("source_id") or "")
                    if d["kind"] == "session":
                        if src.startswith("seed:"):
                            continue
                        sess = src.split(":", 1)[0]
                        if sess not in seen:
                            want_sessions.add(sess)
                    elif d["kind"] == "checkpoint":
                        if src.startswith("review:"):
                            continue
                        if src not in cp_ids:
                            want_cps.add(src)
                added = False
                for cpid in sorted(want_cps):
                    crow = conn.execute(
                        "SELECT * FROM checkpoints WHERE id=?"
                        " AND repo_id=?",
                        (cpid, cp["repo_id"])).fetchone()
                    if crow is None:
                        continue
                    cp_ids.add(cpid)
                    cextra = dict(crow)
                    cextra["files"] = json.loads(cextra["files"])
                    cextra["session_ids"] = json.loads(
                        cextra["session_ids"])
                    extra_cps[cpid] = cextra
                    added = True
                    for s in cextra["session_ids"]:
                        if s not in seen:
                            want_sessions.add(s)
                    for l in self._checkpoint_links(conn, cpid):
                        if l["session_id"] not in seen:
                            want_sessions.add(l["session_id"])
                frontier = [s for s in sorted(want_sessions)
                            if s not in seen]
                if frontier:
                    added = True
                    seen.update(frontier)
                    sid_set.extend(frontier)
                    while frontier:
                        placeholders = ",".join("?" for _ in frontier)
                        children = conn.execute(
                            "SELECT id FROM sessions WHERE"
                            f" parent_session_id IN ({placeholders})",
                            frontier,
                        ).fetchall()
                        frontier = [c["id"] for c in children
                                    if c["id"] not in seen]
                        seen.update(frontier)
                        sid_set.extend(frontier)
                if not added:
                    break
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
            cp_marks = ",".join("?" for _ in cp_ids)
            all_links = conn.execute(
                "SELECT checkpoint_id,session_id,method FROM"
                f" checkpoint_links WHERE checkpoint_id IN ({cp_marks})"
                " ORDER BY checkpoint_id,session_id",
                sorted(cp_ids),
            ).fetchall()
            return {
                "version": SCHEMA_VERSION,
                "repositories": [dict(repo)] if repo else [],
                "sessions": sessions,
                "events": events,
                "checkpoints": [cp] + [
                    extra_cps[c] for c in sorted(extra_cps)],
                "links": [dict(l) for l in all_links],
                "attribution": self._export_attribution(
                    conn, sorted(cp_ids)),
                "native_sessions": self._export_native(conn, sid_set),
                "memory": mem,
            }
        finally:
            conn.close()

    @staticmethod
    def _export_memory(conn, repo_ids, *, checkpoint=None,
                       session_ids=None) -> dict:
        if not repo_ids:
            return {"version": 1, "documents": [], "symbols": [],
                    "edges": [], "decisions": [], "runs": [],
                    "indexes": []}
        ph = ",".join("?" for _ in repo_ids)
        docs = [dict(r) for r in conn.execute(
            "SELECT id,repo_id,kind,source_id,title,text,path,"
            "line_start,line_end,commit_sha,updated_at,embedding,"
            "embedding_model,archived FROM memory_documents"
            f" WHERE repo_id IN ({ph})", repo_ids).fetchall()]
        decisions = [dict(r) for r in conn.execute(
            f"SELECT * FROM decisions WHERE repo_id IN ({ph})",
            repo_ids).fetchall()]
        runs = []
        for r in conn.execute(
                "SELECT id,kind,repo_id,status,created_at,updated_at,"
                "source_ids,report,details FROM workflow_runs"
                f" WHERE repo_id IN ({ph})", repo_ids).fetchall():
            d = dict(r)
            try:
                det = json.loads(d["details"])
            except (ValueError, TypeError):
                det = None
            if isinstance(det, dict):
                # Internal process-ownership bookkeeping (run_owner
                # and the legacy runner/pid marker) is never exported.
                d["details"] = canonical_json(_strip_run_details(det))
            runs.append(d)
        if checkpoint is not None:
            keep = set()
            sids = session_ids or set()
            for d in docs:
                if d["kind"] == "decision":
                    keep.add(d["id"])
                elif d["kind"] == "checkpoint" and \
                        d["source_id"] == checkpoint["id"]:
                    keep.add(d["id"])
                elif d["kind"] == "session" and \
                        d["source_id"].split(":", 1)[0] in sids:
                    keep.add(d["id"])
                elif d["kind"] == "code" and \
                        d["commit_sha"] == checkpoint["commit_sha"]:
                    keep.add(d["id"])
            cited = set()
            for dec in decisions:
                try:
                    cited.update(json.loads(dec["source_ids"]))
                except (ValueError, TypeError):
                    pass
            for run in runs:
                try:
                    cited.update(json.loads(run["source_ids"]))
                    det = json.loads(run["details"])
                    for ev in (det.get("evidence") or []):
                        if isinstance(ev, dict) and ev.get("id"):
                            cited.add(ev["id"])
                except (ValueError, TypeError):
                    pass
            keep |= cited
            docs = [d for d in docs if d["id"] in keep]
        symbols = [dict(r) for r in conn.execute(
            f"SELECT * FROM graph_symbols WHERE repo_id IN ({ph})",
            repo_ids).fetchall()]
        if checkpoint is not None:
            symbols = [s for s in symbols
                       if s["commit_sha"] == checkpoint["commit_sha"]]
        sym_ids = {s["id"] for s in symbols}
        edges = [dict(r) for r in conn.execute(
            f"SELECT * FROM graph_edges WHERE repo_id IN ({ph})",
            repo_ids).fetchall()]
        if checkpoint is not None:
            # Unresolved "import:<module>" ghost targets are never
            # persisted locally and are rejected on import, so they are
            # never exported either.
            edges = [e for e in edges
                     if e["source_id"] in sym_ids
                     and e["target_id"] in sym_ids]
        indexes = [dict(r) for r in conn.execute(
            f"SELECT * FROM repository_indexes WHERE repo_id IN ({ph})",
            repo_ids).fetchall()]
        if checkpoint is not None:
            # A checkpoint bundle only carries the code index when the
            # recorded index commit is exactly the checkpointed commit;
            # otherwise the exported code documents are a historical
            # snapshot and are marked archived so import cannot mistake
            # them for the receiver's current index.
            indexes = [i for i in indexes
                       if i["commit_sha"] == checkpoint["commit_sha"]]
            if not indexes:
                for d in docs:
                    if d["kind"] == "code":
                        d["archived"] = 1
        return {"version": 1, "documents": docs, "symbols": symbols,
                "edges": edges, "decisions": decisions,
                "runs": runs, "indexes": indexes}

    @staticmethod
    def _import_memory(conn, memory, known_repos) -> None:
        if not isinstance(memory, dict):
            raise ValueError("bundle.memory must be an object")
        if memory.get("version") != 1:
            raise ValueError("unsupported bundle.memory version")
        for name in ("documents", "symbols", "edges", "decisions",
                     "runs", "indexes"):
            seq = memory.get(name) or []
            if not isinstance(seq, list) or len(seq) > 200000:
                raise ValueError(
                    f"bundle.memory.{name} must be a bounded list")
            if not all(isinstance(x, dict) for x in seq):
                raise ValueError(
                    f"bundle.memory.{name} entries must be objects")

        def repo_ok(rid):
            return isinstance(rid, str) and rid in known_repos

        fts_ok = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='memory_fts'"
        ).fetchone() is not None
        from .memory import (
            DOC_KINDS, document_id as _did, _symbol_id as _sid)

        def norm_ts(value, field):
            if not isinstance(value, str):
                raise ValueError(f"invalid memory {field}")
            try:
                return normalize_timestamp(value)
            except ValueError as exc:
                raise ValueError(
                    f"invalid memory {field}") from exc

        incoming_idx = {}
        for idx in memory.get("indexes") or []:
            if not repo_ok(idx.get("repo_id")) \
                    or not _SHA_RE.fullmatch(
                        str(idx.get("commit_sha") or "")):
                raise ValueError("invalid repository index record")
            incoming_idx[idx["repo_id"]] = {
                "commit_sha": str(idx["commit_sha"]),
                "indexed_at": norm_ts(
                    idx.get("indexed_at"), "index timestamp")}

        def index_fresher(repo_id):
            # The incoming index replaces local code-index state when it
            # is at least as fresh; a strictly older index leaves the
            # fresher local graph/index untouched.
            inc = incoming_idx.get(repo_id)
            if inc is None:
                return False
            row = conn.execute(
                "SELECT indexed_at FROM repository_indexes"
                " WHERE repo_id=?", (repo_id,)).fetchone()
            return row is None \
                or inc["indexed_at"] >= row["indexed_at"]

        docs = memory.get("documents") or []
        for d in docs:
            if not _HEX64_RE.fullmatch(str(d.get("id"))):
                raise ValueError("invalid memory document id")
            if not repo_ok(d.get("repo_id")):
                raise ValueError("memory document references unknown"
                                 " repository")
            if d.get("kind") not in DOC_KINDS:
                raise ValueError("invalid memory document kind")
            title = d.get("title")
            text = d.get("text")
            if not isinstance(title, str) or len(title) > 1000:
                raise ValueError("invalid memory document title")
            if not isinstance(text, str) or len(text) > 200000:
                raise ValueError("invalid memory document text")
            if has_secret_pattern(title) or has_secret_pattern(text):
                raise ValueError(
                    "memory document contains unsanitized secrets")
            path = d.get("path")
            if path is not None:
                if not isinstance(path, str) or len(path) > 1000 \
                        or not path or path.startswith("/") \
                        or ".." in path.split("/") \
                        or path != path.strip():
                    raise ValueError("invalid memory document path")
            for key in ("line_start", "line_end"):
                v = d.get(key)
                if v is not None and (type(v) is not int
                                      or not 1 <= v <= 10_000_000):
                    raise ValueError("invalid memory document lines")
            if d.get("line_start") is not None \
                    and d.get("line_end") is not None \
                    and d["line_end"] < d["line_start"]:
                raise ValueError("invalid memory document lines")
            sha = d.get("commit_sha")
            if sha is not None and not _SHA_RE.fullmatch(str(sha)):
                raise ValueError("invalid memory document commit")
            archived = d.get("archived", 0)
            if type(archived) is not int or archived not in (0, 1):
                raise ValueError("invalid memory document archived flag")
            sid = d.get("source_id")
            if not isinstance(sid, str) or not sid or len(sid) > 200:
                raise ValueError("invalid memory document source_id")
            if d["kind"] == "session":
                if sid.startswith("seed:"):
                    # Ephemeral seed documents are strictly prefixed,
                    # single-name, and repo-scoped like any other doc.
                    if not _SEED_SOURCE_RE.fullmatch(sid):
                        raise ValueError("invalid seed source_id")
                else:
                    sess, sep, eid = sid.partition(":")
                    if not sep or not eid \
                            or not _HEX64_RE.fullmatch(sess):
                        raise ValueError(
                            "invalid session document source_id")
                    row = conn.execute(
                        "SELECT 1 FROM sessions WHERE id=?"
                        " AND repo_id=?", (sess, d["repo_id"])
                    ).fetchone()
                    if row is None:
                        raise ValueError(
                            "memory document references unknown"
                            " session")
                    if conn.execute(
                            "SELECT 1 FROM events WHERE session_id=?"
                            " AND id=?", (sess, eid)).fetchone() is None:
                        raise ValueError(
                            "memory document references unknown"
                            " session event")
            elif d["kind"] == "checkpoint":
                if sid.startswith("review:"):
                    # Ephemeral review documents are strictly prefixed
                    # "review:<base>..<head>" and carry the head commit.
                    m = _REVIEW_SOURCE_RE.fullmatch(sid)
                    if m is None or sha is None \
                            or sid.rsplit("..", 1)[1] != sha:
                        raise ValueError("invalid review source_id")
                else:
                    if not _HEX32_RE.fullmatch(sid):
                        raise ValueError(
                            "invalid checkpoint document source_id")
                    row = conn.execute(
                        "SELECT 1 FROM checkpoints WHERE id=?"
                        " AND repo_id=?", (sid, d["repo_id"])
                    ).fetchone()
                    if row is None:
                        raise ValueError(
                            "memory document references unknown"
                            " checkpoint")
            elif d["kind"] == "code":
                if not _SHA_RE.fullmatch(sid) or sha is None:
                    raise ValueError(
                        "code document requires blob source id and"
                        " commit sha")
                if path is None or d.get("line_start") is None \
                        or d.get("line_end") is None:
                    raise ValueError(
                        "code document requires path and lines")
            elif d["kind"] == "decision":
                if not _HEX64_RE.fullmatch(sid):
                    raise ValueError(
                        "invalid decision document source_id")
            emb = d.get("embedding")
            if emb is not None:
                vec = json.loads(emb) if isinstance(emb, str) else emb
                if not isinstance(vec, list) or not vec \
                        or len(vec) > 8192 \
                        or any(type(x) not in (int, float)
                               or not math.isfinite(x) for x in vec):
                    raise ValueError("invalid memory embedding")
                if d.get("embedding_model") != EMBEDDING_MODEL:
                    raise ValueError(
                        "memory embedding model mismatch")
                emb = canonical_json(vec)
            elif d.get("embedding_model") is not None:
                raise ValueError("invalid memory embedding model")
            expected = _did(
                d["repo_id"], d["kind"], sid, path, sha,
                d.get("line_start"), text)
            if expected != d["id"]:
                raise ValueError("memory document id mismatch")
            d["_archived"] = archived
            d["_emb"] = emb
            d["_updated"] = norm_ts(
                d.get("updated_at"), "document timestamp")

        incoming_symbols = memory.get("symbols") or []
        incoming_edges = memory.get("edges") or []
        # A fresher (or identical) incoming index atomically archives
        # the repo's old code documents and replaces only that repo's
        # graph; a strictly older incoming index leaves the fresher
        # local state alone.
        fresher = {rid for rid in incoming_idx if index_fresher(rid)}
        for rid in fresher:
            conn.execute(
                "UPDATE memory_documents SET archived=1"
                " WHERE repo_id=? AND kind='code' AND archived=0",
                (rid,))
            conn.execute(
                "DELETE FROM graph_symbols WHERE repo_id=?", (rid,))
            conn.execute(
                "DELETE FROM graph_edges WHERE repo_id=?", (rid,))
        for d in docs:
            archived = d["_archived"]
            if d["kind"] == "code" and d["repo_id"] in incoming_idx \
                    and d["repo_id"] not in fresher:
                archived = 1
            emb, emb_model = d["_emb"], None
            if emb is None:
                prior = conn.execute(
                    "SELECT embedding,embedding_model FROM"
                    " memory_documents WHERE id=?", (d["id"],)
                ).fetchone()
                if prior is not None and prior["embedding"] is not None:
                    emb, emb_model = (
                        prior["embedding"], prior["embedding_model"])
            else:
                emb_model = d.get("embedding_model")
            conn.execute(
                "INSERT OR REPLACE INTO memory_documents(id,repo_id,"
                "kind,source_id,title,text,path,line_start,line_end,"
                "commit_sha,updated_at,embedding,embedding_model,"
                "archived)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (d["id"], d["repo_id"], d["kind"],
                 d["source_id"], d["title"], d["text"],
                 d.get("path"), d.get("line_start"),
                 d.get("line_end"), d.get("commit_sha"),
                 d["_updated"], emb, emb_model,
                 archived))
            if fts_ok:
                conn.execute(
                    "DELETE FROM memory_fts WHERE id=?", (d["id"],))
                conn.execute(
                    "INSERT INTO memory_fts(id,repo_id,kind,title,"
                    "text) VALUES(?,?,?,?,?)",
                    (d["id"], d["repo_id"], d["kind"],
                     d["title"], d["text"]))
        sym_repo: dict[str, str] = {}
        for s in incoming_symbols:
            if not repo_ok(s.get("repo_id")) \
                    or not _HEX64_RE.fullmatch(str(s.get("id"))):
                raise ValueError("invalid graph symbol")
            path = s.get("path")
            if not isinstance(path, str) or len(path) > 1000 \
                    or not path or path.startswith("/") \
                    or ".." in path.split("/"):
                raise ValueError("invalid graph symbol path")
            name = s.get("name")
            qname = s.get("qualified_name")
            if not isinstance(name, str) or not 1 <= len(name) <= 200 \
                    or not isinstance(qname, str) \
                    or not 1 <= len(qname) <= 500:
                raise ValueError("invalid graph symbol name")
            if has_secret_pattern(name) or has_secret_pattern(qname):
                raise ValueError("graph symbol contains secrets")
            if s.get("kind") not in (
                    "module", "function", "class", "definition"):
                raise ValueError("invalid graph symbol kind")
            if s.get("language") not in (
                    "python", "javascript", "typescript", "go",
                    "rust", "java"):
                raise ValueError("invalid graph symbol language")
            if s.get("analysis") not in ("ast", "lexical"):
                raise ValueError("invalid graph symbol analysis")
            if not _SHA_RE.fullmatch(str(s.get("commit_sha") or "")):
                raise ValueError("invalid graph symbol commit")
            line, end_line = s.get("line"), s.get("end_line")
            if type(line) is not int or type(end_line) is not int \
                    or not 1 <= line <= 10_000_000 \
                    or not 1 <= end_line <= 10_000_000 \
                    or end_line < line:
                raise ValueError("invalid graph symbol lines")
            if _sid(s["repo_id"], path, qname) != s["id"]:
                raise ValueError("graph symbol id mismatch")
            # Graph content only applies when the bundle carries an
            # index record for the repo that is at least as fresh as
            # the local one; older graph rows are dropped, never
            # merged into the fresher local graph.
            if s["repo_id"] in fresher:
                conn.execute(
                    "INSERT OR REPLACE INTO graph_symbols(id,"
                    "repo_id,path,name,qualified_name,kind,line,"
                    "end_line,language,analysis,commit_sha)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (s["id"], s["repo_id"], path, name, qname,
                     s["kind"], line, end_line, s["language"],
                     s["analysis"], str(s["commit_sha"])))
                sym_repo[s["id"]] = s["repo_id"]
        for e in incoming_edges:
            if not repo_ok(e.get("repo_id")) \
                    or e.get("kind") not in ("calls", "imports"):
                raise ValueError("invalid graph edge")
            src, dst = e.get("source_id"), e.get("target_id")
            if not isinstance(src, str) or not isinstance(dst, str) \
                    or not _HEX64_RE.fullmatch(src) \
                    or not _HEX64_RE.fullmatch(dst):
                raise ValueError("invalid graph edge endpoint")
            if e["repo_id"] not in fresher:
                # Belongs to an older index snapshot: validated above
                # but never merged into the fresher local graph.
                continue
            if sym_repo.get(src) != e["repo_id"] \
                    or sym_repo.get(dst) != e["repo_id"]:
                raise ValueError(
                    "graph edge endpoint missing from same-repo"
                    " symbols")
            conn.execute(
                "INSERT OR IGNORE INTO graph_edges(repo_id,source_id,"
                "target_id,kind) VALUES(?,?,?,?)",
                (e["repo_id"], src, dst, e["kind"]))
        seen_dec = {}
        for dec in memory.get("decisions") or []:
            if not repo_ok(dec.get("repo_id")) \
                    or not _HEX64_RE.fullmatch(str(dec.get("id"))):
                raise ValueError("invalid decision record")
            title = dec.get("title")
            body = dec.get("body")
            author = dec.get("author")
            if not isinstance(title, str) or not title.strip() \
                    or len(title) > 500 \
                    or not isinstance(body, str) \
                    or not body.strip() or len(body) > 50000 \
                    or not isinstance(author, str) \
                    or not author.strip() or len(author) > 200:
                raise ValueError("invalid decision fields")
            if has_secret_pattern(title) or has_secret_pattern(body):
                raise ValueError(
                    "decision contains unsanitized secrets")
            if dec.get("status") not in ("active", "superseded"):
                raise ValueError("invalid decision status")
            src = dec.get("source_ids")
            if isinstance(src, str):
                try:
                    src = json.loads(src)
                except ValueError:
                    raise ValueError("invalid decision source_ids")
            if not isinstance(src, list) or len(src) > 200 \
                    or any(not _HEX64_RE.fullmatch(str(x))
                           for x in src):
                raise ValueError("invalid decision source_ids")
            sup = dec.get("supersedes")
            if sup is not None and not _HEX64_RE.fullmatch(str(sup)):
                raise ValueError("invalid decision supersedes")
            created = norm_ts(
                dec.get("created_at"), "decision timestamp")
            existing = conn.execute(
                "SELECT repo_id,title,body,status FROM decisions"
                " WHERE id=?", (dec["id"],)).fetchone()
            if existing is not None:
                if existing["repo_id"] != dec["repo_id"] \
                        or existing["title"] != title.strip() \
                        or existing["body"] != body.strip():
                    raise ValueError(
                        "conflicting decision content for existing"
                        " id")
            else:
                conn.execute(
                    "INSERT INTO decisions(id,repo_id,title,body,"
                    "status,source_ids,author,created_at,supersedes)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (dec["id"], dec["repo_id"], title.strip(),
                     body.strip(), dec["status"],
                     canonical_json(src), author.strip(), created,
                     sup))
            seen_dec[dec["id"]] = {
                "repo_id": dec["repo_id"], "status": dec["status"],
                "supersedes": sup, "src": src}
        for did, dec in seen_dec.items():
            # Status transitions are monotonic: an incoming 'superseded'
            # marker is applied, but an 'active' claim never resurrects
            # a locally superseded decision.
            if dec["status"] == "superseded":
                conn.execute(
                    "UPDATE decisions SET status='superseded'"
                    " WHERE id=? AND repo_id=?",
                    (did, dec["repo_id"]))
            sup = dec["supersedes"]
            if sup is not None:
                target = conn.execute(
                    "SELECT repo_id FROM decisions WHERE id=?",
                    (sup,)).fetchone()
                if target is None \
                        or target["repo_id"] != dec["repo_id"]:
                    raise ValueError(
                        "decision supersedes unknown or foreign"
                        " decision")
                chain, cur = {did}, sup
                while cur is not None:
                    if cur in chain:
                        raise ValueError(
                            "decision supersession cycle")
                    chain.add(cur)
                    row = conn.execute(
                        "SELECT supersedes FROM decisions WHERE id=?",
                        (cur,)).fetchone()
                    cur = row["supersedes"] if row else None
            for sid2 in dec["src"]:
                row = conn.execute(
                    "SELECT repo_id FROM memory_documents WHERE id=?",
                    (sid2,)).fetchone()
                if row is None or row["repo_id"] != dec["repo_id"]:
                    raise ValueError(
                        "decision source id not in repository")
        for run in memory.get("runs") or []:
            if not _HEX64_RE.fullmatch(str(run.get("id"))):
                raise ValueError("invalid workflow run id")
            if run.get("kind") not in RUN_KINDS:
                raise ValueError("invalid workflow run kind")
            if run.get("status") not in RUN_STATUSES:
                raise ValueError("invalid workflow run status")
            if run.get("repo_id") is not None \
                    and not repo_ok(run["repo_id"]):
                raise ValueError("invalid workflow run record")
            src = run.get("source_ids")
            if isinstance(src, str):
                try:
                    src = json.loads(src)
                except ValueError:
                    raise ValueError("invalid run source_ids")
            if not isinstance(src, list) or len(src) > 500 \
                    or any(not _HEX64_RE.fullmatch(str(x))
                           for x in src):
                raise ValueError("invalid run source_ids")
            report = run.get("report")
            details = run.get("details")
            if isinstance(report, str):
                try:
                    report = json.loads(report)
                except ValueError:
                    raise ValueError("invalid run report")
            if isinstance(details, str):
                try:
                    details = json.loads(details)
                except ValueError:
                    raise ValueError("invalid run details")
            if not isinstance(report, dict) \
                    or not isinstance(details, dict):
                raise ValueError("run report/details must be objects")
            if len(json.dumps(report)) > _RUN_PAYLOAD_MAX \
                    or len(json.dumps(details)) > _RUN_PAYLOAD_MAX:
                raise ValueError("run payload too large")
            evidence = details.get("evidence") or []
            if not isinstance(evidence, list) or len(evidence) > 500 \
                    or any(not isinstance(e, dict) for e in evidence):
                raise ValueError("invalid run evidence")
            ev_ids = []
            for e in evidence:
                eid = e.get("id")
                if eid is not None:
                    if not isinstance(eid, str) \
                            or not _HEX64_RE.fullmatch(eid):
                        raise ValueError("invalid run evidence id")
                    ev_ids.append(eid)
            for did in [*src, *ev_ids]:
                row = conn.execute(
                    "SELECT repo_id FROM memory_documents WHERE id=?",
                    (did,)).fetchone()
                if row is None:
                    raise ValueError(
                        "workflow run cites unknown document")
                if run.get("repo_id") is not None \
                        and row["repo_id"] != run["repo_id"]:
                    raise ValueError(
                        "workflow run cites foreign document")
                if run.get("repo_id") is None \
                        and row["repo_id"] not in known_repos:
                    raise ValueError(
                        "workflow run cites foreign workspace")
            # Imported runs are claims, not local observations: the
            # stored status is always 'imported' and the claimed status
            # is preserved alongside.  Process-ownership bookkeeping
            # (run_owner and the legacy runner/pid marker) is never
            # trusted from a bundle.
            details = _strip_run_details(details)
            details["imported_status"] = run["status"]
            conn.execute(
                "INSERT OR IGNORE INTO workflow_runs(id,kind,repo_id,"
                "status,created_at,updated_at,source_ids,report,"
                "details) VALUES(?,?,?,?,?,?,?,?,?)",
                (run["id"], run["kind"], run.get("repo_id"),
                 "imported",
                 norm_ts(run.get("created_at"), "run timestamp"),
                 norm_ts(run.get("updated_at"), "run timestamp"),
                 canonical_json(src),
                 canonical_json(redact(report)),
                 canonical_json(redact(details))))
        for rid, inc in incoming_idx.items():
            row = conn.execute(
                "SELECT indexed_at FROM repository_indexes"
                " WHERE repo_id=?", (rid,)).fetchone()
            if row is None or inc["indexed_at"] > row["indexed_at"]:
                conn.execute(
                    "INSERT OR REPLACE INTO repository_indexes("
                    "repo_id,commit_sha,indexed_at) VALUES(?,?,?)",
                    (rid, inc["commit_sha"], inc["indexed_at"]))
        for d in docs:
            if d["kind"] != "decision":
                continue
            row = conn.execute(
                "SELECT repo_id FROM decisions WHERE id=?",
                (d["source_id"],)).fetchone()
            if row is None or row["repo_id"] != d["repo_id"]:
                raise ValueError(
                    "decision document references unknown decision")

    @staticmethod
    def _export_attribution(conn, checkpoint_ids) -> list[dict]:
        if not checkpoint_ids:
            return []
        marks = ",".join("?" for _ in checkpoint_ids)
        rows = conn.execute(
            "SELECT checkpoint_id,report FROM checkpoint_attribution"
            f" WHERE checkpoint_id IN ({marks})",
            checkpoint_ids).fetchall()
        return [{"checkpoint_id": r["checkpoint_id"],
                 "report": json.loads(r["report"])} for r in rows]

    @staticmethod
    def _export_native(conn, session_ids) -> list[dict]:
        if not session_ids:
            return []
        marks = ",".join("?" for _ in session_ids)
        rows = conn.execute(
            "SELECT session_id,agent,native_id,format,archive,"
            "registered_at,source FROM native_sessions"
            f" WHERE session_id IN ({marks})",
            session_ids).fetchall()
        return [{
            "session_id": r["session_id"], "agent": r["agent"],
            "native_id": r["native_id"], "format": r["format"],
            "archive": r["archive"],
            "registered_at": r["registered_at"],
            "source": r["source"],
        } for r in rows]

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
                "attribution": self._export_attribution(conn, cp_ids),
                "native_sessions": self._export_native(conn, sess_ids),
                "memory": self._export_memory(conn, repo_ids),
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
                     "checkpoints", "links", "attribution",
                     "native_sessions"):
            seq = bundle.get(name)
            if seq is None:
                continue
            if not isinstance(seq, list) or len(seq) > 1_000_000:
                raise ValueError(f"bundle.{name} must be a bounded list")
            if not all(isinstance(x, dict) for x in seq):
                raise ValueError(f"bundle.{name} entries must be objects")
        if bundle.get("memory") is not None \
                and not isinstance(bundle["memory"], dict):
            raise ValueError("bundle.memory must be an object")
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
                session_native = {
                    row["id"]: row["native_id"]
                    for row in conn.execute(
                        "SELECT id,native_id FROM sessions")
                }
                for n in bundle.get("native_sessions") or []:
                    nsid = n.get("session_id")
                    agent = n.get("agent")
                    nid = n.get("native_id")
                    fmt = n.get("format")
                    if not isinstance(nsid, str) \
                            or nsid not in session_repo:
                        raise ValueError(
                            "native session references unknown session")
                    if not isinstance(agent, str) \
                            or agent not in NATIVE_AGENTS:
                        raise ValueError("invalid native agent")
                    if not isinstance(nid, str) \
                            or not _NATIVE_ID_RE.fullmatch(nid):
                        raise ValueError("invalid native_id")
                    if session_agent.get(nsid) != agent \
                            or session_native.get(nsid) != nid:
                        raise ValueError(
                            "native identity does not match session")
                    if fmt != "native-id" \
                            and fmt != NATIVE_FORMAT_FOR[agent]:
                        raise ValueError("invalid native format")
                    archive = n.get("archive")
                    if archive is not None and (
                            not isinstance(archive, str)
                            or len(archive) > 8 * 1024 * 1024):
                        raise ValueError("invalid native archive")
                    if archive is not None:
                        from .native import validate_native_text
                        afmt, archive = validate_native_text(
                            agent, nid, archive)
                        if afmt != fmt:
                            raise ValueError(
                                "native archive format mismatch")
                    registered = _norm_ts_opt(
                        n.get("registered_at"),
                        "native registered_at") or now_iso()
                    existing = conn.execute(
                        "SELECT local_path,archive,registered_at"
                        " FROM native_sessions WHERE session_id=?",
                        (nsid,)).fetchone()
                    if existing is None:
                        conn.execute(
                            "INSERT INTO native_sessions(session_id,"
                            "agent,native_id,format,local_path,archive,"
                            "registered_at,source)"
                            " VALUES(?,?,?,?,NULL,?,?,?)",
                            (nsid, agent, nid, fmt, archive,
                             registered, "imported-claim"))
                    elif registered > (existing["registered_at"] or ""):
                        conn.execute(
                            "UPDATE native_sessions SET format=?,"
                            " archive=COALESCE(?,archive),"
                            " registered_at=?, source='imported-claim'"
                            " WHERE session_id=?",
                            (fmt, archive, registered, nsid))
                for a in bundle.get("attribution") or []:
                    rebuilt = _validate_attribution(
                        conn, a, known_cps, session_repo)
                    conn.execute(
                        "INSERT OR IGNORE INTO checkpoint_attribution("
                        "checkpoint_id,report) VALUES(?,?)",
                        (a["checkpoint_id"], canonical_json(rebuilt)))
                if bundle.get("memory") is not None:
                    self._import_memory(
                        conn, bundle["memory"], known_repos)
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
