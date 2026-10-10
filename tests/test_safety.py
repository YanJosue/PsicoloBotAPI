"""Unit tests for the LLM-independent crisis safety layer (``safety.py``).

Detection is recall-first: a missed crisis is the dangerous error, so these
tests pin both the phrasings that MUST escalate and the figurative ones that
must not, but they never require the net to be precise.
"""

import dataclasses
from pathlib import Path

import pytest

import safety
from safety import (
    CRISIS_RESOURCES,
    RISK_ORDER,
    SAFE_REPLY_ALTO,
    SAFE_REPLY_CRITICO,
    assess,
    max_risk,
    normalize_text,
    resources_for,
    safe_reply_for,
)

KNOWN_CATEGORIES = {
    "suicidal_ideation",
    "self_harm",
    "method",
    "imminent_plan",
    "passive_ideation",
    "farewell",
    "burden",
    "hopelessness",
    "isolation",
    "distress",
}

# ─── Phrasings that MUST be CRITICO ─────────────────────────────

# The phrasings the audit proved the old regex guardrail missed.
AUDIT_CRITICAL = [
    "quiero suicidarme",
    "me quiero quitar la vida",
    "me voy a colgar",
    "pienso en el suicidio",
    "QUIERO MORIR",
    "quiero morír",
    "ya no quiero vivir",
    "kiero matarme",
    "me corto los brazos cuando estoy triste",
    "voy a tomarme todas las pastillas",
    "me voy a aventar del puente",
    "ojalá me durmiera y no despertara nunca",
    "me despido de todos, gracias por todo",
    "quierooo morirme",
    "me muero de risa pero a veces quiero suicidarme",
]

# Phrasings the old guardrail already caught: the new layer must not regress.
LEGACY_CRITICAL = [
    "quiero matarme",
    "me quiero matar",
    "no quiero seguir viviendo",
    "mejor me muero",
    "me voy a matar",
    "me voy a suicidar",
    "pienso en morir",
    "pienso en matarme",
    "ya no quiero existir",
    "ya no quiero estar aquí",
    "me lastimo cuando no puedo más",
    "me hago daño",
    "tengo autolesiones",
    "me quiero cortar las venas",
    "tengo un plan para morir",
    "esta noche me muero",
    "esta noche me mato",
    "pastillas para morir",
    "me tomé una sobredosis",
    "voy a ahorcarme",
    "quiero colgarme",
    "me voy a tirar del edificio",
]

# Further forms the new layer is expected to cover.
EXTRA_CRITICAL = [
    "quisiera morirme",
    "quisiera morir",
    "ojalá me muriera",
    "desearía estar muerto",
    "quiero desaparecer para siempre",
    "quiero dormir y no despertar",
    "no quiero despertar nunca",
    "me voy a quitar la vida",
    "voy a quitarme la vida",
    "ya no quiero seguir viviendo",
    "no quiero existir",
    "no tengo ganas de vivir",
    "tengo un plan para suicidarme",
    "tengo un plan para matarme",
    "voy a cortarme",
    "voy a hacerme daño",
    "pastillas para matarme",
    "quiero una sobredosis",
    "me voy a tirar a las vías del tren",
    "voy a aventarme desde la azotea",
    "me voy a pegar un tiro",
    "adiós para siempre",
    "esta es mi despedida",
    "esta es mi carta de despedida",
    "q kiero suicidarme",
    "QUIERO SUICIDARME!!!",
    "quiero suicidarmeee",
    "ya no kiero vivir",
    "xq quiero morir",
    "quiero morirme y que todo acabe",
    "quiero acabar con mi vida",
    "ojalá no despertara nunca",
    "tengo ganas de morirme",
    "me quiero ir de este mundo",
    "mejor me voy a dormir para siempre",
    "me gustaría no despertar",
    "quieroo morirme",
    "quiero matarmee",
]

CRITICAL_PHRASES = AUDIT_CRITICAL + LEGACY_CRITICAL + EXTRA_CRITICAL

# ─── Phrasings that MUST be at least ALTO (and not CRITICO) ─────

REQUIRED_ALTO = [
    "todos estarían mejor sin mí",
    "soy una carga para mi familia",
    "ya no aguanto",
    "no le veo sentido a nada",
]

LEGACY_ALTO = [
    "estarían mejor sin mí",
    "no sirvo para nada",
    "nadie me quiere",
    "nadie me necesita",
    "nadie me extrañaría",
    "nadien me quiere",
    "no tengo razón para vivir",
    "no tengo razón de vivir",
    "ojalá no existiera",
    "ojalá no hubiera nacido",
    "quiero desaparecer",
    "ya no puedo más",
    "estoy harta de todo",
    "no hay salida",
    "me siento vacío",
    "me siento vacía",
    "me siento inútil",
    "me siento invisible",
    "todo me sale mal",
    "no le encuentro sentido a la vida",
    "ya no quiero estar así",
    "ya no quiero seguir",
    "ya no puedo seguir",
]

ALTO_PHRASES = REQUIRED_ALTO + LEGACY_ALTO

# ─── Phrasings that must NOT escalate to ALTO or CRITICO ────────

REQUIRED_BENIGN = [
    "me quiero morir de risa",
    "me muero de hambre",
    "mañana me corto el pelo",
    "me mato estudiando para el examen",
    "vi una serie para matar el tiempo",
    "hoy fue un buen día",
]

EXTRA_BENIGN = [
    "me muero de sueño",
    "me muero de ganas de verte",
    "me estoy muriendo de calor",
    "estoy muerto de cansancio",
    "me morí de risa con ese video",
    "me quiero morir de la vergüenza",
    "me muero de envidia",
    "me corto las uñas cada semana",
    "me voy a cortar el cabello",
    "me corté el pelo ayer",
    "me mato trabajando todo el día",
    "me voy a matar el tiempo con una serie",
    "mi mamá me va a matar si llego tarde",
    "tengo mucho trabajo y estoy estresado",
    "me siento triste hoy",
    "voy a tirarme al sillón a descansar",
    "tomo mis medicinas todos los días",
    "quiero dormir y no despertar temprano",
    "quiero dormir y no despertar hasta el mediodía",
    "",
]

BENIGN_PHRASES = REQUIRED_BENIGN + EXTRA_BENIGN


def test_safety_resolves_to_the_local_module_not_the_pypi_package():
    # A PyPI package called "safety" exists; this repo's module must win.
    repo_root = Path(__file__).resolve().parent.parent
    assert Path(safety.__file__).resolve() == repo_root / "safety.py"


@pytest.mark.parametrize("text", CRITICAL_PHRASES)
def test_crisis_phrasings_are_critico(text):
    assert assess(text).level == "CRITICO"


@pytest.mark.parametrize("text", ALTO_PHRASES)
def test_distress_phrasings_are_alto(text):
    assert assess(text).level == "ALTO"


@pytest.mark.parametrize("text", BENIGN_PHRASES)
def test_figurative_and_everyday_phrasings_do_not_escalate(text):
    assessment = assess(text)

    assert assessment.level not in ("ALTO", "CRITICO")
    assert assessment.categories == ()


def test_a_crisis_phrase_next_to_a_figurative_one_still_escalates():
    figurative_only = assess("me muero de risa")
    mixed = assess("me muero de risa pero a veces quiero suicidarme")

    assert figurative_only.level == "BAJO"
    assert mixed.level == "CRITICO"
    assert "suicidal_ideation" in mixed.categories


def test_detection_works_with_and_without_accents():
    with_accents = assess("ya no quiero vivir, estoy hartísima de todo")
    without_accents = assess("ya no quiero vivir, estoy hartisima de todo")

    assert with_accents == without_accents
    assert with_accents.level == "CRITICO"


def test_crisis_phrase_far_into_a_long_text_is_found():
    text = "hoy fue un día normal. " * 200 + "quiero suicidarme"

    assert len(text) > 4000
    assert assess(text).level == "CRITICO"


def test_highest_level_wins_when_alto_and_critico_phrases_are_mixed():
    assessment = assess("soy una carga y quiero suicidarme")

    assert assessment.level == "CRITICO"
    assert "burden" in assessment.categories
    assert "suicidal_ideation" in assessment.categories


# ─── Assessment shape ───────────────────────────────────────────


@pytest.mark.parametrize(
    "text, category",
    [
        ("quiero suicidarme", "suicidal_ideation"),
        ("me corto los brazos cuando estoy triste", "self_harm"),
        ("voy a tomarme todas las pastillas", "method"),
        ("me voy a colgar", "method"),
        ("tengo un plan para morir", "imminent_plan"),
        ("ojalá me durmiera y no despertara nunca", "passive_ideation"),
        ("me despido de todos, gracias por todo", "farewell"),
        ("soy una carga para mi familia", "burden"),
        ("no le veo sentido a nada", "hopelessness"),
        ("nadie me quiere", "isolation"),
        ("ya no aguanto", "distress"),
    ],
)
def test_categories_name_what_was_detected(text, category):
    assessment = assess(text)

    assert category in assessment.categories
    assert set(assessment.categories) <= KNOWN_CATEGORIES


