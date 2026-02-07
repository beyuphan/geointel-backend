import asyncio
import asyncpg
import xml.etree.ElementTree as ET
import os

DB_DSN = "postgresql://user:password@geo_db:5432/geodb"
OSM_FILE = "/app/data/istanbul_pilot.osm"

# OSM Standart Hızları
SPEED_LIMITS = {
    'motorway': 110, 'trunk': 90, 'primary': 70,
    'secondary': 50, 'tertiary': 40, 'residential': 30,
    'service': 20, 'living_street': 10
}

async def import_osm():
    print("🔌 Veritabanına bağlanılıyor...")
    conn = await asyncpg.connect(DB_DSN)

    # 1. TABLOLARI SIFIRLA
    print("🧹 Tablolar temizleniyor...")
    await conn.execute("DROP TABLE IF EXISTS ways CASCADE;")
    await conn.execute("DROP TABLE IF EXISTS ways_vertices_pgr CASCADE;")
    
    # ibb_match_id: İBB verisiyle eşleşirse buraya ID yazacağız
    await conn.execute("""
        CREATE TABLE ways (
            gid SERIAL PRIMARY KEY,
            osm_id BIGINT,
            source BIGINT, target BIGINT,
            length_m FLOAT,
            cost_time FLOAT, reverse_cost_time FLOAT,
            name TEXT,
            maxspeed INTEGER,
            current_speed INTEGER, -- Canlı hız buraya
            ibb_match_id INTEGER,  -- EŞLEŞME ANAHTARI
            highway TEXT,
            the_geom GEOMETRY(LineString, 4326)
        );
    """)

    # 2. OSM PARSE ET
    print("📂 OSM Dosyası okunuyor...")
    if not os.path.exists(OSM_FILE):
        print("❌ Dosya yok! Önce indir.")
        return

    tree = ET.parse(OSM_FILE)
    root = tree.getroot()
    
    node_coords = {}
    for node in root.findall('node'):
        node_coords[int(node.get('id'))] = (float(node.get('lon')), float(node.get('lat')))

    print("🛣️ Yollar yükleniyor...")
    ways_data = []
    
    for way in root.findall('way'):
        tags = {t.get('k'): t.get('v') for t in way.findall('tag')}
        if 'highway' not in tags: continue
        
        highway = tags['highway']
        speed = SPEED_LIMITS.get(highway, 30)
        name = tags.get('name', 'Bilinmiyor')
        
        nd_refs = [int(nd.get('ref')) for nd in way.findall('nd')]
        if len(nd_refs) < 2: continue

        for i in range(len(nd_refs) - 1):
            s_id, t_id = nd_refs[i], nd_refs[i+1]
            if s_id in node_coords and t_id in node_coords:
                s_lon, s_lat = node_coords[s_id]
                t_lon, t_lat = node_coords[t_id]
                wkt = f"LINESTRING({s_lon} {s_lat}, {t_lon} {t_lat})"
                
                # Başlangıçta ibb_match_id NULL, current_speed = maxspeed
                ways_data.append((name, speed, speed, highway, wkt))

    print(f"💾 {len(ways_data)} parça OSM yolu yükleniyor...")
    await conn.executemany("""
        INSERT INTO ways (name, maxspeed, current_speed, highway, cost_time, the_geom)
        VALUES ($1, $2, $3, $4, 0, ST_GeomFromText($5, 4326))
    """, ways_data)

    # 3. TOPOLOJİ VE MALİYET
    print("🧮 Geometrik hesaplamalar...")
    await conn.execute("UPDATE ways SET length_m = ST_Length(the_geom::geography);")
    
    # Süre Hesabı: Mesafe / Hız
    await conn.execute("""
        UPDATE ways SET 
        cost_time = length_m / (current_speed / 3.6),
        reverse_cost_time = length_m / (current_speed / 3.6);
    """)

    print("🔗 Topoloji oluşturuluyor (pgr_createTopology)...")
    await conn.execute("SELECT pgr_createTopology('ways', 0.0001, 'the_geom', 'gid');")

    print("✅ OSM KURULUMU TAMAM!")
    await conn.close()

if __name__ == "__main__":
    asyncio.run(import_osm())