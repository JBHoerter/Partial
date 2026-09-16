from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    Event,
    canonical_json,
    normalize_timestamp,
    now_iso,
    scoped_session_id,
)
from .privacy import is_sensitive_path, redact
from .store import (
    NATIVE_AGENTS,
    NATIVE_FORMAT_FOR,
    Store,
    repo_id_for,
)

NATIVE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
MAX_NATIVE_FILE = 8 * 1024 * 1024
_BLOCKED_NAMES = frozenset(
    {"auth.json", ".env", "credentials", "credentials.json",
     "config.json", ".netrc"})
_CODEX_RECORD_TYPES = frozenset(
    {"session_meta", "response_item", "event_msg", "turn_context",
     "compacted"})
_DROP_BLOCK_TYPES = frozenset({"reasoning", "thinking"})
_SCRUB_KEYS = frozenset(
    {"encrypted_content", "reasoning_content", "thinking",
     "authorization", "cookie", "set-cookie", "x-api-key"})
_CODEX_POLICY_KEYS = frozenset(
    {"sandbox_policy", "approval_policy", "permissions",
     "permission_profile", "writable_roots", "network_access"})
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def _repo_id(repo: dict) -> str:
    if repo.get("id"):
        return repo["id"]
    return repo_id_for(
        repo["root"], repo.get("common_dir") or "",
        repo.get("remote") or "")


def agent_argv(agent: str, native_id: str,
               settings: Path | None = None) -> list[str]:
    if agent == "devin":
        return ["devin", "--resume", native_id]
    if agent == "claude":
        argv = ["claude", "-r", native_id]
        if settings is not None:
            argv += ["--settings", str(settings)]
        return argv
    if agent == "codex":
        return ["codex", "resume", native_id]
    raise ValueError(f"no native resume command for {agent!r}")


def _check_native_id(native_id: str) -> None:
    if not isinstance(native_id, str) \
            or not NATIVE_ID_RE.fullmatch(native_id):
        raise ValueError("invalid native session id")


_DROP = object()


def _scrub(obj):
    if isinstance(obj, dict):
        if isinstance(obj.get("type"), str) \
                and obj["type"] in _DROP_BLOCK_TYPES:
            return _DROP
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _SCRUB_KEYS:
                continue
            sv = _scrub(v)
            if sv is not _DROP:
                out[k] = sv
        return out
    if isinstance(obj, list):
        kept = []
        for x in obj:
            sv = _scrub(x)
            if sv is _DROP:
                continue
            kept.append(sv)
        return kept
    return obj


def _norm_ts_field(value, name: str) -> str | None:
    if value is None:
        return None
    try:
        return normalize_timestamp(value)
    except ValueError as exc:
        raise ValueError(f"invalid native {name}") from exc


