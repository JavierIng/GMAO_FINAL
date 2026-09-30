import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime

st.set_page_config(page_title="SIGMA-IA", layout="wide")

def conectar():
    return sqlite3.connect("gmao_nube.db", check_same_thread=False)

def inicializar_bd():
    conn = conectar()
    conn.execute('''CREATE TABLE IF NOT EXISTS partes (
                    id_ot INTEGER PRIMARY KEY AUTOINCREMENT,
                    equipo TEXT, operario TEXT, horas REAL,
                    estado TEXT, fecha DATE)''')
    conn.commit()
    conn.close()

inicializar_bd()

st.title("📱 GMAO Meciberia - App de Prueba")

with st.form("form_prueba"):
    equipo = st.selectbox("Equipo", ["Bomba-01", "Válvula-02"])
    horas = st.number_input("Horas", value=1.0)
    if st.form_submit_button("Enviar Parte", type="primary"):
        conn = conectar()
        conn.execute("INSERT INTO partes (equipo, operario, horas, estado, fecha) VALUES (?, 'Javier', ?, 'Pendiente', ?)", 
                     (equipo, horas, datetime.now()))
        conn.commit()
        st.success("✅ ¡Funciona! Parte guardado en la base de datos.")

st.divider()
st.subheader("Datos guardados:")
conn = conectar()
df = pd.read_sql_query("SELECT * FROM partes", conn)
st.dataframe(df, hide_index=True)
