import httpx
from logger import log
from .config import settings
from .models import OSMRequest
from .geometry import get_route_midpoint
from typing import Optional

async def search_infrastructure_osm_handler(
    lat: float,
    lon: float,
    category: Optional[str] = None,
    radius: int = 2000,
    route_polyline: Optional[str] = None,
    fraction: float = 0.5,
) -> list:
    """
    OpenStreetMap üzerinde optimize edilmiş dinamik arama.

    Eğer `route_polyline` verilmişse arama koordinatı olarak başlangıç noktası yerine
    rota üzerindeki `fraction` kesrindeki koordinat (varsayılan: midpoint) kullanılır.
    Bu şekilde "yolun ortasındaki" veya "belirli bir noktadaki" mekan aramaları
    başlangıç noktasına değil, hedefe daha yakın bir konuma odaklanır.

    Args:
        lat: Başlangıç noktası enlemi (yedek, polyline yoksa kullanılır).
        lon: Başlangıç noktası boylamı (yedek, polyline yoksa kullanılır).
        category: OSM amenity etiketi (örn. fuel, hospital, restaurant).
        radius: Metre cinsinden arama yarıçapı (varsayılan 2000m).
        route_polyline: HERE / flex-polyline formatında rota dizisi.
            Verilirse arama koordinatı rota üzerinden hesaplanır.
        fraction: Rota üzerindeki hedef nokta kesri (0.0=başlangıç, 0.5=orta, 1.0=son).
    """
    try:
        # --- MIDPOINT LOGIC ---
        search_lat, search_lon = lat, lon
        midpoint_info = ""

        if route_polyline and len(route_polyline) > 10:
            mp = get_route_midpoint(encoded_polyline=route_polyline, fraction=fraction)
            if mp:
                search_lat = mp["lat"]
                search_lon = mp["lon"]
                midpoint_info = f" [Midpoint @ {mp['distance_from_start_km']} km]"
                log.info(
                    f"🎯 [OSM Midpoint] fraction={fraction:.2f} → "
                    f"({search_lat:.5f}, {search_lon:.5f}){midpoint_info}"
                )

        # Guarantee category is a string before it hits Pydantic's strict checks
        actual_category = category if category else "commercial"

        # Pydantic validasyonu
        req = OSMRequest(lat=search_lat, lon=search_lon, category=actual_category, radius=radius)
        raw_tag = req.category.strip().lower()
        tag_map = {
            "restoran": "restaurant", "lokanta": "restaurant", "yemek": "restaurant", "food": "restaurant",
            "kafe": "cafe", "kahve": "cafe", "kahvaltı": "cafe",
            "döner": "fast_food", "büfe": "fast_food", "hamburger": "fast_food",
            "benzin": "fuel", "yakıt": "fuel", "istasyon": "fuel", "benzinlik": "fuel",
            "eczane": "pharmacy", "ilaç": "pharmacy",
            "avm": "mall", "market": "supermarket"
        }
        tag = tag_map.get(raw_tag, raw_tag)
        search_radius = 50000 if "airport" in tag else req.radius

        # --- OPTİMİZE OVERPASS SORGUSU ---
        query = f"""
        [out:json][timeout:8];
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

        async with httpx.AsyncClient(timeout=8.0) as client:
            last_error = None
            headers = {"Content-Type": "text/plain"}

            for url in settings.OVERPASS_URLS:
                try:
                    log.info(
                        f"🌍 [OSM] Deneniyor: {url} | "
                        f"{raw_tag} → {tag}{midpoint_info}"
                    )

                    resp = await client.post(url, content=query, headers=headers)

                    if resp.status_code == 200:
                        try:
                            data = resp.json()
                        except Exception:
                            log.warning(f"⚠️ [OSM] JSON Parse Hatası ({url})")
                            continue

                        elements = data.get("elements", [])

                        if not elements:
                            log.warning(f"⚠️ [OSM] Sonuç boş döndü ({url})")
                            break

                        places = []
                        for el in elements:
                            tags = el.get("tags", {})
                            name = (
                                tags.get("name")
                                or tags.get("name:tr")
                                or tags.get("name:en")
                            )

                            if not name:
                                continue

                            found_type = (
                                tags.get("amenity")
                                or tags.get("shop")
                                or tags.get("landuse")
                                or tag
                            )

                            places.append({
                                "isim": name,
                                "tur": found_type,
                                "lat": el.get("lat") or el.get("center", {}).get("lat"),
                                "lon": el.get("lon") or el.get("center", {}).get("lon"),
                            })

                        if places:
                            log.success(
                                f"✅ [OSM] Başarılı ({url}) — "
                                f"{len(places)} yer bulundu{midpoint_info}."
                            )
                            return places[:10]

                    elif resp.status_code == 429:
                        log.warning(f"⚠️ [OSM] Rate-limit (429) — {url} bizi banladı.")
                    elif resp.status_code == 504:
                        log.warning(f"⚠️ [OSM] Timeout (504) — {url} çok yavaş.")
                    else:
                        log.warning(f"⚠️ [OSM] HTTP {resp.status_code} — {url}")
                        last_error = f"HTTP {resp.status_code}"

                except Exception as e:
                    log.warning(f"⚠️ [OSM] Bağlantı Hatası ({url}): {e}")
                    last_error = str(e)

            return [{"warning": f"'{tag}' kategorisinde sonuç alınamadı. (Sunucular yoğun olabilir)"}]

    except Exception as e:
        log.error(f"🔥 [OSM] Kritik Hata: {str(e)}")
        return [{"error": f"OSM Sistem Hatası: {str(e)}"}]