#!/usr/bin/env python3
"""
Generador determinístico de la app migrada (cobertura total).

A partir del inventario REAL leído del ZIP (tablas, formularios, menús,
reportes) arma una aplicación moderna y ejecutable —FastAPI + SPA en un solo
proceso, sin Node— que EXPONE TODAS las utilidades del sistema legacy:

- cada tabla .dbf  -> un ABM (alta/baja/modificación/listado) sobre SQLite
- los menús .mpr   -> la navegación de la app
- cada reporte .frx -> una vista de listado/consulta
- un índice COBERTURA.md que enumera todo y su estado

No usa IA: la cobertura es garantizada y reproducible. La IA queda para una
segunda etapa (enriquecer pantallas y portar la lógica de los .prg).
"""

import io
import json
import os
import re
import zipfile

# Tipo de campo DBF -> (tipo SQLite, tipo de input HTML)
DBF_SQL = {
    "C": ("TEXT", "text"), "M": ("TEXT", "textarea"), "G": ("TEXT", "text"),
    "P": ("TEXT", "text"), "N": ("REAL", "number"), "F": ("REAL", "number"),
    "B": ("REAL", "number"), "Y": ("REAL", "number"), "I": ("INTEGER", "number"),
    "L": ("INTEGER", "checkbox"), "D": ("TEXT", "date"), "T": ("TEXT", "date"),
}


def _slug(s):
    s = re.sub(r"[^a-zA-Z0-9_]", "_", str(s or "").strip())
    return re.sub(r"_+", "_", s).strip("_").lower() or "tabla"


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


_DO_FORM_RE = re.compile(r'\bDO\s+FORM\s+(\w+)', re.IGNORECASE)


def _menu_to_tabla(texto, accion, table_index):
    """Resuelve ítem de menú → key de tabla (o None).
    Prueba primero la etiqueta visible y luego el nombre del form de la acción.
    Usa coincidencia parcial para cubrir prefijos/sufijos (ej. 'Recetas' ↔ 'rececab'
    si el nombre empieza con la raíz normalizada)."""
    candidates = [_norm(texto)]
    m = _DO_FORM_RE.search(accion or "")
    if m:
        candidates.append(_norm(m.group(1)))
    for cand in candidates:
        if not cand:
            continue
        for tn, tk in table_index.items():
            if tn and (cand == tn or tn.startswith(cand) or cand.startswith(tn)):
                return tk
    return None


def _sanitize_validaciones(items, field_names):
    """Valida/limpia las reglas ejecutables que propone la IA. Solo deja ops
    conocidas, campos reales y valores con el tipo correcto (anti-inyección)."""
    OPS = {"min", "max", "rango", "regex"}
    out = []
    for v in items or []:
        if not isinstance(v, dict):
            continue
        campo = _slug(v.get("campo"))
        op = str(v.get("op", "")).lower().strip()
        if campo not in field_names or op not in OPS:
            continue
        rule = {"campo": campo, "op": op, "mensaje": str(v.get("mensaje") or "")[:200]}
        val = v.get("valor")
        if op in ("min", "max"):
            try:
                rule["valor"] = float(val)
            except (TypeError, ValueError):
                continue
        elif op == "rango":
            if not (isinstance(val, (list, tuple)) and len(val) == 2):
                continue
            try:
                rule["valor"] = [float(val[0]), float(val[1])]
            except (TypeError, ValueError):
                continue
        elif op == "regex":
            pat = str(val or "")[:200]
            try:
                re.compile(pat)
            except re.error:
                continue
            if not pat:
                continue
            rule["valor"] = pat
        out.append(rule)
        if len(out) >= 30:
            break
    return out


def _apply_enrich(tabla, spec):
    """Fusiona el JSON de enriquecimiento de la IA sobre una tabla (in place)."""
    if not isinstance(spec, dict):
        return
    if spec.get("titulo"):
        tabla["titulo"] = str(spec["titulo"])[:80]
    if spec.get("descripcion"):
        tabla["descripcion"] = str(spec["descripcion"])[:400]
    reglas = spec.get("reglas")
    if isinstance(reglas, list):
        tabla["reglas"] = [str(r)[:300] for r in reglas if r][:20]
    # Reglas EJECUTABLES (min/max/rango/regex) — saneadas.
    field_names = {c["name"] for c in tabla["campos"]}
    tabla["validaciones"] = _sanitize_validaciones(spec.get("validaciones"), field_names)
    # Mapa por nombre de campo (normalizado) -> mejoras.
    by_name = {}
    for c in (spec.get("campos") or []):
        if isinstance(c, dict) and c.get("name"):
            by_name[_slug(c["name"])] = c
    for campo in tabla["campos"]:
        c = by_name.get(campo["name"])
        if not c:
            continue
        if c.get("label"):
            campo["label"] = str(c["label"])[:60]
        if c.get("ayuda"):
            campo["ayuda"] = str(c["ayuda"])[:200]
        campo["requerido"] = bool(c.get("requerido"))
    tabla["enriquecido"] = True


def _forms_index(inventory):
    """Índice tabla -> {etiquetas reales, orden} a partir del layout de los .scx
    (ControlSource/Caption/Top). Permite que el ABM se parezca al formulario."""
    idx = {}
    for f in inventory.get("forms_detail", []):
        tabla = f.get("tabla_real") or f.get("tabla")
        campos = f.get("campos") or []
        if not tabla or not campos:
            continue
        d = idx.setdefault(_slug(tabla), {"labels": {}, "order": {}})
        for rank, c in enumerate(sorted(campos, key=lambda x: x.get("top", 0))):
            fld = _slug(c.get("field"))
            if not fld:
                continue
            if c.get("label"):
                d["labels"].setdefault(fld, str(c["label"])[:60])
            d["order"].setdefault(fld, rank)
    return idx


def build_meta(inventory, title, enrich=None):
    """Arma la metadata para el backend y la SPA a partir del inventario.

    `enrich` (opcional) = {claveTabla: {titulo, descripcion, campos[], reglas[]}}
    producido por la IA; se fusiona para mejorar etiquetas, obligatorios,
    ayudas y mostrar las reglas de negocio del sistema original.
    """
    enrich = enrich or {}
    forms_index = _forms_index(inventory)

    # Datos del .dbc: relaciones entre tablas y propiedades persistentes de campos.
    dbs = inventory.get("databases") or []
    campo_props_dbc = {}   # {tabla_key: {campo_key: {caption, defaultvalue, ...}}}
    relations = []         # [{parent_table, parent_field, child_table, child_field}]
    for db in dbs:
        relations.extend(db.get("relaciones") or [])
        for tkey, flds in (db.get("campos") or {}).items():
            for fkey, props in flds.items():
                campo_props_dbc.setdefault(tkey, {})[fkey] = props
    # FK lookup: {child_table_slug: {child_field_slug: {table, field}}}
    fk_map = {}
    for rel in relations:
        ct = rel.get("child_table")
        cf = rel.get("child_field")
        pt = rel.get("parent_table")
        pf = rel.get("parent_field")
        if ct and cf and pt:
            fk_map.setdefault(ct, {})[cf] = {"table": pt, "field": pf or "id"}

    tablas, tables_sql, seen = [], {}, set()
    for t in inventory.get("tables", []):
        key = _slug(t.get("name"))
        if not key or key in seen:
            continue
        seen.add(key)
        campos, cols = [], []
        for f in (t.get("fields") or []):
            cn = _slug(f.get("name"))
            if not cn or cn == "id" or cn in [c[0] for c in cols]:
                continue
            ftype = f.get("type", "C")
            sql, inp = DBF_SQL.get(ftype, ("TEXT", "text"))
            maxlen = int(f.get("len") or 0) if ftype in ("C", "M", "G", "P") else 0
            campos.append({"name": cn, "label": f.get("name"), "type": ftype,
                           "input": inp, "requerido": False, "ayuda": "", "maxlen": maxlen})
            cols.append([cn, sql])
        if not cols:
            continue
        tables_sql[key] = cols
        tabla = {
            "key": key, "name": t.get("name"), "registros": t.get("records", 0),
            "titulo": t.get("name"), "descripcion": "", "reglas": [], "validaciones": [],
            "enriquecido": False, "origen_formulario": False, "campos": campos,
        }
        # Layout real del formulario .scx: etiquetas (Caption) y orden (Top).
        fi = forms_index.get(key)
        scx_labeled = set()
        if fi:
            for c in campos:
                if c["name"] in fi["labels"]:
                    c["label"] = fi["labels"][c["name"]]
                    scx_labeled.add(c["name"])
            campos.sort(key=lambda c: fi["order"].get(c["name"], 999))
            tabla["origen_formulario"] = True
        # Etiquetas del .dbc (Caption) donde el .scx no aportó ninguna.
        dbc_fields = campo_props_dbc.get(key, {})
        for c in campos:
            if c["name"] not in scx_labeled:
                dbc = dbc_fields.get(c["name"], {})
                if dbc.get("caption"):
                    c["label"] = str(dbc["caption"])[:60]
        # La IA (si se usó) puede refinar título/etiquetas/reglas por encima.
        _apply_enrich(tabla, enrich.get(key))
        tablas.append(tabla)

    # Relaciones del .dbc: marcar campos FK (solo si la tabla padre existe en la app).
    for tabla in tablas:
        fks = fk_map.get(tabla["key"], {})
        for campo in tabla["campos"]:
            if campo["name"] in fks:
                fk = fks[campo["name"]]
                if fk["table"] in tables_sql:
                    campo["fk"] = fk

    # Reportes -> intentamos asociarlos a una tabla por nombre.
    table_index = {_norm(x["name"]): x["key"] for x in tablas}
    reportes = []
    for r in inventory.get("reports", []):
        stem = re.sub(r"\.\w+$", "", str(r))
        n = _norm(stem)
        match = None
        for tn, tk in table_index.items():
            if tn and (tn in n or n in tn):
                match = tk
                break
        reportes.append({"name": stem, "tabla": match})

    formularios = [re.sub(r"\.\w+$", "", str(f)) for f in inventory.get("forms", [])]

    # Menús: resolver cada ítem a una tabla (clave "tabla") para que la SPA
    # pueda navegar directamente al ABM correspondiente.
    menus = []
    for grp in (inventory.get("menus", []) or []):
        items = []
        for it in (grp.get("items", []) or []):
            tabla = _menu_to_tabla(it.get("texto", ""), it.get("accion", ""), table_index)
            items.append({**it, "tabla": tabla})
        menus.append({**grp, "items": items})

    proyecto = inventory.get("project") or None

    relaciones_activas = sum(1 for t in tablas for c in t["campos"] if c.get("fk"))
    sp_count = sum(len(db.get("stored_procs") or []) for db in dbs)

    meta = {
        "titulo": title,
        "tablas": tablas,
        "menus": menus,
        "reportes": reportes,
        "formularios": formularios,
        "proyecto": proyecto,
        "stats": {
            "tablas": len(tablas), "formularios": len(formularios),
            "reportes": len(reportes),
            "items_menu": sum(len(m.get("items", [])) for m in menus),
            "enriquecidas": sum(1 for t in tablas if t.get("enriquecido")),
            "archivos_proyecto": len(proyecto["archivos"]) if proyecto else 0,
            "programa_principal": (proyecto or {}).get("principal", ""),
            "relaciones": relaciones_activas,
            "stored_procs": sp_count,
        },
    }
    return meta, tables_sql


