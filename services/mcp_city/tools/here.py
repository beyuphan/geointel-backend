import httpx
import flexpolyline

from datetime import datetime, timezone
from loguru import logger as log
from .config import settings, http_client
from .models import RouteRequest
from .cache import redis_store
from .local_routing import is_in_service_area, get_local_route
from .weather import get_weather_handler as _get_weather


from .db import get_saved_locations as _db_get_saved_locations

# Saved location resolver — artık orchestrator'a bağımlı değil
async def _resolve_from_db(location: str, session_id: str = "test_pilot") -> str | None:
    """Kayıtlı konumları (Ev, İş vb.) DB'den koordinata çevirir."""
    try:
        saved = await _db_get_saved_locations(username=session_id)
        key = location.lower().strip()
        if key in saved:
            log.info(f"🏠 [SavedLocation] '{location}' → {saved[key]}")
            return saved[key]
    except Exception as e:
        log.warning(f"⚠️ [SavedLocation] DB lookup başarısız: {e}")
    return None

# --- 1. KOORDİNAT ÇÖZÜCÜ ---
async def _resolve_coordinates(location: str, session_id: str = "test_pilot") -> str | None:
    # 0. CURRENT_LOCATION magic keyword → Redis'ten al
    if location.upper().strip() in ("CURRENT_LOCATION", "BENIM_KONUM", "ANLIK_KONUM"):
        loc = redis_store.client.get(f"loc:{session_id}") if redis_store.client else None
        if loc:
            log.info(f"📍 [CurrentLoc] Redis'ten konum alındı: {loc}")
            return loc
        log.warning("⚠️ [CurrentLoc] Anlık konum henüz bilinmiyor.")
        return None

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

    # 1.5. Kayıtlı konum kısayolu mu? (Ev, İş vb.)
    saved_coord = await _resolve_from_db(location, session_id)
    if saved_coord:
        return saved_coord

    # Mevcut konumu bias (ipucu) olarak kullanmak için Redis'ten al
    current_loc_bias = None
    if redis_store.client:
        try:
            loc_data = redis_store.client.get(f"loc:{session_id}")
            if loc_data:
                current_loc_bias = loc_data.decode("utf-8") if isinstance(loc_data, bytes) else loc_data
        except Exception:
            pass

    # 2. A PLANI: GOOGLE MAPS API (Geocoding)
    if settings.GOOGLE_MAPS_API_KEY:
        log.info(f"🌍 [Google Geocode] Deneniyor: {location}")
        try:
            url = "https://maps.googleapis.com/maps/api/geocode/json"
            params = {"address": location, "key": settings.GOOGLE_MAPS_API_KEY, "language": "tr", "region": "tr"}
            if current_loc_bias:
                params["location"] = current_loc_bias
                params["radius"] = "50000"  # 50km bias

            resp = await http_client.get(url, params=params, timeout=10.0)
            data = resp.json()
            if data.get("status") == "OK" and data.get("results"):
                loc = data["results"][0]["geometry"]["location"]
                log.success(f"✅ [Google Geocode] Bulundu: {location} -> {loc['lat']},{loc['lng']}")
                return f"{loc['lat']},{loc['lng']}"
        except Exception as e:
            log.error(f"Google Geocoding Hatası: {e}")

    # 2.5. A2 PLANI: GOOGLE PLACES (POI Search Fallback)
    if settings.GOOGLE_MAPS_API_KEY:
        log.info(f"🔍 [Google Places Search] POI olarak aranıyor: {location}")
        try:
            from .google import search_places_google_handler
            b_lat, b_lon = 0.0, 0.0
            if current_loc_bias and "," in current_loc_bias:
                b_lat, b_lon = map(float, current_loc_bias.split(","))

            places_res = await search_places_google_handler(location, lat=b_lat, lon=b_lon)
            all_found = places_res.get("strict_route_places", []) + places_res.get("relaxed_route_places", [])
            if all_found:
                coords = all_found[0].get("coords")
                if coords:
                    log.success(f"✅ [Google Places] Mekan bulundu: {all_found[0]['name']} -> {coords}")
                    return coords
        except Exception as e:
            log.error(f"Google Places Resolution Hatası: {e}")

    # 3. B PLANI: OSM NOMINATIM
    log.info(f"🌍 [OSM] Geocoding deneniyor (Yedek): {location}")
    try:
        url = "https://nominatim.openstreetmap.org/search"
        headers = {"User-Agent": "GeoIntel_City/3.0"}
        params = {
            "q": location,
            "format": "json",
            "limit": 10,
            "countrycodes": "tr",
            "addressdetails": "1",
            "featuretype": "settlement",
        }
        resp = await http_client.get(url, params=params, headers=headers, timeout=10.0)
        data = resp.json()
        if data:
            data.sort(key=lambda x: float(x.get("importance", 0)), reverse=True)

            best_match = data[0]
            for item in data:
                item_type = item.get("type", "")
                item_class = item.get("class", "")
                if item_class == "place" and item_type in ["city", "town", "municipality"]:
                    best_match = item
                    break

            lat, lon = best_match["lat"], best_match["lon"]

            addr = best_match.get("address", {})
            city_name = (
                addr.get("city")
                or addr.get("town")
                or addr.get("village")
                or best_match.get("display_name", location)
            )
            log.success(f"✅ [OSM] Bulundu: {city_name} → {lat},{lon} (importance: {best_match.get('importance', '?')})")
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
        resp = await http_client.get(url, params=params, timeout=5.0)
        data = resp.json()
        if data.get("results"):
            for comp in data["results"][0]["address_components"]:
                if "administrative_area_level_2" in comp["types"]:
                    return comp["long_name"]
            return data["results"][0]["formatted_address"]
    except Exception:
        pass
    return "Bilinmeyen Konum"

