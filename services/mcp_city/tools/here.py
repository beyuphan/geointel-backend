import httpx
import flexpolyline
from loguru import logger as log
from .config import settings
from .models import RouteRequest
from .cache import redis_store
from .local_routing import is_in_service_area, get_local_route

# --- 1. KOORDİNAT ÇÖZÜCÜ ---
async def _resolve_coordinates(location: str) -> str | None:
    # 1. Zaten koordinat formatında mı?
    if "," in location:
        parts = location.split(",")
        if len(parts) == 2:
            try:
                float(parts[0].strip())
                float(parts[1].strip())
                return location.replace(" ", "")
            except ValueError:
                pass 

    # 2. A PLANI: GOOGLE MAPS API
    if settings.GOOGLE_MAPS_API_KEY:
        log.info(f"🌍 [Google] Geocoding yapılıyor: {location}")
        try:
            async with httpx.AsyncClient() as client:
                url = "https://maps.googleapis.com/maps/api/geocode/json"
                params = {
                    "address": location,
                    "key": settings.GOOGLE_MAPS_API_KEY,
                    "language": "tr",
                    "region": "tr"
                }
                resp = await client.get(url, params=params, timeout=10.0)
                data = resp.json()
                
                if data.get("status") == "OK" and data.get("results"):
                    loc = data["results"][0]["geometry"]["location"]
                    lat, lon = loc["lat"], loc["lng"]
                    log.success(f"✅ [Google] Bulundu: {location} -> {lat},{lon}")
                    return f"{lat},{lon}"
        except Exception as e:
            log.error(f"Google Geocoding Hatası: {e}")

    # 3. B PLANI: OSM NOMINATIM
    log.info(f"🌍 [OSM] Geocoding deneniyor (Yedek): {location}")
    try:
        async with httpx.AsyncClient() as client:
            url = "https://nominatim.openstreetmap.org/search"
            headers = {"User-Agent": "GeoIntel_City/1.0"}
            params = {
                "q": location,
                "format": "json",
                "limit": 1,
                "countrycodes": "tr"
            }
            resp = await client.get(url, params=params, headers=headers, timeout=10.0)
            data = resp.json()
            if data:
                lat, lon = data[0]["lat"], data[0]["lon"]
                log.success(f"✅ [OSM] Bulundu: {location} -> {lat},{lon}")
                return f"{lat},{lon}"
    except Exception as e:
        log.error(f"OSM Geocoding Hatası: {e}")
    
    log.warning(f"❌ Konum hiçbir serviste bulunamadı: {location}")
    return None

# --- 2. YARDIMCI: KONUM ADI BULMA ---
async def get_location_name(lat, lon):
    if not settings.GOOGLE_MAPS_API_KEY:
        return f"{lat},{lon}"
    try:
        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {"latlng": f"{lat},{lon}", "key": settings.GOOGLE_MAPS_API_KEY, "language": "tr"}
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=5.0)
            data = resp.json()
            if data.get("results"):
                for comp in data["results"][0]["address_components"]:
                    if "administrative_area_level_2" in comp["types"]: 
                        return comp["long_name"]
                return data["results"][0]["formatted_address"]
    except:
        pass
    return "Bilinmeyen Konum"

