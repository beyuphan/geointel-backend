"""
routes.py — v4.0 (Temiz Yeniden Yazım)

Değişiklikler:
- Phase sistemi KALDIRILDI
- Action card'lar tamamen deterministik (LLM yanıtına bakılmaz)
- POI overlay mantığı sadeleştirildi
- ask_human node KALDIRILDI
- Gereksiz eski endpoint temizlendi
"""
import json
import asyncio
import time
from typing import Optional
from fastapi import APIRouter, Depends
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from logger import log
from core.graph import intent_node, agent_node, custom_tool_node, should_continue, AgentState
from core.mcp_client import orchestrator
from api.schemas import (
    ApiResponse as ApiEnvelope, ApiError, ApiMetadata,
    ChatResponse, MapData, MapMarker, ActionCard,
    ChatRequest as ChatRequestV1, LocationUpdateRequest as LocUpdateV1,
    PoiOverlay, PoiOverlayCard
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