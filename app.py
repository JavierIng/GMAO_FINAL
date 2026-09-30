import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime, date
import time

# ==========================================
# CONFIGURACIÓN Y ESTILOS
# ==========================================
st.set_page_config(page_title="SIGMA-IA | GMAO", layout="wide", page_icon="🚀")
st.markdown("""
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    .stButton>button { border-radius: 8px; font-weight: bold; }
    </style>
""", unsafe_allow_html=True)

# ==========================================
# BASE DE DATOS (NUEVA VERSIÓN CON SEGURIDAD)
# ==========================================
def conectar():
    return sqlite3.connect("gmao_safety.db", check_same_thread=False)

def inicializar_bd():
    conn = conectar()
    c = conn.cursor()
    # Tabla ampliada con Tipo de Mantenimiento y Protocolos
    c.execute('''CREATE TABLE IF NOT EXISTS ordenes (
                    id_ot INTEGER PRIMARY KEY AUTOINCREMENT,
                    titulo TEXT, equipo TEXT, tipo_mant TEXT, protocolo TEXT,
                    lat REAL, lon REAL, operario_asignado TEXT, fecha_prog DATE,
                    horas REAL, material TEXT, cant_mat INTEGER, obs TEXT, 
                    estado TEXT, coste REAL, venta REAL, margen REAL)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS inventario (
                    codigo TEXT PRIMARY KEY, descripcion TEXT, stock INTEGER, coste REAL, venta REAL)''')
    
    c.execute("SELECT COUNT(*) FROM ordenes")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO inventario VALUES ('MAT-01', 'Junta Tórica Viton', 50, 5.0, 15.0)")
        c.execute("INSERT INTO inventario VALUES ('MAT-02', 'Rodamiento SKF', 10, 25.0, 60.0)")
        c.execute('''INSERT INTO ordenes (titulo, equipo, tipo_mant, protocolo, lat, lon, operario_asignado, fecha_prog, estado) 
                     VALUES ('Revisión Trimestral', 'Bomba Centrífuga B-01', 'Preventivo', 'LOTO - Bloqueo Eléctrico', 40.4168, -3.7038, 'tecnico1', ?, 'Programada')''', (date.today(),))
    conn.commit()
    conn.close()

inicializar_bd()

COSTE_HORA, VENTA_HORA = 20.0, 55.0

# ==========================================
# LOGIN
# ==========================================
if 'logged_in' not in st.session_state:
    st.session_state.update({'logged_in': False, 'rol': None, 'user': None})

USUARIOS = {"tecnico1": {"pass": "123", "rol": "Operario"}, "manager1": {"pass": "admin", "rol": "Manager"}}

if not st.session_state['logged_in']:
    st.markdown("<h1 style='text-align: center; color: #1E3A8A; margin-top: 50px;'>🚀 SIGMA-IA</h1>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center;'>Plataforma Inteligente de Gestión de Activos y Seguridad Industrial</p>", unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns([1, 1.2, 1])
    with col2:
        with st.container(border=True):
            user = st.text_input("Usuario")
            pwd = st.text_input("Contraseña", type="password")
            if st.button("Iniciar Sesión", type="primary", use_container_width=True):
                if user in USUARIOS and USUARIOS[user]["pass"] == pwd:
                    st.session_state.update({'logged_in': True, 'rol': USUARIOS[user]["rol"], 'user': user})
                    st.rerun()
                else:
                    st.error("❌ Credenciales incorrectas.")
else:
    with st.sidebar:
        st.markdown(f"### 👤 {st.session_state['user'].upper()}")
        st.caption(f"Rol: {st.session_state['rol']}")
        st.divider()
        if st.button("🚪 Cerrar Sesión", use_container_width=True):
            st.session_state.update({'logged_in': False, 'rol': None, 'user': None})
            st.rerun()

    conn = conectar()

    # ==========================================
    # ROL 1: OPERARIO (CON CHECKLISTS DE SEGURIDAD)
    # ==========================================
    if st.session_state['rol'] == "Operario":
        st.title("📱 Mi Ruta de Trabajo")
        df_tareas = pd.read_sql_query(f"SELECT * FROM ordenes WHERE operario_asignado='{st.session_state['user']}' AND estado='Programada'", conn)
        
        if df_tareas.empty:
            st.success("🎉 No tienes tareas programadas para hoy.")
        else:
            for index, row in df_tareas.iterrows():
                with st.container(border=True):
                    st.markdown(f"#### 🔧 OT-{row['id_ot']} | {row['titulo']}")
                    st.markdown(f"**📍 Activo:** {row['equipo']} | **Tipo:** {row['tipo_mant']}")
                    
                    with st.expander("▶️ INICIAR TRABAJO Y PROTOCOLOS"):
                        # MÓDULO DE SEGURIDAD (PRL)
                        st.error(f"⚠️ **PROTOCOLO DE SEGURIDAD REQUERIDO:** {row['protocolo']}")
                        st.info("📄 [Ver Instrucción Técnica (IT) del Equipo](#)")
                        
                        seguridad_ok = st.checkbox("Declaro que he leído la IT, aplicado el protocolo de seguridad y usado los EPIs correspondientes.", key=f"seg_{row['id_ot']}")
                        
                        st.divider()
                        df_inv = pd.read_sql_query("SELECT codigo, descripcion FROM inventario WHERE stock > 0", conn)
                        materiales = ["Ninguno"] + (df_inv['codigo'] + " - " + df_inv['descripcion']).tolist()
                        
                        colA, colB = st.columns(2)
                        with colA:
                            horas = st.number_input("Horas", min_value=0.5, value=1.0, step=0.5, key=f"h_{row['id_ot']}")
                        with colB:
                            mat_usado = st.selectbox("Material", materiales, key=f"m_{row['id_ot']}")
                            cant_mat = st.number_input("Cantidad", min_value=0, value=1 if mat_usado != "Ninguno" else 0, key=f"c_{row['id_ot']}")
                        
                        evidencia = st.file_uploader("📸 Adjuntar Evidencia", key=f"f_{row['id_ot']}")
                        obs = st.text_area("Notas del técnico", key=f"o_{row['id_ot']}")
                        
                        if st.button("✅ Completar y Enviar", type="primary", use_container_width=True, key=f"btn_{row['id_ot']}", disabled=not seguridad_ok):
                            cod_mat = mat_usado.split(" - ")[0] if mat_usado != "Ninguno" else "N/A"
                            conn.execute('''UPDATE ordenes SET horas=?, material=?, cant_mat=?, obs=?, estado='Completada' 
                                            WHERE id_ot=?''', (horas, cod_mat, cant_mat, obs, row['id_ot']))
                            conn.commit()
                            st.rerun()

    # ==========================================
    # ROL 2: MANAGER (PLANIFICACIÓN Y PRL)
    # ==========================================
    elif st.session_state['rol'] == "Manager":
        st.title("🛰️ Command Center")
        
        tab1, tab2 = st.tabs(["🗺️ Despacho y Rutas", "📋 QA y Facturación"])
        
        with tab1:
            col_mapa, col_form = st.columns([2, 1])
            with col_mapa:
                df_all = pd.read_sql_query("SELECT * FROM ordenes", conn)
                df_mapa = df_all[df_all['estado'] == 'Programada'].copy()
                if not df_mapa.empty: st.map(df_mapa[['lat', 'lon']], zoom=11)
            
            with col_form:
                st.subheader("➕ Nueva Orden")
                with st.container(border=True):
                    titulo = st.text_input("Título de la tarea")
                    equipo = st.selectbox("Activo", ["Bomba Centrífuga B-01", "Válvula PSV-104", "Compresor C-03"])
                    
                    # NUEVOS CAMPOS DE ORGANIZACIÓN INDUSTRIAL
                    tipo = st.selectbox("Tipo de Mantenimiento", ["Preventivo", "Correctivo", "Predictivo", "Inspección Reglamentaria"])
                    protocolo = st.selectbox("Protocolo de Seguridad", ["00-Básico (Solo EPIs)", "01-LOTO (Bloqueo de Energías)", "02-Trabajos en Altura", "03-Espacios Confinados", "04-Atmósferas ATEX"])
                    
                    operario = st.selectbox("Asignar Técnico", ["tecnico1", "tecnico2"])
                    fecha = st.date_input("Fecha")
                    
                    lat_dict = {"Bomba Centrífuga B-01": 40.4168, "Válvula PSV-104": 40.4500, "Compresor C-03": 40.3800}
                    lon_dict = {"Bomba Centrífuga B-01": -3.7038, "Válvula PSV-104": -3.6900, "Compresor C-03": -3.7200}
                    
                    if st.button("Asignar Trabajo", type="primary", use_container_width=True):
                        conn.execute('''INSERT INTO ordenes (titulo, equipo, tipo_mant, protocolo, lat, lon, operario_asignado, fecha_prog, estado) 
                                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Programada')''', 
                                     (titulo, equipo, tipo, protocolo, lat_dict[equipo], lon_dict[equipo], operario, fecha))
                        conn.commit()
                        st.rerun()

        with tab2:
            st.subheader("Órdenes Completadas (Pendientes de Facturar)")
            df_comp = pd.read_sql_query("SELECT * FROM ordenes WHERE estado='Completada'", conn)
            for index, row in df_comp.iterrows():
                with st.container(border=True):
                    st.write(f"**OT-{row['id_ot']} | {row['titulo']} ({row['tipo_mant']})**")
                    if st.button("Aprobar y Facturar", key=f"fac_{row['id_ot']}", type="primary"):
                        c_tot = (row['horas'] * COSTE_HORA)
                        v_tot = (row['horas'] * VENTA_HORA)
                        conn.execute("UPDATE ordenes SET estado='Facturada', coste=?, venta=?, margen=? WHERE id_ot=?", (c_tot, v_tot, v_tot - c_tot, row['id_ot']))
                        conn.commit()
                        st.rerun()
                  
