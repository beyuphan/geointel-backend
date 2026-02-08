import asyncio
import asyncpg
import time

# DB Bağlantısı
DB_DSN = "postgresql://user:password@geo_db:5432/geodb"

async def fix_graph():
    print("🚀 TOPOLOJİ TAMİRİ BAŞLIYOR...")
    print("   (Bu işlem harita boyutuna göre 1-2 dakika sürebilir, bekle...)")
    
    conn = await asyncpg.connect(DB_DSN)
    
    try:
        start = time.time()

        # 1. Toleransı Artırarak Topolojiyi Yeniden Kur (0.0001 -> ~10 metre)
        # Bu işlem kopuk kavşakları birleştirir.
        print("🔧 1/3: Yollar birbirine yapıştırılıyor (Snap)...")
        await conn.execute("""
            SELECT pgr_createTopology('ways', 0.0001, 'the_geom', 'gid');
        """)
        
        # 2. Hatalı Düğümleri Analiz Et ve Onar
        print("🔧 2/3: Graf analizi yapılıyor...")
        await conn.execute("""
            SELECT pgr_analyzeGraph('ways', 0.0001, 'the_geom', 'gid');
        """)
        
        # 3. Maliyetleri (Süre/Mesafe) Güncelle
        # Kopan yerler birleşince maliyetlerin güncellenmesi gerekir.
        print("🔧 3/3: Yol maliyetleri güncelleniyor...")
        await conn.execute("""
            UPDATE ways SET 
                source = pgr_startPoint(the_geom),
                target = pgr_endPoint(the_geom),
                length_m = ST_Length(the_geom::geography),
                cost_time = (ST_Length(the_geom::geography) / (CASE WHEN maxspeed IS NULL OR maxspeed = 0 THEN 30 ELSE maxspeed END)) * 3.6,
                reverse_cost_time = (ST_Length(the_geom::geography) / (CASE WHEN maxspeed IS NULL OR maxspeed = 0 THEN 30 ELSE maxspeed END)) * 3.6;
        """)

        end = time.time()
        print(f"✅ İŞLEM TAMAMLANDI! ({round(end-start, 2)} saniye)")
        print("   Artık rotalar binaların içinden geçmeyecek.")

    except Exception as e:
        print(f"❌ HATA: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(fix_graph())