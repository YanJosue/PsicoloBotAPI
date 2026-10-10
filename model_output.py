"""Turn a raw model completion into the validated ``/analizar`` response.

The language model is untrusted: its output may be prose, truncated JSON, an
array, an unknown risk label or an enormous string. Everything the client sees
goes through ``build_response``, which is also the single place where the final
risk is computed (``max(model risk, safety level)``), so no code path can
return a lower risk than the safety layer asked for. The risk remembered from
earlier in the conversation (``riesgo_sesion``) is combined here too, and it
keeps the crisis resources on screen after a calm follow-up message.

Privacy: nothing in this module logs, and the raw completion is never copied
into the response. Fields the model invents beyond the documented ones are
dropped.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Literal

from pydantic import BaseModel, Field

from crisis_safety import (
    HOTLINE_SENTENCE,
    RISK_ORDER,
    SafetyAssessment,
    max_risk,
    resources_for,
    safe_reply_for,
)

# ─── Limits ─────────────────────────────────────────────────────
TEMATICA_MAX = 200
RESUMEN_MAX = 600
RESPUESTA_MAX = 1500
MODISMOS_MAX_ITEMS = 20
MODISMO_MAX = 60
# The model emits at most 350 tokens; anything far beyond that is not a
# completion we want to scan.
_RAW_SCAN_MAX = 20_000

# ─── Vocabulary ─────────────────────────────────────────────────
# Same list as SYSTEM_PROMPT, lower-case and without accents.
ALLOWED_EMOTIONS = frozenset(
    {
        "alegria", "amor", "asco", "culpa", "dolor", "enojo", "ira",
        "miedo", "orgullo", "tristeza", "verguenza",
    }
)
OTHER_EMOTION = "otra"

# Spellings a model may use instead of the four canonical labels (accents are
# already stripped and the text upper-cased when these are looked up).
_RISK_SYNONYMS = {
    "CRITICA": "CRITICO",
    "ALTA": "ALTO",
    "MEDIA": "MEDIO",
    "MODERADO": "MEDIO",
    "MODERADA": "MEDIO",
    "BAJA": "BAJO",
    "LEVE": "BAJO",
    "NINGUNO": "BAJO",
    "NINGUNA": "BAJO",
}
# A label nobody can read is never trusted to lower the risk.
_UNKNOWN_RISK = "MEDIO"

# ─── User-facing fallbacks (neutral Mexican Spanish) ────────────
REPHRASE_REPLY = (
    "Disculpa, no logré procesar bien tu mensaje. "
    "¿Podrías decirme con otras palabras cómo te sientes?"
)
EMPTY_REPLY_FALLBACK = (
    "Gracias por compartir lo que sientes conmigo. "
    "Aquí estoy para escucharte, ¿quieres contarme un poco más?"
)
_UNPROCESSED_LABEL = "No detectable"
_UNPROCESSED_SUMMARY = "No se pudo procesar la respuesta del modelo."
_HOTLINE_APPENDIX = (
    f"\n\n⚠️ Si sientes que estás en peligro, por favor llama a la {HOTLINE_SENTENCE}. "
    "No estás solo/a."
)


# ─── Response contract ──────────────────────────────────────────


class CrisisResource(BaseModel):
    """A crisis hotline the app can show and dial."""

    nombre: str
    telefono: str = Field(description="Number as displayed to the person.")
    marcar: str = Field(description="Digits only, ready for a tel: link.")
    disponibilidad: str


class AnalysisResponse(BaseModel):
    """Validated result of ``POST /analizar``."""

    emocion: str = Field(
        description="Main emotion: one of the known set, 'otra' when the model "
        "named something else, or 'No detectable' when procesado is false."
    )
    riesgo: Literal["BAJO", "MEDIO", "ALTO", "CRITICO"] = Field(
        description="Final risk: the higher of the model's estimate and the "
        "independent safety layer."
    )
    riesgo_sesion: Literal["BAJO", "MEDIO", "ALTO", "CRITICO"] = Field(
        description="Highest risk of this conversation within the memory "
        "window (24 h by default), including this message; never lower than "
        "riesgo."
    )
    tematica: str = Field(description=f"At most {TEMATICA_MAX} characters.")
    resumen: str = Field(description=f"At most {RESUMEN_MAX} characters.")
    respuesta: str = Field(description=f"At most {RESPUESTA_MAX} characters, never empty.")
    modismos: list[str] = Field(
        description=f"At most {MODISMOS_MAX_ITEMS} items of {MODISMO_MAX} characters."
    )
    guardrail_activado: bool = Field(
        description="True when the safety layer raised the model's risk."
    )
    recursos_crisis: list[CrisisResource] = Field(
        description="Hotlines to show; filled when riesgo or riesgo_sesion is "
        "ALTO or higher, else empty."
    )
    procesado: bool = Field(
        description="False when the model output could not be used (the model "
        "failed, or its answer was not a JSON object); the reply is then a "
        "safe fallback."
    )


# ─── JSON extraction ────────────────────────────────────────────


def _next_opener(text: str, position: int) -> int | None:
    """Index of the next ``{`` or ``[`` at or after ``position``, or None."""
    found = [i for i in (text.find("{", position), text.find("[", position)) if i >= 0]
    return min(found) if found else None


def _balanced_end(text: str, start: int) -> int | None:
    """Index just past the bracket group that opens at ``start``, or None.

    Brackets inside JSON strings (with escapes) are ignored. The group is only
    checked for balance here; ``json.loads`` decides whether it is valid.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def extract_json_object(raw) -> dict | None:
    """First balanced top-level JSON object in ``raw``, else None.

    Tolerates code fences and prose before or after the object, and braces
    inside strings. A brace group that is not valid JSON is skipped whole; an
    array is skipped whole too (JSON that is not an object is unusable, and an
    object nested in an array or in a truncated outer object is not top
    level). Truncated output is never repaired.
    """
    if not isinstance(raw, str):
        return None
    text = raw[:_RAW_SCAN_MAX]
    position = 0
    while True:
        start = _next_opener(text, position)
        if start is None:
            return None
        end = _balanced_end(text, start)
        if end is None:
            return None
        if text[start] == "{":
            try:
                value = json.loads(text[start:end])
            except (ValueError, RecursionError):
                value = None
            if isinstance(value, dict):
                return value
        position = end


# ─── Normalization ──────────────────────────────────────────────


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_risk(value) -> str:
    """One of the four risk levels; anything unrecognised counts as MEDIO.

    Case, accents and surrounding whitespace are ignored and common synonyms
    (``ALTA``, ``moderado``, ``leve``...) are mapped. The caller still takes
    the maximum with the safety level, so an unknown label can never hide a
    crisis.
    """
    if not isinstance(value, str):
        return _UNKNOWN_RISK
    key = _strip_accents(value).strip().upper()
    key = _RISK_SYNONYMS.get(key, key)
    return key if key in RISK_ORDER else _UNKNOWN_RISK


def normalize_emotion(value) -> str:
    """Known emotion (lower-case, no accents) or ``"otra"``."""
    if not isinstance(value, str):
        return OTHER_EMOTION
    key = _strip_accents(value).strip().lower()
    return key if key in ALLOWED_EMOTIONS else OTHER_EMOTION


def _clean_text(value, limit: int) -> str:
    """Trimmed string capped at ``limit`` characters; non-strings become ''."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit].rstrip()


def _clean_modismos(value) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned = (_clean_text(item, MODISMO_MAX) for item in value)
    return [item for item in cleaned if item][:MODISMOS_MAX_ITEMS]


# ─── Final response ─────────────────────────────────────────────


def build_response(
    raw, seguridad: SafetyAssessment, riesgo_sesion_previo="BAJO"
) -> AnalysisResponse:
    """Validated response for a raw completion (``None`` when the model raised).

    The final risk is ``max(model risk, safety level)``; an unusable completion
    counts as a MEDIO model risk. ``guardrail_activado`` is true when the
    safety layer raised the model's risk.

    ``riesgo_sesion_previo`` is the risk remembered from earlier in the
    conversation (already limited to the memory window by the caller; unusable
    values are ignored). The session risk is ``max(riesgo, previous)`` and the
    crisis resources follow it, so they stay visible on a calm follow-up
    message; the reply text and ``guardrail_activado`` depend only on the
    current message.
    """
    parsed = extract_json_object(raw)
    procesado = parsed is not None
    data = parsed if procesado else {}

    riesgo_modelo = normalize_risk(data.get("riesgo"))
    riesgo = max_risk(riesgo_modelo, seguridad.level)
    riesgo_sesion = max_risk(riesgo, riesgo_sesion_previo)
    guardrail = RISK_ORDER[riesgo] > RISK_ORDER[riesgo_modelo]

    if procesado:
        emocion = normalize_emotion(data.get("emocion"))
        tematica = _clean_text(data.get("tematica"), TEMATICA_MAX)
        resumen = _clean_text(data.get("resumen"), RESUMEN_MAX)
        modismos = _clean_modismos(data.get("modismos"))
        respuesta = _clean_text(data.get("respuesta"), RESPUESTA_MAX)
        empty_reply = EMPTY_REPLY_FALLBACK
    else:
        emocion = tematica = _UNPROCESSED_LABEL
        resumen = _UNPROCESSED_SUMMARY
        modismos = []
        respuesta = ""
        empty_reply = REPHRASE_REPLY

    if not respuesta:
        respuesta = safe_reply_for(riesgo) or empty_reply
    if riesgo == "CRITICO" and HOTLINE_SENTENCE not in respuesta:
        room = RESPUESTA_MAX - len(_HOTLINE_APPENDIX)
        respuesta = respuesta[:room].rstrip() + _HOTLINE_APPENDIX

    return AnalysisResponse(
        emocion=emocion,
        riesgo=riesgo,
        riesgo_sesion=riesgo_sesion,
        tematica=tematica,
        resumen=resumen,
        respuesta=respuesta,
        modismos=modismos,
        guardrail_activado=guardrail,
        recursos_crisis=resources_for(riesgo_sesion),
        procesado=procesado,
    )
