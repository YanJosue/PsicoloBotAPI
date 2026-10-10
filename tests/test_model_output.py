"""Unit tests for turning a raw model completion into a validated response.

Covers JSON extraction, risk/emotion normalization, field caps and the single
place where the final risk is computed (``build_response``). Nothing here needs
the FastAPI app or a model.
"""

import json

import pytest

from crisis_safety import (
    CRISIS_RESOURCES,
    SAFE_REPLY_ALTO,
    SAFE_REPLY_CRITICO,
    SafetyAssessment,
    assess,
)
from model_output import (
    ALLOWED_EMOTIONS,
    EMPTY_REPLY_FALLBACK,
    MODISMO_MAX,
    MODISMOS_MAX_ITEMS,
    REPHRASE_REPLY,
    RESPUESTA_MAX,
    RESUMEN_MAX,
    TEMATICA_MAX,
    AnalysisResponse,
    CrisisResource,
    build_response,
    extract_json_object,
    normalize_emotion,
    normalize_risk,
)
from tests.fakes import model_json

HOTLINE = "Línea de la Vida: 800 911 2000"
NO_RISK = SafetyAssessment(level="BAJO")
ALTO = SafetyAssessment(level="ALTO")
CRITICO = SafetyAssessment(level="CRITICO")

PAYLOAD = {
    "emocion": "tristeza",
    "riesgo": "BAJO",
    "tematica": "estado de ánimo",
    "resumen": "El usuario expresa tristeza.",
    "respuesta": "Aquí estoy contigo.",
    "modismos": [],
}


# ─── JSON extraction ────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps(PAYLOAD),
        "```json\n" + json.dumps(PAYLOAD) + "\n```",
        "```\n" + json.dumps(PAYLOAD) + "\n```",
        "Claro, aquí tienes el análisis:\n" + json.dumps(PAYLOAD),
        json.dumps(PAYLOAD) + "\nEspero que te sirva {de algo}.",
        "Antes\n```json\n" + json.dumps(PAYLOAD) + "\n```\nDespués",
        "   \n" + json.dumps(PAYLOAD) + "  \n",
    ],
    ids=[
        "plain",
        "json-fence",
        "bare-fence",
        "leading-prose",
        "trailing-prose-with-braces",
        "fence-and-prose",
        "whitespace",
    ],
)
def test_extracts_the_object_from_common_model_wrappers(raw):
    assert extract_json_object(raw) == PAYLOAD


def test_extracts_an_object_with_nested_braces():
    raw = 'x {"a": {"b": {"c": [1, {"d": 2}]}}, "e": 3} y'

    assert extract_json_object(raw) == {"a": {"b": {"c": [1, {"d": 2}]}}, "e": 3}


def test_braces_inside_strings_do_not_confuse_the_scanner():
    payload = dict(PAYLOAD, respuesta='Usa "{llaves}" y } sueltas { así')
    raw = "ok " + json.dumps(payload) + " fin"

    assert extract_json_object(raw) == payload


def test_escaped_quotes_inside_strings_are_handled():
    payload = dict(PAYLOAD, respuesta='Dijo "hola" y se fue \\ lejos}')
    raw = json.dumps(payload)

    assert extract_json_object(raw) == payload


def test_the_first_balanced_object_wins():
    raw = '{"emocion": "miedo"} y luego {"emocion": "ira"}'

    assert extract_json_object(raw) == {"emocion": "miedo"}


def test_a_brace_group_that_is_not_json_is_skipped():
    raw = 'Mira {esto no es json} pero {"emocion": "miedo"}'

    assert extract_json_object(raw) == {"emocion": "miedo"}


@pytest.mark.parametrize(
    "raw",
    [
        "esto no es json en absoluto",
        "",
        "   ",
        "{",
        '{"emocion": "miedo"',  # truncated: never "repair" it
        '{"emocion": "miedo", "modismos": [{"a": 1}',  # inner object is not top level
        "{'emocion': 'miedo'}",
        '{"emocion": "miedo",}',
        "[1, 2, 3]",
        '[{"emocion": "miedo", "riesgo": "BAJO"}]',  # array instead of object
        '"hola"',
        "null",
        "42",
        "true",
    ],
)
def test_unusable_completions_extract_to_none(raw):
    assert extract_json_object(raw) is None


@pytest.mark.parametrize("raw", [None, 7, ["{}"], {"a": 1}, b'{"a": 1}'])
def test_non_string_completions_extract_to_none(raw):
    assert extract_json_object(raw) is None


def test_pathological_nesting_is_unusable_not_a_crash():
    raw = '{"a":' * 3000 + "1" + "}" * 3000

    assert extract_json_object(raw) is None


# ─── Risk normalization ─────────────────────────────────────────


