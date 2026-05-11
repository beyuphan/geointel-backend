import asyncio
import json
from loguru import logger as log
from typing import Dict, Any, List, Optional

class RouteStrategyEvaluator:
    """
    Kullanıcının rotasını hesaplayıp, o rota üzerindeki şehirlerde yakıt fiyatlarını
    sorgulayan ve fiyat/performans (eta/sapma) analizi yapan Macro-Tool yöneticisi.
    Bu sınıf Orchestrator'da barındırılır çünkü hem 'mcp_city' hem 'mcp_intel' ile konuşur.
    """
    
    def __init__(self, mcp_client):
        self.mcp = mcp_client

    async def _safe_call(self, service: str, tool_name: str, args: dict) -> dict:
        """MCP RPC call with error handling. Uses orchestrator's _process_mcp_result."""
        try:
            res = await self.mcp.mcp_rpc_call(service, "tools/call", {"name": tool_name, "arguments": args})
            if isinstance(res, dict):
                return res
            return {"status": "error", "message": f"Unexpected response type: {type(res)}"}
        except Exception as e:
            msg = f"API Error in {tool_name}: {str(e)}"
            log.error(msg)
            return {"status": "error", "message": msg}

    async def evaluate(self, origin: str, destination: str, fuel_type: str = "benzin", fuel_range: Optional[float] = None) -> Dict[str, Any]:
        """Ana Macro-Tool metodu. Rota çıkarır, şehirleri bulur, fiyatı basar."""
        log.info(f"🧠 [Macro-Tool] Rota Stratejisi Başlatıldı: {origin} -> {destination}")
        
        # 1. Rotayı Çiz
        route_res = await self._safe_call("city", "get_route_data", {"origin": origin, "destination": destination})
        if "error" in route_res or route_res.get("status") == "error":
             return {"status": "error", "message": f"Rota oluşturulamadı: {route_res.get('message', route_res.get('error', 'Bilinmeyen hata'))}"}

        polyline = route_res.get("polyline") or route_res.get("polyline_encoded", "")
        if not polyline or "GİZLENDİ" in str(polyline) or "HARİTA" in str(polyline):
             return {"status": "error", "message": "Geçerli bir polyline bulunamadı."}

        safe_route_summary = {
            "distance_km": route_res.get("mesafe_km") or route_res.get("distance_km"),
            "duration_min": route_res.get("sure_dk") or route_res.get("duration_min"),
            "polyline": polyline,
            "status": "success"
        }

        # 2. Rota üstü yakıt istasyonlarını bul
        places_res = await self._safe_call("city", "search_hybrid_places", {
            "query": f"{fuel_type} istasyonu",
            "route_polyline": polyline
        })
        
        if "error" in places_res or places_res.get("status") == "error":
            return {"status": "partial_success", "route": safe_route_summary, "warning": "Akaryakıt istasyonları bulunamadı."}

        # V2: Support both old format (strict/relaxed) and new format ({places: [...]})
        places = []
        if isinstance(places_res, list):
            places = places_res
        elif isinstance(places_res, dict):
            places = places_res.get("places", [])
            if not places:
                # Fallback to old format
                places = places_res.get("strict_route_places", []) + places_res.get("relaxed_route_places", [])
        
        if fuel_range:
            total_dist_raw = route_res.get("mesafe_km", 0) or route_res.get("distance_km", 0)
            try:
                total_dist = float(total_dist_raw)
            except:
                total_dist = 500.0

            target_distances = []
            curr = fuel_range * 0.8
            while curr < total_dist:
                target_distances.append(curr)
                curr += fuel_range * 0.8
            
            selected_places = []
            for target in target_distances:
                best_match = None
                best_diff = 9999.0
                for p in places:
                    d = p.get("distance_along_route_km", 0)
                    diff = abs(target - d)
                    if diff < best_diff and diff < (fuel_range * 0.3):
                        best_match = p
                        best_diff = diff
                if best_match and best_match not in selected_places:
                    selected_places.append(best_match)
            
            if selected_places:
                places = selected_places
            else:
                filtered_places = [p for p in places if p.get("distance_along_route_km", 0) <= fuel_range]
                if filtered_places:
                    places = filtered_places
                    places.sort(key=lambda p: abs(fuel_range * 0.8 - p.get("distance_along_route_km", 0)))

        if not places:
             return {
                 "status": "success", 
                 "route": safe_route_summary,
                 "analysis": "Rota üzerinde uygun istasyon bulunamadı."
             }

        # 3. Rota üzerindeki şehirleri ve fiyatları karşılaştır
        city_prices = {}
        processed_cities = set()
        
        best_station = places[0]
        best_address_parts = str(best_station.get("address", "")).split(",")
        predicted_city = best_address_parts[-1].strip().split(" ")[-1] if len(best_address_parts) > 0 else "Bilinmiyor"
        
        # En iyi 8 istasyonu incele (farklı şehirleri yakalamak için)
        for station in places[:8]:
            address = str(station.get("address", ""))
            # Basit şehir tahmini (virgülden sonraki son parça genellikle şehir/ilçe)
            parts = address.split(",")
            if len(parts) >= 2:
                city = parts[-1].strip().split(" ")[-1]
                if city and city not in processed_cities:
                    fuel_data = await self._safe_call("intel", "get_fuel_prices", {"city": city, "district": "merkez"})
                    if fuel_data and "error" not in fuel_data:
                        prices = fuel_data.get("data", fuel_data)
                        if isinstance(prices, list) and len(prices) > 0:
                            # Şehirdeki en ucuz fiyatı bul
                            cheapest_in_city = min([p.get(fuel_type, 999) for p in prices])
                            city_prices[city] = cheapest_in_city
                    processed_cities.add(city)

        # 4. En ucuz şehri belirle
        cheapest_city = None
        if city_prices:
            cheapest_city = min(city_prices, key=city_prices.get)

        return {
            "status": "success",
            "route_summary": {
                 "distance": safe_route_summary["distance_km"],
                 "duration": safe_route_summary["duration_min"]
            },
            "polyline": polyline,
            "cheapest_fuel_city": {
                "city": cheapest_city,
                "price": city_prices.get(cheapest_city) if cheapest_city else None
            },
            "best_station_recommendation": {
                 "name": best_station.get("name"),
                 "address": best_station.get("address"),
                 "open_now": best_station.get("is_open", best_station.get("open_now")),
                 "rating": best_station.get("rating"),
                 "lat": best_station.get("lat"),
                 "lon": best_station.get("lon"),
                 "estimated_price": city_prices.get(predicted_city)
            },
            "all_analyzed_cities": city_prices,
            "stations_found": len(places),
            "places": places[:5], # Return top 5 places to be used as markers
        }

class ContextAwarePOIPlanner:
    """
    Macro-Tool: Anlamsal (semantic) mekan araması yapar, hava durumunu kontrol eder
    ve hava durumuna göre (rain_factor) filtrelenmiş bir rota çizer.
    """
    def __init__(self, mcp_client):
        self.mcp = mcp_client

    async def _safe_call(self, service: str, tool_name: str, args: dict) -> dict:
        try:
            res = await self.mcp.mcp_rpc_call(service, "tools/call", {"name": tool_name, "arguments": args})
            if isinstance(res, dict):
                return res
            if isinstance(res, str):
                import json
                try:
                    return json.loads(res)
                except:
                    return {"status": "success", "data": res}
            return {"status": "error", "message": f"Beklenmeyen tür: {type(res)}"}
        except Exception as e:
            log.error(f"API Error in {tool_name}: {str(e)}")
            return {"status": "error", "message": str(e)}

    async def evaluate(self, current_lat: float, current_lon: float, semantic_query: str, location_name: Optional[str] = None, search_radius: float = 5000, username: str = "test_pilot") -> Dict[str, Any]:
        log.info(f"🧠 [Macro-Tool] ContextAwarePOIPlanner: '{semantic_query}' @ {location_name or (str(current_lat)+','+str(current_lon))}")
        
        # 1. Kullanıcı tercihlerini al (Kişiselleştirme)
        pref_res = await self._safe_call("city", "get_pool", {}) # Placeholder for actual pref call if available, or use a default
        # NOT: Gerçek sistemde bu ProfileManager'dan gelir. Şimdilik manuel sorgu ekliyoruz.
        
        # 2. Koordinat Çözümleme
        search_lat, search_lon = current_lat, current_lon
        if location_name:
            geo_res = await self._safe_call("city", "search_hybrid_places", {"query": location_name, "location_name": location_name, "limit": 1})
            places_data = geo_res.get("places", []) if isinstance(geo_res, dict) else []
            if places_data:
                search_lat = float(places_data[0]["lat"])
                search_lon = float(places_data[0]["lon"])

        # 3. Hava Durumu Analizi
        weather_res = await self._safe_call("city", "get_weather", {"lat": search_lat, "lon": search_lon})
        weather_condition = "Açık"
        is_bad_weather = False
        
        if "error" not in weather_res:
            weather_data = weather_res.get("data", weather_res)
            if isinstance(weather_data, dict) and "ANLIK_DURUM" in weather_data:
                condition_raw = weather_data["ANLIK_DURUM"].get("durum", "").lower()
                weather_condition = condition_raw
                if any(w in condition_raw for w in ["rain", "drizzle", "thunderstorm", "yağmur", "kar", "snow", "fırtına"]):
                    is_bad_weather = True
                    # Hava kötüyse kapalı mekan (indoor) tercihini sorguya ekle
                    if "kapalı" not in semantic_query.lower() and "iç" not in semantic_query.lower():
                        semantic_query += " kapalı mekan"
                    search_radius = 2000 # Hareket kabiliyeti azalır
        
        # 4. HİBRİT ARAMA (Google + OSM + RAG)
        # Artık search_hybrid_places kendi içinde RAG yapıyor!
        poi_res = await self._safe_call("city", "search_hybrid_places", {
            "query": semantic_query,
            "lat": search_lat,
            "lon": search_lon,
            "location_name": location_name,
            "category": "commercial"
        })
        
        places = poi_res.get("places", []) if isinstance(poi_res, dict) else []
        
        if not places:
             return {
                 "status": "error",
                 "message": f"Kritere uygun mekan bulunamadı. ({semantic_query})",
                 "weather_context": weather_condition,
                 "suggestion": "Daha genel bir arama yapmayı deneyin veya farklı bir bölge seçin."
             }

        # 5. Sonuçları işle ve en uygun olanı seç
        best_poi = places[0]
        route_res = await self._safe_call("city", "get_route_data", {
            "origin": f"{current_lat},{current_lon}",
            "destination": f"{best_poi['lat']},{best_poi['lon']}"
        })

        markers = []
        for p in places[:5]: # En iyi 5 mekan
            markers.append({
                "name": p.get("name"),
                "lat": p.get("lat"),
                "lon": p.get("lon"),
                "description": p.get("address", p.get("fusion_status", "")),
                "type": "poi"
            })
        
        return {
            "status": "success",
            "intent_analyzed": semantic_query,
            "weather_analysis": {
                "condition": weather_condition,
                "is_bad_weather": is_bad_weather,
                "impact": "Kapalı mekanlar önceliklendirildi." if is_bad_weather else "Normal arama."
            },
            "recommendation": {
                "name": best_poi.get("name"),
                "address": best_poi.get("address"),
                "rating": best_poi.get("rating"),
                "fusion_status": best_poi.get("fusion_status")
            },
            "map": {
                "markers": markers,
                "polyline": route_res.get("polyline") if isinstance(route_res, dict) else ""
            },
            "route_summary": {
                "distance": route_res.get("mesafe_km"),
                "duration": route_res.get("sure_dk")
            },
            "alternatives": [p.get("name") for p in places[1:4]]
        }

class EnvironmentalAnalyst:
    """Satellite data aggregator for environmental health and imagery."""
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    async def _safe_call(self, service, tool, params):
        try:
            return await self.orchestrator.call_tool(service, tool, params)
        except Exception as e:
            log.error(f"Satellite Macro Error: {str(e)}")
            return {"status": "error", "message": str(e)}

    async def evaluate(self, lat: float, lon: float, analyze_vegetation: bool = True) -> Dict[str, Any]:
        log.info(f"🛰️ [Macro-Tool] EnvironmentalAnalyst: {lat},{lon}")
        
        # BBOX hesapla (yaklaşık 2km x 2km)
        offset = 0.01 
        bbox = {
            "min_lon": lon - offset,
            "min_lat": lat - offset,
            "max_lon": lon + offset,
            "max_lat": lat + offset
        }

        results = {}
        
        # 1. Bitki örtüsü raporu (NDVI/EVI)
        if analyze_vegetation:
            veg_res = await self._safe_call("satellite", "get_vegetation_report", bbox)
            results["vegetation"] = veg_res

        # 2. Son görüntüler
        img_res = await self._safe_call("satellite", "search_satellite_imagery", bbox)
        results["imagery"] = img_res

        return {
            "status": "success",
            "location": f"{lat},{lon}",
            "analysis": results,
            "summary": "Uydu verileri üzerinden çevresel analiz tamamlandı."
        }
