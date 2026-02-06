import httpx
import flexpolyline
from logger import log
from .config import settings
from .models import RouteRequest
from .cache import redis_store
# Hibrit Yapı İçin Yerel Modül
from .local_routing import is_in_samsun, get_local_route

# --- 1. KOORDİNAT ÇÖZÜCÜ (Google > OSM) ---
async def _resolve_coordinates(location: str) -> str | None:
    """
    Konum ismini koordinata çevirir.
    Bulamazsa 'Atakum' stringini değil, NONE döner. (Sistemin patlamaması için kritik nokta burası)
    """
    # 1. Eğer gelen veri zaten koordinatsa (Örn: "41.02,40.52") doğrudan döndür.
    if "," in location:
        parts = location.split(",")
        try:
            # Sadece sayı mı diye kontrol et (Validation)
            float(parts[0].strip())
            float(parts[1].strip())
            return location.replace(" ", "")
        except ValueError:
            pass # İçinde virgül var ama sayı değil (Örn: "Rize, Merkez"), devam et.

    # 2. A PLANI: GOOGLE MAPS (Daha Zeki)
    if settings.GOOGLE_MAPS_API_KEY:
        log.info(f"🌍 [Google] Konum çözümleniyor: {location}")
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

    # 3. B PLANI: OSM NOMINATIM (Yedek)
    log.info(f"🌍 [OSM] Konum çözümleniyor (Yedek): {location}")
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
                lat = data[0]["lat"]
                lon = data[0]["lon"]
                log.success(f"✅ [OSM] Bulundu: {location} -> {lat},{lon}")
                return f"{lat},{lon}"
    except Exception as e:
        log.error(f"OSM Geocoding Hatası: {e}")
    
    # 4. HİÇBİRİ BULAMAZSA
    # Eski kod burada 'return location' yapıyordu, bu da hataya sebep oluyordu.
    # Artık None dönüyoruz ki aşağıda kontrol edebilelim.
    log.warning(f"❌ Konum hiçbir serviste bulunamadı: {location}")
    return None

# --- 2. YARDIMCI: KONUM ADI BULMA (Reverse Geocoding) ---
async def get_location_name(lat, lon):
    try:
        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {"latlng": f"{lat},{lon}", "key": settings.GOOGLE_MAPS_API_KEY, "language": "tr"}
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params)
            data = resp.json()
            if data.get("results"):
                for comp in data["results"][0]["address_components"]:
                    if "administrative_area_level_2" in comp["types"]: 
                        return comp["long_name"]
                return data["results"][0]["formatted_address"]
    except:
        return "Bilinmeyen Konum"
    return "Bilinmeyen Konum"

# --- 3. ANA ROTA HANDLER (HİBRİT BEYİN) ---
async def get_route_data_handler(origin: str, destination: str) -> dict:
    """
    Samsun içindeyse -> Yerel DB (PostGIS)
    Dışındaysa -> HERE Maps API
    """
    try:
        # A. Koordinatları Çöz
        origin_coord = await _resolve_coordinates(origin)
        dest_coord = await _resolve_coordinates(destination)
        
        # --- KRİTİK GÜVENLİK KONTROLÜ ---
        # Eğer koordinat bulunamadıysa (None geldiyse) işlemi burada durdur.
        # Float çevirme hatasını engelleyen kısım burası.
        if not origin_coord:
            return {"error": f"Başlangıç konumu haritada bulunamadı: {origin}"}
        if not dest_coord:
            return {"error": f"Bitiş konumu haritada bulunamadı: {destination}"}

        # B. Float Dönüşümü (Artık güvenli çünkü None olmadığını biliyoruz)
        try:
            lat1, lon1 = map(float, origin_coord.split(","))
            lat2, lon2 = map(float, dest_coord.split(","))
        except ValueError:
             return {"error": "Koordinat formatı hatalı, işlem yapılamadı."}

        # C. SAMSUN KONTROLÜ (YEREL ROTA)
        if is_in_samsun(lat1, lon1) and is_in_samsun(lat2, lon2):
            log.info(f"🏙️ [SAMSUN OPS] Yerel Veritabanı Devrede: {origin} -> {destination}")
            
            local_rows = await get_local_route(lat1, lon1, lat2, lon2)
            
            if local_rows:
                return {
                    "source": "Samsun_Local_DB",
                    "mesafe_km": "Hesaplaniyor (PostGIS)", 
                    "sure_dk": "Hava Durumlu (PostGIS)",
                    "analiz_noktalari": {
                        "baslangic": {"coords": [lat1, lon1], "ad": origin},
                        "bitis": {"coords": [lat2, lon2], "ad": destination}
                    },
                    "polyline_encoded": "LOCAL_DB_ROUTE",
                    "not": "Bu rota Samsun yerel veritabanından, hava durumu faktörü eklenerek çekilmiştir."
                }
            else:
                log.warning("⚠️ Yerel rota hesaplanamadı, HERE API deneniyor.")

        # D. HERE MAPS API (FALLBACK / DIŞ HAT)
        log.info(f"🌍 [HERE API] Dış Hat Rotası Hesaplanıyor: {origin} -> {destination}")
        
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
                
                # Redis'e Kaydet
                redis_store.set_route(encoded_polyline)
                
                # Analiz Noktaları (Orta Nokta vb.)
                decoded_coords = list(flexpolyline.decode(encoded_polyline))
                mid_point = decoded_coords[len(decoded_coords) // 2]
                mid_point_name = await get_location_name(mid_point[0], mid_point[1])

                return {
                    "source": "HERE_Maps_API",
                    "mesafe_km": round(summary["length"] / 1000, 2),
                    "sure_dk": round(summary["duration"] / 60, 0),
                    "analiz_noktalari": {
                        "baslangic": {"coords": decoded_coords[0], "ad": origin},
                        "orta_nokta": {"coords": mid_point, "ad": mid_point_name},
                        "bitis": {"coords": decoded_coords[-1], "ad": destination}
                    },
                    "polyline_encoded": encoded_polyline, 
                }
            
            return {"error": "Rota bulunamadı (HERE API)"}

    except Exception as e:
        log.error(f"Genel Rota Hatası: {e}")
        return {"error": f"Sistem Hatası: {str(e)}"}