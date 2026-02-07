import asyncpg
from .config import settings
from logger import log

# Samsun Pilot Region Bounding Box (Atakum/Ilkadim Area)
SAMSUN_BBOX = {
    "min_lat": 41.20, "max_lat": 41.45,
    "min_lon": 36.15, "max_lon": 36.45
}

def is_in_samsun(lat: float, lon: float) -> bool:
    """Check if the given coordinates are within the Samsun pilot region."""
    return (SAMSUN_BBOX["min_lat"] <= lat <= SAMSUN_BBOX["max_lat"] and
            SAMSUN_BBOX["min_lon"] <= lon <= SAMSUN_BBOX["max_lon"])

async def get_local_route(origin_lat, origin_lon, dest_lat, dest_lon):
    """
    Calculates a route using pgRouting (Dijkstra algorithm) with DIRECTED=FALSE
    to ensure maximum connectivity even in imperfect data.
    """
    conn = await asyncpg.connect(settings.DATABASE_URL)
    
    try:
        # --- 1. HEALTH CHECK ---
        ways_count = await conn.fetchval("SELECT count(*) FROM ways")
        vertices_count = await conn.fetchval("SELECT count(*) FROM ways_vertices_pgr")
        
        log.info(f"🔍 [DB STATS] Ways: {ways_count} | Vertices: {vertices_count}")

        if not vertices_count:
            log.warning("⚠️ [LOCAL ROUTING] Vertices BOŞ! Topoloji onarılıyor...")
            try:
                await conn.execute("SELECT pgr_createTopology('ways', 0.00001, 'the_geom', 'gid');")
                log.success("✅ [REPAIR] Topoloji oluşturuldu!")
            except Exception as e:
                log.error(f"🔥 [REPAIR FAIL] {e}")
                return None

        # --- 2. SRID DETECTION ---
        db_srid = await conn.fetchval("SELECT ST_SRID(the_geom) FROM ways LIMIT 1")
        if not db_srid: db_srid = 4326

        # --- 3. NODE FINDING ---
        if db_srid == 3857:
            pt_sql = "ST_Transform(ST_SetSRID(ST_MakePoint($1, $2), 4326), 3857)"
        else:
            pt_sql = "ST_SetSRID(ST_MakePoint($1, $2), 4326)"

        # Vertex tablosundan en yakın düğümü bul
        node_sql = f"""
        SELECT id FROM ways_vertices_pgr 
        ORDER BY the_geom <-> {pt_sql} 
        LIMIT 1;
        """
        
        source_node = await conn.fetchval(node_sql, origin_lon, origin_lat)
        target_node = await conn.fetchval(node_sql, dest_lon, dest_lat)

        # Yedek: Ways tablosundan bul
        if not source_node:
            fallback_sql = f"SELECT source FROM ways ORDER BY the_geom <-> {pt_sql} LIMIT 1;"
            source_node = await conn.fetchval(fallback_sql, origin_lon, origin_lat)
        if not target_node:
            fallback_sql = f"SELECT target FROM ways ORDER BY the_geom <-> {pt_sql} LIMIT 1;"
            target_node = await conn.fetchval(fallback_sql, dest_lon, dest_lat)

        if not source_node or not target_node:
            log.error(f"❌ [LOCAL ROUTING] Düğüm bulunamadı. (S:{source_node} T:{target_node})")
            return None

        log.info(f"🛣️ [LOCAL ROUTING] Rota Aranıyor: {source_node} -> {target_node}")

        # --- 4. RUN DIJKSTRA (DIRECTED = FALSE) ---
        # directed := false yaparak "Kopuk Ağ" sorunlarını bypass ediyoruz.
        route_sql = """
        SELECT b.the_geom
        FROM pgr_dijkstra(
            'SELECT gid as id, source, target, cost, reverse_cost FROM ways',
            $1::bigint, $2::bigint, directed := false
        ) a
        JOIN ways b ON (a.edge = b.gid);
        """
        rows = await conn.fetch(route_sql, source_node, target_node)
        
        if not rows:
            log.warning("⚠️ [LOCAL ROUTING] Dijkstra yol bulamadı (Ciddi Kopukluk Var).")
            # Son çare: Belki maliyetler NULL'dur?
            check_cost = await conn.fetchval("SELECT count(*) FROM ways WHERE cost IS NULL")
            if check_cost > 0:
                log.error(f"❌ HATA: {check_cost} adet yolun maliyeti (cost) NULL!")
            return None
        
        log.success(f"✅ [LOCAL ROUTING] Başarılı! {len(rows)} segment.")
        return rows

    except Exception as e:
        log.error(f"🔥 [LOCAL ROUTING] Kritik Hata: {e}")
        return None
    finally:
        await conn.close()