"""Sign-in, users, roles, API keys and the audit log for the dashboard (docs/security.md, sections 1, 2, 9, 10).

Users live in a small SQLite database next to the runs (runs/.vocto/users.db):
  - passwords as scrypt hashes with a random salt (a stolen copy of the file can't be used to sign in);
  - sessions and API keys as SHA-256 hashes of random tokens (the token itself is shown once, then only
    its first characters);
  - roles: viewer (read), editor (run tests, edit test cases and agents), admin (users, secrets, API keys,
    and anything that makes the server run code or send a secret somewhere: see code_settings).
The first visit to a dashboard with no users creates the admin account.

Sign-in attempts are rate limited per address, and an account is locked for a while after repeated
failures. Every sign-in, failure, user change, key change, secret change and run started is appended
to runs/.vocto/audit.jsonl (never with a secret in it).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from ..ratelimit import RateLimiter
from ..util import file_lock

ROLES = ("viewer", "editor", "admin")
RANK = {r: i for i, r in enumerate(ROLES)}
USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{1,63}$")
MIN_PASSWORD = 10
COMMON = {"password", "password1", "password12", "password123", "1234567890", "qwertyuiop", "letmein123",
          "iloveyou12", "administrator", "welcome123", "vocto12345", "changeme123"}
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
SESSION_COOKIE = "vocto_session"
KEY_PREFIX = "vk_"
FAILS_BEFORE_LOCK = 5
LOCK_S = 15 * 60


class AuthError(Exception):
    """Refused: a wrong password, a bad value, or not allowed. `status` is the HTTP answer."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, maxmem=64 * 1024 * 1024, **SCRYPT)
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(digest) // 2, maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


_DUMMY = hash_password(secrets.token_hex(8))        # so an unknown user name takes as long as a wrong password


def password_problem(password: str, username: str = "") -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"use at least {MIN_PASSWORD} characters"
    if len(password) > 256:
        return "use at most 256 characters"
    low = password.lower()
    if low in COMMON or (username and username.lower() in low) or len(set(password)) < 4:
        return "that password is too easy to guess"
    return None


def at_least(role: str, needed: str) -> bool:
    return RANK.get(role, -1) >= RANK[needed]


# ---- what only an admin may change in a project's files ----------------------------------------------

