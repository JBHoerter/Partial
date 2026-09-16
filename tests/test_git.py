import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path

from partial.git import (
    METADATA_REF,
    check_git_hook,
    create_checkpoint,
    disable_hooks,
    discover_repo,
    install_claude_hooks,
    install_devin_hooks,
    install_hooks,
    persist_checkpoint,
    sync_checkpoints,
)
from partial.models import Event, scoped_session_id
from partial.store import Store

from helpers import RepoTestCase, commit, git, init_repo

TS = "2026-01-01T00:00:00Z"
ROOT = Path(__file__).resolve().parent.parent


def tool_event(eid, path):
    return Event(
        id=eid, session_id="s1", agent="devin", kind="tool",
        timestamp=TS, tool_name="edit_file",
        data={"tool_input": {"file_path": path}},
    )


def prompt_event(eid, sid="s1", text="x"):
    return Event(id=eid, session_id=sid, agent="devin", kind="prompt",
                 timestamp=TS, text=text)


class DiscoverTests(RepoTestCase):
    def test_discover(self):
        self.add_commit("a.py", "x = 1\n")
        repo = discover_repo(self.repo)
        self.assertEqual(Path(repo["root"]), self.repo.resolve())
        self.assertEqual(repo["branch"], "main")
        (self.repo / "sub").mkdir()
        sub = discover_repo(self.repo / "sub")
        self.assertEqual(sub["root"], repo["root"])


class CheckpointTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.home / "partial.db")
        self.repo_row = self.store.register_repo(self.repo)
        self.rid = self.repo_row["id"]
        self.grepo = discover_repo(self.repo)
        self.wt = str(Path(self.repo).resolve())

    def _persist(self, cp):
        return persist_checkpoint(
            self.grepo, self.store.checkpoint_bundle(cp["id"]), cp["id"])

    def test_root_commit_checkpoint(self):
        sha = self.add_commit("src/main.py", "print('hi')\n")
        cp = create_checkpoint(self.store, self.rid, worktree=self.wt)
        self.assertEqual(cp["commit_sha"], sha)
        self.assertIn("src/main.py", cp["files"])
        self.assertIn("+print('hi')", cp["diff"])
        self.assertEqual(cp["message"], "commit")

    def test_sensitive_file_diff_omitted(self):
        self.write_file(".env", "SECRET=hunter2\n")
        self.write_file("ok.py", "ok = 1\n")
        git(self.repo, "add", "--", ".env", "ok.py")
        commit(self.repo)
        cp = create_checkpoint(self.store, self.rid, worktree=self.wt)
        self.assertIn(".env", cp["files"])
        self.assertNotIn("hunter2", cp["diff"] or "")
        self.assertIn("+ok = 1", cp["diff"])

    def test_paths_with_spaces_and_quotes(self):
        rel = 'dir with space/a "b".py'
        sha = self.add_commit(rel, "z = 3\n")
        cp = create_checkpoint(self.store, self.rid, worktree=self.wt)
        self.assertEqual(cp["commit_sha"], sha)
        self.assertIn(rel, cp["files"])
        self.assertIn("+z = 3", cp["diff"])

    def test_merge_commit_first_parent(self):
        self.add_commit("base.py", "b = 0\n")
        git(self.repo, "checkout", "-b", "feature")
        self.add_commit("feat.py", "f = 1\n")
        git(self.repo, "checkout", "main")
        self.add_commit("mainline.py", "m = 1\n")
        git(self.repo, "-c", "user.name=T", "-c", "user.email=t@e",
            "merge", "--no-ff", "-m", "merge feature", "feature")
        cp = create_checkpoint(self.store, self.rid, worktree=self.wt)
        self.assertIn("feat.py", cp["files"])
        self.assertIn("+f = 1", cp["diff"])
        self.assertNotIn("mainline.py", cp["files"])

    def test_auto_link_observed_paths(self):
        self.store.ingest(
            self.rid, [tool_event("t1", str(self.repo / "a.py"))],
            worktree=self.wt)
        self.add_commit("a.py", "a = 1\n")
        cp = create_checkpoint(self.store, self.rid, worktree=self.wt)
        sid = scoped_session_id(self.rid, "devin", "s1")
        self.assertEqual(cp["session_ids"], [sid])
        self.assertEqual(
            self.store.get_checkpoint(cp["id"])["links"],
            [{"session_id": sid,
              "method": "observed-worktree-overlap"}])

    def test_unrelated_commit_not_linked(self):
        self.store.ingest(
            self.rid, [tool_event("t1", str(self.repo / "a.py"))],
            worktree=self.wt)
        self.add_commit("unrelated.py", "u = 1\n")
        cp = create_checkpoint(self.store, self.rid, worktree=self.wt)
        self.assertEqual(cp["session_ids"], [])

    def test_preexisting_dirty_not_claimed(self):
        self.write_file("dirty.py", "d = 1\n")
        self.store.ingest(self.rid, [Event(
            id="e0", session_id="s2", agent="devin", kind="session_start",
            timestamp=TS)], worktree=self.wt)
        git(self.repo, "add", "--", "dirty.py")
        commit(self.repo)
        cp = create_checkpoint(self.store, self.rid, worktree=self.wt)
        self.assertEqual(cp["session_ids"], [])

    def test_require_links_returns_none(self):
        self.add_commit("solo.py", "s = 1\n")
        cp = create_checkpoint(
            self.store, self.rid, worktree=self.wt, require_links=True)
        self.assertIsNone(cp)
        self.assertEqual(self.store.stats()["checkpoints"], 0)

    def test_explicit_session_validation(self):
        self.store.ingest(self.rid, [prompt_event("e0", "s9", "manual")])
        self.add_commit("m.py", "m = 1\n")
        sid = scoped_session_id(self.rid, "devin", "s9")
        cp = create_checkpoint(
            self.store, self.rid, session_ids=[sid], worktree=self.wt)
        self.assertEqual(cp["session_ids"], [sid])
        cp2 = create_checkpoint(
            self.store, self.rid, session_ids=["s9"], worktree=self.wt)
        self.assertEqual(cp2["session_ids"], [sid])
        with self.assertRaises(ValueError):
            create_checkpoint(
                self.store, self.rid, session_ids=["x" * 64],
                worktree=self.wt)
        with self.assertRaises(ValueError):
            create_checkpoint(
                self.store, self.rid, session_ids=["nosuch"],
                worktree=self.wt)
        self.assertEqual(self.store.stats()["checkpoints"], 1)

    def test_metadata_branch_preserves_head_and_index(self):
        self.add_commit("a.py", "a = 1\n")
        head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        cp = create_checkpoint(
            self.store, self.rid, session_ids=None, worktree=self.wt)
        commit_sha = self._persist(cp)
        self.assertTrue(commit_sha)
        self.assertEqual(
            git(self.repo, "rev-parse", "HEAD").stdout.strip(), head)
        status = git(self.repo, "status", "--porcelain").stdout
        self.assertEqual(status, "")
        blob = git(
            self.repo, "show",
            f"{METADATA_REF}:checkpoints/{cp['id']}.json").stdout
        data = json.loads(blob)
        self.assertEqual(data["checkpoints"][0]["id"], cp["id"])
        self.assertEqual(
            data["checkpoints"][0]["commit_sha"], cp["commit_sha"])

    def test_persist_requires_bundle_and_hex_id(self):
        self.add_commit("a.py", "a = 1\n")
        cp = create_checkpoint(self.store, self.rid, worktree=self.wt)
        with self.assertRaises(Exception):
            persist_checkpoint(self.grepo, cp, "not-hex")
        with self.assertRaises(Exception):
            persist_checkpoint(
                self.grepo, {"version": 1, "checkpoints": []}, cp["id"])

    def test_commit_message_redacted(self):
        self.write_file("s.py", "s = 1\n")
        git(self.repo, "add", "--", "s.py")
        git(self.repo, "-c", "user.name=T", "-c", "user.email=t@e",
            "commit", "-m", "add PASSWORD=hunter2secret")
        cp = create_checkpoint(self.store, self.rid, worktree=self.wt)
        self.assertNotIn("hunter2secret", cp["message"] or "")


class WorktreeTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.home / "partial.db")
        self.add_commit("base.py", "b = 1\n")
        self.wtB = self.tmp / "wtB"
        git(self.repo, "worktree", "add", str(self.wtB), "-b", "wtb")
        self.gA = discover_repo(self.repo)
        self.gB = discover_repo(self.wtB)
        self.assertEqual(self.gA["common_dir"], self.gB["common_dir"])
        self.rid = self.store.register_repo(self.repo)["id"]
        self.wtA = str(Path(self.repo).resolve())
        self.wtB_r = str(Path(self.wtB).resolve())

    def _commit_in(self, worktree, rel, content):
        p = Path(worktree) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        git(worktree, "add", "--", rel)
        git(worktree, "-c", "user.name=T", "-c", "user.email=t@e",
            "commit", "-m", "c")

    def test_two_worktrees_isolated(self):
        sid = scoped_session_id(self.rid, "devin", "s1")
        self.store.ingest(
            self.rid, [tool_event("tB", str(self.wtB / "same.py"))],
            worktree=str(self.wtB))
        self._commit_in(self.repo, "same.py", "a = 1\n")
        cpA = create_checkpoint(self.store, self.rid, worktree=self.wtA)
        self.assertEqual(cpA["session_ids"], [])
        self._commit_in(self.wtB, "same.py", "b = 2\n")
        shaB = git(self.wtB, "rev-parse", "HEAD").stdout.strip()
        cpB = create_checkpoint(
            self.store, self.rid, worktree=str(self.wtB))
        self.assertEqual(cpB["commit_sha"], shaB)
        self.assertEqual(cpB["session_ids"], [sid])

    def test_worktree_mismatch_rejected(self):
        other = init_repo(self.tmp / "unrelated")
        self._commit_in(other, "o.py", "o = 1\n")
        with self.assertRaises(Exception):
            create_checkpoint(
                self.store, self.rid, worktree=str(other))


class HookInstallTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.grepo = discover_repo(self.repo)

    def test_install_nested_format_idempotent(self):
        r1 = install_hooks(self.grepo)
        self.assertIn("git:post-commit", r1["installed"])
        hook = self.repo / ".git" / "hooks" / "post-commit"
        content = hook.read_text()
        self.assertIn("PARTIAL_MANAGED_HOOK=1", content)
        mode = stat.S_IMODE(hook.stat().st_mode)
        self.assertTrue(mode & 0o111)
        install_devin_hooks(self.grepo)
        path = self.repo / ".devin" / "hooks.v1.json"
        data = json.loads(path.read_text())
        entry = data["SessionStart"][0]
        self.assertEqual(entry["matcher"], "")
        self.assertEqual(entry["hooks"], [{
            "type": "command",
            "command": "partial hook devin SessionStart",
            "timeout": 10}])
        install_devin_hooks(self.grepo)
        data2 = json.loads(path.read_text())
        self.assertEqual(len(data2["SessionStart"]), 1)
        r2 = install_hooks(self.grepo)
        self.assertEqual(r2["errors"], [])

    def test_existing_hook_refused(self):
        hook = self.repo / ".git" / "hooks" / "post-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho mine\n")
        self.assertIsNotNone(check_git_hook(self.grepo))
        result = install_hooks(self.grepo)
        self.assertTrue(result["errors"])
        self.assertEqual(hook.read_text(), "#!/bin/sh\necho mine\n")

    def test_shared_hookspath_refused(self):
        outside = self.tmp / "shared-hooks"
        outside.mkdir()
        link = self.repo / ".git" / "hooks"
        if link.exists():
            for f in link.iterdir():
                f.unlink()
            link.rmdir()
        link.symlink_to(outside)
        self.assertIsNotNone(check_git_hook(self.grepo))
        result = install_hooks(self.grepo)
        self.assertTrue(result["errors"])
        self.assertFalse((outside / "post-commit").exists())

    def test_devin_merge_preserves_mixed_groups(self):
        path = self.repo / ".devin" / "hooks.v1.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "Custom": [{"matcher": "x", "hooks": [
                {"type": "command", "command": "echo keep"}]}],
            "Stop": [{"matcher": "", "hooks": [
                {"type": "command", "command": "partial hook devin Stop"},
                {"type": "command", "command": "other --flag"}]}],
            "meta": {"version": 1},
        }))
        install_devin_hooks(self.grepo)
        data = json.loads(path.read_text())
        self.assertEqual(data["meta"], {"version": 1})
        self.assertEqual(len(data["Stop"]), 1)
        cmds = [h["command"] for h in data["Stop"][0]["hooks"]]
        self.assertIn("partial hook devin Stop", cmds)
        self.assertIn("other --flag", cmds)
        disable_hooks(self.grepo)
        data2 = json.loads(path.read_text())
        self.assertEqual(data2["Custom"], [{"matcher": "x", "hooks": [
            {"type": "command", "command": "echo keep"}]}])
        stop_cmds = [
            h["command"]
            for g in data2.get("Stop", [])
            for h in g.get("hooks", [])]
        self.assertEqual(stop_cmds, ["other --flag"])

    def test_invalid_hooks_json_fails_clean(self):
        path = self.repo / ".devin" / "hooks.v1.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        with self.assertRaises(ValueError):
            install_devin_hooks(self.grepo)
        self.assertEqual(path.read_text(), "{not json")

    def test_claude_settings_merge(self):
        path = self.repo / ".devin" / "partial" / "claude-settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "permissions": {"allow": ["x"]},
            "hooks": {"Stop": [{"matcher": "", "hooks": [
                {"type": "command", "command": "mine"}]}]},
        }))
        install_claude_hooks(self.grepo)
        data = json.loads(path.read_text())
        self.assertEqual(data["permissions"], {"allow": ["x"]})
        stop_hooks = [
            h["command"]
            for g in data["hooks"]["Stop"]
            for h in g["hooks"]]
        self.assertIn("mine", stop_hooks)
        self.assertIn("partial hook claude Stop", stop_hooks)
        disable_hooks(self.grepo)
        data2 = json.loads(path.read_text())
        self.assertEqual(data2["permissions"], {"allow": ["x"]})
        stop_hooks2 = [
            h["command"]
            for g in data2["hooks"]["Stop"]
            for h in g["hooks"]]
        self.assertEqual(stop_hooks2, ["mine"])

    def test_hook_command_executes_with_json_stdin(self):
        install_devin_hooks(self.grepo)
        path = self.repo / ".devin" / "hooks.v1.json"
        data = json.loads(path.read_text())
        cmd = data["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertEqual(cmd, "partial hook devin UserPromptSubmit")
        bindir = self.tmp / "bin"
        bindir.mkdir()
        cap = self.tmp / "cap.json"
        fake = bindir / "partial"
        fake.write_text(
            "#!/bin/sh\n"
            f"printf '%s' \"$*\" > {cap}.argv\n"
            f"cat > {cap}\n"
        )
        fake.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        payload = json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "session_id": "x", "prompt": "hi"})
        proc = subprocess.run(
            ["sh", "-c", cmd], input=payload, env=env,
            capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(
            Path(str(cap) + ".argv").read_text(),
            "hook devin UserPromptSubmit")
        self.assertEqual(json.loads(cap.read_text())["prompt"], "hi")


class SyncTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.bare = self.tmp / "remote.git"
        git(self.tmp, "init", "--bare", str(self.bare))
        git(self.repo, "remote", "add", "origin", str(self.bare))
        self.store = Store(self.home / "partial.db")
        self.repo_row = self.store.register_repo(self.repo)
        self.rid = self.repo_row["id"]
        self.grepo = discover_repo(self.repo)
        self.wt = str(Path(self.repo).resolve())
        self._n = 0

    def _make_checkpoint(self, events=None):
        self._n += 1
        sid = scoped_session_id(self.rid, "devin", "s1")
        evs = events or [prompt_event(f"e{self._n}", "s1", "x")]
        self.store.ingest(self.rid, evs, worktree=self.wt)
        self.add_commit(f"f{self._n}.py", f"f{self._n} = 1\n")
        cp = create_checkpoint(
            self.store, self.rid, session_ids=[sid], worktree=self.wt)
        persist_checkpoint(
            self.grepo, self.store.checkpoint_bundle(cp["id"]), cp["id"])
        return cp

    def test_push_pull_roundtrip_transcript(self):
        evs = [
            prompt_event("p1", "s1", "the question"),
            Event(id="t1", session_id="s1", agent="devin", kind="tool",
                  timestamp=TS, tool_name="edit_file",
                  data={"tool_input": {"file_path": "x"}}),
            Event(id="r1", session_id="s1", agent="devin", kind="response",
                  timestamp=TS, text="the answer"),
        ]
        cp = self._make_checkpoint(evs)
        result = sync_checkpoints(self.store, self.grepo, push=True)
        self.assertTrue(result["pushed"], result)
        refs = git(self.bare, "for-each-ref", "--format=%(refname)").stdout
        self.assertIn("partial/checkpoints/v1", refs)
        self.assertNotIn("refs/heads/main", refs)

        other = init_repo(self.tmp / "clone")
        git(other, "remote", "add", "origin", str(self.bare))
        self.add_commit_in(other, "z.py", "z = 9\n")
        orepo = discover_repo(other)
        store2 = Store(self.tmp / "home2" / "partial.db")
        store2.register_repo(other)
        res2 = sync_checkpoints(store2, orepo, pull=True)
        self.assertEqual(res2["pulled"], 1, res2)
        self.assertFalse(res2["diverged"])
        got = store2.get_checkpoint(cp["id"])
        self.assertIsNotNone(got)
        self.assertEqual(got["commit_sha"], cp["commit_sha"])
        self.assertEqual(
            got["links"],
            [{"session_id": scoped_session_id(self.rid, "devin", "s1"),
              "method": "explicit"}])
        sess = store2.get_session(
            scoped_session_id(self.rid, "devin", "s1"))
        kinds = [e["kind"] for e in sess["events"]]
        self.assertEqual(kinds, ["prompt", "tool", "response"])
        self.assertEqual(sess["events"][2]["text"], "the answer")
        self.assertEqual(sess["events"][1]["tool_name"], "edit_file")

    def add_commit_in(self, repo, rel, content):
        p = Path(repo) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        git(repo, "add", "--", rel)
        git(repo, "-c", "user.name=T", "-c", "user.email=t@e",
            "commit", "-m", "c")

    def test_pull_divergence_is_explicit(self):
        self._make_checkpoint()
        sync_checkpoints(self.store, self.grepo, push=True)
        git(self.repo, "update-ref", "-d", METADATA_REF)
        self._make_checkpoint()
        local_ref = git(
            self.repo, "rev-parse", METADATA_REF).stdout.strip()
        res = sync_checkpoints(self.store, self.grepo, pull=True)
        self.assertTrue(res["diverged"])
        self.assertIsNotNone(res["error"])
        new_ref = git(self.repo, "rev-parse", METADATA_REF).stdout.strip()
        self.assertEqual(new_ref, local_ref)


if __name__ == "__main__":
    unittest.main()
