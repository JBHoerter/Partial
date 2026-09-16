import json
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from partial import brain_contract as bc
from partial.ai import OpenAIProvider, _valid_base


class FakeResponse:
    def __init__(self, body):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self._body = body

    def read(self, n=-1):
        if n is None or n < 0:
            return self._body
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, body=b"{}", error=None):
        self.body = body
        self.error = error
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        if self.error is not None:
            raise self.error
        return FakeResponse(self.body)


def emb_payload(vectors, order=None):
    order = list(range(len(vectors))) if order is None else order
    return {"data": [{"index": i, "embedding": vectors[i]}
                     for i in order]}


def chat_payload(obj):
    return {"choices": [{"message": {"content": json.dumps(obj)}}]}


class BaseUrlTests(unittest.TestCase):
    def test_valid_bases(self):
        for url in ("https://api.openai.com/v1",
                    "https://example.com:8443/v1/",
                    "http://127.0.0.1:8080",
                    "http://localhost:11434",
                    "http://[::1]:9000"):
            self.assertTrue(_valid_base(url).rstrip("/")
                            == url.rstrip("/"))

    def test_invalid_bases(self):
        for url in ("", "   ", None, 42,
                    "http://example.com",
                    "http://0.0.0.0:8080",
                    "http://[::]:8080",
                    "ftp://example.com",
                    "https://",
                    "https://user:pw@example.com",
                    "https://user@example.com",
                    "https://example.com/v1?x=1",
                    "https://example.com/v1#frag",
                    "https://example.com:0",
                    "https://example.com:99999",
                    "https://example.com:abc",
                    "not a url",
                    "x" * 600):
            with self.assertRaises(ValueError, msg=repr(url)):
                _valid_base(url)


