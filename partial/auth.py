from __future__ import annotations

import os
import secrets
from pathlib import Path

TOKEN_FILE = "server-token"
MIN_TOKEN_LEN = 32


def _read_token(path: Path) -> str:
    token = path.read_text().strip()
    if len(token) < MIN_TOKEN_LEN:
        raise ValueError(f"{path}: malformed server token file")
    return token


def get_token(home: Path) -> str:
    env = os.environ.get("PARTIAL_TOKEN")
    if env is not None:
        if len(env) < MIN_TOKEN_LEN:
            raise ValueError(
                "PARTIAL_TOKEN must be at least 32 characters")
        return env
    home = Path(home)
    path = home / TOKEN_FILE
    try:
        return _read_token(path)
    except FileNotFoundError:
        pass
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_token(path)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token
