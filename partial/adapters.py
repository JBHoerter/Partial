from __future__ import annotations

import json

from .models import (
    AGENTS,
    MAX_IMPORT_BYTES,
    Event,
    canonical_json,
    deterministic_event_id,
    new_event_id,
    normalize_timestamp,
    now_iso,
    sha256_hex,
)
from .privacy import redact

HOOK_EVENTS = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "prompt",
    "PostToolUse": "tool",
    "Stop": "response",
    "SessionEnd": "session_end",
    "PostCompaction": "compaction",
    "PostCompact": "compaction",
    "PreCompact": "compaction",
}

_HOOK_AGENTS = ("devin", "claude")

_CORE_KEYS = {
    "hook_event_name", "session_id", "prompt", "prompt_id", "event_id",
    "tool_name", "tool_input", "tool_response", "last_assistant_message",
    "timestamp", "parent_session_id", "model", "cwd",
}


def _ts(payload: dict) -> str:
    raw = payload.get("timestamp")
    try:
        return normalize_timestamp(raw)
    except ValueError:
        return now_iso()


def _model(payload: dict) -> str | None:
    m = payload.get("model")
    if isinstance(m, str):
        return m
    if isinstance(m, dict):
        mid = m.get("id") or m.get("model") or m.get("name")
        return str(mid) if mid else None
    return None


def _hook_event_id(agent: str, payload: dict, event_name: str) -> str:
    pid = payload.get("event_id")
    if isinstance(pid, str) and pid:
        return pid
    if event_name == "UserPromptSubmit":
        pid = payload.get("prompt_id")
        if isinstance(pid, str) and pid:
            return pid
    return new_event_id()


def normalize_hook(
    agent: str, payload: dict, event_name: str | None = None
) -> list[Event]:
    if agent not in AGENTS:
        raise ValueError(f"unknown agent: {agent!r}")
    if agent not in _HOOK_AGENTS:
        raise ValueError(f"agent {agent!r} does not emit hook events")
    if not isinstance(payload, dict):
        raise ValueError("hook payload must be a JSON object")
    name = event_name or payload.get("hook_event_name")
    if name not in HOOK_EVENTS:
        raise ValueError(f"unknown hook event: {name!r}")
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("hook payload missing session_id")
    kind = HOOK_EVENTS[name]
    text = ""
    tool_name = None
    data: dict = {}
    if name == "UserPromptSubmit":
        text = str(payload.get("prompt") or "")
        data["prompt_id"] = payload.get("prompt_id")
    elif name == "PostToolUse":
        tool_name = str(payload.get("tool_name") or "tool")
        data["tool_input"] = payload.get("tool_input")
        data["tool_response"] = payload.get("tool_response")
    elif name == "Stop":
        msg = payload.get("last_assistant_message")
        text = msg if isinstance(msg, str) else ""
    extra = {k: v for k, v in payload.items() if k not in _CORE_KEYS}
    if extra:
        data["hook"] = extra
    ev = Event(
        id=_hook_event_id(agent, payload, name),
        session_id=session_id,
        agent=agent,
        kind=kind,
        timestamp=_ts(payload),
        text=text,
        tool_name=tool_name,
        data=redact(data),
        parent_session_id=payload.get("parent_session_id"),
        model=_model(payload),
    )
    return [ev]


def _det_id(agent: str, provider_event: dict) -> str:
    return deterministic_event_id(agent, redact(provider_event))


