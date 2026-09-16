from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

AGENTS = ("devin", "codex", "claude", "chatgpt")

EVENT_KINDS = (
    "prompt",
    "response",
    "tool",
    "session_start",
    "session_end",
    "compaction",
    "usage",
    "error",
    "system",
)

MAX_EVENT_BYTES = 1024 * 1024
MAX_IMPORT_BYTES = 64 * 1024 * 1024

_TS_MIN_YEAR = 2000
_TS_MAX_YEAR = 2100

_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?"
    r"(Z|[+-]\d{2}:?\d{2})?$"
)


def canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        default=str,
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def deterministic_event_id(agent: str, provider_event: dict) -> str:
    return sha256_hex(
        "partial/event/v1\x00" + agent + "\x00" + canonical_json(provider_event)
    )


def new_event_id() -> str:
    return uuid.uuid4().hex


def scoped_session_id(repo_id: str, agent: str, native_id: str) -> str:
    return sha256_hex(
        "partial/session/v1\x00" + repo_id + "\x00" + agent + "\x00" + native_id
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def normalize_timestamp(value: object) -> str:
    if value is None or value == "":
        raise ValueError("missing timestamp")
    if isinstance(value, bool):
        raise ValueError("invalid timestamp type")
    if isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError(f"numeric timestamp out of range: {value!r}") from exc
    elif isinstance(value, str):
        text = value.strip()
        if not _ISO_RE.match(text):
            raise ValueError(f"not an ISO8601 timestamp: {value!r}")
        fixed = text.replace(" ", "T")
        if fixed.endswith("Z"):
            fixed = fixed[:-1] + "+00:00"
        if re.search(r"[+-]\d{4}$", fixed):
            fixed = fixed[:-2] + ":" + fixed[-2:]
        try:
            dt = datetime.fromisoformat(fixed)
        except ValueError as exc:
            raise ValueError(f"not an ISO8601 timestamp: {value!r}") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
    else:
        raise ValueError(f"invalid timestamp type: {type(value).__name__}")
    if not (_TS_MIN_YEAR <= dt.year <= _TS_MAX_YEAR):
        raise ValueError(f"timestamp year out of bounds: {value!r}")
    return dt.isoformat(timespec="microseconds")


@dataclass
class Event:
    id: str
    session_id: str
    agent: str
    kind: str
    timestamp: str
    text: str = ""
    tool_name: str | None = None
    data: dict = field(default_factory=dict)
    parent_session_id: str | None = None
    model: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        if not isinstance(d, dict):
            raise ValueError("event must be an object")
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in d.items() if k in known}
        return cls(**kwargs)


def validate_event(ev: Event) -> Event:
    if not isinstance(ev, Event):
        raise ValueError("expected Event")
    if ev.agent not in AGENTS:
        raise ValueError(f"unknown agent: {ev.agent!r}")
    if ev.kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind: {ev.kind!r}")
    if not isinstance(ev.id, str) or not ev.id or len(ev.id) > 128:
        raise ValueError("invalid event id")
    if not isinstance(ev.session_id, str) or not ev.session_id or len(ev.session_id) > 512:
        raise ValueError("invalid session id")
    if ev.parent_session_id is not None and (
        not isinstance(ev.parent_session_id, str) or len(ev.parent_session_id) > 512
    ):
        raise ValueError("invalid parent session id")
    if ev.model is not None and not isinstance(ev.model, str):
        raise ValueError("model must be a string")
    ev.timestamp = normalize_timestamp(ev.timestamp)
    if ev.text is None:
        ev.text = ""
    elif not isinstance(ev.text, str):
        ev.text = str(ev.text)
    if ev.tool_name is not None and not isinstance(ev.tool_name, str):
        raise ValueError("tool_name must be a string")
    if ev.data is None:
        ev.data = {}
    elif not isinstance(ev.data, dict):
        raise ValueError("event data must be an object")
    if len(canonical_json(ev.to_dict()).encode("utf-8")) > MAX_EVENT_BYTES:
        raise ValueError("event exceeds 1 MiB limit")
    return ev
