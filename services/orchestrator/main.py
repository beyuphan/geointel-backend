import operator
import httpx
import json
import asyncio
import redis
import os
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Literal, List, Dict, Any, Union, Annotated, TypedDict, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, create_model, Field

# --- PROJE BAĞIMLILIKLARI ---
from profile_manager import ProfileManager
try:
    from tools import LOCAL_TOOLS
except ImportError:
    LOCAL_TOOLS = [] 
    
from prompt_manager import get_dynamic_system_prompt 

# --- LANGCHAIN & AI KATMANI ---
from langchain_anthropic import ChatAnthropic        
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import StateGraph, END
from langchain_google_genai import ChatGoogleGenerativeAI

from config import settings
from logger import log

# --- 1. MODELLER VE DURUM YÖNETİMİ ---

class IntentAnalysis(BaseModel):
    category: Literal["fuel", "pharmacy", "event", "routing", "general"] = Field(
        description="Kullanıcının isteğinin ana kategorisi"
    )
    urgency: bool = Field(description="İşlem acil mi?")
    focus_points: List[str] = Field(description="Anahtar kelimeler")
    complexity: Literal["low", "high"] = Field(
        description="Basit bilgi çekme ve arama (Örn: Eczane nerede, fiyatlar nedir) için 'low'. "
                    "Çoklu adım, analiz, rota kıyaslaması ve çevresel faktör sentezi için 'high'."
    )
class AgentState(TypedDict):
    messages: Annotated[List[Any], operator.add]
    intent: Dict[str, Any]
    retry_count: int
    session_id: str 
    visual_data: Dict[str, Any]

# --- 2. MERKEZİ ORCHESTRATOR YÖNETİCİSİ ---

