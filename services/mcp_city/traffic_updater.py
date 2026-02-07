import asyncio
import asyncpg
import requests
import time
import json

DB_DSN = "postgresql://user:password@geo_db:5432/geodb"
LIVE_API_URL = "https://tkmservices.ibb.gov.tr/web/api/TrafficData/v4/SegmentData"

async def update_traffic():
    print("📡 İBB Canlı Trafik Sunucusuna Bağlanılıyor...")
    
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://uym.ibb.gov.tr/",
        "Origin": "https://uym.ibb.gov.tr"
    }

    try:
        # 1. VERİYİ ÇEK
        response = requests.get(LIVE_API_URL, headers=headers, timeout=10)
        
        if response.status_code != 200:
            print(f"❌ API Hatası: {response.status_code}")
            return

        raw_data = response.json()
        
        # --- DÜZELTME BURADA ---
        # Gelen veri {'Date': '...', 'Data': [...]} formatında olabilir.
        if isinstance(raw_data, dict) and "Data" in raw_data:
            traffic_list = raw_data["Data"]
        elif isinstance(raw_data, list):
            traffic_list = raw_data
        else:
            print("❌ Beklenmeyen veri formatı!")
            print(str(raw_data)[:200])
            return
            
        print(f"📥 {len(traffic_list)} adet canlı hız verisi işleniyor...")

        # 2. VERİYİ HAZIRLA (Toplu Güncelleme İçin)
        updates = []
        zero_speed_count = 0
        
        for item in traffic_list:
            seg_id = item.get("S") # Segment ID
            speed = item.get("V")  # Hız (Velocity)
            
            if seg_id is None or speed is None: continue
            
            # Trafik durmuşsa (0 veya negatif), rota hesaplanabilsin diye minik bir hız ver (3 km/s)
            # Amaç: O yoldan kaçsın ama "yol yok" sanmasın.
            if speed <= 0:
                speed = 3
                zero_speed_count += 1
                
            updates.append((speed, seg_id))

        if not updates:
            print("⚠️ Hiçbir veri parse edilemedi.")
            return

        print(f"📊 Veritabanına Yazılacak: {len(updates)} satır (Kilitli Trafik: {zero_speed_count})")

        # 3. VERİTABANINA BAS (Batch Update)
        conn = await asyncpg.connect(DB_DSN)
        
        print("⚡ Hızlar güncelleniyor...")
        start_time = time.time()

        # Geçici tablo oluşturup join ile update etmek en hızlısıdır
        await conn.execute("CREATE TEMP TABLE traffic_updates (speed INT, seg_id INT);")
        
        # Python listesini SQL'e dök
        await conn.copy_records_to_table('traffic_updates', records=updates)
        
        # A) Hızları Güncelle (OSM tablomuzdaki 'current_speed' alanı)
        await conn.execute("""
            UPDATE ways w
            SET current_speed = t.speed
            FROM traffic_updates t
            WHERE w.ibb_match_id = t.seg_id;
        """)
        
        # B) Maliyetleri (Süreleri) Yeniden Hesapla
        # Formül: Süre = Yol / (Hız / 3.6)
        # Hız düştükçe süre artar, algoritma oradan kaçar.
        await conn.execute("""
            UPDATE ways 
            SET cost_time = length_m / (current_speed / 3.6),
                reverse_cost_time = length_m / (current_speed / 3.6)
            WHERE ibb_match_id IS NOT NULL;
        """)
        
        duration = time.time() - start_time
        print(f"✅ GÜNCELLEME TAMAMLANDI! ({duration:.2f} sn)")
        
        # Kontrol Sorgusu (Ortalama hız değişmiş mi?)
        stats = await conn.fetchrow("""
            SELECT AVG(current_speed) as avg_spd, COUNT(*) as cnt 
            FROM ways WHERE ibb_match_id IS NOT NULL;
        """)
        print(f"📉 Güncellenen Yol Sayısı: {stats['cnt']}")
        print(f"🏎️ İstanbul Anlık Hız Ortalaması: {stats['avg_spd']:.1f} km/s")

        await conn.close()

    except Exception as e:
        print(f"🔥 Kritik Hata: {e}")

if __name__ == "__main__":
    asyncio.run(update_traffic())