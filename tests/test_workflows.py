import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from partial.models import Event
from partial.memory import Memory
from partial.store import Store
from partial.workflows import (_agent_env, _communicate,
                               _spawn_agent, run_workflow)

from helpers import RepoTestCase, commit, git

TS = "2026-01-01T00:00:00Z"


def ev(id_, sid, kind, ts=TS, text="", agent="devin", **kw):
    return Event(id=id_, session_id=sid, agent=agent, kind=kind,
                 timestamp=ts, text=text, **kw)


class FakeProvider:
    def __init__(self, response=None, error=None, configured=True):
        self.response = response
        self.error = error
        self.calls = []
        self._configured = configured

    @property
    def configured(self):
        return self._configured

    def complete(self, system, user):
        self.calls.append((system, user))
        if self.error:
            raise self.error
        return self.response(system, user) \
            if callable(self.response) else self.response

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


class WorkflowTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.home / "partial.db")
        self.rid = self.store.register_repo(self.repo)["id"]
        self.store.ingest(self.rid, [
            ev("e1", "s1", "session_start"),
            ev("e2", "s1", "prompt", text="implement helper"),
            ev("e3", "s1", "response", text="done"),
            ev("e4", "s1", "session_end"),
        ], worktree=str(self.repo))
        Memory(self.store).index(None)
        self.doc_ids = [r["id"] for r in
                        Memory(self.store).search("helper")]

    def _enable(self):
        self.store.set_memory_setting("external_ai_enabled", "true")

    def test_dry_run_planned_no_calls(self):
        p = FakeProvider()
        out = run_workflow(self.store, "ask", repo_id=self.rid,
                           query="what does helper do", run=False,
                           provider=p)
        self.assertEqual(out["status"], "planned")
        self.assertEqual(p.calls, [])
        self.assertTrue(out["source_ids"])
        run = self.store.get_run(out["id"])
        self.assertEqual(run["status"], "planned")

    def test_policy_disabled_rejects_run(self):
        out = None
        with self.assertRaises(ValueError):
            run_workflow(self.store, "ask", query="q", run=True,
                         provider=FakeProvider())
        runs = self.store.list_runs()
        self.assertEqual(runs[0]["status"], "error")

    def test_ask_completed(self):
        self._enable()
        p = FakeProvider(response=lambda s, u: {
            "answer": "it adds one",
            "citations": [self.doc_ids[0]],
            "uncertainties": []})
        out = run_workflow(self.store, "ask", query="helper",
                           run=True, provider=p)
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["report"]["answer"], "it adds one")
        self.assertEqual(len(p.calls), 1)
        self.assertIn("evidence", p.calls[0][1])

    def test_ask_invalid_citations_error(self):
        self._enable()
        p = FakeProvider(response={
            "answer": "x", "citations": ["bogus"],
            "uncertainties": []})
        out = run_workflow(self.store, "ask", query="q", run=True,
                           provider=p)
        self.assertEqual(out["status"], "error")

    def test_provider_failure_no_verdict(self):
        self._enable()
        p = FakeProvider(error=ValueError("provider down"))
        out = run_workflow(self.store, "ask", query="helper",
                           run=True, provider=p)
        self.assertEqual(out["status"], "error")
        self.assertIn("provider down", out["report"]["error"])

    def test_investigate_two_passes(self):
        self._enable()
        p = FakeProvider(response=lambda s, u: {
            "answer": "obs", "citations": [], "uncertainties": []})
        out = run_workflow(self.store, "investigate",
                           query="helper", run=True, provider=p)
        self.assertEqual(out["status"], "completed")
        # 2 investigate passes + 1 consolidation
        self.assertEqual(len(p.calls), 3)

    def test_review_provider_slots_and_judge(self):
        self._enable()
        finding = {"severity": "low", "title": "t",
                   "description": "d", "path": None, "line": None,
                   "citations": [self.doc_ids[0]]}
        calls = []

        def resp(system, user):
            calls.append(system[:20])
            return {"findings": [finding], "summary": "ok",
                    "uncertainties": []}
        p = FakeProvider(response=resp)
        out = run_workflow(self.store, "review", query="helper",
                           run=True, provider=p)
        self.assertEqual(out["status"], "completed")
        self.assertTrue(out["report"]["findings"])
        # 2 reviewer passes + judge
        self.assertEqual(len(p.calls), 3)

    def test_review_all_failed_is_error(self):
        self._enable()
        p = FakeProvider(error=ValueError("nope"))
        out = run_workflow(self.store, "review", query="helper",
                           run=True, provider=p)
        self.assertEqual(out["status"], "error")
        self.assertNotIn("findings", out["report"])

    def test_review_agent_timeout_partial(self):
        self._enable()

        def boom(agent, system, user):
            raise TimeoutError("agent timed out")
        p = FakeProvider(response=lambda s, u: {
            "findings": [], "summary": "s", "uncertainties": []})
        with mock.patch(
                "partial.workflows._agent_review", boom):
            out = run_workflow(self.store, "review", query="helper",
                               run=True,
                               agents=["codex"], provider=p)
        # codex failed but judge still ran with zero reports?
        # all reviewers failed -> error
        self.assertEqual(out["status"], "error")

    def test_review_agent_mixed_partial(self):
        self._enable()
        finding = {"severity": "low", "title": "t",
                   "description": "d", "path": None, "line": None,
                   "citations": [self.doc_ids[0]]}
        seen = []

        def fake_agent(agent, system, user):
            seen.append(agent)
            if agent == "codex":
                raise TimeoutError("timeout")
            return {"findings": [finding], "summary": "s",
                    "uncertainties": []}
        p = FakeProvider(response=lambda s, u: {
            "findings": [finding], "summary": "s",
            "uncertainties": []})
        with mock.patch("partial.workflows._agent_review",
                        fake_agent):
            out = run_workflow(
                self.store, "review", query="helper", run=True,
                agents=["codex", "claude"], provider=p)
        self.assertEqual(out["status"], "partial")
        self.assertTrue(out["report"]["findings"])
        self.assertTrue(any("codex" in u
                            for u in out["report"]["uncertainties"]))


    def test_unknown_agent_rejected_before_persist(self):
        self._enable()
        with self.assertRaises(ValueError):
            run_workflow(self.store, "review", query="helper",
                         run=True, agents=["gpt-4"],
                         provider=FakeProvider())
        self.assertEqual(self.store.list_runs(), [])

    def test_too_many_agents_rejected(self):
        self._enable()
        with self.assertRaises(ValueError):
            run_workflow(self.store, "review", query="helper",
                         run=True,
                         agents=["codex", "claude", "devin",
                                 "codex"],
                         provider=FakeProvider())
        self.assertEqual(self.store.list_runs(), [])

    def test_no_evidence_error_without_provider_calls(self):
        empty = Store(self.tmp / "empty.db")
        empty.set_memory_setting("external_ai_enabled", "true")
        p = FakeProvider()
        out = run_workflow(empty, "ask", query="anything",
                           run=True, provider=p)
        self.assertEqual(out["status"], "error")
        self.assertIn("insufficient evidence",
                      out["report"]["error"])
        self.assertEqual(p.calls, [])
        run = empty.get_run(out["id"])
        self.assertEqual(run["status"], "error")
        self.assertNotEqual(run["status"], "running")

    def test_citation_outside_packet_fails(self):
        self._enable()
        # A real document that the "helper" query cannot match, so
        # it exists in the store but never enters the packet.
        conn = self.store._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO memory_documents(id,repo_id,kind,"
                    "source_id,title,text,updated_at,archived)"
                    " VALUES('f' * 64,?,'session','x','x',"
                    "'unrelated zork text',?,0)",
                    (self.rid, TS))
                conn.execute(
                    "INSERT INTO memory_fts(id,repo_id,kind,title,"
                    "text) VALUES('f' * 64,?,'session','x',"
                    "'unrelated zork text')", (self.rid,))
                conn.commit()
        finally:
            conn.close()
        p = FakeProvider(response={
            "answer": "x", "citations": ["f" * 64],
            "uncertainties": []})
        out = run_workflow(self.store, "ask", query="helper",
                           run=True, provider=p)
        self.assertEqual(out["status"], "error")

    def test_explicit_evidence_ids_select_exact_docs(self):
        self._enable()
        picked = self.doc_ids[0]
        p = FakeProvider(response=lambda s, u: {
            "answer": "a", "citations": [picked],
            "uncertainties": []})
        out = run_workflow(self.store, "ask",
                           repo_id=self.rid, query="helper",
                           run=True, evidence_ids=[picked],
                           provider=p)
        self.assertEqual(out["status"], "completed")
        sent_ids = [d["id"] for d in p.calls[0][1]["evidence"]]
        self.assertIn(picked, sent_ids)
        self.assertIn(picked, out["source_ids"])

    def test_evidence_id_outside_repo_fails(self):
        self._enable()
        p = FakeProvider()
        with self.assertRaises(ValueError):
            run_workflow(self.store, "ask", repo_id="e" * 64,
                         query="helper", run=True,
                         evidence_ids=self.doc_ids[:1],
                         provider=p)
        self.assertEqual(p.calls, [])

    def test_dispatch_window_passthrough(self):
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
        Memory(self.store).index(None)
        p = FakeProvider(response=lambda s, u: {
            "answer": "recap", "citations": [],
            "uncertainties": []})
        out = run_workflow(
            self.store, "dispatch", repo_id=self.rid, run=True,
            provider=p, allow_external=True,
            since="2026-01-15", until="2026-02-15", branch="dev")
        self.assertEqual(out["status"], "completed")
        self.assertEqual(len(out["evidence"]), 1)
        self.assertEqual(out["evidence"][0]["source_id"], "2" * 32)

    def test_policy_never_mutated_as_side_effect(self):
        p = FakeProvider()
        with self.assertRaises(ValueError):
            run_workflow(self.store, "ask", query="helper",
                         run=True, provider=p)
        self.assertNotEqual(
            self.store.memory_setting("external_ai_enabled"),
            "true")
        self.assertEqual(p.calls, [])
        # Explicit opt-in works without the persistent policy.
        p2 = FakeProvider(response=lambda s, u: {
            "answer": "a", "citations": [], "uncertainties": []})
        out = run_workflow(self.store, "ask", query="helper",
                           run=True, provider=p2,
                           allow_external=True)
        self.assertEqual(out["status"], "completed")
        self.assertNotEqual(
            self.store.memory_setting("external_ai_enabled"),
            "true")

    def test_native_only_investigate_and_consolidation(self):
        self._enable()
        p = FakeProvider(configured=False)
        calls = []

        def fake_agent(agent, system, user):
            calls.append(agent)
            return {"answer": "obs",
                    "citations": [self.doc_ids[0]],
                    "uncertainties": []}
        with mock.patch("partial.workflows._agent_review",
                        fake_agent):
            out = run_workflow(
                self.store, "investigate", repo_id=self.rid,
                query="helper", run=True, agents=["codex"],
                provider=p)
        self.assertEqual(out["status"], "completed")
        # one investigation pass + one consolidation via codex
        self.assertEqual(calls, ["codex", "codex"])
        self.assertEqual(p.calls, [])

    def test_native_only_review_judge(self):
        self._enable()
        p = FakeProvider(configured=False)
        finding = {"severity": "low", "title": "t",
                   "description": "d", "path": None, "line": None,
                   "citations": [self.doc_ids[0]]}
        calls = []

        def fake_agent(agent, system, user):
            calls.append(agent)
            return {"findings": [finding], "summary": "s",
                    "uncertainties": []}
        with mock.patch("partial.workflows._agent_review",
                        fake_agent):
            out = run_workflow(
                self.store, "review", repo_id=self.rid,
                query="helper", run=True, agents=["claude"],
                provider=p)
        self.assertEqual(out["status"], "completed")
        # one reviewer pass + one judge pass via claude
        self.assertEqual(calls, ["claude", "claude"])
        self.assertEqual(p.calls, [])

    def test_native_mixed_failure_is_partial(self):
        self._enable()
        p = FakeProvider(configured=False)
        finding = {"severity": "low", "title": "t",
                   "description": "d", "path": None, "line": None,
                   "citations": [self.doc_ids[0]]}

        def fake_agent(agent, system, user):
            if agent == "codex":
                raise TimeoutError("codex timed out")
            return {"findings": [finding], "summary": "s",
                    "uncertainties": []}
        with mock.patch("partial.workflows._agent_review",
                        fake_agent):
            out = run_workflow(
                self.store, "review", repo_id=self.rid,
                query="helper", run=True,
                agents=["devin", "codex"], provider=p)
        self.assertEqual(out["status"], "partial")
        self.assertTrue(any("codex" in u for u in
                            out["report"]["uncertainties"]))


