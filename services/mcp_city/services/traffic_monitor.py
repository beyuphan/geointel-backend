import asyncio
import asyncpg
import requests
import time
import json
import logging
import os

# --- AYARLAR ---
# Veritabanı bağlantısı
DB_DSN = os.getenv("DB_DSN", "postgresql://user:password@geo_db:5432/geodb")
LIVE_API_URL = "https://tkmservices.ibb.gov.tr/web/api/TrafficData/v4/SegmentData"
UPDATE_INTERVAL = 120  # 2 Dakika (İdeal süre)

# --- LOGLAMA (PROFESYONEL GÖRÜNÜM) ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [TRAFFIC-MONITOR] - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("TrafficMonitor")

async def update_cycle():
    logger.info("🚀 İBB Canlı Trafik Servisi Başlatıldı (Daemon Modu)")
    
    while True: # SONSUZ DÖNGÜ: Program asla kapanmaz
        start_time = time.time()
        conn = None
        
        try:
            # 1. API'DEN VERİ ÇEK
            headers = {
                "User-Agent": "GeoIntel/1.0",
                "Referer": "https://uym.ibb.gov.tr/",
                "Origin": "https://uym.ibb.gov.tr"
            }
            
            # Timeout ekledik ki internet giderse script donmasın
            response = requests.get(LIVE_API_URL, headers=headers, timeout=20)
            
            if response.status_code != 200:
                logger.warning(f"⚠️ İBB API Hatası: {response.status_code}. Bekleniyor...")
                await asyncio.sleep(60) 
                continue

            raw_data = response.json()
            
            # Veri paketini güvenli şekilde aç
            traffic_list = []
            if isinstance(raw_data, dict) and "Data" in raw_data:
                traffic_list = raw_data["Data"]
            elif isinstance(raw_data, list):
                traffic_list = raw_data
            else:
                logger.error("❌ Veri formatı tanınamadı!")
                await asyncio.sleep(60)
                continue

            if not traffic_list:
                logger.warning("⚠️ API boş veri döndürdü.")
                await asyncio.sleep(60)
                continue

            # 2. VERİYİ SQL İÇİN HAZIRLA
            updates = []
            valid_ids = 0
            
            for item in traffic_list:
                seg_id = item.get("S")
                speed = item.get("V")
                
                # Eksik veri kontrolü
                if seg_id is None or speed is None: continue
                
                # Trafik durmuşsa (<=0), 3 km/s yap ki rota tamamen kopmasın
                if speed <= 0: speed = 3
                
                updates.append((speed, seg_id))
                valid_ids += 1

            # 3. VERİTABANI GÜNCELLEME
            conn = await asyncpg.connect(DB_DSN)
            
            # Geçici tablo oluştur (Bulk Update için en hızlı yöntem)
            await conn.execute("CREATE TEMP TABLE traffic_updates (speed INT, seg_id INT);")
            
            # Python listesini tek seferde SQL'e dök
            await conn.copy_records_to_table('traffic_updates', records=updates)
            
            # A) Hızları Güncelle
            await conn.execute("""
                UPDATE ways w
                SET current_speed = t.speed
                FROM traffic_updates t
                WHERE w.ibb_match_id = t.seg_id;
            """)
            
            # B) Maliyetleri (Süre = Yol / Hız) Yeniden Hesapla
            await conn.execute("""
                UPDATE ways 
                SET cost_time = length_m / (current_speed / 3.6),
                    reverse_cost_time = length_m / (current_speed / 3.6)
                WHERE ibb_match_id IS NOT NULL;
            """)

            # İstatistik al (Loglara basmak için)
            stats = await conn.fetchrow("SELECT AVG(current_speed) as avg FROM ways WHERE ibb_match_id IS NOT NULL")
            avg_speed = stats['avg'] if stats['avg'] else 0
            
            elapsed = time.time() - start_time
            logger.info(f"✅ GÜNCELLEME TAMAM: {valid_ids} yol güncellendi | Ort. Hız: {avg_speed:.1f} km/s | Süre: {elapsed:.2f}sn")

        except requests.exceptions.ConnectionError:
            logger.error("🔥 İnternet Bağlantısı Yok! Tekrar deneniyor...")
        except Exception as e:
            logger.error(f"🔥 Beklenmeyen Hata: {e}")
        finally:
            if conn:
                await conn.close()

        # 4. UYKU MODU (2 Dakika Bekle)
        logger.info(f"💤 Sistem {UPDATE_INTERVAL} saniye uykuya geçiyor...")
        await asyncio.sleep(UPDATE_INTERVAL)

if __name__ == "__main__":
    try:
        asyncio.run(update_cycle())
    except KeyboardInterrupt:
        logger.info("🛑 Servis manuel olarak durduruldu.")