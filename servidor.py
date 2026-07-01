#!/usr/bin/env python3
"""
LegacyMigrator - Servidor local

Actúa como proxy entre el navegador y la API de Anthropic, y además LEE el ZIP
del sistema legacy (forms, reportes, programas y la estructura real de las
tablas .dbf) para que el análisis sea preciso y no inventado.

Ejecutar:  python servidor.py
Abrir:     http://localhost:8080

No requiere dependencias externas: solo Python 3 estándar.
"""

import http.server
import io
import json
import os
import re
import struct
import sys
import urllib.error
import urllib.request
import zipfile

import scaffold  # generador determinístico de la app migrada (cobertura total)

API_KEY = ""  # Se configura desde el navegador
PORT = 8080

# Caché en memoria del último ZIP subido. Permite que /api/scaffold lea los
# datos e índices reales de los .dbf al generar la app, sin re-subir el archivo.
# Se identifica por un token (hash) que el navegador guarda y reenvía.
_ZIP_CACHE = {}        # token -> bytes del ZIP
_ZIP_CACHE_ORDER = []  # orden de inserción (para descartar los más viejos)
ZIP_CACHE_MAX = 3


def _cache_zip(raw):
    import hashlib
    token = hashlib.sha1(raw).hexdigest()[:16]
    if token not in _ZIP_CACHE:
        _ZIP_CACHE[token] = raw
        _ZIP_CACHE_ORDER.append(token)
        while len(_ZIP_CACHE_ORDER) > ZIP_CACHE_MAX:
            _ZIP_CACHE.pop(_ZIP_CACHE_ORDER.pop(0), None)
    return token

# Modo gratuito (sin API key): usa un modelo local vía Ollama (https://ollama.com).
# No requiere clave ni gasta tokens; corre 100% en la máquina del usuario.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# Por defecto un modelo de código LIVIANO: rápido en CPU (sin GPU). Para más
# calidad y si tenés recursos, usá OLLAMA_MODEL=qwen2.5-coder (7B) o mayor.
OLLAMA_DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:1.5b")
# Tope de tokens a generar en Ollama: evita que una llamada tarde "infinito" en CPU.
# 4000 es un equilibrio razonable para CPUs modestas (sin GPU).
OLLAMA_MAX_PREDICT = int(os.environ.get("OLLAMA_MAX_PREDICT", "4000"))
# Tiempo máximo de espera de una respuesta de Ollama (segundos).
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "900"))


def _ollama_timeout_msg(model):
    """Mensaje claro y accionable cuando una generación de Ollama no termina."""
    return (
        f"El modelo local '{model}' no terminó a tiempo (timeout de {OLLAMA_TIMEOUT} s). "
        f"El enriquecimiento por pantalla es chico pero en CPU puede tardar. "
        f"Probá: (1) reintentá; (2) generá con menos tokens iniciando el server "
        f"con OLLAMA_MAX_PREDICT=3000; (3) usá un modelo aún más chico: "
        f"ollama pull qwen2.5-coder:0.5b; (4) usá 📦 (sin IA), que es instantáneo."
    )

# --- Rendimiento del modo gratuito --------------------------------------------
# Ollama ya usa por defecto todos los núcleos físicos y, si hay GPU, tantas capas
# como entren en la VRAM. Estas variables permiten exprimir aún más los recursos.
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))
# Mantiene el modelo cargado en memoria entre peticiones (evita recargarlo cada
# vez, que es lo más lento). "-1" = para siempre mientras el server viva.
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")


def _int_env(name):
    """Lee una variable de entorno como int, o None si no está/!es válida."""
    val = os.environ.get(name)
    try:
        return int(val) if val not in (None, "") else None
    except ValueError:
        return None


# Overrides opcionales (None = dejar que Ollama elija el óptimo):
OLLAMA_NUM_GPU = _int_env("OLLAMA_NUM_GPU")        # 999 = forzar TODAS las capas a la GPU
OLLAMA_NUM_THREAD = _int_env("OLLAMA_NUM_THREAD")  # nº de hilos de CPU (def: núcleos físicos)
OLLAMA_NUM_BATCH = _int_env("OLLAMA_NUM_BATCH")    # p. ej. 1024: procesa el prompt más rápido

# Límites para no saturar el prompt (ni la memoria) con sistemas enormes.
MAX_TABLES = 60          # cuántas tablas .dbf describir con su estructura
MAX_FIELDS_PER_TABLE = 60
MAX_NAME_LIST = 80       # cuántos nombres de forms/reportes listar
MAX_SAMPLES = 40         # cuántos .prg pequeños incluir como muestra de código
MAX_SAMPLE_BYTES = 6000  # tamaño máximo de cada muestra de código
MAX_FORMS_PARSED = 50    # cuántos formularios .scx analizar (controles)

# Extensiones típicas de sistemas legacy y su categoría.
EXT_CATEGORY = {
    ".scx": "forms", ".sct": "forms",
    ".frx": "reports", ".frt": "reports",
    ".dbf": "tables",
    ".dbc": "databases", ".dct": "databases",
    ".prg": "programs",
    ".vcx": "classes", ".vct": "classes",
    ".mnx": "menus", ".mpr": "menus",
    ".pjx": "project", ".pjt": "project",
    ".cbl": "programs", ".cob": "programs", ".cpy": "copybooks",
    ".h": "includes",   # archivos de include VFP (#DEFINE, constantes)
    ".txt": "docs",     # documentación / notas
}

# Tablas de sistema de VFP/FoxPro que NO son datos de negocio.
# Se excluyen del análisis para no generar ABMs inútiles.
VFP_SYSTEM_TABLES = {
    "foxuser",    # preferencias del IDE/runtime
    "vfpgraph",   # soporte de gráficos del IDE
    "foxcode",    # catálogo de código IntelliSense
    "foxtask",    # tareas del proyecto
    "foxref",     # referencias de proyectos
}

# Códigos de TYPE en el proyecto .pjx -> tipo legible.
PJX_TYPE = {
    "H": "Programa principal", "P": "Programa", "S": "Formulario",
    "R": "Reporte", "L": "Etiqueta", "M": "Menú", "V": "Clase",
    "d": "Base de datos", "D": "Base de datos", "Q": "Consulta",
    "T": "Tabla", "B": "Biblioteca (API)", "K": "Texto", "Z": "Otro",
    "I": "Imagen", "X": "Otro", "m": "Menú",
}

# Tipos de campo DBF -> nombre legible (para enriquecer el prompt).
DBF_TYPES = {
    "C": "Character", "N": "Numeric", "F": "Float", "D": "Date",
    "T": "DateTime", "L": "Logical", "M": "Memo", "G": "General",
    "B": "Double", "I": "Integer", "Y": "Currency", "P": "Picture",
}


def parse_dbf_structure(head_bytes):
    """Extrae la estructura (campos) del header de un archivo .dbf.

    No depende de librerías externas: lee el header binario según el formato
    DBF estándar. Devuelve dict con num_records y fields, o None si falla.
    """
    if len(head_bytes) < 32:
        return None
    try:
        num_records = int.from_bytes(head_bytes[4:8], "little")
        header_len = int.from_bytes(head_bytes[8:10], "little")
    except Exception:
        return None

    region = head_bytes[32:]  # los descriptores de campo empiezan en el byte 32
    fields = []
    i = 0
    while i + 32 <= len(region):
        if region[i] == 0x0D:  # terminador de la lista de campos
            break
        raw_name = region[i:i + 11].split(b"\x00")[0]
        name = raw_name.decode("latin-1", "replace").strip()
        if not name:
            break
        ftype = chr(region[i + 11]) if region[i + 11] else "?"
        flen = region[i + 16]
        fields.append({
            "name": name,
            "type": ftype,
            "type_name": DBF_TYPES.get(ftype, ftype),
            "len": flen,
        })
        if len(fields) >= MAX_FIELDS_PER_TABLE:
            break
        i += 32

    if not fields:
        return None
    return {"records": num_records, "fields": fields}


