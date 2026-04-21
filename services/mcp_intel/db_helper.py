"""
db_helper.py — v2.0 (Connection Pool + Lifecycle Management)

Değişiklikler:
- asyncpg.connect() → asyncpg.Pool (her sorguda yeni bağlantı yerine pool)
- Pool startup/shutdown lifecycle'a bağlandı
- NULL-safe sorgular
"""
import asyncpg
import os
from loguru import logger
from datetime import datetime

# Docker network içindeki DB adresi
DB_DSN = os.getenv("DATABASE_URL", "postgresql://user:password@geo_db:5432/geodb")

# Global connection pool
_pool: asyncpg.Pool | None = None


async def init_pool():
    """Server başlarken bir kez çağrılır — pool oluşturur."""
    global _pool
    if _pool is not None:
        return
    try:
        _pool = await asyncpg.create_pool(
            DB_DSN,
            min_size=2,
            max_size=10,
            command_timeout=30,
            statement_cache_size=0,
        )
        logger.success("✅ [Intel DB Pool] asyncpg connection pool başlatıldı (min=2, max=10)")
    except Exception as e:
        logger.error(f"❌ [Intel DB Pool] Pool başlatılamadı: {e}")
        _pool = None


async def close_pool():
    """Server kapanırken bir kez çağrılır — pool'u temizler."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("🔌 [Intel DB Pool] Bağlantı havuzu kapatıldı.")


def get_pool() -> asyncpg.Pool:
    """Pool'u döner; başlatılmamışsa hata fırlatır."""
    if _pool is None:
        raise RuntimeError("Intel DB Pool henüz başlatılmadı. init_pool() çağrılmamış.")
    return _pool


class DBHelper:
    # ---------------------------------------------------------
    # 1. AKARYAKIT (UPSERT: Güncelle veya Ekle)
    # ---------------------------------------------------------
    @staticmethod
    async def save_fuel_prices(data_list, city_name="bilinmiyor"):
        if not data_list: return
        pool = get_pool()
        async with pool.acquire() as conn:
            try:
                # Şema bozukluğu var ise tabloyu garanti altına al
                try:
                    await conn.execute("ALTER TABLE fuel_prices ADD CONSTRAINT unique_fuel_prices UNIQUE(city, district, company);")
                except Exception:
                    pass

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

    # ---------------------------------------------------------
    # 2. ECZANE (CITY REFRESH: Şehir bazlı sil-yaz)
    # ---------------------------------------------------------
    @staticmethod
    async def save_pharmacies(data_list, city):
        if not data_list: return
        pool = get_pool()
        async with pool.acquire() as conn:
            try:
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

    # ---------------------------------------------------------
    # 3. SPOR MÜSABAKALARI (FULL REFRESH: Tabloyu boşalt-yaz)
    # ---------------------------------------------------------
    @staticmethod
    async def save_matches(data_list):
        if not data_list: return
        pool = get_pool()
        async with pool.acquire() as conn:
            try:
                await conn.execute("TRUNCATE TABLE sports_matches RESTART IDENTITY")
                
                query = """
                INSERT INTO sports_matches (home_team, away_team, match_date, stadium, city, traffic_impact_level)
                VALUES ($1, $2, $3, $4, $5, $6)
                """
                
                rows = []
                for m in data_list:
                    m_date = m.get('time', m.get('zaman'))
                    if isinstance(m_date, str):
                        try:
                            m_date = datetime.strptime(m_date, "%d.%m.%Y %H:%M")
                        except:
                            m_date = None

                    impact = 1
                    match_name = m.get('match', m.get('mac', 'Bilinmiyor vs Bilinmiyor'))
                    
                    if any(x in match_name.lower() for x in ['fenerbahçe', 'galatasaray', 'beşiktaş', 'trabzonspor']):
                        impact = 3
                    
                    parts = match_name.split(' vs ')
                    home_team = parts[0].strip() if len(parts) > 0 else match_name
                    away_team = parts[1].strip() if len(parts) > 1 else 'Bilinmiyor'

                    rows.append((
                        home_team,
                        away_team,
                        m_date,
                        m.get('stadium', m.get('stadyum', 'Bilinmiyor')),
                        m.get('city', m.get('sehir', 'Bilinmiyor')),
                        impact
                    ))

                await conn.executemany(query, rows)
                logger.success(f"💾 [DB] Fikstür yenilendi: {len(rows)} maç kaydedildi.")
            except Exception as e:
                logger.error(f"❌ [DB HATA] Maç Kaydı: {e}")

    # ---------------------------------------------------------
    # 4. ETKİNLİKLER (CITY REFRESH: Şehir bazlı sil-yaz)
    # ---------------------------------------------------------
    @staticmethod
    async def save_events(data_list, city):
        if not data_list: return
        pool = get_pool()
        async with pool.acquire() as conn:
            try:
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
                        str(e.get('date')),
                        "Genel",
                        e.get('link')
                    ))
                    
                await conn.executemany(query, rows)
                logger.success(f"💾 [DB] {city.upper()} için {len(data_list)} etkinlik kaydedildi.")
            except Exception as e:
                logger.error(f"❌ [DB HATA] Etkinlik Kaydı: {e}")


    # ---------------------------------------------------------
    # 5. VERİ OKUMA METODLARI
    # ---------------------------------------------------------

    @staticmethod
    async def read_fuel_prices(city: str, district: str):
        pool = get_pool()
        async with pool.acquire() as conn:
            query = """
                SELECT company as firma, gasoline as benzin, diesel as motorin, lpg 
                FROM fuel_prices 
                WHERE city = $1 AND district = $2
                AND updated_at >= NOW() - INTERVAL '24 hours'
                ORDER BY gasoline ASC
            """
            rows = await conn.fetch(query, city.lower(), district.lower())
            return [dict(row) for row in rows]

    @staticmethod
    async def read_pharmacies(city: str, district: str = ""):
        pool = get_pool()
        async with pool.acquire() as conn:
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

    @staticmethod
    async def read_matches():
        pool = get_pool()
        async with pool.acquire() as conn:
            query = """
                SELECT home_team || ' vs ' || away_team as mac, 
                       COALESCE(to_char(match_date, 'DD.MM.YYYY HH24:MI'), 'Tarih Bilinmiyor') as zaman, 
                       COALESCE(stadium, 'Bilinmiyor') as stadyum, 
                       COALESCE(city, 'Bilinmiyor') as sehir, 
                       COALESCE(traffic_impact_level, 1) as traffic_impact_level
                FROM sports_matches 
                WHERE match_date >= CURRENT_DATE OR match_date IS NULL
                ORDER BY match_date ASC NULLS LAST
            """
            rows = await conn.fetch(query)
            
            results = []
            for row in rows:
                r = dict(row)
                if r['traffic_impact_level'] >= 3:
                    r['uyari'] = "⚠️ DİKKAT: Yüksek Trafik Beklentisi! (Derbi/Büyük Maç)"
                else:
                    r['uyari'] = "Normal trafik seyri."
                results.append(r)
            return results

    @staticmethod
    async def read_events(city: str):
        pool = get_pool()
        async with pool.acquire() as conn:
            query = """
                SELECT title, venue, event_date as date, category, source_url as link
                FROM city_events 
                WHERE city = $1
            """
            rows = await conn.fetch(query, city.lower())
            return [dict(row) for row in rows]