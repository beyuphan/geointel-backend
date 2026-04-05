"""
local_routing.py — v2.0

Değişiklikler:
- asyncpg.connect() → pool.acquire() (Connection Pool entegrasyonu)
- rain_factor parametresi eklendi — C_edge = L_edge × (1 + rain_factor × 0.3)
"""
import json
from .db import get_pool
from logger import log

# İstanbul Bounding Box
ISTANBUL_BBOX = {
    "min_lat": 40.80, "max_lat": 41.30,
    "min_lon": 28.50, "max_lon": 29.50,
}


def is_in_service_area(lat: float, lon: float) -> bool:
    return (
        ISTANBUL_BBOX["min_lat"] <= lat <= ISTANBUL_BBOX["max_lat"]
        and ISTANBUL_BBOX["min_lon"] <= lon <= ISTANBUL_BBOX["max_lon"]
    )


async def get_local_route(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    preference: str = "fastest",
    rain_factor: float = 0.0,
):
    """
    pgRouting (Dijkstra) ile yerel rota hesaplar.

    Args:
        preference: 'fastest' (trafik bazlı) | 'shortest' (mesafe bazlı)
        rain_factor: 0.0 (kuru) → 1.0 (sağanak).
                     Maliyet formülü: C_edge = L_edge × (1 + rain_factor × 0.3)
    """
    try:
        pool = get_pool()
    except RuntimeError:
        log.error("[LocalRouting] DB Pool henüz hazır değil.")
        return None

    # Maliyet çarpanı — max %30 ek maliyet yağmurda
    rain_multiplier = 1.0 + (min(max(rain_factor, 0.0), 1.0) * 0.3)

    try:
        async with pool.acquire() as conn:
            # 1. En Yakın Noktaları Bul (Smart Snap)
            node_sql = """
            SELECT id FROM ways_vertices_pgr
            ORDER BY the_geom <-> ST_SetSRID(ST_MakePoint($1, $2), 4326)
            LIMIT 1;
            """
            source_node = await conn.fetchval(node_sql, origin_lon, origin_lat)
            target_node = await conn.fetchval(node_sql, dest_lon, dest_lat)

            if not source_node or not target_node:
                log.error(
                    f"❌ [LOCAL ROUTING] Noktalar harita dışında "
                    f"(S:{source_node} T:{target_node})"
                )
                return None

            # 2. Dinamik Maliyet — Trafik + yağmur çarpanı birlikte
            # current_speed varsa actualtime, yoksa şehir içi varsayılan 25 km/s
            dynamic_cost = (
                f"CASE WHEN current_speed IS NOT NULL AND current_speed > 0 "
                f"THEN (length_m / (current_speed / 3.6)) * {rain_multiplier} "
                f"ELSE (length_m / (25.0 / 3.6)) * {rain_multiplier} END"
            )

            route_sql = f"""
            SELECT
                sum(b.length_m)                                       AS total_meters,
                sum({dynamic_cost})                                   AS total_seconds,
                avg(COALESCE(b.current_speed, 25))                    AS avg_speed,
                ST_AsGeoJSON(ST_MakeLine(b.the_geom ORDER BY a.seq)) AS geometry
            FROM pgr_dijkstra(
                'SELECT gid AS id, source, target,
                 {dynamic_cost} AS cost,
                 {dynamic_cost} AS reverse_cost FROM ways',
                $1::bigint, $2::bigint, directed := false
            ) a
            JOIN ways b ON (a.edge = b.gid);
            """

            row = await conn.fetchrow(route_sql, source_node, target_node)

            if not row or not row["geometry"]:
                log.warning("⚠️ [LOCAL ROUTING] Rota bulunamadı.")
                return None

            # 3. Trafik Durumu Analizi
            avg_speed = row["avg_speed"] or 40.0
            total_seconds = row["total_seconds"] or 0
            total_meters = row["total_meters"] or 0

            # İdeal süre baseline: 60 km/h = 16.67 m/s
            ideal_seconds = total_meters / 16.67
            delay_seconds = max(0, total_seconds - ideal_seconds)

            if avg_speed < 10:
                traffic_status, traffic_color = "STOPPED", "red"
            elif avg_speed < 25:
                traffic_status, traffic_color = "HEAVY", "orange"
            elif avg_speed < 45:
                traffic_status, traffic_color = "MODERATE", "yellow"
            else:
                traffic_status, traffic_color = "FREE_FLOW", "green"

            result = {
                "mode": preference,
                "distance_km": round(total_meters / 1000.0, 2),
                "duration_min": round(total_seconds / 60.0, 1),
                "avg_speed_kmh": round(avg_speed, 1),
                "traffic_status": traffic_status,
                "traffic_color": traffic_color,
                "delay_min": round(delay_seconds / 60.0, 1),
                "rain_factor_applied": rain_factor,
                "geometry": json.loads(row["geometry"]),
                "data_source": "GeoIntel Local Routing (IBB Live Data)",
            }

            log.success(
                f"✅ [LOCAL ROUTING] {result['distance_km']} km, "
                f"{result['duration_min']} dk | {traffic_status} | "
                f"rain_factor={rain_factor}"
            )
            return result

    except Exception as e:
        log.error(f"🔥 [LOCAL ROUTING] Kritik Hata: {e}")
        return None