"""/analizar output validation, response contract and request limits."""

import pytest

from tests.fakes import FailingGenerator, model_json

RESPONSE_FIELDS = {
    "emocion",
    "riesgo",
    "tematica",
    "resumen",
    "respuesta",
    "modismos",
    "guardrail_activado",
    "recursos_crisis",
    "procesado",
}

RAW_SECRET = "SECRETO-DEL-MODELO-123"
CRISIS_TEXT = "quiero suicidarme"
BENIGN_TEXT = "hoy fue un buen día"


def analizar(client, texto):
    return client.post("/analizar", json={"texto": texto})


def use_generator(generator):
    import psicolobot_api

    psicolobot_api.app.dependency_overrides[psicolobot_api.get_generator] = (
        lambda: generator
    )


# ─── Response contract ──────────────────────────────────────────


def test_success_response_has_exactly_the_documented_fields(
    client, fake_generator
):
    body = analizar(client, BENIGN_TEXT).json()

    assert set(body) == RESPONSE_FIELDS
    assert body["procesado"] is True


def test_openapi_documents_the_response_model(client):
    schema = client.get("/openapi.json").json()

    ok = schema["paths"]["/analizar"]["post"]["responses"]["200"]
    ref = ok["content"]["application/json"]["schema"]["$ref"]
    assert ref == "#/components/schemas/AnalysisResponse"

    model = schema["components"]["schemas"]["AnalysisResponse"]
    assert set(model["properties"]) == RESPONSE_FIELDS
    assert set(model["required"]) == RESPONSE_FIELDS
    props = model["properties"]
    assert props["riesgo"]["enum"] == ["BAJO", "MEDIO", "ALTO", "CRITICO"]
    for name in ("emocion", "tematica", "resumen", "respuesta"):
        assert props[name]["type"] == "string"
    assert props["modismos"]["type"] == "array"
    assert props["modismos"]["items"]["type"] == "string"
    assert props["guardrail_activado"]["type"] == "boolean"
    assert props["procesado"]["type"] == "boolean"
    assert props["recursos_crisis"]["type"] == "array"
    assert props["recursos_crisis"]["items"]["$ref"] == (
        "#/components/schemas/CrisisResource"
    )

    resource = schema["components"]["schemas"]["CrisisResource"]
    assert set(resource["properties"]) == {
        "nombre", "telefono", "marcar", "disponibilidad",
    }
    assert set(resource["required"]) == set(resource["properties"])


def test_openapi_documents_the_request_limits(client):
    schema = client.get("/openapi.json").json()

    texto = schema["components"]["schemas"]["PostRequest"]["properties"]["texto"]
    assert texto["maxLength"] == 4000


# ─── error_debug / raw model output never reaches the client ────


@pytest.mark.parametrize(
    "path",
    ["success", "unparseable", "truncated", "array", "exception", "empty"],
)
def test_error_debug_and_raw_output_are_never_returned(
    client, fake_generator, path
):
    if path == "success":
        fake_generator.respond_with(
            model_json(respuesta="Todo bien.", error_debug=RAW_SECRET)
        )
    elif path == "unparseable":
        fake_generator.respond_with(RAW_SECRET + " no es json")
    elif path == "truncated":
        fake_generator.respond_with('{"emocion": "miedo", "x": "' + RAW_SECRET)
    elif path == "array":
        fake_generator.respond_with(f'["{RAW_SECRET}"]')
    elif path == "empty":
        fake_generator.respond_with("")
    else:
        use_generator(FailingGenerator())

    response = analizar(client, BENIGN_TEXT)

    assert response.status_code == 200
    assert "error_debug" not in response.json()
    assert set(response.json()) == RESPONSE_FIELDS
    assert RAW_SECRET not in response.text


@pytest.mark.parametrize("path", ["unparseable", "exception", "success"])
def test_raw_model_output_is_never_logged(client, fake_generator, capsys, path):
    if path == "unparseable":
        fake_generator.respond_with(RAW_SECRET + " no es json")
    elif path == "success":
        fake_generator.respond_with(model_json(resumen=RAW_SECRET))
    else:
        use_generator(FailingGenerator())

    analizar(client, CRISIS_TEXT)

    captured = capsys.readouterr()
    assert RAW_SECRET not in captured.out + captured.err
    assert CRISIS_TEXT not in captured.out + captured.err


# ─── procesado per path ─────────────────────────────────────────


def test_procesado_is_true_for_a_usable_completion(client, fake_generator):
    assert analizar(client, BENIGN_TEXT).json()["procesado"] is True


