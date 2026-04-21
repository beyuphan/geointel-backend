import asyncio
import json
from loguru import logger as log
from typing import Dict, Any, List

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

    async def evaluate(self, origin: str, destination: str, fuel_type: str = "benzin") -> Dict[str, Any]:
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
        
        if not places:
             return {
                 "status": "success", 
                 "route": safe_route_summary,
                 "analysis": "Rota üzerinde uygun istasyon bulunamadı."
             }

        # 3. İstasyonların bulunduğu şehri adreslerinden ayıklayıp Intel'e sor
        best_station = places[0]
        address_parts = str(best_station.get("address", "")).split(",")
        predicted_city = address_parts[-1].strip().split(" ")[-1] if len(address_parts) > 0 else "Bilinmiyor"
        
        # 4. Yakıt Fiyatı Sorgusu (Intel Server)
        fuel_res = await self._safe_call("intel", "get_fuel_prices", {"city": predicted_city, "district": "merkez"})
        
        return {
            "status": "success",
            "route_summary": {
                 "distance": safe_route_summary["distance_km"],
                 "duration": safe_route_summary["duration_min"]
            },
            "polyline": polyline,
            "best_station_recommendation": {
                 "name": best_station.get("name"),
                 "address": best_station.get("address"),
                 "open_now": best_station.get("is_open", best_station.get("open_now")),
                 "rating": best_station.get("rating"),
                 "lat": best_station.get("lat"),
                 "lon": best_station.get("lon"),
            },
            "regional_fuel_prices": fuel_res.get("data", fuel_res) if fuel_res else "Fiyat verisi alınamadı.",
            "stations_found": len(places),
        }
