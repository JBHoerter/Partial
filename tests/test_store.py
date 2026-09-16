import json
import unittest
from pathlib import Path

from partial.models import Event, scoped_session_id
from partial.store import (
    Store, normalize_remote, sanitize_remote, unified_diff_delta,
)

from helpers import RepoTestCase, init_repo

TS = "2026-01-01T00:00:00Z"


def ev(id_, sid, kind, ts=TS, text="", agent="devin", **kw):
    return Event(id=id_, session_id=sid, agent=agent, kind=kind,
                 timestamp=ts, text=text, **kw)


def tool_ev(id_, sid, path, tool="edit_file", resp=None):
    data = {"tool_input": {"file_path": path}}
    if resp is not None:
        data["tool_response"] = resp
    return Event(id=id_, session_id=sid, agent="devin", kind="tool",
                 timestamp=TS, tool_name=tool, data=data)


class StoreTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.home / "partial.db")
        self.repo_row = self.store.register_repo(self.repo)
        self.rid = self.repo_row["id"]
        self.wt = str(Path(self.repo).resolve())

    def test_ingest_list_get(self):
        events = [
            ev("e1", "s1", "session_start"),
            ev("e2", "s1", "prompt", text="fix the flaky test"),
            ev("e3", "s1", "response", text="done"),
            ev("e4", "s1", "session_end"),
        ]
        inserted = self.store.ingest(
            self.rid, events, worktree=str(self.repo), branch="main")
        self.assertEqual(len(inserted), 4)
        sessions = self.store.list_sessions(repo_id=self.rid)
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s["status"], "ended")
        self.assertEqual(s["title"], "fix the flaky test")
        self.assertEqual(s["native_id"], "s1")
        full = self.store.get_session(s["id"])
        self.assertEqual(len(full["events"]), 4)
        self.assertEqual(full["events"][1]["kind"], "prompt")

    def test_same_timestamp_order_and_status(self):
        events = [
            ev("e1", "s1", "prompt", text="t"),
            ev("e2", "s1", "tool", tool_name="edit_file",
               data={"tool_input": {"file_path": "a.py"}}),
            ev("e3", "s1", "response", text="r"),
            ev("e4", "s1", "session_end"),
        ]
        self.store.ingest(self.rid, events, worktree=self.wt)
        full = self.store.get_session(
            scoped_session_id(self.rid, "devin", "s1"))
        self.assertEqual(
            [e["kind"] for e in full["events"]],
            ["prompt", "tool", "response", "session_end"])
        self.assertEqual(full["status"], "ended")
        self.assertEqual(full["title"], "t")

    def test_response_marks_idle_not_active(self):
        self.store.ingest(self.rid, [
            ev("e1", "s1", "prompt", "2026-01-01T00:00:00Z", "go"),
            ev("e2", "s1", "response", "2026-01-01T00:00:01Z", "done"),
        ])
        s = self.store.list_sessions()[0]
        self.assertEqual(s["status"], "idle")

    def test_ingest_idempotent(self):
        events = [ev("e1", "s1", "prompt", text="hi")]
        self.assertEqual(len(self.store.ingest(self.rid, events)), 1)
        self.assertEqual(self.store.ingest(self.rid, events), [])
        full = self.store.get_session(
            scoped_session_id(self.rid, "devin", "s1"))
        self.assertEqual(len(full["events"]), 1)
        self.assertEqual(full["status"], "active")

    def test_duplicate_does_not_touch_session(self):
        self.store.ingest(self.rid, [
            ev("e1", "s1", "session_end", "2026-01-02T00:00:00Z")])
        self.store.ingest(self.rid, [
            ev("e2", "s1", "prompt", "2026-01-01T00:00:00Z", "old")])
        self.store.ingest(self.rid, [
            ev("e1", "s1", "session_end", "2026-01-02T00:00:00Z")])
        s = self.store.list_sessions()[0]
        self.assertEqual(s["status"], "ended")
        self.assertEqual(s["updated_at"], "2026-01-02T00:00:00.000000+00:00")

    def test_scoped_session_ids(self):
        self.store.ingest(self.rid, [ev("a", "s1", "prompt", agent="devin")])
        self.store.ingest(self.rid, [ev("b", "s1", "prompt", agent="claude")])
        sessions = self.store.list_sessions()
        self.assertEqual(len(sessions), 2)
        self.assertNotEqual(sessions[0]["id"], sessions[1]["id"])
        other = init_repo(self.tmp / "repo2")
        row2 = self.store.register_repo(other)
        self.assertNotEqual(row2["id"], self.rid)
        self.store.ingest(row2["id"], [ev("c", "s1", "prompt")])
        self.assertEqual(len(self.store.list_sessions()), 3)

    def test_ambiguous_native_lookup(self):
        self.store.ingest(self.rid, [ev("a", "s1", "prompt", agent="devin")])
        self.store.ingest(self.rid, [ev("b", "s1", "prompt", agent="claude")])
        with self.assertRaises(ValueError):
            self.store.get_session("s1")
        sid = scoped_session_id(self.rid, "devin", "s1")
        self.assertEqual(self.store.get_session(sid)["native_id"], "s1")

    def test_resolve_session(self):
        self.store.ingest(self.rid, [ev("a", "s1", "prompt")])
        sid = scoped_session_id(self.rid, "devin", "s1")
        self.assertEqual(self.store.resolve_session(self.rid, sid), sid)
        self.assertEqual(self.store.resolve_session(self.rid, "s1"), sid)
        with self.assertRaises(ValueError):
            self.store.resolve_session(self.rid, "nope")
        other = init_repo(self.tmp / "repo3")
        row2 = self.store.register_repo(other)
        with self.assertRaises(ValueError):
            self.store.resolve_session(row2["id"], sid)
        self.store.ingest(
            self.rid, [ev("b", "s1", "prompt", agent="claude")])
        with self.assertRaises(ValueError):
            self.store.resolve_session(self.rid, "s1")

    def test_redaction_on_ingest(self):
        self.store.ingest(self.rid, [ev(
            "e1", "s1", "tool", tool_name="edit_file",
            data={"tool_input": {"command": "x",
                                 "api_key": "sk-secret12345"}},
            text="token sk-secret12345")])
        full = self.store.get_session(
            scoped_session_id(self.rid, "devin", "s1"))
        blob = json.dumps(full)
        self.assertNotIn("sk-secret12345", blob)
        self.assertIn("[REDACTED]", blob)

    def _pending(self, files):
        return self.store.pending_links(self.rid, set(files), self.wt)

    def test_pending_path_capture(self):
        self.store.ingest(self.rid, [tool_ev(
            "e1", "s1", str(self.repo / "a.py"))], worktree=self.wt)
        sid = scoped_session_id(self.rid, "devin", "s1")
        self.assertEqual(
            self._pending(["a.py"]),
            [(sid, "observed-worktree-overlap")])

    def test_pending_rules(self):
        sid = scoped_session_id(self.rid, "devin", "s1")
        events = [
            tool_ev("ok", "s1", str(self.repo / "yes.py")),
            tool_ev("fail", "s1", str(self.repo / "no1.py"),
                    resp={"success": False}),
            tool_ev("failerr", "s1", str(self.repo / "no2.py"),
                    resp={"is_error": True}),
            tool_ev("readonly", "s1", str(self.repo / "no3.py"),
                    tool="read_notebook"),
            tool_ev("proc", "s1", str(self.repo / "no4.py"),
                    tool="write_to_process"),
            tool_ev("escape", "s1", "../outside.py"),
            tool_ev("rel", "s1", "rel.py"),
        ]
        self.store.ingest(self.rid, events, worktree=self.wt)
        self.assertEqual(
            self._pending(["yes.py"]),
            [(sid, "observed-worktree-overlap")])
        self.assertEqual(
            self._pending(["no1.py", "no2.py", "no3.py", "no4.py",
                           "outside.py"]), [])
        self.assertEqual(
            self._pending(["rel.py"]),
            [(sid, "observed-worktree-overlap")])

    def test_no_pending_without_worktree_or_flag(self):
        self.store.ingest(self.rid, [tool_ev(
            "e1", "s1", str(self.repo / "a.py"))], worktree=None)
        self.store.ingest(self.rid, [tool_ev(
            "e2", "s2", str(self.repo / "b.py"))], worktree=self.wt,
            track_paths=False)
        self.assertEqual(self._pending(["a.py", "b.py"]), [])

    def test_duplicate_event_no_relink(self):
        e = tool_ev("e1", "s1", str(self.repo / "a.py"))
        self.store.ingest(self.rid, [e], worktree=self.wt)
        sid = scoped_session_id(self.rid, "devin", "s1")
        self.store.save_checkpoint(
            self.rid, "a" * 32, "b" * 40, branch="main", message="m",
            author="t", files=["a.py"], diff=None,
            links=[(sid, "observed-worktree-overlap")], worktree=self.wt)
        self.assertEqual(self._pending(["a.py"]), [])
        self.store.ingest(self.rid, [e], worktree=self.wt)
        self.assertEqual(self._pending(["a.py"]), [])

    def test_export_import_roundtrip(self):
        events = [
            ev("e1", "s1", "prompt", text="hello"),
            ev("e2", "s1", "response", text="world"),
        ]
        self.store.ingest(self.rid, events, worktree=self.wt)
        bundle = self.store.export_bundle()
        self.assertEqual(bundle["version"], 1)
        self.assertEqual(len(bundle["sessions"]), 1)
        self.assertNotIn("worktree", bundle["sessions"][0])
        self.assertNotIn("root", bundle["repositories"][0])
        fresh = Store(self.tmp / "fresh.db")
        result = fresh.import_bundle(bundle)
        self.assertEqual(result["events"], 2)
        self.assertEqual(fresh.stats()["sessions"], 1)
        again = fresh.import_bundle(bundle)
        self.assertEqual(again["events"], 0)
        imported = fresh.get_session(
            scoped_session_id(self.rid, "devin", "s1"))
        self.assertEqual(len(imported["events"]), 2)
        self.assertEqual(
            [e["kind"] for e in imported["events"]],
            ["prompt", "response"])
        repo_row = [r for r in fresh.list_repos()
                    if r["id"] == self.rid][0]
        self.assertIsNone(repo_row["root"])

    def test_import_rejects_forged_scope(self):
        self.store.ingest(self.rid, [ev("e1", "s1", "prompt", text="hi")])
        bundle = self.store.export_bundle()
        forged = json.loads(json.dumps(bundle))
        forged["sessions"][0]["native_id"] = "other"
        with self.assertRaises(ValueError):
            Store(self.tmp / "f.db").import_bundle(forged)
        forged2 = json.loads(json.dumps(bundle))
        forged2["sessions"][0]["id"] = "f" * 64
        with self.assertRaises(ValueError):
            Store(self.tmp / "f2.db").import_bundle(forged2)

    def test_import_rejects_bad_shapes(self):
        self.store.ingest(self.rid, [ev("e1", "s1", "prompt", text="hi")])
        good = self.store.export_bundle()
        cases = []
        b = json.loads(json.dumps(good))
        b["sessions"][0]["status"] = "sleeping"
        cases.append(b)
        b = json.loads(json.dumps(good))
        b["sessions"][0]["updated_at"] = "not-a-time"
        cases.append(b)
        b = json.loads(json.dumps(good))
        b["events"] = [{"id": "x"}]
        cases.append(b)
        b = json.loads(json.dumps(good))
        b["events"] = "nope"
        cases.append(b)
        b = json.loads(json.dumps(good))
        b["events"][0]["agent"] = "claude"
        cases.append(b)
        b = json.loads(json.dumps(good))
        b["events"][0]["session_id"] = "0" * 64
        cases.append(b)
        b = json.loads(json.dumps(good))
        b["checkpoints"] = [{
            "id": "z" * 32, "repo_id": self.rid,
            "commit_sha": "c" * 40, "files": ["../evil.py"],
            "session_ids": [], "created_at": TS}]
        cases.append(b)
        b = json.loads(json.dumps(good))
        b["checkpoints"] = [{
            "id": "z" * 32, "repo_id": self.rid,
            "commit_sha": "c" * 40, "files": ["/abs.py"],
            "session_ids": [], "created_at": TS}]
        cases.append(b)
        b = json.loads(json.dumps(good))
        b["checkpoints"] = [{
            "id": "nothex!!", "repo_id": self.rid,
            "commit_sha": "c" * 40, "files": [], "session_ids": [],
            "created_at": TS}]
        cases.append(b)
        for i, bad in enumerate(cases):
            store = Store(self.tmp / f"bad{i}.db")
            with self.assertRaises(ValueError, msg=f"case {i}"):
                store.import_bundle(bad)
            self.assertEqual(store.stats()["sessions"], 0,
                             f"case {i} leaked writes")

    def test_import_checkpoint_crossrepo_and_collisions(self):
        self.store.ingest(self.rid, [ev("e1", "s1", "prompt", text="x")])
        other = init_repo(self.tmp / "repoB")
        row2 = self.store.register_repo(other)
        self.store.ingest(row2["id"], [ev(
            "e9", "s2", "prompt", text="y")])
        sid1 = scoped_session_id(self.rid, "devin", "s1")
        sid2 = scoped_session_id(row2["id"], "devin", "s2")
        bundle = self.store.export_bundle()
        b = json.loads(json.dumps(bundle))
        b["checkpoints"] = [{
            "id": "a" * 32, "repo_id": self.rid, "commit_sha": "c" * 40,
            "files": ["f.py"], "session_ids": [sid2], "created_at": TS}]
        with self.assertRaises(ValueError):
            self.store.import_bundle(b)
        b = json.loads(json.dumps(bundle))
        b["checkpoints"] = [{
            "id": "a" * 32, "repo_id": self.rid, "commit_sha": "c" * 40,
            "files": ["f.py"], "session_ids": [sid1], "created_at": TS}]
        self.store.import_bundle(b)
        b2 = json.loads(json.dumps(b))
        b2["checkpoints"][0]["commit_sha"] = "d" * 40
        with self.assertRaises(ValueError):
            self.store.import_bundle(b2)
        b3 = json.loads(json.dumps(bundle))
        b3["checkpoints"] = [{
            "id": "b" * 32, "repo_id": self.rid, "commit_sha": "c" * 40,
            "files": ["f.py"], "session_ids": [sid1], "created_at": TS}]
        with self.assertRaises(ValueError):
            self.store.import_bundle(b3)

    def test_import_sanitizes_metadata(self):
        self.store.ingest(self.rid, [ev("e1", "s1", "prompt", text="x")])
        bundle = self.store.export_bundle()
        bundle["sessions"][0]["title"] = "password=sk-abc9999zz"
        bundle["repositories"][0]["name"] = "repo ghp_0123456789abcdefgh"
        fresh = Store(self.tmp / "san.db")
        fresh.import_bundle(bundle)
        s = fresh.list_sessions()[0]
        self.assertNotIn("sk-abc9999zz", s["title"])
        r = fresh.list_repos()[0]
        self.assertNotIn("ghp_0123456789", r["name"])

    def test_stats_counts(self):
        self.store.ingest(self.rid, [ev("e1", "s1", "prompt", text="hi")])
        stats = self.store.stats()
        self.assertEqual(
            stats, {"sessions": 1, "checkpoints": 0, "repositories": 1})

    def test_newer_timestamp_wins(self):
        self.store.ingest(self.rid, [ev(
            "e1", "s1", "prompt", "2026-01-02T00:00:00Z", "new title")])
        self.store.ingest(self.rid, [ev(
            "e2", "s1", "prompt", "2026-01-01T00:00:00Z", "old title")])
        s = self.store.list_sessions()[0]
        self.assertEqual(s["title"], "new title")


class CheckpointBundleTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.home / "partial.db")
        self.rid = self.store.register_repo(self.repo)["id"]
        self.wt = str(Path(self.repo).resolve())

    def test_bundle_contains_linked_transcript(self):
        sid = scoped_session_id(self.rid, "devin", "s1")
        child = scoped_session_id(self.rid, "devin", "s2")
        unrelated = scoped_session_id(self.rid, "devin", "s9")
        self.store.ingest(self.rid, [
            ev("p1", "s1", "prompt", text="q"),
            ev("t1", "s1", "tool", tool_name="edit_file",
               data={"tool_input": {"file_path": "f.py"}}),
            ev("r1", "s1", "response", text="a"),
            Event(id="c1", session_id="s2", agent="devin", kind="prompt",
                  timestamp=TS, text="child", parent_session_id="s1"),
            ev("u1", "s9", "prompt", text="unrelated"),
        ], worktree=self.wt)
        self.store.save_checkpoint(
            self.rid, "c" * 32, "d" * 40, branch="main",
            message="m", author="a", files=["f.py"], diff="patch",
            links=[(sid, "explicit")], worktree=self.wt)
        bundle = self.store.checkpoint_bundle("c" * 32)
        self.assertEqual(bundle["version"], 1)
        self.assertEqual(len(bundle["checkpoints"]), 1)
        got_sids = {s["id"] for s in bundle["sessions"]}
        self.assertEqual(got_sids, {sid, child})
        texts = [e["text"] for e in bundle["events"]]
        self.assertIn("child", texts)
        self.assertNotIn("unrelated", texts)
        self.assertEqual(
            bundle["links"],
            [{"checkpoint_id": "c" * 32, "session_id": sid,
              "method": "explicit"}])
        fresh = Store(self.tmp / "fb.db")
        res = fresh.import_bundle(bundle)
        self.assertGreaterEqual(res["events"], 3)
        cp = fresh.get_checkpoint("c" * 32)
        self.assertEqual(cp["links"], [{
            "session_id": sid, "method": "explicit"}])
        res2 = fresh.import_bundle(bundle)
        self.assertEqual(res2["events"], 0)


