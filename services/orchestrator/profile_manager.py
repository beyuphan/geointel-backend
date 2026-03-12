import asyncpg
import os
from loguru import logger

# Docker network içindeki DB adresi
# Eski satırı sil, bunu yapıştır:
DB_DSN = os.getenv("DATABASE_URL", "postgresql://user:password@geo_db:5432/geodb")
class ProfileManager:
    @staticmethod
    async def get_connection():
        return await asyncpg.connect(DB_DSN)

    @staticmethod
    async def get_user_context(username: str = "test_pilot") -> str:
        """
        LLM için kullanıcının özet profilini oluşturur.
        """
        conn = await ProfileManager.get_connection()
        context = []
        try:
            # 1. Kullanıcı ID'sini bul (Yoksa oluştur)
            user = await conn.fetchrow(
                "INSERT INTO users (username) VALUES ($1) ON CONFLICT (username) DO UPDATE SET username=EXCLUDED.username RETURNING id", 
                username
            )
            user_id = user['id']

            # 2. Araç Bilgisi
            vehicle = await conn.fetchrow("SELECT * FROM user_vehicles WHERE user_id = $1 AND is_primary = TRUE", user_id)
            if vehicle:
                details = []
                if vehicle.get('brand'): details.append(vehicle['brand'])
                if vehicle.get('model'): details.append(vehicle['model'])
                if vehicle.get('year'): details.append(str(vehicle['year']))
                name_str = " ".join(details) if details else vehicle['vehicle_name']
                
                cons_str = f"{vehicle['avg_consumption']}L/100km"
                if vehicle.get('city_consumption') and vehicle.get('highway_consumption'):
                    cons_str = f"Şehir: {vehicle['city_consumption']}L | Uzun Yol: {vehicle['highway_consumption']}L"
                
                context.append(f"🚗 ARAÇ: {name_str} | Tip: {vehicle['fuel_type']} | Tüketim: {cons_str}")
            else:
                context.append("🚗 ARAÇ BİLGİSİ: Bilinmiyor (Varsayılan: Benzinli kabul et)")

            # 3. Kayıtlı Konumlar (Ev, İş)
            locs = await conn.fetch("SELECT name, coordinates FROM saved_locations WHERE user_id = $1", user_id)
            if locs:
                loc_list = ", ".join([f"{l['name']} ({l['coordinates']})" for l in locs])
                context.append(f"📍 KAYITLI KONUMLAR: {loc_list}")

            # 4. Tercihler (Takım, İlgi Alanı)
            prefs = await conn.fetch("SELECT key, value FROM user_preferences WHERE user_id = $1", user_id)
            if prefs:
                pref_list = ", ".join([f"{p['key']}={p['value']}" for p in prefs])
                context.append(f"❤️ TERCİHLER: {pref_list}")

        except Exception as e:
            logger.error(f"Profil hatası: {e}")
            return "Profil verisi alınamadı."
        finally:
            await conn.close()
        
        return "\n".join(context)

    @staticmethod
    async def get_saved_locations(username: str = "test_pilot") -> dict:
        """
        Kullanıcının kayıtlı konumlarını döner.
        Örn: {"ev": "41.02,40.52", "iş": "41.05,39.73", "ayşe teyzem": "41.10,39.90"}
        Koordinat çözücüde 'Ev', 'İş' gibi kısayolları desteklemek için kullanılır.
        """
        conn = await ProfileManager.get_connection()
        try:
            user = await conn.fetchrow("SELECT id FROM users WHERE username = $1", username)
            if not user:
                return {}
            user_id = user["id"]

            rows = await conn.fetch(
                "SELECT name, coordinates FROM saved_locations WHERE user_id = $1 AND coordinates IS NOT NULL",
                user_id
            )
            return {row["name"].lower().strip(): row["coordinates"] for row in rows}

        except Exception as e:
            logger.warning(f"⚠️ [SavedLocations] Kayıtlı konumlar alınamadı: {e}")
            return {}
        finally:
            await conn.close()


    @staticmethod
    async def update_vehicle_profile(
        brand: str,
        model: str,
        year: int,
        city_consumption: float,
        highway_consumption: float,
        fuel_type: str = "gasoline",
        username: str = "test_pilot"
    ):
        """
        Kullanıcının ana araç profilini detaylı olarak günceller.
        """
        conn = await ProfileManager.get_connection()
        try:
            user = await conn.fetchrow("SELECT id FROM users WHERE username = $1", username)
            if not user: return "Kullanıcı bulunamadı."
            user_id = user['id']

            # Mevcut primary aracı bul veya yeni oluştur
            await conn.execute("""
                INSERT INTO user_vehicles 
                (user_id, vehicle_name, brand, model, year, city_consumption, highway_consumption, avg_consumption, fuel_type, is_primary)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, TRUE)
                ON CONFLICT (id) DO UPDATE SET
                    brand = EXCLUDED.brand,
                    model = EXCLUDED.model,
                    year = EXCLUDED.year,
                    city_consumption = EXCLUDED.city_consumption,
                    highway_consumption = EXCLUDED.highway_consumption,
                    avg_consumption = (EXCLUDED.city_consumption + EXCLUDED.highway_consumption) / 2,
                    fuel_type = EXCLUDED.fuel_type
            """, user_id, f"{brand} {model}", brand, model, year, city_consumption, highway_consumption, 
               (city_consumption + highway_consumption) / 2, fuel_type)
            
            return f"✅ Araç profili güncellendi: {brand} {model} ({year})"

        except Exception as e:
            logger.error(f"Araç profil güncelleme hatası: {e}")
            return f"❌ Hata: {e}"
        finally:
            await conn.close()

    @staticmethod
    async def update_memory(category: str, value: str, username: str = "test_pilot"):
        """
        Kullanıcının tercihlerini kaydeder.
        Args:
            category: 'team' (Takım), 'fuel_type' (Yakıt), 'home_location' (Ev)
            value: 'Trabzonspor', 'Diesel', '41.02,40.52'
        """
        conn = await ProfileManager.get_connection()
        try:
            user = await conn.fetchrow("SELECT id FROM users WHERE username = $1", username)
            if not user: return "Kullanıcı bulunamadı."
            
            user_id = user['id']

            if category == 'fuel_type':
                # Araç bilgisini güncelle
                await conn.execute("DELETE FROM user_vehicles WHERE user_id = $1", user_id)
                await conn.execute("""
                    INSERT INTO user_vehicles (user_id, vehicle_name, fuel_type, avg_consumption, is_primary)
                    VALUES ($1, 'Varsayılan Araç', $2, 7.0, TRUE)
                """, user_id, value.lower())
                return f"Araç yakıt tipi '{value}' olarak güncellendi."
            
            elif category == 'home_location':
                # Ev konumunu kaydet
                await conn.execute("""
                    INSERT INTO saved_locations (user_id, name, coordinates, category)
                    VALUES ($1, 'Ev', $2, 'home')
                """, user_id, value)
                return "Ev konumu kaydedildi."

            else:
                # Genel tercih (Takım vb.)
                await conn.execute("""
                    INSERT INTO user_preferences (user_id, key, value)
                    VALUES ($1, $2, $3)
                """, user_id, category, value)
                return f"Tercih kaydedildi: {category} = {value}"

        except Exception as e:
            logger.error(f"Hafıza kayıt hatası: {e}")
            return f"Hata oluştu: {e}"
        finally:
            await conn.close()

    @staticmethod
    async def save_route_history(
        origin: str,
        destination: str,
        distance_km: float,
        duration_min: float,
        username: str = "test_pilot"
    ):
        """
        Başarıyla hesaplanan rotayı kullanıcı geçmişine kaydeder.
        Aynı rota 24 saat içinde zaten kaydedilmişse atlar (duplicate önleme).
        """
        conn = await ProfileManager.get_connection()
        try:
            user = await conn.fetchrow("SELECT id FROM users WHERE username = $1", username)
            if not user:
                return
            user_id = user['id']

            # Duplicate kontrolü: Aynı origin+dest son 24 saatte var mı?
            existing = await conn.fetchrow("""
                SELECT id FROM route_history
                WHERE user_id = $1 AND origin = $2 AND destination = $3
                  AND created_at >= NOW() - INTERVAL '24 hours'
                LIMIT 1
            """, user_id, origin, destination)

            if existing:
                logger.info(f"⏭️ [RouteHistory] Aynı rota 24 saat içinde zaten kayıtlı, atlandı.")
                return

            await conn.execute("""
                INSERT INTO route_history (user_id, origin, destination, distance_km, duration_min, created_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
            """, user_id, origin, destination, distance_km, duration_min)

            logger.success(f"✅ [RouteHistory] Rota kaydedildi: {origin} -> {destination} ({distance_km}km, {duration_min}dk)")

        except Exception as e:
            # route_history tablosu yoksa sessizce geç (migration bekleniyor olabilir)
            logger.warning(f"⚠️ [RouteHistory] Kayıt başarısız (tablo yok mu?): {e}")
        finally:
            await conn.close()

    @staticmethod
    async def get_route_history(username: str = "test_pilot", limit: int = 5) -> list:
        """
        Kullanıcının son rota geçmişini döndürür.
        LLM'e kişiselleştirilmiş öneri yapabilmek için kullanılır.
        """
        conn = await ProfileManager.get_connection()
        try:
            user = await conn.fetchrow("SELECT id FROM users WHERE username = $1", username)
            if not user:
                return []
            user_id = user['id']

            rows = await conn.fetch("""
                SELECT origin, destination, distance_km, duration_min,
                       TO_CHAR(created_at, 'DD.MM.YYYY HH24:MI') AS date_str
                FROM route_history
                WHERE user_id = $1
                ORDER BY created_at DESC
                LIMIT $2
            """, user_id, limit)

            return [
                {
                    "origin": r["origin"],
                    "destination": r["destination"],
                    "distance_km": r["distance_km"],
                    "duration_min": r["duration_min"],
                    "date": r["date_str"]
                }
                for r in rows
            ]

        except Exception as e:
            logger.warning(f"⚠️ [RouteHistory] Geçmiş okunamadı: {e}")
            return []
        finally:
            await conn.close()