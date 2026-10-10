# PsicoloBot API

Servidor de inferencia de PsicoloBot, un prototipo de acompañamiento emocional en español mexicano (proyecto de tesis). Recibe un mensaje breve, lo analiza con un modelo de lenguaje afinado (LoRA) y devuelve la emoción detectada, un nivel de riesgo y una respuesta empática. Antes de llamar al modelo, una capa de seguridad independiente revisa el texto en busca de lenguaje de crisis.

> **Qué es y qué no es.** PsicoloBot es un prototipo de apoyo emocional. **No** es terapia, **no** emite diagnósticos y **no** es un servicio de emergencia. Sus etiquetas de emoción y de riesgo no son una evaluación clínica. Ante una crisis, la persona debe contactar a la Línea de la Vida (800 911 2000), a SAPTEL (55 5259 8121) o al 911.

## Inicio rápido

1. Instalar dependencias y arrancar: `python psicolobot_api.py` (o `iniciar_api.bat` en Windows). El modelo se carga en la GPU antes de aceptar peticiones.
2. Por defecto la API escucha solo en `127.0.0.1:8000`. Para usarla desde el teléfono, exponerla con `ngrok http 8000`.
3. Verificar: `GET http://localhost:8000/health` devuelve `"status": "ok"`.

Guía paso a paso para quien opera el servidor: [INSTRUCCIONES.md](INSTRUCCIONES.md).

## Arquitectura

| Módulo | Responsabilidad |
|---|---|
| `psicolobot_api.py` | Aplicación FastAPI: rutas, autenticación, construcción del prompt, host/puerto/CORS desde el entorno y purga al arrancar. |
| `llm.py` | `LoraGenerator`: carga perezosa del modelo LoRA y generación. `unsloth` y `torch` se importan solo al cargar el modelo, por eso las pruebas no necesitan GPU. |
| `crisis_safety.py` | Capa de seguridad independiente del modelo: normalización de texto, patrones de crisis, exclusiones figurativas, recursos de ayuda y respuestas seguras. |
| `model_output.py` | Convierte la salida cruda del modelo en la respuesta validada (`AnalysisResponse`) y calcula el riesgo final y el riesgo de sesión. |
| `session_store.py` | Almacenamiento SQLite: sesiones, mensajes, hash del token, retención y memoria de riesgo. |
| `datasets/base_conocimientos.csv` | Léxico emocional usado como contexto en el prompt. |
| `lora_psicolobot_v3/` | Pesos del modelo afinado. |

### Flujo de `POST /analizar`

| # | Paso | Detalle |
|---|---|---|
| 1 | Autenticación | `X-API-Key` (si está configurada) y luego el token Bearer. Se revisa **antes** de validar el cuerpo. |
| 2 | Validación del cuerpo | `texto` de 1 a 4000 caracteres y no vacío; si no, 422. |
| 3 | Capa de seguridad | `crisis_safety.assess` lee el texto completo, **antes** del modelo y sin depender de él. |
| 4 | Prompt | Texto truncado a 1800 caracteres, contexto léxico, los últimos 4 mensajes de la propia sesión y, si la sesión tuvo riesgo ALTO o mayor dentro de la ventana, una nota de seguridad. |
| 5 | Modelo | `LoraGenerator.generate`. Si lanza una excepción se trata como salida inutilizable; nunca produce un 500. |
| 6 | Validación y normalización | `model_output.build_response` extrae el primer objeto JSON, normaliza emoción y riesgo, limita longitudes y descarta campos no documentados. La salida cruda nunca se devuelve. |
| 7 | Riesgo final | `riesgo = max(modelo, seguridad)`. Una salida inutilizable cuenta como riesgo MEDIO del modelo. |
| 8 | Riesgo de sesión | `riesgo_sesion = max(riesgo, riesgo recordado dentro de la ventana)`. |
| 9 | Recursos | `recursos_crisis` se llena cuando `riesgo_sesion` es ALTO o CRITICO. |
| 10 | Almacenamiento | Se guardan el mensaje y la respuesta, y se actualiza la memoria de riesgo. Si la sesión se borró mientras el modelo trabajaba, no se guarda nada. |

