import os
import httpx
import json
import flexpolyline
import math  # <--- EKLENDİ: Sonsuzluk kontrolü için şart
from loguru import logger
from shapely.geometry import Point, LineString
from shapely.ops import transform
import pyproj

GOOGLE_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

def get_distance_from_route(location, polyline_str):
    """
    Mekan ile rota arasındaki en kısa mesafeyi (metre cinsinden) döner.
    Hata durumunda veya sonsuzluk durumunda 999999 döner.
    """
    try:
        # Basit validasyonlar
        if not polyline_str or polyline_str == "LATEST": 
            return 0
        
        if len(polyline_str) < 5: # Çok kısa stringler decode hatası verebilir
            return 999999

        try:
            decoded = flexpolyline.decode(polyline_str)
        except Exception:
            import polyline
            try:
                decoded = polyline.decode(polyline_str)
            except:
                return 999999

        if not decoded:
            return 999999

        line_coords = [(lon, lat) for lat, lon in decoded]
        route_line = LineString(line_coords)
        place_point = Point(location["lng"], location["lat"])

        # Projeksiyon (Metre hesabı için)
        project = pyproj.Transformer.from_proj(
            pyproj.Proj('epsg:4326'), # WGS84
            pyproj.Proj('epsg:3857'), # Web Mercator
            always_xy=True
        ).transform

        route_line_m = transform(project, route_line)
        place_point_m = transform(project, place_point)

        distance = route_line_m.distance(place_point_m)

        # 🛡️ GÜVENLİK KONTROLÜ: Sonsuz veya Tanımsız değer kontrolü
        if math.isinf(distance) or math.isnan(distance):
            return 999999
            
        return distance

    except Exception as e:
        # Sadece beklenmedik kritik hataları logla
        logger.warning(f"⚠️ Mesafe ölçümü yapılamadı: {e}")
        return 999999 # Hata varsa çok uzak varsay
async def search_places_google_handler(query: str, lat: float = None, lon: float = None, route_polyline: str = None) -> dict:
    if not GOOGLE_API_KEY:
        return {"error": "GOOGLE_MAPS_API_KEY eksik."}

    should_calc_distance = False
    if route_polyline and len(route_polyline) > 20:
        should_calc_distance = True

    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    
    params = {
        "query": query,
        "key": GOOGLE_API_KEY,
        "language": "tr"
    }
    
    if lat and lon:
        params["location"] = f"{lat},{lon}"
        params["radius"] = "50000" # 50km (Rotayı kapsayacak kadar geniş tutalım)

    async with httpx.AsyncClient() as client:
        try:
            logger.info(f"🔍 [Google] Aranıyor: {query}")
            resp = await client.get(url, params=params)
            data = resp.json()

            if "results" not in data or not data["results"]:
                return {"error": "Mekan bulunamadı."}

            raw_results = data["results"]
            
            on_route_list = []
            detour_list = []
            
            for place in raw_results:
                loc = place["geometry"]["location"]
                rating = place.get("rating", 0)
                user_ratings_total = place.get("user_ratings_total", 0)
                
                place_obj = {
                    "name": place.get("name"),
                    "address": place.get("formatted_address"),
                    "rating": rating,
                    "review_count": user_ratings_total,
                    "coords": f"{loc['lat']},{loc['lng']}",
                    "open_now": place.get("opening_hours", {}).get("open_now", "Bilinmiyor")
                }

                if should_calc_distance:
                    deviation = get_distance_from_route(loc, route_polyline)
                    
                    if isinstance(deviation, (int, float)) and deviation < 900000:
                        place_obj["deviation_meters"] = int(deviation)
                        
                        # 400m rota üstü, 5km sapma
                        if place_obj["deviation_meters"] <= 400:
                            on_route_list.append(place_obj)
                        elif place_obj["deviation_meters"] <= 5000:
                            detour_list.append(place_obj)
                    else:
                         # Mesafe ölçülemediyse ama Google bulduysa, bunu "Uzak" listesine ekleyelim mi?
                         # Şimdilik eklemeyelim, sadece rotadakileri istiyoruz.
                         pass
                else:
                    on_route_list.append(place_obj)

            # Sıralama
            on_route_list.sort(key=lambda x: x['rating'], reverse=True)
            detour_list.sort(key=lambda x: x['rating'], reverse=True)

            return {
                "route_status": "active" if should_calc_distance else "inactive",
                "strict_route_places": on_route_list[:5],
                "relaxed_route_places": detour_list[:5]
            }

        except Exception as e:
            logger.error(f"🔥 Google Search Hatası: {e}")
            return {"error": str(e)}