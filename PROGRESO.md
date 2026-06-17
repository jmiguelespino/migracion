# Estado y progreso del proyecto — LegacyMigrator

> Bitácora para retomar el proyecto en una sesión nueva sin repetir lo ya hecho.
> Última actualización: 2026-06-17.

## 🎯 Objetivo

Un agente que, **partiendo de un ZIP de un sistema legacy** (Visual FoxPro /
FoxPro / COBOL), **escribe una app moderna que expone LAS MISMAS UTILIDADES**
del sistema original: todas las pantallas/ABM, los menús (navegación), los
reportes y la lógica de negocio. Paridad funcional, no una muestra.

## ✅ Estado actual (qué funciona)

- **Lectura real del ZIP** (`servidor.py` → `analyze_zip`): estructura de tablas
  `.dbf` (campos/tipos/registros), controles de formularios `.scx`, reportes
  `.frx`, programas `.prg` (muestras) y **menús** `.mpr`/`.mnx`.
- **App completa (cobertura total) — determinística, SIN IA** (`scaffold.py`):
  genera una app ejecutable **FastAPI + SPA** (un proceso, sin Node) con:
  - un **ABM** (alta/baja/modificación/listado) por **cada** tabla sobre SQLite,
  - los **menús** como navegación,
  - una **vista de consulta** por **cada** reporte,
  - `COBERTURA.md` que enumera todo y su estado.
  Botón en la UI: **📦 Generar app completa**. No depende de ningún modelo.
- **Enriquecimiento por IA (2ª etapa)** — botón **✨ App completa + IA**: por
  cada tabla hace una llamada chica (~1500 tokens) pidiendo JSON con título,
  etiquetas legibles, campos obligatorios, ayuda y **reglas de negocio** (del
  `.prg`). Se hornea en el scaffold. Hasta 12 tablas, con progreso; si una
  falla, esa pantalla queda con el scaffold base (degradación elegante).
- **Flujo "por fases"** (anterior): analizar → generar fase → descargar ZIP de
  la fase. Sigue existiendo (botón **⚡ Analizar sistema (por fases)**).
- **Motores de IA**: **Claude** (API key) y **🆓 Gratis** (Ollama local). El
  modo **Demo** fue ELIMINADO (era salida simulada, redundante).

## 🧱 Arquitectura / archivos

| Archivo | Rol |
|---------|-----|
| `servidor.py` | Servidor local stdlib (`http://localhost:8080`). Proxy a Claude/Ollama, lector de ZIP, parser de menús, endpoints. |
| `scaffold.py` | Generador determinístico de la app migrada (cobertura total) + fusión del enriquecimiento IA. |
| `index.html` | UI completa (HTML/CSS/JS en un archivo). |
| `INICIAR.bat` / `iniciar.sh` | Lanzadores (arrancan Ollama con config de rendimiento). |
| `.devcontainer/` | Codespaces (instala Ollama, abre 8080). |
| `.vscode/tasks.json` | Arranca el server al abrir la carpeta. |

### Endpoints (`servidor.py`)
- `GET /` y `GET /api/ollama/models`
- `POST /api/key`, `/api/zipinfo`, `/api/claude`, `/api/ollama`
- `POST /api/zip` (ZIP de una fase) · `POST /api/scaffold` (app completa)

## 🔑 Decisiones clave

- **Cobertura garantizada = determinística** (no depende del modelo). La IA solo
  *enriquece*. Esto evita timeouts/JSON roto del modelo local.
- **App generada = FastAPI + SPA vanilla en un proceso** (sin Node) para que
  corra fácil en máquinas modestas: `pip install fastapi uvicorn`.
- **Enriquecimiento por pantalla** (JSON chico), no archivos enteros: viable en
  CPU sin GPU.

## 💻 Entorno del usuario

- HP Laptop 15-fd0xxx — **Intel i3-N305** (8 núcleos, sin HT, 1.8 GHz),
  **8 GB RAM**, **Intel UHD Graphics (sin GPU usable por Ollama → CPU only)**.
- Implica: usar `qwen2.5-coder:1.5b` (no el 7B). Ollama no acelera con iGPU
  Intel. Cerrar el navegador pesado al generar.

## 🛠️ Cómo correr / probar

```bash
python servidor.py        # o INICIAR.bat en Windows
# http://localhost:8080
```
Flujo recomendado: subir ZIP → **📦 Generar app completa** (instantáneo) o
**✨ App completa + IA** (lento en CPU). Descarga `app-migrada.zip`; adentro
`COBERTURA.md` + cómo correr la app generada (FastAPI).

## 📌 Próximos pasos / pendientes

- [x] **Validaciones reales en el backend generado**: derivadas del esquema
      `.dbf` (tipos numéricos y longitud máxima) + `requerido` que aporta la IA.
      Devuelven 422 con mensajes claros y la SPA los muestra. _Las reglas de
      negocio de texto siguen siendo informativas (panel)._
- [ ] Enriquecer **todas** las tablas (hoy tope 12) y/o on-demand por pantalla.
- [ ] Convertir las **reglas de negocio de texto** en validaciones estructuradas
      ejecutables (rango/regex/condiciones), no solo informativas.
- [ ] Generar pantallas según el **layout real** de los controles `.scx`
      (posición/orden), no solo la lista de campos.
- [ ] Soportar otras tecnologías destino en el scaffold (hoy: FastAPI + SPA).
- [ ] Mapear cada formulario `.scx` ↔ tabla con más precisión (hoy por nombre).
- [ ] Probar end-to-end con el ZIP real del usuario (314 archivos, 15 tablas).

## 🔄 Convención de trabajo

- Desarrollar en `claude/serene-hypatia-kvew2c`, **mergear a `main` vía PR**.
- Historial: PRs #1–#11 (modo gratuito Ollama, Codespaces, timeouts, multihilo,
  rendimiento, cobertura total/scaffold, enriquecimiento IA, baja de Demo).