def _coverage_md(meta):
    s = meta["stats"]
    out = [
        f"# Cobertura de la migración — {meta['titulo']}",
        "",
        "App generada que **expone las mismas utilidades** del sistema legacy.",
        "",
        "| Utilidad | Cantidad | Estado |",
        "|----------|---------:|--------|",
        f"| Tablas (ABM funcional) | {s['tablas']} | ✅ generado |",
        f"| · de ellas, enriquecidas con IA | {s.get('enriquecidas', 0)} | ✨ etiquetas, obligatorios y reglas |",
        f"| Registros importados | {s.get('registros_importados', 0)} | ✅ datos reales del .dbf |",
        f"| Índices recreados | {s.get('indices', 0)} | 🟡 best-effort (.cdx/.idx) |",
        f"| Imágenes incluidas | {s.get('imagenes', 0)} | ✅ en `web/assets/` |",
        f"| Campos mostrados como imagen | {s.get('campos_imagen', 0)} | ✅ render `<img>` |",
        f"| Menús (navegación) | {len(meta['menus'])} | ✅ generado |",
        f"| Reportes (vista/consulta) | {s['reportes']} | ✅ generado |",
        f"| Formularios originales | {s['formularios']} | 🟡 listados |",
        f"| Relaciones FK (del .dbc) | {s.get('relaciones', 0)} | ✅ generado |",
        f"| Stored procedures | {s.get('stored_procs', 0)} | 🟡 informativo |",
        "",
        "## Tablas → ABM",
        "",
    ]
    idxmap = meta.get("indexes") or {}

    def _fmt_idx(defs):
        parts = []
        for cols in defs:
            cols = cols if isinstance(cols, list) else [cols]
            parts.append("(" + "+".join(cols) + ")" if len(cols) > 1 else cols[0])
        return ", ".join(parts)

    for t in meta["tablas"]:
        mark = " ✨ IA" if t.get("enriquecido") else ""
        reglas = f", {len(t['reglas'])} reglas" if t.get("reglas") else ""
        idx = f", índices: {_fmt_idx(idxmap[t['key']])}" if idxmap.get(t["key"]) else ""
        out.append(f"- **{t['name']}** ({len(t['campos'])} campos, {t['registros']} reg.{reglas}{idx}) → `/#/abm/{t['key']}`{mark}")
    out += ["", "## Reportes"]
    for r in meta["reportes"]:
        dest = f"tabla `{r['tabla']}`" if r["tabla"] else "sin tabla asociada"
        out.append(f"- **{r['name']}** → {dest}")
    out += ["", "## Menús del sistema"]
    for m in meta["menus"]:
        out.append(f"- **{m.get('titulo','')}**: " + ", ".join(i.get("texto", "") for i in m.get("items", [])))

    pj = meta.get("proyecto")
    if pj:
        out += ["", "## Proyecto original (.pjx)",
                f"- **Programa principal:** `{pj.get('principal') or '(no declarado)'}`",
                f"- **Archivos declarados:** {len(pj.get('archivos', []))}", "",
                "| Archivo | Tipo | Excluido |", "|---------|------|:--------:|"]
        for a in pj.get("archivos", [])[:200]:
            out.append(f"| {a['name']} | {a['type_name']} | {'sí' if a['excluido'] else ''} |")

    # Relaciones entre tablas del .dbc
    rel_campos = [(t, c) for t in meta["tablas"] for c in t["campos"] if c.get("fk")]
    if rel_campos:
        out += ["", "## Relaciones entre tablas (.dbc)", ""]
        for t, c in rel_campos:
            fk = c["fk"]
            out.append(
                f"- **{t['name']}.{c.get('label') or c['name']}** → "
                f"**{fk['table']}.{fk['field']}** (FK)")

    out += ["", "---", "_Generado por LegacyMigrator (cobertura total)._"]
    return "\n".join(out)


APP_PY = r'''#!/usr/bin/env python3
"""App migrada — backend FastAPI + SQLite. Generado por LegacyMigrator.

Correr (desde la carpeta del proyecto):
    python -m pip install fastapi uvicorn
    python -m uvicorn backend.app:app --port 8000
Abrir: http://localhost:8000
"""
import io, json, os, re, sqlite3
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "datos.db")
WEB = os.path.join(BASE, "..", "web")

# Los datos (tablas + metadata) se cargan de un JSON aparte para evitar
# problemas de escapado (true/false/null, comillas, backslashes de regex).
with open(os.path.join(BASE, "meta.json"), encoding="utf-8") as _f:
    _DATA = json.load(_f)
TABLES = _DATA["tables"]
META = _DATA["meta"]


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = conn()
    for t, cols in TABLES.items():
        defs = ", ".join('"%s" %s' % (cn, ct) for cn, ct in cols)
        c.execute('CREATE TABLE IF NOT EXISTS "%s" (id INTEGER PRIMARY KEY AUTOINCREMENT, %s)' % (t, defs))
    # Índices del sistema original, derivados de las expresiones de clave de los
    # .cdx/.idx. Cada entrada es una lista de índices; cada índice una lista de
    # columnas (compuesto si tiene más de una). Acepta también el formato viejo
    # (lista de columnas sueltas) por compatibilidad.
    for t, idxdefs in (META.get("indexes") or {}).items():
        if t not in TABLES:
            continue
        valid = {cn for cn, _ in TABLES[t]}
        for i, cols in enumerate(idxdefs):
            cols = cols if isinstance(cols, list) else [cols]
            cols = [c for c in cols if c in valid]
            if not cols:
                continue
            try:
                c.execute('CREATE INDEX IF NOT EXISTS "ix_%s_%d" ON "%s" (%s)' % (
                    t, i, t, ",".join('"%s"' % x for x in cols)))
            except sqlite3.Error:
                pass
    c.commit()
    # Importar los datos reales del sistema legacy una sola vez (si está vacío).
    seed_path = os.path.join(BASE, "seed.json")
    if os.path.exists(seed_path):
        try:
            with open(seed_path, encoding="utf-8") as f:
                seed = json.load(f)
        except (OSError, ValueError):
            seed = {}
        for t, rows in seed.items():
            if t not in TABLES or not rows:
                continue
            if c.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]:
                continue  # ya tiene datos: no re-importamos
            valid = [cn for cn, _ in TABLES[t]]
            for r in rows:
                use = [k for k in valid if k in r]
                if not use:
                    continue
                try:
                    c.execute(
                        'INSERT INTO "%s" (%s) VALUES (%s)' % (
                            t, ",".join('"%s"' % x for x in use), ",".join("?" * len(use))),
                        [r[x] for x in use])
                except sqlite3.Error:
                    pass
        c.commit()
    c.close()


app = FastAPI(title=META.get("titulo", "App migrada"))
init_db()

# Validaciones derivadas del esquema real (.dbf) + lo que aportó la IA.
NUMERIC = {"N", "F", "B", "Y", "I"}
VALID = {t["key"]: {c["name"]: c for c in t["campos"]} for t in META.get("tablas", [])}
VALRULES = {t["key"]: t.get("validaciones", []) for t in META.get("tablas", [])}


def validate(table, data):
    """Lista de errores de validación (vacía si todo OK)."""
    errs = []
    campos = VALID.get(table, {})
    # 1) Validaciones del esquema real (.dbf) + 'requerido' de la IA.
    for name, c in campos.items():
        if name not in data:
            continue
        val = data.get(name)
        empty = val is None or str(val).strip() == ""
        etq = c.get("label") or name
        if c.get("requerido") and empty:
            errs.append("'%s' es obligatorio" % etq)
            continue
        if empty:
            continue
        if c.get("type") in NUMERIC:
            try:
                float(str(val).replace(",", "."))
            except ValueError:
                errs.append("'%s' debe ser numérico" % etq)
        ml = c.get("maxlen") or 0
        if ml and isinstance(val, str) and len(val) > ml:
            errs.append("'%s' admite hasta %d caracteres" % (etq, ml))
    # 2) Reglas de negocio EJECUTABLES (min/max/rango/regex) de la IA.
    for rule in VALRULES.get(table, []):
        name = rule.get("campo")
        if name not in data:
            continue
        val = data.get(name)
        if val is None or str(val).strip() == "":
            continue
        etq = (campos.get(name) or {}).get("label") or name
        op, ref = rule.get("op"), rule.get("valor")
        msg = rule.get("mensaje")
        try:
            if op in ("min", "max", "rango"):
                num = float(str(val).replace(",", "."))
                if op == "min" and num < ref:
                    errs.append(msg or "'%s' debe ser >= %s" % (etq, ref))
                elif op == "max" and num > ref:
                    errs.append(msg or "'%s' debe ser <= %s" % (etq, ref))
                elif op == "rango" and (num < ref[0] or num > ref[1]):
                    errs.append(msg or "'%s' debe estar entre %s y %s" % (etq, ref[0], ref[1]))
            elif op == "regex":
                if not re.search(ref, str(val)):
                    errs.append(msg or "'%s' tiene un formato inválido" % etq)
        except (ValueError, re.error):
            pass
    return errs


@app.get("/api/_meta")
def meta():
    return META


@app.get("/api/_counts")
def counts():
    c = conn()
    out = {}
    for t in TABLES:
        try:
            out[t] = c.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
        except sqlite3.Error:
            out[t] = 0
    c.close()
    return out


@app.get("/api/t/{table}")
def list_rows(table: str, q: str = "", sort: str = "", dir: str = "asc",
              page: int = 1, size: int = 50):
    """Listado con búsqueda, orden y paginación del lado del servidor."""
    if table not in TABLES:
        raise HTTPException(404, "tabla desconocida")
    cols = [cn for cn, _ in TABLES[table]]
    where, params = "", []
    if q:
        where = " WHERE " + " OR ".join('CAST("%s" AS TEXT) LIKE ?' % c for c in cols)
        params = ["%" + q + "%"] * len(cols)
    if sort in cols or sort == "id":
        order = ' ORDER BY "%s" %s' % (sort, "DESC" if str(dir).lower() == "desc" else "ASC")
    else:
        order = " ORDER BY id DESC"
    size = max(1, min(int(size or 50), 500))
    page = max(1, int(page or 1))
    c = conn()
    total = c.execute('SELECT COUNT(*) FROM "%s"%s' % (table, where), params).fetchone()[0]
    rows = [dict(r) for r in c.execute(
        'SELECT * FROM "%s"%s%s LIMIT ? OFFSET ?' % (table, where, order),
        params + [size, (page - 1) * size])]
    c.close()
    return {"rows": rows, "total": total, "page": page, "size": size}


@app.get("/api/t/{table}/export.csv")
def export_csv(table: str):
    if table not in TABLES:
        raise HTTPException(404)
    import csv
    out = io.StringIO()
    cols = ["id"] + [cn for cn, _ in TABLES[table]]
    w = csv.writer(out)
    w.writerow(cols)
    c = conn()
    for r in c.execute('SELECT * FROM "%s" ORDER BY id' % table):
        w.writerow([r[cn] for cn in cols])
    c.close()
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="%s.csv"' % table})


@app.post("/api/t/{table}")
async def create_row(table: str, req: Request):
    if table not in TABLES:
        raise HTTPException(404)
    data = await req.json()
    errs = validate(table, data)
    if errs:
        raise HTTPException(422, "; ".join(errs))
    cols = [cn for cn, _ in TABLES[table]]
    use = [c for c in cols if c in data]
    if not use:
        raise HTTPException(400, "sin datos")
    c = conn()
    cur = c.execute(
        'INSERT INTO "%s" (%s) VALUES (%s)' % (table, ",".join('"%s"' % x for x in use), ",".join("?" * len(use))),
        [data[x] for x in use],
    )
    c.commit()
    rid = cur.lastrowid
    c.close()
    return {"id": rid}


@app.put("/api/t/{table}/{rid}")
async def update_row(table: str, rid: int, req: Request):
    if table not in TABLES:
        raise HTTPException(404)
    data = await req.json()
    errs = validate(table, data)
    if errs:
        raise HTTPException(422, "; ".join(errs))
    cols = [cn for cn, _ in TABLES[table]]
    use = [c for c in cols if c in data]
    if not use:
        raise HTTPException(400, "sin datos")
    c = conn()
    c.execute(
        'UPDATE "%s" SET %s WHERE id=?' % (table, ",".join('"%s"=?' % x for x in use)),
        [data[x] for x in use] + [rid],
    )
    c.commit()
    c.close()
    return {"ok": True}


@app.delete("/api/t/{table}/{rid}")
def delete_row(table: str, rid: int):
    if table not in TABLES:
        raise HTTPException(404)
    c = conn()
    c.execute('DELETE FROM "%s" WHERE id=?' % table, [rid])
    c.commit()
    c.close()
    return {"ok": True}


# SPA estática (debe ir al final para no tapar /api).
app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
'''


INDEX_HTML = r'''<!DOCTYPE html>
<html lang="es" data-theme="light"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>App migrada</title><link rel="stylesheet" href="style.css"></head>
<body>
<header>
  <button id="burger" class="icon" title="Menú" aria-label="Menú">☰</button>
  <b id="apptitle">App migrada</b>
  <span id="stats" class="muted"></span>
  <button id="theme" class="icon" title="Tema claro/oscuro" aria-label="Tema">🌙</button>
</header>
<div class="layout">
  <nav id="nav"></nav>
  <div id="backdrop"></div>
  <main id="main"></main>
</div>
<script src="app.js"></script>
</body></html>
'''