@pytest.mark.parametrize(
    "value, expected",
    [
        ("BAJO", "BAJO"),
        ("MEDIO", "MEDIO"),
        ("ALTO", "ALTO"),
        ("CRITICO", "CRITICO"),
        ("bajo", "BAJO"),
        ("  Alto  ", "ALTO"),
        ("CRÍTICO", "CRITICO"),
        ("crítico", "CRITICO"),
        ("Crítica", "CRITICO"),
        ("CRITICA", "CRITICO"),
        ("ALTA", "ALTO"),
        ("alta", "ALTO"),
        ("MEDIA", "MEDIO"),
        ("moderado", "MEDIO"),
        ("Moderada", "MEDIO"),
        ("BAJA", "BAJO"),
        ("leve", "BAJO"),
        ("Ninguno", "BAJO"),
        ("ninguna", "BAJO"),
    ],
)
def test_risk_labels_and_synonyms_map_to_the_four_levels(value, expected):
    assert normalize_risk(value) == expected


@pytest.mark.parametrize(
    "value",
    [None, 7, 1.5, True, ["CRITICO"], {"riesgo": "ALTO"}, "", "   ", "SUPERALTO",
     "CRITICO!", "riesgo alto"],
)
def test_unknown_or_non_string_risk_is_the_medio_floor(value):
    assert normalize_risk(value) == "MEDIO"


# ─── Emotion normalization ──────────────────────────────────────


def test_allowed_emotions_match_the_system_prompt_list():
    assert ALLOWED_EMOTIONS == frozenset(
        {
            "alegria", "amor", "asco", "culpa", "dolor", "enojo", "ira",
            "miedo", "orgullo", "tristeza", "verguenza",
        }
    )


@pytest.mark.parametrize(
    "value, expected",
    [
        ("tristeza", "tristeza"),
        ("TRISTEZA", "tristeza"),
        ("  Miedo ", "miedo"),
        ("alegría", "alegria"),
        ("Alegría", "alegria"),
        ("vergüenza", "verguenza"),
        ("VERGÜENZA", "verguenza"),
        ("verguenza", "verguenza"),
        ("ira", "ira"),
        ("ansiedad", "otra"),
        ("tristeza profunda", "otra"),
        ("", "otra"),
        ("   ", "otra"),
        (None, "otra"),
        (3, "otra"),
        (["miedo"], "otra"),
    ],
)
def test_emotion_is_lowercased_unaccented_and_limited_to_the_known_set(
    value, expected
):
    assert normalize_emotion(value) == expected


# ─── build_response: shape and procesado ────────────────────────


def test_a_clean_completion_is_processed_and_passes_through():
    response = build_response(json.dumps(PAYLOAD), NO_RISK)

    assert isinstance(response, AnalysisResponse)
    assert response.model_dump() == {
        "emocion": "tristeza",
        "riesgo": "BAJO",
        "riesgo_sesion": "BAJO",
        "tematica": "estado de ánimo",
        "resumen": "El usuario expresa tristeza.",
        "respuesta": "Aquí estoy contigo.",
        "modismos": [],
        "guardrail_activado": False,
        "recursos_crisis": [],
        "procesado": True,
    }


@pytest.mark.parametrize(
    "raw", [None, "", "no json", "[1, 2]", '"hola"', "null", '{"emocion": "miedo"']
)
def test_unusable_completion_is_flagged_not_processed(raw):
    response = build_response(raw, NO_RISK)

    assert response.procesado is False
    assert response.riesgo == "MEDIO"
    assert response.emocion == "No detectable"
    assert response.tematica == "No detectable"
    assert response.resumen == "No se pudo procesar la respuesta del modelo."
    assert response.respuesta == REPHRASE_REPLY
    assert response.modismos == []
    assert response.guardrail_activado is False
    assert response.recursos_crisis == []


def test_a_json_object_is_processed_even_when_its_fields_are_empty():
    response = build_response("{}", NO_RISK)

    assert response.procesado is True
    assert response.riesgo == "MEDIO"  # unknown model risk is the MEDIO floor
    assert response.emocion == "otra"
    assert response.tematica == ""
    assert response.resumen == ""
    assert response.modismos == []
    assert response.respuesta == EMPTY_REPLY_FALLBACK


def test_unusable_completion_on_crisis_text_uses_the_safe_template():
    critico = build_response("no json", CRITICO)
    alto = build_response(None, ALTO)

    assert (critico.riesgo, critico.respuesta) == ("CRITICO", SAFE_REPLY_CRITICO)
    assert (alto.riesgo, alto.respuesta) == ("ALTO", SAFE_REPLY_ALTO)
    assert critico.guardrail_activado and alto.guardrail_activado
    assert critico.procesado is False and alto.procesado is False
    assert [r.model_dump() for r in critico.recursos_crisis] == list(
        CRISIS_RESOURCES
    )


