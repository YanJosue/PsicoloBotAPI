"""Shared fixtures: temporary DB, fake model generator, fake clock and clients.

None of these tests may require a GPU, torch or unsloth, so the real model is
always replaced through ``app.dependency_overrides``.

``anon_client`` sends no credentials (for the auth tests); ``client`` already
owns a consent-gated session and sends its Bearer token on every request.
"""

import pytest

from tests.fakes import (
    FailingGenerator,
    FakeClock,
    FakeGenerator,
    bearer,
    model_json,
    open_session,
    running_app,
)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Point the app at a throwaway SQLite file."""
    path = tmp_path / "chat_memory.db"
    monkeypatch.setenv("PSICOLOBOT_DB_PATH", str(path))
    return path


@pytest.fixture
def fake_generator():
    return FakeGenerator(model_json())


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def anon_client(db_path, fake_generator, clock):
    """Client without credentials."""
    with running_app(fake_generator, clock) as test_client:
        yield test_client


@pytest.fixture
def session_token(anon_client):
    return open_session(anon_client)


@pytest.fixture
def client(anon_client, session_token):
    """Client authenticated as one consenting session."""
    anon_client.headers.update(bearer(session_token))
    return anon_client


@pytest.fixture
def failing_generator(client):
    """Swap the fake generator for one that raises on every call."""
    import psicolobot_api

    generator = FailingGenerator()
    psicolobot_api.app.dependency_overrides[psicolobot_api.get_generator] = (
        lambda: generator
    )
    return generator
