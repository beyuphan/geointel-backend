import operator
import httpx
import json
import asyncio
import redis
import os
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Literal, List, Dict, Any, Union, Annotated, TypedDict
from pydantic import BaseModel, create_model, Field

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.pydantic_v1 import BaseModel, Field
# --- MODÜLER İMPORTLAR (JİLET GİBİ YAPIDAN DEVAM) ---
from profile_manager import ProfileManager           # Hafıza Yöneticisi
from tools import MANUAL_TOOLS                       # Araç Tanımları (tools.py'den)
from prompt_manager import get_dynamic_system_prompt # Zeka/Prompt Yöneticisi

# --- LANGCHAIN & ANTHROPIC (GERİ GELDİ) ---
from langchain_anthropic import ChatAnthropic        # <--- İŞTE BU!
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

from config import settings  # Senin config dosyan
from logger import log

# --- GLOBAL DURUM ---
RUNTIME_TOOLS = []
MCP_SESSIONS: Dict[str, str] = {}
PENDING_REQUESTS: Dict[str, asyncio.Future] = {}


# --- CIRCUIT BREAKER AYARLARI ---
RPC_TIMEOUT = 25.0  # 25 saniye içinde cevap gelmezse kes
CIRCUIT_STATES = {}

# Sınıflandırma şeması
class IntentAnalysis(BaseModel):
    category: Literal["fuel", "pharmacy", "event", "routing", "general"] = Field(
        description="Kullanıcının isteğinin ana kategorisi"
    )
    urgency: bool = Field(description="İşlem acil mi? (Örn: Nöbetçi eczane)")
    focus_points: List[str] = Field(description="Mesajdaki anahtar kelimeler (örn: 'ucuz', 'dizel')")

# 🔄 Agent State Güncellemesi
class AgentState(TypedDict):
    messages: Annotated[List[Any], operator.add]
    intent: Dict[str, Any]  # Classifier'dan gelen niyet
    retry_count: int        # Hata döngüsü kontrolü için

async def intent_node(state: AgentState):
    # Bu düğümde Gemini 1.5 Flash kullanmanı öneririm (Hız ve maliyet için)
    # Şimdilik ana llm üzerinden gidiyoruz:
    msg = state["messages"][-1].content
    
    # Modelin yapılandırılmış çıktı (Structured Output) vermesini sağlıyoruz
    model_with_structure = llm.with_structured_output(IntentAnalysis)
    
    intent_result = await model_with_structure.ainvoke(
        f"Aşağıdaki kullanıcı mesajının niyetini analiz et: {msg}"
    )
    
    return {"intent": intent_result.dict()}

# --- 1. CLASSIFIER NODE (Niyet Belirleyici) ---
async def classifier_node(state: AgentState):
    msg = state["messages"][-1].content
    
    # Gemini 1.5 Flash veya Claude Haiku kullanarak hızlıca niyet analizi yap
    # Structured output özelliği sayesinde model direkt Pydantic döner
    model_with_structure = llm.with_structured_output(IntentAnalysis)
    
    try:
        intent_result = await model_with_structure.ainvoke(
            f"Kullanıcı mesajını analiz et ve GeoIntel asistanı için niyetini belirle: {msg}"
        )
        return {"intent": intent_result.dict(), "retry_count": 0}
    except Exception as e:
        log.error(f"❌ Niyet analizi hatası: {e}")
        return {"intent": {"category": "general", "focus_points": [], "urgency": False}}

# --- 2. VALIDATOR LOGIC (Döngü Kararı) ---
def should_continue(state: AgentState):
    last_message = state["messages"][-1]
    
    # Eğer model tool çağrısı yaptıysa tools düğümüne git
    if last_message.tool_calls:
        return "tools"
    
    # HATA YÖNETİMİ: Eğer cevapta 'bulunamadı' gibi bir ibare varsa ve 
    # henüz çok fazla deneme yapmadıysak ajanı tekrar çalıştır (Retry Loop)
    if "üzgünüm" in last_message.content.lower() or "bulunamadı" in last_message.content.lower():
        if state.get("retry_count", 0) < 2:
            log.warning("🔄 [Retry] Ajan tatmin edici sonuç bulamadı, tekrar deniyor...")
            return "agent" 

    return END

# --- REDIS KURULUMU ---
try:
    # Config'den veya direkt string olarak alabilirsin
    redis_client = redis.Redis(host="geo_redis", port=6379, db=0, decode_responses=True)
    redis_client.ping()
    log.success("✅ [Orchestrator] Redis Hafızası Aktif")
