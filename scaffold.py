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


_DO_FORM_RE = re.compile(r'\bDO\s+FORM\s+([\w./\\]+)', re.IGNORECASE)
_REPORT_RE = re.compile(r'\bREPORT\s+FORM\s+([\w./\\]+)', re.IGNORECASE)

# Prefijos comunes (verbos/abreviaturas) que envuelven el nombre real de la
# utilidad, tanto en la etiqueta visible ('ABM Clientes', 'Buscar cliente') como
# en el nombre del form ('frmPedidos', 'frmDetallePedido'). Se quitan SOLO al
# inicio para quedarnos con el sustantivo ('clientes', 'pedido').
_PREFIJOS_RE = re.compile(
    r'^(?:frm|abm|alta|baja|modif(?:icar|icacion)?|consult(?:a|ar)?|buscar|'
    r'busqueda|ver|listar|listado|mantenimiento|mantenim|detalle|edic(?:ion)?|'
    r'editar|nuevo|gestion|administr(?:ar|acion)|registro|rpt|reporte|informe)+'
)


def _form_name(raw):
    """Nombre de form sin ruta ni extensión: 'forms\\0300_servic.scx' -> '0300_servic'."""
    s = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"\.\w+$", "", s)


def _cands(raw):
    """Candidatos normalizados para resolver un nombre de utilidad → tabla.
    Genera, en orden de confianza: el nombre tal cual, sin prefijo numérico de
    orden ('0300_servic' → 'servic'), y sin prefijos de verbo/abreviatura
    ('frmPedidos' → 'pedidos', 'Buscar cliente' → 'cliente')."""
    out = []
    n = _norm(raw)
    if n:
        out.append(n)
    sin_num = _norm(re.sub(r"^[\d_\-.\s]+", "", str(raw or "")))
    if sin_num and sin_num not in out:
        out.append(sin_num)
    base = _PREFIJOS_RE.sub("", n)
    if base and len(base) >= 3 and base not in out:
        out.append(base)
    return out


def _match_tabla(cand, table_index):
    """Match de un candidato contra el índice de tablas: exacto, o por contención
    (nombre de tabla dentro del candidato o viceversa) prefiriendo el más largo.
    Mínimo 4 chars para la contención, así no matchea espurio."""
    for tn, tk in table_index.items():
        if tn and cand == tn:
            return tk
    best = None
    for tn, tk in table_index.items():
        if not tn:
            continue
        if (len(tn) >= 4 and tn in cand) or (len(cand) >= 4 and cand in tn):
            if best is None or len(tn) > best[0]:
                best = (len(tn), tk)
    return best[1] if best else None


def _menu_to_tabla(texto, accion, table_index, forms_by_name=None):
    """Resuelve un ítem de menú → key de tabla (o None).

    Prueba candidatos en orden de confianza:
      1. la etiqueta visible del menú ('Servicios', 'ABM Clientes');
      2. el nombre del form de la acción ('0300_servic', 'frmPedidos'),
         tolerando prefijo numérico de orden y prefijos de verbo (frm/abm/ver…);
      3. la tabla/vista real a la que el `.scx` está atado (ControlSource).
    Devuelve la primera tabla que matchea (exacto o por contención)."""
    forms_by_name = forms_by_name or {}
    candidates = list(_cands(texto))
    m = _DO_FORM_RE.search(accion or "")
    if m:
        fname = _form_name(m.group(1))
        for c in _cands(fname):
            if c not in candidates:
                candidates.append(c)
        src = (forms_by_name.get(_norm(fname))
               or forms_by_name.get(_norm(re.sub(r"^[\d_\-.]+", "", fname))))
        if src:
            for c in _cands(src):
                if c not in candidates:
                    candidates.append(c)
    for cand in candidates:
        if not cand:
            continue
        tk = _match_tabla(cand, table_index)
        if tk:
            return tk
    return None


