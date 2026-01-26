# services/orchestrator/main.py
from datetime import datetime
from typing import TypedDict, Annotated, List, Optional
import operator

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

# LangChain & LangGraph
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

# Bizim Modüller
from config import settings
from logger import log

# --- UYGULAMA KURULUMU ---
app = FastAPI(title=settings.APP_NAME, version="1.0")

# --- LLM (BEYİN) AYARLARI ---
llm = ChatAnthropic(
    model="claude-sonnet-4-5-20250929",  # Model ismi güncel kalsın
    temperature=0,
    api_key=settings.ANTHROPIC_API_KEY
)

SYSTEM_PROMPT = """Sen üst düzey bir Coğrafi Zeka Ajanısın (GeoIntel Agent).

GÖREVİN: Karmaşık coğrafi soruları, elindeki araçları (tools) birbirine bağlayarak çözmek.

NASIL DÜŞÜNMELİSİN? (ReAct Mantığı):
1. Kullanıcının isteğini anla.
2. Hangi aracı kullanman gerektiğini planla.
3. Aracı çalıştır.
4. SONUCU KONTROL ET.
   - Eğer sonuç başarılıysa: Cevabı ver.
   - EĞER SONUÇ HATALIYSA (Örn: Rota bulunamadı): PES ETME. Nedenini düşün.
     - "Acaba yer ismini koordinata mı çevirmeliyim?" diye sor.
     - 'call_city_search' aracını kullanarak koordinatları bul.
     - Sonra tekrar rota aracını dene.

MEVCUT ARAÇLARIN:
- call_city_weather: Koordinat ver, hava durumu versin.
- call_city_search: Yer ismi ver, detayları (koordinat dahil) versin.
- call_city_route: Başlangıç ve bitiş ver (MUTLAKA KOORDİNAT OLMALI), rota çizsin.

ASLA "Yapamıyorum" deme. Hata alırsan strateji değiştir ve tekrar dene.
Örnek: "Rize'den Trabzon'a git" -> Önce Rize ve Trabzon'un koordinatlarını bul, sonra rota çiz.
"""

# --- İSTEMCİ (TOOLS) ---

@tool
async def call_city_weather(lat: float, lon: float):
    """
    Verilen koordinatın (lat, lon) hava durumunu öğrenmek için BU ARACI KULLAN.
    """
    log.info(f"🌤️ [TOOL: WEATHER] Koordinat: {lat}, {lon}")
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                f"{settings.MCP_CITY_URL}/get_weather", 
                json={"lat": lat, "lon": lon}
            )
            response.raise_for_status()
            log.success(f"✅ Hava durumu alındı.")
            return response.text
        except Exception as e:
            log.error(f"❌ Hava durumu hatası: {e}")
            return f"HATA: Şehir Ajanına ulaşılamadı: {e}"

@tool
async def call_city_search(query: str):
    """Mekan aramak (otel, park, şehir merkezi vs) ve KOORDİNAT bulmak için kullanılır."""
    log.info(f"🔍 [TOOL: SEARCH] Aranıyor: {query}")
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            res = await client.post(
                f"{settings.MCP_CITY_URL}/search_places_google", 
                json={"query": query}
            )
            return res.json() if res.status_code == 200 else res.text
        except Exception as e:
            log.error(f"❌ Arama hatası: {e}")
            return f"HATA: {e}"

@tool
async def call_city_route(origin: str, destination: str):
    """
    İki nokta arasına HERE MAPS ile rota çizer.
    ÇOK ÖNEMLİ: 'origin' ve 'destination' parametreleri MUTLAKA 'Lat,Lon' formatında olmalıdır (Örn: "41.02,40.52").
    ASLA ŞEHİR İSMİ GÖNDERME. Önce search ile koordinat bul.
    """
    log.info(f"🚗 [TOOL: ROUTE] Rota: {origin} -> {destination}")
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            res = await client.post(
                f"{settings.MCP_CITY_URL}/get_route_data", 
                json={"origin": origin, "destination": destination}
            )
            if res.status_code == 200:
                log.success("✅ Rota çizildi.")
                return res.json()
            else:
                log.warning(f"⚠️ Rota hatası: {res.text}")
                return res.text
        except Exception as e:
            log.error(f"❌ Rota bağlantı hatası: {e}")
            return f"HATA: {e}"

# --- LANGGRAPH KURULUMU ---

tools = [call_city_weather, call_city_search, call_city_route]
model_with_tools = llm.bind_tools(tools)

class AgentState(TypedDict):
    messages: Annotated[List[HumanMessage | AIMessage | SystemMessage], operator.add]

def agent_node(state: AgentState):
    messages = state["messages"]
    
    # Zaman Algısı Enjeksiyonu
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    time_context = f"""
    [ŞU ANKİ ZAMAN: {current_time}]
    Cevaplarını bu saate göre ayarla. (Örn: 21:00 ise akşam olduğunu bil).
    """
    
    # System Prompt Güncelleme
    if isinstance(messages[0], SystemMessage):
        # Eğer zaten varsa, zamanı güncellemek için eskisini alıp ekliyoruz
        # (Basitçe her seferinde temiz system prompt + zaman veriyoruz)
        messages[0] = SystemMessage(content=SYSTEM_PROMPT + "\n" + time_context)
    else:
        messages.insert(0, SystemMessage(content=SYSTEM_PROMPT + "\n" + time_context))
        
    log.info("🧠 LLM Düşünüyor...")
    response = model_with_tools.invoke(messages)
    return {"messages": [response]}

tool_node = ToolNode(tools)

# Grafik Akışı
workflow = StateGraph(AgentState)
workflow.add_node("agent", agent_node)
workflow.add_node("tools", tool_node)

workflow.set_entry_point("agent")

def should_continue(state: AgentState):
    last_message = state["messages"][-1]
    if last_message.tool_calls:
        log.info(f"🛠️ LLM Tool Çağırdı: {len(last_message.tool_calls)} adet")
        return "tools"
    return END

workflow.add_conditional_edges("agent", should_continue)
workflow.add_edge("tools", "agent")

app_graph = workflow.compile()

# --- API ENDPOINT ---
class ChatRequest(BaseModel):
    message: str
    history: Optional[List[str]] = []

@app.post("/chat")
async def chat_endpoint(request: ChatRequest):
    log.info(f"💬 Yeni Mesaj: {request.message}")
    
    try:
        inputs = {"messages": [HumanMessage(content=request.message)]}
        final_state = await app_graph.ainvoke(inputs)
        
        last_msg = final_state["messages"][-1].content
        log.success("✅ Cevap Hazır")
        return {"response": last_msg}
        
    except Exception as e:
        log.critical(f"🔥 Kritik Hata: {str(e)}")
        return {"error": "Sistemde beklenmedik bir hata oluştu."}

@app.get("/health")
def health_check():
    return {"status": "active", "service": "Orchestrator"}