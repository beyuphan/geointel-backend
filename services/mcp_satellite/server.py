from fastmcp import FastMCP
from tools.stac_client import SatelliteClient
import json
import uvicorn

# Host/Port burada YOK (0.1.0 standardı)
mcp = FastMCP("GeoIntel Satellite Node")

# İstemciyi başlat
sat_client = SatelliteClient()

@mcp.tool()
def search_satellite_imagery(
    min_lon: float, 
    min_lat: float, 
    max_lon: float, 
    max_lat: float, 
    days_back: int = 10
) -> str:
    """
    Belirtilen koordinat aralığında (Bounding Box) en son Sentinel-2 uydu görüntülerini arar.
    """
    bbox = [min_lon, min_lat, max_lon, max_lat]
    
    try:
        items = sat_client.search_sentinel2(bbox, days_back=days_back)
        
        if not items:
            return "Belirtilen kriterlerde uydu görüntüsü bulunamadı."

        results = []
        for item in items[:5]:
            results.append({
                "id": item.id,
                "date": item.datetime.isoformat(),
                "cloud_cover": item.properties.get("eo:cloud_cover", 100),
                "platform": item.properties.get("platform", "unknown"),
                "thumbnail": item.assets["visual"].href if "visual" in item.assets else "N/A"
            })
            
        return json.dumps(results, indent=2)

    except Exception as e:
        return f"Uydu arama hatası: {str(e)}"

if __name__ == "__main__":
    print("🚀 Satellite Agent (MCP) Başlatılıyor... [Port: 8002]")
    # 0.1.0 Sürümü için ayarlar burada olmalı:
    mcp.run(transport="sse", host="0.0.0.0", port=8002)