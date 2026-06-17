#!/usr/bin/env bash
# LegacyMigrator - Lanzador para macOS / Linux
# Uso:  ./iniciar.sh   (o:  bash iniciar.sh)
cd "$(dirname "$0")" || exit 1

echo "=========================================="
echo "  LegacyMigrator - Iniciando servidor..."
echo "=========================================="
echo "  Abrí tu navegador en http://localhost:8080"
echo "  Para cerrar presioná Ctrl+C"
echo

# Modo gratuito: si Ollama está instalado pero no corriendo, lo arrancamos con
# una configuración de rendimiento (usa todos los recursos disponibles).
if command -v ollama >/dev/null 2>&1; then
  if ! curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo "  Iniciando Ollama (config de rendimiento)..."
    OLLAMA_FLASH_ATTENTION=1 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 \
      OLLAMA_KEEP_ALIVE=30m nohup ollama serve >/tmp/ollama.log 2>&1 &
  fi
fi

# Intenta abrir el navegador automáticamente (no falla si no puede)
( sleep 1; (command -v xdg-open >/dev/null && xdg-open http://localhost:8080) \
  || (command -v open >/dev/null && open http://localhost:8080) ) >/dev/null 2>&1 &

# Usa python3 si existe, si no python
if command -v python3 >/dev/null 2>&1; then
  python3 servidor.py
else
  python servidor.py
fi
