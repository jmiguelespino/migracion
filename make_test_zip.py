"""Genera test_recetas.zip: sistema VFP sintético con todos los tipos de archivo."""
import struct, io, zipfile, datetime, os

BLOCK_SIZE = 64


class MemoDB:
    """Construye un archivo memo .fpt/.sct/.dct compatible con VFP."""
    def __init__(self):
        self._blks = bytearray()
        self._next = 1  # bloque 1 = primero después del header

    def add(self, text):
        """Agrega un memo y devuelve el número de bloque inicial."""
        if not text:
            return 0
        content = text.encode('latin-1', errors='replace') if isinstance(text, str) else bytes(text)
        entry = struct.pack('>II', 0xFFFF0800, len(content)) + content
        pad = (-len(entry)) % BLOCK_SIZE
        entry += b'\x00' * pad
        blk = self._next
        self._blks += entry
        self._next += len(entry) // BLOCK_SIZE
        return blk

    def to_bytes(self):
        hdr = struct.pack('>II', self._next, BLOCK_SIZE) + b'\x00' * (BLOCK_SIZE - 8)
        return hdr + bytes(self._blks)


def dbf_header(num_records, fields):
    header_size = 32 + 32 * len(fields) + 1
    record_size = 1 + sum(f[2] for f in fields)
    now = datetime.date.today()
    h = struct.pack('<BBBB', 3, now.year - 1900, now.month, now.day)
    h += struct.pack('<I', num_records)
    h += struct.pack('<HH', header_size, record_size)
    h += b'\x00' * 20
    for name, typ, length, dec in fields:
        fd = name.encode('ascii').ljust(11, b'\x00')[:11]
        fd += typ.encode('ascii')
        fd += b'\x00' * 4
        fd += bytes([length, dec])
        fd += b'\x00' * 14
        h += fd
    return h + b'\r'


def dbf_rec(fields, values):
    """Serializa un registro DBF. Para tipo 'M', val = bloque (int). Para 'I', val = int."""
    rec = b' '  # byte de borrado = espacio → no borrado
    for (name, typ, length, dec), val in zip(fields, values):
        if typ == 'C':
            rec += str(val).encode('latin-1', errors='replace').ljust(length)[:length]
        elif typ == 'N':
            if dec > 0:
                s = f'{float(val or 0):.{dec}f}'.rjust(length)
            else:
                s = str(int(val or 0)).rjust(length)
            rec += s.encode('ascii')[:length]
        elif typ == 'I':
            rec += struct.pack('<i', int(val or 0))
        elif typ == 'L':
            rec += b'T' if val else b'F'
        elif typ == 'D':
            rec += str(val).replace('-', '').encode('ascii')[:8].ljust(8)
        elif typ == 'M':
            # Puntero VFP: 4 bytes LE + relleno hasta `length`
            blk = int(val) if val else 0
            rec += struct.pack('<I', blk) + b'\x00' * (length - 4)
        else:
            rec += b' ' * length
    return rec


def make_dbf(fields, rows):
    h = dbf_header(len(rows), fields)
    data = h + b''.join(dbf_rec(fields, r) for r in rows)
    return data + b'\x1a'


# ============================================================
# DBF CLIENTES
# ============================================================
CLI_F = [
    ('ID',       'N', 5,  0),
    ('NOMBRE',   'C', 40, 0),
    ('APELLIDO', 'C', 40, 0),
    ('TELEFONO', 'C', 15, 0),
    ('EMAIL',    'C', 60, 0),
    ('ACTIVO',   'L', 1,  0),
    ('FECHAALT', 'D', 8,  0),
]
CLI_R = [
    (1, 'Juan',   'Perez',  '1122334455', 'juan@ej.com',   True,  '20230101'),
    (2, 'Maria',  'Garcia', '9988776655', 'maria@ej.com',  True,  '20230215'),
    (3, 'Carlos', 'Lopez',  '5544332211', 'carlos@ej.com', False, '20220101'),
]

