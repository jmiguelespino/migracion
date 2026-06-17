@echo off
title LegacyMigrator - Servidor Local
color 0A
echo.
echo  ==========================================
echo    LegacyMigrator - Iniciando servidor...
echo  ==========================================
echo.

REM Modo gratuito: config de rendimiento de Ollama (usa todos los recursos).
REM OJO: si Ollama ya esta corriendo como app de bandeja, estos ajustes NO le
REM aplican; ponelos como variables de entorno del usuario y reinicia Ollama.
set "OLLAMA_FLASH_ATTENTION=1"
set "OLLAMA_NUM_PARALLEL=1"
set "OLLAMA_MAX_LOADED_MODELS=1"
set "OLLAMA_KEEP_ALIVE=30m"
where ollama >nul 2>&1
if %errorlevel%==0 (
  curl -s http://localhost:11434/api/tags >nul 2>&1
  if errorlevel 1 (
    echo  Iniciando Ollama con config de rendimiento...
    start "" /min ollama serve
  )
)

echo.
echo  Abriendo navegador en http://localhost:8080
echo  Para cerrar este servidor presiona Ctrl+C
echo.
start "" http://localhost:8080
python servidor.py
pause