except Exception as e:
    log.error(f"❌ [Orchestrator] Redis Hatası: {e}")
    redis_client = None

# --- TOOL ROUTER (YÖNLENDİRİCİ) ---
TOOL_ROUTER = {
    # CITY
    "search_infrastructure_osm": "city",
    "search_places_google": "city",
    "get_route_data": "city",
    "get_weather": "city",
    "analyze_route_weather": "city",
    "save_location": "city",
    "get_toll_prices": "city",
    # INTEL
    "get_pharmacies": "intel",
    "get_fuel_prices": "intel",
    "get_city_events": "intel",
    "get_sports_events": "intel",
    # LOCAL
    "remember_info": "orchestrator",  
}

# --- RPC ÇAĞRISI (SAĞLAM BAĞLANTI MANTIĞI) ---
async def mcp_rpc_call(service_name: str, method: str, params: dict = None) -> Union[dict, str]:
    """
    Güçlendirilmiş RPC Çağrısı (Circuit Breaker & Fallback Dahil)
    """
    # 1. Session Kontrolü
    session_url = MCP_SESSIONS.get(service_name)
    if not session_url:
        log.warning(f"⚠️ [CIRCUIT] {service_name} oturumu yok, tekrar deneniyor...")
        # Basit bir retry mekanizması (1 saniye bekle)
        await asyncio.sleep(1)
        session_url = MCP_SESSIONS.get(service_name)
        if not session_url:
            return {
                "status": "error", 
                "error": f"{service_name.upper()} ajanı çevrimdışı.", 
                "data": []
            }

    req_id = str(int(datetime.now().timestamp() * 1000))
    payload = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": int(req_id)}
    
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    PENDING_REQUESTS[req_id] = future
    
    try:
        log.info(f"⚡ [RPC -> {service_name.upper()}] Metod: {method}")
        
        async with httpx.AsyncClient(timeout=RPC_TIMEOUT + 5.0) as client:
            # İsteği gönder
            resp = await client.post(session_url, json=payload)
            
            if resp.status_code not in [200, 202]:
                raise Exception(f"HTTP {resp.status_code} - {resp.text}")

            # Cevabı bekle (Zaman aşımı kontrolü burada)
            response_data = await asyncio.wait_for(future, timeout=RPC_TIMEOUT)
            
            # --- BAŞARILI YANIT İŞLEME ---
            if "error" in response_data:
                err_msg = response_data["error"]
                log.error(f"❌ [RPC ERROR] {service_name}: {err_msg}")
                return {"status": "error", "error": str(err_msg)}
            
            # MCP sonucunu temizle ve döndür
            result = response_data.get("result")
            
            # FastMCP bazen content listesi döner, bazen direkt dict. 
            # Bunu standartlaştıralım:
            if isinstance(result, dict) and "content" in result:
                # Text içeriğini ayıkla
                content = result["content"]
                if isinstance(content, list) and len(content) > 0:
                    text_data = content[0].get("text")
                    try:
                        # Eğer içindeki text JSON ise parse et
                        return json.loads(text_data)
                    except:
                        return text_data # JSON değilse düz metin dön
            
            return result

    except asyncio.TimeoutError:
        log.error(f"⏱️ [TIMEOUT] {service_name} yanıt vermedi ({RPC_TIMEOUT}s). Devre kesildi.")
        # FALLBACK: Eğer Redis varsa eski veriyi ara (İleride burası gelişecek)
        return {
            "status": "partial_error",
            "error": "Servis zaman aşımına uğradı.",
            "message": "Güncel veriye ulaşılamadı, lütfen daha sonra tekrar deneyin."
        }

    except Exception as e:
        log.error(f"🔥 [CRITICAL] RPC Patladı: {e}")
        return {"status": "error", "error": str(e)}
        
    finally:
        if req_id in PENDING_REQUESTS: del PENDING_REQUESTS[req_id]

# --- SSE LISTENER (OTOMATİK BAĞLANMA) ---
async def sse_listener_loop(service_name: str, base_url: str):
    log.info(f"🎧 [{service_name.upper()}] SSE Dinleniyor: {base_url}")
    async with httpx.AsyncClient(timeout=None) as client:
        try:
            async with client.stream("GET", base_url) as response:
                async for line in response.aiter_lines():
                    if not line: continue
                    if line.startswith("event: endpoint"): continue
                    
                    if line.startswith("data: "):
                        data_str = line.replace("data: ", "").strip()
                        
                        # 1. Session URL Yakalama
                        if data_str.startswith("/") or "http" in data_str:
                            root = base_url.replace("/sse", "")
                            final_url = f"{root}{data_str}" if data_str.startswith("/") else data_str
                            MCP_SESSIONS[service_name] = final_url
                            log.success(f"✅ [{service_name.upper()}] Kanal Açık: {final_url}")
                            
                            # Init Gönder
                            asyncio.create_task(mcp_rpc_call(service_name, "initialize", {
                                "protocolVersion": "2024-11-05", 
                                "capabilities": {}, 
                                "clientInfo": {"name": "Orchestrator", "version": "1.0"}
                            }))
                            continue

                        # 2. RPC Cevabı Yakalama
                        if data_str.startswith("{"):
                            try:
                                msg = json.loads(data_str)
                                if "id" in msg:
                                    req_id = str(msg["id"])
                                    if req_id in PENDING_REQUESTS:
                                        future = PENDING_REQUESTS[req_id]
                                        if not future.done(): future.set_result(msg)
                            except: pass
        except Exception as e:
            log.error(f"🔥 [{service_name.upper()}] SSE Koptu: {e}")
            await asyncio.sleep(3)
            asyncio.create_task(sse_listener_loop(service_name, base_url))

# --- TOOL WRAPPER ---
async def create_dynamic_tool(tool_def: dict):
    name = tool_def["name"]
    desc = tool_def.get("description", "")
    schema = tool_def.get("inputSchema", {"properties": {}})
    fields = {k: (Any, ...) for k in schema.get("properties", {}).keys()}
    DynamicSchema = create_model(f"{name}_Schema", **fields)

    async def execution_wrapper(**kwargs):
        target_service = TOOL_ROUTER.get(name)
        
        # 1. ENJEKSİYON: Ajan 'analyze_route_weather' çağırdığında hafızayı kontrol et
        if name == "analyze_route_weather" and redis_client:
            # Eğer polyline hiç gelmediyse veya 'LATEST' olarak geldiyse
            if not kwargs.get("polyline") or kwargs.get("polyline") == "LATEST":
                latest_route = redis_client.get("latest_route")
                if latest_route:
                    kwargs["polyline"] = latest_route
                    log.info("🧠 [Memory] Son rota hafızadan çekildi ve enjekte edildi.")
                else:
                    return "Hata: Henüz bir rota oluşturulmamış. Lütfen önce bir rota hesaplatın."

        # Yerel (Orchestrator) Araçları
        if target_service == "orchestrator":
            if name == "remember_info":
                return await ProfileManager.update_memory(kwargs.get("category"), kwargs.get("value"))
            return "Bilinmeyen yerel araç."
        
        # Uzak (City/Intel) Araçları
        if not target_service:
            return f"Hata: '{name}' aracı yönlendirilmemiş."

        log.info(f"🚀 [MCP -> {target_service.upper()}] {name} Args: {kwargs}")
        
        # 2. RPC ÇAĞRISINI YAP
        result = await mcp_rpc_call(target_service, "tools/call", {"name": name, "arguments": kwargs})

        # 3. KAYIT: Eğer bir rota oluşturulduysa (get_route_data), polyline'ı Redis'e kaydet
        if name == "get_route_data" and redis_client:
            # result bazen parse edilmiş bir dict, bazen düz string olabilir.
            # get_route_data_handler çıktısına göre 'polyline_encoded' veya 'polyline' aranmalı.
            if isinstance(result, dict) and result.get("polyline"):
                redis_client.set("latest_route", result["polyline"])
                log.info("💾 [Memory] Yeni rota polyline verisi Redis'e kaydedildi.")
            elif isinstance(result, dict) and result.get("polyline_encoded"): # Handler ismine göre alternatif
                redis_client.set("latest_route", result["polyline_encoded"])
                log.info("💾 [Memory] Yeni rota polyline verisi Redis'e kaydedildi.")

        return result

    return StructuredTool.from_function(
        func=None, coroutine=execution_wrapper, name=name, description=desc, args_schema=DynamicSchema
    )

# --- LIFESPAN ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Dinleyicileri başlat
    asyncio.create_task(sse_listener_loop("city", f"{settings.MCP_CITY_URL}/sse"))
    asyncio.create_task(sse_listener_loop("intel", f"{settings.MCP_INTEL_URL}/sse"))
    
    await asyncio.sleep(2) 
    
    log.info("🛠️ Araçlar Yükleniyor...")
    # MANUAL_TOOLS artık tools.py'den geliyor!
    for t_def in MANUAL_TOOLS:
        tool_obj = await create_dynamic_tool(t_def)
        RUNTIME_TOOLS.append(tool_obj)
    
    log.success(f"✅ {len(RUNTIME_TOOLS)} Araç Hazır.")
    yield
    RUNTIME_TOOLS.clear()

app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- LLM AYARLARI (ANTHROPIC) ---
# Burada senin config dosyanı kullanıyoruz
llm = ChatAnthropic(
    model="claude-sonnet-4-5-20250929", 
    temperature=0,
    api_key=settings.ANTHROPIC_API_KEY
)

class ChatRequest(BaseModel):
    session_id: str = "default_session"
    message: str

# --- CHAT ENDPOINT (MODÜLER) ---
@app.post("/chat")
async def chat_endpoint(request: ChatRequest):
    if not RUNTIME_TOOLS: return {"error": "Araçlar yüklenmedi."}
    
    # 1. Profil Yöneticisinden Veriyi Çek
    user_context_str = await ProfileManager.get_user_context("test_pilot")
    
    # 2. Prompt Yöneticisinden Dinamik Promptu Al
    dynamic_prompt = get_dynamic_system_prompt(user_context_str, request.message)
    
    # 3. Model Bağlama
    model_with_tools = llm.bind_tools(RUNTIME_TOOLS)
    tool_node = ToolNode(RUNTIME_TOOLS)
    
    # 4. Geçmiş Yükle
    history = []
    if redis_client:
        try:
            stored = redis_client.lrange(f"chat:{request.session_id}", 0, -1)
            for item in stored:
                msg = json.loads(item)
                if msg["role"] == "user": history.append(HumanMessage(content=msg["content"]))
                elif msg["role"] == "assistant": history.append(AIMessage(content=msg["content"]))
        except: pass

    # 5. Graph
    class AgentState(TypedDict):
        messages: Annotated[List[Any], operator.add]
        intent: Dict[str, Any]  
    def agent_node(state: AgentState):
        dynamic_prompt = get_dynamic_system_prompt(user_context_str, state["intent"])
        retry_note = ""
        if state.get("retry_count", 0) > 0:
            retry_note = "\n\nNOT: Önceki denemede sonuç bulunamadı. Lütfen arama parametrelerini genişlet."

        msgs = [SystemMessage(content=dynamic_prompt + retry_note)] + history + state["messages"]
        
        # retry_count'u artırarak state'i güncelle
        return {
            "messages": [model_with_tools.invoke(msgs)],
            "retry_count": state.get("retry_count", 0) + 1
        }

    def should_continue(state: AgentState):
        return "tools" if state["messages"][-1].tool_calls else END

    workflow = StateGraph(AgentState)
    workflow.add_node("classifier", intent_node) # 1. Adım: Sınıflandır
    workflow.add_node("agent", agent_node)       # 2. Adım: Cevap üret
    workflow.add_node("tools", tool_node)        # 3. Adım: Gerekirse araç kullan

    workflow.set_entry_point("classifier")       # Giriş artık classifier!
    workflow.add_edge("classifier", "agent")     # Sınıflandırmadan ajana geç
    workflow.add_conditional_edges("agent", should_continue, {
    "tools": "tools",
    "agent": "agent", # Retry döngüsü
    END: END
    })
    workflow.add_edge("tools", "agent")
    
    # Derle ve Çalıştır
    app_graph = workflow.compile()
    
    # İlk mesajı gönderirken retry_count ve intent'i başlatıyoruz
    initial_input = {
        "messages": [HumanMessage(content=request.message)],
        "intent": {}, 
        "retry_count": 0
    }
    
    final_state = await app_graph.ainvoke(initial_input)
    final_response = final_state["messages"][-1].content

    # 7. Kaydet ve Bitir
    route_polyline = None
    if redis_client:
        try:
            route_polyline = redis_client.get("latest_route")
            redis_client.rpush(f"chat:{request.session_id}", json.dumps({"role": "user", "content": request.message}))
            redis_client.rpush(f"chat:{request.session_id}", json.dumps({"role": "assistant", "content": final_response}))
            redis_client.expire(f"chat:{request.session_id}", 86400)
            redis_client.ltrim(f"chat:{request.session_id}", -20, -1)
        except: pass

    return {
        "response": final_response, 
        "route_polyline": route_polyline 
    }