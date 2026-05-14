"""
core/graph.py — v7.0 (Temiz Yeniden Yazım)

LangGraph iş akışı. 3 Node:
  1. classifier  → intent belirle
  2. agent       → LLM + tool çağrısı
  3. tools       → MCP araç çalıştırma

Faz sistemi KALDIRILDI. Action card mantığı buraya taşınmadı — routes.py'de.
LLM'in tek görevi: doğru tool'u çağır, 1-2 cümle açıklama yaz.
"""
import json
import asyncio
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from langgraph.graph import END
from logger import log
from prompt_manager import get_dynamic_system_prompt, classify_intent_fast
from core.models import AgentState
from core.mcp_client import orchestrator
from core.result_compressor import compress_result
from profile_manager import ProfileManager


# ─────────────────────────────────────────────────────────────────────────────
# 1. CLASSIFIER NODE
# ─────────────────────────────────────────────────────────────────────────────

async def intent_node(state: AgentState):
    """Intent sınıflandırma — sadece keyword tabanlı, hızlı."""
    # Her yeni istekte görsel veriyi sıfırla
    state["visual_data"] = {"markers": [], "polyline": None, "geojson_layers": []}
    state["route_polyline"] = None

    messages = state["messages"]
    last_msg = messages[-1].content if messages else ""

    intent = classify_intent_fast(last_msg)

    log.success(
        f"🎯 [Intent] {intent['category'].upper()} | "
        f"complexity={intent['complexity']} | urgency={intent['urgency']}"
    )
    return {"intent": intent}


# ─────────────────────────────────────────────────────────────────────────────
# 2. AGENT NODE
# ─────────────────────────────────────────────────────────────────────────────

async def agent_node(state: AgentState):
    """LLM çağrısı. Claude primary, Gemini fallback."""
    session_id = state.get("session_id", "default_session")

    # Aktif rota varsa bunu LLM'e bildir
    has_active_route = (
        orchestrator.redis_client
        and orchestrator.redis_client.exists(f"route:{session_id}")
    )

    # Trip context (yapılandırılmış yolculuk — /trip/plan endpoint'inden gelir)
    trip_context = None
    if orchestrator.redis_client:
        raw_tc = orchestrator.redis_client.get(f"trip_ctx:{session_id}")
        if raw_tc:
            import json as _json
            try:
                trip_context = _json.loads(
                    raw_tc if isinstance(raw_tc, str) else raw_tc.decode("utf-8")
                )
            except Exception:
                pass

    # System prompt oluştur
    user_ctx = await ProfileManager.get_combined_context(session_id)
    sys_prompt = get_dynamic_system_prompt(user_ctx, state["intent"], trip_context=trip_context)
    if has_active_route:
        sys_prompt += (
            "\n\n[SİSTEM NOTU: Kullanıcının aktif bir rotası var. "
            "Rota gerektiren tool çağrılarında polyline='LATEST' kullan, "
            "sistem otomatik Redis'ten gerçek polyline'ı enjekte edecek.]"
        )

    messages = [SystemMessage(content=sys_prompt)] + state["messages"]

    # Model seçimi: routing/day_plan → Claude, diğerleri → Gemini (hız)
    intent = state.get("intent", {})
    use_claude = intent.get("complexity") == "high"

    primary_model  = orchestrator.llm_claude if use_claude else orchestrator.llm_gemini
    fallback_model = orchestrator.llm_gemini if use_claude else orchestrator.llm_claude

    for attempt in range(3):
        model = primary_model if attempt == 0 else fallback_model
        try:
            log.info(f"🧠 [Agent] Model={getattr(model, 'model', '?')} deneme={attempt+1}")
            response = await model.bind_tools(orchestrator.runtime_tools).ainvoke(messages)
            return {"messages": [response], "retry_count": 0}
        except Exception as exc:
            exc_str = str(exc).lower()
            is_quota = any(k in exc_str for k in ["429", "quota", "resource_exhausted", "403"])
            if is_quota or attempt < 2:
                delay = 0 if is_quota else [0.2, 0.5][attempt]
                if delay:
                    await asyncio.sleep(delay)
                log.warning(f"⚡ [Agent] Fallback: {exc_str[:60]}")
                continue
            log.error(f"🔥 [Agent] Başarısız: {exc_str[:150]}")
            raise

    raise RuntimeError("[agent_node] Tüm denemeler başarısız")


