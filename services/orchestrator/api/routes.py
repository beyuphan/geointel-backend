"""
routes.py — v5.0

Değişiklikler:
- /api/v1/trip/plan endpoint eklendi (yapılandırılmış yolculuk planlama)
- POI overlay yakıt fiyatlarıyla zenginleştirildi
- Trip context Redis'e kaydediliyor, sonraki chat mesajları bağlamı biliyor
- Prompt'ta mekan adı yasagı güçlendirildi
"""
import json
import asyncio
import time
from typing import Optional, List
from fastapi import APIRouter, Depends
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from logger import log
from core.graph import intent_node, agent_node, custom_tool_node, should_continue, AgentState
from core.mcp_client import orchestrator
from core.macro_tools import RouteStrategyEvaluator
from api.schemas import (
    ApiResponse as ApiEnvelope, ApiError, ApiMetadata,
    ChatResponse, MapData, MapMarker, ActionCard,
    ChatRequest as ChatRequestV1, LocationUpdateRequest as LocUpdateV1,
    TripPlanRequest, TripAddStopsRequest, PoiOverlay, PoiOverlayCard
)
from api.deps import get_optional_user

router = APIRouter()
memory = MemorySaver()


# ─────────────────────────────────────────────────────────────────────────────
# WORKFLOW
# ─────────────────────────────────────────────────────────────────────────────

def _build_workflow():
    wf = StateGraph(AgentState)
    wf.add_node("classifier", intent_node)
    wf.add_node("agent", agent_node)
    wf.add_node("tools", custom_tool_node)
    wf.set_entry_point("classifier")
    wf.add_edge("classifier", "agent")
    wf.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    wf.add_edge("tools", "agent")
    return wf.compile(checkpointer=memory)


# ─────────────────────────────────────────────────────────────────────────────
# ACTION CARD FACTORY — tamamen deterministik, LLM metnine bağımlı değil
# ─────────────────────────────────────────────────────────────────────────────

def _build_action_cards(intent: dict, visual_data: dict) -> list:
    """
    Kullanıcının intent'ine ve görsel veriye göre action card üretir.
    LLM yanıt metnine hiç bakılmaz.
    """
    category    = intent.get("category", "general")
    has_poly    = bool(visual_data.get("polyline"))
    has_markers = bool(visual_data.get("markers"))
    cards = []

    # ── Rota çizildiyse ──────────────────────────────────────────────────
    if category == "routing" and has_poly:
        cards += [
            {"id": "nav_start",   "label": "Navigasyonu Başlat",        "action": "ui:start_navigation",   "icon": "🗺️", "style": "primary"},
            {"id": "food_route",  "label": "Yolda Yemek",               "action": "Yol güzergahımda uygun restoranları öner", "icon": "🍽️", "style": "secondary"},
            {"id": "fuel_route",  "label": "Yakıt Analizi",             "action": "ui:fuel_range_prompt",  "icon": "⛽", "style": "secondary",
             "action_template": "Yakıt analizimi yap, {range_km} km menzilim var, güzergahtaki istasyonları göster",
             "ui_component": "slider", "min_val": 0, "max_val": 1000},
            {"id": "radar",       "label": "Radar Noktaları",            "action": "Yol üzerindeki radar ve kontrol noktalarını göster", "icon": "📸", "style": "secondary"},
            {"id": "alt_routes",  "label": "Alternatif Rotalar",        "action": "ui:show_alternatives",  "icon": "🔄", "style": "secondary"},
        ]
        return cards[:5]

    # ── Mekan önerildi (POI overlay açık) ───────────────────────────────
    if has_markers and not has_poly:
        cards += [
            {"id": "add_route", "label": "Rotama Ekle",      "action": "Bu mekanı rotama ekle ve güzergahı güncelle", "icon": "➕", "style": "primary"},
            {"id": "alt_poi",   "label": "Başka Öner",        "action": "Farklı alternatifler öner", "icon": "🔍", "style": "secondary"},
            {"id": "map_show",  "label": "Haritada Göster",  "action": "ui:show_on_map",            "icon": "📍", "style": "secondary"},
        ]
        return cards[:3]

    # ── Rota VAR + mekan önerisi ─────────────────────────────────────────
    if has_markers and has_poly:
        cards += [
            {"id": "add_stop",  "label": "Durağı Ekle",      "action": "Bu mekanı ara durak olarak rotama ekle", "icon": "➕", "style": "primary"},
            {"id": "skip_stop", "label": "Geç",               "action": "Bu durağı atla",                         "icon": "⏭️", "style": "secondary"},
        ]
        return cards[:2]

    # ── Yakıt bağımsız sorgu ─────────────────────────────────────────────
    if category == "fuel":
        cards += [
            {"id": "fuel_find", "label": "Yakın İstasyon Bul", "action": "Bulunduğum yere en yakın benzin istasyonunu bul", "icon": "⛽", "style": "primary"},
        ]

    # ── Eczane ──────────────────────────────────────────────────────────
    if category == "pharmacy":
        cards += [
            {"id": "pharm_nav", "label": "Eczaneye Git", "action": "ui:navigate_to_pharmacy", "icon": "💊", "style": "primary"},
        ]

    # ── Etkinlik ─────────────────────────────────────────────────────────
    if category == "event":
        cards += [
            {"id": "event_nav", "label": "Etkinliğe Yol Tarifi", "action": "Bu etkinliğin mekanına navigasyon başlat", "icon": "🎯", "style": "primary"},
        ]

    # ── Gün planı ────────────────────────────────────────────────────────
    if category == "day_plan":
        cards += [
            {"id": "day_route",  "label": "Rota Oluştur",    "action": "Planladığım yerler için optimum rota oluştur", "icon": "🗺️", "style": "primary"},
            {"id": "day_more",   "label": "Daha Fazla Öner", "action": "Başka aktiviteler de öner", "icon": "✨", "style": "secondary"},
        ]

    return cards[:4]


# ─────────────────────────────────────────────────────────────────────────────
# POI OVERLAY BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def _build_poi_overlay(markers: list, intent: dict) -> Optional[PoiOverlay]:
    """Marker listesinden mobil swipe overlay oluşturur."""
    if not markers:
        return None

    category = intent.get("category", "places")
    cat_map = {
        "fuel":    ("⛽ Yakıt İstasyonları",  "benzin_istasyonu"),
        "pharmacy":("💊 Nöbetçi Eczaneler",   "eczane"),
        "event":   ("🎯 Etkinlikler",          "etkinlik"),
    }
    title, cat_label = cat_map.get(category, ("📍 Yakındaki Mekanlar", "poi"))
    if category == "routing":
        title, cat_label = "🍽️ Güzergah Üstü Mekanlar", "restoran"

    cards = []
    best_idx, best_score = 0, -1

    for i, m in enumerate(markers):
        if not isinstance(m, dict):
            continue
        dev    = m.get("deviation_meters", 9999) or 9999
        rating = m.get("rating") or 0
        score  = (1 / (dev + 1)) * 1000 + rating
        if score > best_score:
            best_score, best_idx = score, i

        dev_label = (
            "Yol üstü ✅" if dev <= 400
            else f"{dev}m sapma ⚠️" if dev <= 2000
            else f"{dev/1000:.1f}km uzatır ❌"
        )
        extra_min = round(dev / 500) if dev > 400 else 0
        impact    = f"+{extra_min} dk" if extra_min else "Sıfır ek süre"
        place_id  = f"poi_{i}_{str(m.get('lat',''))[:6]}_{str(m.get('lon',''))[:6]}"

        # Yakıt fiyat bilgisi (fuel_price inject edildiyse taşı)
        fuel_price_info = m.get("fuel_price") or None

        # Önerilen mekan için recommendation_reason hesapla
        rec_reason = None
        m_type = m.get("type", "poi")
        if m_type == "fuel_station" and fuel_price_info:
            price = fuel_price_info.get("price_per_liter")
            company = fuel_price_info.get("company", "")
            rec_reason = f"{company}: {price} TL/L" if price else "Yakıt İstasyonu"

        cards.append(PoiOverlayCard(
            id=place_id,
            name=m.get("name", "Mekan"),
            address=m.get("address") or m.get("snippet"),
            category=cat_label,
            lat=float(m["lat"]), lon=float(m["lon"]),
            deviation_meters=dev if dev < 9999 else None,
            distance_along_route_km=m.get("distance_along_route_km"),
            extra_time_min=extra_min if dev > 400 else 0,
            eta=m.get("eta"),
            on_route_side=m.get("on_route_side"),
            rating=m.get("rating"),
            review_count=m.get("review_count"),
            price_level=m.get("price_level"),
            is_open=m.get("open_now"),
            open_now=m.get("open_now"),
            opening_hours=m.get("opening_hours"),
            phone=m.get("phone"),
            deviation_label=dev_label,
            route_impact_label=impact,
            is_recommended=False,
            fuel_price_info=fuel_price_info,
            recommendation_reason=rec_reason,
        ))


    if cards:
        cards[best_idx] = cards[best_idx].model_copy(update={
            "is_recommended": True,
            "recommendation_reason": "En iyi konum + puan",
        })
        cards.sort(key=lambda c: (0 if c.is_recommended else 1, c.deviation_meters or 9999))

    return PoiOverlay(
        mode="poi_selection",
        title=title,
        subtitle=f"{len(cards)} mekan bulundu · Kaydırarak incele",
        cards=cards,
        primary_action="Rotama Ekle",
        secondary_action="Farklı Mekan Öner",
    )


# ─────────────────────────────────────────────────────────────────────────────
# HISTORY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _load_history(session_id: str) -> list:
    if not orchestrator.redis_client:
        return []
    try:
        raw = orchestrator.redis_client.lrange(f"chat:{session_id}", 0, -1)
        out = []
        for item in raw:
            if isinstance(item, bytes):
                item = item.decode("utf-8")
            msg = json.loads(item)
            if msg["role"] == "user":
                out.append(HumanMessage(content=msg["content"]))
            elif msg["role"] == "assistant":
                out.append(AIMessage(content=msg["content"]))
        return out
    except Exception as e:
        log.error(f"Geçmiş okunamadı: {e}")
        return []


def _save_history(session_id: str, user_msg: str, assistant_msg: str):
    if not orchestrator.redis_client:
        return
    key = f"chat:{session_id}"
    orchestrator.redis_client.rpush(key, json.dumps({"role": "user",      "content": user_msg}))
    orchestrator.redis_client.rpush(key, json.dumps({"role": "assistant", "content": assistant_msg}))
    orchestrator.redis_client.ltrim(key, -20, -1)
    orchestrator.redis_client.expire(key, 86400)


# ─────────────────────────────────────────────────────────────────────────────
# POLYLINE NORMALIZER — HER FORMAT → JSON koordinat listesi
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_polyline(raw) -> str:
    """Her türlü polyline formatını [[lat,lon],...] JSON'a çevirir."""
    if not raw:
        return ""
    try:
        import flexpolyline
        import polyline as gp

        decoded = []
        if isinstance(raw, list):
            decoded = [p[:2] for p in raw]
        elif isinstance(raw, str):
            raw = raw.strip()
            if raw.startswith("["):
                pts = json.loads(raw)
                decoded = [p[:2] for p in pts]
            else:
                try:
                    decoded = flexpolyline.decode(raw)   # HERE v6
                except Exception:
                    try:
                        decoded = gp.decode(raw)          # Google v5
                    except Exception:
                        return raw                        # olduğu gibi gönder

        if decoded:
            return json.dumps([[float(p[0]), float(p[1])] for p in decoded])
        return str(raw)
    except Exception as e:
        log.warning(f"⚠️ Polyline normalize hatası: {e}")
        return str(raw)


# ─────────────────────────────────────────────────────────────────────────────
# CORE CHAT RUNNER
# ─────────────────────────────────────────────────────────────────────────────

async def _run_chat(message: str, session_id: str,
                    current_lat=None, current_lon=None, fcm_token=None) -> dict:
    # Konum + FCM kaydet
    if orchestrator.redis_client and current_lat and current_lon:
        orchestrator.redis_client.setex(f"loc:{session_id}", 3600, f"{current_lat},{current_lon}")
    if orchestrator.redis_client and fcm_token:
        orchestrator.redis_client.setex(f"fcm:{session_id}", 86400 * 30, fcm_token)

    # Geçmiş + yeni mesaj
    history = _load_history(session_id)
    history.append(HumanMessage(content=message))

    executor = _build_workflow()
    config   = {"configurable": {"thread_id": session_id}}

    input_state = {
        "messages":    history,
        "intent":      {},
        "retry_count": 0,
        "session_id":  session_id,
        "visual_data": {"markers": [], "polyline": None, "geojson_layers": []},
        "route_polyline": None,
    }

    final_state = await executor.ainvoke(input_state, config)

    # Yanıt metni
    raw = final_state["messages"][-1].content
    if isinstance(raw, list):
        response_text = "".join(b.get("text", "") for b in raw if isinstance(b, dict))
    else:
        response_text = str(raw)

    _save_history(session_id, message, response_text)

    # Polyline: state → Redis
    raw_poly = final_state.get("visual_data", {}).get("polyline")
    if not raw_poly and orchestrator.redis_client:
        cached = orchestrator.redis_client.get(f"route:{session_id}")
        if cached:
            raw_poly = cached if isinstance(cached, str) else cached.decode("utf-8")

    # Kullanılan araçlar
    tools_used = []
    for msg in final_state.get("messages", []):
        for tc in getattr(msg, "tool_calls", []) or []:
            name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
            if name and name not in tools_used:
                tools_used.append(name)

    return {
        "response_text": response_text,
        "visual_data":   final_state.get("visual_data", {}),
        "route_polyline": raw_poly,
        "intent":        final_state.get("intent", {}),
        "tools_used":    tools_used,
        "retry_count":   final_state.get("retry_count", 0),
    }


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/chat")
async def chat_legacy(request: ChatRequestV1):
    """Geriye uyumluluk endpoint."""
    t0 = time.monotonic()
    result = await _run_chat(
        message=request.message, session_id=request.session_id,
        current_lat=request.current_lat, current_lon=request.current_lon,
        fcm_token=request.fcm_token,
    )
    return {
        "response":       result["response_text"],
        "session_id":     request.session_id,
        "visual_data":    result["visual_data"],
        "route_polyline": result["route_polyline"],
        "action_cards":   _build_action_cards(result["intent"], result["visual_data"]),
        "metadata":       {"response_time_ms": int((time.monotonic() - t0) * 1000)},
    }


@router.post("/location/update")
async def update_location_legacy(req: LocUpdateV1):
    sid = req.session_id or "default_session"
    if orchestrator.redis_client:
        loc = f"{req.lat},{req.lon}"
        orchestrator.redis_client.setex(f"loc:{sid}", 3600, loc)
        return {"status": "ok", "location": loc}
    return {"status": "error", "message": "Redis unavailable"}


@router.get("/health")
async def health_check():
    redis_ok = False
    if orchestrator.redis_client:
        try:
            orchestrator.redis_client.ping()
            redis_ok = True
        except Exception:
            pass
    return {
        "status": "ok",
        "redis": redis_ok,
        "agents_connected": list(orchestrator.sessions.keys()),
        "tool_count": len(orchestrator.runtime_tools),
    }


