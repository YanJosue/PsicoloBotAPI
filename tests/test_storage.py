"""SQLite storage for sessions and messages (explicit ``now`` everywhere).

No FastAPI here: the storage functions take the current time as an argument,
so retention and the session-risk window are tested without any real clock.
"""

import hashlib
import sqlite3

import pytest

import session_store as storage

NOW = 1_800_000_000
DAY = 86_400
HOUR = 3_600
WINDOW = 24 * HOUR


def rows(db_path, query, params=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def new_session(now=NOW, days=30):
    token = storage.create_session("2026-10-v1", now, days)
    return token, storage.hash_token(token)


# ─── Schema ─────────────────────────────────────────────────────


def test_the_schema_has_sessions_and_messages_but_never_the_legacy_table(db_path):
    new_session()

    names = {r[0] for r in rows(db_path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "messages"} <= names
    assert "memory" not in names
    columns = [r[1] for r in rows(db_path, "PRAGMA table_info(sessions)")]
    assert columns == [
        "token_hash", "created_at", "expires_at", "consent_version",
        "max_risk", "max_risk_at",
    ]
    columns = [r[1] for r in rows(db_path, "PRAGMA table_info(messages)")]
    assert columns == ["id", "token_hash", "role", "content", "created_at"]


def test_connections_enforce_foreign_keys_and_overwrite_deleted_data(db_path):
    conn = storage.connect()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert conn.execute("PRAGMA secure_delete").fetchone() == (1,)
    finally:
        conn.close()


def test_messages_are_indexed_by_token_hash(db_path):
    new_session()

    indexes = rows(db_path, "PRAGMA index_list(messages)")
    first_columns = [
        rows(db_path, f"PRAGMA index_info({name})")[0][2] for _, name, *_ in indexes
    ]
    assert "token_hash" in first_columns


def test_the_legacy_memory_table_is_never_read_nor_modified(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE memory (session_id TEXT, role TEXT, content TEXT)")
    conn.execute("INSERT INTO memory VALUES ('default_session', 'user', 'texto viejo')")
    conn.commit()
    conn.close()

    token, digest = new_session()
    storage.record_exchange(digest, "hola", "{}", "BAJO", NOW, WINDOW)
    assert storage.recent_messages(digest, 4)[0]["content"] == "hola"
    storage.purge(NOW + 400 * DAY, 30)
    storage.delete_session(digest)

    assert rows(db_path, "SELECT * FROM memory") == [
        ("default_session", "user", "texto viejo")
    ]


# ─── Sessions ───────────────────────────────────────────────────


def test_a_session_stores_only_the_sha256_of_its_token(db_path):
    token, digest = new_session()

    assert digest == hashlib.sha256(token.encode()).hexdigest()
    assert rows(db_path, "SELECT token_hash FROM sessions") == [(digest,)]
    assert token.encode() not in db_path.read_bytes()


def test_a_session_records_consent_and_a_fixed_expiry(db_path):
    token, digest = new_session(days=7)

    assert rows(
        db_path,
        "SELECT created_at, expires_at, consent_version, max_risk, max_risk_at "
        "FROM sessions",
    ) == [(NOW, NOW + 7 * DAY, "2026-10-v1", "BAJO", None)]


def test_every_session_gets_a_different_token():
    tokens = {new_session()[0] for _ in range(5)}

    assert len(tokens) == 5


def test_get_session_finds_a_live_session_and_rejects_the_rest():
    token, digest = new_session()

    session = storage.get_session(token, NOW + 10)
    assert session is not None and session.token_hash == digest
    assert storage.get_session("otro-token", NOW) is None
    assert storage.get_session("", NOW) is None


def test_a_session_is_valid_until_the_second_before_it_expires():
    token, _ = new_session(days=30)

    assert storage.get_session(token, NOW + 30 * DAY - 1) is not None
    assert storage.get_session(token, NOW + 30 * DAY) is None


def test_delete_session_removes_the_session_and_only_its_messages(db_path):
    _, mine = new_session()
    _, other = new_session()
    storage.record_exchange(mine, "mío", "{}", "BAJO", NOW, WINDOW)
    storage.record_exchange(other, "ajeno", "{}", "BAJO", NOW, WINDOW)

    storage.delete_session(mine)

    assert rows(db_path, "SELECT token_hash FROM sessions") == [(other,)]
    assert {r[0] for r in rows(db_path, "SELECT token_hash FROM messages")} == {other}


def test_deleted_text_is_not_left_behind_in_the_database_file(db_path):
    _, digest = new_session()
    secret = "TEXTO-QUE-DEBE-DESAPARECER-" + "x" * 200
    storage.record_exchange(digest, secret, "{}", "BAJO", NOW, WINDOW)
    assert secret.encode() in db_path.read_bytes()

    storage.delete_session(digest)

    assert secret.encode() not in db_path.read_bytes()


# ─── Messages ───────────────────────────────────────────────────


def test_recent_messages_are_the_last_n_of_one_session_in_order():
    _, mine = new_session()
    _, other = new_session()
    for i in range(1, 4):
        storage.record_exchange(mine, f"u{i}", f"a{i}", "BAJO", NOW + i, WINDOW)
    storage.record_exchange(other, "ajeno", "ajeno", "BAJO", NOW, WINDOW)

    history = storage.recent_messages(mine, 4)

    assert [(m["role"], m["content"]) for m in history] == [
        ("user", "u2"), ("assistant", "a2"), ("user", "u3"), ("assistant", "a3"),
    ]
    assert storage.recent_messages(mine, 1) == [{"role": "assistant", "content": "a3"}]


def test_messages_keep_their_creation_time(db_path):
    _, digest = new_session()

    storage.record_exchange(digest, "u", "a", "BAJO", NOW + 5, WINDOW)

    assert rows(db_path, "SELECT role, created_at FROM messages ORDER BY id") == [
        ("user", NOW + 5), ("assistant", NOW + 5),
    ]


def test_recording_for_a_vanished_session_stores_nothing(db_path):
    _, digest = new_session()
    storage.delete_session(digest)

    stored = storage.record_exchange(digest, "texto", "{}", "CRITICO", NOW, WINDOW)

    assert stored is False
    assert rows(db_path, "SELECT * FROM messages") == []


# ─── Retention ──────────────────────────────────────────────────


def test_purge_removes_expired_sessions_with_their_messages(db_path):
    _, expired = new_session(now=NOW, days=30)
    _, alive = new_session(now=NOW + 20 * DAY, days=30)
    storage.record_exchange(expired, "viejo", "{}", "BAJO", NOW, WINDOW)
    storage.record_exchange(alive, "nuevo", "{}", "BAJO", NOW + 20 * DAY, WINDOW)

    storage.purge(NOW + 31 * DAY, 30)

    assert rows(db_path, "SELECT token_hash FROM sessions") == [(alive,)]
    assert rows(db_path, "SELECT content FROM messages ORDER BY id") == [
        ("nuevo",), ("{}",),
    ]


def test_purge_removes_messages_older_than_the_retention_window(db_path):
    _, digest = new_session(now=NOW, days=30)
    storage.record_exchange(digest, "muy viejo", "{}", "BAJO", NOW, WINDOW)
    storage.record_exchange(digest, "reciente", "{}", "BAJO", NOW + 9 * DAY, WINDOW)

    # The retention was shortened to 5 days: the session is still alive.
    storage.purge(NOW + 10 * DAY, 5)

    assert rows(db_path, "SELECT token_hash FROM sessions") == [(digest,)]
    assert {r[0] for r in rows(db_path, "SELECT content FROM messages")} == {
        "reciente", "{}"
    }


def test_creating_a_session_purges_what_expired(db_path):
    _, old = new_session(now=NOW, days=30)
    storage.record_exchange(old, "viejo", "{}", "BAJO", NOW, WINDOW)

    new_session(now=NOW + 31 * DAY, days=30)

    assert old not in {r[0] for r in rows(db_path, "SELECT token_hash FROM sessions")}
    assert rows(db_path, "SELECT * FROM messages") == []


# ─── Session risk ───────────────────────────────────────────────


def stored_risk(db_path, digest):
    return rows(
        db_path,
        "SELECT max_risk, max_risk_at FROM sessions WHERE token_hash=?",
        (digest,),
    )[0]


def test_a_new_session_has_no_risk_in_the_window():
    token, _ = new_session()

    assert storage.get_session(token, NOW).risk_in_window(NOW, WINDOW) == "BAJO"


def test_the_first_exchange_stores_its_risk_and_time(db_path):
    _, digest = new_session()

    storage.record_exchange(digest, "u", "a", "ALTO", NOW + 5, WINDOW)

    assert stored_risk(db_path, digest) == ("ALTO", NOW + 5)


def test_a_higher_or_equal_risk_replaces_the_stored_one(db_path):
    _, digest = new_session()
    storage.record_exchange(digest, "u", "a", "ALTO", NOW, WINDOW)

    storage.record_exchange(digest, "u", "a", "ALTO", NOW + HOUR, WINDOW)
    assert stored_risk(db_path, digest) == ("ALTO", NOW + HOUR)

    storage.record_exchange(digest, "u", "a", "CRITICO", NOW + 2 * HOUR, WINDOW)
    assert stored_risk(db_path, digest) == ("CRITICO", NOW + 2 * HOUR)


def test_a_lower_risk_inside_the_window_changes_nothing(db_path):
    _, digest = new_session()
    storage.record_exchange(digest, "u", "a", "CRITICO", NOW, WINDOW)

    storage.record_exchange(digest, "u", "a", "MEDIO", NOW + 2 * HOUR, WINDOW)

    assert stored_risk(db_path, digest) == ("CRITICO", NOW)


def test_a_lower_risk_replaces_a_stored_one_that_left_the_window(db_path):
    _, digest = new_session()
    storage.record_exchange(digest, "u", "a", "CRITICO", NOW, WINDOW)

    storage.record_exchange(digest, "u", "a", "BAJO", NOW + WINDOW, WINDOW)

    assert stored_risk(db_path, digest) == ("BAJO", NOW + WINDOW)


def test_risk_in_window_is_strictly_inside_the_window():
    token, digest = new_session()
    storage.record_exchange(digest, "u", "a", "CRITICO", NOW, WINDOW)
    session = storage.get_session(token, NOW)

    assert session.risk_in_window(NOW + WINDOW - 1, WINDOW) == "CRITICO"
    assert session.risk_in_window(NOW + WINDOW, WINDOW) == "BAJO"


def test_an_unknown_risk_label_is_stored_as_the_lowest(db_path):
    _, digest = new_session()

    storage.record_exchange(digest, "u", "a", "???", NOW, WINDOW)

    assert stored_risk(db_path, digest)[0] == "BAJO"


# ─── Configuration ──────────────────────────────────────────────


def test_the_configuration_defaults(monkeypatch):
    for name in (
        "PSICOLOBOT_RETENTION_DAYS",
        "PSICOLOBOT_SESSION_RISK_HOURS",
        "PSICOLOBOT_DB_PATH",
    ):
        monkeypatch.delenv(name, raising=False)

    assert storage.get_retention_days() == 30
    assert storage.get_session_risk_hours() == 24
    assert storage.get_db_path() == "chat_memory.db"


@pytest.mark.parametrize("value, expected", [("7", 7), (" 90 ", 90), ("0", 30), ("-3", 30), ("abc", 30), ("", 30), ("1.5", 30)])
def test_retention_days_comes_from_the_environment(monkeypatch, value, expected):
    monkeypatch.setenv("PSICOLOBOT_RETENTION_DAYS", value)

    assert storage.get_retention_days() == expected


@pytest.mark.parametrize("value, expected", [("6", 6), ("0", 24), ("x", 24)])
def test_session_risk_hours_comes_from_the_environment(monkeypatch, value, expected):
    monkeypatch.setenv("PSICOLOBOT_SESSION_RISK_HOURS", value)

    assert storage.get_session_risk_hours() == expected