@pytest.mark.parametrize(
    "output",
    ["esto no es json", "", "[1, 2, 3]", '"hola"', "null", '{"emocion": "x"'],
)
def test_procesado_is_false_when_the_output_is_unusable(
    client, fake_generator, output
):
    fake_generator.respond_with(output)

    body = analizar(client, BENIGN_TEXT).json()

    assert body["procesado"] is False
    assert body["riesgo"] == "MEDIO"


def test_procesado_is_false_when_the_generator_raises(client):
    use_generator(FailingGenerator())

    body = analizar(client, BENIGN_TEXT).json()

    assert body["procesado"] is False
    assert body["riesgo"] == "MEDIO"


def test_procesado_is_false_but_the_crisis_is_still_escalated(client):
    use_generator(FailingGenerator())

    body = analizar(client, CRISIS_TEXT).json()

    assert body["procesado"] is False
    assert body["riesgo"] == "CRITICO"
    assert body["recursos_crisis"]


# ─── Normalization through the API ──────────────────────────────


@pytest.mark.parametrize(
    "model_risk, expected",
    [("CRÍTICO", "CRITICO"), ("alta", "ALTO"), ("moderada", "MEDIO"),
     ("leve", "BAJO"), ("raro", "MEDIO"), (None, "MEDIO")],
)
def test_model_risk_labels_are_normalized(
    client, fake_generator, model_risk, expected
):
    fake_generator.respond_with(model_json(riesgo=model_risk))

    assert analizar(client, BENIGN_TEXT).json()["riesgo"] == expected


@pytest.mark.parametrize("model_risk", [None, "raro", "leve", 3])
def test_an_unusable_model_risk_never_lowers_a_crisis(
    client, fake_generator, model_risk
):
    fake_generator.respond_with(model_json(riesgo=model_risk))

    body = analizar(client, CRISIS_TEXT).json()

    assert body["riesgo"] == "CRITICO"
    assert body["procesado"] is True
    assert body["recursos_crisis"]


def test_fenced_output_with_prose_and_braces_is_processed(
    client, fake_generator
):
    fake_generator.respond_with(
        "Claro {sin problema}:\n```json\n"
        + model_json(emocion="Alegría", respuesta='Qué bien {"así"} se siente')
        + "\n```\nSaludos {fin}"
    )

    body = analizar(client, BENIGN_TEXT).json()

    assert body["procesado"] is True
    assert body["emocion"] == "alegria"
    assert body["respuesta"] == 'Qué bien {"así"} se siente'


def test_output_fields_are_capped_through_the_api(client, fake_generator):
    fake_generator.respond_with(
        model_json(
            tematica="t" * 500,
            resumen="r" * 900,
            respuesta="p" * 3000,
            modismos=["m" * 80] * 30,
        )
    )

    body = analizar(client, BENIGN_TEXT).json()

    assert len(body["tematica"]) == 200
    assert len(body["resumen"]) == 600
    assert len(body["respuesta"]) == 1500
    assert len(body["modismos"]) == 20
    assert all(len(m) == 60 for m in body["modismos"])


def test_empty_respuesta_is_replaced_by_a_warm_fallback(client, fake_generator):
    fake_generator.respond_with(model_json(respuesta="  "))

    body = analizar(client, BENIGN_TEXT).json()

    assert body["respuesta"].strip()
    assert body["procesado"] is True


# ─── Request validation ─────────────────────────────────────────


@pytest.mark.parametrize("texto", ["", "   ", "\n\t \n"])
def test_blank_text_is_rejected_with_422(client, fake_generator, texto):
    response = analizar(client, texto)

    assert response.status_code == 422
    assert fake_generator.calls == []


def test_text_over_4000_characters_is_rejected_with_422(client, fake_generator):
    response = analizar(client, "a" * 4001)

    assert response.status_code == 422
    assert fake_generator.calls == []


def test_text_of_exactly_4000_characters_is_accepted(client, fake_generator):
    response = analizar(client, "a" * 4000)

    assert response.status_code == 200
    assert response.json()["procesado"] is True


def test_a_crisis_phrase_at_the_end_of_a_4000_character_text_is_detected(
    client, fake_generator
):
    texto = "hoy fue un día normal. " * 175 + CRISIS_TEXT
    texto = texto[-4000:]
    assert len(texto) == 4000

    body = analizar(client, texto).json()

    assert body["riesgo"] == "CRITICO"
    assert body["recursos_crisis"]


@pytest.mark.parametrize("payload", [{}, {"texto": None}, {"texto": 5}, {"texto": ["a"]}])
def test_missing_or_non_string_text_is_rejected_with_422(client, payload):
    assert client.post("/analizar", json=payload).status_code == 422


def test_text_is_not_modified_by_validation(client, fake_generator):
    analizar(client, "  hola  ")

    assert fake_generator.user_content().endswith("Mensaje actual del usuario:\n  hola  ")
