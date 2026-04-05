import httpx
from loguru import logger
from .config import settings
from .geometry import sample_route_points

# HERE Traffic API v7 - Safety Camera / Hazard endpoint
HERE_TRAFFIC_URL = "https://data.traffic.hereapi.com/v7/incidents"


async def get_radars_on_route_handler(route_polyline: str) -> dict:
    """
    HERE Maps Traffic API v7 kullanarak rota üzerindeki gerçek zamanlı hız kameraları
    ve güvenlik tehlikelerini (safety cameras, hazards) getirir.
    
    HERE API'den dönen 'SAFETY_CAMERA' ve 'SPEED_LIMIT' tipindeki
    trafik olaylarını filtreler ve rotaya göre sunar.
    
    Args:
        route_polyline: Rota encoded polyline (HERE Flex Polyline formatı).
    
    Returns:
        Rota üzerindeki kamera/radar noktaları ve özet uyarı.
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
    min_lat = min(lats) - 0.05  # ~5km tolerans
    max_lat = max(lats) + 0.05
    min_lon = min(lons) - 0.05
    max_lon = max(lons) + 0.05

    cameras_on_route = []

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # HERE Traffic API v7: bbox sorgusu için locationReferencing=shape zorunlu
            params = {
                "apiKey": settings.HERE_API_KEY,
                "in": f"bbox:{min_lon},{min_lat},{max_lon},{max_lat}",
                "locationReferencing": "shape",
                "incidentType": "SAFETY_CAMERA,SPEED_LIMIT_ENFORCEMENT",
                "lang": "tr",
            }

            logger.info(f"🚨 [HERE Traffic] BBox: {min_lat:.4f},{min_lon:.4f} -> {max_lat:.4f},{max_lon:.4f}")
            resp = await client.get(HERE_TRAFFIC_URL, params=params)

            if resp.status_code != 200:
                # Kamera endpoint'i başarısız, genel trafik olaylarını dene
                logger.warning(f"⚠️ [HERE Radar] Kamera endpoint {resp.status_code}. Genel incidents deneniyor...")
                params["incidentType"] = "ROAD_CLOSED,CONSTRUCTION,HAZARDOUS_CONDITION,ACCIDENT"
                resp = await client.get(HERE_TRAFFIC_URL, params=params)

            if resp.status_code != 200:
                logger.error(f"❌ [HERE Radar] API hatası: {resp.status_code} - {resp.text[:200]}")
                return {
                    "cameras_on_route": [],
                    "total_count": 0,
                    "note": f"HERE Traffic API erişilemedi (HTTP {resp.status_code}). Plan hesaplanırken dikkatli olun.",
                    "warning": "⚠️ Radar verisi şu an alınamadı. Yine de hız limitine dikkat edin!"
                }

            data = resp.json()
            incidents = data.get("results", [])
            logger.info(f"✅ [HERE Radar] HERE'den {len(incidents)} olay alındı.")

            for incident in incidents:
                props = incident.get("incidentDetails", {})
                location = incident.get("location", {})
                i_type = props.get("type", "")

                # Hız kamerası ve trafik uyarılarını al
                cam_entry = {
                    "type": _translate_incident_type(i_type),
                    "raw_type": i_type,
                    "description": props.get("description", {}).get("value", "Hız Kamerası"),
                    "start_time": props.get("startTime", ""),
                    "end_time": props.get("endTime", ""),
                }

                # Konum bilgisi
                if "polyline" in location:
                    # Polyline varsa başlangıç noktasını al
                    try:
                        decoded = _decode_here_polyline(location["polyline"])
                        if decoded:
                            cam_entry["lat"] = decoded[0][0]
                            cam_entry["lon"] = decoded[0][1]
                    except Exception:
                        pass
                elif "mapMatchedPoint" in location:
                    pt = location["mapMatchedPoint"]
                    cam_entry["lat"] = pt.get("lat")
                    cam_entry["lon"] = pt.get("lng")

                # Hız limiti bilgisi
                road_info = props.get("roadAttributes", {})
                if road_info.get("speedLimit"):
                    cam_entry["limit_kmh"] = road_info["speedLimit"]

                cameras_on_route.append(cam_entry)

    except httpx.TimeoutException:
        logger.error("❌ [HERE Radar] Zaman aşımı!")
        return {
            "cameras_on_route": [],
            "total_count": 0,
            "warning": "⚠️ HERE Radar verisi zaman aşımına uğradı. Dikkatli sürüş öneririz!"
        }
    except Exception as e:
        logger.error(f"❌ [HERE Radar] Beklenmeyen hata: {e}")
        return {
            "cameras_on_route": [],
            "total_count": 0,
            "note": str(e),
            "warning": "⚠️ Radar verisi alınırken hata oluştu."
        }

    total = len(cameras_on_route)
    logger.info(f"🚨 [Radar] Rota üzerinde {total} kamera/tehlike tespit edildi.")

    return {
        "cameras_on_route": cameras_on_route,
        "total_count": total,
        "bounding_box": {
            "min_lat": round(min_lat, 5), "min_lon": round(min_lon, 5),
            "max_lat": round(max_lat, 5), "max_lon": round(max_lon, 5)
        },
        "data_source": "HERE Maps Traffic API v7 (Gerçek Zamanlı)",
        "warning": (
            f"⚠️ Bu rotada {total} hız kamerası/trafik tehlikesi tespit edildi. "
            "Hız limitlerini aşmamaya ve dikkatli sürmeye özen gösterin!"
        ) if total > 0 else None,
        "summary": (
            f"📷 {total} kamera/radar noktası bulundu. Lütfen tüm hız limitlerine uyun."
            if total > 0 else
            "✅ Bu rotada kayıtlı aktif hız kamerası bulunmuyor. Yine de hız limitine dikkat edin."
        )
    }


def _translate_incident_type(i_type: str) -> str:
    """HERE olay tipini Türkçeye çevirir."""
    mapping = {
        "SAFETY_CAMERA": "Hız Kamerası",
        "SPEED_LIMIT_ENFORCEMENT": "Hız Denetim Noktası",
        "HAZARDOUS_CONDITION": "Tehlikeli Durum",
        "ROAD_CLOSED": "Yol Kapalı",
        "CONSTRUCTION": "Yol Çalışması",
        "ACCIDENT": "Kaza",
        "LANE_RESTRICTION": "Şerit Kısıtlaması",
        "MASS_TRANSIT": "Toplu Taşıma Etkisi",
    }
    return mapping.get(i_type, i_type)


def _decode_here_polyline(polyline_str: str) -> list:
    """HERE flexpolyline string'ini [(lat, lon)] listesine çözer."""
    try:
        import flexpolyline
        decoded = flexpolyline.decode(polyline_str)
        return [(pt[0], pt[1]) for pt in decoded]
    except Exception:
        return []
