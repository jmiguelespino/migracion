# CLAUDE.md — contexto para el agente

> **Antes de empezar, leé `PROGRESO.md`**: tiene el estado, las decisiones y los
> pendientes del proyecto. Esto evita repetir trabajo ya hecho.

## Qué es

**LegacyMigrator**: agente que, a partir de un ZIP de un sistema legacy (Visual
FoxPro / FoxPro / COBOL), genera una **app moderna con las mismas utilidades**
del original (ABM por tabla, menús, reportes, lógica). Servidor local en Python
(stdlib) + UI en un solo `index.html`.

## Archivos clave

- `servidor.py` — servidor/proxy/lector de ZIP/parser de menús. Solo stdlib.
- `scaffold.py` — generador determinístico de la app migrada (cobertura total).
- `index.html` — UI completa (HTML/CSS/JS).
- `PROGRESO.md` — estado y próximos pasos (LEER PRIMERO).

## Principios de diseño (respetar)

- **La cobertura es determinística** (en `scaffold.py`, sin IA): garantiza que la
  app generada exponga TODAS las utilidades del ZIP. La IA solo **enriquece**.
- **App generada = FastAPI + SPA vanilla en un proceso** (sin Node), para correr
  fácil en máquinas modestas.
- **Enriquecimiento por pantalla** (JSON chico), nunca archivos enteros: el
  usuario corre en **CPU sin GPU** (i3-N305, 8 GB) con `qwen2.5-coder:1.5b`.
- Motores: **Claude** (API key) y **Gratis** (Ollama local). No hay modo Demo.

## Cómo verificar cambios

- `python3 -m py_compile servidor.py scaffold.py`
- Validar el JS embebido de `index.html` con `node --check` (extraer `<script>`).
- El backend generado por `scaffold.py` debe compilar (`py_compile`).

## Flujo de entrega

- Desarrollar en la rama `claude/serene-hypatia-kvew2c`.
- **Mergear a `main` vía Pull Request** (no push directo a main).
- Commits y PRs en español, claros.