def _secret_refs(value: Any, path: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(value, Mapping):
        for k, v in value.items():
            out += _secret_refs(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out += _secret_refs(v, f"{path}[{i}]")
    elif isinstance(value, str) and "${" in value:
        out.append(f"{path}={value}")
    return out


def code_settings(doc: Any) -> str:
    """The settings of one project file that make Vocto run code (start the agent, import a Python agent
    or tool) or that say where a secret is sent (${NAME} references, by where they are). An editor may
    change everything else; a change to any of these needs an admin."""
    if not isinstance(doc, Mapping):
        return "{}"
    conn = doc.get("connection") if isinstance(doc.get("connection"), Mapping) else {}
    tools = doc.get("tools") if isinstance(doc.get("tools"), list) else []
    out = {
        "secret_destinations": _secret_destinations(doc, conn, tools),
        "agent_process": doc.get("agent_process"),
        "launch": conn.get("launch"),
        "python_agent": conn.get("target") if conn.get("type") == "text_python" else None,
        "python_tools": sorted(f"{t.get('name')}={t.get('upstream')}" for t in tools
                               if isinstance(t, Mapping) and str(t.get("upstream", "")).startswith("python:")),
        "secrets": sorted(_secret_refs(doc)),
        "server_files": _server_files(doc),
    }
    return json.dumps(out, sort_keys=True, default=str)


def _server_files(doc: Mapping[str, Any]) -> list[str]:
    """Files on the server that Vocto reads or writes for the agent: a SQLite backend, a log file, fixture
    .sql files. Pointing one of these somewhere else reads that file back in the dashboard (a log test shows
    its lines, a probe its rows), so choosing them is an admin's change, like the secrets."""
    out = []
    backend = doc.get("backend") if isinstance(doc.get("backend"), Mapping) else {}
    if str(backend.get("connection", "")).startswith("sqlite:"):
        out.append(f"backend={backend.get('connection')}")
    logs = doc.get("logs") if isinstance(doc.get("logs"), Mapping) else {}
    if logs.get("source") == "file" or logs.get("path"):
        out.append(f"logs={logs.get('path')}")
    setup_ = doc.get("setup") if isinstance(doc.get("setup"), Mapping) else {}
    fixtures = setup_.get("fixtures") if isinstance(setup_.get("fixtures"), list) else []
    out += sorted(f"fixture={f}" for f in fixtures if isinstance(f, str) and f.strip().lower().endswith(".sql"))
    return out


def _secret_destinations(doc: Mapping[str, Any], conn: Mapping[str, Any], tools: list) -> dict[str, Any]:
    """Where a secret is sent: the address that goes with secret headers or keys. Moving that address (the agent's
    URL, a tool's upstream, the REST backend, the log server, a LiveKit server) would send the secret somewhere
    else, so it is an admin's change too. A hosted platform's key only ever goes to the platform's own host
    (connectors/platforms), so its agent id is anyone's to change; LiveKit's server address is not."""
    out: dict[str, Any] = {}
    if _secret_refs(conn):
        opts = conn.get("options") if isinstance(conn.get("options"), Mapping) else {}
        out["connection"] = {"type": conn.get("type"), "platform": conn.get("platform"),
                             "target": None if conn.get("type") in ("platform", "phone_call") else conn.get("target"),
                             "url": opts.get("url"), "trigger_url": (opts.get("outbound") or {}).get("trigger_url")
                             if isinstance(opts.get("outbound"), Mapping) else None,
                             "public_url": opts.get("public_url")}
    out["tools"] = sorted(f"{t.get('name')}={t.get('upstream')}" for t in tools
                          if isinstance(t, Mapping) and _secret_refs(t.get("headers") or {}))
    backend = doc.get("backend") if isinstance(doc.get("backend"), Mapping) else {}
    if _secret_refs(backend.get("headers") or {}):
        out["backend"] = backend.get("connection")
    logs = doc.get("logs") if isinstance(doc.get("logs"), Mapping) else {}
    if _secret_refs(logs.get("headers") or {}):
        out["logs"] = logs.get("url")
    return out


# ---- the store ---------------------------------------------------------------------------------------

class Auth:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.db_path = self.folder / "users.db"
        self.audit_path = self.folder / "audit.jsonl"
        self.session_days = _env_int("VOCTO_SESSION_DAYS", 14)          # longest a sign-in lasts
        self.idle_hours = _env_int("VOCTO_SESSION_IDLE_HOURS", 72)       # signed out after this long unused
        self.secure_cookies = os.environ.get("VOCTO_SECURE_COOKIES", "").lower() in ("1", "true", "yes", "on")
        self.login_limit = RateLimiter(_env_int("VOCTO_LOGIN_PER_MINUTE", 10), burst=_env_int("VOCTO_LOGIN_BURST", 10))
        self._fails: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL COLLATE NOCASE, name TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL, pw_hash TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, last_login REAL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL, last_seen REAL NOT NULL, ip TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS api_keys (
                    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL, prefix TEXT NOT NULL, key_hash TEXT UNIQUE NOT NULL, role TEXT NOT NULL,
                    created_at REAL NOT NULL, last_used REAL);
            """)
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA busy_timeout = 10000")
            yield db
        finally:
            db.close()

    # -- users --
    @staticmethod
    def _public(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        return {"id": row["id"], "username": row["username"], "name": row["name"], "role": row["role"],
                "disabled": bool(row["disabled"]), "created_at": row["created_at"], "last_login": row["last_login"]}

    def has_users(self) -> bool:
        with self._db() as db:
            return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def users(self) -> list[dict[str, Any]]:
        with self._db() as db:
            return [self._public(r) for r in db.execute("SELECT * FROM users ORDER BY username")]

    def user(self, user_id: int) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._public(row) if row else None

    def _check_new(self, username: str, password: str, role: str) -> None:
        if not USERNAME.match(username or ""):
            raise AuthError("the user name must be 2-64 letters, digits or . _ @ + -")
        if role not in ROLES:
            raise AuthError(f"the role must be one of {', '.join(ROLES)}")
        problem = password_problem(password, username)
        if problem:
            raise AuthError(f"password: {problem}")

    def create_user(self, username: str, password: str, role: str, name: str = "") -> dict[str, Any]:
        username = (username or "").strip()
        self._check_new(username, password, role)
        try:
            with self._db() as db:
                cur = db.execute("INSERT INTO users (username, name, role, pw_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                                 (username, (name or "").strip()[:100], role, hash_password(password), time.time()))
                return self.user(int(cur.lastrowid))  # type: ignore[return-value]
        except sqlite3.IntegrityError:
            raise AuthError(f"there is already a user called {username}") from None

    def setup_admin(self, username: str, password: str, name: str = "") -> dict[str, Any]:
        """The very first account, made on the first visit. Refused once any user exists."""
        username = (username or "").strip()
        self._check_new(username, password, "admin")
        with file_lock(self.db_path):
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                    db.execute("ROLLBACK")
                    raise AuthError("this dashboard already has an admin: sign in instead", 409)
                cur = db.execute("INSERT INTO users (username, name, role, pw_hash, created_at, last_login) "
                                 "VALUES (?, ?, 'admin', ?, ?, ?)",
                                 (username, (name or "").strip()[:100], hash_password(password), time.time(), time.time()))
                db.execute("COMMIT")
        return self.user(int(cur.lastrowid))  # type: ignore[return-value]

    def _admins_left(self, db: sqlite3.Connection, without: int) -> int:
        return db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0 AND id != ?",
                          (without,)).fetchone()[0]

    def update_user(self, user_id: int, *, role: str | None = None, disabled: bool | None = None,
                    name: str | None = None, password: str | None = None) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                raise AuthError("no such user", 404)
            if role is not None and role not in ROLES:
                raise AuthError(f"the role must be one of {', '.join(ROLES)}")
            demoted = (role is not None and role != "admin") or bool(disabled)
            if row["role"] == "admin" and demoted and not self._admins_left(db, user_id):
                raise AuthError("this is the last admin: make someone else an admin first")
            if password is not None:
                problem = password_problem(password, row["username"])
                if problem:
                    raise AuthError(f"password: {problem}")
                db.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (hash_password(password), user_id))
            if role is not None:
                db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
            if disabled is not None:
                db.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(bool(disabled)), user_id))
            if name is not None:
                db.execute("UPDATE users SET name = ? WHERE id = ?", (name.strip()[:100], user_id))
            if password is not None or disabled or role is not None:
                # A new password, a disabled account or a new role signs the person out everywhere.
                db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            if disabled:
                db.execute("DELETE FROM api_keys WHERE user_id = ?", (user_id,))
            elif role is not None:          # keys never outrank their owner
                db.execute("UPDATE api_keys SET role = ? WHERE user_id = ? AND "
                           "(CASE role WHEN 'admin' THEN 2 WHEN 'editor' THEN 1 ELSE 0 END) > ?",
                           (role, user_id, RANK[role]))
        return self.user(user_id)  # type: ignore[return-value]

    def delete_user(self, user_id: int) -> None:
        with self._db() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                raise AuthError("no such user", 404)
            if row["role"] == "admin" and not self._admins_left(db, user_id):
                raise AuthError("this is the last admin: make someone else an admin first")
            db.execute("DELETE FROM users WHERE id = ?", (user_id,))

    # -- signing in --
    def locked_for(self, username: str) -> float:
        with self._lock:
            n, until = self._fails.get(username.lower(), (0, 0.0))
        return max(0.0, until - time.time()) if n >= FAILS_BEFORE_LOCK else 0.0

    def login(self, username: str, password: str) -> dict[str, Any]:
        """The user, when the name and password are right. Locked after repeated failures."""
        key = (username or "").strip().lower()
        wait = self.locked_for(key)
        if wait:
            raise AuthError(f"too many wrong passwords: try again in {int(wait // 60) + 1} min", 429)
        with self._db() as db:
            row = db.execute("SELECT * FROM users WHERE username = ?", (key,)).fetchone()
        ok = check_password(password or "", row["pw_hash"] if row else _DUMMY) and row is not None
        if not ok or row["disabled"]:
            with self._lock:
                n, until = self._fails.get(key, (0, 0.0))
                n = 1 if until and until < time.time() and n >= FAILS_BEFORE_LOCK else n + 1
                self._fails[key] = (n, time.time() + LOCK_S)
                if len(self._fails) > 10000:
                    self._fails.clear()
            raise AuthError("wrong user name or password" if not ok else "this account is switched off", 401)
        with self._lock:
            self._fails.pop(key, None)
        with self._db() as db:
            now = time.time()
            db.execute("UPDATE users SET last_login = ? WHERE id = ?", (now, row["id"]))
        return {**self._public(row), "last_login": now}

    def new_session(self, user_id: int, ip: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._db() as db:
            db.execute("DELETE FROM sessions WHERE created_at < ? OR last_seen < ?",
                       (now - self.session_days * 86400, now - self.idle_hours * 3600))
            db.execute("INSERT INTO sessions (token_hash, user_id, created_at, last_seen, ip) VALUES (?, ?, ?, ?, ?)",
                       (_sha(token), user_id, now, now, ip[:64]))
        return token

    def session_user(self, token: str) -> dict[str, Any] | None:
        if not token or len(token) > 200:
            return None
        h, now = _sha(token), time.time()
        with self._db() as db:
            row = db.execute("SELECT s.created_at AS s_created, s.last_seen AS s_seen, u.* FROM sessions s "
                             "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?", (h,)).fetchone()
            if not row:
                return None
            if (row["disabled"] or row["s_created"] < now - self.session_days * 86400
                    or row["s_seen"] < now - self.idle_hours * 3600):
                db.execute("DELETE FROM sessions WHERE token_hash = ?", (h,))
                return None
            if now - self._seen.get(h, 0) > 60:              # write "last seen" at most once a minute
                self._seen[h] = now
                if len(self._seen) > 10000:
                    self._seen.clear()
                db.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, h))
        return {**self._public(row), "via": "session"}

    def end_session(self, token: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (_sha(token or ""),))

    def cookie(self, token: str, clear: bool = False) -> str:
        attrs = ["Path=/", "HttpOnly", "SameSite=Strict"] + (["Secure"] if self.secure_cookies else [])
        if clear:
            return f"{SESSION_COOKIE}=; Max-Age=0; " + "; ".join(attrs)
        return f"{SESSION_COOKIE}={token}; Max-Age={self.session_days * 86400}; " + "; ".join(attrs)

    # -- API keys --
    def new_key(self, user: Mapping[str, Any], name: str, role: str | None = None) -> dict[str, Any]:
        role = role or user["role"]
        if role not in ROLES:
            raise AuthError(f"the role must be one of {', '.join(ROLES)}")
        if not at_least(user["role"], role):
            raise AuthError("a key can't do more than its owner", 403)
        name = (name or "").strip()[:80]
        if not name:
            raise AuthError("give the key a name (what uses it, e.g. CI)")
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        with self._db() as db:
            if db.execute("SELECT COUNT(*) FROM api_keys WHERE user_id = ?", (user["id"],)).fetchone()[0] >= 50:
                raise AuthError("at most 50 keys per user: remove some first")
            cur = db.execute("INSERT INTO api_keys (user_id, name, prefix, key_hash, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                             (user["id"], name, key[:10], _sha(key), role, time.time()))
        return {"id": cur.lastrowid, "name": name, "role": role, "prefix": key[:10], "key": key}

    def keys(self, user_id: int | None = None) -> list[dict[str, Any]]:
        q = ("SELECT k.id, k.name, k.prefix, k.role, k.created_at, k.last_used, u.username FROM api_keys k "
             "JOIN users u ON u.id = k.user_id")
        with self._db() as db:
            rows = (db.execute(q + " WHERE k.user_id = ? ORDER BY k.id", (user_id,)) if user_id is not None
                    else db.execute(q + " ORDER BY k.id"))
            return [dict(r) for r in rows]

    def revoke_key(self, key_id: int, user: Mapping[str, Any]) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
            if not row or (row["user_id"] != user["id"] and user["role"] != "admin"):
                raise AuthError("no such key", 404)
            db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
        return {"id": key_id, "name": row["name"], "prefix": row["prefix"]}

    def key_user(self, key: str) -> dict[str, Any] | None:
        if not key.startswith(KEY_PREFIX) or len(key) > 200:
            return None
        with self._db() as db:
            row = db.execute("SELECT k.id AS key_id, k.role AS key_role, k.name AS key_name, k.last_used, u.* "
                             "FROM api_keys k JOIN users u ON u.id = k.user_id WHERE k.key_hash = ?", (_sha(key),)).fetchone()
            if not row or row["disabled"]:
                return None
            now = time.time()
            if not row["last_used"] or now - row["last_used"] > 60:
                db.execute("UPDATE api_keys SET last_used = ? WHERE id = ?", (now, row["key_id"]))
        user = self._public(row)
        # The key's role, never more than its owner's.
        user["role"] = min(row["key_role"], row["role"], key=lambda r: RANK.get(r, 0))
        return {**user, "via": "key", "key": row["key_name"]}

    # -- audit log --
    def audit(self, event: str, user: Mapping[str, Any] | None = None, ip: str = "", **details: Any) -> None:
        row = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event,
               "user": (user or {}).get("username"), "ip": ip, **{k: v for k, v in details.items() if v is not None}}
        line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
        with file_lock(self.audit_path):
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(line)

    def read_audit(self, limit: int = 500) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        with open(self.audit_path, "rb") as f:          # the last lines only: the log only grows
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 400 * limit))
            lines = f.read().decode("utf-8", "replace").splitlines()
        out = []
        for line in reversed(lines[-limit:]):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
