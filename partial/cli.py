from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import __version__
from .adapters import (
    codex_event,
    namespace_codex_events,
    normalize_hook,
    parse_import,
)
from .git import (
    GitError,
    _hooks_dir,
    check_agent_configs,
    check_git_hook,
    claude_settings_path,
    create_checkpoint,
    disable_hooks,
    discover_repo,
    install_claude_hooks,
    install_devin_hooks,
    install_hooks,
    load_local_config,
    persist_checkpoint,
    repo_id_for_repo,
    save_local_config,
    sync_checkpoints,
)
from .handoff import format_handoff
from .models import (
    AGENTS,
    MAX_IMPORT_BYTES,
    Event,
    new_event_id,
    now_iso,
    scoped_session_id,
)
from .privacy import is_sensitive_path, redact
from .store import Store, default_db_path

HOOK_STDIN_CAP = 2 * 1024 * 1024
LINE_CAP = 1024 * 1024


def _store(args) -> Store:
    if args.home:
        return Store(Path(args.home) / "partial.db")
    return Store()


def _store_for_repo(args, repo: dict) -> Store:
    if not args.home and "PARTIAL_HOME" not in os.environ:
        home = load_local_config(repo).get("home")
        if isinstance(home, str) and home:
            return Store(Path(home) / "partial.db")
    return _store(args)


def _home_dir(args) -> Path:
    if args.home:
        return Path(args.home)
    return default_db_path().parent


def _repo_root_arg(args) -> str:
    return args.repo or os.environ.get("DEVIN_PROJECT_DIR") or "."


def _register_repo(args, store: Store) -> tuple[dict, str]:
    repo = discover_repo(_repo_root_arg(args))
    row = store.register_repo(repo["root"])
    return repo, row["id"]


def _warn(msg: str) -> None:
    print(f"partial: {redact(str(msg))}", file=sys.stderr)


def _cmd_enable(args) -> int:
    from . import brain_contract as _bc
    store = _store(args)
    repo, _rid = _register_repo(args, store)
    agents = args.agent or ["devin"]
    if "all" in agents:
        agents = list(AGENTS)
    want_skill = bool(args.memory_skill or args.skill_only)
    skill = Path(repo["root"]) / ".devin" / "skills" \
        / "partial-memory" / "SKILL.md"
    # Preflight every owned write target before modifying anything:
    # the skill file is only inspected (and may only block) when the
    # user actually requested skill installation.
    errors = []
    if want_skill and skill.exists():
        try:
            current = skill.read_text()
        except OSError as exc:
            errors.append(f"cannot read {skill}: {exc}")
        else:
            if current != _bc.BRAIN_SKILL:
                errors.append(
                    f"refusing to overwrite foreign file: {skill}")
    if not args.skill_only:
        git_err = check_git_hook(repo)
        if git_err:
            errors.append(git_err)
        errors += check_agent_configs(
            repo, [a for a in agents if a in ("devin", "claude")])
    if errors:
        for e in errors:
            _warn(e)
        return 2
    installed = []
    if not args.skill_only:
        result = install_hooks(repo)
        for e in result["errors"]:
            _warn(e)
        if result["errors"]:
            return 2
        cfg = load_local_config(repo)
        cfg["home"] = str(Path(store.path.parent).resolve())
        save_local_config(repo, cfg)
        installed = list(result["installed"])
        for agent in agents:
            if agent == "devin":
                installed += install_devin_hooks(repo)["installed"]
            elif agent == "claude":
                installed += install_claude_hooks(repo)["installed"]
            elif agent == "codex":
                print("codex: sessions are captured via"
                      " 'partial run codex' or"
                      " 'partial import --agent codex'")
            elif agent == "chatgpt":
                print("chatgpt: sessions are captured via"
                      " 'partial import --agent chatgpt'")
    if want_skill and not skill.exists():
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(_bc.BRAIN_SKILL)
        print(f"wrote {skill}")
    elif want_skill:
        print(f"memory skill already installed: {skill}")
    print("enabled: " + (", ".join(installed) or "memory skill only"))
    return 0


def _cmd_disable(args) -> int:
    repo = discover_repo(_repo_root_arg(args))
    disable_hooks(repo)
    print("disabled")
    return 0


def _cmd_status(args) -> int:
    store = _store(args)
    stats = store.stats()
    enabled = None
    branch = None
    try:
        repo = discover_repo(_repo_root_arg(args))
        enabled = bool(load_local_config(repo).get("enabled"))
        branch = repo.get("branch")
    except GitError:
        pass
    out = {
        "version": __version__,
        "db": str(store.path),
        "enabled": enabled,
        "branch": branch,
        **stats,
    }
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"partial {out['version']}  db={out['db']}")
        print(f"enabled={enabled} branch={branch}")
        print(f"repositories={stats['repositories']}"
              f" sessions={stats['sessions']}"
              f" checkpoints={stats['checkpoints']}")
    return 0


def _agent_hook_status(repo: dict) -> str:
    """Read-only summary of owned hook commands in agent config files."""
    bits = []

    def count_owned(path: Path, hooks_root=None) -> int:
        data = json.loads(path.read_text())
        if hooks_root is not None:
            data = data.get(hooks_root) if isinstance(data, dict) \
                else None
        if not isinstance(data, dict):
            return 0
        n = 0
        for groups in data.values():
            if not isinstance(groups, list):
                continue
            for g in groups:
                if not isinstance(g, dict):
                    continue
                for h in g.get("hooks") or []:
                    if isinstance(h, dict) and str(
                            h.get("command") or "").startswith(
                            "partial hook "):
                        n += 1
        return n

    dmap = Path(repo["root"]) / ".devin" / "hooks.v1.json"
    if dmap.exists():
        try:
            bits.append(f"devin:{count_owned(dmap)}")
        except (OSError, ValueError):
            bits.append("devin:unreadable")
    cpath = claude_settings_path(repo)
    if cpath.exists():
        try:
            bits.append(f"claude:{count_owned(cpath, 'hooks')}")
        except (OSError, ValueError):
            bits.append("claude:unreadable")
    return ", ".join(bits) if bits else "none installed"


def _cmd_doctor(args) -> int:
    checks = []
    checks.append({
        "check": "git", "ok": shutil.which("git") is not None,
        "detail": shutil.which("git") or "git not found on PATH",
    })
    repo = None
    try:
        store = _store(args)
        checks.append({"check": "db", "ok": True,
                       "detail": str(store.path)})
    except (OSError, ValueError) as exc:
        checks.append({"check": "db", "ok": False, "detail": str(exc)})
    try:
        repo = discover_repo(_repo_root_arg(args))
        checks.append({"check": "repo", "ok": True,
                       "detail": repo["root"]})
        cfg = load_local_config(repo)
        checks.append({"check": "enabled", "ok": True,
                       "detail": str(bool(cfg.get("enabled")))})
    except GitError as exc:
        checks.append({"check": "repo", "ok": False, "detail": str(exc)})
        repo = None
    if repo is not None:
        try:
            hook = _hooks_dir(repo) / "post-commit"
            installed = hook.exists() and "partial" in \
                hook.read_text(errors="replace")
            checks.append({
                "check": "capture-hook", "ok": installed,
                "detail": "installed" if installed else "missing"})
        except (GitError, OSError) as exc:
            checks.append({"check": "capture-hook", "ok": False,
                           "detail": str(exc)})
        checks.append({
            "check": "agent-hooks", "ok": True,
            "detail": _agent_hook_status(repo)})
        skill = Path(repo["root"]) / ".devin" / "skills" \
            / "partial-memory" / "SKILL.md"
        checks.append({
            "check": "memory-skill", "ok": True,
            "detail": "installed" if skill.exists()
            else "not installed (opt-in: 'partial enable"
                 " --memory-skill')"})
    try:
        store = _store(args)
        native = store.list_native()
        checks.append({"check": "native-registry", "ok": True,
                       "detail": f"{len(native)} registrations"})
        checks.append({"check": "attribution", "ok": True,
                       "detail": "line-attribution available"})
        checks.append({
            "check": "memory-fts5",
            "ok": getattr(store, "fts_ok", False),
            "detail": "FTS5 available" if getattr(
                store, "fts_ok", False)
            else "SQLite lacks FTS5; memory search unsupported"})
        conn = store._connect()
        try:
            idx = conn.execute(
                "SELECT COUNT(*) c FROM repository_indexes"
            ).fetchone()["c"]
            docs = conn.execute(
                "SELECT COUNT(*) c FROM memory_documents"
            ).fetchone()["c"]
        finally:
            conn.close()
        checks.append({"check": "memory-index", "ok": True,
                       "detail": f"{idx} indexed repos, {docs}"
                                 " documents"})
        from .ai import OpenAIProvider
        provider = OpenAIProvider()
        checks.append({
            "check": "ai-provider", "ok": True,
            "detail": f"configured ({provider.base})"
                      if provider.configured
                      else "not configured; set PARTIAL_OPENAI_API_KEY"})
        checks.append({
            "check": "external-ai-policy",
            "ok": True,
            "detail": "enabled" if store.memory_setting(
                "external_ai_enabled") == "true" else "disabled"})
        pf = _plugins_file(args)
        try:
            nplug = len(_load_plugins(args))
            checks.append({"check": "plugins", "ok": True,
                           "detail": f"{nplug} registered at {pf}"})
        except ValueError as exc:
            checks.append({"check": "plugins", "ok": False,
                           "detail": str(exc)})
        checks.append({"check": "mcp", "ok": True,
                       "detail": "stdio server available via"
                                 " 'partial mcp'"})
    except (OSError, ValueError) as exc:
        checks.append({"check": "store-features", "ok": False,
                       "detail": str(exc)})
    if args.json:
        print(json.dumps({"checks": checks}, indent=2))
    else:
        for c in checks:
            mark = "ok" if c["ok"] else "FAIL"
            print(f"[{mark}] {c['check']}: {c['detail']}")
    return 0 if all(c["ok"] for c in checks) else 1


