from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
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
from .privacy import redact
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
    store = _store_for_repo(args, repo)
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
    opener = urllib.request.build_opener(_NoRedirect())
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