# --- 3. ANA ROTA HANDLER (HİBRİT YAPININ KALBİ) ---
async def get_route_data_handler(
    origin: str,
    destination: str,
    waypoints: list[str] | None = None,
    preference: str = "fastest",
    session_id: str = "test_pilot",
) -> dict:
    try:
        # A. Koordinat Çözümleme
        origin_coord = await _resolve_coordinates(origin, session_id)
        dest_coord = await _resolve_coordinates(destination, session_id)

        if not origin_coord:
            return {"error": f"Başlangıç konumu bulunamadı: {origin}"}
        if not dest_coord:
            return {"error": f"Bitiş konumu bulunamadı: {destination}"}

        # Waypoint çözümleme (Feature 4: multi-waypoint)
        via_coords = []
        if waypoints:
            for wp in waypoints:
                wc = await _resolve_coordinates(wp.strip(), session_id)
                if wc:
                    via_coords.append(wc)
                else:
                    log.warning(f"⚠️ [Waypoint] Çözümlenemedi, atlandı: {wp}")

        # Cache Kontrolü (waypointler de key'e dahil)
        import hashlib
        import json
        wp_key = "_".join(via_coords)
        cache_key = f"route_calc:{hashlib.md5(f'{origin_coord}_{dest_coord}_{wp_key}'.encode()).hexdigest()}"
        cached_route = redis_store.get(cache_key)
        if cached_route:
            try:
                log.info(f"⚡ [Route Cache] Önceden hesaplanmış rota bulundu: {origin} -> {destination}")
                return json.loads(cached_route)
            except Exception:
                pass

        try:
            lat1, lon1 = map(float, origin_coord.split(","))
            lat2, lon2 = map(float, dest_coord.split(","))
        except ValueError:
            return {"error": "Koordinat formatı hatalı."}

        # B. HİBRİT KARAR MEKANİZMASI: İSTANBUL MU?
        # Waypoint varsa HERE API kullan (yerel DB multi-waypoint desteklemiyor)
        if not via_coords and is_in_service_area(lat1, lon1) and is_in_service_area(lat2, lon2):
            log.info(f"🏙️ [GEOINTEL] Yerel Veritabanı Devrede: {origin} -> {destination}")

            # Hava durumu ile rain_factor hesapla
            rain_factor = 0.0
            try:
                mid_lat = (lat1 + lat2) / 2
                mid_lon = (lon1 + lon2) / 2
                weather = await _get_weather(mid_lat, mid_lon)
                current = weather.get("ANLIK_DURUM", {})
                condition_raw = current.get("durum", "").lower()
                if any(w in condition_raw for w in ["rain", "drizzle", "thunderstorm", "yağmur"]):
                    rain_factor = 0.8
                elif any(w in condition_raw for w in ["snow", "kar"]):
                    rain_factor = 1.0
                elif any(w in condition_raw for w in ["fog", "mist", "sis"]):
                    rain_factor = 0.4
                log.info(f"🌧️ [WeatherCost] rain_factor={rain_factor} ({condition_raw})")
            except Exception as we:
                log.warning(f"⚠️ [WeatherCost] Hava durumu alınamadı: {we}")

            # PostGIS Sorgusu
            local_result = await get_local_route(
                lat1, lon1, lat2, lon2, preference=preference, rain_factor=rain_factor
            )

            if local_result:
                encoded_poly = "LOCAL_ROUTE"
                try:
                    geom = local_result.get("geometry")
                    if geom and "coordinates" in geom:
                        raw_coords = geom["coordinates"]
                        flat_coords = []

                        if geom.get("type") == "MultiLineString":
                            for segment in raw_coords:
                                flat_coords.extend(segment)
                        else:
                            flat_coords = raw_coords

                        lat_lon_coords = [(float(c[1]), float(c[0])) for c in flat_coords]

                        if lat_lon_coords:
                            encoded_poly = flexpolyline.encode(lat_lon_coords)
                except Exception as e:
                    log.error(f"Polyline Encode Hatası: {e}")

                return {
                    "source": "GeoIntel_Local_DB",
                    "mesafe_km": local_result["distance_km"],
                    "sure_dk": local_result["duration_min"],
                    "mode": local_result["mode"],
                    "traffic_status": local_result.get("traffic_status"),
                    "traffic_color": local_result.get("traffic_color"),
                    "delay_min": local_result.get("delay_min"),
                    "avg_speed_kmh": local_result.get("avg_speed_kmh"),
                    "rain_factor_applied": rain_factor,
                    "polyline_encoded": encoded_poly,
                    "geometry": local_result["geometry"],
                    "analiz_noktalari": {
                        "baslangic": {"coords": [lat1, lon1], "ad": origin},
                        "bitis": {"coords": [lat2, lon2], "ad": destination},
                    },
                    "not": "IBB Live Traffic + OSM local routing.",
                }

        # C. FALLBACK: HERE MAPS API (şehirlerarası + multi-waypoint)
        log.info(f"🌍 [HERE API] Dış Hat Rotası: {origin} -> {destination} (via: {via_coords})")

        req = RouteRequest(origin=origin_coord, destination=dest_coord)
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {
            "transportMode": "car",
            "routingMode": "fast" if preference != "shortest" else "short",
            "origin": req.origin,
            "destination": req.destination,
            "return": "summary,polyline",
            "alternatives": 2,
            "departureTime": now_utc,
            "apiKey": settings.HERE_API_KEY,
        }

        if preference == "safest":
            params["avoid[features]"] = "unpavedRoads"
            params["routingMode"] = "fast"

        # httpx'te birden fazla aynı key (via) göndermek için list of tuples
        params_list = list(params.items())
        for via in via_coords:
            params_list.append(("via", via))

        resp = await http_client.get(settings.HERE_ROUTING_URL, params=params_list, timeout=15.0)
        if resp.status_code != 200:
            log.error(f"❌ [HERE API ERROR] Durum Kodu: {resp.status_code}")
            log.error(f"📄 [HERE API RESPONSE] Body: {resp.text}")
            return {"error": f"HERE API Hatası (HTTP {resp.status_code}): {resp.text[:100]}"}

        data = resp.json()

        if data.get("routes"):
            all_routes = []

            for idx, route_data in enumerate(data["routes"]):
                sections = route_data.get("sections", [])
                if not sections:
                    continue

                # ─── Feature 4: Multi-Leg Polyline Stitching ───────────────────────
                # HERE API, her via (waypoint) için ayrı bir section döner.
                # Tüm section polyline'larını tek bir kesintisiz polyline'a birleştir.
                total_length_m = 0
                total_duration_s = 0
                all_leg_coords: list = []
                legs_meta: list = []

                for sec_idx, section in enumerate(sections):
                    sec_summary = section.get("summary", {})
                    sec_poly = section.get("polyline", "")
                    leg_len = sec_summary.get("length", 0)
                    leg_dur = sec_summary.get("duration", 0)

                    total_length_m += leg_len
                    total_duration_s += leg_dur

                    if sec_poly:
                        try:
                            decoded = flexpolyline.decode(sec_poly)
                            # İlk leg dışındakilerin ilk noktasını atla (birleşim noktası tekrarı)
                            if all_leg_coords and decoded:
                                all_leg_coords.extend(decoded[1:])
                            else:
                                all_leg_coords.extend(decoded)
                        except Exception as decode_err:
                            log.warning(f"⚠️ [HERE] Section {sec_idx} decode hatası: {decode_err}")

                    legs_meta.append({
                        "leg_index": sec_idx,
                        "distance_km": round(leg_len / 1000, 2),
                        "duration_min": round(leg_dur / 60, 1),
                        "departure_coords": (
                            section.get("departure", {})
                                   .get("place", {})
                                   .get("originalLocation", {})
                        ),
                        "arrival_coords": (
                            section.get("arrival", {})
                                   .get("place", {})
                                   .get("originalLocation", {})
                        ),
                    })

                # Tüm leg koordinatlarını tek polyline'a encode et
                stitched_polyline = ""
                if all_leg_coords:
                    try:
                        stitched_polyline = flexpolyline.encode(all_leg_coords)
                    except Exception as enc_err:
                        log.warning(f"⚠️ [HERE] Polyline encode hatası: {enc_err}")
                        stitched_polyline = sections[0].get("polyline", "")

                if idx == 0:
                    try:
                        redis_store.set_route(stitched_polyline)
                    except Exception:
                        pass

                route_info = {
                    "isim": f"Rota {idx + 1}" if idx > 0 else "Ana Rota",
                    "mesafe_km": round(total_length_m / 1000, 2),
                    "sure_dk": round(total_duration_s / 60, 0),
                    "polyline_encoded": stitched_polyline,
                    "legs": legs_meta,           # Feature 4: per-leg detail
                    "leg_count": len(sections),
                }
                all_routes.append(route_info)
                log.info(
                    f"📍 [HERE] Rota {idx + 1}: "
                    f"{round(total_length_m/1000, 1)} km, "
                    f"{round(total_duration_s/60, 0)} dk, "
                    f"{len(sections)} leg"
                )

            if not all_routes:
                return {"error": "Rota bulunamadı (HERE API)"}

            primary_route = all_routes[0]

            # Waypoint isimlerini checkpoints'e ekle (Flutter marker'ları için)
            checkpoints = {
                "baslangic": {"coords": [lat1, lon1], "ad": origin},
                "bitis": {"coords": [lat2, lon2], "ad": destination},
            }
            for wp_idx, wp_coord in enumerate(via_coords):
                checkpoints[f"waypoint_{wp_idx + 1}"] = {
                    "coords": [float(c) for c in wp_coord.split(",")],
                    "ad": waypoints[wp_idx] if waypoints and wp_idx < len(waypoints) else wp_coord,
                }

            return {
                "source": "HERE_Maps_API",
                "mesafe_km": primary_route["mesafe_km"],
                "sure_dk": primary_route["sure_dk"],
                "polyline_encoded": primary_route["polyline_encoded"],
                "legs": primary_route.get("legs", []),       # Feature 4: per-leg data
                "leg_count": primary_route.get("leg_count", 1),
                "alternatif_rotalar": all_routes,
                "geometry": None,
                "analiz_noktalari": checkpoints,
            }

        return {"error": "Rota bulunamadı (HERE API)"}

    except Exception as e:
        log.error(f"Genel Rota Hatası: {e}")
        return {"error": f"Sistem Hatası: {str(e)}"}