def validate_native_text(agent: str, native_id: str,
                         text: str) -> tuple[str, str]:
    if agent not in NATIVE_AGENTS:
        raise ValueError(f"unsupported native agent: {agent!r}")
    _check_native_id(native_id)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("native content is empty")
    if len(text.encode("utf-8", "replace")) > MAX_NATIVE_FILE:
        raise ValueError("native file exceeds 8 MiB")
    if agent == "claude":
        if not _UUID_RE.fullmatch(native_id):
            raise ValueError(
                "Claude native records require a UUID session id")
        seen: set = set()
        messages = 0
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("Claude transcript lines must be objects")
            sid = obj.get("sessionId")
            if isinstance(sid, str) and sid:
                seen.add(sid)
            mtype = obj.get("type")
            msg = obj.get("message")
            role = (msg.get("role") if isinstance(msg, dict)
                    else None) or mtype
            if role in ("user", "assistant"):
                messages += 1
            scrubbed = _scrub(obj)
            if scrubbed is not _DROP:
                out.append(canonical_json(redact(scrubbed)))
        if not seen or seen != {native_id}:
            raise ValueError(
                "Claude transcript sessionId does not match native id")
        if not messages:
            raise ValueError(
                "Claude transcript has no user/assistant records")
        return "claude-jsonl", "\n".join(out) + "\n"
    if agent == "codex":
        if not _UUID_RE.fullmatch(native_id):
            raise ValueError(
                "Codex native files require a UUID session id")
        out = []
        first = True
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("Codex rollout lines must be objects")
            rtype = obj.get("type")
            if rtype not in _CODEX_RECORD_TYPES:
                raise ValueError(
                    f"unsupported Codex rollout record type: {rtype!r}")
            if first and rtype != "session_meta":
                raise ValueError(
                    "not a resumable Codex rollout"
                    " (missing session_meta record)")
            first = False
            payload = obj.get("payload")
            if rtype == "session_meta":
                if not isinstance(payload, dict) \
                        or payload.get("id") != native_id:
                    raise ValueError(
                        "Codex session_meta does not match native id")
                cwd = payload.get("cwd")
                if not isinstance(cwd, str) or len(cwd) > 4096:
                    raise ValueError("Codex session_meta lacks cwd")
                _norm_ts_field(payload.get("timestamp"),
                               "session_meta timestamp")
            scrubbed = _scrub(obj)
            if scrubbed is not _DROP:
                out.append(canonical_json(redact(scrubbed)))
        if not out:
            raise ValueError("empty Codex rollout")
        return "codex-rollout", "\n".join(out) + "\n"
    obj = json.loads(text)
    if not isinstance(obj, dict) \
            or not isinstance(obj.get("steps"), list):
        raise ValueError("Devin native file must be an ATIF export")
    sid = obj.get("session_id")
    if sid is not None and sid != native_id:
        raise ValueError("ATIF session_id does not match native id")
    scrubbed = _scrub(obj)
    if scrubbed is _DROP:
        raise ValueError("ATIF export contains no usable content")
    return "devin-atif", canonical_json(redact(scrubbed))


def _read_native_file(agent: str, path) -> str:
    p = Path(path)
    if is_sensitive_path(str(p)) or p.name.lower() in _BLOCKED_NAMES:
        raise ValueError("refusing sensitive file path")
    if p.suffix.lower() not in (".json", ".jsonl"):
        raise ValueError("native file must be a .json or .jsonl file")
    ap = Path(os.path.abspath(p))
    parts = ap.parts
    if not parts or parts[0] != os.sep:
        raise ValueError("native file path must be absolute")
    try:
        fd = os.open(os.sep, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    except OSError as exc:
        raise ValueError(f"cannot read native file: {exc}") from exc
    try:
        for part in parts[1:-1]:
            try:
                nfd = os.open(
                    part, os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                    dir_fd=fd)
            except OSError as exc:
                raise ValueError(
                    f"cannot read native file: {exc}") from exc
            os.close(fd)
            fd = nfd
        try:
            ffd = os.open(
                parts[-1], os.O_RDONLY | _NOFOLLOW, dir_fd=fd)
        except OSError as exc:
            raise ValueError(
                f"cannot read native file: {exc}") from exc
        try:
            st = os.fstat(ffd)
            if not stat.S_ISREG(st.st_mode) \
                    or st.st_size > MAX_NATIVE_FILE:
                raise ValueError(
                    "native file must be a regular file <= 8 MiB")
            with os.fdopen(ffd, "rb") as f:
                ffd = -1
                data = f.read(MAX_NATIVE_FILE + 1)
            if len(data) > MAX_NATIVE_FILE:
                raise ValueError("native file exceeds 8 MiB")
        finally:
            if ffd >= 0:
                os.close(ffd)
    finally:
        os.close(fd)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"cannot decode native file: {exc}") from exc


