import asyncio
import json
from loguru import logger as log
from typing import Dict, Any, List, Optional


class RouteStrategyEvaluator:
    """
    Kullanıcının rotasını hesaplayıp, o rota üzerindeki şehirlerde yakıt fiyatlarını
    sorgulayan ve fiyat/performans (eta/sapma) analizi yapan Macro-Tool yöneticisi.
    Bu sınıf Orchestrator'da barındırılır çünkü hem 'mcp_city' hem 'mcp_intel' ile konuşur.

    Feature 1: Rota polyline'ı oluşturulduktan sonra midpoint üzerinden benzinlik arar.
    Feature 3: mcp_intel pipeline — her istasyona gerçek akaryakıt fiyatı inject eder,
               sonuçları ucuzdan pahalıya sıralar.
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

    async def evaluate(
        self,
        origin: str,
        destination: str,
        fuel_type: str = "benzin",
        fuel_range: Optional[float] = None,
        polyline: Optional[str] = None,
        total_dist_km: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Ana Macro-Tool metodu. Rota çıkarır, şehirleri bulur, fiyatı basar."""
        log.info(f"🧠 [Macro-Tool] Rota Stratejisi Başlatıldı: {origin} -> {destination}")

        # ─── 1. BASE ROUTE — polyline dışarıdan verilmişse route çağrısını atla ──
        if polyline and total_dist_km is not None:
            safe_route_summary = {
                "distance_km": total_dist_km,
                "polyline": polyline,
                "status": "success",
            }
            total_dist = float(total_dist_km)
        else:
            route_res = await self._safe_call(
                "city", "get_route_data", {"origin": origin, "destination": destination}
            )
            if "error" in route_res or route_res.get("status") == "error":
                return {
                    "status": "error",
                    "message": f"Rota oluşturulamadı: {route_res.get('message', route_res.get('error', 'Bilinmeyen hata'))}",
                }

            polyline = route_res.get("polyline") or route_res.get("polyline_encoded", "")
            if not polyline or "GİZLENDİ" in str(polyline) or "HARİTA" in str(polyline):
                return {"status": "error", "message": "Geçerli bir polyline bulunamadı."}

            safe_route_summary = {
                "distance_km": route_res.get("mesafe_km") or route_res.get("distance_km"),
                "duration_min": route_res.get("sure_dk") or route_res.get("duration_min"),
                "polyline": polyline,
                "status": "success",
            }
            total_dist_raw = route_res.get("mesafe_km", 0) or route_res.get("distance_km", 0)
            try:
                total_dist = float(total_dist_raw)
            except Exception:
                total_dist = 500.0

        # ─── 2. MENZILE GÖRE HEDEF MESAFELERİ BELİRLE ──────────────────────────────────────

        target_distances = []
        if fuel_range:
            curr = fuel_range * 0.8
            while curr < total_dist:
                target_distances.append(curr)
                curr += fuel_range * 0.8
        
        if not target_distances:
            target_distances = [total_dist * 0.5]

        # ─── 3. HER HEDEF İÇİN AYRI ARAMA YAP ──────────────────────────────────────
        import asyncio
        search_tasks = []
        for target in target_distances:
            fraction = target / total_dist
            search_tasks.append(self._safe_call(
                "city",
                "search_hybrid_places",
                {"query": f"{fuel_type} istasyonu", "route_polyline": polyline, "target_fraction": fraction},
            ))

        results = await asyncio.gather(*search_tasks)
        
        places: List[dict] = []
        for places_res in results:
            if isinstance(places_res, dict):
                p_list = places_res.get("places", [])
                if not p_list:
                    p_list = places_res.get("strict_route_places", []) + places_res.get("relaxed_route_places", [])
                places.extend(p_list)
            elif isinstance(places_res, list):
                places.extend(places_res)

        selected_places = []
        if fuel_range:
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
                # distance_along_route_km > 0 ve fuel_range içinde olan istasyonları al
                filtered_places = [
                    p for p in places
                    if 0 < p.get("distance_along_route_km", 0) <= fuel_range
                ]
                if filtered_places:
                    places = filtered_places
                    places.sort(
                        key=lambda p: abs(fuel_range * 0.8 - p.get("distance_along_route_km", 0))
                    )

        if not places:
            return {
                "status": "success",
                "route": safe_route_summary,
                "analysis": "Rota üzerinde uygun istasyon bulunamadı.",
            }

        # ─── 4. FEATURE 3: mcp_intel PIPELINE — Fiyat Inject & Sıralama ──────
        log.info(f"⛽ [Fuel Pipeline] {len(places)} istasyon için mcp_intel'den fiyat çekiliyor...")

        city_price_cache: Dict[str, list] = {}  # "city|district" -> FuelPrice listesi

        async def _enrich_station(station: dict) -> dict:
            """Tek bir istasyona mcp_intel'den gerçek akaryakıt fiyatı inject et."""
            address = str(station.get("address", ""))
            parts = [p.strip() for p in address.split(",")]
            if parts and parts[-1].lower() in ["türkiye", "turkey"]:
                parts.pop()
            
            city_guess = ""
            district_guess = "merkez"
            
            if parts:
                last_part = parts[-1]
                tokens = last_part.split(" ")
                location_str = tokens[-1]
                
                if "/" in location_str:
                    d_c = location_str.split("/")
                    district_guess = d_c[0]
                    city_guess = d_c[1]
                else:
                    city_guess = location_str

            if not city_guess:
                station["fuel_price_enriched"] = False
                return station

            ck = f"{city_guess}|{district_guess}".lower()

            if ck not in city_price_cache:
                fuel_res = await self._safe_call(
                    "intel",
                    "get_fuel_prices",
                    {"city": city_guess, "district": district_guess},
                )
                city_price_cache[ck] = fuel_res.get("data", []) if isinstance(fuel_res, dict) else []

            price_list = city_price_cache[ck]

            if price_list:
                type_key_map = {
                    "benzin": "gasoline",
                    "motorin": "diesel",
                    "dizel": "diesel",
                    "lpg": "lpg",
                }
                price_key = type_key_map.get(fuel_type.lower(), "gasoline")
                valid = [
                    p for p in price_list
                    if isinstance(p.get(price_key), (int, float)) and p.get(price_key, 0) > 5
                ]

                if valid:
                    cheapest = min(valid, key=lambda p: p.get(price_key, 9999))
                    station["fuel_price"] = {
                        "price_per_liter": cheapest.get(price_key),
                        "company": cheapest.get("company"),
                        "fuel_type": fuel_type,
                        "city": city_guess,
                        "district": district_guess,
                    }
                    station["fuel_price_enriched"] = True
                    log.info(
                        f"💰 [{station.get('name', '?')}] "
                        f"{cheapest.get(price_key)} TL/{fuel_type} ({cheapest.get('company')})"
                    )
                    return station

            station["fuel_price_enriched"] = False
            return station

        # Paralel fiyat sorgulama (en fazla 8 istasyon)
        enriched_places: List[dict] = list(
            await asyncio.gather(*[_enrich_station(p) for p in places[:8]])
        )

        # Ucuzdan pahalıya sırala; fiyat bilinmeyenler en sona
        def _sort_key(p):
            fp = p.get("fuel_price") or {}
            price = fp.get("price_per_liter")
            has_price = isinstance(price, (int, float))
            dev = p.get("deviation_meters", p.get("mesafe_raw", 99999))
            return (0 if has_price else 1, price if has_price else 9999, dev)

        enriched_places.sort(key=_sort_key)

        best_station = enriched_places[0] if enriched_places else {}
        all_cities: Dict[str, float] = {
            p["fuel_price"]["city"]: p["fuel_price"]["price_per_liter"]
            for p in enriched_places
            if p.get("fuel_price_enriched") and p.get("fuel_price", {}).get("city")
        }
        cheapest_city = min(all_cities, key=lambda c: all_cities[c]) if all_cities else None

        return {
            "status": "success",
            "route_summary": {
                "distance": safe_route_summary.get("distance_km"),
                "duration": safe_route_summary.get("duration_min"),
            },
            "polyline": polyline,
            "cheapest_fuel_city": {
                "city": cheapest_city,
                "price": all_cities.get(cheapest_city) if cheapest_city else None,
            },
            "best_station_recommendation": {
                "name": best_station.get("name"),
                "address": best_station.get("address"),
                "open_now": best_station.get("is_open", best_station.get("open_now")),
                "open_at_arrival": best_station.get("open_at_arrival"),
                "rating": best_station.get("rating"),
                "lat": best_station.get("lat"),
                "lon": best_station.get("lon"),
                "fuel_price": best_station.get("fuel_price"),
            },
            "all_analyzed_cities": all_cities,
            "stations_found": len(enriched_places),
            # Fiyat inject edilmiş, ucuzdan pahalıya sıralı liste (Feature 3)
            "places": enriched_places[:5],
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
                try:
                    return json.loads(res)
                except Exception:
                    return {"status": "success", "data": res}
            return {"status": "error", "message": f"Beklenmeyen tür: {type(res)}"}
        except Exception as e:
            log.error(f"API Error in {tool_name}: {str(e)}")
            return {"status": "error", "message": str(e)}

    async def evaluate(
        self,
        current_lat: float,
        current_lon: float,
        semantic_query: str,
        location_name: Optional[str] = None,
        search_radius: float = 5000,
        username: str = "test_pilot",
    ) -> Dict[str, Any]:
        log.info(
            f"🧠 [Macro-Tool] ContextAwarePOIPlanner: '{semantic_query}' @ "
            f"{location_name or (str(current_lat) + ',' + str(current_lon))}"
        )

        # 1. Koordinat Çözümleme
        search_lat, search_lon = current_lat, current_lon
        if location_name:
            geo_res = await self._safe_call(
                "city",
                "search_hybrid_places",
                {"query": location_name, "location_name": location_name, "limit": 1},
            )
            places_data = geo_res.get("places", []) if isinstance(geo_res, dict) else []
            if places_data:
                search_lat = float(places_data[0]["lat"])
                search_lon = float(places_data[0]["lon"])

        # 2. Hava Durumu Analizi
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
                    if "kapalı" not in semantic_query.lower() and "iç" not in semantic_query.lower():
                        semantic_query += " kapalı mekan"
                    search_radius = 2000

        # 3. HİBRİT ARAMA (Google + OSM + RAG)
        poi_res = await self._safe_call(
            "city",
            "search_hybrid_places",
            {
                "query": semantic_query,
                "lat": search_lat,
                "lon": search_lon,
                "location_name": location_name,
                "category": "commercial",
            },
        )

        places = poi_res.get("places", []) if isinstance(poi_res, dict) else []

        if not places:
            return {
                "status": "error",
                "message": f"Kritere uygun mekan bulunamadı. ({semantic_query})",
                "weather_context": weather_condition,
                "suggestion": "Daha genel bir arama yapmayı deneyin veya farklı bir bölge seçin.",
            }

        # 4. En uygun olanı seç ve rota çiz
        best_poi = places[0]
        route_res = await self._safe_call(
            "city",
            "get_route_data",
            {
                "origin": f"{current_lat},{current_lon}",
                "destination": f"{best_poi['lat']},{best_poi['lon']}",
            },
        )

        markers = [
            {
                "name": p.get("name"),
                "lat": p.get("lat"),
                "lon": p.get("lon"),
                "description": p.get("address", p.get("fusion_status", "")),
                "type": "poi",
            }
            for p in places[:5]
        ]

        return {
            "status": "success",
            "intent_analyzed": semantic_query,
            "weather_analysis": {
                "condition": weather_condition,
                "is_bad_weather": is_bad_weather,
                "impact": "Kapalı mekanlar önceliklendirildi." if is_bad_weather else "Normal arama.",
            },
            "recommendation": {
                "name": best_poi.get("name"),
                "address": best_poi.get("address"),
                "rating": best_poi.get("rating"),
                "fusion_status": best_poi.get("fusion_status"),
            },
            "map": {
                "markers": markers,
                "polyline": route_res.get("polyline") if isinstance(route_res, dict) else "",
            },
            "route_summary": {
                "distance": route_res.get("mesafe_km"),
                "duration": route_res.get("sure_dk"),
            },
            "alternatives": [p.get("name") for p in places[1:4]],
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

        offset = 0.01
        bbox = {
            "min_lon": lon - offset,
            "min_lat": lat - offset,
            "max_lon": lon + offset,
            "max_lat": lat + offset,
        }

        results = {}

        if analyze_vegetation:
            veg_res = await self._safe_call("satellite", "get_vegetation_report", bbox)
            results["vegetation"] = veg_res

        img_res = await self._safe_call("satellite", "search_satellite_imagery", bbox)
        results["imagery"] = img_res

        return {
            "status": "success",
            "location": f"{lat},{lon}",
            "analysis": results,
            "summary": "Uydu verileri üzerinden çevresel analiz tamamlandı.",
        }
