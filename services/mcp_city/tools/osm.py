import httpx
from logger import log
from .config import settings
from .models import OSMRequest

async def search_infrastructure_osm_handler(lat: float, lon: float, category: str) -> list:
    """
    OpenStreetMap (Overpass API) kullanarak altyapı araması yapar.
    """
    try:
        # 1. Pydantic ile Validasyon (Gizli Koruma)
        # Gelen veri hatalıysa burada patlar ve except bloğuna düşer
        req = OSMRequest(lat=lat, lon=lon, category=category)

        # 2. Havalimanı ise çapı büyüt (Config'den değil mantıktan gelir)
        radius = 50000 if req.category == "airport" else req.radius
        
        log.info(f"🌍 [OSM] Taranıyor: {req.category} ({req.lat}, {req.lon}) - Çap: {radius}m")

        # 3. Config'den Tag Map'i çek
        tags = settings.OSM_TAG_MAP.get(req.category)
        if not tags:
            return [{"error": f"Bilinmeyen kategori: {req.category}"}]

        # 4. Overpass Sorgusu Hazırla
        filters = "".join([f'nwr[{t}](around:{radius},{req.lat},{req.lon});' for t in tags])
        query = f"""
        [out:json][timeout:25];
        (
          {filters}
        );
        out center 5;
        """

        # 5. İsteği At
        async with httpx.AsyncClient() as client:
            resp = await client.post(settings.OVERPASS_URL, data=query)
            
            if resp.status_code != 200:
                log.error(f"OSM Hatası: {resp.status_code}")
                return [{"error": f"OSM Sunucu Hatası: {resp.status_code}"}]
            
            # 6. Veriyi Parse Et
            places = []
            for el in resp.json().get("elements", []):
                tags = el.get("tags", {})
                name = tags.get("name") or tags.get("name:tr") or tags.get("name:en")
                if not name: continue
                
                places.append({
                    "isim": name,
                    "kategori": req.category,
                    "lat": el.get("lat") or el.get("center", {}).get("lat"),
                    "lon": el.get("lon") or el.get("center", {}).get("lon")
                })
            
            log.success(f"✅ [OSM] {len(places)} yer bulundu.")
            return places[:3]

    except Exception as e:
        log.error(f"OSM Handler Hatası: {e}")
        return [{"error": str(e)}]