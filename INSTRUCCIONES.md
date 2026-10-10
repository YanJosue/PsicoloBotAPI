# Psicolobot API — Guía de arranque

Esta guía explica cómo levantar la API de inferencia de Psicolobot v3 en una computadora local con placa de video NVIDIA y cómo conectarla con la aplicación móvil. Para el detalle técnico (rutas, campos, límites de la capa de seguridad) consulta [README.md](README.md).

> Psicolobot es un prototipo de apoyo emocional. No es terapia, no diagnostica y no sustituye un servicio de emergencia.

## Requisitos previos

1. **Windows 10/11** o Linux.
2. **Placa de video NVIDIA** (mínimo 6 GB de VRAM, ideal 8 GB o más).
3. **Python 3.10+** instalado (marca la casilla "Add to PATH" al instalar).
4. **Ngrok** instalado y autenticado, si vas a usar la app desde un teléfono.

## Arranque rápido

1. Haz doble clic en **`iniciar_api.bat`** (o, desde una terminal, `pip install -r requirements.txt` y luego `python psicolobot_api.py`).
2. Espera a leer `✅ Modelo listo` y `Application startup complete` en la consola negra.
3. Abre `http://localhost:8000/health`: debe responder `"status": "ok"` y `"model_loaded": true`.

El script instala las dependencias, **carga el modelo en la placa de video antes de aceptar peticiones** y levanta el servidor en `http://localhost:8000`.

*Nota: la primera vez tarda varios minutos en descargar los tensores base del modelo Llama 3 (aprox. 5 GB). Las siguientes veces arranca en segundos.*

## Quién puede conectarse

Por defecto la API escucha **solo en tu propia computadora** (`127.0.0.1`). Nadie más en tu red puede alcanzarla, y ngrok sigue funcionando porque reenvía el tráfico a `localhost`.

| Necesitas | Qué hacer |
|---|---|
| Probar desde un teléfono, en cualquier red | **Ngrok** (recomendado, ver abajo). |
| Probar desde un teléfono en tu misma red Wi-Fi | **Red local** con `PSICOLOBOT_HOST=0.0.0.0`. |
| Probar la app en el navegador (Expo web) | **CORS** con `PSICOLOBOT_CORS_ORIGINS`. |

### Opción 1: Ngrok

Con la API en marcha, abre **otra** terminal y ejecuta:

```bash
ngrok http 8000
```

Ngrok muestra una URL pública (por ejemplo `https://1234-abcd.ngrok-free.app`). Esa es la URL que usará la app. Si cambiaste el puerto con `PSICOLOBOT_PORT`, usa ese mismo número en el comando.

### Opción 2: Red local (LAN)

Para que otros dispositivos de tu red alcancen la API, arráncala escuchando en todas las interfaces:

```bash
# CMD (Windows)
set "PSICOLOBOT_HOST=0.0.0.0"
python psicolobot_api.py
```

```powershell
# PowerShell (Windows)
$env:PSICOLOBOT_HOST = "0.0.0.0"
python psicolobot_api.py
```

En el teléfono, usa `http://<IP-de-tu-PC>:8000` como URL base. Ten en cuenta:

- El tráfico va por **HTTP sin cifrar**. Úsalo solo en una red de confianza, nunca en una red pública.
- Cualquier dispositivo de esa red podrá intentar usar la API. Desactívalo cuando termines de probar.
- El firewall de Windows puede pedir permiso la primera vez.

### Opción 3: Expo web (CORS)

Una app nativa no necesita CORS; solo un cliente que corre en el navegador. Para Expo web:

```bash
set "PSICOLOBOT_CORS_ORIGINS=http://localhost:8081"
```

Puedes listar varios orígenes separados por comas. Sin esta variable, la API no acepta peticiones entre orígenes. Reinicia la API después de cambiarla.

## Cómo configurar variables de entorno

Las variables se leen del entorno del proceso y solo afectan a lo que se inicia desde la misma terminal. Define la variable y después inicia la API en esa misma ventana; para que un cambio surta efecto, reinicia la API.

