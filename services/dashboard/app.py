import streamlit as st
import httpx
import os
import folium
from streamlit_folium import st_folium
import re
import flexpolyline
import uuid

# --- AYARLAR ---
st.set_page_config(page_title="GeoIntel Operasyon Merkezi", layout="wide", page_icon="🌍", initial_sidebar_state="collapsed")
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8000")

# --- STİL ---
st.markdown("""
<style>
    .stApp { background-color: #0e1117; }
    .stChatMessage { background-color: #262730; border-radius: 10px; padding: 10px; border: 1px solid #41444e; }
    h1 { color: #4CAF50 !important; }
</style>
""", unsafe_allow_html=True)

# --- SESSION ---
if "messages" not in st.session_state: st.session_state.messages = []
if "session_id" not in st.session_state: st.session_state.session_id = f"ops_{uuid.uuid4().hex[:8]}"
if "map_center" not in st.session_state: st.session_state.map_center = [41.0082, 28.9784]
if "visual_data" not in st.session_state: st.session_state.visual_data = {"markers": [], "polyline": None}

# --- YARDIMCI FONKSİYONLAR ---
def extract_coordinates_fallback(text):
    """Eğer visual_data boş gelirse metinden koordinat kazır (Regex Güvenlik Ağı)."""
    pattern = r"\(?(\d{1,2}\.\d+),\s*(\d{1,3}\.\d+)\)?"
    matches = re.findall(pattern, text)
    if matches: return [float(matches[-1][0]), float(matches[-1][1])]
    return None

def send_message(prompt):
    try:
        payload = {"session_id": st.session_state.session_id, "message": prompt}
        response = httpx.post(f"{ORCHESTRATOR_URL}/chat", json=payload, timeout=120.0)
        return response.json() if response.status_code == 200 else {"response": "❌ Sunucu Hatası"}
    except Exception as e:
        return {"response": f"🔥 Bağlantı Hatası: {str(e)}"}

# --- ARAYÜZ ---
col1, col2 = st.columns([1, 1], gap="medium")

with col1:
    st.title("🌍 GeoIntel Operasyon Merkezi")
    st.divider()
    
    container = st.container(height=650, border=False)
    with container:
        if not st.session_state.messages: st.info("Sistem Hazır. Görev bekliyorum...")
        for message in st.session_state.messages:
            with st.chat_message(message["role"]): st.markdown(message["content"])

    if prompt := st.chat_input("Talimat girin..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with container:
            with st.chat_message("user"): st.markdown(prompt)
            with st.chat_message("assistant"):
                with st.spinner("İstihbarat toplanıyor..."):
                    api_result = send_message(prompt)
                    response_text = api_result.get("response", "")
                    st.markdown(response_text)
                    st.session_state.messages.append({"role": "assistant", "content": response_text})
                    
                    # Veri Güncelleme
                    if "visual_data" in api_result:
                        st.session_state.visual_data = api_result["visual_data"]
                        # Haritayı son markera odakla
                        if st.session_state.visual_data["markers"]:
                            m = st.session_state.visual_data["markers"][-1]
                            st.session_state.map_center = [m["lat"], m["lon"]]
                        else:
                            # Fallback: Metinden kazı
                            coords = extract_coordinates_fallback(response_text)
                            if coords: st.session_state.map_center = coords
                    st.rerun()

with col2:
    st.subheader("🗺️ Taktik Harita")
    m = folium.Map(location=st.session_state.map_center, zoom_start=13, tiles="CartoDB dark_matter")
    
    v_data = st.session_state.visual_data
    
    # 1. Rota Çizimi
    if v_data.get("polyline"):
        try:
            points = flexpolyline.decode(v_data["polyline"])
            folium.PolyLine(points, color="#00f2ff", weight=5, opacity=0.8, tooltip="Ana Güzergah").add_to(m)
            m.fit_bounds(points)
        except: pass

    # 2. Mekan İşaretçileri (Hepsini göster!)
    for marker in v_data.get("markers", []):
        color = "green" if marker.get("source") == "osm" else "blue" if marker.get("source") == "google" else "red"
        folium.Marker(
            [marker["lat"], marker["lon"]],
            popup=marker["name"],
            tooltip=marker["name"],
            icon=folium.Icon(color=color, icon="info-sign")
        ).add_to(m)
    
    # Eğer marker yoksa default merkeze bir marker koy
    if not v_data.get("markers"):
        folium.Marker(st.session_state.map_center, icon=folium.Icon(color="red")).add_to(m)

    # app.py içindeki st_folium satırını şöyle güncellemek daha sağlıklıdır:
    st_folium(m, width="100%", height=750, key=f"ops_map_{len(st.session_state.messages)}")