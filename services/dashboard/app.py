import streamlit as st
import httpx
import os
import folium
from streamlit_folium import st_folium
import re
import flexpolyline  # Rota kodunu çözmek için lazım

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
if "current_route" not in st.session_state:
    st.session_state.current_route = None # Rotayı tutmak için

# --- YARDIMCI FONKSİYONLAR ---
def extract_coordinates(text):
    pattern = r"\(?(\d{1,2}\.\d+),\s*(\d{1,3}\.\d+)\)?"
    matches = re.findall(pattern, text)
    if matches: return [float(matches[-1][0]), float(matches[-1][1])]
    return None

def send_message(prompt):
    try:
        payload = {"session_id": st.session_state.session_id, "message": prompt}
        response = httpx.post(f"{ORCHESTRATOR_URL}/chat", json=payload, timeout=90.0)
        if response.status_code == 200:
            return response.json() # Tüm JSON'ı dönüyoruz (polyline için)
        return {"response": f"❌ Hata ({response.status_code})"}
    except Exception as e:
        return {"response": f"🔥 Bağlantı Hatası: {str(e)}"}

# --- ARAYÜZ ---
col1, col2 = st.columns([1, 1], gap="medium")

with col1:
    st.title("🌍 GeoIntel Operasyon Merkezi")
    st.divider()
    
    container = st.container(height=600, border=False)
    with container:
        if not st.session_state.messages: st.info("Sistem Hazır. Görev bekliyorum...")
        
        for message in st.session_state.messages:
            with st.chat_message(message["role"], avatar="🧑‍💻" if message["role"] == "user" else "🤖"):
                st.markdown(message["content"])

    if prompt := st.chat_input("Talimat girin..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with container:
            with st.chat_message("user", avatar="🧑‍💻"): st.markdown(prompt)
            with st.chat_message("assistant", avatar="🤖"):
                with st.spinner("Veriler işleniyor..."):
                    api_result = send_message(prompt)
                    response_text = api_result.get("response", "")
                    route_poly = api_result.get("route_polyline")
                    
                    st.markdown(response_text)
        
        st.session_state.messages.append({"role": "assistant", "content": response_text})
        
        # 1. Koordinat Güncelleme
        coords = extract_coordinates(response_text)
        if coords:
            st.session_state.last_coords = coords
        
        # 2. Rota Güncelleme (Varsa)
        if route_poly and route_poly != "LATEST":
            try:
                # Flexpolyline decode ([(lat, lon), ...])
                decoded_route = flexpolyline.decode(route_poly)
                st.session_state.current_route = decoded_route
                st.toast("🛣️ Yeni Rota Çizildi!", icon="🚗")
            except Exception as e:
                print(f"Rota hatası: {e}")

with col2:
    st.subheader("🗺️ Taktik Harita")
    m = folium.Map(location=st.session_state.last_coords, zoom_start=13, tiles="OpenStreetMap")
    
    # Hedef Marker
    folium.Marker(
        st.session_state.last_coords,
        popup="Hedef",
        icon=folium.Icon(color="red", icon="info-sign")
    ).add_to(m)
    
    # Rota Çizgisi (Varsa)
    if st.session_state.current_route:
        folium.PolyLine(
            st.session_state.current_route,
            color="blue",
            weight=5,
            opacity=0.8,
            tooltip="Ana Güzergah"
        ).add_to(m)
        
        # Haritayı rotaya sığdır
        m.fit_bounds(st.session_state.current_route)
    
    st_folium(m, width="100%", height=700)