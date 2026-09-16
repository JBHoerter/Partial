from __future__ import annotations

import json
import re
import sys

from . import __version__
from .memory import DOC_KINDS, Memory
from .store import Store

PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
MAX_LINE = 1024 * 1024
MAX_QUERY = 500
MAX_GRAPH_QUERY = 200
MAX_DOC_ID = 128
MAX_SYMBOL_ID = 200
_ID64 = re.compile(r"[0-9a-f]{64}")
_REPO_ID_PATTERN = r"^[0-9a-f]{64}$"

# Read-only tool surface: search/context/document/graph only. There are
# intentionally no shell, write, or admin tools.
TOOLS = [
    {"name": "partial_search",
     "description": "Search recorded repository context.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "query": {"type": "string", "maxLength": MAX_QUERY},
             "repo_id": {"type": "string",
                         "pattern": _REPO_ID_PATTERN},
             "kind": {"type": "string", "enum": list(DOC_KINDS)},
             "limit": {"type": "integer", "minimum": 1,
                       "maximum": 100},
         },
         "required": ["query"],
         "additionalProperties": False}},
    {"name": "partial_context",
     "description": "Retrieve bounded evidence for a repository"
                    " question.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "query": {"type": "string", "maxLength": MAX_QUERY},
             "repo_id": {"type": "string",
                         "pattern": _REPO_ID_PATTERN}},
         "required": ["query"],
         "additionalProperties": False}},
    {"name": "partial_document",
     "description": "Read an indexed evidence document.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "id": {"type": "string", "maxLength": MAX_DOC_ID}},
         "required": ["id"],
         "additionalProperties": False}},
    {"name": "partial_graph",
     "description": "Inspect indexed code relationships.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "query": {"type": "string",
                       "maxLength": MAX_GRAPH_QUERY},
             "symbol_id": {"type": "string",
                           "maxLength": MAX_SYMBOL_ID},
             "repo_id": {"type": "string",
                         "pattern": _REPO_ID_PATTERN}},
         "additionalProperties": False}},
]

_TOOL_KEYS = {t["name"]: set(t["inputSchema"]["properties"])
              for t in TOOLS}


class _InvalidParams(ValueError):
    """Protocol-level invalid params -> JSON-RPC -32602."""


def _text_result(payload) -> dict:
    return {"content": [{"type": "text",
                         "text": json.dumps(payload)}]}


def _err_result(message: str) -> dict:
    return {"content": [{"type": "text",
                         "text": message}], "isError": True}


def _arg_str(args: dict, key: str, max_len: int, *,
             required=False) -> str | None:
    v = args.get(key)
    if v is None:
        if required:
            raise ValueError(f"{key} is required")
        return None
    if not isinstance(v, str) or len(v) > max_len:
        raise ValueError(f"{key} must be a string <={max_len} chars")
    return v


