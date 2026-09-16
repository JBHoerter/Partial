import json
import unittest

from partial.adapters import codex_event, normalize_hook, parse_import
from partial.privacy import is_sensitive_path, redact


class DevinHookTests(unittest.TestCase):
    def test_full_lifecycle_with_parent_and_tools(self):
        start = normalize_hook("devin", {
            "hook_event_name": "SessionStart",
            "session_id": "s1",
            "parent_session_id": "s0",
            "model": "devin-model",
            "timestamp": "2026-01-01T00:00:00Z",
        })
        self.assertEqual(len(start), 1)
        self.assertEqual(start[0].kind, "session_start")
        self.assertEqual(start[0].parent_session_id, "s0")
        self.assertEqual(start[0].model, "devin-model")

        prompt = normalize_hook("devin", {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s1",
            "prompt_id": "p1",
            "prompt": "fix the flaky test",
        })
        self.assertEqual(prompt[0].kind, "prompt")
        self.assertEqual(prompt[0].text, "fix the flaky test")
        self.assertEqual(prompt[0].id, "p1")

        tool = normalize_hook("devin", {
            "hook_event_name": "PostToolUse",
            "session_id": "s1",
            "tool_name": "edit_file",
            "tool_input": {"file_path": "src/a.py"},
            "tool_response": {"success": True, "output": "ok", "error": ""},
        })
        self.assertEqual(tool[0].kind, "tool")
        self.assertEqual(tool[0].tool_name, "edit_file")
        self.assertEqual(
            tool[0].data["tool_response"]["output"], "ok")

        stop = normalize_hook("devin", {
            "hook_event_name": "Stop",
            "session_id": "s1",
            "last_assistant_message": "all done",
        })
        self.assertEqual(stop[0].kind, "response")
        self.assertEqual(stop[0].text, "all done")

        end = normalize_hook("devin", {
            "hook_event_name": "SessionEnd", "session_id": "s1"})
        self.assertEqual(end[0].kind, "session_end")

        comp = normalize_hook("devin", {
            "hook_event_name": "PostCompaction", "session_id": "s1"})
        self.assertEqual(comp[0].kind, "compaction")

    def test_stop_without_message_has_empty_text(self):
        ev = normalize_hook("devin", {
            "hook_event_name": "Stop", "session_id": "s1"})
        self.assertEqual(ev[0].kind, "response")
        self.assertEqual(ev[0].text, "")

    def test_event_name_override_and_missing_session(self):
        ev = normalize_hook("devin", {"session_id": "s1"}, "Stop")
        self.assertEqual(ev[0].kind, "response")
        with self.assertRaises(ValueError):
            normalize_hook("devin", {"hook_event_name": "Stop"})
        with self.assertRaises(ValueError):
            normalize_hook("devin", {
                "session_id": "s", "hook_event_name": "Bogus"})

    def test_unknown_hook_agents(self):
        for agent in ("codex", "chatgpt", "bogus"):
            with self.assertRaises(ValueError):
                normalize_hook(agent, {"session_id": "s",
                                       "hook_event_name": "Stop"})


class ClaudeImportTests(unittest.TestCase):
    def test_text_and_tool_blocks(self):
        lines = [
            {"type": "user", "sessionId": "cs", "uuid": "u1",
             "timestamp": "2026-01-01T00:00:00Z",
             "message": {"role": "user",
                         "content": [{"type": "text", "text": "hi"}]}},
            {"type": "assistant", "sessionId": "cs", "uuid": "u2",
             "timestamp": "2026-01-01T00:00:01Z",
             "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "working on it"},
                 {"type": "tool_use", "name": "Edit",
                  "input": {"file_path": "a.py"}}]}},
            {"type": "user", "sessionId": "cs", "uuid": "u3",
             "timestamp": "2026-01-01T00:00:02Z",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "content": "done"}]}},
        ]
        content = "\n".join(json.dumps(x) for x in lines)
        evs = parse_import("claude", content)
        kinds = [e.kind for e in evs]
        self.assertEqual(kinds, ["prompt", "response", "tool", "tool"])
        self.assertEqual(evs[2].tool_name, "Edit")
        self.assertEqual(evs[3].data["tool_response"]["content"], "done")
        again = parse_import("claude", content)
        self.assertEqual([e.id for e in evs], [e.id for e in again])

    def test_truncated_jsonl_is_actionable(self):
        content = '{"type":"user","message":{"role":"user","content":[]}}\n' \
                  '{"type":"assistant","message":{"role":"a'
        with self.assertRaises(ValueError) as ctx:
            parse_import("claude", content)
        self.assertIn("line 2", str(ctx.exception))


