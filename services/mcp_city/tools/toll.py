import httpx
from loguru import logger
from .config import settings, http_client
from .geometry import sample_route_points

HERE_ROUTING_URL = "https://router.hereapi.com/v8/routes"


async def get_toll_prices_handler(filter_region: str = None) -> str:
    """
    Köprü ve otoyol ücretlerine genel bilgi verir.
    Spesifik rota ücreti için get_toll_for_route_handler kullanılmalı.
    """
    note = (
        "🚗 **GEÇİŞ ÜCRETLERİ HAKKINDA**\n\n"
        "Geçiş ücretleri rotaya, araç tipine ve günün saatine göre değişmektedir.\n"
        "Kesin rota bazlı ücret hesabı için rotanı belirttikten sonra "
        "`get_toll_for_route` aracını kullan — HERE Maps'ten gerçek zamanlı veri alınır.\n\n"
        "**Ödeme Yöntemi:** HGS veya OGS kartıyla ödenir.\n"
        "**Araç Sınıfı:** Standart otomobil (Sınıf 1) baz alınmaktadır.\n"
    )
    return note


async def get_toll_for_route_handler(route_polyline: str) -> dict:
    """
    Verilen rota polyline'ını (HERE Flex Polyline formatı) çözerek
    HERE Maps Routing API v8'i kullanır ve rotanın başlangıç + bitiş noktaları
    arasındaki tahmini geçiş ücretini, köprü/tünel maliyetlerini döndürür.
    """
    if not route_polyline or len(route_polyline) < 20:
        return {"error": "Geçerli bir rota polyline'ı gerekli."}

    if not settings.HERE_API_KEY:
        return {"error": "HERE API anahtarı bulunamadı."}

    route_points = sample_route_points(encoded_polyline=route_polyline, interval_km=50)
    if not route_points or len(route_points) < 2:
        return {"error": "Rota noktaları çözümlenemedi."}

    origin_pt = route_points[0]
    dest_pt = route_points[-1]
    origin_str = f"{origin_pt['lat']},{origin_pt['lon']}"
    dest_str = f"{dest_pt['lat']},{dest_pt['lon']}"

    logger.info(f"💰 [HERE Toll] Rota geçiş ücreti sorgulanıyor: {origin_str} -> {dest_str}")

    try:
        params = {
            "transportMode": "car",
            "origin": origin_str,
            "destination": dest_str,
            "return": "summary,tolls",
            "tolls[summaryType]": "total",
            "tolls[vehicle][weight]": "1400",
            "tolls[vehicle][axleCount]": "2",
            "currency": "TRY",
            "apiKey": settings.HERE_API_KEY,
        }

        resp = await http_client.get(HERE_ROUTING_URL, params=params, timeout=15.0)

        if resp.status_code != 200:
            logger.error(f"❌ [HERE Toll] API hatası: {resp.status_code} - {resp.text[:300]}")
            return {
                "tolls_on_route": [],
                "total_toll_cost_tl": 0.0,
                "toll_count": 0,
                "summary": "⚠️ HERE Toll API şu an yanıt vermiyor.",
                "data_source": "HERE Maps Routing API v8",
                "error_detail": f"HTTP {resp.status_code}"
            }

        data = resp.json()
        routes = data.get("routes", [])

        if not routes:
            return {
                "tolls_on_route": [],
                "total_toll_cost_tl": 0.0,
                "toll_count": 0,
                "summary": "Bu rota için geçiş ücreti bulunamadı.",
                "data_source": "HERE Maps Routing API v8"
            }

        route = routes[0]
        all_sections = route.get("sections", [])
        
        tolls_on_route = []
        total_cost_try = 0.0

        for section in all_sections:
            section_tolls = section.get("tolls", [])
            for toll in section_tolls:
                toll_entry = _parse_here_toll(toll)
                if toll_entry:
                    tolls_on_route.append(toll_entry)
                    total_cost_try += toll_entry.get("price_tl", 0.0)

        # Route-level toll summary
        route_tolls = route.get("tolls", {})
        if isinstance(route_tolls, dict) and route_tolls and not tolls_on_route:
            total_fares = route_tolls.get("fares", [])
            for fare in total_fares:
                if not isinstance(fare, dict):
                    continue
                if fare.get("currency") == "TRY":
                    total_cost_try += float(fare.get("price", 0))
                elif isinstance(fare.get("convertedPrice"), dict):
                    total_cost_try += float(fare["convertedPrice"].get("price", 0))

        if tolls_on_route:
            summary = (
                f"Bu rotada {len(tolls_on_route)} ücretli geçiş noktası bulunuyor. "
                f"Tahmini toplam HGS/OGS ücreti: {round(total_cost_try, 2)} TL"
            )
        elif total_cost_try > 0:
            summary = f"Tahmini toplam geçiş ücreti: {round(total_cost_try, 2)} TL (HGS/OGS)"
        else:
            summary = "✅ Bu rotada ücretli geçiş noktası bulunmuyor veya HERE veri tabanında kayıtlı değil."

        logger.success(
            f"✅ [HERE Toll] {len(tolls_on_route)} geçiş tespit edildi. "
            f"Toplam: {round(total_cost_try, 2)} TL"
        )

        return {
            "tolls_on_route": tolls_on_route,
            "total_toll_cost_tl": round(total_cost_try, 2),
            "toll_count": len(tolls_on_route),
            "summary": summary,
            "data_source": "HERE Maps Routing API v8 (Gerçek Zamanlı)",
            "note": "Ücretler tahmini olup HERE Maps veri tabanından anlık alınmaktadır. HGS/OGS kartıyla ödenebilir."
        }

    except httpx.TimeoutException:
        logger.error("❌ [HERE Toll] Zaman aşımı!")
        return {
            "tolls_on_route": [], "total_toll_cost_tl": 0.0, "toll_count": 0,
            "summary": "⚠️ HERE Toll API zaman aşımına uğradı.",
            "data_source": "HERE Maps Routing API v8"
        }
    except Exception as e:
        logger.error(f"❌ [HERE Toll] Beklenmeyen hata: {e}")
        return {
            "tolls_on_route": [], "total_toll_cost_tl": 0.0, "toll_count": 0,
            "summary": "⚠️ Geçiş ücreti verisi alınamadı.",
            "error_detail": str(e),
            "data_source": "HERE Maps Routing API v8"
        }