# ============================================================
# DBF PEDIDOS + FPT
# ============================================================
PED_F = [
    ('ID',     'N', 5,  0),
    ('ID_CLI', 'N', 5,  0),
    ('FECHA',  'D', 8,  0),
    ('TOTAL',  'N', 10, 2),
    ('ESTADO', 'C', 20, 0),
    ('NOTAS',  'M', 4,  0),  # ← 4-byte LE memo ptr
]

def make_pedidos():
    fpt = MemoDB()
    blk1 = fpt.add('Entrega urgente')
    blk2 = fpt.add('Cliente espera confirmacion')
    blk3 = fpt.add('')
    rows = [
        (1, 1, '20230115', 150.00, 'Entregado', blk1),
        (2, 1, '20230220', 320.50, 'Pendiente', blk2),
        (3, 2, '20230310',  85.00, 'Enviado',   blk3),
    ]
    return make_dbf(PED_F, rows), fpt.to_bytes()

# ============================================================
# DBF PRODUCTOS
# ============================================================
PRD_F = [
    ('ID',       'N', 5,  0),
    ('CODIGO',   'C', 20, 0),
    ('NOMBRE',   'C', 60, 0),
    ('PRECIO',   'N', 10, 2),
    ('STOCK',    'N', 5,  0),
    ('CATEGORIA','C', 30, 0),
]
PRD_R = [
    (1, 'P001', 'Harina 1kg',     45.50, 100, 'Almacen'),
    (2, 'P002', 'Azucar 1kg',     38.00,  80, 'Almacen'),
    (3, 'P003', 'Aceite Girasol', 120.00, 50, 'Aceites'),
]

# ============================================================
# SCX / SCT  (formulario frmClientes)
# ============================================================
SCX_F = [
    ('PLATFORM',   'C', 8,   0),
    ('UNIQUEID',   'C', 10,  0),
    ('TIMESTAMP',  'N', 10,  0),
    ('CLASS',      'C', 40,  0),
    ('CLASSLOC',   'C', 40,  0),
    ('BASECLASS',  'C', 32,  0),
    ('OBJNAME',    'C', 77,  0),
    ('PARENT',     'C', 77,  0),
    ('PROPERTIES', 'M', 4,   0),  # 4-byte LE memo ptr
    ('PROTECTED',  'C', 1,   0),
    ('METHODS',    'M', 4,   0),
    ('OBJCODE',    'M', 4,   0),
    ('NAME',       'C', 128, 0),
    ('INCLUDE',    'C', 1,   0),
]

def make_scx():
    sct = MemoDB()
    # Memos de PROPERTIES para cada control
    p_form    = sct.add('Caption = "Clientes"\r\nTop = 0\r\nLeft = 0\r\n')
    p_nombre  = sct.add('ControlSource = "clientes.nombre"\r\nCaption = "Nombre"\r\nTop = 50\r\n')
    p_apell   = sct.add('ControlSource = "clientes.apellido"\r\nCaption = "Apellido"\r\nTop = 80\r\n')
    p_tel     = sct.add('ControlSource = "clientes.telefono"\r\nCaption = "Telefono"\r\nTop = 110\r\n')
    p_btn     = sct.add('Caption = "Guardar"\r\nTop = 150\r\n')

    rows = [
        ('WINDOWS','A001',1000,'Form',       '','Form',           'frmClientes','',           p_form,  '',0,0,'',''),
        ('WINDOWS','A002',1001,'textbox',    '','TextBox',        'txtNombre',  'frmClientes', p_nombre,'',0,0,'',''),
        ('WINDOWS','A003',1002,'textbox',    '','TextBox',        'txtApellido','frmClientes', p_apell, '',0,0,'',''),
        ('WINDOWS','A004',1003,'textbox',    '','TextBox',        'txtTelefono','frmClientes', p_tel,   '',0,0,'',''),
        ('WINDOWS','A005',1004,'CommandButton','','CommandButton','cmdGuardar', 'frmClientes', p_btn,   '',0,0,'',''),
    ]
    return make_dbf(SCX_F, rows), sct.to_bytes()