class CodexImportTests(unittest.TestCase):
    FIXTURE = [
        {"type": "thread.started", "thread_id": "thr_1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {
            "id": "i1", "type": "agent_message", "text": "on it"}},
        {"type": "item.completed", "item": {
            "id": "i2", "type": "command_execution",
            "command": "ls -la", "aggregated_output": "files",
            "exit_code": 0}},
        {"type": "item.completed", "item": {
            "id": "i3", "type": "file_change",
            "changes": [{"path": "a.py", "kind": "modified"}]}},
        {"type": "item.completed", "item": {
            "id": "i4", "type": "reasoning", "text": "secret chain"}},
        {"type": "turn.completed", "usage": {
            "input_tokens": 10, "output_tokens": 4}},
    ]

    def test_exec_events_and_duplicate_items(self):
        content = "\n".join(json.dumps(x) for x in self.FIXTURE)
        dup = content + "\n" + json.dumps(self.FIXTURE[2])
        evs = parse_import("codex", dup)
        kinds = [e.kind for e in evs]
        self.assertIn("session_start", kinds)
        self.assertIn("response", kinds)
        self.assertIn("usage", kinds)
        self.assertNotIn("secret chain", json.dumps(
            [e.to_dict() for e in evs]))
        items = [e for e in evs if e.text == "on it"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].id, items[1].id)
        again = parse_import("codex", dup)
        self.assertEqual([e.id for e in evs], [e.id for e in again])
        self.assertTrue(all(e.session_id == "thr_1" for e in evs))
        tool = [e for e in evs if e.tool_name == "file_change"][0]
        self.assertEqual(
            tool.data["changes"], [{"path": "a.py", "kind": "modified"}])
        usage = [e for e in evs if e.kind == "usage"][0]
        self.assertEqual(usage.data["usage"]["input_tokens"], 10)

    def test_usage_distinct_across_turns(self):
        lines = [
            {"type": "thread.started", "thread_id": "tt"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {
                "id": "item_1", "type": "agent_message", "text": "a"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1}},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {
                "id": "item_1", "type": "agent_message", "text": "b"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1}},
        ]
        evs = parse_import(
            "codex", "\n".join(json.dumps(x) for x in lines))
        usage = [e.id for e in evs if e.kind == "usage"]
        self.assertEqual(len(usage), 2)
        self.assertNotEqual(usage[0], usage[1])
        texts = [e.text for e in evs if e.kind == "response"]
        self.assertEqual(texts, ["a", "b"])
        self.assertNotEqual(
            *[e.id for e in evs if e.kind == "response"])

    def test_rollout_formats(self):
        lines = [
            {"type": "event_msg", "payload": {
                "type": "user_message", "message": "hello codex"}},
            {"type": "event_msg", "payload": {
                "type": "agent_message", "message": "hi human"}},
            {"type": "response_item", "payload": {
                "type": "function_call", "name": "shell",
                "arguments": "{\"cmd\": \"ls\"}"}},
            {"type": "response_item", "payload": {
                "type": "function_call_output", "output": "ok"}},
            {"type": "response_item", "payload": {
                "type": "reasoning", "summary": "hidden"}},
        ]
        evs = parse_import("codex", "\n".join(json.dumps(x) for x in lines))
        kinds = [e.kind for e in evs]
        self.assertEqual(kinds, ["prompt", "response", "tool", "tool"])
        self.assertEqual(evs[2].tool_name, "shell")
        self.assertEqual(
            evs[2].data["tool_input"], {"cmd": "ls"})
        self.assertNotIn("hidden", json.dumps([e.to_dict() for e in evs]))

    def test_codex_event_direct(self):
        evs = codex_event({"type": "turn.started"}, "t1")
        self.assertEqual(evs, [])
        evs = codex_event({"type": "error", "message": "boom"}, "t1")
        self.assertEqual(evs[0].kind, "error")


class DevinImportTests(unittest.TestCase):
    def test_atif_steps(self):
        atif = {
            "session_id": "dev-1", "agent": "devin",
            "steps": [
                {"source": "user", "message": "build a CLI",
                 "timestamp": "2026-01-01T00:00:00Z"},
                {"source": "agent", "message": "sure",
                 "tool_calls": [{"name": "run", "arguments": {"c": "ls"}}],
                 "observation": "listed",
                 "timestamp": "2026-01-01T00:00:01Z"},
            ],
        }
        evs = parse_import("devin", json.dumps(atif))
        kinds = [e.kind for e in evs]
        self.assertEqual(kinds, ["prompt", "response", "tool", "tool"])
        self.assertTrue(all(e.session_id == "dev-1" for e in evs))
        again = parse_import("devin", json.dumps(atif))
        self.assertEqual([e.id for e in evs], [e.id for e in again])

    def test_devin_hook_jsonl(self):
        lines = [
            {"hook_event_name": "SessionStart", "session_id": "d1"},
            {"hook_event_name": "UserPromptSubmit", "session_id": "d1",
             "prompt": "go"},
        ]
        evs = parse_import(
            "devin", "\n".join(json.dumps(x) for x in lines))
        self.assertEqual([e.kind for e in evs],
                         ["session_start", "prompt"])

    def test_single_json_hook_import_deterministic(self):
        payload = json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "session_id": "d1", "prompt": "once"})
        a = parse_import("devin", payload)
        b = parse_import("devin", payload)
        self.assertEqual([e.id for e in a], [e.id for e in b])
        cpayload = json.dumps({
            "hook_event_name": "Stop", "session_id": "c1",
            "last_assistant_message": "fin"})
        ca = parse_import("claude", cpayload)
        cb = parse_import("claude", cpayload)
        self.assertEqual([e.id for e in ca], [e.id for e in cb])


