import json
import asyncio
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from logger import log
from prompt_manager import get_dynamic_system_prompt, classify_intent_fast
from core.models import IntentAnalysis, AgentState
from core.mcp_client import orchestrator
from core.result_compressor import compress_result
from profile_manager import ProfileManager

async def intent_node(state: AgentState):
    """V4.2: Hybrid Intent Router. 
    Basit işlerde Regex hızı, karmaşık işlerde LLM zekası."""
    # Her yeni istekte harita verilerini sıfırla (Eski pinler temizlensin)
    state["visual_data"] = {"markers": [], "polyline": None, "geojson_layers": []}
    state["route_polyline"] = None

    # Routing phase tespiti: kullanıcı cevabı mı, yoksa ilk istek mi?
    # Phase 1: İlk rota isteği  |  Phase 2: POI önerisi (yemek/mola/yakıt sorusu)
    # Phase 3: Seçim yapıldı (kart tıklandı / numara söylendi)  |  Phase 4: Final onay (radar+hava+özet)
    messages = state["messages"]
    current_phase = state.get("routing_phase", 1)
    if orchestrator.redis_client:
        try:
            has_route = orchestrator.redis_client.exists(f"route:{state.get('session_id', 'default')}")
            if has_route:
                last_msg = messages[-1].content if messages else ""
                last_lower = last_msg.lower()

                # Phase 4: Kullanıcı final onayı verdi (radar+hava+özet tetiklenecek)
                phase4_triggers = ["hazırım", "gidelim", "tamam gidelim", "başlat", "navigasyon başlat",
                                    "radar ekle", "hava ekle", "evet hazırım", "her şey tamam"]
                if any(t in last_lower for t in phase4_triggers):
                    current_phase = 4

                # Phase 3: Kullanıcı kart seçti veya durak belirledi
                elif any(t in last_lower for t in ["rotama ekle", "koordinatlar:", "seçiyorum",
                                                    "oraya gidelim", "onu seç", "tamam orası",
                                                    "1.", "2.", "3.", "onu istiyorum", "o olsun"]):
                    current_phase = 3

                # Phase 2: Kullanıcı yemek/yakıt/mola cevabı verdi
                elif any(t in last_lower for t in ["evet", "yemek", "restoran", "kafe", "yiyeceğim",
                                                     "acıktım", "yakıt", "benzin", "mola", "dur",
                                                     "evet dur", "hangisini", "öner", "nereden",
                                                     "bir şey ye", "duralım", "molalı"]):
                    current_phase = 2
        except:
            pass
    
    msg = messages[-1].content
    intent_data = classify_intent_fast(msg)
    intent_data["routing_phase"] = current_phase
    
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

    # [Hız Optimizasyonu] Eğer Regex net bir kategori bulduysa LLM'e sorma (Lansman hızı için)
    if intent_data["category"] != "general":
        intent_data["needs_deep_analysis"] = False
        log.info(f"⚡ [Intent Speed] Regex '{intent_data['category']}' yakaladı, LLM analizi atlandı.")

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

    log.success(f"🎯 [Intent] Kategori: {intent_data['category'].upper()} | Karmaşıklık: {intent_data['complexity'].upper()} | Rota Aşaması: Phase {current_phase}")
    return {"intent": intent_data, "routing_phase": current_phase}

from langgraph.graph import END