# --- 3. ANA ROTA HANDLER (HİBRİT YAPININ KALBİ) ---
async def get_route_data_handler(origin: str, destination: str) -> dict:
    try:
        # A. Koordinat Çözümleme
        origin_coord = await _resolve_coordinates(origin)
        dest_coord = await _resolve_coordinates(destination)
        
        if not origin_coord: return {"error": f"Başlangıç konumu bulunamadı: {origin}"}
        if not dest_coord: return {"error": f"Bitiş konumu bulunamadı: {destination}"}

        try:
            lat1, lon1 = map(float, origin_coord.split(","))
            lat2, lon2 = map(float, dest_coord.split(","))
        except ValueError:
             return {"error": "Koordinat formatı hatalı."}

        # B. HİBRİT KARAR MEKANİZMASI: İSTANBUL MU?
        if is_in_service_area(lat1, lon1) and is_in_service_area(lat2, lon2):
            log.info(f"🏙️ [GEOINTEL] Yerel Veritabanı Devrede: {origin} -> {destination}")
            
            # PostGIS Sorgusu
            local_result = await get_local_route(lat1, lon1, lat2, lon2, preference="fastest")
            
            if local_result:
                # 🔥🔥🔥 FİNAL DÜZELTME: MULTILINESTRING DESTEĞİ 🔥🔥🔥
                encoded_poly = "LOCAL_ROUTE" # Varsayılan değer
                
                try:
                    geom = local_result.get("geometry")
                    if geom and "coordinates" in geom:
                        raw_coords = geom["coordinates"]
                        flat_coords = []

                        # Durum 1: MultiLineString (İç içe liste gelir: [[[lon, lat],..], [[lon, lat],..]])
                        if geom.get("type") == "MultiLineString":
                            for segment in raw_coords:
                                flat_coords.extend(segment) # Hepsini tek çizgiye indir
                        
                        # Durum 2: LineString (Düz liste gelir: [[lon, lat], [lon, lat]])
                        else:
                            flat_coords = raw_coords

                        # GeoJSON [Lon, Lat] verir -> Polyline [Lat, Lon] ister
                        # Ayrıca her ihtimale karşı float'a çeviriyoruz
                        lat_lon_coords = [(float(c[1]), float(c[0])) for c in flat_coords]
                        
                        # Artık encode edebiliriz
                        if lat_lon_coords:
                            encoded_poly = flexpolyline.encode(lat_lon_coords)
                            
                except Exception as e:
                    log.error(f"Polyline Encode Hatası: {e}")
                    # Hata olsa bile kod patlamasın, rota bilgisini döndürsün

                return {
                    "source": "GeoIntel_Local_DB",
                    "mesafe_km": local_result["distance_km"], 
                    "sure_dk": local_result["duration_min"],
                    "mode": local_result["mode"],
                    
                    # ARTIK ŞİFRELENMİŞ STRING BURAYA GİDİYOR 👇
                    "polyline_encoded": encoded_poly, 
                    
                    "geometry": local_result["geometry"], 
                    "analiz_noktalari": {
                        "baslangic": {"coords": [lat1, lon1], "ad": origin},
                        "bitis": {"coords": [lat2, lon2], "ad": destination}
                    },
                    "not": "Bu veri İBB Canlı Trafik ve OSM verileriyle yerel sunucuda hesaplanmıştır."
                }

        # C. FALLBACK: HERE MAPS API
        log.info(f"🌍 [HERE API] Dış Hat Rotası: {origin} -> {destination}")
        
        req = RouteRequest(origin=origin_coord, destination=dest_coord)
        params = {
            "transportMode": "car",
            "origin": req.origin,
            "destination": req.destination,
            "return": "summary,polyline",
            "apiKey": settings.HERE_API_KEY
        }

        async with httpx.AsyncClient() as client:
            resp = await client.get(settings.HERE_ROUTING_URL, params=params, timeout=15.0)
            data = resp.json()
            
            if resp.status_code == 200 and data.get("routes"):
                section = data["routes"][0]["sections"][0]
                summary = section["summary"]
                encoded_polyline = section["polyline"]
                
                # Redis Cache
                try:
                    redis_store.set_route(encoded_polyline)
                except: pass
                
                return {
                    "source": "HERE_Maps_API",
                    "mesafe_km": round(summary["length"] / 1000, 2),
                    "sure_dk": round(summary["duration"] / 60, 0),
                    "polyline_encoded": encoded_polyline, 
                    "geometry": None, 
                    "analiz_noktalari": {
                        "baslangic": {"coords": [lat1, lon1], "ad": origin},
                        "bitis": {"coords": [lat2, lon2], "ad": destination}
                    }
                }
            
            return {"error": "Rota bulunamadı (HERE API)"}

    except Exception as e:
        log.error(f"Genel Rota Hatası: {e}")
        return {"error": f"Sistem Hatası: {str(e)}"}