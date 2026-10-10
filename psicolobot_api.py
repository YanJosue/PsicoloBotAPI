from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from llm import LoraGenerator
# Named imports on purpose: a PyPI package called "safety" exists, and if it
# ever shadowed our module this must fail at startup, not on every request.
from safety import (
    HOTLINE_SENTENCE,
    RISK_ORDER,
    SafetyAssessment,
    assess,
    max_risk,
    resources_for,
    safe_reply_for,
)
import functools, json, csv, os, threading, uvicorn

import sqlite3

# ─── Configuración ──────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_CONOCIMIENTOS = os.path.join(BASE_DIR, "datasets", "base_conocimientos.csv")
MAX_INPUT_CHARS = 1800
DEFAULT_DB_PATH = "chat_memory.db"

def get_db_path() -> str:
    """SQLite file path, read on every call so it can be set via PSICOLOBOT_DB_PATH."""
    return os.environ.get("PSICOLOBOT_DB_PATH", DEFAULT_DB_PATH)

# ─── Base de Datos de Memoria ───────────────────────────────────
def _connect() -> sqlite3.Connection:
    """Open the memory DB, creating the schema on first use."""
    conn = sqlite3.connect(get_db_path())
    conn.execute('''CREATE TABLE IF NOT EXISTS memory
                    (session_id TEXT, role TEXT, content TEXT)''')
    conn.commit()
    return conn

def init_db():
    _connect().close()

def obtener_historial(session_id: str, limit: int = 4):
    """Obtiene los últimos N mensajes de la sesión para dar contexto."""
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT role, content FROM memory WHERE session_id=? ORDER BY rowid DESC LIMIT ?", (session_id, limit))
    rows = c.fetchall()
    conn.close()
    # Invertir para que queden en orden cronológico
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

def guardar_interaccion(session_id: str, user_text: str, assistant_text: str):
    conn = _connect()
    c = conn.cursor()
    c.execute("INSERT INTO memory (session_id, role, content) VALUES (?, ?, ?)", (session_id, "user", user_text))
    c.execute("INSERT INTO memory (session_id, role, content) VALUES (?, ?, ?)", (session_id, "assistant", assistant_text))
    conn.commit()
    conn.close()

# ─── Cargar base de conocimientos (léxico emocional) ────────────
def cargar_lexico(path: str) -> dict[str, list[str]]:
    """Carga el CSV de la base de conocimientos y arma un diccionario
    { emocion: [termino1, termino2, ...] } para búsqueda rápida."""
    lexico: dict[str, list[str]] = {}
    if not os.path.exists(path):
        print(f"⚠️  Base de conocimientos no encontrada en {path}")
        return lexico
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            entidad = row.get("Entidad", "").strip().lower()
            termino = row.get("Termino", "").strip().lower()
            if entidad and termino:
                lexico.setdefault(entidad, []).append(termino)
    print(f"✅ Base de conocimientos cargada: {sum(len(v) for v in lexico.values())} términos en {len(lexico)} categorías")
    return lexico

@functools.lru_cache(maxsize=1)
def get_lexico() -> dict[str, list[str]]:
    """Load the lexicon on first use (path is relative to this module)."""
    print("Cargando base de conocimientos...")
    return cargar_lexico(BASE_CONOCIMIENTOS)

# ─── Detector léxico (Fase 1 ligera) ───────────────────────────
def detectar_emociones_lexico(texto: str, lexico: dict[str, list[str]]) -> list[str]:
    """Busca coincidencias del texto contra la base de conocimientos.
    Devuelve la lista de emociones detectadas por el motor léxico."""
    texto_lower = texto.lower()
    detectadas = []
    for emocion, terminos in lexico.items():
        for termino in terminos:
            if termino in texto_lower:
                if emocion not in detectadas:
                    detectadas.append(emocion)
                break  # Una coincidencia por categoría es suficiente
    return detectadas

# ─── Generador LLM (carga perezosa, inyectable) ─────────────────
_generator: LoraGenerator | None = None
_generator_lock = threading.Lock()

def get_generator() -> LoraGenerator:
    """FastAPI dependency: lazily created singleton (the model loads on first use)."""
    global _generator
    with _generator_lock:
        if _generator is None:
            _generator = LoraGenerator()
        return _generator

