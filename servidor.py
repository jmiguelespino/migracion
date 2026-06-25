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

# ZIPs subidos se guardan en disco (carpeta _uploads/) en lugar de RAM.
# El caché en memoria solo guarda rutas (strings), no los bytes completos.
# Esto permite manejar ZIPs grandes sin agotar la memoria del proceso.
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_uploads")
_ZIP_CACHE = {}        # token -> path en disco
_ZIP_CACHE_ORDER = []  # orden de inserción (para descartar los más viejos)
ZIP_CACHE_MAX = 5      # rutas son baratas; guardamos más para facilitar recovery


def _cache_zip(raw):
    import hashlib
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    token = hashlib.sha1(raw).hexdigest()[:16]
    path = os.path.join(UPLOAD_DIR, f"{token}.zip")
    if token not in _ZIP_CACHE:
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(raw)
        _ZIP_CACHE[token] = path
        _ZIP_CACHE_ORDER.append(token)
        while len(_ZIP_CACHE_ORDER) > ZIP_CACHE_MAX:
            old_tok = _ZIP_CACHE_ORDER.pop(0)
            old_path = _ZIP_CACHE.pop(old_tok, None)
            if old_path and os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except Exception:
                    pass
    return token


def _get_zip_path(token):
    """Devuelve la ruta al ZIP en disco, o None si no existe.

    Primero busca en el caché de rutas en memoria; si no está (p.ej. después
    de reiniciar el servidor), intenta reconstruir la ruta desde el token.
    """
    if not token:
        return None
    path = _ZIP_CACHE.get(token)
    if path and os.path.exists(path):
        return path
    candidate = os.path.join(UPLOAD_DIR, f"{token}.zip")
    if os.path.exists(candidate):
        _ZIP_CACHE[token] = candidate
        return candidate
    return None


# ── Estado persistente del proyecto ──────────────────────────────────────────
# Se guarda en migrador_estado.json junto al servidor. Sobrevive reinicios.
# Estructura: {"proyecto", "zip_token", "cargado", "unidades":{}, "datos":{}}
ESTADO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "migrador_estado.json")
UNIDADES_ORDEN = [
    "proyecto", "bases", "imagenes", "clases",
    "menus", "codigo", "reportes", "pantallas",
]