# ─────────────────────────────────────────────────────────────────────────────
# 3. TOOL NODE
# ─────────────────────────────────────────────────────────────────────────────

async def custom_tool_node(state: AgentState):
    """MCP araçlarını paralel çalıştırır, görsel veriyi yakalar."""
    last_msg  = state["messages"][-1]
    session_id = state.get("session_id", "default_session")
    route_key  = f"route:{session_id}"
    visual_data = state.get(
        "visual_data", {"markers": [], "polyline": None, "geojson_layers": []}
    )

    tool_calls = getattr(last_msg, "tool_calls", [])
    if not tool_calls:
        return {"messages": [], "retry_count": state.get("retry_count", 0)}

    async def _run_tool(tc):
        t_name = tc["name"]
        args   = dict(tc.get("args", {}))
        args["session_id"] = session_id

        # LATEST polyline substitution
        if orchestrator.redis_client:
            for k, v in list(args.items()):
                if isinstance(v, str) and v.upper() in ("LATEST", "[HARITAYA ÇİZİLDİ]"):
                    raw = orchestrator.redis_client.get(route_key)
                    if raw:
                        args[k] = raw if isinstance(raw, str) else raw.decode("utf-8")
                        log.info(f"🔄 [PolySubst] {k} → Redis polyline enjekte edildi")
                    else:
                        log.warning(f"⚠️ [PolySubst] {k}=LATEST ama Redis boş ({t_name})")

        log.info(f"🛠️ [Tool] Başlatıldı: {t_name}")
        tool = orchestrator.get_tool_by_name(t_name)
        if not tool:
            return (
                ToolMessage(content=f"Hata: {t_name} bulunamadı", tool_call_id=tc["id"]),
                None,
            )

        try:
            res = await asyncio.wait_for(tool.ainvoke(args), timeout=300.0)
        except asyncio.TimeoutError:
            res = {"status": "error", "message": f"{t_name} 5 dakikada yanıt vermedi"}
            log.error(f"⏳ [Timeout] {t_name}")
        except Exception as e:
            res = {"status": "error", "message": str(e)}
            log.error(f"🔥 [Tool Error] {t_name}: {e}")

        # JSON parse
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except Exception:
                pass

        local_visual = {"markers": [], "polyline": None}

        if isinstance(res, dict):
            if res.get("status") == "error":
                return (
                    ToolMessage(
                        content=json.dumps(res, ensure_ascii=False),
                        tool_call_id=tc["id"],
                    ),
                    None,
                )

            # ── Polyline yakalama ─────────────────────────────────────────
            poly = res.get("polyline") or res.get("polyline_encoded")
            if poly and len(str(poly)) > 50:
                is_proxy = any(
                    k in str(poly).upper()
                    for k in ["LATEST", "GİZLENDİ", "HARİTAYA", "[HARITA"]
                )
                if not is_proxy:
                    local_visual["polyline"] = poly
                    if orchestrator.redis_client:
                        try:
                            orchestrator.redis_client.setex(route_key, 3600, poly)
                        except Exception:
                            pass

            # Polyline gizle (LLM'e token tasarrufu)
            for pk in ("polyline", "polyline_encoded"):
                if pk in res:
                    res[pk] = "[HARİTAYA ÇİZİLDİ]"

            # ── Marker yakalama (sadece POI araçlarından) ─────────────────
            POI_TOOLS = {
                "search_hybrid_places", "get_pharmacies", "get_events",
                "search_web_intel", "get_sports_matches", "find_ev_charging",
                "plan_weather_aware_route", "evaluate_route_strategy"
            }
            if t_name in POI_TOOLS:
                # Tek POI
                if "lat" in res and "lon" in res:
                    local_visual["markers"].append(_build_marker(res))

                # Listeler (strict/relaxed veya places)
                all_places = (
                    res.get("strict_route_places", [])
                    + res.get("relaxed_route_places", [])
                    + res.get("places", [])
                )
                for p in all_places:
                    if "lat" in p and "lon" in p:
                        m = _build_marker(p)
                        if t_name == "get_pharmacies":
                            m["type"] = "pharmacy"
                        elif _is_fuel_place(p, res):
                            m["type"] = "fuel_station"
                        local_visual["markers"].append(m)

            # Waypoint marker'ları (rota güncellendiğinde)
            if t_name == "get_route_data":
                local_visual["distance_km"] = res.get("distance_km")
                local_visual["duration_min"] = res.get("duration_min")
                if args.get("waypoints"):
                    for i, wp in enumerate(str(args["waypoints"]).split("|")):
                        wp = wp.strip()
                        if "," in wp:
                            try:
                                lat, lon = map(float, wp.split(","))
                                local_visual["markers"].append({
                                    "name": f"Durak {i+1}",
                                    "lat": lat, "lon": lon,
                                    "type": "waypoint",
                                    "snippet": "Ara Durak",
                                })
                            except Exception:
                                pass

        output = compress_result(t_name, res)
        return (
            ToolMessage(
                content=json.dumps(output, ensure_ascii=False),
                tool_call_id=tc["id"],
            ),
            local_visual,
        )

    results = await asyncio.gather(*[_run_tool(tc) for tc in tool_calls])

    msgs = []
    for msg, lv in results:
        msgs.append(msg)
        if lv:
            if lv.get("polyline"):
                visual_data["polyline"] = lv["polyline"]
            if lv.get("distance_km"):
                visual_data["distance_km"] = lv["distance_km"]
            if lv.get("duration_min"):
                visual_data["duration_min"] = lv["duration_min"]
            visual_data["markers"].extend(lv["markers"])

    return {
        "messages": msgs,
        "visual_data": visual_data,
        "retry_count": state.get("retry_count", 0) + 1,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ROUTING LOGIC
# ─────────────────────────────────────────────────────────────────────────────

def should_continue(state: AgentState) -> str:
    """Tool çağrısı varsa → tools, yoksa → END."""
    last_msg = state["messages"][-1]
    has_tool_calls = bool(getattr(last_msg, "tool_calls", None))

    if has_tool_calls:
        if state.get("retry_count", 0) >= 4:
            log.warning("⚠️ [MaxRetry] 4 tool döngüsü aşıldı, durduruluyor.")
            return END
        return "tools"

    return END


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _build_marker(p: dict) -> dict:
    return {
        "name":                    p.get("name", "Mekan"),
        "title":                   p.get("name", "Mekan"),
        "lat":                     p["lat"],
        "lon":                     p["lon"],
        "type":                    p.get("type", "poi"),
        "snippet":                 p.get("address", p.get("description", "")),
        "address":                 p.get("address", ""),
        "on_route_side":           p.get("on_route_side", "unknown"),
        "opening_hours":           p.get("opening_hours", []),
        "open_now":                p.get("open_now"),
        "eta":                     p.get("eta"),
        "deviation_meters":        p.get("deviation_meters", 0),
        "distance_along_route_km": p.get("distance_along_route_km"),
        "rating":                  p.get("rating"),
        "review_count":            p.get("review_count"),
        "price_level":             p.get("price_level"),
        "phone":                   p.get("phone"),
    }


def _is_fuel_place(p: dict, res: dict) -> bool:
    fuel_brands = {"benzin", "shell", "opet", "bp", "petrol", "total", "akaryak", "motorin"}
    combined = (p.get("name", "") + str(res)).lower()
    return any(b in combined for b in fuel_brands)
