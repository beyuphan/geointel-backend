import operator
import httpx
import json
import asyncio
import redis
import os
from datetime import datetime
from typing import TypedDict, Annotated, List, Any, Dict
from contextlib import asynccontextmanager
from typing import Union

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, create_model

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
        
        # Yerel (Orchestrator) Araçları
        if target_service == "orchestrator":
            if name == "remember_info":
                return await ProfileManager.update_memory(kwargs.get("category"), kwargs.get("value"))
            return "Bilinmeyen yerel araç."
        
        # Uzak (City/Intel) Araçları
        if not target_service:
            return f"Hata: '{name}' aracı yönlendirilmemiş."

        log.info(f"🚀 [MCP -> {target_service.upper()}] {name} Args: {kwargs}")
        return await mcp_rpc_call(target_service, "tools/call", {"name": name, "arguments": kwargs})

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

    def agent_node(state: AgentState):
        msgs = [SystemMessage(content=dynamic_prompt)] + history + state["messages"]
        return {"messages": [model_with_tools.invoke(msgs)]}

    def should_continue(state: AgentState):
        return "tools" if state["messages"][-1].tool_calls else END

    workflow = StateGraph(AgentState)
    workflow.add_node("agent", agent_node)
    workflow.add_node("tools", tool_node)
    workflow.set_entry_point("agent")
    workflow.add_conditional_edges("agent", should_continue)
    workflow.add_edge("tools", "agent")
    
    # 6. Çalıştır
    final_state = await workflow.compile().ainvoke({"messages": [HumanMessage(content=request.message)]})
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