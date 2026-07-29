# 🧠 Psicolobot API — Paquete de Despliegue

Este paquete contiene todo lo necesario para levantar la API de inferencia de Psicolobot v3 en una computadora local con placa de video NVIDIA y exponerla mediante **Ngrok**.

## 📋 Requisitos Previos en la PC de Destino
1. **Windows 10/11** o Linux.
2. **Placa de video NVIDIA** (mínimo 6GB de VRAM, ideal 8GB+).
3. **Python 3.10+** instalado (marcar la casilla "Add to PATH" al instalar).
4. **Ngrok** instalado y autenticado (para exponer el puerto).

## 🚀 Instrucciones de Arranque

### Paso 1: Levantar la API
Simplemente haz doble clic en el archivo **`iniciar_api.bat`**.
Este script:
- Verificará e instalará todas las dependencias (`requirements.txt`).
- Cargará el modelo a la placa de video.
- Levantará el servidor en `http://localhost:8000`.

*Nota: La primera vez que se ejecute, tardará varios minutos en descargar los tensores base del modelo Llama 3 desde internet (aprox 5GB). Las siguientes veces arrancará en segundos.*

Cuando en la consola negra leas `Application startup complete` y `✅ Modelo listo`, la API estará funcionando.

### Paso 2: Exponer la API con Ngrok
Abre **otra** terminal o símbolo del sistema (CMD) y ejecuta:
```bash
ngrok http 8000
```
Ngrok te generará una URL pública (ej. `https://1234-abcd.ngrok-free.app`).

### Paso 3: Conectar tu Aplicación
En el código de tu aplicación cliente (React, Flutter, etc), configura esa URL de Ngrok apuntando al endpoint de análisis:

```http
POST https://1234-abcd.ngrok-free.app/analizar
Content-Type: application/json

{
  "texto": "Me siento muy triste y no quiero salir de la cama..."
}
```

## 📁 Contenido del Paquete
- `psicolobot_api.py`: El servidor FastAPI.
- `lora_psicolobot_v3/`: Los pesos del entrenamiento (Fine-Tuning) de tu modelo.
- `datasets/base_conocimientos.csv`: El lexicón para la Fase 1 del análisis.
- `iniciar_api.bat`: Script de auto-arranque.
- `requirements.txt`: Dependencias de Python.