def test_response_resources_are_typed_crisis_resources():
    response = build_response(model_json(riesgo="ALTO"), NO_RISK)

    assert all(isinstance(r, CrisisResource) for r in response.recursos_crisis)
    assert [r.nombre for r in response.recursos_crisis] == [
        "Línea de la Vida", "SAPTEL", "Emergencias",
    ]


# ─── build_response: session risk (A4) ──────────────────────────
# Changed in A4: ``recursos_crisis`` follows max(riesgo, riesgo_sesion), not
# only the current ``riesgo``, so a calm follow-up message after a crisis
# still shows the help resources. With no prior session risk nothing changes.


def test_session_risk_defaults_to_the_final_risk():
    calm = build_response(model_json(riesgo="BAJO"), NO_RISK)
    crisis = build_response(model_json(riesgo="BAJO"), CRITICO)

    assert calm.riesgo_sesion == "BAJO"
    assert crisis.riesgo_sesion == "CRITICO"


@pytest.mark.parametrize("prior", ["ALTO", "CRITICO"])
def test_a_high_prior_session_risk_keeps_the_resources_on_a_calm_message(prior):
    response = build_response(
        model_json(riesgo="BAJO", respuesta="Me da gusto."), NO_RISK, prior
    )

    assert response.riesgo == "BAJO"
    assert response.riesgo_sesion == prior
    assert [r.model_dump() for r in response.recursos_crisis] == list(
        CRISIS_RESOURCES
    )
    # Only the resources carry over: the reply and the flags are the current
    # message's (no hotline appended, no guardrail activation).
    assert response.respuesta == "Me da gusto."
    assert response.guardrail_activado is False


@pytest.mark.parametrize("prior", ["BAJO", "MEDIO"])
def test_a_low_prior_session_risk_adds_no_resources(prior):
    response = build_response(model_json(riesgo="BAJO"), NO_RISK, prior)

    assert response.riesgo_sesion == prior
    assert response.recursos_crisis == []


def test_session_risk_never_drops_below_the_current_risk():
    response = build_response(model_json(riesgo="ALTO"), NO_RISK, "MEDIO")

    assert response.riesgo == "ALTO"
    assert response.riesgo_sesion == "ALTO"


@pytest.mark.parametrize("prior", [None, 7, "???", "", ["CRITICO"]])
def test_an_unusable_prior_session_risk_is_ignored(prior):
    response = build_response(model_json(riesgo="BAJO"), NO_RISK, prior)

    assert response.riesgo_sesion == "BAJO"
    assert response.recursos_crisis == []


def test_prior_session_risk_does_not_change_an_unprocessed_reply():
    response = build_response("no json", NO_RISK, "CRITICO")

    assert response.procesado is False
    assert response.riesgo == "MEDIO"
    assert response.riesgo_sesion == "CRITICO"
    assert response.respuesta == REPHRASE_REPLY
    assert response.recursos_crisis


# ─── build_response: the final risk is computed in one place ────


@pytest.mark.parametrize(
    "model_risk", [None, 7, ["CRITICO"], "SUPERALTO", "", "???", "bajo", "BAJA"]
)
def test_an_unusable_or_low_model_risk_never_lowers_a_crisis(model_risk):
    response = build_response(model_json(riesgo=model_risk), CRITICO)

    assert response.riesgo == "CRITICO"
    assert HOTLINE in response.respuesta
    assert response.recursos_crisis


@pytest.mark.parametrize("model_risk", [None, 7, "SUPERALTO", "bajo", "leve"])
def test_an_unusable_or_low_model_risk_never_lowers_alto(model_risk):
    response = build_response(model_json(riesgo=model_risk), ALTO)

    assert response.riesgo == "ALTO"
    assert response.guardrail_activado is True


def test_unknown_model_risk_without_a_crisis_is_medio_and_no_activation():
    response = build_response(model_json(riesgo="???"), NO_RISK)

    assert response.riesgo == "MEDIO"
    assert response.guardrail_activado is False


def test_model_synonym_raises_the_final_risk_like_the_canonical_label():
    response = build_response(model_json(riesgo="Crítica"), NO_RISK)

    assert response.riesgo == "CRITICO"
    assert response.guardrail_activado is False  # the model said it, not the net
    assert HOTLINE in response.respuesta


def test_safety_level_raises_the_model_risk_and_flags_the_guardrail():
    response = build_response(model_json(riesgo="leve"), assess("quiero morir"))

    assert response.riesgo == "CRITICO"
    assert response.guardrail_activado is True


