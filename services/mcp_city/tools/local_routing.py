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

        # 3. 🔥 SİHİRLİ SORGUSU (GELİŞTİRİLDİ) 🔥
        # Trafik verisi (current_speed) varsa onu, yoksa şehir içi varsayılan (25 km/s) hızı kullan.
        # Bu, İBB'nin izlemediği ara sokaklarda rotanın "uçmasını" engeller.
        dynamic_cost = "CASE WHEN current_speed IS NOT NULL AND current_speed > 0 THEN (length_m / (current_speed / 3.6)) ELSE (length_m / (25 / 3.6)) END"
        
        route_sql = f"""
        SELECT sum(b.length_m) as total_meters, 
               sum({dynamic_cost}) as total_seconds,
               avg(COALESCE(b.current_speed, 25)) as avg_speed,
               ST_AsGeoJSON(ST_MakeLine(b.the_geom ORDER BY a.seq)) as geometry
        FROM pgr_dijkstra(
            'SELECT gid as id, source, target, {dynamic_cost} as cost, {dynamic_cost} as reverse_cost FROM ways',
            $1::bigint, $2::bigint, directed := false
        ) a
        JOIN ways b ON (a.edge = b.gid);
        """
        
        row = await conn.fetchrow(route_sql, source_node, target_node)
        
        if not row or not row['geometry']:
            log.warning("⚠️ [LOCAL ROUTING] Rota bulunamadı.")
            return None
        
        # 4. TRAFİK YOĞUNLUK ANALİZİ
        avg_speed = row['avg_speed'] or 40.0
        total_seconds = row['total_seconds'] or 0
        total_meters = row['total_meters'] or 0
        
        # İdeal Süre (Baseline: 60 km/h)
        # 60 km/h = 16.6 m/s
        ideal_seconds = total_meters / 16.6
        delay_seconds = max(0, total_seconds - ideal_seconds)
        
        # Trafik Durumu Belirle
        if avg_speed < 10:
            traffic_status = "⛔ DURMUŞ"
            traffic_color = "red"
        elif avg_speed < 25:
            traffic_status = "🔴 YOĞUN"
            traffic_color = "orange"
        elif avg_speed < 45:
            traffic_status = "🟡 AKICI-YOĞUN"
            traffic_color = "yellow"
        else:
            traffic_status = "🟢 AKICI"
            traffic_color = "green"

        # Sonucu Formatla
        result = {
            "mode": preference,
            "distance_km": round(total_meters / 1000.0, 2),
            "duration_min": round(total_seconds / 60.0, 1),
            "avg_speed_kmh": round(avg_speed, 1),
            "traffic_status": traffic_status,
            "traffic_color": traffic_color,
            "delay_min": round(delay_seconds / 60.0, 1),
            "geometry": json.loads(row['geometry']),
            "data_source": "GeoIntel Yerel Rotalama (İBB Canlı Veri Destekli)"
        }

        log.success(f"✅ [LOCAL ROUTING] {result['distance_km']} km, {result['duration_min']} dk | Durum: {traffic_status}")
        return result

    except Exception as e:
        log.error(f"🔥 [LOCAL ROUTING] Kritik Hata: {e}")
        return None
    finally:
        await conn.close()