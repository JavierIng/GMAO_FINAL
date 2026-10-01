"""
SIGMA-IA Enterprise - GMAO para entorno industrial (Terrassa, Barcelona)

Modelo de dominio (jerarquía típica de un GMAO):
    Cliente 1─N Sede (dirección geocodificada) 1─N Activo (equipo) 1─N Plan de mantenimiento preventivo
    Activo 1─N Orden de Trabajo (preventiva / correctiva / avería)  ─ usa → Inventario (repuestos)

Capas:
  1. Persistencia  -> SQLite (context manager, FK, CHECK, vista v_ot)
  2. Servicios     -> autenticación, geocodificación, generación de preventivos, optimizador de
                      rutas multi-técnico, cierre de OT con stock, KPIs (MTTR, MTBF, disponibilidad)
  3. Presentación  -> Streamlit (vistas por rol; navegación con st.radio, no st.tabs)
"""
import hashlib
import hmac
import json
import math
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from datetime import time as dtime

import pandas as pd
import streamlit as st

# Dependencias opcionales: si faltan, la app arranca igualmente y avisa en pantalla.
try:
    from fpdf import FPDF
except ImportError:
    FPDF = None
try:
    from streamlit_calendar import calendar
except ImportError:
    calendar = None
try:
    import pydeck as pdk
except ImportError:
    pdk = None
try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Europe/Madrid")
except Exception:
    TZ = None

# ==========================================
# CONFIGURACIÓN Y CONSTANTES
# ==========================================
st.set_page_config(page_title="SIGMA-IA | Terrassa Enterprise", layout="wide", page_icon="⚙️")
st.markdown(
    """
    <style>
    #MainMenu {visibility: hidden;} footer {visibility: hidden;}
    .stButton>button { border-radius: 8px; font-weight: bold; }
    </style>
    """,
    unsafe_allow_html=True,
)

DB_PATH = "gmao_terrassa.db"
BASE_LAT, BASE_LON = 41.5631, 2.0100       # Central en Terrassa
JORNADA_H = 8.0
INICIO_JORNADA = "08:00"
VELOCIDAD_KMH = 40.0                        # supuesto de demo
TARIFA_COSTE_H = 35.0                       # €/h coste interno (supuesto de demo)
TARIFA_VENTA_H = 60.0                       # €/h facturación (supuesto de demo)
TIPOS_OT = ["Preventivo", "Correctivo", "Avería"]
PRIORIDADES = ["Baja", "Media", "Alta", "Urgente"]
RANGO = {"Urgente": 0, "Alta": 1, "Media": 2, "Baja": 3}
PRIO_CRITICIDAD = {"A": "Alta", "B": "Media", "C": "Baja"}
ESTADOS = ["Bolsa IA", "Programada", "Completada"]
VENTANA_KPI_DIAS = 90
HORIZONTE_PM_DIAS = 7
PALETA = ["#2563EB", "#10B981", "#F59E0B", "#8B5CF6", "#EF4444", "#14B8A6", "#EC4899", "#64748B"]
MAP_STYLE = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"
SCHEDULER_KEY = "CC-Attribution-NonCommercial-NoDerivatives"  # licencia gratuita no comercial


def _version_tuple(v: str):
    partes = []
    for p in v.split(".")[:3]:
        d = "".join(ch for ch in p if ch.isdigit())
        partes.append(int(d) if d else 0)
    return tuple(partes)


STRETCH = (
    {"width": "stretch"}
    if _version_tuple(st.__version__) >= (1, 50, 0)
    else {"use_container_width": True}
)


def ahora() -> datetime:
    """Hora actual de Madrid (naive). En Streamlit Cloud el servidor va en UTC."""
    return datetime.now(TZ).replace(tzinfo=None) if TZ else datetime.now()


