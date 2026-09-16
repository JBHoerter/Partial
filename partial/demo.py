from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Event, scoped_session_id, sha256_hex
from .store import Store

def _ts(minute: int, second: int = 0) -> str:
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    ts = base + timedelta(minutes=minute, seconds=second)
    return ts.isoformat().replace("+00:00", "Z")


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
        data=None, parent_session_id=None, model=None) -> Event:
    return Event(
        id=sha256_hex(f"partial-demo:ev:{sid}:{i}"),
        session_id=sid, agent=agent, kind=kind,
        timestamp=_ts(minute, second), text=text,
        tool_name=tool_name, data=data or {},
        parent_session_id=parent_session_id, model=model,
    )


def create_demo_store() -> Store:
    tmp = Path(tempfile.mkdtemp(prefix="partial-demo-"))
    store = Store(tmp / "partial.db")
    orbit = _repo(store, "orbit-api")
    design = _repo(store, "design-system")

    devin_sid = "demo-devin-1"
    store.ingest(orbit, [
        _ev(0, devin_sid, "devin", "session_start", 0,
            model="SWE-2 Max"),
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
        _ev(3, devin_sid, "devin", "usage", 1, 30,
            data={"usage": {"input_tokens": 4200,
                            "output_tokens": 740,
                            "cached_input_tokens": 900},
                  "usage_scope": "delta", "usage_id": "step-3"}),
        _ev(4, devin_sid, "devin", "response", 2, 0,
            text="The endpoint now returns a stable cursor and"
                 " preserves the requested page size."),
    ], branch="main")

    devin_child = "demo-devin-1:subagent:review"
    store.ingest(orbit, [
        _ev(0, devin_child, "devin", "session_start", 1, 5,
            parent_session_id=devin_sid, model="SWE-2 Max"),
        _ev(1, devin_child, "devin", "prompt", 1, 10,
            text="Review the pagination edge cases before the edit.",
            parent_session_id=devin_sid),
        _ev(2, devin_child, "devin", "response", 1, 40,
            text="Checked cursor bounds and descending-order edge cases.",
            parent_session_id=devin_sid),
    ], branch="main")

    codex_sid = "demo-codex-1"
    store.ingest(orbit, [
        _ev(0, codex_sid, "codex", "session_start", 3,
            model="GPT-5 Codex"),
        _ev(1, codex_sid, "codex", "prompt", 3, 20,
            text="Cover expired access tokens with a regression test."),
        _ev(2, codex_sid, "codex", "tool", 4, 0,
            tool_name="file_change", data={
                "changes": [{"path": "tests/test_auth.py",
                             "kind": "update"}],
            }),
        _ev(3, codex_sid, "codex", "usage", 4, 20,
            data={"usage": {"input_tokens": 3100,
                            "output_tokens": 520},
                  "usage_scope": "delta", "usage_id": "turn-1"}),
        _ev(4, codex_sid, "codex", "response", 5, 0,
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
        "+    issued = issue_token(ttl=-1)\n"
        "+    assert not validate_access(issued)\n"
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
                "observed-worktree-overlap"),
               (scoped_session_id(orbit, "devin", devin_child),
                "subagent-transcript")],
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

    demo_cp = sha256_hex("partial-demo:checkpoint:1")[:32]
    devin_scoped = scoped_session_id(orbit, "devin", devin_sid)
    conn = store._connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO checkpoint_attribution"
            "(checkpoint_id,report) VALUES(?,?)",
            (demo_cp, json.dumps({
                "version": 1,
                "method": "position-aware-snapshot-diff-v1",
                "capture_source": "local-observation",
                "summary": {
                    "agent_added": 1, "agent_removed": 1,
                    "human_added": 0, "human_removed": 0,
                    "unknown_added": 0, "unknown_removed": 0,
                    "total_changed": 2,
                    "agent_percentage": 100.0,
                    "coverage_percentage": 100.0},
                "files": [{
                    "path": "src/routes/activity.py",
                    "lines": [
                        {"side": "old", "line": 40,
                         "kind": "agent",
                         "session_id": devin_scoped,
                         "evidence": "pre/post tool snapshot"},
                        {"side": "new", "line": 40,
                         "kind": "agent",
                         "session_id": devin_scoped,
                         "evidence": "pre/post tool snapshot"},
                    ]}],
                "excluded": [],
                "limitations": [
                    "Synthetic demo attribution; real captures require"
                    " pre/post tool snapshots."],
            })))
        conn.commit()
    finally:
        conn.close()

    from .memory import Memory, document_id
    mem = Memory(store)
    mem.index(None)
    conn = store._connect()
    try:
        # Synthetic Python sample derived from the demo diff text;
        # not live repository content. The source_id is the real git
        # blob SHA-1 of this content and commit_sha/lines match the
        # indexed-file shape so the document satisfies the bundle
        # import invariants for code documents.
        code = (
            "def activity_feed(request):\n"
            "    limit = min(requested_limit, 100)\n"
            "    cursor = request.args.get('cursor')\n"
            "    return render(request, 'feed.html')\n")
        code_bytes = code.encode("utf-8")
        blob = hashlib.sha1(
            b"blob %d\0" % len(code_bytes) + code_bytes).hexdigest()
        csha = sha256_hex("partial-demo:commit:1")[:40]
        did = document_id(orbit, "code", blob,
                          "src/routes/activity.py", csha, 1, code)
        conn.execute(
            "INSERT OR REPLACE INTO memory_documents(id,repo_id,"
            "kind,source_id,title,text,path,line_start,line_end,"
            "commit_sha,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (did, orbit, "code", blob,
             "src/routes/activity.py (synthetic demo)", code,
             "src/routes/activity.py", 1, 4, csha,
             _ts(10)))
        conn.execute(
            "INSERT OR REPLACE INTO repository_indexes(repo_id,"
            "commit_sha,indexed_at) VALUES(?,?,?)",
            (orbit, csha, _ts(10)))
        if getattr(store, "fts_ok", True):
            conn.execute(
                "INSERT INTO memory_fts(id,repo_id,kind,title,text)"
                " VALUES(?,?,?,?,?)",
                (did, orbit, "code",
                 "src/routes/activity.py (synthetic demo)", code))
        from .memory import _symbol_id
        mod = _symbol_id(orbit, "src/routes/activity.py",
                         "src.routes.activity")
        fn = _symbol_id(orbit, "src/routes/activity.py",
                        "activity_feed")
        for sid_, name, qn, kind in (
                (mod, "src.routes.activity", "src.routes.activity",
                 "module"),
                (fn, "activity_feed", "activity_feed", "function")):
            conn.execute(
                "INSERT OR REPLACE INTO graph_symbols(id,repo_id,"
                "path,name,qualified_name,kind,line,end_line,"
                "language,analysis,commit_sha)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (sid_, orbit, "src/routes/activity.py", name, qn,
                 kind, 1, 4, "python", "ast", csha))
        conn.commit()
        cp_doc = conn.execute(
            "SELECT id FROM memory_documents WHERE kind='checkpoint'"
            " AND source_id=?",
            (sha256_hex("partial-demo:checkpoint:1")[:32],)
        ).fetchone()
    finally:
        conn.close()
    if cp_doc:
        mem.add_decision(
            orbit,
            "Cursor pagination for the activity feed",
            "Use stable cursors instead of page numbers; cap the"
            " requested page size at 100.",
            [cp_doc["id"]], author="Partial Demo")
    return store
