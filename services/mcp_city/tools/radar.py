import httpx
import asyncio
from loguru import logger
from .config import settings
from .geometry import sample_route_points

# HERE Traffic API v7 - Safety Camera / Hazard endpoint
HERE_TRAFFIC_URL = "https://data.traffic.hereapi.com/v7/incidents"


async def get_radars_on_route_handler(route_polyline: str) -> dict:
    """
    HERE Maps Traffic API v7 kullanarak rota üzerindeki gerçek zamanlı hız kameraları
    ve güvenlik tehlikelerini (safety cameras, hazards) getirir.
    """
    if not route_polyline or len(route_polyline) < 20:
        return {"error": "Geçerli bir rota polyline'ı gerekli."}

    if not settings.HERE_API_KEY:
        return {"error": "HERE API anahtarı bulunamadı."}

    # Rota noktalarını örnekle (bounding box hesabı için)
    route_points = sample_route_points(encoded_polyline=route_polyline, interval_km=20)
    if not route_points:
        return {"cameras_on_route": [], "total_count": 0, "note": "Rota çözümlenemedi."}

    # Tüm rota noktalarından bounding box hesapla
    lats = [pt["lat"] for pt in route_points]
    lons = [pt["lon"] for pt in route_points]
    min_lat = min(lats) - 0.02
    max_lat = max(lats) + 0.02
    min_lon = min(lons) - 0.02
    max_lon = max(lons) + 0.02

    # HERE API v7 Sınırı: BBox genişliği ve yüksekliği 1 dereceyi geçemez.
    lat_diff = max_lat - min_lat
    lon_diff = max_lon - min_lon
    
    bboxes = []
    if lat_diff <= 1.0 and lon_diff <= 1.0:
        bboxes.append((min_lat, min_lon, max_lat, max_lon))
    else:
        step = 0.9
        curr_lat = min_lat
        while curr_lat < max_lat:
            next_lat = min(curr_lat + step, max_lat)
            curr_lon = min_lon
            while curr_lon < max_lon:
                next_lon = min(curr_lon + step, max_lon)
                bboxes.append((curr_lat, curr_lon, next_lat, next_lon))
                curr_lon += step
            curr_lat += step
        logger.info(f"🧩 [HERE Radar] Rota büyük ({lat_diff:.2f}x{lon_diff:.2f} deg), {len(bboxes)} parçaya bölündü.")

    if len(bboxes) > 20:
        bboxes = bboxes[:20]

    async def _fetch_bbox(bbox):
        b_min_lat, b_min_lon, b_max_lat, b_max_lon = bbox
        params = {
            "apiKey": settings.HERE_API_KEY,
            "in": f"bbox:{b_min_lon},{b_min_lat},{b_max_lon},{b_max_lat}",
            "locationReferencing": "shape",
            "incidentType": "SAFETY_CAMERA,SPEED_LIMIT_ENFORCEMENT",
            "lang": "tr",
        }
        try:
            from .config import http_client
            resp = await http_client.get(HERE_TRAFFIC_URL, params=params)
            return resp.json().get("results", []) if resp.status_code == 200 else []
        except:
            return []

    cameras_on_route = []

    try:
        results = await asyncio.gather(*[_fetch_bbox(b) for b in bboxes])
        all_incidents = [item for sublist in results for item in sublist]
        
        # Unique incidents by ID
        unique_incidents = {}
        for inc in all_incidents:
            inc_id = inc.get("id")
            if inc_id and inc_id not in unique_incidents:
                unique_incidents[inc_id] = inc
        
        for incident in unique_incidents.values():
            props = incident.get("incidentDetails", {})
            location = incident.get("location", {})
            i_type = props.get("type", "")

            cam_entry = {
                "type": _translate_incident_type(i_type),
                "raw_type": i_type,
                "description": props.get("description", {}).get("value", "Hız Kamerası"),
            }

            if "polyline" in location:
                try:
                    decoded = _decode_here_polyline(location["polyline"])
                    if decoded:
                        cam_entry["lat"], cam_entry["lon"] = decoded[0]
                except: pass
            elif "mapMatchedPoint" in location:
                cam_entry["lat"] = location["mapMatchedPoint"].get("lat")
                cam_entry["lon"] = location["mapMatchedPoint"].get("lng")

            road_info = props.get("roadAttributes", {})
            if road_info.get("speedLimit"):
                cam_entry["limit_kmh"] = road_info["speedLimit"]

            cameras_on_route.append(cam_entry)

    except Exception as e:
        logger.error(f"❌ [HERE Radar] Beklenmeyen hata: {e}")
        return {"cameras_on_route": [], "total_count": 0, "warning": "Radar verisi alınırken hata oluştu."}

    total = len(cameras_on_route)
    logger.info(f"🚨 [Radar] Rota üzerinde {total} kamera/tehlike tespit edildi.")

    return {
        "cameras_on_route": cameras_on_route,
        "total_count": total,
        "bounding_box": {"min_lat": round(min_lat, 5), "min_lon": round(min_lon, 5), "max_lat": round(max_lat, 5), "max_lon": round(max_lon, 5)},
        "data_source": "HERE Maps Traffic API v7",
        "warning": f"⚠️ Bu rotada {total} hız kamerası tespit edildi." if total > 0 else None,
        "summary": f"📷 {total} kamera/radar noktası bulundu." if total > 0 else "✅ Kayıtlı hız kamerası bulunmuyor."
    }


def _translate_incident_type(i_type: str) -> str:
    mapping = {
        "SAFETY_CAMERA": "Hız Kamerası",
        "SPEED_LIMIT_ENFORCEMENT": "Hız Denetim Noktası",
        "HAZARDOUS_CONDITION": "Tehlikeli Durum",
        "ACCIDENT": "Kaza",
    }
    return mapping.get(i_type, i_type)


def _decode_here_polyline(polyline_str: str) -> list:
    try:
        import flexpolyline
        return flexpolyline.decode(polyline_str)
    except:
        return []