class AgentExecTests(unittest.TestCase):
    def _spawn(self, code, **kw):
        args = dict(stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True)
        args.update(kw)
        return subprocess.Popen(
            [sys.executable, "-c", code], **args)

    def test_nonzero_exit_rejected(self):
        proc = self._spawn("import sys; sys.exit(3)")
        with self.assertRaises(ValueError) as cm:
            _communicate("codex", proc)
        self.assertIn("exited with 3", str(cm.exception))

    def test_timeout_kills_process_group(self):
        with mock.patch("partial.workflows._AGENT_TIMEOUT", 0.3):
            proc = self._spawn(
                "import time; time.sleep(30)")
            t0 = time.monotonic()
            with self.assertRaises(TimeoutError):
                _communicate("claude", proc)
            self.assertLess(time.monotonic() - t0, 15)
            self.assertIsNotNone(proc.poll())

    def test_output_bounded_while_streaming(self):
        with mock.patch("partial.workflows._MAX_AGENT_OUT", 64):
            proc = self._spawn(
                "import sys; sys.stdout.write('x' * 10000)")
            with self.assertRaises(ValueError) as cm:
                _communicate("devin", proc)
            self.assertIn("exceeds", str(cm.exception))

    def test_stdin_prompt_delivered(self):
        proc = self._spawn(
            "import sys; d = sys.stdin.buffer.read();"
            " sys.stdout.write('got:%d' % len(d))",
            stdin=subprocess.PIPE)
        payload = b"q" * 200000
        out = _communicate("codex", proc, payload)
        self.assertEqual(out, b"got:200000")

    def test_agent_env_minimal_no_partial_or_secrets(self):
        env = {"PARTIAL_OPENAI_API_KEY": "sk-x",
               "PARTIAL_HOME": "/tmp/x",
               "AWS_SECRET_ACCESS_KEY": "aws",
               "CUSTOM_TOKEN": "tok",
               "OPENAI_API_KEY": "oai",
               "ANTHROPIC_API_KEY": "ant"}
        with mock.patch.dict(os.environ, env):
            claude = _agent_env("claude")
            codex = _agent_env("codex")
            devin = _agent_env("devin")
        for got in (claude, codex, devin):
            for key in got:
                self.assertFalse(key.startswith("PARTIAL_"))
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", got)
            self.assertNotIn("CUSTOM_TOKEN", got)
        # each agent keeps only its own auth variables
        self.assertEqual(claude.get("ANTHROPIC_API_KEY"), "ant")
        self.assertNotIn("OPENAI_API_KEY", claude)
        self.assertEqual(codex.get("OPENAI_API_KEY"), "oai")
        self.assertNotIn("ANTHROPIC_API_KEY", codex)
        self.assertNotIn("OPENAI_API_KEY", devin)
        self.assertNotIn("ANTHROPIC_API_KEY", devin)

    def test_fixed_argv_and_env(self):
        out_by_agent = {
            "codex": json.dumps({"item": {
                "type": "agent_message", "text": "hi"}}).encode(),
            "claude": json.dumps({"result": "hi"}).encode(),
            "devin": b"hi",
        }
        expected = {
            "codex": ["codex", "exec", "--sandbox", "read-only",
                      "--json", "-"],
            "claude": ["claude", "-p", "--output-format", "json",
                       "--tools", ""],
            "devin": ["devin", "--print", "--prompt-file"],
        }
        for agent, out in out_by_agent.items():
            with tempfile.TemporaryDirectory() as td, \
                    mock.patch(
                        "partial.workflows.subprocess.Popen"
                        ) as popen, \
                    mock.patch(
                        "partial.workflows._communicate",
                        return_value=out):
                text = _spawn_agent(agent, "prompt", td)
                self.assertEqual(text, "hi")
                argv = popen.call_args[0][0]
                want = expected[agent]
                self.assertEqual(argv[:len(want)], want)
                self.assertNotIn("--dangerously", " ".join(argv))
                self.assertNotIn("--skip", " ".join(argv))
                kw = popen.call_args[1]
                self.assertTrue(kw["start_new_session"])
                for key in kw["env"]:
                    self.assertFalse(key.startswith("PARTIAL_"))


if __name__ == "__main__":
    unittest.main()