def _refresh_session_bundles(store: Store, repo: dict, repo_id: str,
                             events: list[Event]) -> None:
    for ev in events:
        if ev.kind not in ("response", "session_end"):
            continue
        sid = scoped_session_id(repo_id, ev.agent, ev.session_id)
        for cpid in store.checkpoints_for_session(sid):
            try:
                persist_checkpoint(
                    repo, store.checkpoint_bundle(cpid), cpid)
            except (GitError, ValueError) as exc:
                _warn(f"checkpoint refresh failed: {exc}")


def _cmd_hook(args) -> int:
    if args.hook_agent == "git":
        return _hook_git(args)
    try:
        repo = discover_repo(_repo_root_arg(args))
    except GitError as exc:
        _warn(f"hook skipped: {exc}")
        return 0
    if not load_local_config(repo).get("enabled"):
        return 0
    raw = sys.stdin.read(HOOK_STDIN_CAP + 1)
    if len(raw) > HOOK_STDIN_CAP:
        _warn("hook payload exceeds 2 MiB limit; skipped")
        return 0
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        _warn("hook received invalid JSON on stdin")
        return 0
    store = _store_for_repo(args, repo)
    row = store.register_repo(repo["root"])
    repo_row = {**repo, "id": row["id"]}
    if args.hook_agent == "codex":
        return _hook_codex(store, repo_row, payload)
    event = args.event or payload.get("hook_event_name")
    if event == "PreToolUse":
        try:
            from .provenance import Provenance
            Provenance(store).before_tool(
                repo_row, args.hook_agent, payload)
        except Exception as exc:
            _warn(f"pre-tool attribution capture failed: {exc}")
        return 0
    try:
        events = normalize_hook(args.hook_agent, payload, args.event)
    except ValueError as exc:
        _warn(f"hook payload rejected: {exc}")
        return 0
    if not events:
        return 0
    store.ingest(
        row["id"], events,
        worktree=repo["root"], branch=repo.get("branch"),
    )
    _refresh_session_bundles(store, repo, row["id"], events)
    if event == "PostToolUse":
        try:
            from .provenance import Provenance
            Provenance(store).after_tool(
                repo_row, args.hook_agent, payload)
        except Exception as exc:
            _warn(f"post-tool attribution capture failed: {exc}")
    elif event in ("SessionStart", "UserPromptSubmit"):
        try:
            from .native import register_native
            register_native(
                store, repo_row, args.hook_agent,
                str(payload.get("session_id") or ""),
                transcript_path=payload.get("transcript_path"),
                source="hook")
        except Exception as exc:
            _warn(f"native registration failed: {exc}")
    return 0


def _codex_item_capture(store, repo_row, sid, payload,
                        key_hint: str) -> None:
    if payload.get("type") not in ("item.started", "item.completed"):
        return
    item = payload.get("item")
    changes = (item.get("changes") or []) \
        if isinstance(item, dict) else []
    paths = [c["path"] for c in changes
             if isinstance(c, dict)
             and isinstance(c.get("path"), str) and c["path"]]
    if not paths:
        return
    success = True
    if str(item.get("status") or "").lower() in ("failed", "error"):
        success = False
    ec = item.get("exit_code")
    if isinstance(ec, int) and not isinstance(ec, bool) and ec != 0:
        success = False
    pl = {
        "session_id": sid,
        "tool_name": "edit",
        "tool_input": {"paths": paths},
        "tool_use_id": str(item.get("id") or key_hint),
        "tool_response": {"success": success},
    }
    from .provenance import Provenance
    prov = Provenance(store)
    if payload["type"] == "item.started":
        prov.before_tool(repo_row, "codex", pl)
    else:
        prov.after_tool(repo_row, "codex", pl)


def _hook_codex(store, repo_row, payload) -> int:
    try:
        sid = str(payload.get("thread_id")
                  or payload.get("session_id") or "")
        if not sid and payload.get("type") != "thread.started":
            _warn("codex hook event lacks a session id; dropped")
            return 0
        evs = namespace_codex_events(
            codex_event(payload, sid or "codex"), payload,
            sid=sid or "codex", turn=0, seq=0,
            run="hook-" + sha256_hook(payload),
        )
        if evs:
            store.ingest(
                repo_row["id"], evs, worktree=repo_row["root"],
                branch=repo_row.get("branch"))
            _refresh_session_bundles(
                store, repo_row, repo_row["id"], evs)
        if payload.get("type") == "thread.started" and sid:
            try:
                from .native import register_native
                register_native(store, repo_row, "codex", sid,
                                source="hook")
            except Exception as exc:
                _warn(f"native registration failed: {exc}")
        if sid:
            _codex_item_capture(store, repo_row, sid, payload, "h0")
    except Exception as exc:
        _warn(f"codex hook failed: {exc}")
    return 0


def sha256_hook(payload: dict) -> str:
    import hashlib
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True,
                   default=str).encode()).hexdigest()[:16]


def _hook_git(args) -> int:
    try:
        repo = discover_repo(_repo_root_arg(args))
    except GitError as exc:
        _warn(f"git hook skipped: {exc}")
        return 0
    if not load_local_config(repo).get("enabled"):
        return 0
    store = _store_for_repo(args, repo)
    row = store.register_repo(repo["root"])
    try:
        cp = create_checkpoint(
            store, row["id"], commit="HEAD",
            worktree=repo["root"], require_links=True,
        )
        if cp is not None and cp.get("session_ids"):
            persist_checkpoint(
                repo, store.checkpoint_bundle(cp["id"]), cp["id"])
            print(f"partial: checkpoint {cp['id'][:12]} linked"
                  f" {len(cp['session_ids'])} session(s)",
                  file=sys.stderr)
    except Exception as exc:
        _warn(f"checkpoint failed: {exc}")
    return 0


def _cmd_import(args) -> int:
    path = Path(args.file)
    try:
        size = path.stat().st_size
    except OSError as exc:
        _warn(str(exc))
        return 2
    if size > MAX_IMPORT_BYTES:
        _warn(f"{path}: exceeds 64 MiB import limit")
        return 2
    try:
        content = path.read_text(errors="replace")
        events = parse_import(args.agent, content,
                              session_id=args.session_id)
    except (OSError, ValueError) as exc:
        _warn(str(exc))
        return 2
    store = _store(args)
    repo, rid = _register_repo(args, store)
    inserted = store.ingest(
        rid, events,
        worktree=repo["root"], branch=repo.get("branch"),
        track_paths=False,
    )
    print(f"imported {len(inserted)} event(s)"
          f" from {len(events)} parsed")
    return 0


def _cmd_sessions(args) -> int:
    store = _store(args)
    repo_id = None
    try:
        repo_id = repo_id_for_repo(discover_repo(_repo_root_arg(args)))
    except GitError:
        repo_id = None
    rows = store.list_sessions(
        repo_id=repo_id, agent=args.agent, q=args.search,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for s in rows:
            print(f"{s['id']} {s['agent']:<8} {s['status']:<7}"
                  f" {s.get('updated_at') or ''}  {s.get('title') or ''}")
        if not rows:
            print("no sessions")
    return 0


def _cmd_session(args) -> int:
    store = _store(args)
    sess = store.get_session(args.session_id)
    if sess is None:
        _warn(f"session not found: {args.session_id}")
        return 2
    if args.json:
        print(json.dumps(sess, indent=2))
        return 0
    print(f"session {sess['id']}")
    print(f"agent={sess['agent']} status={sess['status']}"
          f" title={sess.get('title') or ''}")
    for ev in sess["events"]:
        head = f"[{ev['timestamp']}] {ev['kind']}"
        if ev.get("tool_name"):
            head += f" {ev['tool_name']}"
        print(head)
        if ev.get("text"):
            print("  " + ev["text"].replace("\n", "\n  "))
    return 0


def _cmd_checkpoint(args) -> int:
    store = _store(args)
    repo, repo_id = _register_repo(args, store)
    resolved = []
    for ref in args.session or []:
        resolved.append(store.resolve_session(repo_id, ref))
    try:
        cp = create_checkpoint(
            store, repo_id,
            session_ids=resolved or None,
            commit=args.commit,
            worktree=repo["root"],
        )
    except (GitError, ValueError) as exc:
        _warn(str(exc))
        return 2
    if cp is None:
        _warn("no checkpoint created")
        return 2
    try:
        persist_checkpoint(
            repo, store.checkpoint_bundle(cp["id"]), cp["id"])
    except GitError as exc:
        _warn(f"persist failed: {exc}")
        return 2
    if args.json:
        print(json.dumps(cp, indent=2))
    else:
        print(f"checkpoint {cp['id']} commit={cp['commit_sha'][:12]}"
              f" sessions={len(cp['session_ids'])}")
    return 0


def _cmd_checkpoints(args) -> int:
    store = _store(args)
    repo_id = None
    try:
        repo_id = repo_id_for_repo(discover_repo(_repo_root_arg(args)))
    except GitError:
        pass
    rows = store.list_checkpoints(repo_id=repo_id, branch=args.branch,
                                  limit=args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for c in rows:
            print(f"{c['id']} {c['commit_sha'][:12]}"
                  f" {c.get('branch') or ''} {c.get('message') or ''}")
        if not rows:
            print("no checkpoints")
    return 0


def _cmd_export(args) -> int:
    store = _store(args)
    repo_id = None
    if args.repo_only:
        repo, repo_id = _register_repo(args, store)
    bundle = store.export_bundle(repo_id=repo_id)
    print(json.dumps(bundle, indent=2))
    return 0


def _cmd_handoff(args) -> int:
    store = _store(args)
    sess = store.get_session(args.session_id)
    if sess is None:
        _warn(f"session not found: {args.session_id}")
        return 2
    for e in sess["events"]:
        e["data"] = store.strip_paths(
            e.get("data") or {}, sess["repo_id"])
    print(format_handoff(sess))
    return 0


def _cmd_sync(args) -> int:
    store = _store(args)
    repo, _rid = _register_repo(args, store)
    result = sync_checkpoints(
        store, repo, push=args.push, pull=args.pull, remote=args.remote,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"pushed={result['pushed']} pulled={result['pulled']}"
              f" merged={result['merged']}")
    for e in result["import_errors"]:
        _warn(e)
    if result["error"]:
        _warn(result["error"])
    if result["error"] or result["import_errors"]:
        return 2
    return 0


def _cmd_capture(args) -> int:
    from .provenance import Provenance
    store = _store(args)
    sess = store.get_session_meta(args.session)
    if sess is None:
        _warn(f"session not found: {args.session}")
        return 2
    try:
        repo = discover_repo(_repo_root_arg(args))
    except GitError as exc:
        _warn(str(exc))
        return 2
    repo_row = store.register_repo(repo["root"])
    if repo_row["id"] != sess["repo_id"]:
        _warn("current worktree does not belong to the"
              " session's repository")
        return 2
    repo = {**repo, "id": sess["repo_id"]}
    payload = {
        "session_id": sess["native_id"],
        "tool_name": "edit",
        "tool_input": {"file_path": args.file},
        "tool_use_id": args.key,
        "tool_response": {"success": True},
    }
    prov = Provenance(store)
    if args.direction == "before":
        prov.before_tool(repo, sess["agent"], payload)
    else:
        prov.after_tool(repo, sess["agent"], payload)
    return 0


def _cmd_native_register(args) -> int:
    from .native import register_native
    store = _store(args)
    repo, rid = _register_repo(args, store)
    try:
        row = register_native(
            store, {**repo, "id": rid}, args.agent,
            args.session_id, path=args.file, archive=args.archive,
            source="explicit")
    except ValueError as exc:
        _warn(str(exc))
        return 2
    if row is None:
        print("not registered")
        return 0
    out = {k: v for k, v in row.items() if k != "archive"}
    out["has_archive"] = bool(row.get("archive"))
    print(json.dumps(out, indent=2))
    return 0


def _cmd_native_list(args) -> int:
    store = _store(args)
    repo_id = None
    try:
        repo_id = repo_id_for_repo(discover_repo(_repo_root_arg(args)))
    except GitError:
        pass
    rows = store.list_native(repo_id=repo_id)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for n in rows:
            print(f"{n['session_id']} {n['agent']:<7}"
                  f" {n['format']:<14} {n['native_id']}")
        if not rows:
            print("no native registrations")
    return 0


def _cmd_native_show(args) -> int:
    from .native import _resolve_session
    store = _store(args)
    try:
        sess = _resolve_session(store, args.session_id)
    except ValueError as exc:
        _warn(str(exc))
        return 2
    row = store.get_native(sess["id"])
    if row is None:
        _warn("session has no native registration")
        return 2
    out = {k: v for k, v in row.items() if k != "archive"}
    out["has_archive"] = bool(row.get("archive"))
    print(json.dumps(out, indent=2))
    return 0


def _cmd_resume(args) -> int:
    from .native import resume_session
    store = _store(args)
    try:
        result = resume_session(
            store, args.session_id, run=args.run,
            worktree=args.worktree, restore=args.restore_native,
            trust_native_state=args.trust_native_state,
            target_root=args.target_root)
    except ValueError as exc:
        _warn(str(exc))
        return 2
    except FileNotFoundError:
        _warn("agent executable not found")
        return 127
    if args.run:
        return int(result)
    print(json.dumps(result, indent=2))
    for w in result.get("warnings") or []:
        _warn(w)
    return 0


def _cmd_stop(args) -> int:
    from .native import _resolve_session
    store = _store(args)
    try:
        sess = _resolve_session(store, args.session_id)
    except ValueError as exc:
        _warn(str(exc))
        return 2
    store.end_session(sess["id"])
    print(f"marked ended: {sess['id']}")
    return 0


def _cmd_attach(args) -> int:
    from .native import _resolve_session
    store = _store(args)
    repo, repo_id = _register_repo(args, store)
    try:
        sess = _resolve_session(store, args.session_id)
    except ValueError as exc:
        _warn(str(exc))
        return 2
    if sess["repo_id"] != repo_id:
        _warn("session belongs to a different repository")
        return 2
    try:
        cp = create_checkpoint(
            store, repo_id, session_ids=[sess["id"]],
            commit=args.commit, worktree=repo["root"])
    except (GitError, ValueError) as exc:
        _warn(str(exc))
        return 2
    if cp is None:
        _warn("no checkpoint created")
        return 2
    try:
        persist_checkpoint(
            repo, store.checkpoint_bundle(cp["id"]), cp["id"])
    except GitError as exc:
        _warn(f"persist failed: {exc}")
        return 2
    print(f"attached session {sess['id']} to checkpoint"
          f" {cp['id']} commit={cp['commit_sha'][:12]}")
    return 0


def _blame_porcelain(root: str, rel: str) -> dict[int, dict]:
    proc = subprocess.run(
        ["git", "-C", root, "rev-parse", "--verify", "HEAD"],
        capture_output=True, timeout=15)
    if proc.returncode != 0:
        raise ValueError("repository has no HEAD commit")
    proc = subprocess.run(
        ["git", "-C", root, "-c", "core.quotepath=false", "blame",
         "--line-porcelain", "--root", "--", rel],
        capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise ValueError(
            "git blame failed:"
            f" {proc.stderr.decode('utf-8', 'replace').strip()}")
    lines: dict[int, dict] = {}
    cur = None
    for ln in proc.stdout.decode("utf-8", "replace").splitlines():
        if ln.startswith("\t"):
            if cur:
                lines[cur["final"]] = cur
            cur = None
            continue
        if cur is None:
            h = ln.split(" ")
            if len(h) >= 3 and len(h[0]) in (40, 64) \
                    and all(c in "0123456789abcdef" for c in h[0]) \
                    and h[1].isdigit() and h[2].isdigit():
                cur = {"commit": h[0], "orig": int(h[1]),
                       "final": int(h[2]), "author": "",
                       "summary": "", "filename": rel}
            continue
        key, _, val = ln.partition(" ")
        if key == "author":
            cur["author"] = str(redact(val))
        elif key == "summary":
            cur["summary"] = str(redact(val))
        elif key == "filename":
            cur["filename"] = _decode_git_path(val)
    return lines


def _decode_git_path(val: str) -> str:
    if not (val.startswith('"') and val.endswith('"')):
        return val
    body = val[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        c = body[i]
        if c != "\\" or i + 1 >= len(body):
            out += c.encode("utf-8", "surrogateescape")
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in "\\\"":
            out += nxt.encode("ascii")
            i += 2
        elif nxt == "n":
            out += b"\n"
            i += 2
        elif nxt == "t":
            out += b"\t"
            i += 2
        elif nxt in "01234567":
            octs = body[i + 1:i + 4]
            if len(octs) == 3 and all(c in "01234567" for c in octs):
                out.append(int(octs, 8))
                i += 4
            else:
                out += c.encode("utf-8", "surrogateescape")
                i += 1
        else:
            out += nxt.encode("utf-8", "surrogateescape")
            i += 2
    return out.decode("utf-8", "replace")


def _attribution_for(store, repo_id: str, commit: str,
                     filename: str, orig_line: int) -> dict:
    if set(commit) == {"0"}:
        return {"status": "uncommitted", "kind": "unknown"}
    conn = store._connect()
    try:
        row = conn.execute(
            "SELECT id FROM checkpoints WHERE repo_id=?"
            " AND commit_sha=?", (repo_id, commit)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"status": "no-checkpoint", "kind": "unknown"}
    rep = store.get_attribution(row["id"])
    if rep is None:
        return {"status": "no-report", "kind": "unknown",
                "checkpoint_id": row["id"]}
    imported = rep.get("capture_source") == "imported-claim"
    for f in rep.get("files") or []:
        if f.get("path") != filename:
            continue
        for ent in f.get("lines") or []:
            if ent.get("side") == "new" and ent.get("line") == orig_line:
                out = {
                    "status": "attributed",
                    "checkpoint_id": row["id"],
                    "kind": ent.get("kind") or "unknown",
                    "session_id": ent.get("session_id"),
                    "evidence": ent.get("evidence"),
                    "imported": imported,
                }
                sid = ent.get("session_id")
                if isinstance(sid, str) and sid:
                    out["prompt"] = _session_prompt(store, sid)
                return out
    return {"status": "no-line", "kind": "unknown",
            "checkpoint_id": row["id"], "imported": imported}


def _session_prompt(store, session_id: str) -> str | None:
    conn = store._connect()
    try:
        row = conn.execute(
            "SELECT text FROM events WHERE session_id=?"
            " AND kind='prompt' ORDER BY timestamp,rowid LIMIT 1",
            (session_id,)).fetchone()
    finally:
        conn.close()
    if row is None or not isinstance(row["text"], str) \
            or not row["text"]:
        return None
    text = row["text"]
    return text[:200] + ("…" if len(text) > 200 else "")


def _cmd_why_blame(args, single: bool) -> int:
    store = _store(args)
    try:
        repo, repo_id = _register_repo(args, store)
    except GitError as exc:
        _warn(str(exc))
        return 2
    root = repo["root"]
    ap = Path(args.file)
    if not ap.is_absolute():
        ap = Path.cwd() / ap
    try:
        rel_path = Path(os.path.abspath(ap)).relative_to(
            Path(os.path.abspath(root)))
    except (OSError, ValueError):
        _warn("file is outside the repository worktree")
        return 2
    rel = str(rel_path)
    if not rel or rel.startswith(".."):
        _warn("file is outside the repository worktree")
        return 2
    if is_sensitive_path(rel):
        _warn("refusing sensitive path")
        return 2
    cur = Path(root)
    try:
        for part in rel_path.parts:
            cur = cur / part
            if cur.is_symlink():
                _warn("refusing path through a symlink")
                return 2
    except OSError:
        _warn("cannot stat path")
        return 2
    try:
        blamed = _blame_porcelain(root, rel)
    except ValueError as exc:
        _warn(str(exc))
        return 2
    if not blamed:
        _warn("no blame information for file")
        return 2
    lo, hi = 1, max(blamed)
    if args.line:
        if single:
            lo = hi = args.line
        else:
            m = re.fullmatch(r"(\d+)(?:-(\d+))?", args.line)
            if not m:
                _warn("--line must be N or START-END")
                return 2
            lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if lo < 1 or hi > max(blamed) or lo > hi:
            _warn("line range out of bounds")
            return 2
    out = []
    for n in range(lo, hi + 1):
        b = blamed.get(n)
        if not b:
            continue
        att = _attribution_for(
            store, repo_id, b["commit"], b["filename"], b["orig"])
        out.append({
            "line": n, "commit": b["commit"], "author": b["author"],
            "summary": b["summary"], "path": b["filename"],
            "orig_line": b["orig"], **att,
        })
    if args.json:
        print(json.dumps(
            out[0] if single and args.line and out else out,
            indent=2))
        return 0
    for r in out:
        sess = f" session={r['session_id'][:12]}" \
            if r.get("session_id") else ""
        print(f"{r['line']:>5} {r['commit'][:12]} {r['kind']:<7}"
              f" {r.get('evidence') or '-':<10}{sess}"
              f"  {r['author']}  {r['summary']}")
    return 0


def _cmd_why(args) -> int:
    return _cmd_why_blame(args, single=True)


def _cmd_blame(args) -> int:
    return _cmd_why_blame(args, single=False)


def _codex_prompt_from(rest: list[str]) -> str | None:
    positional = [a for a in rest if not a.startswith("-")]
    if len(positional) == 1 and len(rest) == 1:
        return positional[0]
    return None


def _iter_bounded_lines(stream, limit: int = LINE_CAP):
    while True:
        line = stream.readline(limit)
        if not line:
            return
        if not line.endswith(b"\n") and len(line) == limit:
            while True:
                rest = stream.readline(limit)
                if not rest or rest.endswith(b"\n") or len(rest) < limit:
                    break
            yield None
            continue
        yield line


def _run_env_cwd(args, store: Store) -> tuple[dict, str, dict, str]:
    repo = discover_repo(_repo_root_arg(args))
    if not load_local_config(repo).get("enabled"):
        raise GitError(
            "Partial is not enabled for this repository; run"
            " 'partial enable' first")
    row = store.register_repo(repo["root"])
    env = dict(os.environ)
    env["PARTIAL_HOME"] = str(Path(store.path.parent).resolve())
    return env, repo["root"], repo, row["id"]


def _cmd_run_codex(args) -> int:
    store = _store(args)
    env, cwd, repo, repo_id = _run_env_cwd(args, store)
    cmd = ["codex", "exec", "--json", *args.rest]
    sid = ""
    seq = 0
    turn = 0
    run_id = uuid.uuid4().hex
    prompt_text = _codex_prompt_from(args.rest)

    def ingest_now(evs):
        if evs:
            store.ingest(
                repo_id, evs, worktree=repo["root"],
                branch=repo.get("branch"))
            _refresh_session_bundles(store, repo, repo_id, evs)

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=None, stdin=None,
            cwd=cwd, env=env,
        )
    except FileNotFoundError:
        _warn("codex executable not found")
        return 127
    assert proc.stdout is not None
    tee = getattr(sys.stdout, "buffer", None)
    warned_bad = 0
    try:
        for raw_line in _iter_bounded_lines(proc.stdout):
            if raw_line is None:
                _warn("skipped oversize codex output line")
                continue
            if tee is not None:
                tee.write(raw_line)
                tee.flush()
            else:
                sys.stdout.write(raw_line.decode("utf-8", "replace"))
                sys.stdout.flush()
            line = raw_line.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                if warned_bad < 5:
                    _warn(f"unparsed codex output: {line[:200]}")
                    warned_bad += 1
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("type") == "turn.started":
                turn += 1
            if payload.get("type") == "thread.started" and \
                    payload.get("thread_id"):
                sid = str(payload["thread_id"])
            s = sid or "codex"
            evs = namespace_codex_events(
                codex_event(payload, s), payload,
                sid=s, turn=turn, seq=seq, run=run_id,
            )
            ingest_now(evs)
            if payload.get("type") == "thread.started" and sid:
                try:
                    from .native import register_native
                    register_native(
                        store, {**repo, "id": repo_id}, "codex",
                        sid, source="hook")
                except Exception as exc:
                    _warn(f"native registration failed: {exc}")
            if sid:
                try:
                    _codex_item_capture(
                        store, {**repo, "id": repo_id}, sid, payload,
                        f"{run_id}:{seq}")
                except Exception as exc:
                    _warn(f"codex attribution capture failed: {exc}")
            if payload.get("type") == "thread.started" and sid \
                    and prompt_text:
                ingest_now([Event(
                    id=new_event_id(), session_id=sid, agent="codex",
                    kind="prompt", timestamp=now_iso(), text=prompt_text,
                )])
                prompt_text = None
            seq += 1
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return 130
    code = proc.wait()
    if sid:
        ingest_now([Event(
            id=new_event_id(), session_id=sid, agent="codex",
            kind="session_end", timestamp=now_iso(),
        )])
    return code


def _cmd_run_claude(args) -> int:
    store = _store(args)
    env, cwd, repo, _rid = _run_env_cwd(args, store)
    settings = claude_settings_path(repo)
    if not settings.exists():
        install_claude_hooks(repo)
    cmd = ["claude", "--settings", str(settings), *args.rest]
    try:
        return subprocess.call(cmd, cwd=cwd, env=env)
    except FileNotFoundError:
        _warn("claude executable not found")
        return 127


def _cmd_run_devin(args) -> int:
    store = _store(args)
    env, cwd, _repo, _rid = _run_env_cwd(args, store)
    try:
        return subprocess.call(["devin", *args.rest], cwd=cwd, env=env)
    except FileNotFoundError:
        _warn("devin executable not found")
        return 127


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="partial",
        description="Partial: local AI session tracker and git checkpoints",
    )
    p.add_argument("--version", action="version",
                   version=f"partial {__version__}")
    p.add_argument("--home", default=None,
                   help="partial state directory (env PARTIAL_HOME)")
    p.add_argument("--repo", default=None,
                   help="repository path (env DEVIN_PROJECT_DIR)")
    sub = p.add_subparsers(dest="command")

    e = sub.add_parser("enable", help="install hooks in this repository")
    e.add_argument("--agent", action="append",
                   choices=list(AGENTS) + ["all"], default=None)
    e.add_argument("--memory-skill", action="store_true",
                   help="also write .devin/skills/partial-memory/"
                        "SKILL.md (off by default)")
    e.add_argument("--skill-only", action="store_true",
                   help="write only the partial-memory skill file;"
                        " do not install hooks")
    e.set_defaults(func=_cmd_enable)

    d = sub.add_parser("disable", help="disable partial hooks")
    d.set_defaults(func=_cmd_disable)

    s = sub.add_parser("status", help="show store status")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_status)

    doc = sub.add_parser("doctor", help="check environment")
    doc.add_argument("--json", action="store_true")
    doc.set_defaults(func=_cmd_doctor)

    h = sub.add_parser("hook", help="handle an agent/git hook event")
    h.add_argument("hook_agent",
                   choices=["devin", "claude", "codex", "git"])
    h.add_argument("event", nargs="?", default=None)
    h.set_defaults(func=_cmd_hook)

    i = sub.add_parser("import", help="import a transcript file")
    i.add_argument("--agent", required=True, choices=list(AGENTS))
    i.add_argument("file")
    i.add_argument("--session-id", default=None)
    i.set_defaults(func=_cmd_import)

    ss = sub.add_parser("sessions", help="list sessions")
    ss.add_argument("--agent", default=None, choices=list(AGENTS))
    ss.add_argument("--search", default=None)
    ss.add_argument("--limit", type=int, default=100)
    ss.add_argument("--json", action="store_true")
    ss.set_defaults(func=_cmd_sessions)

    se = sub.add_parser("session", help="show one session")
    se.add_argument("session_id")
    se.add_argument("--json", action="store_true")
    se.set_defaults(func=_cmd_session)

    cp = sub.add_parser("checkpoint", help="checkpoint a commit")
    cp.add_argument("--session", action="append", default=None)
    cp.add_argument("--commit", default="HEAD")
    cp.add_argument("--json", action="store_true")
    cp.set_defaults(func=_cmd_checkpoint)

    cps = sub.add_parser("checkpoints", help="list checkpoints")
    cps.add_argument("--branch", default=None)
    cps.add_argument("--limit", type=int, default=100)
    cps.add_argument("--json", action="store_true")
    cps.set_defaults(func=_cmd_checkpoints)

    ex = sub.add_parser("export", help="export bundle to stdout")
    ex.add_argument("--repo-only", action="store_true")
    ex.set_defaults(func=_cmd_export)

    hf = sub.add_parser("handoff", help="render session handoff markdown")
    hf.add_argument("session_id")
    hf.set_defaults(func=_cmd_handoff)

    sy = sub.add_parser("sync", help="push/pull checkpoint metadata")
    sy.add_argument("--push", action="store_true")
    sy.add_argument("--pull", action="store_true")
    sy.add_argument("--remote", default="origin")
    sy.add_argument("--json", action="store_true")
    sy.set_defaults(func=_cmd_sync)

    r = sub.add_parser("run", help="run an agent CLI")
    r.add_argument("run_agent", choices=["codex", "claude", "devin"])
    r.add_argument("rest", nargs=argparse.REMAINDER)
    r.set_defaults(func=_cmd_run)

    au = sub.add_parser("auth", help="authentication helpers")
    ausub = au.add_subparsers(dest="auth_command")
    at = ausub.add_parser(
        "token", help="print the bootstrap token for first-time setup")
    at.set_defaults(func=_cmd_auth_token)

    ac = sub.add_parser("account", help="local account administration")
    acsub = ac.add_subparsers(dest="account_command")
    acc = acsub.add_parser(
        "create", help="create the first owner account (local only)")
    acc.add_argument("--email", required=True)
    acc.add_argument("--name", required=True)
    acc.add_argument("--password-stdin", action="store_true")
    acc.set_defaults(func=_cmd_account_create)
    act = acsub.add_parser(
        "token", help="mint a workspace API token (verifies password)")
    act.add_argument("--email", required=True)
    act.add_argument("--workspace", required=True)
    act.add_argument("--name", required=True)
    act.add_argument("--role", default="member",
                     choices=["member", "viewer"])
    act.add_argument("--expires-days", type=int, default=90)
    act.add_argument("--password-stdin", action="store_true")
    act.set_defaults(func=_cmd_account_token)

    sv = sub.add_parser("serve", help="run the local web workspace")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=4310)
    sv.add_argument("--public-url", default=None)
    sv.add_argument("--demo", action="store_true")
    sv.set_defaults(func=_cmd_serve)

    up = sub.add_parser(
        "upload", help="upload an exported bundle to a partial server")
    up.add_argument("server_url")
    up.add_argument("--repo-only", action="store_true")
    up.add_argument("--workspace", default=None,
                    help="workspace id for multi-workspace servers")
    up.set_defaults(func=_cmd_upload)

    ib = sub.add_parser(
        "ingest-bundle", help="import a partial bundle file")
    ib.add_argument("file")
    ib.set_defaults(func=_cmd_ingest_bundle)

    cap = sub.add_parser(
        "capture", help="attribute a file edit outside agent hooks")
    cap.add_argument("direction", choices=["before", "after"])
    cap.add_argument("--session", required=True)
    cap.add_argument("--file", required=True)
    cap.add_argument("--key", required=True)
    cap.set_defaults(func=_cmd_capture)

    na = sub.add_parser("native", help="native session registry")
    nasub = na.add_subparsers(dest="native_command")
    nr = nasub.add_parser(
        "register", help="register a native session id/file")
    nr.add_argument("--agent", required=True,
                    choices=["devin", "claude", "codex"])
    nr.add_argument("--session-id", required=True)
    nr.add_argument("--file", default=None)
    nr.add_argument("--archive", action="store_true")
    nr.set_defaults(func=_cmd_native_register)
    nl = nasub.add_parser("list", help="list native registrations")
    nl.add_argument("--json", action="store_true")
    nl.set_defaults(func=_cmd_native_list)
    nsh = nasub.add_parser("show", help="show a native registration")
    nsh.add_argument("session_id")
    nsh.set_defaults(func=_cmd_native_show)

    rs = sub.add_parser("resume", help="plan or run a native resume")
    rs.add_argument("session_id")
    rs.add_argument("--run", action="store_true")
    rs.add_argument("--worktree", default=None)
    rs.add_argument("--restore-native", action="store_true")
    rs.add_argument("--trust-native-state", action="store_true")
    rs.add_argument("--target-root", default=None,
                    help="restore target root (defaults to user home)")
    rs.set_defaults(func=_cmd_resume)

    st = sub.add_parser("stop", help="mark a session ended")
    st.add_argument("session_id")
    st.set_defaults(func=_cmd_stop)

    at_ = sub.add_parser(
        "attach", help="link a session to a commit checkpoint")
    at_.add_argument("session_id")
    at_.add_argument("--commit", default="HEAD")
    at_.set_defaults(func=_cmd_attach)

    wy = sub.add_parser(
        "why", help="show recorded evidence for a file line")
    wy.add_argument("file")
    wy.add_argument("--line", type=int, default=None)
    wy.add_argument("--json", action="store_true")
    wy.set_defaults(func=_cmd_why)

    bl = sub.add_parser(
        "blame", help="git blame enriched with attribution")
    bl.add_argument("file")
    bl.add_argument("--line", default=None,
                    help="N or START-END")
    bl.add_argument("--json", action="store_true")
    bl.set_defaults(func=_cmd_blame)

    ix = sub.add_parser(
        "index", help="build the local repository memory index")
    ix.add_argument("--all", action="store_true",
                    help="index all registered repos with local roots")
    ix.add_argument("--semantic", action="store_true",
                    help="also compute embeddings (paid API call)")
    ix.set_defaults(func=_cmd_index)

    sr = sub.add_parser(
        "search", help="lexical memory search")
    sr.add_argument("query")
    sr.add_argument("--code", action="store_true",
                    help="restrict to code documents")
    sr.add_argument("--semantic", action="store_true",
                    help="include embeddings (paid API call)")
    sr.add_argument("--limit", type=int, default=12)
    sr.add_argument("--all-repos", action="store_true",
                    help="search all repositories, not just the"
                         " current one")
    sr.add_argument("--json", action="store_true")
    sr.set_defaults(func=_cmd_search)

    cx = sub.add_parser(
        "context", help="retrieve bounded evidence for a question")
    cx.add_argument("query")
    cx.add_argument("--semantic", action="store_true")
    cx.add_argument("--all-repos", action="store_true",
                    help="search all repositories, not just the"
                         " current one")
    cx.add_argument("--json", action="store_true")
    cx.set_defaults(func=_cmd_context)

    ak = sub.add_parser("ask", help="answer a repository question")
    ak.add_argument("query")
    ak.add_argument("--all-repos", action="store_true",
                    help="search all repositories, not just the"
                         " current one")
    ak.add_argument("--run", action="store_true",
                    help="send evidence to the configured AI provider"
                         " (paid request)")
    ak.set_defaults(func=_cmd_ask)

    de = sub.add_parser("decision", help="record decisions")
    desub = de.add_subparsers(dest="decision_command")
    da = desub.add_parser("add", help="record a decision")
    da.add_argument("--title", required=True)
    da.add_argument("--body", required=True)
    da.add_argument("--source", action="append", default=None,
                    help="evidence document id (repeatable)")
    da.add_argument("--supersedes", default=None)
    da.set_defaults(func=_cmd_decision)
    dl = desub.add_parser("list", help="list decisions")
    dl.add_argument("--json", action="store_true")
    dl.set_defaults(func=_cmd_decision)

    gr = sub.add_parser("graph", help="indexed code relationships")
    grsub = gr.add_subparsers(dest="graph_command")
    gs = grsub.add_parser("search", help="find symbols")
    gs.add_argument("query")
    gs.set_defaults(func=_cmd_graph)
    gn = grsub.add_parser("neighbors", help="symbol neighbors")
    gn.add_argument("symbol_id")
    gn.set_defaults(func=_cmd_graph)
    gi = grsub.add_parser("impact", help="incoming-edge impact")
    gi.add_argument("symbol_id")
    gi.set_defaults(func=_cmd_graph)
    gc = grsub.add_parser("capabilities", help="language coverage")
    gc.set_defaults(func=_cmd_graph)

    dp = sub.add_parser("dispatch", help="recorded-activity dispatch")
    dp.add_argument("--since", default=None)
    dp.add_argument("--until", default=None)
    dp.add_argument("--branch", default=None)
    dp.add_argument("--run", action="store_true",
                    help="AI-generate the dispatch (paid request)")
    dp.set_defaults(func=_cmd_dispatch)
    rc = sub.add_parser("recap", help="alias of dispatch")
    rc.add_argument("--since", default=None)
    rc.add_argument("--until", default=None)
    rc.add_argument("--branch", default=None)
    rc.add_argument("--run", action="store_true")
    rc.set_defaults(func=_cmd_dispatch)

    rv = sub.add_parser("review", help="evidence-backed code review")
    rvsub = rv.add_subparsers(dest="review_command")
    rvr = rvsub.add_parser("run", help="plan or run a review")
    rvr.add_argument("--base", default=None,
                     help="git ref; reviews diff base...HEAD")
    rvr.add_argument("--query", default=None)
    rvr.add_argument("--agents", default=None,
                     help="comma list of codex,claude,devin")
    rvr.add_argument("--run", action="store_true")
    rvr.set_defaults(func=_cmd_review)
    rvs = rvsub.add_parser("show", help="show a workflow run")
    rvs.add_argument("run_id")
    rvs.set_defaults(func=_cmd_review)
    rv.set_defaults(func=_cmd_review, review_command="run",
                    base=None, query=None, agents=None, run=False)

    iv = sub.add_parser(
        "investigate", help="evidence-backed investigation")
    ivsub = iv.add_subparsers(dest="investigate_command")
    ivr = ivsub.add_parser("run", help="plan or run an investigation")
    ivr.add_argument("query")
    ivr.add_argument("--seed", default=None,
                     help="local file (<=64KiB) to include as evidence")
    ivr.add_argument("--agents", default=None)
    ivr.add_argument("--run", action="store_true")
    ivr.set_defaults(func=_cmd_investigate)
    ivs = ivsub.add_parser("show", help="show a workflow run")
    ivs.add_argument("run_id")
    ivs.set_defaults(func=_cmd_investigate)
    iv.set_defaults(func=_cmd_investigate,
                    investigate_command="run", query="",
                    seed=None, agents=None, run=False)

    xp = sub.add_parser(
        "experts", help="sessions linked to checkpoints touching"
                        " a path or topic")
    xp.add_argument("scope")
    xp.add_argument("--json", action="store_true")
    xp.set_defaults(func=_cmd_experts)

    tk = sub.add_parser("tokens", help="reported token usage totals")
    tk.add_argument("--session", default=None)
    tk.add_argument("--checkpoint", default=None)
    tk.set_defaults(func=_cmd_tokens)

    mp = sub.add_parser("mcp", help="serve the MCP stdio protocol")
    mp.set_defaults(func=_cmd_mcp)

    pl = sub.add_parser("plugin", help="explicit local plugins")
    plsub = pl.add_subparsers(dest="plugin_command")
    plr = plsub.add_parser("register", help="register a plugin")
    plr.add_argument("name")
    plr.add_argument("--command", dest="command_path",
                     required=True)
    plr.add_argument("--sha256", required=True)
    plr.set_defaults(func=_cmd_plugin)
    pll = plsub.add_parser("list", help="list plugins")
    pll.set_defaults(func=_cmd_plugin)
    plx = plsub.add_parser("run", help="run a registered plugin")
    plx.add_argument("name")
    plx.add_argument("rest", nargs=argparse.REMAINDER)
    plx.set_defaults(func=_cmd_plugin)

    pj = sub.add_parser("project", help="project groupings")
    pjsub = pj.add_subparsers(dest="project_command")
    pjc = pjsub.add_parser("create", help="create a project")
    pjc.add_argument("name")
    pjc.set_defaults(func=_cmd_project)
    pjl = pjsub.add_parser("list", help="list projects")
    pjl.set_defaults(func=_cmd_project)
    pja = pjsub.add_parser("attach", help="attach a repo to a project")
    pja.add_argument("project_id")
    pja.add_argument("--repo-id", default=None)
    pja.set_defaults(func=_cmd_project)

    cf = sub.add_parser("configure", help="show local configuration")
    cf.add_argument("--show", action="store_true")
    cf.set_defaults(func=_cmd_configure)

    return p


