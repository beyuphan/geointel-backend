import json
import asyncio
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from logger import log
from prompt_manager import get_dynamic_system_prompt, classify_intent_fast
from core.models import IntentAnalysis, AgentState
from core.mcp_client import orchestrator
from core.result_compressor import compress_result

async def intent_node(state: AgentState):
    """V4.1: Hybrid Intent Router. 
    Basit işlerde Regex hızı, karmaşık işlerde LLM zekası."""
    messages = state["messages"]
    msg = messages[-1].content
    intent_data = classify_intent_fast(msg)
    
    # [Hafıza Entegrasyonu] Kısa veya genel mesajlarda geçmişe bak
    if (intent_data["category"] == "general" or len(msg.split()) < 3) and len(messages) > 1:
        prev_content = messages[-2].content if hasattr(messages[-2], "content") else ""
        prev_intent = classify_intent_fast(str(prev_content))
        if prev_intent["category"] != "general":
            log.info(f"🔄 [Intent Memory] Kategori miras alındı: {prev_intent['category'].upper()}")
            intent_data["category"] = prev_intent["category"]
            # Bağlam miras alındığında daima high — devam soruları genelde karmaşık
            intent_data["complexity"] = "high"
            intent_data["needs_deep_analysis"] = False  # LLM çağrısına gerek yok, zaten high

    # Regex'ten gelen ilk tahmini sakla
    category_from_regex = intent_data["category"]

    # [Model Güvenliği] Rota ve şehir verisi HER ZAMAN Claude'a — Gemini Flash başaramaz
    ALWAYS_CLAUDE_CATEGORIES = {"routing", "city_data", "places"}
    if intent_data["category"] in ALWAYS_CLAUDE_CATEGORIES:
        intent_data["complexity"] = "high"
        log.info(f"🔒 [Model Lock] Kategori {intent_data['category'].upper()} → Claude zorunlu.")

    if intent_data.get("needs_deep_analysis"):
        log.info(f"🧠 [DeepIntent] Karmaşık cümle algılandı, LLM ile analiz ediliyor...")
        model = orchestrator.llm_gemini
        prompt = f"""Kullanıcı mesajını analiz et ve JSON dön. Format:
        {{"category": "routing|fuel|pharmacy|event|city_data|places|general", "complexity": "high|low", "urgency": true|false, "focus_points": []}}
        
        Açıklamalar:
        - routing: Rota, yol tarifi, navigasyon, mesafe, seyahat süresi ile ilgili istekler.
        - fuel: Benzin, motorin, yakıt fiyatları, akaryakıt istasyonu aramaları.
        - pharmacy: Nöbetçi eczane ve ilaç aramaları.
        - event: Konser, festival, etkinlik, maç aramaları.
        - city_data: İBB WFS verileri, İSPARK, afet toplanma gibi şehir altyapı sorguları.
        - places: Kafe, restoran, mekan bulma veya yemek yeme ile ilgili istekler.
        - general: Hiçbir kategoriye uymayan düz muhabbet.
        
        Mesaj: {msg}"""
        
        try:
            if hasattr(model, "with_structured_output"):
                structured_model = model.with_structured_output(IntentAnalysis)
                intent_llm = await structured_model.ainvoke(prompt)
                intent_data = intent_llm.model_dump() if hasattr(intent_llm, "model_dump") else intent_llm
                log.success(f"🧠 [DeepIntent] LLM Kararı: {intent_data['category'].upper()}")
                
                # Eğer LLM "general" dediyse ama eski Regex "places" vb dediyse, Regex haklıdır
                if intent_data.get("category") == "general" and category_from_regex != "general":
                     log.info(f"🛡️ [DeepIntent] LLM general dediği için Regex'in '{category_from_regex}' niyetine dönüldü.")
                     intent_data["category"] = category_from_regex
                
                # LLM sonrası da kategori kilidi kontrol et
                if intent_data.get("category") in ALWAYS_CLAUDE_CATEGORIES:
                    intent_data["complexity"] = "high"
            else:
                response = await model.ainvoke(prompt)
                clean_content = response.content.replace("```json", "").replace("```", "").strip()
                intent_data = json.loads(clean_content)
        except Exception as e:
            log.warning(f"⚠️ [DeepIntent] LLM hatası, Regex'e dönülüyor: {e}")

    log.success(f"🎯 [Intent] Kategori: {intent_data['category'].upper()} | Karmaşıklık: {intent_data['complexity'].upper()}")
    return {"intent": intent_data}

from langgraph.graph import END

