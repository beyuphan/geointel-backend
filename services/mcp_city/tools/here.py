import httpx
import flexpolyline
from logger import log
from .config import settings
from .models import RouteRequest
# REDIS STORE'U ÇAĞIRIYORUZ
from .cache import redis_store

# --- YENİ EKLENEN: İSİMDEN KOORDİNAT BULUCU ---
async def _resolve_coordinates(location: str) -> str:
    """
    Konum ismini koordinata çevirir.
    Önce Google Maps Geocoding dener (Daha zeki),
    Patlarsa OSM Nominatim dener (Yedek).
    """
    # 1. Zaten koordinatsa dokunma
    if "," in location:
        parts = location.split(",")
        try:
            float(parts[0])
            float(parts[1])
            return location.replace(" ", "")
        except ValueError:
            pass 

    # 2. ÖNCE GOOGLE MAPS DENEYELİM (İnsan niyetini daha iyi anlar)
    if settings.GOOGLE_MAPS_API_KEY:
        log.info(f"🌍 [Google] Konum çözümleniyor: {location}")
        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {
            "address": location,
            "key": settings.GOOGLE_MAPS_API_KEY,
            "language": "tr",
            "region": "tr" # Türkiye sonuçlarını öncele
        }
        
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, params=params, timeout=10.0)
                data = resp.json()
                
                if data.get("status") == "OK" and data.get("results"):
                    loc = data["results"][0]["geometry"]["location"]
                    lat, lon = loc["lat"], loc["lng"]
                    log.success(f"✅ [Google] Bulundu: {location} -> {lat},{lon}")
                    return f"{lat},{lon}"
        except Exception as e:
            log.error(f"Google Geocoding Hatası: {e}")

    # 3. GOOGLE PATLARSA OSM NOMINATIM (YEDEK)
    log.info(f"🌍 [OSM] Konum çözümleniyor (Yedek): {location}")
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": location,
        "format": "json",
        "limit": 1,
        "countrycodes": "tr" 
    }
    headers = {"User-Agent": "GeoIntel_City/1.0"}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, headers=headers, timeout=10.0)
            data = resp.json()
            if data:
                lat = data[0]["lat"]
                lon = data[0]["lon"]
                log.success(f"✅ [OSM] Bulundu: {location} -> {lat},{lon}")
                return f"{lat},{lon}"
    except Exception as e:
        log.error(f"OSM Geocoding Hatası: {e}")
    
    # Hiçbiri bulamazsa orijinali dön
    return location

# YARDIMCI FONKSİYON: Koordinatın Adını Bul (Tersine Geocoding)
async def get_location_name(lat, lon):
    try:
        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {"latlng": f"{lat},{lon}", "key": settings.GOOGLE_MAPS_API_KEY, "language": "tr"}
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params)
            data = resp.json()
            if data.get("results"):
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
        # --- ÖNCE KOORDİNATLARI ÇÖZ ---
        origin_coord = await _resolve_coordinates(origin)
        dest_coord = await _resolve_coordinates(destination)
        
        # --- SONRA REQUEST MODELİNE VER ---
        # (Artık koordinat olduğu için validation hatası vermez)
        req = RouteRequest(origin=origin_coord, destination=dest_coord)
        
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
                    "polyline_encoded": "LATEST" 
                }
            
            return {"error": "Rota bulunamadı"}

    except Exception as e:
        log.error(f"Rota Hatası: {e}")
        return {"error": str(e)}