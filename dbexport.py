#!/usr/bin/env python3
"""
Exportación de bases de datos a un directorio externo, con sus índices, y
edición de vinculaciones (relaciones) entre tablas.

Flujo:
1. `export_databases()` — a partir del inventario + ZIP en memoria, crea UNA
   base SQLite por cada `.dbc` del sistema legacy (mismo nombre que el
   `.dbc` original), con sus tablas, índices (.cdx/.idx) y datos reales.
   Las tablas que no pertenecen a ningún `.dbc` (tablas sueltas) van a una
   base aparte `_libres.db`.
2. `list_databases()` — vuelve a inspeccionar un directorio ya generado
   (sin necesitar el ZIP ni el inventario en memoria), para poder retomar
   el paso de vinculación en otra sesión.
3. `load_links()` / `save_links()` — `vinculaciones.json` en el mismo
   directorio: relaciones detectadas en el/los `.dbc` + las que el usuario
   agregue o corrija a mano. Queda ahí para que las apps generadas que
   apunten a ese directorio las puedan leer en runtime.

Solo stdlib (sqlite3, json, os) — sin dependencias externas.
"""
import json
import os
import re
import sqlite3

import scaffold
from servidor import (
    read_dbf_records, parse_cdx_expressions, parse_dbf_structure, VFP_SYSTEM_TABLES,
)

LINKS_FILENAME = "vinculaciones.json"
FREE_TABLES_DB = "_libres"


def _safe_db_name(name):
    name = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(name or "").strip())
    return name or "base"


def _table_columns(fields):
    cols = []
    for f in fields or []:
        cn = scaffold._slug(f.get("name"))
        if not cn or cn == "id" or cn in [c[0] for c in cols]:
            continue
        sql, _inp = scaffold.DBF_SQL.get(f.get("type", "C"), ("TEXT", "text"))
        cols.append((cn, sql))
    return cols


def _scan_all_tables(zf, by_stem):
    """Estructura de TODAS las .dbf del ZIP, leyendo el header real de cada
    una — a diferencia de `inventory["tables"]` (servidor.analyze_zip), que
    recorta a MAX_TABLES porque ese inventario alimenta una pantalla de ABM
    por tabla. Acá no generamos pantallas, así que no hace falta el tope: si
    no se exportan todas las tablas, alguna queda fuera de la base sin que el
    usuario se entere (como pasó con una tabla de `login` más allá del
    tope)."""
    tables = {}
    for stem, exts in by_stem.items():
        if ".dbf" not in exts:
            continue
        if stem in VFP_SYSTEM_TABLES:
            continue
        base = os.path.splitext(os.path.basename(exts[".dbf"]))[0]
        try:
            with zf.open(exts[".dbf"]) as fp:
                head = fp.read(32 + 32 * 256)
            struct = parse_dbf_structure(head)
        except Exception:
            struct = None
        if struct:
            tables[scaffold._slug(base)] = {
                "name": base, "records": struct["records"], "fields": struct["fields"],
            }
    return tables


def _group_tables_by_database(inventory, tables_all):
    """Devuelve {db_name: set(tabla_key)} + el set de tablas sin base (.dbc).
    `tables_all` = claves (slug) de TODAS las tablas conocidas (sin tope)."""
    groups = {}
    asignadas = set()
    for db in inventory.get("databases") or []:
        nombre = db.get("name") or "base"
        miembros = {tk for tk in (db.get("tablas") or []) if tk in tables_all}
        if miembros:
            groups[nombre] = miembros
            asignadas |= miembros
    libres = tables_all - asignadas
    return groups, libres


def _vista_sql(v):
    """SELECT best-effort a partir de las propiedades de una vista VFP
    (Tables/Fields/WhereClause). VFP no guarda el SELECT como texto plano —
    esto es una reconstrucción aproximada, no garantizada."""
    tablas = [t.strip() for t in (v.get("tablas") or "").split(",") if t.strip()]
    if not tablas:
        return None
    campos = (v.get("campos") or "").strip() or "*"
    where = (v.get("where") or "").strip()
    sql = 'SELECT %s FROM %s' % (campos, ", ".join('"%s"' % t for t in tablas))
    if where:
        sql += ' WHERE %s' % where
    return sql


