# LegacyMigrator

Agente web que ayuda a empresas con sistemas **legacy** (Visual FoxPro, FoxPro,
COBOL) a migrarlos a tecnologías modernas de forma asistida por IA (Claude).

## Flujo

1. El usuario sube un ZIP con el sistema legacy completo.
2. El agente **lee el ZIP de verdad**: cuenta formularios (`.scx`), reportes
   (`.frx`), programas (`.prg`) y extrae la **estructura real de las tablas
   `.dbf`** (campos y tipos, leyendo el header DBF sin librerías externas).
3. Con esos datos reales, Claude propone un plan de migración por fases.
4. El usuario elige una fase y el agente genera el código moderno
   (backend + frontend + tests).
5. El usuario descarga la fase y continúa con la siguiente.

## Arquitectura

| Archivo | Rol |
|---------|-----|
| `servidor.py` | Servidor local en `http://localhost:8080`. Proxy hacia la API de Anthropic **y** lector del ZIP (`/api/zipinfo`). Solo usa la librería estándar de Python. |
| `index.html` | Interfaz completa (HTML/CSS/JS en un archivo). |
| `INICIAR.bat` | Lanzador para Windows. |
| `iniciar.sh` | Lanzador para macOS / Linux. |
| `LEEME.txt` | Instrucciones para el usuario final. |

### Motores de IA

El agente puede generar con tres motores (se eligen en la barra lateral):

| Motor | API key | Costo | Cuándo usarlo |
|-------|---------|-------|---------------|
| **Claude** | Sí (Anthropic) | Tokens | Mejor calidad de migración |
| **🆓 Gratis (Ollama)** | **No** | **Gratis, local** | Sin clave ni internet; usa un modelo local de código (p. ej. `qwen2.5-coder`) |
| **🧪 Demo** | No | Gratis | Solo simula la salida (sin LLM), para probar el flujo |

Para el modo gratuito necesitás [Ollama](https://ollama.com) instalado y un
modelo descargado:

```bash
ollama pull qwen2.5-coder   # recomendado para generar código
```

El servidor detecta los modelos locales automáticamente (`GET /api/ollama/models`).
Podés cambiar la URL o el modelo por defecto con las variables de entorno
`OLLAMA_URL` y `OLLAMA_MODEL`.

### Endpoints del servidor

- `GET  /` → sirve `index.html`
- `GET  /api/ollama/models` → lista los modelos locales de Ollama (modo gratuito)
- `POST /api/key` → guarda la API key en memoria
- `POST /api/zipinfo` → recibe el ZIP y devuelve el resumen real del sistema
- `POST /api/claude` → reenvía la consulta a `api.anthropic.com/v1/messages`
- `POST /api/ollama` → genera con un modelo local vía Ollama (sin API key)
- `POST /api/zip` → arma el ZIP descargable de una fase (código en `src/`,
  tests en `tests/` y un `README.md` con instrucciones)

## Cómo correr

```bash
python servidor.py
# luego abrir http://localhost:8080
```

### En GitHub Codespaces (en el navegador)

El repo incluye un `.devcontainer` que instala Ollama, lo arranca y descarga un
modelo de código liviano automáticamente:

1. En GitHub: **Code → Codespaces → Create codespace on main**.
2. Esperá a que termine la preparación inicial (instala Ollama + el modelo).
3. El servidor arranca **solo** (task de VS Code al abrir la carpeta). Si no,
   ejecutá `python servidor.py`.
4. Abrí el puerto **8080** (se ofrece solo) y elegí el motor **🆓 Gratis**.

> Los Codespaces no tienen GPU: el modo gratuito anda pero es más lento. Para el
> motor **Claude** solo pegás tu API key. **GitHub Pages no sirve** porque la app
> necesita el backend `servidor.py`.


Requisitos: Python 3 y, según el motor elegido, una API key de Anthropic
(<https://console.anthropic.com>) **o** [Ollama](https://ollama.com) con un
modelo de código instalado (modo gratuito, sin clave). El modo demo no
requiere nada.

## Notas de la última revisión

- **ZIP real**: el análisis ya no se basa solo en el nombre/tamaño del
  archivo; lee el contenido y la estructura de las tablas.
- **`max_tokens`** subido (4000 análisis / 8000 generación) para evitar
  respuestas truncadas.
- **Timeout** del proxy ampliado a 180 s.
- Parseo de JSON más robusto y aviso explícito cuando la respuesta se corta
  por longitud (`stop_reason: max_tokens`).
- Lanzador para macOS/Linux además del `.bat` de Windows.
- **Descarga por fase en ZIP**: al generar una fase se descarga un `.zip` con
  cada archivo en `src/`, los tests en `tests/` y un `README.md` con las
  instrucciones (antes era un único `.txt`).
- **Modo gratuito (Ollama)**: generación real **sin API key ni tokens** usando
  un modelo local de código (p. ej. `qwen2.5-coder`). El servidor detecta los
  modelos instalados y la UI los lista automáticamente.
- **Modo demo**: probar el flujo completo sin API key ni tokens (análisis y
  generación simulados a partir de las tablas reales del ZIP).
- **Lógica real**: el código fuente de los `.prg` relacionados con cada fase se
  envía a Claude para portar la lógica de negocio, no solo la estructura.
- **Estado persistente**: el análisis y las fases se guardan en `localStorage`,
  así no se pierden al recargar (botón "Nuevo proyecto" para reiniciar).
