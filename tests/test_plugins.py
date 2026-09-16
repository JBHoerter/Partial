import hashlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from partial.cli import main

from helpers import RepoTestCase


class PluginTests(RepoTestCase):
    def _run(self, argv, stdin=""):
        argv = ["--repo", str(self.repo), *argv]
        with patch("sys.stdout", new=io.StringIO()) as out, \
                patch("sys.stdin", io.StringIO(stdin)), \
                patch("sys.stderr", new=io.StringIO()) as err:
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def _plugin(self, body="#!/bin/sh\nenv\n"):
        path = self.tmp / "plug.sh"
        path.write_text(body)
        path.chmod(0o755)
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_register_list_run(self):
        marker = self.tmp / "env.txt"
        path, digest = self._plugin(f"#!/bin/sh\nenv > {marker}\n")
        code, out, _ = self._run(
            ["plugin", "register", "envdump", "--command",
             str(path), "--sha256", digest])
        self.assertEqual(code, 0)
        code, out, _ = self._run(["plugin", "list"])
        self.assertIn("envdump", out)
        os.environ["PARTIAL_SECRET_TOKEN_TEST"] = "hunter2"
        try:
            code, _, _ = self._run(["plugin", "run", "envdump"])
        finally:
            os.environ.pop("PARTIAL_SECRET_TOKEN_TEST", None)
        self.assertEqual(code, 0)
        dumped = marker.read_text()
        self.assertIn("PARTIAL_HOME=", dumped)
        self.assertNotIn("hunter2", dumped)
        self.assertNotIn("PARTIAL_SECRET_TOKEN_TEST", dumped)

    def test_digest_change_refused(self):
        path, digest = self._plugin()
        code, _, _ = self._run(
            ["plugin", "register", "p1", "--command", str(path),
             "--sha256", digest])
        self.assertEqual(code, 0)
        path.write_text("#!/bin/sh\necho tampered\n")
        code, _, err = self._run(["plugin", "run", "p1"])
        self.assertEqual(code, 1)
        self.assertIn("digest", err)

    def test_shadow_builtin_and_relative_refused(self):
        path, digest = self._plugin()
        code, _, err = self._run(
            ["plugin", "register", "search", "--command", str(path),
             "--sha256", digest])
        self.assertEqual(code, 2)
        code, _, err = self._run(
            ["plugin", "register", "ok", "--command", "rel/path",
             "--sha256", digest])
        self.assertEqual(code, 2)

    def test_passthrough_argv(self):
        marker = self.tmp / "out.txt"
        path = self.tmp / "p.sh"
        path.write_text(f'#!/bin/sh\necho "$@" > {marker}\n')
        path.chmod(0o755)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self._run(["plugin", "register", "echoer", "--command",
                   str(path), "--sha256", digest])
        code, _, _ = self._run(
            ["plugin", "run", "echoer", "--", "a", "b c"])
        self.assertEqual(code, 0)
        self.assertIn("a b c", marker.read_text())

    def test_malformed_plugins_json_errors_and_survives(self):
        pf = self.repo / ".devin" / "partial" / "plugins.json"
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text("{broken json")
        code, _, err = self._run(["plugin", "list"])
        self.assertEqual(code, 1)
        self.assertIn("malformed", err)
        path, digest = self._plugin()
        code, _, err = self._run(
            ["plugin", "register", "later", "--command", str(path),
             "--sha256", digest])
        self.assertEqual(code, 1)
        self.assertEqual(pf.read_text(), "{broken json")
        code, out, _ = self._run(["doctor", "--json"])
        data = json.loads(out)
        plug = [c for c in data["checks"]
                if c["check"] == "plugins"][0]
        self.assertFalse(plug["ok"])
        self.assertEqual(pf.read_text(), "{broken json")

    def test_register_rejects_non_regular_and_bad_digest(self):
        path, digest = self._plugin()
        code, _, _ = self._run(
            ["plugin", "register", "d", "--command", str(self.tmp),
             "--sha256", digest])
        self.assertEqual(code, 2)
        code, _, _ = self._run(
            ["plugin", "register", "m", "--command",
             str(self.tmp / "missing.sh"), "--sha256", digest])
        self.assertEqual(code, 2)
        link = self.tmp / "link.sh"
        link.symlink_to(path)
        code, _, _ = self._run(
            ["plugin", "register", "l", "--command", str(link),
             "--sha256", digest])
        self.assertEqual(code, 2)
        code, _, _ = self._run(
            ["plugin", "register", "u", "--command", str(path),
             "--sha256", "A" * 64])
        self.assertEqual(code, 2)
        code, _, _ = self._run(
            ["plugin", "register", "s", "--command", str(path),
             "--sha256", digest[:40]])
        self.assertEqual(code, 2)

    def test_run_unknown_and_exit_code_passthrough(self):
        code, _, err = self._run(["plugin", "run", "ghost"])
        self.assertEqual(code, 1)
        self.assertIn("unknown plugin", err)
        path = self.tmp / "seven.sh"
        path.write_text("#!/bin/sh\nexit 7\n")
        path.chmod(0o755)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        code, _, _ = self._run(
            ["plugin", "register", "seven", "--command", str(path),
             "--sha256", digest])
        self.assertEqual(code, 0)
        code, _, _ = self._run(["plugin", "run", "seven"])
        self.assertEqual(code, 7)

    def test_project_cli(self):
        code, out, _ = self._run(["project", "create", "web"])
        self.assertEqual(code, 0)
        pid = json.loads(out)["id"]
        code, out, _ = self._run(
            ["project", "attach", pid])
        self.assertEqual(code, 0)
        code, out, _ = self._run(["project", "list"])
        self.assertIn("web", out)

    def test_enable_memory_skill(self):
        code, out, _ = self._run(["enable", "--memory-skill"])
        self.assertEqual(code, 0)
        skill = self.repo / ".devin" / "skills" / "partial-memory" \
            / "SKILL.md"
        self.assertTrue(skill.exists())
        from partial.brain_contract import BRAIN_SKILL
        self.assertEqual(skill.read_text(), BRAIN_SKILL)
        # foreign content is never overwritten
        skill.write_text("foreign")
        code, out, err = self._run(["enable", "--memory-skill"])
        self.assertEqual(code, 2)
        self.assertEqual(skill.read_text(), "foreign")

    def test_configure_show_and_doctor(self):
        code, out, _ = self._run(["configure", "--show"])
        self.assertEqual(code, 0)
        cfg = json.loads(out)
        self.assertIn("ai_provider_configured", cfg)
        self.assertFalse(cfg["external_ai_enabled"])
        self.assertTrue(cfg["fts5"])
        code, out, _ = self._run(["doctor", "--json"])
        data = json.loads(out)
        names = [c["check"] for c in data["checks"]]
        for want in ("capture-hook", "native-registry",
                     "attribution", "memory-fts5", "ai-provider",
                     "plugins", "mcp"):
            self.assertIn(want, names)


if __name__ == "__main__":
    unittest.main()
