from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from pathlib import Path

from .models import canonical_json, now_iso
from .privacy import is_sensitive_path, redact
from .store import Store, repo_id_for, sanitize_remote

METADATA_REF = "refs/heads/partial/checkpoints/v1"
CHECKPOINT_DIR = "checkpoints"
DIFF_CAP = 2 * 1024 * 1024
HOOK_MARKER = "PARTIAL_MANAGED_HOOK=1"
CHECKPOINT_ID_RE = re.compile(r"[0-9a-f]{32}")
DEVIN_HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "Stop",
    "SessionEnd",
    "PostCompaction",
)
CLAUDE_HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "Stop",
    "SessionEnd",
)


class GitError(RuntimeError):
    pass


def hook_entry(agent: str, event: str) -> dict:
    return {
        "matcher": "",
        "hooks": [{
            "type": "command",
            "command": f"partial hook {agent} {event}",
            "timeout": 10,
        }],
    }


def _owned_commands(agent: str, events) -> set[str]:
    return {f"partial hook {agent} {e}" for e in events}


def _git(root, *args, input_bytes=None, env=None, timeout=30, check=True):
    cmd = ["git", "-C", str(root), *args]
    proc = subprocess.run(
        cmd,
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
        env=env,
    )
    if check and proc.returncode != 0:
        err = redact(proc.stderr.decode("utf-8", "replace").strip())
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}):"
                       f" {err}")
    return proc


def _git_out(root, *args, **kw) -> str:
    return _git(root, *args, **kw).stdout.decode("utf-8", "replace")


def discover_repo(path: str | Path = ".") -> dict:
    root = _git_out(
        path, "rev-parse", "--path-format=absolute", "--show-toplevel"
    ).strip()
    git_dir = _git_out(
        path, "rev-parse", "--path-format=absolute", "--git-dir"
    ).strip()
    common = _git_out(
        path, "rev-parse", "--path-format=absolute", "--git-common-dir"
    ).strip()
    proc = _git(path, "remote", "get-url", "origin", check=False)
    remote_raw = proc.stdout.decode().strip() if proc.returncode == 0 \
        else ""
    proc = _git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    branch = proc.stdout.decode().strip() if proc.returncode == 0 else ""
    if branch == "HEAD":
        branch = ""
    return {
        "root": root,
        "git_dir": git_dir,
        "common_dir": common,
        "remote": sanitize_remote(remote_raw),
        "id": repo_id_for(root, common, remote_raw),
        "name": Path(root).name,
        "branch": branch,
        "worktree": root,
    }


def repo_id_for_repo(repo: dict) -> str:
    if repo.get("id"):
        return repo["id"]
    return repo_id_for(repo["root"], repo["common_dir"], repo["remote"])


def _resolve_commit(root: str, ref: str) -> str:
    return _git_out(
        root, "rev-parse", "--verify", "--end-of-options",
        f"{ref}^{{commit}}",
    ).strip()


def _state_dir(repo: dict) -> Path:
    d = Path(repo["common_dir"]) / "partial"
    created = not d.exists()
    d.mkdir(parents=True, exist_ok=True)
    if created:
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    return d


