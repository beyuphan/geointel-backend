import json
from fastmcp import FastMCP
from loguru import logger  # <--- Loglama eklendi
from tools.models import StandardPlace, RouteResponse, WeatherResponse
from tools.osm import search_infrastructure_osm_handler
from tools.google import search_places_google_handler
from tools.here import get_route_data_handler
from tools.weather import get_weather_handler, analyze_route_weather_handler
from tools.db import save_location_handler
from tools.toll import get_toll_prices_handler 

# --- MCP KURULUMU ---
mcp = FastMCP(name="City Agent")

# --- TOOL TANIMLARI ---

@mcp.tool()
async def search_infrastructure_osm(lat: float, lon: float, category: str) -> str:
    """Kamusal alanları bulur. JSON String döner."""
    try:
        logger.info(f"🛠️ [Tool: OSM] İstek: {category} @ {lat},{lon}")
        raw_data = await search_infrastructure_osm_handler(lat, lon, category)
        
        # Hata kontrolü
        if raw_data and isinstance(raw_data, list) and len(raw_data) > 0 and "error" in raw_data[0]:
            logger.warning(f"⚠️ [Tool: OSM] Hata döndü: {raw_data[0]['error']}")
            return json.dumps({"status": "error", "message": raw_data[0]["error"]})

        # Veriyi StandardPlace modeline döküyoruz
        standard_list = []
        for item in raw_data:
            place = StandardPlace(
                name=item.get("isim"),
                lat=item.get("lat"),
                lon=item.get("lon"),
                category=category,
                source="osm"
            )
            standard_list.append(place.model_dump())

        logger.success(f"✅ [Tool: OSM] {len(standard_list)} mekan bulundu.")
        return json.dumps(standard_list, ensure_ascii=False)
        
    except Exception as e:
        logger.error(f"🔥 [Tool: OSM] Kritik Hata: {e}")
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
async def search_places_google(query: str, lat: float = None, lon: float = None, route_polyline: str = None) -> str:
    """Ticari mekanları bulur. JSON String döner."""
    try:
        logger.info(f"🛠️ [Tool: Google] İstek: '{query}' (Rota Var mı: {'Evet' if route_polyline else 'Hayır'})")
        
        raw_data = await search_places_google_handler(query, lat, lon, route_polyline)

        if "error" in raw_data:
            logger.warning(f"⚠️ [Tool: Google] Servis hatası: {raw_data['error']}")
            return json.dumps({"status": "error", "message": raw_data["error"]})

        # Google'dan gelen 'strict' ve 'relaxed' listelerini birleştirip standartlaştıralım
        strict_places = raw_data.get("strict_route_places", [])
        relaxed_places = raw_data.get("relaxed_route_places", [])
        
        all_places = strict_places + relaxed_places
        
        standard_list = []
        for item in all_places:
            # Koordinatları "41.02,40.52" stringinden ayırıyoruz
            try:
                if "coords" in item and "," in item["coords"]:
                    lat_str, lon_str = item["coords"].split(",")
                    p_lat, p_lon = float(lat_str), float(lon_str)
                else:
                    p_lat, p_lon = 0.0, 0.0
            except Exception:
                p_lat, p_lon = 0.0, 0.0

            place = StandardPlace(
                name=item.get("name"),
                address=item.get("address"),
                lat=p_lat,
                lon=p_lon,
                rating=item.get("rating"),
                is_open=str(item.get("open_now")), 
                source="google"
            )
            # Rota bilgisi varsa mesafeyi de ekleyebiliriz (Model destekliyorsa)
            # Şu anlık StandardPlace modeline sadık kalıyoruz.
            standard_list.append(place.model_dump())

        logger.success(f"✅ [Tool: Google] Toplam {len(standard_list)} mekan işlendi. (Yol Üstü: {len(strict_places)}, Sapma: {len(relaxed_places)})")
        return json.dumps(standard_list, ensure_ascii=False)

    except Exception as e:
        logger.error(f"🔥 [Tool: Google] Kritik Hata: {e}")
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
async def get_route_data(origin: str, destination: str) -> str:
    """Rota verisi. JSON String döner."""
    try:
        logger.info(f"🛠️ [Tool: Rota] Hesapla: {origin} -> {destination}")
        raw_data = await get_route_data_handler(origin, destination)

        if "error" in raw_data:
            logger.error(f"❌ [Tool: Rota] Başarısız: {raw_data['error']}")
            return json.dumps({"status": "error", "message": raw_data["error"]})

        response = RouteResponse(
            distance_km=raw_data.get("mesafe_km", 0),
            duration_min=raw_data.get("sure_dk", 0),
            polyline=raw_data.get("polyline_encoded", ""),
            summary=f"{raw_data.get('mesafe_km')} km, {raw_data.get('sure_dk')} dakika",
            checkpoints=raw_data.get("analiz_noktalari", {})
        )
        
        logger.success(f"✅ [Tool: Rota] Rota oluşturuldu: {response.summary}")
        return response.model_dump_json()

    except Exception as e:
        logger.error(f"🔥 [Tool: Rota] Kritik Hata: {e}")
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
async def get_weather(lat: float, lon: float) -> str:
    """Hava durumu. JSON String döner."""
    try:
        logger.info(f"🛠️ [Tool: Hava] Sorgu: {lat},{lon}")
        raw_data = await get_weather_handler(lat, lon)

        if "error" in raw_data:
            return json.dumps({"status": "error", "message": raw_data["error"]})

        current = raw_data.get("ANLIK_DURUM", {})
        
        response = WeatherResponse(
            location=raw_data.get("lokasyon_koordinat", ""),
            current_temp=current.get("sicaklik", ""),
            feels_like=current.get("hissedilen", ""),
            condition=current.get("durum", ""),
            forecast_hourly=raw_data.get("ONUMUZDEKI_SAATLER", []),
            warning=raw_data.get("uyari")
        )

        return response.model_dump_json()
    except Exception as e:
        logger.error(f"🔥 [Tool: Hava] Hata: {e}")
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
async def analyze_route_weather(polyline: str) -> str:
    try:
        logger.info("🛠️ [Tool: Rota Hava] Analiz başlatılıyor...")
        result = await analyze_route_weather_handler(polyline)
        logger.success("✅ [Tool: Rota Hava] Analiz tamamlandı.")
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        logger.error(f"🔥 [Tool: Rota Hava] Hata: {e}")
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
async def save_location(name: str, lat: float, lon: float, category: str = "Genel", note: str = "") -> str:
    try:
        logger.info(f"💾 [Tool: DB] Kayıt: {name}")
        result = await save_location_handler(name, lat, lon, category, note)
        return json.dumps({"status": "success", "message": result}, ensure_ascii=False)
    except Exception as e:
        logger.error(f"🔥 [Tool: DB] Hata: {e}")
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
async def get_toll_prices(filter_region: str = None) -> str:
    try:
        logger.info("🛠️ [Tool: Otoyol] Fiyatlar çekiliyor...")
        text_result = await get_toll_prices_handler(filter_region)
        return json.dumps({"status": "success", "text": text_result}, ensure_ascii=False)
    except Exception as e:
        logger.error(f"🔥 [Tool: Otoyol] Hata: {e}")
        return json.dumps({"status": "error", "message": str(e)})

if __name__ == "__main__":
    logger.info("🚀 City Agent (MCP) Başlatılıyor...")
    mcp.run(transport="sse", host="0.0.0.0", port=8000)