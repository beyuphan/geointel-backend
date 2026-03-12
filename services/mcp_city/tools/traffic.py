"""
HERE Traffic Flow API Handler
Rota üzerindeki canlı trafik yoğunluğunu çeker ve
kullanıcıya anlaşılır bir trafik uyarısı sunar.
"""
import httpx
from loguru import logger
from .config import settings
from .geometry import sample_route_points

HERE_TRAFFIC_FLOW_URL = "https://data.traffic.hereapi.com/v7/flow"


async def get_route_traffic_handler(route_polyline: str) -> dict:
    """
    HERE Traffic Flow API v7 kullanarak rota segmentlerindeki
    gerçek zamanlı trafik yoğunluğunu çeker.
    
    Args:
        route_polyline: HERE Flex Polyline formatında rota.
    
    Returns:
        Trafik yoğunluk seviyeleri, yavaşlamalar ve özet.
    """
    if not route_polyline or len(route_polyline) < 20:
        return {"error": "Geçerli bir rota polyline'ı gerekli."}

    if not settings.HERE_API_KEY:
        return {"error": "HERE API anahtarı bulunamadı."}

    route_points = sample_route_points(encoded_polyline=route_polyline, interval_km=30)
    if not route_points:
        return {"error": "Rota noktaları çözümlenemedi."}

    lats = [pt["lat"] for pt in route_points]
    lons = [pt["lon"] for pt in route_points]
    min_lat = min(lats) - 0.03
    max_lat = max(lats) + 0.03
    min_lon = min(lons) - 0.03
    max_lon = max(lons) + 0.03

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            params = {
                "apiKey": settings.HERE_API_KEY,
                "in": f"bbox:{min_lon},{min_lat},{max_lon},{max_lat}",
                "locationReferencing": "shape",
            }
            logger.info(f"🚦 [Traffic] HERE Flow API sorgulanıyor...")
            resp = await client.get(HERE_TRAFFIC_FLOW_URL, params=params)

            if resp.status_code != 200:
                logger.error(f"❌ [Traffic] API hatası: {resp.status_code}")
                return {
                    "traffic_segments": [],
                    "congestion_level": "unknown",
                    "summary": "⚠️ Trafik verisi alınamadı. Genel dikkat önerilir.",
                    "data_source": "HERE Traffic Flow API v7"
                }

            data = resp.json()
            results = data.get("results", [])
            logger.info(f"✅ [Traffic] {len(results)} trafik segmenti alındı.")

            congestion_counts = {"free": 0, "minor": 0, "slow": 0, "queuing": 0, "stationary": 0}
            segments = []

            for result in results[:20]:  # İlk 20 segment
                flow = result.get("currentFlow", {})
                speed = flow.get("speed", 0)
                free_speed = flow.get("freeFlow", speed or 1)
                jam_factor = flow.get("jamFactor", 0)

                # Trafik seviyesi hesapla
                if jam_factor < 2:
                    level = "free"
                    emoji = "🟢"
                elif jam_factor < 4:
                    level = "minor"
                    emoji = "🟡"
                elif jam_factor < 7:
                    level = "slow"
                    emoji = "🟠"
                elif jam_factor < 9:
                    level = "queuing"
                    emoji = "🔴"
                else:
                    level = "stationary"
                    emoji = "⛔"

                congestion_counts[level] = congestion_counts.get(level, 0) + 1

                segments.append({
                    "level": level,
                    "emoji": emoji,
                    "current_speed_kmh": round(speed, 1),
                    "free_flow_kmh": round(free_speed, 1),
                    "jam_factor": round(jam_factor, 1),
                })

            # Genel trafik özeti
            dominant = max(congestion_counts, key=congestion_counts.get)
            summary_map = {
                "free": "✅ Rota boyunca trafik akışı serbest, sorunsuz yolculuk bekleniyor.",
                "minor": "🟡 Bazı bölgelerde hafif trafik yoğunluğu var, küçük gecikmeler olabilir.",
                "slow": "🟠 Rota üzerinde belirgin yavaşlamalar var. Alternatif rotaları değerlendirin.",
                "queuing": "🔴 Ciddi trafik sıkışıklığı! Daha uzun süre hesaba katın.",
                "stationary": "⛔ Rota üzerinde trafik durmuş! Güzergahı değiştirmeyi düşünün.",
            }

            return {
                "traffic_segments": segments,
                "congestion_level": dominant,
                "congestion_distribution": congestion_counts,
                "summary": summary_map.get(dominant, "Trafik durumu belirsiz."),
                "data_source": "HERE Traffic Flow API v7 (Gerçek Zamanlı)",
            }

    except httpx.TimeoutException:
        return {
            "traffic_segments": [],
            "congestion_level": "unknown",
            "summary": "⚠️ Trafik verisi zaman aşımına uğradı.",
            "data_source": "HERE Traffic Flow API v7"
        }
    except Exception as e:
        logger.error(f"❌ [Traffic] Hata: {e}")
        return {
            "traffic_segments": [],
            "congestion_level": "unknown",
            "summary": f"⚠️ Trafik verisi alınamadı: {str(e)[:80]}",
            "data_source": "HERE Traffic Flow API v7"
        }
