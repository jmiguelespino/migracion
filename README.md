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

### Endpoints del servidor

- `GET  /` → sirve `index.html`
- `POST /api/key` → guarda la API key en memoria
- `POST /api/zipinfo` → recibe el ZIP y devuelve el resumen real del sistema
- `POST /api/claude` → reenvía la consulta a `api.anthropic.com/v1/messages`

## Cómo correr

```bash
python servidor.py
# luego abrir http://localhost:8080
```

Requisitos: Python 3, conexión a internet y una API key de Anthropic
(<https://console.anthropic.com>).

## Notas de la última revisión

- **ZIP real**: el análisis ya no se basa solo en el nombre/tamaño del
  archivo; lee el contenido y la estructura de las tablas.
- **`max_tokens`** subido (4000 análisis / 8000 generación) para evitar
  respuestas truncadas.
- **Timeout** del proxy ampliado a 180 s.
- Parseo de JSON más robusto y aviso explícito cuando la respuesta se corta
  por longitud (`stop_reason: max_tokens`).
- Lanzador para macOS/Linux además del `.bat` de Windows.
