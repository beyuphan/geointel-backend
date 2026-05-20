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
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from logger import log
from core.graph import intent_node, agent_node, custom_tool_node, should_continue, AgentState
from core.mcp_client import orchestrator
from core.macro_tools import RouteStrategyEvaluator, _reverse_geocode_district
from api.schemas import (
    ApiResponse as ApiEnvelope, ApiError, ApiMetadata,
    ChatResponse, MapData, MapMarker, ActionCard,
    ChatRequest as ChatRequestV1, LocationUpdateRequest as LocUpdateV1,
    TripPlanRequest, TripAddStopsRequest, PoiOverlay, PoiOverlayCard,
    DayPlanRequest, DayPlanCard, DayPlanSection, DayPlanResponse,
    DayPlanScheduleRequest, DayPlanScheduleResponse, ScheduleItem,
    ReplaceStopRequest, ReplaceStopResponse, ReplaceStopSuggestion,
)
from api.deps import get_optional_user

router = APIRouter()
memory = MemorySaver()

_BREAK_REJECT_PATTERNS = [
    "inşaat", "valilik", "vali ", "kaymakamlık",
    "belediye", "müdürlük", "müdürlüğü",
    "okul", "lise", "üniversite", "fakülte",
    "hastane", "klinik", "sağlık merkezi",
    "mahkeme", "adliye", "jandarma", "emniyet", "karakol",
    "cami", "camii", "kilise",
    "konut", "apartman", "rezidans",
    "hizmet binası", "idare binası",
]

def _is_valid_break_stop(place: dict) -> bool:
    name = (place.get("name") or "").lower()
    dev = float(place.get("deviation_meters") or 9999)
    if dev > 2000:
        return False
    for pattern in _BREAK_REJECT_PATTERNS:
        if pattern in name:
            return False
    return True


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


async def _llm_invoke_with_fallback(messages, timeout: float = 15.0) -> str:
    """
    Önce Gemini'yi dener; PERMISSION_DENIED / 403 / rate-limit gibi hatalarda
    Claude'a otomatik düşer. Çağıran kod tek bir string yanıt alır.
    Boş string dönüyorsa caller fallback metni göstermeli.
    """
    last_error: Optional[Exception] = None
    # 1) Gemini denemesi
    if orchestrator.llm_gemini:
        try:
            res = await asyncio.wait_for(
                orchestrator.llm_gemini.ainvoke(messages), timeout=timeout
            )
            text = res.content if hasattr(res, "content") else str(res)
            if isinstance(text, list):
                text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
            text = (text or "").strip()
            if text:
                return text
        except Exception as e:
            last_error = e
            log.warning(f"⚠️ [LLM/Gemini] {type(e).__name__}: {str(e)[:120]}")

    # 2) Claude fallback
    if orchestrator.llm_claude:
        try:
            log.info("🔁 [LLM] Gemini başarısız → Claude'a düşülüyor")
            res = await asyncio.wait_for(
                orchestrator.llm_claude.ainvoke(messages), timeout=timeout
            )
            text = res.content if hasattr(res, "content") else str(res)
            if isinstance(text, list):
                text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
            return (text or "").strip()
        except Exception as e:
            last_error = e
            log.warning(f"⚠️ [LLM/Claude] {type(e).__name__}: {str(e)[:120]}")

    if last_error:
        log.error(f"🔥 [LLM] Hem Gemini hem Claude başarısız: {last_error}")
    return ""


async def _serve_pharmacy_request(
    *, lat: float, lon: float, session_id: str, t0: float
) -> Optional["ApiEnvelope"]:
    """
    Eczane fast-path:
    1) konumdan il/ilçe bul
    2) get_pharmacies çağır
    3) eczaneleri kullanıcı konumuna olan mesafeye göre sırala, en yakın 5
    4) LLM ile samimi öneri metni üret
    5) markers'ı Redis'e kaydet (sonraki "rota oluştur" mesajı için)
    """
    import httpx as _httpx
    from core.macro_tools import _haversine_km as _haversine

    nom_headers = {"User-Agent": "GeoIntel_Orchestrator/4.0"}
    rg: Optional[dict] = None
    async with _httpx.AsyncClient(headers=nom_headers) as nom_client:
        rg = await _reverse_geocode_district(lat, lon, nom_client)
    if not rg:
        log.warning(f"⚠️ [PharmacyFast] {lat},{lon} reverse geocode başarısız")
        return None

    city = rg["city"]
    district = rg["district"]
    log.info(f"💊 [PharmacyFast] Konum: {city}/{district}")

    pharm_tool = orchestrator.get_tool_by_name("get_pharmacies")
    if not pharm_tool:
        return None
    try:
        raw = await asyncio.wait_for(
            pharm_tool.ainvoke({"city": city, "district": district}),
            timeout=30.0,
        )
    except Exception as e:
        log.warning(f"⚠️ [PharmacyFast] get_pharmacies hata: {e}")
        return None

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    pharm_list = []
    if isinstance(raw, dict):
        pharm_list = raw.get("data") or raw.get("items") or []
    elif isinstance(raw, list):
        pharm_list = raw

    # Koordinatlıları parse et + mesafe hesabı
    parsed: List[dict] = []
    for ph in pharm_list:
        if not isinstance(ph, dict):
            continue
        coord_str = ph.get("coordinates") or ph.get("koordinat")
        plat: Optional[float] = None
        plon: Optional[float] = None
        if isinstance(coord_str, str) and "," in coord_str:
            try:
                a, b = coord_str.split(",", 1)
                plat = float(a.strip())
                plon = float(b.strip())
            except Exception:
                plat = plon = None
        if plat is None or plon is None:
            continue
        name = ph.get("name") or ph.get("isim") or "Eczane"
        addr = ph.get("address") or ph.get("adres") or ""
        phone = ph.get("phone") or ph.get("tel") or ""
        dist_label = ph.get("district") or ph.get("ilce") or district
        dist_km = _haversine(lat, lon, plat, plon)
        parsed.append({
            "name": name,
            "address": addr,
            "phone": phone,
            "district": dist_label,
            "lat": plat,
            "lon": plon,
            "distance_km": dist_km,
        })

    if not parsed:
        log.info(f"💊 [PharmacyFast] {city}/{district} için koordinatlı eczane yok")
        return None

    # En yakın 5'i seç
    parsed.sort(key=lambda p: p["distance_km"])
    nearest = parsed[:5]

    # Markers (mesafe ile snippet)
    markers: List[MapMarker] = []
    for i, p in enumerate(nearest):
        dist_label_m = (
            f"{int(p['distance_km'] * 1000)}m"
            if p["distance_km"] < 1
            else f"{p['distance_km']:.1f}km"
        )
        snippet_parts = [dist_label_m]
        if p["address"]:
            snippet_parts.append(p["address"])
        markers.append(MapMarker(
            lat=p["lat"],
            lon=p["lon"],
            title=p["name"],
            type="pharmacy",
            snippet=" · ".join(snippet_parts),
            poi_card={
                "address": p["address"],
                "phone": p["phone"],
                "district": p["district"],
                "distance_km": p["distance_km"],
                "distance_label": dist_label_m,
                "type": "pharmacy",
                "pharmacy_index": i,  # "ilkini istiyorum" için
            },
        ))

    # LLM ile samimi öneri metni
    response_text = await _generate_pharmacy_recommendation(
        nearest=nearest, city=city, district=district
    )

    # Markers'ı Redis'e kaydet — sonraki mesajlar referans çözebilsin
    if orchestrator.redis_client:
        try:
            payload = json.dumps([
                {
                    "name": p["name"],
                    "address": p["address"],
                    "lat": p["lat"],
                    "lon": p["lon"],
                    "distance_km": p["distance_km"],
                }
                for p in nearest
            ], ensure_ascii=False)
            orchestrator.redis_client.setex(
                f"pharm_options:{session_id}", 1800, payload
            )
        except Exception as _e:
            log.warning(f"⚠️ [PharmacyFast] Redis kayıt başarısız: {_e}")

    elapsed = int((time.monotonic() - t0) * 1000)
    return ApiEnvelope(
        success=True,
        data=ChatResponse(
            status="completed",
            message=response_text,
            intent={"category": "pharmacy", "confidence": 1.0},
            map=MapData(markers=markers),
            action_cards=[],
            tools_used=["get_pharmacies"],
            poi_overlay=None,
        ).model_dump(),
        metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
    )


async def _generate_pharmacy_recommendation(
    *, nearest: List[dict], city: str, district: str
) -> str:
    """LLM ile 2-3 cümlelik samimi eczane önerisi üret. Hata olursa basit fallback."""
    try:
        if orchestrator.llm_gemini and nearest:
            lines = []
            for i, p in enumerate(nearest):
                dist = (
                    f"{int(p['distance_km'] * 1000)}m"
                    if p["distance_km"] < 1
                    else f"{p['distance_km']:.1f}km"
                )
                line = f"{i + 1}. {p['name']} — {dist}"
                if p["address"]:
                    line += f" — {p['address']}"
                lines.append(line)
            sys = (
                "Sen bir öneri asistanısın. Kullanıcı 'nöbetçi eczane' aradı. "
                "Sana mesafeye göre sıralı eczane listesi veriliyor. **2-3 kısa "
                "cümle** yaz: en yakın olanı öner, neden onu önerdiğini söyle "
                "(en yakın olduğu için, kolay ulaşılır vs.). Diğer 1-2 alternatifi "
                "de kısaca an. Liste yapma — akıcı paragraf yaz. **Mekan adlarını "
                "kalın** (markdown ** ile) işaretle. Robotik 'Size şu eczaneyi "
                "öneririm' tarzı YASAK. Samimi, arkadaş gibi konuş.\n"
                "Sonunda kullanıcıya 'haritada pinleri görebilir veya bana "
                "hangisini istediğini yazabilirsin' diye hatırlat (1 cümle)."
            )
            usr = (
                f"Kullanıcı konumu: {city}/{district}\n"
                f"Eczaneler (yakından uzağa):\n" + "\n".join(lines)
            )
            res = await asyncio.wait_for(
                orchestrator.llm_gemini.ainvoke([
                    SystemMessage(content=sys),
                    HumanMessage(content=usr),
                ]),
                timeout=10.0,
            )
            text = res.content if hasattr(res, "content") else str(res)
            if isinstance(text, list):
                text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
            text = (text or "").strip()
            if len(text) > 20:
                return text
    except Exception as e:
        log.warning(f"⚠️ [PharmacyFast/LLM] {e}")

    # Fallback
    top = nearest[0]
    dist = (
        f"{int(top['distance_km'] * 1000)}m"
        if top["distance_km"] < 1
        else f"{top['distance_km']:.1f}km"
    )
    return (
        f"📍 **{city}/{district}** çevresinde **{len(nearest)}** nöbetçi eczane "
        f"buldum, haritada işaretledim. Sana en yakın olarak **{top['name']}** "
        f"({dist}) öneririm. Pinlere bakıp birini seçebilir ya da bana "
        f"hangisini istediğini yazarak rota başlatabilirsin."
    )


async def _serve_pharmacy_followup_navigation(
    *, message: str, current_lat: float, current_lon: float,
    session_id: str, t0: float,
) -> Optional["ApiEnvelope"]:
    """
    Eczane listesi sunulduktan sonra kullanıcı 'X eczanesine rota', 'ilkini',
    'en yakına götür' gibi referans yazarsa Redis'teki eczane listesinden eşle
    ve trip plan tetikle.
    """
    if not orchestrator.redis_client:
        return None
    raw = orchestrator.redis_client.get(f"pharm_options:{session_id}")
    if not raw:
        return None
    try:
        options = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(options, list) or not options:
        return None

    msg_l = message.lower()

    # 1) Numara referansı (1, 2, 3, ilk, ikinci, üçüncü)
    chosen: Optional[dict] = None
    _num_words = {
        "ilk": 0, "1.": 0, "1": 0, "birinci": 0,
        "ikinci": 1, "2.": 1, "2": 1,
        "üçüncü": 2, "ucuncu": 2, "3.": 2, "3": 2,
        "dördüncü": 3, "dorduncu": 3, "4.": 3, "4": 3,
        "beşinci": 4, "besinci": 4, "5.": 4, "5": 4,
    }
    for w, idx in _num_words.items():
        if w in msg_l and idx < len(options):
            chosen = options[idx]
            break

    # 2) "en yakın"
    if not chosen and ("en yakın" in msg_l or "en yakini" in msg_l or "yakınına" in msg_l):
        chosen = options[0]

    # 3) Eczane adından eşleşme
    if not chosen:
        for opt in options:
            name_l = (opt.get("name") or "").lower()
            # Eczane adının ana kelimesini bul (örn "Ay Eczanesi" → "ay")
            words = [w for w in name_l.replace("eczanesi", "").replace("eczane", "").split() if len(w) >= 3]
            for w in words:
                if w in msg_l:
                    chosen = opt
                    break
            if chosen:
                break

    if not chosen:
        return None

    log.info(
        f"💊 [PharmacyFollowup] '{chosen.get('name')}' seçildi, trip plan tetikleniyor"
    )

    # Trip plan tetikle (CURRENT_LOCATION → eczane koordinatı)
    try:
        trip_req = TripPlanRequest(
            origin="CURRENT_LOCATION",
            destination=f"{chosen['lat']},{chosen['lon']}",
            waypoints=[],
            waypoint_labels=[chosen.get("name") or "Eczane"],
            current_lat=current_lat,
            current_lon=current_lon,
            session_id=session_id,
            # Eczaneye gidiş — yakıt/mola/yemek soruları anlamsız
            break_interval_hours=0.0,
            food_preference="Fark etmez",
            fuel_remaining_km=0,
            custom_note=f"Hedef: {chosen.get('name') or 'eczane'} (nöbetçi).",
        )
        # plan_trip endpoint'ini direkt çağır (auth opsiyonel)
        resp = await plan_trip(trip_req, user=None)
        return resp
    except Exception as e:
        log.warning(f"⚠️ [PharmacyFollowup] trip plan hata: {e}")
        return None