def _cmd_run(args) -> int:
    if args.run_agent == "codex":
        return _cmd_run_codex(args)
    if args.run_agent == "claude":
        return _cmd_run_claude(args)
    return _cmd_run_devin(args)


def _cmd_auth_token(args) -> int:
    from .accounts import Accounts
    from .auth import get_token
    home = _home_dir(args)
    acc = Accounts(home, home / "partial.db")
    if acc.initialized():
        _warn("accounts are already initialized; the bootstrap token"
              " no longer grants access — create a workspace API"
              " token in the web UI (Settings → API tokens)")
        return 2
    print(get_token(home))
    return 0


def _read_password_arg(args, prompt="Password: ") -> str | None:
    if getattr(args, "password_stdin", False):
        line = sys.stdin.readline(1026)
        if not line:
            return None
        return line.rstrip("\r\n")
    import getpass
    first = getpass.getpass(prompt)
    second = getpass.getpass("Repeat password: ")
    if first != second:
        return None
    return first


def _cmd_account_create(args) -> int:
    from .accounts import Accounts, AccountsError
    home = _home_dir(args)
    acc = Accounts(home, home / "partial.db")
    if acc.initialized():
        _warn("accounts are already initialized; ask a workspace owner"
              " or admin for an invitation")
        return 2
    password = _read_password_arg(args)
    if password is None:
        _warn("passwords did not match" if not args.password_stdin
              else "no password on stdin")
        return 2
    try:
        user = acc.setup(
            email=args.email, name=args.name, password=password)
    except AccountsError as exc:
        _warn(str(exc))
        return 2
    print(f"created owner account {user['email']}"
          f" ({user['id']}) for the default workspace")
    return 0


