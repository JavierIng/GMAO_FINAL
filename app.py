import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime
import time

# ==========================================
# CONFIGURACIÓN VISUAL Y BASE DE DATOS
# ==========================================
st.set_page_config(page_title="SIGMA-IA | GMAO Completo", layout="wide", page_icon="⚙")

def conectar():
    return sqlite3.connect("gmao_master.db", check_same_thread=False)

def inicializar_bd():
    conn = conectar()
    c = conn.cursor()
    # Tabla de Partes de Trabajo (OTs)
    c.execute('''CREATE TABLE IF NOT EXISTS partes (
                    id_ot INTEGER PRIMARY KEY AUTOINCREMENT,
                    equipo TEXT, operario TEXT, horas REAL, 
                    material TEXT, cant_mat INTEGER, obs TEXT, 
                    estado TEXT, fecha DATE, coste REAL, venta REAL, margen REAL)''')
    
    # Tabla de Inventario y Precios
    c.execute('''CREATE TABLE IF NOT EXISTS inventario (
                    codigo TEXT PRIMARY KEY, descripcion TEXT, 
                    stock INTEGER, coste REAL, venta REAL)''')
    
    # Inserción de stock inicial si está vacío
    c.execute("SELECT COUNT(*) FROM inventario")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO inventario VALUES ('MAT-01', 'Junta Tórica Viton', 50, 5.0, 15.0)")
        c.execute("INSERT INTO inventario VALUES ('MAT-02', 'Rodamiento SKF', 10, 25.0, 60.0)")
        c.execute("INSERT INTO inventario VALUES ('MAT-03', 'Aceite ISO VG 46 (L)', 100, 3.0, 9.0)")
    
    conn.commit()
    conn.close()

inicializar_bd()

# Tarifas de Mano de Obra (Horas)
COSTE_HORA = 20.0  # Lo que le cuesta la hora a la empresa
VENTA_HORA = 55.0  # A lo que se le factura al cliente

# ==========================================
# SISTEMA DE SESIONES
# ==========================================
if 'logged_in' not in st.session_state:
    st.session_state['logged_in'] = False
    st.session_state['rol'] = None
    st.session_state['user'] = None

USUARIOS = {"tecnico1": {"pass": "123", "rol": "Operario"}, "manager1": {"pass": "admin", "rol": "Manager"}}

if not st.session_state['logged_in']:
    st.markdown("<h1 style='text-align: center; color: #1E3A8A;'>⚙️ SIGMA-IA (Core System)</h1>", unsafe_allow_html=True)
    st.write("---")
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        with st.form("login_form"):
            user = st.text_input("Usuario (tecnico1 o manager1)")
            pwd = st.text_input("Contraseña (123 o admin)", type="password")
            if st.form_submit_button("Entrar", type="primary", use_container_width=True):
                if user in USUARIOS and USUARIOS[user]["pass"] == pwd:
                    st.session_state['logged_in'] = True
                    st.session_state['rol'] = USUARIOS[user]["rol"]
                    st.session_state['user'] = user
                    st.rerun()
                else:
                    st.error("❌ Credenciales incorrectas.")
else:
    with st.sidebar:
        st.markdown(f"### 👤 {st.session_state['user'].upper()}")
        st.markdown(f"**Rol:** {st.session_state['rol']}")
        st.write("---")
        if st.button("🚪 Cerrar Sesión", use_container_width=True):
            st.session_state['logged_in'] = False
            st.rerun()

    # ==========================================
    # ROL 1: OPERARIO DE CAMPO
    # ==========================================
    if st.session_state['rol'] == "Operario":
        st.title("🛠️ Terminal de Planta")
        
        conn = conectar()
        df_inv = pd.read_sql_query("SELECT codigo, descripcion FROM inventario WHERE stock > 0", conn)
        materiales_disp = df_inv['codigo'] + " - " + df_inv['descripcion']
        
        with st.form("form_operario"):
            st.subheader("1. Datos de la Intervención")
            col1, col2 = st.columns(2)
            with col1:
                equipo = st.selectbox("Máquina / Activo", ["Bomba Centrífuga B-01", "Válvula PSV-104"])
                horas = st.number_input("Horas de trabajo", min_value=0.5, value=1.0, step=0.5)
            with col2:
                material = st.selectbox("Repuesto utilizado", materiales_disp.tolist() + ["Ninguno"])
                cant_mat = st.number_input("Cantidad de repuesto", min_value=0, value=1 if material != "Ninguno" else 0)
            
            st.subheader("2. Evidencias (Foto y Markup)")
            # Usamos file_uploader porque en iPad permite abrir la cámara Y usar el lápiz para dibujar antes de subir
            evidencia = st.file_uploader("Adjuntar foto (En iPad permite marcar averías)", type=['jpg', 'png', 'jpeg'])
            obs = st.text_area("Observaciones técnicas")
            
            if st.form_submit_button("Firmar y Enviar Parte", type="primary", use_container_width=True):
                cod_mat = material.split(" - ")[0] if material != "Ninguno" else "N/A"
                conn.execute('''INSERT INTO partes (equipo, operario, horas, material, cant_mat, obs, estado, fecha) 
                                VALUES (?, ?, ?, ?, ?, ?, 'Pendiente', ?)''', 
                             (equipo, st.session_state['user'], horas, cod_mat, cant_mat, obs, datetime.now().strftime("%Y-%m-%d %H:%M")))
                conn.commit()
                st.success("✅ OT enviada al servidor central.")

    # ==========================================
    # ROL 2: MANAGER (FINANZAS, STOCK Y REPORTES)
    # ==========================================
    elif st.session_state['rol'] == "Manager":
        st.title("📊 Centro de Control Directivo")
        
        tab1, tab2, tab3 = st.tabs(["📋 Bandeja QA & Facturación", "📦 Gestión de Stock", "📈 Reportes Financieros"])
        conn = conectar()
        
        # TAB 1: BANDEJA DE APROBACIÓN
        with tab1:
            st.subheader("Órdenes Pendientes de Revisión")
            df_pendientes = pd.read_sql_query("SELECT * FROM partes WHERE estado='Pendiente'", conn)
            
            if df_pendientes.empty:
                st.success("👍 No hay OTs pendientes.")
            else:
                for index, row in df_pendientes.iterrows():
                    with st.expander(f"🔴 OT-{row['id_ot']} | {row['equipo']} | Técnico: {row['operario']}", expanded=True):
                        st.write(f"**Tiempo:** {row['horas']}h | **Material:** {row['cant_mat']}x {row['material']}")
                        st.write(f"**Notas:** {row['obs']}")
                        
                        if st.button(f"Validar y Facturar OT-{row['id_ot']}", type="primary", key=f"btn_{row['id_ot']}"):
                            # 1. Calcular Costes e Ingresos
                            coste_h = row['horas'] * COSTE_HORA
                            venta_h = row['horas'] * VENTA_HORA
                            coste_m, venta_m = 0, 0
                            
                            if row['material'] != "N/A":
                                cursor = conn.execute("SELECT coste, venta FROM inventario WHERE codigo=?", (row['material'],))
                                res = cursor.fetchone()
                                if res:
                                    coste_m = row['cant_mat'] * res[0]
                                    venta_m = row['cant_mat'] * res[1]
                                    # Restar stock
                                    conn.execute("UPDATE inventario SET stock = stock - ? WHERE codigo=?", (row['cant_mat'], row['material']))
                            
                            coste_total = coste_h + coste_m
                            venta_total = venta_h + venta_m
                            margen = venta_total - coste_total
                            
                            # 2. Actualizar OT
                            conn.execute('''UPDATE partes SET estado='Facturada', coste=?, venta=?, margen=? 
                                            WHERE id_ot=?''', (coste_total, venta_total, margen, row['id_ot']))
                            conn.commit()
                            st.rerun()

        # TAB 2: INVENTARIO
        with tab2:
            st.subheader("Estado del Almacén en Tiempo Real")
            df_stock = pd.read_sql_query("SELECT codigo, descripcion, stock, coste as coste_unitario FROM inventario", conn)
            
            # Alertas visuales de stock
            df_stock['Estado'] = df_stock['stock'].apply(lambda x: "🔴 Rotura" if x < 10 else "🟢 Óptimo")
            st.dataframe(df_stock, use_container_width=True, hide_index=True)

        # TAB 3: REPORTES Y FINANZAS
        with tab3:
            st.subheader("Rendimiento Económico")
            df_fact = pd.read_sql_query("SELECT id_ot, equipo, fecha, coste, venta, margen FROM partes WHERE estado='Facturada'", conn)
            
            if not df_fact.empty:
                c1, c2, c3 = st.columns(3)
                c1.metric("Costes Totales (OpEx)", f"{df_fact['coste'].sum():.2f} €")
                c2.metric("Facturación Total", f"{df_fact['venta'].sum():.2f} €")
                c3.metric("Margen Neto (Beneficio)", f"{df_fact['margen'].sum():.2f} €")
                
                st.write("---")
                st.dataframe(df_fact, use_container_width=True, hide_index=True)
                
                # Generador de CSV (Descarga)
                csv = df_fact.to_csv(index=False).encode('utf-8')
                st.download_button(label="📥 Descargar Reporte Financiero (CSV)", data=csv, file_name="reporte_financiero_gmao.csv", mime="text/csv")
            else:
                st.info("No hay OTs facturadas todavía para generar reportes.")
