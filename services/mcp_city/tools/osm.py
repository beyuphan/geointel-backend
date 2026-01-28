# services/mcp_city/tools/osm.py
import httpx
from logger import log
from .config import settings
from .models import OSMRequest

async def search_infrastructure_osm_handler(lat: float, lon: float, category: str) -> list:
    try:
        req = OSMRequest(lat=lat, lon=lon, category=category)
        radius = 50000 if req.category == "airport" else req.radius
        tags = settings.OSM_TAG_MAP.get(req.category)
        
        filters = "".join([f'nwr[{t}](around:{radius},{req.lat},{req.lon});' for t in tags])
        query = f"[out:json][timeout:15];({filters});out center 5;" # Timeout'u kıstım, hızlı pes etsin

        # --- FALLBACK MEKANİZMASI ---
        async with httpx.AsyncClient() as client:
            last_error = None
            
            for url in settings.OVERPASS_URLS: # Listeyi geziyoruz
                try:
                    log.info(f"🌍 [OSM] Deneniyor: {url}")
                    resp = await client.post(url, data=query)
                    
                    if resp.status_code == 200:
                        # Başarılı oldu, veriyi işle ve döngüyü kır
                        places = []
                        for el in resp.json().get("elements", []):
                            tags = el.get("tags", {})
                            name = tags.get("name") or tags.get("name:tr")
                            if not name: continue
                            places.append({
                                "isim": name,
                                "kategori": req.category,
                                "lat": el.get("lat") or el.get("center", {}).get("lat"),
                                "lon": el.get("lon") or el.get("center", {}).get("lon")
                            })
                        log.success(f"✅ [OSM] Başarılı ({url}) - {len(places)} yer.")
                        return places[:5]
                    
                    else:
                        log.warning(f"⚠️ [OSM] Hata ({resp.status_code}) - Sıradakine geçiliyor...")
                        last_error = f"HTTP {resp.status_code}"

                except Exception as e:
                    log.warning(f"⚠️ [OSM] Bağlantı Hatası: {e} - Sıradakine geçiliyor...")
                    last_error = str(e)
            
            # Buraya geldiysek tüm URL'ler patlamıştır
            return [{"error": f"Tüm OSM sunucuları yanıt vermedi. Son hata: {last_error}"}]

    except Exception as e:
        return [{"error": str(e)}]