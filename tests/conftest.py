"""Shared fixtures: temporary DB, fake model generator and a TestClient.

None of these tests may require a GPU, torch or unsloth, so the real model is
always replaced through ``app.dependency_overrides``.
"""

import pytest
from fastapi.testclient import TestClient

from tests.fakes import FakeGenerator, model_json


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
def client(db_path, fake_generator):
    import psicolobot_api

    psicolobot_api.app.dependency_overrides[psicolobot_api.get_generator] = (
        lambda: fake_generator
    )
    with TestClient(psicolobot_api.app) as test_client:
        yield test_client
    psicolobot_api.app.dependency_overrides.clear()
