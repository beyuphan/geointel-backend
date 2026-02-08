import asyncio
import asyncpg
import json
import time

DB_DSN = "postgresql://user:password@geo_db:5432/geodb"
IBB_JSON = "/app/data/istanbul_complete_static.json"

async def match_layers_fast():
    print("🔌 Veritabanına bağlanılıyor...")
    conn = await asyncpg.connect(DB_DSN)

    # 1. REFERANS TABLOSU (Temiz Başlangıç)
    print("🏗️ İBB Referans Tablosu sıfırlanıyor...")
    await conn.execute("DROP TABLE IF EXISTS ibb_reference CASCADE;")
    await conn.execute("""
        CREATE TABLE ibb_reference (
            id SERIAL PRIMARY KEY,
            segment_id INTEGER,
            the_geom GEOMETRY(LineString, 4326)
        );
    """)

    # 2. JSON YÜKLEME
    print("📂 İBB JSON verisi okunuyor...")
    with open(IBB_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)

    ibb_data = []
    for item in data:
        try:
            coords = json.loads(item["G"])
            # [Lat, Lon] -> "Lon Lat"
            points = [f"{p[1]} {p[0]}" for p in coords]
            wkt = f"LINESTRING({', '.join(points)})"
            ibb_data.append((item["S"], wkt))
        except: pass

    print(f"💾 {len(ibb_data)} İBB segmenti yükleniyor...")
    await conn.executemany("""
        INSERT INTO ibb_reference (segment_id, the_geom)
        VALUES ($1, ST_GeomFromText($2, 4326))
    """, ibb_data)

    # --- BURASI ÇOK ÖNEMLİ: İNDEKS OLUŞTURMA ---
    print("⚡ TURBO MODU AÇILIYOR (Spatial Index)...")
    start_idx = time.time()
    
    # OSM tablosuna indeks (Eğer yoksa)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_ways_geom ON ways USING GIST (the_geom);")
    # İBB tablosuna indeks
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_ibb_geom ON ibb_reference USING GIST (the_geom);")
    
    # İstatistikleri güncelle ki veritabanı akıllı plan yapsın
    await conn.execute("VACUUM ANALYZE ways;")
    await conn.execute("VACUUM ANALYZE ibb_reference;")
    
    print(f"✅ İndeksler hazır! ({time.time() - start_idx:.2f} sn)")

    # 3. MATCHING (HIZLANDIRILMIŞ)
    print("🧲 MAP MATCHING BAŞLIYOR (Optimize Edilmiş)...")
    start_match = time.time()
    
    # ST_DWithin: 0.0002 derece yaklaşık 20 metredir.
    # GIST indeksi sayesinde bu sorgu ışık hızında çalışır.
    match_sql = """
    UPDATE ways w
    SET ibb_match_id = i.segment_id
    FROM ibb_reference i
    WHERE w.ibb_match_id IS NULL
    AND ST_DWithin(w.the_geom, i.the_geom, 0.0002);
    """
    
    await conn.execute(match_sql)
    
    # Sonuçları Gör
    count = await conn.fetchval("SELECT count(*) FROM ways WHERE ibb_match_id IS NOT NULL;")
    total = await conn.fetchval("SELECT count(*) FROM ways;")
    
    print(f"⏱️ Eşleştirme Süresi: {time.time() - start_match:.2f} sn")
    print("-" * 40)
    print(f"✅ İŞLEM TAMAMLANDI!")
    print(f"📊 Toplam Yol: {total}")
    print(f"🔗 Trafiğe Bağlanan: {count}")
    print(f"💡 Başarı Oranı: %{count/total*100:.1f}")

    await conn.close()

if __name__ == "__main__":
    asyncio.run(match_layers_fast())