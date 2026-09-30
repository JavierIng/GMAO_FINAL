import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime

# ==========================================
# CONFIGURACIÓN VISUAL DE LA PÁGINA
# ==========================================
st.set_page_config(page_title="SIGMA-IA | GMAO", layout="wide", page_icon="⚙️️")

# ==========================================
# BASE DE DATOS
# ==========================================
def conectar():
    return sqlite3.connect("gmao_prod.db", check_same_thread=False)

def inicializar_bd():
    conn = conectar()
    conn.execute('''CREATE TABLE IF NOT EXISTS partes (
                    id_ot INTEGER PRIMARY KEY AUTOINCREMENT,
                    equipo TEXT, operario TEXT, horas REAL,
                    obs TEXT, estado TEXT, fecha DATE)''')
    conn.commit()
    conn.close()

inicializar_bd()

# ==========================================
# SISTEMA DE LOGIN Y SESIÓN
# ==========================================
if 'logged_in' not in st.session_state:
    st.session_state['logged_in'] = False
    st.session_state['rol'] = None
    st.session_state['user'] = None

USUARIOS = {
    "tecnico1": {"pass": "123", "rol": "Operario"},
    "manager1": {"pass": "admin", "rol": "Manager"}
}

if not st.session_state['logged_in']:
    # Diseño de la pantalla de Login
    st.markdown("<h1 style='text-align: center; color: #1E3A8A;'>⚙️ SIGMA-IA</h1>", unsafe_allow_html=True)
    st.markdown("<h3 style='text-align: center; color: #555;'>Plataforma Inteligente de Gestión de Activos</h3>", unsafe_allow_html=True)
    st.write("---")
    
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        with st.form("login_form"):
            st.subheader("🔐 Acceso al Sistema")
            user = st.text_input("Usuario (Ej: tecnico1 o manager1)")
            pwd = st.text_input("Contraseña (Ej: 123 o admin)", type="password")
            submit = st.form_submit_button("Iniciar Sesión", type="primary", use_container_width=True)
            
            if submit:
                if user in USUARIOS and USUARIOS[user]["pass"] == pwd:
                    st.session_state['logged_in'] = True
                    st.session_state['rol'] = USUARIOS[user]["rol"]
                    st.session_state['user'] = user
                    st.rerun()
                else:
                    st.error("❌ Credenciales incorrectas. Inténtelo de nuevo.")
else:
    # ==========================================
    # BARRA LATERAL (MENÚ)
    # ==========================================
    with st.sidebar:
        st.markdown(f"### 👤 {st.session_state['user'].upper()}")
        st.markdown(f"**Rol:** {st.session_state['rol']}")
        st.write("---")
        if st.button("🚪 Cerrar Sesión", use_container_width=True):
            st.session_state['logged_in'] = False
            st.rerun()

    # ==========================================
    # ROL 1: VISTA OPERARIO DE CAMPO
    # ==========================================
    if st.session_state['rol'] == "Operario":
        st.title("🛠️ Terminal de Planta")
        st.info("Registre los datos de la intervención en el activo físico.")
        
        with st.form("form_operario"):
            col1, col2 = st.columns(2)
            with col1:
                equipo = st.selectbox("Máquina / Activo intervenido", ["Bomba Centrífuga B-01", "Válvula de Seguridad PSV-104", "Compresor C-02", "Intercambiador HX-99"])
            with col2:
                horas = st.number_input("Horas de trabajo invertidas", min_value=0.5, value=1.0, step=0.5)
            
            obs = st.text_area("Observaciones y evidencias", placeholder="Ej: Se detecta desgaste en el rodamiento. Se sustituye junta de estanqueidad...")
            
            enviado = st.form_submit_button("Firmar y Enviar Parte a QA", type="primary", use_container_width=True)
            
            if enviado:
                conn = conectar()
                conn.execute("INSERT INTO partes (equipo, operario, horas, obs, estado, fecha) VALUES (?, ?, ?, ?, 'Pendiente', ?)", 
                             (equipo, st.session_state['user'], horas, obs, datetime.now().strftime("%Y-%m-%d %H:%M")))
                conn.commit()
                st.success("✅ Parte de trabajo enviado a revisión del Manager con éxito.")

    # ==========================================
    # ROL 2: VISTA MANAGER / FINANZAS
    # ==========================================
    elif st.session_state['rol'] == "Manager":
        st.title("📊 Dashboard Directivo y Aprobaciones")
        
        # Panel de Métricas KPI
        conn = conectar()
        df = pd.read_sql_query("SELECT * FROM partes", conn)
        
        col1, col2, col3 = st.columns(3)
        col1.metric("Órdenes Totales", len(df))
        col2.metric("Pendientes de Revisión", len(df[df['estado'] == 'Pendiente']))
        col3.metric("Horas Totales Imputadas", df['horas'].sum() if not df.empty else 0)
        
        st.write("---")
        st.subheader("📋 Bandeja de Calidad (QA)")
        
        df_pendientes = df[df['estado'] == 'Pendiente']
        
        if df_pendientes.empty:
            st.success("👍 Todo al día. No hay órdenes pendientes de revisión.")
        else:
            for index, row in df_pendientes.iterrows():
                with st.expander(f"🔴 OT-{row['id_ot']} | {row['equipo']} | Técnico: {row['operario']}", expanded=True):
                    colA, colB = st.columns([3, 1])
                    with colA:
                        st.write(f"**Fecha:** {row['fecha']}")
                        st.write(f"**Horas:** {row['horas']} h")
                        st.write(f"**Reporte:** {row['obs']}")
                    with colB:
                        if st.button(f"Aprobar OT", key=f"apr_{row['id_ot']}", type="primary", use_container_width=True):
                            conn.execute(f"UPDATE partes SET estado='Aprobada' WHERE id_ot={row['id_ot']}")
                            conn.commit()
                            st.rerun()
        
        st.write("---")
        st.subheader("📚 Historial Completo de Activos")
        st.dataframe(df, use_container_width=True, hide_index=True)
