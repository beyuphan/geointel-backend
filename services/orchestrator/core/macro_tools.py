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
        try:
            res = await self.mcp.mcp_rpc_call(service, "tools/call", {"name": tool_name, "arguments": args})
            if isinstance(res, dict) and "content" in res:
                 text = res["content"][0].get("text", "")
                 try:
                     return json.loads(text)
                 except: return {"status": "error", "message": text}
            return res if isinstance(res, dict) else {"status": "error", "message": "Unknown format"}
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
             return {"status": "error", "message": f"Rota oluşturulamadı: {route_res.get('message', 'Bilinmeyen hata')}"}

        # Burada rotadaki kilit şehir/ilçeleri çıkarıyoruz (örnek için mock, gerçekte polyline sampling yapabilir. Yada city router'dan yardım isteyebiliriz)
        polyline = route_res.get("polyline", "")
        if not polyline or "GİZLENDİ" in polyline or "HARİTA" in polyline:
             return {"status": "error", "message": "Geçerli bir polyline bulunamadı."}

        # 2. Rotadaki önemli noktaları bulmak için (şu an basitçe origin ve destination kullanıyoruz, genişletilebilir)
        # Örnek: Şehir analizi için Google Places veya benzeri bir servisi çağır (örneğin rota üstü ilk 1 benzili bul)
        places_res = await self._safe_call("city", "search_places_google", {
            "query": "benzin petrol akaryakıt",
            "route_polyline": polyline
        })
        
        if "error" in places_res or places_res.get("status") == "error":
            return {"status": "partial_success", "route": route_res, "warning": "Akaryakıt istasyonları bulunamadı."}
            
        places = places_res.get("strict_route_places", []) + places_res.get("relaxed_route_places", [])
        if not places:
             return {
                 "status": "success", 
                 "route": route_res,
                 "analysis": "Rota üzerinde uygun istasyon bulunamadı."
             }

        # 3. İstasyonların bulunduğu ana şehri/ilçeyi adreslerinden ayıklayıp Intel'e sor
        # Basitlik adına en iyi yorumlu istasyonun şehrini varsayalım
        best_station = places[0]
        # Adresten şehir tahmini (Tricky ama intel servisi için şart)
        address_parts = best_station.get("address", "").split(",")
        predicted_city = address_parts[-1].strip().split(" ")[-1] if len(address_parts) > 0 else "Bilinmiyor"
        
        # 4. Yakıt Fiyatı Sorgusu (Intel Server)
        fuel_res = await self._safe_call("intel", "get_fuel_prices", {"city": predicted_city, "district": "merkez"})
        
        return {
            "status": "success",
            "route_summary": {
                 "distance": route_res.get("mesafe_km"),
                 "duration": route_res.get("sure_dk")
            },
            "best_station_recommendation": {
                 "name": best_station.get("name"),
                 "address": best_station.get("address"),
                 "open_now": best_station.get("open_now"),
                 "eta": best_station.get("eta", "Bilinmiyor"),
                 "deviation_meters": best_station.get("deviation_meters", 0)
            },
            "regional_fuel_prices": fuel_res.get("data", fuel_res) if fuel_res else "Fiyat verisi alınamadı."
        }