def codex_event(payload: dict, session_id: str) -> list[Event]:
    if not isinstance(payload, dict):
        return []
    ts = _ts(payload)
    ptype = payload.get("type")
    sid = session_id or "codex"
    if ptype == "thread.started":
        tid = payload.get("thread_id") or sid
        return [Event(
            id=f"{tid}:thread.started",
            session_id=str(tid), agent="codex", kind="session_start",
            timestamp=ts, data=redact({"thread_id": tid}),
        )]
    if ptype == "turn.started":
        return []
    if ptype in ("turn.completed", "turn.complete"):
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        return [Event(
            id=_det_id("codex", {"t": ptype, "u": usage, "s": sid}),
            session_id=sid, agent="codex", kind="usage",
            timestamp=ts, data=redact({"usage": usage}),
        )]
    if ptype == "item.started":
        return []
    if ptype == "item.completed":
        item = payload.get("item")
        if not isinstance(item, dict):
            return []
        itype = item.get("type")
        if itype == "reasoning":
            return []
        iid = item.get("id")
        eid = (
            f"{sid}:item:{iid}" if isinstance(iid, str) and iid
            else _det_id("codex", {"s": sid, "item": item})
        )
        if itype in ("agent_message", "message", "assistant_message"):
            return [Event(
                id=eid, session_id=sid, agent="codex", kind="response",
                timestamp=ts, text=str(item.get("text") or ""),
                data=redact({"item_type": itype}),
            )]
        if itype in ("user_message", "user_input"):
            return [Event(
                id=eid, session_id=sid, agent="codex", kind="prompt",
                timestamp=ts, text=str(item.get("text") or ""),
            )]
        if itype in ("command_execution", "command"):
            return [Event(
                id=eid, session_id=sid, agent="codex", kind="tool",
                timestamp=ts, tool_name="command_execution",
                data=redact({
                    "command": item.get("command"),
                    "aggregated_output": item.get("aggregated_output"),
                    "exit_code": item.get("exit_code"),
                }),
            )]
        if itype in ("file_change", "file_changes", "patch"):
            return [Event(
                id=eid, session_id=sid, agent="codex", kind="tool",
                timestamp=ts, tool_name="file_change",
                data=redact({"changes": item.get("changes") or []}),
            )]
        return [Event(
            id=eid, session_id=sid, agent="codex", kind="tool",
            timestamp=ts, tool_name=str(itype or "item"),
            data=redact({"item": item}),
        )]
    if ptype in ("error", "turn.failed", "thread.failed"):
        msg = payload.get("message") or payload.get("error") or ""
        if isinstance(msg, dict):
            msg = msg.get("message") or canonical_json(msg)
        return [Event(
            id=_det_id("codex", {"t": ptype, "m": str(msg), "s": sid}),
            session_id=sid, agent="codex", kind="error",
            timestamp=ts, text=str(msg),
        )]
    return []


def _iter_jsonl(content: str, agent: str) -> list[tuple[int, dict]]:
    out = []
    for i, line in enumerate(content.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"malformed or truncated JSONL for {agent} import at line "
                f"{i + 1}: {exc.msg}; nothing was imported"
            ) from exc
        if not isinstance(obj, dict):
            raise ValueError(
                f"line {i + 1} of {agent} import is not a JSON object"
            )
        out.append((i, obj))
    return out


def _import_canonical(obj: dict) -> list[Event]:
    events = obj.get("events")
    if not isinstance(events, list):
        raise ValueError("canonical import requires an events array")
    out = []
    for e in events:
        if not isinstance(e, dict):
            raise ValueError("canonical event entries must be objects")
        ev = Event.from_dict(e)
        if ev.agent not in AGENTS:
            raise ValueError(f"unknown agent: {ev.agent!r}")
        out.append(ev)
    return out


def _import_devin_atif(obj: dict, session_id: str | None) -> list[Event]:
    steps = obj.get("steps")
    if not isinstance(steps, list):
        raise ValueError("ATIF import requires a steps array")
    sid = session_id or str(obj.get("session_id") or "") or \
        "devin-import-" + sha256_hex(canonical_json(obj))[:24]
    out: list[Event] = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"ATIF step {i} is not an object")
        ts = _ts(step)
        base = {"s": sid, "i": i, "step": step}
        source = str(step.get("source") or "").lower()
        message = step.get("message")
        if isinstance(message, str) and message:
            kind = "prompt" if source in ("user", "human") else "response"
            out.append(Event(
                id=_det_id("devin", {**base, "k": kind}),
                session_id=sid, agent="devin", kind=kind, timestamp=ts,
                text=message,
            ))
        calls = step.get("tool_calls") or []
        if isinstance(calls, list):
            for j, call in enumerate(calls):
                if not isinstance(call, dict):
                    continue
                name = call.get("name") or call.get("function_name") or "tool"
                args = call.get("arguments") or call.get("input")
                out.append(Event(
                    id=_det_id("devin", {**base, "k": "tool", "j": j}),
                    session_id=sid, agent="devin", kind="tool", timestamp=ts,
                    tool_name=str(name),
                    data=redact({"tool_input": args}),
                ))
        obs = step.get("observation")
        if obs:
            out.append(Event(
                id=_det_id("devin", {**base, "k": "observation"}),
                session_id=sid, agent="devin", kind="tool", timestamp=ts,
                tool_name="observation",
                data=redact({"tool_response": {"output": obs}}),
            ))
    return out


def _import_devin_hooks(lines: list[tuple[int, dict]],
                        session_id: str | None) -> list[Event]:
    fallback = "devin-import-" + sha256_hex(
        canonical_json([o for _, o in lines])
    )[:24]
    out: list[Event] = []
    for i, obj in lines:
        sid = session_id or str(obj.get("session_id") or "") or fallback
        evs = normalize_hook("devin", {**obj, "session_id": sid})
        for ev in evs:
            if not obj.get("event_id") and not (
                ev.kind == "prompt" and obj.get("prompt_id")
            ):
                ev.id = _det_id("devin", {"i": i, "p": obj, "k": ev.kind})
        out.extend(evs)
    return out


def _import_claude_hook(obj: dict, session_id: str | None) -> list[Event]:
    sid = session_id or str(obj.get("session_id") or "") or \
        "claude-import-" + sha256_hex(canonical_json(obj))[:24]
    evs = normalize_hook("claude", {**obj, "session_id": sid})
    for ev in evs:
        if not obj.get("event_id") and not (
                ev.kind == "prompt" and obj.get("prompt_id")):
            ev.id = _det_id("claude", {"i": 0, "p": obj, "k": ev.kind})
    return evs


def _import_claude(lines: list[tuple[int, dict]],
                   session_id: str | None) -> list[Event]:
    out: list[Event] = []
    fallback_sid = session_id or "claude-import-" + sha256_hex(
        canonical_json([o for _, o in lines])
    )[:24]
    for i, obj in lines:
        sid = session_id or str(obj.get("sessionId") or fallback_sid)
        ts = _ts(obj)
        uuid_ = obj.get("uuid")
        base = {"i": i, "s": sid, "u": uuid_}
        msg = obj.get("message")
        role = obj.get("type") or (msg or {}).get("role")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            continue
        for j, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            eid = (
                f"{sid}:{uuid_}:{j}" if isinstance(uuid_, str) and uuid_
                else _det_id("claude", {**base, "j": j, "b": block})
            )
            if btype == "text":
                kind = "prompt" if role == "user" else "response"
                out.append(Event(
                    id=eid, session_id=sid, agent="claude", kind=kind,
                    timestamp=ts, text=str(block.get("text") or ""),
                ))
            elif btype == "tool_use":
                out.append(Event(
                    id=eid, session_id=sid, agent="claude", kind="tool",
                    timestamp=ts, tool_name=str(block.get("name") or "tool"),
                    data=redact({"tool_input": block.get("input")}),
                ))
            elif btype == "tool_result":
                out.append(Event(
                    id=eid, session_id=sid, agent="claude", kind="tool",
                    timestamp=ts, tool_name="tool_result",
                    data=redact({"tool_response": {
                        "content": block.get("content"),
                        "is_error": block.get("is_error"),
                    }}),
                ))
    return out


def _codex_rollout_event(obj: dict, sid: str, i: int) -> list[Event]:
    ts = _ts(obj)
    ptype = obj.get("type")
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return []
    sub = payload.get("type")
    eid = _det_id("codex", {"i": i, "s": sid, "p": payload})
    if ptype == "event_msg":
        if sub == "user_message":
            return [Event(
                id=eid, session_id=sid, agent="codex", kind="prompt",
                timestamp=ts, text=str(payload.get("message") or ""),
            )]
        if sub == "agent_message":
            return [Event(
                id=eid, session_id=sid, agent="codex", kind="response",
                timestamp=ts, text=str(payload.get("message") or ""),
            )]
        if sub == "token_count":
            info = payload.get("info")
            return [Event(
                id=eid, session_id=sid, agent="codex", kind="usage",
                timestamp=ts, data=redact({"usage": info or {}}),
            )]
        return []
    if ptype == "response_item":
        if sub == "message":
            role = payload.get("role")
            parts = payload.get("content") or []
            text = "".join(
                str(p.get("text") or "") for p in parts
                if isinstance(p, dict)
                and p.get("type") in ("input_text", "output_text", "text")
            )
            if not text:
                return []
            kind = "prompt" if role == "user" else "response"
            return [Event(
                id=eid, session_id=sid, agent="codex", kind=kind,
                timestamp=ts, text=text,
            )]
        if sub == "function_call":
            args = payload.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    pass
            return [Event(
                id=eid, session_id=sid, agent="codex", kind="tool",
                timestamp=ts, tool_name=str(payload.get("name") or "tool"),
                data=redact({"tool_input": args}),
            )]
        if sub == "function_call_output":
            return [Event(
                id=eid, session_id=sid, agent="codex", kind="tool",
                timestamp=ts, tool_name="function_call_output",
                data=redact({"tool_response": {
                    "output": payload.get("output"),
                    "call_id": payload.get("call_id"),
                }}),
            )]
        if sub == "reasoning":
            return []
    return []


def _namespace_codex(ev: Event, sid: str, turn: int, seq: int) -> None:
    base = ev.id
    if base.startswith(sid + ":"):
        base = base[len(sid) + 1:]
    ev.id = f"{sid}:t{turn}:l{seq}:{base}"


def _import_codex(lines: list[tuple[int, dict]],
                  session_id: str | None) -> list[Event]:
    sid = session_id or ""
    for _, obj in lines:
        if obj.get("type") == "thread.started" and obj.get("thread_id"):
            sid = session_id or str(obj["thread_id"])
            break
    if not sid:
        sid = "codex-import-" + sha256_hex(
            canonical_json([o for _, o in lines])
        )[:24]
    out: list[Event] = []
    turn = 0
    for i, obj in lines:
        if obj.get("type") == "turn.started":
            turn += 1
        if obj.get("type") in ("event_msg", "response_item"):
            out.extend(_codex_rollout_event(obj, sid, i))
        else:
            for ev in codex_event(obj, sid):
                ev.session_id = sid
                _namespace_codex(ev, sid, turn, i)
                out.append(ev)
    return out


