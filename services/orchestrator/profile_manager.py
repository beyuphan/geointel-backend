import os
from datetime import datetime, timezone, timedelta
from loguru import logger
from sqlmodel import select, update, delete
from core.db import async_session_maker, User, UserVehicle, SavedLocation, UserPreference, RouteHistory
from sqlalchemy.dialects.postgresql import insert

class ProfileManager:
    @staticmethod
    async def get_combined_context(session_id: str) -> str:
        """
        Kullanıcı profili, anlık konumu ve rota geçmişini tek bir context'te birleştirir.
        Hız için optimize edilmiştir.
        """
        # 1. Temel Profil Bilgileri
        profile = await ProfileManager.get_user_context()
        
        # 2. Anlık Konum (Redis)
        from core.mcp_client import orchestrator
        current_loc = "Bilinmiyor"
        if orchestrator.redis_client:
            try:
                loc_data = orchestrator.redis_client.get(f"loc:{session_id}")
                if loc_data:
                    current_loc = loc_data.decode("utf-8") if isinstance(loc_data, bytes) else loc_data
            except: pass
            
        # 3. Rota Geçmişi
        history = await ProfileManager.get_route_history(limit=3)
        history_str = ""
        if history:
            lines = [f"  - {r['origin']} -> {r['destination']} ({r['distance_km']}km)" for r in history]
            history_str = "\nROTA GEÇMİŞİ:\n" + "\n".join(lines)

        return (
            f"{profile}\n"
            f"ANLIK KONUM KOORDİNATLARI: {current_loc}\n"
            f"{history_str}"
        )

    @staticmethod
    async def get_user_context(username: str = "test_pilot") -> str:
        """
        LLM için kullanıcının özet profilini oluşturur (SQLModel ile refactor edildi).
        """
        context = []
        try:
            async with async_session_maker() as session:
                # 1. Kullanıcı ID'sini bul (Yoksa oluştur)
                stmt = insert(User).values(username=username)
                stmt = stmt.on_conflict_do_update(index_elements=['username'], set_={'username': username})
                await session.execute(stmt)
                await session.flush()
                
                # Retrieve the user id exactly
                result = await session.execute(select(User).where(User.username == username))
                user = result.scalars().first()
                if not user: return "Kullanıcı profil hatası."
                user_id = user.id

                # 2. Araç Bilgisi
                result = await session.execute(select(UserVehicle).where(UserVehicle.user_id == user_id, UserVehicle.is_primary == True))
                vehicle = result.scalars().first()
                if vehicle:
                    details = []
                    if vehicle.brand: details.append(vehicle.brand)
                    if vehicle.model: details.append(vehicle.model)
                    if vehicle.year: details.append(str(vehicle.year))
                    name_str = " ".join(details) if details else vehicle.vehicle_name
                    
                    cons_str = f"{vehicle.avg_consumption}L/100km"
                    if vehicle.city_consumption and vehicle.highway_consumption:
                        cons_str = f"Şehir: {vehicle.city_consumption}L | Uzun Yol: {vehicle.highway_consumption}L"
                    
                    context.append(f"🚗 ARAÇ: {name_str} | Tip: {vehicle.fuel_type} | Tüketim: {cons_str}")
                else:
                    context.append("🚗 ARAÇ BİLGİSİ: Bilinmiyor (Varsayılan: Benzinli kabul et)")

                # 3. Kayıtlı Konumlar (Ev, İş)
                result = await session.execute(select(SavedLocation).where(SavedLocation.user_id == user_id))
                locs = result.scalars().all()
                if locs:
                    loc_list = ", ".join([f"{l.name} ({l.coordinates})" for l in locs])
                    context.append(f"📍 KAYITLI KONUMLAR: {loc_list}")

                # 4. Tercihler (Takım, İlgi Alanı)
                result = await session.execute(select(UserPreference).where(UserPreference.user_id == user_id))
                prefs = result.scalars().all()
                if prefs:
                    pref_list = ", ".join([f"{p.key}={p.value}" for p in prefs])
                    context.append(f"❤️ TERCİHLER: {pref_list}")
                    
                await session.commit()

        except Exception as e:
            logger.error(f"Profil hatası (ORM): {e}")
            return "Profil verisi alınamadı."
        
        return "\n".join(context)

    @staticmethod
    async def get_saved_locations(username: str = "test_pilot") -> dict:
        """
        Kullanıcının kayıtlı konumlarını döner. SQLModel ile.
        """
        try:
            async with async_session_maker() as session:
                result = await session.execute(select(User).where(User.username == username))
                user = result.scalars().first()
                if not user: return {}

                result = await session.execute(select(SavedLocation).where(SavedLocation.user_id == user.id, SavedLocation.coordinates != None))
                locs = result.scalars().all()
                return {l.name.lower().strip(): l.coordinates for l in locs}
        except Exception as e:
            logger.warning(f"⚠️ [SavedLocations] Kayıtlı konumlar alınamadı: {e}")
            return {}

    @staticmethod
    async def update_vehicle_profile(
        brand: str, model: str, year: int, city_consumption: float, highway_consumption: float,
        fuel_type: str = "gasoline", username: str = "test_pilot"
    ):
        """Kullanıcının ana araç profilini detaylı olarak günceller."""
        try:
            async with async_session_maker() as session:
                result = await session.execute(select(User).where(User.username == username))
                user = result.scalars().first()
                if not user: return "Kullanıcı bulunamadı."
                
                avg_cons = (city_consumption + highway_consumption) / 2
                
                # Check if a primary vehicle exists
                result = await session.execute(select(UserVehicle).where(UserVehicle.user_id == user.id, UserVehicle.is_primary == True))
                vehicle = result.scalars().first()
                if vehicle:
                    vehicle.brand = brand
                    vehicle.model = model
                    vehicle.year = year
                    vehicle.city_consumption = city_consumption
                    vehicle.highway_consumption = highway_consumption
                    vehicle.avg_consumption = avg_cons
                    vehicle.fuel_type = fuel_type
                    vehicle.vehicle_name = f"{brand} {model}"
                else:
                    vehicle = UserVehicle(
                        user_id=user.id, vehicle_name=f"{brand} {model}",
                        brand=brand, model=model, year=year,
                        city_consumption=city_consumption, highway_consumption=highway_consumption,
                        avg_consumption=avg_cons, fuel_type=fuel_type, is_primary=True
                    )
                    session.add(vehicle)
                
                await session.commit()
            return f"✅ Araç profili güncellendi: {brand} {model} ({year})"
        except Exception as e:
            logger.error(f"Araç profil güncelleme hatası: {e}")
            return f"❌ Hata: {e}"

    @staticmethod
    async def update_memory(category: str, value: str, username: str = "test_pilot"):
        """Kullanıcının tercihlerini kaydeder."""
        try:
            async with async_session_maker() as session:
                result = await session.execute(select(User).where(User.username == username))
                user = result.scalars().first()
                if not user: return "Kullanıcı bulunamadı."

                if category == 'fuel_type':
                    await session.execute(delete(UserVehicle).where(UserVehicle.user_id == user.id))
                    vehicle = UserVehicle(user_id=user.id, vehicle_name="Varsayılan Araç", fuel_type=value.lower(), avg_consumption=7.0, is_primary=True)
                    session.add(vehicle)
                    msg = f"Araç yakıt tipi '{value}' olarak güncellendi."
                
                elif category == 'home_location':
                    loc = SavedLocation(user_id=user.id, name="Ev", coordinates=value, category="home")
                    session.add(loc)
                    msg = "Ev konumu kaydedildi."

                else:
                    pref = UserPreference(user_id=user.id, key=category, value=value)
                    session.add(pref)
                    msg = f"Tercih kaydedildi: {category} = {value}"

                await session.commit()
                return msg
        except Exception as e:
            logger.error(f"Hafıza kayıt hatası: {e}")
            return f"Hata oluştu: {e}"

    @staticmethod
    async def save_route_history(
        origin: str,
        destination: str,
        distance_km: float,
        duration_min: float,
        username: str = "test_pilot",
        polyline_encoded: str | None = None,
        waypoints: list | None = None,
        waypoint_labels: list | None = None,
        weather_summary: str | None = None,
        warnings: list | None = None,
        narrative: str | None = None,
        stops: list | None = None,
    ):
        """Rota geçmişini kaydeder. 24 saat içinde aynı rota tekrar kaydedilmez.
        Polyline + waypoint + LLM narrative kaydedilirse geçmişten tam re-open mümkün olur."""
        try:
            async with async_session_maker() as session:
                result = await session.execute(select(User).where(User.username == username))
                user = result.scalars().first()
                if not user:
                    return

                # 24 saatlik pencerede aynı rota var mı?
                cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)

                stmt = (
                    select(RouteHistory)
                    .where(
                        RouteHistory.user_id == user.id,
                        RouteHistory.origin == origin,
                        RouteHistory.destination == destination,
                        RouteHistory.created_at >= cutoff,
                    )
                    .limit(1)
                )
                existing = (await session.execute(stmt)).scalars().first()

                if existing:
                    # Aynı rota yeniden planlandı — yeni veriler varsa eskiyi güncelle
                    updated = False
                    if polyline_encoded and not existing.polyline_encoded:
                        existing.polyline_encoded = polyline_encoded
                        updated = True
                    if waypoints is not None and not existing.waypoints:
                        existing.waypoints = waypoints
                        updated = True
                    if waypoint_labels is not None and not existing.waypoint_labels:
                        existing.waypoint_labels = waypoint_labels
                        updated = True
                    if narrative and not existing.narrative:
                        existing.narrative = narrative
                        updated = True
                    if weather_summary and not existing.weather_summary:
                        existing.weather_summary = weather_summary
                        updated = True
                    if warnings is not None and not existing.warnings:
                        existing.warnings = warnings
                        updated = True
                    if stops is not None and not existing.stops:
                        existing.stops = stops
                        updated = True
                    if updated:
                        await session.commit()
                        logger.info(f"🔄 [RouteHistory] Mevcut rota detayları güncellendi: {origin} → {destination}")
                    else:
                        logger.info(
                            f"⏭️ [RouteHistory] 24h içinde aynı rota zaten var, atlandı: "
                            f"{origin} → {destination}"
                        )
                    return

                new_route = RouteHistory(
                    user_id=user.id,
                    origin=origin,
                    destination=destination,
                    distance_km=distance_km,
                    duration_min=duration_min,
                    created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    polyline_encoded=polyline_encoded,
                    waypoints=waypoints,
                    waypoint_labels=waypoint_labels,
                    weather_summary=weather_summary,
                    warnings=warnings,
                    narrative=narrative,
                    stops=stops,
                )
                session.add(new_route)
                await session.commit()
                logger.success(f"✅ [RouteHistory] Rota kaydedildi: {origin} → {destination}")
        except Exception as e:
            logger.warning(f"⚠️ [RouteHistory] Kayıt başarısız: {e}")

    @staticmethod
    async def get_route_history(username: str = "test_pilot", limit: int = 5) -> list:
        """Kullanıcının son rota geçmişini döndürür."""
        try:
            async with async_session_maker() as session:
                result = await session.execute(select(User).where(User.username == username))
                user = result.scalars().first()
                if not user: return []

                result = await session.execute(select(RouteHistory).where(RouteHistory.user_id == user.id).order_by(RouteHistory.id.desc()).limit(limit))
                routes = result.scalars().all()
                return [
                    {
                        "origin": r.origin, "destination": r.destination,
                        "distance_km": r.distance_km, "duration_min": r.duration_min,
                        "date": r.created_at or "Bilinmiyor"
                    } for r in routes
                ]
        except Exception as e:
            logger.warning(f"⚠️ [RouteHistory] Geçmiş okunamadı: {e}")
            return []