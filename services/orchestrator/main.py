import os
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List

# LangChain & LangGraph
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

# --- AYARLAR ---
app = FastAPI(title="GeoIntel Orchestrator", version="1.0")
CITY_AGENT_URL = "http://geo_mcp_city:8000" # Docker içindeki adres

# LLM (Beyin)
llm = ChatAnthropic(
    model="claude-sonnet-4-5-20250929",
    temperature=0,
    api_key=os.getenv("ANTHROPIC_API_KEY")
)
# SYSTEM_PROMPT kısmını bul ve bunla değiştir:

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
- call_city_search: Yer ismi ver, sana Google detaylarını (koordinat dahil) versin.
- call_city_weather: Koordinat ver, hava durumu versin.
- call_city_route: Başlangıç ve bitiş ver (isim veya koordinat), rota çizsin.

ASLA "Yapamıyorum" deme. Hata alırsan strateji değiştir ve tekrar dene.
Örnek: Kullanıcı "Rize'den Trabzon'a git" dedi ve rota aracı "Bulunamadı" dedi.
DOĞRU HAMLE: Önce Rize'yi search et -> Koordinatı al. Sonra Trabzon'u search et -> Koordinatı al. Sonra bu iki koordinatla tekrar Rota çiz.

Hadi başla."""
# --- İSTEMCİ (Client): Şehir Ajanı ile Konuşan Fonksiyonlar ---
# Orchestrator, işi kendisi yapmaz. İşçiye (MCP City) havale eder.
@tool
async def call_city_weather(lat: float, lon: float):
    """
    Verilen koordinatın (lat, lon) hava durumunu öğrenmek için BU ARACI KULLAN.
    """
    print(f"🧠 [ORCHESTRATOR] Tool Tetiklendi: Lat={lat}, Lon={lon}", flush=True)
    print(f"📞 [ORCHESTRATOR] City Agent aranıyor: {CITY_AGENT_URL}/get_weather", flush=True)
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{CITY_AGENT_URL}/get_weather", 
                json={"lat": lat, "lon": lon},
                timeout=10.0 # Zaman aşımı ekleyelim
            )
            print(f"✅ [ORCHESTRATOR] City Agent Cevabı ({response.status_code}): {response.text}", flush=True)
            return response.text
        except Exception as e:
            print(f"❌ [ORCHESTRATOR] Bağlantı Hatası: {e}", flush=True)
            return f"HATA: Şehir Ajanına ulaşılamadı: {e}"
@tool
async def call_city_search(query: str):
    """Mekan aramak (otel, park, vs) için kullanılır."""
    async with httpx.AsyncClient() as client:
        # FastMCP endpoint mantığı: POST /tool_name
        res = await client.post(f"{CITY_AGENT_URL}/search_places_google", json={"query": query})
        return res.json() if res.status_code == 200 else res.text

@tool
async def call_city_route(origin: str, destination: str):
    """
    İki nokta arasına HERE MAPS ile rota çizer.
    ÇOK ÖNEMLİ: 'origin' ve 'destination' parametreleri MUTLAKA 'Lat,Lon' formatında olmalıdır.
    ASLA ŞEHİR İSMİ GÖNDERME.
    Önce 'call_city_search' ile koordinat bul, sonra o koordinatları buraya virgülle yapıştır.
    Örnek: "41.0201,40.5234"
    """
    async with httpx.AsyncClient() as client:
        res = await client.post(f"{CITY_AGENT_URL}/get_route_data", json={"origin": origin, "destination": destination})
        return res.json() if res.status_code == 200 else res.text

# --- TOOL LISTESİ ---
tools = [call_city_weather, call_city_search, call_city_route]

# LLM'e bu aletleri tanıtalım
model_with_tools = llm.bind_tools(tools)

# --- LANGGRAPH AKIŞI ---
from typing import TypedDict, Annotated
import operator

class AgentState(TypedDict):
    messages: Annotated[List[HumanMessage | AIMessage], operator.add]

# 1. Düğüm: Ajan (Karar Verici)
def agent_node(state: AgentState):
    messages = state["messages"]
    response = model_with_tools.invoke(messages)
    return {"messages": [response]}

# 2. Düğüm: Alet Kullanıcısı (Tool Executor)
tool_node = ToolNode(tools)

# 3. Grafik Oluştur
workflow = StateGraph(AgentState)

workflow.add_node("agent", agent_node)
workflow.add_node("tools", tool_node)

workflow.set_entry_point("agent")

# Koşullu Kenar: Ajan bir tool çağırdı mı?
def should_continue(state: AgentState):
    last_message = state["messages"][-1]
    if last_message.tool_calls:
        return "tools" # Evet, alete git
    return END # Hayır, cevap bitti

workflow.add_conditional_edges("agent", should_continue)
workflow.add_edge("tools", "agent") # Alet bitince tekrar ajana dön (yorumlama yapması için)

# Uygulamayı Derle
app_graph = workflow.compile()

# --- API ENDPOINT ---
class ChatRequest(BaseModel):
    message: str
    history: Optional[List[str]] = []

@app.post("/chat")
async def chat_endpoint(request: ChatRequest):
    """Mobil uygulamadan gelen mesajı işler."""
    
    # LangGraph'ı çalıştır
    inputs = {"messages": [HumanMessage(content=request.message)]}
    
    final_state = await app_graph.ainvoke(inputs)
    
    # Son mesajı al (AI Cevabı)
    last_msg = final_state["messages"][-1].content
    
    return {"response": last_msg}

@app.get("/health")
def health_check():
    return {"status": "Orchestrator is running", "brain": "Active"}