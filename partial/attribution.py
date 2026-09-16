from __future__ import annotations

import copy
import hashlib
from difflib import SequenceMatcher

METHOD = "position-aware-snapshot-diff-v1"
MAX_LINES = 10000
MAX_BYTES = 1024 * 1024
HISTORY_LIMIT = 16
KINDS = ("agent", "human", "unknown")
EVIDENCE = ("tool-pair", "turn-window", "external", "unobserved", "overlap")


def fingerprint(content: bytes) -> list[str]:
    if len(content) > MAX_BYTES or b"\x00" in content:
        raise ValueError("attribution requires text files of at most 1 MiB")
    content.decode("utf-8")
    lines = content.splitlines(keepends=True)
    if len(lines) > MAX_LINES:
        raise ValueError("attribution file exceeds 10000 lines")
    return [hashlib.sha256(line).hexdigest() for line in lines]


def _check_hashes(lines: list[str]) -> None:
    if not isinstance(lines, list) or len(lines) > MAX_LINES:
        raise ValueError("invalid attribution line fingerprints")
    if any(not isinstance(x, str) or len(x) != 64 or
           any(c not in "0123456789abcdef" for c in x) for x in lines):
        raise ValueError("invalid attribution line fingerprint")


def _owner(kind: str, session_id: str | None, evidence: str) -> dict:
    if kind not in KINDS or evidence not in EVIDENCE:
        raise ValueError("invalid attribution owner")
    if kind == "agent" and (not isinstance(session_id, str) or not session_id):
        raise ValueError("agent attribution requires a session")
    return {"kind": kind, "session_id": session_id if kind == "agent" else None,
            "evidence": evidence}


def _snapshot(state: dict) -> dict:
    return copy.deepcopy({k: state[k] for k in
                          ("current", "owners", "origins", "deletions")})


def new_state(base: list[str], before: list[str] | None = None) -> dict:
    _check_hashes(base)
    state = {"version": 1, "base": list(base), "current": list(base),
             "owners": [_owner("unknown", None, "unobserved") for _ in base],
             "origins": list(range(len(base))), "deletions": {},
             "history": [], "revision": 0}
    if before is not None and before != base:
        return advance(state, before, kind="unknown", evidence="unobserved")
    return state


def advance(state: dict, after: list[str], *, kind: str,
            session_id: str | None = None, evidence: str = "tool-pair",
            expected_before: list[str] | None = None) -> dict:
    _check_hashes(after)
    result = copy.deepcopy(state)
    writer = _owner(kind, session_id, evidence)
    if expected_before is not None and state["current"] != expected_before:
        writer = _owner("unknown", None, "overlap")
    owners, origins = [], []
    deleted = dict(result["deletions"])
    for tag, a, b, c, d in SequenceMatcher(
            None, state["current"], after, autojunk=False).get_opcodes():
        if tag == "equal":
            owners.extend(copy.deepcopy(state["owners"][a:b]))
            origins.extend(state["origins"][a:b])
            continue
        for original in state["origins"][a:b]:
            if original is not None:
                deleted[str(original)] = dict(writer)
        owners.extend(dict(writer) for _ in range(c, d))
        origins.extend(None for _ in range(c, d))
    history = result.get("history", [])
    if state["current"] != after:
        history.append(_snapshot(state))
    result.update(current=list(after), owners=owners, origins=origins,
                  deletions=deleted, history=history[-HISTORY_LIMIT:],
                  revision=int(state.get("revision", 0)) + 1)
    return result


def _projection(state: dict, committed: list[str]) -> dict:
    if state["current"] == committed:
        return state
    for old in reversed(state.get("history", [])):
        if old["current"] == committed:
            return old
    return advance(state, committed, kind="unknown", evidence="unobserved")


def summarize(lines: list[dict]) -> dict:
    counts = {f"{kind}_{operation}": 0 for kind in KINDS
              for operation in ("added", "removed")}
    seen = set()
    for entry in lines:
        side, number, kind = entry.get("side"), entry.get("line"), entry.get("kind")
        if side not in ("new", "old") or type(number) is not int or number < 1:
            raise ValueError("invalid attribution line position")
        _owner(kind, entry.get("session_id"), entry.get("evidence"))
        if (side, number) in seen:
            raise ValueError("duplicate attribution line position")
        seen.add((side, number))
        counts[f"{kind}_{'added' if side == 'new' else 'removed'}"] += 1
    return _summary_counts(counts)


def aggregate(reports: list[dict]) -> dict:
    totals = {f"{kind}_{operation}": 0 for kind in KINDS
              for operation in ("added", "removed")}
    for item in reports:
        counts = summarize(item["lines"])
        for key in totals:
            totals[key] += counts[key]
    return _summary_counts(totals)


def _summary_counts(counts: dict) -> dict:
    total = sum(counts.values())
    agent = counts["agent_added"] + counts["agent_removed"]
    unknown = counts["unknown_added"] + counts["unknown_removed"]
    return {**counts, "total_changed": total,
            "agent_percentage": round(100 * agent / total, 2) if total else None,
            "coverage_percentage": round(100 * (total - unknown) / total, 2)
            if total else None}


def report(state: dict, committed: list[str]) -> dict:
    _check_hashes(committed)
    projected = _projection(state, committed)
    lines = []
    unknown = _owner("unknown", None, "unobserved")
    for tag, a, b, c, d in SequenceMatcher(
            None, state["base"], committed, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        for i in range(a, b):
            owner = projected["deletions"].get(str(i), unknown)
            lines.append({"side": "old", "line": i + 1, **owner})
        for i in range(c, d):
            lines.append({"side": "new", "line": i + 1,
                          **projected["owners"][i]})
    return {"version": 1, "method": METHOD, "lines": lines,
            "summary": summarize(lines),
            "limitations": [
                "Authorship is inferred from snapshots, not keystrokes.",
                "Human means an edit observed outside a tracked agent tool.",
                "Concurrent edits within a capture window may be misattributed.",
                "Duplicate lines use deterministic sequence alignment.",
                "Unmatched partial staging is unknown; only 16 prior snapshots are retained.",
                "The denominator is added plus removed lines under Partial's diff algorithm."
            ]}


def rebase(state: dict, base: list[str]) -> dict:
    _check_hashes(base)
    if state["base"] == base:
        return copy.deepcopy(state)
    result = new_state(base)
    result = advance(result, state["current"], kind="unknown", evidence="unobserved")
    for i, origin in enumerate(result["origins"]):
        if origin is None:
            result["owners"][i] = copy.deepcopy(state["owners"][i])
    old_to_new = {}
    for block in SequenceMatcher(None, state["base"], base,
                                 autojunk=False).get_matching_blocks():
        for j in range(block.size):
            old_to_new[block.a + j] = block.b + j
    for old, owner in state["deletions"].items():
        mapped = old_to_new.get(int(old))
        if mapped is not None and str(mapped) in result["deletions"]:
            result["deletions"][str(mapped)] = copy.deepcopy(owner)
    result["history"] = []
    result["revision"] = state.get("revision", 0) + 1
    return result