class StripEventPathsTests(RepoTestCase):
    """Recursive path stripping in event data on egress."""

    def setUp(self):
        super().setUp()
        self.store = Store(self.home / "partial.db")
        self.rid = self.store.register_repo(self.repo)["id"]
        self.wt = str(Path(self.repo).resolve())

    def _ingest(self):
        self.store.ingest(self.rid, [
            ev("e1", "s1", "session_start", agent="claude", data={
                "native_id": "n1", "cwd": self.wt, "source": "cli"}),
            ev("e2", "s1", "tool", agent="claude", tool_name="Edit",
               data={
                   "tool_input": {
                       "file_path": self.wt + "/src/a.py",
                       "path": "rel/b.py",
                   },
                   "hook": {
                       "transcript_path":
                           "/home/user/.claude/projects/x/t.jsonl",
                       "nested": {"worktree": self.wt + "/sub"},
                   },
                   "changes": [{"path": self.wt + "/c.py",
                                "kind": "modified"}],
                   "note": "see /etc/hostname for details",
               }),
        ], worktree=self.wt)

    def test_export_bundle_strips_absolute_paths(self):
        self._ingest()
        bundle = self.store.export_bundle(repo_id=self.rid)
        blob = json.dumps(bundle["events"])
        self.assertNotIn(self.wt, blob)
        self.assertNotIn("/home/user", blob)
        d1 = bundle["events"][0]["data"]
        self.assertEqual(d1["cwd"], ".")
        self.assertEqual(d1["native_id"], "n1")
        self.assertEqual(d1["source"], "cli")
        d2 = bundle["events"][1]["data"]
        self.assertEqual(d2["tool_input"]["file_path"], "src/a.py")
        self.assertEqual(d2["tool_input"]["path"], "rel/b.py")
        self.assertEqual(
            d2["hook"]["transcript_path"], "t.jsonl")
        self.assertEqual(d2["hook"]["nested"]["worktree"], "sub")
        self.assertEqual(
            d2["changes"], [{"path": "c.py", "kind": "modified"}])
        # Non-path text is preserved verbatim.
        self.assertEqual(
            d2["note"], "see /etc/hostname for details")

    def test_checkpoint_bundle_strips_absolute_paths(self):
        sid = scoped_session_id(self.rid, "claude", "s1")
        self._ingest()
        self.store.save_checkpoint(
            self.rid, "c" * 32, "d" * 40, branch="main",
            message="m", author="a", files=["c.py"], diff="patch",
            links=[(sid, "explicit")], worktree=self.wt)
        bundle = self.store.checkpoint_bundle("c" * 32)
        blob = json.dumps(bundle["events"])
        self.assertNotIn(self.wt, blob)
        self.assertNotIn("/home/user", blob)
        cwd = [e["data"]["cwd"] for e in bundle["events"]
               if e["id"] == "e1"]
        self.assertEqual(cwd, ["."])

    def test_strip_paths_reduces_foreign_absolute_paths(self):
        # Absolute paths outside every known root (provider state
        # dirs, other worktrees) are reduced to their basename rather
        # than passed through.
        out = self.store.strip_paths(
            {"hook": {"transcript_path": "/home/u/.claude/t.jsonl"},
             "cwd": "/elsewhere/repo",
             "tool_input": {"file_path": self.wt + "/a.py"}},
            repo_id=self.rid)
        self.assertEqual(
            out["hook"]["transcript_path"], "t.jsonl")
        self.assertEqual(out["cwd"], "repo")
        self.assertEqual(out["tool_input"]["file_path"], "a.py")

    def test_strip_paths_preserves_relative_and_text(self):
        out = self.store.strip_paths(
            {"tool_input": {"file_path": "src/a.py"},
             "hook": {"transcript_path": "transcripts/t.jsonl"},
             "note": "absolute-looking /etc/hostname in prose"},
            repo_id=self.rid)
        self.assertEqual(
            out["tool_input"]["file_path"], "src/a.py")
        self.assertEqual(
            out["hook"]["transcript_path"], "transcripts/t.jsonl")
        self.assertEqual(
            out["note"], "absolute-looking /etc/hostname in prose")