@router.post("/api/v1/chat", response_model=ApiEnvelope, tags=["Chat v1"])
async def chat_v1(request: ChatRequestV1, user: dict = Depends(get_optional_user)):
    """📱 Ana mobil chat endpoint."""
    t0 = time.monotonic()
    session_id = request.session_id
    if user:
        session_id = f"{user['user_id']}:{request.session_id}"

    log.info(f"📩 [v1/Chat] session={session_id} | msg={request.message[:50]}...")

    try:
        result = await _run_chat(
            message=request.message, session_id=session_id,
            current_lat=request.current_lat, current_lon=request.current_lon,
            fcm_token=request.fcm_token,
        )
        elapsed = int((time.monotonic() - t0) * 1000)
        visual  = result["visual_data"]
        intent  = result["intent"]

        # ── Marker dönüşümü ───────────────────────────────────────────────
        markers = []
        for m in visual.get("markers", []):
            if not (isinstance(m, dict) and "lat" in m):
                continue
            side = m.get("on_route_side", "unknown")
            poi_card = {
                "on_route_side":           side,
                "side_label": (
                    "✅ Sağ taraf" if side == "right"
                    else "⚠️ Sol taraf" if side == "left"
                    else None
                ),
                "opening_hours":           m.get("opening_hours", []),
                "open_now":                m.get("open_now"),
                "eta":                     m.get("eta"),
                "deviation_meters":        m.get("deviation_meters", 0),
                "distance_along_route_km": m.get("distance_along_route_km"),
                "rating":                  m.get("rating"),
                "review_count":            m.get("review_count"),
                "price_level":             m.get("price_level"),
                "phone":                   m.get("phone"),
                "address":                 m.get("address") or m.get("snippet", ""),
                "type":                    m.get("type", "poi"),
            }
            markers.append(MapMarker(
                lat=m["lat"], lon=m.get("lon", m.get("lng", 0)),
                title=m.get("title", m.get("name")),
                type=m.get("type", "poi"),
                icon=m.get("icon"),
                snippet=m.get("snippet") or m.get("address"),
                poi_card=poi_card,
            ))

        # Deviation'a göre sırala
        markers.sort(key=lambda mk: (mk.poi_card or {}).get("deviation_meters") or 99999)

        # ── Polyline ──────────────────────────────────────────────────────
        polyline = _normalize_polyline(result.get("route_polyline") or visual.get("polyline"))

        # ── Action cards ──────────────────────────────────────────────────
        raw_cards = _build_action_cards(intent, visual)
        cards = []
        for i, c in enumerate(raw_cards):
            act = c["action"]
            cards.append(ActionCard(
                id=c.get("id", f"card_{i}"),
                label=c["label"],
                action=act,
                icon=c.get("icon", ""),
                style=c.get("style", "secondary"),
                action_template=c.get("action_template"),
                is_ui_only=act.startswith("ui:"),
                ui_component=c.get("ui_component"),
                options=c.get("options"),
                min_val=c.get("min_val"),
                max_val=c.get("max_val"),
            ))

        # ── POI Overlay ───────────────────────────────────────────────────
        # Sadece markers varken rota yoksa overlay göster -> iptal, her zaman göster.
        has_poly = bool(polyline)
        poi_overlay = None
        raw_marker_dicts = visual.get("markers", [])
        poi_markers = [m for m in raw_marker_dicts if isinstance(m, dict) and m.get("type") != "waypoint"]
        if poi_markers:
            poi_overlay = _build_poi_overlay(poi_markers, intent)

        status = "completed"

        return ApiEnvelope(
            success=True,
            data=ChatResponse(
                status=status,
                message=result["response_text"],
                intent=intent,
                map=MapData(
                    markers=markers,
                    polyline=polyline,
                    geojson_layers=visual.get("geojson_layers", []),
                ),
                action_cards=cards,
                tools_used=result.get("tools_used", []),
                poi_overlay=poi_overlay,
                distance_km=visual.get("distance_km"),
                duration_min=visual.get("duration_min"),
            ).model_dump(),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )

    except Exception as e:
        log.error(f"🔥 [v1/Chat] Hata: {e}")
        elapsed = int((time.monotonic() - t0) * 1000)
        return ApiEnvelope(
            success=False,
            error=ApiError(code="CHAT_ERROR", message=str(e)),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )


@router.post("/api/v1/location/update", response_model=ApiEnvelope, tags=["Location v1"])
async def update_location_v1(req: LocUpdateV1, user: dict = Depends(get_optional_user)):
    t0 = time.monotonic()
    prefix = user["user_id"] if user else "anon"
    sid = req.session_id or f"{prefix}:default"
    if orchestrator.redis_client:
        loc = f"{req.lat},{req.lon}"
        orchestrator.redis_client.setex(f"loc:{sid}", 3600, loc)
        log.info(f"📍 [Location] {prefix} → {loc}")
        return ApiEnvelope(
            success=True,
            data={"location": loc, "session_id": sid},
            metadata=ApiMetadata(response_time_ms=int((time.monotonic() - t0) * 1000)),
        )
    return ApiEnvelope(
        success=False,
        error=ApiError(code="REDIS_UNAVAILABLE", message="Konum kaydedilemedi."),
        metadata=ApiMetadata(response_time_ms=int((time.monotonic() - t0) * 1000)),
    )


@router.get("/api/v1/history/chat", response_model=ApiEnvelope, tags=["History"])
async def get_chat_history(session_id: str = "default_session",
                            user: dict = Depends(get_optional_user)):
    history = _load_history(session_id)
    messages = [
        {"role": "assistant" if isinstance(m, AIMessage) else "user", "content": m.content}
        for m in history
    ]
    return ApiEnvelope(success=True, data={"messages": messages}, metadata=ApiMetadata())


@router.get("/api/v1/health", tags=["System"])
async def health_v1():
    redis_ok = False
    if orchestrator.redis_client:
        try:
            orchestrator.redis_client.ping()
            redis_ok = True
        except Exception:
            pass

    connected = list(orchestrator.sessions.keys())
    services  = []
    for svc in ["mcp_city", "mcp_intel", "mcp_satellite"]:
        services.append({
            "name":   svc,
            "status": "online" if svc in connected else "offline",
        })

    return {
        "status":            "ok",
        "redis":             redis_ok,
        "services":          services,
        "agents_connected":  connected,
        "tool_count":        len(orchestrator.runtime_tools),
    }