async def agent_node(state: AgentState):
    """V6.2: Clean & Fast Agent Node."""
    session_id = state.get("session_id", "default_session")

    # Active route check — safe with or without Redis
    session_id = state.get("session_id", "default_session")
    
    # Sistem Promptu Hazırla
    user_ctx = await ProfileManager.get_combined_context(session_id)
    sys_prompt = get_dynamic_system_prompt(user_ctx, state["intent"])
    
    # Rota varsa prompt'a ek bilgi enjekte et
    has_active_route = orchestrator.redis_client and orchestrator.redis_client.exists(f"route:{session_id}")
    if has_active_route:
        sys_prompt += "\n[SYSTEM: Kullanıcının aktif bir rotası var. 'LATEST' polyline kullanılabilir.]"

    messages = [SystemMessage(content=sys_prompt)] + state["messages"]

    # MODEL SEÇİMİ
    primary_model  = orchestrator.llm_claude
    fallback_model = orchestrator.llm_gemini

    # RETRY VE MODEL SEÇİMİ
    MAX_RETRIES = 2
    RETRY_DELAYS = [0.1, 0.5]
    last_exc = None

    # [Hız Optimizasyonu] Eğer bu bir özet aşamasıysa (tool çağrısından dönüldüyse)
    # veya kategori basitse Gemini kullan. Claude'u sadece ilk analizde/zor işlerde kullan.
    is_summary_step = state.get("retry_count", 0) > 0
    use_fast_model = is_summary_step or state["intent"].get("complexity") != "high"

    for attempt in range(MAX_RETRIES + 1):
        if use_fast_model:
            use_model = fallback_model
        else:
            use_model = primary_model if attempt == 0 else fallback_model
        
        try:
            log.info(f"🧠 [Agent] Çağrılıyor: {getattr(use_model, 'model', 'Claude')} (Deneme {attempt+1})")
            model_with_tools = use_model.bind_tools(orchestrator.runtime_tools)
            response = await model_with_tools.ainvoke(messages)
            return {"messages": [response], "retry_count": 0}
            
        except Exception as exc:
            last_exc = exc
            exc_str = str(exc).lower()
            
            # Kota veya erişim hatası varsa beklemeden fallback yap
            is_immediate_fallback = any(k in exc_str for k in ["429", "resource_exhausted", "403", "quota"])
            if is_immediate_fallback and attempt < MAX_RETRIES:
                log.warning(f"⚡ [Model Alert] Hızlı fallback tetiklendi: {exc_str[:50]}")
                continue

            if attempt < MAX_RETRIES:
                delay = RETRY_DELAYS[attempt]
                log.warning(f"⏳ [Retry] Hata: {exc_str[:50]}... {delay}s sonra tekrar...")
                await asyncio.sleep(delay)
                continue
            
            log.error(f"🔥 [Agent Critical] Başarısız: {exc_str[:150]}")
            raise

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
    visual_data = state.get("visual_data", {"markers": [], "polyline": None, "geojson_layers": []})
    session_id = state.get("session_id", "default_session")
    route_key = f"route:{session_id}"
    
    tool_calls = getattr(last_msg, "tool_calls", [])
    if not tool_calls:
        return {"messages": [], "retry_count": state.get("retry_count", 0)}

    # PARALEL ÇALIŞTIRMA BAŞLAT
    async def _execute_tool(tc):
        t_name = tc["name"]
        args = tc.get("args", {}).copy()
        args["session_id"] = session_id
        
        log.info(f"🛠️ [Parallel Tool] Başlatıldı: {t_name}")
        tool = orchestrator.get_tool_by_name(t_name)
        if not tool:
            return ToolMessage(content=f"Error: Tool {t_name} not found.", tool_call_id=tc["id"]), None

        try:
            # Kullanıcı uzun beklemeye razı, limiti 5 dakikaya (300s) çıkarıyoruz
            res = await asyncio.wait_for(tool.ainvoke(args), timeout=300.0)
        except asyncio.TimeoutError:
            res = {"status": "error", "message": f"Araç ({t_name}) 5 dakika içinde yanıt vermedi."}
            log.error(f"⏳ [Timeout] {t_name} 5 dakikayı geçti, zorunlu iptal.")
        except Exception as e:
            res = f"Tool Error: {str(e)}"
            log.error(f"🔥 [Parallel Tool] Hata ({t_name}): {e}")

        # Görsel verileri burada yakalayıp asıl ToolMessage ile birlikte döndür
        local_visual = {"markers": [], "polyline": None}
        
        # MCP tool'ları JSON string döndürüyor — parse et
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except (json.JSONDecodeError, TypeError):
                pass  # Parse edilemiyorsa string kalır
        
        if isinstance(res, dict):
            if res.get("status") == "error":
                return ToolMessage(content=json.dumps(res, ensure_ascii=False), tool_call_id=tc["id"]), None

            # Rota Polyline Yakalama
            if "polyline" in res or "polyline_encoded" in res:
                poly_str = res.get("polyline") or res.get("polyline_encoded")
                is_proxy = not poly_str or any(k in str(poly_str).upper() for k in ["LATEST", "GIZLENDI", "HARITA"]) or len(str(poly_str)) < 100
                
                if poly_str and not is_proxy:
                    local_visual["polyline"] = poly_str
                    if orchestrator.redis_client:
                        try: orchestrator.redis_client.setex(route_key, 3600, poly_str)
                        except: pass
                elif is_proxy and orchestrator.redis_client:
                    latest = orchestrator.redis_client.get(route_key)
                    if latest: local_visual["polyline"] = latest.decode('utf-8') if isinstance(latest, bytes) else latest

                # LLM Token Tasarrufu: Polyline'ı gizle
                if "polyline" in res: res["polyline"] = "[HARİTAYA ÇİZİLDİ]"
                if "polyline_encoded" in res: res["polyline_encoded"] = "[HARİTAYA ÇİZİLDİ]"

            # Marker Yakalama — SADECE mekan/POI araçlarından gelen pinleri ekle
            # Rota araçları (get_route_data, evaluate_route_strategy) marker oluşturmamalı
            POI_TOOLS = {"search_hybrid_places", "get_pharmacies", "get_events",
                         "search_web_intel", "get_sports_matches", "find_ev_charging"}
            if t_name in POI_TOOLS:
                if "lat" in res and "lon" in res:
                    local_visual["markers"].append({
                        "name": res.get("name", "Nokta"),
                        "lat": res["lat"], "lon": res["lon"],
                        "type": "poi"
                    })
                # search_hybrid_places: strict + relaxed listelerini işle
                if "strict_route_places" in res or "relaxed_route_places" in res:
                    all_places = res.get("strict_route_places", []) + res.get("relaxed_route_places", [])
                    for p in all_places:
                        if "lat" in p and "lon" in p:
                            local_visual["markers"].append({
                                # Temel alanlar
                                "name": p.get("name", "Mekan"),
                                "title": p.get("name", "Mekan"),
                                "lat": p["lat"], "lon": p["lon"],
                                "type": "fuel_station" if any(
                                    k in (p.get("name", "") + str(res)).lower()
                                    for k in ["benzin", "shell", "opet", "bp", "petrol", "total", "akaryak"]
                                ) else "poi",
                                "snippet": p.get("address", ""),
                                # ★ Zengin POI kart alanları (tamamini aktar)
                                "on_route_side": p.get("on_route_side", "unknown"),
                                "opening_hours": p.get("opening_hours", []),
                                "open_now": p.get("open_now"),
                                "eta": p.get("eta"),
                                "deviation_meters": p.get("deviation_meters", 0),
                                "distance_along_route_km": p.get("distance_along_route_km"),
                                "rating": p.get("rating"),
                                "review_count": p.get("review_count"),
                                "price_level": p.get("price_level"),
                                "phone": p.get("phone"),
                                "address": p.get("address", ""),
                            })
                elif "places" in res and isinstance(res["places"], list):
                    # Eski format (list of places)
                    for p in res["places"]:
                        if "lat" in p and "lon" in p:
                            local_visual["markers"].append({
                                "name": p.get("name", "Mekan"),
                                "title": p.get("name", "Mekan"),
                                "lat": p["lat"], "lon": p["lon"],
                                "type": "poi",
                                "snippet": p.get("address") or p.get("description") or "",
                                "on_route_side": p.get("on_route_side", "unknown"),
                                "opening_hours": p.get("opening_hours", []),
                                "open_now": p.get("open_now"),
                                "rating": p.get("rating"),
                                "deviation_meters": p.get("deviation_meters", 0),
                            })
                # Eczane için type düzenle
                if t_name == "get_pharmacies":
                    for m in local_visual["markers"]:
                        m["type"] = "pharmacy"
                            
            # 🔥 YENİ: get_route_data çağrılırken waypoint varsa onları haritaya PIN olarak ekle
            if t_name == "get_route_data" and args.get("waypoints"):
                wps = args["waypoints"].split("|")
                for i, w in enumerate(wps):
                    w = w.strip()
                    if "," in w:
                        try:
                            lat, lon = map(float, w.split(","))
                            local_visual["markers"].append({
                                "name": f"Durak {i+1}",
                                "lat": lat, "lon": lon,
                                "type": "poi",
                                "snippet": "Seçilen Ara Durak"
                            })
                            log.info(f"📍 [Waypoint Marker] Haritaya eklendi: {lat},{lon}")
                        except: pass


        # Sıkıştırma ve Paketleme
        tool_output = compress_result(t_name, res)
        return ToolMessage(content=json.dumps(tool_output, ensure_ascii=False), tool_call_id=tc["id"]), local_visual

    # Tüm araçları aynı anda çalıştır
    results = await asyncio.gather(*[_execute_tool(tc) for tc in tool_calls])
    
    msgs = []
    for msg, l_visual in results:
        msgs.append(msg)
        if l_visual:
            if l_visual.get("polyline"): visual_data["polyline"] = l_visual["polyline"]
            visual_data["markers"].extend(l_visual["markers"])

    return {"messages": msgs, "visual_data": visual_data, "retry_count": state.get("retry_count", 0) + 1}
