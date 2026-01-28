import httpx
from logger import log
from .config import settings
from .models import GoogleSearchRequest
from .geometry import filter_places_by_polyline
# REDIS STORE'U ÇAĞIRIYORUZ
from .cache import redis_store

async def search_places_google_handler(query: str, lat: float = None, lon: float = None, route_polyline: str = None) -> list:
    """Google Maps Text Search + Redis Rota Filtresi."""
    try:
        req = GoogleSearchRequest(query=query, lat=lat, lon=lon, route_polyline=route_polyline)
        
        log.info(f"🔍 [Google] Aranıyor: {req.query}")
        
        # --- REDIS KONTROLÜ ---
        active_polyline = None
        
        # Durum 1: Parametre boşsa veya "LATEST" ise -> Redis'e bak
        if not req.route_polyline or req.route_polyline == "LATEST" or len(req.route_polyline) < 50:
            stored_route = redis_store.get_route()
            if stored_route:
                log.info("💾 Redis'teki (Cached) rota kullanılıyor.")
                active_polyline = stored_route
            else:
                log.warning("⚠️ Redis'te rota bulunamadı ve parametre olarak da gelmedi.")
        # Durum 2: LLM inat edip uzun string gönderdiyse (kısa rotalar için)
        else:
            active_polyline = req.route_polyline
        
        # ----------------------

        params = {"key": settings.GOOGLE_MAPS_API_KEY, "language": "tr", "query": req.query}
        
        if req.lat and req.lon:
            params["location"] = f"{req.lat},{req.lon}"
            # Eğer elimizde bir rota varsa çapı 5km yapalım ki "Sapma" seçenekleri de gelsin
            params["radius"] = "5000" if active_polyline else "2000"

        async with httpx.AsyncClient() as client:
            resp = await client.get(settings.GOOGLE_PLACES_URL, params=params)
            data = resp.json()
            
            if not data.get("results"):
                return [{"error": "Mekan bulunamadı"}]

            raw_results = []
            # İlk 15 sonucu alıyoruz, filtreleme sonrası azalacaklar
            for item in data["results"][:15]: 
                loc = item["geometry"]["location"]
                raw_results.append({
                    "isim": item["name"],
                    "adres": item["formatted_address"],
                    "puan": item.get("rating", "Yok"),
                    "lat": loc["lat"],
                    "lon": loc["lng"]
                })
            
            # --- CORRIDOR SEARCH ---
            if active_polyline:
                log.info("🐍 Rota filtresi uygulanıyor (Redis destekli)...")
                
                # filter_places_by_polyline artık buffer_meters parametresi almıyor, 
                # içerideki sabit değerleri kullanıyor.
                final_results = filter_places_by_polyline(raw_results, active_polyline)
                
                if not final_results:
                    return [{"uyari": "Rotanız üzerinde veya makul sapma mesafesinde mekan bulunamadı."}]
                
                # En iyi 5 tanesini dönelim (Kıyaslama için)
                return final_results[:5]
            
            return raw_results[:5]

    except Exception as e:
        log.error(f"Google Handler Hatası: {e}")
        return [{"error": str(e)}]