class KeyTests(unittest.TestCase):
    def test_key_only_from_env_or_constructor(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(OpenAIProvider().configured)
        env = {"PARTIAL_OPENAI_API_KEY": "sk-env"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(OpenAIProvider().key, "sk-env")
            self.assertEqual(
                OpenAIProvider(api_key="sk-exp").key, "sk-exp")

    def test_no_request_when_unconfigured(self):
        op = FakeOpener()
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("partial.ai._OPENER", op):
            p = OpenAIProvider()
            with self.assertRaises(ValueError):
                p.complete("sys", {"q": 1})
            with self.assertRaises(ValueError):
                p.embed(["t"])
        self.assertEqual(op.requests, [])


class EmbedTests(unittest.TestCase):
    def _provider(self, body):
        self._op = FakeOpener(body)
        patcher = mock.patch("partial.ai._OPENER", self._op)
        patcher.start()
        self.addCleanup(patcher.stop)
        return OpenAIProvider(base_url="http://127.0.0.1:9",
                              api_key="sk-test")

    def test_reordered_indices_normalized(self):
        p = self._provider(
            emb_payload([[0.0, 1.0], [1.0, 0.0]], order=[1, 0]))
        out = p.embed(["a", "b"])
        self.assertEqual(out, [[0.0, 1.0], [1.0, 0.0]])
        req = self._op.requests[0]
        body = json.loads(req.data)
        self.assertEqual(body["model"], bc.EMBEDDING_MODEL)
        self.assertEqual(body["input"], ["a", "b"])
        self.assertEqual(req.get_header("Authorization"),
                         "Bearer sk-test")

    def test_input_bounds(self):
        p = self._provider(emb_payload([[1.0]]))
        with self.assertRaises(ValueError):
            p.embed([])
        with self.assertRaises(ValueError):
            p.embed(["x"] * 33)
        with self.assertRaises(ValueError):
            p.embed(["x" * 40001])
        with self.assertRaises(ValueError):
            p.embed("not-a-list")
        self.assertEqual(self._op.requests, [])

    def test_wrong_count(self):
        p = self._provider(emb_payload([[1.0]]))
        with self.assertRaises(ValueError):
            p.embed(["a", "b"])

    def test_duplicate_index(self):
        body = {"data": [{"index": 0, "embedding": [1.0]},
                         {"index": 0, "embedding": [0.5]}]}
        p = self._provider(body)
        with self.assertRaises(ValueError):
            p.embed(["a", "b"])

    def test_index_out_of_range(self):
        body = {"data": [{"index": 0, "embedding": [1.0]},
                         {"index": 2, "embedding": [0.5]}]}
        p = self._provider(body)
        with self.assertRaises(ValueError):
            p.embed(["a", "b"])

    def test_mismatched_dimensions(self):
        p = self._provider(emb_payload([[1.0, 0.0], [1.0]]))
        with self.assertRaises(ValueError):
            p.embed(["a", "b"])

    def test_non_finite_vector(self):
        p = self._provider(emb_payload([[float("nan"), 0.0]]))
        with self.assertRaises(ValueError):
            p.embed(["a"])

    def test_zero_vector(self):
        p = self._provider(emb_payload([[0.0, 0.0]]))
        with self.assertRaises(ValueError):
            p.embed(["a"])

    def test_non_numeric_vector(self):
        p = self._provider(emb_payload([["x", 0.0]]))
        with self.assertRaises(ValueError):
            p.embed(["a"])

    def test_response_not_object(self):
        p = self._provider(b"[1,2,3]")
        with self.assertRaises(ValueError):
            p.embed(["a"])


class CompleteTests(unittest.TestCase):
    def _provider(self, body):
        self._op = FakeOpener(body)
        patcher = mock.patch("partial.ai._OPENER", self._op)
        patcher.start()
        self.addCleanup(patcher.stop)
        return OpenAIProvider(base_url="http://127.0.0.1:9",
                              api_key="sk-test")

    def test_ok(self):
        p = self._provider(chat_payload({"answer": "a"}))
        self.assertEqual(p.complete("sys", {"q": 1}),
                         {"answer": "a"})
        body = json.loads(self._op.requests[0].data)
        self.assertEqual(body["model"], bc.ANSWER_MODEL)
        self.assertEqual(
            body["response_format"], {"type": "json_object"})

    def test_completion_must_be_object(self):
        p = self._provider(chat_payload([1, 2]))
        with self.assertRaises(ValueError):
            p.complete("s", {})

    def test_completion_content_not_string(self):
        p = self._provider({"choices": [{"message": {
            "content": {"x": 1}}}]})
        with self.assertRaises(ValueError):
            p.complete("s", {})

    def test_no_choices(self):
        p = self._provider({"choices": []})
        with self.assertRaises(ValueError):
            p.complete("s", {})

    def test_invalid_json(self):
        p = self._provider(b"not-json{")
        with self.assertRaises(ValueError):
            p.complete("s", {})


class TransportErrorTests(unittest.TestCase):
    def _provider(self, error):
        self._op = FakeOpener(error=error)
        patcher = mock.patch("partial.ai._OPENER", self._op)
        patcher.start()
        self.addCleanup(patcher.stop)
        return OpenAIProvider(base_url="http://127.0.0.1:9",
                              api_key="sk-secret-key")

    def test_http_error_drops_body_and_key(self):
        err = urllib.error.HTTPError(
            "http://127.0.0.1:9/x", 500, "fail", None, None)
        p = self._provider(err)
        with self.assertRaises(ValueError) as cm:
            p.complete("s", {})
        msg = str(cm.exception)
        self.assertIn("500", msg)
        self.assertNotIn("sk-secret-key", msg)
        self.assertNotIn("Bearer", msg)

    def test_url_error_generic(self):
        p = self._provider(urllib.error.URLError(
            "dial refused /full/path/sk-secret-key"))
        with self.assertRaises(ValueError) as cm:
            p.complete("s", {})
        self.assertEqual(str(cm.exception),
                         "AI provider unreachable")

    def test_timeout_generic(self):
        p = self._provider(TimeoutError("timed out"))
        with self.assertRaises(ValueError) as cm:
            p.complete("s", {})
        self.assertNotIn("sk-secret-key", str(cm.exception))

    def test_response_too_large(self):
        op = FakeOpener(b" " * 64)
        p = OpenAIProvider(base_url="http://127.0.0.1:9",
                           api_key="sk-test")
        with mock.patch("partial.ai._OPENER", op), \
                mock.patch("partial.ai._MAX_RESPONSE", 10):
            with self.assertRaises(ValueError) as cm:
                p.complete("s", {})
        self.assertIn("too large", str(cm.exception))


class _HitRecorder(BaseHTTPRequestHandler):
    hits = []
    redirect_to = None

    def do_POST(self):
        type(self).hits.append(dict(self.headers))
        if self.redirect_to:
            self.send_response(302)
            self.send_header("Location", self.redirect_to)
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(chat_payload(
                {"answer": "ok"})).encode())

    def log_message(self, *a):
        pass


def _serve(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


class RedirectTests(unittest.TestCase):
    def test_redirect_never_followed_auth_not_leaked(self):
        target = type("T", (_HitRecorder,), {"hits": [],
                                             "redirect_to": None})
        target_srv = _serve(target)
        self.addCleanup(target_srv.shutdown)
        port_b = target_srv.server_address[1]

        redir = type("R", (_HitRecorder,), {
            "hits": [],
            "redirect_to": f"http://127.0.0.1:{port_b}/stolen"})
        redir_srv = _serve(redir)
        self.addCleanup(redir_srv.shutdown)
        port_a = redir_srv.server_address[1]

        p = OpenAIProvider(
            base_url=f"http://127.0.0.1:{port_a}",
            api_key="sk-leak-me")
        with self.assertRaises(ValueError) as cm:
            p.complete("sys", {"q": 1})
        self.assertIn("302", str(cm.exception))
        # The redirect target was never contacted, so the
        # Authorization header cannot have leaked to it.
        self.assertEqual(target.hits, [])
        self.assertEqual(len(redir.hits), 1)
        self.assertEqual(redir.hits[0].get("Authorization"),
                         "Bearer sk-leak-me")


if __name__ == "__main__":
    unittest.main()
