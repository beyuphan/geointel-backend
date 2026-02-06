import operator
import httpx
import json
import asyncio
import redis
import os
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Literal, List, Dict, Any, Union, Annotated, TypedDict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, create_model, Field

# --- MODÜLER İMPORTLAR ---
from profile_manager import ProfileManager           # Hafıza Yöneticisi
from tools import MANUAL_TOOLS                       # Araç Tanımları
from prompt_manager import get_dynamic_system_prompt # Zeka/Prompt Yöneticisi

# --- LANGCHAIN & AI ---
from langchain_anthropic import ChatAnthropic        
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

# from langchain_google_genai import ChatGoogleGenerativeAI # İstersen açarsın

from config import settings
from logger import log

# --- GLOBAL DEĞİŞKENLER ---
RUNTIME_TOOLS = []
MCP_SESSIONS: Dict[str, str] = {}
PENDING_REQUESTS: Dict[str, asyncio.Future] = {}

# --- CIRCUIT BREAKER AYARLARI ---
RPC_TIMEOUT = 25.0
CIRCUIT_STATES = {}

# --- 1. MODELLER VE STATE ---

class IntentAnalysis(BaseModel):
    category: Literal["fuel", "pharmacy", "event", "routing", "general"] = Field(
        description="Kullanıcının isteğinin ana kategorisi"
    )
    urgency: bool = Field(description="İşlem acil mi? (Örn: Nöbetçi eczane)")
    focus_points: List[str] = Field(description="Mesajdaki anahtar kelimeler (örn: 'ucuz', 'dizel')")

class AgentState(TypedDict):
    messages: Annotated[List[Any], operator.add]
    intent: Dict[str, Any]  # Classifier'dan gelen niyet
    retry_count: int        # Hata döngüsü kontrolü için

# --- REDIS KURULUMU ---
try:
    redis_client = redis.Redis(host="geo_redis", port=6379, db=0, decode_responses=True)
    redis_client.ping()
    log.success("✅ [Orchestrator] Redis Hafızası Aktif")
except Exception as e:
    log.error(f"❌ [Orchestrator] Redis Hatası: {e}")
    redis_client = None

# --- TOOL ROUTER ---
TOOL_ROUTER = {
    "search_infrastructure_osm": "city",
    "search_places_google": "city",
    "get_route_data": "city",
    "get_weather": "city",
    "analyze_route_weather": "city",
    "save_location": "city",
    "get_toll_prices": "city",
    "get_pharmacies": "intel",
    "get_fuel_prices": "intel",
    "get_city_events": "intel",
    "get_sports_events": "intel",
    "remember_info": "orchestrator",  
}

# --- LLM AYARLARI (GLOBAL OLARAK TANIMLIYORUZ Kİ FONKSİYONLAR GÖRSÜN) ---
llm = ChatAnthropic(
    model="claude-sonnet-4-5-20250929", 
    temperature=0,
    api_key=settings.ANTHROPIC_API_KEY
)

# --- 2. NODE FONKSİYONLARI ---

async def intent_node(state: AgentState):
    """Kullanıcının niyetini analiz eder."""
    msg = state["messages"][-1].content
    
    # Modelin yapılandırılmış çıktı (Structured Output) vermesini sağlıyoruz
    model_with_structure = llm.with_structured_output(IntentAnalysis)
    
    try:
        intent_result = await model_with_structure.ainvoke(
            f"Aşağıdaki kullanıcı mesajının niyetini analiz et: {msg}"
        )
        return {"intent": intent_result.dict(), "retry_count": 0}
    except Exception as e:
        log.error(f"❌ Niyet analizi hatası: {e}")
        return {"intent": {"category": "general", "focus_points": [], "urgency": False}}

def should_continue(state: AgentState):
    """Akışın nereye gideceğine karar verir."""
    last_message = state["messages"][-1]
    
    # 1. Eğer model tool çağrısı yaptıysa -> tools düğümüne
    if last_message.tool_calls:
        return "tools"
    
    # 2. HATA YÖNETİMİ: Cevap tatmin edici değilse -> agent düğümüne (Retry)
    # Burası senin asıl istediğin mantık
    if "üzgünüm" in last_message.content.lower() or "bulunamadı" in last_message.content.lower():
        if state.get("retry_count", 0) < 2:
            log.warning("🔄 [Retry] Ajan tatmin edici sonuç bulamadı, tekrar deniyor...")
            return "agent" 

    # 3. Yoksa bitir
    return END

