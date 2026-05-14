import os
import httpx
import json
import hashlib
import asyncio
from loguru import logger
from typing import Optional
from .geometry import (
    get_distance_from_route,
    sample_route_points,
    get_place_route_side,
    get_route_midpoint,
)
from .cache import redis_store

# Ortam değişkenlerinden API anahtarını al
GOOGLE_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

# Kategoriler bazında 24-saat açık kalma varsayımı
_ALWAYS_OPEN_KEYWORDS = [
    "benzin", "petrol", "akaryakıt", "istasyon", "opet", "shell", "bp", "total",
    "petrol ofisi", "hastane", "otel", "hotel", "havalimanı", "airport",
    "eczane", "pharmacy",
]


def _is_open_at_eta(opening_hours_raw: list, eta_hour: int, eta_weekday: int) -> bool:
    """
    Google Places weekday_text listesini ('Pazartesi: 09:00–22:00' gibi) parse ederek
    verilen saat ve gün için açık mı kapalı mı olduğunu döner.

    Bilinmeyen / parse edilemeyen durumlarda True döner (muhafazakâr: eleme yapma).

    Args:
        opening_hours_raw: Google'dan gelen weekday_text listesi (Türkçe veya İngilizce).
        eta_hour: Tahmini varış saati (0-23).
        eta_weekday: Python weekday (0=Pazartesi … 6=Pazar).

    Returns:
        True → açık / bilinmiyor  |  False → kesinlikle kapalı
    """
    if not opening_hours_raw:
        return True  # Bilgi yok → muhafazakâr: eleme

    # Google weekday_text sırası Pazartesi=0 … Pazar=6 (Python ile aynı)
    if eta_weekday < 0 or eta_weekday >= len(opening_hours_raw):
        return True

    day_text: str = opening_hours_raw[eta_weekday]

    # "Kapalı" / "Closed" kontrolü
    if any(kw in day_text for kw in ["Kapalı", "Closed", "closed", "kapalı"]):
        return False

    # "24 saat" veya "Open 24 hours" kontrolü
    if any(kw in day_text for kw in ["24 saat", "24 hours", "00:00–24:00", "00:00 – 24:00"]):
        return True

    # "HH:MM–HH:MM" formatını parse et
    import re
    pattern = r"(\d{1,2}):(\d{2})\s*[–\-]\s*(\d{1,2}):(\d{2})"
    match = re.search(pattern, day_text)
    if not match:
        return True  # Format anlaşılmıyor → eleme

    open_h, open_m, close_h, close_m = map(int, match.groups())
    open_total  = open_h  * 60 + open_m
    close_total = close_h * 60 + close_m
    eta_total   = eta_hour * 60

    # Gece yarısını aşan saatler (örn. 10:00–02:00)
    if close_total <= open_total:
        close_total += 24 * 60

    return open_total <= eta_total <= close_total