def _parse_here_toll(toll: dict) -> dict | None:
    """HERE API'den gelen tek bir toll nesnesini parse eder. V3: String fare koruması."""
    if not isinstance(toll, dict):
        return None
    try:
        toll_system = toll.get("tollSystem")
        name = toll_system.get("name", "Ücretli Geçiş") if isinstance(toll_system, dict) else "Ücretli Geçiş"
        
        price_tl = 0.0
        fares = toll.get("fares", [])
        for fare in fares:
            # V3 FIX: HERE bazen fare objesini string olarak dönüyor → crash önleme
            if not isinstance(fare, dict):
                logger.warning(f"⚠️ Toll fare dict değil, atlandı: {type(fare)}")
                continue
            if fare.get("currency") == "TRY":
                price_tl = float(fare.get("price", 0))
                break
            converted = fare.get("convertedPrice")
            if isinstance(converted, dict) and converted.get("currency") == "TRY":
                price_tl = float(converted.get("price", 0))
                break
            elif not price_tl:
                price_tl = float(fare.get("price", 0))

        location = toll.get("location", {})
        
        entry = {
            "type": "Ücretli Geçiş",
            "name": name,
            "price_tl": round(price_tl, 2),
            "payment_methods": toll.get("paymentMethods", ["HGS", "OGS"]),
        }

        if isinstance(location, dict) and location.get("lat") and location.get("lng"):
            entry["lat"] = location["lat"]
            entry["lon"] = location["lng"]

        return entry
    except Exception as e:
        logger.warning(f"⚠️ Toll parse hatası: {e}")
        return None
