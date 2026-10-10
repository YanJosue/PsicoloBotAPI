"""/analizar must escalate crisis text independently of the LLM.

The safety layer runs on the full raw text before the model is called and on
every path: model success, unparseable output and a raising generator.
"""

import json
import sqlite3

import pytest

import crisis_safety
from tests.fakes import model_json

HOTLINE = "Línea de la Vida: 800 911 2000"

EXPECTED_RESOURCES = [
    {
        "nombre": "Línea de la Vida",
        "telefono": "800 911 2000",
        "marcar": "8009112000",
        "disponibilidad": "24/7",
    },
    {
        "nombre": "SAPTEL",
        "telefono": "55 5259 8121",
        "marcar": "5552598121",
        "disponibilidad": "24/7",
    },
    {
        "nombre": "Emergencias",
        "telefono": "911",
        "marcar": "911",
        "disponibilidad": "24/7",
    },
]

REPHRASE_REPLY = (
    "Disculpa, no logré procesar bien tu mensaje. "
    "¿Podrías decirme con otras palabras cómo te sientes?"
)

CRISIS_TEXT = "quiero suicidarme"
HIGH_TEXT = "ya no aguanto"
BENIGN_TEXT = "hoy fue un buen día"


def analizar(client, texto):
    return client.post("/analizar", json={"texto": texto})


# ─── Model success path ─────────────────────────────────────────


def test_model_bajo_for_crisis_text_is_raised_to_critico_with_resources(
    client, fake_generator
):
    fake_generator.respond_with(
        model_json(riesgo="BAJO", respuesta="Qué bueno escucharlo.")
    )

    body = analizar(client, CRISIS_TEXT).json()

    assert body["riesgo"] == "CRITICO"
    assert body["guardrail_activado"] is True
    assert body["recursos_crisis"] == EXPECTED_RESOURCES
    assert HOTLINE in body["respuesta"]
    assert body["respuesta"].startswith("Qué bueno escucharlo.")


def test_alto_text_gets_resources_but_keeps_the_model_reply(
    client, fake_generator
):
    fake_generator.respond_with(
        model_json(riesgo="BAJO", respuesta="Aquí estoy contigo.")
    )

    body = analizar(client, HIGH_TEXT).json()

    assert body["riesgo"] == "ALTO"
    assert body["guardrail_activado"] is True
    assert body["recursos_crisis"] == EXPECTED_RESOURCES
    assert body["respuesta"] == "Aquí estoy contigo."


def test_benign_message_has_no_resources_and_no_guardrail(
    client, fake_generator
):
    fake_generator.respond_with(model_json(riesgo="BAJO"))

    body = analizar(client, BENIGN_TEXT).json()

    assert body["riesgo"] == "BAJO"
    assert body["recursos_crisis"] == []
    assert body["guardrail_activado"] is False


def test_model_critico_on_crisis_text_is_not_a_guardrail_activation(
    client, fake_generator
):
    fake_generator.respond_with(model_json(riesgo="CRITICO"))

    body = analizar(client, CRISIS_TEXT).json()

    assert body["riesgo"] == "CRITICO"
    assert body["guardrail_activado"] is False
    assert body["recursos_crisis"] == EXPECTED_RESOURCES


def test_model_alto_on_benign_text_returns_resources(client, fake_generator):
    fake_generator.respond_with(model_json(riesgo="ALTO"))

    body = analizar(client, BENIGN_TEXT).json()

    assert body["riesgo"] == "ALTO"
    assert body["guardrail_activado"] is False
    assert body["recursos_crisis"] == EXPECTED_RESOURCES


def test_model_medio_gets_no_resources(client, fake_generator):
    fake_generator.respond_with(model_json(riesgo="MEDIO"))

    body = analizar(client, BENIGN_TEXT).json()

    assert body["riesgo"] == "MEDIO"
    assert body["recursos_crisis"] == []


def test_model_risk_is_matched_case_insensitively(client, fake_generator):
    fake_generator.respond_with(model_json(riesgo="bajo"))

    body = analizar(client, CRISIS_TEXT).json()

    assert body["riesgo"] == "CRITICO"
    assert body["guardrail_activado"] is True


@pytest.mark.parametrize("bad_risk", [None, 7, ["CRITICO"], "SUPERALTO"])
def test_unusable_model_risk_never_hides_a_crisis(
    client, fake_generator, bad_risk
):
    fake_generator.respond_with(model_json(riesgo=bad_risk))

    response = analizar(client, CRISIS_TEXT)

    assert response.status_code == 200
    assert response.json()["riesgo"] == "CRITICO"


def test_empty_model_reply_is_replaced_by_the_safe_template(
    client, fake_generator
):
    fake_generator.respond_with(model_json(riesgo="BAJO", respuesta=""))

    body = analizar(client, CRISIS_TEXT).json()

    assert body["respuesta"] == crisis_safety.SAFE_REPLY_CRITICO


def test_crisis_phrase_after_the_prompt_truncation_point_is_still_detected(
    client, fake_generator
):
    import psicolobot_api

    text = "hoy fue un día normal. " * 100 + CRISIS_TEXT
    assert text.index("suicid") > 2000
    assert text.index("suicid") > psicolobot_api.MAX_INPUT_CHARS
    fake_generator.respond_with(model_json(riesgo="BAJO"))

    body = analizar(client, text).json()

    assert body["riesgo"] == "CRITICO"
    assert body["recursos_crisis"] == EXPECTED_RESOURCES
    # The prompt is still truncated; only the safety layer sees the full text.
    assert "suicid" not in fake_generator.user_content()