def serve_mcp(store: Store, repo_id: str | None = None) -> None:
    """Line-delimited JSON-RPC 2.0 loop over stdio.

    Only JSON-RPC responses are ever written to stdout; diagnostics go
    to stderr. ``repo_id`` pins the server to one repository: tools
    receive it implicitly and requests for other repos are rejected.
    """
    if repo_id is not None and not _ID64.fullmatch(repo_id):
        raise ValueError("invalid server repo_id scope")
    mem = Memory(store)

    def scope(value):
        if value is None:
            return repo_id
        if not isinstance(value, str) or not _ID64.fullmatch(value):
            raise ValueError(
                "repo_id must be a 64-char lowercase hex string")
        if repo_id is not None and value != repo_id:
            raise ValueError("repo_id outside server scope")
        return value

    def check_args(name: str, args: dict) -> None:
        extra = set(args) - _TOOL_KEYS[name]
        if extra:
            raise ValueError(
                f"unknown arguments: {', '.join(sorted(extra))}")

    def call(name: str, args):
        if name not in _TOOL_KEYS:
            raise ValueError(f"unknown tool: {name}")
        check_args(name, args)
        if name == "partial_search":
            q = _arg_str(args, "query", MAX_QUERY, required=True)
            lim = args.get("limit", 12)
            if type(lim) is not int or not 1 <= lim <= 100:
                raise ValueError("limit must be an integer 1..100")
            kind = args.get("kind")
            if kind is not None and kind not in DOC_KINDS:
                raise ValueError("invalid kind")
            return _text_result(mem.search(
                q, repo_id=scope(args.get("repo_id")),
                kind=kind, limit=lim))
        if name == "partial_context":
            return _text_result(mem.context(
                _arg_str(args, "query", MAX_QUERY, required=True),
                repo_id=scope(args.get("repo_id"))))
        if name == "partial_document":
            did = _arg_str(args, "id", MAX_DOC_ID, required=True)
            doc = mem.document(did)
            if doc is None:
                raise ValueError("document not found")
            if repo_id is not None \
                    and doc.get("repo_id") != repo_id:
                raise ValueError("document outside server scope")
            return _text_result(doc)
        if name == "partial_graph":
            rid = scope(args.get("repo_id"))
            sym = _arg_str(args, "symbol_id", MAX_SYMBOL_ID)
            q = _arg_str(args, "query", MAX_GRAPH_QUERY)
            if sym:
                return _text_result(mem.graph_neighbors(
                    sym, repo_id=rid))
            if q:
                return _text_result(mem.graph_search(
                    q, repo_id=rid))
            raise ValueError("partial_graph requires query or"
                             " symbol_id")
        raise ValueError(f"unknown tool: {name}")

    def params_obj(msg) -> dict:
        params = msg.get("params")
        if params is None:
            return {}
        if not isinstance(params, dict):
            raise _InvalidParams("params must be an object")
        return params

    def respond(obj) -> None:
        print(json.dumps(obj), flush=True)

    def error(mid, code: int, message: str) -> None:
        respond({"jsonrpc": "2.0", "id": mid,
                 "error": {"code": code, "message": message}})

    try:
        while True:
            line = sys.stdin.buffer.readline(MAX_LINE + 2)
            if not line:
                break
            if len(line) > MAX_LINE:
                # Drain the oversize line in bounded chunks so memory
                # stays flat no matter how long the input is.
                while line and not line.endswith(b"\n"):
                    line = sys.stdin.buffer.readline(MAX_LINE + 2)
                error(None, -32600, "request too large")
                continue
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                error(None, -32700, "parse error")
                continue
            if isinstance(msg, list):
                error(None, -32600, "batch requests unsupported")
                continue
            if not isinstance(msg, dict) \
                    or msg.get("jsonrpc") != "2.0" \
                    or not isinstance(msg.get("method"), str):
                error(None, -32600, "invalid request")
                continue
            mid = msg.get("id")
            if mid is not None and type(mid) not in (str, int):
                error(None, -32600, "invalid request id")
                continue
            method = msg["method"]
            if mid is None:  # notification: never a response
                continue
            try:
                if method == "initialize":
                    proto = params_obj(msg).get("protocolVersion")
                    result = {
                        "protocolVersion":
                            proto if proto in PROTOCOLS
                            else PROTOCOLS[-1],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "partial",
                                       "version": __version__}}
                elif method == "ping":
                    result = {}
                elif method == "tools/list":
                    result = {"tools": TOOLS}
                elif method == "tools/call":
                    params = params_obj(msg)
                    name = params.get("name")
                    args = params.get("arguments")
                    if args is None:
                        args = {}
                    if not isinstance(name, str) \
                            or not isinstance(args, dict):
                        raise ValueError(
                            "tools/call requires a string name and"
                            " object arguments")
                    result = call(name, args)
                else:
                    error(mid, -32601, "method not found")
                    continue
            except _InvalidParams as exc:
                error(mid, -32602, str(exc))
                continue
            except ValueError as exc:
                result = _err_result(str(exc))
            except Exception as exc:
                print(f"partial mcp: internal error: {exc!r}",
                      file=sys.stderr, flush=True)
                result = _err_result("internal error")
            respond({"jsonrpc": "2.0", "id": mid,
                     "result": result})
    except (BrokenPipeError, KeyboardInterrupt):
        pass
