"""SQLite storage for consent-gated sessions and their messages.

Privacy design
    * A session token is a random secret handed to the app once; only its
      SHA-256 hex digest is stored, so a copy of the database cannot be used to
      impersonate anyone.
    * A session lives a fixed number of days (``PSICOLOBOT_RETENTION_DAYS``)
      from its creation (not sliding) and is then purged with its messages.
    * Deleting a session cascades to its messages, and ``secure_delete`` makes
      SQLite overwrite the freed pages instead of leaving the text in the file.
    * The legacy plaintext ``memory`` table of older versions is never read,
      migrated, created or dropped here; it must be deleted by hand.

Every function that depends on the time takes ``now`` (integer epoch seconds)
as an argument, so callers inject the clock and tests never sleep.
Nothing in this module logs; callers must not log tokens, hashes or text.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

from crisis_safety import RISK_ORDER, max_risk

DEFAULT_DB_PATH = "chat_memory.db"
DEFAULT_RETENTION_DAYS = 30
DEFAULT_SESSION_RISK_HOURS = 24

_DAY = 86_400
_LOWEST_RISK = "BAJO"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token_hash      TEXT PRIMARY KEY,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    consent_version TEXT NOT NULL,
    max_risk        TEXT NOT NULL DEFAULT 'BAJO',
    max_risk_at     INTEGER
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL REFERENCES sessions(token_hash) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_token_hash ON messages(token_hash, id);
"""

# SQL expression that ranks the stored risk label like ``RISK_ORDER`` (the
# labels are constants of this code base, never user input).
_RISK_RANK_SQL = (
    "CASE max_risk "
    + " ".join(f"WHEN '{label}' THEN {rank}" for label, rank in RISK_ORDER.items())
    + " ELSE 0 END"
)


# ─── Configuration (read on every call so tests and operators can change it) ──


def get_db_path() -> str:
    """SQLite file path (``PSICOLOBOT_DB_PATH``)."""
    return os.environ.get("PSICOLOBOT_DB_PATH", DEFAULT_DB_PATH)


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value >= 1 else default


def get_retention_days() -> int:
    """Days a session (and its messages) is kept, fixed from its creation."""
    return _positive_int_env("PSICOLOBOT_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)


def get_session_risk_hours() -> int:
    """Hours a session's highest risk is remembered."""
    return _positive_int_env(
        "PSICOLOBOT_SESSION_RISK_HOURS", DEFAULT_SESSION_RISK_HOURS
    )


# ─── Connection ─────────────────────────────────────────────────


def connect() -> sqlite3.Connection:
    """Open the database, creating the schema on first use."""
    conn = sqlite3.connect(get_db_path())
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA secure_delete = ON")
    conn.executescript(_SCHEMA)
    return conn


@contextmanager
def _transaction():
    """One connection, committed on success and rolled back on error."""
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


# ─── Sessions ───────────────────────────────────────────────────


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a session token (the only form ever stored)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Session:
    token_hash: str
    created_at: int
    expires_at: int
    consent_version: str
    max_risk: str
    max_risk_at: int | None

    def risk_in_window(self, now: int, window_seconds: int) -> str:
        """Highest risk seen in the last ``window_seconds``, else ``BAJO``."""
        if self.max_risk_at is None or now - self.max_risk_at >= window_seconds:
            return _LOWEST_RISK
        return max_risk(self.max_risk)


def purge(now: int, retention_days: int) -> None:
    """Delete expired sessions (with their messages) and messages past retention."""
    with _transaction() as conn:
        _purge(conn, now, retention_days)


def _purge(conn: sqlite3.Connection, now: int, retention_days: int) -> None:
    # Messages of a deleted session go with it (ON DELETE CASCADE).
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
    conn.execute(
        "DELETE FROM messages WHERE created_at < ?", (now - retention_days * _DAY,)
    )


def create_session(consent_version: str, now: int, retention_days: int) -> str:
    """Create a session and return its token (shown once, never stored).

    Expired data is purged first, in the same transaction.
    """
    token = secrets.token_urlsafe(32)
    with _transaction() as conn:
        _purge(conn, now, retention_days)
        conn.execute(
            "INSERT INTO sessions (token_hash, created_at, expires_at, consent_version) "
            "VALUES (?, ?, ?, ?)",
            (hash_token(token), now, now + retention_days * _DAY, consent_version),
        )
    return token


def get_session(token: str, now: int) -> Session | None:
    """The live session for ``token``, or None if unknown or expired."""
    with _transaction() as conn:
        row = conn.execute(
            "SELECT token_hash, created_at, expires_at, consent_version, "
            "max_risk, max_risk_at FROM sessions "
            "WHERE token_hash = ? AND expires_at > ?",
            (hash_token(token), now),
        ).fetchone()
    return Session(*row) if row else None


def delete_session(token_hash: str) -> None:
    """Delete a session and, by cascade, all of its messages."""
    with _transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


# ─── Messages and session risk ──────────────────────────────────


def recent_messages(token_hash: str, limit: int) -> list[dict]:
    """The last ``limit`` messages of one session, oldest first."""
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE token_hash = ? "
            "ORDER BY id DESC LIMIT ?",
            (token_hash, limit),
        ).fetchall()
    return [{"role": role, "content": content} for role, content in reversed(rows)]


def record_exchange(
    token_hash: str,
    user_text: str,
    assistant_text: str,
    risk: str,
    now: int,
    window_seconds: int,
) -> bool:
    """Store one user/assistant exchange and update the session's risk memory.

    The stored risk is replaced (with ``now``) only when ``risk`` is at least
    the risk still inside the window, so one atomic statement keeps the
    session maximum. Returns False, storing nothing, when the session no
    longer exists (for instance it was deleted while the model was running).
    """
    risk = max_risk(risk)
    try:
        with _transaction() as conn:
            conn.executemany(
                "INSERT INTO messages (token_hash, role, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                [
                    (token_hash, "user", user_text, now),
                    (token_hash, "assistant", assistant_text, now),
                ],
            )
            conn.execute(
                "UPDATE sessions SET max_risk = :risk, max_risk_at = :now "
                "WHERE token_hash = :hash AND (max_risk_at IS NULL "
                "OR max_risk_at <= :window_start "
                f"OR {_RISK_RANK_SQL} <= :rank)",
                {
                    "risk": risk,
                    "now": now,
                    "hash": token_hash,
                    "window_start": now - window_seconds,
                    "rank": RISK_ORDER[risk],
                },
            )
    except sqlite3.IntegrityError:
        return False
    return True