def _export_vistas(inventory, dest_dir, manifiesto):
    """Escribe `vistas.sql` con TODAS las vistas detectadas en los .dbc, para
    no perderlas aunque no tengan tablas propias para exportar. Es best-effort
    (VFP no guarda el SELECT como texto): se documentan crudas las propiedades
    Y se arma un CREATE VIEW aproximado. Cuando todas las tablas de la vista
    quedaron en UNA sola base ya exportada, además se la crea de verdad ahí.
    Devuelve (total_vistas, creadas_de_verdad)."""
    mapa = manifiesto.get("mapa_tabla_base") or {}
    lines = [
        "-- Vistas extraídas de los .dbc del sistema legacy.",
        "-- VFP no guarda las vistas como texto SQL: se reconstruyen best-effort",
        "-- a partir de las propiedades Tables/Fields/WhereClause. Revisar antes",
        "-- de usar en producción. Donde se pudo, ya se crearon (CREATE VIEW)",
        "-- en la base SQLite correspondiente — ver el comentario 'creada en ...'.",
        "",
    ]
    n_total = n_creadas = 0
    for db in inventory.get("databases") or []:
        for v in (db.get("vistas") or []):
            if not isinstance(v, dict):
                v = {"nombre": str(v)}
            nombre = v.get("nombre") or ""
            if not nombre:
                continue
            n_total += 1
            slug = scaffold._slug(nombre)
            lines.append("-- " + "-" * 60)
            lines.append("-- Vista: %s  (base origen: %s)" % (nombre, db.get("name") or "?"))
            if v.get("tablas"):
                lines.append("-- Tablas: %s" % v["tablas"])
            if v.get("campos"):
                lines.append("-- Campos: %s" % v["campos"])
            if v.get("where"):
                lines.append("-- Where:  %s" % v["where"])
            extra = {k: val for k, val in (v.get("propiedades") or {}).items()
                     if k not in ("Tables", "Fields", "WhereClause")}
            for k, val in list(extra.items())[:15]:
                lines.append("--   %s = %s" % (k, val))

            sql = _vista_sql(v)
            if not sql:
                lines.append("-- (sin info de tablas/campos suficiente para armar el SELECT;"
                              " revisar las propiedades de arriba a mano)")
                if v.get("raw_property"):
                    lines.append("-- --- texto crudo de PROPERTY (para ajustar el parser) ---")
                    for ln in str(v["raw_property"]).splitlines() or [str(v["raw_property"])]:
                        lines.append("--   " + ln)
                if v.get("raw_code"):
                    lines.append("-- --- texto crudo de CODE ---")
                    for ln in str(v["raw_code"]).splitlines() or [str(v["raw_code"])]:
                        lines.append("--   " + ln)
                lines.append("")
                continue

            tablas_slug = [scaffold._slug(t) for t in (v.get("tablas") or "").split(",") if t.strip()]
            archivos = {mapa.get(t) for t in tablas_slug}
            archivo = next(iter(archivos)) if len(archivos) == 1 and None not in archivos else None
            creada = False
            if archivo:
                try:
                    conn = sqlite3.connect(os.path.join(dest_dir, archivo))
                    conn.execute('CREATE VIEW IF NOT EXISTS "%s" AS %s' % (slug, sql))
                    conn.commit()
                    conn.close()
                    creada = True
                    n_creadas += 1
                except sqlite3.Error as e:
                    lines.append("-- (no se pudo crear automáticamente: %s)" % e)
            lines.append('CREATE VIEW IF NOT EXISTS "%s" AS %s;' % (slug, sql))
            if creada:
                lines.append("-- creada en %s" % archivo)
            lines.append("")

    if n_total:
        with open(os.path.join(dest_dir, "vistas.sql"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    return n_total, n_creadas


def _scan_db_objects(path):
    """Tablas y vistas que YA existen en un .db (consulta sqlite_master), con
    columnas y cantidad de filas — para listarlas/previsualizarlas en la UI."""
    tablas, vistas = [], []
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
                "SELECT name, type, sql FROM sqlite_master "
                "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'"):
            try:
                registros = conn.execute('SELECT COUNT(*) FROM "%s"' % row["name"]).fetchone()[0]
                campos = [r[1] for r in conn.execute('PRAGMA table_info("%s")' % row["name"])
                          if r[1] != "id"]
            except sqlite3.Error:
                registros, campos = 0, []
            item = {"key": row["name"], "nombre": row["name"], "registros": registros,
                    "campos": campos}
            (vistas if row["type"] == "view" else tablas).append(item)
        conn.close()
    except sqlite3.Error:
        pass
    return (sorted(tablas, key=lambda x: x["key"]), sorted(vistas, key=lambda x: x["key"]))


def peek(dest_dir, archivo, nombre, limit=100):
    """Primeras filas + columnas de una tabla o vista ya generada, para
    verla/probarla desde la UI sin abrir un cliente SQLite aparte."""
    archivo = os.path.basename(str(archivo or ""))
    path = os.path.join(dest_dir, archivo)
    if not os.path.isfile(path):
        raise ValueError("No existe '%s' en '%s'" % (archivo, dest_dir))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        existe = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
            (nombre,)).fetchone()
        if not existe:
            raise ValueError("'%s' no existe en %s" % (nombre, archivo))
        ident = str(nombre).replace('"', '""')
        cols = [r[1] for r in conn.execute('PRAGMA table_info("%s")' % ident)]
        limit = max(1, min(int(limit or 100), 500))
        rows = [dict(r) for r in conn.execute('SELECT * FROM "%s" LIMIT ?' % ident, (limit,))]
        try:
            total = conn.execute('SELECT COUNT(*) FROM "%s"' % ident).fetchone()[0]
        except sqlite3.Error:
            total = len(rows)
    finally:
        conn.close()
    return {"columns": cols, "rows": rows, "total": total}