# ============================================================
# MNX / MNT  (menú)
# ============================================================
MNX_F = [
    ('OBJTYPE',  'N', 2,   0),
    ('OBJCODE',  'N', 2,   0),
    ('NAME',     'M', 4,   0),
    ('ALIAS',    'C', 10,  0),
    ('PROMPT',   'M', 4,   0),
    ('COMMAND',  'C', 254, 0),
    ('MESSAGE',  'M', 4,   0),
    ('KEYNAME',  'M', 4,   0),
    ('KEYLABEL', 'C', 40,  0),
    ('SKIPFOR',  'M', 4,   0),
    ('MARK',     'C', 1,   0),
    ('ENABLED',  'L', 1,   0),
    ('CHECKED',  'L', 1,   0),
    ('PROCEDURE','M', 4,   0),
    ('SETUPCODE','M', 4,   0),
    ('CLEANCODE','M', 4,   0),
    ('RESERVED1','C', 1,   0),
    ('RESERVED2','C', 20,  0),
]

def make_mnx():
    mnt = MemoDB()
    n_cli  = mnt.add('mnuClientes')
    pr_cli = mnt.add('Clientes')
    n_ped  = mnt.add('mnuPedidos')
    pr_ped = mnt.add('Pedidos')
    n_rpt  = mnt.add('mnuReportes')
    pr_rpt = mnt.add('Reportes')
    n_abm  = mnt.add('ABM Clientes')
    pr_abm = mnt.add('ABM Clientes')
    n_r1   = mnt.add('Rpt Clientes')
    pr_r1  = mnt.add('Reporte de Clientes')
    n_r2   = mnt.add('Rpt Pedidos')
    pr_r2  = mnt.add('Reporte de Pedidos')
    setup  = mnt.add('SET SYSMENU TO\r\nSET SYSMENU AUTOMATIC')

    rows = [
        # PAD Clientes
        (3, 0, n_cli, '', pr_cli, '', 0,0,'',0,'',True,False,0,setup,0,'',''),
        # BAR ABM Clientes
        (5, 0, n_abm, '', pr_abm, 'DO FORM frmClientes', 0,0,'Ctrl+C',0,'',True,False,0,0,0,'',''),
        # PAD Pedidos
        (3, 0, n_ped, '', pr_ped, '', 0,0,'',0,'',True,False,0,0,0,'',''),
        # PAD Reportes
        (3, 0, n_rpt, '', pr_rpt, '', 0,0,'',0,'',True,False,0,0,0,'',''),
        # BAR Rpt Clientes
        (5, 0, n_r1,  '', pr_r1,  'REPORT FORM rptClientes PREVIEW', 0,0,'',0,'',True,False,0,0,0,'',''),
        # BAR Rpt Pedidos
        (5, 0, n_r2,  '', pr_r2,  'REPORT FORM rptPedidos  PREVIEW', 0,0,'',0,'',True,False,0,0,0,'',''),
    ]
    return make_dbf(MNX_F, rows), mnt.to_bytes()

# ============================================================
# DBC / DCT  (Database Container)
# ============================================================
DBC_F = [
    ('OBJECTID',   'I', 4,   0),   # Integer 4 bytes LE signed
    ('PARENTID',   'I', 4,   0),
    ('OBJECTTYPE', 'C', 10,  0),
    ('OBJECTNAME', 'C', 128, 0),
    ('PROPERTY',   'M', 4,   0),
    ('CODE',       'M', 4,   0),
    ('RIINFO',     'C', 6,   0),
    ('USER',       'M', 4,   0),
]

def make_dbc():
    dct = MemoDB()
    # Database
    db_prop  = dct.add('DBVersion = 0x00000060\r\n')
    # Table clientes
    cli_prop = dct.add('Caption = "Clientes"\r\nPath = "clientes.dbf"\r\n')
    # Field id
    id_prop  = dct.add('Caption = "Identificador"\r\nDefaultValue = "0"\r\n')
    # Field nombre
    nom_prop = dct.add(
        'Caption = "Nombre del cliente"\r\n'
        'RuleExpression = "!EMPTY(nombre)"\r\n'
        'RuleText = "Nombre obligatorio"\r\n'
    )
    # Table pedidos
    ped_prop = dct.add('Caption = "Pedidos"\r\nPath = "pedidos.dbf"\r\n')
    # Field id_cli
    fk_prop  = dct.add('Caption = "ID Cliente FK"\r\n')
    # Relation clientes(id) → pedidos(id_cli)
    rel_prop = dct.add(
        'ParentTagName = "id"\r\n'
        'ChildTableName = "pedidos"\r\n'
        'ChildTagName = "id_cli"\r\n'
        'RelType = "1"\r\n'
    )
    # Table productos
    prd_prop = dct.add('Caption = "Productos"\r\nPath = "productos.dbf"\r\n')

    # OBJECTID, PARENTID, TYPE, NAME, PROPERTY, CODE, RIINFO, USER
    rows = [
        (1, 0, 'Database',  'Recetas',   db_prop,  0, '', 0),
        (2, 1, 'Table',     'clientes',  cli_prop, 0, '', 0),
        (3, 2, 'Field',     'id',        id_prop,  0, '', 0),
        (4, 2, 'Field',     'nombre',    nom_prop, 0, '', 0),
        (5, 1, 'Table',     'pedidos',   ped_prop, 0, '', 0),
        (6, 5, 'Field',     'id_cli',    fk_prop,  0, '', 0),
        (7, 2, 'Relation',  '',          rel_prop, 0, '', 0),  # PARENTID=2 → clientes
        (8, 1, 'Table',     'productos', prd_prop, 0, '', 0),
    ]
    return make_dbf(DBC_F, rows), dct.to_bytes()

# ============================================================
# FRX  (reporte)
# ============================================================
FRX_F = [
    ('PLATFORM',  'C', 8,  0),
    ('UNIQUEID',  'C', 10, 0),
    ('TIMESTAMP', 'N', 10, 0),
    ('OBJTYPE',   'N', 2,  0),
    ('OBJCODE',   'N', 2,  0),
    ('NAME',      'C', 77, 0),
    ('EXPR',      'M', 4,  0),
    ('VPOS',      'N', 7,  3),
    ('HPOS',      'N', 7,  3),
    ('HEIGHT',    'N', 7,  3),
    ('WIDTH',     'N', 7,  3),
]
FRX_R = [
    ('WINDOWS','R001',1, 1,0,'rptClientes', 0, 0,  0, 200, 300),
    ('WINDOWS','R002',2, 5,0,'nombre',      0, 20, 10, 10, 100),
    ('WINDOWS','R003',3, 5,0,'telefono',    0, 20,120, 10,  80),
]

# ============================================================
# PJX / PJT  (proyecto)
# ============================================================
PJX_F = [
    ('OBJTYPE',  'N', 2,  0),
    ('NAME',     'M', 4,  0),
    ('TIMESTAMP','N', 10, 0),
    ('HOMEDIR',  'M', 4,  0),
    ('EXCLUDE',  'L', 1,  0),
    ('FLAGS',    'N', 5,  0),
    ('RESERVED', 'C', 5,  0),
]

