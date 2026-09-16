import json
import unittest
from unittest import mock

from partial.models import Event, scoped_session_id
from partial.memory import Memory, document_id
from partial.store import Store

from helpers import RepoTestCase, commit, git, init_repo

TS = "2026-01-01T00:00:00Z"


def ev(id_, sid, kind, ts=TS, text="", agent="devin", **kw):
    return Event(id=id_, session_id=sid, agent=agent, kind=kind,
                 timestamp=ts, text=text, **kw)


class MemoryTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.home / "partial.db")
        self.repo_row = self.store.register_repo(self.repo)
        self.rid = self.repo_row["id"]
        self.mem = Memory(self.store)
        self.sha = self.add_commit(
            "f.py", "def helper():\n    return 1\n\n"
                    "def main():\n    helper()\n    return helper()\n",
            "add f.py")

    def _session(self, sid="s1", agent="devin"):
        self.store.ingest(self.rid, [
            ev("e1", sid, "session_start"),
            ev("e2", sid, "prompt", text="implement helper function"),
            ev("e3", sid, "tool", tool_name="edit",
               data={"tool_input": {"file_path": "f.py"},
                     "tool_response": {"success": True}}),
            ev("e4", sid, "response", text="implemented helper"),
            ev("e5", sid, "usage",
               data={"usage": {"input_tokens": 10},
                     "usage_scope": "delta"}),
            ev("e6", sid, "session_end"),
        ], worktree=str(self.repo), branch="main")

    def test_index_code_and_history(self):
        self._session()
        self.store.save_checkpoint(
            self.rid, "a" * 32, self.sha, branch="main",
            message="add f.py", author="t", files=["f.py"],
            diff="+def helper", links=[], worktree=None,
            created_at=TS)
        out = self.mem.index(
            {"id": self.rid, "root": str(self.repo)})
        self.assertTrue(out["indexed_repositories"])
        rows = self.mem.search("helper")
        kinds = {r["kind"] for r in rows}
        self.assertIn("code", kinds)
        self.assertIn("session", kinds)
        self.assertIn("checkpoint", kinds)
        code = [r for r in rows if r["kind"] == "code"][0]
        self.assertEqual(code["path"], "f.py")
        self.assertEqual(code["commit_sha"], self.sha)
        self.assertNotIn("embedding", code)

    def test_index_idempotent_and_reindex_delete(self):
        out1 = self.mem.index({"id": self.rid, "root": str(self.repo)})
        ids1 = {d["id"] for d in
                self.mem.search("helper", kind="code", limit=50)}
        out2 = self.mem.index({"id": self.rid, "root": str(self.repo)})
        ids2 = {d["id"] for d in
                self.mem.search("helper", kind="code", limit=50)}
        self.assertEqual(ids1, ids2)
        conn = self.store._connect()
        n1 = conn.execute(
            "SELECT COUNT(*) c FROM memory_documents WHERE repo_id=?",
            (self.rid,)).fetchone()["c"]
        conn.close()
        self.assertEqual(n1, len(ids1) or n1)
        git(self.repo, "rm", "-q", "f.py")
        commit(self.repo, "remove f.py")
        self.mem.index({"id": self.rid, "root": str(self.repo)})
        self.assertEqual(
            self.mem.search("helper", kind="code", limit=50), [])

    def test_fts_literal_punctuation(self):
        self._session()
        self.mem.index(None)
        rows = self.mem.search('implement "helper" function!')
        self.assertTrue(rows)

    def test_session_doc_scope_full(self):
        self._session()
        self.mem.index(None)
        docs = {}
        conn = self.store._connect()
        for r in conn.execute(
                "SELECT source_id,kind,text FROM memory_documents"
                " WHERE repo_id=? AND kind='session'",
                (self.rid,)).fetchall():
            docs[r["source_id"]] = r
        conn.close()
        sid = scoped_session_id(self.rid, "devin", "s1")
        self.assertIn(f"{sid}:e2", docs)   # prompt
        self.assertIn(f"{sid}:e3", docs)   # tool
        self.assertIn(f"{sid}:e4", docs)   # response
        self.assertNotIn(f"{sid}:e5", docs)  # usage excluded
        self.assertNotIn(f"{sid}:e6", docs)  # session_end excluded
        self.assertIn("agent:devin", docs[f"{sid}:e2"]["text"])

    def test_sensitive_and_symlink_excluded(self):
        self.write_file(".env", "SECRET=1")
        self.write_file("node_modules/pkg/x.js", "const x = 1;\n")
        self.write_file("ok.py", "x = 1\n")
        git(self.repo, "add", "-A")
        commit(self.repo, "more")
        out = self.mem.index({"id": self.rid, "root": str(self.repo)})
        reasons = {s["path"]: s["reason"] for s in out["skipped"]}
        self.assertIn(".env", reasons)
        self.assertIn("node_modules/pkg/x.js", reasons)
        rows = self.mem.search("SECRET", kind="code")
        self.assertEqual(rows, [])

    def test_python_ast_symbols_and_calls(self):
        self.mem.index({"id": self.rid, "root": str(self.repo)})
        syms = self.mem.graph_search("helper", repo_id=self.rid)
        self.assertTrue(any(s["qualified_name"] == "helper"
                            and s["analysis"] == "ast"
                            for s in syms))
        main = [s for s in self.mem.graph_search(
            "main", repo_id=self.rid)
                if s["qualified_name"] == "main"][0]
        nb = self.mem.graph_neighbors(main["id"], repo_id=self.rid)
        self.assertEqual(nb["analysis"], "ast")
        callees = [e for e in nb["edges"] if e["kind"] == "calls"]
        self.assertTrue(callees)
        helper = [s for s in syms if s["qualified_name"] == "helper"][0]
        imp = self.mem.graph_impact(helper["id"], repo_id=self.rid)
        self.assertTrue(any(n["qualified_name"] == "main"
                            for n in imp["impacted"]))

    def test_lexical_language_inventory(self):
        self.add_commit("g.go", "package main\n\nfunc Serve() {}\n",
                        "go file")
        self.mem.index({"id": self.rid, "root": str(self.repo)})
        syms = self.mem.graph_search("Serve", repo_id=self.rid)
        self.assertTrue(syms)
        self.assertEqual(syms[0]["analysis"], "lexical")
        nb = self.mem.graph_neighbors(
            syms[0]["id"], repo_id=self.rid)
        self.assertTrue(any("lexical" in l
                            for l in nb["limitations"]))

    def test_graph_search_escapes_like_wildcards(self):
        self.mem.index({"id": self.rid, "root": str(self.repo)})
        # '%' and '_' are literal characters, not LIKE wildcards.
        self.assertEqual(
            self.mem.graph_search("%", repo_id=self.rid), [])
        self.assertEqual(
            self.mem.graph_search("_", repo_id=self.rid), [])
        self.add_commit("u.py",
                        "def my_func():\n    return 1\n\n"
                        "def myXfunc():\n    return 2\n",
                        "add u.py")
        self.mem.index({"id": self.rid, "root": str(self.repo)})
        hits = {s["qualified_name"] for s in
                self.mem.graph_search("my_func", repo_id=self.rid)}
        self.assertIn("my_func", hits)
        self.assertNotIn("myXfunc", hits)

    def test_search_filters_and_document(self):
        self._session()
        self.mem.index({"id": self.rid, "root": str(self.repo)})
        rows = self.mem.search("helper", kind="session")
        self.assertTrue(all(r["kind"] == "session" for r in rows))
        doc = self.mem.document(rows[0]["id"])
        self.assertEqual(doc["id"], rows[0]["id"])
        self.assertIsNone(self.mem.document("0" * 64))
        with self.assertRaises(ValueError):
            self.mem.search("", limit=5)

    def test_context_shape(self):
        self._session()
        self.mem.index(None)
        ctx = self.mem.context("helper")
        self.assertEqual(ctx["mode"], "lexical")
        self.assertIn("documents", ctx)
        self.assertEqual(ctx["indexed_repositories"], [])
        self.assertTrue(ctx["limitations"])
        self.mem.index({"id": self.rid, "root": str(self.repo)})
        ctx = self.mem.context("helper")
        self.assertTrue(ctx["indexed_repositories"])

    def test_no_api_calls_by_default(self):
        self._session()
        with mock.patch(
                "partial.ai.OpenAIProvider._post") as m:
            self.mem.index({"id": self.rid, "root": str(self.repo)})
            self.mem.search("helper")
            self.mem.context("helper")
            m.assert_not_called()

    def test_semantic_index_and_search_mock(self):
        self._session()
        vecs = []

        def fake_embed(self, texts):
            out = []
            for t in texts:
                v = [1.0 if "helper" in t else 0.0, 0.5]
                vecs.append(v)
                out.append(v)
            return out

        with mock.patch.object(
                __import__("partial.ai", fromlist=["x"])
                .OpenAIProvider, "configured",
                new_callable=lambda: property(lambda s: True)), \
                mock.patch.object(
                    __import__("partial.ai", fromlist=["x"])
                    .OpenAIProvider, "embed", fake_embed):
            out = self.mem.index(
                {"id": self.rid, "root": str(self.repo)},
                semantic=True)
            self.assertTrue(out["semantic"])
            rows = self.mem.search("helper", semantic=True)
            self.assertTrue(rows)
            self.assertEqual(rows[0].get("retrieval_method"),
                             "fts5-cosine-rrf-v1")

    def test_dimension_mismatch_rejected(self):
        from partial.ai import OpenAIProvider
        p = OpenAIProvider(api_key="k", base_url="http://127.0.0.1:9")
        with mock.patch.object(OpenAIProvider, "_post",
                               return_value={"data": [
                                   {"index": 0, "embedding": []}]}):
            with self.assertRaises(ValueError):
                p.embed(["x"])
        with mock.patch.object(OpenAIProvider, "_post",
                               return_value={"data": [
                                   {"index": 0,
                                    "embedding": [1.0, float("nan")]}
                               ]}):
            with self.assertRaises(ValueError):
                p.embed(["x"])

    def test_false_citations_rejected(self):
        from partial.brain_contract import validate_answer
        with self.assertRaises(ValueError):
            validate_answer(
                {"answer": "x", "citations": ["not-a-doc"],
                 "uncertainties": []},
                [{"id": "real"}])

    def test_decisions(self):
        self._session()
        self.mem.index(None)
        doc = self.mem.search("helper")[0]
        d = self.mem.add_decision(
            self.rid, "use helper", "we chose helper()",
            [doc["id"]], author="tester")
        self.assertEqual(d["status"], "active")
        d2 = self.mem.add_decision(
            self.rid, "use helper v2", "revised", [doc["id"]],
            author="tester", supersedes=d["id"])
        rows = {r["id"]: r for r in self.mem.decisions(self.rid)}
        self.assertEqual(rows[d["id"]]["status"], "superseded")
        self.assertEqual(rows[d2["id"]]["status"], "active")
        hits = self.mem.search("helper v2", kind="decision")
        self.assertTrue(hits)

    def test_decision_source_validation_atomic(self):
        self._session()
        self.mem.index(None)
        conn = self.store._connect()
        conn.execute(
            "INSERT INTO repositories(id,name,created_at)"
            " VALUES(?,?,?)", ("f" * 64, "x", TS))
        conn.execute(
            "INSERT INTO memory_documents(id,repo_id,kind,source_id,"
            "title,text,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("b" * 64, "f" * 64, "session", "x", "t", "body", TS))
        conn.commit()
        conn.close()
        with self.assertRaises(ValueError):
            self.mem.add_decision(
                self.rid, "bad", "body", ["b" * 64], author="t")
        self.assertEqual(self.mem.decisions(self.rid), [])

    def test_dispatch_window(self):
        self.store.save_checkpoint(
            self.rid, "1" * 32, "a" * 40, branch="main",
            message="c1", author="t", files=["f.py"], diff=None,
            links=[], worktree=None,
            created_at="2026-01-01T00:00:00+00:00")
        self.store.save_checkpoint(
            self.rid, "2" * 32, "b" * 40, branch="dev",
            message="c2", author="t", files=["f.py"], diff=None,
            links=[], worktree=None,
            created_at="2026-02-01T00:00:00+00:00")
        out = self.mem.dispatch(
            repo_id=self.rid, since="2026-01-15",
            until="2026-02-15")
        self.assertEqual(len(out["source_ids"]), 1)
        self.assertIn("c2", out["markdown"])
        self.assertNotIn("c1", out["markdown"])
        out = self.mem.dispatch(repo_id=self.rid, branch="main",
                                since="2025-01-01")
        self.assertIn("c1", out["markdown"])
        self.assertNotIn("c2", out["markdown"])
        # scope echoes the requested filter, not a checkpoint row's
        # branch (loop variable must not shadow the parameter)
        self.assertEqual(out["scope"]["branch"], "main")
        out = self.mem.dispatch(repo_id=self.rid, since="2025-01-01")
        self.assertEqual(len(out["source_ids"]), 2)
        self.assertIsNone(out["scope"]["branch"])

    def test_bundle_memory_roundtrip(self):
        self._session()
        self.mem.index({"id": self.rid, "root": str(self.repo)})
        doc = self.mem.search("helper")[0]
        self.mem.add_decision(
            self.rid, "decision one", "body", [doc["id"]],
            author="t")
        bundle = self.store.export_bundle(repo_id=self.rid)
        self.assertIn("memory", bundle)
        self.assertTrue(bundle["memory"]["documents"])
        fresh = Store(self.tmp / "fresh.db")
        out = fresh.import_bundle(bundle)
        mem2 = Memory(fresh)
        rows = mem2.search("helper")
        self.assertTrue(rows)
        self.assertTrue(mem2.decisions(self.rid))
        syms = mem2.graph_search("helper", repo_id=self.rid)
        self.assertTrue(syms)

    def test_bundle_memory_invalid_rolls_back(self):
        self._session()
        self.mem.index(None)
        bundle = self.store.export_bundle(repo_id=self.rid)
        doc = dict(bundle["memory"]["documents"][0])
        doc["id"] = "e" * 64  # no longer matches content hash
        bundle["memory"]["documents"] = [doc]
        fresh = Store(self.tmp / "fresh.db")
        with self.assertRaises(ValueError):
            fresh.import_bundle(bundle)
        conn = fresh._connect()
        n = conn.execute(
            "SELECT COUNT(*) FROM memory_documents").fetchone()[0]
        s = conn.execute(
            "SELECT COUNT(*) FROM sessions").fetchone()[0]
        conn.close()
        self.assertEqual(n, 0)
        self.assertEqual(s, 0)

    def test_experts(self):
        self._session()
        sid = scoped_session_id(self.rid, "devin", "s1")
        self.store.save_checkpoint(
            self.rid, "3" * 32, self.sha, branch="main",
            message="c", author="t", files=["f.py"], diff=None,
            links=[(sid, "observed-worktree-overlap")],
            worktree=None, created_at=TS)
        rows = self.mem.experts("f.py", repo_id=self.rid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], sid)
        self.assertEqual(rows[0]["method"], "linked-checkpoint-count")

    # ---- privacy / redaction ------------------------------------
    def test_index_redacts_secrets_and_strips_paths(self):
        secret = "sk-abcdef1234567890"
        self.store.ingest(self.rid, [
            ev("e1", "s1", "session_start"),
            ev("e2", "s1", "prompt",
               text=f"rotate the key {secret}"),
            ev("e3", "s1", "tool", tool_name="edit",
               data={"tool_input": {
                   "file_path": str(self.repo / "f.py"),
                   "api_key": secret},
                   "tool_response": {"success": True}}),
            ev("e4", "s1", "session_end"),
        ], worktree=str(self.repo), branch="main")
        self.store.save_checkpoint(
            self.rid, "b" * 32, self.sha, branch="main",
            message=f"ship {secret}", author="t", files=["f.py"],
            diff=f"+leak {secret}", links=[], worktree=None,
            created_at=TS)
        self.write_file("sec.py",
                        f"# leaked token {secret}\nx = 1\n")
        git(self.repo, "add", "sec.py")
        commit(self.repo, "add sec")
        self.mem.index({"id": self.rid, "root": str(self.repo)})
        conn = self.store._connect()
        rows = conn.execute(
            "SELECT title,text,path FROM memory_documents").fetchall()
        conn.close()
        self.assertTrue(rows)
        for r in rows:
            self.assertNotIn(secret, r["title"])
            self.assertNotIn(secret, r["text"])
            # absolute worktree roots stripped from stored text
            self.assertNotIn(str(self.repo), r["text"])
        self.assertEqual(self.mem.search("abcdef1234567890"), [])
        # sanitized documents survive bundle export/import validation
        fresh = Store(self.tmp / "fresh.db")
        fresh.import_bundle(
            self.store.export_bundle(repo_id=self.rid))
        self.assertTrue(Memory(fresh).search("rotate"))

    def test_dispatch_redacts_messages_and_titles(self):
        secret = "ghp_1234567890abcdefgh"
        sid = scoped_session_id(self.rid, "devin", "s1")
        self._session()
        conn = self.store._connect()
        conn.execute("UPDATE sessions SET title=? WHERE id=?",
                     (f"title {secret}", sid))
        conn.commit()
        conn.close()
        self.store.save_checkpoint(
            self.rid, "4" * 32, self.sha, branch="main",
            message=f"msg {secret}", author="t", files=["f.py"],
            diff=None, links=[(sid, "observed-worktree-overlap")],
            worktree=None, created_at=TS)
        out = self.mem.dispatch(since="2025-12-01",
                                until="2026-02-01")
        self.assertNotIn(secret, out["markdown"])
        self.assertIn("[REDACTED]", out["markdown"])

    # ---- retention ----------------------------------------------
    def test_history_reindex_preserves_code_graph_and_index(self):
        self.mem.index({"id": self.rid, "root": str(self.repo)})
        code_ids = {d["id"] for d in
                    self.mem.search("helper", kind="code", limit=50)}
        self.assertTrue(code_ids)
        self.assertTrue(
            self.mem.graph_search("helper", repo_id=self.rid))
        conn = self.store._connect()
        idx_before = dict(conn.execute(
            "SELECT * FROM repository_indexes WHERE repo_id=?",
            (self.rid,)).fetchone())
        conn.close()
        self.assertEqual(idx_before["commit_sha"], self.sha)
        self._session()
        # history-only reindex refreshes derived docs but must not
        # archive code docs, graph rows, or the code index row
        self.mem.index(None)
        self.assertEqual(
            code_ids,
            {d["id"] for d in
             self.mem.search("helper", kind="code", limit=50)})
        self.assertTrue(
            self.mem.graph_search("helper", repo_id=self.rid))
        conn = self.store._connect()
        idx_after = dict(conn.execute(
            "SELECT * FROM repository_indexes WHERE repo_id=?",
            (self.rid,)).fetchone())
        active_code = conn.execute(
            "SELECT COUNT(*) c FROM memory_documents WHERE repo_id=?"
            " AND kind='code' AND archived=0",
            (self.rid,)).fetchone()["c"]
        conn.close()
        self.assertEqual(idx_after, idx_before)
        self.assertEqual(active_code, len(code_ids))
        self.assertTrue(
            self.mem.search("implement helper", kind="session"))

    def test_code_reindex_archives_old_docs_citations_resolve(self):
        self.mem.index({"id": self.rid, "root": str(self.repo)})
        old_ids = sorted(d["id"] for d in
                         self.mem.search("helper", kind="code",
                                         limit=50))
        self.assertTrue(old_ids)
        # a decision citing the soon-to-be-archived code docs
        self.mem.add_decision(
            self.rid, "cite helper code", "see helper docs",
            old_ids, author="t")
        self.add_commit(
            "f.py",
            "def helper():\n    return 2\n\n"
            "def main():\n    return helper()\n",
            "update f.py")
        self.mem.index({"id": self.rid, "root": str(self.repo)})
        active = {d["id"] for d in
                  self.mem.search("helper", kind="code", limit=50)}
        self.assertTrue(active)
        self.assertFalse(set(old_ids) & active)
        # archived docs stay retrievable so citations never break
        for did in old_ids:
            doc = self.mem.document(did)
            self.assertIsNotNone(doc)
            self.assertEqual(doc["kind"], "code")

    def test_code_reindex_scoped_to_repo(self):
        repo2 = init_repo(self.tmp / "repo2")
        rid2 = self.store.register_repo(repo2)["id"]
        (repo2 / "g.py").write_text("def serve():\n    return 1\n")
        git(repo2, "add", "g.py")
        commit(repo2, "add g")
        self.mem.index({"id": self.rid, "root": str(self.repo)})
        self.mem.index({"id": rid2, "root": str(repo2)})
        self.assertTrue(self.mem.graph_search("serve", repo_id=rid2))
        git(self.repo, "rm", "-q", "f.py")
        commit(self.repo, "remove f.py")
        self.mem.index({"id": self.rid, "root": str(self.repo)})
        # repo1 graph replaced, repo2 graph and docs untouched
        self.assertEqual(
            self.mem.graph_search("helper", repo_id=self.rid), [])
        self.assertTrue(self.mem.graph_search("serve", repo_id=rid2))
        self.assertTrue(self.mem.search("serve", kind="code",
                                        repo_id=rid2))

    def test_seed_and_review_docs_survive_reindex(self):
        text = "seeded investigation evidence"
        did = document_id(self.rid, "session", "seed:notes.txt",
                          None, None, None, text)
        conn = self.store._connect()
        conn.execute(
            "INSERT INTO memory_documents(id,repo_id,kind,source_id,"
            "title,text,updated_at,archived)"
            " VALUES(?,?,'session',?,?,?,?,0)",
            (did, self.rid, "seed:notes.txt", "notes.txt", text, TS))
        if self.store.fts_ok:
            conn.execute(
                "INSERT INTO memory_fts(id,repo_id,kind,title,text)"
                " VALUES(?,?,'session',?,?)",
                (did, self.rid, "notes.txt", text))
        conn.commit()
        conn.close()
        self.mem.index(None)
        self.mem.index({"id": self.rid, "root": str(self.repo)})
        self.mem.index(None)
        hits = self.mem.search("seeded investigation")
        self.assertTrue(any(h["id"] == did for h in hits))

    # ---- decisions ------------------------------------------------
    def test_decision_supersession_updates_search_state(self):
        self._session()
        self.mem.index(None)
        doc = self.mem.search("helper")[0]
        d1 = self.mem.add_decision(
            self.rid, "pick helper approach", "v1 body",
            [doc["id"]], author="tester")
        old_doc = self.mem.search(
            "helper approach", kind="decision")[0]["id"]
        d2 = self.mem.add_decision(
            self.rid, "pick helper approach v2", "v2 body",
            [doc["id"]], author="tester", supersedes=d1["id"])
        rows = {r["id"]: r for r in self.mem.decisions(self.rid)}
        self.assertEqual(rows[d1["id"]]["status"], "superseded")
        self.assertEqual(rows[d2["id"]]["status"], "active")
        self.assertEqual(rows[d2["id"]]["supersedes"], d1["id"])
        hits = {h["id"]: h for h in
                self.mem.search("helper approach", kind="decision")}
        # the old active-text doc is archived but still retrievable
        self.assertNotIn(old_doc, hits)
        self.assertIsNotNone(self.mem.document(old_doc))
        # the superseded decision stays searchable, marked superseded
        superseded = [h for h in hits.values()
                      if h["source_id"] == d1["id"]]
        self.assertTrue(superseded)
        self.assertIn("status: superseded", superseded[0]["text"])
        self.assertTrue(any("v2 body" in h["text"]
                            for h in hits.values()))
        # reindex regenerates the same searchable state
        self.mem.index(None)
        hits2 = {h["id"]: h for h in
                 self.mem.search("helper approach", kind="decision")}
        self.assertEqual(set(hits2), set(hits))

    def test_decision_source_ids_must_be_list(self):
        self._session()
        self.mem.index(None)
        with self.assertRaises(ValueError):
            self.mem.add_decision(
                self.rid, "t", "b", "not-a-list", author="t")
        self.assertEqual(self.mem.decisions(self.rid), [])

    # ---- dispatch ---------------------------------------------------
    def test_dispatch_boundaries(self):
        for cid, sha, msg, ts in (
                ("1" * 32, "a" * 40, "FIRST",
                 "2026-01-10T00:00:00.123456+00:00"),
                ("2" * 32, "b" * 40, "  \n  ",
                 "2026-01-15T12:00:00+00:00"),
                ("3" * 32, "c" * 40, "LAST",
                 "2026-01-20T00:00:00+00:00")):
            self.store.save_checkpoint(
                self.rid, cid, sha, branch="main", message=msg,
                author="t", files=["f.py"], diff=None, links=[],
                worktree=None, created_at=ts)
        # since inclusive (with microseconds), until exclusive
        out = self.mem.dispatch(
            repo_id=self.rid, since="2026-01-10", until="2026-01-20")
        self.assertEqual(len(out["source_ids"]), 2)
        self.assertIn("FIRST", out["markdown"])
        self.assertIn("(no message)", out["markdown"])
        self.assertNotIn("LAST", out["markdown"])
        # non-UTC offsets normalize to aware UTC bounds
        out = self.mem.dispatch(
            repo_id=self.rid, since="2026-01-10T05:30:00+05:30",
            until="2026-01-19T00:00:00-02:00")
        self.assertEqual(len(out["source_ids"]), 2)
        # naive timestamps are treated as UTC
        out = self.mem.dispatch(
            repo_id=self.rid, since="2026-01-14 00:00:00",
            until="2026-01-16 00:00:00")
        self.assertEqual(len(out["source_ids"]), 1)
        with self.assertRaises(ValueError):
            self.mem.dispatch(since="2026-02-01", until="2026-01-01")
        with self.assertRaises(ValueError):
            self.mem.dispatch(since="2026-01-10", until="2026-01-10")

    def test_dispatch_truncates_explicitly(self):
        conn = self.store._connect()
        with conn:
            for i in range(2001):
                conn.execute(
                    "INSERT INTO checkpoints(id,repo_id,commit_sha,"
                    "branch,message,author,created_at,files,diff,"
                    "session_ids) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (f"{i:032x}", self.rid, f"{i + 1:040x}", "main",
                     "c", "t", "2026-01-15T00:00:00+00:00", "[]",
                     None, "[]"))
        conn.close()
        out = self.mem.dispatch(since="2026-01-01",
                                until="2026-02-01")
        self.assertTrue(out["truncated"])
        self.assertEqual(len(out["source_ids"]), 2000)
        self.assertIn("truncated", out["markdown"])

    # ---- python graph shadowing -------------------------------------
    def _call_pairs(self, path):
        conn = self.store._connect()
        edges = conn.execute(
            "SELECT source_id,target_id FROM graph_edges"
            " WHERE repo_id=? AND kind='calls'", (self.rid,)
        ).fetchall()
        syms = {r["id"]: r["qualified_name"] for r in conn.execute(
            "SELECT id,qualified_name FROM graph_symbols"
            " WHERE repo_id=? AND path=?",
            (self.rid, path)).fetchall()}
        conn.close()
        return {(syms.get(e["source_id"]), syms.get(e["target_id"]))
                for e in edges}

    def test_python_call_shadowing(self):
        self.add_commit(
            "s.py",
            "def helper():\n    return 1\n\n"
            "def clean():\n    return helper()\n\n"
            "def param_shadow(helper):\n    return helper()\n\n"
            "def assign_shadow():\n    helper = lambda: 2\n"
            "    return helper()\n\n"
            "def import_shadow():\n    import helper\n"
            "    return helper()\n\n"
            "def nested_shadow():\n    def helper():\n"
            "        return 3\n    return helper()\n\n"
            "class C:\n    helper = None\n"
            "    def method(self):\n        return helper()\n",
            "shadowing cases")
        out = self.mem.index(
            {"id": self.rid, "root": str(self.repo)})
        pairs = self._call_pairs("s.py")
        self.assertIn(("clean", "helper"), pairs)
        for caller in ("param_shadow", "assign_shadow",
                       "import_shadow", "nested_shadow", "C.method"):
            self.assertNotIn((caller, "helper"), pairs)
        self.assertTrue(any("shadowed" in d
                            for d in out["diagnostics"]))

    def test_module_level_binding_shadows_function(self):
        self.add_commit(
            "m.py",
            "def helper():\n    return 1\n\n"
            "for helper in []:\n    pass\n\n"
            "def caller():\n    return helper()\n",
            "module shadow")
        self.mem.index({"id": self.rid, "root": str(self.repo)})
        self.assertNotIn(("caller", "helper"),
                         self._call_pairs("m.py"))

    def test_parse_failure_is_lexical_not_ast(self):
        self.add_commit(
            "bad.py", "def broken(:\n    pass\n", "broken py")
        out = self.mem.index(
            {"id": self.rid, "root": str(self.repo)})
        self.assertTrue(any("parse failed" in d
                            for d in out["diagnostics"]))
        syms = [s for s in self.mem.graph_search("bad", repo_id=self.rid)
                if s["path"] == "bad.py"]
        self.assertTrue(syms)
        self.assertTrue(all(s["analysis"] == "lexical"
                            for s in syms))

    # ---- semantic scope / errors -------------------------------------
    def test_semantic_scope_and_errors(self):
        import partial.brain_contract as bc
        from partial.ai import OpenAIProvider
        self._session()
        self.mem.index({"id": self.rid, "root": str(self.repo)})

        # unconfigured provider: index must not claim semantic ran,
        # search/context must error clearly (never "hybrid")
        with mock.patch.object(
                OpenAIProvider, "configured",
                new_callable=lambda: property(lambda s: False)):
            out = self.mem.index(None, semantic=True)
            self.assertFalse(out["semantic"])
            with self.assertRaises(ValueError):
                self.mem.search("helper", semantic=True)
            with self.assertRaises(ValueError):
                self.mem.context("helper", semantic=True)

        embedded = []

        def fake_embed(self, texts):
            embedded.extend(texts)
            return [[1.0, 0.5] for _ in texts]

        on = lambda: property(lambda s: True)
        with mock.patch.object(OpenAIProvider, "configured",
                               new_callable=on), \
                mock.patch.object(OpenAIProvider, "embed",
                                  fake_embed):
            # history-only semantic index embeds derived docs but
            # never code docs
            out = self.mem.index(None, semantic=True)
            self.assertTrue(out["semantic"])
            conn = self.store._connect()
            code_total = conn.execute(
                "SELECT COUNT(*) c FROM memory_documents WHERE"
                " kind='code' AND archived=0").fetchone()["c"]
            code_missing = conn.execute(
                "SELECT COUNT(*) c FROM memory_documents WHERE"
                " kind='code' AND archived=0 AND embedding IS NULL"
            ).fetchone()["c"]
            hist_missing = conn.execute(
                "SELECT COUNT(*) c FROM memory_documents WHERE kind"
                " IN ('session','checkpoint','decision')"
                " AND archived=0 AND embedding IS NULL"
            ).fetchone()["c"]
            conn.close()
            self.assertTrue(code_total)
            self.assertEqual(code_missing, code_total)
            self.assertEqual(hist_missing, 0)
            self.assertTrue(embedded)
            self.assertFalse(any("def helper" in t
                                 for t in embedded))
            # explicit repo index embeds that repo's code docs
            embedded.clear()
            self.mem.index({"id": self.rid, "root": str(self.repo)},
                           semantic=True)
            self.assertTrue(any("def helper" in t
                                for t in embedded))
            conn = self.store._connect()
            code_missing = conn.execute(
                "SELECT COUNT(*) c FROM memory_documents WHERE"
                " kind='code' AND archived=0 AND embedding IS NULL"
            ).fetchone()["c"]
            conn.close()
            self.assertEqual(code_missing, 0)
            rows = self.mem.search("helper", semantic=True)
            self.assertEqual(rows[0]["retrieval_method"],
                             "fts5-cosine-rrf-v1")
            self.assertEqual(
                self.mem.context("helper", semantic=True)["mode"],
                "hybrid")
            # foreign-model vectors are not compatible
            conn = self.store._connect()
            conn.execute("UPDATE memory_documents"
                         " SET embedding_model='other-model'")
            conn.commit()
            conn.close()
            with self.assertRaises(ValueError):
                self.mem.search("helper", semantic=True)
            # incompatible dimensions are not compatible either
            conn = self.store._connect()
            conn.execute(
                "UPDATE memory_documents SET embedding=?,"
                " embedding_model=?",
                ("[1.0, 2.0, 3.0]", bc.EMBEDDING_MODEL))
            conn.commit()
            conn.close()
            with self.assertRaises(ValueError):
                self.mem.search("helper", semantic=True)

    def test_reindex_preserves_embeddings(self):
        from partial.ai import OpenAIProvider
        calls = []

        def fake_embed(self, texts):
            calls.extend(texts)
            return [[1.0, 0.5] for _ in texts]

        with mock.patch.object(
                OpenAIProvider, "configured",
                new_callable=lambda: property(lambda s: True)), \
                mock.patch.object(OpenAIProvider, "embed",
                                  fake_embed):
            self.mem.index({"id": self.rid, "root": str(self.repo)},
                           semantic=True)
            conn = self.store._connect()
            emb = {r["id"]: r["embedding"] for r in conn.execute(
                "SELECT id,embedding FROM memory_documents WHERE"
                " repo_id=? AND embedding IS NOT NULL",
                (self.rid,)).fetchall()}
            conn.close()
            self.assertTrue(emb)
            self.mem.index({"id": self.rid, "root": str(self.repo)},
                           semantic=True)
            conn = self.store._connect()
            emb2 = {r["id"]: r["embedding"] for r in conn.execute(
                "SELECT id,embedding FROM memory_documents WHERE"
                " repo_id=? AND embedding IS NOT NULL",
                (self.rid,)).fetchall()}
            conn.close()
            # identical docs keep their vectors; nothing re-embedded
            self.assertEqual(emb, emb2)
            self.assertEqual(len(calls), len(emb))


if __name__ == "__main__":
    unittest.main()