class SummaryFieldTests(RepoTestCase):
    """Store-level summary fields backing Entire-style
    repository/session/checkpoint browsing."""

    def setUp(self):
        super().setUp()
        self.store = Store(self.home / "partial.db")
        self.rid = self.store.register_repo(self.repo)["id"]
        self.wt = str(Path(self.repo).resolve())
        self.sid = scoped_session_id(self.rid, "devin", "s1")
        self.child = scoped_session_id(self.rid, "devin", "s2")
        self.store.ingest(self.rid, [
            ev("e1", "s1", "prompt", "2026-01-01T00:00:00Z", "parent"),
            ev("e2", "s1", "response", "2026-01-01T00:00:01Z", "done"),
            Event(id="c1", session_id="s2", agent="devin",
                  kind="prompt", timestamp="2026-01-01T00:00:02Z",
                  text="child", parent_session_id="s1"),
        ], worktree=self.wt, branch="main")
        self.cp_id = "a" * 32
        self.store.save_checkpoint(
            self.rid, self.cp_id, "b" * 40, branch="main",
            message="add a", author="t", files=["a.py"],
            diff=(
                "diff --git a/a.py b/a.py\n"
                "--- a/a.py\n"
                "+++ b/a.py\n"
                "@@ -1,2 +1,3 @@\n"
                " ctx\n"
                "-old\n"
                "+new\n"
                "+newer\n"),
            links=[(self.sid, "explicit"),
                   (self.child, "observed-worktree-overlap")],
            worktree=self.wt, created_at="2026-01-02T00:00:00Z")

    def test_session_summary_fields(self):
        rows = {s["id"]: s for s in
                self.store.list_sessions(repo_id=self.rid)}
        parent = rows[self.sid]
        self.assertEqual(parent["event_count"], 2)
        self.assertEqual(parent["checkpoint_count"], 1)
        self.assertEqual(parent["child_count"], 1)
        self.assertFalse(parent["is_subagent"])
        self.assertIsNone(parent["parent_session_id"])
        child = rows[self.child]
        self.assertEqual(child["event_count"], 1)
        self.assertEqual(child["checkpoint_count"], 1)
        self.assertEqual(child["child_count"], 0)
        self.assertTrue(child["is_subagent"])
        self.assertEqual(child["parent_session_id"], self.sid)

    def test_checkpoint_summary_fields(self):
        c = self.store.list_checkpoints(repo_id=self.rid)[0]
        self.assertEqual(c["file_count"], 1)
        self.assertEqual(c["session_count"], 2)
        self.assertEqual(c["agents"], ["devin"])
        self.assertEqual(c["additions"], 2)
        self.assertEqual(c["deletions"], 1)
        self.assertIsNone(c["ai_percentage"])
        self.assertIsNone(c["coverage_percentage"])
        full = self.store.get_checkpoint(self.cp_id)
        self.assertEqual(full["file_count"], 1)
        self.assertEqual(full["session_count"], 2)
        self.assertEqual(full["additions"], 2)
        self.assertEqual(full["deletions"], 1)

    def test_checkpoint_search(self):
        self.assertEqual(
            self.store.list_checkpoints(repo_id=self.rid, q="add a"),
            self.store.list_checkpoints(repo_id=self.rid))
        self.assertEqual(
            self.store.list_checkpoints(q="bbbb")[0]["id"],
            self.cp_id)
        self.assertEqual(
            self.store.list_checkpoints(repo_id=self.rid,
                                        q="no match"), [])
        with self.assertRaises(ValueError):
            self.store.list_checkpoints(q="x" * 501)

    def test_checkpoint_attribution_percentages(self):
        report = {
            "version": 1,
            "method": "position-aware-snapshot-diff-v1",
            "capture_source": "local-observation",
            "summary": {
                "agent_added": 2, "agent_removed": 0,
                "human_added": 0, "human_removed": 1,
                "unknown_added": 0, "unknown_removed": 0,
                "total_changed": 3,
                "agent_percentage": 66.67,
                "coverage_percentage": 100.0},
            "files": [], "excluded": [], "limitations": []}
        conn = self.store._connect()
        try:
            conn.execute(
                "INSERT INTO checkpoint_attribution(checkpoint_id,"
                "report) VALUES(?,?)",
                (self.cp_id, json.dumps(report)))
            conn.commit()
        finally:
            conn.close()
        c = self.store.list_checkpoints()[0]
        self.assertEqual(c["ai_percentage"], 66.67)
        self.assertEqual(c["coverage_percentage"], 100.0)
        self.assertEqual(
            self.store.get_checkpoint(self.cp_id)["ai_percentage"],
            66.67)

    def test_repo_summary(self):
        r = self.store.repo_summary(self.rid)
        self.assertNotIn("root", r)
        self.assertEqual(r["id"], self.rid)
        self.assertEqual(r["session_count"], 2)
        self.assertEqual(r["checkpoint_count"], 1)
        self.assertEqual(r["branch_count"], 1)
        self.assertEqual(r["agents"], ["devin"])
        self.assertEqual(r["last_activity"], "2026-01-02T00:00:00Z")
        self.assertEqual(r["latest_session"]["id"], self.child)
        self.assertEqual(r["latest_session"]["title"], "child")
        self.assertEqual(r["latest_checkpoint"]["id"], self.cp_id)
        self.assertEqual(
            r["latest_checkpoint"]["commit_sha"], "b" * 40)
        self.assertEqual(r["latest_checkpoint"]["message"], "add a")
        self.assertEqual(r["latest_checkpoint"]["author"], "t")
        self.assertIsNone(r["indexed"])
        self.assertIsNone(self.store.repo_summary("f" * 64))
        listed = self.store.list_repo_summaries()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["session_count"], 2)
        self.assertNotIn("root", listed[0])

    def test_repo_summary_indexed(self):
        conn = self.store._connect()
        try:
            conn.execute(
                "INSERT INTO repository_indexes(repo_id,commit_sha,"
                "indexed_at) VALUES(?,?,?)",
                (self.rid, "c" * 40, "2026-01-03T00:00:00Z"))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(
            self.store.repo_summary(self.rid)["indexed"],
            {"commit_sha": "c" * 40,
             "indexed_at": "2026-01-03T00:00:00Z"})

    def test_unified_diff_delta(self):
        self.assertEqual(
            unified_diff_delta(None),
            {"additions": 0, "deletions": 0})
        self.assertEqual(
            unified_diff_delta(""),
            {"additions": 0, "deletions": 0})
        self.assertEqual(
            unified_diff_delta(42),
            {"additions": 0, "deletions": 0})
        diff = (
            "diff --git a/f.py b/f.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -1,2 +1,2 @@\n"
            " context\n"
            "-old line\n"
            "+new line\n"
            "+another\n"
            "\\ No newline at end of file\n")
        self.assertEqual(
            unified_diff_delta(diff),
            {"additions": 2, "deletions": 1})

    def test_checkpoint_token_usage(self):
        self.store.ingest(self.rid, [
            Event(id="u1", session_id="s1", agent="devin",
                  kind="usage", timestamp="2026-01-01T00:00:03Z",
                  data={"usage_scope": "delta", "usage_id": "u1",
                        "usage": {"input_tokens": 10,
                                  "output_tokens": 5}}),
            Event(id="u2", session_id="s2", agent="devin",
                  kind="usage", timestamp="2026-01-01T00:00:04Z",
                  data={"usage_scope": "delta", "usage_id": "u2",
                        "usage": {"input_tokens": 4,
                                  "output_tokens": 2,
                                  "cached_input_tokens": 3}}),
        ])
        u = self.store.checkpoint_token_usage(self.cp_id)
        self.assertEqual(u["input_tokens"], 14)
        self.assertEqual(u["output_tokens"], 7)
        self.assertEqual(u["cached_input_tokens"], 3)
        self.assertIsNone(u["cache_creation_input_tokens"])
        self.assertEqual(u["sessions"], 2)
        self.assertEqual(u["events_used"], 2)
        self.assertTrue(u["complete"])
        self.assertIsNone(
            self.store.checkpoint_token_usage("f" * 32))

    def test_checkpoint_token_usage_tolerates_invalid(self):
        # Negative token counts are rejected by usage_totals; the
        # aggregate must skip the bad session rather than fail.
        self.store.ingest(self.rid, [
            Event(id="u9", session_id="s1", agent="devin",
                  kind="usage", timestamp="2026-01-01T00:00:03Z",
                  data={"usage_scope": "delta",
                        "usage": {"input_tokens": -5}}),
        ])
        u = self.store.checkpoint_token_usage(self.cp_id)
        self.assertIsNotNone(u)
        self.assertEqual(u["sessions"], 1)
        self.assertIsNone(u["input_tokens"])
        self.assertFalse(u["complete"])


