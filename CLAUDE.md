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

- **PREMISA RECTORA**: la app generada debe (1) cumplir **TODOS** los
  requerimientos/utilidades del sistema original (paridad funcional: tablas,
  datos, índices, menús, reportes, imágenes, reglas) y (2) ser una versión
  **mejorada y optimizada** — visual y de UX — no una copia mínima. Toda mejora
  nueva debe respetar ambas cosas a la vez.
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
- **No alcanza con compilar: el backend generado debe ARRANCAR.** Generá un ZIP
  de prueba, descomprimilo y corré `python -m uvicorn backend.app:app` para
  confirmar que levanta y responde (GET / → 200).

## Trampas conocidas (NO repetir)

- **JSON vs Python (`true`/`false`/`null`)**: en Python se escribe `True`,
  `False`, `None` — nunca `true`/`false`/`null`. Si generás código Python que
  incrusta datos, **no pegues `json.dumps()` como literal Python** (produce
  `true`/`false`/`null` y rompe con `NameError`). Serializá a un `.json` aparte
  y cargalo en runtime con `json.load()` (así está hoy en `scaffold.py` →
  `backend/meta.json`). Esto además evita problemas de escapado de comillas y
  backslashes (p. ej. patrones regex).
- **`.bat` de Windows con CRLF**: usá siempre `set "VAR=valor"` CON comillas.
  Sin comillas, el salto `\r\n` deja un retorno de carro pegado a la variable y
  `%VAR% ...` se expande mal.
- **`uvicorn` en Windows**: invocá `python -m uvicorn` (no `uvicorn` directo,
  que suele quedar fuera del PATH en `Scripts\`).

## Flujo de entrega

- Desarrollar en la rama `claude/serene-hypatia-kvew2c`.
- **Mergear a `main` vía Pull Request** (no push directo a main).
- Commits y PRs en español, claros.

## Tipos de archivo VFP/FoxPro — qué se lee y para qué (REFERENCIA OBLIGATORIA)

> Al trabajar con un sistema Visual FoxPro hay que considerar **TODOS** estos
> tipos. Un componente de VFP casi siempre son **3 archivos** (datos + memo +
> índice). Si se lee solo uno, se pierde información. Esta tabla es la fuente de
> verdad: si se agrega soporte para un tipo nuevo, actualizar acá.

### Se leen y procesan (aportan info al sistema migrado)

| Ext | Qué es | Pareja(s) | Qué extraemos | Dónde |
|-----|--------|-----------|---------------|-------|
| `.pjx` | Proyecto (manifiesto) | `.pjt` (memo) | Archivos del sistema, programa principal | `parse_pjx` |
| `.dbc` | Database Container | `.dct` (memo), `.dcx` (índice) | Relaciones FK, Caption de campos, stored procs, vistas | `parse_dbc` |
| `.dbf` | Tabla | `.fpt` (memo), `.cdx` (índice) | Estructura (campos/tipos) + **datos reales** | `parse_dbf_structure`, `read_dbf_records` |
| `.cdx`/`.idx` | Índice compuesto | — | Expresiones de clave → índices SQLite | `parse_cdx_expressions` |
| `.scx` | Formulario | `.sct` (memo) | Layout: ControlSource, Caption, orden por Top | `parse_scx_controls` |
| `.frx` | Reporte | `.frt` (memo) | Se listan → vista de consulta por reporte | (inventario) |
| `.vcx` | Biblioteca de clases | `.vct` (memo) | Métodos (OBJCODE) → muestras de código/lógica | `parse_vcx_methods` |
| `.mpr` | Menú (código generado) | `.mpx` (compilado) | Estructura PAD/POPUP/BAR + acciones | `parse_mpr_menu` |
| `.mnx` | Menú (tabla DBF) | `.mnt` (memo) | Ítems + PROCEDURE (fallback si no hay `.mpr`) | `parse_mnx_menu` |
| `.prg` | Código fuente | `.fxp` (compilado) | Muestra de código (lógica de negocio) | (samples) |
| `.h` | Include (`#DEFINE`) | — | Constantes/config del sistema | (samples) |
| `.txt` | Notas/documentación | — | Contexto adicional | (samples) |
| imágenes | `.bmp .jpg .gif .png .ico` etc. | — | Se copian a `web/assets/` | `extract_assets_from_zip` |

**Regla de las parejas:** al leer un `.dbf`/`.dbc`/`.scx`/`.vcx`/`.mnx` SIEMPRE
buscar su **memo** (`.fpt`/`.dct`/`.sct`/`.vct`/`.mnt`) — los campos Memo/General
(Caption, código, propiedades) viven ahí; sin el memo se leen vacíos.

### NO se leen — a propósito (con su motivo)

| Ext | Qué es | Por qué se ignora |
|-----|--------|-------------------|
| `.dcx` | Índice del `.dbc` | Solo B-tree; toda la info está en `.dbc`+`.dct` |
| `.fxp` `.mpx` `.app` `.exe` | Binarios **compilados** | El fuente (`.prg`/`.mpr`) ya se lee; el binario no es legible |
| `.tbk` | Backup automático de tabla | No son datos vigentes |
| `.cdx` (de tablas sistema) | Índice | Solo se usan expresiones, no el árbol |
| `Thumbs.db` | Cache de miniaturas de Windows | No es del sistema |

### Tablas de sistema VFP excluidas (no son datos de negocio)

`foxuser`, `vfpgraph`, `foxcode`, `foxtask`, `foxref` → ver `VFP_SYSTEM_TABLES`
en `servidor.py`. Generarían ABMs inútiles. Si aparece otra tabla de sistema,
agregarla a ese set.
