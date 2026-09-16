import os
import subprocess
import tempfile
import unittest
from pathlib import Path


def git(repo, *args, check=True):
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=30,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {proc.stderr}")
    return proc


def commit(repo, message="commit"):
    return git(repo, "-c", "user.name=Test", "-c",
               "user.email=test@example.com", "commit", "-m", message)


def init_repo(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "main")
    return path


class RepoTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.repo = init_repo(self.tmp / "repo")
        self._env = {
            "PARTIAL_HOME": str(self.home),
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }
        self._saved = {}
        for k, v in self._env.items():
            self._saved[k] = os.environ.get(k)
            os.environ[k] = v
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def write_file(self, rel, content):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def add_commit(self, rel, content, message="commit"):
        self.write_file(rel, content)
        git(self.repo, "add", "--", rel)
        commit(self.repo, message)
        return git(self.repo, "rev-parse", "HEAD").stdout.strip()