def test_the_hotline_is_appended_once_and_only_for_critico():
    critico = build_response(model_json(riesgo="CRITICO", respuesta="Hola."), NO_RISK)
    again = build_response(
        model_json(riesgo="CRITICO", respuesta=f"Llama: {HOTLINE}."), NO_RISK
    )
    alto = build_response(model_json(riesgo="ALTO", respuesta="Hola."), NO_RISK)

    assert critico.respuesta.startswith("Hola.\n\n")
    assert critico.respuesta.count(HOTLINE) == 1
    assert again.respuesta == f"Llama: {HOTLINE}."
    assert alto.respuesta == "Hola."


# ─── build_response: emptiness, caps and types ──────────────────


@pytest.mark.parametrize("empty", ["", "   ", "\n\t ", None, 5, ["hola"]])
def test_empty_respuesta_gets_a_warm_generic_fallback(empty):
    response = build_response(model_json(respuesta=empty), NO_RISK)

    assert response.respuesta == EMPTY_REPLY_FALLBACK
    assert response.respuesta.strip()


def test_empty_respuesta_at_alto_or_critico_uses_the_safe_templates():
    alto = build_response(model_json(riesgo="ALTO", respuesta=""), NO_RISK)
    critico = build_response(model_json(riesgo="BAJO", respuesta=" "), CRITICO)

    assert alto.respuesta == SAFE_REPLY_ALTO
    assert critico.respuesta == SAFE_REPLY_CRITICO


def test_strings_are_trimmed():
    response = build_response(
        model_json(
            tematica="  trabajo \n", resumen="\tresumen  ", respuesta="  hola  "
        ),
        NO_RISK,
    )

    assert response.tematica == "trabajo"
    assert response.resumen == "resumen"
    assert response.respuesta == "hola"


def test_text_fields_are_capped():
    response = build_response(
        model_json(
            tematica="t" * (TEMATICA_MAX + 50),
            resumen="r" * (RESUMEN_MAX + 50),
            respuesta="p" * (RESPUESTA_MAX + 50),
        ),
        NO_RISK,
    )

    assert response.tematica == "t" * TEMATICA_MAX
    assert response.resumen == "r" * RESUMEN_MAX
    assert response.respuesta == "p" * RESPUESTA_MAX


def test_the_documented_caps():
    assert (TEMATICA_MAX, RESUMEN_MAX, RESPUESTA_MAX) == (200, 600, 1500)
    assert (MODISMOS_MAX_ITEMS, MODISMO_MAX) == (20, 60)


def test_a_capped_critico_reply_still_ends_with_the_hotline_and_fits_the_cap():
    response = build_response(
        model_json(riesgo="CRITICO", respuesta="p" * 5000), NO_RISK
    )

    assert len(response.respuesta) <= RESPUESTA_MAX
    assert HOTLINE in response.respuesta
    assert response.respuesta.startswith("ppp")


def test_modismos_are_filtered_trimmed_and_capped():
    modismos = (
        ["  chido ", 7, None, "", "   ", ["x"], {"a": 1}, "neta"]
        + ["m" * 100]
        + [f"mod{i}" for i in range(40)]
    )

    response = build_response(model_json(modismos=modismos), NO_RISK)

    assert response.modismos[:2] == ["chido", "neta"]
    assert response.modismos[2] == "m" * MODISMO_MAX
    assert len(response.modismos) == MODISMOS_MAX_ITEMS
    assert all(isinstance(m, str) and 0 < len(m) <= MODISMO_MAX for m in response.modismos)


@pytest.mark.parametrize("bad", [None, "chido", 5, {"a": "b"}])
def test_modismos_that_are_not_a_list_become_empty(bad):
    assert build_response(model_json(modismos=bad), NO_RISK).modismos == []


@pytest.mark.parametrize("bad", [None, 5, ["x"], {"a": 1}, True])
def test_non_string_text_fields_become_empty_strings(bad):
    response = build_response(model_json(tematica=bad, resumen=bad), NO_RISK)

    assert response.tematica == ""
    assert response.resumen == ""


def test_the_emotion_is_normalized_inside_the_response():
    assert build_response(model_json(emocion="Alegría"), NO_RISK).emocion == "alegria"
    assert build_response(model_json(emocion="ansiedad"), NO_RISK).emocion == "otra"


def test_unknown_extra_keys_from_the_model_never_reach_the_response():
    raw = model_json(error_debug="secret", prompt="system", extra=[1, 2])

    response = build_response(raw, NO_RISK)

    assert set(response.model_dump()) == {
        "emocion", "riesgo", "riesgo_sesion", "tematica", "resumen", "respuesta",
        "modismos", "guardrail_activado", "recursos_crisis", "procesado",
    }