def make_pjx():
    pjt = MemoDB()
    n_proj = pjt.add('RECETAS')
    h_root = pjt.add('.\\')
    n_main = pjt.add('prg\\main.prg')
    n_cli  = pjt.add('prg\\clientes.prg')
    n_frx  = pjt.add('frx\\rptClientes.frx')
    n_scx  = pjt.add('scx\\frmClientes.scx')
    n_mpr  = pjt.add('menu\\menu.mpr')

    rows = [
        (1, n_proj, 1000, h_root, False, 0, ''),  # Proyecto
        (2, n_main, 1001, h_root, False, 0, ''),  # PRG main
        (2, n_cli,  1002, h_root, False, 0, ''),  # PRG clientes
        (3, n_frx,  1003, h_root, False, 0, ''),  # FRX
        (5, n_scx,  1004, h_root, False, 0, ''),  # SCX
        (6, n_mpr,  1005, h_root, False, 0, ''),  # MPR
    ]
    return make_dbf(PJX_F, rows), pjt.to_bytes()

# ============================================================
# Archivos de texto
# ============================================================
MPR_CONTENT = b"""\
*-- MENU.MPR generado por VFP
SET SYSMENU TO
SET SYSMENU AUTOMATIC

DEFINE PAD _01 OF _MSYSMENU PROMPT "\\<Clientes" KEY F2, "" MESSAGE "Gestion de clientes"
DEFINE PAD _02 OF _MSYSMENU PROMPT "\\<Pedidos"  KEY F3, "" MESSAGE "Gestion de pedidos"
DEFINE PAD _03 OF _MSYSMENU PROMPT "\\<Reportes" KEY F4, "" MESSAGE "Reportes del sistema"
DEFINE PAD _04 OF _MSYSMENU PROMPT "\\<Salir"    KEY Alt+F4, ""

ON PAD _01 OF _MSYSMENU ACTIVATE POPUP _01POPUP
ON PAD _02 OF _MSYSMENU ACTIVATE POPUP _02POPUP
ON PAD _03 OF _MSYSMENU ACTIVATE POPUP _03POPUP
ON PAD _04 OF _MSYSMENU DO salir_sistema

DEFINE POPUP _01POPUP MARGIN RELATIVE SHADOW COLOR SCHEME 4
DEFINE BAR 1 OF _01POPUP PROMPT "ABM Clientes"    MESSAGE "Alta/Baja/Modificacion"
DEFINE BAR 2 OF _01POPUP PROMPT "Buscar cliente"  MESSAGE "Busqueda de clientes"
ON SELECTION BAR 1 OF _01POPUP DO FORM frmClientes
ON SELECTION BAR 2 OF _01POPUP DO FORM frmBuscarCliente

DEFINE POPUP _02POPUP MARGIN RELATIVE SHADOW COLOR SCHEME 4
DEFINE BAR 1 OF _02POPUP PROMPT "ABM Pedidos"
DEFINE BAR 2 OF _02POPUP PROMPT "Ver pedido"
ON SELECTION BAR 1 OF _02POPUP DO FORM frmPedidos
ON SELECTION BAR 2 OF _02POPUP DO FORM frmDetallePedido

DEFINE POPUP _03POPUP MARGIN RELATIVE SHADOW COLOR SCHEME 4
DEFINE BAR 1 OF _03POPUP PROMPT "Rpt. Clientes"
DEFINE BAR 2 OF _03POPUP PROMPT "Rpt. Pedidos"
ON SELECTION BAR 1 OF _03POPUP REPORT FORM rptClientes PREVIEW
ON SELECTION BAR 2 OF _03POPUP REPORT FORM rptPedidos  PREVIEW

PROCEDURE salir_sistema
  IF MESSAGEBOX("Salir?",4+32+256,"Confirmar") = 6
    QUIT
  ENDIF
ENDPROC
"""

PRG_MAIN = b"""\
*-- MAIN.PRG - Programa principal
*-- Sistema RECETAS v2.1
SET TALK OFF
SET ECHO OFF
SET SAFETY OFF
SET DELETED ON

DO MENU_PRINCIPAL

PROCEDURE MENU_PRINCIPAL
  DO FORM frmClientes
ENDPROC

PROCEDURE VALIDAR_CLIENTE(nID)
  LOCAL lValido
  lValido = .T.
  SELECT clientes
  SEEK nID
  IF !FOUND()
    MESSAGEBOX("Cliente no encontrado", 0+48, "Error")
    lValido = .F.
  ENDIF
  RETURN lValido
ENDPROC
"""

