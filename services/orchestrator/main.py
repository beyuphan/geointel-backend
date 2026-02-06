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
from profile_manager import ProfileManager           
from tools import MANUAL_TOOLS                       
from prompt_manager import get_dynamic_system_prompt 

# --- LANGCHAIN & AI ---
from langchain_anthropic import ChatAnthropic        
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import StateGraph, END

from config import settings
from logger import log

# --- GLOBAL DEĞİŞKENLER ---
RUNTIME_TOOLS = []
MCP_SESSIONS: Dict[str, str] = {}
PENDING_REQUESTS: Dict[str, asyncio.Future] = {}
RPC_TIMEOUT = 25.0

# --- 1. MODELLER VE STATE ---

class IntentAnalysis(BaseModel):
    category: Literal["fuel", "pharmacy", "event", "routing", "general"] = Field(
        description="Kullanıcının isteğinin ana kategorisi"
    )
    urgency: bool = Field(description="İşlem acil mi?")
    focus_points: List[str] = Field(description="Anahtar kelimeler")

class AgentState(TypedDict):
    messages: Annotated[List[Any], operator.add]
    intent: Dict[str, Any]
    retry_count: int
    session_id: str 

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

llm = ChatAnthropic(
    model="claude-sonnet-4-5-20250929", 
    temperature=0,
    api_key=settings.ANTHROPIC_API_KEY
)

# --- 2. NODE FONKSİYONLARI ---

async def intent_node(state: AgentState):
    msg = state["messages"][-1].content
    log.info(f"🔍 [Classifier] Niyet analizi yapılıyor: '{msg[:50]}...'")
    model_with_structure = llm.with_structured_output(IntentAnalysis)
    try:
        intent_result = await model_with_structure.ainvoke(f"Analiz et: {msg}")
        log.success(f"🎯 [Classifier] Kategori: {intent_result.category.upper()}")
        return {"intent": intent_result.dict()}
    except Exception as e:
        log.error(f"❌ [Classifier] Hata: {e}")
        return {"intent": {"category": "general", "focus_points": [], "urgency": False}}

def should_continue(state: AgentState):
    last_message = state["messages"][-1]
    
    if last_message.tool_calls:
        log.info(f"🛠️ [Router] Ajan {len(last_message.tool_calls)} araç çağrısı yaptı.")
        return "tools"
    
    if state.get("retry_count", 0) < 2:
        if not last_message.content or "üzgünüm" in last_message.content.lower():
            log.warning(f"🔄 [Router] Sonuç yetersiz, Retry #{state.get('retry_count', 0) + 1} başlatılıyor.")
            return "agent"

    log.info("🏁 [Router] Akış sonlandırılıyor.")
    return END

# --- 3. MCP & SSE ALTYAPISI ---