def _dbf_layout(head_bytes):
    """Como parse_dbf_structure pero además devuelve header_len, record_len y el
    offset de cada campo dentro del registro (para leer los datos reales)."""
    if len(head_bytes) < 32:
        return None
    try:
        num_records = int.from_bytes(head_bytes[4:8], "little")
        header_len = int.from_bytes(head_bytes[8:10], "little")
        record_len = int.from_bytes(head_bytes[10:12], "little")
    except Exception:
        return None
    limit = header_len if 32 < header_len <= len(head_bytes) else len(head_bytes)
    region = head_bytes[32:limit]
    fields = []
    offset = 1  # byte 0 del registro = marca de borrado
    i = 0
    while i + 32 <= len(region):
        if region[i] == 0x0D:
            break
        name = region[i:i + 11].split(b"\x00")[0].decode("latin-1", "replace").strip()
        if not name:
            break
        ftype = chr(region[i + 11]) if region[i + 11] else "C"
        flen = region[i + 16]
        dec = region[i + 17]
        fields.append({"name": name, "type": ftype, "len": flen, "dec": dec, "offset": offset})
        offset += flen
        if len(fields) >= MAX_FIELDS_PER_TABLE:
            break
        i += 32
    if not fields:
        return None
    return {"header_len": header_len, "record_len": record_len,
            "num_records": num_records, "fields": fields}


