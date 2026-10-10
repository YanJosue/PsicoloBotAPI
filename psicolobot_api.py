from contextlib import asynccontextmanager
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Response, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, StrictBool, field_validator
from llm import LoraGenerator
from model_output import AnalysisResponse, build_response
# Named imports on purpose: "crisis_safety" (not "safety") because an unrelated
# PyPI package called "safety" can be installed and shadow a module of that
# name; if anything ever shadows this one it must fail at startup.
from crisis_safety import RISK_ORDER, assess
import session_store
import functools, hmac, json, csv, os, threading, time, uvicorn

# ─── Configuración ──────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_CONOCIMIENTOS = os.path.join(BASE_DIR, "datasets", "base_conocimientos.csv")
MAX_INPUT_CHARS = 1800  # prompt truncation (the safety layer reads the full text)
MAX_TEXT_CHARS = 4000  # longest "texto" a request may carry
HISTORY_LIMIT = 4  # messages of the caller's own session sent as context
DEFAULT_HOST = "127.0.0.1"  # loopback: ngrok forwards to localhost
DEFAULT_PORT = 8000

def get_now() -> int:
    """FastAPI dependency: current time as integer epoch seconds (tests inject a fake)."""
    return int(time.time())

def get_server_address() -> tuple[str, int]:
    """Host and port to bind, from PSICOLOBOT_HOST / PSICOLOBOT_PORT.

    Loopback by default; only set PSICOLOBOT_HOST=0.0.0.0 on purpose to serve
    the local network.
    """
    host = os.environ.get("PSICOLOBOT_HOST", "").strip() or DEFAULT_HOST
    try:
        port = int(os.environ.get("PSICOLOBOT_PORT", ""))
    except ValueError:
        port = DEFAULT_PORT
    return host, port if 1 <= port <= 65535 else DEFAULT_PORT

def get_cors_origins() -> list[str]:
    """Allowed browser origins from PSICOLOBOT_CORS_ORIGINS (comma-separated).

    Empty or unset means no cross-origin access at all. A native app does not
    need CORS; this only matters for a web client.
    """
    raw = os.environ.get("PSICOLOBOT_CORS_ORIGINS", "")
    origins = (origin.strip().rstrip("/") for origin in raw.split(","))
    return [origin for origin in origins if origin]

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

# ─── Nota de seguridad de sesión ────────────────────────────────
def session_risk_note(hours: int) -> str:
    """Prompt note added when this conversation already showed high risk."""
    period = "la última hora" if hours == 1 else f"las últimas {hours} horas"
    return (
        f"Nota de seguridad: antes en esta conversación hubo señales de riesgo "
        f"durante {period}. Responde con especial cuidado, con calidez y sin "
        f"minimizar lo que siente, y ten presentes los recursos de ayuda."
    )

# ─── Autenticación ──────────────────────────────────────────────
# Two different 401 challenges so a client can tell a bad deployment key
# ("ApiKey") from a missing/expired session ("Bearer", which means: ask for
# consent again).
_bearer = HTTPBearer(auto_error=False)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def _unauthorized(challenge: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=401, detail=detail, headers={"WWW-Authenticate": challenge}
    )

def require_api_key(provided: str | None = Security(_api_key_header)) -> None:
    """Optional deterrent: when PSICOLOBOT_API_KEY is set, X-API-Key must match.

    Not a secret (it ships inside the mobile app); real access control is the
    session token.
    """
    expected = os.environ.get("PSICOLOBOT_API_KEY", "")
    if not expected:
        return
    # Compare bytes: compare_digest rejects non-ASCII str.
    if provided is None or not hmac.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    ):
        raise _unauthorized("ApiKey", "Clave de API inválida.")

def current_session(
    _: None = Depends(require_api_key),
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    now: int = Depends(get_now),
) -> session_store.Session:
    """The live session named by the Bearer token, else 401."""
    session = (
        session_store.get_session(credentials.credentials, now)
        if credentials is not None
        else None
    )
    if session is None:
        raise _unauthorized("Bearer", "Sesión inválida o expirada.")
    return session

# ─── FastAPI ────────────────────────────────────────────────────
router = APIRouter()