async def agent_node(state: AgentState):
    from profile_manager import ProfileManager
    
    # Defensive: Profile loading failures must not crash the LLM pipeline
    try:
        active_profile = await ProfileManager.get_user_context()
    except Exception as profile_err:
        log.warning(f"⚠️ [agent_node] Profil yüklenemedi: {profile_err}")
        active_profile = "Profil verisi alınamadı."
    
    try:
        route_history = await ProfileManager.get_route_history(limit=3)
    except Exception as hist_err:
        log.warning(f"⚠️ [agent_node] Rota geçmişi alınamadı: {hist_err}")
        route_history = []

    # Active route check — safe with or without Redis
    session_id = state.get("session_id", "default_session")
    has_active_route = False
    if orchestrator.redis_client:
        try:
            active_route_encoded = orchestrator.redis_client.get(f"route:{session_id}")
            has_active_route = bool(active_route_encoded)
        except Exception as redis_err:
            log.warning(f"⚠️ [agent_node] Redis get failed: {redis_err}")

    intent_with_history = state["intent"].copy()
    intent_with_history["route_history"] = route_history
    intent_with_history["has_active_route"] = has_active_route

    sys_prompt = get_dynamic_system_prompt(active_profile, intent_with_history)
    if has_active_route:
        sys_prompt += (
            "\n[SYSTEM: User has an active route on the map. "
            "For nearby POI/fuel requests, use LATEST polyline via tools.]"
        )

    messages = [SystemMessage(content=sys_prompt)] + state["messages"]

    is_high = state["intent"].get("complexity") == "high"
    primary_model  = orchestrator.llm_claude if is_high else orchestrator.llm_gemini
    fallback_model = orchestrator.llm_gemini

    m_name = getattr(primary_model, "model_name", getattr(primary_model, "model", "Unknown"))
    log.info(
        f"🧠 [LLM Router] Session: {session_id[-8:]} | "
        f"Model: {m_name} (complexity: {state['intent'].get('complexity')})"
    )

    # ── Retry + Gemini Fallback ────────────────────────────────────────────
    # Claude 529 (Overloaded) → exponential backoff → Gemini fallback
    MAX_RETRIES = 3
    RETRY_DELAYS = [1.0, 3.0, 8.0]   # saniye
    last_exc = None

    for attempt in range(MAX_RETRIES + 1):      # 0,1,2 = retry; 3 = fallback
        use_model = primary_model if attempt < MAX_RETRIES else fallback_model

        if attempt == MAX_RETRIES:
            fb_name = getattr(fallback_model, "model_name",
                              getattr(fallback_model, "model", "Gemini"))
            log.warning(
                f"⚡ [Fallback] Claude {MAX_RETRIES} denemede yanıt vermedi → {fb_name} devreye girdi."
            )

        try:
            model_with_tools = use_model.bind_tools(orchestrator.runtime_tools)
            response = await model_with_tools.ainvoke(messages)
            if attempt > 0:
                log.success(f"✅ [Retry] Deneme {attempt + 1}'de yanıt alındı.")
            return {"messages": [response], "retry_count": 0}

        except Exception as exc:
            last_exc = exc
            exc_str = str(exc)
            is_retryable = (
                "529" in exc_str
                or "overloaded" in exc_str.lower()
                or "503" in exc_str
                or "502" in exc_str
                or "rate_limit" in exc_str.lower()
            )
            if is_retryable and attempt < MAX_RETRIES:
                delay = RETRY_DELAYS[attempt]
                log.warning(
                    f"⏳ [Retry {attempt + 1}/{MAX_RETRIES}] "
                    f"Claude aşırı yüklenmiş (529). {delay}s bekleyip tekrar deneniyor..."
                )
                await asyncio.sleep(delay)
                continue
            log.error(f"🔥 [LLM] Model yanıt vermedi (deneme {attempt + 1}): {exc_str[:150]}")
            raise
    # ────────────────────────────────────────────────────────────────
    raise RuntimeError(f"[agent_node] Tüm denemeler başarısız: {last_exc}")

def should_continue(state: AgentState) -> str:
    """
    Sadece 2 yol var:
      - Tool çağrısı varsa → 'tools' node'una git
      - Tool çağrısı yoksa → END (sonsuz loop yok)
    Retry guard: 3 turdan fazla tool döngüsü olursa zorla bitir.
    """
    last_msg = state["messages"][-1]

    has_tool_calls = hasattr(last_msg, "tool_calls") and len(last_msg.tool_calls) > 0

    if has_tool_calls:
        if state.get("retry_count", 0) >= 3:
            log.warning("⚠️ Max tool retry (3) aşıldı! Döngü sonlandırılıyor.")
            return END
        return "tools"

    return END

