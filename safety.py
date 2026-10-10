"""LLM-independent crisis safety layer for PsicoloBot.

What it is
    A small, dependency-free keyword net that reads the user's raw text and
    reports how worried we should be (``BAJO``/``MEDIO``/``ALTO``/``CRITICO``),
    without ever asking the language model. The API combines this level with
    the model's own risk estimate (the higher one wins) and applies it on
    every path, including when the model fails or returns garbage.

Recall-first
    Missing a real crisis is the dangerous error; showing a hotline to someone
    who was only venting is not. Patterns are therefore written broadly and
    false positives are accepted (for example "no quiero vivir con mis papás",
    or a third person mentioning a suicide). Only a short list of everyday
    figurative idioms ("me muero de risa", "me corto el pelo", "matar el
    tiempo") is carved out, and each carve-out removes just its own narrow
    span so a real crisis phrase in the same message still escalates.

Not a clinical instrument
    This is a keyword net, not a screening tool or a diagnosis. It cannot read
    context, negation, irony, typos beyond the ones normalised here, slang it
    has not seen, or intent. It must never be the only line of defence and it
    never replaces professional care.

Privacy
    ``assess`` returns only a level and category labels, never the raw or the
    matched text, and nothing here logs. Callers must not log user text either.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# ─── Risk levels ────────────────────────────────────────────────

RISK_ORDER = {"BAJO": 0, "MEDIO": 1, "ALTO": 2, "CRITICO": 3}


def max_risk(*levels) -> str:
    """Highest known risk level among ``levels``.

    Case and surrounding whitespace are ignored. Unknown or non-string values
    (``None``, numbers, unrecognised labels) are skipped; with nothing usable
    the result is ``"BAJO"``.
    """
    best = "BAJO"
    for level in levels:
        if not isinstance(level, str):
            continue
        key = level.strip().upper()
        if key in RISK_ORDER and RISK_ORDER[key] > RISK_ORDER[best]:
            best = key
    return best


# ─── Text normalisation ─────────────────────────────────────────

# Common Mexican texting spellings, expanded as whole words only.
_TEXTING_FORMS = {
    "q": "que",
    "k": "que",
    "ke": "que",
    "xq": "porque",
    "xk": "porque",
    "pq": "porque",
    "pk": "porque",
    "porq": "porque",
    "porke": "porque",
    "kiero": "quiero",
    "kero": "quiero",
    "quero": "quiero",
    "qiero": "quiero",
    "kisiera": "quisiera",
    "nadien": "nadie",
    "tmb": "tambien",
    "tb": "tambien",
    "d": "de",
    "m": "me",
}
_TEXTING_RX = re.compile(
    r"\b("
    + "|".join(sorted(map(re.escape, _TEXTING_FORMS), key=len, reverse=True))
    + r")\b"
)
_ELONGATION_RX = re.compile(r"([a-z])\1{2,}")
# Two-letter word-final elongation ("quieroo", "matarmee"). Spanish has almost
# no legitimate word-final double vowels, so assess() collapses them for
# matching only; normalize_text() keeps the documented 3+ rule.
_FINAL_VOWEL_RX = re.compile(r"([aeiou])\1(?=\s|$)")
_NON_ALNUM_RX = re.compile(r"[^a-z0-9]+")


def normalize_text(text: str) -> str:
    """Reduce ``text`` to a canonical form that the patterns are written for.

    Lowercases; strips accents and other combining marks (``daño`` becomes
    ``dano``, ``mí`` becomes ``mi``) and invisible format characters such as
    zero-width spaces; collapses runs of three or more identical letters
    (``quierooo`` becomes ``quiero``); turns punctuation into spaces; expands
    common texting forms (``q``, ``xq``, ``kiero``, ``nadien``...) as whole
    words; and collapses whitespace.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    kept = "".join(
        ch
        for ch in decomposed
        if not unicodedata.combining(ch) and unicodedata.category(ch) != "Cf"
    )
    kept = _ELONGATION_RX.sub(r"\1", kept)
    kept = _NON_ALNUM_RX.sub(" ", kept)
    kept = _TEXTING_RX.sub(lambda m: _TEXTING_FORMS[m.group(1)], kept)
    return " ".join(kept.split())


# ─── Figurative exclusions ──────────────────────────────────────

# Everyday idioms that sound like a crisis but are not. Each pattern covers
# only its own narrow span, which is replaced by a separator that no crisis
# pattern can match across, so a real phrase elsewhere in the text survives.
_FIGURATIVE_FEELING = (
    r"(?:risa|hambre|sueno|calor|frio|ganas|amor|verguenza|pena|aburrimiento"
    r"|nervios|envidia|cansancio|sed|curiosidad|flojera)"
)
_FIGURATIVE_PATTERNS = (
    # "me quiero morir de risa", "me muero de hambre", "muerto de cansancio"
    r"\b(?:morir(?:me)?|muero|mori|moria|muriendo(?:me)?|muert[oa]|mato|mata"
    r"|matar(?:me)?)\s+de\s+(?:(?:la|el|un|una)\s+)?" + _FIGURATIVE_FEELING + r"\b",
    # "me corto el pelo", "voy a cortarme las uñas"
    r"\b(?:me\s+)?(?:cortar(?:me)?|corto|corte|cortaba|cortare|cortando)\s+"
    r"(?:el|mi|mis|los|las|un|una|unas|unos)\s+"
    r"(?:pelo|cabello|fleco|copete|barba|bigote|unas|puntas|melena)\b",
    # "me mato estudiando"
    r"\bme\s+mato\s+(?:estudiando|trabajando|chambeando|limpiando|cocinando"
    r"|entrenando|corriendo|haciendo)\b",
    # "matar el tiempo"
    r"\bmat(?:ar|o|ando|e|aba)\s+el\s+tiempo\b",
    # "tirarme al sillón", "me echo a la cama"
    r"\b(?:tirar(?:me)?|aventar(?:me)?|echar(?:me)?|lanzar(?:me)?|tiro|aviento"
    r"|echo)\s+(?:al|a\s+la)\s+"
    r"(?:piso|suelo|sillon|sofa|cama|pasto|hamaca|alberca|piscina)\b",
)
_FIGURATIVE_RX = re.compile(
    "|".join(f"(?:{p})" for p in _FIGURATIVE_PATTERNS), re.ASCII
)
# "|" never survives normalisation, so it cannot be part of a crisis phrase.
_SEPARATOR = " | "


# ─── Crisis patterns (written for normalised text) ──────────────

_PILLS = r"(?:pastillas?|pildoras?|medicamentos?|medicinas?|tabletas?)"
_JUMP_TARGET = (
    r"(?:de|del|desde|al|a\s+las?|a\s+un|a\s+una|por|frente|enfrente)"
)

# Level CRITICO: suicidal ideation, self-harm, a method or plan, a goodbye.
_CRITICAL_PATTERNS = {
    "suicidal_ideation": (
        r"\b(?:suicid|sucid|suisid)\w*",
        r"\bquit(?:ar(?:me|se)?|o|aria|are|e)\s+la\s+[vb]ida\b",
        r"\bmatarme\b",
        r"\bme\s+(?:quiero|quisiera|voy\s+a|pienso|podria|puedo|tengo\s+que"
        r"|debo|deberia)\s+matar\b",
        r"\bme\s+mato\b",
        r"\bmejor\s+me\s+(?:muero|mato)\b",
        r"\b(?:pienso|pensando|pense|pensado)\s+(?:mucho\s+)?en\s+"
        r"(?:morir|morirme|matarme|acabar\s+con\s+todo)\b",
        r"\b(?:quiero|quisiera|queria|quisiese|deseo|necesito|prefiero)\s+"
        r"(?:ya\s+|solo\s+|simplemente\s+)?(?:morir|morirme)\b",
        r"\bganas\s+de\s+(?:morir|morirme)\b",
        r"\bojala\s+(?:yo\s+)?(?:me\s+)?(?:muriera|muriese|muera)\b",
        r"\b(?:acabar|terminar)\s+con\s+(?:mi\s+vida|mi\s+existencia"
        r"|mi\s+sufrimiento)\b",
        r"\bponer\s+fin\s+a\s+mi\s+vida\b",
        r"\bno\s+(?:quiero|deseo|quisiera)\s+(?:mas\s+)?(?:seguir\s+|continuar\s+)?"
        r"(?:[vb]iv\w*|exist\w*)",
        r"\bno\s+puedo\s+(?:mas\s+)?seguir\s+(?:[vb]iv\w*|exist\w*)",
        r"\bno\s+quiero\s+(?:seguir\s+|continuar\s+)?(?:estando|estar)\s+"
        r"(?:aqui|aca|vivo|viva|en\s+(?:este|el)\s+mundo)\b",
        r"\bno\s+(?:tengo|siento)\s+ganas\s+de\s+(?:seguir\s+)?[vb]iv\w*",
        r"\bno\s+(?:vale|merece)\s+la\s+pena\s+(?:seguir\s+)?[vb]iv\w*",
        r"\b[vb]iv\w*\s+no\s+(?:vale|merece)\s+la\s+pena\b",
        r"\bdejar\s+de\s+(?:existir|respirar)\b",
    ),
    "self_harm": (
        r"\bcortarme\b",
        r"\bme\s+(?:quiero|quisiera|voy\s+a|pienso|dan\s+ganas\s+de)\s+cortar\b",
        r"\bme\s+(?:corto|corte|cortaba|estoy\s+cortando)\b",
        r"\b(?:abrirme|rajarme)\s+las\s+venas\b",
        r"\bme\s+(?:abro|rajo|voy\s+a\s+abrir|quiero\s+abrir|voy\s+a\s+rajar)"
        r"\s+las\s+venas\b",
        r"\bme\s+(?:lastimo|lastimaba|hago\s+dano|hacia\s+dano|hice\s+dano"
        r"|autolesiono)\b",
        r"\b(?:hacerme\s+dano|lastimarme)\b",
        r"\bauto\s?lesi\w*",
    ),
    "method": (
        r"\b(?:colgarme|ahorcarme|aorcarme)\b",
        r"\bme\s+(?:quiero|quisiera|voy\s+a|pienso)\s+(?:colgar|ahorcar)\b",
        r"\bsobre\s?dosis\b",
        r"\boverdose\b",
        r"\b(?:tomarme|me\s+(?:tome|tomare|voy\s+a\s+tomar|quiero\s+tomar"
        r"|pienso\s+tomar)|(?:voy\s+a|quiero|pienso)\s+tomar)\s+(?:me\s+)?"
        r"(?:todas|todos|muchas|un\s+monton\s+de)\b[^|]{0,30}?\b" + _PILLS + r"\b",
        r"\b" + _PILLS + r"\b[^|]{0,60}\bmorir",
        r"\b" + _PILLS + r"\s+para\s+(?:matarme|suicidarme|no\s+despertar"
        r"|dormir\s+para\s+siempre)\b",
        r"\b(?:tirarme|aventarme|lanzarme|arrojarme)\s+" + _JUMP_TARGET + r"\b",
        r"\bme\s+(?:voy\s+a|quiero|quisiera|pienso|podria)\s+"
        r"(?:tirar|aventar|lanzar|arrojar)\s+" + _JUMP_TARGET + r"\b",
        r"\bsaltar(?:me)?\s+(?:de|del|desde)\s+(?:(?:un|el|la|una)\s+)?"
        r"(?:edificio|puente|azotea|balcon|ventana|techo|acantilado|torre)\b",
        r"\bpegarme\s+un\s+tiro\b",
        r"\bme\s+(?:pego|voy\s+a\s+pegar|quiero\s+pegar)\s+un\s+tiro\b",
        r"\bdispararme\b",
        r"\bvolarme\s+(?:la\s+cabeza|los\s+sesos)\b",
        r"\benvenenarme\b",
        r"\b(?:tomarme|beberme)\s+(?:el\s+|un\s+)?"
        r"(?:veneno|raticida|cloro|cianuro|acido)\b",
    ),
    "imminent_plan": (
        r"\btengo\s+(?:ya\s+)?(?:un\s+)?(?:plan|todo\s+planeado)\s+(?:para|de)\s+"
        r"(?:morir|morirme|matarme|suicidarme|acabar|terminar)\b",
        r"\b(?:hoy|esta\s+noche|esta\s+tarde|ahorita)\s+me\s+"
        r"(?:mato|muero|suicido|quito\s+la\s+vida)\b",
    ),
    "passive_ideation": (
        # The trailing \b stops "\w*" from backtracking past the lookahead.
        r"\b(?:dormir(?:me)?|durmiera|durmiese|dormiria)\b[^|]{0,30}"
        r"\bno\s+(?:me\s+)?despert\w*\b(?!\s+(?:temprano|hasta|antes))",
        r"\b(?:me\s+gustaria|quisiera|quiero|ojala)\s+(?:ya\s+)?no\s+"
        r"(?:me\s+)?despert\w*\b(?!\s+(?:temprano|hasta|antes))",
        r"\b(?:dormir|dormirme|dormid[oa]|durmiera|durmiese)\s+para\s+siempre\b",
        r"\b(?:irme|largarme|me\s+(?:quiero|quisiera|voy\s+a)\s+(?:ir|largar))"
        r"\s+de\s+(?:este|esta)\s+(?:mundo|vida)\b",
        r"\bno\s+(?:me\s+)?despert\w*\s+(?:nunca|jamas|mas)\b",
        r"\bno\s+(?:quiero|quisiera)\s+(?:volver\s+a\s+)?despertar(?:me)?\s+"
        r"(?:nunca|jamas|mas)\b",
        r"\bdesaparec\w*\s+(?:para\s+siempre|del\s+mundo|de\s+este\s+mundo"
        r"|de\s+la\s+faz)\b",
        r"\b(?:desearia|deseo|quisiera|quiero|queria|ojala|me\s+gustaria"
        r"|prefiero|prefiriera|preferiria)\s+(?:yo\s+)?(?:ya\s+)?"
        r"(?:estar|estuviera|estuviese)\s+muert[oa]\b",
        r"\bmejor\s+(?:estar\s+)?muert[oa]\b",
        r"\bojala\s+(?:me\s+)?atropell\w*",
    ),
    "farewell": (
        r"\bme\s+despido\s+de\s+(?:todos|todas|ustedes|mi\s+familia|el\s+mundo"
        r"|la\s+vida)\b",
        r"\b(?:esta|este)\s+es\s+mi\s+(?:despedida"
        r"|ultim[oa]\s+(?:mensaje|carta|nota|adios))\b",
        r"\b(?:carta|nota)\s+de\s+despedida\b",
        r"\badios\s+para\s+siempre\b",
    ),
}

