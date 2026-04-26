import json
import asyncio
from contextlib import asynccontextmanager
from tools.geometry import calculate_distance_meters
import uvicorn
from fastmcp import FastMCP
from loguru import logger
from typing import List, Union, Optional

# --- MODELLER VE HANDLERLAR ---
from tools.models import StandardPlace, RouteResponse, WeatherResponse, ErrorResponse
from tools.osm import search_infrastructure_osm_handler
from tools.google import search_places_google_handler
from tools.here import get_route_data_handler, _resolve_coordinates
from tools.weather import get_weather_handler, analyze_route_weather_handler
from tools.db import save_location_handler, search_spatial_rag, save_poi_with_embedding, init_pool, close_pool
from tools.toll import get_toll_prices_handler, get_toll_for_route_handler
from tools.wfs import fetch_wfs_as_geojson, fetch_ibb_dataset_geojson, list_wfs_datasets
from tools.radar import get_radars_on_route_handler
from tools.cache import redis_store
from tools.route_summary import build_route_summary_handler
from tools.ev_charging import get_ev_charging_handler
from tools.traffic import get_route_traffic_handler
from safe_tools import safe_tool

# --- MCP SUNUCU KURULUMU ---
@asynccontextmanager
async def lifespan(server):
    """DB Pool ve kaynak yönetimi."""
    logger.info("🔌 [City Agent] DB Pool başlatılıyor...")
    await init_pool()
    yield
    logger.info("🔌 [City Agent] DB Pool kapatılıyor...")
    await close_pool()

mcp = FastMCP(name="City Agent", version="3.0.0", lifespan=lifespan)

def handle_critical_error(e: Exception, tool_name: str) -> str:
    """Hataları merkezi bir formatta loglar ve döner."""
    logger.error(f"🔥 [Tool: {tool_name}] Kritik Hata: {str(e)}")
    return ErrorResponse(message=f"{tool_name} işleminde hata oluştu: {str(e)}").model_dump_json()

# --- 1. OSM ALTYAPI ARAMA ---
@mcp.tool()
@safe_tool(fallback_message="OSM search unavailable.")
async def search_infrastructure_osm(lat: float, lon: float, category: str, radius: int = 2000) -> str:
    """
    Finds public (non-commercial) infrastructure near a location using OpenStreetMap.
    Use for: hospitals, schools, parks, stadiums, airports, mosques.
    Do NOT use for restaurants, cafes, shops — use search_hybrid_places instead.

    Args:
        lat: Center latitude.
        lon: Center longitude.
        category: OSM tag (e.g. hospital, park, airport, school).
        radius: Search radius in meters (default 2000).
    """
    try:
        logger.info(f"🛠️ [Tool: OSM] İstek: {category} @ {lat},{lon}")
        raw_data = await search_infrastructure_osm_handler(lat, lon, category, radius)
        
        # Handler hata dönerse (List içinde dict olarak gelme durumu)
        if raw_data and isinstance(raw_data, list) and len(raw_data) > 0 and "error" in raw_data[0]:
            logger.warning(f"⚠️ [Tool: OSM] Handler Hatası: {raw_data[0]['error']}")
            return ErrorResponse(message=raw_data[0]["error"]).model_dump_json()

        # Veriyi StandardPlace modeline dökerek standardize ediyoruz
        standard_list = []
        for item in raw_data:
            # Eksik koordinat veya isim kontrolü
            if not item.get("lat") or not item.get("lon") or not item.get("isim"):
                continue
                
            place = StandardPlace(
                name=item.get("isim"),
                lat=item.get("lat"),
                lon=item.get("lon"),
                category=category,
                source="osm"
            )
            standard_list.append(place.model_dump())

        logger.success(f"✅ [Tool: OSM] {len(standard_list)} mekan doğrulandı ve dönüldü.")
        return json.dumps(standard_list, ensure_ascii=False)
        
    except Exception as e:
        return handle_critical_error(e, "search_infrastructure_osm")

# search_places_google: Internal use only — called by search_hybrid_places macro
# NOT registered as MCP tool to prevent LLM from calling it directly
@safe_tool(fallback_message="Google Maps search unavailable.")
async def search_places_google(query: str, lat: float = 0.0, lon: float = 0.0, route_polyline: str = None) -> str:
    """INTERNAL: Google Places search. Use search_hybrid_places instead."""
    try:
        logger.info(f"🛠️ [Tool: Google] Sorgu: '{query}' (Rota: {'Aktif' if route_polyline else 'Pasif'})")
        raw_data = await search_places_google_handler(query, lat, lon, route_polyline)

        if "error" in raw_data:
            return ErrorResponse(message=raw_data["error"]).model_dump_json()

        # Yol üstü ve sapma listelerini birleştirip Pydantic ile doğruluyoruz
        all_places = raw_data.get("strict_route_places", []) + raw_data.get("relaxed_route_places", [])
        
        standard_list = []
        for item in all_places:
            try:
                # Koordinat parse & Null Island (0,0) koruması
                if "coords" in item and "," in item["coords"]:
                    coords_split = item["coords"].split(",")
                    p_lat, p_lon = float(coords_split[0]), float(coords_split[1])
                    if abs(p_lat) < 0.0001 and abs(p_lon) < 0.0001: continue
                else: continue

                place = StandardPlace(
                    name=item.get("name"),
                    address=item.get("address"),
                    lat=p_lat,
                    lon=p_lon,
                    rating=item.get("rating", 0.0),
                    review_count=item.get("review_count", 0),
                    is_open=str(item.get("open_now", "Bilinmiyor")),
                    source="google",
                    metadata={
                        "durum": item.get("konum_durumu", "Bilinmiyor"),
                        "deviation_meters": item.get("deviation_meters", 0)
                    }
                )
                standard_list.append(place.model_dump())
            except Exception as parse_error:
                logger.warning(f"⚠️ [Google] Mekan parse edilemedi: {parse_error}")
                continue

        logger.success(f"✅ [Tool: Google] {len(standard_list)} mekan işlendi.")
        return json.dumps(standard_list, ensure_ascii=False)

    except Exception as e:
        return handle_critical_error(e, "search_places_google")

# --- 3. HERE ROTA OLUŞTURMA ---
@mcp.tool()
@safe_tool(fallback_message="Route calculation failed.")
async def get_route_data(origin: str, destination: str, preference: str = "fastest") -> str:
    """
    Calculates route between two locations with real-time traffic, distance and ETA.
    Uses Istanbul local DB for city routes, HERE Maps for intercity.

    Args:
        origin: Start location name or 'lat,lon' coordinates.
        destination: End location name or 'lat,lon' coordinates.
        preference: 'fastest', 'shortest' or 'safest' (default: fastest).
    """
    try:
        logger.info(f"🛠️ [Tool: Rota] Hesapla: {origin} -> {destination} (Tercih: {preference})")
        raw_data = await get_route_data_handler(origin, destination, preference=preference)

        if "error" in raw_data:
            return ErrorResponse(message=raw_data["error"]).model_dump_json()

        # Rota polyline verisini belirle (Local veya HERE)
        poly_data = raw_data.get("polyline_encoded")
        final_polyline = poly_data if poly_data else "LOCAL_ROUTE"

        # Alternatif rota verilerini aktar (HERE API alternatifler döndürürse)
        alternatives = raw_data.get("alternatif_rotalar", [])

        response = RouteResponse(
            distance_km=raw_data.get("mesafe_km", 0),
            duration_min=raw_data.get("sure_dk", 0),
            polyline=final_polyline, 
            summary=f"{raw_data.get('mesafe_km')} km, {raw_data.get('sure_dk')} dakika",
            checkpoints=raw_data.get("analiz_noktalari", {}),
            source_system=raw_data.get("source", "Bilinmiyor"),
            alternatives=alternatives
        )
        
        logger.success(f"✅ [Tool: Rota] {response.source_system} üzerinden rota hazır. {len(alternatives)} alternatif.")
        return response.model_dump_json()

    except Exception as e:
        return handle_critical_error(e, "get_route_data")

# --- 4. HAVA DURUMU (KONUM BAZLI) ---
@mcp.tool()
@safe_tool(fallback_message="Weather data unavailable.")
async def get_weather(lat: float, lon: float) -> str:
    """Returns current weather and hourly forecast for the given coordinates."""
    try:
        logger.info(f"🛠️ [Tool: Hava] Sorgu: {lat},{lon}")
        raw_data = await get_weather_handler(lat, lon)

        if "error" in raw_data:
            return ErrorResponse(message=raw_data["error"]).model_dump_json()

        current = raw_data.get("ANLIK_DURUM", {})
        
        response = WeatherResponse(
            location=raw_data.get("lokasyon_koordinat", f"{lat},{lon}"),
            current_temp=current.get("sicaklik", "N/A"),
            feels_like=current.get("hissedilen", "N/A"),
            condition=current.get("durum", "N/A"),
            forecast_hourly=raw_data.get("ONUMUZDEKI_SAATLER", []),
            warning=raw_data.get("uyari")
        )

        return response.model_dump_json()
    except Exception as e:
        return handle_critical_error(e, "get_weather")

# --- 5. ROTA HAVA DURUMU (WHEATHER SHIELD) ---
@mcp.tool()
@safe_tool(fallback_message="Route weather analysis failed.")
async def analyze_route_weather(
    polyline: str,
    avg_speed_kmh: Optional[float] = 80.0,
    departure_minutes_from_now: Optional[float] = 0.0,
) -> str:
    """
    Analyzes weather risks (rain, snow, ice) along a route at 40km intervals based on estimated time of arrival (ETA).
    Returns risk level, ETA-adjusted risk zones, and conditions.
    Use 'LATEST' for active route polyline.
    You can tune avg_speed_kmh and departure_minutes_from_now if the user specifies them.
    """
    try:
        speed = avg_speed_kmh if avg_speed_kmh is not None else 80.0
        departure = departure_minutes_from_now if departure_minutes_from_now is not None else 0.0
        logger.info(f"🛠️ [Tool: Weather Shield] Analiz başlatılıyor... (Hız: {speed}km/h, Kalkış: +{departure}dk)")
        result = await analyze_route_weather_handler(
            polyline,
            avg_speed_kmh=speed,
            departure_minutes_from_now=departure
        )
        
        if isinstance(result, dict) and "error" in result:
            return ErrorResponse(message=result["error"]).model_dump_json()

        logger.success("✅ [Tool: Weather Shield] Analiz tamamlandı.")
        
        # Token optimisation logic — include ETA hour details so LLM can advise correctly
        summary_result = {
            "tarama_noktasi_sayisi": result.get("tarama_noktasi_sayisi", 0),
            "risk_durumu": result.get("risk_durumu", "BİLİNMİYOR"),
            "riskli_bolgeler": result.get("riskli_bolgeler", []),
            "detayli_ozet": result.get("detayli_ozet", []), # ETA saatleri burada
            "tavsiye": result.get("tavsiye", "")
        }
        return json.dumps(summary_result, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "analyze_route_weather")

# --- 6. KONUM KAYDETME (POSTGIS) ---
@mcp.tool()
async def save_location(name: str, lat: float, lon: float, category: str = "Genel", note: str = "") -> str:
    """Saves a location (home, work, favorite) to the database permanently."""
    try:
        logger.info(f"💾 [Tool: DB] Kayıt Deneniyor: {name}")
        result = await save_location_handler(name, lat, lon, category, note)
        return json.dumps({"status": "success", "message": result}, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "save_location")

# --- 7. OTOYOL VE KÖPRÜ ÜCRETLERİ ---
@mcp.tool()
async def get_toll_prices(filter_region: str = None) -> str:
    """Lists current highway, bridge, and tunnel toll prices in Turkey."""
    try:
        logger.info("🛠️ [Tool: Otoyol] Fiyat listesi çekiliyor...")
        text_result = await get_toll_prices_handler(filter_region)
        return json.dumps({"status": "success", "text": text_result}, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "get_toll_prices")

# --- 8. SPATIAL RAG (Hibrit Arama) ---
@mcp.tool()
async def search_hybrid_poi(query: str, lat: float, lon: float, radius: float = 5000, limit: int = 5) -> str:
    """
    Semantic POI search using pgvector embeddings filtered by spatial proximity.
    Finds places matching the query text within the given radius.
    """
    try:
        logger.info(f"🛠️ [Tool: SpatialRAG] Sorgu: '{query}' @ {lat},{lon}")
        results = await search_spatial_rag(query, lat, lon, radius, limit)
        return json.dumps(results, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "search_hybrid_poi")

@mcp.tool()
async def save_poi_to_db(name: str, description: str, lat: float, lon: float, category: str = "Tavsiye") -> str:
    """
    Saves a place with its description as a vector embedding for future RAG searches.
    Use when user recommends or likes a location.
    """
    try:
        logger.info(f"💾 [Tool: SpatialRAG Kayıt] {name}")
        result = await save_poi_with_embedding(name, description, lat, lon, category)
        return json.dumps({"status": "success", "message": result}, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "save_poi_to_db")

@mcp.tool()
async def fetch_wfs_layer(
    base_url: str,
    type_name: str,
    bbox: Optional[str] = None,
    src_epsg: Optional[int] = 5254,
    max_features: Optional[int] = 200,
) -> str:
    """
    Fetches a WFS layer and returns a GeoJSON FeatureCollection.

    Args:
        base_url: WFS endpoint URL.
        type_name: Layer name (WFS typeNames, e.g. 'ibb:afettoplanma').
        bbox: Optional bounding box 'minx,miny,maxx,maxy' (default CRS: EPSG:5254).
        src_epsg: Source coordinate system (usually 5254 for IBB data).
        max_features: Max number of features to return.
    """
    try:
        bbox_tuple = None
        if bbox:
            parts = [p.strip() for p in bbox.split(",")]
            if len(parts) >= 4:
                bbox_tuple = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))

        data = await fetch_wfs_as_geojson(
            base_url=base_url,
            type_name=type_name,
            bbox=bbox_tuple,
            src_epsg=src_epsg,
            dst_epsg=4326,
            max_features=max_features,
        )

        return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "fetch_wfs_layer")

@mcp.tool()
async def fetch_ibb_dataset(
    dataset_id: str,
    bbox: Optional[str] = None,
    max_features: Optional[int] = 200,
) -> str:
    """
    Fetches IBB (Istanbul Municipality) preset WFS datasets as GeoJSON.
    Endpoint is configured via IBB_WFS_BASE_URL env variable.
    """
    try:
        bbox_tuple = None
        if bbox:
            parts = [p.strip() for p in bbox.split(",")]
            if len(parts) >= 4:
                bbox_tuple = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))

        data = await fetch_ibb_dataset_geojson(
            dataset_id=dataset_id,
            bbox=bbox_tuple,
            max_features=max_features,
        )
        return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "fetch_ibb_dataset")

@mcp.tool()
async def list_ibb_datasets() -> str:
    """Lists all available IBB (Istanbul Municipality) WFS dataset IDs."""
    try:
        return json.dumps(list_wfs_datasets(), ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "list_ibb_datasets")


# _resolve_coordinates is imported at top — no duplicate import needed


# --- 11. ROTA RADARLARI (GERÇEK ZAMANLI - HERE MAPS) ---
@mcp.tool()
async def get_route_radars(route_polyline: str) -> str:
    """
    Scans for speed cameras and traffic hazards along a route using HERE Maps Traffic API.
    Always call after get_route_data. Use 'LATEST' for current route polyline.
    """
    try:
        logger.info("🚨 [Tool: Radar] Rota üzerinde kamera taraması yapılıyor...")
        result = await get_radars_on_route_handler(route_polyline)
        if "error" in result:
            return ErrorResponse(message=result["error"]).model_dump_json()
        logger.success(f"✅ [Tool: Radar] {result['total_count']} kamera bulundu.")
        
        # TOKEN OPTIMIZATION: LLM'e tüm radar koordinatlarını dönme. 
        # Sadece sayı durumu ve tehlike bilgisini gönder. 
        summary_result = {
            "total_count": result.get("total_count", 0),
            "summary": result.get("summary", ""),
            "warning": result.get("warning")
        }
        return json.dumps(summary_result, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "get_route_radars")


# --- 12. ROTA GEÇİŞ ÜCRETLERİ (GERÇEK ZAMANLI - HERE MAPS) ---
@mcp.tool()
@safe_tool(fallback_message="Toll query failed.")
async def get_toll_for_route(route_polyline: str) -> str:
    """
    Calculates bridge, tunnel and highway toll costs along a route using HERE Maps API.
    Always call after get_route_data. Use 'LATEST' for current route polyline.
    """
    try:
        logger.info("💰 [Tool: Toll] Rota ücreti hesaplanıyor...")
        result = await get_toll_for_route_handler(route_polyline)
        if "error" in result:
            return ErrorResponse(message=result["error"]).model_dump_json()
        logger.success(f"✅ [Tool: Toll] {result['toll_count']} geçiş tespit edildi. Toplam: {result['total_toll_cost_tl']} TL")
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "get_toll_for_route")

@mcp.tool()
@safe_tool(fallback_message="Hybrid place search failed.")
async def search_hybrid_places(query: str, location_name: Optional[str] = None, lat: Optional[float] = None, lon: Optional[float] = None, category: Optional[str] = "commercial", route_polyline: Optional[str] = None) -> str:
    """
    Finds places using Google + OSM data fusion for best accuracy.
    If you don't have exact coordinates, leave lat/lon empty and provide location_name (e.g. 'Kadikoy', 'Rize').
    DO NOT guess coordinates.
    If user is on a route, pass route_polyline or 'LATEST' to enable ETA-based filtering.
    """
    logger.info(f"🧬 [Tool: Hybrid Fusion] Başlatıldı: '{query}' @ {location_name} | {lat},{lon} | Rota: {'Aktif' if route_polyline else 'Pasif'}")
    
    try:
        # 1. Koordinat Çözümleme (LLM halüsinasyonunu engeller)
        if not lat or not lon:
            if location_name:
                resolved = await _resolve_coordinates(location_name)
                if resolved:
                    lat, lon = map(float, resolved.split(","))
                else:
                    return json.dumps({"status": "error", "message": f"{location_name} için koordinat bulunamadı."})
            elif not route_polyline:
                 return json.dumps({"status": "error", "message": "Konum adı (location_name), koordinatlar (lat, lon) veya route_polyline parametrelerinden en az biri gerekli."})

        # 2, 3, 4. VERİ KAYNAKLARINI PARALEL ÇAĞIR (Performans Optimizasyonu)
        semantic_keywords = ["sessiz", "huzurlu", "lüks", "manzara", "vibe", "kitap", "çalışma", "sessizlik", "konfor", "şık"]
        is_semantic = any(k in query.lower() for k in semantic_keywords)
        
        tasks = [
            search_places_google_handler(query, lat or 0.0, lon or 0.0, route_polyline),
            search_infrastructure_osm_handler(lat, lon, category, radius=2000) if (lat and lon) else asyncio.sleep(0, result=[])
        ]
        if is_semantic:
            tasks.append(search_spatial_rag(query, lat or 0.0, lon or 0.0, radius_meters=5000, limit=3))
        
        # Paralel yürütme
        results = await asyncio.gather(*tasks)
        google_raw = results[0]
        osm_raw = results[1]
        rag_results = results[2] if is_semantic else []

        # Google Verisi İşleme
        if isinstance(google_raw, dict) and "error" in google_raw:
            google_places = []
        else:
            google_places = google_raw.get("strict_route_places", []) + google_raw.get("relaxed_route_places", [])
        
        # RAG Verisi İşleme
        rag_places = []
        for r in rag_results:
            if isinstance(r, dict) and "error" not in r:
                r_lat, r_lon = map(float, r["location"].split(","))
                rag_places.append({
                    "name": f"⭐ {r['name']}",
                    "address": r.get("description", "Premium Seçim"),
                    "rating": 5.0,
                    "is_open": "Açık (Önerilen)",
                    "lat": r_lat,
                    "lon": r_lon,
                    "semantic_score": r.get("semantic_score", 0.9),
                    "fusion_status": "RAG Recommended"
                })

        # OSM Verisi İşleme
        osm_places = osm_raw if isinstance(osm_raw, list) and len(osm_raw) > 0 and "error" not in osm_raw[0] and "warning" not in osm_raw[0] else []

        # Eğer kesin koordinat yoksa (Bounding box merkezsiz çizilemez), sadece Google ve RAG sonuçlarını dön
        if not lat or not lon:
            logger.info("ℹ️ Kesin koordinat olmadığı için OSM füzyonu atlanıyor, Google ve RAG sonuçları dönülecek.")
            fused_results = []
            
            # Smart Search (RAG) sonuçlarını ekle
            fused_results.extend(rag_places)

            for g_place in google_places:
                try:
                    g_lat, g_lon = map(float, g_place.get("coords", "0,0").split(","))
                except:
                    continue
                fused_results.append({
                    "name": g_place.get("name"),
                    "address": g_place.get("address"),
                    "rating": g_place.get("rating"),
                    "is_open": str(g_place.get("open_now")),
                    "lat": g_lat,
                    "lon": g_lon,
                    "fusion_status": "Google Route-Only Match"
                })
            
            return json.dumps({
                "places": fused_results,
                "count": len(fused_results),
                "data_source": "Google Places API + RAG (No OSM Fusion)"
            }, ensure_ascii=False)

        fused_results = []
        
        # Smart Search (RAG) sonuçlarını başa ekle
        fused_results.extend(rag_places)

        matched_osm_ids = set()
        
        # 4. Çakıştırma (Füzyon) Algoritması
        for g_place in google_places:
            try:
                g_lat, g_lon = map(float, g_place.get("coords", "0,0").split(","))
            except:
                continue

            fused_place = {
                "name": g_place.get("name"),
                "address": g_place.get("address"),
                "rating": g_place.get("rating"),
                "is_open": str(g_place.get("open_now")),
                "lat": g_lat, # Varsayılan Google geometrisi
                "lon": g_lon,
                "fusion_status": "Google Only" # Başlangıç durumu
            }

            # OSM ile 20 Metre (Sapma) Kontrolü
            for idx, o_place in enumerate(osm_places):
                if idx in matched_osm_ids: continue
                
                o_lat, o_lon = float(o_place.get("lat", 0)), float(o_place.get("lon", 0))
                distance = calculate_distance_meters(g_lat, g_lon, o_lat, o_lon)
                
                if distance <= 20.0:
                    # EŞLEŞME BULUNDU! 
                    # Geometriyi OSM'in kesin fiziksel konumuna (bina ayak izine) kaydır
                    fused_place["lat"] = o_lat
                    fused_place["lon"] = o_lon
                    fused_place["fusion_status"] = "Fused (Google+OSM)"
                    fused_place["osm_distance_shift"] = f"{distance:.1f}m"
                    matched_osm_ids.add(idx)
                    break # Bu mekan için eşleşme tamam, diğer OSM noktalarına bakmaya gerek yok
            
            fused_results.append(fused_place)

        # 5. Eşleşmeyen ve sadece OSM'de bulunan mekanları ekle (Google'da yoksa bile)
        for idx, o_place in enumerate(osm_places):
            if idx not in matched_osm_ids:
                fused_results.append({
                    "name": o_place.get("name", "Bilinmeyen Mekan"),
                    "address": "OSM Altyapısı",
                    "rating": 0.0,
                    "is_open": "Bilinmiyor",
                    "lat": float(o_place.get("lat", 0)),
                    "lon": float(o_place.get("lon", 0)),
                    "fusion_status": "OSM Only"
                })

        logger.success(f"✅ [Tool: Hybrid Fusion] {len(fused_results)} nesne oluşturuldu.")
        return json.dumps({
            "places": fused_results,
            "count": len(fused_results),
            "data_source": "Google + OSM Fusion"
        }, ensure_ascii=False)

    except Exception as e:
        logger.error(f"🔥 [Tool: Hybrid Fusion] Kritik Hata: {e}")
        return json.dumps({"status": "error", "message": str(e)})


# --- 13. POI GERİ BİLDİRİM (KARA LİSTE) ---
@mcp.tool()
async def report_poi_feedback(place_name: str, reason: str, session_id: str = "default") -> str:
    """
    KULLANICI GERİ BİLDİRİMİ: Kullanıcı bir mekanı “kapalıydı” veya “beğenmedim” dediğinde çağır.
    Bu mekanı oturum kara listesine ekler, bir daha önerilmez.
    Kullanıcı herhangi bir mekan veya öneri hakkında olumsuz geıbildirim verdiğinde kullan.
    """
    try:
        blacklist_key = f"poi_blacklist:{session_id}"
        entry = json.dumps({"name": place_name, "reason": reason}, ensure_ascii=False)
        redis_store.client.rpush(blacklist_key, entry)
        redis_store.client.expire(blacklist_key, 86400 * 7)  # 7 gün
        logger.info(f"🚫 [Feedback] '{place_name}' kara listeye eklendi. Neden: {reason}")
        return json.dumps({
            "status": "ok",
            "message": f"'​{place_name}' kara listeye eklendi. Bir daha önerilmeyecek.",
            "session_id": session_id
        }, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "report_poi_feedback")


@mcp.tool()
async def get_poi_blacklist(session_id: str = "default") -> str:
    """
    Oturuma ait POI kara listesini getirir. Mekan önerisi yapmadan önce bu listeyi kontrol et.
    Kara listedeki mekanlar hiçbir koşulda tekrar önerilmemeli.
    """
    try:
        blacklist_key = f"poi_blacklist:{session_id}"
        items = redis_store.client.lrange(blacklist_key, 0, -1)
        blacklist = [json.loads(item) for item in items] if items else []
        return json.dumps({
            "blacklisted_places": blacklist,
            "count": len(blacklist)
        }, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "get_poi_blacklist")


# --- 14. ROTA ÖZET KARTI ---
@mcp.tool()
async def build_route_summary(
    route_data: str,
    radar_data: Optional[str] = None,
    toll_data: Optional[str] = None,
    weather_data: Optional[str] = None
) -> str:
    """
    ROTA ÖZET KARTI: Rota hesaplandıktan sonra mesafe, süre, radar sayısı,
    geçiş ücreti ve hava durumu bilgilerini birleştirip Markdown özet kart oluşturur.
    Rota araçları tamamlandıktan sonra mutlaka bu aracı çağır.
    """
    try:
        import json as _json
        def _ensure_dict(val):
            if val is None: return None
            if isinstance(val, (dict, list)): return val
            if isinstance(val, str):
                if val.strip() == "" or val.strip() == "None": return None
                try: return _json.loads(val)
                except: return {"text": val}
            return val

        rd = _ensure_dict(route_data) or {}
        rr = _ensure_dict(radar_data)
        td = _ensure_dict(toll_data)
        wd = _ensure_dict(weather_data)
        
        result = build_route_summary_handler(rd, rr, td, wd)
        return _json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "build_route_summary")


# --- 15. EV ŞARJ İSTASYONLARI ---
@mcp.tool()
async def get_ev_charging_stations(route_polyline: str) -> str:
    """
    EV ŞARJ İSTASYONLARI: Rota boyunca elektrikli araç şarj noktalarını bulur (OSM).
    Kullanıcının aracı elektrikli ise veya şarj noktası soran kullanıcıya uygulanmalıdır.
    'LATEST' kullanılabilir.
    """
    try:
        result = await get_ev_charging_handler(route_polyline)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "get_ev_charging_stations")


# --- 16. CANLI TRAFİK DURUMU ---
@mcp.tool()
async def get_route_traffic(route_polyline: str) -> str:
    """
    CANLI TRAFİK: HERE Traffic Flow API'dan rota üzerindeki gerçek zamanlı trafik
    yoğunluğunu çeker. Sıkışıklık seviyesi, hız ve gecikme bilgisi döner.
    Rota hesaplandıktan sonra 'LATEST' ile otomatik çağrılabilir.
    """
    try:
        result = await get_route_traffic_handler(route_polyline)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "get_route_traffic")


# --- 17. ARAÇ PROFİLİ GÜNCELLEME ---
@mcp.tool()
async def update_vehicle_profile(
    brand: str,
    model: str,
    year: int,
    city_consumption: float,
    highway_consumption: float,
    fuel_type: str = "gasoline",
    username: str = "test_pilot"
) -> str:
    """
    Updates user vehicle profile (brand, model, year, fuel consumption).
    Use when user says 'update my car' or 'change fuel type'.
    """
    try:
        # Route to orchestrator via the standard MCP flow
        return json.dumps({
            "status": "error",
            "message": "Vehicle profile update must be called through the orchestrator local tools, not mcp_city."
        }, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "update_vehicle_profile")


if __name__ == "__main__":
    logger.info("🚀 City Agent v2.0 (MCP) Port 8000 üzerinde başlatılıyor...")
    mcp.run(transport="sse", host="0.0.0.0", port=8000)