def _cmd_account_token(args) -> int:
    from .accounts import Accounts, AccountsError
    home = _home_dir(args)
    acc = Accounts(home, home / "partial.db")
    if not acc.initialized():
        _warn("no accounts yet; run `partial account create` first")
        return 2
    if args.password_stdin:
        line = sys.stdin.readline(1026)
        password = line.rstrip("\r\n") if line else None
    else:
        import getpass
        password = getpass.getpass("Password: ")
    if password is None:
        _warn("no password on stdin")
        return 2
    try:
        principal = acc.check_password(args.email, password)
        item = acc.create_api_token(
            principal, args.workspace, args.name, args.role,
            args.expires_days)
    except AccountsError as exc:
        _warn(str(exc))
        return 2
    print(item["token"])
    _warn("token shown once; store it securely")
    return 0


def _cmd_serve(args) -> int:
    if args.demo:
        from .demo import create_demo_store
        store = create_demo_store()
    else:
        store = _store(args)
    from .server import serve
    serve(store, host=args.host, port=args.port,
          public_url=args.public_url, demo=args.demo)
    return 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _upload_opener():
    # Proxies are disabled: environment proxy settings must never see
    # the Authorization header (a plaintext loopback request would
    # send it to the proxy in clear).
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect())


def _validate_upload_url(url: str) -> str:
    from .server import _is_loopback
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ValueError(f"invalid server URL: {url}")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(
            "server URL must be an http(s) URL with a host")
    if parts.username or parts.password or parts.query \
            or parts.fragment:
        raise ValueError(
            "server URL must not contain userinfo, query, or fragment")
    if parts.path not in ("", "/"):
        raise ValueError("server URL must not contain a path")
    try:
        parts.port
    except ValueError:
        raise ValueError("invalid port in server URL")
    if parts.scheme != "https" and not _is_loopback(parts.hostname):
        raise ValueError("remote uploads require HTTPS")
    return url.rstrip("/")


def _cmd_upload(args) -> int:
    token = os.environ.get("PARTIAL_SERVER_TOKEN")
    if not token:
        _warn("set PARTIAL_SERVER_TOKEN to the workspace access token"
              " before uploading")
        return 2
    try:
        base = _validate_upload_url(args.server_url)
    except ValueError as exc:
        _warn(str(exc))
        return 2
    store = _store(args)
    repo_id = None
    if args.repo_only:
        _repo, repo_id = _register_repo(args, store)
    bundle = store.export_bundle(repo_id=repo_id)
    payload = json.dumps(bundle).encode()
    if len(payload) > 16 * 1024 * 1024:
        _warn("bundle exceeds the 16 MiB upload limit; retry with"
              " --repo-only to upload a single repository")
        return 2
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if args.workspace:
        headers["X-Partial-Workspace"] = args.workspace
    req = urllib.request.Request(
        base + "/api/bundles",
        data=payload,
        headers=headers,
        method="POST",
    )
    opener = _upload_opener()
    try:
        with opener.open(req, timeout=30) as resp:
            result = json.loads(resp.read(1024 * 1024 + 1) or b"{}")
    except urllib.error.HTTPError as exc:
        _warn(f"upload failed: HTTP {exc.code}")
        return 1
    except (urllib.error.URLError, OSError) as exc:
        _warn(f"upload failed: {exc}")
        return 1
    print(json.dumps(result))
    return 0


def _cmd_ingest_bundle(args) -> int:
    path = Path(args.file)
    try:
        size = path.stat().st_size
    except OSError as exc:
        _warn(str(exc))
        return 2
    if size > MAX_IMPORT_BYTES:
        _warn(f"{path}: exceeds 64 MiB import limit")
        return 2
    try:
        obj = json.loads(path.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"{path}: invalid bundle JSON ({exc})")
        return 2
    store = _store(args)
    try:
        result = store.import_bundle(obj)
    except ValueError as exc:
        _warn(str(exc))
        return 2
    print(f"imported bundle: {result}")
    return 0


def _cmd_index(args) -> int:
    from .memory import Memory
    store = _store(args)
    mem = Memory(store)
    results = []
    if args.all:
        for row in store.list_repos():
            results.append(mem.index(
                {"id": row["id"], "root": row.get("root")},
                semantic=args.semantic))
    else:
        try:
            repo = discover_repo(_repo_root_arg(args))
            store.register_repo(repo["root"])
            results.append(mem.index(repo, semantic=args.semantic))
        except GitError:
            results.append(mem.index(None, semantic=args.semantic))
    semantic_failed = False
    for r in results:
        for item in r["indexed_repositories"]:
            print(f"indexed repo {item['id'][:12]}"
                  f" commit={item['commit_sha'][:12] or '-'}"
                  f" documents={item['documents']}")
        for s in r["skipped"][:20]:
            print(f"  skipped {s['path']}: {s['reason']}")
        if args.semantic and not r.get("semantic"):
            _warn("semantic indexing skipped: provider not"
                  " configured")
            semantic_failed = True
    # An explicitly requested --semantic index that produced no
    # embeddings is a failure, not a partial success.
    return 1 if semantic_failed else 0