def test_assessment_is_an_immutable_value_without_raw_text():
    text = "quiero suicidarme"

    assessment = assess(text)

    assert isinstance(assessment.categories, tuple)
    assert dataclasses.is_dataclass(assessment)
    with pytest.raises(dataclasses.FrozenInstanceError):
        assessment.level = "BAJO"
    assert text not in repr(assessment)
    assert "suicidarme" not in repr(assessment)


def test_assess_never_prints_user_text(capsys):
    assess("quiero suicidarme")

    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_benign_text_has_bajo_level_and_no_categories():
    assessment = assess("hoy fue un buen día")

    assert assessment.level == "BAJO"
    assert assessment.categories == ()


# ─── normalize_text ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("HOLA Mundo", "hola mundo"),
        ("daño", "dano"),
        ("mí", "mi"),
        ("quiero morír", "quiero morir"),
        ("ojalá", "ojala"),
        ("pingüino", "pinguino"),
        ("quierooo", "quiero"),
        ("noooo más", "no mas"),
        ("jajajaaaa", "jajaja"),
        ("llueve y creer", "llueve y creer"),
        ("q quiero", "que quiero"),
        ("tengo q irme", "tengo que irme"),
        ("xq no", "porque no"),
        ("pq no", "porque no"),
        ("kiero morir", "quiero morir"),
        ("kero morir", "quiero morir"),
        ("nadien me quiere", "nadie me quiere"),
        ("aqui quiero un paquete", "aqui quiero un paquete"),
        ("¿Hola?  ¡Qué... tal!", "hola que tal"),
        ("a-b_c/x", "a b c x"),
        ("  a\t\nb  ", "a b"),
        ("su​icidarme", "suicidarme"),
        ("ｑｕｉｅｒｏ", "quiero"),
        ("", ""),
    ],
)
def test_normalize_text(raw, expected):
    assert normalize_text(raw) == expected


def test_normalize_text_is_idempotent():
    once = normalize_text("¡KIEROOO morírme, xq nadien me quiere!")

    assert normalize_text(once) == once


# ─── Risk ordering ──────────────────────────────────────────────


def test_risk_order_ranks_levels_from_bajo_to_critico():
    assert RISK_ORDER == {"BAJO": 0, "MEDIO": 1, "ALTO": 2, "CRITICO": 3}


@pytest.mark.parametrize(
    "levels, expected",
    [
        (("BAJO", "ALTO"), "ALTO"),
        (("CRITICO", "ALTO"), "CRITICO"),
        (("MEDIO", "BAJO", "MEDIO"), "MEDIO"),
        (("alto", "BAJO"), "ALTO"),
        ((" critico ", "MEDIO"), "CRITICO"),
        (("BAJO", None), "BAJO"),
        ((None, "MEDIO"), "MEDIO"),
        (("SUPERALTO", "MEDIO"), "MEDIO"),
        ((3, "ALTO"), "ALTO"),
        (("???", None), "BAJO"),
        ((), "BAJO"),
    ],
)
def test_max_risk_ignores_unknown_and_missing_levels(levels, expected):
    assert max_risk(*levels) == expected


# ─── Crisis resources and reply templates ───────────────────────


def test_crisis_resources_are_exactly_the_verified_hotlines():
    assert [dict(r) for r in CRISIS_RESOURCES] == [
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


@pytest.mark.parametrize("level", ["ALTO", "CRITICO"])
def test_resources_are_offered_from_alto_upwards(level):
    assert resources_for(level) == [dict(r) for r in CRISIS_RESOURCES]


@pytest.mark.parametrize("level", ["BAJO", "MEDIO", None, "", "SUPERALTO"])
def test_no_resources_below_alto(level):
    assert resources_for(level) == []


def test_resources_for_returns_copies_that_cannot_corrupt_the_constant():
    first = resources_for("CRITICO")
    first[0]["telefono"] = "tampered"
    first.pop()

    assert len(resources_for("CRITICO")) == 3
    assert resources_for("CRITICO")[0]["telefono"] == "800 911 2000"


def test_critico_template_validates_asks_about_safety_and_gives_hotlines():
    reply = safe_reply_for("CRITICO")

    assert reply == SAFE_REPLY_CRITICO
    assert "¿Estás a salvo" in reply
    assert safety.HOTLINE_SENTENCE == "Línea de la Vida: 800 911 2000"
    assert safety.HOTLINE_SENTENCE in reply
    assert "911" in reply.replace("800 911 2000", "")


def test_alto_template_points_to_the_hotlines():
    reply = safe_reply_for("ALTO")

    assert reply == SAFE_REPLY_ALTO
    assert reply != SAFE_REPLY_CRITICO
    assert "800 911 2000" in reply


@pytest.mark.parametrize("level", ["BAJO", "MEDIO", None, "SUPERALTO"])
def test_no_safe_template_below_alto(level):
    assert safe_reply_for(level) is None
