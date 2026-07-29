from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from unsloth import FastLanguageModel
import torch, json, csv, os, uvicorn

import sqlite3

# ─── Configuración ──────────────────────────────────────────────
LORA_DIR = "lora_psicolobot_v3"
BASE_CONOCIMIENTOS = "datasets/base_conocimientos.csv"
MAX_INPUT_CHARS = 1800
DB_PATH = "chat_memory.db"

# ─── Inicializar Base de Datos de Memoria ───────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS memory
                 (session_id TEXT, role TEXT, content TEXT)''')
    conn.commit()
    conn.close()

init_db()

def obtener_historial(session_id: str, limit: int = 4):
    """Obtiene los últimos N mensajes de la sesión para dar contexto."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT role, content FROM memory WHERE session_id=? ORDER BY rowid DESC LIMIT ?", (session_id, limit))
    rows = c.fetchall()
    conn.close()
    # Invertir para que queden en orden cronológico
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

def guardar_interaccion(session_id: str, user_text: str, assistant_text: str):
    conn = sqlite3.connect(DB_PATH)
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

print("Cargando base de conocimientos...")
LEXICO = cargar_lexico(BASE_CONOCIMIENTOS)

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

# ─── Guardrail de seguridad: override de riesgo por frases críticas ──
import re

# Frases que SIEMPRE deben ser CRITICO (ideación suicida, autolesión, deseo de muerte)
PATRONES_CRITICO = [
    r"quiero\s+morir",
    r"quiero\s+matarme",
    r"me\s+quiero\s+matar",
    r"no\s+quiero\s+seguir\s+viv",
    r"mejor\s+me\s+muero",
    r"me\s+voy\s+a\s+(matar|suicidar)",
    r"pienso\s+en\s+(suicid|matarme|morir)",
    r"ya\s+no\s+quiero\s+(estar|existir|vivir)",
    r"me\s+(corto|lastimo|hago\s+daño)",
    r"autolesion",
    r"cortarme\s+(las\s+venas|los\s+brazos)",
    r"tengo\s+un\s+plan\s+para\s+morir",
    r"esta\s+noche\s+me\s+(mato|muero)",
    r"pastillas.*morir",
    r"sobredosis",
    r"colgarme|ahorcarme",
    r"tirarme\s+(de|al)",
]

# Frases que deben ser mínimo ALTO (carga emocional, desesperanza severa)
PATRONES_ALTO = [
    r"mejor\s+sin\s+m[ií]",
    r"estar[ií]an\s+mejor\s+sin\s+m[ií]",
    r"no\s+le\s+(encuentro|veo)\s+sentido",
    r"no\s+sirvo\s+para\s+nada",
    r"soy\s+una\s+carga",
    r"nadie\s+me\s+(quiere|necesita|extrañar)",
    r"no\s+tengo\s+raz[oó]n\s+(para|de)\s+vivir",
    r"ojal[aá]\s+no\s+(hubiera|existiera|despertara)",
    r"desaparecer",
    r"no\s+puedo\s+m[aá]s",
    r"estoy\s+harto.*todo",
    r"ya\s+no\s+aguanto",
    r"todo\s+me\s+sale\s+mal",
    r"no\s+hay\s+salida",
    r"me\s+siento\s+(vac[ií]o|inútil|invisible)",
]

RISK_ORDER = {"BAJO": 0, "MEDIO": 1, "ALTO": 2, "CRITICO": 3}

def evaluar_riesgo_minimo(texto: str) -> str:
    """Analiza el texto buscando patrones de riesgo que el modelo podría subestimar.
    Retorna el nivel de riesgo MÍNIMO que debe aplicarse, o 'BAJO' si no detecta nada."""
    texto_lower = texto.lower()
    # Primero buscar CRITICO (más urgente)
    for patron in PATRONES_CRITICO:
        if re.search(patron, texto_lower):
            return "CRITICO"
    # Luego ALTO
    for patron in PATRONES_ALTO:
        if re.search(patron, texto_lower):
            return "ALTO"
    return "BAJO"

# ─── Cargar modelo LLM ─────────────────────────────────────────
print("Cargando modelo PsicoloBot...")
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name=LORA_DIR, max_seq_length=2048,
    dtype=None, load_in_4bit=True,
)
FastLanguageModel.for_inference(model)
print("✅ Modelo listo")

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
async def analizar(req: PostRequest):
    texto = req.texto[:MAX_INPUT_CHARS]
    session_id = req.session_id

    # Fase 1: Análisis léxico rápido con la base de conocimientos
    emociones_lexicas = detectar_emociones_lexico(texto, LEXICO)
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

    # Usar el chat template nativo del modelo (Llama 3 Instruct)
    prompt_final = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )

    inputs = tokenizer([prompt_final], return_tensors="pt").to("cuda")

    with torch.no_grad():
        out = model.generate(
            **inputs,
            max_new_tokens=350,
            do_sample=False,        # Greedy decoding para mayor precisión
            repetition_penalty=1.2,  # Evitar respuestas repetitivas
            pad_token_id=tokenizer.eos_token_id,
        )

    n = inputs["input_ids"].shape[1]
    raw = tokenizer.decode(out[0][n:], skip_special_tokens=True).strip()

    # Limpiar y parsear el JSON de salida
    raw_clean = raw.replace("```json", "").replace("```", "").strip()
    # A veces el modelo mete texto después del JSON, cortamos en el último '}'
    last_brace = raw_clean.rfind("}")
    if last_brace != -1:
        raw_clean = raw_clean[:last_brace + 1]

    try:
        resultado = json.loads(raw_clean)

        # ── Guardrail de seguridad: override de riesgo ──────────────
        riesgo_modelo = resultado.get("riesgo", "MEDIO").upper()
        riesgo_minimo = evaluar_riesgo_minimo(texto)

        # Si nuestro detector dice que el riesgo es mayor, lo subimos
        if RISK_ORDER.get(riesgo_minimo, 0) > RISK_ORDER.get(riesgo_modelo, 0):
            print(f"⚠️  GUARDRAIL: Riesgo del modelo ({riesgo_modelo}) overridden a {riesgo_minimo}")
            resultado["riesgo"] = riesgo_minimo
            resultado["guardrail_activado"] = True

        # Si el riesgo final es CRITICO, forzar Línea de la Vida
        if resultado.get("riesgo", "").upper() == "CRITICO":
            linea = "Línea de la Vida: 800 911 2000"
            if linea not in resultado.get("respuesta", ""):
                resultado["respuesta"] = (
                    resultado.get("respuesta", "") +
                    f"\n\n⚠️ Si sientes que estás en peligro, por favor llama a la {linea}. No estás solo/a."
                )
                
        # Guardar en memoria: texto del usuario + JSON compacto del modelo (ahorra tokens de contexto)
        compact_assistant = json.dumps({
            "emocion": resultado.get("emocion", ""),
            "respuesta": resultado.get("respuesta", "")
        }, ensure_ascii=False)
        guardar_interaccion(session_id, texto, compact_assistant)
        
        return resultado
    except json.JSONDecodeError:
        return {
            "riesgo": "MEDIO",
            "emocion": "No detectable",
            "tematica": "No detectable",
            "resumen": "No se pudo procesar la respuesta del modelo.",
            "respuesta": "Disculpa, no logré procesar bien tu mensaje. ¿Podrías decirme con otras palabras cómo te sientes?",
            "modismos": [],
            "error_debug": raw[:300],
        }

@app.get("/health")
async def health():
    return {"status": "ok", "model": "psicolobot-v3.1", "lexico_terms": sum(len(v) for v in LEXICO.values())}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
