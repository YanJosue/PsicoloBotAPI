"""Test doubles shared by the API tests."""

import json


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
