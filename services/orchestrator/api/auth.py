"""
api/auth.py — Kullanıcı Kayıt/Giriş/Token Yenileme/Hesap Yönetimi

Mobil uygulama bu endpoint'leri kullanarak JWT token alır.
Tüm korumalı endpoint'ler bu token'ı "Authorization: Bearer <token>" olarak gönderir.
"""
import json
import time
from datetime import datetime, timezone
from fastapi import APIRouter, Depends
from sqlmodel import select, delete

from core.db import (
    async_session_maker, User, UserVehicle,
    SavedLocation, UserPreference, RouteHistory,
)
from api.schemas import (
    ApiResponse, ApiError, ApiMetadata,
    RegisterRequest, LoginRequest, RefreshRequest,
    ForgotPasswordRequest, ResetPasswordRequest,
    TokenData, UserPublic, AuthResponse,
    VehicleResponse, LocationResponse, PreferenceResponse,
    RouteHistoryItem, ChatSessionItem, AccountExportResponse,
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
                email=req.email,
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
                        email=user.email,
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


# ═══════════════════════════════════════════════════════════════════════════
# ACCOUNT DELETE (App Store / Play Store zorunluluğu)
# ═══════════════════════════════════════════════════════════════════════════

@router.delete("/account", response_model=ApiResponse)
async def delete_account(user: dict = Depends(get_current_user)):
    """
    Kullanıcı hesabını ve tüm ilişkili verileri kalıcı olarak siler.
    Apple App Store ve Google Play Store kuralları gereği zorunlu.
    """
    t_start = time.monotonic()

    try:
        async with async_session_maker() as session:
            from uuid import UUID
            uid = UUID(user["user_id"])

            # Cascade silme — sıralı
            await session.execute(delete(UserPreference).where(UserPreference.user_id == uid))
            await session.execute(delete(SavedLocation).where(SavedLocation.user_id == uid))
            await session.execute(delete(UserVehicle).where(UserVehicle.user_id == uid))
            await session.execute(delete(RouteHistory).where(RouteHistory.user_id == uid))
            await session.execute(delete(User).where(User.id == uid))
            await session.commit()

        # Redis temizliği
        try:
            from core.mcp_client import orchestrator
            if orchestrator.redis_client:
                for key in orchestrator.redis_client.scan_iter(f"*{user['user_id']}*"):
                    orchestrator.redis_client.delete(key)
        except Exception:
            pass

        log.warning(f"🗑️ [Auth] Hesap silindi: {user['username']} ({user['user_id']})")

        return ApiResponse(
            success=True,
            data={"message": "Hesabınız ve tüm verileriniz kalıcı olarak silindi."},
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )

    except Exception as e:
        log.error(f"❌ [Auth] Hesap silme hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Hesap silinirken bir hata oluştu."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


# ═══════════════════════════════════════════════════════════════════════════
# ACCOUNT EXPORT (KVKK / GDPR)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/account/export", response_model=ApiResponse)
async def export_account_data(user: dict = Depends(get_current_user)):
    """
    Kullanıcının tüm kişisel verilerini JSON olarak döner.
    KVKK Madde 11 ve GDPR Article 20 (veri taşınabilirliği) uyumlu.
    """
    t_start = time.monotonic()

    try:
        async with async_session_maker() as session:
            from uuid import UUID
            uid = UUID(user["user_id"])

            # User
            u_result = await session.execute(select(User).where(User.id == uid))
            db_user = u_result.scalars().first()
            if not db_user:
                return ApiResponse(
                    success=False,
                    error=ApiError(code="USER_NOT_FOUND", message="Kullanıcı bulunamadı."),
                    metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
                )

            # Vehicle
            v_result = await session.execute(
                select(UserVehicle).where(UserVehicle.user_id == uid)
            )
            vehicle = v_result.scalars().first()

            # Locations
            l_result = await session.execute(
                select(SavedLocation).where(SavedLocation.user_id == uid)
            )
            locations = l_result.scalars().all()

            # Preferences
            p_result = await session.execute(
                select(UserPreference).where(UserPreference.user_id == uid)
            )
            preferences = p_result.scalars().all()

            # Route history
            r_result = await session.execute(
                select(RouteHistory).where(RouteHistory.user_id == uid)
                .order_by(RouteHistory.created_at.desc())
            )
            routes = r_result.scalars().all()

        # Chat sessions (Redis)
        chat_sessions = []
        try:
            from core.mcp_client import orchestrator
            if orchestrator.redis_client:
                pattern = f"chat:{user['user_id']}:*"
                for key in orchestrator.redis_client.scan_iter(pattern):
                    key_str = key.decode() if isinstance(key, bytes) else key
                    sid = key_str.split(":")[-1]
                    count = orchestrator.redis_client.llen(key_str)
                    chat_sessions.append(ChatSessionItem(
                        session_id=sid, message_count=count,
                    ).model_dump())
        except Exception:
            pass

        export = AccountExportResponse(
            user=UserPublic(
                id=str(db_user.id), username=db_user.username,
                email=db_user.email,
                created_at=str(db_user.created_at) if db_user.created_at else None,
            ),
            vehicle=VehicleResponse(
                id=vehicle.id, brand=vehicle.brand, model=vehicle.model,
                year=vehicle.year, fuel_type=vehicle.fuel_type,
                city_consumption=vehicle.city_consumption,
                highway_consumption=vehicle.highway_consumption,
                avg_consumption=vehicle.avg_consumption, is_primary=vehicle.is_primary,
            ) if vehicle else None,
            locations=[
                LocationResponse(id=l.id, name=l.name, coordinates=l.coordinates, category=l.category)
                for l in locations
            ],
            preferences=[PreferenceResponse(key=p.key, value=p.value) for p in preferences],
            route_history=[
                RouteHistoryItem(
                    origin=r.origin, destination=r.destination,
                    distance_km=r.distance_km, duration_min=r.duration_min,
                    date=str(r.created_at) if r.created_at else None,
                ) for r in routes
            ],
            chat_sessions=chat_sessions,
            exported_at=datetime.now(timezone.utc).isoformat(),
        )

        return ApiResponse(
            success=True,
            data=export.model_dump(),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )

    except Exception as e:
        log.error(f"❌ [Auth] Veri export hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Veriler dışa aktarılırken hata oluştu."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


# ═══════════════════════════════════════════════════════════════════════════
# FORGOT PASSWORD
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/forgot-password", response_model=ApiResponse)
async def forgot_password(req: ForgotPasswordRequest):
    """
    Şifre sıfırlama talebi. E-posta kayıtlıysa kısa ömürlü reset token üretir.
    Gerçek e-posta gönderimi için SMTP entegrasyonu gerekir (şimdilik token döner).
    """
    t_start = time.monotonic()

    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(User).where(User.email == req.email)
            )
            db_user = result.scalars().first()

        # Güvenlik: Kullanıcı olsun olmasın aynı mesajı dön
        if not db_user:
            return ApiResponse(
                success=True,
                data={"message": "E-posta adresiniz kayıtlıysa şifre sıfırlama bağlantısı gönderildi."},
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )

        # Kısa ömürlü reset token (1 saat)
        from jose import jwt as jose_jwt
        import os
        reset_payload = {
            "sub": str(db_user.id),
            "type": "password_reset",
            "exp": datetime.now(timezone.utc).timestamp() + 3600,
            "iat": datetime.now(timezone.utc).timestamp(),
        }
        reset_token = jose_jwt.encode(
            reset_payload,
            os.getenv("JWT_SECRET_KEY", "geointel-dev-secret-change-in-production"),
            algorithm="HS256",
        )

        # TODO: SMTP ile e-posta gönderimi — şimdilik Redis'e kaydet
        try:
            from core.mcp_client import orchestrator
            if orchestrator.redis_client:
                orchestrator.redis_client.setex(
                    f"reset:{str(db_user.id)}", 3600, reset_token
                )
        except Exception:
            pass

        log.info(f"🔑 [Auth] Şifre sıfırlama talebi: {req.email}")

        return ApiResponse(
            success=True,
            data={"message": "E-posta adresiniz kayıtlıysa şifre sıfırlama bağlantısı gönderildi."},
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )

    except Exception as e:
        log.error(f"❌ [Auth] Şifre sıfırlama hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="İşlem sırasında bir hata oluştu."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


@router.post("/reset-password", response_model=ApiResponse)
async def reset_password(req: ResetPasswordRequest):
    """Reset token ile şifre güncelleme."""
    t_start = time.monotonic()

    try:
        payload = decode_token(req.token)
        if not payload or payload.get("type") != "password_reset":
            return ApiResponse(
                success=False,
                error=ApiError(code="TOKEN_INVALID", message="Geçersiz veya süresi dolmuş sıfırlama bağlantısı."),
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )

        async with async_session_maker() as session:
            from uuid import UUID
            result = await session.execute(
                select(User).where(User.id == UUID(payload["sub"]))
            )
            db_user = result.scalars().first()
            if not db_user:
                return ApiResponse(
                    success=False,
                    error=ApiError(code="USER_NOT_FOUND", message="Kullanıcı bulunamadı."),
                    metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
                )

            db_user.password_hash = hash_password(req.new_password)
            await session.commit()

        log.success(f"🔓 [Auth] Şifre sıfırlandı: {db_user.username}")

        return ApiResponse(
            success=True,
            data={"message": "Şifreniz başarıyla güncellendi. Yeni şifrenizle giriş yapabilirsiniz."},
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )

    except Exception as e:
        log.error(f"❌ [Auth] Şifre güncelleme hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Şifre güncellenirken hata oluştu."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )
