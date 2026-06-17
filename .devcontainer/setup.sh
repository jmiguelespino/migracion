#!/usr/bin/env bash
# Prepara el Codespace para el modo gratuito de LegacyMigrator (Ollama, sin API key).
set -e

echo "==> Instalando Ollama (si hace falta)..."
if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
fi

echo "==> Arrancando el servidor de Ollama (config de rendimiento)..."
# Rendimiento: flash-attention (más rápido y menos memoria), una sola sesión
# en paralelo para dedicarle todos los recursos a la petición, un solo modelo
# cargado y mantenerlo caliente. Ollama ya usa todos los núcleos físicos + GPU.
export OLLAMA_FLASH_ATTENTION=1
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_MAX_LOADED_MODELS=1
export OLLAMA_KEEP_ALIVE=30m
nohup ollama serve > /tmp/ollama.log 2>&1 &

echo "==> Esperando a que Ollama responda..."
for _ in $(seq 1 30); do
  if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# Modelo de código liviano: rápido en CPU (los Codespaces no tienen GPU).
# Si querés más calidad y tenés recursos, probá:  ollama pull qwen2.5-coder
echo "==> Descargando modelo qwen2.5-coder:1.5b (puede tardar)..."
ollama pull qwen2.5-coder:1.5b || echo "   (no se pudo descargar ahora; podés correr 'ollama pull qwen2.5-coder:1.5b' luego)"

echo ""
echo "==> Listo. Para iniciar la app:  python servidor.py"
echo "    Luego abrí el puerto 8080 y elegí el motor '🆓 Gratis'."