class ChatGptImportTests(unittest.TestCase):
    def _conv(self, current=None):
        return {
            "id": "conv1", "title": "chat",
            "current_node": current,
            "mapping": {
                "root": {"parent": None, "children": ["n1"]},
                "n2": {"parent": "n1", "children": ["n3"], "message": {
                    "id": "m2", "author": {"role": "assistant"},
                    "content": {"parts": ["answer"]},
                    "create_time": 1700000002}},
                "n1": {"parent": "root", "children": ["n2"], "message": {
                    "id": "m1", "author": {"role": "user"},
                    "content": {"parts": ["question"]},
                    "create_time": 1700000001}},
                "n3": {"parent": "n2", "children": [], "message": {
                    "id": "m3", "author": {"role": "user"},
                    "content": {"parts": ["followup", {"skip": "me"}]},
                    "create_time": 1700000003}},
            },
        }

    def test_active_branch_order(self):
        conv = self._conv(current="n3")
        evs = parse_import("chatgpt", json.dumps([conv]))
        self.assertEqual([e.text for e in evs],
                         ["question", "answer", "followup"])
        self.assertEqual(
            [e.kind for e in evs], ["prompt", "response", "prompt"])

    def test_no_current_node_topological(self):
        conv = self._conv(current=None)
        evs = parse_import("chatgpt", json.dumps([conv]))
        self.assertEqual([e.text for e in evs],
                         ["question", "answer", "followup"])


class CanonicalImportTests(unittest.TestCase):
    def test_canonical_events(self):
        bundle = {"events": [{
            "id": "e1", "session_id": "s1", "agent": "claude",
            "kind": "prompt", "timestamp": "2026-01-01T00:00:00Z",
            "text": "hi"}]}
        evs = parse_import("claude", json.dumps(bundle))
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].text, "hi")
        bad = {"events": [{"id": "e", "session_id": "s", "agent": "nope",
                           "kind": "prompt",
                           "timestamp": "2026-01-01T00:00:00Z"}]}
        with self.assertRaises(ValueError):
            parse_import("claude", json.dumps(bad))

    def test_invalid_agent_and_size(self):
        with self.assertRaises(ValueError):
            parse_import("bogus", "{}")
        with self.assertRaises(ValueError):
            parse_import("chatgpt", "x" * (65 * 1024 * 1024))


class PrivacyTests(unittest.TestCase):
    def test_nested_keys_and_free_text(self):
        obj = {
            "config": {"Authorization": "Bearer sk-abc123xyz"},
            "headers": [{"Cookie": "session=abc"}],
            "note": "token is ghp_0123456789abcdefghij and "
                    "github_pat_0123456789abcdef_zz",
            "aws": "key AKIAIOSFODNN7EXAMPLE here",
            "assign": "PASSWORD=hunter2",
            "pem": "-----BEGIN PRIVATE KEY-----\nabc\n"
                   "-----END PRIVATE KEY-----",
            "usage": {"input_tokens": 42, "output_tokens": 7},
        }
        out = redact(obj)
        self.assertEqual(out["config"]["Authorization"], "[REDACTED]")
        self.assertEqual(out["headers"][0]["Cookie"], "[REDACTED]")
        self.assertNotIn("ghp_0123456789", out["note"])
        self.assertNotIn("github_pat_0123456789", out["note"])
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out["aws"])
        self.assertNotIn("hunter2", out["assign"])
        self.assertNotIn("abc", out["pem"])
        self.assertEqual(out["usage"], {"input_tokens": 42,
                                        "output_tokens": 7})
        self.assertNotIn("sk-abc123xyz", json.dumps(out))

    def test_sensitive_paths(self):
        for p in (".env", ".env.local", ".env.example",
                  "config/credentials.json", "cert.pem", "ssh.key",
                  ".git/config", "repo/.git/hooks/x"):
            self.assertTrue(is_sensitive_path(p), p)
        for p in ("src/main.py", "env.txt", "keys.md"):
            self.assertFalse(is_sensitive_path(p), p)


if __name__ == "__main__":
    unittest.main()