def _menu_to_reporte(accion, report_index):
    """Resuelve un ítem de menú con 'REPORT FORM xxx' → índice de reporte (o None)."""
    m = _REPORT_RE.search(accion or "")
    if not m:
        return None
    rname = _form_name(m.group(1))
    for cand in _cands(rname):
        for rn, ri in report_index.items():
            if rn and (cand == rn or (len(rn) >= 4 and rn in cand)
                       or (len(cand) >= 4 and cand in rn)):
                return ri
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
        tabla, campos = f.get("tabla"), f.get("campos") or []
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

    # Mapa nombre-de-formulario → tabla/vista a la que está atado (ControlSource),
    # para resolver acciones de menú tipo "DO FORM 0300_servic" a su ABM real.
    forms_by_name = {}
    for f in inventory.get("forms_detail", []):
        nm = _norm(f.get("name"))
        tb = f.get("tabla")
        if nm and tb:
            forms_by_name[nm] = tb

    # Índice de reportes por nombre normalizado, para wirear ítems "REPORT FORM xxx".
    report_index = {_norm(r["name"]): i for i, r in enumerate(reportes) if r.get("name")}

    # Menús: resolver cada ítem a una tabla (clave "tabla") o a un reporte (clave
    # "reporte") para que la SPA navegue directo a la utilidad correspondiente.
    menus = []
    for grp in (inventory.get("menus", []) or []):
        items = []
        for it in (grp.get("items", []) or []):
            accion = it.get("accion", "")
            # 'REPORT FORM xxx' es señal explícita de reporte: si existe el .frx,
            # va a la vista de reporte; si no, cae al ABM de la tabla más cercana.
            rep = _menu_to_reporte(accion, report_index)
            if rep is not None:
                items.append({**it, "tabla": None, "reporte": rep})
                continue
            tabla = _menu_to_tabla(it.get("texto", ""), accion,
                                   table_index, forms_by_name)
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
  <div class="brand"><span class="brand-dot"></span><b id="apptitle">App migrada</b></div>
  <span id="stats" class="muted"></span>
  <button id="theme" class="icon" title="Tema claro/oscuro" aria-label="Tema">🌙</button>
</header>
<div class="layout">
  <nav id="nav"></nav>
  <div id="backdrop"></div>
  <main id="main"></main>
</div>
<div id="modalRoot"></div>
<script src="app.js"></script>
</body></html>
'''


STYLE_CSS = r''':root{
  --bg:#f7f8fa; --panel:#ffffff; --panel2:#f4f5f7; --text:#16181d; --muted:#71757e;
  --border:#ebecef; --border2:#e0e2e6;
  --brand:#5b54d6; --brand-d:#4a43c4; --brand-bg:#f0effe; --brand-ring:rgba(91,84,214,.18);
  --accent:#0ea5a3; --danger:#e0483d; --danger-bg:#fdeceb;
  --shadow-sm:0 1px 2px rgba(16,18,29,.05);
  --shadow:0 4px 12px rgba(16,18,29,.06),0 1px 3px rgba(16,18,29,.04);
  --shadow-lg:0 20px 50px rgba(16,18,29,.18);
  --radius:14px; --radius-sm:9px;
}
[data-theme=dark]{
  --bg:#0c0e12; --panel:#15181e; --panel2:#1b1f27; --text:#e7e9ed; --muted:#9aa0ac;
  --border:#262b34; --border2:#2f3540;
  --brand:#8e88ff; --brand-d:#a59fff; --brand-bg:#1e2042; --brand-ring:rgba(142,136,255,.25);
  --accent:#2dd4bf; --danger:#f4796f; --danger-bg:#2a1715;
  --shadow-sm:0 1px 2px rgba(0,0,0,.3);
  --shadow:0 4px 14px rgba(0,0,0,.4);
  --shadow-lg:0 24px 60px rgba(0,0,0,.6);
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,sans-serif;color:var(--text);background:var(--bg);font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
::selection{background:var(--brand-bg)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;background:var(--panel2);padding:2px 6px;border-radius:5px}

/* header */
header{background:rgba(255,255,255,.82);backdrop-filter:saturate(180%) blur(12px);border-bottom:1px solid var(--border);padding:0 18px;height:56px;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:40}
[data-theme=dark] header{background:rgba(21,24,30,.82)}
.brand{display:flex;align-items:center;gap:9px}
.brand-dot{width:22px;height:22px;border-radius:7px;background:linear-gradient(135deg,var(--brand),var(--accent));box-shadow:var(--shadow-sm)}
header b{font-size:15px;font-weight:700;letter-spacing:-.01em}
header #stats{flex:1;font-size:12.5px;color:var(--muted)}
.icon{background:transparent;border:1px solid transparent;color:var(--muted);font-size:17px;cursor:pointer;border-radius:9px;width:34px;height:34px;display:inline-flex;align-items:center;justify-content:center;transition:.15s}
.icon:hover{background:var(--panel2);color:var(--text)}
#burger{display:none}

/* layout */
.layout{display:flex;min-height:calc(100vh - 56px)}
nav{width:248px;background:var(--panel);border-right:1px solid var(--border);overflow:auto;padding:12px 10px;flex-shrink:0;position:sticky;top:56px;height:calc(100vh - 56px)}
nav .grp{font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:700;margin:16px 10px 6px}
nav a{display:flex;align-items:center;gap:9px;padding:8px 11px;border-radius:9px;cursor:pointer;font-size:13.5px;color:var(--text);text-decoration:none;font-weight:500;transition:.12s;position:relative}
nav a:hover{background:var(--panel2)}
nav a.active{background:var(--brand-bg);color:var(--brand-d);font-weight:600}
nav a.active::before{content:"";position:absolute;left:0;top:18%;bottom:18%;width:3px;border-radius:3px;background:var(--brand)}
nav a .cnt{margin-left:auto;font-size:11px;color:var(--muted);background:var(--panel2);border:1px solid var(--border);border-radius:20px;padding:1px 8px;font-weight:600}
nav a.active .cnt{background:var(--panel);color:var(--brand-d)}
#backdrop{display:none}
main{flex:1;overflow:auto;padding:28px 32px;max-width:1280px;width:100%;margin:0 auto}

/* typography */
h1{font-size:25px;font-weight:700;letter-spacing:-.02em}
h2{font-size:21px;font-weight:700;letter-spacing:-.02em}
h3{font-size:16px;font-weight:700;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13.5px}
.muted{color:var(--muted)}

/* page header */
.page-head{display:flex;align-items:flex-start;gap:16px;margin-bottom:22px}
.page-head .ttl{flex:1;min-width:0}
.page-head h1,.page-head h2{margin-bottom:3px}
.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--brand-d);font-weight:700;margin-bottom:6px}

/* buttons */
button,.btn{font-family:inherit;background:var(--brand);color:#fff;border:1px solid var(--brand);border-radius:var(--radius-sm);padding:9px 15px;cursor:pointer;font-weight:600;font-size:13px;transition:.15s;display:inline-flex;align-items:center;gap:7px;white-space:nowrap}
button:hover{background:var(--brand-d);border-color:var(--brand-d)}
button:active{transform:translateY(1px)}
button.sec{background:var(--panel);color:var(--text);border:1px solid var(--border2)}
button.sec:hover{background:var(--panel2);border-color:var(--muted)}
button.del{background:var(--danger);border-color:var(--danger)}
button.del:hover{filter:brightness(.94)}
button.sm{padding:6px 11px;font-size:12px}
button.ghost{background:transparent;border-color:transparent;color:var(--muted);padding:6px 8px}
button.ghost:hover{background:var(--panel2);color:var(--text)}
button:disabled{opacity:.45;cursor:not-allowed}

/* toolbar + search */
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:0 0 14px}
.toolbar .sp{flex:1}
.search-wrap{position:relative;flex:1;max-width:380px;min-width:190px}
.search-wrap .si{position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:14px;pointer-events:none}
input.search{width:100%;padding:9px 12px 9px 34px;border:1px solid var(--border2);border-radius:var(--radius-sm);background:var(--panel);color:var(--text);font-size:13px;transition:.15s}
input.search:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px var(--brand-ring)}

/* table */
.tablewrap{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow-sm);overflow:auto}
table{border-collapse:collapse;width:100%}
th,td{padding:11px 16px;text-align:left;font-size:13px;white-space:nowrap}
thead th{font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);position:sticky;top:0;cursor:pointer;user-select:none;border-bottom:1px solid var(--border);background:var(--panel)}
th:hover{color:var(--text)}
th .ar{font-size:9px;color:var(--brand);margin-left:3px}
tbody td{border-bottom:1px solid var(--border)}
tbody tr:last-child td{border-bottom:none}
tbody tr{transition:background .1s}
tbody tr:hover{background:var(--panel2)}
td.actions{text-align:right;white-space:nowrap}
td.actions button{opacity:.55;transition:.12s}
tr:hover td.actions button{opacity:1}
img.thumb{max-width:64px;max-height:48px;border-radius:6px;border:1px solid var(--border);vertical-align:middle;object-fit:cover}

/* pager */
.pager{display:flex;gap:8px;align-items:center;justify-content:flex-end;margin-top:14px;flex-wrap:wrap;font-size:13px}

/* cards / metrics */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(176px,1fr));gap:14px;margin-bottom:22px}
.metric{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:18px;box-shadow:var(--shadow-sm);text-decoration:none;color:var(--text);display:block;transition:.15s}
.metric:hover{box-shadow:var(--shadow);transform:translateY(-2px);border-color:var(--border2)}
.metric .n{font-size:28px;font-weight:700;letter-spacing:-.02em}
.metric .l{font-size:12.5px;color:var(--muted);margin-top:3px;font-weight:500}
.card{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:18px 20px;margin-bottom:16px;box-shadow:var(--shadow-sm)}
.card>b{font-size:14px;font-weight:700}
.card.rules{border-left:3px solid var(--accent)}
.bars{display:flex;flex-direction:column;gap:9px}
.bar{display:grid;grid-template-columns:170px 1fr 64px;gap:12px;align-items:center;font-size:12.5px}
.bar .track{background:var(--panel2);border-radius:6px;height:10px;overflow:hidden}
.bar .fill{background:linear-gradient(90deg,var(--brand),var(--accent));height:100%;border-radius:6px}
.bar a{color:var(--text);text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}
.bar a:hover{color:var(--brand)}
.bar .v{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.tag{display:inline-flex;align-items:center;background:var(--brand-bg);color:var(--brand-d);border-radius:7px;padding:3px 9px;font-size:11.5px;margin:2px 5px 2px 0;font-weight:600}

/* empty state */
.empty{text-align:center;padding:56px 20px;color:var(--muted)}
.empty .ei{font-size:38px;margin-bottom:10px;opacity:.6}
.empty p{font-size:14px}

/* modal */
.modal-backdrop{position:fixed;inset:0;background:rgba(16,18,29,.45);backdrop-filter:blur(3px);display:flex;align-items:flex-start;justify-content:center;padding:40px 16px;z-index:60;opacity:0;transition:opacity .18s;overflow:auto}
.modal-backdrop.show{opacity:1}
.modal{background:var(--panel);border:1px solid var(--border);border-radius:16px;box-shadow:var(--shadow-lg);width:100%;max-width:620px;margin:auto;transform:translateY(8px) scale(.98);transition:transform .2s;overflow:hidden}
.modal-backdrop.show .modal{transform:none}
.modal-head{display:flex;align-items:center;gap:12px;padding:18px 22px;border-bottom:1px solid var(--border)}
.modal-head h3{flex:1}
.modal-body{padding:22px;max-height:70vh;overflow:auto}

/* form */
form.abm{display:grid;grid-template-columns:1fr 1fr;gap:16px}
form.abm label{display:flex;flex-direction:column;font-size:12px;color:var(--muted);gap:5px;font-weight:600}
form.abm input,form.abm textarea,form.abm select{padding:9px 11px;border:1px solid var(--border2);border-radius:var(--radius-sm);font-size:13.5px;background:var(--panel);color:var(--text);font-weight:400;font-family:inherit;transition:.15s}
form.abm textarea{min-height:72px;resize:vertical}
form.abm input:focus,form.abm textarea:focus,form.abm select:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px var(--brand-ring)}
form.abm input.bad{border-color:var(--danger);box-shadow:0 0 0 3px var(--danger-bg)}
form.abm .err{color:var(--danger);font-size:11px;font-weight:600;min-height:0}
form.abm .full{grid-column:1 / -1}
.modal-foot{grid-column:1 / -1;display:flex;gap:10px;justify-content:flex-end;margin-top:6px;padding-top:16px;border-top:1px solid var(--border)}

@media(max-width:860px){
  #burger{display:inline-flex}
  main{padding:20px 16px}
  nav{position:fixed;left:-280px;top:56px;z-index:50;transition:left .2s;box-shadow:var(--shadow-lg)}
  nav.open{left:0}
  #backdrop.show{display:block;position:fixed;inset:56px 0 0 0;background:rgba(0,0,0,.4);z-index:45}
  form.abm{grid-template-columns:1fr}
  .bar{grid-template-columns:110px 1fr 50px}
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
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
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
      // it.tabla / it.reporte vienen pre-computados por Python; byName como fallback.
      const t = byName(it.texto);
      const href = it.tabla ? `#/abm/${it.tabla}`
        : (it.reporte != null ? `#/rep/${it.reporte}`
        : (t ? `#/abm/${t.key}` : `#/info/${mi}/${ii}`));
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
function nf(n){ return (Number(n)||0).toLocaleString('es'); }

function viewHome() {
  const s = META.stats || {};
  const tablas = META.tablas || [];
  const cards = [
    ['Tablas', s.tablas||0], ['Registros', s.registros_importados||0],
    ['Reportes', s.reportes||0], ['Índices', s.indices||0],
    ['Relaciones', s.relaciones||0], ['Imágenes', s.imagenes||0],
  ].map(([l,n]) => `<div class="metric"><div class="n">${nf(n)}</div><div class="l">${l}</div></div>`).join('');
  const max = Math.max(1, ...tablas.map(t => COUNTS[t.key]||0));
  const bars = tablas.slice().sort((a,b)=>(COUNTS[b.key]||0)-(COUNTS[a.key]||0)).map(t => {
    const c = COUNTS[t.key]||0; const w = Math.round((c/max)*100);
    return `<div class="bar"><a href="#/abm/${t.key}">${esc(t.name)}</a><div class="track"><div class="fill" style="width:${w}%"></div></div><span class="v">${nf(c)}</span></div>`;
  }).join('');
  const pj = META.proyecto;
  const pjHtml = pj ? `<div class="card"><b>📦 Proyecto original</b>
    <p class="sub" style="margin:6px 0 0">Programa principal: <b>${esc(pj.principal||'(no declarado)')}</b> · ${(pj.archivos||[]).length} archivos declarados</p>
    <div style="margin-top:10px">${Object.entries(pj.por_tipo||{}).map(([k,v])=>`<span class="tag">${esc(k)}: ${v}</span>`).join('')}</div></div>` : '';
  const fkRels = [];
  (META.tablas||[]).forEach(t => {
    (t.campos||[]).filter(f=>f.fk).forEach(f => {
      const pt = tableByKey(f.fk.table);
      fkRels.push(`<span class="tag">📎 ${esc(t.name)}.${esc(f.label||f.name)} → ${esc(pt?pt.name:f.fk.table)}</span>`);
    });
  });
  const relsHtml = fkRels.length ? `<div class="card"><b>🔗 Relaciones entre tablas</b><div style="margin-top:10px">${fkRels.join('')}</div></div>` : '';
  $('#main').innerHTML = `<div class="page-head"><div class="ttl"><div class="eyebrow">Panel</div>
      <h1>${esc(META.titulo||'App migrada')}</h1>
      <p class="sub">Sistema migrado con todas sus utilidades · datos reales del sistema original.</p></div></div>
    <div class="grid">${cards}</div>
    ${pjHtml}${relsHtml}
    <div class="card"><b>Registros por tabla</b><div class="bars" style="margin-top:14px">${bars||'<span class="muted">Sin datos importados.</span>'}</div></div>`;
}

// ---------- ABM ----------
function vstate(key){ return VS[key] || (VS[key] = { q:'', sort:'', dir:'asc', page:1, size:50 }); }

async function viewAbm(key) {
  const t = tableByKey(key);
  if (!t) { $('#main').innerHTML = '<p>Tabla no encontrada.</p>'; return; }
  const st = vstate(key);
  const qs = new URLSearchParams({ q:st.q, sort:st.sort, dir:st.dir, page:st.page, size:st.size });
  const res = await (await fetch(`/api/t/${key}?${qs}`)).json();
  const rows = res.rows || [], total = res.total || 0;
  const pages = Math.max(1, Math.ceil(total / st.size));
  const fields = t.campos;

  const head = '<tr>' + fields.map(f => {
    const ar = st.sort === f.name ? `<span class="ar">${st.dir==='asc'?'▲':'▼'}</span>` : '';
    return `<th onclick="sortBy('${key}','${f.name}')">${esc(f.label || f.name)}${ar}</th>`;
  }).join('') + '<th></th></tr>';
  const body = rows.map(r => {
    const j = JSON.stringify(r).replace(/'/g, "&#39;");
    return '<tr>' + fields.map(f => `<td>${cellHtml(f, r[f.name])}</td>`).join('') +
      `<td class="actions"><button class="ghost sm" title="Editar" onclick='openForm("${key}", ${j})'>✎</button>` +
      `<button class="ghost sm" title="Borrar" onclick="delRow('${key}',${r.id})">🗑</button></td></tr>`;
  }).join('');

  const tableHtml = rows.length
    ? `<div class="tablewrap"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>
       <div class="pager">
         <span class="muted">Página ${st.page} de ${pages} · ${nf(total)} registros</span>
         <button class="sec sm" ${st.page<=1?'disabled':''} onclick="goPage('${key}',${st.page-1})">‹ Anterior</button>
         <button class="sec sm" ${st.page>=pages?'disabled':''} onclick="goPage('${key}',${st.page+1})">Siguiente ›</button>
       </div>`
    : `<div class="tablewrap"><div class="empty"><div class="ei">${st.q?'🔍':'📭'}</div>
         <p>${st.q?'Sin resultados para tu búsqueda.':'Todavía no hay registros.'}</p>
         ${st.q?'':`<p style="margin-top:14px"><button onclick="openForm('${key}')">＋ Crear el primero</button></p>`}</div></div>`;

  $('#main').innerHTML = `<div class="page-head">
      <div class="ttl"><h2>${esc(t.titulo || t.name)}</h2>
      <p class="sub">${esc(t.descripcion || '')}${t.descripcion?' · ':''}${nf(total)} registros</p></div>
      <button onclick="openForm('${key}')">＋ Nuevo</button>
    </div>
    ${reglasHtml(t)}
    <div class="toolbar">
      <div class="search-wrap"><span class="si">🔎</span><input class="search" id="q" placeholder="Buscar en ${esc(t.name)}..." value="${esc(st.q)}" oninput="onSearch('${key}',this.value)"></div>
      <span class="sp"></span>
      <button class="sec sm" onclick="window.open('/api/t/${key}/export.csv')">⬇ Exportar CSV</button>
    </div>
    ${tableHtml}`;
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
  form += `<div class="modal-foot"><button type="button" class="sec" onclick="closeModal()">Cancelar</button>
           <button type="submit">Guardar</button></div></form>`;
  return form;
}

// ---------- MODAL DE ALTA/EDICIÓN ----------
async function openForm(key, row) {
  const t = tableByKey(key);
  if (!t) return;
  // Pre-cargar opciones FK antes de pintar el formulario.
  const fkTables = [...new Set((t.campos||[]).filter(f=>f.fk).map(f=>f.fk.table))];
  if (fkTables.length) await Promise.all(fkTables.map(loadFkOptions));
  const title = (row ? 'Editar ' : 'Nuevo ') + (t.titulo || t.name);
  $('#modalRoot').innerHTML = `<div class="modal-backdrop" id="mb" onclick="if(event.target===this)closeModal()">
    <div class="modal" role="dialog" aria-modal="true">
      <div class="modal-head"><h3>${esc(title)}</h3><button class="icon" title="Cerrar (Esc)" onclick="closeModal()">✕</button></div>
      <div class="modal-body">${formHtml(t, key)}</div>
    </div></div>`;
  if (row) fillForm(row);
  requestAnimationFrame(() => {
    const mb = $('#mb'); if (mb) mb.classList.add('show');
    const first = document.querySelector('form.abm [id^=f_]:not([type=hidden])');
    if (first) first.focus();
  });
}
function closeModal() {
  const mb = $('#mb'); if (!mb) return;
  mb.classList.remove('show');
  setTimeout(() => { const r = $('#modalRoot'); if (r) r.innerHTML = ''; }, 180);
}
function fillForm(r) {
  for (const k in r) {
    const e = document.getElementById('f_' + k); if (e) e.value = r[k] == null ? '' : r[k];
    const pv = document.getElementById('pv_' + k); if (pv && r[k]) { pv.style.display = ''; pv.src = assetSrc(r[k]); }
  }
  const idf = document.getElementById('f_id'); if (idf) idf.value = r.id;
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
  closeModal();
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
  if (!r.tabla) {
    $('#main').innerHTML = `<div class="page-head"><div class="ttl"><div class="eyebrow">📊 Reporte</div><h2>${esc(r.name)}</h2></div></div>
      <div class="empty"><div class="ei">📊</div><p>Reporte sin tabla de datos asociada.</p></div>`;
    return;
  }
  const t = tableByKey(r.tabla);
  const res = await (await fetch(`/api/t/${r.tabla}?size=500`).catch(()=>({json:()=>({rows:[]})}))).json();
  const rows = res.rows || [];
  const head = '<tr>' + t.campos.map(f => `<th>${esc(f.label||f.name)}</th>`).join('') + '</tr>';
  const body = rows.map(x => '<tr>' + t.campos.map(f => `<td>${cellHtml(f, x[f.name])}</td>`).join('') + '</tr>').join('');
  $('#main').innerHTML = `<div class="page-head">
      <div class="ttl"><div class="eyebrow">📊 Reporte</div><h2>${esc(r.name)}</h2>
      <p class="sub">Sobre <b>${esc(t.name)}</b> · ${nf(res.total||rows.length)} filas</p></div>
      <button class="sec" onclick="window.open('/api/t/${r.tabla}/export.csv')">⬇ Exportar CSV</button>
    </div>
    <div class="tablewrap"><table><thead>${head}</thead><tbody>${body || '<tr><td class="muted" style="padding:16px">Sin datos.</td></tr>'}</tbody></table></div>`;
}

function viewInfo(mi, ii) {
  const it = ((META.menus[mi] || {}).items || [])[ii] || {};
  $('#main').innerHTML = `<div class="page-head"><div class="ttl"><div class="eyebrow">Utilidad</div><h2>${esc(it.texto || 'Pantalla')}</h2></div></div>
    <div class="empty"><div class="ei">🧩</div>
    <p>Esta utilidad del sistema original no tiene una pantalla de datos asociada.</p>
    <p class="muted" style="margin-top:10px;font-size:12px">Acción legacy: <code>${esc(it.accion || '—')}</code></p></div>`;
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

    files = {
        "backend/app.py": app_py,
        "backend/meta.json": meta_json,
        "backend/__init__.py": "",
        "backend/requirements.txt": "fastapi\nuvicorn\n",
        "web/index.html": INDEX_HTML,
        "web/style.css": STYLE_CSS,
        "web/app.js": APP_JS,
        "COBERTURA.md": _coverage_md(meta),
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
