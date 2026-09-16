import json
import subprocess
import sys
import unittest
from pathlib import Path

from partial.mcp import MAX_LINE
from partial.models import Event
from partial.memory import Memory
from partial.store import Store

from helpers import RepoTestCase, init_repo

TS = "2026-01-01T00:00:00Z"

ROOT = Path(__file__).parent.parent


def _spawn(home, cwd):
    proc = subprocess.Popen(
        [sys.executable, "-m", "partial",
         "--home", str(home), "mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=str(cwd),
        env={"PARTIAL_HOME": str(home),
             "PATH": "/usr/bin:/bin",
             "PYTHONPATH": str(ROOT)})
    return proc


class McpTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.home / "partial.db")
        self.rid = self.store.register_repo(self.repo)["id"]
        self.store.ingest(self.rid, [
            Event(id="e1", session_id="s1", agent="devin",
                  kind="prompt", timestamp=TS, text="do the thing"),
            Event(id="e2", session_id="s1", agent="devin",
                  kind="response", timestamp=TS, text="did the thing"),
        ], worktree=str(self.repo))
        Memory(self.store).index(None)
        self.proc = _spawn(self.home, self.repo)
        self.addCleanup(self._stop_proc)

    def _stop_proc(self):
        self.proc.kill()
        self.proc.wait(timeout=10)
        for f in (self.proc.stdin, self.proc.stdout,
                  self.proc.stderr):
            try:
                f.close()
            except Exception:
                pass

    def _rpc(self, method, params=None):
        self._send({"jsonrpc": "2.0", "id": self._next_id(),
                    "method": method, **({"params": params}
                                         if params is not None
                                         else {})})
        return json.loads(self.proc.stdout.readline())

    def _next_id(self):
        self._n = getattr(self, "_n", 0) + 1
        return self._n

    def _send(self, msg):
        self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
        self.proc.stdin.flush()

    def _send_raw(self, payload: bytes):
        self.proc.stdin.write(payload + b"\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def _call(self, name, arguments, **kw):
        params = {"name": name, "arguments": arguments}
        params.update(kw)
        return self._rpc("tools/call", params)

    def test_initialize(self):
        res = self._rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "clientInfo": {"name": "t", "version": "1"}})
        self.assertEqual(res["result"]["protocolVersion"],
                         "2025-03-26")
        self.assertEqual(res["result"]["serverInfo"]["name"],
                         "partial")
        self.assertIn("tools", res["result"]["capabilities"])

    def test_tools_list_and_search(self):
        res = self._rpc("tools/list")
        names = [t["name"] for t in res["result"]["tools"]]
        self.assertEqual(
            names, ["partial_search", "partial_context",
                    "partial_document", "partial_graph"])
        res = self._rpc("tools/call", {
            "name": "partial_search",
            "arguments": {"query": "thing"}})
        items = json.loads(
            res["result"]["content"][0]["text"])
        self.assertTrue(items)
        self.assertEqual(items[0]["kind"], "session")
        res = self._rpc("tools/call", {
            "name": "partial_document",
            "arguments": {"id": items[0]["id"]}})
        doc = json.loads(res["result"]["content"][0]["text"])
        self.assertEqual(doc["id"], items[0]["id"])

    def test_context_tool(self):
        res = self._rpc("tools/call", {
            "name": "partial_context",
            "arguments": {"query": "thing"}})
        ctx = json.loads(res["result"]["content"][0]["text"])
        self.assertEqual(ctx["mode"], "lexical")
        self.assertIn("documents", ctx)

    def test_scope_enforcement(self):
        # server is launched inside the repo, scoping to that repo
        res = self._rpc("tools/call", {
            "name": "partial_search",
            "arguments": {"query": "thing",
                          "repo_id": "0" * 64}})
        self.assertTrue(res["result"].get("isError"))

    def test_in_scope_repo_id_accepted(self):
        res = self._call("partial_search",
                         {"query": "thing", "repo_id": self.rid})
        self.assertNotIn("error", res)
        self.assertFalse(res["result"].get("isError"))
        items = json.loads(res["result"]["content"][0]["text"])
        self.assertTrue(items)
        for doc in items:
            self.assertEqual(doc["repo_id"], self.rid)

    def _add_second_repo(self):
        """Register + index a second repo in the SAME store."""
        repo2 = init_repo(self.tmp / "repo2")
        rid2 = self.store.register_repo(repo2)["id"]
        self.store.ingest(rid2, [
            Event(id="x1", session_id="s2", agent="devin",
                  kind="prompt", timestamp=TS,
                  text="otherrepo uniquephrase content"),
            Event(id="x2", session_id="s2", agent="devin",
                  kind="response", timestamp=TS,
                  text="otherrepo answered uniquephrase"),
        ], worktree=str(repo2))
        Memory(self.store).index(None)
        return rid2

    def test_cross_repo_document_rejected(self):
        rid2 = self._add_second_repo()
        mem = Memory(self.store)
        docs = mem.search("uniquephrase", repo_id=rid2)
        self.assertTrue(docs)
        self.assertEqual(docs[0]["repo_id"], rid2)
        # document exists in the same store but belongs to another repo
        self.assertIsNotNone(mem.document(docs[0]["id"]))
        res = self._call("partial_document",
                         {"id": docs[0]["id"]})
        self.assertTrue(res["result"].get("isError"))
        self.assertIn("scope",
                      res["result"]["content"][0]["text"])

    def test_cross_repo_search_scoped(self):
        self._add_second_repo()
        res = self._call("partial_search",
                         {"query": "uniquephrase"})
        items = json.loads(res["result"]["content"][0]["text"])
        for doc in items:
            self.assertEqual(doc["repo_id"], self.rid)
        res = self._call("partial_search",
                         {"query": "uniquephrase",
                          "repo_id": "0" * 64})
        self.assertTrue(res["result"].get("isError"))

    def test_cross_repo_graph_scoped(self):
        rid2 = self._add_second_repo()
        res = self._call("partial_graph",
                         {"query": "anything",
                          "repo_id": rid2})
        self.assertTrue(res["result"].get("isError"))

    def test_rejects_bad_arguments(self):
        cases = [
            # additionalProperties violations
            ("partial_search", {"query": "thing", "bogus": 1}),
            ("partial_context", {"query": "thing", "limit": 3}),
            ("partial_document", {"id": "f" * 64, "extra": 1}),
            ("partial_graph", {"query": "x", "nope": []}),
            # wrong types / missing required
            ("partial_search", {}),
            ("partial_search", {"query": 123}),
            ("partial_search", {"query": True}),
            ("partial_search", {"query": ["thing"]}),
            ("partial_context", {}),
            ("partial_document", {}),
            ("partial_document", {"id": 42}),
            ("partial_document", {"id": {"x": 1}}),
            ("partial_graph", {"symbol_id": {"x": 1}}),
            ("partial_graph", {"query": 7}),
            # bounds
            ("partial_search", {"query": "x" * 501}),
            ("partial_context", {"query": "x" * 501}),
            ("partial_document", {"id": "x" * 200}),
            ("partial_graph", {"query": "y" * 201}),
            ("partial_graph", {"symbol_id": "y" * 201}),
            ("partial_search", {"query": "thing", "limit": 0}),
            ("partial_search", {"query": "thing", "limit": 101}),
            ("partial_search", {"query": "thing", "limit": 1.5}),
            ("partial_search", {"query": "thing", "limit": True}),
            ("partial_search", {"query": "thing", "limit": "10"}),
            # kind enum
            ("partial_search", {"query": "thing", "kind": "bogus"}),
            ("partial_search", {"query": "thing", "kind": 5}),
            # repo_id format (64 lowercase hex only)
            ("partial_search", {"query": "thing", "repo_id": 5}),
            ("partial_search", {"query": "thing",
                                "repo_id": "0" * 63}),
            ("partial_search", {"query": "thing",
                                "repo_id": "0" * 65}),
            ("partial_search", {"query": "thing",
                                "repo_id": "G" * 64}),
            ("partial_search", {"query": "thing",
                                "repo_id": "A" * 64}),
            # out-of-scope repo_id (valid format, wrong repo)
            ("partial_search", {"query": "thing",
                                "repo_id": "0" * 64}),
            ("partial_context", {"query": "thing",
                                 "repo_id": "0" * 64}),
            # partial_graph requires query or symbol_id
            ("partial_graph", {}),
            ("partial_graph", {"repo_id": self.rid}),
            # missing/unknown tool name is a tool error, not a crash
            ("partial_exec", {"command": "id"}),
            ("nonexistent", {}),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                res = self._call(name, arguments)
                self.assertNotIn("error", res)
                self.assertTrue(res["result"].get("isError"))

    def test_tools_call_param_shapes(self):
        # non-object arguments / non-string name -> isError result
        for params in (
                {"name": "partial_search", "arguments": [1, 2]},
                {"name": "partial_search", "arguments": "x"},
                {"name": "partial_search", "arguments": 0},
                {"name": 5, "arguments": {}},
                {"name": ["partial_search"], "arguments": {}},
                {"arguments": {}},
                {}):
            with self.subTest(params=params):
                res = self._rpc("tools/call", params)
                self.assertNotIn("error", res)
                self.assertTrue(res["result"].get("isError"))
        # non-object params -> JSON-RPC -32602 invalid params
        for params in ([1, 2], "x", 5):
            with self.subTest(params=params):
                res = self._rpc("tools/call", params)
                self.assertEqual(res["error"]["code"], -32602)
        res = self._rpc("initialize", ["not", "an", "object"])
        self.assertEqual(res["error"]["code"], -32602)
        res = self._rpc("initialize", "x")
        self.assertEqual(res["error"]["code"], -32602)

    def test_invalid_jsonrpc_requests(self):
        cases = [
            (b'{"id":1,"method":"ping"}', -32600),
            (b'{"jsonrpc":"1.0","id":2,"method":"ping"}', -32600),
            (b'{"jsonrpc":"2.0","id":3}', -32600),
            (b'{"jsonrpc":"2.0","id":4,"method":5}', -32600),
            (b'{"jsonrpc":"2.0","id":4,"method":null}', -32600),
            (b'{"jsonrpc":"2.0","id":true,"method":"ping"}', -32600),
            (b'{"jsonrpc":"2.0","id":1.5,"method":"ping"}', -32600),
            (b'{"jsonrpc":"2.0","id":[1],"method":"ping"}', -32600),
            (b'{"jsonrpc":"2.0","id":{"x":1},"method":"ping"}',
             -32600),
            (b'null', -32600),
            (b'42', -32600),
            (b'"hello"', -32600),
            (b'[]', -32600),
            (b'[{"jsonrpc":"2.0","id":9,"method":"ping"}]', -32600),
            (b'not valid json', -32700),
            (b'{bad', -32700),
            (b'{"jsonrpc":"2.0","id":7,"method":"ping"', -32700),
        ]
        for payload, code in cases:
            with self.subTest(payload=payload):
                res = self._send_raw(payload)
                self.assertEqual(res["jsonrpc"], "2.0")
                self.assertEqual(res["error"]["code"], code)
                self.assertIsNone(res["id"])

    def test_notifications_produce_no_response(self):
        # notifications (no id) must never produce output, even for
        # unknown methods or tools/call; the ping after them proves
        # the next line on stdout is ping's own response.
        self._send({"jsonrpc": "2.0", "method": "bogus/method"})
        self._send({"jsonrpc": "2.0", "method": "tools/call",
                    "params": {"name": "partial_search",
                               "arguments": {"query": "thing"}}})
        self._send({"jsonrpc": "2.0", "method": "initialize"})
        self._send({"jsonrpc": "2.0", "id": 77, "method": "ping"})
        res = json.loads(self.proc.stdout.readline())
        self.assertEqual(res, {"jsonrpc": "2.0", "id": 77,
                               "result": {}})

    def test_valid_id_forms(self):
        res = self._send_raw(
            b'{"jsonrpc":"2.0","id":"abc","method":"ping"}')
        self.assertEqual(res, {"jsonrpc": "2.0", "id": "abc",
                               "result": {}})
        res = self._send_raw(
            b'{"jsonrpc":"2.0","id":0,"method":"ping"}')
        self.assertEqual(res["id"], 0)

    def test_oversized_line(self):
        # >1 MiB request: rejected -32600, drained, server stays alive
        big = (b'{"jsonrpc":"2.0","id":1,"method":"ping","pad":"'
               + b'x' * MAX_LINE + b'"}')
        self.proc.stdin.write(big + b"\n")
        self._send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        res = json.loads(self.proc.stdout.readline())
        self.assertEqual(res["error"]["code"], -32600)
        res = json.loads(self.proc.stdout.readline())
        self.assertEqual(res, {"jsonrpc": "2.0", "id": 2,
                               "result": {}})

    def test_oversized_line_multi_chunk_drain(self):
        # ~2 MiB forces several bounded reads before the newline;
        # a valid request on the same write must still be answered.
        self.proc.stdin.write(b"z" * (2 * MAX_LINE + 123) + b"\n")
        self._send({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        res = json.loads(self.proc.stdout.readline())
        self.assertEqual(res["error"]["code"], -32600)
        res = json.loads(self.proc.stdout.readline())
        self.assertEqual(res, {"jsonrpc": "2.0", "id": 3,
                               "result": {}})

    def test_document_not_found(self):
        res = self._call("partial_document", {"id": "f" * 64})
        self.assertTrue(res["result"].get("isError"))
        self.assertIn("not found",
                      res["result"]["content"][0]["text"])

    def test_read_only_tool_surface(self):
        res = self._rpc("tools/list")
        tools = res["result"]["tools"]
        self.assertEqual(
            {t["name"] for t in tools},
            {"partial_search", "partial_context",
             "partial_document", "partial_graph"})
        for t in tools:
            self.assertEqual(
                t["inputSchema"]["type"], "object")
            self.assertIs(
                t["inputSchema"].get("additionalProperties"),
                False)
        for name in ("partial_exec", "partial_write",
                     "partial_delete", "shell", "exec", "run"):
            res = self._call(name, {})
            self.assertTrue(res["result"].get("isError"), name)

    def test_unknown_method_and_batch(self):
        res = self._rpc("nonexistent/method")
        self.assertEqual(res["error"]["code"], -32601)
        self.proc.stdin.write(
            b'[{"jsonrpc":"2.0","id":9,"method":"ping"}]\n')
        self.proc.stdin.flush()
        res = json.loads(self.proc.stdout.readline())
        self.assertEqual(res["error"]["code"], -32600)

    def test_no_arbitrary_execution(self):
        res = self._rpc("tools/call", {
            "name": "partial_search",
            "arguments": {"query": "thing; touch /tmp/pwned"}})
        self.assertFalse(Path("/tmp/pwned").exists())
        res = self._rpc("tools/call", {
            "name": "partial_exec",
            "arguments": {"command": "id"}})
        self.assertTrue(res["result"].get("isError"))

    def test_mcp_works_outside_checkout(self):
        proc = _spawn(self.home, "/tmp")

        def _stop():
            proc.kill()
            proc.wait(timeout=10)
            for f in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    f.close()
                except Exception:
                    pass

        self.addCleanup(_stop)
        proc.stdin.write(
            b'{"jsonrpc":"2.0","id":1,"method":"initialize",'
            b'"params":{"protocolVersion":"2024-11-05"}}\n')
        proc.stdin.flush()
        res = json.loads(proc.stdout.readline())
        self.assertEqual(res["result"]["serverInfo"]["name"],
                         "partial")


if __name__ == "__main__":
    unittest.main()
