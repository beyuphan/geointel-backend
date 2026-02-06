import asyncio
import asyncpg
import xml.etree.ElementTree as ET
import os

# Veritabanı Bilgileri (.env ile aynı)
DB_DSN = "postgresql://user:password@geo_db:5432/geodb"
OSM_FILE = "/app/data/samsun.osm"

async def run_import():
    print(f"🔌 Veritabanına bağlanılıyor...")
    conn = await asyncpg.connect(DB_DSN)
    
    # 1. TEMİZLİK
    print("🧹 Tablolar temizleniyor...")
    await conn.execute("DROP TABLE IF EXISTS ways CASCADE;")
    await conn.execute("DROP TABLE IF EXISTS ways_vertices_pgr CASCADE;")
    
    # 2. TABLO OLUŞTURMA (Standart pgRouting yapısı)
    print("🔨 'ways' tablosu oluşturuluyor...")
    await conn.execute("""
        CREATE TABLE ways (
            gid SERIAL PRIMARY KEY,
            source INTEGER,
            target INTEGER,
            cost FLOAT,
            reverse_cost FLOAT,
            length_m FLOAT,
            name TEXT,
            maxspeed INTEGER,
            the_geom GEOMETRY(LineString, 4326)
        );
    """)

    # 3. DOSYAYI OKU VE YÜKLE
    print("📂 XML okunuyor...")
    tree = ET.parse(OSM_FILE)
    root = tree.getroot()
    
    nodes = {n.get('id'): (n.get('lon'), n.get('lat')) for n in root.findall('node')}
    ways_to_insert = []
    
    print("🛣️ Yollar işleniyor...")
    for way in root.findall('way'):
        # Sadece araç yollarını al
        tags = {t.get('k'): t.get('v') for t in way.findall('tag')}
        if 'highway' not in tags: continue
        if tags['highway'] in ['footway', 'pedestrian', 'steps', 'corridor']: continue

        way_nodes = way.findall('nd')
        if len(way_nodes) < 2: continue
        
        # Koordinatları birleştirip Çizgi (LineString) yap
        coords = []
        for nd in way_nodes:
            ref = nd.get('ref')
            if ref in nodes:
                coords.append(f"{nodes[ref][0]} {nodes[ref][1]}")
        
        if len(coords) > 1:
            wkt = f"LINESTRING({', '.join(coords)})"
            speed = int(tags.get('maxspeed', '50').split()[0]) if 'maxspeed' in tags else 50
            name = tags.get('name', 'Unknown')
            ways_to_insert.append((name, speed, wkt))

    print(f"💾 {len(ways_to_insert)} adet yol veritabanına basılıyor...")
    
    # Veriyi Hızlıca Bas
    for name, speed, wkt in ways_to_insert:
        await conn.execute("""
            INSERT INTO ways (name, maxspeed, the_geom, source, target, cost, reverse_cost) 
            VALUES ($1, $2, ST_GeomFromText($3, 4326), 0, 0, 0, 0)
        """, name, speed, wkt)

    # 4. TOPOLOJİ (İŞİN BEYNİ BURASI)
    print("🧠 PostGIS Topoloji Motoru Çalıştırılıyor (pgr_createTopology)...")
    # Bu fonksiyon veritabanının kendi özelliğidir. Yolları analiz edip kavşakları bağlar.
    try:
        await conn.execute("SELECT pgr_createTopology('ways', 0.00001, 'the_geom', 'gid');")
        print("✅ Topoloji başarıyla kuruldu!")
    except Exception as e:
        print(f"⚠️ Topoloji uyarısı (önemsiz olabilir): {e}")

    # 5. ANALİZ VE MALİYET
    print("🧮 Uzunluklar hesaplanıyor...")
    await conn.execute("""
        UPDATE ways SET length_m = ST_Length(the_geom::geography);
        UPDATE ways SET cost = length_m; 
        UPDATE ways SET reverse_cost = length_m;
    """)
    
    print("🚀 İŞLEM TAMAM! Samsun veritabanına gömüldü.")
    await conn.close()

if __name__ == "__main__":
    asyncio.run(run_import())