# Level ALTO: burden, hopelessness, isolation and acute distress.
_HIGH_PATTERNS = {
    "burden": (
        r"\bsoy\s+(?:una|un)\s+(?:carga|estorbo|molestia)\b",
        r"\bme\s+siento\s+(?:como\s+)?(?:una|un)\s+(?:carga|estorbo)\b",
        r"\bmejor\s+sin\s+mi\b",
    ),
    "hopelessness": (
        r"\bno\s+(?:le\s+)?(?:encuentro|veo|hallo)\s+(?:ningun\s+)?sentido\b",
        r"\b(?:mi\s+)?vida\s+no\s+tiene\s+(?:ningun\s+)?sentido\b",
        r"\bno\s+tengo\s+(?:ninguna\s+)?(?:razon|razones|motivo|motivos)\s+"
        r"(?:para|de)\s+(?:seguir\s+)?[vb]iv\w*",
        r"\bojala\s+no\s+(?:hubiera|existiera|despertara|despertase)\b",
        r"\b(?:quisiera|quiero)\s+no\s+(?:existir|haber\s+nacido)\b",
        r"\bno\s+hay\s+(?:salida|esperanza)\b",
        r"\btodo\s+me\s+sale\s+mal\b",
        r"\b(?:quiero|quisiera|voy\s+a)\s+acabar\s+con\s+todo\b",
    ),
    "isolation": (
        r"\bnadie\s+me\s+(?:quiere|necesita|extrana\w*|ama|echaria\s+de\s+menos)\b",
        r"\ba\s+nadie\s+le\s+importo\b",
        r"\bnadie\s+(?:se\s+daria\s+cuenta|notaria)\b",
    ),
    "distress": (
        r"\bno\s+sirvo\s+para\s+nada\b",
        r"\bdesaparecer\w*",
        r"\bno\s+puedo\s+mas\b",
        r"\bestoy\s+(?:harto|harta)\b[^|]{0,60}\btodo\b",
        r"\bya\s+no\s+aguanto\b",
        r"\bno\s+aguanto\s+mas\b",
        r"\bme\s+siento\s+(?:(?:muy|tan|bien|re|super)\s+)?"
        r"(?:vacio|vacia|inutil|invisible)\b",
        r"\bya\s+no\s+quiero\s+estar\b",
        r"\bya\s+no\s+(?:quiero|puedo)\s+seguir\b",
    ),
}


