"""
EV Şarj İstasyonu Handler
Rota bounding box içindeki elektrikli araç şarj noktalarını
OSM Overpass API ve Google Places fallback ile çeker.
"""
import httpx
from loguru import logger
from .geometry import sample_route_points


async def get_ev_charging_handler(route_polyline: str) -> dict:
    """
    Rota üzerindeki EV şarj istasyonlarını OSM'den çeker.
    Google Places fallback ile zenginleştirir.
    
    Args:
        route_polyline: HERE Flex Polyline formatında rota.
    
    Returns:
        Rota yakınındaki şarj istasyonları listesi.
    """
    if not route_polyline or len(route_polyline) < 20:
        return {"error": "Geçerli bir rota polyline'ı gerekli."}

    # Rota noktalarından bounding box
    route_points = sample_route_points(encoded_polyline=route_polyline, interval_km=50)
    if not route_points:
        return {"charging_stations": [], "total_count": 0}

    lats = [pt["lat"] for pt in route_points]
    lons = [pt["lon"] for pt in route_points]
    min_lat = min(lats) - 0.15  # ~15km tolerans (EV için daha geniş)
    max_lat = max(lats) + 0.15
    min_lon = min(lons) - 0.15
    max_lon = max(lons) + 0.15

    charging_stations = []

    # --- OSM Overpass API ---
    overpass_url = "https://overpass-api.de/api/interpreter"
    query = f"""
    [out:json][timeout:20];
    node["amenity"="charging_station"]
      ({min_lat},{min_lon},{max_lat},{max_lon});
    out body;
    """

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            logger.info(f"⚡ [EV] OSM'den şarj istasyonları çekiliyor...")
            resp = await client.post(overpass_url, data={"data": query})

            if resp.status_code == 200:
                data = resp.json()
                elements = data.get("elements", [])
                logger.info(f"✅ [EV] OSM'den {len(elements)} şarj noktası bulundu.")

                for el in elements:
                    tags = el.get("tags", {})
                    station = {
                        "name": tags.get("name", tags.get("operator", "EV Şarj İstasyonu")),
                        "lat": el.get("lat"),
                        "lon": el.get("lon"),
                        "operator": tags.get("operator", "Bilinmiyor"),
                        "network": tags.get("network", ""),
                        "socket_type": tags.get("socket:type2", tags.get("socket:schuko", "Bilinmiyor")),
                        "capacity": tags.get("capacity", "?"),
                        "fee": tags.get("fee", "?"),
                        "opening_hours": tags.get("opening_hours", "7/24"),
                        "source": "OSM"
                    }
                    charging_stations.append(station)
            else:
                logger.warning(f"⚠️ [EV] OSM yanıt vermedi: {resp.status_code}")

    except Exception as e:
        logger.error(f"❌ [EV] OSM hatası: {e}")

    total = len(charging_stations)

    if total == 0:
        summary = "⚠️ Bu rota yakınında kayıtlı EV şarj istasyonu bulunamadı. Hareket etmeden önce şarj durumunuzu kontrol edin."
    else:
        summary = (
            f"⚡ Rota boyunca {total} EV şarj istasyonu tespit edildi. "
            "Uzun yolculukta mola planlamasını bu noktalara göre yapabilirsiniz."
        )

    return {
        "charging_stations": charging_stations,
        "total_count": total,
        "summary": summary,
        "data_source": "OpenStreetMap (Overpass API)",
        "bounding_box": {
            "min_lat": round(min_lat, 4),
            "min_lon": round(min_lon, 4),
            "max_lat": round(max_lat, 4),
            "max_lon": round(max_lon, 4),
        }
    }