El nivel de riesgo siempre se ordena `BAJO < MEDIO < ALTO < CRITICO`, y ningún paso puede dejar un riesgo menor al que pidió la capa de seguridad.

## Contrato HTTP

Todas las rutas usan JSON. Ejemplos en bash, con la API en `http://localhost:8000`:

```bash
# 1) Abrir sesión, tras el consentimiento explícito de la persona
curl -X POST http://localhost:8000/sesion \
  -H "Content-Type: application/json" \
  -d '{"acepta_terminos": true, "mayor_de_edad": true, "version_consentimiento": "2026-10-v1"}'
# 201 {"token": "<token>", "expira_en_dias": 30}

# 2) Analizar un mensaje
curl -X POST http://localhost:8000/analizar \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"texto": "Me siento muy triste y no quiero salir de la cama"}'

# 3) Borrar la sesión y sus mensajes
curl -X DELETE http://localhost:8000/sesion -H "Authorization: Bearer <token>"
# 204 (sin cuerpo)
```

### Rutas

| Ruta | Credenciales | Éxito | Errores |
|---|---|---|---|
| `POST /sesion` | `X-API-Key` solo si `PSICOLOBOT_API_KEY` está definida | `201` | `401` (ApiKey), `422` |
| `POST /analizar` | `Authorization: Bearer <token>` (más `X-API-Key` si está definida) | `200` | `401` (Bearer o ApiKey), `422` |
| `DELETE /sesion` | `Authorization: Bearer <token>` (más `X-API-Key` si está definida) | `204` sin cuerpo | `401` (Bearer o ApiKey) |
| `GET /health` | Ninguna | `200` | — |

### `POST /sesion`

Cuerpo:

| Campo | Regla |
|---|---|
| `acepta_terminos` | Booleano JSON `true`. Cualquier otro valor (`false`, `"true"`, `1`, `null`) da `422`. |
| `mayor_de_edad` | Booleano JSON `true`, con la misma regla. Es una declaración de la persona, no una verificación. |
| `version_consentimiento` | Texto de 1 a 40 caracteres, no vacío. Se guarda junto con la sesión. |

Los campos adicionales se ignoran. Respuesta `201`:

| Campo | Significado |
|---|---|
| `token` | Secreto opaco de 43 caracteres. Se entrega **una sola vez**; solo se guarda su hash SHA-256. |
| `expira_en_dias` | Días hasta que la sesión y sus mensajes se eliminan (`PSICOLOBOT_RETENTION_DAYS`, 30 por defecto). |

### `POST /analizar`

Cuerpo: `{"texto": "..."}`, de 1 a 4000 caracteres y no vacío. La capa de seguridad lee el texto completo; el modelo recibe solo los primeros 1800 caracteres, y esos son los que se guardan.

Respuesta `200` (ejemplo ilustrativo; el texto real lo genera el modelo):

```json
{
  "emocion": "tristeza",
  "riesgo": "ALTO",
  "riesgo_sesion": "ALTO",
  "tematica": "agotamiento emocional",
  "resumen": "La persona expresa agotamiento y desesperanza.",
  "respuesta": "Lamento que te sientas así. Gracias por contármelo.",
  "modismos": [],
  "guardrail_activado": true,
  "recursos_crisis": [
    {"nombre": "Línea de la Vida", "telefono": "800 911 2000", "marcar": "8009112000", "disponibilidad": "24/7"},
    {"nombre": "SAPTEL", "telefono": "55 5259 8121", "marcar": "5552598121", "disponibilidad": "24/7"},
    {"nombre": "Emergencias", "telefono": "911", "marcar": "911", "disponibilidad": "24/7"}
  ],
  "procesado": true
}
```