def register_native(store: Store, repo: dict, agent: str,
                    native_id: str, *, path=None,
                    archive: bool = False, source: str = "explicit",
                    transcript_path=None) -> dict | None:
    if agent not in NATIVE_AGENTS:
        raise ValueError(f"unsupported native agent: {agent!r}")
    _check_native_id(native_id)
    fmt = "native-id"
    text = None
    local_path = None
    if path is not None:
        raw = _read_native_file(agent, path)
        fmt, sanitized = validate_native_text(agent, native_id, raw)
        if fmt != NATIVE_FORMAT_FOR[agent]:
            raise ValueError("native file format mismatch")
        text = sanitized
        local_path = str(Path(path).resolve())
    elif isinstance(transcript_path, str) and transcript_path \
            and len(transcript_path) <= 4096:
        local_path = transcript_path
        if agent in ("claude", "codex"):
            fmt = NATIVE_FORMAT_FOR[agent]
    sid = scoped_session_id(_repo_id(repo), agent, native_id)
    if store.get_session_meta(sid) is None:
        if source == "hook":
            return None
        store.ingest(_repo_id(repo), [Event(
            id=f"{sid}:native-register",
            session_id=native_id, agent=agent, kind="session_start",
            timestamp=now_iso(),
            data={"native_id": native_id, "source": source},
        )], worktree=repo.get("root"))
    return store.upsert_native(
        sid, agent, native_id, fmt, local_path=local_path,
        archive=text if archive else None, source=source)


def _resolve_session(store: Store, session_id: str) -> dict:
    if not isinstance(session_id, str) or not session_id \
            or len(session_id) > 512 or "\x00" in session_id:
        raise ValueError("invalid session id")
    row = store.get_session(session_id)
    if row is None:
        raise ValueError(f"unknown session: {session_id}")
    return row