async def mcp_rpc_call(service_name: str, method: str, params: dict = None) -> Union[dict, str]:
    session_url = MCP_SESSIONS.get(service_name)
    if not session_url:
        log.error(f"🚫 [RPC] {service_name.upper()} ajanı bulunamadı.")
        return {"status": "error", "error": f"{service_name.upper()} ajanı çevrimdışı."}

    req_id = str(int(datetime.now().timestamp() * 1000))
    payload = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": int(req_id)}
    
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    PENDING_REQUESTS[req_id] = future
    
    try:
        log.info(f"📤 [RPC -> {service_name.upper()}] Metod: {method} | ID: {req_id}")
        async with httpx.AsyncClient(timeout=RPC_TIMEOUT + 5.0) as client:
            resp = await client.post(session_url, json=payload)
            if resp.status_code not in [200, 202]: 
                raise Exception(f"HTTP {resp.status_code}: {resp.text}")
            
            response_data = await asyncio.wait_for(future, timeout=RPC_TIMEOUT)
            log.success(f"📥 [RPC <- {service_name.upper()}] Yanıt alındı.")
            
            result = response_data.get("result")
            if isinstance(result, dict) and "content" in result:
                text_data = result["content"][0].get("text")
                try: return json.loads(text_data)
                except: return text_data
            return result
    except Exception as e:
        log.error(f"🔥 [RPC CRITICAL] {service_name.upper()} hatası: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        if req_id in PENDING_REQUESTS: del PENDING_REQUESTS[req_id]

async def sse_listener_loop(service_name: str, base_url: str):
    if not base_url.startswith("http"):
        base_url = f"http://{base_url}"
        
    log.info(f"🎧 [{service_name.upper()}] SSE Dinleme Başladı: {base_url}")
    async with httpx.AsyncClient(timeout=None) as client:
        try:
            async with client.stream("GET", base_url) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line.replace("data: ", "").strip()
                        # 1. ÖNCE JSON MU DİYE BAK (Eğer JSON ise bu bir mesajdır, URL değildir)
                        if data_str.startswith("{"):
                            try:
                                msg = json.loads(data_str)
                                if "id" in msg and str(msg["id"]) in PENDING_REQUESTS:
                                    future = PENDING_REQUESTS[str(msg["id"])]
                                    if not future.done(): future.set_result(msg)
                                continue # JSON ise aşağıya (URL kontrolüne) geçme!
                            except: pass
                            
                        if data_str.startswith("/") or "http" in data_str:
                            root = base_url.replace("/sse", "")
                            final_url = f"{root}{data_str}" if data_str.startswith("/") else data_str
                            MCP_SESSIONS[service_name] = final_url
                            log.success(f"🔗 [{service_name.upper()}] MCP Kanalı Kuruldu: {final_url}")
                            asyncio.create_task(mcp_rpc_call(service_name, "initialize", {
                                "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "Orchestrator", "version": "1.0"}
                            }))
                        elif data_str.startswith("{"):
                            msg = json.loads(data_str)
                            if "id" in msg and str(msg["id"]) in PENDING_REQUESTS:
                                future = PENDING_REQUESTS[str(msg["id"])]
                                if not future.done(): future.set_result(msg)
        except Exception as e:
            log.error(f"📡 [{service_name.upper()}] SSE Bağlantısı Koptu: {e}")
            await asyncio.sleep(3)
            asyncio.create_task(sse_listener_loop(service_name, base_url))

# --- 4. TOOL INJECTION & EXECUTION ---

async def create_dynamic_tool(tool_def: dict):
    name = tool_def["name"]
    desc = tool_def.get("description", "")
    schema = tool_def.get("inputSchema", {"properties": {}})
    
    fields = {k: (Any, ...) for k in schema.get("properties", {}).keys()}
    fields["session_id"] = (str, "default_session") 
    DynamicSchema = create_model(f"{name}_Schema", **fields)

    async def execution_wrapper(**kwargs):
        target_service = TOOL_ROUTER.get(name)
        sid = kwargs.get("session_id", "default_session")
        route_key = f"route:{sid}"
        
        # 🧠 Rota Enjeksiyonu
        if name in ["analyze_route_weather", "search_places_google"] and redis_client:
            poly_param = "polyline" if name == "analyze_route_weather" else "route_polyline"
            if not kwargs.get(poly_param) or kwargs.get(poly_param) == "LATEST":
                latest = redis_client.get(route_key)
                if latest:
                    kwargs[poly_param] = latest
                    log.info(f"🧠 [Memory] '{name}' için '{sid}' rotası enjekte edildi.")
                else:
                    log.warning(f"⚠️ [Memory] '{sid}' için rota bulunamadı, '{name}' boş polyline ile çalışıyor.")

        if target_service == "orchestrator":
            log.info(f"🧠 [Local] {name} çalıştırılıyor...")
            return await ProfileManager.update_memory(kwargs.get("category"), kwargs.get("value"))
        mcp_args = {k: v for k, v in kwargs.items() if k != "session_id"}
        
        log.info(f"🚀 [Tool Execute] {name} (Service: {target_service.upper()})")
        result = await mcp_rpc_call(target_service, "tools/call", {"name": name, "arguments": mcp_args})

        # 💾 Rota Kaydı
        if name == "get_route_data" and redis_client and isinstance(result, dict):
            poly = result.get("polyline") or result.get("polyline_encoded")
            if poly:
                redis_client.set(route_key, poly)
                redis_client.expire(route_key, 3600)
                log.success(f"💾 [Memory] Yeni rota '{sid}' için kaydedildi.")

        return result

    return StructuredTool.from_function(func=None, coroutine=execution_wrapper, name=name, description=desc, args_schema=DynamicSchema)

async def custom_tool_node(state: AgentState):
    msgs = []
    last_msg = state["messages"][-1]
    for tool_call in last_msg.tool_calls:
        log.info(f"🛠️ [Node: Tools] Çağrılıyor: {tool_call['name']}")
        tool_call["args"]["session_id"] = state["session_id"]
        tool = next(t for t in RUNTIME_TOOLS if t.name == tool_call["name"])
        result = await tool.ainvoke(tool_call["args"])
        msgs.append(ToolMessage(content=json.dumps(result) if isinstance(result, dict) else str(result), tool_call_id=tool_call["id"]))
    return {"messages": msgs}

# --- 5. APP SETUP & GRAPH ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(sse_listener_loop("city", f"{settings.MCP_CITY_URL}/sse"))
    asyncio.create_task(sse_listener_loop("intel", f"{settings.MCP_INTEL_URL}/sse"))
    await asyncio.sleep(2) # Ajanların bağlanması için kısa bir beklemeawait asyncio.sleep(2)
    log.info("🛠️ Araçlar yükleniyor...")
    for t_def in MANUAL_TOOLS:
        RUNTIME_TOOLS.append(await create_dynamic_tool(t_def))
    log.success(f"✅ {len(RUNTIME_TOOLS)} Araç hazır.")
    yield
    RUNTIME_TOOLS.clear()

app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class ChatRequest(BaseModel):
    session_id: str = "default_session"
    message: str

@app.post("/chat")
async def chat_endpoint(request: ChatRequest):
    log.info(f"📩 [New Request] Session: {request.session_id} | Msg: {request.message[:30]}...")
    user_context = await ProfileManager.get_user_context(request.session_id)
    model_with_tools = llm.bind_tools(RUNTIME_TOOLS)
    
    history = []
    if redis_client:
        stored = redis_client.lrange(f"chat:{request.session_id}", 0, -1)
        for item in stored:
            m = json.loads(item)
            history.append(HumanMessage(content=m["content"]) if m["role"]=="user" else AIMessage(content=m["content"]))

    def agent_node(state: AgentState):
        log.info(f"🤖 [Node: Agent] Çalışıyor (Retry: {state.get('retry_count', 0)})")
        prompt = get_dynamic_system_prompt(user_context, state["intent"])
        if state.get("retry_count", 0) > 0:
            prompt += "\n⚠️ Önceki cevap yetersizdi. Lütfen araçları daha spesifik parametrelerle kullan."
        msgs = [SystemMessage(content=prompt)] + history + state["messages"]
        response = model_with_tools.invoke(msgs)
        return {"messages": [response], "retry_count": state.get("retry_count", 0) + 1}

    workflow = StateGraph(AgentState)
    workflow.add_node("classifier", intent_node)
    workflow.add_node("agent", agent_node)
    workflow.add_node("tools", custom_tool_node)

    workflow.set_entry_point("classifier")
    workflow.add_edge("classifier", "agent")
    workflow.add_conditional_edges("agent", should_continue, {"tools": "tools", "agent": "agent", END: END})
    workflow.add_edge("tools", "agent")
    
    executor = workflow.compile()
    log.info("🚀 [Graph] Yürütme başlatıldı.")
    final_state = await executor.ainvoke({
        "messages": [HumanMessage(content=request.message)],
        "intent": {}, "retry_count": 0, "session_id": request.session_id
    })
    
    response_text = final_state["messages"][-1].content
    log.success("✅ [Graph] Yürütme tamamlandı.")
    
    if redis_client:
        chat_key = f"chat:{request.session_id}"
        redis_client.rpush(chat_key, json.dumps({"role": "user", "content": request.message}))
        redis_client.rpush(chat_key, json.dumps({"role": "assistant", "content": response_text}))
        redis_client.ltrim(chat_key, -20, -1)
        redis_client.expire(chat_key, 86400)

    return {"response": response_text, "route_polyline": redis_client.get(f"route:{request.session_id}") if redis_client else None}