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


def _build_action_cards(response_text: str, visual_data: dict) -> list:
    """
    LLM yanıtına ve görsel veriye göre mobil hızlı aksiyon butonları üretir.
    """
    cards = []
    if visual_data.get("polyline"):
        cards.append({"label": "Navigasyonu Başlat", "action": "start_navigation", "icon": "🗺️"})
        cards.append({"label": "Alternatif Rotalar", "action": "show_alternatives", "icon": "🔄"})
    if visual_data.get("markers") and len(visual_data["markers"]) > 0:
        cards.append({"label": "Haritada Göster", "action": "show_on_map", "icon": "📍"})
    if "benzin" in response_text.lower() or "yakıt" in response_text.lower():
        cards.append({"label": "Yakıt Karşılaştır", "action": "compare_fuel", "icon": "⛽"})
    return cards


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
    model_used = "claude" if intent.get("complexity") == "high" else "gemini"
    action_cards = _build_action_cards(response_text, visual_data)

    return {
        "response_text": response_text,
        "visual_data": visual_data,
        "route_polyline": route_polyline,
        "intent": intent,
        "model_used": model_used,
        "action_cards": action_cards,
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
                markers.append(MapMarker(
                    lat=m["lat"], lon=m.get("lon", m.get("lng", 0)),
                    title=m.get("title", m.get("name")),
                    type=m.get("type"),
                    icon=m.get("icon"),
                ))

        # Action cardsı ActionCard formatına
        cards = []
        for i, c in enumerate(result["action_cards"]):
            cards.append(ActionCard(
                id=c.get("action", f"action_{i}"),
                label=c["label"],
                action=c["action"],
                icon=c.get("icon", ""),
                style="primary" if i == 0 else "secondary",
            ))

        polyline = result["route_polyline"] or result["visual_data"].get("polyline")

        return ApiEnvelope(
            success=True,
            data=ChatResponse(
                message=result["response_text"],
                intent=result["intent"],
                map=MapData(
                    markers=markers,
                    polyline=polyline,
                    geojson_layers=result["visual_data"].get("geojson_layers", []),
                ),
                action_cards=cards,
                model_used=result["model_used"],
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
async def update_location_v1(req: LocUpdateV1, user: dict = Depends(get_current_user)):
    """📱 Auth'lu konum güncelleme."""
    t_start = time.monotonic()
    session_id = req.session_id or f"{user['user_id']}:default"

    if orchestrator.redis_client:
        loc_str = f"{req.lat},{req.lon}"
        orchestrator.redis_client.setex(f"loc:{session_id}", 3600, loc_str)
        log.info(f"📍 [v1/Location] {user['username']} → {loc_str}")
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