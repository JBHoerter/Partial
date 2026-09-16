from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

from . import attribution as core
from .models import (
    canonical_json,
    now_iso,
    scoped_session_id,
    sha256_hex,
)
from .privacy import is_sensitive_path
from .store import MUTATING_TOOLS, _PATH_KEYS, Store, repo_id_for

MAX_PATHS = 128
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$")
_PATCH_MOVE_RE = re.compile(r"^\*\*\* Move to: (.+)$")
_IGNORE_DIRS = frozenset(
    {".git", ".devin", ".claude", ".codex", "node_modules", "vendor"})
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, timeout=15)


def _git_text(root: Path, *args: str) -> str:
    return _git(root, *args).stdout.decode("utf-8", "replace").strip()


def _head(root: Path) -> str | None:
    proc = _git(root, "rev-parse", "--verify", "HEAD")
    if proc.returncode != 0:
        return None
    return proc.stdout.decode().strip()


def _repo_id(repo: dict) -> str:
    if repo.get("id"):
        return repo["id"]
    return repo_id_for(
        repo["root"], repo.get("common_dir") or "",
        repo.get("remote") or "")


def _call_key(agent: str, payload: dict) -> str:
    for k in ("tool_use_id", "tool_call_id"):
        v = payload.get(k)
        if isinstance(v, str) and v:
            return v
    return sha256_hex(canonical_json({
        "agent": agent,
        "session_id": payload.get("session_id"),
        "prompt_id": payload.get("prompt_id"),
        "tool_name": payload.get("tool_name"),
        "tool_input": payload.get("tool_input"),
    }))


def _tool_paths(payload: dict) -> list[str]:
    name = str(payload.get("tool_name") or "").lower()
    ti = payload.get("tool_input")
    out: list[str] = []
    if name in MUTATING_TOOLS and isinstance(ti, dict):
        for key in _PATH_KEYS:
            v = ti.get(key)
            if isinstance(v, str) and v:
                out.append(v)
        v = ti.get("paths")
        if isinstance(v, list):
            out += [x for x in v if isinstance(x, str) and x]
        if name == "apply_patch":
            for field in ("patch", "input", "command"):
                text = ti.get(field)
                if not isinstance(text, str):
                    continue
                for ln in text.splitlines():
                    m = _PATCH_FILE_RE.match(ln) or _PATCH_MOVE_RE.match(
                        ln)
                    if m:
                        out.append(m.group(1).strip())
    seen: set[str] = set()
    res = []
    for p in out:
        if p not in seen:
            seen.add(p)
            res.append(p)
    return res[:MAX_PATHS]


def _resolve(root: Path, rel: str) -> tuple[tuple, str] | None:
    if not isinstance(rel, str) or not rel or "\x00" in rel \
            or len(rel) > 4096:
        return None
    cand = Path(rel)
    if not cand.is_absolute():
        cand = root / cand
    try:
        cand_abs = Path(os.path.abspath(cand))
        parts = cand_abs.relative_to(root).parts
    except (OSError, ValueError):
        return None
    if not parts:
        return None
    s = "/".join(parts)
    if not s or s == "." or s.startswith("../") or s == "..":
        return None
    if any(seg in _IGNORE_DIRS for seg in parts):
        return None
    if is_sensitive_path(s):
        return None
    cur = root
    for part in parts:
        cur = cur / part
        try:
            if cur.is_symlink():
                return None
        except OSError:
            return None
    try:
        st = cand_abs.lstat()
        if not stat.S_ISREG(st.st_mode):
            return None
    except OSError:
        pass
    return parts, s


def _read_parts(root: Path, parts) -> bytes | None:
    try:
        fd = os.open(
            str(root), os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    except OSError:
        return None
    try:
        for part in parts[:-1]:
            try:
                nfd = os.open(
                    part, os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                    dir_fd=fd)
            except FileNotFoundError:
                return b""
            except OSError:
                return None
            os.close(fd)
            fd = nfd
        try:
            ffd = os.open(
                parts[-1], os.O_RDONLY | _NOFOLLOW, dir_fd=fd)
        except FileNotFoundError:
            return b""
        except OSError:
            return None
        try:
            st = os.fstat(ffd)
            if not stat.S_ISREG(st.st_mode) \
                    or st.st_size > core.MAX_BYTES:
                return None
            with os.fdopen(ffd, "rb") as f:
                ffd = -1
                data = f.read(core.MAX_BYTES + 1)
            if len(data) > core.MAX_BYTES:
                return None
            return data
        finally:
            if ffd >= 0:
                os.close(ffd)
    except OSError:
        return None
    finally:
        os.close(fd)


def _read_current(root: Path, parts) -> bytes | None:
    return _read_parts(root, parts)


def _base_blob(root: Path, commit: str, rel: str) -> bytes | None:
    proc = _git(root, "ls-tree", commit, "--", rel)
    if proc.returncode != 0:
        return None
    entry = proc.stdout.decode("utf-8", "replace").strip()
    if not entry:
        return b""
    mode = entry.split(None, 1)[0]
    if mode in ("120000", "160000"):
        return None
    size_s = _git_text(root, "cat-file", "-s", f"{commit}:{rel}")
    if not size_s.isdigit():
        return None
    if int(size_s) > core.MAX_BYTES:
        return None
    proc = _git(root, "show", f"{commit}:{rel}")
    if proc.returncode != 0:
        return None
    return proc.stdout


def _finger(data: bytes) -> list[str] | None:
    try:
        return core.fingerprint(data)
    except (ValueError, UnicodeDecodeError):
        return None


def _tool_ok(payload: dict) -> bool:
    resp = payload.get("tool_response")
    if isinstance(resp, dict):
        if resp.get("success") is False or resp.get("is_error") is True:
            return False
    return True


class Provenance:
    def __init__(self, store: Store):
        self.store = store

    def _state_row(self, conn, repo_id: str, worktree: str,
                   path: str) -> dict | None:
        row = conn.execute(
            "SELECT state FROM attribution_files WHERE repo_id=?"
            " AND worktree=? AND path=?",
            (repo_id, worktree, path)).fetchone()
        return json.loads(row["state"]) if row else None

    def _save_state(self, conn, repo_id: str, worktree: str,
                    path: str, base_commit: str | None,
                    state: dict) -> None:
        conn.execute(
            "INSERT INTO attribution_files(repo_id,worktree,path,"
            "base_commit,state) VALUES(?,?,?,?,?)"
            " ON CONFLICT(repo_id,worktree,path)"
            " DO UPDATE SET base_commit=excluded.base_commit,"
            " state=excluded.state",
            (repo_id, worktree, path, base_commit,
             canonical_json(state)))

    def _has_pending(self, conn, repo_id: str, worktree: str,
                     path: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM attribution_pending WHERE repo_id=?"
            " AND worktree=? AND path=?",
            (repo_id, worktree, path)).fetchone() is not None

    def before_tool(self, repo: dict, agent: str, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        native = payload.get("session_id")
        if not isinstance(native, str) or not native:
            return
        root = Path(repo["root"]).resolve()
        worktree = str(root)
        repo_id = _repo_id(repo)
        head = _head(root)
        key = _call_key(agent, payload)
        paths = _tool_paths(payload)
        if not paths:
            return
        conn = self.store._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for rel in paths:
                r = _resolve(root, rel)
                if r is None:
                    continue
                parts, rel_s = r
                base = b"" if head is None else _base_blob(
                    root, head, rel_s)
                cur = _read_current(root, parts)
                if base is None or cur is None:
                    continue
                base_h = _finger(base)
                cur_h = _finger(cur)
                if base_h is None or cur_h is None:
                    continue
                state = self._state_row(conn, repo_id, worktree, rel_s)
                if state is None:
                    state = core.new_state(base_h, cur_h)
                else:
                    if state.get("base") != base_h:
                        state = core.rebase(state, base_h)
                    if state["current"] != cur_h:
                        if self._has_pending(
                                conn, repo_id, worktree, rel_s):
                            state = core.advance(
                                state, cur_h, kind="unknown",
                                evidence="overlap")
                        else:
                            state = core.advance(
                                state, cur_h, kind="human",
                                evidence="external")
                self._save_state(
                    conn, repo_id, worktree, rel_s, head, state)
                exists = conn.execute(
                    "SELECT 1 FROM attribution_pending WHERE repo_id=?"
                    " AND worktree=? AND session_id=? AND call_key=?"
                    " AND path=?",
                    (repo_id, worktree, native, key, rel_s)).fetchone()
                if exists:
                    conn.execute(
                        "UPDATE attribution_pending SET ambiguous=1"
                        " WHERE repo_id=? AND worktree=?"
                        " AND session_id=? AND call_key=? AND path=?",
                        (repo_id, worktree, native, key, rel_s))
                else:
                    conn.execute(
                        "INSERT INTO attribution_pending(repo_id,"
                        "worktree,session_id,call_key,path,"
                        "before_hashes,revision,created_at,ambiguous)"
                        " VALUES(?,?,?,?,?,?,?,?,0)",
                        (repo_id, worktree, native, key, rel_s,
                         canonical_json(state["current"]),
                         int(state.get("revision", 0)), now_iso()))
            conn.commit()
        finally:
            conn.close()

    def after_tool(self, repo: dict, agent: str, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        native = payload.get("session_id")
        if not isinstance(native, str) or not native:
            return
        root = Path(repo["root"]).resolve()
        worktree = str(root)
        repo_id = _repo_id(repo)
        key = _call_key(agent, payload)
        paths = _tool_paths(payload)
        sid = scoped_session_id(repo_id, agent, native)
        conn = self.store._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            ok = _tool_ok(payload)
            for rel in paths:
                r = _resolve(root, rel)
                if r is None:
                    continue
                parts, rel_s = r
                cur = _read_current(root, parts)
                if cur is None:
                    continue
                cur_h = _finger(cur)
                if cur_h is None:
                    continue
                pend = conn.execute(
                    "SELECT before_hashes,revision,ambiguous"
                    " FROM attribution_pending WHERE repo_id=?"
                    " AND worktree=? AND session_id=? AND call_key=?"
                    " AND path=?",
                    (repo_id, worktree, native, key, rel_s)).fetchone()
                state = self._state_row(conn, repo_id, worktree, rel_s)
                if not ok:
                    if state is not None \
                            and state["current"] != cur_h:
                        state = core.advance(
                            state, cur_h, kind="unknown",
                            evidence="unobserved")
                        self._save_state(conn, repo_id, worktree,
                                         rel_s, _head(root), state)
                    conn.execute(
                        "DELETE FROM attribution_pending"
                        " WHERE repo_id=? AND worktree=?"
                        " AND session_id=? AND call_key=? AND path=?",
                        (repo_id, worktree, native, key, rel_s))
                    continue
                if state is None:
                    head = _head(root)
                    base = b"" if head is None else _base_blob(
                        root, head, rel_s)
                    base_h = _finger(base) if base is not None else []
                    if base_h is None:
                        continue
                    state = core.new_state(base_h)
                    state = core.advance(
                        state, cur_h, kind="unknown",
                        evidence="unobserved")
                elif pend is not None and not pend["ambiguous"] and \
                        pend["revision"] == state.get("revision"):
                    state = core.advance(
                        state, cur_h, kind="agent", session_id=sid,
                        evidence="tool-pair",
                        expected_before=json.loads(
                            pend["before_hashes"]))
                elif pend is not None:
                    state = core.advance(
                        state, cur_h, kind="unknown",
                        evidence="overlap")
                elif state["current"] != cur_h:
                    state = core.advance(
                        state, cur_h, kind="unknown",
                        evidence="unobserved")
                self._save_state(
                    conn, repo_id, worktree, rel_s, _head(root), state)
                conn.execute(
                    "DELETE FROM attribution_pending WHERE repo_id=?"
                    " AND worktree=? AND session_id=? AND call_key=?"
                    " AND path=?",
                    (repo_id, worktree, native, key, rel_s))
                conn.execute(
                    "INSERT OR IGNORE INTO pending_paths(repo_id,"
                    "session_id,path,worktree,created_at)"
                    " VALUES(?,?,?,?,?)",
                    (repo_id, sid, rel_s, worktree, now_iso()))
            conn.commit()
        finally:
            conn.close()

    def checkpoint(self, repo: dict, checkpoint: dict) -> dict:
        existing = self.get(checkpoint["id"])
        if existing is not None:
            return existing
        root = Path(repo["root"]).resolve()
        worktree = str(root)
        repo_id = _repo_id(repo)
        sha = checkpoint["commit_sha"]
        parent = _git_text(root, "rev-parse", "--verify", sha + "^")
        head = _head(root)
        at_head = head == sha
        files_out: list[dict] = []
        excluded: list[dict] = []
        limitations: list[str] = []
        conn = self.store._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for rel in checkpoint["files"]:
                reason = None
                parts = Path(rel).parts
                if is_sensitive_path(rel) or any(
                        p in _IGNORE_DIRS for p in parts):
                    reason = "sensitive-path"
                committed = base = None
                base_h = committed_h = None
                if reason is None:
                    committed = _base_blob(root, sha, rel)
                    if committed is None:
                        reason = "binary-or-oversize"
                    else:
                        committed_h = _finger(committed)
                        if committed_h is None:
                            reason = "binary-or-oversize"
                if reason is None:
                    base = b"" if not parent else _base_blob(
                        root, parent, rel)
                    if base is None:
                        reason = "binary-or-oversize"
                    else:
                        base_h = _finger(base)
                        if base_h is None:
                            reason = "binary-or-oversize"
                if reason is not None:
                    excluded.append({"path": rel, "reason": reason})
                    continue
                if not at_head:
                    state = core.new_state(base_h, committed_h)
                else:
                    state = self._state_row(
                        conn, repo_id, worktree, rel)
                    if state is None:
                        state = core.new_state(base_h, committed_h)
                    else:
                        if state.get("base") != base_h:
                            state = core.rebase(state, base_h)
                        r = _resolve(root, rel)
                        if r is not None:
                            cur = _read_current(root, r[0])
                            cur_h = (_finger(cur)
                                     if cur is not None else None)
                            if cur_h is not None \
                                    and cur_h != state["current"]:
                                state = core.advance(
                                    state, cur_h, kind="human",
                                    evidence="external")
                rep = core.report(state, committed_h)
                limitations = rep["limitations"]
                files_out.append({
                    "path": rel, "lines": rep["lines"],
                    "summary": rep["summary"]})
                if at_head:
                    self._save_state(
                        conn, repo_id, worktree, rel, sha,
                        core.rebase(state, committed_h))
            summary = core.aggregate(files_out)
            report = {
                "version": 1, "method": core.METHOD,
                "capture_source": "local-observation",
                "summary": summary, "files": files_out,
                "excluded": excluded, "limitations": limitations,
            }
            conn.execute(
                "INSERT OR IGNORE INTO checkpoint_attribution("
                "checkpoint_id,report) VALUES(?,?)",
                (checkpoint["id"], canonical_json(report)))
            conn.commit()
            return report
        finally:
            conn.close()

    def get(self, checkpoint_id: str) -> dict | None:
        conn = self.store._connect()
        try:
            row = conn.execute(
                "SELECT report FROM checkpoint_attribution"
                " WHERE checkpoint_id=?", (checkpoint_id,)).fetchone()
            return json.loads(row["report"]) if row else None
        finally:
            conn.close()
