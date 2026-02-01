import operator
import httpx
import json
import asyncio
import redis
import os
from datetime import datetime
from typing import TypedDict, Annotated, List, Any, Dict
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, create_model

# --- MODÜLER İMPORTLAR (JİLET GİBİ OLDU) ---
from profile_manager import ProfileManager          # Hafıza Yöneticisi
from tools import MANUAL_TOOLS                      # Araç Tanımları
from prompt_manager import get_dynamic_system_prompt # Zeka/Prompt Yöneticisi

# --- LANGCHAIN ---
from langchain_openai import ChatOpenAI 
# (Eğer Claude kullanıyorsan: from langchain_anthropic import ChatAnthropic)
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

from loguru import logger as log

# --- AYARLAR ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CITY_AGENT_URL = "http://geo_mcp_city:8000"
INTEL_AGENT_URL = "http://geo_mcp_intel:8001"
REDIS_HOST = "geo_redis"

# --- GLOBAL DURUM ---
RUNTIME_TOOLS = []
MCP_SESSIONS: Dict[str, str] = {}
PENDING_REQUESTS: Dict[str, asyncio.Future] = {}

# --- REDIS KURULUMU ---
try:
    redis_client = redis.Redis(host=REDIS_HOST, port=6379, db=0, decode_responses=True)
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
    "save_location": "city",
    # INTEL
    "get_pharmacies": "intel",
    "get_fuel_prices": "intel",
    "get_city_events": "intel",
    "get_sports_events": "intel",
    # LOCAL
    "remember_info": "orchestrator",  
}

# --- RPC ÇAĞRISI (SAĞLAM BAĞLANTI MANTIĞI) ---
async def mcp_rpc_call(service_name: str, method: str, params: dict = None):
    # Session ID bekleme döngüsü
    for _ in range(20): 
        if MCP_SESSIONS.get(service_name): break
        await asyncio.sleep(0.5)
    
    session_url = MCP_SESSIONS.get(service_name)
    if not session_url: return f"Hata: {service_name.upper()} Ajanı çevrimdışı."

    req_id = str(int(datetime.now().timestamp() * 1000))
    json_id = int(req_id)
    
    payload = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": json_id}
    
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    PENDING_REQUESTS[req_id] = future
    
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            await client.post(session_url, json=payload)
            response_data = await asyncio.wait_for(future, timeout=80.0)
            
            if "error" in response_data:
                err = response_data["error"]
                log.error(f"❌ RPC Hata ({service_name}): {err}")
                return f"Hata: {err}"
            
            # MCP sonucunu temizle
            result = response_data.get("result")
            if isinstance(result, dict) and "content" in result:
                 # İçerik varsa text kısmını al
                content_list = result["content"]
                if isinstance(content_list, list) and content_list:
                    return content_list[0].get("text", str(content_list))
            return result

    except asyncio.TimeoutError:
        return "Timeout (Servis geç yanıt verdi)"
    except Exception as e:
        return f"RPC Exception: {e}"
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
    asyncio.create_task(sse_listener_loop("city", f"{CITY_AGENT_URL}/sse"))
    asyncio.create_task(sse_listener_loop("intel", f"{INTEL_AGENT_URL}/sse"))
    
    await asyncio.sleep(2) 
    
    log.info("🛠️ Araçlar Yükleniyor...")
    # MANUAL_TOOLS artık tools.py'den geliyor!
    for t_def in MANUAL_TOOLS:
        tool_obj = await create_dynamic_tool(t_def)
        RUNTIME_TOOLS.append(tool_obj)
    
    log.success(f"✅ {len(RUNTIME_TOOLS)} Araç Hazır.")
    yield
    RUNTIME_TOOLS.clear()

app = FastAPI(title="GeoIntel Orchestrator", lifespan=lifespan)

# --- LLM AYARLARI ---
# Eğer Claude kullanıyorsan burayı ChatAnthropic yapabilirsin
llm = ChatOpenAI(model="gpt-4o", temperature=0, openai_api_key=OPENAI_API_KEY)

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
    # (Burada kod kısalıyor, mantık prompt_manager.py içinde)
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
        except: pass

    return {
        "response": final_response, 
        "route_polyline": route_polyline 
    }