# ─── System Prompt con guardrails de seguridad ──────────────────
SYSTEM_PROMPT = """Eres PsicoloBot, un asistente de contención emocional especializado en español mexicano.

## REGLAS DE SEGURIDAD (NUNCA las rompas):
1. NUNCA diagnostiques. No eres psicólogo ni médico. Usa frases como "parece que sientes..." en vez de "tienes depresión".
2. Si detectas riesgo CRITICO (ideación suicida, autolesión), tu respuesta DEBE incluir: "Si sientes que estás en peligro, llama a la Línea de la Vida: 800 911 2000".
3. NUNCA recomiendes medicamentos, tratamientos ni terapias específicas.
4. NUNCA juzgues, minimices o invalides las emociones del usuario.
5. Si no entiendes el mensaje, pide que lo repita. No inventes contexto.
6. Responde SIEMPRE en español mexicano, con tono cálido y cercano.

## TU TAREA:
Analiza el texto del usuario y responde ÚNICAMENTE con un JSON válido con esta estructura exacta:
{
  "emocion": "<emoción principal detectada>",
  "riesgo": "<BAJO|MEDIO|ALTO|CRITICO>",
  "tematica": "<tema central del mensaje>",
  "resumen": "<resumen clínico breve del estado emocional>",
  "respuesta": "<mensaje empático, cálido y breve (2-3 oraciones) dirigido al usuario para continuar la conversación>",
  "modismos": [<lista de modismos o jerga detectada en el texto>]
}

## CRITERIOS DE RIESGO:
- BAJO: Emociones cotidianas, sin indicadores de malestar significativo.
- MEDIO: Malestar emocional presente pero manejable, sin ideación de daño.
- ALTO: Malestar intenso, desesperanza marcada, aislamiento severo.
- CRITICO: Mención de autolesión, ideación suicida o deseo de muerte. Incluir SIEMPRE la Línea de la Vida.

## EMOCIONES VÁLIDAS:
alegria, amor, asco, culpa, dolor, enojo, ira, miedo, orgullo, tristeza, vergüenza"""

# ─── Salida del modelo y guardrail de seguridad ─────────────────
REPHRASE_REPLY = (
    "Disculpa, no logré procesar bien tu mensaje. "
    "¿Podrías decirme con otras palabras cómo te sientes?"
)

def parse_model_output(raw) -> dict | None:
    """Parse the raw completion into a dict, or None when it is unusable
    (not text, not JSON, or JSON that is not an object)."""
    if not isinstance(raw, str):
        return None
    raw_clean = raw.replace("```json", "").replace("```", "").strip()
    # A veces el modelo mete texto después del JSON, cortamos en el último '}'
    last_brace = raw_clean.rfind("}")
    if last_brace != -1:
        raw_clean = raw_clean[:last_brace + 1]
    try:
        parsed = json.loads(raw_clean)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None

def normalize_model_risk(value) -> str:
    """Model risk label as an upper-case level; anything unusable counts as MEDIO."""
    key = value.strip().upper() if isinstance(value, str) else ""
    return key if key in RISK_ORDER else "MEDIO"

def apply_guardrail(resultado: dict, seguridad: SafetyAssessment) -> dict:
    """Final risk = max(model risk, safety level); make sure a CRITICO reply
    carries the Línea de la Vida sentence. Mutates and returns ``resultado``."""
    riesgo_modelo = normalize_model_risk(resultado.get("riesgo"))
    riesgo_final = max_risk(riesgo_modelo, seguridad.level)
    elevado = RISK_ORDER[riesgo_final] > RISK_ORDER[riesgo_modelo]
    if elevado:
        print(f"⚠️  GUARDRAIL: model risk ({riesgo_modelo}) raised to {riesgo_final}")

    respuesta = resultado.get("respuesta")
    if not isinstance(respuesta, str):
        respuesta = ""
    if not respuesta.strip():
        respuesta = safe_reply_for(riesgo_final) or respuesta
    if riesgo_final == "CRITICO" and HOTLINE_SENTENCE not in respuesta:
        respuesta += (
            f"\n\n⚠️ Si sientes que estás en peligro, por favor llama a la {HOTLINE_SENTENCE}. "
            "No estás solo/a."
        )

    resultado["riesgo"] = riesgo_final
    resultado["respuesta"] = respuesta
    resultado["guardrail_activado"] = elevado
    resultado["recursos_crisis"] = resources_for(riesgo_final)
    return resultado