class GeoIntelOrchestrator:
    """
    Tüm MCP bağlantılarını, araç keşiflerini ve RPC trafiğini yöneten merkezi sınıf.
    Global değişken karmaşasını önler ve thread-safe bir yapı sunar.
    """
    def __init__(self):
        self.runtime_tools: List[StructuredTool] = []
        self.tool_router: Dict[str, str] = {}
        self.sessions: Dict[str, str] = {}
        self.pending_requests: Dict[str, asyncio.Future] = {}
        self.rpc_timeout = 25.0
        self.redis_client = self._init_redis()
        self.llm_claude = ChatAnthropic(
            model="claude-sonnet-4-5-20250929", 
            temperature=0,
            api_key=settings.ANTHROPIC_API_KEY
        )
        self.llm_gemini = ChatGoogleGenerativeAI(
            model="gemini-2.0-flash",
            temperature=0,
            google_api_key=settings.GOOGLE_API_KEY
        )

    def _init_redis(self):
        try:
            client = redis.Redis(host="geo_redis", port=6379, db=0, decode_responses=True)
            client.ping()
            log.success("✅ [Orchestrator] Redis Hafızası Aktif")
            return client
        except Exception as e:
            log.error(f"❌ [Orchestrator] Redis Hatası: {e}")
            return None

    def get_tool_by_name(self, name: str) -> Optional[StructuredTool]:
        return next((t for t in self.runtime_tools if t.name == name), None)

    async def mcp_rpc_call(self, service_name: str, method: str, params: dict = None) -> Any:
        session_url = self.sessions.get(service_name)
        if not session_url:
            log.error(f"🚫 [RPC] {service_name.upper()} ajanı bulunamadı.")
            return {"status": "error", "error": f"{service_name.upper()} ajanı çevrimdışı."}

        req_id = str(int(datetime.now().timestamp() * 1000))
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": int(req_id)}
        
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending_requests[req_id] = future
        
        try:
            log.info(f"📤 [RPC -> {service_name.upper()}] Metod: {method} | ID: {req_id}")
            async with httpx.AsyncClient(timeout=self.rpc_timeout + 5.0) as client:
                resp = await client.post(session_url, json=payload)
                
                # Fast Path: Doğrudan HTTP yanıtı
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        if "result" in data:
                            log.success(f"📥 [RPC <- {service_name.upper()}] Yanıt HTTP Body'den alındı.")
                            return self._process_mcp_result(data["result"])
                    except: pass

                # Slow Path: SSE üzerinden asenkron yanıt bekleme
                if resp.status_code not in [200, 202]: 
                    raise Exception(f"HTTP {resp.status_code}: {resp.text}")
                
                response_data = await asyncio.wait_for(future, timeout=self.rpc_timeout)
                log.success(f"📥 [RPC <- {service_name.upper()}] Yanıt SSE'den alındı.")
                return self._process_mcp_result(response_data.get("result"))

        except Exception as e:
            log.error(f"🔥 [RPC CRITICAL] {service_name.upper()} hatası: {e}")
            return {"status": "error", "error": str(e)}
        finally:
            self.pending_requests.pop(req_id, None)

    def _process_mcp_result(self, result: Any) -> Any:
        """MCP'den gelen karmaşık içeriği temiz veriye dönüştürür."""
        if isinstance(result, dict) and "content" in result:
            text_data = result["content"][0].get("text")
            try: return json.loads(text_data)
            except: return text_data
        return result

    def json_schema_to_pydantic(self, name: str, schema: dict) -> Any:
        fields = {}
        required_fields = schema.get("required", [])

        if "properties" in schema:
            for field_name, field_info in schema["properties"].items():
                t_map = {"string": str, "number": float, "integer": int, "boolean": bool}
                field_type = t_map.get(field_info.get("type"), str)
                description = field_info.get("description", "")
                if field_name in required_fields:
                    fields[field_name] = (field_type, Field(description=description))
                else:
                    fields[field_name] = (Optional[field_type], Field(default=None, description=description))
                
        fields["session_id"] = (str, "default_session")
        return create_model(f"{name}Input", **fields)

    async def create_proxy_tool(self, service_name: str, tool_info: dict):
        name = tool_info["name"]
        description = tool_info.get("description", "")
        input_schema = tool_info.get("inputSchema", {})
        pydantic_model = self.json_schema_to_pydantic(name, input_schema)
        
        async def execution_wrapper(**kwargs):
            log.info(f"🚀 [Dynamic Call] {name} -> {service_name.upper()}")
            sid = kwargs.get("session_id", "default_session")
            route_key = f"route:{sid}"
            
            # Yerel araçlar için yönlendirme
            if service_name == "orchestrator":
                 return await ProfileManager.update_memory(kwargs.get("category"), kwargs.get("value"))

            # Rota bazlı araçlar için Redis entegrasyonu
            if name in ["analyze_route_weather", "search_places_google"] and self.redis_client:
                poly_param = "polyline" if name == "analyze_route_weather" else "route_polyline"
                if not kwargs.get(poly_param) or kwargs.get(poly_param) == "LATEST":
                    latest = self.redis_client.get(route_key)
                    if latest: kwargs[poly_param] = latest

            mcp_args = {k: v for k, v in kwargs.items() if k != "session_id"}
            result = await self.mcp_rpc_call(service_name, "tools/call", {"name": name, "arguments": mcp_args})
            
            # Rota verisini cache'leme
            if name == "get_route_data" and self.redis_client and isinstance(result, dict):
                poly = result.get("polyline") or result.get("polyline_encoded")
                if poly:
                    self.redis_client.setex(route_key, 3600, poly)
                    
            return result

        return StructuredTool.from_function(
            func=None, coroutine=execution_wrapper, name=name, description=description, args_schema=pydantic_model
        )

    async def register_agent_tools(self, service_name: str):
        log.info(f"🕵️ [Discovery] {service_name.upper()} yetenekleri taranıyor...")
        response = await self.mcp_rpc_call(service_name, "tools/list")
        
        if not isinstance(response, dict) or "tools" not in response:
            log.warning(f"⚠️ [Discovery] {service_name.upper()} araç bildirmedi.")
            return

        for tool_def in response["tools"]:
            t_name = tool_def["name"]
            self.tool_router[t_name] = service_name
            lc_tool = await self.create_proxy_tool(service_name, tool_def)
            
            # Eski aracı temizle ve yenisini ekle (Atomic Update)
            self.runtime_tools = [t for t in self.runtime_tools if t.name != t_name]
            self.runtime_tools.append(lc_tool)
            
        log.success(f"✅ [Discovery] {service_name.upper()} üzerinden araçlar güncellendi.")

    async def sse_listener_loop(self, service_name: str, base_url: str):
        if not base_url.startswith("http"): base_url = f"http://{base_url}"
        log.info(f"🎧 [{service_name.upper()}] SSE Dinleme Başladı: {base_url}")
        
        async with httpx.AsyncClient(timeout=None) as client:
            try:
                async with client.stream("GET", base_url) as response:
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data: "): continue
                        
                        data_str = line.replace("data: ", "").strip()
                        
                        # Durum 1: JSON RPC Yanıtı (ID eşleşmesi)
                        if data_str.startswith("{"):
                            try:
                                msg = json.loads(data_str)
                                if "id" in msg and str(msg["id"]) in self.pending_requests:
                                    future = self.pending_requests[str(msg["id"])]
                                    if not future.done(): future.set_result(msg)
                                continue 
                            except: pass
                        
                        # Durum 2: Handshake (Session URL Discovery)
                        if data_str.startswith("/") or "http" in data_str:
                            root = base_url.replace("/sse", "")
                            self.sessions[service_name] = f"{root}{data_str}" if data_str.startswith("/") else data_str
                            log.success(f"🔗 [{service_name.upper()}] MCP Kanalı Kuruldu.")
                            
                            # Handshake & Discovery Görevi
                            async def initiate_service():
                                await asyncio.sleep(1.0)
                                await self.mcp_rpc_call(service_name, "initialize", {
                                    "protocolVersion": "2024-11-05", 
                                    "capabilities": {}, 
                                    "clientInfo": {"name": "Orchestrator", "version": "2.0"}
                                })
                                await self.register_agent_tools(service_name)

                            asyncio.create_task(initiate_service())
                            
            except Exception as e:
                log.error(f"📡 [{service_name.upper()}] SSE Bağlantısı Koptu: {e}")
                await asyncio.sleep(3)
                asyncio.create_task(self.sse_listener_loop(service_name, base_url))

# --- 3. LANGGRAPH NODES (BEYİN FONKSİYONLARI) ---

orchestrator = GeoIntelOrchestrator()

async def intent_node(state: AgentState):
    msg = state["messages"][-1].content
    model_with_structure = orchestrator.llm_gemini.with_structured_output(IntentAnalysis)
    try:
        intent_result = await model_with_structure.ainvoke(f"Analiz et: {msg}")
        log.success(f"🎯 [Classifier] Kategori: {intent_result.category.upper()} | Karmaşıklık: {intent_result.complexity.upper()}")
        return {"intent": intent_result.dict()}
    except Exception as e:
        log.error(f"Sınıflandırma hatası: {e}") 
        return {"intent": {"category": "general", "focus_points": [], "urgency": False, "complexity": "high"}}

def should_continue(state: AgentState):
    if state["messages"][-1].tool_calls: return "tools"
    return END

async def agent_node(state: AgentState):
    # Kullanıcı bağlamı ve niyet analizini al
    user_context = await ProfileManager.get_user_context(state["session_id"])
    intent = state.get("intent", {})
    category = intent.get("category", "general")
    prompt = get_dynamic_system_prompt(user_context, intent)
    
    # --- 1. YÖNLENDİRME (ROUTER) MANTIĞI ---
    # Intent içinden complexity değerini al, yoksa güvenli liman olarak 'high' (Claude) seç
    complexity = intent.get("complexity", "high")
    
    if category in ["event", "fuel", "pharmacy"]:
        active_llm = orchestrator.llm_claude
        log.info(f"🛡️ [Guardrail] Kategori {category.upper()} için Claude zorlanıyor.")

    elif complexity == "low":
        active_llm = orchestrator.llm_gemini
        log.info(f"🧠 [LLM Router] Session: {state['session_id']} | Model: Gemini 2.0 Flash (Düşük Karmaşıklık)")

        prompt += """
        \n\nKESİN KURALLAR:
        1- MEKAN VE DURUM SORGULARI: Kullanıcı hava durumu, kafe, hastane vb. bir şey sorduğunda ASLA soru sorma! 
        2- ARAÇ KULLANIMI: Hava durumu için `get_weather`, mekanlar için `search_places_google` aracını DERHAL tetikle. 
        Parametreleri kullanıcının metninden (örn: 'Beşiktaş') ayıkla ve beklemeden çalıştır.
        """
    else:
        active_llm = orchestrator.llm_claude
        log.info(f"🧠 [LLM Router] Session: {state['session_id']} | Model: Claude 4.5 Sonnet (Yüksek Karmaşıklık)")

    # --- 2. REDİS'TEN GEÇMİŞİ ÇEKME ---
    history = []
    last_category = None 
    if orchestrator.redis_client:
        # decode_responses=True olduğu için .decode() yapmana gerek yok, direkt string gelir.
        last_category = orchestrator.redis_client.get(f"last_cat:{state['session_id']}") 

        stored = orchestrator.redis_client.lrange(f"chat:{state['session_id']}", 0, -1)
        for item in stored:
            m = json.loads(item)
            history.append(HumanMessage(content=m["content"]) if m["role"] == "user" else AIMessage(content=m["content"]))

    # 🔥 BAĞLAM TEMİZLİĞİ (CONTEXT RESET) - BURASI AYNI KALIYOR
    if history and last_category and category != last_category:
        log.warning(f"🔄 [Context Reset] Kategori değişti: {last_category} -> {category}")
        reset_msg = SystemMessage(content=f"ÖNEMLİ: Konu '{last_category}' kategorisinden '{category}' kategorisine değişti. Önceki konuya ait özel talimatları KESİNLİKLE UNUT ve yeni kategoriye odaklan.")
        history.append(reset_msg)

    # Güncel kategoriyi bir sonraki tur için Redis'e kaydet
    if orchestrator.redis_client:
        orchestrator.redis_client.set(f"last_cat:{state['session_id']}", category)

    # --- 3. SEÇİLEN MODELİ ÇALIŞTIRMA ---
    # Araçları sadece o an aktif olan modele bağlıyoruz
    model_with_tools = active_llm.bind_tools(orchestrator.runtime_tools)
    msgs = [SystemMessage(content=prompt)] + history + state["messages"]
    
    # Modeli asenkron olarak tetikle
    response = await model_with_tools.ainvoke(msgs)
    
    return {"messages": [response], "retry_count": state.get("retry_count", 0) + 1}