async def custom_tool_node(state: AgentState):
    last_msg = state["messages"][-1]
    msgs = []
    visual_data = state.get("visual_data", {"markers": [], "polyline": None, "geojson_layers": []})
    session_id = state.get("session_id", "default_session")
    route_key = f"route:{session_id}"
    
    for tc in getattr(last_msg, "tool_calls", []):
        t_name = tc["name"]
        args = tc.get("args", {})
        
        log.info(f"🛠️ [Node: Tools] Çağrılıyor: {t_name}")
        
        tool = orchestrator.get_tool_by_name(t_name)
        if not tool:
            msgs.append(ToolMessage(content=f"Error: Tool {t_name} not found.", tool_call_id=tc["id"]))
            continue
            
        try:
            # Otomatik Session Enjeksiyonu
            args["session_id"] = session_id
            res = await tool.ainvoke(args)
        except Exception as e:
            res = f"Tool Error: {str(e)}"
            log.error(f"🔥 [Node: Tools] Hata ({t_name}): {e}")

        # VİZE: Gelen Veriyi Frontend (Harita) İçin Yakalama
        if isinstance(res, dict):
            # Eğer API'den hata döndüyse LLM'e haber ver ama sistemi çökertme
            if res.get("status") == "error":
                msgs.append(ToolMessage(content=json.dumps(res, ensure_ascii=False), tool_call_id=tc["id"]))
                continue

            # Rota Çizgisi Varsa (Polyline)
            if "polyline" in res or "polyline_encoded" in res:
                poly_str = res.get("polyline") or res.get("polyline_encoded")
                
                # Broad proxy check for the return value itself
                is_proxy_return = (
                    not poly_str or
                    "LATEST" in poly_str.upper() or
                    "GİZLENDİ" in poly_str.upper() or
                    "HARİTA" in poly_str.upper()
                )
                
                if poly_str and not is_proxy_return:
                    visual_data["polyline"] = poly_str
                    if orchestrator.redis_client and isinstance(poly_str, str) and len(poly_str) > 100:
                        try:
                            orchestrator.redis_client.setex(route_key, 3600, poly_str)
                        except Exception as e:
                            log.warning(f"[Visualizer] Redis setex failed: {e}")
                elif poly_str and is_proxy_return:
                    latest = None
                    if orchestrator.redis_client:
                        try:
                            latest = orchestrator.redis_client.get(route_key)
                        except Exception as e:
                            log.warning(f"[Visualizer] Redis get failed: {e}")
                    if latest:
                        visual_data["polyline"] = latest
                        log.info("🔄 [Visualizer] Gizlenmiş polyline yakalandı, Redis'ten haritaya aktarıldı.")
                    else:
                        visual_data["polyline"] = poly_str
                        log.warning(f"⚠️ [Visualizer] LATEST algılandı ama Redis '{route_key}' boş!")
                else:
                    visual_data["polyline"] = poly_str
                
                # LLM'in kafası devasa veriyle karışmasın diye polyline metnini tamamen gizliyoruz!
                if "polyline" in res: 
                    res["polyline"] = "[HARİTAYA ÇİZİLDİ - OKUMANA GEREK YOK]"
                if "polyline_encoded" in res: 
                    res["polyline_encoded"] = "[HARİTAYA ÇİZİLDİ - OKUMANA GEREK YOK]"
                
                if "alternatives" in res and isinstance(res["alternatives"], list):
                    for alt in res["alternatives"]:
                        if "polyline_encoded" in alt:
                            alt["polyline_encoded"] = "[GİZLENDİ]"
                        if "polyline" in alt:
                            alt["polyline"] = "[GİZLENDİ]"
            
            # Tek bir hedef verildiyse marker ekle
            if "lat" in res and "lon" in res: 
                visual_data["markers"].append({"name": "Hedef", "lat": res["lat"], "lon": res["lon"]})
                
            # Eğer çoklu sonuç döndüyse (örn. places), marker listesine ekle
            if "places" in res and isinstance(res["places"], list):
                for place in res["places"]:
                    if "lat" in place and "lon" in place:
                        visual_data["markers"].append({
                            "name": place.get("name", "Bilinmeyen"),
                            "lat": place["lat"],
                            "lon": place["lon"]
                        })

        tool_output = res
        if isinstance(res, list):
            tool_output = {"results": res}
        elif not isinstance(res, dict):
            tool_output = {"result": str(res)}
        
        # V2.5: Sonucu sıkıştır — LLM'e giden token miktarını %40-60 azaltır
        tool_output = compress_result(t_name, tool_output)
            
        msgs.append(ToolMessage(content=json.dumps(tool_output, ensure_ascii=False), tool_call_id=tc["id"]))
        
    return {"messages": msgs, "visual_data": visual_data, "retry_count": state.get("retry_count", 0) + 1}