def _minutos(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def flash(msg: str):
    st.session_state["flash"] = msg


# ==========================================
# 1. PERSISTENCIA
# ==========================================
@contextmanager
def db():
    """Una conexión por operación; commit si todo va bien, rollback si falla."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def hash_password(password: str, salt=None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt_hex, hash_hex = stored.split("$")
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
    return hmac.compare_digest(dk.hex(), hash_hex)


def _respaldar_bd_antigua():
    """Si existe una BD de una versión anterior (sin clientes/activos), se aparta con copia."""
    if not os.path.exists(DB_PATH):
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        tablas = {f[0] for f in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    if tablas and not {"clientes", "sedes", "activos", "planes_pm"} <= tablas:
        os.replace(DB_PATH, f"gmao_terrassa_antigua_{datetime.now():%Y%m%d_%H%M%S}.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
    username TEXT PRIMARY KEY,
    pw_hash  TEXT NOT NULL,
    rol      TEXT NOT NULL CHECK (rol IN ('Operario','Manager')),
    activo   INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS clientes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre   TEXT NOT NULL UNIQUE,
    cif      TEXT,
    contacto TEXT,
    telefono TEXT,
    email    TEXT,
    activo   INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS sedes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    id_cliente INTEGER NOT NULL REFERENCES clientes(id),
    nombre     TEXT NOT NULL,
    direccion  TEXT NOT NULL,
    municipio  TEXT,
    cp         TEXT,
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    UNIQUE (id_cliente, nombre)
);
CREATE TABLE IF NOT EXISTS activos (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    id_sede           INTEGER NOT NULL REFERENCES sedes(id),
    codigo            TEXT NOT NULL UNIQUE,
    nombre            TEXT NOT NULL,
    tipo              TEXT,
    criticidad        TEXT NOT NULL DEFAULT 'B' CHECK (criticidad IN ('A','B','C')),
    fabricante        TEXT,
    num_serie         TEXT,
    fecha_instalacion TEXT
);
CREATE TABLE IF NOT EXISTS planes_pm (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    id_activo        INTEGER NOT NULL REFERENCES activos(id),
    tarea            TEXT NOT NULL,
    frecuencia_dias  INTEGER NOT NULL CHECK (frecuencia_dias > 0),
    horas            REAL NOT NULL CHECK (horas > 0),
    ultima_ejecucion TEXT NOT NULL,           -- ISO-8601 YYYY-MM-DD
    activo           INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS inventario (
    codigo      TEXT PRIMARY KEY,
    descripcion TEXT NOT NULL,
    stock       INTEGER NOT NULL CHECK (stock >= 0),
    coste       REAL NOT NULL,
    venta       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ordenes (
    id_ot             INTEGER PRIMARY KEY AUTOINCREMENT,
    titulo            TEXT NOT NULL,
    id_activo         INTEGER NOT NULL REFERENCES activos(id),
    id_plan           INTEGER REFERENCES planes_pm(id),
    tipo              TEXT NOT NULL DEFAULT 'Preventivo'
                      CHECK (tipo IN ('Preventivo','Correctivo','Avería')),
    prioridad         TEXT NOT NULL DEFAULT 'Media'
                      CHECK (prioridad IN ('Baja','Media','Alta','Urgente')),
    fecha_aviso       TEXT NOT NULL,           -- ISO-8601 (fecha u hora de aviso)
    operario_asignado TEXT REFERENCES usuarios(username),
    fecha_prog        TEXT,
    hora_inicio       TEXT,                    -- HH:MM
    hora_fin          TEXT,                    -- HH:MM
    horas             REAL NOT NULL,           -- estimadas
    horas_reales      REAL,
    material          TEXT REFERENCES inventario(codigo),
    cant_mat          INTEGER DEFAULT 0,
    obs               TEXT,
    estado            TEXT NOT NULL DEFAULT 'Bolsa IA'
                      CHECK (estado IN ('Bolsa IA','Programada','Completada')),
    fecha_cierre      TEXT,                    -- ISO-8601 con hora
    coste             REAL,
    venta             REAL
);
CREATE INDEX IF NOT EXISTS idx_ot_estado   ON ordenes(estado);
CREATE INDEX IF NOT EXISTS idx_ot_operario ON ordenes(operario_asignado, estado);
CREATE INDEX IF NOT EXISTS idx_ot_activo   ON ordenes(id_activo);
CREATE INDEX IF NOT EXISTS idx_ot_plan     ON ordenes(id_plan, estado);

DROP VIEW IF EXISTS v_ot;
CREATE VIEW v_ot AS
SELECT o.*, a.codigo AS activo_codigo, a.nombre AS activo, a.criticidad,
       s.nombre AS sede, s.direccion, s.municipio, s.lat, s.lon,
       c.id AS id_cliente, c.nombre AS cliente
FROM ordenes o
JOIN activos  a ON a.id = o.id_activo
JOIN sedes    s ON s.id = a.id_sede
JOIN clientes c ON c.id = s.id_cliente;
"""

# Datos de demostración (empresas y direcciones ficticias en el entorno de Terrassa)
DEMO = [
    ("Tèxtil Vallès S.A.", "A08000001", "Marta Puig", "937 000 001", "mantenimiento@textilvalles.example", [
        ("Planta Santa Margarida", "Pol. Ind. Santa Margarida, Nave 4", "Terrassa", "08223", 41.575, 2.002, [
            ("BOM-01", "Bomba Centrífuga B-01", "Bombeo", "A", [("Revisión de sellos y alineación", 30, 2.0), ("Cambio de aceite", 90, 1.5)]),
            ("CLI-02", "Climatizador Nave 4", "Climatización", "C", [("Cambio de filtros", 60, 1.0)]),
        ]),
        ("Almacén Can Vinyals", "Pol. Ind. Can Vinyals, C/ Alba 12", "Terrassa", "08227", 41.550, 1.985, [
            ("COM-02", "Compresor C-02", "Aire comprimido", "A", [("Revisión general del compresor", 45, 3.0)]),
        ]),
    ]),
    ("Aigües del Vallès S.A.", "A08000002", "Jordi Serra", "937 000 002", "operaciones@aiguesvalles.example", [
        ("Estación de Bombeo Oeste", "Camí de Can Boada, s/n", "Terrassa", "08225", 41.560, 1.970, [
            ("VAL-01", "Válvula de Alta Presión", "Válvulas", "A", [("Inspección y engrase de válvula", 60, 3.5)]),
            ("BOM-02", "Bomba Sumergible B-02", "Bombeo", "A", [("Revisión de bomba sumergible", 30, 2.0)]),
        ]),
        ("Depósito Zona Nord", "Ctra. de Matadepera, km 2", "Terrassa", "08221", 41.580, 2.020, [
            ("CLI-01", "Climatizador Central", "Climatización", "B", [("Revisión del climatizador", 90, 2.5)]),
        ]),
    ]),
    ("Logística Rubí-Terrassa S.L.", "B08000003", "Laura Vidal", "937 000 003", "taller@logisticart.example", [
        ("Nave Carretera de Rubí", "Ctra. de Rubí, 120", "Terrassa", "08228", 41.540, 2.030, [
            ("CIN-01", "Cinta Transportadora", "Transporte", "B", [("Engrase y tensado de cinta", 30, 4.0)]),
        ]),
    ]),
]
# (código de activo, días desde el aviso, horas de reparación) -> averías históricas ya cerradas
HISTORICO_AVERIAS = [("BOM-01", 70, 3.0), ("BOM-01", 31, 2.5), ("COM-02", 55, 4.0), ("VAL-01", 20, 5.0),
                     ("CIN-01", 12, 2.0), ("BOM-02", 40, 3.5), ("COM-02", 8, 2.0)]


def _sembrar(conn):
    hoy = ahora().date()
    if conn.execute("SELECT COUNT(*) FROM clientes").fetchone()[0] == 0:
        desfases = [-5, 2, 6, 20, 40]  # días hasta el vencimiento de cada plan (variedad en la demo)
        k = 0
        for nombre, cif, contacto, tel, email, sedes in DEMO:
            cur = conn.execute(
                "INSERT INTO clientes (nombre, cif, contacto, telefono, email) VALUES (?,?,?,?,?)",
                (nombre, cif, contacto, tel, email),
            )
            id_cli = cur.lastrowid
            for s_nom, s_dir, s_mun, s_cp, lat, lon, activos in sedes:
                id_sede = conn.execute(
                    "INSERT INTO sedes (id_cliente, nombre, direccion, municipio, cp, lat, lon) VALUES (?,?,?,?,?,?,?)",
                    (id_cli, s_nom, s_dir, s_mun, s_cp, lat, lon),
                ).lastrowid
                for cod, a_nom, a_tipo, crit, planes in activos:
                    id_act = conn.execute(
                        "INSERT INTO activos (id_sede, codigo, nombre, tipo, criticidad) VALUES (?,?,?,?,?)",
                        (id_sede, cod, a_nom, a_tipo, crit),
                    ).lastrowid
                    for tarea, frec, horas in planes:
                        vence = hoy + timedelta(days=desfases[k % len(desfases)])
                        k += 1
                        ultima = min(vence - timedelta(days=frec), hoy - timedelta(days=1))
                        id_plan = conn.execute(
                            "INSERT INTO planes_pm (id_activo, tarea, frecuencia_dias, horas, ultima_ejecucion) VALUES (?,?,?,?,?)",
                            (id_act, tarea, frec, horas, ultima.isoformat()),
                        ).lastrowid
                        # Histórico: la última ejecución del plan quedó registrada como OT completada
                        conn.execute(
                            """INSERT INTO ordenes (titulo, id_activo, id_plan, tipo, prioridad, fecha_aviso,
                                   operario_asignado, fecha_prog, hora_inicio, hora_fin, horas, horas_reales,
                                   estado, fecha_cierre, coste, venta)
                               VALUES (?,?,?,'Preventivo',?,?,'tecnico1',?, '09:00', ?, ?, ?, 'Completada', ?, ?, ?)""",
                            (f"PM: {tarea} · {a_nom}", id_act, id_plan, PRIO_CRITICIDAD[crit], ultima.isoformat(),
                             ultima.isoformat(), f"{9 + int(horas):02d}:00", horas, horas,
                             f"{ultima.isoformat()} {9 + int(horas):02d}:00",
                             round(horas * TARIFA_COSTE_H, 2), round(horas * TARIFA_VENTA_H, 2)),
                        )

        ids = {r[0]: r[1] for r in conn.execute("SELECT codigo, id FROM activos")}
        nombres = {r[0]: r[1] for r in conn.execute("SELECT codigo, nombre FROM activos")}
        tecs = ["tecnico1", "tecnico2", "tecnico3"]
        for i, (cod, dias, horas) in enumerate(HISTORICO_AVERIAS):
            aviso = datetime.combine(hoy - timedelta(days=dias), dtime(8, 30))
            ini = aviso + timedelta(hours=1.5)
            fin = ini + timedelta(hours=horas)
            conn.execute(
                """INSERT INTO ordenes (titulo, id_activo, tipo, prioridad, fecha_aviso, operario_asignado,
                       fecha_prog, hora_inicio, hora_fin, horas, horas_reales, estado, fecha_cierre, coste, venta)
                   VALUES (?,?, 'Avería','Alta',?,?,?,?,?,?,?, 'Completada', ?,?,?)""",
                (f"Avería: {nombres[cod]}", ids[cod], aviso.strftime("%Y-%m-%d %H:%M"), tecs[i % 3],
                 ini.strftime("%Y-%m-%d"), ini.strftime("%H:%M"), fin.strftime("%H:%M"), horas, horas,
                 fin.strftime("%Y-%m-%d %H:%M"), round(horas * TARIFA_COSTE_H, 2), round(horas * TARIFA_VENTA_H, 2)),
            )
        # Un aviso urgente abierto
        conn.execute(
            """INSERT INTO ordenes (titulo, id_activo, tipo, prioridad, fecha_aviso, horas, estado)
               VALUES ('Fuga en válvula de alta presión', ?, 'Avería','Urgente',?, 3.0, 'Bolsa IA')""",
            (ids["VAL-01"], ahora().strftime("%Y-%m-%d %H:%M")),
        )
        generar_preventivos(conn)


def inicializar_bd():
    _respaldar_bd_antigua()
    with db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)

        existentes = {f[0] for f in conn.execute("SELECT username FROM usuarios")}
        for u, p, r in [("manager1", "admin", "Manager"), ("tecnico1", "123", "Operario"),
                        ("tecnico2", "123", "Operario"), ("tecnico3", "123", "Operario")]:
            if u not in existentes:  # el hash (lento) solo se calcula si falta el usuario
                conn.execute("INSERT INTO usuarios (username, pw_hash, rol) VALUES (?,?,?)",
                             (u, hash_password(p), r))

        if conn.execute("SELECT COUNT(*) FROM inventario").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO inventario (codigo, descripcion, stock, coste, venta) VALUES (?,?,?,?,?)",
                [("MAT-01", "Junta Tórica Viton", 50, 5.0, 15.0),
                 ("MAT-02", "Filtro de aire industrial", 20, 12.0, 30.0),
                 ("MAT-03", "Rodamiento 6205", 30, 8.0, 22.0)],
            )
        _sembrar(conn)


