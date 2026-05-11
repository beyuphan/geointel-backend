"""
routes.py — v3.0 (Mobil API + Geriye Uyumlu)

Endpoint'ler:
- POST /chat            → Eski format (geriye uyumluluk)
- POST /api/v1/chat     → Yeni standart envelope (mobil app)
- POST /location/update → Eski konum güncelleme
- POST /api/v1/location/update → Yeni konum güncelleme (auth'lu)
- WS   /ws/chat/{id}    → Streaming WebSocket
- GET  /health          → Sağlık kontrolü
"""
import json
import asyncio
import time
from typing import Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from pydantic import BaseModel
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.graph import StateGraph, END
from logger import log
from core.graph import intent_node, agent_node, custom_tool_node, should_continue, AgentState
from core.mcp_client import orchestrator
from api.schemas import (
    ApiResponse as ApiEnvelope, ApiError, ApiMetadata,
    ChatResponse, MapData, MapMarker, ActionCard,
    ChatRequest as ChatRequestV1, LocationUpdateRequest as LocUpdateV1,
    PoiOverlay, PoiOverlayCard, RoutingPhaseInfo,
)
from api.deps import get_current_user, get_optional_user

router = APIRouter()



# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _build_workflow():
    """LangGraph workflow'unu her istek için oluşturur."""
    workflow = StateGraph(AgentState)
    workflow.add_node("classifier", intent_node)
    workflow.add_node("agent", agent_node)
    workflow.add_node("tools", custom_tool_node)

    workflow.set_entry_point("classifier")
    workflow.add_edge("classifier", "agent")
    workflow.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    workflow.add_edge("tools", "agent")
    return workflow.compile()


