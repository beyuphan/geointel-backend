import json
from fastmcp import FastMCP
from tools.models import StandardPlace, RouteResponse, WeatherResponse # Modelleri çağırdık
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
    raw_data = await search_infrastructure_osm_handler(lat, lon, category)
    
    # Hata kontrolü
    if raw_data and isinstance(raw_data, list) and "error" in raw_data[0]:
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
        standard_list.append(place.model_dump()) # Dict'e çevir

    return json.dumps(standard_list, ensure_ascii=False)

@mcp.tool()
async def search_places_google(query: str, lat: float = None, lon: float = None, route_polyline: str = None) -> str:
    """Ticari mekanları bulur. JSON String döner."""
    raw_data = await search_places_google_handler(query, lat, lon, route_polyline)

    if "error" in raw_data:
        return json.dumps({"status": "error", "message": raw_data["error"]})

    # Google'dan gelen 'strict' ve 'relaxed' listelerini birleştirip standartlaştıralım
    all_places = raw_data.get("strict_route_places", []) + raw_data.get("relaxed_route_places", [])
    
    standard_list = []
    for item in all_places:
        # Koordinatları "41.02,40.52" stringinden ayırıyoruz
        try:
            lat_str, lon_str = item["coords"].split(",")
            p_lat, p_lon = float(lat_str), float(lon_str)
        except:
            p_lat, p_lon = 0.0, 0.0

        place = StandardPlace(
            name=item.get("name"),
            address=item.get("address"),
            lat=p_lat,
            lon=p_lon,
            rating=item.get("rating"),
            is_open=str(item.get("open_now")), # String'e çevirelim garanti olsun
            source="google"
        )
        standard_list.append(place.model_dump())

    return json.dumps(standard_list, ensure_ascii=False)

@mcp.tool()
async def get_route_data(origin: str, destination: str) -> str:
    """Rota verisi. JSON String döner."""
    raw_data = await get_route_data_handler(origin, destination)

    if "error" in raw_data:
        return json.dumps({"status": "error", "message": raw_data["error"]})

    # RouteResponse modeline uygun hale getirelim
    response = RouteResponse(
        distance_km=raw_data.get("mesafe_km", 0),
        duration_min=raw_data.get("sure_dk", 0),
        polyline=raw_data.get("polyline_encoded", ""),
        summary=f"{raw_data.get('mesafe_km')} km, {raw_data.get('sure_dk')} dakika",
        checkpoints=raw_data.get("analiz_noktalari", {})
    )
    
    return response.model_dump_json()

@mcp.tool()
async def get_weather(lat: float, lon: float) -> str:
    """Hava durumu. JSON String döner."""
    raw_data = await get_weather_handler(lat, lon)

    if "error" in raw_data:
        return json.dumps({"status": "error", "message": raw_data["error"]})

    # WeatherResponse modeline eşleme (Mapping)
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

@mcp.tool()
async def analyze_route_weather(polyline: str) -> str:
    # Redis bağımlılığı bitti, sadece işini yap.
    result = await analyze_route_weather_handler(polyline)
    return json.dumps(result, ensure_ascii=False)

@mcp.tool()
async def save_location(name: str, lat: float, lon: float, category: str = "Genel", note: str = "") -> str:
    """Basit string dönüşü olduğu için JSON'a sarmaya gerek yok ama standart olsun."""
    result = await save_location_handler(name, lat, lon, category, note)
    return json.dumps({"status": "success", "message": result}, ensure_ascii=False)

@mcp.tool()
async def get_toll_prices(filter_region: str = None) -> str:
    """Metin tabanlı dönüş yapıyor, direkt JSON içinde 'text' olarak verelim."""
    text_result = await get_toll_prices_handler(filter_region)
    return json.dumps({"status": "success", "text": text_result}, ensure_ascii=False)

if __name__ == "__main__":
    mcp.run(transport="sse", host="0.0.0.0", port=8000)