def load_local_config(repo: dict) -> dict:
    cfg = Path(repo["common_dir"]) / "partial" / "config.json"
    try:
        return json.loads(cfg.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_local_config(repo: dict, cfg: dict) -> None:
    d = _state_dir(repo)
    (d / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")


def create_checkpoint(
    store: Store,
    repo_id: str,
    *,
    session_ids: list[str] | None = None,
    commit: str = "HEAD",
    worktree: str | None = None,
    require_links: bool = False,
) -> dict | None:
    repo = store.get_repo(repo_id)
    if repo is None:
        raise GitError(f"unknown repo_id: {repo_id}")
    if worktree:
        wt = discover_repo(worktree)
        if repo_id_for_repo(wt) != repo_id:
            raise GitError(
                f"worktree {worktree} does not belong to repository"
                f" {repo_id[:12]}")
        root = wt["root"]
    else:
        root = repo.get("root")
    if not root:
        raise GitError("repository root unavailable for checkpoint")
    resolved_wt = str(Path(root).resolve())
    sha = _resolve_commit(root, commit)
    proc = _git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    branch = proc.stdout.decode().strip()
    if branch == "HEAD":
        branch = None
    message = _git_out(root, "log", "-1", "--format=%B", sha).strip()
    author = _git_out(root, "log", "-1", "--format=%an <%ae>", sha).strip()
    raw = _git(
        root, "diff-tree", "--root", "--diff-merges=first-parent",
        "--no-commit-id", "-r", "--name-only", "-z", sha,
    ).stdout
    files: list[str] = []
    seen: set[str] = set()
    for f in raw.decode("utf-8", "replace").split("\0"):
        if f and f not in seen:
            seen.add(f)
            files.append(f)
    safe = [f for f in files if not is_sensitive_path(f)]
    diff_text = None
    if safe:
        proc = _git(
            root, "diff-tree", "--root", "--diff-merges=first-parent",
            "-p", "-r", "--no-ext-diff", "--no-textconv", sha, "--",
            *safe,
        )
        data = proc.stdout
        truncated = len(data) > DIFF_CAP
        if truncated:
            data = data[:DIFF_CAP]
        diff_text = data.decode("utf-8", "replace")
        if truncated:
            diff_text += "\n[partial: diff truncated at 2 MiB]\n"
        diff_text = redact(diff_text)
    links: list[tuple[str, str]] = [
        (store.resolve_session(repo_id, s), "explicit")
        for s in (session_ids or [])
    ]
    links += store.pending_links(repo_id, set(files), resolved_wt)
    if require_links and not links:
        return None
    checkpoint_id = uuid.uuid4().hex
    return store.save_checkpoint(
        repo_id, checkpoint_id, sha,
        branch=branch,
        message=str(redact(message)) if message else None,
        author=str(redact(author)) if author else None,
        files=files, diff=diff_text, links=links,
        worktree=resolved_wt,
    )


def _hooks_dir(repo: dict) -> Path:
    out = _git_out(repo["root"], "rev-parse", "--git-path", "hooks").strip()
    p = Path(out)
    if not p.is_absolute():
        p = Path(repo["root"]) / p
    resolved = p.resolve()
    allowed = [
        Path(repo["git_dir"]).resolve(),
        Path(repo["common_dir"]).resolve(),
    ]
    if not any(
        resolved == a or a in resolved.parents for a in allowed
    ):
        raise GitError(
            f"resolved hooks directory {resolved} is outside this"
            " repository's git dir (shared core.hooksPath?); refusing to"
            " modify shared hook path")
    return resolved


def _post_commit_script() -> str:
    return (
        "#!/bin/sh\n"
        f"{HOOK_MARKER}\n"
        "partial hook git post-commit"
        " || echo 'partial: post-commit hook failed' >&2\n"
        "exit 0\n"
    )


def check_git_hook(repo: dict) -> str | None:
    try:
        hooks = _hooks_dir(repo)
    except GitError as exc:
        return str(exc)
    target = hooks / "post-commit"
    if target.exists():
        try:
            content = target.read_text(errors="replace")
        except OSError as exc:
            return str(exc)
        if HOOK_MARKER not in content:
            return (
                f"existing post-commit hook at {target} is not managed by"
                " Partial; refusing to overwrite. Move it aside or chain"
                " 'partial hook git post-commit' from it manually.")
    return None


def install_hooks(repo: dict) -> dict:
    err = check_git_hook(repo)
    result = {"installed": [], "errors": []}
    if err:
        result["errors"].append(err)
        return result
    hooks = _hooks_dir(repo)
    hooks.mkdir(parents=True, exist_ok=True)
    target = hooks / "post-commit"
    target.write_text(_post_commit_script())
    os.chmod(target, 0o755)
    result["installed"].append("git:post-commit")
    cfg = load_local_config(repo)
    cfg["enabled"] = True
    save_local_config(repo, cfg)
    return result


def _hooks_v1_path(repo: dict) -> Path:
    return Path(repo["root"]) / ".devin" / "hooks.v1.json"


_HOOK_EVENT_KEYS = frozenset(DEVIN_HOOK_EVENTS) | frozenset(
    CLAUDE_HOOK_EVENTS)
_HOOK_FIELD_BY_TYPE = {"command": "command", "prompt": "prompt",
                       "http": "url", "url": "url"}


def _validate_hook_groups(data: object, ctx: str) -> dict:
    if not isinstance(data, dict):
        raise ValueError(f"{ctx}: expected a JSON object")
    for key, entries in data.items():
        if not isinstance(entries, list):
            if key in _HOOK_EVENT_KEYS:
                raise ValueError(
                    f"{ctx}: entry for {key!r} is not a"
                    " matcher/hooks group list")
            continue
        for g in entries:
            if not isinstance(g, dict) or not isinstance(
                    g.get("hooks"), list):
                raise ValueError(
                    f"{ctx}: entry for {key!r} is not a matcher/hooks"
                    " group")
            for h in g["hooks"]:
                if not isinstance(h, dict):
                    raise ValueError(
                        f"{ctx}: hook entry for {key!r} is not an"
                        " object")
                htype = h.get("type")
                field = _HOOK_FIELD_BY_TYPE.get(htype)
                if field is not None:
                    if not isinstance(h.get(field), str):
                        raise ValueError(
                            f"{ctx}: {htype} hook for {key!r} lacks a"
                            f" {field} string")
                elif not any(
                        isinstance(h.get(f), str)
                        for f in ("command", "prompt", "url")):
                    raise ValueError(
                        f"{ctx}: hook entry for {key!r} lacks a"
                        " command, prompt, or url")
    return data


def _load_devin_map(repo: dict) -> dict:
    path = _hooks_v1_path(repo)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON ({exc})") from exc
    return _validate_hook_groups(data, str(path))


def _merge_owned(entries: list, agent: str, event: str) -> bool:
    cmd = f"partial hook {agent} {event}"
    for g in entries:
        if not isinstance(g, dict):
            continue
        for h in g.get("hooks") or []:
            if isinstance(h, dict) and h.get("command") == cmd \
                    and h.get("type") == "command":
                return False
    entries.append(hook_entry(agent, event))
    return True


def check_agent_configs(repo: dict, agents: list[str]) -> list[str]:
    errors = []
    if "devin" in agents:
        try:
            _load_devin_map(repo)
        except ValueError as exc:
            errors.append(str(exc))
    if "claude" in agents:
        try:
            _load_claude_settings(repo)
        except ValueError as exc:
            errors.append(str(exc))
    return errors


def install_devin_hooks(repo: dict) -> dict:
    data = _load_devin_map(repo)
    installed = []
    for event in DEVIN_HOOK_EVENTS:
        entries = data.get(event)
        if not isinstance(entries, list):
            entries = []
        if _merge_owned(entries, "devin", event):
            installed.append(f"devin:{event}")
        data[event] = entries
    path = _hooks_v1_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return {"installed": installed, "errors": []}


def claude_settings_path(repo: dict) -> Path:
    return Path(repo["root"]) / ".devin" / "partial" / "claude-settings.json"


def _load_claude_settings(repo: dict) -> dict:
    path = claude_settings_path(repo)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    hooks = data.get("hooks")
    if hooks is not None:
        _validate_hook_groups(hooks, f"{path} hooks")
    return data


def install_claude_hooks(repo: dict) -> dict:
    data = _load_claude_settings(repo)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    installed = []
    for event in CLAUDE_HOOK_EVENTS:
        entries = hooks.get(event)
        if not isinstance(entries, list):
            entries = []
        if _merge_owned(entries, "claude", event):
            installed.append(f"claude:{event}")
        hooks[event] = entries
    data["hooks"] = hooks
    path = claude_settings_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return {"installed": installed, "errors": []}


def _strip_owned(entries: list, owned: set[str]) -> list:
    out = []
    for g in entries:
        if not isinstance(g, dict):
            out.append(g)
            continue
        hooks = g.get("hooks")
        if not isinstance(hooks, list):
            out.append(g)
            continue
        kept = [
            h for h in hooks
            if not (isinstance(h, dict)
                    and h.get("command") in owned
                    and h.get("type") == "command")
        ]
        if kept or not hooks:
            gg = dict(g)
            gg["hooks"] = kept
            out.append(gg)
    return out


def disable_hooks(repo: dict) -> None:
    try:
        hooks = _hooks_dir(repo)
        target = hooks / "post-commit"
        if target.exists():
            try:
                content = target.read_text(errors="replace")
            except OSError:
                content = ""
            if HOOK_MARKER in content:
                target.unlink()
    except GitError:
        pass
    path = _hooks_v1_path(repo)
    if path.exists():
        try:
            data = _load_devin_map(repo)
        except ValueError:
            data = None
        if data is not None:
            owned = _owned_commands("devin", DEVIN_HOOK_EVENTS)
            cleaned = {}
            for key, entries in data.items():
                if isinstance(entries, list):
                    cleaned[key] = _strip_owned(entries, owned)
                else:
                    cleaned[key] = entries
            path.write_text(json.dumps(cleaned, indent=2) + "\n")
    cpath = claude_settings_path(repo)
    if cpath.exists():
        try:
            data = _load_claude_settings(repo)
        except ValueError:
            data = None
        if data is not None:
            owned = _owned_commands("claude", CLAUDE_HOOK_EVENTS)
            hooks = data.get("hooks")
            if isinstance(hooks, dict):
                for key in list(hooks):
                    if isinstance(hooks[key], list):
                        hooks[key] = _strip_owned(hooks[key], owned)
            remaining = any(
                isinstance(v, list) and v for v in hooks.values()
            ) if isinstance(hooks, dict) else False
            other_keys = [k for k in data if k != "hooks"]
            if not remaining and not other_keys:
                cpath.unlink()
            else:
                cpath.write_text(json.dumps(data, indent=2) + "\n")
    cfg = load_local_config(repo)
    cfg["enabled"] = False
    save_local_config(repo, cfg)


def _internal_env(index: Path) -> dict:
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = str(index)
    for role in ("AUTHOR", "COMMITTER"):
        env[f"GIT_{role}_NAME"] = "Partial"
        env[f"GIT_{role}_EMAIL"] = "partial@localhost"
        env[f"GIT_{role}_DATE"] = now_iso()
    return env


def _ref_value(root: str, ref: str) -> str | None:
    proc = _git(root, "rev-parse", "--verify", "--end-of-options", ref,
                check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode().strip()


def persist_checkpoint(repo: dict, bundle: dict, checkpoint_id: str) -> str:
    if not CHECKPOINT_ID_RE.fullmatch(checkpoint_id or ""):
        raise GitError(f"invalid checkpoint id: {checkpoint_id!r}")
    if not isinstance(bundle, dict) or bundle.get("version") != 1:
        raise GitError("persist requires a version-1 checkpoint bundle")
    cps = bundle.get("checkpoints") or []
    if not any(
        isinstance(c, dict) and c.get("id") == checkpoint_id
        for c in cps
    ):
        raise GitError(
            f"bundle does not contain checkpoint {checkpoint_id}")
    root = repo["root"]
    state = _state_dir(repo)
    index = state / f"index-{uuid.uuid4().hex}"
    env = _internal_env(index)
    payload = canonical_json(bundle).encode("utf-8")
    try:
        for _ in range(4):
            old = _ref_value(root, METADATA_REF)
            if old:
                _git(root, "read-tree", old, env=env)
            else:
                _git(root, "read-tree", "--empty", env=env)
            blob = _git(
                root, "hash-object", "-w", "--stdin",
                input_bytes=payload, env=env,
            ).stdout.decode().strip()
            _git(
                root, "update-index", "--add", "--cacheinfo",
                f"100644,{blob},{CHECKPOINT_DIR}/{checkpoint_id}.json",
                env=env,
            )
            tree = _git(root, "write-tree", env=env).stdout.decode().strip()
            args = ["commit-tree", tree, "-m",
                    f"partial checkpoint {checkpoint_id}"]
            if old:
                args += ["-p", old]
            commit = _git(root, *args, env=env).stdout.decode().strip()
            proc = _git(
                root, "update-ref", METADATA_REF, commit,
                old or "0" * 40, check=False,
            )
            if proc.returncode == 0:
                return commit
        raise GitError(
            f"could not update {METADATA_REF}: concurrent modifications")
    finally:
        index.unlink(missing_ok=True)


_BUNDLE_NAME_RE = re.compile(
    r"checkpoints/[0-9a-f]{32}\.json")
_PULL_CAP = 64 * 1024 * 1024


def _validate_remote_name(root: str, remote: str) -> None:
    if not remote or remote.startswith("-") or "://" in remote \
            or "/" in remote:
        raise GitError(f"invalid remote name: {remote!r}")
    remotes = _git_out(root, "remote").split()
    if remote not in remotes:
        raise GitError(
            f"remote {remote!r} is not configured in this repository")


def _list_bundle_blobs(root: str, ref: str) -> dict[str, str]:
    out = _git_out(root, "ls-tree", "-r", "-l", ref)
    blobs: dict[str, str] = {}
    total = 0
    for line in out.splitlines():
        meta, sep, name = line.partition("\t")
        parts = meta.split()
        if not sep or len(parts) != 4 or parts[1] != "blob":
            raise GitError(
                f"{ref}: unexpected non-blob entry in metadata tree")
        size_s = parts[3]
        if not _BUNDLE_NAME_RE.fullmatch(name):
            raise GitError(f"{ref}: unexpected entry {name!r}")
        if not size_s.isdigit():
            raise GitError(f"{ref}: invalid blob size for {name!r}")
        size = int(size_s)
        total += size
        if size > _PULL_CAP or total > _PULL_CAP:
            raise GitError(f"{ref}: metadata tree exceeds 64 MiB")
        blobs[name] = _git_out(root, "show", f"{ref}:{name}")
    return blobs


def _validate_bundles_for_repo(blobs: dict[str, str], repo_id: str
                               ) -> dict[str, dict]:
    bundles: dict[str, dict] = {}
    for name, raw in blobs.items():
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            raise GitError(f"{name}: invalid JSON checkpoint bundle")
        if not isinstance(obj, dict) or obj.get("version") != 1:
            raise GitError(f"{name}: not a version-1 checkpoint bundle")
        for section in ("repositories", "sessions", "checkpoints"):
            entries = obj.get(section)
            if not isinstance(entries, list):
                raise GitError(
                    f"{name}: bundle {section} must be a list")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise GitError(f"{name}: malformed {section} entry")
                key = "id" if section == "repositories" else "repo_id"
                if entry.get(key) != repo_id:
                    raise GitError(
                        f"{name}: bundle belongs to a different"
                        " repository")
        cps = obj["checkpoints"]
        if len(cps) != 1 or cps[0].get("id") != Path(name).stem:
            raise GitError(
                f"{name}: checkpoint id does not match bundle path")
        bundles[name] = obj
    return bundles


def _merge_metadata(store: Store, repo: dict, local: str, fetched: str,
                    remote_names: set[str]) -> str:
    root = repo["root"]
    state = _state_dir(repo)
    index = state / f"index-{uuid.uuid4().hex}"
    env = _internal_env(index)
    try:
        for _ in range(4):
            base = _ref_value(root, METADATA_REF)
            if not base:
                raise GitError(
                    f"{METADATA_REF} disappeared during merge")
            _git(root, "read-tree", base, env=env)
            local_names = set(_git_out(
                root, "ls-tree", "-r", "--name-only", base,
            ).split())
            for name in sorted(local_names | remote_names):
                cpid = name.split("/", 1)[1][:-5]
                bundle = store.checkpoint_bundle(cpid)
                payload = canonical_json(bundle).encode("utf-8")
                blob = _git(
                    root, "hash-object", "-w", "--stdin",
                    input_bytes=payload, env=env,
                ).stdout.decode().strip()
                _git(
                    root, "update-index", "--add", "--cacheinfo",
                    f"100644,{blob},{name}", env=env,
                )
            tree = _git(root, "write-tree",
                        env=env).stdout.decode().strip()
            commit = _git(
                root, "commit-tree", tree, "-p", base, "-p", fetched,
                "-m", "Merge Partial checkpoint context",
                env=env,
            ).stdout.decode().strip()
            proc = _git(
                root, "update-ref", METADATA_REF, commit, base,
                check=False,
            )
            if proc.returncode == 0:
                return commit
        raise GitError(
            f"could not update {METADATA_REF}: concurrent modifications")
    finally:
        index.unlink(missing_ok=True)


def sync_checkpoints(
    store: Store,
    repo: dict,
    *,
    push: bool = False,
    pull: bool = False,
    remote: str = "origin",
) -> dict:
    root = repo["root"]
    _validate_remote_name(root, remote)
    result = {
        "pushed": False, "pulled": 0, "import_errors": [],
        "diverged": False, "merged": False, "error": None,
    }
    if pull:
        private = f"refs/partial/remotes/{remote}/checkpoints-v1"
        proc = _git(
            root, "fetch", remote, f"{METADATA_REF}:{private}",
            check=False, timeout=60,
        )
        if proc.returncode != 0:
            result["error"] = (
                "fetch of metadata branch failed: "
                + str(redact(
                    proc.stderr.decode("utf-8", "replace").strip())))
            return result
        fetched = _ref_value(root, private)
        if not fetched:
            result["error"] = "remote has no metadata branch"
            return result
        rid = repo_id_for_repo(repo)
        try:
            remote_blobs = _list_bundle_blobs(root, fetched)
            remote_bundles = _validate_bundles_for_repo(
                remote_blobs, rid)
        except GitError as exc:
            result["error"] = str(exc)
            return result
        for name, bundle in remote_bundles.items():
            try:
                store.import_bundle(bundle)
                result["pulled"] += 1
            except ValueError as exc:
                result["error"] = f"{name}: {exc}"
                return result
        local = _ref_value(root, METADATA_REF)
        if not local or local == fetched:
            if not local:
                _git(root, "update-ref", METADATA_REF, fetched)
        else:
            anc = _git(root, "merge-base", "--is-ancestor", local,
                       fetched, check=False)
            if anc.returncode == 0:
                _git(root, "update-ref", METADATA_REF, fetched, local)
            else:
                anc2 = _git(root, "merge-base", "--is-ancestor",
                            fetched, local, check=False)
                if anc2.returncode != 0:
                    result["diverged"] = True
                    try:
                        local_blobs = _list_bundle_blobs(root, local)
                        local_bundles = _validate_bundles_for_repo(
                            local_blobs, rid)
                    except GitError as exc:
                        result["error"] = str(exc)
                        return result
                    for name, bundle in local_bundles.items():
                        try:
                            store.import_bundle(bundle)
                        except ValueError as exc:
                            result["error"] = f"{name}: {exc}"
                            return result
                    try:
                        _merge_metadata(
                            store, repo, local, fetched,
                            set(remote_blobs))
                    except (GitError, ValueError) as exc:
                        result["error"] = str(exc)
                        return result
                    result["merged"] = True
    if push:
        if not _ref_value(root, METADATA_REF):
            result["error"] = f"no local {METADATA_REF} to push"
            return result
        proc = _git(
            root, "push", remote, f"{METADATA_REF}:{METADATA_REF}",
            check=False, timeout=60,
        )
        if proc.returncode != 0:
            result["error"] = (
                "push of metadata branch refused (diverged?): "
                + str(redact(
                    proc.stderr.decode("utf-8", "replace").strip())))
            return result
        result["pushed"] = True
    return result
