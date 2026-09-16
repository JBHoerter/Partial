from __future__ import annotations

import json
import unittest
from pathlib import Path

from helpers import RepoTestCase, commit, git

from partial.git import create_checkpoint
from partial.models import Event, now_iso, scoped_session_id
from partial.provenance import Provenance
from partial.store import Store


class ProvenanceCase(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.home / "partial.db")
        row = self.store.register_repo(str(self.repo))
        self.repo_id = row["id"]
        self.rd = {
            "id": self.repo_id, "root": str(self.repo),
            "common_dir": str(self.repo / ".git"), "remote": "",
        }
        self.prov = Provenance(self.store)

    def _session(self, agent="devin", native="s1"):
        self.store.ingest(self.repo_id, [Event(
            id=f"start-{agent}-{native}", session_id=native,
            agent=agent, kind="session_start", timestamp=now_iso(),
        )], worktree=str(self.repo))
        return scoped_session_id(self.repo_id, agent, native)

    def _payload(self, rel, key="call1", session="s1", success=True):
        return {
            "session_id": session, "tool_name": "edit",
            "tool_input": {"file_path": rel},
            "tool_use_id": key,
            "tool_response": {"success": success},
        }

    def _tool(self, rel, key="call1", session="s1", agent="devin",
              success=True):
        p = self._payload(rel, key=key, session=session,
                          success=success)
        self.prov.before_tool(self.rd, agent, p)
        return p

    def _commit_cp(self, session_ids=None):
        git(self.repo, "add", "-A")
        commit(self.repo)
        return self._cp_head(session_ids)

    def _cp_head(self, session_ids=None):
        cp = create_checkpoint(
            self.store, self.repo_id, session_ids=session_ids,
            worktree=str(self.repo))
        return cp, self.store.get_attribution(cp["id"])

    def _lines(self, report, path):
        for f in report["files"]:
            if f["path"] == path:
                return f["lines"]
        return []

    def test_agent_edit_tool_pair(self):
        self._session()
        self.add_commit("app.py", "a = 1\nb = 2\n", "base")
        p = self._tool("app.py")
        self.write_file("app.py", "a = 1\nb = 2\nc = 3\n")
        self.prov.after_tool(self.rd, "devin", p)
        self.sid = scoped_session_id(self.repo_id, "devin", "s1")
        cp, rep = self._commit_cp([self.sid])
        s = rep["summary"]
        self.assertEqual(s["agent_added"], 1)
        self.assertEqual(s["human_added"], 0)
        self.assertEqual(s["unknown_added"], 0)
        self.assertEqual(s["agent_percentage"], 100.0)
        line = [l for l in self._lines(rep, "app.py")
                if l["side"] == "new"][0]
        self.assertEqual(line["kind"], "agent")
        self.assertEqual(line["session_id"], self.sid)
        self.assertEqual(line["evidence"], "tool-pair")

    def test_preexisting_lines_unknown(self):
        self._session()
        self.write_file("app.py", "a = 1\n")
        cp, rep = self._commit_cp()
        lines = self._lines(rep, "app.py")
        self.assertTrue(lines)
        self.assertTrue(all(l["kind"] == "unknown" for l in lines))
        self.assertEqual(rep["summary"]["unknown_added"], 1)

    def test_mixed_agent_human_50(self):
        self._session()
        self.add_commit("app.py", "one\ntwo\n", "base")
        p = self._tool("app.py")
        self.write_file("app.py", "one\ntwo\nagent-line\n")
        self.prov.after_tool(self.rd, "devin", p)
        self.write_file("app.py", "one\ntwo\nagent-line\nhuman-line\n")
        cp, rep = self._commit_cp()
        s = rep["summary"]
        self.assertEqual(s["agent_added"], 1)
        self.assertEqual(s["human_added"], 1)
        self.assertEqual(s["agent_percentage"], 50.0)
        self.assertEqual(s["coverage_percentage"], 100.0)

    def test_revert_preserves_ownership(self):
        self._session()
        self.add_commit("app.py", "a\nb\nc\n", "base")
        p = self._tool("app.py")
        self.write_file("app.py", "a\nb\nc\nagent-line\n")
        self.prov.after_tool(self.rd, "devin", p)
        self.write_file("app.py", "a\nb\nc\n")
        self.write_file("other.py", "z = 1\n")
        cp, rep = self._commit_cp()
        self.assertEqual(self._lines(rep, "app.py"), [])
        self.assertEqual(rep["summary"]["agent_added"], 0)
        self.assertEqual(rep["summary"]["agent_removed"], 0)

    def test_missing_pre_snapshot_unknown(self):
        self._session()
        self.add_commit("app.py", "a\n", "base")
        self.write_file("app.py", "a\nx\n")
        self.prov.after_tool(
            self.rd, "devin", self._payload("app.py"))
        cp, rep = self._commit_cp()
        lines = self._lines(rep, "app.py")
        new = [l for l in lines if l["side"] == "new"]
        self.assertEqual(new[0]["kind"], "unknown")
        self.assertEqual(new[0]["evidence"], "unobserved")

    def test_duplicate_pending_unknown(self):
        self._session()
        self.add_commit("app.py", "a\n", "base")
        p = self._tool("app.py", key="k1")
        self.prov.before_tool(self.rd, "devin", p)
        self.write_file("app.py", "a\nx\n")
        self.prov.after_tool(self.rd, "devin", p)
        cp, rep = self._commit_cp()
        new = [l for l in self._lines(rep, "app.py")
               if l["side"] == "new"]
        self.assertEqual(new[0]["kind"], "unknown")
        self.assertEqual(new[0]["evidence"], "overlap")

    def test_failed_tool_discards_pending(self):
        self._session()
        self.add_commit("app.py", "a\n", "base")
        p = self._tool("app.py")
        self.write_file("app.py", "a\nx\n")
        self.prov.after_tool(self.rd, "devin",
                             self._payload("app.py", success=False))
        self.write_file("app.py", "a\nx\ny\n")
        cp, rep = self._commit_cp()
        new = [l for l in self._lines(rep, "app.py")
               if l["side"] == "new"]
        self.assertTrue(all(l["kind"] in ("human", "unknown")
                            for l in new))
        self.assertEqual(rep["summary"]["agent_added"], 0)

    def test_sensitive_file_excluded(self):
        self._session()
        self.write_file(".env", "API_KEY=secret\n")
        p = self._tool(".env")
        self.prov.after_tool(self.rd, "devin", p)
        git(self.repo, "add", "--", ".env")
        cp, rep = self._commit_cp()
        self.assertEqual(
            rep["excluded"],
            [{"path": ".env", "reason": "sensitive-path"}])
        self.assertEqual(rep["files"], [])
        self.assertIsNone(rep["summary"]["agent_percentage"])

    def test_binary_file_excluded(self):
        self.add_commit("ok.py", "a = 1\n", "base")
        (self.repo / "bin.dat").write_bytes(b"\x00\x01\x02\x00raw")
        git(self.repo, "add", "--", "bin.dat")
        cp, rep = self._commit_cp()
        self.assertEqual(rep["files"], [])
        self.assertEqual(
            rep["excluded"],
            [{"path": "bin.dat", "reason": "binary-or-oversize"}])

    def test_oversize_file_excluded(self):
        (self.repo / "big.py").write_text("x = 1\n" * 200000)
        git(self.repo, "add", "--", "big.py")
        cp, rep = self._commit_cp()
        self.assertEqual(rep["files"], [])
        self.assertEqual(rep["excluded"][0]["reason"],
                         "binary-or-oversize")

    def test_outside_worktree_rejected(self):
        self._session()
        p = self._payload("/etc/hostname", session="s1")
        self.prov.before_tool(self.rd, "devin", p)
        self.prov.after_tool(self.rd, "devin", p)
        p2 = self._payload("../escape.py", session="s1")
        self.prov.before_tool(self.rd, "devin", p2)
        self.prov.after_tool(self.rd, "devin", p2)
        conn = self.store._connect()
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM attribution_pending").fetchone()[0]
            m = conn.execute(
                "SELECT COUNT(*) FROM attribution_files").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0)
        self.assertEqual(m, 0)

    def test_symlink_rejected(self):
        self._session()
        self.add_commit("real.py", "a = 1\n", "base")
        (self.repo / "link.py").symlink_to("real.py")
        p = self._payload("link.py", session="s1")
        self.prov.before_tool(self.rd, "devin", p)
        self.prov.after_tool(self.rd, "devin", p)
        conn = self.store._connect()
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM attribution_pending").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0)

    def test_partial_staging_uses_committed_snapshot(self):
        self._session()
        self.add_commit("app.py", "1\n2\n3\n4\n", "base")
        p = self._tool("app.py")
        self.write_file("app.py", "1\nA\n3\nB\n")
        self.prov.after_tool(self.rd, "devin", p)
        staged = "1\nA\n3\n4\n"
        import subprocess
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "hash-object", "-w",
             "--stdin"], input=staged.encode(), capture_output=True)
        sha = proc.stdout.decode().strip()
        git(self.repo, "update-index", "--cacheinfo",
            f"100644,{sha},app.py")
        commit(self.repo, "stage first hunk only")
        cp = create_checkpoint(
            self.store, self.repo_id, worktree=str(self.repo))
        rep = self.store.get_attribution(cp["id"])
        lines = self._lines(rep, "app.py")
        new = [l for l in lines if l["side"] == "new"]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["kind"], "agent")
        self.assertEqual(rep["summary"]["agent_percentage"], 100.0)
        self.assertEqual(rep["summary"]["total_changed"], 2)

    def test_two_agents_weighted(self):
        self._session("devin", "s1")
        self._session("claude", "c1")
        self.add_commit("app.py", "x\n", "base")
        p1 = self._tool("app.py", key="k1", session="s1", agent="devin")
        self.write_file("app.py", "x\ndevin-line\n")
        self.prov.after_tool(self.rd, "devin", p1)
        p2 = self._tool("app.py", key="k2", session="c1",
                        agent="claude")
        self.write_file("app.py", "x\ndevin-line\nclaude-1\nclaude-2\n")
        self.prov.after_tool(self.rd, "claude", p2)
        cp, rep = self._commit_cp()
        s = rep["summary"]
        self.assertEqual(s["agent_added"], 3)
        self.assertEqual(s["agent_percentage"], 100.0)
        sids = {l["session_id"] for l in self._lines(rep, "app.py")
                if l["side"] == "new"}
        self.assertEqual(sids, {
            scoped_session_id(self.repo_id, "devin", "s1"),
            scoped_session_id(self.repo_id, "claude", "c1")})

    def test_worktrees_isolated(self):
        self._session()
        self.add_commit("app.py", "x\n", "base")
        wt2 = self.tmp / "wt2"
        git(self.repo, "worktree", "add", str(wt2), "HEAD")
        rd2 = {**self.rd, "root": str(wt2)}
        p = self._payload("app.py", session="s1")
        self.prov.before_tool(rd2, "devin", p)
        (wt2 / "app.py").write_text("x\nwt2-line\n")
        self.prov.after_tool(rd2, "devin", p)
        q = self._payload("app.py", key="k2", session="s1")
        self.prov.before_tool(self.rd, "devin", q)
        self.write_file("app.py", "x\nmain-line\n")
        self.prov.after_tool(self.rd, "devin", q)
        cp, rep = self._commit_cp()
        new = [l for l in self._lines(rep, "app.py")
               if l["side"] == "new"]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["kind"], "agent")
        self.assertEqual(rep["summary"]["agent_added"], 1)

    def test_incremental_commit_carryforward(self):
        self._session()
        self.add_commit("app.py", "a\n", "base")
        p = self._tool("app.py", key="k1")
        self.write_file("app.py", "a\nfirst\n")
        self.prov.after_tool(self.rd, "devin", p)
        cp1, rep1 = self._commit_cp()
        self.assertEqual(rep1["summary"]["agent_added"], 1)
        p2 = self._tool("app.py", key="k2")
        self.write_file("app.py", "a\nfirst\nsecond\n")
        self.prov.after_tool(self.rd, "devin", p2)
        cp2, rep2 = self._commit_cp()
        s = rep2["summary"]
        self.assertEqual(s["agent_added"], 1)
        self.assertEqual(s["agent_percentage"], 100.0)
        self.assertEqual(s["unknown_added"], 0)

    def test_bundle_roundtrip(self):
        self._session()
        self.add_commit("app.py", "a\n", "base")
        p = self._tool("app.py")
        self.write_file("app.py", "a\nx\n")
        self.prov.after_tool(self.rd, "devin", p)
        cp, rep = self._commit_cp()
        bundle = self.store.export_bundle()
        store2 = Store(self.tmp / "other" / "partial.db")
        store2.import_bundle(bundle)
        rep2 = store2.get_attribution(cp["id"])
        self.assertIsNotNone(rep2)
        self.assertEqual(rep2["summary"], rep["summary"])
        self.assertEqual(rep2["files"], rep["files"])
        self.assertEqual(rep2["capture_source"], "imported-claim")
        self.assertEqual(rep["capture_source"], "local-observation")

    def test_forged_summary_recomputed(self):
        self._session()
        self.add_commit("app.py", "a\n", "base")
        cp, rep = self._cp_head()
        bundle = self.store.export_bundle()
        forged = dict(bundle["attribution"][0])
        rep_forged = dict(forged["report"])
        rep_forged["summary"] = {
            "agent_added": 99, "agent_removed": 0,
            "human_added": 0, "human_removed": 0,
            "unknown_added": 0, "unknown_removed": 0,
            "total_changed": 99, "agent_percentage": 100.0,
            "coverage_percentage": 100.0}
        forged["report"] = rep_forged
        bundle["attribution"] = [forged]
        store2 = Store(self.tmp / "other" / "partial.db")
        store2.import_bundle(bundle)
        rep2 = store2.get_attribution(cp["id"])
        self.assertEqual(rep2["summary"], rep["summary"])
        self.assertNotEqual(rep2["summary"]["agent_added"], 99)

    def test_foreign_session_rejected(self):
        self._session()
        self.add_commit("app.py", "a\n", "base")
        cp, rep = self._cp_head()
        bundle = self.store.export_bundle()
        bad = json.loads(json.dumps(bundle["attribution"][0]))
        for f in bad["report"]["files"]:
            for l in f["lines"]:
                l["kind"] = "agent"
                l["session_id"] = "0" * 64
                l["evidence"] = "tool-pair"
        bundle["attribution"] = [bad]
        store2 = Store(self.tmp / "other" / "partial.db")
        with self.assertRaises(ValueError):
            store2.import_bundle(bundle)
        self.assertIsNone(store2.get_checkpoint(cp["id"]))
        self.assertIsNone(store2.get_attribution(cp["id"]))

    def test_failed_tool_unknown_then_human(self):
        self._session()
        self.add_commit("app.py", "a\n", "base")
        self._tool("app.py")
        self.write_file("app.py", "a\nx\n")
        self.prov.after_tool(
            self.rd, "devin", self._payload("app.py", success=False))
        self.write_file("app.py", "a\nx\ny\n")
        cp, rep = self._commit_cp()
        new = [l for l in self._lines(rep, "app.py")
               if l["side"] == "new"]
        self.assertEqual(len(new), 2)
        self.assertEqual(new[0]["kind"], "unknown")
        self.assertEqual(new[0]["evidence"], "unobserved")
        self.assertEqual(new[1]["kind"], "human")
        self.assertEqual(new[1]["evidence"], "external")
        self.assertEqual(rep["summary"]["agent_added"], 0)

    def test_before_overlap_with_active_pending(self):
        self._session()
        self.add_commit("app.py", "a\n", "base")
        self._tool("app.py", key="k1")
        self.write_file("app.py", "a\next\n")
        p2 = self._tool("app.py", key="k2")
        self.write_file("app.py", "a\next\nk2line\n")
        self.prov.after_tool(self.rd, "devin", p2)
        cp, rep = self._commit_cp()
        new = [l for l in self._lines(rep, "app.py")
               if l["side"] == "new"]
        self.assertEqual(len(new), 2)
        self.assertEqual(new[0]["kind"], "unknown")
        self.assertEqual(new[0]["evidence"], "overlap")
        self.assertEqual(new[1]["kind"], "agent")
        self.assertEqual(new[1]["evidence"], "tool-pair")

    def test_historical_checkpoint_all_unknown(self):
        self._session()
        sha_a = self.add_commit("app.py", "a\nb\n", "first")
        p = self._tool("app.py")
        self.write_file("app.py", "a\nb\nagent\n")
        self.prov.after_tool(self.rd, "devin", p)
        git(self.repo, "add", "-A")
        commit(self.repo)
        cp_old = create_checkpoint(
            self.store, self.repo_id, commit=sha_a,
            worktree=str(self.repo))
        rep_old = self.store.get_attribution(cp_old["id"])
        s = rep_old["summary"]
        self.assertEqual(s["agent_added"], 0)
        self.assertEqual(s["human_added"], 0)
        self.assertEqual(s["unknown_added"], 2)
        self.assertEqual(s["total_changed"], 2)

    def test_foreign_filepath_rejected(self):
        self._session()
        self.add_commit("app.py", "a\n", "base")
        cp, rep = self._cp_head()
        bundle = self.store.export_bundle()
        bad = json.loads(json.dumps(bundle["attribution"][0]))
        for f in bad["report"]["files"]:
            f["path"] = "elsewhere.py"
        bundle["attribution"] = [bad]
        store2 = Store(self.tmp / "other" / "partial.db")
        with self.assertRaises(ValueError):
            store2.import_bundle(bundle)
        self.assertIsNone(store2.get_checkpoint(cp["id"]))

    def test_malformed_line_body_rejected(self):
        self._session()
        self.add_commit("app.py", "a\n", "base")
        cp, rep = self._cp_head()
        bundle = self.store.export_bundle()
        bad = json.loads(json.dumps(bundle["attribution"][0]))
        bad["report"]["files"][0]["lines"] = ["oops", 42]
        bundle["attribution"] = [bad]
        store2 = Store(self.tmp / "other" / "partial.db")
        with self.assertRaises(ValueError):
            store2.import_bundle(bundle)
        self.assertIsNone(store2.get_checkpoint(cp["id"]))
        self.assertIsNone(store2.get_attribution(cp["id"]))

    def test_foreign_excluded_path_rejected(self):
        self._session()
        self.add_commit("app.py", "a\n", "base")
        cp, rep = self._cp_head()
        bundle = self.store.export_bundle()
        bad = json.loads(json.dumps(bundle["attribution"][0]))
        bad["report"]["excluded"] = [
            {"path": "../elsewhere.py", "reason": "x"},
            {"path": "notincp.py", "reason": "x"}]
        bundle["attribution"] = [bad]
        store2 = Store(self.tmp / "other" / "partial.db")
        with self.assertRaises(ValueError):
            store2.import_bundle(bundle)
        self.assertIsNone(store2.get_attribution(cp["id"]))

    def test_missing_report_marked_excluded(self):
        self._session()
        self.add_commit("app.py", "a\n", "base")
        self.write_file("b.py", "b\n")
        git(self.repo, "add", "--", "b.py")
        commit(self.repo)
        cp = create_checkpoint(
            self.store, self.repo_id, worktree=str(self.repo))
        rep = self.store.get_attribution(cp["id"])
        bundle = self.store.export_bundle()
        att = json.loads(json.dumps(bundle["attribution"][0]))
        att["report"]["files"] = [
            f for f in att["report"]["files"] if f["path"] != "b.py"]
        att["report"]["summary"] = {"agent_added": 999}
        bundle["attribution"] = [att]
        store2 = Store(self.tmp / "other" / "partial.db")
        store2.import_bundle(bundle)
        rep2 = store2.get_attribution(cp["id"])
        self.assertEqual(rep2["summary"]["total_changed"], 0)
        self.assertEqual(rep2["summary"]["unknown_added"], 0)
        excl = {x["path"]: x["reason"] for x in rep2["excluded"]}
        self.assertEqual(excl.get("b.py"), "missing-report")
        self.assertEqual(rep2["capture_source"], "imported-claim")


if __name__ == "__main__":
    unittest.main()