async def _serve_free_mode_place_lookup(
    *,
    message: str,
    query: str,
    category: str,
    lat: float,
    lon: float,
    session_id: str,
    t0: float,
) -> Optional["ApiEnvelope"]:
    """
    Serbest modda genel POI araması: reverse_geocode ile ilçe bul, ardından
    search_hybrid_places'i kullanıcının konumunun çevresinde tetikle, sonuçları
    POI overlay (mode=navigate_to_poi) olarak dön.
    """
    import httpx as _httpx

    # Konum bilgisi (etiket için)
    nom_headers = {"User-Agent": "GeoIntel_Orchestrator/4.0"}
    rg: Optional[dict] = None
    async with _httpx.AsyncClient(headers=nom_headers) as nom_client:
        rg = await _reverse_geocode_district(lat, lon, nom_client)
    city = (rg or {}).get("city", "")
    district = (rg or {}).get("district", "")

    search_tool = orchestrator.get_tool_by_name("search_hybrid_places")
    if not search_tool:
        return None
    try:
        raw = await asyncio.wait_for(
            search_tool.ainvoke({
                "query": query,
                "lat": lat,
                "lon": lon,
                "session_id": f"freelookup:{session_id}",
            }),
            timeout=20.0,
        )
    except Exception as e:
        log.warning(f"⚠️ [FreeLookup] search_hybrid_places hata: {e}")
        return None

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    places: list = []
    if isinstance(raw, dict):
        places = (
            raw.get("places")
            or raw.get("strict_route_places", [])
            + raw.get("relaxed_route_places", [])
        )
    elif isinstance(raw, list):
        places = raw

    # En yakın 6 sonucu al, koordinatı olmayanları ele
    valid_places: list = []
    for p in places:
        if not isinstance(p, dict):
            continue
        plat = p.get("lat")
        plon = p.get("lon")
        if not isinstance(plat, (int, float)) or not isinstance(plon, (int, float)):
            continue
        valid_places.append(p)
    valid_places = valid_places[:8]

    if not valid_places:
        return None

    markers: List[MapMarker] = []
    cards: List[PoiOverlayCard] = []
    for i, p in enumerate(valid_places):
        name = p.get("name") or "Mekan"
        addr = p.get("address") or p.get("snippet") or ""
        plat = float(p["lat"])
        plon = float(p["lon"])
        rating = p.get("rating")
        markers.append(MapMarker(
            lat=plat,
            lon=plon,
            title=name,
            type="free_poi",
            snippet=addr,
            poi_card={
                "address": addr,
                "rating": rating,
                "type": "free_poi",
            },
        ))
        cards.append(PoiOverlayCard(
            id=f"free_{i}",
            name=name,
            address=addr,
            category="free_poi",
            lat=plat,
            lon=plon,
            rating=rating if isinstance(rating, (int, float)) else None,
            is_recommended=(i == 0),
            recommendation_reason=(f"{district}" if district else None),
            ai_recommendation=(f"📍 {addr}" if addr else None),
        ))

    loc_label = f"{district}/{city}".strip("/") if (district or city) else "konumun çevresi"
    # POI overlay YOK — sadece harita üzerinde marker. Kullanıcı marker'a tıklayıp
    # bilgi penceresinden detay görür veya Maps üzerinden gider.
    response_text = (
        f"📍 **{loc_label}** çevresinde **{len(markers)}** sonuç bulundu ve haritada "
        f"işaretlendi. Marker'a dokunarak detay görebilirsin."
    )
    elapsed = int((time.monotonic() - t0) * 1000)
    return ApiEnvelope(
        success=True,
        data=ChatResponse(
            status="completed",
            message=response_text,
            intent={"category": category or "place_lookup", "confidence": 0.9},
            map=MapData(markers=markers),
            action_cards=[],
            tools_used=["search_hybrid_places"],
            poi_overlay=None,
        ).model_dump(),
        metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
    )


# Serbest mod intent sınıflandırması için keyword setleri
_FREE_PLACE_LOOKUP_KEYWORDS = {
    "kafe": ("kafe", "kafe"),
    "kahve": ("kafe kahvehane", "kafe"),
    "restoran": ("restoran lokanta", "restoran"),
    "lokanta": ("lokanta restoran", "restoran"),
    "yemek": ("restoran lokanta", "restoran"),
    "atm": ("atm bankamatik", "atm"),
    "market": ("market süpermarket", "market"),
    "süpermarket": ("market süpermarket", "market"),
    "benzin": ("benzin istasyonu", "yakıt"),
    "yakıt": ("benzin istasyonu", "yakıt"),
    "otopark": ("otopark", "otopark"),
    "hastane": ("hastane", "hastane"),
    "polis": ("polis karakolu", "polis"),
    "manav": ("manav", "manav"),
    "fırın": ("fırın ekmek", "fırın"),
    "berber": ("berber kuaför", "berber"),
}
_FREE_INFORMATIONAL_KEYWORDS = (
    "ne giyeyim", "üstüme",
    "hava nasıl", "yağmur mu", "yağmur var mı",
    "günbatımı", "gün doğumu", "saat kaçta",
    "şu an saat", "ne zaman",
)


@router.post("/api/v1/chat", response_model=ApiEnvelope, tags=["Chat v1"])
async def chat_v1(request: ChatRequestV1, user: dict = Depends(get_optional_user)):
    """📱 Ana mobil chat endpoint."""
    t0 = time.monotonic()
    session_id = request.session_id
    if user:
        session_id = f"{user['user_id']}:{request.session_id}"

    log.info(f"📩 [v1/Chat] session={session_id} | msg={request.message[:50]}...")

    # Serbest mod: önceki trip_ctx ve route polyline cache'ini de temizle —
    # eski rota context'i karışmasın, yeni sorgu kendi haritasını çizebilsin.
    if request.mode == "free" and orchestrator.redis_client:
        orchestrator.redis_client.delete(f"trip_ctx:{session_id}")
        orchestrator.redis_client.delete(f"route:{session_id}")
        log.info(f"🧹 [v1/Chat] Serbest mod — trip_ctx + route polyline ({session_id}) temizlendi")

    # ── Serbest Mod Router ───────────────────────────────────────────────
    # Free mode'da mesajı sınıflandır: place_lookup → POI overlay (navigate_to_poi),
    # informational/complex → mevcut LangGraph chat akışına bırak.
    # Bu router eski rota state'iyle hiç ilişki kurmaz → "Karabük'e ekledi"
    # tipi hatalar engellenir.
    msg_lower = request.message.lower()
    has_location = bool(request.current_lat and request.current_lon)

    # 0) Eczane FOLLOW-UP — daha önce eczane listesi sunulmuşsa ve mesaj
    #    bir referans içeriyorsa (ilkini, en yakına, X eczanesine git), direkt
    #    trip plan tetikle.
    _followup_triggers = (
        "rota", "götür", "götur", "yol tarifi", "git ", "gidelim",
        "buraya git", "ilkini", "en yakın", "en yakini",
    )
    if (
        request.mode == "free"
        and has_location
        and any(t in msg_lower for t in _followup_triggers)
    ):
        try:
            fu_response = await _serve_pharmacy_followup_navigation(
                message=request.message,
                current_lat=request.current_lat,
                current_lon=request.current_lon,
                session_id=session_id,
                t0=t0,
            )
            if fu_response is not None:
                return fu_response
        except Exception as e:
            log.warning(f"⚠️ [v1/Chat] Pharmacy followup başarısız: {e}")

    # 1) Eczane fast-path (özel scraper, koordinatlı + nöbetçi listesi)
    _pharm_keywords = ("nöbetçi eczane", "nobetci eczane", "açık eczane",
                       "acik eczane", "eczane bul", "eczane var mı")
    if (
        request.mode == "free"
        and has_location
        and any(k in msg_lower for k in _pharm_keywords)
    ):
        try:
            pharm_response = await _serve_pharmacy_request(
                lat=request.current_lat,
                lon=request.current_lon,
                session_id=session_id,
                t0=t0,
            )
            if pharm_response is not None:
                return pharm_response
        except Exception as e:
            log.warning(f"⚠️ [v1/Chat] Eczane fast-path başarısız: {e}")

    # 2) Genel place_lookup fast-path (kafe, restoran, atm, market vb.)
    if request.mode == "free" and has_location:
        matched_kw = next(
            (k for k in _FREE_PLACE_LOOKUP_KEYWORDS if k in msg_lower),
            None,
        )
        if matched_kw:
            try:
                lookup_query, category = _FREE_PLACE_LOOKUP_KEYWORDS[matched_kw]
                place_response = await _serve_free_mode_place_lookup(
                    message=request.message,
                    query=lookup_query,
                    category=category,
                    lat=request.current_lat,
                    lon=request.current_lon,
                    session_id=session_id,
                    t0=t0,
                )
                if place_response is not None:
                    return place_response
            except Exception as e:
                log.warning(f"⚠️ [v1/Chat] place_lookup başarısız: {e}")

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
# LLM NARRATIVE HELPER — Rota anlatımı (kuru metin yerine zengin paragraf)
# ─────────────────────────────────────────────────────────────────────────────