async def custom_tool_node(state: AgentState):
    msgs = []; visual_data = state.get("visual_data", {"markers": [], "polyline": None})
    for tc in state["messages"][-1].tool_calls:
        log.info(f"🛠️ [Node: Tools] Çağrılıyor: {tc['name']}")
        tc["args"]["session_id"] = state["session_id"]
        
        tool = orchestrator.get_tool_by_name(tc["name"])
        if tool:
            res = await tool.ainvoke(tc["args"])
            # --- STRUCTURED DATA EXTRACTION (Kritik Ekli Mantık) ---
            if isinstance(res, list): # Mekan listesi (Google/OSM)
                for p in res:
                # 1. Koordinatları güvenli bir şekilde al
                    lat = p.get("lat")
                    lon = p.get("lon")
                    
                    # 2. KRİTİK KONTROL: Koordinatlar yoksa veya 0.0 ise (Null Island) bu kaydı çöpe at
                    if lat is None or lon is None or (abs(lat) < 0.0001 and abs(lon) < 0.0001):
                        log.warning(f"⚠️ [Visualizer] Geçersiz koordinatlı mekan atlandı: {p.get('name', 'Bilinmeyen')}")
                        continue
                        
                    # 3. Veriyi yapılandırılmış (structured) şekilde ekle
                    visual_data["markers"].append({
                        "name": p.get("name") or p.get("isim") or "Adsız Lokasyon",
                        "lat": float(lat),
                        "lon": float(lon),
                        "source": p.get("source", "osm"),
                        "rating": p.get("rating", 0.0), # Varsa puan bilgisini de Dashboard'a pasla
                        "is_open": p.get("is_open", "Bilinmiyor") # Kullanıcı deneyimi için ek bilgi
                    })
            elif isinstance(res, dict):
                if "polyline" in res or "polyline_encoded" in res: 
                    visual_data["polyline"] = res.get("polyline") or res.get("polyline_encoded")
                    
                    # LLM'in kafası devasa veriyle karışmasın diye polyline metnini gizliyoruz!
                    if "polyline" in res: 
                        res["polyline"] = "[HARİTAYA ÇİZİLDİ - OKUMANA GEREK YOK]"
                    if "polyline_encoded" in res: 
                        res["polyline_encoded"] = "[HARİTAYA ÇİZİLDİ - OKUMANA GEREK YOK]"
                if "lat" in res and "lon" in res: visual_data["markers"].append({"name": "Hedef", "lat": res["lat"], "lon": res["lon"]})

            tool_output = res
            if isinstance(res, list):
                tool_output = {"results": res}
            elif not isinstance(res, dict):
                tool_output = {"result": str(res)}
                
            msgs.append(ToolMessage(content=json.dumps(tool_output, ensure_ascii=False), tool_call_id=tc["id"]))
    return {"messages": msgs, "visual_data": visual_data}