| Campo | Significado |
|---|---|
| `emocion` | Una de: `alegria`, `amor`, `asco`, `culpa`, `dolor`, `enojo`, `ira`, `miedo`, `orgullo`, `tristeza`, `verguenza` (minúsculas, sin acentos). `otra` si el modelo nombró algo distinto; `No detectable` si `procesado` es `false`. |
| `riesgo` | `BAJO`, `MEDIO`, `ALTO` o `CRITICO`. Es el mayor entre la estimación del modelo y la capa de seguridad. Una etiqueta ilegible del modelo cuenta como `MEDIO` y nunca baja el riesgo. |
| `riesgo_sesion` | Mayor riesgo de esta conversación dentro de la ventana de memoria (24 h por defecto), incluido este mensaje. Nunca es menor que `riesgo`. |
| `tematica` | Tema central según el modelo (hasta 200 caracteres). |
| `resumen` | Resumen breve según el modelo (hasta 600 caracteres). |
| `respuesta` | Mensaje para la persona (hasta 1500 caracteres, nunca vacío). Si el modelo no la aporta, se usa una respuesta segura predefinida. En `CRITICO` siempre incluye "Línea de la Vida: 800 911 2000". |
| `modismos` | Hasta 20 modismos detectados, de hasta 60 caracteres cada uno. |
| `guardrail_activado` | `true` cuando la capa de seguridad subió el riesgo estimado por el modelo. Depende solo del mensaje actual. |
| `recursos_crisis` | Líneas de ayuda (`nombre`, `telefono`, `marcar`, `disponibilidad`). Se llena cuando `riesgo_sesion` es `ALTO` o `CRITICO`, aunque el mensaje actual sea tranquilo; en otro caso es `[]`. `marcar` contiene solo dígitos, listo para un enlace `tel:`. |
| `procesado` | `false` cuando la salida del modelo no se pudo usar (el modelo falló o no devolvió un objeto JSON). La respuesta es entonces un texto seguro de respaldo. |

**Riesgo de sesión.** La API recuerda el mayor riesgo de cada sesión durante la ventana. La ventana se reinicia cuando llega un mensaje con riesgo igual o mayor al recordado; un mensaje de menor riesgo no la extiende. Cuando el riesgo recordado es `ALTO` o más, el siguiente prompt incluye una nota para que el modelo responda con más cuidado. Un riesgo `MEDIO` recordado no agrega nota ni recursos.

### `DELETE /sesion`

Elimina la sesión y todos sus mensajes. Responde `204` sin cuerpo. Repetir la llamada da `401`, porque la sesión ya no existe.

### `GET /health`

Sin credenciales (tampoco exige `X-API-Key`). Devuelve `status`, `model`, `lexico_terms` (términos del léxico) y `model_loaded`. Con `python psicolobot_api.py` el modelo ya está cargado al aceptar peticiones; si se arranca con `uvicorn psicolobot_api:app` directamente, `model_loaded` es `false` hasta la primera llamada a `/analizar`.

### Errores

| Código | Cabecera | Causa | Qué debe hacer el cliente |
|---|---|---|---|
| `401` | `WWW-Authenticate: Bearer` | Token ausente, desconocido, vencido o de una sesión borrada. | Pedir de nuevo el consentimiento y abrir una sesión (`POST /sesion`). |
| `401` | `WWW-Authenticate: ApiKey` | `PSICOLOBOT_API_KEY` está definida y `X-API-Key` falta o no coincide. | Es un error de configuración; no pedir consentimiento de nuevo. |
| `422` | — | Cuerpo inválido: consentimiento no explícito, `texto` vacío o de más de 4000 caracteres, campos ausentes o de tipo incorrecto. | Mostrar un mensaje propio de la app. |

- **Orden de las comprobaciones:** clave de API, luego token Bearer y al final el cuerpo. Una petición sin token y con `texto` vacío recibe `401`, no `422`.
- **No mostrar `detail` de un `422` a la persona.** Es el formato por defecto de FastAPI e incluye el valor enviado (`input`), que puede ser el texto del usuario.
- Un fallo o una salida inservible del modelo **no** produce error HTTP: la respuesta es `200` con `procesado: false`.

## Configuración

