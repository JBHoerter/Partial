from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import __version__
from .adapters import codex_event, normalize_hook, parse_import
from .git import (
    GitError,
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
    sync_checkpoints,
)
from .models import (
    AGENTS,
    MAX_IMPORT_BYTES,
    Event,
    new_event_id,
    now_iso,
    scoped_session_id,
)
from .privacy import redact
from .store import Store

HOOK_STDIN_CAP = 2 * 1024 * 1024
LINE_CAP = 1024 * 1024


def _store(args) -> Store:
    if args.home:
        return Store(Path(args.home) / "partial.db")
    return Store()


def _repo_root_arg(args) -> str:
    return args.repo or os.environ.get("DEVIN_PROJECT_DIR") or "."


def _register_repo(args, store: Store) -> tuple[dict, str]:
    repo = discover_repo(_repo_root_arg(args))
    row = store.register_repo(repo["root"])
    return repo, row["id"]


def _warn(msg: str) -> None:
    print(f"partial: {redact(str(msg))}", file=sys.stderr)


def _cmd_enable(args) -> int:
    store = _store(args)
    repo, _rid = _register_repo(args, store)
    agents = args.agent or ["devin"]
    if "all" in agents:
        agents = list(AGENTS)
    errors = []
    git_err = check_git_hook(repo)
    if git_err:
        errors.append(git_err)
    errors += check_agent_configs(
        repo, [a for a in agents if a in ("devin", "claude")])
    if errors:
        for e in errors:
            _warn(e)
        return 2
    result = install_hooks(repo)
    for e in result["errors"]:
        _warn(e)
    if result["errors"]:
        return 2
    installed = list(result["installed"])
    for agent in agents:
        if agent == "devin":
            installed += install_devin_hooks(repo)["installed"]
        elif agent == "claude":
            installed += install_claude_hooks(repo)["installed"]
        elif agent == "codex":
            print("codex: sessions are captured via 'partial run codex'"
                  " or 'partial import --agent codex'")
        elif agent == "chatgpt":
            print("chatgpt: sessions are captured via"
                  " 'partial import --agent chatgpt'")
    print("enabled: " + (", ".join(installed) or "nothing"))
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


def _cmd_doctor(args) -> int:
    checks = []
    checks.append({
        "check": "git", "ok": shutil.which("git") is not None,
        "detail": shutil.which("git") or "git not found on PATH",
    })
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
    try:
        events = normalize_hook(args.hook_agent, payload, args.event)
    except ValueError as exc:
        _warn(f"hook payload rejected: {exc}")
        return 0
    if not events:
        return 0
    store = _store(args)
    row = store.register_repo(repo["root"])
    store.ingest(
        row["id"], events,
        worktree=repo["root"], branch=repo.get("branch"),
    )
    _refresh_session_bundles(store, repo, row["id"], events)
    return 0


def _hook_git(args) -> int:
    try:
        repo = discover_repo(_repo_root_arg(args))
    except GitError as exc:
        _warn(f"git hook skipped: {exc}")
        return 0
    if not load_local_config(repo).get("enabled"):
        return 0
    store = _store(args)
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
    except (GitError, ValueError) as exc:
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
    out = []
    title = sess.get("title") or sess["native_id"]
    out.append(f"# Handoff: {title}")
    out.append("")
    out.append(f"- agent: {sess['agent']}")
    out.append(f"- status: {sess['status']}")
    if sess.get("branch"):
        out.append(f"- branch: {sess['branch']}")
    out.append(f"- started: {sess.get('started_at') or 'unknown'}")
    out.append("")
    for ev in sess["events"]:
        out.append(f"## {ev['kind']} — {ev['timestamp']}")
        if ev.get("tool_name"):
            out.append(f"tool: {ev['tool_name']}")
            data = ev.get("data") or {}
            if data.get("tool_input") is not None:
                out.append("```json")
                out.append(json.dumps(data["tool_input"], indent=2)[:4000])
                out.append("```")
        if ev.get("text"):
            out.append(ev["text"])
        out.append("")
    print("\n".join(out))
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
        print(f"pushed={result['pushed']} pulled={result['pulled']}")
    for e in result["import_errors"]:
        _warn(e)
    if result["error"]:
        _warn(result["error"])
    if result["error"] or result["import_errors"]:
        return 2
    return 0


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
    env["PARTIAL_HOME"] = str(store.path.parent)
    return env, repo["root"], repo, row["id"]


def _cmd_run_codex(args) -> int:
    store = _store(args)
    env, cwd, repo, repo_id = _run_env_cwd(args, store)
    cmd = ["codex", "exec", "--json", *args.rest]
    sid = ""
    seq = 0
    turn = 0
    prompt_text = _codex_prompt_from(args.rest)

    def ingest_now(evs):
        if evs:
            store.ingest(
                repo_id, evs, worktree=repo["root"],
                branch=repo.get("branch"))

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
            evs = codex_event(payload, sid or "codex")
            for ev in evs:
                base = ev.id
                s = sid or "codex"
                if base.startswith(s + ":"):
                    base = base[len(s) + 1:]
                ev.session_id = s
                ev.id = f"{s}:t{turn}:l{seq}:{base}"
            ingest_now(evs)
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
    h.add_argument("hook_agent", choices=["devin", "claude", "git"])
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

    return p


def _cmd_run(args) -> int:
    if args.run_agent == "codex":
        return _cmd_run_codex(args)
    if args.run_agent == "claude":
        return _cmd_run_claude(args)
    return _cmd_run_devin(args)


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