class SessionRequest(BaseModel):
    acepta_terminos: StrictBool
    mayor_de_edad: StrictBool
    version_consentimiento: str = Field(min_length=1, max_length=40)

    @field_validator("acepta_terminos", "mayor_de_edad")
    @classmethod
    def debe_ser_verdadero(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("must be true")
        return value

    @field_validator("version_consentimiento")
    @classmethod
    def version_no_vacia(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("version_consentimiento must not be blank")
        return value

class SessionResponse(BaseModel):
    token: str = Field(description="Shown once; send it as 'Authorization: Bearer <token>'.")
    expira_en_dias: int = Field(description="Days until the session and its messages are deleted.")

class PostRequest(BaseModel):
    texto: str = Field(max_length=MAX_TEXT_CHARS)

    @field_validator("texto")
    @classmethod
    def texto_no_vacio(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("texto must not be blank")
        return value

@router.post(
    "/sesion",
    status_code=201,
    response_model=SessionResponse,
    dependencies=[Depends(require_api_key)],
)
def crear_sesion(req: SessionRequest, now: int = Depends(get_now)):
    """Open a session after explicit consent and the 18+ confirmation."""
    retention_days = session_store.get_retention_days()
    token = session_store.create_session(req.version_consentimiento, now, retention_days)
    return SessionResponse(token=token, expira_en_dias=retention_days)

@router.delete("/sesion", status_code=204)
def borrar_sesion(session: session_store.Session = Depends(current_session)):
    """Delete the session and every message stored for it."""
    session_store.delete_session(session.token_hash)
    return Response(status_code=204)

@router.post("/analizar", response_model=AnalysisResponse)
async def analizar(
    req: PostRequest,
    session: session_store.Session = Depends(current_session),
    generator: LoraGenerator = Depends(get_generator),
    now: int = Depends(get_now),
):
    # The safety layer reads the FULL raw text, before the prompt truncation
    # and before the model is called, so it cannot depend on either.
    seguridad = assess(req.texto)
    texto = req.texto[:MAX_INPUT_CHARS]

    # Fase 1: Análisis léxico rápido con la base de conocimientos
    emociones_lexicas = detectar_emociones_lexico(texto, get_lexico())
    etiquetas = ", ".join(emociones_lexicas) if emociones_lexicas else "ninguna detectada"

    # Historial del chat: solo los mensajes de ESTA sesión
    historial = session_store.recent_messages(session.token_hash, HISTORY_LIMIT)

    # Riesgo recordado de esta conversación (solo dentro de la ventana)
    window_hours = session_store.get_session_risk_hours()
    window_seconds = window_hours * 3600
    riesgo_previo = session.risk_in_window(now, window_seconds)

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

    safety_section = ""
    if RISK_ORDER[riesgo_previo] >= RISK_ORDER["ALTO"]:
        safety_section = session_risk_note(window_hours) + "\n\n"

    user_content = (
        f"Contexto léxico: El motor de análisis detectó las siguientes emociones: [{etiquetas}]\n\n"
        f"{context_section}"
        f"{safety_section}"
        f"Mensaje actual del usuario:\n{texto}"
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    # A raising generator is handled like unusable output: never a 500.
    # Only the exception type and sizes are logged; neither the exception
    # message nor the model output may contain or reveal user text.
    raw = None
    try:
        raw = generator.generate(messages)
    except Exception as exc:
        print(f"⚠️  Generator failed ({type(exc).__name__}); using the safety fallback")

    respuesta = build_response(raw, seguridad, riesgo_previo)
    if raw is not None and not respuesta.procesado:
        print(f"⚠️  Model output unusable ({len(raw)} chars); using the safety fallback")
    if respuesta.guardrail_activado:
        print(f"⚠️  GUARDRAIL: model risk raised to {respuesta.riesgo}")

    # Guardar en memoria: texto del usuario + JSON compacto del asistente (ahorra tokens
    # de contexto). También en los fallbacks, para no perder la continuidad. Si la
    # sesión se borró mientras el modelo trabajaba, no se guarda nada.
    compact_assistant = json.dumps({
        "emocion": respuesta.emocion,
        "respuesta": respuesta.respuesta
    }, ensure_ascii=False)
    session_store.record_exchange(
        session.token_hash, texto, compact_assistant, respuesta.riesgo, now, window_seconds
    )

    return respuesta

@router.get("/health")
async def health():
    return {
        "status": "ok",
        "model": "psicolobot-v3.1",
        "lexico_terms": sum(len(v) for v in get_lexico().values()),
        "model_loaded": _generator is not None and _generator.loaded,
    }

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup retention purge. The clock goes through the same dependency the
    # requests use, so a test override applies here too.
    now = app.dependency_overrides.get(get_now, get_now)()
    session_store.purge(now, session_store.get_retention_days())
    yield

def create_app() -> FastAPI:
    """Build the app; CORS reads PSICOLOBOT_CORS_ORIGINS at this moment."""
    application = FastAPI(title="Psicolobot API", version="3.1", lifespan=lifespan)
    origins = get_cors_origins()
    if origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-API-Key"],
        )
    application.include_router(router)
    return application

app = create_app()

if __name__ == "__main__":
    # Warm everything up before serving so first-request latency is unchanged.
    get_lexico()
    get_generator().load()
    host, port = get_server_address()
    uvicorn.run(app, host=host, port=port)
