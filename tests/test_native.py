from __future__ import annotations

import json
import unittest
from pathlib import Path

from helpers import RepoTestCase, commit, git

from partial import native
from partial.adapters import parse_import
from partial.git import create_checkpoint
from partial.models import Event, now_iso, scoped_session_id
from partial.provenance import Provenance
from partial.store import Store

UUID1 = "12345678-1234-1234-1234-1234567890ab"
UUID2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _rollout(uid=UUID1, cwd="/repo", extra_lines=None):
    lines = [
        {"type": "session_meta", "payload": {
            "id": uid, "cwd": cwd,
            "timestamp": "2026-09-16T09:00:00.000Z",
            "source": "cli"}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "hi"}]}},
    ]
    lines += extra_lines or []
    return "\n".join(json.dumps(x) for x in lines) + "\n"


def _claude_jsonl(sid=UUID2, cwd="/repo"):
    rows = [
        {"sessionId": sid, "uuid": "u1", "type": "user",
         "cwd": cwd,
         "message": {"role": "user", "content": "hi"}},
        {"sessionId": sid, "uuid": "u2", "parentUuid": "u1",
         "type": "assistant", "cwd": cwd,
         "message": {"role": "assistant", "id": "msg-1",
                     "content": [{"type": "text", "text": "ok"}],
                     "usage": {"input_tokens": 10,
                               "cache_read_input_tokens": 5,
                               "cache_creation_input_tokens": 2,
                               "output_tokens": 3}}},
    ]
    return "\n".join(json.dumps(x) for x in rows) + "\n"


