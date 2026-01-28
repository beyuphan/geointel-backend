import operator
import httpx
import json
import asyncio
import redis  # <--- EKLENDİ
from datetime import datetime
from typing import TypedDict, Annotated, List, Any, Dict
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel, create_model

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

from config import settings
from logger import log

# --- GLOBAL DURUM ---
RUNTIME_TOOLS = []
MCP_SESSION_URL = None
PENDING_REQUESTS: Dict[str, asyncio.Future] = {}

# --- REDIS AYARLARI (EKLENDİ) ---
# Senin kod yapını bozmadan sadece hafıza bağlantısını ekledim
REDIS_HOST = "geo_redis"
REDIS_PORT = 6379
try:
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
    redis_client.ping()
    log.success("✅ [Orchestrator] Redis Hafızası Aktif")
except Exception as e:
    log.error(f"❌ [Orchestrator] Redis Hatası: {e}")
    redis_client = None

# --- MANUEL TOOL TANIMLARI ---
MANUAL_TOOLS = [
    {
        "name": "search_infrastructure_osm",
        "description": "KAMUSAL ALANLARI (Havalimanı, Meydan) bulur. Koordinat tespiti için İLK BUNU KULLAN.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "Merkez enlem"},
                "lon": {"type": "number", "description": "Merkez boylam"},
                "category": {"type": "string", "description": "ZORUNLU SEÇENEKLER: airport, park, square, mosque, hospital, school"}
            },
            "required": ["lat", "lon", "category"]
        }
    },
    {
        "name": "search_places_google",
        "description": "Ticari mekanları arar. Rota üzeri arama için 'route_polyline' parametresini kullan.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "route_polyline": {"type": "string", "description": "get_route_data aracından dönen 'polyline_encoded' verisi."}
            }
        }
    },
    {
        "name": "get_route_data",
        "description": "İki koordinat arası rota. Sadece 'lat,lon' formatında veri gir.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string"},
                "destination": {"type": "string"}
            },
            "required": ["origin", "destination"]
        }
    },
    {
        "name": "get_weather",
        "description": "Hava durumu.",
        "inputSchema": {"type": "object", "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}}, "required": ["lat", "lon"]}
    },
    {
        "name": "save_location",
        "description": "Kaydet.",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "lat": {"type": "number"}, "lon": {"type": "number"}, "category": {"type": "string"}, "note": {"type": "string"}}, "required": ["name", "lat", "lon"]}
    }
]

# --- SSE DINLEYICI (SENİN ORİJİNAL KODUN) ---
async def sse_listener_loop():
    global MCP_SESSION_URL
    base_url = f"{settings.MCP_CITY_URL}/sse"
    log.info(f"🎧 SSE Dinleniyor: {base_url}")

    async with httpx.AsyncClient(timeout=None) as client:
        try:
            async with client.stream("GET", base_url) as response:
                async for line in response.aiter_lines():
                    if not line: continue
                    if line.startswith("event: endpoint"): continue

                    if line.startswith("data: "):
                        data_str = line.replace("data: ", "").strip()
                        if data_str.startswith("/") or "http" in data_str:
                            final_url = f"{settings.MCP_CITY_URL}{data_str}" if data_str.startswith("/") else data_str
                            MCP_SESSION_URL = final_url
                            log.success(f"✅ Kanal Açık: {MCP_SESSION_URL}")
                            continue

                        if data_str.startswith("{"):
                            try:
                                msg = json.loads(data_str)
                                if "id" in msg:
                                    req_id = str(msg["id"])
                                    if req_id in PENDING_REQUESTS:
                                        future = PENDING_REQUESTS[req_id]
                                        if not future.done():
                                            future.set_result(msg)
                            except: pass
        except Exception as e:
            log.error(f"🔥 SSE Koptu: {e}")
            await asyncio.sleep(3)
            asyncio.create_task(sse_listener_loop())

# --- RPC ÇAĞRISI (SENİN ORİJİNAL KODUN) ---
async def mcp_rpc_call(method: str, params: dict = None):
    for _ in range(20): 
        if MCP_SESSION_URL: break
        await asyncio.sleep(0.5)
    
    if not MCP_SESSION_URL: return "Hata: Kanal yok."

    req_id = str(int(datetime.now().timestamp() * 1000))
    payload = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": int(req_id)}
    
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    PENDING_REQUESTS[req_id] = future
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(MCP_SESSION_URL, json=payload)
            response_data = await asyncio.wait_for(future, timeout=20.0)
            if "error" in response_data:
                err = response_data["error"]
                log.error(f"❌ RPC Hata: {err}")
                return f"Hata: {err}"
            return response_data.get("result")
    except asyncio.TimeoutError:
        return "Timeout"
    except Exception as e:
        return f"RPC Exception: {e}"
    finally:
        if req_id in PENDING_REQUESTS: del PENDING_REQUESTS[req_id]

# --- TOOL WRAPPER ---
async def create_dynamic_tool(tool_def: dict):
    name = tool_def["name"]
    desc = tool_def.get("description", "")
    schema = tool_def.get("inputSchema", {"properties": {}})
    fields = {k: (Any, ...) for k in schema.get("properties", {}).keys()}
    DynamicSchema = create_model(f"{name}_Schema", **fields)

    async def execution_wrapper(**kwargs):
        log.info(f"🚀 [MCP] Çalıştırılıyor: {name} | Argümanlar: {kwargs}")
        result = await mcp_rpc_call("tools/call", {"name": name, "arguments": kwargs})
        
        # Sonucu stringe çevir (LangGraph uyumu için)
        if isinstance(result, dict) and "content" in result:
             text_content = [c["text"] for c in result["content"] if c["type"] == "text"]
             final = "\n".join(text_content)
             log.success(f"✅ [MCP] {name} Sonucu: {final[:200]}...")
             return final
        return str(result)

    return StructuredTool.from_function(
        func=None, coroutine=execution_wrapper, name=name, description=desc, args_schema=DynamicSchema
    )