def _chatgpt_node_order(conv: dict) -> list[str]:
    mapping = conv.get("mapping")
    if not isinstance(mapping, dict):
        return []
    nodes = {k: v for k, v in mapping.items() if isinstance(v, dict)}
    cur = conv.get("current_node")
    if isinstance(cur, str) and cur in nodes:
        chain = []
        seen = set()
        node = cur
        while node and node in nodes and node not in seen:
            seen.add(node)
            chain.append(node)
            node = nodes[node].get("parent")
        return list(reversed(chain))
    order: list[str] = []
    visited: set[str] = set()

    def key(cid: str):
        m = (nodes.get(cid) or {}).get("message")
        ct = m.get("create_time") if isinstance(m, dict) else None
        try:
            ctf = float(ct) if ct is not None else 0.0
        except (TypeError, ValueError):
            ctf = 0.0
        return (ctf, cid)

    def visit(nid: str) -> None:
        if nid in visited or nid not in nodes:
            return
        visited.add(nid)
        order.append(nid)
        children = nodes[nid].get("children") or []
        for c in sorted((c for c in children if c in nodes), key=key):
            visit(c)

    roots = [
        nid for nid, n in nodes.items()
        if not n.get("parent") or n.get("parent") not in nodes
    ]
    for r in sorted(roots, key=key):
        visit(r)
    return order


def _import_chatgpt(obj: object, session_id: str | None) -> list[Event]:
    convs = obj if isinstance(obj, list) else [obj]
    out: list[Event] = []
    for conv in convs:
        if not isinstance(conv, dict):
            continue
        cid = str(conv.get("id") or "chatgpt-import")
        sid = session_id or cid
        title = conv.get("title")
        for nid in _chatgpt_node_order(conv):
            node = (conv.get("mapping") or {}).get(nid) or {}
            msg = node.get("message")
            if not isinstance(msg, dict):
                continue
            role = (msg.get("author") or {}).get("role")
            content = msg.get("content") or {}
            parts = content.get("parts")
            if not isinstance(parts, list):
                continue
            texts = [p for p in parts if isinstance(p, str) and p]
            if not texts:
                continue
            text = "\n".join(texts)
            try:
                ts = normalize_timestamp(msg.get("create_time"))
            except ValueError:
                ts = "2000-01-01T00:00:00+00:00"
            mid = msg.get("id") or nid
            eid = _det_id("chatgpt", {"c": cid, "n": nid, "m": mid})
            if role == "user":
                kind = "prompt"
            elif role == "assistant":
                kind = "response"
            elif role == "tool":
                kind = "tool"
            else:
                continue
            ev = Event(
                id=eid, session_id=sid, agent="chatgpt", kind=kind,
                timestamp=ts, text=text,
                tool_name="chatgpt_tool" if kind == "tool" else None,
                data={"conversation_title": title} if title else {},
            )
            out.append(ev)
    return out


def parse_import(
    agent: str, content: str, *, session_id: str | None = None
) -> list[Event]:
    if agent not in AGENTS:
        raise ValueError(f"unknown agent: {agent!r}")
    if len(content.encode("utf-8", "replace")) > MAX_IMPORT_BYTES:
        raise ValueError("import content exceeds 64 MiB limit")
    stripped = content.strip()
    if not stripped:
        return []
    obj = None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, dict) and isinstance(obj.get("events"), list):
        evs = _import_canonical(obj)
        if session_id:
            for ev in evs:
                ev.session_id = session_id
        return evs
    if agent == "devin":
        if isinstance(obj, dict) and isinstance(obj.get("steps"), list):
            return _import_devin_atif(obj, session_id)
        if isinstance(obj, dict) and obj.get("hook_event_name"):
            return _import_devin_hooks([(0, obj)], session_id)
        return _import_devin_hooks(_iter_jsonl(content, agent), session_id)
    if agent == "claude":
        if isinstance(obj, dict) and obj.get("hook_event_name"):
            return _import_claude_hook(obj, session_id)
        return _import_claude(_iter_jsonl(content, agent), session_id)
    if agent == "codex":
        lines = _iter_jsonl(content, agent)
        return _import_codex(lines, session_id)
    if agent == "chatgpt":
        if not isinstance(obj, (list, dict)):
            raise ValueError("ChatGPT import requires a JSON export")
        return _import_chatgpt(obj, session_id)
    raise ValueError(f"unsupported import for agent {agent!r}")