class NativeCase(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.home / "partial.db")
        row = self.store.register_repo(str(self.repo))
        self.repo_id = row["id"]
        self.rd = {
            "id": self.repo_id, "root": str(self.repo),
            "common_dir": str(self.repo / ".git"), "remote": "",
        }

    def _mk_session(self, agent="codex", nid=UUID1):
        self.store.ingest(self.repo_id, [Event(
            id=f"ev-{agent}-{nid}", session_id=nid, agent=agent,
            kind="session_start", timestamp=now_iso(),
        )], worktree=str(self.repo))
        return scoped_session_id(self.repo_id, agent, nid)

    def test_codex_rollout_register(self):
        f = self.tmp / "rollout.jsonl"
        f.write_text(_rollout())
        row = native.register_native(
            self.store, self.rd, "codex", UUID1, path=f)
        self.assertEqual(row["format"], "codex-rollout")
        self.assertEqual(row["native_id"], UUID1)
        sid = scoped_session_id(self.repo_id, "codex", UUID1)
        self.assertEqual(row["session_id"], sid)
        self.assertEqual(row["local_path"], str(f.resolve()))

    def test_codex_exec_stream_rejected(self):
        f = self.tmp / "exec.jsonl"
        f.write_text(json.dumps(
            {"type": "thread.started", "thread_id": "t1"}) + "\n")
        with self.assertRaises(ValueError):
            native.register_native(
                self.store, self.rd, "codex", UUID1, path=f)

    def test_claude_identity_mismatch(self):
        f = self.tmp / "t.jsonl"
        f.write_text(_claude_jsonl(sid=UUID2))
        with self.assertRaises(ValueError):
            native.register_native(
                self.store, self.rd, "claude",
                "ffffffff-1111-1111-1111-111111111111", path=f)

    def test_claude_register_ok(self):
        f = self.tmp / "t.jsonl"
        f.write_text(_claude_jsonl())
        row = native.register_native(
            self.store, self.rd, "claude", UUID2, path=f)
        self.assertEqual(row["format"], "claude-jsonl")

    def test_malicious_native_id(self):
        for bad in ("../x", "a" * 200, "x;rm -rf", "", "x y",
                    "x/y", None, 5):
            with self.assertRaises(ValueError):
                native.register_native(
                    self.store, self.rd, "codex", bad)
        with self.assertRaises(ValueError):
            native.register_native(
                self.store, self.rd, "codex", UUID1,
                path=Path("/etc/passwd"))
        self.assertIsNone(
            self.store.get_native(
                scoped_session_id(self.repo_id, "codex", "../x")))

    def test_fake_agent_rejected(self):
        with self.assertRaises(ValueError):
            native.register_native(
                self.store, self.rd, "chatgpt", UUID1)
        with self.assertRaises(ValueError):
            native.agent_argv("evil", "id")
        self.assertEqual(
            native.agent_argv("codex", UUID1),
            ["codex", "resume", UUID1])
        self.assertEqual(
            native.agent_argv("claude", UUID2), ["claude", "-r", UUID2])
        self.assertEqual(
            native.agent_argv("devin", "dev-s"),
            ["devin", "--resume", "dev-s"])

    def test_archive_redacts_secrets(self):
        text = _rollout(extra_lines=[{
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant",
                        "content": [{"type": "output_text",
                                     "text": "sk-abc"}]},
            }, {"type": "response_item", "payload": {
                "type": "reasoning",
                "encrypted_content": "blob"}},
            {"type": "response_item", "payload": {
                "type": "message", "role": "assistant",
                "authorization": "Bearer sekret",
                "content": [{"type": "output_text",
                             "text": "done"}]}},
        ])
        f = self.tmp / "rollout.jsonl"
        f.write_text(text)
        row = native.register_native(
            self.store, self.rd, "codex", UUID1, path=f, archive=True)
        arch = row["archive"]
        self.assertIsNotNone(arch)
        self.assertNotIn("encrypted_content", arch)
        self.assertNotIn("reasoning", arch)
        self.assertNotIn("Bearer sekret", arch)
        for line in arch.splitlines():
            json.loads(line)

    def test_resume_plan(self):
        self._mk_session()
        f = self.tmp / "rollout.jsonl"
        f.write_text(_rollout())
        native.register_native(
            self.store, self.rd, "codex", UUID1, path=f)
        sid = scoped_session_id(self.repo_id, "codex", UUID1)
        plan = native.resume_plan(self.store, sid)
        self.assertEqual(plan["argv"], ["codex", "resume", UUID1])
        self.assertIsNone(plan["native_available"])
        self.assertTrue(plan["file_present"])
        self.assertEqual(plan["mode"], "plan")

    def test_resume_plan_missing_native(self):
        self._mk_session()
        sid = scoped_session_id(self.repo_id, "codex", UUID1)
        with self.assertRaises(ValueError):
            native.resume_plan(self.store, sid)

    def test_restore_requires_archive_and_no_overwrite(self):
        self._mk_session()
        f = self.tmp / "rollout.jsonl"
        f.write_text(_rollout())
        native.register_native(
            self.store, self.rd, "codex", UUID1, path=f, archive=True)
        sid = scoped_session_id(self.repo_id, "codex", UUID1)
        target = self.tmp / "target"
        target.mkdir()
        out = native.restore_native(
            self.store, sid, target_root=target)
        dest = Path(out["path"])
        self.assertTrue(dest.is_file())
        self.assertIn("rollout-", dest.name)
        self.assertIn(UUID1, dest.name)
        first = json.loads(dest.read_text().splitlines()[0])
        self.assertEqual(first["payload"]["cwd"], str(self.repo))
        with self.assertRaises(ValueError):
            native.restore_native(self.store, sid, target_root=target)
        sid2 = self._mk_session(agent="codex", nid="nodashid")
        native.register_native(
            self.store, self.rd, "codex", "nodashid")
        with self.assertRaises(ValueError):
            native.restore_native(self.store, sid2, target_root=target)

    def test_restore_strips_codex_policy(self):
        text = _rollout(extra_lines=[
            {"type": "turn_context", "payload": {
                "cwd": "/repo", "sandbox_policy": {"mode": "danger"},
                "approval_policy": "never"}},
            {"type": "event_msg", "payload": {
                "type": "agent_message", "message": "hi"}},
        ])
        f = self.tmp / "rollout.jsonl"
        f.write_text(text)
        native.register_native(
            self.store, self.rd, "codex", UUID1, path=f, archive=True)
        sid = scoped_session_id(self.repo_id, "codex", UUID1)
        (self.tmp / "t2").mkdir()
        out = native.restore_native(
            self.store, sid, target_root=self.tmp / "t2")
        objs = [json.loads(l) for l in
                Path(out["path"]).read_text().splitlines()]
        tc = [o for o in objs if o["type"] == "turn_context"][0]
        self.assertNotIn("sandbox_policy", tc["payload"])
        self.assertNotIn("approval_policy", tc["payload"])

    def test_register_rejects_unknown_record(self):
        text = _rollout(extra_lines=[
            {"type": "mystery", "payload": {}}])
        f = self.tmp / "rollout.jsonl"
        f.write_text(text)
        with self.assertRaises(ValueError):
            native.register_native(
                self.store, self.rd, "codex", UUID1, path=f,
                archive=True)

    def test_restore_rejects_tampered_archive(self):
        self._mk_session()
        f = self.tmp / "rollout.jsonl"
        f.write_text(_rollout())
        native.register_native(
            self.store, self.rd, "codex", UUID1, path=f, archive=True)
        sid = scoped_session_id(self.repo_id, "codex", UUID1)
        conn = self.store._connect()
        conn.execute(
            "UPDATE native_sessions SET archive=? WHERE session_id=?",
            (_rollout(uid=UUID2), sid))
        conn.commit()
        conn.close()
        target = self.tmp / "t3"
        target.mkdir()
        with self.assertRaises(ValueError):
            native.restore_native(
                self.store, sid, target_root=target)
        self.assertEqual(list(target.iterdir()), [])

    def test_devin_restore_unsupported(self):
        self._mk_session(agent="devin", nid="dev-1")
        native.register_native(self.store, self.rd, "devin", "dev-1")
        sid = scoped_session_id(self.repo_id, "devin", "dev-1")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            native.restore_native(
                self.store, sid, target_root=self.tmp / "t4")

    def test_resume_worktree_and_head_unchanged(self):
        self._mk_session()
        f = self.tmp / "rollout.jsonl"
        f.write_text(_rollout())
        native.register_native(
            self.store, self.rd, "codex", UUID1, path=f)
        sid = scoped_session_id(self.repo_id, "codex", UUID1)
        self.write_file("app.py", "x = 1\n")
        git(self.repo, "add", "--", "app.py")
        commit(self.repo, "base")
        head_before = git(
            self.repo, "rev-parse", "HEAD").stdout.strip()
        status_before = git(
            self.repo, "status", "--porcelain").stdout
        cp = create_checkpoint(
            self.store, self.repo_id, session_ids=[sid],
            worktree=str(self.repo))
        wt = self.tmp / "newwt"
        plan = native.resume_session(
            self.store, sid, worktree=wt)
        self.assertTrue((wt / "app.py").is_file())
        self.assertEqual(plan["worktree"], str(wt))
        self.assertEqual(
            git(self.repo, "rev-parse", "HEAD").stdout.strip(),
            head_before)
        self.assertEqual(
            git(self.repo, "status", "--porcelain").stdout,
            status_before)
        with self.assertRaises(ValueError):
            native.resume_session(self.store, sid, worktree=wt)

    def test_resume_requires_trust_for_restore(self):
        self._mk_session()
        native.register_native(
            self.store, self.rd, "codex", UUID1)
        sid = scoped_session_id(self.repo_id, "codex", UUID1)
        with self.assertRaises(ValueError):
            native.resume_session(
                self.store, sid, restore=True,
                target_root=self.tmp / "t5")

    def test_stop_and_attach(self):
        self._mk_session()
        sid = scoped_session_id(self.repo_id, "codex", UUID1)
        self.store.end_session(sid)
        self.assertEqual(
            self.store.get_session_meta(sid)["status"], "ended")
        self.write_file("app.py", "x = 1\n")
        git(self.repo, "add", "--", "app.py")
        commit(self.repo, "attach me")
        cp = create_checkpoint(
            self.store, self.repo_id, session_ids=[sid],
            worktree=str(self.repo))
        self.assertIn(sid, cp["session_ids"])

    def test_native_id_only_registration(self):
        row = native.register_native(
            self.store, self.rd, "devin", "dev-42")
        self.assertEqual(row["format"], "native-id")
        self.assertIsNone(row["archive"])
        sid = scoped_session_id(self.repo_id, "devin", "dev-42")
        self.assertIsNotNone(self.store.get_session_meta(sid))

    def test_hook_source_requires_session(self):
        row = native.register_native(
            self.store, self.rd, "devin", "ghost-1", source="hook")
        self.assertIsNone(row)

    def test_sensitive_file_rejected(self):
        f = self.tmp / "auth.json"
        f.write_text("{}")
        with self.assertRaises(ValueError):
            native.register_native(
                self.store, self.rd, "codex", UUID1, path=f)
        with self.assertRaises(ValueError):
            native.register_native(
                self.store, self.rd, "codex", UUID1,
                path=self.repo / ".env")

    def test_codex_malformed_later_line(self):
        f = self.tmp / "rollout.jsonl"
        f.write_text(_rollout() + "not json at all\n")
        for archive in (False, True):
            with self.assertRaises(ValueError):
                native.register_native(
                    self.store, self.rd, "codex", UUID1, path=f,
                    archive=archive)

    def test_codex_second_session_meta_mismatch(self):
        f = self.tmp / "rollout.jsonl"
        f.write_text(_rollout(extra_lines=[{
            "type": "session_meta", "payload": {
                "id": UUID2, "cwd": "/repo",
                "timestamp": "2026-09-16T09:00:01.000Z"}}]))
        with self.assertRaises(ValueError):
            native.register_native(
                self.store, self.rd, "codex", UUID1, path=f)

    def test_claude_requires_message(self):
        f = self.tmp / "t.jsonl"
        f.write_text(json.dumps(
            {"sessionId": UUID2, "type": "summary"}) + "\n")
        with self.assertRaises(ValueError):
            native.register_native(
                self.store, self.rd, "claude", UUID2, path=f)
        f.write_text(_claude_jsonl() + "12345\n")
        with self.assertRaises(ValueError):
            native.register_native(
                self.store, self.rd, "claude", UUID2, path=f)

    def test_upsert_upgrades_and_conflicts(self):
        self._mk_session()
        sid = scoped_session_id(self.repo_id, "codex", UUID1)
        row = native.register_native(
            self.store, self.rd, "codex", UUID1)
        self.assertEqual(row["format"], "native-id")
        self.assertIsNone(row["archive"])
        f = self.tmp / "rollout.jsonl"
        f.write_text(_rollout())
        row = native.register_native(
            self.store, self.rd, "codex", UUID1, path=f, archive=True,
            source="explicit")
        self.assertEqual(row["format"], "codex-rollout")
        self.assertEqual(row["source"], "explicit")
        self.assertIsNotNone(row["archive"])
        self.assertEqual(row["local_path"], str(f.resolve()))
        with self.assertRaises(ValueError):
            self.store.upsert_native(
                sid, "codex", UUID2, "native-id", local_path=None,
                archive=None, source="explicit")
        with self.assertRaises(ValueError):
            self.store.upsert_native(
                sid, "codex", UUID1, "claude-jsonl", local_path=None,
                archive=None, source="explicit")
        with self.assertRaises(ValueError):
            self.store.upsert_native(
                sid, "chatgpt", UUID1, "native-id", local_path=None,
                archive=None, source="explicit")

    def test_bundle_import_native_roundtrip_codex(self):
        self._mk_session()
        f = self.tmp / "rollout.jsonl"
        f.write_text(_rollout())
        native.register_native(
            self.store, self.rd, "codex", UUID1, path=f, archive=True)
        sid = scoped_session_id(self.repo_id, "codex", UUID1)
        bundle = self.store.export_bundle()
        store2 = Store(self.tmp / "other" / "partial.db")
        store2.import_bundle(bundle)
        n2 = store2.get_native(sid)
        self.assertIsNotNone(n2)
        self.assertEqual(n2["format"], "codex-rollout")
        self.assertEqual(n2["source"], "imported-claim")
        self.assertIsNone(n2["local_path"])
        self.assertIsNotNone(n2["archive"])
        target = self.tmp / "imp-target"
        target.mkdir()
        out = native.restore_native(store2, sid, target_root=target)
        dest = Path(out["path"])
        self.assertTrue(dest.is_file())
        first = json.loads(dest.read_text().splitlines()[0])
        self.assertEqual(first["payload"]["id"], UUID1)

    def test_bundle_import_native_roundtrip_claude(self):
        self._mk_session(agent="claude", nid=UUID2)
        f = self.tmp / "claude.jsonl"
        f.write_text(_claude_jsonl())
        native.register_native(
            self.store, self.rd, "claude", UUID2, path=f, archive=True)
        sid = scoped_session_id(self.repo_id, "claude", UUID2)
        bundle = self.store.export_bundle()
        store2 = Store(self.tmp / "other" / "partial.db")
        store2.import_bundle(bundle)
        n2 = store2.get_native(sid)
        self.assertIsNotNone(n2)
        self.assertEqual(n2["format"], "claude-jsonl")
        self.assertIsNone(n2["local_path"])
        target = self.tmp / "imp-claude"
        target.mkdir()
        out = native.restore_native(store2, sid, target_root=target)
        dest = Path(out["path"])
        self.assertTrue(dest.is_file())
        self.assertIn(".claude", str(dest))
        self.assertEqual(dest.name, f"{UUID2}.jsonl")
        rows = [json.loads(l) for l in dest.read_text().splitlines()]
        self.assertTrue(
            all(r["sessionId"] == UUID2 for r in rows))
        self.assertTrue(all(r.get("cwd") == str(target.resolve())
                            for r in rows if "cwd" in r))

    def test_import_native_wrong_agent_rejected(self):
        self._mk_session()
        native.register_native(
            self.store, self.rd, "codex", UUID1)
        sid = scoped_session_id(self.repo_id, "codex", UUID1)
        bundle = self.store.export_bundle()
        bundle["native_sessions"][0]["agent"] = "claude"
        store2 = Store(self.tmp / "other" / "partial.db")
        with self.assertRaises(ValueError):
            store2.import_bundle(bundle)
        self.assertIsNone(store2.get_native(sid))
        self.assertEqual(store2.stats()["sessions"], 0)

    def test_import_native_bad_archive_rejected(self):
        self._mk_session()
        f = self.tmp / "rollout.jsonl"
        f.write_text(_rollout())
        native.register_native(
            self.store, self.rd, "codex", UUID1, path=f, archive=True)
        bundle = self.store.export_bundle()
        bundle["native_sessions"][0]["archive"] = _rollout(uid=UUID2)
        store2 = Store(self.tmp / "other" / "partial.db")
        with self.assertRaises(ValueError):
            store2.import_bundle(bundle)
        sid = scoped_session_id(self.repo_id, "codex", UUID1)
        self.assertIsNone(store2.get_native(sid))

    def test_restore_symlink_component_refused(self):
        self._mk_session(agent="claude", nid=UUID2)
        f = self.tmp / "claude.jsonl"
        f.write_text(_claude_jsonl())
        native.register_native(
            self.store, self.rd, "claude", UUID2, path=f, archive=True)
        sid = scoped_session_id(self.repo_id, "claude", UUID2)
        target = self.tmp / "tsym"
        target.mkdir()
        evil = self.tmp / "evil"
        evil.mkdir()
        (target / ".claude").symlink_to(evil)
        with self.assertRaises((ValueError, OSError)):
            native.restore_native(
                self.store, sid, target_root=target)
        self.assertEqual(list(evil.iterdir()), [])

    def test_resume_plan_devin_unknown_available(self):
        self._mk_session(agent="devin", nid="dev-9")
        native.register_native(
            self.store, self.rd, "devin", "dev-9")
        sid = scoped_session_id(self.repo_id, "devin", "dev-9")
        plan = native.resume_plan(self.store, sid)
        self.assertIsNone(plan["native_available"])
        self.assertFalse(plan["file_present"])
        self.assertEqual(plan["argv"], ["devin", "--resume", "dev-9"])

    def test_resume_restores_before_plan_recheck(self):
        self._mk_session()
        f = self.tmp / "rollout.jsonl"
        f.write_text(_rollout())
        native.register_native(
            self.store, self.rd, "codex", UUID1, path=f, archive=True)
        sid = scoped_session_id(self.repo_id, "codex", UUID1)
        conn = self.store._connect()
        conn.execute(
            "UPDATE native_sessions SET local_path=? WHERE"
            " session_id=?", ("/nonexistent/path.jsonl", sid))
        conn.commit()
        conn.close()
        plan = native.resume_plan(self.store, sid)
        self.assertFalse(plan["native_available"])
        target = self.tmp / "rt"
        target.mkdir()
        out = native.resume_session(
            self.store, sid, restore=True, trust_native_state=True,
            target_root=target)
        self.assertIsNone(out["native_available"])
        self.assertTrue(out["file_present"])
        self.assertTrue(Path(out["restored"]["path"]).is_file())
        got = self.store.get_native(sid)
        self.assertTrue(Path(got["local_path"]).is_file())


class AtifImportTest(unittest.TestCase):
    def _atif(self):
        return {
            "session_id": "dev-sess-1",
            "agent": {"name": "devin", "version": "1",
                      "model_name": "base-model"},
            "steps": [
                {"step_id": 1, "source": "user",
                 "message": "do the thing"},
                {"step_id": 2, "source": "agent",
                 "model_name": "m-x",
                 "message": [
                     {"type": "text", "text": "working"},
                     {"type": "image",
                      "url": "https://evil.example/x.png"}],
                 "tool_calls": [{
                     "tool_call_id": "call-1",
                     "function_name": "edit",
                     "arguments": {"file_path": "a.py"}}],
                 "observation": {"results": [{
                     "source_call_id": "call-1",
                     "content": "edited"}]},
                 "metrics": {"prompt_tokens": 10,
                             "completion_tokens": 5,
                             "cached_tokens": 2}},
                {"step_id": 3, "source": "system",
                 "message": "context window"},
                {"step_id": 4, "source": "agent",
                 "message": "copied",
                 "is_copied_context": True,
                 "metrics": {"prompt_tokens": 999,
                             "completion_tokens": 1}},
            ],
            "subagent_trajectories": [{
                "trajectory_id": "sub-9",
                "agent": {"name": "devin", "version": "1",
                          "model_name": "child-model"},
                "steps": [{
                    "step_id": 1, "source": "agent",
                    "message": "child step"}]}],
        }

    def test_atif_full(self):
        evs = parse_import("devin", json.dumps(self._atif()))
        kinds = [e.kind for e in evs]
        self.assertIn("system", kinds)
        self.assertIn("usage", kinds)
        prompt = [e for e in evs if e.kind == "prompt"][0]
        self.assertEqual(prompt.text, "do the thing")
        resp = [e for e in evs
                if e.kind == "response" and e.text == "working"][0]
        self.assertEqual(resp.model, "m-x")
        self.assertNotIn("evil.example", resp.text)
        tool = [e for e in evs if e.tool_name == "edit"][0]
        self.assertEqual(tool.data["tool_call_id"], "call-1")
        res = [e for e in evs if e.tool_name == "tool_result"][0]
        self.assertEqual(res.data["source_call_id"], "call-1")
        usages = [e for e in evs if e.kind == "usage"]
        self.assertEqual(len(usages), 1)
        self.assertEqual(usages[0].data["usage"]["input_tokens"], 10)
        self.assertEqual(usages[0].data["usage"]["output_tokens"], 5)
        self.assertEqual(
            usages[0].data["usage"]["cached_input_tokens"], 2)
        self.assertEqual(usages[0].data["usage_scope"], "delta")
        child = [e for e in evs if ":trajectory:" in e.session_id]
        self.assertTrue(child)
        self.assertEqual(
            child[0].session_id, "dev-sess-1:trajectory:sub-9")
        self.assertEqual(child[0].parent_session_id, "dev-sess-1")
        self.assertEqual(child[0].model, "child-model")

    def test_atif_model_resets_per_step(self):
        evs = parse_import("devin", json.dumps(self._atif()))
        copied = [e for e in evs
                  if e.kind == "response" and e.text == "copied"][0]
        self.assertEqual(copied.model, "base-model")

    def test_atif_duplicate_trajectory_rejected(self):
        obj = self._atif()
        obj["subagent_trajectories"].append(
            dict(obj["subagent_trajectories"][0]))
        with self.assertRaises(ValueError):
            parse_import("devin", json.dumps(obj))

    def test_atif_nested_depth_limit(self):
        leaf = {"trajectory_id": "t16",
                "steps": [{"step_id": 1, "source": "agent",
                           "message": "deep"}]}
        node = leaf
        for i in range(16, 0, -1):
            node = {"trajectory_id": f"t{i - 1}",
                    "agent": {"model_name": "m"},
                    "steps": [{"step_id": 1, "source": "agent",
                               "message": f"lvl {i}"}],
                    "subagent_trajectories": [node]}
        obj = self._atif()
        obj["subagent_trajectories"] = [node]
        with self.assertRaises(ValueError):
            parse_import("devin", json.dumps(obj))


class ClaudeUsageTest(unittest.TestCase):
    def test_delta_usage(self):
        evs = parse_import("claude", _claude_jsonl())
        usages = [e for e in evs if e.kind == "usage"]
        self.assertEqual(len(usages), 1)
        u = usages[0].data
        self.assertEqual(u["usage_scope"], "delta")
        self.assertEqual(u["usage_id"], "msg-1")
        self.assertEqual(u["usage"]["input_tokens"], 17)
        self.assertEqual(u["usage"]["output_tokens"], 3)
        self.assertEqual(u["usage"]["cached_input_tokens"], 5)
        self.assertEqual(u["usage"]["cache_creation_input_tokens"], 2)
        self.assertNotIn("cache_read_input_tokens", u["usage"])

    def test_usage_totals_integration(self):
        from partial.brain_contract import usage_totals
        evs = parse_import("claude", _claude_jsonl())
        totals = usage_totals([
            {"id": e.id, "kind": e.kind, "data": e.data}
            for e in evs])
        self.assertEqual(totals["input_tokens"], 17)
        self.assertEqual(totals["output_tokens"], 3)
        self.assertEqual(totals["cached_input_tokens"], 5)
        self.assertEqual(totals["cache_creation_input_tokens"], 2)
        self.assertEqual(totals["basis"], "reported-deltas")
        self.assertEqual(totals["unclassified_events"], 0)


class CodexImportTest(unittest.TestCase):
    def test_rollout_session_meta_identity(self):
        evs = parse_import("codex", _rollout())
        starts = [e for e in evs if e.kind == "session_start"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].session_id, UUID1)
        self.assertEqual(starts[0].data["native_id"], UUID1)
        self.assertEqual(starts[0].data["cwd"], "/repo")
        self.assertTrue(all(e.session_id == UUID1 for e in evs))

    def test_rollout_cumulative_usage(self):
        text = _rollout(extra_lines=[{
            "type": "event_msg", "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {
                    "input_tokens": 100, "output_tokens": 20},
                    "last_token_usage": {
                        "input_tokens": 10, "output_tokens": 2}}}}])
        evs = parse_import("codex", text)
        usages = [e for e in evs if e.kind == "usage"]
        self.assertEqual(len(usages), 1)
        self.assertEqual(usages[0].data["usage_scope"], "cumulative")
        self.assertEqual(usages[0].data["usage"]["input_tokens"], 100)

    def test_rollout_unclassified_usage_no_total(self):
        text = _rollout(extra_lines=[{
            "type": "event_msg", "payload": {
                "type": "token_count",
                "info": {"rate_limits": {"x": 1}}}}])
        evs = parse_import("codex", text)
        usages = [e for e in evs if e.kind == "usage"]
        self.assertEqual(len(usages), 1)
        self.assertEqual(
            usages[0].data["usage_scope"], "unclassified")
        self.assertNotIn("input_tokens",
                         usages[0].data["usage"].get(
                             "total_token_usage") or {})


