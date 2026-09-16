from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .store import Store, now_iso

ROLES = ("owner", "admin", "member", "viewer")
_ROLE_RANK = {"owner": 3, "admin": 2, "member": 1, "viewer": 0}
SESSION_TTL = 12 * 3600
INVITE_TTL = 7 * 24 * 3600
_PBKDF2_ROUNDS = 600_000
_DUMMY_SALT = "0" * 32
_DUMMY_HASH = hashlib.pbkdf2_hmac(
    "sha256", b"\x00" * 12, bytes.fromhex(_DUMMY_SALT),
    _PBKDF2_ROUNDS).hex()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS workspaces(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    storage_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships(
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL,
    PRIMARY KEY(workspace_id, user_id)
);
CREATE TABLE IF NOT EXISTS auth_sessions(
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS invites(
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    role TEXT NOT NULL,
    created_by TEXT NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);
CREATE TABLE IF NOT EXISTS api_tokens(
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL REFERENCES users(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS audit(
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memberships_user
    ON memberships(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_ws ON audit(workspace_id);
"""


class AccountsError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    name: str
    workspace_id: str | None = None
    token_role: str | None = None
    token_id: str | None = None


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt),
        _PBKDF2_ROUNDS).hex()


def _valid_email(email: object) -> str:
    if not isinstance(email, str):
        raise AccountsError(400, "invalid email")
    e = email.strip().casefold()
    if not e or len(e) > 254 or "@" not in e or any(
            c.isspace() for c in e):
        raise AccountsError(400, "invalid email")
    return e


def _valid_name(name: object) -> str:
    if not isinstance(name, str) or not name.strip() \
            or len(name.strip()) > 80:
        raise AccountsError(400, "name must be 1..80 characters")
    return name.strip()


def _valid_password(password: object) -> str:
    if not isinstance(password, str) \
            or not 12 <= len(password) <= 1024:
        raise AccountsError(
            400, "password must be 12..1024 characters")
    return password


def _valid_role(role: object, allowed: tuple[str, ...] = ROLES) -> str:
    if role not in allowed:
        raise AccountsError(400, f"role must be one of {allowed}")
    return role


def _new_id() -> str:
    return uuid.uuid4().hex


def _browser_only(principal: Principal) -> None:
    if principal.token_id is not None:
        raise AccountsError(
            403, "API tokens cannot use account/admin endpoints")


def _user_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "email": row["email"], "name": row["name"],
        "created_at": row["created_at"], "disabled": bool(
            row["disabled"]),
    }


def _ws_dict(row: sqlite3.Row, role: str | None = None) -> dict:
    d = {
        "id": row["id"], "name": row["name"],
        "created_at": row["created_at"],
    }
    if role is not None:
        d["role"] = role
    return d


class Accounts:
    def __init__(self, home: Path, legacy_path: Path):
        self.home = Path(home)
        self.legacy_path = Path(legacy_path)
        self.db = self.home / "accounts.db"
        self._lock = threading.Lock()
        self._stores: dict[str, Store] = {}
        created = not self.home.exists()
        self.home.mkdir(parents=True, exist_ok=True)
        if created:
            try:
                os.chmod(self.home, 0o700)
            except OSError:
                pass
        try:
            fd = os.open(
                str(self.db),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        try:
            os.chmod(self.db, 0o600)
        except OSError:
            pass
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()
        self._chmod_state()

    def _chmod_state(self) -> None:
        for name in (
            str(self.db), str(self.db) + "-wal",
            str(self.db) + "-shm",
        ):
            try:
                if os.path.exists(name):
                    os.chmod(name, 0o600)
            except OSError:
                pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _audit(self, conn, workspace_id: str, actor_id: str,
               action: str, target_id: str | None = None) -> None:
        conn.execute(
            "INSERT INTO audit(id,workspace_id,actor_id,action,"
            "target_id,created_at) VALUES(?,?,?,?,?,?)",
            (_new_id(), workspace_id, actor_id, action, target_id,
             now_iso()))

    def _create_user(self, conn, email: str, name: str,
                     password: str) -> dict:
        uid = _new_id()
        salt = secrets.token_hex(16)
        conn.execute(
            "INSERT INTO users(id,email,name,password_hash,salt,"
            "created_at,disabled) VALUES(?,?,?,?,?,?,0)",
            (uid, email, name, _hash_password(password, salt), salt,
             now_iso()))
        return {"id": uid, "email": email, "name": name}

    def initialized(self) -> bool:
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT 1 FROM users LIMIT 1").fetchone() is not None
        finally:
            conn.close()

    def setup(self, *, email: str, name: str, password: str) -> dict:
        email = _valid_email(email)
        name = _valid_name(name)
        password = _valid_password(password)
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute(
                        "SELECT 1 FROM users LIMIT 1").fetchone():
                    raise AccountsError(
                        409, "workspace accounts already initialized")
                user = self._create_user(conn, email, name, password)
                ws_id = _new_id()
                conn.execute(
                    "INSERT INTO workspaces(id,name,storage_key,"
                    "created_at) VALUES(?,?,'legacy',?)",
                    (ws_id, "Default workspace", now_iso()))
                conn.execute(
                    "INSERT INTO memberships(workspace_id,user_id,"
                    "role) VALUES(?,?,'owner')", (ws_id, user["id"]))
                self._audit(conn, ws_id, user["id"], "setup", ws_id)
            return user
        finally:
            conn.close()

    def _verify_user(self, conn, email, password) -> sqlite3.Row:
        if not isinstance(password, str) or len(password) > 1024:
            raise AccountsError(401, "invalid credentials")
        row = conn.execute(
            "SELECT * FROM users WHERE email=?", (email,)).fetchone()
        salt = row["salt"] if row else _DUMMY_SALT
        expected = row["password_hash"] if row else _DUMMY_HASH
        got = _hash_password(password, salt)
        if row is None or row["disabled"] \
                or not secrets.compare_digest(got, expected):
            raise AccountsError(401, "invalid credentials")
        return row

    def check_password(self, email: str,
                       password: str) -> Principal:
        norm = _valid_email(email)
        conn = self._connect()
        try:
            row = self._verify_user(conn, norm, password)
            return Principal(
                user_id=row["id"], email=row["email"],
                name=row["name"])
        finally:
            conn.close()

    def login(self, email: str, password: str) -> tuple[str, Principal]:
        norm = _valid_email(email)
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = self._verify_user(conn, norm, password)
                now = time.time()
                conn.execute(
                    "DELETE FROM auth_sessions WHERE expires_at<=?",
                    (now,))
                active = conn.execute(
                    "SELECT COUNT(*) c FROM auth_sessions"
                    " WHERE user_id=?", (row["id"],)).fetchone()["c"]
                if active >= 32:
                    raise AccountsError(
                        429, "too many active sessions")
                raw = secrets.token_urlsafe(32)
                conn.execute(
                    "INSERT INTO auth_sessions(token_hash,user_id,"
                    "expires_at) VALUES(?,?,?)",
                    (_hash_token(raw), row["id"], now + SESSION_TTL))
            return raw, Principal(
                user_id=row["id"], email=row["email"],
                name=row["name"])
        finally:
            conn.close()

    def authenticate(self, raw_token: str, *, api_token: bool = False
                     ) -> Principal | None:
        if not isinstance(raw_token, str) or not raw_token \
                or len(raw_token) > 512:
            return None
        th = _hash_token(raw_token)
        now = time.time()
        conn = self._connect()
        try:
            if api_token:
                row = conn.execute(
                    "SELECT t.*, u.email, u.name AS uname,"
                    " u.disabled, m.role AS member_role"
                    " FROM api_tokens t JOIN users u ON u.id=t.user_id"
                    " LEFT JOIN memberships m"
                    " ON m.workspace_id=t.workspace_id"
                    " AND m.user_id=t.user_id"
                    " WHERE t.token_hash=?", (th,)).fetchone()
                if row is None or row["revoked_at"] \
                        or row["expires_at"] <= now \
                        or row["disabled"] or row["member_role"] is None:
                    return None
                effective = min(
                    _ROLE_RANK[row["role"]],
                    _ROLE_RANK[row["member_role"]])
                role = next(
                    r for r, v in _ROLE_RANK.items() if v == effective)
                return Principal(
                    user_id=row["user_id"], email=row["email"],
                    name=row["uname"], workspace_id=row["workspace_id"],
                    token_role=role, token_id=row["id"])
            row = conn.execute(
                "SELECT s.user_id, s.expires_at, u.email, u.name,"
                " u.disabled FROM auth_sessions s"
                " JOIN users u ON u.id=s.user_id"
                " WHERE s.token_hash=?", (th,)).fetchone()
            if row is None or row["expires_at"] <= now \
                    or row["disabled"]:
                return None
            return Principal(
                user_id=row["user_id"], email=row["email"],
                name=row["name"])
        finally:
            conn.close()

    def logout(self, raw_token: str) -> None:
        if not isinstance(raw_token, str) or not raw_token \
                or len(raw_token) > 512:
            return
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM auth_sessions WHERE token_hash=?",
                    (_hash_token(raw_token),))
        finally:
            conn.close()

    def _membership(self, conn, user_id: str,
                    workspace_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT m.role, w.* FROM memberships m"
            " JOIN workspaces w ON w.id=m.workspace_id"
            " WHERE m.workspace_id=? AND m.user_id=?",
            (workspace_id, user_id)).fetchone()

    def workspaces(self, principal: Principal) -> list[dict]:
        conn = self._connect()
        try:
            if principal.token_id is not None:
                row = conn.execute(
                    "SELECT w.*, m.role FROM memberships m"
                    " JOIN workspaces w ON w.id=m.workspace_id"
                    " WHERE m.workspace_id=? AND m.user_id=?",
                    (principal.workspace_id, principal.user_id)
                ).fetchone()
                if row is None:
                    return []
                return [_ws_dict(row, principal.token_role)]
            rows = conn.execute(
                "SELECT w.*, m.role FROM memberships m"
                " JOIN workspaces w ON w.id=m.workspace_id"
                " WHERE m.user_id=? ORDER BY w.created_at,w.id",
                (principal.user_id,)).fetchall()
            return [_ws_dict(r, r["role"]) for r in rows]
        finally:
            conn.close()

    def _ws_store(self, storage_key: str) -> Store:
        if storage_key == "legacy":
            path = self.legacy_path
        else:
            if not all(c in "0123456789abcdef" for c in storage_key) \
                    or len(storage_key) != 32:
                raise AccountsError(404, "workspace not found")
            path = (self.home / "workspaces" / storage_key
                    / "partial.db")
        with self._lock:
            store = self._stores.get(storage_key)
            if store is None:
                path.parent.mkdir(mode=0o700, parents=True,
                                  exist_ok=True)
                store = Store(path)
                self._stores[storage_key] = store
            return store

    def _resolve_ws(self, principal: Principal,
                    workspace_id: str | None, conn) -> tuple:
        if principal.token_id is not None:
            if workspace_id is not None \
                    and workspace_id != principal.workspace_id:
                raise AccountsError(404, "workspace not found")
            ws_id = principal.workspace_id
        elif workspace_id is not None:
            ws_id = workspace_id
        else:
            row = conn.execute(
                "SELECT w.*, m.role FROM memberships m"
                " JOIN workspaces w ON w.id=m.workspace_id"
                " WHERE m.user_id=? ORDER BY w.created_at,w.id"
                " LIMIT 1", (principal.user_id,)).fetchone()
            if row is None:
                raise AccountsError(404, "workspace not found")
            return row, row["role"]
        row = self._membership(conn, principal.user_id, ws_id)
        if row is None:
            raise AccountsError(404, "workspace not found")
        return row, row["role"]

    def workspace_store(
        self, principal: Principal, workspace_id: str | None, *,
        write: bool = False, admin: bool = False, owner: bool = False,
    ) -> tuple[Store, dict]:
        if principal.token_id is not None and (admin or owner):
            raise AccountsError(403, "API tokens cannot administer")
        conn = self._connect()
        try:
            row, member_role = self._resolve_ws(
                principal, workspace_id, conn)
            if principal.token_id is not None:
                rank = min(_ROLE_RANK[principal.token_role],
                           _ROLE_RANK[member_role])
            else:
                rank = _ROLE_RANK[member_role]
            if owner and rank < _ROLE_RANK["owner"]:
                raise AccountsError(403, "owner role required")
            if admin and rank < _ROLE_RANK["admin"]:
                raise AccountsError(403, "admin role required")
            if write and rank < _ROLE_RANK["member"]:
                raise AccountsError(403, "member role required")
            return self._ws_store(row["storage_key"]), _ws_dict(
                row, member_role)
        finally:
            conn.close()

    def create_workspace(self, principal: Principal,
                         name: str) -> dict:
        _browser_only(principal)
        name = _valid_name(name)
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                ws_id, key = _new_id(), _new_id()
                conn.execute(
                    "INSERT INTO workspaces(id,name,storage_key,"
                    "created_at) VALUES(?,?,?,?)",
                    (ws_id, name, key, now_iso()))
                conn.execute(
                    "INSERT INTO memberships(workspace_id,user_id,"
                    "role) VALUES(?,?,'owner')",
                    (ws_id, principal.user_id))
                self._audit(conn, ws_id, principal.user_id,
                            "workspace.create", ws_id)
            path = self.home / "workspaces" / key / "partial.db"
            with self._lock:
                self._stores[key] = Store(path)
            return {"id": ws_id, "name": name, "role": "owner"}
        finally:
            conn.close()

    def invite(self, principal: Principal, workspace_id: str,
               email: str, role: str) -> dict:
        _browser_only(principal)
        email = _valid_email(email)
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row, my_role = self._resolve_ws(
                    principal, workspace_id, conn)
                if _ROLE_RANK[my_role] < _ROLE_RANK["admin"]:
                    raise AccountsError(403, "admin role required")
                if my_role != "owner":
                    role = _valid_role(role, ("member", "viewer"))
                else:
                    role = _valid_role(role)
                raw = secrets.token_urlsafe(32)
                iid = _new_id()
                exp = time.time() + INVITE_TTL
                conn.execute(
                    "INSERT INTO invites(id,token_hash,email,"
                    "workspace_id,role,created_by,expires_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (iid, _hash_token(raw), email, workspace_id,
                     role, principal.user_id, exp))
                self._audit(conn, workspace_id, principal.user_id,
                            "invite.create", iid)
            return {"id": iid, "token": raw, "email": email,
                    "role": role, "workspace_id": workspace_id,
                    "expires_at": exp}
        finally:
            conn.close()

    def accept_invite(self, *, token: str, email: str, name: str,
                      password: str,
                      principal: Principal | None = None) -> dict:
        if not isinstance(token, str) or len(token) > 512:
            raise AccountsError(400, "invalid invite token")
        if principal is not None:
            _browser_only(principal)
        email = _valid_email(email)
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                inv = conn.execute(
                    "SELECT * FROM invites WHERE token_hash=?",
                    (_hash_token(token),)).fetchone()
                if inv is None or inv["used_at"] \
                        or inv["expires_at"] <= time.time():
                    raise AccountsError(400, "invite invalid or expired")
                inviter = conn.execute(
                    "SELECT role FROM memberships WHERE workspace_id=?"
                    " AND user_id=?",
                    (inv["workspace_id"], inv["created_by"])
                ).fetchone()
                if inviter is None \
                        or _ROLE_RANK[inviter["role"]] \
                        < _ROLE_RANK["admin"] \
                        or (inviter["role"] != "owner" and inv["role"]
                            not in ("member", "viewer")):
                    raise AccountsError(
                        400, "invite invalid or expired")
                if inv["email"] != email:
                    raise AccountsError(400, "invite email mismatch")
                if principal is not None:
                    if principal.email != email:
                        raise AccountsError(
                            400, "invite email mismatch")
                    uid = principal.user_id
                else:
                    existing = conn.execute(
                        "SELECT id FROM users WHERE email=?",
                        (email,)).fetchone()
                    if existing:
                        raise AccountsError(
                            409, "account exists; sign in to accept")
                    user = self._create_user(
                        conn, email, _valid_name(name),
                        _valid_password(password))
                    uid = user["id"]
                if conn.execute(
                        "SELECT 1 FROM memberships WHERE workspace_id=?"
                        " AND user_id=?",
                        (inv["workspace_id"], uid)).fetchone():
                    raise AccountsError(
                        409, "already a member of this workspace")
                conn.execute(
                    "INSERT INTO memberships(workspace_id,user_id,"
                    "role) VALUES(?,?,?)",
                    (inv["workspace_id"], uid, inv["role"]))
                conn.execute(
                    "UPDATE invites SET used_at=? WHERE id=?",
                    (time.time(), inv["id"]))
                self._audit(conn, inv["workspace_id"], uid,
                            "invite.accept", inv["id"])
            return {"ok": True, "workspace_id": inv["workspace_id"]}
        finally:
            conn.close()

    def members(self, principal: Principal,
                workspace_id: str) -> list[dict]:
        _browser_only(principal)
        conn = self._connect()
        try:
            row, _role = self._resolve_ws(principal, workspace_id, conn)
            rows = conn.execute(
                "SELECT u.id, u.email, u.name, u.created_at, m.role"
                " FROM memberships m JOIN users u ON u.id=m.user_id"
                " WHERE m.workspace_id=? ORDER BY u.email",
                (workspace_id,)).fetchall()
            return [{
                "user_id": r["id"], "email": r["email"],
                "name": r["name"], "role": r["role"],
                "created_at": r["created_at"],
            } for r in rows]
        finally:
            conn.close()

    def _owner_count(self, conn, workspace_id: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) c FROM memberships WHERE workspace_id=?"
            " AND role='owner'", (workspace_id,)).fetchone()["c"]

    def _check_member_admin(self, conn, principal, workspace_id,
                            target_id, role_change=None):
        row, my_role = self._resolve_ws(
            principal, workspace_id, conn)
        if _ROLE_RANK[my_role] < _ROLE_RANK["admin"]:
            raise AccountsError(403, "admin role required")
        target = conn.execute(
            "SELECT role FROM memberships WHERE workspace_id=?"
            " AND user_id=?", (workspace_id, target_id)).fetchone()
        if target is None:
            raise AccountsError(404, "member not found")
        if role_change is not None and role_change == target["role"]:
            return row
        my_rank = _ROLE_RANK[my_role]
        if my_role != "owner" and (
                _ROLE_RANK[target["role"]] >= my_rank
                or (role_change is not None
                    and _ROLE_RANK[role_change] >= my_rank)):
            raise AccountsError(
                403, "insufficient role for this member change")
        if target["role"] == "owner" and self._owner_count(
                conn, workspace_id) <= 1:
            raise AccountsError(409, "cannot remove the last owner")
        return row

    def change_member(self, principal: Principal, workspace_id: str,
                      user_id: str, role: str) -> None:
        _browser_only(principal)
        role = _valid_role(role)
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                self._check_member_admin(
                    conn, principal, workspace_id, user_id,
                    role_change=role)
                conn.execute(
                    "UPDATE memberships SET role=? WHERE"
                    " workspace_id=? AND user_id=?",
                    (role, workspace_id, user_id))
                self._audit(conn, workspace_id, principal.user_id,
                            "member.role", user_id)
        finally:
            conn.close()

    def remove_member(self, principal: Principal, workspace_id: str,
                      user_id: str) -> None:
        _browser_only(principal)
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                self._check_member_admin(
                    conn, principal, workspace_id, user_id)
                conn.execute(
                    "DELETE FROM memberships WHERE workspace_id=?"
                    " AND user_id=?", (workspace_id, user_id))
                conn.execute(
                    "UPDATE api_tokens SET revoked_at=? WHERE"
                    " workspace_id=? AND user_id=?",
                    (time.time(), workspace_id, user_id))
                self._audit(conn, workspace_id, principal.user_id,
                            "member.remove", user_id)
        finally:
            conn.close()

    def _token_cap(self, principal: Principal, member_role: str
                   ) -> int:
        if principal.token_id is not None:
            return _ROLE_RANK[principal.token_role]
        return _ROLE_RANK[member_role]

    def create_api_token(self, principal: Principal,
                         workspace_id: str, name: str, role: str,
                         expires_days: int = 90) -> dict:
        _browser_only(principal)
        name = _valid_name(name)
        role = _valid_role(role, ("member", "viewer"))
        try:
            days = int(expires_days)
        except (TypeError, ValueError):
            raise AccountsError(400, "invalid expires_days")
        if not 1 <= days <= 3650:
            raise AccountsError(400, "expires_days must be 1..3650")
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row, member_role = self._resolve_ws(
                    principal, workspace_id, conn)
                cap = self._token_cap(principal, member_role)
                if cap < _ROLE_RANK["viewer"]:
                    raise AccountsError(403, "membership required")
                if _ROLE_RANK[role] > cap:
                    raise AccountsError(
                        403, "token role cannot exceed your role")
                raw = "ptk_" + secrets.token_urlsafe(32)
                tid = _new_id()
                exp = time.time() + days * 86400
                conn.execute(
                    "INSERT INTO api_tokens(id,token_hash,user_id,"
                    "workspace_id,name,role,created_at,expires_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (tid, _hash_token(raw), principal.user_id,
                     workspace_id, name, role, now_iso(), exp))
                self._audit(conn, workspace_id, principal.user_id,
                            "token.create", tid)
            return {"id": tid, "token": raw, "name": name,
                    "role": role, "workspace_id": workspace_id,
                    "expires_at": exp}
        finally:
            conn.close()

    def list_api_tokens(self, principal: Principal,
                        workspace_id: str) -> list[dict]:
        _browser_only(principal)
        conn = self._connect()
        try:
            row, member_role = self._resolve_ws(
                principal, workspace_id, conn)
            if _ROLE_RANK[member_role] >= _ROLE_RANK["admin"]:
                rows = conn.execute(
                    "SELECT t.id,t.user_id,t.name,t.role,"
                    "t.created_at,t.expires_at,t.revoked_at,u.email"
                    " FROM api_tokens t JOIN users u"
                    " ON u.id=t.user_id WHERE t.workspace_id=?"
                    " ORDER BY t.created_at", (workspace_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT t.id,t.user_id,t.name,t.role,"
                    "t.created_at,t.expires_at,t.revoked_at,u.email"
                    " FROM api_tokens t JOIN users u"
                    " ON u.id=t.user_id WHERE t.workspace_id=?"
                    " AND t.user_id=? ORDER BY t.created_at",
                    (workspace_id, principal.user_id)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def revoke_api_token(self, principal: Principal,
                         token_id: str) -> None:
        _browser_only(principal)
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM api_tokens WHERE id=?",
                    (token_id,)).fetchone()
                if row is None:
                    raise AccountsError(404, "token not found")
                if row["user_id"] != principal.user_id:
                    _, member_role = self._resolve_ws(
                        principal, row["workspace_id"], conn)
                    if _ROLE_RANK[member_role] < _ROLE_RANK["admin"]:
                        raise AccountsError(
                            403, "admin role required")
                conn.execute(
                    "UPDATE api_tokens SET revoked_at=? WHERE id=?",
                    (time.time(), token_id))
                self._audit(conn, row["workspace_id"],
                            principal.user_id, "token.revoke",
                            token_id)
        finally:
            conn.close()

    def change_password(self, principal: Principal,
                        current_password: str,
                        new_password: str) -> None:
        _browser_only(principal)
        new_password = _valid_password(new_password)
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM users WHERE id=?",
                    (principal.user_id,)).fetchone()
                if row is None:
                    raise AccountsError(401, "invalid credentials")
                self._verify_user(conn, row["email"],
                                  current_password)
                salt = secrets.token_hex(16)
                conn.execute(
                    "UPDATE users SET password_hash=?, salt=?"
                    " WHERE id=?",
                    (_hash_password(new_password, salt), salt,
                     principal.user_id))
                conn.execute(
                    "DELETE FROM auth_sessions WHERE user_id=?",
                    (principal.user_id,))
                conn.execute(
                    "UPDATE api_tokens SET revoked_at=?"
                    " WHERE user_id=?",
                    (time.time(), principal.user_id))
        finally:
            conn.close()

    def list_audit(self, principal: Principal,
                   workspace_id: str) -> list[dict]:
        _browser_only(principal)
        conn = self._connect()
        try:
            row, member_role = self._resolve_ws(
                principal, workspace_id, conn)
            if _ROLE_RANK[member_role] < _ROLE_RANK["admin"]:
                raise AccountsError(403, "admin role required")
            rows = conn.execute(
                "SELECT id,workspace_id,actor_id,action,target_id,"
                "created_at FROM audit WHERE workspace_id=?"
                " ORDER BY created_at,id", (workspace_id,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
