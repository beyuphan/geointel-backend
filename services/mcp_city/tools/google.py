import os
import httpx
import json
import polyline
from loguru import logger
from shapely.geometry import Point, LineString
from shapely.ops import transform
import pyproj

GOOGLE_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

def get_distance_from_route(location, polyline_str):
    """
    Mekan ile rota arasındaki en kısa mesafeyi (metre cinsinden) döner.
    """
    try:
        if not polyline_str or polyline_str == "LATEST": return 0 # Rota yoksa mesafe 0 varsay
        
        decoded = polyline.decode(polyline_str)
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

        return route_line_m.distance(place_point_m)
    except Exception as e:
        logger.error(f"⚠️ Mesafe ölçüm hatası: {e}")
        return 999999 # Hata varsa çok uzak varsay

async def search_places_google_handler(query: str, lat: float = None, lon: float = None, route_polyline: str = None) -> dict:
    # ⚠️ DİKKAT: Dönüş tipini 'list' değil 'dict' yaptım!
    
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
        params["radius"] = "10000" # 10km çapında her şeyi getir, biz süzeceğiz

    async with httpx.AsyncClient() as client:
        try:
            logger.info(f"🔍 [Google] Aranıyor: {query}")
            resp = await client.get(url, params=params)
            data = resp.json()

            if "results" not in data or not data["results"]:
                return {"error": "Mekan bulunamadı."}

            raw_results = data["results"]
            
            # --- 🚦 AYRIŞTIRMA MANTIĞI ---
            on_route_list = [] # Tam yol üstü (Max 300m sapma)
            detour_list = []   # Biraz sapmalı (Max 3km sapma)
            
            for place in raw_results:
                loc = place["geometry"]["location"]
                rating = place.get("rating", 0)
                user_ratings_total = place.get("user_ratings_total", 0)
                
                # Mekan Objesi
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
                    place_obj["deviation_meters"] = int(deviation)
                    
                    if deviation <= 400: # 400 metreye kadar "Yol Üstü" sayalım
                        on_route_list.append(place_obj)
                    elif deviation <= 5000: # 5km'ye kadar "Uzatmalı" sayalım
                        detour_list.append(place_obj)
                else:
                    # Rota yoksa hepsi yol üstü sayılır (Merkez araması)
                    on_route_list.append(place_obj)

            # --- 📊 SIRALAMA (PUANA GÖRE) ---
            # İkisini de puana göre tersten sırala (En yüksek puan en başa)
            on_route_list.sort(key=lambda x: x['rating'], reverse=True)
            detour_list.sort(key=lambda x: x['rating'], reverse=True)

            return {
                "route_status": "active" if should_calc_distance else "inactive",
                "strict_route_places": on_route_list[:5], # En iyi 5
                "relaxed_route_places": detour_list[:5]     # En iyi 5
            }

        except Exception as e:
            logger.error(f"🔥 Google Search Hatası: {e}")
            return {"error": str(e)}