def _current_repo_id(args, store: Store):
    try:
        repo = discover_repo(_repo_root_arg(args))
        store.register_repo(repo["root"])
        return repo_id_for_repo(repo)
    except GitError:
        return None


def _cmd_search(args) -> int:
    from .memory import Memory
    store = _store(args)
    mem = Memory(store)
    kind = "code" if args.code else None
    repo_id = None if args.all_repos else _current_repo_id(args, store)
    rows = mem.search(args.query, repo_id=repo_id, kind=kind,
                      semantic=args.semantic, limit=args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        loc = f" {r['path']}:{r['line_start']}-{r['line_end']}" \
            if r.get("path") else ""
        print(f"{r['id'][:12]} [{r['kind']}]{loc} {r['title']}")
    return 0


def _cmd_context(args) -> int:
    from .memory import Memory
    store = _store(args)
    mem = Memory(store)
    repo_id = None if args.all_repos else _current_repo_id(args, store)
    out = mem.context(args.query, repo_id=repo_id,
                      semantic=args.semantic)
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        for d in out["documents"]:
            loc = f" {d['path']}:{d['line_start']}" \
                if d.get("path") else ""
            print(f"[{d['id'][:12]}] [{d['kind']}]{loc}"
                  f" {d['title']}")
            print("    " + str(d.get("text") or "")
                  .replace("\n", "\n    ")[:800])
    return 0


def _cmd_ask(args) -> int:
    from .workflows import run_workflow
    store = _store(args)
    repo_id = None if args.all_repos else _current_repo_id(args, store)
    out = run_workflow(store, "ask", repo_id=repo_id,
                       query=args.query, run=args.run,
                       allow_external=bool(args.run))
    print(json.dumps(out, indent=2))
    return 0 if out["status"] != "error" else 1


def _cmd_decision(args) -> int:
    from .memory import Memory
    store = _store(args)
    mem = Memory(store)
    if args.decision_command == "add":
        _repo, repo_id = _register_repo(args, store)
        d = mem.add_decision(
            repo_id, args.title, args.body, args.source or [],
            author=os.environ.get("USER") or "local",
            supersedes=args.supersedes)
        print(f"decision {d['id'][:16]} recorded")
        return 0
    rows = mem.decisions()
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for d in rows:
            print(f"{d['id'][:16]} [{d['status']}] {d['title']}"
                  f" (author={d['author']})")
    return 0


def _cmd_graph(args) -> int:
    from .memory import Memory
    store = _store(args)
    mem = Memory(store)
    repo_id = None
    try:
        repo = discover_repo(_repo_root_arg(args))
        repo_id = repo_id_for_repo(repo)
    except GitError:
        pass
    sub = args.graph_command
    if sub == "capabilities":
        print(json.dumps(mem.graph_capabilities(), indent=2))
        return 0
    if sub == "search":
        out = mem.graph_search(args.query, repo_id=repo_id)
    elif sub == "neighbors":
        out = mem.graph_neighbors(args.symbol_id, repo_id=repo_id)
    else:
        out = mem.graph_impact(args.symbol_id, repo_id=repo_id)
    print(json.dumps(out, indent=2))
    return 0


def _cmd_dispatch(args) -> int:
    from .memory import Memory
    from .workflows import run_workflow
    store = _store(args)
    mem = Memory(store)
    repo_id = None
    try:
        repo = discover_repo(_repo_root_arg(args))
        repo_id = repo_id_for_repo(repo)
    except GitError:
        pass
    if args.run:
        out = run_workflow(store, "dispatch", repo_id=repo_id,
                           run=True, since=args.since,
                           until=args.until, branch=args.branch,
                           allow_external=True)
        print(json.dumps(out, indent=2))
        return 0 if out["status"] != "error" else 1
    out = mem.dispatch(repo_id=repo_id, branch=args.branch,
                       since=args.since, until=args.until)
    print(out["markdown"])
    return 0


_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _resolve_rev(root: str, ref: str) -> str:
    from .git import _git
    proc = _git(root, "rev-parse", "--verify", "--end-of-options",
                ref, check=False)
    if proc.returncode != 0:
        raise GitError(f"cannot resolve revision: {ref!r}")
    return proc.stdout.decode().strip()


def _register_review_doc(store: Store, repo_id: str, base_ref: str,
                         root: str) -> str:
    from .git import _git
    from .memory import document_id
    head = _resolve_rev(root, "HEAD")
    if base_ref == _EMPTY_TREE:
        base = _EMPTY_TREE
    else:
        base = _resolve_rev(root, base_ref)
    # `A...B` needs two commits for merge-base; a first commit is
    # reviewed against the empty tree with a plain two-endpoint diff.
    rng = f"{base}...{head}" if base != _EMPTY_TREE \
        else f"{base}..{head}"
    env = {**os.environ, "GIT_LITERAL_PATHSPECS": "1"}
    proc = _git(
        root, "diff", "--no-ext-diff", "--no-textconv",
        "--name-only", "-z", rng, "--", env=env, check=False)
    if proc.returncode != 0:
        raise GitError("git diff failed for review base")
    paths = [p.decode("utf-8", "replace")
             for p in proc.stdout.split(b"\0") if p]
    excluded = sorted(p for p in paths if is_sensitive_path(p))
    safe = [p for p in paths if not is_sensitive_path(p)]
    # Sensitive paths are excluded from the generated evidence rather
    # than merely redacted: never emit their contents into the doc.
    if safe:
        proc = _git(
            root, "diff", "--no-ext-diff", "--no-textconv",
            rng, "--", *safe, env=env, check=False)
        if proc.returncode != 0:
            raise GitError("git diff failed for review base")
        data = proc.stdout
    else:
        data = b""
    truncated = len(data) > 2 * 1024 * 1024
    diff = data[:2 * 1024 * 1024].decode("utf-8", "replace")
    if truncated:
        diff += "\n[partial: diff truncated at 2 MiB]\n"
    title = f"review {base[:12]}...{head[:10]}"
    note = ""
    if excluded:
        note = ("excluded sensitive paths (not reviewed): "
                + ", ".join(excluded[:50])
                + (" …" if len(excluded) > 50 else "") + "\n")
    text = f"{title}\n{note}{str(redact(diff))[:100000]}"
    did = document_id(repo_id, "checkpoint", f"review:{base}..{head}",
                      None, head, None, text)
    conn = store._connect()
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO memory_documents(id,repo_id,"
                "kind,source_id,title,text,path,line_start,line_end,"
                "commit_sha,updated_at,archived)"
                " VALUES(?,?,?,?,?,?,NULL,NULL,NULL,?,?,0)",
                (did, repo_id, "checkpoint", f"review:{base}..{head}",
                 title, text, head, now_iso()))
            if getattr(store, "fts_ok", True):
                conn.execute(
                    "DELETE FROM memory_fts WHERE id=?", (did,))
                conn.execute(
                    "INSERT INTO memory_fts(id,repo_id,kind,title,"
                    "text) VALUES(?,?,?,?,?)",
                    (did, repo_id, "checkpoint", title, text))
            conn.commit()
    finally:
        conn.close()
    return did


def _cmd_review(args) -> int:
    from .workflows import run_workflow
    store = _store(args)
    if args.review_command == "show":
        run = store.get_run(args.run_id)
        if run is None:
            _warn(f"unknown workflow run: {args.run_id}")
            return 1
        print(json.dumps(run, indent=2))
        return 0
    repo_id = None
    root = None
    try:
        repo = discover_repo(_repo_root_arg(args))
        store.register_repo(repo["root"])
        repo_id = repo_id_for_repo(repo)
        root = repo["root"]
    except GitError:
        pass
    if root is None:
        _warn("review requires a git repository; run inside one"
              " or pass --repo")
        return 2
    base = args.base
    if base is None:
        try:
            _resolve_rev(root, "HEAD^")
            base = "HEAD^"
        except GitError:
            base = _EMPTY_TREE
    try:
        evidence_ids = [
            _register_review_doc(store, repo_id, base, root)]
    except GitError as exc:
        _warn(str(exc))
        return 2
    agents = [a.strip() for a in (args.agents or "").split(",")
              if a.strip()]
    out = run_workflow(store, "review", repo_id=repo_id,
                       query=args.query or "", run=args.run,
                       agents=agents, base=args.base,
                       evidence_ids=evidence_ids,
                       allow_external=bool(args.run))
    print(json.dumps(out, indent=2))
    return 0 if out["status"] != "error" else 1


def _cmd_investigate(args) -> int:
    from .memory import document_id
    from .workflows import run_workflow
    store = _store(args)
    if args.investigate_command == "show":
        run = store.get_run(args.run_id)
        if run is None:
            _warn(f"unknown workflow run: {args.run_id}")
            return 1
        print(json.dumps(run, indent=2))
        return 0
    repo_id = None
    try:
        repo = discover_repo(_repo_root_arg(args))
        store.register_repo(repo["root"])
        repo_id = repo_id_for_repo(repo)
    except GitError:
        pass
    evidence_ids = None
    if args.seed:
        if repo_id is None:
            _warn("investigate --seed requires a repository scope;"
                  " run inside a repo or pass --repo")
            return 2
        sp = Path(args.seed)
        try:
            fd = os.open(sp, os.O_RDONLY | os.O_NOFOLLOW
                         | os.O_NONBLOCK)
        except OSError as exc:
            _warn(str(exc))
            return 2
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                _warn("seed must be a regular file")
                return 2
            data = os.read(fd, 64 * 1024 + 1)
        except OSError as exc:
            _warn(str(exc))
            return 2
        finally:
            os.close(fd)
        if len(data) > 64 * 1024:
            _warn("seed file exceeds 64 KiB")
            return 2
        text = str(redact(data.decode("utf-8", "replace")))
        did = document_id(repo_id, "session",
                          f"seed:{sp.name}", None, None, None, text)
        conn = store._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO memory_documents(id,"
                    "repo_id,kind,source_id,title,text,updated_at,"
                    "archived) VALUES(?,?,'session',?,?,?,?,0)",
                    (did, repo_id, f"seed:{sp.name}",
                     sp.name, text, now_iso()))
                if getattr(store, "fts_ok", True):
                    conn.execute(
                        "DELETE FROM memory_fts WHERE id=?", (did,))
                    conn.execute(
                        "INSERT INTO memory_fts(id,repo_id,kind,title,"
                        "text) VALUES(?,?,'session',?,?)",
                        (did, repo_id, sp.name, text))
                conn.commit()
        finally:
            conn.close()
        evidence_ids = [did]
    agents = [a.strip() for a in (args.agents or "").split(",")
              if a.strip()]
    out = run_workflow(store, "investigate", repo_id=repo_id,
                       query=args.query, run=args.run,
                       agents=agents, evidence_ids=evidence_ids,
                       allow_external=bool(args.run))
    print(json.dumps(out, indent=2))
    return 0 if out["status"] != "error" else 1


def _cmd_experts(args) -> int:
    from .memory import Memory
    store = _store(args)
    mem = Memory(store)
    _repo, repo_id = _register_repo(args, store)
    rows = mem.experts(args.scope, repo_id=repo_id)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"{r['session_id'][:16]} [{r['agent']}]"
                  f" {r['title']} contributions={r['contributions']}")
    return 0


