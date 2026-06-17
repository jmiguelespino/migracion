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
import sys
import urllib.error
import urllib.request
import zipfile

API_KEY = ""  # Se configura desde el navegador
PORT = 8080

# Modo gratuito (sin API key): usa un modelo local vía Ollama (https://ollama.com).
# No requiere clave ni gasta tokens; corre 100% en la máquina del usuario.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder")

# Límites para no saturar el prompt (ni la memoria) con sistemas enormes.
MAX_TABLES = 60          # cuántas tablas .dbf describir con su estructura
MAX_FIELDS_PER_TABLE = 60
MAX_NAME_LIST = 80       # cuántos nombres de forms/reportes listar
MAX_SAMPLES = 8          # cuántos .prg pequeños incluir como muestra de código
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
    ".cbl": "programs", ".cob": "programs", ".cpy": "copybooks",
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

    def field_text(rec, off, ln, typ):
        raw = rec[off:off + ln]
        if typ == "M":  # memo: el valor es el nº de bloque (4 bytes LE) en el .sct
            return _read_memo(sct, int.from_bytes(raw[:4], "little"), blocksize)
        return raw.decode("latin-1", "replace").replace("\x00", "").strip()

    counts = {}
    controls = []
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
    if not counts:
        return None
    return {"counts": counts, "controls": controls, "total": sum(counts.values())}


def analyze_zip(raw_bytes):
    """Lee el ZIP en memoria y devuelve un resumen real del sistema legacy."""
    zf = zipfile.ZipFile(io.BytesIO(raw_bytes))
    names = [n for n in zf.namelist() if not n.endswith("/")]
    lower_map = {n.lower(): n for n in names}  # para ubicar el .sct de cada .scx

    by_ext = {}
    counts = {}
    forms, reports, programs = [], [], []
    tables = []
    seen_tables = set()  # evita tablas .dbf duplicadas (mismo nombre en varias carpetas)
    forms_detail = []    # controles reales de los formularios .scx
    samples = []

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
        tname = os.path.splitext(base)[0].lower()
        if ext == ".dbf" and len(tables) < MAX_TABLES and tname not in seen_tables:
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
                    })
            except Exception:
                pass

        # Muestras de código fuente real (programas pequeños).
        if ext in (".prg", ".cbl", ".cob") and len(samples) < MAX_SAMPLES:
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
    }


def safe_name(name, default):
    """Evita rutas peligrosas: deja solo el nombre de archivo."""
    name = os.path.basename(str(name or "")).strip().replace("\\", "").replace("/", "")
    return name or default


def test_filename(target):
    """Nombre/ubicación del archivo de tests según la tecnología destino."""
    t = (target or "").lower()
    if "python" in t or "django" in t or "fastapi" in t:
        return "tests/test_fase.py"
    if "node" in t or "express" in t:
        return "tests/fase.test.js"
    if ".net" in t or "c#" in t or "blazor" in t:
        return "tests/FaseTests.cs"
    if "java" in t or "spring" in t:
        return "tests/FaseTests.java"
    return "tests/tests.txt"


def build_readme(phase, d, source, target):
    """Arma el README.md de la fase con explicación e instrucciones."""
    num = phase.get("numero", "")
    titulo = phase.get("titulo", "Fase")
    out = [f"# Fase {num}: {titulo}", "", f"**Migración:** {source} → {target}"]
    if phase.get("descripcion"):
        out += ["", phase["descripcion"]]
    if d.get("explicacion"):
        out += ["", "## Explicación", "", d["explicacion"]]
    archivos = d.get("archivos") or []
    if archivos:
        out += ["", "## Archivos generados (carpeta `src/`)", ""]
        for f in archivos:
            out.append(f"- `{f.get('nombre', '')}` — {f.get('descripcion', '')}")
    if d.get("tests"):
        out += ["", f"Incluye tests automatizados en `{test_filename(target)}`."]
    if d.get("instrucciones"):
        out += ["", "## Instrucciones de instalación", "", d["instrucciones"]]
    if d.get("interfaz_descripcion"):
        out += ["", "## Interfaz", "", d["interfaz_descripcion"]]
    out += ["", "---", "_Generado por LegacyMigrator._"]
    return "\n".join(out)


def build_phase_zip(payload):
    """Construye en memoria el ZIP descargable de una fase generada."""
    phase = payload.get("phase", {}) or {}
    d = payload.get("data", {}) or {}
    source = payload.get("source", "")
    target = payload.get("target", "")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        used = set()
        for f in d.get("archivos") or []:
            name = safe_name(f.get("nombre"), "archivo.txt")
            stem, ext = os.path.splitext(name)
            i = 1
            while name in used:  # evita colisiones de nombres
                name = f"{stem}_{i}{ext}"
                i += 1
            used.add(name)
            z.writestr(f"src/{name}", f.get("codigo") or "")
        if d.get("tests"):
            z.writestr(test_filename(target), d["tests"])
        z.writestr("README.md", build_readme(phase, d, source, target))
    return buf.getvalue()


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
                print(f"  ✓ ZIP leído: {info['total_files']} archivos, "
                      f"{len(info['tables'])} tablas analizadas")
                self.send_json(200, info)
            except zipfile.BadZipFile:
                self.send_json(400, {"error": {"message": "El archivo no es un ZIP válido"}})
            except Exception as e:
                self.send_json(500, {"error": {"message": f"Error al leer el ZIP: {e}"}})
            return

        if self.path == "/api/zip":
            try:
                payload = json.loads(self.read_body() or b"{}")
            except Exception:
                self.send_json(400, {"error": {"message": "JSON inválido"}})
                return
            try:
                data = build_phase_zip(payload)
            except Exception as e:
                self.send_json(500, {"error": {"message": f"Error al armar el ZIP: {e}"}})
                return
            fname = safe_name(payload.get("filename"), "fase.zip")
            self.send_response(200)
            self.send_cors()
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
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
            req_body = json.dumps({
                "model": model,
                "prompt": prompt,
                "stream": False,
                # temperatura baja: queremos JSON/código determinista, no creatividad.
                "options": {"num_predict": num_predict, "temperature": 0.2},
            }).encode("utf-8")
            req = urllib.request.Request(
                OLLAMA_URL + "/api/generate",
                data=req_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                # Los modelos locales pueden ser lentos: damos margen amplio.
                with urllib.request.urlopen(req, timeout=600) as resp:
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
            except urllib.error.URLError:
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

    server = http.server.HTTPServer(("localhost", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\n  Servidor cerrado. ¡Hasta luego!\n")
        sys.exit(0)
