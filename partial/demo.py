from __future__ import annotations

import tempfile
from pathlib import Path

from .models import Event, scoped_session_id, sha256_hex
from .store import Store

DEMO_BASE = "2026-09-16T09:"


def _ts(minute: int, second: int = 0) -> str:
    return f"{DEMO_BASE}{minute:02d}:{second:02d}.000000Z"


def _repo(store: Store, name: str) -> str:
    rid = sha256_hex(f"partial-demo:{name}")
    conn = store._connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO repositories(id,name,root,remote,"
            "created_at) VALUES(?,?,?,?,?)",
            (rid, name, None, "", _ts(0)),
        )
        conn.commit()
    finally:
        conn.close()
    return rid


def _ev(i: int, sid: str, agent: str, kind: str, minute: int,
        second: int = 0, text: str = "", tool_name=None,
        data=None) -> Event:
    return Event(
        id=sha256_hex(f"partial-demo:ev:{sid}:{i}"),
        session_id=sid, agent=agent, kind=kind,
        timestamp=_ts(minute, second), text=text,
        tool_name=tool_name, data=data or {},
    )


def create_demo_store() -> Store:
    tmp = Path(tempfile.mkdtemp(prefix="partial-demo-"))
    store = Store(tmp / "partial.db")
    orbit = _repo(store, "orbit-api")
    design = _repo(store, "design-system")

    devin_sid = "demo-devin-1"
    store.ingest(orbit, [
        _ev(0, devin_sid, "devin", "session_start", 0),
        _ev(1, devin_sid, "devin", "prompt", 0, 10,
            text="Add cursor pagination to the activity feed."),
        _ev(2, devin_sid, "devin", "tool", 1, 0,
            tool_name="edit", data={
                "tool_input": {
                    "file_path": "src/routes/activity.py",
                    "old_string": "limit = 20",
                    "new_string": "limit = min(requested_limit, 100)",
                },
                "tool_response": {"success": True},
            }),
        _ev(3, devin_sid, "devin", "response", 2, 0,
            text="The endpoint now returns a stable cursor and"
                 " preserves the requested page size."),
    ], branch="main")

    codex_sid = "demo-codex-1"
    store.ingest(orbit, [
        _ev(0, codex_sid, "codex", "session_start", 3),
        _ev(1, codex_sid, "codex", "prompt", 3, 20,
            text="Cover expired access tokens with a regression test."),
        _ev(2, codex_sid, "codex", "tool", 4, 0,
            tool_name="file_change", data={
                "changes": [{"path": "tests/test_auth.py",
                             "kind": "update"}],
            }),
        _ev(3, codex_sid, "codex", "response", 5, 0,
            text="Added coverage for expired tokens and verified that"
                 " refresh failures are surfaced."),
    ], branch="main")

    claude_sid = "demo-claude-1"
    store.ingest(design, [
        _ev(0, claude_sid, "claude", "session_start", 6),
        _ev(1, claude_sid, "claude", "prompt", 6, 15,
            text="Make the command palette usable with the keyboard."),
        _ev(2, claude_sid, "claude", "response", 7, 30,
            text="Arrow keys now move the selection and Escape closes"
                 " the palette."),
    ], branch="main")

    chatgpt_sid = "demo-chatgpt-1"
    store.ingest(design, [
        _ev(0, chatgpt_sid, "chatgpt", "session_start", 8),
        _ev(1, chatgpt_sid, "chatgpt", "prompt", 8, 10,
            text="Explain the trade-offs of cursor pagination."),
        _ev(2, chatgpt_sid, "chatgpt", "response", 9, 0,
            text="Cursors remain stable as new records are inserted;"
                 " page numbers are easier to navigate directly."),
    ], branch="main")

    diff_activity = (
        "diff --git a/src/routes/activity.py b/src/routes/activity.py\n"
        "--- a/src/routes/activity.py\n"
        "+++ b/src/routes/activity.py\n"
        "@@ -40,7 +40,7 @@ def activity_feed(request):\n"
        "-    limit = 20\n"
        "+    limit = min(requested_limit, 100)\n"
        "     cursor = request.args.get('cursor')\n"
    )
    diff_auth = (
        "diff --git a/tests/test_auth.py b/tests/test_auth.py\n"
        "--- a/tests/test_auth.py\n"
        "+++ b/tests/test_auth.py\n"
        "@@ -88,3 +88,12 @@ def test_refresh_rotates_token():\n"
        "+def test_expired_access_token_rejected():\n"
        "+    token = issue_token(ttl=-1)\n"
        "+    assert not validate_access(token)\n"
    )
    diff_palette = (
        "diff --git a/src/ui/palette.ts b/src/ui/palette.ts\n"
        "--- a/src/ui/palette.ts\n"
        "+++ b/src/ui/palette.ts\n"
        "@@ -12,6 +12,9 @@ export function paletteKeys(e) {\n"
        "+  if (e.key === 'ArrowDown') select(index + 1);\n"
        "+  if (e.key === 'ArrowUp') select(index - 1);\n"
        "+  if (e.key === 'Escape') close();\n"
    )

    store.save_checkpoint(
        orbit, sha256_hex("partial-demo:checkpoint:1")[:32],
        sha256_hex("partial-demo:commit:1")[:40],
        branch="main",
        message="Add cursor pagination to the activity feed",
        author="Partial Demo <demo@localhost>",
        files=["src/routes/activity.py"], diff=diff_activity,
        links=[(scoped_session_id(orbit, "devin", devin_sid),
                "observed-worktree-overlap")],
        worktree=None, created_at=_ts(2, 30),
    )
    store.save_checkpoint(
        orbit, sha256_hex("partial-demo:checkpoint:2")[:32],
        sha256_hex("partial-demo:commit:2")[:40],
        branch="main",
        message="Cover expired access tokens with a regression test",
        author="Partial Demo <demo@localhost>",
        files=["tests/test_auth.py"], diff=diff_auth,
        links=[(scoped_session_id(orbit, "codex", codex_sid),
                "observed-worktree-overlap")],
        worktree=None, created_at=_ts(5, 30),
    )
    store.save_checkpoint(
        design, sha256_hex("partial-demo:checkpoint:3")[:32],
        sha256_hex("partial-demo:commit:3")[:40],
        branch="main",
        message="Keyboard navigation for the command palette",
        author="Partial Demo <demo@localhost>",
        files=["src/ui/palette.ts"], diff=diff_palette,
        links=[(scoped_session_id(design, "claude", claude_sid),
                "observed-worktree-overlap")],
        worktree=None, created_at=_ts(8, 0),
    )
    return store
