"""
SIGMA-IA Enterprise - GMAO para entorno industrial (Terrassa, Barcelona)

Arquitectura en capas dentro de un único fichero (para el TFG se puede separar en
db.py / servicios.py / ui.py):
  1. Persistencia  -> SQLite (context manager, claves foráneas, CHECK)
  2. Servicios     -> autenticación, optimizador de rutas, cierre de OT, albaranes
  3. Presentación  -> Streamlit (vistas por rol)
"""
import hashlib
import hmac
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta

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
JORNADA_H = 8.0                            # Duración de la jornada
INICIO_JORNADA = "08:00"
VELOCIDAD_KMH = 40.0                       # Velocidad media supuesta en polígonos
TARIFA_COSTE_H = 35.0                      # €/h coste interno (supuesto de demo)
TARIFA_VENTA_H = 60.0                      # €/h facturación (supuesto de demo)


def _version_tuple(v: str):
    partes = []
    for p in v.split(".")[:3]:
        d = "".join(ch for ch in p if ch.isdigit())
        partes.append(int(d) if d else 0)
    return tuple(partes)


# `use_container_width` está obsoleto y se va retirando; `width="stretch"` es el sustituto.
STRETCH = (
    {"width": "stretch"}
    if _version_tuple(st.__version__) >= (1, 50, 0)
    else {"use_container_width": True}
)


