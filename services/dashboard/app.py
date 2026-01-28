import streamlit as st
import httpx
import os
import folium
from streamlit_folium import st_folium
import re

# --- AYARLAR ---
st.set_page_config(
    page_title="GeoIntel Operasyon Merkezi", 
    layout="wide", 
    page_icon="🌍",
    initial_sidebar_state="collapsed"
)

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8000")

# --- STİL ---
st.markdown("""
<style>
    .stApp { background-color: #0e1117; }
    .stChatMessage { background-color: #262730; border-radius: 10px; padding: 10px; border: 1px solid #41444e; }
    .stChatInput { position: fixed; bottom: 20px; }
    h1 { color: #4CAF50 !important; }
</style>
""", unsafe_allow_html=True)

# --- SESSION ---
if "messages" not in st.session_state: st.session_state.messages = []
if "session_id" not in st.session_state:
    import uuid
    st.session_state.session_id = f"user_{uuid.uuid4().hex[:8]}"
if "last_coords" not in st.session_state:
    st.session_state.last_coords = [41.0082, 28.9784] # Default: İstanbul

# --- KOORDİNAT YAKALAYICI (Geliştirildi) ---
def extract_coordinates(text):
    # Hem "41.0, 29.0" hem de "(41.0, 29.0)" formatını yakalar
    pattern = r"\(?(\d{1,2}\.\d+),\s*(\d{1,3}\.\d+)\)?"
    matches = re.findall(pattern, text)
    if matches:
        # Son bulunan koordinatı al (Genelde sonuç en sondadır)
        return [float(matches[-1][0]), float(matches[-1][1])]
    return None

def send_message(prompt):
    try:
        payload = {"session_id": st.session_state.session_id, "message": prompt}
        response = httpx.post(f"{ORCHESTRATOR_URL}/chat", json=payload, timeout=90.0)
        if response.status_code == 200:
            return response.json().get("response", "⚠️ Cevap yok.")
        return f"❌ Hata ({response.status_code})"
    except Exception as e:
        return f"🔥 Bağlantı Hatası: {str(e)}"

# --- ARAYÜZ ---
col1, col2 = st.columns([1, 1], gap="medium")

with col1:
    st.title("🌍 GeoIntel Operasyon Merkezi")
    st.divider()
    
    container = st.container(height=600, border=False)
    with container:
        if not st.session_state.messages:
            st.info("Sistem Hazır. Görev bekliyorum...")
        
        for message in st.session_state.messages:
            with st.chat_message(message["role"], avatar="🧑‍💻" if message["role"] == "user" else "🤖"):
                st.markdown(message["content"])

    if prompt := st.chat_input("Talimat girin..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with container:
            with st.chat_message("user", avatar="🧑‍💻"): st.markdown(prompt)
            with st.chat_message("assistant", avatar="🤖"):
                with st.spinner("Veriler işleniyor..."):
                    response_text = send_message(prompt)
                    st.markdown(response_text)
        
        st.session_state.messages.append({"role": "assistant", "content": response_text})
        
        # Koordinat varsa güncelle
        coords = extract_coordinates(response_text)
        if coords:
            st.session_state.last_coords = coords
            st.toast(f"📍 Rota Güncellendi: {coords}", icon="🚀")

with col2:
    st.subheader("🗺️ Uydu Haritası")
    # TEMA DEĞİŞTİ: OpenStreetMap (Renkli ve Aydınlık)
    m = folium.Map(location=st.session_state.last_coords, zoom_start=13, tiles="OpenStreetMap")
    
    folium.Marker(
        st.session_state.last_coords,
        popup="Hedef",
        icon=folium.Icon(color="red", icon="info-sign")
    ).add_to(m)
    
    st_folium(m, width="100%", height=700)