Las variables se leen del entorno del proceso; la API no carga archivos `.env`.

| Variable | Valor por defecto | Cuándo se lee | Valor inválido |
|---|---|---|---|
| `PSICOLOBOT_DB_PATH` | `chat_memory.db` (relativa al directorio de trabajo) | En cada acceso a la base. | No se valida; se usa tal cual. |
| `PSICOLOBOT_RETENTION_DAYS` | `30` | Al arrancar (purga) y al crear una sesión. | No entero o menor que 1: se usa `30`. |
| `PSICOLOBOT_SESSION_RISK_HOURS` | `24` | En cada `POST /analizar`. | No entero o menor que 1: se usa `24`. |
| `PSICOLOBOT_CORS_ORIGINS` | vacío (sin acceso entre orígenes) | Al arrancar la aplicación (hay que reiniciar para cambiarla). | Se separa por comas; las entradas vacías se ignoran y se quita la `/` final. Con CORS activo, la API expone la cabecera `WWW-Authenticate` para que el cliente web distinga un 401 de sesión de uno de clave de API. |
| `PSICOLOBOT_HOST` | `127.0.0.1` | Al arrancar con `python psicolobot_api.py`. | Vacío o en blanco: se usa `127.0.0.1`. |
| `PSICOLOBOT_PORT` | `8000` | Al arrancar con `python psicolobot_api.py`. | No entero o fuera de 1 a 65535: se usa `8000`. |
| `PSICOLOBOT_API_KEY` | sin definir (desactivada) | En cada petición. | Vacía equivale a desactivada. |

- Con `PSICOLOBOT_API_KEY` definida, `X-API-Key` debe coincidir en `POST /sesion`, `POST /analizar` y `DELETE /sesion`. Si se arranca con `uvicorn` directamente, `PSICOLOBOT_HOST` y `PSICOLOBOT_PORT` no se usan: valen las opciones de `uvicorn`.
- Cambiar `PSICOLOBOT_RETENTION_DAYS` no modifica el vencimiento de las sesiones ya creadas, pero la purga sí borra los mensajes más antiguos que el valor vigente.
- CORS permite solo los orígenes listados, con los métodos `GET`, `POST`, `DELETE` y las cabeceras `Authorization`, `Content-Type` y `X-API-Key`, sin credenciales. Una app nativa no necesita CORS; solo importa para un cliente web (por ejemplo Expo web en `http://localhost:8081`).
- `0.0.0.0` en `PSICOLOBOT_HOST` expone la API a toda la red local, por HTTP sin cifrar. Debe ser una decisión deliberada.

## Capa de seguridad ante crisis

`crisis_safety.py` es una red de palabras clave sin dependencias que **no consulta al modelo**. Se aplica al texto completo antes de la llamada al modelo y en todos los caminos: respuesta correcta, salida ilegible y excepción del generador. El riesgo final es `max(modelo, seguridad)`; la capa nunca lo baja.

| Aspecto | Comportamiento |
|---|---|
| Prioridad | **Recall primero**: perder una crisis real es el error grave; mostrar una línea de ayuda a quien solo se desahogaba es aceptable. Se aceptan falsos positivos. |
| Normalización | Minúsculas; sin acentos ni caracteres invisibles; letras repetidas colapsadas (`quierooo`); puntuación como espacios; formas de chat expandidas (`q`, `xq`, `kiero`, `nadien`). |
| Niveles | `CRITICO`: ideación suicida, autolesión, método o plan, despedida, ideación pasiva. `ALTO`: carga para otros, desesperanza, aislamiento y malestar agudo. La capa nunca devuelve `MEDIO`. |
| Exclusiones figurativas | Lista corta de modismos cotidianos: "me quiero morir de risa", "me corto el pelo", "me mato estudiando", "matar el tiempo", "tirarme al sillón". Cada exclusión retira solo su fragmento, así que una frase de crisis real en el mismo mensaje sigue escalando. |
| Si el modelo falla | Con riesgo `ALTO` o `CRITICO` la respuesta es un texto seguro predefinido que incluye las líneas de ayuda. |
| Líneas de ayuda | Línea de la Vida 800 911 2000 (24/7), SAPTEL 55 5259 8121 (24/7) y Emergencias 911. |
| Privacidad | Devuelve solo nivel y categorías, nunca el texto. No registra nada. |

### Limitaciones conocidas

Es una red de palabras clave, **no un instrumento clínico**: no está validada con profesionales de la salud mental y debe revisarse con ellos antes de cualquier uso con personas reales. No lee contexto, ironía ni intención, y solo cubre español.

**Falsos positivos aceptados** (escalan aunque no haya crisis):

- Negaciones: "no quiero morir" da `CRITICO`.
- Menciones de terceros: "mi amigo habló del suicidio" da `CRITICO`.
- Frases que suenan a crisis pero no lo son: "no quiero vivir con mis papás" da `CRITICO`; "no puedo más con la tarea" da `ALTO`.
- Cualquier modismo figurativo que no esté en la lista corta de exclusiones.

**Falsos negativos conocidos** (la capa devuelve `BAJO`; solo el modelo podría detectarlos):

- Modismos ambiguos: "me voy a morir", "quiero que todo termine".
- Violencia hacia otras personas ("voy a matar a mi jefe"): la capa solo busca autolesión.
- Texto ofuscado: números por letras (`su1c1dio`) o letras separadas (`s u i c i d a r m e`).
- Otros idiomas (por ejemplo, inglés), jerga que no se ha visto y errores de escritura distintos de los normalizados.

## Privacidad y datos

| Tema | Comportamiento |
|---|---|
| Qué se guarda | Por sesión: hash SHA-256 del token, fechas de creación y vencimiento, versión de consentimiento, mayor riesgo y su hora. Por mensaje: texto de la persona (primeros 1800 caracteres) y la respuesta del asistente (emoción y texto), con su hora. No se piden ni se guardan nombre, correo ni identificadores del dispositivo. |
| Token | Aleatorio y opaco; se entrega una vez. Solo se guarda su hash SHA-256, así que una copia de la base no permite suplantar a nadie. |
| Retención | La sesión vence a los `PSICOLOBOT_RETENTION_DAYS` días de su creación (fijo, no se renueva con el uso). Al vencer se elimina con sus mensajes. Un token vencido da `401` aunque la fila siga en la base. |
| Cuándo se purga | Al arrancar la API y cada vez que se crea una sesión. |
| Borrado | `DELETE /sesion` elimina la sesión y sus mensajes. La base usa `PRAGMA secure_delete = ON`, de modo que SQLite sobrescribe el contenido borrado en lugar de dejarlo en el archivo. No alcanza a copias o respaldos que se hicieran antes. |
| Registros (logs) | La API no imprime texto de usuarios, tokens, hashes, salida cruda del modelo ni mensajes de excepción. Solo escribe avisos operativos: carga del léxico, nivel de riesgo tras el guardrail, tipo de excepción del generador y tamaño de una salida inutilizable. |
| Archivo de base | `chat_memory.db` está en `.gitignore`; aun así contiene texto de conversaciones y debe tratarse como dato sensible. |

### Tabla heredada `memory` (texto en claro)

Las versiones anteriores guardaban todas las conversaciones sin cifrar en la tabla `memory` de `chat_memory.db`. La API actual **nunca lee, migra, crea ni borra** esa tabla: si el archivo viene de una versión anterior, el historial viejo sigue ahí. Debe eliminarse manualmente, con la API detenida.

Opción A, conservar las sesiones actuales (con la CLI de `sqlite3`):

```bash
sqlite3 chat_memory.db ".tables"   # confirma si existe la tabla memory
sqlite3 chat_memory.db "PRAGMA secure_delete = ON; DROP TABLE memory; VACUUM;"
```

`DROP TABLE` quita la tabla y `VACUUM` reescribe el archivo para que no queden restos de su contenido.

Opción B, sin `sqlite3` instalado:

```bash
python -c "import sqlite3; c = sqlite3.connect('chat_memory.db'); c.execute('PRAGMA secure_delete = ON'); c.execute('DROP TABLE IF EXISTS memory'); c.commit(); c.execute('VACUUM'); c.close()"
```

Opción C, la más simple: borrar el archivo `chat_memory.db`. Las tablas nuevas se crean solas al siguiente arranque, pero se pierden las sesiones activas y cada persona debe aceptar el consentimiento otra vez.

## Otras limitaciones conocidas

| Limitación | Efecto |
|---|---|
| Vencimiento fijo | La sesión vence a los 30 días aunque se use a diario; después la app recibe `401` y debe pedir consentimiento de nuevo. |
| Purga solo al arrancar y al crear sesión | Un servidor que no reinicia ni recibe sesiones nuevas conserva filas vencidas. Ya no se pueden usar (el token da `401`), pero siguen en el archivo hasta la siguiente purga. Un token rechazado no dispara la purga. |
| Sin límite de peticiones | No hay control de frecuencia en ninguna ruta; en particular, cualquiera que alcance `POST /sesion` puede crear sesiones sin tope. |
| `/docs`, `/redoc` y `/openapi.json` abiertos | La documentación interactiva de FastAPI no requiere credenciales. |
| `X-API-Key` no es un secreto | Viaja dentro de la app móvil. Solo disuade escaneos casuales; el control de acceso real es el token de sesión. |
| Memoria de riesgo por sesión | Es de la sesión, no de la persona: una sesión nueva empieza sin historial de riesgo. |
| Mayoría de edad declarada | `mayor_de_edad` es una casilla aceptada por la persona; la API no la verifica. |
| Etiquetas del modelo | `emocion`, `riesgo`, `tematica` y `resumen` provienen de un modelo entrenado con etiquetas generadas por un modelo docente que no han sido validadas por profesionales (ver la auditoría del proyecto). No deben presentarse como una evaluación clínica. |
| Contenido de `respuesta` | La API valida su forma (longitud, no vacía, línea de ayuda en `CRITICO`), no si el texto es clínicamente adecuado. |
| Transporte | La API sirve HTTP. El cifrado lo aporta el túnel (ngrok); la API no controla lo que el túnel registre. |

## Pruebas

```bash
pip install fastapi uvicorn pydantic -r requirements-dev.txt
python -m pytest -q
```

No se necesita GPU, `torch` ni `unsloth`: las pruebas sustituyen el modelo por un generador falso y el reloj por uno inyectable, y usan una base SQLite temporal. `requirements-dev.txt` solo añade `pytest` y `httpx`; `fastapi`, `uvicorn` y `pydantic` también vienen en `requirements.txt`.

| Archivo | Cubre |
|---|---|
| `tests/test_api_characterization.py` | `/health` sin cargar el modelo, importación sin la pila de ML, prompt de un solo turno, truncado, historial, guardrail y JSON con vallas de código. |
| `tests/test_api_safety.py` | Capa de seguridad en `/analizar`: riesgo máximo, recursos, modelo que falla o devuelve basura, y que no se imprime texto. |
| `tests/test_api_validation.py` | Contrato de respuesta, `procesado`, normalización de riesgo y emoción, y límites de entrada. |
| `tests/test_api_sessions.py` | Sesiones, autenticación Bearer y clave de API, aislamiento, borrado, retención, riesgo de sesión, CORS, host y puerto, y que no se registran tokens. |
| `tests/test_crisis_safety.py` | Normalización, patrones de crisis y exclusiones figurativas. |
| `tests/test_model_output.py` | Extracción de JSON, normalización, límites de campos y cálculo del riesgo final. |
| `tests/test_storage.py` | Esquema, hash del token, vencimiento, purga, `secure_delete`, memoria de riesgo y configuración. |
| `tests/test_llm.py` | Carga y generación del modelo con `unsloth` y `torch` simulados. |

Las pruebas fijan el comportamiento descrito aquí, pero **no validan** la calidad clínica de la capa de seguridad ni del modelo.