def fallback_response(seguridad: SafetyAssessment, raw: str | None) -> dict:
    """Response for an unusable model completion or a raising generator.
    The risk never drops below MEDIO and never below the safety level."""
    riesgo = max_risk("MEDIO", seguridad.level)
    response = {
        "riesgo": riesgo,
        "emocion": "No detectable",
        "tematica": "No detectable",
        "resumen": "No se pudo procesar la respuesta del modelo.",
        "respuesta": safe_reply_for(riesgo) or REPHRASE_REPLY,
        "modismos": [],
        "guardrail_activado": RISK_ORDER[riesgo] > RISK_ORDER["MEDIO"],
        "recursos_crisis": resources_for(riesgo),
    }
    if raw is not None:
        # Planned for removal in task A3 (raw model output must not leak).
        response["error_debug"] = raw[:300]
    return response

# ─── FastAPI ────────────────────────────────────────────────────
app = FastAPI(title="Psicolobot API", version="3.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class PostRequest(BaseModel):
    texto: str
    session_id: str = "default_session"

@app.post("/analizar")
async def analizar(req: PostRequest, generator: LoraGenerator = Depends(get_generator)):
    # The safety layer reads the FULL raw text, before the prompt truncation
    # and before the model is called, so it cannot depend on either.
    seguridad = assess(req.texto)
    texto = req.texto[:MAX_INPUT_CHARS]
    session_id = req.session_id

    # Fase 1: Análisis léxico rápido con la base de conocimientos
    emociones_lexicas = detectar_emociones_lexico(texto, get_lexico())
    etiquetas = ", ".join(emociones_lexicas) if emociones_lexicas else "ninguna detectada"

    # Recuperar historial de chat
    historial = obtener_historial(session_id)

    # Fase 2: Construir el prompt como UN SOLO TURNO (el modelo fue fine-tuneado así)
    # El historial se inyecta como contexto dentro del mensaje del usuario,
    # NO como turnos separados en el chat template.
    context_section = ""
    if historial:
        context_lines = []
        for msg in historial:
            if msg["role"] == "user":
                context_lines.append(f"- Paciente dijo: \"{msg['content']}\"")
            elif msg["role"] == "assistant":
                try:
                    parsed = json.loads(msg["content"])
                    context_lines.append(
                        f"- PsicoloBot respondió [emoción: {parsed.get('emocion', '?')}]: "
                        f"\"{parsed.get('respuesta', '')}\""
                    )
                except (json.JSONDecodeError, TypeError):
                    context_lines.append(f"- PsicoloBot respondió: \"{msg['content']}\"")
        context_section = (
            "Historial reciente de la conversación (úsalo para dar continuidad):\n"
            + "\n".join(context_lines)
            + "\n\n"
        )

    user_content = (
        f"Contexto léxico: El motor de análisis detectó las siguientes emociones: [{etiquetas}]\n\n"
        f"{context_section}"
        f"Mensaje actual del usuario:\n{texto}"
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    # A raising generator is handled like unparseable output: never a 500.
    # Only the exception type is logged; its message may contain user text.
    raw = None
    try:
        raw = generator.generate(messages)
    except Exception as exc:
        print(f"⚠️  Generator failed ({type(exc).__name__}); using the safety fallback")

    resultado = parse_model_output(raw)
    if resultado is None:
        respuesta = fallback_response(seguridad, raw)
    else:
        respuesta = apply_guardrail(resultado, seguridad)

    # Guardar en memoria: texto del usuario + JSON compacto del asistente (ahorra tokens
    # de contexto). También en los fallbacks, para no perder la continuidad.
    compact_assistant = json.dumps({
        "emocion": respuesta.get("emocion", ""),
        "respuesta": respuesta.get("respuesta", "")
    }, ensure_ascii=False)
    guardar_interaccion(session_id, texto, compact_assistant)

    return respuesta

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model": "psicolobot-v3.1",
        "lexico_terms": sum(len(v) for v in get_lexico().values()),
        "model_loaded": _generator is not None and _generator.loaded,
    }

if __name__ == "__main__":
    # Warm everything up before serving so first-request latency is unchanged.
    init_db()
    get_lexico()
    get_generator().load()
    uvicorn.run(app, host="0.0.0.0", port=8000)
