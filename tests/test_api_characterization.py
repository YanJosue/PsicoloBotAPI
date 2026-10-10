"""Characterization tests: lock the current /analizar and /health behavior.

These pin the behavior of the API before the safety/privacy work so the
refactor can be proven behavior-preserving. The history is now scoped to the
caller's own consent-gated session (the shared ``session_id`` is gone); the
``client`` fixture already carries that session's Bearer token.
"""

import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path

from tests.fakes import model_json

REPO_ROOT = Path(__file__).resolve().parent.parent

HOTLINE = "Línea de la Vida: 800 911 2000"
HOTLINE_APPENDIX = (
    f"\n\n⚠️ Si sientes que estás en peligro, por favor llama a la {HOTLINE}."
    " No estás solo/a."
)


def analizar(client, texto):
    return client.post("/analizar", json={"texto": texto})


# ─── /health ────────────────────────────────────────────────────


def test_health_works_without_loading_the_model(client, fake_generator):
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["model"] == "psicolobot-v3.1"
    assert body["lexico_terms"] > 0
    assert body["model_loaded"] is False
    assert fake_generator.calls == []


def test_importing_the_app_does_not_import_the_ml_stack():
    code = (
        "import sys, psicolobot_api;"
        "assert 'unsloth' not in sys.modules, 'unsloth imported';"
        "assert 'torch' not in sys.modules, 'torch imported'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_lexicon_is_resolved_relative_to_the_module_not_the_cwd(
    client, tmp_path, monkeypatch
):
    import psicolobot_api

    monkeypatch.chdir(tmp_path)
    psicolobot_api.get_lexico.cache_clear()

    response = client.get("/health")

    assert response.json()["lexico_terms"] > 0


# ─── /analizar: happy path ──────────────────────────────────────


def test_analizar_returns_the_model_json_fields(client, fake_generator):
    fake_generator.respond_with(
        model_json(
            emocion="alegria",
            riesgo="BAJO",
            tematica="logro personal",
            resumen="Se siente orgulloso.",
            respuesta="Qué bueno escucharlo.",
            modismos=["chido"],
        )
    )

    response = analizar(client, "Hoy me fue muy chido")

    assert response.status_code == 200
    body = response.json()
    assert body["emocion"] == "alegria"
    assert body["riesgo"] == "BAJO"
    assert body["tematica"] == "logro personal"
    assert body["resumen"] == "Se siente orgulloso."
    assert body["respuesta"] == "Qué bueno escucharlo."
    assert body["modismos"] == ["chido"]
    assert not body.get("guardrail_activado")


def test_prompt_is_a_single_user_turn_with_the_system_prompt(
    client, fake_generator
):
    import psicolobot_api

    analizar(client, "Me siento cansado")

    messages = fake_generator.calls[0]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == psicolobot_api.SYSTEM_PROMPT
    assert messages[1]["content"].endswith(
        "Mensaje actual del usuario:\nMe siento cansado"
    )
    assert "Historial reciente" not in messages[1]["content"]


def test_lexical_context_lists_detected_emotions(client, fake_generator):
    analizar(client, "Siento mucho temor")

    user_content = fake_generator.user_content()
    assert user_content.startswith(
        "Contexto léxico: El motor de análisis detectó las siguientes emociones: ["
    )
    tags = user_content.split("[", 1)[1].split("]", 1)[0].split(", ")
    assert "miedo" in tags


def test_lexical_context_says_none_detected_when_nothing_matches(
    client, fake_generator
):
    analizar(client, "zzzz qqqq")

    assert "emociones: [ninguna detectada]" in fake_generator.user_content()


def test_input_text_is_truncated_to_the_maximum_length(client, fake_generator):
    import psicolobot_api

    analizar(client, "x" * (psicolobot_api.MAX_INPUT_CHARS + 500))

    sent = fake_generator.user_content().split("Mensaje actual del usuario:\n")[1]
    assert sent == "x" * psicolobot_api.MAX_INPUT_CHARS


# ─── /analizar: history ─────────────────────────────────────────


def test_history_from_a_previous_call_is_injected_into_the_next_prompt(
    client, fake_generator
):
    fake_generator.respond_with(
        model_json(emocion="tristeza", respuesta="Aquí estoy."),
        model_json(emocion="alegria", respuesta="Me alegra."),
    )

    analizar(client, "primer mensaje")
    analizar(client, "segundo mensaje")

    first, second = fake_generator.user_content(0), fake_generator.user_content(1)
    assert "Historial reciente" not in first
    assert "Historial reciente de la conversación (úsalo para dar continuidad):" in second
    assert '- Paciente dijo: "primer mensaje"' in second
    assert '- PsicoloBot respondió [emoción: tristeza]: "Aquí estoy."' in second
    assert second.endswith("Mensaje actual del usuario:\nsegundo mensaje")


def test_interaction_is_stored_in_the_configured_sqlite_file(
    client, db_path, session_token, fake_generator
):
    analizar(client, "hola")

    digest = hashlib.sha256(session_token.encode()).hexdigest()
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT token_hash, role FROM messages ORDER BY id"
        ).fetchall()
    assert rows == [(digest, "user"), (digest, "assistant")]


# ─── /analizar: guardrail ───────────────────────────────────────


def test_guardrail_upgrades_critical_phrase_and_appends_hotline(
    client, fake_generator
):
    fake_generator.respond_with(
        model_json(riesgo="BAJO", respuesta="Estoy contigo.")
    )

    body = analizar(client, "quiero matarme").json()

    assert body["riesgo"] == "CRITICO"
    assert body["guardrail_activado"] is True
    assert body["respuesta"] == "Estoy contigo." + HOTLINE_APPENDIX


def test_guardrail_upgrades_to_alto_without_the_hotline(client, fake_generator):
    fake_generator.respond_with(model_json(riesgo="BAJO"))

    body = analizar(client, "ya no aguanto").json()

    assert body["riesgo"] == "ALTO"
    assert body["guardrail_activado"] is True
    assert HOTLINE not in body["respuesta"]


def test_critical_model_risk_gets_hotline_without_guardrail_flag(
    client, fake_generator
):
    fake_generator.respond_with(
        model_json(riesgo="CRITICO", respuesta="Estoy contigo.")
    )

    body = analizar(client, "texto sin frases de riesgo").json()

    assert body["riesgo"] == "CRITICO"
    assert not body.get("guardrail_activado")
    assert body["respuesta"] == "Estoy contigo." + HOTLINE_APPENDIX


def test_hotline_is_not_duplicated_when_the_model_already_included_it(
    client, fake_generator
):
    reply = f"Llama a la {HOTLINE}."
    fake_generator.respond_with(model_json(riesgo="CRITICO", respuesta=reply))

    body = analizar(client, "quiero morir").json()

    assert body["respuesta"] == reply


def test_guardrail_never_lowers_a_higher_model_risk(client, fake_generator):
    fake_generator.respond_with(model_json(riesgo="CRITICO"))

    body = analizar(client, "ya no aguanto").json()

    assert body["riesgo"] == "CRITICO"


# ─── /analizar: JSON cleanup and parse failure ──────────────────


def test_code_fences_and_trailing_text_are_stripped_before_parsing(
    client, fake_generator
):
    fake_generator.respond_with(
        "```json\n" + model_json(emocion="miedo") + "\n```\nEspero que ayude."
    )

    body = analizar(client, "tengo nervios").json()

    assert body["emocion"] == "miedo"
    assert body["riesgo"] == "BAJO"


def test_malformed_model_output_returns_the_medio_fallback(
    client, fake_generator
):
    fake_generator.respond_with("esto no es json en absoluto")

    response = analizar(client, "me siento raro")

    assert response.status_code == 200
    body = response.json()
    assert body["riesgo"] == "MEDIO"
    assert body["emocion"] == "No detectable"
    assert body["tematica"] == "No detectable"
    assert body["resumen"] == "No se pudo procesar la respuesta del modelo."
    assert body["respuesta"] == (
        "Disculpa, no logré procesar bien tu mensaje. "
        "¿Podrías decirme con otras palabras cómo te sientes?"
    )
    assert body["modismos"] == []
    # Raw model output must never reach the client (removed in A3).
    assert "error_debug" not in body
    assert body["procesado"] is False


def test_parse_failure_still_stores_the_interaction(client, fake_generator):
    # Changed in A2 (was: "does not store"): a parse failure now keeps the
    # conversation continuity so a crisis message is never silently forgotten.
    fake_generator.respond_with("no json", model_json())

    analizar(client, "primer mensaje")
    analizar(client, "segundo mensaje")

    second = fake_generator.user_content(1)
    assert '- Paciente dijo: "primer mensaje"' in second
    assert "No detectable" in second
