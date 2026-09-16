from __future__ import annotations

import re

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_TOKENS = (
    "authorization",
    "password",
    "passwd",
    "secret",
    "apikey",
    "accesstoken",
    "refreshtoken",
    "cookie",
    "privatekey",
)

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{4,}")
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{6,}")
_GH_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")
_GH_PAT_RE = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}")
_AWS_RE = re.compile(
    r"\b(AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASCA)[A-Z0-9]{16}\b"
)
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
    r".*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----",
    re.DOTALL,
)
_ASSIGN_RE = re.compile(
    r"(?i)\b(PASSWORD|TOKEN|SECRET|API[_-]?KEY|ACCESS[_-]?TOKEN|AUTH(?:ORIZATION)?)"
    r"(\s*[:=]\s*)"
    r"(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;\"']+)"
)


def _norm_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _is_sensitive_key(key: object) -> bool:
    norm = _norm_key(key)
    if not norm:
        return False
    return any(tok in norm for tok in _SENSITIVE_KEY_TOKENS)


def _redact_text(text: str) -> str:
    out = _PEM_RE.sub(REDACTED, text)
    out = _BEARER_RE.sub("Bearer " + REDACTED, out)
    out = _SK_RE.sub(REDACTED, out)
    out = _GH_RE.sub(REDACTED, out)
    out = _GH_PAT_RE.sub(REDACTED, out)
    out = _AWS_RE.sub(REDACTED, out)
    out = _ASSIGN_RE.sub(lambda m: m.group(1) + m.group(2) + REDACTED, out)
    return out


def redact(value: object) -> object:
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            if _is_sensitive_key(k):
                out[k] = REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def is_sensitive_path(path: str) -> bool:
    p = str(path).replace("\\", "/").strip("/")
    if not p:
        return False
    parts = p.split("/")
    if ".git" in parts:
        return True
    name = parts[-1]
    if name == ".env" or name.startswith(".env."):
        return True
    if name.startswith("credentials"):
        return True
    if name.endswith(".pem") or name.endswith(".key"):
        return True
    return False
