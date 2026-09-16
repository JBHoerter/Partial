from __future__ import annotations

import ipaddress
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import brain_contract as bc

DEFAULT_BASE = "https://api.openai.com/v1"
_MAX_RESPONSE = 8 * 1024 * 1024
_TIMEOUT = 60


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect so the Authorization header can never
    be forwarded to a different origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Proxies are disabled: environment proxy settings must never see
# the Authorization header (plaintext loopback requests would send
# it to the proxy in clear).
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirect)


def _valid_base(url: str) -> str:
    if not isinstance(url, str):
        raise ValueError("invalid AI base URL")
    url = url.strip()
    if not url or len(url) > 500:
        raise ValueError("invalid AI base URL")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid AI base URL") from exc
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("AI base URL must not contain userinfo,"
                         " query, or fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid AI base URL port")
    if parts.scheme == "https":
        if not host:
            raise ValueError("AI base URL must have a host")
    elif parts.scheme == "http":
        loopback = host == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(
                host).is_loopback
        except ValueError:
            pass
        if not loopback:
            raise ValueError(
                "http AI base URL allowed only for loopback hosts")
    else:
        raise ValueError("AI base URL must use https")
    return url.rstrip("/")


class OpenAIProvider:
    def __init__(self, *, base_url: str | None = None,
                 api_key: str | None = None):
        self.key = api_key if api_key is not None else \
            os.environ.get("PARTIAL_OPENAI_API_KEY") or None
        base = base_url or os.environ.get("PARTIAL_AI_BASE_URL") \
            or DEFAULT_BASE
        self.base = _valid_base(base)

    @property
    def configured(self) -> bool:
        return bool(self.key)

    def _post(self, path: str, payload: dict) -> dict:
        if not self.configured:
            raise ValueError(
                "AI provider not configured; set"
                " PARTIAL_OPENAI_API_KEY to enable paid requests")
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.key}",
            },
            method="POST")
        try:
            with _OPENER.open(req, timeout=_TIMEOUT) as res:
                data = res.read(_MAX_RESPONSE + 1)
        except urllib.error.HTTPError as exc:
            # Denied redirects surface here; the body is dropped so
            # provider text (or secrets) cannot leak into errors.
            raise ValueError(
                f"AI provider returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ValueError("AI provider unreachable") from exc
        except OSError as exc:
            raise ValueError("AI provider request failed") from exc
        if len(data) > _MAX_RESPONSE:
            raise ValueError("AI provider response too large")
        try:
            out = json.loads(data)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("AI provider returned invalid JSON") \
                from exc
        if not isinstance(out, dict):
            raise ValueError(
                "AI provider response must be a JSON object")
        return out

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not isinstance(texts, list) or not texts \
                or len(texts) > 32:
            raise ValueError("embed requires 1..32 texts")
        if any(not isinstance(t, str) or len(t) > 40000
               for t in texts):
            raise ValueError("invalid embedding input text")
        out = self._post("/embeddings", {
            "model": bc.EMBEDDING_MODEL, "input": texts})
        data = out.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise ValueError("embedding response has wrong size")
        by_index: dict[int, list] = {}
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("invalid embedding item")
            idx = item.get("index")
            if type(idx) is not int or not 0 <= idx < len(texts) \
                    or idx in by_index:
                raise ValueError("embedding response index invalid")
            vec = item.get("embedding")
            if not isinstance(vec, list) or not vec \
                    or len(vec) > 8192:
                raise ValueError("invalid embedding vector")
            by_index[idx] = vec
        if set(by_index) != set(range(len(texts))):
            raise ValueError("embedding response missing items")
        vectors = [by_index[i] for i in range(len(texts))]
        dim = len(vectors[0])
        for vec in vectors:
            if len(vec) != dim:
                raise ValueError("embedding dimensions do not match")
            bc.cosine(vec, vec)
        return vectors

    def complete(self, system: str, user: dict) -> dict:
        out = self._post("/chat/completions", {
            "model": bc.ANSWER_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user)},
            ],
            "response_format": {"type": "json_object"},
        })
        try:
            content = out["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("AI provider returned no completion") \
                from exc
        if not isinstance(content, str):
            raise ValueError("AI provider returned invalid JSON")
        try:
            obj = json.loads(content)
        except ValueError as exc:
            raise ValueError("AI provider returned invalid JSON") \
                from exc
        if not isinstance(obj, dict):
            raise ValueError("AI completion must be a JSON object")
        return obj
