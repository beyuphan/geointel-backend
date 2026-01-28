import httpx
import flexpolyline
from logger import log
from .config import settings
from .models import RouteRequest
# REDIS STORE'U ÇAĞIRIYORUZ
from .cache import redis_store

# YARDIMCI FONKSİYON: Koordinatın Adını Bul
async def get_location_name(lat, lon):
    try:
        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {"latlng": f"{lat},{lon}", "key": settings.GOOGLE_MAPS_API_KEY, "language": "tr"}
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params)
            data = resp.json()
            if data.get("results"):
                # "Araklı, Trabzon" gibi bir adres döndürür
                for comp in data["results"][0]["address_components"]:
                    if "administrative_area_level_2" in comp["types"]: # İlçe adı
                        return comp["long_name"]
                return data["results"][0]["formatted_address"]
    except:
        return "Bilinmeyen Konum"
    return "Bilinmeyen Konum"

async def get_route_data_handler(origin: str, destination: str) -> dict:
    """HERE Maps API ile rota hesaplar ve REDIS'E KAYDEDER."""
    try:
        req = RouteRequest(origin=origin, destination=destination)
        
        params = {
            "transportMode": "car",
            "origin": req.origin,
            "destination": req.destination,
            "return": "summary,polyline",
            "apiKey": settings.HERE_API_KEY
        }

        async with httpx.AsyncClient() as client:
            resp = await client.get(settings.HERE_ROUTING_URL, params=params)
            data = resp.json()
            
            if resp.status_code == 200 and data.get("routes"):
                section = data["routes"][0]["sections"][0]
                summary = section["summary"]
                encoded_polyline = section["polyline"]
                
                # --- REDIS KAYDI ---
                # Uzun stringi Redis'e atıyoruz, 1 saat saklasın
                redis_store.set_route(encoded_polyline)
                log.info("💾 Rota başarıyla REDIS'e önbelleklendi.")
                
                # Koordinatları çöz (Orta nokta hesabı için)
                decoded_coords = list(flexpolyline.decode(encoded_polyline))
                
                # Orta noktayı al
                mid_point = decoded_coords[len(decoded_coords) // 2]
                mid_point_name = await get_location_name(mid_point[0], mid_point[1])

                check_points = {
                    "baslangic": {"coords": decoded_coords[0], "ad": "Başlangıç"},
                    "orta_nokta": {"coords": mid_point, "ad": mid_point_name},
                    "bitis": {"coords": decoded_coords[-1], "ad": "Bitiş"}
                }

                return {
                    "mesafe_km": round(summary["length"] / 1000, 2),
                    "sure_dk": round(summary["duration"] / 60, 0),
                    "analiz_noktalari": check_points,
                    # LLM'e uzun stringi göndermiyoruz, "LATEST" diyoruz.
                    # Böylece LLM Google tool'unu çağırırken "LATEST" yazacak.
                    "polyline_encoded": "LATEST" 
                }
            
            return {"error": "Rota bulunamadı"}

    except Exception as e:
        log.error(f"Rota Hatası: {e}")
        return {"error": str(e)}