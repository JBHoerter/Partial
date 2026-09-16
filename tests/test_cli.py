import io
import json
import os
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from partial.cli import main
from partial.models import scoped_session_id
from partial.store import Store

from helpers import RepoTestCase, commit, git, init_repo

ROOT = Path(__file__).resolve().parent.parent


class ArgparseTests(unittest.TestCase):
    def test_help_and_no_command(self):
        with self.assertRaises(SystemExit) as ctx:
            with patch("sys.stdout", new=io.StringIO()):
                main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        with patch("sys.stdout", new=io.StringIO()) as out:
            self.assertEqual(main([]), 0)
        self.assertIn("enable", out.getvalue())
        with self.assertRaises(SystemExit) as ctx:
            with patch("sys.stdout", new=io.StringIO()):
                main(["--version"])
        self.assertEqual(ctx.exception.code, 0)


class CliRepoTests(RepoTestCase):
    def _run(self, argv, stdin="", cwd=None):
        argv = ["--repo", str(self.repo), *argv]
        prev = os.getcwd()
        if cwd is not None:
            os.chdir(cwd)
        try:
            with patch("sys.stdout", new=io.StringIO()) as out, \
                    patch("sys.stdin", io.StringIO(stdin)), \
                    patch("sys.stderr", new=io.StringIO()):
                code = main(argv)
        finally:
            if cwd is not None:
                os.chdir(prev)
        return code, out.getvalue()

    def test_status_doctor_no_repo(self):
        with patch("sys.stdout", new=io.StringIO()) as out:
            code = main(["status", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(
            {k: data[k] for k in ("sessions", "checkpoints",
                                  "repositories")},
            {"sessions": 0, "checkpoints": 0, "repositories": 0})
        with patch("sys.stdout", new=io.StringIO()):
            self.assertIn(main(["doctor"]), (0, 1))

    def test_home_is_directory(self):
        home = self.tmp / "hdir"
        with patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(main(["--home", str(home), "status"]), 0)
        self.assertTrue((home / "partial.db").exists())
        self.assertEqual(
            oct((home).stat().st_mode & 0o777), "0o700")

    def test_enable_hook_ingest_and_session_views(self):
        code, out = self._run(["enable"])
        self.assertEqual(code, 0, out)
        payload = json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "session_id": "dsess",
            "prompt_id": "pp1",
            "prompt": "write a parser",
        })
        code, _ = self._run(["hook", "devin"], stdin=payload)
        self.assertEqual(code, 0)
        code, out = self._run(["sessions"])
        lines = [l for l in out.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(lines[0].split()[0]), 64)
        store = Store(self.home / "partial.db")
        sid = store.list_sessions()[0]["id"]
        code, out = self._run(["session", sid, "--json"])
        detail = json.loads(out)
        self.assertEqual(detail["events"][0]["text"], "write a parser")
        code, out = self._run(["handoff", sid])
        self.assertIn("write a parser", out)
        self.assertIn("# Handoff", out)

    def test_enable_all_agents_and_preflight(self):
        code, out = self._run(["enable", "--agent", "all"])
        self.assertEqual(code, 0)
        self.assertIn("chatgpt", out)
        self.assertNotIn("run chatgpt", out)
        bad = self.tmp / "badrepo"
        git(self.tmp, "init", "-b", "main", str(bad))
        (bad / ".devin").mkdir()
        (bad / ".devin" / "hooks.v1.json").write_text("{broken")
        argv = ["--repo", str(bad), "enable", "--agent", "devin"]
        with patch("sys.stdout", new=io.StringIO()), \
                patch("sys.stderr", new=io.StringIO()) as err:
            code = main(argv)
        self.assertEqual(code, 2)
        self.assertIn("invalid JSON", err.getvalue())
        hook = bad / ".git" / "hooks" / "post-commit"
        self.assertFalse(hook.exists())

    def test_hook_git_requires_links(self):
        self._run(["enable"])
        self.add_commit("solo.py", "s = 1\n")
        code, _ = self._run(["hook", "git", "post-commit"])
        self.assertEqual(code, 0)
        store = Store(self.home / "partial.db")
        self.assertEqual(store.stats()["checkpoints"], 0)

    def test_hook_git_post_commit_links_and_refreshes_on_stop(self):
        self._run(["enable"])
        tool_payload = json.dumps({
            "hook_event_name": "PostToolUse",
            "session_id": "s1",
            "tool_name": "edit_file",
            "tool_input": {"file_path": str(self.repo / "x.py")},
            "tool_response": {"success": True, "output": "ok"},
        })
        self._run(["hook", "devin"], stdin=tool_payload)
        self.add_commit("x.py", "x = 1\n")
        code, _ = self._run(["hook", "git", "post-commit"])
        self.assertEqual(code, 0)
        store = Store(self.home / "partial.db")
        cps = store.list_checkpoints()
        self.assertEqual(len(cps), 1)
        self.assertEqual(len(cps[0]["session_ids"]), 1)
        stop = json.dumps({
            "hook_event_name": "Stop", "session_id": "s1",
            "last_assistant_message": "final answer",
        })
        self._run(["hook", "devin"], stdin=stop)
        blob = git(
            self.repo, "show",
            "refs/heads/partial/checkpoints/v1:"
            f"checkpoints/{cps[0]['id']}.json").stdout
        data = json.loads(blob)
        texts = [e.get("text", "") for e in data["events"]]
        self.assertIn("final answer", texts)

    def test_capture_uses_discovered_worktree(self):
        code, _ = self._run(["enable"])
        self.assertEqual(code, 0)
        code, _ = self._run(["hook", "devin"], stdin=json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "session_id": "cs1", "prompt": "x"}))
        self.assertEqual(code, 0)
        store = Store(self.home / "partial.db")
        sid = store.list_sessions()[0]["id"]
        self.add_commit("f.py", "a\n")
        wt2 = self.tmp / "wt2"
        git(self.repo, "worktree", "add", str(wt2), "HEAD")
        def run2(argv, stdin=""):
            argv = ["--repo", str(wt2), *argv]
            with patch("sys.stdout", new=io.StringIO()) as out, \
                    patch("sys.stdin", io.StringIO(stdin)), \
                    patch("sys.stderr", new=io.StringIO()):
                return main(argv), out.getvalue()
        code, _ = run2([
            "capture", "before", "--session", sid,
            "--file", "f.py", "--key", "k1"])
        self.assertEqual(code, 0)
        (wt2 / "f.py").write_text("a\nx\n")
        code, _ = run2([
            "capture", "after", "--session", sid,
            "--file", "f.py", "--key", "k1"])
        self.assertEqual(code, 0)
        conn = store._connect()
        try:
            row = conn.execute(
                "SELECT worktree FROM attribution_files"
                " WHERE path='f.py'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["worktree"], str(Path(wt2).resolve()))
        other = self.tmp / "unrelated"
        git(self.tmp, "init", "-b", "main", str(other))
        argv = ["--repo", str(other), "capture", "before",
                "--session", sid, "--file", "f.py", "--key", "k9"]
        with patch("sys.stdout", new=io.StringIO()), \
                patch("sys.stdin", io.StringIO("")), \
                patch("sys.stderr", new=io.StringIO()):
            code = main(argv)
        self.assertEqual(code, 2)

    def test_hook_event_from_payload(self):
        code, _ = self._run(["enable"])
        self.assertEqual(code, 0)
        self._run(["hook", "devin"], stdin=json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "session_id": "ps1", "prompt": "hi"}))
        self.add_commit("f.py", "a\n")
        code, _ = self._run(["hook", "devin"], stdin=json.dumps({
            "hook_event_name": "PreToolUse", "session_id": "ps1",
            "tool_name": "edit",
            "tool_input": {"file_path": "f.py"},
            "tool_use_id": "k1"}))
        self.assertEqual(code, 0)
        self.write_file("f.py", "a\nx\n")
        code, _ = self._run(["hook", "devin"], stdin=json.dumps({
            "hook_event_name": "PostToolUse", "session_id": "ps1",
            "tool_name": "edit",
            "tool_input": {"file_path": "f.py"},
            "tool_use_id": "k1",
            "tool_response": {"success": True}}))
        self.assertEqual(code, 0)
        store = Store(self.home / "partial.db")
        sid = store.list_sessions()[0]["id"]
        git(self.repo, "add", "--", "f.py")
        commit(self.repo, "x")
        code, out = self._run(["checkpoint", "--session", sid])
        self.assertEqual(code, 0, out)
        code, out = self._run(["blame", "f.py", "--json"],
                              cwd=self.repo)
        rows = json.loads(out)
        self.assertEqual(rows[1]["kind"], "agent")
        self.assertEqual(rows[1]["evidence"], "tool-pair")
        self.assertEqual(rows[0]["kind"], "unknown")

    def test_blame_dirty_line_and_why_all_lines(self):
        self._run(["enable"])
        self.add_commit("f.py", "a\nb\n")
        self.write_file("f.py", "a\nb\ndirty\n")
        code, out = self._run(["blame", "f.py", "--json"],
                              cwd=self.repo)
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2]["kind"], "unknown")
        self.assertEqual(rows[2]["status"], "uncommitted")
        self.assertEqual(rows[0]["kind"], "unknown")
        self.assertNotEqual(rows[0]["kind"], "human")
        code, out = self._run(["why", "f.py", "--json"], cwd=self.repo)
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertIsInstance(rows, list)
        self.assertEqual(len(rows), 3)
        code, out = self._run(
            ["why", "f.py", "--line", "2", "--json"], cwd=self.repo)
        row = json.loads(out)
        self.assertIsInstance(row, dict)
        self.assertEqual(row["line"], 2)

    def test_blame_rejects_sensitive_and_symlink(self):
        self._run(["enable"])
        self.add_commit("f.py", "a\n")
        self.write_file(".env", "KEY=1\n")
        code, _ = self._run(["blame", ".env"], cwd=self.repo)
        self.assertEqual(code, 2)
        (self.repo / "link.py").symlink_to("f.py")
        code, _ = self._run(["blame", "link.py"], cwd=self.repo)
        self.assertEqual(code, 2)

    def test_hook_codex_events(self):
        code, _ = self._run(["enable"])
        self.assertEqual(code, 0)
        self.add_commit("f.py", "a\n")
        uid = "12345678-1234-1234-1234-1234567890ab"
        code, _ = self._run(["hook", "codex"], stdin=json.dumps({
            "type": "thread.started", "thread_id": uid}))
        self.assertEqual(code, 0)
        code, _ = self._run(["hook", "codex"], stdin=json.dumps({
            "type": "item.started", "session_id": uid,
            "item": {"id": "i1", "type": "file_change",
                     "changes": [{"path": "f.py", "kind": "modified"}]}}))
        self.assertEqual(code, 0)
        self.write_file("f.py", "a\nx\n")
        code, _ = self._run(["hook", "codex"], stdin=json.dumps({
            "type": "item.completed", "session_id": uid,
            "item": {"id": "i1", "type": "file_change",
                     "changes": [{"path": "f.py", "kind": "modified"}]}}))
        self.assertEqual(code, 0)
        store = Store(self.home / "partial.db")
        from partial.models import scoped_session_id
        sid = scoped_session_id(
            store.list_sessions(agent="codex")[0]["repo_id"],
            "codex", uid)
        self.assertIsNotNone(store.get_native(sid))
        git(self.repo, "add", "--", "f.py")
        commit(self.repo, "codex edit")
        code, out = self._run(["checkpoint", "--session", sid])
        self.assertEqual(code, 0, out)
        cps = store.list_checkpoints()
        rep = store.get_attribution(cps[-1]["id"])
        self.assertEqual(rep["summary"]["agent_added"], 1)

    def test_git_quoted_filename_decoding(self):
        from partial.cli import _decode_git_path
        self.assertEqual(
            _decode_git_path('"a\\nb.py"'), "a\nb.py")
        self.assertEqual(_decode_git_path("plain.py"), "plain.py")
        self.assertEqual(
            _decode_git_path('"sp\\303\\251ce.py"'), "spéce.py")

    def test_import_command_no_pending(self):
        lines = [
            {"type": "assistant", "sessionId": "imp1", "uuid": "u1",
             "timestamp": "2026-01-01T00:00:00Z",
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "name": "Edit",
                  "input": {"file_path": str(self.repo / "imp.py")}}]}},
        ]
        f = self.tmp / "claude.jsonl"
        f.write_text("\n".join(json.dumps(x) for x in lines))
        code, out = self._run(["import", "--agent", "claude", str(f)])
        self.assertEqual(code, 0)
        self.assertIn("imported 1", out)
        code, out = self._run(["import", "--agent", "claude", str(f)])
        self.assertIn("imported 0", out)
        self._run(["enable"])
        self.add_commit("imp.py", "i = 1\n")
        code, _ = self._run(["hook", "git", "post-commit"])
        self.assertEqual(code, 0)
        store = Store(self.home / "partial.db")
        self.assertEqual(store.stats()["checkpoints"], 0)

    def test_import_truncated_file_errors(self):
        f = self.tmp / "bad.jsonl"
        f.write_text('{"type":"user","message":{"role":"user",')
        code, _ = self._run(["import", "--agent", "claude", str(f)])
        self.assertEqual(code, 2)
        store = Store(self.home / "partial.db")
        self.assertEqual(store.stats()["sessions"], 0)

    def test_export_and_disable(self):
        self._run(["enable"])
        self.add_commit("e.py", "e = 1\n")
        code, out = self._run(["export", "--repo-only"])
        self.assertEqual(code, 0)
        bundle = json.loads(out)
        self.assertEqual(bundle["version"], 1)
        self.assertEqual(len(bundle["repositories"]), 1)
        self.assertNotIn("root", bundle["repositories"][0])
        code, out = self._run(["disable"])
        self.assertEqual(code, 0)

    def test_checkpoint_command_validates_sessions(self):
        self._run(["enable"])
        payload = json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s9", "prompt": "do it",
        })
        self._run(["hook", "devin"], stdin=payload)
        self.add_commit("y.py", "y = 1\n")
        store = Store(self.home / "partial.db")
        sid = store.list_sessions()[0]["id"]
        code, out = self._run(
            ["checkpoint", "--session", sid, "--json"])
        self.assertEqual(code, 0)
        cp = json.loads(out)
        self.assertIn(sid, cp["session_ids"])
        code, out = self._run(["checkpoints", "--json"])
        self.assertEqual(len(json.loads(out)), 1)
        code, _ = self._run(["checkpoint", "--session", "z" * 64])
        self.assertEqual(code, 1)
        self.assertEqual(store.stats()["checkpoints"], 1)


class FakeAgentTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.bindir = self.tmp / "bin"
        self.bindir.mkdir()
        partial = self.bindir / "partial"
        partial.write_text(
            "#!/bin/sh\nexec "
            f"{sys.executable} -m partial \"$@\"\n")
        partial.chmod(0o755)
        with patch("sys.stdout", new=io.StringIO()), \
                patch("sys.stderr", new=io.StringIO()):
            code = main(["--repo", str(self.repo), "enable"])
        self.assertEqual(code, 0)

    def _agent_env(self):
        env = dict(os.environ)
        env["PATH"] = f"{self.bindir}:{env['PATH']}"
        env["PARTIAL_HOME"] = str(self.home)
        env["PYTHONPATH"] = str(ROOT)
        return env

    def _mkagent(self, name, body):
        f = self.bindir / name
        f.write_text("#!/bin/sh\n" + body)
        f.chmod(0o755)
        return f

    def test_run_codex_midturn_commit_links(self):
        self._mkagent("codex", (
            "printf '%s\\n' '{\"type\":\"thread.started\","
            "\"thread_id\":\"thr_fake\"}'\n"
            "printf '%s\\n' '{\"type\":\"turn.started\"}'\n"
            "printf '%s\\n' '{\"type\":\"item.completed\",\"item\":{"
            "\"id\":\"i1\",\"type\":\"file_change\",\"changes\":[{"
            "\"path\":\"mid.py\",\"kind\":\"added\"}]}}'\n"
            "echo 'm = 1' > mid.py\n"
            "git add mid.py\n"
            "git -c user.name=T -c user.email=t@e commit -m mid "
            ">/dev/null\n"
            "printf '%s\\n' '{\"type\":\"item.completed\",\"item\":{"
            "\"id\":\"i2\",\"type\":\"agent_message\",\"text\":\"done\"}}'\n"
            "printf '%s\\n' 'garbage not json'\n"
            "printf '%s\\n' '{\"type\":\"turn.completed\",\"usage\":{"
            "\"input_tokens\":3,\"output_tokens\":1}}'\n"
            "echo 'some stderr' >&2\n"
            "exit 3\n"
        ))
        proc = subprocess.run(
            [sys.executable, "-m", "partial", "run", "codex",
             "fix the bug"],
            cwd=self.repo, env=self._agent_env(), capture_output=True,
            text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 3)
        self.assertIn("thr_fake", proc.stdout)
        self.assertIn("some stderr", proc.stderr)
        self.assertIn("unparsed codex output", proc.stderr)
        store = Store(self.home / "partial.db")
        sessions = store.list_sessions(agent="codex")
        self.assertEqual(len(sessions), 1)
        sess = store.get_session(sessions[0]["id"])
        kinds = [e["kind"] for e in sess["events"]]
        self.assertEqual(
            kinds[:2], ["session_start", "prompt"])
        self.assertIn("response", kinds)
        self.assertIn("usage", kinds)
        self.assertEqual(kinds[-1], "session_end")
        prompt = [e for e in sess["events"] if e["kind"] == "prompt"][0]
        self.assertEqual(prompt["text"], "fix the bug")
        cps = store.list_checkpoints()
        self.assertEqual(len(cps), 1)
        self.assertEqual(cps[0]["session_ids"], [sess["id"]])
        link = store.get_checkpoint(cps[0]["id"])["links"][0]
        self.assertEqual(link["method"], "observed-worktree-overlap")
        blob = git(
            self.repo, "show",
            f"partial/checkpoints/v1:checkpoints/{cps[0]['id']}.json")
        self.assertIn("done", blob.stdout)
        self.assertIsNotNone(store.get_native(sess["id"]))
        from partial.native import resume_plan
        plan = resume_plan(store, sess["id"])
        self.assertEqual(
            plan["argv"], ["codex", "resume", "thr_fake"])

    def test_run_requires_enabled(self):
        disabled = self.tmp / "offrepo"
        git(self.tmp, "init", "-b", "main", str(disabled))
        proc = subprocess.run(
            [sys.executable, "-m", "partial", "run", "devin", "x"],
            cwd=disabled, env=self._agent_env(), capture_output=True,
            text=True, timeout=30,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("partial enable", proc.stderr)

    def test_run_claude_argv_cwd_env(self):
        cap = self.tmp / "claude.cap"
        self._mkagent("claude", (
            f"printf '%s' \"$PWD\" > {cap}.cwd\n"
            f"printf '%s' \"$*\" > {cap}.argv\n"
            f"printf '%s' \"$PARTIAL_HOME\" > {cap}.home\n"
            "exit 0\n"
        ))
        proc = subprocess.run(
            [sys.executable, "-m", "partial", "run", "claude",
             "--print", "hello"],
            cwd=self.repo, env=self._agent_env(), capture_output=True,
            text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        argv = Path(str(cap) + ".argv").read_text()
        self.assertIn("--settings", argv)
        self.assertIn("claude-settings.json", argv)
        self.assertTrue(argv.endswith("--print hello"))
        self.assertEqual(
            Path(str(cap) + ".cwd").read_text(),
            str(Path(self.repo).resolve()))
        self.assertEqual(
            Path(str(cap) + ".home").read_text(), str(self.home))

    def test_run_devin_passthrough(self):
        cap = self.tmp / "devin.cap"
        self._mkagent("devin", (
            f"printf '%s' \"$PWD\" > {cap}.cwd\n"
            f"printf '%s' \"$*\" > {cap}.argv\n"
            "exit 7\n"
        ))
        proc = subprocess.run(
            [sys.executable, "-m", "partial", "run", "devin",
             "sess", "new"],
            cwd=self.repo, env=self._agent_env(), capture_output=True,
            text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 7)
        self.assertEqual(
            Path(str(cap) + ".argv").read_text(), "sess new")
        self.assertEqual(
            Path(str(cap) + ".cwd").read_text(),
            str(Path(self.repo).resolve()))


class MemoryCliTests(RepoTestCase):
    def _run(self, argv, stdin=""):
        return self._run_at(self.repo, argv, stdin=stdin)

    def _run_at(self, repo_arg, argv, stdin=""):
        argv = ["--repo", str(repo_arg), *argv]
        with patch("sys.stdout", new=io.StringIO()) as out, \
                patch("sys.stdin", io.StringIO(stdin)), \
                patch("sys.stderr", new=io.StringIO()) as err:
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def _docs(self, where=""):
        store = Store(self.home / "partial.db")
        conn = store._connect()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM memory_documents" + where).fetchall()]
        finally:
            conn.close()

    # ---- enable ----------------------------------------------------
    def test_enable_default_installs_hooks_only(self):
        code, out, _ = self._run(["enable"])
        self.assertEqual(code, 0, out)
        hook = self.repo / ".git" / "hooks" / "post-commit"
        self.assertTrue(hook.exists())
        self.assertTrue(
            (self.repo / ".devin" / "hooks.v1.json").exists())
        skill = self.repo / ".devin" / "skills" / "partial-memory" \
            / "SKILL.md"
        self.assertFalse(skill.exists())

    def test_enable_memory_skill_and_skill_only(self):
        from partial.brain_contract import BRAIN_SKILL
        skill = self.repo / ".devin" / "skills" / "partial-memory" \
            / "SKILL.md"
        code, out, _ = self._run(["enable", "--memory-skill"])
        self.assertEqual(code, 0, out)
        self.assertEqual(skill.read_text(), BRAIN_SKILL)
        self.assertTrue(
            (self.repo / ".git" / "hooks" / "post-commit").exists())
        repo2 = init_repo(self.tmp / "repo2")
        code, out, _ = self._run_at(repo2, ["enable", "--skill-only"])
        self.assertEqual(code, 0, out)
        self.assertEqual(
            (repo2 / ".devin" / "skills" / "partial-memory"
             / "SKILL.md").read_text(), BRAIN_SKILL)
        self.assertFalse(
            (repo2 / ".git" / "hooks" / "post-commit").exists())
        self.assertFalse(
            (repo2 / ".devin" / "hooks.v1.json").exists())
        self.assertFalse(
            (repo2 / ".git" / "partial" / "config.json").exists())

    def test_enable_foreign_skill_not_gated_without_flag(self):
        skill = self.repo / ".devin" / "skills" / "partial-memory" \
            / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("foreign")
        code, _, _ = self._run(["enable"])
        self.assertEqual(code, 0)
        self.assertEqual(skill.read_text(), "foreign")
        self.assertTrue(
            (self.repo / ".git" / "hooks" / "post-commit").exists())
        # requesting the skill against foreign content preflights to a
        # failure without touching hooks that are already consistent
        code, _, err = self._run(["enable", "--memory-skill"])
        self.assertEqual(code, 2)
        self.assertEqual(skill.read_text(), "foreign")

    def test_enable_preflight_blocks_all_writes(self):
        skill = self.repo / ".devin" / "skills" / "partial-memory" \
            / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("foreign")
        # also make the git hook unpreflightable in a fresh repo
        repo2 = init_repo(self.tmp / "repo3")
        s2 = repo2 / ".devin" / "skills" / "partial-memory" / "SKILL.md"
        s2.parent.mkdir(parents=True)
        s2.write_text("foreign")
        code, _, err = self._run_at(
            repo2, ["enable", "--memory-skill"])
        self.assertEqual(code, 2)
        self.assertIn("foreign", err)
        self.assertFalse(
            (repo2 / ".git" / "hooks" / "post-commit").exists())
        self.assertFalse(
            (repo2 / ".devin" / "hooks.v1.json").exists())

    # ---- doctor ------------------------------------------------------
    def test_doctor_worktree_uses_real_hooks_dir(self):
        code, _, _ = self._run(["enable"])
        self.assertEqual(code, 0)
        self.add_commit("w.py", "w = 1\n")
        wt = self.tmp / "wt"
        git(self.repo, "worktree", "add", str(wt), "HEAD")
        code, out, _ = self._run_at(wt, ["doctor", "--json"])
        data = json.loads(out)
        by_name = {c["check"]: c for c in data["checks"]}
        self.assertTrue(by_name["capture-hook"]["ok"])
        self.assertTrue(by_name["repo"]["ok"])
        self.assertIn("agent-hooks",
                      [c["check"] for c in data["checks"]])
        # doctor is read-only: no repo config was created
        self.assertFalse((wt / ".devin").exists())

    def test_doctor_no_repo_does_not_create_config(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        code, out, _ = self._run_at(plain, ["doctor", "--json"])
        data = json.loads(out)
        self.assertIn(code, (0, 1))
        self.assertFalse((plain / ".devin").exists())
        by_name = {c["check"]: c for c in data["checks"]}
        self.assertIn("plugins", by_name)
        self.assertIn("mcp", by_name)

    # ---- repo scoping -------------------------------------------------
    def test_search_context_ask_scope_current_repo(self):
        self.write_file("mod.py",
                        "def quixotic_umbrella():\n    return 7\n")
        git(self.repo, "add", "--", "mod.py")
        commit(self.repo, "add umbrella")
        code, _, _ = self._run(["index"])
        self.assertEqual(code, 0)
        repo_b = init_repo(self.tmp / "repoB")
        (repo_b / "b.py").write_text("b = 1\n")
        git(repo_b, "add", "--", "b.py")
        git(repo_b, "-c", "user.name=T", "-c", "user.email=t@e",
            "commit", "-qm", "b")
        code, out, _ = self._run_at(repo_b, ["search",
                                             "quixotic_umbrella"])
        self.assertEqual(code, 0)
        self.assertNotIn("mod.py", out)
        code, out, _ = self._run_at(
            repo_b, ["search", "quixotic_umbrella", "--all-repos",
                     "--json"])
        rows = json.loads(out)
        self.assertTrue(any(r.get("path") == "mod.py" for r in rows))
        code, out, _ = self._run_at(
            repo_b, ["context", "quixotic_umbrella", "--json"])
        self.assertEqual(
            json.loads(out)["documents"], [])
        code, out, _ = self._run_at(
            repo_b, ["context", "quixotic_umbrella", "--all-repos",
                     "--json"])
        self.assertNotEqual(json.loads(out)["documents"], [])
        code, out, _ = self._run_at(
            repo_b, ["ask", "what does quixotic_umbrella do"])
        planned = json.loads(out)
        self.assertEqual(planned["status"], "planned")
        self.assertEqual(planned["evidence"], [])
        code, out, _ = self._run_at(
            repo_b, ["ask", "what does quixotic_umbrella do",
                     "--all-repos"])
        planned = json.loads(out)
        self.assertNotEqual(planned["evidence"], [])

    def test_dispatch_scoped_to_current_repo(self):
        sha = self.add_commit("d.py", "d = 1\n")
        code, out, _ = self._run(["checkpoint"])
        self.assertEqual(code, 0)
        code, out, _ = self._run(["dispatch"])
        self.assertIn(sha[:10], out)
        repo_b = init_repo(self.tmp / "repoB")
        (repo_b / "x.py").write_text("x\n")
        git(repo_b, "add", "--", "x.py")
        git(repo_b, "-c", "user.name=T", "-c", "user.email=t@e",
            "commit", "-qm", "x")
        code, out, _ = self._run_at(repo_b, ["dispatch"])
        self.assertEqual(code, 0)
        self.assertNotIn(sha[:10], out)

    # ---- review -------------------------------------------------------
    def _review_doc_texts(self):
        store = Store(self.home / "partial.db")
        conn = store._connect()
        try:
            return [r["text"] for r in conn.execute(
                "SELECT text FROM memory_documents"
                " WHERE source_id LIKE 'review:%'").fetchall()]
        finally:
            conn.close()

    def test_review_first_commit_and_diff_evidence(self):
        self.add_commit("feat.py", "def feat():\n    return 1\n")
        code, out, err = self._run(["review"])
        self.assertEqual(code, 0, err)
        planned = json.loads(out)
        self.assertEqual(planned["status"], "planned")
        self.assertEqual(len(planned["source_ids"]), 1)
        texts = self._review_doc_texts()
        self.assertEqual(len(texts), 1)
        self.assertIn("feat.py", texts[0])
        self.assertIn("+def feat():", texts[0])
        docs = self._docs(" WHERE source_id LIKE 'review:%'")
        self.assertEqual(planned["source_ids"], [docs[0]["id"]])

    def test_review_base_ref_and_bad_base(self):
        self.add_commit("a.py", "a = 1\n")
        self.write_file("a.py", "a = 2\n")
        git(self.repo, "add", "--", "a.py")
        commit(self.repo, "second")
        code, out, err = self._run(["review", "run", "--base",
                                    "HEAD^"])
        self.assertEqual(code, 0, err)
        texts = self._review_doc_texts()
        self.assertIn("-a = 1", texts[-1])
        self.assertIn("+a = 2", texts[-1])
        code, _, err = self._run(
            ["review", "run", "--base", "not-a-real-ref"])
        self.assertEqual(code, 2)
        self.assertIn("cannot resolve revision", err)

    def test_review_excludes_sensitive_paths(self):
        self.add_commit("app.py", "x = 1\n")
        self.write_file(".env", "SUPERSECRET=hunter2\n")
        self.write_file("app.py", "x = 2\n")
        git(self.repo, "add", "-A")
        commit(self.repo, "add env and change")
        code, out, err = self._run(["review"])
        self.assertEqual(code, 0, err)
        texts = self._review_doc_texts()
        self.assertEqual(len(texts), 1)
        self.assertIn("excluded sensitive paths", texts[0])
        self.assertIn("app.py", texts[0])
        self.assertNotIn("hunter2", texts[0])
        self.assertNotIn("+++ b/.env", texts[0])
        self.assertNotIn("--- a/.env", texts[0])

    def test_review_requires_repo(self):
        plain = self.tmp / "plain2"
        plain.mkdir()
        code, _, err = self._run_at(plain, ["review"])
        self.assertEqual(code, 2)
        self.assertIn("requires a git repository", err)
        self.assertEqual(self._docs(" WHERE source_id LIKE 'review:%'"),
                         [])

    def test_review_show_unknown_run(self):
        code, _, err = self._run(["review", "show", "nope"])
        self.assertEqual(code, 1)

    # ---- investigate ----------------------------------------------------
    def test_investigate_seed_requires_repo_scope(self):
        seed = self.tmp / "seed.md"
        seed.write_text("the flaky freeze repro\n")
        plain = self.tmp / "plain3"
        plain.mkdir()
        code, _, err = self._run_at(
            plain, ["investigate", "run", "why flaky", "--seed",
                    str(seed)])
        self.assertEqual(code, 2)
        self.assertIn("repository scope", err)
        self.assertEqual(self._docs(), [])

    def test_investigate_seed_registered_and_capped(self):
        seed = self.tmp / "seed.md"
        seed.write_text("trace line one\n")
        code, out, err = self._run(
            ["investigate", "run", "why does it freeze", "--seed",
             str(seed)])
        self.assertEqual(code, 0, err)
        planned = json.loads(out)
        self.assertEqual(planned["status"], "planned")
        docs = [d for d in self._docs() if d["source_id"]
                == "seed:seed.md"]
        self.assertEqual(len(docs), 1)
        store = Store(self.home / "partial.db")
        rid = store.list_repos()[0]["id"]
        self.assertEqual(docs[0]["repo_id"], rid)
        self.assertIn(docs[0]["id"], planned["source_ids"])
        link = self.tmp / "seedlink.md"
        link.symlink_to(seed)
        code, _, err = self._run(
            ["investigate", "run", "q", "--seed", str(link)])
        self.assertEqual(code, 2)
        big = self.tmp / "big.md"
        big.write_text("x" * (64 * 1024 + 1))
        code, _, err = self._run(
            ["investigate", "run", "q", "--seed", str(big)])
        self.assertEqual(code, 2)
        self.assertIn("64 KiB", err)

    # ---- semantic flags ---------------------------------------------------
    def test_semantic_flags_fail_without_provider(self):
        saved = os.environ.pop("PARTIAL_OPENAI_API_KEY", None)
        self.addCleanup(
            lambda: saved is not None
            and os.environ.__setitem__(
                "PARTIAL_OPENAI_API_KEY", saved))
        self.add_commit("s.py", "s = 1\n")
        code, _, err = self._run(["index", "--semantic"])
        self.assertNotEqual(code, 0)
        code, _, _ = self._run(["index"])
        self.assertEqual(code, 0)
        code, _, err = self._run(["search", "s", "--semantic"])
        self.assertNotEqual(code, 0)
        code, _, _ = self._run(["search", "s"])
        self.assertEqual(code, 0)
        code, _, err = self._run(["context", "s", "--semantic"])
        self.assertNotEqual(code, 0)


class ServerCliTests(RepoTestCase):
    def _run(self, argv, stdin=""):
        argv = ["--repo", str(self.repo), *argv]
        with patch("sys.stdout", new=io.StringIO()) as out, \
                patch("sys.stdin", io.StringIO(stdin)), \
                patch("sys.stderr", new=io.StringIO()):
            code = main(argv)
        return code, out.getvalue()

    def test_auth_token_created_and_stable(self):
        with patch("sys.stdout", new=io.StringIO()) as out:
            code = main(["auth", "token"])
        self.assertEqual(code, 0)
        token = out.getvalue().strip()
        self.assertGreaterEqual(len(token), 32)
        tokfile = self.home / "server-token"
        self.assertTrue(tokfile.exists())
        self.assertEqual(oct(tokfile.stat().st_mode & 0o777), "0o600")
        with patch("sys.stdout", new=io.StringIO()) as out2:
            main(["auth", "token"])
        self.assertEqual(out2.getvalue().strip(), token)

    def test_auth_token_bad_env(self):
        os.environ["PARTIAL_TOKEN"] = "short"
        try:
            code, _ = self._run(["auth", "token"])
            self.assertEqual(code, 1)
        finally:
            os.environ.pop("PARTIAL_TOKEN", None)

    def test_ingest_bundle(self):
        bundle = {"version": 1, "repositories": [], "sessions": [],
                  "events": [], "checkpoints": []}
        f = self.tmp / "b.json"
        f.write_text(json.dumps(bundle))
        with patch("sys.stdout", new=io.StringIO()) as out:
            code = main(["ingest-bundle", str(f)])
        self.assertEqual(code, 0)
        self.assertIn("imported bundle", out.getvalue())
        f.write_text("{bad")
        code, _ = self._run(["ingest-bundle", str(f)])
        self.assertEqual(code, 2)

    def test_account_create_and_token(self):
        code, out = self._run(
            ["account", "create", "--email", "a@x.test",
             "--name", "A", "--password-stdin"],
            stdin="password-one-two\n")
        self.assertEqual(code, 0)
        self.assertIn("a@x.test", out)
        code, _ = self._run(["auth", "token"])
        self.assertEqual(code, 2)
        code, _ = self._run(
            ["account", "create", "--email", "b@x.test",
             "--name", "B", "--password-stdin"],
            stdin="password-one-two\n")
        self.assertEqual(code, 2)
        from partial.accounts import Accounts
        acc = Accounts(self.home, self.home / "partial.db")
        principal = acc.check_password("a@x.test", "password-one-two")
        ws = acc.workspaces(principal)[0]
        code, out = self._run(
            ["account", "token", "--email", "a@x.test",
             "--workspace", ws["id"], "--name", "ci",
             "--password-stdin"], stdin="password-one-two\n")
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().startswith("ptk_"))
        code, _ = self._run(
            ["account", "token", "--email", "a@x.test",
             "--workspace", ws["id"], "--name", "ci",
             "--password-stdin"], stdin="wrong password\n")
        self.assertEqual(code, 2)
        for role in ("admin", "owner"):
            with self.assertRaises(SystemExit) as cm:
                self._run(
                    ["account", "token", "--email", "a@x.test",
                     "--workspace", ws["id"], "--name", "ci",
                     "--role", role, "--password-stdin"],
                    stdin="password-one-two\n")
            self.assertEqual(cm.exception.code, 2)

    def test_upload_validation(self):
        code, _ = self._run(["upload", "http://example.com"])
        self.assertEqual(code, 2)
        os.environ["PARTIAL_SERVER_TOKEN"] = "t" * 40
        try:
            code, _ = self._run(
                ["upload", "http://user:pw@example.com"])
            self.assertEqual(code, 2)
            code, _ = self._run(["upload", "http://example.com/x?y=1"])
            self.assertEqual(code, 2)
            code, _ = self._run(
                ["upload", "http://example.com"])
            self.assertEqual(code, 2)
        finally:
            os.environ.pop("PARTIAL_SERVER_TOKEN", None)

    def test_upload_opener_disables_env_proxies_and_redirects(self):
        from partial.cli import _NoRedirect, _upload_opener
        # Environment proxy settings must never see the Authorization
        # header. Passing ProxyHandler({}) suppresses urllib's default
        # env-reading ProxyHandler, so no proxy handler is installed.
        with patch.dict(os.environ, {
                "http_proxy": "http://127.0.0.1:9",
                "https_proxy": "http://127.0.0.1:9",
                "HTTP_PROXY": "http://127.0.0.1:9",
                "HTTPS_PROXY": "http://127.0.0.1:9"}):
            opener = _upload_opener()
        self.assertFalse(any(
            isinstance(h, urllib.request.ProxyHandler)
            for h in opener.handlers))
        redirects = [h for h in opener.handlers if isinstance(
            h, urllib.request.HTTPRedirectHandler)]
        self.assertEqual(len(redirects), 1)
        self.assertIsInstance(redirects[0], _NoRedirect)
        self.assertIsNone(redirects[0].redirect_request(
            None, None, 302, "Found", {}, "https://other.example/"))


if __name__ == "__main__":
    unittest.main()