# --- 4. FASTAPI UYGULAMASI VE LIFESPAN ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Yerel araçları kaydet
    for t_def in LOCAL_TOOLS:
        orchestrator.runtime_tools.append(await orchestrator.create_proxy_tool("orchestrator", t_def))
    
    # Servisleri dinlemeye başla
    services = {
        "city": settings.MCP_CITY_URL,
        "intel": settings.MCP_INTEL_URL,
        "satellite": settings.MCP_SATELLITE_URL
    }
    for name, url in services.items():
        asyncio.create_task(orchestrator.sse_listener_loop(name, f"{url}/sse"))
        
    yield
    orchestrator.runtime_tools.clear()

app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class ChatRequest(BaseModel):
    session_id: str = "default_session"
    message: str

@app.post("/chat")
async def chat_endpoint(request: ChatRequest):
    log.info(f"📩 [Request] Session: {request.session_id} | Msg: {request.message[:30]}...")
    
    workflow = StateGraph(AgentState)
    workflow.add_node("classifier", intent_node)
    workflow.add_node("agent", agent_node)
    workflow.add_node("tools", custom_tool_node)
    
    workflow.set_entry_point("classifier")
    workflow.add_edge("classifier", "agent")
    workflow.add_conditional_edges("agent", should_continue, {"tools": "tools", "agent": "agent", END: END})
    workflow.add_edge("tools", "agent")
    
    executor = workflow.compile()
    final_state = await executor.ainvoke({
        "messages": [HumanMessage(content=request.message)],
        "intent": {}, "retry_count": 0, "session_id": request.session_id, "visual_data": {"markers": [], "polyline": None}
    })
    
    raw_content = final_state["messages"][-1].content
    
    # AI yanıtı liste (block) olarak dönerse, içindeki metinleri birleştirip tek bir string yapıyoruz:
    if isinstance(raw_content, list):
        response_text = "".join([block.get("text", "") for block in raw_content if isinstance(block, dict) and "text" in block])
    else:
        response_text = str(raw_content)
    
    # Yanıtı Redis'e kaydet
    if orchestrator.redis_client:
        chat_key = f"chat:{request.session_id}"
        orchestrator.redis_client.rpush(chat_key, json.dumps({"role": "user", "content": request.message}))
        orchestrator.redis_client.rpush(chat_key, json.dumps({"role": "assistant", "content": response_text}))
        orchestrator.redis_client.ltrim(chat_key, -20, -1)
        orchestrator.redis_client.expire(chat_key, 86400)

    return {
        "response": response_text, 
        "visual_data": final_state.get("visual_data"), 
        "route_polyline": orchestrator.redis_client.get(f"route:{request.session_id}") if orchestrator.redis_client else None
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)