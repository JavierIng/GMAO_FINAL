"""
SIGMA-IA Enterprise - GMAO para entorno industrial (Terrassa, Barcelona)

Capas:
  1. Persistencia  -> SQLite (context manager, claves foráneas, CHECK, migración ligera)
  2. Servicios     -> autenticación, optimizador multi-técnico, alta/cierre de OT, albaranes
  3. Presentación  -> Streamlit (vistas por rol, navegación con st.radio)

NOTA: la navegación NO usa st.tabs a propósito. FullCalendar dentro de una pestaña
oculta se renderiza con tamaño 0 y "desaparece"; con st.radio solo se dibuja la
sección activa.
"""
import hashlib
import hmac
import math
import os
import sqlite3
import time
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
except Exception:  # sin tzdata (p. ej. Windows)
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
BASE_LAT, BASE_LON = 41.5631, 2.0100      # Central en Terrassa
JORNADA_H = 8.0
INICIO_JORNADA = "08:00"
VELOCIDAD_KMH = 40.0                       # supuesto de demo
TARIFA_COSTE_H = 35.0                      # €/h coste interno (supuesto de demo)
TARIFA_VENTA_H = 60.0                      # €/h facturación (supuesto de demo)
TIPOS_OT = ["Preventivo", "Correctivo", "Avería"]
ESTADOS = ["Bolsa IA", "Programada", "Completada"]
PALETA = ["#2563EB", "#10B981", "#F59E0B", "#8B5CF6", "#EF4444", "#14B8A6", "#EC4899", "#64748B"]
MAP_STYLE = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"
SCHEDULER_KEY = "CC-Attribution-NonCommercial-NoDerivatives"  # licencia gratuita no comercial


def _version_tuple(v: str):
    partes = []
    for p in v.split(".")[:3]:
        d = "".join(ch for ch in p if ch.isdigit())
        partes.append(int(d) if d else 0)
    return tuple(partes)


