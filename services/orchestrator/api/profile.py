"""
api/profile.py — Kullanıcı Profil Yönetimi

Araç, kayıtlı konum ve tercih CRUD endpoint'leri.
Tüm endpoint'ler JWT auth gerektirir.
"""
import time
from fastapi import APIRouter, Depends
from sqlmodel import select, delete

from core.db import async_session_maker, User, UserVehicle, SavedLocation, UserPreference
from api.schemas import (
    ApiResponse, ApiError, ApiMetadata,
    VehicleUpdate, VehicleCreate, VehicleResponse,
    LocationCreate, LocationResponse,
    PreferenceUpdate, PreferenceResponse,
    ProfileResponse, UserPublic,
)
from api.deps import get_current_user
from logger import log

router = APIRouter(prefix="/profile", tags=["Profile"])


def _elapsed(t_start: float) -> int:
    return int((time.monotonic() - t_start) * 1000)


async def _get_user_by_id(session, user_id: str) -> User | None:
    from uuid import UUID
    result = await session.execute(select(User).where(User.id == UUID(user_id)))
    return result.scalars().first()


# ═══════════════════════════════════════════════════════════════════════════
# GET PROFILE (Tam profil: araç + konumlar + tercihler)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("", response_model=ApiResponse)
async def get_profile(user: dict = Depends(get_current_user)):
    """Kullanıcının tam profilini döner: araç, konumlar, tercihler."""
    t_start = time.monotonic()

    try:
        async with async_session_maker() as session:
            db_user = await _get_user_by_id(session, user["user_id"])
            if not db_user:
                return ApiResponse(
                    success=False,
                    error=ApiError(code="USER_NOT_FOUND", message="Kullanıcı bulunamadı."),
                    metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
                )

            # Araç
            veh_result = await session.execute(
                select(UserVehicle).where(UserVehicle.user_id == db_user.id, UserVehicle.is_primary == True)
            )
            vehicle = veh_result.scalars().first()

            # Konumlar
            loc_result = await session.execute(
                select(SavedLocation).where(SavedLocation.user_id == db_user.id)
            )
            locations = loc_result.scalars().all()

            # Tercihler
            pref_result = await session.execute(
                select(UserPreference).where(UserPreference.user_id == db_user.id)
            )
            preferences = pref_result.scalars().all()

            return ApiResponse(
                success=True,
                data=ProfileResponse(
                    user=UserPublic(
                        id=str(db_user.id),
                        username=db_user.username,
                        created_at=str(db_user.created_at) if db_user.created_at else None,
                    ),
                    vehicle=VehicleResponse(
                        id=vehicle.id,
                        brand=vehicle.brand,
                        model=vehicle.model,
                        year=vehicle.year,
                        fuel_type=vehicle.fuel_type,
                        city_consumption=vehicle.city_consumption,
                        highway_consumption=vehicle.highway_consumption,
                        avg_consumption=vehicle.avg_consumption,
                        is_primary=vehicle.is_primary,
                    ) if vehicle else None,
                    locations=[
                        LocationResponse(id=l.id, name=l.name, coordinates=l.coordinates, category=l.category)
                        for l in locations
                    ],
                    preferences=[
                        PreferenceResponse(key=p.key, value=p.value)
                        for p in preferences
                    ],
                ).model_dump(),
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )

    except Exception as e:
        log.error(f"❌ [Profile] Profil okuma hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Profil yüklenirken hata oluştu."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


# ═══════════════════════════════════════════════════════════════════════════
# VEHICLE
# ═══════════════════════════════════════════════════════════════════════════

@router.put("/vehicle", response_model=ApiResponse)
async def update_vehicle(req: VehicleUpdate, user: dict = Depends(get_current_user)):
    """Kullanıcının ana aracını günceller veya oluşturur."""
    t_start = time.monotonic()

    try:
        async with async_session_maker() as session:
            db_user = await _get_user_by_id(session, user["user_id"])
            if not db_user:
                return ApiResponse(
                    success=False,
                    error=ApiError(code="USER_NOT_FOUND", message="Kullanıcı bulunamadı."),
                    metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
                )

            avg_cons = round((req.city_consumption + req.highway_consumption) / 2, 1)

            # Mevcut primary araç var mı?
            result = await session.execute(
                select(UserVehicle).where(UserVehicle.user_id == db_user.id, UserVehicle.is_primary == True)
            )
            vehicle = result.scalars().first()

            if vehicle:
                vehicle.brand = req.brand
                vehicle.model = req.model
                vehicle.year = req.year
                vehicle.fuel_type = req.fuel_type
                vehicle.city_consumption = req.city_consumption
                vehicle.highway_consumption = req.highway_consumption
                vehicle.avg_consumption = avg_cons
                vehicle.vehicle_name = f"{req.brand} {req.model}"
            else:
                vehicle = UserVehicle(
                    user_id=db_user.id,
                    vehicle_name=f"{req.brand} {req.model}",
                    brand=req.brand, model=req.model, year=req.year,
                    fuel_type=req.fuel_type,
                    city_consumption=req.city_consumption,
                    highway_consumption=req.highway_consumption,
                    avg_consumption=avg_cons,
                    is_primary=True,
                )
                session.add(vehicle)

            await session.commit()

            log.success(f"🚗 [Profile] Araç güncellendi: {req.brand} {req.model} ({user['username']})")

            return ApiResponse(
                success=True,
                data={"message": f"Araç güncellendi: {req.brand} {req.model} ({req.year})"},
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )

    except Exception as e:
        log.error(f"❌ [Profile] Araç güncelleme hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Araç güncellenirken hata oluştu."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


# ═══════════════════════════════════════════════════════════════════════════
# LOCATIONS
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/locations", response_model=ApiResponse)
async def get_locations(user: dict = Depends(get_current_user)):
    """Kullanıcının kayıtlı konumlarını listeler."""
    t_start = time.monotonic()

    try:
        async with async_session_maker() as session:
            db_user = await _get_user_by_id(session, user["user_id"])
            if not db_user:
                return ApiResponse(success=False, error=ApiError(code="USER_NOT_FOUND", message="Kullanıcı bulunamadı."))

            result = await session.execute(
                select(SavedLocation).where(SavedLocation.user_id == db_user.id)
            )
            locations = result.scalars().all()

            return ApiResponse(
                success=True,
                data=[
                    LocationResponse(id=l.id, name=l.name, coordinates=l.coordinates, category=l.category).model_dump()
                    for l in locations
                ],
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )

    except Exception as e:
        log.error(f"❌ [Profile] Konum listeleme hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Konumlar yüklenirken hata oluştu."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


@router.post("/locations", response_model=ApiResponse)
async def create_location(req: LocationCreate, user: dict = Depends(get_current_user)):
    """Yeni kayıtlı konum ekler (Ev, İş, Favori)."""
    t_start = time.monotonic()

    try:
        async with async_session_maker() as session:
            db_user = await _get_user_by_id(session, user["user_id"])
            if not db_user:
                return ApiResponse(success=False, error=ApiError(code="USER_NOT_FOUND", message="Kullanıcı bulunamadı."))

            loc = SavedLocation(
                user_id=db_user.id,
                name=req.name,
                coordinates=req.coordinates,
                category=req.category,
            )
            session.add(loc)
            await session.commit()
            await session.refresh(loc)

            log.info(f"📍 [Profile] Konum eklendi: {req.name} ({user['username']})")

            return ApiResponse(
                success=True,
                data=LocationResponse(
                    id=loc.id, name=loc.name, coordinates=loc.coordinates, category=loc.category
                ).model_dump(),
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )

    except Exception as e:
        log.error(f"❌ [Profile] Konum ekleme hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Konum eklenirken hata oluştu."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


@router.delete("/locations/{location_id}", response_model=ApiResponse)
async def delete_location(location_id: int, user: dict = Depends(get_current_user)):
    """Kayıtlı konumu siler."""
    t_start = time.monotonic()

    try:
        async with async_session_maker() as session:
            db_user = await _get_user_by_id(session, user["user_id"])
            if not db_user:
                return ApiResponse(success=False, error=ApiError(code="USER_NOT_FOUND", message="Kullanıcı bulunamadı."))

            result = await session.execute(
                select(SavedLocation).where(
                    SavedLocation.id == location_id,
                    SavedLocation.user_id == db_user.id
                )
            )
            loc = result.scalars().first()
            if not loc:
                return ApiResponse(
                    success=False,
                    error=ApiError(code="NOT_FOUND", message="Konum bulunamadı."),
                    metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
                )

            await session.delete(loc)
            await session.commit()

            return ApiResponse(
                success=True,
                data={"message": f"'{loc.name}' konumu silindi."},
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )

    except Exception as e:
        log.error(f"❌ [Profile] Konum silme hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Konum silinirken hata oluştu."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


# ═══════════════════════════════════════════════════════════════════════════
# PREFERENCES
# ═══════════════════════════════════════════════════════════════════════════

@router.put("/preferences", response_model=ApiResponse)
async def update_preference(req: PreferenceUpdate, user: dict = Depends(get_current_user)):
    """Kullanıcı tercihini günceller veya ekler (takım, mutfak tercihi vb.)."""
    t_start = time.monotonic()

    try:
        async with async_session_maker() as session:
            db_user = await _get_user_by_id(session, user["user_id"])
            if not db_user:
                return ApiResponse(success=False, error=ApiError(code="USER_NOT_FOUND", message="Kullanıcı bulunamadı."))

            # Aynı key varsa güncelle, yoksa oluştur
            result = await session.execute(
                select(UserPreference).where(
                    UserPreference.user_id == db_user.id,
                    UserPreference.key == req.key
                )
            )
            pref = result.scalars().first()

            if pref:
                pref.value = req.value
            else:
                pref = UserPreference(user_id=db_user.id, key=req.key, value=req.value)
                session.add(pref)

            await session.commit()

            return ApiResponse(
                success=True,
                data={"message": f"Tercih güncellendi: {req.key} = {req.value}"},
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )

    except Exception as e:
        log.error(f"❌ [Profile] Tercih güncelleme hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Tercih güncellenirken hata oluştu."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


# ═══════════════════════════════════════════════════════════════════════════
# VEHICLE GARAGE (Multi-Vehicle CRUD)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/vehicles", response_model=ApiResponse)
async def list_vehicles(user: dict = Depends(get_current_user)):
    """Kullanıcının tüm araçlarını listeler (Garage)."""
    t_start = time.monotonic()

    try:
        async with async_session_maker() as session:
            db_user = await _get_user_by_id(session, user["user_id"])
            if not db_user:
                return ApiResponse(success=False, error=ApiError(code="USER_NOT_FOUND", message="Kullanıcı bulunamadı."))

            result = await session.execute(
                select(UserVehicle).where(UserVehicle.user_id == db_user.id)
            )
            vehicles = result.scalars().all()

            return ApiResponse(
                success=True,
                data=[
                    VehicleResponse(
                        id=v.id, brand=v.brand, model=v.model, year=v.year,
                        fuel_type=v.fuel_type, city_consumption=v.city_consumption,
                        highway_consumption=v.highway_consumption,
                        avg_consumption=v.avg_consumption, is_primary=v.is_primary,
                    ).model_dump()
                    for v in vehicles
                ],
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )

    except Exception as e:
        log.error(f"❌ [Profile] Araç listeleme hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Araçlar yüklenirken hata oluştu."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


@router.post("/vehicles", response_model=ApiResponse)
async def add_vehicle(req: VehicleCreate, user: dict = Depends(get_current_user)):
    """Garaja yeni araç ekler."""
    t_start = time.monotonic()

    try:
        async with async_session_maker() as session:
            db_user = await _get_user_by_id(session, user["user_id"])
            if not db_user:
                return ApiResponse(success=False, error=ApiError(code="USER_NOT_FOUND", message="Kullanıcı bulunamadı."))

            avg_cons = round((req.city_consumption + req.highway_consumption) / 2, 1)

            # Eğer is_primary True ise diğerlerini False yap
            if req.is_primary:
                existing = await session.execute(
                    select(UserVehicle).where(UserVehicle.user_id == db_user.id, UserVehicle.is_primary == True)
                )
                for v in existing.scalars().all():
                    v.is_primary = False

            vehicle = UserVehicle(
                user_id=db_user.id,
                vehicle_name=f"{req.brand} {req.model}",
                brand=req.brand, model=req.model, year=req.year,
                fuel_type=req.fuel_type,
                city_consumption=req.city_consumption,
                highway_consumption=req.highway_consumption,
                avg_consumption=avg_cons,
                is_primary=req.is_primary,
            )
            session.add(vehicle)
            await session.commit()
            await session.refresh(vehicle)

            log.success(f"🚗 [Profile] Araç eklendi: {req.brand} {req.model} ({user['username']})")

            return ApiResponse(
                success=True,
                data=VehicleResponse(
                    id=vehicle.id, brand=vehicle.brand, model=vehicle.model,
                    year=vehicle.year, fuel_type=vehicle.fuel_type,
                    city_consumption=vehicle.city_consumption,
                    highway_consumption=vehicle.highway_consumption,
                    avg_consumption=vehicle.avg_consumption,
                    is_primary=vehicle.is_primary,
                ).model_dump(),
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )

    except Exception as e:
        log.error(f"❌ [Profile] Araç ekleme hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Araç eklenirken hata oluştu."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


@router.delete("/vehicles/{vehicle_id}", response_model=ApiResponse)
async def delete_vehicle(vehicle_id: int, user: dict = Depends(get_current_user)):
    """Garajdan araç siler."""
    t_start = time.monotonic()

    try:
        async with async_session_maker() as session:
            db_user = await _get_user_by_id(session, user["user_id"])
            if not db_user:
                return ApiResponse(success=False, error=ApiError(code="USER_NOT_FOUND", message="Kullanıcı bulunamadı."))

            result = await session.execute(
                select(UserVehicle).where(
                    UserVehicle.id == vehicle_id,
                    UserVehicle.user_id == db_user.id
                )
            )
            vehicle = result.scalars().first()
            if not vehicle:
                return ApiResponse(
                    success=False,
                    error=ApiError(code="NOT_FOUND", message="Araç bulunamadı."),
                    metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
                )

            name = f"{vehicle.brand} {vehicle.model}"
            await session.delete(vehicle)
            await session.commit()

            return ApiResponse(
                success=True,
                data={"message": f"'{name}' garajdan kaldırıldı."},
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )

    except Exception as e:
        log.error(f"❌ [Profile] Araç silme hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Araç silinirken hata oluştu."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )


@router.patch("/vehicles/{vehicle_id}/primary", response_model=ApiResponse)
async def set_primary_vehicle(vehicle_id: int, user: dict = Depends(get_current_user)):
    """Belirtilen aracı primary (ana araç) yapar."""
    t_start = time.monotonic()

    try:
        async with async_session_maker() as session:
            db_user = await _get_user_by_id(session, user["user_id"])
            if not db_user:
                return ApiResponse(success=False, error=ApiError(code="USER_NOT_FOUND", message="Kullanıcı bulunamadı."))

            # Tüm araçları primary=False yap
            all_result = await session.execute(
                select(UserVehicle).where(UserVehicle.user_id == db_user.id)
            )
            for v in all_result.scalars().all():
                v.is_primary = False

            # Seçileni primary yap
            target_result = await session.execute(
                select(UserVehicle).where(
                    UserVehicle.id == vehicle_id,
                    UserVehicle.user_id == db_user.id
                )
            )
            target = target_result.scalars().first()
            if not target:
                return ApiResponse(
                    success=False,
                    error=ApiError(code="NOT_FOUND", message="Araç bulunamadı."),
                    metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
                )

            target.is_primary = True
            await session.commit()

            log.info(f"🚗 [Profile] Primary araç değiştirildi: {target.brand} {target.model} ({user['username']})")

            return ApiResponse(
                success=True,
                data={"message": f"{target.brand} {target.model} ana araç olarak ayarlandı."},
                metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
            )

    except Exception as e:
        log.error(f"❌ [Profile] Primary araç hatası: {e}")
        return ApiResponse(
            success=False,
            error=ApiError(code="SERVER_ERROR", message="Ana araç değiştirilirken hata oluştu."),
            metadata=ApiMetadata(response_time_ms=_elapsed(t_start)),
        )