# --- LIFESPAN ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(sse_listener_loop())
    await asyncio.sleep(2) 
    
    log.info("🤝 Protokol Başlatılıyor (Initialize)...")
    init_result = await mcp_rpc_call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "Orchestrator", "version": "1.0"}})
    
    if init_result: log.success("✅ Protokol Onaylandı.")
    else: log.warning("⚠️ Initialize cevapsız kaldı.")

    log.info("🛠️ Araçlar Yükleniyor...")
    for t_def in MANUAL_TOOLS:
        tool_obj = await create_dynamic_tool(t_def)
        RUNTIME_TOOLS.append(tool_obj)
    
    log.success(f"✅ {len(RUNTIME_TOOLS)} Araç Hazır.")
    yield
    task.cancel()
    RUNTIME_TOOLS.clear()

app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

# --- SENİN MODEL AYARLARIN (DOKUNMADIM) ---
llm = ChatAnthropic(
    model="claude-sonnet-4-5-20250929", 
    temperature=0,
    api_key=settings.ANTHROPIC_API_KEY
)

SYSTEM_PROMPT = """
Sen GeoIntel Ajanısın. Görevin kesin coğrafi verilerle planlama yapmak.

KURALLAR:
1. ASLA koordinat tahmini yapma.
2. Kamusal alanlar (Havalimanı, Meydan, Okul, Cami) için `search_infrastructure_osm` kullan.
   - OSM kategorileri: 'airport', 'park', 'square', 'mosque', 'hospital', 'school'.
3. Ticari işletmeler için `search_places_google` kullan.
4. Rota hesapla (`get_route_data`).
5. Rota sonucunda gelen `analiz_noktalari` için `get_weather` kontrolü yap.
6. ANALİZ YAP: Yağmur, kar, rüzgar uyarısı ver.

ASLA sadece ham veriyi basma. Bir seyahat asistanı gibi davran.

ROTA ÜZERİ ARAMA KURALI:
1. `get_route_data` çalışınca rota Redis'e kaydolur.
2. `search_places_google` için `route_polyline` parametresine sadece "LATEST" yaz.
3. Uzun polyline stringi kopyalama.

HAFIZA KURALI:
- Kullanıcı "Orayı kaydet" veya "Bahsettiğin yer" derse, geçmiş mesajlardaki mekan bilgilerini ve koordinatlarını hatırla.
"""

# --- GÜNCELLENEN REQUEST MODELİ (Session ID Eklendi) ---
class ChatRequest(BaseModel):
    session_id: str = "default_session"  # EKLENDİ
    message: str

@app.post("/chat")
async def chat_endpoint(request: ChatRequest):
    if not RUNTIME_TOOLS: return {"error": "Araçlar yok."}
    
    model_with_tools = llm.bind_tools(RUNTIME_TOOLS)
    tool_node = ToolNode(RUNTIME_TOOLS)
    
    # --- 1. GEÇMİŞİ REDIS'TEN ÇEK ---
    history_msgs = []
    if redis_client:
        try:
            stored_history = redis_client.lrange(f"chat:{request.session_id}", 0, -1)
            for item in stored_history:
                msg_data = json.loads(item)
                if msg_data["role"] == "user":
                    history_msgs.append(HumanMessage(content=msg_data["content"]))
                elif msg_data["role"] == "assistant":
                    history_msgs.append(AIMessage(content=msg_data["content"]))
        except Exception as e:
            log.warning(f"Geçmiş çekilemedi: {e}")

    class AgentState(TypedDict):
        messages: Annotated[List[Any], operator.add]

    # --- 2. HAFIZAYI BEYNE ENJEKTE ET ---
    def agent_node(state: AgentState):
        # System Prompt + REDIS GEÇMİŞİ + Şu anki mesaj
        # Not: state["messages"] sadece bu request'teki yeni mesajı içerir.
        msgs = [SystemMessage(content=SYSTEM_PROMPT)] + history_msgs + state["messages"]
        return {"messages": [model_with_tools.invoke(msgs)]}

    def should_continue(state: AgentState):
        return "tools" if state["messages"][-1].tool_calls else END

    workflow = StateGraph(AgentState)
    workflow.add_node("agent", agent_node)
    workflow.add_node("tools", tool_node)
    workflow.set_entry_point("agent")
    workflow.add_conditional_edges("agent", should_continue)
    workflow.add_edge("tools", "agent")
    
    # --- 3. ÇALIŞTIR ---
    final = await workflow.compile().ainvoke({"messages": [HumanMessage(content=request.message)]})
    final_response = final["messages"][-1].content

    # --- 4. GEÇMİŞİ KAYDET ---
    if redis_client:
        try:
            # User Message
            redis_client.rpush(f"chat:{request.session_id}", json.dumps({"role": "user", "content": request.message}))
            # AI Response
            redis_client.rpush(f"chat:{request.session_id}", json.dumps({"role": "assistant", "content": final_response}))
            # 24 Saat Sakla
            redis_client.expire(f"chat:{request.session_id}", 86400)
            # Son 20 mesajı tut (Sliding Window)
            redis_client.ltrim(f"chat:{request.session_id}", -20, -1)
        except Exception as e:
            log.warning(f"Geçmiş kaydedilemedi: {e}")

    return {"response": final_response}