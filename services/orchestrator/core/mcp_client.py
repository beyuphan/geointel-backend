import asyncio
import json
import hashlib
import httpx
import redis
from datetime import datetime
from typing import Dict, Any, List, Optional
from pydantic import Field, create_model
from langchain_core.tools import StructuredTool
from langchain_anthropic import ChatAnthropic        
from langchain_google_genai import ChatGoogleGenerativeAI
from logger import log
from config import settings
from profile_manager import ProfileManager

class GeoIntelOrchestrator:
    """
    Tüm MCP bağlantılarını, araç keşiflerini ve RPC trafiğini yöneten merkezi sınıf.
    """
    def __init__(self):
        self.runtime_tools: List[StructuredTool] = []
        self.tool_router: Dict[str, str] = {}
        self.sessions: Dict[str, str] = {}
        self.pending_requests: Dict[str, asyncio.Future] = {}
        # Geointel-Backend Timeout
        # Rize-İstanbul gibi karmaşık ve uzun menzilli aramalarda süreyi 3 dakikaya (180s) uzatıyoruz
        self.rpc_timeout = 180.0
        self.redis_client = self._init_redis()
        
        # V2.5: Cache TTLs (seconds)
        self.cache_ttls = {
            "get_route_data": 120,          # 2 min (traffic changes frequently)
            "get_fuel_prices": 3600,        # 60 min
            "get_weather": 900,             # 15 min
            "get_pharmacies": 3600,         # 60 min
            "get_events": 7200,             # 120 min
            "get_toll_prices": 3600,        # 60 min
            "get_sports_matches": 3600,     # 60 min
        }
        
        # LLM Clients (V2026: Updated to latest stable models)
        self.llm_claude = ChatAnthropic(
            model="claude-sonnet-4-6", 
            # temperature=0,  # V4.x modellerinde opsiyonel veya depreke olabilir
            api_key=settings.ANTHROPIC_API_KEY
        )
        self.llm_gemini = ChatGoogleGenerativeAI(
            model="gemini-3-flash-preview",
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
            log.error(f"❌ [Orchestrator] Redis Bağlantı Hatası: {e}")
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
                t_map = {
                    "string": str, "number": float, "integer": int, "boolean": bool,
                    "array": list, "object": dict,
                }
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
                if name == "evaluate_route_strategy":
                    from core.macro_tools import RouteStrategyEvaluator
                    return await RouteStrategyEvaluator(self).evaluate(
                        origin=kwargs.get("origin"),
                        destination=kwargs.get("destination"),
                        fuel_type=kwargs.get("fuel_type", "benzin")
                    )
                if name == "plan_weather_aware_route":
                    from core.macro_tools import ContextAwarePOIPlanner
                    return await ContextAwarePOIPlanner(self).evaluate(
                        current_lat=kwargs.get("current_lat"),
                        current_lon=kwargs.get("current_lon"),
                        location_name=kwargs.get("location_name"),
                        semantic_query=kwargs.get("semantic_query"),
                        search_radius=kwargs.get("search_radius", 5000)
                    )
                if name == "get_environmental_analysis":
                    from core.macro_tools import EnvironmentalAnalyst
                    return await EnvironmentalAnalyst(self).evaluate(
                        lat=kwargs.get("lat"),
                        lon=kwargs.get("lon"),
                        analyze_vegetation=kwargs.get("analyze_vegetation", True)
                    )
                return await ProfileManager.update_memory(kwargs.get("category"), kwargs.get("value"))

            # Rota bazlı araçlar için Redis entegrasyonu
            polyline_tools = {
                "analyze_route_weather": "polyline",
                "search_places_google": "route_polyline",
                "get_route_radars": "route_polyline",
                "get_toll_for_route": "route_polyline",
                "search_hybrid_places": "route_polyline",
                "evaluate_route_strategy": "route_polyline", # Macro-tool eklendi
                "plan_weather_aware_route": "route_polyline" # Yeni Macro-tool
            }
            if name in polyline_tools and self.redis_client:
                poly_param = polyline_tools[name]
                poly_val = kwargs.get(poly_param, "")
                poly_val_str = str(poly_val) if poly_val else ""

                is_proxy = (
                    not poly_val_str
                    or "LATEST" in poly_val_str.upper()
                    or "GIZLENDI" in poly_val_str.upper()
                    or "HARITA" in poly_val_str.upper()
                    or len(poly_val_str) < 100
                )

                if is_proxy:
                    try:
                        latest = self.redis_client.get(route_key)
                        if latest:
                            if isinstance(latest, bytes):
                                latest = latest.decode('utf-8')
                            kwargs[poly_param] = latest
                            log.info(f"🔄 [Proxy] Sahte polyline algılandı, Redis'ten gerçeğiyle değiştirildi.")
                        else:
                            log.warning(f"⚠️ [Proxy] Redis '{route_key}' boş ama sahte polyline gönderildi!")
                    except Exception as redis_err:
                        log.warning(f"⚠️ [Proxy] Redis okunamadı: {redis_err}")

            mcp_args = {k: v for k, v in kwargs.items() if k != "session_id"}
            
            # V2.5: Redis Smart Cache — tekrarlı API çağrılarını önle
            cache_ttl = self.cache_ttls.get(name)
            cache_key = None
            if cache_ttl and self.redis_client:
                arg_hash = hashlib.md5(json.dumps(mcp_args, sort_keys=True).encode()).hexdigest()[:12]
                cache_key = f"tool_cache:{name}:{arg_hash}"
                cached = self.redis_client.get(cache_key)
                if cached:
                    log.info(f"💾 [Cache Hit] {name} | Key: {cache_key}")
                    try:
                        return json.loads(cached)
                    except:
                        return cached
            
            result = await self.mcp_rpc_call(service_name, "tools/call", {"name": name, "arguments": mcp_args})
            
            # Cache yaz (başarılı sonuç varsa)
            if cache_key and cache_ttl and self.redis_client:
                if isinstance(result, dict) and result.get("status") != "error":
                    try:
                        self.redis_client.setex(cache_key, cache_ttl, json.dumps(result, ensure_ascii=False))
                        log.info(f"💾 [Cache Write] {name} | TTL: {cache_ttl}s")
                    except: pass
            
            # Rota verisini cache'leme + otomatik geçmiş kaydı
            if name == "get_route_data" and isinstance(result, dict) and "error" not in result:
                poly = result.get("polyline") or result.get("polyline_encoded")
                if poly and self.redis_client:
                    self.redis_client.setex(route_key, 3600, poly)

                # Rota geçmişini arka planda DB'ye kaydet (non-blocking)
                try:
                    import asyncio
                    asyncio.create_task(ProfileManager.save_route_history(
                        origin=mcp_args.get("origin", "Bilinmiyor"),
                        destination=mcp_args.get("destination", "Bilinmiyor"),
                        distance_km=float(result.get("mesafe_km", 0)),
                        duration_min=float(result.get("sure_dk", 0)),
                    ))
                    log.info("📚 [RouteHistory] Rota geçmişe kaydediliyor (arka plan)...")
                except Exception as e:
                    log.warning(f"⚠️ [RouteHistory] Kayıt başlatılamadı: {e}")
                    
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

    async def sse_listener_loop(self, service_name: str, base_url: str, _backoff: int = 3):
        if not base_url.startswith("http"): base_url = f"http://{base_url}"
        log.info(f"🎧 [{service_name.upper()}] SSE Dinleme Başladı: {base_url}")
        
        async with httpx.AsyncClient(timeout=None) as client:
            try:
                async with client.stream("GET", base_url) as response:
                    _backoff = 3  # Başarılı bağlantıda backoff sıfırla
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
                                    "clientInfo": {"name": "Orchestrator", "version": "3.0"}
                                })
                                await self.register_agent_tools(service_name)

                            asyncio.create_task(initiate_service())
                            
            except Exception as e:
                next_backoff = min(_backoff * 2, 30)
                log.error(f"📡 [{service_name.upper()}] SSE Koptu: {e} — {_backoff}s sonra yeniden denenecek...")
                await asyncio.sleep(_backoff)
                # Store task reference to prevent garbage collection
                reconnect_task = asyncio.create_task(self.sse_listener_loop(service_name, base_url, next_backoff))
                reconnect_task.add_done_callback(lambda t: t.exception() if not t.cancelled() and t.exception() else None)

orchestrator = GeoIntelOrchestrator()
