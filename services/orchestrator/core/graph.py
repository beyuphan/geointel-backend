import json
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from logger import log
from prompt_manager import get_dynamic_system_prompt, classify_intent_fast
from core.models import IntentAnalysis, AgentState
from core.mcp_client import orchestrator
from core.result_compressor import compress_result

async def intent_node(state: AgentState):
    """V2.5: LLM çağrısı yapmadan, keyword regex ile sınıflandırma (500 token/istek tasarruf)."""
    msg = state["messages"][-1].content
    intent_data = classify_intent_fast(msg)
    log.success(f"🎯 [FastClassifier] Kategori: {intent_data['category'].upper()} | Karmaşıklık: {intent_data['complexity'].upper()}")
    return {"intent": intent_data}

from langgraph.graph import END

async def agent_node(state: AgentState):
    from profile_manager import ProfileManager
    # 1. Kullanıcı Profilini ve Rota Geçmişini Çek
    active_profile = await ProfileManager.get_user_context()
    route_history = await ProfileManager.get_route_history(limit=3)
    
    # Rota geçmişini intent'e enjekte et (Prompt Manager kullanacak)
    intent_with_history = state["intent"].copy()
    intent_with_history["route_history"] = route_history
    
    # 2. Dinamik Prompt'u Üret
    sys_prompt = get_dynamic_system_prompt(active_profile, intent_with_history)
    messages = [SystemMessage(content=sys_prompt)] + state["messages"]
    
    model = orchestrator.llm_claude if state["intent"].get("complexity") == "high" else orchestrator.llm_gemini
    
    m_name = getattr(model, "model_name", getattr(model, "model", "Unknown"))
    log.info(f"🧠 [LLM Router] Session: {state.get('session_id')[-8:]} | Model: {m_name} (Karmaşıklık: {state['intent'].get('complexity')})")
    
    model_with_tools = model.bind_tools(orchestrator.runtime_tools)
    response = await model_with_tools.ainvoke(messages)
    
    return {"messages": [response], "retry_count": 0}

def should_continue(state: AgentState) -> str:
    last_msg = state["messages"][-1]
    
    if hasattr(last_msg, "tool_calls") and len(last_msg.tool_calls) > 0:
        if state.get("retry_count", 0) >= 3:
            log.warning("⚠️ Max tool retry limit reached! Killing execution loop.")
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