def _cargar_estado():
    try:
        if os.path.exists(ESTADO_FILE):
            with open(ESTADO_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _guardar_estado(est):
    try:
        with open(ESTADO_FILE, "w", encoding="utf-8") as f:
            json.dump(est, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ! No se pudo guardar el estado: {e}")


def _estado_nuevo(nombre_zip, zip_token):
    import datetime
    return {
        "proyecto": nombre_zip,
        "zip_token": zip_token,
        "zip_path": os.path.join(UPLOAD_DIR, f"{zip_token}.zip"),
        "cargado": datetime.datetime.now().isoformat(timespec="seconds"),
        "unidades": {
            u: {"estado": "pendiente"} for u in UNIDADES_ORDEN
        },
        "datos": {},   # resultados acumulados por unidad
    }


def _todas_completas(est):
    """True si todas las unidades de trabajo están en estado 'completo'."""
    return all(
        est.get("unidades", {}).get(u, {}).get("estado") == "completo"
        for u in UNIDADES_ORDEN
    )


def _procesar_unidad(nombre, zip_path, est, body):
    """Procesa la unidad `nombre` del proyecto.

    zip_path es la ruta al ZIP en disco (no bytes en RAM).
    Actualiza est["unidades"][nombre] y est["datos"][nombre] en el lugar.
    Devuelve un dict con el resultado (se enviará como JSON al navegador).
    """
    import datetime

    def _ts():
        return datetime.datetime.now().isoformat(timespec="seconds")

    inv = est.get("datos", {}).get("zipinfo", {})
    zf  = zipfile.ZipFile(zip_path)
    names = [n for n in zf.namelist() if not n.endswith("/")]

    if nombre == "proyecto":
        # Extraer info del .pjx (ya está en el inventario)
        prj = inv.get("project") or {}
        resultado = {
            "principal": prj.get("principal", "(no declarado)"),
            "n_archivos": len(prj.get("archivos", [])),
        }
        est["unidades"]["proyecto"] = {
            "estado": "completo", "cuando": _ts(), **resultado
        }
        est["datos"]["proyecto"] = resultado
        return {"ok": True, **resultado}

    if nombre == "bases":
        # La unidad más pesada: DBF + CDX + DBC con soporte de paginación.
        # body puede traer {"limite": N} para controlar cuántas tablas por pasada.
        limite = max(1, min(200, int(body.get("limite") or 20)))
        tablas_all = inv.get("tables") or []
        n_total = len(tablas_all)

        # Acumular sobre lo ya procesado en pasadas anteriores
        datos_bases = est.get("datos", {}).get("bases") or {
            "seed": {}, "indexes": {}, "tablas_ok": 0, "tablas_total": n_total,
        }
        n_ok   = datos_bases.get("tablas_ok", 0)
        seed   = datos_bases.get("seed", {})
        indexes = datos_bases.get("indexes", {})

        # by_stem: base sin extensión (lower) -> {ext: ruta}; prioridad al más grande
        by_stem = {}
        file_sizes = {n: zf.getinfo(n).file_size for n in names}
        for n in names:
            base = os.path.basename(n)
            stem, ext = os.path.splitext(base)
            sk, ek = stem.lower(), ext.lower()
            prev = by_stem.get(sk, {}).get(ek)
            if prev is None or file_sizes.get(n, 0) > file_sizes.get(prev, 0):
                by_stem.setdefault(sk, {})[ek] = n

        pendientes = tablas_all[n_ok: n_ok + limite]
        for t in pendientes:
            name_t = t.get("name") or ""
            key    = scaffold._slug(name_t)
            entry  = by_stem.get(name_t.lower())
            if not entry or ".dbf" not in entry:
                n_ok += 1
                continue
            # Datos reales
            try:
                dbf_b = zf.read(entry[".dbf"])
                fpt_b = zf.read(entry[".fpt"]) if ".fpt" in entry else b""
                rows  = read_dbf_records(dbf_b, fpt_b)
            except Exception:
                rows = []
            if rows:
                seed[key] = rows
            # Índices CDX/IDX — respetar expresiones originales
            field_slugs = [scaffold._slug(f.get("name")) for f in (t.get("fields") or [])]
            for ext_i in (".cdx", ".idx"):
                if ext_i not in entry:
                    continue
                try:
                    new_defs = parse_cdx_expressions(zf.read(entry[ext_i]), field_slugs)
                    existing = indexes.get(key, [])
                    seen_sigs = {tuple(d) for d in existing}
                    for d in new_defs:
                        sig = tuple(d)
                        if sig not in seen_sigs:
                            existing.append(d)
                            seen_sigs.add(sig)
                    if existing:
                        indexes[key] = existing[:16]
                except Exception:
                    pass
            n_ok += 1

        total_rows  = sum(len(v) for v in seed.values())
        total_idx   = sum(len(v) for v in indexes.values())
        completado  = n_ok >= n_total
        resultado = {
            "tablas_ok": n_ok, "tablas_total": n_total,
            "total_rows": total_rows, "total_idx": total_idx,
        }
        est["datos"]["bases"] = {
            "seed": seed, "indexes": indexes, **resultado,
        }
        est["unidades"]["bases"] = {
            "estado": "completo" if completado else "parcial",
            "cuando": _ts(), **resultado,
        }
        return {"ok": True, "completado": completado, **resultado}

    if nombre == "imagenes":
        imgs = extract_assets_from_zip(zip_path)
        resultado = {"count": len(imgs)}
        # Guardamos solo los nombres (los bytes son pesados)
        est["datos"]["imagenes"] = {"nombres": list(imgs.keys())}
        est["unidades"]["imagenes"] = {"estado": "completo", "cuando": _ts(), **resultado}
        return {"ok": True, **resultado}

    if nombre == "clases":
        vcx_count = sum(1 for n in names if n.lower().endswith(".vcx"))
        prg_count = sum(1 for n in names if n.lower().endswith(".prg"))
        resultado = {"vcx": vcx_count, "prg": prg_count}
        est["datos"]["clases"] = resultado
        est["unidades"]["clases"] = {"estado": "completo", "cuando": _ts(), **resultado}
        return {"ok": True, **resultado}

    if nombre == "menus":
        menus = inv.get("menus") or []
        n_items = sum(len(m.get("items") or []) for m in menus)
        resultado = {"menus": len(menus), "items": n_items}
        est["datos"]["menus"] = resultado
        est["unidades"]["menus"] = {"estado": "completo", "cuando": _ts(), **resultado}
        return {"ok": True, **resultado}

    if nombre == "codigo":
        prg_count = (inv.get("by_ext") or {}).get(".prg", 0)
        h_count   = (inv.get("by_ext") or {}).get(".h", 0)
        resultado = {"prg": prg_count, "h": h_count}
        est["datos"]["codigo"] = resultado
        est["unidades"]["codigo"] = {"estado": "completo", "cuando": _ts(), **resultado}
        return {"ok": True, **resultado}

    if nombre == "reportes":
        # Paginación: parsea N archivos .frx por pasada desde el ZIP en disco.
        limite = max(1, min(100, int(body.get("limite") or 30)))
        frx_all = sorted(n for n in names if n.lower().endswith(".frx"))
        n_total = len(frx_all)

        datos_rep = est.get("datos", {}).get("reportes") or {
            "reports_detail": [], "frx_ok": 0, "frx_total": n_total,
        }
        n_ok = datos_rep.get("frx_ok", 0)
        reports_detail = datos_rep.get("reports_detail", [])
        seen_rep = {r["name"].lower() for r in reports_detail}
        lower_map = {n.lower(): n for n in names}

        for frx_name in frx_all[n_ok: n_ok + limite]:
            base = os.path.basename(frx_name)
            stem = os.path.splitext(base)[0]
            if stem.lower() not in seen_rep:
                seen_rep.add(stem.lower())
                try:
                    frx_bytes = zf.read(frx_name)
                    frt_key   = (os.path.splitext(frx_name)[0] + ".frt").lower()
                    frt_real  = lower_map.get(frt_key)
                    frt_bytes = zf.read(frt_real) if frt_real else b""
                    info = parse_frx(frx_bytes, frt_bytes)
                    if info is not None:
                        info["name"] = stem
                        reports_detail.append(info)
                except Exception:
                    pass
            n_ok += 1

        completado = n_ok >= n_total
        resultado = {"frx_ok": n_ok, "frx_total": n_total, "reports": len(reports_detail)}
        datos_rep.update({"reports_detail": reports_detail, "frx_ok": n_ok, "frx_total": n_total})
        est["datos"]["reportes"] = datos_rep
        est["unidades"]["reportes"] = {
            "estado": "completo" if completado else "parcial",
            "cuando": _ts(), **resultado,
        }
        return {"ok": True, "completado": completado, **resultado}

    if nombre == "pantallas":
        # Paginación: parsea N archivos .scx por pasada desde el ZIP en disco.
        limite = max(1, min(100, int(body.get("limite") or 30)))
        scx_all = sorted(n for n in names if n.lower().endswith(".scx"))
        n_total = len(scx_all)

        datos_pant = est.get("datos", {}).get("pantallas") or {
            "forms_detail": [], "scx_ok": 0, "scx_total": n_total,
        }
        n_ok = datos_pant.get("scx_ok", 0)
        forms_detail = datos_pant.get("forms_detail", [])
        seen_scx = {f["name"].lower() for f in forms_detail}
        lower_map = {n.lower(): n for n in names}

        for scx_name in scx_all[n_ok: n_ok + limite]:
            base = os.path.basename(scx_name)
            stem = os.path.splitext(base)[0]
            if stem.lower() not in seen_scx:
                seen_scx.add(stem.lower())
                try:
                    scx_bytes = zf.read(scx_name)
                    sct_key   = (os.path.splitext(scx_name)[0] + ".sct").lower()
                    sct_real  = lower_map.get(sct_key)
                    sct_bytes = zf.read(sct_real) if sct_real else b""
                    info = parse_scx_controls(scx_bytes, sct_bytes)
                    if info:
                        forms_detail.append({
                            "name": stem,
                            "controles": info["counts"],
                            "total": info["total"],
                            "tabla": info.get("tabla", ""),
                            "campos": info.get("campos", []),
                            "metodos": info.get("metodos", []),
                        })
                except Exception:
                    pass
            n_ok += 1

        completado = n_ok >= n_total
        resultado = {"scx_ok": n_ok, "scx_total": n_total, "forms": len(forms_detail)}
        datos_pant.update({"forms_detail": forms_detail, "scx_ok": n_ok, "scx_total": n_total})
        est["datos"]["pantallas"] = datos_pant
        est["unidades"]["pantallas"] = {
            "estado": "completo" if completado else "parcial",
            "cuando": _ts(), **resultado,
        }
        return {"ok": True, "completado": completado, **resultado}

    return {"error": f"Unidad desconocida: {nombre}"}

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


def build_seed_from_zip(source, inventory):
    """Extrae datos e índices reales del ZIP para sembrar la app generada.
    source puede ser bytes (compat.) o una ruta a un archivo en disco.
    Devuelve (seed, indexes, total_filas) con claves = slug del nombre de tabla."""
    zf = zipfile.ZipFile(source if isinstance(source, str) else io.BytesIO(source))
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


def extract_assets_from_zip(source):
    """Extrae las imágenes del ZIP para incluirlas en la app generada.
    source puede ser bytes (compat.) o una ruta a un archivo en disco.
    Devuelve {nombre_base_lower: bytes}. Acota tamaño por archivo y total."""
    zf = zipfile.ZipFile(source if isinstance(source, str) else io.BytesIO(source))
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


def _frx_field_name(expr):
    """Extrae el slug del campo de una expresión FRX ('tabla.campo', FUNC(campo), etc.)."""
    expr = expr.strip()
    m = re.match(r'^[\w]+\.([\w]+)$', expr)
    if m:
        return scaffold._slug(m.group(1))
    m = re.match(r'^([\w]+)$', expr)
    if m:
        return scaffold._slug(m.group(1))
    for pat in [r'^\w+\s*\(\s*[\w]+\.([\w]+)', r'^\w+\s*\(\s*([\w]+)']:
        m = re.match(pat, expr, re.I)
        if m:
            return scaffold._slug(m.group(1))
    return scaffold._slug(re.sub(r'[^a-zA-Z0-9_]', '_', expr)[:30])


def parse_frx(frx_bytes, frt_bytes=None):
    """Extrae metadatos de un reporte VFP (.frx + .frt).

    El .frx es una tabla DBF donde cada fila es un objeto del reporte:
    OBJTYPE=1  → encabezado del reporte (data source en EXPR)
    OBJTYPE=8  → banda (título, encabezado de página/columna, detalle, etc.)
    OBJTYPE=9  → campo o etiqueta de texto

    Estrategia: los campos de datos tienen expresiones sin comillas
    (ej. clientes.nombre, ALLTRIM(nombre)) y los encabezados de columna
    tienen expresiones entre comillas ("Nombre", "Importe").
    Se ordenan por HPOS y se emparejan por proximidad.

    Retorna {title, data_expr, tabla_base, cols: [{expr, field, label}]} o None.
    """
    rows = read_dbf_records(frx_bytes, frt_bytes or b"", max_records=5000)
    if not rows:
        return None

    data_expr = ""
    tabla_base = ""

    # 1. Registro raíz (OBJTYPE=1) → fuente de datos del reporte
    for r in rows:
        if str(r.get("objtype") or "").strip() == "1":
            data_expr = str(r.get("expr") or "").strip()
            break

    if data_expr:
        # FROM tabla → tabla_base
        m = re.search(r'\bFROM\b\s+([A-Za-z_][A-Za-z0-9_]*)', data_expr, re.I)
        if m:
            tabla_base = scaffold._slug(m.group(1))
        elif re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', data_expr):
            tabla_base = scaffold._slug(data_expr)

    # 2. Identificar bandas y sus rangos VPOS para clasificar campos
    bands = {}   # objcode_str → (vpos_start, vpos_end)
    for r in rows:
        if str(r.get("objtype") or "").strip() != "8":
            continue
        try:
            code = str(r.get("objcode") or "").strip()
            vps = float(str(r.get("vpos") or 0))
            hgt = float(str(r.get("height") or 0))
            if code not in bands:
                bands[code] = (vps, vps + max(hgt, 50))
        except (TypeError, ValueError):
            pass

    def _which_band(vpos_val):
        for code, (vs, ve) in bands.items():
            if vs <= vpos_val <= ve + 80:
                return code
        return None

    # 3. Clasificar OBJTYPE=9 por banda
    detail_objs = []    # [(hpos, expr, width)]
    header_objs = []    # [(hpos, quoted_label, width)]
    title_text  = ""

    for r in rows:
        if str(r.get("objtype") or "").strip() != "9":
            continue
        expr = str(r.get("expr") or "").strip()
        if not expr:
            continue
        try:
            hpos  = float(str(r.get("hpos")   or 0))
            vpos  = float(str(r.get("vpos")   or 0))
            width = float(str(r.get("width")  or 720))
        except (TypeError, ValueError):
            continue

        is_label = expr.startswith('"') or expr.startswith("'")
        band = _which_band(vpos)

        # Banda de detalle (3): campos de datos (no etiquetas)
        if band == "3" and not is_label:
            detail_objs.append((hpos, expr, width))
        # Banda de encabezado de columna (2) o encabezado de página (1): etiquetas
        elif band in ("1", "2") and is_label:
            header_objs.append((hpos, expr.strip('"\''), width))
        # Banda de título (0): primer texto → título del reporte
        elif band == "0" and is_label and not title_text:
            title_text = expr.strip('"\'')

    # Fallback: si no hay bandas reconocidas, heurística por expresión
    if not detail_objs and not header_objs:
        all9 = []
        for r in rows:
            if str(r.get("objtype") or "").strip() != "9":
                continue
            expr = str(r.get("expr") or "").strip()
            if not expr:
                continue
            try:
                hpos  = float(str(r.get("hpos") or 0))
                width = float(str(r.get("width") or 720))
            except (TypeError, ValueError):
                continue
            is_label = expr.startswith('"') or expr.startswith("'")
            if is_label:
                header_objs.append((hpos, expr.strip('"\''), width))
            else:
                detail_objs.append((hpos, expr, width))

    # Título desde encabezados si no se encontró en banda título
    if not title_text:
        for _, lbl, _ in sorted(header_objs):
            if lbl:
                title_text = lbl
                break

    # 4. Construir columnas: ordenar detalle por HPOS, emparejar con header
    detail_objs.sort(key=lambda x: x[0])
    header_objs.sort(key=lambda x: x[0])

    cols = []
    for hpos, expr, width in detail_objs:
        label = expr   # default
        best  = width * 2
        for chpos, clabel, cwidth in header_objs:
            dist = abs(chpos - hpos)
            if dist < best:
                best  = dist
                label = clabel
        cols.append({
            "expr":  expr,
            "field": _frx_field_name(expr),
            "label": label,
        })
        if len(cols) >= 30:
            break

    return {
        "title":      title_text,
        "data_expr":  data_expr,
        "tabla_base": tabla_base,
        "cols":       cols,
    }


def _split_args(s):
    """Divide argumentos de función separados por coma, respetando paréntesis y comillas."""
    parts, depth, cur, in_str, str_ch = [], 0, [], False, ''
    for ch in s:
        if in_str:
            cur.append(ch)
            if ch == str_ch:
                in_str = False
        elif ch in ('"', "'"):
            in_str, str_ch = True, ch
            cur.append(ch)
        elif ch == '(':
            depth += 1; cur.append(ch)
        elif ch == ')':
            depth -= 1; cur.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(cur)); cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append(''.join(cur))
    return parts


def _translate_vfp_view_sql(sql):
    """Traducción best-effort de SQL VFP a SQLite.
    Retorna (sql_sqlite: str, warnings: list[str])."""
    if not sql or not sql.strip():
        return "", []
    warns = []
    s = sql.strip()

    # Continuación de línea VFP con punto y coma
    s = re.sub(r';\s*\n', ' ', s)

    # Cláusulas de destino/salida exclusivas de VFP (no existen en SQLite)
    for pat in [
        r'\bINTO\s+(?:CURSOR|TABLE|ARRAY)\s+\w+',
        r'\bTO\s+(?:FILE|PRINTER)\s*\S*',
        r'\bNOFILTER\b', r'\bREADWRITE\b', r'\bNOUPDATE\b',
        r'\bWITH\s+BUFFERING\b', r'\bNOCONSOLE\b', r'\bNOLOG\b',
    ]:
        s = re.sub(pat, '', s, flags=re.I)

    # Comentarios VFP: && hasta fin de línea; * al inicio de línea
    s = re.sub(r'&&[^\n]*', '', s)
    lines = []
    for line in s.split('\n'):
        stripped = line.lstrip()
        if stripped.startswith('* ') or stripped == '*':
            lines.append('-- ' + stripped[1:].lstrip())
        else:
            lines.append(line)
    s = '\n'.join(lines)

    # Prefijo de esquema: database!tabla → tabla
    s = re.sub(r'\b\w+!(\w+)\b', r'\1', s)

    # Literales de fecha {^YYYY-MM-DD} → 'YYYY-MM-DD'
    s = re.sub(
        r'\{\^(\d{4}[-/]\d{2}[-/]\d{2})[^}]*\}',
        lambda m: "'" + m.group(1).replace('/', '-') + "'", s)
    def _mdy(m):
        p = m.group(1).split('/')
        return (f"'{p[2]}-{p[0].zfill(2)}-{p[1].zfill(2)}'"
                if len(p) == 3 else m.group(0))
    s = re.sub(r'\{(\d{1,2}/\d{1,2}/\d{4})\}', _mdy, s)

    # Literales lógicos y operadores
    s = re.sub(r'\.(T|TRUE)\.',  '1',     s, flags=re.I)
    s = re.sub(r'\.(F|FALSE)\.', '0',     s, flags=re.I)
    s = re.sub(r'\.NULL\.',      'NULL',  s, flags=re.I)
    s = re.sub(r'\.NOT\.',       'NOT ',  s, flags=re.I)
    s = re.sub(r'\.AND\.',       ' AND ', s, flags=re.I)
    s = re.sub(r'\.OR\.',        ' OR ',  s, flags=re.I)

    # DELETED() → 0 (SQLite no tiene flag de borrado lógico)
    if re.search(r'\bDELETED\s*\(', s, re.I):
        s = re.sub(r'\bDELETED\s*\(\s*\)', '0', s, flags=re.I)
        warns.append("DELETED() → 0 (sin flag de borrado en SQLite)")

    # EMPTY(x) → (x IS NULL OR TRIM(CAST(x AS TEXT))='')
    s = re.sub(
        r'\bEMPTY\s*\(([^)]+)\)',
        lambda m: (f"({m.group(1).strip()} IS NULL OR "
                   f"TRIM(CAST({m.group(1).strip()} AS TEXT))='')"),
        s, flags=re.I)

    # ISNULL(x) → x IS NULL; NVL → COALESCE
    s = re.sub(r'\bISNULL\s*\(([^)]+)\)', r'\1 IS NULL', s, flags=re.I)
    s = re.sub(r'\bNVL\s*\(', 'COALESCE(', s, flags=re.I)

    # IIF(cond, a, b) → CASE WHEN cond THEN a ELSE b END (multipase para anidados)
    def _iif(m):
        parts = _split_args(m.group(1))
        if len(parts) == 3:
            return (f"CASE WHEN {parts[0].strip()} "
                    f"THEN {parts[1].strip()} ELSE {parts[2].strip()} END")
        warns.append(f"IIF() con {len(parts)} args no traducido")
        return m.group(0)
    for _ in range(5):
        prev = s
        s = re.sub(r'\bIIF\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)', _iif, s, flags=re.I)
        if s == prev:
            break
    if re.search(r'\bIIF\s*\(', s, re.I):
        warns.append("IIF() anidados profundos — revisar manualmente")

    # Funciones de cadena
    s = re.sub(r'\bALLTRIM\s*\(', 'TRIM(', s, flags=re.I)
    s = re.sub(r'\bLEN\s*\(',     'LENGTH(', s, flags=re.I)
    s = re.sub(r'\bSTR\s*\(([^,)]+)(?:,[^)]*)?\)', r'CAST(\1 AS TEXT)', s, flags=re.I)
    s = re.sub(r'\bVAL\s*\(([^)]+)\)', r'CAST(\1 AS REAL)',    s, flags=re.I)
    s = re.sub(r'\bINT\s*\(([^)]+)\)', r'CAST(\1 AS INTEGER)', s, flags=re.I)

    # Funciones de fecha → strftime de SQLite
    s = re.sub(r'\bYEAR\s*\(([^)]+)\)',  r"CAST(strftime('%Y',\1) AS INTEGER)", s, flags=re.I)
    s = re.sub(r'\bMONTH\s*\(([^)]+)\)', r"CAST(strftime('%m',\1) AS INTEGER)", s, flags=re.I)
    s = re.sub(r'\bDAY\s*\(([^)]+)\)',   r"CAST(strftime('%d',\1) AS INTEGER)", s, flags=re.I)
    s = re.sub(r'\bDATE\s*\(\s*\)',      "date('now')", s, flags=re.I)
    s = re.sub(r'\bDATETIME\s*\(\s*\)', "datetime('now')", s, flags=re.I)
    s = re.sub(r'\bDTOC\s*\(([^)]+)\)', r"strftime('%d/%m/%Y',\1)", s, flags=re.I)
    s = re.sub(r'\bDTOS\s*\(([^)]+)\)', r"strftime('%Y%m%d',\1)",   s, flags=re.I)

    # SELECT TOP n → SELECT ... LIMIT n (VFP 9)
    top_m = re.search(r'\bSELECT\s+TOP\s+(\d+)\s+', s, re.I)
    if top_m:
        n = top_m.group(1)
        s = re.sub(r'\bSELECT\s+TOP\s+\d+\s+', 'SELECT ', s, flags=re.I, count=1)
        if not re.search(r'\bLIMIT\s+\d+', s, re.I):
            s = s.rstrip().rstrip(';') + f'\nLIMIT {n}'

    # Limpiar ruido generado por DELETED() → 0
    s = re.sub(r'\bWHERE\s+NOT\s+0\s*(?=$|\n)', '',  s, flags=re.I | re.M)
    s = re.sub(r'\bWHERE\s+0\s+AND\s+', 'WHERE ', s, flags=re.I)
    s = re.sub(r'\bAND\s+NOT\s+0\b', '',           s, flags=re.I)
    s = re.sub(r'\bNOT\s+0\s+AND\s+', '',          s, flags=re.I)
    s = re.sub(r'\bAND\s+0\b', '',                  s, flags=re.I)

    s = re.sub(r'[ \t]{2,}', ' ', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip(), warns


def parse_dbc(dbc_bytes, dct_bytes):
    """Lee el Database Container de VFP (.dbc + memo .dct).

    Un .dbc es un DBF especial: cada registro describe un objeto de la base
    de datos (tabla, campo, relación, vista, stored procedure, etc.).
    Se identifica el tipo por el campo OBJECTTYPE y la jerarquía por PARENTID.

    Devuelve:
    - relaciones:    [{parent_table, parent_field, child_table, child_field}]
    - campos:        {tabla_slug: {campo_slug: {caption, defaultvalue, inputmask, ...}}}
    - stored_procs:  [{name, code}]
    - vistas:        [{name, sql_vfp, sql_sqlite, tabla_base, warnings}]
    - vista_to_tabla: {vista_slug: tabla_base_slug}
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
    vista_to_tabla: dict = {}
    seen_rels: set = set()

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
                sql_sqlite, warns = _translate_vfp_view_sql(code) if code else ("", [])
                # Tabla base: buscar en el SQL traducido (o el original si falla)
                src = sql_sqlite or code or ""
                m2 = re.search(r'\bFROM\b\s+([A-Za-z_][A-Za-z0-9_]*)', src, re.I)
                base_tabla = scaffold._slug(m2.group(1)) if m2 else ""
                vistas.append({
                    "name":       oname,
                    "sql_vfp":    code[:3000] if code else "",
                    "sql_sqlite": sql_sqlite,
                    "tabla_base": base_tabla,
                    "warnings":   warns,
                })
                if base_tabla:
                    vista_to_tabla[scaffold._slug(oname)] = base_tabla

    return {
        "relaciones": relaciones,
        "campos": campos_props,
        "stored_procs":   stored_procs[:20],
        "vistas":         vistas[:50],
        "vista_to_tabla": vista_to_tabla,
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
    has_methods = "methods" in fmap
    if has_methods:
        mo, ml, mt = fmap["methods"]
    has_parent = "parent" in fmap
    if has_parent:
        paro, parl, part = fmap["parent"]

    def field_text(rec, off, ln, typ):
        raw = rec[off:off + ln]
        if typ == "M":  # memo: el valor es el nº de bloque (4 bytes LE) en el .sct
            return _read_memo(sct, int.from_bytes(raw[:4], "little"), blocksize)
        return raw.decode("latin-1", "replace").replace("\x00", "").strip()

    def parse_props(text):
        """De un memo 'properties' saca ControlSource, Caption y Top."""
        cs = re.search(r'ControlSource\s*=\s*([^\r\n]+)', text, re.I)
        cap = re.search(r'Caption\s*=\s*([^\r\n]+)', text, re.I)
        top = re.search(r'(?:^|\n)\s*Top\s*=\s*([\d.]+)', text, re.I)
        src = cs.group(1).strip().strip('"').strip() if cs else ""
        label = cap.group(1).strip().strip('"').strip() if cap else ""
        try:
            tval = float(top.group(1)) if top else 0.0
        except ValueError:
            tval = 0.0
        return src, label, tval

    counts = {}
    controls = []
    campos = []          # controles atados a campos reales (ControlSource)
    tabla_freq = {}      # frecuencia de tabla referenciada -> para inferir la tabla
    metodos = []         # eventos con código de negocio
    grid_names = set()   # nombres de objetos grid (para resolver columnas)
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
        # Registrar grids para resolver columnas hijas
        if baseclass == "grid":
            grid_names.add(objname.lower())
        # Extraer parent (para resolver columnas de grids)
        parent_name = ""
        if has_parent:
            try:
                parent_name = field_text(rec, paro, parl, part).strip()
            except Exception:
                parent_name = ""
        # Layout real: ControlSource (atadura a campo) y Caption (etiqueta).
        if has_props:
            try:
                src, label, top = parse_props(field_text(rec, po, pl, pt))
            except Exception:
                src, label, top = "", "", 0.0
            if "." in src:
                pref, _, fld = src.partition(".")
                pref, fld = pref.strip().lower(), fld.strip()
                if pref and fld and pref not in ("thisform", "this", "_screen"):
                    # Si el padre es un grid → campo de subgrilla (orden al final)
                    if has_parent and parent_name.lower() in grid_names:
                        tabla_freq[pref] = tabla_freq.get(pref, 0) + 1
                        if len(campos) < MAX_FIELDS_PER_TABLE:
                            campos.append({"field": fld, "label": label or fld,
                                           "top": top + 1000, "en_grid": True})
                    else:
                        tabla_freq[pref] = tabla_freq.get(pref, 0) + 1
                        if len(campos) < MAX_FIELDS_PER_TABLE:
                            campos.append({"field": fld, "label": label or fld, "top": top})
        # Extraer métodos/eventos con código de negocio
        if has_methods:
            try:
                mcode = field_text(rec, mo, ml, mt).strip()
            except Exception:
                mcode = ""
            if mcode and len(mcode) > 20:
                non_ascii = sum(1 for c in mcode if ord(c) > 127) / max(len(mcode), 1)
                if non_ascii < 0.2 and (not mcode or ord(mcode[0]) >= 32):
                    eventos = re.findall(r'PROCEDURE\s+(\w+)', mcode, re.I)
                    if eventos and len(metodos) < 20:
                        metodos.append({
                            "obj": objname,
                            "eventos": eventos[:6],
                            "codigo": mcode[:800],
                        })
    if not counts:
        return None
    tabla = max(tabla_freq, key=tabla_freq.get) if tabla_freq else ""
    return {"counts": counts, "controls": controls, "total": sum(counts.values()),
            "campos": campos, "tabla": tabla, "metodos": metodos}


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


def analyze_zip(source):
    """Lee el ZIP y devuelve un resumen real del sistema legacy.
    source puede ser bytes (compat.) o una ruta a un archivo en disco."""
    zf = zipfile.ZipFile(source if isinstance(source, str) else io.BytesIO(source))
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
    forms_detail   = []  # controles reales de los formularios .scx
    reports_detail = []  # metadatos parseados de los .frx
    seen_reports   = set()
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
        elif cat == "reports" and ext == ".frx":
            # Sin cap: listar TODOS los reportes del sistema
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
                        "metodos": info.get("metodos", []),
                    })
            except Exception:
                pass

        # Reportes: parsear .frx (DBF) + .frt (memo) para obtener data source y columnas.
        # Igual que .scx: se usa read_dbf_records + el memo .frt para campos memo.
        if ext == ".frx":
            stem = os.path.splitext(base)[0]
            if stem.lower() not in seen_reports:
                seen_reports.add(stem.lower())
                try:
                    frx_bytes = zf.read(name)
                    frt_key   = (os.path.splitext(name)[0] + ".frt").lower()
                    frt_real  = lower_map.get(frt_key)
                    frt_bytes = zf.read(frt_real) if frt_real else b""
                    info = parse_frx(frx_bytes, frt_bytes)
                    if info is not None:
                        info["name"] = stem
                        reports_detail.append(info)
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

    # Clases VFP (.vcx + .vct): extraer métodos como muestras adicionales de código.
    # Son reutilizables y contienen lógica de negocio en sus event handlers.
    vcx_names = [n for n in sorted(names) if n.lower().endswith(".vcx")]
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
        except Exception:
            pass

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

    # Construir mapa global vista→tabla desde todos los .dbc
    _vista_to_tabla = {}
    for db in databases:
        _vista_to_tabla.update(db.get("vista_to_tabla") or {})

    # Resolver el campo "tabla" de cada form usando el mapa de vistas
    for fd in forms_detail:
        t = scaffold._slug(fd.get("tabla", ""))
        if t and t in _vista_to_tabla:
            fd["tabla_real"] = _vista_to_tabla[t]
            fd["tabla_es_vista"] = True
        else:
            fd["tabla_real"] = t
            fd["tabla_es_vista"] = False

    return {
        "total_files": len(names),
        "size_mb": round(len(raw_bytes) / 1024 / 1024, 1),
        "by_ext": by_ext,
        "counts": counts,
        "tables": tables,
        "forms": forms,
        "forms_detail": forms_detail,
        "reports": reports,
        "reports_detail": reports_detail,
        "programs": sorted(set(programs))[:MAX_NAME_LIST],
        "samples": samples,
        "menus": menus,
        "project": project,
        "databases": databases,
        "vista_to_tabla": _vista_to_tabla,
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
        elif self.path == "/api/estado":
            est = _cargar_estado()
            # Informar si el ZIP sigue en caché (si no está, hay que re-subir)
            if est:
                est["zip_en_cache"] = bool(_get_zip_path(est.get("zip_token")))
            self.send_json(200, est or {})

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

        if self.path == "/api/estado/reset":
            try:
                if os.path.exists(ESTADO_FILE):
                    os.remove(ESTADO_FILE)
                self.send_json(200, {"ok": True})
            except Exception as e:
                self.send_json(500, {"ok": False, "error": str(e)})
            return

        if self.path.startswith("/api/unidad/"):
            nombre = self.path[len("/api/unidad/"):]
            if nombre not in UNIDADES_ORDEN:
                self.send_json(400, {"error": f"Unidad desconocida: {nombre}"})
                return
            est = _cargar_estado()
            if not est:
                self.send_json(400, {"error": "No hay proyecto activo. Subí un ZIP primero."})
                return
            zip_token = est.get("zip_token")
            zip_path = _get_zip_path(zip_token)
            if not zip_path:
                self.send_json(400, {
                    "error": "El ZIP no está disponible en disco. Volvé a subir el archivo ZIP.",
                    "zip_en_cache": False,
                })
                return
            try:
                body = json.loads(self.read_body() or b"{}") if int(
                    self.headers.get("Content-Length", 0)) else {}
            except Exception:
                body = {}
            est["unidades"][nombre]["estado"] = "corriendo"
            _guardar_estado(est)
            try:
                resultado = _procesar_unidad(nombre, zip_path, est, body)
                _guardar_estado(est)
                self.send_json(200, resultado)
            except Exception as e:
                est["unidades"][nombre]["estado"] = "error"
                est["unidades"][nombre]["error"] = str(e)
                _guardar_estado(est)
                self.send_json(500, {"error": str(e)})
            return

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
                token = _cache_zip(raw)   # guarda en disco antes de analizar
                del raw                   # libera RAM: el ZIP ya está en disco
                zip_path = _get_zip_path(token)
                info = analyze_zip(zip_path)
                info["zip_token"] = token
                print(f"  ✓ ZIP leído: {info['total_files']} archivos, "
                      f"{len(info['tables'])} tablas analizadas")
                # Inicializar (o reiniciar) el estado persistente del proyecto.
                nombre_zip = info.get("nombre") or info.get("name") or "sistema.zip"
                est = _estado_nuevo(nombre_zip, token)
                est["datos"]["zipinfo"] = info   # inventario completo disponible para unidades
                _guardar_estado(est)
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
            # Usar el ZIP desde disco (no desde RAM).
            zip_path = _get_zip_path(payload.get("zip_token"))
            assets = {}
            # Usar datos ya procesados por las unidades del estado.
            est_actual = _cargar_estado()
            datos_est = (est_actual or {}).get("datos", {})
            datos_bases = datos_est.get("bases") or {}
            if datos_bases.get("seed") and datos_bases.get("tablas_ok"):
                payload.setdefault("seed",    datos_bases["seed"])
                payload.setdefault("indexes", datos_bases.get("indexes", {}))
                print(f"  ✓ Datos del estado: {datos_bases.get('total_rows',0)} registros, "
                      f"{datos_bases.get('total_idx',0)} índices (unidad 'bases')")
            elif zip_path:
                try:
                    seed, indexes, total = build_seed_from_zip(zip_path,
                                                               payload.get("inventory") or {})
                    payload["seed"] = seed
                    payload["indexes"] = indexes
                    print(f"  ✓ Datos a importar: {total} registros en {len(seed)} tablas, "
                          f"{sum(len(v) for v in indexes.values())} índices")
                except Exception as e:
                    print(f"  ! No se pudieron importar los datos: {e}")
            if zip_path:
                try:
                    assets = extract_assets_from_zip(zip_path)
                    if assets:
                        print(f"  ✓ Imágenes a incluir: {len(assets)}")
                except Exception as e:
                    print(f"  ! No se pudieron extraer las imágenes: {e}")
            # Enriquecer el inventario con pantallas y reportes procesados por las unidades
            # (que pueden tener cobertura total, superando los caps del analyze_zip inicial).
            inv = payload.get("inventory") or {}
            datos_pant = datos_est.get("pantallas") or {}
            if datos_pant.get("forms_detail"):
                inv["forms_detail"] = datos_pant["forms_detail"]
                print(f"  ✓ Pantallas del estado: {len(datos_pant['forms_detail'])} formularios")
            datos_rep = datos_est.get("reportes") or {}
            if datos_rep.get("reports_detail"):
                inv["reports_detail"] = datos_rep["reports_detail"]
                print(f"  ✓ Reportes del estado: {len(datos_rep['reports_detail'])} reportes")
            if inv:
                payload["inventory"] = inv
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
