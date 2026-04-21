"""
api/auth.py — Kullanıcı Kayıt/Giriş/Token Yenileme

Mobil uygulama bu endpoint'leri kullanarak JWT token alır.
Tüm korumalı endpoint'ler bu token'ı "Authorization: Bearer <token>" olarak gönderir.
"""
import time
from fastapi import APIRouter, Depends
from sqlmodel import select

from core.db import async_session_maker, User
from api.schemas import (
    ApiResponse, ApiError, ApiMetadata,
    RegisterRequest, LoginRequest, RefreshRequest,
    TokenData, UserPublic, AuthResponse,
)
from api.deps import (
    hash_password, verify_password,
    create_access_token, create_refresh_token,
    decode_token, get_current_user,
    ACCESS_TOKEN_EXPIRE_HOURS,
)
from logger import log

router = APIRouter(prefix="/auth", tags=["Auth"])


# ═══════════════════════════════════════════════════════════════════════════
# REGISTER
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/register", response_model=ApiResponse)
async def register(req: RegisterRequest):
    """Yeni kullanıcı kaydı. Username benzersiz olmalı."""
    t_start = time.monotonic()

    try:
        async with async_session_maker() as session:
            # Username kontrolü
            existing = await session.execute(
                select(User).where(User.username == req.username)
            )
            if existing.scalars().first():
                return ApiResponse(
                    success=False,
                    error=ApiError(code="USER_EXISTS", message="Bu kullanıcı adı zaten alınmış."),
                    metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
                )

            # Kullanıcı oluştur
            user = User(
                username=req.username,
                password_hash=hash_password(req.password),
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

            # Token üret
            access = create_access_token(str(user.id), user.username)
            refresh = create_refresh_token(str(user.id))

            log.success(f"👤 [Auth] Yeni kullanıcı kaydı: {req.username}")

            return ApiResponse(
                success=True,
                data=AuthResponse(
                    token=TokenData(
                        access_token=access,
                        refresh_token=refresh,
                        expires_in=ACCESS_TOKEN_EXPIRE_HOURS * 3600,
                    ),
                    user=UserPublic(
                        id=str(user.id),
                        username=user.username,
                        created_at=str(user.created_at) if user.created_at else None,
                    ),
                ).model_dump(),
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )

    except Exception as e:
        log.error(f"❌ [Auth] Kayıt hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Kayıt sırasında bir hata oluştu."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


# ═══════════════════════════════════════════════════════════════════════════
# LOGIN
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/login", response_model=ApiResponse)
async def login(req: LoginRequest):
    """Kullanıcı girişi. Başarılı olursa JWT token döner."""
    t_start = time.monotonic()

    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(User).where(User.username == req.username)
            )
            user = result.scalars().first()

            if not user or not user.password_hash:
                return ApiResponse(
                    success=False,
                    error=ApiError(code="AUTH_FAILED", message="Kullanıcı adı veya şifre hatalı."),
                    metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
                )

            if not verify_password(req.password, user.password_hash):
                return ApiResponse(
                    success=False,
                    error=ApiError(code="AUTH_FAILED", message="Kullanıcı adı veya şifre hatalı."),
                    metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
                )

            access = create_access_token(str(user.id), user.username)
            refresh = create_refresh_token(str(user.id))

            log.info(f"🔓 [Auth] Giriş başarılı: {user.username}")

            return ApiResponse(
                success=True,
                data=AuthResponse(
                    token=TokenData(
                        access_token=access,
                        refresh_token=refresh,
                        expires_in=ACCESS_TOKEN_EXPIRE_HOURS * 3600,
                    ),
                    user=UserPublic(
                        id=str(user.id),
                        username=user.username,
                        created_at=str(user.created_at) if user.created_at else None,
                    ),
                ).model_dump(),
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )

    except Exception as e:
        log.error(f"❌ [Auth] Giriş hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Giriş sırasında bir hata oluştu."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


# ═══════════════════════════════════════════════════════════════════════════
# REFRESH TOKEN
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/refresh", response_model=ApiResponse)
async def refresh_token(req: RefreshRequest):
    """
    Access token süresi dolduğunda yeni token almak için.
    Body'de refresh_token gönderilir, yeni access + refresh token döner.
    """
    t_start = time.monotonic()

    try:
        # Refresh token'ı decode et
        payload = decode_token(req.refresh_token)
        if not payload:
            return ApiResponse(
                success=False,
                error=ApiError(code="TOKEN_INVALID", message="Geçersiz veya süresi dolmuş refresh token."),
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )

        if payload.get("type") != "refresh":
            return ApiResponse(
                success=False,
                error=ApiError(code="TOKEN_TYPE_ERROR", message="Bu endpoint için refresh token gerekli."),
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )

        user_id = payload["sub"]

        # Kullanıcıyı DB'den çek (username için)
        async with async_session_maker() as session:
            from uuid import UUID
            result = await session.execute(
                select(User).where(User.id == UUID(user_id))
            )
            db_user = result.scalars().first()

        username = db_user.username if db_user else "unknown"

        access = create_access_token(user_id, username)
        refresh = create_refresh_token(user_id)

        return ApiResponse(
            success=True,
            data=TokenData(
                access_token=access,
                refresh_token=refresh,
                expires_in=ACCESS_TOKEN_EXPIRE_HOURS * 3600,
            ).model_dump(),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )

    except Exception as e:
        log.error(f"❌ [Auth] Token yenileme hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Token yenileme başarısız."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


# ═══════════════════════════════════════════════════════════════════════════
# HELPER
# ═══════════════════════════════════════════════════════════════════════════

def _elapsed(t_start: float) -> int:
    return int((time.monotonic() - t_start) * 1000)
