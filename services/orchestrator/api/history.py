"""
api/history.py — Rota ve Sohbet Geçmişi

Kullanıcının rota geçmişi ve sohbet geçmişi endpoint'leri.
"""
import json
import time
from fastapi import APIRouter, Depends, Query

from core.db import async_session_maker, User, RouteHistory, DayPlanHistory
from core.mcp_client import orchestrator
from api.schemas import (
    ApiResponse, ApiError, ApiMetadata,
    RouteHistoryItem, ChatHistoryItem, ChatSessionItem,
    RouteHistoryUpdateRequest,
)
from api.deps import get_current_user
from logger import log
from sqlmodel import select
from uuid import UUID

router = APIRouter(prefix="/history", tags=["History"])


def _elapsed(t_start: float) -> int:
    return int((time.monotonic() - t_start) * 1000)


# ═══════════════════════════════════════════════════════════════════════════
# ROUTE HISTORY
# ═══════════════════════════════════════════════════════════════════════════

def _serialize_route(r: RouteHistory, *, include_polyline: bool = True) -> dict:
    """RouteHistory row → RouteHistoryItem dict. Liste için polyline'ı dışla
    (response boyutu küçük kalsın), detayda dahil et."""
    stops_data = getattr(r, "stops", None)
    # Eski kayıtlar için fallback: waypoints → kind='waypoint' stops
    if not stops_data and (r.waypoints or r.origin or r.destination):
        stops_data = []
        stops_data.append({
            "kind": "origin", "name": r.origin or "Başlangıç",
            "address": r.origin or "", "lat": None, "lon": None, "km": 0,
        })
        if r.waypoints:
            labels = r.waypoint_labels or []
            for i, wp in enumerate(r.waypoints):
                label = labels[i] if i < len(labels) else f"Durak {i+1}"
                stops_data.append({
                    "kind": "waypoint", "name": label,
                    "address": wp, "lat": None, "lon": None, "km": None,
                })
        stops_data.append({
            "kind": "destination", "name": r.destination or "Varış",
            "address": r.destination or "", "lat": None, "lon": None,
            "km": float(r.distance_km or 0),
        })

    return RouteHistoryItem(
        id=r.id,
        origin=r.origin,
        destination=r.destination,
        distance_km=float(r.distance_km or 0),
        duration_min=float(r.duration_min or 0),
        date=str(r.created_at) if r.created_at else None,
        polyline_encoded=r.polyline_encoded if include_polyline else None,
        waypoints=r.waypoints,
        waypoint_labels=r.waypoint_labels,
        label=r.label,
        weather_summary=r.weather_summary,
        warnings=r.warnings,
        narrative=r.narrative if include_polyline else None,
        stops=stops_data if include_polyline else None,
    ).model_dump()


@router.get("/routes", response_model=ApiResponse)
async def get_route_history(
    limit: int = Query(default=20, ge=1, le=50),
    user: dict = Depends(get_current_user),
):
    """Kullanıcının son rota geçmişini döner (polyline ve narrative liste için dışlanır)."""
    t_start = time.monotonic()

    try:
        async with async_session_maker() as session:
            result = await session.execute(select(User).where(User.id == UUID(user["user_id"])))
            db_user = result.scalars().first()
            if not db_user:
                return ApiResponse(
                    success=False,
                    error=ApiError(code="USER_NOT_FOUND", message="Kullanıcı bulunamadı."),
                    metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
                )

            result = await session.execute(
                select(RouteHistory)
                .where(RouteHistory.user_id == db_user.id)
                .order_by(RouteHistory.created_at.desc())
                .limit(limit)
            )
            routes = result.scalars().all()

            items = [_serialize_route(r, include_polyline=False) for r in routes]

            return ApiResponse(
                success=True,
                data=items,
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )

    except Exception as e:
        log.error(f"❌ [History] Rota geçmişi hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Rota geçmişi yüklenirken hata oluştu."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


@router.get("/routes/{route_id}", response_model=ApiResponse)
async def get_route_detail(
    route_id: int,
    user: dict = Depends(get_current_user),
):
    """Tek bir geçmiş rotanın tam detayını döner (polyline + waypoints + narrative dahil)."""
    t_start = time.monotonic()
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(RouteHistory).where(
                    RouteHistory.id == route_id,
                    RouteHistory.user_id == UUID(user["user_id"]),
                )
            )
            r = result.scalars().first()
            if not r:
                return ApiResponse(
                    success=False,
                    error=ApiError(code="NOT_FOUND", message="Rota bulunamadı."),
                    metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
                )
            return ApiResponse(
                success=True,
                data=_serialize_route(r, include_polyline=True),
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )
    except Exception as e:
        log.error(f"❌ [History] Rota detay hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Rota detayı alınamadı."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


@router.delete("/routes/{route_id}", response_model=ApiResponse)
async def delete_route(route_id: int, user: dict = Depends(get_current_user)):
    """Geçmişten tek rota siler."""
    t_start = time.monotonic()
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(RouteHistory).where(
                    RouteHistory.id == route_id,
                    RouteHistory.user_id == UUID(user["user_id"]),
                )
            )
            r = result.scalars().first()
            if not r:
                return ApiResponse(
                    success=False,
                    error=ApiError(code="NOT_FOUND", message="Rota bulunamadı."),
                    metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
                )
            await session.delete(r)
            await session.commit()
            return ApiResponse(
                success=True,
                data={"message": "Rota silindi."},
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )
    except Exception as e:
        log.error(f"❌ [History] Rota silme hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Rota silinemedi."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


@router.patch("/routes/{route_id}", response_model=ApiResponse)
async def update_route_label(
    route_id: int,
    req: RouteHistoryUpdateRequest,
    user: dict = Depends(get_current_user),
):
    """Rota etiketini günceller (örn. 'Rize Sahil Turu')."""
    t_start = time.monotonic()
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(RouteHistory).where(
                    RouteHistory.id == route_id,
                    RouteHistory.user_id == UUID(user["user_id"]),
                )
            )
            r = result.scalars().first()
            if not r:
                return ApiResponse(
                    success=False,
                    error=ApiError(code="NOT_FOUND", message="Rota bulunamadı."),
                    metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
                )
            # None değer "etiketi sil" anlamına gelir; boş string de aynı
            r.label = (req.label or None) if req.label != "" else None
            await session.commit()
            return ApiResponse(
                success=True,
                data=_serialize_route(r, include_polyline=False),
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )
    except Exception as e:
        log.error(f"❌ [History] Etiket güncelleme hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Etiket güncellenemedi."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


# ═══════════════════════════════════════════════════════════════════════════
# DAY PLAN HISTORY
# ═══════════════════════════════════════════════════════════════════════════

def _serialize_day_plan(d: DayPlanHistory) -> dict:
    return {
        "id": d.id,
        "created_at": str(d.created_at) if d.created_at else None,
        "plan_date": d.plan_date,
        "city": d.city,
        "activity_note": d.activity_note,
        "summary": d.summary,
        "schedule": d.schedule or [],
    }


@router.get("/day_plans", response_model=ApiResponse)
async def get_day_plan_history(
    limit: int = Query(default=20, ge=1, le=50),
    user: dict = Depends(get_current_user),
):
    """Kullanıcının geçmiş günlük plan (schedule) sonuçlarını listeler."""
    t_start = time.monotonic()
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(User).where(User.id == UUID(user["user_id"]))
            )
            db_user = result.scalars().first()
            if not db_user:
                return ApiResponse(
                    success=False,
                    error=ApiError(code="USER_NOT_FOUND", message="Kullanıcı bulunamadı."),
                    metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
                )
            result = await session.execute(
                select(DayPlanHistory)
                .where(DayPlanHistory.user_id == db_user.id)
                .order_by(DayPlanHistory.created_at.desc())
                .limit(limit)
            )
            items = [_serialize_day_plan(d) for d in result.scalars().all()]
            return ApiResponse(
                success=True,
                data=items,
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )
    except Exception as e:
        log.error(f"❌ [History] DayPlan geçmişi hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Günlük plan geçmişi yüklenemedi."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


@router.get("/day_plans/{plan_id}", response_model=ApiResponse)
async def get_day_plan_detail(
    plan_id: int,
    user: dict = Depends(get_current_user),
):
    t_start = time.monotonic()
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(DayPlanHistory).where(
                    DayPlanHistory.id == plan_id,
                    DayPlanHistory.user_id == UUID(user["user_id"]),
                )
            )
            d = result.scalars().first()
            if not d:
                return ApiResponse(
                    success=False,
                    error=ApiError(code="NOT_FOUND", message="Plan bulunamadı."),
                    metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
                )
            return ApiResponse(
                success=True,
                data=_serialize_day_plan(d),
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )
    except Exception as e:
        log.error(f"❌ [History] DayPlan detay hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Plan detayı alınamadı."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


# ═══════════════════════════════════════════════════════════════════════════
# CHAT HISTORY
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/chat", response_model=ApiResponse)
async def get_chat_history(
    session_id: str = Query(default="default_session"),
    limit: int = Query(default=20, ge=1, le=100),
    user: dict = Depends(get_current_user),
):
    """Redis'ten sohbet geçmişini döner."""
    t_start = time.monotonic()

    if not orchestrator.redis_client:
        return ApiResponse(
            success=True,
            data=[],
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )

    try:
        # Session key: user_id bazlı olmalı
        key = f"chat:{user['user_id']}:{session_id}"

        # Önce eski formatta dene, yoksa user-scoped key ile
        raw = orchestrator.redis_client.lrange(key, 0, -1)
        if not raw:
            # Fallback: eski format (session_id tabanlı)
            raw = orchestrator.redis_client.lrange(f"chat:{session_id}", 0, -1)

        messages = []
        for item in raw[-limit:]:  # Son N mesaj
            if isinstance(item, bytes):
                item = item.decode("utf-8")
            try:
                msg = json.loads(item)
                messages.append(
                    ChatHistoryItem(role=msg["role"], content=msg["content"]).model_dump()
                )
            except (json.JSONDecodeError, KeyError):
                continue

        return ApiResponse(
            success=True,
            data=messages,
            metadata=ApiMetadata(
                response_time_ms=_elapsed(t_start),
                session_id=session_id,
            ),
        )

    except Exception as e:
        log.error(f"❌ [History] Sohbet geçmişi hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Sohbet geçmişi yüklenirken hata oluştu."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


@router.delete("/chat", response_model=ApiResponse)
async def clear_chat_history(
    session_id: str = Query(default="default_session"),
    user: dict = Depends(get_current_user),
):
    """Belirtilen session'ın sohbet geçmişini temizler."""
    t_start = time.monotonic()

    if not orchestrator.redis_client:
        return ApiResponse(
            success=True,
            data={"message": "Redis bağlantısı yok, silinecek veri bulunamadı."},
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )

    try:
        # Her iki key formatını da temizle
        orchestrator.redis_client.delete(f"chat:{user['user_id']}:{session_id}")
        orchestrator.redis_client.delete(f"chat:{session_id}")

        log.info(f"🗑️ [History] Sohbet geçmişi temizlendi: {session_id} ({user['username']})")

        return ApiResponse(
            success=True,
            data={"message": "Sohbet geçmişi temizlendi."},
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )

    except Exception as e:
        log.error(f"❌ [History] Sohbet temizleme hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Sohbet geçmişi temizlenirken hata oluştu."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


# ═══════════════════════════════════════════════════════════════════════════
# CHAT SESSIONS LIST
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/sessions", response_model=ApiResponse)
async def get_chat_sessions(
    user: dict = Depends(get_current_user),
):
    """Kullanıcının tüm chat session'larını listeler."""
    t_start = time.monotonic()

    if not orchestrator.redis_client:
        return ApiResponse(
            success=True, data=[],
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )

    try:
        sessions = []
        pattern = f"chat:{user['user_id']}:*"

        for key in orchestrator.redis_client.scan_iter(pattern):
            key_str = key.decode() if isinstance(key, bytes) else key
            sid = key_str.split(":")[-1]
            count = orchestrator.redis_client.llen(key_str)

            # Son mesajı al
            last_raw = orchestrator.redis_client.lindex(key_str, -1)
            last_msg = None
            if last_raw:
                try:
                    if isinstance(last_raw, bytes):
                        last_raw = last_raw.decode("utf-8")
                    parsed = json.loads(last_raw)
                    last_msg = parsed.get("content", "")[:100]
                except Exception:
                    pass

            sessions.append(ChatSessionItem(
                session_id=sid,
                message_count=count,
                last_message=last_msg,
            ).model_dump())

        # En çok mesajı olan session'lar önce
        sessions.sort(key=lambda s: s["message_count"], reverse=True)

        return ApiResponse(
            success=True,
            data=sessions,
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )

    except Exception as e:
        log.error(f"❌ [History] Session listesi hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Session listesi yüklenirken hata oluştu."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )
