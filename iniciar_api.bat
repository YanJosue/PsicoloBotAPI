@echo off
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