def _build_action_cards(response_text: str, visual_data: dict, routing_phase: int = 1) -> list:
    """
    LLM yanıtındaki anahtar kelimelere, görsel veriye ve routing fazına göre
    dinamik action butonları üretir.

    action alanı iki türde olabilir:
      - "ui:<action>"   → Mobil uygulama direkt handle eder, AI'a GÖNDERİLMEZ
      - "<metin>"       → Kullanıcı adına AI'a gönderilecek mesaj
    """
    cards = []
    text_lower = response_text.lower()
    has_polyline = bool(visual_data.get("polyline"))
    has_markers = bool(visual_data.get("markers"))

    food_keywords = ["yemek", "restoran", "kafe", "mola", "aç mısın", "acıktım", "yiyelim", "kahvaltı", "mekan"]
    fuel_keywords = ["yakıt", "benzin", "mazot", "istasyon", "dolduralım", "şarj"]
    weather_keywords = ["hava", "yağmur", "kar", "fırtına"]
    radar_keywords = ["radar", "kontrol noktası", "jandarma"]
    nav_keywords = ["navigasyon başlat", "yolun açık", "hazırım", "gidelim"]

    # ── FAZ 4: Final özet — sadece navigasyon başlatma kartı ─────────────────
    if routing_phase == 4 or any(k in text_lower for k in nav_keywords):
        cards.append({
            "label": "Navigasyonu Başlat",
            "action": "ui:start_navigation",
            "icon": "🗺️",
            "style": "primary"
        })
        if has_polyline:
            cards.append({
                "label": "Rotayı Paylaş",
                "action": "ui:share_route",
                "icon": "📤",
                "style": "secondary"
            })
        return cards[:3]

    # ── FAZ 3: Seçim yapıldı, rota güncellendi ──────────────────────────────
    if routing_phase == 3 and has_polyline:
        cards.append({
            "label": "Radar ve Hava Durumu Ekle",
            "action": "Evet, radar ve hava durumunu da ekle, yolculuk özetini hazırla",
            "icon": "📸",
            "style": "primary"
        })
        cards.append({
            "label": "Navigasyonu Başlat",
            "action": "ui:start_navigation",
            "icon": "🗺️",
            "style": "primary"
        })
        cards.append({
            "label": "Yolda Yemek mi Yiyeceksin?",
            "action": "Evet yolda yemek yemek istiyorum, güzergahımın ortasında uygun restoranları öner",
            "icon": "🍽️",
            "style": "secondary"
        })
        cards.append({
            "label": "Yakıt Durumumu Analiz Et",
            "action": "ui:fuel_range_prompt",
            "action_template": "Yakıt analizimi yap, {range_km} km menzilim var, güzergahtaki en uygun durak noktalarini ve her birinde alternatif istasyonlari goster",
            "icon": "⛽",
            "style": "secondary"
        })
        return cards[:4]

    # ── FAZ 2: POI önerisi — overlay zaten açık, minimum kart ───────────────
    if routing_phase == 2 and has_markers:
        # Overlay varken "Bu Mekanı Rotama Ekle" gereksiz (kart üzerinde zaten var)
        # Sadece farklı öneri ve atla kartları yeterli
        cards.append({
            "label": "Yolda Yemek mi Yiyeceksin?",
            "action": "Evet yolda yemek yemek istiyorum, güzergahımın ortasında uygun restoranları öner",
            "icon": "🍽️",
            "style": "secondary"
        })
        cards.append({
            "label": "Yakıt Durumumu Analiz Et",
            "action": "ui:fuel_range_prompt",
            "action_template": "Yakıt analizimi yap, {range_km} km menzilim var, güzergahtaki en uygun durak noktalarini ve her birinde alternatif istasyonlari goster",
            "icon": "⛽",
            "style": "secondary"
        })
        return cards[:4]

    # ── FAZ 1: Yeni rota çizildi → proaktif soru kartları ───────────────────
    if has_polyline:
        cards.append({
            "label": "Navigasyonu Başlat",
            "action": "ui:start_navigation",
            "icon": "🗺️",
            "style": "primary"
        })
        # Alternatif rotalar — UI-only (AI'a gönderilmez, harita katmanını toggle eder)
        cards.append({
            "label": "Alternatifleri Gör",
            "action": "ui:show_alternatives",
            "icon": "🔄",
            "style": "secondary"
        })
        # Yemek sorusu — güzergah bilgisiyle zenginleştirilmiş mesaj
        cards.append({
            "label": "Yolda Yemek mi Yiyeceksin?",
            "action": "Evet yolda yemek yemek istiyorum, güzergahımın ortasında uygun restoranları öner",
            "icon": "🍽️",
            "style": "secondary"
        })
        # Yakıt kartı — UI önce menzil sorar, sonra AI'a detaylı mesaj gönderir
        cards.append({
            "label": "Yakıt Durumumu Analiz Et",
            "action": "ui:fuel_range_prompt",
            # Mobil bu template'i kullanır: "Yakıt analizimi yap, {range_km} km menzilim var, güzergahtaki en uygun durak ilçelerini ve istasyonları bul"
            "action_template": "Yakıt analizimi yap, {range_km} km menzilim var, güzergahtaki en uygun durak noktalarini ve her birinde alternatif istasyonlari goster",
            "icon": "⛽",
            "style": "secondary"
        })
        # Radar — çalışan action
        cards.append({
            "label": "Radar Kontrolü",
            "action": "Yol üzerindeki aktif radar ve kontrol noktalarını göster",
            "icon": "📸",
            "style": "secondary"
        })
        # Yeni nesil Premium Action
        cards.append({
            "label": "Manzaralı / Kaliteli Molalar",
            "action": "Yol üzerindeki en iyi manzaralı, kaliteli ve premium mola mekanlarını (kahve/dinlenme) göster",
            "icon": "☕",
            "style": "secondary"
        })
        
        return cards[:5]

    # ── Mekan önerildi ama polyline yok (standalone POI sorgusu) ─────────────
    if has_markers and not has_polyline:
        cards.append({
            "label": "Haritada Göster",
            "action": "ui:show_on_map",
            "icon": "📍",
            "style": "primary"
        })
        cards.append({
            "label": "Rotama Ekle",
            "action": "Bu mekanı rotama ekle ve güzergahı güncelle",
            "icon": "➕",
            "style": "secondary"
        })
        cards.append({
            "label": "Farklı Mekan Öner",
            "action": "Başka alternatifleri öner",
            "icon": "🔍",
            "style": "secondary"
        })
        return cards[:3]

    # ── Bağımsız yakıt/hava/radar kartları ──────────────────────────────────
    if any(k in text_lower for k in fuel_keywords) and not has_polyline:
        cards.append({
            "label": "Yakıt Analizi",
            "action": "Yakıt fiyatlarını karşılaştır ve en uygununu bul",
            "icon": "⛽",
            "style": "primary"
        })
    if any(k in text_lower for k in weather_keywords):
        cards.append({
            "label": "Hava Durumu Detayı",
            "action": "Rota boyunca hava durumunu detaylı analiz et",
            "icon": "☁️",
            "style": "secondary"
        })
    if any(k in text_lower for k in radar_keywords) and not has_polyline:
        cards.append({
            "label": "Radarları Göster",
            "action": "Yol üzerindeki aktif radar ve kontrol noktalarını bul",
            "icon": "📸",
            "style": "secondary"
        })

    # Maksimum 5 kart — UI taşmasın
    return cards[:5]


def _build_poi_overlay(markers: list, response_text: str) -> PoiOverlay | None:
    """
    Marker listesinden mobil tam ekran swipe overlay nesnesi oluşturur.
    Sadece mekan önerisi varken (markers > 0, polyline yok veya POI fazı) çağrılır.
    """
    if not markers:
        return None

    text_lower = response_text.lower()
    is_fuel = any(k in text_lower for k in ["benzin", "yakıt", "istasyon", "petrol", "opet", "shell", "bp"])
    is_food = any(k in text_lower for k in ["restoran", "yemek", "kafe", "lokanta", "kahvaltı", "döner"])
    is_market = any(k in text_lower for k in ["market", "avm", "alışveriş"])

    if is_fuel:
        title = "⛽ Yol Üzerindeki Yakıt İstasyonları"
        category = "benzin_istasyonu"
    elif is_food:
        title = "🍽️ Yol Üzerindeki Yemek Mekanları"
        category = "restoran"
    elif is_market:
        title = "🛒 Yakındaki Marketler"
        category = "market"
    else:
        title = "📍 Yakındaki Mekanlar"
        category = "poi"

    cards = []
    best_idx = 0  # En iyi kartı belirle (en düşük sapma + en yüksek puan)
    best_score = -1

    for i, m in enumerate(markers):
        poi_card = m.poi_card or {}
        deviation = poi_card.get("deviation_meters", 9999) or 9999
        rating = poi_card.get("rating") or 0
        # Sapma azlığı + puan = skorlama
        score = (1 / (deviation + 1)) * 1000 + rating
        if score > best_score:
            best_score = score
            best_idx = i

        # Sapma etiketi
        if deviation <= 400:
            deviation_label = "Yol üstü ✅"
        elif deviation <= 2000:
            deviation_label = f"{deviation}m sapma ⚠️"
        else:
            deviation_label = f"{deviation/1000:.1f}km uzatır ❌"

        # Ek süre etiketi (yaklaşık: deviation_meters / 500 m/dk ≈ dk)
        extra_min = round(deviation / 500) if deviation > 400 else 0
        if extra_min == 0:
            route_impact_label = "Sıfır ek süre"
        else:
            route_impact_label = f"+{extra_min} dk"

        place_id = f"poi_{i}_{m.lat}_{m.lon}".replace(".", "_")

        cards.append(PoiOverlayCard(
            id=place_id,
            name=m.title or m.snippet or "Mekan",
            address=poi_card.get("address") or m.snippet,
            category=category,
            lat=m.lat,
            lon=m.lon,
            deviation_meters=deviation if deviation < 9999 else None,
            distance_along_route_km=poi_card.get("distance_along_route_km"),
            extra_time_min=extra_min if deviation > 400 else 0,
            eta=poi_card.get("eta"),
            on_route_side=poi_card.get("on_route_side"),
            rating=poi_card.get("rating"),
            review_count=poi_card.get("review_count"),
            price_level=poi_card.get("price_level"),
            is_open=poi_card.get("is_open") or poi_card.get("open_now"),
            open_now=poi_card.get("open_now"),
            opening_hours=poi_card.get("opening_hours"),
            phone=poi_card.get("phone"),
            deviation_label=deviation_label,
            route_impact_label=route_impact_label,
            is_recommended=False,  # Sonradan best_idx ile set edilecek
            recommendation_reason=None,
        ))

    # En iyi kartı işaretle
    if cards:
        best_card = cards[best_idx]
        cards[best_idx] = best_card.model_copy(update={
            "is_recommended": True,
            "recommendation_reason": "En iyi konum ve puan kombinasyonu",
        })

    # Önce önerilen kart, sonra yol üstündekiler, sonra sapmaya göre sırala
    def _sort_key(c: PoiOverlayCard):
        return (
            0 if c.is_recommended else 1,
            0 if c.on_route_side == "right" else 1,
            c.deviation_meters or 9999,
        )
    cards.sort(key=_sort_key)

    subtitle = f"{len(cards)} mekan bulundu · Kaydırarak incele"

    return PoiOverlay(
        mode="poi_selection",
        title=title,
        subtitle=subtitle,
        cards=cards,
        primary_action="Rotama Ekle",
        secondary_action="Farklı Mekan Öner",
    )


def _build_routing_phase_info(
    phase: int,
    visual_data: dict,
    session_id: str,
    destination: str | None = None,
    waypoints_count: int = 0,
) -> RoutingPhaseInfo:
    """Mevcut routing fazını ve harita durumunu mobil için paketler."""
    phase_labels = {
        1: "Rota Hazır",
        2: "Mekan Seçimi",
        3: "Rota Güncellendi",
        4: "Yolculuk Özeti",
    }
    has_polyline = bool(visual_data.get("polyline"))
    return RoutingPhaseInfo(
        phase=phase,
        phase_label=phase_labels.get(phase, "Rota Hazır"),
        has_active_route=has_polyline,
        active_destination=destination,
        waypoints_count=waypoints_count,
    )



def _load_history(session_id: str) -> list:
    """Redis'ten sohbet geçmişini okur."""
    messages = []
    if not orchestrator.redis_client:
        return messages
    try:
        raw = orchestrator.redis_client.lrange(f"chat:{session_id}", 0, -1)
        for item in raw:
            if isinstance(item, bytes):
                item = item.decode("utf-8")
            msg = json.loads(item)
            if msg["role"] == "user":
                messages.append(HumanMessage(content=msg["content"]))
            elif msg["role"] == "assistant":
                messages.append(AIMessage(content=msg["content"]))
    except Exception as e:
        log.error(f"Geçmiş okunurken hata: {e}")
    return messages


def _save_history(session_id: str, user_msg: str, assistant_msg: str):
    """Redis'e sohbet yanıtını kaydeder."""
    if not orchestrator.redis_client:
        return
    key = f"chat:{session_id}"
    orchestrator.redis_client.rpush(key, json.dumps({"role": "user", "content": user_msg}))
    orchestrator.redis_client.rpush(key, json.dumps({"role": "assistant", "content": assistant_msg}))
    orchestrator.redis_client.ltrim(key, -20, -1)   # Son 20 mesajı tut
    orchestrator.redis_client.expire(key, 86400)     # 24 saat


# ---------------------------------------------------------------------------
# ENDPOINTS
# ---------------------------------------------------------------------------

@router.post("/chat")
async def chat_endpoint(request: ChatRequestV1):
    """
    Eski chat endpoint'i (geriye uyumluluk).
    Yeni mobil app /api/v1/chat kullanmalı.
    """
    t_start = time.monotonic()
    log.info(f"📩 [Request] Session: {request.session_id} | Msg: {request.message[:40]}...")

    result = await _run_chat(
        message=request.message,
        session_id=request.session_id,
        current_lat=request.current_lat,
        current_lon=request.current_lon,
        fcm_token=request.fcm_token,
    )

    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    return {
        "response": result["response_text"],
        "session_id": request.session_id,
        "visual_data": result["visual_data"],
        "route_polyline": result["route_polyline"],
        "action_cards": result["action_cards"],
        "metadata": {
            "model_used": result["model_used"],
            "response_time_ms": elapsed_ms,
            "tool_calls_count": result.get("retry_count", 0),
        },
    }


@router.post("/location/update")
async def update_location(req: LocUpdateV1):
    """
    Mobil uygulama arka planda çalışırken konumu günceller.
    Her /chat isteğinde göndermek yerine bu endpoint kullanılabilir.
    """
    session_id = req.session_id or "default_session"
    if orchestrator.redis_client:
        loc_str = f"{req.lat},{req.lon}"
        orchestrator.redis_client.setex(f"loc:{session_id}", 3600, loc_str)
        log.info(f"📍 [LocationUpdate] Session: {session_id} → {loc_str}")
        return {"status": "ok", "location": loc_str}
    return {"status": "error", "message": "Redis unavailable"}


@router.get("/health")
async def health_check():
    """Basit sağlık kontrolü (Docker healthcheck ve mobil için)."""
    redis_ok = False
    if orchestrator.redis_client:
        try:
            orchestrator.redis_client.ping()
            redis_ok = True
        except Exception:
            pass

    agents_online = list(orchestrator.sessions.keys())

    return {
        "status": "ok",
        "redis": redis_ok,
        "agents_connected": agents_online,
        "tool_count": len(orchestrator.runtime_tools),
    }


@router.websocket("/ws/chat/{session_id}")
async def websocket_chat(websocket: WebSocket, session_id: str):
    """
    Streaming WebSocket chat endpoint.
    Mobil uygulama bağlanır, mesaj gönderir, token token yanıt alır.

    Protocol:
        Client → {"message": "...", "current_lat": float|null, "current_lon": float|null}
        Server → {"type": "chunk", "content": "..."} (token token)
        Server → {"type": "done", "visual_data": {...}, "action_cards": [...]}
        Server → {"type": "error", "message": "..."}
    """
    await websocket.accept()
    log.info(f"🔌 [WS] Bağlantı kuruldu: {session_id}")

    executor = _build_workflow()

    try:
        while True:
            # Mesaj al
            raw_data = await websocket.receive_json()
            message = raw_data.get("message", "")
            if not message.strip():
                continue

            # Anlık konum güncelle
            c_lat = raw_data.get("current_lat")
            c_lon = raw_data.get("current_lon")
            if orchestrator.redis_client and c_lat and c_lon:
                orchestrator.redis_client.setex(f"loc:{session_id}", 3600, f"{c_lat},{c_lon}")

            # Geçmiş + mesaj
            history = _load_history(session_id)
            history.append(HumanMessage(content=message))

            try:
                # LangGraph streaming
                collected_text = []
                final_visual = {"markers": [], "polyline": None, "geojson_layers": []}

                async for event in executor.astream_events(
                    {
                        "messages": history,
                        "intent": {},
                        "retry_count": 0,
                        "session_id": session_id,
                        "visual_data": {"markers": [], "polyline": None, "geojson_layers": []},
                    },
                    version="v1",
                ):
                    kind = event.get("event", "")
                    # Sadece LLM çıktısı chunk'larını stream et
                    if kind == "on_chat_model_stream":
                        chunk_content = event.get("data", {}).get("chunk", {})
                        if hasattr(chunk_content, "content"):
                            text = chunk_content.content
                            if isinstance(text, str) and text:
                                collected_text.append(text)
                                await websocket.send_json({"type": "chunk", "content": text})
                    # Visual data güncellemelerini yakala
                    elif kind == "on_chain_end":
                        output = event.get("data", {}).get("output", {})
                        if isinstance(output, dict) and "visual_data" in output:
                            final_visual = output["visual_data"]

                response_text = "".join(collected_text)
                if response_text:
                    _save_history(session_id, message, response_text)

                # 🔥 BULLETPROOF POLYLINE SİSTEMİ (WebSocket)
                raw_poly = final_visual.get("polyline")
                if not raw_poly or len(str(raw_poly)) < 50:
                    try:
                        cached = orchestrator.redis_client.get(f"route:{session_id}")
                        if cached: raw_poly = cached if isinstance(cached, str) else cached.decode('utf-8')
                    except: pass
                
                new_polyline = ""
                if raw_poly:
                    try:
                        import flexpolyline
                        import polyline as google_polyline
                        if isinstance(raw_poly, list):
                            new_polyline = google_polyline.encode([p[:2] for p in raw_poly])
                        elif isinstance(raw_poly, str):
                            if raw_poly.startswith('[') or raw_poly.startswith('{'):
                                import json
                                pts = json.loads(raw_poly)
                                if isinstance(pts, list): new_polyline = google_polyline.encode([p[:2] for p in pts])
                            elif raw_poly.startswith('v'):
                                decoded = flexpolyline.decode(raw_poly)
                                new_polyline = google_polyline.encode([p[:2] for p in decoded])
                            else:
                                new_polyline = raw_poly
                        final_visual["polyline"] = new_polyline
                    except: pass

                # Tamamlama mesajı
                await websocket.send_json({
                    "type": "done",
                    "visual_data": final_visual,
                    "action_cards": _build_action_cards(response_text, final_visual),
                })

            except Exception as e:
                log.error(f"🔥 [WS] İşlem hatası: {e}")
                await websocket.send_json({"type": "error", "message": str(e)})

    except WebSocketDisconnect:
        log.info(f"🔌 [WS] Bağlantı kesildi: {session_id}")
    except Exception as e:
        log.error(f"🔥 [WS] Kritik hata: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass


# ===========================================================================
# V1 API — STANDART ENVELOPE (MOBİL APP)
# ===========================================================================


async def _run_chat(message: str, session_id: str, current_lat=None, current_lon=None, fcm_token=None):
    """Ortak chat işleme mantığı. Hem eski hem yeni endpoint kullanır."""
    # Anlık konumu Redis'e kaydet
    if orchestrator.redis_client and current_lat and current_lon:
        loc_str = f"{current_lat},{current_lon}"
        orchestrator.redis_client.setex(f"loc:{session_id}", 3600, loc_str)

    # FCM token kaydet
    if orchestrator.redis_client and fcm_token:
        orchestrator.redis_client.setex(f"fcm:{session_id}", 86400 * 30, fcm_token)

    # Geçmişi oku + yeni mesajı ekle
    history = _load_history(session_id)
    history.append(HumanMessage(content=message))

    # LangGraph çalıştır
    executor = _build_workflow()
    final_state = await executor.ainvoke({
        "messages": history,
        "intent": {},
        "retry_count": 0,
        "session_id": session_id,
        "visual_data": {"markers": [], "polyline": None, "geojson_layers": []},
        "routing_phase": 1,
        "route_polyline": None,
        "poi_suggestions": None,
    })

    # Yanıt metni çıkar
    raw = final_state["messages"][-1].content
    if isinstance(raw, list):
        response_text = "".join(b.get("text", "") for b in raw if isinstance(b, dict) and "text" in b)
    else:
        response_text = str(raw)

    # Geçmişi kaydet
    _save_history(session_id, message, response_text)

    # Son polyline'ı Redis'ten al
    route_polyline = None
    if orchestrator.redis_client:
        val = orchestrator.redis_client.get(f"route:{session_id}")
        route_polyline = val.decode("utf-8") if isinstance(val, bytes) else val

    visual_data = final_state.get("visual_data", {})
    intent = final_state.get("intent", {})
    routing_phase = intent.get("routing_phase", 1)
    model_used = "claude" if intent.get("complexity") == "high" else "gemini"
    action_cards = _build_action_cards(response_text, visual_data, routing_phase=routing_phase)

    # Kullanılan tool adlarını çıkar (Context Indicator için)
    tools_used = []
    for msg in final_state.get("messages", []):
        if hasattr(msg, "tool_calls"):
            for tc in (msg.tool_calls or []):
                name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                if name and name not in tools_used:
                    tools_used.append(name)

    return {
        "response_text": response_text,
        "visual_data": visual_data,
        "route_polyline": route_polyline,
        "intent": intent,
        "model_used": model_used,
        "action_cards": action_cards,
        "tools_used": tools_used,
        "retry_count": final_state.get("retry_count", 0),
    }


@router.post("/api/v1/chat", response_model=ApiEnvelope, tags=["Chat v1"])
async def chat_v1(request: ChatRequestV1, user: dict = Depends(get_optional_user)):
    """
    📱 Mobil API chat endpoint'i.
    Standart ApiResponse envelope ile döner.
    Auth opsiyonel — token varsa user context kullanılır.
    """
    t_start = time.monotonic()
    session_id = request.session_id

    # Auth'lu kullanıcı varsa session_id'yi user-scoped yap
    if user:
        session_id = f"{user['user_id']}:{request.session_id}"

    log.info(f"📩 [v1/Chat] Session: {session_id} | Msg: {request.message[:40]}...")

    try:
        result = await _run_chat(
            message=request.message,
            session_id=session_id,
            current_lat=request.current_lat,
            current_lon=request.current_lon,
            fcm_token=request.fcm_token,
        )

        elapsed_ms = int((time.monotonic() - t_start) * 1000)

        # Marker'ları MapMarker formatına çevir
        raw_markers = result["visual_data"].get("markers", [])
        markers = []
        for m in raw_markers:
            if isinstance(m, dict) and "lat" in m:
                # Zengin POI kart metadata'sı
                on_route_side = m.get("on_route_side", "unknown")
                poi_card = {
                    "on_route_side": on_route_side,
                    # Sağ/sol taraf görseli için etiket
                    "side_label": (
                        "✅ Sağ taraf (U-dönüşü gerekmez)" if on_route_side == "right"
                        else "⚠️ Sol taraf (Karşı şerit)" if on_route_side == "left"
                        else None
                    ),
                    "opening_hours": m.get("opening_hours", []),
                    "open_now": m.get("open_now"),
                    "eta": m.get("eta"),
                    "deviation_meters": m.get("deviation_meters", 0),
                    "distance_along_route_km": m.get("distance_along_route_km"),
                    "rating": m.get("rating"),
                    "review_count": m.get("review_count"),
                    "price_level": m.get("price_level"),  # 0-4 arası
                    "phone": m.get("phone"),
                    "address": m.get("snippet") or m.get("address") or m.get("description"),
                    "type": m.get("type", "poi"),
                }
                markers.append(MapMarker(
                    lat=m["lat"], lon=m.get("lon", m.get("lng", 0)),
                    title=m.get("title", m.get("name")),
                    type=m.get("type", "poi"),
                    icon=m.get("icon"),
                    snippet=m.get("snippet") or m.get("address") or m.get("description"),
                    poi_card=poi_card,
                ))
        # ★ POI kartlarını mantıklı sırala: Önce sapması düşük olanlar (deviation_meters), sonra rating.
        # Note: Eğer deviation bilgisi yoksa arkaya at.
        def _sort_marker(m):
            card = getattr(m, "poi_card", {}) or getattr(m, "poi_card") or {}
            if isinstance(card, dict):
                dev = card.get("deviation_meters")
                if dev is not None:
                    return dev
            return 99999
        
        markers.sort(key=_sort_marker)

        # Action cardsı ActionCard formatına
        cards = []
        for i, c in enumerate(result["action_cards"]):
            action_str = c["action"]
            is_ui_only = action_str.startswith("ui:")
            cards.append(ActionCard(
                id=c.get("action", f"action_{i}"),
                label=c["label"],
                action=action_str,
                icon=c.get("icon", ""),
                style=c.get("style", "primary" if i == 0 else "secondary"),
                action_template=c.get("action_template"),
                is_ui_only=is_ui_only,
            ))

        # 🔥 BULLETPROOF POLYLINE SİSTEMİ (v2.1)
        # 1. Ham veriyi bul (State -> Redis -> VisualData)
        raw_polyline = result.get("route_polyline")
        
        if not raw_polyline or len(str(raw_polyline)) < 50:
            try:
                cached = orchestrator.redis_client.get(f"route:{session_id}")
                if cached: 
                    raw_polyline = cached if isinstance(cached, str) else cached.decode('utf-8')
            except: pass

        if not raw_polyline:
            raw_polyline = result["visual_data"].get("polyline")

        # 2. Formatı Algıla ve Dönüştür
        polyline = ""
        if raw_polyline:
            import flexpolyline
            import polyline as google_polyline
            
            try:
                # Durum A: Liste formatında ham koordinatlar [(lat,lon), ...]
                if isinstance(raw_polyline, list):
                    polyline = google_polyline.encode([p[:2] for p in raw_polyline])
                    log.success(f"✅ [Polyline] List -> Google v5 (Points: {len(raw_polyline)})")
                
                # Durum B: String formatı
                # 🚨 RADİKAL ÇÖZÜM: Şifrelemeyi (Encoding) çöpe atıyoruz. 
                # Mobil tarafla koordinat listesi (JSON) üzerinden haberleşeceğiz.
                
                # Önce ne gelirse gelsin çözmeye çalış (List, v6 veya v5)
                decoded_points = []
                if isinstance(raw_polyline, list):
                    decoded_points = [p[:2] for p in raw_polyline]
                elif isinstance(raw_polyline, str):
                    raw_polyline = raw_polyline.strip()
                    if raw_polyline.startswith('['):
                        try: decoded_points = json.loads(raw_polyline)
                        except: pass
                    else:
                        # v6 veya v5 deniyoruz
                        try:
                            import flexpolyline
                            decoded_points = flexpolyline.decode(raw_polyline)
                            log.success(f"✅ [Polyline] v6 Decoded (Points: {len(decoded_points)})")
                        except:
                            try:
                                import polyline as google_polyline
                                decoded_points = google_polyline.decode(raw_polyline)
                                log.success(f"✅ [Polyline] v5 Decoded (Points: {len(decoded_points)})")
                            except:
                                log.error("❌ Polyline çözülemedi!")
                
                # Sonuç: Her zaman açık JSON listesi gönder
                if decoded_points:
                    polyline = json.dumps([[float(p[0]), float(p[1])] for p in decoded_points])
                    log.success(f"🚀 [Polyline] Raw JSON List sent to Mobile (Points: {len(decoded_points)})")
                else:
                    polyline = str(raw_polyline)
            except Exception as e:
                log.error(f"❌ Polyline dönüştürme hatası: {e}")
                polyline = str(raw_polyline)
        

        # 🗺️ Alternatif Rotaları Yakala
        alternatives_list = []
        raw_alternatives = result.get("alternatif_rotalar", [])
        if isinstance(raw_alternatives, list):
            for alt in raw_alternatives:
                try:
                    alt_raw = alt.get("polyline_encoded", "")
                    if not alt_raw or alt_raw == raw_polyline: continue
                    # Alternatifleri de ham JSON listesi olarak gönderiyoruz (Bulletproof!)
                    import flexpolyline
                    alt_decoded = flexpolyline.decode(alt_raw)
                    if alt_decoded:
                        alternatives_list.append(json.dumps([[float(p[0]), float(p[1])] for p in alt_decoded]))
                except: continue

        if polyline:
            log.info(f"📤 [Final Polyline] Sent to Mobile (Alternatives: {len(alternatives_list)})")

        # ★ POI OVERLAY: Mekan önerisi fazındaysa (markers var, polyline YOK veya phase==2)
        # AI yemek/yakıt/mola önerisi yapıyorsa mobil tam ekran kart arayüzünü açsın
        current_phase = result.get("intent", {}).get("routing_phase", 1)
        has_poi_markers = len(markers) > 0
        has_route_polyline = bool(polyline)
        # Phase 2: sadece POI önerisi yapılan tur → overlay aç
        # Phase 1 + markers: rota çizildi VE aynı anda POI önerildi → overlay aç
        should_show_overlay = has_poi_markers and (
            current_phase == 2
            or (current_phase == 1 and not has_route_polyline)
        )
        poi_overlay = _build_poi_overlay(markers, result["response_text"]) if should_show_overlay else None

        # ★ ROUTING PHASE INFO
        # Aktif destinasyonu rota geçmişinden tahmin et
        active_dest = None
        route_history = result.get("intent", {}).get("route_history", [])
        if route_history:
            active_dest = route_history[-1].get("destination")
        waypoints_count = sum(
            1 for m in markers if (m.poi_card or {}).get("type") == "waypoint"
        )
        routing_phase_info = _build_routing_phase_info(
            phase=current_phase,
            visual_data=result["visual_data"],
            session_id=session_id,
            destination=active_dest,
            waypoints_count=waypoints_count,
        ) if result.get("intent", {}).get("category") == "routing" else None

        return ApiEnvelope(
            success=True,
            data=ChatResponse(
                message=result["response_text"],
                intent=result["intent"],
                map=MapData(
                    markers=markers,
                    polyline=polyline,
                    alternatives=alternatives_list,
                    geojson_layers=result["visual_data"].get("geojson_layers", []),
                ),
                action_cards=cards,
                tools_used=result.get("tools_used", []),
                poi_overlay=poi_overlay,
                routing_phase=routing_phase_info,
            ).model_dump(),
            metadata=ApiMetadata(
                response_time_ms=elapsed_ms,
                session_id=session_id,
            ),
        )

    except Exception as e:
        log.error(f"🔥 [v1/Chat] Hata: {e}")
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        return ApiEnvelope(
            success=False,
            error=ApiError(code="CHAT_ERROR", message=str(e)),
            metadata=ApiMetadata(response_time_ms=elapsed_ms, session_id=session_id),
        )


@router.post("/api/v1/location/update", response_model=ApiEnvelope, tags=["Location v1"])
async def update_location_v1(req: LocUpdateV1, user: dict = Depends(get_optional_user)):
    """📱 Opsiyonel Auth'lu konum güncelleme."""
    t_start = time.monotonic()
    
    # Kullanıcı giriş yapmamışsa bile anonim session id ile kaydet
    user_prefix = user['user_id'] if user else "anonymous"
    session_id = req.session_id or f"{user_prefix}:default"

    if orchestrator.redis_client:
        loc_str = f"{req.lat},{req.lon}"
        orchestrator.redis_client.setex(f"loc:{session_id}", 3600, loc_str)
        log.info(f"📍 [v1/Location] {user_prefix} → {loc_str}")
        return ApiEnvelope(
            success=True,
            data={"location": loc_str, "session_id": session_id},
            metadata=ApiMetadata(response_time_ms=int((time.monotonic() - t_start) * 1000)),
        )

    return ApiEnvelope(
        success=False,
        error=ApiError(code="REDIS_UNAVAILABLE", message="Konum kaydedilemedi."),
        metadata=ApiMetadata(response_time_ms=int((time.monotonic() - t_start) * 1000)),
    )

@router.get("/api/v1/history/chat", response_model=ApiEnvelope, tags=["History"])
async def get_chat_history_v1(session_id: str = "default_session", user: dict = Depends(get_optional_user)):
    """📱 Kullanıcının sohbet geçmişini getirir."""
    t_start = time.monotonic()
    history = _load_history(session_id)
    messages = []
    for m in history:
        role = "assistant" if isinstance(m, AIMessage) else "user"
        messages.append({"role": role, "content": m.content})
    
    return ApiEnvelope(
        success=True,
        data={"messages": messages},
        metadata=ApiMetadata(response_time_ms=int((time.monotonic() - t_start) * 1000))
    )

@router.get("/api/v1/history/routes", response_model=ApiEnvelope, tags=["History"])
async def get_route_history_v1(user: dict = Depends(get_optional_user)):
    """📱 Kullanıcının geçmiş rotalarını getirir."""
    t_start = time.monotonic()
    # Mock data for demonstration, in production fetch from DB
    routes = [
        {"origin": "Beşiktaş", "destination": "Sarıyer", "distance_km": 15.2, "duration_min": 25, "date": "2024-04-24"},
        {"origin": "Kadıköy", "destination": "Pendik", "distance_km": 28.5, "duration_min": 45, "date": "2024-04-23"}
    ]
    return ApiEnvelope(
        success=True,
        data={"routes": routes},
        metadata=ApiMetadata(response_time_ms=int((time.monotonic() - t_start) * 1000))
    )


# ===========================================================================
# HEALTH — Detaylı Servis Durumu (MCP Status Panel)
# ===========================================================================

@router.get("/api/v1/health", tags=["System"])
async def health_v1():
    """
    📱 Detaylı sistem sağlık kontrolü.
    Her MCP servisinin ayrı ayrı durumunu raporlar.
    """
    import time as _time

    redis_ok = False
    if orchestrator.redis_client:
        try:
            orchestrator.redis_client.ping()
            redis_ok = True
        except Exception:
            pass

    # Her bağlı MCP agent'ın durumunu kontrol et
    services = []
    known_services = ["mcp_city", "mcp_intel", "mcp_satellite"]

    connected_agents = list(orchestrator.sessions.keys())

    for svc in known_services:
        if svc in connected_agents:
            # Bağlı — tool sayısını bul
            tool_count = sum(
                1 for t in orchestrator.runtime_tools
                if hasattr(t, 'name') and svc.replace('mcp_', '') in getattr(t, 'description', '').lower()
            )
            services.append({
                "name": svc,
                "status": "online",
                "tools": tool_count,
            })
        else:
            services.append({
                "name": svc,
                "status": "offline",
                "tools": 0,
            })

    return {
        "status": "ok",
        "redis": redis_ok,
        "services": services,
        "agents_connected": connected_agents,
        "tool_count": len(orchestrator.runtime_tools),
    }