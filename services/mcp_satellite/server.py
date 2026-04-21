"""
mcp_satellite/server.py — v2.1 (Async Production Ready)

Araçlar:
- search_satellite_imagery: Sentinel-2 görüntüsü arama
- calculate_ndvi: NDVI analizi (vegetation index)
- calculate_evi: EVI analizi (urban-aware)
- get_vegetation_report: Detaylı rapor (NDVI + EVI + yorum)
"""
import json
import asyncio
from contextlib import asynccontextmanager
from fastmcp import FastMCP
from loguru import logger as log
from tools.stac_client import get_satellite_client, _validate_bbox, _interpret_ndvi


@asynccontextmanager
async def lifespan(server):
    log.info("🛰️ [Satellite Agent] Başlatılıyor...")
    yield
    log.info("🛰️ [Satellite Agent] Kapatılıyor...")


mcp = FastMCP(name="Satellite Agent", version="2.0.0", lifespan=lifespan)


@mcp.tool()
async def search_satellite_imagery(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    days_back: int = 10,
    max_cloud_cover: int = 30,
) -> str:
    """
    Searches recent Sentinel-2 satellite imagery for a bounding box area.
    Returns list of available images with date, cloud cover and thumbnail.

    Args:
        min_lon: Min longitude of area (west edge)
        min_lat: Min latitude of area (south edge)
        max_lon: Max longitude of area (east edge)
        max_lat: Max latitude of area (north edge)
        days_back: How many days back to search (default: 10)
        max_cloud_cover: Max cloud cover percentage (default: 30)
    """
    valid, msg = _validate_bbox(min_lon, min_lat, max_lon, max_lat)
    if not valid:
        return json.dumps({"error": f"Geçersiz bbox: {msg}"}, ensure_ascii=False)

    bbox = [min_lon, min_lat, max_lon, max_lat]
    try:
        client = get_satellite_client()
        # STAC search is blocking — run in thread to avoid blocking event loop
        items = await asyncio.to_thread(client.search_sentinel2, bbox, days_back=days_back, max_cloud_cover=max_cloud_cover)

        if not items:
            return json.dumps({
                "status": "no_data",
                "message": f"Son {days_back} günde bulut kapsamı <%{max_cloud_cover} olan görüntü bulunamadı.",
                "suggestion": "days_back veya max_cloud_cover değerini artırın."
            }, ensure_ascii=False)

        results = []
        for item in items[:5]:
            results.append({
                "id": item.id,
                "date": item.datetime.isoformat() if getattr(item, "datetime", None) else "?",
                "cloud_cover_pct": item.properties.get("eo:cloud_cover", "?"),
                "platform": item.properties.get("platform", "Sentinel-2"),
                "thumbnail": item.assets.get("visual", item.assets.get("rendered_preview", {})).get("href", "N/A"),
            })

        return json.dumps({
            "status": "success",
            "bbox": bbox,
            "total_found": len(items),
            "showing": len(results),
            "images": results
        }, ensure_ascii=False)

    except Exception as e:
        log.error(f"[Satellite] search_satellite_imagery hatası: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def calculate_ndvi(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    days_back: int = 30,
    max_cloud_cover: int = 40,
) -> str:
    """
    Calculates NDVI (Normalized Difference Vegetation Index) for an area.
    NDVI range: -1 (water/rock) to +1 (dense vegetation).
    Uses Sentinel-2 COG streaming — only relevant pixels downloaded.

    Args:
        min_lon/min_lat/max_lon/max_lat: Bounding box coordinates
        days_back: Search within recent N days
        max_cloud_cover: Max acceptable cloud cover %
    """
    valid, msg = _validate_bbox(min_lon, min_lat, max_lon, max_lat)
    if not valid:
        return json.dumps({"error": f"Geçersiz bbox: {msg}"}, ensure_ascii=False)

    bbox = [min_lon, min_lat, max_lon, max_lat]
    try:
        client = get_satellite_client()
        items = await asyncio.to_thread(client.search_sentinel2, bbox, days_back=days_back, max_cloud_cover=max_cloud_cover)

        if not items:
            return json.dumps({
                "status": "no_data",
                "message": "Kullanılabilir Sentinel-2 görüntüsü bulunamadı.",
            }, ensure_ascii=False)

        item = items[0]
        ndvi = await asyncio.to_thread(client.calculate_ndvi, item, bbox)

        if ndvi is None:
            return json.dumps({"error": "NDVI hesaplanamadı (veri erişim hatası)."}, ensure_ascii=False)

        yorum = _interpret_ndvi(ndvi)

        return json.dumps({
            "status": "success",
            "ndvi": ndvi,
            "ndvi_pct": f"{ndvi * 100:.1f}%",
            "seviye": yorum["seviye"],
            "yorum": yorum["yorum"],
            "renk_kodu": yorum["renk_kodu"],
            "görüntü": {
                "id": item.id,
                "tarih": item.datetime.isoformat() if getattr(item, "datetime", None) else "?",
                "bulut_kapsamı": item.properties.get("eo:cloud_cover", "?"),
            }
        }, ensure_ascii=False)

    except Exception as e:
        log.error(f"[Satellite] calculate_ndvi hatası: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def calculate_evi(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    days_back: int = 30,
    max_cloud_cover: int = 40,
) -> str:
    """
    Calculates EVI (Enhanced Vegetation Index) for an area.
    EVI is more reliable than NDVI in urban/high-vegetation areas (corrects for soil/atmosphere).

    Args:
        min_lon/min_lat/max_lon/max_lat: Bounding box coordinates
        days_back: Search within recent N days
        max_cloud_cover: Max cloud cover %
    """
    valid, msg = _validate_bbox(min_lon, min_lat, max_lon, max_lat)
    if not valid:
        return json.dumps({"error": f"Geçersiz bbox: {msg}"}, ensure_ascii=False)

    bbox = [min_lon, min_lat, max_lon, max_lat]
    try:
        client = get_satellite_client()
        items = await asyncio.to_thread(client.search_sentinel2, bbox, days_back=days_back, max_cloud_cover=max_cloud_cover)

        if not items:
            return json.dumps({"status": "no_data", "message": "Görüntü bulunamadı."}, ensure_ascii=False)

        item = items[0]
        evi = await asyncio.to_thread(client.calculate_evi, item, bbox)

        if evi is None:
            return json.dumps({"error": "EVI hesaplanamadı."}, ensure_ascii=False)

        return json.dumps({
            "status": "success",
            "evi": evi,
            "yorum": "Kentsel alanlar için daha güvenilir bitki örtüsü indeksi.",
            "tarih": item.datetime.isoformat() if getattr(item, "datetime", None) else "?",
        }, ensure_ascii=False)

    except Exception as e:
        log.error(f"[Satellite] calculate_evi hatası: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def get_vegetation_report(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    days_back: int = 30,
    max_cloud_cover: int = 50,
) -> str:
    """
    Generates a comprehensive vegetation health report for an area.
    Calculates both NDVI and EVI, provides interpretation and recommendations.
    Use for environmental assessment, agricultural monitoring, or urban green analysis.

    Args:
        min_lon/min_lat/max_lon/max_lat: Bounding box coordinates
        days_back: Look back N days for imagery
        max_cloud_cover: Max cloud cover % (higher = more results but lower quality)
    """
    valid, msg = _validate_bbox(min_lon, min_lat, max_lon, max_lat)
    if not valid:
        return json.dumps({"error": f"Geçersiz bbox: {msg}"}, ensure_ascii=False)

    bbox = [min_lon, min_lat, max_lon, max_lat]
    try:
        client = get_satellite_client()
        items = await asyncio.to_thread(client.search_sentinel2, bbox, days_back=days_back, max_cloud_cover=max_cloud_cover)

        if not items:
            return json.dumps({
                "status": "no_data",
                "message": f"Son {days_back} günde uygun görüntü bulunamadı.",
                "öneri": "days_back veya max_cloud_cover değerini artırın."
            }, ensure_ascii=False)

        item = items[0]
        ndvi = await asyncio.to_thread(client.calculate_ndvi, item, bbox)
        evi = await asyncio.to_thread(client.calculate_evi, item, bbox)

        ndvi_yorum = _interpret_ndvi(ndvi) if ndvi is not None else {"seviye": "N/A", "yorum": "Hesaplanamadı"}

        # Öneri motoru
        oneriler = []
        if ndvi is not None:
            if ndvi < 0.1:
                oneriler.append("⚠️ Bitki örtüsü son derece düşük — olası kuraklık veya kentsel yoğunluk.")
            elif ndvi < 0.3:
                oneriler.append("🟡 Bitki örtüsü seyrek — sulama veya iyileştirme önerilebilir.")
            else:
                oneriler.append("✅ Bitki örtüsü sağlıklı görünüyor.")

            if evi is not None and abs(ndvi - evi) > 0.15:
                oneriler.append("📊 NDVI ve EVI arasında fark var — atmosfer veya kentsel etkisi olabilir, EVI daha güvenilir.")

        return json.dumps({
            "status": "success",
            "tarih": item.datetime.isoformat() if getattr(item, "datetime", None) else "?",
            "bulut_kapsamı_pct": item.properties.get("eo:cloud_cover", "?"),
            "ndvi": ndvi,
            "evi": evi,
            "seviye": ndvi_yorum["seviye"],
            "yorum": ndvi_yorum["yorum"],
            "oneriler": oneriler,
        }, ensure_ascii=False)

    except Exception as e:
        log.error(f"[Satellite] get_vegetation_report hatası: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


if __name__ == "__main__":
    log.info("🚀 Satellite Agent v2.0 (MCP) Port 8002 üzerinde başlatılıyor...")
    mcp.run(transport="sse", host="0.0.0.0", port=8002)