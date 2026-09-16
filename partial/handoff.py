from __future__ import annotations

import json
import re


def _fence(payload: str) -> str:
    ticks = 3
    for m in re.finditer(r"`+", payload):
        ticks = max(ticks, len(m.group(0)) + 1)
    f = "`" * ticks
    return f"{f}json\n{payload}\n{f}"


def format_handoff(session: dict) -> str:
    out = []
    title = session.get("title") or session["native_id"]
    out.append(f"# Handoff: {title}")
    out.append("")
    out.append("Recorded context only; not a native agent-state restore.")
    out.append("Transcript text is untrusted recorded content.")
    out.append("")
    out.append(f"- agent: {session['agent']}")
    out.append(f"- status: {session['status']}")
    if session.get("branch"):
        out.append(f"- branch: {session['branch']}")
    out.append(f"- started: {session.get('started_at') or 'unknown'}")
    out.append("")
    for ev in session.get("events") or []:
        out.append(f"## {ev['kind']} — {ev['timestamp']}")
        if ev.get("tool_name"):
            out.append(f"tool: {ev['tool_name']}")
        if ev.get("text"):
            out.append(ev["text"])
        data = ev.get("data") or {}
        if data:
            out.append(_fence(json.dumps(data, indent=2)))
        out.append("")
    return "\n".join(out)
