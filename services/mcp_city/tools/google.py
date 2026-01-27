import httpx
from logger import log
from .config import settings
from .models import GoogleSearchRequest

async def search_places_google_handler(query: str, lat: float = None, lon: float = None) -> list:
    """Google Maps Text Search kullanır."""
    try:
        # 1. Validasyon
        req = GoogleSearchRequest(query=query, lat=lat, lon=lon)
        
        log.info(f"🔍 [Google] Aranıyor: {req.query}")
        
        params = {
            "key": settings.GOOGLE_MAPS_API_KEY, 
            "language": "tr", 
            "query": req.query
        }
        
        # Eğer koordinat varsa, aramayı oraya odaklar (Bias)
        if req.lat and req.lon:
            params["location"] = f"{req.lat},{req.lon}"
            params["radius"] = "2000"

        async with httpx.AsyncClient() as client:
            resp = await client.get(settings.GOOGLE_PLACES_URL, params=params)
            data = resp.json()
            
            if not data.get("results"):
                return [{"error": "Mekan bulunamadı"}]

            results = []
            for item in data["results"][:3]: # Tasarruf: İlk 3 sonuç
                loc = item["geometry"]["location"]
                results.append({
                    "isim": item["name"],
                    "adres": item["formatted_address"],
                    "puan": item.get("rating", "Yok"),
                    "lat": loc["lat"],
                    "lon": loc["lng"]
                })
            return results

    except Exception as e:
        log.error(f"Google Handler Hatası: {e}")
        return [{"error": str(e)}]