PRG_CLIENTES = b"""\
*-- CLIENTES.PRG - Modulo de clientes
PROCEDURE ALTA_CLIENTE(cNombre, cApellido, cTel, cEmail)
  IF EMPTY(cNombre)
    MESSAGEBOX("El nombre es obligatorio", 0+48, "Error")
    RETURN .F.
  ENDIF
  IF LEN(ALLTRIM(cTel)) < 7
    MESSAGEBOX("Telefono invalido (minimo 7 digitos)", 0+48, "Error")
    RETURN .F.
  ENDIF
  INSERT INTO clientes (nombre, apellido, telefono, email, activo, fechaalt) ;
    VALUES (cNombre, cApellido, cTel, cEmail, .T., DATE())
  RETURN .T.
ENDPROC

PROCEDURE BAJA_CLIENTE(nID)
  IF !VALIDAR_CLIENTE(nID)
    RETURN .F.
  ENDIF
  UPDATE clientes SET activo = .F. WHERE id = nID
  RETURN .T.
ENDPROC
"""

H_INCLUDE = b"""\
*-- INCLUYE.H - Constantes del sistema RECETAS
#DEFINE VERSION_SISTEMA  "2.1"
#DEFINE MAX_REGISTROS    9999
#DEFINE MIN_TELEFONO     7
#DEFINE ESTADO_ACTIVO    .T.
#DEFINE MSG_ERROR_NOMBRE "El nombre es obligatorio"
#DEFINE MSG_ERROR_TEL    "Telefono invalido"
"""

TXT_NOTAS = b"""\
SISTEMA RECETAS v2.1
===================
Desarrollado para Distribuidora El Sol

Modulos:
- Clientes: ABM completo
- Pedidos: vinculados a clientes
- Productos: stock y precios
- Reportes: por cliente, por fecha, stock
"""

# ============================================================
# ARMAR EL ZIP
# ============================================================
out = '/home/user/migracion/test_recetas.zip'

ped_dbf, ped_fpt = make_pedidos()
scx_dbf, sct     = make_scx()
mnx_dbf, mnt     = make_mnx()
dbc_dbf, dct     = make_dbc()
pjx_dbf, pjt     = make_pjx()

with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
    z.writestr('RECETAS.PJX',          pjx_dbf)
    z.writestr('RECETAS.PJT',          pjt)
    z.writestr('datos/RECETAS.DBC',    dbc_dbf)
    z.writestr('datos/RECETAS.DCT',    dct)
    z.writestr('datos/CLIENTES.DBF',   make_dbf(CLI_F, CLI_R))
    z.writestr('datos/PEDIDOS.DBF',    ped_dbf)
    z.writestr('datos/PEDIDOS.FPT',    ped_fpt)
    z.writestr('datos/PRODUCTOS.DBF',  make_dbf(PRD_F, PRD_R))
    z.writestr('scx/FRMCLIENTES.SCX',  scx_dbf)
    z.writestr('scx/FRMCLIENTES.SCT',  sct)
    z.writestr('menu/MENU.MPR',        MPR_CONTENT)
    z.writestr('menu/MENU.MNX',        mnx_dbf)
    z.writestr('menu/MENU.MNT',        mnt)
    z.writestr('frx/RPTCLIENTES.FRX',  make_dbf(FRX_F, FRX_R))
    z.writestr('prg/MAIN.PRG',         PRG_MAIN)
    z.writestr('prg/CLIENTES.PRG',     PRG_CLIENTES)
    z.writestr('prg/INCLUYE.H',        H_INCLUDE)
    z.writestr('NOTAS.TXT',            TXT_NOTAS)

print(f'ZIP: {os.path.getsize(out)} bytes')
with zipfile.ZipFile(out) as z2:
    for n in z2.namelist():
        print(f'  {n}')