# ─────────────────────────────────────────────────────────────────────────────
# TRIP PLAN ENDPOINT — Yapılandırılmış Yolculuk Planlama
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/api/v1/trip/plan", response_model=ApiEnvelope, tags=["Trip Planning"])
async def plan_trip(request: TripPlanRequest, user: dict = Depends(get_optional_user)):
    """
    🗺️ Akıllı Yolculuk Planlayıcı

    TripSetupWizard'dan gelen yapılandırılmış parametreleri alır:
    - Deterministik araç çağrısı (LLM'e doğal dil gönderilmez)
    - Rota + mola + yemek + yakıt analizini tek seferde yapar
    - Trip context'i session'a kaydeder → sonraki chat mesajları bu bağlamı bilir
    """
    t0 = time.monotonic()
    session_id = request.session_id
    if user:
        session_id = f"{user['user_id']}:{request.session_id}"

    log.info(f"🗺️ [TripPlan] session={session_id} | {request.origin} → {request.destination}")

    try:
        # ── 1. Konum çöz ──────────────────────────────────────────────────
        origin = request.origin
        if origin == "CURRENT_LOCATION" and request.current_lat and request.current_lon:
            origin = f"{request.current_lat},{request.current_lon}"

        # Konum + FCM kaydet
        if orchestrator.redis_client and request.current_lat and request.current_lon:
            orchestrator.redis_client.setex(
                f"loc:{session_id}", 3600,
                f"{request.current_lat},{request.current_lon}"
            )
        if orchestrator.redis_client and request.fcm_token:
            orchestrator.redis_client.setex(f"fcm:{session_id}", 86400 * 30, request.fcm_token)

        # ── 2. Kullanıcı profili — yakıt tipi ve tüketim ─────────────────
        from profile_manager import ProfileManager
        user_ctx = await ProfileManager.get_combined_context(session_id)
        fuel_type = "benzin"
        fuel_consumption = 8.0  # varsayılan L/100km
        if isinstance(user_ctx, dict):
            fuel_type = user_ctx.get("fuel_type", "benzin")
            fuel_consumption = float(user_ctx.get("highway_consumption") or 8.0)

        # ── 3. Temel rota hesapla ─────────────────────────────────────────
        waypoints_str = "|".join(request.waypoints) if request.waypoints else None
        route_args = {
            "origin": origin,
            "destination": request.destination,
            "preference": "fastest",
        }
        if waypoints_str:
            route_args["waypoints"] = waypoints_str

        route_tool = orchestrator.get_tool_by_name("get_route_data")
        if not route_tool:
            raise RuntimeError("get_route_data tool bulunamadı")

        route_res = await asyncio.wait_for(route_tool.ainvoke(route_args), timeout=30.0)
        if isinstance(route_res, str):
            try:
                route_res = json.loads(route_res)
            except Exception:
                pass

        if not isinstance(route_res, dict):
            raise RuntimeError("Rota verisi alınamadı")

        # RouteResponse model key'leri: polyline, distance_km, duration_min
        polyline = (
            route_res.get("polyline")
            or route_res.get("polyline_encoded")
            or ""
        )
        total_km = float(
            route_res.get("distance_km")
            or route_res.get("mesafe_km")
            or 0
        )
        total_min = int(
            route_res.get("duration_min")
            or route_res.get("sure_dk")
            or 0
        )

        # Polyline Redis'e kaydet
        if orchestrator.redis_client and polyline:
            orchestrator.redis_client.setex(f"route:{session_id}", 3600, polyline)

        # ── 4. Mola ve yemek slotlarını hesapla ──────────────────────────
        break_interval_km = request.break_interval_hours * 80  # ~80km/saat
        break_slots: List[float] = []
        if request.break_interval_hours > 0 and total_km > break_interval_km:
            curr = break_interval_km
            while curr < total_km - 20:
                break_slots.append(curr)
                curr += break_interval_km

        # Yemek noktası
        food_fraction = {"Başları": 0.25, "Ortaları": 0.5, "Sonları": 0.75}.get(
            request.food_location, 0.5
        )
        food_target_km = total_km * food_fraction

        # ── 5. Yemek yeri ara ────────────────────────────────────────────
        food_markers: list = []
        if total_km > 50 and polyline and request.food_preference != "Molasız":
            food_query_map = {
                "Yöresel Lezzetler": "yöresel restoran lokanta",
                "Fast Food": "fast food hamburger",
                "Ev Yemekleri": "ev yemeği lokanta",
                "Kahve & Tatlı": "kafe kahve tatlı",
                "Fark etmez": "restoran",
            }
            food_query = food_query_map.get(request.food_preference, "restoran")
            places_tool = orchestrator.get_tool_by_name("search_hybrid_places")
            if places_tool:
                food_res = await asyncio.wait_for(
                    places_tool.ainvoke({
                        "query": food_query,
                        "route_polyline": polyline,
                        "target_fraction": food_fraction,
                    }),
                    timeout=20.0,
                )
                if isinstance(food_res, str):
                    try:
                        food_res = json.loads(food_res)
                    except Exception:
                        food_res = {}
                if isinstance(food_res, dict):
                    all_places = (
                        food_res.get("strict_route_places", [])
                        + food_res.get("relaxed_route_places", [])
                        + food_res.get("places", [])
                    )
                    # Yemek noktasına en yakın 5 mekan
                    all_places.sort(
                        key=lambda p: abs(float(p.get("distance_along_route_km") or 0) - food_target_km)
                    )
                    food_markers = all_places[:5]

        # ── 6. Yakıt analizi ─────────────────────────────────────────────
        fuel_markers: list = []
        fuel_section_summary = {}
        if total_km > 100 and polyline:
            # Anlık menzil belirleniyor — varsa kullanıcının girdiği, yoksa araç profili
            effective_fuel_km = request.fuel_remaining_km if request.fuel_remaining_km > 0 else (
                (40 / fuel_consumption) * 100  # varsayılan 40L depo
            )
            evaluator = RouteStrategyEvaluator(orchestrator)
            fuel_result = await evaluator.evaluate(
                origin=origin,
                destination=request.destination,
                fuel_type=fuel_type,
                fuel_range=effective_fuel_km,
            )
            if fuel_result.get("status") == "success":
                fuel_markers = fuel_result.get("places", [])
                for fm in fuel_markers:
                    fm["type"] = "fuel_station"
                fuel_section_summary = {
                    "cheapest_city": fuel_result.get("cheapest_fuel_city"),
                    "best_station": fuel_result.get("best_station_recommendation"),
                }

        # ── 7. Hava durumu (150km+) ───────────────────────────────────────
        weather_warnings: list = []
        if total_km >= 150 and request.current_lat and request.current_lon:
            weather_tool = orchestrator.get_tool_by_name("get_weather")
            if weather_tool:
                try:
                    # Rotanın ortasını kontrol et — basit yaklaşım: varış koordinatlarını kullan
                    w_res = await asyncio.wait_for(
                        weather_tool.ainvoke({
                            "lat": request.current_lat,
                            "lon": request.current_lon,
                        }),
                        timeout=10.0,
                    )
                    if isinstance(w_res, str):
                        try:
                            w_res = json.loads(w_res)
                        except Exception:
                            w_res = {}
                    if isinstance(w_res, dict):
                        condition = ""
                        weather_data = w_res.get("data", w_res)
                        if isinstance(weather_data, dict) and "ANLIK_DURUM" in weather_data:
                            condition = weather_data["ANLIK_DURUM"].get("durum", "").lower()
                        bad_conditions = ["rain", "drizzle", "thunderstorm", "yağmur", "kar", "snow", "fırtına", "sis"]
                        if any(c in condition for c in bad_conditions):
                            weather_warnings.append({
                                "location": "Başlangıç",
                                "condition": condition,
                                "severity": "warning",
                                "message": f"Dikkat: {condition} bekleniyor, yavaş gidin.",
                            })
                except Exception:
                    pass

        # ── 8. POI Overlay oluştur ────────────────────────────────────────
        all_markers = food_markers + fuel_markers
        poi_overlay = None
        sections = []

        if food_markers:
            food_cards = []
            # Rota km'sine en yakın 5 mekanı al, deviation'a göre de sırala
            food_markers_sorted = sorted(
                food_markers,
                key=lambda m: (
                    abs(float(m.get("distance_along_route_km") or 0) - food_target_km),
                    m.get("deviation_meters") or 9999
                )
            )[:5]
            for i, m in enumerate(food_markers_sorted):
                if not isinstance(m, dict) or "lat" not in m:
                    continue
                dev = m.get("deviation_meters")
                if dev is None:
                    dev = 9999
                extra_min = round(dev / 500) if dev > 400 and dev != 9999 else 0
                rating = m.get("rating")
                is_open = m.get("open_now")
                dist_along = m.get("distance_along_route_km") or 0
                name = m.get("name", "Restoran")

                # AI öneri açıklaması üret
                rec_parts = []
                if dev <= 400:
                    rec_parts.append("Yol üstünde")
                elif dev < 2000:
                    rec_parts.append(f"{int(dev)}m sapmayı hak ediyor")
                if rating and float(rating) >= 4.0:
                    rec_parts.append(f"{rating}/5 puan ile çok beğenilen")
                elif rating and float(rating) >= 3.5:
                    rec_parts.append(f"{rating}/5 puan ile güvenilir")
                if is_open is True:
                    rec_parts.append("varışında açık olacak")
                elif is_open is False:
                    rec_parts.append("⚠️ varışta kapalı olabilir")
                if int(dist_along) > 0:
                    rec_parts.append(f"rotanın ~{int(dist_along)}. km'sinde")
                ai_rec = (f"{name} — " + ", ".join(rec_parts) + ".") if rec_parts else None

                food_cards.append(PoiOverlayCard(
                    id=f"food_{i}_{str(m.get('lat',''))[:6]}",
                    name=name,
                    address=m.get("address") or m.get("snippet"),
                    category="restoran",
                    lat=float(m["lat"]), lon=float(m["lon"]),
                    deviation_meters=dev if dev < 9999 else None,
                    distance_along_route_km=m.get("distance_along_route_km"),
                    extra_time_min=extra_min,
                    eta=m.get("eta"),
                    rating=rating,
                    review_count=m.get("review_count"),
                    price_level=m.get("price_level"),
                    is_open=is_open,
                    open_now=is_open,
                    opening_hours=m.get("opening_hours"),
                    phone=m.get("phone"),
                    deviation_label="Yol üstü ✅" if dev <= 400 else f"{dev}m sapma ⚠️",
                    route_impact_label=f"+{extra_min} dk" if extra_min else "Sıfır ek süre",
                    is_recommended=(i == 0),
                    recommendation_reason="Rota üstü en iyi seçenek" if i == 0 else None,
                    ai_recommendation=ai_rec,
                ))
            if food_cards:
                sections.append({
                    "type": "food",
                    "title": f"🍽️ {request.food_preference} Önerileri",
                    "subtitle": f"Rotayı dörde böldük, {request.food_location.lower()} öneriyoruz",
                    "target_km": int(food_target_km),
                    "cards": [c.model_dump() for c in food_cards],
                })

        if fuel_markers:
            fuel_cards = []
            # Önce fiyata göre sırala, fiyat yoksa sapmaya göre
            fuel_markers_sorted = sorted(
                fuel_markers,
                key=lambda m: (
                    0 if m.get("fuel_price") else 1,
                    m.get("fuel_price", {}).get("price_per_liter", 9999) if m.get("fuel_price") else 9999,
                    m.get("deviation_meters") or 9999,
                )
            )
            for i, m in enumerate(fuel_markers_sorted):
                if not isinstance(m, dict) or "lat" not in m:
                    continue
                dev = m.get("deviation_meters")
                if dev is None:
                    dev = 9999
                fp = m.get("fuel_price")
                price_label = None
                name = m.get("name", "Benzin İstasyonu")

                if fp:
                    price_label = f"{fp.get('company', '')}: {fp.get('price_per_liter', '?')} TL/L"

                # AI öneri açıklaması üret
                rec_parts = []
                if fp and fp.get("price_per_liter"):
                    rec_parts.append(f"{fp['price_per_liter']} TL/L ile {fp.get('company', 'istasyon')}'dan doldurun")
                if dev <= 400:
                    rec_parts.append("yol üstünde")
                elif dev < 2000:
                    rec_parts.append(f"{int(dev)}m sapma ile ulaşılabilir")
                dist_along = m.get("distance_along_route_km") or 0
                if int(dist_along) > 0:
                    rec_parts.append(f"rotanın {int(dist_along)}. km'sinde")
                ai_rec = " — ".join(rec_parts).capitalize() + "." if rec_parts else None

                fuel_cards.append(PoiOverlayCard(
                    id=f"fuel_{i}_{str(m.get('lat',''))[:6]}",
                    name=name,
                    address=m.get("address") or m.get("snippet"),
                    category="benzin_istasyonu",
                    lat=float(m["lat"]), lon=float(m["lon"]),
                    deviation_meters=dev if dev < 9999 else None,
                    distance_along_route_km=m.get("distance_along_route_km"),
                    rating=m.get("rating"),
                    is_open=m.get("open_now"),
                    open_now=m.get("open_now"),
                    deviation_label="Yol üstü ✅" if dev <= 400 else f"{dev}m sapma ⚠️",
                    route_impact_label="Sıfır ek süre" if dev <= 400 else f"+{round(dev/500)} dk",
                    is_recommended=(i == 0),
                    fuel_price_info=fp,
                    recommendation_reason=price_label or "En uygun istasyon",
                    ai_recommendation=ai_rec,
                ))
            if fuel_cards:
                sections.append({
                    "type": "fuel",
                    "title": "⛽ Yakıt Durakları",
                    "subtitle": f"Menzilini aşmadan önce doldurun — {int(request.fuel_remaining_km)} km menzil kaldı",
                    "cards": [c.model_dump() for c in fuel_cards],
                })

        if sections or all_markers:
            poi_overlay = PoiOverlay(
                mode="trip_plan",
                title=f"🗺️ {request.destination} Yolculuk Planı",
                subtitle=f"{int(total_km)} km · ~{total_min // 60}sa {total_min % 60}dk",
                cards=[],
                primary_action="Navigasyonu Başlat",
                secondary_action="Planı Düzenle",
                route_summary={
                    "total_km": total_km,
                    "total_min": total_min,
                    "break_count": len(break_slots),
                    "food_stops": len(food_markers),
                    "fuel_stops": len(fuel_markers),
                    "fuel_summary": fuel_section_summary,
                },
                weather_warnings=weather_warnings if weather_warnings else None,
                sections=sections if sections else None,
            )

        # ── 9. Trip context Redis'e kaydet ───────────────────────────────
        trip_context = {
            "origin": origin,
            "destination": request.destination,
            "total_km": total_km,
            "total_min": total_min,
            "fuel_remaining_km": request.fuel_remaining_km,
            "fuel_type": fuel_type,
            "food_preference": request.food_preference,
            "break_interval_hours": request.break_interval_hours,
            "custom_note": request.custom_note,
            "waypoints": request.waypoints,
        }
        if orchestrator.redis_client:
            orchestrator.redis_client.setex(
                f"trip_ctx:{session_id}", 3600 * 8,
                json.dumps(trip_context, ensure_ascii=False)
            )

        # ── 10. Tüm markerları harita için hazırla ──────────────────────
        map_markers: List[MapMarker] = []
        for m in all_markers:
            if not isinstance(m, dict) or "lat" not in m:
                continue
            map_markers.append(MapMarker(
                lat=m["lat"], lon=m["lon"],
                title=m.get("name", m.get("title")),
                type=m.get("type", "poi"),
                snippet=m.get("address") or m.get("snippet"),
                poi_card={
                    "rating": m.get("rating"),
                    "deviation_meters": m.get("deviation_meters", 0),
                    "open_now": m.get("open_now"),
                    "fuel_price": m.get("fuel_price"),
                    "type": m.get("type", "poi"),
                },
            ))

        normalized_poly = _normalize_polyline(polyline)
        elapsed = int((time.monotonic() - t0) * 1000)

        # ── Akıllı yanıt metni ───────────────────────────────────────────
        note_part = f" Not: '{request.custom_note}'" if request.custom_note else ""
        reply = (
            f"Rotanı hazırladım kanka! {request.destination}'a {int(total_km)} km, "
            f"yaklaşık {total_min // 60} saat {total_min % 60} dakika yol var.{note_part} "
            f"{'Hava durumuna dikkat et!' if weather_warnings else 'Güvenli yolculuklar!'}"
        )

        # Action cards
        action_cards = [
            ActionCard(
                id="nav_start", label="Navigasyonu Başlat",
                action="ui:start_navigation", icon="🗺️", style="primary",
                is_ui_only=True,
            ),
            ActionCard(
                id="fuel_refine", label="Yakıt Analizi",
                action="Rota üzerindeki yakıt istasyonlarını analiz et",
                icon="⛽", style="secondary",
            ),
        ]
        if weather_warnings:
            action_cards.append(ActionCard(
                id="weather_detail", label="Hava Durumu",
                action="Rota boyunca hava durumunu detaylı göster",
                icon="🌦️", style="secondary",
            ))

        return ApiEnvelope(
            success=True,
            data=ChatResponse(
                status="completed",
                message=reply,
                intent={"category": "routing", "complexity": "high"},
                map=MapData(markers=map_markers, polyline=normalized_poly),
                action_cards=action_cards,
                poi_overlay=poi_overlay,
                distance_km=total_km,
                duration_min=total_min,
                trip_plan=trip_context,
            ).model_dump(),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )

    except Exception as e:
        log.error(f"🔥 [TripPlan] Hata: {e}")
        elapsed = int((time.monotonic() - t0) * 1000)
        return ApiEnvelope(
            success=False,
            error=ApiError(code="TRIP_PLAN_ERROR", message=str(e)),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )


# ─────────────────────────────────────────────────────────────────────────────
# TRIP ADD STOPS — Deterministik Durak Ekleme (LLM KULLANILMAZ)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/api/v1/trip/add_stops", response_model=ApiEnvelope, tags=["Trip Planning"])
async def add_stops_to_trip(request: TripAddStopsRequest, user: dict = Depends(get_optional_user)):
    """
    🛑 Seçilen durakları rotaya ekler — LLM YOK, tamamen deterministik.

    POI seçim ekranından "Rotama Ekle" basıldığında çağrılır.
    Seçilen mekanların koordinatlarını waypoint olarak get_route_data'ya inject eder.
    Sonuç: güncellenmiş polyline + mesafe/süre.
    """
    t0 = time.monotonic()
    session_id = request.session_id
    if user:
        session_id = f"{user['user_id']}:{request.session_id}"

    log.info(f"🛑 [AddStops] session={session_id} | {len(request.selected_stops)} durak")

    try:
        # ── 1. Başlangıç koordinatını çöz ─────────────────────────────────
        origin = request.origin
        if not origin and request.current_lat and request.current_lon:
            origin = f"{request.current_lat},{request.current_lon}"
        if not origin and orchestrator.redis_client:
            cached_loc = orchestrator.redis_client.get(f"loc:{session_id}")
            if cached_loc:
                origin = cached_loc if isinstance(cached_loc, str) else cached_loc.decode("utf-8")
        if not origin:
            raise ValueError("Başlangıç konumu bulunamadı.")

        # ── 2. Hedefi ve eski waypointleri çöz ───────────────────────────
        destination = request.destination
        existing_waypoints = []
        tc = {}
        if orchestrator.redis_client:
            raw_tc = orchestrator.redis_client.get(f"trip_ctx:{session_id}")
            if raw_tc:
                tc = json.loads(raw_tc if isinstance(raw_tc, str) else raw_tc.decode("utf-8"))
                if not destination:
                    destination = tc.get("destination")
                # Eski waypoint'leri al
                old_wps = tc.get("waypoints")
                if old_wps:
                    if isinstance(old_wps, list):
                        existing_waypoints = old_wps
                    elif isinstance(old_wps, str):
                        existing_waypoints = old_wps.split("|")

        if not destination:
            raise ValueError("Hedef bilgisi bulunamadı. Önce rota planlayın.")

        # ── 3. Seçilen durakları waypoint string'ine dönüştür ────────────
        if not request.selected_stops:
            raise ValueError("Hiç durak seçilmedi.")

        new_waypoints = [
            f"{s['lat']},{s['lon']}"
            for s in request.selected_stops
            if isinstance(s, dict) and "lat" in s and "lon" in s
        ]
        
        # Eski ve yeni waypoint'leri birleştir
        combined_waypoints = existing_waypoints + new_waypoints
        waypoints_str = "|".join(combined_waypoints)

        if not waypoints_str:
            raise ValueError("Geçerli koordinat bulunamadı.")

        # ── 4. Rotayı güncelle ────────────────────────────────────────────
        route_tool = orchestrator.get_tool_by_name("get_route_data")
        if not route_tool:
            raise RuntimeError("get_route_data tool bulunamadı.")

        route_args = {
            "origin": origin,
            "destination": destination,
            "waypoints": waypoints_str,
            "preference": "fastest",
        }

        log.info(f"🗺️ [AddStops] Rota güncelleniyor: {origin} → {destination} | wp={waypoints_str[:60]}")

        route_res = await asyncio.wait_for(route_tool.ainvoke(route_args), timeout=30.0)
        if isinstance(route_res, str):
            try:
                route_res = json.loads(route_res)
            except Exception:
                pass

        if not isinstance(route_res, dict):
            raise RuntimeError("Rota verisi alınamadı.")

        polyline = (
            route_res.get("polyline")
            or route_res.get("polyline_encoded")
            or ""
        )
        total_km = float(route_res.get("distance_km") or route_res.get("mesafe_km") or 0)
        total_min = int(route_res.get("duration_min") or route_res.get("sure_dk") or 0)

        # Güncel polyline'ı ve context'i Redis'e kaydet
        if orchestrator.redis_client:
            if polyline:
                orchestrator.redis_client.setex(f"route:{session_id}", 3600, polyline)
            if tc:
                tc["waypoints"] = combined_waypoints
                orchestrator.redis_client.setex(f"trip_ctx:{session_id}", 3600 * 8, json.dumps(tc, ensure_ascii=False))

        # ── 5. Marker'ları hazırla ────────────────────────────────────────
        stop_names = [s.get("name", f"Durak {i+1}") for i, s in enumerate(request.selected_stops)]
        map_markers = []
        for i, s in enumerate(request.selected_stops):
            if "lat" not in s or "lon" not in s:
                continue
            map_markers.append(MapMarker(
                lat=s["lat"], lon=s["lon"],
                title=s.get("name", f"Durak {i+1}"),
                type=s.get("type", "waypoint"),
                snippet=s.get("address", "Seçilen durak"),
            ))

        normalized_poly = _normalize_polyline(polyline)
        elapsed = int((time.monotonic() - t0) * 1000)

        # ── 6. Yanıt ─────────────────────────────────────────────────────
        stops_summary = ", ".join(stop_names[:3])
        if len(stop_names) > 3:
            stops_summary += f" ve {len(stop_names)-3} durak daha"

        reply = (
            f"Rotana {len(request.selected_stops)} durak eklendi kanka! "
            f"({stops_summary}) "
            f"Güncellenen rota: {int(total_km)} km, ~{total_min // 60}sa {total_min % 60}dk. "
            "Navigasyonu başlatmak için hazır!"
        )

        return ApiEnvelope(
            success=True,
            data=ChatResponse(
                status="completed",
                message=reply,
                intent={"category": "routing", "complexity": "high"},
                map=MapData(
                    markers=map_markers,
                    polyline=normalized_poly,
                ),
                action_cards=[
                    ActionCard(
                        id="nav_start", label="Navigasyonu Başlat",
                        action="ui:start_navigation", icon="🗺️", style="primary",
                        is_ui_only=True,
                    ),
                ],
                distance_km=total_km,
                duration_min=total_min,
            ).model_dump(),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )

    except Exception as e:
        log.error(f"🔥 [AddStops] Hata: {e}")
        elapsed = int((time.monotonic() - t0) * 1000)
        return ApiEnvelope(
            success=False,
            error=ApiError(code="ADD_STOPS_ERROR", message=str(e)),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )