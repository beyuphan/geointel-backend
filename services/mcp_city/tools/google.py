import os
import httpx
import json
from loguru import logger
from .geometry import get_distance_from_route

# Ortam değişkenlerinden API anahtarını al
GOOGLE_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

async def search_places_google_handler(query: str, lat: float = None, lon: float = None, route_polyline: str = None) -> dict:
    """
    Google Places Text Search API üzerinden mekan araması ve rota filtrelemesi yapar.
    """
    if not GOOGLE_API_KEY:
        return {"error": "Sistem hatası: GOOGLE_MAPS_API_KEY tanımlanmamış."}

    # Rota modu aktif mi?
    should_calc_distance = bool(route_polyline and len(route_polyline) > 20)

    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {
        "query": query,
        "key": GOOGLE_API_KEY,
        "language": "tr"
    }
    
    # Lokasyon bazlı ağırlıklandırma (Bias)
    if lat and lon:
        params["location"] = f"{lat},{lon}"
        params["radius"] = "50000" # 50km tarama alanı

    async with httpx.AsyncClient() as client:
        try:
            logger.info(f"🔍 [Google API] Aranıyor: {query} (Rota Modu: {should_calc_distance})")
            resp = await client.get(url, params=params, timeout=15.0)
            data = resp.json()

            if data.get("status") != "OK":
                if data.get("status") == "ZERO_RESULTS":
                    return {"strict_route_places": [], "relaxed_route_places": []}
                return {"error": f"Google API Hatası: {data.get('status')}"}

            raw_results = data.get("results", [])
            on_route_list = []
            detour_list = []
            
            for place in raw_results:
                geom = place.get("geometry", {}).get("location")
                if not geom: continue
                
                # Temel mekan objesi
                place_obj = {
                    "name": place.get("name"),
                    "address": place.get("formatted_address"),
                    "rating": place.get("rating", 0.0),
                    "review_count": place.get("user_ratings_total", 0),
                    "coords": f"{geom['lat']},{geom['lng']}",
                    "open_now": place.get("opening_hours", {}).get("open_now", "Bilinmiyor")
                }

                # Rota üzerindeyse mesafe analizi yap
                if should_calc_distance:
                    deviation = get_distance_from_route(geom, route_polyline)
                    
                    if isinstance(deviation, (int, float)) and deviation < 900000:
                        place_obj["deviation_meters"] = int(deviation)
                        
                        # KRİTERLER: 400m (Yol üstü), 5000m (Kısa sapma)
                        if place_obj["deviation_meters"] <= 400:
                            on_route_list.append(place_obj)
                        elif place_obj["deviation_meters"] <= 5000:
                            detour_list.append(place_obj)
                else:
                    # Rota yoksa tüm sonuçları ana listeye ekle
                    on_route_list.append(place_obj)

            # Sonuçları puana göre sırala (En yüksek puan üstte)
            on_route_list.sort(key=lambda x: x.get('rating', 0), reverse=True)
            detour_list.sort(key=lambda x: x.get('rating', 0), reverse=True)

            return {
                "route_status": "active" if should_calc_distance else "inactive",
                "strict_route_places": on_route_list[:5], # En iyi 5 yol üstü
                "relaxed_route_places": detour_list[:5]   # En iyi 5 sapma
            }

        except Exception as e:
            logger.error(f"🔥 [Google Handler] Kritik Hata: {e}")
            return {"error": f"Google servisiyle iletişim kurulamadı: {str(e)}"}