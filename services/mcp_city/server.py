from fastmcp import FastMCP
from tools.osm import search_infrastructure_osm_handler
from tools.google import search_places_google_handler
from tools.here import get_route_data_handler
from tools.weather import get_weather_handler
from tools.db import save_location_handler

# --- MCP KURULUMU ---
mcp = FastMCP(name="City Agent")

# --- TOOL TANIMLARI (Sadece Yönlendirme Yapar) ---

@mcp.tool()
async def search_infrastructure_osm(lat: float, lon: float, category: str) -> list:
    """Kamusal alanları (Havalimanı, Park, Meydan) OSM'den bulur."""
    return await search_infrastructure_osm_handler(lat, lon, category)

@mcp.tool()
async def search_places_google(query: str, lat: float = None, lon: float = None) -> list:
    """Ticari mekanları (Restoran, Kafe) Google'dan bulur."""
    return await search_places_google_handler(query, lat, lon)

@mcp.tool()
async def get_route_data(origin: str, destination: str) -> dict:
    """İki nokta arası rota ve süre hesaplar."""
    return await get_route_data_handler(origin, destination)

@mcp.tool()
async def get_weather(lat: float, lon: float) -> dict:
    """Koordinatın hava durumunu getirir."""
    return await get_weather_handler(lat, lon)

@mcp.tool()
async def save_location(name: str, lat: float, lon: float, category: str = "Genel", note: str = "") -> str:
    """Konumu veritabanına kaydeder."""
    return await save_location_handler(name, lat, lon, category, note)

if __name__ == "__main__":
    mcp.run(transport="sse", host="0.0.0.0", port=8000)