def _cmd_tokens(args) -> int:
    from .brain_contract import usage_totals
    store = _store(args)
    conn = store._connect()
    try:
        if args.session:
            sess = store.get_session(args.session)
            if sess is None:
                _warn("unknown session")
                return 1
            events = sess["events"]
            label = sess["id"]
            totals = usage_totals(events)
            print(json.dumps({"session": label, **totals}, indent=2))
            return 0
        if args.checkpoint:
            cp = store.get_checkpoint(args.checkpoint)
            if cp is None:
                _warn("unknown checkpoint")
                return 1
            seen = set()
            out = []
            for link in cp["links"]:
                sid = link["session_id"]
                if sid in seen:
                    continue
                seen.add(sid)
                sess = store.get_session(sid)
                if sess is None:
                    continue
                out.append({"session": sid,
                            **usage_totals(sess["events"])})
            print(json.dumps({"checkpoint": cp["id"],
                              "sessions": out}, indent=2))
            return 0
        out = []
        for s in store.list_sessions(limit=1000):
            sess = store.get_session(s["id"])
            out.append({"session": s["id"], "title": s["title"],
                        **usage_totals(sess["events"] if sess
                                       else [])})
        print(json.dumps(out, indent=2))
        return 0
    finally:
        conn.close()


def _cmd_mcp(args) -> int:
    from .mcp import serve_mcp
    store = _store(args)
    repo_id = None
    try:
        repo = discover_repo(_repo_root_arg(args))
        repo_id = repo_id_for_repo(repo)
    except GitError:
        pass
    serve_mcp(store, repo_id)
    return 0


_PLUGIN_ENV = ("PATH", "HOME", "LANG", "TERM", "TMPDIR",
               "PARTIAL_HOME")


def _plugins_file(args) -> Path:
    try:
        repo = discover_repo(_repo_root_arg(args))
        return Path(repo["root"]) / ".devin" / "partial" \
            / "plugins.json"
    except GitError:
        return Path.home() / ".config" / "devin" / "partial" \
            / "plugins.json"


def _load_plugins(args) -> dict:
    pf = _plugins_file(args)
    try:
        raw = pf.read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError(f"cannot read plugin config: {exc}") from exc
    try:
        obj = json.loads(raw)
    except ValueError as exc:
        raise ValueError(
            f"malformed plugin config {pf}: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"malformed plugin config {pf}:"
                         " expected a JSON object")
    for name, entry in obj.items():
        if not isinstance(entry, dict) \
                or not isinstance(entry.get("command"), str) \
                or not isinstance(entry.get("sha256"), str):
            raise ValueError(
                f"malformed plugin config {pf}: invalid entry"
                f" for {name!r}")
    return obj


def _is_regular_executable(path: Path) -> bool:
    """True only for an existing regular executable file; symlinks,
    directories, and other non-regular files do not qualify."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    return os.access(path, os.X_OK)


_BUILTIN_COMMANDS = frozenset({
    "enable", "disable", "status", "doctor", "hook", "import",
    "sessions", "session", "checkpoint", "checkpoints", "export",
    "handoff", "sync", "run", "auth", "account", "serve", "upload",
    "ingest-bundle", "capture", "native", "resume", "stop", "attach",
    "why", "blame", "index", "search", "context", "ask", "decision",
    "graph", "dispatch", "recap", "review", "investigate", "experts",
    "tokens", "mcp", "plugin", "project", "configure"})


def _cmd_plugin(args) -> int:
    if args.plugin_command == "register":
        name = args.name
        if not isinstance(name, str) or not name \
                or not name.replace("-", "").replace("_", "") \
                .replace(".", "").isalnum() or len(name) > 64:
            _warn("invalid plugin name")
            return 2
        if name in _BUILTIN_COMMANDS:
            _warn("plugin name shadows a builtin command")
            return 2
        cmd = Path(args.command_path)
        if not cmd.is_absolute():
            _warn("plugin command must be an absolute path")
            return 2
        if not _is_regular_executable(cmd):
            _warn("plugin command must be an existing regular"
                  " executable file (not a symlink or directory)")
            return 2
        if not isinstance(args.sha256, str) or not \
                re.fullmatch(r"[0-9a-f]{64}", args.sha256):
            _warn("--sha256 must be a lowercase sha256 hex digest")
            return 2
        pf = _plugins_file(args)
        plugins = _load_plugins(args)
        plugins[name] = {"command": str(cmd), "sha256": args.sha256}
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(json.dumps(plugins, indent=2))
        try:
            os.chmod(pf, 0o600)
        except OSError:
            pass
        print(f"registered plugin {name}")
        return 0
    if args.plugin_command == "list":
        print(json.dumps(_load_plugins(args), indent=2))
        return 0
    if args.plugin_command == "run":
        plugins = _load_plugins(args)
        entry = plugins.get(args.name)
        if entry is None:
            _warn(f"unknown plugin: {args.name}")
            return 1
        cmd = Path(entry["command"])
        if not cmd.is_absolute() or not _is_regular_executable(cmd):
            _warn("plugin command must be an absolute path to a"
                  " regular executable file")
            return 1
        if not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            _warn("plugin entry has an invalid sha256 digest")
            return 1
        try:
            digest = hashlib.sha256(cmd.read_bytes()).hexdigest()
        except OSError as exc:
            _warn(str(exc))
            return 1
        if digest != entry["sha256"]:
            _warn("plugin digest mismatch; refusing to run")
            return 1
        env = {k: os.environ[k] for k in _PLUGIN_ENV
               if k in os.environ}
        env["PARTIAL_HOME"] = str(_home_dir(args))
        rest = list(args.rest or [])
        if rest and rest[0] == "--":
            rest = rest[1:]
        try:
            return subprocess.call(
                [str(cmd), *rest], env=env, timeout=300)
        except subprocess.TimeoutExpired:
            _warn("plugin timed out")
            return 124
        except FileNotFoundError:
            _warn("plugin command not found")
            return 1
        except KeyboardInterrupt:
            return 130
    return 0


def _cmd_project(args) -> int:
    store = _store(args)
    if args.project_command == "create":
        p = store.create_project(args.name)
        print(json.dumps(p, indent=2))
        return 0
    if args.project_command == "attach":
        repo_id = args.repo_id
        if not repo_id:
            _repo, repo_id = _register_repo(args, store)
        try:
            p = store.attach_project_repo(args.project_id, repo_id)
        except KeyError:
            _warn("unknown project or repository")
            return 1
        print(json.dumps(p, indent=2))
        return 0
    print(json.dumps(store.list_projects(), indent=2))
    return 0


def _cmd_configure(args) -> int:
    from .ai import OpenAIProvider
    store = _store(args)
    if args.show:
        provider = OpenAIProvider()
        out = {
            "home": str(_home_dir(args)),
            "db": str(store.path),
            "ai_provider_configured": provider.configured,
            "ai_base": provider.base,
            "external_ai_enabled": store.memory_setting(
                "external_ai_enabled") == "true",
            "plugins_file": str(_plugins_file(args)),
            "plugins": sorted(_load_plugins(args)),
            "fts5": getattr(store, "fts_ok", False),
        }
        print(json.dumps(out, indent=2))
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args))
    except BrokenPipeError:
        return 0
    except (ValueError, OSError, sqlite3.Error, GitError,
            subprocess.SubprocessError) as exc:
        _warn(f"error: {exc}")
        if getattr(args, "command", "") == "hook":
            return 0
        return 1