async def search_places_google_handler(
    query: str,
    lat: float = None,
    lon: float = None,
    route_polyline: str = None,
    fraction: float = 0.5,
) -> dict:
    """
    Google Places Text Search API üzerinden mekan araması ve rota filtrelemesi yapar.

    Feature 1 (Midpoint): `route_polyline` verildiğinde arama merkezi rota başlangıcı
    DEĞİL, `fraction` parametresine göre hesaplanan rota noktasıdır
    (varsayılan %50 = midpoint).

    Feature 2 (ETA Hours): ETA hesabına göre varış saatinde kapalı olacak mekanlar
    _is_open_at_eta() fonksiyonu ile backend seviyesinde elenir.
    """
    if not GOOGLE_API_KEY:
        return {"error": "Sistem hatası: GOOGLE_MAPS_API_KEY tanımlanmamış."}

    # Rota modu aktif mi?
    should_calc_distance = bool(route_polyline and len(route_polyline) > 20)

    # 1. Cache kontrolü
    cache_key = (
        f"google_search:{query}:{lat}:{lon}:{fraction}:"
        f"{hashlib.md5((route_polyline or '').encode()).hexdigest()}"
    )
    cached_result = redis_store.get(cache_key)
    if cached_result:
        try:
            logger.info(f"⚡ [Google API Cache] Önceden aranmış: {query}")
            return json.loads(cached_result)
        except Exception:
            pass

    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"

    # 2. Arama lokasyonlarını belirle — MIDPOINT ÖNCE (Feature 1)
    locations_to_search = []
    points = []

    if should_calc_distance:
        # Rota hedef noktasını birincil arama konumu yap
        mp = get_route_midpoint(encoded_polyline=route_polyline, fraction=fraction)
        if mp:
            locations_to_search.append((mp["lat"], mp["lon"]))
            logger.info(
                f"🎯 [Google Midpoint] fraction={fraction:.2f} → "
                f"({mp['lat']:.4f}, {mp['lon']:.4f}) @ +{mp['distance_from_start_km']} km"
            )
            # Daha fazla çeşitlilik için hedefin biraz gerisini ve ilerisini de ara
            mp_prev = get_route_midpoint(encoded_polyline=route_polyline, fraction=max(0.0, fraction - 0.03))
            if mp_prev: locations_to_search.append((mp_prev["lat"], mp_prev["lon"]))
            mp_next = get_route_midpoint(encoded_polyline=route_polyline, fraction=min(1.0, fraction + 0.03))
            if mp_next: locations_to_search.append((mp_next["lat"], mp_next["lon"]))

        # Yalnızca hedeflenen noktayı (ve etrafını) arayacağız
        points = sample_route_points(encoded_polyline=route_polyline, interval_km=40)


    # LLM'den gelen spesifik koordinat varsa en öne ekle
    if lat and lon and lat != 0.0 and lon != 0.0:
        if (lat, lon) not in locations_to_search:
            locations_to_search.insert(0, (lat, lon))

    if not locations_to_search:
        locations_to_search = [(None, None)]

    async with httpx.AsyncClient() as client:
        try:
            logger.info(f"🔍 [Google API] Aranıyor: {query} (Lokasyon sayısı: {len(locations_to_search)})")

            all_raw_results = []
            seen_place_ids = set()

            # API İstekleri — PARALEL ÇALIŞTIR!
            tasks = []
            for s_lat, s_lon in locations_to_search:
                params = {
                    "query": query,
                    "key": GOOGLE_API_KEY,
                    "language": "tr",
                }
                if s_lat and s_lon:
                    params["location"] = f"{s_lat},{s_lon}"
                    params["radius"] = "50000"  # 50 km tarama alanı

                tasks.append(client.get(url, params=params, timeout=15.0))

            responses = await asyncio.gather(*tasks, return_exceptions=True)

            for resp in responses:
                if isinstance(resp, Exception):
                    logger.warning(f"⚠️ [Google API] İstek hatası: {resp}")
                    continue
                try:
                    data = resp.json()
                    if data.get("status") == "OK":
                        for place in data.get("results", []):
                            pid = place.get("place_id") or place.get("name")
                            if pid not in seen_place_ids:
                                seen_place_ids.add(pid)
                                all_raw_results.append(place)
                    elif data.get("status") in ["REQUEST_DENIED", "OVER_QUERY_LIMIT"]:
                        logger.error(
                            f"❌ [Google API] Kritik Hata: {data.get('status')} — "
                            f"{data.get('error_message')}"
                        )
                        return {
                            "error": (
                                f"Google API Hatası: {data.get('status')} - "
                                f"{data.get('error_message')}"
                            )
                        }
                except Exception as e:
                    logger.warning(f"⚠️ [Google API] JSON/İstek hatası: {e}")
                    continue

            if not all_raw_results:
                return {"strict_route_places": [], "relaxed_route_places": []}

            on_route_list: list = []
            detour_list: list = []

            avg_speed_kmh = 80.0

            from datetime import datetime, timedelta, timezone
            # Türkiye saati (UTC+3)
            now = datetime.now(timezone.utc) + timedelta(hours=3)

            # 24-saat açık muafiyet kontrolü — query seviyesinde
            query_lower = query.lower()
            always_open_query = any(k in query_lower for k in _ALWAYS_OPEN_KEYWORDS)

            for place in all_raw_results:
                geom = place.get("geometry", {}).get("location")
                if not geom:
                    continue

                rating = place.get("rating", 0.0)
                review_count = place.get("user_ratings_total", 0)
                opening_hours_raw = place.get("opening_hours", {}).get("weekday_text", [])

                place_obj = {
                    "name": place.get("name"),
                    "address": place.get("formatted_address"),
                    "rating": rating,
                    "review_count": review_count,
                    "score": rating * review_count,
                    "coords": f"{geom['lat']},{geom['lng']}",
                    "open_now": place.get("opening_hours", {}).get("open_now", "Bilinmiyor"),
                    "opening_hours": opening_hours_raw,
                    "price_level": place.get("price_level"),
                    "phone": place.get("formatted_phone_number"),
                    "website": place.get("website"),
                }

                # --- Feature 2: ETA-tabanlı dinamik çalışma saatleri kontrolü ---
                if should_calc_distance and points:
                    deviation = get_distance_from_route(geom, route_polyline)

                    if isinstance(deviation, (int, float)) and deviation < 900000:
                        place_obj["deviation_meters"] = int(deviation)

                        # En yakın rota noktasını bul → ETA hesapla
                        nearest_pt = min(
                            points,
                            key=lambda p: (
                                (p["lat"] - geom["lat"]) ** 2
                                + (p["lon"] - geom["lng"]) ** 2
                            ),
                        )
                        distance_to_pt_km = nearest_pt.get("km_point", 0)
                        eta_hours = distance_to_pt_km / avg_speed_kmh
                        eta_time = now + timedelta(hours=eta_hours)

                        place_obj["eta"] = eta_time.strftime("%H:%M")
                        place_obj["distance_along_route_km"] = distance_to_pt_km
                        place_obj["eta_based_check"] = True

                        # Varış saatinde kapalı mı?
                        name_lower = (place_obj["name"] or "").lower()
                        always_open_name = any(k in name_lower for k in _ALWAYS_OPEN_KEYWORDS)

                        if not always_open_query and not always_open_name and opening_hours_raw:
                            is_open_at_arrival = _is_open_at_eta(
                                opening_hours_raw,
                                eta_hour=eta_time.hour,
                                eta_weekday=eta_time.weekday(),
                            )
                            if not is_open_at_arrival:
                                logger.info(
                                    f"🚫 [ETA Filter] '{place_obj['name']}' elendi — "
                                    f"ETA {eta_time.strftime('%H:%M %a')} saatinde KAPALI."
                                )
                                continue
                            place_obj["open_at_arrival"] = True
                        else:
                            place_obj["open_at_arrival"] = "Muaf (24h veya bilinmiyor)"

                        # Yol üstü (≤400 m) vs sapma (≤5 km)
                        if place_obj["deviation_meters"] <= 400:
                            place_obj["on_route_side"] = get_place_route_side(
                                geom["lat"], geom["lng"], route_polyline
                            )
                            on_route_list.append(place_obj)
                        elif place_obj["deviation_meters"] <= 5000:
                            place_obj["on_route_side"] = get_place_route_side(
                                geom["lat"], geom["lng"], route_polyline
                            )
                            detour_list.append(place_obj)
                else:
                    # Rota yoksa tüm sonuçları ana listeye ekle
                    on_route_list.append(place_obj)

            # Puan × yorum sayısına göre sırala
            on_route_list.sort(key=lambda x: x.get("score", 0), reverse=True)
            detour_list.sort(key=lambda x: x.get("score", 0), reverse=True)

            limit = 5 if should_calc_distance else 15

            result = {
                "route_status": "active" if should_calc_distance else "inactive",
                "strict_route_places": on_route_list[:limit],
                "relaxed_route_places": detour_list[:limit],
            }

            # 12 saatliğine cache'le
            redis_store.set(cache_key, json.dumps(result, ensure_ascii=False), ex=43200)
            return result

        except Exception as e:
            logger.error(f"🔥 [Google Handler] Kritik Hata: {e}")
            return {"error": f"Google servisiyle iletişim kurulamadı: {str(e)}"}