# `use_container_width` está obsoleto; `width="stretch"` es el sustituto.
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
    """BD de la primera versión (sin usuarios/horas_reales): se aparta con copia."""
    if not os.path.exists(DB_PATH):
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        tablas = {f[0] for f in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols = [f[1] for f in conn.execute("PRAGMA table_info(ordenes)")]
    finally:
        conn.close()
    if tablas and ("usuarios" not in tablas or "horas_reales" not in cols):
        os.replace(DB_PATH, f"gmao_terrassa_antigua_{datetime.now():%Y%m%d_%H%M%S}.db")


def inicializar_bd():
    _respaldar_bd_antigua()
    with db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS usuarios (
                username TEXT PRIMARY KEY,
                pw_hash  TEXT NOT NULL,
                rol      TEXT NOT NULL CHECK (rol IN ('Operario','Manager')),
                activo   INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS inventario (
                codigo      TEXT PRIMARY KEY,
                descripcion TEXT NOT NULL,
                stock       INTEGER NOT NULL CHECK (stock >= 0),
                coste       REAL NOT NULL,
                venta       REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS instalaciones (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL UNIQUE,
                lat    REAL NOT NULL,
                lon    REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ordenes (
                id_ot             INTEGER PRIMARY KEY AUTOINCREMENT,
                titulo            TEXT NOT NULL,
                equipo            TEXT,
                direccion         TEXT,
                lat               REAL,
                lon               REAL,
                tipo              TEXT NOT NULL DEFAULT 'Preventivo',
                operario_asignado TEXT REFERENCES usuarios(username),
                fecha_prog        TEXT,            -- ISO-8601 YYYY-MM-DD
                hora_inicio       TEXT,            -- HH:MM
                hora_fin          TEXT,            -- HH:MM
                horas             REAL NOT NULL,   -- estimadas
                horas_reales      REAL,
                material          TEXT REFERENCES inventario(codigo),
                cant_mat          INTEGER DEFAULT 0,
                obs               TEXT,
                estado            TEXT NOT NULL DEFAULT 'Bolsa IA'
                                  CHECK (estado IN ('Bolsa IA','Programada','Completada')),
                coste             REAL,
                venta             REAL
            );
            CREATE INDEX IF NOT EXISTS idx_ordenes_estado ON ordenes(estado);
            CREATE INDEX IF NOT EXISTS idx_ordenes_operario ON ordenes(operario_asignado, estado);
            """
        )

        # Migración ligera para BD de la versión anterior (misma estructura, sin estas columnas)
        if "activo" not in {f[1] for f in conn.execute("PRAGMA table_info(usuarios)")}:
            conn.execute("ALTER TABLE usuarios ADD COLUMN activo INTEGER NOT NULL DEFAULT 1")
        if "tipo" not in {f[1] for f in conn.execute("PRAGMA table_info(ordenes)")}:
            conn.execute("ALTER TABLE ordenes ADD COLUMN tipo TEXT NOT NULL DEFAULT 'Preventivo'")

        # Usuarios de DEMO (el hash solo se calcula si el usuario no existe)
        demo = [
            ("manager1", "admin", "Manager"),
            ("tecnico1", "123", "Operario"),
            ("tecnico2", "123", "Operario"),
            ("tecnico3", "123", "Operario"),
        ]
        existentes = {f[0] for f in conn.execute("SELECT username FROM usuarios")}
        for u, p, r in demo:
            if u not in existentes:
                conn.execute(
                    "INSERT INTO usuarios (username, pw_hash, rol) VALUES (?,?,?)",
                    (u, hash_password(p), r),
                )

        if conn.execute("SELECT COUNT(*) FROM inventario").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO inventario (codigo, descripcion, stock, coste, venta) VALUES (?,?,?,?,?)",
                [
                    ("MAT-01", "Junta Tórica Viton", 50, 5.0, 15.0),
                    ("MAT-02", "Filtro de aire industrial", 20, 12.0, 30.0),
                    ("MAT-03", "Rodamiento 6205", 30, 8.0, 22.0),
                ],
            )

        if conn.execute("SELECT COUNT(*) FROM instalaciones").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO instalaciones (nombre, lat, lon) VALUES (?,?,?)",
                [
                    ("Pol. Ind. Santa Margarida", 41.575, 2.002),
                    ("Pol. Ind. Can Vinyals", 41.550, 1.985),
                    ("Zona Nord - St. Pere", 41.580, 2.020),
                    ("Carretera de Rubí", 41.540, 2.030),
                    ("Estación de Bombeo Oeste", 41.560, 1.970),
                ],
            )

        if conn.execute("SELECT COUNT(*) FROM ordenes").fetchone()[0] == 0:
            locs = [
                ("Pol. Ind. Santa Margarida", 41.575, 2.002, "Bomba Centrífuga B-01", 2.0),
                ("Pol. Ind. Can Vinyals", 41.550, 1.985, "Compresor C-02", 3.0),
                ("Zona Nord - St. Pere", 41.580, 2.020, "Climatizador Central", 2.5),
                ("Carretera de Rubí", 41.540, 2.030, "Cinta Transportadora", 4.0),
                ("Estación de Bombeo Oeste", 41.560, 1.970, "Válvula de Alta Presión", 3.5),
            ]
            conn.executemany(
                """INSERT INTO ordenes (titulo, equipo, direccion, lat, lon, horas, tipo, estado)
                   VALUES (?,?,?,?,?,?, 'Preventivo', 'Bolsa IA')""",
                [
                    (f"Preventivo Recurrente P-{i + 1}", l[3], l[0], l[1], l[2], l[4])
                    for i, l in ((i, locs[i % 5]) for i in range(12))
                ],
            )


# ==========================================
# 2. SERVICIOS
# ==========================================
def autenticar(username: str, password: str):
    with db() as conn:
        fila = conn.execute(
            "SELECT pw_hash, rol FROM usuarios WHERE username=?", (username,)
        ).fetchone()
    if fila and verify_password(password, fila[0]):
        return fila[1]
    return None


def tecnicos(solo_activos=True):
    q = "SELECT username FROM usuarios WHERE rol='Operario'"
    if solo_activos:
        q += " AND activo=1"
    with db() as conn:
        return [r[0] for r in conn.execute(q + " ORDER BY username")]


def mapa_colores():
    """Color estable por técnico (según orden alfabético de todos los operarios)."""
    return {u: PALETA[i % len(PALETA)] for i, u in enumerate(tecnicos(solo_activos=False))}


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


def bot_optimizar_rutas() -> int:
    """
    Heurística voraz multi-técnico (vecino más próximo + jornadas de JORNADA_H horas).
    Cada día laborable, cada técnico activo sale de la central y va cogiendo de la bolsa
    la tarea más cercana a su posición (haversine); el desplazamiento se estima como
    distancia / velocidad media. Si el técnico ya tiene tareas ese día, se programa a
    continuación de la última. Cuando la siguiente tarea no cabe, se pasa al siguiente técnico/día.
    Devuelve el número de órdenes programadas.
    """
    with db() as conn:
        activos = [
            r[0]
            for r in conn.execute(
                "SELECT username FROM usuarios WHERE rol='Operario' AND activo=1 ORDER BY username"
            )
        ]
        filas = conn.execute(
            "SELECT id_ot, lat, lon, horas FROM ordenes WHERE estado='Bolsa IA'"
        ).fetchall()
        if not filas or not activos:
            return 0

        # Última hora de fin ya ocupada por técnico y día
        ocupado = {}
        for u, f, fin in conn.execute(
            """SELECT operario_asignado, fecha_prog, MAX(hora_fin) FROM ordenes
               WHERE estado IN ('Programada','Completada') AND fecha_prog IS NOT NULL
               GROUP BY operario_asignado, fecha_prog"""
        ):
            ocupado[(u, f)] = fin

        pendientes = [{"id": f[0], "lat": f[1], "lon": f[2], "horas": f[3]} for f in filas]
        actualizaciones = []
        dia = siguiente_laborable(ahora().date())  # primer día laborable a partir de mañana

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
                    sig = min(pendientes, key=lambda p: haversine_km(*pos, p["lat"], p["lon"]))
                    viaje = timedelta(hours=haversine_km(*pos, sig["lat"], sig["lon"]) / VELOCIDAD_KMH)
                    ini_t = reloj + viaje
                    fin_t = ini_t + timedelta(hours=sig["horas"])
                    if tareas_dia > 0 and fin_t > limite:
                        break
                    actualizaciones.append(
                        (tec, dia.strftime("%Y-%m-%d"), ini_t.strftime("%H:%M"), fin_t.strftime("%H:%M"), sig["id"])
                    )
                    pendientes.remove(sig)
                    reloj, pos, tareas_dia = fin_t, (sig["lat"], sig["lon"]), tareas_dia + 1
            dia = siguiente_laborable(dia)

        conn.executemany(
            """UPDATE ordenes
               SET operario_asignado=?, fecha_prog=?, hora_inicio=?, hora_fin=?, estado='Programada'
               WHERE id_ot=?""",
            actualizaciones,
        )
        return len(actualizaciones)


def hay_solape(conn, tecnico, fecha_iso, ini, fin) -> bool:
    filas = conn.execute(
        """SELECT hora_inicio, hora_fin FROM ordenes
           WHERE operario_asignado=? AND fecha_prog=? AND estado IN ('Programada','Completada')""",
        (tecnico, fecha_iso),
    ).fetchall()
    return any(ini < f and fin > i for i, f in filas)  # HH:MM con cero a la izquierda: comparable


def crear_orden(titulo, id_inst, equipo, tipo, horas, tecnico, fecha, hora_ini):
    """Alta de OT. Con técnico -> Programada (comprobando solapes); sin técnico -> Bolsa IA."""
    with db() as conn:
        inst = conn.execute("SELECT nombre, lat, lon FROM instalaciones WHERE id=?", (id_inst,)).fetchone()
        if inst is None:
            return False, "Instalación no válida."
        if tecnico:
            ini_dt = datetime.combine(fecha, hora_ini)
            fin_dt = ini_dt + timedelta(hours=horas)
            if fin_dt.date() != fecha:
                return False, "La orden terminaría después de medianoche."
            ini, fin = ini_dt.strftime("%H:%M"), fin_dt.strftime("%H:%M")
            if hay_solape(conn, tecnico, fecha.strftime("%Y-%m-%d"), ini, fin):
                return False, f"{tecnico} ya tiene una tarea que se solapa en ese horario."
            conn.execute(
                """INSERT INTO ordenes (titulo, equipo, direccion, lat, lon, tipo, operario_asignado,
                                        fecha_prog, hora_inicio, hora_fin, horas, estado)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?, 'Programada')""",
                (titulo, equipo, inst[0], inst[1], inst[2], tipo, tecnico,
                 fecha.strftime("%Y-%m-%d"), ini, fin, horas),
            )
            return True, f"Orden programada para {tecnico} ({ini}-{fin})."
        conn.execute(
            """INSERT INTO ordenes (titulo, equipo, direccion, lat, lon, tipo, horas, estado)
               VALUES (?,?,?,?,?,?,?, 'Bolsa IA')""",
            (titulo, equipo, inst[0], inst[1], inst[2], tipo, horas),
        )
        return True, "Orden añadida a la bolsa para el optimizador."


def completar_orden(id_ot, horas_reales, codigo_mat, cantidad, obs):
    """Cierra la OT y descuenta stock en una única transacción."""
    with db() as conn:
        coste = horas_reales * TARIFA_COSTE_H
        venta = horas_reales * TARIFA_VENTA_H
        if codigo_mat and cantidad > 0:
            precio = conn.execute(
                "SELECT coste, venta FROM inventario WHERE codigo=?", (codigo_mat,)
            ).fetchone()
            cur = conn.execute(
                "UPDATE inventario SET stock = stock - ? WHERE codigo=? AND stock >= ?",
                (cantidad, codigo_mat, cantidad),
            )
            if cur.rowcount == 0:
                return False, "Stock insuficiente para ese material."
            coste += cantidad * precio[0]
            venta += cantidad * precio[1]
        else:
            codigo_mat, cantidad = None, 0
        conn.execute(
            """UPDATE ordenes
               SET estado='Completada', horas_reales=?, material=?, cant_mat=?, obs=?, coste=?, venta=?
               WHERE id_ot=? AND estado='Programada'""",
            (horas_reales, codigo_mat, cantidad, obs, round(coste, 2), round(venta, 2), id_ot),
        )
    return True, ""


def estado_tecnicos():
    """Situación de cada técnico activo en el momento de la consulta."""
    ahora_ = ahora()
    hoy, hhmm = ahora_.date().isoformat(), ahora_.strftime("%H:%M")
    colores = mapa_colores()
    with db() as conn:
        filas = conn.execute(
            """SELECT operario_asignado, titulo, hora_inicio, hora_fin FROM ordenes
               WHERE fecha_prog=? AND estado IN ('Programada','Completada')
                     AND operario_asignado IS NOT NULL ORDER BY hora_inicio""",
            (hoy,),
        ).fetchall()
    res = []
    for t in tecnicos():
        propias = [f for f in filas if f[0] == t]
        horas = sum(_minutos(f[3]) - _minutos(f[2]) for f in propias) / 60
        actual = next((f for f in propias if f[2] <= hhmm < f[3]), None)
        proxima = next((f for f in propias if f[2] > hhmm), None)
        if actual:
            situacion = f"🔴 Ocupado: {actual[1]} (hasta {actual[3]})"
        elif proxima:
            situacion = f"🟢 Libre · próxima tarea a las {proxima[2]}"
        else:
            situacion = "🟢 Libre · sin más tareas hoy"
        res.append({"usuario": t, "color": colores.get(t, "#64748B"), "situacion": situacion,
                    "horas": horas, "carga": horas / JORNADA_H})
    return res


def _l1(texto) -> str:
    """Las fuentes base de FPDF solo admiten latin-1."""
    return str(texto if texto is not None else "").encode("latin-1", "replace").decode("latin-1")


def generar_albaran(id_ot: int) -> bytes:
    with db() as conn:
        o = conn.execute(
            """SELECT o.id_ot, o.titulo, o.equipo, o.direccion, o.operario_asignado, o.fecha_prog,
                      o.horas_reales, o.material, i.descripcion, o.cant_mat, o.obs, o.venta
               FROM ordenes o LEFT JOIN inventario i ON i.codigo = o.material
               WHERE o.id_ot=? AND o.estado='Completada'""",
            (id_ot,),
        ).fetchone()
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
        ("Orden de trabajo", f"OT-{o[0]:05d}"), ("Trabajo", o[1]), ("Equipo", o[2]),
        ("Ubicación", o[3]), ("Técnico", o[4]), ("Fecha", o[5]),
        ("Horas reales", f"{o[6]:.2f} h"), ("Material", f"{o[8]} x{o[9]}" if o[7] else "—"),
    ]
    for etiqueta, valor in campos:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(45, 7, _l1(etiqueta))
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 7, _l1(valor), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 7, "Observaciones", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, _l1(o[10] or "—"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, _l1(f"Importe: {o[11]:.2f} EUR (IVA no incluido)"), new_x="LMARGIN", new_y="NEXT")
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
    return [245, 158, 11]  # Bolsa IA


def vista_mapa(usuario=None, key="mapa"):
    cols = "id_ot, titulo, equipo, direccion, lat, lon, estado, tipo, operario_asignado, fecha_prog"
    with db() as conn:
        if usuario:
            df = pd.read_sql_query(
                f"SELECT {cols} FROM ordenes WHERE operario_asignado=? AND estado='Programada'",
                conn, params=(usuario,),
            )
        else:
            df = pd.read_sql_query(f"SELECT {cols} FROM ordenes", conn)

    if not usuario:
        sel = st.multiselect("Mostrar estados", ESTADOS, default=["Bolsa IA", "Programada"], key=f"flt_{key}")
        df = df[df["estado"].isin(sel)].copy()

    df["operario_asignado"] = df["operario_asignado"].fillna("sin asignar")
    df["fecha_prog"] = df["fecha_prog"].fillna("—")
    df["color"] = [color_punto(e, t) for e, t in zip(df["estado"], df["tipo"])]
    central = pd.DataFrame([{
        "id_ot": 0, "titulo": "Central SIGMA-IA", "equipo": "", "direccion": "Terrassa",
        "lat": BASE_LAT, "lon": BASE_LON, "estado": "Base", "tipo": "", "operario_asignado": "",
        "fecha_prog": "", "color": [17, 24, 39],
    }])

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
            tooltip={"text": "{titulo}\n{direccion} · {equipo}\n{tipo} · {estado}\nTécnico: {operario_asignado} · {fecha_prog}"},
        )
        try:
            st.pydeck_chart(deck, height=520)
        except TypeError:  # versiones antiguas sin parámetro height
            st.pydeck_chart(deck)
    st.caption("🔴 Avería activa · 🟠 Bolsa IA · 🔵 Programada · 🟢 Completada · ⚫ Central Terrassa")


def opciones_calendario(vista="timeGridWeek"):
    return {
        "initialView": vista,
        "headerToolbar": {
            "left": "prev,next today",
            "center": "title",
            "right": "dayGridMonth,timeGridWeek,timeGridDay,listWeek",
        },
        "buttonText": {"today": "Hoy", "month": "Mes", "week": "Semana", "day": "Día", "list": "Lista"},
        "firstDay": 1,
        "slotMinTime": "07:00:00",
        "slotMaxTime": "19:00:00",
        "nowIndicator": True,
        "navLinks": True,
        "allDaySlot": False,
        "height": 700,
    }


def eventos_calendario(usuario=None, con_recurso=False):
    colores = mapa_colores()
    with db() as conn:
        if usuario:
            df = pd.read_sql_query(
                """SELECT * FROM ordenes WHERE operario_asignado=? AND fecha_prog IS NOT NULL
                   AND estado IN ('Programada','Completada')""",
                conn, params=(usuario,),
            )
        else:
            df = pd.read_sql_query(
                """SELECT * FROM ordenes WHERE fecha_prog IS NOT NULL
                   AND estado IN ('Programada','Completada')""",
                conn,
            )
    eventos = []
    for _, r in df.iterrows():
        marca = "✔ " if r["estado"] == "Completada" else ("⚠ " if r["tipo"] == "Avería" else "")
        titulo = f"{marca}{r['titulo']}" if usuario else f"{marca}[{r['operario_asignado']}] {r['titulo']}"
        color = colores.get(r["operario_asignado"], "#64748B")
        ev = {
            "title": titulo,
            "start": f"{r['fecha_prog']}T{r['hora_inicio']}:00",
            "end": f"{r['fecha_prog']}T{r['hora_fin']}:00",
            "backgroundColor": color,
            "borderColor": color,
        }
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
    colores = mapa_colores()
    activos = set(tecnicos())
    partes = [
        f"<span style='color:{c};font-size:1.2em'>●</span> {u}{'' if u in activos else ' (inactivo)'}"
        for u, c in colores.items()
    ]
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
        cols = st.columns(len(fila))
        for col, t in zip(cols, fila):
            with col, st.container(border=True):
                st.markdown(
                    f"<span style='color:{t['color']};font-size:1.4em'>●</span> **{t['usuario']}**",
                    unsafe_allow_html=True,
                )
                st.write(t["situacion"])
                st.progress(min(t["carga"], 1.0))
                st.caption(f"{t['horas']:.1f} h de {JORNADA_H:.0f} h programadas hoy")

    opciones = {
        "initialView": "resourceTimelineDay",
        "initialDate": ahora().date().isoformat(),
        "schedulerLicenseKey": SCHEDULER_KEY,
        "headerToolbar": {
            "left": "prev,next today",
            "center": "title",
            "right": "resourceTimelineDay,resourceTimelineWeek",
        },
        "buttonText": {"today": "Hoy"},
        "views": {
            "resourceTimelineDay": {"buttonText": "Día"},
            "resourceTimelineWeek": {"buttonText": "Semana"},
        },
        "firstDay": 1,
        "slotMinTime": "07:00:00",
        "slotMaxTime": "19:00:00",
        "nowIndicator": True,
        "resourceAreaHeaderContent": "Técnicos",
        "resourceAreaWidth": "14%",
        "height": 420,
    }
    colores = mapa_colores()
    opciones["resources"] = [{"id": t["usuario"], "title": t["usuario"],
                              "eventColor": colores.get(t["usuario"], "#64748B")} for t in estado]
    activos = {t["usuario"] for t in estado}
    eventos = [e for e in eventos_calendario(con_recurso=True) if e["resourceId"] in activos]
    pintar_calendario(eventos, opciones, key="cal_planner")
    st.caption("Si la línea de tiempo por técnico no se dibuja, usa «Agenda global»: muestra los mismos datos con un color por técnico.")


def vista_tareas_operario(usuario):
    with db() as conn:
        df = pd.read_sql_query(
            """SELECT id_ot, titulo, equipo, direccion, fecha_prog, hora_inicio, hora_fin, horas, tipo
               FROM ordenes WHERE operario_asignado=? AND estado='Programada'
               ORDER BY fecha_prog, hora_inicio""",
            conn, params=(usuario,),
        )
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
            st.markdown(f"**📍 Ubicación:** {r['direccion']} | **Activo:** {r['equipo']}")
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
    base = """SELECT id_ot, titulo, operario_asignado, fecha_prog, horas_reales, coste, venta,
                     ROUND(venta - coste, 2) AS margen
              FROM ordenes WHERE estado='Completada'"""
    with db() as conn:
        if usuario:
            df = pd.read_sql_query(base + " AND operario_asignado=? ORDER BY id_ot DESC", conn, params=(usuario,))
        else:
            df = pd.read_sql_query(base + " ORDER BY id_ot DESC", conn)
    if df.empty:
        st.info("Todavía no hay órdenes completadas.")
        return
    st.dataframe(df, hide_index=True, **STRETCH)
    st.markdown("##### 🧾 Albaranes")
    for _, r in df.iterrows():
        c1, c2 = st.columns([4, 1])
        c1.write(f"OT-{int(r['id_ot']):05d} · {r['titulo']}")
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


def vista_despacho():
    with db() as conn:
        inst = conn.execute("SELECT id, nombre FROM instalaciones ORDER BY nombre").fetchall()
    col_m, col_f = st.columns([2, 1])
    with col_m:
        st.subheader("🗺️ Mapa de averías e instalaciones")
        vista_mapa(key="despacho")
    with col_f:
        st.subheader("➕ Nueva Orden")
        with st.form("nueva_orden", clear_on_submit=True):
            titulo = st.text_input("Título de la tarea")
            id_inst = st.selectbox("Instalación", [i[0] for i in inst], format_func=lambda x: dict(inst)[x])
            equipo = st.text_input("Equipo / Activo")
            tipo = st.selectbox("Tipo", TIPOS_OT)
            horas = st.number_input("Horas estimadas", 0.5, 12.0, 2.0, 0.5)
            auto = "— Bolsa IA (automático) —"
            tec = st.selectbox("Asignar técnico", [auto] + tecnicos())
            fecha = st.date_input("Fecha", ahora().date())
            hora_ini = st.time_input("Hora de inicio", value=dtime(8, 0), step=900)
            st.caption("Fecha y hora solo se usan si asignas un técnico concreto.")
            if st.form_submit_button("Crear orden", type="primary", **STRETCH):
                if not titulo.strip():
                    st.error("Indica un título.")
                else:
                    ok, msg = crear_orden(titulo.strip(), id_inst, equipo.strip(), tipo, float(horas),
                                          None if tec == auto else tec, fecha, hora_ini)
                    if ok:
                        st.success(msg)
                        time.sleep(0.8)
                        st.rerun()
                    else:
                        st.error(msg)


def vista_bot_ia():
    col_a, col_b = st.columns([1.5, 1])
    with col_a:
        st.subheader("Bolsa de preventivos pendientes de asignar")
        with db() as conn:
            df = pd.read_sql_query(
                "SELECT id_ot, titulo, tipo, direccion, horas FROM ordenes WHERE estado='Bolsa IA'", conn
            )
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
            st.write("Reparte la bolsa entre los técnicos activos por vecino más próximo (distancia geodésica), "
                     "con desplazamiento estimado y jornadas de 8 horas.")
            if st.button("🚀 Ejecutar IA de Enrutamiento", type="primary", **STRETCH):
                if not tecnicos():
                    st.warning("No hay técnicos activos.")
                else:
                    with st.spinner("Calculando distancias y asignando slots..."):
                        time.sleep(0.8)
                        n = bot_optimizar_rutas()
                    if n:
                        st.success(f"¡{n} órdenes programadas correctamente!")
                        time.sleep(0.8)
                        st.rerun()
                    else:
                        st.warning("La bolsa está vacía.")


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
        secciones[nav(list(secciones), "nav_operario")]()

    elif rol == "Manager":
        st.title("🛰️ Command Center - Planta Terrassa")
        secciones = {
            "🗺️ Despacho y Mapa": vista_despacho,
            "🤖 Bot IA": vista_bot_ia,
            "👷 Planner Técnicos": vista_planner,
            "📅 Agenda Global": vista_agenda,
            "📦 Inventario": vista_inventario,
            "🧾 Historial": vista_historial,
        }
        secciones[nav(list(secciones), "nav_manager")]()


main()
