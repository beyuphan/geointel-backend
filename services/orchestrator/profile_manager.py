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
                context.append(f"🚗 ARAÇ BİLGİSİ: {vehicle['vehicle_name']} | Tip: {vehicle['fuel_type']} | Tüketim: {vehicle['avg_consumption']}L/100km")
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