# --- 3. MCP & SSE ALTYAPISI ---
# (Buradaki fonksiyonlar sağlamdı, aynen korudum)

async def mcp_rpc_call(service_name: str, method: str, params: dict = None) -> Union[dict, str]:
    session_url = MCP_SESSIONS.get(service_name)
    if not session_url:
        await asyncio.sleep(1)
        session_url = MCP_SESSIONS.get(service_name)
        if not session_url:
            return {"status": "error", "error": f"{service_name.upper()} ajanı çevrimdışı."}

    req_id = str(int(datetime.now().timestamp() * 1000))
    payload = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": int(req_id)}
    
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    PENDING_REQUESTS[req_id] = future
    
    try:
        log.info(f"⚡ [RPC -> {service_name.upper()}] Metod: {method}")
        async with httpx.AsyncClient(timeout=RPC_TIMEOUT + 5.0) as client:
            resp = await client.post(session_url, json=payload)
            if resp.status_code not in [200, 202]: raise Exception(f"HTTP {resp.status_code}")

            response_data = await asyncio.wait_for(future, timeout=RPC_TIMEOUT)
            
            if "error" in response_data: return {"status": "error", "error": str(response_data["error"])}
            
            result = response_data.get("result")
            if isinstance(result, dict) and "content" in result:
                content = result["content"]
                if isinstance(content, list) and len(content) > 0:
                    text_data = content[0].get("text")
                    try: return json.loads(text_data)
                    except: return text_data
            return result

    except asyncio.TimeoutError:
        return {"status": "partial_error", "error": "Servis zaman aşımı."}
    except Exception as e:
        log.error(f"🔥 [CRITICAL] RPC Patladı: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        if req_id in PENDING_REQUESTS: del PENDING_REQUESTS[req_id]

async def sse_listener_loop(service_name: str, base_url: str):
    log.info(f"🎧 [{service_name.upper()}] SSE Dinleniyor: {base_url}")
    async with httpx.AsyncClient(timeout=None) as client:
        try:
            async with client.stream("GET", base_url) as response:
                async for line in response.aiter_lines():
                    if not line or line.startswith("event: endpoint"): continue
                    if line.startswith("data: "):
                        data_str = line.replace("data: ", "").strip()
                        if data_str.startswith("/") or "http" in data_str:
                            root = base_url.replace("/sse", "")
                            final_url = f"{root}{data_str}" if data_str.startswith("/") else data_str
                            MCP_SESSIONS[service_name] = final_url
                            log.success(f"✅ [{service_name.upper()}] Kanal Açık: {final_url}")
                            asyncio.create_task(mcp_rpc_call(service_name, "initialize", {
                                "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "Orchestrator", "version": "1.0"}
                            }))
                            continue
                        if data_str.startswith("{"):
                            try:
                                msg = json.loads(data_str)
                                if "id" in msg and str(msg["id"]) in PENDING_REQUESTS:
                                    future = PENDING_REQUESTS[str(msg["id"])]
                                    if not future.done(): future.set_result(msg)
                            except: pass
        except Exception as e:
            log.error(f"🔥 [{service_name.upper()}] SSE Koptu: {e}")
            await asyncio.sleep(3)
            asyncio.create_task(sse_listener_loop(service_name, base_url))

async def create_dynamic_tool(tool_def: dict):
    name = tool_def["name"]
    desc = tool_def.get("description", "")
    schema = tool_def.get("inputSchema", {"properties": {}})
    fields = {k: (Any, ...) for k in schema.get("properties", {}).keys()}
    DynamicSchema = create_model(f"{name}_Schema", **fields)

    async def execution_wrapper(**kwargs):
        target_service = TOOL_ROUTER.get(name)
        
        # Enjeksiyonlar
        if name == "analyze_route_weather" and redis_client:
            if not kwargs.get("polyline") or kwargs.get("polyline") == "LATEST":
                latest_route = redis_client.get("latest_route")
                if latest_route:
                    kwargs["polyline"] = latest_route
                    log.info("🧠 [Memory] Son rota hafızadan çekildi.")
                else:
                    return "Hata: Önce bir rota oluşturulmalı."

        if target_service == "orchestrator":
            if name == "remember_info":
                return await ProfileManager.update_memory(kwargs.get("category"), kwargs.get("value"))
            return "Bilinmeyen yerel araç."
        
        if not target_service: return f"Hata: '{name}' yönlendirilmemiş."

        log.info(f"🚀 [MCP -> {target_service.upper()}] {name} Args: {kwargs}")
        result = await mcp_rpc_call(target_service, "tools/call", {"name": name, "arguments": kwargs})

        if name == "get_route_data" and redis_client:
            if isinstance(result, dict):
                poly = result.get("polyline") or result.get("polyline_encoded")
                if poly:
                    redis_client.set("latest_route", poly)
                    log.info("💾 [Memory] Rota kaydedildi.")

        return result

    return StructuredTool.from_function(
        func=None, coroutine=execution_wrapper, name=name, description=desc, args_schema=DynamicSchema
    )

# --- FASTAPI SETUP ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(sse_listener_loop("city", f"{settings.MCP_CITY_URL}/sse"))
    asyncio.create_task(sse_listener_loop("intel", f"{settings.MCP_INTEL_URL}/sse"))
    await asyncio.sleep(2)
    
    log.info("🛠️ Araçlar Yükleniyor...")
    for t_def in MANUAL_TOOLS:
        tool_obj = await create_dynamic_tool(t_def)
        RUNTIME_TOOLS.append(tool_obj)
    
    log.success(f"✅ {len(RUNTIME_TOOLS)} Araç Hazır.")
    yield
    RUNTIME_TOOLS.clear()

app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class ChatRequest(BaseModel):
    session_id: str = "default_session"
    message: str

# --- 4. CHAT ENDPOINT (DÜZELTİLDİ) ---

@app.post("/chat")
async def chat_endpoint(request: ChatRequest):
    if not RUNTIME_TOOLS: return {"error": "Araçlar yüklenmedi."}
    
    # 1. Profil Verisi
    user_context_str = await ProfileManager.get_user_context("test_pilot")
    
    # 2. Modeli Bağla
    model_with_tools = llm.bind_tools(RUNTIME_TOOLS)
    tool_node = ToolNode(RUNTIME_TOOLS)
    
    # 3. Geçmişi Yükle
    history = []
    if redis_client:
        try:
            stored = redis_client.lrange(f"chat:{request.session_id}", 0, -1)
            for item in stored:
                msg = json.loads(item)
                if msg["role"] == "user": history.append(HumanMessage(content=msg["content"]))
                elif msg["role"] == "assistant": history.append(AIMessage(content=msg["content"]))
        except: pass

    # 4. Ajan Düğümü (Context burada işleniyor)
    def agent_node(state: AgentState):
        # Intent classifier'dan geliyor
        intent_data = state["intent"]
        
        # Dinamik promptu burada oluşturuyoruz
        dynamic_prompt = get_dynamic_system_prompt(user_context_str, intent_data)
        
        retry_note = ""
        if state.get("retry_count", 0) > 0:
            retry_note = "\n\n⚠️ NOT: Önceki denemede sonuç bulunamadı veya eksik kaldı. Lütfen arama parametrelerini değiştir veya genişlet."

        # Mesaj listesi: System Prompt + Geçmiş + Güncel Mesajlar
        msgs = [SystemMessage(content=dynamic_prompt + retry_note)] + history + state["messages"]
        
        return {
            "messages": [model_with_tools.invoke(msgs)],
            "retry_count": state.get("retry_count", 0) + 1
        }

    # 5. Graph Oluşturma
    workflow = StateGraph(AgentState)
    workflow.add_node("classifier", intent_node)
    workflow.add_node("agent", agent_node)
    workflow.add_node("tools", tool_node)

    workflow.set_entry_point("classifier")
    
    workflow.add_edge("classifier", "agent")
    
    # BURASI DÜZELDİ: Global 'should_continue' fonksiyonunu kullanıyoruz!
    workflow.add_conditional_edges(
        "agent", 
        should_continue, 
        {
            "tools": "tools",
            "agent": "agent", # Retry döngüsü artık çalışacak
            END: END
        }
    )
    workflow.add_edge("tools", "agent")
    
    app_graph = workflow.compile()
    
    # 6. Çalıştır
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

    # Frontend eğer "LATEST" görürse Redis'ten çekeceğini biliyor, 
    # ama biz yine de varsa gönderelim.
    if route_polyline and route_polyline == "LATEST":
         # Zaten değişkende duruyor, pass
         pass

    return {
        "response": final_response, 
        "route_polyline": route_polyline 
    }