def read_vistas_sql(dest_dir):
    path = os.path.join(dest_dir, "vistas.sql")
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def export_databases(raw_zip_bytes, inventory, dest_dir):
    """Crea (o actualiza) en `dest_dir` una base SQLite por `.dbc`, con sus
    tablas, índices y datos reales. Devuelve el manifiesto generado.

    `manifiesto` = {
      "dir": dest_dir,
      "bases": [{"nombre", "archivo", "tablas": [{"key","nombre","registros","campos":[...]}]}],
      "mapa_tabla_base": {tabla_key: archivo_db},
    }
    """
    import io
    import zipfile

    os.makedirs(dest_dir, exist_ok=True)

    zf = zipfile.ZipFile(io.BytesIO(raw_zip_bytes))
    names = [n for n in zf.namelist() if not n.endswith("/")]
    by_stem = {}
    file_sizes = {n: zf.getinfo(n).file_size for n in names}
    for n in names:
        base = os.path.basename(n)
        stem, ext = os.path.splitext(base)
        sk, ek = stem.lower(), ext.lower()
        prev = by_stem.get(sk, {}).get(ek)
        if prev is None or file_sizes.get(n, 0) > file_sizes.get(prev, 0):
            by_stem.setdefault(sk, {})[ek] = n

    # Todas las .dbf del ZIP (sin el tope MAX_TABLES del inventario de
    # pantallas), para no perder tablas que están más allá de ese tope.
    tables_by_key = _scan_all_tables(zf, by_stem)
    groups, libres = _group_tables_by_database(inventory, set(tables_by_key))
    if libres:
        groups[FREE_TABLES_DB] = libres

    # Bases .dbc que no tienen ninguna tabla propia (sólo vistas/stored procs,
    # p.ej. "vistgest" con 50 vistas armadas sobre tablas de otras bases): no
    # generan un .db (no hay nada que exportar como tabla), pero igual se
    # listan para que no desaparezcan del panel.
    sin_tablas = []
    for db in inventory.get("databases") or []:
        nombre = db.get("name") or "base"
        if nombre not in groups:
            sin_tablas.append({
                "nombre": nombre,
                "vistas": len(db.get("vistas") or []),
                "relaciones": len(db.get("relaciones") or []),
            })

    manifiesto = {"dir": dest_dir, "bases": [], "sin_tablas": sin_tablas, "mapa_tabla_base": {}}

    for db_nombre, tkeys in groups.items():
        archivo = _safe_db_name(db_nombre) + ".db"
        path = os.path.join(dest_dir, archivo)
        conn = sqlite3.connect(path)
        tablas_info = []
        try:
            for tkey in sorted(tkeys):
                t = tables_by_key.get(tkey)
                if not t:
                    continue
                cols = _table_columns(t.get("fields"))
                if not cols:
                    continue
                defs = ", ".join('"%s" %s' % (cn, ct) for cn, ct in cols)
                conn.execute(
                    'CREATE TABLE IF NOT EXISTS "%s" '
                    '(id INTEGER PRIMARY KEY AUTOINCREMENT, %s)' % (tkey, defs))

                entry = by_stem.get((t.get("name") or "").lower(), {})
                field_slugs = [cn for cn, _ in cols]

                # Índices reales del .cdx/.idx original.
                idx_defs, seen = [], set()
                for ext in (".cdx", ".idx"):
                    if ext in entry:
                        try:
                            for idxcols in parse_cdx_expressions(zf.read(entry[ext]), field_slugs):
                                sig = tuple(idxcols)
                                if idxcols and sig not in seen:
                                    seen.add(sig)
                                    idx_defs.append(idxcols)
                        except Exception:
                            pass
                valid_cols = {cn for cn, _ in cols}
                for i, idxcols in enumerate(idx_defs[:16]):
                    idxcols = [c for c in idxcols if c in valid_cols]
                    if not idxcols:
                        continue
                    try:
                        conn.execute(
                            'CREATE INDEX IF NOT EXISTS "ix_%s_%d" ON "%s" (%s)' % (
                                tkey, i, tkey, ",".join('"%s"' % c for c in idxcols)))
                    except sqlite3.Error:
                        pass

                # Datos reales (.dbf + memo .fpt).
                registros = 0
                if ".dbf" in entry:
                    try:
                        dbf_bytes = zf.read(entry[".dbf"])
                        fpt_bytes = zf.read(entry[".fpt"]) if ".fpt" in entry else b""
                        rows = read_dbf_records(dbf_bytes, fpt_bytes)
                    except Exception:
                        rows = []
                    if rows and conn.execute('SELECT COUNT(*) FROM "%s"' % tkey).fetchone()[0] == 0:
                        for r in rows:
                            use = [k for k in valid_cols if k in r]
                            if not use:
                                continue
                            try:
                                conn.execute(
                                    'INSERT INTO "%s" (%s) VALUES (%s)' % (
                                        tkey, ",".join('"%s"' % x for x in use),
                                        ",".join("?" * len(use))),
                                    [r[x] for x in use])
                            except sqlite3.Error:
                                pass
                    registros = conn.execute('SELECT COUNT(*) FROM "%s"' % tkey).fetchone()[0]
                conn.commit()

                tablas_info.append({
                    "key": tkey, "nombre": t.get("name"), "registros": registros,
                    "campos": field_slugs,
                })
                manifiesto["mapa_tabla_base"][tkey] = archivo
        finally:
            conn.close()

        if tablas_info:
            manifiesto["bases"].append({
                "nombre": db_nombre, "archivo": archivo, "tablas": tablas_info,
            })

    n_vistas, n_vistas_creadas = _export_vistas(inventory, dest_dir, manifiesto)
    if n_vistas:
        manifiesto["vistas"] = {"total": n_vistas, "creadas": n_vistas_creadas,
                                "archivo": "vistas.sql"}
    # Adjuntamos a cada base las vistas que efectivamente se crearon ahí, para
    # poder verlas/probarlas igual que las tablas.
    for b in manifiesto["bases"]:
        _tablas_db, vistas_db = _scan_db_objects(os.path.join(dest_dir, b["archivo"]))
        if vistas_db:
            b["vistas"] = vistas_db

    # Sembramos vinculaciones.json con las relaciones que ya trae el .dbc,
    # sin pisar las que el usuario ya haya agregado/corregido a mano.
    existentes = load_links(dest_dir)
    ya = {(r.get("tabla_origen"), r.get("campo_origen"),
           r.get("tabla_destino"), r.get("campo_destino")) for r in existentes}
    for db in inventory.get("databases") or []:
        for rel in db.get("relaciones") or []:
            sig = (rel.get("child_table"), rel.get("child_field"),
                   rel.get("parent_table"), rel.get("parent_field") or "id")
            if sig in ya or not sig[0] or not sig[2]:
                continue
            ya.add(sig)
            existentes.append({
                "tabla_origen": sig[0], "campo_origen": sig[1],
                "tabla_destino": sig[2], "campo_destino": sig[3],
                "origen": "dbc",
            })
    save_links(dest_dir, existentes)

    return manifiesto


def list_databases(dest_dir):
    """Inspecciona un directorio ya generado (sin ZIP ni inventario en
    memoria): lee cada .db con sqlite3 y devuelve la misma forma que
    `export_databases` (tablas Y vistas que ya existan en el archivo)."""
    manifiesto = {"dir": dest_dir, "bases": [], "mapa_tabla_base": {}}
    if not os.path.isdir(dest_dir):
        return manifiesto
    for archivo in sorted(os.listdir(dest_dir)):
        if not archivo.endswith(".db"):
            continue
        tablas_info, vistas_info = _scan_db_objects(os.path.join(dest_dir, archivo))
        for t in tablas_info:
            manifiesto["mapa_tabla_base"][t["key"]] = archivo
        if tablas_info or vistas_info:
            base = {"nombre": os.path.splitext(archivo)[0], "archivo": archivo,
                    "tablas": tablas_info}
            if vistas_info:
                base["vistas"] = vistas_info
            manifiesto["bases"].append(base)
    return manifiesto


def load_links(dest_dir):
    path = os.path.join(dest_dir, LINKS_FILENAME)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_links(dest_dir, links):
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, LINKS_FILENAME)
    limpio = []
    for r in links or []:
        to = scaffold._slug(r.get("tabla_origen"))
        co = scaffold._slug(r.get("campo_origen"))
        td = scaffold._slug(r.get("tabla_destino"))
        cd = scaffold._slug(r.get("campo_destino")) if r.get("campo_destino") else "id"
        if not to or not td:
            continue
        limpio.append({"tabla_origen": to, "campo_origen": co,
                       "tabla_destino": td, "campo_destino": cd,
                       "origen": r.get("origen") or "manual"})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(limpio, f, ensure_ascii=False, indent=2)
    return limpio
