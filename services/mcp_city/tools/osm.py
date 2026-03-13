import httpx
from logger import log
from .config import settings
from .models import OSMRequest
from typing import Optional

async def search_infrastructure_osm_handler(lat: float, lon: float, category: Optional[str] = None, radius: int = 2000) -> list:
    """
    OpenStreetMap üzerinde optimize edilmiş dinamik arama.
    """
    try:
        # Guarantee category is a string before it hits Pydantic's strict checks
        actual_category = category if category else "commercial"
        
        # Pydantic validasyonu
        req = OSMRequest(lat=lat, lon=lon, category=actual_category, radius=radius)
        
        tag = req.category.strip().lower()
        search_radius = 50000 if "airport" in tag else req.radius

        # --- OPTİMİZE SORGUSU ---
        # Timeout süresini 45 saniyeye çıkardık.
        # Çok ağır olmaması için en kritik katmanları bıraktık.
        query = f"""
        [out:json][timeout:45];
        (
          nwr["amenity"="{tag}"](around:{search_radius},{req.lat},{req.lon});
          nwr["shop"="{tag}"](around:{search_radius},{req.lat},{req.lon});
          nwr["leisure"="{tag}"](around:{search_radius},{req.lat},{req.lon});
          nwr["landuse"="{tag}"](around:{search_radius},{req.lat},{req.lon});
          nwr["tourism"="{tag}"](around:{search_radius},{req.lat},{req.lon});
          nwr["building"="{tag}"](around:{search_radius},{req.lat},{req.lon});
        );
        out center 10;
        """

        async with httpx.AsyncClient(timeout=60.0) as client: # Client timeout sunucudan uzun olmalı
            last_error = None
            
            # Header ekleyelim ki bot sanıp engellemesinler
            headers = {"Content-Type": "text/plain"}

            for url in settings.OVERPASS_URLS:
                try:
                    log.info(f"🌍 [OSM] Deneniyor: {url} | Tag: {tag}")
                    
                    # --- FIX: data= yerine content= kullanıyoruz (Deprecation Fix) ---
                    resp = await client.post(url, content=query, headers=headers)
                    
                    if resp.status_code == 200:
                        try:
                            data = resp.json()
                        except:
                            log.warning(f"⚠️ [OSM] JSON Parse Hatası ({url})")
                            continue

                        elements = data.get("elements", [])
                        
                        if not elements:
                            log.warning(f"⚠️ [OSM] Sonuç boş döndü ({url}) - Diğerleri deneniyor...")
                            continue 
                        
                        places = []
                        for el in elements:
                            tags = el.get("tags", {})
                            name = tags.get("name") or tags.get("name:tr") or tags.get("name:en")
                            
                            if not name: continue
                            
                            found_type = tags.get("amenity") or tags.get("shop") or tags.get("landuse") or tag

                            places.append({
                                "isim": name,
                                "tur": found_type,
                                "lat": el.get("lat") or el.get("center", {}).get("lat"),
                                "lon": el.get("lon") or el.get("center", {}).get("lon")
                            })
                        
                        if places:
                            log.success(f"✅ [OSM] Başarılı ({url}) - {len(places)} yer bulundu.")
                            return places[:10]
                        
                    elif resp.status_code == 429:
                        log.warning(f"⚠️ [OSM] Çok Fazla İstek (429) - {url} bizi banladı, geçiyoruz.")
                    elif resp.status_code == 504:
                        log.warning(f"⚠️ [OSM] Sunucu Zaman Aşımı (504) - {url} çok yavaş.")
                    else:
                        log.warning(f"⚠️ [OSM] HTTP Hata ({resp.status_code}) - {url}")
                        last_error = f"HTTP {resp.status_code}"

                except Exception as e:
                    log.warning(f"⚠️ [OSM] Bağlantı Hatası ({url}): {e}")
                    last_error = str(e)
            
            return [{"warning": f"Aradığın kriterde ('{tag}') sonuç alınamadı. (Sunucular yoğun olabilir)"}]

    except Exception as e:
        log.error(f"🔥 [OSM] Kritik Hata: {str(e)}")
        return [{"error": f"OSM Sistem Hatası: {str(e)}"}]