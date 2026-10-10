@echo off

REM ------------------------------------------------------------
REM Configuracion opcional. Para activar una linea, quita "REM ".
REM Por defecto no se cambia nada: la API escucha solo en
REM 127.0.0.1:8000 (ngrok funciona porque reenvia a localhost).
REM Detalle de cada variable: README.md e INSTRUCCIONES.md.
REM ------------------------------------------------------------

REM Exponer la API a la red local (LAN). El trafico va por HTTP sin
REM cifrar: usalo solo en una red de confianza.
REM set "PSICOLOBOT_HOST=0.0.0.0"
REM set "PSICOLOBOT_PORT=8000"

REM Permitir una app web (por ejemplo Expo web) desde este origen.
REM Varios origenes se separan con comas.
REM set "PSICOLOBOT_CORS_ORIGINS=http://localhost:8081"

REM Exigir la cabecera X-API-Key. No es un secreto: viaja dentro de la app.
REM set "PSICOLOBOT_API_KEY=cambia-esta-clave"

REM Base de datos (por defecto chat_memory.db en la carpeta de trabajo).
REM set "PSICOLOBOT_DB_PATH=C:\psicolobot\chat_memory.db"

REM Dias que se conserva una sesion con sus mensajes (30) y horas que se
REM recuerda el mayor riesgo de una conversacion (24).
REM set "PSICOLOBOT_RETENTION_DAYS=30"
REM set "PSICOLOBOT_SESSION_RISK_HOURS=24"

echo ==========================================
echo    INICIANDO API PSICOLOBOT V3 LOCAL
echo ==========================================
echo.
echo [1] Verificando instalacion de dependencias...
pip install -r requirements.txt

echo.
echo [2] Levantando Servidor FastAPI + Modelo LoRA...
python psicolobot_api.py

pause