def _latest_checkpoint(store: Store, session_id: str) -> dict | None:
    conn = store._connect()
    try:
        row = conn.execute(
            "SELECT c.id,c.commit_sha FROM checkpoints c"
            " JOIN checkpoint_links l ON l.checkpoint_id=c.id"
            " WHERE l.session_id=?"
            " ORDER BY c.created_at DESC,c.id LIMIT 1",
            (session_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def resume_plan(store: Store, session_id: str,
                *, repo=None) -> dict:
    sess = _resolve_session(store, session_id)
    native = store.get_native(sess["id"])
    if native is None:
        raise ValueError("session has no native registration")
    agent = native["agent"]
    warnings: list[str] = []
    settings = None
    if agent == "claude":
        try:
            from .git import claude_settings_path
            repo_row = store.get_repo(sess["repo_id"])
            if repo_row and repo_row.get("root"):
                settings = claude_settings_path({"root": repo_row["root"]})
                if not settings.exists():
                    settings = None
        except Exception:
            settings = None
    argv = agent_argv(agent, native["native_id"], settings)
    file_present = False
    local_path = native.get("local_path")
    if local_path:
        file_present = Path(local_path).is_file()
    available = None
    if agent == "devin":
        warnings.append(
            "Devin native state lives in the machine-local devin"
            " registry; resumable only on the original machine")
    elif native["format"] == "native-id":
        warnings.append("requires-local-native-state")
    elif not file_present:
        available = False
        warnings.append("requires-local-native-state")
    elif _provider_path(agent, local_path):
        available = True
    else:
        warnings.append(
            "registered file is present but not at the provider's"
            " native location; provider state unverified")
    cp = _latest_checkpoint(store, sess["id"])
    if shutil.which(argv[0]) is None:
        warnings.append(f"{argv[0]} executable not found on PATH")
    return {
        "session_id": sess["id"], "agent": agent,
        "native_id": native["native_id"], "format": native["format"],
        "argv": argv, "mode": "plan",
        "native_available": available,
        "file_present": file_present,
        "checkpoint_id": cp["id"] if cp else None,
        "commit_sha": cp["commit_sha"] if cp else None,
        "warnings": warnings,
    }


def _provider_path(agent: str, local_path: str) -> bool:
    try:
        p = Path(local_path).resolve()
    except OSError:
        return False
    if agent == "codex":
        home = Path(
            os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    elif agent == "claude":
        home = Path.home() / ".claude"
    else:
        return False
    try:
        p.relative_to(home.resolve())
        return True
    except (ValueError, OSError):
        return False


def _mangle(project: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", project)


def _mkdir_nofollow(base: Path, parts) -> int:
    fd = os.open(str(base), os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            nfd = os.open(
                part, os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write_excl_dirfd(dirfd: int, name: str, data: bytes) -> Path:
    try:
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600, dir_fd=dirfd)
    except FileExistsError as exc:
        raise ValueError(
            f"refusing to overwrite existing native file: {name}"
        ) from exc
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _session_project(store: Store, sess: dict,
                     target_root: Path, override=None) -> str:
    if override:
        return str(override)
    if sess.get("worktree"):
        return str(sess["worktree"])
    repo = store.get_repo(sess["repo_id"])
    if repo and repo.get("root"):
        return str(repo["root"])
    return str(target_root)


def restore_native(store: Store, session_id: str,
                   *, target_root: Path,
                   project: str | None = None) -> dict:
    sess = _resolve_session(store, session_id)
    native = store.get_native(sess["id"])
    if native is None:
        raise ValueError("session has no native registration")
    agent = native["agent"]
    nid = native["native_id"]
    if agent == "devin":
        raise ValueError(
            "Devin ATIF restore is unsupported; the ATIF export is"
            " portable context only. Use 'devin --resume ID' on the"
            " machine where the session ran.")
    if agent == "claude" and not _UUID_RE.fullmatch(nid):
        raise ValueError("invalid native id for restore")
    if agent == "codex" and not _UUID_RE.fullmatch(nid):
        raise ValueError("invalid native id for restore")
    archive = native.get("archive")
    if not isinstance(archive, str) or not archive:
        raise ValueError(
            "no native archive stored; register again with --archive")
    fmt, sanitized = validate_native_text(agent, nid, archive)
    if fmt != native["format"]:
        raise ValueError("native archive format mismatch")
    target_root = Path(target_root).resolve()
    if not target_root.is_dir():
        raise ValueError(f"restore target root missing: {target_root}")
    project = _session_project(store, sess, target_root, project)
    if agent == "claude":
        out_lines = []
        for line in sanitized.splitlines():
            obj = json.loads(line)
            if isinstance(obj, dict) and "cwd" in obj:
                obj["cwd"] = project
            out_lines.append(canonical_json(obj))
        data = ("\n".join(out_lines) + "\n").encode("utf-8")
        parts = (".claude", "projects", _mangle(project))
        name = f"{nid}.jsonl"
    elif agent == "codex":
        lines = sanitized.splitlines()
        first = json.loads(lines[0])
        ts_raw = first["payload"].get("timestamp")
        try:
            dt = datetime.fromisoformat(
                normalize_timestamp(ts_raw)).astimezone(timezone.utc)
        except ValueError:
            dt = datetime.now(timezone.utc)
        first["payload"]["cwd"] = project
        out_lines = [canonical_json(first)]
        for line in lines[1:]:
            obj = json.loads(line)
            if obj.get("type") == "turn_context" and isinstance(
                    obj.get("payload"), dict):
                for k in _CODEX_POLICY_KEYS:
                    obj["payload"].pop(k, None)
            out_lines.append(canonical_json(obj))
        data = ("\n".join(out_lines) + "\n").encode("utf-8")
        stamp = dt.strftime("%Y-%m-%dT%H-%M-%S")
        codex_home = os.environ.get("CODEX_HOME")
        if codex_home:
            base = Path(codex_home).resolve()
            if not base.is_dir():
                raise ValueError("CODEX_HOME is not a directory")
            parts = ("sessions", dt.strftime("%Y"), dt.strftime("%m"),
                     dt.strftime("%d"))
            dirfd = _mkdir_nofollow(base, parts)
        else:
            parts = (".codex", "sessions", dt.strftime("%Y"),
                     dt.strftime("%m"), dt.strftime("%d"))
            dirfd = _mkdir_nofollow(target_root, parts)
        name = f"rollout-{stamp}-{nid}.jsonl"
        try:
            _write_excl_dirfd(dirfd, name, data)
        finally:
            os.close(dirfd)
        dest = (base if codex_home else target_root) / Path(*parts) / name
        store.upsert_native(
            sess["id"], agent, nid, fmt, local_path=str(dest),
            archive=sanitized, source=native["source"])
        return {
            "session_id": sess["id"], "agent": agent,
            "native_id": nid, "format": fmt, "path": str(dest),
            "warnings": [],
        }
    else:
        raise ValueError(f"no restore support for {agent!r}")
    dirfd = _mkdir_nofollow(target_root, parts)
    try:
        _write_excl_dirfd(dirfd, name, data)
    finally:
        os.close(dirfd)
    dest = target_root / Path(*parts) / name
    store.upsert_native(
        sess["id"], agent, nid, fmt, local_path=str(dest),
        archive=sanitized, source=native["source"])
    return {
        "session_id": sess["id"], "agent": agent,
        "native_id": nid, "format": fmt, "path": str(dest),
        "warnings": [],
    }


def resume_session(store: Store, session_id: str, *, run: bool = False,
                   worktree=None, restore: bool = False,
                   trust_native_state: bool = False,
                   target_root=None) -> dict | int:
    sess = _resolve_session(store, session_id)
    plan = resume_plan(store, sess["id"])
    repo = store.get_repo(sess["repo_id"])
    native = store.get_native(sess["id"])
    if restore:
        if not trust_native_state:
            raise ValueError(
                "--restore-native requires --trust-native-state")
        if native is None:
            raise ValueError("session has no native registration")
        agent = native["agent"]
        nid = native["native_id"]
        if agent == "devin":
            raise ValueError(
                "Devin ATIF restore is unsupported; the ATIF export is"
                " portable context only. Use 'devin --resume ID' on"
                " the machine where the session ran.")
        if not _UUID_RE.fullmatch(nid):
            raise ValueError("invalid native id for restore")
        archive = native.get("archive")
        if not isinstance(archive, str) or not archive:
            raise ValueError(
                "no native archive stored; register again with"
                " --archive")
        fmt, _ = validate_native_text(agent, nid, archive)
        if fmt != native["format"]:
            raise ValueError("native archive format mismatch")
    if run:
        exe = plan["argv"][0]
        if shutil.which(exe) is None:
            raise FileNotFoundError(exe)
    if worktree is not None:
        if not plan.get("commit_sha"):
            raise ValueError(
                "session has no checkpoint commit to create a"
                " worktree from")
        wt = Path(worktree)
        if not wt.is_absolute():
            raise ValueError("--worktree path must be absolute")
        if wt.exists():
            raise ValueError(f"worktree path already exists: {wt}")
        if not repo or not repo.get("root"):
            raise ValueError("session repository root is unknown")
        proc = subprocess.run(
            ["git", "-C", repo["root"], "worktree", "add", "--detach",
             str(wt), plan["commit_sha"]],
            capture_output=True, timeout=60)
        if proc.returncode != 0:
            raise ValueError(
                "git worktree add failed:"
                f" {proc.stderr.decode('utf-8', 'replace').strip()}")
        plan["worktree"] = str(wt)
    if restore:
        out = restore_native(
            store, sess["id"],
            target_root=Path(target_root or Path.home()),
            project=str(worktree) if worktree else None)
        plan["restored"] = out
        refreshed = resume_plan(store, sess["id"])
        for k in ("native_available", "file_present", "warnings"):
            plan[k] = refreshed[k]
        plan["restored"] = out
    if not run:
        return plan
    cwd = str(worktree) if worktree is not None else None
    if cwd is None:
        cand = sess.get("worktree") or (
            repo.get("root") if repo else None)
        if cand and Path(cand).is_dir():
            cwd = cand
    if cwd is None:
        raise ValueError("no existing local path to run the agent in")
    from .git import discover_repo, repo_id_for_repo
    try:
        discovered = repo_id_for_repo(discover_repo(cwd))
    except Exception as exc:
        raise ValueError(
            f"cannot verify repository for resume run: {exc}") from exc
    if discovered != sess["repo_id"]:
        raise ValueError(
            "resume cwd does not belong to the session's repository")
    env = dict(os.environ)
    env["PARTIAL_HOME"] = str(Path(store.path.parent).resolve())
    return subprocess.call(plan["argv"], cwd=cwd, env=env)
