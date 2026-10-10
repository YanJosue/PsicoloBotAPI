"""Test doubles and helpers shared by the API tests."""

import json
from contextlib import contextmanager

CONSENT = {
    "acepta_terminos": True,
    "mayor_de_edad": True,
    "version_consentimiento": "2026-10-v1",
}


class FakeClock:
    """Injectable clock (integer epoch seconds) that tests move by hand."""

    def __init__(self, start: int = 1_800_000_000):
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: int = 0, *, hours: int = 0, days: int = 0) -> None:
        self.now += seconds + hours * 3600 + days * 86400


class FakeGenerator:
    """Stand-in for the LLM generator that returns canned completions.

    Responses are consumed in order; the last one repeats once the queue is
    exhausted. Every ``messages`` list it receives is recorded in ``calls``.
    """

    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    def respond_with(self, *responses: str) -> None:
        self.responses = list(responses)

    def generate(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]

    def user_content(self, call_index: int = -1) -> str:
        """Content of the user message of a recorded call."""
        return next(
            m["content"] for m in self.calls[call_index] if m["role"] == "user"
        )


class FailingGenerator:
    """Generator whose ``generate`` always raises.

    The error message echoes the whole prompt, so tests can prove the API
    never logs or returns exception text (which may contain user text).
    """

    def __init__(self):
        self.calls: list[list[dict]] = []

    def generate(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        raise RuntimeError(f"model crashed while processing: {messages}")


def model_json(**overrides) -> str:
    """A well-formed model completion; override any field per test."""
    payload = {
        "emocion": "tristeza",
        "riesgo": "BAJO",
        "tematica": "estado de ánimo",
        "resumen": "El usuario expresa tristeza.",
        "respuesta": "Aquí estoy contigo.",
        "modismos": [],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def bearer(token: str) -> dict:
    """Authorization header for a session token."""
    return {"Authorization": f"Bearer {token}"}


def open_session(client, headers: dict | None = None, **overrides) -> str:
    """Create a session through ``POST /sesion`` and return its token."""
    response = client.post("/sesion", json={**CONSENT, **overrides}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["token"]


@contextmanager
def running_app(generator, clock):
    """The module-level app with a fake generator and clock, started once.

    Entering runs the startup (lifespan) code; leaving clears the overrides.
    """
    import psicolobot_api
    from fastapi.testclient import TestClient

    app = psicolobot_api.app
    app.dependency_overrides[psicolobot_api.get_generator] = lambda: generator
    app.dependency_overrides[psicolobot_api.get_now] = clock
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
