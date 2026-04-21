"""
api/history.py — Rota ve Sohbet Geçmişi

Kullanıcının rota geçmişi ve sohbet geçmişi endpoint'leri.
"""
import json
import time
from fastapi import APIRouter, Depends, Query

from core.db import async_session_maker, User, RouteHistory
from core.mcp_client import orchestrator
from api.schemas import (
    ApiResponse, ApiError, ApiMetadata,
    RouteHistoryItem, ChatHistoryItem,
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

@router.get("/routes", response_model=ApiResponse)
async def get_route_history(
    limit: int = Query(default=10, ge=1, le=50),
    user: dict = Depends(get_current_user),
):
    """Kullanıcının son rota geçmişini döner."""
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

            items = [
                RouteHistoryItem(
                    origin=r.origin,
                    destination=r.destination,
                    distance_km=r.distance_km,
                    duration_min=r.duration_min,
                    date=str(r.created_at) if r.created_at else None,
                ).model_dump()
                for r in routes
            ]

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
