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
- [x] **Revisión de pantallas `.scx` una por una**: nueva vista en `index.html`
      (`openScxReview()` / `renderScxReview()`) que muestra cada formulario
      parseado (`forms_detail`) de a uno — tabla asociada, etiquetas y orden de
      campos (con ▲▼) — para que el usuario corrija y dé el visto bueno antes de
      pasar al siguiente. El estado (`estado: 'pendiente'|'aprobado'`) se guarda
      dentro de cada formulario en `S.zipInfo`, que ya se persiste en
      `localStorage`; si se cierra el navegador y se vuelve, retoma en el primer
      formulario sin aprobar. Las correcciones (label/orden/tabla) son las mismas
      que ya usa `scaffold.py` (`_forms_index`) para generar el ABM, así que se
      reflejan directo en la app final. Sin cambios de backend: todo vive en
      `forms_detail`, que ya viaja completo en el payload de `/api/scaffold`.
- [x] **Caption de columnas de grid (`parse_scx_controls`)**: en el patrón de
      ABM por grilla (`g_clases.vcx`), el `Caption` de cada columna vive en el
      objeto `Header`, no en el `Textbox` que trae el `ControlSource` — son
      objetos hermanos (mismo `PARENT`). Antes las columnas de grid quedaban
      con la etiqueta igual al nombre del campo; ahora se asocian por
      `parent`. Verificado con `.scx`/`.sct` reales del sistema Recetas
      (`rececab`, `recedet`): "cod_rece" → "Código", "des_rece" → "Descripción", etc.
- [x] **Columnas de grid perdidas/mal atribuidas (`parse_scx_controls`)**: dos
      bugs encontrados con `.scx` reales (institu, convenio, tipomenu,
      instrubr). (1) Las regex de `ControlSource`/`Caption` no estaban
      ancladas a inicio de línea: `Column1.ControlSource` de un grid se
      confundía con la propiedad propia del objeto. (2) En un grid nativo (sin
      clase de columna custom) el `ControlSource` de cada columna vive **solo**
      en `ColumnN.ControlSource` del propio grid — el Textbox hijo no lo
      repite, así que esas columnas se perdían del todo (ej. `rzn_soc`, `iva`).
      Ahora se leen ambas fuentes y se deduplican por `(parent, campo)`.
- [x] **Caption de botones (`parse_vcx_captions` + `parse_scx_controls`)**: el
      texto de los botones de un ABM (Grabar/Cancelar/Editar/Agregar/
      Eliminar/Listar/Salida) casi nunca se repite por instancia en el `.scx`
      — vive en la clase base (`g_clases.vcx`) y solo se sobreescribe si
      cambió. Ahora `parse_scx_controls` lee el campo `CLASS` de cada
      `commandbutton` y `analyze_zip` resuelve el Caption contra el `.vcx` del
      sistema (nuevo `parse_vcx_captions`); si tampoco hay clase disponible,
      se descarta en vez de mostrar un badge vacío. Se muestra en la vista de
      revisión de pantallas.
- [x] **Caption de botones vía herencia de clases + filas basura de `.vcx`**:
      con `g_clases.vcx` real se comprobó que los botones de ABM
      (Grabar/Cancelar/Editar/Agregar/Eliminar/Listar/Salida) no definen su
      propio Caption — heredan de una clase ancestro (ej. "grabargb" hereda de
      "grabar", que hereda de "command" en `m_clases.vcx`). Nuevo
      `parse_vcx_class_defs` + `resolve_vcx_caption` suben la cadena hasta
      encontrar el Caption, cruzando `.vcx` distintos si hace falta. De paso:
      tanto `.vcx` como `.scx` (mismo formato base) tienen filas
      `COMMENT RESERVED` con punteros de memo reciclados de otro registro que
      pisaban entradas válidas — se filtran por `PLATFORM != WINDOWS`.
- [x] **Muestras de código de `.vcx` vacías (`parse_vcx_methods`)**: leía el
      campo `OBJCODE`, que es el bytecode YA COMPILADO del método (binario) —
      el filtro anti-binario lo descartaba siempre, así que nunca devolvía
      código real (0 muestras con cualquier sistema). El fuente PRG legible
      vive en el campo memo `METHODS`. Verificado con `_BASE.vcx`/`_UI.vcx`
      (clases estándar VFP) y las clases del sistema del usuario: de 0
      muestras pasa a extraer código real y legible.
- [ ] Soportar otras tecnologías destino en el scaffold (hoy: FastAPI + SPA).
- [ ] Wirear los ítems de menú a la pantalla exacta del formulario (hoy por nombre).

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
