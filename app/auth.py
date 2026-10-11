"""Personal accounts and sessions instead of one shared browser password.

Passwords are stored as PBKDF2-SHA256 hashes; a session cookie carries a random token whose
SHA-256 is kept server-side, so logout and deactivation take effect at once. Failed logins
are limited per address and per login. The first admin account ``psc`` is created from
PSC_PASSWORD when no accounts exist yet, so the existing pilot credentials keep working.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from app.store import Conflict, NotFound, Store

SESSION_COOKIE = "psc_session"
SESSION_HOURS = 12
PBKDF2_ITERATIONS = 390_000
MIN_PASSWORD = 10
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, display_name TEXT NOT NULL,
        password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','user')),
        active INTEGER NOT NULL DEFAULT 1, must_change INTEGER NOT NULL DEFAULT 0,
        password_source TEXT NOT NULL DEFAULT 'user', created_at TEXT NOT NULL, last_login_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id)",
    """CREATE TABLE IF NOT EXISTS auth_events (
        id INTEGER PRIMARY KEY, username TEXT NOT NULL, event TEXT NOT NULL, address TEXT NOT NULL,
        actor TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""",
]


class LoginRejected(ValueError):
    pass


def now() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str, *, salt: bytes | None = None, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(iterations, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def check_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt, digest = encoded.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


DUMMY_HASH = hash_password("dummy-password-for-timing", salt=b"psc-dummy-salt00")


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Пароль должен содержать не меньше {MIN_PASSWORD} символов")
    if password.isdigit() or len(set(password)) < 5:
        raise ValueError("Пароль слишком простой: используйте буквы, цифры и не повторяйте символы")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Throttle:
    """In-memory failure counter (one worker). Both the address and the login are limited."""

    def __init__(self, limit: int = MAX_FAILURES, window: int = LOCK_SECONDS, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.failures: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def _recent(self, key: str) -> list[float]:
        cutoff = self.clock() - self.window
        recent = [t for t in self.failures.get(key, []) if t > cutoff]
        if recent:
            self.failures[key] = recent
        else:
            self.failures.pop(key, None)
        return recent

    def blocked(self, *keys: str) -> bool:
        with self.lock:
            return any(len(self._recent(key)) >= self.limit for key in keys)

    def attempt(self, *keys: str) -> float | None:
        """Refuse a blocked attempt or count it, under one lock: a burst of parallel requests
        cannot all pass the check before any of them is recorded. None = blocked."""
        with self.lock:
            if any(len(self._recent(key)) >= self.limit for key in keys):
                return None
            stamp = self.clock()
            for key in keys:
                self.failures.setdefault(key, []).append(stamp)
            return stamp

    def succeeded(self, stamp: float, address_key: str, login_key: str) -> None:
        """A valid login: this attempt no longer counts for the address (earlier failures from it
        still do: logging into one's own account is no reset) and the login's own count is reset."""
        with self.lock:
            entries = self.failures.get(address_key, [])
            if stamp in entries:
                entries.remove(stamp)
            if not entries:
                self.failures.pop(address_key, None)
            self.failures.pop(login_key, None)

    def fail(self, *keys: str) -> None:
        with self.lock:
            for key in keys:
                self.failures.setdefault(key, []).append(self.clock())

    def clear(self, *keys: str) -> None:
        with self.lock:
            for key in keys:
                self.failures.pop(key, None)


class Auth:
    def __init__(self, store: Store, throttle: Throttle | None = None):
        self.store = store
        self.throttle = throttle or Throttle()

    def initialize(self, bootstrap_password: str = "") -> None:
        with self.store.connect(write=True) as db:
            for statement in SCHEMA:
                db.execute(statement)
            if not bootstrap_password:
                return
            if not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                db.execute("""INSERT INTO users(username,display_name,password_hash,role,password_source,created_at)
                    VALUES('psc','Администратор ПСК',?,'admin','env',?)""", (hash_password(bootstrap_password), now().isoformat()))
                self._event(db, "psc", "bootstrap_admin", "", "")
                return
            # Until the admin sets a password in the interface, PSC_PASSWORD stays the password of "psc".
            row = db.execute("SELECT * FROM users WHERE username='psc' AND password_source='env'").fetchone()
            if row and not check_password(bootstrap_password, row["password_hash"]):
                db.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(bootstrap_password), row["id"]))
                db.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))

    def has_users(self) -> bool:
        with self.store.connect() as db:
            return db.execute("SELECT 1 FROM users WHERE active=1 LIMIT 1").fetchone() is not None

    @staticmethod
    def _event(db, username: str, event: str, address: str, actor: str) -> None:
        db.execute("INSERT INTO auth_events(username,event,address,actor,created_at) VALUES(?,?,?,?,?)",
                   (username, event, address, actor, now().isoformat()))

    @staticmethod
    def _public(row) -> dict:
        return {"id": row["id"], "username": row["username"], "display_name": row["display_name"],
                "role": row["role"], "active": bool(row["active"]), "must_change": bool(row["must_change"]),
                "last_login_at": row["last_login_at"]}

    def authenticate(self, username: str, password: str, address: str) -> dict:
        username = username.strip()
        keys = ("addr:" + address, "user:" + username.casefold())
        stamp = self.throttle.attempt(*keys)
        if stamp is None:
            with self.store.connect(write=True) as db:
                self._event(db, username, "login_blocked", address, "")
            raise LoginRejected("Слишком много неудачных попыток. Подождите 15 минут.")
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        # An unknown login costs the same hash time, so timing does not tell which logins exist.
        valid = check_password(password, row["password_hash"] if row else DUMMY_HASH) and bool(row and row["active"])
        if not valid:  # already counted by attempt()
            with self.store.connect(write=True) as db:  # outside the failed path, so the record is kept
                self._event(db, username, "login_failed", address, "")
            raise LoginRejected("Неверный логин или пароль")
        self.throttle.succeeded(stamp, *keys)
        with self.store.connect(write=True) as db:
            db.execute("UPDATE users SET last_login_at=? WHERE id=?", (now().isoformat(), row["id"]))
            self._event(db, row["username"], "login", address, "")
        return self._public(row)

    def login(self, username: str, password: str, address: str) -> tuple[str, dict]:
        user = self.authenticate(username, password, address)
        token = secrets.token_urlsafe(32)
        created = now()
        with self.store.connect(write=True) as db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (created.isoformat(),))
            db.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                       (token_hash(token), user["id"], created.isoformat(),
                        (created + timedelta(hours=SESSION_HOURS)).isoformat()))
        return token, user

    def session_user(self, token: str) -> dict | None:
        if not token:
            return None
        with self.store.connect() as db:
            row = db.execute("""SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.token_hash=? AND s.expires_at > ? AND u.active=1""", (token_hash(token), now().isoformat())).fetchone()
        return self._public(row) if row else None

    def logout(self, token: str, address: str) -> None:
        with self.store.connect(write=True) as db:
            row = db.execute("""SELECT u.username FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.token_hash=?""", (token_hash(token),)).fetchone()
            db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))
            if row:
                self._event(db, row["username"], "logout", address, row["username"])

    def users(self) -> list[dict]:
        with self.store.connect() as db:
            return [self._public(r) for r in db.execute("SELECT * FROM users ORDER BY active DESC, username")]

    def create_user(self, username: str, display_name: str, role: str, password: str, actor: str) -> dict:
        username = username.strip()
        if not username or len(username) > 60 or any(c.isspace() for c in username):
            raise ValueError("Логин: от 1 до 60 символов без пробелов")
        if role not in {"admin", "user"}:
            raise ValueError("Неизвестная роль")
        validate_password(password)
        with self.store.connect(write=True) as db:
            if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise Conflict("Такой логин уже есть")
            cursor = db.execute("""INSERT INTO users(username,display_name,password_hash,role,must_change,created_at)
                VALUES(?,?,?,?,1,?)""", (username, display_name.strip() or username, hash_password(password), role, now().isoformat()))
            self._event(db, username, "user_created", "", actor)
            return self._public(db.execute("SELECT * FROM users WHERE id=?", (cursor.lastrowid,)).fetchone())

    def change_password(self, user_id: int, current: str, new: str, address: str) -> None:
        validate_password(new)
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not row or not check_password(current, row["password_hash"]):
                raise LoginRejected("Текущий пароль указан неверно")
            if check_password(new, row["password_hash"]):
                raise ValueError("Новый пароль должен отличаться от текущего")
            db.execute("UPDATE users SET password_hash=?, must_change=0, password_source='user' WHERE id=?",
                       (hash_password(new), user_id))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self._event(db, row["username"], "password_changed", address, row["username"])

    def reset_password(self, user_id: int, password: str, actor: str) -> None:
        validate_password(password)
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                raise NotFound("Пользователь не найден")
            db.execute("UPDATE users SET password_hash=?, must_change=1, password_source='user' WHERE id=?",
                       (hash_password(password), user_id))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self._event(db, row["username"], "password_reset", "", actor)

    def set_active(self, user_id: int, active: bool, actor_id: int, actor: str) -> dict:
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                raise NotFound("Пользователь не найден")
            if not active and user_id == actor_id:
                raise Conflict("Нельзя отключить собственную учётную запись")
            if not active and row["role"] == "admin" and db.execute(
                    "SELECT count(*) FROM users WHERE role='admin' AND active=1 AND id<>?", (user_id,)).fetchone()[0] == 0:
                raise Conflict("Должен остаться хотя бы один активный администратор")
            db.execute("UPDATE users SET active=? WHERE id=?", (int(active), user_id))
            if not active:
                db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self._event(db, row["username"], "user_enabled" if active else "user_disabled", "", actor)
            return self._public(db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def events(self, limit: int = 100) -> list[dict]:
        with self.store.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM auth_events ORDER BY id DESC LIMIT ?", (limit,))]