def test_safety_layer_runs_before_the_model_is_called(client, failing_generator):
    response = analizar(client, CRISIS_TEXT)

    # The generator was called (and raised), yet the answer is already safe.
    assert len(failing_generator.calls) == 1
    assert response.status_code == 200
    assert response.json()["riesgo"] == "CRITICO"


# ─── Generator raises ───────────────────────────────────────────


def test_generator_exception_with_crisis_text_returns_the_safe_critico_reply(
    client, failing_generator
):
    response = analizar(client, CRISIS_TEXT)

    assert response.status_code == 200
    body = response.json()
    assert body["riesgo"] == "CRITICO"
    assert body["guardrail_activado"] is True
    assert body["recursos_crisis"] == EXPECTED_RESOURCES
    assert body["respuesta"] == crisis_safety.SAFE_REPLY_CRITICO
    assert HOTLINE in body["respuesta"]


def test_generator_exception_with_alto_text_returns_the_safe_alto_reply(
    client, failing_generator
):
    body = analizar(client, HIGH_TEXT).json()

    assert body["riesgo"] == "ALTO"
    assert body["guardrail_activado"] is True
    assert body["recursos_crisis"] == EXPECTED_RESOURCES
    assert body["respuesta"] == crisis_safety.SAFE_REPLY_ALTO


def test_generator_exception_with_benign_text_is_a_medio_rephrase_request(
    client, failing_generator
):
    response = analizar(client, BENIGN_TEXT)

    assert response.status_code == 200
    body = response.json()
    assert body["riesgo"] == "MEDIO"
    assert body["respuesta"] == REPHRASE_REPLY
    assert body["recursos_crisis"] == []
    assert body["guardrail_activado"] is False


def test_generator_exception_text_never_reaches_the_client(
    client, failing_generator
):
    response = analizar(client, CRISIS_TEXT)

    assert "model crashed" not in response.text
    assert CRISIS_TEXT not in response.text


# ─── Unparseable model output ───────────────────────────────────


def test_malformed_output_with_crisis_text_returns_the_safe_critico_reply(
    client, fake_generator
):
    fake_generator.respond_with("esto no es json en absoluto")

    response = analizar(client, CRISIS_TEXT)

    assert response.status_code == 200
    body = response.json()
    assert body["riesgo"] == "CRITICO"
    assert body["guardrail_activado"] is True
    assert body["recursos_crisis"] == EXPECTED_RESOURCES
    assert body["respuesta"] == crisis_safety.SAFE_REPLY_CRITICO


def test_malformed_output_with_alto_text_returns_the_safe_alto_reply(
    client, fake_generator
):
    fake_generator.respond_with("esto no es json en absoluto")

    body = analizar(client, HIGH_TEXT).json()

    assert body["riesgo"] == "ALTO"
    assert body["recursos_crisis"] == EXPECTED_RESOURCES
    assert body["respuesta"] == crisis_safety.SAFE_REPLY_ALTO


def test_malformed_output_with_benign_text_keeps_the_rephrase_request(
    client, fake_generator
):
    fake_generator.respond_with("esto no es json en absoluto")

    body = analizar(client, BENIGN_TEXT).json()

    assert body["riesgo"] == "MEDIO"
    assert body["respuesta"] == REPHRASE_REPLY
    assert body["recursos_crisis"] == []
    assert body["guardrail_activado"] is False


@pytest.mark.parametrize("output", ["[1, 2, 3]", '"hola"', "null", "42", ""])
def test_json_that_is_not_an_object_is_treated_as_unparseable(
    client, fake_generator, output
):
    fake_generator.respond_with(output)

    response = analizar(client, CRISIS_TEXT)

    assert response.status_code == 200
    body = response.json()
    assert body["riesgo"] == "CRITICO"
    assert body["respuesta"] == crisis_safety.SAFE_REPLY_CRITICO


# ─── Memory continuity on the fallback paths ────────────────────


def test_unparseable_output_still_stores_the_interaction(
    client, db_path, fake_generator
):
    fake_generator.respond_with("no json", model_json())

    analizar(client, CRISIS_TEXT)
    analizar(client, "segundo mensaje")

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages ORDER BY id"
        ).fetchall()
    assert [r[0] for r in rows[:2]] == ["user", "assistant"]
    assert rows[0][1] == CRISIS_TEXT
    assert json.loads(rows[1][1]) == {
        "emocion": "No detectable",
        "respuesta": crisis_safety.SAFE_REPLY_CRITICO,
    }
    second = fake_generator.user_content(1)
    assert f'- Paciente dijo: "{CRISIS_TEXT}"' in second


def test_generator_exception_still_stores_the_interaction(
    client, db_path, failing_generator
):
    analizar(client, HIGH_TEXT)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages ORDER BY id"
        ).fetchall()
    assert [r[0] for r in rows] == ["user", "assistant"]
    assert rows[0][1] == HIGH_TEXT
    assert json.loads(rows[1][1])["respuesta"] == crisis_safety.SAFE_REPLY_ALTO


# ─── Privacy: user text is never logged ─────────────────────────


@pytest.mark.parametrize("path", ["success", "unparseable", "exception"])
def test_user_text_is_never_printed(
    client, fake_generator, capsys, path
):
    if path == "unparseable":
        fake_generator.respond_with("esto no es json")
    elif path == "exception":
        import psicolobot_api
        from tests.fakes import FailingGenerator

        psicolobot_api.app.dependency_overrides[psicolobot_api.get_generator] = (
            lambda: FailingGenerator()
        )

    analizar(client, CRISIS_TEXT)

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert CRISIS_TEXT not in output
    assert "suicid" not in output
    assert "model crashed" not in output
