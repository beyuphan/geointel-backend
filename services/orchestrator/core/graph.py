import json
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

    # [Model Güvenliği] Rota ve şehir verisi HER ZAMAN Claude'a — Gemini Flash başaramaz
    ALWAYS_CLAUDE_CATEGORIES = {"routing", "city_data"}
    if intent_data["category"] in ALWAYS_CLAUDE_CATEGORIES:
        intent_data["complexity"] = "high"
        log.info(f"🔒 [Model Lock] Kategori {intent_data['category'].upper()} → Claude zorunlu.")

    if intent_data.get("needs_deep_analysis"):
        log.info(f"🧠 [DeepIntent] Karmaşık cümle algılandı, LLM ile analiz ediliyor...")
        model = orchestrator.llm_gemini
        prompt = f"""Kullanıcı mesajını analiz et ve şu formatta JSON dön:
        {{"category": "routing|fuel|pharmacy|event|city_data|general", "complexity": "high|low", "urgency": true|false, "focus_points": []}}
        
        Mesaj: {msg}"""
        
        try:
            if hasattr(model, "with_structured_output"):
                structured_model = model.with_structured_output(IntentAnalysis)
                intent_llm = await structured_model.ainvoke(prompt)
                intent_data = intent_llm.model_dump() if hasattr(intent_llm, "model_dump") else intent_llm
                log.success(f"🧠 [DeepIntent] LLM Kararı: {intent_data['category'].upper()}")
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
    # 1. Kullanıcı Profilini ve Rota Geçmişini Çek
    active_profile = await ProfileManager.get_user_context()
    route_history = await ProfileManager.get_route_history(limit=3)
    
    # AKTİF ROTA KONTROLÜ (Bunu yeni ekliyoruz)
    session_id = state.get("session_id", "default_session")
    active_route_encoded = orchestrator.redis_client.get(f"route:{session_id}")
    has_active_route = True if active_route_encoded else False
    
    # Rota geçmişini ve aktif rota durumunu intent'e enjekte et
    intent_with_history = state["intent"].copy()
    intent_with_history["route_history"] = route_history
    intent_with_history["has_active_route"] = has_active_route # Gemini'nin bilmesi için
    
    # 2. Dinamik Prompt'u Üret (Prompt Manager bu veriyi kullanacak)
    sys_prompt = get_dynamic_system_prompt(active_profile, intent_with_history)
    
    if has_active_route:
        # Prompt'un sonuna küçük bir fısıltı ekleyelim
        sys_prompt += "\n[SİSTEM BİLGİSİ: Kullanıcının şu anda haritada çizilmiş aktif bir rotası (polyline) bulunmaktadır. Eğer yol üstü mekan önerisi vs. isterse doğrudan tool'ları kullanarak veya sohbet geçmişindeki şehirlere bakarak yanıt ver.]"

    messages = [SystemMessage(content=sys_prompt)] + state["messages"]
    
    model = orchestrator.llm_claude if state["intent"].get("complexity") == "high" else orchestrator.llm_gemini
    
    m_name = getattr(model, "model_name", getattr(model, "model", "Unknown"))
    log.info(f"🧠 [LLM Router] Session: {state.get('session_id')[-8:]} | Model: {m_name} (Karmaşıklık: {state['intent'].get('complexity')})")
    
    model_with_tools = model.bind_tools(orchestrator.runtime_tools)
    response = await model_with_tools.ainvoke(messages)
    
    return {"messages": [response], "retry_count": 0}

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
                     if isinstance(poly_str, str) and len(poly_str) > 100:
                         orchestrator.redis_client.setex(route_key, 3600, poly_str)
                elif poly_str and is_proxy_return:
                    latest = orchestrator.redis_client.get(route_key)
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