class RemoteTests(unittest.TestCase):
    def test_normalize_remote(self):
        cases = {
            "git@github.com:Org/Repo.git": "github.com/Org/Repo",
            "https://user:pw@github.com/Org/Repo.git?x=1":
                "github.com/Org/Repo",
            "ssh://git@host.xz:22/path/repo.git": "host.xz/path/repo",
        }
        for raw, want in cases.items():
            self.assertEqual(normalize_remote(raw), want, raw)

    def test_scp_forms_share_identity(self):
        scp = "git@github.com:JBHoerter/Partial.git"
        san = sanitize_remote(scp)
        self.assertEqual(san, "ssh://github.com/JBHoerter/Partial.git")
        want = "github.com/JBHoerter/Partial"
        self.assertEqual(normalize_remote(scp), want)
        self.assertEqual(normalize_remote(san), want)
        self.assertEqual(
            normalize_remote("https://github.com/JBHoerter/Partial.git"),
            want)
        self.assertEqual(
            normalize_remote("ssh://git@github.com:22/JBHoerter/"
                             "Partial.git"), want)
        self.assertEqual(
            normalize_remote("github.com:JBHoerter/Partial.git"), want)

    def test_local_paths_not_mistaken_for_scp(self):
        self.assertEqual(
            normalize_remote("/home/x/repo"), "home/x/repo")
        self.assertEqual(
            normalize_remote("C:\\code\\repo"), "\\code\\repo")

    def test_sanitize_remote_strips_credentials(self):
        out = sanitize_remote("https://user:secret@example.com/r.git?tok=1")
        self.assertNotIn("secret", out)
        self.assertNotIn("tok", out)
        self.assertIn("example.com", out)


if __name__ == "__main__":
    unittest.main()
