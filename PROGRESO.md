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
- **Flujo "por fases"** (analizar → generar fase → ZIP por fase): **ELIMINADO**.
  Dependía 100% de la IA (frágil en CPU/Ollama), no garantizaba cobertura ni que
  la app arrancara, y entregaba la salida fragmentada. Lo reemplazan 📦 y ✨, que
  cumplen la premisa rectora (cobertura total + app que corre). Se quitó el botón
  ⚡, la vista de fases y el endpoint `/api/zip`.
- **Motores de IA**: **Claude** (API key) y **🆓 Gratis** (Ollama local), usados
  solo para el enriquecimiento ✨. El modo **Demo** fue ELIMINADO (salida simulada).
- **Reanudar sesión**: el inventario del ZIP se persiste en `localStorage` apenas
  se lee (con fallback de cuota: sin muestras → sin zipInfo). Al reabrir, se
  restaura y se puede **generar la app sin volver a subir el ZIP**.

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
- `POST /api/scaffold` (app completa, determinística + enriquecimiento IA)

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
- [x] **Reglas de negocio ejecutables**: la IA devuelve validaciones
      estructuradas (`min`/`max`/`rango`/`regex`), se sanean (anti-inyección) y
      el backend generado las aplica (422). Se muestran en el panel de cada
      pantalla. Las reglas de texto libre siguen como informativas.
- [x] **Enriquecer todas las tablas** (se quitó el tope de 12): prioriza las que
      tienen señal real (formulario/`.prg`) y enriquece el resto también; tope de
      seguridad 200. Se subió `MAX_SAMPLES` a 40 (más `.prg` para lógica).
- [x] **Layout real de los `.scx`**: se parsea el memo `properties`
      (`ControlSource`, `Caption`, `Top`) → etiquetas reales, orden de campos por
      posición y **mapeo exacto formulario→tabla** (no por nombre). El ABM
      generado refleja el formulario original. (`MAX_SAMPLES`=40, `re` a nivel
      módulo.)
- [x] **Importar los datos reales de los `.dbf`**: el servidor cachea el ZIP
      subido (token) y, al generar la app, lee los registros (`read_dbf_records`,
      con memos `.fpt`, fechas, lógicos, currency, etc.) y los hornea en
      `backend/seed.json`. El backend generado los importa a SQLite en el primer
      arranque (si la tabla está vacía; idempotente). Se muestran los registros
      importados en la UI (header `X-Rows-Imported`) y en `COBERTURA.md`.
- [x] **Recrear índices (parser real de expresiones)**: `parse_cdx_expressions`
      extrae las EXPRESIONES de clave de los `.cdx`/`.idx` (incluidas las
      compuestas tipo `STR(GRUPO)+STR(MENU)` y con funciones tipo
      `UPPER(NOMBRE)`), saca los campos en orden y genera **índices compuestos**
      (`CREATE INDEX ... (col1, col2)`) en el backend. Heurística defensiva:
      acepta runs que son un campo exacto, traen función VFP, o concatenan con
      '+', para no confundir datos del árbol B con expresiones.
- [x] **Leer el .dbc (Database Container)**: `parse_dbc` extrae relaciones entre
      tablas (→ campos FK con `<select>` en el ABM), Caption persistente de campos
      (mejor etiqueta cuando el `.scx` no aporta una), stored procedures y vistas.
      Lee el trío `.dbc` + `.dct` (memo); el `.dcx` (índice) no aporta info nueva.
- [x] **Cobertura total de tipos VFP**: `.vcx`/`.vct` (métodos de clases →
      muestras de código), `.mnt` (memo del `.mnx` → ítems/PROCEDURE), `.h`
      (includes con `#DEFINE`) y `.txt` (notas). Tablas de sistema VFP
      (`foxuser`, `vfpgraph`, ...) filtradas para no generar ABMs inútiles.
      **Ver la tabla de referencia completa en `CLAUDE.md`** (qué se lee, qué no
      y por qué; regla de las "parejas" datos+memo+índice).
- [x] **Prueba end-to-end con ZIP real** (314 archivos, sistema Recetas): 13 ABM,
      21 244 registros, 143 imágenes, 23 índices, 2 reportes, menú con 10 ítems
      reales. App generada compila y arranca (`uvicorn`). Ver hallazgos abajo.
- [x] **Menú dinámico desde `programa.dbf` + `menues.dbf`**: patrón de sistemas
      VFP que no usan `.mpr`/`.mnx` estándar — las opciones de menú se guardan en
      tablas. `_parse_programa_menu()` lo soporta; fallback automático cuando los
      menús MPR/MNX tienen < 3 ítems.
- [x] **Filtro de código compilado en `.vcx`**: `OBJCODE` puede contener bytecode
      VFP (empieza con `0xFE`). Se descartan entradas cuyo primer byte sea < 32 o
      que tengan > 20 % de chars no-ASCII.
- [x] **Deduplicación de `.dbc`**: si el mismo `.dbc` aparece en varias carpetas
      (p.ej. `ZZ_EJECUTABLES/` y `datos/`), se prefiere el de la ruta con "dato".
- [x] **Deduplicación de `.dbf` en seed**: cuando hay dos copias del mismo DBF, se
      importa la **más grande** (más registros = datos de producción).
- [ ] Soportar otras tecnologías destino en el scaffold (hoy: FastAPI + SPA).
- [x] **Wireo robusto de ítems de menú → utilidad real** (`_menu_to_tabla` /
      `_menu_to_reporte` en `scaffold.py`): resuelve `DO FORM xxx` a su ABM
      tolerando prefijo numérico de orden (`0300_servic` → `servicios`), prefijos
      de verbo/abreviatura en etiqueta y form (`frmPedidos`, `ABM Clientes`,
      `Buscar cliente`, `Ver pedido` → su ABM) y usando el ControlSource del
      `.scx`. Los ítems `REPORT FORM xxx` van a la vista de reporte si existe el
      `.frx`; si no, caen al ABM de la tabla más cercana (listado + export). Antes
      muchos ítems quedaban como placeholder muerto ("Acción legacy: DO FORM…").
      Validado end-to-end con `test_recetas.zip`: 6/6 ítems wireados, backend
      arranca (GET / → 200), JS válido.

## 🔍 Hallazgos del sistema Recetas (ZIP real)

- **Menú dinámico en tablas**: `programa.dbf` (nombre, menu, tipo=FORM, nmenu) +
  `menues.dbf` (numero, menu=título del grupo). El `.mpr` (`GENERAL.MPR`) solo
  tenía 2 ítems placeholder del template de VFP.
- **Formularios apuntan a vistas, no a tablas**: `vistcomi.dbc` tiene 47 vistas
  (`vreccab1`, `vrecedet`, `vingredi`, …) y `login.dbc` tiene 6 vistas. Los SCX
  referencian esas vistas como `ControlSource`.
- **Sin FK persistentes**: el sistema no declara relaciones FK en el DBC; la
  integridad se maneja en las vistas y en código `.prg`.
- **Datos reales en `ZZ_EJECUTABLES/`**: los DBF de `datos/` eran minúsculas
  (4 rows); los de `ZZ_EJECUTABLES/` tenían los datos reales (hasta 10 408 rows).

## 🔄 Convención de trabajo

- Desarrollar en `claude/serene-hypatia-kvew2c`, **mergear a `main` vía PR**.
- Historial: PRs #1–#15 (modo gratuito Ollama, Codespaces, timeouts, multihilo,
  rendimiento, cobertura total/scaffold, enriquecimiento IA, baja de Demo,
  validaciones de esquema + reglas ejecutables, layout real de los .scx).