STYLE_CSS = r''':root{
  --bg:#f4f5f7; --panel:#ffffff; --panel2:#fafafa; --text:#1a1d21; --muted:#6b7280;
  --border:#e5e7eb; --brand:#5b54d6; --brand-d:#4239b8; --brand-bg:#eeedfe;
  --accent:#0ea5a3; --danger:#dc2626; --shadow:0 1px 3px rgba(0,0,0,.06),0 1px 2px rgba(0,0,0,.04);
}
[data-theme=dark]{
  --bg:#0f1216; --panel:#171b21; --panel2:#1d222a; --text:#e6e8eb; --muted:#9aa3af;
  --border:#2a3039; --brand:#8b84ff; --brand-d:#6d64f0; --brand-bg:#21264f;
  --accent:#2dd4bf; --danger:#f87171; --shadow:0 1px 3px rgba(0,0,0,.4);
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;color:var(--text);background:var(--bg);font-size:14px}
header{background:var(--panel);border-bottom:1px solid var(--border);padding:0 16px;height:54px;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:20}
header b{font-size:16px;color:var(--brand)}
header #stats{flex:1;font-size:12px}
.icon{background:transparent;border:1px solid transparent;color:var(--text);font-size:18px;cursor:pointer;border-radius:8px;padding:4px 9px;line-height:1}
.icon:hover{background:var(--panel2);border-color:var(--border)}
#burger{display:none}
.layout{display:flex;min-height:calc(100vh - 54px)}
nav{width:260px;background:var(--panel);border-right:1px solid var(--border);overflow:auto;padding:10px;flex-shrink:0;position:sticky;top:54px;height:calc(100vh - 54px)}
nav .grp{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:700;margin:14px 8px 5px}
nav a{display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:8px;cursor:pointer;font-size:13px;color:var(--text);text-decoration:none}
nav a:hover{background:var(--panel2)}
nav a.active{background:var(--brand-bg);color:var(--brand-d);font-weight:600}
nav a .cnt{margin-left:auto;font-size:11px;color:var(--muted);background:var(--panel2);border-radius:10px;padding:1px 7px}
#backdrop{display:none}
main{flex:1;overflow:auto;padding:22px;max-width:100%}
h1{font-size:22px;margin-bottom:4px}
h2{font-size:19px;margin-bottom:4px}
.sub{color:var(--muted);margin-bottom:16px}
.muted{color:var(--muted)}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:14px 0}
.toolbar .sp{flex:1}
input.search{padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:var(--panel);color:var(--text);font-size:13px;min-width:200px}
.tablewrap{background:var(--panel);border:1px solid var(--border);border-radius:12px;box-shadow:var(--shadow);overflow:auto}
table{border-collapse:collapse;width:100%}
th,td{padding:9px 12px;border-bottom:1px solid var(--border);font-size:13px;text-align:left;white-space:nowrap}
th{background:var(--panel2);font-weight:600;position:sticky;top:0;cursor:pointer;user-select:none}
th:hover{color:var(--brand)}
th .ar{font-size:10px;color:var(--brand)}
tbody tr:hover{background:var(--panel2)}
td.actions{text-align:right;white-space:nowrap}
img.thumb{max-width:72px;max-height:54px;border-radius:6px;border:1px solid var(--border);vertical-align:middle;object-fit:contain}
button{background:var(--brand);color:#fff;border:none;border-radius:8px;padding:8px 14px;cursor:pointer;font-weight:600;font-size:13px}
button:hover{background:var(--brand-d)}
button.sec{background:var(--panel);color:var(--brand);border:1px solid var(--border)}
button.sec:hover{background:var(--panel2)}
button.del{background:var(--danger)}
button.sm{padding:5px 9px;font-size:12px}
.pager{display:flex;gap:8px;align-items:center;justify-content:flex-end;margin-top:12px;flex-wrap:wrap}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin-bottom:20px}
.metric{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px;box-shadow:var(--shadow);text-decoration:none;color:var(--text);display:block}
.metric:hover{border-color:var(--brand)}
.metric .n{font-size:26px;font-weight:700;color:var(--brand)}
.metric .l{font-size:12px;color:var(--muted);margin-top:2px}
.bars{display:flex;flex-direction:column;gap:7px}
.bar{display:grid;grid-template-columns:160px 1fr 70px;gap:10px;align-items:center;font-size:12px}
.bar .track{background:var(--panel2);border-radius:6px;height:18px;overflow:hidden}
.bar .fill{background:linear-gradient(90deg,var(--brand),var(--accent));height:100%;border-radius:6px}
.bar a{color:var(--text);text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar a:hover{color:var(--brand)}
form.abm{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:18px;margin-bottom:16px;display:grid;grid-template-columns:1fr 1fr;gap:14px;box-shadow:var(--shadow)}
form.abm label{display:flex;flex-direction:column;font-size:12px;color:var(--muted);gap:4px;font-weight:600}
form.abm input,form.abm textarea,form.abm select{padding:8px 10px;border:1px solid var(--border);border-radius:8px;font-size:13px;background:var(--panel2);color:var(--text);font-weight:400}
form.abm input:focus,form.abm textarea:focus,form.abm select:focus{outline:2px solid var(--brand-bg);border-color:var(--brand)}
form.abm input.bad{border-color:var(--danger)}
form.abm .err{color:var(--danger);font-size:11px;min-height:0}
form.abm .full{grid-column:1 / -1;display:flex;gap:8px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:14px;box-shadow:var(--shadow)}
.card.rules{border-left:3px solid var(--accent)}
.tag{display:inline-block;background:var(--brand-bg);color:var(--brand-d);border-radius:6px;padding:2px 8px;font-size:11px;margin:2px 4px 2px 0}
@media(max-width:860px){
  #burger{display:inline-block}
  nav{position:fixed;left:-280px;top:54px;z-index:30;transition:left .2s;box-shadow:var(--shadow)}
  nav.open{left:0}
  #backdrop.show{display:block;position:fixed;inset:54px 0 0 0;background:rgba(0,0,0,.4);z-index:25}
  form.abm{grid-template-columns:1fr}
}
'''