# ==========================================
# 2. SERVICIOS
# ==========================================
def autenticar(username: str, password: str):
    with db() as conn:
        fila = conn.execute("SELECT pw_hash, rol FROM usuarios WHERE username=?", (username,)).fetchone()
    if fila and verify_password(password, fila[0]):
        return fila[1]
    return None


def tecnicos(solo_activos=True):
    q = "SELECT username FROM usuarios WHERE rol='Operario'" + (" AND activo=1" if solo_activos else "")
    with db() as conn:
        return [r[0] for r in conn.execute(q + " ORDER BY username")]


def mapa_colores():
    return {u: PALETA[i % len(PALETA)] for i, u in enumerate(tecnicos(solo_activos=False))}


def geocodificar(direccion, municipio="", cp=""):
    """Geocodificación con Nominatim (OpenStreetMap). Devuelve (lat, lon) o None."""
    q = ", ".join(x.strip() for x in [direccion, cp, municipio, "España"] if x and x.strip())
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": q, "format": "json", "limit": 1, "countrycodes": "es"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": "SIGMA-IA-TFG/1.0 (proyecto academico)"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            datos = json.load(r)
        if datos:
            return float(datos[0]["lat"]), float(datos[0]["lon"])
    except Exception:
        return None
    return None


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlam = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def siguiente_laborable(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def generar_preventivos(conn, horizonte_dias=HORIZONTE_PM_DIAS) -> int:
    """
    Mantenimiento preventivo basado en calendario: para cada plan activo cuyo próximo
    vencimiento (última ejecución + frecuencia) cae dentro del horizonte y que no tiene ya
    una OT abierta, crea una OT preventiva en la Bolsa IA. Prioridad según criticidad del activo.
    """
    limite = ahora().date() + timedelta(days=horizonte_dias)
    filas = conn.execute(
        """SELECT p.id, p.id_activo, p.tarea, p.frecuencia_dias, p.horas, p.ultima_ejecucion,
                  a.nombre, a.criticidad
           FROM planes_pm p JOIN activos a ON a.id = p.id_activo
           WHERE p.activo = 1 AND NOT EXISTS (
               SELECT 1 FROM ordenes o WHERE o.id_plan = p.id AND o.estado IN ('Bolsa IA','Programada'))"""
    ).fetchall()
    creadas = 0
    for pid, id_act, tarea, frec, horas, ultima, a_nom, crit in filas:
        vence = date.fromisoformat(ultima) + timedelta(days=frec)
        if vence <= limite:
            conn.execute(
                """INSERT INTO ordenes (titulo, id_activo, id_plan, tipo, prioridad, fecha_aviso, horas, estado)
                   VALUES (?,?,?, 'Preventivo', ?, ?, ?, 'Bolsa IA')""",
                (f"PM: {tarea} · {a_nom}", id_act, pid, PRIO_CRITICIDAD[crit], vence.isoformat(), horas),
            )
            creadas += 1
    return creadas


def bot_optimizar_rutas() -> int:
    """
    Heurística voraz multi-técnico:
      - Se atiende por prioridad (Urgente > Alta > Media > Baja).
      - Cada día laborable, cada técnico activo sale de la central y coge la tarea más cercana
        (haversine) de entre las de mayor prioridad pendientes.
      - Desplazamiento = distancia / velocidad media. Jornada máxima: JORNADA_H horas.
      - Respeta lo ya programado ese día para el técnico.
    Devuelve el número de órdenes programadas.
    """
    with db() as conn:
        activos = [r[0] for r in conn.execute(
            "SELECT username FROM usuarios WHERE rol='Operario' AND activo=1 ORDER BY username")]
        filas = conn.execute(
            "SELECT id_ot, lat, lon, horas, prioridad FROM v_ot WHERE estado='Bolsa IA'").fetchall()
        if not filas or not activos:
            return 0

        ocupado = {(u, f): fin for u, f, fin in conn.execute(
            """SELECT operario_asignado, fecha_prog, MAX(hora_fin) FROM ordenes
               WHERE estado IN ('Programada','Completada') AND fecha_prog IS NOT NULL
               GROUP BY operario_asignado, fecha_prog""")}

        pendientes = [{"id": f[0], "lat": f[1], "lon": f[2], "horas": f[3], "rank": RANGO[f[4]]} for f in filas]
        actualizaciones = []
        dia = siguiente_laborable(ahora().date())

        for _ in range(365):
            if not pendientes:
                break
            for tec in activos:
                if not pendientes:
                    break
                inicio = datetime.strptime(f"{dia} {INICIO_JORNADA}", "%Y-%m-%d %H:%M")
                limite = inicio + timedelta(hours=JORNADA_H)
                reloj, pos, tareas_dia = inicio, (BASE_LAT, BASE_LON), 0
                previa = ocupado.get((tec, dia.strftime("%Y-%m-%d")))
                if previa:
                    reloj = max(reloj, datetime.strptime(f"{dia} {previa}", "%Y-%m-%d %H:%M"))
                    tareas_dia = 1

                while pendientes:
                    nivel = min(p["rank"] for p in pendientes)
                    cand = [p for p in pendientes if p["rank"] == nivel]
                    sig = min(cand, key=lambda p: haversine_km(*pos, p["lat"], p["lon"]))
                    viaje = timedelta(hours=haversine_km(*pos, sig["lat"], sig["lon"]) / VELOCIDAD_KMH)
                    ini_t = reloj + viaje
                    fin_t = ini_t + timedelta(hours=sig["horas"])
                    if tareas_dia > 0 and fin_t > limite:
                        break
                    actualizaciones.append((tec, dia.strftime("%Y-%m-%d"), ini_t.strftime("%H:%M"),
                                            fin_t.strftime("%H:%M"), sig["id"]))
                    pendientes.remove(sig)
                    reloj, pos, tareas_dia = fin_t, (sig["lat"], sig["lon"]), tareas_dia + 1
            dia = siguiente_laborable(dia)

        conn.executemany(
            """UPDATE ordenes SET operario_asignado=?, fecha_prog=?, hora_inicio=?, hora_fin=?, estado='Programada'
               WHERE id_ot=?""", actualizaciones)
        return len(actualizaciones)


def hay_solape(conn, tecnico, fecha_iso, ini, fin) -> bool:
    filas = conn.execute(
        """SELECT hora_inicio, hora_fin FROM ordenes
           WHERE operario_asignado=? AND fecha_prog=? AND estado IN ('Programada','Completada')""",
        (tecnico, fecha_iso)).fetchall()
    return any(ini < f and fin > i for i, f in filas)


def crear_orden(titulo, id_activo, tipo, prioridad, horas, tecnico, fecha, hora_ini):
    """Alta de OT/aviso. Con técnico -> Programada (comprobando solapes); sin técnico -> Bolsa IA."""
    with db() as conn:
        act = conn.execute("SELECT nombre FROM activos WHERE id=?", (id_activo,)).fetchone()
        if act is None:
            return False, "Activo no válido."
        titulo = titulo or f"{tipo} · {act[0]}"
        aviso = ahora().strftime("%Y-%m-%d %H:%M")
        if tecnico:
            ini_dt = datetime.combine(fecha, hora_ini)
            fin_dt = ini_dt + timedelta(hours=horas)
            if fin_dt.date() != fecha:
                return False, "La orden terminaría después de medianoche."
            ini, fin = ini_dt.strftime("%H:%M"), fin_dt.strftime("%H:%M")
            if hay_solape(conn, tecnico, fecha.strftime("%Y-%m-%d"), ini, fin):
                return False, f"{tecnico} ya tiene una tarea que se solapa en ese horario."
            conn.execute(
                """INSERT INTO ordenes (titulo, id_activo, tipo, prioridad, fecha_aviso, operario_asignado,
                                        fecha_prog, hora_inicio, hora_fin, horas, estado)
                   VALUES (?,?,?,?,?,?,?,?,?,?, 'Programada')""",
                (titulo, id_activo, tipo, prioridad, aviso, tecnico, fecha.strftime("%Y-%m-%d"), ini, fin, horas))
            return True, f"Orden programada para {tecnico} ({ini}-{fin})."
        conn.execute(
            """INSERT INTO ordenes (titulo, id_activo, tipo, prioridad, fecha_aviso, horas, estado)
               VALUES (?,?,?,?,?,?, 'Bolsa IA')""", (titulo, id_activo, tipo, prioridad, aviso, horas))
        return True, "Orden añadida a la bolsa para el optimizador."


def completar_orden(id_ot, horas_reales, codigo_mat, cantidad, obs):
    """Cierra la OT, descuenta stock y actualiza el plan preventivo, todo en una transacción."""
    with db() as conn:
        fila = conn.execute("SELECT estado, id_plan FROM ordenes WHERE id_ot=?", (id_ot,)).fetchone()
        if fila is None or fila[0] != "Programada":
            return False, "La orden ya no está pendiente."
        coste, venta = horas_reales * TARIFA_COSTE_H, horas_reales * TARIFA_VENTA_H
        if codigo_mat and cantidad > 0:
            precio = conn.execute("SELECT coste, venta FROM inventario WHERE codigo=?", (codigo_mat,)).fetchone()
            cur = conn.execute("UPDATE inventario SET stock = stock - ? WHERE codigo=? AND stock >= ?",
                               (cantidad, codigo_mat, cantidad))
            if cur.rowcount == 0:
                return False, "Stock insuficiente para ese material."
            coste += cantidad * precio[0]
            venta += cantidad * precio[1]
        else:
            codigo_mat, cantidad = None, 0
        cierre = ahora()
        conn.execute(
            """UPDATE ordenes SET estado='Completada', horas_reales=?, material=?, cant_mat=?, obs=?,
                   fecha_cierre=?, coste=?, venta=? WHERE id_ot=?""",
            (horas_reales, codigo_mat, cantidad, obs, cierre.strftime("%Y-%m-%d %H:%M"),
             round(coste, 2), round(venta, 2), id_ot))
        if fila[1]:
            conn.execute("UPDATE planes_pm SET ultima_ejecucion=? WHERE id=?", (cierre.date().isoformat(), fila[1]))
    return True, ""


def estado_tecnicos():
    """Situación de cada técnico activo en el momento de la consulta."""
    ahora_ = ahora()
    hoy, hhmm = ahora_.date().isoformat(), ahora_.strftime("%H:%M")
    colores = mapa_colores()
    with db() as conn:
        filas = conn.execute(
            """SELECT operario_asignado, titulo, cliente, hora_inicio, hora_fin FROM v_ot
               WHERE fecha_prog=? AND estado IN ('Programada','Completada') AND operario_asignado IS NOT NULL
               ORDER BY hora_inicio""", (hoy,)).fetchall()
    res = []
    for t in tecnicos():
        propias = [f for f in filas if f[0] == t]
        horas = sum(_minutos(f[4]) - _minutos(f[3]) for f in propias) / 60
        actual = next((f for f in propias if f[3] <= hhmm < f[4]), None)
        proxima = next((f for f in propias if f[3] > hhmm), None)
        if actual:
            situacion = f"🔴 Ocupado: {actual[1]} · {actual[2]} (hasta {actual[4]})"
        elif proxima:
            situacion = f"🟢 Libre · próxima tarea a las {proxima[3]}"
        else:
            situacion = "🟢 Libre · sin más tareas hoy"
        res.append({"usuario": t, "color": colores.get(t, "#64748B"), "situacion": situacion,
                    "horas": horas, "carga": horas / JORNADA_H})
    return res


def _a_datetime(serie):
    """Fechas ISO con o sin hora ('YYYY-MM-DD' / 'YYYY-MM-DD HH:MM') -> datetime (NaT si vacío)."""
    t = serie.fillna("").astype(str).str.slice(0, 16)
    t = t.where(t.str.len() > 10, t + " 00:00")
    return pd.to_datetime(t, format="%Y-%m-%d %H:%M", errors="coerce")


def calcular_kpis(ventana_dias=VENTANA_KPI_DIAS):
    """KPIs de mantenimiento sobre las OT cerradas en la ventana (norma UNE-EN 15341 como referencia)."""
    hoy = ahora()
    corte = hoy - timedelta(days=ventana_dias)
    with db() as conn:
        df = pd.read_sql_query("SELECT * FROM v_ot", conn)
        vencidas = conn.execute(
            """SELECT COUNT(*) FROM ordenes WHERE tipo='Preventivo' AND estado IN ('Bolsa IA','Programada')
               AND substr(fecha_aviso,1,10) < ?""", (hoy.date().isoformat(),)).fetchone()[0]
    out = {"vacio": df.empty}
    if df.empty:
        return out
    df["aviso"] = _a_datetime(df["fecha_aviso"])
    df["cierre"] = _a_datetime(df["fecha_cierre"])
    comp = df[(df["estado"] == "Completada") & (df["cierre"] >= corte)].copy()
    fallas = comp[comp["tipo"] == "Avería"].copy()
    fallas["mttr_h"] = (fallas["cierre"] - fallas["aviso"]).dt.total_seconds() / 3600

    out["abiertas"] = int((df["estado"] != "Completada").sum())
    out["completadas"] = int(len(comp))
    out["pct_preventivo"] = round(float(100 * (comp["tipo"] == "Preventivo").sum() / len(comp)), 1) if len(comp) else 0.0
    out["n_averias"] = int(len(fallas))
    out["mttr_h"] = round(float(fallas["mttr_h"].mean()), 2) if len(fallas) else None
    prev_ok = int((comp["tipo"] == "Preventivo").sum())
    out["cumplimiento_pm"] = round(100 * prev_ok / (prev_ok + vencidas), 1) if (prev_ok + vencidas) else None
    out["pm_vencidos"] = int(vencidas)

    if len(fallas):
        g = fallas.groupby(["activo_codigo", "activo"]).agg(
            averias=("id_ot", "count"), mttr_h=("mttr_h", "mean")).reset_index()
        g["mtbf_h"] = ventana_dias * 24 / g["averias"]
        g["disponibilidad_%"] = (100 * g["mtbf_h"] / (g["mtbf_h"] + g["mttr_h"])).round(1)
        g["mttr_h"], g["mtbf_h"] = g["mttr_h"].round(2), g["mtbf_h"].round(1)
        out["por_activo"] = g.sort_values("averias", ascending=False)
    else:
        out["por_activo"] = pd.DataFrame()
    out["por_cliente"] = comp.groupby("cliente")[["coste", "venta"]].sum().round(2) if len(comp) else pd.DataFrame()
    out["por_tipo"] = comp["tipo"].value_counts() if len(comp) else pd.Series(dtype=int)
    out["ventana"] = ventana_dias
    return out


def _l1(texto) -> str:
    return str(texto if texto is not None else "").encode("latin-1", "replace").decode("latin-1")


def generar_albaran(id_ot: int) -> bytes:
    with db() as conn:
        o = conn.execute(
            """SELECT v.id_ot, v.titulo, v.cliente, v.sede, v.direccion, v.municipio, v.activo_codigo, v.activo,
                      v.operario_asignado, v.fecha_prog, v.horas_reales, v.material, i.descripcion,
                      v.cant_mat, v.obs, v.venta
               FROM v_ot v LEFT JOIN inventario i ON i.codigo = v.material
               WHERE v.id_ot=? AND v.estado='Completada'""", (id_ot,)).fetchone()
    if o is None:
        raise ValueError("La orden no existe o no está completada.")
    if FPDF is None:
        raise RuntimeError("El paquete fpdf2 no está instalado en el servidor.")

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, _l1("SIGMA-IA | Albarán de trabajo"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, _l1(f"Emitido: {ahora():%d/%m/%Y %H:%M}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    campos = [
        ("Orden de trabajo", f"OT-{o[0]:05d}"), ("Trabajo", o[1]), ("Cliente", o[2]),
        ("Sede", f"{o[3]} - {o[4]} ({o[5]})"), ("Activo", f"{o[6]} · {o[7]}"),
        ("Técnico", o[8]), ("Fecha", o[9]), ("Horas reales", f"{o[10]:.2f} h"),
        ("Material", f"{o[12]} x{o[13]}" if o[11] else "—"),
    ]
    for etiqueta, valor in campos:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(40, 7, _l1(etiqueta))
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 7, _l1(valor), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 7, "Observaciones", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, _l1(o[14] or "—"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, _l1(f"Importe: {o[15]:.2f} EUR (IVA no incluido)"), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


# ==========================================
# 3. PRESENTACIÓN
# ==========================================
def nav(opciones, key):
    return st.radio("Sección", opciones, horizontal=True, label_visibility="collapsed", key=key)


def color_punto(estado, tipo):
    if estado == "Completada":
        return [16, 185, 129]
    if tipo == "Avería":
        return [220, 38, 38]
    if estado == "Programada":
        return [37, 99, 235]
    return [245, 158, 11]


def vista_mapa(usuario=None, key="mapa"):
    cols = ("id_ot, titulo, cliente, sede, direccion, activo, lat, lon, estado, tipo, prioridad, "
            "operario_asignado, fecha_prog")
    with db() as conn:
        if usuario:
            df = pd.read_sql_query(f"SELECT {cols} FROM v_ot WHERE operario_asignado=? AND estado='Programada'",
                                   conn, params=(usuario,))
        else:
            df = pd.read_sql_query(f"SELECT {cols} FROM v_ot", conn)
    if not usuario:
        sel = st.multiselect("Mostrar estados", ESTADOS, default=["Bolsa IA", "Programada"], key=f"flt_{key}")
        df = df[df["estado"].isin(sel)].copy()

    df["operario_asignado"] = df["operario_asignado"].fillna("sin asignar")
    df["fecha_prog"] = df["fecha_prog"].fillna("—")
    df["color"] = [color_punto(e, t) for e, t in zip(df["estado"], df["tipo"])]
    central = pd.DataFrame([{
        "id_ot": 0, "titulo": "Central SIGMA-IA", "cliente": "", "sede": "", "direccion": "Terrassa",
        "activo": "", "lat": BASE_LAT, "lon": BASE_LON, "estado": "Base", "tipo": "", "prioridad": "",
        "operario_asignado": "", "fecha_prog": "", "color": [17, 24, 39]}])

    if pdk is None:
        st.map(pd.concat([df, central])[["lat", "lon"]])
    else:
        capas = [
            pdk.Layer("ScatterplotLayer", data=df, get_position="[lon, lat]", get_fill_color="color",
                      get_line_color=[255, 255, 255], line_width_min_pixels=2, stroked=True,
                      get_radius=120, radius_min_pixels=8, radius_max_pixels=18, pickable=True),
            pdk.Layer("ScatterplotLayer", data=central, get_position="[lon, lat]", get_fill_color="color",
                      get_radius=160, radius_min_pixels=10, radius_max_pixels=20, pickable=True),
        ]
        deck = pdk.Deck(
            layers=capas,
            initial_view_state=pdk.ViewState(latitude=BASE_LAT, longitude=BASE_LON, zoom=11.2),
            map_style=MAP_STYLE,
            tooltip={"text": "{titulo}\n{cliente} · {sede}\n{direccion}\n{activo}\n{tipo} ({prioridad}) · {estado}\nTécnico: {operario_asignado} · {fecha_prog}"},
        )
        try:
            st.pydeck_chart(deck, height=520)
        except TypeError:
            st.pydeck_chart(deck)
    st.caption("🔴 Avería activa · 🟠 Bolsa IA · 🔵 Programada · 🟢 Completada · ⚫ Central Terrassa")


def opciones_calendario(vista="timeGridWeek"):
    return {
        "initialView": vista,
        "headerToolbar": {"left": "prev,next today", "center": "title",
                          "right": "dayGridMonth,timeGridWeek,timeGridDay,listWeek"},
        "buttonText": {"today": "Hoy", "month": "Mes", "week": "Semana", "day": "Día", "list": "Lista"},
        "firstDay": 1, "slotMinTime": "07:00:00", "slotMaxTime": "19:00:00",
        "nowIndicator": True, "navLinks": True, "allDaySlot": False, "height": 700,
    }


def eventos_calendario(usuario=None, con_recurso=False):
    colores = mapa_colores()
    base = "SELECT * FROM v_ot WHERE fecha_prog IS NOT NULL AND estado IN ('Programada','Completada')"
    with db() as conn:
        if usuario:
            df = pd.read_sql_query(base + " AND operario_asignado=?", conn, params=(usuario,))
        else:
            df = pd.read_sql_query(base, conn)
    eventos = []
    for _, r in df.iterrows():
        marca = "✔ " if r["estado"] == "Completada" else ("⚠ " if r["tipo"] == "Avería" else "")
        quien = "" if usuario else f"[{r['operario_asignado']}] "
        color = colores.get(r["operario_asignado"], "#64748B")
        ev = {"title": f"{marca}{quien}{r['titulo']} · {r['cliente']}",
              "start": f"{r['fecha_prog']}T{r['hora_inicio']}:00",
              "end": f"{r['fecha_prog']}T{r['hora_fin']}:00",
              "backgroundColor": color, "borderColor": color}
        if con_recurso:
            ev["resourceId"] = r["operario_asignado"]
        eventos.append(ev)
    return eventos


def pintar_calendario(eventos, opciones, key):
    if calendar is None:
        st.warning("streamlit-calendar no está instalado en el servidor (revisa requirements.txt). Se muestra una tabla.")
        st.dataframe(pd.DataFrame(eventos), hide_index=True, **STRETCH)
        return
    calendar(events=eventos, options=opciones, key=f"{key}_{len(eventos)}")


def leyenda_tecnicos():
    activos = set(tecnicos())
    partes = [f"<span style='color:{c};font-size:1.2em'>●</span> {u}{'' if u in activos else ' (inactivo)'}"
              for u, c in mapa_colores().items()]
    st.markdown(" &nbsp;&nbsp; ".join(partes), unsafe_allow_html=True)


def vista_agenda(usuario=None):
    st.subheader("Cuadrante de carga de trabajo" if usuario else "Agenda global")
    if not usuario:
        leyenda_tecnicos()
    pintar_calendario(eventos_calendario(usuario), opciones_calendario(), key=f"cal_{usuario or 'global'}")


def vista_planner():
    st.subheader("👷 Planner de técnicos activos")
    estado = estado_tecnicos()
    if not estado:
        st.info("No hay técnicos activos. Actívalos en la sección «Bot IA».")
        return
    st.caption(f"Situación a las {ahora():%H:%M} (hora de Madrid)")
    for fila in [estado[i:i + 4] for i in range(0, len(estado), 4)]:
        for col, t in zip(st.columns(len(fila)), fila):
            with col, st.container(border=True):
                st.markdown(f"<span style='color:{t['color']};font-size:1.4em'>●</span> **{t['usuario']}**",
                            unsafe_allow_html=True)
                st.write(t["situacion"])
                st.progress(min(t["carga"], 1.0))
                st.caption(f"{t['horas']:.1f} h de {JORNADA_H:.0f} h programadas hoy")

    colores = mapa_colores()
    opciones = {
        "initialView": "resourceTimelineDay", "initialDate": ahora().date().isoformat(),
        "schedulerLicenseKey": SCHEDULER_KEY,
        "headerToolbar": {"left": "prev,next today", "center": "title",
                          "right": "resourceTimelineDay,resourceTimelineWeek"},
        "buttonText": {"today": "Hoy"},
        "views": {"resourceTimelineDay": {"buttonText": "Día"}, "resourceTimelineWeek": {"buttonText": "Semana"}},
        "firstDay": 1, "slotMinTime": "07:00:00", "slotMaxTime": "19:00:00", "nowIndicator": True,
        "resourceAreaHeaderContent": "Técnicos", "resourceAreaWidth": "14%", "height": 420,
        "resources": [{"id": t["usuario"], "title": t["usuario"],
                       "eventColor": colores.get(t["usuario"], "#64748B")} for t in estado],
    }
    activos = {t["usuario"] for t in estado}
    eventos = [e for e in eventos_calendario(con_recurso=True) if e["resourceId"] in activos]
    pintar_calendario(eventos, opciones, key="cal_planner")
    st.caption("Si la línea de tiempo por técnico no se dibuja, usa «Agenda Global»: muestra los mismos datos con un color por técnico.")


def vista_kpis():
    st.subheader("📊 Indicadores de mantenimiento")
    k = calcular_kpis()
    if k["vacio"]:
        st.info("Todavía no hay órdenes registradas.")
        return
    c = st.columns(5)
    c[0].metric("OT abiertas", k["abiertas"])
    c[1].metric(f"OT cerradas ({k['ventana']} d)", k["completadas"])
    c[2].metric("% preventivo", f"{k['pct_preventivo']} %")
    c[3].metric("MTTR (h)", k["mttr_h"] if k["mttr_h"] is not None else "—")
    c[4].metric("Cumplimiento PM", f"{k['cumplimiento_pm']} %" if k["cumplimiento_pm"] is not None else "—",
                help=f"Preventivos vencidos sin ejecutar: {k['pm_vencidos']}")
    st.caption(
        "MTTR = media(cierre − aviso) de averías · MTBF ≈ ventana / nº de averías · "
        "Disponibilidad = MTBF / (MTBF + MTTR) · Cumplimiento PM = preventivos cerrados / (cerrados + vencidos pendientes)")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("##### Fiabilidad por activo")
        if k["por_activo"].empty:
            st.info("Sin averías cerradas en la ventana.")
        else:
            st.dataframe(k["por_activo"], hide_index=True, **STRETCH)
    with c2:
        st.markdown("##### Coste y facturación por cliente (€)")
        if not k["por_cliente"].empty:
            st.bar_chart(k["por_cliente"])
    st.markdown("##### OT cerradas por tipo")
    if not k["por_tipo"].empty:
        st.bar_chart(k["por_tipo"])


def vista_despacho():
    n = st.session_state.setdefault("orden_n", 0)
    with db() as conn:
        clientes = conn.execute("SELECT id, nombre FROM clientes WHERE activo=1 ORDER BY nombre").fetchall()
    col_m, col_f = st.columns([2, 1])
    with col_m:
        st.subheader("🗺️ Mapa de averías e instalaciones")
        vista_mapa(key="despacho")
    with col_f:
        st.subheader("➕ Nueva orden / aviso de avería")
        if not clientes:
            st.info("Primero crea un cliente en «Clientes y Sedes».")
            return
        id_cli = st.selectbox("Cliente", [c[0] for c in clientes], format_func=dict(clientes).get, key=f"cli_{n}")
        with db() as conn:
            sedes = conn.execute("SELECT id, nombre FROM sedes WHERE id_cliente=? ORDER BY nombre", (id_cli,)).fetchall()
        if not sedes:
            st.info("Este cliente aún no tiene sedes.")
            return
        id_sede = st.selectbox("Sede", [s[0] for s in sedes], format_func=dict(sedes).get, key=f"sede_{n}_{id_cli}")
        with db() as conn:
            acts = conn.execute("SELECT id, codigo || ' · ' || nombre FROM activos WHERE id_sede=? ORDER BY codigo",
                                (id_sede,)).fetchall()
        if not acts:
            st.info("Esta sede aún no tiene activos. Créalos en «Activos y Planes PM».")
            return
        id_act = st.selectbox("Activo", [a[0] for a in acts], format_func=dict(acts).get, key=f"act_{n}_{id_sede}")
        titulo = st.text_input("Título (opcional)", key=f"tit_{n}")
        tipo = st.selectbox("Tipo", TIPOS_OT, index=2, key=f"tipo_{n}")
        prio = st.selectbox("Prioridad", PRIORIDADES, index=2, key=f"prio_{n}")
        horas = st.number_input("Horas estimadas", 0.5, 12.0, 2.0, 0.5, key=f"hor_{n}")
        auto = "— Bolsa IA (automático) —"
        tec = st.selectbox("Asignar técnico", [auto] + tecnicos(), key=f"tec_{n}")
        fecha = st.date_input("Fecha", ahora().date(), key=f"fec_{n}")
        hora_ini = st.time_input("Hora de inicio", value=dtime(8, 0), step=900, key=f"hin_{n}")
        st.caption("Fecha y hora solo se usan si asignas un técnico concreto.")
        if st.button("Crear orden", type="primary", **STRETCH, key=f"crear_{n}"):
            ok, msg = crear_orden(titulo.strip(), id_act, tipo, prio, float(horas),
                                  None if tec == auto else tec, fecha, hora_ini)
            if ok:
                st.session_state["orden_n"] = n + 1
                flash(msg)
                st.rerun()
            else:
                st.error(msg)


def vista_clientes():
    with db() as conn:
        df = pd.read_sql_query(
            """SELECT c.id, c.nombre, c.cif, c.contacto, c.telefono, c.email, COUNT(s.id) AS sedes
               FROM clientes c LEFT JOIN sedes s ON s.id_cliente = c.id GROUP BY c.id ORDER BY c.nombre""", conn)
    c1, c2 = st.columns([2, 1])
    with c1:
        st.subheader("👥 Clientes")
        st.dataframe(df, hide_index=True, **STRETCH)
    with c2:
        with st.form("nuevo_cliente", clear_on_submit=True):
            st.markdown("##### ➕ Nuevo cliente")
            nombre = st.text_input("Razón social *")
            cif = st.text_input("CIF/NIF")
            contacto = st.text_input("Persona de contacto")
            tel = st.text_input("Teléfono")
            email = st.text_input("Email")
            if st.form_submit_button("Guardar cliente", type="primary", **STRETCH):
                if not nombre.strip():
                    st.error("La razón social es obligatoria.")
                else:
                    try:
                        with db() as conn:
                            conn.execute(
                                "INSERT INTO clientes (nombre, cif, contacto, telefono, email) VALUES (?,?,?,?,?)",
                                (nombre.strip(), cif.strip(), contacto.strip(), tel.strip(), email.strip()))
                        flash(f"Cliente «{nombre.strip()}» creado.")
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("Ya existe un cliente con ese nombre.")

    st.divider()
    st.subheader("📍 Sedes y direcciones")
    if df.empty:
        st.info("Crea un cliente para poder añadir sus sedes.")
        return
    nombres = dict(zip(df["id"].tolist(), df["nombre"].tolist()))
    id_cli = st.selectbox("Cliente", list(nombres), format_func=nombres.get, key="sede_cli")
    with db() as conn:
        sedes = pd.read_sql_query(
            "SELECT id, nombre, direccion, municipio, cp, lat, lon FROM sedes WHERE id_cliente=? ORDER BY nombre",
            conn, params=(int(id_cli),))
    s1, s2 = st.columns([2, 1])
    with s1:
        if sedes.empty:
            st.info("Este cliente todavía no tiene sedes.")
        else:
            st.dataframe(sedes, hide_index=True, **STRETCH)
    with s2:
        with st.form("nueva_sede", clear_on_submit=True):
            st.markdown("##### ➕ Nueva sede / dirección")
            s_nom = st.text_input("Nombre de la sede *")
            s_dir = st.text_input("Dirección *", placeholder="Calle y número")
            s_mun = st.text_input("Municipio", value="Terrassa")
            s_cp = st.text_input("Código postal")
            manual = st.checkbox("Introducir coordenadas manualmente")
            lat = st.number_input("Latitud", value=BASE_LAT, format="%.6f")
            lon = st.number_input("Longitud", value=BASE_LON, format="%.6f")
            st.caption("Si no marcas «manualmente», las coordenadas se buscan en OpenStreetMap a partir de la dirección.")
            if st.form_submit_button("Guardar sede", type="primary", **STRETCH):
                if not s_nom.strip() or not s_dir.strip():
                    st.error("Nombre y dirección son obligatorios.")
                else:
                    coords = (lat, lon) if manual else None
                    if not manual:
                        with st.spinner("Buscando coordenadas…"):
                            coords = geocodificar(s_dir, s_mun, s_cp)
                    if coords is None:
                        st.error("No se encontraron coordenadas para esa dirección. Revísala o marca «coordenadas manualmente».")
                    else:
                        try:
                            with db() as conn:
                                conn.execute(
                                    """INSERT INTO sedes (id_cliente, nombre, direccion, municipio, cp, lat, lon)
                                       VALUES (?,?,?,?,?,?,?)""",
                                    (int(id_cli), s_nom.strip(), s_dir.strip(), s_mun.strip(), s_cp.strip(),
                                     coords[0], coords[1]))
                            flash(f"Sede «{s_nom.strip()}» creada ({coords[0]:.5f}, {coords[1]:.5f}).")
                            st.rerun()
                        except sqlite3.IntegrityError:
                            st.error("Ese cliente ya tiene una sede con ese nombre.")


def vista_activos():
    head, boton = st.columns([3, 1])
    head.subheader("🏭 Activos y planes de mantenimiento preventivo")
    if boton.button("⚙️ Generar preventivos vencidos", **STRETCH,
                    help=f"Crea OT para los planes que vencen en los próximos {HORIZONTE_PM_DIAS} días"):
        with db() as conn:
            n = generar_preventivos(conn)
        flash(f"{n} orden(es) preventiva(s) generada(s) en la bolsa." if n else "No hay preventivos pendientes de generar.")
        st.rerun()

    with db() as conn:
        sedes = conn.execute(
            "SELECT s.id, c.nombre || ' · ' || s.nombre FROM sedes s JOIN clientes c ON c.id = s.id_cliente ORDER BY 2"
        ).fetchall()
    if not sedes:
        st.info("Crea primero un cliente con al menos una sede.")
        return
    id_sede = st.selectbox("Sede", [s[0] for s in sedes], format_func=dict(sedes).get, key="act_sede")
    with db() as conn:
        activos = pd.read_sql_query(
            """SELECT id, codigo, nombre, tipo, criticidad, fabricante, num_serie, fecha_instalacion
               FROM activos WHERE id_sede=? ORDER BY codigo""", conn, params=(int(id_sede),))
        planes = pd.read_sql_query(
            """SELECT p.id, a.codigo, p.tarea, p.frecuencia_dias, p.horas, p.ultima_ejecucion,
                      date(p.ultima_ejecucion, '+' || p.frecuencia_dias || ' days') AS proximo_vencimiento
               FROM planes_pm p JOIN activos a ON a.id = p.id_activo WHERE a.id_sede=? ORDER BY proximo_vencimiento""",
            conn, params=(int(id_sede),))

    a1, a2 = st.columns([2, 1])
    with a1:
        st.markdown("##### Activos de la sede")
        if activos.empty:
            st.info("Esta sede todavía no tiene activos.")
        else:
            st.dataframe(activos, hide_index=True, **STRETCH)
    with a2:
        with st.form("nuevo_activo", clear_on_submit=True):
            st.markdown("##### ➕ Nuevo activo")
            cod = st.text_input("Código *", placeholder="BOM-03")
            nom = st.text_input("Nombre *")
            tipo = st.text_input("Tipo")
            crit = st.selectbox("Criticidad", ["A", "B", "C"], index=1, help="A = crítico para la producción")
            fab = st.text_input("Fabricante")
            serie = st.text_input("Nº de serie")
            f_inst = st.date_input("Fecha de instalación", ahora().date())
            if st.form_submit_button("Guardar activo", type="primary", **STRETCH):
                if not cod.strip() or not nom.strip():
                    st.error("Código y nombre son obligatorios.")
                else:
                    try:
                        with db() as conn:
                            conn.execute(
                                """INSERT INTO activos (id_sede, codigo, nombre, tipo, criticidad, fabricante,
                                       num_serie, fecha_instalacion) VALUES (?,?,?,?,?,?,?,?)""",
                                (int(id_sede), cod.strip().upper(), nom.strip(), tipo.strip(), crit, fab.strip(),
                                 serie.strip(), f_inst.isoformat()))
                        flash(f"Activo {cod.strip().upper()} creado.")
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("Ya existe un activo con ese código.")

    st.divider()
    p1, p2 = st.columns([2, 1])
    with p1:
        st.markdown("##### Planes preventivos de la sede")
        if planes.empty:
            st.info("Sin planes preventivos.")
        else:
            st.dataframe(planes, hide_index=True, **STRETCH)
    with p2:
        if activos.empty:
            return
        with st.form("nuevo_plan", clear_on_submit=True):
            st.markdown("##### ➕ Nuevo plan preventivo")
            id_act = st.selectbox("Activo", activos["id"].tolist(),
                                  format_func=dict(zip(activos["id"].tolist(), activos["codigo"].tolist())).get)
            tarea = st.text_input("Tarea *")
            frec = st.number_input("Frecuencia (días)", 1, 730, 30)
            horas = st.number_input("Horas estimadas", 0.5, 24.0, 2.0, 0.5)
            ult = st.date_input("Última ejecución", ahora().date())
            if st.form_submit_button("Guardar plan", type="primary", **STRETCH):
                if not tarea.strip():
                    st.error("Indica la tarea.")
                else:
                    with db() as conn:
                        conn.execute(
                            """INSERT INTO planes_pm (id_activo, tarea, frecuencia_dias, horas, ultima_ejecucion)
                               VALUES (?,?,?,?,?)""", (int(id_act), tarea.strip(), int(frec), float(horas), ult.isoformat()))
                    flash("Plan preventivo creado.")
                    st.rerun()


def vista_bot_ia():
    col_a, col_b = st.columns([1.5, 1])
    with col_a:
        st.subheader("Bolsa de órdenes pendientes de asignar")
        with db() as conn:
            df = pd.read_sql_query(
                """SELECT id_ot, prioridad, tipo, titulo, cliente, direccion, horas FROM v_ot
                   WHERE estado='Bolsa IA' ORDER BY CASE prioridad WHEN 'Urgente' THEN 0 WHEN 'Alta' THEN 1
                   WHEN 'Media' THEN 2 ELSE 3 END, id_ot""", conn)
        st.dataframe(df, hide_index=True, **STRETCH)
    with col_b:
        with st.container(border=True):
            st.markdown("### 👷 Técnicos activos")
            todos = tecnicos(solo_activos=False)
            sel = st.multiselect("Disponibles para planificar", todos, default=tecnicos(), key="sel_activos")
            if st.button("Guardar disponibilidad", **STRETCH):
                with db() as conn:
                    conn.executemany("UPDATE usuarios SET activo=? WHERE username=?",
                                     [(1 if u in sel else 0, u) for u in todos])
                st.rerun()
        with st.container(border=True):
            st.markdown("### 🧠 Optimizador de rutas (8 h)")
            st.write("Reparte la bolsa entre los técnicos activos: por prioridad, luego vecino más próximo "
                     "(distancia geodésica), con desplazamiento estimado y jornadas de 8 horas.")
            if st.button("🚀 Ejecutar IA de Enrutamiento", type="primary", **STRETCH):
                if not tecnicos():
                    st.warning("No hay técnicos activos.")
                else:
                    with st.spinner("Calculando distancias y asignando slots..."):
                        time.sleep(0.8)
                        n = bot_optimizar_rutas()
                    if n:
                        flash(f"¡{n} órdenes programadas correctamente!")
                        st.rerun()
                    else:
                        st.warning("La bolsa está vacía.")


def vista_tareas_operario(usuario):
    with db() as conn:
        df = pd.read_sql_query(
            """SELECT id_ot, titulo, tipo, prioridad, cliente, sede, direccion, municipio, lat, lon,
                      activo_codigo, activo, fecha_prog, hora_inicio, hora_fin, horas
               FROM v_ot WHERE operario_asignado=? AND estado='Programada' ORDER BY fecha_prog, hora_inicio""",
            conn, params=(usuario,))
        inv = conn.execute("SELECT codigo, descripcion, stock FROM inventario ORDER BY codigo").fetchall()
    opciones = {"— Sin material —": None}
    for cod, desc, stock in inv:
        opciones[f"{cod} · {desc} (stock {stock})"] = cod

    if df.empty:
        st.info("No hay tareas pendientes en tu ruta actual.")
    for _, r in df.iterrows():
        with st.container(border=True):
            marca = "⚠️ AVERÍA · " if r["tipo"] == "Avería" else ""
            st.markdown(f"#### 🔧 {marca}{r['fecha_prog']} ({r['hora_inicio']} - {r['hora_fin']}) | {r['titulo']}")
            st.markdown(f"**🏢 Cliente:** {r['cliente']} · {r['sede']}  \n"
                        f"**📍 Dirección:** {r['direccion']}, {r['municipio']}  \n"
                        f"**⚙️ Activo:** {r['activo_codigo']} · {r['activo']} · Prioridad {r['prioridad']}")
            st.markdown(f"[🧭 Cómo llegar](https://www.google.com/maps/dir/?api=1&destination={r['lat']},{r['lon']})")
            with st.expander("▶️ EJECUTAR TRABAJO"):
                with st.form(f"form_{r['id_ot']}"):
                    horas = st.number_input("Horas reales", 0.25, 24.0, float(r["horas"]), 0.25, key=f"h_{r['id_ot']}")
                    mat = st.selectbox("Material utilizado", list(opciones), key=f"m_{r['id_ot']}")
                    cant = st.number_input("Cantidad", 0, 1000, 0, key=f"c_{r['id_ot']}")
                    obs = st.text_area("Observaciones", key=f"o_{r['id_ot']}")
                    if st.form_submit_button("✅ Completar Orden", type="primary"):
                        ok, msg = completar_orden(int(r["id_ot"]), horas, opciones[mat], int(cant), obs)
                        if ok:
                            st.rerun()
                        else:
                            st.error(msg)


def vista_historial(usuario=None):
    base = """SELECT id_ot, titulo, cliente, activo_codigo, tipo, operario_asignado, fecha_prog, horas_reales,
                     coste, venta, ROUND(venta - coste, 2) AS margen
              FROM v_ot WHERE estado='Completada'"""
    with db() as conn:
        if usuario:
            df = pd.read_sql_query(base + " AND operario_asignado=? ORDER BY id_ot DESC LIMIT 100", conn, params=(usuario,))
        else:
            df = pd.read_sql_query(base + " ORDER BY id_ot DESC LIMIT 100", conn)
    if df.empty:
        st.info("Todavía no hay órdenes completadas.")
        return
    st.dataframe(df, hide_index=True, **STRETCH)
    st.markdown("##### 🧾 Albaranes (últimas 15 órdenes)")
    for _, r in df.head(15).iterrows():
        c1, c2 = st.columns([4, 1])
        c1.write(f"OT-{int(r['id_ot']):05d} · {r['titulo']} · {r['cliente']}")
        try:
            pdf_bytes = generar_albaran(int(r["id_ot"]))
        except Exception as e:
            c2.error("Error PDF")
            st.caption(f"⚠️ {type(e).__name__}: {e}")
            continue
        c2.download_button("📄 PDF", data=pdf_bytes, file_name=f"albaran_OT-{int(r['id_ot']):05d}.pdf",
                           mime="application/pdf", key=f"alb_{usuario or 'all'}_{r['id_ot']}")


def vista_inventario():
    with db() as conn:
        df = pd.read_sql_query("SELECT codigo, descripcion, stock, coste, venta FROM inventario ORDER BY codigo", conn)
    st.dataframe(df, hide_index=True, **STRETCH)
    with st.form("reponer"):
        st.markdown("##### ➕ Reponer stock")
        codigo = st.selectbox("Material", df["codigo"].tolist())
        cant = st.number_input("Unidades a añadir", 1, 10000, 10)
        if st.form_submit_button("Reponer"):
            with db() as conn:
                conn.execute("UPDATE inventario SET stock = stock + ? WHERE codigo=?", (int(cant), codigo))
            st.rerun()


def vista_login():
    st.markdown("<h1 style='text-align: center; color: #1E3A8A; margin-top: 50px;'>⚙️ SIGMA-IA | Terrassa</h1>",
                unsafe_allow_html=True)
    _, col, _ = st.columns([1, 1.2, 1])
    with col, st.container(border=True):
        with st.form("login"):
            user = st.text_input("Usuario")
            pwd = st.text_input("Contraseña", type="password")
            enviado = st.form_submit_button("Iniciar Sesión", type="primary", **STRETCH)
        if enviado:
            rol = autenticar(user.strip(), pwd)
            if rol:
                st.session_state.update({"logged_in": True, "rol": rol, "user": user.strip()})
                st.rerun()
            else:
                st.error("❌ Credenciales incorrectas.")


# ==========================================
# MAIN
# ==========================================
def main():
    inicializar_bd()
    st.session_state.setdefault("logged_in", False)
    if not st.session_state["logged_in"]:
        vista_login()
        return

    user, rol = st.session_state["user"], st.session_state["rol"]
    with st.sidebar:
        st.markdown(f"### 👤 {user.upper()}")
        st.caption(f"Rol: {rol} · Central: Terrassa (BCN)")
        st.divider()
        if st.button("🚪 Cerrar Sesión", **STRETCH):
            st.session_state.update({"logged_in": False, "rol": None, "user": None})
            st.rerun()

    if rol == "Operario":
        st.title("📱 Portal del Técnico de Campo")
        secciones = {
            "🛣️ Tareas": lambda: vista_tareas_operario(user),
            "🗺️ Mapa de ruta": lambda: (st.subheader("Mis intervenciones"), vista_mapa(user, key="op")),
            "📅 Agenda": lambda: vista_agenda(user),
            "🧾 Historial": lambda: vista_historial(user),
        }
        clave = "nav_operario"
    else:
        st.title("🛰️ Command Center - Planta Terrassa")
        secciones = {
            "📊 KPIs": vista_kpis,
            "🗺️ Despacho y Mapa": vista_despacho,
            "👥 Clientes y Sedes": vista_clientes,
            "🏭 Activos y Planes PM": vista_activos,
            "🤖 Bot IA": vista_bot_ia,
            "👷 Planner Técnicos": vista_planner,
            "📅 Agenda Global": vista_agenda,
            "📦 Inventario": vista_inventario,
            "🧾 Historial": vista_historial,
        }
        clave = "nav_manager"

    elegida = nav(list(secciones), clave)
    mensaje = st.session_state.pop("flash", None)
    if mensaje:
        st.success(mensaje)
    secciones[elegida]()


main()
