import asyncpg
import os
from loguru import logger
from datetime import datetime

# Docker network içindeki DB adresi
DB_DSN = os.getenv("DATABASE_URL", "postgresql://user:password@geo_db:5432/geodb")
class DBHelper:
    @staticmethod
    async def get_connection():
        return await asyncpg.connect(DB_DSN)

    # ---------------------------------------------------------
    # 1. AKARYAKIT (UPSERT: Güncelle veya Ekle)
    # ---------------------------------------------------------
    @staticmethod
    async def save_fuel_prices(data_list, city_name="bilinmiyor"):
        if not data_list: return
        conn = await DBHelper.get_connection()
        try:
            # Şema bozukluğu var ise tabloyu garanti altına al (Eğer UNIQUE uçurulmuşsa)
            try:
                await conn.execute("ALTER TABLE fuel_prices ADD CONSTRAINT unique_fuel_prices UNIQUE(city, district, company);")
            except Exception:
                pass # Eğer zaten varsa veya constraint uyuşmazlığı varsa devam et

            # Aynı istasyon varsa fiyatı güncelle, yoksa yeni kayıt aç
            query = """
            INSERT INTO fuel_prices (city, district, company, gasoline, diesel, lpg, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, NOW())
            ON CONFLICT (city, district, company) 
            DO UPDATE SET 
                gasoline = EXCLUDED.gasoline,
                diesel = EXCLUDED.diesel,
                lpg = EXCLUDED.lpg,
                updated_at = NOW();
            """
            for item in data_list:
                # Hem Türkçe hem İngilizce (Live vs Fixed_Live) keyleri destekleyelim ki hata olmasın.
                target_city = item.get('city', item.get('sehir', city_name)).lower()
                target_district = item.get('district', item.get('ilce', 'bilinmiyor')).lower()
                target_company = item.get('company', item.get('firma', 'bilinmiyor'))
                
                await conn.execute(query, 
                    target_city,
                    target_district, 
                    target_company, 
                    float(item.get('gasoline', item.get('benzin', 0.0))), 
                    float(item.get('diesel', item.get('motorin', 0.0))), 
                    float(item.get('lpg', item.get('lpg', 0.0)))
                )
            logger.success(f"💾 [DB] {len(data_list)} adet yakıt verisi işlendi.")
        except Exception as e:
            logger.error(f"❌ [DB HATA] Yakıt Kaydı: {e}")
        finally:
            await conn.close()

    # ---------------------------------------------------------
    # 2. ECZANE (CITY REFRESH: Şehir bazlı sil-yaz)
    # ---------------------------------------------------------
    @staticmethod
    async def save_pharmacies(data_list, city):
        if not data_list: return
        conn = await DBHelper.get_connection()
        try:
            # O şehrin eski verisini temizle (Çünkü nöbetçi eczane her gün değişir)
            await conn.execute("DELETE FROM pharmacies WHERE city = $1", city.lower())
            
            query = """
            INSERT INTO pharmacies (city, district, name, address, phone, coordinates)
            VALUES ($1, $2, $3, $4, $5, $6)
            """
            rows = [
                (city.lower(), d['ilce'], d['isim'], d['adres'], d['tel'], d.get('koordinat')) 
                for d in data_list
            ]
            await conn.executemany(query, rows)
            logger.success(f"💾 [DB] {city.upper()} için {len(data_list)} eczane güncellendi.")
        except Exception as e:
            logger.error(f"❌ [DB HATA] Eczane Kaydı: {e}")
        finally:
            await conn.close()

    # ---------------------------------------------------------
    # 3. SPOR MÜSABAKALARI (FULL REFRESH: Tabloyu boşalt-yaz)
    # ---------------------------------------------------------
    @staticmethod
    async def save_matches(data_list):
        if not data_list: return
        conn = await DBHelper.get_connection()
        try:
            # Haftalık güncelleme olduğu için eski fikstürü temizliyoruz
            # Not: İleride geçmiş maçları tutmak istersen burayı "DELETE FROM ... WHERE date > NOW()" yapabiliriz.
            await conn.execute("TRUNCATE TABLE sports_matches RESTART IDENTITY")
            
            query = """
            INSERT INTO sports_matches (home_team, away_team, match_date, stadium, city, traffic_impact_level)
            VALUES ($1, $2, $3, $4, $5, $6)
            """
            
            rows = []
            for m in data_list:
                # Tarih formatını kontrol et (datetime objesi gelmeli)
                m_date = m.get('time', m.get('zaman')) # Scraper datetime objesi dönmeli
                if isinstance(m_date, str):
                    try:
                        m_date = datetime.strptime(m_date, "%d.%m.%Y %H:%M")
                    except:
                        m_date = None

                # Basit bir trafik etki puanı (İleride algoritma ile gelişecek)
                impact = 1
                match_name = m.get('match', m.get('mac', 'Bilinmiyor vs Bilinmiyor'))
                
                if any(x in match_name.lower() for x in ['fenerbahçe', 'galatasaray', 'beşiktaş', 'trabzonspor']):
                    impact = 3 # Derbi veya büyük maç
                
                parts = match_name.split(' vs ')
                home_team = parts[0].strip() if len(parts) > 0 else match_name
                away_team = parts[1].strip() if len(parts) > 1 else 'Bilinmiyor'

                rows.append((
                    home_team, # Home
                    away_team, # Away
                    m_date,
                    m.get('stadium', m.get('stadyum', 'Bilinmiyor')),
                    m.get('city', m.get('sehir', 'Bilinmiyor')),
                    impact
                ))

            await conn.executemany(query, rows)
            logger.success(f"💾 [DB] Fikstür yenilendi: {len(rows)} maç kaydedildi.")
        except Exception as e:
            logger.error(f"❌ [DB HATA] Maç Kaydı: {e}")
        finally:
            await conn.close()

    # ---------------------------------------------------------
    # 4. ETKİNLİKLER (CITY REFRESH: Şehir bazlı sil-yaz)
    # ---------------------------------------------------------
    @staticmethod
    async def save_events(data_list, city):
        if not data_list: return
        conn = await DBHelper.get_connection()
        try:
            # Şehrin eski etkinliklerini temizle
            await conn.execute("DELETE FROM city_events WHERE city = $1", city.lower())
            
            query = """
            INSERT INTO city_events (city, title, venue, event_date, category, source_url)
            VALUES ($1, $2, $3, $4, $5, $6)
            """
            
            rows = []
            for e in data_list:
                rows.append((
                    city.lower(),
                    e.get('title'),
                    e.get('venue'),
                    str(e.get('date')), # Tarih formatı karışık gelebilir, string tutalım
                    "Genel", # Kategori (Scraper geliştirilince burası dinamik olacak)
                    e.get('link')
                ))
                
            await conn.executemany(query, rows)
            logger.success(f"💾 [DB] {city.upper()} için {len(data_list)} etkinlik kaydedildi.")
        except Exception as e:
            logger.error(f"❌ [DB HATA] Etkinlik Kaydı: {e}")
        finally:
            await conn.close()