APP_JS = r'''let META = null, COUNTS = {};
const $ = (s) => document.querySelector(s);
// Estado de la vista de cada tabla (búsqueda/orden/página).
const VS = {};
// Caché de datos de tablas padre para dropdowns FK.
const FK_CACHE = {};
async function loadFkOptions(table) {
  if (FK_CACHE[table]) return FK_CACHE[table];
  try { FK_CACHE[table] = ((await (await fetch(`/api/t/${table}?size=1000`)).json()).rows) || []; }
  catch (_) { FK_CACHE[table] = []; }
  return FK_CACHE[table];
}

async function boot() {
  initTheme();
  META = await (await fetch('/api/_meta')).json();
  try { COUNTS = await (await fetch('/api/_counts')).json(); } catch (_) { COUNTS = {}; }
  $('#apptitle').textContent = META.titulo || 'App migrada';
  const s = META.stats || {};
  $('#stats').textContent = `${s.tablas||0} tablas · ${s.registros_importados||0} registros · ${s.reportes||0} reportes`;
  buildNav();
  $('#burger').onclick = () => { $('#nav').classList.toggle('open'); $('#backdrop').classList.toggle('show'); };
  $('#backdrop').onclick = closeNav;
  $('#theme').onclick = toggleTheme;
  route();
}
window.addEventListener('hashchange', () => { closeNav(); route(); });

function initTheme(){ const t = localStorage.getItem('tema') || 'light'; document.documentElement.setAttribute('data-theme', t); setThemeIcon(t); }
function toggleTheme(){ const cur = document.documentElement.getAttribute('data-theme'); const t = cur === 'dark' ? 'light' : 'dark'; document.documentElement.setAttribute('data-theme', t); localStorage.setItem('tema', t); setThemeIcon(t); }
function setThemeIcon(t){ $('#theme').textContent = t === 'dark' ? '☀️' : '🌙'; }
function closeNav(){ $('#nav').classList.remove('open'); $('#backdrop').classList.remove('show'); }

function buildNav() {
  let h = `<a href="#/">🏠 Inicio</a>`;
  (META.menus || []).forEach((m, mi) => {
    h += `<div class="grp">${esc(m.titulo || 'Menú')}</div>`;
    (m.items || []).forEach((it, ii) => {
      // it.tabla viene pre-computado por Python; byName como fallback para meta.json antiguo.
      const t = byName(it.texto);
      const href = it.tabla ? `#/abm/${it.tabla}` : (t ? `#/abm/${t.key}` : `#/info/${mi}/${ii}`);
      h += `<a href="${href}">${esc(it.texto || '')}</a>`;
    });
  });
  h += `<div class="grp">Tablas (ABM)</div>`;
  (META.tablas || []).forEach(t => {
    const c = COUNTS[t.key]; const badge = (c != null) ? `<span class="cnt">${c}</span>` : '';
    h += `<a href="#/abm/${t.key}">${esc(t.name)}${badge}</a>`;
  });
  if ((META.reportes || []).length) {
    h += `<div class="grp">Reportes</div>`;
    META.reportes.forEach((r, i) => { h += `<a href="#/rep/${i}">${esc(r.name)}</a>`; });
  }
  $('#nav').innerHTML = h;
}

function byName(txt) {
  const n = norm(txt);
  return (META.tablas || []).find(t => { const tn = norm(t.name); return tn && (n.includes(tn) || tn.includes(n)); });
}
function norm(s){return String(s||'').toLowerCase().replace(/[^a-z0-9]/g,'');}
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function assetSrc(val){ const s=String(val||'').trim().replace(/\\/g,'/'); return 'assets/' + encodeURIComponent(s.split('/').pop()); }
function cellHtml(f, val){
  if (f.es_imagen && val) return `<img class="thumb" src="${assetSrc(val)}" alt="${esc(val)}" title="${esc(val)}" onerror="this.replaceWith(document.createTextNode('${esc(val)}'))">`;
  if (f.input === 'checkbox') return (val==1||val===true||val==='1') ? '✅' : (val==null||val==='' ? '' : '—');
  return esc(val);
}

function route() {
  const hash = location.hash || '#/';
  document.querySelectorAll('nav a').forEach(a => a.classList.toggle('active', a.getAttribute('href') === hash));
  const p = hash.split('/');
  if (p[1] === 'abm') return viewAbm(p[2]);
  if (p[1] === 'rep') return viewReport(+p[2]);
  if (p[1] === 'info') return viewInfo(+p[2], +p[3]);
  return viewHome();
}
function tableByKey(k){return (META.tablas||[]).find(t=>t.key===k);}

// ---------- DASHBOARD ----------
function viewHome() {
  const s = META.stats || {};
  const tablas = META.tablas || [];
  const cards = [
    ['Tablas', s.tablas||0], ['Registros', s.registros_importados||0],
    ['Reportes', s.reportes||0], ['Índices', s.indices||0],
    ['Relaciones FK', s.relaciones||0], ['Imágenes', s.imagenes||0],
  ].map(([l,n]) => `<div class="metric"><div class="n">${n}</div><div class="l">${l}</div></div>`).join('');
  const max = Math.max(1, ...tablas.map(t => COUNTS[t.key]||0));
  const bars = tablas.slice().sort((a,b)=>(COUNTS[b.key]||0)-(COUNTS[a.key]||0)).map(t => {
    const c = COUNTS[t.key]||0; const w = Math.round((c/max)*100);
    return `<div class="bar"><a href="#/abm/${t.key}">${esc(t.name)}</a><div class="track"><div class="fill" style="width:${w}%"></div></div><span class="muted">${c}</span></div>`;
  }).join('');
  const pj = META.proyecto;
  const pjHtml = pj ? `<div class="card"><b>📦 Proyecto original (.pjx)</b>
    <p class="sub" style="margin:6px 0 0">Programa principal: <b>${esc(pj.principal||'(no declarado)')}</b> · ${(pj.archivos||[]).length} archivos declarados</p>
    <div style="margin-top:8px">${Object.entries(pj.por_tipo||{}).map(([k,v])=>`<span class="tag">${esc(k)}: ${v}</span>`).join('')}</div></div>` : '';
  const fkRels = [];
  (META.tablas||[]).forEach(t => {
    (t.campos||[]).filter(f=>f.fk).forEach(f => {
      const pt = tableByKey(f.fk.table);
      fkRels.push(`<span class="tag">📎 ${esc(t.name)}.${esc(f.label||f.name)} → ${esc(pt?pt.name:f.fk.table)}</span>`);
    });
  });
  const relsHtml = fkRels.length ? `<div class="card"><b>🔗 Relaciones entre tablas</b><div style="margin-top:8px">${fkRels.join('')}</div></div>` : '';
  $('#main').innerHTML = `<h1>${esc(META.titulo||'App migrada')}</h1>
    <p class="sub">Sistema migrado con todas sus utilidades. Datos reales importados del sistema original.</p>
    <div class="grid">${cards}</div>
    ${pjHtml}${relsHtml}
    <div class="card"><b>Registros por tabla</b><div class="bars" style="margin-top:12px">${bars||'<span class="muted">Sin datos importados.</span>'}</div></div>`;
}

// ---------- ABM ----------
function vstate(key){ return VS[key] || (VS[key] = { q:'', sort:'', dir:'asc', page:1, size:50 }); }

async function viewAbm(key) {
  const t = tableByKey(key);
  if (!t) { $('#main').innerHTML = '<p>Tabla no encontrada.</p>'; return; }
  // Pre-cargar opciones FK antes de renderizar el formulario.
  const fkTables = [...new Set((t.campos||[]).filter(f=>f.fk).map(f=>f.fk.table))];
  if (fkTables.length) await Promise.all(fkTables.map(loadFkOptions));
  const st = vstate(key);
  const qs = new URLSearchParams({ q:st.q, sort:st.sort, dir:st.dir, page:st.page, size:st.size });
  const res = await (await fetch(`/api/t/${key}?${qs}`)).json();
  const rows = res.rows || [], total = res.total || 0;
  const pages = Math.max(1, Math.ceil(total / st.size));
  const fields = t.campos;

  const form = formHtml(t, key);
  const head = '<tr>' + fields.map(f => {
    const ar = st.sort === f.name ? `<span class="ar">${st.dir==='asc'?'▲':'▼'}</span>` : '';
    return `<th onclick="sortBy('${key}','${f.name}')">${esc(f.label || f.name)} ${ar}</th>`;
  }).join('') + '<th></th></tr>';
  const body = rows.map(r => '<tr>' + fields.map(f => `<td>${cellHtml(f, r[f.name])}</td>`).join('') +
    `<td class="actions"><button class="sec sm" onclick='editRow(${JSON.stringify(r).replace(/'/g,"&#39;")})'>✎</button>
     <button class="del sm" onclick="delRow('${key}',${r.id})">🗑</button></td></tr>`).join('');

  $('#main').innerHTML = `<h2>${esc(t.titulo || t.name)}</h2>
    <p class="sub">${esc(t.descripcion || '')} <span class="muted">${total} registros</span></p>
    ${reglasHtml(t)}
    ${form}
    <div class="toolbar">
      <input class="search" id="q" placeholder="🔎 Buscar..." value="${esc(st.q)}" oninput="onSearch('${key}',this.value)">
      <span class="sp"></span>
      <button class="sec sm" onclick="window.open('/api/t/${key}/export.csv')">⬇ Exportar CSV</button>
    </div>
    <div class="tablewrap"><table><thead>${head}</thead><tbody>${body || '<tr><td class="muted" style="padding:16px">Sin registros.</td></tr>'}</tbody></table></div>
    <div class="pager">
      <span class="muted">Página ${st.page} de ${pages}</span>
      <button class="sec sm" ${st.page<=1?'disabled':''} onclick="goPage('${key}',${st.page-1})">‹ Anterior</button>
      <button class="sec sm" ${st.page>=pages?'disabled':''} onclick="goPage('${key}',${st.page+1})">Siguiente ›</button>
    </div>`;
}

function formHtml(t, key) {
  let form = `<form class="abm" id="abmform" onsubmit="return saveRow('${key}')"><input type="hidden" id="f_id">`;
  t.campos.forEach(f => {
    const req = (f.requerido && f.input !== 'checkbox') ? 'required' : '';
    const tip = f.ayuda ? `title="${esc(f.ayuda).replace(/"/g,'&quot;')}"` : '';
    const star = f.requerido ? ' *' : '';
    const onv = ` oninput="liveVal('${key}','${f.name}')"`;
    let ctl;
    if (f.fk) {
      // Campo FK: dropdown con opciones de la tabla padre.
      const opts = FK_CACHE[f.fk.table] || [];
      const pt = tableByKey(f.fk.table);
      // Primer campo de texto de la tabla padre como etiqueta visible.
      const dispF = pt ? (pt.campos.find(c => c.type==='C' && c.name!==f.fk.field) || pt.campos[0]) : null;
      const dKey = dispF ? dispF.name : f.fk.field;
      const selOpts = opts.map(r => {
        const v = r[f.fk.field] != null ? String(r[f.fk.field]) : '';
        const d = dispF && r[dKey] != null ? String(r[dKey]) : v;
        return `<option value="${esc(v)}">${esc(d || v)}</option>`;
      }).join('');
      ctl = `<select id="f_${f.name}" ${req} ${tip} onchange="liveVal('${key}','${f.name}')"><option value="">— Seleccionar —</option>${selOpts}</select>`;
    } else if (f.input === 'textarea') {
      ctl = `<textarea id="f_${f.name}" ${req} ${tip}${onv}></textarea>`;
    } else if (f.es_imagen) {
      ctl = `<input id="f_${f.name}" type="text" ${req} ${tip} oninput="document.getElementById('pv_${f.name}').src=assetSrc(this.value);liveVal('${key}','${f.name}')">`;
    } else {
      ctl = `<input id="f_${f.name}" type="${f.input==='checkbox'?'text':f.input}" ${req} ${tip}${onv}>`;
    }
    const pv = f.es_imagen ? `<img id="pv_${f.name}" class="thumb" alt="" onerror="this.style.display='none'">` : '';
    form += `<label>${esc(f.label || f.name)}${star}${ctl}${pv}<span class="err" id="e_${f.name}"></span></label>`;
  });
  form += `<div class="full"><button type="submit">💾 Guardar</button>
           <button type="button" class="sec" onclick="clearForm()">Limpiar</button></div></form>`;
  return form;
}

// Validación en vivo en el cliente (espejo de la del backend: requerido/numérico/maxlen).
function liveVal(key, name) {
  const t = tableByKey(key), f = t.campos.find(x => x.name === name);
  const e = document.getElementById('f_' + name), msg = document.getElementById('e_' + name);
  if (!f || !e || !msg) return true;
  const v = e.value; let err = '';
  if (f.requerido && !v.trim()) err = 'Obligatorio';
  else if (v.trim() && ['N','F','B','Y','I'].includes(f.type) && isNaN(Number(v.replace(',','.')))) err = 'Debe ser numérico';
  else if (f.maxlen && v.length > f.maxlen) err = `Máx. ${f.maxlen} caracteres`;
  msg.textContent = err; e.classList.toggle('bad', !!err);
  return !err;
}

function onSearch(key, v){ const st = vstate(key); st.q = v; st.page = 1; clearTimeout(st._t); st._t = setTimeout(() => viewAbm(key), 250); }
function sortBy(key, col){ const st = vstate(key); if (st.sort === col) st.dir = st.dir === 'asc' ? 'desc' : 'asc'; else { st.sort = col; st.dir = 'asc'; } viewAbm(key); }
function goPage(key, p){ vstate(key).page = p; viewAbm(key); }

function reglasHtml(t) {
  const r = t.reglas || [], v = t.validaciones || [];
  if (!r.length && !v.length) return '';
  let h = `<div class="card rules"><b>📋 Reglas de negocio (del sistema original)</b>`;
  if (r.length) h += `<ul style="margin:8px 0 0 18px">` + r.map(x => `<li>${esc(x)}</li>`).join('') + `</ul>`;
  if (v.length) h += `<div style="margin-top:8px">` +
    v.map(x => `<span class="tag">${esc(x.campo + ' ' + x.op + (x.valor !== undefined ? ' ' + JSON.stringify(x.valor) : ''))}</span>`).join('') + `</div>`;
  return h + `</div>`;
}

function clearForm(){document.querySelectorAll('form.abm [id^=f_]').forEach(e=>{e.value='';});document.querySelectorAll('form.abm .err').forEach(e=>e.textContent='');document.querySelectorAll('form.abm .bad').forEach(e=>e.classList.remove('bad'));}
function editRow(r){for(const k in r){const e=document.getElementById('f_'+k);if(e)e.value=r[k]==null?'':r[k];const pv=document.getElementById('pv_'+k);if(pv&&r[k]){pv.style.display='';pv.src=assetSrc(r[k]);}}document.getElementById('f_id').value=r.id;window.scrollTo({top:0,behavior:'smooth'});}

async function saveRow(key) {
  const t = tableByKey(key);
  let ok = true;
  t.campos.forEach(f => { if (!liveVal(key, f.name)) ok = false; });
  if (!ok) return false;
  const data = {};
  t.campos.forEach(f => { const e = document.getElementById('f_' + f.name); if (e) data[f.name] = e.value; });
  const id = document.getElementById('f_id').value;
  const url = id ? `/api/t/${key}/${id}` : `/api/t/${key}`;
  const r = await fetch(url, { method: id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    alert('No se pudo guardar:\n' + (e.detail || ('Error ' + r.status)));
    return false;
  }
  try { COUNTS = await (await fetch('/api/_counts')).json(); buildNav(); } catch(_) {}
  viewAbm(key);
  return false;
}
async function delRow(key, id) {
  if (!confirm('¿Borrar el registro?')) return;
  await fetch(`/api/t/${key}/${id}`, { method: 'DELETE' });
  try { COUNTS = await (await fetch('/api/_counts')).json(); buildNav(); } catch(_) {}
  viewAbm(key);
}

async function viewReport(i) {
  const r = META.reportes[i];
  if (!r) { $('#main').innerHTML = '<p>Reporte no encontrado.</p>'; return; }
  if (!r.tabla) { $('#main').innerHTML = `<h2>Reporte: ${esc(r.name)}</h2><div class="card muted">Sin tabla asociada.</div>`; return; }
  const t = tableByKey(r.tabla);
  const res = await (await fetch(`/api/t/${r.tabla}?size=500`).catch(()=>({json:()=>({rows:[]})}))).json();
  const rows = res.rows || [];
  const head = '<tr>' + t.campos.map(f => `<th>${esc(f.label||f.name)}</th>`).join('') + '</tr>';
  const body = rows.map(x => '<tr>' + t.campos.map(f => `<td>${cellHtml(f, x[f.name])}</td>`).join('') + '</tr>').join('');
  $('#main').innerHTML = `<h2>📊 ${esc(r.name)}</h2>
    <p class="sub">Reporte sobre la tabla <b>${esc(t.name)}</b> · ${res.total||rows.length} filas</p>
    <div class="toolbar"><span class="sp"></span><button class="sec sm" onclick="window.open('/api/t/${r.tabla}/export.csv')">⬇ Exportar CSV</button></div>
    <div class="tablewrap"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>`;
}

function viewInfo(mi, ii) {
  const it = ((META.menus[mi] || {}).items || [])[ii] || {};
  $('#main').innerHTML = `<h2>${esc(it.texto || 'Pantalla')}</h2>
    <div class="card">Utilidad del sistema original.<br><span class="muted">Acción legacy: ${esc(it.accion || '—')}</span></div>`;
}

boot();
'''


IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g|gif|bmp|ico|webp|tiff?)$", re.I)
# Pistas en el nombre del campo para detectar imágenes aunque no haya datos.
IMG_FIELD_HINT = re.compile(r"(imagen|imag|foto|photo|image|logo|dibujo|pic|icono|icon|thumb)", re.I)


def _basename_lower(val):
    """Nombre de archivo (sin ruta) en minúsculas, de un valor tipo 'fotos\\a.jpg'."""
    s = str(val or "").strip().replace("\\", "/")
    return s.rsplit("/", 1)[-1].lower()


def _detect_image_fields(meta, seed, asset_names):
    """Marca como imagen los campos cuyos valores son nombres de archivo de
    imagen (idealmente presentes en assets) o cuyo nombre sugiere una imagen.
    Devuelve la cantidad de campos marcados."""
    marked = 0
    for t in meta["tablas"]:
        rows = seed.get(t["key"]) or []
        for campo in t["campos"]:
            if campo.get("type") not in ("C", "M", "G", "P"):
                continue
            name = campo["name"]
            vals = [r.get(name) for r in rows if r.get(name)]
            looks = [v for v in vals if IMAGE_EXT_RE.search(str(v))]
            in_assets = [v for v in looks if _basename_lower(v) in asset_names]
            es_img = False
            if vals and len(looks) >= max(1, len(vals) * 0.3):
                es_img = True                      # los datos son nombres de imagen
            elif asset_names and IMG_FIELD_HINT.search(name) and (looks or not vals):
                es_img = True                      # el nombre del campo lo sugiere
            if es_img:
                campo["es_imagen"] = True
                campo["input"] = "text"            # se edita como texto (nombre de archivo)
                marked += 1
    return marked


# ── Tipo SQL de cada tipo DBF (para el script de importación) ──────────────────
_DBF_SQL_TYPE = {
    "C": "TEXT", "M": "TEXT", "G": "TEXT", "P": "TEXT",
    "N": "REAL", "F": "REAL",  "B": "REAL", "Y": "REAL", "I": "INTEGER",
    "L": "INTEGER", "D": "TEXT", "T": "TEXT",
}


def genera_proyecto_md(inventory, title="Sistema"):
    """Genera PROYECTO.md estructurado para ser leído por el agente como contexto.

    Formato conciso pero completo: tablas, campos, relaciones FK, menús, vistas,
    fragmentos de código y notas para el agente de migración.
    """
    import datetime
    today = datetime.date.today().isoformat()

    tables   = inventory.get("tables")       or []
    dbs      = inventory.get("databases")    or []
    menus    = inventory.get("menus")        or []
    forms    = inventory.get("forms_detail") or []
    samples  = inventory.get("samples")      or []
    by_ext   = inventory.get("by_ext")       or {}
    project  = inventory.get("project")      or {}

    relations, vistas = [], []
    for db in dbs:
        relations.extend(db.get("relaciones") or [])
        vistas.extend(db.get("vistas") or [])

    total_rec = sum(t.get("records", 0) for t in tables)
    IMG_EXTS  = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico", ".webp")
    total_img = sum(by_ext.get(e, 0) for e in IMG_EXTS)

    form_for_table = {}
    for f in forms:
        k = _slug(f.get("tabla_real") or f.get("tabla", ""))
        if k:
            form_for_table[k] = f.get("name", "")

    L = [
        f"# Sistema: {title}",
        f"> Generado por LegacyMigrator el {today}. "
        "Este archivo es el contexto para el agente de migración.",
        "",
        "## Resumen",
        "",
        f"| | |",
        f"|---|---|",
        f"| Tablas (.dbf) | {len(tables)} |",
        f"| Registros totales | {total_rec:,} |",
        f"| Bases de datos (.dbc) | {len(dbs)} |",
        f"| Relaciones FK | {len(relations)} |",
        f"| Vistas (.dbc) | {len(vistas)} |",
        f"| Formularios (.scx) | {by_ext.get('.scx', 0)} |",
        f"| Reportes (.frx) | {by_ext.get('.frx', 0)} |",
        f"| Clases (.vcx) | {by_ext.get('.vcx', 0)} |",
        f"| Programas (.prg) | {by_ext.get('.prg', 0)} |",
        f"| Imágenes | {total_img} |",
        "",
    ]

    if project:
        L += [
            "## Proyecto (.pjx)",
            "",
            f"- Programa principal: `{project.get('principal', '(no declarado)')}`",
            f"- Archivos declarados: {len(project.get('archivos', []))}",
            "",
        ]

    # Tablas ordenadas por cantidad de registros
    sorted_tables = sorted(tables, key=lambda t: t.get("records", 0), reverse=True)
    L += [
        "## Tablas",
        "",
        "| Tabla | Registros | Campos | Formulario |",
        "|-------|-----------|--------|------------|",
    ]
    for t in sorted_tables:
        key  = _slug(t.get("name", ""))
        form = form_for_table.get(key, "—")
        L.append(
            f"| `{t.get('name')}` | {t.get('records', 0):,} "
            f"| {len(t.get('fields') or [])} | {form} |"
        )
    L.append("")

    # Detalle de campos
    L += ["## Campos por tabla", ""]
    for t in sorted_tables:
        fields = t.get("fields") or []
        if not fields:
            continue
        L.append(f"### `{t.get('name')}`")
        for f in fields:
            L.append(f"- `{f.get('name')}` {f.get('type','C')}({f.get('len',0)})")
        L.append("")

    # Relaciones FK del .dbc
    if relations:
        L += [
            "## Relaciones FK (.dbc)",
            "",
            "| Tabla hija | Campo FK | Tabla padre | Campo padre |",
            "|-----------|----------|-------------|-------------|",
        ]
        for rel in relations:
            L.append(
                f"| `{rel.get('child_table')}` | `{rel.get('child_field')}` "
                f"| `{rel.get('parent_table')}` | `{rel.get('parent_field')}` |"
            )
        L.append("")

    if vistas:
        L += [f"## Vistas (.dbc)", f"", f"{len(vistas)} vistas definidas:"]
        for v in vistas[:30]:
            L.append(f"- `{v}`")
        if len(vistas) > 30:
            L.append(f"- _(y {len(vistas)-30} más)_")
        L.append("")

    if menus:
        L += ["## Menús", ""]
        for m in menus:
            items = m.get("items") or []
            L.append(f"### {m.get('titulo', 'Menú')}")
            for it in items:
                L.append(f"- **{it.get('texto', '')}** → `{it.get('accion', '')}`")
        L.append("")

    if samples:
        L += ["## Fragmentos de código (.prg / .vcx)", ""]
        for s in samples[:6]:
            content = str(s.get("content", ""))[:900]
            L += [
                f"### `{s.get('name', '')}`",
                "```foxpro",
                content,
                "```",
                "",
            ]

    # Tablas sin formulario (útil para el agente)
    sin_form = [
        f"`{t.get('name')}`"
        for t in sorted_tables
        if _slug(t.get("name", "")) not in form_for_table
    ]
    L += [
        "## Notas para el agente",
        "",
        f"- Tecnología origen: Visual FoxPro",
        f"- Tecnología destino: Python + FastAPI + SQLite + SPA vanilla",
        f"- Cobertura: 1 ABM por tabla × {len(tables)} tablas",
        f"- Datos disponibles: {total_rec:,} registros",
    ]
    if relations:
        L.append(f"- FK a respetar: {len(relations)} relaciones del .dbc")
    if vistas:
        L.append(f"- Formularios apuntan a vistas, no a tablas directas: revisar ControlSource en los .scx")
    if sin_form:
        L.append(f"- Tablas sin formulario SCX: {', '.join(sin_form[:20])}")
    L.append("")

    return "\n".join(L)


def genera_import_sql(inventory, seed, indexes, title="Sistema"):
    """Genera importar_datos.sql: script SQL idempotente para importar todos los
    datos del sistema legacy a SQLite.

    Incluye en orden:
    1. CREATE TABLE IF NOT EXISTS (esquema derivado del .dbf)
    2. CREATE INDEX de las expresiones .cdx/.idx originales
    3. CREATE INDEX para los campos FK del .dbc (hijo y padre)
    4. INSERT OR IGNORE con todos los registros del sistema viejo

    El script es seguro de re-ejecutar: OR IGNORE y IF NOT EXISTS.
    Revisarlo antes de correr, especialmente los INSERT.
    """
    import datetime
    today = datetime.date.today().isoformat()

    tables   = inventory.get("tables")    or []
    dbs      = inventory.get("databases") or []

    # Relaciones FK del .dbc → índices adicionales
    relations = []
    for db in dbs:
        relations.extend(db.get("relaciones") or [])

    # Construir esquema por tabla
    tables_sql   = {}   # key -> [(col, sql_type)]
    table_names  = {}   # key -> nombre original
    for t in tables:
        key = _slug(t.get("name", ""))
        if not key or key in tables_sql:
            continue
        cols = []
        for f in (t.get("fields") or []):
            cn = _slug(f.get("name", ""))
            if not cn or cn == "id" or cn in [c[0] for c in cols]:
                continue
            cols.append((cn, _DBF_SQL_TYPE.get(f.get("type", "C"), "TEXT")))
        if cols:
            tables_sql[key]  = cols
            table_names[key] = t.get("name", key)

    if not tables_sql:
        return f"-- Sin tablas para importar ({title})\n"

    total_rows = sum(len(v) for v in seed.values())
    total_idx  = sum(len(v) for v in indexes.values())

    L = [
        "-- importar_datos.sql",
        f"-- Sistema : {title}",
        f"-- Generado: {today}",
        f"-- Tablas  : {len(tables_sql)}  |  "
        f"Registros: {total_rows:,}  |  Índices CDX: {total_idx}",
        "--",
        "-- ANTES DE EJECUTAR:",
        "--   1. Revisar que los datos sean correctos.",
        "--   2. El script es idempotente (IF NOT EXISTS / OR IGNORE).",
        "--   3. Ejecutar sobre la base SQLite destino de la app generada.",
        "",
        "PRAGMA foreign_keys = OFF;",
        "BEGIN TRANSACTION;",
        "",
    ]

    fk_idx_added = set()   # evitar CREATE INDEX duplicados

    for key, cols in tables_sql.items():
        name    = table_names.get(key, key)
        rows    = seed.get(key) or []
        valid_c = {cn for cn, _ in cols}

        L.append(f"-- ── {name}  ({len(rows):,} registros) ──────────────────")

        # CREATE TABLE
        col_defs = ",\n    ".join(f'"{cn}" {ct}' for cn, ct in cols)
        L += [
            f'CREATE TABLE IF NOT EXISTS "{key}" (',
            f'    "id" INTEGER PRIMARY KEY AUTOINCREMENT,',
            f'    {col_defs}',
            f');',
        ]

        # CREATE INDEX de expresiones .cdx/.idx (índices originales del sistema)
        for i, idx_cols in enumerate(indexes.get(key) or []):
            idx_cols = [
                c for c in (idx_cols if isinstance(idx_cols, list) else [idx_cols])
                if c in valid_c
            ]
            if not idx_cols:
                continue
            col_list = ", ".join(f'"{c}"' for c in idx_cols)
            L.append(
                f'CREATE INDEX IF NOT EXISTS "ix_{key}_{i}" ON "{key}" ({col_list});'
            )

        # CREATE INDEX para campos FK del .dbc:
        # — campo hijo (child_field) para joins rápidos
        # — campo padre (parent_field) si no está ya indexado
        for rel in relations:
            ct = _slug(rel.get("child_table",  ""))
            cf = _slug(rel.get("child_field",  ""))
            pt = _slug(rel.get("parent_table", ""))
            pf = _slug(rel.get("parent_field", ""))
            if ct == key and cf and cf in valid_c:
                sig = (ct, "fk", cf)
                if sig not in fk_idx_added:
                    fk_idx_added.add(sig)
                    L.append(
                        f'CREATE INDEX IF NOT EXISTS "ix_{ct}_fk_{cf}" '
                        f'ON "{ct}" ("{cf}");'
                    )
            if pt == key and pf and pf in valid_c:
                sig = (pt, "pk", pf)
                if sig not in fk_idx_added:
                    fk_idx_added.add(sig)
                    L.append(
                        f'CREATE INDEX IF NOT EXISTS "ix_{pt}_pk_{pf}" '
                        f'ON "{pt}" ("{pf}");'
                    )

        L.append("")

        # INSERT OR IGNORE (bloques de 100 para no generar SQL kilométrico)
        if rows:
            BATCH = 100
            valid_list = [cn for cn, _ in cols]
            for b_start in range(0, len(rows), BATCH):
                batch = rows[b_start:b_start + BATCH]
                # Detectar qué columnas tienen datos en este bloque
                use_cols = []
                for r in batch:
                    for k in r:
                        if k in valid_c and k not in use_cols:
                            use_cols.append(k)
                if not use_cols:
                    continue
                col_part = ", ".join(f'"{c}"' for c in use_cols)
                val_rows = []
                for r in batch:
                    vals = []
                    for c in use_cols:
                        v = r.get(c)
                        if v is None:
                            vals.append("NULL")
                        elif isinstance(v, bool):
                            vals.append("1" if v else "0")
                        elif isinstance(v, (int, float)):
                            vals.append(str(v))
                        else:
                            vals.append("'" + str(v).replace("'", "''") + "'")
                    val_rows.append("(" + ", ".join(vals) + ")")
                L.append(f'INSERT OR IGNORE INTO "{key}" ({col_part}) VALUES')
                L.append("    " + ",\n    ".join(val_rows) + ";")
            L.append("")

    L += [
        "COMMIT;",
        "PRAGMA foreign_keys = ON;",
        "",
        f"-- Fin de importar_datos.sql  ({len(tables_sql)} tablas)",
    ]
    return "\n".join(L)


def build_app_scaffold(payload, assets=None):
    """payload = {inventory, title, seed, indexes}. `assets` = {nombre: bytes}
    con las imágenes a incluir. Devuelve (bytes_zip, meta)."""
    inventory = payload.get("inventory") or {}
    assets = assets or {}
    title = (payload.get("title") or inventory.get("nombre") or "App migrada").strip()
    enrich = payload.get("enrich") or {}

    meta, tables_sql = build_meta(inventory, title, enrich)

    # Datos e índices reales extraídos del ZIP (los provee el servidor). Solo
    # conservamos las tablas y columnas que existen en el esquema generado.
    seed_in = payload.get("seed") or {}
    indexes_in = payload.get("indexes") or {}
    seed, total_rows = {}, 0
    for key, cols in tables_sql.items():
        rows = seed_in.get(key)
        if not rows:
            continue
        valid = {cn for cn, _ in cols}
        clean = [{k: v for k, v in r.items() if k in valid} for r in rows]
        clean = [r for r in clean if r]
        if clean:
            seed[key] = clean
            total_rows += len(clean)
    # indexes_in[key] = lista de índices; cada índice = lista de columnas
    # (compuesto si tiene más de una). Filtramos a columnas válidas y dedupe.
    indexes = {}
    for key, defs in indexes_in.items():
        if key not in tables_sql:
            continue
        valid = {cn for cn, _ in tables_sql[key]}
        clean, seen = [], set()
        for cols in defs:
            cols = [c for c in (cols if isinstance(cols, list) else [cols]) if c in valid]
            sig = tuple(cols)
            if cols and sig not in seen:
                seen.add(sig)
                clean.append(cols)
        if clean:
            indexes[key] = clean
    meta["indexes"] = indexes
    meta["stats"]["registros_importados"] = total_rows
    meta["stats"]["tablas_con_datos"] = len(seed)
    meta["stats"]["indices"] = sum(len(v) for v in indexes.values())

    # Imágenes: detectar qué campos son imágenes (para mostrarlas en la SPA).
    asset_names = {k.lower() for k in assets}
    img_fields = _detect_image_fields(meta, seed, asset_names)
    meta["stats"]["imagenes"] = len(assets)
    meta["stats"]["campos_imagen"] = img_fields

    app_py = APP_PY
    meta_json = json.dumps({"tables": tables_sql, "meta": meta}, ensure_ascii=False)
    seed_json = json.dumps(seed, ensure_ascii=False) if seed else ""

    readme = "\n".join([
        f"# {title} — app migrada",
        "",
        "App moderna generada desde el sistema legacy, con **las mismas utilidades**.",
        "",
        "## Correr",
        "",
        "**Lo más fácil:** doble clic en `iniciar.bat` (Windows) o `bash iniciar.sh` (Mac/Linux).",
        "",
        "**Manual** (necesitás Python 3.10+):",
        "```bash",
        "python -m pip install fastapi uvicorn",
        "python -m uvicorn backend.app:app --port 8000",
        "```",
        "Abrir http://localhost:8000",
        "",
        "## Qué incluye",
        f"- {meta['stats']['tablas']} tablas con ABM (alta/baja/modificación/listado)",
        f"- {meta['stats'].get('registros_importados', 0)} registros importados de los .dbf reales",
        f"- {meta['stats'].get('indices', 0)} índices recreados (best-effort desde .cdx/.idx)",
        f"- {meta['stats'].get('imagenes', 0)} imágenes del sistema en `web/assets/` "
        f"({meta['stats'].get('campos_imagen', 0)} campos se muestran como imagen)",
        f"- {len(meta['menus'])} menús como navegación",
        f"- {meta['stats']['reportes']} reportes como vistas de consulta",
        "- Base de datos SQLite local (`backend/datos.db`, se crea sola)",
        "",
        "Los datos se cargan en `datos.db` la **primera vez** que arranca la app",
        "(si la tabla está vacía). Para re-importar desde cero, borrá `backend/datos.db`.",
        "",
        "Ver `COBERTURA.md` para el detalle de qué se cubrió.",
    ])

    # Usamos "python -m pip / -m uvicorn" porque en Windows los ejecutables que
    # instala pip (uvicorn.exe) suelen quedar en un Scripts\ fuera del PATH, y
    # "uvicorn" directo da "no se reconoce como comando".
    # IMPORTANTE: en el .bat usamos `set "PY=..."` CON comillas; sin comillas y
    # con saltos CRLF la variable se queda con un retorno de carro pegado y
    # `%PY% -m pip` se rompe ("no such option: -m"). Detectamos Python por su
    # --version (no por si pip falla, que puede ser por red) y preferimos el
    # lanzador `py` cuando existe.
    run_sh = (
        "#!/usr/bin/env bash\n"
        "cd \"$(dirname \"$0\")\" || exit 1\n"
        "PY=python3; command -v python3 >/dev/null 2>&1 || PY=python\n"
        "if ! command -v \"$PY\" >/dev/null 2>&1; then\n"
        "  echo 'ERROR: Python 3 no esta instalado. Instalalo desde https://www.python.org/downloads/'\n"
        "  exit 1\n"
        "fi\n"
        "echo 'Instalando dependencias (solo la primera vez)...'\n"
        "\"$PY\" -m pip install --quiet fastapi uvicorn\n"
        "echo 'Abriendo http://localhost:8000 ...'\n"
        "( sleep 2; (command -v xdg-open >/dev/null 2>&1 && xdg-open http://localhost:8000) || (command -v open >/dev/null 2>&1 && open http://localhost:8000) ) >/dev/null 2>&1 &\n"
        "\"$PY\" -m uvicorn backend.app:app --port 8000\n"
    )
    run_bat = (
        "@echo off\r\n"
        "cd /d \"%~dp0\"\r\n"
        "set \"PY=\"\r\n"
        "py -3 --version >nul 2>nul && set \"PY=py -3\"\r\n"
        "if not defined PY ( python --version >nul 2>nul && set \"PY=python\" )\r\n"
        "if not defined PY (\r\n"
        "  echo ERROR: Python no esta instalado o no esta en el PATH.\r\n"
        "  echo Instalalo desde https://www.python.org/downloads/\r\n"
        "  echo IMPORTANTE: marca \"Add Python to PATH\" durante la instalacion.\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        "echo Instalando dependencias (solo la primera vez)...\r\n"
        "%PY% -m pip install fastapi uvicorn\r\n"
        "echo.\r\n"
        "echo Abriendo http://localhost:8000 ...\r\n"
        "start \"\" http://localhost:8000\r\n"
        "%PY% -m uvicorn backend.app:app --port 8000\r\n"
        "pause\r\n"
    )

    # Generar PROYECTO.md (contexto estructurado para el agente) e importar_datos.sql
    proyecto_md = genera_proyecto_md(inventory, title)
    import_sql  = genera_import_sql(inventory, seed, indexes, title)

    files = {
        "backend/app.py": app_py,
        "backend/meta.json": meta_json,
        "backend/__init__.py": "",
        "backend/requirements.txt": "fastapi\nuvicorn\n",
        "web/index.html": INDEX_HTML,
        "web/style.css": STYLE_CSS,
        "web/app.js": APP_JS,
        "COBERTURA.md": _coverage_md(meta),
        "PROYECTO.md": proyecto_md,
        "importar_datos.sql": import_sql,
        "README.md": readme,
        "iniciar.sh": run_sh,
        "iniciar.bat": run_bat,
    }
    if seed_json:
        files["backend/seed.json"] = seed_json

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, content in files.items():
            z.writestr(path, content)
        # Imágenes del sistema original -> web/assets/ (servidas por el backend).
        for name, data in (assets or {}).items():
            z.writestr("web/assets/" + os.path.basename(name), data)
    return buf.getvalue(), meta
