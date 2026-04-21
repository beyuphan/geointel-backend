import os
import httpx
import json
import hashlib
import asyncio
from loguru import logger
from .geometry import get_distance_from_route, sample_route_points
from .cache import redis_store

# Ortam değişkenlerinden API anahtarını al
GOOGLE_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

async def search_places_google_handler(query: str, lat: float = None, lon: float = None, route_polyline: str = None) -> dict:
    """
    Google Places Text Search API üzerinden mekan araması ve rota filtrelemesi yapar.
    """
    if not GOOGLE_API_KEY:
        return {"error": "Sistem hatası: GOOGLE_MAPS_API_KEY tanımlanmamış."}

    # Rota modu aktif mi?
    should_calc_distance = bool(route_polyline and len(route_polyline) > 20)
    
    # 1. Cache kontrolü
    cache_key = f"google_search:{query}:{lat}:{lon}:{hashlib.md5((route_polyline or '').encode()).hexdigest()}"
    cached_result = redis_store.get(cache_key)
    if cached_result:
        try:
             logger.info(f"⚡ [Google API Cache] Önceden aranmış: {query}")
             return json.loads(cached_result)
        except:
             pass

    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    
    # 2. Rota üzerinden örnek lokasyonlar al
    locations_to_search = []
    points = []
    if should_calc_distance:
        points = sample_route_points(encoded_polyline=route_polyline, interval_km=40)
        if points:
            if len(points) <= 3:
                 locations_to_search = [(p['lat'], p['lon']) for p in points]
            else:
                 # Baş, Orta, Son al
                 locations_to_search = [
                     (points[0]['lat'], points[0]['lon']),
                     (points[len(points)//2]['lat'], points[len(points)//2]['lon']),
                     (points[-1]['lat'], points[-1]['lon'])
                 ]
    
    # Eğer LLM spesifik bir lat/lon hedefi verdiyse, rota olsa dahi O hedefi muhakkak aramaya ekle
    if lat and lon and lat != 0.0 and lon != 0.0:
        if (lat, lon) not in locations_to_search:
             locations_to_search.insert(0, (lat, lon))

    if not locations_to_search:
         locations_to_search = [(None, None)]

    async with httpx.AsyncClient() as client:
        try:
            logger.info(f"🔍 [Google API] Aranıyor: {query} (Lokasyon sayısı: {len(locations_to_search)})")
            
            all_raw_results = []
            seen_place_ids = set()
            
            # API İstekleri - PARALEL ÇALIŞTIR! (Timeout önlemek için)
            tasks = []
            for s_lat, s_lon in locations_to_search:
                params = {
                    "query": query,
                    "key": GOOGLE_API_KEY,
                    "language": "tr"
                }
                if s_lat and s_lon:
                    params["location"] = f"{s_lat},{s_lon}"
                    params["radius"] = "50000" # 50km tarama alanı
                    
                tasks.append(client.get(url, params=params, timeout=15.0))
            
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            
            for resp in responses:
                if isinstance(resp, Exception):
                    logger.warning(f"⚠️ [Google API] Bir istek zaman aşımına uğradı veya hata verdi: {resp}")
                    continue
                try:
                    data = resp.json()
                    if data.get("status") == "OK":
                        for place in data.get("results", []):
                            pid = place.get("place_id") or place.get("name")
                            if pid not in seen_place_ids:
                                seen_place_ids.add(pid)
                                all_raw_results.append(place)
                    elif data.get("status") in ["REQUEST_DENIED", "OVER_QUERY_LIMIT"]:
                        logger.error(f"❌ [Google API] Kritik Hata: API reddetti ({data.get('status')}): {data.get('error_message')}")
                        return {"error": f"Google API Hatası: {data.get('status')} - {data.get('error_message')}"}
                except Exception as e:
                    logger.warning(f"⚠️ [Google API] JSON Pars/İstek hatası: {e}")
                    continue
                     
            if not all_raw_results:
                 return {"strict_route_places": [], "relaxed_route_places": []}
                 
            on_route_list = []
            detour_list = []
            
            # Rota hız tahmini
            avg_speed_kmh = 80.0
            
            from datetime import datetime, timedelta
            now = datetime.now()
            
            for place in all_raw_results:
                geom = place.get("geometry", {}).get("location")
                if not geom: continue
                
                # Temel mekan objesi
                rating = place.get("rating", 0.0)
                review_count = place.get("user_ratings_total", 0)
                place_obj = {
                    "name": place.get("name"),
                    "address": place.get("formatted_address"),
                    "rating": rating,
                    "review_count": review_count,
                    "score": rating * review_count, # Yeni puanlama algoritması
                    "coords": f"{geom['lat']},{geom['lng']}",
                    "open_now": place.get("opening_hours", {}).get("open_now", "Bilinmiyor")
                }

                # Rota üzerindeyse mesafe ve ZAMAN (ETA) analizi yap
                if should_calc_distance:
                    deviation = get_distance_from_route(geom, route_polyline)
                    
                    if isinstance(deviation, (int, float)) and deviation < 900000:
                        place_obj["deviation_meters"] = int(deviation)
                        
                        # ZAMAN FARKINDALIĞI (Time-Awareness)
                        # Mekana en yakın rota noktasını bulup tahmini varış süresini (ETA) hesapla
                        if points:
                            # Hızlıca en yakın noktayı bul (öklid)
                            nearest_pt = min(points, key=lambda p: (p['lat']-geom['lat'])**2 + (p['lon']-geom['lng'])**2)
                            distance_to_pt_km = nearest_pt.get("km_point", 0)
                            
                            # Tahmini varış süresi
                            eta_hours = distance_to_pt_km / avg_speed_kmh
                            eta_time = now + timedelta(hours=eta_hours)
                            place_obj["eta"] = eta_time.strftime("%H:%M")
                            place_obj["distance_along_route_km"] = distance_to_pt_km
                            
                            # Eğer şu an açıksa AMA tahmini varış saatinde geç saat olacağından kapalı olma riski varsa 
                            # (Google API detaylı saat vermediği için basit heuristik: Gece 23:00 - 06:00 arası filtrele)
                            if place_obj["open_now"] is True: # Şu an açık
                                if eta_time.hour >= 23 or eta_time.hour < 6:
                                    # 24 Saat açık olması muhtemel mekanlar (Benzinlik, Otel, Hastane) için istisna
                                    query_lower = query.lower()
                                    name_lower = place_obj['name'].lower()
                                    exempt_keywords = ["benzin", "petrol", "akaryakıt", "istasyon", "opet", "shell", "bp", "hastane", "otel", "hotel", "havalimanı"]
                                    
                                    is_exempt = any(k in query_lower or k in name_lower for k in exempt_keywords)
                                    
                                    if not is_exempt:
                                        # Gece varılacak mekanlar muhtemelen kapalıdır, listeye alma.
                                        logger.info(f"Mekan ETA nedeniyle elendi (Gece varış): {place_obj['name']} | ETA: {place_obj['eta']}")
                                        continue
                        
                        # KRİTERLER: 400m (Yol üstü), 5000m (Kısa sapma)
                        if place_obj["deviation_meters"] <= 400:
                            on_route_list.append(place_obj)
                        elif place_obj["deviation_meters"] <= 5000:
                            detour_list.append(place_obj)
                else:
                    # Rota yoksa tüm sonuçları ana listeye ekle
                    on_route_list.append(place_obj)

            # Sonuçları puan * yorum sayısına göre sırala (En yüksek puan üstte)
            on_route_list.sort(key=lambda x: x.get('score', 0), reverse=True)
            detour_list.sort(key=lambda x: x.get('score', 0), reverse=True)

            # Rota pasifse (şehir araması) LLM'e daha bol veri (15) sun, rota aktifse 5 ile sınırla (token tasarrufu)
            limit = 5 if should_calc_distance else 15
            
            result = {
                "route_status": "active" if should_calc_distance else "inactive",
                "strict_route_places": on_route_list[:limit], 
                "relaxed_route_places": detour_list[:limit]   
            }
            
            # 3. Sonucu 12 saatliğine (43200 saniye) cache'le
            redis_store.set(cache_key, json.dumps(result, ensure_ascii=False), ex=43200)

            return result

        except Exception as e:
            logger.error(f"🔥 [Google Handler] Kritik Hata: {e}")
            return {"error": f"Google servisiyle iletişim kurulamadı: {str(e)}"}