async def _generate_trip_narrative(
    *,
    origin: str,
    destination: str,
    total_km: float,
    total_min: int,
    eta_display: str,
    weather_warnings: list,
    radar_count: int,
    toll_info: Optional[dict],
    food_markers: list,
    fuel_section_summary: Optional[str],
    custom_note: Optional[str],
    waypoint_labels: Optional[list],
    break_markers: Optional[list] = None,
    fuel_type: Optional[str] = None,
) -> Optional[str]:
    """
    Toplanan deterministik rota verisini LLM'e sunup 3-5 cümlelik
    sıcak/akıcı bir anlatım üretir. Hata olursa None döner — caller fallback yapar.
    """
    try:
        from langchain_core.messages import SystemMessage, HumanMessage

        # Veriyi LLM'e geçilebilir bir özete dönüştür
        hours = total_min // 60
        mins = total_min % 60
        wp_names = [w for w in (waypoint_labels or []) if isinstance(w, str) and w.strip()]
        weather_lines = []
        for w in (weather_warnings or [])[:5]:
            if isinstance(w, dict):
                msg = w.get("message") or w.get("title") or w.get("severity")
                temp = w.get("temperature", "")
                rain = w.get("rain_probability", "")
                if msg:
                    detail = ""
                    if temp and temp != "?" and temp not in msg:
                        detail += f" {temp}"
                    if rain and rain not in msg:
                        detail += f", yağış olasılığı {rain}"
                    weather_lines.append(str(msg) + detail)
        food_names = [
            m.get("name") for m in (food_markers or [])[:5]
            if isinstance(m, dict) and m.get("name")
        ]

        ctx_parts = [
            f"Başlangıç: {origin}",
            f"Bitiş: {destination}",
            f"Mesafe: {int(total_km)} km",
            f"Süre: {hours} saat {mins} dakika",
            f"Tahmini varış: {eta_display}",
        ]
        if wp_names:
            ctx_parts.append(f"Ara duraklar: {', '.join(wp_names)}")
        if weather_lines:
            ctx_parts.append(f"Hava uyarıları: {' | '.join(weather_lines)}")
        if radar_count:
            ctx_parts.append(f"Radar noktası: {radar_count}")
        if toll_info and toll_info.get("total_tl"):
            ctx_parts.append(
                f"Ücretli geçiş: ~{toll_info.get('total_tl', 0):.0f} TL ({toll_info.get('count', 0)} nokta)"
            )
        if fuel_section_summary:
            # stops_by_km varsa bullet listesi olarak prompt'a yaz
            if isinstance(fuel_section_summary, dict):
                stops_km_list = fuel_section_summary.get("stops_by_km") or []
                if stops_km_list:
                    fuel_lines = []
                    for stop in stops_km_list[:5]:
                        best = stop.get("best", {}) or {}
                        fp = best.get("fuel_price") or {}
                        km = stop.get("stop_target_km") or best.get("distance_along_route_km")
                        district = fp.get("district") or ""
                        city = fp.get("city") or ""
                        company = fp.get("company") or best.get("name") or "İstasyon"
                        price = fp.get("price_per_liter")
                        if isinstance(price, (int, float)):
                            price_str = f"{price:.2f} ₺/L"
                        else:
                            price_str = fp.get("price_label") or "fiyat bilgisi yok"
                        loc = f"{district}/{city}".strip("/") if (district or city) else ""
                        fuel_lines.append(
                            f"~{int(km) if km else '?'}. km — {loc} — {company} — {price_str}"
                        )
                    if fuel_lines:
                        ctx_parts.append(
                            "Yakıt durakları:\n  · " + "\n  · ".join(fuel_lines)
                        )
                else:
                    cheapest = fuel_section_summary.get("cheapest_city")
                    best_st = fuel_section_summary.get("best_station") or {}
                    if cheapest or best_st:
                        ctx_parts.append(
                            f"Yakıt: cheapest_city={cheapest}, best_station={best_st.get('name')}"
                        )
            else:
                ctx_parts.append(f"Yakıt durumu: {fuel_section_summary}")
        if fuel_type:
            ctx_parts.append(f"Aracın yakıt tipi: {fuel_type}")
        if food_names:
            ctx_parts.append(f"Yol üstü yemek önerileri: {', '.join(food_names)}")
        # Mola noktaları — anlatımda mutlaka geçsin (kullanıcı dikkat çekti)
        break_names = [
            f"{m.get('name')} (~{int(m.get('distance_along_route_km') or 0)}. km)"
            for m in (break_markers or [])
            if isinstance(m, dict) and m.get("name")
        ]
        if break_names:
            ctx_parts.append(f"Önerilen mola noktaları: {' · '.join(break_names)}")
        if custom_note:
            ctx_parts.append(f"Kullanıcı notu: {custom_note}")

        context_block = "\n".join(f"- {p}" for p in ctx_parts)

        system = (
            "Sen GeoIntel uygulamasının yolculuk anlatıcısısın. Kullanıcı rotayı "
            "henüz başlatmadı; senin görevin rotayı BAŞLIK + MADDE LİSTESİ formatında "
            "kısa ama BİLGİ YOĞUN şekilde tanıtmak. Türkçe, samimi ama somut.\n\n"
            "**ÇIKTI MARKDOWN OLACAK** — mobile flutter_markdown render eder.\n"
            "- Her bölümde **mutlaka** `### emoji Başlık` ile aç.\n"
            "- Bölüm gövdesi: **2-4 madde işaretli satır** (`- `). Düz paragraf yazma — "
            "  satır başına bir somut bilgi yaz.\n"
            "- Mesafe, süre, km, fiyat, ilçe, marka, mekan adı → her zaman `**kalın**`.\n"
            "- Boş cümle, dolgu metni, 'güzel yolculuklar' gibi kapanış jargonları "
            "  YASAK (sadece son bölümün son satırında 1 cümle olarak).\n\n"
            "BÖLÜM SIRASI VE İÇERİĞİ:\n\n"
            "### 🛣️ Rota Özeti\n"
            "- **{distance} km** · **{hh} sa {mm} dk** · varış **{ETA}**\n"
            "- Yolun karakteri (sahil/dağ/otoyol/iç hat — konumlardan çıkar)\n"
            "- Varsa ara duraklar: **{Durak1}**, **{Durak2}**\n\n"
            "### 🌤️ Hava ve Güvenlik\n"
            "Hava uyarıları varsa **her biri için bir madde**: km + durum + sıcaklık + yağış%.\n"
            "Hiç uyarı yoksa tek satır: '- Rota boyunca hava açık.'\n"
            "Radar varsa: '- 📸 **{N}** radar noktası — hız limitine dikkat.'\n\n"
            "### 🍽️ Yol Üstü Öneriler\n"
            "food_markers'ı madde olarak listele (max 4):\n"
            "- **Mekan Adı** — yöre/km/puan kısa not\n"
            "Mola noktaları varsa altına 1-2 madde olarak ekle (kahve/dinlenme).\n\n"
            "### ⛽ Yakıt & Maliyet\n"
            "**ÖNEMLİ**: yakıt durakları context'te varsa **HER BİRİNİ AYRI MADDE** olarak yaz, "
            "asla tek satıra koşma. Format:\n"
            "- ~**{km}. km** · **{İlçe/İl}** · **{İstasyon}** · **{X.XX ₺/L}**\n"
            "Sonrasında 1 satır: 'En ucuz nokta: **{İlçe}** (**{X.XX ₺/L}**).'\n"
            "Varsa ücretli geçiş: '- 💳 Toplam **{TL}** TL (**{N}** gişe)'.\n\n"
            "### 💬 Kapanış\n"
            "- Kullanıcı özel notu varsa o not için 1 madde yanıt.\n"
            "- 1 satır güvenli yolculuk dileği.\n\n"
            "KESIN KURALLAR:\n"
            "- Veri yoksa o bölümü atla; ASLA uydurma.\n"
            "- Mesafe/süreyi tek bir bölümde söyle (Rota Özeti).\n"
            "- Toplam çıktı 10-18 satır olmalı (madde başına 1 satır).\n"
            "- Düz cümle paragraflar yerine madde işaretleri."
        )
        user_msg = (
            "Aşağıdaki rota için kullanıcıya gösterilecek 4-paragraflık tanıtımı yaz:\n\n"
            f"{context_block}"
        )

        # Gemini daha hızlı; narrative için yeterli
        res = await orchestrator.llm_gemini.ainvoke([
            SystemMessage(content=system),
            HumanMessage(content=user_msg),
        ])
        text = res.content if hasattr(res, "content") else str(res)
        if isinstance(text, list):
            text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
        text = (text or "").strip()
        if len(text) < 150:
            log.warning(f"⚠️ [TripPlan] Narrative çok kısa ({len(text)} char), fallback'e düşülüyor")
            return None
        return text
    except Exception as e:
        log.warning(f"⚠️ [TripPlan] Narrative üretilemedi: {e}")
        return None


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
        if origin == "CURRENT_LOCATION":
            if request.current_lat and request.current_lon:
                origin = f"{request.current_lat},{request.current_lon}"
            elif orchestrator.redis_client:
                _cached_loc = orchestrator.redis_client.get(f"loc:{session_id}")
                if _cached_loc:
                    origin = _cached_loc if isinstance(_cached_loc, str) else _cached_loc.decode("utf-8")

        # Konum + FCM kaydet
        if orchestrator.redis_client and request.current_lat and request.current_lon:
            orchestrator.redis_client.setex(
                f"loc:{session_id}", 3600,
                f"{request.current_lat},{request.current_lon}"
            )
        if orchestrator.redis_client and request.fcm_token:
            orchestrator.redis_client.setex(f"fcm:{session_id}", 86400 * 30, request.fcm_token)

        # ── 2. Kullanıcı profili — yakıt tipi ve tüketim ─────────────────
        # Yetkili kullanıcının primary aracını DB'den çek (ProfileManager
        # eski string-context döndürdüğü için inline sorgu yapıyoruz).
        fuel_type = "benzin"
        fuel_consumption = 8.0  # varsayılan L/100km
        if user and user.get("user_id"):
            try:
                from core.db import async_session_maker, UserVehicle
                from sqlmodel import select as _select
                from uuid import UUID as _UUID
                async with async_session_maker() as _s:
                    _res = await _s.execute(
                        _select(UserVehicle).where(
                            UserVehicle.user_id == _UUID(user["user_id"]),
                            UserVehicle.is_primary == True,  # noqa: E712
                        )
                    )
                    _veh = _res.scalars().first()
                    if _veh:
                        # DB'de "gasoline/diesel/lpg/electric" tutuyoruz; downstream
                        # tool'lar Türkçe ("benzin/dizel/lpg/elektrik") bekliyor.
                        _ft_map = {
                            "gasoline": "benzin", "diesel": "dizel",
                            "lpg": "lpg", "electric": "elektrik",
                            "benzin": "benzin", "dizel": "dizel",
                        }
                        fuel_type = _ft_map.get(
                            (_veh.fuel_type or "").lower(), _veh.fuel_type or "benzin"
                        )
                        if _veh.highway_consumption:
                            fuel_consumption = float(_veh.highway_consumption)
                        log.info(
                            f"🚗 [TripPlan] User vehicle: {_veh.brand} {_veh.model} "
                            f"({fuel_type}, {fuel_consumption} L/100km)"
                        )
            except Exception as _e:
                log.warning(f"⚠️ [TripPlan] Vehicle lookup failed: {_e}")

        # ── 3. Temel rota hesapla ─────────────────────────────────────────
        # Kullanıcının seçtiği duraklar passThrough modunda — section break yapma,
        # tüm rotayı tek optimizasyonda çöz (sahil/manzara yolu doğal akış)
        waypoints_str = (
            "|".join(f"{w}!pt" for w in request.waypoints)
            if request.waypoints else None
        )
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

        # ── ETA hesapla ───────────────────────────────────────────────────
        from datetime import datetime, timedelta, timezone as _tz
        _now = datetime.now(_tz.utc).astimezone()
        _eta_dt = _now + timedelta(minutes=total_min)
        eta_str = _eta_dt.strftime("%H:%M")
        eta_display = (
            f"{eta_str} ({_eta_dt.strftime('%d %b')})"
            if _eta_dt.date() > _now.date()
            else eta_str
        )

        # ── 4. Mola ve yemek slotlarını hesapla ──────────────────────────
        break_interval_km = request.break_interval_hours * 80  # ~80km/saat
        break_slots: List[float] = []
        if request.break_interval_hours > 0 and total_km > break_interval_km:
            curr = break_interval_km
            while curr < total_km - 20:
                break_slots.append(curr)
                curr += break_interval_km

        # ── 4b. Mola noktası araması ──────────────────────────────────────
        break_markers: list = []
        if break_slots and polyline and total_km > 0:
            places_tool_brk = orchestrator.get_tool_by_name("search_hybrid_places")
            if places_tool_brk:
                _BRK_QUERIES = ["benzin istasyonu akaryakıt", "otoyol hizmet alanı mola", "kafe restoran mola"]
                break_task_list = []
                for slot_km in break_slots:
                    frac = slot_km / total_km
                    for q in _BRK_QUERIES:
                        break_task_list.append((
                            slot_km, q,
                            asyncio.wait_for(
                                places_tool_brk.ainvoke({
                                    "query": q,
                                    "route_polyline": polyline,
                                    "target_fraction": frac,
                                }),
                                timeout=15.0,
                            ),
                        ))

                try:
                    break_results = await asyncio.gather(*[t[2] for t in break_task_list], return_exceptions=True)
                    seen_brk = set()
                    slot_best_tp: dict = {}
                    for i, br in enumerate(break_results):
                        if isinstance(br, Exception):
                            continue
                        if isinstance(br, str):
                            try:
                                br = json.loads(br)
                            except Exception:
                                continue
                        if isinstance(br, dict):
                            slot_key = break_task_list[i][0]
                            if slot_key in slot_best_tp:
                                continue
                            candidates = (
                                br.get("strict_route_places", [])
                                + br.get("relaxed_route_places", [])
                                + br.get("places", [])
                            )
                            for p in candidates[:10]:
                                nm = p.get("name", "")
                                if not nm or nm in seen_brk:
                                    continue
                                if "lat" not in p and "coords" in p:
                                    try:
                                        clat, clon = map(float, p["coords"].split(","))
                                        p["lat"] = clat
                                        p["lon"] = clon
                                    except Exception:
                                        continue
                                if "lat" not in p:
                                    continue
                                if not _is_valid_break_stop(p):
                                    continue
                                seen_brk.add(nm)
                                p["type"] = "break_stop"
                                p["distance_along_route_km"] = slot_key
                                slot_best_tp[slot_key] = p
                                break
                    break_markers = list(slot_best_tp.values())
                    log.info(f"☕ [TripPlan] Mola: {len(break_markers)} nokta (slots={break_slots})")
                except Exception as exc:
                    log.warning(f"⚠️ [TripPlan] Mola araması genel hata: {exc}")

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
                # Geniş alan araması: ±15% fraksiyonlarda parallel
                food_fracs = sorted({
                    max(0.1, round(food_fraction - 0.15, 2)),
                    food_fraction,
                    min(0.9, round(food_fraction + 0.15, 2)),
                })
                food_tasks = [
                    asyncio.wait_for(
                        places_tool.ainvoke({
                            "query": food_query,
                            "route_polyline": polyline,
                            "target_fraction": frac,
                        }),
                        timeout=20.0,
                    )
                    for frac in food_fracs
                ]
                food_results = await asyncio.gather(*food_tasks, return_exceptions=True)
                seen_food: set = set()
                all_places: list = []
                for food_res in food_results:
                    if isinstance(food_res, Exception):
                        continue
                    if isinstance(food_res, str):
                        try:
                            food_res = json.loads(food_res)
                        except Exception:
                            continue
                    if isinstance(food_res, dict):
                        for p in (
                            food_res.get("strict_route_places", [])
                            + food_res.get("relaxed_route_places", [])
                            + food_res.get("places", [])
                        ):
                            nm = p.get("name", "")
                            if nm and nm not in seen_food:
                                seen_food.add(nm)
                                all_places.append(p)
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
                polyline=polyline,
                total_dist_km=total_km,
            )
            if fuel_result.get("status") == "success":
                # Rota uzunluğunu aşan durakları filtrele (700km öneri 677km rotada olmaz)
                fuel_markers = [
                    m for m in fuel_result.get("places", [])
                    if (m.get("distance_along_route_km") or 0) <= total_km
                ]
                for fm in fuel_markers:
                    fm["type"] = "fuel_station"
                fuel_section_summary = {
                    "cheapest_city": fuel_result.get("cheapest_fuel_city"),
                    "best_station": fuel_result.get("best_station_recommendation"),
                    "stops_by_km": fuel_result.get("stops_by_km", []),
                }

        # ── 7. Hava durumu — rota boyunca 40km aralıklarla analiz ────────
        weather_warnings: list = []
        weather_details: list = []  # tüm km-bazlı kontrol noktaları (genişletilebilir kart)
        _weather_analyzed = False
        if total_km >= 50 and polyline:
            weather_tool = orchestrator.get_tool_by_name("analyze_route_weather")
            if not weather_tool:
                log.warning("⚠️ [TripPlan] analyze_route_weather tool bulunamadı")
            else:
                try:
                    w_res = await asyncio.wait_for(
                        weather_tool.ainvoke({
                            "polyline": polyline,
                            "avg_speed_kmh": "90",
                            "departure_minutes_from_now": "0",
                        }),
                        timeout=20.0,
                    )
                    if isinstance(w_res, str):
                        try:
                            w_res = json.loads(w_res)
                        except Exception:
                            w_res = {}
                    if isinstance(w_res, dict):
                        risk = w_res.get("risk_durumu", "BİLİNMİYOR")
                        zones = w_res.get("riskli_bolgeler", [])
                        _weather_analyzed = True
                        # Tüm km noktalarını sakla — genişletilebilir kart için
                        for pt in w_res.get("detayli_ozet", []):
                            if isinstance(pt, dict):
                                weather_details.append({
                                    "km": pt.get("km"),
                                    "saat": pt.get("tahmini_saat") or pt.get("saat"),
                                    "durum": pt.get("durum"),
                                    "sicaklik": pt.get("sicaklik"),
                                    "yagis_olasiligi": pt.get("yagis_olasiligi"),
                                    "ruzgar": pt.get("ruzgar"),
                                    "riskli_mi": bool(pt.get("riskli_mi")),
                                })
                        log.info(f"🌦️ [TripPlan] Hava analizi: risk={risk}, {len(zones)} riskli bölge, {len(weather_details)} kontrol noktası")
                        for zone in zones:
                            if isinstance(zone, dict):
                                temp_str = zone.get("sicaklik", "")
                                rain_str = zone.get("yagis_olasiligi", "")
                                detail = f" {temp_str}" if temp_str else ""
                                if rain_str:
                                    detail += f", yağış olasılığı {rain_str}"
                                weather_warnings.append({
                                    "location": zone.get("km", "Rota"),
                                    "condition": zone.get("durum", ""),
                                    "temperature": temp_str,
                                    "rain_probability": rain_str,
                                    "severity": "warning",
                                    "message": f"Dikkat: {zone.get('durum', 'kötü hava')}{detail}",
                                })
                            elif isinstance(zone, str):
                                weather_warnings.append({
                                    "location": "Rota",
                                    "condition": zone,
                                    "severity": "warning",
                                    "message": zone,
                                })
                        # Risk varsa ama riskli_bolgeler boşsa genel özetten al
                        if not weather_warnings and risk not in ("DÜŞÜK", "YOK", "BİLİNMİYOR"):
                            for pt in w_res.get("detayli_ozet", [])[:3]:
                                temp_str = pt.get("sicaklik", "?")
                                rain_str = pt.get("yagis_olasiligi", "")
                                detail = f" {temp_str}" if temp_str else ""
                                if rain_str:
                                    detail += f", yağış olasılığı {rain_str}"
                                weather_warnings.append({
                                    "location": pt.get("km", "Rota"),
                                    "condition": pt.get("durum", ""),
                                    "temperature": temp_str,
                                    "rain_probability": rain_str,
                                    "severity": "warning" if pt.get("riskli_mi") else "info",
                                    "message": f"{pt.get('durum', 'Hava koşullarını takip edin.')}{detail}",
                                })
                    else:
                        log.warning(f"⚠️ [TripPlan] Hava analizi beklenmeyen format: {type(w_res)}")
                except asyncio.TimeoutError:
                    log.warning("⚠️ [TripPlan] Hava analizi timeout (20s)")
                except Exception as exc:
                    log.warning(f"⚠️ [TripPlan] Hava analizi hata: {exc}")

        # Hava analizi tamamlandı, risk yoksa açık hava bildirimi ekle
        if _weather_analyzed and not weather_warnings:
            weather_warnings.append({
                "location": "Tüm Rota",
                "condition": "Açık",
                "severity": "info",
                "message": "☀️ Rota boyunca hava açık, güvenli yolculuklar!",
            })

        # ── 7.5 Radar noktaları ───────────────────────────────────────────
        radar_count = 0
        if polyline and total_km >= 50:
            radar_tool = orchestrator.get_tool_by_name("get_route_radars")
            if radar_tool:
                try:
                    r_res = await asyncio.wait_for(
                        radar_tool.ainvoke({"route_polyline": polyline}),
                        timeout=15.0,
                    )
                    if isinstance(r_res, str):
                        try:
                            r_res = json.loads(r_res)
                        except Exception:
                            r_res = {}
                    if isinstance(r_res, dict):
                        radar_count = r_res.get("radar_count", 0) or len(r_res.get("radars", []))
                        log.info(f"📸 [TripPlan] Radar: {radar_count} nokta bulundu")
                except Exception as exc:
                    log.warning(f"⚠️ [TripPlan] Radar araması hata: {exc}")

        # ── 8. Ücretli geçiş hesapla ─────────────────────────────────────
        toll_info = None
        if total_km > 50 and polyline:
            toll_tool = orchestrator.get_tool_by_name("get_toll_for_route")
            if toll_tool:
                try:
                    toll_res = await asyncio.wait_for(
                        toll_tool.ainvoke({"route_polyline": polyline}),
                        timeout=15.0,
                    )
                    if isinstance(toll_res, str):
                        try:
                            toll_res = json.loads(toll_res)
                        except Exception:
                            toll_res = {}
                    if isinstance(toll_res, dict) and toll_res.get("toll_count", 0) > 0:
                        toll_info = {
                            "total_tl": toll_res.get("total_toll_cost_tl"),
                            "count": toll_res.get("toll_count"),
                            "details": toll_res.get("tolls", [])[:5],
                        }
                except Exception:
                    pass

        # ── 9. POI Overlay oluştur ────────────────────────────────────────
        # NOT: break_markers (mola noktaları) da haritada görünmeli — eskiden
        # eksikti, kullanıcı 2sa interval verdiğinde nokta gözükmüyordu.
        all_markers = food_markers + fuel_markers + break_markers
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
            # Önce fiyata göre sırala, fiyat yoksa sapmaya göre.
            # NOT: yeni ilçe-bazlı algoritmada fuel_price dict her zaman var ama
            # price_per_liter None olabilir (sadece price_label seti) — None karşılaştırması
            # yapma, 9999 fallback'ine düş.
            def _fuel_sort_key(m):
                fp = m.get("fuel_price") or {}
                ppl = fp.get("price_per_liter")
                has_price = isinstance(ppl, (int, float))
                dev = m.get("deviation_meters")
                if not isinstance(dev, (int, float)):
                    dev = 9999
                return (
                    0 if has_price else 1,
                    ppl if has_price else 9999,
                    dev,
                )
            fuel_markers_sorted = sorted(fuel_markers, key=_fuel_sort_key)
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

        # Mola noktaları kullanıcıya SEÇTİRİLMEZ — biz öneririz, harita üzerinde
        # zaten görünüyor (all_markers'a dahil edildi). POI seçim ekranında
        # ayrı bir section çıkmıyor — kullanıcı "şu mı bu mu" diye uğraşmasın.

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
                    "eta": eta_str,
                    "eta_display": eta_display,
                    "food_stops": len(food_markers),
                    "fuel_stops": len(fuel_markers),
                    "fuel_summary": fuel_section_summary,
                    "toll": toll_info,
                    "radar_count": radar_count,
                },
                weather_warnings=weather_warnings if weather_warnings else None,
                weather_details=weather_details if weather_details else None,
                sections=sections if sections else None,
            )

        # ── 9. Trip context Redis'e kaydet ───────────────────────────────
        trip_context = {
            "origin": origin,
            "destination": request.destination,
            "total_km": total_km,
            "total_min": total_min,
            "eta": eta_str,
            "eta_display": eta_display,
            "fuel_remaining_km": request.fuel_remaining_km,
            "fuel_type": fuel_type,
            "food_preference": request.food_preference,
            "break_interval_hours": request.break_interval_hours,
            "custom_note": request.custom_note,
            "waypoints": request.waypoints,
            "toll_info": toll_info,
            "weather_warnings": weather_warnings,
            "weather_details": weather_details,
            "radar_count": radar_count,
            "break_interval_km": break_interval_km,
        }
        if orchestrator.redis_client:
            orchestrator.redis_client.setex(
                f"trip_ctx:{session_id}", 3600 * 8,
                json.dumps(trip_context, ensure_ascii=False)
            )

        # 9b: save_route_history zengin kayıt — narrative üretildikten sonra yapılır
        # (Aşağıda Adım 11'de _generate_trip_narrative sonrasında çağırıyoruz.)

        # ── 10. Tüm markerları harita için hazırla ──────────────────────
        import re as _re_wp
        _WP_COORD_RE = _re_wp.compile(r'^-?\d+\.?\d*,-?\d+\.?\d*$')
        waypoint_markers: List[MapMarker] = []
        for i, wp in enumerate(request.waypoints):
            if _WP_COORD_RE.match(wp.strip()):
                try:
                    wlat, wlon = map(float, wp.split(','))
                    label = (request.waypoint_labels[i]
                             if i < len(request.waypoint_labels) else f"Durak {i+1}")
                    waypoint_markers.append(MapMarker(
                        lat=wlat, lon=wlon,
                        title=label,
                        type="waypoint",
                        snippet="Ara durak",
                    ))
                except Exception:
                    pass

        map_markers: List[MapMarker] = list(waypoint_markers)
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

        _severe_weather = any(w.get("severity") in ("warning", "critical") for w in weather_warnings)

        # ── LLM Anlatımı (zengin paragraf — kuru metnin yerine) ──────────
        # Tüm deterministik veri toplandıktan sonra LLM'e bağlam sunup
        # 3-5 cümlelik akıcı bir rota tanıtımı yazdırıyoruz.
        narrative_text = await _generate_trip_narrative(
            origin=origin,
            destination=request.destination,
            total_km=total_km,
            total_min=total_min,
            eta_display=eta_display,
            weather_warnings=weather_warnings,
            radar_count=radar_count,
            toll_info=toll_info,
            food_markers=food_markers,
            fuel_section_summary=fuel_section_summary,
            custom_note=request.custom_note,
            waypoint_labels=request.waypoint_labels,
            break_markers=break_markers,
            fuel_type=fuel_type,
        )

        # Fallback: LLM hata verirse mevcut deterministik özet
        if not narrative_text:
            note_part = f" Not: '{request.custom_note}'" if request.custom_note else ""
            toll_part = (
                f" Ücretli geçiş: yaklaşık {toll_info['total_tl']:.0f} TL ({toll_info['count']} nokta)."
                if toll_info else ""
            )
            weather_part = " ⚠️ Dikkat: rota üzerinde olumsuz hava koşulları var!" if _severe_weather else ""
            radar_part = f" 📸 Rota üzerinde {radar_count} radar noktası var." if radar_count else ""
            narrative_text = (
                f"Rotanı hazırladım! {request.destination}'a {int(total_km)} km, "
                f"yaklaşık {total_min // 60} saat {total_min % 60} dakika yol var. "
                f"Tahmini varış: {eta_display}.{toll_part}{weather_part}{radar_part}{note_part} "
                f"{'Güvenli yolculuklar!' if not _severe_weather else ''}"
            ).strip()

        reply = narrative_text

        # ── 11b. Geçmişe zengin kayıt (polyline + waypoint_labels + hava + narrative) ──
        # get_route_data zaten basic kayıt yaptı (mcp_client); bu UPDATE ile zenginleştirir.
        if user:
            try:
                from profile_manager import ProfileManager as _PM
                weather_summary_short = None
                if weather_warnings:
                    severe = [w for w in weather_warnings
                              if isinstance(w, dict) and w.get("severity") in ("warning", "critical")]
                    if severe:
                        weather_summary_short = severe[0].get("message") or severe[0].get("title")

                # Stops listesi: SADECE kullanıcının kesin tercihleri.
                # food_markers/fuel_markers/break_markers ÖNERİ — kullanıcı henüz
                # seçmedi, geçmiş rotalarda ara durak olarak görünmemeli. Bunlar
                # add_stops endpoint'inden geldiğinde kaydedilir (selected_stops).
                _enriched_stops: list = []
                _enriched_stops.append({
                    "kind": "origin",
                    "name": origin,
                    "address": origin,
                    "lat": None, "lon": None, "km": 0,
                })
                # Ara duraklar (sadece kullanıcının manuel girdiği waypoint'ler)
                import re as _re_st
                _WP_RE = _re_st.compile(r'^-?\d+\.?\d*,-?\d+\.?\d*$')
                for i, wp in enumerate(request.waypoints):
                    wp_stripped = wp.strip()
                    label = (request.waypoint_labels[i]
                             if i < len(request.waypoint_labels) else f"Durak {i+1}")
                    wplat = wplon = None
                    if _WP_RE.match(wp_stripped):
                        try:
                            wplat, wplon = map(float, wp_stripped.split(','))
                        except Exception:
                            pass
                    _enriched_stops.append({
                        "kind": "waypoint",
                        "name": label,
                        "address": wp,
                        "lat": wplat, "lon": wplon, "km": None,
                    })
                _enriched_stops.append({
                    "kind": "destination",
                    "name": request.destination,
                    "address": request.destination,
                    "lat": None, "lon": None,
                    "km": total_km,
                })
                # km'ye göre sırala (None'lar uçlarda kalır)
                _enriched_stops.sort(
                    key=lambda s: (
                        0 if s["kind"] == "origin" else (2 if s["kind"] == "destination" else 1),
                        s["km"] if isinstance(s.get("km"), (int, float)) else 0,
                    )
                )

                import asyncio as _asyncio
                _asyncio.create_task(_PM.save_route_history(
                    origin=origin,
                    destination=request.destination,
                    distance_km=total_km,
                    duration_min=total_min,
                    username=user["username"],
                    polyline_encoded=polyline,
                    waypoints=request.waypoints,
                    waypoint_labels=request.waypoint_labels,
                    weather_summary=weather_summary_short,
                    warnings=weather_warnings or None,
                    narrative=narrative_text,
                    stops=_enriched_stops,
                ))
            except Exception as _e:
                log.warning(f"⚠️ [TripPlan] Zengin kayıt başlatılamadı: {_e}")

        elapsed = int((time.monotonic() - t0) * 1000)

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
        if _severe_weather:
            action_cards.append(ActionCard(
                id="weather_detail", label="Hava Durumu Uyarısı",
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
        import traceback as _tb
        log.error(f"🔥 [TripPlan] Hata: {e}\n{_tb.format_exc()}")
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

    # ── GUARD: Serbest mod (chat_ prefix) bu endpoint'i çağıramaz ─────────
    # Free mode'da POI seçimi rotaya eklenmemeli — yeni rota olarak
    # değerlendirilmeli. Bu savunma hattı eski rotaya patch atılmasını engeller.
    raw_sid = request.session_id or ""
    if raw_sid.startswith("chat_"):
        log.warning(f"🚫 [AddStops] Chat session ({raw_sid}) add_stops çağrısı reddedildi")
        return ApiEnvelope(
            success=False,
            error=ApiError(
                code="ADD_STOPS_FORBIDDEN_IN_FREE_MODE",
                message="Serbest moddan rotaya ekleme yapılamaz. Yeni rota oluşturmak için akıllı rotaya geç.",
            ),
            metadata=ApiMetadata(response_time_ms=0, session_id=session_id),
        )

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

        # ── 3. Seçilen durakları (yemek + yakıt) km sırasına göre diz ───
        if not request.selected_stops:
            raise ValueError("Hiç durak seçilmedi.")

        import re as _re
        # Koordinat formatı: "lat,lon" — opsiyonel "!pt" suffix passthrough işareti
        _COORD_RE = _re.compile(r'^-?\d+\.?\d*,-?\d+\.?\d*(!pt)?$')

        def _strip_pt(w: str) -> str:
            return w[:-3] if w.endswith("!pt") else w

        existing_coord_waypoints = [
            _strip_pt(w.strip()) for w in existing_waypoints
            if isinstance(w, str) and _COORD_RE.match(w.strip())
        ]

        user_stops_sorted = sorted(
            request.selected_stops,
            key=lambda s: float(s.get("distance_along_route_km") or 0),
        )
        user_waypoints_with_dist = [
            (float(s.get("distance_along_route_km") or 0), f"{s['lat']},{s['lon']}", s)
            for s in user_stops_sorted
            if isinstance(s, dict) and "lat" in s and "lon" in s
        ]

        # ── 4. Otomatik mola araması (kullanıcıya sormadan) ────────────
        auto_break_stops: list = []
        break_interval_km_ctx = float(tc.get("break_interval_km") or (float(tc.get("break_interval_hours") or 2.0) * 80))
        total_km_ctx = float(tc.get("total_km") or 0)

        polyline_cached = None
        if orchestrator.redis_client:
            _cp = orchestrator.redis_client.get(f"route:{session_id}")
            if _cp:
                polyline_cached = _cp if isinstance(_cp, str) else _cp.decode("utf-8")

        if break_interval_km_ctx > 0 and total_km_ctx > break_interval_km_ctx and polyline_cached:
            selected_kms = [dist for dist, _, _ in user_waypoints_with_dist]
            break_slots = []
            curr = break_interval_km_ctx
            while curr < total_km_ctx - 30:
                if not any(abs(skm - curr) <= 50 for skm in selected_kms):
                    break_slots.append(curr)
                curr += break_interval_km_ctx

            if break_slots:
                places_tool_brk = orchestrator.get_tool_by_name("search_hybrid_places")
                if places_tool_brk:
                    _BRK_QUERIES = ["benzin istasyonu akaryakıt", "otoyol hizmet alanı mola", "kafe restoran mola"]
                    brk_task_list = []
                    for slot in break_slots:
                        for q in _BRK_QUERIES:
                            brk_task_list.append((
                                slot, q,
                                asyncio.wait_for(
                                    places_tool_brk.ainvoke({
                                        "query": q,
                                        "route_polyline": polyline_cached,
                                        "target_fraction": slot / max(total_km_ctx, 1),
                                    }),
                                    timeout=15.0,
                                ),
                            ))
                    brk_results = await asyncio.gather(*[t[2] for t in brk_task_list], return_exceptions=True)
                    seen_brk: set = set()
                    slot_best: dict = {}
                    for i, br in enumerate(brk_results):
                        if isinstance(br, Exception):
                            continue
                        if isinstance(br, str):
                            try:
                                br = json.loads(br)
                            except Exception:
                                continue
                        if isinstance(br, dict):
                            slot_key = brk_task_list[i][0]
                            if slot_key in slot_best:
                                continue
                            candidates = (
                                br.get("strict_route_places", [])
                                + br.get("relaxed_route_places", [])
                                + br.get("places", [])
                            )
                            for p in candidates[:10]:
                                nm = p.get("name", "")
                                if not nm or nm in seen_brk:
                                    continue
                                if "lat" not in p and "coords" in p:
                                    try:
                                        clat, clon = map(float, p["coords"].split(","))
                                        p["lat"] = clat
                                        p["lon"] = clon
                                    except Exception:
                                        continue
                                if "lat" not in p:
                                    continue
                                if not _is_valid_break_stop(p):
                                    log.debug(f"🚫 [AddStops] Mola reddedildi: {nm}")
                                    continue
                                seen_brk.add(nm)
                                p["type"] = "break_stop"
                                p["distance_along_route_km"] = slot_key
                                slot_best[slot_key] = p
                                break
                    auto_break_stops = list(slot_best.values())
                    log.info(f"☕ [AddStops] Otomatik mola: {len(auto_break_stops)} nokta eklendi (slots={break_slots})")

        # ── 4.5 Tüm waypoint'leri (kullanıcı seçimleri + otomatik molalar) birleştir ──
        # auto_break_stops için passthrough YOK (driver gerçekten duruyor)
        # user_waypoints için "!pt" marker → HERE passThrough=true (sahil/manzara yolu kopmasın)
        auto_with_dist = [
            (float(bs.get("distance_along_route_km") or 0), f"{bs['lat']},{bs['lon']}", bs, False)
            for bs in auto_break_stops
            if "lat" in bs and "lon" in bs
        ]
        user_with_dist_marked = [
            (d, w, s, True) for (d, w, s) in user_waypoints_with_dist
        ]
        all_stops_with_dist = user_with_dist_marked + auto_with_dist
        all_stops_with_dist.sort(key=lambda x: x[0])

        new_waypoints = [
            (f"{w}!pt" if is_user else w)
            for _, w, _, is_user in all_stops_with_dist
        ]
        # Eski waypoint'ler de büyük olasılıkla kullanıcı seçimi — passthrough işaretle
        existing_marked = [
            (w if w.endswith("!pt") else f"{w}!pt")
            for w in existing_coord_waypoints
        ]
        combined_waypoints = existing_marked + new_waypoints
        waypoints_str = "|".join(combined_waypoints)

        if not waypoints_str:
            raise ValueError("Geçerli koordinat bulunamadı.")

        # ── 5. Rotayı güncelle ────────────────────────────────────────────
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

        # ── 6. Marker'ları hazırla (kullanıcı seçimleri + otomatik molalar) ──
        map_markers = []
        all_displayed_stops = [s for _, _, s, _ in all_stops_with_dist]
        for i, s in enumerate(all_displayed_stops):
            if "lat" not in s or "lon" not in s:
                continue
            stype = s.get("type", "waypoint")
            map_markers.append(MapMarker(
                lat=s["lat"], lon=s["lon"],
                title=s.get("name", f"Durak {i+1}"),
                type=stype,
                snippet=s.get("address", "break_stop" if stype == "break_stop" else "Seçilen durak"),
            ))

        normalized_poly = _normalize_polyline(polyline)
        elapsed = int((time.monotonic() - t0) * 1000)

        # ── 7. Zengin yanıt metni (hava + radar dahil) ───────────────────
        weather_ctx = tc.get("weather_warnings") or []
        radar_count_ctx = int(tc.get("radar_count") or 0)
        _severe_ctx = any(w.get("severity") in ("warning", "critical") for w in weather_ctx)

        food_cnt = sum(1 for _, _, s, _ in all_stops_with_dist if s.get("type") not in ("fuel_station", "break_stop"))
        fuel_cnt = sum(1 for _, _, s, _ in all_stops_with_dist if s.get("type") == "fuel_station")
        brk_cnt = len(auto_break_stops)

        parts = []
        if food_cnt:
            parts.append(f"{food_cnt} yemek durağı")
        if fuel_cnt:
            parts.append(f"{fuel_cnt} yakıt durağı")
        if brk_cnt:
            parts.append(f"{brk_cnt} otomatik mola")
        stops_text = " ve ".join(parts) if parts else f"{len(request.selected_stops)} durak"

        weather_line = (
            "\n⚠️ Dikkat: Rota üzerinde olumsuz hava koşulları var, dikkatli sür!" if _severe_ctx
            else ("\n☀️ Hava durumu: Rota boyunca açık ve güvenli." if weather_ctx else "")
        )
        radar_line = f"\n📸 Rota üzerinde {radar_count_ctx} radar noktası var, dikkatli ol." if radar_count_ctx else ""

        reply = (
            f"Rotana {stops_text} eklendi!\n"
            f"Güncellenen rota: {int(total_km)} km, ~{total_min // 60}sa {total_min % 60}dk."
            f"{weather_line}{radar_line}"
        )

        # ── 8. Geçmiş rotaya zenginleştirilmiş stops kaydı ────────────────
        # SADECE kullanıcının seçtiği durakları + otomatik molaları persist et.
        # /trip/plan'da gösterilen öneri kartları (food_markers vb.) BURAYA gelmez —
        # kullanıcı seçim yapmazsa kayda da girmez.
        if user:
            try:
                from profile_manager import ProfileManager as _PM
                _enriched_stops: list = []
                _enriched_stops.append({
                    "kind": "origin", "name": origin, "address": origin,
                    "lat": None, "lon": None, "km": 0,
                })
                for _d, _w, _s, _is_user in all_stops_with_dist:
                    if "lat" not in _s or "lon" not in _s:
                        continue
                    stype = _s.get("type", "waypoint")
                    kind = (
                        "fuel" if stype == "fuel_station"
                        else "rest" if stype == "break_stop"
                        else "food" if stype in ("poi", "food", "restaurant") and not _is_user
                        else "waypoint"
                    )
                    _enriched_stops.append({
                        "kind": kind,
                        "name": _s.get("name") or "Durak",
                        "address": _s.get("address") or _s.get("snippet"),
                        "lat": _s.get("lat"), "lon": _s.get("lon"),
                        "km": _s.get("distance_along_route_km"),
                    })
                _enriched_stops.append({
                    "kind": "destination", "name": destination, "address": destination,
                    "lat": None, "lon": None, "km": total_km,
                })

                import asyncio as _asyncio
                _asyncio.create_task(_PM.save_route_history(
                    origin=origin,
                    destination=destination,
                    distance_km=total_km,
                    duration_min=total_min,
                    username=user["username"],
                    polyline_encoded=polyline,
                    waypoints=combined_waypoints,
                    waypoint_labels=[s.get("name", "") for _, _, s, _ in all_stops_with_dist],
                    weather_summary=None,
                    warnings=weather_ctx or None,
                    narrative=None,
                    stops=_enriched_stops,
                ))
            except Exception as _e:
                log.warning(f"⚠️ [AddStops] Zengin kayıt başlatılamadı: {_e}")

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


# ─────────────────────────────────────────────────────────────────────────────
# HELPER — Google Polyline 2-nokta encoder (day plan için)
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# DAY PLAN ENDPOINT — Event-Anchored Günlük Plan
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/api/v1/day_plan", tags=["Day Planning"])
async def day_plan(request: DayPlanRequest, user: dict = Depends(get_optional_user)):
    """
    📅 Event-Anchored Günlük Plan

    Şehirdeki etkinlik/maç bilgisini anchor olarak kullanarak
    sabahtan akşama bağlantılı bir gün planı üretir.
    Toplu taşıma desteği YOK (araç / yürüyerek).
    Şehir opsiyonel — belirtilmezse mevcut konum merkez alınır.
    """
    import math as _math
    import uuid as _uuid

    t0 = time.monotonic()
    session_id = request.session_id
    if user:
        session_id = f"{user['user_id']}:{request.session_id}"

    # Aramalar için Redis route proxy'den kaçınmak için benzersiz session_id
    search_sid = f"dayplan:{_uuid.uuid4().hex[:12]}"

    city_label = request.city or "mevcut konum"
    log.info(f"📅 [DayPlan] session={session_id} | city={city_label} | date={request.date}")

    try:
        # ── 1. Tool referansları ─────────────────────────────────────────
        events_tool = orchestrator.get_tool_by_name("get_city_events")
        sports_tool = orchestrator.get_tool_by_name("get_sports_events")
        weather_tool = orchestrator.get_tool_by_name("get_weather")
        search_tool  = orchestrator.get_tool_by_name("search_hybrid_places")

        async def _safe_invoke(tool, args: dict, timeout: float = 15.0):
            if not tool:
                return None
            try:
                result = await asyncio.wait_for(tool.ainvoke(args), timeout=timeout)
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except Exception:
                        pass
                return result
            except Exception as exc:
                log.warning(f"[DayPlan] tool hatası: {exc}")
                return None

        # ── 2. Paralel: etkinlikler + maçlar + hava durumu ────────────────
        events_coro = (
            _safe_invoke(events_tool, {"city": request.city})
            if request.city and events_tool
            else asyncio.sleep(0)
        )
        events_res, sports_res, weather_res = await asyncio.gather(
            events_coro,
            _safe_invoke(sports_tool, {}),
            _safe_invoke(weather_tool, {"lat": request.city_lat, "lon": request.city_lon}, timeout=10.0),
        )

        # ── 3. Event/match parse ─────────────────────────────────────────
        events_list: list = []
        if isinstance(events_res, dict):
            events_list = events_res.get("data") or []
        elif isinstance(events_res, list):
            events_list = events_res

        sports_list: list = []
        if isinstance(sports_res, dict):
            sports_list = sports_res.get("data") or []
        elif isinstance(sports_res, list):
            sports_list = sports_res

        try:
            _dp = request.date.split("-")
            req_date_tr = f"{_dp[2]}.{_dp[1]}.{_dp[0]}"
            req_month_year = f"{_dp[1]}.{_dp[0]}"
        except Exception:
            req_date_tr = ""
            req_month_year = ""

        req_city_lower = request.city.lower()

        anchor_match = None
        for m in sports_list:
            m_time = m.get("time", "")
            m_city = m.get("city", "").lower()
            date_ok = req_date_tr and req_date_tr in m_time
            city_ok = (not req_city_lower) or (req_city_lower in m_city or m_city in req_city_lower)
            if date_ok and city_ok:
                anchor_match = m
                break

        anchor_event = None
        if not anchor_match:
            for ev in events_list:
                ev_date = ev.get("date", "")
                if not ev_date or ev_date in ("Belirtilmemiş", ""):
                    continue
                if req_date_tr in ev_date or req_month_year in ev_date:
                    anchor_event = ev
                    break

        event_anchor: dict | None = None
        if anchor_match:
            event_anchor = {
                "type": "sports",
                "title": anchor_match.get("match", "Maç"),
                "time": anchor_match.get("time", ""),
                "venue": anchor_match.get("stadium", ""),
                "city": anchor_match.get("city", city_label),
                "warning": anchor_match.get("warning", ""),
            }
        elif anchor_event:
            event_anchor = {
                "type": "event",
                "title": anchor_event.get("title", "Etkinlik"),
                "time": anchor_event.get("date", ""),
                "venue": anchor_event.get("venue", ""),
                "city": city_label,
                "link": anchor_event.get("link", ""),
            }

        # Ek etkinlikler — anchor dışında o gün için bulunan diğer event'ler.
        # Akşam slot'unda extra kartlar olarak gösterilir.
        extra_events: list[dict] = []
        for ev in events_list:
            if ev is anchor_event:
                continue
            ev_date = ev.get("date", "")
            if not ev_date or ev_date in ("Belirtilmemiş", ""):
                continue
            if req_date_tr in ev_date or (req_month_year and req_month_year in ev_date):
                extra_events.append({
                    "type": "event",
                    "title": ev.get("title", "Etkinlik"),
                    "time": ev.get("date", ""),
                    "venue": ev.get("venue", ""),
                    "link": ev.get("link", ""),
                })
            if len(extra_events) >= 3:
                break

        # ── 3b. TÜM o-gün etkinliklerini (üst banner için) topla ──────────
        # Kullanıcı "konser var mı, tiyatro var mı" diye sormadan otomatik
        # "bugün şehirde X etkinlik var, ilgileniyor musun?" diyebilelim.
        all_events: list[dict] = []
        for m in sports_list:
            m_time = m.get("time", "")
            m_city = m.get("city", "").lower()
            date_ok = req_date_tr and req_date_tr in m_time
            city_ok = (not req_city_lower) or (req_city_lower in m_city or m_city in req_city_lower)
            if date_ok and city_ok:
                all_events.append({
                    "type": "sports",
                    "title": m.get("match", "Maç"),
                    "time": m_time,
                    "venue": m.get("stadium", ""),
                    "city": m.get("city", city_label),
                })
        for ev in events_list:
            ev_date = ev.get("date", "")
            if not ev_date or ev_date in ("Belirtilmemiş", ""):
                continue
            if req_date_tr in ev_date or (req_month_year and req_month_year in ev_date):
                all_events.append({
                    "type": "event",
                    "title": ev.get("title", "Etkinlik"),
                    "time": ev_date,
                    "venue": ev.get("venue", ""),
                    "link": ev.get("link", ""),
                })
            if len(all_events) >= 8:  # üst banner'da aşırı kalabalık olmasın
                break

        # ── 4. Hava durumu özeti ─────────────────────────────────────────
        weather_summary = ""
        if isinstance(weather_res, dict):
            anlik = weather_res.get("ANLIK_DURUM") or weather_res
            if isinstance(anlik, dict):
                durum = anlik.get("durum") or anlik.get("condition") or ""
                temp  = anlik.get("sicaklik") or anlik.get("temperature") or ""
                if durum or temp:
                    weather_summary = f"{temp}°C, {durum}".strip(", ")

        # ── 5. Hedef nokta belirleme ─────────────────────────────────────
        # Anchor venue varsa onu hedef yap, yoksa şehir merkezi (yoksa mevcut konum)
        target_lat = request.city_lat
        target_lon = request.city_lon
        venue_name = ""
        if event_anchor and event_anchor.get("venue"):
            venue_name = event_anchor["venue"]

        # WALK modu: kullanıcı yürüyerek gezecek — şehir merkezine kadar
        # interpolation YAPMA, current konum etrafında kal. Slot çeşitliliği
        # için ufak offset (~500m) her slot için yeter.
        is_walk = request.transport_mode == "walk"
        if is_walk:
            target_lat = request.current_lat
            target_lon = request.current_lon
            # Akşam anchor (event venue) varsa onu koru, gerisinde current konum
            # (event venue genelde yürüme mesafesinde değil ama venue_name ile
            # search_hybrid_places location_name'i o noktayı bulur).

        # CAR modu: şehir merkezine kadar interpolation. Mevcut konum zaten
        # şehir merkezine çok yakınsa offset ekle.
        if not is_walk:
            if abs(target_lat - request.current_lat) < 0.005 and abs(target_lon - request.current_lon) < 0.005:
                target_lat += 0.015   # ~1.6 km
                target_lon += 0.015

        # ── 6. Mesafe filtresi (şehir sınırı) ────────────────────────────
        # Walk: yürüme mesafesi (4km), Car: çevre ilçeler dahil (30km)
        max_km = 3.5 if is_walk else 30.0

        # Walk modunda mesafe ölçüm noktası kullanıcının bulunduğu yer;
        # Car modunda şehir merkezi (çevre ilçeler de dahil olsun).
        ref_lat = request.current_lat if is_walk else request.city_lat
        ref_lon = request.current_lon if is_walk else request.city_lon

        def _within_city(p: dict) -> bool:
            try:
                plat = float(p.get("lat") or 0)
                plon = float(p.get("lon") or 0)
                if plat == 0 and plon == 0:
                    return False
                phi1 = _math.radians(ref_lat)
                phi2 = _math.radians(plat)
                dphi = _math.radians(plat - ref_lat)
                dlam = _math.radians(plon - ref_lon)
                a = (_math.sin(dphi / 2) ** 2
                     + _math.cos(phi1) * _math.cos(phi2) * _math.sin(dlam / 2) ** 2)
                dist_km = 2 * 6371 * _math.asin(_math.sqrt(a))
                return dist_km <= max_km
            except Exception:
                return True

        # ── 7. LLM-driven dinamik slot üretimi ────────────────────────────
        from datetime import datetime as _dt, date as _date
        import re as _re_dp
        note = (request.activity_note or "").strip()
        try:
            req_date = _dt.strptime(request.date, "%Y-%m-%d").date()
        except Exception:
            req_date = _date.today()
        now = _dt.now()
        is_today = (req_date == now.date())
        now_hhmm = now.strftime("%H:%M") if is_today else ""

        # Varsayılan KATEGORİ-bazlı slotlar (LLM çıktısı yoksa veya çağrı başarısızsa).
        # Sabah/Öğle/Akşam zaman dilimleri yerine kullanıcıya "ne tarz mekan
        # istiyorsun?" sorusunu cevaplayan kategori başlıkları.
        _DEFAULT_SLOTS: List[dict] = [
            {"label": "☕ Kafeler", "start_time": "", "end_time": "",
             "intent_query": "kafe sessiz kahve",
             "narrative_intro": "Sakin bir kahve molası ister misin? Sessiz, manzaralı yerleri seçtim.",
             "location_hint": None, "is_locked": False},
            {"label": "🍽️ Yemek", "start_time": "", "end_time": "",
             "intent_query": "restoran lokanta yemek",
             "narrative_intro": "Acıkınca uğrayabileceğin yerel mekanlar.",
             "location_hint": None, "is_locked": False},
            {"label": "🏖️ Sahil & Doğa", "start_time": "", "end_time": "",
             "intent_query": "sahil park yürüyüş manzara",
             "narrative_intro": "Bol oksijen, sahil-kıyı veya park önerileri.",
             "location_hint": None, "is_locked": False},
            {"label": "🎭 Kültür", "start_time": "", "end_time": "",
             "intent_query": "müze sanat galerisi tarihi yer",
             "narrative_intro": "Şehrin tarihine ve sanatına dokunmak istersen.",
             "location_hint": None, "is_locked": False},
            {"label": "🌃 Akşam", "start_time": "", "end_time": "",
             "intent_query": "canlı müzik bar akşam mekan",
             "narrative_intro": "Akşam keyfi için canlı müzik ve gece mekanları.",
             "location_hint": venue_name, "is_locked": False},
        ]

        def _filter_past(slots: List[dict]) -> List[dict]:
            """
            Slot'lar artık varsayılan kategori başlıklı (saatten bağımsız).
            Kullanıcı LLM çıktısında belirli start/end time belirtirse,
            bitişi şimdiden önce olan slot'ları atla (sadece bugün için).
            """
            if not is_today:
                return slots
            out = []
            for s in slots:
                start_time = s.get("start_time") or ""
                end_time = s.get("end_time") or ""
                if not start_time and not end_time:
                    # Zamansız kategori — her zaman göster
                    out.append(s)
                    continue
                try:
                    end_h, end_m = map(int, end_time.split(":"))
                    if (end_h, end_m) > (now.hour, now.minute):
                        out.append(s)
                except Exception:
                    out.append(s)
            return out

        async def _generate_dynamic_slots() -> List[dict]:
            """
            Kullanıcı notunu LLM ile kategori-bazlı + anlatıcı slot listesine çevir.
            Sabit zaman dilimleri YOK — kullanıcı saat belirttiyse korunur, etmediyse
            sadece kategori başlığı kullanılır ("☕ Kafeler", "🏖️ Sahil & Doğa").
            """
            if not orchestrator.llm_gemini:
                return _DEFAULT_SLOTS
            if not note:
                return _DEFAULT_SLOTS

            system_prompt = (
                "Sen bir günlük plan asistanısın. Kullanıcı bir gün için ne tarz "
                "mekanlara gitmek istediğini yazıyor. Cevabını **KATEGORİ + "
                "ANLATICI** olarak ver — saat blokları DEĞİL.\n\n"
                "Her kategori JSON nesnesi:\n"
                "{\n"
                '  "label": "kategori başlığı emoji+kelime (örn. \\"☕ Kafeler\\", \\"🏖️ Sahil & Doğa\\", \\"🎭 Kültür\\")",\n'
                '  "start_time": "" veya "HH:MM" (kullanıcı saat belirttiyse),\n'
                '  "end_time": "" veya "HH:MM",\n'
                '  "intent_query": "google places araması için 2-4 anahtar kelime (Türkçe)",\n'
                '  "narrative_intro": "1 cümle samimi anlatım: \\"Sahil kenarında sessiz bir kahve molası ister misin?\\"",\n'
                '  "location_hint": "konum ipucu veya null",\n'
                '  "is_locked": true/false  // kullanıcının kesin etkinliği mi\n'
                "}\n\n"
                "KURALLAR:\n"
                "- Sabit \"Sabah/Öğle/Öğleden Sonra/Akşam\" etiketi KULLANMA. "
                "Bunun yerine \"☕ Kafeler\", \"🍽️ Yemek\", \"🏖️ Sahil & Doğa\", "
                "\"🎭 Kültür\", \"🌃 Akşam Mekanları\" gibi kategori başlıkları.\n"
                "- Kullanıcı 'saat 14'te X'te 2 saatlik iş' derse → bir locked blok "
                "(start_time=14:00, end_time=16:00, label='X (iş)', is_locked=true, "
                "intent_query=null).\n"
                "- narrative_intro her kategori için ZORUNLU — kullanıcıya hitap eden, "
                "samimi 1 cümle. Robotik 'Sabah aktiviteleri:' formatı YASAK.\n"
                "- 3-6 kategori üret. Kullanıcı tek bir tip ister gibiyse (örn. sadece "
                "kahve) 2-3 alt-kategoriye böl (kahve + manzara + kitap kafe gibi).\n"
                "- Sadece JSON array dön, başka metin YOK.\n"
            )
            user_prompt = (
                f"Tarih: {request.date} ({'BUGÜN' if is_today else 'GELECEK'})\n"
                f"Şu an: {now_hhmm or '—'}\n"
                f"Ulaşım: {request.transport_mode}\n"
                f"Şehir: {city_label}\n"
                f"Kullanıcı notu: \"{note}\"\n\n"
                f"JSON kategori array'ini üret:"
            )
            try:
                text = await _llm_invoke_with_fallback(
                    [
                        SystemMessage(content=system_prompt),
                        HumanMessage(content=user_prompt),
                    ],
                    timeout=15.0,
                )
                # JSON array bul
                m = _re_dp.search(r"\[\s*\{.*\}\s*\]", text, _re_dp.DOTALL)
                if not m:
                    log.warning("[DayPlan/LLM] JSON array bulunamadı, default'a düşülüyor")
                    return _filter_past(_DEFAULT_SLOTS)
                parsed = json.loads(m.group(0))
                if not isinstance(parsed, list) or not parsed:
                    return _filter_past(_DEFAULT_SLOTS)
                # Şema kontrol + normalize
                cleaned: List[dict] = []
                for s in parsed[:6]:
                    if not isinstance(s, dict):
                        continue
                    label = (s.get("label") or "").strip() or "Slot"
                    start_time = (s.get("start_time") or "").strip()
                    end_time = (s.get("end_time") or "").strip()
                    # start/end_time opsiyonel; format yanlış olursa boş bırak
                    if start_time and not _re_dp.match(r"^\d{1,2}:\d{2}$", start_time):
                        start_time = ""
                    if end_time and not _re_dp.match(r"^\d{1,2}:\d{2}$", end_time):
                        end_time = ""
                    cleaned.append({
                        "label": label,
                        "start_time": start_time,
                        "end_time": end_time,
                        "intent_query": (s.get("intent_query") or "").strip() or None,
                        "narrative_intro": (s.get("narrative_intro") or "").strip() or None,
                        "location_hint": (s.get("location_hint") or "").strip() or None,
                        "is_locked": bool(s.get("is_locked")),
                    })
                if not cleaned:
                    return _filter_past(_DEFAULT_SLOTS)
                return _filter_past(cleaned)
            except Exception as e:
                log.warning(f"[DayPlan/LLM] slot üretimi hata: {e}")
                return _filter_past(_DEFAULT_SLOTS)

        dynamic_slots = await _generate_dynamic_slots()

        def _slot_log_label(s: dict) -> str:
            base = s.get("label", "?")
            st = s.get("start_time") or ""
            et = s.get("end_time") or ""
            time_part = f" {st}-{et}" if (st or et) else ""
            lock = " 🔒" if s.get("is_locked") else ""
            return f"{base}{time_part}{lock}"

        log.info(f"📅 [DayPlan/Slots] {len(dynamic_slots)} dinamik slot üretildi: "
                 f"{[_slot_log_label(s) for s in dynamic_slots]}")

        # İsim-bazlı kategori filtresi: yeme-içme niyetinde sanat merkezi/müze ele.
        _FOOD_DRINK_KEYS = ("kafe", "kahve", "restoran", "lokanta", "yemek", "bistro", "kahvaltı")
        _CULTURE_BLOCK_KEYS = ("sanat merkezi", "sanat galerisi", "müze", "müzesi", "galeri", "kütüphane", "tiyatro")
        def _name_matches_intent(query: str, place: dict) -> bool:
            q = (query or "").lower()
            name = (place.get("name") or "").lower()
            if any(k in q for k in _FOOD_DRINK_KEYS):
                if any(b in name for b in _CULTURE_BLOCK_KEYS):
                    return False
            return True

        # ── 8. Slot başına arama: her slot kendi intent_query + location_hint'iyle ──
        def _lerp(a: float, b: float, t: float) -> float:
            return a + (b - a) * t

        def _slot_fraction(idx: int, total: int) -> float:
            """Sıralı slotu rota üzerinde dağıt (0..1)."""
            if total <= 1:
                return 0.0
            return idx / (total - 1)

        async def _search_dynamic_slot(slot: dict, idx: int, total: int) -> list:
            if not search_tool:
                return []
            if slot.get("is_locked"):
                return []  # kilitli slot — arama gerekmez
            query = slot.get("intent_query") or "gezilecek yer"
            location_hint = slot.get("location_hint")
            frac = _slot_fraction(idx, total)
            s_lat = _lerp(request.current_lat, target_lat, frac)
            s_lon = _lerp(request.current_lon, target_lon, frac)

            args = {
                "query": query,
                "lat": s_lat,
                "lon": s_lon,
                "location_name": location_hint,
                "session_id": search_sid,
            }
            args = {k: v for k, v in args.items() if v is not None}
            result = await _safe_invoke(search_tool, args, timeout=20.0)
            if not result:
                return []
            raw = (
                result.get("places")
                or result.get("strict_route_places")
                or (result if isinstance(result, list) else [])
            )
            return [
                p for p in raw
                if p.get("name") and _within_city(p) and _name_matches_intent(query, p)
            ][:4]

        # Dinamik slotlar için paralel arama
        slot_results = await asyncio.gather(*[
            _search_dynamic_slot(s, i, len(dynamic_slots))
            for i, s in enumerate(dynamic_slots)
        ])

        # Akşam slotunu belirle: en geç başlayan veya 18:00+ başlayan
        def _is_evening_slot(s: dict) -> bool:
            try:
                h = int(s["start_time"].split(":")[0])
                return h >= 18
            except Exception:
                return False

        # ── 9. Sections oluştur ───────────────────────────────────────────
        sections: List[DayPlanSection] = []
        evening_idx = None
        for i, s in enumerate(dynamic_slots):
            if _is_evening_slot(s):
                evening_idx = i

        for i, slot in enumerate(dynamic_slots):
            places = slot_results[i]
            is_evening = (i == evening_idx)
            is_event_slot = (is_evening and event_anchor is not None)
            slot_label = slot["label"]
            st = slot.get("start_time") or ""
            et = slot.get("end_time") or ""
            time_range = f"{st} – {et}" if (st and et) else ""
            is_locked = bool(slot.get("is_locked"))

            cards: List[DayPlanCard] = []

            # Kilitli (locked) slot — kullanıcının kesin etkinliği
            if is_locked:
                cards.append(DayPlanCard(
                    name=slot.get("location_hint") or slot_label,
                    address=slot.get("location_hint") or "",
                    lat=target_lat,
                    lon=target_lon,
                    description=f"{time_range} — kullanıcı planı",
                    category="locked",
                    is_anchor=True,
                ))

            # Akşam anchor event kartı (kilitli) — varsa
            if is_event_slot:
                cards.append(DayPlanCard(
                    name=event_anchor["title"],
                    address=event_anchor.get("venue", ""),
                    lat=target_lat,
                    lon=target_lon,
                    description=event_anchor.get("time", ""),
                    category="event",
                    is_anchor=True,
                ))

            # Akşam slot'unda ek etkinlikler — anchor olmasa bile bu kartlar görünür
            if is_evening:
                for ev in extra_events:
                    cards.append(DayPlanCard(
                        name=ev["title"],
                        address=ev.get("venue") or "",
                        lat=target_lat,
                        lon=target_lon,
                        description=ev.get("time") or "",
                        category="event",
                        is_anchor=False,
                    ))

            # Toplu micro-narrative: slot içindeki place'leri tek LLM çağrısıyla
            # 1-2 cümle açıklama ile zenginleştir.
            place_narratives: dict = {}
            if orchestrator.llm_gemini and places:
                try:
                    name_list = [p.get("name") for p in places if p.get("name")][:5]
                    if name_list:
                        narrative_system = (
                            "Sen sıcak, samimi bir öneri asistanısın. Verilen mekan listesi "
                            "için her birine **1-2 kısa cümlelik** açıklama yaz — atmosfer, "
                            "ne için uygun, hangi durumda iyi gider gibi.\n"
                            "Çıktı SADECE JSON object olsun: {\"Mekan Adı\": \"açıklama\", ...}. "
                            "Robotik 'X mekanı bir restorandır' yazma — kullanıcıyla konuşur gibi."
                        )
                        narrative_user = (
                            f"Kategori: {slot.get('label', '')}\n"
                            f"Niyet: {slot.get('intent_query', '')}\n"
                            f"Şehir: {city_label}\n"
                            f"Mekanlar:\n" + "\n".join(f"- {n}" for n in name_list)
                        )
                        text_n = await _llm_invoke_with_fallback(
                            [
                                SystemMessage(content=narrative_system),
                                HumanMessage(content=narrative_user),
                            ],
                            timeout=12.0,
                        )
                        m_n = _re_dp.search(r"\{[\s\S]*\}", text_n)
                        if m_n:
                            try:
                                place_narratives = json.loads(m_n.group(0))
                            except Exception:
                                place_narratives = {}
                except Exception as e:
                    log.warning(f"[DayPlan/PlaceNarrative] {slot.get('label')} hata: {e}")
                    place_narratives = {}

            for p in places:
                p_name = p.get("name", "")
                cards.append(DayPlanCard(
                    name=p_name,
                    address=p.get("address") or "",
                    lat=float(p.get("lat") or 0),
                    lon=float(p.get("lon") or 0),
                    rating=p.get("rating"),
                    description=(place_narratives.get(p_name) or "").strip(),
                    category=(slot.get("intent_query") or "poi").split()[0] if slot.get("intent_query") else "poi",
                ))

            if not cards:
                continue

            sections.append(DayPlanSection(
                slot=slot_label,
                time_range=time_range,
                cards=cards[:5],
                is_event_slot=is_event_slot,
                event_detail=event_anchor if is_event_slot else None,
                is_locked=is_locked,
                intent=slot.get("intent_query"),
                narrative_intro=slot.get("narrative_intro"),
            ))

        # ── 10. Narrative ─────────────────────────────────────────────────
        if event_anchor:
            narrative = (
                f"Bugün {city_label}'da {event_anchor['title']} var. "
                f"Gün mevcut konumundan başlayıp etkinliğe doğru ilerliyor."
            )
        else:
            narrative = (
                f"{city_label} için sabahtan akşama bir plan hazırlandı."
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        log.info(
            f"✅ [DayPlan] Tamamlandı {elapsed}ms | {len(sections)} bölüm "
            f"| anchor={bool(event_anchor)} | max_km={max_km}"
        )

        return ApiEnvelope(
            success=True,
            data=DayPlanResponse(
                success=True,
                date=request.date,
                city=city_label,
                weather_summary=weather_summary,
                event_anchor=event_anchor,
                sections=sections,
                narrative=narrative,
                all_events=all_events,
            ).model_dump(),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )

    except Exception as e:
        log.error(f"🔥 [DayPlan] Hata: {e}")
        elapsed = int((time.monotonic() - t0) * 1000)
        return ApiEnvelope(
            success=False,
            error=ApiError(code="DAY_PLAN_ERROR", message=str(e)),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )


# ─────────────────────────────────────────────────────────────────────────────
# DAY PLAN SCHEDULE — Seçilen mekanlar + orijinal not → saat-saat program
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/api/v1/day_plan/schedule", tags=["Day Planning"])
async def day_plan_schedule(
    request: DayPlanScheduleRequest, user: dict = Depends(get_optional_user)
):
    """
    📅 İkinci aşama: kategori önerilerinden seçilen mekanları + kullanıcının
    orijinal notunu LLM'e verip **saat-saat program** üretir.

    Mekan seçimi BOŞ olabilir — bu durumda LLM sadece kullanıcı notuna göre
    boş bir iskelet kurar (locked etkinlikler + serbest zaman).
    """
    import re as _re_sch
    from datetime import datetime as _dt, date as _date

    t0 = time.monotonic()
    session_id = request.session_id
    if user:
        session_id = f"{user['user_id']}:{request.session_id}"

    log.info(
        f"📅 [Schedule] session={session_id} | date={request.date} | "
        f"{len(request.selected_places)} mekan | note={request.activity_note[:50]!r}"
    )

    try:
        # Now bilgisi (bugün için ise saat penceresinde ilerleyen blok üret)
        try:
            req_date = _dt.strptime(request.date, "%Y-%m-%d").date()
        except Exception:
            req_date = _date.today()
        now = _dt.now()
        is_today = (req_date == now.date())
        now_hhmm = now.strftime("%H:%M") if is_today else ""

        # Seçim metnini LLM için derli toplu hazırla
        places_text = ""
        if request.selected_places:
            lines = []
            for p in request.selected_places:
                line = f"- {p.name}"
                if p.category:
                    line += f" [{p.category}]"
                if p.address:
                    line += f" — {p.address}"
                if p.lat is not None and p.lon is not None:
                    line += f" ({p.lat:.4f},{p.lon:.4f})"
                lines.append(line)
            places_text = "\n".join(lines)
        else:
            places_text = "(Kullanıcı hiç mekan seçmedi — programı sadece notuna göre kur)"

        if not orchestrator.llm_gemini:
            return ApiEnvelope(
                success=False,
                error=ApiError(code="LLM_UNAVAILABLE", message="LLM mevcut değil."),
                metadata=ApiMetadata(response_time_ms=0, session_id=session_id),
            )

        system_prompt = (
            "Sen kişisel günlük plan asistanısın. Kullanıcının notundaki "
            "kısıtlamaları (saat+yer belirtilen kesin etkinlikler) önce sabitler, "
            "sonra seçtiği mekanları o günün akışına mantıklı bir sırayla "
            "yerleştirirsin. Mekanlar arası geçiş süresini de hesaba katarsın.\n\n"
            "ÇIKTI **SADECE JSON OBJECT** olmalı:\n"
            "{\n"
            '  "summary": "Programın 1-2 cümle özeti (samimi, doğal).",\n'
            '  "schedule": [\n'
            "    {\n"
            '      "time": "HH:MM",\n'
            '      "end_time": "HH:MM" (opsiyonel),\n'
            '      "name": "Mekan/Etkinlik adı",\n'
            '      "address": "Adres" (opsiyonel),\n'
            '      "type": "locked | place | meal | travel | free",\n'
            '      "duration_min": 60,\n'
            '      "note": "Samimi 1 cümle — neden bu saat, ne yapacaksın",\n'
            '      "lat": float (varsa), "lon": float (varsa)\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "KURALLAR:\n"
            "- Kullanıcı 'saat 14'te X'te 2 saatlik iş' derse: type=locked, "
            "  time=14:00, end_time=16:00, name='X (iş)'.\n"
            "- Mekan seçimleri 'locked' bloklarla çakışmamalı. Locked'ı koru.\n"
            "- Her schedule item için **note** ZORUNLU: 'Sahil kenarında bir kahve "
            "  molası — molaların arasında nefes al.' gibi samimi 1 cümle.\n"
            "- type='meal' öğle/akşam yemeği için (saat 12-14 öğle, 19-21 akşam).\n"
            "- type='travel' uzun yol/geçiş için (>30dk seyahat).\n"
            "- type='free' kullanıcının açıklamasız serbest zamanı için.\n"
            "- BUGÜN için sorgu yapılırsa şu andan ÖNCEKİ saatlere blok koyma.\n"
            "- Mekanları kategori sırasına göre değil, **günün akışına** göre diz "
            "  (sabah→öğle→akşam mantığıyla).\n"
            "- JSON dışında metin yazma."
        )
        user_prompt = (
            f"Tarih: {request.date} ({'BUGÜN' if is_today else 'GELECEK'})\n"
            f"Şu an: {now_hhmm or '—'}\n"
            f"Şehir: {request.city or '—'}\n"
            f"Ulaşım: {request.transport_mode}\n"
            f"Kullanıcı notu:\n\"{request.activity_note}\"\n\n"
            f"Seçilen mekanlar:\n{places_text}\n\n"
            f"JSON programı üret:"
        )

        text = await _llm_invoke_with_fallback(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_prompt),
            ],
            timeout=25.0,
        )
        m = _re_sch.search(r"\{[\s\S]*\}", text)
        if not m:
            log.warning("[Schedule] LLM JSON object dönmedi")
            return ApiEnvelope(
                success=False,
                error=ApiError(code="LLM_PARSE_ERROR", message="Program üretilemedi."),
                metadata=ApiMetadata(response_time_ms=0, session_id=session_id),
            )
        try:
            parsed = json.loads(m.group(0))
        except Exception as e:
            log.warning(f"[Schedule] JSON parse hata: {e}")
            return ApiEnvelope(
                success=False,
                error=ApiError(code="LLM_PARSE_ERROR", message="Program JSON geçersiz."),
                metadata=ApiMetadata(response_time_ms=0, session_id=session_id),
            )

        summary = (parsed.get("summary") or "").strip()
        raw_schedule = parsed.get("schedule") or []
        schedule_items: List[ScheduleItem] = []
        for it in raw_schedule:
            if not isinstance(it, dict):
                continue
            time_v = (it.get("time") or "").strip()
            if not _re_sch.match(r"^\d{1,2}:\d{2}$", time_v):
                continue
            end_v = (it.get("end_time") or "").strip() or None
            if end_v and not _re_sch.match(r"^\d{1,2}:\d{2}$", end_v):
                end_v = None
            try:
                lat_v = float(it["lat"]) if it.get("lat") is not None else None
            except Exception:
                lat_v = None
            try:
                lon_v = float(it["lon"]) if it.get("lon") is not None else None
            except Exception:
                lon_v = None
            try:
                dur_v = int(it["duration_min"]) if it.get("duration_min") is not None else None
            except Exception:
                dur_v = None
            schedule_items.append(ScheduleItem(
                time=time_v,
                end_time=end_v,
                name=(it.get("name") or "Etkinlik").strip(),
                address=(it.get("address") or None),
                type=(it.get("type") or "place").strip(),
                duration_min=dur_v,
                note=(it.get("note") or None),
                lat=lat_v,
                lon=lon_v,
            ))

        elapsed = int((time.monotonic() - t0) * 1000)
        log.info(f"✅ [Schedule] {len(schedule_items)} öğelik program {elapsed}ms")

        # ── History'e kaydet (giriş yapmış kullanıcılar için) ────────────
        if user and schedule_items:
            try:
                from core.db import async_session_maker, User, DayPlanHistory
                from sqlmodel import select as _sm_select
                from uuid import UUID as _UUID

                async def _save_history() -> None:
                    try:
                        async with async_session_maker() as s:
                            res = await s.execute(
                                _sm_select(User).where(
                                    User.id == _UUID(user["user_id"])
                                )
                            )
                            db_user = res.scalars().first()
                            if not db_user:
                                return
                            row = DayPlanHistory(
                                user_id=db_user.id,
                                plan_date=request.date,
                                city=request.city or None,
                                activity_note=request.activity_note or None,
                                summary=summary or None,
                                schedule=[it.model_dump() for it in schedule_items],
                            )
                            s.add(row)
                            await s.commit()
                    except Exception as _e:
                        log.warning(f"⚠️ [Schedule/Save] {_e}")

                asyncio.create_task(_save_history())
            except Exception as _e:
                log.warning(f"⚠️ [Schedule/SaveSetup] {_e}")

        return ApiEnvelope(
            success=True,
            data=DayPlanScheduleResponse(
                success=True,
                date=request.date,
                summary=summary,
                schedule=schedule_items,
            ).model_dump(),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )
    except Exception as e:
        log.error(f"🔥 [Schedule] Hata: {e}")
        elapsed = int((time.monotonic() - t0) * 1000)
        return ApiEnvelope(
            success=False,
            error=ApiError(code="SCHEDULE_ERROR", message=str(e)),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )


# ─────────────────────────────────────────────────────────────────────────────
# REPLACE STOP ENDPOINT — Rotadaki bir durağı LLM aramasıyla değiştir
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/api/v1/route/replace_stop", response_model=ApiEnvelope, tags=["Trip Planning"])
async def replace_stop(request: ReplaceStopRequest, user: dict = Depends(get_optional_user)):
    """
    🔄 Rotadaki bir durağı kullanıcının serbest metin talebine göre değiştirir.

    Akış:
      1. Mevcut durağın etrafında search_hybrid_places(query) ile yakın mekanları bul
      2. LLM ile en uygun ana öneriyi seç (mesafe + rating + intent uyumu)
      3. Yeni öneri + 2 alternatif dön — kullanıcı seçer
      4. Kullanıcı onaylayınca FE add_stops/trip_plan ile rotayı yeniden hesaplar
    """
    import math as _math
    t0 = time.monotonic()
    session_id = request.session_id
    if user:
        session_id = f"{user['user_id']}:{request.session_id}"

    log.info(
        f"🔄 [ReplaceStop] session={session_id} | stop_index={request.stop_index} | "
        f"query='{request.query}' | {len(request.waypoints)} waypoint"
    )

    try:
        # ── 1. Hedef durağın koordinatı (arama merkezi) ─────────────────
        target_lat = request.current_lat
        target_lon = request.current_lon
        if 0 <= request.stop_index < len(request.waypoints):
            wp_raw = request.waypoints[request.stop_index]
            wp_clean = wp_raw[:-3] if wp_raw.endswith("!pt") else wp_raw
            try:
                target_lat, target_lon = map(float, wp_clean.split(","))
            except Exception:
                pass

        # ── 2. Aramayı yap — proxy karışmasın diye temiz session_id ────
        import uuid as _uuid
        search_sid = f"replace:{_uuid.uuid4().hex[:10]}"
        search_tool = orchestrator.get_tool_by_name("search_hybrid_places")
        if not search_tool:
            return ApiEnvelope(
                success=False,
                error=ApiError(code="TOOL_MISSING", message="Arama aracı bulunamadı."),
                metadata=ApiMetadata(response_time_ms=int((time.monotonic() - t0) * 1000)),
            )

        try:
            raw_search = await asyncio.wait_for(
                search_tool.ainvoke({
                    "query": request.query,
                    "lat": target_lat,
                    "lon": target_lon,
                    "session_id": search_sid,
                }),
                timeout=15.0,
            )
        except Exception as exc:
            log.warning(f"⚠️ [ReplaceStop] Arama hatası: {exc}")
            raw_search = None

        if isinstance(raw_search, str):
            try:
                raw_search = json.loads(raw_search)
            except Exception:
                raw_search = None

        places: list = []
        if isinstance(raw_search, dict):
            places = (
                raw_search.get("places")
                or raw_search.get("strict_route_places")
                or []
            )
        elif isinstance(raw_search, list):
            places = raw_search

        # En yakın 5 sonucu ele (haversine ile mesafe)
        def _dist_km(p: dict) -> float:
            try:
                plat = float(p.get("lat") or 0)
                plon = float(p.get("lon") or 0)
                phi1 = _math.radians(target_lat)
                phi2 = _math.radians(plat)
                dphi = _math.radians(plat - target_lat)
                dlam = _math.radians(plon - target_lon)
                a = (_math.sin(dphi / 2) ** 2
                     + _math.cos(phi1) * _math.cos(phi2) * _math.sin(dlam / 2) ** 2)
                return 2 * 6371 * _math.asin(_math.sqrt(a))
            except Exception:
                return 9999

        scored = [(p, _dist_km(p)) for p in places if p.get("name")]
        scored.sort(key=lambda x: x[1])
        top = scored[:5]

        if not top:
            return ApiEnvelope(
                success=True,
                data=ReplaceStopResponse(
                    success=False,
                    reason="Bu civarda eşleşen bir mekan bulamadım.",
                ).model_dump(),
                metadata=ApiMetadata(response_time_ms=int((time.monotonic() - t0) * 1000), session_id=session_id),
            )

        # ── 3. LLM seçimi — top 5 içinden en uygun olanı seç ─────────────
        reason_text: Optional[str] = None
        picked_idx = 0
        try:
            from langchain_core.messages import SystemMessage, HumanMessage
            candidates_block = "\n".join(
                f"{i}. {p.get('name')} — {p.get('address', '')} "
                f"(rating: {p.get('rating', '—')}, {dist:.1f} km uzakta)"
                for i, (p, dist) in enumerate(top)
            )
            sys = (
                "Sen GeoIntel rota asistanısın. Kullanıcı rotadaki bir durağı değiştirmek "
                "istiyor. Adaylar arasından **en uygun olanı tek bir indexle** seç ve "
                "tek cümleyle gerekçesini yaz. Format:\n"
                "INDEX: <0-N>\nGEREKÇE: <tek cümle>"
            )
            usr = (
                f"Kullanıcı isteği: \"{request.query}\"\n\n"
                f"Adaylar:\n{candidates_block}"
            )
            res = await asyncio.wait_for(
                orchestrator.llm_gemini.ainvoke([
                    SystemMessage(content=sys),
                    HumanMessage(content=usr),
                ]),
                timeout=10.0,
            )
            text = res.content if hasattr(res, "content") else str(res)
            if isinstance(text, list):
                text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
            text = str(text or "").strip()
            for line in text.splitlines():
                if line.upper().startswith("INDEX"):
                    try:
                        picked_idx = int(line.split(":", 1)[1].strip())
                    except Exception:
                        pass
                elif line.upper().startswith("GEREKÇE") or line.upper().startswith("GEREKCE"):
                    reason_text = line.split(":", 1)[1].strip() if ":" in line else None
            picked_idx = max(0, min(picked_idx, len(top) - 1))
        except Exception as e:
            log.warning(f"⚠️ [ReplaceStop] LLM seçim hatası: {e}")
            picked_idx = 0  # mesafe sıralamasından en yakını

        def _to_suggestion(p: dict) -> ReplaceStopSuggestion:
            return ReplaceStopSuggestion(
                name=str(p.get("name", "")),
                address=p.get("address"),
                lat=float(p.get("lat") or 0),
                lon=float(p.get("lon") or 0),
                rating=(p.get("rating") if isinstance(p.get("rating"), (int, float)) else None),
                description=p.get("description"),
            )

        suggestion = _to_suggestion(top[picked_idx][0])
        alternates = [
            _to_suggestion(p) for i, (p, _) in enumerate(top) if i != picked_idx
        ][:3]

        elapsed = int((time.monotonic() - t0) * 1000)
        log.info(f"✅ [ReplaceStop] Önerildi: {suggestion.name} ({elapsed}ms)")

        return ApiEnvelope(
            success=True,
            data=ReplaceStopResponse(
                success=True,
                suggestion=suggestion,
                alternates=alternates,
                reason=reason_text,
            ).model_dump(),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )

    except Exception as e:
        log.error(f"🔥 [ReplaceStop] Hata: {e}")
        elapsed = int((time.monotonic() - t0) * 1000)
        return ApiEnvelope(
            success=False,
            error=ApiError(code="REPLACE_STOP_ERROR", message=str(e)),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )
