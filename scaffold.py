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
        if fi:
            for c in campos:
                if c["name"] in fi["labels"]:
                    c["label"] = fi["labels"][c["name"]]
            campos.sort(key=lambda c: fi["order"].get(c["name"], 999))
            tabla["origen_formulario"] = True
        # La IA (si se usó) puede refinar título/etiquetas/reglas por encima.
        _apply_enrich(tabla, enrich.get(key))
        tablas.append(tabla)

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
    menus = inventory.get("menus", []) or []

    meta = {
        "titulo": title,
        "tablas": tablas,
        "menus": menus,
        "reportes": reportes,
        "formularios": formularios,
        "stats": {
            "tablas": len(tablas), "formularios": len(formularios),
            "reportes": len(reportes),
            "items_menu": sum(len(m.get("items", [])) for m in menus),
            "enriquecidas": sum(1 for t in tablas if t.get("enriquecido")),
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
        f"| Menús (navegación) | {len(meta['menus'])} | ✅ generado |",
        f"| Reportes (vista/consulta) | {s['reportes']} | ✅ generado |",
        f"| Formularios originales | {s['formularios']} | 🟡 listados |",
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
    out += ["", "---", "_Generado por LegacyMigrator (cobertura total)._"]
    return "\n".join(out)


APP_PY = r'''#!/usr/bin/env python3
"""App migrada — backend FastAPI + SQLite. Generado por LegacyMigrator.

Correr (desde la carpeta del proyecto):
    python -m pip install fastapi uvicorn
    python -m uvicorn backend.app:app --port 8000
Abrir: http://localhost:8000
"""
import json, os, re, sqlite3
from fastapi import FastAPI, HTTPException, Request
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


@app.get("/api/t/{table}")
def list_rows(table: str):
    if table not in TABLES:
        raise HTTPException(404, "tabla desconocida")
    c = conn()
    rows = [dict(r) for r in c.execute('SELECT * FROM "%s" ORDER BY id DESC LIMIT 2000' % table)]
    c.close()
    return rows


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
<html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>App migrada</title><link rel="stylesheet" href="style.css"></head>
<body>
<header><b id="apptitle">App migrada</b><span id="stats"></span></header>
<div class="layout">
  <nav id="nav"></nav>
  <main id="main"><p class="muted">Elegí una utilidad del menú de la izquierda.</p></main>
</div>
<script src="app.js"></script>
</body></html>
'''


STYLE_CSS = r'''*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,Segoe UI,sans-serif;color:#1a1917;background:#f6f5f2}
header{background:#534AB7;color:#fff;padding:10px 16px;display:flex;align-items:center;gap:12px}
header b{font-size:16px}
header #stats{font-size:12px;opacity:.85}
.layout{display:flex;height:calc(100vh - 44px)}
nav{width:280px;background:#fff;border-right:1px solid #e2dfd8;overflow:auto;padding:8px}
nav .grp{font-size:11px;text-transform:uppercase;color:#9c9a92;font-weight:700;margin:12px 8px 4px}
nav a{display:block;padding:6px 10px;border-radius:6px;cursor:pointer;font-size:13px;color:#1a1917;text-decoration:none}
nav a:hover{background:#f0ede8}
nav a.active{background:#EEEDFE;color:#3C3489;font-weight:600}
main{flex:1;overflow:auto;padding:18px}
h2{font-size:18px;margin-bottom:10px}
.muted{color:#9c9a92}
table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #e2dfd8;border-radius:8px;overflow:hidden}
th,td{padding:7px 10px;border-bottom:1px solid #eee;font-size:13px;text-align:left}
th{background:#f0ede8;font-weight:700}
button{background:#534AB7;color:#fff;border:none;border-radius:6px;padding:7px 12px;cursor:pointer;font-weight:600}
button.sec{background:#fff;color:#534AB7;border:1px solid #ccc9c0}
button.del{background:#A32D2D}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
form.abm{background:#fff;border:1px solid #e2dfd8;border-radius:8px;padding:14px;margin-bottom:14px;display:grid;grid-template-columns:1fr 1fr;gap:10px}
form.abm label{display:flex;flex-direction:column;font-size:12px;color:#6b6963;gap:3px}
form.abm input,form.abm textarea{padding:6px 8px;border:1px solid #ccc9c0;border-radius:6px;font-size:13px}
form.abm .full{grid-column:1 / -1;display:flex;gap:8px}
.card{background:#fff;border:1px solid #e2dfd8;border-radius:8px;padding:14px;margin-bottom:10px}
'''


APP_JS = r'''let META = null;
const $ = (s) => document.querySelector(s);

async function boot() {
  META = await (await fetch('/api/_meta')).json();
  $('#apptitle').textContent = META.titulo || 'App migrada';
  const s = META.stats || {};
  $('#stats').textContent = `${s.tablas||0} tablas · ${s.reportes||0} reportes · ${s.items_menu||0} ítems de menú`;
  buildNav();
  route();
}
window.addEventListener('hashchange', route);

function buildNav() {
  const nav = $('#nav');
  let h = '';
  // Menús originales del sistema (navegación legacy).
  (META.menus || []).forEach((m, mi) => {
    h += `<div class="grp">${esc(m.titulo || 'Menú')}</div>`;
    (m.items || []).forEach((it, ii) => {
      const t = byName(it.texto);
      const href = t ? `#/abm/${t.key}` : `#/info/${mi}/${ii}`;
      h += `<a href="${href}">${esc(it.texto || '')}</a>`;
    });
  });
  // Todas las tablas (ABM) — garantiza cobertura.
  h += `<div class="grp">Tablas (ABM)</div>`;
  (META.tablas || []).forEach(t => { h += `<a href="#/abm/${t.key}">${esc(t.name)}</a>`; });
  // Reportes.
  if ((META.reportes || []).length) {
    h += `<div class="grp">Reportes</div>`;
    META.reportes.forEach((r, i) => { h += `<a href="#/rep/${i}">${esc(r.name)}</a>`; });
  }
  nav.innerHTML = h;
}

// Busca una tabla cuyo nombre se parezca al texto de un ítem de menú.
function byName(txt) {
  const n = norm(txt);
  return (META.tablas || []).find(t => { const tn = norm(t.name); return tn && (n.includes(tn) || tn.includes(n)); });
}
function norm(s){return String(s||'').toLowerCase().replace(/[^a-z0-9]/g,'');}
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

function route() {
  document.querySelectorAll('nav a').forEach(a => a.classList.toggle('active', a.getAttribute('href') === location.hash));
  const p = (location.hash || '').split('/');
  if (p[1] === 'abm') return viewAbm(p[2]);
  if (p[1] === 'rep') return viewReport(+p[2]);
  if (p[1] === 'info') return viewInfo(+p[2], +p[3]);
  $('#main').innerHTML = '<p class="muted">Elegí una utilidad del menú de la izquierda.</p>';
}

function tableByKey(k){return (META.tablas||[]).find(t=>t.key===k);}

async function viewAbm(key) {
  const t = tableByKey(key);
  if (!t) { $('#main').innerHTML = '<p>Tabla no encontrada.</p>'; return; }
  const rows = await (await fetch('/api/t/' + key)).json();
  const fields = t.campos;
  let form = `<form class="abm" onsubmit="return saveRow('${key}')">`;
  form += `<input type="hidden" id="f_id">`;
  fields.forEach(f => {
    const req = (f.requerido && f.input !== 'checkbox') ? 'required' : '';
    const tip = f.ayuda ? `title="${esc(f.ayuda).replace(/"/g,'&quot;')}"` : '';
    const star = f.requerido ? ' *' : '';
    const ctl = f.input === 'textarea'
      ? `<textarea id="f_${f.name}" ${req} ${tip}></textarea>`
      : `<input id="f_${f.name}" type="${f.input}" ${req} ${tip}>`;
    form += `<label>${esc(f.label || f.name)}${star}${ctl}</label>`;
  });
  form += `<div class="full"><button type="submit">Guardar</button>
           <button type="button" class="sec" onclick="clearForm()">Limpiar</button></div></form>`;
  let head = '<tr>' + fields.map(f => `<th>${esc(f.label || f.name)}</th>`).join('') + '<th></th></tr>';
  let body = rows.map(r => '<tr>' + fields.map(f => `<td>${esc(r[f.name])}</td>`).join('') +
    `<td><button class="sec" onclick='editRow(${JSON.stringify(r)})'>✎</button>
     <button class="del" onclick="delRow('${key}',${r.id})">🗑</button></td></tr>`).join('');
  $('#main').innerHTML = `<h2>${esc(t.titulo || t.name)} <span class="muted">(${rows.length} registros)</span></h2>
    ${t.descripcion ? `<p class="muted" style="margin-bottom:10px">${esc(t.descripcion)}</p>` : ''}
    ${reglasHtml(t)}${form}
    <table><thead>${head}</thead><tbody>${body || ''}</tbody></table>`;
}

function reglasHtml(t) {
  const r = t.reglas || [], v = t.validaciones || [];
  if (!r.length && !v.length) return '';
  let h = `<div class="card"><b>📋 Reglas de negocio (del sistema original)</b>`;
  if (r.length) h += `<ul style="margin:6px 0 0 18px">` + r.map(x => `<li>${esc(x)}</li>`).join('') + `</ul>`;
  if (v.length) h += `<div class="muted" style="margin-top:6px">⚙️ Validaciones activas: ` +
    v.map(x => esc(x.campo + ' ' + x.op + (x.valor !== undefined ? ' ' + JSON.stringify(x.valor) : ''))).join(' · ') + `</div>`;
  return h + `</div>`;
}

function clearForm(){document.querySelectorAll('form.abm [id^=f_]').forEach(e=>{e.value='';});}
function editRow(r){for(const k in r){const e=document.getElementById('f_'+k);if(e)e.value=r[k];}document.getElementById('f_id').value=r.id;}

async function saveRow(key) {
  const t = tableByKey(key);
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
  viewAbm(key);
  return false;
}
async function delRow(key, id) {
  if (!confirm('¿Borrar el registro?')) return;
  await fetch(`/api/t/${key}/${id}`, { method: 'DELETE' });
  viewAbm(key);
}

async function viewReport(i) {
  const r = META.reportes[i];
  if (!r) { $('#main').innerHTML = '<p>Reporte no encontrado.</p>'; return; }
  if (!r.tabla) { $('#main').innerHTML = `<h2>Reporte: ${esc(r.name)}</h2><div class="card muted">Sin tabla asociada (consulta a definir).</div>`; return; }
  const t = tableByKey(r.tabla);
  const rows = await (await fetch('/api/t/' + r.tabla).catch(()=>({json:()=>[]}))).json();
  const head = '<tr>' + t.campos.map(f => `<th>${esc(f.label||f.name)}</th>`).join('') + '</tr>';
  const body = rows.map(x => '<tr>' + t.campos.map(f => `<td>${esc(x[f.name])}</td>`).join('') + '</tr>').join('');
  $('#main').innerHTML = `<h2>Reporte: ${esc(r.name)} <span class="muted">(${rows.length} filas)</span></h2>
    <table><thead>${head}</thead><tbody>${body}</tbody></table>`;
}

function viewInfo(mi, ii) {
  const it = ((META.menus[mi] || {}).items || [])[ii] || {};
  $('#main').innerHTML = `<h2>${esc(it.texto || 'Pantalla')}</h2>
    <div class="card">Utilidad del sistema original.<br><span class="muted">Acción legacy: ${esc(it.accion || '—')}</span><br><br>
    Pantalla pendiente de implementar la lógica (2ª etapa con IA).</div>`;
}

boot();
'''


def build_app_scaffold(payload):
    """payload = {inventory, title}. Devuelve los bytes del ZIP de la app."""
    inventory = payload.get("inventory") or {}
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
    return buf.getvalue(), meta