# ---------------------------------------------------------
    # 5. VERİ OKUMA METODLARI (Orchestrator İçin)
    # ---------------------------------------------------------

    @staticmethod
    async def read_fuel_prices(city: str, district: str):
        conn = await DBHelper.get_connection()
        try:
            # En güncel fiyatları getir
            query = """
                SELECT company as firma, gasoline as benzin, diesel as motorin, lpg 
                FROM fuel_prices 
                WHERE city = $1 AND district = $2
                AND updated_at >= NOW() - INTERVAL '24 hours'
                ORDER BY gasoline ASC
            """
            rows = await conn.fetch(query, city.lower(), district.lower())
            return [dict(row) for row in rows]
        finally:
            await conn.close()

    @staticmethod
    async def read_pharmacies(city: str, district: str = ""):
        conn = await DBHelper.get_connection()
        try:
            # İlçe filtresi varsa uygula, yoksa tüm şehri getir
            if district:
                query = """
                    SELECT name as isim, address as adres, phone as tel, district as ilce, coordinates as koordinat
                    FROM pharmacies 
                    WHERE city = $1 AND district = $2
                """
                rows = await conn.fetch(query, city.lower(), district.lower())
            else:
                query = """
                    SELECT name as isim, address as adres, phone as tel, district as ilce, coordinates as koordinat
                    FROM pharmacies 
                    WHERE city = $1
                """
                rows = await conn.fetch(query, city.lower())
            return [dict(row) for row in rows]
        finally:
            await conn.close()

    @staticmethod
    async def read_matches():
        conn = await DBHelper.get_connection()
        try:
            # Sadece bugünün ve geleceğin maçlarını getir
            query = """
                SELECT home_team || ' vs ' || away_team as mac, 
                       to_char(match_date, 'DD.MM.YYYY HH24:MI') as zaman, 
                       stadium as stadyum, city as sehir, traffic_impact_level
                FROM sports_matches 
                WHERE match_date >= CURRENT_DATE
                ORDER BY match_date ASC
            """
            rows = await conn.fetch(query)
            
            results = []
            for row in rows:
                r = dict(row)
                # Trafik uyarısını veriye ekleyelim
                if r['traffic_impact_level'] >= 3:
                    r['uyari'] = "⚠️ DİKKAT: Yüksek Trafik Beklentisi! (Derbi/Büyük Maç)"
                else:
                    r['uyari'] = "Normal trafik seyri."
                results.append(r)
            return results
        finally:
            await conn.close()

    @staticmethod
    async def read_events(city: str):
        conn = await DBHelper.get_connection()
        try:
            query = """
                SELECT title, venue, event_date as date, category, source_url as link
                FROM city_events 
                WHERE city = $1
            """
            rows = await conn.fetch(query, city.lower())
            return [dict(row) for row in rows]
        finally:
            await conn.close()