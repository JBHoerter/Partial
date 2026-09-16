"""Bundle/memory import validation and workflow-run lifecycle tests.

Covers the security/correctness invariants enforced by
``Store.import_bundle`` for the memory extension (documents, graph,
decisions, runs, repository indexes), checkpoint-bundle citation
closure, and owner-aware interrupted-run recovery.
"""

import json
import os
import unittest
from pathlib import Path

from partial.memory import Memory, document_id, _symbol_id
from partial.models import Event, scoped_session_id
from partial.store import Store

from helpers import RepoTestCase

TS = "2026-01-01T00:00:00Z"
RID = "a" * 64
RID2 = "b" * 64
SHA1 = "c" * 40
SHA2 = "d" * 40
BLOB = "e" * 40


def repo_entry(rid=RID):
    return {"id": rid, "name": "r", "remote": None, "created_at": TS}


def session_entry(rid=RID, native="n1", agent="devin"):
    return {
        "id": scoped_session_id(rid, agent, native),
        "repo_id": rid, "native_id": native, "agent": agent,
        "title": "sess", "branch": None, "parent_session_id": None,
        "model": None, "status": "active", "started_at": TS,
        "updated_at": TS}


def event_entry(sid, eid="e1", kind="prompt", text="hi"):
    return {"id": eid, "session_id": sid, "agent": "devin",
            "kind": kind, "timestamp": TS, "text": text, "data": {}}


def doc(rid=RID, kind="session", source_id="seed:x", text="body",
        path=None, commit_sha=None, line_start=None, line_end=None,
        title="t", archived=0, **kw):
    d = {
        "id": document_id(rid, kind, source_id, path, commit_sha,
                          line_start, text),
        "repo_id": rid, "kind": kind, "source_id": source_id,
        "title": title, "text": text, "path": path,
        "line_start": line_start, "line_end": line_end,
        "commit_sha": commit_sha, "updated_at": TS,
        "archived": archived,
    }
    d.update(kw)
    return d


def code_doc(rid=RID, commit=SHA1, blob=BLOB, path="f.py", **kw):
    return doc(rid, "code", blob, "def f():\n    return 1\n",
               path=path, commit_sha=commit, line_start=1, line_end=2,
               title=path, **kw)


def sym(rid=RID, path="f.py", qname="f", name=None, commit=SHA1):
    return {
        "id": _symbol_id(rid, path, qname), "repo_id": rid,
        "path": path, "name": name or qname.split(".")[-1],
        "qualified_name": qname, "kind": "function", "line": 1,
        "end_line": 2, "language": "python", "analysis": "ast",
        "commit_sha": commit}


def edge(src, dst, rid=RID, kind="calls"):
    return {"repo_id": rid, "source_id": src, "target_id": dst,
            "kind": kind}


def index_entry(rid=RID, commit=SHA1, indexed_at=TS):
    return {"repo_id": rid, "commit_sha": commit,
            "indexed_at": indexed_at}


def decision(did, rid=RID, title="t", body="b", status="active",
             source_ids=None, supersedes=None):
    return {"id": did, "repo_id": rid, "title": title, "body": body,
            "status": status, "source_ids": source_ids or [],
            "author": "a", "created_at": TS, "supersedes": supersedes}


def run(repo_id=RID, status="completed", source_ids=None, report=None,
        details=None, kind="ask", run_id="9" * 64,
        created_at=TS, updated_at=TS):
    return {"id": run_id, "kind": kind, "repo_id": repo_id,
            "status": status, "created_at": created_at,
            "updated_at": updated_at,
            "source_ids": source_ids or [],
            "report": report if report is not None else {"answer": "x"},
            "details": details if details is not None else {}}


def bundle(*, repos=None, sessions=None, events=None, checkpoints=None,
           links=None, memory=None):
    return {
        "version": 1,
        "repositories": repos if repos is not None else [repo_entry()],
        "sessions": sessions or [],
        "events": events or [],
        "checkpoints": checkpoints or [],
        "links": links or [],
        "memory": memory,
    }


def mem(**parts):
    m = {"version": 1, "documents": [], "symbols": [], "edges": [],
         "decisions": [], "runs": [], "indexes": []}
    m.update(parts)
    return m


def _dead_pid():
    pid = 400000
    while True:
        try:
            os.kill(pid, 0)
            pid += 1
        except ProcessLookupError:
            return pid
        except PermissionError:
            pid += 1


class MemoryDocImportTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.home / "partial.db")
        self.sid = session_entry()["id"]

    def _import(self, memory, **kw):
        return Store(self.tmp / "in.db").import_bundle(
            bundle(memory=memory, **kw))

    def _expect_bad(self, memory, **kw):
        store = Store(self.tmp / f"bad{MemoryDocImportTests._n}.db")
        MemoryDocImportTests._n += 1
        with self.assertRaises(ValueError):
            store.import_bundle(bundle(memory=memory, **kw))
        conn = store._connect()
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM memory_documents").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0)

    _n = 0

    def test_session_doc_roundtrip(self):
        s = session_entry()
        d = doc(source_id=f"{s['id']}:e1")
        self._import(mem(documents=[d]),
                     sessions=[s], events=[event_entry(s["id"])])
        fresh = Store(self.tmp / "in.db")
        self.assertIsNotNone(Memory(fresh).document(d["id"]))

    def test_seed_and_review_docs(self):
        seed = doc(source_id="seed:notes.txt")
        review = doc(kind="checkpoint",
                     source_id=f"review:{SHA1}..{SHA2}",
                     commit_sha=SHA2)
        self._import(mem(documents=[seed, review]))
        fresh = Store(self.tmp / "in.db")
        self.assertIsNotNone(Memory(fresh).document(seed["id"]))
        self.assertIsNotNone(Memory(fresh).document(review["id"]))

    def test_doc_invariants(self):
        s = session_entry()
        base = doc(source_id=f"{s['id']}:e1")
        good = {"sessions": [s], "events": [event_entry(s["id"])]}

        def re_id(d):
            d = dict(d)
            d["id"] = document_id(
                d["repo_id"], d["kind"], d["source_id"], d.get("path"),
                d.get("commit_sha"), d.get("line_start"), d["text"])
            return d

        cases = [
            # forged id (not recomputed to canonical form)
            {**base, "id": "f" * 64},
            # unknown / empty repo
            {**re_id({**base, "repo_id": "0" * 64}), "repo_id": "0" * 64},
            {**base, "repo_id": ""},
            re_id({**base, "path": "/abs.py"}),
            re_id({**base, "path": "../esc.py"}),
            re_id({**base, "line_start": 0}),
            re_id({**base, "line_start": 5, "line_end": 2}),
            re_id({**base, "commit_sha": "abc123"}),   # bad SHA length
            {**base, "archived": 2},
            {**base, "updated_at": "not-a-time"},
            {**base, "text": "token sk-secret12345"},
            {**base, "embedding": [0.1, float("nan")],
             "embedding_model": "text-embedding-3-small"},
            {**base, "embedding": [0.1],
             "embedding_model": "other-model"},
            {**base, "embedding_model": "text-embedding-3-small"},
            # session doc invariants
            re_id({**base, "source_id": "0" * 64 + ":e1"}),  # foreign session
            re_id({**base, "source_id": f"{self.sid}:nope"}),  # no event
            re_id({**base, "source_id": self.sid}),          # missing :eid
            re_id({**base, "source_id": "seed:"}),           # empty seed
            re_id({**base, "source_id": "seed:a/b"}),        # pathy seed
            # checkpoint doc invariants
            re_id({**base, "kind": "checkpoint", "source_id": "z" * 32}),
            re_id({**base, "kind": "checkpoint",
                   "source_id": "review:notasha..x"}),
            re_id({**base, "kind": "checkpoint",
                   "source_id": f"review:{SHA1}..{SHA2}",
                   "commit_sha": SHA1}),   # head mismatch
            # code doc invariants
            re_id({**base, "kind": "code", "source_id": "c" * 7}),
            re_id({**base, "kind": "decision", "source_id": "nothex"}),
        ]
        for i, bad_doc in enumerate(cases):
            store = Store(self.tmp / f"docbad{i}.db")
            with self.assertRaises(ValueError, msg=f"case {i}"):
                store.import_bundle(
                    bundle(memory=mem(documents=[bad_doc]), **good))
            conn = store._connect()
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM memory_documents"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(n, 0, f"case {i} leaked a document")

    def test_code_doc_requires_path_lines_commit(self):
        d = code_doc()
        del d["path"]
        d["id"] = document_id(RID, "code", BLOB, None, SHA1, 1,
                              d["text"])
        self._expect_bad(mem(documents=[d]))
        d2 = code_doc(commit=None)
        self._expect_bad(mem(documents=[d2]))


class GraphImportTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.home / "partial.db")

    def _graph(self, store, rid=RID):
        conn = store._connect()
        try:
            syms = conn.execute(
                "SELECT * FROM graph_symbols WHERE repo_id=?",
                (rid,)).fetchall()
            edges = conn.execute(
                "SELECT * FROM graph_edges WHERE repo_id=?",
                (rid,)).fetchall()
            idx = conn.execute(
                "SELECT * FROM repository_indexes WHERE repo_id=?",
                (rid,)).fetchone()
            docs = conn.execute(
                "SELECT id,archived,commit_sha FROM memory_documents"
                " WHERE repo_id=? AND kind='code'", (rid,)).fetchall()
            return ([dict(s) for s in syms], [dict(e) for e in edges],
                    dict(idx) if idx else None,
                    [dict(d) for d in docs])
        finally:
            conn.close()

    def test_symbols_edges_roundtrip(self):
        s1, s2 = sym(qname="f"), sym(qname="g")
        store = Store(self.tmp / "g.db")
        store.import_bundle(bundle(memory=mem(
            documents=[code_doc()], symbols=[s1, s2],
            edges=[edge(s1["id"], s2["id"])],
            indexes=[index_entry()])))
        syms, edges, idx, _ = self._graph(store)
        self.assertEqual(len(syms), 2)
        self.assertEqual(len(edges), 1)
        self.assertEqual(idx["commit_sha"], SHA1)

    def test_edge_endpoint_rules(self):
        s1, s2 = sym(qname="f"), sym(qname="g")
        other = sym(rid=RID2, qname="h")
        cases = [
            # ghost import target
            [edge(s1["id"], "import:os")],
            # unresolved endpoint
            [edge(s1["id"], "0" * 64)],
            # endpoint from a different repo
            [edge(s1["id"], other["id"])],
            # unknown kind
            [edge(s1["id"], s2["id"], kind="reads")],
        ]
        for i, edges in enumerate(cases):
            store = Store(self.tmp / f"e{i}.db")
            with self.assertRaises(ValueError, msg=f"case {i}"):
                store.import_bundle(bundle(
                    repos=[repo_entry(), repo_entry(RID2)],
                    memory=mem(
                        symbols=[s1, s2, other], edges=edges,
                        indexes=[index_entry(), index_entry(RID2)])))

    def test_symbol_id_mismatch_rejected(self):
        s = sym()
        s["id"] = "1" * 64
        store = Store(self.tmp / "sm.db")
        with self.assertRaises(ValueError):
            store.import_bundle(bundle(memory=mem(
                symbols=[s], indexes=[index_entry()])))

    def test_graph_without_index_record_dropped(self):
        # Symbols/edges without an index record for the repo are old
        # snapshots: validated but never merged into the local graph.
        s1, s2 = sym(qname="f"), sym(qname="g")
        store = Store(self.tmp / "ni.db")
        store.import_bundle(bundle(memory=mem(
            symbols=[s1, s2], edges=[edge(s1["id"], s2["id"])])))
        syms, edges, idx, _ = self._graph(store)
        self.assertEqual(syms, [])
        self.assertEqual(edges, [])
        self.assertIsNone(idx)

    def test_newer_index_replaces_older_keeps_fresher(self):
        # Seed local index at SHA1/T1.
        s1, s2 = sym(qname="f"), sym(qname="g")
        store = Store(self.tmp / "fr.db")
        store.import_bundle(bundle(memory=mem(
            documents=[code_doc()], symbols=[s1, s2],
            edges=[edge(s1["id"], s2["id"])],
            indexes=[index_entry(indexed_at="2026-01-01T00:00:00Z")])))
        # Newer index at SHA2 replaces only this repo's graph and
        # archives the old code docs.
        n1 = sym(qname="h", commit=SHA2, name="h")
        store.import_bundle(bundle(memory=mem(
            documents=[code_doc(commit=SHA2, blob="f" * 40)],
            symbols=[n1],
            indexes=[index_entry(commit=SHA2,
                                 indexed_at="2026-02-01T00:00:00Z")])))
        syms, edges, idx, docs = self._graph(store)
        self.assertEqual([s["qualified_name"] for s in syms], ["h"])
        self.assertEqual(edges, [])
        self.assertEqual(idx["commit_sha"], SHA2)
        archived = {d["commit_sha"]: d["archived"] for d in docs}
        self.assertEqual(archived, {SHA1: 1, SHA2: 0})
        # A strictly older incoming index must not touch the fresher
        # local graph/index; its code docs import archived.
        store.import_bundle(bundle(memory=mem(
            documents=[code_doc()], symbols=[s1, s2],
            edges=[edge(s1["id"], s2["id"])],
            indexes=[index_entry(indexed_at="2026-01-01T00:00:00Z")])))
        syms, edges, idx, docs = self._graph(store)
        self.assertEqual([s["qualified_name"] for s in syms], ["h"])
        self.assertEqual(idx["commit_sha"], SHA2)
        archived = {d["commit_sha"]: d["archived"] for d in docs}
        self.assertEqual(archived, {SHA1: 1, SHA2: 0})
        # Re-importing the same (equal) index is idempotent.
        store.import_bundle(bundle(memory=mem(
            documents=[code_doc(commit=SHA2, blob="f" * 40)],
            symbols=[n1],
            indexes=[index_entry(commit=SHA2,
                                 indexed_at="2026-02-01T00:00:00Z")])))
        syms, _, idx, docs = self._graph(store)
        self.assertEqual([s["qualified_name"] for s in syms], ["h"])
        self.assertEqual(idx["commit_sha"], SHA2)
        self.assertEqual(
            {d["commit_sha"]: d["archived"] for d in docs},
            {SHA1: 1, SHA2: 0})


class DecisionImportTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.home / "partial.db")

    def test_decision_roundtrip_and_status(self):
        d1 = decision("1" * 64, title="old", status="superseded")
        d2 = decision("2" * 64, title="new", supersedes="1" * 64)
        store = Store(self.tmp / "d.db")
        store.import_bundle(bundle(memory=mem(decisions=[d1, d2])))
        got = {d["id"]: d for d in Memory(store).decisions(RID)}
        self.assertEqual(got["1" * 64]["status"], "superseded")
        self.assertEqual(got["2" * 64]["status"], "active")

    def test_conflicting_existing_id_rejected(self):
        d = decision("1" * 64)
        self.store.import_bundle(
            bundle(memory=mem(decisions=[d])))
        bad = decision("1" * 64, body="different")
        with self.assertRaises(ValueError):
            self.store.import_bundle(
                bundle(memory=mem(decisions=[bad])))
        # Foreign-repo decision sharing an id is also a conflict.
        self.store.import_bundle(bundle(
            repos=[repo_entry(RID2)],
            memory=mem(decisions=[decision("3" * 64, rid=RID2)])))
        clash = decision("3" * 64, rid=RID)  # same content, other repo
        with self.assertRaises(ValueError):
            self.store.import_bundle(
                bundle(memory=mem(decisions=[clash])))

    def test_supersedes_same_repo_and_acyclic(self):
        cases = [
            [decision("1" * 64, supersedes="2" * 64),
             decision("2" * 64, supersedes="1" * 64)],          # cycle
            [decision("1" * 64, supersedes="1" * 64)],          # self
            [decision("1" * 64, supersedes="9" * 64)],          # unknown
        ]
        for i, decs in enumerate(cases):
            with self.assertRaises(ValueError, msg=f"case {i}"):
                Store(self.tmp / f"c{i}.db").import_bundle(
                    bundle(memory=mem(decisions=decs)))
        # Cross-repo supersedes.
        store = Store(self.tmp / "xr.db")
        with self.assertRaises(ValueError):
            store.import_bundle(bundle(
                repos=[repo_entry(), repo_entry(RID2)],
                memory=mem(decisions=[
                    decision("1" * 64, supersedes="2" * 64),
                    decision("2" * 64, rid=RID2)])))

    def test_source_ids_same_repo_docs(self):
        d = code_doc()
        good = decision("1" * 64, source_ids=[d["id"]])
        store = Store(self.tmp / "ok.db")
        store.import_bundle(bundle(
            memory=mem(documents=[d], decisions=[good])))
        # Unknown doc id.
        with self.assertRaises(ValueError):
            Store(self.tmp / "b1.db").import_bundle(bundle(memory=mem(
                decisions=[decision("1" * 64, source_ids=["7" * 64])])))
        # Doc belonging to a different repo.
        foreign = code_doc(rid=RID2)
        with self.assertRaises(ValueError):
            Store(self.tmp / "b2.db").import_bundle(bundle(
                repos=[repo_entry(), repo_entry(RID2)],
                memory=mem(
                    documents=[foreign],
                    decisions=[decision("1" * 64,
                                        source_ids=[foreign["id"]])])))
        # Non-hex source id.
        with self.assertRaises(ValueError):
            Store(self.tmp / "b3.db").import_bundle(bundle(memory=mem(
                decisions=[decision("1" * 64, source_ids=["zz"])])))

    def test_active_claim_does_not_resurrect_superseded(self):
        d = decision("1" * 64, status="superseded")
        self.store.import_bundle(bundle(memory=mem(decisions=[d])))
        again = decision("1" * 64, status="active")
        self.store.import_bundle(bundle(memory=mem(decisions=[again])))
        got = Memory(self.store).decisions(RID)
        self.assertEqual(got[0]["status"], "superseded")


class RunImportTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.home / "partial.db")

    def test_run_imported_as_claim(self):
        d = code_doc()
        r = run(source_ids=[d["id"]],
                details={"evidence": [{"id": d["id"]}],
                         "run_owner": "1-forged",
                         "runner": "forged-server", "pid": 1})
        store = Store(self.tmp / "r.db")
        store.import_bundle(bundle(
            memory=mem(documents=[d], runs=[r])))
        got = store.get_run(r["id"])
        self.assertEqual(got["status"], "imported")
        self.assertEqual(
            got["details"]["imported_status"], "completed")
        for key in ("run_owner", "runner", "pid"):
            self.assertNotIn(key, got["details"])

    def test_run_field_validation(self):
        d = code_doc()
        cases = [
            run(kind="hack"),
            run(status="verified"),
            run(repo_id="0" * 64),               # unknown repo
            run(run_id="nope"),                   # bad id
            run(source_ids=["zz"]),
            run(source_ids=["7" * 64]),           # unknown doc
            run(report="notjson{"),
            run(report=42),
            run(details="notjson{"),
            run(details={"evidence": "x"}),       # evidence not a list
            run(details={"evidence": [{"id": "z" * 64}]}),  # unknown
            run(details={"evidence": [{"id": "zz"}]}),      # bad id
            run(created_at="never"),
        ]
        for i, r in enumerate(cases):
            store = Store(self.tmp / f"r{i}.db")
            with self.assertRaises(ValueError, msg=f"case {i}"):
                store.import_bundle(bundle(
                    memory=mem(documents=[d], runs=[r])))
            conn = store._connect()
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(n, 0, f"case {i} leaked a run")

    def test_run_cites_foreign_doc(self):
        foreign = code_doc(rid=RID2)
        store = Store(self.tmp / "rf.db")
        with self.assertRaises(ValueError):
            store.import_bundle(bundle(
                repos=[repo_entry(), repo_entry(RID2)],
                memory=mem(documents=[foreign],
                           runs=[run(source_ids=[foreign["id"]])])))


class CheckpointBundleClosureTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.home / "partial.db")
        self.repo_row = self.store.register_repo(self.repo)
        self.rid = self.repo_row["id"]
        self.mem = Memory(self.store)

    def _session(self, native="s1"):
        self.store.ingest(self.rid, [
            Event(id="e1", session_id=native, agent="devin",
                  kind="prompt", timestamp=TS, text="work")],
            worktree=str(self.repo))

    def test_cited_doc_closure_pulls_session_and_checkpoint(self):
        self._session("s1")
        sha1 = self.add_commit("f.py", "x = 1\n", "c1")
        self.store.save_checkpoint(
            self.rid, "a" * 32, sha1, branch="main", message="c1",
            author="t", files=["f.py"], diff=None, links=[],
            worktree=None, created_at=TS)
        sha2 = self.add_commit("g.py", "y = 2\n", "c2")
        self.store.save_checkpoint(
            self.rid, "b" * 32, sha2, branch="main", message="c2",
            author="t", files=["g.py"], diff=None, links=[],
            worktree=None, created_at="2026-01-02T00:00:00Z")
        self.mem.index({"id": self.rid, "root": str(self.repo)})
        conn = self.store._connect()
        try:
            sess_doc = conn.execute(
                "SELECT id FROM memory_documents WHERE kind='session'"
                " LIMIT 1").fetchone()["id"]
            cp1_doc = conn.execute(
                "SELECT id FROM memory_documents WHERE kind='checkpoint'"
                " AND source_id=?", ("a" * 32,)).fetchone()["id"]
        finally:
            conn.close()
        # Decisions cite a session doc of an unlinked session and a
        # doc of a different checkpoint; both must be carried with the
        # entities they need for a clean import.
        self.mem.add_decision(
            self.rid, "cite", "body", [sess_doc, cp1_doc], author="t")
        b = self.store.checkpoint_bundle("b" * 32)
        doc_ids = {d["id"] for d in b["memory"]["documents"]}
        self.assertIn(sess_doc, doc_ids)
        self.assertIn(cp1_doc, doc_ids)
        sid = scoped_session_id(self.rid, "devin", "s1")
        self.assertIn(sid, {s["id"] for s in b["sessions"]})
        self.assertIn("a" * 32, {c["id"] for c in b["checkpoints"]})
        ev_sids = {e["session_id"] for e in b["events"]}
        self.assertIn(sid, ev_sids)
        fresh = Store(self.tmp / "fb.db")
        res = fresh.import_bundle(b)
        self.assertGreater(res["events"], 0)
        self.assertIsNotNone(Memory(fresh).document(sess_doc))
        self.assertIsNotNone(fresh.get_checkpoint("a" * 32))

    def test_export_never_leaks_settings_or_owner(self):
        self._session("s9")
        self.store.set_memory_setting("external_ai_enabled", "true")
        self.store.save_run("8" * 64, "ask", self.rid, "running",
                            [], {}, {})
        # A row written before run_owner existed can still carry the
        # legacy server runner/pid marker; it must not be exported.
        conn = self.store._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE workflow_runs SET details=? WHERE id=?",
                    (json.dumps({"run_owner": "9-gone",
                                 "runner": "legacy-srv", "pid": 4242,
                                 "keep": "yes"}), "8" * 64))
        finally:
            conn.close()
        self.store.create_project("p")
        b = self.store.export_bundle()
        blob = json.dumps(b)
        self.assertNotIn("external_ai_enabled", blob)
        self.assertNotIn("run_owner", blob)
        self.assertNotIn("legacy-srv", blob)
        self.assertNotIn("projects", b)
        run_row = [r for r in b["memory"]["runs"]
                   if r["id"] == "8" * 64][0]
        details = run_row["details"]
        if isinstance(details, str):
            details = json.loads(details)
        for key in ("run_owner", "runner", "pid"):
            self.assertNotIn(key, details)
        self.assertEqual(details.get("keep"), "yes")


class RunLifecycleTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.home / "partial.db")
        self.rid = self.store.register_repo(self.repo)["id"]

    def test_save_run_validation(self):
        rid = "8" * 64
        for kwargs in [
            dict(run_id="nope"), dict(kind="hack"),
            dict(status="bogus"), dict(repo_id="x"),
            dict(source_ids="nope"), dict(source_ids=["zz"]),
            dict(report=[]), dict(details=[]),
        ]:
            args = dict(run_id=rid, kind="ask", repo_id=self.rid,
                        status="planned", source_ids=[], report={},
                        details={})
            args.update(kwargs)
            with self.assertRaises(ValueError, msg=kwargs):
                self.store.save_run(**args)

    def test_save_run_strips_internal_markers(self):
        rid = "8" * 64
        # Caller-supplied ownership keys are never persisted, even for
        # non-running statuses; save_run only stamps run_owner itself.
        self.store.save_run(
            rid, "ask", self.rid, "completed", [], {},
            {"run_owner": "forged", "runner": "srv", "pid": 1,
             "note": "ok"})
        self.assertEqual(self.store.get_run(rid)["details"],
                         {"note": "ok"})
        conn = self.store._connect()
        try:
            det = json.loads(conn.execute(
                "SELECT details FROM workflow_runs WHERE id=?",
                (rid,)).fetchone()["details"])
        finally:
            conn.close()
        self.assertEqual(det, {"note": "ok"})

    def test_list_runs_hides_legacy_markers(self):
        rid = "8" * 64
        conn = self.store._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO workflow_runs(id,kind,repo_id,status,"
                    "created_at,updated_at,source_ids,report,details)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (rid, "ask", self.rid, "running", TS, TS, "[]",
                     "{}", json.dumps(
                         {"runner": "old-server",
                          "pid": os.getpid(), "note": "ok"})))
        finally:
            conn.close()
        det = self.store.list_runs()[0]["details"]
        for key in ("run_owner", "runner", "pid"):
            self.assertNotIn(key, det)
        self.assertEqual(det.get("note"), "ok")

    def test_interrupt_skips_live_owner(self):
        rid = "8" * 64
        self.store.save_run(rid, "ask", self.rid, "running", [], {}, {})
        self.assertEqual(self.store.interrupt_running_runs(), 0)
        run = self.store.get_run(rid)
        self.assertEqual(run["status"], "running")
        self.assertNotIn("run_owner", run["details"])

    def test_interrupt_dead_and_ownerless(self):
        dead = "7" * 64
        orphan = "6" * 64
        legacy_dead = "5" * 64
        legacy_live = "4" * 64
        dead_pid = _dead_pid()
        self.store.save_run(dead, "ask", self.rid, "running", [], {}, {})
        conn = self.store._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE workflow_runs SET details=? WHERE id=?",
                    (json.dumps({"run_owner": "99999999-gone"}), dead))
                for rid, details in (
                        (orphan, {}),
                        # Rows written before run_owner existed carry
                        # the legacy server runner/pid marker.
                        (legacy_dead,
                         {"runner": "old-server", "pid": dead_pid}),
                        (legacy_live,
                         {"runner": "old-server",
                          "pid": os.getpid()})):
                    conn.execute(
                        "INSERT INTO workflow_runs(id,kind,repo_id,"
                        "status,created_at,updated_at,source_ids,"
                        "report,details) VALUES(?,?,?,?,?,?,?,?,?)",
                        (rid, "ask", self.rid, "running", TS, TS,
                         "[]", "{}", json.dumps(details)))
        finally:
            conn.close()
        # Dead owner, ownerless row, and dead legacy pid are all
        # interrupted; the live legacy pid row is left running.
        self.assertEqual(self.store.interrupt_running_runs(), 3)
        for rid in (dead, orphan, legacy_dead):
            run = self.store.get_run(rid)
            self.assertEqual(run["status"], "interrupted", rid)
            for key in ("run_owner", "runner", "pid"):
                self.assertNotIn(key, run["details"], rid)
        self.assertIn("interrupted",
                      self.store.get_run(dead)["report"]["error"])
        run = self.store.get_run(legacy_live)
        self.assertEqual(run["status"], "running")
        # The internal legacy marker is still hidden from readers.
        for key in ("run_owner", "runner", "pid"):
            self.assertNotIn(key, run["details"])

    def test_rollback_on_any_invalid_memory_record(self):
        s = session_entry()
        b = bundle(
            repos=[repo_entry(RID)],
            sessions=[s], events=[event_entry(s["id"])],
            memory=mem(
                documents=[doc(source_id=f"{s['id']}:e1")],
                decisions=[decision("1" * 64, source_ids=["7" * 64])]))
        store = Store(self.tmp / "rb.db")
        with self.assertRaises(ValueError):
            store.import_bundle(b)
        self.assertEqual(store.stats(),
                         {"sessions": 0, "checkpoints": 0,
                          "repositories": 0})


class BundleEventPathTests(RepoTestCase):
    """Absolute local paths in event data must not survive export."""

    def test_export_strips_absolute_paths_from_imported_events(self):
        # A bundle carrying machine-local paths (e.g. recorded by a
        # producer before sanitization, or crafted) imports verbatim,
        # but re-export relativizes to known roots and reduces foreign
        # absolute paths to their basename.  Imported repos carry no
        # root, so this exercises the no-known-roots fallback too.
        store = Store(self.home / "partial.db")
        s = session_entry()
        ev = {**event_entry(s["id"]), "data": {
            "cwd": "/home/eve/secret/proj",
            "hook": {
                "transcript_path": "/home/eve/.claude/p/t.jsonl"},
            "tool_input": {"file_path": "/home/eve/secret/proj/a.py"},
            "changes": [{"path": "/home/eve/secret/proj/b.py",
                         "kind": "modified"}],
        }}
        store.import_bundle(bundle(
            repos=[repo_entry()], sessions=[s], events=[ev]))
        out = store.export_bundle()
        blob = json.dumps(out["events"])
        self.assertNotIn("/home/eve", blob)
        data = out["events"][0]["data"]
        self.assertEqual(data["cwd"], "proj")
        self.assertEqual(data["hook"]["transcript_path"], "t.jsonl")
        self.assertEqual(data["tool_input"]["file_path"], "a.py")
        self.assertEqual(
            data["changes"],
            [{"path": "b.py", "kind": "modified"}])


if __name__ == "__main__":
    unittest.main()