# ==========================================
# 1. PERSISTENCIA
# ==========================================
@contextmanager
def db():
    """Abre una conexión por operación; commit si todo va bien, rollback si falla."""
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
    """Si existe una BD con el esquema anterior, la aparta (con copia) y se crea una nueva."""
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
                rol      TEXT NOT NULL CHECK (rol IN ('Operario','Manager'))
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
                equipo            TEXT,
                direccion         TEXT,
                lat               REAL,
                lon               REAL,
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
                coste             REAL,            -- se fija al completar
                venta             REAL
            );
            CREATE INDEX IF NOT EXISTS idx_ordenes_estado ON ordenes(estado);
            CREATE INDEX IF NOT EXISTS idx_ordenes_operario ON ordenes(operario_asignado, estado);
            """
        )

        if conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0:
            # Usuarios de DEMO. En producción: alta por el Manager, contraseñas robustas.
            conn.executemany(
                "INSERT INTO usuarios (username, pw_hash, rol) VALUES (?,?,?)",
                [
                    ("tecnico1", hash_password("123"), "Operario"),
                    ("manager1", hash_password("admin"), "Manager"),
                ],
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

        if conn.execute("SELECT COUNT(*) FROM ordenes").fetchone()[0] == 0:
            locs = [
                ("Pol. Ind. Santa Margarida", 41.575, 2.002, "Bomba Centrífuga B-01", 2.0),
                ("Pol. Ind. Can Vinyals", 41.550, 1.985, "Compresor C-02", 3.0),
                ("Zona Nord - St. Pere", 41.580, 2.020, "Climatizador Central", 2.5),
                ("Carretera de Rubí", 41.540, 2.030, "Cinta Transportadora", 4.0),
                ("Estación de Bombeo Oeste", 41.560, 1.970, "Válvula de Alta Presión", 3.5),
            ]
            conn.executemany(
                """INSERT INTO ordenes (titulo, equipo, direccion, lat, lon, horas, estado)
                   VALUES (?,?,?,?,?,?,'Bolsa IA')""",
                [
                    (f"Preventivo Recurrente P-{i + 1}", l[3], l[0], l[1], l[2], l[4])
                    for i, l in ((i, locs[i % 5]) for i in range(12))
                ],
            )


# ==========================================
# 2. SERVICIOS
# ==========================================
def autenticar(username: str, password: str):
    """Devuelve el rol si las credenciales son válidas; None en caso contrario."""
    with db() as conn:
        fila = conn.execute(
            "SELECT pw_hash, rol FROM usuarios WHERE username=?", (username,)
        ).fetchone()
    if fila and verify_password(password, fila[0]):
        return fila[1]
    return None


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlam = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def siguiente_laborable(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:  # sábado/domingo
        d += timedelta(days=1)
    return d


def bot_optimizar_rutas(operario: str = "tecnico1") -> int:
    """
    Heurística voraz de vecino más próximo con jornadas de JORNADA_H horas.
    - Parte de la central cada día.
    - Elige siempre la tarea pendiente más cercana a la posición actual (haversine).
    - Tiempo de desplazamiento = distancia / velocidad media.
    - Cuando la siguiente tarea no cabe en la jornada, abre el siguiente día laborable.
    Devuelve el número de órdenes programadas.
    Complejidad: O(n^2) (n = tareas pendientes), suficiente para cientos de OT.
    """
    with db() as conn:
        filas = conn.execute(
            "SELECT id_ot, lat, lon, horas FROM ordenes WHERE estado='Bolsa IA'"
        ).fetchall()
        if not filas:
            return 0

        pendientes = [{"id": f[0], "lat": f[1], "lon": f[2], "horas": f[3]} for f in filas]
        actualizaciones = []
        dia = siguiente_laborable(date.today())

        while pendientes:
            inicio = datetime.strptime(f"{dia} {INICIO_JORNADA}", "%Y-%m-%d %H:%M")
            limite = inicio + timedelta(hours=JORNADA_H)
            reloj, pos, tareas_dia = inicio, (BASE_LAT, BASE_LON), 0

            while pendientes:
                sig = min(pendientes, key=lambda p: haversine_km(*pos, p["lat"], p["lon"]))
                viaje = timedelta(hours=haversine_km(*pos, sig["lat"], sig["lon"]) / VELOCIDAD_KMH)
                ini_tarea = reloj + viaje
                fin_tarea = ini_tarea + timedelta(hours=sig["horas"])

                # Si no es la primera tarea del día y no cabe, se cierra la jornada.
                # (La primera siempre se asigna para evitar bucles con OT > jornada.)
                if tareas_dia > 0 and fin_tarea > limite:
                    break

                actualizaciones.append(
                    (
                        operario,
                        dia.strftime("%Y-%m-%d"),
                        ini_tarea.strftime("%H:%M"),
                        fin_tarea.strftime("%H:%M"),
                        sig["id"],
                    )
                )
                pendientes.remove(sig)
                reloj, pos, tareas_dia = fin_tarea, (sig["lat"], sig["lon"]), tareas_dia + 1

            dia = siguiente_laborable(dia)

        conn.executemany(
            """UPDATE ordenes
               SET operario_asignado=?, fecha_prog=?, hora_inicio=?, hora_fin=?, estado='Programada'
               WHERE id_ot=?""",
            actualizaciones,
        )
        return len(actualizaciones)


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
               SET estado='Completada', horas_reales=?, material=?, cant_mat=?, obs=?,
                   coste=?, venta=?
               WHERE id_ot=? AND estado='Programada'""",
            (horas_reales, codigo_mat, cantidad, obs, round(coste, 2), round(venta, 2), id_ot),
        )
    return True, ""


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
    pdf.cell(0, 6, _l1(f"Emitido: {datetime.now():%d/%m/%Y %H:%M}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    campos = [
        ("Orden de trabajo", f"OT-{o[0]:05d}"),
        ("Trabajo", o[1]),
        ("Equipo", o[2]),
        ("Ubicación", o[3]),
        ("Técnico", o[4]),
        ("Fecha", o[5]),
        ("Horas reales", f"{o[6]:.2f} h"),
        ("Material", f"{o[8]} x{o[9]}" if o[7] else "—"),
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
def vista_login():
    st.markdown(
        "<h1 style='text-align: center; color: #1E3A8A; margin-top: 50px;'>⚙️ SIGMA-IA | Terrassa</h1>",
        unsafe_allow_html=True,
    )
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


def vista_tareas_operario(usuario):
    with db() as conn:
        df = pd.read_sql_query(
            """SELECT id_ot, titulo, equipo, direccion, fecha_prog, hora_inicio, hora_fin, horas
               FROM ordenes WHERE operario_asignado=? AND estado='Programada'
               ORDER BY fecha_prog, hora_inicio""",
            conn,
            params=(usuario,),
        )
        inv = conn.execute("SELECT codigo, descripcion, stock FROM inventario ORDER BY codigo").fetchall()

    opciones = {"— Sin material —": None}
    for cod, desc, stock in inv:
        opciones[f"{cod} · {desc} (stock {stock})"] = cod

    if df.empty:
        st.info("No hay tareas pendientes en tu ruta actual.")
    for _, r in df.iterrows():
        with st.container(border=True):
            st.markdown(f"#### 🔧 {r['fecha_prog']} ({r['hora_inicio']} - {r['hora_fin']}) | {r['titulo']}")
            st.markdown(f"**📍 Ubicación:** {r['direccion']} | **Activo:** {r['equipo']}")
            with st.expander("▶️ EJECUTAR TRABAJO"):
                with st.form(f"form_{r['id_ot']}"):
                    horas = st.number_input(
                        "Horas reales", 0.25, 24.0, float(r["horas"]), 0.25, key=f"h_{r['id_ot']}"
                    )
                    mat = st.selectbox("Material utilizado", list(opciones), key=f"m_{r['id_ot']}")
                    cant = st.number_input("Cantidad", 0, 1000, 0, key=f"c_{r['id_ot']}")
                    obs = st.text_area("Observaciones", key=f"o_{r['id_ot']}")
                    if st.form_submit_button("✅ Completar Orden", type="primary"):
                        ok, msg = completar_orden(int(r["id_ot"]), horas, opciones[mat], int(cant), obs)
                        if ok:
                            st.rerun()
                        else:
                            st.error(msg)


def vista_calendario(usuario=None):
    with db() as conn:
        if usuario:
            df = pd.read_sql_query(
                "SELECT * FROM ordenes WHERE operario_asignado=? AND estado='Programada'",
                conn,
                params=(usuario,),
            )
        else:
            df = pd.read_sql_query("SELECT * FROM ordenes WHERE estado='Programada'", conn)

    eventos = [
        {
            "title": f"{r['titulo']} ({r['direccion']})" if usuario else f"[{r['operario_asignado']}] {r['titulo']}",
            "start": f"{r['fecha_prog']}T{r['hora_inicio']}:00",
            "end": f"{r['fecha_prog']}T{r['hora_fin']}:00",
            "backgroundColor": "#1E3A8A" if usuario else "#10B981",
        }
        for _, r in df.iterrows()
    ]
    if calendar is None:
        st.warning("streamlit-calendar no está instalado. Se muestra una tabla en su lugar.")
        st.dataframe(pd.DataFrame(eventos), hide_index=True, **STRETCH)
        return
    opciones = {"initialView": "timeGridWeek", "slotMinTime": "07:00:00", "slotMaxTime": "19:00:00"}
    # La key incluye el nº de eventos para forzar el refresco del componente.
    calendar(events=eventos, options=opciones, key=f"cal_{usuario or 'global'}_{len(eventos)}")


def vista_historial(usuario=None):
    with db() as conn:
        if usuario:
            df = pd.read_sql_query(
                """SELECT id_ot, titulo, operario_asignado, fecha_prog, horas_reales, coste, venta,
                          ROUND(venta - coste, 2) AS margen
                   FROM ordenes WHERE estado='Completada' AND operario_asignado=? ORDER BY id_ot DESC""",
                conn,
                params=(usuario,),
            )
        else:
            df = pd.read_sql_query(
                """SELECT id_ot, titulo, operario_asignado, fecha_prog, horas_reales, coste, venta,
                          ROUND(venta - coste, 2) AS margen
                   FROM ordenes WHERE estado='Completada' ORDER BY id_ot DESC""",
                conn,
            )
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
        except Exception as e:  # p. ej. conflicto fpdf / fpdf2
            c2.error("Error PDF")
            st.caption(f"⚠️ {type(e).__name__}: {e}")
            continue
        c2.download_button(
            "📄 PDF",
            data=pdf_bytes,
            file_name=f"albaran_OT-{int(r['id_ot']):05d}.pdf",
            mime="application/pdf",
            key=f"alb_{usuario or 'all'}_{r['id_ot']}",
        )


def vista_inventario():
    with db() as conn:
        df = pd.read_sql_query(
            "SELECT codigo, descripcion, stock, coste, venta FROM inventario ORDER BY codigo", conn
        )
    st.dataframe(df, hide_index=True, **STRETCH)
    with st.form("reponer"):
        st.markdown("##### ➕ Reponer stock")
        codigo = st.selectbox("Material", df["codigo"].tolist())
        cant = st.number_input("Unidades a añadir", 1, 10000, 10)
        if st.form_submit_button("Reponer"):
            with db() as conn:
                conn.execute("UPDATE inventario SET stock = stock + ? WHERE codigo=?", (int(cant), codigo))
            st.rerun()


def vista_bot_ia():
    col_a, col_b = st.columns([1.5, 1])
    with col_a:
        st.subheader("Bolsa de Preventivos Pendientes de Asignar")
        with db() as conn:
            df = pd.read_sql_query(
                "SELECT id_ot, titulo, direccion, horas FROM ordenes WHERE estado='Bolsa IA'", conn
            )
        st.dataframe(df, hide_index=True, **STRETCH)
    with col_b, st.container(border=True):
        st.markdown("### 🧠 Optimizador de Rutas (8h)")
        st.write(
            "Ordena los preventivos por vecino más próximo (distancia geodésica), "
            "estima el desplazamiento según la distancia y los reparte en jornadas de 8 horas."
        )
        if st.button("🚀 Ejecutar IA de Enrutamiento", type="primary", **STRETCH):
            with st.spinner("Calculando distancias geográficas y asignando slots..."):
                time.sleep(0.8)
                n = bot_optimizar_rutas()
            if n:
                st.success(f"¡{n} órdenes programadas correctamente!")
                time.sleep(0.8)
                st.rerun()
            else:
                st.warning("La bolsa está vacía.")


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
        t1, t2, t3 = st.tabs(["🛣️ Tareas Asignadas", "📅 Mi Agenda Semanal", "🧾 Historial y Albaranes"])
        with t1:
            vista_tareas_operario(user)
        with t2:
            st.subheader("Cuadrante de Carga de Trabajo")
            vista_calendario(user)
        with t3:
            vista_historial(user)

    elif rol == "Manager":
        st.title("🛰️ Command Center - Planta Terrassa")
        t1, t2, t3, t4 = st.tabs(
            ["🤖 Bot IA (Enrutamiento)", "📅 Agenda Global", "📦 Inventario", "🧾 Historial y Albaranes"]
        )
        with t1:
            vista_bot_ia()
        with t2:
            st.subheader("Planificador Operativo Global")
            vista_calendario()
        with t3:
            vista_inventario()
        with t4:
            vista_historial()


main()
