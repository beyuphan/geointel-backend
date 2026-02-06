import asyncpg
from .config import settings
from logger import log

# Samsun Pilot Region Bounding Box (Atakum/Ilkadim Area)
# Coordinates outside this box will fallback to HERE Maps API.
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
    Calculates a route using pgRouting (Dijkstra algorithm) from the local PostGIS database.
    Returns: List of geometry rows or None if no route found.
    """
    conn = await asyncpg.connect(settings.DATABASE_URL)
    
    try:
        # 1. Find Nearest Nodes (Source & Target)
        # Snap the coordinates to the nearest road network node.
        node_sql = """
        SELECT id FROM ways_vertices_pgr 
        ORDER BY the_geom <-> ST_SetSRID(ST_MakePoint($1, $2), 4326) 
        LIMIT 1;
        """
        source_node = await conn.fetchval(node_sql, origin_lon, origin_lat)
        target_node = await conn.fetchval(node_sql, dest_lon, dest_lat)

        if not source_node or not target_node:
            log.warning("[LOCAL ROUTING] Source or Target node could not be found in local map data.")
            return None

        log.info(f"🛣️ [LOCAL ROUTING] Calculating path: Node {source_node} -> Node {target_node}")

        # 2. Run Dijkstra Algorithm
        # This query returns the geometry of each segment in the optimal path.
        route_sql = """
        SELECT b.the_geom
        FROM pgr_dijkstra(
            'SELECT gid as id, source, target, cost, reverse_cost FROM ways',
            $1, $2, directed := true
        ) a
        JOIN ways b ON (a.edge = b.gid);
        """
        rows = await conn.fetch(route_sql, source_node, target_node)
        
        if not rows:
            log.warning("[LOCAL ROUTING] No path found between selected nodes.")
            return None
            
        return rows

    except Exception as e:
        log.error(f"[LOCAL ROUTING] System Error: {e}")
        return None
    finally:
        await conn.close()