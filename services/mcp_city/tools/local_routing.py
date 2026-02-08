import asyncpg
import json
import os
from .config import settings
from logger import log

# İstanbul Bounding Box
ISTANBUL_BBOX = {
    "min_lat": 40.80, "max_lat": 41.30,
    "min_lon": 28.50, "max_lon": 29.50
}

def is_in_service_area(lat: float, lon: float) -> bool:
    return (ISTANBUL_BBOX["min_lat"] <= lat <= ISTANBUL_BBOX["max_lat"] and
            ISTANBUL_BBOX["min_lon"] <= lon <= ISTANBUL_BBOX["max_lon"])

async def get_local_route(origin_lat, origin_lon, dest_lat, dest_lon, preference="fastest"):
    """
    pgRouting (Dijkstra) kullanarak yerel rota hesaplar.
    """
    db_url = getattr(settings, "DATABASE_URL", "postgresql://user:password@geo_db:5432/geodb")
    conn = await asyncpg.connect(db_url)
    
    try:
        # 1. En Yakın Noktaları Bul (Smart Snap)
        node_sql = """
        SELECT id FROM ways_vertices_pgr 
        ORDER BY the_geom <-> ST_SetSRID(ST_MakePoint($1, $2), 4326) 
        LIMIT 1;
        """
        source_node = await conn.fetchval(node_sql, origin_lon, origin_lat)
        target_node = await conn.fetchval(node_sql, dest_lon, dest_lat)

        if not source_node or not target_node:
            log.error(f"❌ [LOCAL ROUTING] Noktalar harita dışında (S:{source_node} T:{target_node})")
            return None

        # 2. Maliyet Ayarı
        if preference == "shortest":
            sql_cost = "length_m"
            sql_reverse = "length_m" # Mesafe her iki yönde aynıdır
        else:
            # En Hızlı: Trafik verisi (süre) kullanılır.
            # cost_time: Gidiş süresi
            # reverse_cost_time: Dönüş süresi (Tek yön ise burada -1 veya çok yüksek sayı vardır)
            sql_cost = "cost_time"
            sql_reverse = "reverse_cost_time"

        # 3. 🔥 SİHİRLİ SORGUSU (BURASI DEĞİŞTİ) 🔥
        # ST_MakeLine ve ORDER BY a.seq sayesinde rota "ip gibi" düzgün çıkar.
        route_sql = f"""
        SELECT sum(b.length_m) as total_meters, 
               sum(b.cost_time) as total_seconds, 
               ST_AsGeoJSON(ST_MakeLine(b.the_geom ORDER BY a.seq)) as geometry
        FROM pgr_dijkstra(
            'SELECT gid as id, source, target, {sql_cost} as cost, {sql_reverse} as reverse_cost FROM ways',
            $1::bigint, $2::bigint, directed := false
        ) a
        JOIN ways b ON (a.edge = b.gid);
        """
        
        row = await conn.fetchrow(route_sql, source_node, target_node)
        
        if not row or not row['geometry']:
            log.warning("⚠️ [LOCAL ROUTING] Rota bulunamadı.")
            return None
        
        # Sonucu Formatla
        result = {
            "mode": preference,
            "distance_km": round(row['total_meters'] / 1000.0, 2) if row['total_meters'] else 0,
            "duration_min": round(row['total_seconds'] / 60.0, 1) if row['total_seconds'] else 0,
            "geometry": json.loads(row['geometry'])
        }

        log.success(f"✅ [LOCAL ROUTING] {result['distance_km']} km, {result['duration_min']} dk.")
        return result

    except Exception as e:
        log.error(f"🔥 [LOCAL ROUTING] Kritik Hata: {e}")
        return None
    finally:
        await conn.close()