class TenantIsolationTest(RepoTestCase):
    def test_workspace_isolation_same_ids(self):
        from partial.accounts import Accounts
        acc = Accounts(self.home, self.home / "partial.db")
        user = acc.setup(
            email="o@x.test", name="O",
            password="correct horse battery")
        principal = acc.check_password("o@x.test", "correct horse battery")
        ws2 = acc.create_workspace(principal, "second")
        wss = acc.workspaces(principal)
        s1, _ = acc.workspace_store(principal, wss[0]["id"])
        s2, _ = acc.workspace_store(principal, ws2["id"])
        self.assertNotEqual(s1.path, s2.path)
        for store in (s1, s2):
            row = store.register_repo(str(self.repo))
            store.ingest(row["id"], [Event(
                id="ev1", session_id="n1", agent="devin",
                kind="session_start", timestamp=now_iso(),
            )], worktree=str(self.repo))
            native.register_native(
                store, {"id": row["id"], "root": str(self.repo)},
                "devin", "n1")
        sid = scoped_session_id(row["id"], "devin", "n1")
        self.assertIsNotNone(s1.get_native(sid))
        self.assertIsNotNone(s2.get_native(sid))
        conn = s1._connect()
        conn.execute(
            "INSERT INTO checkpoints(id,repo_id,commit_sha,files,"
            "session_ids,created_at) VALUES(?,?,?,?,?,?)",
            ("a" * 32, row["id"], "b" * 40, "[]", "[]", now_iso()))
        conn.execute(
            "INSERT INTO checkpoint_attribution(checkpoint_id,report)"
            " VALUES(?,?)",
            ("a" * 32, json.dumps({"summary": {}})))
        conn.commit()
        conn.close()
        self.assertIsNotNone(s1.get_attribution("a" * 32))
        self.assertIsNone(s2.get_attribution("a" * 32))


if __name__ == "__main__":
    unittest.main()