def _decode_dbf_value(raw, ftype, dec, fpt, blocksize):
    """Convierte los bytes de un campo .dbf al tipo Python adecuado para SQLite."""
    t = (ftype or "C").upper()
    if t in ("C", "P", "V"):
        return raw.decode("latin-1", "replace").replace("\x00", " ").rstrip()
    if t in ("M", "G"):
        if len(raw) >= 4 and not raw[:4].strip(b"\x00 ").isdigit():
            blk = int.from_bytes(raw[:4], "little")          # VFP: puntero binario LE
        else:
            s = raw.decode("latin-1", "replace").strip()     # dBASE: nº de bloque ASCII
            blk = int(s) if s.isdigit() else 0
        return _read_memo(fpt, blk, blocksize) if blk > 0 else ""
    if t in ("N", "F"):
        s = raw.decode("latin-1", "replace").strip()
        if not s or s in (".", "-", "*"):
            return None
        try:
            return int(s) if (dec == 0 and "." not in s) else float(s)
        except ValueError:
            return None
    if t == "I":
        return int.from_bytes(raw[:4], "little", signed=True) if len(raw) >= 4 else None
    if t == "B":
        try:
            return struct.unpack("<d", raw[:8])[0]
        except struct.error:
            return None
    if t == "Y":
        try:
            return struct.unpack("<q", raw[:8])[0] / 10000.0
        except struct.error:
            return None
    if t == "L":
        ch = chr(raw[0]) if raw else " "
        if ch in "TtYy":
            return 1
        if ch in "FfNn":
            return 0
        return None
    if t == "D":
        s = raw.decode("latin-1", "replace").strip()
        return "%s-%s-%s" % (s[0:4], s[4:6], s[6:8]) if (len(s) == 8 and s.isdigit()) else None
    if t == "T":
        if len(raw) >= 8:
            jdate = int.from_bytes(raw[0:4], "little")
            ms = int.from_bytes(raw[4:8], "little")
            if jdate <= 0:
                return None
            try:
                import datetime
                d = datetime.date.fromordinal(jdate - 1721425)
                secs = ms // 1000
                return "%sT%02d:%02d:%02d" % (d.isoformat(), secs // 3600, (secs % 3600) // 60, secs % 60)
            except (ValueError, OverflowError):
                return None
        return None
    return raw.decode("latin-1", "replace").replace("\x00", " ").rstrip()


def read_dbf_records(dbf_bytes, fpt_bytes, max_records=50000):
    """Lee los registros reales de un .dbf (datos, no solo el header). Devuelve
    una lista de dicts {slug_campo: valor}, saltando los registros borrados."""
    layout = _dbf_layout(dbf_bytes[: 32 + 32 * 256])
    if not layout or layout["record_len"] <= 0:
        return []
    rec_len = layout["record_len"]
    fields = layout["fields"]
    blocksize = int.from_bytes(fpt_bytes[6:8], "big") if (fpt_bytes and len(fpt_bytes) >= 8) else 64
    if blocksize <= 0:
        blocksize = 64
    rows = []
    pos = layout["header_len"]
    total = len(dbf_bytes)
    limit = min(layout["num_records"], max_records) if layout["num_records"] > 0 else max_records
    count = 0
    while pos + rec_len <= total and count < limit:
        rec = dbf_bytes[pos:pos + rec_len]
        pos += rec_len
        count += 1
        if rec[:1] == b"*":  # registro marcado como borrado
            continue
        row = {}
        for f in fields:
            raw = rec[f["offset"]:f["offset"] + f["len"]]
            try:
                row[scaffold._slug(f["name"])] = _decode_dbf_value(
                    raw, f["type"], f["dec"], fpt_bytes, blocksize)
            except Exception:
                row[scaffold._slug(f["name"])] = None
        rows.append(row)
    return rows


# Funciones típicas de VFP que envuelven una expresión de índice (señal fuerte
# de que un texto ASCII es una expresión de clave y no datos del árbol B).
_VFP_IDX_FUNCS = re.compile(
    r"\b(UPPER|LOWER|STR|VAL|DTOS|DTOC|DTOT|TTOC|CTOD|ALLTRIM|TRIM|LTRIM|RTRIM|"
    r"PADL|PADR|PADC|LEFT|RIGHT|SUBSTR|STUFF|TRANSFORM|RECNO|DELETED|IIF|"
    r"BINTOC|ASC|CHR|MONTH|YEAR|DAY)\b", re.I)
# Caracteres que pueden aparecer en una expresión de índice VFP.
_EXPR_RUN = re.compile(r"[A-Za-z0-9_.()+\-*/, '\"<>=!$:]{2,}")


def parse_cdx_expressions(idx_bytes, field_slugs):
    """Parser de los índices .cdx/.idx de FoxPro.

    El valor accionable de un índice para SQLite son los CAMPOS de su expresión
    de clave. Esas expresiones se guardan como texto ASCII en las cabeceras de
    cada tag (p. ej. "CODIGO" o "STR(GRUPO)+STR(MENU)"); los datos del árbol B
    (las claves concretas) están comprimidos y no nos interesan para recrear el
    índice. Por eso localizamos las EXPRESIONES y de cada una extraemos, en
    orden, los campos reales involucrados → un índice (compuesto si aplica).

    Devuelve una lista de índices; cada uno es una lista ordenada de slugs de
    campo. Heurística defensiva: solo acepta runs que (a) sean exactamente un
    campo real, (b) contengan una función de índice VFP, o (c) concatenen
    campos con '+'. Así evita falsos positivos de los nodos de datos."""
    if not idx_bytes:
        return []
    text = idx_bytes.decode("latin-1", "replace")
    fset = set(field_slugs)
    defs, seen = [], set()
    for run in _EXPR_RUN.findall(text):
        run = run.strip()
        idents = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", run)
        cols = []
        for ident in idents:
            s = scaffold._slug(ident)
            if s in fset and s not in cols:
                cols.append(s)
        if not cols:
            continue
        single_field = (len(idents) == 1 and cols and len(run) <= 32)
        has_func = bool(_VFP_IDX_FUNCS.search(run))
        compound = ("+" in run and len(cols) >= 1)
        if not (single_field or has_func or compound):
            continue
        sig = tuple(cols)
        if sig in seen:
            continue
        seen.add(sig)
        defs.append(cols)
    return defs[:16]


def build_seed_from_zip(raw_bytes, inventory):
    """Extrae datos e índices reales del ZIP para sembrar la app generada.
    Devuelve (seed, indexes, total_filas) con claves = slug del nombre de tabla."""
    zf = zipfile.ZipFile(io.BytesIO(raw_bytes))
    names = [n for n in zf.namelist() if not n.endswith("/")]
    # by_stem: nombre base sin extensión (lower) -> {ext: ruta real}
    # Cuando hay duplicados (p.ej. datos/ vs ZZ_EJECUTABLES/) preferimos el
    # archivo más GRANDE porque suele tener los datos reales de producción.
    by_stem = {}
    file_sizes = {n: zf.getinfo(n).file_size for n in names}
    for n in names:
        base = os.path.basename(n)
        stem, ext = os.path.splitext(base)
        sk, ek = stem.lower(), ext.lower()
        prev = by_stem.get(sk, {}).get(ek)
        if prev is None or file_sizes.get(n, 0) > file_sizes.get(prev, 0):
            by_stem.setdefault(sk, {})[ek] = n

    seed, indexes, total = {}, {}, 0
    for t in inventory.get("tables", []):
        name = t.get("name") or ""
        key = scaffold._slug(name)
        entry = by_stem.get(name.lower())
        if not key or not entry or ".dbf" not in entry:
            continue
        try:
            dbf_bytes = zf.read(entry[".dbf"])
            fpt_bytes = zf.read(entry[".fpt"]) if ".fpt" in entry else b""
            rows = read_dbf_records(dbf_bytes, fpt_bytes)
        except Exception:
            rows = []
        if rows:
            seed[key] = rows
            total += len(rows)
        field_slugs = [scaffold._slug(f.get("name")) for f in (t.get("fields") or [])]
        defs, seen = [], set()
        for ext in (".cdx", ".idx"):
            if ext in entry:
                try:
                    for cols in parse_cdx_expressions(zf.read(entry[ext]), field_slugs):
                        sig = tuple(cols)
                        if cols and sig not in seen:
                            seen.add(sig)
                            defs.append(cols)
                except Exception:
                    pass
        if defs:
            indexes[key] = defs[:16]   # lista de índices; cada uno = lista de columnas
    return seed, indexes, total


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tif", ".tiff"}
ASSET_MAX_FILE = 8 * 1024 * 1024     # tamaño máx por imagen
ASSET_MAX_TOTAL = 80 * 1024 * 1024   # tamaño máx total de imágenes a incluir


def extract_assets_from_zip(raw_bytes):
    """Extrae las imágenes del ZIP para incluirlas en la app generada.
    Devuelve {nombre_base_lower: bytes}. Acota tamaño por archivo y total."""
    zf = zipfile.ZipFile(io.BytesIO(raw_bytes))
    assets, total = {}, 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        base = os.path.basename(info.filename)
        ext = os.path.splitext(base)[1].lower()
        if ext not in IMAGE_EXTS or info.file_size > ASSET_MAX_FILE:
            continue
        key = base.lower()
        if key in assets:
            continue
        if total + info.file_size > ASSET_MAX_TOTAL:
            break
        try:
            data = zf.read(info.filename)
        except Exception:
            continue
        assets[key] = data
        total += len(data)
    return assets


def parse_pjx(pjx_bytes, pjt_bytes):
    """Lee el proyecto VFP (.pjx + memo .pjt). Es una tabla DBF: cada registro
    describe un archivo del sistema. Devuelve el manifiesto: lista de archivos
    (con tipo, si está excluido y si es el principal) y el programa de arranque.

    Es el archivo MÁS importante para el armado: define qué compone el sistema
    y cuál es su punto de entrada. Reusa el lector de .dbf (read_dbf_records)."""
    rows = read_dbf_records(pjx_bytes, pjt_bytes, max_records=5000)
    archivos, principal, por_tipo = [], "", {}
    for r in rows:
        name = str(r.get("name") or "").strip().replace("\x00", "")
        if not name:
            continue
        typ = (str(r.get("type") or "").strip() or "?")[:1]
        es_main = bool(r.get("mainprog") or r.get("main"))
        item = {
            "name": name.replace("\\", "/").split("/")[-1],
            "ruta": name,
            "type": typ,
            "type_name": PJX_TYPE.get(typ, typ),
            "excluido": bool(r.get("exclude")),
            "principal": es_main,
        }
        archivos.append(item)
        por_tipo[item["type_name"]] = por_tipo.get(item["type_name"], 0) + 1
        if es_main and not principal:
            principal = item["name"]
        if not principal and typ == "H":
            principal = item["name"]
    return {"archivos": archivos, "principal": principal, "por_tipo": por_tipo}


def _dbc_get_prop(text, key):
    """Extrae el valor de una propiedad del bloque PROPERTY de un objeto .dbc."""
    m = re.search(r'(?:^|\n)\s*' + re.escape(key) + r'\s*=\s*"?([^"\r\n]*)"?', text, re.I)
    return m.group(1).strip().strip('"') if m else ""


def parse_dbc(dbc_bytes, dct_bytes):
    """Lee el Database Container de VFP (.dbc + memo .dct).

    Un .dbc es un DBF especial: cada registro describe un objeto de la base
    de datos (tabla, campo, relación, vista, stored procedure, etc.).
    Se identifica el tipo por el campo OBJECTTYPE y la jerarquía por PARENTID.

    Devuelve:
    - relaciones: [{parent_table, parent_field, child_table, child_field}]
    - campos:     {tabla_slug: {campo_slug: {caption, defaultvalue, inputmask, ...}}}
    - stored_procs: [{name, code}]
    - vistas: [name]
    """
    rows = read_dbf_records(dbc_bytes, dct_bytes, max_records=10000)
    if not rows:
        return {"relaciones": [], "campos": {}, "stored_procs": [], "vistas": []}

    # Índice OBJECTID → fila para resolver parentid (jerarquía tabla→campo/relación).
    by_id = {}
    for r in rows:
        oid = r.get("objectid")
        if oid is not None:
            try:
                by_id[int(oid)] = r
            except (TypeError, ValueError):
                pass

    relaciones, campos_props, stored_procs, vistas = [], {}, [], []
    seen_rels = set()

    for r in rows:
        otype = str(r.get("objecttype") or "").strip().lower()
        oname = str(r.get("objectname") or "").strip()
        prop  = str(r.get("property") or "").strip()
        code  = str(r.get("code") or "").strip()
        try:
            parent_id = int(r.get("parentid") or 0)
        except (TypeError, ValueError):
            parent_id = 0

        if otype == "relation":
            # Relación 1-N entre tablas (la tabla padre es el padre del registro).
            pt_row = by_id.get(parent_id) if parent_id else None
            parent_table = str(pt_row.get("objectname") or "") if pt_row else ""
            parent_tag  = _dbc_get_prop(prop, "ParentTagName")
            child_table = _dbc_get_prop(prop, "ChildTableName")
            child_tag   = _dbc_get_prop(prop, "ChildTagName")
            pt = scaffold._slug(parent_table)
            ct = scaffold._slug(child_table)
            pf = scaffold._slug(parent_tag)
            cf = scaffold._slug(child_tag)
            sig = (pt, pf, ct, cf)
            if ct and cf and sig not in seen_rels:
                seen_rels.add(sig)
                relaciones.append({
                    "parent_table": pt, "parent_field": pf,
                    "child_table":  ct, "child_field":  cf,
                })

        elif otype == "field":
            # Propiedades persistentes de un campo (Caption, Default, etc.).
            pt_row = by_id.get(parent_id) if parent_id else None
            if pt_row and str(pt_row.get("objecttype") or "").strip().lower() == "table":
                tabla_key = scaffold._slug(str(pt_row.get("objectname") or "").strip())
                campo_key = scaffold._slug(oname)
                if tabla_key and campo_key:
                    props = {}
                    for k in ("Caption", "DefaultValue", "InputMask",
                              "RuleExpression", "RuleText"):
                        v = _dbc_get_prop(prop, k)
                        if v:
                            props[k.lower()] = v
                    if props:
                        campos_props.setdefault(tabla_key, {})[campo_key] = props

        elif otype in ("storedprocedurecode", "storedprocedure"):
            if oname and code:
                stored_procs.append({"name": oname, "code": code[:3000]})

        elif otype in ("view", "localview", "remoteview"):
            if oname:
                vistas.append(oname)

    return {
        "relaciones": relaciones,
        "campos": campos_props,
        "stored_procs": stored_procs[:20],
        "vistas": vistas[:50],
    }


def _read_memo(sct, blocknum, blocksize):
    """Lee un campo memo de un archivo .sct/.fpt dado su número de bloque."""
    if not sct or blocknum <= 0:
        return ""
    loc = blocknum * blocksize
    if loc + 8 > len(sct):
        return ""
    length = int.from_bytes(sct[loc + 4:loc + 8], "big")  # longitud (big-endian)
    if length <= 0 or length > 1_000_000:
        return ""
    return sct[loc + 8:loc + 8 + length].decode("latin-1", "replace").replace("\x00", "").strip()


def parse_scx_controls(scx, sct):
    """Extrae los controles de un formulario VFP .scx (es una tabla DBF: cada
    registro es un objeto). En VFP, baseclass y objname son campos MEMO: el .scx
    guarda un puntero de 4 bytes y el texto vive en el .sct. Por eso necesitamos
    ambos archivos. Sin dependencias externas."""
    if len(scx) < 32:
        return None
    try:
        num_records = int.from_bytes(scx[4:8], "little")
        header_len = int.from_bytes(scx[8:10], "little")
        rec_len = int.from_bytes(scx[10:12], "little")
    except Exception:
        return None
    if rec_len <= 0 or header_len <= 32:
        return None
    blocksize = int.from_bytes(sct[6:8], "big") if (sct and len(sct) >= 8) else 64
    if blocksize <= 0:
        blocksize = 64

    # Descriptores de campo -> nombre: (offset, longitud, tipo).
    region = scx[32:header_len]
    fmap = {}
    offset = 1  # primer byte del registro es la marca de borrado
    i = 0
    while i + 32 <= len(region):
        if region[i] == 0x0D:
            break
        fname = region[i:i + 11].split(b"\x00")[0].decode("latin-1", "replace").strip().lower()
        ftype = chr(region[i + 11]) if region[i + 11] else "?"
        flen = region[i + 16]
        if fname:
            fmap[fname] = (offset, flen, ftype)
        offset += flen
        i += 32

    if "baseclass" not in fmap or "objname" not in fmap:
        return None
    bo, bl, bt = fmap["baseclass"]
    no, nl, nt = fmap["objname"]
    has_props = "properties" in fmap
    if has_props:
        po, pl, pt = fmap["properties"]
    has_parent = "parent" in fmap
    if has_parent:
        pao, pal, pat = fmap["parent"]
    has_class = "class" in fmap
    if has_class:
        clo, cll, clt = fmap["class"]

    def field_text(rec, off, ln, typ):
        raw = rec[off:off + ln]
        if typ == "M":  # memo: el valor es el nº de bloque (4 bytes LE) en el .sct
            return _read_memo(sct, int.from_bytes(raw[:4], "little"), blocksize)
        return raw.decode("latin-1", "replace").replace("\x00", "").strip()

    def parse_props(text):
        """De un memo 'properties' saca ControlSource, Caption y Top del objeto
        (ancladas a inicio de línea: si no, "Column1.ControlSource" de un grid
        se confundiría con la propiedad propia del objeto)."""
        cs = re.search(r'(?:^|\n)\s*ControlSource\s*=\s*([^\r\n]+)', text, re.I)
        cap = re.search(r'(?:^|\n)\s*Caption\s*=\s*([^\r\n]+)', text, re.I)
        top = re.search(r'(?:^|\n)\s*Top\s*=\s*([\d.]+)', text, re.I)
        src = cs.group(1).strip().strip('"').strip() if cs else ""
        label = cap.group(1).strip().strip('"').strip() if cap else ""
        try:
            tval = float(top.group(1)) if top else 0.0
        except ValueError:
            tval = 0.0
        return src, label, tval

    def parse_grid_columns(text):
        """De las propiedades propias de un grid saca "ColumnN.ControlSource".
        En un grid nativo (sin clase de columna custom) es la ÚNICA copia del
        ControlSource: el Textbox hijo de la columna no la repite."""
        out = []
        for cn, csrc in re.findall(r'Column(\d+)\.ControlSource\s*=\s*"?([^"\r\n]+)"?', text, re.I):
            out.append((int(cn), csrc.strip().strip('"').strip()))
        return out

    counts = {}
    controls = []
    botones = []         # Caption de los commandbutton (Grabar, Cancelar, Salida...)
    raw_fields = []      # (pref, fld, label, top, parent) de cada campo real detectado
    header_caption = {}  # parent -> Caption, de objetos "header" (columnas de grid)
    for r in range(num_records):
        start = header_len + r * rec_len
        rec = scx[start:start + rec_len]
        if len(rec) < rec_len:
            break
        baseclass = field_text(rec, bo, bl, bt).lower()
        objname = field_text(rec, no, nl, nt)
        if not baseclass:
            continue
        counts[baseclass] = counts.get(baseclass, 0) + 1
        if len(controls) < 40:
            controls.append({"name": objname, "type": baseclass})
        # Layout real: ControlSource (atadura a campo) y Caption (etiqueta).
        if has_props:
            props_text = field_text(rec, po, pl, pt)
            try:
                src, label, top = parse_props(props_text)
            except Exception:
                src, label, top = "", "", 0.0
            parent = field_text(rec, pao, pal, pat) if has_parent else ""
            # En columnas de grid, el Caption vive en el "header" (objeto hermano
            # del textbox que trae el ControlSource): lo guardamos por parent para
            # asociarlo después. Sin esto, las columnas de grid quedan sin etiqueta.
            if baseclass == "header" and label:
                header_caption[parent] = label
            if baseclass == "commandbutton" and len(botones) < 30:
                # El Caption suele venir heredado de la clase (no se repite por
                # instancia si no cambió): si no está acá, se resuelve después
                # contra el .vcx con el nombre de clase (campo "class").
                clase = field_text(rec, clo, cll, clt).lower() if has_class else ""
                botones.append({"name": objname, "clase": clase,
                                 "caption": _clean_prompt(label) if label else ""})
            if "." in src:
                pref, _, fld = src.partition(".")
                pref, fld = pref.strip().lower(), fld.strip()
                if pref and fld and pref not in ("thisform", "this", "_screen"):
                    raw_fields.append((pref, fld, label, top, parent))
            # En grids, cada columna guarda su ControlSource como propiedad
            # propia del grid ("ColumnN.ControlSource"); a veces es la única
            # copia (el Textbox hijo no la repite), así que la leemos aparte.
            if baseclass == "grid":
                grid_path = f"{parent}.{objname}" if parent else objname
                for cn, csrc in parse_grid_columns(props_text):
                    if "." not in csrc:
                        continue
                    pref, _, fld = csrc.partition(".")
                    pref, fld = pref.strip().lower(), fld.strip()
                    if pref and fld:
                        raw_fields.append((pref, fld, "", float(cn), f"{grid_path}.Column{cn}"))
    if not counts:
        return None
    tabla_freq = {}
    campos_by_key = {}   # (parent, fld) -> campo, para no duplicar entre Textbox y grid
    for pref, fld, label, top, parent in raw_fields:
        tabla_freq[pref] = tabla_freq.get(pref, 0) + 1
        resolved = label or header_caption.get(parent) or fld
        key = (parent, fld)
        prev = campos_by_key.get(key)
        if prev is None or (prev["label"] == fld and resolved != fld):
            campos_by_key[key] = {"field": fld, "label": resolved, "top": top}
    campos = list(campos_by_key.values())[:MAX_FIELDS_PER_TABLE]
    tabla = max(tabla_freq, key=tabla_freq.get) if tabla_freq else ""
    return {"counts": counts, "controls": controls, "total": sum(counts.values()),
            "campos": campos, "tabla": tabla, "botones": botones}


def _clean_prompt(s):
    """Limpia un PROMPT de menú VFP: saca el marcador de atajo \\< y espacios."""
    return s.replace("\\<", "").replace("\\", "").strip()


def parse_mpr_menu(text):
    """Extrae la estructura de menú de un .mpr (código generado por VFP).

    Devuelve [{"titulo": <pad>, "items": [{"texto":..,"accion":..}, ...]}].
    Reconstruye los PAD (menú superior), su POPUP asociado y los BAR (ítems).
    """
    import re
    pads = re.findall(r'DEFINE\s+PAD\s+(\w+)\s+OF\s+\w+\s+PROMPT\s+"([^"]*)"', text, re.I)
    pad_prompt = {name: _clean_prompt(p) for name, p in pads}
    # PAD -> POPUP
    pad_popup = {}
    for pad, popup in re.findall(r'ON\s+PAD\s+(\w+)\s+OF\s+\w+\s+ACTIVATE\s+POPUP\s+(\w+)', text, re.I):
        pad_popup[pad] = popup
    # BARs por popup: DEFINE BAR <num> OF <popup> PROMPT "<texto>"
    bars = {}
    for num, popup, prompt in re.findall(r'DEFINE\s+BAR\s+(\d+)\s+OF\s+(\w+)\s+PROMPT\s+"([^"]*)"', text, re.I):
        bars.setdefault(popup, []).append((int(num), _clean_prompt(prompt)))
    # acción de cada BAR (DO form / DO prog)
    bar_action = {}
    for num, popup, action in re.findall(r'ON\s+SELECTION\s+BAR\s+(\d+)\s+OF\s+(\w+)\s+(.+)', text, re.I):
        bar_action[(popup, int(num))] = action.strip().splitlines()[0][:120]

    menus = []
    used_popups = set()
    for pad, _ in pads:
        popup = pad_popup.get(pad)
        items = []
        for num, prompt in sorted(bars.get(popup, [])):
            if prompt:
                items.append({"texto": prompt, "accion": bar_action.get((popup, num), "")})
        used_popups.add(popup)
        menus.append({"titulo": pad_prompt.get(pad, pad), "items": items})
    # popups sueltos (submenús no colgados de un PAD)
    for popup, items in bars.items():
        if popup in used_popups:
            continue
        its = [{"texto": p, "accion": bar_action.get((popup, n), "")} for n, p in sorted(items) if p]
        if its:
            menus.append({"titulo": popup, "items": its})
    return menus


def parse_mnx_menu(mnx_bytes, mnt_bytes=b""):
    """Fallback: extrae los ítems de un .mnx (tabla DBF) como lista plana.

    Acepta el memo .mnt para poder leer campos Memo (PROCEDURE, MESSAGE, etc.).
    Usa read_dbf_records para soportar ambos tipos de campo correctamente.
    """
    rows = read_dbf_records(mnx_bytes, mnt_bytes, max_records=500)
    items = []
    for r in rows:
        txt = _clean_prompt(str(r.get("prompt") or "").replace("\x00", ""))
        if not txt:
            continue
        accion = str(r.get("procedure") or r.get("action") or
                     r.get("command") or "")[:120].strip()
        items.append({"texto": txt, "accion": accion})
    return [{"titulo": "Menú", "items": items}] if items else []


def parse_vcx_methods(vcx_bytes, vct_bytes):
    """Extrae los métodos (código) de una biblioteca de clases VFP (.vcx + .vct).

    Un .vcx es un DBF donde cada registro es un miembro de clase. El campo
    OBJCODE (memo, en el .vct) contiene el código PRG del método.

    Devuelve lista de {name, class_name, code} con los métodos no vacíos.
    """
    rows = read_dbf_records(vcx_bytes, vct_bytes, max_records=2000)
    methods = []
    for r in rows:
        code = str(r.get("objcode") or "").strip()
        if not code or len(code) < 15:
            continue
        # El campo OBJCODE puede contener código compilado (binario). Si el primer
        # carácter es no-imprimible el bloque entero es código objeto → descartarlo.
        if ord(code[0]) < 32:
            continue
        # Verificación adicional: descartar si más del 20 % son caracteres no-ASCII.
        non_ascii = sum(1 for c in code[:200] if ord(c) > 127 or ord(c) < 9)
        if non_ascii > len(code[:200]) * 0.20:
            continue
        objname  = str(r.get("objname")  or "").strip()
        baseclass = str(r.get("baseclass") or "").strip()
        methods.append({
            "name":       objname,
            "class_name": baseclass,
            "code":       code[:MAX_SAMPLE_BYTES],
        })
    return methods


def parse_vcx_captions(vcx_bytes, vct_bytes):
    """Mapea nombre de clase (el que aparece en el campo CLASS de un .scx,
    p.ej. "grabargb") -> su Caption por defecto, definido en la clase del .vcx.

    En VFP, si una instancia no sobreescribe el Caption, el texto real vive
    solo en la clase base (acá) y no en el .scx que la usa — sin esto, los
    botones de un formulario ABM (Grabar/Cancelar/Editar/...) quedan sin
    etiqueta aunque el usuario sí los vea así en pantalla."""
    rows = read_dbf_records(vcx_bytes, vct_bytes, max_records=2000)
    out = {}
    for r in rows:
        name = str(r.get("objname") or "").strip().lower()
        props = str(r.get("properties") or "")
        if not name or not props:
            continue
        m = re.search(r'(?:^|\n)\s*Caption\s*=\s*([^\r\n]+)', props, re.I)
        if not m:
            continue
        cap = m.group(1).strip().strip('"').strip()
        if cap:
            out.setdefault(name, _clean_prompt(cap))
    return out


def _parse_programa_menu(prog_bytes, menues_bytes=b""):
    """Construye navegación desde programa.dbf + menues.dbf (patrón de menú
    dinámico común en sistemas VFP que no usan .mpr/.mnx estándar).

    programa.dbf tiene: nombre (form), menu (label), tipo (FORM/MENU),
    nmenu (nro de menú), smenu (nro de submenú).
    menues.dbf tiene: numero, menu (nombre del grupo de menú).
    """
    prog_rows = read_dbf_records(prog_bytes, b"", max_records=500)
    if not prog_rows:
        return []

    # menues.dbf: {numero -> nombre del grupo}
    menu_names = {}
    if menues_bytes:
        for r in read_dbf_records(menues_bytes, b"", max_records=50):
            num = r.get("numero")
            name = str(r.get("menu") or "").strip()
            if num is not None and name:
                try:
                    menu_names[int(num)] = name
                except (TypeError, ValueError):
                    pass

    # Agrupar FORMs por nmenu
    by_menu = {}
    for r in prog_rows:
        if str(r.get("tipo") or "").strip().upper() != "FORM":
            continue
        nombre = str(r.get("nombre") or "").strip()
        label  = str(r.get("menu")   or "").strip()
        nmenu  = r.get("nmenu")
        if not nombre or not label or nmenu is None:
            continue
        try:
            nmenu = int(nmenu)
        except (TypeError, ValueError):
            continue
        titulo = menu_names.get(nmenu, f"Menú {nmenu}")
        if nmenu not in by_menu:
            by_menu[nmenu] = {"titulo": titulo, "items": []}
        by_menu[nmenu]["items"].append({
            "texto":  label,
            "accion": f"DO FORM {nombre}",
        })

    return [by_menu[k] for k in sorted(by_menu) if by_menu[k]["items"]]


def analyze_zip(raw_bytes):
    """Lee el ZIP en memoria y devuelve un resumen real del sistema legacy."""
    zf = zipfile.ZipFile(io.BytesIO(raw_bytes))
    names = [n for n in zf.namelist() if not n.endswith("/")]
    lower_map = {n.lower(): n for n in names}  # para ubicar el .sct de cada .scx

    # 1) PRIMERO el proyecto .pjx: es el manifiesto del sistema (qué archivos lo
    # componen y cuál es el programa principal). Orienta todo el armado.
    project = None
    pjx_name = next((n for n in names if n.lower().endswith(".pjx")), None)
    if pjx_name:
        try:
            pjt_real = lower_map.get(os.path.splitext(pjx_name)[0].lower() + ".pjt")
            pjt_bytes = zf.read(pjt_real) if pjt_real else b""
            project = parse_pjx(zf.read(pjx_name), pjt_bytes)
        except Exception:
            project = None

    # 2) Bases de datos .dbc: relaciones entre tablas, propiedades de campos,
    #    stored procedures y vistas. Muy útil para FK constraints y etiquetas.
    # Deduplicamos por nombre base: si hay duplicados (p.ej. ZZ_EJECUTABLES/ vs
    # datos/) preferimos la copia en la carpeta más "profunda" (datos de producción).
    databases = []
    dbc_by_stem = {}   # stem_lower -> nombre real elegido
    for dbc_name in sorted(n for n in names if n.lower().endswith(".dbc")):
        stem_key = os.path.splitext(os.path.basename(dbc_name))[0].lower()
        prev = dbc_by_stem.get(stem_key)
        # Preferir rutas con "datos" en la ruta sobre "ejecutables" u otras
        if prev is None:
            dbc_by_stem[stem_key] = dbc_name
        else:
            parts_new = dbc_name.lower().split("/")
            parts_old = prev.lower().split("/")
            # Si el nuevo tiene "dato" y el viejo no, o si el nuevo es más profundo
            has_datos_new = any("dato" in p for p in parts_new)
            has_datos_old = any("dato" in p for p in parts_old)
            if has_datos_new and not has_datos_old:
                dbc_by_stem[stem_key] = dbc_name
    for dbc_name in dbc_by_stem.values():
        try:
            dbc_bytes = zf.read(dbc_name)
            stem = os.path.splitext(dbc_name)[0]
            dct_real = lower_map.get(stem.lower() + ".dct")
            dct_bytes = zf.read(dct_real) if dct_real else b""
            db_info = parse_dbc(dbc_bytes, dct_bytes)
            db_info["name"] = os.path.splitext(os.path.basename(dbc_name))[0]
            databases.append(db_info)
            if db_info["relaciones"] or db_info["stored_procs"] or db_info["vistas"]:
                print(f"  .dbc: {db_info['name']} — "
                      f"{len(db_info['relaciones'])} rels, "
                      f"{len(db_info['stored_procs'])} procs, "
                      f"{len(db_info['vistas'])} vistas")
        except Exception:
            pass

    by_ext = {}
    counts = {}
    forms, reports, programs = [], [], []
    tables = []
    seen_tables = set()  # evita tablas .dbf duplicadas (mismo nombre en varias carpetas)
    forms_detail = []    # controles reales de los formularios .scx
    samples = []
    menus = []           # estructura de menús (.mpr / .mnx)
    mnx_pending = []     # .mnx a parsear solo si no hubo .mpr

    # Ordenamos para que el muestreo sea estable entre ejecuciones.
    for name in sorted(names):
        base = os.path.basename(name)
        ext = os.path.splitext(base)[1].lower()
        if not ext:
            continue
        by_ext[ext] = by_ext.get(ext, 0) + 1
        cat = EXT_CATEGORY.get(ext)
        if cat:
            counts[cat] = counts.get(cat, 0) + 1

        if cat == "forms" and ext == ".scx" and len(forms) < MAX_NAME_LIST:
            forms.append(base)
        elif cat == "reports" and ext == ".frx" and len(reports) < MAX_NAME_LIST:
            reports.append(base)
        elif cat == "programs":
            programs.append(base)

        # Estructura real de las tablas .dbf (leyendo solo el header).
        # Excluimos las tablas de sistema de VFP que no son datos de negocio.
        tname = os.path.splitext(base)[0].lower()
        if (ext == ".dbf" and len(tables) < MAX_TABLES
                and tname not in seen_tables and tname not in VFP_SYSTEM_TABLES):
            try:
                with zf.open(name) as fp:
                    head = fp.read(32 + 32 * 256)  # suficiente para todos los campos
                struct = parse_dbf_structure(head)
                if struct:
                    seen_tables.add(tname)
                    tables.append({
                        "name": os.path.splitext(base)[0],
                        "records": struct["records"],
                        "fields": struct["fields"],
                    })
            except Exception:
                pass

        # Controles reales de los formularios .scx (es una tabla DBF + memo .sct).
        if ext == ".scx" and len(forms_detail) < MAX_FORMS_PARSED:
            try:
                with zf.open(name) as fp:
                    scx_bytes = fp.read()
                sct_bytes = b""
                sct_real = lower_map.get((os.path.splitext(name)[0] + ".sct").lower())
                if sct_real:
                    with zf.open(sct_real) as fp2:
                        sct_bytes = fp2.read()
                info = parse_scx_controls(scx_bytes, sct_bytes)
                if info:
                    forms_detail.append({
                        "name": os.path.splitext(base)[0],
                        "controles": info["counts"],
                        "total": info["total"],
                        "tabla": info.get("tabla", ""),
                        "campos": info.get("campos", []),
                        "botones": info.get("botones", []),
                    })
            except Exception:
                pass

        # Menús: .mpr es código generado (texto) y se parsea mejor que el .mnx.
        if ext == ".mpr":
            try:
                with zf.open(name) as fp:
                    txt = fp.read(400000).decode("latin-1", "replace")
                for m in parse_mpr_menu(txt):
                    if m.get("items"):
                        menus.append(m)
            except Exception:
                pass
        elif ext == ".mnx":
            mnx_pending.append(name)

        # Muestras de código fuente real: .prg, .cbl, .cob, y también archivos
        # de include .h (constantes #DEFINE) y .txt (notas/documentación).
        if ext in (".prg", ".cbl", ".cob", ".h", ".txt") and len(samples) < MAX_SAMPLES:
            try:
                info = zf.getinfo(name)
                if info.file_size <= MAX_SAMPLE_BYTES * 3:
                    with zf.open(name) as fp:
                        content = fp.read(MAX_SAMPLE_BYTES + 200)
                    text = content.decode("latin-1", "replace")
                    if len(text) > MAX_SAMPLE_BYTES:
                        text = text[:MAX_SAMPLE_BYTES] + "\n... (truncado)"
                    samples.append({"name": base, "content": text})
            except Exception:
                pass

    # Clases VFP (.vcx + .vct): extraer métodos como muestras adicionales de código,
    # y el Caption por defecto de cada clase (p.ej. los botones de un ABM heredan
    # "Grabar"/"Cancelar"/... de la clase y no lo repiten en cada .scx que los usa).
    vcx_names = [n for n in sorted(names) if n.lower().endswith(".vcx")]
    vcx_captions = {}
    for vcx_name in vcx_names[:15]:  # máx 15 archivos de clase
        try:
            vcx_bytes = zf.read(vcx_name)
            stem = os.path.splitext(vcx_name)[0]
            vct_real = lower_map.get(stem.lower() + ".vct")
            vct_bytes = zf.read(vct_real) if vct_real else b""
            vcx_base = os.path.basename(vcx_name)
            for m in parse_vcx_methods(vcx_bytes, vct_bytes)[:6]:
                if len(samples) >= MAX_SAMPLES:
                    break
                samples.append({
                    "name": f"{vcx_base}.{m['name']}",
                    "content": m["code"],
                })
            vcx_captions.update(parse_vcx_captions(vcx_bytes, vct_bytes))
        except Exception:
            pass

    # Resolver los botones de cada formulario contra los Caption de clase
    # recién leídos, y descartar los que quedaron sin etiqueta (ni propia ni
    # heredada) para no mostrar badges vacíos en la revisión.
    for f in forms_detail:
        resueltos = []
        for b in f.get("botones") or []:
            cap = b.get("caption") or vcx_captions.get(b.get("clase", ""), "")
            if cap:
                resueltos.append({"name": b["name"], "caption": cap})
        f["botones"] = resueltos

    # Si no hubo menús .mpr, intentamos con los .mnx (DBF + memo .mnt) como fallback.
    if not menus and mnx_pending:
        for name in mnx_pending[:5]:
            try:
                with zf.open(name) as fp:
                    body = fp.read(200000)
                mnt_real = lower_map.get(os.path.splitext(name)[0].lower() + ".mnt")
                mnt_bytes = zf.read(mnt_real) if mnt_real else b""
                menus.extend(parse_mnx_menu(body, mnt_bytes))
            except Exception:
                pass

    # Fallback adicional: sistemas que usan menú dinámico desde programa.dbf.
    # Si los menús extraídos hasta ahora tienen < 3 ítems en total, buscamos
    # programa.dbf (cualquier copia, preferimos la más grande) + menues.dbf.
    total_items = sum(len(m.get("items", [])) for m in menus)
    if total_items < 3:
        prog_candidates = [n for n in names if os.path.basename(n).lower() == "programa.dbf"]
        if prog_candidates:
            # La más grande suele ser la de producción
            prog_name = max(prog_candidates, key=lambda n: zf.getinfo(n).file_size)
            try:
                prog_bytes = zf.read(prog_name)
                men_name = next(
                    (n for n in names if os.path.basename(n).lower() == "menues.dbf"), None)
                men_bytes = zf.read(men_name) if men_name else b""
                prog_menus = _parse_programa_menu(prog_bytes, men_bytes)
                if len(prog_menus) > 0:
                    menus = prog_menus  # reemplaza los menús placeholder
                    print(f"  menú dinámico: {sum(len(m['items']) for m in menus)} "
                          f"ítems en {len(menus)} grupos (desde programa.dbf)")
            except Exception:
                pass

    return {
        "total_files": len(names),
        "size_mb": round(len(raw_bytes) / 1024 / 1024, 1),
        "by_ext": by_ext,
        "counts": counts,
        "tables": tables,
        "forms": forms,
        "forms_detail": forms_detail,
        "reports": reports,
        "programs": sorted(set(programs))[:MAX_NAME_LIST],
        "samples": samples,
        "menus": menus,
        "project": project,
        "databases": databases,
    }


def safe_name(name, default):
    """Evita rutas peligrosas: deja solo el nombre de archivo."""
    name = os.path.basename(str(name or "")).strip().replace("\\", "").replace("/", "")
    return name or default


class Handler(http.server.BaseHTTPRequestHandler):

    def handle(self):
        # El navegador a veces corta la conexión a mitad de la respuesta
        # (recarga, cambia de página o cancela). En Windows eso lanza
        # ConnectionAbortedError/ConnectionResetError; no es un error real,
        # así que lo ignoramos para no ensuciar la consola con un traceback.
        try:
            super().handle()
        except ConnectionError:
            pass

    def log_message(self, fmt, *args):
        try:
            print(f"  {args[0]} {args[1]}")
        except Exception:
            pass

    # --- helpers de respuesta -------------------------------------------
    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")

    def send_json(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    # --- métodos HTTP ----------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors()
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.serve_file("index.html", "text/html; charset=utf-8")
        elif self.path == "/api/ollama/models":
            # Lista los modelos locales instalados en Ollama (para el modo gratuito).
            # Si Ollama no está corriendo, devuelve available=False sin romper la UI.
            try:
                req = urllib.request.Request(OLLAMA_URL + "/api/tags", method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read() or b"{}")
                models = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
                self.send_json(200, {"available": True, "models": models,
                                     "default": OLLAMA_DEFAULT_MODEL})
            except Exception:
                self.send_json(200, {"available": False, "models": [],
                                     "default": OLLAMA_DEFAULT_MODEL})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        global API_KEY

        if self.path == "/api/key":
            try:
                body = json.loads(self.read_body() or b"{}")
            except Exception:
                self.send_json(400, {"ok": False, "error": "JSON inválido"})
                return
            API_KEY = (body.get("key") or "").strip()
            ok = API_KEY.startswith("sk-")
            if ok:
                print("\n  ✓ API key configurada correctamente\n")
            self.send_json(200, {"ok": ok})
            return

        if self.path == "/api/zipinfo":
            raw = self.read_body()
            if not raw:
                self.send_json(400, {"error": {"message": "No se recibió el archivo ZIP"}})
                return
            try:
                info = analyze_zip(raw)
                info["zip_token"] = _cache_zip(raw)  # para importar datos al generar
                print(f"  ✓ ZIP leído: {info['total_files']} archivos, "
                      f"{len(info['tables'])} tablas analizadas")
                self.send_json(200, info)
            except zipfile.BadZipFile:
                self.send_json(400, {"error": {"message": "El archivo no es un ZIP válido"}})
            except Exception as e:
                self.send_json(500, {"error": {"message": f"Error al leer el ZIP: {e}"}})
            return

        if self.path == "/api/scaffold":
            # Cobertura total: arma una app completa (FastAPI + SPA) que expone
            # TODAS las utilidades del ZIP. Determinístico, sin IA.
            try:
                payload = json.loads(self.read_body() or b"{}")
            except Exception:
                self.send_json(400, {"error": {"message": "JSON inválido"}})
                return
            # Si tenemos el ZIP en caché, importamos los datos, índices e imágenes.
            raw = _ZIP_CACHE.get(payload.get("zip_token"))
            assets = {}
            if raw:
                try:
                    seed, indexes, total = build_seed_from_zip(raw, payload.get("inventory") or {})
                    payload["seed"] = seed
                    payload["indexes"] = indexes
                    print(f"  ✓ Datos a importar: {total} registros en {len(seed)} tablas, "
                          f"{sum(len(v) for v in indexes.values())} índices")
                except Exception as e:
                    print(f"  ! No se pudieron importar los datos: {e}")
                try:
                    assets = extract_assets_from_zip(raw)
                    if assets:
                        print(f"  ✓ Imágenes a incluir: {len(assets)}")
                except Exception as e:
                    print(f"  ! No se pudieron extraer las imágenes: {e}")
            try:
                data, meta = scaffold.build_app_scaffold(payload, assets=assets)
            except Exception as e:
                self.send_json(500, {"error": {"message": f"Error al generar la app: {e}"}})
                return
            st = meta.get("stats", {})
            print(f"  ✓ App generada: {st.get('tablas', 0)} ABM, "
                  f"{len(meta['menus'])} menús, {st.get('reportes', 0)} reportes, "
                  f"{st.get('registros_importados', 0)} registros, "
                  f"{st.get('imagenes', 0)} imágenes")
            fname = safe_name(payload.get("filename"), "app-migrada.zip")
            self.send_response(200)
            self.send_cors()
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("X-Rows-Imported", str(st.get("registros_importados", 0)))
            self.send_header("X-Tables-Seeded", str(st.get("tablas_con_datos", 0)))
            self.send_header("X-Images-Imported", str(st.get("imagenes", 0)))
            self.send_header("Access-Control-Expose-Headers",
                             "X-Rows-Imported, X-Tables-Seeded, X-Images-Imported")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path == "/api/ollama":
            # Modo gratuito: genera con un modelo local vía Ollama (sin API key).
            try:
                payload = json.loads(self.read_body() or b"{}")
            except Exception:
                self.send_json(400, {"error": {"message": "JSON inválido"}})
                return
            model = (payload.get("model") or OLLAMA_DEFAULT_MODEL).strip()
            prompt = payload.get("prompt") or ""
            try:
                num_predict = int(payload.get("num_predict") or 4000)
            except Exception:
                num_predict = 4000
            # Tope los tokens a generar: en CPU, pedir 16000 tarda muchísimo.
            num_predict = min(num_predict, OLLAMA_MAX_PREDICT)
            options = {
                "num_predict": num_predict,
                # temperatura baja: queremos JSON/código determinista.
                "temperature": 0.2,
                # contexto amplio: el prompt incluye tablas/código del ZIP y
                # con el default (2-4k) se truncaría y el modelo perdería datos.
                "num_ctx": OLLAMA_NUM_CTX,
            }
            # Exprimir recursos: GPU/threads/batch (si se configuraron por entorno).
            if OLLAMA_NUM_GPU is not None:
                options["num_gpu"] = OLLAMA_NUM_GPU
            if OLLAMA_NUM_THREAD is not None:
                options["num_thread"] = OLLAMA_NUM_THREAD
            if OLLAMA_NUM_BATCH is not None:
                options["num_batch"] = OLLAMA_NUM_BATCH
            req_body = json.dumps({
                "model": model,
                "prompt": prompt,
                "stream": False,
                "keep_alive": OLLAMA_KEEP_ALIVE,  # mantener el modelo caliente
                "options": options,
            }).encode("utf-8")
            req = urllib.request.Request(
                OLLAMA_URL + "/api/generate",
                data=req_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                # Los modelos locales pueden ser lentos: damos margen amplio.
                with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
                    data = resp.read()
                print(f"  ✓ Respuesta de Ollama ({model})")
                self.send_response(200)
                self.send_cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except urllib.error.HTTPError as e:
                msg = ""
                try:
                    msg = json.loads(e.read() or b"{}").get("error", "")
                except Exception:
                    pass
                if "not found" in str(msg).lower() or e.code == 404:
                    msg = (f"El modelo '{model}' no está instalado. "
                           f"Ejecutá:  ollama pull {model}")
                self.send_json(e.code, {"error": {"message": msg or f"Error de Ollama (HTTP {e.code})"}})
            except TimeoutError:
                self.send_json(504, {"error": {"message": _ollama_timeout_msg(model)}})
            except urllib.error.URLError as e:
                # Un timeout de lectura puede llegar envuelto en URLError.
                if isinstance(getattr(e, "reason", None), TimeoutError):
                    self.send_json(504, {"error": {"message": _ollama_timeout_msg(model)}})
                else:
                    self.send_json(503, {"error": {"message":
                        f"No se pudo conectar con Ollama en {OLLAMA_URL}. "
                        f"Instalalo desde https://ollama.com, abrilo y ejecutá:  "
                        f"ollama pull {model}"}})
            except Exception as e:
                self.send_json(500, {"error": {"message": str(e)}})
            return

        if self.path == "/api/claude":
            if not API_KEY:
                self.send_json(401, {"error": {"message": "API key no configurada"}})
                return

            body = self.read_body()
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    data = resp.read()
                self.send_response(200)
                self.send_cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except urllib.error.HTTPError as e:
                error_body = e.read()
                self.send_response(e.code)
                self.send_cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(error_body)))
                self.end_headers()
                self.wfile.write(error_body)
            except TimeoutError:
                self.send_json(504, {"error": {"message":
                    "La API de Anthropic no respondió a tiempo (timeout de 180 s). "
                    "Reintentá; si persiste, revisá tu conexión a internet."}})
            except urllib.error.URLError as e:
                if isinstance(getattr(e, "reason", None), TimeoutError):
                    self.send_json(504, {"error": {"message":
                        "La API de Anthropic no respondió a tiempo (timeout). Reintentá."}})
                else:
                    self.send_json(502, {"error": {"message":
                        f"No se pudo contactar a la API de Anthropic: {e.reason}. "
                        f"Revisá tu conexión a internet."}})
            except Exception as e:
                self.send_json(500, {"error": {"message": str(e)}})
            return

        self.send_response(404)
        self.end_headers()

    def serve_file(self, filename, content_type):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_cors()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    print("\n" + "=" * 50)
    print("  LegacyMigrator — Servidor local")
    print("=" * 50)
    print(f"\n  ► Abrí tu navegador en:  http://localhost:{PORT}")
    print(f"  ► Para cerrar presioná:  Ctrl+C\n")
    print("=" * 50 + "\n")

    # Multihilo: una llamada larga al modelo no bloquea otras peticiones del
    # navegador (favicon, recargas, etc.), así la UI sigue respondiendo.
    server = http.server.ThreadingHTTPServer(("localhost", PORT), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\n  Servidor cerrado. ¡Hasta luego!\n")
        sys.exit(0)