def _compile(table: dict[str, tuple[str, ...]]):
    return tuple(
        (category, re.compile("|".join(f"(?:{p})" for p in patterns), re.ASCII))
        for category, patterns in table.items()
    )


_CRITICAL_RX = _compile(_CRITICAL_PATTERNS)
_HIGH_RX = _compile(_HIGH_PATTERNS)


# ─── Assessment ─────────────────────────────────────────────────


@dataclass(frozen=True)
class SafetyAssessment:
    """Outcome of ``assess``: a risk level and category labels, no raw text."""

    level: str
    categories: tuple[str, ...] = ()


def _matched_categories(table, text: str) -> list[str]:
    return [category for category, rx in table if rx.search(text)]


def assess(text: str) -> SafetyAssessment:
    """Assess ``text`` for crisis language, independently of any model.

    The level is ``CRITICO`` when any critical pattern matches, else ``ALTO``
    when a high-risk pattern matches, else ``BAJO``. ``MEDIO`` is reserved for
    callers that need a floor (for example when the model failed); this net
    never produces it. ``categories`` lists every category that matched.
    """
    normalized = _FINAL_VOWEL_RX.sub(r"\1", normalize_text(text))
    cleaned = _FIGURATIVE_RX.sub(_SEPARATOR, normalized)
    critical = _matched_categories(_CRITICAL_RX, cleaned)
    high = _matched_categories(_HIGH_RX, cleaned)
    if critical:
        level = "CRITICO"
    elif high:
        level = "ALTO"
    else:
        level = "BAJO"
    return SafetyAssessment(level=level, categories=tuple(critical + high))


# ─── Crisis resources and safe replies ──────────────────────────

# Verified against the official sources. Treat as read-only: ``resources_for``
# hands out copies.
CRISIS_RESOURCES: tuple[dict[str, str], ...] = (
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
)

# The exact sentence the API makes sure a CRITICO reply contains.
HOTLINE_SENTENCE = "Línea de la Vida: 800 911 2000"

SAFE_REPLY_CRITICO = (
    "Gracias por decirme lo que estás sintiendo; no es fácil ponerlo en "
    "palabras y me importa lo que te pasa. Lo que describes es serio y no "
    "tienes que enfrentarlo en soledad. ¿Estás a salvo en este momento? "
    "Si sientes que estás en peligro o que podrías hacerte daño, por favor "
    f"llama ahora a la {HOTLINE_SENTENCE} (atiende las 24 horas) o al 911. "
    "Si puedes, avísale también a alguien de confianza que esté cerca de ti. "
    "Aquí sigo contigo."
)

SAFE_REPLY_ALTO = (
    "Lamento mucho que estés pasando por algo tan pesado; lo que sientes es "
    "válido y te agradezco que lo compartas conmigo. No tienes que cargar con "
    "esto en soledad: platicarlo con alguien de confianza o con una persona "
    "capacitada puede ayudarte. Si en algún momento sientes que te desborda o "
    f"piensas en hacerte daño, puedes llamar a la {HOTLINE_SENTENCE} o a "
    "SAPTEL: 55 5259 8121, ambas las 24 horas. ¿Cómo te sientes en este "
    "momento?"
)


def _at_least_alto(level) -> bool:
    return RISK_ORDER[max_risk(level)] >= RISK_ORDER["ALTO"]


def resources_for(level) -> list[dict[str, str]]:
    """Crisis hotlines to show for ``level``: all of them from ALTO up, else none."""
    if not _at_least_alto(level):
        return []
    return [dict(resource) for resource in CRISIS_RESOURCES]


def safe_reply_for(level) -> str | None:
    """Pre-written reply for ``ALTO``/``CRITICO`` (used when the model fails), else None."""
    key = max_risk(level)
    if key == "CRITICO":
        return SAFE_REPLY_CRITICO
    if key == "ALTO":
        return SAFE_REPLY_ALTO
    return None