Si prefieres no escribirlas cada vez, abre `iniciar_api.bat`: trae líneas comentadas con `REM` para cada variable; quita el `REM ` de las que quieras activar. Por defecto no cambia nada.

| Variable | Por defecto | Para qué sirve |
|---|---|---|
| `PSICOLOBOT_HOST` | `127.0.0.1` | Dirección donde escucha. `0.0.0.0` abre la API a la red local. |
| `PSICOLOBOT_PORT` | `8000` | Puerto del servidor. |
| `PSICOLOBOT_CORS_ORIGINS` | vacío | Orígenes web permitidos, separados por comas. |
| `PSICOLOBOT_API_KEY` | sin definir | Si se define, la app debe enviar el mismo valor en `X-API-Key`. |
| `PSICOLOBOT_DB_PATH` | `chat_memory.db` | Ubicación del archivo de base de datos. |
| `PSICOLOBOT_RETENTION_DAYS` | `30` | Días que se conserva una sesión con sus mensajes. |
| `PSICOLOBOT_SESSION_RISK_HOURS` | `24` | Horas que se recuerda el mayor riesgo de una conversación. |

La tabla completa (cuándo se lee cada una y qué pasa con un valor inválido) está en la sección Configuración del [README.md](README.md).

## Conectar la aplicación

En el proyecto móvil (Expo) define:

| Variable | Valor |
|---|---|
| `EXPO_PUBLIC_API_URL` | URL **base** de la API, **sin ruta**. Por ejemplo `https://1234-abcd.ngrok-free.app`, no `https://1234-abcd.ngrok-free.app/analizar`. |
| `EXPO_PUBLIC_API_KEY` | Solo si definiste `PSICOLOBOT_API_KEY` en la API: el mismo valor. |

La app construye por sí misma las rutas `/sesion` y `/analizar`. Reinicia Expo después de cambiar estos valores. Si ngrok te asigna una URL nueva, actualiza `EXPO_PUBLIC_API_URL`.

Recuerda que `localhost` dentro de un teléfono es el propio teléfono: para un dispositivo físico usa la URL de ngrok o la IP de tu PC con la opción de red local.

### Flujo esperado entre la app y la API

1. Tras mostrar el consentimiento (aceptar términos y confirmar mayoría de edad), la app llama a `POST /sesion`.
2. La API devuelve un `token`, que se entrega una sola vez. La app lo guarda y lo envía como `Authorization: Bearer <token>` en cada petición.
3. Cada mensaje se envía a `POST /analizar`.
4. Para borrar los datos de la persona, la app llama a `DELETE /sesion`, que elimina la sesión y sus mensajes del servidor.

Prueba manual (bash, con la API en `localhost`):

```bash
curl -X POST http://localhost:8000/sesion \
  -H "Content-Type: application/json" \
  -d '{"acepta_terminos": true, "mayor_de_edad": true, "version_consentimiento": "2026-10-v1"}'

curl -X POST http://localhost:8000/analizar \
  -H "Authorization: Bearer <token-recibido>" -H "Content-Type: application/json" \
  -d '{"texto": "Me siento muy triste y no quiero salir de la cama"}'
```

## Datos que guarda la API

- Las conversaciones se guardan en `chat_memory.db` (texto de la persona y respuesta del asistente, con su hora), **hasta 30 días** desde que se abre la sesión (`PSICOLOBOT_RETENTION_DAYS`).
- El token no se guarda en claro, solo su hash SHA-256.
- `DELETE /sesion` elimina la sesión y todos sus mensajes del servidor.
- `chat_memory.db` contiene texto sensible: no lo compartas ni lo subas a un repositorio (ya está en `.gitignore`).

## Limpiar la base heredada (tabla `memory`)

Las versiones anteriores de la API guardaban todas las conversaciones **sin cifrar** en una tabla llamada `memory`. La versión actual no la usa, pero **tampoco la borra**: si tu `chat_memory.db` viene de una versión anterior, ese historial sigue ahí. Elimínalo así, con la API detenida:

1. Confirma si existe: `sqlite3 chat_memory.db ".tables"` (si aparece `memory`, hay historial viejo).
2. Bórrala, conservando las sesiones actuales:

   ```bash
   sqlite3 chat_memory.db "PRAGMA secure_delete = ON; DROP TABLE memory; VACUUM;"
   ```

   Si no tienes `sqlite3`, el equivalente con Python:

   ```bash
   python -c "import sqlite3; c = sqlite3.connect('chat_memory.db'); c.execute('PRAGMA secure_delete = ON'); c.execute('DROP TABLE IF EXISTS memory'); c.commit(); c.execute('VACUUM'); c.close()"
   ```

3. Alternativa más simple: borra el archivo `chat_memory.db`. Se crea uno nuevo al siguiente arranque, pero se pierden las sesiones activas y cada persona tendrá que aceptar el consentimiento otra vez.

## Solución de problemas

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| `401` con `detail` "Sesión inválida o expirada." y cabecera `WWW-Authenticate: Bearer` | La sesión no existe, venció (30 días desde su creación) o se borró. | La app debe pedir el consentimiento de nuevo y abrir una sesión con `POST /sesion`. |
| `401` con `detail` "Clave de API inválida." y cabecera `WWW-Authenticate: ApiKey` | `PSICOLOBOT_API_KEY` está definida y `X-API-Key` falta o no coincide. | Haz que `EXPO_PUBLIC_API_KEY` tenga el mismo valor, o quita `PSICOLOBOT_API_KEY`. No pidas consentimiento de nuevo: no es un problema de sesión. |
| `422` en `POST /sesion` | `acepta_terminos` o `mayor_de_edad` no son exactamente `true`, o falta `version_consentimiento`. | Revisa el cuerpo enviado. |
| `422` en `POST /analizar` | `texto` vacío, solo espacios o de más de 4000 caracteres. | Valida el texto en la app. No muestres el `detail` del error a la persona: incluye el texto enviado. |
| La app no conecta desde el teléfono | `EXPO_PUBLIC_API_URL` apunta a `localhost`, o la API solo escucha en `127.0.0.1`. | Usa la URL de ngrok, o la IP de tu PC con `PSICOLOBOT_HOST=0.0.0.0`. |
| Error de CORS en la consola del navegador | El origen de Expo web no está permitido. | Define `PSICOLOBOT_CORS_ORIGINS` con ese origen y reinicia la API. |
| Un cambio de variable no surte efecto | La API se inició antes de definirla, o desde otra terminal. | Define la variable y reinicia la API en la misma terminal. |
| El puerto 8000 está ocupado | Otro programa lo usa. | Define `PSICOLOBOT_PORT` (y usa ese mismo número en `ngrok http`). |
| `/health` muestra `"model_loaded": false` | La API se arrancó con `uvicorn` en lugar de `python psicolobot_api.py`. | El modelo se carga en la primera petición a `/analizar`. Arranca con `python psicolobot_api.py` para precargarlo. |

## Contenido del paquete

- `psicolobot_api.py`: servidor FastAPI (rutas, autenticación y construcción del prompt).
- `llm.py`: carga y generación del modelo LoRA.
- `crisis_safety.py`: capa de seguridad ante crisis, independiente del modelo.
- `model_output.py`: validación y normalización de la salida del modelo.
- `session_store.py`: base de datos SQLite de sesiones y mensajes.
- `lora_psicolobot_v3/`: pesos del entrenamiento (fine-tuning) del modelo.
- `datasets/base_conocimientos.csv`: léxico para la fase 1 del análisis.
- `iniciar_api.bat`: script de auto-arranque.
- `requirements.txt` y `requirements-dev.txt`: dependencias de la API y de las pruebas.
- `tests/`: pruebas automáticas (`python -m pytest -q`, no requieren GPU).
