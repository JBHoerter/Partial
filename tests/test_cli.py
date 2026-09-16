import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from partial.cli import main
from partial.models import scoped_session_id
from partial.store import Store

from helpers import RepoTestCase, git

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
    def _run(self, argv, stdin=""):
        argv = ["--repo", str(self.repo), *argv]
        with patch("sys.stdout", new=io.StringIO()) as out, \
                patch("sys.stdin", io.StringIO(stdin)), \
                patch("sys.stderr", new=io.StringIO()):
            code = main(argv)
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


if __name__ == "__main__":
    unittest.main()
